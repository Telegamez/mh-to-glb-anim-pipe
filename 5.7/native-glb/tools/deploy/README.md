# Companion deploy — getting a baked MetaHuman into an environment

Everything downstream of stage 04. Stage 04 leaves a character in `docs/characters/<id>/`,
`publish_characters.ps1` copies it into the monorepo's `public/models/<id>/`, and this is what
takes it from there to a companion a person can pick in a room.

Runs on this host. PowerShell and Node built-ins only — nothing to install.

---

## The five steps, and which are automated

| # | Step | How |
|---|---|---|
| 1 | Rows exist in the target environment | **Manual today** — see *The gap* below |
| 2 | Publish the directory | `publish_to_cdn.ps1`, or the console |
| 3 | Point the row at the new key | SQL, printed for you |
| 4 | Capture previews | `capture-companion-previews.mjs` (monorepo) |
| 5 | Publish previews | `publish-companion-previews.mjs` (monorepo) — **local only today** |

Steps 2 and 3 are what make a companion *load*. Steps 4 and 5 are what stop its card being
blank. A companion can pass 1–3 and still show an empty tile; that is a normal intermediate
state, not a fault.

---

## The console

```powershell
node 5.7/native-glb/tools/deploy/server.mjs
# http://127.0.0.1:7311
```

Point it at a models root, see every character directory with its readiness, tick the ones you
want, publish. Output streams live.

It **shells out to the same scripts** documented below rather than reimplementing them, so
anything it does can be done without it, and nothing can drift between the two.

Three things it checks before letting you publish, each because its absence has cost time:

- **`mh_materials.json` present.** The runtime keys "is this a MetaHuman" off this file. Without
  it a character publishes fine and renders as plain glTF — no clothing textures, no hair tint —
  which looks exactly like a bad bake and is not one.
- **`textures/` non-empty.**
- **No stray `manifest.json`.** The publish route refuses a manifest arriving as a file
  (`MANIFEST_AS_FILE`), because one sent that way would publish unchecked. Caught here rather
  than 400-ing halfway through a batch.

It also lists characters that are **not in the shipping roster** — `telly-research` sits beside
the six — and leaves them unticked. Listing them keeps the directory's contents answerable;
pre-ticking them would publish a research asset on the first careless run.

**The admin cookie** is typed into the page, held in memory for the process lifetime, and passed
to child processes through the environment. It is never written to disk, never logged, and never
echoed back. The server binds to `127.0.0.1` only.

---

## Or from the shell

```powershell
$env:TG_ADMIN_TOKEN = "<session cookie value>"

# what is already live?
./5.7/native-glb/tools/deploy/Test-CompanionsLive.ps1 -ApiBase https://api.panda.telegames.ai

# publish one
./5.7/native-glb/tools/publish_to_cdn.ps1 -Char telly -AvatarId <uuid> `
    -ApiBase https://api.panda.telegames.ai -BakeHash telly-panda1
```

`Test-CompanionsLive.ps1` is read-only and safe to run at any time. It exits non-zero if a
companion would fail to load, so it can gate a pipeline rather than being read by eye. Incomplete
previews are reported but do not fail the run — a companion with no preview still loads and
talks.

There is also an assets-only mode that needs no token, for checking the CDN before a token
exists:

```powershell
./Test-CompanionsLive.ps1 -CdnBase https://telegames-assets-local.s3.us-west-2.amazonaws.com `
                          -ModelKeys @('avatars/<uuid>/<hash>/telly.glb')
```

---

## Getting the cookie

Sign in at `<ApiBase>/api/auth/signin`, then DevTools → Application → Cookies →
`__Secure-authjs.session-token`.

**Check the cookie's domain before copying it.** A cookie from the bare host is a *production*
session. There is no machine credential yet — that is G14, still open — so this step is manual
by design.

---

## Publishing does not go live

`publish_to_cdn.ps1` uploads bytes to `avatars/<avatarId>/<bakeHash>/` and returns the key. It
deliberately does **not** update `avatars.model_path`:

> publishing bytes and pointing the registry at them are separate steps, so a bad publish cannot
> take a live companion down with it, and a rollback is a row update

So a publish is always safe to run. Nothing a user sees changes until the row moves, and rolling
back is putting the old key back — not restoring objects. Record the before and after keys; that
pair *is* the rollback.

### On bake hashes

Leave `-BakeHash` off and it is derived from the directory's content, so an unchanged bake
republishes to the same prefix. Pass one explicitly when you need a fresh prefix for identical
content.

Any of `[A-Za-z0-9._-]` is safe. That was not always true: the re-seed guard used to recognise
only `[0-9a-z]`, so a hash like `panda-2026-08-28` published fine, the rooms worked, and the next
unrelated re-seed silently reverted every row to the placeholder. Fixed in telegamez#3579, with
the guard's charset now pinned to the route's by test.

---

## The gap — new characters in a remote environment

Steps 2–5 assume the `avatars` and `avatar_companions` rows already exist. Locally the roster
seeder creates them. For panda and beta they are created through the admin front door, and that
part is **not scripted yet**.

Until it is, a genuinely new character needs its rows made by hand before the console can publish
to it — the console asks for an avatar id per character for exactly this reason, rather than
guessing.

Two other things still owed:

- **`publish-companion-previews.mjs` cannot target a remote environment.** It mints its own
  session from a local `NEXTAUTH_SECRET` and its docblock is explicit that this "must not become
  a way in to a deployed environment". It needs the same operator-supplied-cookie treatment
  `publish_to_cdn.ps1` already has.
- **Row updates are printed as SQL, not applied.** `UpdateAvatarInput` does not declare
  `modelPath`, so there is no sanctioned API call for it.
