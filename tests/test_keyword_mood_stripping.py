"""Search keywords are reduced to the concrete things in them.

Image search cannot act on "eerie" or "cinematic" -- those terms pull in
horror-poster artwork and film stills, which the relevance gate then rejects
for not being real photographs. The subject is what should be searched for.

Stripping happens in the repair pass, before validation, so a mood-laden brief
is fixed in place rather than failing and sending the whole script back for
another revision round. Validation itself is unchanged: a brief with nothing
concrete in it keeps its original text and still fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.scripter import (  # noqa: E402
    _repair_slot_fields,
    _script_validation_errors,
    strip_mood_words,
)
from core.utils import ScriptTimingProfile  # noqa: E402


def _repair_keywords(keywords: str, visual: str = "google_photo") -> str:
    data = {"sections": [{"id": 1, "slots": [
        {"visual": visual, "prompt": "a photo", "keywords": keywords},
    ]}]}
    _repair_slot_fields(data)
    return data["sections"][0]["slots"][0]["keywords"]


# --- the worked example ----------------------------------------------------

def test_the_example_from_the_brief():
    result = strip_mood_words(
        "strange lights snowy mountain pass night eerie cinematic"
    )
    assert set(result.split()) == {"lights", "snow", "mountain", "pass", "night"}
    # "snowy" becomes the thing itself, so it still narrows the search.
    assert "snow" in result.split()
    assert "snowy" not in result


# --- mood and style go ------------------------------------------------------

@pytest.mark.parametrize(
    "word",
    ["eerie", "cinematic", "haunting", "mysterious", "ominous", "creepy",
     "spooky", "dramatic", "atmospheric", "moody", "stylized", "surreal",
     "ethereal", "terrifying", "breathtaking", "majestic", "strange"],
)
def test_mood_words_are_removed(word):
    result = strip_mood_words(f"{word} mountain pass")
    assert word not in result.lower().split()
    assert "mountain" in result and "pass" in result


@pytest.mark.parametrize(
    "adjective,noun",
    [("snowy", "snow"), ("foggy", "fog"), ("misty", "mist"),
     ("rainy", "rain"), ("stormy", "storm"), ("wintry", "winter")],
)
def test_weather_adjectives_become_the_thing(adjective, noun):
    result = strip_mood_words(f"{adjective} mountain pass")
    assert noun in result.split()
    assert adjective not in result.split()


# --- concrete subjects survive ---------------------------------------------

@pytest.mark.parametrize(
    "brief",
    [
        "Dyatlov Pass search party photograph 1959",
        "Santiago Bernabeu stadium exterior",
        "FBI evidence photograph of the tie",
        "torn tent in snow",
        "memorial plaque",
        "Soviet investigation file document",
        "Enzo Fernandez Chelsea signing",
        # Words that read as mood but name real places or states.
        "dark room abandoned factory desolate landscape",
        "empty stadium at night",
    ],
)
def test_concrete_briefs_are_untouched(brief):
    assert strip_mood_words(brief) == brief


def test_people_places_objects_documents_and_events_all_survive():
    brief = (
        "Dyatlov hikers Ural Mountains tent Soviet investigation report 1959 "
        "search party memorial"
    )
    assert strip_mood_words(brief) == brief


# --- validation is not weakened --------------------------------------------

def test_a_brief_of_only_mood_words_is_never_blanked():
    """Emptying it would hide the problem instead of reporting it."""
    assert _repair_keywords("eerie cinematic haunting") == "eerie cinematic haunting"


def test_a_brief_of_only_mood_words_still_fails_validation():
    data = {
        "title": "t",
        "sections": [{
            "id": 1,
            "narration": "n",
            "estimated_duration_seconds": 10,
            "slots": [{
                "visual": "google_photo",
                "prompt": "a cinematic shot",
                "keywords": "eerie cinematic haunting",
            }],
        }],
    }
    errors, _ = _script_validation_errors(
        data,
        numbering_order=None,
        max_visual_hold_seconds=5.0,
        crossfade=0.3,
        timing_profile=ScriptTimingProfile(
            model_name="test", words_per_minute=150.0,
            section_overhead_seconds=1.0,
        ),
        web_photos_only=True,
    )
    assert errors, "a brief with nothing concrete in it should still be rejected"


def test_stripping_does_not_rescue_a_non_photographable_subject():
    """Removing "cinematic" must not turn a graphic device into a subject."""
    from core.scripter import non_photographable_reason

    assert non_photographable_reason(strip_mood_words("cinematic question mark"))


# --- the repair happens in place -------------------------------------------

@pytest.mark.parametrize(
    "visual",
    ["google_photo", "stock_photo", "b_roll", "info_card", "info_slide",
     "title_card", "subscribe_cta"],
)
def test_every_searched_type_gets_stripped(visual):
    result = _repair_keywords("eerie snowy mountain pass", visual=visual)
    assert "eerie" not in result
    assert "mountain" in result and "pass" in result


def test_generated_lanes_keep_their_wording():
    """ai_photo is drawn from the prompt; its keywords are not a search."""
    result = _repair_keywords("eerie cinematic forest", visual="ai_photo")
    assert result == "eerie cinematic forest"


def test_an_already_clean_brief_is_not_rewritten():
    assert _repair_keywords("torn tent in snow") == "torn tent in snow"


def test_stripping_runs_before_validation():
    """Otherwise a mood word costs a whole script revision round-trip."""
    source = (REPO_ROOT / "core" / "scripter.py").read_text(encoding="utf-8")
    block = source[source.index("def _validate_generated_script"):]
    block = block[: block.index("def _script_validation_feedback")]
    assert "_repair_slot_fields(content)" in block
    assert block.index("_repair_slot_fields(content)") < block.index(
        "_script_validation_errors("
    )


def test_the_repair_edits_the_script_in_place():
    """No new object: the caller's already-generated script is preserved."""
    slot = {"visual": "google_photo", "prompt": "p", "keywords": "eerie ridge"}
    data = {"sections": [{"id": 1, "slots": [slot]}]}
    assert _repair_slot_fields(data) is None
    assert data["sections"][0]["slots"][0] is slot, "the slot object was replaced"
    assert slot["keywords"] == "ridge"


def test_unrelated_slot_fields_are_preserved():
    slot = {
        "visual": "info_slide",
        "prompt": "A map of the pass",
        "keywords": "eerie map",
        "props": {"text": "body", "title": "Heading"},
        "visual_policy": "photo_backed_info_slide",
    }
    _repair_slot_fields({"sections": [{"id": 1, "slots": [slot]}]})
    assert slot["props"] == {"text": "body", "title": "Heading"}
    assert slot["visual_policy"] == "photo_backed_info_slide"
    assert slot["prompt"] == "A map of the pass"
    assert slot["keywords"] == "map"
