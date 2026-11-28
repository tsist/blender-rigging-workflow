# SPDX-License-Identifier: GPL-3.0-or-later
"""Conservative source-surface ownership for a shared calculation proxy."""
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from . import skinning


def source_triangles(source, sources, proxy_vertices, proxy_triangles):
    """Keep complete triangles nearest to this original source surface.

    Coincident source seams may share ownership. This geometric rule cannot
    infer semantic ownership of identical overlapping surfaces or restore an
    interior surface removed by a union remesh; unavailable coverage must fail.
    """
    if len(sources) == 1:
        allowed = np.ones(len(proxy_triangles), dtype=bool)
        allowed.setflags(write=False)
        return allowed, {'method': 'single_body_source', 'source': source.name,
                         'allowed_triangles': len(allowed), 'excluded_triangles': 0}
    span = float(np.ptp(proxy_vertices, axis=0).max())
    if not np.isfinite(span) or span <= 0:
        raise ValueError('组合代理表面范围退化')
    origin = proxy_vertices.min(axis=0)
    tolerance = 4 * float(np.finfo(np.float32).eps)
    if np.spacing(float(np.abs(proxy_vertices).max())) > span * tolerance:
        raise ValueError('世界坐标精度不足以稳定区分身体部件表面；请在工作副本中调整坐标范围')
    # Native BVH uses float32. A common unit-size frame keeps both its distance
    # arithmetic and seam tolerance independent of world translation and scale.
    normalized = (proxy_vertices - origin) / span
    if not np.isfinite(normalized).all():
        raise ValueError('组合代理不能转换到有限的来源检查坐标')
    queries = [Vector(v) for v in normalized]
    own = None
    others = np.full(len(proxy_vertices), np.inf, dtype=np.float64)
    for obj in sources:
        vertices, triangles = skinning.mesh_arrays(obj)
        if np.spacing(float(np.abs(vertices).max())) > span * tolerance:
            raise ValueError('身体部件世界坐标精度不足以稳定区分原表面：' + obj.name)
        normalized = (vertices - origin) / span
        if not np.isfinite(normalized).all():
            raise ValueError('身体部件不能转换到有限的来源检查坐标：' + obj.name)
        tree = BVHTree.FromPolygons([Vector(v) for v in normalized],
                                   triangles.tolist(), all_triangles=True)
        distances = np.empty(len(proxy_vertices), dtype=np.float64)
        for i, point in enumerate(queries):
            hit = tree.find_nearest(point)
            if hit[0] is None or not np.isfinite(hit[3]):
                raise ValueError('身体部件没有可核对来源的有效表面：' + obj.name)
            distances[i] = hit[3]
        if obj == source:
            own = distances
        else:
            np.minimum(others, distances, out=others)
    if own is None:
        raise ValueError('生产来源不在明确的身体计算域中')
    supported = own <= others + tolerance
    allowed = supported[proxy_triangles].all(axis=1)
    allowed.setflags(write=False)
    if not allowed.any():
        raise ValueError('此部件在组合代理外表面没有独立来源覆盖；请分域处理重叠部件，未启动求解')
    return allowed, {'method': 'nearest_original_source_surface_complete_triangles',
        'source': source.name, 'sources': [o.name for o in sources],
        'coordinate_space': 'common normalized proxy frame',
        'normalization_origin_world': origin.tolist(), 'normalization_span_world_units': span,
        'tie_tolerance_world_units': tolerance * span,
        'shared_seam_vertices': int(np.count_nonzero(abs(own - others) <= tolerance)),
        'owned_proxy_vertices': int(np.count_nonzero(supported)),
        'allowed_triangles': int(np.count_nonzero(allowed)),
        'excluded_triangles': int(np.count_nonzero(~allowed)),
        'semantic_overlap_inference': False,
        'uncovered_production_rows': 'reject_entire_candidate; no_other_part_fallback'}
