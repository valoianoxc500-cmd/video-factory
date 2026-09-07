"""One instance per worker, enforced by the OS.

Regression cover for a checkout found running four workers at once: two
`run_worker.py` and two `vrf_worker.py`. The video worker's guard was a PID
file read-then-written, so two launches in the same second both saw a file
nobody held and both wrote -- and `.worker.lock` ended up naming the process
that started *second*. The VRF worker had no guard at all.

The important cases here run real subprocesses. An in-process test cannot
prove a lock works, because the thing being defended against is another
process.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "worker"))

from singleton import (  # noqa: E402
    RESTART_GRACE_SECONDS,
    AlreadyRunningError,
    SingleInstanceLock,
)


def _child(script: str, *args: str) -> subprocess.CompletedProcess:
    """Run a snippet in a separate interpreter, so the lock is really tested."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )


# --- the lock itself ------------------------------------------------------


def test_a_second_process_cannot_take_a_held_lock(tmp_path):
    lock_path = tmp_path / ".worker.lock"

    with SingleInstanceLock(lock_path, name="video worker"):
        result = _child(
            """
            import sys
            sys.path.insert(0, sys.argv[1])
            from singleton import AlreadyRunningError, SingleInstanceLock

            try:
                SingleInstanceLock(sys.argv[2], name="video worker").acquire()
            except AlreadyRunningError as exc:
                print(exc)
                sys.exit(2)
            sys.exit(0)
            """,
            str(REPO_ROOT / "worker"),
            str(lock_path),
        )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "already running" in result.stdout


def test_the_lock_is_free_once_the_holder_exits(tmp_path):
    lock_path = tmp_path / ".worker.lock"

    with SingleInstanceLock(lock_path):
        pass  # released here

    # A second process gets it without having to reclaim anything.
    result = _child(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from singleton import SingleInstanceLock

        SingleInstanceLock(sys.argv[2]).acquire()
        sys.exit(0)
        """,
        str(REPO_ROOT / "worker"),
        str(lock_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_killed_holder_does_not_leave_a_stale_lock(tmp_path):
    """The OS releases the lock on exit, so there is nothing to reclaim.

    This is the case the PID file needed a liveness probe for -- and the
    probe is what made the guard racy.
    """
    lock_path = tmp_path / ".worker.lock"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, time
                sys.path.insert(0, sys.argv[1])
                from singleton import SingleInstanceLock

                SingleInstanceLock(sys.argv[2]).acquire()
                print("held", flush=True)
                time.sleep(120)
                """
            ),
            str(REPO_ROOT / "worker"),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        # While it lives, nobody else may have the lock.
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(lock_path).acquire()
    finally:
        holder.kill()
        holder.wait(timeout=30)

    # Killed, not shut down cleanly, and the lock comes back on its own.
    # Windows drops a dead process's locks a moment after the process object
    # reports exit, which is what the restart grace covers.
    lock = SingleInstanceLock(lock_path).acquire(RESTART_GRACE_SECONDS)
    lock.release()


def test_the_error_names_the_process_holding_the_lock(tmp_path):
    lock_path = tmp_path / ".worker.lock"

    with SingleInstanceLock(lock_path, name="video worker"):
        with pytest.raises(AlreadyRunningError) as caught:
            SingleInstanceLock(lock_path, name="video worker").acquire()

    message = str(caught.value)
    assert str(os.getpid()) in message, "the operator needs the holder's pid"
    assert str(lock_path) in message, "and the lock file to look at"
    assert "video worker" in message


def test_the_holders_pid_stays_readable_while_the_lock_is_held(tmp_path):
    """A Windows lock is mandatory, so the pid must sit outside the locked byte."""
    lock_path = tmp_path / ".worker.lock"

    with SingleInstanceLock(lock_path):
        text = lock_path.read_text(encoding="utf-8", errors="replace")

    assert text.split("\x00", 1)[0].strip() == str(os.getpid())


def test_two_workers_racing_from_one_launcher_produce_one_winner(tmp_path):
    """The launch shape that actually happened: both start together."""
    lock_path = tmp_path / ".worker.lock"
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from singleton import AlreadyRunningError, SingleInstanceLock
        import time

        try:
            SingleInstanceLock(sys.argv[2]).acquire()
        except AlreadyRunningError:
            sys.exit(2)
        time.sleep(3)   # hold it long enough for the sibling to collide
        sys.exit(0)
        """
    )
    args = [sys.executable, "-c", script, str(REPO_ROOT / "worker"), str(lock_path)]

    first = subprocess.Popen(args)
    second = subprocess.Popen(args)
    codes = sorted([first.wait(timeout=60), second.wait(timeout=60)])

    assert codes == [0, 2], "exactly one may win the race"


def test_a_lock_is_per_path_so_the_two_workers_do_not_block_each_other(tmp_path):
    """The video worker and the VRF worker are meant to run side by side."""
    with SingleInstanceLock(tmp_path / ".worker.lock", name="video worker"):
        with SingleInstanceLock(tmp_path / ".vrf_worker.lock", name="vrf worker"):
            pass


# --- the workers wire it in ----------------------------------------------


def test_the_video_worker_refuses_to_start_when_the_lock_is_held(monkeypatch):
    """main() must exit 2 with a clear log, not run a second worker."""
    import worker as worker_module

    lock_path = REPO_ROOT / "workspace" / ".worker.lock"
    preflight_calls: list[int] = []
    monkeypatch.setattr(
        worker_module, "preflight", lambda: preflight_calls.append(1)
    )

    with SingleInstanceLock(lock_path, name="video worker"):
        assert worker_module.main() == 2

    assert preflight_calls == [], (
        "a worker that cannot have the lock must not run preflight: it shells "
        "out to ffmpeg and node and touches storage"
    )


def test_the_video_worker_lock_and_the_vrf_lock_are_different_files():
    import worker as worker_module
    import vrf_worker

    assert worker_module.worker_lock().path != vrf_worker.vrf_lock().path


def test_serving_without_the_lock_is_refused():
    """A caller that skipped main()'s acquire cannot reach the claim loop."""
    import worker as worker_module

    unheld = SingleInstanceLock(REPO_ROOT / "workspace" / ".unused.lock")
    with pytest.raises(worker_module.WorkerConfigError):
        worker_module._serve(unheld, once=True)
