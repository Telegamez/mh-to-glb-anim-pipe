# Studio HDRI — companion lighting environment

The baked equirectangular environment that lights every companion on every surface.
Ticket **#3660**; authored to
`_docs/Architecture/Companions/metahuman/studio-hdri-authoring-spec.md` in the `telegamez`
repo.

```powershell
./Render-StudioHdri.ps1
```

Outputs land in `out/`. Takes about a minute on the bake box's GPU.

## What ships

| File | What it is |
|---|---|
| `companion-studio-v1.hdr` | **the deliverable** — 1024 × 512 Radiance, ~740 KB |
| `companion-studio-v1.exr` | same pixels, 32-bit OpenEXR, for archival |
| `companion-studio-v1-2048.hdr/.exr` | 2048 × 1024 master |
| `companion-studio-v1.json` | sidecar: every value used, so a render is reproducible |
| `companion-studio-v1.blend` | the scene, for the next revision |

1024 × 512 is what the spec asks for: the runtime convolves any input down to a 256-pixel
cube map, so detail above this is discarded. The 2048 master exists only so a future
revision can be re-derived without re-rendering.

## The numbers are not typed in here

The spec prints panel positions in metres, but says the real source of truth is
`packages/web-next/src/services/RoomManager/Agent/materials/studio-lighting.json`, which
describes panels as **directions** — azimuth, elevation, angular size — and that the JSON
wins on any disagreement.

So `author_studio_hdri.py` builds from the JSON's angles and places panels on a sphere of
radius **4.0 m**. That reproduces the spec's metre table exactly, all five panels, to the
0.01 m it is printed at:

| panel | spec (x, y, z, size) | from JSON at r=4.0 |
|---|---|---|
| key | −2.57, −2.16, 2.18, 2.14 | −2.57, −2.16, 2.18, 2.14 |
| fill | 2.87, −2.77, −0.28, 5.20 | 2.87, −2.77, −0.28, 5.20 |
| rim | 1.58, 3.55, 0.97, 1.41 | 1.58, 3.55, 0.97, 1.41 |
| hair | −1.34, 2.32, 2.97, 2.14 | −1.34, 2.32, 2.97, 2.14 |
| catchlight | −0.48, −3.90, 0.76, 0.84 | −0.48, −3.90, 0.76, 0.84 |

Pass `--lighting-json <path>` to read the live file; there is an embedded copy of the same
values so the script still runs without the sister repo checked out.

## The two judgement calls

Everything else came from the spec. These two did not, and both are in the sidecar.

**Surround albedo (0.05 walls, 0.02 floor).** The spec gives the surround colours as
*emission* — "walls and ceiling `#151a24` at emission 0.007". A pure emitter does not
reflect, so a surround built only from those numbers would glow without bouncing, and
bounce is the entire reason for the ticket. The surround is therefore emission exactly as
specified **plus** a diffuse albedo, and that albedo had to be chosen. `#151a24` used
directly as albedo is ~0.01 linear — darker than any real paint, and effectively no bounce.
0.05 is a dark studio wall. The floor is lower again, per constraint 3.

**Emission gain (×10).** Constraint 5 says panels must stay above 1.0 so the float headroom
survives, but at the spec's own numbers the key sits at exactly 1.0. Every emission is
multiplied by 10, so ratios are untouched and the key clears 1.0 comfortably. The spec is
explicit that absolute brightness does not matter and the import step re-normalises.

## Softbox housings

`key`, `rim` and `hair` get a shallow open-fronted dark box whose side walls stand slightly
proud of the emissive face. That lip shades the panel at grazing angles, so the source
feathers at its edges instead of being uniform to the edge — the "real softbox falloff" the
ticket opens with. It is built from five planes, not a cube, because a cube's front face
would sit coplanar with the panel and occlude it.

`fill` has none: at 66° it is a bounce wall, not a fixture. `catchlight` has none either —
constraint 2 keeps it, and "no recognisable geometry near the camera axis" means nothing
but the bare source belongs there, or it shows up as a shape in the eye reflections.

## Checking a render

```powershell
python verify_studio_hdri.py out/companion-studio-v1-2048.hdr --lighting-json <path>
```

It measures the file rather than trusting the scene: finds the bright regions, identifies
each one by elevation (all five spec elevations are distinct, so this needs no orientation
assumption), and reports position, solid angle and luminance ratio against the JSON.

Last run:

```
value range     min 0.00001   median 0.00473   max 15.94
headroom        4.64% of pixels above 1.0   OK - not clamped
surround        median/peak = 0.00030  (dark)

panel         spec el  meas el  spec size  meas size  want ratio  meas ratio     err
key              33.0     33.4       30.0       30.0       1.000       1.000    0.0%
fill             -4.0     -4.2       66.0       62.9       0.027       0.027    1.7%
rim              14.0     14.1       20.0       20.2       1.741       1.717    1.4%
hair             48.0     48.7       30.0       29.9       0.723       0.725    0.3%
catchlight       11.0     11.0       12.0       12.2       0.199       0.197    1.1%
```

"want ratio" is not the JSON's `intensity`. Intensity is a radiometric scalar and the
panels are different colours, so a rim at 1.8 in a cool blue does not measure 1.8× a warm
key. The column is `intensity × relative luminance of the colour`, which is what a
luminance measurement should actually equal.

## Orientation

Measured, not assumed. The verifier solves the longitude convention from the panel
positions it finds, and this file is:

```
u = 0.5 - azimuth/360      (consistency 1.0000, offset 0.5000)
```

That is: the centre of the image is azimuth 0 — straight toward the viewer — with azimuth
increasing to the left, and +Z up. Three.js's `EquirectangularReflectionMapping` puts
`u = 0.5` on **+X** instead, so a loader that applies no rotation will have the rig turned
90° about the vertical. The spec says the import step owns conversion and orientation; this
is the number it needs.

## Reviewing it

`/dev/hdri-harness` on the local stack (`telegamez` repo, `packages/web-next`). It loads a
real companion, lets the MetaHuman material profile attach exactly as it does in a room,
then swaps **only** `scene.environment` between the coded environment we ship today and
this file. `E` blinks between them; "measure" reports face contrast, exposure and
silhouette separation against the ticket's bar.
