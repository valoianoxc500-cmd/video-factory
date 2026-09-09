"""Animated Stories: a fourth generation path, additive to the other three.

Most of these tests exist to prove something did NOT change. The feature is
only safe if Football, Horror Stories and True Stories are bit-for-bit the
channels they were, and if nothing on those paths can reach an animation
provider.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core import animated_stories as anim
from core.animated_stories import AnimatedScene, AnimationBudget
from core.providers.animation import MOTION_GUARDRAILS, FalImageToVideoProvider
from core.utils import load_channel_config

CHANNELS = Path("config/channels")
EXISTING = ["football_news", "horror_stories", "true_stories"]


# ── the existing channels are untouched ──────────────────────────────

@pytest.mark.parametrize("slug", EXISTING)
def test_existing_channels_do_not_animate(slug):
    assert load_channel_config(slug).animation.enabled is False


@pytest.mark.parametrize("slug", EXISTING)
def test_existing_channel_configs_mention_no_animation_model(slug):
    text = (CHANNELS / f"{slug}.json").read_text(encoding="utf-8")
    assert "image-to-video" not in text
    assert "stick figure" not in text.lower()


@pytest.mark.parametrize("slug", EXISTING)
def test_existing_channels_keep_their_own_generator(slug):
    """The scene generator for each channel is what it was."""
    cfg = load_channel_config(slug)
    expected = {
        "football_news": "gemini-3.1-flash-lite-image",
        "horror_stories": "fal-ai/flux/schnell",
        "true_stories": "fal-ai/flux/schnell",
    }[slug]
    assert cfg.image_sourcing.generated_fallback_model == expected


def test_football_is_still_web_photos_only():
    assert load_channel_config("football_news").image_sourcing.web_photos_only is True


def test_the_animation_provider_is_not_reachable_from_other_paths():
    """No other module imports it."""
    for module in ("core/image_sourcer.py", "core/thumbnailer.py",
                   "core/providers/scene_images.py",
                   "core/providers/thumbnails.py"):
        text = Path(module).read_text(encoding="utf-8")
        assert "providers.animation" not in text
        assert "FalImageToVideoProvider" not in text


# ── the new channel ──────────────────────────────────────────────────

def test_animated_stories_channel_exists_and_animates():
    cfg = load_channel_config("animated_stories")
    assert cfg.channel_id == "animated_stories"
    assert cfg.animation.enabled is True
    assert cfg.animation.model == "fal-ai/wan/v2.2-5b/image-to-video"


def test_scene_artwork_is_generated_not_searched():
    cfg = load_channel_config("animated_stories")
    assert cfg.image_sourcing.prefer_generated_visuals is True
    assert cfg.image_sourcing.web_photos_only is False
    assert cfg.image_sourcing.generated_fallback_model == "fal-ai/flux/schnell"


@pytest.mark.parametrize("lang,marker", [("en", "What would"), ("ar", "ماذا")])
def test_each_language_closes_in_its_own_language(lang, marker):
    cfg = load_channel_config("animated_stories", language=lang)
    assert marker in cfg.closing_question_fallback


def test_the_thumbnail_strategy_forbids_rendered_text():
    cfg = load_channel_config("animated_stories")
    instruction = cfg.thumbnail_strategies[0].instruction
    assert "NO text" in instruction


# ── style and character consistency ──────────────────────────────────

def test_the_style_matches_the_stick_figure_specification():
    style = anim.STICK_FIGURE_STYLE.lower()
    for phrase in ("round white head", "eyebrows", "stick-figure",
                   "colourful thematic clothing", "dramatic lighting",
                   "saturated colours"):
        assert phrase in style


def test_the_style_excludes_photorealism():
    excl = anim.STYLE_EXCLUSIONS.lower()
    assert "not photorealistic" in excl
    assert "no 3d render" in excl


def test_the_character_sheet_asks_for_one_character_in_several_views():
    prompt = anim.character_sheet_prompt("a night guard in a navy uniform")
    assert "reference sheet" in prompt.lower()
    assert "same character" in prompt.lower()
    assert "front view" in prompt and "side view" in prompt
    assert anim.STICK_FIGURE_STYLE in prompt


def test_every_scene_restates_the_same_character():
    prompt = anim.scene_prompt(
        "the guard walks down a dark corridor",
        character="a night guard in a navy uniform",
    )
    assert "SAME character as the reference sheet" in prompt
    assert "Do not redesign the character" in prompt
    assert anim.STICK_FIGURE_STYLE in prompt


def test_a_scene_without_a_character_still_names_a_subject():
    assert "the main character" in anim.scene_prompt("a door opens", character="")


# ── motion, and what it must never do ────────────────────────────────

def test_motion_guardrails_forbid_the_known_failure_modes():
    g = MOTION_GUARDRAILS.lower()
    for banned in ("no identity change", "no wardrobe change", "no morphing",
                   "no teleporting", "no lip sync", "no pan", "no zoom"):
        assert banned in g


def test_the_motion_prompt_describes_the_beat_not_a_generic_move():
    prompt = anim.motion_prompt("the guard turns and runs from the door")
    assert "turns and runs" in prompt
    assert "Real character and environment movement" in prompt


def test_the_provider_sends_the_guardrails_with_every_request(tmp_path):
    from PIL import Image

    img = tmp_path / "scene.png"
    Image.new("RGB", (640, 360)).save(img)

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"video": {"url": "data:video/mp4;base64,QUJD"}}

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self):
            self.body = None

        async def post(self, url, *, headers=None, json=None):
            self.body = json
            self.url = url
            return _Resp()

        async def get(self, url, timeout=None):
            return _Resp()

    client = _Client()
    provider = FalImageToVideoProvider(api_key="k")
    out = asyncio.run(provider.animate(img, "the guard runs", client=client, seconds=5))
    assert out == b"ABC"
    assert MOTION_GUARDRAILS in client.body["prompt"]
    assert client.body["image_url"].startswith("data:image/png;base64,")
    assert client.body["duration"] == 5
    assert client.url.endswith("fal-ai/wan/v2.2-5b/image-to-video")


def test_the_model_is_configurable():
    provider = FalImageToVideoProvider(model="fal-ai/other/i2v", api_key="k")
    assert provider.url.endswith("fal-ai/other/i2v")


def test_the_provider_needs_a_key():
    assert not FalImageToVideoProvider(api_key="").status().usable


def test_animating_a_missing_image_is_refused(tmp_path):
    provider = FalImageToVideoProvider(api_key="k")
    with pytest.raises(ValueError, match="scene image not found"):
        asyncio.run(provider.animate(tmp_path / "gone.png", "x", client=None))


def test_animating_without_motion_is_refused(tmp_path):
    from PIL import Image

    img = tmp_path / "s.png"
    Image.new("RGB", (64, 64)).save(img)
    provider = FalImageToVideoProvider(api_key="k")
    with pytest.raises(ValueError, match="motion description"):
        asyncio.run(provider.animate(img, "   ", client=None))


# ── narration is the master timeline ─────────────────────────────────

class _Slot:
    def __init__(self, prompt, visual="info_slide"):
        self.prompt = prompt
        self.keywords = ""
        self.visual = visual


class _Section:
    def __init__(self, sid, slots, duration, words=None):
        self.id = sid
        self.slots = slots
        self.actual_duration_seconds = duration
        self.estimated_duration_seconds = duration
        self.word_timestamps = words


class _Script:
    def __init__(self, sections):
        self.sections = sections


def test_scenes_take_their_span_from_the_narration():
    words = [{"word": f"w{i}", "start": i * 1.0, "end": i * 1.0 + 0.9}
             for i in range(10)]
    script = _Script([_Section(1, [_Slot("a"), _Slot("b")], 10.0, words)])
    scenes = anim.scenes_from_script(script)
    assert len(scenes) == 2
    assert scenes[0].start_time == pytest.approx(0.0)
    assert scenes[1].end_time == pytest.approx(9.9, abs=0.1)
    # Contiguous and ordered: no scene starts before the one before it ends.
    assert scenes[1].start_time >= scenes[0].start_time


def test_scenes_fall_back_to_an_even_split_before_narration_exists():
    script = _Script([_Section(1, [_Slot("a"), _Slot("b")], 8.0, None)])
    scenes = anim.scenes_from_script(script)
    assert [round(s.duration, 1) for s in scenes] == [4.0, 4.0]


def test_sections_are_laid_end_to_end_on_one_clock():
    script = _Script([
        _Section(1, [_Slot("a")], 6.0, None),
        _Section(2, [_Slot("b")], 6.0, None),
    ])
    scenes = anim.scenes_from_script(script)
    assert scenes[0].start_time == pytest.approx(0.0)
    assert scenes[1].start_time == pytest.approx(6.0)


def test_a_scene_records_everything_the_timeline_needs():
    scene = AnimatedScene(index=1, section_id=1, beat="b",
                          start_time=0.0, end_time=4.0)
    record = scene.to_record()
    for key in ("start_time", "end_time", "duration", "source_image",
                "animation_provider", "generation_status"):
        assert key in record
    assert record["duration"] == 4.0


# ── the cost guard ───────────────────────────────────────────────────

def test_the_budget_animates_only_what_it_can_afford():
    scenes = [
        AnimatedScene(index=i, section_id=1, beat=f"b{i}",
                      start_time=i * 5.0, end_time=i * 5.0 + 5.0)
        for i in range(12)
    ]
    budget = AnimationBudget(ceiling_usd=0.60, cost_per_clip_usd=0.15)
    anim.plan_scenes(scenes, budget)
    animated = [s for s in scenes if s.kind == "clip"]
    assert len(animated) == 4          # 0.60 / 0.15
    assert len(scenes) - len(animated) == 8
    assert budget.skipped


def test_unaffordable_scenes_are_stills_not_gaps():
    scenes = [
        AnimatedScene(index=i, section_id=1, beat="b",
                      start_time=i * 4.0, end_time=i * 4.0 + 4.0)
        for i in range(6)
    ]
    budget = AnimationBudget(ceiling_usd=0.15, cost_per_clip_usd=0.15)
    anim.plan_scenes(scenes, budget)
    assert all(s.kind in ("clip", "still") for s in scenes)
    assert sum(1 for s in scenes if s.kind == "clip") == 1


def test_beats_too_short_or_too_long_are_never_animated():
    scenes = [
        AnimatedScene(index=1, section_id=1, beat="tiny", start_time=0, end_time=1.0),
        AnimatedScene(index=2, section_id=1, beat="huge", start_time=1, end_time=21.0),
        AnimatedScene(index=3, section_id=1, beat="fine", start_time=21, end_time=25.0),
    ]
    budget = AnimationBudget(ceiling_usd=10.0, cost_per_clip_usd=0.15)
    anim.plan_scenes(scenes, budget)
    assert scenes[0].kind == "still"
    assert scenes[1].kind == "still"
    assert scenes[2].kind == "clip"


def test_the_longest_beats_are_animated_first():
    scenes = [
        AnimatedScene(index=1, section_id=1, beat="short", start_time=0, end_time=3.1),
        AnimatedScene(index=2, section_id=1, beat="long", start_time=4, end_time=9.9),
    ]
    budget = AnimationBudget(ceiling_usd=0.15, cost_per_clip_usd=0.15)
    anim.plan_scenes(scenes, budget)
    assert scenes[1].kind == "clip"
    assert scenes[0].kind == "still"


def test_the_budget_never_goes_over():
    budget = AnimationBudget(ceiling_usd=0.30, cost_per_clip_usd=0.15)
    assert budget.can_afford_one()
    budget.charge()
    budget.charge()
    assert not budget.can_afford_one()
    assert budget.remaining_usd == pytest.approx(0.0)
    assert budget.spent_usd == pytest.approx(0.30)


def test_retries_are_charged_and_counted_separately():
    budget = AnimationBudget(ceiling_usd=1.0, cost_per_clip_usd=0.15)
    budget.charge()
    budget.charge(retry=True)
    assert budget.clips == 1
    assert budget.retries == 1
    assert budget.spent_usd == pytest.approx(0.30)


def test_the_timeline_record_reports_the_whole_run():
    scenes = [
        AnimatedScene(index=1, section_id=1, beat="b", start_time=0, end_time=5.0,
                      kind="clip"),
        AnimatedScene(index=2, section_id=1, beat="b", start_time=5, end_time=9.0),
    ]
    budget = AnimationBudget(ceiling_usd=0.60, cost_per_clip_usd=0.15)
    budget.charge()
    record = anim.timeline_record(scenes, budget)
    assert record["total_scenes"] == 2
    assert record["animated_scenes"] == 1
    assert record["still_scenes"] == 1
    assert record["narration_seconds"] == 9.0
    assert record["animation_spend_usd"] == pytest.approx(0.15)
    assert record["ceiling_usd"] == pytest.approx(0.60)


# ── pricing ──────────────────────────────────────────────────────────

def test_the_animation_model_is_priced():
    from core.costs import _load_pricing_catalog

    entry = next(
        (e for e in _load_pricing_catalog().entries
         if e.model == "fal-ai/wan/v2.2-5b/image-to-video"),
        None,
    )
    assert entry is not None
    assert entry.output_image_rate_usd_per_image == pytest.approx(0.15)
    assert entry.reconciliation_supported is False


def test_the_configured_clip_price_matches_the_catalogue():
    from core.costs import _load_pricing_catalog

    cfg = load_channel_config("animated_stories")
    entry = next(
        e for e in _load_pricing_catalog().entries if e.model == cfg.animation.model
    )
    assert cfg.animation.cost_per_clip_usd == pytest.approx(
        entry.output_image_rate_usd_per_image
    )


# ── the UI registers a third section, not a replacement ──────────────

def test_the_web_ui_declares_three_sections():
    text = Path("web/lib/engines.ts").read_text(encoding="utf-8")
    for section in ('id: "football"', 'id: "story"', 'id: "animated"'):
        assert section in text


def test_the_existing_engines_are_still_registered():
    text = Path("web/lib/engines.ts").read_text(encoding="utf-8")
    for slug in ("football_news", "horror_stories", "true_stories",
                 "animated_stories"):
        assert f'slug: "{slug}"' in text


def test_every_registered_engine_has_a_channel_config():
    import re

    text = Path("web/lib/engines.ts").read_text(encoding="utf-8")
    slugs = re.findall(
        r'slug:\s*"([a-z0-9_]+)",\s*\n\s*label:[^\n]*\n\s*section:', text
    )
    assert "animated_stories" in slugs
    for slug in slugs:
        assert (CHANNELS / f"{slug}.json").exists(), slug
