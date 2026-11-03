"""Explicit additive derivation of one already-declared rotation clip; no bpy."""
from . import motion_contract as motion

REQUEST_SCHEMA = 'hrig.motion-clip.derive.v1'
MODES = motion.MODES
DerivationError = motion.MotionError


def _fail(code, message, path=None):
    raise DerivationError(code, message, {'path': path} if path else {})


def canonical_bones(value):
    return motion.canonical_bones(value)


def canonical_name(value):
    return motion.canonical_name(value)


def canonical_base(value):
    return motion.canonical_request(value)


def canonical_request(value):
    motion._keys(value, ('schema', 'expected_snapshot_sha256', 'base_clip', 'action_name', 'tracks'), 'derivation')
    if value['schema'] != REQUEST_SCHEMA:
        _fail('DERIVATION_SCHEMA', 'Expected ' + REQUEST_SCHEMA, 'derivation.schema')
    base = canonical_base(value['base_clip'])
    added = motion.canonical_request({'schema': motion.REQUEST_SCHEMA, 'action_name': value['action_name'],
        'expected_snapshot_sha256': value['expected_snapshot_sha256'], 'tracks': value['tracks']})
    if added['action_name'] == base['action_name']:
        _fail('DERIVATION_NAME', 'Derived Action must have a distinct new name', 'derivation.action_name')
    if {row['bone'] for row in base['tracks']} & {row['bone'] for row in added['tracks']}:
        _fail('DERIVATION_OVERLAP', 'Added tracks must not replace any original bone track', 'derivation.tracks')
    # The old pure contract independently enforces combined32 bones,4096 vector
    # keys, float32 values, first-key==before, quaternion continuity and exactly
    # the same key-frame list across both the old and appended tracks.
    motion.canonical_request({'schema': motion.REQUEST_SCHEMA, 'action_name': added['action_name'],
        'expected_snapshot_sha256': added['expected_snapshot_sha256'], 'tracks': base['tracks'] + added['tracks']})
    return {'schema': REQUEST_SCHEMA, 'expected_snapshot_sha256': added['expected_snapshot_sha256'],
            'base_clip': base, 'action_name': added['action_name'], 'tracks': added['tracks']}


def combined_request(value):
    """Verification envelope only; never authorization to call old create_clip."""
    request = canonical_request(value)
    return motion.canonical_request({'schema': motion.REQUEST_SCHEMA, 'action_name': request['action_name'],
        'expected_snapshot_sha256': request['expected_snapshot_sha256'],
        'tracks': request['base_clip']['tracks'] + request['tracks']})


def digest(value):
    return motion.digest(value)
