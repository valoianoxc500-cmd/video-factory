"""Unified media search across every source we can legally draw from.

Twelve providers behind one `search`, routed by subject, filtered by a central
licence policy, and normalised to one asset shape. Everything downstream --
the vision ranking, the perceptual deduplication, the diversity selection --
is unchanged and now works on a much larger and much better pool.

    from medialab.media import search, MediaType, SearchContext

    result = await search(
        "Real Madrid stadium",
        media_type=MediaType.IMAGE,
        context=SearchContext(portrait=True, commercial=True),
    )

`result.assets` is what may be used; `result.refused` is what could not be,
each with the reason. Nothing reaches the renderer without a defensible reuse
basis recorded against it.
"""

from medialab.media.aggregator import (
    PROVIDERS,
    SearchResult,
    available_providers,
    log_status,
    provider_status,
    search,
)
from medialab.media.licensing import evaluate, filter_usable, normalise
from medialab.media.router import classify, route_for
from medialab.media.types import (
    MediaAsset,
    MediaType,
    ProviderStatus,
    SearchContext,
)

__all__ = [
    "search", "SearchResult", "PROVIDERS", "provider_status",
    "available_providers", "log_status",
    "MediaAsset", "MediaType", "SearchContext", "ProviderStatus",
    "classify", "route_for",
    "normalise", "evaluate", "filter_usable",
]
