"""Build an explicit candidate from frozen profiles and read-only RNA inventory.

This never discovers a rig by its names alone or re-signs its rest state.  Names
below are the selected Anion control mapping, checked against the inventory.
"""
from copy import deepcopy
from native_controls.contract import validate, require

EULER_MODES = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}
AXES = ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))


def build(base_profile, leg_profile, inventory):
    """Return ``(profile, catalog)``; both are detached JSON-compatible values.

    The catalog records candidate bounds changes and UI group membership.
    Runtime guards, native response, skin quality and saved-state validation
    remain the caller's responsibility; inventory is not an acceptance receipt.
    """
    base, leg = validate(base_profile), validate(leg_profile)
    require(base['character_id'] == leg['character_id'] == 'anion', 'CHARACTER_MISMATCH')
    require(base['runtime'] == leg['runtime'] and base['rig'] == leg['rig'], 'FROZEN_BINDING_MISMATCH')
    require(type(inventory) is dict and type(inventory.get('bones')) is list, 'INVENTORY_SCHEMA')
    bones = {b['name']: deepcopy(b) for b in inventory['bones']}
    require(len(bones) == len(inventory['bones']), 'DUPLICATE_INVENTORY_BONE')
    roles = {r['role_id']: r for r in base['roles']}
    for role in leg['roles']:
        require(role['role_id'] not in roles or role == roles[role['role_id']], 'ROLE_MAPPING_CONFLICT')
        roles[role['role_id']] = deepcopy(role)
    inherited_ids = set(roles)
    bounds_changes = []

    def check(role):
        bone = bones.get(role['bone'])
        require(bone is not None, 'CONTROL_NOT_IN_INVENTORY', role['bone'])
        require(bone['parent'] == role['parent'] and bone['mode'] == role['rotation_mode'], 'INVENTORY_BINDING_DRIFT', role['role_id'])
        kind, index = role['kind'], role['index']
        if kind == 'mode_enum':
            require(type(bone['properties'].get(role['property'])) in (int, float), 'PROPERTY_NOT_IN_INVENTORY', role['role_id'])
        elif kind == 'quaternion_delta':
            require(bone['mode'] == 'QUATERNION' and not any(bone['locks']['rotation']) and not bone['locks']['rotation_w'], 'ROTATION_LOCKED', role['role_id'])
        else:
            field = 'location' if kind == 'location_delta' else 'scale' if kind == 'scale_set' else 'rotation'
            require(not bone['locks'][field][index], 'COMPONENT_LOCKED', role['role_id'])
            if kind == 'euler_delta': require(bone['mode'] in EULER_MODES, 'EULER_MODE', role['role_id'])
            if kind == 'location_delta': require(not bone['connected'], 'CONNECTED_LOCATION', role['role_id'])
        for guard in role['requires']:
            require(guard['bone'] in bones and guard['property'] in bones[guard['bone']]['properties'], 'MISSING_MODE_GUARD')

    for role in roles.values(): check(role)

    def add(role_id, bone_name, kind, index=0, bounds=(-.7, .7), guards=(), property_name=None):
        require(role_id not in roles, 'DUPLICATE_ROLE', role_id)
        bone = bones.get(bone_name)
        require(bone is not None, 'CONTROL_NOT_IN_INVENTORY', bone_name)
        role = {'role_id': role_id, 'bone': bone_name, 'parent': bone['parent'],
                'rotation_mode': bone['mode'], 'kind': kind, 'index': index,
                'axis': list(AXES[index]), 'property': property_name, 'bounds': list(bounds),
                'requires': deepcopy(list(guards)), 'label': role_id + ' (local ' + 'XYZ'[index] + ')'}
        check(role); roles[role_id] = role

    def extend_axes(role_id, bounds=None):
        original = roles[role_id]
        for index, suffix in ((1, '.y'), (2, '.z')):
            add(role_id + suffix, original['bone'], original['kind'], index,
                original['bounds'] if bounds is None else bounds, original['requires'])

    # Preserve established identifiers as local-X.  New axes are explicit roles,
    # not simultaneous writes through the old one-transform-per-bone contract.
    for part in ('root', 'spine', 'chest', 'neck', 'head'):
        extend_axes('body.' + part)
    for index, suffix in ((0, ''), (1, '.y'), (2, '.z')):
        add('body.pelvis' + suffix, '盆骨控制器', 'euler_delta', index)
        add('body.root.location' + suffix, '根骨骼', 'location_delta', index, (-.02, .02))
    for side in ('L', 'R'):
        add('shoulder.' + side, '肩膀.' + side, 'quaternion_delta')
        extend_axes('shoulder.' + side)
        for part in ('fk.upper', 'fk.hand', 'ik.target', 'ik.pole'):
            extend_axes('arm.' + side + '.' + part)
        for part in ('fk.thigh', 'fk.foot', 'ik.target', 'ik.pole'):
            extend_axes('leg.' + side + '.' + part)
        for finger in ('thumb', 'index', 'middle', 'ring', 'pinky'):
            original = roles['finger.' + side + '.' + finger]
            # These local axes are candidate controls. Native constraints may
            # limit them; the case result must report that rather than guess.
            add(original['role_id'] + '.curl', original['bone'], 'quaternion_delta', 0, (-1.2, 1.2))
            add(original['role_id'] + '.splay', original['bone'], 'quaternion_delta', 2, (-.35, .35))
    add('body.stretch', '骨架设置', 'mode_enum', bounds=(0, 1), property_name='躯干拉伸')

    # The old leg slice deliberately allowed only .3 radians. A new full-body
    # candidate needs declared, reviewable pressure-pose ranges; this does not
    # modify the frozen source profiles or certify the native joint limits.
    for side in ('L', 'R'):
        for role_id, bounds in (('arm.' + side + '.fk.forearm', [-1.65, 1.65]),
                                ('leg.' + side + '.fk.shin', [-1.65, 1.65]),
                                ('leg.' + side + '.fk.thigh', [-1.1, 1.1]),
                                ('leg.' + side + '.fk.foot', [-.65, .65])):
            bounds_changes.append({'role': role_id, 'before': roles[role_id]['bounds'], 'after': bounds,
                                   'reason': 'new candidate pressure-pose input range; native limits remain unchanged'})
            roles[role_id]['bounds'] = bounds

    profile = deepcopy(base)
    profile.update(profile_id='anion.fullbody-controls.v094', revision=max(base['revision'], leg['revision']) + 1,
                   roles=list(roles.values()))
    profile = validate(profile)
    groups = []
    for group_id, label, prefixes in (
        ('body', '躯干与头部', ('body.', 'shoulder.')),
        ('arms', '双臂 FK / IK', ('arm.',)),
        ('legs', '双腿 FK / IK', ('leg.', 'toe.')),
        ('hands', '十指', ('finger.',)),
        ('face-existing', '既有视线与嘴部输入', ('gaze.', 'face.'))):
        members = [r for r in profile['roles'] if r['role_id'].startswith(prefixes)]
        groups.append({'id': group_id, 'label': label, 'roles': [r['role_id'] for r in members],
                       'bones': sorted({r['bone'] for r in members})})
    catalog = {'schema': 'hrig.fullbody-catalog.v1', 'profile_id': profile['profile_id'],
               'groups': groups, 'new_roles': sorted(set(roles) - inherited_ids),
               'bounds_changes': bounds_changes, 'role_count': len(profile['roles']),
               'units': {'location_delta': 'Blender local-basis units (BU)',
                         'euler_delta': 'radians', 'quaternion_delta': 'local-axis radians',
                         'scale_set': 'absolute scale component', 'mode_enum': 'ordinary endpoint 0 or 1'},
               'mode_method': 'endpoint selection only; no pose-preserving IK/FK snap',
               'scope_gaps': [{'area': 'garment/auxiliary controls', 'status': 'unknown',
                               'reason': 'provided bone inventory has no skirt/garment control records; its name-filtered coverage cannot prove these controls absent or accept garment integration'},
                              {'area': 'complete facial expressions', 'status': 'not_certified',
                               'reason': 'existing gaze/mouth roles are retained as basic integration; complete expression work remains a separate requirement'}],
               'qualification': 'mapping candidate; native response, animation and skin quality require actual verification'}
    return profile, catalog
