<#
.SYNOPSIS
    Install the AI Video Maker worker as a durable Windows scheduled task.

.DESCRIPTION
    The third sibling of install_windows_service.ps1 and
    install_vrf_windows_service.ps1, using the same arrangement for the same
    reasons -- all of which were established the hard way on this machine:

    * Runs at logon, as the current user. The worker reaches Google with
      Application Default Credentials from this user's profile, so SYSTEM would
      not find them, and that in turn rules out an AtStartup trigger because it
      fires before anyone logs on.

    * Recovery is a repeating trigger, not the "restart the task if it fails"
      setting. That setting does not fire for an action returning non-zero;
      a Once trigger dated in the past with an indefinite one-minute repetition
      does. Each repeat is a no-op while the worker is healthy because
      MultipleInstances is IgnoreNew -- and if one ever slipped past that, the
      kernel lock at workspace\.aivideo_worker.lock refuses it.

    * ExecutionTimeLimit is one day rather than unlimited. IgnoreNew skips the
      keep-alive while an instance is *registered as running*, which is not the
      same as the worker being alive: a wrapper wedged at "Terminate batch job
      (Y/N)?" keeps the instance Running with no Python behind it, and the VRF
      task sat exactly like that for four days. A finite limit means the
      scheduler eventually reaps a wedged instance and the next heartbeat
      starts a healthy one. A healthy worker is also recycled daily, which is
      cheap: it polls, so it resumes immediately, and an interrupted job
      resumes from its checkpoint rather than restarting.

    Environment comes from worker\.env, which aivideo_worker.py reads itself --
    nothing has to be exported in a shell.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File worker\install_aivideo_windows_service.ps1

.EXAMPLE
    # Remove it again
    powershell -ExecutionPolicy Bypass -File worker\install_aivideo_windows_service.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$TaskName = "AiVideoMakerWorker"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
# aivideo_worker.py sits at the repository root, not under worker\.
$entry = Join-Path $repoRoot "aivideo_worker.py"
$launcher = Join-Path $repoRoot "worker\run_aivideo_worker.cmd"
$logDir = Join-Path $repoRoot "logs"
$log = Join-Path $logDir "aivideo_worker.log"

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

# Run the .cmd launcher directly: embedding the command inline makes Windows
# quoting swallow the paths.
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

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 1) `
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
    -Description "AI Video Maker worker: polls the Vercel app for AI Video jobs and renders them. One instance only; the worker also takes a kernel lock at workspace\.aivideo_worker.lock." | Out-Null

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
