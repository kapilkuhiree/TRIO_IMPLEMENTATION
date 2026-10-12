# TRIO — local session trigger (Windows Task Scheduler)
#
# WHY THIS EXISTS
# GitHub Actions `schedule:` cron triggers are NOT reliable for intraday
# trading. Observed delays on this repo:
#     session   cron 03:40 UTC -> actually started 10:52 UTC  (~7h12m late)
#     premarket cron 10:00 UTC -> actually started 16:42 UTC  (~6h43m late)
# A 7h delay means the job wakes up AFTER the 15:00 entry cutoff and the
# 15:10 hard-flat, so it trades nothing.
#
# `workflow_dispatch` starts in a minute or two, so we let this machine
# (which is awake at the open) fire the workflow instead of cron.
# Registered by setup_task.ps1 to fire Mon-Fri at 09:18 IST.
#
# Manual run (any time):
#     powershell -ExecutionPolicy Bypass -File trigger_session.ps1
#
# Requires: gh CLI authenticated with the `workflow` scope.

$ErrorActionPreference = 'Continue'

$repo    = 'kapilkuhiree/TRIO_IMPLEMENTATION'
$wf      = 'session.yml'
$logDir  = Join-Path $PSScriptRoot 'logs'
$logFile = Join-Path $logDir 'trigger_session.log'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $logFile -Value $line
    Write-Output $line
}

Write-Log "Triggering $wf on $repo ..."

# Retry a couple of times: a transient network blip must not lose the session.
$ok = $false
for ($i = 1; $i -le 3 -and -not $ok; $i++) {
    try {
        $out = & gh workflow run $wf --ref main --repo $repo 2>&1
        if ($LASTEXITCODE -eq 0) {
            $ok = $true
            Write-Log "Dispatched OK (attempt $i): $out"
        } else {
            Write-Log "Attempt $i failed (exit $LASTEXITCODE): $out"
        }
    } catch {
        Write-Log "Attempt $i threw: $($_.Exception.Message)"
    }
    if (-not $ok) { Start-Sleep -Seconds 20 }
}

if ($ok) {
    Write-Log "Session workflow requested. It should appear in 'gh run list'."
} else {
    Write-Log "FAILED to dispatch after 3 attempts."
    exit 1
}