<#
.SYNOPSIS
  Bake one MetaHuman all the way to a publishable package.

.DESCRIPTION
  Bootstrap, the five stages, the package copy and the previews -- the whole route, in the
  order the console runs it, as a script anyone can call.

  It exists because the batch runner needs the same sequence the console does, and a second
  copy of a five-stage pipeline is a copy that drifts.

  The console's own bake still has its inline version. That is deliberate: migrating a working
  path onto an untested script, in the same change that introduces the script, is how both end
  up broken. Once a batch has run this end to end the switch is one line with evidence behind
  it.

  ## What it does not decide

  Body archetype is required rather than defaulted, because a wrong or missing one ships
  silently as the female animation library whatever the character is (telegamez#3285). A
  missing preview stack is reported and skipped, never guessed at.

.PARAMETER Asset
  UE asset path, e.g. /Game/Tele. What Unreal opens.

.PARAMETER Name
  Optional. Give this bake a character id different from the asset's own name, so one
  MetaHuman can produce two companions side by side.

  Hair calibration follows the ASSET, not this name: stage 03 embeds it into the sidecar,
  keyed on output_name, which stays the asset's name when --id moves the id. Renaming costs
  nothing.

.PARAMETER Body
  female or male. Required -- see above.

.PARAMETER Force
  Re-bake: replace an existing character of this id rather than refusing.

.PARAMETER NoCapture
  Skip the preview photographs. The model is finished and correct either way; previews are a
  separate step that needs the local stack.

.PARAMETER FromStage
  Resume an already-bootstrapped character at a later stage, by 0-based index into the stage
  list: 0 Unreal assemble, 1 Unreal GLB export, 2 Blender assemble, 3 FBX reweight, 4 webview.

  For a change that only affects a late stage -- a sidecar rule, a material mapping -- stages
  00 and 01 reproduce byte-identical Unreal output and cost about six of every ten minutes.
  This skips them.

  Bootstrap is skipped too, and must be: it deletes and recreates the character directory, so
  running it would destroy the stage 01 output this depends on. The character has to exist
  already, and the id is taken from -Name (or derived from -Asset) rather than read back from
  bootstrap, so it is checked against the directory before anything runs.

.PARAMETER Packages
  Where finished packages go. Defaults to the `characters` folder beside the repo, which is
  what the console uses.

.PARAMETER Stack
  Where the local stack serves the capture page. Default http://localhost:3000.

.EXAMPLE
  ./bake_character.ps1 -Asset /Game/Tele -Name tim -Body male -Force
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Asset,
    [string]$Name,
    [Parameter(Mandatory = $true)][ValidateSet('female', 'male')][string]$Body,
    [switch]$Force,
    [switch]$NoCapture,
    [ValidateRange(0, 4)][int]$FromStage = 0,
    [string]$Packages,
    [string]$Stack = 'http://localhost:3000',
    [string]$Bg = '18385d'
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$PipelineRoot = (Resolve-Path (Join-Path $here '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $PipelineRoot '../..')).Path

# Same charset the publish path validates, and it becomes a directory name and a filename.
if ($Name -and $Name -notmatch '^[A-Za-z0-9._-]{1,64}$') {
    throw "Name must be 1-64 characters of [A-Za-z0-9._-]; got '$Name'."
}

# The order the console runs. Stage 03 is reached through the reweight launcher, which exports
# the GLB as its last act -- the UE glTF exporter alone produces corrupt skin weights, so the
# weights come from the FBX.
$Stages = @(
    @{ Label = 'Stage 00 - Unreal assemble';     Script = 'stages/00-unreal-assemble/tools/run_assemble.ps1' },
    @{ Label = 'Stage 01 - Unreal GLB export';   Script = 'stages/01-unreal-glb-export/tools/run_export.ps1' },
    @{ Label = 'Stage 02 - Blender assemble';    Script = 'stages/02-blender-assemble/tools/run_assemble.ps1' },
    @{ Label = 'FBX reweight (exports the GLB)'; Script = 'tools/run_fbx_reweight.ps1' },
    @{ Label = 'Stage 04 - Webview build';       Script = 'stages/04-webview-build/tools/run_site.ps1' }
)

function Write-Step($label) {
    Write-Host ''
    Write-Host "=== $label" -ForegroundColor Cyan
}

# --- Bootstrap first: it derives the character id, and everything after is keyed on it -------
#
# Unless we are resuming. Bootstrap deletes and recreates the character directory, which is
# where stage 01's output lives -- running it on a resume would delete exactly what the resume
# exists to reuse.

if ($FromStage -gt 0) {
    if ($Name) { $Char = $Name.ToLower() }
    else { $Char = ($Asset -split '/')[-1].ToLower() }
    $charDir = Join-Path $PipelineRoot "characters/$Char"
    if (-not (Test-Path $charDir)) {
        throw "-FromStage $FromStage needs an existing character, and $charDir is not there. Run a full bake first."
    }
    Write-Host ''
    Write-Host "[bake] resuming '$Char' at stage $($Stages[$FromStage].Label)" -ForegroundColor Green
    Write-Host '[bake] bootstrap and earlier stages skipped; their output is reused as it stands.'
} else {

Write-Step 'Bootstrap'
$bootArgs = @((Join-Path $PipelineRoot 'tools/bootstrap_character.py'), '--asset', $Asset, '--body', $Body)
if ($Name)  { $bootArgs += @('--id', $Name) }
if ($Force) { $bootArgs += '--force' }

$bootOut = & python @bootArgs 2>&1
$bootOut | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    if ($bootOut -match 'already exists') {
        throw 'That character has already been baked. Pass -Force to replace it.'
    }
    throw "Bootstrap failed (exit $LASTEXITCODE). Nothing was changed."
}

# Read the id back rather than re-deriving it. bootstrap owns the rule that turns an asset path
# into an id; a second implementation here would be a second rule, free to disagree.
$idLine = $bootOut | Select-String -Pattern '\[bootstrap\]\s*char_id:\s*(\S+)' | Select-Object -First 1
if (-not $idLine) { throw 'Bootstrap did not report a character id - stopping rather than guessing one.' }
$Char = $idLine.Matches[0].Groups[1].Value
Write-Host ''
Write-Host "[bake] character id: $Char" -ForegroundColor Green

}

# --- The five stages -------------------------------------------------------------------------

foreach ($stage in $Stages[$FromStage..($Stages.Count - 1)]) {
    $script = Join-Path $PipelineRoot $stage.Script
    if (-not (Test-Path $script)) { throw "Missing launcher: $($stage.Script)" }
    Write-Step $stage.Label
    & pwsh -NoProfile -File $script -Char $Char
    if ($LASTEXITCODE -ne 0) {
        throw "$($stage.Label) failed (exit $LASTEXITCODE). Stopped here - later stages depend on it."
    }
}

# An exit code says a script finished, not that it produced anything.
$built = Join-Path $PipelineRoot "docs/characters/$Char"
if (-not (Test-Path (Join-Path $built "$Char.glb"))) {
    throw "Stage 04 reported success but $Char.glb is not in $built."
}

# --- Package ----------------------------------------------------------------------------------

Write-Step "Package - $Char"
$pkgArgs = @('-NoProfile', '-File', (Join-Path $here 'package_character.ps1'), '-Char', $Char)
if ($Packages) { $pkgArgs += @('-Packages', $Packages) }
& pwsh @pkgArgs
if ($LASTEXITCODE -ne 0) { throw "Packaging failed (exit $LASTEXITCODE)." }

if ($Packages) { $pkgDir = Join-Path $Packages $Char }
else { $pkgDir = Join-Path (Split-Path -Parent $RepoRoot) "characters/$Char" }

# --- Previews -----------------------------------------------------------------------------------
#
# Skipped rather than failed when the stack is down. The model is finished and correct by this
# point; previews are photographs of it and can be taken later.

if ($NoCapture) {
    Write-Host ''
    Write-Host '[bake] Previews skipped (-NoCapture).'
} else {
    $monorepo = @(
        (Join-Path (Split-Path -Parent $RepoRoot) 'telegamez'),
        (Join-Path (Split-Path -Parent (Split-Path -Parent $RepoRoot)) 'telegamez')
    ) | Where-Object { Test-Path (Join-Path $_ 'scripts/media/capture-companion-previews.mjs') } |
        Select-Object -First 1

    $stackUp = $false
    if ($monorepo) {
        try {
            $r = Invoke-WebRequest -Uri "$Stack/dev/companion-capture" -TimeoutSec 5 -UseBasicParsing
            $stackUp = ($r.StatusCode -eq 200)
        } catch { $stackUp = $false }
    }

    if (-not $monorepo) {
        Write-Host ''
        Write-Host '[bake] Previews skipped: could not find the telegamez checkout.' -ForegroundColor Yellow
    } elseif (-not $stackUp) {
        Write-Host ''
        Write-Host "[bake] Previews skipped: the local stack is not answering at $Stack." -ForegroundColor Yellow
        Write-Host '[bake] The model is finished and correct - start the stack and capture later.'
    } else {
        Write-Step 'Previews - thumbnail, image, video'
        Push-Location $monorepo
        try {
            & node (Join-Path $monorepo 'scripts/media/capture-companion-previews.mjs') --dir $pkgDir --base $Stack --bg $Bg
            if ($LASTEXITCODE -ne 0) {
                Write-Host ''
                Write-Host '[bake] Previews failed. The model is still finished and correct.' -ForegroundColor Yellow
            }
        } finally { Pop-Location }
    }
}

$have = if (Test-Path $pkgDir) { (Get-ChildItem -LiteralPath $pkgDir -Name) -join ', ' } else { '(nothing)' }
Write-Host ''
Write-Host "[bake] package: $pkgDir"
Write-Host "[bake] contains: $have"
Write-Host '[bake] done' -ForegroundColor Green

# Printed last and in one place, so a batch runner can lift the id and location without
# parsing five stages of output.
Write-Host "[bake] RESULT char=$Char dir=$pkgDir"
