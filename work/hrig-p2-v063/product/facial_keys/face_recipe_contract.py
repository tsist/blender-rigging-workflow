"""Pure data contract and deterministic kernel for HRIG facial key recipes.

This module must remain importable outside Blender.  It has no bpy dependency,
does not read files, and does not mutate any native datablock.  The native
adapter in :mod:`facial_keys` uses the same validation and coordinate kernel.

The public plan shape is intentionally exact.  Names are opaque: a source key
name or a recipe name is never interpreted as a semantic facial region.
"""

import hashlib
import json
import math
import re
import struct


SCHEMA = 'hrig.face-recipes.v1'
LEDGER_SCHEMA = 'hrig.face-recipes.ledger.v1'
LEDGER_PROPERTY = 'hrig_face_recipes_v1'
MESH_IDENTITY_SCHEMA = 'hrig.face-recipes.mesh-identity.v1'
INSPECTION_SCHEMA = 'hrig.face-recipes.inspect.v1'
APPLY_SCHEMA = 'hrig.face-recipes.apply.v1'

MAX_REGION_VERTICES = 100000
MAX_RECIPES = 16
MAX_SOURCES = 8
SLIDER_LIMIT = 2.0
MAX_VERTEX_INDEX = 2147483647

RAW_MESH_DOMAIN = b'hrig.face-recipes.raw-mesh.v1\0'
KEY_COORDINATE_DOMAIN = b'hrig.face-recipes.key-coordinates.f32le.v1\0'
REGION_DOMAIN = b'hrig.face-recipes.region-vertices.v1\0'

_SHA256_RE = re.compile(r'[0-9a-f]{64}\Z')


class FaceRecipeError(ValueError):
    """Dedicated contract/native error with a stable code and JSON-able details."""

    def __init__(self, code, message, path=None, details=None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
        if path is not None:
            self.details.setdefault('path', path)


def _fail(code, message, path=None, **details):
    raise FaceRecipeError(code, message, path, details)


def _require(condition, code, message, path=None, **details):
    if not condition:
        _fail(code, message, path, **details)


def _object_fields(value, fields, path):
    if type(value) is not dict or set(value) != set(fields):
        _fail('PLAN_FIELDS', 'Object fields do not exactly match the contract',
              path, expected=sorted(fields),
              actual=([repr(key) for key in sorted(value, key=repr)]
                      if type(value) is dict else None))


def _readable_name(value, path, label):
    if type(value) is not str:
        _fail('INVALID_NAME', label + ' must be a string', path)
    if not value:
        _fail('EMPTY_NAME', label + ' must not be empty', path)
    if '\x00' in value:
        _fail('NAME_NUL', label + ' must not contain NUL', path)
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail('INVALID_TEXT', label + ' contains an unpaired UTF-16 surrogate', path)
    return value


def _finite_number(value, path, label):
    if type(value) not in (int, float):
        _fail('INVALID_NUMBER', label + ' must be a finite JSON number, not bool or string', path)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _fail('INVALID_NUMBER', label + ' cannot be represented as a finite number', path)
    if not math.isfinite(number):
        _fail('NONFINITE_NUMBER', label + ' must be finite', path)
    return number


def _f32_number(value, path, label):
    number = _finite_number(value, path, label)
    try:
        stored = struct.unpack('<f', struct.pack('<f', number))[0]
    except (OverflowError, struct.error):
        _fail('FLOAT32_RANGE', label + ' is not representable as float32', path)
    if not math.isfinite(stored):
        _fail('FLOAT32_RANGE', label + ' overflows float32', path)
    if number != 0.0 and stored == 0.0:
        _fail('FLOAT32_RANGE', label + ' underflows float32 to zero', path)
    return stored


def _integer(value, path, label, low, high):
    if type(value) is not int:
        _fail('INVALID_INTEGER', label + ' must be a JSON integer, not bool or float', path)
    if value < low or value > high:
        _fail('INVALID_INDEX', label + ' is outside ' + str((low, high)), path, value=value)
    return value


def _sha256(value, path):
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail('INVALID_SHA256', 'Expected lowercase SHA-256 hexadecimal', path)
    return value


def _region(value):
    if type(value) is not list:
        _fail('INVALID_REGION', 'region_vertices must be an explicit JSON array', 'plan.region_vertices')
    if len(value) > MAX_REGION_VERTICES:
        _fail('REGION_BUDGET', 'region_vertices exceeds 100000 entries',
              'plan.region_vertices', count=len(value), limit=MAX_REGION_VERTICES)
    result = []
    seen = set()
    for index, item in enumerate(value):
        path = 'plan.region_vertices[' + str(index) + ']'
        vertex = _integer(item, path, 'vertex index', 0, MAX_VERTEX_INDEX)
        if vertex in seen:
            _fail('DUPLICATE_VERTEX', 'region_vertices must be unique', path, vertex=vertex)
        seen.add(vertex)
        result.append(vertex)
    # Region semantics are set-like; canonical sorting makes equivalent plans
    # produce the same plan digest while preserving an explicit index list.
    return sorted(result)


def _sources(value, path):
    if type(value) is not list:
        _fail('INVALID_SOURCES', 'sources must be a JSON array', path)
    if len(value) > MAX_SOURCES:
        _fail('SOURCE_BUDGET', 'At most 8 sources are supported', path,
              count=len(value), limit=MAX_SOURCES)
    result = []
    seen = set()
    for index, item in enumerate(value):
        item_path = path + '[' + str(index) + ']'
        _object_fields(item, ('key', 'coefficient'), item_path)
        key = _readable_name(item['key'], item_path + '.key', 'source key name')
        if key in seen:
            _fail('DUPLICATE_SOURCE', 'Each source key may occur only once in a recipe',
                  item_path + '.key', key=key)
        seen.add(key)
        coefficient = _finite_number(
            item['coefficient'], item_path + '.coefficient', 'source coefficient')
        if coefficient == 0.0:
            coefficient = 0.0
        result.append({'key': key, 'coefficient': coefficient})
    return result


def _slider(value, path, label):
    raw = _finite_number(value, path, label)
    if not -SLIDER_LIMIT <= raw <= SLIDER_LIMIT:
        _fail('SLIDER_RANGE', label + ' must be within [-2, 2]', path, value=raw)
    number = _f32_number(value, path, label)
    if not -SLIDER_LIMIT <= number <= SLIDER_LIMIT:
        _fail('SLIDER_RANGE', label + ' float32 storage must remain within [-2, 2]',
              path, value=number)
    return 0.0 if number == 0.0 else number


def _recipes(value):
    if type(value) is not list:
        _fail('INVALID_RECIPES', 'recipes must be a JSON array', 'plan.recipes')
    if not 1 <= len(value) <= MAX_RECIPES:
        _fail('RECIPE_BUDGET', 'recipes must contain 1 through 16 entries',
              'plan.recipes', count=len(value))
    result = []
    names = set()
    for index, item in enumerate(value):
        path = 'plan.recipes[' + str(index) + ']'
        _object_fields(item, ('name', 'sources', 'slider_min', 'slider_max'), path)
        name = _readable_name(item['name'], path + '.name', 'recipe name')
        if name in names:
            _fail('DUPLICATE_RECIPE_NAME', 'Recipe names must be unique within a plan',
                  path + '.name', name=name)
        names.add(name)
        slider_min = _slider(item['slider_min'], path + '.slider_min', 'slider_min')
        slider_max = _slider(item['slider_max'], path + '.slider_max', 'slider_max')
        if slider_min > slider_max:
            _fail('SLIDER_ORDER', 'slider_min must not exceed slider_max',
                  path, slider_min=slider_min, slider_max=slider_max)
        if not slider_min <= 0.0 <= slider_max:
            _fail('SLIDER_ZERO', 'Each slider range must include zero',
                  path, slider_min=slider_min, slider_max=slider_max)
        result.append({
            'name': name,
            'sources': _sources(item['sources'], path + '.sources'),
            'slider_min': slider_min,
            'slider_max': slider_max,
        })
    return result


def validate_plan(value):
    """Validate and canonicalize an already-decoded plan.

    Exact top-level and nested fields are required.  The returned object is a
    fresh, JSON-safe canonical copy.  This function never performs I/O.
    """

    _object_fields(value, (
        'schema', 'instance_id', 'mesh', 'expected_raw_mesh_sha256',
        'region_vertices', 'recipes'), 'plan')
    if value['schema'] != SCHEMA:
        _fail('PLAN_SCHEMA', 'Unsupported face recipe schema', 'plan.schema',
              expected=SCHEMA)
    instance_id = _readable_name(value['instance_id'], 'plan.instance_id', 'instance_id')
    mesh = _readable_name(value['mesh'], 'plan.mesh', 'mesh Object name')
    expected_sha = _sha256(value['expected_raw_mesh_sha256'],
                           'plan.expected_raw_mesh_sha256')
    region = _region(value['region_vertices'])
    recipes = _recipes(value['recipes'])
    return {
        'schema': SCHEMA,
        'instance_id': instance_id,
        'mesh': mesh,
        'expected_raw_mesh_sha256': expected_sha,
        'region_vertices': region,
        'recipes': recipes,
    }


def _reject_json_constant(token):
    raise FaceRecipeError(
        'NONFINITE_NUMBER',
        'JSON non-finite token is forbidden: ' + token,
        details={'token': token})


def _json_float(token):
    value = float(token)
    if not math.isfinite(value):
        raise FaceRecipeError(
            'NONFINITE_NUMBER',
            'JSON number is not finite: ' + token,
            details={'token': token})
    return value


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FaceRecipeError(
                'DUPLICATE_JSON_KEY',
                'Duplicate JSON object key is forbidden: ' + key,
                details={'key': key})
        result[key] = value
    return result


def strict_json_loads(payload):
    """Decode UTF-8 JSON with duplicate-key and non-finite rejection."""

    if type(payload) is bytes:
        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError as error:
            raise FaceRecipeError(
                'INVALID_JSON_ENCODING',
                'JSON payload must be valid UTF-8',
                details={'error': str(error)}) from error
    elif type(payload) is str:
        text = payload
    else:
        _fail('INVALID_JSON', 'JSON payload must be str or bytes')
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_json_constant,
            parse_float=_json_float)
    except FaceRecipeError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError, ValueError) as error:
        raise FaceRecipeError(
            'INVALID_JSON',
            'Malformed JSON payload',
            details={'error': str(error)}) from error


def parse_plan(payload):
    """Parse UTF-8 JSON bytes/text, then validate and canonicalize the plan."""

    return validate_plan(strict_json_loads(payload))


def canonical_json(value):
    """Return the canonical JSON text used for plan and ledger hashes."""

    try:
        text = json.dumps(
            value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False)
        text.encode('utf-8')
    except (TypeError, ValueError, UnicodeError, OverflowError) as error:
        raise FaceRecipeError(
            'INVALID_JSON_VALUE',
            'Value is not finite UTF-8 JSON',
            details={'error': str(error)}) from error
    return text


def plan_sha256(plan):
    """Hash the canonical, validated plan using SHA-256."""

    canonical = validate_plan(plan)
    return hashlib.sha256(canonical_json(canonical).encode('utf-8')).hexdigest()


def region_vertices_sha256(region_vertices):
    """Hash a canonical unique region-index list (little-endian uint32s)."""

    region = _region(region_vertices)
    digest = hashlib.sha256(REGION_DOMAIN)
    digest.update(struct.pack('<Q', len(region)))
    for index in region:
        digest.update(struct.pack('<I', index))
    return digest.hexdigest()


def coordinate_sha256(values):
    """Hash finite stored key coordinates as little-endian float32 values.

    ``values`` is a flat x,y,z sequence.  It must contain a multiple of three
    values.  The digest covers a u64 count followed by the float32 payload.
    """

    if (isinstance(values, (str, bytes, bytearray, dict))
            or not hasattr(values, '__len__')
            or not hasattr(values, '__getitem__')):
        _fail('INVALID_COORDINATES', 'Coordinates must be a flat numeric sequence',
              'coordinates')
    count = len(values)
    if count % 3:
        _fail('INVALID_COORDINATES', 'Coordinate count must be a multiple of three',
              'coordinates', count=count)
    normalized = []
    for index in range(count):
        normalized.append(_f32_number(
            values[index], 'coordinates[' + str(index) + ']', 'coordinate'))
    digest = hashlib.sha256(KEY_COORDINATE_DOMAIN)
    digest.update(struct.pack('<Q', count))
    chunk_size = 4096
    for start in range(0, count, chunk_size):
        chunk = normalized[start:start + chunk_size]
        digest.update(struct.pack('<' + str(len(chunk)) + 'f', *chunk))
    return digest.hexdigest()


def _coordinate_list(values, expected_count, path):
    if (isinstance(values, (str, bytes, bytearray, dict))
            or not hasattr(values, '__len__')
            or not hasattr(values, '__getitem__')):
        _fail('INVALID_COORDINATES', 'Coordinates must be a flat numeric sequence', path)
    if len(values) != expected_count:
        _fail('COORDINATE_COUNT', 'Coordinate count does not match vertex_count',
              path, expected=expected_count, actual=len(values))
    result = []
    for index in range(expected_count):
        result.append(_f32_number(
            values[index], path + '[' + str(index) + ']', 'coordinate'))
    return result


def evaluate_recipe(plan, recipe_name, reference_coordinates,
                    source_coordinates, vertex_count=None):
    """Evaluate one named recipe; convenience wrapper around evaluate_recipes."""

    canonical = validate_plan(plan)
    names = [recipe['name'] for recipe in canonical['recipes']]
    if type(recipe_name) is not str or recipe_name not in names:
        _fail('UNKNOWN_RECIPE', 'Recipe name does not exist in the plan',
              'recipe_name', name=recipe_name)
    results = evaluate_recipes(
        canonical, reference_coordinates, source_coordinates, vertex_count)
    return results[recipe_name]


def evaluate_recipes(plan, reference_coordinates, source_coordinates,
                     vertex_count=None):
    """Compute absolute new-key coordinates for all recipes.

    Formula per component inside the explicit region:

        float32(reference + sum_i(coefficient_i * (source_i - reference)))

    Reference and source values are read as float32.  Coefficients are finite
    binary64 values from the plan.  The accumulation is performed in binary64
    in declared source order, then rounded once to float32.  Components outside
    the region are copied from the float32 reference exactly.
    """

    canonical = validate_plan(plan)
    region = canonical['region_vertices']
    if vertex_count is None:
        if (isinstance(reference_coordinates, (str, bytes, bytearray, dict))
                or not hasattr(reference_coordinates, '__len__')
                or not hasattr(reference_coordinates, '__getitem__')):
            _fail('INVALID_COORDINATES', 'reference_coordinates must be a sequence',
                  'reference_coordinates')
        if len(reference_coordinates) % 3:
            _fail('INVALID_COORDINATES',
                  'reference_coordinates length must be a multiple of three',
                  'reference_coordinates')
        vertex_count = len(reference_coordinates) // 3
    else:
        vertex_count = _integer(
            vertex_count, 'vertex_count', 'vertex_count', 0, MAX_VERTEX_INDEX + 1)
    coordinate_count = vertex_count * 3
    reference = _coordinate_list(
        reference_coordinates, coordinate_count, 'reference_coordinates')
    if region and region[-1] >= vertex_count:
        _fail('INVALID_INDEX', 'region_vertices contains an index outside the mesh',
              'plan.region_vertices', vertex=region[-1], vertex_count=vertex_count)

    if type(source_coordinates) is not dict:
        _fail('INVALID_SOURCES', 'source_coordinates must map key names to flat arrays',
              'source_coordinates')
    needed = []
    seen = set()
    for recipe in canonical['recipes']:
        for source in recipe['sources']:
            if source['key'] not in seen:
                seen.add(source['key'])
                needed.append(source['key'])
    source_values = {}
    for key in needed:
        if key not in source_coordinates:
            _fail('MISSING_SOURCE_COORDINATES',
                  'No native coordinate array supplied for source key',
                  'source_coordinates', key=key)
        source_values[key] = _coordinate_list(
            source_coordinates[key], coordinate_count,
            'source_coordinates[' + repr(key) + ']')

    results = {}
    for recipe in canonical['recipes']:
        target = list(reference)
        sources = [(source_values[source['key']], source['coefficient'])
                   for source in recipe['sources']]
        for vertex in region:
            offset = vertex * 3
            base = reference[offset:offset + 3]
            accumulated = [float(base[0]), float(base[1]), float(base[2])]
            for source_coordinates_flat, coefficient in sources:
                source = source_coordinates_flat[offset:offset + 3]
                accumulated[0] += coefficient * (source[0] - base[0])
                accumulated[1] += coefficient * (source[1] - base[1])
                accumulated[2] += coefficient * (source[2] - base[2])
            for axis in range(3):
                path = ('recipes[' + repr(recipe['name']) + '].co[' +
                        str(offset + axis) + ']')
                try:
                    stored = struct.unpack('<f', struct.pack('<f', accumulated[axis]))[0]
                except (OverflowError, struct.error):
                    _fail('NONFINITE_RESULT',
                          'Recipe coordinate is not representable as float32', path)
                if not math.isfinite(stored):
                    _fail('NONFINITE_RESULT', 'Recipe coordinate is not finite', path)
                if accumulated[axis] != 0.0 and stored == 0.0:
                    _fail('FLOAT32_RANGE',
                          'Recipe coordinate underflows float32 to zero', path)
                target[offset + axis] = stored
        results[recipe['name']] = target
    return results


def expected_key_hashes(plan, reference_coordinates, source_coordinates,
                        vertex_count=None):
    """Convenience digest map for all computed recipe keys."""

    values = evaluate_recipes(
        plan, reference_coordinates, source_coordinates, vertex_count)
    return {name: coordinate_sha256(coords) for name, coords in values.items()}


def ledger_document():
    """Return an empty ledger document for a new Key ID custom property."""

    return {'schema': LEDGER_SCHEMA, 'instances': {}}
