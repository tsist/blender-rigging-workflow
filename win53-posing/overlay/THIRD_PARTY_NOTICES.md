# Asset-free Win53 posing runtime notices

Project-owned Rigging Workflow code and the source-only build are licensed under
GPL-3.0-or-later. See the accompanying LICENSE. The source inventory lists each
reviewed member and its SHA-256.

- `vendor/pycpd` is MIT-licensed, copyright (c) 2010–2016 Siavash Khallaghi.
  See the retained `vendor/pycpd/LICENSE`. Source is pinned to upstream commit
  `e5ca02d2501fb4b633c1664de939c336ffa2349e`.
- `volume` is project-owned GPL-3.0-or-later code. Its CUDA kernel source is
  included alongside `volume/LICENSE`. No compiled GPU artifact is included.
- Blender, its native importers, NumPy, Rigify and optional MMD Tools/OpenCC
  files are separate host dependencies. The build does not fetch or install
  any dependency.

Provide your authorized character, rig, calibration and motion files
separately. This build contains no character assets, templates, downloaded
animation, bundled PMX or third-party add-on binaries.
