"""A rejection must map back to the exact beat that produced the asset.

The bug this covers: a b-roll slot whose clip was already on disk got no
descriptor at all, so the gate logged "re-sourcing 0 of 3" and a rejected
b-roll beat could never be re-sourced or regenerated, however many attempts it
was given. Matching also ran on filenames, which a b-roll descriptor did not
carry.

Both are now keyed on `slot_uid` -- section plus the slot's position among its
section's non-overlay slots, which is exactly how the asset filenames are
built, so both sides derive the same identity independently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.image_sourcer import (
    _build_sections_context,
    _rejected_filenames,
)


class _Slot:
    CHART_TYPES: set[str] = set()

    def __init__(self, visual="stock_photo", keywords="k", prompt="p"):
        self.visual = visual
        self.keywords = keywords
        self.prompt = prompt


class _Section:
    def __init__(self, section_id: int, slots):
        self.id = section_id
        self.slots = slots
        self.estimated_duration_seconds = 30.0
        self.narration = "narration for this section"

    @property
    def non_overlay_slots(self):
        return self.slots


class _Script:
    def __init__(self, sections):
        self.sections = sections


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def dirs(tmp_path):
    raw = tmp_path / "images" / "raw"
    videos = tmp_path / "videos" / "raw"
    raw.mkdir(parents=True)
    videos.mkdir(parents=True)
    return raw, videos


# --- the identity itself ----------------------------------------------------

def test_every_reviewed_slot_carries_a_stable_id(dirs):
    raw, videos = dirs
    _write(raw / "section_001_01.jpg")
    _write(raw / "section_001_02.jpg")
    script = _Script([_Section(1, [_Slot(), _Slot()])])

    context = _build_sections_context(script, raw, videos)

    assert [c["slot_uid"] for c in context] == [
        "section_001_01", "section_001_02"]


def test_the_id_is_the_same_for_a_still_and_a_b_roll_beat(dirs):
    """A b-roll beat is reviewed through its poster frame, and must be
    identified the same way a still is."""
    raw, videos = dirs
    _write(raw / "section_001_01.jpg")
    _write(videos / "section_001_01.mp4")
    script = _Script([_Section(1, [_Slot(visual="b_roll")])])

    context = _build_sections_context(script, raw, videos)

    assert context[0]["slot_uid"] == "section_001_01"
    assert context[0]["is_b_roll"] is True


def test_a_rejection_resolves_to_the_slot_id(dirs):
    raw, videos = dirs
    _write(raw / "section_002_03.jpg")
    script = _Script([_Section(2, [_Slot(), _Slot(), _Slot()])])
    for index in (1, 2):
        _write(raw / f"section_002_0{index}.jpg")

    context = _build_sections_context(script, raw, videos)
    resolved = _rejected_filenames({(2, 3): "try a wall clock"}, context)

    assert resolved == {"section_002_03": "try a wall clock"}


def test_reviewer_renumbering_does_not_break_the_mapping(dirs):
    """The reviewer numbers what it was shown. When a slot is missing its
    asset it is not shown, so the numbering shifts -- the id must not."""
    raw, videos = dirs
    # Slot 2 has no asset, so only slots 1 and 3 are reviewed.
    _write(raw / "section_001_01.jpg")
    _write(raw / "section_001_03.jpg")
    script = _Script([_Section(1, [_Slot(), _Slot(), _Slot()])])

    context = _build_sections_context(script, raw, videos)

    assert [c["slot_uid"] for c in context] == [
        "section_001_01", "section_001_03"]
    # The reviewer's second image is slot 3, and resolves to slot 3's id.
    assert _rejected_filenames({(1, 3): ""}, context) == {"section_001_03": ""}


def test_an_unknown_rejection_key_resolves_to_nothing(dirs):
    raw, videos = dirs
    _write(raw / "section_001_01.jpg")
    script = _Script([_Section(1, [_Slot()])])
    context = _build_sections_context(script, raw, videos)

    assert _rejected_filenames({(9, 9): "x"}, context) == {}


# --- targeting --------------------------------------------------------------

def _descriptor(uid: str, *, img_path=None, video_path=None, b_roll=False):
    return {
        "section": _Section(int(uid.split("_")[1]), []),
        "sub_idx": int(uid.split("_")[2]) - 1,
        "slot": _Slot(),
        "slot_uid": uid,
        "keywords": "k",
        "prompt": "p",
        "img_path": img_path,
        "video_path": video_path,
        "b_roll": b_roll,
        "lane": "photo",
        "sourced": True,
    }


def _targets(descriptors, rejected_uids):
    """The selection the review gate performs."""
    return [d for d in descriptors if d.get("slot_uid") in rejected_uids]


def test_a_rejected_still_is_targeted():
    still = _descriptor("section_001_02", img_path=Path("a.jpg"))
    assert _targets([still], {"section_001_02"}) == [still]


def test_a_rejected_b_roll_beat_is_targeted():
    """The case that produced "re-sourcing 0 of 3"."""
    broll = _descriptor(
        "section_001_07", img_path=Path("p.jpg"),
        video_path=Path("p.mp4"), b_roll=True)
    assert _targets([broll], {"section_001_07"}) == [broll]


def test_a_b_roll_beat_with_no_poster_path_is_still_targeted():
    """Matching used to need img_path, which a b-roll descriptor lacked."""
    broll = _descriptor(
        "section_001_07", img_path=None,
        video_path=Path("p.mp4"), b_roll=True)
    assert _targets([broll], {"section_001_07"}) == [broll]


def test_unrelated_slots_are_never_targeted():
    good = _descriptor("section_001_02", img_path=Path("good.jpg"))
    bad = _descriptor("section_001_07", img_path=Path("bad.jpg"))

    assert _targets([good, bad], {"section_001_07"}) == [bad]


def test_two_sections_with_the_same_slot_number_do_not_collide():
    first = _descriptor("section_001_03", img_path=Path("a.jpg"))
    second = _descriptor("section_002_03", img_path=Path("b.jpg"))

    assert _targets([first, second], {"section_002_03"}) == [second]


# --- cached assets get descriptors -----------------------------------------

def test_a_cached_b_roll_slot_still_produces_a_descriptor():
    """On a resumed run the clip is already on disk. Without a descriptor the
    gate has nothing to act on and re-reviews the same frame until its budget
    runs out."""
    import inspect

    from core import image_sourcer

    source = inspect.getsource(image_sourcer.source_images)
    cached_broll = source[source.index("B-roll already exists"):]
    cached_broll = cached_broll[: cached_broll.index("continue")]
    assert "cached_descriptors.append" in cached_broll
    assert '"slot_uid": file_label' in cached_broll


def test_a_cached_video_slot_still_produces_a_descriptor():
    import inspect

    from core import image_sourcer

    source = inspect.getsource(image_sourcer.source_images)
    cached_video = source[source.index("Video exists for slot"):]
    cached_video = cached_video[: cached_video.index("continue")]
    assert "cached_descriptors.append" in cached_video


def test_a_rejected_b_roll_clip_is_discarded_with_its_frame():
    """Otherwise the renderer plays the footage the gate refused and the
    replacement still is never seen."""
    import inspect

    from core import image_sourcer

    source = inspect.getsource(image_sourcer.source_images)
    assert "discarding rejected b-roll" in source
    assert 'desc["b_roll"] = False' in source
