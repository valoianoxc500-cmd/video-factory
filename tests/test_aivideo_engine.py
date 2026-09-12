"""AI Video Maker: the lifecycle, and the ways it must refuse to die.

The product promise is narrow and testable: a topic goes in, a valid MP4 comes
out, and the things that routinely go wrong in the middle -- a provider being
down, one clip 404ing, a search returning nothing, a worker restarting -- cost
that one thing rather than the whole video.

Everything here runs on fixtures. No provider is called, no credit is spent,
and the suite finishes in about a second.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aivideo import footage, pipeline, script as script_stage, subtitles, voice
from aivideo.spec import (
    CAPTION_PRESETS,
    CaptionStyle,
    VideoSpec,
    normalise_spec,
    voices_for,
)


# ── the spec the whole product agrees on ─────────────────────────────

def test_a_spec_survives_junk_from_the_client():
    """Clamped, never rejected: a stored preference must not fail a job."""
    spec = normalise_spec({
        "topic": "  5 crazy facts about Dubai  ",
        "language": "klingon",
        "duration_seconds": 35,
        "aspect_ratio": "4:3",
        "voice": "not-a-voice",
        "voice_rate": 9.0,
        "clip_seconds": 0.1,
        "captions": {"preset": "nope", "size": 999, "position": "sideways"},
    })
    assert spec.topic == "5 crazy facts about Dubai"
    assert spec.language == "en"
    assert spec.duration_seconds == 30          # snapped to the nearest offered
    assert spec.aspect_ratio == "9:16"
    assert spec.voice in {v["id"] for v in voices_for("en")}
    assert spec.voice_rate == 2.0
    assert spec.clip_seconds == 1.5
    assert spec.captions.size == 140
    assert spec.captions.position == "bottom"


def test_the_offered_durations_and_ratios_are_what_the_ui_shows():
    from aivideo.spec import ASPECT_SIZES, DURATIONS

    assert DURATIONS == (30, 45, 60, 90)
    assert set(ASPECT_SIZES) == {"9:16", "16:9", "1:1"}
    assert ASPECT_SIZES["9:16"] == (1080, 1920)


def test_every_caption_preset_is_complete_and_renderable():
    for name in CAPTION_PRESETS:
        style = CaptionStyle.from_preset(name)
        assert style.size > 0
        assert style.text_color.startswith("#")
        assert style.position in {"top", "center", "bottom"}
        assert style.words_per_line >= 1


def test_advanced_overrides_beat_the_preset():
    spec = normalise_spec({
        "topic": "t",
        "captions": {"preset": "viral", "text_color": "#FF0000", "size": 50},
    })
    assert spec.captions.preset == "viral"
    assert spec.captions.text_color == "#FF0000"
    assert spec.captions.size == 50


# ── Arabic ───────────────────────────────────────────────────────────

def test_arabic_is_forced_onto_a_font_that_can_shape_it():
    """A Latin-only font renders Arabic as disconnected boxes, silently."""
    from aivideo.spec import ARABIC_CAPABLE_FONTS

    spec = normalise_spec({
        "topic": "حقائق عن دبي",
        "language": "ar",
        "captions": {"preset": "cinematic"},     # playfair: Latin only
    })
    assert spec.captions.font in ARABIC_CAPABLE_FONTS
    assert spec.captions.uppercase is False      # Arabic has no letter case
    assert spec.voice in {v["id"] for v in voices_for("ar")}


def test_arabic_text_is_reshaped_and_reordered():
    plain = "ريال مدريد"
    shaped = subtitles.shape_arabic(plain)
    assert shaped != plain, "Arabic was not shaped; letters will not join"
    assert len(shaped) >= 3


def test_latin_text_is_left_alone_by_the_shaper():
    assert subtitles.shape_arabic("Dubai skyline") == "Dubai skyline"


def test_an_arabic_caption_file_is_written_with_a_real_font(tmp_path):
    words = [voice.Word("مرحبا", 0.0, 0.5), voice.Word("بالعالم", 0.5, 1.2)]
    style = CaptionStyle.from_preset("bold", "ar")
    out = subtitles.build_ass(words, style, (1080, 1920), tmp_path / "c.ass",
                              language="ar")
    assert out is not None
    body = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in body
    assert "Dialogue:" in body
    name, path = subtitles.resolve_font(style.font)
    assert path is not None, "no Arabic-capable font file found on this machine"


# ── captions ─────────────────────────────────────────────────────────

def test_lines_break_on_sentence_ends_as_well_as_word_count():
    words = [
        voice.Word("One", 0, 1), voice.Word("two.", 1, 2),
        voice.Word("Three", 2, 3), voice.Word("four", 3, 4),
        voice.Word("five", 4, 5), voice.Word("six", 5, 6),
    ]
    lines = subtitles.group_lines(words, per_line=4)
    assert lines[0].text == "One two."           # broke early on the full stop
    assert lines[0].start == 0 and lines[0].end == 2


def test_colours_convert_to_ass_bgr_with_inverted_alpha():
    assert subtitles._ass_colour("#FFFFFF") == "&H00FFFFFF"
    assert subtitles._ass_colour("#FF0000") == "&H000000FF"   # BGR, not RGB
    assert subtitles._ass_colour("#00000080").startswith("&H7F")


def test_captions_can_be_turned_off(tmp_path):
    style = CaptionStyle(enabled=False)
    assert subtitles.build_ass(
        [voice.Word("hi", 0, 1)], style, (1080, 1920), tmp_path / "c.ass"
    ) is None


# ── footage: one failure is one clip ─────────────────────────────────

def test_a_search_that_finds_nothing_is_broadened_not_abandoned():
    assert footage.simplify("abandoned desert highway") == "desert highway"
    assert footage.simplify("desert highway") == "highway"
    assert footage.simplify("highway") == ""


@pytest.mark.asyncio
async def test_one_dead_beat_does_not_lose_the_others(tmp_path, monkeypatch):
    async def flaky(term, target, *, client, seen, portrait):
        if "broken" in term:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 60_000)
        return footage.Clip(target, "pexels", term, 1080, 1920, 6.0)

    monkeypatch.setattr(footage, "fetch_one", flaky)
    clips, missing = await footage.gather(
        ["dubai skyline", "broken thing", "desert road"], tmp_path
    )
    assert len(clips) == 2
    assert missing == ["broken thing"]


@pytest.mark.asyncio
async def test_a_beat_that_raises_is_contained(tmp_path, monkeypatch):
    async def explodes(term, target, *, client, seen, portrait):
        if "bad" in term:
            raise RuntimeError("provider exploded")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 60_000)
        return footage.Clip(target, "pixabay", term, 1080, 1920, 5.0)

    monkeypatch.setattr(footage, "fetch_one", explodes)
    clips, missing = await footage.gather(["good", "bad", "good two"], tmp_path)
    assert len(clips) == 2 and missing == ["bad"]


@pytest.mark.asyncio
async def test_no_footage_at_all_is_the_one_terminal_footage_case(tmp_path, monkeypatch):
    async def nothing(term, target, *, client, seen, portrait):
        return None

    monkeypatch.setattr(footage, "fetch_one", nothing)
    with pytest.raises(footage.NoFootage):
        await footage.gather(["a", "b"], tmp_path)


def test_provider_order_puts_the_free_libraries_first():
    assert [name for name, _ in footage.PROVIDERS] == ["pexels", "pixabay"]


# ── voice ────────────────────────────────────────────────────────────

def test_missing_word_boundaries_fall_back_to_weighted_timings():
    words = voice._even_timings("a extraordinary word", 3.0)
    assert len(words) == 3
    assert words[0].start == 0.0
    assert words[-1].end == pytest.approx(3.0, rel=1e-6)
    # Longer words hold the screen longer.
    assert (words[1].end - words[1].start) > (words[0].end - words[0].start)


def test_edge_rate_and_volume_use_signed_percentages():
    assert voice._rate_arg(1.0) == "+0%"
    assert voice._rate_arg(1.25) == "+25%"
    assert voice._rate_arg(0.8) == "-20%"


# ── script ───────────────────────────────────────────────────────────

def test_the_word_budget_tracks_the_requested_duration():
    assert script_stage.word_budget(30, "en") == 75
    assert script_stage.word_budget(60, "en") == 150
    # Arabic is read more slowly, so the same seconds buy fewer words.
    assert script_stage.word_budget(60, "ar") < script_stage.word_budget(60, "en")


def test_the_prompt_demands_english_search_terms_for_an_arabic_script():
    prompt = script_stage.build_prompt("حقائق عن دبي", 30, "ar")
    assert "Every word of \"script\" must be Arabic" in prompt
    assert "in ENGLISH even when the script is Arabic" in prompt


def test_script_cleaning_removes_the_scaffolding_models_add():
    cleaned = script_stage._clean_script(
        "```\n## Intro\nNarration: **Dubai** is a city.\n[pause]\n- built fast\n```"
    )
    assert "##" not in cleaned and "**" not in cleaned
    assert "[pause]" not in cleaned and not cleaned.startswith("Narration")
    assert "Dubai is a city." in cleaned


def test_terms_always_come_back_even_from_an_empty_reply():
    terms = script_stage._clean_terms([], "Why the Titanic sank")
    assert terms and all(t.strip() for t in terms)


# ── the lifecycle, end to end on fixtures ────────────────────────────

@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    """Stand in for every external call and for FFmpeg."""
    calls = {"script": 0, "footage": 0, "voice": 0, "render": 0}

    async def fake_script(topic, *, duration_seconds, language, ledger=None):
        calls["script"] += 1
        if ledger is not None:
            ledger.record_model_call(
                model="gemini-3-flash-preview",
                input_tokens=400, output_tokens=120, label="script",
            )
        return "Dubai was built in a hurry.", ["dubai skyline", "desert road"]

    async def fake_gather(terms, directory, *, portrait=True, concurrency=4):
        calls["footage"] += 1
        directory.mkdir(parents=True, exist_ok=True)
        clips = []
        for i, term in enumerate(terms):
            p = directory / f"clip_{i:02d}.mp4"
            p.write_bytes(b"x" * 60_000)
            clips.append(footage.Clip(p, "pexels", term, 1080, 1920, 6.0))
        return clips, []

    async def fake_voice(text, voice_id, output, *, rate=1.0, volume=1.0):
        calls["voice"] += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"x" * 40_000)
        words = [voice.Word(w, i * 0.5, i * 0.5 + 0.5)
                 for i, w in enumerate(text.split())]
        return voice.Narration(output, words, 12.0, voice_id)

    def fake_normalise(source, target, spec, seconds):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 20_000)
        return target

    def fake_track(clips, target, total, spec):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 20_000)
        return target

    def fake_render(*, visual, narration, output, spec, captions, music, duration):
        calls["render"] += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"x" * 200_000)
        return output

    monkeypatch.setattr(pipeline.script_stage, "write_script", fake_script)
    monkeypatch.setattr(pipeline.footage, "gather", fake_gather)
    monkeypatch.setattr(pipeline.voice, "synthesize", fake_voice)
    monkeypatch.setattr(pipeline.compose, "normalise_clip", fake_normalise)
    monkeypatch.setattr(pipeline.compose, "build_visual_track", fake_track)
    monkeypatch.setattr(pipeline.compose, "render", fake_render)
    monkeypatch.setattr(pipeline.compose, "validate_output",
                        lambda p, **k: (True, "12.0s 1080x1920"))
    monkeypatch.setattr(pipeline.compose, "poster_frame", lambda v, t, at=1.0: None)
    return calls


SPEC = {"topic": "5 crazy facts about Dubai", "language": "en",
        "duration_seconds": 30, "aspect_ratio": "9:16"}


@pytest.mark.asyncio
async def test_a_full_run_produces_a_validated_video(tmp_path, fake_engine):
    seen: list[tuple[str, int]] = []
    state = await pipeline.run(SPEC, tmp_path, on_progress=lambda l, p: seen.append((l, p)))

    assert state.completed == list(pipeline.STAGES)
    assert Path(state.output).exists()
    assert state.providers_used == ["pexels"]
    # The customer saw plain-language progress, in order, and nothing internal.
    labels = [l for l, _ in seen]
    assert "Writing script" in labels and "Rendering" in labels
    for label in labels:
        assert not any(bad in label.lower() for bad in
                       ("pexels", "edge", "ffmpeg", "http", "error", "stage"))


@pytest.mark.asyncio
async def test_resuming_never_repeats_a_finished_stage(tmp_path, fake_engine):
    await pipeline.run(SPEC, tmp_path)
    assert fake_engine == {"script": 1, "footage": 1, "voice": 1, "render": 1}

    await pipeline.run(SPEC, tmp_path)          # a worker restart
    assert fake_engine == {"script": 1, "footage": 1, "voice": 1, "render": 1}, \
        "a completed stage was paid for twice"


@pytest.mark.asyncio
async def test_a_restart_mid_run_keeps_the_script_and_footage(tmp_path, fake_engine):
    """The two expensive stages must survive a crash in a later one."""
    state = pipeline.load_state(tmp_path)
    assert state.completed == []

    # Simulate: script + footage done, then the worker died.
    await pipeline.run(SPEC, tmp_path)
    state = pipeline.load_state(tmp_path)
    state.completed = ["script", "footage"]
    pipeline.save_state(tmp_path, state)
    before = dict(fake_engine)

    await pipeline.run(SPEC, tmp_path)
    assert fake_engine["script"] == before["script"], "script was rewritten"
    assert fake_engine["footage"] == before["footage"], "footage was re-downloaded"
    assert fake_engine["voice"] == before["voice"] + 1


@pytest.mark.asyncio
async def test_a_corrupt_checkpoint_restarts_instead_of_stranding_the_job(
    tmp_path, fake_engine
):
    (tmp_path / "state.json").write_text("{ not json", encoding="utf-8")
    state = await pipeline.run(SPEC, tmp_path)
    assert state.completed == list(pipeline.STAGES)


@pytest.mark.asyncio
async def test_an_empty_topic_is_terminal(tmp_path, fake_engine):
    with pytest.raises(pipeline.TerminalFailure):
        await pipeline.run({"topic": "   "}, tmp_path)


@pytest.mark.asyncio
async def test_no_footage_anywhere_is_terminal(tmp_path, fake_engine, monkeypatch):
    async def nothing(terms, directory, *, portrait=True, concurrency=4):
        raise footage.NoFootage("nothing found")

    monkeypatch.setattr(pipeline.footage, "gather", nothing)
    with pytest.raises(pipeline.TerminalFailure):
        await pipeline.run(SPEC, tmp_path)


@pytest.mark.asyncio
async def test_a_transient_render_failure_is_recoverable_not_terminal(
    tmp_path, fake_engine, monkeypatch
):
    from aivideo import compose

    def boom(**kwargs):
        raise compose.RenderFailed("ffmpeg fell over")

    monkeypatch.setattr(pipeline.compose, "render", boom)
    with pytest.raises(pipeline.RecoverableFailure):
        await pipeline.run(SPEC, tmp_path)

    # And the checkpoint still holds everything that succeeded.
    state = pipeline.load_state(tmp_path)
    assert "script" in state.completed and "footage" in state.completed
    assert "render" not in state.completed


@pytest.mark.asyncio
async def test_music_failure_falls_back_to_a_silent_render(
    tmp_path, fake_engine, monkeypatch
):
    """Losing optional music must never lose the video."""
    from aivideo import compose

    attempts = {"n": 0}

    def render(*, visual, narration, output, spec, captions, music, duration):
        attempts["n"] += 1
        if music is not None:
            raise compose.RenderFailed("bad music stream")
        output.write_bytes(b"x" * 200_000)
        return output

    library = tmp_path / "music"
    library.mkdir()
    (library / "bed.mp3").write_bytes(b"x" * 1000)
    monkeypatch.setattr(pipeline.compose, "render", render)

    state = await pipeline.run(SPEC, tmp_path, music_library=library)
    assert attempts["n"] == 2
    assert any("without background music" in f for f in state.fallbacks)
    assert Path(state.output).exists()


@pytest.mark.asyncio
async def test_an_unpreparable_clip_is_dropped_not_fatal(
    tmp_path, fake_engine, monkeypatch
):
    def one_bad(source, target, spec, seconds):
        if source.name.endswith("_00.mp4"):
            raise RuntimeError("corrupt download")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 20_000)
        return target

    monkeypatch.setattr(pipeline.compose, "normalise_clip", one_bad)
    state = await pipeline.run(SPEC, tmp_path)
    assert Path(state.output).exists()
    assert any("replaced an unusable clip" in f for f in state.fallbacks)


@pytest.mark.asyncio
async def test_a_video_that_fails_validation_reopens_render(
    tmp_path, fake_engine, monkeypatch
):
    """Never ship a file that is not a video, even if render claimed success."""
    monkeypatch.setattr(pipeline.compose, "validate_output",
                        lambda p, **k: (False, "output has no audio stream"))
    with pytest.raises(pipeline.RecoverableFailure):
        await pipeline.run(SPEC, tmp_path)
    assert "render" not in pipeline.load_state(tmp_path).completed


@pytest.mark.asyncio
async def test_a_full_disk_stops_before_rendering(tmp_path, fake_engine, monkeypatch):
    monkeypatch.setattr(pipeline, "free_bytes", lambda p: 10 * 1024 * 1024)
    with pytest.raises(pipeline.RecoverableFailure, match="disk space"):
        await pipeline.run(SPEC, tmp_path)


@pytest.mark.asyncio
async def test_arabic_runs_end_to_end(tmp_path, fake_engine):
    state = await pipeline.run(
        {"topic": "حقائق عن دبي", "language": "ar", "duration_seconds": 30},
        tmp_path,
    )
    assert state.completed == list(pipeline.STAGES)


# ── cost ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_finished_run_records_what_it_cost(tmp_path, fake_engine):
    state = await pipeline.run(SPEC, tmp_path)
    assert state.cost_usd > 0, "a metered model call was not costed"
    assert state.cost_usd < 0.05, "a 30s video should cost fractions of a cent"
    labels = [i["label"] for i in state.cost_items]
    assert "script" in labels
    # The free stages are itemised too, so the total is explainable.
    assert "voice" in labels


@pytest.mark.asyncio
async def test_resuming_does_not_double_charge(tmp_path, fake_engine):
    first = await pipeline.run(SPEC, tmp_path)
    again = await pipeline.run(SPEC, tmp_path)
    assert again.cost_usd == first.cost_usd


def test_the_ledger_prices_a_known_model_and_flags_an_unknown_one():
    from aivideo.cost import CostLedger

    ledger = CostLedger()
    ledger.record_model_call(
        model="gemini-3-flash-preview", input_tokens=1_000_000,
        output_tokens=0, label="script",
    )
    assert ledger.total_usd == pytest.approx(0.30)
    assert ledger.unpriced_models == []

    ledger.record_model_call(
        model="some-new-model", input_tokens=100, output_tokens=10, label="x"
    )
    assert "some-new-model" in ledger.unpriced_models, \
        "an unpriced model must be visible, not silently free"


def test_free_stages_are_recorded_as_free_not_omitted():
    from aivideo.cost import CostLedger

    ledger = CostLedger()
    ledger.record_free("voice", "edge")
    assert ledger.total_usd == 0.0
    assert ledger.items[0]["detail"] == "edge"


# ── music selection ──────────────────────────────────────────────────

def test_auto_music_is_deterministic_so_a_resume_sounds_the_same(tmp_path):
    library = tmp_path / "m"
    library.mkdir()
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (library / name).write_bytes(b"x")
    spec = normalise_spec({"topic": "Dubai", "music": "auto"})
    picked = pipeline.pick_music(spec, library)
    assert picked is not None
    assert pipeline.pick_music(spec, library) == picked


def test_no_music_library_is_not_an_error(tmp_path):
    spec = normalise_spec({"topic": "t", "music": "auto"})
    assert pipeline.pick_music(spec, None) is None
    assert pipeline.pick_music(spec, tmp_path / "missing") is None


def test_music_none_means_none(tmp_path):
    library = tmp_path / "m"
    library.mkdir()
    (library / "a.mp3").write_bytes(b"x")
    spec = normalise_spec({"topic": "t", "music": "none"})
    assert pipeline.pick_music(spec, library) is None


# ── isolation from the other products ────────────────────────────────

def test_the_engine_does_not_import_the_channel_pipeline():
    """A broken Football module must not be able to break this product."""
    import inspect

    for module in (pipeline, footage, subtitles, voice, script_stage):
        source = inspect.getsource(module)
        assert "from core" not in source, f"{module.__name__} imports core/"
        assert "import core" not in source, f"{module.__name__} imports core/"


def test_output_validation_rejects_the_shapes_that_are_not_videos(tmp_path):
    from aivideo import compose

    missing = tmp_path / "nope.mp4"
    ok, why = compose.validate_output(missing)
    assert not ok and "no output file" in why

    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"x" * 100)
    ok, why = compose.validate_output(tiny)
    assert not ok and "bytes" in why
