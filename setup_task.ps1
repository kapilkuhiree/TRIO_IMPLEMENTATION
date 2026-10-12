# TRIO — register the local session trigger in Windows Task Scheduler.
#
# Run ONCE (PowerShell, no admin needed for a per-user task):
#     powershell -ExecutionPolicy Bypass -File setup_task.ps1
#
# Creates "TRIO-Session-Trigger": Mon-Fri at 09:18 IST, which runs
# trigger_session.ps1 to dispatch the session workflow on GitHub.
#
# Requirements:
#   - gh CLI authenticated (gh auth status shows token with `workflow` scope)
#   - this PC must be ON at 09:18 IST (StartWhenAvailable catches a miss)

$ErrorActionPreference = 'Stop'

$taskName = 'TRIO-Session-Trigger'
$script   = Join-Path $PSScriptRoot 'trigger_session.ps1'

if (-not (Test-Path $script)) { throw "Missing $script" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""

# Mon-Fri at 09:18 local time (= IST on this machine).
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At '09:18'

# StartWhenAvailable: if the PC was asleep/off at 09:18, run as soon as it can.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Dispatches the TRIO GitHub Actions session workflow at 09:18 IST, Mon-Fri (GitHub cron is unreliable).' `
    -Force | Out-Null

Write-Host "Registered scheduled task: $taskName"
Write-Host '  Trigger : Mon-Fri 09:18 local time'
Write-Host "  Script  : $script"
Write-Host ''
Write-Host 'Test it now with:'
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
Write-Host 'Remove it with:'
Write-Host "  Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"