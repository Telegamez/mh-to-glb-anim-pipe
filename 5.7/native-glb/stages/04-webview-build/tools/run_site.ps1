param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

# Resolve workspace root from this script's location (stages/04-webview-build/tools/ -> up 3)
$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $ToolsDir "..\..\..") | Select-Object -ExpandProperty Path

$PyScript = Join-Path $ToolsDir "build_site.py"

# Prefer system python; fall back to py launcher; last resort Blender's bundled Python.
$Py = $null
foreach ($cand in @("python", "py")) {
    $null = & $cand --version 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = $cand; break }
}
if (-not $Py) {
    $Config = Join-Path $Workspace "_config\pipeline.yaml"
    if (-not (Test-Path $Config)) {
        $Config = Join-Path (Join-Path $Workspace "..\..\") "_config\pipeline.yaml"
        $Config = (Resolve-Path $Config).Path
    }
    $BlenderLine = (Select-String -Path $Config -Pattern '^\s*blender_exe:' | Select-Object -First 1).Line
    if ($BlenderLine) {
        $BlenderExe = ($BlenderLine -replace '^\s*blender_exe:\s*"?([^"]+)"?\s*$', '$1').Trim()
        $BlenderDir = Split-Path -Parent $BlenderExe
        $BlPython = Get-ChildItem -Path $BlenderDir -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($BlPython) { $Py = $BlPython.FullName }
    }
}
if (-not $Py) { throw "No Python found (tried python, py, and Blender's bundled python.exe)" }

Write-Host "[run_site] python    = $Py"
Write-Host "[run_site] workspace = $Workspace"
Write-Host "[run_site] char      = $Char"

# TELEGAMEZ: this file sets $ErrorActionPreference = "Stop", and PowerShell
# converts a native command's stderr into an ErrorRecord whenever the CALLER
# captures output. Benign chatter - OpenXR version warnings, Blender startup
# noise - then terminates the launcher before the $LASTEXITCODE check below
# ever runs. The stage works by hand and dies under a batch runner or CI.
# Drop to "Continue" across the call so the exit code decides success, which
# is what the check was written to do. See TELEGAMEZ.md.
$PrevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Py $PyScript --char $Char --workspace $Workspace
$ErrorActionPreference = $PrevEap
$code = $LASTEXITCODE
Write-Host "[run_site] exit code: $code"

# Write the per-character manifest before exiting so stage completion
# survives a Claude/dispatcher crash. The launcher is the source of
# truth for stage state — agents only verify, they don't write.
$UpdateScript = Join-Path $Workspace "tools\_update_manifest.py"
if (Test-Path $UpdateScript) {
    & python $UpdateScript --char $Char --workspace $Workspace `
        --stage "04_webview_build" --exit $code 2>&1 |
        ForEach-Object { Write-Host $_ }
}
exit $code
