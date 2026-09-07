"""Pluggable object storage for finished media.

One backend per process, chosen by MEDIA_STORAGE_PROVIDER. Every factory
channel (football_news, horror, history, ...) shares this layer, so adding a
channel never means adding storage code.

Backends
--------
gcs       Google Cloud Storage (default). Resumable uploads, so a 120 MB MP4
          goes up in 8 MiB chunks that survive a dropped connection. Auth is
          Application Default Credentials -- the same ADC the pipeline already
          uses for Vertex AI and STT, so there is no new secret to manage.
supabase  The previous backend, kept so an existing deployment can roll back
          with one env var. Supabase's free tier rejects any object over
          50 MB (EntityTooLarge) on BOTH the standard and the resumable/TUS
          endpoint, which is why it cannot hold finished videos.

Every backend returns a public https URL that a plain <video src> can play,
so the job rows, the API and Recent Videos are unaffected by the choice.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

import httpx

logger = logging.getLogger("worker.storage")

PROVIDER = os.environ.get("MEDIA_STORAGE_PROVIDER", "gcs").strip().lower()

# --- GCS -------------------------------------------------------------------
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
# Resumable chunk size. GCS requires every non-final chunk to be a multiple of
# 256 KiB; 8 MiB keeps the request count low without holding much in memory.
_CHUNK = 8 * 1024 * 1024
_GCS_API = "https://storage.googleapis.com/storage/v1"
_GCS_UPLOAD = "https://storage.googleapis.com/upload/storage/v1"

# --- Supabase (legacy) -----------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "media")


class StorageError(RuntimeError):
    """Raised when the backend refuses or fails an operation."""


# ---------------------------------------------------------------------------
# Config surface used by the worker's preflight
# ---------------------------------------------------------------------------

def required_env() -> tuple[tuple[str, str], ...]:
    """Env vars this provider needs, as (name, value) pairs for preflight."""
    if PROVIDER == "gcs":
        return (("GCS_BUCKET", GCS_BUCKET),)
    return (("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY))


def describe() -> str:
    return f"{PROVIDER}:{GCS_BUCKET or SUPABASE_BUCKET}"


# ---------------------------------------------------------------------------
# GCS
# ---------------------------------------------------------------------------

_gcs_creds = None


def _gcs_token() -> str:
    """A fresh OAuth token from ADC, refreshed only when actually expired."""
    global _gcs_creds
    import google.auth
    from google.auth.transport.requests import Request

    if _gcs_creds is None:
        _gcs_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
        )
    if not _gcs_creds.valid:
        _gcs_creds.refresh(Request())
    return _gcs_creds.token


def _gcs_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {_gcs_token()}"}


def _gcs_public_url(object_name: str) -> str:
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{quote(object_name)}"


def _gcs_upload(path: Path, object_name: str, content_type: str) -> str:
    """Resumable upload. Returns the object's public URL.

    Streams the file in _CHUNK slices rather than reading the whole MP4 into
    memory, and retries a failed chunk from the offset the server reports
    instead of restarting the upload.
    """
    size = path.stat().st_size

    start = httpx.post(
        f"{_GCS_UPLOAD}/b/{GCS_BUCKET}/o?uploadType=resumable"
        f"&name={quote(object_name, safe='')}",
        headers={
            **_gcs_headers(),
            "content-type": "application/json; charset=UTF-8",
            "x-upload-content-type": content_type,
            "x-upload-content-length": str(size),
        },
        json={
            "name": object_name,
            "contentType": content_type,
            "cacheControl": "public, max-age=31536000, immutable",
        },
        timeout=httpx.Timeout(120.0, connect=30.0),
    )
    if start.status_code >= 400:
        raise StorageError(
            f"GCS resumable session failed ({start.status_code}): "
            f"{start.text[:300]}"
        )
    session = start.headers.get("location")
    if not session:
        raise StorageError("GCS resumable session returned no Location header")

    offset = 0
    with path.open("rb") as handle:
        while offset < size:
            handle.seek(offset)
            chunk = handle.read(_CHUNK)
            end = offset + len(chunk) - 1
            resp = httpx.put(
                session,
                content=chunk,
                headers={
                    "content-length": str(len(chunk)),
                    "content-range": f"bytes {offset}-{end}/{size}",
                },
                timeout=httpx.Timeout(600.0, connect=30.0),
            )
            if resp.status_code in (200, 201):
                offset = size
                break
            if resp.status_code == 308:
                # The server acknowledges bytes 0..N; continue from N+1. A
                # missing Range means it kept nothing, so resend this chunk.
                rng = resp.headers.get("range")
                offset = int(rng.split("-")[-1]) + 1 if rng else offset
                continue
            raise StorageError(
                f"GCS chunk upload failed ({resp.status_code}) at byte "
                f"{offset}: {resp.text[:300]}"
            )

    return _gcs_public_url(object_name)


def _gcs_list(prefix: str) -> list[dict]:
    """Objects under `prefix`, oldest first, as {name, created} dicts."""
    items: list[dict] = []
    token = None
    while True:
        url = (
            f"{_GCS_API}/b/{GCS_BUCKET}/o?prefix={quote(prefix, safe='')}"
            f"&fields=items(name,timeCreated),nextPageToken&maxResults=1000"
        )
        if token:
            url += f"&pageToken={token}"
        r = httpx.get(url, headers=_gcs_headers(), timeout=60.0)
        r.raise_for_status()
        body = r.json()
        items.extend(
            {"name": o["name"], "created": o.get("timeCreated", "")}
            for o in body.get("items", [])
        )
        token = body.get("nextPageToken")
        if not token:
            break
    items.sort(key=lambda o: o["created"])
    return items


def _gcs_delete(names: list[str]) -> None:
    for name in names:
        r = httpx.delete(
            f"{_GCS_API}/b/{GCS_BUCKET}/o/{quote(name, safe='')}",
            headers=_gcs_headers(),
            timeout=60.0,
        )
        # 404 means it is already gone, which is the state we wanted.
        if r.status_code >= 400 and r.status_code != 404:
            raise StorageError(
                f"GCS delete failed for {name} ({r.status_code}): {r.text[:200]}"
            )


# ---------------------------------------------------------------------------
# Supabase (legacy)
# ---------------------------------------------------------------------------

def _sb_root() -> str:
    return f"{SUPABASE_URL}/storage/v1"


def _sb_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}


def _sb_upload(path: Path, object_name: str, content_type: str) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        resp = httpx.post(
            f"{_sb_root()}/object/{SUPABASE_BUCKET}/{object_name}",
            content=handle,
            headers={
                **_sb_headers(),
                "content-type": content_type,
                "content-length": str(size),
                "cache-control": "31536000",
                "x-upsert": "true",
            },
            timeout=httpx.Timeout(600.0, connect=30.0),
        )
    if resp.status_code >= 400:
        raise StorageError(
            f"Storage upload failed ({resp.status_code}): {resp.text[:300]}"
        )
    return f"{_sb_root()}/object/public/{SUPABASE_BUCKET}/{object_name}"


def _sb_list(prefix: str) -> list[dict]:
    r = httpx.post(
        f"{_sb_root()}/object/list/{SUPABASE_BUCKET}",
        headers={**_sb_headers(), "content-type": "application/json"},
        json={
            "prefix": prefix,
            "limit": 500,
            "sortBy": {"column": "created_at", "order": "asc"},
        },
        timeout=60.0,
    )
    r.raise_for_status()
    # Supabase returns names relative to the prefix; re-attach it so callers
    # see the same full object names GCS reports.
    return [
        {"name": f"{prefix}/{o['name']}", "created": o.get("created_at", "")}
        for o in r.json()
    ]


def _sb_delete(names: list[str]) -> None:
    httpx.request(
        "DELETE",
        f"{_sb_root()}/object/{SUPABASE_BUCKET}",
        headers={**_sb_headers(), "content-type": "application/json"},
        json={"prefixes": names},
        timeout=120.0,
    ).raise_for_status()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_media(path: Path, object_name: str, content_type: str) -> str:
    """Upload a file and return its public URL."""
    size = path.stat().st_size
    logger.info(
        f"uploading {path.name} ({size / 1e6:.1f} MB) -> "
        f"{PROVIDER}:{object_name}"
    )
    if PROVIDER == "gcs":
        url = _gcs_upload(path, object_name, content_type)
    else:
        url = _sb_upload(path, object_name, content_type)
    logger.info(f"uploaded -> {url}")
    return url


def public_url(object_name: str) -> str:
    """The URL an object is readable at, without uploading anything.

    The library needs this to read back an index it has already written, and
    the website needs it to fetch one without credentials.
    """
    if PROVIDER == "gcs":
        return _gcs_public_url(object_name)
    if not SUPABASE_URL:
        return ""
    return (
        f"{SUPABASE_URL}/storage/v1/object/public/"
        f"{SUPABASE_BUCKET}/{quote(object_name)}"
    )


def list_media(prefix: str) -> list[dict]:
    """Objects under `prefix`, oldest first."""
    return _gcs_list(prefix) if PROVIDER == "gcs" else _sb_list(prefix)


def delete_media(names: list[str]) -> None:
    """Delete the given full object names. Missing objects are not an error."""
    if not names:
        return
    if PROVIDER == "gcs":
        _gcs_delete(names)
    else:
        _sb_delete(names)
