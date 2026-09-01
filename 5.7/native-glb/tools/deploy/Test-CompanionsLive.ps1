<#
.SYNOPSIS
  Verify that companions are actually live in an environment — assets, rows and previews.

.DESCRIPTION
  Answers one question honestly: *would a person opening a room see this companion?*

  Every check here exists because its absence has already looked like success. A publish
  reports the files it wrote and says nothing about whether the registry points at them;
  a registry row can name a key that was never uploaded; and an S3 403 for a missing
  object is indistinguishable at a glance from a permissions failure. So this reads the
  rows from the environment's own API and then fetches what they name.

  Read-only. Safe to run at any time, against any environment, as often as you like.

  Six checks per companion:

    row        the avatar row exists and is active
    published  model_path is a real publish, not the seeder's placeholder
    model      the GLB actually fetches
    manifest   manifest.json fetches beside it — the loader's non-404 path
    previews   thumbnail / imagePreview / videoPreview URLs are set
    assets     each preview URL actually fetches

  A companion can pass the first four and still show blank cards; that is exactly the
  state panda was in, and why previews are checked separately rather than assumed.

.PARAMETER ApiBase
  Admin API for the environment, e.g. https://api.panda.telegames.ai

.PARAMETER Token
  Admin session cookie value. Falls back to $env:TG_ADMIN_TOKEN. Get it by signing in
  at <ApiBase>/api/auth/signin and copying `__Secure-authjs.session-token`. Check the
  cookie's domain matches the environment you mean — a cookie from the bare host is a
  PRODUCTION session.

.PARAMETER Slugs
  Companion slugs to check. Defaults to the six MetaHumans.

.EXAMPLE
  $env:TG_ADMIN_TOKEN = "<value>"
  ./Test-CompanionsLive.ps1 -ApiBase https://api.panda.telegames.ai

.EXAMPLE
  # Assets only, no admin API — useful before a token exists.
  ./Test-CompanionsLive.ps1 -CdnBase https://telegames-assets-local.s3.us-west-2.amazonaws.com `
                            -ModelKeys avatars/8e67.../bf64.../telly.glb
#>
[CmdletBinding(DefaultParameterSetName = 'Api')]
param(
  [Parameter(Mandatory = $true, ParameterSetName = 'Api')][string]$ApiBase,
  [Parameter(ParameterSetName = 'Api')][string]$Token = $env:TG_ADMIN_TOKEN,
  [Parameter(ParameterSetName = 'Api')][string[]]$Slugs = @('mh-telly','mh-tele','mh-telie','mh-telee','mh-tele1','mh-tele2'),

  [Parameter(Mandatory = $true, ParameterSetName = 'Assets')][string]$CdnBase,
  [Parameter(Mandatory = $true, ParameterSetName = 'Assets')][string[]]$ModelKeys
)

$ErrorActionPreference = 'Stop'

# ---- helpers ---------------------------------------------------------------------------

function Get-UrlStatus {
  <#
    A HEAD would be cheaper, but S3 answers HEAD and GET differently for some objects and
    the browser issues a GET. Ask the question the way the client asks it, and read only
    the status - `-Method Get` with no body handling still streams, so cap it.
  #>
  param([string]$Url)
  try {
    $r = Invoke-WebRequest -Uri $Url -Method Head -SkipHttpErrorCheck -TimeoutSec 30
    return [int]$r.StatusCode
  } catch {
    if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
    return -1
  }
}

function Test-Published {
  # Mirrors PUBLISHED_PATH in packages/shared/db/src/seed-metahuman-roster.ts. A path the
  # seeder wrote (`models/<id>/<id>.glb`) is NOT a publish, however plausible it looks.
  #
  # Not anchored, deliberately - the same correction server.mjs's /api/resolve already
  # carries. The column stores the relative key, but the admin LIST endpoint this script
  # reads formats it into a full CDN URL on the way out, so a live row arrives as
  # `https://<bucket>.s3.../avatars/<id>/<hash>/<file>.glb`. Anchoring at `^avatars/`
  # matched none of them and reported every live companion as PLACEHOLDER, which printed
  # as "NOT LIVE: 6 of 6" over rows whose model and manifest had both just fetched 200.
  # Requiring the published segment and a .glb ending still excludes the seeded
  # placeholder, which is `models/<id>/<id>.glb` in either form.
  param([string]$Path)
  return [bool]($Path -and $Path -match 'avatars/[0-9a-f-]{36}/[A-Za-z0-9._-]+/[^/]+\.glb$')
}

function Get-ManifestUrl {
  # The loader derives it as `${dirname(modelUrl)}/manifest.json` - avatarManifest.ts.
  param([string]$ModelUrl)
  $i = $ModelUrl.LastIndexOf('/')
  if ($i -le 0) { return $null }
  return $ModelUrl.Substring(0, $i) + '/manifest.json'
}

$results = @()

# ---- assets-only mode ------------------------------------------------------------------

if ($PSCmdlet.ParameterSetName -eq 'Assets') {
  foreach ($key in $ModelKeys) {
    $url = "$($CdnBase.TrimEnd('/'))/$($key.TrimStart('/'))"
    $man = Get-ManifestUrl $url
    $results += [PSCustomObject]@{
      Companion = Split-Path $key -Leaf
      Model     = Get-UrlStatus $url
      Manifest  = if ($man) { Get-UrlStatus $man } else { 'n/a' }
    }
  }
  $results | Format-Table -AutoSize
  return
}

# ---- full mode -------------------------------------------------------------------------

if (-not $Token) { throw "No token. Set `$env:TG_ADMIN_TOKEN, or use -CdnBase/-ModelKeys for an assets-only check." }
$headers = @{ Authorization = "Bearer $Token" }

Write-Host "[verify] reading rows from $ApiBase"
$resp = Invoke-RestMethod -Uri "$ApiBase/api/admin/avatars?limit=200" -Headers $headers -Method Get
$rows = if ($resp.data) { $resp.data } elseif ($resp.avatars) { $resp.avatars } else { $resp }

foreach ($slug in $Slugs) {
  $row = $rows | Where-Object { $_.slug -eq $slug } | Select-Object -First 1

  if (-not $row) {
    $results += [PSCustomObject]@{
      Companion = $slug; Row = 'MISSING'; Published = '-'; Model = '-'
      Manifest = '-'; Previews = '-'; Assets = '-'
    }
    continue
  }

  $published = Test-Published $row.modelPath
  $modelUrl = $row.modelPath
  # The API returns modelPath already formatted to a URL on the public route; the admin
  # route returns the raw key. Handle both rather than assuming which.
  if ($modelUrl -notmatch '^https?://') { $modelUrl = $null }

  $modelStatus = if ($modelUrl) { Get-UrlStatus $modelUrl } else { 'no-url' }
  $manUrl = if ($modelUrl) { Get-ManifestUrl $modelUrl } else { $null }
  $manStatus = if ($manUrl) { Get-UrlStatus $manUrl } else { 'no-url' }

  $previewUrls = @($row.thumbnailUrl, $row.imagePreviewUrl, $row.videoPreviewUrl) | Where-Object { $_ }
  $previewsSet = "$($previewUrls.Count)/3"
  $assetStatus = if ($previewUrls.Count -eq 0) { 'none' } else {
    ($previewUrls | ForEach-Object { Get-UrlStatus $_ }) -join ','
  }

  $results += [PSCustomObject]@{
    Companion = $slug
    Row       = if ($row.isActive) { 'active' } else { 'INACTIVE' }
    Published = if ($published) { 'yes' } else { 'PLACEHOLDER' }
    Model     = $modelStatus
    Manifest  = $manStatus
    Previews  = $previewsSet
    Assets    = $assetStatus
  }
}

$results | Format-Table -AutoSize

# ---- verdict ---------------------------------------------------------------------------
#
# Non-zero exit on anything a room would notice, so this can gate a pipeline rather than
# being read by eye. Previews are reported but do NOT fail the run: a companion with no
# preview still loads and talks, and the previews are a later stage by design.

$blocking = $results | Where-Object {
  $_.Row -ne 'active' -or $_.Published -ne 'yes' -or $_.Model -ne 200 -or $_.Manifest -ne 200
}

Write-Host ""
if ($blocking) {
  Write-Host "[verify] NOT LIVE: $($blocking.Count) of $($results.Count) companions would fail to load." -ForegroundColor Red
  exit 1
}

$noPreview = $results | Where-Object { $_.Previews -ne '3/3' }
if ($noPreview) {
  Write-Host "[verify] loads OK, but $($noPreview.Count) companion(s) have incomplete previews — cards will be blank." -ForegroundColor Yellow
} else {
  Write-Host "[verify] all $($results.Count) companions live, with previews." -ForegroundColor Green
}
