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
  [string]$BakeHash
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

# Every file, with its path RELATIVE to the directory root. Those relative paths are the
# contract: the sidecar names its textures this way and the runtime resolves them against the
# model's directory, so they are preserved verbatim all the way into the S3 key.
$root = (Resolve-Path $Source).Path
$files = Get-ChildItem -Path $root -Recurse -File | ForEach-Object {
  [PSCustomObject]@{
    FullName = $_.FullName
    Relative = $_.FullName.Substring($root.Length + 1).Replace('\', '/')
    Length   = $_.Length
  }
}

if (-not $BakeHash) {
  # Content-addressed: hash of every relative path and its file hash, so the prefix changes
  # if and only if the directory's content changes. Deriving it from a timestamp instead
  # would republish identical bytes to a new prefix on every run and defeat immutable caching.
  $acc = ($files | Sort-Object Relative | ForEach-Object {
    "$($_.Relative):$((Get-FileHash $_.FullName -Algorithm SHA256).Hash)"
  }) -join "`n"
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $BakeHash = ([BitConverter]::ToString(
    $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($acc))
  ) -replace '-', '').Substring(0, 16).ToLower()
}

$totalMb = [math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "[publish_to_cdn] $Char -> avatars/$AvatarId/$BakeHash/"
Write-Host "[publish_to_cdn] $($files.Count) files, $totalMb MB"

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

  foreach ($f in $files) {
    # Streamed, not read into memory: a single GLB is up to 70 MB and the roster is ~330 MB.
    $stream = [System.IO.File]::OpenRead($f.FullName)
    $streams += $stream
    $part = [System.Net.Http.StreamContent]::new($stream)
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
  Write-Host ""
  Write-Host "Publishing bytes does NOT point the registry at them. To go live:"
  Write-Host "  update avatars set model_path = '$modelKey' where id = '$AvatarId';"
  Write-Host "Rolling back is putting the previous model_path back."
}
finally {
  foreach ($s in $streams) { $s.Dispose() }
  $content.Dispose()
  $client.Dispose()
}
