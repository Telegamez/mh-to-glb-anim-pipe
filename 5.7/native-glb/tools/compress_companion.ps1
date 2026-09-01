<#
.SYNOPSIS
  Compress one companion directory for the web (#3662).

.DESCRIPTION
  Wraps compress_companion.mjs so the publisher can stream it the same way it streams
  publish_to_cdn.ps1 — one script that does the job, callable from the GUI, from CI, or by
  hand, with no second copy of the logic in the console.

  Writes to <root>/_compressed/<char>/, NEVER inside the character directory. publish_to_cdn.ps1
  uploads its -Source recursively, so a compressed copy nested inside the character folder
  would be published alongside the original and double the payload.

.PARAMETER Char
  Character id, matching the folder name and <char>.glb inside it.

.PARAMETER Root
  Folder holding the character folders.

.PARAMETER Mode
  none (default) - no lossy step. Stage 03 already caps and de-alphas the sidecar
                   textures, which is where the weight actually was.
  meshopt        - PAUSED. Measurably lossy on morph targets (0.85 mm median RMS on the
                   largest expression targets); held pending a rendered A/B.
  full           - meshopt plus KTX2 textures. Also NOT loadable by the runtime yet.

.PARAMETER TexturePx
  Resample textures to this square size in `full` mode. 0 leaves them alone.

.EXAMPLE
  ./compress_companion.ps1 -Char telly -Root S:\bakes\ready
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Char,
    [Parameter(Mandatory = $true)][string]$Root,
    [ValidateSet('none', 'meshopt', 'full')][string]$Mode = 'none',
    [int]$TexturePx = 0,
    [string]$KtxBin = ''
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# In the repo this sits beside the .mjs; in the shared bundle everything is flat beside this
# script. Same lookup the publisher's own launcher uses, so the zip cannot drift from the repo.
$core = Join-Path $here 'compress_companion.mjs'
if (-not (Test-Path $core)) { throw "compress_companion.mjs not found beside $here" }

$src = Join-Path $Root $Char
if (-not (Test-Path $src)) { throw "character folder not found: $src" }

$out = Join-Path (Join-Path $Root '_compressed') $Char

$toolArgs = @($core, '--src', $src, '--out', $out, '--mode', $Mode)
if ($TexturePx -gt 0) { $toolArgs += @('--texture-px', $TexturePx) }
if ($KtxBin) { $toolArgs += @('--ktx-bin', $KtxBin) }

Write-Host "source : $src"
Write-Host "output : $out"
Write-Host ""

& node @toolArgs
if ($LASTEXITCODE -ne 0) { throw "compression failed ($LASTEXITCODE)" }
