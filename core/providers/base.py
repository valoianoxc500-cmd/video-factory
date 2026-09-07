"""One shape for every source, and the manners every source needs.

Six providers, three of which hand back pictures, one of which hands back
freely-licensed files of any kind, and two of which hand back reading matter
rather than media at all. Callers should not have to know which is which, so
everything arrives as a `MediaItem` or a `ResearchItem`.

Licensing is part of the normal shape, not a footnote
-----------------------------------------------------
Every `MediaItem` carries the licence it arrived under, who to credit, and the
page it came from. That is not bookkeeping: the licences genuinely differ, and
three of them impose obligations this code has to honour rather than record.

  Pexels    free to use, no attribution required
  Pixabay   free to use, no attribution required; their terms forbid
            hot-linking, so the file is downloaded rather than embedded
  Unsplash  free to use, but attribution to the photographer *and* Unsplash is
            required, and a use must ping the photo's download endpoint
  Commons   per file, and most of it is unusable here -- the licence is read
            from the file's own metadata and checked against the reuse gate
            already used for footage

`requires_attribution` is set from the licence rather than passed in, so a
provider cannot forget it, and `to_provenance()` emits exactly the record the
image sourcer already writes to `asset_provenance.json`.

Research items are metadata only
--------------------------------
YouTube and NewsAPI return things this pipeline may read but must not
republish: a news article's text is the publisher's, and a YouTube video's file
is never offered by the API in the first place. `ResearchItem` therefore
carries a headline, a summary and a link, and no body text and no media.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable, Protocol, TypeVar

import httpx

logger = logging.getLogger("video_factory")

T = TypeVar("T")


class Availability(str, Enum):
    READY = "ready"
    NEEDS_CREDENTIALS = "needs_credentials"
    #: Reachable, but not for this kind of request.
    NOT_APPLICABLE = "not_applicable"


class MediaKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"


@dataclass
class ProviderStatus:
    name: str
    availability: Availability
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.availability is Availability.READY

    def to_record(self) -> dict:
        return {
            "provider": self.name,
            "availability": self.availability.value,
            "reason": self.reason,
        }


#: Licences that oblige the user to credit somebody. Pexels and Pixabay do not;
#: Unsplash does by its API guidelines even though its licence text does not;
#: CC-BY and its share-alike variants do by the licence itself.
_ATTRIBUTION_REQUIRED = ("cc by", "cc-by", "unsplash")

#: Licences that place no obligation on the user at all.
_NO_OBLIGATION = ("cc0", "public domain", "pdm", "pd")


def requires_attribution(licence: str) -> bool:
    """Whether using this material obliges us to credit somebody.

    A licence we do not recognise is treated as the strictest case, not the
    most convenient one. Crediting where no credit was due costs a line of
    text; the reverse is a licence breach.
    """
    text = str(licence or "").strip().lower()
    if not text:
        return True
    if any(token in text for token in _NO_OBLIGATION):
        return False
    if any(token in text for token in _ATTRIBUTION_REQUIRED):
        return True
    # Recognised as permissive by name, e.g. the Pexels and Pixabay licences.
    if "no attribution required" in text:
        return False
    return True


@dataclass
class MediaItem:
    """A picture, clip or sound, and everything needed to use it lawfully."""

    provider: str
    provider_id: str
    kind: MediaKind
    #: Where the bytes are.
    url: str
    #: The human-facing page, which attribution links to.
    source_page: str = ""
    licence: str = ""
    #: Who to credit. Present even when credit is not required, because the
    #: obligation can change and the record should not have to be rebuilt.
    attribution: str = ""
    title: str = ""
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0
    preview_url: str = ""
    query: str = ""
    #: Provider-specific follow-up a use must perform, e.g. Unsplash's
    #: download endpoint. Called through `report_use`.
    use_hook: str = ""

    @property
    def needs_attribution(self) -> bool:
        return requires_attribution(self.licence)

    @property
    def is_portrait(self) -> bool:
        return bool(self.height and self.width and self.height > self.width)

    def credit_line(self) -> str:
        """What a caption or description should say. Empty when nothing is due."""
        if not self.needs_attribution:
            return ""
        who = self.attribution or "Unknown"
        where = {
            "unsplash": "Unsplash",
            "wikimedia_commons": "Wikimedia Commons",
            "pixabay": "Pixabay",
            "pexels": "Pexels",
        }.get(self.provider, self.provider)
        licence = f" ({self.licence})" if self.licence else ""
        return f"{who} / {where}{licence}"

    def to_provenance(self) -> dict:
        """The record shape `core.image_sourcer` already writes per asset."""
        return {
            "platform": self.provider,
            "url": self.url,
            "source_page": self.source_page,
            "licence": self.licence,
            "attribution": self.attribution,
            "width": self.width,
            "height": self.height,
        }

    def to_record(self) -> dict:
        return {
            **self.to_provenance(),
            "provider_id": self.provider_id,
            "kind": self.kind.value,
            "title": self.title,
            "duration_seconds": self.duration_seconds,
            "preview_url": self.preview_url,
            "query": self.query,
            "needs_attribution": self.needs_attribution,
            "credit": self.credit_line(),
        }


@dataclass
class ResearchItem:
    """Something to read, with a link. Never a file to republish."""

    provider: str
    provider_id: str
    title: str
    url: str
    source: str = ""
    #: The provider's own short summary. Never the full body: an article's text
    #: belongs to its publisher.
    summary: str = ""
    author: str = ""
    published_at: datetime | None = None
    thumbnail_url: str = ""
    #: Whatever numbers the provider reports, unnormalised and optional.
    metrics: dict = field(default_factory=dict)
    query: str = ""

    def to_record(self) -> dict:
        return {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "summary": self.summary,
            "author": self.author,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "thumbnail_url": self.thumbnail_url,
            "metrics": dict(self.metrics),
            "query": self.query,
            # Reference material. Nothing here is a publishing input.
            "usage": "research_only",
        }


@dataclass
class ProviderResult:
    """What one provider returned, including how it failed."""

    provider: str
    media: list[MediaItem] = field(default_factory=list)
    research: list[ResearchItem] = field(default_factory=list)
    status: ProviderStatus | None = None
    error: str = ""
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return not self.error


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

#: Worth trying again: the provider is busy or briefly broken.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BASE_DELAY = 1.5
MAX_DELAY = 30.0


def is_retryable(exc: BaseException) -> bool:
    """Whether this failure might succeed on another attempt.

    An auth failure or a malformed query never will, and retrying one spends
    the caller's rate-limit budget to arrive at the same answer.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def retry_after_seconds(exc: BaseException) -> float | None:
    """The provider's own instruction, when it gave one."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    raw = exc.response.headers.get("retry-after", "")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def backoff_delay(attempt: int, *, jitter: bool = True) -> float:
    """Exponential, with jitter so parallel providers do not resynchronise."""
    delay = min(MAX_DELAY, BASE_DELAY * (2 ** max(0, attempt - 1)))
    return delay * (0.5 + random.random() / 2) if jitter else delay


async def with_retries(
    call: Callable[[], Awaitable[T]],
    *,
    provider: str,
    attempts: int = MAX_ATTEMPTS,
    sleep=asyncio.sleep,
) -> T:
    """Run `call`, retrying only what is worth retrying.

    A provider's own Retry-After wins over the computed backoff: it knows when
    it will serve us again and guessing shorter is how an app gets blocked.
    """
    last: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await call()
        except BaseException as exc:      # noqa: BLE001 - re-raised below
            last = exc
            if not is_retryable(exc) or attempt == attempts:
                raise
            delay = retry_after_seconds(exc)
            if delay is None:
                delay = backoff_delay(attempt)
            logger.info(
                f"[{provider}] attempt {attempt}/{attempts} failed ({exc}); "
                f"retrying in {delay:.1f}s"
            )
            await sleep(delay)
    assert last is not None                # pragma: no cover - unreachable
    raise last


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Concurrency cap plus a minimum gap between calls, per provider.

    Both halves matter. The cap keeps a fan-out from opening thirty sockets to
    one host; the gap is what stops a burst of parallel queries tripping a
    per-second limit -- which is how the Commons sourcing path started getting
    429s in the first place.
    """

    def __init__(self, *, concurrency: int = 4, min_interval: float = 0.0) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = max(0.0, min_interval)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self._semaphore.acquire()
        if self._min_interval:
            async with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
        return self

    async def __aexit__(self, *exc_info) -> bool:
        self._semaphore.release()
        return False


class MediaProvider(Protocol):
    """A source of pictures, clips or sound."""

    name: str

    def status(self) -> ProviderStatus: ...

    async def search(
        self, query: str, *, client: httpx.AsyncClient, limit: int, **kwargs
    ) -> list[MediaItem]: ...


class ResearchProvider(Protocol):
    """A source of things to read about a subject."""

    name: str

    def status(self) -> ProviderStatus: ...

    async def search(
        self, query: str, *, client: httpx.AsyncClient, limit: int, **kwargs
    ) -> list[ResearchItem]: ...


def credential(*names: str) -> str:
    """The first of these settings or environment variables that is set.

    Settings first so a .env parsed at import time wins, environment second so
    a container can override without a file.
    """
    import os

    try:
        from settings import settings
    except Exception:                      # pragma: no cover - settings absent
        settings = None

    for name in names:
        if settings is not None:
            value = str(getattr(settings, name.lower(), "") or "").strip()
            if value:
                return value
        value = str(os.environ.get(name.upper(), "") or "").strip()
        if value:
            return value
    return ""
