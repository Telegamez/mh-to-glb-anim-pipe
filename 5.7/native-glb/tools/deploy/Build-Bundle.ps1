<#
.SYNOPSIS
  Build the shareable Telegames MetaHuman Publisher zip.

.DESCRIPTION
  Produces a self-contained folder anyone can be sent — no repo, no install, no Node modules.
  They unzip it and run `Start-Publisher.ps1`.

  It copies the SAME files the repo runs, flat. Both the launcher and the server search for
  their neighbours in either layout, so there is one implementation rather than a repo copy and
  a bundle copy that quietly drift until the thing you posted to somebody is a version nobody
  is testing.

  The zip is written to the repo so it can be grabbed and sent without building anything.

.PARAMETER OutDir
  Where to write the zip. Defaults to `dist/` at the repo root.

.EXAMPLE
  ./Build-Bundle.ps1
#>
[CmdletBinding()]
param([string]$OutDir)

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$repo = Resolve-Path (Join-Path $here '..\..\..\..')
if (-not $OutDir) { $OutDir = Join-Path $repo 'dist' }

# Everything the publisher needs at runtime, and nothing else. Listed explicitly rather than
# globbed: a bundle that picks up whatever happens to be in a directory is a bundle that one
# day ships a scratch file or a credential someone left there.
$files = @(
  @{ From = Join-Path $repo 'Start-Publisher.ps1';     As = 'Start-Publisher.ps1' }
  @{ From = Join-Path $repo 'PUBLISHER.md';            As = 'README.md' }
  @{ From = Join-Path $here 'server.mjs';              As = 'server.mjs' }
  @{ From = Join-Path $here 'console.html';            As = 'console.html' }
  @{ From = Join-Path $here 'Test-CompanionsLive.ps1'; As = 'Test-CompanionsLive.ps1' }
  @{ From = Join-Path $here '..\publish_to_cdn.ps1';   As = 'publish_to_cdn.ps1' }
)

foreach ($f in $files) {
  if (-not (Test-Path $f.From)) { throw "Missing: $($f.From)" }
}

$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("tgpub-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$inner = Join-Path $stage 'TelegamesPublisher'
New-Item -ItemType Directory -Path $inner -Force | Out-Null

foreach ($f in $files) {
  Copy-Item $f.From (Join-Path $inner $f.As) -Force
  Write-Host "  + $($f.As)"
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$zip = Join-Path $OutDir 'TelegamesPublisher.zip'
if (Test-Path $zip) { Remove-Item $zip -Force }

Compress-Archive -Path $inner -DestinationPath $zip -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force

$kb = [math]::Round((Get-Item $zip).Length / 1KB, 1)
Write-Host ""
Write-Host "  $zip  ($kb KB)" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Send it. They unzip and run Start-Publisher.ps1." -ForegroundColor DarkGray
Write-Host "  Needs Node.js and PowerShell 7 on their machine; nothing else." -ForegroundColor DarkGray
