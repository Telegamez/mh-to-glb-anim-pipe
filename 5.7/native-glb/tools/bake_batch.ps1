<#
.SYNOPSIS
  Run stages 00-04 for one or more characters, sequentially.

.DESCRIPTION
  Stages report intent, not outcome — several have reported DONE while
  persisting nothing. So this checks each stage's launcher exit code AND
  stops that character on the first failure rather than carrying a broken
  intermediate into the next stage. Other characters still run.

  Per-character logs land in _logs/<char>-<stage>.log.

.EXAMPLE
  ./tools/bake_batch.ps1 -Chars tele,telie,telee,tele1
#>
param(
  [Parameter(Mandatory = $true)][string[]] $Chars,
  [string[]] $Stages = @('00-unreal-assemble', '01-unreal-glb-export',
                         '02-blender-assemble', '03-export-to-glb',
                         '04-webview-build')
)

$ErrorActionPreference = 'Continue'
$Root    = Split-Path -Parent $PSScriptRoot
$LogDir  = Join-Path $Root '_logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$results = @()

foreach ($char in $Chars) {
  Write-Host "======== $char ========"
  $failed = $null

  foreach ($stage in $Stages) {
    $stageDir = Join-Path $Root "stages\$stage"
    $runner = Get-ChildItem (Join-Path $stageDir 'tools\run_*.ps1') -ErrorAction SilentlyContinue |
              Select-Object -First 1
    if (-not $runner) {
      Write-Host "  [$stage] no runner found - skipping"
      continue
    }

    $log = Join-Path $LogDir "$char-$stage.log"
    $started = Get-Date
    Write-Host ("  [{0}] start {1:HH:mm:ss} -> {2}" -f $stage, $started, $log)

    Push-Location $stageDir
    try {
      & $runner.FullName -Char $char *>&1 | Tee-Object -FilePath $log | Out-Null
      $code = $LASTEXITCODE
    } catch {
      $_ | Out-String | Add-Content -Path $log
      $code = 1
    } finally {
      Pop-Location
    }

    $mins = [int]((Get-Date) - $started).TotalMinutes
    if ($code -ne 0) {
      Write-Host "  [$stage] FAILED (exit $code) after ${mins}m - stopping $char"
      $failed = $stage
      break
    }
    Write-Host "  [$stage] ok (${mins}m)"
  }

  $results += [pscustomobject]@{ Character = $char; FailedAt = $failed }
}

Write-Host "`n======== summary ========"
foreach ($r in $results) {
  if ($r.FailedAt) { Write-Host ("{0,-8} FAILED at {1}" -f $r.Character, $r.FailedAt) }
  else             { Write-Host ("{0,-8} all stages ok" -f $r.Character) }
}
if ($results | Where-Object { $_.FailedAt }) { exit 1 }
exit 0
