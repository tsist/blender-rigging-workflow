# SPDX-License-Identifier: GPL-3.0-or-later
# Canonical candidate relocation; see SOURCE-MAP.json.
import math
import re
from pathlib import Path
from .io_support import Failure
FORMATS = ('GLB', 'FBX')
BOOL = {'type': 'boolean'}
TEXT={'type':'string','minLength':1,'maxLength':1024}
SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}

def obj(properties, required=None):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(properties) if required is None else required}

def enum(*values):
    return {'enum': list(values)}

def array(item, minimum=0, maximum=1000):
    return {'type': 'array', 'items': item, 'minItems': minimum, 'maxItems': maximum}

def number(low=-1000000000.0, high=1000000000.0):
    return {'type': 'number', 'minimum': low, 'maximum': high}

def integer(low, high):
    return {'type': 'integer', 'minimum': low, 'maximum': high}
NAME = {'type': 'string', 'minLength': 1, 'maxLength': 63, 'pattern': '^[^\\x00-\\x1f\\x7f]+$'}

def validate(value, schema, path='$'):

    def fail(message):
        raise Failure('INVALID_REQUEST', path + ': ' + message)
    if 'oneOf' in schema:
        matches = 0
        for s in schema['oneOf']:
            try:
                validate(value, s, path)
                matches += 1
            except Failure:
                pass
        if matches != 1:
            fail('does not match exactly one allowed shape')
        return
    types = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list), 'string': lambda v: isinstance(v, str), 'number': lambda v: type(v) is int or (type(v) is float and math.isfinite(v)), 'integer': lambda v: type(v) is int, 'boolean': lambda v: type(v) is bool, 'null': lambda v: v is None}
    if 'type' in schema and (not types[schema['type']](value)):
        fail('invalid type')
    if 'const' in schema and value != schema['const']:
        fail('invalid constant')
    if 'enum' in schema and value not in schema['enum']:
        fail('invalid enum')
    if type(value) in (int, float):
        if not schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf):
            fail('number outside range')
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', math.inf) or ('pattern' in schema and (not re.search(schema['pattern'], value))):
            fail('invalid string')
        if schema == NAME and len(value.encode('utf-8')) > 63:
            fail('name exceeds 63 UTF-8 bytes')
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', math.inf):
            fail('array outside bounds')
        for i, v in enumerate(value):
            validate(v, schema.get('items', {}), f'{path}/{i}')
        if schema.get('uniqueItems') and len(set(value)) != len(value):
            fail('duplicate items')
    if isinstance(value, dict):
        props = schema.get('properties', {})
        if set(schema.get('required', [])) - set(value) or (schema.get('additionalProperties') is False and set(value) - set(props)):
            fail('missing or unknown fields')
        for k, v in value.items():
            validate(v, props.get(k, {}), path + '/' + k)
FILE = obj({'file': TEXT, 'expected_sha256': SHA})
PROFILE = obj({'mode': {'const': 'NATIVE_SIMPLE_V1'}, 'source_sha256': SHA, 'inventory_sha256': SHA, 'driver_count': integer(0, 4096), 'acknowledged_unresolved': {**array(SHA, 0, 4096), 'uniqueItems': True}})
RIG_SAMPLING = obj({'seed_substeps': {'type': 'integer', **enum(1, 2, 4, 8)}, 'max_depth': integer(0, 8), 'max_evaluations': integer(5, 4096), 'max_vertex_samples': integer(1, 256000000), 'max_affine_joints': integer(1, 2048), 'max_output_bytes': integer(1, 268435456), 'max_position_error': number(1e-07, 0.001)})
EXPORT = obj({'format': enum(*FORMATS), 'scene': NAME, 'view_layer': NAME, 'objects': {**array(NAME, 1, 100), 'uniqueItems': True}, 'mode': enum('STATIC', 'ANIMATION'), 'frame_start': integer(1, 100000), 'frame_end': integer(1, 100000), 'meters_per_unit': number(1e-06, 1000000), 'materials': enum('NONE', 'GLTF_PBR')})
IMPORT = obj({'format': enum(*FORMATS), 'scene': NAME, 'frame': integer(1, 100000), 'fps': integer(1, 240), 'scale_to_meters': number(1e-06, 1000000), 'mode': enum('STATIC', 'ANIMATION')})

SHARED_RIG_ADAPTER='BBONE_SHARED_CONTINUOUS_V1'
SHARED_IMPORT_ADAPTER='SHARED_RIG_NATIVE_TIME_V1'
# New fields are optional; omission keeps the original 32 sampling tables.
for _field in ('witness_substeps','format_substeps'):
    RIG_SAMPLING['properties'][_field]={'type':'integer','enum':[1,2,4,8,32]}
EXPORT['properties'].update(rig_adapter={'const':SHARED_RIG_ADAPTER},rig_sampling=RIG_SAMPLING)
EXPORT['required'] += ['rig_adapter','rig_sampling']
EXPORT['properties'].update(format=enum('GLB','FBX'),mode={'const':'ANIMATION'},materials={'const':'NONE'},meters_per_unit={'const':1},objects={**array(NAME,2,17),'uniqueItems':True})
IMPORT['properties'].update(format=enum('GLB','FBX'),mode={'const':'ANIMATION'},scale_to_meters={'const':1},rig_adapter={'const':SHARED_IMPORT_ADAPTER})
IMPORT['required'].append('rig_adapter')
def normalize_profile(value):
    validate(value,PROFILE);return value
def normalize_export(value):
    validate(value,EXPORT)
    if value['frame_end']<=value['frame_start']:raise Failure('INVALID_REQUEST','Shared export requires positive frame interval')
    return value
def normalize_import(value):
    validate(value,IMPORT);return value
