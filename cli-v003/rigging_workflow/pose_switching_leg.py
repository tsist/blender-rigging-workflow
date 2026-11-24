# SPDX-License-Identifier: GPL-3.0-or-later
"""Native C94 three-bone leg matching with bounded residuals and rollback.

The generated rig's IK, limits, foot pivots and toe drivers remain native.
No rest, constraints, drivers, curl inputs or animation keys are authored here.
The measured error guard is not visual approval or a full-ROM guarantee.
Structural checks cover the relations below, not every native RNA setting.
"""
import math

import bpy
from mathutils import Matrix

from . import pose_switching as common

_TOES = ('大脚趾', '第二趾', '第三趾', '第四趾', '第五趾')
_ROTATIONS = ('rotation_euler', 'rotation_quaternion', 'rotation_axis_angle')


def _require(value, message):
    if not value:
        raise ValueError(message)


def _finite_matrix(matrix):
    return all(math.isfinite(v) for row in matrix for v in row)


def _unit_matrix(matrix):
    if not _finite_matrix(matrix):
        return False
    linear = matrix.to_3x3()
    return (linear.determinant() > 0
            and all(abs(linear.col[i].length - 1) <= 1e-3 for i in range(3))
            and all(abs(linear.col[i].dot(linear.col[j])) <= 1e-3
                    for i in range(3) for j in range(i)))


def _spec(side):
    suffix = '.' + side
    pivots = tuple(n + suffix for n in (
        '机制-脚部-枢轴-足外', '机制-脚部-枢轴-足内', '机制脚部-枢轴-脚跟',
        '机制-脚部-枢轴-趾尖', '脚部-枢轴-趾根', '脚部-枢轴-脚背'))
    return {'key': ('左' if side == 'L' else '右') + '腿FK-IK',
        'fk': tuple(n + suffix for n in ('大腿FK', '膝盖FK', '小腿FK', '脚掌FK', '脚跟FK')),
        'mixed': tuple(n + suffix for n in ('机制-大腿', '机制-膝盖', '机制-小腿', '脚掌', '脚跟')),
        'ik': tuple(n + suffix for n in ('机制-大腿IK', '机制-膝盖IK', '机制-小腿IK')),
        'main': '腿部IK-目标' + suffix, 'target': '机制-腿部IK-目标' + suffix,
        'pole': '腿部IK-极点' + suffix, 'foot': '脚掌IK' + suffix,
        'toe': '脚趾' + suffix, 'toe_ik': '脚趾IK' + suffix,
        'heel_ik': '脚跟IK' + suffix, 'arch': '跖骨' + suffix,
        'arch_ik': '跖骨IK' + suffix, 'toe_parent': '机制-脚趾IK' + suffix,
        'toe_target': '机制-脚部-脚趾-IK目标' + suffix,
        'roll': '脚部滚转IK' + suffix, 'pivots': pivots,
        'connect': ('左' if side == 'L' else '右') + '脚脚趾连接'}


def _types(rig, name, types):
    constraints = tuple(rig.pose.bones[name].constraints)
    _require(tuple(c.type for c in constraints) == types,
             '原生腿部约束图已改变：' + name)
    return constraints


def _transform(c, rig, target, space):
    return (c.type == 'TRANSFORM' and c.target == rig and c.subtarget == target
            and c.owner_space == c.target_space == space
            and c.map_from == c.map_to == 'ROTATION' and c.mix_mode_rot == 'ADD')


def _copy(c, rig, target):
    return (c.type == 'COPY_TRANSFORMS' and c.target == rig and c.subtarget == target
            and c.owner_space == c.target_space == 'WORLD' and c.mix_mode == 'REPLACE'
            and not c.mute and abs(c.head_tail) < 1e-6)


def _validate(rig, side):
    s = _spec(side)
    required = {*s['fk'], *s['mixed'], *s['ik'], *s['pivots'], '骨架设置', '根骨骼',
        *(s[key] for key in ('main', 'target', 'pole', 'foot', 'toe', 'toe_ik',
                            'heel_ik', 'arch', 'arch_ik', 'toe_parent', 'toe_target', 'roll'))}
    _require(all(name in rig.pose.bones for name in required),
             '此生成骨架缺少当前腿部、足底枢轴或脚趾的原生骨')
    settings = rig.pose.bones['骨架设置']
    mode = settings.get(s['key'])
    _require(type(mode) in (int, float) and math.isfinite(mode), '腿部FK/IK属性无效')
    _require(abs(mode) < 1e-6 or abs(mode - 1) < 1e-6,
             '先把腿部FK/IK滑块设到完整FK或完整IK端点，再保姿态切换')
    connect = settings.get(s['connect'])
    _require(type(connect) in (int, float) and math.isfinite(connect) and 0 <= connect <= 1,
             '脚趾连接属性无效，原属性保持')
    parent_pairs = [
        (s['main'], '根骨骼'),
        (s['ik'][1], s['ik'][0]), (s['ik'][2], s['ik'][1]),
        (s['fk'][1], s['fk'][0]), (s['fk'][2], s['fk'][1]),
        (s['fk'][3], s['fk'][2]), (s['fk'][4], s['fk'][3]),
        (s['target'], s['pivots'][-1]), (s['foot'], s['target']),
        (s['pole'], s['target']), (s['heel_ik'], s['foot']),
        (s['toe'], s['arch']), (s['arch'], s['mixed'][3]),
        (s['arch_ik'], s['foot']), (s['toe_parent'], s['arch_ik']),
        (s['toe_ik'], s['toe_parent']), (s['toe_target'], s['pivots'][3]),
        (s['roll'], s['main']), (s['pivots'][0], s['main']),
        *zip(s['pivots'][1:], s['pivots'][:-1])]
    _require(all(rig.pose.bones[child].parent == rig.pose.bones[parent]
                 for child, parent in parent_pairs),
             '原生三骨膝链、足底枢轴或脚趾父链已改变')
    for name in (s['target'], s['pole'], s['roll']):
        _types(rig, name, ())
    for name in (s['fk'][0], s['fk'][2], s['main']):
        limit, = _types(rig, name, ('LIMIT_ROTATION',))
        _require(not limit.mute and limit.owner_space == 'LOCAL', '原生腿部旋转限制已改变：' + name)
    _types(rig, s['fk'][1], ())
    _types(rig, s['fk'][3], ())
    ik, limit = _types(rig, s['ik'][2], ('IK', 'LIMIT_ROTATION'))
    _require(ik.target == rig and ik.subtarget == s['target'] and ik.chain_count == 3
             and ik.pole_target == rig and ik.pole_subtarget == s['pole']
             and ik.use_tail and ik.use_location and not ik.use_rotation
             and not ik.mute and abs(ik.influence - 1) < 1e-6
             and not limit.mute and limit.owner_space == 'LOCAL',
             '原生腿部IK目标、极点或三骨链设置已改变')
    for name, fk, ik_name in zip(s['mixed'], s['fk'], (*s['ik'], s['foot'], s['heel_ik'])):
        copies = _types(rig, name, ('COPY_TRANSFORMS', 'COPY_TRANSFORMS'))
        _require(_copy(copies[0], rig, fk) and _copy(copies[1], rig, ik_name),
                 '原生腿部FK/IK混合关系已改变：' + name)
    location, = _types(rig, s['foot'], ('COPY_LOCATION',))
    _require(location.target == rig and location.subtarget == s['ik'][2]
             and location.owner_space == location.target_space == 'WORLD'
             and abs(location.head_tail - 1) < 1e-6 and not location.mute
             and abs(location.influence - 1) < 1e-6
             and location.use_x and location.use_y and location.use_z
             and not any((location.invert_x, location.invert_y, location.invert_z, location.use_offset)),
             '脚掌IK复制小腿末端位置的关系已改变')
    toe_copy, = _types(rig, s['toe'], ('COPY_TRANSFORMS',))
    _require(_copy(toe_copy, rig, s['toe_ik']), '脚趾FK/IK空间关系已改变')
    toe_limit, toe_transform, toe_extra = _types(rig, s['toe_ik'], ('LIMIT_ROTATION', 'TRANSFORM', 'TRANSFORM'))
    _require(not toe_limit.mute and toe_limit.owner_space == 'LOCAL'
             and _transform(toe_transform, rig, s['pivots'][4], 'LOCAL') and not toe_transform.mute
             and _transform(toe_extra, rig, s['roll'], 'LOCAL') and toe_extra.mute,
             '脚趾IK原生限制或足底附加旋转关系已改变')
    for name in (s['fk'][4], s['heel_ik']):
        heel_transform, = _types(rig, name, ('TRANSFORM',))
        _require(_transform(heel_transform, rig, s['mixed'][3], 'LOCAL_WITH_PARENT')
                 and not heel_transform.mute, '脚跟原生附加旋转关系已改变：' + name)
    return s, settings, float(mode)


def _blend_values(rig, s, er, mode):
    for name in s['mixed']:
        copies = er.pose.bones[name].constraints
        _require(abs(copies[0].influence - 1) < 1e-5 and abs(copies[1].influence - mode) < 1e-5,
                 '腿部模式驱动没有按当前端点求值：' + name)
    _require(abs(er.pose.bones[s['toe']].constraints[0].influence - mode) < 1e-5,
             '脚趾模式驱动没有按当前腿部端点求值')


def _plane(start, joint, end):
    axis = end - start
    length = axis.length
    _require(math.isfinite(length) and length >= 1e-8, '腿部根与末端重合，无法确定切换平面')
    axis /= length
    radial = joint - start - axis * (joint - start).dot(axis)
    _require(math.isfinite(radial.length) and radial.length / length >= 1e-5,
             '膝部过直，无法确定稳定极点；请略微屈膝后重试')
    return axis, radial


def _basis(bone, matrix, er):
    parent_pose = er.pose.bones[bone.parent.name].matrix if bone.parent else Matrix.Identity(4)
    parent_rest = bone.parent.bone.matrix_local if bone.parent else Matrix.Identity(4)
    return bone.bone.convert_local_to_pose(matrix, bone.bone.matrix_local,
        parent_matrix=parent_pose, parent_matrix_local=parent_rest, invert=True)


def _match(rig, name, wanted, *, position=True):
    """At most four native local-basis residual corrections, with limits intact."""
    pb = rig.pose.bones[name]
    saved_location = pb.location.copy()
    errors = []
    for _ in range(4):
        er = common._update(rig)
        actual = er.pose.bones[name].matrix.copy()
        aim = wanted.copy()
        if not position:
            aim.translation = actual.translation
        _require(_finite_matrix(actual) and _finite_matrix(aim), '腿部匹配产生非有限矩阵，已恢复输入')
        error = max(abs(a - b) for row_a, row_b in zip(aim, actual) for a, b in zip(row_a, row_b))
        errors.append(error)
        if error < 2e-6:
            break
        desired_basis, actual_basis = _basis(pb, aim, er), _basis(pb, actual, er)
        _require(abs(actual_basis.determinant()) > 1e-12, '腿部约束求值矩阵不可逆，已恢复输入')
        pb.matrix_basis = desired_basis @ actual_basis.inverted() @ pb.matrix_basis
        if not position:
            pb.location = saved_location
    common._update(rig)
    return errors


def _to_fk(rig, s):
    er = common._update(rig)
    wanted = {name: er.pose.bones[name].matrix.copy() for name in (*s['mixed'], s['toe'])}
    _plane(wanted[s['mixed'][0]].translation, wanted[s['mixed'][1]].translation,
           er.pose.bones[s['mixed'][2]].tail.copy())
    rig.pose.bones['骨架设置'][s['key']] = 0.0
    common._update(rig)
    details = {fk: _match(rig, fk, wanted[mixed])
               for fk, mixed in zip(s['fk'], s['mixed'])}
    details[s['toe']] = _match(rig, s['toe'], wanted[s['toe']])
    return details


def _to_ik(rig, s, main_channels):
    er = common._update(rig)
    desired = [er.pose.bones[name].matrix.copy() for name in s['mixed']]
    wanted_toe = er.pose.bones[s['toe']].matrix.copy()
    root = desired[0].translation.copy()
    joint = root + desired[0].to_3x3().col[1] * rig.data.bones[s['ik'][0]].length
    knee_end = joint + desired[1].to_3x3().col[1] * rig.data.bones[s['ik'][1]].length
    end = knee_end + desired[2].to_3x3().col[1] * rig.data.bones[s['ik'][2]].length
    _plane(root, joint, end)  # Geometry failures precede the first pose write.
    main = rig.pose.bones[s['main']]
    matrix = er.pose.bones[s['main']].matrix.copy()
    matrix.translation += end - er.pose.bones[s['target']].head
    main.matrix = matrix
    # Matrix decomposition can perturb untouched rotation/scale channels.
    # Only this control's position is a matching input.
    for key in (*_ROTATIONS, 'scale'):
        setattr(main, key, main_channels[key])
    main.rotation_mode = main_channels['rotation_mode']
    common._update(rig)
    foot_errors = _match(rig, s['foot'], desired[3], position=False)
    angles = []
    for _ in range(2):
        er = common._update(rig)
        first, last = er.pose.bones[s['ik'][0]], er.pose.bones[s['ik'][2]]
        axis, actual = _plane(first.head, first.tail, last.tail)
        wanted = joint - first.head - axis * (joint - first.head).dot(axis)
        pole_matrix = er.pose.bones[s['pole']].matrix.copy()
        relative = pole_matrix.translation - first.head
        axial = axis * relative.dot(axis)
        radial = relative - axial
        _require(min(radial.length, wanted.length) >= 1e-8,
                 '极点位于腿部轴上，无法稳定匹配；请调整极点或略微屈膝')
        angle = math.atan2(axis.dot(actual.cross(wanted)), actual.dot(wanted))
        angles.append(angle)
        pole_matrix.translation = first.head + axial + Matrix.Rotation(angle, 3, axis) @ radial
        rig.pose.bones[s['pole']].matrix = pole_matrix
        common._update(rig)
    rig.pose.bones['骨架设置'][s['key']] = 1.0
    common._update(rig)
    toe_errors = _match(rig, s['toe_ik'], wanted_toe)
    heel_errors = _match(rig, s['heel_ik'], desired[4])
    return {'foot': foot_errors, 'pole_radians': angles, 'toe': toe_errors, 'heel': heel_errors}


def switch(rig, *, side='L', target='IK', max_error_meters=0.001):
    """Match the generated C94 leg, preserving native foot inputs; no auto-key.

    Only complete FK/IK endpoints are accepted. Stretch, singular geometry and
    native-limit requests outside the measured guard are refused. Unsuccessful
    matching restores all involved channels and the mode before raising.
    """
    _require(side in {'L', 'R'} and target in {'IK', 'FK'}, 'side必须为L或R，target必须为IK或FK')
    _require(type(max_error_meters) in (int, float) and math.isfinite(max_error_meters)
             and max_error_meters > 0, 'max_error_meters必须是有限正数，单位为米')
    scale = bpy.context.scene.unit_settings.scale_length
    _require(math.isfinite(scale) and scale > 0, '场景米制比例必须为有限正数')
    metadata = common._metadata(rig)
    s, settings, original_mode = _validate(rig, side)
    er = common._update(rig)
    _blend_values(rig, s, er, original_mode)
    wanted_mode = 1.0 if target == 'IK' else 0.0
    if abs(original_mode - wanted_mode) < 1e-6:
        return {'ok': True, 'status': 'no_change', 'rig': rig.name, 'limb': 'LEG',
                'side': side, 'target': target, 'changed_inputs': [], 'auto_keyed': False}
    mutable = (*s['fk'], s['main'], s['pole'], s['foot'], s['toe_ik'], s['heel_ik'], s['toe'])
    originals = {name: common._channels(rig.pose.bones[name]) for name in mutable}
    _require(all(all(math.isfinite(v) for key in common._CHANNELS for v in values[key])
                 and all(abs(v - 1) <= 1e-3 for v in values['scale']) for values in originals.values())
             and all(_unit_matrix(er.pose.bones[name].matrix) for name in (*s['mixed'], s['toe'])),
             '当前腿部具有拉伸、剪切或非单位局部缩放，尚未验证保姿态切换')
    roll_channels = common._channels(rig.pose.bones[s['roll']])
    toe_connect = settings[s['connect']]
    toe_curls = {pb.name: common._channels(pb) for pb in rig.pose.bones
                 if pb.name.endswith('.' + side) and pb.name.startswith(_TOES)}
    recovery = {**originals, s['roll']: roll_channels, **toe_curls}
    main_channels = originals[s['main']]
    witness = None
    try:
        witness = common._Witness(rig, metadata['deforms'])
        before = witness.read()
        details = _to_ik(rig, s, main_channels) if target == 'IK' else _to_fk(rig, s)
        er = common._update(rig)
        _blend_values(rig, s, er, wanted_mode)
        after = witness.read()
        metrics = witness.compare(before, after)
        preserved = {'foot_roll': common._channels(rig.pose.bones[s['roll']]) == roll_channels,
            'toe_connect': settings[s['connect']] == toe_connect,
            'toe_curl_inputs': all(common._channels(rig.pose.bones[name]) == channels
                                   for name, channels in toe_curls.items()),
            'main_target_rotation_scale': all(common._channels(rig.pose.bones[s['main']])[key] == main_channels[key]
                for key in ('rotation_mode', *_ROTATIONS, 'scale'))}
        _require(all(preserved.values()), '腿部匹配改变了受保护足底或脚趾输入，已恢复')
        changed = [name for name in mutable if common._channels(rig.pose.bones[name]) != originals[name]]
        if max(metrics['weighted_max_meters'], metrics['endpoint_max_meters']) > max_error_meters:
            failure = {'ok': False, 'status': 'rejected', 'rig': rig.name, 'limb': 'LEG',
                'side': side, 'target': target, 'mode_before': original_mode,
                'requested_mode': wanted_mode, 'metrics': metrics, 'max_error_meters': max_error_meters,
                'changed_inputs_before_rollback': changed, 'matching': details, 'restored': False,
                'rest_modified': False, 'auto_keyed': False,
                'scope': 'native three-bone leg; measured guard is not visual approval'}
            raise common.PoseSwitchError('当前三骨膝链或足底限制不能保持姿态，已恢复；请略屈膝或调整当前姿态', failure)
        return {'ok': True, 'status': 'switched', 'rig': rig.name, 'limb': 'LEG',
            'side': side, 'target': target, 'mode_property': s['key'], 'mode_before': original_mode,
            'mode_after': wanted_mode, 'changed_inputs': changed, 'metrics': metrics,
            'matching': details, 'preserved': preserved, 'toe_curl_input_count': len(toe_curls),
            'max_error_meters': max_error_meters, 'rest_modified': False, 'auto_keyed': False,
            'scope': 'native C94 three-bone leg; bounded foot/toe matching; no bake or full-ROM claim'}
    except Exception as exc:
        settings[s['connect']] = toe_connect
        common._restore(rig, recovery, settings, s['key'], original_mode)
        restored = (settings[s['key']] == original_mode
                    and settings[s['connect']] == toe_connect
                    and all(common._channels(rig.pose.bones[name]) == values
                            for name, values in recovery.items()))
        if isinstance(exc, common.PoseSwitchError):
            exc.report['restored'] = restored
        if not restored:
            raise RuntimeError('腿部匹配失败后，输入恢复未通过；请使用先前检查点') from exc
        raise
    finally:
        if witness is not None:
            witness.close()
