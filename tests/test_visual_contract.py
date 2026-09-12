"""Every beat's visual has to answer to that beat, not to the topic.

The failure this exists for is the one the final gate caught on run 99db2deb:
"showing the wrong player for the match-winning goal". Every layer had approved
that photograph and each was right on its own terms -- a real photo, a real
Real Madrid player, a real Champions League match. It was Camavinga, and the
narration was about Rodrygo.

So the contract is derived from the beat, and the candidate selector and the
review gate are handed the identical text. A picture can no longer pass one and
fail the other for a reason the first was never told about.
"""

import pytest

import prompts
from core.visual_contract import (
    VisualRequirement,
    derive_requirement,
    requirement_block,
)
from core.visual_router import BeatClass
from core.utils import ScriptSection, VisualSlot

CLUBS = {"Real Madrid", "Inter Milan"}
PEOPLE = {"Rodrygo", "Carlo Ancelotti", "Alessandro Bastoni"}


class _Facts:
    home = "Real Madrid"
    away = "Inter Milan"
    date_text = "Sep 8, 2026"


def _requirement(keywords="", prompt="", narration="", props=None, sub=1):
    section = ScriptSection(id=3, narration=narration or "لقطة من المباراة.")
    slot = VisualSlot(visual="google_photo", prompt=prompt, keywords=keywords,
                      props=props or {})
    return derive_requirement(
        section=section, slot=slot, sub_index=sub, facts=_Facts(),
        known_people=PEOPLE, known_clubs=CLUBS,
    )


# ── what the contract says ───────────────────────────────────────────

def test_the_contract_names_the_person_the_beat_is_about():
    req = _requirement(keywords="Rodrygo running onto the pitch")
    assert req.person == "Rodrygo"
    assert req.beat is BeatClass.NAMED_REAL_PERSON
    assert req.fixture == "Real Madrid v Inter Milan"
    assert req.date_text == "Sep 8, 2026"


def test_a_declared_player_beats_the_matcher():
    req = _requirement(
        keywords="anything",
        props={"football_subject": "player",
               "football_player_name": "Vinicius Junior",
               "football_current_club": "Real Madrid"},
    )
    assert req.person == "Vinicius Junior"
    assert req.team == "Real Madrid"


def test_the_club_is_taken_only_when_it_is_unambiguous():
    """Two clubs in one keyword string names neither as *the* context."""
    assert _requirement(keywords="Rodrygo Real Madrid sideline").team == "Real Madrid"
    assert _requirement(
        keywords="Real Madrid vs Inter Milan midfield battle"
    ).team == ""


# ── exactness: the distinction the brief turns on ────────────────────

@pytest.mark.parametrize("keywords", [
    "Rodrygo scores the winning goal",
    "Rodrygo celebrates the winner",
    "the substitution board shows Rodrygo coming on",
    "scoreboard showing the final score",
])
def test_a_beat_about_a_moment_requires_the_moment(keywords):
    req = _requirement(keywords=keywords)
    assert req.exact_event_required is True
    assert req.context_photo_allowed is False


def test_a_beat_that_merely_discusses_someone_accepts_context():
    """"Ancelotti is a thoughtful coach" is not a claim about a moment."""
    req = _requirement(keywords="Carlo Ancelotti portrait on the touchline")
    assert req.exact_event_required is False
    assert req.context_photo_allowed is True


def test_a_stadium_beat_never_demands_exact_event_imagery():
    req = _requirement(keywords="San Siro stadium exterior at night")
    assert req.beat is BeatClass.STADIUM_LOCATION
    assert req.exact_event_required is False


def test_the_action_can_come_from_the_narration():
    req = _requirement(
        keywords="Rodrygo",
        narration="ثم جاء الهدف الفائز في الدقائق الأخيرة.",
    )
    assert req.action != ""
    assert req.exact_event_required is True


def test_a_card_is_always_an_acceptable_answer():
    """There is no beat where a truthful card loses to a wrong photograph."""
    for keywords in ("Rodrygo scores", "San Siro", "the ball on the grass"):
        assert _requirement(keywords=keywords).card_allowed is True


# ── the text handed to both judges ───────────────────────────────────

def test_the_block_forbids_substituting_another_player():
    block = requirement_block(_requirement(keywords="Rodrygo receives the ball"))
    assert "The subject is Rodrygo" in block
    assert "A different player is NEVER" in block
    assert "generic squad or crowd shot is not Rodrygo" in block


def test_the_block_refuses_topic_matching():
    block = requirement_block(_requirement(keywords="Rodrygo receives the ball"))
    assert "Topic match is not beat" in block
    assert "merely because the club, competition" in block


def test_an_exact_event_beat_says_a_card_is_the_better_outcome():
    block = requirement_block(_requirement(keywords="Rodrygo scores the winner"))
    assert "EXACT EVENT REQUIRED" in block
    assert "different match" in block
    assert "better" in block


def test_a_context_beat_says_so_explicitly():
    block = requirement_block(_requirement(keywords="Carlo Ancelotti on the touchline"))
    assert "not required" in block
    assert "still be the right subject" in block


def test_the_block_carries_the_quality_floor():
    """A real photograph is not automatically better than no photograph."""
    block = requirement_block(_requirement(keywords="San Siro at night"))
    for reject in (
        "wrong person", "wrong club", "wrong era", "watermark",
        "training-course", "AI-generated search junk", "readable text",
    ):
        assert reject in block, reject
    assert "not automatically better" in block


def test_a_beat_with_no_person_does_not_invent_one():
    block = requirement_block(_requirement(keywords="the ball on the penalty spot"))
    assert "The subject is" not in block


# ── both judges get the same text ────────────────────────────────────

def test_the_selector_prompt_carries_the_contract():
    block = requirement_block(_requirement(keywords="Rodrygo scores the winner"))
    prompt = prompts.pexels_candidate_selection_prompt(
        keywords="k", prompt="p", num_images=4, narration="n", requirement=block,
    )
    assert "The subject is Rodrygo" in prompt
    assert "EXACT EVENT REQUIRED" in prompt


def test_the_review_prompt_carries_the_same_contract():
    block = requirement_block(_requirement(keywords="Rodrygo scores the winner"))
    prompt = prompts.image_review_prompt([{
        "section_id": 3, "sub_image_index": 1, "narration": "n",
        "visual_type": "google_photo", "image_search_keywords": "k",
        "image_filename": "section_003_01.jpg", "requirement": block,
    }])
    assert "The subject is Rodrygo" in prompt
    assert "EXACT EVENT REQUIRED" in prompt


def test_a_beat_with_no_contract_changes_neither_prompt():
    """Channels without grounded football facts are untouched."""
    selector = prompts.pexels_candidate_selection_prompt(
        keywords="k", prompt="p", num_images=2, narration="n",
    )
    assert "THIS BEAT'S VISUAL REQUIREMENT" not in selector
    review = prompts.image_review_prompt([{
        "section_id": 1, "sub_image_index": 1, "narration": "n",
        "visual_type": "google_photo", "image_search_keywords": "k",
        "image_filename": "section_001_01.jpg",
    }])
    assert "THIS BEAT'S VISUAL REQUIREMENT" not in review


def test_the_contract_is_deterministic():
    a = requirement_block(_requirement(keywords="Rodrygo scores the winner"))
    b = requirement_block(_requirement(keywords="Rodrygo scores the winner"))
    assert a == b


# ── wiring ───────────────────────────────────────────────────────────

def test_every_beat_gets_a_contract_and_the_sourcer_publishes_it():
    import inspect
    from core import image_sourcer

    source = inspect.getsource(image_sourcer)
    # Derived once per run, before anything is searched...
    assert "_REQUIREMENTS_BY_STEM[f\"section_{_sid:03d}_{_sub:02d}\"]" in source
    # ...published for the selector on the beat's own task...
    assert "_ACTIVE_REQUIREMENT.set(" in source
    # ...and read back where the candidate is chosen.
    assert "requirement=_ACTIVE_REQUIREMENT.get()" in source


def test_the_requirement_is_per_task_not_a_shared_global():
    """Beats source concurrently; a plain global would cross-contaminate."""
    import contextvars
    from core import image_sourcer

    assert isinstance(image_sourcer._ACTIVE_REQUIREMENT, contextvars.ContextVar)
