<#
.SYNOPSIS
  Compress one companion directory for the web (#3662).

.DESCRIPTION
  Wraps compress_companion.mjs so the publisher can stream it the same way it streams
  publish_to_cdn.ps1 — one script that does the job, callable from the GUI, from CI, or by
  hand, with no second copy of the logic in the console.

  Writes to <root>/_compressed_ktx2/<char>/, NEVER inside the character directory.
  publish_to_cdn.ps1 uploads its -Source recursively, so a compressed copy nested inside the
  character folder would be published alongside the original and double the payload.

  ktx2 is the only build. Owner decision 2026-10-08: the ktx2 build is the default on panda and
  beta, and to avoid confusion end to end the meshopt build (and with it the none and full
  modes, and the <root>/_compressed/ folder they wrote) is removed. Nothing here reads or writes
  _compressed/ any more; an old one on disk is inert. meshopt itself stays as the geometry codec
  INSIDE the ktx2 build.

.PARAMETER Char
  Character id, matching the folder name and <char>.glb inside it.

.PARAMETER Root
  Folder holding the character folders.

.PARAMETER Mode
  ktx2 (the default, and the only value) - the build that ships (telegamez#3662, #3728): a KTX2
            room model, a PNG <char>.compositor.glb for the native compositor, and every
            sidecar PNG kept beside a .ktx2 sibling, both models meshopt-compressed. The bake
            makes it and Upload remakes a stale one. Publish it with the avatar manifest
            variants recorded in compression_manifest.json, or the compositor falls back to the
            KTX2 room model. Kept as a parameter so existing `-Mode ktx2` calls still work.
            meshopt, none and full were removed on 2026-10-08 (owner decision, above).

.PARAMETER NoPruneMorphs
  Keep every morph target on the room model. By default the room model drops,
  per mesh, the targets the published build stores as zeros (teeth keep 18 of 72, eyes 13):
  lossless, ~84 -> ~67 MB of three.js morph texture on tg-drake. Use this for an A/B.

.PARAMETER MorphNormals
  small (default): drop morph-target normals on every mesh but the head, no
  visible change. drop: the head too, ~26 MB less GPU memory but shallower expression creases
  up close. keep: for an A/B.

.PARAMETER NoLeanTextures
  Keep teeth/eye maps at full size and the body's specular/roughness maps too.
  By default they go to 512/512/128 px.

.PARAMETER NoSeat
  Skip <char>.seat.glb, the room model for seat-size slots (telegamez#4863): morph normals
  dropped on every mesh and embedded textures capped at 1024 px. Made by default.

.PARAMETER TexturePx
  Resample the KTX2 textures to this square size. 0 leaves them alone.

.EXAMPLE
  ./compress_companion.ps1 -Char telly -Root S:\bakes\ready
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Char,
    [Parameter(Mandatory = $true)][string]$Root,
    [ValidateSet('ktx2')][string]$Mode = 'ktx2',
    [int]$TexturePx = 0,
    [switch]$NoPruneMorphs,
    [ValidateSet('small', 'drop', 'keep')][string]$MorphNormals = 'small',
    [switch]$NoLeanTextures,
    [switch]$NoSeat,
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

$out = Join-Path (Join-Path $Root '_compressed_ktx2') $Char

$toolArgs = @($core, '--src', $src, '--out', $out, '--mode', $Mode)
if ($TexturePx -gt 0) { $toolArgs += @('--texture-px', $TexturePx) }
if ($KtxBin) { $toolArgs += @('--ktx-bin', $KtxBin) }
if ($NoPruneMorphs) { $toolArgs += @('--prune-morphs', 'off') }
$toolArgs += @('--morph-normals', $MorphNormals)
if ($NoLeanTextures) { $toolArgs += @('--lean-textures', 'off') }
if ($NoSeat) { $toolArgs += @('--seat', 'off') }

Write-Host "source : $src"
Write-Host "output : $out"
Write-Host ""

& node @toolArgs
if ($LASTEXITCODE -ne 0) { throw "compression failed ($LASTEXITCODE)" }
