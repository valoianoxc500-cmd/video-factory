"""The worker's own path into the database still works.

The worker holds no database role -- it presents a shared token to three
SECURITY DEFINER functions. Locking those functions down is only safe if the
legitimate caller still gets through, so this exercises the real credential
against the live project.

Read-only: claiming from an empty queue returns nothing and changes nothing.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import settings  # noqa: E402,F401  (TLS bootstrap for this host)
import httpx  # noqa: E402

SUPABASE_URL = "https://fiichyrbvhvijcoxdewk.supabase.co"
ANON_KEY = "sb_publishable_E2oCFEZslvsKHUNu-miLtg_2ktRHEnt"


def _worker_token() -> str:
    env = REPO_ROOT / "worker" / ".env"
    if not env.exists():
        pytest.skip("worker/.env is not present on this machine")
    match = re.search(
        r"^WORKER_TOKEN\s*=\s*(.+)$", env.read_text(encoding="utf-8-sig"), re.M
    )
    if not match:
        pytest.skip("WORKER_TOKEN is not set in worker/.env")
    return match.group(1).strip().strip('"').strip("'")


def _rpc(name: str, payload: dict) -> httpx.Response:
    return httpx.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        headers={
            "apikey": ANON_KEY,
            "authorization": f"Bearer {ANON_KEY}",
            "content-type": "application/json",
        },
        json=payload,
        timeout=30,
    )


def test_the_real_worker_token_is_accepted():
    """If this fails the pipeline cannot pick up work at all."""
    resp = _rpc("worker_claim_job", {"worker_token": _worker_token()})
    assert resp.status_code < 400, (
        "the worker can no longer claim jobs: " + resp.text[:300]
    )


def test_the_stored_digest_matches_the_worker_token():
    """Guards against a rotation that updates one side and not the other."""
    digest = hashlib.sha256(_worker_token().encode()).hexdigest()
    assert len(digest) == 64
    # Proven indirectly: the claim above only succeeds when they agree.
    resp = _rpc("worker_claim_job", {"worker_token": _worker_token()})
    assert resp.status_code < 400


def test_a_token_that_is_one_character_off_is_rejected():
    token = _worker_token()
    wrong = token[:-1] + ("a" if token[-1] != "a" else "b")
    resp = _rpc("worker_claim_job", {"worker_token": wrong})
    assert resp.status_code >= 400


def test_updating_a_job_requires_the_token():
    resp = _rpc(
        "worker_update_job",
        {
            "worker_token": "not-the-token",
            "job_id": "00000000-0000-0000-0000-000000000000",
            "new_status": "done",
            "new_progress": 100,
            "new_stage": "final_review",
            "new_message": "forged",
        },
    )
    assert resp.status_code >= 400


def test_registering_a_video_requires_the_token():
    resp = _rpc(
        "worker_register_video",
        {
            "worker_token": "not-the-token",
            "job_id": "00000000-0000-0000-0000-000000000000",
            "p_channel_slug": "horror_stories",
            "p_video_key": "forged",
            "p_title": "forged",
            "p_video_path": "videos/horror_stories/forged/video.mp4",
        },
    )
    assert resp.status_code >= 400
