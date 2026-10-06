# Stage 06 — Web compression



Takes stage 03's GLB and its sidecar textures and produces the payload a phone

actually downloads. Ticket **#3662**.



## Why this stage exists



Measured on the local bake of `tele`, and against the published `mh-tele` with

`scripts/media/audit-companion-payload.mjs` in the telegamez repo:



| | published `mh-tele` |

|---|---|

| model `tele.glb` | 47.78 MB |

| — of which morph targets | **34.67 MB (73%)** |

| — of which embedded images | 11.05 MB (23%) |

| — of which Draco geometry | 1.30 MB (3%) |

| sidecar clothing textures (10 PNG) | 39.14 MB |

| **total per companion** | **86.93 MB** |



The geometry is already Draco-compressed and is not the problem. Nearly three

quarters of the model is uncompressed FLOAT32 morph-target data, and the sidecar

textures are uncompressed PNG — and in the local bake they are 4096 × 4096 RGBA,

including normal and ORM maps that have no use for an alpha channel.



Beta currently serves only Avaturn avatars at ~11.5 MB each, so shipping

MetaHumans as they stand multiplies the mobile payload by roughly eight.



## One implementation, two callers

The work is done by `5.7/native-glb/tools/compress_companion.mjs`, which the **publisher**
also drives (its Compress button, and `tools/compress_companion.ps1`). This stage is a thin
wrapper that points it at the pipeline's folder layout. A stage with its own copy would
drift from the publisher's, and the drift would surface as "the bake compressed differently
from the publish".

## Scope (hard rule)



Same as every other stage: run the launcher, verify its outputs. Do not modify

pipeline code. This stage writes only to `characters/<id>/06-web/` and never

touches `03-glb/`.



## Inputs



| Source | File | Why |

|---|---|---|

| Stage 03 GLB | `characters/<id>/03-glb/<id>.glb` | the model |

| Stage 03 textures | `characters/<id>/03-glb/textures/*.png` | sidecar clothing/hair maps |

| Stage 03 sidecar | `characters/<id>/03-glb/mh_materials.json` | texture references |



Requires **node** and the **`ktx` CLI** from KTX-Software 4.4+.

`gltf-transform` is fetched by `npx` at a pinned version.



KTX-Software **4.4.2 is installed on the bake box** at

`C:/Program Files/KTX-Software/bin`, on the machine PATH, so no flag is needed.

On a machine without it, install from

<https://github.com/KhronosGroup/KTX-Software/releases> — note the Windows

installer's `/S` silent mode does **not** add itself to PATH, so either add

`<install>/bin` by hand or pass `--ktx-bin <dir>` to the stage.



## Process



```powershell

./tools/run_compress.ps1 -Char tele            # keep sidecar resolution

./tools/run_compress.ps1 -Char tele -TexturePx 1024

```



The recipe, in this order:



1. `uastc` on normal / occlusion / metallicRoughness

2. `etc1s` on everything else

3. `meshopt` on geometry and morph targets



### Three things about that order that cost time to find



**Texture codecs must run before meshopt.** gltf-transform decodes

`EXT_meshopt_compression` whenever it has to touch a file and does not re-apply

it. Running a texture pass afterwards produced a file *larger* than its input —

16.59 MB went to 37 MB.



**UASTC for normal and ORM, ETC1S for albedo.** ETC1S fits per-block endpoints,

which mangles normals and the three independent channels of an ORM. Those also

need `--assign-tf linear`; they carry linear data and the encoder otherwise

assumes sRGB and applies a transfer that was never there. ETC1S also cannot be

combined with `--zstd` — BasisLZ carries its own entropy coding and the encoder

refuses the combination outright.



**Do not add `dedup`.** It is the obvious next step and it is wrong here: on this

asset it saved *exactly zero bytes* while merging 17 materials into 10 and 12

meshes into 11. `metaHumanMaterials.ts` matches materials **by name** to decide

what is skin, hair or eye, so merging them produces a companion with no hair tint

and no clothing textures — which reads as a bad bake, not as a compression bug.



## Outputs



| Artifact | Location |

|---|---|

| Compressed GLB | `characters/<id>/06-web/<id>.glb` |

| KTX2 textures | `characters/<id>/06-web/textures/*.ktx2` |

| Rewritten sidecar | `characters/<id>/06-web/mh_materials.json` |

| Manifest | `characters/<id>/06-web/web_compress_manifest.json` |



Measured on `tele`: model **47.48 MB → 10.65 MB**. Sidecar PNGs drop about 60%

(56.71 MB → 21.68 MB at 2048 px; 15.32 MB → 6.36 MB at 1024 px).



## Verification



The stage aborts if mesh, primitive, morph-target or material counts change —

those are not compression results. On `tele` all 571 morph targets, 17

primitives, 17 materials and 18 images survive unchanged.



One structural change is expected and benign: meshopt un-shares a single skin

into one per primitive (1 → 9, joints 1185 → 10665, the same list nine times).

That costs roughly 680 KB of inverse-bind matrices and changes nothing visually.



Independently, from the telegamez repo:



```bash

node scripts/media/audit-companion-payload.mjs --model <url>

```



Note that tool reports morph sizes from `bufferView.byteLength`, which for a

meshopt file is the **decoded** size — it will show percentages over 100% on a

compressed model. That is the tool being honest about a format it predates, not

a bad bake; the file size is the ground truth.



## Fidelity — meshopt is lossy, and was checked rather than assumed

Compression that changes the face is worse than a large download, so this was measured
twice and then looked at.

Morph-target deltas, compared permutation-invariantly (meshopt reorders vertices for cache
locality, so an index-by-index comparison reports errors the size of the signal and means
nothing):

| target peak displacement | targets | median RMS error | worst |
|---|---|---|---|
| >= 10 mm | 79 | 0.85 mm | 4.58 mm |
| 3 - 10 mm | 100 | 0.32 mm | 1.76 mm |
| below 0.3 mm | 413 | ~0 | 0.03 mm |

A control -- the same model read and written with no transforms -- measures **0.0000 mm** in
every bucket, so those are the compression and not the measuring.

**But the morph error is not the main effect.** Rendering at rest and again at full
`jawOpen`, base-geometry quantisation accounts for ~87% of the visible difference: meshopt
quantises base vertex positions too, so the change is present in every frame regardless of
expression. It concentrates on **hair- and eyelash-card edges**, where a sub-pixel shift
becomes a whole pixel. Peak 52/255 on 0.5% of a close-up frame.

**Verdict: not visible.** Both models were loaded side by side through the real runtime --
`/dev/model-ab` in the telegamez repo, with the MetaHuman material profile, per-character
hair calibration and clothing textures all live -- and blink-compared. Indistinguishable.

A Blender render agreed, but could not settle it alone: it does not run the hair shader,
which is precisely where the differences are.

## Before publishing — read this



**Meshopt needs no runtime change** on the `useGLTF` path, which is what actually loads

companions. drei 10.7.6's `useGLTF(url, useDraco = true, useMeshopt = true, ...)` calls

`setMeshoptDecoder` by default.

`TalkShowCompanionAvatar` uses `useGLTF`, so the talk-show, capture and harness surfaces

already accept `EXT_meshopt_compression` today.



Two things still block the KTX2 half:



1. **No `KTX2Loader`.** Sidecar textures load through a plain `THREE.TextureLoader` — the

   injectable `loadTexture` in `metaHumanSurface.ts`, which is the single place to add one.

   Embedded KTX2 in the GLB needs `setKTX2Loader` on the loader as well, which drei does

   *not* do by default.

2. **`alpha_stem` / `tangent_stem` resolve to `.png` in the runtime**, not in the sidecar —

   `metaHumanMaterials.ts` appends the extension itself (lines ~182, 203, 206). This stage

   rewrites the explicit `textures.*` paths and deliberately leaves the stems alone,

   recording the count in the manifest, because a stem with an extension bolted on would

   break the current runtime without telling anyone.



`Agent3DHead.tsx` and `SupportTele3DHead.tsx` build their own `GLTFLoader` and set only a

Draco decoder, so those two surfaces would need `setMeshoptDecoder` adding if they are ever

pointed at a compressed model.



The honest sequence: ship meshopt-only first (it is the 34.7 MB of morph data, needs no

loader work, and is the bulk of the win), verify on panda, then do KTX2 with the loader

change. Panda is where new work is proven; beta is production.

## KTX2: `--mode ktx2` (telegamez#3662)

The two blockers above are lifted in telegamez branch `3662-ktx2-browser-loader`: one shared
`KTX2Loader` on every companion load path, and `browser_texture_format: "ktx2"` in the sidecar
telling the browser to fetch each texture's `.ktx2` sibling.

**Why a second mode rather than fixing `full`.** The native compositor reads the same published
files and decodes textures with the `image` crate (PNG/WebP only). A KTX2-only build renders in
a room and fails on the TV and program output. `ktx2` therefore carries both:

| Output | For |
|---|---|
| `<id>.glb` — KTX2 textures inside, meshopt | the browser (`variants.room`) |
| `<id>.compositor.glb` — PNG textures inside, meshopt, same as the `meshopt` build | the compositor (`variants.compositor`) |
| `textures/*.png` + a `.ktx2` sibling for each | PNG for the compositor, KTX2 for the browser |
| `mh_materials.json` — paths unchanged, plus `browser_texture_format: "ktx2"` | both |
| `compression_manifest.json` — records `variants: { room, compositor }` | the publisher |

**Publishing it needs the avatar manifest.** The publisher must send `variants` as the upload
route's `manifest` field (it refuses a `manifest.json` sent as a file). Without
`variants.compositor` the compositor falls back to the room model and cannot decode it. That
publish step does not exist yet.

**Colour or data.** UASTC with a linear transfer for every data map, ETC1S sRGB for colour.
Model: `normalTexture`, `occlusionTexture`, `metallicRoughnessTexture`, `specularTexture`
(`KHR_materials_specular` intensity, on skin, body and teeth). Sidecar, by stem: `_N`, `_ORM`,
`_Tangent`, `_Attribute` (hair/brow card coverage and depth) and `_Coverage` (eyelashes). The
earlier `full` mode sent `_Attribute`, `_Coverage` and `specularTexture` to ETC1S as sRGB.

Measured on `tg-drake` (2026-10-05 rebake): per browser visit 72.27 MB → 27.33 MB (room model
48.39 → 12.51 MB, sidecar 23.89 MB PNG → 14.82 MB KTX2). The compositor model is 17.67 MB.

**Visual check: `/dev/model-ab`, A = meshopt+PNG, B = this build.** No PNG requested from
the KTX2 directory, every `.ktx2` and the transcoder 200, no console errors. Face crops matched
frame for frame: mean difference 1.8–4.4/255. Desktop GPU (S3TC only), so the transcode target
was BC, not the ASTC/ETC2 a phone gets. A phone check is still required.

**Harness trap found doing this.** Load two DIFFERENT builds into `/dev/model-ab` and only the
one that attaches first casts face and hair shadows: `attachShadowKey` returns early once the
scene has a key, so the second model's head and hair never get `castShadow`. KTX2 transcoding
makes the KTX2 build attach second every time, which looked exactly like "KTX2 loses the braid
shadows". Compare each build loaded into BOTH slots, one run per build.

## WebP sidecar textures (telegamez#3701)

`compress_companion.mjs --texture-format webp` (modes `none` and `meshopt`) converts the sidecar
PNGs to WebP — lossless for `_N` and `_Tangent` maps, q95 for everything else — rewrites the
explicit `textures.*` paths, and writes `texture_format: "webp"` into the sidecar and the
compression manifest. Atlas stems are left alone; the runtime appends the extension itself.

Measured on Julius's hair atlas: 2048 PNG 5.9 MB, 4096 WebP q95 4.7 MB, 2048 WebP q95 1.5 MB.

The publisher refuses a WebP build until `TG_WEBP_RUNTIME_READY=1`, for the same reason it
refuses `full`: until the runtime reads `texture_format`, the build uploads cleanly and renders
hair and clothing untextured. Needs `sharp-cli` through `npx` (online once), like gltf-transform.
