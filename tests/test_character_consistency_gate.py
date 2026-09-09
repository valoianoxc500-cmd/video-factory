"""The Character Bible gate has to be wired to something.

`animation_stage.review_character_consistency` was fully written and never
called. Its own docstring describes a caller -- "the caller re-sources those
beats and asks again" -- that did not exist, so the review was dead code and
an Animated Stories run could still ship twenty-one scenes of twenty-one
different people, which is the failure the Character Bible was built for.

These cover the caller: that it redraws what the review rejects, that it stops
rather than redrawing forever, and above all that it stays entirely off the
sourced-photo channels.
"""

from __future__ import annotations

import asyncio

import pytest

import core.image_sourcer as image_sourcer
from core.utils import ChannelConfig, Script, ScriptSection, VisualSlot


def _plain_config() -> ChannelConfig:
    """A channel with no `animation` block, like every sourced-photo channel."""
    return ChannelConfig(
        channel_name="Test",
        niche={
            "category": "test",
            "focus": "test",
            "audience": "general",
            "content_style": "informative",
        },
        youtube={
            "tags": ["test"],
            "title_formats": [{"name": "q", "instruction": "q"}],
            "description_styles": [{"name": "s", "instruction": "s"}],
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "hero"}],
    )


def _animated_config() -> ChannelConfig:
    """The real Animated Stories channel, so `enabled()` is exercised honestly."""
    from core.utils import load_channel_config

    return load_channel_config("animated_stories")


def _script() -> Script:
    return Script(
        title="A story",
        video_type="story",
        sections=[
            ScriptSection(
                id=1,
                narration="n",
                estimated_duration_seconds=10.0,
                slots=[
                    VisualSlot(visual="ai_illustration", prompt="a scene", keywords="scene")
                ],
            )
        ],
    )


def _descriptor(raw_dir, name="section_001_01.jpg"):
    path = raw_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"drawn")
    section = _script().sections[0]
    return {
        "section": section,
        "sub_idx": 0,
        "slot": section.slots[0],
        "keywords": "scene",
        "prompt": "a scene with the locked character",
        "img_path": path,
        "lane": "illustration",
        "sourced": True,
    }


def _run(config, descriptors, raw_dir, image_paths, workspace=None):
    return asyncio.run(
        image_sourcer.enforce_character_consistency(
            script=_script(),
            config=config,
            workspace=workspace or raw_dir.parent.parent,
            descriptors=descriptors,
            sourcing_log=[],
            raw_dir=raw_dir,
            image_paths=image_paths,
        )
    )


def _stub_review(monkeypatch, verdicts):
    """Return each verdict in turn, so a redraw can be followed by a re-check."""
    calls = {"n": 0}
    from core import animation_stage

    async def review(script, config, workspace, *, image_paths):
        index = min(calls["n"], len(verdicts) - 1)
        calls["n"] += 1
        return verdicts[index]

    monkeypatch.setattr(animation_stage, "review_character_consistency", review)
    return calls


def _stub_generate(monkeypatch, redrawn: list):
    async def generate(*, descriptors, config, sourcing_log, **kwargs):
        for desc in descriptors:
            redrawn.append(Path_name(desc))
            desc["img_path"].write_bytes(b"redrawn")
            desc["sourced"] = True
        return None

    monkeypatch.setattr(image_sourcer, "_generate_missing_visuals", generate)


def Path_name(desc) -> str:
    return desc["img_path"].name


# --- the channels that must never see this --------------------------------


def test_a_sourced_photo_channel_never_runs_the_review(monkeypatch, tmp_path):
    """Football, Horror Stories and True Stories have no animation block."""
    from core import animation_stage

    called = {"n": 0}

    async def review(*a, **k):
        called["n"] += 1
        return {"passed": False, "rejected": ["section_001_01.jpg"], "reason": "x"}

    monkeypatch.setattr(animation_stage, "review_character_consistency", review)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)
    assert _run(_plain_config(), [desc], raw_dir, [desc["img_path"]]) == 0
    assert called["n"] == 0, "a non-animated channel must not reach the reviewer"


@pytest.mark.parametrize("slug", ["football_news", "horror_stories", "true_stories"])
def test_the_real_sourced_photo_channels_are_not_animated(slug):
    """The guard is `animation.enabled`; these channels must leave it off."""
    from core import animation_stage
    from core.utils import load_channel_config

    try:
        config = load_channel_config(slug)
    except Exception:
        pytest.skip(f"{slug} not configured on this checkout")
    assert animation_stage.enabled(config) is False


def test_animated_stories_is_animated():
    from core import animation_stage

    assert animation_stage.enabled(_animated_config()) is True


# --- the gate itself -------------------------------------------------------


def test_a_consistent_set_is_left_alone(monkeypatch, tmp_path):
    _stub_review(monkeypatch, [{"passed": True, "rejected": [], "reason": "ok"}])
    redrawn: list = []
    _stub_generate(monkeypatch, redrawn)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    assert _run(_animated_config(), [desc], raw_dir, [desc["img_path"]]) == 0
    assert redrawn == []
    assert desc["img_path"].read_bytes() == b"drawn"


def test_a_drifted_scene_is_redrawn_and_rechecked(monkeypatch, tmp_path):
    calls = _stub_review(
        monkeypatch,
        [
            {"passed": False, "rejected": ["section_001_01.jpg"], "reason": "different face"},
            {"passed": True, "rejected": [], "reason": "ok"},
        ],
    )
    redrawn: list = []
    _stub_generate(monkeypatch, redrawn)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    assert _run(_animated_config(), [desc], raw_dir, [desc["img_path"]]) == 1
    assert redrawn == ["section_001_01.jpg"]
    assert desc["img_path"].read_bytes() == b"redrawn"
    assert calls["n"] == 2, "the redraw must be checked again, not assumed fixed"


def test_only_the_rejected_scene_is_redrawn(monkeypatch, tmp_path):
    _stub_review(
        monkeypatch,
        [
            {"passed": False, "rejected": ["section_001_02.jpg"], "reason": "drift"},
            {"passed": True, "rejected": [], "reason": "ok"},
        ],
    )
    redrawn: list = []
    _stub_generate(monkeypatch, redrawn)

    raw_dir = tmp_path / "images" / "raw"
    keep = _descriptor(raw_dir, "section_001_01.jpg")
    drift = _descriptor(raw_dir, "section_001_02.jpg")

    _run(_animated_config(), [keep, drift], raw_dir, [keep["img_path"], drift["img_path"]])

    assert redrawn == ["section_001_02.jpg"]
    assert keep["img_path"].read_bytes() == b"drawn", "a consistent scene was redrawn"


def test_it_stops_rather_than_redrawing_for_ever(monkeypatch, tmp_path):
    """A character still wrong after a redraw will not come right on the tenth."""
    calls = _stub_review(
        monkeypatch,
        [{"passed": False, "rejected": ["section_001_01.jpg"], "reason": "drift"}],
    )
    redrawn: list = []
    _stub_generate(monkeypatch, redrawn)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    _run(_animated_config(), [desc], raw_dir, [desc["img_path"]])

    assert calls["n"] <= image_sourcer._CHARACTER_REVIEW_MAX_ATTEMPTS
    assert len(redrawn) < calls["n"] + 1


def test_a_rejected_file_owned_by_no_beat_does_not_crash(monkeypatch, tmp_path):
    _stub_review(
        monkeypatch,
        [{"passed": False, "rejected": ["section_099_09.jpg"], "reason": "drift"}],
    )
    redrawn: list = []
    _stub_generate(monkeypatch, redrawn)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    assert _run(_animated_config(), [desc], raw_dir, [desc["img_path"]]) == 0
    assert redrawn == []


def test_an_unavailable_reviewer_redraws_nothing(monkeypatch, tmp_path):
    """review_character_consistency returns passed=True when it cannot run."""
    _stub_review(
        monkeypatch,
        [{"passed": True, "rejected": [], "reason": "reviewer unavailable"}],
    )
    redrawn: list = []
    _stub_generate(monkeypatch, redrawn)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    assert _run(_animated_config(), [desc], raw_dir, [desc["img_path"]]) == 0
    assert redrawn == []


def test_no_images_means_no_review(monkeypatch, tmp_path):
    calls = _stub_review(monkeypatch, [{"passed": True, "rejected": [], "reason": ""}])
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)

    assert _run(_animated_config(), [], raw_dir, []) == 0
    assert calls["n"] == 0


# --- a failed redraw must not cost the beat its picture --------------------


def _stub_failed_generate(monkeypatch):
    """A redraw that produces nothing, which is what a 429 storm looks like."""

    async def generate(*, descriptors, config, sourcing_log, **kwargs):
        return None

    monkeypatch.setattr(image_sourcer, "_generate_missing_visuals", generate)


def test_a_failed_redraw_keeps_the_previous_frame(monkeypatch, tmp_path):
    """Deleting first cost a real run sixteen of twenty-one beats.

    The gate used to unlink the drifted image and then ask the generator for a
    new one. When the generator was rate-limited the beat ended up with no file
    at all, and the sourcer's last resort covered it with a text card showing
    the slot's own image brief -- character-bible text on screen. A drifted
    character is a defect; an empty beat is a hole.
    """
    _stub_review(
        monkeypatch,
        [{"passed": False, "rejected": ["section_001_01.jpg"], "reason": "drift"}],
    )
    _stub_failed_generate(monkeypatch)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    _run(_animated_config(), [desc], raw_dir, [desc["img_path"]])

    assert desc["img_path"].exists(), "the beat lost its only picture"
    assert desc["img_path"].read_bytes() == b"drawn"
    assert desc["sourced"] is True, "an existing frame must still count as sourced"


def test_a_failed_redraw_leaves_no_backup_files_behind(monkeypatch, tmp_path):
    _stub_review(
        monkeypatch,
        [{"passed": False, "rejected": ["section_001_01.jpg"], "reason": "drift"}],
    )
    _stub_failed_generate(monkeypatch)

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    _run(_animated_config(), [desc], raw_dir, [desc["img_path"]])

    assert list(raw_dir.glob("*.prechar")) == []


def test_a_successful_redraw_discards_the_previous_frame(monkeypatch, tmp_path):
    _stub_review(
        monkeypatch,
        [
            {"passed": False, "rejected": ["section_001_01.jpg"], "reason": "drift"},
            {"passed": True, "rejected": [], "reason": "ok"},
        ],
    )
    _stub_generate(monkeypatch, [])

    raw_dir = tmp_path / "images" / "raw"
    desc = _descriptor(raw_dir)

    _run(_animated_config(), [desc], raw_dir, [desc["img_path"]])

    assert desc["img_path"].read_bytes() == b"redrawn"
    assert list(raw_dir.glob("*.prechar")) == []


# --- wired into the stage, not merely defined ------------------------------


def test_the_gate_is_called_before_the_relevance_gate():
    """It was dead code before. It must run, and run first."""
    from pathlib import Path as P

    source = (P(__file__).resolve().parent.parent / "core" / "image_sourcer.py").read_text(
        encoding="utf-8"
    )
    assert "await enforce_character_consistency(" in source, "gate is not called"
    gate_at = source.index("await enforce_character_consistency(")
    review_at = source.index("result = await review_gate(")
    assert gate_at < review_at, (
        "a drifted character must be redrawn before image_review approves the frame"
    )
