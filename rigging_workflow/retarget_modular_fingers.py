"""Optional finger rotation channels for a modular retarget Action."""

import json
import math

import bpy

from . import retarget_action, retarget_source, semantic_rig_map


def _bindings(source, target, source_map):
    mapping = semantic_rig_map.describe(target)
    target_bones, target_controls = mapping['bones'], mapping['controls']
    drivers = {curve.data_path for curve in target.animation_data.drivers}
    bindings, complete, skipped = [], [], {}
    for side in semantic_rig_map.SIDES:
        hand = f'hand.{side}'
        src_hand = source.data.bones[source_map[hand]].matrix_local.to_quaternion()
        dst_hand = target.data.bones[target_bones[hand]].matrix_local.to_quaternion()
        for digit in semantic_rig_map.FINGERS:
            legacy = [f'{digit}{i}.{side}' for i in range(1, 4)]
            canonical = [semantic_rig_map.from_profile_role(key) for key in legacy]
            chain = f'{digit}.{side}'
            if (not all(key in source_map for key in legacy)
                    or not all(key in target_controls and key in target_bones for key in canonical)):
                skipped[chain] = 'incomplete source or target chain'
                continue
            complete.append(chain)
            for old, new in zip(legacy, canonical, strict=True):
                src_name = source_map[old]
                dst = target.pose.bones[target_controls[new]]
                src_rest = src_hand.inverted() @ source.data.bones[src_name].matrix_local.to_quaternion()
                dst_rest = dst_hand.inverted() @ target.data.bones[target_bones[new]].matrix_local.to_quaternion()
                axis = src_rest.inverted() @ dst_rest
                channel = 'rotation_quaternion' if dst.rotation_mode == 'QUATERNION' else 'rotation_euler'
                path = dst.path_from_id(channel)
                if path in drivers or dst.rotation_mode == 'AXIS_ANGLE':
                    raise ValueError('Unsupported driven or axis-angle finger control: ' + dst.name)
                bindings.append((src_name, dst, path, axis))
    if not bindings:
        raise ValueError('Finger retarget requires at least one complete mapped chain')
    return bindings, complete, skipped


def write(source, target, source_map, result, clip, cancelled):
    bindings, complete, skipped = _bindings(source, target, source_map)
    action, slot, report = result['action'], result['slot'], result['report']
    bag = action.layers[0].strips[0].channelbag(slot)
    frames = [float(point.co.x) for point in bag.fcurves[0].keyframe_points]
    scene = bpy.context.scene
    old_frame = scene.frame_current, scene.frame_subframe
    source_binding = retarget_action.binding(source)
    source_ad = source.animation_data
    selected = clip or retarget_source.active_clip(source, source_fps=report['source_fps'])
    channels, previous = {}, {}
    try:
        source_ad.action, source_ad.action_slot = selected['action'], selected['slot']
        source_ad.use_nla = False
        source_ad.action_blend_type, source_ad.action_influence = 'REPLACE', 1.
        ratio = report['source_fps'] / report['target_fps']
        for frame in frames:
            if cancelled and cancelled():
                raise InterruptedError('Finger retarget cancelled')
            t = report['source_range'][0] + (frame - frames[0]) * ratio
            integer = math.floor(t)
            scene.frame_set(integer, subframe=t - integer)
            evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for src_name, dst, path, axis in bindings:
                delta = evaluated.pose.bones[src_name].matrix_basis.to_quaternion()
                rotation = (axis.inverted() @ delta @ axis).normalized()
                prior = previous.get(path)
                if dst.rotation_mode == 'QUATERNION':
                    if prior is not None and prior.dot(rotation) < 0:
                        rotation.negate()
                    value = rotation
                else:
                    value = (rotation.to_euler(dst.rotation_mode, prior) if prior is not None
                             else rotation.to_euler(dst.rotation_mode))
                previous[path] = value
                for index, component in enumerate(value):
                    channels.setdefault((path, index), []).append((frame, component))
        for (path, index), samples in channels.items():
            curve = bag.fcurves.new(path, index=index)
            curve.keyframe_points.add(len(samples))
            curve.keyframe_points.foreach_set('co', [v for point in samples for v in point])
            for key in curve.keyframe_points:
                key.interpolation = 'LINEAR'
            curve.update()
    finally:
        retarget_action.restore_binding(source, source_binding)
        scene.frame_set(old_frame[0], subframe=old_frame[1])
    report.update(include_fingers=True, finger_chains=complete, skipped_fingers=skipped,
                  finger_policy='WRIST_REST_ALIGNED_NATIVE_FINGER_ROTATIONS')
    report['controls'] += [dst.name for _, dst, _, _ in bindings]
    action[retarget_action.META] = json.dumps(report, ensure_ascii=False)
