<#
.SYNOPSIS
  Upload a character's three preview assets to the platform's admin API.

.DESCRIPTION
  The other half of publishing an avatar. `publish_to_cdn.ps1` puts the model directory in the
  bucket and returns the key; this fills the three preview columns that decide whether the
  avatar shows a picture or a dead image in the catalogue:

      thumbnail.png     -> avatars.thumbnail_url
      imagePreview.jpg  -> avatars.image_preview_url
      videoPreview.mp4  -> avatars.video_preview_url

  It posts to `POST /api/admin/avatars/smart-upload`, which writes each asset to its own slot
  and sets only that asset's column. Unlike the model publish, this one DOES update the row -
  the route owns those columns and there is no separate step to point them anywhere.

  A side effect worth knowing: any asset upload also sets `status = 'active'`. A row created
  through the admin API starts as 'processing', so completing an avatar's previews is also what
  takes it out of that state.

  THE MODEL IS NEVER SENT. `smart-upload` accepts a `model` field, and supplying it would write
  `model_path = /avatars/<id>/model.glb` - the flat legacy slot - silently replacing the
  content-addressed key the directory publish produced and reverting the character. There is no
  parameter here that can send it, which is the point.

.PARAMETER Char
  Character id, e.g. `telly`. Its directory is taken from -Source.

.PARAMETER AvatarId
  The `avatars` row UUID these previews belong to.

.PARAMETER ApiBase
  Admin API base, e.g. https://api.panda.telegames.ai

.PARAMETER Token
  Admin bearer token. Falls back to $env:TG_ADMIN_TOKEN.

.PARAMETER Source
  Directory holding the three preview files. Defaults to the packages folder the bake console
  writes to.

.EXAMPLE
  ./publish_previews.ps1 -Char telly -AvatarId 8e671d15-... -ApiBase https://api.panda.telegames.ai
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Char,
  [Parameter(Mandatory = $true)][string]$AvatarId,
  [Parameter(Mandatory = $true)][string]$ApiBase,
  [string]$Token = $env:TG_ADMIN_TOKEN,
  [string]$Source
)

$ErrorActionPreference = 'Stop'

if (-not $Source) {
  $Source = Join-Path 'S:\Avatars\metahuman\characters' $Char
}
if (-not (Test-Path $Source)) { throw "Source directory not found: $Source" }

# All three or none. A catalogue entry with a thumbnail but no video is a half-finished avatar
# that looks finished, and the missing one is only noticed by whoever opens the card.
$assets = @(
  @{ Field = 'thumbnail';    File = 'thumbnail.png';    Type = 'image/png' }
  @{ Field = 'imagePreview'; File = 'imagePreview.jpg'; Type = 'image/jpeg' }
  @{ Field = 'videoPreview'; File = 'videoPreview.mp4'; Type = 'video/mp4' }
)

$missing = @()
foreach ($a in $assets) {
  if (-not (Test-Path (Join-Path $Source $a.File))) { $missing += $a.File }
}
if ($missing.Count -gt 0) {
  throw "Missing preview file(s) in ${Source}: $($missing -join ', '). Capture them first."
}

$totalKb = [math]::Round((
  $assets | ForEach-Object { (Get-Item (Join-Path $Source $_.File)).Length } | Measure-Object -Sum
).Sum / 1KB, 1)
Write-Host "[publish_previews] $Char -> avatar $AvatarId"
Write-Host "[publish_previews] 3 files, $totalKb KB"

Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [TimeSpan]::FromMinutes(10)
if ($Token) {
  $client.DefaultRequestHeaders.Authorization =
    [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Token)
}

$content = [System.Net.Http.MultipartFormDataContent]::new()
$streams = @()

<#
  Content-Disposition is set explicitly with QUOTED values and no `filename*`, for the same
  reason publish_to_cdn.ps1 does it: `MultipartFormDataContent.Add()` writes RFC 2388-style
  unquoted parameters, and the API's `request.formData()` is undici, which follows RFC 7578 and
  rejects an unquoted `name`. Every request fails with "Failed to parse body as FormData" at any
  size. Do not replace these with plain .Add() calls.
#>
function Add-QuotedPart {
  param($Container, $Part, [string]$Name, [string]$FileName)
  $cd = [System.Net.Http.Headers.ContentDispositionHeaderValue]::new('form-data')
  $cd.Name = '"' + $Name + '"'
  if ($FileName) { $cd.FileName = '"' + $FileName + '"' }   # FileNameStar left unset on purpose
  $Part.Headers.ContentDisposition = $cd
  $Container.Add($Part)
}

try {
  Add-QuotedPart $content ([System.Net.Http.StringContent]::new($AvatarId)) 'avatarId'

  foreach ($a in $assets) {
    $path = Join-Path $Source $a.File
    $stream = [System.IO.File]::OpenRead($path)
    $streams += $stream
    $part = [System.Net.Http.StreamContent]::new($stream)
    # The route reads file.type for the stored content type and falls back per field, so set it
    # rather than letting a video be stored as octet-stream and refuse to play in a card.
    $part.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::new($a.Type)
    Add-QuotedPart $content $part $a.Field $a.File
  }

  $url = "$($ApiBase.TrimEnd('/'))/api/admin/avatars/smart-upload"
  Write-Host "[publish_previews] POST $url"
  $response = $client.PostAsync($url, $content).GetAwaiter().GetResult()
  $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()

  if (-not $response.IsSuccessStatusCode) {
    throw "Preview upload failed ($([int]$response.StatusCode)): $body"
  }

  $parsed = $body | ConvertFrom-Json
  $avatar = $parsed.data.avatar
  Write-Host "[publish_previews] OK"
  Write-Host "[publish_previews]   thumbnail_url     = $($avatar.thumbnailUrl)"
  Write-Host "[publish_previews]   image_preview_url = $($avatar.imagePreviewUrl)"
  Write-Host "[publish_previews]   video_preview_url = $($avatar.videoPreviewUrl)"
  Write-Host "[publish_previews]   status            = $($avatar.status)"
  Write-Host ""
  Write-Host "These columns are written by the route - no SQL needed for previews."
}
finally {
  foreach ($s in $streams) { $s.Dispose() }
  $content.Dispose()
  $client.Dispose()
}
