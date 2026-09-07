"""The worker loop: task dispatch, and per-job account isolation.

The isolation tests here are the ones that matter. The worker serves every
user, so an ambient or mixed-up user id is the bug that posts one person's
video to another person's account.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from viral.accounts import TokenCipher, generate_token_key
from viral.publishing import MAX_ATTEMPTS, PublishResult, PublishStatus
from viral.runner import (
    WorkerClient,
    _token_for,
    handle_task,
    poll_once,
    run_discover,
    run_publish_job,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def cipher():
    return TokenCipher(generate_token_key())


class FakeApi:
    """Stands in for the worker endpoint, recording every call."""

    def __init__(self, cipher: TokenCipher):
        self.cipher = cipher
        self.calls: list[tuple[str, dict]] = []
        self.accounts: dict[tuple[str, str], dict] = {}
        self.updates: list[dict] = []
        self.completions: list[dict] = []
        self.tasks: list[dict] = []
        self.jobs: list[dict] = []

    def connect(self, user_id: str, platform: str, token: str, *, expires_in=3600):
        self.accounts[(user_id, platform)] = {
            "id": f"acct-{user_id[:4]}-{platform}",
            "account_ref": f"ref-{user_id[:4]}",
            "account_handle": f"@{user_id[:4]}",
            "access_token_encrypted": self.cipher.encrypt(
                token, user_id=user_id, platform=platform),
            "refresh_token_encrypted": "",
            # Anchored to the real clock: _token_for compares against
            # datetime.now, so a fixed date would go stale on its own.
            "token_expires_at": (
                (datetime.now(timezone.utc)
                 + timedelta(seconds=expires_in)).isoformat()
                if expires_in else None
            ),
        }

    def call(self, action: str, **params) -> dict:
        self.calls.append((action, params))
        if action == "account":
            return {
                "account": self.accounts.get(
                    (params.get("user_id"), params.get("platform")))
            }
        if action == "update_publish_job":
            self.updates.append(params)
            return {"ok": True}
        if action == "complete_task":
            self.completions.append(params)
            return {"ok": True}
        if action == "claim_task":
            return {"task": self.tasks.pop(0) if self.tasks else None}
        if action == "claim_publish_job":
            return {"job": self.jobs.pop(0) if self.jobs else None}
        return {"ok": True}


def _client(api: FakeApi) -> WorkerClient:
    client = WorkerClient.__new__(WorkerClient)
    client.call = api.call            # type: ignore[method-assign]
    return client


def _job(user_id=ALICE, platform="youtube", **over):
    job = {
        "id": f"job-{user_id[:4]}-{platform}",
        "user_id": user_id,
        "asset_id": "asset-1",
        "platform": platform,
        "caption": "A caption",
        "mode": "publish_now",
        "scheduled_for": None,
        "attempts": 0,
        "processed_path": "https://cdn/clip.mp4",
    }
    job.update(over)
    return job


# --- discovery -------------------------------------------------------------

def test_discovery_reports_every_provider_even_when_none_are_usable(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    result = run_discover({"niche": "cars"})

    platforms = {p["platform"] for p in result["providers"]}
    assert platforms == {"youtube", "tiktok", "instagram", "facebook", "snapchat"}
    assert result["videos"] == []
    assert "YOUTUBE_API_KEY" in result["note"]


def test_discovery_never_claims_a_platform_it_cannot_search(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    statuses = {
        p["platform"]: p["availability"] for p in run_discover({"niche": "cars"})["providers"]
    }
    assert statuses["snapchat"] == "no_public_api"
    assert statuses["tiktok"] == "restricted_api"


# --- tokens ----------------------------------------------------------------

def test_each_user_gets_their_own_token(cipher):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    api.connect(BOB, "youtube", "bob-token")
    client = _client(api)

    assert _token_for(client, cipher, ALICE, "youtube") == "alice-token"
    assert _token_for(client, cipher, BOB, "youtube") == "bob-token"


def test_a_token_row_belonging_to_someone_else_will_not_decrypt(cipher):
    """The proof that a mixed-up row cannot post to a stranger's account."""
    from viral.accounts import TokenSecurityError

    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    # Alice's row served for Bob, as a swap or a bug would produce.
    api.accounts[(BOB, "youtube")] = api.accounts[(ALICE, "youtube")]

    with pytest.raises(TokenSecurityError):
        _token_for(_client(api), cipher, BOB, "youtube")


def test_no_connected_account_is_a_clear_failure(cipher):
    with pytest.raises(RuntimeError, match="No connected"):
        _token_for(_client(FakeApi(cipher)), cipher, ALICE, "youtube")


def test_an_expired_token_with_no_refresh_asks_for_a_reconnect(cipher):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "stale", expires_in=None)
    # Past relative to the real clock, which is what _token_for compares to.
    api.accounts[(ALICE, "youtube")]["token_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    with pytest.raises(RuntimeError, match="reconnect"):
        _token_for(_client(api), cipher, ALICE, "youtube")


# --- publishing ------------------------------------------------------------

def _publish_with(api, cipher, monkeypatch, result: PublishResult, job=None):
    seen: dict = {}

    def fake_publish(platform, request, token, context, adapters=None):
        seen["platform"] = platform
        seen["token"] = token
        seen["user_id"] = request.user_id
        seen["scheduled_for"] = request.scheduled_for
        seen["page_id"] = context.page_id
        return result

    monkeypatch.setattr("viral.runner.publish_to", fake_publish)
    run_publish_job(_client(api), cipher, job or _job())
    return seen


def test_a_job_is_published_with_its_own_owners_token(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    seen = _publish_with(api, cipher, monkeypatch, PublishResult(
        "youtube", PublishStatus.PUBLISHED, post_id="p1", url="https://y/p1"))

    assert seen["token"] == "alice-token"
    assert seen["user_id"] == ALICE
    assert api.updates[0]["status"] == "published"
    assert api.updates[0]["post_url"] == "https://y/p1"


def test_a_transient_failure_is_rescheduled(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    _publish_with(api, cipher, monkeypatch, PublishResult(
        "youtube", PublishStatus.FAILED, error="502", retryable=True))

    update = api.updates[0]
    assert update["status"] == "queued"
    assert update["next_attempt_at"] is not None
    assert update["attempts"] == 1


def test_a_permanent_failure_is_not_retried(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    _publish_with(api, cipher, monkeypatch, PublishResult(
        "youtube", PublishStatus.FAILED, error="Invalid credentials",
        retryable=False))

    update = api.updates[0]
    assert update["status"] == "failed"
    assert update["next_attempt_at"] is None


def test_retries_stop_at_the_limit(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    _publish_with(
        api, cipher, monkeypatch,
        PublishResult("youtube", PublishStatus.FAILED, error="502", retryable=True),
        job=_job(attempts=MAX_ATTEMPTS - 1),
    )
    update = api.updates[0]
    assert update["status"] == "failed"
    assert "gave up" in update["error"]


def test_a_missing_account_fails_the_job_without_retrying(cipher, monkeypatch):
    api = FakeApi(cipher)          # nothing connected
    _publish_with(api, cipher, monkeypatch, PublishResult(
        "youtube", PublishStatus.PUBLISHED))

    update = api.updates[0]
    assert update["status"] == "failed"
    assert update["next_attempt_at"] is None
    assert "No connected" in update["error"]


def test_an_unsupported_platform_is_recorded_as_unsupported(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "snapchat", "token")
    _publish_with(
        api, cipher, monkeypatch,
        PublishResult("snapchat", PublishStatus.UNSUPPORTED,
                      error="No server-side API.", retryable=False),
        job=_job(platform="snapchat"),
    )
    assert api.updates[0]["status"] == "unsupported"


def test_a_natively_scheduled_job_passes_the_time_to_the_platform(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    when = (NOW + timedelta(days=1)).isoformat()
    seen = _publish_with(
        api, cipher, monkeypatch,
        PublishResult("youtube", PublishStatus.PUBLISHED),
        job=_job(mode="native_schedule", scheduled_for=when),
    )
    assert seen["scheduled_for"] is not None


def test_a_queued_job_does_not_pass_a_time_to_the_platform(cipher, monkeypatch):
    """It is due now; sending the original time would schedule it in the past."""
    api = FakeApi(cipher)
    api.connect(ALICE, "instagram", "alice-token")
    seen = _publish_with(
        api, cipher, monkeypatch,
        PublishResult("instagram", PublishStatus.PUBLISHED),
        job=_job(platform="instagram", mode="queue_until_due",
                 scheduled_for=NOW.isoformat()),
    )
    assert seen["scheduled_for"] is None


def test_the_accounts_own_ref_is_passed_to_the_adapter(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "facebook", "alice-token")
    seen = _publish_with(
        api, cipher, monkeypatch,
        PublishResult("facebook", PublishStatus.PUBLISHED),
        job=_job(platform="facebook"),
    )
    assert seen["page_id"] == "ref-aaaa"


# --- task dispatch ---------------------------------------------------------

def test_an_unknown_task_kind_fails_the_task_rather_than_the_worker(cipher):
    api = FakeApi(cipher)
    handle_task(_client(api), cipher, {"id": "t1", "kind": "mystery", "payload": {}})
    assert api.completions[0]["status"] == "failed"
    assert "mystery" in api.completions[0]["error"]


def test_a_failing_task_is_marked_failed_with_its_reason(cipher, monkeypatch):
    api = FakeApi(cipher)
    monkeypatch.setattr(
        "viral.runner.run_discover",
        lambda payload: (_ for _ in ()).throw(RuntimeError("quota exhausted")))
    handle_task(_client(api), cipher, {"id": "t1", "kind": "discover", "payload": {}})
    assert api.completions[0]["status"] == "failed"
    assert "quota exhausted" in api.completions[0]["error"]


def test_a_successful_task_stores_its_result(cipher, monkeypatch):
    api = FakeApi(cipher)
    monkeypatch.setattr("viral.runner.run_discover", lambda payload: {"videos": []})
    handle_task(_client(api), cipher, {"id": "t1", "kind": "discover", "payload": {}})
    assert api.completions[0]["status"] == "done"
    assert api.completions[0]["result"] == {"videos": []}


def test_an_idle_poll_does_nothing_and_says_so(cipher):
    api = FakeApi(cipher)
    assert poll_once(_client(api), cipher) is False
    assert [a for a, _ in api.calls] == ["claim_task", "claim_publish_job"]


def test_a_poll_handles_a_task_and_a_job_in_one_pass(cipher, monkeypatch):
    api = FakeApi(cipher)
    api.connect(ALICE, "youtube", "alice-token")
    api.tasks.append({"id": "t1", "kind": "discover", "payload": {}})
    api.jobs.append(_job())
    monkeypatch.setattr("viral.runner.run_discover", lambda payload: {"videos": []})
    monkeypatch.setattr(
        "viral.runner.publish_to",
        lambda *a, **k: PublishResult("youtube", PublishStatus.PUBLISHED))

    assert poll_once(_client(api), cipher) is True
    assert api.completions and api.updates
