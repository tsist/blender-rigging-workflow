# Source and dependencies

This distribution contains selected self-authored Blender AI / tsist development code, released under GPL-3.0-or-later. Existing source boundaries and component origins are retained in SOURCE_MAP.json. Packaging changes replace a small set of absolute workstation code paths with package-relative lookup and provide explicit development import setup.

The included `tools/blenderctl/inspection.py` and `protocol.py` support modules originate from this owner's GPL-3.0-or-later [blenderctl project](https://github.com/tsist/blenderctl); the same GPL license applies here. Source adapters that refer to Snow retain its Blender Foundation / studio.blender.org CC BY 4.0 attribution, but no Snow model, weight data or template catalog is distributed.

Python, Blender (`bpy`, `mathutils`, `bmesh`) and NumPy are external runtimes, not bundled. Consult the actual installed distributions for their licenses. No character `.blend`, model, texture, commercial add-on, AI model, private profile, credential or original audit data is distributed.

Auto-Rig Pro, auto-rigpro-ai, Rigify, wiggle-2-rtx-version, the Aon panel addition script and voxel-skinning implementations are future research/integration candidates. Their code and assets are not included. Specific license, version and attribution obligations must be checked before future code inclusion. Project names do not imply endorsement or partnership.
