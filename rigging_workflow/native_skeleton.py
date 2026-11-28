# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit adoption of a local native skeleton, without generation or quality claims.

Only attach_identity writes one JSON identity property. All pose/rest/segment
measurements reuse the existing native-neutral helpers; no geometry or weights
are created. A source SHA records adoption provenance, not generation ownership.
"""
import hashlib
import json
import math
from pathlib import Path

import bpy
import numpy as np

from . import modular_neutral, neutral_binding

KEY = 'rigging_native_skeleton_v1'
SCHEMA = 'rigging.native-skeleton.v1'
NEUTRAL_SCHEMA = 'rigging.native-skeleton.native-neutral.v1'
TOLERANCE_METERS = modular_neutral.TOLERANCE_METERS
_OTHER_IDENTITIES = ('c94_generated_rig_v1', 'rigging_modular_v1')
_FIELDS = {'schema', 'adoption', 'label', 'source_file', 'source_sha256',
           'all_bones', 'rest_signature', 'deforms', 'primary_deforms',
           'helper_deforms', 'native_neutral', 'quality_authorization', 'record_sha256'}
_NEUTRAL_FIELDS = {'schema', 'transforms', 'capture_metric', 'capture_scope',
                   'capture_scale_length', 'tolerance_meters', 'capture_point_count',
                   'spatial_rest_error_meters', 'head_tail_rest_error_meters',
                   'capture_sample_errors_meters', 'conservative_joint_envelope_max_meters'}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _hash(value):
    return neutral_binding._hash(value)


def _local(rig):
    _require(isinstance(rig, bpy.types.Object) and rig.type == 'ARMATURE'
             and not rig.library and not rig.override_library and not rig.data.library
             and not rig.data.override_library and rig.mode != 'EDIT'
             and bpy.context.view_layer.objects.get(rig.name) == rig,
             '原生身份需要当前视图层的本地骨架，并结束编辑模式')
    _require(not any(key in rig for key in _OTHER_IDENTITIES),
             '拒绝将C94或模块化生成身份改装为独立原生身份')


def _names(value, label):
    _require(isinstance(value, list) and value
             and all(isinstance(name, str) and name for name in value)
             and len(set(value)) == len(value), label + '必须完整、非空且无重复')
    return value


def _sha(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in '0123456789abcdef' for character in value))


def _rest(rig, names):
    # Include control/mechanism rest and inheritance, not only deform outputs.
    return neutral_binding._rest_signature(rig, names)


def _scale(value):
    _require(type(value) in (float, int) and math.isfinite(value) and value > 0,
             '原生中立检查需要有限正数场景单位比例')
    return float(value)


def _measure(rig, names, scale):
    """Observe native consumption relative to actual rest; never reset a pose."""
    _require(rig.data.pose_position == 'POSE', 'REST显示不能建立或证明原生控制姿态中立')
    world = neutral_binding._world_linear(rig, scale)
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    transforms, samples = {}, {}
    maximum = head_tail = envelope = 0.0
    for name in names:
        bone, pose = rig.data.bones[name], evaluated.pose.bones[name]
        current = modular_neutral._transforms(rig, evaluated, name)
        for matrix in current:
            neutral_binding._matrix(matrix.tolist())
            envelope = max(envelope, modular_neutral._error(rig, name, matrix, np.eye(4), scale))
        errors = modular_neutral._capture_sample_errors(rig, evaluated, name, current, scale)
        _require(len(errors) == 15, '原生rest探针数量异常')
        samples[name] = errors
        maximum = max(maximum, max(errors))
        for point, rest in ((pose.head, bone.head_local), (pose.tail, bone.tail_local)):
            displacement = world @ (np.asarray(point, dtype=float) - np.asarray(rest, dtype=float))
            _require(np.isfinite(displacement).all(), '原生中立骨端点非有限')
            head_tail = max(head_tail, float(np.linalg.norm(displacement)))
        transforms[name] = current
    _require(math.isfinite(maximum) and math.isfinite(head_tail) and math.isfinite(envelope),
             '原生中立误差非有限')
    return transforms, samples, maximum, head_tail, envelope


def _validate(rig, metadata):
    _require(isinstance(metadata, dict) and set(metadata) == _FIELDS
             and metadata.get('schema') == SCHEMA
             and metadata.get('adoption') == 'explicit_adopted_native_rig'
             and metadata.get('quality_authorization') == 'none_identity_only',
             '原生登记记录缺失、损坏或版本不支持；身份不是质量许可')
    _require(_sha(metadata['record_sha256']) and metadata['record_sha256'] == _hash(
        {key: value for key, value in metadata.items() if key != 'record_sha256'}),
        '原生登记内容已改变')
    # Reading a valid adopted rig must not depend on the attachment file surviving.
    _require(isinstance(metadata['source_file'], str) and bool(metadata['source_file'])
             and Path(metadata['source_file']).is_absolute() and _sha(metadata['source_sha256']),
             '原生登记没有明确来源路径和内容SHA')
    _require(isinstance(metadata['label'], str) and bool(metadata['label']), '原生登记名称损坏')
    all_names = _names(metadata['all_bones'], '全部rest骨集合')
    deforms = _names(metadata['deforms'], '原生变形骨集合')
    _require(set(all_names) == {bone.name for bone in rig.data.bones}
             and set(deforms) == {bone.name for bone in rig.data.bones if bone.use_deform}
             and metadata['primary_deforms'] == deforms and metadata['helper_deforms'] == [],
             '原生全部rest/变形输出集合已改变，需重新描述并独立质检')
    _require(metadata['rest_signature'] == _rest(rig, all_names),
             '原生骨架的全部rest或B-Bone定义已改变，登记失效')
    record = metadata['native_neutral']
    _require(isinstance(record, dict) and set(record) == _NEUTRAL_FIELDS
             and record.get('schema') == NEUTRAL_SCHEMA
             and record.get('capture_metric') == 'native_bbone_selected_point_samples_v1'
             and isinstance(record.get('capture_scope'), str) and bool(record['capture_scope'])
             and record.get('tolerance_meters') == TOLERANCE_METERS,
             '原生空间中立证据缺失或合同已改变')
    _scale(record['capture_scale_length'])
    _require(type(record['capture_point_count']) is int
             and record['capture_point_count'] == len(deforms) * 15,
             '原生空间中立探针集合损坏')
    for field in ('spatial_rest_error_meters', 'head_tail_rest_error_meters'):
        value = record[field]
        _require(type(value) in (int, float) and math.isfinite(value)
                 and 0 <= value <= TOLERANCE_METERS, '登记时实际rest空间中立没有有效证据')
    value = record['conservative_joint_envelope_max_meters']
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
             '原生B-Bone关节包络记录无效')
    _require(isinstance(record['transforms'], dict) and set(record['transforms']) == set(deforms)
             and isinstance(record['capture_sample_errors_meters'], dict)
             and set(record['capture_sample_errors_meters']) == set(deforms),
             '原生空间中立证据的输出骨集合损坏')
    actual_maximum = 0.0
    for name in deforms:
        matrices = record['transforms'][name]
        count = rig.data.bones[name].bbone_segments + 1 if rig.data.bones[name].bbone_segments > 1 else 1
        _require(isinstance(matrices, list) and len(matrices) == count, '原生B-Bone段证据损坏')
        for matrix in matrices:
            neutral_binding._matrix(matrix)
        errors = neutral_binding._array(record['capture_sample_errors_meters'][name], (15,))
        _require((errors >= 0).all() and (errors <= TOLERANCE_METERS).all(), '登记rest探针误差无效')
        actual_maximum = max(actual_maximum, float(errors.max()))
    _require(actual_maximum == record['spatial_rest_error_meters'], '原生rest探针最大误差与记录不符')
    return metadata


def attach_identity(rig, source_file, expected_sha256):
    """Explicitly adopt the current local rig only after its real rest-neutral gate.

    The SHA binds the declared local source file at adoption; the all-bone rest
    fingerprint describes the actual adopted rig. This does not claim generation
    ownership, byte equality of live scene content to that file, or quality PASS.
    Existing identities are never overwritten and no pose/control is changed.
    """
    _local(rig)
    _require(KEY not in rig, '原生身份已经登记，不覆盖或重采集已有中立证据')
    if isinstance(expected_sha256, str):
        expected_sha256 = expected_sha256.lower()
    _require(_sha(expected_sha256), '请提供明确的来源SHA256十六进制值')
    source = Path(source_file)
    _require(source.is_absolute(), '原生登记必须明确绝对来源文件路径')
    source = source.resolve(strict=True)
    _require(source.is_file(), '原生登记来源不是明确本地文件')
    with source.open('rb') as stream:
        actual_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    _require(actual_sha == expected_sha256, '原生登记来源SHA256不符，未写身份')
    all_names = sorted(bone.name for bone in rig.data.bones)
    deforms = sorted(bone.name for bone in rig.data.bones if bone.use_deform)
    _names(all_names, '全部rest骨集合'); _names(deforms, '原生变形骨集合')
    scale = _scale(float(bpy.context.scene.unit_settings.scale_length))
    transforms, samples, maximum, head_tail, envelope = _measure(rig, deforms, scale)
    _require(maximum <= TOLERANCE_METERS and head_tail <= TOLERANCE_METERS,
             f'实际原生输出未满足rest空间中立（探针{maximum:g}米，端点{head_tail:g}米），拒绝登记')
    metadata = {
        'schema': SCHEMA, 'adoption': 'explicit_adopted_native_rig', 'label': rig.name,
        'source_file': str(source), 'source_sha256': actual_sha,
        'all_bones': all_names, 'rest_signature': _rest(rig, all_names),
        'deforms': deforms, 'primary_deforms': list(deforms), 'helper_deforms': [],
        'quality_authorization': 'none_identity_only',
        'native_neutral': {
            'schema': NEUTRAL_SCHEMA,
            'transforms': {name: [matrix.tolist() for matrix in matrices] for name, matrices in transforms.items()},
            'capture_metric': 'native_bbone_selected_point_samples_v1',
            'capture_scope': '15 actual rest probes per deform, with native selected B-Bone segments; not whole skin or continuous space',
            'capture_scale_length': scale, 'tolerance_meters': TOLERANCE_METERS,
            'capture_point_count': len(deforms) * 15, 'spatial_rest_error_meters': maximum,
            'head_tail_rest_error_meters': head_tail, 'capture_sample_errors_meters': samples,
            'conservative_joint_envelope_max_meters': envelope}}
    metadata['record_sha256'] = _hash(metadata)
    _validate(rig, metadata)
    # The only persistent mutation in this module; all rejection paths above it are read-only.
    rig[KEY] = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return metadata


def read_generated(rig):
    """Read an adopted identity without granting generation ownership or recapture."""
    _local(rig)
    raw = rig.get(KEY)
    _require(isinstance(raw, str), '缺少独立原生骨架JSON身份，请显式登记，不伪造生成标签')
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError('独立原生骨架身份JSON损坏') from error
    return _validate(rig, metadata)


def check_current(rig, metadata, names, scale):
    """Check actual native neutral consumption; never recapture a posed baseline."""
    current_metadata = read_generated(rig)
    _require(metadata == current_metadata, '传入元数据与该骨架实际原生身份不一致')
    _names(names, '参与骨集合')
    _require(set(names) <= set(metadata['deforms']), '参与骨必须属于完整原生变形输出')
    scale = _scale(scale)
    current, _, rest_error, head_tail, _ = _measure(rig, names, scale)
    baseline_error = 0.0
    for name in names:
        for now, stored in zip(current[name], metadata['native_neutral']['transforms'][name]):
            error = modular_neutral._error(rig, name, now, neutral_binding._matrix(stored), scale)
            _require(math.isfinite(error), '原生中立基线比较误差非有限')
            baseline_error = max(baseline_error, error)
    _require(baseline_error <= TOLERANCE_METERS and rest_error <= TOLERANCE_METERS
             and head_tail <= TOLERANCE_METERS,
             f'原生参与骨不在已登记的实际rest中立状态（基线{baseline_error:g}米，rest探针{rest_error:g}米，端点{head_tail:g}米），先恢复中立')
    return max(baseline_error, rest_error, head_tail)
