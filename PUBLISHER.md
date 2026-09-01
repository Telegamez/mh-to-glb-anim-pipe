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

## Compressing before you publish

A MetaHuman bake costs about **87 MB** to download. Beta serves Avaturn avatars at ~11.5 MB
each, so publishing these as they come out of the bake multiplies the mobile payload by
roughly eight. Nearly three quarters of the model is uncompressed morph-target data — the
geometry is already Draco'd and is not the problem.

**Compress**, beside Publish. Tick the characters, press it, and it writes a compressed copy
to `_compressed/<character>/` next to the bakes. The size column then shows both:

```
telly    49 MB
         -> 16 MB meshopt
```

Then tick **publish the compressed build** and publish as usual. It is the ordinary publish
pointed at the compressed folder — same upload, same SQL, same rollback.

### The two modes, and why only one of them can be published

| | |
|---|---|
| **meshopt** | geometry and morph targets. **Publishable today** — this is the big win. |
| **full (+KTX2)** | also compresses the textures. **Cannot be published** — nothing in the web app can load KTX2 yet. |

The console will refuse to publish a `full` build rather than warn about it. A companion
published that way uploads fine, reports success everywhere, and then renders nothing —
exactly the shape of the G13 failure above, and nothing downstream would catch it either.
`full` exists so the texture saving can be measured while the loader work is outstanding.

### A stale compressed build

The compressed copy is made from the bake, so it can fall behind it — same as a thumbnail.
If the bake changed after it was compressed, the size column marks it **stale** and
publishing asks first. Re-compress rather than shipping it: a stale build publishes the
previous companion while reporting success.

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
- **Previews** (thumbnail, image, video) are photographs of the finished character, so they come
  last. The publishing half only works against a local stack today.
