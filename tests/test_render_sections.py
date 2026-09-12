"""Tests for render_sections slot building, backdrop figure scenes, and timing."""

import asyncio
import json

import pytest

import core.render_sections as render_sections
from core.render_sections import (
    PRESETS,
    DIRECTIONS,
    _build_remotion_gpu_cmd,
    _build_remotion_render_cmd,
    _build_section_encode_cmd,
    _check_windows_gpu_preflight,
    _expected_section_clip_duration,
    _parse_remotion_gpu_output,
    _section_clip_matches_duration,
    _build_slot,
    _select_transition,
    _validate_max_visual_hold,
)
from core.utils import Script, ScriptSection, VisualSlot, ChannelConfig, RenderingDefaults, compute_sub_durations
from settings import PROJECT_ROOT

INTRA_CROSSFADE = RenderingDefaults().intra_slot_crossfade  # 0.3


# ── Test helpers ─────────────────────────────────────────────────


def _section(
    slots: list | None = None,
    actual_duration: float | None = 15.0,
) -> ScriptSection:
    default_slots = [VisualSlot(visual="google_photo", prompt="prompt", keywords="test")]
    return ScriptSection(
        id=1,
        narration="Test narration.",
        slots=[VisualSlot(**s) if isinstance(s, dict) else s for s in (slots or default_slots)],
        actual_duration_seconds=actual_duration,
    )


def _minimal_config(**overrides) -> ChannelConfig:
    defaults = {
        "channel_name": "test",
        "niche": {"category": "test", "focus": "test", "audience": "general", "content_style": "informative"},
        "youtube": {"channel_id": "x", "tags": ["t"], "title_formats": [{"name": "a", "instruction": "a"}], "description_styles": [{"name": "a", "instruction": "a"}]},
        "thumbnail_strategies": [{"name": "hero", "instruction": "Make a hero thumbnail"}],
    }
    defaults.update(overrides)
    return ChannelConfig(**defaults)


def _script(*sections: ScriptSection) -> Script:
    return Script(
        title="Test Title",
        video_type="explainer",
        sections=list(sections),
    )


# ── Backdrop figure scenes ───────────────────────────────────────


@pytest.mark.parametrize(
    ("visual", "figure_component", "props"),
    [
        ("title_card", "TitleCard", {"title": "El Boom Económico"}),
        ("fact_highlight", "FactHighlight", {"value": "28", "label": "marcas"}),
        ("title_banner", "TitleBanner", {"title": "Turmeric", "section_number": 2}),
        ("subscribe_cta", "SubscribeCTA", {"cta_text": "Join"}),
    ],
)
def test_backdrop_figure_slot_renders_backdrop_scene(tmp_path, visual, figure_component, props):
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (ready_dir / "section_001_01.png").write_bytes(b"png-bytes")

    built = _build_slot(
        VisualSlot(
            visual=visual,
            prompt="Realistic photo of an older adult at home.",
            keywords="older adult home real photo",
            props=props,
        ),
        sub_idx=0,
        duration_frames=180,
        fps=30,
        videos_dir=tmp_path / "videos",
        ready_dir=ready_dir,
        data_dir=tmp_path / "data",
        staging_dir=staging_dir,
        config=_minimal_config(),
        section_id=1,
    )

    assert built["type"] == "component"
    assert built["component"] == "BackdropFigureScene"
    assert built["props"]["background_path"] == "_sections/section_001_01.png"
    assert built["props"]["figure_component"] == figure_component
    assert built["props"]["figure_props"] == props
    assert built["durationFrames"] == 180


def test_backdrop_figure_slot_skipped_without_background_image(tmp_path):
    built = _build_slot(
        VisualSlot(
            visual="title_card",
            prompt="Realistic photo of a kitchen table.",
            keywords="kitchen table real photo",
            props={"title": "Title"},
        ),
        sub_idx=0,
        duration_frames=180,
        fps=30,
        videos_dir=tmp_path / "videos",
        ready_dir=tmp_path / "ready",
        data_dir=tmp_path / "data",
        staging_dir=tmp_path / "staging",
        config=_minimal_config(),
        section_id=1,
    )
    assert built is None


# ── Transition selection ─────────────────────────────────────────


def test_transition_deterministic():
    config = _minimal_config(style={"video": {"transition_pool": ["fade", "wipeleft"]}})
    t1 = _select_transition(config, "Same Title", INTRA_CROSSFADE)
    t2 = _select_transition(config, "Same Title", INTRA_CROSSFADE)
    assert t1 == t2


def test_transition_cut_has_zero_duration():
    config = _minimal_config(style={"video": {"transition_pool": ["cut"]}})
    t = _select_transition(config, "Any Title", INTRA_CROSSFADE)
    assert t["type"] == "cut"
    assert t["durationFrames"] == 0


def test_transition_crossfade_mapped_to_fade():
    config = _minimal_config(style={"video": {"transition_pool": ["crossfade"]}})
    t = _select_transition(config, "Any Title", INTRA_CROSSFADE)
    assert t["type"] == "fade"


# A spread of real football subjects: transfers, results, explainers, and
# stories, in both languages the channel produces. Section titles are what the
# transition hash keys on, so this is the axis along which behaviour can vary
# from topic to topic.
FOOTBALL_SECTION_TITLES = [
    "صفقة الانتقال الأغلى في تاريخ النادي",
    "ما الذي حسم مباراة دوري الأبطال",
    "كيف يعمل اللعب المالي النظيف",
    "قصة عودة لن ينساها الجمهور",
    "الحكم المساعد بالفيديو وقرار مثير للجدل",
    "Record Transfer Fee Explained",
    "What Decided The Champions League Tie",
    "Why This Manager Was Sacked",
    "The Comeback That Shocked Football",
    "Financial Fair Play In Plain Arabic",
]


def test_football_transitions_are_renderable_across_topics():
    """Every topic must resolve to a transition both render paths implement.

    `dissolve` was in the football pool while the Remotion renderer knew only
    fade/wipeleft/cut, so roughly half of all section boundaries silently
    degraded to a plain fade. Nothing errored, which is why it went unnoticed
    -- hence checking the resolved name rather than the configured one.
    """
    from core import assembler
    from core.utils import load_channel_config
    from tests.test_config import _remotion_supported_transitions

    config = load_channel_config("football_news")
    supported = _remotion_supported_transitions() | set(assembler._TRANSITION_MAP)

    for title in FOOTBALL_SECTION_TITLES:
        t = _select_transition(config, title, INTRA_CROSSFADE)
        assert t["type"] in supported, (
            f"{title!r} resolved to transition {t['type']!r}, which no render "
            f"path implements"
        )


def test_football_transition_pool_is_actually_exercised():
    """The configured pool should vary across topics, not collapse to one name.

    Guards the other half of the bug: a pool can be fully renderable and still
    be pointless if the selector only ever returns its first entry.
    """
    from core.utils import load_channel_config

    config = load_channel_config("football_news")
    pool = config.style.video.get("transition_pool", [])
    if len(pool) < 2:
        pytest.skip("pool has a single entry; nothing to vary")

    seen = {
        _select_transition(config, title, INTRA_CROSSFADE)["type"]
        for title in FOOTBALL_SECTION_TITLES
    }
    assert len(seen) >= 2, (
        f"across {len(FOOTBALL_SECTION_TITLES)} topics only {seen} was ever "
        f"selected from pool {pool}"
    )


# ── Sub-slot duration computation ────────────────────────────────


def test_sub_durations_sum_covers_section():
    section = _section(
        slots=[
            VisualSlot(visual="google_photo", prompt="a", keywords="a"),
            VisualSlot(visual="stock_photo", prompt="b", keywords="b"),
            VisualSlot(visual="ai_photo", prompt="c"),
        ],
        actual_duration=15.0,
    )
    durations = compute_sub_durations(section, 3, INTRA_CROSSFADE)
    assert len(durations) == 3
    assert all(d > 0 for d in durations)


def test_sub_durations_fall_back_to_balanced_split_when_gap_split_breaks_hold_cap():
    section = _section(
        slots=[
            VisualSlot(visual="b_roll", keywords="walking outdoors"),
            VisualSlot(visual="google_photo", prompt="a", keywords="a"),
            VisualSlot(visual="stock_photo", prompt="b", keywords="b"),
        ],
        actual_duration=31.21,
    )
    section.word_timestamps = [
        {"word": "Do", "start": 0.0, "end": 6.0},
        {"word": "your", "start": 6.05, "end": 12.0},
        {"word": "legs", "start": 12.05, "end": 20.6},
        {"word": "feel", "start": 21.0, "end": 27.2},
        {"word": "heavy", "start": 27.6, "end": 31.0},
    ]

    durations = compute_sub_durations(
        section,
        3,
        INTRA_CROSSFADE,
        max_visual_hold_seconds=16.0,
    )

    assert max(durations) <= 16.0
    assert durations == pytest.approx([10.603333333333333] * 3)


def test_reconcile_slot_frame_budget_does_not_extend_video_only_slots():
    slots = [
        {"type": "video", "durationFrames": 90},
        {"type": "video", "durationFrames": 120},
    ]

    render_sections._reconcile_slot_frame_budget(
        section=_section(
            slots=[VisualSlot(visual="b_roll"), VisualSlot(visual="b_roll")],
            actual_duration=8.0,
        ),
        slots=slots,
        frame_error=12,
    )

    assert slots == [
        {"type": "video", "durationFrames": 90},
        {"type": "video", "durationFrames": 120},
    ]


def test_single_slot_gets_full_duration():
    section = _section(actual_duration=10.0)
    durations = compute_sub_durations(section, 1, INTRA_CROSSFADE)
    assert len(durations) == 1
    assert durations[0] >= 10.0


def test_validate_max_visual_hold_allows_slots_within_cap():
    section = _section(slots=[
        VisualSlot(visual="google_photo", prompt="a", keywords="a"),
        VisualSlot(visual="stock_photo", prompt="b", keywords="b"),
    ])
    _validate_max_visual_hold(section, [8.0, 16.0], max_seconds=16.0)


def test_validate_max_visual_hold_rejects_overlong_slot():
    section = _section(slots=[
        VisualSlot(visual="google_photo", prompt="a", keywords="a"),
        VisualSlot(visual="stock_photo", prompt="b", keywords="b"),
    ], actual_duration=30.0)
    with pytest.raises(RuntimeError, match="max visual hold of 16.0s"):
        _validate_max_visual_hold(section, [17.2, 9.0], max_seconds=16.0)


def test_expected_section_clip_duration_includes_xfade_pad():
    section = _section(actual_duration=10.0)
    assert _expected_section_clip_duration(section, xfade_pad_frames=24, fps=30) == pytest.approx(10.8)


def test_section_clip_matches_duration_within_tolerance(monkeypatch, tmp_path):
    output_path = tmp_path / "section_001.mp4"
    monkeypatch.setattr(render_sections, "_probe_video_duration", lambda path: 10.05)
    assert _section_clip_matches_duration(output_path, expected_duration=10.0) is True


def test_section_clip_rejects_duration_mismatch(monkeypatch, tmp_path):
    output_path = tmp_path / "section_001.mp4"
    monkeypatch.setattr(render_sections, "_probe_video_duration", lambda path: 9.5)
    assert _section_clip_matches_duration(output_path, expected_duration=10.0) is False


def test_a_section_clip_is_reused_only_when_its_visuals_are_unchanged(tmp_path):
    """Duration alone was the cache key, and that shipped the wrong video.

    Replacing a beat's photograph, or turning it into a card, changes what the
    section looks like without changing how long it runs. On the real resume of
    run 99db2deb that meant image_source produced a whole new set of visuals
    and assemble stitched together the previous render anyway.
    """
    from core.render_sections import (
        _cached_fingerprint,
        _section_props_fingerprint,
        _write_fingerprint,
    )

    before = {"slots": [{"type": "image", "file": "a.png", "durationFrames": 75}]}
    after = {"slots": [{"type": "component", "component": "InfoCard",
                        "durationFrames": 75}]}

    assert _section_props_fingerprint(before) != _section_props_fingerprint(after)
    # Same payload, different key order, must be the same clip.
    assert _section_props_fingerprint({"a": 1, "b": 2}) == _section_props_fingerprint(
        {"b": 2, "a": 1}
    )

    clip = tmp_path / "section_001.mp4"
    clip.write_bytes(b"x")
    # A clip rendered before fingerprints existed is not trusted.
    assert _cached_fingerprint(clip) == ""
    _write_fingerprint(clip, _section_props_fingerprint(before))
    assert _cached_fingerprint(clip) == _section_props_fingerprint(before)
    assert _cached_fingerprint(clip) != _section_props_fingerprint(after)


def test_info_card_slot_renders_infocard_component(tmp_path):
    slot = VisualSlot(
        visual="info_card",
        prompt="seated knee extension with quadriceps highlighted",
        props={
            "text": "Straighten the knee slowly and pause for one breath.",
            "illustration_style": "framed",
            "layout": "image-right",
            "highlighted_keywords": ["knee", "pause"],
        },
    )
    built = _build_slot(
        slot,
        sub_idx=0,
        duration_frames=240,
        fps=30,
        videos_dir=tmp_path / "videos",
        ready_dir=tmp_path / "ready",
        data_dir=tmp_path / "data",
        staging_dir=tmp_path / "staging",
        config=_minimal_config(),
        section_id=1,
    )
    assert built["type"] == "component"
    assert built["component"] == "InfoCard"
    assert built["props"]["text"] == "Straighten the knee slowly and pause for one breath."
    assert built["props"]["illustration_style"] == "framed"
    assert "illustration_url" not in built["props"]


def test_info_slide_slot_renders_infoslide_component(tmp_path):
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (ready_dir / "section_001_01.png").write_bytes(b"png-bytes")

    slot = VisualSlot(
        visual="info_slide",
        prompt="seated knee extension with quadriceps highlighted",
        props={
            "title": "Quad Pump",
            "text": "Straighten the knee slowly and pause for one breath.",
            "layout": "image-right",
            "highlighted_keywords": ["knee", "pause"],
        },
    )
    built = _build_slot(
        slot,
        sub_idx=0,
        duration_frames=240,
        fps=30,
        videos_dir=tmp_path / "videos",
        ready_dir=ready_dir,
        data_dir=tmp_path / "data",
        staging_dir=staging_dir,
        config=_minimal_config(),
        section_id=1,
    )
    assert built["type"] == "component"
    assert built["component"] == "InfoSlide"
    assert built["props"]["title"] == "Quad Pump"
    assert built["props"]["text"] == "Straighten the knee slowly and pause for one breath."
    assert built["props"]["illustration_url"] == "_sections/section_001_01.png"


def test_ai_prompt_preview_text_only_slide_renders_text_only_component(tmp_path):
    slot = VisualSlot(
        visual="text_only_slide",
        props={
            "variant": "ai_prompt_preview",
            "title": "AI image prompt",
            "text": "Model: gemini-test\n\nPrompt:\nOlder adult seated in a chair.",
            "model": "gemini-test",
            "operation": "image_generate",
            "prompt_text": "Older adult seated in a chair.",
        },
    )
    built = _build_slot(
        slot,
        sub_idx=0,
        duration_frames=240,
        fps=30,
        videos_dir=tmp_path / "videos",
        ready_dir=tmp_path / "ready",
        data_dir=tmp_path / "data",
        staging_dir=tmp_path / "staging",
        config=_minimal_config(),
        section_id=1,
    )
    assert built["type"] == "component"
    assert built["component"] == "TextOnlySlide"
    assert built["props"]["title"] == "AI image prompt"
    assert "illustration_url" not in built["props"]


def test_authored_text_only_slide_fails_loudly(tmp_path):
    slot = VisualSlot(
        visual="text_only_slide",
        props={"title": "Heel Slides", "text": "- Keep the heel on the bed"},
    )

    with pytest.raises(RuntimeError, match="reserved for internal AI prompt preview"):
        _build_slot(
            slot,
            sub_idx=0,
            duration_frames=240,
            fps=30,
            videos_dir=tmp_path / "videos",
            ready_dir=tmp_path / "ready",
            data_dir=tmp_path / "data",
            staging_dir=tmp_path / "staging",
            config=_minimal_config(),
            section_id=1,
        )


def test_info_slide_slot_uses_sourced_illustration_when_present(tmp_path):
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (ready_dir / "section_001_01.png").write_bytes(b"png-bytes")

    slot = VisualSlot(
        visual="info_slide",
        prompt="guided stretch illustration",
        props={"title": "Stretch", "text": "Move slowly."},
    )
    built = _build_slot(
        slot,
        sub_idx=0,
        duration_frames=240,
        fps=30,
        videos_dir=tmp_path / "videos",
        ready_dir=ready_dir,
        data_dir=tmp_path / "data",
        staging_dir=staging_dir,
        config=_minimal_config(),
        section_id=1,
    )

    assert built["props"]["illustration_url"] == "_sections/section_001_01.png"


def test_info_slide_without_sourced_media_fails_loudly(tmp_path):
    slot = VisualSlot(
        visual="info_slide",
        prompt="guided stretch illustration",
        props={"title": "Stretch", "text": "Move slowly."},
    )

    with pytest.raises(RuntimeError, match="missing sourced media"):
        _build_slot(
            slot,
            sub_idx=0,
            duration_frames=240,
            fps=30,
            videos_dir=tmp_path / "videos",
            ready_dir=tmp_path / "ready",
            data_dir=tmp_path / "data",
            staging_dir=tmp_path / "staging",
            config=_minimal_config(),
            section_id=1,
        )

# ── Remotion composition discovery ──────────────────────────────


def test_new_components_in_remotion_compositions():
    """All component compositions used by slot rendering must exist in Root.tsx."""
    from settings import get_remotion_compositions

    get_remotion_compositions.cache_clear()
    compositions = get_remotion_compositions()
    for name in ("AnimatedBarChart", "DonutGauge", "ComparisonBars", "InfoCard", "InfoSlide", "TextOnlySlide"):
        assert name in compositions, f"{name} not found in Root.tsx compositions"


def test_check_remotion_ready_requires_babel_parser(monkeypatch, tmp_path):
    remotion_dir = tmp_path / "remotion"
    (remotion_dir / "node_modules" / "@remotion" / "cli").mkdir(parents=True)
    (remotion_dir / "node_modules" / "@remotion" / "cli" / "remotion-cli.js").write_text(
        "cli",
        encoding="utf-8",
    )

    monkeypatch.setattr(render_sections, "REMOTION_DIR", remotion_dir)
    monkeypatch.setattr(
        render_sections.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0})(),
    )

    with pytest.raises(RuntimeError, match="@babel/parser"):
        render_sections._check_remotion_ready()


def test_check_remotion_ready_accepts_required_remotion_runtime_deps(monkeypatch, tmp_path):
    remotion_dir = tmp_path / "remotion"
    (remotion_dir / "node_modules" / "@remotion" / "cli").mkdir(parents=True)
    (remotion_dir / "node_modules" / "@remotion" / "cli" / "remotion-cli.js").write_text(
        "cli",
        encoding="utf-8",
    )
    (remotion_dir / "node_modules" / "@babel" / "parser").mkdir(parents=True)

    monkeypatch.setattr(render_sections, "REMOTION_DIR", remotion_dir)
    monkeypatch.setattr(
        render_sections.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0})(),
    )

    render_sections._check_remotion_ready()


def test_build_remotion_render_cmd_for_sequence_output(tmp_path):
    props_path = tmp_path / "props.json"
    output_dir = tmp_path / "frames"
    original_platform = render_sections.sys.platform
    render_sections.sys.platform = "linux"
    cmd = _build_remotion_render_cmd(
        component="SectionComposition",
        props_path=props_path,
        output_target=output_dir,
        width=1920,
        height=1080,
        fps=30,
        duration_frames=150,
        image_sequence=True,
        sequence_image_format="jpeg",
        sequence_jpeg_quality=80,
    )
    try:
        assert cmd[0] == "node"
        assert cmd[1].endswith(r"node_modules\@remotion\cli\remotion-cli.js") or cmd[1].endswith(
            "node_modules/@remotion/cli/remotion-cli.js"
        )
        assert cmd[2] == "render"
        assert str(output_dir.resolve()) in cmd
        assert "--sequence" in cmd
        assert "--image-format" in cmd
        assert "jpeg" in cmd
        assert "--jpeg-quality" in cmd
        jpeg_quality_index = cmd.index("--jpeg-quality")
        assert cmd[jpeg_quality_index + 1] == "80"
        assert "--image-sequence-pattern" in cmd
        assert "frame-[frame].[ext]" in cmd
        assert "--codec" not in cmd
        assert "--hardware-acceleration" not in cmd
        gl_index = cmd.index("--gl")
        assert cmd[gl_index + 1] == "vulkan"
        assert "--chrome-mode" not in cmd
    finally:
        render_sections.sys.platform = original_platform


def test_build_remotion_render_cmd_uses_cft_and_angle_on_windows(tmp_path, monkeypatch):
    props_path = tmp_path / "props.json"
    output_dir = tmp_path / "frames"
    monkeypatch.setattr(render_sections.sys, "platform", "win32")

    cmd = _build_remotion_render_cmd(
        component="SectionComposition",
        props_path=props_path,
        output_target=output_dir,
        width=1920,
        height=1080,
        fps=30,
        duration_frames=150,
        image_sequence=True,
        sequence_image_format="jpeg",
        sequence_jpeg_quality=80,
    )

    chrome_mode_index = cmd.index("--chrome-mode")
    gl_index = cmd.index("--gl")
    assert cmd[chrome_mode_index + 1] == "chrome-for-testing"
    assert cmd[gl_index + 1] == "angle"
    assert "--jpeg-quality" in cmd


def test_build_remotion_render_cmd_omits_jpeg_quality_for_png_sequences(tmp_path, monkeypatch):
    props_path = tmp_path / "props.json"
    output_dir = tmp_path / "frames"
    monkeypatch.setattr(render_sections.sys, "platform", "win32")

    cmd = _build_remotion_render_cmd(
        component="SectionComposition",
        props_path=props_path,
        output_target=output_dir,
        width=1920,
        height=1080,
        fps=30,
        duration_frames=150,
        image_sequence=True,
        sequence_image_format="png",
        sequence_jpeg_quality=80,
    )

    image_format_index = cmd.index("--image-format")
    assert cmd[image_format_index + 1] == "png"
    assert "--jpeg-quality" not in cmd


def test_build_remotion_gpu_cmd_uses_cft_and_angle():
    cmd = _build_remotion_gpu_cmd(
        chrome_mode="chrome-for-testing",
        gl_backend="angle",
    )

    assert cmd[0] == "node"
    assert cmd[2] == "gpu"
    assert cmd[-4:] == ["--chrome-mode", "chrome-for-testing", "--gl", "angle"]


def test_parse_remotion_gpu_output_strips_ansi_codes():
    output = "\x1b[32mCanvas: Hardware accelerated\x1b[39m\n\x1b[31mOpenGL: Disabled\x1b[39m"
    statuses = _parse_remotion_gpu_output(output)
    assert statuses == {
        "Canvas": "Hardware accelerated",
        "OpenGL": "Disabled",
    }


def test_check_windows_gpu_preflight_accepts_hardware_accelerated_output(monkeypatch):
    output = "\n".join([
        "Canvas: Hardware accelerated",
        "Compositing: Hardware accelerated",
        "Rasterization: Hardware accelerated",
        "WebGL: Hardware accelerated",
        "OpenGL: Enabled",
    ])

    def fake_run(cmd, capture_output, text, cwd, **kwargs):
        assert cmd[-4:] == ["--chrome-mode", "chrome-for-testing", "--gl", "angle"]

        class _Result:
            returncode = 0
            stdout = output
            stderr = ""

        return _Result()

    monkeypatch.setattr(render_sections.subprocess, "run", fake_run)

    statuses = _check_windows_gpu_preflight()

    assert statuses["Canvas"] == "Hardware accelerated"
    assert statuses["OpenGL"] == "Enabled"


def test_check_windows_gpu_preflight_rejects_software_output(monkeypatch):
    output = "\n".join([
        "Canvas: Software only. Hardware acceleration disabled",
        "Compositing: Software only. Hardware acceleration disabled",
        "Rasterization: Software only. Hardware acceleration disabled",
        "WebGL: Disabled",
        "OpenGL: Disabled",
    ])

    def fake_run(cmd, capture_output, text, cwd, **kwargs):
        class _Result:
            returncode = 0
            stdout = output
            stderr = ""

        return _Result()

    monkeypatch.setattr(render_sections.subprocess, "run", fake_run)
    monkeypatch.setattr(render_sections.settings, "remotion_require_gpu", True)

    with pytest.raises(RuntimeError, match="software rendering"):
        _check_windows_gpu_preflight()


def test_check_windows_gpu_preflight_rejects_malformed_output(monkeypatch):
    def fake_run(cmd, capture_output, text, cwd, **kwargs):
        class _Result:
            returncode = 0
            stdout = "unexpected output"
            stderr = ""

        return _Result()

    monkeypatch.setattr(render_sections.subprocess, "run", fake_run)
    monkeypatch.setattr(render_sections.settings, "remotion_require_gpu", True)

    with pytest.raises(RuntimeError, match="no parseable status"):
        _check_windows_gpu_preflight()


def test_check_windows_gpu_preflight_warns_when_gpu_not_required(monkeypatch, caplog):
    def fake_run(cmd, capture_output, text, cwd, **kwargs):
        class _Result:
            returncode = 0
            stdout = "Canvas: Software only. Hardware acceleration disabled"
            stderr = ""

        return _Result()

    monkeypatch.setattr(render_sections.subprocess, "run", fake_run)
    monkeypatch.setattr(render_sections.settings, "remotion_require_gpu", False)

    with caplog.at_level("WARNING"):
        assert _check_windows_gpu_preflight() == {}

    assert "software rendering" in caplog.text


def test_build_section_encode_cmd_uses_nvenc(tmp_path):
    frames_dir = tmp_path / "frames"
    output_path = tmp_path / "section_001.mp4"
    cmd = _build_section_encode_cmd(
        frames_dir,
        output_path,
        fps=30,
        duration_frames=150,
        sequence_image_format="jpeg",
    )
    assert cmd[0] == "ffmpeg"
    assert cmd[1:7] == ["-y", "-framerate", "30", "-start_number", "0", "-i"]
    assert cmd[6] == "-i"
    assert cmd[7] == str(frames_dir / "frame-%03d.jpeg")
    assert "-frames:v" in cmd
    assert "150" in cmd
    assert "h264_nvenc" in cmd
    assert cmd[-1] == str(output_path)


def test_render_scene_uses_sequence_and_nvenc_on_windows(monkeypatch, tmp_path):
    props_path = tmp_path / "props.json"
    props_path.write_text("{}", encoding="utf-8")
    output_path = tmp_path / "section_001.mp4"
    config = _minimal_config()
    calls: list[tuple] = []
    removed: list[tuple] = []

    async def fake_run_remotion_render(cmd, cwd, **kwargs):
        # **kwargs absorbs the timeout budget and retry label the real
        # function now takes.
        calls.append(("render", cmd, cwd))

    def fake_encode_section_frames(frames_dir, out_path, fps, duration_frames, sequence_image_format):
        calls.append(("encode", frames_dir, out_path, fps, duration_frames, sequence_image_format))

    def fake_rmtree(path, ignore_errors=False):
        removed.append((path, ignore_errors))

    monkeypatch.setattr(render_sections.sys, "platform", "win32")
    monkeypatch.setattr(render_sections, "_run_remotion_render", fake_run_remotion_render)
    monkeypatch.setattr(render_sections, "_encode_section_frames", fake_encode_section_frames)
    monkeypatch.setattr(render_sections.shutil, "rmtree", fake_rmtree)

    asyncio.run(
        render_sections._render_remotion_scene(
            "SectionComposition",
            props_path,
            output_path,
            width=1920,
            height=1080,
            fps=30,
            rendering_defaults=config.rendering_defaults,
            duration_frames=150,
        )
    )

    frames_dir = tmp_path / "section_001_frames"
    assert calls[0][0] == "render"
    assert "--sequence" in calls[0][1]
    assert str(frames_dir.resolve()) in calls[0][1]
    assert "jpeg" in calls[0][1]
    assert calls[1] == ("encode", frames_dir, output_path, 30, 150, "jpeg")
    assert removed == [(frames_dir, False)]


def test_render_sections_runs_windows_gpu_preflight_before_render(monkeypatch, tmp_path):
    script = _script(_section())
    config = _minimal_config()
    calls: list[str] = []

    monkeypatch.setattr(render_sections.sys, "platform", "win32")
    monkeypatch.setattr(render_sections, "REMOTION_PUBLIC", tmp_path / "public")
    monkeypatch.setattr(render_sections, "_check_remotion_ready", lambda: calls.append("ready"))
    monkeypatch.setattr(render_sections, "_check_windows_gpu_preflight", lambda: calls.append("gpu"))
    monkeypatch.setattr(
        render_sections,
        "build_section_composition_plan",
        lambda *args, **kwargs: [
            {
                "section_id": 1,
                "duration_frames": 120,
                "transition_to_next": None,
                "props": {"slots": [{"type": "image", "preset": "parallax", "durationFrames": 120}]},
                "props_path": tmp_path / "section_props_001.json",
            }
        ],
    )

    async def fake_render_scene(*args, **kwargs):
        calls.append("render")

    monkeypatch.setattr(render_sections, "_render_remotion_scene", fake_render_scene)
    monkeypatch.setattr(render_sections.shutil, "rmtree", lambda *args, **kwargs: None)

    result = asyncio.run(render_sections.render_sections(script, config, tmp_path))

    assert calls == ["ready", "gpu", "render"]
    assert result["rendered"] == 1


def test_render_sections_writes_slot_only_props(monkeypatch, tmp_path):
    script = _script(
        ScriptSection(
            id=1,
            narration="Section one",
            actual_duration_seconds=1.0,
            slots=[VisualSlot(visual="google_photo", prompt="a", keywords="a")],
        ),
    )
    config = _minimal_config(rendering_defaults={"render_concurrency": 1})
    captured_props = {}

    monkeypatch.setattr(render_sections, "_check_remotion_ready", lambda: None)
    monkeypatch.setattr(render_sections, "_check_windows_gpu_preflight", lambda: None)
    monkeypatch.setattr(
        render_sections,
        "_build_slot",
        lambda *args, **kwargs: {"type": "image", "durationFrames": 30},
    )

    async def fake_render(component, props_path, output_path, *args, **kwargs):
        captured_props.update(json.loads(props_path.read_text(encoding="utf-8")))

    monkeypatch.setattr(render_sections, "_render_remotion_scene", fake_render)

    result = asyncio.run(render_sections.render_sections(script, config, tmp_path))

    assert result["rendered"] == 1
    assert "overlay" not in captured_props
    assert captured_props["slots"] == [{"type": "image", "durationFrames": 30}]


def test_render_sections_raises_on_partial_failure(monkeypatch, tmp_path):
    script = _script(
        ScriptSection(
            id=1,
            narration="Section one",
            actual_duration_seconds=1.0,
            slots=[VisualSlot(visual="google_photo", prompt="a", keywords="a")],
        ),
        ScriptSection(
            id=2,
            narration="Section two",
            actual_duration_seconds=1.0,
            slots=[VisualSlot(visual="google_photo", prompt="b", keywords="b")],
        ),
    )
    config = _minimal_config(rendering_defaults={"render_concurrency": 1})

    monkeypatch.setattr(render_sections, "_check_remotion_ready", lambda: None)
    monkeypatch.setattr(render_sections, "_check_windows_gpu_preflight", lambda: None)
    monkeypatch.setattr(
        render_sections,
        "_build_slot",
        lambda *args, **kwargs: {"type": "image", "durationFrames": 30},
    )

    async def fake_render(component, props_path, output_path, *args, **kwargs):
        if output_path.name == "section_002.mp4":
            raise RuntimeError("boom")

    monkeypatch.setattr(render_sections, "_render_remotion_scene", fake_render)

    with pytest.raises(RuntimeError, match=r"Section rendering failed for 1 section\(s\): boom"):
        asyncio.run(render_sections.render_sections(script, config, tmp_path))


def test_render_sections_aborts_when_windows_gpu_preflight_fails(monkeypatch, tmp_path):
    script = render_sections.Script(
        title="Test",
        video_type="listicle",
        sections=[_section()],
    )
    config = _minimal_config()
    render_called = False

    monkeypatch.setattr(render_sections.sys, "platform", "win32")
    monkeypatch.setattr(render_sections, "REMOTION_PUBLIC", tmp_path / "public")
    monkeypatch.setattr(render_sections, "_check_remotion_ready", lambda: None)

    def fail_gpu_preflight():
        raise RuntimeError("gpu preflight failed")

    async def fake_render_scene(*args, **kwargs):
        nonlocal render_called
        render_called = True

    monkeypatch.setattr(render_sections, "_check_windows_gpu_preflight", fail_gpu_preflight)
    monkeypatch.setattr(render_sections, "_render_remotion_scene", fake_render_scene)

    with pytest.raises(RuntimeError, match="gpu preflight failed"):
        asyncio.run(render_sections.render_sections(script, config, tmp_path))

    assert render_called is False


def test_section_composition_preview_has_no_sample_overlay_default():
    root_path = PROJECT_ROOT / "rendering" / "remotion" / "src" / "Root.tsx"
    contents = root_path.read_text(encoding="utf-8")

    assert 'id="SectionComposition"' in contents
    assert "Sample Section" not in contents
