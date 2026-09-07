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
echo [%DATE% %TIME%] worker exited with %ERRORLEVEL% >> "logs\worker.log"
endlocal
