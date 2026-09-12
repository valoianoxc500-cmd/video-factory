"""The AI Video Maker worker: starting, staying single, and not lying.

Every case here is a failure this product actually had. Jobs sat at
"Waiting to start — 0%" in production for two reasons at once, and neither
produced an error anywhere:

  * nothing ran the worker -- there was no scheduled task for it;
  * and if something had, it would have exited immediately, because
    `settings.py` does not read `worker/.env` and the worker looked for
    APP_URL and WORKER_TOKEN in a bare environment.

The rest guards the arrangement that keeps it running: one instance, restart
on crash, restart at logon, and a customer-facing state for the case where the
worker is simply not there.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import aivideo_worker as worker  # noqa: E402

INSTALLER = REPO_ROOT / "worker" / "install_aivideo_windows_service.ps1"
LAUNCHER = REPO_ROOT / "worker" / "run_aivideo_worker.cmd"
VRF_INSTALLER = REPO_ROOT / "worker" / "install_vrf_windows_service.ps1"


# ── the environment root cause ───────────────────────────────────────

def test_the_worker_reads_worker_env_itself(tmp_path, monkeypatch):
    """settings.py does not load worker/.env; the worker must."""
    env = tmp_path / ".env"
    env.write_text(
        "APP_URL=https://example.test\nWORKER_TOKEN=abc123\n# comment\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.delenv("WORKER_TOKEN", raising=False)

    loaded = worker.load_worker_env(env)

    assert loaded >= 2
    import os
    assert os.environ["APP_URL"] == "https://example.test"
    assert os.environ["WORKER_TOKEN"] == "abc123"


def test_a_real_environment_variable_beats_the_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("APP_URL=https://from-file.test\n", encoding="utf-8")
    monkeypatch.setenv("APP_URL", "https://from-shell.test")

    worker.load_worker_env(env)

    import os
    assert os.environ["APP_URL"] == "https://from-shell.test"


def test_the_api_base_alias_is_accepted(tmp_path, monkeypatch):
    """worker/.env may spell it WORKER_API_BASE; both names must resolve."""
    env = tmp_path / ".env"
    env.write_text("WORKER_API_BASE=https://aliased.test\n", encoding="utf-8")
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.delenv("WORKER_API_BASE", raising=False)

    worker.load_worker_env(env)

    import os
    assert os.environ["APP_URL"] == "https://aliased.test"


def test_a_missing_env_file_is_not_an_error(tmp_path):
    assert worker.load_worker_env(tmp_path / "nope.env") == 0


def test_the_worker_points_at_production():
    assert worker.APP_URL == "https://video-factory-omega.vercel.app"
    assert worker.WORKER_TOKEN, "no worker token was loaded"


def test_serving_without_configuration_fails_loudly(monkeypatch):
    """Better a clear config error than a silent exit and a stuck queue."""
    monkeypatch.setattr(worker, "APP_URL", "")
    with pytest.raises(worker.WorkerConfigError, match="APP_URL"):
        worker.serve(once=True)


# ── one instance only ────────────────────────────────────────────────

def test_the_worker_takes_its_own_lock():
    lock = worker.aivideo_lock()
    assert lock.path.name == ".aivideo_worker.lock"


def test_each_worker_locks_a_different_file():
    """Three workers run side by side; only duplicates of one are the problem."""
    import vrf_worker

    assert worker.aivideo_lock().path != vrf_worker.vrf_lock().path


def test_a_second_instance_is_refused_while_the_first_holds_the_lock(tmp_path):
    from worker.singleton import AlreadyRunningError, SingleInstanceLock

    path = tmp_path / "test.lock"
    first = SingleInstanceLock(path, name="test").acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(path, name="test").acquire()
    finally:
        first.release()

    # And it is available again once the holder lets go.
    SingleInstanceLock(path, name="test").acquire().release()


def test_a_duplicate_start_exits_quietly_rather_than_erroring(monkeypatch, capsys):
    """The keep-alive fires every minute; a no-op must not look like a crash."""
    import inspect

    source = inspect.getsource(worker.main)
    assert "except AlreadyRunningError" in source
    assert "return 0" in source.split("except AlreadyRunningError")[1][:200]


# ── the scheduled task ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def installer() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_the_installer_and_launcher_exist():
    assert INSTALLER.exists()
    assert LAUNCHER.exists()


def test_it_starts_at_logon(installer):
    assert "-AtLogOn" in installer


def test_it_restarts_after_a_crash(installer):
    """A repeating trigger, because the restart-on-failure setting does not
    fire for an action that returns non-zero -- established on this machine."""
    assert "RepetitionInterval" in installer
    assert "Repetition.Duration" in installer
    assert "-RestartCount" in installer


def test_only_one_instance_may_run(installer):
    assert "-MultipleInstances IgnoreNew" in installer


def test_a_wedged_instance_is_eventually_reaped(installer):
    """IgnoreNew skips the heartbeat while an instance is *registered*
    running, which is not the same as alive."""
    assert "-ExecutionTimeLimit" in installer
    assert "Days 1" in installer


def test_it_runs_as_the_user_not_system(installer):
    """ADC lives in this user's profile; SYSTEM would not find it."""
    assert "-LogonType Interactive" in installer
    assert "USERNAME" in installer


def test_it_matches_the_proven_vrf_arrangement():
    """Same shape as the worker that already survives reboots here."""
    ours = INSTALLER.read_text(encoding="utf-8")
    theirs = VRF_INSTALLER.read_text(encoding="utf-8")
    for setting in (
        "-AtLogOn", "-MultipleInstances IgnoreNew", "-RestartCount 999",
        "-StartWhenAvailable", "-LogonType Interactive",
        "$heartbeat.Repetition.Duration", "-ExecutionTimeLimit",
    ):
        assert setting in ours and setting in theirs, setting


def test_the_launcher_returns_the_workers_exit_code():
    """Without this the wrapper always exits 0 and a dead worker looks fine."""
    body = LAUNCHER.read_text(encoding="utf-8")
    assert "exit /b %RC%" in body
    assert "aivideo_worker.py" in body


def test_the_launcher_logs_start_and_exit():
    body = LAUNCHER.read_text(encoding="utf-8")
    assert "starting aivideo worker" in body
    assert "exited with" in body
    assert "logs\\aivideo_worker.log" in body


def test_the_installer_targets_this_worker_and_not_another(installer):
    assert "aivideo_worker.py" in installer
    assert "AiVideoMakerWorker" in installer
    assert "vrf_worker.py" not in installer
    assert "run_worker.cmd" not in installer


# ── customer-safe failure messages ───────────────────────────────────

@pytest.mark.parametrize(
    "detail,expected",
    [
        ("no topic was provided", "Add a topic"),
        ("no stock provider returned usable footage", "footage"),
        ("not enough free disk space to render", "space"),
    ],
)
def test_engine_failures_become_something_a_customer_can_act_on(detail, expected):
    message = worker._customer_message(RuntimeError(detail), terminal=True)
    assert expected.lower() in message.lower()


def test_no_customer_message_leaks_internals():
    for exc, terminal in (
        (RuntimeError("pexels 429 rate limited"), False),
        (RuntimeError("ffmpeg exited 1"), True),
        (RuntimeError("edge_tts ClientConnectorCertificateError"), False),
    ):
        message = worker._customer_message(exc, terminal=terminal)
        low = message.lower()
        for leak in ("pexels", "ffmpeg", "edge", "429", "traceback", "http"):
            assert leak not in low, f"{leak!r} leaked into {message!r}"


def test_a_recoverable_failure_says_progress_is_saved():
    message = worker._customer_message(RuntimeError("render stage: boom"), terminal=False)
    assert "saved" in message.lower()


# ── bounded retries ──────────────────────────────────────────────────

def test_retries_are_bounded():
    assert 1 <= worker.MAX_ATTEMPTS <= 10


def test_a_failure_after_rendering_does_not_kill_the_worker():
    """The first real job rendered, then died on the upload and took the
    process with it. A worker that dies on one bad job stops the queue."""
    import inspect

    source = inspect.getsource(worker.run_job)
    publish = source[source.index("_publish("):]
    assert "except Exception" in publish, "the publish step is unguarded"
    # And the render stays checkpointed, so the retry re-uploads not re-renders.
    assert "could not be stored" in source


def test_the_serving_loop_survives_a_handler_crash():
    import inspect

    source = inspect.getsource(worker.serve)
    assert "run_job(client, job)" in source
    loop = source[source.index("run_job(client, job)") - 200:]
    assert "except Exception" in loop, "one bad job can stop the queue"


def test_uploads_are_named_per_job_so_a_retry_overwrites():
    """An upload retry must not leave an orphaned object behind."""
    import inspect

    source = inspect.getsource(worker._publish)
    assert 'f"videos/{job_id}.mp4"' in source
    assert '"video/mp4"' in source


def test_a_job_out_of_attempts_stops_rather_than_looping():
    import inspect

    source = inspect.getsource(worker.run_job)
    assert "attempts < MAX_ATTEMPTS" in source
    # And a job that may still be retried goes back to queued, not error.
    assert '"queued" if will_retry else "error"' in source
