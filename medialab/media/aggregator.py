"""One search across the right sources, filtered down to what is usable.

The pipeline this feeds is unchanged: it still fetches several candidates,
still ranks them with the vision call, still rejects near-duplicates, still
picks for diversity. What changes is the size and quality of the pool those
steps get to work with -- twelve sources instead of two, and correctly
identified pictures of real subjects instead of only generic stock.

The order here is deliberate. Licence filtering happens *before* the
expensive vision ranking, because ranking an asset that can never be used is
money spent on nothing. Exact-duplicate removal happens before perceptual
work, because comparing URLs is free and comparing pixels is not.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from medialab.media import licensing, router
from medialab.media.base import DEFAULT_TIMEOUT, MediaProvider
from medialab.media.heritage import Europeana, LibraryOfCongress, Smithsonian
from medialab.media.open import InternetArchive, MetMuseum, Openverse, WikimediaCommons
from medialab.media.science import NASA, NOAA
from medialab.media.stock import Pexels, Pixabay, Unsplash
from medialab.media.types import MediaAsset, MediaType, ProviderStatus, SearchContext

logger = logging.getLogger("medialab.media")

__all__ = [
    "PROVIDERS", "provider_status", "available_providers", "search",
    "SearchResult",
]

#: Every source, by name. Instantiated once: they are stateless and cheap.
PROVIDERS: dict[str, MediaProvider] = {
    p.name: p for p in (
        Pexels(), Pixabay(), Unsplash(),
        Openverse(), WikimediaCommons(), InternetArchive(), MetMuseum(),
        LibraryOfCongress(), Smithsonian(), Europeana(),
        NASA(), NOAA(),
    )
}

#: How many providers to have in flight at once. High enough that a route
#: finishes in one wave, low enough not to look like a scraper.
_CONCURRENCY = 6


class SearchResult:
    """What one aggregated search produced, and what it threw away and why."""

    def __init__(self) -> None:
        self.assets: list[MediaAsset] = []
        self.refused: list[tuple[MediaAsset, str]] = []
        self.searched: list[str] = []
        self.failed: list[str] = []
        self.subject: str = ""

    @property
    def by_provider(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for asset in self.assets:
            counts[asset.provider] = counts.get(asset.provider, 0) + 1
        return counts

    def summary(self) -> str:
        kept = ", ".join(f"{k} {v}" for k, v in sorted(self.by_provider.items()))
        return (
            f"{len(self.assets)} usable from [{kept or 'none'}]; "
            f"{len(self.refused)} refused on licence; "
            f"{len(self.failed)} source(s) unavailable"
        )


def provider_status() -> list[ProviderStatus]:
    """Every source and whether this deployment can use it.

    Reported at startup so an operator can see what is switched on without
    reading the environment, and so a missing optional key is visible as a
    fact rather than as a source that quietly never returns anything.
    """
    return [PROVIDERS[name].status() for name in sorted(PROVIDERS)]


def available_providers() -> set[str]:
    return {s.name for s in provider_status() if s.available}


def log_status() -> None:
    ready = [s for s in provider_status() if s.available]
    missing = [s for s in provider_status() if not s.available]
    logger.info(f"[media] {len(ready)}/{len(PROVIDERS)} sources available")
    for status in ready:
        logger.info(f"[media]   + {status}")
    for status in missing:
        logger.info(f"[media]   - {status}")


async def search(
    query: str,
    *,
    media_type: MediaType = MediaType.IMAGE,
    context: SearchContext | None = None,
    providers: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> SearchResult:
    """Search the sources that suit this subject, concurrently.

    `providers` overrides the router, which is what the tests and the
    diagnostic script use; normally the subject decides.
    """
    context = context or SearchContext()
    result = SearchResult()
    result.subject = router.classify(query, says=context.says)

    names = providers or router.route_for(
        query, says=context.says, available=available_providers()
    )
    names = [n for n in names if n in PROVIDERS and PROVIDERS[n].supports(media_type)]
    if not names:
        logger.info(f"[media] no configured source can answer {query!r}")
        return result

    owns = client is None
    client = client or httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT, follow_redirects=True
    )
    limiter = asyncio.Semaphore(_CONCURRENCY)

    broader = router.core_query(query)

    async def one(name: str) -> tuple[str, list[MediaAsset]]:
        async with limiter:
            found = await PROVIDERS[name].safe_search(
                client, query, media_type, context
            )
            if found or not broader:
                return name, found
            # Exactly one retry, and only for a source that found nothing.
            # The archives match every word literally, so a phrasing that
            # works on a stock library can return zero from them while the
            # same subject is sitting there under a plainer title.
            retried = await PROVIDERS[name].safe_search(
                client, broader, media_type, context
            )
            if retried:
                logger.debug(
                    f"[media] {name}: nothing for {query!r}, "
                    f"{len(retried)} for {broader!r}"
                )
            return name, retried

    try:
        gathered = await asyncio.gather(*(one(n) for n in names))
    finally:
        if owns:
            await client.aclose()

    # Rank order follows the route: an earlier lane is the better source for
    # this subject, so its results lead when everything else is equal.
    priority = {name: i for i, name in enumerate(names)}
    found: list[MediaAsset] = []
    for name, assets in gathered:
        result.searched.append(name)
        if not assets:
            result.failed.append(name)
        found.extend(assets)
    found.sort(key=lambda a: priority.get(a.provider, 99))

    deduped = _drop_exact_duplicates(found)
    usable, refused = licensing.filter_usable(deduped, commercial=context.commercial)
    result.assets = usable
    result.refused = refused

    for asset, reason in refused:
        logger.debug(f"[media] refused {asset.key}: {reason}")
    logger.info(f"[media] {query!r} ({result.subject}): {result.summary()}")
    return result


def _drop_exact_duplicates(assets: list[MediaAsset]) -> list[MediaAsset]:
    """Same asset id, or the same bytes URL, from more than one lane.

    Cheap and exact. Perceptual near-duplicate removal stays where it already
    is, in `medialab.fingerprint`, once the files are on disk -- there is no
    point downloading two copies of one photograph to discover they match.
    """
    seen_keys: set[str] = set()
    seen_urls: set[str] = set()
    out: list[MediaAsset] = []
    for asset in assets:
        url = (asset.download_url or "").split("?")[0].lower()
        if asset.key in seen_keys or (url and url in seen_urls):
            continue
        seen_keys.add(asset.key)
        if url:
            seen_urls.add(url)
        out.append(asset)
    return out
