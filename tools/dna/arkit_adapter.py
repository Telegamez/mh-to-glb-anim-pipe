"""ARKit blendshape name -> RigLogic raw control weights, read from the pipeline's own export.

    python arkit_adapter.py <character> [-o out.json] [--dna Telly_Head.dna]

Slice 5.2b. Rung 2 drives RigLogic, whose inputs are the 263 `CTRL_expressions.*` raw
controls. Everything upstream — the Inworld viseme map, the emotion layer, the harness — speaks
ARKit blendshape names. Something has to translate, and it must not be hand-authored: F2 spent
a review cycle establishing that authored weights are plausible-until-judged, and a hand-written
ARKit map would carry the same problem with none of the review.

It does not need authoring, because stage 01 already exports the table. When the ARKit-52 bake
runs, each of the 66 poses in `PA_MetaHuman_ARKit_Mapping` is applied and the rig's curve state
captured to:

    characters/<id>/01-glb/arkit_pose_curves.json

That file is the mapping: for each ARKit pose, the raw-control weights RigLogic was actually
driven with when that shape was captured. Per character, already on disk, derived from the same
asset the bake trusts.

## What has to be filtered out, and why

The captured curve state mixes three namespaces, only one of which is an input:

    ctrl_expressions_*        the 263 raw RigLogic controls          <- inputs, keep
    ctrl_riglogic_offon       a rig enable switch                    <- not an expression
    head_*, teeth_*,          RigLogic OUTPUTS captured alongside    <- results, not causes
    cartilage_*               (animated maps and blendshape channels)

Feeding an output back in as an input would be a loop that happens to typecheck. On Telly the
split is 304 `ctrl_expressions_*` occurrences against 554 output-namespace ones, so most of the
file is not the mapping.

## The three constants, flagged rather than quietly dropped

`ctrl_expressions_nosewrinkleupperl`, `ctrl_expressions_nosewrinkleupperr` and
`ctrl_riglogic_offon` appear in **every one of the 66 poses at exactly 1.0** — including
`Default`, which is supposed to be the neutral rest. Verified identical on telly, telee and
tele, so it is systematic rather than one bad capture.

A value that is constant across every pose carries no ARKit signal by construction: it cannot
distinguish `JawOpen` from `EyeBlinkLeft`, so it cannot be part of a mapping from one to the
other. They are dropped for that reason, not because they look wrong.

But they *do* look wrong, and that is worth saying out loud rather than burying: a neutral face
with both upper nose-wrinkle controls at full is not a neutral face. The likely explanation is
that the capture records the ABP's own baseline state rather than a pose delta. **If the rung-2
integration ever renders a permanently wrinkled nose, this is the first place to look.** Nobody
should have to rediscover it from the geometry.

## Composition

Weights are emitted per ARKit pose and are meant to be **summed** across simultaneously active
ARKit shapes, then clamped to the rig's 0..1 control range — which is how RigLogic's own control
input works and how the ARKit bake drives it. It is deliberately not a max() or a
winner-takes-all: co-activation is the whole reason correctives exist.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

RAW_PREFIX = "ctrl_expressions_"
DNA_PREFIX = "CTRL_expressions."
DEFAULT_POSE = "Default"

HERE = os.path.dirname(os.path.abspath(__file__))
CHARACTERS = os.path.join(HERE, "..", "..", "5.7", "native-glb", "characters")


def build(curves_path: str, dna_controls: list[str] | None = None) -> dict:
    with open(curves_path, "r", encoding="utf-8") as f:
        raw_table = json.load(f)

    if DEFAULT_POSE not in raw_table:
        raise RuntimeError(f"{curves_path} has no '{DEFAULT_POSE}' pose; cannot establish rest")

    # Keep only RigLogic inputs.
    kept = {
        pose: {k[len(RAW_PREFIX):]: float(v)
               for k, v in curves.items() if k.startswith(RAW_PREFIX)}
        for pose, curves in raw_table.items()
    }

    dropped_namespaces = sorted({
        k.split("_")[0] for curves in raw_table.values() for k in curves
        if not k.startswith(RAW_PREFIX)
    })

    # Controls whose value never varies across the 66 poses carry no ARKit signal.
    all_controls = sorted({c for c in (k for p in kept.values() for k in p)})
    constant = []
    for c in all_controls:
        seen = {round(p.get(c, 0.0), 6) for p in kept.values()}
        if len(seen) == 1 and all(c in p for p in kept.values()):
            constant.append((c, seen.pop()))

    constant_names = {c for c, _ in constant}
    poses = {
        pose: {c: round(w, 6) for c, w in curves.items()
               if c not in constant_names and abs(w) > 1e-6}
        for pose, curves in kept.items()
    }
    # Default is the rest state and is not an emittable shape.
    default_after = poses.pop(DEFAULT_POSE, {})

    unmapped = []
    if dna_controls is not None:
        known = {c[len(DNA_PREFIX):].lower() if c.startswith(DNA_PREFIX) else c.lower()
                 for c in dna_controls}
        unmapped = sorted({c for p in poses.values() for c in p if c.lower() not in known})

    return {
        "poses": poses,
        "diagnostics": {
            "source": os.path.basename(curves_path),
            "arkit_poses": len(poses),
            "distinct_controls": len({c for p in poses.values() for c in p}),
            "dropped_output_namespaces": dropped_namespaces,
            "constant_controls_dropped": [{"control": c, "value": v} for c, v in constant],
            "default_pose_residual": default_after,
            "controls_not_in_dna": unmapped,
        },
    }


def _dna_raw_controls(dna_path: str) -> list[str]:
    sys.path.insert(0, HERE)
    from parse_bhvr import Reader, read_sections  # noqa: E402

    data = open(dna_path, "rb").read()
    r = Reader(data, read_sections(data)["defn"]["offset"])
    for _ in range(4):
        r.vec_u16()
        for _ in range(r.u32()):
            r.vec_u16()

    def s() -> str:
        n = r.u32()
        v = r.d[r.o:r.o + n].decode("utf-8", "replace")
        r.o += n
        return v

    def sv() -> list[str]:
        return [s() for _ in range(r.u32())]

    sv()
    return sv()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("character")
    p.add_argument("-o", "--out")
    p.add_argument("--dna", help="cross-check control names against a .dna definition layer")
    ns = p.parse_args()

    curves = os.path.join(CHARACTERS, ns.character, "01-glb", "arkit_pose_curves.json")
    if not os.path.exists(curves):
        raise SystemExit(f"no ARKit curve export for '{ns.character}' at {curves}\n"
                         f"Run stage 01 for this character first; this tool never authors a "
                         f"mapping, it only reads the one the bake produced.")

    dna_controls = _dna_raw_controls(ns.dna) if ns.dna else None
    table = build(curves, dna_controls)
    d = table["diagnostics"]

    print(f"{ns.character}: {d['arkit_poses']} ARKit poses -> "
          f"{d['distinct_controls']} distinct raw controls")
    print(f"  dropped output namespaces : {', '.join(d['dropped_output_namespaces'])}")
    print(f"  dropped constants         : "
          f"{len(d['constant_controls_dropped'])} control(s) constant across all poses")
    for c in d["constant_controls_dropped"]:
        print(f"      {c['control']} = {c['value']} in ALL poses including Default")
    if d["default_pose_residual"]:
        print(f"  WARNING: Default is not empty after filtering: {d['default_pose_residual']}")
    else:
        print("  Default reduces to empty after filtering — rest is rest")
    if dna_controls is not None:
        if d["controls_not_in_dna"]:
            print(f"  NOT IN DNA ({len(d['controls_not_in_dna'])}): "
                  f"{d['controls_not_in_dna'][:8]}")
        else:
            print(f"  every control cross-checks against the DNA's "
                  f"{len(dna_controls)} raw controls")

    if ns.out:
        with open(ns.out, "w", encoding="utf-8") as f:
            json.dump(table, f, indent=2)
            f.write("\n")
        print(f"  wrote {ns.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
