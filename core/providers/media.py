"""The picture and clip sources, normalised.

Pexels is deliberately thin here. `core.image_sourcer` already has a Pexels
path with a query ladder, relevance ranking, cross-beat de-duplication and
provenance, all of it measured against real runs. This wraps the same
endpoints for callers that want one uniform interface; it does not replace
that path, and the sourcing pipeline keeps using its own.
"""

from __future__ import annotations

import logging
import re

import httpx

from core.footage_discovery import is_reusable_licence
from core.providers.base import (
    Availability,
    MediaItem,
    MediaKind,
    ProviderStatus,
    RateLimiter,
    credential,
    with_retries,
)

logger = logging.getLogger("video_factory")

#: Wikimedia's user-agent policy asks for the tool, a contact, and a way to
#: reach whoever runs it. A burst of searches from a vague agent is what their
#: robot policy returns 403 for, so this names all three.
COMMONS_USER_AGENT = (
    "FirstVideoCheck/1.0 "
    "(https://video-factory-omega.vercel.app; ibrahemxc500@gmail.com) "
    "python-httpx"
)


def _orientation_params(orientation: str) -> str:
    return orientation if orientation in {"portrait", "landscape", "square"} else ""


def _clip_words(text: str, limit: int) -> str:
    """Trim to `limit` characters without cutting a word in half."""
    words = str(text or "").split()
    out: list[str] = []
    for word in words:
        candidate = " ".join(out + [word])
        if len(candidate) > limit:
            break
        out.append(word)
    # A single word longer than the limit still has to be cut somewhere.
    return " ".join(out) or str(text or "")[:limit]


# ---------------------------------------------------------------------------
# Pexels
# ---------------------------------------------------------------------------

class PexelsProvider:
    """Pexels photos and videos.

    Pexels License: free to use commercially, no attribution required. The
    photographer is recorded regardless, because a provenance file that only
    lists what was legally compulsory is not much of a provenance file.
    """

    name = "pexels"
    PHOTOS_URL = "https://api.pexels.com/v1/search"
    VIDEOS_URL = "https://api.pexels.com/v1/videos/search"
    LICENCE = "Pexels License (free to use, no attribution required)"

    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key if api_key is not None else credential("pexels_api_key")
        self._limiter = RateLimiter(concurrency=4, min_interval=0.1)

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "Set PEXELS_API_KEY.",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def search(
        self,
        query: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 10,
        orientation: str = "portrait",
        kind: MediaKind = MediaKind.PHOTO,
        **_,
    ) -> list[MediaItem]:
        if not self.status().usable or not query.strip():
            return []

        url = self.VIDEOS_URL if kind is MediaKind.VIDEO else self.PHOTOS_URL
        params = {"query": query, "per_page": max(1, min(limit, 80))}
        if _orientation_params(orientation):
            params["orientation"] = orientation

        async def call():
            response = await client.get(
                url, params=params, headers={"Authorization": self._key})
            response.raise_for_status()
            return response.json() or {}

        async with self._limiter:
            body = await with_retries(call, provider=self.name)

        if kind is MediaKind.VIDEO:
            return [self._video(v, query) for v in (body.get("videos") or [])]
        return [self._photo(p, query) for p in (body.get("photos") or [])]

    def _photo(self, photo: dict, query: str) -> MediaItem:
        return MediaItem(
            provider=self.name,
            provider_id=str(photo.get("id") or ""),
            kind=MediaKind.PHOTO,
            url=str((photo.get("src") or {}).get("large2x")
                    or (photo.get("src") or {}).get("original") or ""),
            source_page=str(photo.get("url") or ""),
            licence=self.LICENCE,
            attribution=str(photo.get("photographer") or ""),
            title=str(photo.get("alt") or ""),
            width=int(photo.get("width") or 0),
            height=int(photo.get("height") or 0),
            preview_url=str((photo.get("src") or {}).get("medium") or ""),
            query=query,
        )

    def _video(self, video: dict, query: str) -> MediaItem:
        files = sorted(
            video.get("video_files") or [],
            key=lambda f: int(f.get("width") or 0),
            reverse=True,
        )
        best = files[0] if files else {}
        return MediaItem(
            provider=self.name,
            provider_id=str(video.get("id") or ""),
            kind=MediaKind.VIDEO,
            url=str(best.get("link") or ""),
            source_page=str(video.get("url") or ""),
            licence=self.LICENCE,
            attribution=str((video.get("user") or {}).get("name") or ""),
            width=int(best.get("width") or video.get("width") or 0),
            height=int(best.get("height") or video.get("height") or 0),
            duration_seconds=float(video.get("duration") or 0.0),
            preview_url=str(video.get("image") or ""),
            query=query,
        )


# ---------------------------------------------------------------------------
# Pixabay
# ---------------------------------------------------------------------------

class PixabayProvider:
    """Pixabay photos and videos.

    Pixabay Content License: free for commercial use, no attribution required.
    Their terms do forbid hot-linking images from pixabay.com, so the URL here
    is one to download and store, never one to embed -- which is what the rest
    of the pipeline does with it anyway.
    """

    name = "pixabay"
    PHOTOS_URL = "https://pixabay.com/api/"
    VIDEOS_URL = "https://pixabay.com/api/videos/"
    LICENCE = "Pixabay Content License (free to use, no attribution required)"

    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key if api_key is not None else credential("pixabay_api_key")
        # Pixabay asks for no more than roughly 100 requests a minute.
        self._limiter = RateLimiter(concurrency=3, min_interval=0.6)

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "Set PIXABAY_API_KEY (free from pixabay.com/api/docs).",
            )
        return ProviderStatus(self.name, Availability.READY)

    async def search(
        self,
        query: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 10,
        orientation: str = "portrait",
        kind: MediaKind = MediaKind.PHOTO,
        **_,
    ) -> list[MediaItem]:
        if not self.status().usable or not query.strip():
            return []

        params = {
            "key": self._key,
            # Pixabay rejects a query over 100 characters with a bare 400, and
            # the briefs this is called with are whole sentences. Truncated on
            # a word boundary so the query stays meaningful rather than
            # ending mid-word.
            "q": _clip_words(query, 100),
            # Pixabay rejects per_page below 3.
            "per_page": max(3, min(limit, 200)),
            "safesearch": "true",
        }
        if kind is MediaKind.PHOTO:
            params["image_type"] = "photo"
            if orientation in {"portrait", "horizontal"}:
                params["orientation"] = (
                    "vertical" if orientation == "portrait" else "horizontal"
                )

        url = self.VIDEOS_URL if kind is MediaKind.VIDEO else self.PHOTOS_URL

        async def call():
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json() or {}

        async with self._limiter:
            body = await with_retries(call, provider=self.name)

        hits = body.get("hits") or []
        if kind is MediaKind.VIDEO:
            return [self._video(hit, query) for hit in hits]
        return [self._photo(hit, query) for hit in hits]

    def _photo(self, hit: dict, query: str) -> MediaItem:
        return MediaItem(
            provider=self.name,
            provider_id=str(hit.get("id") or ""),
            kind=MediaKind.PHOTO,
            url=str(hit.get("largeImageURL") or hit.get("webformatURL") or ""),
            source_page=str(hit.get("pageURL") or ""),
            licence=self.LICENCE,
            attribution=str(hit.get("user") or ""),
            title=str(hit.get("tags") or ""),
            width=int(hit.get("imageWidth") or 0),
            height=int(hit.get("imageHeight") or 0),
            preview_url=str(hit.get("previewURL") or ""),
            query=query,
        )

    def _video(self, hit: dict, query: str) -> MediaItem:
        streams = hit.get("videos") or {}
        best = {}
        for size in ("large", "medium", "small", "tiny"):
            candidate = streams.get(size) or {}
            if candidate.get("url"):
                best = candidate
                break
        return MediaItem(
            provider=self.name,
            provider_id=str(hit.get("id") or ""),
            kind=MediaKind.VIDEO,
            url=str(best.get("url") or ""),
            source_page=str(hit.get("pageURL") or ""),
            licence=self.LICENCE,
            attribution=str(hit.get("user") or ""),
            title=str(hit.get("tags") or ""),
            width=int(best.get("width") or 0),
            height=int(best.get("height") or 0),
            duration_seconds=float(hit.get("duration") or 0.0),
            query=query,
        )


# ---------------------------------------------------------------------------
# Unsplash
# ---------------------------------------------------------------------------

class UnsplashProvider:
    """Unsplash photos, with the two obligations their API imposes.

    The Unsplash License is permissive, but the API Guidelines are a condition
    of holding a key and they require more than the licence text does:

      1. Attribution naming the photographer and Unsplash, linking back to
         their profile with this application's utm parameters.
      2. A request to the photo's `links.download_location` whenever the photo
         is actually used, which is how photographers see their download
         counts.

    Both are implemented rather than noted: `credit_line()` carries the first,
    and `report_use` performs the second. Skipping either is a term-of-service
    breach, and the cost of honouring them is one request.
    """

    name = "unsplash"
    SEARCH_URL = "https://api.unsplash.com/search/photos"
    LICENCE = "Unsplash License (credit required by API guidelines)"

    def __init__(self, access_key: str | None = None, app_name: str = "") -> None:
        self._key = (access_key if access_key is not None else credential("unsplash_access_key"))
        self._app = app_name or credential("unsplash_app_name") or "video_factory"
        # Demo keys allow 50 requests an hour; production 5000. Spacing calls
        # keeps a fan-out from spending an hour's allowance in a second.
        self._limiter = RateLimiter(concurrency=2, min_interval=1.0)

    def status(self) -> ProviderStatus:
        if not self._key:
            return ProviderStatus(
                self.name, Availability.NEEDS_CREDENTIALS,
                "Set UNSPLASH_ACCESS_KEY (register an app at unsplash.com/developers).",
            )
        return ProviderStatus(self.name, Availability.READY)

    def _utm(self, url: str) -> str:
        if not url:
            return ""
        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}utm_source={self._app}&utm_medium=referral"

    async def search(
        self,
        query: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 10,
        orientation: str = "portrait",
        kind: MediaKind = MediaKind.PHOTO,
        **_,
    ) -> list[MediaItem]:
        # Unsplash is photographs only; asking it for video is not a failure,
        # it simply has none.
        if kind is not MediaKind.PHOTO:
            return []
        if not self.status().usable or not query.strip():
            return []

        params = {"query": query, "per_page": max(1, min(limit, 30))}
        if orientation == "portrait":
            params["orientation"] = "portrait"
        elif orientation == "landscape":
            params["orientation"] = "landscape"

        async def call():
            response = await client.get(
                self.SEARCH_URL,
                params=params,
                headers={
                    "Authorization": f"Client-ID {self._key}",
                    "Accept-Version": "v1",
                },
            )
            response.raise_for_status()
            return response.json() or {}

        async with self._limiter:
            body = await with_retries(call, provider=self.name)

        return [self._photo(p, query) for p in (body.get("results") or [])]

    def _photo(self, photo: dict, query: str) -> MediaItem:
        user = photo.get("user") or {}
        links = photo.get("links") or {}
        profile = self._utm(str((user.get("links") or {}).get("html") or ""))
        return MediaItem(
            provider=self.name,
            provider_id=str(photo.get("id") or ""),
            kind=MediaKind.PHOTO,
            url=str((photo.get("urls") or {}).get("full")
                    or (photo.get("urls") or {}).get("regular") or ""),
            source_page=self._utm(str(links.get("html") or "")),
            licence=self.LICENCE,
            attribution=f"{user.get('name') or 'Unknown'} ({profile})"
            if profile else str(user.get("name") or "Unknown"),
            title=str(photo.get("alt_description") or photo.get("description") or ""),
            width=int(photo.get("width") or 0),
            height=int(photo.get("height") or 0),
            preview_url=str((photo.get("urls") or {}).get("small") or ""),
            query=query,
            # Pinged by `report_use` once the photo is actually used.
            use_hook=str(links.get("download_location") or ""),
        )

    async def report_use(self, item: MediaItem, *, client: httpx.AsyncClient) -> bool:
        """Tell Unsplash the photo was used, as their guidelines require.

        Best effort by design: a failure here must not cost the run a picture
        it has already lawfully obtained, so it is logged and swallowed.
        """
        if item.provider != self.name or not item.use_hook:
            return False
        try:
            response = await client.get(
                item.use_hook,
                headers={"Authorization": f"Client-ID {self._key}"},
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.info(f"[unsplash] download ping failed for {item.provider_id}: {exc}")
            return False


# ---------------------------------------------------------------------------
# Wikimedia Commons
# ---------------------------------------------------------------------------

class CommonsProvider:
    """Wikimedia Commons files, filtered to licences that permit reuse.

    Commons publishes an explicit licence per file, and most of what it holds
    cannot be used in a monetisable derivative. The filter is the same
    `is_reusable_licence` gate the footage path uses -- one definition of
    "reusable", so a licence rejected for a clip is not quietly accepted for a
    still.

    No key: Commons asks only that clients identify themselves.
    """

    name = "wikimedia_commons"
    API_URL = "https://commons.wikimedia.org/w/api.php"

    _FILETYPE = {
        MediaKind.PHOTO: "bitmap",
        MediaKind.VIDEO: "video",
        MediaKind.AUDIO: "audio",
    }

    def __init__(self) -> None:
        # Commons answers a steady trickle and 403s a burst, citing their
        # robot policy. One request at a time, two seconds apart: a rescue
        # tier has no reason to go faster, and going faster got us blocked.
        self._limiter = RateLimiter(concurrency=1, min_interval=2.0)

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, Availability.READY)

    async def search(
        self,
        query: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 10,
        kind: MediaKind = MediaKind.PHOTO,
        **_,
    ) -> list[MediaItem]:
        if not query.strip():
            return []

        filetype = self._FILETYPE.get(kind, "bitmap")

        async def call():
            response = await client.get(
                self.API_URL,
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrsearch": f"{query} filetype:{filetype}",
                    "gsrnamespace": "6",
                    "gsrlimit": str(max(1, min(limit, 50))),
                    "prop": "imageinfo",
                    "iiprop": "url|extmetadata|size|mime",
                },
                headers={"User-Agent": COMMONS_USER_AGENT},
            )
            response.raise_for_status()
            return response.json() or {}

        async with self._limiter:
            body = await with_retries(call, provider=self.name)

        pages = (body.get("query") or {}).get("pages") or {}
        items: list[MediaItem] = []
        rejected = 0

        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            meta = info.get("extmetadata") or {}
            licence = str((meta.get("LicenseShortName") or {}).get("value") or "")

            if not is_reusable_licence(licence):
                rejected += 1
                continue

            attribution = re.sub(
                r"<[^>]+>", "",
                str((meta.get("Artist") or {}).get("value") or ""),
            ).strip()

            items.append(MediaItem(
                provider=self.name,
                provider_id=str(page.get("pageid") or ""),
                kind=kind,
                url=str(info.get("url") or ""),
                source_page=str(info.get("descriptionurl") or ""),
                licence=licence,
                attribution=attribution,
                title=str(page.get("title") or ""),
                width=int(info.get("width") or 0),
                height=int(info.get("height") or 0),
                duration_seconds=float(info.get("duration") or 0.0),
                query=query,
            ))

        if rejected:
            logger.info(
                f"[commons] {len(items)} reusable, {rejected} rejected on licence"
            )
        return items
