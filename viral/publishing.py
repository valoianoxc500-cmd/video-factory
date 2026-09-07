"""Publishing and scheduling across connected accounts, via official APIs.

Capability honesty
------------------
Each platform supports a different subset of what the brief asks for, and the
differences are real rather than incidental:

  youtube    Data API v3 `videos.insert` uploads Shorts and supports native
             scheduled publishing via `status.publishAt`. Fully supported.
  instagram  Graph API two-step: create a REELS container from a public URL,
             then publish it. No native scheduling -- the queue holds the job
             until its time. Business/Creator accounts only.
  facebook   Graph API Page video publishing, and it does support native
             scheduling. Requires a Page access token.
  tiktok     Content Posting API. Direct Post requires an approved app and an
             audited scope; unaudited apps can only send to the user's drafts.
             No native scheduling.
  snapchat   No server-side publishing API exists. Creative Kit is an
             app-to-app share from a mobile client, which a backend cannot
             drive. Selecting Snapchat produces a clear refusal rather than a
             job that fails later for an unexplained reason.

`capability_report` is what the UI renders next to each checkbox, so a user
learns Snapchat is unavailable before scheduling rather than after.

Tokens
------
No credential is stored here. Adapters receive a short-lived access token
resolved per call from the encrypted per-user store, so a token never sits in
a job row, a log line, or this module's memory beyond the request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol

logger = logging.getLogger("viral.publishing")


class PublishStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class Capability(str, Enum):
    DIRECT_PUBLISH = "direct_publish"
    NATIVE_SCHEDULING = "native_scheduling"
    QUEUE_SCHEDULING = "queue_scheduling"   # we hold it and post on time
    DRAFT_ONLY = "draft_only"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PlatformCapabilities:
    platform: str
    capabilities: frozenset[Capability]
    requires: str = ""
    note: str = ""

    @property
    def can_publish(self) -> bool:
        return bool(
            self.capabilities & {Capability.DIRECT_PUBLISH, Capability.DRAFT_ONLY}
        )

    @property
    def can_schedule(self) -> bool:
        return bool(
            self.capabilities
            & {Capability.NATIVE_SCHEDULING, Capability.QUEUE_SCHEDULING}
        )


CAPABILITIES: dict[str, PlatformCapabilities] = {
    "youtube": PlatformCapabilities(
        "youtube",
        frozenset({Capability.DIRECT_PUBLISH, Capability.NATIVE_SCHEDULING}),
        requires="YouTube Data API v3, youtube.upload scope",
        note="Scheduled publishing is native via status.publishAt.",
    ),
    "instagram": PlatformCapabilities(
        "instagram",
        frozenset({Capability.DIRECT_PUBLISH, Capability.QUEUE_SCHEDULING}),
        requires="Instagram Graph API, Business or Creator account",
        note=(
            "Two-step container publish. No native scheduling, so the queue "
            "holds the job until its scheduled time."
        ),
    ),
    "facebook": PlatformCapabilities(
        "facebook",
        frozenset({Capability.DIRECT_PUBLISH, Capability.NATIVE_SCHEDULING}),
        requires="Facebook Graph API, Page access token",
    ),
    "tiktok": PlatformCapabilities(
        "tiktok",
        frozenset({Capability.DIRECT_PUBLISH, Capability.DRAFT_ONLY,
                   Capability.QUEUE_SCHEDULING}),
        requires="TikTok Content Posting API, audited app for Direct Post",
        note=(
            "Unaudited apps can only send to the user's drafts; Direct Post "
            "needs an approved audit."
        ),
    ),
    "snapchat": PlatformCapabilities(
        "snapchat",
        frozenset({Capability.UNSUPPORTED}),
        note=(
            "Snapchat has no server-side publishing API. Creative Kit is an "
            "app-to-app share from a mobile client and cannot be driven by a "
            "backend."
        ),
    ),
}


def capability_report() -> list[dict]:
    """What the UI shows beside each platform checkbox."""
    return [
        {
            "platform": cap.platform,
            "can_publish": cap.can_publish,
            "can_schedule": cap.can_schedule,
            "native_scheduling": Capability.NATIVE_SCHEDULING in cap.capabilities,
            "draft_only": (
                Capability.DRAFT_ONLY in cap.capabilities
                and Capability.DIRECT_PUBLISH not in cap.capabilities
            ),
            "supported": Capability.UNSUPPORTED not in cap.capabilities,
            "requires": cap.requires,
            "note": cap.note,
        }
        for cap in CAPABILITIES.values()
    ]


@dataclass
class PublishRequest:
    user_id: str
    video_id: str
    media_path: str            # object-storage path, not a local file
    caption: str = ""
    platforms: tuple[str, ...] = ()
    scheduled_for: datetime | None = None
    #: Credit line required by the rights basis, prepended to the caption.
    attribution: str = ""

    def caption_for(self, platform: str) -> str:
        """Caption with attribution, trimmed to the platform's limit."""
        text = self.caption.strip()
        if self.attribution:
            credit = self.attribution.strip()
            if credit and credit.lower() not in text.lower():
                text = f"{text}\n\n{credit}".strip()
        return text[: CAPTION_LIMITS.get(platform, 2200)]


CAPTION_LIMITS = {
    "youtube": 5000,
    "instagram": 2200,
    "facebook": 5000,
    "tiktok": 2200,
    "snapchat": 250,
}


@dataclass
class PublishResult:
    platform: str
    status: PublishStatus
    post_id: str = ""
    url: str = ""
    error: str = ""
    retryable: bool = False
    attempts: int = 0

    def to_record(self) -> dict:
        return {
            "platform": self.platform,
            "status": self.status.value,
            "post_id": self.post_id,
            "url": self.url,
            "error": self.error,
            "retryable": self.retryable,
            "attempts": self.attempts,
        }


class PublishAdapter(Protocol):
    platform: str

    def publish(self, request: PublishRequest, access_token: str) -> PublishResult: ...


class UnsupportedAdapter:
    """A platform with no server-side publishing API.

    Returns UNSUPPORTED rather than FAILED: a failure implies something went
    wrong and invites a retry, while this will never succeed no matter how
    many times it runs.
    """

    def __init__(self, platform: str):
        self.platform = platform

    def publish(self, request: PublishRequest, access_token: str) -> PublishResult:
        cap = CAPABILITIES.get(self.platform)
        return PublishResult(
            self.platform, PublishStatus.UNSUPPORTED,
            error=cap.note if cap else "Not supported.",
            retryable=False,
        )


def validate_request(request: PublishRequest, *, now: datetime | None = None) -> list[str]:
    """Problems that would make this request fail, found before it is queued."""
    problems: list[str] = []
    if not request.platforms:
        problems.append("Select at least one platform.")
    if not str(request.media_path).strip():
        problems.append("No processed video to publish.")

    for platform in request.platforms:
        cap = CAPABILITIES.get(platform)
        if cap is None:
            problems.append(f"Unknown platform {platform!r}.")
            continue
        if not cap.can_publish:
            problems.append(f"{platform}: {cap.note}")
        elif request.scheduled_for and not cap.can_schedule:
            problems.append(f"{platform} cannot be scheduled.")

    if request.scheduled_for:
        when = request.scheduled_for
        if when.tzinfo is None:
            problems.append("Scheduled time must include a timezone.")
        else:
            reference = now or datetime.now(timezone.utc)
            if when <= reference:
                problems.append("Scheduled time is in the past.")
            elif when > reference + timedelta(days=180):
                problems.append("Scheduled time is more than 180 days out.")
    return problems


def plan_publication(
    request: PublishRequest,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """One job per platform, with how each will actually be delivered.

    A platform that cannot do what was asked is planned as UNSUPPORTED here,
    so the user sees it immediately instead of discovering it when the job
    fails hours later.
    """
    jobs: list[dict] = []
    for platform in request.platforms:
        cap = CAPABILITIES.get(platform)
        if cap is None or not cap.can_publish:
            jobs.append({
                "platform": platform,
                "status": PublishStatus.UNSUPPORTED.value,
                "reason": (cap.note if cap else f"Unknown platform {platform!r}."),
                "deliver_at": None,
            })
            continue

        if request.scheduled_for and Capability.NATIVE_SCHEDULING in cap.capabilities:
            mode = "native_schedule"
        elif request.scheduled_for:
            mode = "queue_until_due"
        else:
            mode = "publish_now"

        draft_only = (
            Capability.DRAFT_ONLY in cap.capabilities
            and Capability.DIRECT_PUBLISH not in cap.capabilities
        )
        jobs.append({
            "platform": platform,
            "status": PublishStatus.QUEUED.value,
            "mode": mode,
            "draft_only": draft_only,
            "deliver_at": (
                request.scheduled_for.isoformat() if request.scheduled_for else None
            ),
            "caption": request.caption_for(platform),
        })
    return jobs


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

#: Errors worth retrying: transient transport and the platforms' own rate
#: limits. An auth or validation failure is not retried -- repeating a
#: rejected request wastes the user's rate-limit budget and never succeeds.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5


def is_retryable(status_code: int | None, message: str = "") -> bool:
    if status_code in RETRYABLE_STATUS:
        return True
    text = str(message or "").lower()
    return any(
        token in text
        for token in ("timeout", "timed out", "temporarily", "rate limit",
                      "try again", "connection reset")
    )


def backoff_seconds(attempt: int, *, base: float = 30.0, cap: float = 3600.0) -> float:
    """Exponential backoff. Respects the platforms' rate limits rather than
    hammering them, which is itself a term-of-service obligation."""
    if attempt < 1:
        return base
    return min(cap, base * (2 ** (attempt - 1)))
