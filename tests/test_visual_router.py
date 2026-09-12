"""Classifying a beat before spending anything on it.

Every case here is a real beat from run 99db2deb's own script. The router
exists because that run sent all twenty-three of them down one archival-photo
pipeline: the stadium beats and the crowd beats burned the Gemini quota, and
the beats that genuinely needed a specific photograph then failed for want of
it.

The line the router draws is not "cheap vs expensive". It is **what a picture
would assert**. A generated stadium says nothing that could be false. A
generated goal is a fabricated record of an event, and a generated player who
is not that player is the wrong-person error the final gate already caught on
this run. So `generation_is_safe` is the assertion most of this file is about.
"""

import pytest

from core.visual_router import (
    BeatClass,
    classify_beat,
    generation_is_safe,
    prefers_cheap_generation,
    search_attempt_budget,
)

CLUBS = {"Real Madrid", "Inter Milan"}
PEOPLE = {"Rodrygo", "Carlo Ancelotti", "Alessandro Bastoni"}


def route(prompt="", keywords="", narration="", props=None):
    return classify_beat(
        prompt=prompt, keywords=keywords, narration=narration, props=props,
        known_people=PEOPLE, known_clubs=CLUBS,
    )


# ── the classifications that were wrong before ───────────────────────

@pytest.mark.parametrize("keywords", [
    "San Siro stadium atmosphere Real Madrid vs Inter Milan",
    "stadium exterior at night",
    "floodlights over the pitch",
])
def test_a_ground_is_a_place_not_a_person(keywords):
    """"San Siro" is two capitalised words and is not somebody."""
    assert route(keywords=keywords) is BeatClass.STADIUM_LOCATION


@pytest.mark.parametrize("keywords", [
    "UEFA Champions League logo",
    "Real Madrid badge and colours",
    "Inter Milan defensive shape",
])
def test_a_competition_or_club_is_not_a_person(keywords):
    assert route(keywords=keywords) is not BeatClass.NAMED_REAL_PERSON


@pytest.mark.parametrize("keywords,expected", [
    ("Alessandro Bastoni tackle Real Madrid", "Alessandro Bastoni"),
    ("Carlo Ancelotti thoughtful on the sideline", "Carlo Ancelotti"),
    ("Rodrygo running onto the pitch Real Madrid", "Rodrygo"),
    ("Simone Inzaghi coaching Inter Milan", "Simone Inzaghi"),
])
def test_a_named_footballer_is_recognised(keywords, expected):
    """Including a single-name player, which the two-word matcher missed."""
    assert route(keywords=keywords) is BeatClass.NAMED_REAL_PERSON


@pytest.mark.parametrize("prompt", [
    "Abstract background with Real Madrid and Inter Milan logos",
    "Wide shot of the stands",
    "Tactical board showing the formation",
    "Dramatic closeup of the ball",
])
def test_a_capitalised_first_word_is_not_a_person(prompt):
    """"Abstract background..." was routed as a beat about someone named Abstract."""
    assert route(prompt=prompt) is not BeatClass.NAMED_REAL_PERSON


def test_a_role_word_alone_is_not_a_person_beat():
    """"the defenders form a wall" names nobody."""
    beat = route(keywords="defensive wall in front of the penalty area")
    assert beat is not BeatClass.NAMED_REAL_PERSON


def test_a_role_word_becomes_a_person_beat_when_the_narration_names_one():
    beat = route(
        keywords="the coach gives instructions",
        narration="Carlo Ancelotti changed the game with one substitution.",
    )
    assert beat is BeatClass.NAMED_REAL_PERSON


def test_the_script_can_say_outright_that_a_beat_is_a_player():
    beat = route(keywords="anything at all", props={"football_subject": "player"})
    assert beat is BeatClass.NAMED_REAL_PERSON


@pytest.mark.parametrize("keywords", [
    "scoreboard showing the final score",
    "league table standings",
    "لوحة النتائج في الملعب",
])
def test_a_scoreline_beat_is_a_fact_beat(keywords):
    assert route(keywords=keywords) is BeatClass.FACT_STAT_SCORE


@pytest.mark.parametrize("keywords", [
    "the winning goal is scored",
    "celebration after the goal",
    "penalty awarded",
])
def test_a_moment_of_the_match_is_an_event_beat(keywords):
    assert route(keywords=keywords) is BeatClass.EXACT_MATCH_EVENT


@pytest.mark.parametrize("keywords", [
    "tactical board with the formation",
    "dressing room before kickoff",
    "close up of the ball on the grass",
])
def test_identity_free_football_is_generic(keywords):
    assert route(keywords=keywords) in {
        BeatClass.GENERIC_FOOTBALL, BeatClass.OTHER_SAFE_VISUAL
    }


def test_arabic_prompts_classify_too():
    """The scripts are Arabic; an English-only matcher classified nothing."""
    assert route(prompt="صورة بانورامية لملعب سان سيرو") is BeatClass.STADIUM_LOCATION
    assert route(prompt="لقطة الهدف الفائز") is BeatClass.EXACT_MATCH_EVENT


# ── what may and may not be generated ────────────────────────────────

def test_an_event_is_never_generated():
    """A generated goal is a fabricated record, however grounded the match."""
    for reference in (True, False):
        assert generation_is_safe(
            BeatClass.EXACT_MATCH_EVENT, has_identity_reference=reference
        ) is False


def test_a_scoreline_is_never_generated():
    for reference in (True, False):
        assert generation_is_safe(
            BeatClass.FACT_STAT_SCORE, has_identity_reference=reference
        ) is False


def test_a_named_person_needs_an_identity_reference():
    assert generation_is_safe(
        BeatClass.NAMED_REAL_PERSON, has_identity_reference=False
    ) is False
    assert generation_is_safe(
        BeatClass.NAMED_REAL_PERSON, has_identity_reference=True
    ) is True


@pytest.mark.parametrize("beat", [
    BeatClass.GENERIC_FOOTBALL,
    BeatClass.STADIUM_LOCATION,
    BeatClass.OTHER_SAFE_VISUAL,
])
def test_an_identity_free_beat_may_always_be_generated(beat):
    assert generation_is_safe(beat, has_identity_reference=False) is True


# ── where the money goes ─────────────────────────────────────────────

def test_a_generic_beat_does_not_get_the_full_archival_ladder():
    """The stadium beats are what exhausted the quota the person beats needed."""
    generic = search_attempt_budget(BeatClass.GENERIC_FOOTBALL)
    person = search_attempt_budget(BeatClass.NAMED_REAL_PERSON)
    assert generic < person
    assert search_attempt_budget(BeatClass.STADIUM_LOCATION) < person


def test_a_fact_beat_spends_nothing_on_searching():
    """It is drawn locally from citations; there is nothing to find."""
    assert search_attempt_budget(BeatClass.FACT_STAT_SCORE) == 0


def test_only_identity_free_visuals_prefer_the_cheap_generator():
    assert prefers_cheap_generation(BeatClass.GENERIC_FOOTBALL) is True
    assert prefers_cheap_generation(BeatClass.STADIUM_LOCATION) is True
    assert prefers_cheap_generation(BeatClass.NAMED_REAL_PERSON) is False
    assert prefers_cheap_generation(BeatClass.EXACT_MATCH_EVENT) is False


def test_every_class_has_a_budget_and_a_generation_answer():
    for beat in BeatClass:
        assert isinstance(search_attempt_budget(beat), int)
        assert isinstance(generation_is_safe(beat, has_identity_reference=True), bool)


# ── the product rule: no visual dead end once the facts are grounded ──

def test_every_class_resolves_to_some_truthful_visual():
    """Each class must have a route that ends in a real visual, never nothing.

    Generated where the picture asserts nothing, referenced-and-reconstructed
    where it shows a person, and a verified information card where neither is
    allowed. No class falls off the end.
    """
    for beat in BeatClass:
        generated = generation_is_safe(beat, has_identity_reference=True)
        card = beat in {
            BeatClass.FACT_STAT_SCORE, BeatClass.EXACT_MATCH_EVENT,
        }
        assert generated or card, f"{beat} has no terminal visual"
