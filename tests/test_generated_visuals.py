"""The rules that make last-resort image generation safe.

Most of this file is about refusal. Generating an atmospheric frame for a beat
with no photograph is a small convenience; generating something that looks like
archival evidence of a real event is a forgery that the pipeline would then
present with a documentary voiceover. The asymmetry is the reason the checks
are strict and the reason these tests are mostly negative.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import generated_visuals as gv  # noqa: E402


# --- things that must never be generated -----------------------------------

ARCHIVAL_REQUESTS = [
    "archival photograph of the abandoned cabin",
    "FBI evidence photograph of the ransom note",
    "newsreel still of the search party",
    "crime scene photo of the clearing",
    "surveillance camera footage of the corridor",
    "CCTV still from the stairwell",
    "the actual photograph taken that night",
    "real photo of the wreckage",
    "police composite sketch of the man",
    "newspaper clipping about the disappearance",
    "declassified document showing the flight path",
    "mugshot of the man arrested",
    "historical photograph of the mountain pass",
    "press photo of the rescue team",
    "wanted poster from the period",
]


@pytest.mark.parametrize("prompt", ARCHIVAL_REQUESTS)
def test_archival_and_evidentiary_requests_are_refused(prompt):
    reason = gv.refusal_reason(prompt)
    assert reason, f"would have generated fake evidence for: {prompt!r}"
    assert not gv.is_safe_to_generate(prompt)


PERSON_REQUESTS = [
    "portrait of the missing woman",
    "the face of the killer",
    "a likeness of the suspect",
    "photo of Dwight Eisenhower",
    "the victim smiling at the camera",
]


@pytest.mark.parametrize("prompt", PERSON_REQUESTS)
def test_identifiable_people_are_refused(prompt):
    assert gv.refusal_reason(prompt), f"would have generated a person: {prompt!r}"


NAMED_SUBJECTS = [
    "the disappearance of Amelia Earhart",
    "a shot of Dyatlov Pass in winter",
    "the cabin at Mount Rainier",
]


@pytest.mark.parametrize("prompt", NAMED_SUBJECTS)
def test_named_people_and_places_are_refused(prompt):
    """A generated frame of a named real place still reads as a record of it."""
    assert gv.refusal_reason(prompt), f"named subject was allowed: {prompt!r}"


def test_the_subject_is_checked_as_well_as_the_prompt():
    """A clean prompt attached to a real case is still a fabrication."""
    reason = gv.refusal_reason(
        "a dark forest at night, mist between the trees",
        subject="the D.B. Cooper ransom note",
    )
    assert reason, "a safe-looking prompt smuggled a real subject through"


def test_an_empty_prompt_is_refused():
    assert gv.refusal_reason("")
    assert gv.refusal_reason("   ")


# --- what may be generated -------------------------------------------------

SAFE_REQUESTS = [
    "a dark forest at night, mist between the trees",
    "an empty road disappearing into fog",
    "a cold mountain ridge under heavy cloud",
    "an abandoned room with peeling wallpaper",
    "moonlight through a broken window",
]


@pytest.mark.parametrize("prompt", SAFE_REQUESTS)
def test_generic_atmospheric_imagery_is_allowed(prompt):
    assert gv.is_safe_to_generate(prompt), f"refused a safe prompt: {prompt!r}"


def test_the_prompt_declares_the_image_is_not_a_record():
    built = gv.build_prompt("a dark forest at night")
    assert "not a photograph of any real event" in built
    assert "no recognisable faces" in built
    assert "no readable text" in built


def test_the_style_suffix_is_included_when_given():
    built = gv.build_prompt("a dark forest", style_suffix="desaturated, 1970s film stock")
    assert "desaturated, 1970s film stock" in built


# --- the per-video ceiling -------------------------------------------------

def _slot(i: int, brief: str = "an empty road in fog") -> dict:
    return {"section_id": i, "sub_image_index": 1, "brief": brief}


def test_generation_stops_at_the_limit():
    planned, budget = gv.plan_fallback([_slot(i) for i in range(12)], limit=5)
    assert len(planned) == 5
    assert budget.limit == 5
    assert any("limit" in r for r in budget.refused)


def test_a_zero_limit_generates_nothing():
    planned, budget = gv.plan_fallback([_slot(1)], limit=0)
    assert planned == []
    assert budget.exhausted


def test_unsafe_slots_do_not_consume_the_budget():
    """A refusal must not use up a slot a safe beat could have had."""
    slots = [
        _slot(1, "FBI evidence photograph of the note"),
        _slot(2, "an empty road in fog"),
        _slot(3, "a cold ridge under cloud"),
    ]
    planned, budget = gv.plan_fallback(slots, limit=2)
    assert len(planned) == 2
    assert all("evidence" not in p["prompt"].lower() for p in planned)
    assert len(budget.refused) == 1


def test_refusals_say_why():
    _, budget = gv.plan_fallback([_slot(1, "archival photo of the site")], limit=5)
    assert budget.refused
    assert "forgery" in budget.refused[0]


# --- provenance ------------------------------------------------------------

def test_a_generated_frame_is_marked_as_generated():
    budget = gv.FallbackBudget(limit=5)
    visual = gv.record_generated(
        budget,
        section_id=2,
        sub_image_index=1,
        path="/tmp/sec02_img01.png",
        prompt="an empty road in fog",
        model="gemini-3.1-flash-lite-image",
    )
    record = visual.to_provenance()
    assert record["generated"] is True
    assert record["source"] == "ai_generated_fallback"
    assert "not a photograph" in record["provenance"]
    assert record["model"] == "gemini-3.1-flash-lite-image"


def test_provenance_carries_the_file_name_only():
    budget = gv.FallbackBudget(limit=5)
    visual = gv.record_generated(
        budget, section_id=1, sub_image_index=1,
        path="/home/someone/workspace/run/images/sec01_img01.png",
        prompt="fog", model="m",
    )
    assert visual.to_provenance()["file"] == "sec01_img01.png"


# --- cost tracking ---------------------------------------------------------

def test_cost_is_recorded_per_image():
    budget = gv.FallbackBudget(limit=5)
    for i in range(3):
        gv.record_generated(
            budget, section_id=i, sub_image_index=1,
            path=f"/tmp/{i}.png", prompt="fog", model="m",
        )
    assert budget.used == 3
    assert budget.total_cost_usd == pytest.approx(3 * gv.COST_PER_IMAGE_USD, rel=1e-6)


def test_the_summary_reports_spend_and_refusals():
    planned, budget = gv.plan_fallback(
        [_slot(1, "archival photo"), _slot(2), _slot(3)], limit=5
    )
    for slot in planned:
        gv.record_generated(
            budget, section_id=slot["section_id"], sub_image_index=1,
            path="/tmp/x.png", prompt=slot["prompt"], model="m",
        )
    summary = budget.summary()
    assert summary["generated_images"] == 2
    assert summary["limit"] == 5
    assert summary["refused"] == 1
    assert summary["total_cost_usd"] > 0
    assert summary["refusal_reasons"]


def test_a_run_that_generated_nothing_costs_nothing():
    budget = gv.FallbackBudget(limit=5)
    assert budget.total_cost_usd == 0
    assert budget.summary()["generated_images"] == 0


# --- configuration ---------------------------------------------------------

def test_the_fallback_is_off_by_default():
    """Existing channels must not start generating images because of this."""
    from core.utils import ImageSourcingConfig

    assert ImageSourcingConfig().allow_generated_fallback is False
    assert ImageSourcingConfig().max_generated_fallback_images == 5
    assert (
        ImageSourcingConfig().generated_fallback_model
        == "gemini-3.1-flash-lite-image"
    )


def test_football_news_has_a_bounded_generic_fallback_after_real_sources():
    import json

    config = json.loads(
        (REPO_ROOT / "config" / "channels" / "football_news.json").read_text(
            encoding="utf-8"
        )
    )
    sourcing = config.get("image_sourcing", {})
    assert sourcing.get("allow_generated_fallback", False) is True
    assert sourcing.get("generated_fallback_model") == "fal-ai/flux/schnell"
    assert sourcing.get("max_generated_fallback_images") == 4
    assert sourcing.get("allow_generated_player_reconstruction") is True
    # Bounded, not pinned. Reconstruction is now the primary route for a
    # named-person beat that no real photograph covers -- it draws the player
    # from a verified face reference rather than letting the beat die -- so a
    # ceiling of two made the ladder decorative on a script that talks about
    # five people across twenty beats. It must still be a ceiling.
    reconstructions = sourcing.get("max_generated_player_reconstructions")
    assert isinstance(reconstructions, int)
    assert 0 < reconstructions <= 12
    assert sourcing.get("web_photos_only") is True
