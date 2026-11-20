# Runtime source and dependency notices

Project-owned workflow code is GPL-3.0-or-later; the full license is in LICENSE.
SOURCE-MAP.json preserves the historical relocation origins and a current file
inventory, excluding its own bytes and the runtime manifest to avoid hash cycles.
The retargeting algorithms and orchestration are locally authored; Rokoko/Expy
were design references, with no upstream source or runtime dependency incorporated.

Bundled code:
- vendor/pycpd: MIT, copyright (c) 2010-2016 Siavash Khallaghi;
  fixed upstream commit e5ca02d2501fb4b633c1664de939c336ffa2349e.
  The upstream MIT license is retained beside the files.
- volume: project-owned harmonic diffusion kernel, GPL-3.0-or-later;
  complete license retained in volume/LICENSE.

Bundled reference resource:
- resources/mmd/uka/MS_Uka.pmx: CC BY 4.0, unmodified PMX, fixed upstream
  commit 659f0d1a740fec68bb2817dc9c694a35e7a15f12.
  CG-CA Uka (c) 2023-2024 by Nagoya Institute of Technology, Moonshot R&D
  Goal 1 Avatar Symbiotic Society.
  Source https://github.com/mmdagent-ex/uka ; license
  https://creativecommons.org/licenses/by/4.0/legalcode . Attribution and
  the upstream Usage Guideline/Disclaimer are retained beside the PMX.
  The code GPL does not replace this resource's CC BY license. Textures are
  not included: this is a source skeleton/IK reference, not a complete visual model.

Host dependencies, used through an existing installation and not vendored:
- Blender 5.2.1 LTS: bpy/mathutils/NumPy and installed native importers.
  io_scene_fbx is GPL-2.0-or-later; io_scene_gltf2 is Apache-2.0.
- MMD source/camera file import requires MMD Tools 4.5.14, GPL-3.0-or-later,
  and its OpenCC-python-reimplemented 0.1.7 wheel, Apache License 2.0.
  https://github.com/MMD-Blender/blender_mmd_tools . The GUI does not enable
  MMD Tools; the explicit background importer owns registration and removal.
- MMD file/camera background import uses this project's blenderctl 0.85.0
  and PowerShell 7 on Windows. They are external host tools, not part of this ZIP.

This is a local Windows/Blender development package. F:/Blender AI defaults
describe the tested machine; on another machine choose the actual CLI/backend/
OpenCC paths in the file-import and camera panels. No dependency is auto-installed.
Previously imported native source Actions do not require MMD Tools for playback.
Small internal worker packets are local execution snapshots, not standalone
redistribution packages. Preserve resource sidecars when separately transferring
the reference PMX. See RETARGET-USER-GUIDE.md for the supported local paths.

No private C94 rig template, target model, font or texture, Mixamo/CMU sample
motion, VMD, or music is included. Sample research permissions do not grant
unrelated asset redistribution rights. No public release has been performed.

- Linux read leases and namespace monitoring use Blenderctl’s GPL-3.0-or-later
  `tools/blenderctl/linux_file_guard.py` from the qualified [workflow read guard](https://github.com/tsist/blenderctl/pull/9)
  (upstream file SHA-256 `aa4e5dc02841b07233e131da64492119ce049a93d4600b4f035833a8651615b6`).
  The only change to that file is the import of its local Failure exception adapter.
