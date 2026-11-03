"""Bounded original-RNA Cloth quality selection/patch contract; no bpy."""
import hashlib
import json
import math
import re
import unicodedata

REQUEST_SCHEMA = 'hrig.cloth-quality.patch.v1'
MAX_TARGETS = 8
QUALITY_MIN, QUALITY_MAX = 1, 80


class DynamicsError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _fail(code, message, path):
    raise DynamicsError(code, message, {'path': path})


def _keys(value, expected, path):
    if type(value) is not dict or set(value) != set(expected):
        _fail('DYNAMICS_SCHEMA', 'Object fields do not match the contract', path)


def _name(value, path):
    if (type(value) is not str or not value.strip() or len(value) > 1024
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)):
        _fail('DYNAMICS_VALUE', 'Expected a readable name of 1 through 1024 characters', path)
    return value


def _integer(value, minimum, maximum, path):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail('DYNAMICS_VALUE', 'Expected a bounded integer, not bool', path)
    return value


def canonical_quality(value, path='quality'):
    return _integer(value, QUALITY_MIN, QUALITY_MAX, path)


def _rows(value, name, patch=False):
    if type(value) is not list or not 1 <= len(value) <= MAX_TARGETS:
        _fail('DYNAMICS_SCHEMA', 'Expected 1 through 8 explicit Cloth targets', name)
    result = []
    for index, row in enumerate(value):
        path = '%s[%d]' % (name, index)
        fields = ('object', 'modifier', 'index', 'before', 'after') if patch else ('object', 'modifier', 'index')
        _keys(row, fields, path)
        record = {'object': _name(row['object'], path + '.object'),
                  'modifier': _name(row['modifier'], path + '.modifier'),
                  'index': _integer(row['index'], 0, 255, path + '.index')}
        if patch:
            record.update(before=canonical_quality(row['before'], path + '.before'),
                          after=canonical_quality(row['after'], path + '.after'))
        result.append(record)
    if len({(r['object'], r['index']) for r in result}) != len(result):
        _fail('DYNAMICS_DUPLICATE', 'Each Object/modifier-index target must be unique', name)
    return sorted(result, key=lambda row: (row['object'], row['index'], row['modifier']))


def canonical_targets(value):
    return _rows(value, 'targets')


def canonical_request(value):
    _keys(value, ('schema', 'expected_snapshot_sha256', 'changes'), 'request')
    if value['schema'] != REQUEST_SCHEMA:
        _fail('DYNAMICS_SCHEMA', 'Unsupported Cloth quality patch schema', 'request.schema')
    sha = value['expected_snapshot_sha256']
    if type(sha) is not str or re.fullmatch('[0-9a-f]{64}', sha) is None:
        _fail('DYNAMICS_VALUE', 'Expected lowercase SHA-256', 'request.expected_snapshot_sha256')
    return {'schema': REQUEST_SCHEMA, 'expected_snapshot_sha256': sha,
            'changes': _rows(value['changes'], 'request.changes', patch=True)}


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
        raise DynamicsError('DYNAMICS_VALUE', 'Snapshot must be finite JSON')
    check(value)
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DynamicsError('DYNAMICS_VALUE', 'Snapshot is not finite UTF-8 JSON') from exc
