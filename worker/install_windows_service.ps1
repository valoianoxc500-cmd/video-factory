<#
.SYNOPSIS
    Install the Video Factory worker as a durable Windows scheduled task.

.DESCRIPTION
    Runs the worker at logon and restarts it if it exits, so it survives
    reboots and crashes instead of living in whatever terminal started it.

    The task runs as the current user on purpose: the pipeline authenticates
    to Google Cloud with Application Default Credentials from this user's
    profile, so running as SYSTEM would not find them.

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

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

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

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Video Factory worker: polls the Vercel app and renders videos." | Out-Null

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
