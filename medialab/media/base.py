"""The interface every source implements, and the safety net around it.

A provider is one `search`. It never raises for "found nothing" -- an empty
list is a normal answer. It may raise on transport failure, and `safe_search`
turns that into an empty list too, because the product rule is that no single
source can cost a generation. Twelve providers means twelve chances to be
down, rate-limited, unconfigured or slow, and the video has to be made anyway.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import httpx

from medialab.media.types import MediaAsset, MediaType, ProviderStatus, SearchContext

logger = logging.getLogger("medialab.media")

#: Short on purpose. A slow archive must not hold up the eleven sources that
#: already answered; the aggregator waits for all of them at once.
DEFAULT_TIMEOUT = httpx.Timeout(12.0, connect=5.0)

#: One retry, and only for the errors a retry can fix. Anything else is a
#: source that is not going to answer this time.
MAX_ATTEMPTS = 2
_RETRY_STATUS = {429, 500, 502, 503, 504}


class MediaProvider(ABC):
    """One searchable source of reusable media."""

    #: Short stable id used in routing tables, logs and asset records.
    name: str = ""
    #: What this source can return. Most archives are stills only.
    media_types: tuple[MediaType, ...] = (MediaType.IMAGE,)
    #: Env var that enables it, when one is needed at all.
    env_var: str = ""

    @abstractmethod
    async def search(
        self, client: httpx.AsyncClient, query: str,
        media_type: MediaType, context: SearchContext,
    ) -> list[MediaAsset]:
        """Candidates for one query. `[]` when there are none."""

    def status(self) -> ProviderStatus:
        """Whether this source can be used on this deployment."""
        return ProviderStatus(self.name, True)

    def supports(self, media_type: MediaType) -> bool:
        return media_type in self.media_types

    async def safe_search(
        self, client: httpx.AsyncClient, query: str,
        media_type: MediaType, context: SearchContext,
    ) -> list[MediaAsset]:
        """`search` that cannot fail the caller.

        Errors are logged at info and swallowed. A customer never sees a
        provider name, a status code or a quota message: from their side the
        video simply drew on the sources that were answering.
        """
        if not self.supports(media_type) or not self.status().available:
            return []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                found = await self.search(client, query, media_type, context)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRY_STATUS and attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.6 * attempt)
                    continue
                logger.info(
                    f"[media] {self.name} declined {query!r} "
                    f"({exc.response.status_code}); skipping this source"
                )
                return []
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.4 * attempt)
                    continue
                logger.info(
                    f"[media] {self.name} unreachable for {query!r} "
                    f"({type(exc).__name__}); skipping this source"
                )
                return []
            except Exception as exc:      # noqa: BLE001 - never fatal
                logger.info(
                    f"[media] {self.name} failed on {query!r} "
                    f"({type(exc).__name__}: {str(exc)[:120]}); skipping"
                )
                return []
            for asset in found:
                asset.search_query = query
            return found[: max(1, context.per_provider)]
        return []


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]
