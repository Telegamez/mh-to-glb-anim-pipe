<#
.SYNOPSIS
  Start the MetaHuman Bake console and open it in your browser.

.DESCRIPTION
  One command, from the repo root. Pick a MetaHuman saved in Unreal, press Bake, and it runs
  the pipeline and the preview capture, leaving a finished package ready to publish.

  Leave the window open while you work; Ctrl+C stops it.

  Nothing to install beyond what the pipeline already needs — Unreal, Blender, Python and
  Node. Previews also need the local stack running; the console tells you if it isn't.

.PARAMETER Port
  Defaults to 7312. The publisher uses 7311, so both can run at once.

.PARAMETER Stack
  Where the local stack serves the capture page. Defaults to http://localhost:3000.

.PARAMETER NoBrowser
  Start the server but don't open a browser.

.EXAMPLE
  ./Start-Bake.ps1
#>
[CmdletBinding()]
param(
  [int]$Port = 7312,
  [string]$Stack,
  [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

# Works from the repo root and from a flat bundle, where server.mjs sits beside this script.
$candidates = @(
  (Join-Path $PSScriptRoot '5.7\native-glb\tools\bake\server.mjs'),
  (Join-Path $PSScriptRoot 'server.mjs')
)
$server = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $server) {
  throw "Cannot find server.mjs - expected beside this script, or under 5.7\native-glb\tools\bake."
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js is not on PATH. Install it, or open a shell where `node --version` works."
}

# Refuse rather than start a second copy on a busy port: two consoles sharing a port is a
# confusing way to find out the one you are looking at is not the one you started.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
  throw "Port $Port is already in use. Either the console is already running (open http://127.0.0.1:$Port) or pass -Port with something else."
}

Write-Host ""
Write-Host "  MetaHuman Bake" -ForegroundColor Cyan
Write-Host "  http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Leave this window open. Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

if (-not $NoBrowser) {
  # Give the listener a moment before the browser asks for the page, or the first load races
  # the server and shows a connection error that looks like a broken install.
  Start-Job -ScriptBlock {
    param($p)
    for ($i = 0; $i -lt 40; $i++) {
      Start-Sleep -Milliseconds 250
      if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) {
        Start-Process "http://127.0.0.1:$p"
        return
      }
    }
  } -ArgumentList $Port | Out-Null
}

# Foreground, so its output is right here and Ctrl+C stops it.
$serverArgs = @($server, '--port', $Port)
if ($Stack) { $serverArgs += @('--stack', $Stack) }
& node $serverArgs
