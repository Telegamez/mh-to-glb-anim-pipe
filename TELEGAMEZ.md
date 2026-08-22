# Telegamez fork — what diverges from upstream, and why

Fork of [smorchj/metahuman-to-glb](https://github.com/smorchj/metahuman-to-glb).
Branch: **`ue58-compat`**. Upstream `main` is unmodified.

Purpose: bake MetaHuman characters into GLBs that satisfy the **Telegamez companion asset
contract** so they can be driven by the existing naturalMotion / naturalLipsync stack.
Contract: `_docs/Architecture/Companions/NaturalMotion/07-Asset-Pipeline-And-Contracts.md` in
the `telegamez` monorepo, enforced by `scripts/motion/audit_models.py`.

Everything here lives in `5.7/native-glb/stages/`. The `5.6/cinematic` and `5.7/cinematic`
trees are untouched.

---

## The two categories, and why the distinction matters

Changes fall into two groups that must not be confused. Mixing them up means either offering
upstream a patch that breaks their tool, or dropping a fix that everyone would benefit from.

### A. General fixes — candidates to upstream

Nothing Telegamez-specific. These are bugs, version drift, or environmental hazards that would
affect anyone running the pipeline.

| Fix | Why it is general |
|---|---|
| `MetaHumanDefaultPipelineType` built by `hasattr` | 5.7 exposes `CINEMATIC/OPTIMIZED/DCC`, 5.8 drops `DCC` and adds `UEFN`. The dict literal dereferenced all three eagerly, so a *cinematic* build died on 5.8 with "no attribute 'DCC'" without reaching the lookup. Version-agnostic now. |
| Stage 00 saves via `save_dirty_packages` | Saving was hardcoded to `/Game/<Name>/`; 5.8 builds into `/Game/Unpacked/<Name>/`, so the save missed and the exception was swallowed as a warning — the stage reported `DONE` having persisted nothing. **See the re-bake note below.** |
| A failed save is fatal | A stage that cannot persist its own output must not report success. |
| Stage 01 resolves the built-asset folder | Same 5.7-vs-5.8 path assumption, second location. |
| `scan_paths_synchronous` before the ARKit lookup | UE's AssetRegistry lazy-loads plugin content, so in a fresh commandlet `load_asset` returns `None` for assets that exist. The stage then skipped the **entire ARKit bake** while writing a manifest with "0 warnings". `_list_under` already documents this exact hazard — the guard was simply never applied here. |
| `--factory-startup` on both Blender launchers | `read_factory_settings()` tears down whatever is registered, and a third-party addon that unregisters badly takes Blender with it. Observed: `cc_blender_tools` raised in `unregister()` and Blender died with `EXCEPTION_ACCESS_VIOLATION` before the stage ran a line of its own. A reproducible bake must not depend on an operator's installed addons. |
| Publish the `_Tangent` atlas | It drives anisotropic strand specular and was never deployed — stage 02 did not declare it, so stage 03 did not forward it and stage 04 did not copy it. The file was in `01-glb/textures/` the whole time. A published avatar silently lost the strand highlight while a local bake kept it. |
| `"character"` derived, not hardcoded `"ada"` | The sidecar named every character "ada", demos included. |
| Outfit `Color1` carried into the sidecar | UE's GLTFExporter writes outfit materials with neither a `baseColorTexture` nor a `baseColorFactor`, and glTF defaults absent base colour to **white**. A character whose shirt is navy and chinos khaki in the editor exports dressed entirely in white, with nothing reporting a problem. Stage 01 now records the colour and stage 02 writes it into `mh_materials.json`. |
| `_resolve_mi_params` reads *effective* params | `_read_mi_params` reads `vector_parameter_values`, which is the **override** array on one instance. Groom MIs override `hairMelanin` directly so it worked there; outfit MIs inherit `Color1` from the parent chain, so it came back empty and the caller's `if not (vectors or scalars): continue` dropped them silently. The resolver walks to the base material and asks `MaterialEditingLibrary` for the resolved value. |
| Thumbnail step is non-fatal *under capture* too | See "non-fatal only when nobody is looking" below. |

### B. Telegamez platform contract — do NOT upstream

These exist to satisfy our asset contract. They would be wrong for a general-purpose
MetaHuman→GLB tool, and upstream should not carry them.

| Addition | What it does |
|---|---|
| **15 visemes + 6 convenience shapes** | Composed in stage 02 from captured ARKit deltas as weighted sums of existing shape keys. ARKit does not provide them; `@telegamez/lipsync`'s `VISEME_MAP` requires them. Takes the face from 51 to 72 morph targets. |
| **Bone renames** | 16 MetaHuman joints renamed to Mixamo-family contract names (`pelvis`→`Hips`, `spine_01/03/05`→`Spine/Spine1/Spine2`, `FACIAL_L_Eye`→`LeftEye`, …). A rename, not a hierarchy collapse — nothing is deleted, and `spine_02/04` stay as intermediates. |
| **Mesh split** | The single 8-slot face mesh is split by material into `Head_Mesh`, `Eye_Mesh`, `EyeAO_Mesh`, `Eyelash_Mesh`, `Teeth_Mesh`; the body becomes `Body_Mesh`. Required because our lid-morph rule asserts blink coverage on each eye-region mesh independently. |
| **`wardrobe_overrides`** | An optional block in `characters/<id>/manifest.json` that overrides outfit material params in stage 01. Keys match as case-insensitive substrings of the MI name, so one entry covers every character wearing the same outfit. See below for why it is not done in UE. |

### Why wardrobe colour is overridden in the pipeline, not in UE

Some stock garments ship white: every current companion wears *HS Casual Outfit 64*, whose
Desert Boots are `(1,1,1)`. The obvious fix is to recolour the MI under
`/Game/Unpacked/<Name>/Clothing` in the editor. **That does not hold** — stage 00 regenerates
that folder, so the edit is silently lost on the next bake, and per the re-bake trap below,
every character edit after the first *is* a re-bake.

Overriding in stage 01 instead survives re-bakes, requires no mutation of the operator's UE
project, and is visible in a manifest rather than buried in a `.uasset`. The override logs
before→after so the substitution is never invisible.

---

## Running on UE 5.8 and Blender 4.4

Upstream targets UE 5.7 and states Blender 5.x. This fork is verified on **UE 5.8.1** and
**Blender 4.4.1**.

- `ue_version` in `_config/pipeline.yaml` selects both the path block **and the pipeline
  directory**, and there is no `5.8/` tree. Leave it at `"5.7"` and point that block at the 5.8
  install. Mislabelled but functional.
- `editor_cmd` must be the **`-Cmd`** binary. `run_assemble.ps1` rewrites it to
  `UnrealEditor.exe` itself for stage 00, which needs the GUI editor's tick loop for async
  cloud texture downloads. Putting `UnrealEditor.exe` there breaks stage 01.
- Blender has no version gate anywhere in the stage scripts, and 4.4.1 was verified directly:
  `io_scene_gltf2` enabled, all 13 export params accepted, FBX importer present,
  `extern_draco.dll` present. Stage 03 sets `CYCLES` explicitly before baking, so the
  EEVEE-Next change in 4.2 does not bite.

UE 5.8's MetaHuman Python API was diffed against upstream's committed 5.7.4 probe baseline: 13
UCLASS checks identical, subsystem methods purely additive (24 added, 0 removed, 0 renamed),
property names identical. Only the enum above actually broke.

---

## The re-bake trap

**Read this before changing anything in stage 00's save path.**

`new_assets` is a set-difference of SkeletalMeshes before vs after the build. It is **empty on a
re-bake** — the assets already exist, so nothing is "new". Any save logic keyed off that list
does nothing at all on the second and every subsequent bake.

That is not hypothetical. The first fix here derived its save folders from `new_assets`; it
worked perfectly on a first bake, and when a character was edited in MetaHuman Creator and
re-baked, it rebuilt in memory, saved nothing, and still reported `DONE` — the same silent
success as the bug it replaced, reached by a different route.

**Every character edit after the first is a re-bake.** The broken path was the common one.
`save_dirty_packages` does not care where the build wrote or whether the assets already
existed, which is why it is the right mechanism.

---

## Non-fatal only when nobody is looking

Stage 00's thumbnail step was written to be non-fatal: it calls `extract_thumbnail.py`, checks
`$LASTEXITCODE`, and prints "non-fatal, continuing" on failure. The script is absent from
upstream, so that path ran constantly and harmlessly.

It stops being harmless the moment a caller **captures output**. PowerShell turns a native
command's stderr into an `ErrorRecord` only when the stream is redirected, and
`run_assemble.ps1` sets `$ErrorActionPreference = "Stop"` at the top — so under a batch runner
or CI the script terminates *before* reaching its own guard. Observed: stage 00 aborting in
under a second for four characters on a missing file it was explicitly written to tolerate,
having worked fine every time it was run by hand.

The general shape is worth remembering: **a guard that only holds interactively is not a
guard.** Anything relying on `$LASTEXITCODE` after a native call in a `Stop` script needs the
call wrapped, or its stderr neutralised, or it will behave differently in automation than in
the terminal where it was tested.

## Things that look like bugs and are not

- **`can_build_meta_human: False` at rest** is expected. Stage 00's lifecycle is
  `request_texture_sources` (async, Epic cloud) → wait for the flag → `build_meta_human`.
- **Missing eyelashes** usually means the character asset has `eyelashes: { type: None }` — no
  style assigned in MetaHuman Creator, so no coverage texture, so nothing to draw. Set a type
  and re-bake; every downstream link then works untouched.
- **`preview.png` showing a bald character.** Stage 04's headless render validation reports
  `OK` while producing a preview with no hair, reproducibly. The asset is fine. Serve `docs/`
  over http and look in a browser; `file://` fails on CORS and looks worse still.
- **`extract_thumbnail.py` not found.** Referenced by `run_assemble.ps1`, absent from upstream.
  Non-fatal; the thumbnail step just fails.

---

## Publishing to the platform

`tools/publish_characters.ps1` copies a bake into the monorepo's `public/models/<id>/`, which
serves local development. `tools/publish_to_cdn.ps1` publishes the same directory to the
platform, and it goes through the admin API — `POST /api/admin/avatars/<id>/companion-directory`
— rather than copying to the bucket.

That indirection is plan D4: the admin API stays the one front door for assets. It also
validates what a copy cannot — that the sidecar is present, and that every texture the sidecar
NAMES was actually uploaded — so a half-copied directory fails at publish instead of rendering
wrong later.

Objects land at `avatars/<avatarId>/<bakeHash>/` with the bake hash derived from content, so a
re-bake publishes alongside the live one and rollback is repointing a database row. The script
prints the `modelKey` and the SQL to go live; it deliberately does not run it.

Two things that will bite:

- **The route needs an operator session today.** `withAdminAuth` accepts only an interactive
  NextAuth login, so the bake box cannot yet publish unattended (monorepo plan G14).
- **The character id comes from the model FILENAME**, so keep publishing `<id>.glb`. Name it
  anything else and every per-character calibration silently reverts to defaults, because the
  published directory is a bake hash rather than the id.

---

## Re-syncing with upstream

Category A changes may conflict if upstream fixes the same things independently — take theirs
where equivalent. Category B changes are additive and should survive a merge, but they live in
the same functions as upstream's stage-02 logic, so expect conflicts in `import_glb.py`.

Every Telegamez change is marked with a `TELEGAMEZ` comment at the site, with the reasoning
inline. Search for it before merging.
