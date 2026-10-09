<#
.SYNOPSIS
  Publish a baked character directory to S3 through the platform's admin ingest API.

.DESCRIPTION
  `publish_characters.ps1` copies a bake into the monorepo's `public/models/<id>/`, which
  serves local development. This publishes the same directory to the platform, and it does so
  through `POST /api/admin/avatars/<avatarId>/companion-directory` rather than by copying to
  the bucket.

  That indirection is the point, and it is plan D4: the admin API stays the one front door for
  assets. A bake box holding bucket credentials and running `aws s3 cp` would be quicker and
  would put the platform's asset store outside the platform's own control - no permission
  check, no activity log, no validation, and no single place to change when the layout moves.

  What the API validates, which a copy cannot:

  * the sidecar is present - the runtime decides "is this a MetaHuman" by its presence, so a
    directory without one publishes an avatar that silently renders as plain glTF
  * every texture the sidecar NAMES was actually uploaded, so a half-copied directory fails
    at publish instead of rendering wrong later
  * the model named as `modelPath` is really in the payload

  A `ktx2` build (telegamez#3662) carries two models: `<char>.glb` for the browser, with KTX2
  textures, and `<char>.compositor.glb` with PNG textures for the native compositor, which reads
  the same published directory and cannot decode KTX2. The compositor finds its file through the
  avatar manifest's `variants.compositor`, so when the build's `compression_manifest.json` records
  `variants`, they are sent as the `manifest` form field and the route writes `manifest.json`
  from them. Without it the compositor falls back to the KTX2 model and fails on the TV.

  The publish is content-addressed: files land under `avatars/<avatarId>/<bakeHash>/`, so a
  re-bake publishes ALONGSIDE the live one rather than over it. Nothing changes for users
  until the returned `modelKey` is written into the avatar row, and rolling back is putting
  the old key back - not restoring objects.

.PARAMETER Char
  Character id, e.g. `telly`. Its directory is taken from -Source.

.PARAMETER AvatarId
  The `avatars` row UUID this directory belongs to.

.PARAMETER ApiBase
  Admin API base, e.g. http://localhost:3005 locally.

.PARAMETER Token
  Admin bearer token. Falls back to $env:TG_ADMIN_TOKEN.

.PARAMETER Source
  Directory holding <char>.glb, mh_materials.json and textures/.
  Defaults to the monorepo publish dir so this runs after publish_characters.ps1.

.PARAMETER BakeHash
  Overrides the computed hash. Normally leave it: the default is derived from the content, so
  an unchanged bake republishes to the same prefix and a changed one cannot collide.

.PARAMETER Build
  What the package holds, recorded on the version (`ktx2`). Sent as the `build` form field so
  the registry does not have to infer it from the bake hash's name (telegamez#4632 phase 1b).

.PARAMETER Reason
  Why this is being published, recorded in the character's history with the version. The
  platform defaults it while the publisher is in transition; sending one is the contract.

.PARAMETER PreviewsFrom
  Directory holding `thumbnail.png`, `imagePreview.jpg` and `videoPreview.mp4`. When given, the
  three ride INSIDE the version with a generated `character.json` naming them, so they go live,
  roll back and get deleted together with the model (telegamez#4632 phase 1b). All three must be
  there. Without it the version carries no previews and the avatar keeps whatever it has.

.EXAMPLE
  ./publish_to_cdn.ps1 -Char telly -AvatarId 8e671d15-... -ApiBase http://localhost:3005
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Char,
  [Parameter(Mandatory = $true)][string]$AvatarId,
  [Parameter(Mandatory = $true)][string]$ApiBase,
  [string]$Token = $env:TG_ADMIN_TOKEN,
  [string]$Source,
  [string]$BakeHash,
  [string]$Build,
  [string]$Reason,
  [string]$PreviewsFrom
)

$ErrorActionPreference = 'Stop'

if (-not $Source) {
  $Source = Join-Path 'S:\Avatars\metahuman\telegamez\packages\web-next\public\models' $Char
}
if (-not (Test-Path $Source)) { throw "Source directory not found: $Source" }

$model = "$Char.glb"
if (-not (Test-Path (Join-Path $Source $model))) { throw "Model not found: $Source\$model" }
if (-not (Test-Path (Join-Path $Source 'mh_materials.json'))) {
  throw "mh_materials.json not found in $Source - the runtime finds it beside the model"
}

# telegamez#3662: a build that records `variants` (the ktx2 mode) publishes them as the avatar
# manifest. Checked here rather than left to the route, so a half-copied ktx2 build fails before
# any upload: the route would refuse a variant that is not in the payload, but only after the
# operator watched ~60 MB go up.
$avatarManifest = $null
$compressionManifest = Join-Path $Source 'compression_manifest.json'
if (Test-Path $compressionManifest) {
  $cm = Get-Content $compressionManifest -Raw | ConvertFrom-Json
  if ($cm.mode -eq 'ktx2' -and -not $cm.variants) {
    throw "compression_manifest.json says mode=ktx2 but records no variants - the compositor would fall back to the KTX2 model. Recompress."
  }
  if ($cm.variants) {
    if ($cm.variants.room -ne $model) {
      throw "variants.room '$($cm.variants.room)' is not the model being published ('$model')"
    }
    foreach ($kind in 'room', 'compositor') {
      $file = $cm.variants.$kind
      if ($file -and -not (Test-Path (Join-Path $Source $file))) {
        throw "variants.$kind names '$file', which is not in $Source"
      }
    }
    # Only the variants. Everything else an avatar manifest can carry (faceProfile,
    # compositorMorphDrops) is a per-rig decision that does not come from compression.
    $avatarManifest = @{ variants = @{ room = $cm.variants.room; compositor = $cm.variants.compositor } } |
      ConvertTo-Json -Compress -Depth 4
  }
}

# Every file, with its path RELATIVE to the directory root. Those relative paths are the
# contract: the sidecar names its textures this way and the runtime resolves them against the
# model's directory, so they are preserved verbatim all the way into the S3 key.
#
# The three preview assets are never taken from the source directory itself: a stray copy there
# would be sent as a texture. They enter only through -PreviewsFrom, below, named by a
# character.json so the platform knows what they are.
$PREVIEW_ASSETS = @('thumbnail.png', 'imagePreview.jpg', 'videoPreview.mp4')

$root = (Resolve-Path $Source).Path
$files = Get-ChildItem -Path $root -Recurse -File |
  Where-Object { $PREVIEW_ASSETS -notcontains $_.Name } |
  ForEach-Object {
  [PSCustomObject]@{
    FullName = $_.FullName
    Relative = $_.FullName.Substring($root.Length + 1).Replace('\', '/')
    Length   = $_.Length
    Bytes    = $null
  }
}

# Previews ride inside the version (telegamez#4632 phase 1b). `character.json` names them; on
# go-live the platform mirrors them onto the avatar's preview columns, on rollback the previous
# version's come back, and Remove old versions takes them with their version. It also carries
# the tuning id, so the runtime stops deriving it from the model's file name.
$extras = @()
$previewsIncluded = $false
if ($PreviewsFrom) {
  if (-not (Test-Path $PreviewsFrom)) { throw "-PreviewsFrom not found: $PreviewsFrom" }
  $missingPreviews = @($PREVIEW_ASSETS | Where-Object { -not (Test-Path (Join-Path $PreviewsFrom $_)) })
  if ($missingPreviews.Count -gt 0) {
    throw "-PreviewsFrom $PreviewsFrom is missing $($missingPreviews -join ', ') - capture previews first, or publish without -PreviewsFrom"
  }
  if ($files | Where-Object { $_.Relative -eq 'character.json' }) {
    throw "$Source already holds a character.json; the publisher generates that file from the previews"
  }
  foreach ($p in $PREVIEW_ASSETS) {
    $item = Get-Item (Join-Path $PreviewsFrom $p)
    $extras += [PSCustomObject]@{ FullName = $item.FullName; Relative = $p; Length = $item.Length; Bytes = $null }
  }
  $release = [ordered]@{
    previews = [ordered]@{ thumbnail = 'thumbnail.png'; image = 'imagePreview.jpg'; video = 'videoPreview.mp4' }
  }
  # -cmatch: the platform's schema is case-sensitive lowercase; -match would pass a name it refuses.
  if ($Char -cmatch '^[a-z0-9][a-z0-9_-]{0,63}$') { $release.tuningId = $Char }
  $releaseBytes = [Text.Encoding]::UTF8.GetBytes((($release | ConvertTo-Json -Depth 4) + "`n"))
  $extras += [PSCustomObject]@{ FullName = $null; Relative = 'character.json'; Length = $releaseBytes.Length; Bytes = $releaseBytes }
  $previewsIncluded = $true
}
$parts = @($files) + @($extras)

if (-not $BakeHash) {
  # Content-addressed: hash of every relative path and its file hash, so the prefix changes
  # if and only if the package's content changes - previews included, since they are part of
  # the version now. Deriving it from a timestamp instead would republish identical bytes to a
  # new prefix on every run and defeat immutable caching.
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $acc = ($parts | Sort-Object Relative | ForEach-Object {
    $h = if ($_.Bytes) { ([BitConverter]::ToString($sha.ComputeHash($_.Bytes)) -replace '-', '') }
         else { (Get-FileHash $_.FullName -Algorithm SHA256).Hash }
    "$($_.Relative):$h"
  }) -join "`n"
  $BakeHash = ([BitConverter]::ToString(
    $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($acc))
  ) -replace '-', '').Substring(0, 16).ToLower()
}

$totalMb = [math]::Round(($parts | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "[publish_to_cdn] $Char -> avatars/$AvatarId/$BakeHash/"
Write-Host "[publish_to_cdn] $($parts.Count) files, $totalMb MB"
if ($Build) { Write-Host "[publish_to_cdn] build: $Build" }
Write-Host "[publish_to_cdn] previews: $(if ($previewsIncluded) { 'included in the version' } else { 'not included (no -PreviewsFrom)' })"
if ($avatarManifest) { Write-Host "[publish_to_cdn] avatar manifest: $avatarManifest" }

Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [TimeSpan]::FromMinutes(20)
if ($Token) {
  $client.DefaultRequestHeaders.Authorization =
    [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Token)
}

$content = [System.Net.Http.MultipartFormDataContent]::new()
$streams = @()

<#
  Every part's Content-Disposition is set EXPLICITLY, with quoted values and no `filename*`.

  `MultipartFormDataContent.Add()` writes RFC 2388-style unquoted parameters -
  `form-data; name=files; filename=x.glb; filename*=utf-8''x.glb` - and the API's
  `request.formData()` is undici, which follows RFC 7578 strictly and rejects an unquoted
  `name`. Every request failed with "Failed to parse body as FormData", at any size: a 32-byte
  body failed exactly as a 100 MB one did, while the identical request from curl (which quotes)
  reached validation and was refused properly.

  This is why the tool had never worked against undici. The 19 August publish that the
  replacement plan calls the "operator path" was actually a Node client with a session cookie -
  the admin activity log shows user_agent=node and 45-char auto-session keys.
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
  Add-QuotedPart $content ([System.Net.Http.StringContent]::new($BakeHash)) 'bakeHash'
  Add-QuotedPart $content ([System.Net.Http.StringContent]::new($model)) 'modelPath'
  if ($avatarManifest) {
    Add-QuotedPart $content ([System.Net.Http.StringContent]::new($avatarManifest)) 'manifest'
  }
  # telegamez#4632 phase 1b: the version records what it holds and why it was published. The
  # platform defaults both while the publisher is in transition; this is the publisher sending them.
  if ($Build) { Add-QuotedPart $content ([System.Net.Http.StringContent]::new($Build)) 'build' }
  if ($Reason) { Add-QuotedPart $content ([System.Net.Http.StringContent]::new($Reason)) 'reason' }

  foreach ($f in $parts) {
    if ($f.Bytes) {
      $part = [System.Net.Http.ByteArrayContent]::new($f.Bytes)
    } else {
      # Streamed, not read into memory: a single GLB is up to 70 MB and the roster is ~330 MB.
      $stream = [System.IO.File]::OpenRead($f.FullName)
      $streams += $stream
      $part = [System.Net.Http.StreamContent]::new($stream)
    }
    # The part FILENAME carries the relative path; the API uses it as the key suffix.
    Add-QuotedPart $content $part 'files' $f.Relative
  }

  $url = "$($ApiBase.TrimEnd('/'))/api/admin/avatars/$AvatarId/companion-directory"
  Write-Host "[publish_to_cdn] POST $url"
  $response = $client.PostAsync($url, $content).GetAwaiter().GetResult()
  $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()

  if (-not $response.IsSuccessStatusCode) {
    throw "Ingest failed ($([int]$response.StatusCode)): $body"
  }

  $parsed = $body | ConvertFrom-Json
  $modelKey = $parsed.data.modelKey
  Write-Host "[publish_to_cdn] OK - $($parsed.data.files.Count) files published"
  Write-Host "[publish_to_cdn] modelKey: $modelKey"
  # Since telegamez#4632 phase 1b the response carries the version row: `ready` means every
  # object was read back from the bucket and matched its digest. An older platform returns no
  # version; the line is then omitted rather than invented.
  $version = $parsed.data.version
  if ($version -and $version.state) {
    Write-Host "[publish_to_cdn] version: $($version.version) state $($version.state) build $($version.build)"
  }
  Write-Host ""
  Write-Host "Publishing bytes does NOT make them live. To go live, through the API:"
  Write-Host "  PUT $($ApiBase.TrimEnd('/'))/api/admin/avatars/$AvatarId/model-path  { `"modelKey`": `"$modelKey`", `"reason`": `"...`" }"
  Write-Host "Rolling back is the same call with the previous key. Never update avatars.model_path by hand:"
  Write-Host "the registry would not know, and db:characters:verify reports it as pointer drift."
}
finally {
  foreach ($s in $streams) { $s.Dispose() }
  $content.Dispose()
  $client.Dispose()
}
