"""What each beat's visual has to prove before it is allowed on screen.

Run 99db2deb's final gate rejected a finished video for "showing the wrong
player for the match-winning goal". Every layer below had approved that photo,
and each was right on its own terms: it was a real photograph, of a real Real
Madrid player, from a real Champions League match. It was Camavinga, and the
narration was about Rodrygo.

That is the gap this module closes. Relevance was being judged against the
*topic* -- club, competition, stadium, "football" -- when it has to be judged
against the **current narration beat**. So before anything is sourced, each
beat gets a written requirement derived from the grounded script:

    who        the person the beat is about, if any
    team       the club context that person is in for this fixture
    fixture    the grounded match, and its date
    action     what is being described at this moment
    exactness  whether the picture must be *of that event*, or may merely be
               temporally-correct context

and the rules for what may stand in when no photograph satisfies it.

The requirement is derived deterministically and locally -- no model call --
so the same beat always produces the same contract, and the contract can be
handed to the candidate selector, the review gate and the tests as one string
they all agree on.

Nothing here loosens a factual threshold. It is strictly a narrowing: a photo
that would previously have passed on club-and-competition alone now has to
match the beat as well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.visual_router import BeatClass, classify_beat, _named_person_in

__all__ = [
    "VisualRequirement",
    "derive_requirement",
    "requirement_block",
]

# Actions that make a picture a claim about a specific moment. If the beat is
# about one of these, a photograph of the same people on a different day is
# not context -- shown under this narration it reads as a record of the event.
_EXACT_EVENT_ACTIONS = (
    ("goal", ("goal", "scores", "scoring", "winner", "equaliser", "equalizer",
              "هدف", "يسجل", "تسجيل", "التعادل")),
    ("celebration", ("celebrat", "احتفال", "يحتفل")),
    ("substitution", ("substitut", "sub board", "تبديل", "بديل")),
    ("card", ("red card", "yellow card", "booking", "بطاقة")),
    ("penalty", ("penalty", "spot kick", "ركلة جزاء")),
    ("save", ("save", "tackle", "block", "تصدي", "تدخل")),
    ("final whistle", ("final whistle", "full time", "صافرة النهاية")),
    ("scoreline", ("scoreboard", "score line", "لوحة النتائج")),
)


@dataclass(frozen=True)
class VisualRequirement:
    """The contract one beat's visual must satisfy."""

    section_id: int
    sub_index: int
    beat: BeatClass
    person: str = ""
    team: str = ""
    fixture: str = ""
    date_text: str = ""
    action: str = ""
    exact_event_required: bool = False

    @property
    def slot_label(self) -> str:
        return f"s{self.section_id}.{self.sub_index}"

    @property
    def card_allowed(self) -> bool:
        """A grounded card is always an acceptable last answer.

        There is no beat where a truthful card is worse than a wrong
        photograph, which is the whole point of the ladder having a floor.
        """
        return True

    @property
    def context_photo_allowed(self) -> bool:
        """Whether a temporally-correct non-event photo may stand in.

        Allowed only when the narration does not imply the picture *is* the
        event. Under "Rodrygo scored the winner", a photo of Rodrygo from
        another match is presented as that goal; under "Rodrygo came on", a
        current photo of Rodrygo is simply who is being discussed.
        """
        return not self.exact_event_required


def _action_in(text: str) -> str:
    lowered = text.lower()
    for label, needles in _EXACT_EVENT_ACTIONS:
        if any(needle in lowered for needle in needles):
            return label
    return ""


def _team_for(text: str, person: str, clubs: set[str], slot_props: dict) -> str:
    """The club this beat's subject belongs to, when the run established it."""
    declared = str((slot_props or {}).get("football_current_club") or "").strip()
    if declared:
        return declared
    lowered = text.lower()
    # The club named closest to the person is the one meant; with only one
    # club mentioned there is no ambiguity to resolve.
    mentioned = [club for club in clubs if club and club.lower() in lowered]
    return mentioned[0] if len(mentioned) == 1 else ""


def derive_requirement(
    *,
    section,
    slot,
    sub_index: int,
    facts=None,
    known_people: set[str] | None = None,
    known_clubs: set[str] | None = None,
) -> VisualRequirement:
    """Build the contract for one beat from the grounded script.

    `facts` is the run's GroundedFacts, used only for the fixture and its
    date -- the two things that are properties of the match rather than of the
    beat. Everything else comes from what this beat actually says.
    """
    props = dict(getattr(slot, "props", None) or {})
    prompt = str(getattr(slot, "prompt", "") or "")
    keywords = str(getattr(slot, "keywords", "") or "")
    narration = str(getattr(section, "narration", "") or "")
    raw = f"{prompt} {keywords}"

    beat = classify_beat(
        prompt=prompt, keywords=keywords, narration=narration, props=props,
        known_people=known_people, known_clubs=known_clubs,
    )

    person = str(props.get("football_player_name") or "").strip()
    if not person and beat is BeatClass.NAMED_REAL_PERSON:
        person = _named_person_in(raw, known_people, known_clubs) or \
            _named_person_in(narration, known_people, known_clubs)

    clubs = set(known_clubs or ())
    team = _team_for(raw, person, clubs, props)

    fixture = ""
    date_text = ""
    if facts is not None:
        home, away = getattr(facts, "home", ""), getattr(facts, "away", "")
        if home and away:
            fixture = f"{home} v {away}"
        date_text = getattr(facts, "date_text", "") or ""

    # The action can be named by the beat or by the sentence it sits under;
    # the beat wins, because the narration covers several seconds.
    action = _action_in(raw) or _action_in(narration)

    exact = bool(action) and beat in {
        BeatClass.EXACT_MATCH_EVENT,
        BeatClass.NAMED_REAL_PERSON,
        BeatClass.FACT_STAT_SCORE,
    }

    return VisualRequirement(
        section_id=getattr(section, "id", 0),
        sub_index=sub_index,
        beat=beat,
        person=person,
        team=team,
        fixture=fixture,
        date_text=date_text,
        action=action,
        exact_event_required=exact,
    )


def requirement_block(requirement: VisualRequirement) -> str:
    """The contract as instructions for whichever model is judging the image.

    Written as requirements rather than hints. The candidate selector and the
    review gate get the identical text, so the two cannot disagree about what
    the beat needed -- which is how a photo passed selection and review and
    was then caught by the final gate.
    """
    lines: list[str] = ["THIS BEAT'S VISUAL REQUIREMENT (all of it must hold):"]

    if requirement.person:
        lines.append(
            f"- The subject is {requirement.person}. The image must show "
            f"{requirement.person} specifically. A different player is NEVER "
            f"acceptable, however famous, however good the photo, and however "
            f"well the club or competition matches. A generic squad or crowd "
            f"shot is not {requirement.person}."
        )
    if requirement.team:
        lines.append(
            f"- The club context is {requirement.team}. Reject a photo showing "
            f"the wrong club's colours or crest."
        )
    if requirement.fixture:
        when = f" on {requirement.date_text}" if requirement.date_text else ""
        lines.append(f"- The fixture is {requirement.fixture}{when}.")
    if requirement.action:
        lines.append(f"- The moment being described is: {requirement.action}.")

    if requirement.exact_event_required:
        lines.append(
            "- EXACT EVENT REQUIRED. The narration presents this image as the "
            "moment itself, so only imagery from that event qualifies. A "
            "correct player from a different match, or the same fixture in a "
            "different season, is a false record here -- reject it. If no "
            "image of the event is available, reject them all: a grounded "
            "information card will be drawn instead, and that is the better "
            "outcome."
        )
    else:
        lines.append(
            "- Exact-event imagery is preferred but not required, because the "
            "narration does not claim this picture is the moment itself. A "
            "temporally-correct photograph of the right subject is acceptable. "
            "It must still be the right subject."
        )

    lines.append(
        "- Do NOT accept an image merely because the club, competition, "
        "stadium or general football topic matches. Topic match is not beat "
        "match."
    )
    lines.append(
        "- A real photograph is not automatically better than no photograph. "
        "Reject: the wrong person; the wrong club; a materially wrong era or "
        "season; an unrelated match presented as this one; anything too small "
        "or blurred to fill a vertical frame; stock watermarks or burned-in "
        "site banners; training-course, lineup-builder or fantasy graphics; "
        "AI-generated search junk; and any image whose readable text would "
        "mislead a viewer about who or what this is."
    )
    return "\n".join(lines)
