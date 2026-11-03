"""Draft finite-frame observer; no integration, source save, or mutation API.

The outer owned-worker runner authenticates source/dependencies/build and
enforces Cloth cache policy before this function. No Blender work on import.
"""
from __future__ import annotations

from array import array
import hashlib
import json
import math
from pathlib import Path
import sys
from time import perf_counter_ns

from .diagnostic_contract import validate_plan

REPORT_SCHEMA = 'hrig.diagnostic-observation.v1'


class DiagnosticProbeError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _require(condition, code, message):
    if not condition:
        raise DiagnosticProbeError(code, message)


def _finite(value):
    value = float(value)
    _require(math.isfinite(value), 'NONFINITE_OBSERVATION', 'Nonfinite native observation')
    return value


def _vector(value):
    return [_finite(component) for component in value]


def _matrix(value):
    return [_vector(row) for row in value]


def _identity(item):
    return {'name': item.name, 'type': item.type,
            'library': item.library.filepath if item.library else None}


def _scene_object(scene, name):
    matches = [obj for obj in scene.objects if obj.name == name]
    _require(len(matches) == 1, 'TARGET_IDENTITY',
             'Selected object name must resolve uniquely in the Scene: ' + name)
    return matches[0]


def _resolve(context, plan):
    scene = context.scene
    layer = context.view_layer
    _require(scene is not None and layer is not None, 'CONTEXT_MISMATCH', 'Scene and ViewLayer required')
    _require(scene.name == plan['context']['scene'] and layer.name == plan['context']['view_layer']
             and scene.view_layers.get(layer.name) == layer,
             'CONTEXT_MISMATCH', 'The active Scene/ViewLayer differs from the plan')
    objects = []
    for row in plan['objects']:
        obj = _scene_object(scene, row['name'])
        _require(obj.type in ('MESH', 'ARMATURE'), 'TARGET_DOMAIN',
                 'Selected object must be a Mesh or Armature in the selected Scene: ' + row['name'])
        _require(obj.type == 'MESH' or not row['vertex_indices'], 'TARGET_DOMAIN',
                 'Armature selections cannot contain vertex indices')
        objects.append((row, obj))
    bones = []
    for row in plan['bones']:
        rig = _scene_object(scene, row['armature'])
        _require(rig.type == 'ARMATURE' and rig.pose is not None
                 and rig.pose.bones.get(row['name']) is not None, 'TARGET_DOMAIN',
                 'Selected bone must exist on an Armature in the selected Scene')
        bones.append((row, rig))
    return scene, layer, objects, bones


def _evaluated(graph, original):
    evaluated = graph.id_eval_get(original)
    _require(evaluated is not None and evaluated != original and evaluated.is_evaluated
             and evaluated.original == original and evaluated.type == original.type,
             'EVALUATED_ID_UNAVAILABLE',
             'Native evaluated object is absent or an original fallback: ' + original.name)
    return evaluated


def _cache_states(scene):
    """Hints only; cache getters do not enumerate/restore solver memory."""
    result = []
    for obj in scene.objects:
        for index, modifier in enumerate(obj.modifiers):
            if modifier.type != 'CLOTH':
                continue
            cache = modifier.point_cache
            fields = {}
            missing = []
            for name in ('frame_start', 'frame_end', 'frame_step', 'index', 'is_baked',
                         'is_outdated', 'is_baking', 'use_disk_cache', 'use_external', 'info'):
                if not hasattr(cache, name):
                    missing.append(name)
                    continue
                value = getattr(cache, name)
                if type(value) in (int, bool, str):
                    fields[name] = value
                elif type(value) is float:
                    fields[name] = _finite(value)
                else:
                    missing.append(name)
            result.append({'object': _identity(obj), 'modifier_index': index,
                           'modifier': modifier.name, 'observed_fields': fields,
                           'unavailable_or_unhandled_fields': missing})
    return result


def _little_endian_bytes(values, expected_itemsize):
    _require(values.itemsize == expected_itemsize, 'UNSUPPORTED_HOST_ENCODING',
             'Unexpected native array item size')
    if sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes()


def _topology(mesh):
    """Index-ordered connectivity, independent from selected-point values."""
    components = (
        ('edges.vertices', mesh.edges, 'vertices', len(mesh.edges) * 2),
        ('loops.vertex_index', mesh.loops, 'vertex_index', len(mesh.loops)),
        ('loops.edge_index', mesh.loops, 'edge_index', len(mesh.loops)),
        ('polygons.loop_start', mesh.polygons, 'loop_start', len(mesh.polygons)),
        ('polygons.loop_total', mesh.polygons, 'loop_total', len(mesh.polygons)),
    )
    hashes = {}
    for label, collection, field, size in components:
        values = array('i', [0]) * size
        if size:
            collection.foreach_get(field, values)
        hashes[label] = hashlib.sha256(_little_endian_bytes(values, 4)).hexdigest()
    return {'vertex_count': len(mesh.vertices), 'edge_count': len(mesh.edges),
            'loop_count': len(mesh.loops), 'polygon_count': len(mesh.polygons),
            'component_sha256': hashes,
            'encoding': 'index-ordered signed int32 little-endian per named component',
            'correspondence_claim': 'Hashes witness indexed connectivity only; unchanged topology does not prove semantic vertex identity across arbitrary inputs'}


def _full_mesh(mesh, totals):
    count = len(mesh.vertices)
    totals['full_vertices_requested'] += count
    coordinates = array('f', [0.0]) * (len(mesh.vertices) * 3)
    if coordinates:
        mesh.vertices.foreach_get('co', coordinates)
    totals['full_vertices_captured'] += count
    _require(all(math.isfinite(value) for value in coordinates), 'NONFINITE_OBSERVATION',
             'Nonfinite full mesh coordinates')
    coordinate_bytes = _little_endian_bytes(coordinates, 4)
    return {'local_coords_sha256': hashlib.sha256(coordinate_bytes).hexdigest(),
            'coords_encoding': 'vertex-index-ordered local xyz float32 little-endian; array f/foreach_get',
            'topology': _topology(mesh)}


def _modifier_hints(obj):
    return [{'index': index, 'name': modifier.name, 'type': modifier.type,
             'execution_seconds': _finite(modifier.execution_time),
             'attribution': 'may_be_cached_last_execution; not evidence this modifier recomputed in this frame'}
            for index, modifier in enumerate(obj.modifiers)]


def _capture(graph, objects, bones, plan, totals):
    rows = []
    evaluated_by_pointer = {}
    full_vertices = 0
    for selection, original in objects:
        evaluated = _evaluated(graph, original)
        evaluated_by_pointer[original.as_pointer()] = evaluated
        matrix = evaluated.matrix_world.copy()
        row = {'selection': selection, 'original': _identity(original),
               'evaluated_identity_verified': True, 'matrix_world': _matrix(matrix),
               'selected_vertices': [], 'modifier_execution_time_hints': _modifier_hints(evaluated)}
        if evaluated.type == 'MESH':
            mesh = evaluated.data
            row['vertex_count'] = len(mesh.vertices)
            for index in selection['vertex_indices']:
                _require(index < len(mesh.vertices), 'VERTEX_INDEX_UNAVAILABLE',
                         'Selected evaluated vertex index does not exist')
                coordinate = mesh.vertices[index].co.copy()
                row['selected_vertices'].append({'index': index, 'local': _vector(coordinate),
                                                  'world': _vector(matrix @ coordinate)})
            if plan['mode'] == 'witness':
                count = len(mesh.vertices)
                _require(totals['full_vertices_captured'] + count <= plan['full_vertex_budget'],
                         'FULL_VERTEX_BUDGET',
                         'Actual cumulative full vertices across all selected Mesh objects and frames exceed the run budget')
                row['full_mesh'] = _full_mesh(mesh, totals)
                full_vertices += count
        rows.append(row)
    bone_rows = []
    for selection, original in bones:
        evaluated = evaluated_by_pointer.get(original.as_pointer())
        if evaluated is None:
            evaluated = _evaluated(graph, original)
            evaluated_by_pointer[original.as_pointer()] = evaluated
        bone = evaluated.pose.bones.get(selection['name']) if evaluated.pose else None
        _require(bone is not None, 'EVALUATED_BONE_UNAVAILABLE', 'Selected evaluated bone is unavailable')
        bone_rows.append({'selection': selection, 'original_armature': _identity(original),
                          'evaluated_identity_verified': True,
                          'world_matrix': _matrix(evaluated.matrix_world @ bone.matrix)})
    return {'objects': rows, 'bones': bone_rows,
            'full_vertices_processed': full_vertices,
            'full_mesh_capture': plan['mode'] == 'witness'}


def observe(context, plan, output_dir):
    """Observe consecutive frames in an already authenticated owned worker.

    Contract/context/output-directory errors raise before frame changes.
    Runtime failures return a failed report after attempting frame/subframe
    restoration. No files are written here. The outer runner owns reporting.
    """
    import bpy

    plan = validate_plan(plan)
    directory = Path(output_dir)
    _require(directory.is_absolute() and directory.is_dir(), 'OUTPUT_DIRECTORY',
             'output_dir must be an existing absolute owned-worker directory')
    scene, layer, objects, bones = _resolve(context, plan)
    original_frame = int(scene.frame_current)
    original_subframe = _finite(scene.frame_subframe)
    before_cache = _cache_states(scene)
    totals = {'full_vertices_requested': 0, 'full_vertices_captured': 0}
    report = {'schema': REPORT_SCHEMA, 'status': 'running', 'plan': plan,
              'output_dir': str(directory.resolve()),
              'blender': {'version': bpy.app.version_string,
                          'build': bpy.app.build_hash.decode('ascii', errors='strict')},
              'FPS_claim': False, 'source_saved': False, 'authoring_properties_edited': False,
              'frame_changes_requested': True, 'samples': [], 'error': None,
              'total_full_vertices_captured': 0, 'total_full_vertices_requested': 0,
              'debug_api': {'app_debug_depsgraph_time_available': hasattr(bpy.app, 'debug_depsgraph_time'),
                            'app_debug_depsgraph_time_initial': bool(bpy.app.debug_depsgraph_time)
                            if hasattr(bpy.app, 'debug_depsgraph_time') else None,
                            'debug_enabled_by_probe': False,
                            'graph_debug_stats_gnuplot_available': None},
              'timing_scope': {'frame_set_ns': 'scene.frame_set including its native evaluation',
                               'explicit_update_ns': 'the subsequent explicit view_layer.update call',
                               'acquire_graph_ns': 'context.evaluated_depsgraph_get after update',
                               'capture_ns': 'all common sampling plus optional full coords/topology/hash; excludes graph acquisition',
                               'modifier_execution_time': 'may_be_cached_last_execution; not attributable to current frame, not additive or an operation profile',
                               'not_measured': ['interactive playback FPS', 'draw/GPU time', 'pure solver time']},
              'restore': {'attempted': False, 'success': False,
                          'original_frame': original_frame, 'original_subframe': original_subframe,
                          'error': None},
              'memory_cache': {'before': before_cache, 'after_restore': None,
                               'may_have_changed': True, 'exact_cache_restoration_claim': False,
                               'coverage': 'selected Scene Cloth RNA hints only; native solver memory is not serialized'}}
    try:
        for frame in plan['frames']:
            started = perf_counter_ns()
            scene.frame_set(frame, subframe=0.0)
            frame_end = perf_counter_ns()
            layer.update()
            update_end = perf_counter_ns()
            graph = context.evaluated_depsgraph_get()
            graph_end = perf_counter_ns()
            _require(context.scene == scene and context.view_layer == layer,
                     'CONTEXT_MISMATCH', 'Scene/ViewLayer changed during sampling')
            _require(scene.frame_current == frame and float(scene.frame_subframe) == 0.0,
                     'FRAME_MISMATCH', 'Native frame differs from requested frame')
            sample = _capture(graph, objects, bones, plan, totals)
            capture_end = perf_counter_ns()
            sample.update(frame=frame, subframe=0.0, graph_mode=graph.mode,
                          frame_set_ns=frame_end-started, explicit_update_ns=update_end-frame_end,
                          acquire_graph_ns=graph_end-update_end, capture_ns=capture_end-graph_end)
            report['debug_api']['graph_debug_stats_gnuplot_available'] = hasattr(graph, 'debug_stats_gnuplot')
            report['samples'].append(sample)
            # No evaluated object/bone/mesh references escape this frame.
            del graph
        report['status'] = 'pass'
    except Exception as exc:
        report['status'] = 'fail'
        report['error'] = {'code': getattr(exc, 'code', 'NATIVE_OBSERVATION_ERROR'),
                           'type': type(exc).__name__, 'message': str(exc),
                           'requested_frame': frame}
    finally:
        report['total_full_vertices_captured'] = totals['full_vertices_captured']
        report['total_full_vertices_requested'] = totals['full_vertices_requested']
        restoration = report['restore']
        restoration['attempted'] = True
        try:
            scene.frame_set(original_frame, subframe=original_subframe)
            layer.update()
            restoration['actual_frame'] = int(scene.frame_current)
            restoration['actual_subframe'] = _finite(scene.frame_subframe)
            restoration['success'] = (scene.frame_current == original_frame
                and abs(float(scene.frame_subframe)-original_subframe) <= 1e-7)
            if not restoration['success']:
                restoration['error'] = {'code': 'FRAME_RESTORE_MISMATCH', 'message': 'Frame/subframe not restored'}
        except Exception as exc:
            restoration['error'] = {'code': 'FRAME_RESTORE_FAILED', 'type': type(exc).__name__, 'message': str(exc)}
        if not restoration['success']:
            report['status'] = 'fail'
        try:
            report['memory_cache']['after_restore'] = _cache_states(scene)
            report['memory_cache']['observed_rna_equal'] = before_cache == report['memory_cache']['after_restore']
        except Exception as exc:
            report['memory_cache']['observation_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            report['status'] = 'fail'
    # Strict serialization is part of the return contract; no file write.
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report
