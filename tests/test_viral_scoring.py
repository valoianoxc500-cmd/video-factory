"""Viral scoring: measurement, forecast, and honesty about missing data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from viral.scoring import (
    Confidence,
    NewVersionInputs,
    Potential,
    VideoMetrics,
    new_version_probability,
    potential_band,
    summarise,
    viral_score,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _metrics(**over):
    base = dict(
        views=1_000_000, likes=90_000, comments=4_000, shares=25_000,
        followers=20_000, posted_at=NOW - timedelta(hours=48),
        duration_seconds=28.0, platform="tiktok",
    )
    base.update(over)
    return VideoMetrics(**base)


# --- measurement -----------------------------------------------------------

def test_a_breakout_video_scores_high():
    """1M views on a 20k-follower account, heavily shared."""
    result = viral_score(_metrics(), now=NOW)
    assert result.score >= 70
    assert result.confidence is Confidence.HIGH


def test_a_flat_video_scores_low():
    result = viral_score(_metrics(
        views=800, likes=6, comments=0, shares=0, followers=50_000,
    ), now=NOW)
    assert result.score <= 25


def test_views_against_followers_separates_breakout_from_big_account():
    """The same view count means different things at different account sizes."""
    small = viral_score(_metrics(followers=2_000), now=NOW)
    large = viral_score(_metrics(followers=20_000_000), now=NOW)
    assert small.score > large.score


def test_score_is_deterministic():
    a = viral_score(_metrics(), now=NOW)
    b = viral_score(_metrics(), now=NOW)
    assert a.score == b.score


def test_score_is_bounded():
    absurd = viral_score(_metrics(
        views=10**12, likes=10**11, comments=10**10, shares=10**11, followers=1,
    ), now=NOW)
    assert 0 <= absurd.score <= 100


# --- missing data lowers confidence, never gets imputed --------------------

def test_missing_followers_drops_confidence_not_correctness():
    full = viral_score(_metrics(), now=NOW)
    partial = viral_score(_metrics(followers=None), now=NOW)
    assert partial.coverage < full.coverage
    signal = next(s for s in partial.signals if s.name == "views_per_follower")
    assert signal.observed is False
    assert "follower" in " ".join(partial.notes).lower()


def test_a_platform_with_only_views_is_low_confidence():
    """YouTube gives no share count; many platforms give no followers."""
    result = viral_score(VideoMetrics(views=500_000, platform="youtube"), now=NOW)
    assert result.confidence is Confidence.LOW
    assert result.score > 0            # still scored on what exists


def test_no_metrics_at_all_scores_zero_with_a_reason():
    result = viral_score(VideoMetrics(), now=NOW)
    assert result.score == 0
    assert result.confidence is Confidence.LOW
    assert result.notes


def test_absent_signals_do_not_drag_the_score_down():
    """Renormalising over observed signals is the point of the coverage model."""
    only_views = viral_score(VideoMetrics(views=5_000_000), now=NOW)
    assert only_views.score > 20, (
        "a huge view count scored near zero because absent signals counted "
        "as zeroes rather than being excluded"
    )


def test_a_brand_new_post_has_no_velocity_signal():
    fresh = viral_score(_metrics(posted_at=NOW - timedelta(minutes=5)), now=NOW)
    velocity = next(s for s in fresh.signals if s.name == "velocity")
    assert velocity.observed is False


def test_naive_timestamps_are_treated_as_utc():
    naive = viral_score(_metrics(posted_at=datetime(2026, 9, 4, 12, 0)), now=NOW)
    assert naive.metrics_ok if hasattr(naive, "metrics_ok") else True
    velocity = next(s for s in naive.signals if s.name == "velocity")
    assert velocity.observed is True


# --- forecast --------------------------------------------------------------

def test_forecast_starts_from_the_measured_original():
    original = viral_score(_metrics(), now=NOW)
    forecast = new_version_probability(original, NewVersionInputs())
    assert abs(forecast.score - original.score) <= 10


def test_a_strong_new_version_raises_the_estimate():
    original = viral_score(_metrics(), now=NOW)
    weak = new_version_probability(original, NewVersionInputs(
        hook_strength=0.1, retention_potential=0.1, topic_freshness=0.1,
        length_fit=0.1, is_reused_format=False,
    ))
    strong = new_version_probability(original, NewVersionInputs(
        hook_strength=0.95, retention_potential=0.9, topic_freshness=0.9,
        length_fit=0.9, is_reused_format=False,
    ))
    assert strong.score > weak.score


def test_a_reused_format_is_capped_near_the_original():
    """A new take on a proven format is still a different video."""
    original = viral_score(_metrics(), now=NOW)
    forecast = new_version_probability(original, NewVersionInputs(
        hook_strength=1.0, retention_potential=1.0, topic_freshness=1.0,
        length_fit=1.0, is_reused_format=True,
    ))
    assert forecast.score <= original.score + 8


def test_probability_never_promises_certainty():
    perfect = viral_score(_metrics(views=10**9, followers=1, shares=10**8), now=NOW)
    forecast = new_version_probability(perfect, NewVersionInputs(
        hook_strength=1.0, retention_potential=1.0, topic_freshness=1.0,
        length_fit=1.0, is_reused_format=False,
    ))
    assert forecast.score <= 95, "a forecast implied near-certainty"


def test_probability_has_a_floor():
    awful = viral_score(VideoMetrics(views=1, likes=0, followers=10**7), now=NOW)
    forecast = new_version_probability(awful, NewVersionInputs(
        hook_strength=0.0, retention_potential=0.0, topic_freshness=0.0,
        length_fit=0.0, is_reused_format=False,
    ))
    assert forecast.score >= 1


def test_an_unanalysed_new_version_says_so():
    original = viral_score(_metrics(), now=NOW)
    forecast = new_version_probability(original, NewVersionInputs())
    assert any("not been analysed" in n for n in forecast.notes)
    assert forecast.confidence is Confidence.LOW


def test_forecast_confidence_cannot_exceed_the_measurement():
    """A confident forecast built on a thin measurement is false precision."""
    thin = viral_score(VideoMetrics(views=100_000), now=NOW)
    forecast = new_version_probability(thin, NewVersionInputs(
        hook_strength=0.9, retention_potential=0.9, topic_freshness=0.9,
        length_fit=0.9,
    ))
    assert forecast.confidence is not Confidence.HIGH


# --- presentation ----------------------------------------------------------

@pytest.mark.parametrize("value,band", [
    (95, Potential.EXCEPTIONAL), (85, Potential.EXCEPTIONAL),
    (70, Potential.HIGH), (50, Potential.MODERATE), (10, Potential.LOW),
])
def test_potential_bands(value, band):
    assert potential_band(value) is band


def test_summary_carries_both_numbers_and_the_disclaimer():
    original = viral_score(_metrics(), now=NOW)
    forecast = new_version_probability(original, NewVersionInputs(hook_strength=0.8))
    card = summarise(original, forecast)
    assert card["original_viral_score"] == original.score
    assert card["new_version_probability"] == forecast.score
    assert card["confidence"] in {"low", "medium", "high"}
    assert "not a guarantee" in card["disclaimer"].lower()
    assert card["original_breakdown"]["is_estimate"] is True


def test_breakdown_explains_the_number():
    """A score nobody can interrogate is not usable in a product."""
    result = viral_score(_metrics(), now=NOW)
    record = result.to_record()
    assert record["signals"]
    for signal in record["signals"]:
        assert {"name", "value", "weight", "observed", "detail"} <= set(signal)
