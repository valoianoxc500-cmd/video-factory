"""Publishing adapters, against mocked platform APIs.

These check the request shapes that are easy to get subtly wrong and expensive
to discover in production -- YouTube's publishAt only working on private
videos, Instagram's container needing to reach FINISHED before publish, TikTok
being honest that an unaudited app only reaches drafts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from viral.adapters import (
    AdapterContext,
    FacebookAdapter,
    InstagramAdapter,
    TikTokAdapter,
    YouTubeAdapter,
    build_adapters,
    publish_to,
)
from viral.publishing import PublishRequest, PublishStatus

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
USER = "11111111-1111-1111-1111-111111111111"
TOKEN = "ya29.super-secret"


def _request(**over):
    params = dict(user_id=USER, video_id="v1", media_path="vrf/u/v1.mp4",
                  caption="A caption", platforms=("youtube",))
    params.update(over)
    return PublishRequest(**params)


class Recorder:
    """Captures every request so the shapes can be asserted on."""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        self.client = httpx.Client(transport=httpx.MockTransport(wrapped))

    def body(self, index: int) -> dict:
        raw = self.requests[index].content
        try:
            return json.loads(raw)
        except Exception:
            from urllib.parse import parse_qs

            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}


# --- YouTube ---------------------------------------------------------------

def _youtube_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(
            200, headers={"location": "https://upload.googleapis.com/session/1"},
            json={})
    return httpx.Response(200, json={"id": "yt-abc"})


def test_youtube_uploads_and_returns_the_watch_url(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video-bytes")
    rec = Recorder(_youtube_handler)

    result = YouTubeAdapter(client=rec.client).publish(
        _request(), TOKEN, media_file=media, title="My Short")

    assert result.status is PublishStatus.PUBLISHED
    assert result.post_id == "yt-abc"
    assert result.url == "https://www.youtube.com/watch?v=yt-abc"
    assert rec.body(0)["snippet"]["title"] == "My Short"
    assert rec.body(0)["status"]["privacyStatus"] == "public"


def test_a_scheduled_youtube_upload_is_private_until_its_time(tmp_path):
    """publishAt is ignored on a public video, which would post it now."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video-bytes")
    rec = Recorder(_youtube_handler)

    YouTubeAdapter(client=rec.client).publish(
        _request(scheduled_for=NOW + timedelta(days=1)), TOKEN, media_file=media)

    status = rec.body(0)["status"]
    assert status["privacyStatus"] == "private"
    assert status["publishAt"] == "2026-09-07T12:00:00Z"


def test_the_token_is_sent_as_a_bearer_header_not_in_the_url(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video-bytes")
    rec = Recorder(_youtube_handler)

    YouTubeAdapter(client=rec.client).publish(_request(), TOKEN, media_file=media)

    assert rec.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(rec.requests[0].url)


def test_a_missing_local_file_fails_without_retrying():
    result = YouTubeAdapter(client=Recorder(_youtube_handler).client).publish(
        _request(), TOKEN, media_file=None)
    assert result.status is PublishStatus.FAILED
    assert result.retryable is False


def test_a_youtube_quota_error_is_marked_retryable(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")

    def handler(request):
        return httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})

    rec = Recorder(handler)
    result = YouTubeAdapter(client=rec.client).publish(
        _request(), TOKEN, media_file=media)
    assert result.status is PublishStatus.FAILED
    assert result.retryable is True
    assert "Rate limit" in result.error


def test_an_auth_error_is_not_retried(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")

    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})

    result = YouTubeAdapter(client=Recorder(handler).client).publish(
        _request(), TOKEN, media_file=media)
    assert result.retryable is False


def test_an_error_never_echoes_the_token(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")

    def handler(request):
        return httpx.Response(403, json={"error": {"message": "Forbidden"}})

    result = YouTubeAdapter(client=Recorder(handler).client).publish(
        _request(), TOKEN, media_file=media)
    assert TOKEN not in result.error


# --- Facebook --------------------------------------------------------------

def test_facebook_publishes_to_the_page():
    rec = Recorder(lambda r: httpx.Response(200, json={"id": "fb-1"}))
    result = FacebookAdapter(client=rec.client).publish(
        _request(platforms=("facebook",)), TOKEN,
        page_id="page-9", media_url="https://cdn/clip.mp4")

    assert result.status is PublishStatus.PUBLISHED
    assert result.post_id == "fb-1"
    assert "page-9/videos" in str(rec.requests[0].url)
    assert rec.body(0)["file_url"] == "https://cdn/clip.mp4"


def test_a_scheduled_facebook_post_is_unpublished_with_a_timestamp():
    rec = Recorder(lambda r: httpx.Response(200, json={"id": "fb-1"}))
    FacebookAdapter(client=rec.client).publish(
        _request(platforms=("facebook",), scheduled_for=NOW + timedelta(days=1)),
        TOKEN, page_id="page-9", media_url="https://cdn/clip.mp4")

    body = rec.body(0)
    assert body["published"] == "false"
    assert body["scheduled_publish_time"] == str(int((NOW + timedelta(days=1)).timestamp()))


def test_facebook_without_a_page_fails_clearly():
    result = FacebookAdapter(client=Recorder(lambda r: httpx.Response(200)).client
                             ).publish(_request(), TOKEN, media_url="https://cdn/c.mp4")
    assert result.status is PublishStatus.FAILED
    assert "Page" in result.error
    assert result.retryable is False


# --- Instagram -------------------------------------------------------------

def _instagram_handler(states):
    calls = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/media"):
            return httpx.Response(200, json={"id": "container-1"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "ig-post-1"})
        state = states[min(calls["polls"], len(states) - 1)]
        calls["polls"] += 1
        return httpx.Response(200, json={"status_code": state})

    return handler


def test_instagram_waits_for_processing_then_publishes():
    rec = Recorder(_instagram_handler(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]))
    result = InstagramAdapter(client=rec.client, sleep=lambda s: None).publish(
        _request(platforms=("instagram",)), TOKEN,
        ig_user_id="ig-1", media_url="https://cdn/clip.mp4")

    assert result.status is PublishStatus.PUBLISHED
    assert result.post_id == "ig-post-1"
    # container, poll, poll, poll, publish
    assert len(rec.requests) == 5
    assert rec.body(0)["media_type"] == "REELS"


def test_instagram_does_not_publish_a_container_that_errored():
    rec = Recorder(_instagram_handler(["ERROR"]))
    result = InstagramAdapter(client=rec.client, sleep=lambda s: None).publish(
        _request(platforms=("instagram",)), TOKEN,
        ig_user_id="ig-1", media_url="https://cdn/clip.mp4")

    assert result.status is PublishStatus.FAILED
    assert result.retryable is False
    assert not any(r.url.path.endswith("/media_publish") for r in rec.requests)


def test_instagram_gives_up_if_processing_never_finishes():
    rec = Recorder(_instagram_handler(["IN_PROGRESS"]))
    adapter = InstagramAdapter(client=rec.client, sleep=lambda s: None)
    adapter.MAX_POLLS = 3
    result = adapter.publish(
        _request(platforms=("instagram",)), TOKEN,
        ig_user_id="ig-1", media_url="https://cdn/clip.mp4")

    assert result.status is PublishStatus.FAILED
    assert result.retryable is True


def test_instagram_needs_a_public_media_url():
    result = InstagramAdapter(client=Recorder(lambda r: httpx.Response(200)).client
                              ).publish(_request(), TOKEN, ig_user_id="ig-1")
    assert result.status is PublishStatus.FAILED
    assert result.retryable is False


# --- TikTok ----------------------------------------------------------------

def test_an_unaudited_tiktok_app_posts_to_drafts_and_says_so():
    rec = Recorder(lambda r: httpx.Response(
        200, json={"data": {"publish_id": "tt-1"}, "error": {"code": "ok"}}))
    result = TikTokAdapter(client=rec.client, direct_post_approved=False).publish(
        _request(platforms=("tiktok",)), TOKEN, media_url="https://cdn/clip.mp4")

    assert result.status is PublishStatus.PUBLISHED
    assert "drafts" in result.error.lower()
    assert rec.requests[0].url.path.endswith("/inbox/video/init/")


def test_an_audited_tiktok_app_posts_directly():
    rec = Recorder(lambda r: httpx.Response(
        200, json={"data": {"publish_id": "tt-2"}, "error": {"code": "ok"}}))
    result = TikTokAdapter(client=rec.client, direct_post_approved=True).publish(
        _request(platforms=("tiktok",)), TOKEN, media_url="https://cdn/clip.mp4")

    assert result.post_id == "tt-2"
    assert result.error == ""
    assert rec.requests[0].url.path.endswith("/video/init/")
    assert rec.body(0)["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"


def test_a_tiktok_error_in_a_200_body_is_still_a_failure():
    """TikTok returns 200 with an error object; treating it as success would
    mark an unpublished video as published."""
    rec = Recorder(lambda r: httpx.Response(200, json={
        "error": {"code": "spam_risk_too_many_posts",
                  "message": "Daily post limit reached"}}))
    result = TikTokAdapter(client=rec.client).publish(
        _request(platforms=("tiktok",)), TOKEN, media_url="https://cdn/clip.mp4")

    assert result.status is PublishStatus.FAILED
    assert "spam_risk" in result.error


# --- dispatch --------------------------------------------------------------

def test_snapchat_dispatches_to_the_unsupported_adapter():
    result = publish_to(
        "snapchat", _request(platforms=("snapchat",)), TOKEN,
        AdapterContext(), adapters=build_adapters())
    assert result.status is PublishStatus.UNSUPPORTED


def test_an_unknown_platform_is_unsupported_not_a_crash():
    result = publish_to("myspace", _request(), TOKEN, AdapterContext(),
                        adapters=build_adapters())
    assert result.status is PublishStatus.UNSUPPORTED


def test_dispatch_passes_each_adapter_what_it_needs(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    rec = Recorder(_youtube_handler)
    adapters = {"youtube": YouTubeAdapter(client=rec.client)}

    result = publish_to(
        "youtube", _request(), TOKEN,
        AdapterContext(media_file=media, title="Dispatched"),
        adapters=adapters)

    assert result.status is PublishStatus.PUBLISHED
    assert rec.body(0)["snippet"]["title"] == "Dispatched"
