"""Concrete publishing adapters, one per platform's official API.

Each adapter speaks its platform's documented endpoints and nothing else. No
adapter logs a token, and none of them retries internally -- retry is the
queue's job, because only the queue knows how many attempts a job has already
spent and whether the failure is worth another one.

Every adapter takes an injected HTTP client, so the request shapes below are
testable against a mock transport without a live account or a real upload.

  youtube    resumable `videos.insert`: a metadata POST that returns an upload
             URL, then the bytes. `status.publishAt` schedules natively, and
             requires `privacyStatus: private` until then -- posting a public
             video with a future publishAt silently publishes immediately,
             which is the failure mode this adapter exists to avoid.
  facebook   Page `/videos` with `file_url`. Native scheduling through
             `scheduled_publish_time` with `published=false`.
  instagram  Two steps and a wait: create a REELS container from a public URL,
             poll it to FINISHED, then publish. The poll matters -- publishing
             an IN_PROGRESS container fails with a misleading error.
  tiktok     Content Posting API with PULL_FROM_URL. Unaudited apps may only
             reach the user's drafts, so the adapter sends to the inbox
             endpoint and says so in the result rather than pretending a post
             went live.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from viral.publishing import (
    CAPABILITIES,
    Capability,
    PublishRequest,
    PublishResult,
    PublishStatus,
    is_retryable,
)

logger = logging.getLogger("viral.adapters")

DEFAULT_TIMEOUT = 120.0


class AdapterError(Exception):
    """A platform rejected the request in a way worth surfacing verbatim."""


def _client(client=None):
    if client is not None:
        return client
    import httpx

    return httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True)


def _failure(platform: str, response, action: str) -> PublishResult:
    """Turn a platform error into a result, without leaking request headers."""
    status = getattr(response, "status_code", None)
    try:
        body = response.json()
        message = (
            body.get("error", {}).get("message")
            or body.get("error", {}).get("error_user_msg")
            or body.get("error_description")
            or str(body)[:300]
        )
    except Exception:
        message = str(getattr(response, "text", ""))[:300]
    return PublishResult(
        platform=platform,
        status=PublishStatus.FAILED,
        error=f"{action}: {message}",
        retryable=is_retryable(status, message),
    )


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

class YouTubeAdapter:
    """Uploads a Short via the Data API v3, with native scheduling."""

    platform = "youtube"
    UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

    def __init__(self, client=None, chunk_size: int = 8 * 1024 * 1024) -> None:
        self._client = client
        self._chunk_size = chunk_size

    def publish(
        self,
        request: PublishRequest,
        access_token: str,
        *,
        media_file: Path | None = None,
        title: str = "",
    ) -> PublishResult:
        if media_file is None or not Path(media_file).exists():
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error="The processed video file is not available locally.",
                retryable=False,
            )
        path = Path(media_file)
        caption = request.caption_for(self.platform)
        headline = title or (caption.splitlines()[0] if caption else "") or "Short"
        body: dict = {
            "snippet": {
                "title": headline[:100],
                "description": caption,
                "categoryId": "22",
            },
            "status": {"selfDeclaredMadeForKids": False},
        }
        if request.scheduled_for:
            # publishAt is only honoured while the video is private.
            body["status"]["privacyStatus"] = "private"
            body["status"]["publishAt"] = _iso_z(request.scheduled_for)
        else:
            body["status"]["privacyStatus"] = "public"

        client = _client(self._client)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(path.stat().st_size),
        }
        started = client.post(
            self.UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers=headers, json=body,
        )
        if started.status_code >= 400:
            return _failure(self.platform, started, "starting the upload")

        upload_url = started.headers.get("location") or started.headers.get("Location")
        if not upload_url:
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error="YouTube accepted the metadata but returned no upload URL.",
                retryable=True,
            )

        uploaded = client.put(
            upload_url,
            headers={"Content-Type": "video/mp4"},
            content=path.read_bytes(),
        )
        if uploaded.status_code >= 400:
            return _failure(self.platform, uploaded, "uploading the video")

        video_id = (uploaded.json() or {}).get("id", "")
        return PublishResult(
            platform=self.platform,
            status=PublishStatus.PUBLISHED,
            post_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        )


# ---------------------------------------------------------------------------
# Facebook Pages
# ---------------------------------------------------------------------------

class FacebookAdapter:
    """Publishes to a Page the user manages, with native scheduling."""

    platform = "facebook"
    GRAPH = "https://graph.facebook.com/v21.0"

    def __init__(self, client=None) -> None:
        self._client = client

    def publish(
        self,
        request: PublishRequest,
        access_token: str,
        *,
        page_id: str = "",
        media_url: str = "",
    ) -> PublishResult:
        if not page_id:
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error="No Facebook Page selected for this account.",
                retryable=False,
            )
        if not media_url:
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error="Facebook pulls the file from a public URL; none was given.",
                retryable=False,
            )

        payload = {
            "file_url": media_url,
            "description": request.caption_for(self.platform),
            "access_token": access_token,
        }
        if request.scheduled_for:
            payload["published"] = "false"
            payload["scheduled_publish_time"] = str(
                int(request.scheduled_for.timestamp()))

        response = _client(self._client).post(
            f"{self.GRAPH}/{page_id}/videos", data=payload)
        if response.status_code >= 400:
            return _failure(self.platform, response, "publishing to the Page")

        post_id = (response.json() or {}).get("id", "")
        return PublishResult(
            platform=self.platform,
            status=PublishStatus.PUBLISHED,
            post_id=post_id,
            url=f"https://www.facebook.com/{post_id}" if post_id else "",
        )


# ---------------------------------------------------------------------------
# Instagram Reels
# ---------------------------------------------------------------------------

class InstagramAdapter:
    """Container, wait, publish. No native scheduling exists."""

    platform = "instagram"
    GRAPH = "https://graph.facebook.com/v21.0"
    POLL_SECONDS = 5.0
    MAX_POLLS = 60

    def __init__(self, client=None, sleep=time.sleep) -> None:
        self._client = client
        self._sleep = sleep

    def publish(
        self,
        request: PublishRequest,
        access_token: str,
        *,
        ig_user_id: str = "",
        media_url: str = "",
    ) -> PublishResult:
        if not ig_user_id or not media_url:
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error=(
                    "Instagram needs a connected Business account and a public "
                    "media URL."
                ),
                retryable=False,
            )

        client = _client(self._client)
        created = client.post(f"{self.GRAPH}/{ig_user_id}/media", data={
            "media_type": "REELS",
            "video_url": media_url,
            "caption": request.caption_for(self.platform),
            "access_token": access_token,
        })
        if created.status_code >= 400:
            return _failure(self.platform, created, "creating the Reel container")

        container_id = (created.json() or {}).get("id", "")
        if not container_id:
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error="Instagram returned no container id.", retryable=True)

        # Instagram transcodes before the container can be published.
        for _ in range(self.MAX_POLLS):
            status = client.get(f"{self.GRAPH}/{container_id}", params={
                "fields": "status_code,status",
                "access_token": access_token,
            })
            if status.status_code >= 400:
                return _failure(self.platform, status, "checking the container")
            code = (status.json() or {}).get("status_code", "")
            if code == "FINISHED":
                break
            if code == "ERROR":
                return PublishResult(
                    self.platform, PublishStatus.FAILED,
                    error=(
                        "Instagram could not process the video: "
                        f"{(status.json() or {}).get('status', '')}"
                    ),
                    retryable=False,
                )
            self._sleep(self.POLL_SECONDS)
        else:
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error="Instagram did not finish processing in time.",
                retryable=True,
            )

        published = client.post(f"{self.GRAPH}/{ig_user_id}/media_publish", data={
            "creation_id": container_id,
            "access_token": access_token,
        })
        if published.status_code >= 400:
            return _failure(self.platform, published, "publishing the Reel")

        post_id = (published.json() or {}).get("id", "")
        return PublishResult(
            platform=self.platform,
            status=PublishStatus.PUBLISHED,
            post_id=post_id,
        )


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

class TikTokAdapter:
    """Content Posting API. Direct Post only when the app has been audited."""

    platform = "tiktok"
    DIRECT_INIT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    INBOX_INIT = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"

    def __init__(self, client=None, *, direct_post_approved: bool = False) -> None:
        self._client = client
        self._direct = bool(direct_post_approved)

    def publish(
        self,
        request: PublishRequest,
        access_token: str,
        *,
        media_url: str = "",
    ) -> PublishResult:
        if not media_url:
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error="TikTok pulls the file from a verified URL; none was given.",
                retryable=False,
            )

        body: dict = {"source_info": {
            "source": "PULL_FROM_URL",
            "video_url": media_url,
        }}
        if self._direct:
            body["post_info"] = {
                "title": request.caption_for(self.platform)[:2200],
                "privacy_level": "PUBLIC_TO_EVERYONE",
            }
        endpoint = self.DIRECT_INIT if self._direct else self.INBOX_INIT

        response = _client(self._client).post(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json; charset=UTF-8"},
            json=body,
        )
        if response.status_code >= 400:
            return _failure(self.platform, response, "starting the TikTok upload")

        payload = response.json() or {}
        error = payload.get("error") or {}
        if error.get("code") not in (None, "", "ok"):
            return PublishResult(
                self.platform, PublishStatus.FAILED,
                error=f"{error.get('code')}: {error.get('message', '')}",
                retryable=is_retryable(None, error.get("message", "")),
            )

        publish_id = (payload.get("data") or {}).get("publish_id", "")
        if self._direct:
            return PublishResult(
                self.platform, PublishStatus.PUBLISHED, post_id=publish_id)
        # Honest about what actually happened: it is in their drafts.
        return PublishResult(
            platform=self.platform,
            status=PublishStatus.PUBLISHED,
            post_id=publish_id,
            error=(
                "Sent to the TikTok app's drafts. Direct posting requires an "
                "audited app; open TikTok to finish posting."
            ),
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class AdapterContext:
    """Everything an adapter needs besides the request and the token."""

    media_url: str = ""
    media_file: Path | None = None
    page_id: str = ""
    ig_user_id: str = ""
    title: str = ""


def build_adapters(*, client=None, tiktok_direct_post: bool = False) -> dict:
    from viral.publishing import UnsupportedAdapter

    return {
        "youtube": YouTubeAdapter(client=client),
        "facebook": FacebookAdapter(client=client),
        "instagram": InstagramAdapter(client=client),
        "tiktok": TikTokAdapter(client=client, direct_post_approved=tiktok_direct_post),
        "snapchat": UnsupportedAdapter("snapchat"),
    }


def publish_to(
    platform: str,
    request: PublishRequest,
    access_token: str,
    context: AdapterContext,
    *,
    adapters: dict | None = None,
) -> PublishResult:
    """Dispatch to the right adapter with the arguments it needs."""
    key = str(platform or "").strip().lower()
    registry = adapters if adapters is not None else build_adapters()
    adapter = registry.get(key)
    if adapter is None:
        return PublishResult(
            key, PublishStatus.UNSUPPORTED,
            error=f"Unknown platform {platform!r}.", retryable=False)

    capability = CAPABILITIES.get(key)
    if capability and Capability.UNSUPPORTED in capability.capabilities:
        return adapter.publish(request, access_token)

    extra = {
        "youtube": {"media_file": context.media_file, "title": context.title},
        "facebook": {"page_id": context.page_id, "media_url": context.media_url},
        "instagram": {"ig_user_id": context.ig_user_id,
                      "media_url": context.media_url},
        "tiktok": {"media_url": context.media_url},
    }.get(key, {})
    return adapter.publish(request, access_token, **extra)


def _iso_z(when: datetime) -> str:
    reference = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return reference.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
