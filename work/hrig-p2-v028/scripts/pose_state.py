"""Exact selected active-rotation mask over the declared character observer."""
import copy


def protected(snapshot, rig_name, armature_name, request):
    result = copy.deepcopy(snapshot)
    objects = [b for b in result['blocks'] if b['type'] == 'Object'
               and b['name'] == rig_name and b['library'] is None]
    arms = [b for b in result['blocks'] if b['type'] == 'Armature'
            and b['name'] == armature_name and b['library'] is None]
    if len(objects) != 1 or len(arms) != 1:
        raise ValueError('Pose target observation ambiguous')
    obj = objects[0]
    for row in request['rotations']:
        bone = row['bone']
        field = 'rotation_quaternion' if row['rotation_mode'] == 'QUATERNION' else 'rotation_euler'
        matches = [p for p in obj['pose'] if p['name'] == bone]
        if len(matches) != 1:
            raise ValueError('Pose observation missing or ambiguous')
        pb = matches[0]
        # matrix_basis is a second representation of the selected local LRS.
        # It is not an authorization to change location, scale or another mode:
        # those original channels remain strictly compared below this mask.
        pb.pop('matrix_basis')
        for settings in (pb['settings'], obj['character_extra']['pose_custom'][bone]):
            settings.pop(field)
            settings.pop('matrix_basis', None)
    return result


def protected_locations(snapshot, rig_name, armature_name, request):
    if request['schema'] != 'hrig.pose-locations.patch.v1':
        raise ValueError('Explicit location schema required')
    result = copy.deepcopy(snapshot)
    objects = [row for row in result['blocks'] if row['type'] == 'Object'
        and row['name'] == rig_name and row['library'] is None]
    arms = [row for row in result['blocks'] if row['type'] == 'Armature'
        and row['name'] == armature_name and row['library'] is None]
    if len(objects) != 1 or len(arms) != 1:
        raise ValueError('Location target observation ambiguous')
    obj = objects[0]
    for row in request['locations']:
        name = row['bone'];matches = [pb for pb in obj['pose'] if pb['name'] == name]
        if len(matches) != 1:
            raise ValueError('Location bone observation missing or ambiguous')
        pb = matches[0];custom = obj['character_extra']['pose_custom'][name]
        if pb['settings']['location'] != custom['location']:
            raise ValueError('Location storage aliases disagree')
        if not (pb['matrix_basis'] == pb['settings']['matrix_basis'] == custom['matrix_basis']):
            raise ValueError('Location matrix_basis aliases disagree')
        del pb['matrix_basis'];del pb['settings']['matrix_basis'];del custom['matrix_basis']
        del pb['settings']['location'];del custom['location']
    return result
