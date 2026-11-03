"""Finite native Cloth embedded-bake contract; pure JSON, never changes range."""
import hashlib
import json
import math
import re
import unicodedata

REQUEST_SCHEMA = 'hrig.cloth-cache.bake.v1'
MAX_TARGETS = 8
MIN_FRAME, MAX_FRAME = 1, 250
LIMITS = {'targets': [1, MAX_TARGETS], 'cache_frames': [MIN_FRAME, MAX_FRAME],
          'frame_step': 1, 'same_native_range_required': True,
          'cache_storage': 'embedded only; disk/external/baked/baking refused before bake',
          'failure_policy': 'discard owned worker candidate; no in-memory cache rollback guarantee'}


class CacheError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _fail(code, message, path):
    raise CacheError(code, message, {'path': path})


def _keys(value, expected, path):
    if type(value) is not dict or set(value) != set(expected):
        _fail('CACHE_SCHEMA', 'Object fields do not match the contract', path)


def _name(value, path):
    if (type(value) is not str or not value.strip() or len(value) > 1024
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)):
        _fail('CACHE_VALUE', 'Expected a readable name of 1 through 1024 characters', path)
    return value


def canonical_targets(value):
    if type(value) is not list or not 1 <= len(value) <= MAX_TARGETS:
        _fail('CACHE_SCHEMA', 'Expected 1 through 8 explicit Cloth targets', 'targets')
    result = []
    for ordinal, row in enumerate(value):
        path = 'targets[%d]' % ordinal
        _keys(row, ('object', 'modifier', 'index'), path)
        if type(row['index']) is not int or not 0 <= row['index'] <= 255:
            _fail('CACHE_VALUE', 'Expected integer modifier index0..255, not bool', path + '.index')
        result.append({'object': _name(row['object'], path + '.object'),
                       'modifier': _name(row['modifier'], path + '.modifier'), 'index': row['index']})
    if len({(row['object'], row['index']) for row in result}) != len(result):
        _fail('CACHE_DUPLICATE', 'Each Object/modifier-index target must be unique', 'targets')
    return sorted(result, key=lambda row: (row['object'], row['index'], row['modifier']))


def canonical_range(start, end, step):
    if (type(start) is not int or type(end) is not int or type(step) is not int
            or not MIN_FRAME <= start <= end <= MAX_FRAME or step != 1):
        _fail('CACHE_RANGE', 'Native cache must retain start1..end<=250 and step1', 'native_cache_range')
    return {'frame_start': start, 'frame_end': end, 'frame_step': step}


def canonical_request(value):
    _keys(value, ('schema', 'expected_snapshot_sha256', 'targets'), 'request')
    if value['schema'] != REQUEST_SCHEMA:
        _fail('CACHE_SCHEMA', 'Unsupported native Cloth cache bake schema', 'request.schema')
    sha = value['expected_snapshot_sha256']
    if type(sha) is not str or re.fullmatch('[0-9a-f]{64}', sha) is None:
        _fail('CACHE_VALUE', 'Expected lowercase SHA-256', 'request.expected_snapshot_sha256')
    return {'schema': REQUEST_SCHEMA, 'expected_snapshot_sha256': sha,
            'targets': canonical_targets(value['targets'])}


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
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise CacheError('CACHE_VALUE', 'Snapshot must be finite JSON')
    check(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CacheError('CACHE_VALUE', 'Snapshot is not finite UTF-8 JSON') from exc
