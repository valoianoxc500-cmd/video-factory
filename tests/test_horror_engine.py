"""Horror Stories engine: registry, config, routing, and asset provenance."""

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "worker"))

from core.utils import load_channel_config  # noqa: E402

import worker as worker_mod  # noqa: E402

CHANNELS = REPO_ROOT / "config" / "channels"
ENGINES_TS = (REPO_ROOT / "web" / "lib" / "engines.ts").read_text(encoding="utf-8")


# --- Match Analysis is gone ------------------------------------------------

def test_match_analysis_channel_config_is_removed():
    assert not (CHANNELS / "match_analysis.json").exists()


def test_match_analysis_is_absent_from_the_ui_registry():
    assert "match_analysis" not in ENGINES_TS
    assert "Match Analysis" not in ENGINES_TS


def test_worker_falls_back_for_the_removed_engine():
    # A job queued before the swap must not crash the worker.
    assert worker_mod._channel_for_job({"engine": "match_analysis"}) == worker_mod.CHANNEL


# --- Football News untouched -----------------------------------------------

def test_football_news_is_unchanged_and_loads():
    cfg = load_channel_config("football_news")
    assert cfg.channel_name == "Football News"
    assert cfg.language == "ar"
    assert cfg.image_sourcing.web_photos_only is True


def test_football_news_declares_no_footage_layer():
    assert load_channel_config("football_news").match_footage is None


def test_football_news_has_no_story_types():
    """Only engines that declare styles get the story-type control."""
    football = re.search(
        r'slug:\s*"football_news".*?(?=\{\s*slug:|\]\s*;)', ENGINES_TS, re.S
    )
    assert football and "styles:" not in football.group(0)


# --- Horror engine ---------------------------------------------------------

def test_horror_channel_config_exists_and_loads():
    cfg = load_channel_config("horror_stories")
    assert cfg.channel_name == "Horror Stories"
    assert cfg.language == "ar"


def test_horror_is_registered_in_the_ui():
    assert '"horror_stories"' in ENGINES_TS
    assert "Horror Stories" in ENGINES_TS
    assert 'theme: "horror"' in ENGINES_TS
    assert "Generate Story" in ENGINES_TS


def test_horror_declares_all_four_story_types():
    for slug in ("true_story", "paranormal", "urban_legend", "custom_horror"):
        assert f'slug: "{slug}"' in ENGINES_TS, slug


def test_horror_routes_to_its_own_channel():
    assert worker_mod._channel_for_job({"engine": "horror_stories"}) == "horror_stories"


def test_horror_keeps_the_shared_vertical_render_contract():
    horror = load_channel_config("horror_stories")
    football = load_channel_config("football_news")
    assert horror.video.resolution == football.video.resolution == [1080, 1920]
    assert horror.video.fps == football.video.fps == 30


def test_horror_uses_real_photos_only():
    """No AI-generated photograph may stand in for a real case or place."""
    cfg = load_channel_config("horror_stories")
    assert cfg.image_sourcing.web_photos_only is True


def test_horror_paces_one_visual_per_beat():
    rd = load_channel_config("horror_stories").rendering_defaults
    assert rd.max_visual_hold_seconds <= 5.0
    assert rd.image_slot_min_duration <= 2.5


def test_horror_script_rules_separate_fact_from_claim():
    raw = json.loads((CHANNELS / "horror_stories.json").read_text(encoding="utf-8"))
    instructions = raw["script_style"]["instructions"]
    for token in ("[VERIFIED]", "[REPORTED]", "[UNVERIFIED]"):
        assert token in instructions, token
    assert "true_story" in instructions
    assert "custom_horror" in instructions
    # Fiction must never be dressed as a real case.
    assert "openly fiction" in instructions


def test_horror_forbids_fabricated_photos_of_real_events():
    raw = json.loads((CHANNELS / "horror_stories.json").read_text(encoding="utf-8"))
    guidance = raw["video"]["visual_guidance"]
    assert "Never" in guidance["ai_photo"]
    assert "gore" in guidance["forbidden_visuals"]


def test_horror_part_two_cta_is_conditional():
    raw = json.loads((CHANNELS / "horror_stories.json").read_text(encoding="utf-8"))
    rules = " ".join(raw["business_strategy"]["cta_rules"])
    assert "الجزء الثاني" in rules
    assert "ONLY when" in rules


# --- style plumbing --------------------------------------------------------

@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "true story", "True_Story", "a" * 100, "--set", ""],
)
def test_malformed_style_is_not_passed_to_the_pipeline(hostile):
    assert worker_mod._STYLE_SLUG_RE.fullmatch(hostile) is None


@pytest.mark.parametrize(
    "slug", ["true_story", "paranormal", "urban_legend", "custom_horror"]
)
def test_valid_style_slugs_pass_validation(slug):
    assert worker_mod._STYLE_SLUG_RE.fullmatch(slug) is not None


# --- every registered engine is runnable -----------------------------------

def test_every_registered_engine_has_a_channel_config():
    slugs = re.findall(r'slug:\s*"([a-z0-9_]+)"', ENGINES_TS)
    engine_slugs = [s for s in slugs if (CHANNELS / f"{s}.json").exists()]
    assert "football_news" in engine_slugs
    assert "horror_stories" in engine_slugs
