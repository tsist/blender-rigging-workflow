# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent C94 bone-input drafts and ordered neutral-relative pose mixing.

No Mesh, weight or Key is authored. Property/manual-Key inputs must already be
at the declared neutral. Drafts survive weight edits; Key samples must be made
again after skinning changes. This does not certify facial deformation quality.
"""
import copy
import json
import math

import bpy
from mathutils import Euler, Quaternion, Vector

from . import corrective_authoring as sig, face_modes as fm

STATE_KEY = 'c94_face_pose_library_v1'
SCHEMA = 'c94.face-pose-library.v1'
_NOTE = '仅混合骨输入；不混合面部属性/手工Key，不制作Mesh/Key，不代表角色权重已就绪。'
_EULER_MODES = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}


def _owned(rig):
    # The separate Key board contains driver-computed combination bones. Bone
    # authoring owns the three physical control layers, never that other route.
    groups = [rig.data.collections_all.get(fm.SPACES[k]) for k in ('basic', 'main', 'secondary')]
    _require(all(c is not None and len(c.bones) for c in groups), '缺少贴脸骨控分层')
    return sorted({b.name for c in groups for b in c.bones})


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _name(value):
    _require(isinstance(value, str) and bool(value.strip()), '姿态名称必须是非空文本')
    return value.strip()


def _rotation(row):
    mode = row['rotation_mode']
    if mode == 'QUATERNION':
        q = Quaternion(row['rotation_quaternion'])
        _require(q.magnitude > 1e-12, '不支持零长度Quaternion')
    elif mode == 'AXIS_ANGLE':
        angle, *xyz = row['rotation_axis_angle']
        axis = Vector(xyz)
        _require(axis.length > 1e-12 or angle == 0, '非零轴角旋转需要有效轴')
        q = Quaternion(axis.normalized(), angle) if angle else Quaternion((1, 0, 0, 0))
    else:
        _require(mode in _EULER_MODES, '不支持此面部旋转模式')
        q = Euler(row['rotation_euler'], mode).to_quaternion()
    _require(all(math.isfinite(v) for v in q), '旋转求值不是有限数')
    q.normalize()
    return q


def _validate(rig, values, neutral):
    fm._validate_inputs(rig, values, neutral)
    owned = set(_owned(rig))
    _require(all(row == neutral['bones'][name] for name, row in values['bones'].items()
                 if name not in owned), '图形表情板须保持中立；本库只混合贴脸骨控')
    # fm._validate_inputs above already proved object/Key identity and the
    # optional ref/index compatibility. Q9 neutral records store no ref while
    # _inputs serializes it; compare the semantic fields only, never rewrite
    # the old neutral or drop the identity gate.
    def manual_semantics(rows):
        return [{key: row[key] for key in ('object', 'data', 'key', 'value')}
                for row in rows]
    _require(values['properties'] == neutral['properties']
             and manual_semantics(values['manual_keys']) == manual_semantics(neutral['manual_keys']),
             '本切片要求面部属性/手工Key保持已记录的基线值')
    for name, row in values['bones'].items():
        _require(row['rotation_mode'] == neutral['bones'][name]['rotation_mode'],
                 '旋转模式与中立记录不同，请保留原库并明确重录中立：' + name)
        _require(all(v > 0 for v in row['scale']), '面部scale必须有限且为正：' + name)
        for field in fm._FIELDS:
            prop = rig.pose.bones[name].bl_rna.properties[field]
            _require(all(prop.hard_min <= v <= prop.hard_max for v in row[field]),
                     '面部输入超过原生RNA范围：' + name + '/' + field)
        _rotation(row)


def _context(rig):
    fm._rig(rig)
    _require(rig.is_editable and not rig.override_library and rig.name in bpy.context.scene.objects
             and rig.name in bpy.context.view_layer.objects, '需要当前场景可编辑的本地C94角色')
    _require(fm._mode(rig) == 'BONE', '姿态草稿库仅在BONE模式工作，不自动切换模式')
    neutral = fm._read(rig)['neutral']
    current = fm._inputs(rig)
    _validate(rig, neutral, neutral)
    _validate(rig, current, neutral)
    paths = {rig.pose.bones[name].path_from_id(field) for name in _owned(rig)
             for field in (*fm._FIELDS, 'rotation_mode')}
    paths.update(fm._property_path(rig.pose.bones[r['bone']], r['key']) for r in neutral['properties'])
    paths.add(fm._property_path(rig.pose.bones[fm.MODE_BONE], fm.MODE_PROPERTY))
    fm._static(rig, paths)
    key_data = fm._key_blocks(rig)
    for row in neutral['manual_keys']:
        keys = key_data[row['data']]
        fm._static(keys, {keys.key_blocks[row['key']].path_from_id('value')})
    return neutral, current


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, '姿态库含重复JSON字段；保留原库，不自动修复')
        result[key] = value
    return result


def _weights(value):
    _require(isinstance(value, (list, tuple)), '权重必须是有序(name, 0..1)列表')
    result, names = [], set()
    for item in value:
        _require(isinstance(item, (list, tuple)) and len(item) == 2, '每项权重须为(name, 0..1)')
        name, weight = _name(item[0]), item[1]
        _require(name not in names and type(weight) in (int, float) and math.isfinite(weight)
                 and 0 <= weight <= 1, '姿态权重重复、非有限或超出0..1')
        names.add(name)
        result.append([name, float(weight)])
    return result


def _load(rig, neutral):
    raw = rig.get(STATE_KEY)
    fingerprint = sig._hash(neutral)
    if raw is None:
        return {'schema': SCHEMA, 'neutral_fingerprint': fingerprint, 'poses': {}, 'previous': None}
    try:
        _require(isinstance(raw, str), '姿态库属性不是JSON文本')
        record = json.loads(raw, object_pairs_hook=_pairs)
        _require(isinstance(record, dict) and set(record) == {'schema', 'neutral_fingerprint', 'poses', 'previous'}
                 and record['schema'] == SCHEMA, '姿态库字段或版本损坏')
        _require(record['neutral_fingerprint'] == fingerprint, '中立定义已改变；旧姿态库保留，不能按新基线混合')
        _require(isinstance(record['poses'], dict), '姿态草稿列表损坏')
        for name, bones in record['poses'].items():
            _require(_name(name) == name and isinstance(bones, dict), '姿态名称或骨输入损坏')
            _validate(rig, dict(neutral, bones=bones), neutral)
        previous = record['previous']
        if previous is not None:
            _require(isinstance(previous, dict) and set(previous) == {'before', 'after', 'weights'}, '上次混合记录损坏')
            _validate(rig, previous['before'], neutral)
            _validate(rig, previous['after'], neutral)
            _weights(previous['weights'])
        return record
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('姿态库损坏；原JSON保持，请检查记录') from exc


def _write(rig, record):
    # Serialize completely before the single metadata commit.
    payload = json.dumps(sig._plain(record), ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    old = rig.get(STATE_KEY)
    try:
        rig[STATE_KEY] = payload
    except Exception:
        if old is None:
            if STATE_KEY in rig:
                del rig[STATE_KEY]
        else:
            rig[STATE_KEY] = old
        raise


def inspect(rig):
    neutral, _ = _context(rig)
    record = _load(rig, neutral)
    return {'configured': STATE_KEY in rig, 'pose_names': list(record['poses']),
            'pose_count': len(record['poses']), 'control_count': len(_owned(rig)),
            'neutral_fingerprint': record['neutral_fingerprint'],
            'has_previous': record['previous'] is not None, 'note': _NOTE}


def capture(rig, name, replace=False):
    neutral, current = _context(rig)
    record, name = _load(rig, neutral), _name(name)
    _require(type(replace) is bool, 'replace必须为明确布尔值')
    _require(replace or name not in record['poses'], '同名姿态已存在；替换须明确replace=True')
    record['poses'][name] = copy.deepcopy(current['bones'])
    _write(rig, record)
    return {'name': name, 'pose_count': len(record['poses']), 'inputs_unchanged': True, 'note': _NOTE}


def _mixed(neutral, poses, weights, owned):
    target = copy.deepcopy(neutral)
    active = [(poses[name], weight) for name, weight in weights if weight > 0]
    if not active:
        return target
    identity = Quaternion((1, 0, 0, 0))
    for name in owned:
        base = neutral['bones'][name]
        row = target['bones'][name]
        for field in ('location', 'scale'):
            row[field] = [base[field][i] + sum(weight * (pose[name][field][i] - base[field][i])
                                               for pose, weight in active) for i in range(3)]
        q0 = _rotation(base)
        q = q0.copy()
        for pose, weight in active:
            delta = q0.conjugated() @ _rotation(pose[name])
            delta.normalize()
            if delta.w < 0:
                delta.negate()
            q = q @ identity.slerp(delta, weight)
        q.normalize()
        if q.dot(q0) < 0:
            q.negate()
        mode = base['rotation_mode']
        if mode == 'QUATERNION':
            row['rotation_quaternion'] = list(q)
        elif mode == 'AXIS_ANGLE':
            axis, angle = q.to_axis_angle()
            row['rotation_axis_angle'] = [angle, *axis]
        else:
            row['rotation_euler'] = list(q.to_euler(mode, Euler(base['rotation_euler'], mode)))
    return target


def _apply_bones(rig, values):
    for name in _owned(rig):
        for field in fm._FIELDS:
            if list(getattr(rig.pose.bones[name], field)) != values['bones'][name][field]:
                setattr(rig.pose.bones[name], field, values['bones'][name][field])


def _apply(rig, neutral, record, target, *, weights=None):
    _validate(rig, target, neutral)
    fm._preflight(rig, target)
    before, old = fm._snapshot(rig), rig.get(STATE_KEY)
    try:
        _apply_bones(rig, target)
        fm._update(rig)
        after = fm._snapshot(rig)
        _require(after['mode'] == before['mode'] and after['visibility'] == before['visibility'],
                 '混合后模式或显隐发生变化，已拒绝提交')
        _validate(rig, after['inputs'], neutral)
        record['previous'] = ({'before': before['inputs'], 'after': after['inputs'], 'weights': weights}
                              if weights is not None else None)
        _write(rig, record)
        return {'applied': True, 'weights': weights, 'restored_previous': weights is None,
                'control_count': len(_owned(rig)), 'note': _NOTE}
    except Exception as error:
        errors = []
        def attempt(label, operation):
            try:
                operation()
            except Exception as recovery:
                errors.append(label + ': ' + str(recovery))
        # Restore the real captured scope, not only the bones this library
        # writes: non-owned properties/manual Keys, mode and collection display
        # may have been changed by native evaluation during the failed update.
        attempt('恢复骨/属性/手工Key输入', lambda: fm._apply_inputs(rig, before['inputs']))
        attempt('恢复模式', lambda: fm._set_mode(rig, before['mode']))
        attempt('恢复显隐', lambda: fm._apply_visibility(rig, before['visibility']))
        attempt('刷新恢复状态', lambda: fm._update(rig))
        if old is None:
            def clear_record():
                if STATE_KEY in rig:
                    del rig[STATE_KEY]
            attempt('移除本次姿态库记录', clear_record)
        else:
            attempt('恢复原姿态库记录', lambda: rig.__setitem__(STATE_KEY, old))
        def verify():
            _require(fm._snapshot(rig) == before, '混合失败后的完整输入/模式/显隐恢复不一致')
        attempt('核实完整恢复', verify)
        if errors:
            raise RuntimeError(str(error) + '；姿态混合恢复未完整核实：' + '; '.join(errors)) from error
        raise


def mix(rig, weights):
    neutral, _ = _context(rig)
    record, rows = _load(rig, neutral), _weights(weights)
    _require(all(name in record['poses'] for name, _ in rows), '混合列表包含未知姿态，零权重也不能引用未知名')
    target = _mixed(neutral, record['poses'], rows, _owned(rig))
    return _apply(rig, neutral, record, target, weights=rows)


def remove(rig, name):
    neutral, _ = _context(rig)
    record, name = _load(rig, neutral), _name(name)
    _require(name in record['poses'], '没有此姿态，不删除其它内容')
    del record['poses'][name]
    _write(rig, record)
    return {'removed': name, 'pose_count': len(record['poses']), 'inputs_unchanged': True}


def restore_previous(rig):
    neutral, current = _context(rig)
    record = _load(rig, neutral)
    previous = record['previous']
    _require(previous is not None, '没有本库上次混合可恢复')
    _require(current == previous['after'], '上次混合后面部输入已手改，拒绝覆盖')
    return _apply(rig, neutral, record, copy.deepcopy(previous['before']))
