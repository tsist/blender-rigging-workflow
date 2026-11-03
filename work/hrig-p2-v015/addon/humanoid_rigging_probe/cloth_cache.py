"""Selected native embedded Cloth bakes; every authoring field stays unchanged.

This core starts native cache mutation only after full finite preflight. A failed
or interrupted bake requires disposal of its owned worker/candidate: there is no
promise to roll back in-memory cache bytes, and this module never saves anything.
"""
import bpy
from . import cache_contract as contract
from . import motion_clips
from . import styles

CAPTURE_SCHEMA = 'hrig.cloth-cache.snapshot.v1'
CACHE_AUTHOR_FIELDS = ('frame_start', 'frame_end', 'frame_step', 'index', 'use_disk_cache',
                       'name', 'filepath', 'use_external', 'use_library_path')
CACHE_DERIVED_FIELDS = ('is_baked', 'is_baking', 'is_outdated', 'is_frame_skip', 'info')
CAPTURE_SCOPE = ('Selected local native Cloth modifier/settings/collision/effector and PointCache '
                 'writable authoring fields, identical bounded cache ranges and native consumer evidence; '
                 'no arbitrary script/dependency completeness, collision quality, FPS, or bake persistence guarantee')


def _fail(code, message, details=None):
    raise contract.CacheError(code, message, details)


def _derived(cache):
    return {name: motion_clips._plain(getattr(cache, name)) for name in CACHE_DERIVED_FIELDS}


def _error(exc, started=False, completed=None, restoration_error=None):
    details = dict(getattr(exc, 'details', {}))
    if restoration_error is not None:
        details['context_restore_error'] = restoration_error
    return {'ok': False, 'changed': started, 'cache_mutation_started': started,
            'discard_required': started, 'completed_bakes': completed or [],
            'error': {'code': getattr(exc, 'code', 'CACHE_INVALID'), 'message': str(exc), 'details': details},
            'recovery_scope': 'No in-memory cache rollback; discard owned worker/candidate and reopen frozen input'}


def _capture(context, targets, *, allow_baked=False):
    if tuple(bpy.app.version) != (5, 2, 1):
        _fail('DOMAIN', 'Requires verified Blender 5.2.1')
    if (context.mode != 'OBJECT' or context.scene.library or context.scene.override_library):
        _fail('DOMAIN', 'Local non-override Scene and Object mode required')
    if context.scene.frame_current != 0 or context.scene.frame_subframe != 0:
        _fail('CACHE_CONTEXT', 'Cache authoring inspection requires frame0/subframe0')
    rows, handles, paths, derived, shared_range = [], [], [], [], None
    for target in targets:
        obj = bpy.data.objects.get(target['object'])
        if (obj is None or obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.library or obj.override_library
                or obj.data.library or obj.data.override_library or context.scene.objects.get(obj.name) != obj):
            _fail('CACHE_TARGET', 'Explicit local non-override Scene Mesh Object/data required', target)
        if target['index'] >= len(obj.modifiers):
            _fail('CACHE_TARGET', 'Explicit modifier index is absent', target)
        modifier = obj.modifiers[target['index']]
        if modifier.type != 'CLOTH' or modifier.name != target['modifier'] or modifier.id_data != obj:
            _fail('CACHE_TARGET', 'Native Cloth modifier index/name/type/owner differ', target)
        if modifier.show_viewport is not True or modifier.show_render is not True:
            _fail('CACHE_SWITCH', 'Selected Cloth must retain enabled viewport and render evaluation', target)
        cache = modifier.point_cache
        if cache is None:
            _fail('CACHE_STATE', 'Selected native Cloth lacks PointCache', target)
        if (cache.is_baking or cache.use_disk_cache or cache.use_external or (cache.is_baked and not allow_baked)):
            _fail('CACHE_STATE', 'Already baked/baking/disk/external cache is outside fresh embedded bake', target)
        author = motion_clips._settings(cache)
        if set(author) != set(CACHE_AUTHOR_FIELDS):
            _fail('CACHE_RNA', 'Native writable PointCache fields differ from the verified schema',
                  {'target': target, 'actual_fields': sorted(author), 'expected_fields': sorted(CACHE_AUTHOR_FIELDS)})
        native_range = contract.canonical_range(cache.frame_start, cache.frame_end, cache.frame_step)
        if shared_range is not None and native_range != shared_range:
            _fail('CACHE_RANGE', 'Every selected target must retain the same native cache range', target)
        shared_range = native_range
        prefix = modifier.path_from_id() + '.point_cache'
        native = obj.path_resolve(prefix)
        if native.as_pointer() != cache.as_pointer():
            _fail('CACHE_RNA', 'PointCache native RNA container identity differs', target)
        for name in CACHE_AUTHOR_FIELDS:
            path = prefix + '.' + name
            if motion_clips._plain(obj.path_resolve(path)) != author[name]:
                _fail('CACHE_RNA', 'Native PointCache property identity/value differs', {'target': target, 'field': name})
            paths.append((obj, path, obj.name, name))
        rows.append({**target, 'object_id': styles.identity(obj), 'mesh_id': styles.identity(obj.data),
                     'modifier_type': modifier.type, 'modifier_settings': motion_clips._settings(modifier),
                     'cloth_settings': motion_clips._settings(modifier.settings),
                     'collision_settings': motion_clips._settings(modifier.collision_settings),
                     'effector_weights': motion_clips._settings(modifier.settings.effector_weights),
                     'cache_authoring': author, 'native_path': prefix})
        handles.append((obj, modifier))
        derived.append({**target, 'cache': _derived(cache)})
    consumers = styles._consumers(paths)
    writers = [row for row in consumers if row['kind'] not in ('driver_reader', 'driver_reader_alias')]
    if writers:
        _fail('CACHE_WRITER', 'Native Action/driver writes selected cache authoring fields', {'consumers': writers})
    readers = [row for row in consumers if row['kind'] in ('driver_reader', 'driver_reader_alias')]
    if readers:
        _fail('CACHE_CONSUMER', 'Selected cache authoring fields have identified native readers', {'readers': readers})
    snapshot = {'schema': CAPTURE_SCHEMA, 'targets': targets, 'fields': rows, 'range': shared_range,
                'context': {'scene': styles.identity(context.scene), 'view_layer': context.view_layer.name,
                            'frame': context.scene.frame_current, 'subframe': context.scene.frame_subframe,
                            'mode': context.mode, 'active_object': styles.identity(context.view_layer.objects.active),
                            'selected_objects': sorted(obj.name_full for obj in context.selected_objects)},
                'consumers': consumers, 'scope': CAPTURE_SCOPE}
    return {'ok': True, 'fields': [{**target, 'cache_authoring': row['cache_authoring'], 'range': shared_range}
                                   for target, row in zip(targets, rows)],
            'range': shared_range, 'snapshot': snapshot, 'snapshot_sha256': contract.digest(snapshot),
            'derived_cache': derived}, handles


def inspect_cache(context, targets):
    try:
        observed, _ = _capture(context, contract.canonical_targets(targets))
        return observed
    except Exception as exc:
        return _error(exc)


def _poll_bake(context, obj, modifier):
    with context.temp_override(scene=context.scene, object=obj, active_object=obj, point_cache=modifier.point_cache):
        return bpy.ops.ptcache.bake.poll()


def _assert_complete(modifier):
    cache = modifier.point_cache
    observed = _derived(cache)
    if (observed['is_baked'] is not True or observed['is_baking'] is not False
            or observed['is_outdated'] is not False or observed['is_frame_skip'] is not False
            or cache.use_disk_cache or cache.use_external):
        _fail('CACHE_INCOMPLETE', 'Native bake did not leave a complete current embedded cache', {'cache': observed})
    return observed


def _bake_one(context, obj, modifier):
    """Native operator seam: never use bake_all or assign readonly cache flags."""
    with context.temp_override(scene=context.scene, object=obj, active_object=obj, point_cache=modifier.point_cache):
        if not bpy.ops.ptcache.bake.poll():
            _fail('CACHE_POLL', 'Selected native ptcache bake context no longer polls')
        result = bpy.ops.ptcache.bake(bake=True)
    if result != {'FINISHED'}:
        _fail('CACHE_BAKE_FAILED', 'Native selected cache bake did not finish', {'operator_result': sorted(result)})
    return {'operator_result': sorted(result), 'cache': _assert_complete(modifier)}


def _after_bake(context, obj, modifier, ordinal):
    """Fixture injection seam after one real verified bake; no rollback claim."""


def bake_cache(context, request):
    started, restoring, completed, restoration_error = False, False, [], None
    try:
        req = contract.canonical_request(request)
        observed, handles = _capture(context, req['targets'])
        if observed['snapshot_sha256'] != req['expected_snapshot_sha256']:
            _fail('SNAPSHOT_CONFLICT', 'Selected native cache authoring semantics changed')
        for obj, modifier in handles:
            if not _poll_bake(context, obj, modifier):
                _fail('CACHE_POLL', 'A selected native bake context does not poll', {'object': obj.name, 'modifier': modifier.name})
        # Frame movement belongs to the owned worker's native bake operation;
        # all cache authoring ranges and all selection/visibility stay unchanged.
        restoring = True
        try:
            for ordinal, ((obj, modifier), target) in enumerate(zip(handles, req['targets']), 1):
                context.scene.frame_set(observed['range']['frame_start'], subframe=0)
                started = True
                result = _bake_one(context, obj, modifier)
                completed.append({**target, **result})
                _after_bake(context, obj, modifier, ordinal)
        finally:
            try:
                context.scene.frame_set(0, subframe=0)
                context.view_layer.update()
            except Exception as exc:
                restoration_error = str(exc)
        if restoration_error is not None:
            _fail('CACHE_CONTEXT', 'Native bake context could not return to frame0/subframe0')
        after, _ = _capture(context, req['targets'], allow_baked=True)
        for _, modifier in handles:
            _assert_complete(modifier)
        if after['snapshot'] != observed['snapshot']:
            _fail('CACHE_AUTHORING_CHANGED', 'Native bake changed original authoring fields or context')
        return {'ok': True, 'changed': True, 'cache_mutation_started': True, 'discard_required': False,
                'bakes': completed, 'context_restored': True, 'after_snapshot_sha256': after['snapshot_sha256'],
                'derived_cache': after['derived_cache'],
                'scope': 'Native selected embedded caches only; persistence/random-access/quality require independent worker validation'}
    except Exception as exc:
        # Even a frame update can warm runtime caches. Conservatively discard a
        # worker that entered native baking, without claiming cache restoration.
        return _error(exc, started or restoring, completed, restoration_error)
