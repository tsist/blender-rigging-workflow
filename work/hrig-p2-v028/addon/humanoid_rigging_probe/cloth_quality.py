"""Finite consumer-free Cloth quality edits; no cache/collision/animation/geometry edits."""
import copy
import bpy
from . import dynamics_contract as contract
from . import motion_clips
from . import styles

CAPTURE_SCHEMA = 'hrig.cloth-quality.snapshot.v1'
CACHE_AUTHOR_FIELDS = ('frame_start', 'frame_end', 'frame_step', 'use_disk_cache',
                       'use_external', 'filepath', 'name', 'compression', 'use_library_path')
CACHE_DERIVED_FIELDS = ('is_baked', 'is_baking', 'is_outdated', 'info')
CAPTURE_SCOPE = ('Declared original-RNA Cloth modifier/settings/collision/effector configuration, '
                 'explicit cache authoring fields; identified native readers/writers refused; no arbitrary script, '
                 'complete dependency graph, simulation cache, contact, visual or performance certification')


def _fail(code, message, details=None):
    raise contract.DynamicsError(code, message, details)


def _fields(item, names):
    return {name: motion_clips._plain(getattr(item, name)) for name in names if hasattr(item, name)}


def _error(exc):
    return {'ok': False, 'changed': False,
            'error': {'code': getattr(exc, 'code', 'DYNAMICS_INVALID'), 'message': str(exc),
                      'details': getattr(exc, 'details', {})}}


def _capture(context, targets):
    if tuple(bpy.app.version) != (5, 2, 1):
        _fail('DOMAIN', 'Requires verified Blender 5.2.1')
    if context.mode != 'OBJECT':
        _fail('DOMAIN', 'Object mode required')
    if context.scene.frame_current != 0 or context.scene.frame_subframe != 0:
        _fail('DYNAMICS_CONTEXT', 'Cloth quality authoring requires frame0/subframe0')
    rows, handles, paths, derived = [], [], [], []
    for target in targets:
        obj = bpy.data.objects.get(target['object'])
        if (obj is None or obj.type != 'MESH' or obj.mode != 'OBJECT' or obj.library
                or obj.override_library or obj.data.library or obj.data.override_library):
            _fail('DYNAMICS_TARGET', 'Local non-override Mesh Object/data in Object mode required', target)
        if context.scene.objects.get(obj.name) != obj:
            _fail('DYNAMICS_TARGET', 'Explicit target must belong to the Scene', target)
        if target['index'] >= len(obj.modifiers):
            _fail('DYNAMICS_TARGET', 'Explicit modifier index is absent', target)
        modifier = obj.modifiers[target['index']]
        if modifier.type != 'CLOTH' or modifier.name != target['modifier']:
            _fail('DYNAMICS_TARGET', 'Native Cloth modifier name/index/type differ', target)
        cache = modifier.point_cache
        if cache is None or any(getattr(cache, name, False) for name in (
                'is_baked', 'is_baking', 'use_disk_cache', 'use_external')):
            _fail('DYNAMICS_CACHE', 'Baked/baking/disk/external Cloth cache is outside this edit contract', target)
        path = modifier.path_from_id() + '.settings.quality'
        parent = obj.path_resolve(path.rpartition('.')[0])
        if (modifier.id_data != obj or parent.as_pointer() != modifier.settings.as_pointer()
                or obj.path_resolve(path) != modifier.settings.quality):
            _fail('DYNAMICS_RNA', 'Native Cloth quality RNA property identity differs', target)
        prop = modifier.settings.bl_rna.properties.get('quality')
        if prop is None or prop.is_readonly or prop.type != 'INT':
            _fail('DYNAMICS_RNA', 'Writable native integer Cloth quality property required', target)
        quality = contract.canonical_quality(modifier.settings.quality, 'native.quality')
        rows.append({**target, 'quality': quality, 'object_id': styles.identity(obj),
                     'mesh_id': styles.identity(obj.data),
                     'modifier_settings': motion_clips._settings(modifier),
                     'cloth_settings': motion_clips._settings(modifier.settings),
                     'collision_settings': motion_clips._settings(modifier.collision_settings),
                     'effector_weights': motion_clips._settings(modifier.settings.effector_weights),
                     'cache_authoring': _fields(cache, CACHE_AUTHOR_FIELDS), 'native_path': path})
        handles.append((obj, modifier))
        paths.append((obj, path, obj.name, 'quality'))
        derived.append({**target, 'cache': _fields(cache, CACHE_DERIVED_FIELDS)})
    consumers = styles._consumers(paths)
    writers = [row for row in consumers if row['kind'] not in ('driver_reader', 'driver_reader_alias')]
    if writers:
        _fail('DYNAMICS_WRITER', 'Native Action/driver writes a selected quality property', {'consumers': writers})
    readers = [row for row in consumers if row['kind'] in ('driver_reader', 'driver_reader_alias')]
    if readers:
        _fail('DYNAMICS_CONSUMER', 'This finite quality-only domain refuses identified native readers that can drive other properties',
              {'readers': readers})
    snapshot = {'schema': CAPTURE_SCHEMA, 'targets': targets, 'fields': rows,
                'context': {'scene': styles.identity(context.scene), 'view_layer': context.view_layer.name,
                            'mode': context.mode, 'frame': context.scene.frame_current,
                            'subframe': context.scene.frame_subframe,
                            'active_object': styles.identity(context.view_layer.objects.active),
                            'selected_objects': sorted(obj.name_full for obj in context.selected_objects)},
                'consumers': consumers, 'scope': CAPTURE_SCOPE}
    return {'ok': True, 'fields': [{**target, 'quality': row['quality']} for target, row in zip(targets, rows)],
            'snapshot': snapshot, 'snapshot_sha256': contract.digest(snapshot),
            'derived_cache': derived}, handles


def inspect_quality(context, targets):
    try:
        observed, _ = _capture(context, contract.canonical_targets(targets))
        return observed
    except Exception as exc:
        return _error(exc)


def _write_quality(modifier, value):
    """Injectable seam; any wrapper must perform the real setter before faulting."""
    modifier.settings.quality = value
    if modifier.settings.quality != value:
        _fail('WRITE_MISMATCH', 'Native Cloth quality did not store the exact requested integer')


def apply_quality(context, request):
    backup = None
    try:
        req = contract.canonical_request(request)
        targets = [{name: row[name] for name in ('object', 'modifier', 'index')} for row in req['changes']]
        observed, handles = _capture(context, targets)
        if observed['snapshot_sha256'] != req['expected_snapshot_sha256']:
            _fail('SNAPSHOT_CONFLICT', 'Selected native Cloth semantics changed')
        writes = []
        for change, field, handle in zip(req['changes'], observed['fields'], handles):
            if change['before'] != field['quality']:
                _fail('DYNAMICS_BEFORE_CONFLICT', 'Before quality differs from native value', change)
            if change['before'] != change['after']:
                writes.append((change, handle[1]))
        # Full preflight precedes every setter; backups use actual native handles.
        backup = []
        for row, modifier in writes:
            backup.append((modifier, row['before']))
            _write_quality(modifier, row['after'])
        for row, modifier in writes:
            if modifier.settings.quality != row['after']:
                _fail('WRITE_MISMATCH', 'Native Cloth quality changed after its setter', row)
        actual, _ = _capture(context, targets)
        expected = copy.deepcopy(observed['snapshot'])
        for row, change in zip(expected['fields'], req['changes']):
            row['quality'] = change['after']
            row['cloth_settings']['quality'] = change['after']
        if actual['snapshot'] != expected:
            _fail('WRITE_MISMATCH', 'Selected Cloth authoring changed beyond the declared quality values')
        return {'ok': True, 'changed': bool(writes), 'writes': [row for row, _ in writes],
                'after_snapshot_sha256': actual['snapshot_sha256']}
    except Exception as exc:
        result = _error(exc)
        if backup is not None:
            failures = []
            for modifier, value in reversed(backup):
                try:
                    modifier.settings.quality = value
                    if modifier.settings.quality != value:
                        raise RuntimeError('Native restored Cloth quality differs')
                except Exception as restore:
                    failures.append({'modifier': modifier.name, 'message': str(restore)})
            result['rollback'] = {'ok': not failures, 'failures': failures}
            if failures:
                result['changed'] = True
        return result
