# SPDX-License-Identifier: GPL-3.0-or-later
"""One visual candidate: share part of head Y overflow with the native neck.

This is a basic retarget allocation, not an overlay correction or a DLS fit.
The original graph, follow settings and all limits remain active. Native
evaluation determines the current head-parent response; no fixed C-to-D model
is used. Actual output bones must be inspected in motion before adoption.
"""
import math
from mathutils import Euler

CONTROLS = ('颈椎控制器', '头颅控制器')


def _configuration(pb):
    active = [c for c in pb.constraints if not c.mute and c.influence > 0]
    if len(active) != 1 or active[0].type != 'LIMIT_ROTATION':
        raise ValueError('Head/neck candidate requires the original single Limit Rotation')
    c = active[0]
    if c.owner_space != 'LOCAL' or abs(c.influence - 1) > 1e-6:
        raise ValueError('Head/neck limit space or influence differs')
    order = pb.rotation_mode if c.euler_order == 'AUTO' else c.euler_order
    if order == 'QUATERNION':
        order = 'XYZ'
    if order != 'XYZ' or any(pb.lock_rotation):
        raise ValueError('Head/neck Euler order or locked components need adaptation')
    if pb.rotation_mode not in ('XYZ', 'QUATERNION'):
        raise ValueError('Head/neck native rotation mode needs adaptation')
    if not all(getattr(c, 'use_limit_' + a) for a in 'xyz'):
        raise ValueError('Head/neck candidate expects all original axis bounds')
    return [(float(getattr(c, 'min_' + a)), float(getattr(c, 'max_' + a))) for a in 'xyz']


def _read(pb):
    if pb.rotation_mode == 'XYZ':
        return pb.rotation_euler.copy()
    return pb.matrix_basis.to_quaternion().to_euler('XYZ')


def _write(pb, e):
    if pb.rotation_mode == 'XYZ':
        pb.rotation_euler = e
    else:
        q = e.to_quaternion()
        if q.dot(pb.rotation_quaternion) < 0:
            q.negate()
        pb.rotation_quaternion = q


def _bounded(e, bounds):
    return Euler([max(lo, min(hi, float(e[i]))) for i, (lo, hi) in enumerate(bounds)], 'XYZ')


def allocate(rig, head_goal_control, update, basis, set_rotation):
    """Allocate once from the nominal pose. Caller owns state/Action recovery.

    Half the excess is a deliberate visual compromise. At most 15 degrees of
    additional neck input avoids forcing the full missing turn into the neck.
    These are one candidate's style choices, not animation acceptance gates.
    """
    neck, head = [rig.pose.bones[n] for n in CONTROLS]
    nb, hb = _configuration(neck), _configuration(head)
    nraw, h0 = _read(neck), _read(head)
    n0 = _bounded(nraw, nb)
    goal = head_goal_control.copy()

    def required_head(evaluated, previous):
        aim = goal.copy()
        aim.translation = evaluated.pose.bones[head.name].matrix.translation
        return basis(head, aim, evaluated).to_quaternion().to_euler('XYZ', previous)

    evaluated = update(rig)
    need0 = required_head(evaluated, h0)
    overflow = need0.y - max(hb[1][0], min(hb[1][1], need0.y))
    diagnostic = {'policy': 'half_head_Y_overflow_neck_cap_15deg',
                  'head_required_degrees': [math.degrees(x) for x in need0],
                  'head_overflow_degrees': math.degrees(overflow),
                  'neck_original_degrees': [math.degrees(x) for x in nraw],
                  'applied': False}
    if abs(overflow) < 1e-7:
        return diagnostic

    # The source nominal channel can itself be outside a native limit. Work
    # from its legal equivalent before measuring the current parent response.
    _write(neck, n0)
    need0 = required_head(update(rig), need0)
    overflow = need0.y - max(hb[1][0], min(hb[1][1], need0.y))
    if abs(overflow) < 1e-7:
        _write(neck, nraw)
        update(rig)
        diagnostic['reason'] = 'legal_neck_baseline_has_no_head_overflow'
        return diagnostic

    # One bounded native probe measures the current parent's response. This
    # derivative is of the REQUIRED local input, not an assumed world yaw.
    probe = n0.copy()
    probe.y = min(nb[1][1], n0.y + 0.001)
    if probe.y - n0.y < 1e-5:
        probe.y = max(nb[1][0], n0.y - 0.001)
    step = probe.y - n0.y
    if abs(step) < 1e-7:
        _write(neck, nraw)
        update(rig)
        diagnostic['reason'] = 'neck_Y_has_no_local_room'
        return diagnostic
    try:
        _write(neck, probe)
        need1 = required_head(update(rig), need0)
        slope = (need1.y - need0.y) / step
    finally:
        _write(neck, nraw)
        update(rig)
    diagnostic['required_head_Y_per_neck_Y'] = slope
    if not math.isfinite(slope) or abs(slope) < 0.05:
        diagnostic['reason'] = 'neck_Y_is_not_a_useful_head_parent_response'
        return diagnostic

    extra = -0.5 * overflow / slope
    cap = math.radians(15)
    extra = max(-cap, min(cap, extra))
    final_neck = n0.copy()
    final_neck.y += extra
    final_neck = _bounded(final_neck, nb)
    set_rotation(neck, final_neck.to_quaternion())
    final_required = required_head(update(rig), need0)
    final_head = _bounded(final_required, hb)
    set_rotation(head, final_head.to_quaternion())
    update(rig)
    diagnostic.update(applied=True,
                      neck_extra_degrees=math.degrees(final_neck.y - n0.y),
                      neck_final_degrees=[math.degrees(x) for x in final_neck],
                      head_final_degrees=[math.degrees(x) for x in final_head],
                      head_required_after_degrees=[math.degrees(x) for x in final_required])
    return diagnostic
