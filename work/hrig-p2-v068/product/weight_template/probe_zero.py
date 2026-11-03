import json
import sys
from pathlib import Path
import bpy

bpy.ops.wm.open_mainfile(
    filepath=sys.argv[sys.argv.index("--") + 1], load_ui=False, use_scripts=False
)
body = bpy.data.objects["GEO-snow-body"]
zeros = []
for vertex in body.data.vertices:
    for membership in vertex.groups:
        if float(membership.weight) == 0.0:
            zeros.append(
                {
                    "vertex": vertex.index,
                    "group": membership.group,
                    "name": body.vertex_groups[membership.group].name,
                }
            )
print(json.dumps({"zero_memberships": len(zeros), "examples": zeros[:20]}))
