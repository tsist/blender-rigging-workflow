# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve a Rigify distal deformation roll after native IK/FK matching.

Native Rigify snaps the IK mechanism. Its one-axis middle joint cannot carry
arbitrary FK pronation. The existing Y-only tweak is the intended deformation
input for that roll. This helper changes no rest bones, constraints or weights.
The caller must first run the generated native snap and explicitly switch mode.
"""
import math
from mathutils import Matrix
import bpy


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
