"""Parse a MetaHuman `.dna` file's **behaviour** layer, engine-free, and cost it.

    python parse_bhvr.py Telly_Head.dna [--json out.json]

This is the reader half of slice 5.1 (F5 — RigLogic → WASM). The behaviour layer is the
whole of what an evaluator needs: `geom` is 91% of the file and describes meshes the GLB
already carries. Everything RigLogic *does* per frame is here.

How the layout was established
------------------------------
Not from memory of the spec. The section table gives `bhvr` an exact offset and size, so a
proposed layout is falsifiable: walk it, and if the cursor lands on `offset + size` to the
byte, the field order is right; if not, it desynchronises and lands elsewhere.

That test fixes the *types*' positions but cannot separate three same-typed vectors, so the
`JointGroup` order was narrowed the rest of the way by properties that must hold of the data:

* `len(values) == len(inputIndices) * len(outputIndices)` in **all 122 groups** — a joint
  group is a dense output x input matrix. This eliminated four of six byte-compatible orders.
* index ranges: `outputIndices` max 7826 < 7830 joint attribute rows (870 joints x 9);
  `inputIndices` max 825, the same domain the blendshape mapping indexes; `jointIndices` is
  870 entries, all distinct, max 869 — exactly one per joint.

`assert_layout()` re-runs the boundary check on every parse, so a DNA written by a different
engine version fails loudly here instead of producing plausible nonsense downstream. That is
the same lesson as the viseme capture tool: an instrument that approximates the thing it
measures is worse than no instrument, because it looks like one.

Layout (DNA generation 2 version 5, big-endian throughout)
----------------------------------------------------------
    bhvr := Controls Joints BlendShapes AnimatedMaps

    Controls     := uint16 psdCount
                    Conditionals guiToRaw
                    vector<uint16> psdRows
                    vector<uint16> psdColumns
                    vector<float>  psdValues

    Joints       := uint16 rowCount           (jointCount * 9 attributes)
                    uint16 columnCount
                    vector<JointGroup>

    JointGroup   := vector<uint16> lods       (rows active per LOD)
                    vector<uint16> inputIndices
                    vector<uint16> outputIndices
                    vector<float>  values     (len == len(in) * len(out))
                    vector<uint16> jointIndices

    BlendShapes  := vector<uint16> lods
                    vector<uint16> inputIndices
                    vector<uint16> outputIndices

    AnimatedMaps := vector<uint16> lods
                    Conditionals

    Conditionals := vector<uint16> inputIndices
                    vector<uint16> outputIndices
                    vector<float>  fromValues
                    vector<float>  toValues
                    vector<float>  slopeValues
                    vector<float>  cutValues
"""
from __future__ import annotations

import argparse
import json
import struct
import sys

SECTION_ENTRY_BYTES = 16
HEADER_BYTES = 11
JOINT_ATTRS = 9  # tx ty tz rx ry rz sx sy sz


class Reader:
    """Big-endian cursor. Every vector is a uint32 count followed by that many elements."""

    __slots__ = ("d", "o")

    def __init__(self, data: bytes, offset: int):
        self.d = data
        self.o = offset

    def u16(self) -> int:
        v = struct.unpack_from(">H", self.d, self.o)[0]
        self.o += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.d, self.o)[0]
        self.o += 4
        return v

    def vec_u16(self) -> list[int]:
        n = self.u32()
        v = list(struct.unpack_from(f">{n}H", self.d, self.o)) if n else []
        self.o += 2 * n
        return v

    def vec_f32(self) -> list[float]:
        n = self.u32()
        v = list(struct.unpack_from(f">{n}f", self.d, self.o)) if n else []
        self.o += 4 * n
        return v

    def count_f32(self) -> int:
        """Length of a float vector, skipping its payload — for costing without loading."""
        n = self.u32()
        self.o += 4 * n
        return n


def read_sections(data: bytes) -> dict:
    count = struct.unpack_from(">I", data, 7)[0]
    out = {}
    o = HEADER_BYTES
    for _ in range(count):
        sid = data[o:o + 4].decode("ascii", "replace")
        soff, ssize = struct.unpack_from(">II", data, o + 8)
        out[sid] = {"offset": soff, "size": ssize}
        o += SECTION_ENTRY_BYTES
    return out


def _conditionals(r: Reader) -> dict:
    """A piecewise-linear remap table: out[o] += clamp(in[i]) * slope + cut."""
    inputs = r.vec_u16()
    outputs = r.vec_u16()
    r.count_f32()  # from
    r.count_f32()  # to
    r.count_f32()  # slope
    r.count_f32()  # cut
    return {"count": len(inputs),
            "max_input": max(inputs) if inputs else -1,
            "max_output": max(outputs) if outputs else -1}


def parse_bhvr(path: str, keep_values: bool = False) -> dict:
    with open(path, "rb") as f:
        data = f.read()
    if data[:3] != b"DNA":
        raise ValueError(f"{path}: not a DNA file")

    sections = read_sections(data)
    if "bhvr" not in sections:
        raise ValueError(f"{path}: no behaviour layer")
    start = sections["bhvr"]["offset"]
    end = start + sections["bhvr"]["size"]

    r = Reader(data, start)

    # ---- Controls ------------------------------------------------------------
    psd_count = r.u16()
    gui_to_raw = _conditionals(r)
    psd_rows = r.vec_u16()
    r.vec_u16()          # psd columns — same length as rows
    r.count_f32()        # psd values  — same length as rows

    # ---- Joints --------------------------------------------------------------
    joint_rows = r.u16()
    joint_cols = r.u16()
    n_groups = r.u32()

    groups = []
    all_inputs: list[int] = []
    all_outputs: list[int] = []
    all_joints: list[int] = []
    for gi in range(n_groups):
        lods = r.vec_u16()
        inputs = r.vec_u16()
        outputs = r.vec_u16()
        values = r.vec_f32() if keep_values else r.count_f32()
        joints = r.vec_u16()
        n_values = len(values) if keep_values else values
        # The invariant that identified this field order in the first place. Checked on
        # every parse, not once during discovery.
        if n_values != len(inputs) * len(outputs):
            raise ValueError(
                f"{path}: joint group {gi} has {n_values} values but "
                f"{len(inputs)}x{len(outputs)} = {len(inputs) * len(outputs)} cells. "
                f"The behaviour layout does not match this file.")
        g = {"lods": lods, "inputs": len(inputs), "outputs": len(outputs),
             "values": n_values, "joints": len(joints)}
        if keep_values:
            g["input_indices"] = inputs
            g["output_indices"] = outputs
            g["joint_indices"] = joints
            g["value_data"] = values
        groups.append(g)
        all_inputs += inputs
        all_outputs += outputs
        all_joints += joints

    # ---- BlendShapes ---------------------------------------------------------
    bs_lods = r.vec_u16()
    bs_inputs = r.vec_u16()
    bs_outputs = r.vec_u16()

    # ---- AnimatedMaps --------------------------------------------------------
    am_lods = r.vec_u16()
    animated_maps = _conditionals(r)

    if r.o != end:
        raise ValueError(
            f"{path}: behaviour layer parse ended at {r.o}, section ends at {end} "
            f"(delta {r.o - end:+}). The layout does not match this file; refusing to "
            f"report numbers derived from a desynchronised read.")

    return {
        "path": path,
        "psd_count": psd_count,
        "gui_to_raw_conditionals": gui_to_raw,
        "psd_matrix_entries": len(psd_rows),
        "joint_rows": joint_rows,
        "joint_columns": joint_cols,
        "joint_count": joint_rows // JOINT_ATTRS,
        "joint_group_count": n_groups,
        "joint_groups": groups,
        "joint_matrix_cells": sum(g["values"] for g in groups),
        "joint_input_max": max(all_inputs) if all_inputs else -1,
        "joint_output_max": max(all_outputs) if all_outputs else -1,
        "joint_indices_total": len(all_joints),
        "joint_indices_distinct": len(set(all_joints)),
        "blend_shape_lods": bs_lods,
        "blend_shape_mappings": len(bs_inputs),
        "blend_shape_input_max": max(bs_inputs) if bs_inputs else -1,
        "blend_shape_output_max": max(bs_outputs) if bs_outputs else -1,
        "animated_map_lods": am_lods,
        "animated_map_conditionals": animated_maps,
        "control_vector_size": max(
            max(all_inputs) if all_inputs else -1,
            max(bs_inputs) if bs_inputs else -1,
            animated_maps["max_input"]) + 1,
        "bytes": {"offset": start, "size": sections["bhvr"]["size"],
                  "file_size": len(data),
                  "share_of_file": round(sections["bhvr"]["size"] / len(data), 4)},
    }


def cost_per_lod(parsed: dict) -> list[dict]:
    """Multiply-accumulate count per frame, per LOD.

    A joint group's `lods` vector gives how many of its output rows are active at each LOD,
    so LOD 0 is the full row count and higher LODs are prefixes. Cost is rows x inputs,
    because RigLogic evaluates a dense block per group.
    """
    n_lods = max((len(g["lods"]) for g in parsed["joint_groups"]), default=0)
    out = []
    for lod in range(n_lods):
        macs = 0
        rows = 0
        for g in parsed["joint_groups"]:
            if lod < len(g["lods"]):
                active_rows = g["lods"][lod]
                rows += active_rows
                macs += active_rows * g["inputs"]
        out.append({"lod": lod, "active_joint_rows": rows, "joint_macs": macs})
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dna")
    p.add_argument("--json", help="write the full parse here")
    ns = p.parse_args()

    b = parse_bhvr(ns.dna)
    print(f"{b['path']}")
    print(f"  behaviour layer      {b['bytes']['size'] / 1e6:7.2f} MB  "
          f"({b['bytes']['share_of_file'] * 100:.1f}% of the file)")
    print(f"  control vector       {b['control_vector_size']:7d}  "
          f"(raw controls + {b['psd_count']} PSDs + solver outputs)")
    print(f"  gui -> raw conds     {b['gui_to_raw_conditionals']['count']:7d}")
    print(f"  psd matrix entries   {b['psd_matrix_entries']:7d}")
    print(f"  joints               {b['joint_count']:7d}  "
          f"({b['joint_rows']} attribute rows x {b['joint_columns']} columns)")
    print(f"  joint groups         {b['joint_group_count']:7d}")
    print(f"  joint matrix cells   {b['joint_matrix_cells']:7d}")
    print(f"  blendshape mappings  {b['blend_shape_mappings']:7d}")
    print(f"  animated map conds   {b['animated_map_conditionals']['count']:7d}")
    print("  per-frame joint cost by LOD:")
    for c in cost_per_lod(b):
        print(f"    LOD {c['lod']}  rows {c['active_joint_rows']:5d}  "
              f"MACs {c['joint_macs']:9d}  "
              f"at 60 Hz {c['joint_macs'] * 60 / 1e6:8.1f} M/s")

    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump(b, f, indent=2)
        print(f"  wrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
