# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve the local surface and read-only context for chosen deform bones."""

from collections import defaultdict, deque
import math

import numpy as np

from . import local_skinning_job, skinning, skinning_policy as policy


def _distance(points, head, tail):
    axis = tail - head
    projection = np.clip((points - head) @ axis / np.dot(axis, axis), 0.0, 1.0)
    return np.linalg.norm(points - (head + projection[:, None] * axis), axis=1)


def _connected_vertices(mesh, window, anchors):
    pairs = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    mesh.edges.foreach_get('vertices', pairs)
    pairs = pairs.reshape(-1, 2)
    adjacency = defaultdict(list)
    for a, b in pairs[window[pairs].all(axis=1)].tolist():
        adjacency[a].append(b)
        adjacency[b].append(a)
    selected = anchors & window
    queue = deque(np.flatnonzero(selected).tolist())
    while queue:
        current = queue.popleft()
        for other in adjacency[current]:
            if not selected[other]:
                selected[other] = True
                queue.append(other)
    return np.flatnonzero(selected)


def _touches_box(head, tail, low, high):
    """Clip the bone segment against the local proxy bounds."""
    first, last = 0.0, 1.0
    for coordinate, direction, lower, upper in zip(head, tail - head, low, high):
        if abs(direction) < 1e-14:
            if coordinate < lower or coordinate > upper:
                return False
            continue
        a, b = sorted(((lower - coordinate) / direction, (upper - coordinate) / direction))
        first, last = max(first, a), min(last, b)
    return first <= last


def resolve(target, rig, proxy, selected_bones, *, vertex_indices=None, alpha=None,
            radius_scale=1.0, context_bones=None):
    """Return an explicit target, RAW vertex list and bone set for review."""
    policy._bound(target, rig)
    policy._object(proxy, 'MESH')
    policy._require(type(radius_scale) in (float, int) and math.isfinite(radius_scale)
                    and 0.1 <= radius_scale <= 4.0, '范围比例须在0.1与4之间')
    scale = policy._rig(rig, selected_bones)
    deform_groups = {group.index: group.name for group in target.vertex_groups
                     if (bone := rig.data.bones.get(group.name)) and bone.use_deform}
    group_indices = {name: index for index, name in deform_groups.items()}
    policy._require(all(name in group_indices for name in selected_bones), '目标网格必须已有全部所选骨的顶点组')
    unlocked = [name for name in selected_bones if not target.vertex_groups[name].lock_weight]
    policy._require(unlocked, '所选骨骼的顶点组均已锁定')
    count = len(target.data.vertices)
    radii = {}
    if vertex_indices is None:
        points, _ = skinning.mesh_arrays(target)
        points *= scale
        segments = skinning.bone_segments(rig, unlocked)
        best = np.zeros(count)
        columns = {group_indices[name]: np.zeros(count) for name in unlocked}
        for point in target.data.vertices:
            for group in point.groups:
                if group.group in deform_groups:
                    best[point.index] = max(best[point.index], group.weight)
                    if group.group in columns:
                        columns[group.group][point.index] = group.weight
        window = np.zeros(count, dtype=bool)
        anchors = window.copy()
        for segment in segments:
            name = segment['name']
            strength = columns[group_indices[name]]
            reliable = (strength >= 0.1) & (strength >= best * 0.8)
            policy._require(reliable.any(), f'{name} 没有可靠的原有权重范围，请明确选点')
            head, tail = (np.asarray(segment[key], dtype=float) * scale for key in ('head', 'tail'))
            distance = _distance(points, head, tail)
            reference = float(np.quantile(distance[reliable], 0.95))
            radius = max(reference * 1.15, np.linalg.norm(tail - head) * 0.1) * radius_scale
            nearby = distance <= radius
            window |= nearby
            anchors |= reliable & nearby
            radii[name] = float(radius)
        ids = _connected_vertices(target.data, window, anchors)
        policy._require(len(ids) > 0, '没有可可靠连接到所选骨的附近顶点，请扩大范围或明确指定点')
    else:
        ids = policy._ids(vertex_indices, count)

    blend = np.ones(len(ids)).tolist() if alpha is None else np.asarray(alpha, dtype=float).tolist()
    policy._require(isinstance(blend, list) and len(blend) == len(ids)
                    and all(type(value) is float and math.isfinite(value) and 0 <= value <= 1 for value in blend)
                    and any(blend), '每个选点需要有效的0至1过渡系数')
    if context_bones is None:
        context_bones = list(selected_bones)
        # Only existing, meaningful influences on this local surface can be
        # considered as context. The solver still requires an actual seed.
        scores = defaultdict(float)
        for index, value in zip(ids, blend):
            if value <= 0.0:
                continue
            for group in target.data.vertices[int(index)].groups:
                name = deform_groups.get(group.group)
                if name and name not in selected_bones and group.weight >= 0.03:
                    scores[name] += group.weight
        if scores:
            proxy_points, _ = skinning.mesh_arrays(proxy)
            proxy_points *= scale
            lo, hi = proxy_points.min(axis=0), proxy_points.max(axis=0)
            options = sorted(scores, key=lambda name: (-scores[name], name))
            for bone in skinning.bone_segments(rig, options):
                head, tail = (np.asarray(bone[key], dtype=float) * scale for key in ('head', 'tail'))
                if _touches_box(head, tail, lo, hi):
                    context_bones.append(bone['name'])
    policy._rig(rig, context_bones)
    policy._require(len(context_bones) >= 2 and set(unlocked) <= set(context_bones),
                    '局部求权需要所选骨和足够的只读解算骨，请指定闭合代理或明确补充只读骨')
    return {'target': target.name, 'rig': rig.name, 'proxy': proxy.name, 'selected_bones': list(selected_bones),
            'context_bones': list(context_bones), 'vertex_indices': ids.tolist(), 'alpha': blend,
            'mode': 'AUTO_CONNECTED' if vertex_indices is None else 'EXPLICIT_RAW', 'radii_meters': radii}


def check(target, rig, proxy, selected_bones, settings, *, candidate_name=None, **selection):
    scope = resolve(target, rig, proxy, selected_bones, **selection)
    result = local_skinning_job.check(target, rig, proxy, scope['selected_bones'], scope['vertex_indices'],
        scope['alpha'], settings, write_mode='selected_direct', context_bone_names=scope['context_bones'])
    result['selection'] = scope
    if candidate_name is not None:
        result['selected_apply_scope'] = local_skinning_job.selected_apply_scope(result['selected_write_scope'],
                                                                                name=candidate_name)
    return result


def start(target, rig, proxy, selected_bones, settings, *, jobs_directory, **selection):
    scope = resolve(target, rig, proxy, selected_bones, **selection)
    return local_skinning_job.start(target, rig, proxy, scope['selected_bones'], scope['vertex_indices'],
        scope['alpha'], settings, write_mode='selected_direct', context_bone_names=scope['context_bones'],
        jobs_directory=jobs_directory)
