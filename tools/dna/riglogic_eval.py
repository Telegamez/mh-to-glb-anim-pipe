"""A RigLogic evaluator built from the DNA behaviour layer, and its per-frame cost.

    python riglogic_eval.py Telly_Head.dna [--frames 600] [--lod 0] [--json out.json]

Slice 5.1 of the face-fidelity plan (F5 — RigLogic → WASM). This is the reference
implementation: readable, checkable, and instrumented, so that when the WASM version exists
there is something to diff it against. It is deliberately NOT the shipping evaluator.

What it establishes
-------------------
The per-frame arithmetic is fully determined by the behaviour layer, so the cost is a
measurable property of the rig rather than an estimate. On Telly at LOD 0 the joint stage is
1,000,930 multiply-accumulates per frame — one dense block per joint group — plus 259 GUI
conditionals, 1,430 PSD terms, 782 blendshape gathers and 258 animated-map conditionals.

The numbers this file prints are a **floor**, not a browser prediction: numpy dispatches the
joint blocks to OpenBLAS on native x86-64 with the whole matrix resident. A WASM SIMD build
in a worker should be taken as some multiple of this, and the point of measuring here is that
the multiple is what remains unknown, not the workload.

Evaluation order (RigLogic's four stages)
-----------------------------------------
1. **GUI to raw** — piecewise-linear conditionals map the 174 artist-facing controls onto raw
   controls: ``raw[out] += clamp(gui[in], from, to) * slope + cut``. A rig driven directly by
   raw controls, as `tg_viseme_poses.json` does, skips this stage entirely.
2. **PSDs** — 545 correctives, each the product of its input terms. This is where
   co-articulation lives, and it is precisely what a baked morph target collapses: a bake
   samples the product at one pose, while RigLogic evaluates it continuously.
3. **Joints** — per group, a dense ``(outputs x inputs)`` block times the control slice,
   accumulated into 7,830 joint attribute rows (870 joints x 9).
4. **Blendshapes and animated maps** — a 782-entry gather, then 258 more conditionals.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time

import numpy as np

from parse_bhvr import Reader, parse_bhvr, read_sections

JOINT_ATTRS = 9


def _load_control_stage(path: str) -> dict:
    """Re-read the pieces `parse_bhvr` summarises but does not keep: conditional tables
    and the PSD triples. Kept separate so the summariser stays cheap."""
    data = open(path, "rb").read()
    sections = read_sections(data)
    r = Reader(data, sections["bhvr"]["offset"])

    psd_count = r.u16()
    gui_in = r.vec_u16()
    gui_out = r.vec_u16()
    gui_from = r.vec_f32()
    gui_to = r.vec_f32()
    gui_slope = r.vec_f32()
    gui_cut = r.vec_f32()
    psd_rows = r.vec_u16()
    psd_cols = r.vec_u16()
    psd_values = r.vec_f32()

    return {
        "psd_count": psd_count,
        "gui": {"in": np.array(gui_in, np.int32), "out": np.array(gui_out, np.int32),
                "from": np.array(gui_from, np.float32), "to": np.array(gui_to, np.float32),
                "slope": np.array(gui_slope, np.float32),
                "cut": np.array(gui_cut, np.float32)},
        "psd": {"rows": np.array(psd_rows, np.int32), "cols": np.array(psd_cols, np.int32),
                "values": np.array(psd_values, np.float32)},
    }


class RigLogic:
    """The behaviour layer, arranged for evaluation."""

    def __init__(self, path: str, lod: int = 0):
        self.b = parse_bhvr(path, keep_values=True)
        self.c = _load_control_stage(path)
        self.lod = lod
        self.control_size = self.b["control_vector_size"]
        self.joint_rows = self.b["joint_rows"]
        self.joint_count = self.b["joint_count"]

        # One dense block per group, truncated to the rows this LOD uses. RigLogic stores
        # rows in LOD order precisely so a lower LOD is a prefix, not a gather.
        self.blocks = []
        for g in self.b["joint_groups"]:
            rows = g["lods"][lod] if lod < len(g["lods"]) else 0
            if rows == 0 or g["inputs"] == 0:
                continue
            m = np.asarray(g["value_data"], np.float32).reshape(g["outputs"], g["inputs"])
            self.blocks.append((
                np.asarray(g["input_indices"], np.int32),
                np.asarray(g["output_indices"][:rows], np.int32),
                np.ascontiguousarray(m[:rows]),
            ))

        self.macs = int(sum(b[2].size for b in self.blocks))

    # -- stages ---------------------------------------------------------------
    def gui_to_raw(self, gui: np.ndarray, raw: np.ndarray) -> None:
        g = self.c["gui"]
        v = np.clip(gui[g["in"]], g["from"], g["to"])
        np.add.at(raw, g["out"], v * g["slope"] + g["cut"])

    def psds(self, controls: np.ndarray) -> None:
        """Each PSD is the product of its weighted input terms.

        Multiplicative, not additive: that is the whole reason a corrective exists. Computed
        by segment-multiplying the weighted terms grouped by output row.
        """
        p = self.c["psd"]
        terms = controls[p["cols"]] * p["values"]
        out = np.ones(self.control_size, np.float32)
        np.multiply.at(out, p["rows"], terms)
        touched = np.unique(p["rows"])
        controls[touched] = out[touched]

    def joints(self, controls: np.ndarray, out: np.ndarray) -> None:
        for inputs, outputs, m in self.blocks:
            out[outputs] += m @ controls[inputs]

    def evaluate(self, controls: np.ndarray) -> np.ndarray:
        """Controls in, joint attribute deltas out. Controls are raw + PSD slots."""
        work = controls.copy()
        self.psds(work)
        joints = np.zeros(self.joint_rows, np.float32)
        self.joints(work, joints)
        return joints


def benchmark(rig: RigLogic, frames: int, seed: int = 7) -> dict:
    """Time the joint stage over a plausible control stream.

    Random controls rather than a fixed pose: a zero or constant vector would let the
    hardware and the branch predictor flatter the result, and the cost of a dense block is
    input-independent anyway, so the only thing randomness changes is honesty.
    """
    rng = np.random.default_rng(seed)
    stream = rng.random((frames, rig.control_size), dtype=np.float32)

    rig.evaluate(stream[0])  # warm the allocator and BLAS handles

    t0 = time.perf_counter()
    for i in range(frames):
        rig.evaluate(stream[i])
    dt = time.perf_counter() - t0

    per_frame_ms = dt / frames * 1000.0
    return {
        "frames": frames,
        "lod": rig.lod,
        "joint_macs_per_frame": rig.macs,
        "total_seconds": round(dt, 4),
        "per_frame_ms": round(per_frame_ms, 4),
        "frames_per_second": round(1000.0 / per_frame_ms, 1),
        "budget_fraction_at_60hz": round(per_frame_ms / (1000.0 / 60.0), 4),
        "effective_mflops": round(rig.macs * 2 / (per_frame_ms / 1000.0) / 1e6, 1),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dna")
    p.add_argument("--frames", type=int, default=600)
    p.add_argument("--lod", type=int, default=0)
    p.add_argument("--json")
    ns = p.parse_args()

    rig = RigLogic(ns.dna, lod=ns.lod)
    print(f"{ns.dna}  LOD {ns.lod}")
    print(f"  control vector      {rig.control_size}")
    print(f"  joint attr rows     {rig.joint_rows}  ({rig.joint_count} joints x {JOINT_ATTRS})")
    print(f"  dense blocks        {len(rig.blocks)}")
    print(f"  joint MACs / frame  {rig.macs}")

    r = benchmark(rig, ns.frames)
    print(f"\n  {r['frames']} frames in {r['total_seconds']}s")
    print(f"  per frame           {r['per_frame_ms']:.4f} ms")
    print(f"  sustained           {r['frames_per_second']:.0f} fps")
    print(f"  of a 16.67 ms frame {r['budget_fraction_at_60hz'] * 100:.2f}%")
    print(f"  effective           {r['effective_mflops']:.0f} MFLOP/s "
          f"(native x86-64 + OpenBLAS — a floor, not a browser number)")

    if ns.json:
        out = {"dna": ns.dna, "structure": {
            "control_vector": rig.control_size, "joint_rows": rig.joint_rows,
            "joint_count": rig.joint_count, "blocks": len(rig.blocks)}, "benchmark": r}
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print(f"  wrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
