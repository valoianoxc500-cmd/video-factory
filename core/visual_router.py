"""Decide what kind of thing a beat is *before* spending anything on it.

Every Football beat used to go down one pipeline: hunt the web for an archival
photograph, run a vision selector over the candidates, and fail the beat if
nothing survived. That is the right pipeline for "Rodrygo scores the winner"
and a waste of money and reliability for "floodlights over a stadium" -- and on
run 99db2deb it was the second kind of beat that burned the quota which then
starved the first kind.

So each beat is classified first, and the class chooses the ladder:

    NAMED_REAL_PERSON   a specific footballer, coach, referee is on screen
    EXACT_MATCH_EVENT   a moment from the actual grounded match
    STADIUM_LOCATION    a real ground, inside or out
    GENERIC_FOOTBALL    a ball, a net, a tunnel, a crowd -- no identity at all
    FACT_STAT_SCORE     a scoreline, a table, a statistic
    OTHER_SAFE_VISUAL   anything else

Classification is deterministic and local: no model call, no network, and the
same beat always lands in the same class, so the routing decision is auditable
and testable rather than another thing that can be rate-limited.

The separation this encodes is the product rule behind it: **facts stay
grounded, but visual representation is allowed to be generated.** A generated
stadium under a truthful sentence is honest; a generated *fact* is not, and no
class here ever authorises one. Anything showing a real person or a real event
keeps its evidence requirements -- see `generation_is_safe`.
"""

from __future__ import annotations

import re
from enum import Enum

__all__ = [
    "BeatClass",
    "classify_beat",
    "generation_is_safe",
    "prefers_cheap_generation",
    "search_attempt_budget",
]


class BeatClass(str, Enum):
    NAMED_REAL_PERSON = "named_real_person"
    EXACT_MATCH_EVENT = "exact_match_event"
    STADIUM_LOCATION = "stadium_location"
    GENERIC_FOOTBALL = "generic_football"
    FACT_STAT_SCORE = "fact_stat_score"
    OTHER_SAFE_VISUAL = "other_safe_visual"


# ── vocabulary ───────────────────────────────────────────────────────
#
# Arabic alongside English throughout: the scripts these classify are written
# in Arabic, and an English-only matcher put every beat in OTHER_SAFE_VISUAL.

_SCORE_WORDS = (
    "scoreboard", "score line", "scoreline", "final score",
    "league table", "standings", "statistics", "stat sheet", "possession %",
    "match stats", "result graphic", "score graphic", "timeline of the match",
    "لوحة النتائج", "النتيجة النهائية", "لوحة نتائج", "الإحصائيات", "إحصائيات",
    "جدول الترتيب", "نسبة الاستحواذ",
)

_STADIUM_WORDS = (
    "stadium", "arena", "san siro", "bernabeu", "bernabéu", "giuseppe meazza",
    "stadium exterior", "pitch", "turf", "floodlight", "stands", "terraces",
    "ملعب", "الملعب", "سان سيرو", "برنابيو", "المدرجات", "أرضية الملعب",
    "الأضواء الكاشفة",
)

_GENERIC_WORDS = (
    "football", "soccer ball", "the ball", "goal net", "net", "goalpost",
    "tunnel", "dressing room", "locker room", "crowd", "supporters", "fans",
    "atmosphere", "tactical board", "tactics board", "whiteboard", "formation",
    "diagram", "abstract background", "logos", "badge", "kit", "boots",
    "corner flag", "referee whistle", "scarf",
    "كرة", "الكرة", "شباك", "المرمى", "النفق", "غرفة الملابس", "الجماهير",
    "المشجعين", "أجواء", "لوحة تكتيكية", "تشكيل", "مخطط", "خلفية", "شعار",
)

_EVENT_WORDS = (
    "goal", "scores", "scoring", "celebration", "celebrates", "tackle",
    "save", "penalty", "free kick", "corner", "substitution", "red card",
    "yellow card", "kick-off", "final whistle", "winning goal", "assist",
    "هدف", "الهدف", "يسجل", "تسجيل", "احتفال", "يحتفل", "تدخل", "تصدي",
    "ركلة جزاء", "ركلة حرة", "ركنية", "تبديل", "بطاقة حمراء", "بطاقة صفراء",
    "صافرة النهاية", "الهدف الفائز",
)

_ROLE_WORDS = (
    "player", "footballer", "coach", "manager", "goalkeeper", "keeper",
    "referee", "captain", "striker", "defender", "midfielder", "substitute",
    "لاعب", "اللاعب", "مدرب", "المدرب", "حارس", "الحارس", "حكم", "الحكم",
    "قائد", "مهاجم", "مدافع",
)

# A capitalised one-to-three word Latin run: "Vinicius Junior", "Simone
# Inzaghi", "Rodrygo".
_LATIN_NAME_RE = re.compile(
    r"\b([A-Z][a-zà-öø-ÿ'’-]{2,}(?:\s+[A-Z][a-zà-öø-ÿ'’-]{2,}){0,2})\b"
)

# Capitalised words that are never a person, whatever the matcher thinks. Every
# one of these was a real misclassification on run 99db2deb's own script:
# "San Siro" and "UEFA Champions League" were routed down the named-person
# ladder, which is both the most expensive one and the wrong one.
_NOT_A_PERSON = {
    # competitions and governing bodies
    "uefa", "fifa", "champions", "league", "europa", "premier", "liga",
    "laliga", "serie", "bundesliga", "ligue", "world", "cup", "copa", "del",
    "rey", "supercopa", "classico", "clasico", "derby",
    # grounds
    "san", "siro", "bernabeu", "bernabéu", "santiago", "giuseppe", "meazza",
    "stadium", "arena", "stadio", "estadio",
    # generic nouns a headline capitalises
    "game", "analysis", "final", "score", "result", "match", "photo",
    "photograph", "image", "video", "highlights", "report", "news", "sports",
    "football", "soccer", "team", "squad", "club", "fans", "crowd", "goal",
    "the", "and", "vs", "versus", "mid", "full", "time", "half", "first",
    "second", "minute", "extra", "penalty", "shootout",
    # Sentence-initial words a visual brief routinely capitalises. "Abstract
    # background with Real Madrid and Inter Milan logos" was classified as a
    # beat about somebody named Abstract, which sent it down the
    # named-person ladder and then looked for that person's face.
    "abstract", "close", "wide", "panoramic", "aerial", "dramatic", "closeup",
    "shot", "view", "scene", "background", "graphic", "illustration", "banner",
    "tactical", "board", "diagram", "stadium", "pitch", "crowd", "atmosphere",
    "celebration", "action", "moment", "during", "before", "after",
}


def _blocked_tokens(known_clubs: set[str] | None) -> set[str]:
    """Words that cannot, on their own, indicate a person.

    The run's clubs come from its grounded facts rather than a hardcoded list,
    so "Inter Milan" blocks correctly for this run without the module needing
    to know every club in Europe.
    """
    blocked = set(_NOT_A_PERSON)
    for club in known_clubs or ():
        blocked.update(w.lower() for w in re.findall(r"[^\W\d_]+", club))
    return blocked

# How many search attempts a class is worth before generation is the better
# answer. A named person or a specific event deserves the full ladder; a
# stadium or a football does not, and spending one there is what exhausted the
# quota the specific beats needed.
_SEARCH_BUDGET = {
    BeatClass.NAMED_REAL_PERSON: 3,
    BeatClass.EXACT_MATCH_EVENT: 3,
    BeatClass.STADIUM_LOCATION: 1,
    BeatClass.GENERIC_FOOTBALL: 1,
    BeatClass.FACT_STAT_SCORE: 0,
    BeatClass.OTHER_SAFE_VISUAL: 2,
}

#: Classes whose visual carries no claim about a specific person or event, so a
#: generated picture under it states nothing that could be false.
_SAFE_TO_GENERATE_FREELY = {
    BeatClass.GENERIC_FOOTBALL,
    BeatClass.STADIUM_LOCATION,
    BeatClass.OTHER_SAFE_VISUAL,
}


def _haystack(*parts: str) -> str:
    return " ".join(str(p or "") for p in parts).lower()


def _mentions(text: str, words) -> bool:
    return any(word.lower() in text for word in words)


def classify_beat(
    *,
    prompt: str = "",
    keywords: str = "",
    narration: str = "",
    props: dict | None = None,
    known_people: set[str] | None = None,
    known_clubs: set[str] | None = None,
) -> BeatClass:
    """Which kind of visual this beat needs.

    `known_people` and `known_clubs` come from the run's grounded facts, not
    from a hardcoded list. The club set matters as much as the people set: a
    beat whose prompt says "Real Madrid" is not a beat about a person, and
    without the clubs the capitalised-name matcher classifies every one of them
    as NAMED_REAL_PERSON and sends it down the most expensive ladder.

    Order is significance, not convenience. A scoreboard beat is a scoreboard
    beat even though it also says "Real Madrid"; a beat naming a player is a
    person beat even though it also says "stadium".
    """
    props = props or {}
    # Two forms of the same string: the capitalised-name matcher needs the
    # original casing, everything else matches case-insensitively.
    raw = " ".join(str(p or "") for p in (prompt, keywords))
    text = raw.lower()
    # Narration gives context for role words but is much longer than the
    # prompt, so it is consulted only where the prompt is ambiguous.
    raw_context = str(narration or "")

    # The script can say so outright; football_subject is already set by the
    # scripter for player beats and is more reliable than any matcher.
    subject = str(props.get("football_subject") or "").strip().lower()
    if subject == "player":
        return BeatClass.NAMED_REAL_PERSON
    if subject in {"scoreboard", "stat", "score"}:
        return BeatClass.FACT_STAT_SCORE

    if _mentions(text, _SCORE_WORDS):
        return BeatClass.FACT_STAT_SCORE

    if _named_person_in(raw, known_people, known_clubs):
        return BeatClass.NAMED_REAL_PERSON
    # "the goalkeeper dives" with no name is still a person beat only if the
    # narration is about a specific one; otherwise it is generic football.
    if _mentions(text, _ROLE_WORDS) and _named_person_in(
        raw_context, known_people, known_clubs
    ):
        return BeatClass.NAMED_REAL_PERSON

    if _mentions(text, _EVENT_WORDS):
        return BeatClass.EXACT_MATCH_EVENT

    if _mentions(text, _STADIUM_WORDS):
        return BeatClass.STADIUM_LOCATION

    if _mentions(text, _GENERIC_WORDS):
        return BeatClass.GENERIC_FOOTBALL

    return BeatClass.OTHER_SAFE_VISUAL


def _named_person_in(
    text: str,
    known_people: set[str] | None,
    known_clubs: set[str] | None,
) -> str:
    """The person this text is about, or "".

    Takes the text in its original casing. Checked against the run's own people
    first; the capitalised-run matcher is the fallback for a name research
    never listed, and it has nothing to match on in a lowercased string.
    """
    lowered = text.lower()
    for person in known_people or ():
        if person and person.lower() in lowered:
            return person

    blocked = _blocked_tokens(known_clubs)
    for match in _LATIN_NAME_RE.finditer(text):
        candidate = match.group(1)
        words = [w.lower() for w in re.findall(r"[^\W\d_]+", candidate)]
        # A capitalised run made entirely of club, ground, competition or
        # headline words is not a person -- "San Siro", "UEFA Champions
        # League", "Real Madrid", "Final Score".
        if not words or all(word in blocked for word in words):
            continue
        # Strip the blocked words and keep what is left: "Rodrygo" out of
        # "Rodrygo Real Madrid", which is how the keyword strings are written.
        remaining = [w for w in candidate.split() if w.lower() not in blocked]
        return " ".join(remaining) if remaining else candidate
    return ""


def search_attempt_budget(beat: BeatClass) -> int:
    """How many real-photo attempts this class earns before generation."""
    return _SEARCH_BUDGET.get(beat, 2)


def prefers_cheap_generation(beat: BeatClass) -> bool:
    """Whether a cheap text-to-image generator is the sensible second step.

    True only where the picture carries no identity and no event claim, so the
    cheapest generator is not a quality compromise -- there is nothing for a
    more expensive one to get right.
    """
    return beat in {BeatClass.GENERIC_FOOTBALL, BeatClass.STADIUM_LOCATION}


def generation_is_safe(beat: BeatClass, *, has_identity_reference: bool) -> bool:
    """Whether this beat may be filled by a generated image at all.

    The line the whole module exists to draw. A stadium, a ball, a crowd: yes,
    freely -- the image asserts nothing. A named real person: only with a
    verified identity reference in hand, so the generator is depicting the
    person the narration names rather than inventing a lookalike, and the
    result is still recorded as a reconstruction.

    EXACT_MATCH_EVENT and FACT_STAT_SCORE are never generated. A generated
    picture of a goal being scored *is* a fabricated record of the event, which
    is exactly the thing that must never happen however grounded the match is.
    Those beats fall through to the verified information card instead.
    """
    if beat in _SAFE_TO_GENERATE_FREELY:
        return True
    if beat is BeatClass.NAMED_REAL_PERSON:
        return has_identity_reference
    return False
