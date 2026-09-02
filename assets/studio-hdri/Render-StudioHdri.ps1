<#
.SYNOPSIS
  Renders the companion studio HDRI (#3660). See README.md.

.DESCRIPTION
  Runs author_studio_hdri.py in headless Blender and then verifies the result with
  verify_studio_hdri.py.

  --factory-startup is not optional. The bake box has third-party Blender addons installed,
  and at least one of them (cc_blender_tools) throws from its unregister handler and takes
  Blender down with an access violation before the scene is ever built. Factory startup
  loads none of them.
#>
[CmdletBinding()]
param(
    [string]$OutDir,
    [string]$LightingJson,
    [string]$Name = 'companion-studio-v1',
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $OutDir) { $OutDir = Join-Path $here 'out' }

# The source of truth, if the sister repo is checked out beside this one. The script falls
# back to its own embedded copy of the same values when it is not.
if (-not $LightingJson) {
    $guess = Join-Path $here '../../../telegamez/packages/web-next/src/services/RoomManager/Agent/materials/studio-lighting.json'
    if (Test-Path $guess) { $LightingJson = (Resolve-Path $guess).Path }
}

$blender = 'C:/Program Files/Blender Foundation/Blender 4.4/blender.exe'
$cfg = Join-Path $here '../../_config/pipeline.yaml'
if (Test-Path $cfg) {
    $line = Select-String -Path $cfg -Pattern '^\s*blender_exe:\s*"?([^"]+)"?\s*$' | Select-Object -First 1
    if ($line) { $blender = $line.Matches[0].Groups[1].Value.Trim() }
}
if (-not (Test-Path $blender)) { throw "blender not found: $blender" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$toolArgs = @(
    '--background', '--factory-startup',
    '--python', (Join-Path $here 'author_studio_hdri.py'),
    '--', '--out', $OutDir, '--name', $Name
)
if ($LightingJson) { $toolArgs += @('--lighting-json', $LightingJson) }

Write-Host "blender : $blender"
Write-Host "rig     : $(if ($LightingJson) { $LightingJson } else { 'embedded copy' })"
Write-Host "out     : $OutDir`n"

& $blender @toolArgs
if ($LASTEXITCODE -ne 0) { throw "blender exited $LASTEXITCODE" }

if (-not $SkipVerify) {
    Write-Host "`n--- verifying ---"
    $master = Join-Path $OutDir "$Name-2048.hdr"
    $verifyArgs = @((Join-Path $here 'verify_studio_hdri.py'), $master)
    if ($LightingJson) { $verifyArgs += @('--lighting-json', $LightingJson) }
    & python @verifyArgs
    if ($LASTEXITCODE -ne 0) { throw "verification failed" }
}

Write-Host "`nship: $(Join-Path $OutDir "$Name.hdr")"
