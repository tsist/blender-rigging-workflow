# SPDX-License-Identifier: GPL-3.0-or-later
"""Target policy and adapter for the modular Rigify human.

This module only owns the modular-human policy and controller adapter.  The
shared :mod:`retarget_action` core remains the owner of Action/slot creation,
sampling, source/target snapshots, preview, cancellation and restoration.

The supported target is the generated ``rigging_modular_v1`` /
``rigging.modular.generated.v1`` HUMAN rig built from Blender's bundled Rigify
``basic_human`` metarig with body, head and all four limbs.  It is a native
Rigify controller rig, not a detached FK copy and not a synthetic C94 rig.

The adapter forces the four limb ``IK_FK`` properties to the FK endpoint (1.0),
writes those properties as Action channels, drives the native FK/torso/limb
controllers, and reports the measured residual instead of promising full-ROM
fidelity.
"""

from __future__ import annotations

import hashlib
import json
import math

import bpy
from mathutils import Matrix, Quaternion, Vector


KIND = 'MODULAR_HUMAN_FK_V1'
POLICY_SCHEMA = 'rigging.modular.retarget_policy.v1'
POLICY_VERSION = 1
PROPERTY_SCHEMA = 'rigging.modular.retarget_properties.v1'
REPORT_SCHEMA = 'rigging.modular.retarget.report.v1'

GENERATED_KEY = 'rigging_modular_v1'
GENERATED_SCHEMA = 'rigging.modular.generated.v1'
C94_GENERATED_KEY = 'c94_generated_rig_v1'

MODE_PROPERTY = 'IK_FK'
FK_VALUE = 1.0

__all__ = (
    'KIND', 'POLICY_SCHEMA', 'PROPERTY_SCHEMA', 'REPORT_SCHEMA',
    'MODE_PROPERTY', 'FK_VALUE',
    'policy', 'validate', 'channels', 'snapshot_properties',
    'restore_properties', 'Adapter',
)

TORSO_CONTROLS = (
    'root', 'torso', 'hips', 'chest',
    'spine_fk', 'spine_fk.001', 'spine_fk.002', 'spine_fk.003',
    'neck', 'head',
)
SHOULDER_CONTROLS = ('shoulder.L', 'shoulder.R')
LIMB_FK_CONTROLS = tuple(
    name + '.' + side
    for side in ('L', 'R')
    for name in ('upper_arm_fk', 'forearm_fk', 'hand_fk',
                 'thigh_fk', 'shin_fk', 'foot_fk', 'toe_fk')
)
LIMB_PARENTS = tuple(
    name + '.' + side
    for side in ('L', 'R')
    for name in ('upper_arm_parent', 'thigh_parent')
)
# A small, fixed native roll set.  These existing Rigify tweak bones have a
# free local Y despite their parent MCH DAMPED_TRACK; X/Z are locked by the
# generated rig.  Other tweaks remain non-target.
ROLL_CONTROLS = (
    'upper_arm_tweak.L', 'upper_arm_tweak.R',
    'thigh_tweak.L', 'thigh_tweak.R',
)
ROLL_PARENTS = {
    'upper_arm_tweak.L': 'MCH-upper_arm_tweak.L',
    'upper_arm_tweak.R': 'MCH-upper_arm_tweak.R',
    'thigh_tweak.L': 'MCH-thigh_tweak.L',
    'thigh_tweak.R': 'MCH-thigh_tweak.R',
}
ROLL_SEMANTICS = (
    ('upper_arm.L', 'upper_arm_tweak.L', 'DEF-upper_arm.L'),
    ('upper_arm.R', 'upper_arm_tweak.R', 'DEF-upper_arm.R'),
    ('thigh.L', 'thigh_tweak.L', 'DEF-thigh.L'),
    ('thigh.R', 'thigh_tweak.R', 'DEF-thigh.R'),
)
ROLL_AXIS = 'LOCAL_Y'
ROLL_EULER_INDEX = 1
ROLL_MAX_DEGREES = 170.0
CONTROLS = TORSO_CONTROLS + SHOULDER_CONTROLS + LIMB_FK_CONTROLS + LIMB_PARENTS + ROLL_CONTROLS
CHANNEL_CONTROLS = TORSO_CONTROLS + SHOULDER_CONTROLS + LIMB_FK_CONTROLS + ROLL_CONTROLS
MODE_CONTROLS = ('upper_arm_parent.L', 'upper_arm_parent.R',
                 'thigh_parent.L', 'thigh_parent.R')

SPINE_DEFS = ('DEF-spine', 'DEF-spine.001', 'DEF-spine.002', 'DEF-spine.003',
              'DEF-spine.004', 'DEF-spine.005', 'DEF-spine.006')
PELVIS_DEFS = ('DEF-pelvis.L', 'DEF-pelvis.R')
SHOULDER_DEFS = ('DEF-shoulder.L', 'DEF-shoulder.R')
LIMB_DEFS = tuple(
    'DEF-' + name + '.' + side
    for side in ('L', 'R')
    for name in ('upper_arm', 'forearm', 'hand', 'thigh', 'shin', 'foot', 'toe')
)
EXPECTED_DEFS = tuple(SPINE_DEFS + PELVIS_DEFS + SHOULDER_DEFS + LIMB_DEFS)

OBSERVED = {
    'hips': 'DEF-spine',
    'spine': 'DEF-spine.001',
    'spine1': 'DEF-spine.002',
    'chest': 'DEF-spine.003',
    'neck': 'DEF-spine.004',
    'head': 'DEF-spine.006',
    'shoulder.L': 'DEF-shoulder.L',
    'shoulder.R': 'DEF-shoulder.R',
    'upper_arm.L': 'DEF-upper_arm.L',
    'upper_arm.R': 'DEF-upper_arm.R',
    'forearm.L': 'DEF-forearm.L',
    'forearm.R': 'DEF-forearm.R',
    'hand.L': 'DEF-hand.L',
    'hand.R': 'DEF-hand.R',
    'thigh.L': 'DEF-thigh.L',
    'thigh.R': 'DEF-thigh.R',
    'shin.L': 'DEF-shin.L',
    'shin.R': 'DEF-shin.R',
    'foot.L': 'DEF-foot.L',
    'foot.R': 'DEF-foot.R',
    'toes.L': 'DEF-toe.L',
    'toes.R': 'DEF-toe.R',
}

# semantic: (native controller, target observed DEF, target rest child DEF,
#            source rest child semantic)
_PLAN_SPEC = (
    ('hips', 'spine_fk', 'DEF-spine', 'DEF-spine.001', 'spine'),
    ('spine', 'spine_fk.001', 'DEF-spine.001', 'DEF-spine.002', 'spine1'),
    ('spine1', 'spine_fk.002', 'DEF-spine.002', 'DEF-spine.003', 'chest'),
    ('chest', 'spine_fk.003', 'DEF-spine.003', 'DEF-spine.004', 'neck'),
    ('neck', 'neck', 'DEF-spine.004', 'DEF-spine.005', 'head'),
    ('head', 'head', 'DEF-spine.006', None, None),
    ('shoulder.L', 'shoulder.L', 'DEF-shoulder.L', 'DEF-upper_arm.L', 'upper_arm.L'),
    ('shoulder.R', 'shoulder.R', 'DEF-shoulder.R', 'DEF-upper_arm.R', 'upper_arm.R'),
    ('upper_arm.L', 'upper_arm_fk.L', 'DEF-upper_arm.L', 'DEF-forearm.L', 'forearm.L'),
    ('upper_arm.R', 'upper_arm_fk.R', 'DEF-upper_arm.R', 'DEF-forearm.R', 'forearm.R'),
    ('forearm.L', 'forearm_fk.L', 'DEF-forearm.L', 'DEF-hand.L', 'hand.L'),
    ('forearm.R', 'forearm_fk.R', 'DEF-forearm.R', 'DEF-hand.R', 'hand.R'),
    ('hand.L', 'hand_fk.L', 'DEF-hand.L', None, None),
    ('hand.R', 'hand_fk.R', 'DEF-hand.R', None, None),
    ('thigh.L', 'thigh_fk.L', 'DEF-thigh.L', 'DEF-shin.L', 'shin.L'),
    ('thigh.R', 'thigh_fk.R', 'DEF-thigh.R', 'DEF-shin.R', 'shin.R'),
    ('shin.L', 'shin_fk.L', 'DEF-shin.L', 'DEF-foot.L', 'foot.L'),
    ('shin.R', 'shin_fk.R', 'DEF-shin.R', 'DEF-foot.R', 'foot.R'),
    ('foot.L', 'foot_fk.L', 'DEF-foot.L', 'DEF-toe.L', 'toes.L'),
    ('foot.R', 'foot_fk.R', 'DEF-foot.R', 'DEF-toe.R', 'toes.R'),
    ('toes.L', 'toe_fk.L', 'DEF-toe.L', None, None),
    ('toes.R', 'toe_fk.R', 'DEF-toe.R', None, None),
)

EXPECTED_MODULES = ('body', 'head', 'forelimb.L', 'forelimb.R',
                    'hindlimb.L', 'hindlimb.R')
OPTIONAL_MODULES = ('tail', 'correctives', 'shoulder_girdle', 'fingers.L', 'fingers.R', 'face')
ALLOWED_MODULES = EXPECTED_MODULES + OPTIONAL_MODULES
SCAPULAR_CONTROLS = ('scapula.L', 'scapula.R')
REQUIRED_SOURCE_LIMBS = tuple(
    name + '.' + side
    for side in ('L', 'R')
    for name in ('upper_arm', 'forearm', 'hand', 'thigh', 'shin', 'foot')
)
TAIL_DEFS = ('DEF-tail.001', 'DEF-tail.002', 'DEF-tail.003', 'DEF-tail.004')
TAIL_REFERENCE_BONES = ('tail.001', 'tail.002', 'tail.003', 'tail.004')
TAIL_CHAIN_CONTROLS = ('tail.001', 'tail.002', 'tail.003', 'tail.004')
TAIL_MASTER_CANDIDATES = ('tail_master.001', 'tail_master')
TAIL_TWEAK_CONTROLS = ('tweak_tail.001', 'tweak_tail.002',
                       'tweak_tail.003', 'tweak_tail.004')
CORRECTIVE_COLLECTION = '局部修正形变'
CORRECTIVE_CONSTRAINT_NAME = '局部关节旋转分担'
CORRECTIVE_SPECS = {
    'DEF-CORR-shoulder.L': {
        'region': 'shoulder', 'side': 'L', 'source': 'DEF-upper_arm.L',
        'parent': 'DEF-spine.003',
        'donors': ['DEF-spine.003', 'DEF-upper_arm.L'],
    },
    'DEF-CORR-shoulder.R': {
        'region': 'shoulder', 'side': 'R', 'source': 'DEF-upper_arm.R',
        'parent': 'DEF-spine.003',
        'donors': ['DEF-spine.003', 'DEF-upper_arm.R'],
    },
    'DEF-CORR-hip.L': {
        'region': 'hip', 'side': 'L', 'source': 'DEF-thigh.L',
        'parent': 'DEF-spine',
        'donors': ['DEF-spine', 'DEF-thigh.L'],
    },
    'DEF-CORR-hip.R': {
        'region': 'hip', 'side': 'R', 'source': 'DEF-thigh.R',
        'parent': 'DEF-spine',
        'donors': ['DEF-spine', 'DEF-thigh.R'],
    },
}
CORRECTIVE_DEFS = tuple(sorted(CORRECTIVE_SPECS))


def require(value, message):
    if not value:
        raise ValueError(message)


def _canonical_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _matrix_list(matrix):
    return [[float(matrix[row][column]) for column in range(4)]
            for row in range(4)]


def _vector_list(vector):
    return [float(value) for value in vector]


def _finite_matrix(matrix):
    return all(math.isfinite(float(matrix[row][column]))
               for row in range(4) for column in range(4))


def _angle_degrees(first, second):
    first_rotation = (first.to_quaternion()
                      if hasattr(first, 'to_quaternion') else first)
    second_rotation = (second.to_quaternion()
                       if hasattr(second, 'to_quaternion') else second)
    rotation = first_rotation.inverted() @ second_rotation
    angle = math.degrees(abs(rotation.angle))
    return min(angle, 360.0 - angle)


def _direction_angle_degrees(first, second):
    if first.length <= 1e-12 or second.length <= 1e-12:
        return 0.0
    angle = math.degrees(first.normalized().angle(second.normalized()))
    return min(angle, 360.0 - angle)


def _twist_angle_degrees(first, second):
    """Unsigned roll around the first orientation's anatomical Y axis."""
    first_rotation = (first.to_quaternion()
                      if hasattr(first, 'to_quaternion') else first)
    second_rotation = (second.to_quaternion()
                       if hasattr(second, 'to_quaternion') else second)
    y = first_rotation.to_matrix().col[1].normalized()
    first_x = first_rotation.to_matrix().col[0]
    second_x = second_rotation.to_matrix().col[0]
    first_x = first_x - y * first_x.dot(y)
    second_x = second_x - y * second_x.dot(y)
    if first_x.length <= 1e-7 or second_x.length <= 1e-7:
        return 0.0
    angle = math.degrees(first_x.normalized().angle(second_x.normalized()))
    return min(angle, 360.0 - angle)


def _signed_twist_degrees(desired, actual):
    """Signed roll of actual around desired Y (positive = desired X -> actual X)."""
    first_rotation = (desired.to_quaternion()
                      if hasattr(desired, 'to_quaternion') else desired)
    second_rotation = (actual.to_quaternion()
                       if hasattr(actual, 'to_quaternion') else actual)
    y = first_rotation.to_matrix().col[1].normalized()
    first_x = first_rotation.to_matrix().col[0]
    second_x = second_rotation.to_matrix().col[0]
    first_x = first_x - y * first_x.dot(y)
    second_x = second_x - y * second_x.dot(y)
    if first_x.length <= 1e-7 or second_x.length <= 1e-7:
        return 0.0
    first_x.normalize()
    second_x.normalize()
    return math.degrees(math.atan2(first_x.cross(second_x).dot(y),
                                   first_x.dot(second_x)))


def _update(rig):
    from . import pose_switching as pose
    return pose._update(rig)


def _armature(target):
    from .retarget_source import armature
    return armature(target)


def _rest_signature(target):
    from . import retarget_calibration as calibration
    return calibration.bone_rest_signature(target)


def _static_constraint_signature(target):
    """Static constraint graph hash, excluding IK_FK-driven influence/mute."""
    rows = []
    for pose_bone in sorted(target.pose.bones, key=lambda item: item.name):
        for constraint in pose_bone.constraints:
            row = {
                'bone': pose_bone.name,
                'name': constraint.name,
                'type': constraint.type,
            }
            for attribute in ('target', 'subtarget', 'pole_target',
                              'pole_subtarget', 'chain_count', 'owner_space',
                              'target_space', 'mix_mode', 'mix_mode_rot',
                              'use_offset', 'head_tail'):
                if not hasattr(constraint, attribute):
                    continue
                value = getattr(constraint, attribute)
                if isinstance(value, bpy.types.ID):
                    value = value.name
                if value is None or isinstance(value, (str, bool, int, float)):
                    row[attribute] = value
            rows.append(row)
    return _canonical_hash(rows)


def _read_generated(target):
    """Read the real modular generated record through rig_identity.read.

    The local development package may not yet contain ``modular_rig.py``.  In
    that one situation the exact same custom-property key/schema is parsed
    strictly here so an actual Rigify-generated candidate can be tested before
    the authoring module is installed.  An invalid record is never accepted by
    this fallback.
    """
    try:
        from . import rig_identity
        return rig_identity.read(target)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, 'name', ''))
        if not (missing.endswith('modular_rig') or missing.endswith('rig_identity')):
            raise
    except RuntimeError as exc:
        raise ValueError('模块化生成记录未通过作者校验：' + str(exc)) from exc
    raw = target.get(GENERATED_KEY)
    require(isinstance(raw, str) and raw, '缺少模块化生成记录：' + GENERATED_KEY)
    try:
        record = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('模块化生成记录JSON损坏') from exc
    require(isinstance(record, dict), '模块化生成记录必须是JSON对象')
    require(record.get('schema') == GENERATED_SCHEMA,
            '不是模块化生成schema：' + str(record.get('schema')))
    require(C94_GENERATED_KEY not in target, '拒绝把C94生成身份当模块化目标')
    required = {'schema', 'kind', 'label', 'configuration', 'reference',
                'reference_signature', 'module_bones', 'deforms',
                'mechanism_mapping', 'controls', 'tweak_collections_hidden',
                'correctives', 'rigify', 'generated_at', 'limitations'}
    require(required <= set(record), '模块化生成记录缺少必需字段')
    require(not any('c94' in str(key).lower() for key in record),
            '模块化生成记录不允许C94身份字段')
    actual_deforms = {bone.name for bone in target.data.bones if bone.use_deform}
    require(isinstance(record.get('deforms'), list)
            and record['deforms'] == sorted(set(record['deforms']))
            and set(record['deforms']) == actual_deforms,
            '模块化生成记录deforms与实际形变骨不一致')
    mapping = record.get('mechanism_mapping')
    require(isinstance(mapping, dict) and set(mapping) == actual_deforms,
            '模块化生成记录mechanism_mapping不完整')
    require(all(isinstance(name, str) and name in target.data.bones
                for name in mapping.values()),
            '模块化生成记录mechanism_mapping含不存在骨')
    controls = record.get('controls')
    require(isinstance(controls, list)
            and controls == sorted(set(controls))
            and all(name in target.pose.bones for name in controls),
            '模块化生成记录controls无效')
    return record


def _metadata_summary(record):
    return {
        'object_key': GENERATED_KEY,
        'schema': record['schema'],
        'kind': record['kind'],
        'label': record.get('label'),
        'reference': record.get('reference'),
        'reference_signature': record.get('reference_signature'),
        'configuration': json.loads(json.dumps(record.get('configuration'),
                                               ensure_ascii=False)),
        'correctives': json.loads(json.dumps(record.get('correctives'),
                                             ensure_ascii=False)),
        'rigify': json.loads(json.dumps(record.get('rigify'), ensure_ascii=False)),
        'deform_count': len(record['deforms']),
        'deforms_sha256': _canonical_hash(sorted(record['deforms'])),
        'mechanism_mapping_sha256': _canonical_hash(record['mechanism_mapping']),
        'mechanism_mapping_methods_sha256': _canonical_hash(
            record.get('mechanism_mapping_methods', {})),
        'primary_deforms_sha256': (
            _canonical_hash(sorted(record['primary_deforms']))
            if isinstance(record.get('primary_deforms'), list) else None
        ),
        'auxiliary_specs_sha256': (
            _canonical_hash(record['auxiliary_specs'])
            if isinstance(record.get('auxiliary_specs'), list) else None
        ),
        'module_bones_sha256': _canonical_hash(record.get('module_bones', {})),
        'controls_count': len(record['controls']),
        'controls_sha256': _canonical_hash(record['controls']),
        'tweak_collections_hidden': list(record.get('tweak_collections_hidden', [])),
        'generated_at': record.get('generated_at'),
    }


def _validate_corrective_spec(spec):
    require(isinstance(spec, dict), 'correctives辅助骨规格不是对象')
    name = spec.get('name')
    require(name in CORRECTIVE_SPECS, 'correctives含非预期辅助骨：' + str(name))
    expected = CORRECTIVE_SPECS[name]
    for field in ('region', 'side', 'source', 'parent'):
        require(spec.get(field) == expected[field],
                'correctives辅助骨身份不符：' + name + '/' + field)
    require(spec.get('donors') == expected['donors'],
            'correctives辅助骨donors不符：' + name)
    strength = spec.get('strength')
    require(type(strength) in (int, float)
            and abs(float(strength) - 0.3) < 1e-6,
            'correctives辅助骨strength不符：' + name)
    radius = spec.get('radius')
    require(type(radius) in (int, float)
            and math.isfinite(float(radius)) and float(radius) > 0,
            'correctives辅助骨radius无效：' + name)
    for field in ('head', 'tail'):
        value = spec.get(field)
        require(isinstance(value, list) and len(value) == 3
                and all(type(item) in (int, float)
                        and math.isfinite(float(item)) for item in value),
                'correctives辅助骨' + field + '无效：' + name)
    length = Vector(spec['tail']) - Vector(spec['head'])
    require(length.length > 1e-7, 'correctives辅助骨长度退化：' + name)
    return {
        'name': name,
        'region': expected['region'],
        'side': expected['side'],
        'source': expected['source'],
        'parent': expected['parent'],
        'donors': list(expected['donors']),
        'strength': float(strength),
        'radius': float(radius),
        'head': [float(item) for item in spec['head']],
        'tail': [float(item) for item in spec['tail']],
    }


def _validate_generated_metadata(record):
    """Validate the generated record and return its explicit module contract."""
    require(record.get('schema') == GENERATED_SCHEMA,
            '不是模块化生成schema：' + str(record.get('schema')))
    require(not any('c94' in str(key).lower() for key in record),
            '模块化生成记录不允许C94身份字段')
    require(record.get('kind') == 'HUMAN',
            '只支持模块化HUMAN全四肢目标；当前kind=' + str(record.get('kind')))
    configuration = record.get('configuration')
    require(isinstance(configuration, dict), '模块化生成记录缺少configuration')
    require(configuration.get('kind') == 'HUMAN',
            '目标配置不是HUMAN全四肢')
    require(configuration.get('segments') in (1, 2) and configuration.get('bbones') in (1, 2),
            '目标四肢分段须为1或2')
    modules = configuration.get('enabled_modules')
    require(isinstance(modules, list) and all(isinstance(name, str) for name in modules),
            '目标enabled_modules无效')
    require(len(modules) == len(set(modules)), '目标enabled_modules含重复模块')
    require(set(modules) <= set(ALLOWED_MODULES),
            '只接受body/head/全四肢及可选tail/correctives/shoulder_girdle；当前模块='
            + json.dumps(modules, ensure_ascii=False))
    require(set(EXPECTED_MODULES) <= set(modules),
            '只支持完整HUMAN body/head/全四肢目标；缺少='
            + str(sorted(set(EXPECTED_MODULES) - set(modules))))
    extra_modules = tuple(name for name in OPTIONAL_MODULES if name in modules)
    tail_on = 'tail' in extra_modules
    correctives_on = 'correctives' in extra_modules
    shoulder_girdle_on = 'shoulder_girdle' in extra_modules
    primary_defs = tuple(EXPECTED_DEFS) + (TAIL_DEFS if tail_on else ())
    extended = configuration['segments'] == 2 or bool(set(modules) & {'face', 'fingers.L', 'fingers.R'})
    if extended:
        # _read_generated validates the actual deforms and their mechanism mapping.
        additional = set(record['deforms']) - set(primary_defs) - set(CORRECTIVE_DEFS)
        primary_defs += tuple(sorted(additional))
    expected_defs = primary_defs + (CORRECTIVE_DEFS if correctives_on else ())
    deforms = record.get('deforms')
    require(isinstance(deforms, list)
            and deforms == sorted(set(deforms))
            and set(deforms) == set(expected_defs),
            '目标形变骨与允许模块DEF集合不一致：多出='
            + str(sorted(set(deforms or []) - set(expected_defs)))
            + ' 缺少=' + str(sorted(set(expected_defs) - set(deforms or []))))
    mapping = record.get('mechanism_mapping')
    require(isinstance(mapping, dict) and set(mapping) == set(expected_defs),
            '目标mechanism_mapping未完整覆盖允许DEF')
    controls = record.get('controls')
    require(isinstance(controls, list)
            and controls == sorted(set(controls))
            and all(isinstance(name, str) for name in controls),
            '目标controls记录无效')
    missing_controls = [name for name in CONTROLS if name not in controls]
    require(not missing_controls,
            '目标生成记录缺少真实控制器：' + ', '.join(missing_controls))
    module_bones = record.get('module_bones')
    require(isinstance(module_bones, dict)
            and set(module_bones) == set(modules),
            '目标module_bones键与enabled_modules不一致')
    if shoulder_girdle_on:
        require(module_bones['shoulder_girdle'] == list(SCAPULAR_CONTROLS),
                'shoulder_girdle参考骨必须为scapula.L/R')
        require(all(name in controls for name in SCAPULAR_CONTROLS)
                and not (set(SCAPULAR_CONTROLS) & set(deforms)),
                '肩胛载体必须是具名非DEF手动控制')
    else:
        require(not any(name in controls for name in SCAPULAR_CONTROLS),
                '无shoulder_girdle模块但记录含肩胛控制')

    tail_controls = ()
    tail_master = None
    if tail_on:
        require(module_bones.get('tail') == sorted(TAIL_REFERENCE_BONES),
                'tail模块参考骨身份不符')
        for index, name in enumerate(TAIL_DEFS, 1):
            require(mapping.get(name) == 'ORG-tail.' + str(index).zfill(3),
                    'tail DEF机制映射不符：' + name)
        masters = [name for name in TAIL_MASTER_CANDIDATES if name in controls]
        require(len(masters) == 1, 'tail模块缺少唯一master控制')
        tail_master = masters[0]
        tail_controls = (tuple(TAIL_CHAIN_CONTROLS) + (tail_master,)
                         + tuple(TAIL_TWEAK_CONTROLS))
        require(all(name in controls for name in tail_controls),
                'tail模块缺少真实Tail/Tweak控制')
        require(sorted(name for name in controls if 'tail' in name.lower())
                == sorted(tail_controls),
                'tail模块含非预期Tail控制')
    else:
        require(not any('tail' in name.lower() for name in controls),
            '无tail模块但目标含Tail控制')
        require(not any(name.startswith('DEF-tail.') for name in deforms),
                '无tail模块但目标含DEF-tail形变骨')

    correctives_info = None
    corrective_specs = {}
    if correctives_on:
        correctives = record.get('correctives')
        require(isinstance(correctives, dict)
                and correctives.get('enabled') is True
                and correctives.get('status') == 'installed'
                and str(correctives.get('module', '')).rsplit('.', 1)[-1]
                == 'modular_correctives',
                'correctives模块安装身份无效')
        require(module_bones.get('correctives') == [],
                'correctives模块参考骨表必须为空')
        primary = record.get('primary_deforms')
        require(isinstance(primary, list)
                and primary == sorted(set(primary))
                and set(primary) == set(primary_defs),
                'correctives primary_deforms与允许主DEF不一致')
        specs = record.get('auxiliary_specs')
        require(isinstance(specs, list), 'correctives auxiliary_specs无效')
        names = [spec.get('name') if isinstance(spec, dict) else None
                 for spec in specs]
        require(len(names) == len(set(names))
                and set(names) == set(CORRECTIVE_DEFS),
                'correctives auxiliary_specs骨集合不符')
        for spec in specs:
            checked = _validate_corrective_spec(spec)
            corrective_specs[checked['name']] = checked
            require(mapping.get(checked['name']) == checked['source'],
                    'correctives机制映射不符：' + checked['name'])
        correctives_info = dict(correctives)
        require(not any('CORR' in name for name in controls),
                'correctives不应有显式控制器')
    else:
        correctives = record.get('correctives')
        require(isinstance(correctives, dict)
                and correctives.get('enabled') is False,
                '无correctives模块但元数据标记为已安装')
        require('primary_deforms' not in record
                and 'auxiliary_specs' not in record,
                '无correctives模块但含辅助骨元数据')
        require(not any(name.startswith('DEF-CORR-') for name in deforms),
                '无correctives模块但含DEF-CORR形变骨')

    return {
        'enabled_modules': tuple(modules),
        'extra_modules': extra_modules,
        'tail_on': tail_on,
        'correctives_on': correctives_on,
        'shoulder_girdle_on': shoulder_girdle_on,
        'scapular_controls': SCAPULAR_CONTROLS if shoulder_girdle_on else (),
        'primary_defs': tuple(sorted(primary_defs)),
        'expected_defs': tuple(sorted(expected_defs)),
        'tail_defs': TAIL_DEFS if tail_on else (),
        'tail_controls': tuple(tail_controls),
        'tail_master': tail_master,
        'corrective_defs': CORRECTIVE_DEFS if correctives_on else (),
        'corrective_specs': corrective_specs,
        'correctives_info': correctives_info,
        'bbones': configuration['bbones'],
    }


def _world_check(target):
    matrix = target.matrix_world
    require(_finite_matrix(matrix), '目标对象世界变换非有限')
    columns = [matrix.to_3x3().col[i] for i in range(3)]
    lengths = [column.length for column in columns]
    require(min(lengths) > 1e-8, '目标对象世界变换退化')
    require(matrix.determinant() > 0, '目标对象为负缩放/镜像，需单独适配')
    require(max(lengths) - min(lengths) < max(lengths) * 1e-5,
            '目标对象非均匀缩放需单独适配')
    require(all(abs(columns[i].dot(columns[j]))
                < lengths[i] * lengths[j] * 1e-5
                for i in range(3) for j in range(i)),
            '目标对象含剪切变换，需单独适配')
    return lengths[0]


def _object_conflicts(target):
    rows = []
    if target.library or target.data.library:
        rows.append({'kind': 'linked_target'})
    if target.parent is not None:
        rows.append({'kind': 'object_parent', 'object': target.parent.name})
    for constraint in target.constraints:
        rows.append({'kind': 'object_constraint', 'name': constraint.name,
                     'type': constraint.type})
    return rows


def _property_schema(target):
    """Structural custom-property schema; IK_FK's animated value is excluded."""
    rows = []
    for name in sorted(set(CONTROLS + MODE_CONTROLS)):
        pose_bone = target.pose.bones[name]
        for key in sorted(key for key in pose_bone.keys() if key != '_RNA_UI'):
            try:
                value = pose_bone[key]
            except (TypeError, ReferenceError) as exc:
                raise ValueError('控制属性不可读：' + name + '/' + key) from exc
            row = {'bone': name, 'property': key,
                   'value_type': type(value).__name__}
            if key == MODE_PROPERTY:
                row['animated_write_domain'] = True
            elif isinstance(value, bool):
                row['value'] = bool(value)
            elif type(value) in (int, float):
                require(math.isfinite(float(value)),
                        '控制属性非有限：' + name + '/' + key)
                row['value'] = value
            else:
                try:
                    row['length'] = len(value)
                except TypeError:
                    row['value_repr'] = repr(value)
            rows.append(row)
    return rows


def _pose_mode_rows(target):
    return {
        name: {
            'rotation_mode': target.pose.bones[name].rotation_mode,
            'lock_rotation': list(target.pose.bones[name].lock_rotation),
            'lock_rotation_w': bool(target.pose.bones[name].lock_rotation_w),
            'lock_location': list(target.pose.bones[name].lock_location),
            'lock_scale': list(target.pose.bones[name].lock_scale),
        }
        for name in CONTROLS
    }


def _driver_rows(target):
    rows = []
    for owner, label in ((target, 'OBJECT'), (target.data, 'ARMATURE')):
        animation_data = owner.animation_data
        if animation_data is None:
            continue
        for curve in animation_data.drivers:
            rows.append({'owner': label, 'path': curve.data_path,
                         'index': curve.array_index})
    return rows


def _write_domain_paths(record_or_none=None):
    """Data paths the adapter writes; a driver here cannot be combined safely."""
    controls = (CHANNEL_CONTROLS if record_or_none is None
                else tuple(record_or_none.get('channel_controls', CHANNEL_CONTROLS)))
    paths = set()
    for name in controls:
        prefix = 'pose.bones[' + json.dumps(name, ensure_ascii=False) + ']'
        for field in ('rotation_quaternion', 'rotation_euler',
                      'rotation_axis_angle', 'location'):
            paths.add(prefix + '.' + field)
    for name in MODE_CONTROLS:
        prefix = 'pose.bones[' + json.dumps(name, ensure_ascii=False) + ']'
        paths.add(prefix + '[' + json.dumps(MODE_PROPERTY,
                                             ensure_ascii=False) + ']')
    return paths


def _driver_conflicts(target, record_or_none=None):
    from . import control_layer
    paths = _write_domain_paths(record_or_none)
    owned = control_layer.verified_retarget_driver_keys(target)
    return [row for row in _driver_rows(target) if row['path'] in paths
            and (row['owner'] != 'OBJECT' or (row['path'], row['index']) not in owned)]


def _control_constraint_rows(target):
    rows = []
    names = sorted(set(CONTROLS + MODE_CONTROLS + tuple(OBSERVED.values())))
    for name in names:
        pose_bone = target.pose.bones[name]
        for constraint in pose_bone.constraints:
            rows.append({
                'bone': name,
                'name': constraint.name,
                'type': constraint.type,
                'mute': bool(constraint.mute),
                'influence': float(constraint.influence),
                'target': constraint.target.name
                if getattr(constraint, 'target', None) else None,
                'subtarget': getattr(constraint, 'subtarget', None),
            })
    return rows


def _object_transform_action_rows(target):
    rows = []
    animation_data = target.animation_data
    if animation_data is None:
        return rows
    actions = [animation_data.action] if animation_data.action else []
    for track in animation_data.nla_tracks:
        actions.extend(strip.action for strip in track.strips if strip.action)
    object_paths = {'location', 'rotation_euler', 'rotation_quaternion',
                    'rotation_axis_angle', 'scale', 'delta_location',
                    'delta_rotation_euler', 'delta_rotation_quaternion',
                    'delta_scale'}
    for action in actions:
        for layer in action.layers:
            for strip in layer.strips:
                if strip.type != 'KEYFRAME':
                    continue
                for slot in action.slots:
                    bag = strip.channelbag(slot)
                    if bag is None:
                        continue
                    for curve in bag.fcurves:
                        if curve.data_path in object_paths:
                            rows.append({'action': action.name,
                                         'path': curve.data_path,
                                         'index': curve.array_index})
    return rows


def _fingerprint(target):
    metadata = _read_generated(target)
    summary = _metadata_summary(metadata)
    property_schema = _property_schema(target)
    conflicts = _object_conflicts(target)
    return {
        'rest_signature': _rest_signature(target),
        'constraint_signature': _static_constraint_signature(target),
        'property_schema_sha256': _canonical_hash(property_schema),
        'pose_mode_sha256': _canonical_hash(_pose_mode_rows(target)),
        'driver_rows_sha256': _canonical_hash(_driver_rows(target)),
        'object_transform_action_sha256': _canonical_hash(
            _object_transform_action_rows(target)),
        'metadata_sha256': _canonical_hash(summary),
        'object_matrix': _matrix_list(target.matrix_world),
        'pose_position': target.data.pose_position,
        'object_parent': target.parent.name if target.parent else None,
        'object_conflicts': conflicts,
    }


def _validate_tail_target(target, contract):
    require(contract['tail_on'], 'tail契约未启用')
    for index, name in enumerate(TAIL_DEFS, 1):
        require(name in target.data.bones, 'tail模块缺少DEF骨：' + name)
        bone = target.data.bones[name]
        require(bone.use_deform, 'tail DEF不是形变骨：' + name)
        require(bone.bbone_segments >= 1, 'tail DEF B-Bone段数无效：' + name)
        org = 'ORG-tail.' + str(index).zfill(3)
        require(org in target.data.bones, 'tail模块缺少ORG骨：' + org)
        pose_bone = target.pose.bones[name]
        require(len(pose_bone.constraints) == 1,
                'tail DEF约束图不符：' + name)
        constraint = pose_bone.constraints[0]
        require(constraint.type == 'COPY_TRANSFORMS'
                and constraint.target == target
                and constraint.subtarget == org
                and constraint.owner_space == 'WORLD'
                and constraint.target_space == 'WORLD'
                and abs(constraint.influence - 1.0) < 1e-6
                and not constraint.mute,
                'tail DEF机制约束不符：' + name)
    for name in contract['tail_controls']:
        require(name in target.pose.bones, 'tail模块缺少真实控制：' + name)
    for index, name in enumerate(TAIL_CHAIN_CONTROLS):
        expected = (contract['tail_master'] if index == 0
                    else TAIL_CHAIN_CONTROLS[index - 1])
        pose_bone = target.pose.bones[name]
        require(len(pose_bone.constraints) == 1,
                'tail控制约束图不符：' + name)
        constraint = pose_bone.constraints[0]
        require(constraint.type == 'COPY_ROTATION'
                and constraint.target == target
                and constraint.subtarget == expected
                and constraint.owner_space == 'LOCAL'
                and constraint.target_space == 'LOCAL'
                and constraint.mix_mode == 'BEFORE'
                and abs(constraint.influence - 1.0) < 1e-6
                and not constraint.mute,
                'tail控制机制约束不符：' + name)
    require(not (set(contract['tail_controls']) & set(CHANNEL_CONTROLS)),
            'tail控制不能进入动作写域')
    require(not (set(contract['tail_defs']) & set(CHANNEL_CONTROLS)),
            'tail DEF不能进入动作写域')


def _validate_correctives_target(target, contract):
    require(contract['correctives_on'], 'correctives契约未启用')
    require(set(contract['corrective_specs']) == set(CORRECTIVE_DEFS),
            'correctives规格集合不完整')
    for name, spec in contract['corrective_specs'].items():
        require(name in target.data.bones, 'correctives缺少辅助骨：' + name)
        bone = target.data.bones[name]
        require(bone.use_deform, 'correctives辅助骨不是形变骨：' + name)
        require(bone.bbone_segments == 1,
                'correctives辅助骨B-Bone段数不符：' + name)
        require(bone.parent is not None and bone.parent.name == spec['parent'],
                'correctives辅助骨父级不符：' + name)
        collections = {collection.name for collection in bone.collections}
        require(CORRECTIVE_COLLECTION in collections,
                'correctives辅助骨不在局部修正集合：' + name)
        head_error = max(abs(a - b) for a, b in zip(bone.head_local, spec['head']))
        tail_error = max(abs(a - b) for a, b in zip(bone.tail_local, spec['tail']))
        require(head_error < 1e-3 and tail_error < 1e-3,
                'correctives辅助骨rest位置与规格不符：' + name)
        pose_bone = target.pose.bones[name]
        require(len(pose_bone.constraints) == 1,
                'correctives辅助骨约束图不符：' + name)
        constraint = pose_bone.constraints[0]
        require(constraint.name == CORRECTIVE_CONSTRAINT_NAME
                and constraint.type == 'COPY_ROTATION'
                and constraint.target == target
                and constraint.subtarget == spec['source']
                and constraint.owner_space == 'LOCAL'
                and constraint.target_space == 'LOCAL'
                and constraint.mix_mode == 'OFFSET'
                and abs(constraint.influence - 0.5) < 1e-6
                and not constraint.mute,
                'correctives辅助骨机制约束不符：' + name)
        require(name not in CHANNEL_CONTROLS,
                'correctives辅助骨不能进入动作写域：' + name)


def _validate_shoulder_girdle_target(target):
    """Check the authored SC/AC/GH carrier graph without moving or driving it."""
    rows = []
    for side in ('L', 'R'):
        roles = ('shoulder', 'ORG-shoulder', 'DEF-shoulder', 'scapula',
                 'ORG-scapula', 'ORG-upper_arm', 'DEF-upper_arm', 'upper_arm_parent')
        names = {role: role + '.' + side for role in roles}
        require(all(name in target.pose.bones for name in names.values()),
                '肩带缺少真实控制/ORG/DEF骨：' + side)
        bones = {role: target.data.bones[name] for role, name in names.items()}
        parents = {
            'shoulder': 'ORG-spine.003', 'ORG-shoulder': 'ORG-spine.003',
            'DEF-shoulder': names['ORG-shoulder'],
            'scapula': names['ORG-shoulder'], 'ORG-scapula': names['ORG-shoulder'],
            'ORG-upper_arm': names['ORG-scapula'],
            'DEF-upper_arm': names['ORG-scapula'],
            'upper_arm_parent': names['ORG-scapula'],
        }
        for role, parent in parents.items():
            require(bones[role].parent is not None and bones[role].parent.name == parent,
                    '肩带父链不符：' + names[role])
            require(bones[role].use_deform == role.startswith('DEF-'),
                    '肩带控制/ORG/DEF职责不符：' + names[role])
        for org, control in (('ORG-shoulder', 'shoulder'), ('ORG-scapula', 'scapula')):
            constraints = target.pose.bones[names[org]].constraints
            require(len(constraints) == 1, '肩带ORG必须为唯一原生复制：' + names[org])
            constraint = constraints[0]
            require(constraint.type == 'COPY_TRANSFORMS'
                    and constraint.target == target and constraint.subtarget == names[control]
                    and constraint.owner_space == constraint.target_space == 'WORLD'
                    and constraint.mix_mode == 'REPLACE' and constraint.influence == 1.0
                    and not constraint.mute and constraint.head_tail == 0.0,
                    '肩带ORG复制目标/空间/影响不符：' + names[org])
        scapula = target.pose.bones[names['scapula']]
        require(not scapula.constraints, '手动肩胛控制不能含未适配约束：' + scapula.name)
        drivers = target.animation_data.drivers if target.animation_data else ()
        require(not any(fc.data_path.startswith(scapula.path_from_id() + '.')
                        or fc.data_path.startswith(scapula.path_from_id() + '[') for fc in drivers),
                '手动肩胛输入含未适配driver：' + scapula.name)
        # Tolerance is for coincident generated rest copies, not anatomical
        # length, ROM or skin quality. The policy fingerprint freezes all rest.
        tolerance = max(bones[r].length for r in ('shoulder', 'scapula', 'ORG-upper_arm')) * 1e-5
        ac, gh = bones['shoulder'].tail_local, bones['ORG-upper_arm'].head_local
        pairs = [
            (bones['shoulder'].head_local, bones['ORG-shoulder'].head_local),
            (ac, bones['ORG-shoulder'].tail_local), (ac, bones['DEF-shoulder'].tail_local),
            (ac, bones['scapula'].head_local), (ac, bones['ORG-scapula'].head_local),
            (bones['scapula'].tail_local, bones['ORG-scapula'].tail_local),
            (gh, bones['DEF-upper_arm'].head_local), (gh, bones['upper_arm_parent'].head_local),
        ]
        require(tolerance > 0 and all((a - b).length <= tolerance for a, b in pairs),
                '肩带SC/AC/GH实际rest副本不一致：' + side)
        require((gh - ac).length > tolerance and not bones['ORG-upper_arm'].use_connect
                and not bones['DEF-upper_arm'].use_connect,
                '必须保留AC到GH的非连接偏置：' + side)
        rows.append({'side': side, 'control': names['scapula'], 'mechanism': names['ORG-scapula'],
                     'SC_rest_local': list(bones['shoulder'].head_local), 'AC_rest_local': list(ac),
                     'GH_rest_local': list(gh), 'AC_to_GH_rest_local': list(gh - ac),
                     'parents': {names[role]: parent for role, parent in parents.items()}})
    return {'controls': list(SCAPULAR_CONTROLS), 'sides': rows,
            'source_semantics': 'NO_INDEPENDENT_SCAPULA_MAPPING', 'not_driven': True,
            'preservation': 'keep_current_input; shared_core_snapshots_restore_all_pose_channels',
            'automatic_rhythm': False}


def _validate_target_controls(target, contract=None):
    if contract is None:
        contract = {
            'tail_on': False,
            'correctives_on': False,
            'tail_defs': (),
            'tail_controls': (),
            'tail_master': None,
            'corrective_specs': {},
        }
    _world_check(target)
    require(target.mode != 'EDIT', '请结束目标骨架编辑模式')
    require(target.data.pose_position == 'POSE', '目标骨架必须使用POSE求值模式')
    require(target.animation_data is None or not target.animation_data.use_tweak_mode,
            '请退出目标NLA Tweak模式')
    require(not _object_conflicts(target),
            '目标对象含未适配父级/约束/链接，不能保持原控制器语义')
    require(not _driver_conflicts(target),
            '目标控制写域含driver，需单独适配')
    require(not _object_transform_action_rows(target),
            '目标对象含对象级位移动画，不能安全叠加根运动')
    missing_controls = [name for name in CONTROLS if name not in target.pose.bones]
    require(not missing_controls, '目标缺少模块化Rigify控制器：'
            + ', '.join(missing_controls))
    for name in CHANNEL_CONTROLS:
        pose_bone = target.pose.bones[name]
        require(pose_bone.rotation_mode in
                ('QUATERNION', 'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'),
                '不支持目标控制器rotation mode：' + name)
        if name in ROLL_CONTROLS:
            require(pose_bone.rotation_mode == 'ZXY',
                    'roll tweak必须为ZXY Euler：' + name)
            require(tuple(bool(value) for value in pose_bone.lock_rotation)
                    == (True, False, True)
                    and not pose_bone.lock_rotation_w,
                    'roll tweak必须只开放local Y旋转：' + name)
            require(pose_bone.parent is not None
                    and pose_bone.parent.name == ROLL_PARENTS[name],
                    'roll tweak父级机制不符：' + name)
        else:
            require(not any(pose_bone.lock_rotation)
                    and not pose_bone.lock_rotation_w,
                    '目标控制器旋转写域有作者锁，不绕过：' + name)
        if name == 'root':
            require(not any(pose_bone.lock_location),
                    '目标根控制器位移写域有作者锁，不绕过：' + name)
        require(not pose_bone.constraints,
                '目标控制器含约束，无法保持直接控制器语义：' + name)
        require(max(abs(value - 1.0) for value in pose_bone.scale) < 1e-5,
                '目标控制器已有缩放输入，首版不重写：' + name)
        if name != 'root':
            require(pose_bone.location.length < 1e-5,
                    '目标非根控制器已有位移输入，首版不重写：' + name)
    for name in MODE_CONTROLS:
        pose_bone = target.pose.bones[name]
        require(MODE_PROPERTY in pose_bone.keys(),
                '目标缺少IK_FK模式属性：' + name)
        value = pose_bone[MODE_PROPERTY]
        require(type(value) in (int, float) and math.isfinite(float(value)),
                '目标IK_FK模式属性不是有限数值：' + name)
    for name in EXPECTED_DEFS:
        require(name in target.data.bones, '目标缺少真实observed DEF：' + name)
        bone = target.data.bones[name]
        require(bone.use_deform, '目标observed不是真实形变骨：' + name)
    for name in LIMB_DEFS:
        require(1 <= target.data.bones[name].bbone_segments <= contract.get('bbones', 1),
                '目标四肢DEF的B-Bone分段与配置不符：' + name)
    mechanism = ('MCH-upper_arm_parent.L', 'MCH-upper_arm_parent.R',
                 'MCH-thigh_parent.L', 'MCH-thigh_parent.R',
                 'MCH-hand_fk.L', 'MCH-hand_fk.R',
                 'MCH-foot_fk.L', 'MCH-foot_fk.R', 'MCH-spine',
                 'MCH-ROT-neck', 'MCH-ROT-head', 'tweak_spine')
    missing_mechanism = [name for name in mechanism if name not in target.data.bones]
    require(not missing_mechanism,
            '目标不是完整的Rigify控制器机制，缺少：' + ', '.join(missing_mechanism))
    constraint_count = sum(len(pose_bone.constraints)
                           for pose_bone in target.pose.bones)
    require(constraint_count >= 50,
            '目标约束图不足以证明原生控制器机制，拒绝普通FK退化输出')
    if contract['tail_on']:
        _validate_tail_target(target, contract)
    else:
        require(not any(name in target.data.bones for name in TAIL_DEFS),
                '无tail模块但目标含DEF-tail骨')
        require(not any(name in target.pose.bones for name in (
            TAIL_CHAIN_CONTROLS + TAIL_MASTER_CANDIDATES + TAIL_TWEAK_CONTROLS)),
            '无tail模块但目标含Tail控制')
    if contract['correctives_on']:
        _validate_correctives_target(target, contract)
    else:
        require(not any(bone.name.startswith('DEF-CORR-')
                        for bone in target.data.bones),
                '无correctives模块但目标含DEF-CORR骨')
    if contract.get('shoulder_girdle_on'):
        return _validate_shoulder_girdle_target(target)
    else:
        require(not any(name in target.pose.bones for name in SCAPULAR_CONTROLS),
                '无shoulder_girdle模块但目标含肩胛载体')
    return None


def _plan_rows():
    return [
        {'semantic': semantic, 'control': control, 'observed': observed,
         'target_child': target_child, 'source_child': source_child}
        for semantic, control, observed, target_child, source_child in _PLAN_SPEC
    ]


def _neutral_property_values(target):
    return {
        'schema': PROPERTY_SCHEMA,
        'kind': KIND,
        'items': [
            {'bone': name, 'property': MODE_PROPERTY,
             'value': target.pose.bones[name][MODE_PROPERTY]}
            for name in MODE_CONTROLS
        ],
    }


def policy(target):
    """Create a signed, structural target policy without modifying the target."""
    _armature(target)
    metadata = _read_generated(target)
    contract = _validate_generated_metadata(metadata)
    shoulder_girdle_identity = _validate_target_controls(target, contract)
    summary = _metadata_summary(metadata)
    property_schema = _property_schema(target)
    constraints = _control_constraint_rows(target)
    fingerprint = _fingerprint(target)
    mapping = metadata['mechanism_mapping']
    extra_modules = list(contract['extra_modules'])
    extra_deforms = sorted(set(contract['expected_defs']) - set(EXPECTED_DEFS))
    protected_tweaks = sorted(
        pose_bone.name for pose_bone in target.pose.bones
        if 'tweak' in pose_bone.name.lower()
        and not pose_bone.name.startswith('MCH-')
        and pose_bone.name not in ROLL_CONTROLS)
    roll_controls = [
        {'semantic': semantic, 'control': control, 'observed': observed,
         'axis': ROLL_AXIS, 'euler_index': ROLL_EULER_INDEX,
         'parent': ROLL_PARENTS[control]}
        for semantic, control, observed in ROLL_SEMANTICS
    ]
    tail_identity = None
    if contract['tail_on']:
        tail_identity = {
            'deforms': list(contract['tail_defs']),
            'controls': list(contract['tail_controls']),
            'master': contract['tail_master'],
            'mechanism_mapping': {
                name: mapping[name] for name in contract['tail_defs']},
            'not_driven': True,
            'preservation': 'keep_current_value_or_old_action',
        }
    correctives_identity = None
    if contract['correctives_on']:
        correctives_identity = {
            'enabled': True,
            'status': contract['correctives_info'].get('status'),
            'module': contract['correctives_info'].get('module'),
            'bones': list(contract['corrective_defs']),
            'primary_deforms': list(contract['primary_defs']),
            'auxiliary_specs': [
                dict(contract['corrective_specs'][name])
                for name in sorted(contract['corrective_specs'])
            ],
            'constraints': [
                {
                    'bone': name,
                    'type': 'COPY_ROTATION',
                    'subtarget': contract['corrective_specs'][name]['source'],
                    'owner_space': 'LOCAL',
                    'target_space': 'LOCAL',
                    'mix_mode': 'OFFSET',
                    'influence': 0.5,
                }
                for name in sorted(contract['corrective_specs'])
            ],
            'natural_response': True,
            'explicit_channels': False,
        }
    record = {
        'schema': POLICY_SCHEMA,
        'kind': KIND,
        'version': POLICY_VERSION,
        'target': target.name,
        'target_mode': 'MODULAR_HUMAN_FK_CONTROLS',
        'generated_identity': summary,
        'metadata_sha256': _canonical_hash(summary),
        'target_object_unit': float(_world_check(target)),
        'controls': list(CONTROLS),
        'channel_controls': list(CHANNEL_CONTROLS),
        'roll_controls': roll_controls,
        'roll_axis': ROLL_AXIS,
        'roll_max_degrees': ROLL_MAX_DEGREES,
        'non_target_tweaks': protected_tweaks,
        'extra_modules': extra_modules,
        'extra_deforms': extra_deforms,
        'non_target_controls': list(contract['tail_controls']) + list(contract['scapular_controls']),
        'shoulder_girdle': shoulder_girdle_identity,
        'tail': tail_identity,
        'correctives': correctives_identity,
        'target_has_extra_modules': bool(extra_modules),
        'neutral_controls': ['torso', 'hips', 'chest'],
        'observed': dict(OBSERVED),
        'observed_by_control': {
            row['control']: row['observed'] for row in _plan_rows()
        },
        'semantic_plan': _plan_rows(),
        'property_domain': {
            'property': MODE_PROPERTY,
            'fk_value': FK_VALUE,
            'controls': list(MODE_CONTROLS),
            'write_semantics': 'Force native FK endpoint; Action channels cover the property.',
        },
        'mode_controls': list(MODE_CONTROLS),
        'ik_fk_write': [
            {'control': name, 'property': MODE_PROPERTY, 'value': FK_VALUE}
            for name in MODE_CONTROLS
        ],
        'property_schema': property_schema,
        'control_constraints': constraints,
        'fingerprint': fingerprint,
        'fingerprint_hash': _canonical_hash(fingerprint),
        'rest_reference': 'TARGET_OBSERVED_DEF_DATA_BONES_REST',
        'source_rest_reference': 'SOURCE_ARMATURE_DATA_BONES_REST',
        'body_frame_reference': 'SOURCE_REST_AND_TARGET_OBSERVED_DEF_REST',
        'translation_reference': 'CLIP_START',
        'motion_scale_basis': 'MEAN_WORLD_LEG_LENGTH',
        'limited_scopes': ['fingers', 'face', 'gaze', 'tail_motion',
                           'contact_correction'],
        'c94_identity': False,
        'body_only': True,
    }
    record['signature'] = _canonical_hash(record)
    return record


def validate(target, record):
    """Recompute the structural fingerprint without mutating the target."""
    require(isinstance(record, dict)
            and record.get('schema') == POLICY_SCHEMA
            and record.get('kind') == KIND
            and record.get('version') == POLICY_VERSION,
            '未知或损坏的模块化目标策略')
    require(record.get('signature') == _canonical_hash(
        {key: value for key, value in record.items() if key != 'signature'}),
        '目标策略记录签名不匹配，请重新建立')
    _armature(target)
    metadata = _read_generated(target)
    contract = _validate_generated_metadata(metadata)
    shoulder_girdle_identity = _validate_target_controls(target, contract)
    summary = _metadata_summary(metadata)
    require(_canonical_hash(summary) == record.get('metadata_sha256'),
            '模块化生成记录已改变，请重新建立目标策略')
    require(list(record.get('extra_modules', [])) == list(contract['extra_modules']),
            '目标额外模块记录已改变，请重新建立')
    require(list(record.get('extra_deforms', []))
            == sorted(set(contract['expected_defs']) - set(EXPECTED_DEFS)),
            '目标额外形变骨记录已改变，请重新建立')
    require(not (set(record['channel_controls'])
                 & set(record.get('non_target_controls', []))),
            '非动作目标控制不能进入动作写域')
    require(record.get('non_target_controls')
            == list(contract['tail_controls']) + list(contract['scapular_controls']),
            '非目标尾/肩胛控制保护域与实际模块不一致')
    if contract['shoulder_girdle_on']:
        require(record.get('shoulder_girdle') == shoulder_girdle_identity,
                '肩带实际结构或AC/GH偏置已改变，请重新建立目标策略')
    require(not (set(record['channel_controls'])
                 & set(record.get('non_target_tweaks', []))),
            '非目标tweak不能进入动作写域')
    for name in record.get('non_target_tweaks', []):
        require(name in target.pose.bones, '非目标tweak已不存在：' + name)
    for entry in record.get('roll_controls', []):
        require(entry.get('control') in record['channel_controls'],
                'roll控制必须在动作写域：' + str(entry.get('control')))
        require(entry.get('control') in target.pose.bones,
                'roll控制已不存在：' + str(entry.get('control')))
        require(entry.get('axis') == ROLL_AXIS
                and entry.get('euler_index') == ROLL_EULER_INDEX,
                'roll控制轴契约无效：' + str(entry.get('control')))
    recorded = record.get('fingerprint')
    require(isinstance(recorded, dict), '目标策略缺少控制图指纹')
    current = _fingerprint(target)
    checks = (
        ('rest_signature', '目标rest已改变，请重新转换'),
        ('constraint_signature', '目标约束控制图已改变，请重新转换'),
        ('property_schema_sha256', '目标控制自定义属性图已改变，请重新转换'),
        ('pose_mode_sha256', '目标控制rotation mode/锁已改变，请重新转换'),
        ('driver_rows_sha256', '目标控制driver已改变，请重新转换'),
        ('object_transform_action_sha256', '目标对象级动画已改变，请重新转换'),
        ('metadata_sha256', '模块化生成记录哈希已改变，请重新转换'),
        ('object_matrix', '目标对象变换已改变，请重新转换'),
        ('pose_position', '目标姿态求值模式已改变，请重新转换'),
        ('object_parent', '目标对象父级已改变，请重新转换'),
        ('object_conflicts', '目标对象约束/链接已改变，请重新转换'),
    )
    for key, message in checks:
        require(current[key] == recorded.get(key), message)
    require(_canonical_hash(current) == record.get('fingerprint_hash'),
            '目标策略控制图指纹不匹配，请重新建立')
    for name in record['controls']:
        require(name in target.pose.bones, '目标控制器已不存在：' + name)
    for name in record.get('non_target_controls', []):
        require(name in target.pose.bones, '非动作目标控制已不存在：' + name)
    for name in record['observed'].values():
        require(name in target.data.bones, '目标observed DEF已不存在：' + name)
        require(target.data.bones[name].use_deform,
                '目标observed不是形变骨：' + name)
    for entry in record['property_domain']['controls']:
        pose_bone = target.pose.bones[entry]
        require(MODE_PROPERTY in pose_bone.keys(),
                '目标IK_FK模式属性已缺失：' + entry)
    return record


def channels(target, record):
    """Return every native controller channel needed by the Action."""
    require(isinstance(record, dict) and record.get('kind') == KIND,
            '不是模块化人形目标策略')
    values = {}
    for name in record['channel_controls']:
        pose_bone = target.pose.bones[name]
        field = ('rotation_quaternion' if pose_bone.rotation_mode == 'QUATERNION'
                 else 'rotation_euler')
        for index, value in enumerate(getattr(pose_bone, field)):
            require(math.isfinite(float(value)),
                    '模块化控制器旋转产生非有限值：' + name)
            values[(pose_bone.path_from_id(field), index)] = float(value)
    root = target.pose.bones['root']
    for index, value in enumerate(root.location):
        require(math.isfinite(float(value)),
                '模块化根控制器位移产生非有限值')
        values[(root.path_from_id('location'), index)] = float(value)
    for entry in record['property_domain']['controls']:
        pose_bone = target.pose.bones[entry]
        value = pose_bone[MODE_PROPERTY]
        require(type(value) in (int, float) and math.isfinite(float(value)),
                '模块化IK_FK模式属性非有限：' + entry)
        path = pose_bone.path_from_id() + '[' + json.dumps(
            MODE_PROPERTY, ensure_ascii=False) + ']'
        values[(path, 0)] = float(value)
    return values


def snapshot_properties(target, record):
    """Capture the original mode-property values for shared-core restoration."""
    require(isinstance(record, dict) and record.get('kind') == KIND,
            '不是模块化人形目标策略')
    values = _neutral_property_values(target)
    for item in values['items']:
        require(type(item['value']) in (int, float)
                and math.isfinite(float(item['value'])),
                '目标IK_FK原值无效：' + item['bone'])
    return values


def restore_properties(target, values):
    """Restore the exact mode-property values captured before conversion."""
    require(isinstance(values, dict)
            and values.get('schema') == PROPERTY_SCHEMA
            and values.get('kind') == KIND,
            '模块化目标属性快照无效')
    for item in values.get('items', []):
        name = item.get('bone')
        prop = item.get('property')
        require(name in target.pose.bones, '恢复时目标控制器已不存在：' + str(name))
        pose_bone = target.pose.bones[name]
        require(prop in pose_bone.keys(), '恢复时目标属性已不存在：'
                + str(name) + '/' + str(prop))
        pose_bone[prop] = item['value']
    _update(target)


def _body_frame(values):
    """World-space body frame from hips/chest/thigh joint positions."""
    x = values['thigh.L'].translation - values['thigh.R'].translation
    z = values['chest'].translation - values['hips'].translation
    require(x.length > 1e-7 and z.length > 1e-7, '人体REST身体框架退化')
    x.normalize()
    z -= x * z.dot(x)
    require(z.length > 1e-7, '人体REST身体框架共线')
    z.normalize()
    y = z.cross(x)
    require(y.length > 1e-7, '人体REST身体框架右手系退化')
    y.normalize()
    x = y.cross(z).normalized()
    return Matrix((x, y, z)).transposed()


def _roll_axis(matrix, direction):
    """Choose a stable local roll axis perpendicular to the anatomical Y."""
    y = direction.normalized()
    for axis in (Vector((1.0, 0.0, 0.0)),
                 Vector((0.0, 0.0, 1.0)),
                 Vector((0.0, 1.0, 0.0))):
        reference = matrix.to_3x3() @ axis
        reference -= y * reference.dot(y)
        if reference.length > 1e-5:
            return axis
    return None


def _anatomical_quotient(matrix, direction, roll_axis):
    """Full anatomical frame: Y follows joint direction, roll comes from source."""
    y = direction.normalized()
    if y.length <= 1e-8:
        raise ValueError('源解剖方向退化')
    axis = roll_axis
    reference = None
    if axis is not None:
        reference = matrix.to_3x3() @ axis
        reference -= y * reference.dot(y)
        if reference.length <= 1e-6:
            reference = None
    if reference is None:
        for candidate in (Vector((1.0, 0.0, 0.0)),
                          Vector((0.0, 0.0, 1.0)),
                          Vector((0.0, 1.0, 0.0))):
            reference = matrix.to_3x3() @ candidate
            reference -= y * reference.dot(y)
            if reference.length > 1e-5:
                break
            reference = None
    if reference is None:
        reference = y.orthogonal()
        reference -= y * reference.dot(y)
    z = reference.normalized()
    x = y.cross(z)
    if x.length <= 1e-8:
        reference = y.orthogonal()
        z = (reference - y * reference.dot(y)).normalized()
        x = y.cross(z)
    x.normalize()
    z = x.cross(y).normalized()
    return Matrix((x, y, z)).transposed().to_quaternion().normalized()


def _control_depth(target, name):
    depth = 0
    bone = target.data.bones[name]
    while bone.parent is not None:
        depth += 1
        bone = bone.parent
    return depth


class Adapter:
    """Drive the native modular Rigify controllers from a resolved sample.

    The constructor resets only the declared controller write domain and the
    four limb modes to their FK endpoint.  The shared core owns the surrounding
    snapshot/restore and the Action/slot lifecycle.
    """

    def __init__(self, source, target, mapped, record, *, motion='PRESERVE'):
        validate(target, record)
        require(motion in ('PRESERVE', 'IN_PLACE'), '根运动模式无效')
        from . import retarget_source as source_api
        source_api.armature(source)
        source_api.armature(target)
        self.source = source
        self.target = target
        self.record = record
        self.motion = motion
        bones = mapped.get('bones') if isinstance(mapped, dict) and 'bones' in mapped else mapped
        require(isinstance(bones, dict) and bones, '源映射必须提供bones语义表')
        self.mapping = dict(bones)
        missing = [name for name in REQUIRED_SOURCE_LIMBS
                   if name not in self.mapping]
        require(not missing, '只支持全四肢HUMAN源映射，缺少：'
                + ', '.join(missing))
        for semantic in ('hips', 'chest', 'head'):
            require(semantic in self.mapping, '源映射缺少身体语义：' + semantic)
        require(all(name in source.data.bones for name in self.mapping.values()),
                '源映射含不存在骨')
        self.source_calibration = None
        self.source_profile = None
        if isinstance(mapped, dict):
            calibration_record = mapped.get('modular_source_calibration')
            if calibration_record is not None:
                require(isinstance(calibration_record, dict),
                        'modular_source_calibration必须是对象')
                require(calibration_record.get('schema')
                        == 'rigging.modular.source_calibration.v1',
                        'modular_source_calibration schema无效')
                require(calibration_record.get('orientation_reference') == 'REST',
                        '模块化Profile桥只接受REST朝向参考')
                require(calibration_record.get('translation_origin') == 'CLIP_START',
                        '模块化Profile桥当前只支持CLIP_START位移原点；'
                        '不会静默忽略：' + str(calibration_record.get('translation_origin')))
                require(calibration_record.get('motion_scale_basis') == 'LEG_LENGTH',
                        '模块化Profile桥当前只支持LEG_LENGTH比例；'
                        '不会静默忽略：' + str(calibration_record.get('motion_scale_basis')))
                require(calibration_record.get('unit_division')
                        == {'source': 0, 'target': 1},
                        '模块化Profile桥只接受source=0,target=1 unit除法')
                self.source_calibration = json.loads(
                    json.dumps(calibration_record, ensure_ascii=False))
            profile_record = mapped.get('modular_profile')
            if profile_record is not None:
                require(isinstance(profile_record, dict),
                        'modular_profile必须是对象')
                self.source_profile = json.loads(
                    json.dumps(profile_record, ensure_ascii=False))

        self.names = list(record['channel_controls'])
        self.extra_modules = tuple(record.get('extra_modules', ()))
        self.extra_deforms = tuple(record.get('extra_deforms', ()))
        self.non_target_controls = tuple(record.get('non_target_controls', ()))
        self.finger_chains = []
        self.palm_calibration_sides = []
        self.rows = []
        for spec in record['semantic_plan']:
            if spec['semantic'] not in self.mapping:
                continue
            row = dict(spec)
            row['control_depth'] = _control_depth(target, row['control'])
            self.rows.append(row)
        self.rows.sort(key=lambda row: (row['control_depth'],
                                        row['control'], row['semantic']))
        self.roll_rows = []
        for entry in record.get('roll_controls', []):
            semantic = entry.get('semantic')
            control = entry.get('control')
            observed = entry.get('observed')
            row = next((item for item in self.rows
                        if item['semantic'] == semantic), None)
            require(row is not None and row['observed'] == observed,
                    'roll语义与observed行不符：' + str(semantic))
            require(control in target.pose.bones,
                    'roll控制不在目标骨架：' + str(control))
            require(entry.get('axis') == ROLL_AXIS
                    and entry.get('euler_index') == ROLL_EULER_INDEX,
                    'roll控制轴契约无效：' + str(control))
            self.roll_rows.append({
                'semantic': semantic,
                'control': control,
                'observed': observed,
                'axis': ROLL_AXIS,
                'euler_index': ROLL_EULER_INDEX,
                'parent': entry.get('parent'),
            })
        self.roll_by_semantic = {
            row['semantic']: row for row in self.roll_rows}
        self.last_roll = []
        self.roll_history = []
        self.roll_max_before = 0.0
        self.roll_max_after = 0.0
        self.roll_max_applied = 0.0
        self.observed = {row['semantic']: row['observed'] for row in self.rows}
        self.observed_by_control = {row['control']: row['observed']
                                    for row in self.rows}
        self.skipped_semantics = {
            spec['semantic']: {'reason': 'source_mapping_missing'}
            for spec in record['semantic_plan']
            if spec['semantic'] not in self.mapping
        }

        self.source_rest = {
            semantic: source.matrix_world @ source.data.bones[name].matrix_local
            for semantic, name in self.mapping.items()
        }
        self.target_rest = {
            semantic: target.matrix_world @ target.data.bones[name].matrix_local
            for semantic, name in record['observed'].items()
        }
        self.target_rest_by_def = {
            name: target.matrix_world @ target.data.bones[name].matrix_local
            for name in EXPECTED_DEFS
        }
        self.root_rest = target.matrix_world @ target.data.bones['root'].matrix_local
        self.source_leg = {
            side: ((self.source_rest['shin.' + side].translation
                    - self.source_rest['thigh.' + side].translation).length
                   + (self.source_rest['foot.' + side].translation
                      - self.source_rest['shin.' + side].translation).length)
            for side in ('L', 'R')
        }
        self.target_leg = {
            side: ((self.target_rest['shin.' + side].translation
                    - self.target_rest['thigh.' + side].translation).length
                   + (self.target_rest['foot.' + side].translation
                      - self.target_rest['shin.' + side].translation).length)
            for side in ('L', 'R')
        }
        require(all(value > 1e-8 for value in self.source_leg.values()),
                '源REST腿长退化')
        require(all(value > 1e-8 for value in self.target_leg.values()),
                '目标observed REST腿长退化')
        self.leg_ratios = {
            side: self.target_leg[side] / self.source_leg[side]
            for side in ('L', 'R')
        }
        self.scale = sum(self.leg_ratios.values()) / 2.0
        require(math.isfinite(self.scale) and self.scale > 0,
                '无法从REST计算腿长位移比例')
        self.source_frame = _body_frame(self.source_rest)
        self.target_frame = _body_frame(self.target_rest)
        self.global_alignment = (
            self.source_frame @ self.target_frame.inverted()).to_4x4()
        self.global_alignment_q = self.global_alignment.to_quaternion()
        self.global_alignment_degrees = math.degrees(
            abs(self.global_alignment_q.angle))
        self.calibration = {}
        self.joint_calibration = {}
        self.skipped_joint_references = {}
        self.rest_swing_degrees = {}
        self.source_anatomical_rest = {}
        self.direction_references = {}
        for row in self.rows:
            semantic = row['semantic']
            target_rest_q = self.target_rest[semantic].to_quaternion()
            source_child = row.get('source_child')
            target_child = row.get('target_child')
            mode = None
            local_offset = None
            source_reference = None
            if source_child and source_child in self.mapping:
                mode = 'mapped_child'
                source_reference = source_child
                source_direction = (
                    self.source_rest[source_child].translation
                    - self.source_rest[semantic].translation)
            elif source_child:
                self.skipped_joint_references[semantic] = {
                    'target_child': target_child,
                    'source_child': source_child,
                    'reason': 'source_child_semantic_missing',
                }
            if mode is None:
                pseudo = self._pseudo_source_child(semantic)
                if pseudo is not None:
                    mode, source_reference, local_offset = (
                        'pseudo_child', pseudo[0], pseudo[1])
                    source_direction = (
                        self.source_rest[semantic].to_3x3()
                        @ local_offset)
                else:
                    mode = 'bone_y'
                    source_reference = self.mapping[semantic]
                    source_direction = (
                        self.source_rest[semantic].to_3x3().col[1])
            if source_direction.length <= 1e-7:
                self.skipped_joint_references[semantic] = {
                    'target_child': target_child,
                    'source_child': source_reference,
                    'reason': 'coincident_source_rest_reference',
                }
                mode = 'bone_y'
                source_reference = self.mapping[semantic]
                local_offset = None
                source_direction = self.source_rest[semantic].to_3x3().col[1]
            require(source_direction.length > 1e-7,
                    '源REST解剖方向退化：' + semantic)
            source_direction = source_direction.normalized()
            target_direction = (
                self.target_rest_by_def[target_child].translation
                - self.target_rest[semantic].translation
                if target_child in self.target_rest_by_def
                else self.target_rest[semantic].to_3x3().col[1]
            )
            require(target_direction.length > 1e-7,
                    '目标REST参考方向退化：' + semantic)
            target_direction = target_direction.normalized()
            desired_direction = (
                self.global_alignment.inverted().to_3x3()
                @ source_direction).normalized()
            swing = target_direction.rotation_difference(desired_direction)
            roll_axis = _roll_axis(self.source_rest[semantic], source_direction)
            anatomical_rest = _anatomical_quotient(
                self.source_rest[semantic], source_direction, roll_axis)
            self.source_anatomical_rest[semantic] = anatomical_rest
            self.rest_swing_degrees[semantic] = math.degrees(
                abs(swing.angle))
            tail_to_joint = _direction_angle_degrees(
                self.source_rest[semantic].to_3x3().col[1],
                source_direction)
            self.joint_calibration[semantic] = {
                'target_child': target_child,
                'source_child': source_reference,
                'source_reference_mode': mode,
                'source_tail_to_joint_degrees': tail_to_joint,
                'calibration_swing_degrees': self.rest_swing_degrees[semantic],
            }
            self.direction_references[semantic] = {
                'mode': mode,
                'source_child': source_reference,
                'local_offset': local_offset.copy() if local_offset is not None else None,
                'roll_axis': roll_axis.copy() if roll_axis is not None else None,
                'source_rest_direction': source_direction.copy(),
                'source_anatomical_rest': anatomical_rest.copy(),
                'target_reference_direction': target_direction.copy(),
            }
            self.calibration[semantic] = (swing @ target_rest_q).normalized()

        self.previous = {}
        self.last_diagnostics = []
        self.last_sweeps = 0
        self.max_nonroot_basis_location = 0.0
        self.max_basis_scale_error = 0.0
        self.joint_residual_max = 0.0
        # Full-sample accounting.  The shared core calls solve() once per
        # sampled frame; report_fields() must never expose only the last frame.
        self.history_frames = 0
        self.history_max_rotation = 0.0
        self.history_max_direction = 0.0
        self.history_max_twist = 0.0
        self.history_max_joint = 0.0
        self.history_by_semantic = {}
        self.history_worst_rotation = []
        self.history_worst_direction = []
        self.history_worst_twist = []
        self.mode_values = {name: FK_VALUE for name in MODE_CONTROLS}
        self._reset_target_domain()
        self._set_fk_modes()
        self.neutral = {
            name: target.pose.bones[name].matrix.copy() for name in self.names
        }
        self.root_neutral = target.pose.bones['root'].matrix.copy()
        evaluated = _update(target)
        self.neutral_observed = {
            semantic: (target.matrix_world
                       @ evaluated.pose.bones[def_name].matrix).copy()
            for semantic, def_name in self.observed.items()
        }
        self.rest_neutral_degrees = 0.0
        self.rest_neutral_position = 0.0
        for semantic, def_name in self.observed.items():
            rest = self.target_rest[semantic]
            neutral = self.neutral_observed[semantic]
            self.rest_neutral_degrees = max(
                self.rest_neutral_degrees, _angle_degrees(rest, neutral))
            self.rest_neutral_position = max(
                self.rest_neutral_position,
                (rest.translation - neutral.translation).length)
        require(self.rest_neutral_degrees < 1e-2
                and self.rest_neutral_position < 1e-4,
                '目标控制器零姿态与observed DEF REST不一致：角度%.6f度，位置%.8f'
                % (self.rest_neutral_degrees, self.rest_neutral_position))

        scene = bpy.context.scene
        evaluated_source = source.evaluated_get(
            bpy.context.evaluated_depsgraph_get())
        self.source_origin_frame = float(
            scene.frame_current + scene.frame_subframe)
        self.source_origin = (
            evaluated_source.matrix_world
            @ evaluated_source.pose.bones[self.mapping['hips']].matrix
        ).translation.copy()
        self.source_clip_start_to_rest_degrees = {}
        for semantic in self.observed:
            current = (evaluated_source.matrix_world
                       @ evaluated_source.pose.bones[self.mapping[semantic]].matrix)
            self.source_clip_start_to_rest_degrees[semantic] = _angle_degrees(
                self.source_rest[semantic], current)
        self.source_clip_start_max_degrees = max(
            self.source_clip_start_to_rest_degrees.values(), default=0.0)
        self.target_object_unit = float(
            target.matrix_world.to_3x3().col[0].length)
        if self.source_calibration is not None:
            require(abs(self.target_object_unit - 1.0) <= 1e-6,
                    '模块化Profile桥要求目标对象unit=1.0，实测%.9f'
                    % self.target_object_unit)
            origin_frame = self.source_calibration.get('source_origin_frame')
            if origin_frame is not None:
                require(abs(self.source_origin_frame - float(origin_frame)) <= 1e-6,
                        '模块化Profile桥CLIP_START帧与source_origin_frame不符')
            origin_world = self.source_calibration.get('source_origin_world')
            if origin_world is not None:
                expected = Vector(origin_world)
                require((self.source_origin - expected).length <= 1e-3,
                        '模块化Profile桥CLIP_START髋位与profile记录不符：%.8f'
                        % (self.source_origin - expected).length)

    def _reset_target_domain(self):
        target = self.target
        for name in self.names:
            pose_bone = target.pose.bones[name]
            pose_bone.location = (0.0, 0.0, 0.0)
            if pose_bone.rotation_mode == 'QUATERNION':
                pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            else:
                pose_bone.rotation_euler = (0.0, 0.0, 0.0)
            pose_bone.scale = (1.0, 1.0, 1.0)
        _update(target)

    def _set_fk_modes(self):
        changed = False
        for name, value in self.mode_values.items():
            pose_bone = self.target.pose.bones[name]
            if float(pose_bone[MODE_PROPERTY]) != float(value):
                pose_bone[MODE_PROPERTY] = float(value)
                changed = True
        if changed:
            _update(self.target)

    def _pseudo_source_child(self, semantic):
        """Pick a real descendant joint for terminal hand/head/toe semantics."""
        bone = self.source.data.bones[self.mapping[semantic]]
        children = sorted(bone.children, key=lambda item: item.name)
        if not children:
            return None
        wanted = ()
        if semantic.startswith('hand.'):
            wanted = ('Middle1', 'Index1')
        elif semantic == 'head':
            wanted = ('Top_End',)
        elif semantic.startswith('toes.'):
            wanted = ('Toe_End', 'End')
        selected = None
        for token in wanted:
            for child in children:
                if token.lower() in child.name.lower():
                    selected = child
                    break
            if selected is not None:
                break
        if selected is None:
            selected = children[0]
        offset = bone.matrix_local.inverted() @ selected.head_local
        if offset.length <= 1e-7:
            return None
        return selected.name, offset

    def _current_source_direction(self, sample, semantic):
        reference = self.direction_references[semantic]
        mode = reference['mode']
        if mode == 'mapped_child':
            child = reference['source_child']
            return sample[child].translation - sample[semantic].translation
        if mode == 'pseudo_child':
            return sample[semantic].to_3x3() @ reference['local_offset']
        return sample[semantic].to_3x3().col[1]

    def _desired_orientation(self, sample, semantic):
        reference = self.direction_references[semantic]
        current_direction = self._current_source_direction(sample, semantic)
        if current_direction.length <= 1e-7:
            current_direction = reference['source_rest_direction']
        anatomical_current = _anatomical_quotient(
            sample[semantic], current_direction, reference['roll_axis'])
        source_delta = (
            anatomical_current
            @ reference['source_anatomical_rest'].inverted()
        ).normalized()
        target_delta = (
            self.global_alignment_q.inverted()
            @ source_delta
            @ self.global_alignment_q
        ).normalized()
        return (target_delta @ self.calibration[semantic]).normalized()

    def _apply_continuity(self, pose_bone):
        name = pose_bone.name
        previous = self.previous.get(name)
        if pose_bone.rotation_mode == 'QUATERNION':
            rotation = pose_bone.rotation_quaternion.copy()
            if previous is not None and rotation.dot(previous) < 0:
                rotation.negate()
                pose_bone.rotation_quaternion = rotation
            self.previous[name] = rotation.copy()
        else:
            rotation = pose_bone.rotation_euler.to_quaternion()
            if previous is not None:
                rotation = rotation.to_euler(pose_bone.rotation_mode, previous)
            else:
                rotation = rotation.to_euler(pose_bone.rotation_mode)
            pose_bone.rotation_euler = rotation
            self.previous[name] = rotation.copy()

    def _set_world_matrix(self, pose_bone, world_matrix):
        armature_matrix = self.target.matrix_world.inverted() @ world_matrix
        pose_bone.matrix = armature_matrix
        self._apply_continuity(pose_bone)
        if pose_bone.name == 'root':
            return
        self.max_nonroot_basis_location = max(
            self.max_nonroot_basis_location, pose_bone.location.length)
        self.max_basis_scale_error = max(
            self.max_basis_scale_error,
            max(abs(value - 1.0) for value in pose_bone.scale))
        require(pose_bone.location.length < 1e-4,
                '控制器旋转无法以零位移表示，拒绝写非根位移：'
                + pose_bone.name + ' %.8f' % pose_bone.location.length)
        require(max(abs(value - 1.0) for value in pose_bone.scale) < 1e-4,
                '控制器旋转无法以单位尺度表示：' + pose_bone.name)

    def _solve_root(self, sample):
        displacement = sample['hips'].translation - self.source_origin
        displacement = (self.global_alignment.inverted().to_3x3()
                        @ displacement) * self.scale
        if self.motion == 'IN_PLACE':
            displacement.x = 0.0
            displacement.y = 0.0
        desired = self.root_rest.copy()
        desired.translation = self.root_rest.translation + displacement
        root = self.target.pose.bones['root']
        self._set_world_matrix(root, desired)
        _update(self.target)

    def _solve_row(self, row, sample):
        desired = self._desired_orientation(sample, row['semantic'])
        evaluated = _update(self.target)
        observed_world = (
            self.target.matrix_world
            @ evaluated.pose.bones[row['observed']].matrix)
        control_world = (
            self.target.matrix_world
            @ self.target.pose.bones[row['control']].matrix)
        current_observed = observed_world.to_quaternion()
        current_control = control_world.to_quaternion()
        desired_control = (
            desired @ current_observed.inverted() @ current_control
        ).normalized()
        desired_world = desired_control.to_matrix().to_4x4()
        desired_world.translation = control_world.translation
        self._set_world_matrix(self.target.pose.bones[row['control']],
                               desired_world)
        _update(self.target)
        return _angle_degrees(desired, current_observed)

    def _solve_roll(self, row, sample):
        """Single-axis local-Y compensation on an existing native roll tweak."""
        semantic = row['semantic']
        control = row['control']
        observed = row['observed']
        pose_bone = self.target.pose.bones[control]
        require(pose_bone.rotation_mode == 'ZXY',
                'roll tweak必须为ZXY Euler：' + control)
        desired = self._desired_orientation(sample, semantic)
        evaluated = _update(self.target)
        actual = (self.target.matrix_world
                  @ evaluated.pose.bones[observed].matrix)
        before_rotation = _angle_degrees(desired, actual)
        before_twist = _signed_twist_degrees(desired, actual)
        before_direction = _direction_angle_degrees(
            actual.to_3x3().col[1], desired.to_matrix().col[1])
        old_euler = pose_bone.rotation_euler.copy()
        detail = {
            'semantic': semantic,
            'control': control,
            'observed': observed,
            'axis': ROLL_AXIS,
            'before_rotation_deg': before_rotation,
            'before_twist_deg': before_twist,
            'before_direction_deg': before_direction,
        }
        if abs(before_twist) < 1e-3:
            detail.update({
                'accepted': True, 'applied_deg': 0.0, 'gain': None,
                'after_rotation_deg': before_rotation,
                'after_twist_deg': before_twist,
                'after_direction_deg': before_direction,
                'reason': 'already_zero',
            })
            self.last_roll.append(detail)
            return detail
        # Measure the sign/gain of the native local-Y roll DOF.
        probe_deg = 1.0
        pose_bone.rotation_euler[ROLL_EULER_INDEX] = (
            old_euler[ROLL_EULER_INDEX] + math.radians(probe_deg))
        self._apply_continuity(pose_bone)
        evaluated = _update(self.target)
        probed = (self.target.matrix_world
                  @ evaluated.pose.bones[observed].matrix)
        probed_twist = _signed_twist_degrees(desired, probed)
        gain = probed_twist - before_twist
        pose_bone.rotation_euler = old_euler
        self._apply_continuity(pose_bone)
        _update(self.target)
        if abs(gain) < 0.05:
            detail.update({
                'accepted': False, 'applied_deg': 0.0,
                'gain': gain, 'after_rotation_deg': before_rotation,
                'after_twist_deg': before_twist,
                'after_direction_deg': before_direction,
                'reason': 'no_roll_dof',
            })
            self.last_roll.append(detail)
            return detail
        delta = -before_twist / gain
        delta = max(-ROLL_MAX_DEGREES, min(ROLL_MAX_DEGREES, delta))
        pose_bone.rotation_euler[ROLL_EULER_INDEX] = (
            old_euler[ROLL_EULER_INDEX] + math.radians(delta))
        self._apply_continuity(pose_bone)
        evaluated = _update(self.target)
        after = (self.target.matrix_world
                 @ evaluated.pose.bones[observed].matrix)
        after_rotation = _angle_degrees(desired, after)
        after_twist = _signed_twist_degrees(desired, after)
        after_direction = _direction_angle_degrees(
            after.to_3x3().col[1], desired.to_matrix().col[1])
        accepted = (after_rotation <= before_rotation + 0.25
                    and after_direction <= before_direction + 0.5)
        if not accepted:
            pose_bone.rotation_euler = old_euler
            self._apply_continuity(pose_bone)
            _update(self.target)
            after_rotation = before_rotation
            after_twist = before_twist
            after_direction = before_direction
            delta = 0.0
        detail.update({
            'accepted': accepted,
            'applied_deg': delta,
            'gain': gain,
            'after_rotation_deg': after_rotation,
            'after_twist_deg': after_twist,
            'after_direction_deg': after_direction,
            'reason': None if accepted else 'rejected_worse_rotation_or_direction',
        })
        self.last_roll.append(detail)
        self.roll_max_before = max(self.roll_max_before, before_rotation)
        self.roll_max_after = max(self.roll_max_after, after_rotation)
        self.roll_max_applied = max(self.roll_max_applied, abs(delta))
        self.roll_history.append(dict(detail))
        return detail

    def solve(self, sample):
        # Solve every frame from the declared neutral write domain.  Carrying
        # the previous frame's FK controls through the native 50/50 spine pivot
        # can select a 180-degree roll branch in MCH-pivot and make the whole
        # remaining clip stay on that branch.  Resetting only the declared
        # channel controls (tail/correctives are outside the domain) makes each
        # frame deterministic without removing any channel or constraint.
        self._reset_target_domain()
        self.last_roll = []
        self._set_fk_modes()
        self._solve_root(sample)
        sweeps = 0
        pass_changes = []
        for _ in range(2):
            changes = []
            for row in self.rows:
                changes.append(self._solve_row(row, sample))
                if _ == 1 and row['semantic'] in self.roll_by_semantic:
                    self._solve_roll(
                        self.roll_by_semantic[row['semantic']], sample)
            sweeps += 1
            pass_changes.append(max(changes, default=0.0))
            if max(changes, default=0.0) < 1e-4:
                break
        self.last_sweeps = sweeps
        evaluated = _update(self.target)
        diagnostics = []
        for row in self.rows:
            semantic = row['semantic']
            desired = self._desired_orientation(sample, semantic)
            actual = (
                self.target.matrix_world
                @ evaluated.pose.bones[row['observed']].matrix)
            rotation_degrees = _angle_degrees(desired, actual)
            direction_degrees = _direction_angle_degrees(
                actual.to_3x3().col[1], desired.to_matrix().col[1])
            row_report = {
                'semantic': semantic,
                'control': row['control'],
                'observed': row['observed'],
                'rotation_degrees': min(rotation_degrees, 360.0 - rotation_degrees),
                'direction_degrees': direction_degrees,
                'twist_degrees': _twist_angle_degrees(desired, actual),
            }
            child = row.get('target_child')
            source_child = row.get('source_child')
            if child and source_child and source_child in self.mapping:
                expected = (
                    self.global_alignment.inverted().to_3x3()
                    @ (sample[source_child].translation
                       - sample[semantic].translation))
                actual_direction = (
                    evaluated.pose.bones[child].head
                    - evaluated.pose.bones[row['observed']].head)
                actual_direction_world = (
                    self.target.matrix_world.to_3x3() @ actual_direction)
                joint_degrees = _direction_angle_degrees(
                    actual_direction_world, expected)
                row_report['joint_direction_degrees'] = joint_degrees
                self.joint_residual_max = max(
                    self.joint_residual_max, joint_degrees)
            diagnostics.append(row_report)
        self.last_diagnostics = diagnostics
        self.last_pass_changes = pass_changes
        self.history_frames += 1
        scene = bpy.context.scene
        sampled_frame = float(scene.frame_current + scene.frame_subframe)
        for row in diagnostics:
            item = dict(row)
            item['frame'] = sampled_frame
            semantic = item['semantic']
            entry = self.history_by_semantic.setdefault(semantic, {
                'rotation_degrees': 0.0,
                'direction_degrees': 0.0,
                'twist_degrees': 0.0,
                'joint_direction_degrees': 0.0,
            })
            for key in ('rotation_degrees', 'direction_degrees', 'twist_degrees',
                        'joint_direction_degrees'):
                value = float(item.get(key, 0.0))
                entry[key] = max(entry[key], value)
            self.history_max_rotation = max(
                self.history_max_rotation, float(item['rotation_degrees']))
            self.history_max_direction = max(
                self.history_max_direction, float(item['direction_degrees']))
            self.history_max_twist = max(
                self.history_max_twist, float(item.get('twist_degrees', 0.0)))
            self.history_max_joint = max(
                self.history_max_joint,
                float(item.get('joint_direction_degrees', 0.0)))
            self.history_worst_rotation = sorted(
                self.history_worst_rotation + [dict(item)],
                key=lambda value: value['rotation_degrees'],
                reverse=True)[:24]
            self.history_worst_direction = sorted(
                self.history_worst_direction + [dict(item)],
                key=lambda value: value['direction_degrees'],
                reverse=True)[:24]
            self.history_worst_twist = sorted(
                self.history_worst_twist + [dict(item)],
                key=lambda value: value.get('twist_degrees', 0.0),
                reverse=True)[:24]
        return diagnostics

    def report_fields(self):
        rotation_by_semantic = {
            semantic: entry['rotation_degrees']
            for semantic, entry in self.history_by_semantic.items()
        }
        direction_by_semantic = {
            semantic: entry['direction_degrees']
            for semantic, entry in self.history_by_semantic.items()
        }
        twist_by_semantic = {
            semantic: entry['twist_degrees']
            for semantic, entry in self.history_by_semantic.items()
        }
        last_rotation = max(
            (row['rotation_degrees'] for row in self.last_diagnostics),
            default=0.0)
        last_direction = max(
            (row['direction_degrees'] for row in self.last_diagnostics),
            default=0.0)
        last_twist = max(
            (row.get('twist_degrees', 0.0) for row in self.last_diagnostics),
            default=0.0)
        last_joint = max(
            (row.get('joint_direction_degrees', 0.0)
             for row in self.last_diagnostics), default=0.0)
        neutral_hash = _canonical_hash({
            name: _matrix_list(matrix)
            for name, matrix in sorted(self.neutral.items())
        })
        return {
            'schema': REPORT_SCHEMA,
            'adapter': KIND,
            'target_mode': 'MODULAR_HUMAN_FK_CONTROLS',
            'controller_animation': True,
            'motion': self.motion,
            'target_policy': self.record,
            'target_policy_schema': POLICY_SCHEMA,
            'target_policy_kind': KIND,
            'generated_identity': self.record['generated_identity'],
            'source_profile': getattr(self, 'source_profile', None),
            'source_calibration': getattr(self, 'source_calibration', None),
            'profile_calibration_reuse': (
                False if getattr(self, 'source_calibration', None) is not None
                else None),
            'finger_policy': (
                'NOT_APPLICABLE_MODULAR_TARGET_HAS_NO_FINGER_CONTROLS'
                if getattr(self, 'source_profile', None) is not None
                else 'NO_FINGER_CONTROLS'),
            'controls': self.names,
            'all_controls': list(self.record['controls']),
            'channel_controls': list(self.names),
            'extra_modules': list(self.extra_modules),
            'extra_deforms': list(self.extra_deforms),
            'non_target_controls': list(self.non_target_controls),
            'non_target_tweaks': list(self.record.get('non_target_tweaks', [])),
            'roll_controls': list(self.roll_rows),
            'roll_axis': ROLL_AXIS,
            'roll_max_applied_degrees': self.roll_max_applied,
            'roll_history_max_before_degrees': self.roll_max_before,
            'roll_history_max_after_degrees': self.roll_max_after,
            'roll_samples': len(self.roll_history),
            'roll_last_frame': list(self.last_roll),
            'tail': self.record.get('tail'),
            'correctives': self.record.get('correctives'),
            'shoulder_girdle': self.record.get('shoulder_girdle'),
            'tail_policy': (
                'NOT_TARGET_PRESERVE_CURRENT_OR_OLD_ACTION'
                if 'tail' in self.extra_modules else None),
            'correctives_policy': (
                'NATIVE_LOCAL_COPY_ROTATION_NO_EXPLICIT_CHANNELS'
                if 'correctives' in self.extra_modules else None),
            'tail_control_channels_written': False,
            'corrective_bone_channels_written': False,
            'corrective_constraints_removed': False,
            'mode_controls': list(MODE_CONTROLS),
            'solve_order': [
                {'semantic': row['semantic'], 'control': row['control'],
                 'observed': row['observed'],
                 'control_depth': row['control_depth']}
                for row in self.rows
            ],
            'mode_properties': [
                {'control': name, 'property': MODE_PROPERTY, 'value': FK_VALUE}
                for name in MODE_CONTROLS
            ],
            'ik_fk_semantics': '1.0 = native FK endpoint',
            'observed_bones': dict(self.observed),
            'observed_by_control': dict(self.observed_by_control),
            'finger_chains': [],
            'palm_calibration_sides': [],
            'include_fingers': False,
            'finger_conversion': False,
            'face_conversion': False,
            'gaze_conversion': False,
            'contact_correction': False,
            'finger_orientation_reference': None,
            'finger_input_allocation': None,
            'skipped_fingers': {'all': 'unsupported_no_fingers'},
            'spine_distribution': (
                'native spine_fk chain plus neck/head; torso/hips/chest '
                'kept neutral'),
            'contact_detection': {
                'status': 'not_run',
                'reason': 'modular_human_body_only_adapter',
                'hand_contact': 'not_analyzed',
                'body_contact': 'not_analyzed',
                'automatic_correction': False,
            },
            'body_frame_alignment': _matrix_list(self.global_alignment),
            'body_frame_alignment_degrees': self.global_alignment_degrees,
            'rest_swing_degrees': dict(self.rest_swing_degrees),
            'rest_joint_calibration': dict(self.joint_calibration),
            'skipped_joint_references': dict(self.skipped_joint_references),
            'source_direction_reference': {
                semantic: {
                    'mode': reference['mode'],
                    'source_child': reference['source_child'],
                    'tail_to_joint_degrees': (
                        self.joint_calibration[semantic]
                        ['source_tail_to_joint_degrees']
                        if semantic in self.joint_calibration else None),
                }
                for semantic, reference in self.direction_references.items()
            },
            'source_twist_basis': (
                'source matrix local X/Z projected onto the semantic joint '
                'direction; DataBone tail-Y is not used as anatomical Y'),
            'source_rest_reference': 'SOURCE_ARMATURE_DATA_BONES_REST',
            'target_rest_reference': 'TARGET_OBSERVED_DEF_DATA_BONES_REST',
            'first_frame_used_as_rest': False,
            'source_origin_mode': 'CLIP_START',
            'source_origin_frame': self.source_origin_frame,
            'source_origin_world': _vector_list(self.source_origin),
            'source_clip_start_to_rest_degrees': dict(
                self.source_clip_start_to_rest_degrees),
            'source_clip_start_max_degrees': self.source_clip_start_max_degrees,
            'root_translation_reference': 'CLIP_START',
            'root_control': 'root',
            'root_translation_parent_space': 'ARMATURE_THEN_OBJECT_MATRIX',
            'motion_scale_basis': 'MEAN_WORLD_LEG_LENGTH',
            'motion_scale': self.scale,
            'leg_ratio': dict(self.leg_ratios),
            'source_leg_lengths_world': dict(self.source_leg),
            'target_leg_lengths_world': dict(self.target_leg),
            'target_object_unit': self.target_object_unit,
            'unit_application': 'target.matrix_world.inverted() once around world-space root translation',
            'neutral_controls': ['torso', 'hips', 'chest'],
            'neutral_controls_hash': neutral_hash,
            'rest_neutral_max_degrees': self.rest_neutral_degrees,
            'rest_neutral_max_position': self.rest_neutral_position,
            'skipped_semantics': dict(self.skipped_semantics),
            'refine_sweeps': self.last_sweeps,
            'refine_pass_changes_degrees': list(
                getattr(self, 'last_pass_changes', [])),
            'rotation_residual_max_degrees': self.history_max_rotation,
            'direction_residual_max_degrees': self.history_max_direction,
            'twist_residual_max_degrees': self.history_max_twist,
            'joint_direction_residual_max_degrees': self.joint_residual_max,
            'rotation_residual_by_semantic': rotation_by_semantic,
            'direction_residual_by_semantic': direction_by_semantic,
            'twist_residual_by_semantic': twist_by_semantic,
            'worst_residuals': list(self.history_worst_rotation),
            'worst_twists': list(self.history_worst_twist),
            'history_samples': self.history_frames,
            'history_definition': (
                'per-frame maxima accumulated across every Adapter.solve() '
                'sample; not last_diagnostics'),
            'last_frame': {
                'rotation_residual_max_degrees': last_rotation,
                'direction_residual_max_degrees': last_direction,
                'twist_residual_max_degrees': last_twist,
                'joint_direction_residual_max_degrees': last_joint,
            },
            'max_nonroot_basis_location': self.max_nonroot_basis_location,
            'max_basis_scale_error': self.max_basis_scale_error,
            'nonroot_translation_written': False,
            'target_scale_written': False,
            'preservation': {
                'source_action_written': False,
                'target_previous_action_written': False,
                'constraints_removed': False,
                'rest_changed': False,
                'weights_changed': False,
                'tail_controls_written': False,
                'corrective_bones_written': False,
                'corrective_constraints_removed': False,
                'scapular_controls_written': False,
                'automatic_scapular_rhythm': False,
                'automatic_contact_correction': False,
                'nla_published': False,
            },
            'limitations': [
                '原生Rigify控制器Action，不是普通FK骨或C94身份',
                '强制四肢IK_FK=1并将该模式属性写入Action通道；原值由共享快照恢复',
                'torso/hips/chest保持neutral，脊柱由spine_fk链驱动；原生重叠约束可能留下残余',
                '可选tail为非动作目标：不写tail控制/DEF通道，保留当前值或旧Action',
                '肩胛为独立手动非DEF载体：无独立源肩胛语义时不写scapula通道，不推断节律/贴肋',
                '可选correctives为原生局部修正骨：不写显式控制通道，按主骨COPY_ROTATION自然响应',
                '源首帧不作为REST；源REST与目标observed DEF REST用于身体框架/child方向校准',
                '源解剖Y取真实child/head/finger/toe关节方向；源DataBone tail-Y仅作roll参考，不直接当动作轴',
                '不承诺全ROM，逐语义报告实测旋转/方向残余',
                '无手指、面部、视线、接触检测或接触修正',
            ],
        }
