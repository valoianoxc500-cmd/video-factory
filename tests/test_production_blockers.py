"""The two blockers a real Horror run exposed, and the provenance gap with them.

1. Research source validation judged a Horror story against the *football*
   outlet allowlist, discarded the research, and let the scripter work
   "without a verified brief" -- which is the most likely cause of the weak
   image briefs that then failed image_review.
2. image_review failed twice and the video shipped anyway, because the gate
   was on the production allow-fail list.
3. FLUX frames generated after a rejection were written to `sourcing_log` but
   not to `asset_provenance.json`, so a run reported `generated: 0` while
   three generated frames were in the finished video.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import factory
from core import image_sourcer, researcher
from core.utils import load_channel_config


def _sources(*urls: str) -> dict:
    return {
        "as_of_date": date.today().isoformat(),
        "sources": [{"url": u} for u in urls],
    }


# ── 1. research validation is channel-aware ──────────────────────────

FOOTBALL = load_channel_config("football_news")
HORROR = load_channel_config("horror_stories")
TRUE = load_channel_config("true_stories")


def test_football_still_uses_the_football_allowlist():
    """The channel that shaped these rules must be unchanged."""
    assert researcher.trusted_domains_for(FOOTBALL) is researcher._TRUSTED_SOURCE_DOMAINS
    reason = researcher._reject_fabricated_research(
        _sources("https://www.skysports.com/x"), FOOTBALL
    )
    assert reason == ""


def test_football_allowlist_membership_is_unchanged():
    """The split into shared/football blocks must not have dropped an outlet."""
    for outlet in (
        "bbc.co.uk", "skysports.com", "premierleague.com", "transfermarkt.com",
        "lequipe.fr", "yallakora.com", "theathletic.com", "fbref.com",
    ):
        assert outlet in researcher._TRUSTED_SOURCE_DOMAINS


@pytest.mark.parametrize("config", [HORROR, TRUE], ids=["horror", "true_stories"])
def test_a_story_channel_does_not_use_the_football_allowlist(config):
    assert (
        researcher.trusted_domains_for(config)
        is researcher._GENERAL_TRUSTED_DOMAINS
    )


@pytest.mark.parametrize("config", [HORROR, TRUE], ids=["horror", "true_stories"])
@pytest.mark.parametrize("url", [
    "https://www.bbc.co.uk/news/uk-12345",
    "https://www.theguardian.com/uk-news/2026/story",
    "https://www.snopes.com/fact-check/vanishing-hitchhiker/",
    "https://www.smithsonianmag.com/history/x",
    "https://www.atlasobscura.com/places/mile-43",
    "https://archive.org/details/x",
    "https://www.nationalarchives.gov.uk/x",
    "https://en.wikipedia.org/wiki/Vanishing_hitchhiker",
    "https://www.npr.org/2026/01/01/x",
])
def test_horror_research_is_not_rejected_by_football_only_rules(config, url):
    """THE REGRESSION. A documentary source must satisfy a story channel."""
    reason = researcher._reject_fabricated_research(_sources(url), config)
    assert reason == "", f"{url} was rejected for {config.channel_name}"


def test_the_exact_run_that_failed_would_now_be_judged_correctly():
    """The real run cited facebook.com three times and was rejected.

    It should still be rejected -- social posts are not sources -- but the
    reason must not be about football outlets.
    """
    reason = researcher._reject_fabricated_research(
        _sources("https://facebook.com/a", "https://facebook.com/b"), HORROR
    )
    assert reason, "social-only citations must still be rejected"
    assert "football" not in reason
    assert "archive or reference" in reason


@pytest.mark.parametrize("config", [HORROR, TRUE], ids=["horror", "true_stories"])
@pytest.mark.parametrize("url", [
    "https://facebook.com/groups/ghosts",
    "https://www.reddit.com/r/nosleep/x",
    "https://youtube.com/watch?v=x",
    "https://scary-legends-daily.example/x",
    "https://medium.com/@someone/x",
    "https://pinterest.com/pin/x",
])
def test_story_validation_is_not_weakened(config, url):
    """A wider list is not a weaker one: farms and forums still fail."""
    reason = researcher._reject_fabricated_research(_sources(url), config)
    assert reason, f"{url} should not satisfy {config.channel_name}"


def test_a_football_desk_does_not_satisfy_a_story_channel_alone():
    """The lists are different, not merged."""
    reason = researcher._reject_fabricated_research(
        _sources("https://www.transfermarkt.com/x"), HORROR
    )
    assert reason


def test_a_folklore_source_does_not_satisfy_football():
    reason = researcher._reject_fabricated_research(
        _sources("https://www.snopes.com/fact-check/x"), FOOTBALL
    )
    assert reason
    assert "football" in reason


def test_future_dated_research_is_still_rejected_on_every_channel():
    """The other structural check must survive the split."""
    from datetime import timedelta

    for config in (FOOTBALL, HORROR, TRUE):
        research = {
            "as_of_date": (date.today() + timedelta(days=5)).isoformat(),
            "sources": [{"url": "https://www.bbc.co.uk/news/x"}],
        }
        reason = researcher._reject_fabricated_research(research, config)
        assert "have not happened" in reason


def test_the_default_is_still_football_for_callers_that_pass_no_config():
    """Existing call sites and tests pass one argument."""
    assert researcher.trusted_domains_for(None) is researcher._TRUSTED_SOURCE_DOMAINS
    assert "football" in researcher._reject_fabricated_research(
        _sources("https://facebook.com/x")
    )


# ── 2. image_review can no longer be waived ──────────────────────────

def test_image_review_is_unwaivable():
    assert "image_review" in factory._UNWAIVABLE_REVIEW_GATES


def test_asking_to_waive_image_review_is_ignored_not_honoured():
    """The token still parses -- the worker passes it -- but does nothing."""
    allowed = factory._parse_allowed_review_failures(
        "image_review,thumbnail_review,final_review"
    )
    assert "image_review" not in allowed
    assert allowed == {"thumbnail_review", "final_review"}


def test_the_short_alias_is_ignored_too():
    assert factory._parse_allowed_review_failures("image") == set()


def test_the_other_gates_can_still_be_waived():
    """Only the visual gate is being tightened."""
    assert factory._parse_allowed_review_failures(
        "script_review,thumbnail_review,final_review"
    ) == {"script_review", "thumbnail_review", "final_review"}


def test_an_unknown_gate_is_still_a_bad_parameter():
    import click

    with pytest.raises(click.BadParameter):
        factory._parse_allowed_review_failures("not_a_gate")


def test_the_worker_no_longer_asks_to_waive_image_review():
    text = Path("worker/worker.py").read_text(encoding="utf-8")
    line = next(
        l for l in text.splitlines()
        if '"ALLOWED_REVIEW_FAILURES"' in l
    )
    assert "image_review" not in line
    assert "thumbnail_review" in line and "final_review" in line


def test_a_failed_image_review_still_fails_the_run():
    """With the gate unwaivable, the handler's waive branch cannot be taken."""
    source = Path("factory.py").read_text(encoding="utf-8")
    # The waive branch is guarded by membership in allowed_review_failures,
    # which _parse_allowed_review_failures can no longer put image_review in.
    assert 'if "image_review" in allowed_review_failures:' in source
    assert "_UNWAIVABLE_REVIEW_GATES" in source


def test_preview_mode_is_the_only_remaining_waiver_and_ships_nothing():
    """`--preview-remotion` stops before render/export, so nothing ships."""
    source = Path("factory.py").read_text(encoding="utf-8")
    assert "exports nothing" in source
    assert '--preview-remotion cannot be combined with --stage' in source


def test_the_retry_and_regeneration_path_is_still_configured():
    """Rejected beats must be re-sourced before the gate is allowed to fail."""
    for slug in ("horror_stories", "true_stories"):
        cfg = load_channel_config(slug)
        assert cfg.image_sourcing.generate_when_rejected is True
        assert cfg.image_sourcing.max_generated_when_rejected >= 1
        assert cfg.review_thresholds.image_review_max_attempts >= 1


# ── 3. provenance for generated frames ───────────────────────────────

def test_a_generated_frame_is_recorded_in_asset_provenance(tmp_path):
    image_sourcer._reset_provenance()
    record = {
        "generated": True,
        "source": "ai_generated_fallback",
        "provenance": "AI-generated illustration, not a photograph",
        "model": "fal-ai/flux/schnell",
        "prompt": "a fog-covered road",
    }
    image_sourcer._record_generated_asset_provenance(
        tmp_path / "section_001_01.jpg", record
    )
    assets = image_sourcer._ASSET_PROVENANCE
    assert "section_001_01.jpg" in assets
    entry = assets["section_001_01.jpg"]
    assert entry["generated"] is True
    assert entry["model"] == "fal-ai/flux/schnell"
    assert entry["file"] == "section_001_01.jpg"


def test_generated_frames_reach_the_written_provenance_file(tmp_path):
    """asset_provenance.json is written from _ASSET_PROVENANCE, not the log."""
    import json

    image_sourcer._reset_provenance()
    image_sourcer._record_generated_asset_provenance(
        tmp_path / "section_002_03.jpg",
        {"generated": True, "model": "fal-ai/flux/schnell"},
    )
    out = image_sourcer.save_asset_provenance(tmp_path)
    written = json.loads(out.read_text(encoding="utf-8"))["assets"]
    assert [a["file"] for a in written] == ["section_002_03.jpg"]
    assert written[0]["generated"] is True


def test_both_generation_paths_record_provenance():
    """The rejection fallback and the ai_gen lane, not just one of them."""
    source = Path("core/image_sourcer.py").read_text(encoding="utf-8")
    assert source.count("_record_generated_asset_provenance(") >= 3  # def + 2 calls
    assert '"source": "ai_generated_lane"' in source


def test_provenance_is_cleared_between_runs():
    image_sourcer._record_generated_asset_provenance(
        "x.jpg", {"generated": True})
    image_sourcer._reset_provenance()
    assert image_sourcer._ASSET_PROVENANCE == {}


# ── 4. FLUX fallback and real-media preference are untouched ─────────

@pytest.mark.parametrize("slug", ["horror_stories", "true_stories"])
def test_flux_is_still_the_story_channel_generator(slug):
    cfg = load_channel_config(slug)
    assert cfg.image_sourcing.generated_fallback_model == "fal-ai/flux/schnell"


def test_football_generation_is_untouched():
    cfg = load_channel_config("football_news")
    assert "fal" not in cfg.image_sourcing.generated_fallback_model


def test_story_channels_keep_their_distinct_visual_priority():
    """Horror is fictional; True Stories must exhaust real sourcing first."""
    horror = load_channel_config("horror_stories")
    true_stories = load_channel_config("true_stories")
    assert horror.image_sourcing.prefer_generated_visuals is True
    assert true_stories.image_sourcing.prefer_generated_visuals is False
    assert horror.image_sourcing.open_library_fallback is True
    assert true_stories.image_sourcing.open_library_fallback is True


def test_thumbnail_behaviour_is_untouched():
    thumb = Path("core/providers/thumbnails.py").read_text(encoding="utf-8")
    assert "gemini-25-flash-image/edit" in thumb
    assert "flux" not in thumb.lower()
