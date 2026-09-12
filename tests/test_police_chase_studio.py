from pathlib import Path

from medialab.fingerprint import ClipSignature
from medialab.shots import Shot
from viral import police_chase as chase
from viral.clipping import (
    ClipSpec,
    build_clip_command,
    caption_cues,
    caption_filter,
    run_clip_batch,
)
from viral.police_chase import (
    CTA_ALTERNATIVES,
    action_candidates,
    caption_decision,
    choose_cta,
    normalize_source_metadata,
    select_chase_moments,
)


def _words(text: str, start: float = 10.0):
    return [
        {"word": word, "start": start + index * 0.45, "end": start + index * 0.45 + 0.4}
        for index, word in enumerate(text.split())
    ]


def test_source_metadata_records_reuse_and_verification_without_overclaiming():
    owned = normalize_source_metadata({"title": "Dashcam", "reuse_basis": "owned_or_permitted"})
    assert owned.verification_status == "user_attested"
    assert owned.reuse_basis == "owned_or_permitted"

    reusable = normalize_source_metadata({
        "title": "Agency release",
        "source_agency": "Example Police Department",
        "original_source_url": "https://example.gov/release/42",
        "reuse_basis": "public_domain",
        "attribution_requirement": "Agency public-domain release",
    })
    assert reusable.verification_status == "verified_reusable"
    assert reusable.to_record()["original_source_url"].startswith("https://")


def test_caption_policy_avoids_duplicate_burned_english_and_keeps_arabic_explicit():
    assert caption_decision("auto", True)["action"] == "preserve"
    assert caption_decision("en", True)["action"] == "preserve"
    assert caption_decision("en", False)["action"] == "generate_en"
    assert caption_decision("ar", True)["action"] == "translate_ar"
    assert caption_decision("none", False)["action"] == "none"


def test_cta_is_deterministic_customizable_and_optional():
    first = choose_cta("auto", "", "asset-1:clip-1")
    assert first == choose_cta("auto", "", "asset-1:clip-1")
    assert first["text"] in CTA_ALTERNATIVES
    assert choose_cta("custom", "  Watch the full report  ", "x")["text"] == "Watch the full report"
    assert choose_cta("off", "ignored", "x")["text"] == ""


def test_chase_selection_uses_supported_transcript_and_visual_evidence_only():
    words = _words("dispatch reports the pursuit began then the suspect made a wrong way near miss")
    moments = select_chase_moments(
        words,
        [
            {"seconds": 44, "label": "pit_maneuver", "confidence": 0.92},
            {"seconds": 80, "label": "explosion", "confidence": 0.99},
            {"seconds": 95, "label": "arrest", "confidence": 0.30},
        ],
        source_duration=120,
        target_seconds=30,
        count=3,
    )
    assert moments
    assert any(moment.reason == "Pit Maneuver" for moment in moments)
    assert all(moment.reason != "Explosion" for moment in moments)
    assert all(moment.reason != "Arrest" for moment in moments)
    assert all(0 <= moment.start < moment.end <= 120 for moment in moments)


def test_one_failed_chase_clip_does_not_remove_completed_clips_and_resume_skips_work():
    specs = [ClipSpec("one", 0, 15), ClipSpec("two", 15, 30)]
    calls = []

    def render(spec):
        calls.append(spec.id)
        if spec.id == "two":
            raise RuntimeError("temporary render issue")
        return f"{spec.id}.mp4"

    result = run_clip_batch(specs, render, max_attempts=1)
    assert [(row.id, row.status) for row in result] == [("one", "done"), ("two", "failed")]
    calls.clear()
    resumed = run_clip_batch(specs, render, completed={"one": "one.mp4"}, max_attempts=1)
    assert resumed[0].status == "skipped"
    assert calls == ["two"]


# ── what gets picked ─────────────────────────────────────────────────

def test_the_strongest_kind_of_event_is_picked_first_not_the_earliest():
    """A dispatch radio call used to rank level with a PIT manoeuvre."""
    moments = select_chase_moments(
        [],
        [
            {"seconds": 20, "label": "police_interaction", "confidence": 0.99},
            {"seconds": 90, "label": "pit_maneuver", "confidence": 0.70},
        ],
        source_duration=120,
        target_seconds=30,
        count=1,
    )
    assert moments[0].reason == "Pit Maneuver"


def test_a_crash_in_the_transcript_outranks_routine_police_chatter():
    words = _words(
        "an officer on patrol asked dispatch to run the plate and then began "
        "a routine traffic stop and told the driver to pull over calmly",
        start=8.0,
    ) + _words(
        "the driver lost control coming off the ramp and the crash threw the "
        "car sideways into a collision that left the wreck across both lanes",
        start=60.0,
    )
    moments = select_chase_moments(
        words, [], source_duration=120, target_seconds=30, count=1
    )
    assert moments[0].reason == "Crash"


def test_action_density_proposes_moments_but_never_names_an_event():
    """Police sources are often one long take with no usable transcript.

    A burst of content changes is evidence the camera is working hard. It is
    not evidence of a PIT manoeuvre, and the proposal must not claim one.
    """
    shots = [Shot(index * 2.0, index * 2.0 + 2.0) for index in range(15)]
    found = action_candidates(shots, source_duration=30, target_seconds=10)
    assert found
    assert all(moment.reason == "Sustained on-camera action" for moment in found)
    assert all(not moment.signals.get("visual_event") for moment in found)


def test_a_single_continuous_take_proposes_nothing_from_action_density():
    assert action_candidates([Shot(0.0, 120.0)], source_duration=120, target_seconds=30) == []


def _stub_video(monkeypatch, tmp_path, shots, signature):
    video = tmp_path / "chase.mp4"
    video.write_bytes(b"")
    monkeypatch.setattr(chase.shotlib, "detect_shots", lambda source: shots)
    monkeypatch.setattr(
        chase.fingerprint, "signature_between",
        lambda source, start, end, count=3: signature(start),
    )
    return video


def test_two_moments_that_look_identical_are_not_both_shipped(monkeypatch, tmp_path):
    """One impact seen from two windows is one clip, not two.

    Temporal overlap does not catch this: windows seconds apart on the same
    crash barely overlap and are the same footage to a viewer.
    """
    same = ClipSignature(path=Path("chase.mp4"), hashes=[0xA1B2C3D4E5F60718])
    video = _stub_video(monkeypatch, tmp_path, [], lambda start: same)
    moments = select_chase_moments(
        [],
        [
            {"seconds": 30, "label": "crash", "confidence": 0.9},
            {"seconds": 75, "label": "pit_maneuver", "confidence": 0.9},
        ],
        source_duration=120, target_seconds=20, count=3, video=video,
    )
    assert len(moments) == 1


def test_moments_that_look_different_are_both_kept(monkeypatch, tmp_path):
    video = _stub_video(
        monkeypatch, tmp_path, [],
        lambda start: ClipSignature(
            path=Path("chase.mp4"),
            hashes=[0x0000000000000000 if start < 50 else 0xFFFFFFFFFFFFFFFF],
        ),
    )
    moments = select_chase_moments(
        [],
        [
            {"seconds": 30, "label": "crash", "confidence": 0.9},
            {"seconds": 75, "label": "pit_maneuver", "confidence": 0.9},
        ],
        source_duration=120, target_seconds=20, count=3, video=video,
    )
    assert len(moments) == 2


def test_a_clip_is_cut_inside_a_shot_rather_than_across_one(monkeypatch, tmp_path):
    shots = [Shot(0.0, 40.0), Shot(40.0, 100.0), Shot(100.0, 120.0)]
    video = _stub_video(
        monkeypatch, tmp_path, shots,
        lambda start: ClipSignature(path=Path("chase.mp4")),
    )
    moments = select_chase_moments(
        [], [{"seconds": 90, "label": "crash", "confidence": 0.9}],
        source_duration=120, target_seconds=30, count=1, video=video,
    )
    assert 40.0 <= moments[0].start and moments[0].end <= 100.0


def test_a_short_shot_does_not_shorten_the_clip(monkeypatch, tmp_path):
    """A 20-second request came back as a 7-second clip on a real source.

    The instant sat inside a 7-second shot and the window was truncated to
    it. Opening on the cut is what matters; police sources are compilations
    and a pursuit legitimately runs across several camera angles.
    """
    shots = [Shot(0.0, 24.0), Shot(24.0, 31.0), Shot(31.0, 83.0)]
    video = _stub_video(
        monkeypatch, tmp_path, shots,
        lambda start: ClipSignature(path=Path("chase.mp4")),
    )
    moments = select_chase_moments(
        [], [{"seconds": 26.0, "label": "crash", "confidence": 0.9}],
        source_duration=83.0, target_seconds=20, count=1, video=video,
    )
    assert moments[0].start == 24.0, "the clip should open on the cut"
    assert moments[0].duration >= 19.0, (
        f"asked for 20s, got {moments[0].duration}s"
    )


def test_selection_without_a_video_behaves_exactly_as_before():
    """The visual half is additive; a source we cannot open still works."""
    words = _words(
        "the pursuit began on the interstate and the suspect made a wrong way "
        "turn into oncoming traffic and came within inches of a near miss"
    )
    moments = select_chase_moments(
        words, [], source_duration=120, target_seconds=30, count=2
    )
    assert moments
    assert all(0 <= m.start < m.end <= 120 for m in moments)


# ── Arabic captions ──────────────────────────────────────────────────

#: "The police pursued the vehicle at high speed on the highway and then
#: stopped it." Escapes, not literals: this repository has had an Arabic
#: source file silently mojibaked by a PowerShell round trip once already.
_ARABIC = (
    "\u0637\u0627\u0631\u062f\u062a \u0627\u0644\u0634\u0631\u0637"
    "\u0629 \u0627\u0644\u0645\u0631\u0643\u0628\u0629 \u0628\u0633"
    "\u0631\u0639\u0629 \u0639\u0627\u0644\u064a\u0629 \u0639\u0644"
    "\u0649 \u0627\u0644\u0637\u0631\u064a\u0642 \u0627\u0644\u0633"
    "\u0631\u064a\u0639 \u062b\u0645 \u0623\u0648\u0642\u0641\u062a"
    "\u0647\u0627"
)


def _arabic_words():
    return [
        {"word": word, "start": index * 0.5, "end": index * 0.5 + 0.45}
        for index, word in enumerate(_ARABIC.split())
    ]


def test_arabic_captions_are_shaped_and_never_exceed_two_lines():
    cues = caption_cues(_arabic_words(), clip_start=0.0, clip_end=12.0, language="ar")
    assert cues
    for cue in cues:
        assert cue.text.count("\n") <= 1, f"three or more lines in {cue.text!r}"
        assert cue.text.strip()
    # Shaping rewrites letters into their joined presentation forms. Their
    # absence means the caption would render as disconnected islands.
    body = "".join(cue.text for cue in cues)
    shaped = any("\ufe70" <= ch <= "\ufeff" or "\ufb50" <= ch <= "\ufdff" for ch in body)
    assert shaped, "text was never shaped into joined presentation forms"


def test_arabic_cues_are_not_pre_reversed():
    """libass runs bidi. Reversing here too breaks every letter's joining."""
    cues = caption_cues(_arabic_words(), clip_start=0.0, clip_end=12.0, language="ar")
    first_plain_word = _ARABIC.split()[0]
    first_shaped_word = cues[0].text.replace("\n", " ").split()[0]
    assert len(first_shaped_word) == len(first_plain_word), (
        "the first word on screen is not the first word spoken"
    )


def test_arabic_cues_stay_inside_the_clip_and_in_order():
    cues = caption_cues(_arabic_words(), clip_start=0.0, clip_end=12.0, language="ar")
    assert all(0.0 <= cue.start < cue.end <= 12.0 for cue in cues)
    assert cues == sorted(cues, key=lambda cue: cue.start)


def test_english_captions_are_untouched_by_the_arabic_path():
    words = _words("the pursuit ended on the highway", start=0.0)
    cues = caption_cues(words, clip_start=0.0, clip_end=8.0)
    assert cues
    assert all("\n" not in cue.text for cue in cues)
    assert "pursuit" in " ".join(cue.text for cue in cues)


def test_the_arabic_burn_in_names_a_font_libass_can_actually_find(tmp_path):
    """A style naming a face libass was never pointed at renders as boxes."""
    fragment = caption_filter(tmp_path / "c.srt", "bold", language="ar")
    assert "FontName=Noto Sans Arabic" in fragment
    assert "fontsdir=" in fragment
    assert "FontName" not in caption_filter(tmp_path / "c.srt", "bold")


def test_the_original_police_audio_is_kept(tmp_path):
    command = " ".join(build_clip_command(
        Path("source.mp4"), tmp_path / "out.mp4",
        ClipSpec("keep", 0, 20, aspect="9:16"), has_audio=True,
    ))
    assert " -an" not in f" {command}"


def test_vertical_render_command_can_add_deterministic_cta_without_changing_defaults(tmp_path):
    spec = ClipSpec("turn", 5, 20, aspect="9:16", captions=False)
    plain = build_clip_command(Path("source.mp4"), Path("plain.mp4"), spec)
    with_cta = build_clip_command(Path("source.mp4"), Path("cta.mp4"), spec, cta_path=tmp_path / "cta.srt")
    assert "subtitles=" not in " ".join(plain)
    assert "cta.srt" in " ".join(with_cta)
    assert "1080:1920" in " ".join(with_cta)
