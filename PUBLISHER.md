# Telegames MetaHuman Publisher

```powershell
./Start-Publisher.ps1
```

Opens at `http://127.0.0.1:7311`. Leave the window open; **Ctrl+C** stops it.

The interface leads you through it — pick an environment, connect, tick characters, publish. This
page is for the things it can't show you.

```powershell
./Start-Publisher.ps1 -Port 7400     # if 7311 is taken
./Start-Publisher.ps1 -NoBrowser     # don't open a browser
```

---

## Sending it to someone

`dist/TelegamesPublisher.zip` is ready to send. They unzip it and run `Start-Publisher.ps1` —
no repo, no install, nothing to build. They need Node.js and PowerShell 7 and nothing else.

One exception, and only if they use **Compress**: the first run fetches `gltf-transform`
through `npx`, so that machine needs to be online once. It also needs the `ktx` CLI
(KTX-Software 4.4+) installed.

The first time, press **Browse** and point it at wherever the character folders are. It marks
which folders hold characters as you go, so you are looking for the one that says *6 characters*
rather than reading paths. It remembers that folder from then on.

To rebuild the zip after a change:

```powershell
./5.7/native-glb/tools/deploy/Build-Bundle.ps1
```

It copies the same files the repo runs, flat — there is no second copy to drift.

---

## Panda and beta are not the same job

| | **panda** | **beta** |
|---|---|---|
| Who sees it | internal | **real customers** |
| Sign in at | `api.panda.telegames.ai` | `api.beta.telegames.ai` |
| Files go to | `telegames-assets-local` | `telegames-assets-beta` |
| Served from | the bucket directly | CloudFront, `assets.beta.telegames.ai` |
| Publish from here? | yes | **no**: beta takes characters by promotion from panda |

### Beta receives characters by promotion

Since telegamez#4632 phase 2 nothing is uploaded to beta from this machine. A version is published
to panda, goes live there, gets its approval recorded on panda's admin Characters page, and is then
**promoted** from beta's admin Characters page: beta copies the files from panda's bucket, verifies
every digest, and holds the version as `ready` until someone takes it live there, one companion in
one internal room first. The publisher shows beta as *promotion only* and publishes nothing to it;
the platform refuses a direct upload there as well.

Panda has no CDN at all — it serves straight from the bucket — which is why panda is where new
work is proven.

### The hosts differ by one word

```
api.panda.telegames.ai    panda
api.beta.telegames.ai     beta — production
api.telegames.ai          production, directly
```

A cookie from the bare host is a production session whatever you meant by it. The publisher
itself got this wrong once — it had beta pointed at `api.telegamez.com`, which is the *service's*
name, not any environment's address.

---

## The character's id: `character.json`

Every package folder carries a `character.json` naming the character it publishes as
(telegamez#4632 phase 2):

```json
{ "id": "drake", "name": "Drake", "gender": "male" }
```

The `id` is the character's identity in **every** environment's registry: lowercase letters,
digits and dashes, chosen once and never renamed. The folder name and the model file name play no
part (`tg-tom` publishes as `tim`). `name`, `gender` and `tags` are optional for a character the
registry already knows (its values are kept); a new id needs at least a `name`, and its first
publish creates the character. A package without the file is listed in red and cannot be
published. Re-bakes keep the file.

Tick and publish. The **What will happen** column says which case it is:

| | |
|---|---|
| **First publish creates `<id>`** | the registry has no such character yet; this publish creates it |
| **Update to a new version** | already published — this adds a new version alongside the old one |
| **Add its model** | the character is here but has no live version yet |
| red text | folder problem, fix before publishing |

A character's display name, gender and tags change by publishing a package whose `character.json`
says so; what it *serves* changes only by going live.

Folder problems:

| | |
|---|---|
| *no mh_materials.json* | folder is incomplete — re-copy it. Without this the character publishes fine and renders with no clothing textures or hair tint, which looks exactly like a bad bake. |
| *no textures/* | same |
| *stray manifest.json* | delete that file — the publisher makes its own, and one sitting in the folder would be uploaded unchecked |
| *no character.json* | add one with the character's id (above) |

---

## One build: ktx2

**ktx2 is the only build** (owner decision, 2026-10-08). There is no selector and no meshopt
build any more: two builds of one character under one name is how a meshopt tg-drake reached beta
when ktx2 was meant, and nothing downstream could tell them apart.

The ktx2 build (telegamez#3662) is lighter for phones: KTX2 textures stay compressed on the GPU,
and unused blendshapes are dropped. On tg-drake it takes a companion's GPU memory from ~314 MB to
~108 MB and one visit's download from 41.6 MB to 24.5 MB. Its geometry is still
meshopt-compressed inside the file; that is the codec, not a build. It carries **two models**, plus an optional third:

- `<character>.glb` for the browser, with KTX2 textures;
- `<character>.compositor.glb` for the TV and program output, with PNG textures, because the
  native compositor cannot decode KTX2;
- `<character>.seat.glb` for small seats such as the Hold'em table (telegamez#4863): the room
  model with morph normals dropped and textures capped at 1024 px, on the same sidecar.

It lives in `_compressed_ktx2/<character>/` beside the bakes. **Bake** ends by making it;
**Publish** sends it, making it first if it is missing, older than the package, or does not name
the character's two models; **Compress** remakes it on demand. Upload sends both models, the PNGs
with a `.ktx2` beside each, and the `variants` that route the compositor to its own model (the
platform writes them into the avatar's `manifest.json`). Every environment's web app loads it
(telegamez#4551; beta since 2.0.0-rc52.3).

**The published path names the build.** The version folder is `<character>-ktx2-<suffix>`, so a
row reads `avatars/<id>/tg-drake-ktx2-20261008-6326/tg-drake.glb`, and the go-live call, the
rollback and the journal all say ktx2. Since telegamez#4632 phase 1b the build is also sent as
its own field and recorded on the version, so nothing has to read it out of the name. Older publishes carry no build in the path
(`tg-drake-20261008-6670`); the journal's `source` line says which build those were, and the
**live here** line on each character shows it: green for ktx2, amber for anything else.

Old `_compressed/` (meshopt) folders on disk are ignored. **Remove old versions**, below, deletes
the published ones.

## Names don't matter: the id and Rename

The id in `character.json` is the whole of a package's identity. Rename the folder and it still
publishes to the same character, with the same companions, in every environment. The old
per-environment `avatar-binding.json` is read by nothing any more; where one still sits in a
package it is harmless.

**Rename**, beside each character, renames the folder, the model, its ktx2 build and the name
in the sidecar, keeping file times so the build stays current. Nothing in any environment changes.
It refuses a name the runtime treats as a calibrated character (`telly`, `tele`, `telie`, `telee`,
`tele1`, `tele2`, `tele4`), because the web app takes hair, scalp and eye tuning from the model
file's name where a release carries no tuning id. A folder renamed by hand in Explorer is listed
in red until Rename (with the folder's own name) brings the model in line.

## Previews (telegamez#3662, telegamez#4632)

Previews are captured, **checked**, then published **with the model**:

1. Tick the characters and press **Capture previews**. Each is photographed through the real web
   app: an 8-second video (720x720 MP4), a thumbnail (512 PNG) and a still (1024 JPEG). If nothing
   answers at `http://localhost:3000` the publisher starts the telegamez web app itself (about a
   minute the first time) and stops it when the publisher closes. Nothing is uploaded.
2. The **Previews** panel shows every character's thumbnail, image and playable video as they
   will be uploaded. Framing wrong (feet or head cut off; the log says when auto-framing fell
   back to the default camera)? Tick it and press **Capture previews** again.
3. **Publish** sends them inside the version: the three files and a `character.json` naming them
   land in the same prefix as the model, so they go live, roll back and get removed together with
   it (telegamez#4632 phase 1b). A character published without captured previews gets a version
   with none, and its avatar keeps whatever previews it already has; the log says which.

The files are written next to the model (`thumbnail.png`, `imagePreview.jpg`,
`videoPreview.mp4`). Capturing needs the telegamez checkout beside this repo
(`S:\Avatars\metahuman\telegamez` on the bake box) or `--monorepo <path>`, plus `ffmpeg` on PATH.

There is no separate preview upload any more: the platform's older route is gone (telegamez#4645),
so changing an avatar's previews means capturing them again and publishing a new version.

**Go live waits for previews.** It refuses a character whose previews are neither inside the
version it just published nor already on the avatar. The order is: Capture previews, check them,
Publish, Go live.

**Every change carries a reason.** The **Reason** box in the Characters step is written into each
character's history by Publish, Go live, Remove old versions and Clean up old avatars; the platform
keeps that history permanently and the admin pages show it per version.

## Retiring, withdrawing and re-pointing: the platform's

Nothing here retires a character or moves its companions any more (telegamez#4632 phase 2). That
is the platform's admin **Characters** page: retire (hidden from new selections, keeps serving),
withdraw from service (every companion and seat moved to a reviewed replacement, or marked
unavailable), and the change history. The publisher does three things: publish a version, take it
live, remove old versions.

## Removing old versions

Every publish leaves its predecessor in the bucket. **Remove old versions** lists, for the ticked
characters, every version the platform's registry holds except the live one, with its state:
`ready` (verified, never live, **not** pre-ticked and with its own **Go live**), `retired` (was
live), `uploaded` (a publish that never finished; one started in the last twenty minutes is not
pre-ticked, it may still be uploading), `deleting` (a removal to retry). **Dry run**, then
**Remove** (type `beta` on beta). Each is one `DELETE /api/admin/characters/<id>/versions/<version>`
with your reason, which walks the version through `deleting` to `deleted`, objects removed one by
one, and refuses the live one or one a preview still points into.

There is **no undo**, and a removed version can no longer be rolled back to: go live and check
first, then remove. Removals are journaled in `<env>-cleanup-<time>.md`.

## Publishing doesn't go live

Publishing uploads the package and records a version in state `ready`: every object read back
from the bucket and matched. Nobody sees a change until **Go live**, which is one API call
(`POST /api/admin/characters/<id>/versions/<version>/live` with your reason): the previous version
becomes `retired`, the new one `live`, and the page reads that state back and shows it. Rolling
back is the same call on the previous version.

Never update `avatars.model_path` by hand. The registry would not know, and the platform's
`db:characters:verify` reports it as pointer drift. Going live does not change a retired
character's state either: it stays retired until it is set active on the admin Characters page,
with a reason.

That's deliberate: a bad publish can't take a working companion down, and undoing it is putting a
value back rather than restoring files.

### Every publish is written down

You don't have to keep the SQL out of the log yourself. Each publish appends to:

```
C:\Users\<you>\.telegames-publisher\publishes\<environment>-<bake suffix>.md
```

One file per run — all six characters of a batch land together — holding what was published, when,
against which environment, the version state the registry reported, whether the previews rode
inside, the reason, and **both** API calls: the one that goes live and the one that puts it back.
That second call is the reason this exists: the value it restores is whatever the row pointed at
before, and after a publish overwrites it, nothing else remembers it.

Failed characters are recorded too. A record saying a publish failed is what tells you it was
attempted; a missing record would look the same as never having tried.

The file contains avatar ids and CDN paths. It never contains your cookie.

---

## When it says no

| | |
|---|---|
| `admin API 401` | wrong, expired, or from a different environment. Sign in again, copy from that same site. |
| `Port 7311 is already in use` | probably already running — open http://127.0.0.1:7311 |
| `MANIFEST_AS_FILE` | stray `manifest.json` in a character folder |
| `Node.js is not on PATH` | open a shell where `node --version` works |
| `a reason is required` | fill the **Reason** box in the Characters step; it goes into the character's history |
| `no character.json` / `"id" must be` | the package needs a `character.json` with the character's id (see *The character's id*) |
| `Package identity must match the character URL` | the `id` inside `character.json` is not the one the publisher sent; the file was edited mid-run. Reload and publish again |
| `Existing avatar matches this identity; apply its reviewed alias mapping first` | the platform has a legacy avatar with this name or slug that nobody has mapped to a character id yet; the platform side applies the reviewed mapping, then publish |
| `Existing identity requires a reviewed primary alias in this environment` | the character exists but has no reviewed avatar in this environment; the platform side's mapping, not a publish, creates that |
| `Beta requires verified promotion provenance` | nothing publishes to beta from here; promote from beta's admin Characters page |
| `no character registry here` | this platform predates phase 2; nothing can be published to it from here |
| `Only retired or unverified (uploaded) versions can be deleted` | the live version, or one a preview still points into. Go live on another version first |
| `Unsupported file type in companion directory: videoPreview.mp4` | the platform predates telegamez#4644 (video types inside a version); deploy it, or publish without captured previews |

---

## Still manual

- **The companion record** — the thing the picker lists — needs the published path and a voice,
  so it can only be made after the publish. Ask an engineer.
- (Previews are no longer manual; see **Previews** above.)
