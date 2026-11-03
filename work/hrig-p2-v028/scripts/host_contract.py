"""Pure-host contracts for rigctl. No bpy imports or native execution.

These contracts describe the isolated candidate tool, not public blenderctl
rigging commands. Unknown fields, duplicate JSON keys and nonfinite numbers
are refused before a Blender worker is submitted.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

OP_SCHEMA = 'hrig.operation.v1'
BUNDLE_SCHEMA = 'hrig.cli.bundle.v1'
FRONTEND_VERSION = '0.15.0'
SHA = re.compile(r'[0-9a-f]{64}')
NEGATIVE_CASES = ('wrong_source', 'external_driver', 'empty_witness',
                  'changed_weight', 'modifier_disabled', 'quaternion_w_lock',
                  'arbitrary_fk_twist', 'duplicate_module')
CODES = {'INVALID_REQUEST': 2, 'NOT_FOUND': 3, 'CONFLICT': 4,
         'WORKER_FAILED': 5, 'TIMEOUT': 6, 'CANCELLED': 7,
         'VALIDATION_FAILED': 8, 'IO_ERROR': 9, 'UNSUPPORTED': 10}


class ContractError(ValueError):
    def __init__(self, code, message, *, exit_code=None):
        self.code = code
        self.exit_code = exit_code if exit_code is not None else CODES.get(code, 5)
        super().__init__(message)


def require(condition, message, code='INVALID_REQUEST'):
    if not condition:
        raise ContractError(code, message)


def fields(value, required, optional=(), label='object'):
    require(type(value) is dict, label + ' must be an object')
    require(set(required) <= set(value), label + ' missing fields: ' +
            ', '.join(sorted(set(required) - set(value))))
    require(not set(value) - set(required) - set(optional),
            label + ' has unknown fields: ' +
            ', '.join(sorted(set(value) - set(required) - set(optional))))


def text(value, label, maximum=1024):
    require(type(value) is str and 0 < len(value) <= maximum and '\x00' not in value,
            label + ' must be a nonempty bounded string')
    return value


def number(value, label, minimum=None, maximum=None):
    try:
        good = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        good = False
    require(good, label + ' must be a finite number')
    require(minimum is None or value >= minimum, label + ' is below its minimum')
    require(maximum is None or value <= maximum, label + ' exceeds its maximum')
    return value


def finite_tree(value):
    if type(value) in (float, int):
        number(value, 'JSON number')
    elif type(value) is dict:
        for child in value.values():
            finite_tree(child)
    elif type(value) is list:
        for child in value:
            finite_tree(child)


def _pairs(items):
    value = {}
    for key, item in items:
        require(key not in value, 'Duplicate JSON key: ' + key)
        value[key] = item
    return value


def parse_json(raw):
    def bad_constant(value):
        raise ContractError('INVALID_REQUEST', 'Nonfinite JSON literal: ' + value)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=bad_constant)
        finite_tree(value)
        return value
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        raise ContractError('INVALID_REQUEST', 'Invalid JSON: ' + str(error)) from error


def read_json(path):
    path = Path(path)
    require(path.is_file(), 'Missing JSON file: ' + str(path), 'NOT_FOUND')
    require(path.stat().st_size <= 32 * 1024 * 1024,
            'JSON exceeds the 32 MiB host limit: ' + str(path))
    return parse_json(path.read_text(encoding='utf-8-sig'))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def absolute(value, *, exists=True):
    text(str(value), 'file path', 32768)
    path = Path(value)
    require(path.is_absolute(), 'Absolute file path required: ' + str(path))
    if exists:
        require(path.is_file(), 'Missing input file: ' + str(path), 'NOT_FOUND')
    return path.resolve(strict=exists)


def descriptor(path, expected=None):
    path = absolute(path)
    if expected is not None:
        require(type(expected) is str and SHA.fullmatch(expected) is not None,
                'Expected SHA256 must be 64 lowercase hexadecimal characters')
    observed = sha256(path)
    require(expected is None or observed == expected,
            'Input SHA256 mismatch: ' + str(path), 'CONFLICT')
    return {'file': str(path), 'expected_sha256': observed}


def check_descriptor(value, *, verify=True):
    fields(value, ('file', 'expected_sha256'), label='descriptor')
    require(type(value['file']) is str, 'Descriptor file must be a string')
    path = Path(value['file'])
    require(path.is_absolute(), 'Descriptor path must be absolute')
    require(type(value['expected_sha256']) is str and
            SHA.fullmatch(value['expected_sha256']) is not None,
            'Descriptor expected_sha256 must be lowercase SHA256')
    if verify:
        descriptor(path, value['expected_sha256'])
    return value


def check_descriptors(values):
    require(type(values) is list, 'Descriptor set must be an array')
    seen = set()
    for value in values:
        check_descriptor(value)
        key = str(Path(value['file']).resolve()).casefold()
        require(key not in seen, 'Duplicate descriptor: ' + value['file'])
        seen.add(key)
    return values


def walk_descriptors(value):
    if type(value) is dict:
        if 'file' in value and 'expected_sha256' in value:
            yield check_descriptor(value)
            return
        for child in value.values():
            yield from walk_descriptors(child)
    elif type(value) is list:
        for child in value:
            yield from walk_descriptors(child)


def matrix(value, label):
    require(type(value) is list and len(value) == 4 and
            all(type(row) is list and len(row) == 4 for row in value),
            label + ' must be a row-major 4x4 array')
    for row in value:
        for cell in row:
            number(cell, label)
    require(max(abs(value[3][c] - (1 if c == 3 else 0)) for c in range(4)) <= 1e-7,
            label + ' must be affine')


def context(value):
    fields(value, ('scene', 'view_layer', 'frame', 'subframe'), label='context')
    text(value['scene'], 'scene')
    text(value['view_layer'], 'view_layer')
    require(type(value['frame']) is int, 'frame must be an integer')
    number(value['frame'], 'frame', -1000000, 1000000)
    number(value['subframe'], 'subframe', 0, 1)
    require(value['subframe'] < 1, 'subframe must be less than 1')


def spec(value):
    fields(value, ('module_id',), ('name', 'side', 'rest_roll', 'object_uniform_scale'), 'spec')
    require(type(value['module_id']) is str and
            re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['module_id']) is not None,
            'module_id must contain 1..64 ASCII letters/digits/_/-')
    if 'name' in value:
        text(value['name'], 'name', 80)
    if 'side' in value:
        number(value['side'], 'side')
        require(value['side'] in (-1, 1), 'side must equal -1 or 1')
    if 'rest_roll' in value:
        number(value['rest_roll'], 'rest_roll')
    if 'object_uniform_scale' in value:
        number(value['object_uniform_scale'], 'object_uniform_scale', 1e-3, 1e3)


def operation(value):
    require(type(value) is dict, 'Operation must be a JSON object')
    require(value.get('schema') == OP_SCHEMA, 'Expected schema ' + OP_SCHEMA)
    action = value.get('action')
    common = ('schema', 'action')
    if action == 'create_limb':
        fields(value, common + ('spec',), label='create_limb operation')
        spec(value['spec'])
        return value
    if action in ('match_world', 'switch_space'):
        extras = ('target_world',) if action == 'match_world' else ('property',)
        fields(value, common + ('context', 'target', 'expected_current_world') + extras,
               label=action + ' operation')
        context(value['context'])
        fields(value['target'], ('rig', 'bone'), label='target')
        for key in ('rig', 'bone'):
            text(value['target'][key], 'target.' + key)
        matrix(value['expected_current_world'], 'expected_current_world')
        if action == 'match_world':
            matrix(value['target_world'], 'target_world')
        else:
            prop = value['property']
            fields(prop, ('bone', 'key', 'expected', 'value'), label='property')
            text(prop['bone'], 'property.bone')
            text(prop['key'], 'property.key')
            for key in ('expected', 'value'):
                number(prop[key], 'property.' + key)
                require(prop[key] in (0, 1), 'Space selectors must be endpoints 0/1')
        return value
    if action in ('snap_ik_to_fk', 'snap_fk_to_ik'):
        fields(value, common + ('context', 'manifest', 'expected_selector'), label=action)
        context(value['context'])
        check_descriptor(value['manifest'])
        number(value['expected_selector'], 'expected_selector')
        require(value['expected_selector'] == (1 if action == 'snap_ik_to_fk' else 0),
                'Snap direction and expected_selector disagree')
        return value
    raise ContractError('UNSUPPORTED', 'Unknown operation action: ' + str(action))


def operation_schemas():
    """Small discovery schemas; procedural checks above remain authoritative."""
    string = {'type': 'string', 'minLength': 1}
    desc = {'type': 'object', 'additionalProperties': False,
            'required': ['file', 'expected_sha256'], 'properties': {
                'file': string, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}}
    mat = {'type': 'array', 'minItems': 4, 'maxItems': 4, 'items': {
        'type': 'array', 'minItems': 4, 'maxItems': 4, 'items': {'type': 'number'}}}
    ctx = {'type': 'object', 'additionalProperties': False,
           'required': ['scene', 'view_layer', 'frame', 'subframe'], 'properties': {
               'scene': string, 'view_layer': string, 'frame': {'type': 'integer'},
               'subframe': {'type': 'number', 'minimum': 0, 'exclusiveMaximum': 1}}}
    specs = {'type': 'object', 'additionalProperties': False, 'required': ['module_id'],
             'properties': {'module_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,64}$'},
                            'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                            'side': {'enum': [-1, 1]}, 'rest_roll': {'type': 'number'},
                            'object_uniform_scale': {'type': 'number', 'minimum': 1e-3, 'maximum': 1e3}}}
    target = {'type': 'object', 'additionalProperties': False,
              'required': ['rig', 'bone'], 'properties': {'rig': string, 'bone': string}}
    prop = {'type': 'object', 'additionalProperties': False,
            'required': ['bone', 'key', 'expected', 'value'], 'properties': {
                'bone': string, 'key': string, 'expected': {'enum': [0, 1]}, 'value': {'enum': [0, 1]}}}
    definitions = {
        'create_limb': {'spec': specs},
        'match_world': {'context': ctx, 'target': target, 'target_world': mat, 'expected_current_world': mat},
        'switch_space': {'context': ctx, 'target': target, 'property': prop, 'expected_current_world': mat},
        'snap_ik_to_fk': {'context': ctx, 'manifest': desc, 'expected_selector': {'const': 1}},
        'snap_fk_to_ik': {'context': ctx, 'manifest': desc, 'expected_selector': {'const': 0}}}
    return {action: {'type': 'object', 'additionalProperties': False,
                     'required': ['schema', 'action', *props],
                     'properties': {'schema': {'const': OP_SCHEMA}, 'action': {'const': action}, **props}}
            for action, props in definitions.items()}
