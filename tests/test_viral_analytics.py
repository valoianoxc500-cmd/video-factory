"""Analytics collection, and predicted-versus-actual honesty."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from viral.analytics import (
    MIN_SAMPLE,
    Comparison,
    MetricSnapshot,
    calibration_report,
    collect,
    compare,
    growth,
)
from viral.scoring import VideoMetrics, viral_score

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
POSTED = NOW - timedelta(days=7)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- collection ------------------------------------------------------------

def test_youtube_statistics_are_read():
    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"items": [{"statistics": {
            "viewCount": "120000", "likeCount": "9000", "commentCount": "410"}}]})

    snapshot = collect("youtube", "yt-1", "tok", client=_client(handler), now=NOW)
    assert snapshot.views == 120_000
    assert snapshot.likes == 9_000
    assert snapshot.comments == 410


def test_youtube_shares_stay_unknown_rather_than_zero():
    def handler(request):
        return httpx.Response(200, json={"items": [{"statistics": {
            "viewCount": "10"}}]})

    assert collect("youtube", "yt-1", "tok",
                   client=_client(handler), now=NOW).shares is None


def test_instagram_insights_are_read():
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"name": "plays", "values": [{"value": 55000}]},
            {"name": "likes", "values": [{"value": 4300}]},
            {"name": "shares", "values": [{"value": 900}]},
        ]})

    snapshot = collect("instagram", "ig-1", "tok", client=_client(handler), now=NOW)
    assert snapshot.views == 55_000
    assert snapshot.shares == 900
    assert snapshot.comments is None


def test_facebook_insights_are_read():
    def handler(request):
        return httpx.Response(200, json={
            "video_insights": {"data": [
                {"name": "total_video_views", "values": [{"value": 8000}]}]},
            "likes": {"summary": {"total_count": 300}},
            "comments": {"summary": {"total_count": 25}},
        })

    snapshot = collect("facebook", "fb-1", "tok", client=_client(handler), now=NOW)
    assert snapshot.views == 8000
    assert snapshot.likes == 300
    assert snapshot.comments == 25


def test_a_platform_error_yields_no_snapshot_rather_than_zeroes():
    """Zeroes would be recorded as a real result and poison the calibration."""
    def handler(request):
        return httpx.Response(403, json={"error": {"message": "no access"}})

    assert collect("youtube", "yt-1", "tok",
                   client=_client(handler), now=NOW) is None


def test_a_platform_with_no_analytics_path_returns_nothing():
    assert collect("tiktok", "tt-1", "tok",
                   client=_client(lambda r: httpx.Response(200)), now=NOW) is None
    assert collect("snapchat", "s-1", "tok",
                   client=_client(lambda r: httpx.Response(200)), now=NOW) is None


def test_a_missing_post_id_is_not_requested():
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("called the API with no post id")

    assert collect("youtube", "", "tok", client=_client(handler), now=NOW) is None


# --- predicted versus actual ----------------------------------------------

def _snapshot(views=500_000, **over):
    params = dict(platform="youtube", post_id="p1", captured_at=NOW,
                  views=views,
                  likes=(views // 20) if views else None,
                  comments=(views // 300) if views else None,
                  followers=10_000)
    params.update(over)
    return MetricSnapshot(**params)


def test_a_comparison_scores_the_real_numbers_on_the_same_scale():
    predicted = viral_score(VideoMetrics(views=400_000, followers=10_000), now=NOW)
    result = compare(video_id="v1", predicted=predicted,
                     snapshot=_snapshot(), posted_at=POSTED, now=NOW)
    assert result.actual is not None
    assert result.error == round(result.actual - predicted.score, 1)


def test_a_post_with_no_metrics_yet_says_so():
    result = compare(video_id="v1", predicted=60.0, snapshot=None,
                     posted_at=POSTED, now=NOW)
    assert result.actual is None
    assert result.error is None
    assert "No metrics yet" in result.note


def test_a_fresh_post_is_flagged_as_still_climbing():
    result = compare(video_id="v1", predicted=60.0, snapshot=_snapshot(),
                     posted_at=NOW - timedelta(hours=3), now=NOW)
    assert result.settled is False
    assert "keeps climbing" in result.note


def test_a_settled_post_is_not_flagged():
    result = compare(video_id="v1", predicted=60.0, snapshot=_snapshot(),
                     posted_at=POSTED, now=NOW)
    assert result.settled is True


def test_the_band_comparison_is_reported():
    result = compare(video_id="v1", predicted=60.0, snapshot=_snapshot(),
                     posted_at=POSTED, now=NOW)
    assert result.band_matched in {True, False}
    assert result.to_record()["band_matched"] == result.band_matched


def test_follower_count_is_filled_in_when_the_platform_omits_it():
    with_followers = compare(
        video_id="v1", predicted=60.0,
        snapshot=_snapshot(followers=None), posted_at=POSTED,
        followers=5_000, now=NOW)
    without = compare(
        video_id="v1", predicted=60.0, snapshot=_snapshot(followers=None),
        posted_at=POSTED, now=NOW)
    assert with_followers.actual != without.actual


# --- calibration -----------------------------------------------------------

def _comparison(predicted, actual, settled=True):
    return Comparison(video_id="v", platform="youtube", predicted=predicted,
                      actual=actual, posted_at=POSTED, settled=settled)


def test_no_settled_results_says_so_plainly():
    report = calibration_report([_comparison(60, 50, settled=False)])
    assert report["sample"] == 0
    assert report["reliable"] is False
    assert "No settled results" in report["message"]


def test_a_small_sample_is_called_provisional():
    report = calibration_report([_comparison(60, 58) for _ in range(3)])
    assert report["reliable"] is False
    assert "too few" in report["message"]


def test_a_systematically_optimistic_predictor_is_named():
    report = calibration_report([_comparison(80, 55) for _ in range(MIN_SAMPLE)])
    assert report["reliable"] is True
    assert "optimistic" in report["message"]
    assert report["mean_error"] == -25.0


def test_a_conservative_predictor_is_named():
    report = calibration_report([_comparison(40, 70) for _ in range(MIN_SAMPLE)])
    assert "conservative" in report["message"]


def test_a_well_calibrated_predictor_is_reported_as_tracking():
    mixed = ([_comparison(60, 63)] * (MIN_SAMPLE // 2)
             + [_comparison(60, 57)] * (MIN_SAMPLE - MIN_SAMPLE // 2))
    report = calibration_report(mixed)
    assert "track" in report["message"]
    assert report["mean_absolute_error"] == 3.0


def test_band_accuracy_is_reported():
    report = calibration_report([_comparison(90, 88) for _ in range(MIN_SAMPLE)])
    assert report["band_accuracy"] == 1.0


# --- growth ----------------------------------------------------------------

def test_growth_needs_two_readings():
    assert growth([_snapshot()])["measurable"] is False
    assert growth([])["measurable"] is False


def test_growth_reports_views_added_and_the_rate():
    first = _snapshot(views=10_000, captured_at=NOW - timedelta(hours=10))
    last = _snapshot(views=40_000, captured_at=NOW)
    result = growth([last, first])           # deliberately out of order
    assert result["views_added"] == 30_000
    assert result["hours"] == 10.0
    assert result["views_per_hour"] == 3000.0


def test_readings_without_a_view_count_are_ignored():
    readings = [
        _snapshot(views=None, captured_at=NOW - timedelta(hours=5)),
        _snapshot(views=100, captured_at=NOW - timedelta(hours=2)),
    ]
    assert growth(readings)["measurable"] is False
