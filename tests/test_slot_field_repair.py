"""Every sourced slot reaches image search with both fields it needs.

A Dyatlov run burned two full revision attempts and died in the script stage
because the model wrote the picture's description into the `visual` field:

    "visual": "Group photo of the Dyatlov hikers, smiling and posing…"

Perfectly usable content in the wrong field. A separate run shipped info_slides
with a prompt and an empty `keywords`, so image search was handed nothing.

The repair pass puts both shapes right before validation runs. Validation
itself is not relaxed -- these tests check both halves: that repairable output
is repaired, and that genuinely unusable output still fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.scripter import (  # noqa: E402
    _derive_keywords,
    _repair_slot_fields,
    _script_validation_errors,
)
from core.utils import ScriptTimingProfile, VisualSlot  # noqa: E402

SOURCED = ["google_photo", "stock_photo", "b_roll", "info_card", "info_slide",
           "title_card", "title_banner", "fact_highlight", "subscribe_cta"]


def _repair(slots: list[dict]) -> list[dict]:
    data = {"sections": [{"id": 1, "slots": slots}]}
    _repair_slot_fields(data)
    return data["sections"][0]["slots"]


# --- a description written into the type field -----------------------------

def test_a_description_in_the_visual_field_is_moved_to_prompt():
    """The exact failure: every slot typed with its own description."""
    description = (
        "Group photo of the Dyatlov hikers, smiling and posing together in "
        "winter gear during their expedition. Black and white photo."
    )
    slot = _repair([{"visual": description}])[0]

    assert slot["visual"] == "google_photo"
    assert slot["prompt"] == description
    assert slot["keywords"].strip()


def test_the_repaired_slot_passes_validation():
    """Repair is only worth anything if the result actually validates."""
    slots = _repair([
        {"visual": "snow blowing across a dark mountain ridge at night"},
    ])
    assert not _validate(slots[0])


def test_an_existing_prompt_is_not_overwritten():
    slot = _repair([{
        "visual": "a description that landed in the wrong field",
        "prompt": "the prompt the model actually wrote",
    }])[0]
    assert slot["prompt"] == "the prompt the model actually wrote"
    assert slot["visual"] == "google_photo"


def test_a_real_visual_type_is_left_alone():
    for visual in SOURCED:
        slot = _repair([{
            "visual": visual, "prompt": "p", "keywords": "k",
        }])[0]
        assert slot["visual"] == visual, f"{visual} was rewritten"


def test_a_single_word_unknown_visual_is_not_treated_as_a_description():
    """"photo" is a typo to be reported, not prose to be relocated."""
    slot = _repair([{"visual": "photo"}])[0]
    assert slot["visual"] == "photo", "a bare unknown type should still fail validation"


# --- keywords derived from the prompt --------------------------------------

@pytest.mark.parametrize("visual", SOURCED)
def test_missing_keywords_are_derived_for_every_sourced_type(visual):
    slot = _repair([{
        "visual": visual,
        "prompt": "A map of the Ural Mountains with the pass marked",
        "props": {"text": "b"},
    }])[0]
    assert slot["keywords"].strip(), f"{visual} got no keywords"
    assert "Ural" in slot["keywords"]


def test_existing_keywords_are_never_replaced():
    slot = _repair([{
        "visual": "google_photo",
        "prompt": "The abandoned tent covered in snow",
        "keywords": "Dyatlov tent 1959",
    }])[0]
    assert slot["keywords"] == "Dyatlov tent 1959"


def test_derived_keywords_drop_filler_and_keep_the_subject():
    derived = _derive_keywords(
        "A wide shot of the memorial plaque showing the names of the hikers"
    )
    assert "memorial" in derived and "plaque" in derived
    for filler in (" the ", " of ", " a "):
        assert filler not in f" {derived} "


def test_derived_keywords_are_bounded():
    derived = _derive_keywords(" ".join(f"word{i}" for i in range(40)))
    assert 0 < len(derived.split()) <= 8


def test_a_prompt_of_only_filler_derives_nothing_rather_than_junk():
    slot = _repair([{"visual": "google_photo", "prompt": "the of a in on"}])[0]
    # Nothing usable to derive: leave it empty so validation reports it.
    assert not slot.get("keywords", "").strip()


# --- validation is not weakened --------------------------------------------

def _validate(slot: dict) -> list[str]:
    data = {
        "title": "t",
        "sections": [{
            "id": 1,
            "narration": "n",
            "estimated_duration_seconds": 10,
            "slots": [slot],
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
    return errors


@pytest.mark.parametrize("visual", SOURCED)
def test_an_unrepairable_slot_still_fails(visual):
    """No prompt and no keywords is not repairable, and must not pass."""
    errors = _validate({"visual": visual, "props": {"text": "b"}})
    assert [e for e in errors if "keywords" in e], (
        f"{visual} with neither field passed validation"
    )


@pytest.mark.parametrize("visual", ["google_photo", "subscribe_cta", "info_card"])
def test_a_missing_prompt_is_reported(visual):
    errors = _validate({"visual": visual, "keywords": "k", "props": {"text": "b"}})
    assert [e for e in errors if "prompt" in e], f"{visual} prompt not required"


def test_subscribe_cta_is_described_as_a_background():
    errors = _validate({"visual": "subscribe_cta"})
    assert [e for e in errors if "background image" in e], (
        "subscribe_cta should ask for a background, not a generic image"
    )


def test_info_slide_gets_one_prompt_instruction_not_two():
    """It has its own prompt rule; a second would be contradictory noise."""
    errors = _validate({"visual": "info_slide", "keywords": "k", "props": {"text": "b"}})
    prompt_errors = [e for e in errors if "prompt" in e]
    assert len(prompt_errors) == 1, f"expected one, got {prompt_errors}"


def test_a_complete_slot_passes():
    assert not _validate({
        "visual": "google_photo",
        "prompt": "The abandoned tent",
        "keywords": "Dyatlov tent 1959",
    })


def test_component_only_slots_are_not_asked_for_images():
    """A chart is drawn, not sourced -- it needs no prompt or keywords."""
    for visual in ("bar_chart", "line_chart", "donut_gauge", "comparison_bars"):
        errors = _validate({"visual": visual, "props": {"text": "b"}})
        assert not [e for e in errors if "keywords" in e], visual


def test_the_searched_set_matches_the_model():
    """If a new sourced type appears, this test says so."""
    from core.scripter import _SEARCHED_VISUAL_TYPES

    assert set(SOURCED) == set(_SEARCHED_VISUAL_TYPES)


def test_generated_lanes_are_not_asked_for_search_keywords():
    """ai_photo and ai_illustration are drawn from the prompt, not searched.

    Demanding keywords for them would newly fail every script on a channel
    that uses an AI lane.
    """
    from core.scripter import _SEARCHED_VISUAL_TYPES

    assert "ai_photo" not in _SEARCHED_VISUAL_TYPES
    assert "ai_illustration" not in _SEARCHED_VISUAL_TYPES

    for visual in ("ai_photo", "ai_illustration"):
        errors = _validate({"visual": visual, "prompt": "a dark forest"})
        assert not [e for e in errors if "keywords" in e], visual


# --- the revision loop sees the repaired script ----------------------------

def test_repair_runs_before_validation_in_the_generation_loop():
    source = (REPO_ROOT / "core" / "scripter.py").read_text(encoding="utf-8")
    block = source[source.index("def _validate_generated_script"):]
    block = block[: block.index("def _script_validation_feedback")]
    # Matched on the call, not its argument list, which has grown a keyword.
    repair_at = block.index("_repair_slot_fields(")
    validate_at = block.index("_script_validation_errors(")
    assert repair_at < validate_at, (
        "validation runs before the repair pass, so repairable output still fails"
    )


def test_the_generator_is_told_the_field_contract():
    """The repair is a safety net; the prompt should stop it being needed."""
    source = (REPO_ROOT / "prompts.py").read_text(encoding="utf-8")
    assert "is a TYPE NAME from the allowed list, never a description" in source
    assert "cannot be sourced at all" in source
