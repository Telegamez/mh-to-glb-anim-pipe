<#
.SYNOPSIS
  Stage 05 — bake MetaHuman body animation loops into one shared animations GLB.

.DESCRIPTION
  Two halves: UnrealEditor-Cmd exports each animset.json AnimSequence to GLB
  via the native glTF exporter (stage 01's option set — same exporter, units
  and bone frames as the character GLBs, by construction), then Blender
  rewrites the animation tracks through the platform's 16-pair contract
  bone-rename map and exports a single animation-only GLB. A units gate in
  the Blender half refuses Hips outside a plausible metre range — the exact
  failure the v1 FBX detour shipped.

  Not per-character: one file serves the roster (clips bind by node name).

  Refuses to run while animset.json has unsourced clips — the D7 Fab pack must
  be installed in the UE project and its asset paths filled in first.

.PARAMETER PublishTo
  Optional directory to copy the finished GLB into (e.g. the web app's
  public/models/metahuman-shared). Copy is verified by SHA-256, per §6.8 —
  mtime lies after cp.

.EXAMPLE
  ./stages/05-animation-bake/tools/run_animbake.ps1
  ./stages/05-animation-bake/tools/run_animbake.ps1 -PublishTo S:\...\public\models\metahuman-shared
#>
param(
  [string] $PublishTo
)

$ErrorActionPreference = 'Stop'

$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$StageDir  = Split-Path -Parent $ToolsDir
$Workspace = Resolve-Path (Join-Path $StageDir '..\..') | Select-Object -ExpandProperty Path

# --- config (same resolution as the other stage runners) --------------------
$Config = Join-Path $Workspace '_config\pipeline.yaml'
if (-not (Test-Path $Config)) {
  $Config = (Resolve-Path (Join-Path $Workspace '..\..\_config\pipeline.yaml')).Path
}
function Read-Yaml([string]$key) {
  $line = (Select-String -Path $Config -Pattern "^\s*$($key):" | Select-Object -First 1).Line
  if (-not $line) { throw "$key not found in $Config" }
  ($line -replace "^\s*$($key):\s*`"?([^`"]+)`"?\s*$", '$1').Trim()
}
$EditorCmd  = Read-Yaml 'ue_editor_cmd'
$Project    = Read-Yaml 'ue_project_path'
$BlenderExe = Read-Yaml 'blender_exe'

# --- pre-flight: refuse while the animset is unsourced ----------------------
$animset = Join-Path $StageDir 'animset.json'
$cfg = Get-Content $animset -Raw | ConvertFrom-Json
$empty = @()
foreach ($grp in 'required', 'gestures') {
  foreach ($p in $cfg.$grp.PSObject.Properties) {
    if (-not $p.Value) { $empty += $p.Name }
  }
}
if ($empty.Count) {
  Write-Host "[run_animbake] animset.json has unsourced clips: $($empty -join ', ')"
  Write-Host "[run_animbake] install the D7 pack into the UE project and fill in the asset paths."
  exit 2
}

Write-Host "[run_animbake] editor  = $EditorCmd"
Write-Host "[run_animbake] project = $Project"
Write-Host "[run_animbake] blender = $BlenderExe"

# --- UE half: AnimSequence -> GLB (native glTF; v1's FBX detour sheared the mesh) ---
$GlbDir = Join-Path $StageDir '_work\glb'
New-Item -ItemType Directory -Force -Path $GlbDir | Out-Null

# Same commandlet invocation as stage 01's run_export.ps1 — the proven flag set.
$ScriptArg = Join-Path $ToolsDir 'export_anims.py'
& $EditorCmd $Project -run=pythonscript "-script=$ScriptArg" -AllowCommandletRendering -unattended -nosplash
if ($LASTEXITCODE -ne 0) { Write-Host "[run_animbake] UE export failed ($LASTEXITCODE)"; exit 1 }

# Assert on artifacts: every configured clip must have a real FBX.
$missing = @()
foreach ($grp in 'required', 'gestures') {
  foreach ($p in $cfg.$grp.PSObject.Properties) {
    $f = Join-Path $GlbDir "$($p.Name).glb"
    if (-not (Test-Path $f) -or (Get-Item $f).Length -lt 10KB) { $missing += $p.Name }
  }
}
if ($missing.Count) { Write-Host "[run_animbake] GLB missing/empty: $($missing -join ', ')"; exit 1 }

# --- Blender half: FBX set -> renamed animation-only GLB --------------------
$OutGlb = Join-Path $Workspace "docs\animations\$($cfg.output)"
& $BlenderExe --factory-startup --background --python (Join-Path $ToolsDir 'build_animations_glb.py') `
  -- --glb-dir $GlbDir --animset $animset --out $OutGlb
if ($LASTEXITCODE -ne 0) { Write-Host "[run_animbake] Blender build failed ($LASTEXITCODE)"; exit 1 }

# --- optional publish, hash-verified -----------------------------------------
if ($PublishTo) {
  New-Item -ItemType Directory -Force -Path $PublishTo | Out-Null
  $dst = Join-Path $PublishTo $cfg.output
  Copy-Item $OutGlb $dst -Force
  $a = (Get-FileHash $OutGlb -Algorithm SHA256).Hash
  $b = (Get-FileHash $dst    -Algorithm SHA256).Hash
  if ($a -ne $b) { Write-Host "[run_animbake] PUBLISH HASH MISMATCH"; exit 1 }
  Write-Host "[run_animbake] published $dst ($a)"
}

Write-Host "[run_animbake] done: $OutGlb"
Write-Host "[run_animbake] harness check: paste the published URL into 'animations url' and expect binds ~100%"
exit 0
