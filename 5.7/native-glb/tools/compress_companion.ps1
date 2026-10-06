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
  none    - no lossy step, and now a near-copy: stage 03 already caps and de-alphas the
            sidecar textures, which is where most of the weight actually was. Kept as the
            control for a comparison, not as a way to save anything.
  meshopt - the real saving, roughly halving a companion again. Lossy on geometry: morph
            targets drift 0.85 mm median RMS on the largest expression targets, and base
            vertex positions are quantised too, which is about 87% of the visible
            difference and lands on hair and eyelash card edges. Compared against the
            original through the real runtime -- material profile, hair calibration and
            clothing textures live -- and indistinguishable. This is the build that ships
            (telegamez#3728): the bake makes it and Upload remakes a stale one. No flag.
  ktx2    - the publishable KTX2 build (telegamez#3662): a KTX2 room model, a PNG
            <char>.compositor.glb for the native compositor, and every sidecar PNG kept beside
            a .ktx2 sibling. Publish it with the avatar manifest variants recorded in
            compression_manifest.json, or the compositor falls back to the KTX2 room model.
  full    - meshopt plus KTX2 textures only. Browser-only: the native compositor reads the
            same files and cannot decode KTX2. Measurement only.

.PARAMETER NoPruneMorphs
  `ktx2` only. Keep every morph target on the room model. By default the room model drops,
  per mesh, the targets the published build stores as zeros (teeth keep 18 of 72, eyes 13):
  lossless, ~84 -> ~67 MB of three.js morph texture on tg-drake. Use this for an A/B.

.PARAMETER TexturePx
  Resample the KTX2 textures to this square size in `ktx2`/`full` mode. 0 leaves them alone.

.EXAMPLE
  ./compress_companion.ps1 -Char telly -Root S:\bakes\ready
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Char,
    [Parameter(Mandatory = $true)][string]$Root,
    [ValidateSet('none', 'meshopt', 'ktx2', 'full')][string]$Mode = 'meshopt',
    [int]$TexturePx = 0,
    [switch]$NoPruneMorphs,
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
if ($NoPruneMorphs) { $toolArgs += @('--prune-morphs', 'off') }

Write-Host "source : $src"
Write-Host "output : $out"
Write-Host ""

& node @toolArgs
if ($LASTEXITCODE -ne 0) { throw "compression failed ($LASTEXITCODE)" }
