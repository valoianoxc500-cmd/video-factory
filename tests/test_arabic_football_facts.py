"""Arabic topics must be fact-checked too, not just Latin-script ones.

A Football run picked this topic and generated a whole script from it:

    "آخر أخبار إيميليانو مارتينيز بعد انتقاله من أستون فيلا إلى تشيلسي"
    ("Latest on Emiliano Martínez after his transfer from Aston Villa to
      Chelsea")

Martínez has never played for Chelsea. The squad-record gate never fired,
because player names were found by looking for capitalised runs -- a
Latin-script idea that finds nothing in Arabic. The invented transfer reached
image sourcing, where the review gate finally said so, six minutes and a full
script too late.
"""

from __future__ import annotations

import asyncio

import pytest

from core import arabic_names, player_facts
from core.arabic_names import (
    clubs_in,
    contains_arabic,
    destination_clubs,
    person_name_candidates,
    transliterate,
)
from core.player_facts import PlayerStatus, premise_conflict
from core.researcher import _latin_destination_clubs

TOPIC = "آخر أخبار إيميليانو مارتينيز بعد انتقاله من أستون فيلا إلى تشيلسي"

MARTINEZ = PlayerStatus(
    name="E. Martínez",
    player_id=19599,
    current_club="Aston Villa",
    former_clubs=["Arsenal", "Reading"],
    source="api-football",
    last_transfer_date="2020-09-16",
)


# --- reading the Arabic ---------------------------------------------------


def test_the_topic_is_recognised_as_arabic():
    assert contains_arabic(TOPIC) is True
    assert contains_arabic("Barcelona interest in Julian Alvarez") is False


def test_the_player_is_found_in_the_arabic_topic():
    """Capitalised-run detection returned nothing here. This must not."""
    candidates = person_name_candidates(TOPIC)
    assert candidates, "no player detected in the Arabic topic"
    assert "مارتينيز" in candidates[0]["arabic"]


def test_the_surname_transliterates_to_something_the_provider_knows():
    """A naive reading gives "martynyz", which finds nobody."""
    readings = transliterate("مارتينيز")
    assert "martinez" in readings


def test_the_forename_transliterates_correctly():
    """"إيميليانو" reads as "eimiliano"; the player is "Emiliano"."""
    readings = transliterate("إيميليانو")
    assert "emiliano" in readings


def test_both_halves_of_the_name_are_carried_for_lookup():
    """Matching on one half found Lautaro Martínez and an Albanian Emiliano."""
    candidate = person_name_candidates(TOPIC)[0]
    assert any("martin" in t for t in candidate["search_terms"])
    assert any("miliano" in t for t in candidate["confirm_terms"])


# --- clubs ----------------------------------------------------------------


def test_both_clubs_are_read_from_the_arabic():
    assert set(clubs_in(TOPIC)) == {"Chelsea", "Aston Villa"}


def test_only_the_destination_club_is_treated_as_the_claim():
    """"from Aston Villa TO Chelsea" asserts Chelsea, not Aston Villa."""
    assert destination_clubs(TOPIC) == ["Chelsea"]


def test_a_club_mentioned_without_a_destination_marker_is_not_a_claim():
    assert destination_clubs("أخبار تشيلسي اليوم") == []


def test_latin_destinations_are_read_too():
    assert _latin_destination_clubs("Martinez moves to Chelsea") == ["Chelsea"]
    assert _latin_destination_clubs("Chelsea news today") == []


# --- the premise gate -----------------------------------------------------


def test_the_false_chelsea_premise_is_rejected():
    """The whole point: this topic must never reach the scripter."""
    reason = premise_conflict(destination_clubs(TOPIC), [MARTINEZ])

    assert reason, "the false premise was accepted"
    assert "Chelsea" in reason
    assert "Aston Villa" in reason


def test_a_true_premise_passes():
    assert premise_conflict(["Aston Villa"], [MARTINEZ]) == ""


def test_a_former_club_as_destination_is_also_rejected():
    reason = premise_conflict(["Arsenal"], [MARTINEZ])
    assert "already left" in reason


def test_an_unresolved_player_does_not_reject_anything():
    """Unverified is not the same as false."""
    assert premise_conflict(["Chelsea"], [PlayerStatus(name="Unknown")]) == ""


def test_no_asserted_club_means_nothing_to_check():
    assert premise_conflict([], [MARTINEZ]) == ""


# --- the pipeline refuses to script it ------------------------------------


def test_research_returns_a_premise_conflict_and_no_brief(monkeypatch):
    """No brief means no script: the run stops before anything is written."""
    from core import researcher
    from core.utils import load_channel_config

    async def fake_news(*a, **k):
        return []

    async def fake_resolve(display, terms, **k):
        return MARTINEZ

    monkeypatch.setattr(researcher.news_sources, "fetch_current_news", fake_news)
    monkeypatch.setattr(
        researcher.player_facts, "resolve_player_by_terms", fake_resolve
    )

    async def never_called(*a, **k):
        raise AssertionError("the model must not be asked about a false premise")

    monkeypatch.setattr(researcher.clients, "research_with_search", never_called)

    result = asyncio.run(
        researcher.research_topic(
            topic=TOPIC, angle="", config=load_channel_config("football_news")
        )
    )

    assert result["premise_conflict"]
    assert result["brief"] == "", "an empty brief is what stops the scripter"
    assert result["grounded"] is False


def test_the_factory_fails_the_run_on_a_premise_conflict():
    """Wired, not merely computed."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parent.parent / "factory.py"
    ).read_text(encoding="utf-8")
    assert 'research.get("premise_conflict")' in source
    assert "No script or visuals were generated" in source
    conflict_at = source.index('research.get("premise_conflict")')
    script_at = source.index("generate_script(")
    assert conflict_at < script_at, "the check must run before the script is written"


# --- Horror is untouched --------------------------------------------------


def test_arabic_horror_is_not_given_football_treatment():
    from core.researcher import _is_news_channel
    from core.utils import load_channel_config

    assert _is_news_channel(load_channel_config("horror_stories")) is False


def test_a_horror_topic_yields_no_football_clubs():
    assert clubs_in("قصة رعب عن منزل مهجور") == []
