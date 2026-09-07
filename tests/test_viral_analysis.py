"""AI analysis: depth follows rights, and absences stay absent."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from viral.analysis import (
    AnalysisDepth,
    Scene,
    VideoAnalysis,
    analyse,
    build_frame_prompt,
    build_metadata_prompt,
    depth_for,
    parse_analysis,
    summarise_for_ui,
    to_new_version_inputs,
)
from viral.rights import Source, attest
from viral.scoring import VideoMetrics, new_version_probability, viral_score

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
USER = "11111111-1111-1111-1111-111111111111"

VIDEO = {
    "platform": "youtube", "title": "He didn't expect this", "author": "Dashcam Daily",
    "duration_seconds": 41.0, "views": 2_400_000, "likes": 180_000,
    "comments": 9_000, "followers": 50_000,
}

FULL_PAYLOAD = {
    "hook": "Opens mid-chase with no setup",
    "hook_seconds": 1.2,
    "topic": "Police pursuit dashcam",
    "format": "POV compilation",
    "pacing": "Fast, cuts on every beat",
    "cuts_per_minute": 34.0,
    "scenes": [
        {"start_seconds": 0.0, "label": "cold open", "purpose": "stop the scroll"},
        {"start_seconds": 8.0, "label": "escalation", "purpose": "raise stakes"},
        {"label": "", "purpose": "dropped: no label"},
    ],
    "caption_style": "Bold white with black stroke, centred",
    "audio_style": "Tense score, no voiceover",
    "engagement_drivers": ["unresolved outcome", "comment-bait ending", ""],
    "why_it_performs": "It starts at the peak and never explains itself.",
    "replicable_elements": ["start at the peak moment"],
    "risks": ["graphic content could limit reach"],
    "hook_strength": 0.9,
    "retention_potential": 0.8,
    "topic_freshness": 0.4,
}


# --- depth follows the rights basis ---------------------------------------

def test_a_discovered_video_is_analysed_from_metadata_only():
    """Frame analysis would mean copying someone else's video."""
    found = attest(source=Source.DISCOVERED, user_id=USER, now=NOW)
    assert depth_for(found) is AnalysisDepth.METADATA


def test_no_attestation_also_means_metadata_only():
    assert depth_for(None) is AnalysisDepth.METADATA


@pytest.mark.parametrize("source", [
    Source.OWN_RECORDING, Source.LICENSED, Source.PUBLIC_DOMAIN,
])
def test_footage_the_user_holds_can_be_analysed_in_full(source):
    held = attest(source=source, user_id=USER, rights_holder="Someone",
                  evidence="https://example/licence", now=NOW)
    assert depth_for(held) is AnalysisDepth.FULL


# --- prompts ---------------------------------------------------------------

def test_the_metadata_prompt_forbids_describing_unseen_footage():
    prompt = build_metadata_prompt(VIDEO)
    assert "have NOT been given the video" in prompt
    assert "2400000" in prompt
    assert "He didn't expect this" in prompt


def test_the_frame_prompt_lists_the_sample_times():
    prompt = build_frame_prompt(VIDEO, [0.3, 1.5, 12.0])
    assert "0.3s, 1.5s, 12.0s" in prompt


def test_missing_metrics_read_as_unknown_not_zero():
    prompt = build_metadata_prompt({"platform": "youtube", "title": "x"})
    assert "Views: unknown" in prompt
    assert "Views: 0" not in prompt


def test_the_system_instruction_rules_out_evasion_advice():
    from viral.analysis import SYSTEM_INSTRUCTION

    lowered = SYSTEM_INSTRUCTION.lower()
    assert "evade copyright detection" in lowered
    assert "duplicate-content detection" in lowered


# --- parsing ---------------------------------------------------------------

def test_a_full_response_parses():
    analysis = parse_analysis(FULL_PAYLOAD, AnalysisDepth.FULL)
    assert analysis.hook.startswith("Opens mid-chase")
    assert analysis.hook_seconds == 1.2
    assert analysis.video_format == "POV compilation"
    assert analysis.cuts_per_minute == 34.0
    assert analysis.caption_style.startswith("Bold white")
    assert analysis.why_it_performs
    assert analysis.analysed is True


def test_unlabelled_scenes_and_blank_drivers_are_dropped():
    analysis = parse_analysis(FULL_PAYLOAD, AnalysisDepth.FULL)
    assert [s.label for s in analysis.scenes] == ["cold open", "escalation"]
    assert analysis.engagement_drivers == [
        "unresolved outcome", "comment-bait ending"]


def test_nulls_stay_null_rather_than_becoming_zero():
    """A zero hook score is a judgement; null means nobody made one."""
    analysis = parse_analysis(
        {"hook": "x", "hook_strength": None, "cuts_per_minute": "null"},
        AnalysisDepth.METADATA)
    assert analysis.hook_strength is None
    assert analysis.cuts_per_minute is None


def test_junk_from_the_model_does_not_crash_the_parse():
    analysis = parse_analysis(
        {"hook": 42, "scenes": "not a list", "engagement_drivers": {"a": 1},
         "hook_strength": "very high"},
        AnalysisDepth.METADATA)
    assert analysis.hook == "42"
    assert analysis.scenes == []
    assert analysis.engagement_drivers == []
    assert analysis.hook_strength is None


def test_an_empty_response_is_not_analysed():
    assert parse_analysis({}, AnalysisDepth.METADATA).analysed is False


def test_a_metadata_reading_says_the_video_was_not_copied():
    analysis = parse_analysis(FULL_PAYLOAD, AnalysisDepth.METADATA)
    assert any("not copied or downloaded" in n for n in analysis.notes)


def test_an_unanalysed_card_says_so():
    record = summarise_for_ui(VideoAnalysis())
    assert any("not been analysed" in n for n in record["notes"])


# --- the analysis feeds the forecast --------------------------------------

def test_the_reading_drives_the_forecast_inputs():
    analysis = parse_analysis(FULL_PAYLOAD, AnalysisDepth.FULL)
    inputs = to_new_version_inputs(analysis, length_fit=0.8, is_reused_format=False)
    assert inputs.hook_strength == 0.9
    assert inputs.retention_potential == 0.8
    assert inputs.topic_freshness == 0.4
    assert inputs.length_fit == 0.8


def test_unestablished_readings_are_not_invented():
    """A shallow reading must not masquerade as a confident one."""
    shallow = parse_analysis({"hook": "cold open"}, AnalysisDepth.METADATA)
    default = to_new_version_inputs(shallow)
    from viral.scoring import NewVersionInputs

    assert default.hook_strength == NewVersionInputs().hook_strength


def test_out_of_range_scores_are_clamped():
    analysis = parse_analysis(
        {"hook": "x", "hook_strength": 7.5, "retention_potential": -2},
        AnalysisDepth.FULL)
    inputs = to_new_version_inputs(analysis)
    assert inputs.hook_strength == 1.0
    assert inputs.retention_potential == 0.0


def test_a_strong_reading_raises_the_forecast_over_a_weak_one():
    original = viral_score(
        VideoMetrics(views=2_400_000, likes=180_000, comments=9_000,
                     followers=50_000, platform="youtube"), now=NOW)
    strong = new_version_probability(
        original,
        to_new_version_inputs(parse_analysis(FULL_PAYLOAD, AnalysisDepth.FULL),
                              is_reused_format=False))
    weak = new_version_probability(
        original,
        to_new_version_inputs(
            parse_analysis({**FULL_PAYLOAD, "hook_strength": 0.1,
                            "retention_potential": 0.1, "topic_freshness": 0.1},
                           AnalysisDepth.FULL),
            is_reused_format=False))
    assert strong.score > weak.score


# --- running it, with the model injected -----------------------------------

def test_a_discovered_video_never_reaches_the_vision_path(tmp_path):
    """Even with a local file present, no rights means no frame analysis."""
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"not really a video")
    calls = {"metadata": 0, "vision": 0}

    async def model_call(prompt, **kwargs):
        calls["metadata"] += 1
        return FULL_PAYLOAD

    async def vision_call(prompt, images, **kwargs):  # pragma: no cover
        calls["vision"] += 1
        raise AssertionError("analysed a discovered video's frames")

    analysis = asyncio.run(analyse(
        VIDEO,
        attestation=attest(source=Source.DISCOVERED, user_id=USER, now=NOW),
        source_path=fake_video,
        model_call=model_call, vision_call=vision_call,
    ))
    assert calls == {"metadata": 1, "vision": 0}
    assert analysis.depth is AnalysisDepth.METADATA


def test_owned_footage_falls_back_to_metadata_when_no_frames_can_be_read(tmp_path):
    """A file ffmpeg cannot decode must not silently produce a fake reading."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 64)

    async def model_call(prompt, **kwargs):
        return FULL_PAYLOAD

    analysis = asyncio.run(analyse(
        {**VIDEO, "duration_seconds": 41.0},
        attestation=attest(source=Source.OWN_RECORDING, user_id=USER, now=NOW),
        source_path=broken, model_call=model_call,
    ))
    assert analysis.depth is AnalysisDepth.METADATA


def test_no_source_file_means_metadata_depth():
    async def model_call(prompt, **kwargs):
        return FULL_PAYLOAD

    analysis = asyncio.run(analyse(
        VIDEO,
        attestation=attest(source=Source.OWN_RECORDING, user_id=USER, now=NOW),
        model_call=model_call,
    ))
    assert analysis.depth is AnalysisDepth.METADATA
    assert analysis.hook
