<#
.SYNOPSIS
  Refuse to start an Unreal stage while another Unreal editor is running.

.DESCRIPTION
  A second editor is not merely untidy — it makes a stage fail seven minutes in, having
  already done all the work, and the log gives almost no clue why.

  What happens: the running editor holds Unreal's Remote Control port (30010). The stage's
  headless UnrealEditor-Cmd then cannot bind it and logs

      LogHttpListener: Error: HttpListener unable to bind to 127.0.0.1:30010

  which is the ONLY error in UE's summary — everything else is warnings. But UE exits 1 for
  any error in that summary, and the launchers take the exit code as the verdict. So a
  complete, correct export is reported as a failure and the batch stops.

  Observed on tele4, 2026-09-02: stage 01 wrote all 5 GLBs, both FBXs and 14 textures,
  logged "Python script executed successfully", and was still called a failure. The culprit
  was an editor a person had left open seven hours earlier.

  Checking the exit code is right; UE just gives an unhelpful one here. Catching the cause
  before the work happens is better than second-guessing the result afterwards.

.PARAMETER Stage
  Label for the message, e.g. "stage00".

.PARAMETER Allow
  Skip the check. Also honoured via TG_ALLOW_UNREAL_RUNNING=1, for the case where someone
  knowingly wants both — accepting that the stage may fail this way.
#>
function Assert-NoUnrealRunning {
    [CmdletBinding()]
    param(
        [string]$Stage = 'stage',
        [switch]$Allow
    )

    if ($Allow -or $env:TG_ALLOW_UNREAL_RUNNING -eq '1') {
        Write-Host "[$Stage] skipping the Unreal-already-running check (override set)"
        return
    }

    # Both the GUI editor and the -Cmd variant hold the port. Match either.
    $running = @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like 'UnrealEditor*' })

    if ($running.Count -eq 0) { return }

    $lines = foreach ($p in $running) {
        $started = try { $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss') } catch { 'unknown' }
        "    PID $($p.Id)  $($p.ProcessName)  started $started"
    }

    # Name the port holder explicitly when we can see it — that is the actual mechanism, and
    # an operator with two editors open needs to know which one matters.
    $holder = ''
    try {
        $conn = Get-NetTCPConnection -LocalPort 30010 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) { $holder = "  Port 30010 is held by PID $($conn.OwningProcess)." }
    } catch { }

    throw @"
[$Stage] An Unreal editor is already running, so this stage would fail.

$($lines -join "`n")
$holder

  It holds Unreal's Remote Control port (30010). This stage's headless editor cannot bind
  it, and UE then exits 1 even when the export itself succeeds — costing the full build
  time and reporting a failure that is not one.

  Close the editor and run again. Save your work first: this refuses rather than killing
  anything, because that process may be a session someone is using.

  To proceed anyway, pass -AllowUnrealRunning or set TG_ALLOW_UNREAL_RUNNING=1.
"@
}
