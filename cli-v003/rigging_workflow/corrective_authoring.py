"""Restricted, persistent native pose-sculpt authoring for plain single-segment LBS.

begin returns a RAW posed Mesh with the original Subsurf and no Armature.
commit adds one relative Key at manual value 1 only after native replay succeeds.
Existing Keys, B-Bone deformation, DQ and non-Subsurf stacks are unsupported.
This module does not install UI, pose drivers, handlers or artistic corrections.
"""
import hashlib
import json
import math
import time
import uuid

import bpy
import numpy as np
from mathutils import Matrix

_TAG = 'c94_pose_sculpt_v1'
_SOURCE = 'c94_pose_sculpt_source'
_SOURCE_MESH = 'c94_pose_sculpt_source_mesh'
_RIG = 'c94_pose_sculpt_rig'
_CAPTURE = 'c94_pose_sculpt_capture'
_SCENE = 'c94_pose_sculpt_scene'
_TARGET_EDIT_ATTRIBUTES = {
    '.select_vert': ('POINT', 'BOOLEAN'), '.select_edge': ('EDGE', 'BOOLEAN'),
    '.select_poly': ('FACE', 'BOOLEAN'), '.sculpt_mask': ('POINT', 'FLOAT'),
    '.sculpt_face_set': ('FACE', 'INT'),
    '.uv_select_vert': ('CORNER', 'BOOLEAN'),
    '.uv_select_edge': ('CORNER', 'BOOLEAN'),
    '.uv_select_face': ('FACE', 'BOOLEAN'),
}


class CorrectiveAuthoringError(ValueError):
    """A rejected operation; ``code`` is stable for GUI/CLI presentation."""
    def __init__(self, code, message, report=None):
        super().__init__(message)
        self.code = code
        self.report = report or {}


def _require(value, code, message, report=None):
    if not value:
        raise CorrectiveAuthoringError(code, message, report)


def _plain(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bpy.types.ID):
        return [value.bl_rna.identifier, value.name_full,
                value.library.filepath if value.library else None]
    if hasattr(value, 'to_dict'):
        return _plain(value.to_dict())
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted(value)
    return [_plain(v) for v in value]


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _array_hash(value):
    a = np.ascontiguousarray(value)
    return [str(a.dtype), list(a.shape), hashlib.sha256(a.tobytes()).hexdigest()]


def _persist(text, data):
    """Same JSON truth, short native Text lines; avoid quadratic long-line edits."""
    started = time.perf_counter()
    payload = json.dumps(data, ensure_ascii=False, indent=0, allow_nan=False)
    serialized = time.perf_counter()
    previous = text.as_string()
    try:
        text.from_string(payload)
        written = time.perf_counter()
        _require(json.loads(text.as_string()) == data, 'PERSISTENCE_READBACK_FAILED',
                 '捕获JSON持久化回读不一致')
    except Exception:
        text.from_string(previous)
        raise
    return {'json_seconds': serialized - started,
            'native_text_seconds': written - serialized,
            'readback_seconds': time.perf_counter() - written,
            'characters': len(payload), 'lines': payload.count('\n') + 1}


def _rna(owner):
    result = {}
    for prop in owner.bl_rna.properties:
        if prop.identifier == 'rna_type' or prop.is_readonly:
            continue
        value = getattr(owner, prop.identifier)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            result[prop.identifier] = _plain(value)
        elif prop.type == 'POINTER' and isinstance(value, bpy.types.ID):
            result[prop.identifier] = _plain(value)
    return result


def _buffer(collection, field, width=1, dtype=np.float32):
    values = np.empty(len(collection) * width, dtype=dtype)
    collection.foreach_get(field, values)
    return _array_hash(values)


def _coordinates(mesh):
    a = np.empty((len(mesh.vertices), 3), dtype=np.float32)
    mesh.vertices.foreach_get('co', a.ravel())
    return a


def _mesh_signature(mesh, *, coordinates=True, details=False):
    attrs = []
    for attr in mesh.attributes:
        if not coordinates and attr.name in _TARGET_EDIT_ATTRIBUTES:
            _require((attr.domain, attr.data_type) == _TARGET_EDIT_ATTRIBUTES[attr.name],
                     'TARGET_AUXILIARY_ATTRIBUTE_INVALID', '原生编辑辅助属性的类型或域已改变')
            continue
        if not coordinates and attr.name == 'position':
            continue
        fields = []
        if len(attr.data):
            for prop in attr.data[0].bl_rna.properties:
                if prop.type not in {'BOOLEAN', 'INT', 'FLOAT', 'STRING'}:
                    continue
                name = prop.identifier
                if prop.type == 'STRING':
                    values = _hash([getattr(item, name) for item in attr.data])
                else:
                    width = prop.array_length if prop.is_array else 1
                    dtype = {'BOOLEAN': np.bool_, 'INT': np.int32,
                             'FLOAT': np.float32}[prop.type]
                    values = _buffer(attr.data, name, width, dtype)
                fields.append([name, values])
        attrs.append([attr.name, attr.domain, attr.data_type, fields])
    data = {'count': len(mesh.vertices), 'attributes': attrs,
            'edges': _buffer(mesh.edges, 'vertices', 2, np.int32),
            'loops': _buffer(mesh.loops, 'vertex_index', 1, np.int32),
            'starts': _buffer(mesh.polygons, 'loop_start', 1, np.int32),
            'totals': _buffer(mesh.polygons, 'loop_total', 1, np.int32),
            'materials': [_plain(m) for m in mesh.materials],
            'face_materials': _buffer(mesh.polygons, 'material_index', 1, np.int32),
            'smooth': _buffer(mesh.polygons, 'use_smooth', 1, np.bool_),
            'custom_normals': mesh.has_custom_normals,
            'uv': [[u.name, bool(u.active_render), _buffer(u.data, 'uv', 2)]
                   for u in mesh.uv_layers],
            'active_uv': mesh.uv_layers.active_index}
    if coordinates:
        data['coordinates'] = _array_hash(_coordinates(mesh))
        data['corner_normals'] = _buffer(mesh.corner_normals, 'vector', 3)
    return data if details else _hash(data)


def _weights(source):
    names = [g.name for g in source.vertex_groups]
    rows = [[[g.group, float(g.weight)] for g in v.groups]
            for v in source.data.vertices]
    return names, rows


def _drivers(owner):
    animation = owner.animation_data
    if not animation:
        return None
    return {'action': _plain(animation.action),
            'drivers': [{'fcurve': _rna(fc), 'driver': _rna(fc.driver),
                         'modifiers': [_rna(m) for m in fc.modifiers],
                         'variables': [{'rna': _rna(v),
                                        'targets': [_rna(t) for t in v.targets]}
                                       for v in fc.driver.variables]}
                        for fc in animation.drivers]}


def _rig_state(rig):
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    bones = []
    for bone in rig.data.bones:
        pb = rig.pose.bones[bone.name]
        constraints = []
        for c in pb.constraints:
            item = _rna(c)
            if c.type == 'ARMATURE':
                item['targets'] = [_rna(t) for t in c.targets]
            constraints.append(item)
        bones.append({'name': bone.name, 'parent': bone.parent.name if bone.parent else None,
                      'rest': _plain(bone.matrix_local), 'bone': _rna(bone),
                      'pose': _rna(pb), 'properties': _plain(dict(pb.items())),
                      'constraints': constraints})
    matrices = np.asarray([np.asarray(p.matrix, dtype=np.float32)
                           for p in evaluated.pose.bones], dtype=np.float32)
    return {'contract': _hash({'bones': bones, 'drivers': _drivers(rig),
                              'properties': _plain(dict(rig.items())),
                              'world': _plain(rig.matrix_world)}),
            'evaluated_names': [p.name for p in evaluated.pose.bones],
            'evaluated_matrices': _array_hash(matrices),
            'evaluated_world': _plain(evaluated.matrix_world)}


def _state(source, rig):
    names, weights = _weights(source)
    scene = bpy.context.scene
    return {'mesh': _mesh_signature(source.data),
            'weights': _hash([names, weights, [g.lock_weight for g in source.vertex_groups]]),
            'modifiers': _hash([_rna(m) for m in source.modifiers]),
            'object_world': _plain(source.matrix_world),
            'evaluated_object_world': _plain(source.evaluated_get(
                bpy.context.evaluated_depsgraph_get()).matrix_world),
            'object_constraints': _hash([_rna(c) for c in source.constraints]),
            'object_animation': _hash(_drivers(source)),
            'rig': _rig_state(rig), 'scene_frame': [scene.frame_current, scene.frame_subframe],
            'unit_scale': float(scene.unit_settings.scale_length),
            'view_layer': bpy.context.view_layer.name}


def _supported(source):
    _require(bpy.context.mode == 'OBJECT', 'OBJECT_MODE_REQUIRED', '请先结束编辑或雕刻模式')
    _require(isinstance(source, bpy.types.Object) and source.type == 'MESH'
             and source.name in bpy.context.view_layer.objects
             and not source.library and not source.data.library
             and not source.override_library and _single_user_mesh(source)
             and source.is_editable and source.data.is_editable,
             'LOCAL_SINGLE_USER_MESH_REQUIRED', '需要当前视图层可编辑的本地单用户网格')
    _require(source.data.shape_keys is None, 'EXISTING_KEYS_UNSUPPORTED',
             '当前局部作者入口不支持已有形态键，请保留原工程并使用无Key工作副本')
    _require([m.type for m in source.modifiers] in (['ARMATURE'], ['ARMATURE', 'SUBSURF']),
             'MODIFIER_STACK_UNSUPPORTED', '仅支持唯一普通LBS Armature，其后可有一个Subsurf')
    arm = source.modifiers[0]
    rig = arm.object
    _require(rig and rig.type == 'ARMATURE' and rig.name in bpy.context.view_layer.objects
             and not rig.library and not rig.data.library and rig.mode != 'EDIT',
             'RIG_UNSUPPORTED', 'Armature目标必须为当前视图层内的本地骨架')
    _require(arm.show_viewport and arm.use_vertex_groups and not arm.use_bone_envelopes
             and not arm.use_deform_preserve_volume and not arm.use_multi_modifier
             and not arm.vertex_group, 'PLAIN_LBS_REQUIRED',
             '仅支持启用的普通顶点组LBS，不支持mask、envelope、DQ或multi modifier')
    _require(not (source.animation_data and source.animation_data.drivers),
             'SOURCE_OBJECT_DRIVERS_UNSUPPORTED', '当前入口不支持源对象自身driver')
    return rig


def _single_user_mesh(source):
    # Persistent ID references in owned captures are bookkeeping users, not
    # additional geometry consumers. Reject every other extra Mesh user.
    owned_refs = sum(1 for obj in bpy.data.objects
                     if obj.get(_TAG) and obj.get(_SOURCE_MESH) == source.data)
    consumers = sum(1 for obj in bpy.data.objects if obj.type == 'MESH' and obj.data == source.data)
    return consumers == 1 and source.data.users == 1 + owned_refs


def _number(value, field):
    _require(not isinstance(value, bool) and isinstance(value, (int, float))
             and math.isfinite(value) and value > 0,
             'POSITIVE_FINITE_VALUE_REQUIRED', field + '必须为明确的有限正数')
    return float(value)


def _domain(indices, count):
    try:
        values = list(indices)
    except TypeError as exc:
        raise CorrectiveAuthoringError('EXPLICIT_DOMAIN_REQUIRED', '必须明确提供顶点索引域') from exc
    _require(bool(values) and len(values) <= count
             and all(not isinstance(i, (bool, np.bool_)) and isinstance(i, (int, np.integer))
                     and 0 <= int(i) < count for i in values)
             and len(set(int(i) for i in values)) == len(values),
             'UNIQUE_FINITE_DOMAIN_REQUIRED', '顶点域必须唯一、非空且索引有效')
    return np.asarray(sorted(int(i) for i in values), dtype=np.int32)


def _discard(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _raw_clone(source):
    """Evaluate Armature on an isolated copy; never toggle source modifiers."""
    clone = source.copy()
    clone.data = source.data.copy()
    clone.animation_data_clear()
    clone.parent = None
    for c in list(clone.constraints):
        clone.constraints.remove(c)
    clone.matrix_world = source.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
    for mod in clone.modifiers:
        if mod.type == 'SUBSURF':
            mod.show_viewport = False
    bpy.context.scene.collection.objects.link(clone)
    clone.hide_set(False)
    clone.hide_viewport = False
    bpy.context.view_layer.update()
    return clone


def _evaluated(obj):
    graph = bpy.context.evaluated_depsgraph_get()
    owner = obj.evaluated_get(graph)
    mesh = owner.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
    try:
        return _coordinates(mesh), _buffer(mesh.loops, 'vertex_index', 1, np.int32), \
            _buffer(mesh.polygons, 'loop_total', 1, np.int32)
    finally:
        owner.to_mesh_clear()


def _native_A(clone, source, rig, ids):
    names, rows = _weights(source)
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    valid = {i: n for i, n in enumerate(names)
             if n in evaluated.pose.bones and evaluated.data.bones[n].use_deform}
    W = np.zeros((len(ids), len(names)), dtype=np.float64)
    for k, index in enumerate(ids):
        for group, weight in rows[int(index)]:
            W[k, group] = weight
    active = [i for i in valid if np.any(W[:, i] > 0)]
    _require(all(evaluated.data.bones[valid[i]].bbone_segments == 1 for i in active),
             'MULTISEGMENT_DEFORM_UNSUPPORTED', '所选域实际参与的形变骨必须为single segment')
    total = W[:, list(valid)].sum(axis=1)
    _require(np.isfinite(W).all() and (W >= 0).all() and np.all(total > 0),
             'VALID_DEFORM_WEIGHTS_REQUIRED', '所选顶点必须具备有效形变骨权重')
    world = np.asarray(clone.matrix_world, dtype=np.float64)
    rig_world = np.asarray(evaluated.matrix_world, dtype=np.float64)
    into = np.linalg.inv(rig_world) @ world
    back = np.linalg.inv(world) @ rig_world
    A = np.zeros((len(ids), 3, 3), dtype=np.float64)
    for i in active:
        name = valid[i]
        D = np.asarray(evaluated.pose.bones[name].matrix, dtype=np.float64) @ np.linalg.inv(
            np.asarray(evaluated.data.bones[name].matrix_local, dtype=np.float64))
        A += (W[:, i] / total)[:, None, None] * (back @ D @ into)[:3, :3]
    clone.crazyspace_eval(bpy.context.evaluated_depsgraph_get(), bpy.context.scene)
    try:
        native = np.asarray([np.column_stack([np.asarray(
            clone.crazyspace_displacement_to_deformed(vertex_index=int(i), displacement=axis),
            dtype=np.float64) for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]) for i in ids])
    finally:
        clone.crazyspace_eval_clear()
    error = float(np.max(np.abs(A - native)))
    _require(np.isfinite(A).all() and error <= 1e-5, 'NATIVE_MATRIX_MISMATCH',
             '解析LBS与实际三轴crazyspace不一致', {'matrix_max_abs_error': error})
    sigma = np.linalg.svd(A, compute_uv=False)
    _require(np.isfinite(sigma).all() and np.all(sigma[:, -1] >
             64 * np.finfo(np.float32).eps * sigma[:, 0]), 'SINGULAR_LBS_MATRIX',
             '所选域LBS矩阵不可稳定逆解')
    return A, {'native_matrix_max_abs_error': error,
               'sigma_min': float(sigma[:, -1].min()),
               'condition_max': float((sigma[:, 0] / sigma[:, -1]).max()),
               'participating_bones': [valid[i] for i in active]}


def begin(source, vertex_indices, *, name, max_rest_displacement_meters):
    """Return owned, persistent posed RAW target; source remains untouched.

    ``vertex_indices`` is explicit (duplicates rejected). The rest displacement
    budget is frozen here in scene-unit-adjusted world metres, not at commit.
    Save the .blend normally to preserve the target, references and capture.
    """
    rig = _supported(source)
    ids = _domain(vertex_indices, len(source.data.vertices))
    budget = _number(max_rest_displacement_meters, '静置位移预算')
    _require(isinstance(name, str) and bool(name.strip()), 'NAME_REQUIRED', '请提供目标名称')
    bpy.context.view_layer.update()
    before = _state(source, rig)
    target = None
    text = None
    try:
        target = _raw_clone(source)
        A, oracle = _native_A(target, source, rig, ids)
        graph = bpy.context.evaluated_depsgraph_get()
        owner = target.evaluated_get(graph)
        evaluated = owner.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
        try:
            posed_mesh = evaluated.copy()
        finally:
            owner.to_mesh_clear()
        old_mesh = target.data
        target.data = posed_mesh
        if old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
        target.modifiers.remove(target.modifiers[0])
        if len(target.modifiers):
            target.modifiers[0].show_viewport = source.modifiers[1].show_viewport
        target.data.update()
        bpy.context.view_layer.update()
        initial = _coordinates(target.data)
        _require(len(initial) == len(source.data.vertices) and np.isfinite(initial).all(),
                 'RAW_TOPOLOGY_MISMATCH', 'Armature RAW求值未保持源顶点域')
        expected = _evaluated(source)
        actual = _evaluated(target)
        _require(expected[1:] == actual[1:] and np.array_equal(expected[0], actual[0]),
                 'INITIAL_NATIVE_REPLAY_MISMATCH', '独立RAW目标未exact重放原Subsurf表面')
        _require(_state(source, rig) == before, 'SOURCE_CHANGED_DURING_BEGIN',
                 '捕获期间源数据或骨架实际求值发生变化')
        owner_id = str(uuid.uuid4())
        target.name = name.strip()
        target[_TAG] = owner_id
        target.data[_TAG] = owner_id
        target[_SOURCE] = source
        target[_SOURCE_MESH] = source.data
        target[_RIG] = rig
        target[_SCENE] = bpy.context.scene
        capture = {'schema': 'c94.pose_sculpt.v1', 'id': owner_id, 'status': 'editing',
                   'source_state': before, 'vertex_indices': ids.tolist(),
                   'initial_posed_local': initial.tolist(),
                   'source_raw_local': _coordinates(source.data).tolist(),
                   'analytical_A': A.tolist(), 'oracle': oracle,
                   'max_rest_displacement_meters': budget,
                   'target_world': _plain(target.matrix_world),
                   'target_mesh_signature': _mesh_signature(target.data, coordinates=False),
                   'target_weights': _hash(_weights(target)),
                   'target_modifiers': _hash([_rna(m) for m in target.modifiers])}
        text = bpy.data.texts.new('姿态修正捕获_' + owner_id)
        text[_TAG] = owner_id
        target['c94_pose_sculpt_persistence_timing'] = _persist(text, capture)
        target[_CAPTURE] = text
        return target
    except Exception:
        if target is not None:
            _discard(target)
        if text is not None and text.users == 0:
            bpy.data.texts.remove(text)
        raise


def _capture(target):
    _require(isinstance(target, bpy.types.Object) and target.type == 'MESH'
             and bool(target.get(_TAG)), 'OWNED_TARGET_REQUIRED', '请选择本工具创建的姿态修正目标')
    text = target.get(_CAPTURE)
    _require(isinstance(text, bpy.types.Text) and text.get(_TAG) == target[_TAG],
             'CAPTURE_MISSING', '持久捕获记录缺失或身份不匹配')
    try:
        data = json.loads(text.as_string())
    except (ValueError, TypeError) as exc:
        raise CorrectiveAuthoringError('INVALID_CAPTURE', '持久捕获记录无法解析') from exc
    _require(data.get('schema') == 'c94.pose_sculpt.v1' and data.get('id') == target[_TAG],
             'INVALID_CAPTURE', '持久捕获版本或身份不匹配')
    return data


def inspect(target):
    """Read identity/domain/current RAW coordinate edits; never mutates source."""
    data = _capture(target)
    current = _coordinates(target.data)
    initial = np.asarray(data['initial_posed_local'], dtype=np.float32)
    valid_count = current.shape == initial.shape
    changed = np.flatnonzero(np.any(current != initial, axis=1)) if valid_count else np.arange(len(current))
    allowed = data['vertex_indices']
    outside = sorted(set(changed.tolist()) - set(allowed))
    source = target.get(_SOURCE)
    return {'schema': data['schema'], 'id': data['id'], 'status': data['status'],
            'target': target.name, 'source': source.name if source else None,
            'allowed_vertex_indices': allowed, 'allowed_count': len(allowed),
            'allowed_vertex_count': len(allowed),
            'changed_vertex_indices': changed.tolist(), 'changed_count': len(changed),
            'changed_vertex_count': len(changed),
            'outside_changed_vertex_indices': outside, 'raw_count_matches': valid_count,
            'outside_changed_vertex_count': len(outside),
            'object_matrix_matches': _plain(target.matrix_world) == data['target_world'],
            'max_rest_displacement_meters': data['max_rest_displacement_meters'],
            'manual_key_value': 1.0 if data['status'] == 'committed' else None,
            'automatic_driver_created': False}


def _check_target(target, data):
    _require(target.name in bpy.context.view_layer.objects and not target.library
             and target.data.users == 1 and target.data.get(_TAG) == data['id'],
             'TARGET_IDENTITY_CHANGED', '目标必须仍为本视图层内自有单用户网格')
    _require(_plain(target.matrix_world) == data['target_world'] and not target.parent
             and not target.constraints and not target.animation_data,
             'TARGET_TRANSFORM_CHANGED', '只能编辑目标几何，不能移动对象或添加对象驱动')
    _require(target.data.shape_keys is None and
             _mesh_signature(target.data, coordinates=False) == data['target_mesh_signature']
             and _hash(_weights(target)) == data['target_weights']
             and _hash([_rna(m) for m in target.modifiers]) == data['target_modifiers'],
             'TARGET_STRUCTURE_CHANGED', '目标拓扑、UV、权重或Subsurf设置发生改变')
    initial = np.asarray(data['initial_posed_local'], dtype=np.float32)
    desired = _coordinates(target.data)
    ids = np.asarray(data['vertex_indices'], dtype=np.int32)
    outside = np.ones(len(initial), dtype=bool)
    outside[ids] = False
    _require(np.isfinite(desired).all() and np.array_equal(desired[outside], initial[outside]),
             'OUTSIDE_DOMAIN_CHANGED', '目标存在域外顶点修改；提交已拒绝，源保持不变')
    return ids, outside, desired, initial


def _update(source):
    source.data.update()
    source.update_tag(refresh={'OBJECT', 'DATA'})
    if source.data.shape_keys:
        source.data.shape_keys.update_tag()
    bpy.context.view_layer.update()


def commit(target, *, key_name, max_replay_error_meters=1e-5):
    """Commit a manual relative Key=1 after native RAW/Subsurf and rig checks.

    Rejected preflight leaves source unchanged. A replay failure removes only
    this operation's Key/Basis, restores the source display state and raises.
    Target remains available after both rejection and successful commit.
    """
    data = _capture(target)
    _require(data['status'] == 'editing', 'TARGET_ALREADY_COMMITTED', '此目标已提交或已取消')
    source, rig = target.get(_SOURCE), target.get(_RIG)
    _require(source and rig and source.data == target.get(_SOURCE_MESH)
             and target.get(_SCENE) == bpy.context.scene,
             'SOURCE_IDENTITY_CHANGED', '源Mesh、骨架或场景引用发生改变')
    actual_rig = _supported(source)
    _require(actual_rig == rig, 'RIG_IDENTITY_CHANGED', 'Armature真实目标已改变')
    limit = _number(max_replay_error_meters, '重放误差阈值')
    _require(isinstance(key_name, str) and bool(key_name.strip()) and key_name.strip() != 'Basis',
             'KEY_NAME_REQUIRED', '请提供与Basis不同的修正Key名称')
    ids, outside, desired, initial = _check_target(target, data)
    before = _state(source, rig)
    _require(before == data['source_state'], 'STALE_CAPTURE',
             '捕获后源数据、权重、modifier、帧、pose或实际骨架矩阵发生变化；请重新开始')
    A = np.asarray(data['analytical_A'], dtype=np.float64)
    delta = np.linalg.solve(A, (desired[ids].astype(np.float64) - initial[ids])[..., None])[..., 0]
    world3 = np.asarray(source.matrix_world, dtype=np.float64)[:3, :3]
    lengths = np.linalg.norm(delta @ world3.T, axis=1) * before['unit_scale']
    max_delta = float(lengths.max())
    _require(np.isfinite(delta).all() and max_delta <= data['max_rest_displacement_meters'],
             'REST_DISPLACEMENT_BUDGET_EXCEEDED', '静置位移超出开始时冻结的预算；未写入源',
             {'max_rest_displacement_meters': max_delta,
              'budget_meters': data['max_rest_displacement_meters']})
    raw = np.asarray(data['source_raw_local'], dtype=np.float32)
    wanted = raw.astype(np.float64)
    wanted[ids] += delta
    target_final = _evaluated(target)
    created = []
    clone = None
    old_index, old_only = source.active_shape_key_index, source.show_only_shape_key
    try:
        basis = source.shape_key_add(name='Basis', from_mix=False)
        created.append(basis)
        key = source.shape_key_add(name=key_name.strip(), from_mix=False)
        created.append(key)
        _require(key.name == key_name.strip(), 'KEY_NAME_CHANGED', 'Key名称发生冲突或被截断')
        key.relative_key = basis
        source.data.shape_keys.use_relative = True
        for index in ids:
            key.data[int(index)].co = wanted[int(index)]
        key.value = 1.0
        source.show_only_shape_key = False
        _update(source)
        _require(np.array_equal(np.asarray([tuple(p.co) for p in basis.data], dtype=np.float32), raw)
                 and np.array_equal(np.asarray([tuple(p.co) for p in key.data], dtype=np.float32)[outside], raw[outside]),
                 'KEY_WRITE_DOMAIN_VIOLATION', 'Basis或Key域外坐标保护失败')
        # RAW oracle on a disposable copy: source Subsurf flags never change.
        clone = _raw_clone(source)
        actual_raw = _evaluated(clone)[0]
        _discard(clone)
        clone = None
        actual_final = _evaluated(source)
        unit = before['unit_scale']
        raw_error = float(np.linalg.norm((actual_raw.astype(np.float64) - desired) @ world3.T, axis=1).max() * unit)
        _require(actual_final[1:] == target_final[1:] and actual_final[0].shape == target_final[0].shape,
                 'FINAL_TOPOLOGY_MISMATCH', '原Subsurf重放拓扑不匹配')
        final_error = float(np.linalg.norm((actual_final[0].astype(np.float64) - target_final[0]) @ world3.T, axis=1).max() * unit)
        _require(raw_error <= limit and final_error <= limit, 'NATIVE_REPLAY_FAILED',
                 'Key=1实际RAW或原Subsurf未重放目标',
                 {'raw_error_meters': raw_error, 'subsurf_error_meters': final_error})
        _require(np.array_equal(actual_raw[outside], initial[outside]),
                 'NATIVE_OUTSIDE_DOMAIN_CHANGED', 'Key重放改变了RAW域外顶点')
        _require(_state(source, rig) == before, 'SOURCE_OR_RIG_FEEDBACK',
                 'Key使原源数据或C94实际骨架矩阵发生变化')
        _require(source.data.shape_keys.animation_data is None, 'UNEXPECTED_KEY_ANIMATION',
                 '新Key存在意外动画或driver')
        report = {'ok': True, 'target': target.name, 'source': source.name,
                  'key_name': key.name, 'manual_value': 1.0, 'relative': True,
                  'automatic_driver_created': False, 'authored_vertex_count': len(ids),
                  'max_rest_displacement_meters': max_delta,
                  'raw_error_meters': raw_error, 'subsurf_error_meters': final_error,
                  'raw_outside_exact': True, 'basis_source_mesh_weights_rig_exact': True,
                  'oracle': data['oracle']}
        data['status'] = 'committed'
        data['commit_receipt'] = report
        target['c94_pose_sculpt_persistence_timing'] = _persist(target[_CAPTURE], data)
        return report
    except Exception:
        if clone is not None:
            _discard(clone)
        for block in reversed(created):
            source.shape_key_remove(block)
        source.active_shape_key_index = old_index
        source.show_only_shape_key = old_only
        _update(source)
        raise


def cancel(target):
    """Remove this owned comparison target/capture only; never removes a Key."""
    data = _capture(target)
    text = target.get(_CAPTURE)
    mesh = target.data
    mesh_owned = mesh.get(_TAG) == data['id']
    name = target.name
    bpy.data.objects.remove(target, do_unlink=True)
    if mesh_owned and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    if text.users == 0:
        bpy.data.texts.remove(text)
    return {'ok': True, 'removed_target': name, 'source_changed': False,
            'source_keys_removed': False, 'previous_status': data['status']}
