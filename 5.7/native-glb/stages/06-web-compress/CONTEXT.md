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



## Fidelity — meshopt is NOT free, and this is the important caveat



Compression that changes the face is worse than a large download. Measured by comparing

morph-target deltas before and after, permutation-invariantly (meshopt reorders vertices

for cache locality, so a straight index-by-index comparison reports errors the size of the

signal and means nothing):



| target peak displacement | targets | median RMS error | worst RMS error |

|---|---|---|---|

| >= 10 mm | 79 | **0.85 mm** | 4.58 mm |

| 3 - 10 mm | 100 | 0.32 mm | 1.76 mm |

| 1 - 3 mm | 26 | 0.18 mm | 0.25 mm |

| 0.3 - 1 mm | 25 | 0.03 mm | 0.15 mm |

| below 0.3 mm | 413 | ~0 | 0.03 mm |



A control — the same model read and written with no transforms at all — measures

**0.0000 mm** in every bucket, so the numbers above are the compression and not the

measuring.



86 targets are zeroed outright, but the largest of them moved 0.057 mm, which is nothing.

Separately, **300 of the 643 morph targets are already all-zero in the source** and are

pure payload; removing those upstream is free and lossless, unlike anything here.



Raising `--quantize-position` from 14 to 16 bits does **not** help the large targets

(2.35 mm against 2.38 mm peak error) and costs 0.06 MB, so the loss is in meshopt's own

morph encoding rather than in that setting.



**So do not claim "nothing visible changes" on the strength of the file size.** 0.85 mm RMS

on a head roughly 230 mm tall is about 0.4% — probably sub-pixel in a video-chat crop, but

that is an argument, not a measurement. Put it through `/dev/companion-capture` on two

companions the way #3659 did before shipping it.



## Before publishing — read this



**Meshopt needs no runtime change** on the `useGLTF` path, which is what actually loads

companions. drei 10.7.6's `useGLTF(url, useDraco = true, useMeshopt = true, ...)` calls

`setMeshoptDecoder` by default, and `three-stdlib@2.36.1` exports `MeshoptDecoder`.

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

