"""The launchers and installers that keep a worker alive.

Two defects made "restart it if it exits" a claim rather than a behaviour, and
both are easy to reintroduce because neither shows up until a worker actually
dies:

  1. The .cmd launchers ended with an `echo`, so cmd.exe returned 0 no matter
     how the worker exited. A killed worker reported success to Task Scheduler
     while the log said "exited with -1".
  2. Even with a truthful exit code, the task's RestartCount/RestartInterval
     ("if the task fails, restart every minute") does not fire for an action
     that returns non-zero. Recovery needs a repeating trigger.

Nothing here touches the live workspace lock or the real scheduled tasks:
the file checks read the repo, and the one behavioural test builds its own
launcher in tmp_path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LAUNCHERS = [
    REPO_ROOT / "worker" / "run_worker.cmd",
    REPO_ROOT / "worker" / "run_vrf_worker.cmd",
]
INSTALLERS = [
    REPO_ROOT / "worker" / "install_windows_service.ps1",
    REPO_ROOT / "worker" / "install_vrf_windows_service.ps1",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _powershell_code(script: str) -> str:
    """The script minus comments, so a comment explaining a trap is not a trap.

    Crude on purpose: `#` starting a line, and the `<# ... #>` help block.
    """
    out, in_block = [], False
    for raw in script.splitlines():
        line = raw.strip()
        if line.startswith("<#"):
            in_block = True
        if in_block:
            if line.endswith("#>"):
                in_block = False
            continue
        if line.startswith("#"):
            continue
        out.append(raw)
    return "\n".join(out)


def _executable_lines(script: str) -> list[str]:
    """Lines that actually run: no comments, no blanks."""
    lines = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("REM ") or line.startswith("::"):
            continue
        lines.append(line)
    return lines


# --- the launchers hand back the worker's exit code -----------------------


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_a_launcher_returns_the_workers_exit_code(launcher):
    assert launcher.exists(), f"{launcher} is missing"
    last = _executable_lines(_text(launcher))[-1]
    assert last == "endlocal & exit /b %RC%", (
        f"{launcher.name} must end by returning the worker's exit code; "
        f"its last statement is {last!r}"
    )


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_a_launcher_does_not_end_on_an_echo(launcher):
    """The original bug: cmd.exe returns the echo's status, which is always 0."""
    last = _executable_lines(_text(launcher))[-1]
    assert not last.lower().startswith("echo "), (
        f"{launcher.name} ends with an echo, so it always exits 0 and a dead "
        f"worker reports success to Task Scheduler"
    )


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_a_launcher_captures_errorlevel_before_logging_it(launcher):
    """%ERRORLEVEL% must be saved before the echo overwrites it."""
    lines = _executable_lines(_text(launcher))
    saved = next(i for i, line in enumerate(lines) if line.startswith('set "RC='))
    logged = next(i for i, line in enumerate(lines) if line.lower().startswith("echo ") and "%RC%" in line)
    assert saved < logged


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_a_launcher_uses_the_venv_interpreter(launcher):
    assert '".venv\\Scripts\\python.exe"' in _text(launcher), (
        f"{launcher.name} must run the venv interpreter, not whatever python "
        f"is on PATH"
    )


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe is Windows-only")
def test_the_exit_code_idiom_actually_propagates(tmp_path):
    """Prove the idiom itself works on this shell, not just that it is present.

    Builds a launcher in tmp_path with the same shape as the real ones and
    runs it. The real launchers start a worker that polls forever, so they
    cannot be executed here -- but the mechanism they rely on can.
    """
    launcher = tmp_path / "launcher.cmd"
    launcher.write_text(
        "@echo off\n"
        "setlocal\n"
        f'"{sys.executable}" -c "raise SystemExit(7)"\n'
        'set "RC=%ERRORLEVEL%"\n'
        "echo exited with %RC%\n"
        "endlocal & exit /b %RC%\n",
        encoding="utf-8",
    )

    result = subprocess.run([str(launcher)], capture_output=True, text=True, timeout=60)

    assert result.returncode == 7, (
        "the launcher swallowed the child's exit code; Task Scheduler would "
        "see success"
    )


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe is Windows-only")
def test_ending_on_an_echo_is_what_swallowed_the_code(tmp_path):
    """The bug this guards against, demonstrated rather than asserted."""
    launcher = tmp_path / "buggy.cmd"
    launcher.write_text(
        "@echo off\n"
        "setlocal\n"
        f'"{sys.executable}" -c "raise SystemExit(7)"\n'
        "echo exited with %ERRORLEVEL%\n"
        "endlocal\n",
        encoding="utf-8",
    )

    result = subprocess.run([str(launcher)], capture_output=True, text=True, timeout=60)

    assert "exited with 7" in result.stdout, "the child really did exit 7"
    assert result.returncode == 0, (
        "this is the old shape: the worker's code is lost and the task looks "
        "successful"
    )


# --- the installers configure a recovery trigger --------------------------


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda p: p.name)
def test_an_installer_configures_a_repeating_trigger(installer):
    """Recovery is the repetition; RestartCount alone never fires."""
    script = _text(installer)
    assert "-RepetitionInterval" in script, (
        f"{installer.name} has no repeating trigger, so a worker that exits "
        f"is never restarted"
    )
    assert 'Repetition.Duration = ""' in script, (
        f"{installer.name} must clear the repetition duration; an unset "
        f"duration is what Task Scheduler reads as 'repeat indefinitely'"
    )


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda p: p.name)
def test_an_installer_keeps_the_logon_trigger(installer):
    assert "-AtLogOn" in _text(installer), (
        f"{installer.name} must still start the worker at logon"
    )


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda p: p.name)
def test_an_installer_never_asks_for_timespan_maxvalue(installer):
    """It serialises to P99999999DT23H59M59S, which the scheduler rejects."""
    assert "[TimeSpan]::MaxValue" not in _powershell_code(_text(installer))


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda p: p.name)
def test_an_installer_runs_as_the_current_user(installer):
    """SYSTEM would not find the user profile's Google credentials."""
    script = _text(installer)
    assert "$env:USERDOMAIN\\$env:USERNAME" in script
    assert "-LogonType Interactive" in script


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda p: p.name)
def test_an_installer_refuses_to_start_two_task_instances(installer):
    assert "-MultipleInstances IgnoreNew" in _text(installer), (
        f"{installer.name} must let the scheduler decline a repeat while the "
        f"worker is healthy"
    )


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda p: p.name)
def test_an_installer_points_at_its_own_launcher(installer):
    """Each task runs the .cmd, not the .py: inline quoting mangles the paths."""
    script = _text(installer)
    assert "run_vrf_worker.cmd" in script or "run_worker.cmd" in script
    assert "-Execute $launcher" in script
