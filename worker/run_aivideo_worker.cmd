@echo off
REM Launcher for the AI Video Maker worker, mirroring run_vrf_worker.cmd.
REM Kept as a file rather than an inline scheduled-task argument so Windows
REM quoting rules cannot mangle the paths or the redirection.
REM
REM aivideo_worker.py lives at the repository root, not under worker/, but it
REM reads its credentials from worker\.env -- the same file the other two
REM workers use -- so all three launchers live together.

setlocal
set "REPO=%~dp0.."
cd /d "%REPO%"

if not exist "logs" mkdir "logs"

set PYTHONUNBUFFERED=1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [%DATE% %TIME%] starting aivideo worker >> "logs\aivideo_worker.log"
".venv\Scripts\python.exe" "aivideo_worker.py" >> "logs\aivideo_worker.log" 2>&1
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] aivideo worker exited with %RC% >> "logs\aivideo_worker.log"

REM Hand the worker's exit code back to Task Scheduler. Without this the
REM script's own exit code is the echo above -- always 0 -- so a worker that
REM died would report success and the restart setting would never fire.
endlocal & exit /b %RC%
