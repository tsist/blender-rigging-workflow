import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

source_path = Path(sys.argv[sys.argv.index("--") + 1])
template_path = Path(sys.argv[sys.argv.index("--") + 2])
bpy.ops.wm.open_mainfile(filepath=str(source_path), load_ui=False, use_scripts=False)
bpy.context.view_layer.update()
body = bpy.data.objects["GEO-snow-body"]
shirt = bpy.data.objects["GEO-snow-shirt"]
rig = bpy.data.objects["RIG-Snow"]
template = json.loads(template_path.read_text(encoding="utf-8"))
deform = {bone.name for bone in rig.data.bones if bone.use_deform}
group_names = {group.index: group.name for group in body.vertex_groups}
rows = []
sums = []
for vertex in body.data.vertices:
    row = {}
    for membership in vertex.groups:
        name = group_names[membership.group]
        if name in deform:
            row[name] = float(membership.weight)
    rows.append(row)
    sums.append(math.fsum(row.values()))
body.calc_loop_triangles()
body_tris = [list(t.vertices) for t in body.data.loop_triangles]
shirt.calc_loop_triangles()
shirt_tris = [list(t.vertices) for t in shirt.data.loop_triangles]


def select_vertices(obj, xmax, ymin, zmin, zmax):
    points = [obj.matrix_world @ v.co for v in obj.data.vertices]
    return {
        i
        for i, p in enumerate(points)
        if abs(p.x) <= xmax and p.y >= ymin and zmin <= p.z <= zmax
    }


def patch_from_shirt(vert_set):
    tris = [t for t in shirt_tris if all(v in vert_set for v in t)]
    used = sorted({v for t in tris for v in t})
    return used, tris


def barycentric(point, a, b, c):
    u = b - a
    v = c - a
    w = point - a
    uu = u.dot(u)
    uv = u.dot(v)
    vv = v.dot(v)
    wu = w.dot(u)
    wv = w.dot(v)
    den = uu * vv - uv * uv
    y = (vv * wu - uv * wv) / den
    z = (uu * wv - uv * wu) / den
    values = [1 - y - z, y, z]
    values = [max(0.0, min(1.0, value)) for value in values]
    total = sum(values)
    return [value / total for value in values]


def interpolate(ids, basis):
    values = {}
    for index, vertex in enumerate(ids):
        for name, weight in rows[vertex].items():
            values[name] = values.get(name, 0.0) + basis[index] * weight
    return {name: weight for name, weight in values.items() if weight > 0}


for variant in (
    (0.10, 0.02, 1.08, 1.34),
    (0.14, 0.02, 1.08, 1.34),
    (0.14, 0.02, 1.05, 1.36),
    (0.12, 0.03, 1.10, 1.32),
):
    xmax, ymin, zmin, zmax = variant
    source_set = {
        i
        for i in select_vertices(body, xmax + 0.04, 0.0, zmin - 0.03, zmax + 0.03)
        if abs(sums[i] - 1.0) <= 1e-6
    }
    source_tris = [t for t in body_tris if all(v in source_set for v in t)]
    if not source_tris:
        continue
    source_points = [body.matrix_world @ v.co for v in body.data.vertices]
    tree = BVHTree.FromPolygons(source_points, source_tris, all_triangles=True)
    shirt_set = select_vertices(shirt, xmax, ymin, zmin, zmax)
    used, target_tris = patch_from_shirt(shirt_set)
    if not target_tris:
        continue
    target_points = [shirt.matrix_world @ shirt.data.vertices[i].co for i in used]
    distances = []
    ambiguous = 0
    max_inf = 0
    groups = set()
    for p in target_points:
        near, normal, tri, dist = tree.find_nearest(p)
        if tri is None:
            continue
        distances.append(dist)
        ids = source_tris[tri]
        weights = interpolate(ids, barycentric(near, *(source_points[v] for v in ids)))
        max_inf = max(max_inf, len(weights))
        groups.update(weights)
        eps = max(1e-7, dist * 1e-6)
        for alt, _, t2, d2 in tree.find_nearest_range(p, dist + eps):
            if t2 == tri or abs(d2 - dist) > eps:
                continue
            # report only if neighboring triangles are truly equidistant
            alt_ids = source_tris[t2]
            alt_w = interpolate(
                alt_ids, barycentric(alt, *(source_points[v] for v in alt_ids))
            )
            if any(abs(alt_w.get(n, 0.0) - weights.get(n, 0.0)) > 1e-6 for n in groups | set(alt_w)):
                ambiguous += 1
                break
    print(
        json.dumps(
            {
                "box": variant,
                "source_vertices": len(source_set),
                "source_triangles": len(source_tris),
                "shirt_vertices": len(used),
                "shirt_triangles": len(target_tris),
                "max_distance": max(distances, default=None),
                "mean_distance": (
                    math.fsum(distances) / len(distances) if distances else None
                ),
                "ambiguous_vertices": ambiguous,
                "max_influences": max_inf,
                "used_groups": sorted(groups),
            },
            ensure_ascii=False,
        )
    )
