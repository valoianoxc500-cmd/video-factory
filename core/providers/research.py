"""The research sources: things to read about a subject.

Neither of these is a media source, and treating them as one is the mistake
this module exists to prevent.

  YouTube   The Data API returns titles, descriptions, statistics and a
            thumbnail. It never returns the video file, and obtaining one
            another way breaks the Terms of Service. Results are reference
            material for a script, not footage for it.
  NewsAPI   Returns a headline, a short description and a link. The article
            body belongs to the publisher; NewsAPI's own terms require linking
            to the original rather than reproducing it. Only the summary the
            API itself provides is kept, and it is kept as a citation.

Both therefore produce `ResearchItem`, which has no media URL to misuse.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from core.providers.base import (
    Availability,
    ProviderStatus,
    RateLimiter,
    ResearchItem,
    credential,
    with_retries,
)

logger = logging.getLogger("video_factory")


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# YouTube Data API v3
# ---------------------------------------------------------------------------

class YouTubeResearchProvider:
    """Public videos on a subject, with their real engagement numbers.

    Two calls, because search results carry no statistics: `search.list` for
    candidates and `videos.list` for the numbers. Quota is the binding
    constraint on this API -- 100 units per search against a default 10,000 a
    day -- so the second call is made once for the whole shortlist rather than
    once per video.
    """

    name = "youtube"
    SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
    VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key if api_key is not None else credential("youtube_api_key")
        self._limiter = RateLimiter(concurrency=2, min_interval=0.2)

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "Set YOUTUBE_API_KEY to a YouTube Data API v3 key.",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def search(
        self,
        query: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 10,
        language: str = "",
        **_,
    ) -> list[ResearchItem]:
        if not self.status().usable or not query.strip():
            return []

        params = {
            "key": self._key,
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "relevance",
            "maxResults": str(max(1, min(limit, 50))),
        }
        if language:
            params["relevanceLanguage"] = language

        async def find():
            response = await client.get(self.SEARCH_URL, params=params)
            response.raise_for_status()
            return response.json() or {}

        async with self._limiter:
            found = await with_retries(find, provider=self.name)

        ids = [
            item["id"]["videoId"]
            for item in (found.get("items") or [])
            if isinstance(item.get("id"), dict) and item["id"].get("videoId")
        ]
        if not ids:
            return []

        async def detail():
            response = await client.get(self.VIDEOS_URL, params={
                "key": self._key,
                "part": "snippet,statistics",
                "id": ",".join(ids[:50]),
            })
            response.raise_for_status()
            return response.json() or {}

        async with self._limiter:
            details = await with_retries(detail, provider=self.name)

        items: list[ResearchItem] = []
        for entry in details.get("items") or []:
            snippet = entry.get("snippet") or {}
            stats = entry.get("statistics") or {}
            video_id = str(entry.get("id") or "")
            items.append(ResearchItem(
                provider=self.name,
                provider_id=video_id,
                title=str(snippet.get("title") or ""),
                url=f"https://www.youtube.com/watch?v={video_id}",
                source=str(snippet.get("channelTitle") or ""),
                # The API's own description field, not a transcript.
                summary=str(snippet.get("description") or "")[:1000],
                author=str(snippet.get("channelTitle") or ""),
                published_at=_parse_time(snippet.get("publishedAt")),
                thumbnail_url=str(
                    ((snippet.get("thumbnails") or {}).get("high") or {}).get("url") or ""
                ),
                metrics={
                    "views": _as_int(stats.get("viewCount")),
                    "likes": _as_int(stats.get("likeCount")),
                    "comments": _as_int(stats.get("commentCount")),
                },
                query=query,
            ))
        return items


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------

class NewsApiProvider:
    """Recent news coverage of a subject.

    Only what the API itself returns is kept: headline, the publisher's own
    short description, byline, timestamp and the link. The body is not
    fetched, because it is the publisher's and NewsAPI's terms require sending
    readers to the original rather than reproducing it.

    A script quoting these should cite the outlet and the date, which is why
    both are on every item.
    """

    name = "newsapi"
    EVERYTHING_URL = "https://newsapi.org/v2/everything"

    def __init__(self, api_key: str | None = None) -> None:
        # NEWSAPI_KEY is the name already in use on this deployment; the other
        # two are the spellings people reach for. All three are accepted so a
        # working key is not ignored over a naming preference.
        self._key = api_key if api_key is not None else credential(
            "newsapi_api_key", "NEWSAPI_KEY", "NEWS_API_KEY")
        # The free tier allows 100 requests a day; there is no reason to burst.
        self._limiter = RateLimiter(concurrency=2, min_interval=0.5)

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "Set NEWSAPI_API_KEY (free tier at newsapi.org).",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def search(
        self,
        query: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 10,
        language: str = "en",
        sort_by: str = "relevancy",
        **_,
    ) -> list[ResearchItem]:
        if not self.status().usable or not query.strip():
            return []

        params = {
            "q": query,
            "pageSize": max(1, min(limit, 100)),
            "sortBy": sort_by if sort_by in {
                "relevancy", "popularity", "publishedAt"} else "relevancy",
        }
        if language:
            params["language"] = language

        async def call():
            response = await client.get(
                self.EVERYTHING_URL,
                params=params,
                # In the header rather than the query string: an API key in a
                # URL ends up in logs and referrers.
                headers={"X-Api-Key": self._key},
            )
            response.raise_for_status()
            return response.json() or {}

        async with self._limiter:
            body = await with_retries(call, provider=self.name)

        if str(body.get("status") or "") == "error":
            # NewsAPI reports quota and key problems in a 200 body as well as
            # by status code; treating that as an empty result would read as
            # "no coverage exists".
            raise RuntimeError(
                f"newsapi: {body.get('message') or body.get('code') or 'error'}"
            )

        items: list[ResearchItem] = []
        for article in body.get("articles") or []:
            url = str(article.get("url") or "")
            if not url:
                continue
            items.append(ResearchItem(
                provider=self.name,
                provider_id=url,
                title=str(article.get("title") or ""),
                url=url,
                source=str((article.get("source") or {}).get("name") or ""),
                # The publisher's own summary, as served by the API.
                summary=str(article.get("description") or "")[:1000],
                author=str(article.get("author") or ""),
                published_at=_parse_time(article.get("publishedAt")),
                thumbnail_url=str(article.get("urlToImage") or ""),
                query=query,
            ))
        return items
