param([switch]$SkipBrowser)
$ErrorActionPreference = 'Stop'
$kcRoot = Split-Path -Parent $PSScriptRoot
$kcPython = Join-Path $kcRoot '.venv-local\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $kcPython)) {
    $kcSeed = Join-Path $kcRoot '.venv-kiro\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $kcSeed)) { $kcSeed = 'python' }
    & $kcSeed -m venv (Join-Path $kcRoot '.venv-local')
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
& $kcPython -m pip install --only-binary=:all: -r (Join-Path $kcRoot 'requirements-local.lock')
if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed' }
if (Test-Path -LiteralPath (Join-Path $kcRoot 'scripts\materialize_local_kit.py')) {
    & $kcPython (Join-Path $kcRoot 'scripts\materialize_local_kit.py')
    if ($LASTEXITCODE -ne 0) { throw 'Portable annotation setup failed' }
}
if (-not $SkipBrowser) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $kcRoot '.tools\browser-local'
    & $kcPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Chromium install failed' }
}
Push-Location $kcRoot
try {
    & $kcPython -X utf8 -m kiroclops.local_engine doctor
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed; inspect the missing dependencies above' }
}
finally { Pop-Location }
