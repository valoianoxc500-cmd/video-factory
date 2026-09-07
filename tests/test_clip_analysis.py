"""Clip analysis: the declared path, the honest-refusal path, and the gate."""

import json

import pytest

import core.clip_analysis as ca
import core.footage_stage as stage


def _clip(tmp_path, name="match.mp4"):
    p = tmp_path / name
    p.write_bytes(b"\0")
    return p


def _sidecar(clip, data, *, bom=False, alt=False):
    path = clip.with_suffix(".align.json") if alt else clip.with_suffix(
        clip.suffix + ".align.json"
    )
    text = json.dumps(data)
    path.write_bytes(("﻿" if bom else "") + text and
                     (("﻿" if bom else "") + text).encode("utf-8"))
    return path


# --- declared events -------------------------------------------------------

def test_declared_single_event_is_read(tmp_path):
    c = _clip(tmp_path)
    _sidecar(c, {"event_offset_seconds": 5.0, "event_label": "goal"})
    result = ca.read_declared_events(c)
    assert result.ok
    assert result.events[0].seconds == 5.0
    assert result.events[0].label == "goal"
    assert result.events[0].confidence == 1.0


def test_sidecar_written_with_a_bom_is_still_read(tmp_path):
    # Notepad and PowerShell's -Encoding utf8 both prepend a BOM. Reading as
    # plain utf-8 silently drops the declaration and falls through to a guess.
    c = _clip(tmp_path)
    _sidecar(c, {"event_offset_seconds": 5.0}, bom=True)
    assert ca.read_declared_events(c).events[0].seconds == 5.0


def test_multiple_declared_events_are_sorted(tmp_path):
    c = _clip(tmp_path)
    _sidecar(c, {"events": [{"seconds": 30}, {"seconds": 5, "label": "goal"}]})
    result = ca.read_declared_events(c)
    assert [e.seconds for e in result.events] == [5.0, 30.0]


def test_full_match_sidecar_declares_no_clip_events(tmp_path):
    # kickoff_offset_seconds belongs to the alignment system, not clip mode.
    c = _clip(tmp_path)
    _sidecar(c, {"kickoff_offset_seconds": 0})
    assert ca.read_declared_events(c).ok is False


def test_unparseable_or_negative_timestamps_are_dropped(tmp_path):
    c = _clip(tmp_path)
    _sidecar(c, {"events": [{"seconds": "soon"}, {"seconds": -3}]})
    assert ca.read_declared_events(c).ok is False


def test_alternate_sidecar_name_is_accepted(tmp_path):
    c = _clip(tmp_path)
    _sidecar(c, {"event_offset_seconds": 7.5}, alt=True)
    assert ca.read_declared_events(c).events[0].seconds == 7.5


# --- audio energy honesty --------------------------------------------------

def test_flat_audio_reports_no_event_rather_than_the_loudest_second(monkeypatch, tmp_path):
    """Measured on a real goal clip, a 7 dB peak was the WRONG moment."""
    c = _clip(tmp_path)
    # A gently varying profile: nothing 12 dB above baseline.
    series = [(i * 0.5, -30 + (i % 9)) for i in range(200)]
    monkeypatch.setattr(ca, "loudness_series", lambda p: series)
    result = ca.detect_by_audio_energy(c)
    assert result.ok is False
    assert "no crowd reaction" in result.reason


def test_an_emphatic_roar_is_detected(monkeypatch, tmp_path):
    c = _clip(tmp_path)
    series = [(i * 0.5, -40.0) for i in range(200)]
    for i in range(100, 120):                 # ~10s, 25 dB above baseline
        series[i] = (i * 0.5, -15.0)
    monkeypatch.setattr(ca, "loudness_series", lambda p: series)
    result = ca.detect_by_audio_energy(c)
    assert result.ok
    assert 45 <= result.events[0].seconds <= 62
    assert result.events[0].confidence >= ca.MIN_CONFIDENCE


def test_silent_or_unreadable_audio_reports_no_event(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "loudness_series", lambda p: [])
    assert ca.detect_by_audio_energy(_clip(tmp_path)).ok is False


# --- provider precedence and the vision switch -----------------------------

def test_declared_wins_over_audio(monkeypatch, tmp_path):
    c = _clip(tmp_path)
    _sidecar(c, {"event_offset_seconds": 5.0})
    monkeypatch.setattr(
        ca, "detect_by_audio_energy",
        lambda p: pytest.fail("audio must not run when a timestamp is declared"),
    )
    result = ca.analyse_clip(c)
    assert result.detector == "declared"
    assert result.events[0].seconds == 5.0


def test_vision_is_off_unless_its_variable_is_set(monkeypatch):
    monkeypatch.delenv(ca.VISION_ENV, raising=False)
    assert ca.vision_enabled() is False
    monkeypatch.setenv(ca.VISION_ENV, "1")
    assert ca.vision_enabled() is True


def test_analysis_fails_closed_when_nothing_is_confident(monkeypatch, tmp_path):
    c = _clip(tmp_path)
    monkeypatch.delenv(ca.VISION_ENV, raising=False)
    monkeypatch.setattr(ca, "loudness_series", lambda p: [])
    result = ca.analyse_clip(c)
    assert result.ok is False
    assert ca.VISION_ENV in result.reason


def test_missing_file_is_reported_not_raised(tmp_path):
    assert ca.analyse_clip(tmp_path / "nope.mp4").ok is False


# --- the generation gate ---------------------------------------------------

def test_gate_passes_when_no_footage_is_configured():
    ok, reason = stage.analysis_gate(None)
    assert ok and "photo pipeline" in reason


def test_gate_passes_on_an_empty_footage_directory(tmp_path):
    ok, _ = stage.analysis_gate(tmp_path)
    assert ok


def test_gate_passes_when_a_clip_declares_its_event(tmp_path):
    c = _clip(tmp_path)
    _sidecar(c, {"event_offset_seconds": 5.0})
    ok, reason = stage.analysis_gate(tmp_path)
    assert ok and "declared" in reason


def test_gate_passes_for_a_full_match_recording(tmp_path):
    c = _clip(tmp_path)
    _sidecar(c, {"kickoff_offset_seconds": 0})
    ok, reason = stage.analysis_gate(tmp_path)
    assert ok and "alignment" in reason


def test_gate_fails_when_footage_cannot_be_analysed(tmp_path, monkeypatch):
    _clip(tmp_path)                       # footage present, no sidecar
    monkeypatch.delenv(ca.VISION_ENV, raising=False)
    monkeypatch.setattr(ca, "loudness_series", lambda p: [])
    ok, reason = stage.analysis_gate(tmp_path)
    assert ok is False
    assert "match.mp4" in reason
