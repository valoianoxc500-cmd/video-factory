"""Horror visual sourcing: search specificity, starvation, and branding.

Grounded in the Flight 19 / Bermuda Triangle run, where 11 of 18 slots failed
to source a real photograph. Seven images then had to cover 74.4 seconds, so
the renderer re-showed them and a US Navy rescue boat ended up on the sentence
about a plane exploding in mid-air.
"""

from __future__ import annotations

import json
import math
import pathlib

import pytest

import prompts
from core.utils import (
    ScriptSection,
    compute_sub_durations,
    load_channel_config,
    minimum_visual_slots_for_duration,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HORROR = json.loads(
    (REPO_ROOT / "config" / "channels" / "horror_stories.json")
    .read_text(encoding="utf-8")
)
GUIDANCE = HORROR["video"]["visual_guidance"]
ALL_GUIDANCE = " ".join(str(v) for v in GUIDANCE.values())


# --- search specificity ----------------------------------------------------

def test_keywords_must_carry_a_proper_noun():
    """The rule that separates a subject from a period prop."""
    assert "proper noun" in GUIDANCE["google_photo"]


def test_the_exact_bad_keyword_from_the_flight_19_run_is_called_out():
    """'vintage 1940s aircraft radio equipment' produced the Air France frame."""
    assert "vintage 1940s aircraft radio equipment" in GUIDANCE["google_photo"]
    assert "BAD:" in GUIDANCE["google_photo"]


def test_specific_examples_name_event_unit_and_year():
    good = GUIDANCE["google_photo"]
    for token in ("Flight 19", "Martin PBM Mariner", "1945"):
        assert token in good, token


def test_bare_period_props_are_forbidden():
    forbidden = GUIDANCE["forbidden_visuals"]
    assert "period prop" in forbidden
    assert "1940s radio" in forbidden


def test_neighbouring_slots_must_name_different_subjects():
    """Repeated subjects collapse into one search result and one image."""
    assert "distinctness" in GUIDANCE
    assert "DIFFERENT subject" in GUIDANCE["distinctness"]


def test_guidance_still_forbids_generating_a_missing_historical_photo():
    """Specificity must not become an excuse to synthesise the image."""
    assert "Never" in GUIDANCE["ai_photo"]
    assert "fabricated document" in GUIDANCE["ai_photo"]


def test_a_missing_exact_photo_falls_back_to_real_archival_material():
    assert "thin_photographic_record" in GUIDANCE
    text = GUIDANCE["thin_photographic_record"]
    assert "Do not fill the gap with artwork" in text


# --- the settings the fix must not have weakened ---------------------------

def test_web_photos_only_is_still_on():
    assert load_channel_config("horror_stories").image_sourcing.web_photos_only is True


def test_the_five_second_visual_hold_is_unchanged():
    rd = load_channel_config("horror_stories").rendering_defaults
    assert rd.max_visual_hold_seconds == 5.0


def test_football_news_guidance_is_untouched_by_the_horror_fix():
    football = json.loads(
        (REPO_ROOT / "config" / "channels" / "football_news.json")
        .read_text(encoding="utf-8")
    )
    assert "distinctness" not in football["video"]["visual_guidance"]
    assert football["video"]["music_pool"] == ["news_bed_calm", "news_bed_drive"]


# --- starvation and repetition maths ---------------------------------------

def _section(seconds: float, words: int = 60) -> ScriptSection:
    return ScriptSection(
        id=1,
        narration=" ".join(["كلمة"] * words),
        estimated_duration_seconds=seconds,
        actual_duration_seconds=seconds,
    )


def test_a_74_second_video_needs_far_more_than_seven_images():
    """The Flight 19 shortfall, stated as a number."""
    needed = minimum_visual_slots_for_duration(74.4, 5.0, 0.3)
    assert needed >= 15
    assert needed > 7


@pytest.mark.parametrize("seconds,cap", [(36.0, 5.0), (38.4, 5.0), (74.4, 5.0)])
def test_required_slot_count_always_respects_the_hold_cap(seconds, cap):
    needed = minimum_visual_slots_for_duration(seconds, cap, 0.3)
    per_slot = (seconds + 0.3 * (needed - 1)) / needed
    assert per_slot <= cap + 1e-6


def test_repetition_is_what_starvation_costs():
    """Fewer distinct images means each is re-shown across unrelated beats."""
    beats = minimum_visual_slots_for_duration(38.37, 5.0, 0.3)
    distinct_sourced = 3                       # what the real run had
    reshows = math.ceil(beats / distinct_sourced)
    assert beats >= 9
    assert reshows >= 3, (
        "with three images over nine beats each is shown about three times, "
        "which is how a rescue boat lands on a plane-explosion sentence"
    )


def test_holding_one_image_for_a_whole_section_would_break_the_cap():
    section = _section(38.37)
    held = compute_sub_durations(section, 3, crossfade=0.3,
                                 max_visual_hold_seconds=5.0)
    assert max(held) > 5.0, (
        "three slots cannot cover 38s inside a 5s cap -- the renderer must "
        "re-show images rather than hold one"
    )


# --- image review: conflicting branding ------------------------------------

def _review_prompt() -> str:
    return prompts.image_review_prompt([
        {
            "section_id": 1,
            "sub_image_index": 1,
            "image_filename": "section_001_01.jpg",
            "narration": "US Navy Flight 19 disappeared off Florida in 1945.",
            "visual_type": "google_photo",
            "image_search_keywords": "Flight 19 TBM Avenger Fort Lauderdale 1945",
            "prompt": "Flight 19 TBM Avenger Fort Lauderdale 1945",
        }
    ])


def test_review_declares_the_conflicting_branding_failure_type():
    prompt = _review_prompt()
    assert "conflicting_branding" in prompt


def test_review_uses_the_air_france_case_as_its_example():
    prompt = _review_prompt()
    assert "AIR FRANCE" in prompt
    assert "US Navy" in prompt


def test_review_rejects_only_legible_contradicting_marks():
    prompt = _review_prompt()
    assert "LEGIBLE" in prompt
    for kind in ("logo", "livery", "flag", "insignia"):
        assert kind in prompt, kind


def test_review_does_not_reject_incidental_manufacturer_marks():
    """The gate must not become excessively strict."""
    prompt = _review_prompt()
    assert "manufacturer's plate" in prompt
    assert "serial number" in prompt
    assert "maker's name is normal" in prompt


def test_subject_relevance_checks_are_still_present():
    prompt = _review_prompt()
    for existing in ("wrong_subject", "pose_mismatch", "anatomy_error", "weak_match"):
        assert existing in prompt, existing


def test_conflicting_branding_is_an_error_not_a_warning():
    prompt = _review_prompt()
    branding = prompt[prompt.index("conflicting_branding"):]
    assert "contradict" in branding
