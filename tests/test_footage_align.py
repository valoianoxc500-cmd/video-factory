"""Clock alignment: match minute -> position in the operator's footage."""

import json

import pytest

from core.footage_align import (
    FootageAlignment,
    load_alignment,
    minute_to_video_seconds,
    sidecar_path,
)


def _footage(tmp_path, name="match.mp4"):
    p = tmp_path / name
    p.write_bytes(b"\0")
    return p


def _write_sidecar(footage, data, *, alt=False):
    path = (
        footage.with_suffix(".align.json") if alt else sidecar_path(footage)
    )
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- loading ---------------------------------------------------------------

def test_missing_sidecar_yields_no_alignment(tmp_path):
    assert load_alignment(_footage(tmp_path)) is None


def test_sidecar_next_to_the_file_is_accepted(tmp_path):
    f = _footage(tmp_path)
    _write_sidecar(f, {"kickoff_offset_seconds": 120.0})
    a = load_alignment(f)
    assert a is not None and a.kickoff_offset_seconds == 120.0


def test_hand_written_sidecar_name_is_also_accepted(tmp_path):
    # "match.align.json" is what people actually type, not "match.mp4.align.json".
    f = _footage(tmp_path)
    _write_sidecar(f, {"kickoff_offset_seconds": 60.0}, alt=True)
    assert load_alignment(f) is not None


def test_sidecar_without_kickoff_is_refused(tmp_path):
    f = _footage(tmp_path)
    _write_sidecar(f, {"second_half_kickoff_seconds": 3000})
    assert load_alignment(f) is None


def test_unparseable_or_negative_kickoff_is_refused(tmp_path):
    f = _footage(tmp_path)
    _write_sidecar(f, {"kickoff_offset_seconds": "soon"})
    assert load_alignment(f) is None
    _write_sidecar(f, {"kickoff_offset_seconds": -5})
    assert load_alignment(f) is None


def test_corrupt_sidecar_is_refused_not_raised(tmp_path):
    f = _footage(tmp_path)
    sidecar_path(f).write_text("{not json", encoding="utf-8")
    assert load_alignment(f) is None


def test_second_half_before_kickoff_is_discarded(tmp_path):
    f = _footage(tmp_path)
    _write_sidecar(
        f,
        {"kickoff_offset_seconds": 300.0, "second_half_kickoff_seconds": 100.0},
    )
    a = load_alignment(f)
    assert a is not None
    assert a.can_align_second_half is False


# --- minute mapping --------------------------------------------------------

def _align(tmp_path, kickoff=120.0, second_half=None, duration=7200.0):
    return FootageAlignment(
        source=_footage(tmp_path),
        kickoff_offset_seconds=kickoff,
        second_half_kickoff_seconds=second_half,
        duration_seconds=duration,
    )


def test_first_half_minute_is_measured_from_kickoff(tmp_path):
    a = _align(tmp_path, kickoff=132.0)
    assert minute_to_video_seconds(0, a) == pytest.approx(132.0)
    assert minute_to_video_seconds(23, a) == pytest.approx(132.0 + 23 * 60)


def test_video_time_is_not_match_time(tmp_path):
    # The whole point of the module: a 23' goal is not at 23:00 in the file.
    a = _align(tmp_path, kickoff=132.0)
    assert minute_to_video_seconds(23, a) != pytest.approx(23 * 60)


def test_second_half_absorbs_the_half_time_break(tmp_path):
    a = _align(tmp_path, kickoff=120.0, second_half=3600.0)
    # 60' is 15 minutes into the second half, not 60 minutes after kickoff.
    assert minute_to_video_seconds(60, a) == pytest.approx(3600.0 + 15 * 60)
    naive = 120.0 + 60 * 60
    assert minute_to_video_seconds(60, a) != pytest.approx(naive)


def test_second_half_minute_is_refused_without_its_offset(tmp_path):
    a = _align(tmp_path, kickoff=120.0, second_half=None)
    assert minute_to_video_seconds(60, a) is None
    # First-half moments still work, so the video is partly footage-backed.
    assert minute_to_video_seconds(20, a) is not None


def test_boundary_minute_belongs_to_the_first_half(tmp_path):
    a = _align(tmp_path, kickoff=0.0, second_half=3000.0)
    assert minute_to_video_seconds(45, a) == pytest.approx(45 * 60)
    assert minute_to_video_seconds(46, a) == pytest.approx(3000.0 + 60)


def test_position_past_the_end_of_the_file_is_refused(tmp_path):
    a = _align(tmp_path, kickoff=0.0, duration=600.0)
    assert minute_to_video_seconds(90, a) is None


def test_negative_minute_is_refused(tmp_path):
    assert minute_to_video_seconds(-1, _align(tmp_path)) is None
