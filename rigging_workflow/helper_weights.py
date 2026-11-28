# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded corrective allocation after primary volume diffusion.

Helpers are explicit local consumers of named primary groups, never extra
global Dirichlet sources. This layer does not alter the diffusion operator,
normalise unrelated groups, project across shells, or choose nearest bones.
"""
import math
import numpy as np


def allocate(points, weights, primary_names, specifications, *, donor_cap=0.35):
    points = np.asarray(points, dtype=np.float64)
    base = np.asarray(weights, dtype=np.float64)
    names = list(primary_names)
    if (points.ndim != 2 or points.shape[1] != 3 or not len(points)
            or not np.isfinite(points).all() or base.shape != (len(points), len(names))
            or not names or len(set(names)) != len(names)
            or not np.isfinite(base).all() or (base < 0).any()
            or np.max(abs(base.sum(axis=1) - 1.0)) > 1e-5):
        raise ValueError('修正骨分配需要完整、有限且非负的主体体积权重')
    if type(donor_cap) not in (int, float) or not math.isfinite(donor_cap) or not 0 < donor_cap < 1:
        raise ValueError('单个主骨的局部让渡上限必须在0与1之间')
    specs = list(specifications)
    helper_names = [s.get('name') for s in specs]
    if (any(not isinstance(n, str) or not n for n in helper_names)
            or len(set(helper_names)) != len(helper_names) or set(names) & set(helper_names)):
        raise ValueError('辅助骨必须逐项声明且不得与主骨重名')
    if not specs:
        return base.copy(), names, {'method': 'primary_volume', 'helpers': []}
    affinities, donor_indices = [], []
    for spec in specs:
        head, tail = np.asarray(spec.get('head'), dtype=float), np.asarray(spec.get('tail'), dtype=float)
        radius, strength = spec.get('radius'), spec.get('strength', .3)
        donors = spec.get('donors')
        if (head.shape != (3,) or tail.shape != (3,) or not np.isfinite([head, tail]).all()
                or type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0
                or type(strength) not in (int, float) or not math.isfinite(strength) or not 0 < strength <= donor_cap
                or not isinstance(donors, list) or not donors or len(set(donors)) != len(donors)
                or not set(donors) <= set(names)):
            raise ValueError('辅助骨需要有限胶囊支持域、明确主骨供体和有界让渡比例')
        axis = tail - head
        if float(axis @ axis) <= 1e-16:
            raise ValueError('辅助骨支持段不能退化')
        along = np.clip((points - head) @ axis / float(axis @ axis), 0, 1)
        distance = np.linalg.norm(points - (head + along[:, None] * axis), axis=1)
        t = np.clip(1.0 - distance / radius, 0.0, 1.0)
        # C1 smoothstep reaches exactly zero outside the declared capsule.
        affinity = strength * t * t * (3 - 2 * t)
        if 'support_plane' in spec:
            plane = spec['support_plane']
            origin, normal = np.asarray(plane.get('origin'), dtype=float), np.asarray(plane.get('normal'), dtype=float)
            transition = plane.get('transition')
            if (origin.shape != (3,) or normal.shape != (3,) or not np.isfinite([origin, normal]).all()
                    or np.linalg.norm(normal) < 1e-12 or type(transition) not in (int, float)
                    or not math.isfinite(transition) or transition <= 0):
                raise ValueError('左右辅助骨的支持平面需要有限原点、方向和正过渡宽度')
            side = np.clip((points - origin) @ (normal / np.linalg.norm(normal)) / transition, 0, 1)
            affinity *= side * side * (3 - 2 * side)
        affinities.append(affinity)
        donor_indices.append([names.index(n) for n in donors])
    output = np.zeros((len(points), len(names) + len(specs)), dtype=np.float64)
    output[:, :len(names)] = base
    for column in range(len(names)):
        participants = [i for i, indices in enumerate(donor_indices) if column in indices]
        if not participants:
            continue
        shares = np.column_stack([affinities[i] for i in participants])
        total = shares.sum(axis=1)
        limit = np.minimum(1.0, donor_cap / np.maximum(total, np.finfo(float).tiny))
        shares *= limit[:, None]
        donations = base[:, column, None] * shares
        output[:, column] -= donations.sum(axis=1)
        for offset, index in enumerate(participants):
            output[:, len(names) + index] += donations[:, offset]
    rows = []
    for index, spec in enumerate(specs):
        field = output[:, len(names) + index]
        count = int(np.count_nonzero(field > 0))
        if spec.get('coverage_required', True) and not count:
            raise ValueError('辅助骨在明确支持域内没有可用主骨权重：' + spec['name'])
        outside = affinities[index] == 0
        if np.any(field[outside] != 0):
            raise RuntimeError('辅助骨权重泄漏到支持域之外')
        rows.append({'bone': spec['name'], 'vertices': count, 'max': float(field.max(initial=0)),
                     'outside_support_nonzero': int(np.count_nonzero(field[outside])),
                     'donors': list(spec['donors']), 'radius': spec['radius']})
    if ((output < -1e-14).any() or not np.isfinite(output).all()
            or np.max(abs(output.sum(axis=1) - base.sum(axis=1))) > 1e-12):
        raise RuntimeError('辅助骨局部质量守恒校验失败')
    return output, names + helper_names, {'method': 'volume_primary_with_bounded_corrective_allocation',
        'donor_cap': donor_cap, 'helpers': rows,
        'max_mass_error': float(np.max(abs(output.sum(axis=1) - base.sum(axis=1))))}
