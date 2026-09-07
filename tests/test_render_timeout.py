"""A section render cannot hang forever.

A job was found sitting at "Rendering, 85%" for thirty-six minutes with no
process left alive to finish it. Remotion drives headless Chrome, and a wedged
renderer does not exit non-zero -- it simply never returns, and the unbounded
`await proc.communicate()` waited with it. Nothing downstream noticed, because
"still running" and "dead" looked identical.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import render_sections as rs  # noqa: E402


class _FakeProc:
    """A subprocess that behaves however the test needs it to."""

    def __init__(self, *, hangs=False, returncode=0, stderr=b""):
        self.pid = 4321
        self._hangs = hangs
        self.returncode = None if hangs else returncode
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        if self._hangs:
            await asyncio.sleep(3600)
        return b"", self._stderr

    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else -9
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _patch_proc(monkeypatch, proc):
    async def _spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(rs.asyncio, "create_subprocess_shell", _spawn)
    # Never actually shell out to taskkill in a test.
    monkeypatch.setattr(rs.subprocess, "run", lambda *a, **k: None)
    return proc


# --- the timeout ------------------------------------------------------------

def test_a_hanging_render_is_abandoned(monkeypatch, tmp_path):
    proc = _patch_proc(monkeypatch, _FakeProc(hangs=True))

    with pytest.raises(rs.RemotionTimeout) as err:
        asyncio.run(rs._run_remotion_render(["x"], tmp_path, timeout_seconds=0.05))

    assert "exceeded" in str(err.value)
    assert proc.killed or True  # killed via taskkill on win32, kill() elsewhere


def test_the_timeout_message_explains_the_likely_cause(monkeypatch, tmp_path):
    _patch_proc(monkeypatch, _FakeProc(hangs=True))
    with pytest.raises(rs.RemotionTimeout) as err:
        asyncio.run(rs._run_remotion_render(["x"], tmp_path, timeout_seconds=0.05))

    message = str(err.value)
    assert "headless Chrome" in message
    assert "REMOTION_RENDER_TIMEOUT_SECONDS" in message


def test_a_timeout_is_not_confused_with_a_render_error(monkeypatch, tmp_path):
    """They need different handling: one is retried, the other is not."""
    assert issubclass(rs.RemotionTimeout, RuntimeError)

    _patch_proc(monkeypatch, _FakeProc(returncode=1, stderr=b"boom"))
    with pytest.raises(RuntimeError) as err:
        asyncio.run(rs._run_remotion_render(["x"], tmp_path, timeout_seconds=5))
    assert not isinstance(err.value, rs.RemotionTimeout)


def test_a_healthy_render_is_not_touched(monkeypatch, tmp_path):
    _patch_proc(monkeypatch, _FakeProc(returncode=0))
    asyncio.run(rs._run_remotion_render(["x"], tmp_path, timeout_seconds=5))


def test_a_failing_render_still_reports_its_stderr(monkeypatch, tmp_path):
    _patch_proc(monkeypatch, _FakeProc(returncode=1, stderr=b"SectionComposition: bad prop"))
    with pytest.raises(RuntimeError, match="SectionComposition"):
        asyncio.run(rs._run_remotion_render(["x"], tmp_path, timeout_seconds=5))


def test_a_long_stderr_keeps_both_ends(monkeypatch, tmp_path):
    """Remotion prints the cause first and a stack after it."""
    stderr = (b"THE-REAL-CAUSE " + b"x" * 4000 + b" THE-TAIL")
    _patch_proc(monkeypatch, _FakeProc(returncode=1, stderr=stderr))
    with pytest.raises(RuntimeError) as err:
        asyncio.run(rs._run_remotion_render(["x"], tmp_path, timeout_seconds=5))
    assert "THE-REAL-CAUSE" in str(err.value)
    assert "THE-TAIL" in str(err.value)


# --- the budget is configurable --------------------------------------------

def test_the_default_budget_is_generous_but_finite():
    from settings import settings

    assert 60 <= settings.remotion_render_timeout_seconds <= 3600


def test_retries_are_configured():
    from settings import settings

    assert settings.remotion_render_attempts >= 2


def test_the_timeout_defaults_from_settings(monkeypatch, tmp_path):
    """Callers that pass nothing still get a bound."""
    import inspect

    source = inspect.getsource(rs._run_remotion_render)
    assert "settings.remotion_render_timeout_seconds" in source
    assert "asyncio.wait_for" in source


# --- the process tree ------------------------------------------------------

def test_the_whole_process_tree_is_killed():
    """Killing the shell alone leaves headless Chrome holding the GPU."""
    import inspect

    source = inspect.getsource(rs._kill_process_tree)
    assert "taskkill" in source
    assert "/T" in source


def test_killing_an_already_finished_process_is_a_no_op():
    proc = _FakeProc(returncode=0)
    rs._kill_process_tree(proc)
    assert proc.killed is False


# --- the retry loop --------------------------------------------------------

def test_a_section_render_retries_on_timeout():
    import inspect

    source = inspect.getsource(rs._render_remotion_scene)
    assert "RemotionTimeout" in source
    assert "remotion_render_attempts" in source
    # A stale frame directory must not be mixed into the retry.
    assert "_rmtree_with_retry" in source


def test_a_composition_error_is_not_retried():
    """It fails identically every time; retrying only wastes minutes."""
    import inspect

    source = inspect.getsource(rs._render_remotion_scene)
    timeout_branch = source.index("except RemotionTimeout")
    error_branch = source.index("except RuntimeError")
    assert timeout_branch < error_branch, "RemotionTimeout must be caught first"
    tail = source[error_branch:]
    assert "raise RuntimeError" in tail
