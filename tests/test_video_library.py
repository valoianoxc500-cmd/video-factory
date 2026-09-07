"""Permanent per-channel video library.

A job row is a queue record -- pruned, superseded, gone when the queue clears.
The library is the durable copy the website lists.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "worker"))

from library import (  # noqa: E402
    VideoLibrary,
    VideoRecord,
    channel_prefix,
    index_path,
    video_prefix,
)


class FakeStorage:
    """In-memory stand-in with the same three methods as worker.storage."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def upload_media(self, path: Path, object_name: str, content_type: str) -> str:
        self.objects[object_name] = Path(path).read_bytes()
        self.uploads.append(object_name)
        return self.public_url(object_name)

    def list_media(self, prefix: str) -> list[dict]:
        return [
            {"name": name, "size": len(blob)}
            for name, blob in self.objects.items()
            if name.startswith(prefix)
        ]

    def delete_media(self, names: list[str]) -> None:
        for name in names:
            self.objects.pop(name, None)

    def public_url(self, object_name: str) -> str:
        return f"https://storage.example/{object_name}"


@pytest.fixture
def library(monkeypatch):
    store = FakeStorage()
    lib = VideoLibrary(store)

    # list_channel/_read_json_object fetch over https; serve from the fake.
    def fake_get(url, **_kwargs):
        name = url.replace("https://storage.example/", "")

        class Resp:
            status_code = 200 if name in store.objects else 404

            def json(self):
                return json.loads(store.objects[name].decode("utf-8"))

        return Resp()

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    return lib, store


@pytest.fixture
def media(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 2048)
    thumb = tmp_path / "thumbnail.png"
    thumb.write_bytes(b"\x89PNG" + b"\x00" * 512)
    return video, thumb


def _record(**over) -> VideoRecord:
    base = dict(
        video_id="vid-1",
        job_id="job-1",
        channel_id="horror_stories",
        title="من هو دي بي كوبر؟",
        description="قصة حقيقية",
        duration_seconds=60.0,
        width=1080,
        height=1920,
        fps=30,
        review_status="approved",
    )
    base.update(over)
    return VideoRecord(**base)


# --- object layout ---------------------------------------------------------

def test_the_three_objects_land_at_the_required_paths(library, media):
    lib, store = library
    video, thumb = media
    lib.register(_record(), video_file=video, thumbnail_file=thumb)

    for name in (
        "videos/horror_stories/vid-1/video.mp4",
        "videos/horror_stories/vid-1/thumbnail.png",
        "videos/horror_stories/vid-1/metadata.json",
    ):
        assert name in store.objects, f"missing {name}"


def test_path_helpers_match_the_layout():
    assert channel_prefix("horror_stories") == "videos/horror_stories"
    assert video_prefix("horror_stories", "vid-1") == "videos/horror_stories/vid-1"
    assert index_path("horror_stories") == "videos/horror_stories/index.json"


# --- metadata --------------------------------------------------------------

def test_metadata_carries_every_required_field(library, media):
    lib, store = library
    video, thumb = media
    lib.register(_record(), video_file=video, thumbnail_file=thumb)

    meta = json.loads(
        store.objects["videos/horror_stories/vid-1/metadata.json"].decode("utf-8")
    )
    for key in (
        "title", "description", "channel_id", "created_at", "duration_seconds",
        "resolution", "fps", "video_path", "thumbnail_path", "review_status",
        "job_id", "video_id",
    ):
        assert key in meta, f"metadata is missing {key}"
    assert meta["resolution"] == "1080x1920"
    assert meta["video_path"] == "videos/horror_stories/vid-1/video.mp4"
    assert meta["thumbnail_path"] == "videos/horror_stories/vid-1/thumbnail.png"


def test_created_at_is_filled_in_when_absent(library, media):
    lib, _ = library
    video, _thumb = media
    saved = lib.register(_record(), video_file=video)
    assert saved.created_at, "created_at was left empty"


# --- duplicate prevention --------------------------------------------------

def test_registering_twice_uploads_once(library, media):
    lib, store = library
    video, thumb = media
    lib.register(_record(), video_file=video, thumbnail_file=thumb)
    first = len(store.uploads)

    lib.register(_record(), video_file=video, thumbnail_file=thumb)
    assert len(store.uploads) == first, "the same video was uploaded twice"


def test_is_registered_reports_the_truth(library, media):
    lib, _ = library
    video, _thumb = media
    assert lib.is_registered("horror_stories", "vid-1") is False
    lib.register(_record(), video_file=video)
    assert lib.is_registered("horror_stories", "vid-1") is True


def test_overwrite_is_opt_in(library, media):
    lib, store = library
    video, thumb = media
    lib.register(_record(), video_file=video, thumbnail_file=thumb)
    before = len(store.uploads)
    lib.register(_record(), video_file=video, thumbnail_file=thumb, overwrite=True)
    assert len(store.uploads) > before


# --- channel separation ----------------------------------------------------

def test_channels_share_no_object(library, media):
    lib, store = library
    video, thumb = media
    lib.register(_record(channel_id="horror_stories"), video_file=video,
                 thumbnail_file=thumb)
    lib.register(_record(video_id="vid-2", channel_id="football_news"),
                 video_file=video, thumbnail_file=thumb)

    horror = {n for n in store.objects if n.startswith("videos/horror_stories/")}
    football = {n for n in store.objects if n.startswith("videos/football_news/")}
    assert horror and football
    assert horror.isdisjoint(football)


def test_a_channel_listing_shows_only_its_own(library, media):
    lib, _ = library
    video, thumb = media
    lib.register(_record(channel_id="horror_stories"), video_file=video,
                 thumbnail_file=thumb)
    lib.register(_record(video_id="vid-2", channel_id="football_news"),
                 video_file=video, thumbnail_file=thumb)

    horror = lib.list_channel("horror_stories")
    assert [r["video_id"] for r in horror] == ["vid-1"]
    assert all(r["channel_id"] == "horror_stories" for r in horror)


def test_the_same_video_id_in_two_channels_stays_separate(library, media):
    lib, store = library
    video, _thumb = media
    lib.register(_record(channel_id="horror_stories"), video_file=video)
    lib.register(_record(channel_id="football_news"), video_file=video)
    assert "videos/horror_stories/vid-1/video.mp4" in store.objects
    assert "videos/football_news/vid-1/video.mp4" in store.objects


# --- listing ---------------------------------------------------------------

def test_listing_is_newest_first(library, media):
    lib, _ = library
    video, _thumb = media
    lib.register(_record(video_id="old", created_at="2026-01-01T00:00:00+00:00"),
                 video_file=video)
    lib.register(_record(video_id="new", created_at="2026-09-01T00:00:00+00:00"),
                 video_file=video)
    assert [r["video_id"] for r in lib.list_channel("horror_stories")] == ["new", "old"]


def test_an_empty_channel_lists_nothing(library):
    lib, _ = library
    assert lib.list_channel("horror_stories") == []


def test_re_registering_replaces_rather_than_duplicates_the_entry(library, media):
    lib, _ = library
    video, _thumb = media
    lib.register(_record(), video_file=video)
    lib.register(_record(title="عنوان جديد"), video_file=video, overwrite=True)
    entries = lib.list_channel("horror_stories")
    assert len(entries) == 1
    assert entries[0]["title"] == "عنوان جديد"


# --- safety ----------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../escape", "..", "/", ""])
def test_ids_that_could_escape_the_folder_are_rejected(bad):
    with pytest.raises(ValueError):
        VideoRecord(video_id=bad, job_id="j", channel_id="horror_stories", title="t")


def test_a_missing_video_file_is_an_error(library, tmp_path):
    lib, _ = library
    with pytest.raises(FileNotFoundError):
        lib.register(_record(), video_file=tmp_path / "nope.mp4")


def test_a_missing_thumbnail_is_tolerated(library, media):
    lib, store = library
    video, _thumb = media
    saved = lib.register(_record(), video_file=video,
                         thumbnail_file=Path("does-not-exist.png"))
    assert "videos/horror_stories/vid-1/video.mp4" in store.objects
    assert saved.thumbnail_path == ""


def test_the_backend_is_injected_not_imported():
    """Swapping GCS for a database must not require touching callers."""
    import inspect

    src = inspect.getsource(VideoLibrary)
    assert "import storage" not in src
    assert "self._storage" in src
