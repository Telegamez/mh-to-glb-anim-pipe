"""Slice 5.3 — what does live RigLogic recover that baked morph targets cannot?

    python measure_corrective_residual.py Telly_Head.dna tg_viseme_poses.json [--json out]

The rung-3 go/no-go turns on one number, and it is easy to get wrong by asking the wrong
question. Asking "how much of the rig is corrective" gives 51% — that is the share of the
joint matrix's weight mass sitting on PSD inputs. It is also close to meaningless, because a
PSD is a *product* of controls: at 0.5 a two-term corrective contributes 0.25 where a raw
control contributes 0.5, and on a single held viseme most correctives do not fire at all.

The question that decides the architecture is about **blends**, because that is where speech
lives. Two ways to get from viseme A to viseme B at parameter t:

    baked   (1-t) * RigLogic(A) + t * RigLogic(B)
            what a GLB morph target can express -- each target is a fixed vertex delta, so
            blending them is necessarily linear

    live    RigLogic((1-t) * A + t * B)
            correctives see both poses' controls at once and fire on the combination

They differ exactly to the extent the rig is non-linear, and that difference is the ceiling
the baked path sits under. It needs no engine, no bake and no operator: the DNA and the
authored control values are enough.

Both stages are measured, because both carry correctives:

* the **joint** stage, 870 joints x 9 attributes
* the **blendshape** stage, where 545 of 782 channels are driven from PSD slots rather than
  raw controls -- MetaHuman's own corrective shapes, which a baked viseme collapses the same way

Reported in joint-attribute and blendshape-weight space, not rendered vertex displacement.
Skinning is close to linear in the joint deltas so the percentages should carry, but that is
an assumption this tool does not verify.

**These numbers are provisional until the evaluator is validated against UE.** The parse is
strongly evidenced -- it lands on the section boundary to the byte across six DNAs, and PSD
row indices independently agree with the definition layer's raw-control count -- but structural
correctness is not semantic correctness. That the PSD stage is a plain product, and that joint
blocks accumulate additively, are read from the data's shape rather than confirmed against
ground truth.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys

import numpy as np

from parse_bhvr import Reader, parse_bhvr, read_sections
from riglogic_eval import RigLogic

RAW_CONTROL_COUNT = 263


def _definition_names(data: bytes, sections: dict) -> tuple[list[str], list[str]]:
    r = Reader(data, sections["defn"]["offset"])
    for _ in range(4):                       # four LOD mappings
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

    return sv(), sv()                        # gui names, raw names


def _blendshape_mapping(data: bytes, sections: dict) -> tuple[np.ndarray, np.ndarray]:
    """Skip to the blendshape block and return its (input, output) index arrays."""
    r = Reader(data, sections["bhvr"]["offset"])
    r.u16()
    r.vec_u16(); r.vec_u16()
    for _ in range(4):
        r.count_f32()
    r.vec_u16(); r.vec_u16(); r.count_f32()
    r.u16(); r.u16()
    for _ in range(r.u32()):
        r.vec_u16(); r.vec_u16(); r.vec_u16(); r.count_f32(); r.vec_u16()
    r.vec_u16()                              # blendshape lods
    return np.array(r.vec_u16()), np.array(r.vec_u16())


def load(dna: str, poses_path: str):
    rig = RigLogic(dna, lod=0)
    data = open(dna, "rb").read()
    sections = read_sections(data)
    _, raw_names = _definition_names(data, sections)
    bs_in, bs_out = _blendshape_mapping(data, sections)

    index = {n: i for i, n in enumerate(raw_names)}
    cfg = json.load(open(poses_path, encoding="utf-8"))

    vectors: dict[str, np.ndarray] = {}
    for pose in cfg["poses"]:
        if not pose["controls"]:
            continue                          # viseme_sil is zero-delta by design
        v = np.zeros(rig.control_size, np.float32)
        for name, val in pose["controls"].items():
            # Pose keys are bare ('mouthPressUL'); DNA raw controls are dotted. The config's
            # own `control_prefix` is the snake-case UE curve name and is not the join key.
            i = index.get(f"CTRL_expressions.{name}")
            if i is not None:
                v[i] = float(val)
        vectors[pose["name"]] = v
    return rig, vectors, bs_in, bs_out


def residuals(rig: RigLogic, vectors: dict, bs_in, bs_out, ts=(0.25, 0.5, 0.75)):
    psd_end = RAW_CONTROL_COUNT + rig.c["psd_count"]
    n_bs = int(bs_out.max()) + 1

    def blendshapes(controls: np.ndarray) -> np.ndarray:
        w = controls.copy()
        rig.psds(w)
        out = np.zeros(n_bs, np.float32)
        out[bs_out] = w[bs_in]
        return out

    joints = {k: rig.evaluate(v) for k, v in vectors.items()}
    shapes = {k: blendshapes(v) for k, v in vectors.items()}

    rows = []
    for a, b in itertools.combinations(vectors, 2):
        worst_j = worst_b = 0.0
        worst_t = 0.0
        for t in ts:
            mixed = (1 - t) * vectors[a] + t * vectors[b]

            live_j = rig.evaluate(mixed)
            baked_j = (1 - t) * joints[a] + t * joints[b]
            dj = float(np.abs(live_j).sum())
            pj = float(np.abs(live_j - baked_j).sum()) / dj * 100 if dj else 0.0

            live_b = blendshapes(mixed)
            baked_b = (1 - t) * shapes[a] + t * shapes[b]
            db = float(np.abs(live_b).sum())
            pb = float(np.abs(live_b - baked_b).sum()) / db * 100 if db else 0.0

            if max(pj, pb) > max(worst_j, worst_b):
                worst_t = t
            worst_j = max(worst_j, pj)
            worst_b = max(worst_b, pb)

        probe = 0.5 * vectors[a] + 0.5 * vectors[b]
        w = probe.copy()
        rig.psds(w)
        rows.append({"pair": f"{a} + {b}", "joint_pct": worst_j, "blendshape_pct": worst_b,
                     "at_t": worst_t,
                     "active_psds": int(np.count_nonzero(w[RAW_CONTROL_COUNT:psd_end]))})
    return rows


def _summary(vals: list[float]) -> dict:
    a = np.array(vals)
    return {"mean": round(float(a.mean()), 2), "median": round(float(np.median(a)), 2),
            "p90": round(float(np.percentile(a, 90)), 2), "max": round(float(a.max()), 2),
            "over_10pct": int((a > 10).sum()), "over_25pct": int((a > 25).sum()),
            "n": len(a)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dna")
    p.add_argument("poses")
    p.add_argument("--json")
    p.add_argument("--top", type=int, default=10)
    ns = p.parse_args()

    rig, vectors, bs_in, bs_out = load(ns.dna, ns.poses)
    psd_end = RAW_CONTROL_COUNT + rig.c["psd_count"]
    print(f"{len(vectors)} authored poses; {len(bs_in)} blendshape channels, of which "
          f"{int(((bs_in >= RAW_CONTROL_COUNT) & (bs_in < psd_end)).sum())} are PSD-driven")

    rows = residuals(rig, vectors, bs_in, bs_out)
    rows.sort(key=lambda r: -max(r["joint_pct"], r["blendshape_pct"]))

    print(f"\n{'pair':<32}{'joints':>9}{'shapes':>9}{'t':>6}{'PSDs':>6}")
    for r in rows[:ns.top]:
        print(f"{r['pair']:<32}{r['joint_pct']:>8.1f}%{r['blendshape_pct']:>8.1f}%"
              f"{r['at_t']:>6}{r['active_psds']:>6}")

    js = _summary([r["joint_pct"] for r in rows])
    bsum = _summary([r["blendshape_pct"] for r in rows])
    print(f"\nover {js['n']} pairs")
    print(f"  joint stage       mean {js['mean']:5.1f}%  median {js['median']:5.1f}%  "
          f"p90 {js['p90']:5.1f}%  max {js['max']:5.1f}%")
    print(f"  blendshape stage  mean {bsum['mean']:5.1f}%  median {bsum['median']:5.1f}%  "
          f"p90 {bsum['p90']:5.1f}%  max {bsum['max']:5.1f}%")

    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump({"dna": ns.dna, "poses": ns.poses, "pairs": rows,
                       "joint_summary": js, "blendshape_summary": bsum}, f, indent=2)
        print(f"  wrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
