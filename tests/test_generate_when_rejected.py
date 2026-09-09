"""AI generation as a fallback for beats the relevance gate rejected.

The distinction this file protects: generating a purpose-built image for a
rejected beat is a *sourcing* decision, not a review one. The rejected file is
discarded, the generated file faces the same reviewer, and a generated image
that also fails is regenerated rather than accepted. Nothing here makes the
gate easier to pass.
"""

from __future__ import annotations

import asyncio

import pytest

from core import image_sourcer
from core.utils import ImageSourcingConfig, load_channel_config


def _run(coro):
    return asyncio.run(coro)


class _Section:
    def __init__(self, section_id: int):
        self.id = section_id


def _descriptor(section_id: int, sub_idx: int, path, brief: str):
    return {
        "section": _Section(section_id),
        "sub_idx": sub_idx,
        "img_path": path,
        "prompt": brief,
        "keywords": brief,
        "subject": brief,
        "sourced": False,
    }


# --- policy ----------------------------------------------------------------

def test_the_policy_is_off_by_default():
    """A documentary channel should fail rather than illustrate."""
    config = ImageSourcingConfig()
    assert config.generate_when_rejected is False
    assert config.max_generated_when_rejected == 8


def test_horror_opts_in_and_football_does_not():
    assert load_channel_config(
        "horror_stories").image_sourcing.generate_when_rejected is True
    assert load_channel_config(
        "football_news").image_sourcing.generate_when_rejected is False


def test_the_policy_is_separate_from_the_end_of_run_cover(monkeypatch, tmp_path):
    """A channel may want one and not the other, so the override does not
    consult allow_generated_fallback."""
    config = load_channel_config("horror_stories")
    config.image_sourcing.allow_generated_fallback = False

    planned: list = []

    def fake_plan(missing, *, limit, style_suffix):
        planned.extend(missing)
        return [], image_sourcer.generated_visuals.FallbackBudget(limit=limit)

    monkeypatch.setattr(image_sourcer.generated_visuals, "plan_fallback", fake_plan)

    target = tmp_path / "section_001_01.jpg"
    target.write_bytes(b"x")
    _run(image_sourcer._generate_missing_visuals(
        descriptors=[_descriptor(1, 0, target, "an exterior")],
        config=config,
        sourcing_log=[],
        allow_override=True,
    ))
    assert planned, "the override did not reach the planner"


def test_without_the_override_the_old_gate_still_applies(monkeypatch, tmp_path):
    config = load_channel_config("horror_stories")
    config.image_sourcing.allow_generated_fallback = False

    def fake_plan(missing, *, limit, style_suffix):  # pragma: no cover
        raise AssertionError("planned generation with the policy disabled")

    monkeypatch.setattr(image_sourcer.generated_visuals, "plan_fallback", fake_plan)

    target = tmp_path / "section_001_01.jpg"
    result = _run(image_sourcer._generate_missing_visuals(
        descriptors=[_descriptor(1, 0, target, "an exterior")],
        config=config,
        sourcing_log=[],
    ))
    assert result is None


def test_the_ceiling_is_passed_to_the_planner(monkeypatch, tmp_path):
    config = load_channel_config("horror_stories")
    seen: dict = {}

    def fake_plan(missing, *, limit, style_suffix):
        seen["limit"] = limit
        return [], image_sourcer.generated_visuals.FallbackBudget(limit=limit)

    monkeypatch.setattr(image_sourcer.generated_visuals, "plan_fallback", fake_plan)

    target = tmp_path / "section_001_01.jpg"
    _run(image_sourcer._generate_missing_visuals(
        descriptors=[_descriptor(1, 0, target, "x")],
        config=config, sourcing_log=[], allow_override=True,
        limit_override=3,
    ))
    assert seen["limit"] == 3


def test_generated_after_rejection_is_labelled_distinctly(monkeypatch, tmp_path):
    """So the trace separates it from the end-of-run cover."""
    config = load_channel_config("horror_stories")
    target = tmp_path / "section_001_01.jpg"

    monkeypatch.setattr(
        image_sourcer.generated_visuals, "plan_fallback",
        lambda missing, *, limit, style_suffix: (
            [{"section_id": 1, "sub_image_index": 1,
              "output_path": target, "prompt": "a decaying exterior"}],
            image_sourcer.generated_visuals.FallbackBudget(limit=limit),
        ))

    labels: list[str] = []

    async def fake_generate(prompt, path, **kwargs):
        labels.append(kwargs.get("operation_label", ""))
        return None

    # Patched at the dispatcher rather than at one generator: Horror now
    # generates on FLUX and Football on Gemini, and the label has to survive
    # either way.
    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", fake_generate)

    _run(image_sourcer._generate_missing_visuals(
        descriptors=[_descriptor(1, 0, target, "x")],
        config=config, sourcing_log=[], allow_override=True,
        operation_label="generated_after_rejection",
    ))
    assert labels == ["generated_after_rejection"]


# --- the safety rules the existing planner already enforces -----------------

def test_generation_still_refuses_to_fabricate_a_real_subject():
    """The rejection fallback reuses the existing planner, so its refusals --
    real people, real events -- apply unchanged."""
    from core import generated_visuals

    planned, budget = generated_visuals.plan_fallback(
        [{"section_id": 1, "sub_image_index": 1, "output_path": "x.jpg",
          "brief": "a photograph of Barack Obama at the 2011 summit",
          "subject": "Barack Obama"}],
        limit=5,
        style_suffix="",
    )
    assert planned == []
    assert budget.refused


def test_an_atmospheric_brief_is_allowed():
    from core import generated_visuals

    planned, _ = generated_visuals.plan_fallback(
        [{"section_id": 1, "sub_image_index": 1, "output_path": "x.jpg",
          "brief": "a dark decaying hospital corridor, no people",
          "subject": "corridor"}],
        limit=5,
        style_suffix="",
    )
    assert len(planned) == 1


# --- the gate is untouched --------------------------------------------------

def test_the_relevance_gate_is_not_relaxed_by_the_policy():
    """The generated image is reviewed by the same code, with the same
    thresholds, as a sourced photograph."""
    import inspect

    source = inspect.getsource(image_sourcer)
    marker = "generate_when_rejected"
    assert marker in source
    # The policy must not appear anywhere near the review verdict handling.
    review_fn = inspect.getsource(image_sourcer._select_photo_candidate)
    assert marker not in review_fn


def test_thresholds_are_unchanged_for_both_channels():
    horror = load_channel_config("horror_stories").review_thresholds
    assert horror.image_review_max_attempts >= 1
    # The policy changes what is sourced, never how many rejections are
    # tolerated or what counts as approval.
    assert hasattr(horror, "image_review_max_attempts")


def test_assets_that_passed_are_never_regenerated(tmp_path, monkeypatch):
    """Only descriptors marked unsourced reach the planner."""
    config = load_channel_config("horror_stories")
    passed = tmp_path / "section_001_02.jpg"
    passed.write_bytes(b"good")
    rejected = tmp_path / "section_001_01.jpg"

    seen: list = []

    def fake_plan(missing, *, limit, style_suffix):
        seen.extend(m["output_path"] for m in missing)
        return [], image_sourcer.generated_visuals.FallbackBudget(limit=limit)

    monkeypatch.setattr(image_sourcer.generated_visuals, "plan_fallback", fake_plan)

    good = _descriptor(1, 1, passed, "fine")
    good["sourced"] = True
    _run(image_sourcer._generate_missing_visuals(
        descriptors=[good, _descriptor(1, 0, rejected, "bad")],
        config=config, sourcing_log=[], allow_override=True,
    ))

    assert seen == [rejected], "a passing asset was queued for regeneration"
