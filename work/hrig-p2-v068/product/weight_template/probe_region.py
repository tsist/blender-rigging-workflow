"""Read-only region probe for real Snow shirt-versus-body correspondence."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree


def segment_distance(point, start, end):
    edge = end - start
    t = max(0.0, min(1.0, (point - start).dot(edge) / edge.length_squared))
    return (point - (start + edge * t)).length


def main():
    source = Path(sys.argv[sys.argv.index("--") + 1])
    bpy.ops.wm.open_mainfile(
        filepath=str(source), load_ui=False, use_scripts=False
    )
    bpy.context.view_layer.update()
    body = bpy.data.objects["GEO-snow-body"]
    shirt = bpy.data.objects["GEO-snow-shirt"]
    rig = bpy.data.objects["RIG-Snow"]
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    group_names = {group.index: group.name for group in body.vertex_groups}
    body_sums = []
    for vertex in body.data.vertices:
        body_sums.append(
            math.fsum(
                float(membership.weight)
                for membership in vertex.groups
                if group_names[membership.group] in deform
            )
        )
    segments = []
    for name in ("UpperArm.L", "Forearm.L", "Wrist.L"):
        bone = rig.data.bones[name]
        segments.append(
            (
                name,
                rig.matrix_world @ bone.head_local,
                rig.matrix_world @ bone.tail_local,
            )
        )
    body_points = [body.matrix_world @ vertex.co for vertex in body.data.vertices]
    body.data.calc_loop_triangles()
    body_triangles = [tuple(triangle.vertices) for triangle in body.data.loop_triangles]
    tree = BVHTree.FromPolygons(body_points, body_triangles, all_triangles=True)
    results = {}
    for radius in (0.055, 0.07, 0.085, 0.10, 0.115, 0.13):
        selected_body = [
            index
            for index, point in enumerate(body_points)
            if body_sums[index] > 0.999
            and min(segment_distance(point, start, end) for _, start, end in segments)
            <= radius
        ]
        selected_body_set = set(selected_body)
        body_triangles_selected = [
            index
            for index, ids in enumerate(body_triangles)
            if all(vertex in selected_body_set for vertex in ids)
        ]
        shirt_points = [
            shirt.matrix_world @ vertex.co for vertex in shirt.data.vertices
        ]
        shirt_selected = [
            index
            for index, point in enumerate(shirt_points)
            if min(segment_distance(point, start, end) for _, start, end in segments)
            <= radius
        ]
        shirt_selected_set = set(shirt_selected)
        shirt.data.calc_loop_triangles()
        shirt_triangles = [
            tuple(triangle.vertices) for triangle in shirt.data.loop_triangles
        ]
        shirt_triangle_indices = [
            index
            for index, ids in enumerate(shirt_triangles)
            if all(vertex in shirt_selected_set for vertex in ids)
        ]
        distances = []
        source_sums = []
        for index in shirt_selected:
            near, normal, triangle, distance = tree.find_nearest(shirt_points[index])
            if triangle is None:
                continue
            distances.append(distance)
            ids = body_triangles[triangle]
            source_sums.extend(body_sums[vertex] for vertex in ids)
        results[str(radius)] = {
            "body_vertices": len(selected_body),
            "body_triangles": len(body_triangles_selected),
            "shirt_vertices": len(shirt_selected),
            "shirt_triangles": len(shirt_triangle_indices),
            "max_distance": max(distances, default=None),
            "mean_distance": (
                math.fsum(distances) / len(distances) if distances else None
            ),
            "source_triangle_sum_min": min(source_sums, default=None),
            "source_triangle_sum_max": max(source_sums, default=None),
        }
    print(
        json.dumps(
            {
                "radius_results": results,
                "selected_shirt_vertices_at_0.10": [
                    index
                    for index, point in enumerate(
                        [shirt.matrix_world @ vertex.co for vertex in shirt.data.vertices]
                    )
                    if min(
                        segment_distance(point, start, end)
                        for _, start, end in segments
                    )
                    <= 0.10
                ][:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
