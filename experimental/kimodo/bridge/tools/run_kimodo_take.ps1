<#
.SYNOPSIS
  Kimodo BVH -> MetaHuman AnimSequence -> stage 05 evaluated GLB -> render check.

.DESCRIPTION
  1. Blender: SOMA BVH -> FBX (centimetres, placeholder mesh)            bvh_to_fbx.py
  2. UE:      import, IK_SOMA, RTG_SOMA_to_MH, batch retarget            ue_import_retarget.py
  3. UE:      stage 05's own evaluated bake on a throwaway one-clip set  bake_evaluated_anims.py
  4. Blender: render front/side/back at start, middle, end              render_check.py

  Never edits animset.json / animset.male.json / animset.female.json. The retargeted asset
  lands at /Game/Kimodo/Retargeted/<Take>; adding it to a real set is a separate, deliberate step.

.EXAMPLE
  ./run_kimodo_take.ps1 -Bvh S:\Avatars\metahuman\kimodo\_out\test_listen\test_listen_00.bvh -Take KM_TestListen_00
#>
param(
  [Parameter(Mandatory)] [string] $Bvh,
  [Parameter(Mandatory)] [string] $Take,
  [Parameter(Mandatory)] [string] $TargetMesh,
  [switch] $SkipBake,
  # Rebuild the shared SOMA mesh, IK_SOMA and RTG_SOMA_to_MH from scratch (after changing the
  # retarget setup). Otherwise they are reused across takes.
  [switch] $RebuildRigs
)
$ErrorActionPreference = 'Stop'

$Tools  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bridge = Split-Path -Parent $Tools
$Work   = Join-Path $Bridge "_work\$Take"
New-Item -ItemType Directory -Force $Work | Out-Null

$RepoRoot = (Resolve-Path (Join-Path $Bridge '../../..')).Path
$Config = Join-Path $RepoRoot '_config/pipeline.yaml'
if (-not (Test-Path $Config)) { throw "Missing $Config; copy and configure _config/pipeline.example.yaml" }
if ($Take -notmatch '^[A-Za-z0-9_]{1,64}$') { throw 'Take must contain only letters, digits and underscores (1-64 characters).' }
function Read-Yaml([string]$key) {
  $line = (Select-String -Path $Config -Pattern "^\s*$($key):" | Select-Object -First 1).Line
  ($line -replace "^\s*$($key):\s*`"?([^`"]+)`"?\s*$", '$1').Trim()
}
$EditorCmd  = Read-Yaml 'ue_editor_cmd'
$Project    = Read-Yaml 'ue_project_path'
$BlenderExe = Read-Yaml 'blender_exe'
$Stage05 = Join-Path $RepoRoot '5.7/native-glb/stages/05-animation-bake/tools/bake_evaluated_anims.py'

# --- 1. BVH -> FBX -----------------------------------------------------------
$Fbx = Join-Path $Work "$Take.fbx"
& $BlenderExe --factory-startup --background --python-exit-code 1 --python (Join-Path $Tools 'bvh_to_fbx.py') -- --bvh $Bvh --out $Fbx 2>&1 |
  Select-String '\[bvh_to_fbx\]|Error|Traceback' | ForEach-Object { Write-Host $_.Line }
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Fbx)) { throw "BVH -> FBX failed" }

# --- 2. UE import + retarget -------------------------------------------------
$env:KIMODO_FBX    = $Fbx
$env:KIMODO_TAKE   = $Take
$env:KIMODO_TARGET = $TargetMesh
$env:KIMODO_RESULT = Join-Path $Work 'retarget_result.json'
$env:KIMODO_LOG    = Join-Path $Work 'retarget.txt'
$env:KIMODO_REBUILD_RIGS = if ($RebuildRigs) { '1' } else { '' }
Remove-Item $env:KIMODO_LOG, $env:KIMODO_RESULT -ErrorAction SilentlyContinue
& $EditorCmd $Project -run=pythonscript "-script=$(Join-Path $Tools 'ue_import_retarget.py')" -unattended -nosplash -nullrhi *> (Join-Path $Work 'ue_retarget.log')
if (Test-Path $env:KIMODO_LOG) { Get-Content $env:KIMODO_LOG | ForEach-Object { Write-Host $_ } }
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $env:KIMODO_RESULT)) { throw "UE import/retarget failed - see $Work\ue_retarget.log" }
$Result = Get-Content $env:KIMODO_RESULT -Raw | ConvertFrom-Json
if ($SkipBake) { Write-Host "[run] retargeted: $($Result.retargeted) (bake skipped)"; exit 0 }

# --- 3. stage 05 evaluated bake on a one-clip set ----------------------------
$Set = Join-Path $Work 'animset.json'
@{ _comment = "throwaway one-clip set for kimodo take $Take"; required = @{ $Take = $Result.retargeted }; gestures = @{}; output = "$Take.glb" } |
  ConvertTo-Json | Set-Content $Set
$env:MH_ANIMSET = $Set
$env:MH_GLB_DIR = Join-Path $Work 'bake'
if (Test-Path $env:MH_GLB_DIR) { Remove-Item $env:MH_GLB_DIR -Recurse -Force }
New-Item -ItemType Directory -Force $env:MH_GLB_DIR | Out-Null
& $EditorCmd $Project -run=pythonscript "-script=$Stage05 --mesh=$TargetMesh" -AllowCommandletRendering -unattended -nosplash *> (Join-Path $Work 'ue_bake.log')
$Glb = Join-Path $env:MH_GLB_DIR "$Take.glb"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Glb)) { throw "stage 05 bake failed - see $Work\ue_bake.log" }
Write-Host "[run] baked $Glb ($([int]((Get-Item $Glb).Length/1KB)) KB)"

# --- 4. render check ---------------------------------------------------------
$Renders = Join-Path $Work 'renders'
& $BlenderExe --factory-startup --background --python-exit-code 1 --python (Join-Path $Tools 'render_check.py') -- --glb $Glb --out $Renders 2>&1 |
  Select-String '\[render_check\]|Traceback' | ForEach-Object { Write-Host $_.Line }
Write-Host "[run] renders in $Renders"
