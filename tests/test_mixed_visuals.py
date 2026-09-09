"""Mixed video/photo visuals on the documentary channels.

Two things are being protected. That the channels can actually use short
licensed clips among their photographs -- and that the mix stays honest: b-roll
is atmosphere between sourced photographs, never something a viewer could read
as footage of the real event under a factual voiceover.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.image_sourcer import _broll_allowed  # noqa: E402
from core.utils import ImageSourcingConfig  # noqa: E402

CHANNELS = REPO_ROOT / "config" / "channels"


def _channel(name: str) -> dict:
    return json.loads((CHANNELS / f"{name}.json").read_text(encoding="utf-8"))


class _Config:
    """Just enough ChannelConfig for the ratio gate."""

    def __init__(self, **kwargs):
        self.image_sourcing = ImageSourcingConfig(**kwargs)


# --- the opt-in ------------------------------------------------------------

def test_video_broll_is_off_by_default():
    """No channel gains motion footage merely by being upgraded."""
    assert ImageSourcingConfig().allow_video_broll is False


def test_a_channel_that_has_not_opted_in_gets_no_clips():
    config = _Config(allow_video_broll=False)
    assert _broll_allowed(config, used=0, total_slots=12) is False


@pytest.mark.parametrize("channel", ["horror_stories", "football_news"])
def test_both_documentary_channels_opted_in(channel):
    sourcing = _channel(channel)["image_sourcing"]
    assert sourcing.get("allow_video_broll") is True
    assert 0 < sourcing.get("max_broll_ratio", 0) <= 0.5


@pytest.mark.parametrize("channel", ["horror_stories", "football_news"])
def test_each_channel_declares_how_its_stills_are_obtained(channel):
    """Football sources photographs; Horror generates its beats."""
    src = _channel(channel)["image_sourcing"]
    if channel == "football_news":
        assert src["web_photos_only"] is True
        assert src.get("prefer_generated_visuals", False) is False
    else:
        assert src["prefer_generated_visuals"] is True


def test_the_two_channels_stay_separate():
    """Same capability, independent configuration."""
    horror = _channel("horror_stories")
    football = _channel("football_news")
    assert horror["image_sourcing"] is not football["image_sourcing"]
    # Horror keeps its generated-visual fallback; football must not have it.
    assert horror["image_sourcing"].get("allow_generated_fallback") is True
    assert football["image_sourcing"].get("allow_generated_fallback", False) is False


# --- the ratio -------------------------------------------------------------

def test_the_mix_is_bounded():
    """A story told mostly in stock footage is not that story."""
    config = _Config(allow_video_broll=True, max_broll_ratio=0.34)
    assert _broll_allowed(config, used=0, total_slots=12) is True
    assert _broll_allowed(config, used=3, total_slots=12) is True
    assert _broll_allowed(config, used=4, total_slots=12) is False


def test_a_short_video_still_gets_one_clip():
    """A ratio that rounds to zero would silently disable the feature."""
    config = _Config(allow_video_broll=True, max_broll_ratio=0.34)
    assert _broll_allowed(config, used=0, total_slots=2) is True
    assert _broll_allowed(config, used=1, total_slots=2) is False


def test_a_video_with_no_slots_gets_nothing():
    config = _Config(allow_video_broll=True)
    assert _broll_allowed(config, used=0, total_slots=0) is False


def test_a_zero_ratio_still_permits_a_single_clip_only():
    config = _Config(allow_video_broll=True, max_broll_ratio=0.0)
    assert _broll_allowed(config, used=0, total_slots=20) is True
    assert _broll_allowed(config, used=1, total_slots=20) is False


# --- relevance -------------------------------------------------------------

def test_documentary_review_judges_broll_by_scene_not_category():
    from prompts import image_review_prompt

    context = [{
        "section_id": 1, "sub_image_index": 1, "narration": "n",
        "visual_type": "b_roll", "is_section_opener": True, "text_only": False,
        "prompt": "p", "image_search_keywords": "k",
        "image_filename": "f.jpg", "is_b_roll": True,
    }]
    documentary = image_review_prompt(context, documentary=True)

    assert "not the broad topic" in documentary
    assert "never evidence" in documentary
    # The permissive demo-channel rule must not reach a documentary channel.
    assert "any busy market" not in documentary


def test_the_demo_channel_rules_are_unchanged():
    """Other channels keep the behaviour they had."""
    from prompts import image_review_prompt

    context = [{
        "section_id": 1, "sub_image_index": 1, "narration": "n",
        "visual_type": "b_roll", "is_section_opener": True, "text_only": False,
        "prompt": "p", "image_search_keywords": "k",
        "image_filename": "f.jpg", "is_b_roll": True,
    }]
    default = image_review_prompt(context)
    assert "any busy market" in default


def test_no_broll_rules_when_there_is_no_broll():
    from prompts import image_review_prompt

    context = [{
        "section_id": 1, "sub_image_index": 1, "narration": "n",
        "visual_type": "google_photo", "is_section_opener": True,
        "text_only": False, "prompt": "p", "image_search_keywords": "k",
        "image_filename": "f.jpg", "is_b_roll": False,
    }]
    assert "b_roll_rules" not in image_review_prompt(context, documentary=True)


def test_planner_guidance_is_not_fitness_specific():
    """The old guidance described leg stretches; these channels are not that."""
    source = (REPO_ROOT / "prompts.py").read_text(encoding="utf-8")
    block = source[source.index('- "b_roll"'):]
    block = block[: block.index("COMPONENT TYPES")]
    assert "elderly woman doing leg stretches" not in block
    assert "specific named" in block
    assert "record of what happened" in block
