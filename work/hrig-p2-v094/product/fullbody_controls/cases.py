"""Finite input and pressure-pose definitions; not acceptance results.

The caller must inspect original input, evaluate actual bones/skin and restore
after every case. Bone axis signs and native limits need runtime calibration.
"""
from native_controls.contract import validate_operations, require


def _role_map(profile):
    return {r['role_id']: r for r in profile['roles']}


def _guard_operations(profile, operations):
    roles = _role_map(profile)
    mode_roles = {(r['bone'], r['property']): r['role_id'] for r in roles.values() if r['kind'] == 'mode_enum'}
    modes = {}
    for op in operations:
        for guard in roles[op['role']]['requires']:
            key = (guard['bone'], guard['property'])
            require(key in mode_roles, 'CASE_MODE_ROLE_MISSING', key)
            name, value = mode_roles[key], guard['value']
            require(name not in modes or modes[name] == value, 'CASE_MODE_CONFLICT', name)
            modes[name] = value
    explicit = {op['role']: op['value'] for op in operations}
    for name, value in modes.items(): require(name not in explicit or explicit[name] == value, 'CASE_MODE_CONFLICT', name)
    result = [{'role': name, 'value': value} for name, value in modes.items() if name not in explicit] + operations
    validate_operations(profile, result)
    return result


def _case(profile, name, operations, regions, scope, calibration=False):
    operations = _guard_operations(profile, operations)
    roles = _role_map(profile)
    mode_inputs = [dict(o) for o in operations if roles[o['role']]['kind'] == 'mode_enum']
    transform_inputs = [dict(o) for o in operations if roles[o['role']]['kind'] != 'mode_enum']
    return {'case_id': name, 'operations': operations,
            'baseline_operations': mode_inputs,
            'input_operations': transform_inputs,
            'isolate_mode_effect': bool(transform_inputs),
            'expected_control_bones': sorted({roles[o['role']]['bone'] for o in operations if roles[o['role']]['kind'] != 'mode_enum'}),
            'expected_regions': list(regions), 'quality_scope': scope,
            'requires_axis_calibration': calibration,
            'checks': ['establish and observe same-mode baseline before transform input',
                       'input readback', 'actual evaluated bone response relative to same-mode baseline', 'finite evaluated skin',
                       'declared author protection', 'complete input/author/actual/skin restoration'],
            'must_observe': 'consumer response is a runtime finding; a limit-clamped or collinear input is not a successful deform-control claim'}


def axis_cases(profile, catalog=None):
    """One finite case per role, including every newly declared axis.

    Finger scale and quaternion roles are separate cases because the established
    native batch contract allows one transform input per bone per batch.
    """
    answer = []
    for role in profile['roles']:
        kind = role['kind']
        value = .003 if kind == 'location_delta' else .2 if kind in ('euler_delta', 'quaternion_delta') else .8 if kind == 'scale_set' else 0
        value = min(max(value, role['bounds'][0]), role['bounds'][1])
        region = 'body' if role['role_id'].startswith(('body.', 'shoulder.')) else role['role_id'].split('.')[0]
        answer.append(_case(profile, 'axis.' + role['role_id'], [{'role': role['role_id'], 'value': value}],
                            [region], 'finite local input; not pressure-quality acceptance'))
    return answer


def pressure_cases(profile):
    """Representative common-movement candidates, in radians/BU.

    ± signs are intentionally paired where anatomy cannot be inferred from the
    inventory. Inspect constraints and actual joint angles before accepting one
    endpoint as anatomical flexion. No parameter search is implied or required.
    """
    out = []
    def add(name, ops, regions):
        out.append(_case(profile, name, [{'role': r, 'value': v} for r, v in ops], regions,
                         'joint-pressure candidate: inspect contour, volume, seams, contact and bone/skin motion', True))
    for side in ('L', 'R'):
        for sign, suffix in ((1., 'positive'), (-1., 'negative')):
            add('elbow.' + side + '.' + suffix, [('arm.' + side + '.fk.forearm', sign * 1.3)], ['elbow.' + side, 'forearm.' + side])
            add('knee.' + side + '.' + suffix, [('leg.' + side + '.fk.shin', sign * 1.3)], ['knee.' + side, 'calf.' + side])
        add('arm_raise.' + side, [('arm.' + side + '.fk.upper.z', .9 if side == 'L' else -.9)], ['shoulder.' + side, 'armpit.' + side])
        add('hip_flex.' + side, [('leg.' + side + '.fk.thigh', .95)], ['hip.' + side, 'groin.' + side])
        add('ankle.' + side, [('leg.' + side + '.fk.foot', -.5)], ['ankle.' + side, 'foot.' + side])
        add('hand_curl.' + side, [('finger.' + side + '.' + f, .65) for f in ('thumb', 'index', 'middle', 'ring', 'pinky')], ['hand.' + side, 'finger joints.' + side])
        add('hand_splay.' + side, [('finger.' + side + '.' + f + '.splay', v) for f, v in
                                 (('thumb', -.25), ('index', -.15), ('middle', 0.), ('ring', .12), ('pinky', .22))], ['hand.' + side, 'finger webs.' + side])
    add('body_bend', [('body.spine', .5), ('body.chest', .3), ('body.neck', -.2)], ['waist', 'torso', 'neck'])
    add('body_turn', [('body.pelvis.y', .25), ('body.spine.y', .35), ('body.chest.y', .25), ('body.head.y', -.3)], ['pelvis', 'waist', 'neck'])
    add('fullbody_fk', [('body.pelvis', .15), ('body.spine', -.2), ('body.chest.y', .2),
                        ('arm.L.fk.upper.z', .65), ('arm.L.fk.forearm', -.9),
                        ('arm.R.fk.upper.z', -.4), ('arm.R.fk.forearm', -.65),
                        ('leg.L.fk.thigh', .45), ('leg.L.fk.shin', -.7), ('leg.L.fk.foot', .2),
                        ('leg.R.fk.thigh', -.15), ('leg.R.fk.shin', -.25),
                        ('finger.L.index', .8), ('finger.R.index', .8)],
        ['whole body', 'shoulders', 'elbows', 'hips', 'knees', 'hands'])
    add('fullbody_ik', [('body.spine', .15), ('arm.L.ik.target.y', .008), ('arm.R.ik.target.z', -.006),
                        ('leg.L.ik.target.y', .004), ('leg.R.ik.target.z', .003),
                        ('arm.L.ik.pole.z', .004), ('leg.L.ik.pole.y', .003)],
        ['whole body', 'IK targets', 'IK poles'])
    return out
