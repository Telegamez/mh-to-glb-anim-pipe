<#
.SYNOPSIS
  Stage 01 launcher (5.7 native-glb) - run UE's GLTF exporter against
  every SkeletalMesh + hair-card StaticMesh under /Game/<char>/ and write
  <name>.glb + mh_manifest.json into characters/<char>/01-glb/.

.USAGE
  ./run_export.ps1 -Char ada

.NOTES
  Uses UnrealEditor-Cmd.exe + -run=pythonscript. No Slate ticks needed -
  AssetExportTask is synchronous.
#>
param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = (Resolve-Path (Join-Path $ToolsDir "..\..\..")).Path
# Directory name ("5.7"), used to index ue_by_version. Not the engine version -
# this box runs the 5.7 tree on UE 5.8.
$PipelineVer = Split-Path -Leaf (Split-Path -Parent $Workspace)

$ConfigPath = Join-Path (Join-Path $Workspace "..\..\") "_config\pipeline.yaml"
$ConfigPath = (Resolve-Path $ConfigPath).Path

# ue_by_version parser (same shape as stage 00 launcher).
function Get-YamlVersionPath([string]$Path, [string]$Version, [string]$Key) {
    $state = 0
    $verPat = "^\s*""?" + [regex]::Escape($Version) + """?:\s*$"
    $keyPat = "^\s+${Key}:\s*(.*)$"
    $nextVerPat = "^  ""?[^""]+""?:\s*$"
    foreach ($line in Get-Content $Path) {
        switch ($state) {
            0 { if ($line -match "^ue_by_version:\s*$") { $state = 1 } }
            1 {
                if ($line -match $verPat) { $state = 2 }
                elseif ($line -match "^[^\s#]") { $state = 0 }
            }
            2 {
                if ($line -match $keyPat) {
                    $val = $matches[1].Trim()
                    if ($val.StartsWith('"') -and $val.EndsWith('"')) {
                        $val = $val.Substring(1, $val.Length - 2)
                    }
                    return $val
                }
                if ($line -match $nextVerPat) { $state = 1 }
                elseif ($line -match "^[^\s#]") { $state = 0 }
            }
        }
    }
    throw "missing ue_by_version['$Version'].$Key in $Path"
}

# Build.version ships beside the binary, so it cannot drift from the editor we
# are about to launch the way a hand-kept yaml field can.
function Get-EngineBuild([string]$EditorExe) {
    $engineDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $EditorExe))
    $buildFile = Join-Path $engineDir "Build\Build.version"
    if (-not (Test-Path $buildFile)) { return $null }
    try {
        $v = Get-Content $buildFile -Raw | ConvertFrom-Json
        return "$($v.MajorVersion).$($v.MinorVersion).$($v.PatchVersion)"
    } catch { return $null }
}

$UEProject = Get-YamlVersionPath $ConfigPath $PipelineVer "project_path"
$UECmd     = Get-YamlVersionPath $ConfigPath $PipelineVer "editor_cmd"

$DeclaredEngine = try { Get-YamlVersionPath $ConfigPath $PipelineVer "engine_version" } catch { "" }
$EngineBuild    = Get-EngineBuild $UECmd
if ($DeclaredEngine -and $EngineBuild -and $DeclaredEngine -ne $EngineBuild) {
    Write-Warning "[stage01] pipeline.yaml declares engine_version $DeclaredEngine but $UECmd is $EngineBuild. Exporting with $EngineBuild."
}
# export_glb.py stamps mh_manifest.json with the engine it is running in; pass
# the launcher's reading too so a mismatch would be visible rather than silent.
$env:MH_ENGINE_BUILD = $EngineBuild
# Use the commandlet binary for headless Python scripts.
if (-not $UECmd.EndsWith("UnrealEditor-Cmd.exe")) {
    $UECmd = $UECmd -replace "UnrealEditor\.exe$", "UnrealEditor-Cmd.exe"
}

$CharManifest = Join-Path $Workspace "characters\$Char\manifest.json"
if (-not (Test-Path $CharManifest)) { throw "character manifest not found: $CharManifest" }
$Manifest = Get-Content $CharManifest | ConvertFrom-Json
if ($Manifest.ue_project_path) { $UEProject = $Manifest.ue_project_path }

# UE's -script parser trips over spaces in paths. Copy to a spaceless dir.
if (-not (Test-Path "C:\tmp\mh")) { New-Item -ItemType Directory -Path "C:\tmp\mh" | Out-Null }
$PyScript = Join-Path $ToolsDir "export_glb.py"
Copy-Item -Force $PyScript "C:\tmp\mh\export_glb.py"

$env:MH_PIPELINE_WORKSPACE = $Workspace

Write-Host "[stage01] pipeline dir: $PipelineVer"
Write-Host "[stage01] UE engine   : $(if ($EngineBuild) { $EngineBuild } else { 'unknown (no Build.version beside editor)' })"
Write-Host "[stage01] UE project  : $UEProject"
Write-Host "[stage01] UE cmd      : $UECmd"
Write-Host "[stage01] char        : $Char"
Write-Host "[stage01] workspace   : $Workspace"

# Workspace path has spaces - passing it through -script= splits on
# whitespace and breaks argparse. Use the MH_PIPELINE_WORKSPACE env var
# instead (export_glb._parse_args falls back to it when --workspace is
# absent). The Python file path itself we already copied to a
# space-free C:/tmp/mh/.
# GLTFExporter's USE_MESH_DATA bake needs a rendering subsystem to
# evaluate the material's shader graph. Default commandlet mode runs
# headless without a renderer, which makes the bake silently skip and
# produces GLBs with NO textures. -AllowCommandletRendering spins up
# the Slate-less renderer so the bake can execute.
$ScriptArg = "C:/tmp/mh/export_glb.py -- --char=$Char"
# TELEGAMEZ: this file sets $ErrorActionPreference = "Stop", and PowerShell
# converts a native command's stderr into an ErrorRecord whenever the CALLER
# captures output. Benign chatter - OpenXR version warnings, Blender startup
# noise - then terminates the launcher before the $LASTEXITCODE check below
# ever runs. The stage works by hand and dies under a batch runner or CI.
# Drop to "Continue" across the call so the exit code decides success, which
# is what the check was written to do. See TELEGAMEZ.md.
$PrevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $UECmd $UEProject -run=pythonscript "-script=$ScriptArg" -AllowCommandletRendering -unattended -nosplash
$ErrorActionPreference = $PrevEap
$code = $LASTEXITCODE
Write-Host "[stage01] UE exit code: $code"

# Write the per-character manifest before exiting so stage completion
# survives a Claude/dispatcher crash. The launcher is the source of
# truth for stage state — agents only verify, they don't write.
$UpdateScript = Join-Path $Workspace "tools\_update_manifest.py"
if (Test-Path $UpdateScript) {
    & python $UpdateScript --char $Char --workspace $Workspace `
        --stage "01_unreal_glb_export" --exit $code 2>&1 |
        ForEach-Object { Write-Host $_ }
}
exit $code
