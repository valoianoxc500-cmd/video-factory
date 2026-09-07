"""Dropped visual beats must be re-sourced, never quietly cycled.

The failure this covers, from job 8b9a3c3d:

  * the script passed validation with ~20 slots
  * image sourcing dropped 11 of them (web_photos_only found no real photo)
  * script.json was rewritten with 4/3/2 slots and nothing re-checked pacing
  * the renderer received 9 images for 70.4s needing 16 beats and cycled them

Frame clustering on the shipped video found five visuals reappearing after a
gap; section 3 showed two photographs three times each across 25 seconds.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from dataclasses import dataclass, field

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import core.image_sourcer as isrc
from core.image_sourcer import (
    enforce_minimum_slots,
    minimum_slots_for_section,
    underpopulated_sections,
)
from core.utils import load_channel_config

HORROR = load_channel_config("horror_stories")

# Horror now opts into `complete_over_coverage`: a starved section finishes the
# video instead of stopping it. STRICT is the same channel with that trade off,
# so the enforcement rules below are still exercised with identical pacing
# numbers -- it is the outcome that differs, not the arithmetic.
STRICT = HORROR.model_copy(deep=True)
STRICT.image_sourcing.complete_over_coverage = False


@dataclass
class FakeSlot:
    visual: str = "google_photo"
    keywords: str = "old traditional saudi house dark"
    prompt: str = ""
    visual_policy: str = "source_as_written"
    props: dict = field(default_factory=dict)


@dataclass
class FakeSection:
    id: int
    slots: list
    narration: str = "نص عربي"
    actual_duration_seconds: float | None = None
    estimated_duration_seconds: float | None = None


@dataclass
class FakeScript:
    sections: list
    title: str = "الزوجة السعودية التي أكلت لحم البشر"
    tags: list = field(default_factory=lambda: ["Saudi Arabia", "true crime"])


def _section(sid, n_slots, seconds):
    return FakeSection(
        id=sid,
        slots=[FakeSlot() for _ in range(n_slots)],
        actual_duration_seconds=seconds,
    )


# --- the minimum itself ----------------------------------------------------

def test_minimum_tracks_the_five_second_cap():
    assert HORROR.rendering_defaults.max_visual_hold_seconds == 5.0
    # 25.1s at a 5s cap needs 6 beats.
    assert minimum_slots_for_section(_section(3, 2, 25.05), HORROR) == 6


def test_minimum_prefers_measured_duration_over_the_estimate():
    """The estimate ran ~15% short of real TTS, which under-counts beats."""
    section = FakeSection(id=1, slots=[FakeSlot()],
                          estimated_duration_seconds=18.9,
                          actual_duration_seconds=25.1)
    assert minimum_slots_for_section(section, HORROR) == 6      # from 25.1s
    section.actual_duration_seconds = None
    # 18.9s needs only 4 beats -- which is exactly how the shipped script
    # under-counted: the estimate was 18.9s but the narration ran 25.1s.
    assert minimum_slots_for_section(section, HORROR) == 4


def test_a_section_with_no_duration_is_not_forced():
    assert minimum_slots_for_section(FakeSection(id=1, slots=[]), HORROR) == 1


# --- detection, using the real job's numbers -------------------------------

def test_the_real_job_is_detected_as_underpopulated():
    script = FakeScript([
        _section(1, 4, 23.01),
        _section(2, 3, 22.29),
        _section(3, 2, 25.05),
    ])
    short = underpopulated_sections(script, HORROR)
    assert set(short) == {1, 2, 3}
    assert short[3] == (2, 6), "section 3 had 2 images for 6 beats"


def test_a_properly_populated_script_is_not_flagged():
    script = FakeScript([_section(1, 6, 23.0), _section(2, 6, 22.3)])
    assert underpopulated_sections(script, HORROR) == {}


def test_exactly_meeting_the_minimum_is_accepted():
    need = minimum_slots_for_section(_section(1, 1, 23.01), HORROR)
    script = FakeScript([_section(1, need, 23.01)])
    assert underpopulated_sections(script, HORROR) == {}


# --- enforcement -----------------------------------------------------------

def test_enforcement_fails_the_run_rather_than_cycling():
    script = FakeScript([_section(3, 2, 25.05)])
    with pytest.raises(RuntimeError, match="without repeating images"):
        enforce_minimum_slots(script, STRICT)


def test_a_channel_that_prefers_finishing_warns_instead(caplog):
    """Horror trades the other way: the story ships, loudly."""
    script = FakeScript([_section(3, 2, 25.05)])
    with caplog.at_level("WARNING"):
        enforce_minimum_slots(script, HORROR)
    assert any("rather than abandoning" in r.message for r in caplog.records)


def test_the_failure_names_the_section_and_the_shortfall():
    script = FakeScript([_section(3, 2, 25.05)])
    with pytest.raises(RuntimeError) as err:
        enforce_minimum_slots(script, STRICT)
    message = str(err.value)
    assert "section 3" in message
    assert "2 real photograph" in message
    assert "needs 6" in message


def test_enforcement_passes_a_healthy_script():
    enforce_minimum_slots(FakeScript([_section(1, 8, 23.0)]), HORROR)


def test_enforcement_only_judges_sections_that_lost_beats():
    """A thin section that never dropped anything is not this rule's business.

    Sourcing runs before some sections have measured durations, and small or
    deliberately short sections exist. Applying the minimum to scripts that
    lost nothing would fail runs the old contract already allowed.
    """
    script = FakeScript([_section(1, 2, 25.05), _section(2, 8, 23.0)])
    # Section 1 is thin, but only section 2 lost beats: nothing to enforce.
    enforce_minimum_slots(script, STRICT, only_sections={2})
    # Once section 1 is the one that lost beats, it must fail.
    with pytest.raises(RuntimeError, match="section 1"):
        enforce_minimum_slots(script, STRICT, only_sections={1})


# --- the rescue pass -------------------------------------------------------

def _descriptor(section, sub_idx, sourced):
    return {
        "section": section,
        "slot": section.slots[0],
        "sub_idx": sub_idx,
        "lane": "photo",
        "prompt": "",
        "sourced": sourced,
    }


def test_rescue_runs_for_dropped_beats_in_a_thin_section(tmp_path, monkeypatch):
    section = _section(3, 2, 25.05)
    script = FakeScript([section])
    descriptors = [
        _descriptor(section, 0, True),
        _descriptor(section, 1, False),      # dropped beat
    ]

    calls = []

    async def fake_source(**kwargs):
        calls.append(kwargs["keywords"])
        return "serper"

    monkeypatch.setattr(isrc, "_source_single_image", fake_source)

    asyncio.run(isrc._rescue_underpopulated_sections(
        descriptors=descriptors, script=script, config=HORROR,
        client=None, seen_hashes=set(), sourcing_log=[], raw_dir=tmp_path,
    ))

    assert calls, "the dropped beat was never re-sourced"
    assert descriptors[1]["sourced"] is True


def test_rescue_never_relaxes_the_relevance_gate(tmp_path, monkeypatch):
    section = _section(3, 2, 25.05)
    script = FakeScript([section])
    descriptors = [_descriptor(section, 1, False)]
    seen = []

    async def fake_source(**kwargs):
        seen.append(kwargs)
        return None                      # force it to try every query

    monkeypatch.setattr(isrc, "_source_single_image", fake_source)
    asyncio.run(isrc._rescue_underpopulated_sections(
        descriptors=descriptors, script=script, config=HORROR,
        client=None, seen_hashes=set(), sourcing_log=[], raw_dir=tmp_path,
    ))

    assert seen, "no rescue attempt was made"
    for call in seen:
        assert call["require_relevance_review"] is True
        assert call["allow_generation_fallback"] is False
        assert call["allow_last_resort_generation"] is False


def test_rescue_queries_are_anchored_on_the_real_subject(tmp_path):
    """Terms come from the slot and the story's entities, never from nowhere."""
    from core.bilingual_queries import is_single_script

    section = _section(3, 2, 25.05)
    script = FakeScript([section])
    queries = isrc._subject_rescue_queries(script, section, section.slots[0])
    assert queries, "no rescue queries produced"

    source = " ".join([
        section.slots[0].keywords, script.title, *script.tags,
        "archival photograph", "historical photograph", "صورة أرشيفية",
    ]).lower()
    for q in queries:
        # No invented vocabulary: every term traces back to the slot, the
        # story, or the fixed archive suffixes.
        for term in q.lower().split():
            assert term in source, f"{term!r} in {q!r} came from nowhere"


def test_rescue_queries_never_mix_scripts(tmp_path):
    """The Flight 19 defect: 'الرحلة 19 archival photograph' matched nothing."""
    from core.bilingual_queries import is_single_script

    section = _section(3, 2, 25.05)
    script = FakeScript([section])
    for q in isrc._subject_rescue_queries(script, section, section.slots[0]):
        assert is_single_script(q), q


def test_rescue_is_skipped_when_no_section_is_thin(tmp_path, monkeypatch):
    section = _section(1, 8, 23.0)
    script = FakeScript([section])
    descriptors = [_descriptor(section, 1, False)]

    called = False

    async def fake_source(**kwargs):
        nonlocal called
        called = True
        return "serper"

    monkeypatch.setattr(isrc, "_source_single_image", fake_source)
    asyncio.run(isrc._rescue_underpopulated_sections(
        descriptors=descriptors, script=script, config=HORROR,
        client=None, seen_hashes=set(), sourcing_log=[], raw_dir=tmp_path,
    ))
    assert called is False, "rescue ran for a section that was already healthy"


# --- settings that must survive this fix -----------------------------------

def test_web_photos_only_is_still_on():
    assert HORROR.image_sourcing.web_photos_only is True


def test_the_hold_cap_is_unchanged():
    assert HORROR.rendering_defaults.max_visual_hold_seconds == 5.0


def test_football_news_is_also_protected_by_the_same_rule():
    """The enforcement is engine-agnostic, not horror-specific."""
    football = load_channel_config("football_news")
    script = FakeScript([_section(1, 2, 40.0)])
    with pytest.raises(RuntimeError):
        enforce_minimum_slots(script, football)
