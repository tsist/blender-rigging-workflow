# SPDX-License-Identifier: GPL-3.0-or-later
"""Restricted native face-pose samples to independent multi-Key candidates.

Only a measured near-identity neutral RAW output is accepted. This is not
an inverse-skinning solver or a promise that linear Key combinations reproduce
nonlinear bone-pose combinations. Sessions and submission authority are RAM-only.
"""
from contextlib import contextmanager
import copy
import json
import math
import uuid

import bpy
import numpy as np
from bpy.app.handlers import persistent

from . import corrective_authoring as sig, face_modes as modes, pose_switching
from . import skinning_policy
from .face_pose_library import STATE_KEY as POSE_LIBRARY_KEY

TOLERANCE_METERS = 1e-5
_SESSIONS = {}
_FIELDS = modes._FIELDS
_IGNORE_RNA = {'matrix', 'matrix_basis', 'matrix_channel', 'select',
               'select_head', 'select_tail', 'hide'}


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _identity(value):
    return value.as_pointer() if value is not None else None


def _rna(owner, id_owner=None, *, omit=()):
    """Freeze writable inputs, not driver-evaluated values or UI selections."""
    values = sig._rna(owner)
    driven = {}
    ad = id_owner.animation_data if id_owner else None
    if ad:
        for curve in ad.drivers:
            driven.setdefault(curve.data_path, set()).add(curve.array_index)
    for field in tuple(values):
        if field in _IGNORE_RNA or field in omit:
            del values[field]
            continue
        try:
            path = owner.path_from_id(field)
        except (ValueError, TypeError, AttributeError):
            path = field
        if path in driven:
            value = values[field]
            if isinstance(value, list):
                values[field] = [None if i in driven[path] else v for i, v in enumerate(value)]
            else:
                values[field] = None
    # Pointer identity matters when an equal-named dependency is replaced.
    values['_pointers'] = {}
    for prop in owner.bl_rna.properties:
        if prop.type != 'POINTER' or prop.identifier == 'rna_type':
            continue
        value = getattr(owner, prop.identifier)
        if isinstance(value, bpy.types.ID):
            values['_pointers'][prop.identifier] = _identity(value)
    return values


def _properties(owner, allowed=(), id_owner=None):
    values = dict(owner.items())
    driven = {c.data_path for c in id_owner.animation_data.drivers} if id_owner and id_owner.animation_data else set()
    prefix = '' if isinstance(owner, bpy.types.ID) else owner.path_from_id()
    for key in tuple(values):
        path = prefix + '[' + json.dumps(key, ensure_ascii=False) + ']'
        if key in allowed or path in driven:
            del values[key]
    return sig._plain(values)


def _action(action):
    if action is None:
        return None
    rows = []
    for layer in action.layers:
        strips = []
        for strip in layer.strips:
            bags = []
            _require(strip.type == 'KEYFRAME', '当前Action条带无法冻结，请使用普通Keyframe Action')
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag is not None:
                    bags.append([slot.handle, [{
                        'curve': sig._rna(f),
                        'keys': [sig._rna(k) for k in f.keyframe_points],
                        'samples': [list(k.co) for k in f.sampled_points],
                        'modifiers': [sig._rna(m) for m in f.modifiers],
                    } for f in bag.fcurves]])
            strips.append([sig._rna(strip), bags])
        rows.append([sig._rna(layer), strips])
    return [_identity(action), rows]


def _animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    def strips(items):
        return [[sig._rna(s), _action(s.action) if s.action else None,
                 strips(s.strips) if s.type == 'META' else []] for s in items]
    return [sig._drivers(owner), sig._rna(ad), _action(ad.action),
            [[sig._rna(t), strips(t.strips)] for t in ad.nla_tracks]]


def _rig_state(rig, neutral):
    allowed_properties = {}
    for row in neutral['properties']:
        allowed_properties.setdefault(row['bone'], set()).add(row['key'])
    allowed_properties.setdefault(modes.MODE_BONE, set()).add(modes.MODE_PROPERTY)
    bones = []
    for bone in rig.data.bones:
        pb = rig.pose.bones[bone.name]
        bones.append({
            'name': bone.name, 'parent': bone.parent.name if bone.parent else None,
            'rest': sig._plain(bone.matrix_local), 'bone': _rna(bone, rig.data),
            'handles': [getattr(bone, f).name if getattr(bone, f) else None for f in
                        ('bbone_custom_handle_start', 'bbone_custom_handle_end')],
            'pose': _rna(pb, rig, omit=(*_FIELDS, 'rotation_mode') if bone.name in neutral['bones'] else ()),
            'properties': _properties(pb, allowed_properties.get(bone.name, ()), rig),
            'constraints': [[_rna(c, rig), [_rna(t, rig) for t in getattr(c, 'targets', ())]] for c in pb.constraints],
            'display_transform': pb.custom_shape_transform.name if pb.custom_shape_transform else None,
        })
    return sig._hash({
        'bones': bones, 'properties': _properties(rig, (modes.STATE_KEY, POSE_LIBRARY_KEY), rig),
        'data_properties': _properties(rig.data, id_owner=rig.data),
        # Armature dimensions are derived from the currently visible bones.
        # KEY/BONE display changes can alter them without changing the rig.
        # Assigning dimensions changes scale, still guarded by basis/world.
        'object': _rna(rig, rig, omit=(*_FIELDS, 'rotation_mode', 'dimensions')),
        'basis': sig._plain(rig.matrix_basis), 'world': sig._plain(rig.matrix_world),
        'parent': _identity(rig.parent), 'parent_inverse': sig._plain(rig.matrix_parent_inverse),
        'constraints': [_rna(c, rig) for c in rig.constraints],
        'animation': _animation(rig), 'data_animation': _animation(rig.data),
        'collections': [[c.name, c.parent.name if c.parent else None, sorted(b.name for b in c.bones)]
                        for c in rig.data.collections_all],
        'pose_position': rig.data.pose_position,
    })


def _supported(source, rig):
    _require(bpy.context.mode in {'OBJECT', 'POSE'}, '先退出网格或骨架编辑模式')
    for obj, kind in ((source, 'MESH'), (rig, 'ARMATURE')):
        _require(isinstance(obj, bpy.types.Object) and obj.type == kind
                 and obj.name in bpy.context.scene.objects and obj.name in bpy.context.view_layer.objects
                 and not obj.library and not obj.override_library and not obj.data.library
                 and not obj.data.override_library and obj.is_editable and obj.data.is_editable,
                 '明确选择当前场景/视图层内可编辑的本地源网格与C94骨架')
        matrix = np.asarray(obj.matrix_world, dtype=float)
        _require(np.isfinite(matrix).all() and abs(np.linalg.det(matrix[:3, :3])) > 1e-12,
                 '对象世界矩阵必须有限可逆')
    _require(source.mode == 'OBJECT' and rig.mode in {'OBJECT', 'POSE'}, '源网格须为对象模式，骨架不能处于编辑模式')
    _require(source.data.users == 1 and sum(o.type == 'MESH' and o.data == source.data for o in bpy.data.objects) == 1,
             '源Mesh必须本地单用户，请先隔离副本')
    _require(len(source.data.vertices) > 0 and not source.constraints
             and source.animation_data is None and source.data.animation_data is None,
             '源网格不能为空，且不支持Object/Mesh动画或对象约束')
    _require([m.type for m in source.modifiers] in (['ARMATURE'], ['ARMATURE', 'SUBSURF']),
             '仅支持唯一普通Armature，其后可有一个Subsurf')
    arm = source.modifiers[0]
    _require(arm.object == rig and arm.show_viewport and arm.use_vertex_groups
             and not arm.use_bone_envelopes and not arm.use_deform_preserve_volume
             and not arm.use_multi_modifier and not arm.vertex_group,
             '需要明确同rig、启用组权重的普通LBS Armature，不支持DQ/mask/envelope')
    metadata = pose_switching._metadata(rig)
    _require(rig.data.pose_position == 'POSE', '骨架须显示POSE求值')
    keys = source.data.shape_keys
    _require(not source.show_only_shape_key, '请关闭仅显示活动形态键')
    if keys:
        _require(keys.use_relative and keys.animation_data is None and not keys.library and not keys.override_library,
                 '只支持本地无动画/driver的普通relative Keys')
        _require(all(not k.vertex_group and math.isfinite(k.value)
                     and (k == keys.reference_key or k.value == 0) for k in keys.key_blocks),
                 '所有非Basis Key必须为0，且不能使用Key顶点组遮罩')
        _require(all(len(k.data) == len(source.data.vertices) and k.relative_key in keys.key_blocks[:]
                     for k in keys.key_blocks), '已有Key拓扑或相对关系无效')
    output_names = set(metadata['deforms'])
    used = set()
    for vertex in source.data.vertices:
        for value in vertex.groups:
            _require(math.isfinite(value.weight) and value.weight >= 0, '权重必须有限非负')
            group = source.vertex_groups[value.group]
            bone = rig.data.bones.get(group.name)
            if value.weight > 0 and bone and bone.use_deform:
                _require(group.name in output_names, '实际权重包含非最终C94输出骨')
                used.add(group.name)
    _require(bool(used), '源网格没有实际C94最终输出骨权重')
    scale = float(bpy.context.scene.unit_settings.scale_length)
    _require(math.isfinite(scale) and scale > 0, '场景单位比例必须有限且为正')
    return sorted(used), scale


def _basis(source):
    if source.data.shape_keys:
        points = source.data.shape_keys.reference_key.data
        result = np.empty((len(points), 3), dtype=np.float32)
        points.foreach_get('co', result.ravel())
        return result
    return sig._coordinates(source.data)


def _source_state(source):
    return sig._hash({
        'geometry': sig._mesh_signature(source.data), 'weights': sig._weights(source),
        'locks': [g.lock_weight for g in source.vertex_groups],
        'keys': skinning_policy._shape_state(source),
        'key_identity': _identity(source.data.shape_keys),
        'modifiers': [_rna(m) for m in source.modifiers],
        'world': sig._plain(source.matrix_world), 'basis': sig._plain(source.matrix_basis),
        'parent': _identity(source.parent), 'parent_type': source.parent_type,
        'parent_bone': source.parent_bone, 'parent_inverse': sig._plain(source.matrix_parent_inverse),
        'properties': sig._plain(dict(source.items())),
    })


def _discard(obj, *, mesh=None):
    """Release our object and tracked Mesh even if a handler removed the ID."""
    if mesh is None and obj is not None:
        try:
            mesh = obj.data
        except ReferenceError:
            return
    try:
        if obj is not None:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except ReferenceError:
                pass  # Already deleted; still release the tracked Mesh below.
    finally:
        if mesh is not None:
            try:
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            except ReferenceError:
                pass


def _raw_points(source):
    """Native B-Bone evaluation on an owned Mesh copy; source flags stay intact."""
    clone = mesh = None
    try:
        clone = source.copy()
        mesh = source.data.copy()
        clone.data = mesh
        clone.parent = None
        clone.matrix_world = source.matrix_world.copy()
        clone.hide_render = True
        clone.hide_viewport = False
        for modifier in clone.modifiers:
            if modifier.type == 'SUBSURF':
                modifier.show_viewport = False
        bpy.context.scene.collection.objects.link(clone)
        clone.hide_set(False)
        bpy.context.view_layer.update()
        evaluated = clone.evaluated_get(bpy.context.evaluated_depsgraph_get())
        output = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=bpy.context.evaluated_depsgraph_get())
        try:
            _require(len(output.vertices) == len(source.data.vertices), 'RAW原生求值改变顶点数')
            _require(sig._buffer(output.loops, 'vertex_index', 1, np.int32) ==
                     sig._buffer(source.data.loops, 'vertex_index', 1, np.int32)
                     and sig._buffer(output.polygons, 'loop_total', 1, np.int32) ==
                     sig._buffer(source.data.polygons, 'loop_total', 1, np.int32),
                     'RAW原生求值拓扑不一致')
            points = sig._coordinates(output)
            _require(np.isfinite(points).all(), 'RAW原生求值含非有限坐标')
            return points
        finally:
            evaluated.to_mesh_clear()
    finally:
        if clone is not None:
            bpy.data.objects.remove(clone, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _error(session, a, b):
    delta = (np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)) @ session._world3.T
    return float(np.linalg.norm(delta, axis=1).max(initial=0) * session._scale)


@contextmanager
def _neutralized(session):
    rig = session.rig
    before = modes._snapshot(rig)
    modes._validate_inputs(rig, before['inputs'], session._neutral)
    modes._preflight(rig, session._neutral, mode=session._neutral_mode)
    try:
        modes._apply_inputs(rig, session._neutral)
        modes._set_mode(rig, session._neutral_mode)
        modes._update(rig)
        yield
    finally:
        modes._apply_inputs(rig, before['inputs'])
        modes._set_mode(rig, before['mode'])
        if modes._visibility(rig) != before['visibility']:
            modes._visibility_preflight(rig, before['visibility'])
            modes._apply_visibility(rig, before['visibility'])
        modes._update(rig)
        _require(modes._snapshot(rig) == before, '面部临时中立恢复不一致，请保留当前工程并检查输入')


class Session:
    """Opaque RAM-only capture; use module functions, never persist this object."""
    def __init__(self, source, rig, neutral, scale):
        self.token = uuid.uuid4().hex
        self.source, self.rig = source, rig
        self._scene, self._view_layer = bpy.context.scene, bpy.context.view_layer
        self._mesh, self._rig_data = source.data, rig.data
        self._neutral = copy.deepcopy(neutral)
        self._neutral_mode = modes._mode(rig)
        self._scale = scale
        self._frame = (self._scene.frame_current, self._scene.frame_subframe)
        self._world3 = np.asarray(source.matrix_world, dtype=np.float64)[:3, :3].copy()
        self._basis = _basis(source).copy()
        self._neutral_raw = None
        self._source = self._rig = None
        self._samples = {}
        self._done = False
        self.report = {}

    def _check(self):
        _require(not self._done and _SESSIONS.get(self.token) is self, '面部捕获会话已结束或未在本进程中建立')
        try:
            _require(bpy.context.scene == self._scene and bpy.context.view_layer == self._view_layer
                     and self.source.data == self._mesh and self.rig.data == self._rig_data,
                     'Scene/ViewLayer或源/rig数据身份改变，请重新开始')
            _, scale = _supported(self.source, self.rig)
            _require(scale == self._scale and
                     (self._scene.frame_current, self._scene.frame_subframe) == self._frame,
                     '帧或单位比例已改变，请重新开始')
            modes._rig(self.rig)
            _require(modes._read(self.rig)['neutral'] == self._neutral, '面部中立定义已改变，请重新开始')
            _require(_source_state(self.source) == self._source, '源几何/权重/Key/堆栈或绑定发生变化，请重新开始')
            _require(_rig_state(self.rig, self._neutral) == self._rig,
                     'rig结构、驱动、动画或非面部输入发生变化，请重新开始')
        except ReferenceError as exc:
            raise ValueError('源或骨架已被删除，会话失效') from exc


def begin(source, rig):
    """Capture a declared, measured neutral; source and rig remain unchanged."""
    names, scale = _supported(source, rig)
    modes._rig(rig)
    neutral = modes._read(rig)['neutral']
    _require(not modes._conflicts(modes._inputs(rig), neutral), '先回到显式记录的面部中立输入再开始')
    session = Session(source, rig, neutral, scale)
    source_before = _source_state(source)
    rig_before = _rig_state(rig, neutral)
    with _neutralized(session):
        # Head/tail matrices alone do not certify B-Bone neutral deformation.
        evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        bone_diagnostics = []
        for name in names:
            pb, bone = evaluated.pose.bones[name], rig.data.bones[name]
            delta = np.asarray(pb.matrix @ bone.matrix_local.inverted(), dtype=np.float64)
            endpoint_error = max((rig.matrix_world.to_3x3() @ (a - b)).length * scale
                                 for a, b in ((pb.head, bone.head_local), (pb.tail, bone.tail_local)))
            linear_error = float(np.max(abs(delta[:3, :3] - np.eye(3))))
            _require(np.isfinite(delta).all() and endpoint_error <= TOLERANCE_METERS,
                     '参与输出骨不在C94校准中立：' + name
                     + '; endpoint_error_meters=' + format(endpoint_error, '.9g')
                     + '; linear_error=' + format(linear_error, '.9g'))
            # A dimensionless matrix entry is not a distance bound. Finite
            # endpoint checks, the full RAW/Basis check below and per-Key
            # native replay certify only these measured vertices and samples.
            bone_diagnostics.append({'bone': name, 'linear_error': linear_error,
                                     'endpoint_error_meters': endpoint_error})
        session._neutral_raw = _raw_points(source)
        error = _error(session, session._neutral_raw, session._basis)
        _require(error <= TOLERANCE_METERS, 'RAW中立与Basis误差超过1e-5米，不能使用直接位移捕获')
    _require(_source_state(source) == source_before and _rig_state(rig, neutral) == rig_before,
             '开始捕获后源或非面部状态不一致')
    session._source, session._rig = source_before, rig_before
    session.report = {'schema': 'c94.face-capture.v1', 'source': source.name, 'rig': rig.name,
                      'neutral_error_meters': error, 'sample_count': 0,
                      'neutral_bone_diagnostics': bone_diagnostics,
                      'neutral_mode': session._neutral_mode, 'keys_default_zero': True,
                      'method': 'native_raw_pose_minus_neutral_on_measured_raw_neutral',
                      'output_semantics': 'Key targets are captured native BONE-mode outputs; replay uses captured neutral mode; KEY/BONE baselines are not certified anatomically equivalent',
                      'limitations': ['not inverse skinning', 'Key combinations are not bone-combination certification',
                                      'no file-restored session authority', 'not complete facial authorship']}
    register()
    _SESSIONS[session.token] = session
    return session


def get_session(token):
    _require(isinstance(token, str) and token in _SESSIONS, '没有活动面部捕获会话；保存/换文件/卸载后请重新开始')
    return _SESSIONS[token]


def _sample_payload(session, name):
    """Read-only shared sample rules; caller checks state before/after batch."""
    _require(isinstance(name, str) and name.strip() and len(name.strip().encode('utf-8')) <= 63,
             '请提供不超过63 UTF-8字节的非空表情Key名称')
    name = name.strip()
    existing = {k.name for k in session.source.data.shape_keys.key_blocks} if session.source.data.shape_keys else {'Basis'}
    _require(name not in existing and name not in session._samples, '表情名称已存在；每个采样名称必须唯一')
    _require(modes._mode(session.rig) == 'BONE', '请显式切到骨控模式，再摆出并采样面部姿态')
    points = _raw_points(session.source)
    changed = _error(session, points, session._neutral_raw)
    return name, points, changed


def sample(session, name):
    """Read one current BONE-mode native pose without writing source inputs."""
    _require(isinstance(session, Session), '需要begin返回的内存Session')
    session._check()
    name, points, changed = _sample_payload(session, name)
    _require(changed > 0, '实际RAW网格未改变，不记录空表情')
    session._check()
    session._samples[name] = points.copy()
    session.report['sample_count'] = len(session._samples)
    return {'name': name, 'sample_count': len(session._samples), 'max_displacement_meters': changed}


def create_candidate(session, *, name=None):
    """Create new Keys on an independent Mesh, replay natively, then end session."""
    _require(isinstance(session, Session), '需要begin返回的内存Session')
    session._check()
    _require(bool(session._samples), '至少采样一个实际改变的面部表情')
    if name is None:
        prefix = session.source.name
        suffix = '__面部Key候选_' + session.token[:8]
        while len((prefix + suffix).encode('utf-8')) > 63:
            prefix = prefix[:-1]
        label = prefix + suffix
    else:
        label = name
    _require(isinstance(label, str) and label.strip() and len(label.encode('utf-8')) <= 63
             and label not in bpy.data.objects, '候选名称须非空、不重复且不超过63 UTF-8字节')
    candidate = mesh = None
    try:
        candidate = session.source.copy()
        mesh = session.source.data.copy()
        candidate.data = mesh
        candidate.name = label
        _require(candidate.name == label, '候选对象名称被截断，请使用更短名称')
        _require(not mesh.shape_keys or mesh.shape_keys != session.source.data.shape_keys,
                 '候选未隔离原Key数据，拒绝写入')
        bpy.context.scene.collection.objects.link(candidate)
        old_keys = skinning_policy._shape_state(session.source)
        if mesh.shape_keys is None:
            basis = candidate.shape_key_add(name='Basis', from_mix=False)
        else:
            basis = mesh.shape_keys.reference_key
        created = []
        for key_name, points in session._samples.items():
            key = candidate.shape_key_add(name=key_name, from_mix=False)
            _require(key.name == key_name, '新Key名称发生冲突或截断')
            key.relative_key = basis
            positions = (session._basis.astype(np.float64) + points.astype(np.float64)
                         - session._neutral_raw.astype(np.float64)).astype(np.float32)
            key.data.foreach_set('co', positions.ravel())
            key.value = 0
            created.append(key)
        replay = {}
        with _neutralized(session):
            for key in created:
                key.value = 1
                mesh.update()
                candidate.update_tag(refresh={'OBJECT', 'DATA'})
                bpy.context.view_layer.update()
                try:
                    actual = _raw_points(candidate)
                    replay[key.name] = _error(session, actual, session._samples[key.name])
                    _require(replay[key.name] <= TOLERANCE_METERS,
                             'Key原生中立重放误差超过1e-5米：' + key.name)
                finally:
                    key.value = 0
                    mesh.update()
                    candidate.update_tag(refresh={'OBJECT', 'DATA'})
                    bpy.context.view_layer.update()
        # Mesh.copy must retain every pre-existing Key and its relationships.
        if old_keys:
            current = skinning_policy._shape_state(candidate)
            _require(current[:2] == old_keys[:2] and current[2][:len(old_keys[2])] == old_keys[2],
                     '候选改变了已有Key坐标、关系或设置')
        _require(sig._mesh_signature(mesh) == sig._mesh_signature(session.source.data)
                 and sig._weights(candidate) == sig._weights(session.source)
                 and [g.lock_weight for g in candidate.vertex_groups] ==
                     [g.lock_weight for g in session.source.vertex_groups]
                 and [_rna(m) for m in candidate.modifiers] == [_rna(m) for m in session.source.modifiers],
                 '候选改变了原几何/UV/权重/材质或修改器')
        session._check()
        report = dict(session.report, samples=list(session._samples), replay_error_meters=replay,
                      candidate=candidate.name, keys_default_zero=True)
        candidate['c94_face_capture_candidate_v1'] = json.dumps(report, ensure_ascii=False, allow_nan=False)
        session.report = report
        cancel(session)
        return candidate
    except Exception as exc:
        try:
            _discard(candidate, mesh=mesh)
        except Exception as cleanup:
            raise RuntimeError(str(exc) + '；候选清理失败：' + str(cleanup)) from exc
        raise


def cancel(session):
    """Release only this RAM session; no source or completed candidate is deleted."""
    _require(isinstance(session, Session), '需要begin返回的内存Session')
    if _SESSIONS.get(session.token) is session:
        del _SESSIONS[session.token]
    session._done = True
    session._samples.clear()
    return {'cancelled': True, 'files_or_candidates_deleted': False}


@persistent
def _clear_sessions(_dummy=None):
    for session in tuple(_SESSIONS.values()):
        cancel(session)


def register():
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre):
        if _clear_sessions not in handlers:
            handlers.append(_clear_sessions)


def unregister():
    _clear_sessions()
    for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre):
        if _clear_sessions in handlers:
            handlers.remove(_clear_sessions)
