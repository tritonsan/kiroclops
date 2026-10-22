param([ValidateSet('before','after','reference')][string]$Variant = 'after')
$ErrorActionPreference = 'Stop'
$KiroClopsRoot = Split-Path -Parent $PSScriptRoot
if ($Variant -eq 'reference') {
    $KiroClopsProject = Join-Path $KiroClopsRoot 'work\tiny'
    $KiroClopsEngine = Join-Path $KiroClopsRoot '.tools\godot3\Godot_v3.6-stable_win64.exe'
} else {
    $KiroClopsProject = Join-Path $KiroClopsRoot ('work\slay-' + $Variant)
    $KiroClopsEngine = Join-Path $KiroClopsRoot '.tools\godot4\Godot_v4.5.1-stable_win64.exe'
}
if (-not (Test-Path -LiteralPath $KiroClopsEngine)) { throw 'Run scripts/bootstrap.py first.' }
$KiroClopsData = Join-Path $KiroClopsRoot ('.runtime\play-' + $Variant)
$env:APPDATA = Join-Path $KiroClopsData 'appdata'
$env:LOCALAPPDATA = Join-Path $KiroClopsData 'localappdata'
New-Item -ItemType Directory -Force -Path $env:APPDATA,$env:LOCALAPPDATA | Out-Null
# This launch is intentionally interactive: the user is opening the real game.
& $KiroClopsEngine --path $KiroClopsProject
