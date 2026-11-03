"""Pure, explicit encoding for the only indirect-driver form supported here."""
from copy import deepcopy

POINT_FIELDS = ('co', 'handle_left', 'handle_right', 'handle_left_type',
                'handle_right_type', 'interpolation', 'type', 'easing', 'back',
                'amplitude', 'period', 'select_control_point', 'select_left_handle',
                'select_right_handle')
TARGET_FIELDS = ('id', 'id_type', 'data_path', 'bone_target', 'transform_type',
                 'rotation_mode', 'transform_space', 'context_property',
                 'use_fallback_value', 'fallback_value', 'is_fallback_used')


def copy_definition(raw):
    driver = raw['driver']
    variables = driver['variables']
    variable = variables[0] if len(variables) == 1 else {}
    targets = variable.get('targets', [])
    return {'path': raw['path'], 'index': raw['index'], 'mute': raw['mute'],
            'extrapolation': raw['extrapolation'], 'driver_type': driver['type'],
            'expression': driver['expression'], 'use_self': driver['use_self'],
            'variable_count': len(variables), 'variable_name': variable.get('name'),
            'variable_type': variable.get('type'), 'target_count': len(targets),
            'targets': [{k: deepcopy(t[k]) for k in TARGET_FIELDS} for t in targets],
            'keyframes': [{k: deepcopy(p[k]) for k in POINT_FIELDS} for p in raw['keyframes']],
            'sampled_points': deepcopy(raw['sampled_points']), 'modifiers': deepcopy(raw['modifiers'])}
