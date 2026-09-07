"""Permanent per-channel video library.

A finished video currently lives only in the job row, which is a queue record:
it is pruned, it can be superseded, and it disappears when the queue is
cleared. This module is the durable copy -- once a video is registered it stays
in the library and the website can list it forever.

Layout, one folder per video:

    videos/{channel_id}/{video_id}/video.mp4
    videos/{channel_id}/{video_id}/thumbnail.png
    videos/{channel_id}/{video_id}/metadata.json

Channels are separated by the path, so Horror Stories and Football News share
no object, no index and no listing. Adding a channel needs no code here.

    videos/{channel_id}/index.json

The index is what the website reads. Listing a bucket needs credentials;
fetching one public JSON does not, so the site stays credential-free. It is a
cache of the per-video metadata.json files, which remain the source of truth --
a lost index can be rebuilt from them.

Storage is reached only through `worker.storage`, and every record is a plain
dict, so replacing the backend with a database later means reimplementing
`VideoLibrary` against the same three methods rather than touching callers.
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("worker.library")

LIBRARY_ROOT = "videos"
INDEX_NAME = "index.json"
_SAFE_ID = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


def _safe(value: str, *, field_name: str) -> str:
    """Reject anything that could escape its folder or break a URL.

    Rejects rather than sanitises. Silently rewriting "../escape" to "escape"
    would be safe but would file the video under an id its caller never chose,
    and the mismatch would only surface as a video the library cannot find.
    Real ids -- UUID job ids and snake_case channel slugs -- pass untouched.
    """
    raw = str(value or "")
    cleaned = "".join(ch for ch in raw if ch in _SAFE_ID)
    if not cleaned:
        raise ValueError(f"{field_name} is empty or contains no usable characters")
    if cleaned != raw:
        raise ValueError(
            f"{field_name} {raw!r} contains characters that are not allowed in a "
            f"storage path; use letters, digits, '-' or '_'"
        )
    return cleaned


@dataclass
class VideoRecord:
    """One permanently saved video.

    Mirrors what a database row would hold, so the move off object storage is a
    change of backend rather than of shape.
    """

    video_id: str
    job_id: str
    channel_id: str
    title: str
    description: str = ""
    created_at: str = ""
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    video_path: str = ""
    thumbnail_path: str = ""
    video_url: str = ""
    thumbnail_url: str = ""
    review_status: str = ""
    review_log: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.video_id = _safe(self.video_id, field_name="video_id")
        self.channel_id = _safe(self.channel_id, field_name="channel_id")
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["resolution"] = self.resolution
        return data


def channel_prefix(channel_id: str) -> str:
    return f"{LIBRARY_ROOT}/{_safe(channel_id, field_name='channel_id')}"


def video_prefix(channel_id: str, video_id: str) -> str:
    return f"{channel_prefix(channel_id)}/{_safe(video_id, field_name='video_id')}"


def index_path(channel_id: str) -> str:
    return f"{channel_prefix(channel_id)}/{INDEX_NAME}"


class VideoLibrary:
    """Repository over object storage.

    `storage` is injected rather than imported so tests can drive a fake and a
    future database backend can be swapped in without changing callers. It must
    provide upload_media(path, object_name, content_type) -> url,
    list_media(prefix) -> list[dict] and delete_media(names).
    """

    def __init__(self, storage) -> None:
        self._storage = storage

    # -- reading ------------------------------------------------------------

    def _read_json_object(self, object_name: str) -> object | None:
        """Fetch a stored JSON object through its public URL."""
        import httpx

        url = self._public_url(object_name)
        if not url:
            return None
        try:
            resp = httpx.get(url, timeout=20, follow_redirects=True)
            if resp.status_code >= 400:
                return None
            return resp.json()
        except Exception as exc:  # network, JSON, anything
            logger.debug(f"could not read {object_name}: {exc}")
            return None

    def _public_url(self, object_name: str) -> str:
        getter = getattr(self._storage, "public_url", None)
        if callable(getter):
            return getter(object_name)
        return ""

    def list_channel(self, channel_id: str) -> list[dict]:
        """Every saved video for a channel, newest first."""
        data = self._read_json_object(index_path(channel_id))
        records = data if isinstance(data, list) else []
        return sorted(
            (r for r in records if isinstance(r, dict)),
            key=lambda r: str(r.get("created_at", "")),
            reverse=True,
        )

    def is_registered(self, channel_id: str, video_id: str) -> bool:
        """Whether this video already has a folder in the library.

        Checked against storage rather than the index, because the index is a
        cache: a video whose objects exist is registered even if an index write
        was lost, and re-uploading it would waste the bandwidth and risk
        replacing a good file with a worse one.
        """
        prefix = video_prefix(channel_id, video_id)
        try:
            existing = self._storage.list_media(prefix)
        except Exception as exc:
            logger.warning(f"could not check the library for {video_id}: {exc}")
            return False
        names = {str(item.get("name", "")) for item in existing or []}
        return any(name.endswith("/metadata.json") for name in names)

    # -- writing ------------------------------------------------------------

    def _upload_json(self, payload: object, object_name: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return self._storage.upload_media(path, object_name, "application/json")

    def register(
        self,
        record: VideoRecord,
        *,
        video_file: Path,
        thumbnail_file: Path | None = None,
        overwrite: bool = False,
    ) -> VideoRecord:
        """Save a finished video permanently. Idempotent unless `overwrite`."""
        if not video_file.exists():
            raise FileNotFoundError(f"video not found: {video_file}")

        prefix = video_prefix(record.channel_id, record.video_id)

        if not overwrite and self.is_registered(record.channel_id, record.video_id):
            logger.info(
                f"{record.video_id} is already in the {record.channel_id} "
                f"library; leaving it as it is"
            )
            return record

        record.video_path = f"{prefix}/video.mp4"
        record.video_url = self._storage.upload_media(
            video_file, record.video_path, "video/mp4"
        )

        if thumbnail_file is not None and thumbnail_file.exists():
            record.thumbnail_path = f"{prefix}/thumbnail.png"
            record.thumbnail_url = self._storage.upload_media(
                thumbnail_file, record.thumbnail_path, "image/png"
            )

        self._upload_json(record.to_dict(), f"{prefix}/metadata.json")
        self._refresh_index(record)
        logger.info(f"library: saved {record.video_id} to {record.channel_id}")
        return record

    def _refresh_index(self, record: VideoRecord) -> None:
        """Add or replace this video in its channel index."""
        existing = self.list_channel(record.channel_id)
        merged = [r for r in existing if r.get("video_id") != record.video_id]
        merged.append(record.to_dict())
        merged.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        try:
            self._upload_json(merged, index_path(record.channel_id))
        except Exception as exc:
            # The per-video metadata.json already landed, so the video is saved
            # even if the listing is briefly stale.
            logger.warning(f"library index update failed: {exc}")
