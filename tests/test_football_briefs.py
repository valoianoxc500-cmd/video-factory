"""Football briefs must ask for something a camera has photographed.

A Football run died with section 3 holding two usable photographs where it
needed four. Sourcing was fine; the briefs were not. Five of that section's
six beats asked for things no archive contains -- a finger clicking a YouTube
bell icon, a smartphone showing a sports app -- and on a web_photos_only
channel each is searched for as a real photograph. Search returned
screenshots, line art and an "Access Restricted" error page, and the relevance
gate rejected every one, correctly.

The gate is untouched and still strict. These cover the briefs being rewritten
so it has something it can accept.
"""

from __future__ import annotations

import pytest

from core.scripter import (
    _is_football_channel,
    _named_clubs,
    _repair_football_briefs,
    brief_requests_interface,
    disambiguate_football,
)
from core.utils import load_channel_config

# Verbatim from the failing run.
UI_BRIEFS = [
    ("notification bell icon click smartphone",
     "A finger clicking on a YouTube notification bell icon on a smartphone screen."),
    ("smartphone sports news football app",
     "A a smartphone displaying a sports news app with football headlines."),
]

# Real photographic subjects from the same run that sourced without trouble.
GOOD_BRIEFS = [
    ("Julián Álvarez Atlético Madrid kit photograph",
     "Julián Álvarez wearing the Atlético Madrid home kit during a match."),
    ("Joan Laporta Barcelona president press conference photograph",
     "FC Barcelona president Joan Laporta speaking at a press conference."),
    ("worried Barcelona fans stadium photograph",
     "A group of FC Barcelona fans looking worried while watching a match."),
]


def _script(*slots: tuple[str, str], visual: str = "google_photo") -> dict:
    return {
        "title": "Barcelona interest in Julian Alvarez",
        "sections": [
            {
                "id": 3,
                "narration": "Alvarez's future is unclear. Subscribe for more.",
                "slots": [
                    {"visual": visual, "keywords": kw, "prompt": p}
                    for kw, p in slots
                ],
            }
        ],
    }


# --- the gate: football only ----------------------------------------------


def test_football_channel_is_detected():
    assert _is_football_channel(load_channel_config("football_news")) is True


def test_horror_is_not_a_football_channel():
    assert _is_football_channel(load_channel_config("horror_stories")) is False


# --- interface briefs are recognised --------------------------------------


@pytest.mark.parametrize("keywords,prompt", UI_BRIEFS)
def test_the_failing_briefs_are_recognised_as_interfaces(keywords, prompt):
    assert brief_requests_interface(prompt) is True


@pytest.mark.parametrize(
    "text",
    [
        "A finger clicking on a YouTube notification bell icon",
        "smartphone displaying a sports news app",
        "screenshot of the league table",
        "a 3d render of the trophy",
        "an illustration of the stadium",
        "subscribe button animation",
        "phone screen showing the highlights",
        "infographic of the transfer fee",
    ],
)
def test_interface_and_artwork_wording_is_caught(text):
    assert brief_requests_interface(text) is True


@pytest.mark.parametrize("keywords,prompt", GOOD_BRIEFS)
def test_a_real_photographic_brief_is_left_alone(keywords, prompt):
    assert brief_requests_interface(keywords) is False
    assert brief_requests_interface(prompt) is False


def test_a_stadium_crowd_is_not_an_interface():
    assert brief_requests_interface(
        "A packed football stadium with flags waving"
    ) is False


# --- the rewrite ----------------------------------------------------------


def test_an_interface_brief_becomes_a_real_football_photograph():
    script = _script(*UI_BRIEFS)

    _repair_football_briefs(script)

    for slot in script["sections"][0]["slots"]:
        assert brief_requests_interface(slot["keywords"]) is False
        assert brief_requests_interface(slot["prompt"]) is False
        assert "bell" not in slot["prompt"].lower()
        assert "smartphone" not in slot["prompt"].lower()


def test_the_replacement_names_a_club_the_script_mentions():
    """The gate wants a specific subject; a named club is one."""
    script = _script(*UI_BRIEFS)

    _repair_football_briefs(script)

    for slot in script["sections"][0]["slots"]:
        assert "Barcelona" in slot["keywords"] or "Barcelona" in slot["prompt"]


def test_a_cta_beat_does_not_have_to_be_the_named_player():
    """Six distinct action photos of one player do not exist to be found."""
    script = _script(*UI_BRIEFS)

    _repair_football_briefs(script)

    joined = " ".join(
        s["keywords"] + s["prompt"] for s in script["sections"][0]["slots"]
    )
    assert "Alvarez" not in joined and "Álvarez" not in joined


def test_replacements_differ_so_beats_are_not_identical():
    script = _script(*UI_BRIEFS)

    _repair_football_briefs(script)

    keywords = [s["keywords"] for s in script["sections"][0]["slots"]]
    assert len(set(keywords)) == len(keywords)


def test_good_briefs_survive_the_repair_unchanged():
    script = _script(*GOOD_BRIEFS)
    before = [(s["keywords"], s["prompt"]) for s in script["sections"][0]["slots"]]

    _repair_football_briefs(script)

    after = [(s["keywords"], s["prompt"]) for s in script["sections"][0]["slots"]]
    assert after == before


def test_slots_the_renderer_draws_are_not_rewritten():
    """A chart is drawn, not searched for, so its wording is not our business."""
    script = _script(*UI_BRIEFS, visual="bar_chart")
    before = [s["keywords"] for s in script["sections"][0]["slots"]]

    _repair_football_briefs(script)

    assert [s["keywords"] for s in script["sections"][0]["slots"]] == before


def test_a_subscribe_cta_beat_is_rewritten():
    """The outro CTA is sourced as a real photograph, so it is in scope.

    Slot 3.6 in the failing run was a subscribe_cta asking for a stadium and
    was rejected along with the rest.
    """
    script = _script(
        ("notification bell icon click smartphone", "A finger clicking a bell icon."),
        visual="subscribe_cta",
    )

    _repair_football_briefs(script)

    slot = script["sections"][0]["slots"][0]
    assert brief_requests_interface(slot["prompt"]) is False
    assert "Barcelona" in slot["keywords"]


# --- which football -------------------------------------------------------


def test_a_bare_football_query_is_told_which_sport():
    """"All candidates show American football, which is explicitly prohibited"."""
    assert "soccer" in disambiguate_football("football on green grass pitch")


def test_a_query_that_already_says_soccer_is_left_alone():
    text = "soccer football pitch photograph"
    assert disambiguate_football(text) == text


def test_a_competition_name_is_enough_to_fix_the_sport():
    text = "Premier League football stadium"
    assert disambiguate_football(text) == text


def test_a_deliberate_american_football_query_is_not_rewritten():
    text = "American football stadium photograph"
    assert disambiguate_football(text) == text


def test_a_query_without_football_is_untouched():
    text = "Joan Laporta press conference photograph"
    assert disambiguate_football(text) == text


def test_disambiguation_is_applied_by_the_repair():
    script = _script(("football on green grass pitch photograph", "A football on grass."))

    _repair_football_briefs(script)

    assert "soccer" in script["sections"][0]["slots"][0]["keywords"]


# --- club detection -------------------------------------------------------


def test_clubs_are_taken_from_the_script_itself():
    script = _script(*GOOD_BRIEFS)
    clubs = _named_clubs(script)
    assert "Barcelona" in clubs


def test_a_script_naming_no_club_still_repairs():
    """Nothing is invented; the beat just gets an unnamed club."""
    script = {
        "title": "A story",
        "sections": [
            {
                "id": 1,
                "narration": "n",
                "slots": [
                    {
                        "visual": "google_photo",
                        "keywords": "notification bell icon",
                        "prompt": "A finger clicking a bell icon on a screen.",
                    }
                ],
            }
        ],
    }

    _repair_football_briefs(script)

    slot = script["sections"][0]["slots"][0]
    assert brief_requests_interface(slot["prompt"]) is False
