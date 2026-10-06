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
| Groom MIs resolved the same way (telegamez#3701) | The groom dump still read the override array. Every head-hair cards instance MetaHuman Creator writes inherits its colour from the plugin parent, so it was dropped, stage 02 logged `mi=None`, and every head groom shipped a hard-coded fallback brown. Measured on Julius: effective melanin 0.80 / redness 0.25; sidecar (0.18, 0.10, 0.05). The dump now records effective values plus the instance's own overrides, and stage 02's colour synthesis honours `WhiteAmount` and `Highlights`. |
| Hull normals on card grooms (telegamez#3701) | UE exports each card with its own plane's normal (within 14° of the face normal, median, on ~4,600 Coil cards), so a PBR renderer lights every card separately — the patchwork and glitter. Stage 02 transfers the scalp's normals onto every card mesh (Data Transfer, nearest face interpolated) and the sidecar declares `hair_normals: "hull"`. `MH_HULL_NORMALS=0` disables it. The reference viewer honours the flag; see the sequencing note below. |
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

## Hair and grooms (telegamez#3701)

Three things changed for every card groom, and one of them has an ordering constraint.

- **The colour is real now.** See the table row above. `mh_materials.json` also carries an
  `mh` block per hair material with MetaHuman's own shading parameters (Spec0/Spec1, the
  roughness pair, melanin variation, card opacity gain, white amount, highlights), so a runtime
  can converge on the editor's look instead of being hand-tuned blind per character.
- **Cards carry hull normals.** The sidecar says `hair_normals: "hull"` (or `"card"` when the
  transfer was disabled or found nothing). three.js flips the normal on the back face of a
  double-sided material; that is right for a flat card and wrong for a hull normal, which
  already points out of the volume. A renderer must skip the flip on a hull bake. The reference
  viewer (`stages/04-webview-build/templates/viewer.js`) does; the platform runtime change is
  tracked on the same ticket.
- **Sequencing.** Do not publish a hull-normal bake to an environment whose runtime still flips
  back-face normals on hair — those cards render dark from behind. Runtime first (panda before
  beta, as always), then re-bakes.
- **WebP atlases, opt-in.** `compress_companion.mjs --texture-format webp` converts the sidecar
  textures (lossless for normal and tangent maps, q95 otherwise) and writes
  `texture_format: "webp"` into the sidecar. Measured on Julius's hair atlas: 2048 PNG 5.9 MB,
  4096 WebP q95 4.7 MB, 2048 WebP q95 1.5 MB. The publisher refuses such a build until
  `TG_WEBP_RUNTIME_READY=1`, because the runtime appends `.png` to atlas stems itself until it
  reads `texture_format`. Same shape as the meshopt gate, for the same silent-failure reason.

---

## Accessories, masked clothing and unweighted Fab outfits (telegamez#3725)

Tommy (`/Game/tommy`: Fab *Roman Soldier* Chaos outfit plus a horsehair crest worn as a
legacy **SkeletalMesh wardrobe item**) exposed four assumptions the pipeline made about a
MetaHuman. None of them is about him; they are what any accessory item, masked garment or
Fab outfit hits. All four changes are default-off for the existing roster.

| Change | Why it is general |
|---|---|
| Stage 01 discovers skinned meshes from the built `BP_<Name>` (`_discover_from_blueprint`) and merges them with the folder listing | The SkeletalMesh wardrobe pipeline references its asset *in place* (`/Game/Characters/RomanPlume/SK_RomanPlume`) and copies nothing into the build folder, so a folder listing can never see it. The outfit pipeline does copy into `Clothing/`, and that difference is the role rule: `Body`→body, `Face`→face, `SkeletalMesh*` inside the build→outfit, outside→**accessory**. No Blueprint, or one without body and face, falls back to the old listing. |
| `export_fbx_weights.py` includes accessories | Same corrupt-glTF-weights problem, same fix; `dump_fbx_weights.py` and the `auto=auto` mapping already enumerate every `*_weights.fbx`. |
| Stage 01 records `blend_mode`/`two_sided` per clothing material; stage 02 writes `blend_mode`, `alpha_clip`, `alpha_channel`, `alpha_source` and `double_sided` on `clothing` records whose material is not opaque (every MetaHuman garment is two-sided in Unreal, so opaque records are left exactly as before) | The GLB cannot carry a Masked blend mode, and a card-mesh accessory is nothing else. Plain `Material`s (hand-built accessories have no instance) now resolve their textures via `get_used_textures` and default `Color1` to white instead of being dropped as "unmatched". The **runtime** side (alpha test on clothing, and not flipping back-face normals on `double_sided` records, as #3703 did for hair) is a separate telegamez change with the same deploy-before-publish gate. |
| Stage 02 accepts Fab's spelled-out map names (`_BaseColor`, `_Normal`, `_OcclusionRoughnessMetallic`) alongside `_BC`/`_N`/`_ORM` | Every Fab garment material is a plain `Material` sampling maps named that way; with the suffix rule alone the whole Roman outfit came out unmapped. |
| `dump_fbx_weights.py` imports the weight FBX with `ignore_leaf_bones=False` | Unreal's FBX carries no marker end bones, so Blender's leaf-bone option drops real bones that have no children and folds their weights into the parent. The MetaHuman build reduces each clothing mesh to its required bones, which leaves `head` a leaf: Tommy's crest and helmet came out bound to `neck_02`. Roster outfits carry no head weights, so nothing showed. |
| The garment `Head`→`Neck` remap (stage 03 fold and `solve_skin.py`) exempts vertices that are at least 75% `Head`-weighted (`RIGID_HEAD_SHARE`), but only on a mesh where at least 5% of the vertices are Head-dominated (`RIGID_HEAD_MESH_FRACTION`) | The remap keeps collars off the runtime's procedural nods. A helmet is a garment mesh too, and it must ride those nods with the face and the crest; without the exemption Tommy's helmet solved to 79% Neck and would drift away from its own crest on every head turn. A polo collar has a handful of vertices above the per-vertex threshold (8 of 14,110 on tg-tom); the per-mesh gate keeps the collar rule on every one of them, so roster outfits solve exactly as before. Tommy's outfit mesh is 21% Head-dominated. |
| Stage 03 hide-body: a vertex whose own outward ray already hits cloth is never tucked; the opening veto on the tuck is relative to the poke (`OPENING_VETO_FLOOR` 5 mm, `OPENING_VETO_RATIO` 3×) while face deletion keeps the absolute 20 mm veto | The tuck decided "pushed through" from the nearest cloth surface alone. Fab armour is double-walled, so for a cheek sitting properly inside the helmet the nearest surface is the inner wall, whose normal faces the head: the cheek counted as outside and was moved 4 mm past that wall, straight out through the outer one. Tommy's cheek patches were manufactured by the pass (1,140 of 1,148 helmet tucks; his face is clean before it). Ray-covered means hidden whichever surface is nearest. The relative veto exists for skin that really pokes through a small plate, where the absolute 20 mm veto treated the whole plate as an opening. Roster effect: tg-tom's only tucks were 2 such mis-tucks on the chinos and boots, now left alone; nothing else changes. |
| Character manifest key `texture_overrides: {"<stem>": "<png path>"}` - stage 03 ships the listed file in place of the Unreal export for that sidecar texture (same cap and de-alpha); a missing file is an error | One Unreal asset, two companions: `bootstrap_character.py --asset /Game/tommy --id tommy-gold` plus eight overrides gives Tommy-Gold polished-gold armour (`accessories/roman_plume/gold`, made by `make_gold_textures.py` from the embossed maps: base colour recoloured to a gold reflectance keeping its detail, ORM roughness compressed to 0.08 + 0.35x, metallic 1; normal maps untouched so the SC emboss survives) without touching the Fab assets in the project. Relative paths resolve against the character directory, then the workspace. |
| Stage 01 writes `groom_materials` and `clothing_materials` sorted; stage 02 picks the eyelash MI whose style matches the exported coverage texture | The AssetRegistry returns assets in a different order on every run and stage 02 took the first name match. Two same-day bakes of the same Tele build resolved the eyebrows to `_Facial_Hair` on one and `_Hair` on the other, and the lashes to `Eyelashes_S_Fine` (a stale instance) on one and `Eyelashes_S_Sparse` (what the character wears) on the other. Sorting makes a bake reproducible; the coverage texture makes the lash pick correct. |
| `reweight_from_fbx.py` repairs unweighted vertices from their nearest weighted neighbour | Fab outfits ship them (Roman Soldier: 24 breastplate + 56 greave vertices; Unreal raises the `ClothAsset.cpp:552` ensure on every build). They bind to root and spike under animation. Logged loudly with the count and the farthest copy distance. |

New character-manifest keys (all optional):

- `extra_assets`: `[{"asset_path": "/Game/...", "role": "accessory"}]` for anything discovery cannot see. A missing asset is an error, not a skip.
- `wardrobe_overrides` unchanged; it applies to accessory materials too since they are matched by MI name substring.

Stage 03's `target_tri_budget` is still advisory and `max_texture_px` still global; a per-character override and an opt-in "delete body faces under rigid armour" pass are tracked on the same ticket and not in this change.

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
