<#
.SYNOPSIS
  Skinning decomposition — solve 4-influence weights for a character.

.DESCRIPTION
  Two halves, because Blender ships numpy but not scipy:

    dump_skin.py   (Blender, on the stage 02 blend)  full weights + bind skeleton -> .npz
    solve_skin.py  (system Python, needs scipy)      least-squares solve         -> .npz

  Stage 03 then applies the solved weights instead of the fold/filter/smooth stack. Run this
  after stage 02 and BEFORE stage 03. Re-run it whenever stage 02 output or the animation
  library changes — export_glb.py fails loudly if the solve no longer matches the mesh.

.EXAMPLE
  ./tools/run_weight_solve.ps1 -Char telly
#>
param(
  [Parameter(Mandatory=$true)][string] $Char,
  [int] $FramesPerClip = 6
)

$ErrorActionPreference = 'Stop'
$Root      = Split-Path -Parent $PSScriptRoot
$Workspace = $Root

$Config = Join-Path $Workspace '_config\pipeline.yaml'
if (-not (Test-Path $Config)) { $Config = (Resolve-Path (Join-Path $Workspace '..\..\_config\pipeline.yaml')).Path }
function Read-Yaml($key) {
  $line = (Select-String -Path $Config -Pattern "^\s*${key}:" | Select-Object -First 1).Line
  if (-not $line) { throw "$key not found in $Config" }
  ($line -replace "^\s*${key}:\s*`"?([^`"]+)`"?\s*$", '$1').Trim()
}
$BlenderExe = Read-Yaml 'blender_exe'

$Blend   = Join-Path $Workspace "characters\$Char\02-blend\$Char.blend"
$OutDir  = Join-Path $Workspace "characters\$Char\_solve"
$Dump    = Join-Path $OutDir 'skin_dump.npz'
$Solved  = Join-Path $OutDir 'skin_solved.npz'
$Anims   = Join-Path $Workspace 'docs\animations\mh-animations.glb'

if (-not (Test-Path $Blend)) { throw "stage 02 blend not found: $Blend" }
if (-not (Test-Path $Anims)) { throw "animation library not found: $Anims - run stage 05 first" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "[weight_solve] char    = $Char"
Write-Host "[weight_solve] blend   = $Blend"
Write-Host "[weight_solve] anims   = $Anims"

# stderr from a native command becomes a terminating ErrorRecord when the caller captures
# output; the exit code is the source of truth. Same reason as the other stage runners.
$PrevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $BlenderExe --factory-startup --background $Blend --python-exit-code 1 `
  --python (Join-Path $PSScriptRoot '..\stages\03-export-to-glb\tools\dump_skin.py') -- --out $Dump
$dumpCode = $LASTEXITCODE
$ErrorActionPreference = $PrevEap
if ($dumpCode -ne 0) { Write-Host "[weight_solve] dump failed ($dumpCode)"; exit 1 }
if (-not (Test-Path $Dump)) { Write-Host "[weight_solve] no dump produced"; exit 1 }

& python (Join-Path $PSScriptRoot 'solve_skin.py') --dump $Dump --animations $Anims `
  --out $Solved --frames-per-clip $FramesPerClip
if ($LASTEXITCODE -ne 0) { Write-Host "[weight_solve] solve failed"; exit 1 }

Write-Host "[weight_solve] done: $Solved"
exit 0
