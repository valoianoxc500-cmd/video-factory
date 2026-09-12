"""Library of Congress, Smithsonian Open Access, Europeana.

The history lane. Between them they hold most of what an American or European
historical beat could want, and all three publish rights information per item
rather than per collection -- which matters, because a digitised photograph
from 1890 and a 1990 press print sit in the same collection under very
different terms.

Two of the three need a free key. Neither blocks anything: an unconfigured
source reports itself unavailable and the router simply asks the others.
"""

from __future__ import annotations

import httpx

from settings import settings

from medialab.media.base import MediaProvider, _int, _text
from medialab.media.licensing import normalise
from medialab.media.types import MediaAsset, MediaType, ProviderStatus, SearchContext

_UA = "ValoianoMediaLab/1.0 (video production; contact via site owner)"


class LibraryOfCongress(MediaProvider):
    """Keyless JSON API. `?fo=json` on any search page returns structured data.

    Only items the Library itself marks as having no known copyright
    restrictions are emitted; its collections include a great deal of
    in-copyright material that is merely viewable.
    """

    name = "loc"
    media_types = (MediaType.IMAGE,)

    async def search(self, client, query, media_type, context):
        resp = await client.get(
            "https://www.loc.gov/photos/",
            headers={"User-Agent": _UA},
            params={"q": query, "fo": "json", "c": 12, "at": "results"},
        )
        resp.raise_for_status()
        out = []
        for row in (resp.json().get("results") or []):
            image = _first_url(row.get("image_url"))
            if not image:
                continue
            rights = _text(
                row.get("rights_advisory") or row.get("rights") or "", 300
            )
            out.append(MediaAsset(
                provider=self.name, asset_id=_text(row.get("id"), 200),
                media_type=MediaType.IMAGE,
                preview_url=image,
                original_url=_text(row.get("url"), 500),
                download_url=_first_url(row.get("image_url"), last=True) or image,
                creator=_text(_first(row.get("contributor")), 120),
                title=_text(row.get("title"), 200),
                # The Library's own phrasing for public-domain material.
                license="public-domain" if _no_known_restrictions(rights) else "",
                license_url="https://www.loc.gov/legal/",
                attribution=f"Library of Congress — {_text(row.get('title'), 120)}",
                extra={"rights_advisory": rights},
            ))
        return out


def _no_known_restrictions(rights: str) -> bool:
    text = rights.lower()
    return any(
        phrase in text
        for phrase in (
            "no known restrictions", "no known copyright",
            "public domain", "not subject to copyright",
        )
    )


def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def _first_url(value, *, last: bool = False) -> str:
    if isinstance(value, list):
        if not value:
            return ""
        return _text(value[-1] if last else value[0], 500)
    return _text(value, 500)


class Smithsonian(MediaProvider):
    """Smithsonian Open Access. Needs a free api.data.gov key.

    Filtered to CC0 items, which is what "Open Access" means in their
    catalogue; the rest of the collection is metadata-only or restricted.
    """

    name = "smithsonian"
    media_types = (MediaType.IMAGE,)
    env_var = "SMITHSONIAN_API_KEY"

    def status(self) -> ProviderStatus:
        key = (getattr(settings, "smithsonian_api_key", "") or "").strip()
        if not key:
            return ProviderStatus(
                self.name, False,
                "set SMITHSONIAN_API_KEY (free, https://api.data.gov/signup/)",
                self.env_var,
            )
        return ProviderStatus(self.name, True, env_var=self.env_var)

    async def search(self, client, query, media_type, context):
        key = settings.smithsonian_api_key.strip()
        resp = await client.get(
            "https://api.si.edu/openaccess/api/v1.0/search",
            headers={"User-Agent": _UA},
            params={
                "api_key": key,
                "q": f"{query} AND online_media_type:Images AND media_usage:CC0",
                "rows": 12,
            },
        )
        resp.raise_for_status()
        rows = ((resp.json().get("response") or {}).get("rows") or [])
        out = []
        for row in rows:
            content = row.get("content") or {}
            descriptive = content.get("descriptiveNonRepeating") or {}
            media = ((descriptive.get("online_media") or {}).get("media") or [])
            if not media:
                continue
            first = media[0]
            link = first.get("content") or first.get("thumbnail")
            if not link:
                continue
            out.append(MediaAsset(
                provider=self.name, asset_id=_text(row.get("id"), 120),
                media_type=MediaType.IMAGE,
                preview_url=_text(first.get("thumbnail"), 500),
                original_url=_text(
                    (descriptive.get("record_link") or first.get("guid")), 500
                ),
                download_url=link,
                creator=_text(
                    ((content.get("freetext") or {}).get("name") or [{}])[0].get("content"),
                    120,
                ),
                title=_text(row.get("title"), 200),
                license="cc0", commercial_use_allowed=True,
                license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            ))
        return out


class Europeana(MediaProvider):
    """Europeana. Needs a free key; rights are per item and vary widely."""

    name = "europeana"
    media_types = (MediaType.IMAGE,)
    env_var = "EUROPEANA_API_KEY"

    def status(self) -> ProviderStatus:
        key = (getattr(settings, "europeana_api_key", "") or "").strip()
        if not key:
            return ProviderStatus(
                self.name, False,
                "set EUROPEANA_API_KEY (free, https://pro.europeana.eu/pages/get-api)",
                self.env_var,
            )
        return ProviderStatus(self.name, True, env_var=self.env_var)

    async def search(self, client, query, media_type, context):
        key = settings.europeana_api_key.strip()
        resp = await client.get(
            "https://api.europeana.eu/record/v2/search.json",
            headers={"User-Agent": _UA},
            params={
                "wskey": key, "query": query, "rows": 12,
                "media": "true", "thumbnail": "true",
                "qf": "TYPE:IMAGE",
                # Europeana's own reusability facet; each item's rights URL is
                # still read below rather than trusted from this alone.
                "reusability": "open",
                "profile": "rich",
            },
        )
        resp.raise_for_status()
        out = []
        for row in (resp.json().get("items") or []):
            link = _first(row.get("edmIsShownBy")) or _first(row.get("edmPreview"))
            if not link:
                continue
            rights = _first(row.get("rights")) or ""
            out.append(MediaAsset(
                provider=self.name, asset_id=_text(row.get("id"), 200),
                media_type=MediaType.IMAGE,
                preview_url=_text(_first(row.get("edmPreview")), 500),
                original_url=_text(_first(row.get("edmIsShownAt")) or row.get("guid"), 500),
                download_url=_text(link, 500),
                creator=_text(_first(row.get("dcCreator")), 120),
                title=_text(_first(row.get("title")), 200),
                license=normalise(rights),
                license_url=_text(rights, 300),
                extra={"data_provider": _text(_first(row.get("dataProvider")), 120)},
            ))
        return out
