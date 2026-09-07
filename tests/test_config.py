"""Config loading, schema validation, and utility tests."""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from core import assembler
from settings import settings
from core.utils import (
    ChannelConfig,
    Checkpoint,
    TemplateStyle,
    ThumbnailStrategyConfig,
    apply_dot_override,
    create_workspace,
    find_output_video,
    load_channel_config,
    load_checkpoint,
    save_checkpoint,
    select_least_recent,
)

CHANNELS_DIR = Path(__file__).resolve().parent.parent / "config" / "channels"


# ── Config loading ────────────────────────────────────────────────

def test_channel_config_loads(channel_slug, channel_config):
    assert channel_config.channel_name
    assert channel_config.thumbnail_strategies
    assert channel_config.niche.category


def test_channel_config_has_style(channel_config):
    assert isinstance(channel_config.style, TemplateStyle)
    assert channel_config.style.name  # every channel should have a named style


# ── Dot overrides ─────────────────────────────────────────────────

def test_dot_override_applies():
    data = {"video": {"fps": 30}}
    apply_dot_override(data, "video.fps=60")
    assert data["video"]["fps"] == 60


def test_dot_override_list_index():
    data = {"thumbnail_strategies": [{"name": "a"}, {"name": "b"}]}
    apply_dot_override(data, "thumbnail_strategies.0.name=c")
    assert data["thumbnail_strategies"][0]["name"] == "c"
    assert data["thumbnail_strategies"][1]["name"] == "b"


def test_dot_override_creates_nested():
    data = {}
    apply_dot_override(data, "a.b.c=hello")
    assert data["a"]["b"]["c"] == "hello"


def test_dot_override_bool_cast():
    data = {}
    apply_dot_override(data, "flag=true")
    assert data["flag"] is True


# ── Thumbnail strategy validation ─────────────────────────────────

def test_thumbnail_strategy_requires_instruction():
    with pytest.raises(Exception):
        ThumbnailStrategyConfig(name="hero")


def test_thumbnail_strategy_rejects_empty_values():
    with pytest.raises(Exception):
        ThumbnailStrategyConfig(name="", instruction="Make a thumbnail")
    with pytest.raises(Exception):
        ThumbnailStrategyConfig(name="hero", instruction=" ")
    with pytest.raises(Exception):
        ThumbnailStrategyConfig(name="hero", instruction="Make a thumbnail", reference_image="")
    with pytest.raises(Exception):
        ThumbnailStrategyConfig(
            name="reference",
            instruction="Make a thumbnail",
            reference_image="channels/demo_channel/reference.png",
        )


def test_thumbnail_strategy_accepts_name_and_instruction():
    cfg = ThumbnailStrategyConfig(name="hero", instruction="Make a strong thumbnail")
    assert cfg.name == "hero"
    assert cfg.instruction == "Make a strong thumbnail"


def test_thumbnail_strategy_accepts_reference_image():
    cfg = ThumbnailStrategyConfig(
        name="reference",
        instruction="Use a reference image",
        reference_image="channels/demo_channel/reference.png",
        reference_instruction="Use the reference subject.",
    )

    assert cfg.reference_image == "channels/demo_channel/reference.png"
    assert cfg.reference_instruction == "Use the reference subject."


def test_thumbnail_strategies_must_be_nonempty():
    with pytest.raises(Exception):
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[],
        )


def test_thumbnail_strategies_accept_one_or_many():
    single = ChannelConfig(
        channel_name="Test",
        niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
    )
    many = ChannelConfig(
        channel_name="Test",
        niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
        youtube={
            "tags": ["t"],
            "title_formats": [{"name": "a", "instruction": "a"}],
            "description_styles": [{"name": "a", "instruction": "a"}],
        },
        thumbnail_strategies=[
            {"name": "hero", "instruction": "Make a hero thumbnail"},
            {"name": "data", "instruction": "Make a data thumbnail"},
        ],
    )

    assert len(single.thumbnail_strategies) == 1
    assert len(many.thumbnail_strategies) == 2


def test_business_strategy_requires_content_families():
    with pytest.raises(Exception):
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
            business_strategy={
                "channel_goal": "Sell products from content.",
                "video_jobs": ["Get the click."],
                "content_families": [],
                "mid_ticket_offer": "Main offer",
                "launch_strategy": "Launch later.",
            },
        )


def test_demo_channel_has_public_thumbnail_strategy():
    cfg = load_channel_config("demo_channel")
    assert [s.name for s in cfg.thumbnail_strategies] == ["demo_thumbnail"]
    strategy = next(s for s in cfg.thumbnail_strategies if s.name == "demo_thumbnail")

    assert strategy.reference_image is None
    assert strategy.reference_instruction is None
    assert "educational thumbnail" in strategy.instruction
    assert "exact thumbnail_text" in strategy.instruction


def test_demo_channel_business_strategy_has_three_content_families():
    cfg = load_channel_config("demo_channel")
    strategy = cfg.business_strategy

    assert strategy is not None
    assert [family.name for family in strategy.content_families] == [
        "practical_checklist",
        "concept_explainer",
        "workflow_story",
    ]
    assert strategy.content_families[0].lead_magnet == ""
    assert strategy.content_families[1].low_ticket_offer == ""
    assert strategy.mid_ticket_offer == ""
    assert strategy.cta_rules[0] == "Primary CTA is a simple subscribe CTA."


def test_all_channels_use_single_image_generation_model(channel_config):
    # Pinned to the configured default rather than a literal, so migrating the
    # image model in settings/.env doesn't require editing every channel test.
    assert channel_config.image_sourcing.generation_model == settings.gemini_image_model


def test_demo_channel_enables_inline_component_art():
    cfg = load_channel_config("demo_channel")

    assert cfg.image_sourcing.generate_info_slide_illustrations is True
    assert cfg.image_sourcing.generate_info_card_illustrations is True


def test_enabled_video_type_requires_allowed_thumbnail_strategies():
    with pytest.raises(Exception, match="must declare allowed_thumbnail_strategies"):
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
            video_types={
                "listicle": {
                    "enabled": True,
                    "section_style": "s",
                    "pacing": "p",
                }
            },
        )


def test_enabled_video_type_rejects_unknown_allowed_thumbnail_strategy():
    with pytest.raises(Exception, match="references unknown thumbnail strategies: reference"):
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
            video_types={
                "listicle": {
                    "enabled": True,
                    "section_style": "s",
                    "pacing": "p",
                    "allowed_thumbnail_strategies": ["reference"],
                }
            },
        )


def test_enabled_video_type_rejects_duplicate_allowed_thumbnail_strategies():
    with pytest.raises(Exception, match="has duplicate allowed_thumbnail_strategies"):
        ChannelConfig(
            channel_name="Test",
            niche={"category": "Test", "focus": "Test", "audience": "Test", "content_style": "Test"},
            youtube={
                "tags": ["t"],
                "title_formats": [{"name": "a", "instruction": "a"}],
                "description_styles": [{"name": "a", "instruction": "a"}],
            },
            thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
            video_types={
                "listicle": {
                    "enabled": True,
                    "section_style": "s",
                    "pacing": "p",
                    "allowed_thumbnail_strategies": ["hero", "hero"],
                }
            },
        )


# ── Checkpoint ────────────────────────────────────────────────────

def test_checkpoint_roundtrip(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    cp = Checkpoint(channel="test", started_at="2026-01-01T00:00:00", workspace_dir=str(ws))
    cp.stages_completed = ["planning", "script"]
    cp.current_stage = "script"
    save_checkpoint(ws, cp)
    loaded = load_checkpoint(ws)
    assert loaded.channel == "test"
    assert loaded.stages_completed == ["planning", "script"]
    assert loaded.current_stage == "script"


# ── Workspace creation ────────────────────────────────────────────

def test_create_workspace_is_unique():
    root = Path("C:/Projects/video-factory/workspace")
    with patch("core.utils.WORKSPACE_DIR", root), patch("pathlib.Path.mkdir"):
        first = create_workspace("channel")
        second = create_workspace("channel")
    assert first.name != second.name


def test_find_output_video_ignores_output_mp4_and_picks_newest_descriptive(tmp_path):
    legacy = tmp_path / "output.mp4"
    old = tmp_path / "2026-04-07_120000_old-title.mp4"
    new = tmp_path / "2026-04-07_130000_new-title.mp4"
    clip = tmp_path / "clip_preview.mp4"

    legacy.write_bytes(b"legacy")
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    clip.write_bytes(b"clip")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    os.utime(legacy, (3, 3))
    os.utime(clip, (4, 4))

    assert find_output_video(tmp_path) == new


def test_find_output_video_requires_descriptive_mp4(tmp_path):
    (tmp_path / "output.mp4").write_bytes(b"legacy")

    with pytest.raises(FileNotFoundError):
        find_output_video(tmp_path)


# ── Style ────────────────────────────────────────────────────────

def test_demo_channel_no_sepia_filter():
    config = load_channel_config("demo_channel")
    effects = config.style.effects
    assert effects.get("color_filter") != "sepia"


def test_channel_styles_have_transition_pool(channel_config):
    pool = channel_config.style.video.get("transition_pool")
    assert pool and len(pool) >= 1, "style.video.transition_pool must be a non-empty list"


# Phrases that only ever came from demo_channel's productivity-explainer copy.
# A football channel carrying these is steering its planner and scripter with
# the wrong exemplars, which surfaces later as off-topic scripts and image
# queries that no football photo can satisfy.
_DEMO_LEFTOVER_MARKERS = (
    "everyday habits",
    "design choices that make tools easier",
    "simple systems are easier to maintain",
    "The Simple Workflow That Saved an Afternoon",
    "Digital Tools Easier to Use",
)


@pytest.mark.parametrize("marker", _DEMO_LEFTOVER_MARKERS)
def test_football_config_has_no_demo_channel_leftovers(marker):
    """football_news must not inherit demo_channel's example content.

    These fields are prompt inputs, not documentation: example_topics,
    video_types[*].example and script_style all reach the model.
    """
    raw = (
        Path(__file__).resolve().parents[1]
        / "config" / "channels" / "football_news.json"
    ).read_text(encoding="utf-8")
    assert marker.lower() not in raw.lower(), (
        f"football_news.json still contains demo_channel copy: {marker!r}"
    )


def _remotion_supported_transitions() -> set[str]:
    """Transition names the Remotion renderer can actually draw.

    Read out of transitions.ts rather than duplicated here, so adding a
    transition on one side and forgetting the other is caught.
    """
    src = (
        Path(__file__).resolve().parents[1]
        / "rendering" / "remotion" / "src" / "lib" / "transitions.ts"
    ).read_text(encoding="utf-8")
    # The declared type contains "=>", so scan lazily to the opening brace
    # rather than trying to bracket-match the generic.
    body = re.search(r"const TRANSITIONS\b.*?=\s*\{(.*?)\n\};", src, re.S)
    assert body, "could not locate the TRANSITIONS record in transitions.ts"
    return set(re.findall(r"^\s*(\w+)\s*[,:]", body.group(1), re.M))


def test_transition_pool_names_are_renderable(channel_config):
    """Every configured transition must exist in both render paths.

    `dissolve` sat in the football pool while transitions.ts implemented only
    fade/wipeleft/cut, so every Remotion boundary quietly rendered a plain
    fade. Nothing failed -- the videos just silently lost half their
    transition variety.
    """
    supported = _remotion_supported_transitions() | set(assembler._TRANSITION_MAP)
    pool = channel_config.style.video.get("transition_pool") or []
    unknown = sorted(set(pool) - supported)
    assert not unknown, (
        f"transition_pool references {unknown}, which no render path implements. "
        f"Supported: {sorted(supported)}"
    )


# ── Script structure diversification ─────────────────────────────

def test_channels_have_multiple_video_types(channel_config):
    enabled = [n for n, vt in channel_config.video_types.items() if vt.enabled]
    assert len(enabled) >= 2, f"Need >= 2 enabled video types, got {enabled}"


def test_video_types_have_narrative_hooks(channel_config):
    for name, vt in channel_config.video_types.items():
        if vt.enabled and vt.narrative_hooks:
            assert len(vt.narrative_hooks) >= 3, (
                f"{name}: narrative_hooks has {len(vt.narrative_hooks)} entries, need >= 3"
            )


def test_enabled_video_types_have_allowed_thumbnail_strategies(channel_config):
    strategy_names = {s.name for s in channel_config.thumbnail_strategies}
    for name, vt in channel_config.video_types.items():
        if not vt.enabled:
            continue
        assert vt.allowed_thumbnail_strategies, (
            f"{name}: enabled video types must declare allowed_thumbnail_strategies"
        )
        assert len(vt.allowed_thumbnail_strategies) == len(set(vt.allowed_thumbnail_strategies)), (
            f"{name}: allowed_thumbnail_strategies contains duplicates"
        )
        assert set(vt.allowed_thumbnail_strategies) <= strategy_names, (
            f"{name}: unknown thumbnail strategy in allowed_thumbnail_strategies"
        )


def test_narrative_hooks_are_nonempty_strings(channel_config):
    for name, vt in channel_config.video_types.items():
        for i, hook in enumerate(vt.narrative_hooks):
            assert isinstance(hook, str) and hook.strip(), (
                f"{name}.narrative_hooks[{i}] is empty or not a string"
            )


def test_select_least_recent_rotates_video_type():
    history = [
        {"video_type": "listicle"},
        {"video_type": "narrative"},
        {"video_type": "listicle"},
    ]
    options = ["listicle", "narrative", "countdown"]
    idx = select_least_recent(options, history, "video_type")
    assert options[idx] == "countdown"  # never used — picked first


def test_select_least_recent_picks_oldest():
    history = [
        {"video_type": "listicle"},
        {"video_type": "countdown"},
        {"video_type": "narrative"},
    ]
    options = ["listicle", "narrative", "countdown"]
    idx = select_least_recent(options, history, "video_type")
    assert options[idx] == "listicle"  # used longest ago


def test_topic_prompt_includes_preferred_type():
    from prompts import topic_selection_prompt
    prompt = topic_selection_prompt(
        channel_name="Test",
        niche_focus="test",
        audience="test",
        example_topics=[],
        avoid_topics=[],
        video_types={"listicle": {"enabled": True, "pacing": "p", "example": "e", "section_style": "s"}},
        past_topics=[],
        language="es",
        preferred_type="listicle",
    )
    assert "PREFERRED" in prompt
    assert "listicle" in prompt


def test_topic_prompt_includes_content_families():
    from prompts import topic_selection_prompt
    prompt = topic_selection_prompt(
        channel_name="Test",
        niche_focus="test",
        audience="test",
        example_topics=[],
        avoid_topics=[],
        video_types={"listicle": {"enabled": True, "pacing": "p", "example": "e", "section_style": "s"}},
        past_topics=[],
        language="en",
        content_families=[
            {
                "name": "concept_explainer",
                "planning_focus": "Warning signs plus next steps",
                "lead_magnet": "Doctor Visit Checklist",
                "low_ticket_offer": "Symptom Tracker",
            }
        ],
        preferred_content_family="concept_explainer",
    )
    assert "ALLOWED CONTENT FAMILIES" in prompt
    assert "Doctor Visit Checklist" in prompt
    assert '"content_family": "chosen content family name"' in prompt


def test_script_prompt_includes_narrative_hook():
    from prompts import script_generation_prompt
    prompt = script_generation_prompt(
        topic="test", video_type="listicle", angle="hook",
        channel_name="Test", audience="test", language="es",
        target_duration_minutes=10, sections_range=[5, 10],
        section_style="s", pacing="p", style_prompt_suffix="",
        title_format_instruction="t", description_style_instruction="d",
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        narrative_hook="Open with the most surprising item",
    )
    assert "NARRATIVE APPROACH" in prompt
    assert "Open with the most surprising item" in prompt


def test_script_prompt_omits_narrative_hook_when_empty():
    from prompts import script_generation_prompt
    prompt = script_generation_prompt(
        topic="test", video_type="listicle", angle="hook",
        channel_name="Test", audience="test", language="es",
        target_duration_minutes=10, sections_range=[5, 10],
        section_style="s", pacing="p", style_prompt_suffix="",
        title_format_instruction="t", description_style_instruction="d",
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        narrative_hook="",
    )
    assert "NARRATIVE APPROACH" not in prompt


def test_script_prompt_includes_business_strategy_context():
    from prompts import script_generation_prompt
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        channel_goal="Turn each video into a funnel.",
        content_family="practical_checklist",
        lead_magnet="Mobility Starter Guide",
        low_ticket_offer="7-Day Mobility Reset",
        mid_ticket_offer="30-Day Mobility System",
        later_offers=["Newsletter"],
        video_jobs=["Get the click.", "Move to the lead magnet."],
        cta_rules=["Primary CTA is always the lead magnet."],
        cta_angle="Invite viewers to download the printable guide.",
    )
    assert "BUSINESS STRATEGY" in prompt
    assert "Mobility Starter Guide" in prompt
    assert '"lead_magnet": "Mobility Starter Guide"' in prompt


def test_script_prompt_uses_subscribe_cta_when_no_lead_magnet():
    from prompts import script_generation_prompt
    prompt = script_generation_prompt(
        topic="test",
        video_type="listicle",
        angle="hook",
        channel_name="Test",
        audience="test",
        language="en",
        target_duration_minutes=10,
        sections_range=[5, 10],
        section_style="s",
        pacing="p",
        style_prompt_suffix="",
        title_format_instruction="t",
        description_style_instruction="d",
        thumbnail_strategies=[{"name": "hero", "instruction": "Make a hero thumbnail"}],
        channel_goal="Build an audience first.",
        content_family="practical_checklist",
        cta_rules=["Primary CTA is subscribe."],
        cta_angle="Invite viewers to subscribe for more simple routines.",
    )
    assert "- Primary CTA: subscribe" in prompt
    assert "Do not invent guides, checklists, downloads, lead magnets, or products" in prompt
    assert '"content_family": "practical_checklist"' in prompt
    assert '"lead_magnet":' not in prompt


def test_thumbnail_prompts_reject_badge_and_packaging_text():
    from prompts import thumbnail_generation_prompt, thumbnail_review_prompt

    generation = thumbnail_generation_prompt(
        title="test",
        thumbnail_text="TEXT",
        thumbnail_brief="brief",
        strategy_instruction="strategy",
        content_context="content",
        channel_style="style",
        image_style_prompt_suffix="suffix",
    )
    review = thumbnail_review_prompt(
        title="test",
        video_type="listicle",
        thumbnail_text="TEXT",
        thumbnail_strategy="demo_thumbnail",
        thumbnail_brief="brief",
        strategy_instruction="strategy",
        content_context="content",
    )

    assert "badge text" in generation
    assert "packaging text" in generation
    assert "name-tag text" in review
    assert "clipboard/form text" in review


def test_compute_sections_range_from_duration():
    from core.utils import compute_sections_range, VideoTypeConfig
    vt = VideoTypeConfig(enabled=True)
    # 1-minute video: keep a small multi-section range
    assert compute_sections_range(1, vt) == [2, 3]
    # 7-minute video: mid-length videos stay compact instead of ballooning
    assert compute_sections_range(7, vt) == [5, 8]
    # 3-minute video: still small and practical
    assert compute_sections_range(3, vt) == [2, 3]
    # 20-minute video: cap long-form auto ranges so planning/script stay tractable
    assert compute_sections_range(20, vt) == [8, 12]


def test_compute_sections_range_respects_config_override():
    from core.utils import compute_sections_range, VideoTypeConfig
    vt = VideoTypeConfig(enabled=True, sections_range=[4, 6])
    # Config override wins over auto-calculation
    assert compute_sections_range(1, vt) == [4, 6]
    assert compute_sections_range(10, vt) == [4, 6]


def test_topic_prompt_includes_sections_range():
    from prompts import topic_selection_prompt
    prompt = topic_selection_prompt(
        channel_name="Test",
        niche_focus="test",
        audience="test",
        example_topics=[],
        avoid_topics=[],
        video_types={"listicle": {"enabled": True, "pacing": "p", "example": "e", "section_style": "s"}},
        past_topics=[],
        language="en",
        preferred_type="listicle",
        target_duration_minutes=1,
        sections_range=[2, 3],
    )
    assert "2-3 content sections" in prompt
    assert "MUST match the section count" in prompt
    assert '"estimated_sections": 3' in prompt
