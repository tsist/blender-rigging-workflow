"""Native HRIG facial-key recipe core.

The module has no import-time bpy dependency and performs no mutation at import
time.  ``mesh_identity``, ``inspect``/``inspect_plan`` and ``apply`` load bpy
inside their own call frames.  Applying only appends new relative shape-key
blocks and commits a JSON ledger on the existing Key ID.  It never calls a
Blender operator, changes the active mode/frame, or saves the file.

All native claims in this file still require the real-Blender fixtures listed in
DESIGN.md.  Outside-Blender unit tests cover only the pure recipe contract.
"""

import copy
import hashlib
import math
import struct
import sys
from array import array

import face_recipe_contract as contract


class NativeFaceRecipeError(contract.FaceRecipeError):
    """Dedicated native/core error; original and rollback errors are separate."""


def _fail(code, message, path=None, **details):
    raise NativeFaceRecipeError(code, message, path, details)


def _require(condition, code, message, path=None, **details):
    if not condition:
        _fail(code, message, path, **details)


def _error_payload(error):
    return {
        'code': getattr(error, 'code', 'NATIVE_ERROR'),
        'type': type(error).__name__,
        'message': str(error),
        'details': getattr(error, 'details', {}),
    }


def _load_bpy():
    try:
        import bpy
    except Exception as error:
        raise NativeFaceRecipeError(
            'BPY_UNAVAILABLE',
            'Native facial-key operations require Blender bpy',
            details={'error': str(error)}) from error
    return bpy


def _same(first, second):
    if first is None or second is None:
        return first is second
    try:
        return bool(first == second)
    except Exception:
        return False


def _local_editable(value, path, label):
    if value is None:
        _fail('READ_ONLY_DATA', label + ' is absent', path)
    if getattr(value, 'library', None) is not None:
        _fail('READ_ONLY_DATA', label + ' is library-linked', path)
    if getattr(value, 'override_library', None) is not None:
        _fail('READ_ONLY_DATA', label + ' is a library override', path)
    if not bool(getattr(value, 'is_editable', True)):
        _fail('READ_ONLY_DATA', label + ' is not editable', path)


def _coerce_plan(plan):
    if type(plan) is bytes or type(plan) is str:
        return contract.parse_plan(plan)
    return contract.validate_plan(plan)


def _mesh_owner_context(bpy, mesh_obj):
    if not isinstance(mesh_obj, bpy.types.Object):
        _fail('INVALID_MESH_OBJECT', 'An explicit bpy Object is required', 'mesh_obj')
    if mesh_obj.type != 'MESH':
        _fail('INVALID_MESH_OBJECT', 'Object.type must be MESH', 'mesh_obj')
    _local_editable(mesh_obj, 'mesh_obj', 'Mesh Object')
    mesh = mesh_obj.data
    if not isinstance(mesh, bpy.types.Mesh):
        _fail('INVALID_MESH', 'Object.data must be a bpy Mesh', 'mesh_obj.data')
    _local_editable(mesh, 'mesh_obj.data', 'Mesh')
    if getattr(mesh_obj, 'mode', 'OBJECT') != 'OBJECT':
        _fail('OBJECT_MODE_REQUIRED', 'Mesh Object must be in Object mode', 'mesh_obj')
    if bool(getattr(mesh, 'is_editmode', False)):
        _fail('OBJECT_MODE_REQUIRED', 'Mesh data must not be in edit mode', 'mesh_obj.data')

    owners = []
    for owner in bpy.data.objects:
        if owner.type == 'MESH' and _same(owner.data, mesh):
            owners.append(owner)
    owner_names = sorted(owner.name for owner in owners)
    if len(owners) != 1 or not _same(owners[0], mesh_obj):
        _fail('SHARED_MESH',
              'Exactly one real Mesh Object owner is required; a fake user is ignored',
              'mesh_obj', real_object_names=owner_names,
              real_object_count=len(owners))
    return {
        'bpy': bpy,
        'mesh_obj': mesh_obj,
        'mesh': mesh,
        'vertex_count': len(mesh.vertices),
        'owner_names': owner_names,
        'real_object_count': len(owners),
        'fake_user': bool(getattr(mesh, 'use_fake_user', False)),
        'native_users': int(getattr(mesh, 'users', 0)),
    }


def _shape_context(owner_context):
    mesh_obj = owner_context['mesh_obj']
    mesh = owner_context['mesh']
    shape_keys = getattr(mesh, 'shape_keys', None)
    if shape_keys is None:
        _fail('SHAPE_KEYS_REQUIRED', 'Existing relative shape keys are required',
              'mesh_obj.data.shape_keys')
    if not isinstance(shape_keys, owner_context['bpy'].types.Key):
        _fail('INVALID_SHAPE_KEYS', 'Mesh shape_keys is not a bpy Key',
              'mesh_obj.data.shape_keys')
    _local_editable(shape_keys, 'mesh_obj.data.shape_keys', 'Shape Keys Key ID')
    if not bool(getattr(shape_keys, 'use_relative', False)):
        _fail('ABSOLUTE_SHAPE_KEYS_UNSUPPORTED',
              'Only relative shape keys are supported', 'shape_keys.use_relative')

    blocks = list(shape_keys.key_blocks)
    if not blocks:
        _fail('REFERENCE_KEY_REQUIRED', 'At least one reference KeyBlock is required',
              'shape_keys.key_blocks')
    names = [block.name for block in blocks]
    if len(names) != len(set(names)):
        _fail('DUPLICATE_KEY_NAME', 'Native KeyBlock names must be unique',
              'shape_keys.key_blocks')

    # ``Key.reference_key`` is the Blender 5.2 path used here.  The first block
    # fallback preserves compatibility if the property is unavailable; the
    # real-native fixture must confirm which path was taken.
    reference_source = 'reference_key'
    try:
        reference = shape_keys.reference_key
    except AttributeError:
        reference = None
        reference_source = 'key_blocks[0]_fallback'
    if reference is None:
        reference = blocks[0]
        reference_source = 'key_blocks[0]_fallback'

    reference_index = None
    for index, block in enumerate(blocks):
        if _same(block, reference):
            reference_index = index
            break
    if reference_index is None:
        _fail('REFERENCE_KEY_UNAVAILABLE',
              'shape_keys.reference_key is not present in key_blocks',
              'shape_keys.reference_key')
    reference_vertex_group = getattr(reference, 'vertex_group', '') or ''
    if reference_vertex_group:
        _fail('REFERENCE_MASK_UNSUPPORTED',
              'A masked reference key is outside the supported domain',
              'reference.vertex_group', vertex_group=reference_vertex_group)
    if len(reference.data) != owner_context['vertex_count']:
        _fail('KEY_VERTEX_COUNT',
              'Reference KeyBlock vertex count differs from the Mesh',
              'reference', expected=owner_context['vertex_count'],
              actual=len(reference.data))

    return {
        'shape_keys': shape_keys,
        'blocks': blocks,
        'block_names': names,
        'reference': reference,
        'reference_index': reference_index,
        'reference_source': reference_source,
        'active_shape_key_index': int(mesh_obj.active_shape_key_index),
    }


def _mesh_identity_data(context):
    mesh = context['mesh']
    vertices = mesh.vertices
    edges = mesh.edges
    polygons = mesh.polygons
    loops = mesh.loops
    digest = hashlib.sha256(contract.RAW_MESH_DOMAIN)
    digest.update(struct.pack(
        '<4Q', len(vertices), len(edges), len(polygons), len(loops)))

    for index, vertex in enumerate(vertices):
        if vertex.index != index:
            _fail('MESH_INDEX', 'Vertex indices must be contiguous and collection ordered',
                  'mesh.vertices', index=index, native_index=vertex.index)
        coordinate = vertex.co
        values = (float(coordinate[0]), float(coordinate[1]), float(coordinate[2]))
        if not all(math.isfinite(value) for value in values):
            _fail('NONFINITE_GEOMETRY', 'Raw Mesh coordinate is not finite',
                  'mesh.vertices[' + str(index) + '].co')
        digest.update(struct.pack('<Ifff', index, values[0], values[1], values[2]))

    for index, edge in enumerate(edges):
        if edge.index != index:
            _fail('MESH_INDEX', 'Edge indices must be contiguous and collection ordered',
                  'mesh.edges', index=index, native_index=edge.index)
        edge_vertices = list(edge.vertices)
        if len(edge_vertices) != 2:
            _fail('MESH_TOPOLOGY', 'Every edge must have exactly two vertices',
                  'mesh.edges[' + str(index) + ']')
        digest.update(struct.pack(
            '<I2I', index, int(edge_vertices[0]), int(edge_vertices[1])))

    for index, polygon in enumerate(polygons):
        if polygon.index != index:
            _fail('MESH_INDEX', 'Polygon indices must be contiguous and collection ordered',
                  'mesh.polygons', index=index, native_index=polygon.index)
        polygon_vertices = [int(value) for value in polygon.vertices]
        digest.update(struct.pack(
            '<I2I', index, int(polygon.loop_start), int(polygon.loop_total)))
        digest.update(struct.pack('<I', len(polygon_vertices)))
        if polygon_vertices:
            digest.update(struct.pack(
                '<' + str(len(polygon_vertices)) + 'I', *polygon_vertices))

    for index, loop in enumerate(loops):
        if loop.index != index:
            _fail('MESH_INDEX', 'Loop indices must be contiguous and collection ordered',
                  'mesh.loops', index=index, native_index=loop.index)
        digest.update(struct.pack(
            '<I2I', index, int(loop.vertex_index), int(loop.edge_index)))

    return {
        'schema': contract.MESH_IDENTITY_SCHEMA,
        'protocol': 'hrig.face-recipes.raw-mesh.v1',
        'object': context['mesh_obj'].name,
        'mesh': mesh.name,
        'counts': {
            'vertices': len(vertices),
            'edges': len(edges),
            'polygons': len(polygons),
            'loops': len(loops),
        },
        'raw_mesh_sha256': digest.hexdigest(),
        'real_object_names': list(context['owner_names']),
        'real_object_count': context['real_object_count'],
        'fake_user': context['fake_user'],
        'native_users': context['native_users'],
    }


def _key_coordinates(block, vertex_count, label):
    data = getattr(block, 'data', None)
    if data is None or len(data) != vertex_count:
        _fail('KEY_VERTEX_COUNT', 'KeyBlock vertex count differs from the Mesh',
              label, expected=vertex_count,
              actual=None if data is None else len(data))
    values = array('f', [0.0]) * (3 * vertex_count)
    try:
        data.foreach_get('co', values)
    except Exception as error:
        _fail('NATIVE_READBACK', 'Could not read KeyBlock coordinates', label,
              error=str(error))
    result = [float(value) for value in values]
    for index, value in enumerate(result):
        if not math.isfinite(value):
            _fail('NONFINITE_EXISTING_KEY', 'Existing KeyBlock coordinate is not finite',
                  label, coordinate_index=index)
    return result


def _float32_hex(value, label):
    try:
        return struct.pack('<f', float(value)).hex()
    except (OverflowError, ValueError, TypeError, struct.error):
        _fail('NATIVE_VALUE', 'Native value cannot be represented as float32', label)


def _key_definition(block, index, vertex_count):
    coordinates = _key_coordinates(
        block, vertex_count, 'key_blocks[' + str(index) + ']')
    relative = getattr(block, 'relative_key', None)
    vertex_group = getattr(block, 'vertex_group', '')
    if vertex_group is None:
        vertex_group = ''
    if type(vertex_group) is not str:
        _fail('NATIVE_VALUE', 'KeyBlock.vertex_group must be a string',
              'key_blocks[' + str(index) + '].vertex_group')
    return {
        'index': index,
        'name': block.name,
        'coordinate_sha256': contract.coordinate_sha256(coordinates),
        'vertex_count': vertex_count,
        'relative_key': None if relative is None else relative.name,
        'relative_key_is_self': _same(relative, block),
        'vertex_group': vertex_group,
        'value_f32_hex': _float32_hex(
            block.value, 'key_blocks[' + str(index) + '].value'),
        'slider_min_f32_hex': _float32_hex(
            block.slider_min, 'key_blocks[' + str(index) + '].slider_min'),
        'slider_max_f32_hex': _float32_hex(
            block.slider_max, 'key_blocks[' + str(index) + '].slider_max'),
        'mute': bool(block.mute),
        'interpolation': getattr(block, 'interpolation', None),
    }


def _key_definitions(blocks, vertex_count):
    return [_key_definition(block, index, vertex_count)
            for index, block in enumerate(blocks)]


def _keys_sha256(definitions, use_relative, reference_name):
    payload = {
        'use_relative': bool(use_relative),
        'reference_key': reference_name,
        'keys': definitions,
    }
    return hashlib.sha256(
        contract.canonical_json(payload).encode('utf-8')).hexdigest()


def _source_context(shape_context, plan, reference_coordinates, vertex_count):
    reference = shape_context['reference']
    by_name = {block.name: block for block in shape_context['blocks']}
    needed = []
    seen = set()
    for recipe in plan['recipes']:
        for source in recipe['sources']:
            if source['key'] not in seen:
                seen.add(source['key'])
                needed.append(source['key'])

    source_coordinates = {}
    source_records = []
    for key in sorted(needed):
        block = by_name.get(key)
        if block is None:
            _fail('SOURCE_KEY_MISSING', 'Recipe source KeyBlock does not exist',
                  'plan.recipes.sources', key=key)
        vertex_group = getattr(block, 'vertex_group', '') or ''
        if vertex_group:
            _fail('SOURCE_MASK_UNSUPPORTED',
                  'Source KeyBlock vertex masks are outside the supported domain',
                  'source.vertex_group', key=key, vertex_group=vertex_group)
        if not _same(getattr(block, 'relative_key', None), reference):
            _fail('SOURCE_RELATIVE_MISMATCH',
                  'Every source KeyBlock.relative_key must be the reference key',
                  'source.relative_key', key=key,
                  reference=reference.name)
        coordinates = _key_coordinates(
            block, vertex_count,
            'source_key[' + repr(key) + ']')
        source_coordinates[key] = coordinates
        source_records.append({
            'name': key,
            'coordinate_sha256': contract.coordinate_sha256(coordinates),
        })

    return {
        'coordinates': source_coordinates,
        'fingerprint': {
            'reference_key': {
                'name': reference.name,
                'coordinate_sha256': contract.coordinate_sha256(reference_coordinates),
            },
            'source_keys': source_records,
        },
    }


def _load_ledger(shape_keys):
    raw = shape_keys.get(contract.LEDGER_PROPERTY)
    if raw is None:
        return None, None, None
    if type(raw) is not str:
        _fail('LEDGER_TYPE', 'Key ID ledger must be a JSON string',
              contract.LEDGER_PROPERTY, native_type=type(raw).__name__)
    try:
        ledger = contract.strict_json_loads(raw)
    except contract.FaceRecipeError as error:
        raise NativeFaceRecipeError(
            'LEDGER_INVALID',
            'Existing Key ID ledger is invalid JSON or has duplicate keys',
            contract.LEDGER_PROPERTY,
            {'cause': _error_payload(error)}) from error
    if type(ledger) is not dict:
        _fail('LEDGER_INVALID', 'Existing Key ID ledger must be a JSON object',
              contract.LEDGER_PROPERTY)
    if ledger.get('schema') != contract.LEDGER_SCHEMA:
        _fail('LEDGER_SCHEMA',
              'Unknown Key ID ledger schema is protected and not overwritten',
              contract.LEDGER_PROPERTY, schema=ledger.get('schema'),
              expected=contract.LEDGER_SCHEMA)
    instances = ledger.get('instances')
    if type(instances) is not dict:
        _fail('LEDGER_INVALID', 'Ledger.instances must be a JSON object',
              contract.LEDGER_PROPERTY + '.instances')
    return raw, ledger, instances


def _ledger_sha256(raw):
    if raw is None:
        return None
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _fingerprint(context, shape_context, definitions, ledger_raw):
    keys_sha = _keys_sha256(
        definitions, shape_context['shape_keys'].use_relative,
        shape_context['reference'].name)
    return {
        'raw_mesh_sha256': context['identity']['raw_mesh_sha256'],
        'mesh_counts': dict(context['identity']['counts']),
        'object': context['mesh_obj'].name,
        'mesh': context['mesh'].name,
        'shape_keys_name': shape_context['shape_keys'].name,
        'use_relative': bool(shape_context['shape_keys'].use_relative),
        'reference_key': shape_context['reference'].name,
        'reference_key_index': shape_context['reference_index'],
        'reference_key_source': shape_context['reference_source'],
        'active_shape_key_index': shape_context['active_shape_key_index'],
        'key_count': len(definitions),
        'keys_sha256': keys_sha,
        'keys': definitions,
        'ledger_property': contract.LEDGER_PROPERTY,
        'ledger_present': ledger_raw is not None,
        'ledger_sha256': _ledger_sha256(ledger_raw),
    }


def _template_intent(plan, context, source_fingerprint):
    return {
        'contract_schema': contract.SCHEMA,
        'mesh': plan['mesh'],
        'mesh_data': context['mesh'].name,
        'expected_raw_mesh_sha256': plan['expected_raw_mesh_sha256'],
        'region_vertex_count': len(plan['region_vertices']),
        'region_vertices_sha256': contract.region_vertices_sha256(
            plan['region_vertices']),
        'reference_key': copy.deepcopy(source_fingerprint['reference_key']),
        'source_keys': copy.deepcopy(source_fingerprint['source_keys']),
        'recipes': copy.deepcopy(plan['recipes']),
    }


def _proposed_entry(plan, context, source_fingerprint, expected_hashes):
    return {
        'plan_sha256': contract.plan_sha256(plan),
        'template_intent': _template_intent(plan, context, source_fingerprint),
        'keys': [
            {'name': recipe['name'],
             'coordinate_sha256': expected_hashes[recipe['name']]}
            for recipe in plan['recipes']
        ],
    }


def _analyze_instance(plan, instances, plan_sha, source_fingerprint,
                      expected_hashes, existing_names, current_hashes):
    conflicts = []
    instance_id = plan['instance_id']
    if instance_id not in instances:
        other_ledger_names = set()
        for other_id, other_entry in instances.items():
            if other_id == instance_id or type(other_entry) is not dict:
                continue
            other_keys = other_entry.get('keys')
            if type(other_keys) is not list:
                continue
            for item in other_keys:
                if type(item) is dict and type(item.get('name')) is str:
                    other_ledger_names.add(item['name'])
        for recipe in plan['recipes']:
            if recipe['name'] in existing_names:
                conflicts.append({
                    'code': 'KEY_NAME_CONFLICT',
                    'name': recipe['name'],
                    'message': 'A KeyBlock with this recipe name already exists',
                })
            elif recipe['name'] in other_ledger_names:
                conflicts.append({
                    'code': 'LEDGER_KEY_NAME_CONFLICT',
                    'name': recipe['name'],
                    'message': 'Another ledger instance already claims this key name',
                })
        return ('conflict' if conflicts else 'ready'), conflicts

    entry = instances[instance_id]
    if type(entry) is not dict:
        return 'conflict', [{
            'code': 'LEDGER_INSTANCE_INVALID',
            'message': 'Ledger entry for this instance is not an object',
        }]
    stored_plan_sha = entry.get('plan_sha256')
    if type(stored_plan_sha) is not str or stored_plan_sha != plan_sha:
        return 'conflict', [{
            'code': 'INSTANCE_PLAN_MISMATCH',
            'message': 'This instance_id already has a different plan',
            'stored_plan_sha256': stored_plan_sha,
            'plan_sha256': plan_sha,
        }]

    template = entry.get('template_intent')
    if type(template) is not dict:
        return 'conflict', [{
            'code': 'LEDGER_TEMPLATE_INVALID',
            'message': 'Ledger template_intent is missing or malformed',
        }]
    template_checks = {
        'contract_schema': template.get('contract_schema') == contract.SCHEMA,
        'mesh': template.get('mesh') == plan['mesh'],
        'expected_raw_mesh_sha256': (
            template.get('expected_raw_mesh_sha256')
            == plan['expected_raw_mesh_sha256']),
        'region_vertex_count': (
            template.get('region_vertex_count') == len(plan['region_vertices'])),
        'region_vertices_sha256': (
            template.get('region_vertices_sha256')
            == contract.region_vertices_sha256(plan['region_vertices'])),
        'recipes': template.get('recipes') == plan['recipes'],
    }
    template_drift = [name for name, matched in template_checks.items() if not matched]
    if template_drift:
        conflicts.append({
            'code': 'LEDGER_TEMPLATE_DRIFT',
            'message': 'Ledger template intent differs from the plan',
            'fields': template_drift,
        })
    stored_reference = template.get('reference_key')
    stored_sources = template.get('source_keys')
    if stored_reference != source_fingerprint['reference_key']:
        conflicts.append({
            'code': 'REFERENCE_DRIFT',
            'message': 'Reference key coordinates differ from ledger intent',
        })
    if stored_sources != source_fingerprint['source_keys']:
        conflicts.append({
            'code': 'SOURCE_DRIFT',
            'message': 'Source key coordinates differ from ledger intent',
        })

    ledger_keys = entry.get('keys')
    recipes = plan['recipes']
    if type(ledger_keys) is not list or len(ledger_keys) != len(recipes):
        return 'conflict', conflicts + [{
            'code': 'LEDGER_KEYS_INVALID',
            'message': 'Ledger keys must be a complete recipe-ordered list',
        }]
    for index, recipe in enumerate(recipes):
        name = recipe['name']
        item = ledger_keys[index]
        if (type(item) is not dict or item.get('name') != name
                or type(item.get('coordinate_sha256')) is not str):
            conflicts.append({
                'code': 'LEDGER_KEYS_INVALID',
                'name': name,
                'message': 'Ledger key entry is malformed or out of order',
            })
            continue
        ledger_hash = item['coordinate_sha256']
        expected_hash = expected_hashes[name]
        current_hash = current_hashes.get(name)
        if current_hash is None:
            conflicts.append({
                'code': 'LEDGER_KEY_MISSING',
                'name': name,
                'message': 'Ledger records a key that is no longer present',
            })
        elif ledger_hash != expected_hash:
            conflicts.append({
                'code': 'LEDGER_HASH_DRIFT',
                'name': name,
                'message': 'Ledger coordinate hash differs from the current recipe result',
                'ledger_sha256': ledger_hash,
                'expected_sha256': expected_hash,
            })
        elif current_hash != expected_hash:
            conflicts.append({
                'code': 'KEY_COORDINATE_DRIFT',
                'name': name,
                'message': 'Existing KeyBlock coordinates were changed outside the recipe',
                'current_sha256': current_hash,
                'expected_sha256': expected_hash,
            })
        elif ledger_hash != current_hash:
            conflicts.append({
                'code': 'LEDGER_HASH_DRIFT',
                'name': name,
                'message': 'Ledger and existing KeyBlock coordinate hashes differ',
            })
    return ('conflict' if conflicts else 'idempotent'), conflicts


def _prepare(mesh_obj, plan):
    plan = _coerce_plan(plan)
    bpy = _load_bpy()
    owner_context = _mesh_owner_context(bpy, mesh_obj)
    if mesh_obj.name != plan['mesh']:
        _fail('MESH_NAME_MISMATCH', 'Plan mesh must be the exact real Object name',
              'plan.mesh', expected=plan['mesh'], actual=mesh_obj.name)
    identity = _mesh_identity_data(owner_context)
    owner_context['identity'] = identity
    if identity['raw_mesh_sha256'] != plan['expected_raw_mesh_sha256']:
        _fail('RAW_MESH_MISMATCH',
              'Raw Mesh coordinates/topology do not match the plan',
              'plan.expected_raw_mesh_sha256',
              expected=plan['expected_raw_mesh_sha256'],
              actual=identity['raw_mesh_sha256'])
    shape_context = _shape_context(owner_context)
    reference_coordinates = _key_coordinates(
        shape_context['reference'], owner_context['vertex_count'], 'reference')
    source_context = _source_context(
        shape_context, plan, reference_coordinates,
        owner_context['vertex_count'])
    expected_coordinates = contract.evaluate_recipes(
        plan, reference_coordinates, source_context['coordinates'],
        owner_context['vertex_count'])
    expected_hashes = {
        recipe['name']: contract.coordinate_sha256(
            expected_coordinates[recipe['name']])
        for recipe in plan['recipes']
    }

    definitions = _key_definitions(
        shape_context['blocks'], owner_context['vertex_count'])
    ledger_raw, ledger, instances = _load_ledger(shape_context['shape_keys'])
    before_fingerprint = _fingerprint(
        owner_context, shape_context, definitions, ledger_raw)
    plan_sha = contract.plan_sha256(plan)
    current_hashes = {
        definition['name']: definition['coordinate_sha256']
        for definition in definitions
    }
    status, conflicts = _analyze_instance(
        plan, {} if instances is None else instances, plan_sha,
        source_context['fingerprint'], expected_hashes,
        set(shape_context['block_names']), current_hashes)
    proposed = _proposed_entry(
        plan, owner_context, source_context['fingerprint'], expected_hashes)
    return {
        'bpy': bpy,
        'plan': plan,
        'plan_sha256': plan_sha,
        'owner_context': owner_context,
        'shape_context': shape_context,
        'identity': identity,
        'reference_coordinates': reference_coordinates,
        'source_context': source_context,
        'expected_coordinates': expected_coordinates,
        'expected_hashes': expected_hashes,
        'before_definitions': definitions,
        'before_blocks': list(shape_context['blocks']),
        'before_fingerprint': before_fingerprint,
        'ledger_raw': ledger_raw,
        'ledger': ledger,
        'instances': instances,
        'status': status,
        'conflicts': conflicts,
        'proposed_entry': proposed,
    }


def _inspection_result(state):
    recipe_rows = []
    for recipe in state['plan']['recipes']:
        recipe_rows.append({
            'name': recipe['name'],
            'coordinate_sha256': state['expected_hashes'][recipe['name']],
            'slider_min': recipe['slider_min'],
            'slider_max': recipe['slider_max'],
            'sources': copy.deepcopy(recipe['sources']),
        })
    return {
        'status': state['status'],
        'can_apply': state['status'] in ('ready', 'idempotent'),
        'instance_id': state['plan']['instance_id'],
        'mesh': state['plan']['mesh'],
        'plan_sha256': state['plan_sha256'],
        'mesh_identity': copy.deepcopy(state['identity']),
        'source_fingerprint': copy.deepcopy(
            state['source_context']['fingerprint']),
        'expected_keys': recipe_rows,
        'proposed_ledger_entry': copy.deepcopy(state['proposed_entry']),
        'conflicts': copy.deepcopy(state['conflicts']),
        'before_fingerprint': copy.deepcopy(state['before_fingerprint']),
        'ledger_present': state['ledger_raw'] is not None,
        'ledger_property': contract.LEDGER_PROPERTY,
    }


def _result_envelope(operation, ok, result=None, error=None, created=None,
                     rollback=None):
    if operation in ('inspect', 'inspect_plan'):
        schema = contract.INSPECTION_SCHEMA
    elif operation == 'mesh_identity':
        schema = contract.MESH_IDENTITY_SCHEMA
    else:
        schema = contract.APPLY_SCHEMA
    return {
        'ok': ok,
        'operation': operation,
        'schema': schema,
        'status': (result.get('status')
                   if isinstance(result, dict) else None),
        'result': result,
        'error': error,
        'created': [] if created is None else created,
        'rollback': rollback or {
            'attempted': False, 'ok': True, 'errors': [],
        },
    }


def _not_attempted_rollback():
    return {
        'attempted': False,
        'ok': True,
        'created_removed': [],
        'created_remaining': [],
        'ledger_restored': None,
        'active_shape_key_index_restored': None,
        'errors': [],
        'original_state_verified': None,
    }


def mesh_identity(mesh_obj):
    """Return the native raw Mesh SHA and owner accounting; never mutate."""

    try:
        bpy = _load_bpy()
        context = _mesh_owner_context(bpy, mesh_obj)
        identity = _mesh_identity_data(context)
        return _result_envelope('mesh_identity', True, identity)
    except Exception as error:
        return _result_envelope(
            'mesh_identity', False, None, _error_payload(error),
            rollback=_not_attempted_rollback())


def _inspect_impl(operation, mesh_obj, plan):
    try:
        state = _prepare(mesh_obj, plan)
        return _result_envelope(
            operation, True, _inspection_result(state),
            rollback=_not_attempted_rollback())
    except Exception as error:
        return _result_envelope(
            operation, False, None, _error_payload(error),
            rollback=_not_attempted_rollback())


def inspect(mesh_obj, plan):
    """Read-only native plan/mesh/ledger inspection."""

    return _inspect_impl('inspect', mesh_obj, plan)


def inspect_plan(mesh_obj, plan):
    """Named read-only alternative to :func:`inspect` for CLI/GUI callers."""

    return _inspect_impl('inspect_plan', mesh_obj, plan)


def _contains_block(blocks, target):
    for block in blocks:
        if _same(block, target):
            return True
    return False


def _float32_bytes(values):
    raw = array('f', values)
    if sys.byteorder != 'little':
        raw.byteswap()
    return raw.tobytes()


def _coordinates_equal(first, second):
    return _float32_bytes(first) == _float32_bytes(second)


def _verify_written_keys(state, created_entries, definitions_after):
    owner = state['owner_context']
    shape = state['shape_context']
    plan = state['plan']
    mesh_obj = owner['mesh_obj']
    n = owner['vertex_count']
    original_count = len(state['before_definitions'])
    if len(definitions_after) != original_count + len(plan['recipes']):
        _fail('KEY_COUNT_READBACK', 'Unexpected KeyBlock count after creation',
              'key_blocks', expected=original_count + len(plan['recipes']),
              actual=len(definitions_after))
    if definitions_after[:original_count] != state['before_definitions']:
        _fail('ORIGINAL_KEY_CHANGED',
              'An original KeyBlock coordinate or definition changed',
              'key_blocks')

    created_records = []
    region = set(plan['region_vertices'])
    current_blocks = list(shape['shape_keys'].key_blocks)
    for position, recipe in enumerate(plan['recipes']):
        expected_index = original_count + position
        block = created_entries[position]['block']
        if not _contains_block(current_blocks, block):
            _fail('KEY_CREATE_READBACK', 'New KeyBlock is absent after creation',
                  'shape_keys.key_blocks', name=recipe['name'])
        if (expected_index >= len(current_blocks)
                or not _same(current_blocks[expected_index], block)):
            _fail('KEY_INDEX_READBACK',
                  'New KeyBlock is not at the expected appended index',
                  'shape_keys.key_blocks', name=recipe['name'],
                  expected_index=expected_index)
        definition = _key_definition(block, expected_index, n)
        if definition['name'] != recipe['name']:
            _fail('KEY_NAME_READBACK', 'Native KeyBlock name differs from the recipe',
                  'new_key.name', expected=recipe['name'], actual=definition['name'])
        if definition['relative_key'] != shape['reference'].name:
            _fail('KEY_RELATIVE_READBACK',
                  'New KeyBlock must be relative to the reference key',
                  'new_key.relative_key', expected=shape['reference'].name,
                  actual=definition['relative_key'])
        if definition['vertex_group'] != '':
            _fail('KEY_MASK_UNSUPPORTED', 'New KeyBlock must not have a vertex mask',
                  'new_key.vertex_group', actual=definition['vertex_group'])
        if definition['value_f32_hex'] != _float32_hex(0.0, 'new_key.value'):
            _fail('KEY_VALUE_READBACK', 'New KeyBlock value must be 0.0',
                  'new_key.value', actual=definition['value_f32_hex'])
        if definition['mute']:
            _fail('KEY_MUTE_READBACK', 'New KeyBlock must not be muted',
                  'new_key.mute')
        expected_min = _float32_hex(recipe['slider_min'], 'recipe.slider_min')
        expected_max = _float32_hex(recipe['slider_max'], 'recipe.slider_max')
        if (definition['slider_min_f32_hex'] != expected_min
                or definition['slider_max_f32_hex'] != expected_max):
            _fail('KEY_SLIDER_READBACK',
                  'New KeyBlock slider bounds differ from the recipe',
                  'new_key.slider', recipe=recipe['name'])

        expected_coordinates = state['expected_coordinates'][recipe['name']]
        actual_coordinates = _key_coordinates(
            block, n, 'new_key[' + repr(recipe['name']) + ']')
        if not _coordinates_equal(actual_coordinates, expected_coordinates):
            _fail('KEY_COORDINATE_READBACK',
                  'New KeyBlock float32 coordinates differ from the computed recipe',
                  'new_key.co', name=recipe['name'])
        reference_coordinates = state['reference_coordinates']
        for vertex in range(n):
            if vertex in region:
                continue
            offset = vertex * 3
            if not _coordinates_equal(
                    actual_coordinates[offset:offset + 3],
                    reference_coordinates[offset:offset + 3]):
                _fail('OUTSIDE_REGION_CHANGED',
                      'New KeyBlock coordinate outside the explicit region is not reference exact',
                      'new_key.co', name=recipe['name'], vertex=vertex)

        actual_hash = contract.coordinate_sha256(actual_coordinates)
        if actual_hash != state['expected_hashes'][recipe['name']]:
            _fail('KEY_HASH_READBACK',
                  'New KeyBlock coordinate hash differs from the expected hash',
                  'new_key.co', name=recipe['name'])
        created_records.append({
            'name': recipe['name'],
            'index': expected_index,
            'relative_key': shape['reference'].name,
            'value': 0.0,
            'coordinate_sha256': actual_hash,
        })

    identity_after = _mesh_identity_data({
        'bpy': state['bpy'],
        'mesh_obj': mesh_obj,
        'mesh': owner['mesh'],
        'vertex_count': n,
        'owner_names': owner['owner_names'],
        'real_object_count': owner['real_object_count'],
        'fake_user': owner['fake_user'],
        'native_users': owner['native_users'],
    })
    if identity_after['raw_mesh_sha256'] != state['identity']['raw_mesh_sha256']:
        _fail('RAW_MESH_CHANGED',
              'Raw Mesh coordinates/topology changed during key creation',
              'mesh')
    if int(mesh_obj.active_shape_key_index) != state[
            'shape_context']['active_shape_key_index']:
        _fail('ACTIVE_KEY_INDEX', 'active_shape_key_index was not restored',
              'mesh_obj.active_shape_key_index')
    return created_records, identity_after


def _commit_ledger(state, created_records):
    shape_keys = state['shape_context']['shape_keys']
    if state['instances'] is not None and state['plan']['instance_id'] in state['instances']:
        _fail('INSTANCE_PLAN_MISMATCH',
              'Refusing to overwrite an existing instance ledger entry',
              contract.LEDGER_PROPERTY, instance_id=state['plan']['instance_id'])
    if state['ledger'] is None:
        ledger = contract.ledger_document()
    else:
        ledger = copy.deepcopy(state['ledger'])
    instances = ledger.get('instances')
    if type(instances) is not dict:
        _fail('LEDGER_INVALID', 'Ledger.instances must be an object',
              contract.LEDGER_PROPERTY + '.instances')
    entry = copy.deepcopy(state['proposed_entry'])
    entry['keys'] = [
        {'name': row['name'], 'coordinate_sha256': row['coordinate_sha256']}
        for row in created_records
    ]
    instances[state['plan']['instance_id']] = entry
    text = contract.canonical_json(ledger)
    return text, entry


def _restore_active_index(mesh_obj, expected):
    mesh_obj.active_shape_key_index = int(expected)
    if int(mesh_obj.active_shape_key_index) != int(expected):
        _fail('ACTIVE_KEY_INDEX', 'Could not restore active_shape_key_index',
              'mesh_obj.active_shape_key_index', expected=expected,
              actual=int(mesh_obj.active_shape_key_index))


def _rollback(state, created_entries, ledger_write_attempted):
    errors = []
    removed = []
    remaining = []
    ledger_restored = None
    active_restored = None
    original_verified = None
    if state is None:
        return {
            'attempted': False,
            'ok': True,
            'created_removed': [],
            'created_remaining': [],
            'ledger_restored': None,
            'active_shape_key_index_restored': None,
            'errors': [],
            'original_state_verified': None,
        }

    shape_context = state.get('shape_context')
    mesh_obj = state['owner_context']['mesh_obj']
    shape_keys = None if shape_context is None else shape_context['shape_keys']
    if shape_keys is not None:
        current_blocks = list(shape_keys.key_blocks)
        for record in reversed(created_entries):
            block = record.get('block')
            if block is None:
                continue
            if any(_same(block, original)
                   for original in state['before_blocks']):
                errors.append({
                    'stage': 'remove_created_key',
                    'code': 'REFUSED_ORIGINAL_KEY_REMOVAL',
                    'message': 'Rollback refused to remove an original KeyBlock',
                    'name': record.get('name'),
                })
                continue
            if not _contains_block(current_blocks, block):
                continue
            name = record.get('name')
            try:
                mesh_obj.shape_key_remove(block)
                current_blocks = list(shape_keys.key_blocks)
                removed.append(name)
            except Exception as error:
                errors.append({
                    'stage': 'remove_created_key',
                    'key_name': name,
                    **_error_payload(error),
                })

    if ledger_write_attempted and shape_keys is not None:
        try:
            if state['ledger_raw'] is None:
                if shape_keys.get(contract.LEDGER_PROPERTY) is None:
                    ledger_restored = True
                else:
                    del shape_keys[contract.LEDGER_PROPERTY]
                    ledger_restored = (
                        shape_keys.get(contract.LEDGER_PROPERTY) is None)
            else:
                shape_keys[contract.LEDGER_PROPERTY] = state['ledger_raw']
                ledger_restored = (
                    shape_keys.get(contract.LEDGER_PROPERTY) == state['ledger_raw'])
            if not ledger_restored:
                errors.append({
                    'stage': 'restore_ledger',
                    'code': 'LEDGER_RESTORE_READBACK',
                    'message': 'Ledger readback differs after restore',
                })
        except Exception as error:
            ledger_restored = False
            errors.append({'stage': 'restore_ledger', **_error_payload(error)})

    try:
        _restore_active_index(
            mesh_obj, state['shape_context']['active_shape_key_index'])
        active_restored = True
    except Exception as error:
        active_restored = False
        errors.append({'stage': 'restore_active_index', **_error_payload(error)})

    try:
        if shape_keys is not None:
            current_blocks = list(shape_keys.key_blocks)
            remaining_blocks = [
                record for record in created_entries
                if record.get('block') is not None
                and _contains_block(current_blocks, record['block'])
            ]
            remaining = [record.get('name') for record in remaining_blocks]
            current_definitions = _key_definitions(
                current_blocks, state['owner_context']['vertex_count'])
            original_count = len(state['before_definitions'])
            original_verified = (
                len(current_definitions) == original_count
                and current_definitions == state['before_definitions'])
            if not original_verified:
                errors.append({
                    'stage': 'verify_original_keys',
                    'code': 'ROLLBACK_KEY_READBACK',
                    'message': 'Original KeyBlock state was not restored exactly',
                })
            current_identity = _mesh_identity_data(state['owner_context'])
            if current_identity['raw_mesh_sha256'] != state['identity']['raw_mesh_sha256']:
                original_verified = False
                errors.append({
                    'stage': 'verify_raw_mesh',
                    'code': 'ROLLBACK_MESH_READBACK',
                    'message': 'Raw Mesh identity differs after rollback',
                })
            if (int(mesh_obj.active_shape_key_index)
                    != state['shape_context']['active_shape_key_index']):
                original_verified = False
                errors.append({
                    'stage': 'verify_active_index',
                    'code': 'ROLLBACK_ACTIVE_INDEX',
                    'message': 'active_shape_key_index differs after rollback',
                })
            if state['ledger_raw'] is None:
                if shape_keys.get(contract.LEDGER_PROPERTY) is not None:
                    original_verified = False
                    errors.append({
                        'stage': 'verify_ledger',
                        'code': 'ROLLBACK_LEDGER_READBACK',
                        'message': 'Ledger should be absent after rollback',
                    })
            elif shape_keys.get(contract.LEDGER_PROPERTY) != state['ledger_raw']:
                original_verified = False
                errors.append({
                    'stage': 'verify_ledger',
                    'code': 'ROLLBACK_LEDGER_READBACK',
                    'message': 'Original ledger string was not restored exactly',
                })
    except Exception as error:
        original_verified = False
        errors.append({'stage': 'verify_rollback', **_error_payload(error)})

    return {
        'attempted': True,
        'ok': not errors and not remaining,
        'created_removed': removed,
        'created_remaining': remaining,
        'ledger_restored': ledger_restored,
        'active_shape_key_index_restored': active_restored,
        'errors': errors,
        'original_state_verified': original_verified,
    }


def _make_after_fingerprint(state, definitions, ledger_raw, created_records):
    fingerprint = _fingerprint(
        state['owner_context'], state['shape_context'], definitions, ledger_raw)
    fingerprint['original_key_count'] = len(state['before_definitions'])
    fingerprint['original_keys_sha256'] = _keys_sha256(
        definitions[:len(state['before_definitions'])],
        state['shape_context']['shape_keys'].use_relative,
        state['shape_context']['reference'].name)
    fingerprint['created_key_count'] = len(created_records)
    fingerprint['created_key_names'] = [
        record['name'] for record in created_records]
    return fingerprint


def apply(mesh_obj, plan):
    """Preflight, compute, append new relative keys, and commit the ledger.

    Any failure after native creation removes only blocks tracked as created by
    this call.  The original ledger (or its absence) is restored independently.
    """

    state = None
    created_entries = []
    public_created = []
    ledger_write_attempted = False
    try:
        state = _prepare(mesh_obj, plan)
        if state['status'] == 'conflict':
            raise NativeFaceRecipeError(
                'PLAN_CONFLICT',
                'Plan cannot be applied without overwriting or adopting existing data',
                details={'conflicts': state['conflicts']})

        if state['status'] == 'idempotent':
            definitions = state['before_definitions']
            after = _make_after_fingerprint(
                state, definitions, state['ledger_raw'], [])
            result = _inspection_result(state)
            result.update({
                'status': 'idempotent',
                'applied_now': False,
                'created': [],
                'after_fingerprint': after,
                'ledger_entry': copy.deepcopy(
                    state['instances'][state['plan']['instance_id']]),
            })
            return _result_envelope(
                'apply', True, result, created=[],
                rollback=_not_attempted_rollback())

        # All native writes below happen only after full validation, full
        # coordinate computation for every recipe, and ledger conflict checks.
        active_before = state['shape_context']['active_shape_key_index']
        shape = state['shape_context']
        for recipe in state['plan']['recipes']:
            block = mesh_obj.shape_key_add(name=recipe['name'], from_mix=False)
            if block is None:
                _fail('KEY_CREATE_FAILED', 'shape_key_add returned no KeyBlock',
                      'shape_key_add', name=recipe['name'])
            created_entries.append({'block': block, 'name': block.name})
            if any(_same(block, original) for original in state['before_blocks']):
                _fail('KEY_CREATE_FAILED',
                      'shape_key_add unexpectedly returned an original KeyBlock',
                      'shape_key_add', name=recipe['name'])
            block.relative_key = shape['reference']
            block.value = 0.0
            block.slider_min = recipe['slider_min']
            block.slider_max = recipe['slider_max']
            block.mute = False
            if getattr(block, 'vertex_group', '') not in ('', None):
                _fail('KEY_MASK_UNSUPPORTED',
                      'New KeyBlock unexpectedly has a vertex mask',
                      'new_key.vertex_group')
            block.data.foreach_set(
                'co', state['expected_coordinates'][recipe['name']])

        _restore_active_index(mesh_obj, active_before)
        blocks_after = list(shape['shape_keys'].key_blocks)
        definitions_after = _key_definitions(
            blocks_after, state['owner_context']['vertex_count'])
        public_created, identity_after = _verify_written_keys(
            state, created_entries, definitions_after)
        if identity_after['raw_mesh_sha256'] != state['identity']['raw_mesh_sha256']:
            _fail('RAW_MESH_CHANGED', 'Raw Mesh identity changed', 'mesh')
        if state['before_fingerprint']['keys_sha256'] != _keys_sha256(
                definitions_after[:len(state['before_definitions'])],
                shape['shape_keys'].use_relative, shape['reference'].name):
            _fail('ORIGINAL_KEY_CHANGED', 'Original key fingerprint changed',
                  'shape_keys.key_blocks')

        ledger_text, ledger_entry = _commit_ledger(state, public_created)
        ledger_write_attempted = True
        shape['shape_keys'][contract.LEDGER_PROPERTY] = ledger_text
        if shape['shape_keys'].get(contract.LEDGER_PROPERTY) != ledger_text:
            _fail('LEDGER_READBACK', 'Key ID ledger readback differs after commit',
                  contract.LEDGER_PROPERTY)

        definitions_final = _key_definitions(
            list(shape['shape_keys'].key_blocks),
            state['owner_context']['vertex_count'])
        if definitions_final != definitions_after:
            _fail('KEY_STATE_CHANGED_AFTER_LEDGER',
                  'KeyBlock state changed after ledger commit', 'shape_keys')
        if int(mesh_obj.active_shape_key_index) != active_before:
            _fail('ACTIVE_KEY_INDEX',
                  'active_shape_key_index changed after the write phase',
                  'mesh_obj.active_shape_key_index')
        after = _make_after_fingerprint(
            state, definitions_final, ledger_text, public_created)
        if after['original_keys_sha256'] != state['before_fingerprint']['keys_sha256']:
            _fail('ORIGINAL_KEY_CHANGED',
                  'Original key fingerprint changed after ledger commit',
                  'shape_keys.key_blocks')
        if after['raw_mesh_sha256'] != state['before_fingerprint']['raw_mesh_sha256']:
            _fail('RAW_MESH_CHANGED', 'Raw Mesh identity changed', 'mesh')
        if after['active_shape_key_index'] != active_before:
            _fail('ACTIVE_KEY_INDEX', 'active_shape_key_index final mismatch',
                  'mesh_obj.active_shape_key_index')

        result = _inspection_result(state)
        result.update({
            'status': 'applied',
            'applied_now': True,
            'created': copy.deepcopy(public_created),
            'before_fingerprint': copy.deepcopy(state['before_fingerprint']),
            'after_fingerprint': copy.deepcopy(after),
            'ledger_entry': copy.deepcopy(ledger_entry),
            'ledger_present': True,
            'ledger_sha256': _ledger_sha256(ledger_text),
        })
        return _result_envelope(
            'apply', True, result, created=copy.deepcopy(public_created),
            rollback=_not_attempted_rollback())
    except Exception as error:
        try:
            rollback = _rollback(
                state, created_entries, ledger_write_attempted)
        except Exception as rollback_error:
            rollback = {
                'attempted': True,
                'ok': False,
                'created_removed': [],
                'created_remaining': [
                    record.get('name') for record in created_entries],
                'ledger_restored': False,
                'active_shape_key_index_restored': False,
                'errors': [{
                    'stage': 'rollback_dispatch',
                    **_error_payload(rollback_error),
                }],
                'original_state_verified': False,
            }
        if rollback.get('ok'):
            created_final = []
        else:
            remaining_names = set(rollback.get('created_remaining', []))
            created_final = [
                row for row in public_created if row.get('name') in remaining_names]
            known = {row.get('name') for row in created_final}
            for record in created_entries:
                name = record.get('name')
                if name in remaining_names and name not in known:
                    created_final.append({'name': name})
        before = None if state is None else copy.deepcopy(
            state.get('before_fingerprint'))
        return _result_envelope(
            'apply', False, {
                'status': 'failed',
                'instance_id': None if state is None else state['plan']['instance_id'],
                'mesh': None if state is None else state['plan']['mesh'],
                'plan_sha256': None if state is None else state['plan_sha256'],
                'before_fingerprint': before,
                'after_fingerprint': None,
                'created_attempted': [
                    {'name': record.get('name')}
                    for record in created_entries
                ],
            },
            error=_error_payload(error), created=created_final,
            rollback=rollback)
