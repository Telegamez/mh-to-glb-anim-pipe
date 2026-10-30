# Roadmap

Our goal is to make expressive MetaHuman characters practical for Three.js and other web experiences, and develop that capability openly with the people who use it. These are proposed improvements, not a release schedule or a claim that every feature is implemented. Contributions and alternative approaches are welcome.

## Make the pipeline reproducible

- [ ] Verify a fresh-character export on a clean Windows machine; fix blockers and publish exact tested versions.
- [ ] Maintain a compatibility matrix for Unreal, MetaHuman, Blender, OS, and browsers.
- [ ] Provide a documented, deterministic core pipeline command without requiring an AI assistant or Telegamez services.
- [ ] Reconcile stale setup, bootstrap, reweighting, version, output, and viewer documentation.
- [ ] Add legally shareable fixtures and automated checks for stages, GLB validity, deformation, and viewer loading.
- [ ] Document provenance and redistribution rights for checked-in characters, textures, animations, and third-party content.

## Hair

- [ ] Improve hair-card coverage, transparency, sorting, shadows, and scalp blending across styles and lighting.
- [ ] Improve eyebrows, eyelashes, beards, and mustaches, including facial deformation.
- [ ] Compare output against Unreal references and collect screenshots and device performance measurements.
- [ ] Investigate groom-to-card and other browser-friendly workflows, including tooling and licensing constraints.

**A useful first contribution:** document a reproducible hair rendering failure with screenshots and device details, or improve one viewer material.

## Clothing

- [ ] Reconstruct masked and layered clothing colors and materials.
- [ ] Improve skin-weight transfer and deformation across body shapes, garments, and poses; reduce clipping and tearing.
- [ ] Define test poses and visual regressions for garments, including movement and extreme joints.
- [ ] Explore web-appropriate layering, material variants, and optional cloth motion.

**A useful first contribution:** identify a material input lost between Unreal and the viewer and document or implement its mapping with before/after evidence.

## Locomotion and animation

- [ ] Document a skeleton and animation contract: bone names, rest poses, units, clip naming, and compatibility checks.
- [ ] Add a minimal Three.js example for idle, walk, run, turns, transitions, and a controllable character.
- [ ] Investigate retargeting, root motion versus in-place movement, foot contact, sliding, loop seams, and blending across characters.
- [ ] Measure animation quality and runtime cost on desktop and mobile.
- [ ] Explore [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) as a potential offline motion-generation and retargeting workflow, and [NVIDIA MotionBricks](https://github.com/NVlabs/motionbricks) as research for controllable motion and locomotion. Evaluate skeleton conversion, quality, hardware and runtime requirements, model and asset licenses, and applicable MetaHuman terms. These are exploratory ideas, not existing integrations or browser support.

**A useful first contribution:** demonstrate a legally usable walk cycle retargeted to an exported character, document the steps, and show foot contact and root-motion behavior in Three.js.

## Web quality and integration

- [ ] Publish a minimal standalone Three.js example explaining GLB, Draco, sidecar materials and textures, facial controls, and animation loading.
- [ ] Improve eye occlusion, eyelashes, face materials, and expression fidelity; document differences from Unreal.
- [ ] Measure download size, memory, frame rate, and load time; evaluate texture compression and LOD strategies.
- [ ] Investigate runtime LOD selection, including separate LOD files and a possible `MSFT_lod` post-processing approach.
- [ ] Separate reusable pipeline and viewer features from Telegamez-specific publishing and runtime integration.

## Community

- [ ] Enable a public place for questions, bug reports, ideas, and contribution coordination, and link it from the README.
- [ ] Add contributor guidance, issue templates, a code of conduct, and clearly scoped starter tasks.
- [ ] Invite independent developers to try their own characters and share results, device measurements, and reproducible failures.
- [ ] Document how maintainers review proposals, handle asset rights, and make project decisions.

A good proposal explains the problem, intended user, how to reproduce or demonstrate it, and how the result will be tested.
