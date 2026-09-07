"""Experimental: discover reusable match footage online.

Gated entirely behind MATCH_FOOTAGE_MODE=experimental. With the variable
unset, absent, or set to anything else, `is_enabled()` is False and nothing
in this module runs -- that is the single switch that turns the mode off.

What this does and does not do
------------------------------
It searches sources that publish a MACHINE-READABLE REUSE LICENCE and keeps
only results whose licence is on the allowlist below. Every kept clip carries
its source URL, platform, licence and attribution into the run manifest.

It does NOT search YouTube, X/Twitter, Reddit, Facebook, TikTok, or
broadcaster and highlights sites. Not because those are hard to reach, but
because "publicly reachable" is not "licensed to reuse": clips there are
almost always broadcast footage owned by a league or broadcaster, and
republishing them infringes whether or not any technical control was
involved. `core.footage.is_allowed_source` still refuses those hosts, and
this module never removes that check.

The practical consequence, stated plainly: freely licensed footage of a
SPECIFIC goal in a SPECIFIC match essentially does not exist. This mode will
usually find nothing and fall through to the photo pipeline. It is built for
the cases where something genuinely reusable does exist -- an archive clip,
a federation release, licensed stock atmosphere -- not as a way to obtain
broadcast goals.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from core.footage import is_allowed_source

logger = logging.getLogger("video_factory")

MODE_ENV = "MATCH_FOOTAGE_MODE"
EXPERIMENTAL = "experimental"

# Licences that permit reuse in a derivative, monetisable video. Anything not
# on this list is rejected, including "no known copyright" style assertions
# that are not an actual grant.
# Spaces are normalised to hyphens before matching, so "Public Domain" arrives
# here as "public-domain".
_ALLOWED_LICENCE_RE = re.compile(
    r"^(cc0|cc-?by(-sa)?(-\d(\.\d)?)?|public[-\s]*domain|pd|pdm)$", re.I
)

# Hosts we will actually fetch bytes from. An allowlist, not a denylist: a
# discovery result that points anywhere else is dropped rather than trusted.
_DOWNLOAD_HOST_ALLOWLIST = (
    "upload.wikimedia.org",
    "commons.wikimedia.org",
    "videos.pexels.com",
    "player.vimeo.com.pexels",  # pexels CDN variants
)

_VIDEO_EXTENSIONS = (".webm", ".ogv", ".mp4", ".mov")
# Commons serves audio as ogg/oga far more often than mp3.
AUDIO_EXTENSIONS = (".ogg", ".oga", ".mp3", ".wav", ".flac", ".m4a", ".opus")

# Discovery is a test aid, not a crawler: one page of results per query.
_MAX_RESULTS = 12
_MAX_BYTES = 400 * 1024 * 1024


@dataclass
class DiscoveredClip:
    """A candidate clip and the provenance that makes it usable."""

    url: str
    platform: str
    licence: str
    title: str = ""
    attribution: str = ""
    duration_seconds: float = 0.0
    local_path: Path | None = None

    def as_record(self) -> dict:
        return {
            "url": self.url,
            "platform": self.platform,
            "licence": self.licence,
            "title": self.title,
            "attribution": self.attribution,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class DiscoveryResult:
    clips: list[DiscoveredClip] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    reason: str = ""


def is_enabled() -> bool:
    """True only when MATCH_FOOTAGE_MODE is exactly 'experimental'."""
    return os.environ.get(MODE_ENV, "").strip().lower() == EXPERIMENTAL


def is_reusable_licence(licence: str) -> bool:
    """Whether a licence string grants reuse in a derivative work."""
    if not licence:
        return False
    cleaned = licence.strip().lower()
    # Strip a trailing jurisdiction/version tail: "CC BY-SA 4.0" -> "cc by-sa".
    cleaned = re.sub(r"\s+\d+(\.\d+)?$", "", cleaned)
    cleaned = cleaned.replace(" ", "-")
    if cleaned.endswith("-deed"):
        cleaned = cleaned[: -len("-deed")]
    # Non-commercial and no-derivatives cannot be used here.
    if "nc" in cleaned.split("-") or "nd" in cleaned.split("-"):
        return False
    return bool(_ALLOWED_LICENCE_RE.match(cleaned))


def is_downloadable_url(
    url: str,
    extensions: tuple[str, ...] = _VIDEO_EXTENSIONS,
) -> bool:
    """Allowlisted host, https, and an expected media extension.

    `extensions` lets the audio asset tool reuse the same host allowlist and
    scheme check without widening what the footage path will accept.
    """
    if not url or not url.lower().startswith("https://"):
        return False
    lowered = url.lower()
    host = lowered.split("/")[2] if len(lowered.split("/")) > 2 else ""
    if not any(host == h or host.endswith("." + h) for h in _DOWNLOAD_HOST_ALLOWLIST):
        return False
    path = lowered.split("?")[0]
    return path.endswith(extensions)


def _reject(result: DiscoveryResult, url: str, platform: str, why: str) -> None:
    logger.info(f"discovery rejected {platform} result ({why}): {url[:100]}")
    result.rejected.append({"url": url, "platform": platform, "reason": why})


def search_wikimedia_commons(
    query: str,
    *,
    client: httpx.Client,
    limit: int = _MAX_RESULTS,
) -> DiscoveryResult:
    """Search Wikimedia Commons for freely licensed video.

    Commons publishes an explicit licence per file and permits programmatic
    access, so licence filtering here is based on the source's own metadata
    rather than a guess.
    """
    result = DiscoveryResult()
    try:
        resp = client.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"{query} filetype:video",
                "gsrnamespace": "6",
                "gsrlimit": str(limit),
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|size|mime",
            },
            headers={
                # Commons asks API clients to identify themselves.
                "User-Agent": "VideoFactory/1.0 (match analysis; contact via repo)"
            },
            timeout=45.0,
        )
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or {}
    except Exception as exc:
        result.reason = f"commons search failed: {exc}"
        logger.warning(result.reason)
        return result

    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        url = info.get("url") or ""
        meta = info.get("extmetadata") or {}
        licence = str((meta.get("LicenseShortName") or {}).get("value") or "")
        attribution = str((meta.get("Artist") or {}).get("value") or "")
        attribution = re.sub(r"<[^>]+>", "", attribution).strip()

        if not is_reusable_licence(licence):
            _reject(result, url, "wikimedia_commons", f"licence {licence or 'unknown'!r}")
            continue
        if not is_downloadable_url(url):
            _reject(result, url, "wikimedia_commons", "url not on the download allowlist")
            continue

        result.clips.append(
            DiscoveredClip(
                url=url,
                platform="wikimedia_commons",
                licence=licence,
                title=str(page.get("title") or ""),
                attribution=attribution,
                duration_seconds=float(info.get("duration") or 0.0),
            )
        )

    result.reason = (
        f"commons: {len(result.clips)} reusable, {len(result.rejected)} rejected"
    )
    logger.info(result.reason)
    return result


def download_clip(
    clip: DiscoveredClip,
    destination_dir: Path,
    *,
    client: httpx.Client,
) -> Path | None:
    """Fetch an allowlisted, reusable clip. Returns None on any doubt."""
    if not is_reusable_licence(clip.licence):
        logger.warning(f"refusing download: licence {clip.licence!r} is not reusable")
        return None
    if not is_downloadable_url(clip.url):
        logger.warning(f"refusing download: {clip.url[:80]} is not allowlisted")
        return None

    destination_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(clip.url.split("?")[0]).suffix or ".mp4"
    safe = re.sub(r"[^a-z0-9]+", "_", clip.title.lower()).strip("_")[:48] or "clip"
    out = destination_dir / f"{clip.platform}_{safe}{suffix}"
    if out.exists():
        clip.local_path = out
        return out

    try:
        with client.stream("GET", clip.url, timeout=180.0) as response:
            response.raise_for_status()
            written = 0
            with out.open("wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    written += len(chunk)
                    if written > _MAX_BYTES:
                        raise RuntimeError("clip exceeds the size cap")
                    handle.write(chunk)
    except Exception as exc:
        logger.warning(f"download failed for {clip.url[:80]}: {exc}")
        out.unlink(missing_ok=True)
        return None

    # Belt and braces: the downloaded file must still pass the same local
    # source check every other footage path goes through.
    if not is_allowed_source(str(out)):
        logger.warning(f"downloaded file failed the source policy: {out.name}")
        out.unlink(missing_ok=True)
        return None

    clip.local_path = out
    logger.info(
        f"downloaded {out.name} ({written / 1e6:.1f} MB) "
        f"from {clip.platform} under {clip.licence}"
    )
    return out


def discover_for_query(
    query: str,
    destination_dir: Path,
    *,
    limit: int = _MAX_RESULTS,
) -> DiscoveryResult:
    """Search, filter by licence, and download what survives.

    Returns an empty result -- never raises -- so the caller falls back to the
    photo pipeline on any failure.
    """
    if not is_enabled():
        return DiscoveryResult(reason="experimental footage mode is off")
    if not query.strip():
        return DiscoveryResult(reason="empty query")

    with httpx.Client(follow_redirects=True) as client:
        result = search_wikimedia_commons(query, client=client, limit=limit)
        kept: list[DiscoveredClip] = []
        for clip in result.clips:
            if download_clip(clip, destination_dir, client=client) is not None:
                kept.append(clip)
        result.clips = kept
    return result
