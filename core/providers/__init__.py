"""Every source, asked at once, answering in one shape.

    from core.providers import search_media, search_research

    found = await search_media("abandoned lighthouse at dusk")
    for item in found.items:
        print(item.provider, item.licence, item.credit_line())

What this does
--------------
Runs each configured provider concurrently, normalises what comes back, and
reports what each one did. Providers are independent: one being unconfigured,
rate-limited or broken costs its own results and nothing else, which is the
whole reason for asking several.

What it deliberately does not do
--------------------------------
Replace the Pexels path in `core.image_sourcer`. That path has a query ladder,
relevance ranking, cross-beat de-duplication and subject-aware cropping, all
measured against real runs. This is a uniform way to reach every source,
including Pexels; the sourcing pipeline keeps its own.

Ordering
--------
Results come back ranked by how well the source suits this pipeline, not by
whichever provider answered first. `PREFERENCE` encodes that: a provider whose
licence imposes no obligation sorts above one that does, and a portrait image
sorts above a landscape one because the output is 9:16. Within a provider the
provider's own ordering is preserved, since it ranked for relevance and we
have no better signal.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from core.providers.base import (
    Availability,
    MediaItem,
    MediaKind,
    ProviderResult,
    ProviderStatus,
    RateLimiter,
    ResearchItem,
    requires_attribution,
    with_retries,
)
from core.providers.media import (
    CommonsProvider,
    PexelsProvider,
    PixabayProvider,
    UnsplashProvider,
)
from core.providers.research import NewsApiProvider, YouTubeResearchProvider

__all__ = [
    "Availability",
    "CommonsProvider",
    "MediaItem",
    "MediaKind",
    "MediaSearch",
    "NewsApiProvider",
    "PexelsProvider",
    "PixabayProvider",
    "ProviderResult",
    "ProviderStatus",
    "RateLimiter",
    "ResearchItem",
    "ResearchSearch",
    "UnsplashProvider",
    "YouTubeResearchProvider",
    "availability_report",
    "default_media_providers",
    "default_research_providers",
    "requires_attribution",
    "search_media",
    "search_research",
    "with_retries",
]

logger = logging.getLogger("video_factory")

DEFAULT_TIMEOUT = 45.0

#: Sorts undated research to the end.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Lower sorts first. Pexels leads because the rest of the pipeline is already
#: built around it and its licence asks nothing of us; Commons is last of the
#: media sources because its files are the most varied in quality and the most
#: likely to carry an attribution obligation.
PREFERENCE = {
    "pexels": 0,
    "pixabay": 1,
    "unsplash": 2,
    "wikimedia_commons": 3,
}


def default_media_providers() -> list:
    return [
        PexelsProvider(),
        PixabayProvider(),
        UnsplashProvider(),
        CommonsProvider(),
    ]


def default_research_providers() -> list:
    return [YouTubeResearchProvider(), NewsApiProvider()]


@dataclass
class MediaSearch:
    """Everything the media providers returned, and how each of them fared."""

    items: list[MediaItem] = field(default_factory=list)
    results: list[ProviderResult] = field(default_factory=list)

    @property
    def providers_used(self) -> list[str]:
        return [r.provider for r in self.results if r.ok and r.media]

    @property
    def failures(self) -> dict[str, str]:
        return {r.provider: r.error for r in self.results if r.error}

    def by_provider(self, provider: str) -> list[MediaItem]:
        return [item for item in self.items if item.provider == provider]

    def to_record(self) -> dict:
        return {
            "items": [item.to_record() for item in self.items],
            "providers": [
                {
                    "provider": r.provider,
                    "count": len(r.media),
                    "error": r.error,
                    "status": r.status.to_record() if r.status else None,
                }
                for r in self.results
            ],
        }


@dataclass
class ResearchSearch:
    items: list[ResearchItem] = field(default_factory=list)
    results: list[ProviderResult] = field(default_factory=list)

    @property
    def failures(self) -> dict[str, str]:
        return {r.provider: r.error for r in self.results if r.error}

    def by_provider(self, provider: str) -> list[ResearchItem]:
        return [item for item in self.items if item.provider == provider]

    def to_record(self) -> dict:
        return {
            "items": [item.to_record() for item in self.items],
            "providers": [
                {
                    "provider": r.provider,
                    "count": len(r.research),
                    "error": r.error,
                    "status": r.status.to_record() if r.status else None,
                }
                for r in self.results
            ],
        }


def _rank(item: MediaItem) -> tuple:
    """Preferred provider first, portrait before landscape, then as returned."""
    return (
        PREFERENCE.get(item.provider, 99),
        0 if item.is_portrait else 1,
        0 if not item.needs_attribution else 1,
    )


async def _run_media(provider, query: str, client, limit: int, kwargs) -> ProviderResult:
    """One provider's attempt, with its failure captured rather than raised."""
    result = ProviderResult(provider=provider.name, status=provider.status())
    if not result.status.usable:
        return result
    try:
        result.media = await provider.search(
            query, client=client, limit=limit, **kwargs)
    except Exception as exc:
        result.error = str(exc)[:300]
        logger.warning(f"[{provider.name}] search failed: {result.error}")
    return result


async def _run_research(provider, query: str, client, limit: int, kwargs) -> ProviderResult:
    result = ProviderResult(provider=provider.name, status=provider.status())
    if not result.status.usable:
        return result
    try:
        result.research = await provider.search(
            query, client=client, limit=limit, **kwargs)
    except Exception as exc:
        result.error = str(exc)[:300]
        logger.warning(f"[{provider.name}] research failed: {result.error}")
    return result


async def search_media(
    query: str,
    *,
    providers: list | None = None,
    client: httpx.AsyncClient | None = None,
    limit: int = 10,
    kind: MediaKind = MediaKind.PHOTO,
    orientation: str = "portrait",
    minimum: int = 0,
    **kwargs,
) -> MediaSearch:
    """Ask every configured media source at once.

    `minimum` is the fallback lever: when the preferred providers return fewer
    items than that, the remaining ones are asked with a larger limit rather
    than the caller being handed a thin set. Set it to 0 to just take what one
    round returns.
    """
    chosen = providers if providers is not None else default_media_providers()
    owns = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True)

    try:
        call_kwargs = {"kind": kind, "orientation": orientation, **kwargs}
        results = list(await asyncio.gather(*[
            _run_media(provider, query, http, limit, call_kwargs)
            for provider in chosen
        ]))

        items = [item for result in results for item in result.media]

        if minimum and len(items) < minimum:
            # Nothing to widen to if every provider already answered, so this
            # only fires when some were unconfigured or failed.
            short_by = minimum - len(items)
            logger.info(
                f"media search for {query!r} returned {len(items)}; "
                f"retrying the sources that came back empty for {short_by} more"
            )
            retryable = [
                provider for provider, result in zip(chosen, results)
                if result.ok and not result.media and result.status
                and result.status.usable
            ]
            if retryable:
                extra = list(await asyncio.gather(*[
                    _run_media(provider, query, http, limit + short_by, call_kwargs)
                    for provider in retryable
                ]))
                for result in extra:
                    items.extend(result.media)
                results.extend(extra)

        items.sort(key=_rank)
        return MediaSearch(items=items, results=results)
    finally:
        if owns:
            await http.aclose()


async def search_research(
    query: str,
    *,
    providers: list | None = None,
    client: httpx.AsyncClient | None = None,
    limit: int = 10,
    **kwargs,
) -> ResearchSearch:
    """Ask every configured research source at once.

    Everything returned is reference material: a link, a headline and the
    provider's own summary. Nothing here is a file to republish.
    """
    chosen = providers if providers is not None else default_research_providers()
    owns = client is None
    http = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True)

    try:
        results = list(await asyncio.gather(*[
            _run_research(provider, query, http, limit, kwargs)
            for provider in chosen
        ]))
        items = [item for result in results for item in result.research]
        # Newest first, undated last: research a year old is rarely what a
        # script about a current subject needs.
        items.sort(key=lambda i: i.published_at or _EPOCH, reverse=True)
        return ResearchSearch(items=items, results=results)
    finally:
        if owns:
            await http.aclose()


def availability_report() -> list[dict]:
    """What each source can do on this deployment, and what it needs.

    Rendered at startup and in diagnostics, so a missing key is visible as a
    missing key rather than as a source that mysteriously returns nothing.
    """
    providers = default_media_providers() + default_research_providers()
    return [provider.status().to_record() for provider in providers]
