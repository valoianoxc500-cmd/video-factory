"""Tests for thumbnail strategy generation helpers."""

import asyncio

import pytest
from PIL import Image

import core.thumbnailer as thumbnailer
from core.thumbnailer import (
    THUMBNAIL_SIZE,
    _generate_ai_thumbnail,
    _thumbnail_content_context,
    _thumbnail_reference_instruction,
    _thumbnail_reference_image,
    _thumbnail_strategy_by_name,
    create_thumbnail,
)
from core.utils import ChannelConfig, Script, ScriptSection, VisualSlot


def _config() -> ChannelConfig:
    return ChannelConfig(
        channel_name="Test",
        niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[
            {"name": "hero", "instruction": "Make one bold hero subject."},
            {"name": "data", "instruction": "Make one abstract evidence fragment."},
        ],
    )


def _script() -> Script:
    return Script(
        title="Workflow habits",
        video_type="listicle",
        thumbnail_text="REPAIRS MUSCLE",
        thumbnail_brief="A checklist on a dark educational thumbnail.",
        thumbnail_strategy="hero",
        sections=[
            ScriptSection(
                id=1,
                narration="Extra details about oil.",
                slots=[
                    VisualSlot(
                        visual="title_banner",
                        prompt="Realistic photo of extra virgin olive oil in a glass bottle on a wooden table.",
                        keywords="extra virgin olive oil bottle real photo",
                        props={"title": "Extra Virgin Olive Oil"},
                    )
                ],
            ),
            ScriptSection(
                id=2,
                narration="Extra details about salmon.",
                slots=[
                    VisualSlot(
                        visual="title_banner",
                        prompt="Realistic photo of wild-caught salmon fillets on ice.",
                        keywords="wild caught salmon fillets real photo",
                        props={"title": "Wild-Caught Salmon"},
                    )
                ],
            ),
        ],
    )


def test_thumbnail_content_context_uses_title_banner_subjects():
    assert _thumbnail_content_context(_script()) == (
        "Section 1: Extra Virgin Olive Oil; Section 2: Wild-Caught Salmon"
    )


def test_thumbnail_strategy_lookup_by_name():
    strategy = _thumbnail_strategy_by_name(_config(), "data")

    assert strategy.name == "data"
    assert strategy.instruction == "Make one abstract evidence fragment."


def test_thumbnail_strategy_lookup_rejects_unknown_name():
    with pytest.raises(ValueError, match="Unknown thumbnail strategy"):
        _thumbnail_strategy_by_name(_config(), "missing")


def test_thumbnail_reference_image_resolves_assets_path(monkeypatch, tmp_path):
    ref_dir = tmp_path / "channels" / "demo_channel"
    ref_dir.mkdir(parents=True)
    ref_path = ref_dir / "reference.png"
    ref_path.write_bytes(b"image")
    monkeypatch.setattr(thumbnailer, "ASSETS_DIR", tmp_path)
    strategy = _thumbnail_strategy_by_name(
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[
                {
                    "name": "reference",
                    "instruction": "Use a reference image.",
                    "reference_image": "channels/demo_channel/reference.png",
                    "reference_instruction": "Use the reference subject.",
                }
            ],
        ),
        "reference",
    )

    assert _thumbnail_reference_image(strategy) == ref_path


def test_thumbnail_reference_image_fails_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(thumbnailer, "ASSETS_DIR", tmp_path)
    strategy = _thumbnail_strategy_by_name(
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[
                {
                    "name": "reference",
                    "instruction": "Use a reference image.",
                    "reference_image": "channels/demo_channel/reference.png",
                    "reference_instruction": "Use the reference subject.",
                }
            ],
        ),
        "reference",
    )

    with pytest.raises(FileNotFoundError, match="reference_image not found"):
        _thumbnail_reference_image(strategy)


def test_thumbnail_reference_instruction_returns_config_value():
    strategy = _thumbnail_strategy_by_name(
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[
                {
                    "name": "reference",
                    "instruction": "Use a reference image.",
                    "reference_image": "channels/demo_channel/reference.png",
                    "reference_instruction": "Keep the same reference subject.",
                }
            ],
        ),
        "reference",
    )

    assert _thumbnail_reference_instruction(strategy) == "Keep the same reference subject."


def test_generate_ai_thumbnail_passes_prompt_inputs(monkeypatch, tmp_path):
    captured = {}

    def fake_prompt(**kwargs):
        captured.update(kwargs)
        return "thumbnail prompt"

    async def fake_edit(prompt, output_path, *, source_images, aspect_ratio="16:9",
                        operation_label=None):
        # The strategy prompt still leads; the identity instruction is
        # appended after it.
        assert prompt.startswith("thumbnail prompt")
        assert operation_label == "thumbnail_generate"
        Image.new("RGB", THUMBNAIL_SIZE, color=(0, 0, 0)).save(output_path)
        return output_path

    monkeypatch.setattr(thumbnailer.prompts, "thumbnail_generation_prompt", fake_prompt)
    monkeypatch.setattr(thumbnailer.clients, "edit_thumbnail_image", fake_edit)

    source = tmp_path / "source.png"
    Image.new("RGB", (1200, 800), color=(9, 9, 9)).save(source)

    output_path = tmp_path / "thumbnail.png"
    asyncio.run(_generate_ai_thumbnail(
        title="Workflow habits",
        thumbnail_text="REPAIRS MUSCLE",
        thumbnail_brief="Bowl of powder",
        strategy_instruction="Hero subject",
        content_context="Section 1: Powder",
        config=_config(),
        output_path=output_path,
        revision_notes="Make text clearer",
        source_images=[source],
    ))

    assert output_path.exists()
    assert captured["thumbnail_text"] == "REPAIRS MUSCLE"
    assert captured["thumbnail_brief"] == "Bowl of powder"
    assert captured["strategy_instruction"] == "Hero subject"
    assert captured["revision_notes"] == "Make text clearer"


def test_generate_ai_thumbnail_passes_reference_image(monkeypatch, tmp_path):
    captured = {}
    reference_image = tmp_path / "reference.png"
    reference_image.write_bytes(b"image")

    def fake_prompt(**kwargs):
        return "thumbnail prompt"

    async def fake_edit(prompt, output_path, *, source_images, aspect_ratio="16:9",
                        operation_label=None):
        captured["prompt"] = prompt
        captured["source_images"] = list(source_images)
        captured["operation_label"] = operation_label
        Image.new("RGB", THUMBNAIL_SIZE, color=(0, 0, 0)).save(output_path)
        return output_path

    monkeypatch.setattr(thumbnailer.prompts, "thumbnail_generation_prompt", fake_prompt)
    monkeypatch.setattr(thumbnailer.clients, "edit_thumbnail_image", fake_edit)

    output_path = tmp_path / "thumbnail.png"
    asyncio.run(_generate_ai_thumbnail(
        title="Workflow habits",
        thumbnail_text="REPAIRS MUSCLE",
        thumbnail_brief="Bowl of powder",
        strategy_instruction="Hero subject",
        content_context="Section 1: Powder",
        config=_config(),
        output_path=output_path,
        reference_image=reference_image,
        reference_instruction="Keep the same reference subject.",
    ))

    # The strategy's reference art reaches the edit as a source image rather
    # than as its own parameter: the edit model takes pictures, not slots.
    assert reference_image in captured["source_images"]
    assert captured["operation_label"] == "thumbnail_generate"
    assert "REFERENCE IMAGE INSTRUCTION" in captured["prompt"]
    assert "Keep the same reference subject." in captured["prompt"]


def test_generate_ai_thumbnail_fails_when_model_returns_no_image(monkeypatch, tmp_path):
    async def fake_edit(prompt, output_path, *, source_images, aspect_ratio="16:9",
                        operation_label=None):
        return None

    monkeypatch.setattr(thumbnailer.clients, "edit_thumbnail_image", fake_edit)

    source = tmp_path / "source.png"
    Image.new("RGB", (1200, 800), color=(9, 9, 9)).save(source)

    with pytest.raises(RuntimeError, match="did not produce an image"):
        asyncio.run(_generate_ai_thumbnail(
            title="Workflow habits",
            thumbnail_text="REPAIRS MUSCLE",
            thumbnail_brief="Bowl of powder",
            strategy_instruction="Hero subject",
            content_context="Section 1: Powder",
            config=_config(),
            output_path=tmp_path / "thumbnail.png",
            source_images=[source],
        ))


def test_create_thumbnail_always_runs_review_gate(monkeypatch, tmp_path):
    async def fake_edit(prompt, output_path, *, source_images, aspect_ratio="16:9",
                        operation_label=None):
        assert operation_label == "thumbnail_generate"
        Image.new("RGB", THUMBNAIL_SIZE, color=(0, 0, 0)).save(output_path)
        return output_path

    # No sourced images in this workspace, so the stage generates a base and
    # then edits it -- the final picture still comes from the edit model.
    async def fake_base(prompt, output_path, *, reference_image=None,
                        aspect_ratio="16:9", operation_label=None):
        Image.new("RGB", THUMBNAIL_SIZE, color=(0, 0, 0)).save(output_path)
        return output_path

    async def fake_review_gate(**kwargs):
        assert kwargs["gate_name"] == "thumbnail_review"
        assert kwargs["image_paths"] == [tmp_path / "thumbnail.png"]
        return {"approved": True, "attempts": 1, "flagged_for_review": False}

    monkeypatch.setattr(thumbnailer.clients, "edit_thumbnail_image", fake_edit)
    monkeypatch.setattr(thumbnailer.clients, "generate_image_gemini", fake_base)
    monkeypatch.setattr(thumbnailer, "review_gate", fake_review_gate)

    result = asyncio.run(create_thumbnail(_script(), _config(), tmp_path))

    assert result["approved"] is True
    assert result["attempts"] == 1
