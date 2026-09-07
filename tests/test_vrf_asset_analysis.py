"""Analysing a video the user added, and refusing to process one with no file.

Adding a video queues an `analyse` task carrying an `asset_id`. The worker
only understood `source_id`, so every one of those tasks failed with "That
saved video no longer exists" -- a message about a record the user never
touched. These cover the asset path, and the honest failures around it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from viral import runner
from viral.runner import _analyse_asset, public_metrics, run_analyse, run_process

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class FakeApi:
    """Records what the worker asks the deployment for, and what it stores."""

    def __init__(self, asset: dict | None = None, source: dict | None = None):
        self.asset = asset
        self.source = source
        self.saved: dict = {}
        self.calls: list[tuple[str, dict]] = []

    def call(self, action: str, **params) -> dict:
        self.calls.append((action, params))
        if action == "asset":
            return {"asset": self.asset}
        if action == "source":
            return {"source": self.source}
        if action in {"save_asset_analysis", "save_analysis", "update_asset_ingest"}:
            self.saved = params
            return {"ok": True}
        return {"ok": True}


def _client(api: FakeApi):
    client = runner.WorkerClient.__new__(runner.WorkerClient)
    client.call = api.call            # type: ignore[method-assign]
    return client


def _asset(**over) -> dict:
    asset = {
        "id": "asset-1",
        "user_id": ALICE,
        "title": "My clip",
        "storage_path": "",
        "processed_path": "",
        "rights_source": "owned_or_permitted",
        "rights_holder": "",
        "rights_evidence": "",
        "source_platform": "youtube",
        "source_video_id": "vid1",
        "source_author": "Dashcam Daily",
        "ingest_status": "pending",
        "ingest_detail": "",
    }
    asset.update(over)
    return asset


@pytest.fixture
def no_model(monkeypatch):
    """The analysis model is not the subject here; the plumbing is."""
    async def fake_analyse(video, **kwargs):
        from viral.analysis import AnalysisDepth, parse_analysis

        return parse_analysis(
            {"hook": "cold open", "why_it_performs": "it starts at the peak",
             "hook_strength": 0.8, "retention_potential": 0.7},
            AnalysisDepth.METADATA,
        )

    monkeypatch.setattr(runner, "analyse", fake_analyse)


# --- routing between the two shapes ----------------------------------------

def test_an_asset_task_is_not_treated_as_a_saved_discovery(no_model, monkeypatch):
    """The bug: asset_id fell through to the source path and always failed."""
    monkeypatch.setattr(runner, "public_metrics", lambda p, v: None)
    api = FakeApi(asset=_asset())

    result = run_analyse(_client(api), {"asset_id": "asset-1"})

    assert result["analysed"] is True
    assert [action for action, _ in api.calls][0] == "asset"
    assert "source" not in [action for action, _ in api.calls]


def test_a_saved_discovery_still_uses_the_source_path(no_model):
    api = FakeApi(source={
        "user_id": ALICE, "platform": "youtube", "title": "Found it",
        "author": "Someone", "views": 100, "likes": 5,
    })

    run_analyse(_client(api), {"source_id": "src-1"})

    assert [action for action, _ in api.calls][0] == "source"


def test_a_deleted_asset_says_so_about_the_asset(no_model):
    api = FakeApi(asset=None)
    with pytest.raises(RuntimeError, match="video no longer exists"):
        run_analyse(_client(api), {"asset_id": "gone"})


# --- scoring, only where the numbers are real ------------------------------

def test_an_asset_is_scored_from_the_sources_public_metrics(no_model, monkeypatch):
    from viral.scoring import VideoMetrics

    monkeypatch.setattr(runner, "public_metrics", lambda p, v: VideoMetrics(
        views=2_400_000, likes=180_000, comments=9_000, followers=50_000,
        posted_at=datetime.now(timezone.utc) - timedelta(days=3),
        duration_seconds=41.0, platform="youtube",
    ))
    api = FakeApi(asset=_asset())

    result = _analyse_asset(_client(api), "asset-1")

    assert result["scored"] is True
    assert api.saved["original_viral_score"] > 0
    assert api.saved["new_version_probability"] > 0
    assert api.saved["probability_confidence"] in {"low", "medium", "high"}
    assert api.saved["duration_seconds"] == 41.0


def test_a_platform_that_reports_nothing_leaves_the_score_absent(no_model, monkeypatch):
    """An invented score would read as a measurement of the original."""
    monkeypatch.setattr(runner, "public_metrics", lambda p, v: None)
    api = FakeApi(asset=_asset(source_platform="tiktok"))

    result = _analyse_asset(_client(api), "asset-1")

    assert result["scored"] is False
    assert api.saved["original_viral_score"] is None
    assert api.saved["new_version_probability"] is None
    assert api.saved["probability_confidence"] == ""


def test_the_analysis_is_stored_even_without_a_score(no_model, monkeypatch):
    monkeypatch.setattr(runner, "public_metrics", lambda p, v: None)
    api = FakeApi(asset=_asset(source_platform="instagram"))

    _analyse_asset(_client(api), "asset-1")

    assert api.saved["score_breakdown"]["analysis"]["hook"] == "cold open"
    assert api.saved["score_breakdown"]["original"] is None


def test_analysis_never_asks_for_the_media_file(no_model, monkeypatch):
    """Depth follows the rights basis; a discovered original is never copied."""
    monkeypatch.setattr(runner, "public_metrics", lambda p, v: None)
    api = FakeApi(asset=_asset())

    result = _analyse_asset(_client(api), "asset-1")

    assert result["depth"] == "metadata"


# --- public metrics --------------------------------------------------------

def test_only_youtube_has_a_public_metrics_endpoint(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "key")
    for platform in ("tiktok", "instagram", "facebook", ""):
        assert public_metrics(platform, "vid1") is None


def test_without_a_key_metrics_are_unavailable_rather_than_zero(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert public_metrics("youtube", "vid1") is None


def test_youtube_metrics_are_read(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": [{
                "snippet": {"channelId": "ch1", "publishedAt": "2026-09-01T00:00:00Z"},
                "statistics": {"viewCount": "1000", "likeCount": "80",
                               "commentCount": "9"},
                "contentDetails": {"duration": "PT41S"},
            }]})
        return httpx.Response(200, json={"items": [
            {"statistics": {"subscriberCount": "5000"}}]})

    # Captured before patching: the replacement is the same attribute the
    # lambda would otherwise call back into.
    real_client = httpx.Client
    monkeypatch.setattr(
        runner.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))

    metrics = public_metrics("youtube", "vid1")
    assert metrics.views == 1000
    assert metrics.followers == 5000
    assert metrics.duration_seconds == 41.0
    # No share count on this endpoint; absent rather than zero.
    assert metrics.shares is None


def test_a_failing_metrics_call_is_not_fatal(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "key")
    real_client = httpx.Client
    monkeypatch.setattr(
        runner.httpx, "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))))

    assert public_metrics("youtube", "vid1") is None


# --- processing without a file ---------------------------------------------

def test_a_metadata_only_asset_explains_why_it_cannot_be_processed():
    api = FakeApi(asset=_asset(
        ingest_status="metadata_only",
        ingest_detail="YouTube's API never returns the video file.",
    ))
    with pytest.raises(RuntimeError, match="never returns the video file"):
        run_process(_client(api), {"asset_id": "asset-1"})


def test_an_asset_still_waiting_on_its_import_says_so():
    api = FakeApi(asset=_asset(ingest_status="pending"))
    with pytest.raises(RuntimeError, match="not been imported yet"):
        run_process(_client(api), {"asset_id": "asset-1"})


def test_a_failed_import_is_not_reported_as_still_waiting():
    api = FakeApi(asset=_asset(ingest_status="failed"))
    with pytest.raises(RuntimeError, match="no source file"):
        run_process(_client(api), {"asset_id": "asset-1"})
