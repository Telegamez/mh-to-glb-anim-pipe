<#
.SYNOPSIS
  Copy a finished character from stage 04's output to where publishing looks for it.

.DESCRIPTION
  This used to live inside the console's bake loop, which meant it could only run as part of
  a full bake. Re-run one stage -- a corrected sidecar, a re-export -- and there was no way to
  refresh the package short of baking the character again from Unreal. That cost about ten
  minutes for a file copy, and it is the reason this is a script: the console shells out to
  it, and so can a person.

  Source is `docs/characters/<id>` (stage 04's output), because that is what the package has
  always been copied from. A copy, not a move: the viewer site is built from its own copy and
  breaks if the character is taken out from under it.

  ## Previews are preserved, deliberately

  The three preview assets are excluded from the copy -- they are photographs of the
  character, produced separately -- and any already in the destination are LEFT ALONE.

  The console used to clear the destination first, which is right in a full bake because
  capture then writes fresh previews. It is wrong everywhere else: if capture is skipped
  because the local stack is down, or the packaging runs on its own, clearing leaves the
  character with no previews at all and nothing says so. The console's own message after a
  skipped capture -- "the model is finished and correct, start the stack and press Capture"
  -- assumes the previous previews are still there.

  Keeping them is also no less safe. The console's scan already compares preview mtimes
  against the content and marks a stale one, so an out-of-date picture is reported rather
  than silently shipped.

  Everything else IS cleared, so a re-bake cannot leave a file from the previous one behind:
  a stale texture nothing references still gets published with the character.

.PARAMETER Char
  Character id.

.PARAMETER Packages
  Where finished packages live. Defaults to the `characters` folder beside the repo, which is
  what the console uses.

.EXAMPLE
  ./package_character.ps1 -Char telly
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Char,
    [string]$Packages
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$PipelineRoot = (Resolve-Path (Join-Path $here '..')).Path

if (-not $Packages) {
    # <repo>/5.7/native-glb/tools -> up four is the folder holding the repo.
    $Packages = (Resolve-Path (Join-Path $here '..\..\..\..\characters') -ErrorAction SilentlyContinue).Path
    if (-not $Packages) { $Packages = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PipelineRoot))) 'characters' }
}

$built = Join-Path $PipelineRoot "docs/characters/$Char"
$model = Join-Path $built "$Char.glb"

# An exit code says a script finished, not that it produced anything.
if (-not (Test-Path $model)) {
    throw "Stage 04 output not found: $model. Run stage 04 for '$Char' first."
}

# Photographs of the character, not part of the built model. Excluded from the copy and left
# in place at the destination.
$PreviewAssets = @('preview.png', 'index.html', 'thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4')

$dest = Join-Path $Packages $Char
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$kept = @()
foreach ($p in $PreviewAssets) {
    if (Test-Path (Join-Path $dest $p)) { $kept += $p }
}

# Clear everything that is not a preview, so a re-bake leaves nothing of the last one behind.
Get-ChildItem -LiteralPath $dest -Force | Where-Object { $PreviewAssets -notcontains $_.Name } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }

$copied = 0
Get-ChildItem -LiteralPath $built -Force | Where-Object { $PreviewAssets -notcontains $_.Name } |
    ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Recurse -Force
        $copied++
    }

$bytes = (Get-ChildItem -LiteralPath $dest -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host "[package] $Char -> $dest"
Write-Host ("[package] {0} item(s) copied, {1:N1} MB total" -f $copied, ($bytes / 1MB))
if ($kept.Count) {
    Write-Host "[package] previews left in place: $($kept -join ', ')"
} else {
    Write-Host "[package] no previews here yet - capture them before publishing previews"
}
