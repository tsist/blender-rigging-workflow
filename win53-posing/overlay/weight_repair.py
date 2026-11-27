# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen regional recipes, one atomic independent candidate per target.

No production writes, whole-body rebinding or disk-restored submission rights.
Numerical operators live in weight_repair_math; GUI and AI share this module.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

import bpy
from bpy.app.handlers import persistent
import numpy as np

from . import skinning_policy as policy, corrective_authoring as sig
from . import skeleton_gate, skinning, skinning_job, rig_identity
from . import weight_repair_math as numerical

SCHEMA = 'rigging.weight-repair.recipe.v1'
_PLANS = set()
_EPOCH = 0
_OBSERVER_OWNER = object()


def _require(test, message):
    if not test:
        raise ValueError(message)


def _json(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _edges(target):
    rows = np.empty((len(target.data.edges), 2), dtype=np.int64)
    target.data.edges.foreach_get('vertices', rows.ravel())
    return rows


def _ids(value, count):
    # Validate the original scalars before NumPy can coerce mixed bool/int lists.
    return numerical._strict_id_array(value, count, 'RAW点编号')


def mesh_fingerprint(target):
    """Persistent input binding: no stale RAW ids on a different input."""
    _require(target is not None and target.type == 'MESH', '需要明确网格对象')
    mesh = target.data
    points = np.empty((len(mesh.vertices), 3), dtype=np.float32)
    mesh.vertices.foreach_get('co', points.ravel())
    loops = np.empty(len(mesh.loops), dtype=np.int64)
    starts = np.empty(len(mesh.polygons), dtype=np.int64)
    totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.loops.foreach_get('vertex_index', loops)
    mesh.polygons.foreach_get('loop_start', starts)
    mesh.polygons.foreach_get('loop_total', totals)
    armatures = [m.object for m in target.modifiers if m.type == 'ARMATURE' and m.object]
    rig_binding = None
    if len(armatures) == 1:
        rig = armatures[0]
        rig_binding = {'object': rig.name, 'data': rig.data.name,
            'matrix_world': [list(row) for row in rig.matrix_world],
            'rest': _hash([[b.name, b.parent.name if b.parent else None,
                [list(row) for row in b.matrix_local], list(b.head_local), list(b.tail_local), b.length,
                {k: v for k, v in sig._rna(b).items() if k.startswith('bbone_') or k in
                    {'use_deform', 'use_connect', 'use_inherit_rotation', 'inherit_scale',
                     'use_local_location', 'use_relative_parent'}},
                [getattr(getattr(b, field, None), 'name', None) for field in
                 ('bbone_custom_handle_start', 'bbone_custom_handle_end')]] for b in rig.data.bones])}
    return {'object': target.name, 'mesh': mesh.name, 'vertices': len(points),
            'geometry': _hash([sig._array_hash(a) for a in (points, _edges(target), loops, starts, totals)]),
            'matrix_world': [list(row) for row in target.matrix_world],
            'groups': [g.name for g in target.vertex_groups], 'rig': rig_binding,
            'unit_scale': float(bpy.context.scene.unit_settings.scale_length)}


def capture_region(target, *, indices=None, mask_group=None, rings=0,
                   barriers=(), allowed_ids=None):
    _require(target and target.type == 'MESH', '请选择网格捕获明确区域')
    _require(indices is None or mask_group is None, '点索引与遮罩组只能选择一种来源')
    _require(type(rings) is int and 0 <= rings <= 100, '扩圈需要0–100整数')
    if target.mode == 'EDIT':
        target.update_from_editmode()
    if mask_group is not None:
        group = target.vertex_groups.get(mask_group)
        _require(group is not None, '遮罩组不存在')
        values = {v.index: next((m.weight for m in v.groups if m.group == group.index), 0.)
                  for v in target.data.vertices}
        indices = [i for i, value in values.items() if value > 0]
    elif indices is None:
        indices = [v.index for v in target.data.vertices if v.select and not v.hide]
    ids = _ids(indices, len(target.data.vertices)).tolist()
    region = numerical.grow_region(len(target.data.vertices), _edges(target), ids,
        rings=rings, barriers=barriers, allowed_ids=allowed_ids)
    if mask_group is not None and not rings:
        region['alpha'] = [values[i] for i in region['ids']]
    region['binding'] = mesh_fingerprint(target)
    region['capture'] = {'source': 'MASK' if mask_group is not None else 'EXPLICIT',
                         'mask_group': mask_group, 'rings': rings}
    return _json(region)


def bind_recipe(recipe, targets):
    """Explicit same-input remap, never retarget old indices to changed geometry."""
    result = _json(recipe)
    _require(result.get('schema') == SCHEMA, '配方schema不匹配')
    for region in result.get('regions', {}).values():
        target = targets.get(region.get('target'))
        _require(target is not None, '区域缺少显式目标映射')
        current, previous = mesh_fingerprint(target), region.get('binding')
        _require(previous is not None and all(previous.get(k) == current[k]
            for k in ('geometry', 'matrix_world', 'groups', 'vertices', 'rig', 'unit_scale')),
            'RAW坐标/拓扑/变换/组不同：请重新捕获区域，不能套用旧点索引')
        region['binding'] = current
    return result


def save_recipe(recipe, path):
    value = _json(recipe)
    _require(value.get('schema') == SCHEMA, '配方schema不匹配')
    destination = Path(path)
    _require(destination.is_absolute(), '配方保存需要绝对路径')
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return str(destination)


def load_recipe(path):
    destination = Path(path)
    _require(destination.is_absolute() and destination.stat().st_size <= 16 * 1024 * 1024,
             '需要绝对路径且不超过16MiB的配方JSON')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, '配方JSON含重复键')
            result[key] = value
        return result
    value = json.loads(destination.read_text(encoding='utf-8-sig'), object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('配方含非有限数值')))
    _require(value.get('schema') == SCHEMA, '配方schema不匹配')
    return _json(value)


def _curve(curve):
    return {'rna': sig._rna(curve),
            'keys': [sig._rna(point) for point in curve.keyframe_points],
            'samples': [list(point.co) for point in curve.sampled_points],
            'group': curve.group.name if curve.group else None,
            'modifiers': [{'rna': sig._rna(modifier),
                'control_points': [sig._rna(point) for point in modifier.control_points]
                    if modifier.type == 'ENVELOPE' else None} for modifier in curve.modifiers]}


def _animation(owner):
    """Full content, not merely Action names, protects pending live plans."""
    ad = getattr(owner, 'animation_data', None)
    if ad is None:
        return None
    actions = set()
    if ad.action:
        actions.add(ad.action)
    def strips(collection):
        rows = []
        for strip in collection:
            if strip.action:
                actions.add(strip.action)
            rows.append({'rna': sig._rna(strip), 'type': strip.type,
                'curves': [_curve(f) for f in strip.fcurves],
                'children': strips(strip.strips) if strip.type == 'META' else []})
        return rows
    tracks = [{'rna': sig._rna(track), 'strips': strips(track.strips)} for track in ad.nla_tracks]
    contents = []
    for action in sorted(actions, key=lambda a: a.session_uid):
        layers = []
        for layer in action.layers:
            strip_rows = []
            for strip in layer.strips:
                _require(strip.type == 'KEYFRAME', '不支持该Action strip类型的完整内容核验')
                bags = []
                for slot in action.slots:
                    bag = strip.channelbag(slot)
                    if bag is not None:
                        bags.append({'slot': slot.identifier, 'rna': sig._rna(bag),
                                     'curves': [_curve(f) for f in bag.fcurves],
                                     'groups': [sig._rna(group) for group in bag.groups]})
                strip_rows.append({'rna': sig._rna(strip), 'type': strip.type, 'bags': bags})
            layers.append({'rna': sig._rna(layer), 'strips': strip_rows})
        contents.append({'uid': action.session_uid, 'rna': sig._rna(action),
            'properties': sig._plain(dict(action.items())), 'layers': layers,
            'slots': [sig._rna(slot) for slot in action.slots]})
    return {'rna': sig._rna(ad), 'tracks': tracks, 'actions': contents,
            'drivers': sig._drivers(owner), 'driver_curves': [_curve(f) for f in ad.drivers]}


def _consumers(target, names):
    """Reject known non-skin consumers; do not claim arbitrary scripts closed."""
    conflicts = []
    keys = target.data.shape_keys
    if keys:
        for key in keys.key_blocks:
            if key.vertex_group in names:
                conflicts.append(f'Key:{key.name}:{key.vertex_group}')
    for obj in bpy.data.objects:
        if obj.parent == target and obj.parent_type in {'VERTEX', 'VERTEX_3'}:
            conflicts.append(f'顶点父级:{obj.name}')
        for modifier in obj.modifiers:
            if obj != target:
                if modifier.type == 'DATA_TRANSFER' and modifier.object == target and modifier.use_vert_data and \
                        'VGROUP_WEIGHTS' in modifier.data_types_verts:
                    conflicts.append(f'读取源骨组的DataTransfer:{obj.name}/{modifier.name}')
                # Most modifier.vertex_group fields belong to the modifier
                # owner, not its referenced target. Do not reject name matches.
                continue
            for prop in modifier.bl_rna.properties:
                if prop.type == 'STRING' and ('vertex_group' in prop.identifier or 'vgroup' in prop.identifier):
                    value = getattr(modifier, prop.identifier)
                    if value in names:
                        conflicts.append(f'Modifier:{obj.name}/{modifier.name}/{prop.identifier}:{value}')
        constraints = list(obj.constraints)
        if obj.type == 'ARMATURE':
            constraints += [c for bone in obj.pose.bones for c in bone.constraints]
        for constraint in constraints:
            if getattr(constraint, 'target', None) == target and getattr(constraint, 'subtarget', '') in names:
                conflicts.append(f'Constraint:{obj.name}/{constraint.name}')
            for entry in getattr(constraint, 'targets', ()):
                if getattr(entry, 'target', None) == target and getattr(entry, 'subtarget', '') in names:
                    conflicts.append(f'ConstraintTarget:{obj.name}/{constraint.name}')
    _require(not conflicts, '写组存在非蒙皮consumer冲突：' + '; '.join(conflicts))
    return {'scope': 'stored data and enumerated native consumers; arbitrary external readers are not closed',
            'conflicts': conflicts}


def _snapshot(target, rig):
    owners = [rig, rig.data, target, target.data]
    if target.data.shape_keys:
        owners.append(target.data.shape_keys)
    return {'policy': policy._state(target, rig),
            'animation': [_animation(owner) for owner in owners],
            'custom': [sig._plain(dict(owner.items())) for owner in owners]}


def _dense(target, names):
    columns = {target.vertex_groups[name].index: j for j, name in enumerate(names)}
    field = np.zeros((len(target.data.vertices), len(names)), dtype=np.float64)
    for vertex in target.data.vertices:
        for member in vertex.groups:
            if member.group in columns:
                field[vertex.index, columns[member.group]] = member.weight
    return field


def _mapped_field(values, donor_names, mapping, groups, *, allowed_targets):
    _require(isinstance(mapping, dict) and mapping and len(set(mapping.values())) == len(mapping),
             '需要目标唯一的显式完整骨组映射')
    _require(set(mapping) <= set(donor_names), '映射含不存在的供体骨列')
    _require(set(mapping.values()) <= set(allowed_targets), '映射目的列不是当前实际可用变形组')
    positive = {name for j, name in enumerate(donor_names) if np.any(values[:, j] > 0)}
    _require(positive <= set(mapping), '供体正权列未全部映射，不能静默丢质量')
    _require(set(groups) <= set(mapping.values()), '可写目标组缺少显式供体映射')
    result = np.zeros((len(values), len(groups)), dtype=float)
    for source, dest in mapping.items():
        if dest in groups:
            result[:, groups.index(dest)] = values[:, donor_names.index(source)]
    return result


def build_mirror_pairs(target, left_ids, right_ids, *, axis=0, plane_meters=0.0,
                       tolerance_meters, center_ids=()):
    _require(type(axis) is int and axis in (0, 1, 2), '对称轴需要0/1/2')
    points, _ = skinning.mesh_arrays(target)
    scale = float(bpy.context.scene.unit_settings.scale_length)
    _require(math.isfinite(scale) and scale > 0, '需要有效单位')
    result = numerical.mirror_pairs(points * scale, left_ids, right_ids,
        axis=axis, plane=plane_meters, tolerance=tolerance_meters)
    centers = _ids(center_ids, len(points)).tolist() if len(center_ids) else []
    _require(not set(centers) & (set(left_ids) | set(right_ids)), '中心域必须与两侧域独立')
    _require(all(abs(points[i, axis] * scale - plane_meters) <= tolerance_meters for i in centers),
             '中心域包含未处于指定对称平面容差内的点')
    result['pairs'].extend([[i, i] for i in centers])
    result.update({'binding': mesh_fingerprint(target), 'left_ids': list(left_ids),
        'right_ids': list(right_ids), 'axis': axis, 'plane_meters': plane_meters,
        'tolerance_meters': tolerance_meters, 'unit_scale': scale,
        'center_ids': centers, 'confirmed': False})
    return _json(result)


def confirm_pairing(target, pairing):
    _require(pairing.get('binding') == mesh_fingerprint(target), '配对输入已经改变')
    actual = build_mirror_pairs(target, pairing['left_ids'], pairing['right_ids'],
        axis=pairing['axis'], plane_meters=pairing['plane_meters'],
        tolerance_meters=pairing['tolerance_meters'], center_ids=pairing.get('center_ids', ()))
    _require(actual['pairs'] == pairing.get('pairs') and not actual.get('unmatched')
             and not actual.get('ambiguous'), '配对不完整/有歧义或已过时')
    actual['confirmed'] = True
    actual['confirmation'] = _hash({k: v for k, v in actual.items() if k != 'confirmation'})
    return actual


def _mirror_operation(target, weights, names, region, operation):
    pairing = operation.get('pairing', {})
    _require(pairing.get('confirmed') is True and pairing.get('confirmation') ==
        _hash({k: v for k, v in pairing.items() if k != 'confirmation'}), '镜像配对尚未明确确认')
    verified = confirm_pairing(target, pairing)
    _require(verified == pairing, '镜像配对或单位已经改变')
    mapping = operation.get('group_mapping')
    _require(isinstance(mapping, dict) and mapping and len(set(mapping.values())) == len(mapping),
             '镜像需要显式且目标唯一的骨组映射')
    _require(set(mapping) <= set(names) and set(mapping.values()) == set(operation['groups']),
             '镜像映射必须完整覆盖可写目标组')
    pairs = pairing['pairs']
    mode = operation.get('mode', 'COPY')
    _require(mode in {'COPY', 'AVERAGE'}, '镜像模式只能COPY或AVERAGE')
    pair_map = {int(right): int(left) for left, right in pairs}
    if mode == 'AVERAGE':
        pair_map.update({int(left): int(right) for left, right in pairs})
        _require(set(mapping) == set(mapping.values()) and
            all(mapping.get(mapping[name]) == name for name in mapping), '对称平均的组映射必须为双向对合')
    ids = region['ids']
    _require(all(i in pair_map for i in ids), '镜像写域含未配对点或方向错误')
    if mode == 'COPY' and any(pair_map[i] == i for i in ids):
        _require(all(source == dest for source, dest in mapping.items()),
                 '中心自配COPY只允许自映射骨组；互换组请明确使用AVERAGE')
    source_rows = weights[[pair_map[i] for i in ids]]
    positive = {name for j, name in enumerate(names) if np.any(source_rows[:, j] > 0)}
    _require(positive <= set(mapping), '镜像供侧正权骨列未完整映射，不能先截列隐藏漏项')
    relevant = sorted(set(mapping))
    source_field = source_rows[:, [names.index(n) for n in relevant]]
    field = _mapped_field(source_field, relevant, mapping, operation['groups'], allowed_targets=names)
    if mode == 'AVERAGE':
        target_rows = weights[np.asarray(ids)][:, [names.index(n) for n in operation['groups']]]
        source_mass, target_mass = field.sum(axis=1), target_rows.sum(axis=1)
        _require(np.all(source_mass > 0) and np.all(target_mass > 0), '对称平均需要两侧有明确正质量')
        field = .5 * (field / source_mass[:, None] + target_rows / target_mass[:, None])
    return dict(operation, kind='FIELD', field=field.tolist())


def _compile_stage(current, names, edges, regions, stage):
    """Each operation gets only its E columns and its compact read graph.

    All operations in a Stage read current. Their positive-alpha RAW rows must
    be disjoint, including when their E columns differ. No RNA reads here.
    """
    started = time.perf_counter()
    output = current.copy()
    occupied, receipts = set(), []
    edge_set = {tuple(sorted((int(a), int(b)))) for a, b in edges}
    for operation in stage['operations']:
        region = regions[operation['region']]
        ids = _ids(region['ids'], len(current))
        read_ids = _ids(region['read_ids'], len(current))
        _require(set(ids.tolist()) <= set(read_ids.tolist()), 'read域必须包含全部write域')
        alpha = np.asarray(region['alpha'], dtype=float)
        active_ids = set(ids[alpha > 0].tolist())
        _require(not occupied & active_ids, '同Stage区域写域重叠；请拆成具名有序Stage')
        occupied.update(active_ids)
        local_map = np.full(len(current), -1, dtype=np.int64)
        local_map[read_ids] = np.arange(len(read_ids))
        allowed_edges = (local_map[edges] >= 0).all(axis=1)
        local_edges = local_map[edges[allowed_edges]]
        barriers = []
        for edge in region.get('barriers', []):
            _require(isinstance(edge, (list, tuple)) and len(edge) == 2 and
                all(type(i) is int for i in edge) and tuple(sorted(edge)) in edge_set,
                'barrier必须为RAW真实边')
            if local_map[edge[0]] >= 0 and local_map[edge[1]] >= 0:
                barriers.append([int(local_map[edge[0]]), int(local_map[edge[1]])])
        local_region = {'ids': local_map[ids].tolist(), 'alpha': alpha.tolist(),
            'read_ids': list(range(len(read_ids))), 'barriers': barriers}
        groups = operation['groups']
        columns = [names.index(name) for name in groups]
        frozen = current[np.ix_(read_ids, columns)]
        values, receipt = numerical.compile_weights(frozen, groups, local_edges,
            {operation['region']: local_region}, [{'label': stage['label'], 'operations': [operation]}])
        authorization = np.zeros(frozen.shape, dtype=bool)
        authorization[local_map[ids[alpha > 0]]] = True
        _require(values.shape == frozen.shape and not ((values.astype(float) != frozen) & ~authorization).any(),
                 '局部数值提案改变未授权read域，拒绝候选')
        output[np.ix_(read_ids, columns)] = values
        receipt['local_to_raw'] = read_ids.tolist()
        receipt['raw_write_ids'] = ids[alpha > 0].tolist()
        receipt['explicit_E'] = groups
        receipts.append(receipt)
    return output.astype(np.float32), {'label': stage['label'], 'operations': receipts,
        'write_vertices': len(occupied), 'seconds': time.perf_counter() - started,
        'read_semantics': 'all operations read the same frozen stage input'}


class PreparedRepair:
    def __init__(self, targets, rig, recipe):
        self._gate_token = skeleton_gate.require_pass(rig, operation='weight_repair.PreparedRepair')
        started = time.perf_counter()
        _require(isinstance(targets, dict) and targets and all(isinstance(k, str) and k for k in targets),
                 '需要显式目标逻辑名→Object映射')
        _require(len(set(targets.values())) == len(targets), '同一个源不能用多个逻辑名重复绑定')
        self.targets, self.rig = dict(targets), rig
        self.recipe = _json(recipe)
        _require(self.recipe.get('schema') == SCHEMA and self.recipe.get('regions') and
            isinstance(self.recipe.get('stages'), list) and self.recipe['stages'], '配方需要区域与有序Stage')
        self._scene = bpy.context.scene
        self._epoch = _EPOCH
        self._done = False
        self._previews = []
        self._preview_meshes = []
        self._sources, self._donors, self._outputs, self._names, self._original = {}, {}, {}, {}, {}
        self._authorized = {}
        self._meshes = {key: obj.data for key, obj in targets.items()}
        self._rig_data = rig.data
        self._regions = self.recipe['regions']
        self.report = {'schema': SCHEMA, 'source_unchanged': True, 'targets': {}, 'timing': {},
                       'status': 'prepared', 'recipe_sha256': _hash(self.recipe)}
        all_deforms = rig_identity.read(rig)['deforms']
        for key, target in self.targets.items():
            policy._bound(target, rig)
            _require(target.data.users == 1 and sum(o.type == 'MESH' and o.data == target.data
                for o in bpy.data.objects) == 1, '共享Mesh暂不支持；请显式建立独立维护输入：' + key)
            _require(not (target.animation_data and target.animation_data.nla_tracks) and
                not (target.data.animation_data and target.data.animation_data.nla_tracks), '网格对象/数据NLA暂不支持')
            names = [g.name for g in target.vertex_groups if g.name in all_deforms]
            policy._rig(rig, names)
            actual = {g.name for g in target.vertex_groups if (b := rig.data.bones.get(g.name)) and b.use_deform}
            _require(actual <= set(all_deforms), '存在未声明实际变形组，不能漏列')
            weights = _dense(target, names)
            _require(np.isfinite(weights).all() and (weights >= 0).all() and (weights <= 1).all()
                and (abs(weights.sum(axis=1, dtype=np.float64) - 1) <= 1e-5).all(),
                '输入实际deform权重必须有限、0..1且总和在1±1e-5内')
            for vertex in target.data.vertices:
                _require(all(math.isfinite(m.weight) and 0 <= m.weight <= 1 for m in vertex.groups),
                         '输入包含无效非变形组权重')
            self._names[key], self._original[key] = names, weights
            self._sources[key] = _snapshot(target, rig)
            frozen_binding, frozen_edges = mesh_fingerprint(target), _edges(target)
            local_regions = {}
            for name, region in self._regions.items():
                _require(region.get('target') in self.targets, '区域目标未明确映射')
                if region['target'] == key:
                    _require(region.get('binding') == frozen_binding, '区域输入已变化，请重新捕获或显式同输入重映射')
                    local_regions[name] = region
            requested_groups = {name for stage in self.recipe['stages'] for op in stage.get('operations', [])
                if op.get('region') in local_regions for name in op.get('groups', [])}
            _consumers(target, requested_groups)
            stages, current = [], weights.copy()
            authorized = np.zeros(weights.shape, dtype=bool)
            step_reports = []
            for stage in self.recipe['stages']:
                operations = []
                for operation in stage.get('operations', []):
                    _require(operation.get('region') in self._regions, '操作引用未知区域')
                    region = self._regions[operation['region']]
                    if region['target'] != key:
                        continue
                    groups = operation.get('groups', [])
                    _require(isinstance(groups, list) and groups and len(set(groups)) == len(groups)
                        and set(groups) <= set(names), '写组必须为明确已有最终变形组')
                    _require(not any(target.vertex_groups[n].lock_weight for n in groups), '请求包含锁组，拒绝整个操作')
                    region_ids = _ids(region['ids'], len(weights))
                    region_alpha = np.asarray(region['alpha'], dtype=float)
                    _require(region_alpha.shape == region_ids.shape and np.isfinite(region_alpha).all()
                        and (region_alpha >= 0).all() and (region_alpha <= 1).all(), '区域alpha形状/范围无效')
                    for name in groups:
                        authorized[region_ids[region_alpha > 0], names.index(name)] = True
                    op = copy.deepcopy(operation)
                    if op['kind'] == 'SURFACE_TRANSFER':
                        donor = bpy.data.objects.get(op.get('donor', ''))
                        _require(donor is not None and donor not in self.targets.values(),
                                 '供体必须明确且不能为同Plan任一target')
                        self._donors[donor] = _snapshot(donor, rig)
                        donor_ids = op.get('donor_ids')
                        if donor_ids is not None:
                            donor_ids = _ids(donor_ids, len(donor.data.vertices)).tolist()
                        points, triangles, field, donor_names, scale = policy._field(
                            donor, rig, None, donor_ids)
                        distance = op.get('max_distance_meters')
                        _require(type(distance) in (int, float) and math.isfinite(distance) and distance > 0,
                                 '表面转移需要明确有限正米制距离')
                        target_points, _ = skinning.mesh_arrays(target)
                        field, receipt = skinning.transfer_surface_field(
                            target_points[region['ids']] * scale, points * scale,
                            triangles, field, max_distance=distance)
                        op.update(kind='FIELD', field=_mapped_field(field, donor_names,
                            op.get('bone_mapping'), groups, allowed_targets=names).tolist())
                        op['source_pool'] = [n for n, dest in op['bone_mapping'].items() if dest in groups]
                        op['preserved_other_target_columns'] = [dest for dest in op['bone_mapping'].values() if dest not in groups]
                        op['transfer_receipt'] = receipt
                    elif op['kind'] == 'MIRROR':
                        op = _mirror_operation(target, current, names, region, op)
                    operations.append(op)
                if operations:
                    compiled_stage = {'label': stage.get('label', ''), 'operations': operations}
                    current, receipt = _compile_stage(current, names, frozen_edges,
                        local_regions, compiled_stage)
                    current = current.astype(np.float64)
                    stages.append(compiled_stage)
                    step_reports.append(receipt)
            _require(stages, '该目标没有任何明确修复操作：' + key)
            output = current.astype(np.float32)
            self._outputs[key] = output
            changed = output.astype(np.float64) != weights
            _require(output.shape == weights.shape and not (changed & ~authorized).any(),
                     '数值提案越过配方授权点×组边界，拒绝候选')
            self._authorized[key] = authorized
            self.report['targets'][key] = {'source': target.name, 'binding': frozen_binding,
                'steps': step_reports, 'changed_cells': int(changed.sum()),
                'changed_vertices': int(changed.any(axis=1).sum()),
                'deform_mass_max_drift': float(abs(output.sum(axis=1, dtype=np.float64) - weights.sum(axis=1)).max(initial=0)),
                'numeric_field_extractions': 1, 'protection_snapshots_separate': True, 'candidate_clones': 0,
                'output_sha256': hashlib.sha256(output.tobytes()).hexdigest()}
            self._outputs[key].setflags(write=False)
        self.report['timing']['prepare_seconds'] = time.perf_counter() - started
        self._check()
        _PLANS.add(self)
        _install_lifecycle()

    def _check(self):
        skeleton_gate.check_pass(self.rig,self._gate_token,operation='weight_repair.PreparedRepair')
        if bpy.context.scene != self._scene:
            self.cancel()
        _require(not self._done and self._epoch == _EPOCH and bpy.context.scene == self._scene
                 and self.rig.name in bpy.context.view_layer.objects and self.rig.data == self._rig_data,
                 '计划已结束、保存/切换场景或角色身份改变；请重新检查')
        _require(_hash(self.recipe) == self.report['recipe_sha256'], '配方在准备后已改变；请重新检查')
        for key, source in self.targets.items():
            policy._bound(source, self.rig)
            _require(source.data == self._meshes[key] and _snapshot(source, self.rig) == self._sources[key],
                     '源权重/锁/几何/Key/动画/骨架/帧已改变；旧计划失效')
            groups = {name for stage in self.recipe['stages'] for op in stage['operations']
                if self._regions[op['region']]['target'] == key for name in op['groups']}
            _consumers(source, groups)
        for donor, state in self._donors.items():
            _require(_snapshot(donor, self.rig) == state, '供体状态已经改变')

    def cancel(self):
        if not self._done:
            self._done = True
            self.report['status'] = 'cancelled'
        _PLANS.discard(self)
        failures = _cleanup(self._previews, mesh_ids=self._preview_meshes)
        if not failures:
            self._previews = []
            self._preview_meshes = []
        _require(not failures, '显示副本清理未完成：' + '; '.join(failures))

    def preview(self, name_prefix='权重区域预览'):
        self._check()
        outputs = []
        try:
            for key, target in self.targets.items():
                alpha = np.zeros(len(target.data.vertices))
                for region in self._regions.values():
                    if region['target'] == key:
                        alpha[region['ids']] = np.maximum(alpha[region['ids']], region['alpha'])
                outputs.append(policy.create_scope_preview(target, alpha,
                    name=name_prefix + '_' + key + '_' + uuid.uuid4().hex[:6], rig=self.rig))
            self._check()
            self._previews.extend(outputs)
            self._preview_meshes.extend(obj.data for obj in outputs)
            return outputs
        except Exception:
            _cleanup(outputs)
            raise

    def apply(self, name_prefix='局部权重候选'):
        self._check()
        started = time.perf_counter()
        outputs = []
        owned_meshes = []
        try:
            for key, source in self.targets.items():
                before = skinning_job._points(source)
                candidate = source.copy()
                outputs.append(candidate)
                mesh = source.data.copy()
                owned_meshes.append(mesh)
                candidate.data = mesh
                candidate.name = name_prefix + '_' + key + '_' + uuid.uuid4().hex[:6]
                self._scene.collection.objects.link(candidate)
                values, old = self._outputs[key], self._original[key]
                changed = values.astype(np.float64) != old
                _require(not (changed & ~self._authorized[key]).any(), '待写提案已越过授权cell边界')
                for j, name in enumerate(self._names[key]):
                    group = candidate.vertex_groups[name]
                    for i in np.flatnonzero(changed[:, j]):
                        value = float(values[i, j])
                        if value == 0:
                            group.remove([int(i)])
                        else:
                            group.add([int(i)], value, 'REPLACE')
                _verify_candidate(source, candidate, self._names[key], values, changed)
                bpy.context.view_layer.update()
                after = skinning_job._points(candidate)
                scale = float(self._scene.unit_settings.scale_length)
                _require(before.shape == after.shape and np.linalg.norm(after - before, axis=1).max(initial=0) * scale <= 1e-5,
                         '候选中立求值跳变，已撤回本次候选')
                self.report['targets'][key]['candidate_clones'] = 1
                self.report['targets'][key]['output'] = candidate.name
            self._check()
            self.report['status'] = 'applied'
            self.report['timing']['apply_seconds'] = time.perf_counter() - started
            payload = json.dumps({'recipe': self.recipe, 'report': self.report},
                ensure_ascii=False, allow_nan=False, sort_keys=True)
            for candidate in outputs:
                candidate['rigging_weight_repair_v1'] = payload
                _require(candidate['rigging_weight_repair_v1'] == payload, '最终候选回执回读不一致')
            failures = _cleanup(self._previews, mesh_ids=self._preview_meshes)
            _require(not failures, '提交时显示副本清理失败：' + '; '.join(failures))
            self._previews = []
            self._preview_meshes = []
            self._done = True
            _PLANS.discard(self)
            return outputs
        except Exception as exc:
            failures = _cleanup(outputs, mesh_ids=owned_meshes)
            preview_failures = _cleanup(self._previews, mesh_ids=self._preview_meshes)
            failures.extend(preview_failures)
            if not preview_failures:
                self._previews = []
                self._preview_meshes = []
            self.report['status'] = 'rollback_incomplete' if failures else 'failed_no_candidates'
            self.report['cleanup_errors'] = failures
            self._done = True
            _PLANS.discard(self)
            if failures:
                exc.add_note('回退未闭合：' + '; '.join(failures))
            raise


def _verify_candidate(source, candidate, names, values, changed):
    _require(sig._mesh_signature(source.data) == sig._mesh_signature(candidate.data)
             and policy._shape_state(source) == policy._shape_state(candidate), '候选改变了RAW/UV/Key等非权重数据')
    source_names, old = sig._weights(source)
    candidate_names, new = sig._weights(candidate)
    _require(source_names == candidate_names and [g.lock_weight for g in source.vertex_groups] ==
             [g.lock_weight for g in candidate.vertex_groups], '组集合/顺序或锁改变')
    column = {candidate.vertex_groups[name].index: j for j, name in enumerate(names)}
    for i, (before, after) in enumerate(zip(old, new)):
        previous, current = dict(before), dict(after)
        mutable = {g for g, j in column.items() if changed[i, j]}
        _require({g: w for g, w in previous.items() if g not in mutable} ==
                 {g: w for g, w in current.items() if g not in mutable}, '非目标cell或membership改变')
        for group in mutable:
            _require(current.get(group, 0.) == float(values[i, column[group]]), '写入未保持编译float32值')


def _cleanup(objects, *, mesh_ids=None):
    failures = []
    meshes = list(mesh_ids) if mesh_ids is not None else []
    for obj in reversed(objects):
        try:
            if mesh_ids is None:
                meshes.append(obj.data)
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        except ReferenceError:
            pass
        except Exception as exc:
            failures.append(str(exc))
    for mesh in meshes:
        try:
            if mesh.name in bpy.data.meshes and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        except ReferenceError:
            pass
        except Exception as exc:
            failures.append(str(exc))
    return failures


def prepare(targets, rig, recipe):
    return PreparedRepair(targets, rig, recipe)


def run_batch(items, *, continue_on_error=False, cancelled=None):
    _require(isinstance(items, list) and items, '批次需要明确非空条目')
    results = []
    for item in items:
        if cancelled is not None and cancelled():
            return {'schema': 'rigging.weight-repair.batch.v1', 'status': 'cancelled_between_roles',
                'results': results, 'unprocessed': len(items) - len(results), 'atomicity': 'per_role_candidate_set'}
        label = item.get('label', '') if isinstance(item, dict) else ''
        try:
            _require(isinstance(item, dict), '批次条目需要dict')
            plan = prepare(item['targets'], item['rig'], item['recipe'])
            outputs = plan.apply(name_prefix=item.get('name_prefix', '批次权重候选'))
            results.append({'label': label, 'status': 'succeeded',
                'outputs': [obj.name for obj in outputs], 'report': plan.report})
        except Exception as exc:
            results.append({'label': label, 'status': 'failed', 'error': str(exc)})
            if not continue_on_error:
                break
    return {'schema': 'rigging.weight-repair.batch.v1', 'results': results,
            'unprocessed': len(items) - len(results), 'atomicity': 'per_role_candidate_set'}


@persistent
def _invalidate(_):
    global _EPOCH
    _EPOCH += 1
    for plan in list(_PLANS):
        plan.cancel()


def _install_lifecycle():
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre):
        if _invalidate not in handlers:
            handlers.append(_invalidate)
    if _observe_scene not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_observe_scene)
    bpy.msgbus.clear_by_owner(_OBSERVER_OWNER)
    bpy.msgbus.subscribe_rna(key=(bpy.types.Window, 'scene'), owner=_OBSERVER_OWNER,
        args=(), notify=invalidate_scene_context, options={'PERSISTENT'})


def invalidate_scene_context():
    """Required before programmatic Scene switching; no event-history guessing.

    Direct RNA A->B->A with no notification is not observable in synchronous
    Blender. See the independently approved scene lifecycle amendment.
    """
    _invalidate(None)


def _scene_changed():
    for plan in tuple(_PLANS):
        if bpy.context.scene != plan._scene:
            plan.cancel()


@persistent
def _observe_scene(scene, depsgraph):
    _scene_changed()


def release():
    _invalidate(None)
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre):
        if _invalidate in handlers:
            handlers.remove(_invalidate)
    if _observe_scene in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_observe_scene)
    bpy.msgbus.clear_by_owner(_OBSERVER_OWNER)
