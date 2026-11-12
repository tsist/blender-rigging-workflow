# SPDX-License-Identifier: GPL-3.0-or-later
"""Complete named-Key library capture and effective per-Key verification.

Adapted from the bounded R03 audit helper (key-library-probe.py, SHA
b23135ebb772b8323aa65a43f35df21946da09fc4c0ddbd6b9167ac3e789a147) with the
audit entry point removed.  Native checks use fixed RAW topology and original
relative/mask metadata; format checks use an isolated unanimated copy and the
effective one-hot delta of every named Key, including Keys never played in the
clip.  Format identity prefers the exported scalar source-vertex attribute;
the positional fallback is explicit and refuses repeated/near-overlapping
basis points or connectivity changes.
"""
from contextlib import contextmanager
from collections import Counter
import bpy
import numpy as np
from mathutils import Vector
from mathutils.kdtree import KDTree
from .io_support import Failure

class KeyLibraryUnsupported(Failure):

    def __init__(self, message):
        super().__init__('UNSUPPORTED', message)

class KeyLibraryMismatch(Failure):

    def __init__(self, message):
        super().__init__('VALIDATION_FAILED', message)

def _finite(value):
    value = np.asarray(value, dtype=np.float64)
    if not np.isfinite(value).all():
        raise KeyLibraryUnsupported('Nonfinite Key-library data')
    return value

def _world(points, matrix):
    points, matrix = (_finite(points), _finite(matrix))
    return points @ matrix[:3, :3].T + matrix[:3, 3]

def _mask(obj, name):
    if not name:
        return np.ones(len(obj.data.vertices), dtype=np.float64)
    group = obj.vertex_groups.get(name)
    if group is None:
        raise KeyLibraryUnsupported('Named Key mask group is missing: ' + name)
    values = np.zeros(len(obj.data.vertices), dtype=np.float64)
    for vertex in obj.data.vertices:
        for item in vertex.groups:
            if item.group == group.index:
                values[vertex.index] = item.weight
    return values

def _triangles(mesh):
    mesh.calc_loop_triangles()
    return [tuple((int(i) for i in tri.vertices)) for tri in mesh.loop_triangles]

def capture_library(obj):
    """Capture the complete relative-Key library before the source scene clears."""
    if obj.type != 'MESH' or obj.mode == 'EDIT':
        raise KeyLibraryUnsupported('Explicit Mesh outside Edit Mode required')
    keys = obj.data.shape_keys
    if keys and (not keys.use_relative or keys.reference_key.name != 'Basis'):
        raise KeyLibraryUnsupported('Only relative Keys with actual Basis are supported')
    matrix = np.asarray(obj.matrix_world, dtype=np.float64)
    basis = keys.reference_key.data if keys else obj.data.vertices
    result = {'source': obj.name, 'vertices': len(obj.data.vertices), 'basis': _world([list(point.co) for point in basis], matrix), 'faces': [tuple(polygon.vertices) for polygon in obj.data.polygons], 'edges': [tuple(edge.vertices) for edge in obj.data.edges], 'triangles': _triangles(obj.data), 'keys': []}
    if keys:
        for block in keys.key_blocks:
            result['keys'].append({'name': block.name, 'relative_key': block.relative_key.name if block.relative_key else None, 'vertex_group': block.vertex_group, 'mask': _mask(obj, block.vertex_group), 'coords': _world([list(point.co) for point in block.data], matrix), 'slider_min': float(block.slider_min), 'slider_max': float(block.slider_max), 'mute': bool(block.mute), 'lock_shape': bool(block.lock_shape)})
    by_name = {key['name']: key for key in result['keys']}
    if len(by_name) != len(result['keys']):
        raise KeyLibraryUnsupported('Duplicate named Keys')
    for key in result['keys']:
        if key['relative_key'] not in by_name:
            raise KeyLibraryUnsupported('Unresolved relative Key: ' + key['name'])
        key['effective_delta'] = (key['coords'] - by_name[key['relative_key']]['coords']) * key['mask'][:, None]
    return result

def _maximum(a, b):
    a, b = (np.asarray(a), np.asarray(b))
    if a.shape != b.shape:
        raise KeyLibraryMismatch('Key-library array shape changed')
    return float(np.linalg.norm(a - b, axis=-1).max(initial=0.0))

def verify_native_library(library, target, *, position_tolerance, mask_aliases=None):
    """Exact-library check on fixed RAW topology/order."""
    mask_aliases = dict(mask_aliases or {})
    observed = capture_library(target)
    names = [key['name'] for key in library['keys']]
    if observed['vertices'] != library['vertices'] or observed['faces'] != library['faces'] or observed['edges'] != library['edges']:
        raise KeyLibraryMismatch('Native RAW topology/order changed')
    if [key['name'] for key in observed['keys']] != names:
        raise KeyLibraryMismatch('Native named-Key inventory/order changed')
    baseline_error = _maximum(observed['basis'], library['basis'])
    if baseline_error > position_tolerance:
        raise KeyLibraryMismatch('Native Basis world coordinates changed')
    rows = []
    for expected, actual in zip(library['keys'], observed['keys']):
        expected_mask = mask_aliases.get(expected['vertex_group'], expected['vertex_group'])
        if actual['relative_key'] != expected['relative_key'] or actual['vertex_group'] != expected_mask:
            raise KeyLibraryMismatch('Native relative/mask identity changed: ' + expected['name'])
        if actual['mute'] != expected['mute'] or actual['lock_shape'] != expected['lock_shape'] or any((abs(actual[key] - expected[key]) > 1e-06 for key in ('slider_min', 'slider_max'))):
            raise KeyLibraryMismatch('Native Key mute/lock/range changed: ' + expected['name'])
        mask_error = float(np.abs(actual['mask'] - expected['mask']).max(initial=0.0))
        coordinate_error = _maximum(actual['coords'], expected['coords'])
        if mask_error > 1e-07 or coordinate_error > position_tolerance:
            raise KeyLibraryMismatch('Native Key coordinates/effective mask changed: ' + expected['name'])
        rows.append({'name': expected['name'], 'coordinates_max_error': coordinate_error, 'mask_max_error': mask_error, 'mask_alias': expected_mask})
    return {'ok': True, 'source': library['source'], 'target': target.name, 'keys_including_Basis': len(rows), 'basis_error': baseline_error, 'position_tolerance': position_tolerance, 'rows': rows}

@contextmanager
def _isolated_raw_copy(obj):
    """No source channel changes, no Armature or post modifiers in the probe."""
    copied = obj.copy()
    data = obj.data.copy()
    keys = data.shape_keys
    try:
        if data == obj.data or (keys and keys == obj.data.shape_keys):
            raise KeyLibraryUnsupported('Native copy did not isolate Mesh/Key')
        copied.data = data
        world = obj.matrix_world.copy()
        copied.parent = None
        copied.matrix_world = world
        copied.constraints.clear()
        copied.modifiers.clear()
        copied.animation_data_clear()
        data.animation_data_clear()
        if keys:
            keys.animation_data_clear()
            for key in keys.key_blocks:
                key.value = 0.0
                key.mute = False
        copied.hide_viewport = False
        copied.hide_render = False
        bpy.context.scene.collection.objects.link(copied)
        copied.hide_set(False)
        yield copied
    finally:
        bpy.data.objects.remove(copied, do_unlink=True)
        bpy.data.batch_remove(ids=[data] + ([keys] if keys else []))

def _raw_points(obj):
    obj.update_tag()
    if obj.data.shape_keys:
        obj.data.shape_keys.update_tag()
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        if len(mesh.vertices) != len(obj.data.vertices):
            raise KeyLibraryUnsupported('Key-only probe unexpectedly changes vertex count')
        return _world([list(point.co) for point in mesh.vertices], obj.matrix_world)
    finally:
        evaluated.to_mesh_clear()

def _source_ids(obj, baseline, library, *, position_tolerance, mapping):
    count = library['vertices']
    if mapping == 'native_raw':
        if len(baseline) != count or [tuple(polygon.vertices) for polygon in obj.data.polygons] != library['faces']:
            raise KeyLibraryUnsupported('Native RAW identity is not fixed')
        ids = np.arange(count)
    elif mapping == 'source_vertex_id':
        attributes = [attribute for attribute in obj.data.attributes if attribute.name.casefold() == '_c94_source_vertex']
        if len(attributes) != 1 or attributes[0].domain != 'POINT' or attributes[0].data_type not in {'INT', 'FLOAT'}:
            raise KeyLibraryUnsupported('Unique scalar POINT source-vertex attribute required')
        raw = np.asarray([float(point.value) for point in attributes[0].data])
        ids = np.rint(raw).astype(np.int64)
        if not np.isfinite(raw).all() or np.max(np.abs(raw - ids), initial=0.0) > 1e-06:
            raise KeyLibraryUnsupported('Source vertex IDs are not finite integers')
    elif mapping == 'unique_base_position':
        tree = KDTree(count)
        for index, point in enumerate(library['basis']):
            tree.insert(Vector(point), index)
        tree.balance()
        for index, point in enumerate(library['basis']):
            if len(tree.find_range(Vector(point), 2 * position_tolerance)) != 1:
                raise KeyLibraryUnsupported('No source ID: duplicate/near-overlapping source Basis positions are ambiguous')
        values = []
        for point in baseline:
            matches = tree.find_range(Vector(point), position_tolerance)
            if len(matches) != 1:
                raise KeyLibraryUnsupported('No unique source Basis position for imported vertex')
            values.append(matches[0][1])
        ids = np.asarray(values, dtype=np.int64)
    elif mapping == 'source_uv_id':
        ids = _uv_source_ids(obj, count)
    else:
        raise KeyLibraryUnsupported('Unknown explicit vertex identity policy')
    if len(ids) != len(baseline) or np.any(ids < 0) or np.any(ids >= count) or (set(ids.tolist()) != set(range(count))):
        raise KeyLibraryMismatch('Source vertex mapping does not completely cover the source library')
    if _maximum(baseline, library['basis'][ids]) > position_tolerance:
        raise KeyLibraryMismatch('Mapped unposed Basis world coordinates changed')
    source_triangles = Counter((tuple(sorted(t)) for t in library['triangles']))
    target_triangles = Counter((tuple(sorted((int(ids[i]) for i in t))) for t in _triangles(obj.data)))
    if source_triangles != target_triangles:
        raise KeyLibraryUnsupported('Mapped triangle connectivity differs; no safe Key correspondence')
    return ids

def _uv_source_ids(obj, count):
    candidates = []
    for layer in obj.data.uv_layers:
        per_vertex = {}
        ok = True
        for loop_index, loop in enumerate(obj.data.loops):
            uv = layer.data[loop_index].uv
            u = float(uv.x)
            v = float(uv.y)
            if abs(v) > 1e-06 or abs(u - round(u)) > 1e-06:
                ok = False
                break
            value = int(round(u))
            if loop.vertex_index in per_vertex and per_vertex[loop.vertex_index] != value:
                ok = False
                break
            per_vertex[loop.vertex_index] = value
        if not ok:
            continue
        ids = [per_vertex.get(vertex.index) for vertex in obj.data.vertices]
        if any((value is None for value in ids)):
            continue
        if set(ids) != set(range(count)):
            continue
        candidates.append(ids)
    if len(candidates) != 1:
        raise KeyLibraryUnsupported('Ambiguous or missing source-ID UV channel: %d candidates' % len(candidates))
    return np.asarray(candidates[0], dtype=np.int64)

def verify_effective_library(library, target, *, position_tolerance, mapping='source_vertex_id'):
    """Validate every named Key, including never-played Keys, with no armature."""
    if target.type != 'MESH' or target.data is None:
        raise KeyLibraryUnsupported('Explicit target Mesh Object required')
    expected_names = [key['name'] for key in library['keys']]
    keys = target.data.shape_keys
    if ([key.name for key in keys.key_blocks] if keys else []) != expected_names:
        raise KeyLibraryMismatch('Effective named-Key library requires identical names')
    rows = []
    with _isolated_raw_copy(target) as probe:
        baseline = _raw_points(probe)
        ids = _source_ids(probe, baseline, library, position_tolerance=position_tolerance, mapping=mapping)
        for key in library['keys']:
            if key['name'] == 'Basis':
                continue
            block = probe.data.shape_keys.key_blocks[key['name']]
            block.value = 1.0
            observed = _raw_points(probe) - baseline
            block.value = 0.0
            expected = key['effective_delta'][ids]
            error = _maximum(observed, expected)
            rows.append({'name': key['name'], 'max_effective_delta_error': error})
            if error > position_tolerance:
                raise KeyLibraryMismatch('Effective named Key changed: ' + key['name'] + ' error=' + str(error))
    return {'ok': True, 'source': library['source'], 'target': target.name, 'mapping': mapping, 'relative_mode': bool(keys.use_relative) if keys else False, 'tested_keys': len(rows), 'evaluations': len(rows) + 1, 'target_vertices': len(ids), 'UV_split_duplicates': len(ids) - library['vertices'], 'position_tolerance': position_tolerance, 'rows': rows, 'scope': 'Unposed/unanimated effective one-hot Key library; not clip animation or literal format mask/relative metadata equality'}

def choose_format_mapping(target, library, position_tolerance):
    """Explicit identity policy; never silently guesses."""
    attributes = [attribute for attribute in target.data.attributes if attribute.name.casefold() == '_c94_source_vertex']
    if len(attributes) == 1:
        return 'source_vertex_id'
    try:
        _uv_source_ids(target, library['vertices'])
        return 'source_uv_id'
    except KeyLibraryUnsupported:
        pass
    try:
        with _isolated_raw_copy(target) as probe:
            _source_ids(probe, _raw_points(probe), library, position_tolerance=position_tolerance, mapping='unique_base_position')
    except KeyLibraryUnsupported as error:
        raise KeyLibraryUnsupported('No source ID channel and positional identity is not qualified: ' + str(error))
    return 'unique_base_position'
