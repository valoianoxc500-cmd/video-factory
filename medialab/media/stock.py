"""The three general stock libraries.

These are the only sources in the aggregator that carry video at useful
volume, and the only ones with a bespoke licence rather than a Creative
Commons one. All three permit commercial use and modification without
attribution; the credit lines are still recorded, because knowing where a
frame came from is worth having whether or not it is required.
"""

from __future__ import annotations

import httpx

from settings import settings

from medialab.media.base import MediaProvider, _int, _text
from medialab.media.types import MediaAsset, MediaType, ProviderStatus, SearchContext

#: Below this a clip or photo is upscaled past recognition on a 1080 canvas.
_MIN_WIDTH = 720


def _keyed_status(name: str, key: str, env_var: str, where: str) -> ProviderStatus:
    value = (key or "").strip()
    if not value:
        return ProviderStatus(name, False, f"set {env_var} ({where})", env_var)
    if not value.isascii():
        return ProviderStatus(name, False, f"{env_var} is not a valid key", env_var)
    return ProviderStatus(name, True, env_var=env_var)


class Pexels(MediaProvider):
    name = "pexels"
    media_types = (MediaType.IMAGE, MediaType.VIDEO)
    env_var = "PEXELS_API_KEY"

    def status(self) -> ProviderStatus:
        return _keyed_status(
            self.name, settings.pexels_api_key, self.env_var,
            "https://www.pexels.com/api/",
        )

    async def search(self, client, query, media_type, context):
        key = settings.pexels_api_key.strip()
        orientation = "portrait" if context.portrait else "landscape"
        if media_type is MediaType.VIDEO:
            resp = await client.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": key},
                params={"query": query, "per_page": 12, "orientation": orientation},
            )
            resp.raise_for_status()
            return [a for a in map(self._video, resp.json().get("videos", [])) if a]
        resp = await client.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": key},
            params={"query": query, "per_page": 12, "orientation": orientation},
        )
        resp.raise_for_status()
        return [a for a in map(self._photo, resp.json().get("photos", [])) if a]

    def _video(self, row: dict) -> MediaAsset | None:
        best = None
        for f in row.get("video_files") or []:
            if _int(f.get("width")) < _MIN_WIDTH:
                continue
            if best is None or _int(f.get("width")) < _int(best.get("width")):
                best = f            # smallest file still above the floor
        if not best or not best.get("link"):
            return None
        return MediaAsset(
            provider=self.name, asset_id=str(row.get("id") or ""),
            media_type=MediaType.VIDEO,
            preview_url=_text(row.get("image"), 500),
            original_url=_text(row.get("url"), 500),
            download_url=best["link"],
            width=_int(best.get("width")), height=_int(best.get("height")),
            duration=float(row.get("duration") or 0),
            creator=_text((row.get("user") or {}).get("name"), 120),
            license="pexels", commercial_use_allowed=True,
        )

    def _photo(self, row: dict) -> MediaAsset | None:
        src = row.get("src") or {}
        link = src.get("large2x") or src.get("large") or src.get("original")
        if not link or _int(row.get("width")) < _MIN_WIDTH:
            return None
        return MediaAsset(
            provider=self.name, asset_id=str(row.get("id") or ""),
            media_type=MediaType.IMAGE,
            preview_url=_text(src.get("medium"), 500),
            original_url=_text(row.get("url"), 500),
            download_url=link,
            width=_int(row.get("width")), height=_int(row.get("height")),
            creator=_text(row.get("photographer"), 120),
            title=_text(row.get("alt"), 200),
            license="pexels", commercial_use_allowed=True,
        )


class Pixabay(MediaProvider):
    name = "pixabay"
    media_types = (MediaType.IMAGE, MediaType.VIDEO)
    env_var = "PIXABAY_API_KEY"

    def status(self) -> ProviderStatus:
        return _keyed_status(
            self.name, settings.pixabay_api_key, self.env_var,
            "https://pixabay.com/api/docs/",
        )

    async def search(self, client, query, media_type, context):
        key = settings.pixabay_api_key.strip()
        if media_type is MediaType.VIDEO:
            resp = await client.get(
                "https://pixabay.com/api/videos/",
                params={"key": key, "q": query, "per_page": 12, "video_type": "film"},
            )
            resp.raise_for_status()
            return [a for a in map(self._video, resp.json().get("hits", [])) if a]
        resp = await client.get(
            "https://pixabay.com/api/",
            params={
                "key": key, "q": query, "per_page": 12, "image_type": "photo",
                "orientation": "vertical" if context.portrait else "horizontal",
            },
        )
        resp.raise_for_status()
        return [a for a in map(self._photo, resp.json().get("hits", [])) if a]

    def _video(self, row: dict) -> MediaAsset | None:
        streams = row.get("videos") or {}
        for size in ("medium", "large", "small"):
            f = streams.get(size) or {}
            if f.get("url") and _int(f.get("width")) >= _MIN_WIDTH:
                return MediaAsset(
                    provider=self.name, asset_id=str(row.get("id") or ""),
                    media_type=MediaType.VIDEO,
                    original_url=_text(row.get("pageURL"), 500),
                    download_url=f["url"],
                    width=_int(f.get("width")), height=_int(f.get("height")),
                    duration=float(row.get("duration") or 0),
                    creator=_text(row.get("user"), 120),
                    license="pixabay", commercial_use_allowed=True,
                )
        return None

    def _photo(self, row: dict) -> MediaAsset | None:
        link = row.get("largeImageURL") or row.get("webformatURL")
        if not link or _int(row.get("imageWidth")) < _MIN_WIDTH:
            return None
        return MediaAsset(
            provider=self.name, asset_id=str(row.get("id") or ""),
            media_type=MediaType.IMAGE,
            preview_url=_text(row.get("previewURL"), 500),
            original_url=_text(row.get("pageURL"), 500),
            download_url=link,
            width=_int(row.get("imageWidth")), height=_int(row.get("imageHeight")),
            creator=_text(row.get("user"), 120),
            title=_text(row.get("tags"), 200),
            license="pixabay", commercial_use_allowed=True,
        )


class Unsplash(MediaProvider):
    """Stills only, and the best-looking of the three for lifestyle subjects."""

    name = "unsplash"
    media_types = (MediaType.IMAGE,)
    env_var = "UNSPLASH_ACCESS_KEY"

    def status(self) -> ProviderStatus:
        return _keyed_status(
            self.name, settings.unsplash_access_key, self.env_var,
            "https://unsplash.com/developers",
        )

    async def search(self, client, query, media_type, context):
        resp = await client.get(
            "https://api.unsplash.com/search/photos",
            headers={
                "Authorization": f"Client-ID {settings.unsplash_access_key.strip()}",
                "Accept-Version": "v1",
            },
            params={
                "query": query, "per_page": 12,
                "orientation": "portrait" if context.portrait else "landscape",
            },
        )
        resp.raise_for_status()
        out = []
        for row in resp.json().get("results", []):
            urls = row.get("urls") or {}
            link = urls.get("full") or urls.get("regular")
            if not link:
                continue
            user = row.get("user") or {}
            out.append(MediaAsset(
                provider=self.name, asset_id=str(row.get("id") or ""),
                media_type=MediaType.IMAGE,
                preview_url=_text(urls.get("small"), 500),
                original_url=_text((row.get("links") or {}).get("html"), 500),
                download_url=link,
                width=_int(row.get("width")), height=_int(row.get("height")),
                creator=_text(user.get("name") or user.get("username"), 120),
                title=_text(row.get("description") or row.get("alt_description"), 200),
                license="unsplash", commercial_use_allowed=True,
            ))
        return out
