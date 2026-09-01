<#
.SYNOPSIS
  Stage 06 launcher — web compression (#3662). See ../CONTEXT.md.

.DESCRIPTION
  A thin wrapper. The work is done by tools/compress_companion.mjs, which the PUBLISHER also
  drives — one implementation rather than two. A stage that grew its own copy would drift
  from the publisher's, and the drift would surface as "the bake compressed differently from
  the publish", which is a miserable bug to chase.

  Reads  characters/<id>/03-glb/   (stage 03's output)
  Writes characters/<id>/06-web/

.PARAMETER Mode
  meshopt (default) - geometry and morph targets. Loadable by the runtime today.
  full              - adds KTX2 textures. NOT loadable yet.

.EXAMPLE
  ./run_compress.ps1 -Char tele
  ./run_compress.ps1 -Char tele -Mode full -TexturePx 1024
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Char,
    [ValidateSet('meshopt', 'full')][string]$Mode = 'meshopt',
    [int]$TexturePx = 0,
    [string]$KtxBin = ''
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# tools -> 06-web-compress -> stages -> pipeline root
$pipelineRoot = (Resolve-Path (Join-Path $here '../../..')).Path

$core = Join-Path $pipelineRoot 'tools/compress_companion.mjs'
if (-not (Test-Path $core)) { throw "shared compressor not found: $core" }

$src = Join-Path $pipelineRoot "characters/$Char/03-glb"
if (-not (Test-Path (Join-Path $src "$Char.glb"))) {
    throw "stage 03 output missing: $src\$Char.glb - run stage 03 first"
}
$out = Join-Path $pipelineRoot "characters/$Char/06-web"

# --name because the source folder is `03-glb`, not the character id, so the model file name
# cannot be derived from the directory the way it can for a publishable folder.
$toolArgs = @($core, '--src', $src, '--out', $out, '--name', $Char, '--mode', $Mode)
if ($TexturePx -gt 0) { $toolArgs += @('--texture-px', $TexturePx) }
if ($KtxBin) { $toolArgs += @('--ktx-bin', $KtxBin) }

Write-Host "character : $Char"
Write-Host "source    : $src"
Write-Host "output    : $out"
Write-Host ""

& node @toolArgs
if ($LASTEXITCODE -ne 0) { throw "compression failed ($LASTEXITCODE)" }
