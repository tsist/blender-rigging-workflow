# SPDX-License-Identifier: GPL-3.0-or-later
"""Native surface residuals: world-metre samples -> inverse LBS -> relative Keys.

This is Mesh Key animation, not bone animation or a physics solver. A separate
owner captures/simulates trajectories and drives the preview carrier clock.
"""
import json
import math

import bpy
import numpy as np

from . import corrective_authoring as sig

STATE_KEY = 'c94_surface_bake_v1'
CLOCK = '时钟'
FROZEN = '冻结'
MAX_NUMERIC_BYTES = 256 * 1024 * 1024
MAX_KEYS = 4096
MAX_CONDITION = 1.0 / (64 * np.finfo(np.float32).eps)


def _require(value, message):
    if not value:
        raise ValueError(message)


def _scale():
    scale = float(bpy.context.scene.unit_settings.scale_length)
    _require(math.isfinite(scale) and scale > 0, '场景米制比例必须有限且为正')
    return scale


def _supported(obj, rig):
    _require(bpy.context.mode == 'OBJECT', '表面残差捕获需要对象模式')
    _require(isinstance(obj, bpy.types.Object) and obj.type == 'MESH'
             and isinstance(rig, bpy.types.Object) and rig.type == 'ARMATURE',
             '明确指定源Mesh与其Armature')
    _require(bpy.context.view_layer.objects.get(obj.name) == obj
             and bpy.context.view_layer.objects.get(rig.name) == rig,
             '源Mesh与骨架须在当前视图层中')
    _require(not obj.library and not obj.override_library and not obj.data.library
             and not obj.data.override_library, '需要本地Mesh工作副本')
    _require(obj.data.shape_keys is None and obj.animation_data is None
             and obj.data.animation_data is None and not obj.constraints,
             '表面残差源须无Key、对象/网格动画和对象约束；保留原源另建输入')
    _require([m.type for m in obj.modifiers] in (['ARMATURE'], ['ARMATURE', 'SUBSURF']),
             '源只支持唯一Armature后可选一个Subsurf；Cloth在独立模拟输入中求解')
    arm = obj.modifiers[0]
    _require(arm.object == rig and arm.show_viewport and arm.use_vertex_groups
             and not arm.use_bone_envelopes and not arm.use_deform_preserve_volume
             and not arm.use_multi_modifier and not arm.vertex_group,
             '仅支持启用的普通组权重LBS，不接DQ、envelope、mask或multi modifier')
    _require(rig.data.pose_position == 'POSE' and len(obj.data.vertices) > 0,
             '需要POSE骨架和非空RAW网格')
    return arm


def _static(obj, rig):
    """No current pose/frame, evaluated rig matrices or group mix values here."""
    _supported(obj, rig)
    return {'object': obj.session_uid, 'mesh': obj.data.session_uid,
            'rig': rig.session_uid, 'rig_data': rig.data.session_uid,
            'scene': bpy.context.scene.session_uid,
            'view_layer': bpy.context.view_layer.name, 'scale': _scale(),
            'mesh_signature': sig._mesh_signature(obj.data),
            'weights': sig._hash([sig._weights(obj), [g.lock_weight for g in obj.vertex_groups]]),
            'modifiers': [sig._rna(m) for m in obj.modifiers],
            'parent': obj.parent.session_uid if obj.parent else None,
            'parent_type': obj.parent_type, 'parent_bone': obj.parent_bone,
            'parent_inverse': sig._plain(obj.matrix_parent_inverse),
            'object_basis': sig._plain(obj.matrix_basis),
            'rest': sig._hash([[b.name, b.parent.name if b.parent else None,
                               sig._plain(b.matrix_local), b.use_deform, b.bbone_segments]
                              for b in rig.data.bones])}


def _remove_object(obj, mesh=None, key=None):
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    if key is not None:
        try:
            if key.users == 0:
                bpy.data.batch_remove(ids=[key])
        except ReferenceError:
            pass  # Mesh removal may already have freed its owned Key.


def _raw_oracle(obj, matrices):
    """Read real Armature output on an owned copy, never toggle source modifiers."""
    clone = mesh = None
    try:
        clone = obj.copy()
        mesh = obj.data.copy()
        clone.data = mesh
        bpy.context.scene.collection.objects.link(clone)
        clone.hide_set(False)
        clone.hide_viewport = False
        for modifier in clone.modifiers:
            if modifier.type == 'SUBSURF':
                modifier.show_viewport = False
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        evaluated = clone.evaluated_get(graph)
        result = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
        try:
            _require(len(result.vertices) == len(mesh.vertices), '原生RAW Armature改变顶点域')
            actual = np.column_stack((sig._coordinates(result), np.ones(len(result.vertices))))
            actual = (np.asarray(evaluated.matrix_world) @ actual.T).T[:, :3] * _scale()
        finally:
            evaluated.to_mesh_clear()
        basis = np.column_stack((sig._coordinates(obj.data), np.ones(len(obj.data.vertices))))
        predicted = np.einsum('nij,nj->ni', matrices, basis)[:, :3]
        error = float(np.max(np.linalg.norm(actual-predicted, axis=1)))
        tolerance = 32 * np.finfo(np.float32).eps * max(1., float(np.max(np.abs(actual))))
        _require(np.isfinite(actual).all() and error <= tolerance,
                 '解析LBS未匹配实际RAW Armature世界输出；拒绝残差逆解')
    finally:
        _remove_object(clone, mesh)


def capture_affines(obj, rig):
    """Return readonly (N,4,4): RAW Mesh-local -> current world metres.

    Positive deform-weight mass is normalized as native ordinary LBS does.
    Points with no positive deform mass retain the native undeformed transform.
    """
    _supported(obj, rig)
    before = _static(obj, rig)
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    erig = rig.evaluated_get(graph)
    world = np.asarray(obj.evaluated_get(graph).matrix_world, dtype=np.float64)
    rw = np.asarray(erig.matrix_world, dtype=np.float64)
    _require(np.isfinite(world).all() and np.isfinite(rw).all(), '对象/骨架世界矩阵必须有限')
    try:
        into = np.linalg.solve(rw, world)
    except np.linalg.LinAlgError as exc:
        raise ValueError('骨架世界矩阵不可逆') from exc
    count = len(obj.data.vertices)
    _require(count * 16 * 8 < MAX_NUMERIC_BYTES, 'LBS仿射捕获超过256MiB数值预算')
    by_index = {g.index: g.name for g in obj.vertex_groups}
    used = {}
    masses = np.zeros(count, dtype=np.float64)
    for vertex in obj.data.vertices:
        for group in vertex.groups:
            weight = float(group.weight)
            _require(math.isfinite(weight) and weight >= 0, '源权重必须有限且非负')
            name = by_index.get(group.group)
            bone = erig.data.bones.get(name) if name else None
            if weight > 0 and bone is not None and bone.use_deform:
                _require(bone.bbone_segments == 1, '实际正权参与的B-Bone必须为single segment')
                if name not in used:
                    used[name] = []
                used[name].append((vertex.index, weight))
                masses[vertex.index] += weight
    blend = np.zeros((count, 4, 4), dtype=np.float64)
    for name, rows in used.items():
        bone = erig.data.bones[name]
        pose = erig.pose.bones[name]
        try:
            deformation = np.asarray(pose.matrix) @ np.linalg.inv(np.asarray(bone.matrix_local))
        except np.linalg.LinAlgError as exc:
            raise ValueError('参与骨静置矩阵不可逆') from exc
        indices = np.asarray([i for i, _w in rows], dtype=np.int64)
        weights = np.asarray([w for _i, w in rows]) / masses[indices]
        blend[indices] += weights[:, None, None] * deformation
    blend[masses == 0] = np.eye(4)
    result = np.einsum('ij,njk,kl->nil', rw, blend, into)
    result[:, :3, :] *= _scale()
    _require(np.isfinite(result).all(), 'LBS仿射输出包含非有限值')
    _raw_oracle(obj, result)
    _require(_static(obj, rig) == before, '仿射捕获期间源几何/权重/堆栈或身份改变')
    result.setflags(write=False)
    return result


def _controls(rig, enabled, mix):
    settings = rig.pose.bones.get('骨架设置')
    _require(settings is not None and isinstance(enabled, str) and isinstance(mix, str)
             and enabled != mix and enabled in settings and mix in settings,
             '先在骨架设置建立明确的组启用/混合原生属性')
    _require(all(type(settings[n]) in (bool, int, float) and math.isfinite(settings[n])
                 for n in (enabled, mix)), '组启用/混合须为有限原生标量')
    return settings


def _property_path(owner, name):
    return owner.path_from_id() + '[' + json.dumps(name, ensure_ascii=False) + ']'


def _value_driver(block, carrier, rig, scene, settings, enabled, mix, time, step, lo, hi):
    curve = block.driver_add('value')
    # Shape Key driver_add creates a 0..1 mapping whose endpoint epsilon
    # snaps small expression values. No mapping means direct expression output.
    while curve.keyframe_points:
        curve.keyframe_points.remove(curve.keyframe_points[-1], fast=True)
    curve.update()
    driver = curve.driver
    driver.type = 'SCRIPTED'
    driver.expression = ('min(max(e,0),1)*min(max(m,0),1)*max(0,1-abs((t-(%r))/(%r)))'
                         '*(z or (%r<=frame and frame<=%r))' % (time, step, lo, hi))
    for name, owner, path, kind in (
            ('t', carrier, '[' + json.dumps(CLOCK, ensure_ascii=False) + ']', 'OBJECT'),
            ('z', carrier, '[' + json.dumps(FROZEN, ensure_ascii=False) + ']', 'OBJECT'),
            ('e', rig, _property_path(settings, enabled), 'OBJECT'),
            ('m', rig, _property_path(settings, mix), 'OBJECT')):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        target = variable.targets[0]
        target.id_type = kind
        target.id = owner
        target.data_path = path
    return curve


class Prepared:
    """Live transaction. Geometry arrays are readonly; no disk token restores it."""
    def __init__(self, obj, rig, times, coordinates, *, label, enabled, mix, validate_source):
        self.source, self.rig, self.scene = obj, rig, bpy.context.scene
        self.times, self.coordinates = times, coordinates
        self.label, self.enable_property, self.mix_property = label, enabled, mix
        self.validate_source = validate_source
        self._before = _static(obj, rig)
        self.object = self.carrier = self.action = None
        self._mesh = self._key = None
        self._output_before = None

    def _check(self):
        _require(bpy.context.scene == self.scene and _static(self.source, self.rig) == self._before,
                 '残差源几何/权重/堆栈/Key身份或静态输入改变，请重新捕获')
        _controls(self.rig, self.enable_property, self.mix_property)
        if self.validate_source is not None:
            _require(self.validate_source() is not False, '基础动作或时序总体签名改变，请重新捕获')

    def _output_state(self):
        _require(self.object is not None and self.carrier is not None
                 and self.scene.objects.get(self.object.name) == self.object
                 and self.scene.objects.get(self.carrier.name) == self.carrier
                 and self.object.data == self._mesh and self._mesh.shape_keys == self._key,
                 '本次残差对象/时钟或Mesh/Key身份改变')
        return {'mesh': sig._mesh_signature(self._mesh), 'weights': sig._weights(self.object),
                'modifiers': [sig._rna(m) for m in self.object.modifiers],
                'keys': [[k.name, k.relative_key.name, sig._buffer(k.data, 'co', 3)]
                         for k in self._key.key_blocks],
                'drivers': sig._drivers(self._key)}

    def _discard(self):
        action = self.action
        self.action = None
        if self.carrier is not None:
            bpy.data.objects.remove(self.carrier, do_unlink=True)
        self.carrier = None
        _remove_object(self.object, self._mesh, self._key)
        self.object = self._mesh = self._key = None
        self._output_before = None
        if action is not None:
            action.use_fake_user = False
            if action.users == 0:
                bpy.data.actions.remove(action)

    def preview(self):
        """Create the owned Key residual object and unanimated native clock."""
        self._check()
        if self.object is not None:
            _require(self._output_state() == self._output_before, '已有残差输出改变，保留用户编辑并重新准备')
            return {'object': self.object, 'carrier': self.carrier, 'kind': 'mesh_key_residual'}
        _require(self.label not in bpy.data.objects and self.label+'__时钟' not in bpy.data.objects,
                 '残差对象/时钟名称已使用，不覆盖已有对象')
        try:
            obj = self.source.copy()
            self.object = obj
            self._mesh = self.source.data.copy()
            obj.data = self._mesh
            obj.name = self.label
            self.scene.collection.objects.link(obj)
            obj.hide_set(False)
            obj.hide_viewport = False
            obj.show_only_shape_key = False
            basis = obj.shape_key_add(name='Basis', from_mix=False)
            self._key = obj.data.shape_keys
            self._key.use_relative = True
            carrier = bpy.data.objects.new(self.label+'__时钟', None)
            self.carrier = carrier
            self.scene.collection.objects.link(carrier)
            carrier.hide_render = True
            carrier.empty_display_size = .05
            carrier[CLOCK] = float(self.times[0])
            carrier[FROZEN] = False
            settings = _controls(self.rig, self.enable_property, self.mix_property)
            for index, (time, coordinates) in enumerate(zip(self.times, self.coordinates)):
                block = obj.shape_key_add(name='残差_%04d' % index, from_mix=False)
                block.relative_key = basis
                block.data.foreach_set('co', coordinates.ravel())
                block.value = 0.
                _value_driver(block, carrier, self.rig, self.scene, settings,
                              self.enable_property, self.mix_property, float(time),
                              float(self.times[1]-self.times[0]),
                              float(self.times[0]), float(self.times[-1]))
            obj.active_shape_key_index = 0
            record = {'schema': 'c94.surface-key-residual.v1', 'source': self.source.name,
                      'rig': self.rig.name, 'carrier': carrier.name, 'samples': len(self.times),
                      'start': float(self.times[0]), 'end': float(self.times[-1]),
                      'kind': 'mesh_key_residual', 'bone_bake': False}
            obj[STATE_KEY] = json.dumps(record, ensure_ascii=False, allow_nan=False)
            carrier[STATE_KEY] = obj.name
            _require(sig._mesh_signature(self._mesh) == sig._mesh_signature(self.source.data)
                     and sig._weights(obj) == sig._weights(self.source)
                     and [sig._rna(m) for m in obj.modifiers] == [sig._rna(m) for m in self.source.modifiers],
                     '残差副本改变了源Basis/UV/材质/权重或修改器')
            bpy.context.view_layer.update()
            self._check()
            self._output_before = self._output_state()
            return {'object': obj, 'carrier': carrier, 'kind': 'mesh_key_residual'}
        except Exception:
            self._discard()
            raise

    def bake(self, *, name):
        """Animate the owned clock in a fresh native Action; no source rig keys."""
        self._check()
        _require(self.action is None and isinstance(name, str) and name.strip()
                 and name not in bpy.data.actions, '明确指定未使用的新Action名称；不覆盖已有动画')
        self.preview()
        _require(self.carrier.animation_data is None, '本次预览时钟已有外部动画，保留并重新准备')
        try:
            action = bpy.data.actions.new(name)
            self.action = action
            action.use_fake_user = True
            slot = action.slots.new(id_type='OBJECT', name=self.carrier.name)
            strip = action.layers.new('表面Key残差时钟').strips.new(type='KEYFRAME')
            bag = strip.channelbag(slot, ensure=True)
            curve = bag.fcurves.new('[' + json.dumps(CLOCK, ensure_ascii=False) + ']', index=0)
            curve.keyframe_points.add(2)
            for point, time in zip(curve.keyframe_points, (self.times[0], self.times[-1])):
                point.co = (float(time), float(time))
                point.interpolation = 'LINEAR'
            curve.extrapolation = 'CONSTANT'
            curve.update()
            ad = self.carrier.animation_data_create()
            ad.action = action
            ad.action_slot = slot
            self.carrier[FROZEN] = False
            self.carrier.update_tag(refresh={'OBJECT'})
            bpy.context.view_layer.update()
            self._check()
            _require(self._output_state() == self._output_before, '建时钟Action改变了残差几何或驱动')
            return {'object': self.object, 'carrier': self.carrier, 'action': action,
                    'kind': 'mesh_key_residual', 'bone_bake': False}
        except Exception:
            self._discard()
            raise


def prepare(obj, rig, times, world_positions, affines, *, label,
            enable_property, mix_property, validate_source=None):
    """Inverse each actual source-time LBS; never freeze the current rig pose.

    Samples are uniform source frames including both endpoints. Position units
    are world metres. Budget covers retained/copy numeric inputs and Key coords;
    native Blender allocations still require a bounded source/clip selection.
    """
    _supported(obj, rig)
    _controls(rig, enable_property, mix_property)
    _require(isinstance(label, str) and label.strip(), '明确填写独立残差对象名称')
    _require(validate_source is None or callable(validate_source), 'validate_source须为无参检查回调')
    times = np.asarray(times)
    positions = np.asarray(world_positions)
    matrices = np.asarray(affines)
    count = len(obj.data.vertices)
    _require(times.ndim == 1 and 2 <= len(times) <= MAX_KEYS
             and positions.shape == (len(times), count, 3)
             and matrices.shape == (len(times), count, 4, 4),
             '需要2..4096个uniform时刻与匹配(F,N,3)世界米制坐标/(F,N,4,4)仿射')
    _require(all(array.dtype.kind in 'fiu' for array in (times, positions, matrices)),
             '采样输入须为实数数值数组')
    # Check before dtype expansion, solve buffers and float32 Key allocation.
    estimate = positions.nbytes + matrices.nbytes + len(times)*count*((3+16+3)*8+3*4) + count*3*8
    _require(estimate <= MAX_NUMERIC_BYTES, '表面残差数值数据超过256MiB预算；缩小片段或源点域')
    times = np.asarray(times, dtype=np.float64)
    positions = np.asarray(positions, dtype=np.float64)
    matrices = np.asarray(matrices, dtype=np.float64)
    _require(np.isfinite(times).all() and np.isfinite(positions).all() and np.isfinite(matrices).all(),
             '采样时刻/世界坐标/LBS仿射必须全部有限')
    delta = np.diff(times)
    _require(np.all(delta > 0) and np.allclose(delta, delta[0], rtol=1e-7, atol=1e-8),
             '采样时刻必须严格递增且uniform，包含片段首末帧')
    _require(np.allclose(matrices[:, :, 3, :], (0., 0., 0., 1.), rtol=0, atol=1e-8),
             'LBS仿射末行必须为[0,0,0,1]')
    linear = matrices[:, :, :3, :3]
    condition = np.linalg.cond(linear)
    _require(np.isfinite(condition).all() and np.all(condition < MAX_CONDITION),
             'LBS仿射奇异或条件数不适合float32 Key残差逆解')
    try:
        local = np.linalg.solve(linear, (positions-matrices[:, :, :3, 3])[..., None])[..., 0]
    except np.linalg.LinAlgError as exc:
        raise ValueError('LBS仿射不可逆') from exc
    coordinates = local.astype(np.float32)
    _require(np.isfinite(coordinates).all(), '逆解的RAW Key坐标超出float32可用范围')
    frozen_times = times.copy()
    frozen_times.setflags(write=False)
    coordinates.setflags(write=False)
    result = Prepared(obj, rig, frozen_times, coordinates, label=label.strip(),
                      enabled=enable_property, mix=mix_property, validate_source=validate_source)
    result._check()
    return result
