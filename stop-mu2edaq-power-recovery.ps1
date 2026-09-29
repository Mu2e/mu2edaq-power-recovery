<#
.SYNOPSIS
  Stop a running recovery cleanly (Windows).

.DESCRIPTION
  Asks the driver to close, waits for the grace period, and only then forces
  it. The driver handles the request the same way it handles Ctrl-C: it records
  the interruption, marks the run 'interrupted', and destroys the run's private
  Kerberos caches, which can hold root-capable service tickets.

  The target is the driver's own pid, from its run lock:
  'python -m mu2edaq_power_recovery.runlock pid' prints a pid only while the
  lock is held; a free lock means any recorded pid is stale and nothing is
  stopped. Before each stop the process's command line is read again with
  Get-CimInstance Win32_Process and must contain mu2edaq_power_recovery or
  mu2e-power, so a recycled pid is never stopped.

  -Force terminates outright and none of that happens: the store is left saying
  'running' and the caches remain. Check with 'klist -l' if you use it.

.PARAMETER Force
  Terminate immediately without the grace period.

.PARAMETER Status
  Report whether a run is active and change nothing.

.PARAMETER LockFile
  Lock file (default: run.lock_file from the configuration).
#>
param([switch]$Force, [switch]$Status, [int]$Grace = 30, [string]$LockFile = '')

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $ProjectDir 'venv' }
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
  Write-Error "no python at $VenvPy (set VENV_DIR, or run bootstrap.ps1)"
  exit 2
}

# The helper from this checkout, whatever the venv has installed.
$env:PYTHONPATH = (Join-Path $ProjectDir 'src') + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { '' })

function Invoke-RunLock([string]$Command) {
  $extra = @()
  if ($LockFile) { $extra = @('--lock-file', $LockFile) }
  $output = & $VenvPy -m mu2edaq_power_recovery.runlock $Command @extra
  return @{ Code = $LASTEXITCODE; Output = ($output -join "`n").Trim() }
}

function Test-Driver([int]$ProcessId) {
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
  if (-not $proc) { return $false }
  return ($proc.CommandLine -match 'mu2edaq_power_recovery|mu2e-power')
}

function Test-Holder([int]$ProcessId) {
  $r = Invoke-RunLock 'pid'
  return ($r.Code -eq 0 -and $r.Output -eq "$ProcessId")
}

if ($Status) {
  $r = Invoke-RunLock 'status'
  Write-Host $r.Output
  if ($r.Code -le 1) { exit 0 } else { exit $r.Code }
}

$r = Invoke-RunLock 'pid'
if ($r.Code -eq 1) {
  Write-Host 'no recovery run is active (the run lock is free); nothing stopped'
  exit 0
}
if ($r.Code -ne 0 -or -not $r.Output) {
  Write-Error "could not determine the run lock holder (exit $($r.Code))"
  exit 2
}
$processId = [int]$r.Output

if (-not (Test-Driver $processId)) {
  Write-Host "error: the run lock names pid $processId, but that process is not the recovery driver; refusing to stop it."
  exit 1
}

if ($Force) {
  Write-Host "==> terminating pid $processId immediately (-Force)"
  if (Test-Driver $processId) { Stop-Process -Id $processId -Force }
  Write-Host '    note: the run store may not have recorded the interruption.'
  exit 0
}

Write-Host "==> asking pid $processId to stop, waiting up to ${Grace}s"
$process = Get-Process -Id $processId -ErrorAction SilentlyContinue
if ($process -and (Test-Driver $processId)) { $process.CloseMainWindow() | Out-Null }
$deadline = (Get-Date).AddSeconds($Grace)
while ((Get-Date) -lt $deadline) {
  if (-not (Test-Driver $processId) -or -not (Test-Holder $processId)) {
    Write-Host '    stopped cleanly'
    exit 0
  }
  Start-Sleep -Seconds 1
}

if (-not (Test-Driver $processId) -or -not (Test-Holder $processId)) {
  Write-Host '    stopped'
  exit 0
}
Write-Host "==> still running after ${Grace}s; terminating"
Stop-Process -Id $processId -Force
# The lock file stays: the lock, not the file, is authoritative.
Write-Host '    terminated. The report pages may be incomplete -- re-run'
Write-Host '    mu2e-power-report to regenerate them from the run store.'
