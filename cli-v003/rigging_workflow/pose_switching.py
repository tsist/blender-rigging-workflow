"""Native C94 IK/FK matching, with measured output and exception rollback.

This operates on generated rigs only. It does not change rest data, constraints,
finger curl inputs, or animation keys. The distinct three-bone leg mechanism
uses pose_switching_leg; arbitrary finger-IK-to-curl inversion is unsupported.
"""
import json
import math

import bpy
from mathutils import Matrix, Vector

RIG_KEY = 'c94_generated_rig_v1'
FINGERS = ('大拇指', '食指', '中指', '无名指', '小拇指')
_CHANNELS = ('location', 'rotation_euler', 'rotation_quaternion',
             'rotation_axis_angle', 'scale')


class PoseSwitchError(ValueError):
    """A measured matching failure; inputs have been restored before delivery."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def _update(rig):
    rig.update_tag()
    bpy.context.view_layer.update()
    return rig.evaluated_get(bpy.context.evaluated_depsgraph_get())


def _channels(bone):
    return {'rotation_mode': bone.rotation_mode,
            **{key: tuple(getattr(bone, key)) for key in _CHANNELS}}


def _restore(rig, channels, settings, key, mode):
    for name, values in channels.items():
        bone = rig.pose.bones[name]
        for field in _CHANNELS:
            if tuple(getattr(bone, field)) != values[field]:
                setattr(bone, field, values[field])
        bone.rotation_mode = values['rotation_mode']
    settings[key] = mode
    _update(rig)


def _metadata(rig):
    if (not isinstance(rig, bpy.types.Object) or rig.type != 'ARMATURE'
            or rig.library or rig.data.library or rig.mode == 'EDIT'
            or rig.name not in bpy.context.view_layer.objects):
        raise ValueError('请选择当前视图层内可编辑的生成角色骨架，结束编辑模式后再切换')
    try:
        data = json.loads(rig.get(RIG_KEY, ''))
    except (ValueError, TypeError) as exc:
        raise ValueError('当前对象没有有效的C94生成记录') from exc
    if not isinstance(data, dict) or data.get('schema') != 'c94.generated.v1':
        raise ValueError('当前对象不是受支持的C94生成角色')
    names, mapping = data.get('deforms'), data.get('mechanism_mapping')
    if (not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names)
            or len(set(names)) != len(names)
            or not isinstance(mapping, dict) or set(mapping) != set(names)
            or any(not isinstance(name, str) or name not in rig.pose.bones
                   or not isinstance(mapping[name], str) or mapping[name] not in rig.pose.bones
                   for name in names)):
        raise ValueError('生成记录中的输出骨或机制映射不完整')
    return data


def _arm(rig, side):
    suffix = '.' + side
    spec = {'mode_key': ('左' if side == 'L' else '右') + '臂FK-IK',
            'fk': tuple(n + suffix for n in ('大臂FK', '小臂FK', '手掌FK')),
            'mixed': tuple(n + suffix for n in ('机制-大臂', '机制-小臂', '手掌')),
            'ik': tuple(n + suffix for n in ('大臂IK', '小臂IK')),
            'target': '手掌IK扭转-目标' + suffix,
            'main_target': '手掌IK-目标' + suffix,
            'pole': '大臂IK-极点' + suffix,
            'fingers': tuple((finger + 'IK' + suffix,
                              finger + ('2' if finger == '大拇指' else '3') + suffix)
                             for finger in FINGERS)}
    required = (*spec['fk'], *spec['mixed'], *spec['ik'], spec['target'],
                spec['main_target'], spec['pole'], '骨架设置',
                *(name for pair in spec['fingers'] for name in pair))
    if any(name not in rig.pose.bones for name in required):
        raise ValueError('此生成骨架缺少当前手臂或五指的原生控制骨')
    settings = rig.pose.bones['骨架设置']
    value = settings.get(spec['mode_key'])
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('手臂FK/IK属性无效')
    if not (abs(value) < 1e-6 or abs(value - 1) < 1e-6):
        raise ValueError('先把手臂FK/IK滑块设到完整FK或完整IK端点，再保姿态切换')
    constraints = [c for c in rig.pose.bones[spec['ik'][1]].constraints if c.type == 'IK']
    if (len(constraints) != 1 or constraints[0].target != rig
            or constraints[0].subtarget != spec['target'] or constraints[0].chain_count != 2
            or constraints[0].pole_target != rig or constraints[0].pole_subtarget != spec['pole']
            or rig.pose.bones[spec['ik'][1]].parent != rig.pose.bones[spec['ik'][0]]
            or rig.pose.bones[spec['target']].parent != rig.pose.bones[spec['main_target']]):
        raise ValueError('原生手臂IK目标、极点或两骨链已改变，无法使用当前切换方式')
    for mixed, fk, ik in zip(spec['mixed'], spec['fk'], (*spec['ik'], '机制-手掌IK' + suffix)):
        copies = [c for c in rig.pose.bones[mixed].constraints if c.type == 'COPY_TRANSFORMS']
        if (len(copies) != 2 or {c.subtarget for c in copies} != {fk, ik}
                or any(c.target != rig or c.owner_space != 'WORLD' or c.target_space != 'WORLD'
                       or c.mix_mode != 'REPLACE' or c.mute for c in copies)):
            raise ValueError('原生手臂FK/IK混合关系已改变：' + mixed)
    for name in (spec['target'], spec['pole'], *(p[0] for p in spec['fingers'])):
        if any(not c.mute and c.influence for c in rig.pose.bones[name].constraints):
            raise ValueError('匹配目标具有额外约束：' + name)
    for target, tip in spec['fingers']:
        controls = rig.pose.bones[target]
        if not isinstance(controls.get('手指IK'), (float, int)):
            raise ValueError('手指IK属性无效：' + target)
        iks = [c for c in rig.pose.bones[tip].constraints if c.type == 'IK']
        if len(iks) != 1 or iks[0].target != rig or iks[0].subtarget != target:
            raise ValueError('手指IK目标关系已改变：' + target)
    return spec, settings, float(value)


def _plane(start, joint, end):
    axis = end - start
    length = axis.length
    if length < 1e-8:
        raise ValueError('手臂根与末端重合，无法确定切换平面')
    axis /= length
    radial = joint - start - axis * (joint - start).dot(axis)
    if radial.length / length < 1e-5:
        raise ValueError('手臂过直，无法确定稳定极点；请略微屈肘后重试')
    return axis, radial


class _Witness:
    """Short-lived native modifier samples; all owned data is removed on exit."""

    def __init__(self, rig, names):
        self.rig, self.names = rig, names
        self.object = self.mesh = None
        vertices = []
        for name in names:
            bone = rig.data.bones[name]
            radius = min(.002, bone.length * .1)
            vertices.extend(bone.matrix_local @ Vector((radius, bone.length * t, 0))
                            for t in (.2, .5, .8))
        try:
            self.mesh = bpy.data.meshes.new('C94-切换临时验证点')
            self.mesh.from_pydata(vertices, [], [])
            self.object = bpy.data.objects.new('C94-切换临时验证点', self.mesh)
            bpy.context.scene.collection.objects.link(self.object)
            self.object.matrix_world = rig.matrix_world.copy()
            self.object.hide_render = True
            for i, name in enumerate(names):
                self.object.vertex_groups.new(name=name).add(list(range(i * 3, i * 3 + 3)), 1, 'REPLACE')
            self.object.modifiers.new('C94原生切换验证', 'ARMATURE').object = rig
        except Exception:
            self.close()
            raise

    def read(self):
        er = _update(self.rig)
        em = self.object.evaluated_get(bpy.context.evaluated_depsgraph_get())
        world = self.object.matrix_world
        points = [world @ v.co for v in em.data.vertices]
        endpoints = {name: (self.rig.matrix_world @ er.pose.bones[name].head,
                            self.rig.matrix_world @ er.pose.bones[name].tail) for name in self.names}
        if (not all(math.isfinite(v) for point in points for v in point)
                or not all(math.isfinite(v) for pair in endpoints.values() for point in pair for v in point)):
            raise ValueError('切换产生非有限位置，已恢复输入')
        return points, endpoints

    def compare(self, before, after):
        points_a, bones_a = before
        points_b, bones_b = after
        distances = [(a - b).length for a, b in zip(points_a, points_b)]
        sample_index = max(range(len(distances)), key=distances.__getitem__)
        bone_errors = {name: max((a - b).length for a, b in zip(bones_a[name], bones_b[name]))
                       for name in self.names}
        worst = max(bone_errors, key=bone_errors.get)
        unit_scale = bpy.context.scene.unit_settings.scale_length
        return {'coordinate_space': 'WORLD', 'meters_per_blender_unit': unit_scale,
                'weighted_sample_count': len(distances), 'output_bone_count': len(self.names),
                'bbone_output_count': sum(self.rig.data.bones[n].bbone_segments > 1 for n in self.names),
                'weighted_max': distances[sample_index], 'weighted_max_meters': distances[sample_index] * unit_scale,
                'weighted_worst_bone': self.names[sample_index // 3],
                'weighted_worst_index': sample_index, 'endpoint_max': bone_errors[worst],
                'endpoint_max_meters': bone_errors[worst] * unit_scale, 'endpoint_worst_bone': worst,
                'sampling': 'three single-weight off-axis points per output; native Armature modifier'}

    def close(self):
        if self.object is not None:
            bpy.data.objects.remove(self.object, do_unlink=True)
            self.object = None
        if self.mesh is not None:
            bpy.data.meshes.remove(self.mesh)
            self.mesh = None


def _to_ik(rig, spec, er, align_fingers):
    desired = [er.pose.bones[name].matrix.copy() for name in spec['mixed']]
    root = er.pose.bones[spec['ik'][0]].head.copy()
    for matrix in desired[:2]:
        if any(abs(matrix.to_3x3().col[i].length - 1) > 1e-3 for i in range(3)):
            raise ValueError('当前手臂具有拉伸或非单位局部缩放，尚未验证保姿态切换')
    joint = root + desired[0].to_3x3().col[1] * rig.data.bones[spec['ik'][0]].length
    end = joint + desired[1].to_3x3().col[1] * rig.data.bones[spec['ik'][1]].length
    axis, wanted = _plane(root, joint, end)  # Fail before the first pose write.
    tips = {target: er.pose.bones[tip].tail.copy() for target, tip in spec['fingers']}
    target_matrix = desired[2].copy()
    target_matrix.translation = end
    rig.pose.bones[spec['target']].matrix = target_matrix
    er = _update(rig)
    first, last = (er.pose.bones[n] for n in spec['ik'])
    axis, actual = _plane(first.head, first.tail, last.tail)
    wanted = joint - first.head - axis * (joint - first.head).dot(axis)
    pole_matrix = er.pose.bones[spec['pole']].matrix.copy()
    relative = pole_matrix.translation - first.head
    axial = axis * relative.dot(axis)
    radial = relative - axial
    if min(radial.length, wanted.length) < 1e-8:
        raise ValueError('极点位于手臂轴上，无法稳定匹配；已恢复输入')
    offset = math.atan2(axis.dot(actual.cross(radial)), actual.dot(radial))
    radial = Matrix.Rotation(offset, 3, axis) @ wanted.normalized() * radial.length
    pole_matrix.translation = first.head + axial + radial
    rig.pose.bones[spec['pole']].matrix = pole_matrix
    _update(rig)
    if align_fingers:
        for name, tip in spec['fingers']:
            matrix = rig.pose.bones[name].matrix.copy()
            matrix.translation = tips[name]
            rig.pose.bones[name].matrix = matrix
            _update(rig)


def _to_fk(rig, spec):
    er = _update(rig)
    desired = [er.pose.bones[name].matrix.copy() for name in spec['mixed']]
    _plane(desired[0].translation, desired[1].translation,
           er.pose.bones[spec['mixed'][1]].tail.copy())
    rig.pose.bones['骨架设置'][spec['mode_key']] = 0.0
    _update(rig)
    for name, matrix in zip(spec['fk'], desired):
        rig.pose.bones[name].matrix = matrix
        _update(rig)


def switch(rig, *, limb='ARM', side='L', target='IK', align_fingers=True,
           max_error_meters=0.001):
    """Match a generated arm or leg and return actual measured output error.

    Raises ValueError for unsupported inputs, and restores involved channels and
    the mode property on any failure. Five finger IK targets are aligned when
    entering IK; their mode values and curl channels are always preserved.
    World errors are converted through scene unit scale. ``max_error_meters``
    is a positive finite rollback guard, not a visual-quality acceptance metric.
    ``ok`` describes execution, not visual approval or an animation bake.
    """
    if limb not in {'ARM', 'LEG'}:
        raise ValueError('limb必须为ARM或LEG')
    if side not in {'L', 'R'} or target not in {'IK', 'FK'}:
        raise ValueError('side必须为L或R，target必须为IK或FK')
    if not isinstance(align_fingers, bool):
        raise ValueError('align_fingers必须为布尔值')
    if (isinstance(max_error_meters, bool) or not isinstance(max_error_meters, (int, float))
            or not math.isfinite(max_error_meters) or max_error_meters <= 0):
        raise ValueError('max_error_meters必须是有限正数，单位为米')
    from . import control_layer
    if isinstance(rig, bpy.types.Object) and control_layer.CONTROL_KEY in rig:
        return control_layer.set_limb_mode(rig, limb=limb.title(), side=side, mode=target,
            align_fingers=align_fingers, max_error_meters=max_error_meters)
    if limb == 'LEG':
        from . import pose_switching_leg
        return pose_switching_leg.switch(rig, side=side, target=target,
                                         max_error_meters=max_error_meters)
    metadata = _metadata(rig)
    spec, settings, original_mode = _arm(rig, side)
    wanted_mode = 1.0 if target == 'IK' else 0.0
    if abs(original_mode - wanted_mode) < 1e-6:
        return {'ok': True, 'status': 'no_change', 'rig': rig.name, 'limb': limb,
                'side': side, 'target': target, 'changed_inputs': [], 'auto_keyed': False}
    mutable = (*spec['fk'], spec['target'], spec['pole'], *(p[0] for p in spec['fingers']))
    originals = {name: _channels(rig.pose.bones[name]) for name in mutable}
    finger_modes = {name: rig.pose.bones[name]['手指IK'] for name, _ in spec['fingers']}
    main_matrix = rig.pose.bones[spec['main_target']].matrix_basis.copy()
    witness = None
    try:
        witness = _Witness(rig, metadata['deforms'])
        before = witness.read()
        er = _update(rig)
        if target == 'IK':
            _to_ik(rig, spec, er, align_fingers)
            settings[spec['mode_key']] = wanted_mode
        else:
            _to_fk(rig, spec)
        actual_mode = float(_update(rig).pose.bones[settings.name][spec['mode_key']])
        if not math.isfinite(actual_mode) or abs(actual_mode - wanted_mode) > 1e-6:
            raise PoseSwitchError('模式仍受动作或驱动控制，无法完成切换；已恢复原姿态',
                {'ok': False, 'status': 'rejected', 'rig': rig.name, 'limb': limb,
                 'side': side, 'target': target, 'mode_before': original_mode,
                 'requested_mode': wanted_mode, 'actual_mode': actual_mode,
                 'reason': 'mode_input_overridden', 'restored': False,
                 'rest_modified': False, 'auto_keyed': False})
        after = witness.read()
        report = witness.compare(before, after)
        changed = [name for name in mutable if _channels(rig.pose.bones[name]) != originals[name]]
        if max(report['weighted_max_meters'], report['endpoint_max_meters']) > max_error_meters:
            failure = {'ok': False, 'status': 'rejected', 'rig': rig.name,
                       'limb': limb, 'side': side, 'target': target,
                       'mode_before': original_mode, 'requested_mode': wanted_mode,
                       'max_error_meters': max_error_meters, 'metrics': report,
                       'changed_inputs_before_rollback': changed,
                       'scope': 'native arm matching; measured guard is not visual approval',
                       'rest_modified': False, 'auto_keyed': False, 'restored': False}
            raise PoseSwitchError('当前受限链不能保持姿态，已恢复；请先略屈肘或调整当前姿态', failure)
        return {'ok': True, 'status': 'switched', 'rig': rig.name, 'limb': limb, 'side': side,
                'target': target, 'mode_property': spec['mode_key'], 'mode_before': original_mode,
                'mode_after': wanted_mode, 'changed_inputs': changed, 'metrics': report,
                'finger_ik_values': finger_modes, 'finger_modes_preserved': all(
                    rig.pose.bones[n]['手指IK'] == value for n, value in finger_modes.items()),
                'main_target_unchanged': rig.pose.bones[spec['main_target']].matrix_basis == main_matrix,
                'auto_keyed': False, 'rest_modified': False, 'max_error_meters': max_error_meters,
                'scope': 'native generated arm; curl inputs retained; arbitrary finger IK shape inversion unsupported'}
    except Exception as exc:
        _restore(rig, originals, settings, spec['mode_key'], original_mode)
        if isinstance(exc, PoseSwitchError):
            exc.report['restored'] = True
        raise
    finally:
        if witness is not None:
            witness.close()
