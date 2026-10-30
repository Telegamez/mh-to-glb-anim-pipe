<#
.SYNOPSIS
  Start Kimodo Studio on http://localhost:7870.

.DESCRIPTION
  Kimodo + its Llama-3 text encoder take ~18 GB of the 4090's 24 GB, so only ONE process may hold
  the model: this stops the stock Kimodo demo (port 7860) if it is running. Headless UE bakes
  started from the studio run alongside; if one runs out of VRAM, stop the studio first.
#>
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = 'S:\Avatars\metahuman\kimodo\.venv\Scripts\python.exe'

$demo = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue
if ($demo) {
  Write-Host "[studio] stopping the Kimodo demo on :7860 (pid $($demo.OwningProcess)) to free the GPU"
  Stop-Process -Id $demo.OwningProcess -Force
}
$env:PYTHONIOENCODING = 'utf-8'
$env:VCToolsVersion = $null
Start-Process 'http://localhost:7870'
& $Py (Join-Path $Here 'server.py')
