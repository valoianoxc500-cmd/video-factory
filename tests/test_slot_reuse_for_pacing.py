"""A section short on sourced images re-shows one rather than holding it.

On a web-photo-only channel a slot that no real photograph can satisfy is
dropped rather than filled with a wrong or generated image. Lose enough and the
survivors must cover the section between them:

    Section 1 exceeds the max visual hold of 5.0s. Slot 1 (google_photo) would
    stay on screen for 6.82s across a 26.37s section with 4 slots.

Cutting back to an image already used in the section keeps every frame a real,
already-reviewed photograph and keeps every beat under the cap.
"""

import pytest

from core.render_sections import _reuse_slots_to_hold_the_cap, _validate_max_visual_hold
from core.utils import (
    ScriptSection,
    VisualSlot,
    compute_sub_durations,
    minimum_visual_slots_for_duration,
)

MAX_HOLD = 5.0
CROSSFADE = 0.3


def _section(duration: float, n_slots: int) -> ScriptSection:
    return ScriptSection(
        id=1,
        narration="n",
        actual_duration_seconds=duration,
        slots=[
            VisualSlot(visual="google_photo", keywords=f"subject {i}", prompt=f"p{i}")
            for i in range(n_slots)
        ],
    )


# --- the exact failure -----------------------------------------------------

def test_the_failing_section_now_holds_the_cap():
    section = _section(26.37, 4)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)

    durations = compute_sub_durations(
        section, section.sub_slot_count, CROSSFADE,
        max_visual_hold_seconds=MAX_HOLD,
    )
    _validate_max_visual_hold(
        section, durations, max_seconds=MAX_HOLD, crossfade=CROSSFADE
    )
    assert max(durations) <= MAX_HOLD + 1e-9


def test_enough_beats_are_added():
    section = _section(26.37, 4)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    assert len(section.non_overlay_slots) >= minimum_visual_slots_for_duration(
        26.37, MAX_HOLD, CROSSFADE
    )


# --- only real, already-reviewed images are used --------------------------

def test_no_new_subject_is_invented():
    """Every beat must still be one of the images that was actually sourced."""
    section = _section(26.37, 4)
    originals = {s.keywords for s in section.non_overlay_slots}
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    assert {s.keywords for s in section.non_overlay_slots} == originals


def test_a_repeat_never_sits_next_to_itself():
    """Two identical beats in a row read as a stutter, not a return."""
    section = _section(26.37, 4)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    keys = [s.keywords for s in section.non_overlay_slots]
    assert all(a != b for a, b in zip(keys, keys[1:])), keys


def test_every_image_is_shown_before_any_is_repeated():
    section = _section(26.37, 4)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    keys = [s.keywords for s in section.non_overlay_slots]
    assert keys[:4] == [f"subject {i}" for i in range(4)]


def test_a_single_sourced_image_still_reaches_the_cap():
    """With one image there is no way to avoid consecutive repeats -- the cap
    still has to be met, and a repeated real photo beats an overlong hold."""
    section = _section(13.57, 1)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    durations = compute_sub_durations(
        section, section.sub_slot_count, CROSSFADE,
        max_visual_hold_seconds=MAX_HOLD,
    )
    assert max(durations) <= MAX_HOLD + 1e-9


def test_a_reused_beat_points_back_at_the_original_file():
    """The sourced file is named for the beat that fetched it.

    Without this a repeat looks for a section_003_04.png that nothing ever
    downloaded, and the run dies at ready-image validation.
    """
    from core.utils import REUSED_MEDIA_KEY

    section = _section(26.37, 4)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    slots = section.non_overlay_slots
    for idx, slot in enumerate(slots):
        origin = (slot.props or {}).get(REUSED_MEDIA_KEY)
        if idx < 4:
            assert origin is None, f"beat {idx} is original but was marked reused"
        else:
            assert origin is not None, f"beat {idx} is a repeat but carries no origin"
            assert 0 <= origin < 4


def test_reused_beats_are_not_expected_to_have_been_sourced():
    """The validator must not ask for a file the repeat never downloaded."""
    from core.utils import Script, expected_sourced_image_slots, load_channel_config

    section = _section(26.37, 4)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    script = Script(title="t", video_type="narrative", sections=[section])

    expected = expected_sourced_image_slots(script, load_channel_config("horror_stories"))
    assert len(expected) == 4, (
        f"expected the 4 sourced beats only, got {len(expected)}: {expected}"
    )


def test_copies_are_independent_objects():
    section = _section(26.37, 4)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    slots = section.non_overlay_slots
    assert len({id(s) for s in slots}) == len(slots)


# --- inert when it should be ----------------------------------------------

def test_a_well_sourced_section_is_untouched():
    section = _section(20.0, 6)
    before = [s.keywords for s in section.non_overlay_slots]
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    assert [s.keywords for s in section.non_overlay_slots] == before


def test_a_section_with_no_slots_is_safe():
    section = ScriptSection(id=1, narration="n", actual_duration_seconds=20.0, slots=[])
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    assert section.slots == []


@pytest.mark.parametrize(
    "duration,sourced", [(26.37, 4), (25.85, 5), (28.45, 3), (45.0, 4), (13.57, 1)]
)
def test_any_shortfall_ends_under_the_cap(duration, sourced):
    section = _section(duration, sourced)
    _reuse_slots_to_hold_the_cap(section, max_seconds=MAX_HOLD, crossfade=CROSSFADE)
    durations = compute_sub_durations(
        section, section.sub_slot_count, CROSSFADE,
        max_visual_hold_seconds=MAX_HOLD,
    )
    assert max(durations) <= MAX_HOLD + 1e-9, (
        f"{sourced} sourced -> {len(section.non_overlay_slots)} beats, "
        f"peak {max(durations):.2f}s"
    )
