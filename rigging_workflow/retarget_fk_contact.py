# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a heel-hold Action on an explicitly calibrated plain FK chain."""
import json
import math

import bpy
from mathutils import Matrix, Vector

from . import retarget_action as rt, retarget_generic_target as generic
from . import retarget_contact_samples as samples, retarget_contact_correction as correction

META = 'c94_retarget_fk_contact_v1'
require = correction.require


def _knee(hip, knee, ankle, goal):
    thigh, shin = (knee - hip).length, (ankle - knee).length
    axis = goal - hip
    length = axis.length
    require(abs(thigh - shin) + 1e-6 < length < thigh + shin - 1e-6, '脚跟目标超出原腿可达域')
    axis.normalize()
    direction = knee - hip
    direction -= axis * direction.dot(axis)
    require(direction.length > .001, '膝部弯曲平面退化，请保留原姿势并手调')
    direction.normalize()
    along = (thigh**2 - shin**2 + length**2) / (2 * length)
    return hip + axis * along + direction * math.sqrt(max(thigh**2 - along**2, 0.0))


def _swung(pose, origin, baseline, desired):
    return Matrix.LocRotScale(origin, baseline.rotation_difference(desired) @ pose.to_quaternion(), pose.to_scale())


def _bases(world, chain, poses):
    inverse = world.inverted()
    parent_pose = chain[0].parent.matrix
    for bone, matrix in zip(chain, poses, strict=True):
        local_pose = inverse @ matrix
        basis = bone.bone.convert_local_to_pose(local_pose, bone.bone.matrix_local, invert=True,
            parent_matrix=parent_pose, parent_matrix_local=bone.parent.bone.matrix_local)
        require((basis.translation - bone.matrix_basis.translation).length < 5e-6
                and (basis.to_scale() - bone.matrix_basis.to_scale()).length < 5e-6,
                '关节求解需要改写旋转以外的通道，请手调')
        yield basis.to_quaternion()
        parent_pose = local_pose


def _point(mesh, ids, graph):
    skin = mesh.evaluated_get(graph)
    points = [skin.matrix_world @ skin.data.vertices[index].co for index in ids]
    return sum(points, Vector()) / len(points), min(point.z for point in points)


def _channels(action, slot, bones):
    require(len(action.layers) == 1 and len(action.layers[0].strips) == 1, 'FK 修正需单层单条曲线动作')
    bag = action.layers[0].strips[0].channelbag(slot)
    require(bag is not None, 'FK 动作槽没有曲线')
    return [[bag.fcurves.find(bone.path_from_id('rotation_quaternion'), index=i)
             for i in range(4)] for bone in bones]


def build(target, mesh, document):
    """Leave the base bound, save the derived curves in the original object slot."""
    scene = bpy.context.scene
    require(target.name == document['target'] and mesh.name == document['mesh'], '骨架或网格与校准不同')
    require(not rt.has_preview(target) and target.data.pose_position == 'POSE', '先完成当前预览并进入姿态求值')
    conflicts = [row for row in generic.conflicts(target) if row['kind'] != 'object_parent']
    require(not conflicts, '存在未适配的控制或动画输入')
    parent = target.parent
    while parent is not None:
        require(not parent.animation_data and not parent.constraints, '只接受静态父级')
        parent = parent.parent
    generic._world(target)
    binding = rt.binding(target)
    base, slot = binding['action'], binding['slot']
    require(base is not None and slot is not None and base.library is None and META not in base
            and base.name == document['action'] and slot.identifier == document['slot']
            and correction.action_fingerprint(base) == document['action_sha256'], '基础 Action 或 Slot 已变化')
    require(not any(not track.mute for track in target.animation_data.nla_tracks), '请先明确独立基础 Action')
    require(scene.unit_settings.scale_length == 1 and abs(scene.render.fps / scene.render.fps_base - document['fps']) < 1e-7,
            '场景时基或单位与校准不同')
    names = document['chain']
    require(len(names) == 3 and len(set(names)) == 3 and all(name in target.pose.bones for name in names), '明确髋、膝、踝链')
    bones = [target.pose.bones[name] for name in names]
    require(bones[0].parent and bones[1].parent == bones[0] and bones[2].parent == bones[1]
            and all(b.rotation_mode == 'QUATERNION' and not any(b.lock_rotation) and not b.lock_rotation_w for b in bones),
            '需直接相连的三骨原生四元数 FK 链')
    samples._inspect_mesh(scene, mesh, target)
    require(samples._topology_identity(mesh.data) == document['topology']
            and samples._weight_digest(mesh) == document['weights'], '足底样本拓扑或权重已变化')
    ids = document['heel_vertex_ids']
    allowed = {mesh.vertex_groups[g].index for g in document['heel_groups'] if g in mesh.vertex_groups}
    require(len(ids) >= 3 and len(set(ids)) == len(ids) and allowed
            and all(type(i) is int and 0 <= i < len(mesh.data.vertices)
                    and any(g.group in allowed and g.weight > 0 for g in mesh.data.vertices[i].groups) for i in ids),
            '请提供有明确足部权重的脚跟顶点索引')
    touch, floor, lift, maximum = (document[k] for k in ('touchdown', 'plane_z_m', 'clearance_m', 'max_delta_m'))
    require(all(type(v) in (int, float) and math.isfinite(v) for v in (touch, floor, lift, maximum))
            and base.frame_range[0] < touch < base.frame_range[1] and 0 <= lift < maximum, '着地时间、标高或位移范围无效')
    curves = _channels(base, slot, bones)
    first, last = (round(value) for value in base.frame_range)
    domain = list(range(first, last + 1))
    require(all(fc and not fc.modifiers and not fc.sampled_points
            and [p.co.x for p in fc.keyframe_points] == domain
            and all(p.interpolation == 'LINEAR' for p in fc.keyframe_points) for channels in curves for fc in channels),
            '原 FK 旋转曲线需为完整整数帧线性采样')
    current = (scene.frame_current, scene.frame_subframe)
    scheduled = sorted({touch, *(frame for frame in domain if frame >= touch)})
    candidate = None
    try:
        plan = []
        anchor = None
        for frame in scheduled:
            samples._frame_set(scene, frame)
            graph = bpy.context.evaluated_depsgraph_get()
            owner = target.evaluated_get(graph)
            chain = [owner.pose.bones[name] for name in names]
            world = owner.matrix_world
            thigh, shin, foot = (world @ bone.matrix for bone in chain)
            hip, knee, ankle = (matrix.translation.copy() for matrix in (thigh, shin, foot))
            center, low = _point(mesh, ids, graph)
            if anchor is None:
                anchor = center.copy()
            delta = anchor - center
            delta.z = floor + lift - low
            require(delta.length <= maximum, '超出明确的脚跟最大位移')
            goal = ankle + delta
            new_knee = _knee(hip, knee, ankle, goal)
            foot.translation = goal
            desired = (_swung(thigh, hip, knee - hip, new_knee - hip),
                       _swung(shin, new_knee, ankle - knee, goal - new_knee), foot)
            plan.append((frame, list(_bases(world, chain, desired))))
        candidate = base.copy()
        candidate.name = base.name + '_脚跟支撑'
        candidate.use_fake_user = True
        new_slot, = [new_slot for new_slot in candidate.slots if new_slot.identifier == slot.identifier]
        outputs = _channels(candidate, new_slot, bones)
        for frame, rotations in plan:
            for channels, previous_channels, rotation in zip(outputs, curves, rotations, strict=True):
                if sum(fc.evaluate(frame) * component for fc, component in zip(previous_channels, rotation, strict=True)) < 0:
                    rotation.negate()
                for fc, component in zip(channels, rotation, strict=True):
                    options = {'FAST', 'REPLACE'} if frame in domain else {'FAST'}
                    key = fc.keyframe_points.insert(frame, component, options=options)
                    require(key is not None, 'FK 曲线写入失败')
                    key.interpolation = 'LINEAR'
        for channels in outputs:
            for fc in channels:
                fc.update()
        target.animation_data.action = candidate
        target.animation_data.action_slot = new_slot
        errors = []
        for frame in scheduled:
            samples._frame_set(scene, frame)
            center, low = _point(mesh, ids, bpy.context.evaluated_depsgraph_get())
            errors.append(max((center.xy - anchor.xy).length, abs(low - floor - lift)))
        require(max(errors) <= .001, '回读实际脚跟残差超出 1 mm，请保持基础动作')
        candidate[META] = json.dumps({'document': document, 'max_readback_error_m': max(errors), 'applied': False}, ensure_ascii=False)
        return candidate
    except BaseException:
        if candidate is not None:
            rt.restore_binding(target, binding)
            bpy.data.actions.remove(candidate)
        raise
    finally:
        rt.restore_binding(target, binding)
        scene.frame_set(current[0], subframe=current[1])
