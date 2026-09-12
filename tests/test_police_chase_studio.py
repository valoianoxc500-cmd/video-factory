from pathlib import Path

from viral.clipping import ClipSpec, build_clip_command, run_clip_batch
from viral.police_chase import (
    CTA_ALTERNATIVES,
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


def test_vertical_render_command_can_add_deterministic_cta_without_changing_defaults(tmp_path):
    spec = ClipSpec("turn", 5, 20, aspect="9:16", captions=False)
    plain = build_clip_command(Path("source.mp4"), Path("plain.mp4"), spec)
    with_cta = build_clip_command(Path("source.mp4"), Path("cta.mp4"), spec, cta_path=tmp_path / "cta.srt")
    assert "subtitles=" not in " ".join(plain)
    assert "cta.srt" in " ".join(with_cta)
    assert "1080:1920" in " ".join(with_cta)
