"""Finite, float32 active-rotation patch contract; no bpy or mutation."""
import hashlib
import json
import math
import re
import struct
import unicodedata

REQUEST_SCHEMA = 'hrig.pose-rotations.patch.v1'
LOCATION_REQUEST_SCHEMA = 'hrig.pose-locations.patch.v1'
LOCATION_BOUND = 1000000.0
MODES = ('QUATERNION', 'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX')


class PoseError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _fail(code, message, path, **details):
    raise PoseError(code, message, {'path': path, **details})


def _keys(value, fields, path):
    if type(value) is not dict or set(value) != set(fields):
        _fail('POSE_SCHEMA', 'Object fields do not match the contract', path)


def _bone(value, path):
    if (type(value) is not str or not value.strip() or len(value) > 128
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)):
        _fail('POSE_VALUE', 'Expected a readable nonempty bone name up to 128 characters', path)
    return value


def canonical_bones(value):
    if type(value) is not list or not 1 <= len(value) <= 256:
        _fail('POSE_SCHEMA', 'Expected 1 through 256 bone names', 'bones')
    result = [_bone(v, 'bones[%d]' % i) for i, v in enumerate(value)]
    if len(set(result)) != len(result):
        _fail('POSE_DUPLICATE', 'Bone selection must be unique', 'bones')
    return sorted(result)


def canonical_rotation(mode, value, path='rotation'):
    if type(mode) is not str or mode not in MODES:
        _fail('POSE_MODE', 'Unsupported active rotation mode', path + '.rotation_mode')
    size = 4 if mode == 'QUATERNION' else 3
    if type(value) is not list or len(value) != size:
        _fail('POSE_VALUE', 'Active rotation has the wrong vector length', path)
    bound = 1.001 if size == 4 else 8 * math.pi
    result = []
    for i, v in enumerate(value):
        if type(v) not in (int, float):
            _fail('POSE_VALUE', 'Expected finite numbers, not bool', path + '[%d]' % i)
        try:
            number = float(v)
        except (OverflowError, ValueError):
            _fail('POSE_VALUE', 'Number cannot be represented', path)
        if not math.isfinite(number) or abs(number) > bound:
            _fail('POSE_VALUE', 'Rotation component is outside its finite bound', path + '[%d]' % i)
        number = struct.unpack('!f', struct.pack('!f', number))[0]
        if abs(number) > bound:
            _fail('POSE_VALUE', 'Float32 rotation crosses the permitted bound', path + '[%d]' % i)
        result.append(0.0 if number == 0.0 else number)
    if size == 4 and abs(math.sqrt(sum(v * v for v in result)) - 1.0) > 2e-5:
        _fail('POSE_VALUE', 'Quaternion must already be unit length within 2e-5', path)
    return result


def canonical_request(value):
    _keys(value, ('schema', 'expected_snapshot_sha256', 'rotations'), 'request')
    if value['schema'] != REQUEST_SCHEMA:
        _fail('POSE_SCHEMA', 'Unsupported pose rotation schema', 'request.schema')
    sha = value['expected_snapshot_sha256']
    if type(sha) is not str or re.fullmatch('[0-9a-f]{64}', sha) is None:
        _fail('POSE_VALUE', 'Expected lowercase SHA-256', 'request.expected_snapshot_sha256')
    rows = value['rotations']
    if type(rows) is not list or not 1 <= len(rows) <= 256:
        _fail('POSE_SCHEMA', 'Expected 1 through 256 rotation rows', 'request.rotations')
    result = []
    for i, row in enumerate(rows):
        path = 'request.rotations[%d]' % i
        _keys(row, ('bone', 'rotation_mode', 'before', 'after'), path)
        name = _bone(row['bone'], path + '.bone')
        mode = row['rotation_mode']
        before = canonical_rotation(mode, row['before'], path + '.before')
        after = canonical_rotation(mode, row['after'], path + '.after')
        result.append({'bone': name, 'rotation_mode': mode, 'before': before, 'after': after})
    if len({r['bone'] for r in result}) != len(result):
        _fail('POSE_DUPLICATE', 'Each bone may appear only once', 'request.rotations')
    return {'schema': REQUEST_SCHEMA, 'expected_snapshot_sha256': sha,
            'rotations': sorted(result, key=lambda r: r['bone'])}


def digest(value):
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
            return
        raise PoseError('POSE_VALUE', 'Snapshot must be finite JSON')
    check(value)
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PoseError('POSE_VALUE', 'Snapshot is not finite UTF-8 JSON') from exc


def canonical_location_vector(value, path='location'):
    if type(value) is not list or len(value) != 3:
        _fail('LOCATION_VALUE', 'Location requires exactly three components', path)
    result = []
    for index, item in enumerate(value):
        if type(item) not in (int, float):
            _fail('LOCATION_VALUE', 'Expected finite location numbers, not bool', path)
        try:
            number = float(item)
            if not math.isfinite(number) or abs(number) > LOCATION_BOUND:
                _fail('LOCATION_VALUE', 'Location component exceeds finite bound', path)
            stored = struct.unpack('<f', struct.pack('<f', number))[0]
        except (ValueError, OverflowError, struct.error):
            _fail('LOCATION_VALUE', 'Location is not representable as float32', path)
        if not math.isfinite(stored) or abs(stored) > LOCATION_BOUND or (number != 0 and stored == 0):
            _fail('LOCATION_VALUE', 'Location float32 overflow/bound/underflow', path + '[%d]' % index)
        result.append(0.0 if stored == 0 else stored)
    return result


def canonical_location_request(value):
    _keys(value, ('schema', 'expected_snapshot_sha256', 'locations'), 'request')
    if value['schema'] != LOCATION_REQUEST_SCHEMA:
        _fail('POSE_SCHEMA', 'Unsupported local location schema', 'request.schema')
    sha = value['expected_snapshot_sha256']
    if type(sha) is not str or re.fullmatch('[0-9a-f]{64}', sha) is None:
        _fail('POSE_VALUE', 'Expected lowercase SHA-256', 'request.expected_snapshot_sha256')
    rows = value['locations']
    if type(rows) is not list or not 1 <= len(rows) <= 256:
        _fail('POSE_SCHEMA', 'Expected 1 through 256 location rows', 'request.locations')
    result = []
    for index, row in enumerate(rows):
        path = 'request.locations[%d]' % index
        _keys(row, ('bone', 'before', 'after'), path)
        result.append({'bone': _bone(row['bone'], path + '.bone'),
            'before': canonical_location_vector(row['before'], path + '.before'),
            'after': canonical_location_vector(row['after'], path + '.after')})
    if len({row['bone'] for row in result}) != len(result):
        _fail('POSE_DUPLICATE', 'Each bone may appear only once', 'request.locations')
    return {'schema': LOCATION_REQUEST_SCHEMA, 'expected_snapshot_sha256': sha,
            'locations': sorted(result, key=lambda row: row['bone'])}
