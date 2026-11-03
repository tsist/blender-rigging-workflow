import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

request = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
bpy.ops.wm.open_mainfile(filepath=request["candidate"], load_ui=False, use_scripts=False)
target = bpy.data.objects[request["target"]]
rig = bpy.data.objects[request["rig"]]
target.modifiers.clear()
mod = target.modifiers.new("WT-LBS-Diagnose", "ARMATURE")
mod.object = rig
mod.use_vertex_groups = True
mod.use_bone_envelopes = False
mod.use_deform_preserve_volume = False
rig.data.pose_position = "POSE"
pose = rig.pose.bones[request["pose_bone"]]
pose.rotation_mode = "QUATERNION"
pose.rotation_quaternion = Quaternion((0, 0, 1), request["angle"])
bpy.context.view_layer.update()
deps = bpy.context.evaluated_depsgraph_get()
deps.update()
ev = target.evaluated_get(deps)
mesh = ev.to_mesh()
names = {g.index: g.name for g in target.vertex_groups}
deform = {b.name for b in rig.data.bones if b.use_deform}
rows = []
for v in target.data.vertices:
    rows.append({names[m.group]: float(m.weight) for m in v.groups})


def matrix_for(name, use_channel=False):
    bone = rig.data.bones[name]
    pb = rig.pose.bones[name]
    if use_channel:
        skin = pb.matrix_channel
    else:
        skin = pb.matrix @ bone.matrix_local.inverted()
    return rig.matrix_world @ skin @ rig.matrix_world.inverted() @ target.matrix_world


def error(use_channel=False, normalize=True, use_evaluated=False):
    eval_rig = rig.evaluated_get(deps) if use_evaluated else rig
    eval_pose = eval_rig.pose.bones
    values = []
    predicted_values = []
    actual_values = []
    for i, v in enumerate(target.data.vertices):
        row = {n: w for n, w in rows[i].items() if n in deform}
        total = math.fsum(row.values())
        predicted = Vector((0, 0, 0))
        if total > 0:
            for name, weight in row.items():
                bone = rig.data.bones[name]
                pb = eval_pose[name]
                skin = pb.matrix_channel if use_channel else pb.matrix @ bone.matrix_local.inverted()
                m = rig.matrix_world @ skin @ rig.matrix_world.inverted() @ target.matrix_world
                predicted += (m @ v.co) * ((weight / total) if normalize else weight)
        actual = ev.matrix_world @ mesh.vertices[i].co
        predicted_values.append(predicted)
        actual_values.append(actual)
        values.append((actual - predicted).length)
    worst = max(range(len(values)), key=lambda i: values[i])
    return {
        "max": max(values),
        "mean": math.fsum(values) / len(values),
        "worst": worst,
        "actual": list(actual_values[worst]),
        "predicted": list(predicted_values[worst]),
        "row": rows[worst],
        "sum": math.fsum(rows[worst].values()),
    }


result = {
    "target": target.name,
    "vertices": len(target.data.vertices),
    "pose": request["pose_bone"],
    "variants": {
        "normalized_local": error(False, True, False),
        "raw_local": error(False, False, False),
        "normalized_channel": error(True, True, False),
        "raw_channel": error(True, False, False),
        "normalized_evaluated": error(False, True, True),
        "normalized_channel_evaluated": error(True, True, True),
    },
}
print(json.dumps(result, indent=2))
ev.to_mesh_clear()
