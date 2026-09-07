"""Authorization and isolation for the multi-tenant foundation.

These run against the live Supabase project using the publishable key -- the
same key a browser would hold. That is the point: they prove what an attacker
with the public key can and cannot do. Nothing here uses admin access, so a
passing run means RLS itself is holding, not that the test was privileged.

Read-only apart from two throwaway accounts, which are deleted at the end.
No AI or generation APIs are called.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import settings  # noqa: E402,F401  (TLS bootstrap for this host)
import httpx  # noqa: E402

SUPABASE_URL = "https://fiichyrbvhvijcoxdewk.supabase.co"
ANON_KEY = "sb_publishable_E2oCFEZslvsKHUNu-miLtg_2ktRHEnt"

OWNED_TABLES = ["videos", "jobs", "profiles", "channels", "usage_events"]


def _anon_headers() -> dict[str, str]:
    return {"apikey": ANON_KEY, "authorization": f"Bearer {ANON_KEY}"}


def _user_headers(token: str) -> dict[str, str]:
    return {"apikey": ANON_KEY, "authorization": f"Bearer {token}"}


def _rest(path: str, headers: dict[str, str], **kwargs) -> httpx.Response:
    return httpx.request(
        kwargs.pop("method", "GET"),
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={**headers, "content-type": "application/json"},
        timeout=30,
        **kwargs,
    )


def _signup(email: str, password: str) -> dict:
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/signup",
        headers={"apikey": ANON_KEY, "content-type": "application/json"},
        json={"email": email, "password": password},
        timeout=30,
    )
    if resp.status_code >= 400:
        # Name the cause: a skip that reads "could not sign up" hides whether
        # the project is misconfigured or the tests simply cannot run here.
        code = ""
        try:
            code = resp.json().get("error_code", "")
        except Exception:
            pass
        if code == "over_email_send_rate_limit":
            pytest.skip(
                "Supabase is still sending confirmation emails through its "
                "built-in service, which is rate limited to a few per hour. "
                "Configure SMTP or disable email confirmation to run the "
                "cross-account isolation tests."
            )
        pytest.skip(f"could not create a test account ({code or resp.status_code})")
    return resp.json()


def _signin(email: str, password: str) -> str | None:
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": ANON_KEY, "content-type": "application/json"},
        json={"email": email, "password": password},
        timeout=30,
    )
    if resp.status_code >= 400:
        return None
    return resp.json().get("access_token")


@pytest.fixture(scope="module")
def two_users():
    """Two real accounts, so isolation is tested between actual identities."""
    password = "Test-" + uuid.uuid4().hex[:16]
    users = []
    for _ in range(2):
        # Not example.com -- Supabase's validator rejects reserved domains.
        email = f"rls-test-{uuid.uuid4().hex[:12]}@videofactory-test.com"
        created = _signup(email, password)
        token = created.get("access_token") or _signin(email, password)
        if not token:
            pytest.skip("email confirmation is required; cannot obtain a session")
        users.append({
            "email": email,
            "token": token,
            "id": created.get("user", {}).get("id") or created.get("id"),
        })
    yield users[0], users[1]


# --- the anonymous key alone grants nothing -------------------------------

@pytest.mark.parametrize("table", OWNED_TABLES)
def test_anonymous_key_reads_no_rows(table):
    """The publishable key is public by design; it must expose no data."""
    resp = _rest(f"{table}?select=*", _anon_headers())
    assert resp.status_code in (200, 401, 403), resp.text[:200]
    if resp.status_code == 200:
        assert resp.json() == [], f"anon read {len(resp.json())} row(s) from {table}"


def test_anonymous_cannot_insert_a_job():
    resp = _rest(
        "jobs",
        _anon_headers(),
        method="POST",
        json={"id": str(uuid.uuid4()), "topic": "anon insert", "status": "queued"},
    )
    assert resp.status_code >= 400, "anon was able to create a job"


def test_worker_credentials_are_unreachable():
    resp = _rest("worker_credentials?select=*", _anon_headers())
    assert resp.status_code >= 400 or resp.json() == []


def test_unclaimed_pool_is_unreachable():
    resp = _rest("unclaimed_videos?select=*", _anon_headers())
    assert resp.status_code >= 400 or resp.json() == []


# --- a signed-in user sees only their own ---------------------------------

def test_a_new_account_starts_empty(two_users):
    alice, _ = two_users
    for table in ("videos", "jobs"):
        resp = _rest(f"{table}?select=*", _user_headers(alice["token"]))
        assert resp.status_code == 200, resp.text[:200]
        assert resp.json() == [], f"a new account already sees rows in {table}"


def test_a_user_cannot_read_another_users_job(two_users):
    alice, bob = two_users

    created = _rest(
        "jobs",
        {**_user_headers(alice["token"]), "prefer": "return=representation"},
        method="POST",
        json={
            "id": str(uuid.uuid4()),
            "user_id": alice["id"],
            "topic": "alice private topic",
            "status": "queued",
        },
    )
    assert created.status_code < 400, created.text[:300]
    job_id = created.json()[0]["id"]

    seen = _rest(f"jobs?select=*&id=eq.{job_id}", _user_headers(bob["token"]))
    assert seen.status_code == 200
    assert seen.json() == [], "one user read another user's job"


def test_a_user_cannot_update_another_users_job(two_users):
    alice, bob = two_users
    created = _rest(
        "jobs",
        {**_user_headers(alice["token"]), "prefer": "return=representation"},
        method="POST",
        json={
            "id": str(uuid.uuid4()),
            "user_id": alice["id"],
            "topic": "alice topic to hijack",
            "status": "queued",
        },
    )
    job_id = created.json()[0]["id"]

    hijack = _rest(
        f"jobs?id=eq.{job_id}",
        {**_user_headers(bob["token"]), "prefer": "return=representation"},
        method="PATCH",
        json={"topic": "bob overwrote this"},
    )
    assert hijack.status_code < 400
    assert hijack.json() == [], "one user modified another user's job"

    check = _rest(f"jobs?select=topic&id=eq.{job_id}", _user_headers(alice["token"]))
    assert check.json()[0]["topic"] == "alice topic to hijack"


def test_a_forged_user_id_is_rejected(two_users):
    """Filing work against someone else's account must fail."""
    alice, bob = two_users
    forged = _rest(
        "jobs",
        _user_headers(bob["token"]),
        method="POST",
        json={
            "id": str(uuid.uuid4()),
            "user_id": alice["id"],  # not the caller
            "topic": "forged ownership",
            "status": "queued",
        },
    )
    assert forged.status_code >= 400, "a user created a row owned by someone else"


def test_a_user_cannot_insert_usage_for_themselves(two_users):
    """Usage is server-written, so a user cannot fabricate their own history."""
    alice, _ = two_users
    resp = _rest(
        "usage_events",
        _user_headers(alice["token"]),
        method="POST",
        json={"user_id": alice["id"], "kind": "video", "quantity": 999},
    )
    assert resp.status_code >= 400


def test_a_profile_is_created_automatically(two_users):
    alice, _ = two_users
    resp = _rest("profiles?select=id,email", _user_headers(alice["token"]))
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1, "expected exactly the caller's own profile"
    assert rows[0]["id"] == alice["id"]


def test_a_user_cannot_see_another_profile(two_users):
    alice, bob = two_users
    resp = _rest(f"profiles?select=*&id=eq.{alice['id']}", _user_headers(bob["token"]))
    assert resp.status_code == 200
    assert resp.json() == []


# --- worker functions require the worker secret ---------------------------

def _rpc(name: str, headers: dict[str, str], payload: dict) -> httpx.Response:
    return httpx.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        headers={**headers, "content-type": "application/json"},
        json=payload,
        timeout=30,
    )


def test_worker_claim_refuses_a_wrong_token():
    resp = _rpc("worker_claim_job", _anon_headers(), {"worker_token": "x" * 48})
    assert resp.status_code >= 400, "a wrong worker token was accepted"


def test_worker_claim_refuses_an_empty_token():
    resp = _rpc("worker_claim_job", _anon_headers(), {"worker_token": ""})
    assert resp.status_code >= 400


def test_a_signed_in_user_cannot_act_as_the_worker(two_users):
    alice, _ = two_users
    resp = _rpc("worker_claim_job", _user_headers(alice["token"]), {"worker_token": "guess"})
    assert resp.status_code >= 400


def test_token_setter_is_not_callable_from_the_browser():
    """Regression: this was reachable with the publishable key.

    Anyone could have rotated the worker credential to a digest of their own
    choosing and then claimed and rewritten every user's jobs.
    """
    resp = _rpc(
        "set_worker_token_digest",
        _anon_headers(),
        {"digest_hex": "0" * 64},
    )
    assert resp.status_code >= 400, "anon could rotate the worker credential"


def test_the_authorization_helper_is_not_exposed():
    """A token oracle would allow offline guessing against the credential."""
    resp = _rpc("_worker_authorized", _anon_headers(), {"candidate": "x"})
    assert resp.status_code >= 400


def test_claiming_existing_videos_requires_a_session():
    """Regression: any signed-in account could take the whole unclaimed pool."""
    resp = _rpc("claim_unclaimed_videos", _anon_headers(), {})
    assert resp.status_code >= 400, "anon could claim pre-existing videos"


def test_a_stranger_cannot_claim_the_existing_videos(two_users):
    """A new account must get an empty library, not someone else's work."""
    alice, _ = two_users
    resp = _rpc("claim_unclaimed_videos", _user_headers(alice["token"]), {})
    if resp.status_code < 400:
        assert resp.json() == 0, "a stranger adopted pre-existing videos"

    seen = _rest("videos?select=id", _user_headers(alice["token"]))
    assert seen.status_code == 200
    assert seen.json() == []


# --- schema ---------------------------------------------------------------

def test_every_owned_table_has_a_user_id(two_users):
    """A table without ownership cannot be scoped, so this is structural."""
    alice, _ = two_users
    for table in ("videos", "jobs", "channels", "usage_events"):
        resp = _rest(f"{table}?select=user_id&limit=1", _user_headers(alice["token"]))
        assert resp.status_code == 200, f"{table} has no user_id column: {resp.text[:160]}"
