import json, sys
from pathlib import Path
import bpy

bpy.ops.wm.open_mainfile(filepath=sys.argv[sys.argv.index("--") + 1], load_ui=False, use_scripts=False)
rig = bpy.data.objects["RIG-Snow"]
rows = []
for name in (
    "DEF-Forearm_1.L", "DEF-Forearm_2.L", "DEF-UpperArm_1.L",
    "DEF-UpperArm_2.L", "DEF-Wrist.L", "DEF-Chest", "DEF-RibCage",
    "DEF-Spine", "DEF-Head", "DEF-Finger_Index_Carpal.L",
    "DEF-Finger_Index1.L", "DEF-Finger_Index2.L", "DEF-Finger_Index3.L",
):
    b = rig.data.bones.get(name)
    if not b:
        continue
    rows.append({
        "name": name,
        "bbone_segments": b.bbone_segments,
        "use_deform": b.use_deform,
        "inherit_scale": b.inherit_scale,
        "use_inherit_rotation": b.use_inherit_rotation,
        "use_connect": b.use_connect,
        "constraints": [c.type for c in rig.pose.bones[name].constraints],
        "drivers": [c.data_path for c in rig.animation_data.drivers] if rig.animation_data else [],
    })
print(json.dumps(rows, indent=2))
