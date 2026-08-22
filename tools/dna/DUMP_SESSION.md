# Ground-truth dump session — one command, and how to know it worked

Produces Unreal's own evaluated pose for all 322 validation poses, which is what slice 5.3's
numbers get validated against and what unblocks 5.2.

Everything below is read from the tools, not from memory: paths from
`_dna/dna_export_report.json`, the AnimSequence's shape from `validation_anim_report.json`,
and the gate text verbatim from `export_pose_transforms.py`.

---

## Prerequisite (already done — nothing to do)

`AS_TG_Validation` exists at `/Game/TG_Face/AS_TG_Validation`, built 2026-08-22:

```
323 frames · 322 intervals · 263 curves · 30 fps · 10.733 s · errors [] · warnings []
```

If it is somehow missing, rebuild it with:

```powershell
$base = "s:\Avatars\metahuman\metahuman-to-glb\5.7\native-glb\stages\01-unreal-glb-export"
New-Item -ItemType Directory -Force C:\tmp\mh | Out-Null
Copy-Item "$base\tools\author_viseme_anim.py"        C:\tmp\mh\ -Force
Copy-Item "$base\config\tg_validation_poses.json"    C:\tmp\mh\ -Force

& "C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" `
  "G:\ue\telegames\telegames.uproject" -run=pythonscript `
  "-script=C:/tmp/mh/author_viseme_anim.py -- --poses=C:/tmp/mh/tg_validation_poses.json --report=C:/tmp/mh/validation_anim_report.json" `
  -AllowCommandletRendering -unattended -nosplash
```

> **Why `C:/tmp/mh` and forward slashes.** UE's `-script=` argument parser mangles a path
> containing `\5.7\` — it truncates at `metahuman-to-glb.7` and reports "Could not load Python
> file". Staging to a space-free path is what `run_export.ps1` already does for the same
> reason. Do not "simplify" this by pointing at the repo path.

---

## The command

```powershell
$UE   = "C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
$Proj = "G:\ue\telegames\telegames.uproject"
$Src  = "s:\Avatars\metahuman\metahuman-to-glb\5.7\native-glb\stages\05-animation-bake\tools"

New-Item -ItemType Directory -Force C:\tmp\mh | Out-Null
Copy-Item "$Src\export_pose_transforms.py" C:\tmp\mh\ -Force

$ErrorActionPreference = "Continue"   # see note below
& $UE $Proj -run=pythonscript `
  "-script=C:/tmp/mh/export_pose_transforms.py -- --mesh=/Game/Unpacked/Telly/Face/SKM_Telly_FaceMesh --anim=/Game/TG_Face/AS_TG_Validation --out=C:/tmp/mh/telly_validation_dump.npz --fps=30" `
  -AllowCommandletRendering -unattended -nosplash
Write-Host "UE exit code: $LASTEXITCODE"
```

> `$ErrorActionPreference = "Continue"` is deliberate. With `Stop`, PowerShell turns benign
> native stderr (OpenXR warnings, D3D12 chatter) into a terminating error and kills the
> launcher before the exit code is ever read. `run_export.ps1` carries the same note.

**Expected wall time:** the authoring step took ~14 minutes for 85k curve keys. The dump
samples 323 frames against ~870 bones twice each; budget similar, and it logs every 200 frames.

---

## The three gates it must print

These come from `export_pose_transforms.py` and exist because each corresponding failure
produced a plausible-looking file once. **A pass line, not the absence of an error, is the
success signal.**

| gate | what it proves | failure text |
|---|---|---|
| **1** | the mesh has a post-process Anim Blueprint, so rig logic runs at all | `GATE 1 FAILED: … has no post-process Anim Blueprint` |
| **2** | consecutive frames differ, so the animation was actually driven | `GATE 2 FAILED: every sampled frame is identical` |
| **3** | the evaluated pose differs from the **raw** AnimSequence pose, so the ABP contributed | `GATE 3 FAILED: the evaluated pose is identical to the raw AnimSequence pose` |

Gate 3 is the one that matters: it separates "an animation played" from "MetaHuman's rig logic
ran", and nothing downstream can tell those apart by looking at the file.

**The line to look for:**

```
GATES PASSED: frames vary, and the ABP moves bones by up to <N>cm against the raw sequence
```

`<N>` should be a few centimetres, not `0.00`. A pass with `0.01cm` would technically clear the
`1e-4` threshold while meaning the rig barely moved — worth a second look rather than a tick.

---

## The artifact

```
C:\tmp\mh\telly_validation_dump.npz
```

The tool prints its own size on success: `wrote … (NN.NN MB)`. Expect **roughly 8–25 MB** —
323 frames × ~870 bones × 10 floats, twice (raw and evaluated), compressed.

Contents (`numpy.savez_compressed`, six arrays):

| key | shape | meaning |
|---|---|---|
| `bone_names` | (~870,) | object array of bone names |
| `ref` | (~870, 10) | the mesh's reference pose |
| `times` | (323,) | sample times in seconds |
| `raw` | (323, ~870, 10) | the AnimSequence pose, **before** the ABP |
| `evaluated` | (323, ~870, 10) | the pose **after** the post-process ABP — this is ground truth |
| `meta` | (1,) | JSON: mesh, anim, abp, length, fps, units, `abp_delta_max_cm`, diagnostics |

Each 10-float row is `[tx ty tz, qx qy qz qw, sx sy sz]` in **centimetres, Unreal component
space**. Units matter: the cm/m trap has bitten this project before.

---

## Checking it worked, without trusting the run

Three checks. Run from anywhere with numpy:

```python
import json, numpy as np
d = np.load(r"C:\tmp\mh\telly_validation_dump.npz", allow_pickle=True)
m = json.loads(d["meta"][0])

# 1. shape agrees with the AnimSequence that produced it
assert d["evaluated"].shape[0] == 323, d["evaluated"].shape
print("frames", d["evaluated"].shape[0], "bones", d["evaluated"].shape[1])

# 2. the ABP actually contributed, and by a sane amount
print("abp_delta_max_cm", m["abp_delta_max_cm"])          # want: a few cm, not ~0

# 3. the FACIAL_* joints are the ones that moved
names = [str(b) for b in d["bone_names"]]
fac = [i for i, n in enumerate(names) if n.upper().startswith("FACIAL")]
moved = (np.abs(d["evaluated"][:, fac, :3] - d["evaluated"][0, fac, :3]).max(axis=(0, 2)) > 1e-4).sum()
print(f"FACIAL_* bones that move across the sequence: {moved}/{len(fac)}")
```

Check 3 is the one worth caring about. A dump that passes all three gates but moves only body
bones would be measuring the wrong thing entirely, and no gate catches that — the gates ask
whether *something* moved, not whether the *face* did.

**Then hand me the `.npz`** (or its path). Order from there is settled: diff the Python oracle
against it first, then the compiled RigLogic library.

---

## If a gate fails

- **Gate 1** — the face SKM has no post-process ABP. Check the mesh path; `SKM_Telly_FaceMesh`
  is the one with the ABP, `SKM_Telly_FaceMesh_DNA` may not be. Do not work around it: without
  the ABP the file is not MetaHuman's deformation and is worthless as ground truth.
- **Gate 2** — the AnimSequence did not drive. Re-check `validation_anim_report.json` shows 263
  curves and 323 frames.
- **Gate 3** — the ABP loaded but contributed nothing. This is the interesting failure; send
  the log rather than retrying.

Nothing here writes to the repo or to any published asset. The only output is the `.npz` in
`C:\tmp\mh`.
