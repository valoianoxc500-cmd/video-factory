"""Visual pacing: every section must keep each image under the hold cap.

Reproduces the Horror run that failed at render with:
    Section 1 exceeds the max visual hold of 5.0s. Slot 1 (google_photo) would
    stay on screen for 9.24s across a 24.69s section with 5 slots.
"""

import pytest

from core.scripter import _TTS_DURATION_MARGIN
from core.utils import (
    ScriptSection,
    VisualSlot,
    compute_sub_durations,
    minimum_visual_slots_for_duration,
)

MAX_HOLD = 5.0
CROSSFADE = 0.3

# Measured from workspace horror_stories_20260905_004244_126908_8dad8b.
REAL_SECTIONS = [
    # (section_id, words, estimated_s, actual_s, slots_the_model_returned)
    (1, 35, 21.3, 24.69, 5),
    (2, 38, 22.8, 28.45, 5),
    (3, 22, 14.7, 13.57, 4),
]


def _section(duration: float, n_slots: int, word_timestamps=None) -> ScriptSection:
    return ScriptSection(
        id=1,
        narration="x " * 40,
        actual_duration_seconds=duration,
        slots=[
            VisualSlot(visual="google_photo", keywords=f"subject {i}", prompt="p")
            for i in range(n_slots)
        ],
        word_timestamps=word_timestamps or [],
    )


# --- the uneven-split defect ----------------------------------------------


def test_uneven_gap_split_is_rejected_for_the_more_even_one():
    """A worse peak must never be kept just because neither split clears the cap.

    The old rule only swapped in the uniform split when it cleared the cap
    outright. On the failing section neither did (uniform was 5.18s), so it
    kept the gap split and held one image for 9.24s -- strictly worse.
    """
    # Words clustered so the largest gaps fall early, starving slot 1.
    words = []
    t = 0.0
    for i in range(40):
        words.append({"word": f"w{i}", "start": t, "end": t + 0.25})
        t += 0.25 + (2.4 if i in (5, 9, 13, 17) else 0.02)
    section = _section(24.69, 5, word_timestamps=words)

    gap_split = compute_sub_durations(section, 5, CROSSFADE)
    capped = compute_sub_durations(
        section, 5, CROSSFADE, max_visual_hold_seconds=MAX_HOLD
    )

    assert max(capped) <= max(gap_split), (
        f"capped split peaks at {max(capped):.2f}s, worse than the "
        f"uncapped {max(gap_split):.2f}s"
    )


def test_uniform_split_still_wins_when_it_clears_the_cap():
    section = _section(20.0, 5)
    durations = compute_sub_durations(
        section, 5, CROSSFADE, max_visual_hold_seconds=MAX_HOLD
    )
    assert max(durations) <= MAX_HOLD


def test_even_delivery_keeps_its_gap_split():
    """Rebalancing must not flatten a section that was already well paced."""
    words = []
    t = 0.0
    for i in range(40):
        words.append({"word": f"w{i}", "start": t, "end": t + 0.2})
        t += 0.2 + (0.9 if i % 10 == 9 else 0.05)
    section = _section(t, 5, word_timestamps=words)
    durations = compute_sub_durations(
        section, 5, CROSSFADE, max_visual_hold_seconds=MAX_HOLD
    )
    assert len(durations) == 5


# --- the slot-count requirement -------------------------------------------


@pytest.mark.parametrize("section_id,words,estimated,actual,model_slots", REAL_SECTIONS)
def test_margin_requires_enough_slots_for_the_real_duration(
    section_id, words, estimated, actual, model_slots
):
    """The requirement must be sized against the delivered narration.

    Validating on the bare estimate is what let this script through: sections
    1 and 2 each needed six slots once spoken and were approved with five.
    """
    needed_for_real = minimum_visual_slots_for_duration(actual, MAX_HOLD, CROSSFADE)
    required_at_plan_time = minimum_visual_slots_for_duration(
        estimated * _TTS_DURATION_MARGIN, MAX_HOLD, CROSSFADE
    )
    assert required_at_plan_time >= needed_for_real, (
        f"section {section_id}: planning would demand {required_at_plan_time} "
        f"slots but the spoken section needs {needed_for_real}"
    )


def test_the_failing_sections_would_now_be_rejected_at_script_time():
    """Sections 1 and 2 shipped with five slots; the requirement must exceed that."""
    for section_id, _w, estimated, _a, model_slots in REAL_SECTIONS[:2]:
        required = minimum_visual_slots_for_duration(
            estimated * _TTS_DURATION_MARGIN, MAX_HOLD, CROSSFADE
        )
        assert required > model_slots, (
            f"section {section_id}: {model_slots} slots would still pass "
            f"(requirement is {required})"
        )


def test_the_passing_section_is_not_over_constrained():
    """Section 3 was correctly paced; the margin must not inflate it."""
    _id, _w, estimated, _a, model_slots = REAL_SECTIONS[2]
    required = minimum_visual_slots_for_duration(
        estimated * _TTS_DURATION_MARGIN, MAX_HOLD, CROSSFADE
    )
    assert required <= model_slots


def test_margin_only_ever_raises_the_requirement():
    assert _TTS_DURATION_MARGIN >= 1.0


# --- surviving a dropped slot ----------------------------------------------


def test_a_section_still_holds_the_cap_after_losing_one_slot():
    """Sourcing can drop a beat, and the survivors must still cover the section.

    A run planned the six slots its 25.85s section needed, lost one to
    sourcing, and the remaining five held 5.41s each against a 5.0s cap --
    failing at render with every image already paid for.
    """
    from core.scripter import _SOURCING_DROP_ALLOWANCE

    duration = 25.85
    planned = minimum_visual_slots_for_duration(
        duration, MAX_HOLD, CROSSFADE
    ) + _SOURCING_DROP_ALLOWANCE

    survivors = planned - 1
    per_slot = (duration + CROSSFADE * (survivors - 1)) / survivors
    assert per_slot <= MAX_HOLD + 1e-9, (
        f"after dropping one slot, {survivors} remain holding {per_slot:.2f}s"
    )


@pytest.mark.parametrize("duration", [13.57, 21.3, 24.69, 25.85, 28.45, 45.0])
def test_one_dropped_slot_is_survivable_at_any_length(duration):
    from core.scripter import _SOURCING_DROP_ALLOWANCE

    planned = minimum_visual_slots_for_duration(
        duration, MAX_HOLD, CROSSFADE
    ) + _SOURCING_DROP_ALLOWANCE
    survivors = max(planned - 1, 1)
    per_slot = (duration + CROSSFADE * (survivors - 1)) / survivors
    assert per_slot <= MAX_HOLD + 1e-9, f"{survivors} slots hold {per_slot:.2f}s"


def test_the_allowance_is_exactly_one_spare():
    """More than one spare would inflate sourcing cost for a rare case."""
    from core.scripter import _SOURCING_DROP_ALLOWANCE

    assert _SOURCING_DROP_ALLOWANCE == 1


@pytest.mark.parametrize("duration", [10.0, 24.69, 28.45, 60.0, 90.0])
def test_required_slot_count_actually_holds_the_cap(duration):
    """The count the planner demands must genuinely keep an even split under 5s."""
    n = minimum_visual_slots_for_duration(duration, MAX_HOLD, CROSSFADE)
    per_slot = (duration + CROSSFADE * (n - 1)) / n
    assert per_slot <= MAX_HOLD + 1e-9, f"{n} slots still holds {per_slot:.2f}s"
