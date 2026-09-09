"""Offline regression coverage for Animated Stories cost and recovery guards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from core import animation_stage
from core import animated_budget
from core import character_bible as cb
from core import image_sourcer
from core.utils import Script, ScriptSection, VisualSlot, load_channel_config


def _config():
    return load_channel_config("animated_stories")


def _script(prompt: str = "Sam runs down the hall and climbs the railing"):
    return Script(
        title="The locked door",
        video_type="story",
        sections=[ScriptSection(
            id=1,
            narration=prompt,
            estimated_duration_seconds=4.0,
            slots=[VisualSlot(visual="ai_illustration", prompt=prompt, keywords="Sam hallway")],
        )],
    )


def test_existing_character_sheet_is_reused_without_a_paid_call(monkeypatch, tmp_path):
    config = _config()
    character_dir = tmp_path / "character"
    character_dir.mkdir()
    sheet = character_dir / "char_01_sheet.png"
    sheet.write_bytes(b"s" * 2048)

    async def bible(*args, **kwargs):
        return cb.bible_from_entries([{"name": "Sam", "role": "main"}])

    async def must_not_generate(*args, **kwargs):
        raise AssertionError("a valid reference sheet must be reused on resume")

    monkeypatch.setattr(animation_stage.bible_mod, "build_bible", bible)
    monkeypatch.setattr(animation_stage.clients, "generate_scene_image", must_not_generate)
    result = asyncio.run(animation_stage.build_character_sheet(
        _script(), config, tmp_path, plan={"topic": "t"},
    ))
    assert result == sheet
    record = (character_dir / "character_bible.json").read_text(encoding="utf-8")
    assert '"sheet": "char_01_sheet.png"' in record


def test_paid_motion_retry_never_exceeds_its_emergency_budget(monkeypatch, tmp_path):
    config = _config()
    config.animation.ai_motion_budget_usd = 0.15
    config.animation.cost_per_clip_usd = 0.15
    config.animation.max_attempts_per_scene = 2
    raw = tmp_path / "images" / "raw"
    raw.mkdir(parents=True)
    (raw / "section_001_01.jpg").write_bytes(b"scene")

    class Provider:
        TIMEOUT = 1.0
        name = "mock"

        def __init__(self, model):
            self.model = model

        def status(self):
            return SimpleNamespace(usable=True, reason="")

        async def animate(self, *args, **kwargs):
            raise RuntimeError("rate limited")

    monkeypatch.setattr("core.providers.animation.FalImageToVideoProvider", Provider)
    record = asyncio.run(animation_stage.animate_scenes(_script(), config, tmp_path))

    assert record["animation_spend_usd"] == 0.15
    assert record["retries"] == 0
    scene = record["scenes"][0]
    assert scene["kind"] == "local"
    assert scene["generation_status"] == "failed_fell_back_to_local"


def test_configured_generation_target_is_within_the_approved_range():
    config = _config()
    assert 0.30 <= config.animation.max_animation_usd <= 0.40
    assert config.animation.ai_motion_budget_usd <= config.animation.max_animation_usd
    assert config.animation.max_ai_clips == 1


def test_shared_total_budget_covers_sheet_scene_art_redraws_and_motion(tmp_path):
    config = _config()
    budget = animated_budget.activate(tmp_path, config)
    assert budget is not None

    # Ten Flash-image reservations (one sheet plus scene/redraw calls) leave
    # one cent; a paid $0.15 motion clip cannot breach the same $0.40 ceiling.
    for index in range(10):
        assert budget.reserve(
            kind="character_sheet" if index == 0 else "scene_art",
            cost_usd=0.039,
            detail=str(index),
        )
    assert budget.spent_usd == 0.39
    assert budget.reserve(kind="image_to_video", cost_usd=0.15) is False
    assert budget.spent_usd <= 0.40


def test_exhausted_shared_budget_skips_scene_generation_and_preserves_fallback(monkeypatch, tmp_path):
    config = _config()
    budget = animated_budget.activate(tmp_path, config)
    assert budget is not None
    assert budget.reserve(kind="scene_art", cost_usd=0.40)

    async def must_not_generate(*args, **kwargs):
        raise AssertionError("scene generation exceeded the shared total budget")

    monkeypatch.setattr(image_sourcer.clients, "generate_scene_image", must_not_generate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await image_sourcer._source_single_image(
                "Sam at the locked door", "Sam at the locked door", "ai_gen",
                config, tmp_path / "scene.jpg", client, set(), "photo",
            )

    assert asyncio.run(run()) is None
    assert budget.spent_usd == 0.40
