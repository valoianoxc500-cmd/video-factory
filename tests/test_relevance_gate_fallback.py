"""A rejected candidate must not be kept on a web-photo-only channel.

`_retry_missed_slots` re-ran every query tier with the relevance gate switched
off, keeping whatever search ranked first. Search ranks confidently wrong
things, so a Horror run was handed Bigfoot for an investigator, toy soldiers
for parachutes, and an icon set for a photograph:

    [image_review] REJECTED attempt 1: Several images were rejected for being
    completely wrong subjects (Bigfoot instead of an investigator, toy soldiers
    instead of parachutes, and icons instead of a photo).

Those images only existed because the sourcing layer put them there after its
own gate had already rejected them.
"""

import asyncio

import pytest
from PIL import Image

import core.image_sourcer as image_sourcer
from core.utils import Script, ScriptSection, VisualSlot


def _script() -> Script:
    return Script(
        title="D.B. Cooper",
        video_type="narrative",
        sections=[
            ScriptSection(
                id=1,
                narration="The investigator reviewed the file.",
                slots=[
                    VisualSlot(
                        visual="google_photo",
                        keywords="FBI investigator 1971",
                        prompt="An investigator with the case file.",
                    )
                ],
            )
        ],
    )


def _run(config, monkeypatch, tmp_path):
    """Drive the retry path and record every (query, require_review) attempt."""
    calls: list[dict] = []

    async def fake_source_single_image(**kwargs):
        calls.append(kwargs)
        return None  # never succeeds, so every tier is exhausted

    monkeypatch.setattr(
        image_sourcer, "_source_single_image", fake_source_single_image
    )

    script = _script()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    descriptors = [{
        "section": script.sections[0],
        "sub_idx": 0,
        "slot": script.sections[0].slots[0],
        "keywords": "FBI investigator 1971",
        "prompt": "An investigator with the case file.",
        "img_path": raw_dir / "section_001_01.jpg",
        "b_roll": False,
        "lane": "photo",
        "sourced": False,
    }]

    asyncio.run(
        image_sourcer._retry_missed_slots(
            descriptors=descriptors,
            script=script,
            config=config,
            client=None,
            seen_hashes=set(),
            sourcing_log=[],
            raw_dir=raw_dir,
        )
    )
    return calls, descriptors


def _web_only_config():
    cfg = image_sourcer_config()
    cfg.image_sourcing.web_photos_only = True
    return cfg


def image_sourcer_config():
    from core.utils import load_channel_config
    return load_channel_config("horror_stories")


# --- the fix ---------------------------------------------------------------

def test_web_photo_channel_never_drops_the_relevance_gate(monkeypatch, tmp_path):
    calls, _ = _run(_web_only_config(), monkeypatch, tmp_path)
    assert calls, "no retry attempts were made"
    relaxed = [c for c in calls if c.get("require_relevance_review") is False]
    assert relaxed == [], (
        f"{len(relaxed)} attempt(s) ran with the relevance gate off; a rejected "
        f"candidate would be kept and then fail image review"
    )


def test_the_slot_is_left_unsourced_rather_than_filled_wrongly(monkeypatch, tmp_path):
    _, descriptors = _run(_web_only_config(), monkeypatch, tmp_path)
    assert descriptors[0]["sourced"] is False, (
        "the slot was marked sourced despite every candidate being rejected"
    )


def test_queries_are_still_widened(monkeypatch, tmp_path):
    """Dropping the relaxed tier must not also drop the widening retries."""
    calls, _ = _run(_web_only_config(), monkeypatch, tmp_path)
    widened = [c for c in calls if c.get("require_relevance_review") is True]
    assert widened, "the widening retries were removed along with the relaxed tier"


def test_no_attempt_disables_the_relevance_gate(monkeypatch, tmp_path):
    """The point of the fix: nothing may ask for the gate to be switched off.

    Checked as "not False" rather than "is True" because the final last-resort
    call omits the argument entirely and takes the default.
    """
    calls, _ = _run(_web_only_config(), monkeypatch, tmp_path)
    assert all(c.get("require_relevance_review") is not False for c in calls)


# --- other channels are unchanged -----------------------------------------

def test_non_web_photo_channel_keeps_its_fallback(monkeypatch, tmp_path):
    """Football News relies on this; its behaviour must be identical."""
    from core.utils import load_channel_config

    cfg = load_channel_config("football_news")
    assert cfg.image_sourcing.web_photos_only is True, (
        "fixture assumption changed; pick a channel with generation enabled"
    )
    cfg.image_sourcing.web_photos_only = False

    calls, _ = _run(cfg, monkeypatch, tmp_path)
    relaxed = [c for c in calls if c.get("require_relevance_review") is False]
    assert relaxed, "the relaxed tier was removed for a channel that still wants it"


# --- no generated stand-in on a web-photo-only channel ---------------------

def test_web_photo_channel_never_generates_a_stand_in(monkeypatch, tmp_path):
    """The beat is dropped, not illustrated.

    Once unsourceable slots stopped being filled with a rejected candidate they
    fell through to the last-resort generator instead, and a run that was meant
    to contain only real photographs ended up with three generated 1344x768
    images that then failed the raw-image size check.
    """
    calls, _ = _run(_web_only_config(), monkeypatch, tmp_path)
    generated = [
        c for c in calls
        if c.get("image_source") == "ai_gen"
        or c.get("allow_last_resort_generation") is True
    ]
    assert generated == [], (
        f"{len(generated)} generation attempt(s) on a web_photos_only channel"
    )


def test_generation_is_still_available_to_other_channels(monkeypatch, tmp_path):
    from core.utils import load_channel_config

    cfg = load_channel_config("football_news")
    cfg.image_sourcing.web_photos_only = False
    calls, _ = _run(cfg, monkeypatch, tmp_path)
    generated = [c for c in calls if c.get("allow_last_resort_generation") is True]
    assert generated, "the last-resort generator was removed for every channel"


def test_horror_is_a_web_photo_only_channel():
    """The fix only binds because Horror declares web_photos_only."""
    assert image_sourcer_config().image_sourcing.web_photos_only is True
