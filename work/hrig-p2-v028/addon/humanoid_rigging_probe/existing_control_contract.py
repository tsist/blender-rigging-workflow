"""Pure, finite Aon existing-property wire contract; never coerces JSON types."""
import copy
import hashlib
import json
import math
import ntpath
import posixpath
import re

SELECTION_SCHEMA = 'hrig.existing-control-properties.selection.v1'
REQUEST_SCHEMA = 'hrig.existing-control-properties.patch.v1'
OPERATION_SCHEMA = 'hrig.existing-control-properties.operation.v1'
ADAPTER = 'AON_HAIR_SWITCHES_V1'
RIG = '千早爱音-素体骨架'
BONE = '骨架设置'
KEYS = ('前发物理', '后发物理')
EFFECTS = [
    {'object': '千早爱音-' + side + '物理', 'modifier': '布料', 'index': 0,
     'type': 'CLOTH', 'field': field, 'control_key': side + '物理',
     'formula': 'int_gt_zero', 'driver_path': 'modifiers["布料"].' + field,
     'array_index': 0}
    for side in ('前发', '后发') for field in ('show_viewport', 'show_render')]


class SettingsError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def _fail(message, path, code='SETTINGS_SCHEMA'):
    raise SettingsError(code, message, {'path': path})


def _keys(value, keys, path):
    if type(value) is not dict or set(value) != set(keys):
        _fail('Expected exactly the declared object fields', path)


def _exact(value, expected, path):
    # Python equality alone accepts bool as int. JSON representation is typed.
    if type(value) is not type(expected):
        _fail('Native JSON type differs', path)
    if type(expected) is dict:
        _keys(value, expected, path)
        for key in expected:
            _exact(value[key], expected[key], path + '.' + key)
    elif type(expected) is list:
        if len(value) != len(expected):
            _fail('Exact ordered list required', path)
        for index, item in enumerate(expected):
            _exact(value[index], item, path + '[%d]' % index)
    elif value != expected:
        _fail('Value outside the finite adapter contract', path)


def _sha(value, path):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        _fail('Expected lowercase SHA-256', path)
    return value


def _path(value, path):
    if (type(value) is not str or not value or len(value) > 4096 or '\x00' in value
            or not (ntpath.isabs(value) or posixpath.isabs(value))):
        _fail('Expected a bounded absolute path', path)
    return value


def typed_value(value, path='value'):
    _keys(value, ('type', 'value'), path)
    if value['type'] != 'int' or type(value['type']) is not str:
        _fail('Existing int property required', path + '.type')
    if type(value['value']) is not int or value['value'] not in (0, 1):
        _fail('Expected int 0 or 1, never bool/float/string', path + '.value')
    return {'type': 'int', 'value': value['value']}


def canonical_selection(value):
    expected = {'schema': SELECTION_SCHEMA, 'adapter': ADAPTER,
                'context': {'scene': 'Scene', 'view_layer': 'ViewLayer', 'frame': 0, 'subframe': 0.0},
                'rig': RIG, 'control_bone': BONE, 'properties': list(KEYS), 'effects': EFFECTS}
    _exact(value, expected, 'selection')
    return copy.deepcopy(expected)


def canonical_request(value):
    _keys(value, ('schema', 'expected_snapshot_sha256', 'expected_definition_sha256', 'changes'), 'patch')
    _exact(value['schema'], REQUEST_SCHEMA, 'patch.schema')
    if type(value['changes']) is not list or len(value['changes']) != 2:
        _fail('Both ordered existing properties must be declared', 'patch.changes')
    changes = []
    for index, key in enumerate(KEYS):
        row, path = value['changes'][index], 'patch.changes[%d]' % index
        _keys(row, ('key', 'before', 'after'), path)
        _exact(row['key'], key, path + '.key')
        changes.append({'key': key, 'before': typed_value(row['before'], path + '.before'),
                        'after': typed_value(row['after'], path + '.after')})
    return {'schema': REQUEST_SCHEMA,
            'expected_snapshot_sha256': _sha(value['expected_snapshot_sha256'], 'patch.expected_snapshot_sha256'),
            'expected_definition_sha256': _sha(value['expected_definition_sha256'], 'patch.expected_definition_sha256'),
            'changes': changes}


def canonical_operation(value):
    _keys(value, ('schema', 'selection', 'patch', 'external_inputs', 'inherited_missing_weak_references'), 'operation')
    _exact(value['schema'], OPERATION_SCHEMA, 'operation.schema')
    external, missing = value['external_inputs'], value['inherited_missing_weak_references']
    if type(external) is not list or len(external) > 4096 or type(missing) is not list or len(missing) > 1024:
        _fail('Explicit bounded dependency lists required', 'operation')
    result, seen = [], set()
    for index, item in enumerate(external):
        path = 'operation.external_inputs[%d]' % index
        _keys(item, ('file', 'expected_sha256'), path)
        filename = _path(item['file'], path + '.file')
        token = ntpath.normpath(filename).casefold()
        if token in seen:
            _fail('Duplicate external dependency', path)
        seen.add(token)
        result.append({'file': filename, 'expected_sha256': _sha(item['expected_sha256'], path + '.expected_sha256')})
    weak, weak_seen = [], set()
    for index, filename in enumerate(missing):
        _path(filename, 'operation.inherited_missing_weak_references[%d]' % index)
        token = ntpath.normpath(filename).casefold()
        if token in weak_seen or token in seen:
            _fail('Duplicate or contradictory inherited weak dependency', 'operation.inherited_missing_weak_references')
        weak_seen.add(token)
        weak.append(filename)
    # Existence, bytes/SHA and native dependency closure are independently checked by the host.
    return {'schema': OPERATION_SCHEMA, 'selection': canonical_selection(value['selection']),
            'patch': canonical_request(value['patch']), 'external_inputs': result,
            'inherited_missing_weak_references': weak}


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
        raise SettingsError('SETTINGS_VALUE', 'Expected finite JSON data')
    check(value)
    try:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SettingsError('SETTINGS_VALUE', 'Expected finite UTF-8 JSON data') from exc
