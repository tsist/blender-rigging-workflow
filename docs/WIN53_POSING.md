# Win53 posing runtime

This opt-in source edition provides the add-on used by the Win53 posing and
retargeting CLI. Its runtime identity is `0.42.3-win53-phase3`. The pinned host
is Windows Blender 5.3.0 build `02f0899d78e3`, started offline with automatic
script execution disabled. The existing 0.40.1 download and v003 CLI preview
have separate package and support contracts.

## Build and select the package

From a repository checkout, use Python 3.12 or newer:

```powershell
python -B scripts/build_win53_posing.py --output dist/rigging_workflow-win53-posing.zip
Get-FileHash -Algorithm SHA256 dist/rigging_workflow-win53-posing.zip
```

The build checks a relative source inventory, validates each input SHA-256,
emits a complete runtime manifest and verifies every member after writing.
The ZIP uses fixed metadata and an uncompressed format so identical sources
produce identical bytes. Build into a new path.

For [blenderctl](https://github.com/tsist/blenderctl)'s matching Win53 posing
handoff, supply this ZIP via `--rigging-package` and its lowercase SHA-256 via
`--rigging-package-sha256`, along with the explicit Blender executable.
The CLI unpacks the package in a private profile. To use the native panel,
install the ZIP in a separate Blender profile and enable this edition there.
Keep one Rigging Workflow edition enabled in that profile.

## Workflow scope

- Inspect native FBX Takes and Slots, build a body Profile from a reference
  FBX, and reuse it for animation-only files. Exact Take/Slot selection follows
  the actual import receipt, including Blender-created name suffixes.
- Use verified C94 controls or a supported native FK target. Keep the
  target's original rig, rest transforms, weights and base Action in the
  scene. Retargeting writes a distinct editable Action.
- Use the shoulder-girdle and ROM inspection tools, the three-beat authoring
  workflow, and the calibrated FK heel and gait contact candidates. A gait
  preview retains the base Action for restoration.
- The companion CLI opens a separately owned workspace and captures keyed
  hand edits with a SHA-256-bound receipt. Generic FK retargeting assumes
  the supported original FK graph and a single base Action. The named shoulder
  adapter covers C94 and the validated 19-bone CesiumMan-style graph.

Provide your own authorized input rigs, meshes, calibrations and motion
files. The build assembles the reviewed code from `win53-posing/overlay`
and unchanged sources from the public baseline and v003 preview. It contains
no character assets, private template, motion samples, MMD reference PMX,
compiled CUDA kernel or local audit files. Optional MMD and GPU features are
outside this edition's qualification. See the packaged third-party notices
for the retained source dependencies and licenses.

The companion [rigging skill](../skills/blender-rigging/SKILL.md) explains
how to choose the route and verify an editable output. Always cold-open a
new candidate, inspect the intended native Action and Slot, and compare the
same motion at equivalent source times before using the result.
