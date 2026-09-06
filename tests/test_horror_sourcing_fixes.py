"""Three fixes from the Dyatlov Pass run that died two beats short.

The run failed with 14 photographs sourced and 5 beats dropped. Behind that
were three separate defects:

  * the generated-visual fallback read two descriptor keys that do not exist,
    so it planned work and then silently skipped all of it;
  * its logger sat outside the pipeline's logging tree, so that silence left
    no trace in pipeline.log;
  * slot briefs carried staging -- viewpoint, season, weather, time of day,
    instrument state -- that made correct photographs of the right subject
    fail the relevance gate;
  * two info_slides shipped with an empty keywords field, so image search was
    handed nothing at all.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import generated_visuals as gv  # noqa: E402
from core.scripter import _destage_slot_briefs  # noqa: E402
from core.utils import ScriptTimingProfile  # noqa: E402


# --- 1. the key names the descriptors actually use -------------------------

def test_the_fallback_reads_the_real_descriptor_keys():
    """Regression: it read "path" and "sub_index"; the keys are img_path/sub_idx.

    Reading the wrong ones produced output_path=None for every slot, which the
    generation loop skipped without generating or logging anything.
    """
    source = (REPO_ROOT / "core" / "image_sourcer.py").read_text(encoding="utf-8")
    block = source[source.index("async def _generate_missing_visuals"):]
    block = block[: block.index("\nasync def _rescue_underpopulated_sections")]

    assert 'd.get("img_path")' in block, "output_path is not read from img_path"
    assert 'd.get("sub_idx", 0)' in block, "sub_image_index is not read from sub_idx"
    assert 'd.get("path")' not in block
    assert 'd.get("sub_index"' not in block


def test_descriptors_really_do_use_those_keys():
    """Pin the contract, so renaming either key fails here rather than silently."""
    source = (REPO_ROOT / "core" / "image_sourcer.py").read_text(encoding="utf-8")
    assert '"img_path": img_path' in source
    assert '"sub_idx": sub_idx' in source


def test_a_planned_frame_reaches_the_generator(monkeypatch):
    """End to end through plan -> record, with the descriptor shape it will see."""
    budget = gv.FallbackBudget(limit=5)
    planned, budget = gv.plan_fallback(
        [{
            "section_id": 3,
            "sub_image_index": 2,
            "output_path": "/tmp/section_003_02.png",
            "brief": "an empty road disappearing into fog",
        }],
        limit=5,
    )
    assert len(planned) == 1
    assert planned[0]["output_path"], "a planned frame with no path cannot be written"

    visual = gv.record_generated(
        budget,
        section_id=planned[0]["section_id"],
        sub_image_index=planned[0]["sub_image_index"],
        path=planned[0]["output_path"],
        prompt=planned[0]["prompt"],
        model="m",
    )
    assert visual.to_provenance()["sub_image_index"] == 2


# --- 2. the logger reaches the run log -------------------------------------

def test_generated_visuals_logs_into_the_pipeline_log():
    """factory.py attaches the workspace handler to "video_factory" alone."""
    assert gv.logger.name == "video_factory", (
        "a private logger name keeps this module's refusals out of pipeline.log"
    )


def test_every_core_module_logs_under_the_same_name():
    import re

    mismatched = []
    for path in (REPO_ROOT / "core").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.findall(r'^logger = logging\.getLogger\("([^"]+)"\)', text, re.M):
            if match != "video_factory":
                mismatched.append(f"{path.name}: {match}")
    assert not mismatched, f"loggers outside the pipeline tree: {mismatched}"


# --- 3. staging is stripped, subject is not --------------------------------

def _destage(brief: str) -> str:
    data = {"sections": [{"slots": [{"keywords": brief, "prompt": brief}]}]}
    _destage_slot_briefs(data, web_photos_only=True)
    return data["sections"][0]["slots"][0]["keywords"]


@pytest.mark.parametrize(
    "brief,expected",
    [
        # The two the failure named.
        ("view from inside torn tent snow night", "torn tent snow"),
        ("memorial plaque in a snowy landscape", "memorial plaque"),
        # The rest of the family from the same run.
        ("Geiger counter with the needle indicating a high reading", "Geiger counter"),
        (
            "modern empty desolate landscape under a grey sky",
            "modern empty desolate landscape",
        ),
        ("abandoned cabin at night", "abandoned cabin"),
        ("the tent seen from outside in heavy fog", "the tent"),
        ("aerial shot of the mountain pass", "mountain pass"),
    ],
)
def test_staging_qualifiers_are_removed(brief, expected):
    assert _destage(brief) == expected


@pytest.mark.parametrize(
    "brief",
    [
        # Bare weather is the setting, not staging -- dropping it would return
        # a tent in a field.
        "torn tent in snow",
        "memorial plaque",
        "Santiago Bernabeu stadium exterior",
        "Dyatlov Pass search party photograph 1959 Ural Mountains",
        # Named archival artefacts keep their framing wording, as before.
        "FBI evidence photograph of the tie",
        "newspaper clipping about the disappearance",
    ],
)
def test_the_subject_survives_untouched(brief):
    assert _destage(brief) == brief


def test_a_brief_is_never_emptied():
    """An empty brief is worse than an over-specified one."""
    for brief in ("at night", "view from inside", "under a grey sky"):
        data = {"sections": [{"slots": [{"keywords": brief, "prompt": brief}]}]}
        _destage_slot_briefs(data, web_photos_only=True)
        assert data["sections"][0]["slots"][0]["keywords"].strip()


def test_destaging_is_off_for_channels_that_allow_illustration():
    data = {
        "sections": [{"slots": [{"keywords": "abandoned cabin at night"}]}]
    }
    _destage_slot_briefs(data, web_photos_only=False)
    assert data["sections"][0]["slots"][0]["keywords"] == "abandoned cabin at night"


def test_night_is_only_stripped_when_it_trails():
    """"night watchman" is a subject; "... at night" is lighting."""
    assert _destage("night watchman at the gate") == "night watchman at the gate"


# --- 4. component slots cannot ship without keywords -----------------------

def _validate(slot: dict) -> list[str]:
    from core.scripter import _script_validation_errors

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
            model_name="test",
            words_per_minute=150.0,
            section_overhead_seconds=1.0,
        ),
        web_photos_only=True,
    )
    return errors


@pytest.mark.parametrize("visual", ["info_slide", "info_card"])
def test_a_component_slot_with_no_keywords_is_rejected(visual):
    """Regression: two info_slides shipped with a prompt and no keywords."""
    errors = _validate({
        "visual": visual,
        "keywords": "",
        "prompt": "A map of the Ural Mountains with the pass marked",
        "props": {"text": "body"},
    })
    empty = [e for e in errors if "empty" in e and "keywords" in e]
    assert empty, f"{visual} with no keywords produced no error: {errors}"
    assert "cannot be sourced" in empty[0]


@pytest.mark.parametrize("visual", ["info_slide", "info_card"])
def test_whitespace_keywords_count_as_empty(visual):
    errors = _validate({
        "visual": visual,
        "keywords": "   ",
        "prompt": "p",
        "props": {"text": "body"},
    })
    assert [e for e in errors if "empty" in e and "keywords" in e]


def test_a_component_slot_with_keywords_passes():
    errors = _validate({
        "visual": "info_slide",
        "keywords": "Ural Mountains map",
        "prompt": "A map of the Ural Mountains",
        "props": {"text": "body"},
    })
    assert not [e for e in errors if "empty" in e and "keywords" in e]


def test_photo_slots_are_unaffected_by_the_new_check():
    """The check is scoped to the two component types."""
    errors = _validate({"visual": "google_photo", "keywords": "torn tent in snow"})
    assert not [e for e in errors if "empty" in e and "keywords" in e]
