"""The single-worker lock must probe a PID, never terminate it.

`os.kill(pid, 0)` is the POSIX idiom for "does this process exist". On Windows
CPython maps any non-console signal to TerminateProcess, so the same call kills
the process it was meant to test, and raises SystemError on a stale PID -- which
stopped the worker from starting at all.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "worker"))

import worker as worker_mod  # noqa: E402


def test_this_process_is_alive():
    assert worker_mod._process_is_alive(os.getpid()) is True


def test_an_impossible_pid_is_not_alive():
    assert worker_mod._process_is_alive(-1) is False
    assert worker_mod._process_is_alive(0) is False


def test_a_stale_pid_is_not_alive_and_does_not_raise():
    """A killed worker leaves its PID behind; reading it must not explode."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    proc.kill()
    proc.wait(timeout=10)
    # Windows can keep the PID resolvable for a moment after exit.
    for _ in range(20):
        if worker_mod._process_is_alive(pid) is False:
            break
        time.sleep(0.1)
    assert worker_mod._process_is_alive(pid) is False


def test_probing_a_live_process_leaves_it_running():
    """The regression that matters: probing must not be a kill."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert worker_mod._process_is_alive(proc.pid) is True
        time.sleep(0.5)
        assert proc.poll() is None, (
            "the liveness probe terminated the process it was testing"
        )
        # Still alive on a second probe.
        assert worker_mod._process_is_alive(proc.pid) is True
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


# --- the lock itself -------------------------------------------------------

def test_lock_is_acquired_and_released(tmp_path):
    lock_path = tmp_path / "worker.lock"
    with worker_mod._SingleWorkerLock(lock_path):
        assert lock_path.exists()
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not lock_path.exists()


def test_a_stale_lock_is_reclaimed(tmp_path):
    lock_path = tmp_path / "worker.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.kill()
    proc.wait(timeout=10)
    lock_path.write_text(str(proc.pid), encoding="utf-8")

    for _ in range(20):
        if not worker_mod._process_is_alive(proc.pid):
            break
        time.sleep(0.1)

    with worker_mod._SingleWorkerLock(lock_path):
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_a_live_lock_blocks_a_second_worker(tmp_path):
    lock_path = tmp_path / "worker.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        lock_path.write_text(str(proc.pid), encoding="utf-8")
        with pytest.raises(worker_mod.WorkerConfigError, match="already running"):
            with worker_mod._SingleWorkerLock(lock_path):
                pass
        # And the blocked attempt must not have killed the holder.
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_garbage_lock_file_does_not_block_startup(tmp_path):
    lock_path = tmp_path / "worker.lock"
    lock_path.write_text("not-a-pid", encoding="utf-8")
    with worker_mod._SingleWorkerLock(lock_path):
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
