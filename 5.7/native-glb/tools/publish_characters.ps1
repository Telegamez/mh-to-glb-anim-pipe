<#
.SYNOPSIS
  Publish baked characters from docs/characters/<id>/ into the monorepo's
  public/models/<id>/.

.DESCRIPTION
  Stage 04 leaves a character in `docs/characters/<id>/`. Nothing moves it into
  the web app — that copy has been manual, is easy to forget, and forgetting it
  looks exactly like "the bake did nothing", because the app keeps serving the
  previous publish with no error anywhere.

  Two things this does that a plain copy does not:

  * **Compares by content, never by timestamp.** `cp` without `-p` makes every
    destination look newer than its source, so an mtime comparison cannot tell a
    fresh publish from a stale one. Files are hashed.

  * **Prunes what the sidecar no longer references.** A re-bake that changes a
    character's groom leaves the previous style's atlas behind in the
    destination, because a copy only ever adds. Three of the six shipped
    companions were carrying atlases for grooms they no longer wear — ~13 MB of
    textures nothing loads. Harmless to the renderer, but it makes "which atlas
    is this character actually using" unanswerable by listing the directory,
    and that question has already cost this project two investigations.

  Verification is by artifact, per the handoff's §6.1: the summary reports what
  was copied, what was identical, and what was pruned. An empty result prints as
  "0", not as silence.

.PARAMETER Chars
  Workspace ids. Defaults to every character directory that stage 04 produced.

.PARAMETER Dest
  The monorepo's models root.

.PARAMETER Prune
  Delete destination files the source no longer has. Off by default — deleting
  is the irreversible half, so it is opt-in.

.PARAMETER DryRun
  Report what would happen and change nothing.

.EXAMPLE
  ./tools/publish_characters.ps1 -Chars telly,tele -DryRun

.EXAMPLE
  ./tools/publish_characters.ps1 -Prune
#>
param(
  [string[]] $Chars,
  [string]   $Dest = 'S:\Avatars\metahuman\telegamez\packages\web-next\public\models',
  [switch]   $Prune,
  [switch]   $DryRun
)

$ErrorActionPreference = 'Stop'
$Root   = Split-Path -Parent $PSScriptRoot
$SrcRoot = Join-Path $Root 'docs\characters'

if (-not (Test-Path $SrcRoot)) { throw "no bake output at $SrcRoot" }
if (-not (Test-Path $Dest))    { throw "destination does not exist: $Dest" }

# The companion roster, not every directory under docs/characters/.
#
# `ada`, `bo` and `bruce` are the pipeline's own demo characters. They live in
# the same output folder and a "publish everything" default would create web-app
# model directories for them — shipping demo assets into the product because
# they happened to share a parent directory.
$Roster = @('telly', 'tele', 'telie', 'telee', 'tele1', 'tele2')

if (-not $Chars) {
  $Chars = $Roster
  Write-Host "no -Chars given; publishing the roster: $($Chars -join ', ')"
} else {
  $offRoster = $Chars | Where-Object { $Roster -notcontains $_ }
  if ($offRoster) {
    Write-Host "note: not on the companion roster - $($offRoster -join ', ')"
  }
}

# Gallery scaffolding, not runtime assets. The web app loads the GLB, the
# sidecar and the textures; index.html and preview.png belong to the pipeline's
# own static gallery and have never been published.
$SkipNames = @('index.html', 'preview.png')

$summary = @()

foreach ($char in $Chars) {
  $src = Join-Path $SrcRoot $char
  $dst = Join-Path $Dest    $char

  if (-not (Test-Path $src)) {
    Write-Host "[$char] SKIP - no bake output at $src"
    $summary += [pscustomobject]@{
      Character = $char; Copied = 0; Same = 0; Pruned = 0; Unreferenced = 0; Note = 'no source'
    }
    continue
  }

  $copied = 0; $same = 0; $pruned = 0
  if (-not (Test-Path $dst)) {
    Write-Host "[$char] creating $dst"
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $dst | Out-Null }
  }

  $srcFiles = Get-ChildItem $src -Recurse -File |
              Where-Object { $SkipNames -notcontains $_.Name }

  foreach ($f in $srcFiles) {
    $rel    = $f.FullName.Substring($src.Length).TrimStart('\', '/')
    $target = Join-Path $dst $rel

    $identical = $false
    if (Test-Path $target) {
      # Length first: it settles almost every case without reading 8 MB of PNG.
      $t = Get-Item $target
      if ($t.Length -eq $f.Length) {
        $identical = (Get-FileHash $f.FullName -Algorithm SHA256).Hash -eq
                     (Get-FileHash $target     -Algorithm SHA256).Hash
      }
    }

    if ($identical) { $same++; continue }

    Write-Host "  + $rel"
    if (-not $DryRun) {
      $parent = Split-Path -Parent $target
      if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
      Copy-Item $f.FullName $target -Force
    }
    $copied++
  }

  # Prune: anything in the destination the source no longer produces. Compared
  # on the relative path set, so a renamed atlas registers as one add and one
  # delete rather than silently accumulating.
  $srcRel = @{}
  foreach ($f in $srcFiles) { $srcRel[$f.FullName.Substring($src.Length).TrimStart('\','/')] = $true }

  if (Test-Path $dst) {
    foreach ($f in (Get-ChildItem $dst -Recurse -File)) {
      $rel = $f.FullName.Substring($dst.Length).TrimStart('\', '/')
      if ($srcRel.ContainsKey($rel)) { continue }
      if ($SkipNames -contains $f.Name) { continue }
      if ($Prune) {
        Write-Host "  - $rel (pruned)"
        if (-not $DryRun) { Remove-Item $f.FullName -Force }
        $pruned++
      } else {
        Write-Host "  ! $rel (stale - not in bake; re-run with -Prune to remove)"
        $pruned++
      }
    }
  }

  # Textures the sidecar does not reference.
  #
  # This is a different question from "did the source have it", and the one
  # that actually catches drift: stage 04 adds to its output folder rather than
  # rebuilding it, so when a character's groom changes, the previous style's
  # atlas survives in the BAKE as well as in the publish. Both sides agree, the
  # file/content comparison above is clean, and the character is still carrying
  # an atlas for hair it no longer has.
  #
  # Reported, never deleted, even under -Prune: the source has it, so removing
  # it here would just be undone by the next publish. The fix is a clean
  # re-bake, and this is how you find out you need one.
  $sidecar = Join-Path $dst 'mh_materials.json'
  $unref = @()
  if (Test-Path $sidecar) {
    try {
      $mh = Get-Content $sidecar -Raw | ConvertFrom-Json
      $refd = [System.Collections.Generic.HashSet[string]]::new()
      foreach ($m in $mh.materials) {
        foreach ($k in 'alpha_stem', 'tangent_stem', 'coverage_stem') {
          $v = $m.params.$k
          if ($v) { [void]$refd.Add("$v.png") }
        }
        if ($m.textures) {
          foreach ($p in $m.textures.PSObject.Properties) {
            if ($p.Value) { [void]$refd.Add((Split-Path -Leaf $p.Value)) }
          }
        }
      }
      $texDir = Join-Path $dst 'textures'
      if ((Test-Path $texDir) -and $refd.Count -gt 0) {
        $unref = Get-ChildItem $texDir -File |
                 Where-Object { -not $refd.Contains($_.Name) }
      }
    } catch {
      Write-Host "  ! could not parse mh_materials.json - $($_.Exception.Message)"
    }
  }
  foreach ($u in $unref) {
    Write-Host ("  ? {0} ({1:N1} MB) - published but referenced by no material" -f `
                $u.Name, ($u.Length / 1MB))
  }

  Write-Host ("[{0}] copied {1}, unchanged {2}, stale {3}, unreferenced {4}" -f `
              $char, $copied, $same, $pruned, $unref.Count)
  $summary += [pscustomobject]@{
    Character = $char; Copied = $copied; Same = $same; Pruned = $pruned
    Unreferenced = $unref.Count; Note = ''
  }
}

Write-Host "`n======== publish summary ========"
$summary | Format-Table -AutoSize | Out-String | Write-Host
if ($DryRun) { Write-Host 'DRY RUN - nothing was written.' }
elseif (-not $Prune -and ($summary | Where-Object { $_.Pruned -gt 0 })) {
  Write-Host 'Stale files were reported but not removed. Re-run with -Prune to delete them.'
}
exit 0
