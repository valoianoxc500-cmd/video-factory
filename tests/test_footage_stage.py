"""Footage stage: narration sync, clip placement, and the photo fallback."""

import json
from dataclasses import dataclass

import pytest

import core.footage_stage as stage
from core.footage import MatchMoment


@dataclass
class FakeSection:
    id: int
    narration: str


@dataclass
class FakeScript:
    sections: list


def _moments():
    return [
        MatchMoment("goal", 23, "A scores"),
        MatchMoment("red_card", 61, "B sent off"),
    ]


# --- narration sync --------------------------------------------------------

def test_sections_are_matched_by_the_minute_they_name():
    script = FakeScript([
        FakeSection(1, "The opening exchanges were cagey."),
        FakeSection(2, "In the 61st minute B was sent off."),
        FakeSection(3, "The goal arrived in minute 23."),
    ])
    assigned = stage.match_moments_to_sections(_moments(), script.sections)
    assert assigned[2].minute == 61
    assert assigned[3].minute == 23


def test_arabic_digits_are_read_as_minutes():
    script = FakeScript([FakeSection(1, "في الدقيقة ٢٣ سجل اللاعب هدفه")])
    assigned = stage.match_moments_to_sections(_moments(), script.sections)
    assert assigned[1].minute == 23


def test_unclaimed_moments_fall_back_to_match_order():
    script = FakeScript([FakeSection(1, "No minutes here"), FakeSection(2, "Nor here")])
    assigned = stage.match_moments_to_sections(_moments(), script.sections)
    assert [assigned[1].minute, assigned[2].minute] == [23, 61]


def test_a_moment_is_never_assigned_to_two_sections():
    script = FakeScript([
        FakeSection(1, "minute 23 again"),
        FakeSection(2, "minute 23 once more"),
    ])
    assigned = stage.match_moments_to_sections([MatchMoment("goal", 23, "x")],
                                               script.sections)
    assert list(assigned.values()).count(assigned[1]) == 1


def test_implausible_numbers_are_not_treated_as_minutes():
    # A scoreline or a year must not be read as a match minute.
    assert 2026 not in stage._minutes_in("in 2026 the score was 3-1")
    assert 3 in stage._minutes_in("in 2026 the score was 3-1")


# --- fallback guarantees ---------------------------------------------------

def _build(tmp_path, **over):
    kwargs = dict(
        workspace=tmp_path,
        script=FakeScript([FakeSection(1, "minute 23")]),
        moments=_moments(),
        footage_dir=None,
        allow_licensed_stock=False,
    )
    kwargs.update(over)
    return stage.build_match_clips(**kwargs)


def test_no_moments_produces_no_clips(tmp_path):
    assert _build(tmp_path, moments=[]) == []


def test_no_footage_directory_falls_back_to_photos(tmp_path):
    assert _build(tmp_path) == []


def test_empty_footage_directory_falls_back_to_photos(tmp_path):
    empty = tmp_path / "footage"
    empty.mkdir()
    assert _build(tmp_path, footage_dir=empty) == []


def test_footage_without_an_alignment_sidecar_falls_back_to_photos(tmp_path):
    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "match.mp4").write_bytes(b"\0")
    # Present but unaligned: placing clips would be guesswork, so photos win.
    assert _build(tmp_path, footage_dir=footage) == []


def test_second_half_moments_are_skipped_when_only_kickoff_is_known(
    tmp_path, monkeypatch
):
    footage = tmp_path / "footage"
    footage.mkdir()
    source = footage / "match.mp4"
    source.write_bytes(b"\0")
    (footage / "match.mp4.align.json").write_text(
        json.dumps({"kickoff_offset_seconds": 0.0}), encoding="utf-8"
    )
    monkeypatch.setattr(stage, "probe_duration", lambda p: 7200.0)

    cut_calls = {}

    def fake_cut(windows, out_dir, **kw):
        cut_calls["windows"] = windows
        out_dir.mkdir(parents=True, exist_ok=True)
        made = []
        for w in windows:
            f = out_dir / w.output_name
            f.write_bytes(b"\0")
            made.append(f)
        return made

    monkeypatch.setattr(stage, "cut_clips", fake_cut)

    script = FakeScript([FakeSection(1, "minute 23"), FakeSection(2, "minute 61")])
    placements = _build(tmp_path, footage_dir=footage, script=script)

    # 23' is placeable; 61' is in the unaligned second half and must not be.
    assert [p.moment.minute for p in placements] == [23]
    assert [w.moment.minute for w in cut_calls["windows"]] == [23]


def test_placed_clips_are_named_for_the_slot_the_renderer_looks_for(
    tmp_path, monkeypatch
):
    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "match.mp4").write_bytes(b"\0")
    (footage / "match.mp4.align.json").write_text(
        json.dumps(
            {"kickoff_offset_seconds": 0.0, "second_half_kickoff_seconds": 3000.0}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stage, "probe_duration", lambda p: 7200.0)

    def fake_cut(windows, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        made = []
        for w in windows:
            f = out_dir / w.output_name
            f.write_bytes(b"\0")
            made.append(f)
        return made

    monkeypatch.setattr(stage, "cut_clips", fake_cut)

    script = FakeScript([FakeSection(7, "minute 23"), FakeSection(9, "minute 61")])
    placements = _build(tmp_path, footage_dir=footage, script=script)

    names = {p.clip_path.name for p in placements}
    assert names == {"section_007_01.mp4", "section_009_01.mp4"}
    for p in placements:
        assert p.clip_path.parent == tmp_path / "videos" / "raw"
        assert p.clip_path.exists()

    manifest = json.loads((tmp_path / "footage_manifest.json").read_text("utf-8"))
    assert len(manifest["clips"]) == 2
    assert manifest["licensed_stock_enabled"] is False
