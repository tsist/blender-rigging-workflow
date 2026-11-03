"""Pure-Python helpers shared by the native adapter and independent oracle."""
from __future__ import annotations

import hashlib
import json
import math
import struct


def canonical_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_value(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def f32(value) -> float:
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def f32_hex(value) -> str:
    return struct.pack("<f", float(value)).hex()


def topology_sha256(vertex_count, edges, polygons) -> str:
    """Match public modeling.topology's serialization for interoperability."""
    value = {
        "vertices": int(vertex_count),
        "edges": [[int(index) for index in edge] for edge in edges],
        "faces": [[int(index) for index in face] for face in polygons],
    }
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def vertices_sha256(vertices) -> str:
    return sha256_value([[float(value) for value in vertex] for vertex in vertices])


def rest_sha256(vertex_count, vertices, topology_digest) -> str:
    return sha256_value(
        {
            "vertex_count": int(vertex_count),
            "vertices": [[float(value) for value in vertex] for vertex in vertices],
            "topology_sha256": topology_digest,
        }
    )


def sum_rows(rows, deform=False):
    return [
        math.fsum(
            float(item["weight"])
            for item in row
            if not deform or bool(item["deform"])
        )
        for row in rows
    ]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, value):
    return (a[0] * value, a[1] * value, a[2] * value)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length_squared(a):
    return _dot(a, a)


def closest_point_on_triangle(point, a, b, c):
    """Ericson point/triangle projection; returns point and nonnegative barycentrics."""
    point = tuple(float(value) for value in point)
    a = tuple(float(value) for value in a)
    b = tuple(float(value) for value in b)
    c = tuple(float(value) for value in c)
    ab = _sub(b, a)
    ac = _sub(c, a)
    ap = _sub(point, a)
    d1 = _dot(ab, ap)
    d2 = _dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return a, (1.0, 0.0, 0.0)
    bp = _sub(point, b)
    d3 = _dot(ab, bp)
    d4 = _dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return b, (0.0, 1.0, 0.0)
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        denominator = d1 - d3
        if abs(denominator) <= 1e-30:
            return a, (1.0, 0.0, 0.0)
        v = d1 / denominator
        return _add(a, _scale(ab, v)), (1.0 - v, v, 0.0)
    cp = _sub(point, c)
    d5 = _dot(ab, cp)
    d6 = _dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return c, (0.0, 0.0, 1.0)
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        denominator = d2 - d6
        if abs(denominator) <= 1e-30:
            return a, (1.0, 0.0, 0.0)
        w = d2 / denominator
        return _add(a, _scale(ac, w)), (1.0 - w, 0.0, w)
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        denominator = (d4 - d3) + (d5 - d6)
        if abs(denominator) <= 1e-30:
            return b, (0.0, 1.0, 0.0)
        w = (d4 - d3) / denominator
        return _add(b, _scale(_sub(c, b), w)), (0.0, 1.0 - w, w)
    denominator = va + vb + vc
    if abs(denominator) <= 1e-30:
        raise ValueError("Degenerate source triangle")
    inverse = 1.0 / denominator
    v = vb * inverse
    w = vc * inverse
    result = _add(a, _add(_scale(ab, v), _scale(ac, w)))
    return result, (1.0 - v - w, v, w)


def distance_squared(a, b):
    return _length_squared(_sub(a, b))


def raw_rows_for_vertex(obj, deform_bones):
    names = {group.index: group.name for group in obj.vertex_groups}
    rows = []
    for vertex in obj.data.vertices:
        row = []
        for membership in sorted(vertex.groups, key=lambda item: item.group):
            name = names[membership.group]
            row.append(
                {
                    "group_index": int(membership.group),
                    "group": name,
                    "bone": name if name in deform_bones else None,
                    "weight": float(membership.weight),
                    "deform": name in deform_bones,
                }
            )
        rows.append(row)
    return rows
