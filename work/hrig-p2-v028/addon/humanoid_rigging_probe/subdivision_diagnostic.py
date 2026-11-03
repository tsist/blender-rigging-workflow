"""Owned-worker, single-Subsurf causal observation; never a production edit.

This domain temporarily changes one exact show_viewport leaf. Its cost includes
the changed topology's downstream skinning/modifiers. No source save or debug
enable; frame restoration does not promise exact solver-memory restoration.
Import and plan validation have no bpy dependency.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from time import perf_counter_ns

from .diagnostic_contract import validate_plan as validate_observation
from . import diagnostics

SCHEMA = 'hrig.subdivision-diagnostic.v1'
REPORT_SCHEMA = 'hrig.subdivision-diagnostic-report.v1'


class SubdivisionDiagnosticError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _require(condition, code, message):
    if not condition:
        raise SubdivisionDiagnosticError(code, message)


def _fields(value, keys, label):
    _require(type(value) is dict and set(value) == set(keys),
             'INVALID_SUBDIVISION_PLAN', label + ' has missing or unknown fields')


def _name(value, label):
    _require(type(value) is str and bool(value) and '\x00' not in value,
             'INVALID_SUBDIVISION_PLAN', label + ' must be a nonempty name without NUL')


def validate_plan(value):
    """Validate exact pure-data fields; source/provenance remain outer duties."""
    _fields(value, ('schema', 'observation', 'target', 'enabled'), 'plan')
    _require(type(value['schema']) is str and value['schema'] == SCHEMA,
             'INVALID_SUBDIVISION_PLAN', 'Expected ' + SCHEMA)
    observation = validate_observation(value['observation'])
    target = value['target']
    _fields(target, ('object', 'modifier', 'index', 'expected_show_viewport'), 'target')
    _name(target['object'], 'target.object')
    _name(target['modifier'], 'target.modifier')
    _require(type(target['index']) is int and target['index'] >= 0,
             'INVALID_SUBDIVISION_PLAN', 'target.index must be a nonnegative native integer')
    _require(type(target['expected_show_viewport']) is bool
             and target['expected_show_viewport'] is True,
             'INVALID_SUBDIVISION_PLAN', 'Only an explicitly expected true baseline is admitted')
    _require(type(value['enabled']) is bool, 'INVALID_SUBDIVISION_PLAN', 'enabled must be a native bool')
    selected = [row for row in observation['objects'] if row['name'] == target['object']]
    _require(len(selected) == 1 and selected[0]['vertex_indices'] == [],
             'INVALID_SUBDIVISION_PLAN',
             'Target must be selected once with empty vertex_indices; topology changes invalidate same-index comparison')
    result = copy.deepcopy(value)
    result['observation'] = observation
    return result


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _modifier(obj, target):
    _require(obj.name == target['object'] and obj.type == 'MESH',
             'TARGET_IDENTITY', 'Target object identity/type differs')
    index = target['index']
    _require(index < len(obj.modifiers), 'TARGET_IDENTITY', 'Target modifier index is absent')
    modifier = obj.modifiers[index]
    _require(modifier.name == target['modifier'] and modifier.type == 'SUBSURF',
             'TARGET_IDENTITY', 'Exact modifier name/index/type differs')
    return modifier


def _masked(snapshot, target, expected):
    """Mask exactly the verified one-leaf representation, never a whole ID."""
    result = copy.deepcopy(snapshot)
    _require(type(result) is dict and type(result.get('blocks')) is list,
             'STORAGE_MASK', 'Character snapshot blocks are unavailable')
    blocks = [row for row in result['blocks']
              if row.get('type') == 'Object' and row.get('name') == target['object']
              and row.get('library') is None]
    _require(len(blocks) == 1, 'STORAGE_MASK', 'Exactly one local target Object block required')
    obj = blocks[0].get('object')
    _require(type(obj) is dict and type(obj.get('modifiers')) is list,
             'STORAGE_MASK', 'Object modifier snapshot is unavailable')
    modifiers = obj['modifiers']
    index = target['index']
    _require(index < len(modifiers), 'STORAGE_MASK', 'Snapshot modifier index is absent')
    modifier = modifiers[index]
    # inspection.properties omits readonly RNA type. Native _modifier already
    # verified SUBSURF; bind its real writable serialized configuration here.
    _require(type(modifier) is dict and modifier.get('name') == target['modifier']
             and modifier.get('subdivision_type') in ('CATMULL_CLARK', 'SIMPLE')
             and type(modifier.get('levels')) is int, 'STORAGE_MASK',
             'Snapshot exact modifier identity differs')
    _require(type(modifier.get('show_viewport')) is bool
             and modifier['show_viewport'] is expected,
             'STORAGE_MASK', 'Snapshot flag does not equal the expected native bool')
    del modifier['show_viewport']
    return result


def _cache_policy(scene, frames):
    for obj in scene.objects:
        for modifier in obj.modifiers:
            if modifier.type != 'CLOTH':
                continue
            cache = modifier.point_cache
            required = ('is_baked', 'is_baking', 'use_disk_cache', 'use_external')
            _require(all(hasattr(cache, field) for field in required),
                     'CACHE_DOMAIN', 'Required PointCache admission fields unavailable')
            _require(not any(bool(getattr(cache, field)) for field in required),
                     'CACHE_DOMAIN', 'Baked/baking/disk/external Cloth is outside this fresh-memory domain')
            _require(cache.frame_start == frames[0] and frames[-1] <= cache.frame_end
                     and cache.frame_step == 1, 'CACHE_DOMAIN',
                     'Cloth requires cache-start consecutive frames, step1, within authored range')


def _handler_policy(bpy):
    fields = ('frame_change_pre', 'frame_change_post',
              'depsgraph_update_pre', 'depsgraph_update_post')
    for field in fields:
        _require(hasattr(bpy.app.handlers, field) and not list(getattr(bpy.app.handlers, field)),
                 'REGISTERED_HANDLER', 'Registered or unavailable frame/depsgraph handler list: ' + field)
    return {'checked_handler_lists': list(fields), 'all_empty': True,
            'coverage': 'Target no AnimData and these four native handler lists; not a proof about arbitrary timers/opaque external Python'}


def _state(graph, obj, target, expected):
    original = _modifier(obj, target)
    evaluated_obj = diagnostics._evaluated(graph, obj)
    evaluated = _modifier(evaluated_obj, target)
    _require(type(original.show_viewport) is bool and type(evaluated.show_viewport) is bool
             and original.show_viewport is expected and evaluated.show_viewport is expected,
             'INTERVENTION_OVERRIDDEN', 'Original/evaluated target flag differs from the requested group')
    return {'object': obj.name, 'modifier': target['modifier'], 'index': target['index'],
            'original': {'type': original.type, 'show_viewport': original.show_viewport,
                         'levels': int(original.levels)},
            'evaluated': {'type': evaluated.type, 'show_viewport': evaluated.show_viewport,
                          'levels': int(evaluated.levels)},
            'evaluated_identity_verified': True, 'effective_flag_verified': True,
            'evaluated_vertex_count': len(evaluated_obj.data.vertices),
            'geometry_effectiveness': 'Compare group-specific topology/count and upstream witnesses separately; flag alone is not proof of skipped cost'}


def _error(exc, frame=None):
    return {'code': getattr(exc, 'code', 'NATIVE_SUBDIVISION_DIAGNOSTIC_ERROR'),
            'type': type(exc).__name__, 'message': str(exc), 'requested_frame': frame}


def observe(context, plan, output_dir, capture_storage, *, test_hook=None):
    """Temporarily observe one exact modifier; return failure/restoration data.

    test_hook(stage, frame, object, modifier) is a private owned-test callback,
    not a JSON parameter. No native work occurs during import/validate_plan.
    """
    import bpy

    plan = validate_plan(plan)
    observation, target = plan['observation'], plan['target']
    enabled = plan['enabled']
    directory = Path(output_dir)
    _require(directory.is_absolute() and directory.is_dir(), 'OUTPUT_DIRECTORY',
             'Existing absolute owned-worker output directory required')
    _require(callable(capture_storage) and (test_hook is None or callable(test_hook)),
             'CALLBACK_DOMAIN', 'Storage observer and optional private test hook must be callable')
    report = {'schema': REPORT_SCHEMA, 'status': 'running', 'plan': plan,
              'output_dir': str(directory.resolve()), 'samples': [], 'error': None,
              'source_saved': False, 'FPS_claim': False,
              'authoring_temporary_edit_requested': not enabled,
              'authoring_properties_temporarily_edited': False,
              'test_hook_supplied': test_hook is not None,
              'cost_scope': 'Single Subsurf toggle including changed-density skinning and all downstream response; not pure Subsurf self time',
              'storage_protection': {'exact_equal': False, 'mask_verified': False,
                                     'scope': 'bounded character-storage snapshot; one exact intermediate show_viewport leaf, final unmasked equality'},
              'restore': {'success': False, 'flag_restored': False, 'frame_restored': False,
                          'storage_restored': False, 'errors': []},
              'memory_cache': {'may_have_changed': True, 'exact_cache_restoration_claim': False,
                               'before': None, 'after_restore': None},
              'total_full_vertices_captured': 0, 'total_full_vertices_requested': 0,
              'debug': {'enabled_by_probe': False,
                        'initial_debug_depsgraph_time': bool(bpy.app.debug_depsgraph_time)
                        if hasattr(bpy.app, 'debug_depsgraph_time') else None},
              'timing_scope': {'frame_set_ns': 'frame_set including native evaluation',
                               'explicit_update_ns': 'subsequent explicit ViewLayer update',
                               'acquire_graph_ns': 'fresh depsgraph acquisition after update',
                               'capture_ns': 'target original/evaluated state plus common/full witness capture',
                               'test_hook_ns': 'private test hooks only; excluded from native phase times',
                               'modifier_execution_time': 'may_be_cached_last_execution; not attributable/additive'}}
    scene = layer = obj = None
    original_frame = original_subframe = original_flag = None
    before = None
    frame = None
    frame_attempted = False
    flag_write_attempted = False
    totals = {'full_vertices_captured': 0, 'full_vertices_requested': 0}
    preparation_started = perf_counter_ns()
    try:
        scene, layer, objects, bones = diagnostics._resolve(context, observation)
        original_frame, original_subframe = int(scene.frame_current), diagnostics._finite(scene.frame_subframe)
        obj = diagnostics._scene_object(scene, target['object'])
        _require(context.mode == 'OBJECT' and obj.mode == 'OBJECT', 'OBJECT_MODE_REQUIRED',
                 'Subdivision experiment requires Object mode')
        _require(obj.type == 'MESH' and obj.library is None and obj.override_library is None
                 and obj.data.library is None and obj.data.users == 1,
                 'TARGET_OWNERSHIP', 'One local, non-override, unshared Mesh owner required')
        owners = [item for item in bpy.data.objects if item.type == 'MESH' and item.data == obj.data]
        _require(len(owners) == 1 and owners[0] == obj, 'TARGET_OWNERSHIP', 'Mesh owner identity is not unique')
        _require(obj.animation_data is None, 'ANIMATED_TARGET', 'Target Object has AnimData')
        report['writer_admission'] = _handler_policy(bpy)
        _require(not scene.render.use_simplify, 'SIMPLIFY_DOMAIN', 'Scene Simplify must be disabled')
        modifier = _modifier(obj, target)
        original_flag = modifier.show_viewport
        _require(type(original_flag) is bool and original_flag is True,
                 'TARGET_BASELINE', 'Expected original target flag true')
        _require(modifier.levels >= 1, 'TARGET_BASELINE', 'Nonzero authored Subsurf levels required')
        _cache_policy(scene, observation['frames'])
        report['memory_cache']['before'] = diagnostics._cache_states(scene)
        report['restore'].update(original_flag=original_flag, original_frame=original_frame,
                                 original_subframe=original_subframe)
        before = capture_storage()
        masked_before = _masked(before, target, True)
        protection = report['storage_protection']
        protection['before_sha256'] = _digest(before)
        protection['masked_before_sha256'] = _digest(masked_before)
        if not enabled:
            flag_write_attempted = True
            modifier.show_viewport = False
            report['authoring_properties_temporarily_edited'] = True
            during = capture_storage()
            masked_during = _masked(during, target, False)
            protection['masked_during_sha256'] = _digest(masked_during)
            _require(masked_before == masked_during
                     and protection['masked_before_sha256'] == protection['masked_during_sha256'], 'STORAGE_MASK',
                     'Author storage differs beyond the one admitted show_viewport leaf')
        else:
            protection['masked_during_sha256'] = protection['masked_before_sha256']
        protection['mask_verified'] = True
        report['preparation_ns'] = perf_counter_ns() - preparation_started
        for frame in observation['frames']:
            started = perf_counter_ns()
            frame_attempted = True
            scene.frame_set(frame, subframe=0.0)
            frame_end = perf_counter_ns()
            if test_hook is not None:
                test_hook('after_frame_set', frame, obj, _modifier(obj, target))
            hook_end = perf_counter_ns()
            layer.update()
            update_end = perf_counter_ns()
            graph = context.evaluated_depsgraph_get()
            graph_end = perf_counter_ns()
            _require(context.scene == scene and context.view_layer == layer
                     and scene.frame_current == frame and float(scene.frame_subframe) == 0.0,
                     'CONTEXT_MISMATCH', 'Context/frame changed during sampling')
            target_state = _state(graph, obj, target, enabled)
            sample = diagnostics._capture(graph, objects, bones, observation, totals)
            capture_end = perf_counter_ns()
            if test_hook is not None:
                test_hook('after_capture', frame, obj, _modifier(obj, target))
                _require(_modifier(obj, target).show_viewport is enabled,
                         'INTERVENTION_OVERRIDDEN', 'Target flag changed after the capture hook')
            second_hook_end = perf_counter_ns()
            sample.update(frame=frame, subframe=0.0, graph_mode=graph.mode, target_state=target_state,
                          frame_set_ns=frame_end-started, explicit_update_ns=update_end-hook_end,
                          acquire_graph_ns=graph_end-update_end, capture_ns=capture_end-graph_end,
                          test_hook_ns=(hook_end-frame_end)+(second_hook_end-capture_end)
                          if test_hook is not None else 0)
            report['samples'].append(sample)
            del graph
        report['status'] = 'pass'
    except Exception as exc:
        report['status'] = 'fail'
        report['error'] = _error(exc, frame)
    finally:
        restoration_started = perf_counter_ns()
        restoration = report['restore']
        restoration['flag_write_attempted'] = flag_write_attempted
        restoration['frame_sampling_attempted'] = frame_attempted
        # Preflight refusal must not write a rejected target. Once its exact
        # original flag was admitted, restore only a changed value, then frame.
        if obj is not None and original_flag is True:
            try:
                modifier = _modifier(obj, target)
                restoration['flag_before_restore'] = bool(modifier.show_viewport)
                if modifier.show_viewport is not original_flag:
                    modifier.show_viewport = original_flag
                    restoration['flag_setter_used'] = True
                else:
                    restoration['flag_setter_used'] = False
                restoration['actual_flag'] = modifier.show_viewport
                restoration['flag_restored'] = modifier.show_viewport is original_flag
            except Exception as exc:
                restoration['errors'].append(_error(exc))
        if scene is not None and original_frame is not None:
            try:
                if frame_attempted or flag_write_attempted:
                    scene.frame_set(original_frame, subframe=original_subframe)
                    layer.update()
                restoration['actual_frame'] = int(scene.frame_current)
                restoration['actual_subframe'] = diagnostics._finite(scene.frame_subframe)
                restoration['frame_restored'] = (scene.frame_current == original_frame
                    and abs(float(scene.frame_subframe)-original_subframe) <= 1e-7)
                # A frame writer could overwrite the flag during restoration.
                if obj is not None and original_flag is True:
                    restoration['actual_flag'] = _modifier(obj, target).show_viewport
                    restoration['flag_restored'] = restoration['actual_flag'] is original_flag
            except Exception as exc:
                restoration['errors'].append(_error(exc))
        if before is not None:
            try:
                after = capture_storage()
                report['storage_protection']['after_sha256'] = _digest(after)
                equal = before == after and report['storage_protection']['before_sha256'] == report['storage_protection']['after_sha256']
                report['storage_protection']['exact_equal'] = equal
                restoration['storage_restored'] = equal
            except Exception as exc:
                restoration['errors'].append(_error(exc))
        if scene is not None:
            try:
                after_cache = diagnostics._cache_states(scene)
                report['memory_cache']['after_restore'] = after_cache
                report['memory_cache']['observed_rna_equal'] = report['memory_cache']['before'] == after_cache
            except Exception as exc:
                restoration['errors'].append(_error(exc))
        restoration['success'] = (restoration['flag_restored'] and restoration['frame_restored']
                                  and restoration['storage_restored'] and not restoration['errors'])
        if not restoration['success']:
            report['status'] = 'fail'
            if report['error'] is None:
                report['error'] = {'code': 'RESTORE_PROTECTION', 'type': 'RestorationFailure',
                                   'message': 'Flag/frame or exact author storage not restored', 'requested_frame': frame}
        report['restore_ns'] = perf_counter_ns() - restoration_started
        report['total_full_vertices_captured'] = totals['full_vertices_captured']
        report['total_full_vertices_requested'] = totals['full_vertices_requested']
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report
