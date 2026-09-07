"""Discovery: the YouTube provider, niche expansion, sorting, and honesty
about the platforms that have no lawful discovery API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from viral.discovery import (
    Availability,
    DiscoveredVideo,
    SearchQuery,
    SortOrder,
    UnavailableProvider,
    YouTubeDiscovery,
    default_providers,
    expand_niche,
    parse_iso8601_duration,
    sort_results,
)
from viral.scoring import VideoMetrics, viral_score

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


# --- niches ----------------------------------------------------------------

@pytest.mark.parametrize("niche,expected", [
    ("police_chase", "police chase"),
    ("football", "football skills"),
    ("cars", "car build"),
    ("stories", "true story"),
    ("fitness", "workout"),
    ("business", "business tips"),
])
def test_known_niches_expand_to_real_search_terms(niche, expected):
    assert expand_niche(niche) == expected


def test_a_keyword_narrows_the_niche():
    assert expand_niche("cars", "drift") == "car build drift"


def test_an_unknown_niche_falls_back_to_its_own_words():
    assert expand_niche("underwater_welding") == "underwater welding"


def test_a_bare_keyword_works_without_a_niche():
    assert expand_niche("", "dashcam") == "dashcam"


# --- durations -------------------------------------------------------------

@pytest.mark.parametrize("iso,seconds", [
    ("PT30S", 30.0), ("PT1M2S", 62.0), ("PT1H2M3S", 3723.0), ("PT0S", 0.0),
])
def test_iso_durations_parse(iso, seconds):
    assert parse_iso8601_duration(iso) == seconds


def test_an_unparseable_duration_is_none_not_zero():
    """Zero would read as a real measurement; None says 'unknown'."""
    assert parse_iso8601_duration("banana") is None


# --- provider availability -------------------------------------------------

def test_platforms_without_a_lawful_api_say_so():
    status = {p.status().platform: p.status() for p in default_providers()}
    assert status["snapchat"].availability is Availability.NO_PUBLIC_API
    for platform in ("tiktok", "instagram", "facebook"):
        assert status[platform].availability is Availability.RESTRICTED_API
        assert status[platform].reason, f"{platform} gives no reason"


def test_an_unavailable_provider_returns_nothing_rather_than_pretending():
    provider = UnavailableProvider(
        "tiktok", Availability.RESTRICTED_API, "Research API is gated.")
    assert provider.search(SearchQuery(niche="cars")) == []
    assert provider.status().usable is False


def test_youtube_without_a_key_is_not_usable():
    provider = YouTubeDiscovery(api_key="")
    assert provider.status().availability is Availability.NEEDS_CREDENTIALS
    assert provider.search(SearchQuery(niche="cars")) == []


# --- the YouTube provider, against a mocked API ----------------------------

def _youtube_transport():
    """Stands in for the Data API with realistic payload shapes."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(200, json={"items": [
                {"id": {"videoId": "vid1"},
                 "snippet": {"title": "Police chase", "channelId": "ch1"}},
                {"id": {"videoId": "vid2"},
                 "snippet": {"title": "Another", "channelId": "ch2"}},
            ]})
        if path.endswith("/videos"):
            return httpx.Response(200, json={"items": [
                {
                    "id": "vid1",
                    "snippet": {
                        "title": "Police chase", "channelId": "ch1",
                        "channelTitle": "Dashcam Daily",
                        "publishedAt": "2026-09-04T12:00:00Z",
                        "thumbnails": {"high": {"url": "https://img/1.jpg"}},
                    },
                    "statistics": {
                        "viewCount": "2400000", "likeCount": "180000",
                        "commentCount": "9000",
                    },
                    "contentDetails": {"duration": "PT41S"},
                },
                {
                    "id": "vid2",
                    "snippet": {
                        "title": "Another", "channelId": "ch2",
                        "channelTitle": "Small Channel",
                        "publishedAt": "2026-09-05T12:00:00Z",
                        "thumbnails": {"high": {"url": "https://img/2.jpg"}},
                    },
                    "statistics": {"viewCount": "5000", "likeCount": "40"},
                    "contentDetails": {"duration": "PT58S"},
                },
            ]})
        if path.endswith("/channels"):
            return httpx.Response(200, json={"items": [
                {"id": "ch1", "statistics": {"subscriberCount": "50000"}},
                {"id": "ch2", "statistics": {"subscriberCount": "900000"}},
            ]})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_youtube_returns_scored_results():
    with _youtube_transport() as client:
        results = YouTubeDiscovery(api_key="k", client=client).search(
            SearchQuery(niche="police_chase", max_results=10))

    assert len(results) == 2
    first = next(r for r in results if r.video_id == "vid1")
    assert first.platform == "youtube"
    assert first.metrics.views == 2_400_000
    assert first.metrics.likes == 180_000
    assert first.metrics.followers == 50_000
    assert first.url.endswith("vid1")
    assert first.score is not None and first.score.score > 0


def test_youtube_reports_shares_as_unavailable_rather_than_zero():
    """The Data API exposes no share count; zero would be a false measurement."""
    with _youtube_transport() as client:
        results = YouTubeDiscovery(api_key="k", client=client).search(
            SearchQuery(niche="police_chase"))
    assert all(r.metrics.shares is None for r in results)
    share_signal = next(
        s for s in results[0].score.signals if s.name == "share_rate")
    assert share_signal.observed is False


def test_the_breakout_video_outscores_the_big_account_flop():
    with _youtube_transport() as client:
        results = YouTubeDiscovery(api_key="k", client=client).search(
            SearchQuery(niche="police_chase"))
    by_id = {r.video_id: r for r in results}
    assert by_id["vid1"].score.score > by_id["vid2"].score.score


def test_a_failing_api_returns_nothing_rather_than_raising():
    def broken(request):
        return httpx.Response(500, json={"error": "backend"})

    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        assert YouTubeDiscovery(api_key="k", client=client).search(
            SearchQuery(niche="cars")) == []


def test_results_are_marked_analysis_only():
    """Discovery output must never look like a publishable asset."""
    with _youtube_transport() as client:
        results = YouTubeDiscovery(api_key="k", client=client).search(
            SearchQuery(niche="cars"))
    for record in (r.to_record() for r in results):
        assert record["usage"] == "analysis_only"


# --- sorting ---------------------------------------------------------------

def _video(vid, views, hours_old, score):
    metrics = VideoMetrics(
        views=views, likes=views // 20, comments=views // 200,
        followers=10_000, posted_at=NOW - timedelta(hours=hours_old),
        platform="youtube",
    )
    video = DiscoveredVideo("youtube", vid, f"https://y/{vid}", metrics=metrics)
    video.score = viral_score(metrics, now=NOW)
    return video


@pytest.fixture
def catalogue():
    return [
        _video("old_big", 5_000_000, 720, 0),
        _video("new_fast", 400_000, 6, 0),
        _video("mid", 900_000, 100, 0),
    ]


def test_sort_by_views(catalogue):
    assert sort_results(catalogue, SortOrder.VIEWS, now=NOW)[0].video_id == "old_big"


def test_sort_by_newest(catalogue):
    assert sort_results(catalogue, SortOrder.NEWEST, now=NOW)[0].video_id == "new_fast"


def test_sort_by_fastest_growing(catalogue):
    """400k in 6 hours beats 5M over a month."""
    fastest = sort_results(catalogue, SortOrder.FASTEST_GROWING, now=NOW)
    assert fastest[0].video_id == "new_fast"


def test_sort_by_viral_score_is_ordered(catalogue):
    ranked = sort_results(catalogue, SortOrder.VIRAL_SCORE, now=NOW)
    scores = [v.score.score for v in ranked]
    assert scores == sorted(scores, reverse=True)


def test_videos_missing_the_sort_key_fall_to_the_end(catalogue):
    blank = DiscoveredVideo("youtube", "blank", "https://y/blank")
    ranked = sort_results(catalogue + [blank], SortOrder.VIEWS, now=NOW)
    assert ranked[-1].video_id == "blank"
