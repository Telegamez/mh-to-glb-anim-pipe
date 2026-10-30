# Kimodo Studio and bridge (experimental source)

These experimental tools explore generating Kimodo motion, previewing it on a MetaHuman in Three.js, and retargeting it in Unreal for the animation-bake pipeline. This is a source snapshot for collaboration, not a working standalone distribution yet.

- `studio/`: local Python server, Three.js interface and browser retargeting.
- `bridge/`: BVH-to-FBX conversion, Unreal retargeting, evaluated animation bake, and inspection utilities.

## Current limitations

The studio's `static/app.js` imports `static/vendor/mh-materials.js`, which is not included: the local generated bundle incorporates code from the Telegamez application and third-party dependencies, and needs a separate provenance and license review and a reusable replacement. The Studio page will not load without it. The server also references machine-specific paths and Telegamez assets; the bridge launcher defaults to a local project and a specific target skeleton. Do not run these scripts without inspecting and configuring them for your environment. The original launcher can stop another process listening on port 7860; review it before running.

Original tools were used on Windows with a separately installed [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo), Python environment, GPU, Unreal project, Blender, and locally generated character assets. Kimodo, model weights, generated takes, characters, textures, and Telegamez application code are not included. Their respective licenses and terms apply separately.

## Work needed for a public release

1. Replace or extract the material and loader integration with independently reusable source and third-party license notices.
2. Replace hard-coded paths and target assets with explicit configuration, validate dependencies, and prevent automatic termination of unrelated processes.
3. Document Kimodo installation, model and hardware requirements, environment setup, character input layout, and how to run both preview and bake.
4. Test a clean install using a shareable character and evaluate browser preview against the evaluated Unreal bake.
5. Audit Unreal output destinations and generated files, publish no assets without redistribution rights, and add meaningful automated checks.

Browser retargeting is a preview and does not run Unreal's MetaHuman post-process rig. Kimodo motion does not include finger animation in this workflow. Retargeting and garment deformation should be reviewed before using output in production.
