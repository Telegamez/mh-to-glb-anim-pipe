<#
.SYNOPSIS
  Start the Telegames MetaHuman Publisher and open it in your browser.

.DESCRIPTION
  One command, from the repo root. Starts the local publisher interface and opens it.
  Leave the window open while you work; Ctrl+C stops it.

  Nothing to install — it runs on Node's built-in modules and PowerShell.

.PARAMETER Port
  Defaults to 7311. Change it only if something else already has that port.

.PARAMETER Root
  Optional. The publisher remembers the last folder that actually held characters, so this
  is only needed to pin one - a shortcut with -Root set will always win over the memory.


.PARAMETER NoBrowser
  Start the server but don't open a browser.

.EXAMPLE
  ./Start-Publisher.ps1

.EXAMPLE
  ./Start-Publisher.ps1 -Root D:\bakes\ready
#>
[CmdletBinding()]
param(
  [int]$Port = 7311,
  [string]$Root,
  [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

# Works from the repo root and from the shared bundle, where everything sits flat beside this
# script. One launcher rather than two, so the bundle cannot drift from the repo.
$candidates = @(
  (Join-Path $PSScriptRoot '5.7\native-glb\tools\deploy\server.mjs'),
  (Join-Path $PSScriptRoot 'server.mjs')
)
$server = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $server) {
  throw "Cannot find server.mjs - expected beside this script, or under 5.7\native-glb\tools\deploy."
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js is not on PATH. Install it, or open a shell where `node --version` works."
}

# Refuse rather than start a second copy on a port already in use — two publishers sharing a
# port is a confusing way to discover that the one you are looking at is not the one you started.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
  throw "Port $Port is already in use. Either the publisher is already running (open http://127.0.0.1:$Port) or pass -Port with something else."
}

Write-Host ""
Write-Host "  Telegames MetaHuman Publisher" -ForegroundColor Cyan
Write-Host "  http://127.0.0.1:$Port" -ForegroundColor Cyan
if ($Root) { Write-Host "  models: $Root" }
Write-Host ""
Write-Host "  Leave this window open. Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

if (-not $NoBrowser) {
  # Give the listener a moment before the browser asks for the page, or the first load
  # races the server and shows a connection error that looks like a broken install.
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

# Runs in the foreground so its output is right here and Ctrl+C stops it.
# Only pass --root when one was given. Otherwise the server uses the folder it remembers,
# and passing a default here would silently override it on every launch.
$serverArgs = @($server, '--port', $Port)
if ($Root) { $serverArgs += @('--root', $Root) }
& node $serverArgs
