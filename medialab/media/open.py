"""Openly licensed sources: Openverse, Wikimedia Commons, Internet Archive.

These are where correctly identified pictures of real subjects live -- a named
footballer, a specific stadium, a dated event. Stock libraries have none of
that; they have a model in a generic kit on a generic pitch, which is the
wrong picture no matter how well shot.

The trade is that licensing here is heterogeneous and has to be read per
asset. Commons hosts fair-use screenshots beside public-domain photographs,
Openverse indexes the non-commercial CC variants, and the Internet Archive
accepts uploads with no rights statement at all. Every adapter below reads
whatever the source actually says and hands it to `licensing`, which refuses
anything it cannot place.
"""

from __future__ import annotations

import httpx

from medialab.media.base import MediaProvider, _int, _text
from medialab.media.licensing import normalise
from medialab.media.types import MediaAsset, MediaType, ProviderStatus, SearchContext

_UA = "ValoianoMediaLab/1.0 (video production; contact via site owner)"


class Openverse(MediaProvider):
    """The CC search index. Keyless, rate-limited; a token raises the limit."""

    name = "openverse"
    media_types = (MediaType.IMAGE,)
    env_var = "OPENVERSE_API_TOKEN"

    def status(self) -> ProviderStatus:
        from settings import settings

        token = (getattr(settings, "openverse_api_token", "") or "").strip()
        return ProviderStatus(
            self.name, True,
            "authenticated" if token else "anonymous (lower rate limit)",
            self.env_var,
        )

    async def search(self, client, query, media_type, context):
        from settings import settings

        headers = {"User-Agent": _UA}
        token = (getattr(settings, "openverse_api_token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await client.get(
            "https://api.openverse.org/v1/images/",
            headers=headers,
            params={
                "q": query, "page_size": 12,
                # Ask the index to pre-filter, then verify per asset anyway:
                # the licence field is the authority, not the query string.
                "license_type": "commercial,modification",
            },
        )
        resp.raise_for_status()
        out = []
        for row in resp.json().get("results", []):
            link = row.get("url")
            if not link:
                continue
            out.append(MediaAsset(
                provider=self.name, asset_id=_text(row.get("id"), 80),
                media_type=MediaType.IMAGE,
                preview_url=_text(row.get("thumbnail"), 500),
                original_url=_text(row.get("foreign_landing_url"), 500),
                download_url=link,
                width=_int(row.get("width")), height=_int(row.get("height")),
                creator=_text(row.get("creator"), 120),
                title=_text(row.get("title"), 200),
                license=normalise(row.get("license") or ""),
                license_url=_text(row.get("license_url"), 300),
                extra={"source": _text(row.get("source"), 60)},
            ))
        return out


class WikimediaCommons(MediaProvider):
    """Keyless MediaWiki search, with the licence read from each file's page.

    `extmetadata` is the only field that says whether a Commons file is
    reusable. Searching without reading it is how fair-use screenshots end up
    in a commercial video.
    """

    name = "commons"
    media_types = (MediaType.IMAGE,)
    #: Restrict a search to files matching this, for the scoped subclasses.
    scope: str = ""

    async def search(self, client, query, media_type, context):
        term = f"{self.scope} {query}".strip() if self.scope else query
        resp = await client.get(
            "https://commons.wikimedia.org/w/api.php",
            headers={"User-Agent": _UA},
            params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": f"filetype:bitmap {term}",
                "gsrnamespace": 6, "gsrlimit": 14,
                "prop": "imageinfo",
                "iiprop": "url|size|extmetadata|user",
                "iiurlwidth": 1600,
            },
        )
        resp.raise_for_status()
        pages = ((resp.json().get("query") or {}).get("pages") or {}).values()
        out = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            link = info.get("thumburl") or info.get("url")
            if not link:
                continue
            raw_license = (
                _value(meta, "LicenseShortName")
                or _value(meta, "License")
                or _value(meta, "UsageTerms")
            )
            out.append(MediaAsset(
                provider=self.name, asset_id=str(page.get("pageid") or ""),
                media_type=MediaType.IMAGE,
                preview_url=_text(info.get("thumburl"), 500),
                original_url=_text(info.get("descriptionurl"), 500),
                download_url=link,
                width=_int(info.get("thumbwidth") or info.get("width")),
                height=_int(info.get("thumbheight") or info.get("height")),
                creator=_strip_html(
                    _value(meta, "Artist") or _text(info.get("user"), 120)
                ),
                title=_text(page.get("title"), 200),
                license=normalise(raw_license),
                license_url=_text(_value(meta, "LicenseUrl"), 300),
                attribution=_strip_html(_value(meta, "Attribution")),
                extra={"raw_license": _text(raw_license, 80)},
            ))
        return out


def _value(meta: dict, key: str) -> str:
    entry = meta.get(key) or {}
    return str(entry.get("value") or "") if isinstance(entry, dict) else ""


def _strip_html(value: str) -> str:
    import re

    return _text(re.sub(r"<[^>]+>", " ", str(value or "")), 200)


class InternetArchive(MediaProvider):
    """Keyless. Rich for historical film and photographs, thin on metadata.

    Only items whose own `licenseurl` or `rights` field establishes reuse get
    through; the archive hosts a great deal of material uploaded with nothing
    said about rights at all, and silence is not permission.
    """

    name = "archive"
    media_types = (MediaType.IMAGE,)

    async def search(self, client, query, media_type, context):
        resp = await client.get(
            "https://archive.org/advancedsearch.php",
            headers={"User-Agent": _UA},
            params={
                "q": f'({query}) AND mediatype:(image)',
                "fl[]": ["identifier", "title", "creator", "licenseurl", "rights"],
                "rows": 12, "page": 1, "output": "json",
            },
        )
        resp.raise_for_status()
        docs = ((resp.json().get("response") or {}).get("docs") or [])
        out = []
        for doc in docs:
            identifier = _text(doc.get("identifier"), 120)
            if not identifier:
                continue
            raw = doc.get("licenseurl") or doc.get("rights") or ""
            if isinstance(raw, list):
                raw = raw[0] if raw else ""
            out.append(MediaAsset(
                provider=self.name, asset_id=identifier,
                media_type=MediaType.IMAGE,
                preview_url=f"https://archive.org/services/img/{identifier}",
                original_url=f"https://archive.org/details/{identifier}",
                # `download/<id>/__ia_thumb.jpg` is unreliable; the services
                # image endpoint always resolves to something renderable.
                download_url=f"https://archive.org/services/img/{identifier}",
                creator=_text(_first(doc.get("creator")), 120),
                title=_text(doc.get("title"), 200),
                license=normalise(raw),
                license_url=_text(raw if str(raw).startswith("http") else "", 300),
                extra={"raw_rights": _text(raw, 120)},
            ))
        return out


def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value


class MetMuseum(MediaProvider):
    """The Met's Open Access collection. Keyless; CC0 items only.

    Two calls -- search returns object ids, and each object has to be fetched
    for its image. Capped hard because that is one request per result.
    """

    name = "met"
    media_types = (MediaType.IMAGE,)
    _MAX_OBJECTS = 6

    async def search(self, client, query, media_type, context):
        resp = await client.get(
            "https://collectionapi.metmuseum.org/public/collection/v1/search",
            headers={"User-Agent": _UA},
            params={"q": query, "hasImages": "true", "isPublicDomain": "true"},
        )
        resp.raise_for_status()
        ids = (resp.json().get("objectIDs") or [])[: self._MAX_OBJECTS]
        out = []
        for object_id in ids:
            try:
                detail = await client.get(
                    "https://collectionapi.metmuseum.org/public/collection/v1/"
                    f"objects/{object_id}",
                    headers={"User-Agent": _UA},
                )
                detail.raise_for_status()
                row = detail.json()
            except Exception:
                continue          # one object missing is not a failed search
            image = row.get("primaryImage") or row.get("primaryImageSmall")
            if not image or not row.get("isPublicDomain"):
                continue
            out.append(MediaAsset(
                provider=self.name, asset_id=str(object_id),
                media_type=MediaType.IMAGE,
                preview_url=_text(row.get("primaryImageSmall"), 500),
                original_url=_text(row.get("objectURL"), 500),
                download_url=image,
                creator=_text(row.get("artistDisplayName"), 120),
                title=_text(row.get("title"), 200),
                license="cc0", commercial_use_allowed=True,
            ))
        return out
