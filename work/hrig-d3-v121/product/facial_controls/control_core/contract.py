"""Pure data contract; profiles never contain Python or arbitrary RNA requests."""
from copy import deepcopy
import hashlib, json, math

SCHEMA = 'hrig.facial-control-profile.v3'
KINDS = frozenset(('location_delta', 'euler_delta', 'quaternion_delta', 'scale_set', 'mode_enum'))


class ControlError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def require(ok, code, detail=None):
    if not ok: raise ControlError(code, detail)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def exact(value, names, path):
    require(type(value) is dict and set(value) == set(names), 'PROFILE_FIELDS', path)


def text(value, path):
    require(type(value) is str and 0 < len(value) <= 1024 and '\x00' not in value, 'PROFILE_STRING', path)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(document):
    exact(document, ('schema', 'profile_id', 'revision', 'character_id', 'runtime', 'rig', 'roles', 'facial_binding'), 'root')
    require(document['schema'] == SCHEMA, 'PROFILE_VERSION')
    for field in ('profile_id', 'character_id'): text(document[field], field)
    require(type(document['revision']) is int and document['revision'] > 0, 'PROFILE_REVISION')
    runtime = document['runtime']
    exact(runtime, ('blender_version', 'build_hash', 'scene', 'view_layer'), 'runtime')
    require(type(runtime['blender_version']) is list and len(runtime['blender_version']) == 3 and all(type(v) is int for v in runtime['blender_version']), 'PROFILE_RUNTIME')
    for field in ('build_hash', 'scene', 'view_layer'): text(runtime[field], field)
    exact(document['rig'], ('object', 'data', 'witness_bones', 'rest_sha256', 'output_keys', 'consumer_rigs', 'indirect_output_keys'), 'rig')
    for field in ('object', 'data'): text(document['rig'][field], field)
    roster = document['rig']['witness_bones']
    require(type(roster) is list and 1 <= len(roster) <= 10000, 'WITNESS_ROSTER')
    for bone in roster: text(bone, 'witness')
    require(len(set(roster)) == len(roster), 'WITNESS_ROSTER')
    signature = document['rig']['rest_sha256']
    require(type(signature) is str and len(signature) == 64 and all(c in '0123456789abcdef' for c in signature), 'REST_SIGNATURE')
    require(type(document['rig']['output_keys']) is list and len(document['rig']['output_keys']) <= 128, 'OUTPUT_KEYS')
    for owner in document['rig']['output_keys']:
        exact(owner, ('object', 'data', 'key'), 'output_key')
        for field in ('object', 'data', 'key'): text(owner[field], field)
    require(type(document['rig']['consumer_rigs']) is list and len(document['rig']['consumer_rigs']) <= 8, 'CONSUMER_RIGS')
    for owner in document['rig']['consumer_rigs']:
        exact(owner, ('object', 'data', 'rest_sha256'), 'consumer_rig')
        for field in ('object', 'data', 'rest_sha256'): text(owner[field], field)
    require(type(document['roles']) is list and 1 <= len(document['roles']) <= 256, 'PROFILE_ROLES')
    seen = set()
    for role in document['roles']:
        exact(role, ('role_id', 'bone', 'parent', 'rotation_mode', 'kind', 'index', 'axis', 'property', 'bounds', 'requires', 'label'), 'role')
        for field in ('role_id', 'bone', 'rotation_mode', 'kind', 'label'): text(role[field], field)
        require(role['role_id'] not in seen, 'DUPLICATE_ROLE'); seen.add(role['role_id'])
        require(role['bone'] in roster and role['kind'] in KINDS, 'ROLE_UNSUPPORTED')
        require(role['parent'] is None or type(role['parent']) is str, 'PARENT_SCHEMA')
        require(type(role['index']) is int and 0 <= role['index'] <= 2, 'INDEX_SCHEMA')
        require(type(role['bounds']) is list and len(role['bounds']) == 2 and all(finite(v) for v in role['bounds']) and role['bounds'][0] <= role['bounds'][1], 'INPUT_BOUNDS')
        require(type(role['axis']) is list and len(role['axis']) == 3 and all(finite(v) for v in role['axis']), 'AXIS_SCHEMA')
        if role['kind'] == 'quaternion_delta': require(abs(sum(v*v for v in role['axis']) - 1) <= 1e-6, 'AXIS_NOT_UNIT')
        if role['kind'] == 'mode_enum': text(role['property'], 'property')
        else: require(role['property'] is None, 'UNEXPECTED_PROPERTY')
        require(type(role['requires']) is list and len(role['requires']) <= 4, 'MODE_REQUIREMENTS')
        for guard in role['requires']:
            exact(guard, ('bone', 'property', 'value'), 'requires')
            text(guard['bone'], 'guard.bone'); text(guard['property'], 'guard.property')
            require(guard['bone'] in roster and finite(guard['value']) and guard['value'] in (0, 1), 'MODE_REQUIREMENTS')
    from .indirect_contract import validate_indirect, validate_binding
    validate_indirect(document['rig']['output_keys'], document['rig']['indirect_output_keys'])
    validate_binding(document['facial_binding'], document['roles'])
    return deepcopy(document)


def decode(raw):
    def pairs(items):
        answer = {}
        for key, value in items:
            require(key not in answer, 'DUPLICATE_JSON_KEY', key); answer[key] = value
        return answer
    try: value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        if isinstance(error, ControlError): raise
        raise ControlError('PROFILE_JSON') from error
    return validate(value)


def validate_operations(profile, operations):
    require(type(operations) is list and 1 <= len(operations) <= 24, 'BATCH_SCHEMA')
    roles = {role['role_id']: role for role in profile['roles']}
    answer = []; used = set()
    for operation in operations:
        exact(operation, ('role', 'value'), 'operation')
        text(operation['role'], 'operation.role')
        require(operation['role'] in roles, 'ROLE_UNSUPPORTED', operation['role'])
        role = roles[operation['role']]; value = operation['value']
        require(finite(value) and role['bounds'][0] <= value <= role['bounds'][1], 'INPUT_BOUNDS', operation['role'])
        if role['kind'] == 'mode_enum': require(value in (0, 1), 'MODE_ENUM')
        if role['kind'] == 'mode_enum':
            key = (role['bone'], 'property', role['property'])
            require(key not in used, 'OVERLAPPING_INPUT', key)
        elif role['kind'] == 'location_delta':
            key = (role['bone'], 'location', role['index'])
            require(key not in used and (role['bone'], 'transform') not in used, 'OVERLAPPING_INPUT', key)
        else:
            key = (role['bone'], 'transform')
            require(key not in used and not any(k[:2] == (role['bone'], 'location') for k in used), 'OVERLAPPING_INPUT', key)
        used.add(key)
        answer.append((deepcopy(role), float(value)))
    return answer


def merge_location(original, deltas):
    """Pure one-vector f32 projection; duplicate indices are forbidden."""
    import struct
    require(type(original) is list and len(original) == 3 and all(finite(x) for x in original), 'LOCATION_VECTOR')
    expected = list(original); seen = set()
    for index, delta in deltas:
        require(type(index) is int and 0 <= index <= 2 and index not in seen and finite(delta), 'LOCATION_COMPONENT')
        seen.add(index)
        try: expected[index] = struct.unpack('<f', struct.pack('<f', original[index] + delta))[0]
        except (OverflowError, struct.error): raise ControlError('LOCATION_OVERFLOW')
        require(finite(expected[index]), 'LOCATION_OVERFLOW')
    return expected
