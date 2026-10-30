# Kimodo Studio and bridge

An experimental local workflow to generate motion with [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo), preview it on a MetaHuman GLB in Three.js, and retarget a take in Unreal for the stage 05 animation bake. The model, weights, Unreal, Blender, and character assets are separate dependencies; their own terms apply.

## Setup

1. Install Kimodo following its official instructions, including its model weights and compatible Python/PyTorch environment. This workflow was developed with `Kimodo-SOMA-RP-v1.1`; model availability and hardware requirements depend on that installation.
2. In the Kimodo environment install the server dependencies: `python -m pip install fastapi uvicorn`. NumPy, PyTorch, and Kimodo are also required.
3. Put a compatible GLB under a folder named for the character, for example `C:/characters/ada/ada.glb`. Use assets you have rights to use. Studio loads clothing and hair-card textures from a neighboring `mh_materials.json` when present; it does not reproduce every production hair shader.
4. On Windows with PowerShell 7, run from the repository root:

   ```powershell
   ./experimental/kimodo/studio/start-studio.ps1 -Python C:/path/to/kimodo/.venv/Scripts/python.exe -Characters C:/characters
   ```

   Open `http://localhost:7870`. Wait for the model status to become ready. Use `-Port` to change the port, `-Animations` to provide a directory of animation-only GLBs, and `-TargetMesh` for the optional Unreal bake. Studio binds to localhost. It does not stop the original Kimodo demo; both processes may exceed available GPU memory.

Studio also accepts `KIMODO_STUDIO_MODEL`, `KIMODO_STUDIO_PORT`, `KIMODO_STUDIO_DATA`, `KIMODO_STUDIO_LIBRARY`, `KIMODO_CHARACTERS`, `KIMODO_ANIMATIONS`, and `KIMODO_TARGET_MESH` environment variables. Generated takes are stored in `studio/library/` by default. Model loading and browser decoder downloads may need network access.

## Unreal retarget and bake

Configure the repository's `_config/pipeline.yaml`, including the legacy `ue_project_path`, `ue_editor_cmd`, and `blender_exe` fields. The Unreal project needs a compatible MetaHuman body, `/Game/MetaHumans/Common/Common/IK_metahuman`, and the plugins required by the existing animation bake and IK retargeting tools. Pass your target skeletal mesh asset path explicitly:

```powershell
./experimental/kimodo/bridge/tools/run_kimodo_take.ps1 -Bvh C:/path/to/take.bvh -Take KM_Test -TargetMesh /Game/MetaHumans/YourCharacter/Body/SKM_YourCharacter_BodyMesh
```

The bridge imports and writes assets under `/Game/Kimodo`, including a retargeted AnimSequence, and creates per-take output under `bridge/_work/`. It can replace assets with matching names; use a test project and distinct take names. `-RebuildRigs` recreates shared rigs. `-SkipBake` stops after retargeting. The Studio Bake button uses the same bridge and requires `-TargetMesh` at Studio launch. The evaluated bake uses stage 05 and its own Unreal project assumptions; verify the output against your character before production.

For looping motion, `bridge/tools/make_bookends.py` can make constraints from a Kimodo take. `seam_npz.py` and `compare_segments.py` provide diagnostic checks.

## Limitations

Browser retargeting is a preview; it does not run Unreal's MetaHuman post-process rig, reproduce production twist correction, or automatically adapt animation clips between different rest poses. The portable material adapter can differ from the original viewer, especially hair shading. This workflow does not animate fingers from Kimodo. KTX2/Draco/meshopt support uses Three.js decoders fetched from a CDN. Review clothing deformation, foot sliding, seams, and source licenses before shipping.

The standalone integration and Unreal retarget have not yet been verified end to end on a clean machine. Reports with versions, logs, and legally shareable reproductions are welcome.
