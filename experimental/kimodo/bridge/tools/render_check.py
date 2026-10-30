"""Render a few frames of an animation GLB (with its preview mesh) for a visual pose check.

blender --factory-startup --background --python render_check.py -- --glb X.glb --out DIR [--frames 0,60,119]
Writes <out>/<stem>_f<frame>_<view>.png with Workbench, front and side orthographic views.
"""
import argparse
import math
import os
import sys

import bpy
from mathutils import Vector


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--glb", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--frames", default="")
    p.add_argument("--res", type=int, default=640)
    return p.parse_args(argv)


def main():
    a = _args()
    os.makedirs(a.out, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if a.glb.lower().endswith(".bvh"):
        # Source-side comparison: the raw Kimodo SOMA skeleton, metres, Z-up like the glTF import.
        bpy.context.scene.render.fps = 24  # match the glTF importer's frame mapping
        bpy.ops.import_anim.bvh(filepath=a.glb, global_scale=0.01, frame_start=0, use_fps_scale=True,
                                rotate_mode="NATIVE", axis_forward="-Z", axis_up="Y")
    else:
        bpy.ops.import_scene.gltf(filepath=a.glb)
    scene = bpy.context.scene
    arm = next(o for o in scene.objects if o.type == "ARMATURE")
    act = arm.animation_data.action if arm.animation_data else None
    f0, f1 = (int(v) for v in act.frame_range) if act else (0, 0)
    frames = [int(x) for x in a.frames.split(",")] if a.frames else [f0, (f0 + f1) // 2, f1]
    print(f"[render_check] action={act.name if act else None} range={f0}..{f1} frames={frames}", flush=True)

    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "SINGLE"
    scene.render.resolution_x = scene.render.resolution_y = a.res
    scene.render.film_transparent = False
    world = bpy.data.worlds.new("w")
    world.color = (0.18, 0.18, 0.2)
    scene.world = world

    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = 2.2
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam

    scene.display.shading.color_type = "OBJECT"
    meshes = [o for o in scene.objects if o.type == "MESH" and o.name != "Icosphere"]
    for o in scene.objects:
        if o.name.startswith("Icosphere"):
            o.hide_render = True
    for m in meshes:
        m.color = (0.75, 0.75, 0.75, 1)
    # Stick figure over the deform skeleton: a clothed body mesh shows only exposed skin, and
    # the face is a separate mesh, so the bones are what actually show the pose.
    skip = ("twist", "corrective", "bulge", "FACIAL", "metacarpal", "_02_", "_03_", "_04_", "_05_")
    bones = [b.name for b in arm.data.bones
             if not any(s in b.name for s in skip) and b.parent is not None]
    stick_mesh = bpy.data.meshes.new("stick")
    stick = bpy.data.objects.new("stick", stick_mesh)
    stick.color = (0.95, 0.35, 0.2, 1)
    scene.collection.objects.link(stick)
    stick.show_in_front = True

    def build_stick():
        import bmesh
        bm = bmesh.new()
        for name in bones:
            pb = arm.pose.bones[name]
            h = arm.matrix_world @ pb.head
            p = arm.matrix_world @ pb.parent.head
            d = h - p
            if d.length < 1e-4:
                continue
            r = 0.012
            ax = d.normalized()
            side = ax.cross(Vector((0, 0, 1)) if abs(ax.z) < 0.9 else Vector((1, 0, 0))).normalized() * r
            up = ax.cross(side).normalized() * r
            vs = [bm.verts.new(v) for v in (p + side, p + up, p - side, p - up, h + side, h + up, h - side, h - up)]
            for i in range(4):
                j = (i + 1) % 4
                bm.faces.new((vs[i], vs[j], vs[j + 4], vs[i + 4]))
        bm.to_mesh(stick_mesh)
        bm.free()

    stem = os.path.splitext(os.path.basename(a.glb))[0]
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        build_stick()
        # frame on the posed skeleton's bounds
        pts = [arm.matrix_world @ arm.pose.bones[n].head for n in bones]
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        mid = (lo + hi) / 2
        cam_data.ortho_scale = max(hi.z - lo.z, 1.0) * 1.15
        for view, d in (("front", Vector((0, -6, 0))), ("side", Vector((6, 0, 0))), ("back", Vector((0, 6, 0)))):
            cam.location = mid + d
            direction = mid - cam.location
            cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            scene.render.filepath = os.path.join(a.out, f"{stem}_f{f:03d}_{view}.png")
            bpy.ops.render.render(write_still=True)
    print("[render_check] done", flush=True)


main()
