param(
  [Parameter(Mandatory)] [string] $Python,
  [string] $Characters,
  [string] $Animations,
  [string] $TargetMesh,
  [int] $Port = 7870
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Python)) { throw "Python executable not found: $Python" }
if ($Characters) { $env:KIMODO_CHARACTERS = (Resolve-Path -LiteralPath $Characters).Path }
if ($Animations) { $env:KIMODO_ANIMATIONS = (Resolve-Path -LiteralPath $Animations).Path }
if ($TargetMesh) { $env:KIMODO_TARGET_MESH = $TargetMesh }
$env:KIMODO_STUDIO_PORT = "$Port"
$env:PYTHONIOENCODING = 'utf-8'
Write-Host "Open http://localhost:$Port after the server starts."
& $Python (Join-Path $PSScriptRoot 'server.py')
exit $LASTEXITCODE
