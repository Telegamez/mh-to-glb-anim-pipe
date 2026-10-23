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

  A loop-seam gate stands between the build and the publish: every looping clip
  is measured first-keyframe against last, and a library that snaps is refused
  rather than copied. See -MaxSeamDeg.

  Refuses to run while animset.json has unsourced clips — the D7 Fab pack must
  be installed in the UE project and its asset paths filled in first.

.PARAMETER MaxSeamDeg
  Largest tolerated first-to-last bone rotation, in degrees, on a looping clip.
  Default 2.0. Raising it is an explicit, logged decision to ship a seam.

.PARAMETER PublishTo
  Optional directory to copy the finished GLB into (e.g. the web app's
  public/models/metahuman-shared). Copy is verified by SHA-256, per §6.8 —
  mtime lies after cp.

.EXAMPLE
  ./stages/05-animation-bake/tools/run_animbake.ps1
  ./stages/05-animation-bake/tools/run_animbake.ps1 -PublishTo S:\...\public\models\metahuman-shared
#>
param(
  [string] $PublishTo,
  # Which clip set to bake. The roster is split by gender (issue #3214), so this
  # runs once per set; the file names the output GLB, so two runs cannot collide.
  [string] $AnimSet = 'animset.json',
  # Largest tolerated first-to-last bone rotation on a LOOPING clip. The gate
  # below is hard - there is no switch to skip it - but the threshold is a knob,
  # because it is a judgement about what reads as a snap and not a constant.
  [double] $MaxSeamDeg = 2.0
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
$animset = Join-Path $StageDir $AnimSet
if (-not (Test-Path $animset)) { Write-Host "[run_animbake] no such animset: $animset"; exit 1 }
# The UE half takes no argv (-run=pythonscript), so the choice travels by env var.
$env:MH_ANIMSET = $animset
Write-Host "[run_animbake] animset   : $animset"
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
# Per-set scratch. The UE half names each export after its CLIP, so two sets
# sharing one directory would let run 2 pass its "every clip exists" check on
# run 1's leftovers - a stale female Idle silently shipping inside the male GLB.
$SetTag = [IO.Path]::GetFileNameWithoutExtension($AnimSet) -replace '^animset\.?', ''
if (-not $SetTag) { $SetTag = 'shared' }
$GlbDir = Join-Path $StageDir "_work\glb\$SetTag"
if (Test-Path $GlbDir) { Remove-Item $GlbDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $GlbDir | Out-Null
$env:MH_GLB_DIR = $GlbDir
Write-Host "[run_animbake] scratch   : $GlbDir"

# Same commandlet invocation as stage 01's run_export.ps1 — the proven flag set.
#
# bake_evaluated_anims.py, NOT export_anims.py. The latter exports each AnimSequence directly,
# which is the INPUT to MetaHuman's deformation machine — the post-process ABPs
# (ABP_Body_PostProcess, ABP_Clothing_PostProcess) never run, so all ~1130 corrective and twist
# joints come out with constant transforms. That single fact is the root of the torn clothing,
# the candy-wrapped wrists and the collapsing waist documented in 00-START-HERE §6.14-6.18.
# The evaluated bake records the mesh's posed transforms with those ABPs running: 309 bones move
# instead of 53. export_anims.py is kept only as the v2 reference.
$ScriptArg = Join-Path $ToolsDir 'bake_evaluated_anims.py'
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
& $BlenderExe --factory-startup --background --python-exit-code 1 --python (Join-Path $ToolsDir 'build_animations_glb.py') `
  -- --glb-dir $GlbDir --animset $animset --out $OutGlb
if ($LASTEXITCODE -ne 0) { Write-Host "[run_animbake] Blender build failed ($LASTEXITCODE)"; exit 1 }

# --- loop-seam gate — a snapping library must not reach a room ---------------
# UE's glTF exporter samples an AnimSequence's range INCLUSIVELY, so an N-frame
# clip ships N+1 samples; the duplicate tail is what THREE.LoopRepeat jumps
# across every cycle, and it shipped as a 36 deg snap on every looping clip
# (telegamez#3321). build_animations_glb.py trims it — this proves the trim
# worked on THIS build, before the copy rather than after someone notices.
#
# Deliberately hard, and deliberately BEFORE the publish: a failed gate leaves
# the built GLB in docs/animations for inspection but never copies it onward.
$SeamCheck = Join-Path $ToolsDir 'check_loop_seams.py'
# Gate every clip the animset lists as REQUIRED: those are the looping state clips (gestures play
# once and hold their last frame). A fixed list would miss loops added later, such as the
# BreathingIdle/Listening2/Listening3 set (telegamez#4908), and fail sets that lack them.
$Looping = ($cfg.required.PSObject.Properties | ForEach-Object { $_.Name }) -join ','
Write-Host "[run_animbake] loop-seam gate (max $MaxSeamDeg deg) on: $Looping"
& python $SeamCheck $OutGlb --max-deg $MaxSeamDeg --clips $Looping
if ($LASTEXITCODE -ne 0) {
  Write-Host "[run_animbake] LOOP SEAM GATE FAILED — refusing to publish $($cfg.output)."
  Write-Host "[run_animbake] the build is at $OutGlb for inspection; it was NOT copied."
  exit 1
}

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
