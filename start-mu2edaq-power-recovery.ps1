<#
.SYNOPSIS
  Start a power-outage recovery run (Windows).

.DESCRIPTION
  Bootstraps the virtual environment if it is missing, then passes every
  argument to mu2e-power-recovery.  With no arguments it runs the read-only
  survey, so a bare invocation looks at the cluster rather than changing it.

.EXAMPLE
  .\start-mu2edaq-power-recovery.ps1 --phase assess
  .\start-mu2edaq-power-recovery.ps1 --phase all --simulate
#>
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$VenvDir = Join-Path $ProjectDir 'venv'
$PidFile = Join-Path $ProjectDir 'logs\power-recovery.pid'
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
  Write-Host '==> no virtual environment found; bootstrapping'
  & (Join-Path $ProjectDir 'bootstrap.ps1')
}

$logs = Join-Path $ProjectDir 'logs'
if (-not (Test-Path $logs)) { New-Item -ItemType Directory -Path $logs | Out-Null }

if (Test-Path $PidFile) {
  $existing = Get-Content $PidFile -ErrorAction SilentlyContinue
  if ($existing -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
    Write-Error "a recovery run is already active (pid $existing). Use stop-mu2edaq-power-recovery.ps1 first."
  }
  Remove-Item $PidFile -Force
}

$arguments = $args
if ($arguments.Count -eq 0) {
  Write-Host '==> no arguments given; running the read-only survey (phase 1)'
  $arguments = @('--phase', 'assess')
}

$PID | Out-File -FilePath $PidFile -Encoding ascii
try {
  & (Join-Path $VenvDir 'Scripts\mu2e-power-recovery.exe') @arguments
  exit $LASTEXITCODE
} finally {
  if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
}
