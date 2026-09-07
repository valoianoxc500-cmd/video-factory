"""A generated frame must fit the frame the renderer will show.

Regression cover for a Horror run that failed final validation with

    Raw image(s) too small: section_001_03.jpg is 1376x768 (minimum 1080x1280),
    section_002_01.jpg is 1376x768 ... (five in all)

1376x768 is what the Gemini image models return at their 16:9 default. Every
downloaded photo goes through `_download_valid_image_bytes`, which checks the
size and reframes; generated frames were written straight to the slot's
filename by the image client and skipped all of it, so a portrait channel
shipped landscape images into a portrait render.

Two halves to the fix, and both are covered here: ask for the aspect ratio the
channel actually renders at, and conform whatever comes back rather than
trusting it.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

import core.image_sourcer as image_sourcer
from core.utils import (
    ChannelConfig,
    Script,
    ScriptSection,
    VisualSlot,
    meets_minimum_source_size,
    minimum_source_size,
)

PORTRAIT = (1080, 1920)


def _write_jpg(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 60, 90)).save(path, format="JPEG")
    return path


def _size(path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


def _config(**sourcing) -> ChannelConfig:
    config = ChannelConfig(
        channel_name="Test",
        niche={
            "category": "test",
            "focus": "test",
            "audience": "general",
            "content_style": "informative",
        },
        youtube={
            "tags": ["test"],
            "title_formats": [{"name": "question", "instruction": "question"}],
            "description_styles": [{"name": "short", "instruction": "short"}],
        },
        image_sourcing={
            "generation_model": "gemini-test",
            "style_prompt_suffix": "suffix",
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "hero"}],
    )
    config.video.resolution = list(PORTRAIT)
    for key, value in sourcing.items():
        setattr(config.image_sourcing, key, value)
    return config


# --- ask for the right shape ---------------------------------------------


def test_a_portrait_channel_asks_for_a_portrait_frame():
    assert image_sourcer.generation_aspect_ratio(PORTRAIT) == "9:16"


def test_a_landscape_channel_still_asks_for_landscape():
    assert image_sourcer.generation_aspect_ratio((1920, 1080)) == "16:9"


def test_a_square_target_asks_for_square():
    assert image_sourcer.generation_aspect_ratio((1080, 1080)) == "1:1"


def test_a_nonsense_target_does_not_crash():
    assert image_sourcer.generation_aspect_ratio((0, 0)) == "9:16"


def test_the_fallback_generator_requests_the_render_shape(monkeypatch, tmp_path):
    """The exact defect: this call took the client's 16:9 default."""
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    section = ScriptSection(
        id=1,
        narration="narration",
        estimated_duration_seconds=10.0,
        slots=[VisualSlot(visual="ai_photo", prompt="a scene", keywords="scene")],
    )
    descriptor = {
        "section": section,
        "sub_idx": 0,
        "slot": section.slots[0],
        "keywords": "scene",
        "prompt": "a scene",
        "img_path": raw_dir / "section_001_01.jpg",
        "lane": "photo",
        "sourced": False,
    }

    seen: dict = {}

    async def fake_generate(prompt, output_path, **kwargs):
        seen.update(kwargs)
        _write_jpg(output_path, PORTRAIT)
        return output_path

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate)

    asyncio.run(
        image_sourcer._generate_missing_visuals(
            descriptors=[descriptor],
            config=_config(allow_generated_fallback=True),
            sourcing_log=[],
        )
    )

    assert seen.get("aspect_ratio") == "9:16", (
        "the fallback generator must ask for the channel's own shape"
    )


# --- conform whatever comes back -----------------------------------------


def test_a_landscape_frame_is_reframed_to_the_target(tmp_path):
    """1376x768 is the exact shape that failed the run."""
    path = _write_jpg(tmp_path / "section_001_03.jpg", (1376, 768))

    assert image_sourcer._conform_image_to_target(
        path, target_size=PORTRAIT, label="section_001_03.jpg"
    )

    assert _size(path) == PORTRAIT
    assert meets_minimum_source_size(*_size(path), PORTRAIT)


def test_a_conformed_frame_would_pass_final_validation(tmp_path):
    path = _write_jpg(tmp_path / "section_001_03.jpg", (1376, 768))
    min_w, min_h = minimum_source_size(PORTRAIT)

    assert not meets_minimum_source_size(1376, 768, PORTRAIT), (
        f"1376x768 must be under the {min_w}x{min_h} minimum, or this test "
        f"is not reproducing the failure"
    )
    image_sourcer._conform_image_to_target(
        path, target_size=PORTRAIT, label="x"
    )
    assert meets_minimum_source_size(*_size(path), PORTRAIT)


def test_an_already_correct_frame_is_left_untouched(tmp_path):
    path = _write_jpg(tmp_path / "section_001_01.jpg", PORTRAIT)
    before = path.read_bytes()

    assert image_sourcer._conform_image_to_target(
        path, target_size=PORTRAIT, label="x"
    )
    assert path.read_bytes() == before, "a valid frame must not be re-encoded"


def test_a_hopelessly_small_frame_is_rejected_and_deleted(tmp_path):
    """Blowing a thumbnail up to 1080x1920 is not preserving quality."""
    path = _write_jpg(tmp_path / "section_001_01.jpg", (64, 36))

    assert not image_sourcer._conform_image_to_target(
        path, target_size=PORTRAIT, label="x"
    )
    assert not path.exists(), (
        "an unusable file left on disk gets renamed into a surviving beat's "
        "slot when the unsourced one is dropped"
    )


def test_an_unreadable_file_is_rejected_and_deleted(tmp_path):
    path = tmp_path / "section_001_01.jpg"
    path.write_bytes(b"not an image")

    assert not image_sourcer._conform_image_to_target(
        path, target_size=PORTRAIT, label="x"
    )
    assert not path.exists()


def test_reframing_keeps_the_target_aspect_ratio_exactly(tmp_path):
    for source in [(1376, 768), (2048, 1152), (1200, 1200), (900, 1600)]:
        path = _write_jpg(tmp_path / f"s_{source[0]}x{source[1]}.jpg", source)
        assert image_sourcer._conform_image_to_target(
            path, target_size=PORTRAIT, label="x"
        ), f"{source} should have been usable"
        assert _size(path) == PORTRAIT, f"{source} came out the wrong shape"


# --- nothing invalid survives to validation -------------------------------


def test_an_undersized_image_counts_as_a_missing_beat(tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    videos_dir = tmp_path / "videos" / "raw"
    _write_jpg(raw_dir / "section_001_01.jpg", (1376, 768))

    found = image_sourcer._expected_asset_path(
        section_id=1,
        sub_idx=1,
        raw_dir=raw_dir,
        videos_dir=videos_dir,
        target_size=PORTRAIT,
    )

    assert found is None, "a landscape frame is not a usable portrait asset"
    assert not (raw_dir / "section_001_01.jpg").exists(), (
        "the invalid file must be removed, not just ignored"
    )


def test_a_valid_image_is_still_found(tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    videos_dir = tmp_path / "videos" / "raw"
    good = _write_jpg(raw_dir / "section_001_01.jpg", PORTRAIT)

    assert image_sourcer._expected_asset_path(
        section_id=1,
        sub_idx=1,
        raw_dir=raw_dir,
        videos_dir=videos_dir,
        target_size=PORTRAIT,
    ) == good


def test_a_clip_is_not_judged_on_image_dimensions(tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    videos_dir = tmp_path / "videos" / "raw"
    videos_dir.mkdir(parents=True)
    clip = videos_dir / "section_001_01.mp4"
    clip.write_bytes(b"clip")

    assert image_sourcer._expected_asset_path(
        section_id=1,
        sub_idx=1,
        raw_dir=raw_dir,
        videos_dir=videos_dir,
        target_size=PORTRAIT,
    ) == clip


def test_reconciliation_reports_an_undersized_beat_as_missing(tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    _write_jpg(raw_dir / "section_001_01.jpg", (1376, 768))
    _write_jpg(raw_dir / "section_001_02.jpg", PORTRAIT)

    script = Script(
        title="A story",
        video_type="story",
        sections=[
            ScriptSection(
                id=1,
                narration="narration",
                estimated_duration_seconds=10.0,
                slots=[
                    VisualSlot(visual="ai_photo", prompt="a", keywords="a"),
                    VisualSlot(visual="ai_photo", prompt="b", keywords="b"),
                ],
            )
        ],
    )

    missing = image_sourcer._missing_expected_slots(
        script, _config(), raw_dir, tmp_path / "videos" / "raw"
    )

    assert missing == [(1, 1)], (
        "the landscape beat must be re-sourced; the portrait one must not"
    )


@pytest.mark.parametrize("bad_size", [(1376, 768), (1024, 576), (800, 450)])
def test_landscape_generations_never_reach_validation(monkeypatch, tmp_path, bad_size):
    """Whatever shape the model returns, the beat is left valid or unsourced."""
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    section = ScriptSection(
        id=1,
        narration="narration",
        estimated_duration_seconds=10.0,
        slots=[VisualSlot(visual="ai_photo", prompt="a scene", keywords="scene")],
    )
    descriptor = {
        "section": section,
        "sub_idx": 0,
        "slot": section.slots[0],
        "keywords": "scene",
        "prompt": "a scene",
        "img_path": raw_dir / "section_001_01.jpg",
        "lane": "photo",
        "sourced": False,
    }

    async def ignores_the_request(prompt, output_path, **kwargs):
        _write_jpg(output_path, bad_size)
        return output_path

    monkeypatch.setattr(
        image_sourcer.clients, "generate_image_gemini", ignores_the_request
    )

    asyncio.run(
        image_sourcer._generate_missing_visuals(
            descriptors=[descriptor],
            config=_config(allow_generated_fallback=True),
            sourcing_log=[],
        )
    )

    path = raw_dir / "section_001_01.jpg"
    if descriptor["sourced"]:
        assert meets_minimum_source_size(*_size(path), PORTRAIT), (
            "a beat marked sourced must hold an asset validation accepts"
        )
    else:
        assert not path.exists(), "a rejected frame must not be left behind"
