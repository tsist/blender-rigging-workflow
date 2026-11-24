# SPDX-License-Identifier: GPL-3.0-or-later
"""Generation-owned neutral proof in spatial units for native modular rigs."""
import hashlib
import json
import math
import bpy
import numpy as np
from mathutils import Vector

KEY = 'modular_native_neutral'
TOLERANCE_METERS = 1e-5


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _rest(rig):
    from . import neutral_binding
    names = [b.name for b in rig.data.bones if b.use_deform]
    return _hash({'outputs_and_bbone_handles': neutral_binding._rest_signature(rig, names),
        'all_bones': [{'name': b.name, 'parent': b.parent.name if b.parent else None,
        'matrix': [list(r) for r in b.matrix_local], 'length': b.length,
        'deform': b.use_deform, 'segments': b.bbone_segments}
        for b in rig.data.bones]})


def _probes(bone):
    radius = min(.01, bone.length * .15)
    return np.asarray([bone.matrix_local @ Vector((x, y, z))
        for y in (0.0, bone.length * .5, bone.length)
        for x, z in ((0, 0), (radius, 0), (-radius, 0), (0, radius), (0, -radius))], dtype=float)


def _transforms(rig, evaluated, name):
    bone, pose = rig.data.bones[name], evaluated.pose.bones[name]
    if bone.bbone_segments == 1:
        return [np.asarray(pose.matrix @ bone.matrix_local.inverted(), dtype=float)]
    return [np.asarray(pose.matrix @ pose.bbone_segment_matrix(index, rest=False)
            @ pose.bbone_segment_matrix(index, rest=True).inverted()
            @ bone.matrix_local.inverted(), dtype=float)
            for index in range(bone.bbone_segments + 1)]


def _error(rig, name, current, expected, scale):
    difference = current - expected
    probes = _probes(rig.data.bones[name])
    transformed = probes @ difference[:3, :3].T + difference[:3, 3]
    world = np.asarray(rig.matrix_world, dtype=float)[:3, :3] * scale
    return float(np.linalg.norm(transformed @ world.T, axis=1).max(initial=0))


def _capture_sample_errors(rig, evaluated, name, transforms, scale):
    """Finite rest probes using the B-Bone joints actually consumed per point.

    Segment selection follows the official PoseBone.bbone_segment_matrix API
    example. It is not an all-joint affine envelope or whole-skin certificate.
    """
    pose = evaluated.pose.bones[name]
    world = np.asarray(rig.matrix_world, dtype=float)[:3, :3] * scale
    errors = []
    for point in _probes(rig.data.bones[name]):
        if pose.bone.bbone_segments == 1:
            transform = transforms[0]
        else:
            index, blend = pose.bbone_segment_index(Vector(point))
            if (not 0 <= index < len(transforms) - 1 or
                    not math.isfinite(blend) or not 0 <= blend <= 1):
                raise ValueError('原生B-Bone采样段索引或混合无效')
            transform = transforms[index] * (1 - blend) + transforms[index + 1] * blend
        delta = transform - np.eye(4)
        displacement = (delta[:3, :3] @ point + delta[:3, 3]) @ world.T
        if not np.isfinite(displacement).all():
            raise ValueError('中立消费采样位移非有限')
        errors.append(float(np.linalg.norm(displacement)))
    return errors


def capture(rig, metadata):
    """Only call on an owned, freshly generated rig before exposing it."""
    if metadata.get('schema') != 'rigging.modular.generated.v1' or rig.data.pose_position != 'POSE':
        raise ValueError('只有新模块化生成骨架可以建立原生中立证据')
    scale = float(bpy.context.scene.unit_settings.scale_length)
    if not math.isfinite(scale) or scale <= 0: raise ValueError('需要有效场景单位')
    names = metadata['deforms']
    if set(names) != {b.name for b in rig.data.bones if b.use_deform}:
        raise ValueError('生成输出骨不完整')
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    rows, maximum, worst = {}, 0.0, None
    conservative_maximum, sample_errors = 0.0, {}
    for name in names:
        transforms = _transforms(rig, evaluated, name)
        for delta in transforms:
            if not np.isfinite(delta).all(): raise ValueError('中立骨矩阵非有限')
            error = _error(rig, name, delta, np.eye(4), scale)
            conservative_maximum = max(conservative_maximum, error)
        sample_errors[name] = _capture_sample_errors(rig, evaluated, name, transforms, scale)
        sample_maximum = max(sample_errors[name], default=0)
        if sample_maximum > maximum: maximum, worst = sample_maximum, name
        rows[name] = [delta.tolist() for delta in transforms]
    if maximum > TOLERANCE_METERS:
        raise ValueError(f'生成输出{worst}未满足空间中立要求（{maximum:g}米），拒绝建立基线')
    record = {'schema': 'rigging.modular.native-neutral.v1', 'rest': _rest(rig),
        'transforms': rows, 'spatial_rest_error_meters': maximum,
        'probe_count': sum(len(row) for row in rows.values()) * 15,
        'includes_native_bbone_segments': True, 'tolerance_meters': TOLERANCE_METERS,
        'capture_metric': 'native_bbone_selected_point_samples_v1',
        'capture_scope': 'Original 15 rest probes per deform bone only; not whole skin or continuous-space proof',
        'capture_point_count': sum(len(values) for values in sample_errors.values()),
        'capture_sample_errors_meters': sample_errors,
        'conservative_joint_envelope_max_meters': conservative_maximum}
    record['signature'] = _hash(record)
    metadata[KEY] = record
    return record


def check_current(rig, metadata, names, scale):
    if not isinstance(rig,bpy.types.Object) or rig.type!='ARMATURE' or rig.data.pose_position!='POSE':
        raise ValueError('中立检查需要骨架的POSE显示；REST显示不能证明控制姿态中立')
    record = metadata.get(KEY)
    if (not isinstance(record, dict) or record.get('schema') != 'rigging.modular.native-neutral.v1'
            or record.get('signature') != _hash({k: v for k, v in record.items() if k != 'signature'})
            or record.get('rest') != _rest(rig)):
        raise ValueError('新骨架中立证据丢失或rest已改变，请从已确认参考重新生成')
    if set(record['transforms']) != set(metadata['deforms']) or not set(names) <= set(record['transforms']):
        raise ValueError('中立证据与输出骨集合不一致')
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    maximum = 0.0
    for name in names:
        current, expected = _transforms(rig, evaluated, name), record['transforms'][name]
        if len(current) != len(expected): raise ValueError('原生B-Bone段数已改变，请重新生成')
        for now, baseline in zip(current, expected):
            baseline = np.asarray(baseline, dtype=float)
            if baseline.shape != (4, 4) or not np.isfinite(baseline).all():
                raise ValueError('中立空间证据损坏')
            maximum = max(maximum, _error(rig, name, now, baseline, scale))
    if maximum > TOLERANCE_METERS:
        raise ValueError(f'参与骨不在生成时的原生中立状态（{maximum:g}米），先恢复中立再求解')
    return maximum
