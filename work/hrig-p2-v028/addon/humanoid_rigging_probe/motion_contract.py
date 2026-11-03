"""Bounded float32 rotation-only clip creation contract; no bpy or mutation."""
import re
import unicodedata
from . import pose_contract as pose

REQUEST_SCHEMA = 'hrig.motion-clip.create.v1'
MODES = pose.MODES


class MotionError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _fail(code, message, path=None, **details):
    raise MotionError(code, message, dict(details, **({'path': path} if path else {})))


def _keys(value, fields, path):
    if type(value) is not dict or set(value) != set(fields):
        _fail('MOTION_SCHEMA', 'Object fields do not match the clip contract', path)


def _bone(value, path):
    try:
        return pose._bone(value, path)
    except pose.PoseError as exc:
        raise MotionError(exc.code.replace('POSE_', 'MOTION_'), str(exc), exc.details) from exc


def canonical_name(value):
    if (type(value) is not str or not value.strip()
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)):
        _fail('MOTION_NAME', 'Expected a readable nonempty Action name', 'action_name')
    if len(value.encode('utf-8')) > 63:
        _fail('MOTION_NAME', 'Action name exceeds 63 UTF-8 bytes', 'action_name')
    return value


def canonical_bones(value):
    if type(value) is not list or not 1 <= len(value) <= 32:
        _fail('MOTION_SCHEMA', 'Expected 1 through 32 bone names', 'bones')
    result = [_bone(v, 'bones[%d]' % i) for i, v in enumerate(value)]
    if len(set(result)) != len(result):
        _fail('MOTION_DUPLICATE', 'Bone selection must be unique', 'bones')
    return sorted(result)


def canonical_rotation(mode, value, path='rotation'):
    try:
        return pose.canonical_rotation(mode, value, path)
    except pose.PoseError as exc:
        raise MotionError(exc.code.replace('POSE_', 'MOTION_'), str(exc), exc.details) from exc


def canonical_request(value):
    _keys(value, ('schema', 'action_name', 'expected_snapshot_sha256', 'tracks'), 'request')
    if value['schema'] != REQUEST_SCHEMA:
        _fail('MOTION_SCHEMA', 'Unsupported clip schema', 'request.schema')
    name = canonical_name(value['action_name'])
    sha = value['expected_snapshot_sha256']
    if type(sha) is not str or re.fullmatch('[0-9a-f]{64}', sha) is None:
        _fail('MOTION_VALUE', 'Expected lowercase SHA-256', 'request.expected_snapshot_sha256')
    tracks = value['tracks']
    if type(tracks) is not list or not 1 <= len(tracks) <= 32:
        _fail('MOTION_SCHEMA', 'Expected 1 through 32 rotation tracks', 'request.tracks')
    result, common_frames, total = [], None, 0
    for i, row in enumerate(tracks):
        path = 'request.tracks[%d]' % i
        _keys(row, ('bone', 'rotation_mode', 'before', 'keys'), path)
        bone, mode = _bone(row['bone'], path + '.bone'), row['rotation_mode']
        before = canonical_rotation(mode, row['before'], path + '.before')
        keys = row['keys']
        if type(keys) is not list or not 2 <= len(keys) <= 128:
            _fail('MOTION_SCHEMA', 'Expected 2 through 128 keys per track', path + '.keys')
        total += len(keys)
        if total > 4096:
            _fail('MOTION_SCHEMA', 'Clip exceeds 4096 vector keys', path + '.keys')
        points, frames, previous = [], [], None
        for j, key in enumerate(keys):
            kp = path + '.keys[%d]' % j
            _keys(key, ('frame', 'value'), kp)
            frame = key['frame']
            if type(frame) is not int or not 1 <= frame <= 250:
                _fail('MOTION_FRAME', 'Key frame must be an integer from 1 through 250', kp + '.frame')
            if frames and frame <= frames[-1]:
                _fail('MOTION_FRAME', 'Key frames must be strictly increasing', kp + '.frame')
            vector = canonical_rotation(mode, key['value'], kp + '.value')
            if mode == 'QUATERNION' and previous is not None and sum(a*b for a, b in zip(previous, vector)) < 0:
                _fail('MOTION_QUATERNION_SIGN', 'Adjacent quaternion keys must already have nonnegative dot', kp)
            frames.append(frame)
            points.append({'frame': frame, 'value': vector})
            previous = vector
        if points[0]['value'] != before:
            _fail('MOTION_FIRST_KEY', 'First key must equal the explicit before rotation', path)
        if common_frames is None:
            common_frames = frames
        elif common_frames != frames:
            _fail('MOTION_FRAME', 'All tracks must have the same key frame list', path)
        result.append({'bone': bone, 'rotation_mode': mode, 'before': before, 'keys': points})
    if len({row['bone'] for row in result}) != len(result):
        _fail('MOTION_DUPLICATE', 'Each bone may appear only once', 'request.tracks')
    return {'schema': REQUEST_SCHEMA, 'action_name': name, 'expected_snapshot_sha256': sha,
            'tracks': sorted(result, key=lambda row: row['bone'])}


def digest(value):
    try:
        return pose.digest(value)
    except pose.PoseError as exc:
        raise MotionError(exc.code.replace('POSE_', 'MOTION_'), str(exc), exc.details) from exc
