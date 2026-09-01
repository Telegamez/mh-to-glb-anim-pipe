"""
Studio HDRI authoring — builds and renders the companion lighting environment.

Implements _docs/Architecture/Companions/metahuman/studio-hdri-authoring-spec.md
(telegamez repo, ticket #3660). Headless Blender:

    blender --background --python author_studio_hdri.py -- --out <dir>

The spec's metre positions are GENERATED from
`packages/web-next/src/services/RoomManager/Agent/materials/studio-lighting.json`,
and the spec says the JSON wins on any disagreement. So this script works from the
JSON's angular description (azimuth / elevation / angular size) and places panels on a
sphere of radius PANEL_RADIUS_M. That reproduces the spec's metre table exactly — all
five panels, positions and sizes, to the 0.01 m the spec is printed at (verified; see
README). Pass --lighting-json to read the live file; the embedded table is a checked-in
copy of the same values for when the sister repo is not on disk.

Scene convention (spec "Scene setup"):
    metres, Z up, subject's head at the world origin
    +X camera-right, +Z up, -Y toward the viewer
    the virtual camera sits at (0, -4, 0) looking toward +Y
The panorama is rendered FROM THE ORIGIN, not from the camera position: the environment
is what the subject sees.
"""

import argparse
import json
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Matrix, Vector

# --------------------------------------------------------------------------------------
# Values from studio-lighting.json. Kept here so the script runs without the sister repo;
# --lighting-json overrides every one of them from the live file.
# --------------------------------------------------------------------------------------

EMBEDDED = {
    "environment": {
        "skyColor": "#151a24",
        "groundColor": "#070910",
        "skyIntensity": 0.007,
        "groundIntensity": 0.003,
    },
    "panels": [
        # id            azimuth  elevation  angular   colour       intensity
        {"id": "key",        "azimuthDeg":  -50.0, "elevationDeg":  33.0, "angularSizeDeg": 30.0, "color": "#fff2e2", "intensity": 1.00},
        {"id": "fill",       "azimuthDeg":   46.0, "elevationDeg":  -4.0, "angularSizeDeg": 66.0, "color": "#dce9ff", "intensity": 0.03},
        {"id": "rim",        "azimuthDeg":  156.0, "elevationDeg":  14.0, "angularSizeDeg": 20.0, "color": "#e8f1ff", "intensity": 1.80},
        {"id": "hair",       "azimuthDeg": -150.0, "elevationDeg":  48.0, "angularSizeDeg": 30.0, "color": "#fff6ec", "intensity": 0.70},
        {"id": "catchlight", "azimuthDeg":   -7.0, "elevationDeg":  11.0, "angularSizeDeg": 12.0, "color": "#ffffff", "intensity": 0.18},
    ],
}

# Radius the spec's metre table is printed at. key (-50, 33) at 4.0 m is (-2.57, -2.16,
# 2.18) and its 30-degree angular size is 2.14 m — which is the spec's row exactly.
PANEL_RADIUS_M = 4.0

# Panels that get a softbox housing: a shallow dark open-fronted box behind the emissive
# face. This is the "real softbox falloff" the ticket is for — the interior lip shades
# grazing angles so the source feathers at its edges instead of being uniform to the edge
# like a flat quad. Depth is a fraction of the panel's own size.
#
# `fill` is excluded on purpose: at 66 degrees it is a bounce wall / scrim, not a fixture,
# and a housing round it would be wrong. `catchlight` is excluded because constraint 2
# plus "no recognisable geometry near the camera axis" mean nothing but the bare source
# belongs on the camera axis — a housing there would read as a shape in the eye
# reflections.
HOUSED = {"key", "rim", "hair"}
HOUSING_DEPTH_FRAC = 0.12

# The one number the spec does not fix, and it has to be chosen for bounce to exist at
# all. The spec gives the surround colours as EMISSION ("walls and ceiling #151a24 at
# emission 0.007"), and a pure emitter does not reflect, so a surround built only from
# those would glow without bouncing — and bounce is the whole reason for the ticket. So
# the surround is emission (exactly as specified) PLUS a diffuse albedo, and this is that
# albedo. #151a24 used directly as albedo is ~0.01 linear, darker than any real paint and
# effectively no bounce; a real dark studio wall is nearer 0.05.
#
# Deliberately low, per constraint 1: bounce is a whisper, not a second key. The floor is
# lower again per constraint 3, "do not brighten the floor".
WALL_ALBEDO = 0.05
FLOOR_ALBEDO = 0.02

# Global multiplier on every emission. The spec is explicit that absolute brightness does
# not matter (the import step re-normalises) but that panels must stay above 1.0 so the
# float headroom survives — constraint 5. At the spec's own numbers the key would sit at
# exactly 1.0. This lifts the whole rig, ratios untouched.
GAIN = 10.0

# Clear of the outermost panel corner. Snug rather than generous: a larger room is a
# darker one, and the bounce has to actually reach the subject.
ROOM_MARGIN_M = 1.0

RES_X, RES_Y = 2048, 1024
SAMPLES = 2048


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_linear(h):
    h = h.lstrip("#")
    return tuple(srgb_to_linear(int(h[i:i + 2], 16) / 255.0) for i in (0, 2, 4))


def direction(azimuth_deg, elevation_deg):
    """JSON's camera-relative frame -> a world unit vector.

    azimuth 0 is the camera's own direction (-Y, toward the viewer), positive is camera
    right (+X), 180 is behind the subject (+Y). Elevation is positive above the eye line.
    """
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))


def emissive_material(name, color_linear, strength):
    """One-sided emitter: the front face emits, the back absorbs.

    A bare Emission node emits from both faces, so every panel would light the room from
    behind as well and quietly raise the ambient — which is the flat look the rig exists
    to remove. A real softbox does not emit backwards.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    mix = nt.nodes.new("ShaderNodeMixShader")
    emit = nt.nodes.new("ShaderNodeEmission")
    back = nt.nodes.new("ShaderNodeBsdfDiffuse")
    geo = nt.nodes.new("ShaderNodeNewGeometry")

    emit.inputs["Color"].default_value = (*color_linear, 1.0)
    emit.inputs["Strength"].default_value = strength
    back.inputs["Color"].default_value = (0.01, 0.01, 0.01, 1.0)

    nt.links.new(geo.outputs["Backfacing"], mix.inputs["Fac"])
    nt.links.new(emit.outputs["Emission"], mix.inputs[1])   # Fac 0 -> front
    nt.links.new(back.outputs["BSDF"], mix.inputs[2])       # Fac 1 -> back
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    return mat


def surround_material(name, color_linear, emission_strength, albedo):
    """Dim emitter plus a dark diffuse, so the surround both glows and bounces."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    add = nt.nodes.new("ShaderNodeAddShader")
    emit = nt.nodes.new("ShaderNodeEmission")
    diff = nt.nodes.new("ShaderNodeBsdfDiffuse")

    emit.inputs["Color"].default_value = (*color_linear, 1.0)
    emit.inputs["Strength"].default_value = emission_strength
    # Albedo carries the surround's own hue, scaled to a plausible dark-studio level.
    peak = max(color_linear) or 1.0
    diff.inputs["Color"].default_value = (*[c / peak * albedo for c in color_linear], 1.0)
    diff.inputs["Roughness"].default_value = 0.9

    nt.links.new(emit.outputs["Emission"], add.inputs[0])
    nt.links.new(diff.outputs["BSDF"], add.inputs[1])
    nt.links.new(add.outputs["Shader"], out.inputs["Surface"])
    return mat


def dark_material(name):
    """Fixture bodies. Near-black, so housings shape light without adding any."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.015, 0.015, 0.018, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.85
    return mat


def panel_basis(pos):
    """Rotation putting a plane's +Z normal on the origin, with a level horizontal axis."""
    normal = (-pos).normalized()
    up = Vector((0.0, 0.0, 1.0))
    if abs(normal.dot(up)) > 0.999:            # looking straight up/down
        up = Vector((0.0, 1.0, 0.0))
    x_axis = up.cross(normal).normalized()     # horizontal, so panels never roll
    y_axis = normal.cross(x_axis).normalized()
    return Matrix((x_axis, y_axis, normal)).transposed().to_4x4()


def build_panel(spec, collection):
    """One emissive panel, plus its housing when it has one. Returns world-space corners."""
    pos = direction(spec["azimuthDeg"], spec["elevationDeg"]) * PANEL_RADIUS_M
    size = 2.0 * PANEL_RADIUS_M * math.tan(math.radians(spec["angularSizeDeg"]) / 2.0)
    half = size / 2.0
    basis = panel_basis(pos)
    xform = Matrix.Translation(pos) @ basis

    bpy.ops.mesh.primitive_plane_add(size=1.0)
    panel = bpy.context.object
    panel.name = f"panel_{spec['id']}"
    panel.matrix_world = xform @ Matrix.Diagonal((size, size, 1.0, 1.0))
    panel.data.materials.append(
        emissive_material(f"emit_{spec['id']}", hex_to_linear(spec["color"]), spec["intensity"] * GAIN)
    )
    for c in list(panel.users_collection):
        c.objects.unlink(panel)
    collection.objects.link(panel)

    if spec["id"] in HOUSED:
        build_housing(spec["id"], xform, size, collection)

    rot = basis.to_3x3()
    return [pos + rot @ Vector((sx, sy, 0.0)) for sx in (-half, half) for sy in (-half, half)]


def build_housing(panel_id, xform, size, collection):
    """Open-fronted dark box behind an emissive panel.

    Built from five planes rather than a cube: a cube's front face would sit coplanar
    with the emissive panel and occlude the very source it houses.

    The side walls run from the back plane to slightly PROUD of the emissive face. That
    small lip is the whole point — it shades the panel at grazing angles, so the source
    feathers at its edges the way a physical softbox does instead of staying uniform to
    the edge like a flat quad. The lip is deliberately shallow: at ~7% of the panel's
    half-width it only touches angles beyond about 85 degrees, so the panel's effective
    size and the tuned ratios are left alone.
    """
    depth = size * HOUSING_DEPTH_FRAC
    lip = depth * 0.3
    half = size * 1.06 / 2.0
    span = depth + lip
    mid_z = (lip - depth) / 2.0
    mat = dark_material(f"body_{panel_id}")

    faces = [
        ("back", Matrix.Translation(Vector((0, 0, -depth))) @ Matrix.Diagonal((2 * half, 2 * half, 1.0, 1.0))),
        ("xp", Matrix.Translation(Vector((half, 0, mid_z))) @ Matrix.Rotation(math.pi / 2, 4, "Y") @ Matrix.Diagonal((span, 2 * half, 1.0, 1.0))),
        ("xn", Matrix.Translation(Vector((-half, 0, mid_z))) @ Matrix.Rotation(-math.pi / 2, 4, "Y") @ Matrix.Diagonal((span, 2 * half, 1.0, 1.0))),
        ("yp", Matrix.Translation(Vector((0, half, mid_z))) @ Matrix.Rotation(-math.pi / 2, 4, "X") @ Matrix.Diagonal((2 * half, span, 1.0, 1.0))),
        ("yn", Matrix.Translation(Vector((0, -half, mid_z))) @ Matrix.Rotation(math.pi / 2, 4, "X") @ Matrix.Diagonal((2 * half, span, 1.0, 1.0))),
    ]
    for name, local in faces:
        bpy.ops.mesh.primitive_plane_add(size=1.0)
        ob = bpy.context.object
        ob.name = f"housing_{panel_id}_{name}"
        ob.matrix_world = xform @ local
        ob.data.materials.append(mat)
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        collection.objects.link(ob)


def build_room(corners, env, collection):
    """Dark box sized to clear every panel, so nothing occludes a source."""
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    hx = max(abs(min(xs)), abs(max(xs))) + ROOM_MARGIN_M
    hy = max(abs(min(ys)), abs(max(ys))) + ROOM_MARGIN_M
    z_lo = min(zs) - ROOM_MARGIN_M
    z_hi = max(zs) + ROOM_MARGIN_M

    sky = surround_material("surround_walls", hex_to_linear(env["skyColor"]),
                            env["skyIntensity"] * GAIN, WALL_ALBEDO)
    ground = surround_material("surround_floor", hex_to_linear(env["groundColor"]),
                               env["groundIntensity"] * GAIN, FLOOR_ALBEDO)

    faces = [
        ("floor",   (0, 0, z_lo), (0, 0, 0),            (2 * hx, 2 * hy), ground),
        ("ceiling", (0, 0, z_hi), (math.pi, 0, 0),      (2 * hx, 2 * hy), sky),
        ("wall_xp", (hx, 0, (z_lo + z_hi) / 2), (0, -math.pi / 2, 0), (z_hi - z_lo, 2 * hy), sky),
        ("wall_xn", (-hx, 0, (z_lo + z_hi) / 2), (0, math.pi / 2, 0), (z_hi - z_lo, 2 * hy), sky),
        ("wall_yp", (0, hy, (z_lo + z_hi) / 2), (math.pi / 2, 0, 0),  (2 * hx, z_hi - z_lo), sky),
        ("wall_yn", (0, -hy, (z_lo + z_hi) / 2), (-math.pi / 2, 0, 0), (2 * hx, z_hi - z_lo), sky),
    ]
    for name, loc, rot, (sx, sy), mat in faces:
        bpy.ops.mesh.primitive_plane_add(size=1.0, location=loc, rotation=rot)
        ob = bpy.context.object
        ob.name = f"surround_{name}"
        ob.scale = (sx, sy, 1.0)
        ob.data.materials.append(mat)
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        collection.objects.link(ob)

    return {"half_x": hx, "half_y": hy, "z_lo": z_lo, "z_hi": z_hi}


def configure_render(scene, out_dir):
    scene.render.engine = "CYCLES"
    scene.cycles.samples = SAMPLES
    scene.cycles.use_denoising = True
    scene.cycles.max_bounces = 16
    scene.cycles.diffuse_bounces = 8          # bounce is the point of the ticket
    scene.cycles.glossy_bounces = 4
    scene.cycles.transmission_bounces = 8

    # Constraint 5: do not clamp highlights. Zero disables both clamps.
    scene.cycles.sample_clamp_direct = 0.0
    scene.cycles.sample_clamp_indirect = 0.0

    scene.render.resolution_x = RES_X
    scene.render.resolution_y = RES_Y
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False

    # Blender 4.x defaults to the AgX view transform. Saving an HDR through a tone map
    # would silently destroy every value above 1.0 — the one thing this file exists for.
    scene.view_settings.view_transform = "Raw"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI"):
            try:
                prefs.compute_device_type = backend
            except TypeError:
                continue
            prefs.get_devices()
            if any(d.type == backend for d in prefs.devices):
                for d in prefs.devices:
                    d.use = d.type in (backend, "CPU")
                scene.cycles.device = "GPU"
                print(f"[studio-hdri] render device: GPU ({backend})")
                break
        else:
            print("[studio-hdri] render device: CPU")
    except Exception as exc:                                  # noqa: BLE001
        print(f"[studio-hdri] GPU setup skipped ({exc}); using CPU")


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--lighting-json", default=None, help="studio-lighting.json (source of truth)")
    ap.add_argument("--name", default="companion-studio-v1")
    args = ap.parse_args(argv)

    if args.lighting_json and os.path.exists(args.lighting_json):
        with open(args.lighting_json, "r", encoding="utf-8") as fh:
            live = json.load(fh)
        env = live["environment"]
        panels = live["panels"]
        source = os.path.abspath(args.lighting_json)
    else:
        env, panels, source = EMBEDDED["environment"], EMBEDDED["panels"], "embedded copy"
    print(f"[studio-hdri] rig values from: {source}")

    # Empty scene. Constraint 4: no subject, stand-in or mannequin, ever — anything left
    # in the file is baked into the lighting for every companion forever.
    #
    # Cleared by hand rather than with read_factory_settings(): that reloads the addon
    # set, and an unregister bug in a third-party addon on the bake box takes Blender down
    # with an access violation before the scene is ever built. Launch with
    # --factory-startup as well (Render-StudioHdri.ps1 does) so no addon loads at all.
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"

    rig = bpy.data.collections.new("studio_rig")
    scene.collection.children.link(rig)

    corners = []
    placed = []
    for spec in panels:
        corners += build_panel(spec, rig)
        pos = direction(spec["azimuthDeg"], spec["elevationDeg"]) * PANEL_RADIUS_M
        size = 2.0 * PANEL_RADIUS_M * math.tan(math.radians(spec["angularSizeDeg"]) / 2.0)
        placed.append({
            "id": spec["id"],
            "position_m": [round(v, 2) for v in pos],
            "size_m": round(size, 2),
            "intensity_relative": spec["intensity"],
            "color": spec["color"],
            "housed": spec["id"] in HOUSED,
        })
        print(f"[studio-hdri] {spec['id']:<11} pos=({pos.x:6.2f},{pos.y:6.2f},{pos.z:6.2f})  "
              f"size={size:.2f} m  emission={spec['intensity']:.2f}")

    room = build_room(corners, env, rig)
    print(f"[studio-hdri] room x+-{room['half_x']:.2f} y+-{room['half_y']:.2f} "
          f"z {room['z_lo']:.2f}..{room['z_hi']:.2f} m")

    # Framing reference only. An empty never renders, so it cannot contaminate the bake.
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0.0, -4.0, 0.0))
    bpy.context.object.name = "REF_virtual_camera_do_not_render"

    # Panoramic camera AT THE ORIGIN: the environment is what the subject sees, not what
    # the virtual camera sees. (pi/2, 0, pi) puts the centre of the equirectangular image
    # on -Y, the direction of the viewer, and keeps +Z up.
    cam_data = bpy.data.cameras.new("pano")
    cam_data.type = "PANO"
    try:
        cam_data.panorama_type = "EQUIRECTANGULAR"
    except (AttributeError, TypeError):
        cam_data.cycles.panorama_type = "EQUIRECTANGULAR"
    cam = bpy.data.objects.new("pano_camera", cam_data)
    cam.location = (0.0, 0.0, 0.0)
    cam.rotation_euler = (math.pi / 2, 0.0, math.pi)
    scene.collection.objects.link(cam)
    scene.camera = cam

    configure_render(scene, args.out)
    os.makedirs(args.out, exist_ok=True)

    blend_path = os.path.join(args.out, f"{args.name}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    print(f"[studio-hdri] rendering {RES_X}x{RES_Y} at {SAMPLES} samples ...")
    bpy.ops.render.render(write_still=False)
    result = bpy.data.images["Render Result"]

    def save_render_result(path, fmt, depth):
        """Render Result can only leave Blender through save_render.

        That path applies the scene's view transform, which is why it is pinned to Raw:
        anything else would tone-map the file and throw away every value above 1.0.
        """
        s = scene.render.image_settings
        s.file_format = fmt
        try:
            s.color_depth = depth          # Radiance HDR exposes only its own depth
        except TypeError:
            pass
        s.color_management = "OVERRIDE"
        s.view_settings.view_transform = "Raw"
        s.view_settings.look = "None"
        s.view_settings.exposure = 0.0
        s.view_settings.gamma = 1.0
        result.save_render(filepath=path, scene=scene)
        print(f"[studio-hdri] wrote {path}  ({os.path.getsize(path) / 1024:.0f} KB)")

    # ---------------------------------------------------------------- handedness
    #
    # Blender's equirectangular camera sweeps longitude the OPPOSITE way to the
    # glTF/three.js convention the runtime samples with. Rendered as-is, the file encodes
    #
    #     u = 0.5 - azimuth/360
    #
    # where the runtime wants `u = 0.5 + azimuth/360`. That is a mirror, not a rotation, so
    # no camera orientation can fix it — the image has to be flipped.
    #
    # It is worth being blunt about how this got shipped once: a mirrored environment
    # preserves every elevation and every luminance ratio exactly, so the first version of
    # this script's verifier passed it. What it does NOT preserve is which side of the face
    # the key is on. The key landed camera-RIGHT, measured face contrast came out at 0.75
    # against 3.50 for the rig it replaces — below 1.0 meaning the shadow side was brighter
    # than the lit side. verify_studio_hdri.py now asserts handedness for exactly this
    # reason; see check_handedness there.
    raw_exr = os.path.join(args.out, ".raw-render.exr")
    save_render_result(raw_exr, "OPEN_EXR", "32")

    flipped = bpy.data.images.load(raw_exr)
    px = np.empty(len(flipped.pixels), dtype=np.float32)
    flipped.pixels.foreach_get(px)
    w, h = flipped.size
    # Reverse each row of RGBA pixels. Blender stores bottom-up, which a horizontal flip
    # does not care about.
    flipped.pixels.foreach_set(px.reshape((h, w, 4))[:, ::-1, :].reshape(-1))
    os.remove(raw_exr)

    def save_image(image, path, fmt):
        """Straight through, no colour management — the data is already linear."""
        image.file_format = fmt
        image.filepath_raw = path
        image.save()
        print(f"[studio-hdri] wrote {path}  ({os.path.getsize(path) / 1024:.0f} KB)")

    master_exr = os.path.join(args.out, f"{args.name}-{RES_X}.exr")
    master_hdr = os.path.join(args.out, f"{args.name}-{RES_X}.hdr")
    save_image(flipped, master_exr, "OPEN_EXR")
    save_image(flipped, master_hdr, "HDR")

    # The shipping file. The spec asks for 1024x512 — the runtime convolves down to a
    # 256px cube map, so anything above this is discarded. Resampled from the saved master
    # rather than re-rendered: same pixels, and averaging 4 samples per output pixel is
    # cleaner than rendering half-size. Written with save() rather than save_render() so
    # the already-linear data is passed straight through with no colour management at all.
    ship_hdr = os.path.join(args.out, f"{args.name}.hdr")
    ship_exr = os.path.join(args.out, f"{args.name}.exr")
    flipped.scale(RES_X // 2, RES_Y // 2)
    save_image(flipped, ship_hdr, "HDR")
    save_image(flipped, ship_exr, "OPEN_EXR")

    sidecar = {
        "name": args.name,
        "spec": "_docs/Architecture/Companions/metahuman/studio-hdri-authoring-spec.md",
        "ticket": 3660,
        "rig_source": source,
        "panel_radius_m": PANEL_RADIUS_M,
        "emission_gain": GAIN,
        "note_on_gain": (
            "Every emission is the spec's relative value times this gain. Ratios are "
            "untouched; the gain only lifts the key above 1.0 so the float headroom "
            "survives, per constraint 5. The import step re-normalises anyway."
        ),
        "surround": {
            "sky_color": env["skyColor"], "sky_emission": env["skyIntensity"],
            "ground_color": env["groundColor"], "ground_emission": env["groundIntensity"],
            "wall_albedo": WALL_ALBEDO, "floor_albedo": FLOOR_ALBEDO,
            "note_on_albedo": (
                "Albedo is the one value the spec does not fix. The spec gives the "
                "surround as emission only, and a pure emitter cannot bounce, so a "
                "diffuse term had to be chosen for bounce to exist at all."
            ),
        },
        "room_bounds_m": room,
        "panels": placed,
        "render": {
            "engine": "CYCLES", "camera": "PANO/EQUIRECTANGULAR at origin",
            "orientation": "u = 0.5 + azimuth/360, +Z up; horizontally flipped after render because Blender sweeps longitude opposite to the glTF/three.js convention",
            "resolution": [RES_X, RES_Y], "samples": SAMPLES,
            "denoised": True, "clamping": "disabled (constraint 5)",
            "view_transform": "Raw",
        },
        "outputs": {
            "ship": os.path.basename(ship_hdr),
            "ship_exr": os.path.basename(ship_exr),
            "master_hdr": os.path.basename(master_hdr),
            "master_exr": os.path.basename(master_exr),
            "blend": os.path.basename(blend_path),
        },
    }
    with open(os.path.join(args.out, f"{args.name}.json"), "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2)

    print("[studio-hdri] done")


if __name__ == "__main__":
    main()
