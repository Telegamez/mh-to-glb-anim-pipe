<#
.SYNOPSIS
  Stage 02 launcher (5.7 native-glb) — headless Blender import of stage 01's
  per-mesh GLBs + LSE FBX, produces characters/<id>/02-blend/<id>.blend with
  51 ARKit shape keys baked onto the face mesh + propagated onto eyebrow /
  beard / mustache card meshes.

.USAGE
  ./run_assemble.ps1 -Char ada
#>
param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

# Resolve workspace root from this script's location (stages/02-blender-assemble/tools/ -> up 3)
$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $ToolsDir "..\..\..") | Select-Object -ExpandProperty Path

# Load blender_exe from _config/pipeline.yaml. Try in-pipeline first, fall
# back to worktree-root location (config is shared across pipelines).
$Config = Join-Path $Workspace "_config\pipeline.yaml"
if (-not (Test-Path $Config)) {
    $Config = Join-Path (Join-Path $Workspace "..\..\") "_config\pipeline.yaml"
    $Config = (Resolve-Path $Config).Path
}
$BlenderLine = (Select-String -Path $Config -Pattern '^\s*blender_exe:' | Select-Object -First 1).Line
if (-not $BlenderLine) { throw "blender_exe not found in $Config" }
$BlenderExe = ($BlenderLine -replace '^\s*blender_exe:\s*"?([^"]+)"?\s*$', '$1').Trim()

$PyScript = Join-Path $ToolsDir "import_glb.py"
$InRoot   = Join-Path $Workspace "characters\$Char\01-glb"

if (-not (Test-Path $InRoot)) { throw "stage 01 outputs not found: $InRoot (run stage 01 first)" }
if (-not (Test-Path $PyScript)) { throw "import_glb.py not found: $PyScript" }

Write-Host "[run_assemble] blender   = $BlenderExe"
Write-Host "[run_assemble] workspace = $Workspace"
Write-Host "[run_assemble] char      = $Char"
Write-Host "[run_assemble] in_root   = $InRoot"

# TELEGAMEZ PATCH: --factory-startup so the bake never loads the operator's
# user addons or preferences. import_glb.py calls read_factory_settings(),
# which tears down whatever is registered — and a third-party addon that
# unregisters badly takes Blender down with it. Seen on this box:
# cc_blender_tools-2_2_5 raised in unregister() and Blender died with
# EXCEPTION_ACCESS_VIOLATION before the stage ran a single line of its own.
#
# A reproducible bake must not depend on what happens to be installed in
# someone's Blender profile. The bundled glTF and FBX IO addons are enabled
# in factory defaults, which is all this stage needs.
# TELEGAMEZ: this file sets $ErrorActionPreference = "Stop", and PowerShell
# converts a native command's stderr into an ErrorRecord whenever the CALLER
# captures output. Benign chatter - OpenXR version warnings, Blender startup
# noise - then terminates the launcher before the $LASTEXITCODE check below
# ever runs. The stage works by hand and dies under a batch runner or CI.
# Drop to "Continue" across the call so the exit code decides success, which
# is what the check was written to do. See TELEGAMEZ.md.
$PrevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $BlenderExe --factory-startup --background --python-exit-code 1 --python $PyScript -- --char $Char --workspace $Workspace
$ErrorActionPreference = $PrevEap
$code = $LASTEXITCODE
Write-Host "[run_assemble] exit code: $code"

# Write the per-character manifest before exiting so stage completion
# survives a Claude/dispatcher crash. The launcher is the source of
# truth for stage state — agents only verify, they don't write.
$UpdateScript = Join-Path $Workspace "tools\_update_manifest.py"
if (Test-Path $UpdateScript) {
    & python $UpdateScript --char $Char --workspace $Workspace `
        --stage "02_blender_assemble" --exit $code 2>&1 |
        ForEach-Object { Write-Host $_ }
}
exit $code
