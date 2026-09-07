"""Tests for the Match Analysis footage layer.

The source-policy tests are the important ones: they are what stops a future
change from turning this module into a broadcast-footage scraper.
"""

import pytest

from core.footage import (
    MAX_CLIP_SECONDS,
    MIN_CLIP_SECONDS,
    ClipWindow,
    FootageAvailability,
    MatchMoment,
    clip_window_for,
    discover_local_footage,
    is_allowed_source,
    plan_moment_clips,
    resolve_availability,
)


# --- source policy ---------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "http://youtu.be/abc",
        "https://twitter.com/x/status/1",
        "https://x.com/x/status/1",
        "https://www.espn.com/highlights.mp4",
        "https://skysports.com/clip.mp4",
        "https://beinsports.com/clip.mp4",
        "https://cdn.example.com/broadcast.mp4",
        "//cdn.example.com/broadcast.mp4",
        "https://t.me/somechannel/12",
        "https://streamable.com/abc",
    ],
)
def test_remote_and_broadcast_sources_are_refused(url):
    assert is_allowed_source(url) is False


def test_licensed_provider_identifier_is_allowed():
    assert is_allowed_source("pexels") is True


def test_local_path_is_allowed_only_when_it_exists(tmp_path):
    real = tmp_path / "match.mp4"
    real.write_bytes(b"\0")
    assert is_allowed_source(str(real)) is True
    assert is_allowed_source(str(tmp_path / "missing.mp4")) is False


def test_empty_source_is_refused():
    assert is_allowed_source("") is False
    assert is_allowed_source("   ") is False


def test_a_local_path_that_mentions_a_forbidden_host_is_still_refused(tmp_path):
    # A file literally named after a broadcaster must not become a loophole.
    sneaky = tmp_path / "youtube.com_download.mp4"
    sneaky.write_bytes(b"\0")
    assert is_allowed_source(str(sneaky)) is False


# --- discovery -------------------------------------------------------------

def test_discovery_returns_nothing_without_a_directory(tmp_path):
    assert discover_local_footage(None) == []
    assert discover_local_footage(tmp_path / "nope") == []


def test_discovery_finds_only_video_files(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"\0")
    (tmp_path / "b.mov").write_bytes(b"\0")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "cover.png").write_bytes(b"\0")
    found = discover_local_footage(tmp_path)
    assert [p.name for p in found] == ["a.mp4", "b.mov"]


def test_availability_explains_itself(tmp_path):
    empty = resolve_availability(footage_dir=tmp_path, allow_licensed_stock=False)
    assert empty.has_local is False
    assert "photos only" in empty.reason

    (tmp_path / "match.mp4").write_bytes(b"\0")
    have = resolve_availability(footage_dir=tmp_path, allow_licensed_stock=False)
    assert have.has_local is True
    assert "operator-provided" in have.reason


# --- clip windows ----------------------------------------------------------

def _moment(offset):
    return MatchMoment(label="goal", minute=23, description="x",
                       footage_offset_seconds=offset)


def test_window_takes_lead_in_and_reaction_around_the_event():
    start, duration = clip_window_for(_moment(600.0), source_duration=3000.0)
    assert start == pytest.approx(595.0)          # 5s before
    assert MIN_CLIP_SECONDS <= duration <= MAX_CLIP_SECONDS
    assert start + duration > 600.0               # covers the event itself


def test_window_never_starts_before_the_file():
    start, duration = clip_window_for(_moment(2.0), source_duration=3000.0)
    assert start == 0.0
    assert duration <= MAX_CLIP_SECONDS


def test_window_is_clamped_to_the_end_of_the_file():
    start, duration = clip_window_for(_moment(1795.0), source_duration=1800.0)
    assert start + duration <= 1800.0 + 1e-6
    assert duration >= MIN_CLIP_SECONDS


def test_window_is_none_without_a_footage_offset():
    assert clip_window_for(_moment(None), source_duration=3000.0) is None


def test_window_is_none_when_the_file_is_too_short():
    assert clip_window_for(_moment(2.0), source_duration=6.0) is None


def test_window_is_none_for_an_unreadable_source():
    assert clip_window_for(_moment(600.0), source_duration=0.0) is None


# --- planning and fallback -------------------------------------------------

def test_no_local_footage_plans_no_clips_rather_than_failing():
    moments = [_moment(600.0), _moment(1200.0)]
    availability = FootageAvailability(local_clips=[], licensed_enabled=True)
    assert plan_moment_clips(moments, availability) == []


def test_moments_without_offsets_are_skipped_not_fatal(tmp_path, monkeypatch):
    import core.footage as footage

    source = tmp_path / "match.mp4"
    source.write_bytes(b"\0")
    monkeypatch.setattr(footage, "probe_duration", lambda p: 3000.0)

    moments = [
        MatchMoment("goal", 10, "a", footage_offset_seconds=600.0),
        MatchMoment("red_card", 70, "b", footage_offset_seconds=None),
    ]
    windows = plan_moment_clips(
        moments, FootageAvailability(local_clips=[source])
    )
    assert len(windows) == 1
    assert windows[0].moment.label == "goal"
    assert isinstance(windows[0], ClipWindow)
    assert windows[0].output_name == "moment_01_goal.mp4"
