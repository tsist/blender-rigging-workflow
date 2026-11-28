# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve a Rigify distal deformation roll after native IK/FK matching.

Native Rigify snaps the IK mechanism. Its one-axis middle joint cannot carry
arbitrary FK pronation. The existing Y-only tweak is the intended deformation
input for that roll. This helper changes no rest bones, constraints or weights.
The caller must first run the generated native snap and explicitly switch mode.
"""
import math
import json
from mathutils import Matrix
import bpy


def bake_mode(rig, *, limb, side, mode, start, end=None, name, max_error_meters=0.0001):
    """Write a separate Action with the chosen IK/FK solution at each frame."""
    from . import control_layer, mode_animation, pose_switching, retarget_action

    end = start if end is None else end
    if (type(start) is not int or type(end) is not int or not 0 <= end - start <= 1000
            or mode not in {"IK", "FK"} or not isinstance(name, str) or not name.strip()
            or name in bpy.data.actions):
        raise ValueError("Supply a new action name, a mode, and a bounded integer frame range")
    scene = bpy.context.scene
    ad = rig.animation_data
    if ad is None or ad.nla_tracks or ad.use_tweak_mode or ad.action_blend_type != "REPLACE" or ad.action_influence != 1.0:
        raise ValueError("Mode baking requires an isolated active Action/Slot or an unanimated armature")
    binding = retarget_action.binding(rig)
    source_action, source_slot = binding["action"], binding["slot"]
    if source_action is not None:
        if source_slot is None or start < source_action.frame_range[0] or end > source_action.frame_range[1]:
            raise ValueError("Provide a frame range inside the active action")
        mode_animation._bag(source_action, source_slot)
    family, owner, prop, ik_value = control_layer._limb(rig, limb, side)
    value = ik_value if mode == "IK" else 1 - ik_value
    prop_path = mode_animation._property_path(owner, prop)
    original_mode = owner[prop]
    if any(curve.data_path == prop_path for curve in ad.drivers):
        raise ValueError("The selected mode property is driven")
    frame = scene.frame_current, scene.frame_subframe
    state = mode_animation._snapshot(rig)
    original_pose = {bone.name: control_layer._pose_channels(bone) for bone in rig.pose.bones}
    if family == "c94":
        native_names, _, _ = control_layer._c94_matching(rig, limb, side, mode, True)
    else:
        native_names, _, _ = control_layer._rigify_matching(rig, limb, side, mode, owner)
    controlled, _ = control_layer._controlled_links(rig, native_names)
    output_names = tuple(dict.fromkeys(controlled[name].name if name in controlled else name
                                     for name in native_names))
    samples = []
    candidate = None
    try:
        for current in range(start, end + 1):
            scene.frame_set(current)
            evaluated = pose_switching._update(rig)
            initial = {bone.name: control_layer._pose_channels(bone) for bone in evaluated.pose.bones}
            old_mode = float(evaluated.pose.bones[owner.name][prop])
            ad.action = None
            for bone in rig.pose.bones:
                control_layer._restore_pose(bone, initial[bone.name])
            owner[prop] = old_mode
            pose_switching._update(rig)
            report = (control_layer._match_mode(rig, limb, side, mode, owner, prop, value,
                                                family, True, max_error_meters)
                      if old_mode != value else {})
            changed = {bone: pose_switching._channels(rig.pose.bones[bone]) for bone in output_names}
            samples.append((current, changed, report.get("metrics", {}).get("sample_max_meters", 0.0)))
            retarget_action.restore_binding(rig, binding)
            owner[prop] = original_mode
        candidate = source_action.copy() if source_action else bpy.data.actions.new(name)
        candidate.name = name
        if candidate.name != name:
            raise ValueError("Blender did not accept the action name as written")
        candidate.use_fake_user = True
        slot = candidate.slots[source_slot.identifier] if source_slot else candidate.slots.new(id_type="OBJECT", name=rig.name)
        if not source_action:
            candidate.layers.new("IK/FK bake").strips.new(type="KEYFRAME")
        ad.action, ad.action_slot = candidate, slot
        bag = candidate.layers[0].strips[0].channelbag(slot, ensure=True)
        written = {(prop_path, 0)}
        fields = {name: control_layer._channels(rig.pose.bones[name])
                  for _, changed, _ in samples for name in changed}
        for current, changed, _ in samples:
            for bone, transforms in changed.items():
                for field in fields[bone]:
                    path = rig.pose.bones[bone].path_from_id(field)
                    for index, component in enumerate(transforms[field]):
                        key = path, index
                        curve = bag.fcurves.find(path, index=index) or bag.fcurves.new(path, index=index)
                        curve.keyframe_points.insert(current, component, options={"FAST"}).interpolation = "LINEAR"
                        written.add(key)
            curve = bag.fcurves.find(prop_path, index=0) or bag.fcurves.new(prop_path, index=0)
            curve.keyframe_points.insert(current, value, options={"FAST"}).interpolation = "CONSTANT"
        for path, index in written:
            bag.fcurves.find(path, index=index).update()
        report = {"action": candidate.name, "slot": slot.identifier, "mode": mode, "side": side,
                  "limb": limb, "frame_start": start, "frame_end": end, "frames": len(samples),
                  "max_pose_error_meters": max(error for _, _, error in samples)}
        candidate["rigging_pose_match_v1"] = json.dumps(report)
        return report
    except BaseException:
        if candidate is not None:
            ad.action = None
            bpy.data.actions.remove(candidate)
        raise
    finally:
        retarget_action.restore_binding(rig, binding)
        scene.frame_set(frame[0], subframe=frame[1])
        for bone in rig.pose.bones:
            control_layer._restore_pose(bone, original_pose[bone.name])
        for (bone, key), original in state["modes"].items():
            rig.pose.bones[bone][key] = original
        owner[prop] = original_mode
        for (path, index), original in state["animated"].items():
            mode_animation._restore_value(rig, path, index, original)
        pose_switching._update(rig)


def preserve_distal_roll(rig, deform_name, tweak_name, target_world):
    allowed = {('DEF-'+stem+'.'+side, stem+'_tweak.'+side)
               for stem in ('forearm', 'shin') for side in ('L', 'R')}
    if (deform_name, tweak_name) not in allowed:
        raise ValueError('Only explicit Rigify forearm/shin deform and tweak pairs are supported')
    if rig is None or rig.type != 'ARMATURE' or rig != bpy.context.view_layer.objects.active:
        raise ValueError('The current active local armature is required')
    deform = rig.pose.bones.get(deform_name)
    tweak = rig.pose.bones.get(tweak_name)
    if not deform or not deform.bone.use_deform or not tweak:
        raise ValueError('Missing actual deform or tweak control')
    if tweak.rotation_mode != 'ZXY' or tuple(tweak.lock_rotation) != (True, False, True):
        raise ValueError('Expected the native Rigify Y-only ZXY tweak control')
    if abs(tweak.rotation_euler.x) > 1e-6 or abs(tweak.rotation_euler.z) > 1e-6:
        raise ValueError('A non-axial existing tweak needs an explicit matching policy')
    if not any(c.type == 'COPY_TRANSFORMS' and c.target == rig and c.subtarget == tweak_name for c in deform.constraints):
        raise ValueError('The named tweak is not consumed by the actual deform bone')
    target = Matrix(target_world).to_quaternion().normalized()
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    current = (evaluated.matrix_world @ evaluated.pose.bones[deform_name].matrix).to_quaternion().normalized()
    relative = current.rotation_difference(target)
    magnitude = math.hypot(relative.w, relative.y)
    if magnitude < 1e-8:
        raise ValueError('Ambiguous 180-degree swing: axial roll cannot be isolated')
    angle = 2 * math.atan2(relative.y / magnitude, relative.w / magnitude)
    angle = (angle + math.pi) % (2 * math.pi) - math.pi
    before = float(tweak.rotation_euler.y)
    tweak.rotation_euler.y = before + angle
    rig.update_tag(); bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    actual = (evaluated.matrix_world @ evaluated.pose.bones[deform_name].matrix).to_quaternion().normalized()
    remaining = actual.rotation_difference(target)
    residual = 2 * math.atan2(math.sqrt(remaining.x**2 + remaining.y**2 + remaining.z**2), abs(remaining.w))
    return {'deform': deform_name, 'tweak': tweak_name, 'previous_y': before,
            'applied_roll_radians': angle, 'result_y': float(tweak.rotation_euler.y),
            'remaining_rotation_degrees': math.degrees(residual)}


def snap_fk_to_ik_preserving_roll(rig, limb, side):
    """Use generated native snap, then preserve the distal roll via native tweak.

    Bounded to the current simple Rigify arm/leg layout. Does not bake or key;
    callers key the returned controls and mode explicitly if animation is wanted.
    Numerical IK residuals must still be reviewed in the real native output.
    """
    if limb not in {'arm', 'leg'} or side not in {'L', 'R'}:
        raise ValueError('Explicit arm/leg and L/R are required')
    if rig is None or rig.type != 'ARMATURE' or rig != bpy.context.view_layer.objects.active or rig.mode != 'POSE':
        raise ValueError('Select the actual armature in Pose Mode')
    if not isinstance(rig.data.get('rig_id'), str):
        raise ValueError('Missing generated Rigify operator identity')
    stem, lower, end = ('upper_arm', 'forearm', 'hand') if limb == 'arm' else ('thigh', 'shin', 'foot')
    prop = stem + '_parent.' + side
    if rig.pose.bones[prop]['IK_FK'] != 1:
        raise ValueError('Source must actually be in FK mode')
    deform = 'DEF-' + lower + '.' + side
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    target = evaluated.matrix_world @ evaluated.pose.bones[deform].matrix
    fk = [stem+'_fk.'+side, lower+'_fk.'+side, end+'_fk.'+side] + ([] if limb == 'arm' else ['toe_fk.'+side])
    ik = [stem+'_ik.'+side, 'MCH-'+lower+'_ik.'+side, 'MCH-'+stem+'_ik_target.'+side]
    ctrl = [stem+'_ik.'+side, stem+'_ik_target.'+side, end+'_ik.'+side]
    tail = [] if limb == 'arm' else ['toe_ik.'+side]
    extra = [] if limb == 'arm' else ['foot_heel_ik.'+side, 'foot_spin_ik.'+side]
    from . import skeleton_qc
    import json
    saved = skeleton_qc._runtime_state(rig)
    try:
        result = getattr(bpy.ops.pose, 'rigify_limb_ik2fk_' + rig.data['rig_id'])(
            prop_bone=prop, fk_bones=json.dumps(fk), ik_bones=json.dumps(ik),
            ctrl_bones=json.dumps(ctrl), tail_bones=json.dumps(tail), extra_ctrls=json.dumps(extra))
        if 'FINISHED' not in result:
            raise ValueError('Generated native snap did not finish')
        rig.pose.bones[prop]['IK_FK'] = 0.
        rig.update_tag(); bpy.context.view_layer.update()
        correction = preserve_distal_roll(rig, deform, lower+'_tweak.'+side, target)
        return {'native_operator': 'pose.rigify_limb_ik2fk_' + rig.data['rig_id'],
                'roll': correction, 'controls_to_key': ctrl + tail + extra + [lower+'_tweak.'+side],
                'properties_to_key': [[prop, 'IK_FK']]}
    except Exception:
        skeleton_qc._restore_runtime(saved)
        raise
