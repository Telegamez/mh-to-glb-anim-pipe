"""Blender: one GLB = Avaturn library armature + its Dance2/Dance3/KnockOut actions + NY mesh skinned to it.

blender -b --factory-startup -P make_avaturn_src.py -- <animations.glb> <NY.glb> <out.glb>
The NY mesh is only an IK-rig preview mesh: it is re-pointed at the animation armature (same bone
names), so UE imports the animation armature's rest pose as the skeleton.
"""
import sys
import bpy

anims, ny, out = sys.argv[sys.argv.index('--') + 1:][:3]
KEEP = ['Dance2', 'Dance3']
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=anims)
arm = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE')
arm.name = 'Avaturn'
before = set(bpy.context.scene.objects)
bpy.ops.import_scene.gltf(filepath=ny)
new = [o for o in bpy.context.scene.objects if o not in before]
ny_arm = [o for o in new if o.type == 'ARMATURE']
meshes = [o for o in new if o.type == 'MESH']
bones = {b.name for b in arm.data.bones}
for m in meshes:
    for mod in m.modifiers:
        if mod.type == 'ARMATURE':
            mod.object = arm
    mw = m.matrix_world.copy()
    m.parent = arm
    m.matrix_world = mw
    missing = [vg.name for vg in m.vertex_groups if vg.name not in bones]
    if missing:
        print('[avsrc]', m.name, 'vertex groups without bone:', missing[:6])
for o in ny_arm:
    bpy.data.objects.remove(o, do_unlink=True)
for o in list(bpy.context.scene.objects):
    if o.type == 'EMPTY':
        bpy.data.objects.remove(o, do_unlink=True)
# Bake the armature object's transform (the Y-up/Mixamo correction) into the rest pose, so the armature
# node is identity in BOTH the bind pose and the animation. Without this UE's proxy root got a 90 deg roll
# in the animation but 0 in the reference pose, and the retargeter copied it onto the MetaHuman root.
print('[avsrc] armature object before apply: rot', tuple(round(v, 3) for v in arm.rotation_euler), 'scale', tuple(arm.scale))
bpy.ops.object.select_all(action='DESELECT')
for o in [arm] + meshes:
    if o.name in bpy.data.objects:
        o.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
print('[avsrc] armature object after apply: rot', tuple(round(v, 3) for v in arm.rotation_euler), 'scale', tuple(arm.scale))
# Actions -> NLA tracks for the kept clips only.
if arm.animation_data is None:
    arm.animation_data_create()
for tr in list(arm.animation_data.nla_tracks):
    arm.animation_data.nla_tracks.remove(tr)
arm.animation_data.action = None
found = []
for a in bpy.data.actions:
    base = a.name.split('_Armature')[0].split('.')[0]
    if base in KEEP and base not in found:
        tr = arm.animation_data.nla_tracks.new(); tr.name = base
        tr.strips.new(base, int(a.frame_range[0]), a); tr.mute = True
        a.name = base
        found.append(base)
print('[avsrc] actions kept:', found, 'of', [a.name for a in bpy.data.actions][:40])
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB', export_animations=True,
                          export_animation_mode='NLA_TRACKS', export_skins=True, export_morph=False,
                          export_cameras=False, export_lights=False, export_yup=True)
print('[avsrc] wrote', out)
