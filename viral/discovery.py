"""Finding trending short-form video, through official APIs only.

The constraint that shapes this module
--------------------------------------
The brief asks to discover trending videos with views, likes and follower
counts across TikTok, Instagram, YouTube, Facebook and Snapchat. Only one of
those can be done lawfully today with a public API:

  YouTube    Data API v3 search + videos.list gives real view/like/comment
             counts for arbitrary public videos. Fully supported. Implemented.
  TikTok     The Research API returns public video data but is granted case by
             case (largely academic/non-profit) and is region-limited. The
             Display API only ever returns the authenticated user's own
             videos. Neither supports open trend discovery for a SaaS.
  Instagram  The Graph API exposes the authenticated business account and
             hashtag search with severe limits. There is no endpoint that
             returns arbitrary trending Reels with metrics.
  Facebook   Same Graph API position as Instagram.
  Snapchat   No public content-discovery API at all.

The way to get that data anyway is to scrape, which breaks those platforms'
terms and would put the operator's own accounts at risk. So this module is
built as a provider interface with one real implementation and explicit,
declared gaps -- a provider that has no lawful path reports `unavailable` with
the reason, and the UI shows that instead of silently returning nothing or
quietly filling the gap by scraping.

Adding a provider later is implementing `DiscoveryProvider` and registering
it. Nothing else changes.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from viral.scoring import ScoreBreakdown, VideoMetrics, viral_score

logger = logging.getLogger("viral.discovery")


class Availability(str, Enum):
    READY = "ready"                    # credentials present, provider usable
    NEEDS_CREDENTIALS = "needs_credentials"
    NO_PUBLIC_API = "no_public_api"    # no lawful endpoint exists
    RESTRICTED_API = "restricted_api"  # exists, but access is gated


@dataclass
class ProviderStatus:
    platform: str
    availability: Availability
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.availability is Availability.READY


@dataclass
class DiscoveredVideo:
    """One candidate, with its metrics and score. Reference material only."""

    platform: str
    video_id: str
    url: str
    title: str = ""
    author: str = ""
    thumbnail_url: str = ""
    metrics: VideoMetrics = field(default_factory=VideoMetrics)
    niche: str = ""
    language: str = ""
    score: ScoreBreakdown | None = None

    def to_record(self) -> dict:
        return {
            "platform": self.platform,
            "video_id": self.video_id,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "thumbnail_url": self.thumbnail_url,
            "niche": self.niche,
            "language": self.language,
            "views": self.metrics.views,
            "likes": self.metrics.likes,
            "comments": self.metrics.comments,
            "shares": self.metrics.shares,
            "followers": self.metrics.followers,
            "posted_at": (
                self.metrics.posted_at.isoformat() if self.metrics.posted_at else None
            ),
            "duration_seconds": self.metrics.duration_seconds,
            "viral_score": self.score.score if self.score else None,
            "score_confidence": self.score.confidence.value if self.score else None,
            # Discovery output is never a publishing input; see viral.rights.
            "usage": "analysis_only",
        }


@dataclass
class SearchQuery:
    niche: str = ""
    keyword: str = ""
    language: str = ""
    platforms: tuple[str, ...] = ()
    max_results: int = 25
    #: Only consider posts newer than this, in days. Trends decay fast.
    max_age_days: int = 30


class SortOrder(str, Enum):
    VIRAL_SCORE = "viral_score"
    NEWEST = "newest"
    FASTEST_GROWING = "fastest_growing"
    VIEWS = "views"


class DiscoveryProvider(Protocol):
    platform: str

    def status(self) -> ProviderStatus: ...

    def search(self, query: SearchQuery) -> list[DiscoveredVideo]: ...


# ---------------------------------------------------------------------------
# Niches
# ---------------------------------------------------------------------------

#: Search expansions per niche. Keeps a niche button meaningful rather than
#: passing the label straight through as a keyword.
NICHES: dict[str, tuple[str, ...]] = {
    "police_chase": ("police chase", "pursuit dashcam", "police pursuit"),
    "football": ("football skills", "soccer goal", "football highlights"),
    "cars": ("car build", "supercar", "car mods"),
    "stories": ("true story", "storytime", "reddit story"),
    "fitness": ("workout", "gym transformation", "fitness tips"),
    "business": ("business tips", "entrepreneur", "side hustle"),
    "animals": ("animal rescue", "pet", "wildlife"),
    "food": ("recipe", "street food", "cooking"),
}


def expand_niche(niche: str, keyword: str = "") -> str:
    """Build the search term for a niche, optionally narrowed by a keyword."""
    slug = re.sub(r"[^a-z0-9_]+", "_", str(niche or "").strip().lower())
    terms = NICHES.get(slug, ())
    base = terms[0] if terms else slug.replace("_", " ")
    keyword = " ".join(str(keyword or "").split())
    if keyword and base:
        return f"{base} {keyword}".strip()
    return (keyword or base).strip()


# ---------------------------------------------------------------------------
# YouTube: the one provider with a lawful path to open discovery
# ---------------------------------------------------------------------------

_ISO_DURATION = re.compile(
    r"P(?:(?P<d>\d+)D)?T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?"
)


def parse_iso8601_duration(value: str) -> float | None:
    """Seconds from YouTube's ISO-8601 duration, or None if unparseable."""
    match = _ISO_DURATION.fullmatch(str(value or "").strip())
    if not match:
        return None
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return float(
        parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]
    )


class YouTubeDiscovery:
    """Search public YouTube Shorts via the official Data API v3.

    Two calls per search: `search.list` for candidates, then `videos.list` for
    statistics, because search results carry no view counts. Channel follower
    counts need a third call and are fetched only for the shortlist, since
    quota is the binding constraint on this API.
    """

    platform = "youtube"
    SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
    VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
    CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

    def __init__(self, api_key: str | None = None, client=None) -> None:
        self._api_key = api_key or os.environ.get("YOUTUBE_API_KEY", "")
        self._client = client

    def status(self) -> ProviderStatus:
        if not self._api_key:
            return ProviderStatus(
                self.platform, Availability.NEEDS_CREDENTIALS,
                "Set YOUTUBE_API_KEY to a YouTube Data API v3 key.",
            )
        return ProviderStatus(self.platform, Availability.READY)

    def _http(self):
        if self._client is not None:
            return self._client
        import httpx

        return httpx.Client(timeout=30.0, follow_redirects=True)

    def search(self, query: SearchQuery) -> list[DiscoveredVideo]:
        if not self.status().usable:
            return []

        term = expand_niche(query.niche, query.keyword)
        if not term:
            return []
        published_after = None
        if query.max_age_days:
            cutoff = datetime.now(timezone.utc).timestamp() - query.max_age_days * 86400
            published_after = datetime.fromtimestamp(
                cutoff, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {
            "key": self._api_key,
            "part": "snippet",
            "q": term,
            "type": "video",
            "videoDuration": "short",       # Shorts-length only
            "order": "viewCount",
            "maxResults": str(max(1, min(query.max_results, 50))),
        }
        if query.language:
            params["relevanceLanguage"] = query.language
        if published_after:
            params["publishedAfter"] = published_after

        client = self._http()
        try:
            found = client.get(self.SEARCH_URL, params=params)
            found.raise_for_status()
            items = found.json().get("items", [])
        except Exception as exc:
            logger.warning(f"youtube search failed: {exc}")
            return []

        ids = [
            i["id"]["videoId"] for i in items
            if isinstance(i.get("id"), dict) and i["id"].get("videoId")
        ]
        if not ids:
            return []

        try:
            detail = client.get(self.VIDEOS_URL, params={
                "key": self._api_key,
                "part": "statistics,contentDetails,snippet",
                "id": ",".join(ids[:50]),
            })
            detail.raise_for_status()
            details = detail.json().get("items", [])
        except Exception as exc:
            logger.warning(f"youtube statistics lookup failed: {exc}")
            return []

        followers = self._channel_followers(
            client, [d.get("snippet", {}).get("channelId") for d in details]
        )

        results: list[DiscoveredVideo] = []
        for item in details:
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            posted = snippet.get("publishedAt")
            metrics = VideoMetrics(
                views=_as_int(stats.get("viewCount")),
                likes=_as_int(stats.get("likeCount")),
                comments=_as_int(stats.get("commentCount")),
                # YouTube does not expose share counts on this endpoint.
                shares=None,
                followers=followers.get(snippet.get("channelId")),
                posted_at=_as_datetime(posted),
                duration_seconds=parse_iso8601_duration(
                    item.get("contentDetails", {}).get("duration", "")
                ),
                platform=self.platform,
            )
            video = DiscoveredVideo(
                platform=self.platform,
                video_id=item.get("id", ""),
                url=f"https://www.youtube.com/watch?v={item.get('id', '')}",
                title=snippet.get("title", ""),
                author=snippet.get("channelTitle", ""),
                thumbnail_url=(
                    snippet.get("thumbnails", {}).get("high", {}).get("url", "")
                ),
                metrics=metrics,
                niche=query.niche,
                language=query.language,
            )
            video.score = viral_score(metrics)
            results.append(video)
        return results

    def _channel_followers(self, client, channel_ids) -> dict[str, int]:
        ids = [c for c in dict.fromkeys(channel_ids) if c][:50]
        if not ids:
            return {}
        try:
            resp = client.get(self.CHANNELS_URL, params={
                "key": self._api_key,
                "part": "statistics",
                "id": ",".join(ids),
            })
            resp.raise_for_status()
        except Exception as exc:
            logger.info(f"youtube channel statistics unavailable: {exc}")
            return {}
        out: dict[str, int] = {}
        for item in resp.json().get("items", []):
            count = _as_int(item.get("statistics", {}).get("subscriberCount"))
            if count is not None:
                out[item.get("id", "")] = count
        return out


class UnavailableProvider:
    """A platform with no lawful discovery path, declared rather than faked.

    Returning an empty list from a provider that looks implemented reads as
    "nothing is trending". This reports why the platform cannot be searched so
    the interface can say so.
    """

    def __init__(self, platform: str, availability: Availability, reason: str):
        self.platform = platform
        self._availability = availability
        self._reason = reason

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.platform, self._availability, self._reason)

    def search(self, query: SearchQuery) -> list[DiscoveredVideo]:
        return []


def default_providers() -> list:
    """Every provider, with the honest status of each."""
    return [
        YouTubeDiscovery(),
        UnavailableProvider(
            "tiktok", Availability.RESTRICTED_API,
            "TikTok's Research API is granted case by case and is not open to "
            "commercial SaaS; the Display API returns only the authenticated "
            "user's own videos. Connect an account to analyse your own posts.",
        ),
        UnavailableProvider(
            "instagram", Availability.RESTRICTED_API,
            "The Instagram Graph API exposes your own business account and "
            "limited hashtag search. There is no endpoint for arbitrary "
            "trending Reels with metrics.",
        ),
        UnavailableProvider(
            "facebook", Availability.RESTRICTED_API,
            "The Facebook Graph API covers Pages you manage, not open "
            "discovery across the platform.",
        ),
        UnavailableProvider(
            "snapchat", Availability.NO_PUBLIC_API,
            "Snapchat publishes no content-discovery API.",
        ),
    ]


def sort_results(
    videos: list[DiscoveredVideo],
    order: SortOrder,
    *,
    now: datetime | None = None,
) -> list[DiscoveredVideo]:
    """Order results. Videos missing the sort key fall to the end."""
    def score_key(v: DiscoveredVideo):
        return (v.score.score if v.score else -1)

    def newest_key(v: DiscoveredVideo):
        posted = v.metrics.posted_at
        return posted.timestamp() if posted else float("-inf")

    def velocity_key(v: DiscoveredVideo):
        age = v.metrics.age_hours(now)
        if not age or not v.metrics.views:
            return float("-inf")
        return v.metrics.views / age

    def views_key(v: DiscoveredVideo):
        return v.metrics.views if v.metrics.views is not None else -1

    keys = {
        SortOrder.VIRAL_SCORE: score_key,
        SortOrder.NEWEST: newest_key,
        SortOrder.FASTEST_GROWING: velocity_key,
        SortOrder.VIEWS: views_key,
    }
    return sorted(videos, key=keys[order], reverse=True)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
