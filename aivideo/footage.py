"""Stock footage, with a provider ladder and per-clip replacement.

Adapted from MoneyPrinterTurbo's `app/services/material.py` (MIT): the same two
free providers, the same "ask for landscape/portrait and pick the largest file
under the size cap" selection, the same content-hash dedup so one search does
not fill a video with the same clip.

The product rule this file exists to enforce is narrower than MPT's: **one
missing clip must never fail the video.** So every unit of work here is a
single beat, every failure is contained to that beat, and the caller gets back
whatever succeeded plus a list of what did not. A beat with no footage is
filled by a neighbour rather than raising.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import httpx

from settings import settings

logger = logging.getLogger("aivideo")

#: Smallest clip worth downloading. Anything under this is upscaled beyond
#: recognition on a 1080-wide canvas.
_MIN_WIDTH = 720
#: Biggest file worth downloading for one beat. Stock 4K originals run to
#: hundreds of MB and buy nothing after the vertical crop.
_MAX_BYTES = 60 * 1024 * 1024
_TIMEOUT = httpx.Timeout(45.0, connect=10.0)


@dataclass
class Clip:
    path: Path
    provider: str
    term: str
    width: int
    height: int
    duration: float
    source_url: str = ""

    @property
    def is_portrait(self) -> bool:
        return self.height >= self.width


class NoFootage(RuntimeError):
    """Not one clip could be obtained from any provider."""


# ── providers ────────────────────────────────────────────────────────
#
# Each returns candidate descriptors, newest/largest first. They never raise
# for "nothing found" -- an empty list is a normal answer and the ladder moves
# on. They raise only on a transport failure the caller may want to retry.

async def _pexels_candidates(
    client: httpx.AsyncClient, term: str, portrait: bool
) -> list[dict]:
    key = (settings.pexels_api_key or "").strip()
    if not key or not key.isascii():
        return []
    resp = await client.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": key},
        params={
            "query": term,
            "per_page": 12,
            "orientation": "portrait" if portrait else "landscape",
        },
    )
    if resp.status_code != 200:
        logger.debug(f"[aivideo] pexels {resp.status_code} for {term!r}")
        return []

    out: list[dict] = []
    for video in resp.json().get("videos", []):
        best = None
        for f in video.get("video_files", []):
            if (f.get("width") or 0) < _MIN_WIDTH:
                continue
            if best is None or (f.get("width") or 0) < (best.get("width") or 0):
                best = f          # smallest file that still clears the floor
        if best and best.get("link"):
            out.append({
                "url": best["link"],
                "width": best.get("width") or 0,
                "height": best.get("height") or 0,
                "duration": float(video.get("duration") or 0),
                "provider": "pexels",
                "page": video.get("url", ""),
            })
    return out


async def _pixabay_candidates(
    client: httpx.AsyncClient, term: str, portrait: bool
) -> list[dict]:
    key = (settings.pixabay_api_key or "").strip()
    if not key or not key.isascii():
        return []
    resp = await client.get(
        "https://pixabay.com/api/videos/",
        params={"key": key, "q": term, "per_page": 12, "video_type": "film"},
    )
    if resp.status_code != 200:
        logger.debug(f"[aivideo] pixabay {resp.status_code} for {term!r}")
        return []

    out: list[dict] = []
    for hit in resp.json().get("hits", []):
        streams = hit.get("videos") or {}
        for name in ("medium", "large", "small"):
            f = streams.get(name) or {}
            if not f.get("url") or (f.get("width") or 0) < _MIN_WIDTH:
                continue
            out.append({
                "url": f["url"],
                "width": f.get("width") or 0,
                "height": f.get("height") or 0,
                "duration": float(hit.get("duration") or 0),
                "provider": "pixabay",
                "page": hit.get("pageURL", ""),
            })
            break
    return out


#: Tried in order. Pexels first because its library is better curated for the
#: cinematic look this product wants; Pixabay is the fallback and needs no
#: header auth, so it also covers a Pexels outage.
PROVIDERS = (
    ("pexels", _pexels_candidates),
    ("pixabay", _pixabay_candidates),
)


def simplify(term: str) -> str:
    """Broaden a term that found nothing, rather than giving up on the beat.

    "abandoned desert highway" -> "desert highway" -> "desert". Each step drops
    the most specific word, which is the one most likely to be missing from a
    stock library.
    """
    words = term.split()
    return " ".join(words[1:]) if len(words) > 1 else ""


async def _download(
    client: httpx.AsyncClient, candidate: dict, target: Path, seen: set[str]
) -> Clip | None:
    try:
        async with client.stream("GET", candidate["url"]) as resp:
            if resp.status_code != 200:
                return None
            size = int(resp.headers.get("content-length") or 0)
            if size > _MAX_BYTES:
                logger.debug(f"[aivideo] skipping {size / 1e6:.0f}MB clip")
                return None
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_BYTES:
                    return None
    except Exception as exc:
        logger.debug(f"[aivideo] download failed: {type(exc).__name__}: {exc}")
        return None

    if len(body) < 50_000:
        return None
    digest = hashlib.md5(bytes(body)).hexdigest()
    if digest in seen:
        return None                     # the same clip on two beats reads as a loop
    seen.add(digest)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(body))
    return Clip(
        path=target,
        provider=candidate["provider"],
        term=candidate.get("term", ""),
        width=candidate["width"],
        height=candidate["height"],
        duration=candidate["duration"],
        source_url=candidate.get("page", ""),
    )


async def fetch_one(
    term: str,
    target: Path,
    *,
    client: httpx.AsyncClient,
    seen: set[str],
    portrait: bool,
) -> Clip | None:
    """One beat's clip, walking providers then broadening the query.

    Returns None rather than raising: the caller decides what an uncovered
    beat means, and for this product it means "show a neighbouring clip
    longer", not "lose the video".
    """
    query = term
    while query:
        for name, search in PROVIDERS:
            try:
                candidates = await search(client, query, portrait)
            except Exception as exc:
                logger.info(
                    f"[aivideo] {name} unavailable for {query!r} "
                    f"({type(exc).__name__}); trying the next provider"
                )
                continue
            random.shuffle(candidates)          # avoid every video opening alike
            for candidate in candidates[:4]:
                candidate["term"] = term
                clip = await _download(client, candidate, target, seen)
                if clip:
                    logger.info(
                        f"[aivideo] {target.name}: {name} · {query!r} "
                        f"({clip.width}x{clip.height}, {clip.duration:.0f}s)"
                    )
                    return clip
        broader = simplify(query)
        if broader:
            logger.info(f"[aivideo] no footage for {query!r}; broadening to {broader!r}")
        query = broader
    return None


async def gather(
    terms: list[str],
    directory: Path,
    *,
    portrait: bool = True,
    concurrency: int = 4,
) -> tuple[list[Clip], list[str]]:
    """Fetch a clip per term. Returns (clips, terms that found nothing).

    Beats run concurrently but each is isolated: one provider outage, one bad
    URL or one oversized file costs that beat and nothing else. An exception
    escaping a single task is caught here for the same reason.
    """
    directory.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    limiter = asyncio.Semaphore(max(1, concurrency))
    results: list[Clip | None] = [None] * len(terms)

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        async def run(index: int, term: str) -> None:
            async with limiter:
                try:
                    results[index] = await fetch_one(
                        term,
                        directory / f"clip_{index:02d}.mp4",
                        client=client,
                        seen=seen,
                        portrait=portrait,
                    )
                except Exception as exc:
                    logger.warning(
                        f"[aivideo] beat {index} footage failed "
                        f"({type(exc).__name__}: {exc}); continuing without it"
                    )

        await asyncio.gather(*(run(i, t) for i, t in enumerate(terms)))

    clips = [c for c in results if c is not None]
    missing = [t for c, t in zip(results, terms) if c is None]
    if not clips:
        raise NoFootage(
            "no stock provider returned usable footage for any search term"
        )
    if missing:
        logger.info(
            f"[aivideo] {len(missing)} beat(s) found no footage; the video will "
            f"hold the surrounding clips longer: {', '.join(missing)}"
        )
    return clips, missing
