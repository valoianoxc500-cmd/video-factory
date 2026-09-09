"""Clipping: the failure modes that used to cost a customer their work.

Offline and mocked throughout -- no ffmpeg, no network, no paid call. The
renderer is injected, so batching, retry and resume are tested as logic rather
than as a video pipeline.

The properties worth pinning are the ones that decide whether this is a
product or a demo:

    a bad clip does not take the good ones with it
    a retry does not re-render what already succeeded
    a broken transcript costs the captions, not the clip
    detection being unavailable costs the framing, not the clip
    rights refusals still stop everything, unchanged
"""

import pytest

from viral import clipping as clip
from viral.processing import SourceProbe
from viral.rights import RightsAttestation, RightsError, Source


# ── fixtures ─────────────────────────────────────────────────────────

def _attestation() -> RightsAttestation:
    """An attestation that passes the existing gate.

    `OWNED_OR_PERMITTED` is what the Add-a-video form actually records, so
    that is what these tests exercise rather than a value the product never
    produces.
    """
    from viral.rights import attest
    return attest(
        source=Source.OWNED_OR_PERMITTED.value,
        user_id="user-1",
        rights_holder="user-1",
        evidence="uploaded by the owner",
    )


def _probe(duration: float = 120.0) -> SourceProbe:
    return SourceProbe(width=1920, height=1080, duration=duration, fps=30.0, has_audio=True)


def _requests(*ranges, **options):
    return [
        {"id": f"clip_{i + 1}", "start": s, "end": e, **options}
        for i, (s, e) in enumerate(ranges)
    ]


# ── 1. authorized source flow ────────────────────────────────────────

def test_an_authorized_source_plans_its_clips():
    plan = clip.plan_clips(
        _probe(), _requests((0, 15), (30, 48)), attestation=_attestation()
    )
    assert [s.id for s in plan.specs] == ["clip_1", "clip_2"]
    assert plan.specs[0].duration == 15.0
    assert not plan.rejected


def test_planning_without_an_attestation_is_refused():
    """The existing rights gate is unchanged and still runs first."""
    with pytest.raises(RightsError):
        clip.plan_clips(_probe(), _requests((0, 10)), attestation=None)


@pytest.mark.parametrize("technique", [
    "mirror flip to avoid matching",
    "content id evasion",
    "watermark removal",
    "fingerprint perturbation",
])
def test_a_prohibited_technique_is_still_refused(technique):
    """Clipping inherits the same refusal list as single-file processing.

    A plain "mirror" is not on it, and should not be: flipping footage you own
    is ordinary editing. What is refused is flipping it *to avoid matching*.
    """
    with pytest.raises(RightsError):
        clip.plan_clips(
            _probe(), _requests((0, 10)),
            attestation=_attestation(),
            requested_steps=[technique],
        )


def test_ordinary_editing_steps_are_not_refused():
    plan = clip.plan_clips(
        _probe(), _requests((0, 10)),
        attestation=_attestation(),
        requested_steps=["reframe", "normalise audio", "trim"],
    )
    assert len(plan.specs) == 1


def test_a_rights_refusal_is_never_retried_in_a_batch():
    """A refusal is a decision; retrying it would be arguing with the gate."""
    specs = [clip.ClipSpec(id="a", start=0, end=5)]
    calls = []

    def render(spec):
        calls.append(spec.id)
        raise RightsError("You have not confirmed you own this footage.")

    with pytest.raises(RightsError):
        clip.run_clip_batch(specs, render)
    assert calls == ["a"], "a rights refusal must not be retried"


# ── 2. unsupported / invalid ranges are rejected safely ──────────────

@pytest.mark.parametrize("start,end,fragment", [
    (50, 20, "ends before it starts"),
    (500, 520, "starts after the video ends"),
    (10, 10.2, "too short"),
])
def test_bad_ranges_are_rejected_with_a_readable_reason(start, end, fragment):
    plan = clip.plan_clips(
        _probe(), _requests((start, end)), attestation=_attestation()
    )
    assert not plan.specs
    assert fragment in plan.rejected[0].reason


def test_unreadable_requests_do_not_crash_planning():
    plan = clip.plan_clips(
        _probe(),
        ["nonsense", None, 42, {"id": "ok", "start": 0, "end": 10}],
        attestation=_attestation(),
    )
    assert [s.id for s in plan.specs] == ["ok"]
    assert len(plan.rejected) == 3


def test_non_numeric_times_are_rejected_not_coerced():
    plan = clip.plan_clips(
        _probe(),
        [{"id": "x", "start": "soon", "end": "later"}],
        attestation=_attestation(),
    )
    assert not plan.specs
    assert "not numbers" in plan.rejected[0].reason


def test_a_clip_past_the_end_is_shortened_and_the_user_is_told():
    plan = clip.plan_clips(
        _probe(duration=60), _requests((50, 90)), attestation=_attestation()
    )
    assert plan.specs[0].end == 60.0
    assert any("shortened" in w for w in plan.warnings)


def test_duplicate_ids_are_rejected_rather_than_overwriting():
    plan = clip.plan_clips(
        _probe(),
        [{"id": "same", "start": 0, "end": 5}, {"id": "same", "start": 10, "end": 15}],
        attestation=_attestation(),
    )
    assert len(plan.specs) == 1
    assert "Duplicate" in plan.rejected[0].reason


def test_an_unmeasured_duration_does_not_reject_everything():
    """A failed probe reports 0.0; clamping to it would reject every clip."""
    plan = clip.plan_clips(
        SourceProbe(duration=0.0), _requests((0, 20)), attestation=_attestation()
    )
    assert len(plan.specs) == 1


# ── 3. options ───────────────────────────────────────────────────────

def test_every_option_defaults_safely_when_junk_is_sent():
    plan = clip.plan_clips(
        _probe(),
        [{"id": "a", "start": 0, "end": 10, "aspect": "17:3",
          "quality": "ultra", "focus": "diagonal", "caption_style": "neon"}],
        attestation=_attestation(),
    )
    spec = plan.specs[0]
    assert spec.aspect == clip.DEFAULT_ASPECT
    assert spec.quality == clip.DEFAULT_QUALITY
    assert spec.focus == clip.DEFAULT_FOCUS
    assert spec.caption_style == clip.DEFAULT_CAPTION_STYLE


@pytest.mark.parametrize("aspect", list(clip.ASPECTS))
def test_each_aspect_produces_its_own_geometry(aspect):
    spec = clip.ClipSpec(id="a", start=0, end=5, aspect=aspect)
    cmd = clip.build_clip_command(
        __import__("pathlib").Path("in.mp4"),
        __import__("pathlib").Path("out.mp4"),
        spec,
    )
    graph = cmd[cmd.index("-vf") + 1]
    target = clip.ASPECTS[aspect]
    assert f"scale={target.width}:{target.height}" in graph
    assert f"crop={target.width}:{target.height}" in graph


@pytest.mark.parametrize("quality", list(clip.QUALITIES))
def test_quality_choice_reaches_the_encoder(quality):
    spec = clip.ClipSpec(id="a", start=0, end=5, quality=quality)
    cmd = clip.build_clip_command(
        __import__("pathlib").Path("in.mp4"),
        __import__("pathlib").Path("out.mp4"),
        spec,
    )
    assert str(clip.QUALITIES[quality].crf) in cmd
    assert clip.QUALITIES[quality].preset in cmd


def test_a_silent_source_is_encoded_without_an_audio_track():
    cmd = clip.build_clip_command(
        __import__("pathlib").Path("in.mp4"),
        __import__("pathlib").Path("out.mp4"),
        clip.ClipSpec(id="a", start=0, end=5),
        has_audio=False,
    )
    assert "-an" in cmd
    assert "aac" not in cmd


def test_the_clip_is_cut_at_the_requested_range():
    cmd = clip.build_clip_command(
        __import__("pathlib").Path("in.mp4"),
        __import__("pathlib").Path("out.mp4"),
        clip.ClipSpec(id="a", start=12.5, end=27.5),
    )
    assert cmd[cmd.index("-ss") + 1] == "12.500"
    assert cmd[cmd.index("-t") + 1] == "15.000"


# ── 4. captions on/off and recovery ──────────────────────────────────

WORDS = [
    {"word": "the", "start": 0.0, "end": 0.3},
    {"word": "night", "start": 0.3, "end": 0.7},
    {"word": "guard", "start": 0.7, "end": 1.1},
    {"word": "checked", "start": 1.1, "end": 1.6},
    {"word": "every", "start": 1.6, "end": 2.0},
    {"word": "floor", "start": 2.0, "end": 2.5},
]


def test_captions_off_puts_no_subtitle_filter_in_the_graph():
    cmd = clip.build_clip_command(
        __import__("pathlib").Path("in.mp4"),
        __import__("pathlib").Path("out.mp4"),
        clip.ClipSpec(id="a", start=0, end=5, captions=False),
        subtitle_path=__import__("pathlib").Path("subs.srt"),
    )
    assert "subtitles" not in cmd[cmd.index("-vf") + 1]


def test_captions_on_burns_the_subtitle_file():
    cmd = clip.build_clip_command(
        __import__("pathlib").Path("in.mp4"),
        __import__("pathlib").Path("out.mp4"),
        clip.ClipSpec(id="a", start=0, end=5, captions=True),
        subtitle_path=__import__("pathlib").Path("subs.srt"),
    )
    assert "subtitles" in cmd[cmd.index("-vf") + 1]


def test_cues_are_rebased_to_the_clip():
    """A clip cut from 60s in must caption from zero, not from sixty."""
    words = [{"word": w, "start": 60 + i * 0.4, "end": 60 + (i + 1) * 0.4}
             for i, w in enumerate("a b c d".split())]
    cues = clip.caption_cues(words, clip_start=60.0, clip_end=62.0)
    assert cues
    assert cues[0].start == pytest.approx(0.0, abs=0.05)


def test_cues_are_split_by_length_and_time():
    cues = clip.caption_cues(WORDS, clip_start=0.0, clip_end=3.0, max_chars=12)
    assert len(cues) > 1
    assert all(len(c.text) <= 20 for c in cues)


@pytest.mark.parametrize("broken", [
    None,
    "not a list",
    42,
    [],
    [None, 5, True],
    [{}, {"word": ""}, {"nope": 1}],
])
def test_a_malformed_transcript_yields_no_captions_and_no_exception(broken):
    assert clip.caption_cues(broken, clip_start=0.0, clip_end=10.0) == []


def test_words_without_timestamps_fall_back_to_segmentation():
    """A transcript with words but no timings is still worth captioning."""
    words = [{"word": w} for w in "one two three four five".split()]
    cues = clip.caption_cues(words, clip_start=0.0, clip_end=5.0)
    assert cues, "a timed fallback was available and should have been used"
    assert cues[0].start >= 0.0
    assert cues[-1].end <= 5.01


def test_words_without_timestamps_and_no_clip_length_are_dropped():
    """Nothing to spread across: guessing would put captions anywhere."""
    words = [{"word": w} for w in "one two three".split()]
    assert clip.caption_cues(words, clip_start=0.0, clip_end=None) == []


def test_unusable_times_are_skipped_rather_than_poisoning_the_cue():
    words = [
        {"word": "good", "start": 0.0, "end": 0.4},
        {"word": "bad", "start": "x", "end": None},
        {"word": "also", "start": float("nan"), "end": 1.0},
        {"word": "fine", "start": 0.4, "end": 0.9},
    ]
    cues = clip.caption_cues(words, clip_start=0.0, clip_end=2.0)
    text = " ".join(c.text for c in cues)
    assert "good" in text and "fine" in text


def test_out_of_order_and_zero_length_words_are_repaired():
    words = [
        {"word": "second", "start": 1.0, "end": 1.4},
        {"word": "first", "start": 0.0, "end": 0.0},
    ]
    cues = clip.caption_cues(words, clip_start=0.0, clip_end=2.0)
    assert cues
    assert cues[0].text.startswith("first")
    assert all(c.end > c.start for c in cues)


def test_words_outside_the_clip_window_are_excluded():
    words = [
        {"word": "before", "start": 0.0, "end": 1.0},
        {"word": "inside", "start": 5.2, "end": 5.8},
        {"word": "after", "start": 30.0, "end": 31.0},
    ]
    cues = clip.caption_cues(words, clip_start=5.0, clip_end=6.0)
    assert " ".join(c.text for c in cues) == "inside"


def test_srt_is_well_formed_or_empty():
    assert clip.cues_to_srt([]) == ""
    srt = clip.cues_to_srt(clip.caption_cues(WORDS, clip_start=0.0, clip_end=3.0))
    assert srt.startswith("1\n")
    assert "-->" in srt
    assert "00:00:" in srt


def test_the_caption_filter_escapes_a_windows_path():
    """An unescaped drive colon breaks the whole filtergraph."""
    fragment = clip.caption_filter(
        __import__("pathlib").Path(r"C:\tmp\subs.srt"), "clean"
    )
    assert r"\:" in fragment
    assert "\\t" not in fragment.replace(r"\:", "")


def test_caption_styles_differ_and_clear_the_lower_third():
    seen = set()
    for style in clip.CAPTION_STYLES:
        args = clip._CAPTION_STYLE_ARGS[style]
        seen.add(args)
        margin = int(args.split("MarginV=")[1])
        assert margin >= 100, "captions must not sit over a face"
    assert len(seen) == len(clip.CAPTION_STYLES)


# ── 5. person focus and fallback ─────────────────────────────────────

def test_auto_focus_uses_the_detector_when_it_works():
    spec = clip.ClipSpec(id="a", start=0, end=8, focus="auto")
    centre = clip.focus_centre(
        __import__("pathlib").Path("in.mp4"), spec, detector=lambda p, t: 0.8
    )
    assert centre == pytest.approx(0.8)


def test_auto_focus_samples_inside_the_clip_not_the_file_start():
    seen = {}

    def detector(path, at):
        seen["at"] = at
        return 0.5

    spec = clip.ClipSpec(id="a", start=300.0, end=308.0, focus="auto")
    clip.focus_centre(__import__("pathlib").Path("in.mp4"), spec, detector=detector)
    assert seen["at"] >= 300.0, "sampled the wrong part of the video"


def test_focus_falls_back_to_centre_when_detection_is_unavailable():
    def detector(path, at):
        raise ImportError("no imaging libraries here")

    spec = clip.ClipSpec(id="a", start=0, end=8, focus="auto")
    assert clip.focus_centre(
        __import__("pathlib").Path("in.mp4"), spec, detector=detector
    ) == 0.5


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0, 12.0, "left"])
def test_a_nonsense_detection_is_clamped_or_centred(bad):
    spec = clip.ClipSpec(id="a", start=0, end=8, focus="auto")
    value = clip.focus_centre(
        __import__("pathlib").Path("in.mp4"), spec, detector=lambda p, t: bad
    )
    assert 0.0 <= value <= 1.0


@pytest.mark.parametrize("mode,expected", [
    ("center", 0.5), ("left", 0.25), ("right", 0.75),
])
def test_manual_focus_never_calls_the_detector(mode, expected):
    def detector(path, at):
        raise AssertionError("manual focus must not run detection")

    spec = clip.ClipSpec(id="a", start=0, end=8, focus=mode)
    assert clip.focus_centre(
        __import__("pathlib").Path("in.mp4"), spec, detector=detector
    ) == expected


# ── 6. one failure must not kill the job ─────────────────────────────

def _specs(*ids):
    return [clip.ClipSpec(id=i, start=0, end=5) for i in ids]


def test_one_failed_clip_leaves_the_others_finished():
    def render(spec):
        if spec.id == "b":
            raise RuntimeError("ffmpeg exited with code 1")
        return f"/out/{spec.id}.mp4"

    results = clip.run_clip_batch(_specs("a", "b", "c"), render)
    by_id = {r.id: r for r in results}
    assert by_id["a"].status == "done"
    assert by_id["c"].status == "done"
    assert by_id["b"].status == "failed"

    summary = clip.batch_summary(results)
    assert summary["ready"] == 2
    assert summary["failed"] == 1
    assert summary["ok"] is True, "partial success is success"


def test_a_batch_where_everything_fails_is_not_ok():
    results = clip.run_clip_batch(
        _specs("a", "b"), lambda spec: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    summary = clip.batch_summary(results)
    assert summary["ready"] == 0
    assert summary["ok"] is False
    assert "No clips could be created" in summary["message"]


def test_a_renderer_returning_nothing_counts_as_a_failure():
    results = clip.run_clip_batch(_specs("a"), lambda spec: "")
    assert results[0].status == "failed"


def test_retries_are_bounded():
    attempts = []

    def render(spec):
        attempts.append(spec.id)
        raise RuntimeError("still broken")

    clip.run_clip_batch(_specs("a"), render, max_attempts=2)
    assert len(attempts) == 2, "retries must be bounded"


def test_a_clip_that_succeeds_on_retry_is_kept():
    calls = {"n": 0}

    def render(spec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "/out/a.mp4"

    results = clip.run_clip_batch(_specs("a"), render, max_attempts=2)
    assert results[0].status == "done"
    assert results[0].attempts == 2


# ── 7. resume without duplicate work ─────────────────────────────────

def test_completed_clips_are_not_rendered_again():
    rendered = []

    def render(spec):
        rendered.append(spec.id)
        return f"/out/{spec.id}.mp4"

    results = clip.run_clip_batch(
        _specs("a", "b", "c"), render,
        completed={"a": "/out/a.mp4", "b": "/out/b.mp4"},
    )
    assert rendered == ["c"], "a resume re-rendered work that was already done"
    by_id = {r.id: r for r in results}
    assert by_id["a"].status == "skipped"
    assert by_id["a"].path == "/out/a.mp4"
    assert clip.batch_summary(results)["ready"] == 3


def test_a_blank_completed_path_is_not_trusted():
    """An empty path is a lost file, not a finished clip."""
    rendered = []

    def render(spec):
        rendered.append(spec.id)
        return f"/out/{spec.id}.mp4"

    clip.run_clip_batch(_specs("a"), render, completed={"a": ""})
    assert rendered == ["a"]


def test_resuming_a_fully_complete_job_renders_nothing():
    def render(spec):
        raise AssertionError("nothing should be rendered")

    results = clip.run_clip_batch(
        _specs("a", "b"), render,
        completed={"a": "/out/a.mp4", "b": "/out/b.mp4"},
    )
    assert clip.batch_summary(results)["reused"] == 2


# ── 8. customer-facing states and errors ─────────────────────────────

@pytest.mark.parametrize("stage,expected", [
    ("queued", "Preparing"),
    ("downloading", "Preparing"),
    ("transcribing", "Analyzing"),
    ("clipping", "Creating clips"),
    ("encoding", "Rendering"),
    ("done", "Ready"),
])
def test_internal_stages_map_to_simple_states(stage, expected):
    assert clip.customer_state(stage) == expected


def test_an_unknown_stage_never_leaks_its_name():
    assert clip.customer_state("ffmpeg_pass_2") == "Preparing"
    assert clip.customer_state("") == "Preparing"


def test_every_state_shown_is_one_of_the_five():
    for stage in list(clip._STATE_BY_STAGE) + ["nonsense", ""]:
        assert clip.customer_state(stage) in clip.CUSTOMER_STATES


@pytest.mark.parametrize("raw", [
    "ffmpeg exited with code 1: Invalid argument",
    "Traceback (most recent call last): ...",
    "/tmp/vrf_process_ab12/source.mp4: No such file",
    r"C:\Users\kokoi\out.mp4 could not be written",
    "libx264 encoder error",
    "https://storage.googleapis.com/bucket/x.mp4 403",
    "",
    None,
])
def test_internal_detail_never_reaches_the_customer(raw):
    message = clip.safe_clip_error(raw)
    assert not clip._INTERNAL.search(message), f"leaked: {message}"
    assert message


def test_a_rights_message_is_shown_because_the_user_can_act_on_it():
    message = clip.safe_clip_error(
        "You have not confirmed you own this footage."
    )
    assert "own this footage" in message


# ── 9. the runner seam ───────────────────────────────────────────────

def _plan(trim_start=0.0, trim_end=0.0):
    from viral.processing import build_plan
    return build_plan(
        _probe(duration=120.0), "tiktok",
        attestation=_attestation(),
        trim_start=trim_start, trim_end=trim_end,
    )


def test_options_become_a_spec_whose_range_comes_from_the_trims():
    """Two places computing a start time is how they drift apart."""
    from viral.runner import _clip_spec_from
    spec = _clip_spec_from(
        {"aspect": "1:1", "quality": "high", "focus": "left", "captions": True},
        _probe(duration=120.0), _plan(trim_start=10.0, trim_end=20.0),
    )
    assert spec.start == 10.0
    assert spec.end == 100.0
    assert spec.aspect == "1:1"
    assert spec.quality == "high"
    assert spec.focus == "left"
    assert spec.captions is True


def test_the_spec_survives_an_unmeasurable_duration():
    from viral.runner import _clip_spec_from
    spec = _clip_spec_from({}, SourceProbe(duration=0.0), _plan())
    assert spec.duration >= 1.0, "a zero-length spec would render nothing"


def test_omitted_options_take_the_documented_defaults():
    from viral.runner import _clip_spec_from
    spec = _clip_spec_from({}, _probe(), _plan())
    assert spec.aspect == clip.DEFAULT_ASPECT
    assert spec.quality == clip.DEFAULT_QUALITY
    assert spec.focus == clip.DEFAULT_FOCUS
    assert spec.captions is False


def test_captions_are_dropped_when_the_transcriber_is_unavailable(tmp_path, monkeypatch):
    """A missing recogniser costs the captions, not the clip."""
    import viral.runner as runner

    class _Fail:
        def __init__(self, *a, **k):
            self.returncode = 0

    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: _Fail())
    # No audio file is produced, so the caption path bails before transcribing.
    result = runner._build_subtitles(
        tmp_path / "in.mp4",
        clip.ClipSpec(id="a", start=0, end=5, captions=True),
        tmp_path,
        {},
    )
    assert result is None


def test_batch_messages_are_plain_language():
    ready = clip.batch_summary([clip.ClipResult("a", "done", path="/o/a.mp4")])
    assert ready["message"] == "1 clip ready."
    mixed = clip.batch_summary([
        clip.ClipResult("a", "done", path="/o/a.mp4"),
        clip.ClipResult("b", "failed", error="That clip could not be created."),
    ])
    assert "1 clip ready" in mixed["message"]
    assert not clip._INTERNAL.search(mixed["message"])
