"""Importing the owner's own file, and refusing to pretend when it cannot.

Assets were created `pending` with nothing able to move them: the process task
expected a file that only an import could have put there, so every added video
sat on "Queued" forever. This covers the step that fetches it, and â€” more
importantly â€” every way it can legitimately fail.
"""

from __future__ import annotations

import httpx
import pytest

from viral import runner
from viral.accounts import TokenCipher, generate_token_key
from viral.runner import owner_media_url, run_ingest

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture
def cipher():
    return TokenCipher(generate_token_key())


class FakeApi:
    def __init__(self, cipher: TokenCipher, asset: dict):
        self.cipher = cipher
        self.asset = asset
        self.ingest: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.accounts: dict[tuple[str, str], dict] = {}

    def connect(self, user_id: str, platform: str, token: str = "tok"):
        self.accounts[(user_id, platform)] = {
            "id": f"acct-{platform}",
            "account_ref": "ref-1",
            "account_handle": "@owner",
            "access_token_encrypted": self.cipher.encrypt(
                token, user_id=user_id, platform=platform),
            "refresh_token_encrypted": "",
            "token_expires_at": None,
        }

    def call(self, action: str, **params) -> dict:
        self.calls.append((action, params))
        if action == "asset":
            return {"asset": self.asset}
        if action == "account":
            return {
                "account": self.accounts.get(
                    (params.get("user_id"), params.get("platform")))
            }
        if action == "update_asset_ingest":
            self.ingest.append(params)
            # Keep the asset in step, so a later process call sees the file.
            self.asset = {**self.asset, "ingest_status": params["status"]}
            if params.get("storage_path"):
                self.asset["storage_path"] = params["storage_path"]
            return {"ok": True}
        return {"ok": True}

    @property
    def statuses(self) -> list[str]:
        return [entry["status"] for entry in self.ingest]


def _client(api: FakeApi):
    client = runner.WorkerClient.__new__(runner.WorkerClient)
    client.call = api.call            # type: ignore[method-assign]
    return client


def _asset(**over) -> dict:
    asset = {
        "id": "asset-1",
        "user_id": ALICE,
        "title": "My reel",
        "storage_path": "",
        "processed_path": "",
        "rights_source": "owned_or_permitted",
        "rights_holder": "",
        "rights_evidence": "",
        "source_platform": "instagram",
        "source_video_id": "media-1",
        "ingest_status": "pending",
        "ingest_detail": "",
    }
    asset.update(over)
    return asset


# --- resolving the owner's media URL ---------------------------------------

def test_instagram_returns_the_media_url():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"media_url": "https://cdn/reel.mp4"})))
    assert owner_media_url("instagram", "media-1", "tok", client=client) == (
        "https://cdn/reel.mp4")


def test_facebook_returns_the_source_url():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"source": "https://cdn/page.mp4"})))
    assert owner_media_url("facebook", "vid-1", "tok", client=client) == (
        "https://cdn/page.mp4")


def test_instagram_omitting_the_url_is_an_empty_answer_not_an_error():
    """Instagram omits media_url when the media carries copyrighted audio."""
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"media_type": "VIDEO"})))
    assert owner_media_url("instagram", "media-1", "tok", client=client) == ""


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "", "myspace"])
def test_platforms_with_no_file_endpoint_are_never_asked(platform):
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError(f"called an API for {platform}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert owner_media_url(platform, "vid-1", "tok", client=client) == ""


def test_a_platform_error_is_raised_with_its_reason():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"message": "bad token"}})))
    with pytest.raises(RuntimeError, match="did not return the file"):
        owner_media_url("instagram", "media-1", "tok", client=client)


def test_the_token_is_never_put_in_the_path():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"media_url": "https://cdn/x.mp4"})

    owner_media_url("instagram", "media-1", "sekret",
                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "/media-1" in seen["url"]
    assert "sekret" not in seen["url"].split("?")[0]


# --- the step ---------------------------------------------------------------

def test_a_successful_import_moves_through_fetching_to_imported(cipher, monkeypatch):
    api = FakeApi(cipher, _asset())
    api.connect(ALICE, "instagram")
    monkeypatch.setattr(runner, "owner_media_url",
                        lambda *a, **k: "https://cdn/reel.mp4")
    monkeypatch.setattr(runner, "_download", lambda url, dest: dest.write_bytes(b"v"))
    monkeypatch.setattr(runner, "_upload_media", lambda *a, **k: "https://store/source.mp4")
    monkeypatch.setattr(runner, "run_process", lambda client, payload: {"processed": True})

    result = run_ingest(_client(api), cipher, {"asset_id": "asset-1"})

    assert result["imported"] is True
    assert api.statuses == ["fetching", "imported"]
    assert api.ingest[-1]["storage_path"] == "https://store/source.mp4"


def test_the_new_version_is_made_in_the_same_task(cipher, monkeypatch):
    """Queued separately, the processing would wait on a file a failed import
    never produced."""
    api = FakeApi(cipher, _asset())
    api.connect(ALICE, "instagram")
    monkeypatch.setattr(runner, "owner_media_url", lambda *a, **k: "https://cdn/r.mp4")
    monkeypatch.setattr(runner, "_download", lambda url, dest: dest.write_bytes(b"v"))
    monkeypatch.setattr(runner, "_upload_media", lambda *a, **k: "https://store/source.mp4")
    processed: list[dict] = []

    def fake_process(client, payload):
        processed.append(payload)
        return {"processed": True}

    monkeypatch.setattr(runner, "run_process", fake_process)

    run_ingest(_client(api), cipher, {"asset_id": "asset-1", "platform": "youtube"})

    assert processed == [{"asset_id": "asset-1", "platform": "youtube"}]


def test_a_video_the_platform_will_not_release_becomes_metadata_only(cipher, monkeypatch):
    """Not `failed`: nothing the user does to the connection will change it."""
    api = FakeApi(cipher, _asset())
    api.connect(ALICE, "instagram")
    monkeypatch.setattr(runner, "owner_media_url", lambda *a, **k: "")

    result = run_ingest(_client(api), cipher, {"asset_id": "asset-1"})

    assert result["imported"] is False
    assert api.statuses[-1] == "metadata_only"
    assert "copyrighted content" in api.ingest[-1]["detail"]


def test_no_connected_account_fails_with_that_reason(cipher):
    api = FakeApi(cipher, _asset())          # nothing connected

    result = run_ingest(_client(api), cipher, {"asset_id": "asset-1"})

    assert result["imported"] is False
    assert api.statuses[-1] == "failed"
    assert "No connected" in api.ingest[-1]["detail"]


def test_a_platform_error_is_recorded_rather_than_raised(cipher, monkeypatch):
    api = FakeApi(cipher, _asset())
    api.connect(ALICE, "instagram")
    monkeypatch.setattr(
        runner, "owner_media_url",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("instagram said no")))

    result = run_ingest(_client(api), cipher, {"asset_id": "asset-1"})

    assert result["imported"] is False
    assert "instagram said no" in api.ingest[-1]["detail"]


def test_a_failed_download_does_not_leave_the_asset_fetching(cipher, monkeypatch):
    api = FakeApi(cipher, _asset())
    api.connect(ALICE, "instagram")
    monkeypatch.setattr(runner, "owner_media_url", lambda *a, **k: "https://cdn/r.mp4")
    monkeypatch.setattr(
        runner, "_download",
        lambda url, dest: (_ for _ in ()).throw(RuntimeError("connection reset")))

    result = run_ingest(_client(api), cipher, {"asset_id": "asset-1"})

    assert result["imported"] is False
    assert api.statuses[-1] == "failed"
    assert "could not be downloaded" in api.ingest[-1]["detail"]


def test_a_worker_without_the_key_says_so_instead_of_failing_obscurely(cipher):
    api = FakeApi(cipher, _asset())

    result = run_ingest(_client(api), None, {"asset_id": "asset-1"})

    assert result["imported"] is False
    assert "Token encryption is not configured" in api.ingest[-1]["detail"]


def test_a_deleted_asset_stops_the_task(cipher):
    api = FakeApi(cipher, _asset())
    api.asset = None
    with pytest.raises(RuntimeError, match="no longer exists"):
        run_ingest(_client(api), cipher, {"asset_id": "gone"})


def test_the_ingest_kind_is_dispatched(cipher, monkeypatch):
    api = FakeApi(cipher, _asset())
    called: list[str] = []
    monkeypatch.setattr(
        runner, "run_ingest",
        lambda client, c, payload: called.append("ingest") or {"imported": True})

    runner.handle_task(_client(api), cipher,
                       {"id": "t1", "kind": "ingest", "payload": {"asset_id": "a"}})

    assert called == ["ingest"]
