<#
.SYNOPSIS
  Start the MetaHuman bake console and open it in your browser.

.DESCRIPTION
  One command, one page: bake a MetaHuman out of Unreal, photograph it, make its ktx2 build,
  and leave a finished package for the Publisher. The grid at the top shows where every
  character has got to, so you can see what is left rather than remember it.

  Publishing is not on this page any more (telegamez#4632 phase 2): the platform's avatar
  routes this console used to call are retired, and a package reaches panda through
  Start-Publisher.ps1, by the character id in its character.json.

  Leave the window open while you work; Ctrl+C stops it.

  Making a character needs Unreal, Blender and the local stack on this machine.

.PARAMETER Port
  Defaults to 7312. The Publisher uses 7311, so both can run at once.

.PARAMETER Root
  The folder holding finished character packages. Remembered between runs once you pick one,
  so this is only needed to pin a different folder.

.PARAMETER Stack
  Where the local stack serves the capture page. Defaults to http://localhost:3000.

.PARAMETER NoBrowser
  Start the server but don't open a browser.

.PARAMETER MeshoptConfirmed
  Ignored. ktx2 is the only build (owner decision 2026-10-08; meshopt was removed as a build):
  every bake ends with a ktx2 build and the Publisher sends that build, making or refreshing it
  first when it has to. The switch stays so an old shortcut still starts the console.

.EXAMPLE
  ./Start-Console.ps1
#>
[CmdletBinding()]
param(
  [int]$Port = 7312,
  [string]$Root,
  [string]$Stack,
  [switch]$NoBrowser,
  [switch]$MeshoptConfirmed
)

$ErrorActionPreference = 'Stop'

# Works from the repo root and from a flat bundle, where server.mjs sits beside this script.
$candidates = @(
  (Join-Path $PSScriptRoot '5.7\native-glb\tools\console\server.mjs'),
  (Join-Path $PSScriptRoot 'server.mjs')
)
$server = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $server) {
  throw "Cannot find server.mjs - expected beside this script, or under 5.7\native-glb\tools\console."
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
Write-Host "  MetaHuman bake" -ForegroundColor Cyan
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

if ($MeshoptConfirmed) { Write-Host '  -MeshoptConfirmed is ignored: ktx2 is the only build now (meshopt was removed 2026-10-08).' -ForegroundColor DarkGray }

# Foreground, so its output is right here and Ctrl+C stops it.
$serverArgs = @($server, '--port', $Port)
if ($Root)  { $serverArgs += @('--root', $Root) }
if ($Stack) { $serverArgs += @('--stack', $Stack) }
& node $serverArgs
