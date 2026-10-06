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
through `npx`, so that machine needs to be online once. `ktx2` and `full` additionally need the
`ktx` CLI (KTX-Software 4.4+) installed; `meshopt` does not.

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

Panda is: publish, run the SQL, done. Beta isn't.

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
| **extra** | not one of the standard six. Tick it only if you mean to. |
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

## Compression is the standard

A MetaHuman bake costs about **87 MB** to download; the meshopt build of the same character is
about **50 MB**, and nobody wants the other one. So there is nothing to choose (telegamez#3728):

- **Bake** ends by writing the meshopt build to `_compressed/<character>/` beside the bakes.
- **Upload** sends that build. If it is missing, older than the package, or was made in
  another mode, Upload makes it first and then uploads - one press, and the journal always
  says `COMPRESSED build`.
- **Compress** remains for an explicit re-compress; you will rarely need it.

The size column reads:

```
telly    87 -> 50 MB · meshopt              ready as it stands
tommy    87 MB · compresses on publish      no build yet; Upload makes one
telly    87 MB · compressed build is STALE  the bake changed; Upload remakes it
```

There is no `-MeshoptConfirmed` any more. The switch still starts the console, and does
nothing.

### What meshopt does to the model

Geometry and morph targets are quantised: about 0.85 mm median drift on the largest
expression targets, compared against the original through the real runtime with the material
profile, hair calibration and clothing textures live, and indistinguishable. Textures are not
touched; stage 03 already caps and de-alphas them, which is where most of the weight was.

`full` (KTX2 textures only) exists in the compressor for measurement and cannot be published:
the native compositor reads the same files and cannot decode KTX2. WebP sidecar textures are
held until one is confirmed in a room.

## The ktx2 build (telegamez#3662)

A lighter build for phones: KTX2 textures stay compressed on the GPU, and unused blendshapes are
dropped. On tg-drake it takes a companion's GPU memory from ~314 MB (meshopt, PNG) to ~108 MB and
one visit's download from 41.6 MB to 24.5 MB. It carries **two models**:

- `<character>.glb` for the browser, with KTX2 textures;
- `<character>.compositor.glb` for the TV and program output, with PNG textures, because the
  native compositor cannot decode KTX2.

It lives in its own folder, `_compressed_ktx2/<character>/`, so making it never replaces the
meshopt build. Upload sends both models, the PNGs with a `.ktx2` beside each, and the
`variants` that route the compositor to its own model (the platform writes them into the
avatar's `manifest.json`).

**It is the default for panda.** The `build` selector next to Publish / Upload model switches to
ktx2 when you pick panda and to meshopt for beta and local; you can change it either way. Only
publish ktx2 to an environment whose web app includes telegamez#4551 (panda once `dev` is
deployed there); on an older web app the browser cannot load it. A production publish asks for
confirmation naming the build.

The size column shows the ktx2 build on its own line when there is one. The journal records
`COMPRESSED ktx2 build (_compressed_ktx2/)` and the manifest that went up. Go live and roll back
work exactly as for meshopt: the row points at `<character>.glb` either way.

## Previews (telegamez#3662)

**Publish previews** captures any that are missing, then uploads them: one press for one
character or a batch. Capturing photographs each character through the real web app (8-second
video, thumbnail, still), so if nothing answers at `http://localhost:3000` the publisher starts
the telegamez web app itself (about a minute the first time) and stops it when the publisher
closes. The files are written next to the model (`thumbnail.png`, `imagePreview.jpg`,
`videoPreview.mp4`); they are not part of the model publish, so they never change its bake hash.

It needs the telegamez checkout beside this repo (`S:\Avatars\metahuman\telegamez` on the bake
box) or `--monorepo <path>`, plus `ffmpeg` on PATH. A bundled publisher on a machine without
them says so instead of uploading nothing.

**Go live waits for previews.** It refuses a character whose previews are not published yet, so
nothing reaches the avatar picker with a broken card. The order is: Publish, Publish previews,
Go live.

## Publishing doesn't go live

Publishing only uploads files. Nobody sees a change until the SQL runs.

That's deliberate: a bad publish can't take a working companion down, and undoing it is putting a
value back rather than restoring files.

### Every publish is written down

You don't have to keep the SQL out of the log yourself. Each publish appends to:

```
C:\Users\<you>\.telegames-publisher\publishes\<environment>-<bake suffix>.md
```

One file per run — all six characters of a batch land together — holding what was published, when,
against which environment, and **both** SQL statements: the one that goes live and the one that puts
it back. That second statement is the reason this exists: the value it restores is whatever the row
pointed at before, and after a publish overwrites it, nothing else remembers it.

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

---

## Still manual

- **The companion record** — the thing the picker lists — needs the published path and a voice,
  so it can only be made after the publish. Ask an engineer.
- (Previews are no longer manual; see **Previews** above.)
