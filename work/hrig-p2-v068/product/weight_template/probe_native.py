"""Read-only native probe for the owned Snow weight-template work."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import bpy


def sha(value):
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def topology(mesh):
    return sha(
        {
            "vertices": len(mesh.vertices),
            "edges": [list(edge.vertices) for edge in mesh.edges],
            "faces": [list(face.vertices) for face in mesh.polygons],
        }
    )


def main():
    source = Path(sys.argv[sys.argv.index("--") + 1])
    bpy.ops.wm.open_mainfile(
        filepath=str(source), load_ui=False, use_scripts=False
    )
    bpy.context.view_layer.update()
    rig = bpy.data.objects["RIG-Snow"]
    body = bpy.data.objects["GEO-snow-body"]
    shirt = bpy.data.objects["GEO-snow-shirt"]
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    group_names = {group.index: group.name for group in body.vertex_groups}
    group_stats = []
    for group in body.vertex_groups:
        members = []
        for vertex in body.data.vertices:
            for membership in vertex.groups:
                if membership.group == group.index:
                    members.append(float(membership.weight))
                    break
        positive = [value for value in members if value > 0]
        group_stats.append(
            {
                "index": group.index,
                "name": group.name,
                "lock": bool(group.lock_weight),
                "bone_exists": group.name in rig.data.bones,
                "deform": group.name in deform,
                "positive_vertices": len(positive),
                "weight_sum": math.fsum(positive),
                "weight_min": min(positive, default=0.0),
                "weight_max": max(positive, default=0.0),
            }
        )
    sums = []
    influence_counts = []
    non_mapped = set()
    for vertex in body.data.vertices:
        row = [
            float(membership.weight)
            for membership in vertex.groups
            if group_names[membership.group] in deform
        ]
        sums.append(math.fsum(row))
        influence_counts.append(sum(value > 0 for value in row))
        for membership in vertex.groups:
            if group_names[membership.group] not in deform:
                non_mapped.add(group_names[membership.group])
    bones = []
    for name in (
        "root",
        "Hips",
        "Spine",
        "RibCage",
        "Chest",
        "Neck",
        "Head",
        "Shoulder.L",
        "UpperArm.L",
        "Forearm.L",
        "Wrist.L",
        "Finger_Index1.L",
        "Thigh.L",
        "Shin.L",
        "Foot.L",
    ):
        bone = rig.data.bones.get(name)
        if not bone:
            continue
        bones.append(
            {
                "name": bone.name,
                "deform": bone.use_deform,
                "parent": bone.parent.name if bone.parent else None,
                "head_local": list(bone.head_local),
                "tail_local": list(bone.tail_local),
                "matrix_local": [[value for value in row] for row in bone.matrix_local],
            }
        )
    shirt_world = [shirt.matrix_world @ vertex.co for vertex in shirt.data.vertices]
    body_world = [body.matrix_world @ vertex.co for vertex in body.data.vertices]
    report = {
        "source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "blender": list(bpy.app.version),
        "build": bpy.app.build_hash.decode(),
        "autoexec_enabled": bool(
            bpy.context.preferences.filepaths.use_scripts_auto_execute
        ),
        "objects": {
            "body": {
                "name": body.name,
                "data": body.data.name,
                "vertices": len(body.data.vertices),
                "polygons": len(body.data.polygons),
                "edges": len(body.data.edges),
                "triangles": len(body.data.loop_triangles)
                if body.data.calc_loop_triangles() is None
                else len(body.data.loop_triangles),
                "groups": len(body.vertex_groups),
                "topology_sha256": topology(body.data),
                "matrix_world": [[value for value in row] for row in body.matrix_world],
                "modifiers": [
                    {"name": modifier.name, "type": modifier.type, "object": modifier.object.name if getattr(modifier, "object", None) else None}
                    for modifier in body.modifiers
                ],
                "parent": body.parent.name if body.parent else None,
                "shape_keys": len(body.data.shape_keys.key_blocks)
                if body.data.shape_keys
                else 0,
            },
            "shirt": {
                "name": shirt.name,
                "data": shirt.data.name,
                "vertices": len(shirt.data.vertices),
                "polygons": len(shirt.data.polygons),
                "edges": len(shirt.data.edges),
                "groups": len(shirt.vertex_groups),
                "group_names": [group.name for group in shirt.vertex_groups],
                "topology_sha256": topology(shirt.data),
                "matrix_world": [[value for value in row] for row in shirt.matrix_world],
                "modifiers": [
                    {"name": modifier.name, "type": modifier.type, "object": modifier.object.name if getattr(modifier, "object", None) else None}
                    for modifier in shirt.modifiers
                ],
                "parent": shirt.parent.name if shirt.parent else None,
                "shape_keys": len(shirt.data.shape_keys.key_blocks)
                if shirt.data.shape_keys
                else 0,
                "bounds_world": [
                    [min(point[axis] for point in shirt_world) for axis in range(3)],
                    [max(point[axis] for point in shirt_world) for axis in range(3)],
                ],
            },
            "rig": {
                "name": rig.name,
                "bones": len(rig.data.bones),
                "deform_bones": len(deform),
                "matrix_world": [[value for value in row] for row in rig.matrix_world],
            },
        },
        "body_bounds_world": [
            [min(point[axis] for point in body_world) for axis in range(3)],
            [max(point[axis] for point in body_world) for axis in range(3)],
        ],
        "body_weight_stats": {
            "deform_sum_min": min(sums),
            "deform_sum_max": max(sums),
            "non_unit_count": sum(abs(value - 1.0) > 1e-6 for value in sums),
            "unweighted_count": sum(value <= 1e-8 for value in sums),
            "max_influences": max(influence_counts),
            "influence_histogram": {
                str(count): influence_counts.count(count)
                for count in sorted(set(influence_counts))
            },
            "unmapped_non_deform_groups": sorted(non_mapped),
            "sum_examples": [
                {"vertex": index, "sum": sums[index]}
                for index in range(min(12, len(sums)))
            ],
        },
        "group_stats": group_stats,
        "relevant_bones": bones,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
