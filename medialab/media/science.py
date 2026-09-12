"""NASA and NOAA.

Both agencies produce work that is public domain as a U.S. Government work,
which makes them the right first stop for space, weather, ocean and Earth
observation beats -- and the only place a genuine satellite view of a named
place is going to come from.

NASA publishes a first-class media API. NOAA does not: its photo library and
its data portals have no documented JSON search for imagery. Rather than
pretend otherwise, the NOAA lane below searches the NOAA-authored material
mirrored on Wikimedia Commons, which is tens of thousands of files, all
carrying an explicit PD-USGov-NOAA tag, and reachable without a key. The
docstring on that class says so plainly so nobody later mistakes it for a
first-party integration.
"""

from __future__ import annotations

import httpx

from medialab.media.base import MediaProvider, _int, _text
from medialab.media.open import WikimediaCommons
from medialab.media.types import MediaAsset, MediaType, ProviderStatus, SearchContext

_UA = "ValoianoMediaLab/1.0 (video production; contact via site owner)"


class NASA(MediaProvider):
    """images-api.nasa.gov. Keyless.

    Nearly all of it is public domain, but not quite all -- some items are
    contributed under other terms -- so the per-item rights field is read
    rather than assumed.
    """

    name = "nasa"
    media_types = (MediaType.IMAGE, MediaType.VIDEO)

    async def search(self, client, query, media_type, context):
        wanted = "video" if media_type is MediaType.VIDEO else "image"
        resp = await client.get(
            "https://images-api.nasa.gov/search",
            headers={"User-Agent": _UA},
            params={"q": query, "media_type": wanted, "page_size": 12},
        )
        resp.raise_for_status()
        items = ((resp.json().get("collection") or {}).get("items") or [])
        out = []
        for item in items[:12]:
            data = (item.get("data") or [{}])[0]
            links = item.get("links") or []
            preview = next(
                (l.get("href") for l in links if l.get("rel") == "preview"), ""
            )
            if not preview:
                continue
            # `preview` is a thumbnail, far too small for a 1080-wide canvas.
            # The renderable copy is the `~large` variant; `~orig` exists too
            # but runs to hundreds of megabytes for scanned mission film.
            full = _variant(links, "~large.jpg") or _variant(links, "~orig.jpg") or preview
            identifier = _text(data.get("nasa_id"), 200)
            # A rights note that names a third party means it is not simply a
            # government work; refuse rather than guess.
            restricted = bool(_text(data.get("secondary_creator"), 200)) and bool(
                _text(data.get("rights"), 200)
            )
            out.append(MediaAsset(
                provider=self.name, asset_id=identifier,
                media_type=MediaType.IMAGE if wanted == "image" else MediaType.VIDEO,
                preview_url=_text(preview, 500),
                original_url=f"https://images.nasa.gov/details/{identifier}",
                download_url=_text(full, 500),
                creator=_text(data.get("center") or "NASA", 120),
                title=_text(data.get("title"), 200),
                license="" if restricted else "usgov",
                license_url="https://www.nasa.gov/nasa-brand-center/images-and-media/",
                attribution=f"NASA — {_text(data.get('title'), 120)}",
                extra={"rights": _text(data.get("rights"), 200)},
            ))
        return out


def _variant(links: list[dict], suffix: str) -> str:
    """The link ending in `suffix`, if the item has one."""
    for link in links:
        href = str(link.get("href") or "")
        if href.endswith(suffix):
            return href
    return ""


class NOAA(WikimediaCommons):
    """NOAA public media, via the NOAA-authored files mirrored on Commons.

    NOAA has no documented media search API. Its photo library is a browsable
    site and its data portals index datasets, not pictures. What does exist is
    a very large body of NOAA-authored imagery on Wikimedia Commons, tagged
    PD-USGov-NOAA, reachable keylessly and with the rights statement attached
    to each file.

    So this is a scoped Commons query rather than a first-party integration,
    and it is named honestly for what it searches. If NOAA publishes a media
    API later, only this class changes.
    """

    name = "noaa"
    scope = "NOAA"

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            self.name, True, "NOAA-authored files via Wikimedia Commons"
        )
