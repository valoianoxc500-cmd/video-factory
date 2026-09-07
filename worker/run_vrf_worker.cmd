@echo off
REM Launcher for the Viral Reels Finder worker, mirroring run_worker.cmd.
REM Kept as a file rather than an inline scheduled-task argument so Windows
REM quoting rules cannot mangle the paths or the redirection.
REM
REM vrf_worker.py lives at the repository root, not under worker/, but it reads
REM its credentials from worker\.env -- the same file the video worker uses --
REM so both launchers live together.

setlocal
set "REPO=%~dp0.."
cd /d "%REPO%"

if not exist "logs" mkdir "logs"

set PYTHONUNBUFFERED=1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [%DATE% %TIME%] starting vrf worker >> "logs\vrf_worker.log"
".venv\Scripts\python.exe" "vrf_worker.py" >> "logs\vrf_worker.log" 2>&1
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] vrf worker exited with %RC% >> "logs\vrf_worker.log"

REM Hand the worker's exit code back to Task Scheduler. Without this the
REM script's own exit code is the echo above -- always 0 -- so a worker that
REM died reported success, and "restart the task if it fails" never fired.
endlocal & exit /b %RC%
