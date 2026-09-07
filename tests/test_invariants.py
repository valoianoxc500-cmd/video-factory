"""Invariant tests against real pipeline output.

These tests validate workspace artifacts produced by actual pipeline runs.
All tests skip if no workspace exists for the channel.
"""

import pytest

from core.validator import (
    validate_audio,
    validate_ready_images,
    validate_script,
    validate_thumbnail,
    validate_video,
)


def test_script_structure(workspace_script):
    errors = validate_script(workspace_script)
    assert errors == [], errors


def test_all_images_present(
    latest_workspace, workspace_script, channel_config, require_stage
):
    require_stage("process")
    errors = validate_ready_images(latest_workspace, workspace_script, channel_config)
    assert errors == [], errors


def test_audio_sections_match_script(
    latest_workspace, workspace_script, require_stage
):
    require_stage("audio_source")
    errors = validate_audio(latest_workspace, workspace_script)
    assert errors == [], errors


def test_thumbnail_dimensions(latest_workspace, require_stage, channel_config):
    """Expected dimensions are per-channel: a 9:16 channel publishes a vertical
    thumbnail, so checking against a fixed 1280x720 fails a correct one."""
    require_stage("thumbnail")
    thumb = latest_workspace / "thumbnail.png"
    if not thumb.exists():
        pytest.skip("No thumbnail in workspace")
    errors = validate_thumbnail(thumb, channel_config)
    assert errors == [], errors


def test_video_has_correct_streams(latest_workspace, channel_config, require_stage):
    require_stage("assemble")
    videos = list(latest_workspace.glob("*.mp4"))
    if not videos:
        pytest.skip("No video in workspace")
    errors = validate_video(videos[0], channel_config)
    # Filter to only stream/codec errors for this test
    stream_errors = [e for e in errors if "stream" in e or "codec" in e or "format" in e]
    assert stream_errors == [], stream_errors


def test_video_resolution_matches_config(latest_workspace, channel_config, require_stage):
    require_stage("assemble")
    videos = list(latest_workspace.glob("*.mp4"))
    if not videos:
        pytest.skip("No video in workspace")
    errors = validate_video(videos[0], channel_config)
    res_errors = [e for e in errors if "Resolution" in e]
    assert res_errors == [], res_errors


def test_video_has_no_decode_errors(latest_workspace, channel_config, require_stage):
    require_stage("assemble")
    videos = list(latest_workspace.glob("*.mp4"))
    if not videos:
        pytest.skip("No video in workspace")
    errors = validate_video(videos[0], channel_config)
    decode_errors = [e for e in errors if "Decode" in e]
    assert decode_errors == [], decode_errors
