<#
.SYNOPSIS
  Stop a running recovery cleanly (Windows).

.DESCRIPTION
  Asks the driver to close, waits for the grace period, and only then forces
  it. The driver handles the request the same way it handles Ctrl-C: it records
  the interruption, marks the run 'interrupted', and destroys the run's private
  Kerberos caches, which can hold root-capable service tickets.

  -Force terminates outright and none of that happens: the store is left saying
  'running' and the caches remain. Check with 'klist -l' if you use it.

.PARAMETER Force
  Terminate immediately without the grace period.

.PARAMETER Status
  Report whether a run is active and change nothing.
#>
param([switch]$Force, [switch]$Status, [int]$Grace = 30)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $ProjectDir 'logs\power-recovery.pid'

if (-not (Test-Path $PidFile)) {
  Write-Host "no recovery run is recorded as active (no $PidFile)"
  exit 0
}

$processId = Get-Content $PidFile -ErrorAction SilentlyContinue
$process = if ($processId) { Get-Process -Id $processId -ErrorAction SilentlyContinue } else { $null }

if (-not $process) {
  Write-Host "stale pid file for pid $processId; cleaning up"
  Remove-Item $PidFile -Force
  exit 0
}

if ($Status) {
  Write-Host "a recovery run is active: pid $processId"
  exit 0
}

if ($Force) {
  Write-Host "==> terminating pid $processId immediately (-Force)"
  Stop-Process -Id $processId -Force
  Remove-Item $PidFile -Force
  Write-Host '    note: the run store may not have recorded the interruption.'
  exit 0
}

Write-Host "==> asking pid $processId to stop, waiting up to ${Grace}s"
$process.CloseMainWindow() | Out-Null
if ($process.WaitForExit($Grace * 1000)) {
  Remove-Item $PidFile -Force
  Write-Host '    stopped cleanly'
  exit 0
}

Write-Host "==> still running after ${Grace}s; terminating"
Stop-Process -Id $processId -Force
Remove-Item $PidFile -Force
Write-Host '    terminated. The report pages may be incomplete -- re-run'
Write-Host '    mu2e-power-report to regenerate them from the run store.'
