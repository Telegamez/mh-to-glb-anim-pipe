# Telegames MetaHuman Publisher — user guide

Getting a baked MetaHuman into a Telegames environment, so a person can pick it in a room.

---

## Start it

From the repo root:

```powershell
./Start-Publisher.ps1
```

That's it. It starts the publisher and opens `http://127.0.0.1:7311` in your browser. Leave the
window open while you work; **Ctrl+C** stops it.

Nothing to install — PowerShell and Node's built-in modules only.

```powershell
./Start-Publisher.ps1 -Root D:\bakes\ready    # a different models folder
./Start-Publisher.ps1 -Port 7400              # if 7311 is taken
./Start-Publisher.ps1 -NoBrowser              # don't open a browser
```

---

## What you'll do, in order

1. **Pick the environment** — panda, beta or local.
2. **Paste the admin cookie** and press *Set*.
3. **Press Resolve rows** to find each character's avatar record.
4. **Tick the characters** you want and press *Publish selected*.
5. **Run the SQL** it prints, to point the records at what you just published.
6. **Verify**, and then do previews.

Steps 4 and 5 are what make a companion **load**. Previews are separate and come after — a
companion can be fully working and still show a blank card for a while. That's normal, not a
fault.

---

## 1 · Pick the environment

| | |
|---|---|
| **panda** | the acceptance platform. Where new work is proven. |
| **beta** | **production, with real customers.** The page warns you and asks twice. |
| **local** | your own stack. |

---

## 2 · The admin cookie

There's no machine login yet, so you sign in as yourself and lend the publisher your session.

1. Open `https://api.panda.telegames.ai/api/auth/signin` and sign in.
2. **F12** → **Application** → **Cookies** → pick that site.
3. Copy the value of **`__Secure-authjs.session-token`**.
4. Paste it into *Admin cookie* and press **Set**.

> **Check the domain before you copy.** A cookie from `api.telegames.ai` — no `panda.` — is a
> **production** session. Copy from the site you actually mean to publish to.

The cookie is held in memory only for as long as the publisher is running. It is never written to
disk, never logged, and never shown back to you. Closing the window forgets it.

---

## 3 · Resolve rows

Press **Resolve rows**. The publisher asks the environment for each character's avatar record and
shows you one of:

| What you see | What it means |
|---|---|
| a uuid + **published** | ready; publishing again makes a new version |
| a uuid + **row exists, not published** | the record is there, no model uploaded yet |
| a uuid + **(inactive)** | it exists but is hidden from the picker |
| **no row** + a *Create* button | this character has never existed here |

**You never type a uuid.** Every environment has its own — panda's differ from local's and from
beta's — so a number carried over from somewhere else would publish a perfectly good character
onto *somebody else's* record, and nothing later would notice. Switching environments clears them
for the same reason.

### A brand-new character

Press **Create**. You'll be asked for a display name and gender, and it makes the avatar record.
The record starts with no model — that's expected, the publish fills it.

One thing is still manual: the **companion record** (the thing the picker actually lists) needs
the published model path and a voice, so it can only be made *after* the publish. Ask an engineer
for that step until it's automated.

---

## 4 · Pick characters and publish

The table lists every character folder it found, with size and texture count.

**Ticked automatically:** the six shipping MetaHumans, if their folder is complete.

**Listed but not ticked:** anything else it finds, like `telly-research`. These are shown so you
can see what's in the folder, but never selected for you — you'd have to tick one deliberately.

**Greyed out with a reason in red:** something's wrong and it won't publish. You'll see one of:

| Problem | What to do |
|---|---|
| *no mh_materials.json* | The folder is incomplete. Without this file the character publishes fine and then renders with no clothing textures and no hair tint — looking exactly like a bad bake when it isn't one. Re-copy the folder. |
| *no textures/* | Same — incomplete folder. |
| *stray manifest.json* | Delete that file from the folder. The publisher generates it; one sitting in the folder would be uploaded unchecked, so it's refused. |

**Bake suffix** is just a label making this publish distinct, e.g. `panda1` gives
`telly-panda1`. Use something new each time. Letters, numbers, dots, dashes and underscores.

Then press **Publish selected**. Progress streams on the right, one character at a time. If one
fails the batch stops there, so you always know exactly how far it got.

---

## 5 · Nothing is live yet — run the SQL

**Publishing only uploads files.** Nobody sees any change until the records point at them.

That's deliberate: a bad publish can't take a working companion down, and undoing it is just
putting the old value back rather than restoring files.

The publisher prints the statements. Give them to whoever has database access for that
environment. Keep the **before** values — that's your undo.

---

## 6 · Check it worked

Press **Verify live**. For every character it reports:

- the record exists and is active
- it points at a real publish, not a placeholder
- the model file actually downloads
- its manifest downloads beside it
- how many of the three preview assets are set, and whether they download

**Green** means someone opening a room would see that companion.

**Yellow** means it loads and talks, but its card will be blank — previews aren't done yet.

**Red** means it would fail to load. Don't move on.

---

## 7 · Previews (last)

Preview images and video are photographs of the finished character, so they can only be made
*after* the model is published. Three assets per companion — a 512px thumbnail, a 1024px image,
and an 8-second video — all produced by a script that opens a browser and records the real
character.

This step isn't in the interface yet, and the publishing half currently only works against a
local stack. Ask an engineer to run it for panda or beta.

---

## If something goes wrong

| Message | Meaning |
|---|---|
| `admin API 401 — is the cookie for panda?` | The cookie is wrong, expired, or from a different environment. Sign in again and re-copy. |
| `Port 7311 is already in use` | It's probably already running — open http://127.0.0.1:7311. Otherwise use `-Port`. |
| `no resolved avatar row for: …` | Press **Resolve rows** first. |
| `MANIFEST_AS_FILE` | A stray `manifest.json` in a character folder. Delete it. |
| `Node.js is not on PATH` | Open a shell where `node --version` works. |

---

## The short version

```powershell
./Start-Publisher.ps1
```

Environment → cookie → **Resolve rows** → tick → **Publish selected** → run the SQL →
**Verify live**. Previews after.
