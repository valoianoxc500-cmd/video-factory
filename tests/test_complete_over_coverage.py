"""Horror finishes the video; Football still refuses to ship a thin one.

Two channels, opposite trades. For news, a video whose beats are half-covered
is worse than no video, so a short section stops the run. For a story, losing
eight minutes of narration, narration timing and rendering to two unfindable
photographs is the worse outcome -- the beat is covered and the story ships.

What the trade never includes: reusing one section's photograph in another,
inventing evidence, or lowering the bar for what counts as a real photograph.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.image_sourcer import _cover_unsourced_slots, enforce_minimum_slots  # noqa: E402
from core.utils import ImageSourcingConfig  # noqa: E402

CHANNELS = REPO_ROOT / "config" / "channels"


def _channel(name: str) -> dict:
    return json.loads((CHANNELS / f"{name}.json").read_text(encoding="utf-8"))


# --- the opt-in is per channel ---------------------------------------------

def test_finishing_early_is_off_by_default():
    assert ImageSourcingConfig().complete_over_coverage is False


def test_horror_opted_in():
    assert _channel("horror_stories")["image_sourcing"]["complete_over_coverage"] is True


def test_football_news_finishes_through_its_safe_coverage_ladder():
    """Football may cover a miss, but must not reuse an unrelated beat."""
    sourcing = _channel("football_news")["image_sourcing"]
    assert sourcing.get("complete_over_coverage", False) is True


# --- the abort becomes a warning, but only where opted in ------------------

class _Section:
    def __init__(self, sid: int, slots: list):
        self.id = sid
        self.slots = slots
        self.non_overlay_slots = slots
        self.narration = "n"
        self.estimated_duration_seconds = 30.0


class _Script:
    def __init__(self, sections):
        self.sections = sections


class _Slot:
    def __init__(self, visual="google_photo", prompt="p", keywords="k"):
        self.visual = visual
        self.prompt = prompt
        self.keywords = keywords
        self.props: dict = {}
        self.overlay = False


class _Config:
    def __init__(self, **kwargs):
        self.image_sourcing = ImageSourcingConfig(**kwargs)
        self.rendering_defaults = type(
            "R", (), {"max_visual_hold_seconds": 5.0}
        )()


def _short_script():
    # One section far too thin for its runtime.
    return _Script([_Section(1, [_Slot(), _Slot()])])


def test_a_thin_section_still_stops_a_news_channel(monkeypatch):
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "underpopulated_sections", lambda s, c: {1: (2, 6)})
    with pytest.raises(RuntimeError, match="Not enough real photographs"):
        enforce_minimum_slots(_short_script(), _Config(complete_over_coverage=False))


def test_a_thin_section_lets_a_story_channel_finish(monkeypatch, caplog):
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "underpopulated_sections", lambda s, c: {1: (2, 6)})
    with caplog.at_level("WARNING"):
        enforce_minimum_slots(_short_script(), _Config(complete_over_coverage=True))
    assert any("rather than abandoning" in r.message for r in caplog.records)


def test_a_fully_covered_section_never_warns(monkeypatch, caplog):
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "underpopulated_sections", lambda s, c: {})
    with caplog.at_level("WARNING"):
        enforce_minimum_slots(_short_script(), _Config(complete_over_coverage=True))
    assert not caplog.records


def test_the_warning_says_no_image_crosses_sections(monkeypatch, caplog):
    """The one guarantee that must survive the trade."""
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "underpopulated_sections", lambda s, c: {1: (2, 6)})
    with caplog.at_level("WARNING"):
        enforce_minimum_slots(_short_script(), _Config(complete_over_coverage=True))
    assert any("another section" in r.message for r in caplog.records)


# --- the coverage pass ------------------------------------------------------

def _descriptor(sid: int = 1, keywords: str = "mountain pass", prompt: str = "A pass"):
    slot = _Slot(prompt=prompt, keywords=keywords)
    return {
        "section": _Section(sid, [slot]),
        "slot": slot,
        "sub_idx": 0,
        "keywords": keywords,
        "sourced": False,
        "target_duration": 4.0,
    }


async def _cover(descriptors, config, tmp_path, log=None):
    return await _cover_unsourced_slots(
        descriptors=descriptors,
        config=config,
        sourcing_log=log if log is not None else [],
        videos_dir=tmp_path,
        raw_dir=tmp_path,
        seen_hashes=set(),
        target_size=(1080, 1920),
        fps=30,
    )


@pytest.mark.asyncio
async def test_coverage_does_nothing_when_not_opted_in(tmp_path):
    descriptors = [_descriptor()]
    covered = await _cover(descriptors, _Config(complete_over_coverage=False), tmp_path)
    assert covered == 0
    assert descriptors[0]["sourced"] is False


@pytest.mark.asyncio
async def test_an_unsourced_beat_becomes_an_info_card(tmp_path, monkeypatch):
    """With no Pexels key configured, the card is the cover."""
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "_usable_pexels_key", lambda: "")
    descriptors = [_descriptor(prompt="A map of the pass")]
    log: list[dict] = []

    covered = await _cover(
        descriptors, _Config(complete_over_coverage=True), tmp_path, log
    )

    assert covered == 1
    slot = descriptors[0]["slot"]
    assert slot.visual == "info_card"
    assert slot.props["text"].startswith("A map of the pass")
    assert descriptors[0]["sourced"] is True
    assert log[0]["source"] == "info_card (coverage)"


@pytest.mark.asyncio
async def test_a_sourced_beat_is_left_alone(tmp_path, monkeypatch):
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "_usable_pexels_key", lambda: "")
    descriptor = _descriptor()
    descriptor["sourced"] = True
    covered = await _cover([descriptor], _Config(complete_over_coverage=True), tmp_path)
    assert covered == 0
    assert descriptor["slot"].visual == "google_photo"


@pytest.mark.asyncio
async def test_coverage_never_reuses_another_beats_image(tmp_path, monkeypatch):
    """The failure mode the whole guard exists to prevent."""
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "_usable_pexels_key", lambda: "")
    sourced = _descriptor(keywords="the tent")
    sourced["sourced"] = True
    sourced["img_path"] = tmp_path / "already.jpg"
    empty = _descriptor(sid=2, keywords="the ridge")

    log: list[dict] = []
    await _cover([sourced, empty], _Config(complete_over_coverage=True), tmp_path, log)

    # The covered beat got a card of its own, not the other beat's picture.
    assert empty["slot"].visual == "info_card"
    assert all(entry.get("file") != "already.jpg" for entry in log)


@pytest.mark.asyncio
async def test_a_beat_with_nothing_to_say_is_not_covered(tmp_path, monkeypatch):
    """An empty card is worse than a dropped beat."""
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "_usable_pexels_key", lambda: "")
    descriptor = _descriptor(keywords="", prompt="")
    descriptor["section"].narration = ""
    covered = await _cover([descriptor], _Config(complete_over_coverage=True), tmp_path)
    assert covered == 0
    assert descriptor["sourced"] is False


@pytest.mark.asyncio
async def test_existing_card_props_are_preserved(tmp_path, monkeypatch):
    import core.image_sourcer as isrc

    monkeypatch.setattr(isrc, "_usable_pexels_key", lambda: "")
    descriptor = _descriptor(prompt="A map of the pass")
    descriptor["slot"].props = {"title": "Heading"}
    await _cover([descriptor], _Config(complete_over_coverage=True), tmp_path)
    assert descriptor["slot"].props["title"] == "Heading"
    assert descriptor["slot"].props["text"]


@pytest.mark.asyncio
async def test_a_licensed_clip_is_preferred_over_a_card(tmp_path, monkeypatch):
    import core.image_sourcer as isrc

    async def _fake_pexels(**kwargs):
        kwargs["output_path"].write_bytes(b"clip")
        return True

    monkeypatch.setattr(isrc, "_search_pexels_video", _fake_pexels)
    monkeypatch.setattr(isrc, "_extract_video_frame", lambda *a, **k: True)

    descriptor = _descriptor()
    log: list[dict] = []
    covered = await _cover(
        [descriptor],
        _Config(complete_over_coverage=True, allow_video_broll=True),
        tmp_path,
        log,
    )

    assert covered == 1
    assert descriptor["slot"].visual == "b_roll"
    assert log[0]["source"] == "pexels_video (coverage)"


@pytest.mark.asyncio
async def test_a_card_covers_when_no_clip_is_found(tmp_path, monkeypatch):
    import core.image_sourcer as isrc

    async def _no_clip(**kwargs):
        return False

    monkeypatch.setattr(isrc, "_search_pexels_video", _no_clip)
    descriptor = _descriptor(prompt="A map of the pass")
    covered = await _cover(
        [descriptor],
        _Config(complete_over_coverage=True, allow_video_broll=True),
        tmp_path,
    )
    assert covered == 1
    assert descriptor["slot"].visual == "info_card"


# --- factual verification is untouched -------------------------------------

def test_the_relevance_gate_is_not_relaxed():
    """Coverage changes what happens after sourcing fails, not the bar."""
    source = (REPO_ROOT / "core" / "image_sourcer.py").read_text(encoding="utf-8")
    block = source[source.index("async def _cover_unsourced_slots"):]
    block = block[: block.index("def _broll_allowed")]
    assert "require_relevance_review" not in block
    assert "web_photos_only" not in block


def test_coverage_does_not_generate_imagery():
    """Generation has its own refusal rules; coverage must not bypass them."""
    source = (REPO_ROOT / "core" / "image_sourcer.py").read_text(encoding="utf-8")
    block = source[source.index("async def _cover_unsourced_slots"):]
    block = block[: block.index("def _broll_allowed")]
    assert "generate_image_gemini" not in block
