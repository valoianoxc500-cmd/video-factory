<#
.SYNOPSIS
    Install the Video Factory worker as a durable Windows scheduled task.

.DESCRIPTION
    Runs the worker at logon and restarts it if it exits, so it survives
    reboots and crashes instead of living in whatever terminal started it.

    The task runs as the current user on purpose: the pipeline authenticates
    to Google Cloud with Application Default Credentials from this user's
    profile, so running as SYSTEM would not find them. That also rules out an
    at-startup trigger, which fires before anyone logs on.

    Recovery is the repeating trigger, not the "restart the task if it fails"
    setting. That setting does not fire for an action that returns non-zero:
    verified on this machine by killing the worker and watching the task sit at
    LastTaskResult -1 for 200 seconds without restarting. A Once trigger with a
    start time in the past and an indefinite one-minute repetition does fire.
    Each repeat is a no-op while the worker is healthy, because
    MultipleInstances is IgnoreNew -- and if one ever slipped past that, the
    kernel lock in worker/singleton.py refuses it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File worker\install_windows_service.ps1

.EXAMPLE
    # Remove it again
    powershell -ExecutionPolicy Bypass -File worker\install_windows_service.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$TaskName = "VideoFactoryWorker"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$entry = Join-Path $repoRoot "worker\run_worker.py"
$launcher = Join-Path $repoRoot "worker\run_worker.cmd"
$logDir = Join-Path $repoRoot "logs"
$log = Join-Path $logDir "worker.log"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

foreach ($p in @($python, $entry, $launcher)) {
    if (-not (Test-Path $p)) { throw "Not found: $p" }
}
if (-not (Test-Path (Join-Path $repoRoot "worker\.env"))) {
    throw "worker\.env is missing. Copy worker\.env.example and fill it in."
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Run the .cmd launcher directly: embedding the command inline made Windows
# quoting swallow the paths and the task exited 1 before producing any log.
$action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $repoRoot

# 1. At logon, for a fresh session.
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# 2. The keep-alive. Start boundary in the past so the repetition is already
#    running rather than waiting for the next logon; -RepetitionInterval is
#    what creates the Repetition object, and clearing Duration makes it
#    indefinite ([TimeSpan]::MaxValue serialises to a value the scheduler
#    rejects as out of range).
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$heartbeat.Repetition.Duration = ""
$heartbeat.Repetition.StopAtDurationEnd = $false

# Keep it alive: retry on failure, never stop it for running "too long",
# and do not let Windows kill it to save battery.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($logon, $heartbeat) `
    -Settings $settings -Principal $principal `
    -Description "Video Factory worker: polls the Vercel app and renders videos. One instance only; the worker also takes a kernel lock at workspace\.worker.lock." | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started scheduled task '$TaskName'."
Write-Host "  python : $python"
Write-Host "  entry  : $entry"
Write-Host "  log    : $log"
Write-Host ""
Write-Host "Manage it with:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Get-Content '$log' -Tail 40 -Wait"
