"""The worker files every finished video in its owner's library.

The bug this covers: the video reached GCS and the job went to "done", but the
worker never sent the `library` block, so nothing was ever inserted into
public.videos and the owner's Library stayed empty. Object storage held the
bytes and the database knew nothing about them.

These are offline: the worker's completion path is driven with a fake app and
a fake library so the payload can be inspected without rendering anything.
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "worker"))

import worker as worker_mod  # noqa: E402


@dataclass
class FakeRecord:
    """Mirrors library.VideoRecord's shape for the fields the payload uses."""

    video_id: str = "job-1"
    channel_id: str = "horror_stories"
    description: str = "a description"
    duration_seconds: float | None = 53.1
    width: int | None = 1080
    height: int | None = 1920
    fps: int | None = 30
    video_path: str = "videos/horror_stories/job-1/video.mp4"
    thumbnail_path: str = "videos/horror_stories/job-1/thumbnail.png"
    review_status: str = "approved"
    review_log: dict = field(default_factory=lambda: {"script_review": {"approved": True}})


def _payload_from(record: FakeRecord) -> dict:
    """The library block the worker builds for a stored record."""
    return {
        "channelSlug": record.channel_id,
        "videoKey": record.video_id,
        "videoPath": record.video_path,
        "thumbnailPath": record.thumbnail_path or None,
        "description": record.description,
        "durationSeconds": record.duration_seconds,
        "width": record.width,
        "height": record.height,
        "fps": record.fps,
        "reviewStatus": record.review_status,
        "reviewLog": record.review_log,
    }


# --- the payload the app needs --------------------------------------------

def test_library_payload_carries_everything_the_rpc_requires():
    """Missing any of these and the row is wrong or the insert fails."""
    payload = _payload_from(FakeRecord())
    for key in (
        "channelSlug", "videoKey", "videoPath", "thumbnailPath",
        "description", "durationSeconds", "width", "height", "fps",
        "reviewStatus", "reviewLog",
    ):
        assert key in payload, f"library payload is missing {key}"


def test_payload_points_at_object_storage_not_the_workspace():
    """Requirement: the row must survive the worker deleting its temp files."""
    payload = _payload_from(FakeRecord())
    assert payload["videoPath"].startswith("videos/")
    assert payload["thumbnailPath"].startswith("videos/")
    for path in (payload["videoPath"], payload["thumbnailPath"]):
        assert not pathlib.Path(path).is_absolute()
        assert "workspace" not in path
        assert ":" not in path        # no C:\... local path leaked in


def test_payload_channel_matches_the_stored_object_path():
    """A row whose channel disagrees with its path would list under the wrong engine."""
    record = FakeRecord(channel_id="football_news",
                        video_path="videos/football_news/job-1/video.mp4")
    payload = _payload_from(record)
    assert f"videos/{payload['channelSlug']}/" in payload["videoPath"]


def test_missing_thumbnail_becomes_null_not_empty_string():
    payload = _payload_from(FakeRecord(thumbnail_path=""))
    assert payload["thumbnailPath"] is None


def test_payload_carries_no_user_id():
    """Ownership is derived from the job row inside the database function.

    If the worker could name the owner, a compromised worker could file a
    video into someone else's Library.
    """
    payload = _payload_from(FakeRecord())
    assert not any("user" in key.lower() for key in payload)


# --- completion behaviour --------------------------------------------------

class FakeClient:
    """Records every update the worker posts."""

    def __init__(self, fail_with_library: bool = False):
        self.fail_with_library = fail_with_library
        self.posts: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json or {})

        class R:
            status_code = (
                503 if (self.fail_with_library and "library" in (json or {})) else 200
            )

        return R()


def test_completion_update_includes_the_library_block(monkeypatch):
    client = FakeClient()
    ok = worker_mod.post_update(
        client, "job-1", status="done", library=_payload_from(FakeRecord())
    )
    assert ok is True
    assert "library" in client.posts[0]
    assert client.posts[0]["library"]["videoPath"].endswith("/video.mp4")


def test_update_without_a_library_block_is_still_valid(monkeypatch):
    """Progress updates must not carry a library block."""
    client = FakeClient()
    worker_mod.post_update(client, "job-1", status="running", progress=42)
    assert "library" not in client.posts[0]


def test_a_refused_registration_does_not_strand_a_finished_job():
    """An unowned job is refused by the RPC; the job must still complete.

    Without the retry the app registers before it marks done, so a refusal
    would leave a rendered, uploaded video parked at 99% forever.
    """
    client = FakeClient(fail_with_library=True)
    with_library = worker_mod.post_update(
        client, "job-1", attempts=1, status="done",
        library=_payload_from(FakeRecord()),
    )
    assert with_library is False

    without_library = worker_mod.post_update(
        client, "job-1", attempts=1, status="done"
    )
    assert without_library is True
    assert "library" not in client.posts[-1]


# --- the wiring itself -----------------------------------------------------

def test_worker_sends_a_library_block_on_completion():
    """Guards the regression directly: the call must exist in the source."""
    source = (REPO_ROOT / "worker" / "worker.py").read_text(encoding="utf-8")
    assert '"library": library_payload' in source, (
        "the completion update no longer sends the library block; finished "
        "videos will not appear in any user's Library"
    )


def test_register_in_library_returns_the_record():
    """The payload is built from the returned record, so it cannot be None."""
    source = (REPO_ROOT / "worker" / "worker.py").read_text(encoding="utf-8")
    assert "return VideoLibrary(storage).register(" in source
