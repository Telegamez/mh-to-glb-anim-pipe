# Stage 05 — Animation Bake (MetaHuman body loops → shared animations GLB)

## What this stage produces

One **animation-only GLB** (`mh-animations.glb`) carrying the conversational body loops for
MetaHuman companions: the D3 set — Idle, Listening, Thinking, Focused, Speaking, plus Nod,
HeadShake, Shrug, Dismiss. The runtime binds these clips onto any companion **by node name**,
exactly as the existing Avaturn `animations.glb` works.

Unlike stages 00–04 this is **not per-character**: one shared file serves the roster, because
clips bind by name and all six characters share MetaHuman skeleton topology. (Their rest poses
differ per bone — measured 2026-08-17 — which is the same situation the Avaturn fleet already
tolerates; rotation tracks apply across same-topology skeletons.)

## Why the bone rename is the heart of it

The platform contract renames 16 MetaHuman bones to Mixamo-family names at stage 02
(`CONTRACT_BONE_RENAMES` in `import_glb.py`). A raw UE animation export names **MetaHuman**
bones — which would bind to everything on our models *except* those 16, the exact inverse of
the Mixamo problem (which binds *only* those 16, leaving all 30 finger bones rigid).

So this stage rewrites every action's f-curve paths through the same 16-pair map. The result
binds contract names for the 16 AND MetaHuman names for everything else — fingers included,
which is the fidelity point of going native. The map is **duplicated** here
(`CONTRACT_BONE_RENAMES` in `build_animations_glb.py`) with a pointer to the stage 02 source;
if stage 02's map ever changes, this stage must change with it.

## Inputs

| Input | Where | Notes |
|---|---|---|
| `animset.json` | this directory | clip name → UE AnimSequence asset path. **Paths are empty until the Fab pack (decision D7) is installed**; the runner exits loudly listing unfilled entries |
| UE project | `_config/pipeline.yaml` → `project_path` | the pack's AnimSequences must be inside it |
| UE editor | `pipeline.yaml` → `editor_cmd` (the `-Cmd` binary) | same binary rule as stage 01 |
| Blender | `pipeline.yaml` → `blender_exe` | `--factory-startup`, like every Blender stage |

## Outputs / verification (assert on artifacts — §6.1)

- `_work/fbx/<Clip>.fbx` per clip — the runner fails the clip list if any is missing or empty
- `docs/animations/mh-animations.glb` — and, with `-PublishTo`, a copy beside the web models
- The Blender script logs, per clip: tracks total, tracks renamed, and the **bound-node count
  the harness should report**. The harness `binds N/M` readout is the acceptance check:
  a native file should read at or near 100% on a MetaHuman model, against Mixamo's 14/65.

## Run

```powershell
# from 5.7/native-glb
./stages/05-animation-bake/tools/run_animbake.ps1
# optionally publish straight to the web app:
./stages/05-animation-bake/tools/run_animbake.ps1 `
  -PublishTo S:\Avatars\metahuman\telegamez\packages\web-next\public\models\metahuman-shared
```

Then in the harness: paste `/models/metahuman-shared/mh-animations.glb` into the body-animation
panel's *animations url* and compare against the Mixamo baseline.

## Traps known in advance

- **Nothing else may hold Unreal** (30010 listener, same as stages 00/01).
- AnimSequences must target the **MetaHuman body skeleton** the companions were built from.
  **The UE5 Mannequin trap is worse than predicted** (measured 2026-08-17): Manny shares the
  core bone names, so a Manny-rigged clip does NOT bind-nothing — it *half-binds with wrong
  proportions*, which looks like a subtle quality problem instead of an obvious failure. The
  build therefore refuses any clip without `FACIAL_`/`_bulge_` marker curves (present on every
  real MetaHuman clip, absent on Manny). The one Manny clip in the first pack was caught this
  way, not by eye.
- **Interchange is glTF end to end, never FBX.** v1 exported FBX and shipped centimetre tracks
  in alien bone frames — detached head, sheared mesh. The UE half uses stage 01's glTF option
  set (`export_uniform_scale 0.01` = metres) and the Blender half imports GLB, so units and
  bone-local frames match the character pipeline by construction. The units gate (Hips rest
  0.3–2.0 m) makes the v1 failure unshippable.
- **The Blender glTF importer stashes its own NLA track** on the armature it creates, named
  `<AssetName>_0`, and the NLA_TRACKS exporter ships every track it finds. Two runs leaked an
  extra clip this way; the keeper's tracks are wiped before the stage's own are added, and the
  clip-set gate compares the finished GLB against animset.json either way.
- Clip names in `animset.json` are the names the runtime's state map keys on
  (`AgentAnimationContext` / `stateAnimationMap`). `Idle` specifically is also the
  root-motion pin reference in `stabilizeRootMotion` — do not omit it.
