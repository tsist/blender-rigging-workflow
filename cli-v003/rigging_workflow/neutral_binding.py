# SPDX-License-Identifier: GPL-3.0-or-later
"""Generation-owned native bind baseline; checkers never recapture a pose.

The rest signature binds only final skin outputs and their B-Bone handles.
Pose checks compare native matrices/segments to the verified generation state,
while retaining the existing numeric and world-space neutral tolerances.
"""
import hashlib
import json
import math

import bpy
import numpy as np

KEY = 'neutral_binding'
SCHEMA = 'c94.native-neutral-binding.v1'
TOLERANCE = 1e-5
_BONE_FIELDS = (
    'bbone_segments', 'bbone_x', 'bbone_z', 'bbone_mapping_mode',
    'bbone_handle_type_start', 'bbone_handle_type_end',
    'bbone_easein', 'bbone_easeout', 'bbone_curveinx', 'bbone_curveinz',
    'bbone_curveoutx', 'bbone_curveoutz', 'bbone_rollin', 'bbone_rollout',
    'bbone_scalein', 'bbone_scaleout', 'use_endroll_as_inroll', 'use_scale_easing',
    'bbone_handle_use_ease_start', 'bbone_handle_use_ease_end',
    'bbone_handle_use_scale_start', 'bbone_handle_use_scale_end',
)
_POSE_FIELDS = (
    'bbone_easein', 'bbone_easeout', 'bbone_curveinx', 'bbone_curveinz',
    'bbone_curveoutx', 'bbone_curveoutz', 'bbone_rollin', 'bbone_rollout',
    'bbone_scalein', 'bbone_scaleout',
)
_HANDLES = ('bbone_custom_handle_start', 'bbone_custom_handle_end')


def _require(value, message):
    if not value:
        raise ValueError(message)


def _plain(value):
    if isinstance(value, str) or type(value) in (int, bool):
        return value
    if isinstance(value, float):
        _require(math.isfinite(value), '中立基线字段包含非有限值')
        return value
    return [_plain(item) for item in value]


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _array(value, shape):
    def numbers(item):
        if isinstance(item, list):
            return all(numbers(child) for child in item)
        return type(item) in (float, int) and math.isfinite(item)
    _require(isinstance(value, list) and numbers(value), '中立基线数值字段损坏')
    result = np.asarray(value, dtype=float)
    _require(result.shape == shape and np.isfinite(result).all(), '中立基线数值字段维度或有限性无效')
    return result


def _matrix(value):
    result = _array(value, (4, 4))
    _require(abs(np.linalg.det(result[:3, :3])) > 1e-12 and
             np.max(abs(result[3] - (0, 0, 0, 1))) <= 1e-7, '中立基线矩阵不可逆或不是仿射矩阵')
    return result


def _names(rig, metadata):
    names = metadata.get('deforms')
    _require(isinstance(names, list) and names and
             all(isinstance(name, str) and name for name in names) and
             len(set(names)) == len(names), '生成骨架输出骨列表损坏')
    _require(set(names) == {bone.name for bone in rig.data.bones if bone.use_deform},
             '生成中立基线的最终输出骨集合已改变，请重新生成')
    return names


def _rest_signature(rig, names):
    relevant = {name: rig.data.bones[name] for name in names}
    pending = list(relevant.values())
    while pending:
        bone = pending.pop()
        for field in _HANDLES:
            handle = getattr(bone, field)
            if handle and handle.name not in relevant:
                relevant[handle.name] = handle
                pending.append(handle)
    rows = []
    for name in sorted(relevant):
        bone = relevant[name]
        matrix = _plain(bone.matrix_local)
        _matrix(matrix)
        rows.append({'name': name, 'matrix': matrix, 'head': _plain(bone.head_local),
                     'tail': _plain(bone.tail_local), 'length': bone.length,
                     'parent': bone.parent.name if bone.parent else None,
                     'connected': bone.use_connect, 'deform': bone.use_deform,
                     'inherit_scale': bone.inherit_scale,
                     'inherit_rotation': bone.use_inherit_rotation,
                     'local_location': bone.use_local_location,
                     'bbone': {field: _plain(getattr(bone, field)) for field in _BONE_FIELDS},
                     'handles': {field: getattr(bone, field).name if getattr(bone, field) else None
                                 for field in _HANDLES}})
    return _hash(rows)


def _rows(rig, names):
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    rows = []
    for name in names:
        bone, pose = rig.data.bones[name], evaluated.pose.bones[name]
        delta = pose.matrix @ bone.matrix_local.inverted()
        segments = []
        if bone.bbone_segments > 1:
            for index in range(bone.bbone_segments + 1):
                transform = (pose.matrix @ pose.bbone_segment_matrix(index, rest=False) @
                             pose.bbone_segment_matrix(index, rest=True).inverted() @
                             bone.matrix_local.inverted())
                segments.append(_plain(transform))
        row = {'name': name, 'delta': _plain(delta), 'head': _plain(pose.head),
               'tail': _plain(pose.tail), 'segments': segments,
               'bbone_pose': {field: _plain(getattr(pose, field)) for field in _POSE_FIELDS}
                             if bone.bbone_segments > 1 else {}}
        _matrix(row['delta'])
        for matrix in segments:
            _matrix(matrix)
        rows.append(row)
    return rows


def _world_linear(rig, scale):
    matrix = np.asarray(rig.matrix_world, dtype=float)
    _require(math.isfinite(scale) and scale > 0 and np.isfinite(matrix).all() and
             abs(np.linalg.det(matrix[:3, :3])) > 1e-12, '中立基线需要有限且可逆的世界变换')
    return matrix[:3, :3] * scale


def _probes(bone, fraction=None):
    rest = np.asarray(bone.matrix_local, dtype=float)
    radius = min(.002, bone.length * .1)
    positions = (0, bone.length) if fraction is None else (bone.length * fraction,)
    return [rest[:3, :3] @ (x, y, z) + rest[:3, 3]
            for y in positions for x, z in ((0, 0), (radius, 0), (0, radius))]


def _point_error(current, baseline, probes, world):
    difference = current - baseline
    return max((float(np.linalg.norm(world @ (difference[:3, :3] @ point + difference[:3, 3])))
                for point in probes), default=0.0)


def capture_verified(rig, metadata, verification, mechanism_error, scale):
    """Called only by successful generation, after its real native neutral gate."""
    _require(rig.data.pose_position == 'POSE', '生成中立基线需要POSE姿态显示，不能从REST显示记录')
    names = _names(rig, metadata)
    _require(set(verification) >= {'sample_max', 'head_tail_max', 'sample_count'} and
             type(verification['sample_count']) is int and verification['sample_count'] == len(names) * 15,
             '生成中立验证报告不完整')
    for field, value in (('sample_max', verification['sample_max']),
                         ('head_tail_max', verification['head_tail_max']),
                         ('mechanism_max', mechanism_error)):
        _require(type(value) in (float, int) and math.isfinite(value) and 0 <= value <= TOLERANCE,
                 f'生成尚未通过原生中立校验，不能记录基线（{field}={value!r}，上限{TOLERANCE:g}）')
    signature = metadata.get('reference_signature')
    _require(isinstance(signature, str) and len(signature) == 64,
             '生成参考身份未绑定，不能记录中立基线')
    world = _world_linear(rig, scale)
    rows = _rows(rig, names)
    maximum = 0.0
    for row in rows:
        bone = rig.data.bones[row['name']]
        maximum = max(maximum, float(np.linalg.norm(world @ (_array(row['head'], (3,)) - np.asarray(bone.head_local)))),
                      float(np.linalg.norm(world @ (_array(row['tail'], (3,)) - np.asarray(bone.tail_local)))))
        maximum = max(maximum, _point_error(_matrix(row['delta']), np.eye(4), _probes(bone), world))
        for index, matrix in enumerate(row['segments']):
            maximum = max(maximum, _point_error(_matrix(matrix), np.eye(4),
                                               _probes(bone, index / bone.bbone_segments), world))
    _require(maximum <= TOLERANCE, '生成中立基线的真实世界探针未通过原1e-5米门')
    record = {'schema': SCHEMA, 'deforms': list(names), 'rest_signature': _rest_signature(rig, names),
              'reference_signature': signature, 'bones': rows,
              'verified': {'sample_max': verification['sample_max'],
                           'head_tail_max': verification['head_tail_max'],
                           'sample_count': verification['sample_count'],
                           'mechanism_max': mechanism_error, 'world_probe_max_meters': maximum}}
    record['record_sha256'] = _hash(record)
    return record


def check_current(rig, metadata, names, scale):
    """Validate a recorded generation state without granting recapture authority."""
    _require(rig.data.pose_position == 'POSE', '中立基线检查需要POSE姿态显示，请先离开REST显示')
    record = metadata.get(KEY)
    fields = {'schema', 'deforms', 'rest_signature', 'reference_signature', 'bones', 'verified', 'record_sha256'}
    _require(isinstance(record, dict) and set(record) == fields and record.get('schema') == SCHEMA,
             '生成中立基线缺失、损坏或版本不支持，请重新生成')
    checksum = record['record_sha256']
    _require(isinstance(checksum, str) and checksum == _hash({key: value for key, value in record.items() if key != 'record_sha256'}),
             '生成中立基线内容已改变，请重新生成')
    all_names = _names(rig, metadata)
    _require(record['deforms'] == all_names and record['reference_signature'] == metadata.get('reference_signature') and
             record['rest_signature'] == _rest_signature(rig, all_names),
             '生成中立基线的rest、输出骨或参考身份已改变，请重新生成')
    verified = record['verified']
    _require(isinstance(verified, dict) and set(verified) == {'sample_max', 'head_tail_max', 'sample_count', 'mechanism_max', 'world_probe_max_meters'} and
             type(verified['sample_count']) is int and verified['sample_count'] == len(all_names) * 15,
             '生成中立基线验证字段损坏')
    for field in ('sample_max', 'head_tail_max', 'mechanism_max', 'world_probe_max_meters'):
        value = verified[field]
        _require(type(value) in (float, int) and math.isfinite(value) and 0 <= value <= TOLERANCE,
                 '生成中立基线未具备有效的原生验证')
    _require(isinstance(record['bones'], list) and len(record['bones']) == len(all_names), '生成中立基线缺少输出骨')
    baseline = {}
    for name, row in zip(all_names, record['bones']):
        _require(isinstance(row, dict) and set(row) == {'name', 'delta', 'head', 'tail', 'segments', 'bbone_pose'} and row['name'] == name,
                 '生成中立基线的骨成员或字段损坏')
        _matrix(row['delta']);_array(row['head'], (3,));_array(row['tail'], (3,))
        bone = rig.data.bones[name]
        _require(isinstance(row['segments'], list) and len(row['segments']) == (bone.bbone_segments + 1 if bone.bbone_segments > 1 else 0),
                 '生成中立基线的B-Bone段数量损坏')
        for matrix in row['segments']:
            _matrix(matrix)
        expected_fields = set(_POSE_FIELDS) if bone.bbone_segments > 1 else set()
        _require(isinstance(row['bbone_pose'], dict) and set(row['bbone_pose']) == expected_fields,
                 '生成中立基线的B-Bone输入字段损坏')
        for field, value in row['bbone_pose'].items():
            current = _plain(getattr(rig.pose.bones[name], field))
            _array(value if isinstance(value, list) else [value], np.asarray(current).reshape(-1).shape)
        baseline[name] = row
    _require(all(name in baseline for name in names), '参与骨不在生成中立基线中')
    world = _world_linear(rig, scale)
    linear = spatial = fields_error = world_neutral = 0.0
    for current in _rows(rig, names):
        stored = baseline[current['name']];bone = rig.data.bones[current['name']]
        actual_delta, base_delta = _matrix(current['delta']), _matrix(stored['delta'])
        linear = max(linear, float(np.max(abs((actual_delta @ np.linalg.inv(base_delta))[:3, :3] - np.eye(3)))))
        for field in ('head', 'tail'):
            spatial = max(spatial, float(np.linalg.norm(world @ (_array(current[field], (3,)) - _array(stored[field], (3,))))))
        world_neutral = max(world_neutral,
                            float(np.linalg.norm(world @ (_array(current['head'], (3,)) - np.asarray(bone.head_local)))),
                            float(np.linalg.norm(world @ (_array(current['tail'], (3,)) - np.asarray(bone.tail_local)))),
                            _point_error(actual_delta, np.eye(4), _probes(bone), world))
        spatial = max(spatial, _point_error(actual_delta, base_delta, _probes(bone), world))
        for index, (actual, old) in enumerate(zip(current['segments'], stored['segments'])):
            actual, old = _matrix(actual), _matrix(old)
            linear = max(linear, float(np.max(abs((actual @ np.linalg.inv(old))[:3, :3] - np.eye(3)))))
            spatial = max(spatial, _point_error(actual, old, _probes(bone, index / bone.bbone_segments), world))
            world_neutral = max(world_neutral, _point_error(actual, np.eye(4),
                                                           _probes(bone, index / bone.bbone_segments), world))
        for field, value in current['bbone_pose'].items():
            fields_error = max(fields_error, float(np.max(abs(np.asarray(value) - np.asarray(stored['bbone_pose'][field])))))
    _require(linear <= TOLERANCE and spatial <= TOLERANCE and fields_error <= TOLERANCE and world_neutral <= TOLERANCE,
             f'参与骨当前不在已验证生成中立基线；先恢复中立再求解（线性{linear:g}，空间{spatial:g}米，B-Bone输入{fields_error:g}，实际中立{world_neutral:g}米）')
    return {'linear_error': linear, 'spatial_error_meters': spatial, 'bbone_input_error': fields_error,
            'world_neutral_error_meters': world_neutral}
