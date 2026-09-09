"""A job whose worker dies stops looking like work in progress.

The dashboard polls a job row and shows whatever it says. When the worker was
killed mid-render the row stayed `running` forever, so the UI showed
"Generating…" indefinitely -- and because `activeForUser` still returned that
job, the account could not start a new one either.

Three defences, each covering the previous one's failure:

  worker heartbeat  a healthy slow render keeps touching its row, so silence
                    means something is actually wrong
  server expiry     a row untouched past the window is safely requeued on the next read
  client guard      if both of those fail, the view still stops claiming work
                    is happening
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

WEB = REPO_ROOT / "web"
REPOSITORIES = WEB / "lib" / "repositories.ts"
JOBS_ROUTE = WEB / "app" / "api" / "jobs" / "route.ts"
JOB_ROUTE = WEB / "app" / "api" / "jobs" / "[id]" / "route.ts"
CREATE_VIDEO = WEB / "components" / "CreateVideo.tsx"
WORKER = REPO_ROOT / "worker" / "worker.py"


def _read(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


# --- the worker heartbeats --------------------------------------------------

def test_the_worker_touches_the_row_between_stages():
    """Rendering runs for minutes without crossing a stage boundary."""
    source = _read(WORKER)
    assert "HEARTBEAT_SECONDS" in source
    block = source[source.index("for line in proc.stdout:"):]
    block = block[: block.index("code = proc.wait()")]
    assert "HEARTBEAT_SECONDS" in block, "no heartbeat inside the output loop"
    assert "post_update(" in block


def test_the_heartbeat_is_well_under_the_expiry_window():
    """Otherwise a healthy render would be expired mid-flight."""
    source = _read(WORKER)
    match = re.search(
        r'WORKER_HEARTBEAT_SECONDS", "(\d+)"', source
    )
    assert match, "heartbeat interval is not configurable"
    heartbeat = int(match.group(1))

    window = re.search(r"olderThanMinutes = (\d+)", _read(REPOSITORIES))
    assert window, "no expiry window found"
    assert heartbeat < int(window.group(1)) * 60 / 3, (
        "the heartbeat must fire several times inside the expiry window"
    )


def test_a_stage_marker_also_resets_the_clock():
    source = _read(WORKER)
    block = source[source.index("for line in proc.stdout:"):]
    block = block[: block.index("code = proc.wait()")]
    assert "last_post = time.monotonic()" in block


# --- the server expires abandoned jobs -------------------------------------

def test_the_repository_can_expire_stale_jobs():
    source = _read(REPOSITORIES)
    assert "async expireStale(" in source


def test_expiry_covers_only_running_jobs():
    """Queued work may be waiting for capacity; it is not a dead worker."""
    source = _read(REPOSITORIES)
    block = source[source.index("async expireStale("):]
    block = block[: block.index("\n  /**")]
    assert '.eq("status", "running")' in block


def test_expiry_requeues_with_a_safe_recovery_message():
    source = _read(REPOSITORIES)
    block = source[source.index("async expireStale("):]
    assert 'status: "queued"' in block
    assert "Saved progress is ready to resume." in block


def test_expiry_never_fails_the_request_that_triggered_it():
    """Housekeeping must not turn a working page into an error."""
    source = _read(REPOSITORIES)
    block = source[source.index("async expireStale("):]
    block = block[: block.index("\n  /**")]
    assert "return 0" in block, "an expiry failure should be swallowed"


def test_the_polling_endpoints_run_the_sweep():
    """The read a stuck user is guaranteed to make is where recovery belongs."""
    for path in (JOBS_ROUTE, JOB_ROUTE):
        assert "expireStale()" in _read(path), f"{path.name} does not sweep"


def test_creating_a_job_sweeps_before_the_one_at_a_time_check():
    """Otherwise a dead job locks the account out of starting anything."""
    source = _read(JOBS_ROUTE)
    block = source[source.index("export async function POST"):]
    assert block.index("expireStale()") < block.index("activeForUser()")


def test_the_dead_module_is_no_longer_the_only_sweep():
    """Regression: expireStaleJobs lived in lib/jobs.ts, which nothing imports.

    The SaaS rebuild moved to lib/repositories.ts and left the sweep behind, so
    abandoned jobs stopped being cleaned up at all.
    """
    imports = []
    for path in WEB.rglob("*.ts*"):
        if any(p in path.parts for p in ("node_modules", ".next", ".test-build")):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if 'from "@/lib/jobs"' in text or 'from "./jobs"' in text:
            imports.append(path.name)

    # Either lib/jobs.ts is wired back in, or the live repository owns it.
    assert imports or "async expireStale(" in _read(REPOSITORIES)


# --- the client cannot spin forever ----------------------------------------

def test_the_progress_view_gives_up_eventually():
    source = _read(CREATE_VIDEO)
    assert "STALL_AFTER_MS" in source
    assert "setStalled(true)" in source


def test_the_stall_threshold_sits_above_the_server_window():
    """The client is the last resort, not the first."""
    client = re.search(r"STALL_AFTER_MS = (\d+) \* 60 \* 1000", _read(CREATE_VIDEO))
    server = re.search(r"olderThanMinutes = (\d+)", _read(REPOSITORIES))
    assert client and server
    assert int(client.group(1)) > int(server.group(1))


def test_progress_movement_resets_the_stall_clock():
    source = _read(CREATE_VIDEO)
    assert "lastChangeRef.current = Date.now()" in source
    assert "next.updated_at !== job.updated_at" in source


def test_the_stalled_view_offers_a_way_out():
    """A dead end with no action is barely better than a spinner."""
    source = _read(CREATE_VIDEO)
    assert "Start over" in source
    assert "has not reported progress" in source


def test_the_spinner_is_hidden_once_stalled():
    source = _read(CREATE_VIDEO)
    assert "stalled && job.status" in source
