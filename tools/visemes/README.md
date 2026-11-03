# Viseme bake verification (face-fidelity-plan F2)

Tools that prove the baked viseme poses are what F2 claims, without needing a
character workspace or a full 00→04 pipeline run. They exist because "the
visemes are present" and "the visemes are baked with real tongue articulation"
are different claims, and only measurement separates them.

## The run order

```powershell
# 1. Author the AnimSequence from the committed pose table (slice 2.1)
& $UECmd $UEProject -run=pythonscript `
    "-script=<repo>/5.7/native-glb/stages/01-unreal-glb-export/tools/author_viseme_anim.py" `
    -unattended -nosplash

# 2. Bake it through the real stage-01 path into a scratch dir (slice 2.2)
$env:TG_VERIFY_OUT    = "C:\temp\viseme_verify"
$env:TG_VERIFY_REPORT = "C:\temp\viseme_verify\verify.json"
& $UECmd $UEProject -run=pythonscript `
    "-script=<repo>/tools/visemes/verify_stage01_viseme_bake.py" `
    -unattended -nosplash -AllowCommandletRendering

# 3. Bake the ARKit mapping alongside it, as the comparison baseline
& $UECmd $UEProject -run=pythonscript `
    "-script=<repo>/tools/visemes/export_arkit_reference_bake.py" `
    -unattended -nosplash -AllowCommandletRendering

# 4. Measure baked vs composed (slice 2.3 evidence)
& $Blender --background --factory-startup `
    --python <repo>/tools/visemes/compare_baked_vs_composed.py `
    -- C:\temp\viseme_verify C:\temp\viseme_compare.json
```

## What each one asserts

| Tool | Asserts |
|---|---|
| `verify_stage01_viseme_bake.py` | The FBX and names JSON are produced; frame counts agree; the rate is measured per sequence (ARKit **24**, TG visemes **30**); and **a missing asset returns `None` and writes nothing** — the rollback property. |
| `export_arkit_reference_bake.py` | Produces `LS_arkit_full.fbx` + `arkit_pose_names.json` as the composed-recipe baseline. |
| `compare_baked_vs_composed.py` | Per shape: max displacement and **tongue displacement**, baked vs composed, from the same face mesh. |

## Reading the numbers

`telly_baseline_measurements.json` is the recorded baseline. Two sanity anchors
and one verdict column:

- **`jawOpen` = 32.88 mm** against the 32.7 mm already on record. If this drifts,
  the capture is wrong and nothing else in the file means anything.
- **No shape may measure 0.00 mm except `viseme_sil`**, which is deliberately
  zero-delta. A zero elsewhere is a dead morph the runtime will drive to no
  effect.
- **The tongue column is the verdict.** Tongue vertices are identified by
  probing `tongueOut` (the one purely lingual ARKit shape), *not* by the teeth
  material slot — the slot contains the teeth, the teeth ride the jaw, and a
  slot-based set scores jaw motion as tongue motion.

Baked vs composed tongue displacement, Telly, 2026-08-22:

| shape | baked mm | composed mm | ratio |
|---|---|---|---|
| `viseme_nn` | 19.14 | 5.21 | **3.7x** |
| `viseme_SS` |  8.83 | 1.56 | **5.7x** |
| `viseme_DD` | 16.00 | 6.22 | **2.6x** |
| `viseme_CH` |  6.88 | 3.11 | 2.2x |
| `viseme_TH` | 13.11 | 9.87 | 1.3x |
| `viseme_kk` |  7.42 | 5.60 | 1.3x |

These are the four tongue-dependent visemes the `VISEME_RECIPES` comment named
as a "KNOWN CEILING … not liftable here", plus the two the recipes reached with
lip cues alone. They are lifted.

## What these tools do NOT establish

They measure **magnitude and location**, not **correctness of shape**. A pose can
move the tongue 19 mm in the wrong direction and score well here. Slice 2.1's
`[V]` screenshot review against the articulation brief is the only thing that
adjudicates that, and it remains required — these numbers make it cheap to spot
a regression, not unnecessary to look.

Several shapes measure *lower* baked than composed on overall displacement
(`viseme_O` 10.6 vs 15.6, `viseme_E` 10.1 vs 11.9, `viseme_aa` 21.5 vs 23.0).
That is not automatically a defect: the composed `viseme_O` leans hard on
`mouthFunnel`, which the recipe comment itself records as the wrong control for
rounding ("it FLARES and parts the lip opening … used heavily it exposes
teeth"). Whether the baked amplitudes are right is an on-screen judgement, and
it is the one the operator session should spend its time on.

## Two bugs these tools found

1. **`mouthClose` baked to 0.00 mm** — a dead morph. `mouthLipsTogether*` is a
   no-op at a closed jaw (it closes the lips against an *open* one), so the pose
   carried no delta from rest. Fixed in the pose table by leaning on
   press/roll-in, which is also why the old synthesis reached for them.

2. **The Sequencer bake dropped the final pose** (`export_glb.py`). Playback end
   is exclusive, so a range of `intervals` bakes `0..intervals-1`. This has
   always been true and was invisible: the ARKit mapping's last six poses are
   `Pose_12..Pose_17`, correctives stage 02 filters out by name, so the dropped
   pose was one nobody captured. `AS_TG_Visemes` ends on a real contract shape
   and exposed it.
