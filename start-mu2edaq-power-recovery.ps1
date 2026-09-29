<#
.SYNOPSIS
  Start a power-outage recovery run (Windows).

.DESCRIPTION
  Bootstraps the virtual environment if it is missing, then passes every
  argument to mu2e-power-recovery.  With no arguments it runs the read-only
  survey, so a bare invocation looks at the cluster rather than changing it.

  There is no PID file. The driver takes an exclusive lock on run.lock_file
  (logs\power-recovery.lock) itself for every run that can act on hardware,
  so a second concurrent run exits 2 naming the first. The previous wrapper
  recorded its own PowerShell $PID, which the stop script then terminated,
  orphaning the Python driver; stop-mu2edaq-power-recovery.ps1 now asks the
  lock for the driver's own pid.

.EXAMPLE
  .\start-mu2edaq-power-recovery.ps1 --phase assess
  .\start-mu2edaq-power-recovery.ps1 --phase all --simulate
#>
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $ProjectDir 'venv' }
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
  Write-Host '==> no virtual environment found; bootstrapping'
  & (Join-Path $ProjectDir 'bootstrap.ps1')
}

$arguments = $args
if ($arguments.Count -eq 0) {
  Write-Host '==> no arguments given; running the read-only survey (phase 1)'
  $arguments = @('--phase', 'assess')
}

& (Join-Path $VenvDir 'Scripts\mu2e-power-recovery.exe') @arguments
exit $LASTEXITCODE
