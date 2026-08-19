<#
.SYNOPSIS
  Replace a character's corrupted glTF skin weights with Unreal's FBX weights, end to end.

.DESCRIPTION
  Unreal's glTF exporter corrupts skin weights (00-START-HERE 7.0 addendum). This runner
  applies the fix to one character:

    1. UE headless:   export body+outfit as FBX      -> characters/<id>/source/*_weights.fbx
    2. Blender:       dump FBX weights to npz        -> characters/<id>/source/fbx_weights.npz
    3. Blender:       reweight the stage-02 blend    (gates on exact vertex correspondence)
    4. run_weight_solve.ps1                          (skin dump + 4-influence solve)
    5. stage 03 export                               -> characters/<id>/03-glb/<id>.glb

  Run it on every character whose blend predates 2026-08-18 — a bake without this carries
  the corruption. Idempotent: reweighting an already-clean blend is a no-op change.

.EXAMPLE
  ./tools/run_fbx_reweight.ps1 -Char tele2
#>
param(
  [Parameter(Mandatory=$true)][string] $Char,
  [int] $FramesPerClip = 8
)

$ErrorActionPreference = 'Stop'
$Workspace = Split-Path -Parent $PSScriptRoot

$Config = Join-Path $Workspace '_config\pipeline.yaml'
if (-not (Test-Path $Config)) { $Config = (Resolve-Path (Join-Path $Workspace '..\..\_config\pipeline.yaml')).Path }
function Read-Yaml($key) {
  $line = (Select-String -Path $Config -Pattern "^\s*${key}:" | Select-Object -First 1).Line
  if (-not $line) { throw "$key not found in $Config" }
  ($line -replace "^\s*${key}:\s*`"?([^`"]+)`"?\s*$", '$1').Trim()
}
$EditorCmd  = Read-Yaml 'ue_editor_cmd'
$Project    = Read-Yaml 'ue_project_path'
$BlenderExe = Read-Yaml 'blender_exe'

$CharDir   = Join-Path $Workspace "characters\$Char"
$Manifest  = Join-Path $CharDir '01-glb\mh_manifest.json'
$SourceDir = Join-Path $CharDir 'source'
$Blend     = Join-Path $CharDir "02-blend\$Char.blend"
$Npz       = Join-Path $SourceDir 'fbx_weights.npz'
foreach ($p in @($Manifest, $Blend)) {
  if (-not (Test-Path $p)) { throw "missing $p - run stages 01/02 first" }
}
New-Item -ItemType Directory -Force $SourceDir | Out-Null

Write-Host "[fbx_reweight] 1/5 UE FBX export ($Char)"
$Script = Join-Path $Workspace 'stages\01-unreal-glb-export\tools\export_fbx_weights.py'
$UeLog = Join-Path $SourceDir 'fbx_export_ue.log'
& $EditorCmd $Project -run=pythonscript `
  "-script=$($Script -replace '\\','/') --manifest=$($Manifest -replace '\\','/') --out-dir=$($SourceDir -replace '\\','/')" `
  -AllowCommandletRendering -unattended -nosplash 2>&1 | Out-File $UeLog -Encoding utf8
Get-Content $UeLog | Where-Object { $_ -match '\[fbx_weights\]|LogPython.*Error|Traceback' } |
  ForEach-Object { Write-Host "  $_" }
if (-not (Test-Path (Join-Path $SourceDir 'fbx_weights_manifest.json'))) {
  throw "UE FBX export produced no manifest - check the UE log"
}

Write-Host "[fbx_reweight] 2/5 dump FBX weights"
& $BlenderExe --factory-startup --background --python-exit-code 1 `
  --python (Join-Path $Workspace 'stages\02-blender-assemble\tools\dump_fbx_weights.py') `
  -- --source-dir $SourceDir --out $Npz 2>&1 |
  Where-Object { $_ -match '\[dump_fbx\]|Error' } | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { throw "FBX weight dump failed" }

Write-Host "[fbx_reweight] 3/5 reweight blend"
& $BlenderExe --factory-startup --background $Blend --python-exit-code 1 `
  --python (Join-Path $Workspace 'stages\02-blender-assemble\tools\reweight_from_fbx.py') `
  -- --npz $Npz --map "auto=auto" 2>&1 |
  Where-Object { $_ -match '\[reweight\]|Error|RuntimeError' } | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { throw "reweight failed" }

Write-Host "[fbx_reweight] 4/5 skin dump + solve"
& (Join-Path $PSScriptRoot 'run_weight_solve.ps1') -Char $Char -FramesPerClip $FramesPerClip
if ($LASTEXITCODE -ne 0) { throw "weight solve failed" }

Write-Host "[fbx_reweight] 5/5 stage 03 export"
& (Join-Path $Workspace 'stages\03-export-to-glb\tools\run_export.ps1') -Char $Char
if ($LASTEXITCODE -ne 0) { throw "stage 03 export failed" }

Write-Host "[fbx_reweight] done: characters/$Char/03-glb/$Char.glb"
