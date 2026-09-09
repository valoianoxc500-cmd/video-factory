"""Engine registry: routing, isolation, and the Football News guarantee.

Engine-specific behaviour lives in the per-engine test modules; this file
covers the routing layer every engine shares.
"""

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


# --- the existing engine must not have been disturbed ----------------------

def test_football_news_config_still_loads_and_keeps_its_identity():
    cfg = load_channel_config("football_news")
    assert cfg.channel_name == "Football News"
    assert cfg.language == "ar"
    assert cfg.image_sourcing.web_photos_only is True


def test_engines_are_separate_files_and_cannot_share_state():
    news = json.loads((CHANNELS / "football_news.json").read_text(encoding="utf-8"))
    horror = json.loads((CHANNELS / "horror_stories.json").read_text(encoding="utf-8"))
    assert news["channel_name"] != horror["channel_name"]
    # Editing one must be incapable of changing the other.
    assert news["script_style"]["instructions"] != horror["script_style"]["instructions"]
    assert news["video"]["visual_guidance"] != horror["video"]["visual_guidance"]


def test_football_news_cannot_enter_a_footage_code_path():
    """The match-footage stage is gated on this being present."""
    assert load_channel_config("football_news").match_footage is None


def test_every_engine_keeps_the_shared_vertical_render_contract():
    for slug in ("football_news", "horror_stories"):
        cfg = load_channel_config(slug)
        assert cfg.video.resolution == [1080, 1920], slug
        assert cfg.video.fps == 30, slug


# --- worker routing --------------------------------------------------------

def test_job_engine_selects_that_channel():
    assert worker_mod._channel_for_job({"engine": "horror_stories"}) == "horror_stories"
    assert worker_mod._channel_for_job({"engine": "football_news"}) == "football_news"


def test_missing_engine_falls_back_to_the_configured_channel():
    assert worker_mod._channel_for_job({}) == worker_mod.CHANNEL
    assert worker_mod._channel_for_job({"engine": ""}) == worker_mod.CHANNEL
    assert worker_mod._channel_for_job({"engine": None}) == worker_mod.CHANNEL


def test_unknown_or_retired_engine_falls_back_rather_than_failing_the_job():
    # A newer web deploy, or a job queued against a since-removed engine,
    # must not be able to kill an older worker's run.
    assert worker_mod._channel_for_job({"engine": "crime"}) == worker_mod.CHANNEL
    assert worker_mod._channel_for_job({"engine": "match_analysis"}) == worker_mod.CHANNEL


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "football_news; rm -rf /",
        "football news",
        "Football_News",          # slug casing is not negotiable
        "a" * 100,
        "--set",
        "./football_news",
    ],
)
def test_malformed_engine_never_reaches_the_command_line(hostile):
    # The engine becomes a CLI argument and a filename, so anything that is
    # not a plain slug must be refused before it gets there.
    assert worker_mod._channel_for_job({"engine": hostile}) == worker_mod.CHANNEL


def _registered_engine_slugs() -> list[str]:
    """Every engine slug declared in the web UI.

    An engine object declares a `section`; a story-type object does not, which
    is what keeps the two apart. Matching on `section` rather than on the
    line that happens to follow `label` means adding a field to an engine
    cannot silently empty this list -- which it did, and the assertion below
    then passed on nothing.
    """
    engines_ts = (REPO_ROOT / "web" / "lib" / "engines.ts").read_text(encoding="utf-8")
    return re.findall(
        r'slug:\s*"([a-z0-9_]+)",\s*\n\s*label:[^\n]*\n\s*section:', engines_ts
    )


def test_every_registered_web_engine_has_a_channel_config():
    """The UI must never offer an engine the worker cannot run."""
    slugs = _registered_engine_slugs()
    assert set(slugs) == {
        "football_news", "horror_stories", "true_stories",
        # Added as a third standalone section, not a replacement for any of
        # the three above.
        "animated_stories",
    }, slugs
    for slug in slugs:
        assert (CHANNELS / f"{slug}.json").exists(), f"no channel config for {slug}"


def test_the_engine_list_is_actually_found():
    """Guards the regex above: an empty list must never look like a pass."""
    assert len(_registered_engine_slugs()) >= 3


def test_story_to_video_holds_exactly_horror_and_true_stories():
    """The sidebar's Story To Video group is these two engines and no others."""
    engines_ts = (REPO_ROOT / "web" / "lib" / "engines.ts").read_text(encoding="utf-8")
    story = re.findall(
        r'slug:\s*"([a-z0-9_]+)",\s*\n\s*label:[^\n]*\n\s*section:\s*"story"',
        engines_ts,
    )
    assert set(story) == {"horror_stories", "true_stories"}, story
