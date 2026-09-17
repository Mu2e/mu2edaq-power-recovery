<#
.SYNOPSIS
  Create or refresh the mu2edaq-power-recovery Python virtual environment.

.DESCRIPTION
  The Windows counterpart of bootstrap.sh.  Idempotent: safe on a fresh clone,
  after a git pull, or repeatedly.

  Note for Windows operators: the recovery tools drive ssh and ipmitool, so a
  Windows workstation needs an OpenSSH client (Windows 10 and later ship one)
  and a Kerberos client that can obtain an FNAL.GOV ticket.  Everything else --
  the report, the simulated rehearsal, the inventory tools -- works unchanged.

.PARAMETER WithCpp
  Also configure and build the optional C/C++ probe library with CMake.

.EXAMPLE
  .\bootstrap.ps1
  .\bootstrap.ps1 -WithCpp
#>
param([switch]$WithCpp)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$VenvDir = Join-Path $ProjectDir 'venv'
$Python  = if ($env:MU2EDAQ_PYTHON) { $env:MU2EDAQ_PYTHON } else { 'python' }

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
  Write-Error "$Python not found. Set MU2EDAQ_PYTHON to your interpreter."
}

$version = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]'3.9') {
  Write-Error "Python 3.9 or newer is required, found $version"
}

if (-not (Test-Path $VenvDir)) {
  Write-Host "==> creating the virtual environment in $VenvDir"
  & $Python -m venv $VenvDir
} else {
  Write-Host "==> reusing the existing virtual environment in $VenvDir"
}

$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
Write-Host '==> upgrading pip'
& $VenvPy -m pip install --quiet --upgrade pip setuptools wheel

Write-Host '==> installing mu2edaq-power-recovery and its dependencies'
& $VenvPy -m pip install --quiet --editable ".[dev]"

foreach ($dir in @('logs', 'data', 'html', 'core')) {
  $path = Join-Path $ProjectDir $dir
  if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
}

if ($WithCpp) {
  if (Get-Command cmake -ErrorAction SilentlyContinue) {
    Write-Host '==> building the optional C/C++ probe library'
    cmake -S $ProjectDir -B (Join-Path $ProjectDir 'build') `
          -DCMAKE_BUILD_TYPE=Release -DBOOTSTRAP_VENV=OFF
    cmake --build (Join-Path $ProjectDir 'build') --config Release
  } else {
    Write-Host '    cmake not found; skipping the C++ library.'
    Write-Host '    The pure-Python reachability fallback will be used.'
  }
}

$envFile = Join-Path $ProjectDir 'config\.env'
if (-not (Test-Path $envFile)) {
  Write-Host '==> note: config\.env does not exist.'
  Write-Host '    Copy config\.env.example to config\.env to set local overrides.'
}

Write-Host ''
Write-Host 'Bootstrap complete.'
Write-Host "  activate :  $VenvDir\Scripts\Activate.ps1"
Write-Host '  rehearse :  mu2e-power-recovery --phase all --simulate'
Write-Host '  survey   :  mu2e-power-state'
