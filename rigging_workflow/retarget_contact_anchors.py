# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit bone-local contact markers, never skin/sole geometry or approval."""
import hashlib
import json
import math
import bpy
from mathutils import Vector
from . import retarget_contact_samples as samples

MODE = 'bone_anchors'
COVERAGE = 'explicit_declared_anchors'
SCOPE = 'declared_bone_anchors'


class AnchorRestorationError(RuntimeError):
    """Existing state-core restore or its declared guard failed."""


def require(value, message):
    if not value:
        raise ValueError(message)


def mode(document):
    value = document.get('measurement_mode', 'surface')
    require(value in ('surface', MODE), '未知接触测量模式')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def validate(scene, rig, declared):
    require(rig is not None and rig.type == 'ARMATURE' and rig.name in scene.objects,
            '骨锚点需要本Scene的明确Armature')
    require(rig.data.pose_position == 'POSE', '骨锚点仅接受实际POSE求值')
    require(isinstance(declared, dict) and set(declared) == set(samples.SIDES),
            '骨锚点必须明确L/R两侧')
    unit = scene.unit_settings.scale_length
    require(math.isfinite(unit) and unit > 0, 'Scene米制单位无效')
    world = rig.matrix_world
    require(all(math.isfinite(v) for row in world for v in row) and
            math.isfinite(world.determinant()) and world.determinant() != 0,
            '骨锚点对象变换必须有限且可逆')
    ids = set()
    for side in samples.SIDES:
        regions = declared[side]
        require(isinstance(regions, dict) and set(regions) == set(samples.REGIONS),
                '每侧必须独立校准heel/ball/toe三点，不能复制前掌补脚趾')
        rest_points = []
        for region in samples.REGIONS:
            row = regions[region]
            require(isinstance(row, dict) and set(row) ==
                {'anchor_id', 'bone', 'local_offset', 'units', 'semantic', 'basis'},
                '骨锚点字段需anchor_id/bone/local_offset/units/semantic/basis')
            anchor_id = row['anchor_id']
            require(isinstance(anchor_id, str) and anchor_id.strip() and anchor_id not in ids,
                    'anchor_id需全局唯一且非空')
            ids.add(anchor_id)
            require(row['units'] == 'bone_local_scene_units' and row['semantic'] == region,
                    '锚点必须声明骨pose-local Scene单位及对应片区语义')
            require(isinstance(row['basis'], str) and row['basis'].strip(), '锚点需要明确校准依据')
            bone = rig.data.bones.get(row['bone']) if isinstance(row['bone'], str) else None
            require(bone is not None and bone.use_deform and bone.name in rig.pose.bones,
                    '锚点必须引用实际存在的输出形变骨')
            offset = row['local_offset']
            require(isinstance(offset, (list, tuple)) and len(offset) == 3 and
                all(type(v) in (int, float) and math.isfinite(v) for v in offset),
                'local_offset需明确有限三维坐标')
            point = world @ bone.matrix_local @ Vector(offset)
            require(all((point - other).length * unit > 1e-7 for other in rest_points),
                    'heel/ball/toe必须是独立真实局部点，不能以不同ID复制同一点')
            rest_points.append(point)
    return declared


def identity(scene, rig, declared):
    validate(scene, rig, declared)
    names=sorted({row['bone'] for regions in declared.values() for row in regions.values()})
    return {'measurement_mode': MODE, 'rig': rig.name, 'armature': rig.data.name,
        'anchors': json.loads(json.dumps(declared, allow_nan=False)),
        'rest': samples._rest_catalog(rig),
        'bone_rest_frames':{name:{'matrix_local':[list(r) for r in rig.data.bones[name].matrix_local],
            'use_deform':rig.data.bones[name].use_deform} for name in names},
        'meters_per_unit': scene.unit_settings.scale_length}


def read_points(scene, rig, declared):
    graph = samples._scene_depsgraph(scene)
    evaluated = rig.evaluated_get(graph)
    world = evaluated.matrix_world
    require(all(math.isfinite(v) for row in world for v in row) and
            math.isfinite(world.determinant()) and world.determinant() != 0,
            '采样时骨锚点对象变换必须有限且可逆')
    result = {}
    for side, regions in declared.items():
        result[side] = {}
        for region, row in regions.items():
            pb = evaluated.pose.bones.get(row['bone'])
            require(pb is not None, '实际求值输出骨不存在')
            point = world @ pb.matrix @ Vector(row['local_offset'])
            require(all(math.isfinite(v) for v in point), '骨锚点求值产生非有限坐标')
            result[side][region] = point
    return result


def sample_schedule(scene, rig, declared, frames, times_s, *, plane, state_core=None):
    samples.require_background()
    before = identity(scene, rig, declared)
    frames, times = samples._validate_frames(frames, times_s)
    ground = samples._plane_doc(plane)
    require(ground['status'] == 'explicit', '骨锚点地面必须显式校准')
    normal = Vector(ground['normal'])
    output = {s: {r: {'mode': 'points', 'samples': [], 'patch_min': []}
                  for r in samples.REGIONS} for s in samples.SIDES}
    actions_before = samples._action_catalog()
    constraints_before = samples._constraint_catalog(rig)
    captured = samples._capture_state(scene, rig, state_core=state_core)
    try:
        for frame in frames:
            samples._frame_set(scene, frame)
            samples._scene_view_layer(scene).update()
            points = read_points(scene, rig, declared)
            for side in samples.SIDES:
                for region in samples.REGIONS:
                    point = points[side][region]
                    output[side][region]['samples'].append([list(point)])
                    output[side][region]['patch_min'].append(normal.dot(point) - ground['d'])
    finally:
        try:
            samples._restore_state(captured)
            guard = samples._declared_state_equal(captured)
            previous = captured['scene']
            guard['scene_equal'] = (scene.frame_current == previous['frame'] and
                scene.frame_subframe == previous['subframe'] and scene.frame_start == previous['frame_start'] and
                scene.frame_end == previous['frame_end'] and scene.render.fps == previous['fps'] and
                scene.render.fps_base == previous['fps_base'] and
                samples._scene_view_layer(scene).objects.active == previous['active'])
            guard['actions_equal'] = samples._action_catalog() == actions_before
            guard['constraints_equal'] = samples._constraint_catalog(rig) == constraints_before
            guard['anchor_rest_identity_equal'] = identity(scene, rig, declared) == before
            if not all(guard.values()):
                raise AnchorRestorationError('骨锚点采样声明输入/骨图/原Action恢复核验失败')
        except AnchorRestorationError:
            raise
        except Exception as exc:
            raise AnchorRestorationError('骨锚点采样恢复失败') from exc
    return {'samples': output, 'frames': frames, 'timestamps_s': times,
            'state_guard': {**guard, 'restore_ok': True, 'mode': captured['mode'],
                            'scope': 'declared rig state; existing sampler state core; no whole-scene claim'}}
