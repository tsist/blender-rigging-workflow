"""Unintegrated, bpy-free contract for an owned-worker observation draft."""
from __future__ import annotations

import copy
import json
import math

PLAN_SCHEMA = 'hrig.diagnostic-plan.v1'


class DiagnosticPlanError(ValueError):
    def __init__(self, message, code='INVALID_DIAGNOSTIC_PLAN'):
        super().__init__(message)
        self.code = code


def _require(condition, message):
    if not condition:
        raise DiagnosticPlanError(message)


def _fields(value, names, label):
    _require(type(value) is dict, label + ' must be an object')
    _require(set(value) == set(names), label + ' fields must be exactly ' + ', '.join(names))


def _name(value, label):
    _require(type(value) is str and bool(value) and '\x00' not in value,
             label + ' must be a nonempty string without NUL')


def _integer(value, lower, upper, label):
    _require(type(value) is int and lower <= value <= upper,
             label + ' must be an integer in [%s, %s]' % (lower, upper))


def _finite_tree(value):
    if type(value) is float:
        _require(math.isfinite(value), 'Nonfinite JSON number')
    elif type(value) is dict:
        _require(all(type(key) is str for key in value), 'Object keys must be strings')
        for item in value.values():
            _finite_tree(item)
    elif type(value) is list:
        for item in value:
            _finite_tree(item)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _constant(value):
    raise DiagnosticPlanError('Nonfinite JSON token: ' + value)


def parse_plan(raw):
    """Parse original JSON text with duplicate-key/NaN/Infinity rejection.

    A pre-parsed dict has already lost duplicate-key evidence. Entry runners
    must use this function when consuming a JSON file or raw JSON bytes.
    """
    _require(type(raw) in (str, bytes, bytearray), 'Plan JSON must be text or bytes')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except DiagnosticPlanError:
        raise
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DiagnosticPlanError('Malformed plan JSON: ' + str(exc)) from exc
    return validate_plan(value)


def validate_plan(plan):
    """Validate exact fields and return an independent plain-data copy."""
    _finite_tree(plan)
    _fields(plan, ('schema', 'context', 'frames', 'mode', 'objects', 'bones',
                   'full_vertex_budget'), 'plan')
    _require(plan['schema'] == PLAN_SCHEMA and type(plan['schema']) is str,
             'Expected schema ' + PLAN_SCHEMA)
    _fields(plan['context'], ('scene', 'view_layer'), 'context')
    for key in ('scene', 'view_layer'):
        _name(plan['context'][key], 'context.' + key)
    frames = plan['frames']
    _require(type(frames) is list and 1 <= len(frames) <= 64,
             'frames must contain 1..64 consecutive frame integers')
    for frame in frames:
        _integer(frame, -10000, 10000, 'frame')
    _require(all(second == first + 1 for first, second in zip(frames, frames[1:])),
             'frames must be strictly consecutive and forward')
    _require(type(plan['mode']) is str and plan['mode'] in ('timing', 'witness'),
             'mode must be timing or witness')
    _integer(plan['full_vertex_budget'], 1, 4000000, 'full_vertex_budget')
    objects = plan['objects']
    _require(type(objects) is list and 1 <= len(objects) <= 6,
             'objects must contain 1..6 explicit selections')
    names = set()
    selected_count = 0
    for row in objects:
        _fields(row, ('name', 'vertex_indices'), 'object selection')
        _name(row['name'], 'object.name')
        _require(row['name'] not in names, 'Duplicate selected object: ' + row['name'])
        names.add(row['name'])
        indices = row['vertex_indices']
        _require(type(indices) is list and len(indices) <= 64,
                 'vertex_indices must be a list of at most 64 indices')
        for index in indices:
            _integer(index, 0, 3999999, 'vertex index')
        _require(len(set(indices)) == len(indices), 'Duplicate selected vertex index')
        selected_count += len(indices)
    _require(selected_count <= 64, 'At most 64 selected vertices across all objects')
    bones = plan['bones']
    _require(type(bones) is list and len(bones) <= 48,
             'bones must be a list with at most 48 explicit selections')
    identities = set()
    for row in bones:
        _fields(row, ('armature', 'name'), 'bone selection')
        _name(row['armature'], 'bone.armature')
        _name(row['name'], 'bone.name')
        identity = (row['armature'], row['name'])
        _require(identity not in identities, 'Duplicate selected bone identity')
        identities.add(identity)
    return copy.deepcopy(plan)
