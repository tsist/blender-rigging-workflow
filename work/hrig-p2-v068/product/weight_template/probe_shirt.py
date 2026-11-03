import json
import sys
from pathlib import Path

import bpy

src = Path(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.open_mainfile(filepath=str(src), load_ui=False, use_scripts=False)
bpy.context.view_layer.update()
for name in (
    "GEO-snow-body",
    "GEO-snow-shirt",
    "GEO-snow-head",
    "GEO-snow-eye_anim",
    "GEO-snow-eyes",
):
    obj = bpy.data.objects.get(name)
    if not obj:
        continue
    points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    print(
        name,
        "n",
        len(points),
        "min",
        [round(min(point[i] for point in points), 4) for i in range(3)],
        "max",
        [round(max(point[i] for point in points), 4) for i in range(3)],
        "center",
        [round(sum(point[i] for point in points) / len(points), 4) for i in range(3)],
    )
for obj in bpy.data.objects:
    if obj.type == "MESH" and (
        "eye" in obj.name.lower() or "head" in obj.name.lower()
    ):
        print(
            "OBJ",
            obj.name,
            "loc",
            [round(value, 4) for value in obj.matrix_world.translation],
        )
