<#
.SYNOPSIS
  Bake a whole roster of MetaHumans, unattended.

.DESCRIPTION
  Runs bake_character.ps1 once per entry in a plan file, in order, one at a time. Eight
  characters is most of a working day of Unreal and Blender; this exists so that day does not
  need a person sitting in front of it clicking Bake.

  ## One at a time, and why that is not a limitation to work around

  The stages read and write the same character directory, and stage 01 drives a live Unreal
  editor over its remote-control port. Two bakes at once is not slow-but-fine, it is two
  editors fighting over one port and a character nobody can explain afterwards. So this is
  strictly sequential, and it refuses to start at all if an Unreal editor is already open --
  a stale editor holding port 30010 is what made a successful tele4 export report failure.

  ## Failure does not stop the roster

  A character that fails is recorded and the batch moves on. Eight bakes is long enough that
  stopping on the first failure means finding out hours later that seven of them never ran.
  Everything that failed is listed at the end, with its log, and can be re-run on its own.
  Pass -StopOnFailure for the opposite.

  ## Resume

  By default a character whose package already exists is skipped, so an interrupted batch can
  be restarted and picks up where it stopped. -Force re-bakes everything, replacing what is
  there.

.PARAMETER Plan
  JSON file listing the characters. Defaults to batch_bake.plan.json beside this script.

.PARAMETER Only
  Bake just these names, from the plan. Comma-separated.

.PARAMETER DryRun
  Print exactly what would be baked, in order, with every precondition checked -- and bake
  nothing. Cheap to run before committing several hours to a batch.

.PARAMETER Force
  Re-bake characters that already have a package, instead of skipping them.

.PARAMETER NoCapture
  Skip previews for every character. Useful when the local stack is not running; the models
  are still finished and correct, and previews can be captured afterwards from the console.

.PARAMETER StopOnFailure
  Stop at the first failure rather than recording it and continuing.

.PARAMETER LogDir
  Where per-character logs go. Defaults to a timestamped folder under logs/batch-bake/.

.EXAMPLE
  ./batch_bake.ps1 -DryRun

.EXAMPLE
  ./batch_bake.ps1

.EXAMPLE
  ./batch_bake.ps1 -Only clara,roy -Force
#>
[CmdletBinding()]
param(
    [string]$Plan,
    [string[]]$Only,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$NoCapture,
    [switch]$StopOnFailure,
    [string]$Packages,
    [string]$Stack = 'http://localhost:3000',
    [string]$LogDir,
    [switch]$AllowUnrealRunning
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$PipelineRoot = (Resolve-Path (Join-Path $here '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $PipelineRoot '../..')).Path

if (-not $Plan) { $Plan = Join-Path $here 'batch_bake.plan.json' }
if (-not (Test-Path $Plan)) { throw "Plan not found: $Plan" }
if (-not $Packages) { $Packages = Join-Path (Split-Path -Parent $RepoRoot) 'characters' }

# NOT $plan: the parameter is [string]$Plan, and PowerShell keeps that type constraint on the
# variable, so assigning the parsed document to it -- names differ only in case, which
# PowerShell does not distinguish -- silently stringifies the object. `.characters` then reads
# $null and the plan validates as one empty character.
$planDoc = Get-Content -LiteralPath $Plan -Raw | ConvertFrom-Json
$entries = @($planDoc.characters)
if (-not $entries.Count) { throw "Plan has no characters: $Plan" }

if ($Only) {
    # Split on commas as well as taking a real array. `pwsh -File` passes every argument as a
    # plain string, so `-Only clara,roy` arrives as the single element "clara,roy" and matches
    # nothing -- which reads as "those characters are not in the plan" when they are.
    $want = $Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLower() } |
            Where-Object { $_ }
    $entries = @($entries | Where-Object { $want -contains $_.name.ToLower() })
    $missing = @($want | Where-Object { $n = $_; -not ($planDoc.characters | Where-Object { $_.name.ToLower() -eq $n }) })
    if ($missing.Count) { throw "Not in the plan: $($missing -join ', ')" }
}

# --- Validate the whole plan before baking any of it -----------------------------------------
#
# All of it, not the first bad entry: finding out at character six that its body is misspelled,
# after fifty minutes of Unreal, is the failure this check exists to prevent.

$problems = @()
$seen = @{}
foreach ($e in $entries) {
    $label = if ($e.name) { $e.name } else { '(unnamed)' }
    if (-not $e.asset) { $problems += "$label : no asset" }
    if (-not $e.name)  { $problems += "$($e.asset) : no name" }
    elseif ($e.name -notmatch '^[A-Za-z0-9._-]{1,64}$') {
        $problems += "$label : name must be 1-64 chars of [A-Za-z0-9._-]"
    }
    if ($e.body -ne 'female' -and $e.body -ne 'male') {
        # Refused rather than defaulted. The value decides the animation library and a wrong
        # one ships silently as female whatever the character is (telegamez#3285).
        $problems += "$label : body must be female or male, got '$($e.body)'"
    }
    if ($e.name) {
        $k = $e.name.ToLower()
        if ($seen.ContainsKey($k)) { $problems += "$label : name used twice - the second would overwrite the first" }
        $seen[$k] = $true
    }
}
if ($problems.Count) {
    Write-Host ''
    Write-Host 'Plan is not runnable:' -ForegroundColor Red
    $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 2
}

# --- Preconditions ------------------------------------------------------------------------------

$baker = Join-Path $here 'bake_character.ps1'
if (-not (Test-Path $baker)) { throw "bake_character.ps1 not found beside this script." }

# Stage 01 drives a live editor over port 30010. A second one already open takes that port, and
# the export then fails after doing all its work -- or worse, succeeds against the wrong editor.
$guard = Join-Path $here 'Assert-NoUnrealRunning.ps1'
if ((Test-Path $guard) -and -not $AllowUnrealRunning) {
    & pwsh -NoProfile -File $guard
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host 'Close the editor and run again, or pass -AllowUnrealRunning if you are sure.' -ForegroundColor Yellow
        exit 1
    }
}

$stackUp = $false
try {
    $r = Invoke-WebRequest -Uri "$Stack/dev/companion-capture" -TimeoutSec 5 -UseBasicParsing
    $stackUp = ($r.StatusCode -eq 200)
} catch { $stackUp = $false }

if (-not $LogDir) {
    $LogDir = Join-Path $PipelineRoot ("logs/batch-bake/" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}

# --- What is about to happen --------------------------------------------------------------------

$queue = @()
foreach ($e in $entries) {
    $pkg = Join-Path $Packages $e.name
    $done = Test-Path (Join-Path $pkg "$($e.name).glb")
    $queue += [pscustomobject]@{
        Asset = $e.asset; Name = $e.name; Body = $e.body
        Skip  = ($done -and -not $Force)
        Note  = if ($done -and -not $Force) { 'already baked - skipping (use -Force to replace)' }
                elseif ($done) { 're-bake, replaces the existing package' }
                else { '' }
    }
}

Write-Host ''
Write-Host "  Batch bake - $($queue.Count) character(s)" -ForegroundColor Cyan
Write-Host "  plan      $Plan"
Write-Host "  packages  $Packages"
Write-Host "  logs      $LogDir"
Write-Host ("  previews  " + $(if ($NoCapture) { 'skipped (-NoCapture)' }
                              elseif ($stackUp) { "yes, via $Stack" }
                              else { "NO - the stack is not answering at $Stack" })) `
           -ForegroundColor $(if ($NoCapture -or $stackUp) { 'Gray' } else { 'Yellow' })
Write-Host ''
foreach ($q in $queue) {
    $mark = if ($q.Skip) { ' -- ' } else { ' => ' }
    Write-Host ("   {0,-14}{1}{2,-10} {3,-7} {4}" -f $q.Asset, $mark, $q.Name, $q.Body, $q.Note)
}
Write-Host ''

$todo = @($queue | Where-Object { -not $_.Skip })
if (-not $todo.Count) {
    Write-Host 'Nothing to do - everything in the plan is already baked. Use -Force to re-bake.' -ForegroundColor Yellow
    exit 0
}

if ($DryRun) {
    Write-Host "Dry run: nothing was baked. $($todo.Count) character(s) would run, in the order above." -ForegroundColor Cyan
    exit 0
}

if (-not $NoCapture -and -not $stackUp) {
    Write-Host 'Previews will be skipped for every character. The models will still be finished' -ForegroundColor Yellow
    Write-Host 'and correct; previews can be captured afterwards from the console.' -ForegroundColor Yellow
    Write-Host ''
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --- Run ------------------------------------------------------------------------------------------

$results = @()
$started = Get-Date
$n = 0

foreach ($q in $todo) {
    $n++
    $t0 = Get-Date
    $log = Join-Path $LogDir "$($q.Name).log"

    Write-Host ''
    Write-Host ('-' * 78) -ForegroundColor DarkGray
    Write-Host ("[{0}/{1}] {2} -> {3}   ({4})" -f $n, $todo.Count, $q.Asset, $q.Name, $q.Body) -ForegroundColor Cyan
    Write-Host "        log: $log" -ForegroundColor DarkGray
    Write-Host ('-' * 78) -ForegroundColor DarkGray

    $bakeArgs = @('-NoProfile', '-File', $baker,
                  '-Asset', $q.Asset, '-Name', $q.Name, '-Body', $q.Body,
                  '-Packages', $Packages, '-Stack', $Stack, '-Force')
    if ($NoCapture -or -not $stackUp) { $bakeArgs += '-NoCapture' }

    # Teed rather than swallowed: an unattended batch still has to be watchable, and a failure
    # hours in needs its own log rather than a scroll back through eight characters of output.
    & pwsh @bakeArgs 2>&1 | Tee-Object -FilePath $log
    $code = $LASTEXITCODE
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)

    $pkg = Join-Path $Packages $q.Name
    $glb = Join-Path $pkg "$($q.Name).glb"
    # The exit code says the script finished. Whether it produced a model is a separate
    # question, and the only one that matters here.
    $made = Test-Path $glb
    $mb = if ($made) {
        [math]::Round((Get-ChildItem -LiteralPath $pkg -Recurse -File |
                       Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    } else { 0 }

    $tuned = $null
    $sidecar = Join-Path $pkg 'mh_materials.json'
    if (Test-Path $sidecar) {
        try {
            $sc = Get-Content -LiteralPath $sidecar -Raw | ConvertFrom-Json
            if ($sc.hair_tuning_source) { $tuned = $sc.hair_tuning_source.tuned_as }
        } catch { }
    }

    $previews = @('thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4') |
                Where-Object { Test-Path (Join-Path $pkg $_) }

    $results += [pscustomobject]@{
        Name = $q.Name; Asset = $q.Asset; Body = $q.Body
        Ok = ($code -eq 0 -and $made); Code = $code; Minutes = $mins; Mb = $mb
        TunedAs = $tuned; Previews = $previews.Count; Log = $log
    }

    if ($code -eq 0 -and $made) {
        Write-Host ''
        Write-Host ("[batch] {0} done in {1} min, {2} MB" -f $q.Name, $mins, $mb) -ForegroundColor Green
    } else {
        Write-Host ''
        Write-Host ("[batch] {0} FAILED (exit {1}) after {2} min - see {3}" -f $q.Name, $code, $mins, $log) -ForegroundColor Red
        if ($StopOnFailure) {
            Write-Host '[batch] -StopOnFailure: stopping here.' -ForegroundColor Yellow
            break
        }
        Write-Host '[batch] carrying on with the rest of the roster.' -ForegroundColor Yellow
    }
}

# --- Report ------------------------------------------------------------------------------------------

$elapsed = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
$ok = @($results | Where-Object Ok)
$bad = @($results | Where-Object { -not $_.Ok })

Write-Host ''
Write-Host ('=' * 78) -ForegroundColor DarkGray
Write-Host ("  {0} of {1} baked in {2} min" -f $ok.Count, $results.Count, $elapsed) -ForegroundColor Cyan
Write-Host ('=' * 78) -ForegroundColor DarkGray
Write-Host ''
Write-Host ("   {0,-10} {1,-8} {2,7} {3,-10} {4,-8} {5}" -f 'name', 'body', 'MB', 'hair', 'previews', 'result')
foreach ($r in $results) {
    $hair = if ($r.TunedAs) { $r.TunedAs } else { 'default' }
    $prev = "$($r.Previews)/3"
    $state = if ($r.Ok) { 'ok' } else { "FAILED (exit $($r.Code))" }
    $colour = if ($r.Ok) { if ($r.TunedAs) { 'Gray' } else { 'Yellow' } } else { 'Red' }
    Write-Host ("   {0,-10} {1,-8} {2,7} {3,-10} {4,-8} {5}" -f $r.Name, $r.Body, $r.Mb, $hair, $prev, $state) -ForegroundColor $colour
}

# Said plainly rather than left to be read off the table. A companion with default hair renders
# straw instead of a tuned groom and nothing at runtime reports it, so the one moment it can be
# noticed is here.
$untuned = @($results | Where-Object { $_.Ok -and -not $_.TunedAs })
if ($untuned.Count) {
    Write-Host ''
    Write-Host ("   hair: {0} baked with DEFAULT calibration - {1}" -f $untuned.Count, (($untuned.Name) -join ', ')) -ForegroundColor Yellow
    Write-Host '   Their source MetaHumans have no HAIR_PROFILES entry, so there was nothing to' -ForegroundColor Yellow
    Write-Host '   embed. Not caused by renaming them; tuning one means adding it to hairProfile.ts.' -ForegroundColor Yellow
}

$noPreviews = @($results | Where-Object { $_.Ok -and $_.Previews -lt 3 })
if ($noPreviews.Count) {
    Write-Host ''
    Write-Host ("   previews: {0} incomplete - {1}" -f $noPreviews.Count, (($noPreviews.Name) -join ', ')) -ForegroundColor Yellow
    Write-Host '   The models are finished and correct. Start the local stack, then tick them in' -ForegroundColor Yellow
    Write-Host '   the console and press "Previews only".' -ForegroundColor Yellow
}

if ($bad.Count) {
    Write-Host ''
    Write-Host '   failed:' -ForegroundColor Red
    foreach ($r in $bad) { Write-Host ("     {0,-10} {1}" -f $r.Name, $r.Log) -ForegroundColor Red }
    Write-Host ''
    Write-Host ("   Re-run just these:  ./batch_bake.ps1 -Only {0} -Force" -f (($bad.Name) -join ',')) -ForegroundColor Yellow
}

Write-Host ''
Write-Host "   logs: $LogDir"
Write-Host ''

exit $(if ($bad.Count) { 1 } else { 0 })
