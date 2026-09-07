"""Discover always reaches a state the user can read.

The bug this guards against had no error and no result: a search was enqueued,
nothing ever claimed it, and the page polled a row that would never change
while showing "Searching…" indefinitely. Every branch below exists so that a
search ends in either results or an explanation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DISCOVER = REPO_ROOT / "web" / "components" / "reels" / "Discover.tsx"
RUNNER = REPO_ROOT / "viral" / "runner.py"
VRF_ENTRY = REPO_ROOT / "vrf_worker.py"


def _read(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


# --- the spinner is bounded ------------------------------------------------

def test_polling_gives_up_on_an_unclaimed_search():
    source = _read(DISCOVER)
    assert "UNCLAIMED_TIMEOUT_MS" in source, (
        "there is no bound on how long a queued search may spin"
    )
    assert "RUNNING_TIMEOUT_MS" in source


def test_the_timeout_message_names_the_actual_cause():
    """"Something went wrong" would not have helped anyone here."""
    source = _read(DISCOVER)
    assert "vrf_worker.py" in source, (
        "the stall message should name the worker that runs discovery"
    )
    assert "No worker picked up this search" in source


def test_a_failed_poll_does_not_silently_end_polling():
    source = _read(DISCOVER)
    block = source[source.index("const poll = useCallback"):]
    block = block[: block.index("}, []);")]
    assert "try {" in block and "catch" in block, (
        "a rejected fetch would abort polling with the spinner still showing"
    )
    assert "response.ok" in block, "a non-200 poll response is not handled"


def test_an_expired_session_is_reported_as_such():
    source = _read(DISCOVER)
    assert "session expired" in source.lower()


def test_the_spinner_yields_to_the_stall_message():
    source = _read(DISCOVER)
    assert "running && !stalled" in source, (
        "the searching message can still render alongside the error"
    )


def test_queued_and_running_read_differently():
    """"Queued" and "searching" are different facts about a search."""
    source = _read(DISCOVER)
    assert "Queued" in source
    assert "waiting for the worker" in source.lower()


def test_starting_a_new_search_clears_the_previous_stall():
    source = _read(DISCOVER)
    block = source[source.index("async function search"):]
    block = block[: block.index("async function save")]
    assert 'setStalled("")' in block


# --- a completed-but-empty search explains itself --------------------------

def test_a_configuration_note_is_rendered():
    source = _read(DISCOVER)
    assert "task.result?.note" in source, (
        "the worker explains an empty result in `note`; the page ignores it"
    )


def test_provider_reasons_are_rendered_when_there_is_no_note():
    source = _read(DISCOVER)
    assert "No discovery source is available" in source


def test_the_worker_explains_a_missing_youtube_key():
    source = _read(RUNNER)
    block = source[source.index("def run_discover"):]
    block = block[: block.index("\ndef ")]
    assert "YOUTUBE_API_KEY" in block
    assert "No discovery provider is configured" in block


# --- the worker can actually run -------------------------------------------

def test_the_vrf_entry_point_bootstraps_tls():
    """Without this the worker cannot reach the deployment to claim anything."""
    source = _read(VRF_ENTRY)
    assert re.search(r"^import settings", source, re.M), (
        "vrf_worker.py does not import settings, so HTTPS fails wherever TLS "
        "is intercepted and no task is ever claimed"
    )


def test_a_missing_token_key_does_not_stop_discovery():
    """Discovery never touches an OAuth token; it must not need the key."""
    source = _read(RUNNER)
    block = source[source.index("def main("):]
    assert "return 2" not in block.split("client = WorkerClient()")[0], (
        "the worker still exits when VRF_TOKEN_KEY is missing, which disables "
        "discovery over a capability it does not use"
    )
    assert "cipher = None" in block


def test_publish_jobs_are_not_claimed_without_the_key():
    """Claiming one would take it out of the queue only to fail it."""
    source = _read(RUNNER)
    block = source[source.index("def poll_once"):]
    block = block[: block.index("def main(")]
    assert "if cipher is not None:" in block


def test_the_worker_says_what_is_disabled():
    source = _read(RUNNER)
    assert "publishing disabled: no VRF_TOKEN_KEY" in source


# --- discovery only uses lawful sources ------------------------------------

@pytest.mark.parametrize(
    "platform", ["tiktok", "instagram", "facebook", "snapchat"]
)
def test_platforms_without_an_open_api_report_why(platform):
    from viral.discovery import default_providers

    statuses = {p.status().platform: p.status() for p in default_providers()}
    assert platform in statuses
    assert statuses[platform].reason, f"{platform} gives no reason"
    assert statuses[platform].availability.value != "ready"


def test_youtube_reports_missing_credentials_rather_than_failing(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    from viral.discovery import Availability, YouTubeDiscovery

    status = YouTubeDiscovery(api_key="").status()
    assert status.availability is Availability.NEEDS_CREDENTIALS
    assert "YOUTUBE_API_KEY" in status.reason
