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
| Ready to publish? | yes | **not yet** |

### Beta isn't ready, and it fails silently

Beta's CloudFront distribution doesn't yet pass the browser's `Origin` through — tracked as
**G13**. Until that's fixed, a companion published to beta will upload fine, update fine, report
success everywhere, **and still not load in a room**. The files are genuinely there; the browser
is refused when it asks for them. Nothing in the publisher can detect this.

**Don't publish to beta until someone confirms G13 is done.**

Panda has no distribution at all — it serves straight from the bucket, which is already
configured — so none of this applies there. That's why panda is where new work is proven.

### Beta goes live in stages

Panda is: publish, go live, done. Beta isn't.

1. Publish and verify — flip nothing.
2. Turn on **one** companion, in one internal room.
3. Someone spends fifteen minutes talking to her.
4. Only then, the rest.

Each step is someone's decision. "Publish to beta" means step 1 unless you're told otherwise.

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

## New or existing — you don't have to know

Tick and publish. The publisher works out whether the character already exists here and either
adds it or updates it. The **What will happen** column says which:

| | |
|---|---|
| **Add as new** | never been here. Type a display name in the box; everything else is handled. |
| **Update to a new version** | already published — this adds a new version alongside the old one |
| **Add its model** | the record is here but has no files yet |
| **extra** | neither a roster id nor bound anywhere. Tick it only if you mean to. |
| red text | folder problem, fix before publishing |

### Changing one that already exists

**Edit existing**, top right. Pick a companion from the list and change its name, description,
gender, or whether it's shown in the picker. Save.

What a companion *is pointed at* isn't editable here — that only changes by publishing.

Folder problems:

| | |
|---|---|
| *no mh_materials.json* | folder is incomplete — re-copy it. Without this the character publishes fine and renders with no clothing textures or hair tint, which looks exactly like a bad bake. |
| *no textures/* | same |
| *stray manifest.json* | delete that file — the publisher makes its own, and one sitting in the folder would be uploaded unchecked |

---

## One build: ktx2

**ktx2 is the only build** (owner decision, 2026-10-08). There is no selector and no meshopt
build any more: two builds of one character under one name is how a meshopt tg-drake reached beta
when ktx2 was meant, and nothing downstream could tell them apart.

The ktx2 build (telegamez#3662) is lighter for phones: KTX2 textures stay compressed on the GPU,
and unused blendshapes are dropped. On tg-drake it takes a companion's GPU memory from ~314 MB to
~108 MB and one visit's download from 41.6 MB to 24.5 MB. Its geometry is still
meshopt-compressed inside the file; that is the codec, not a build. It carries **two models**:

- `<character>.glb` for the browser, with KTX2 textures;
- `<character>.compositor.glb` for the TV and program output, with PNG textures, because the
  native compositor cannot decode KTX2.

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

## Names don't matter: bindings and Rename

A package is tied to its avatar in each environment by the avatar's **id**, written into the
package as `avatar-binding.json` the first time it is matched (by slug `mh-<folder>`) or created
there. From then on the folder name plays no part: rename it and it still updates the same
avatar, with the same companions. The row says where a package is bound (`bound: panda, beta`).
A re-bake keeps the file. Delete an entry only to bind it again.

**Rename**, beside each character, renames the folder, the model, its ktx2 build and the name
in the sidecar, keeping file times so the build stays current. Nothing in any environment changes.
Before renaming, connect once to every environment the character exists in, so it is bound there;
the publisher warns about any it is not. It refuses a name the runtime treats as a calibrated
character (`telly`, `tele`, `telie`, `telee`, `tele1`, `tele2`, `tele4`), because the web app takes
hair, scalp and eye tuning from the model file's name. A folder renamed by hand in Explorer is
listed in red until Rename (with the folder's own name) brings the model in line.

**Sync slugs** makes the ticked avatars' slugs read `mh-<folder>` in the environment you are
connected to, so the admin pages show the new name too. Cosmetic: id, display name, model and
companions are untouched, and each change is journaled with its undo. On beta you type `beta`.
The telegamez seed still names the old slugs; a deploy logs a warning for each and changes nothing.

A package that matches nothing here offers **or update an existing avatar** beside **Add as new**.
Pick the avatar it really is, and it is bound to it instead of becoming a second one.

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

**Publish previews** is the older path: it uploads the three files to the avatar's own preview
slots through `smart-upload`, outside any version. Use it only to fix the previews of an avatar
that is already live without re-publishing its model. The platform removes that route once every
environment runs the version registry.

**Go live waits for previews.** It refuses a character whose previews are neither inside the
version it just published nor already on the avatar. The order is: Capture previews, check them,
Publish, Go live.

**Every change carries a reason.** The **Reason** box in the Characters step is written into each
character's history by Publish, Go live, Remove old versions and Clean up old avatars; the platform
keeps that history permanently and the admin pages show it per version.

## Cleaning up old avatars (telegamez#3662)

**Clean up old avatars** (below the Characters step once you are connected) retires avatars from
the environment you are signed in to, panda or beta.

1. **Scan** lists every avatar with the companions and talk-show slots that use it. The six
   pre-tg ids (`mh-tele`, `mh-telly`, `mh-telie`, `mh-telee`, `mh-tele1`, `mh-tele2`) come
   pre-ticked; tick or untick anything.
2. For an avatar that is in use, pick **Replace with**: its companions and talk-show slots move
   to that avatar and keep their names, voices and prompts. A replacement must be active and not
   itself being retired.
3. **Delete files** only if you want it gone for good (the row and `avatars/<id>/` in S3). Without
   it the avatar is archived: hidden from pickers, files kept, reversible. Since telegamez#4632
   phase 1b an avatar with a lifecycle history (registered versions, or any audited status change)
   cannot be deleted for good: the platform refuses, the run says *kept* and carries on, and its
   old versions go through **Remove old versions** instead.
4. **Dry run** lists every change. Nothing has happened yet. On beta (production) you must type
   `beta` before **Apply** works.
5. **Apply** makes the changes in that order and stops at the first failure. Every change is
   written to `~/.telegames-publisher/publishes/<env>-cleanup-<time>.md` with the exact call that
   undoes it (permanent deletes have none).

Each companion is re-read after it is moved, so if the platform ever stops accepting the change
the run stops before anything is archived.

**Your packages are protected.** An avatar a package in this folder is bound to is marked
*current: package <name>*, listed first under **Replace with**, never pre-ticked, and refused by
the dry run if you tick it.

## Removing old versions

Every publish leaves its predecessor in the bucket. **Remove old versions** lists, for the ticked
characters, every version except the one each avatar is live on. Where the platform runs the
version registry (telegamez#4632 phase 1b) the list comes from it, with each version's state:
`ready` (verified, never live), `retired` (was live), `uploaded` (a publish that never finished),
`deleting` (a removal to retry). Versions the registry does not know yet, from before its
backfill, come from the journals marked `unregistered`. Without the registry (beta before the rc
that carries it) the journals are all there is, as before. All are pre-ticked. **Dry run**, then
**Remove** (type `beta` on beta). Each is one `DELETE companion-directory?bakeHash=&reason=`, which
walks a registered version through `deleting` to `deleted`, objects removed one by one, and
refuses the live one or one a preview still points into.

There is **no undo**, and a removed version can no longer be rolled back to: go live and check
first, then remove. Removals are journaled in `<env>-cleanup-<time>.md` and not offered again.

## Publishing doesn't go live

Publishing only uploads files and, on a platform with the version registry (telegamez#4632 phase
1b), records a version in state `ready`: every object read back from the bucket and matched. Nobody
sees a change until **Go live** points the avatar at it, which is one API call
(`PUT /api/admin/avatars/<id>/model-path` with the key and your reason): the previous version
becomes `retired`, the new one `live`, and the page reads that state back and shows it.

Never update `avatars.model_path` by hand. The registry would not know, and the platform's
`db:characters:verify` reports it as pointer drift. Going live does not change the avatar's status
either: an archived avatar stays archived until it is set active in the admin pages, with a reason.

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
| `VERSION_NOT_REGISTERED` | the key is not a registered version: publish it through this tool, or have the platform's `db:characters:backfill` register uploads that predate the registry |
| `BACKFILL_FAILED` | the avatar's current model could not be registered on the way to going live; the message names the prefix and why. Fix the objects or map the primary model, then retry |
| `lifecycle history` | an avatar with versions or audited changes stays archived; remove its old versions instead of deleting it |
| `Only retired or unverified (uploaded) versions can be deleted` | the live version, or one a preview still points into. Go live on another version first |
| `Unsupported file type in companion directory: videoPreview.mp4` | the platform predates telegamez#4644 (video types inside a version); deploy it, or publish without captured previews |

---

## Still manual

- **The companion record** — the thing the picker lists — needs the published path and a voice,
  so it can only be made after the publish. Ask an engineer.
- (Previews are no longer manual; see **Previews** above.)
