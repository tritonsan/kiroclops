<#
.SYNOPSIS
Creates or checks the project-local, locked Python environment for KiroClops Phase 2.
.DESCRIPTION
Uses CPython 3.10 x64. A new environment installs wheels only from the exact lock.
An existing environment is checked without upgrading or replacing packages.
No system PATH, global Python, Kiro account or Phase 1 environment is modified.
.PARAMETER PythonExecutable
Optional full path to CPython 3.10. Used only when creating .venv-kiro.
.PARAMETER CheckOnly
Check an existing environment. Never create an environment or download packages.
#>
[CmdletBinding()]
param(
    [string]$PythonExecutable,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$KiroProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$KiroEnvironment = Join-Path $KiroProjectRoot '.venv-kiro'
$KiroPython = Join-Path $KiroEnvironment 'Scripts\python.exe'
$KiroLock = Join-Path $KiroProjectRoot 'requirements-kiro.lock'

if (-not (Test-Path -LiteralPath $KiroLock -PathType Leaf)) {
    throw "Missing dependency lock: $KiroLock"
}

function Test-KiroPython {
    param([string]$Executable)
    $KiroProbe = 'import json,platform,struct,sys; print(json.dumps({"version":list(sys.version_info[:3]),"implementation":platform.python_implementation(),"bits":struct.calcsize("P")*8,"executable":sys.executable,"prefix":sys.prefix,"base_prefix":sys.base_prefix}))'
    $KiroProbeResult = & $Executable -I -c $KiroProbe
    if ($LASTEXITCODE -ne 0) {
        throw "Python probe failed: $Executable"
    }
    $KiroInfo = $KiroProbeResult | ConvertFrom-Json
    if ($KiroInfo.implementation -ne 'CPython' -or $KiroInfo.version[0] -ne 3 -or
        $KiroInfo.version[1] -ne 10 -or $KiroInfo.bits -ne 64) {
        throw "CPython 3.10 x64 is required; found $($KiroInfo.implementation) $($KiroInfo.version -join '.') / $($KiroInfo.bits) bit."
    }
    return $KiroInfo
}

function Find-KiroBasePython {
    if ($PythonExecutable) {
        if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
            throw "Python executable not found: $PythonExecutable"
        }
        $KiroExplicit = (Resolve-Path -LiteralPath $PythonExecutable).Path
        $null = Test-KiroPython $KiroExplicit
        return $KiroExplicit
    }
    $KiroPhase1Python = Join-Path $KiroProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $KiroPhase1Python -PathType Leaf) {
        $null = Test-KiroPython $KiroPhase1Python
        return $KiroPhase1Python
    }
    $KiroLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($KiroLauncher) {
        $KiroLocated = & $KiroLauncher.Source -3.10 -I -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -eq 0 -and $KiroLocated -and (Test-Path -LiteralPath $KiroLocated -PathType Leaf)) {
            $null = Test-KiroPython $KiroLocated
            return $KiroLocated
        }
    }
    throw 'CPython 3.10 x64 was not found. Supply -PythonExecutable with its full python.exe path.'
}

$KiroCreated = $false
if (Test-Path -LiteralPath $KiroEnvironment) {
    $KiroEnvironmentItem = Get-Item -LiteralPath $KiroEnvironment -Force
    if (-not $KiroEnvironmentItem.PSIsContainer -or
        ($KiroEnvironmentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw '.venv-kiro must be a real directory inside this project, not a reparse point.'
    }
    if (-not (Test-Path -LiteralPath $KiroPython -PathType Leaf)) {
        throw 'Existing .venv-kiro has no Python executable. Review the incomplete directory before recreating it; this script will not delete it.'
    }
    Write-Host 'Checking existing .venv-kiro; installed packages will not be changed.'
} else {
    if ($CheckOnly) {
        throw '.venv-kiro does not exist. Run without -CheckOnly to create it.'
    }
    $KiroBasePython = Find-KiroBasePython
    Write-Host 'Creating project-local .venv-kiro with CPython 3.10 x64.'
    & $KiroBasePython -I -m venv $KiroEnvironment
    if ($LASTEXITCODE -ne 0) {
        throw 'Virtual environment creation failed; no existing environment was removed.'
    }
    $KiroCreated = $true
}

$KiroRuntime = Test-KiroPython $KiroPython
if ([IO.Path]::GetFullPath($KiroRuntime.prefix) -ne $KiroEnvironment -or
    $KiroRuntime.prefix -eq $KiroRuntime.base_prefix) {
    throw 'Python is not running inside the expected .venv-kiro environment.'
}

if ($KiroCreated) {
    # --no-deps prevents resolving unpinned transitive packages outside the lock.
    # pip check below verifies that the lock itself is dependency-complete.
    & $KiroPython -I -m pip --isolated --disable-pip-version-check install --only-binary=:all: --no-deps --requirement $KiroLock
    if ($LASTEXITCODE -ne 0) {
        throw 'Locked wheel installation failed. The incomplete local environment was preserved for inspection.'
    }
}

$KiroVerify = @'
from importlib import metadata
import json
from pathlib import Path
import re
import sys

def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()

expected = {}
for number, raw in enumerate(Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
    if not match:
        raise SystemExit(f"Lock line {number} is not an exact package==version pin")
    name, version = canonical(match[1]), match[2]
    if name in expected:
        raise SystemExit(f"Duplicate package in lock: {name}")
    expected[name] = version
if not expected:
    raise SystemExit("Dependency lock is empty")
actual = {canonical(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}
errors = [f"{name}: expected {version}, installed {actual.get(name, 'MISSING')}"
          for name, version in expected.items() if actual.get(name) != version]
errors += [f"Unexpected installed package: {name}=={version}" for name, version in actual.items() if name not in expected]
if errors:
    print("Dependency lock mismatch; no existing packages were changed.", file=sys.stderr)
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
import cv2
import numpy
import mcp
print(json.dumps({"locked_packages": len(expected), "python": sys.version.split()[0],
                  "opencv": cv2.__version__, "numpy": numpy.__version__,
                  "mcp": metadata.version("mcp")}, ensure_ascii=False))
'@
& $KiroPython -I -c $KiroVerify $KiroLock
if ($LASTEXITCODE -ne 0) {
    throw 'Environment verification failed. Review version differences before explicitly rebuilding .venv-kiro.'
}
& $KiroPython -I -m pip --isolated --disable-pip-version-check check
if ($LASTEXITCODE -ne 0) {
    throw 'The locked environment has unsatisfied dependency requirements.'
}
Write-Host 'KiroClops Phase 2 environment is ready. No Kiro model run was started.'
