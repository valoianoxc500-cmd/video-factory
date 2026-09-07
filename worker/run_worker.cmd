@echo off
REM Launcher for the Video Factory worker.
REM Kept as a file rather than an inline scheduled-task argument so Windows
REM quoting rules cannot mangle the paths or the redirection.

setlocal
set "REPO=%~dp0.."
cd /d "%REPO%"

if not exist "logs" mkdir "logs"

set PYTHONUNBUFFERED=1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [%DATE% %TIME%] starting worker >> "logs\worker.log"
".venv\Scripts\python.exe" "worker\run_worker.py" >> "logs\worker.log" 2>&1
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] worker exited with %RC% >> "logs\worker.log"

REM Hand the worker's exit code back to Task Scheduler. Without this the
REM script's own exit code is the echo above -- always 0 -- so a worker that
REM died reported success, and the task's "restart on failure" never fired.
endlocal & exit /b %RC%
