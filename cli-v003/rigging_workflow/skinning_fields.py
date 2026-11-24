# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared array composition and bounded native surface transfer; no scene writes."""
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from .volume.contract import VolumeError


def combine_partitions(vertex_count, bone_names, partitions):
    """Blend complete overlapping volume fields on explicit source vertex IDs.

    Each partition has source_ids, bone_names, weights and blend arrays. Added
    cap vertices use -1. Zero blend denotes a computation-only boundary row.
    """
    columns = {name: i for i, name in enumerate(bone_names)}
    combined = np.zeros((vertex_count, len(bone_names)), dtype=np.float64)
    total = np.zeros(vertex_count, dtype=np.float64)
    for row in partitions:
        ids = np.asarray(row['source_ids'], dtype=np.int64)
        weights = np.asarray(row['weights'], dtype=float)
        blend = np.asarray(row['blend'], dtype=float)
        if (weights.shape != (len(ids), len(row['bone_names'])) or blend.shape != (len(ids),)
                or not np.isfinite(weights).all() or not np.isfinite(blend).all()
                or (weights < 0).any() or (blend < 0).any() or (blend > 1).any()
                or (ids < -1).any() or (ids >= vertex_count).any()):
            raise ValueError('分域权重、原顶点映射或过渡系数无效')
        if np.max(abs(weights.sum(axis=1) - 1), initial=0) > 1e-5:
            raise ValueError('分域权重未通过和为一校验')
        valid = ids >= 0
        if len(np.unique(ids[valid])) != int(valid.sum()):
            raise ValueError('一个分域内出现重复生产顶点映射')
        try:
            indices = [columns[name] for name in row['bone_names']]
        except KeyError as exc:
            raise ValueError('分域包含未声明骨组') from exc
        if len(set(indices)) != len(indices):
            raise ValueError('分域骨名重复')
        combined[np.ix_(ids[valid], indices)] += weights[valid] * blend[valid, None]
        total[ids[valid]] += blend[valid]
    covered = total > 0
    combined[covered] /= total[covered, None]
    return {'weights': combined.astype(np.float32), 'covered': covered,
            'blend_sum': total, 'bone_names': list(bone_names)}


def surface_projection(points, vertices, triangles, *, max_distance, cancel_check=None, progress=None,
                       components=None):
    """Return bounded nearest-triangle geometry, without inventing weights."""
    points = np.asarray(points, dtype=float)
    vertices = np.asarray(vertices, dtype=float)
    triangles = np.asarray(triangles, dtype=np.int64)
    if (points.ndim != 2 or points.shape[1] != 3 or vertices.ndim != 2
            or vertices.shape[1] != 3
            or triangles.ndim != 2 or triangles.shape[1] != 3 or not len(triangles)
            or not np.isfinite(points).all() or not np.isfinite(vertices).all()
            or not np.isfinite(max_distance) or max_distance <= 0
            or triangles.min() < 0 or triangles.max() >= len(vertices)):
        raise ValueError('生产拓扑采样输入无效')
    tree = BVHTree.FromPolygons([Vector(v) for v in vertices], triangles.tolist(), all_triangles=True)
    if components is not None:
        point_components, vertex_components = (np.asarray(labels) for labels in components)
        if (point_components.shape != (len(points),) or vertex_components.shape != (len(vertices),)
                or point_components.dtype.kind not in 'iu' or vertex_components.dtype.kind not in 'iu'
                or (point_components < 0).any() or (vertex_components < 0).any()):
            raise ValueError('生产拓扑需要有效的RAW点与采样面壳体编号')
        triangle_components = vertex_components[triangles]
        if not (triangle_components == triangle_components[:, :1]).all():
            raise ValueError('采样三角跨越真实RAW壳体')
        triangle_components = triangle_components[:, 0]
        absent = np.setdiff1d(point_components, triangle_components)
        if len(absent):
            raise ValueError(f'生产壳体{absent.tolist()}没有完整覆盖的同壳采样三角')
        trees = {}
    vertex_ids = np.empty((len(points), 3), dtype=np.int64)
    barycentrics = np.empty((len(points), 3), dtype=np.float64)
    distances = []
    shell_reassigned = 0
    for index, point in enumerate(points):
        if index % 128 == 0:
            if cancel_check is not None and cancel_check():
                raise VolumeError('CANCELLED', 'Surface transfer cancelled; no candidate accepted')
            if progress is not None:
                progress('surface_transfer', done=index, total=len(points))
        query = Vector(point)
        location, _, triangle, distance = tree.find_nearest(query, float(max_distance))
        shell = triangles
        if location is not None and components is not None and triangle_components[triangle] != point_components[index]:
            # Preserve the old exact triangle and barycentrics for every
            # already-correct query, including equal-distance BVH ties.
            component = point_components[index]
            if component not in trees:
                allowed = triangles[triangle_components == component]
                used, inverse = np.unique(allowed, return_inverse=True)
                local = inverse.reshape(allowed.shape)
                bvh = BVHTree.FromPolygons([Vector(v) for v in vertices[used]], local.tolist(), all_triangles=True)
                trees[component] = (bvh, allowed)
            bvh, shell = trees[component]
            location, _, triangle, distance = bvh.find_nearest(query, float(max_distance))
            shell_reassigned += 1
        if location is None:
            owner = '' if components is None else f'的RAW壳体{point_components[index]}'
            raise ValueError(f'生产采样点{index}{owner}在{max_distance:g}距离内没有合法三角（输入坐标单位）')
        ids = shell[triangle]
        a, b, c = vertices[ids]
        u, v, w = b - a, c - a, np.asarray(location) - a
        uu, uv, vv = u @ u, u @ v, v @ v
        denominator = uu * vv - uv * uv
        if denominator <= 1e-24:
            raise ValueError('代理表面包含不可采样的退化三角')
        s = (vv * (w @ u) - uv * (w @ v)) / denominator
        t = (uu * (w @ v) - uv * (w @ u)) / denominator
        barycentric = np.clip([1 - s - t, s, t], 0, 1)
        barycentric /= barycentric.sum()
        vertex_ids[index] = ids
        barycentrics[index] = barycentric
        distances.append(float(distance))
    if progress is not None:
        progress('surface_transfer', done=len(points), total=len(points))
    report = {'vertices': len(points), 'max_distance': max(distances, default=0),
              'distance_bound': max_distance, 'method': 'bounded_nearest_surface_projection'}
    if components is not None:
        report.update(method='bounded_same_source_shell_projection', shell_reassigned=shell_reassigned)
    return vertex_ids, barycentrics, report


def transfer_surface_field(points, vertices, triangles, weights, *, max_distance, cancel_check=None, progress=None,
                           projection_recorder=None, components=None):
    """Interpolate an existing volume result using bounded surface geometry."""
    weights = np.asarray(weights, dtype=float)
    if weights.ndim != 2 or len(weights) != len(vertices) or not np.isfinite(weights).all():
        raise ValueError('生产拓扑权重采样输入无效')
    ids, barycentric, report = surface_projection(points, vertices, triangles,
        max_distance=max_distance, cancel_check=cancel_check, progress=progress, components=components)
    output = np.einsum('ij,ijk->ik', barycentric, weights[ids])
    if (output < 0).any() or np.max(abs(output.sum(axis=1) - 1), initial=0) > 1e-5:
        raise ValueError('代理转移未保持有效权重场')
    if projection_recorder is not None:
        if not callable(projection_recorder):raise ValueError('投影记录器必须为明确可信调用')
        projection_recorder(ids,barycentric,report)
    return output.astype(np.float32), {**report, 'method': ('barycentric_volume_field_transfer' if components is None
                                                             else 'barycentric_same_source_shell_transfer')}
