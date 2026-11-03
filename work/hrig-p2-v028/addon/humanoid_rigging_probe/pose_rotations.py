"""Explicit active-rotation patches; no frame, rest, solver or animation edits."""
import math
import bpy
from . import pose_contract as contract
from . import styles

CAPTURE_SCHEMA = 'hrig.pose-rotations.snapshot.v1'
CAPTURE_SCOPE = ('Selected original-RNA pose transforms, locks, parent/rest and writable constraint '
                 'parameters only; not full dependency, arbitrary script, evaluated solver or cache certification')


def _fail(code, message, details=None):
    raise contract.PoseError(code, message, details)


def _plain(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail('POSE_NATIVE_VALUE', 'Nonfinite original-RNA capture')
        return value
    if isinstance(value, bpy.types.ID):
        return styles.identity(value)
    if hasattr(value, 'id_data') and hasattr(value, 'name'):
        return {'owner': styles.identity(value.id_data), 'name': value.name,
                'type': value.bl_rna.identifier}
    try:
        return [_plain(v) for v in value]
    except TypeError:
        _fail('POSE_NATIVE_VALUE', 'Unsupported native capture value', {'type': type(value).__name__})


def _constraint(item):
    fields = {}
    # Writable settings exclude error_location/error_rotation/is_valid and all
    # evaluated readonly getters, avoiding dynamic solver state in a pre-write SHA.
    for prop in item.bl_rna.properties:
        if prop.identifier == 'rna_type' or prop.is_readonly or prop.type == 'COLLECTION':
            continue
        fields[prop.identifier] = _plain(getattr(item, prop.identifier))
    return {'type': item.type, 'name': item.name, 'fields': fields}


def _field(mode):
    return 'rotation_quaternion' if mode == 'QUATERNION' else 'rotation_euler'


def _domain(context, rig, bones):
    if tuple(bpy.app.version) != (5, 2, 1):
        _fail('DOMAIN', 'Requires verified Blender 5.2.1')
    if context.mode != 'OBJECT' or rig.mode != 'OBJECT':
        _fail('DOMAIN', 'Object mode required')
    if (rig.type != 'ARMATURE' or rig.library or rig.override_library
            or rig.data.library or rig.data.override_library):
        _fail('DOMAIN', 'Local non-override Armature required')
    if context.scene.objects.get(rig.name) != rig or context.view_layer.objects.get(rig.name) != rig:
        _fail('DOMAIN', 'Rig must belong to explicit scene and view layer')
    owners = [o for o in bpy.data.objects if o.type == 'ARMATURE' and o.data == rig.data]
    if owners != [rig] or rig.data.users != 1 + int(rig.data.use_fake_user):
        _fail('SHARED_ARMATURE', 'Single real Armature owner required')
    paths = []
    for name in bones:
        pb = rig.pose.bones.get(name)
        if pb is None:
            _fail('BINDING_MISSING', 'Selected bone missing', {'bone': name})
        if pb.rotation_mode not in contract.MODES:
            _fail('POSE_MODE', 'Native active rotation mode is unsupported', {'bone': name, 'mode': pb.rotation_mode})
        for field in ('rotation_mode', _field(pb.rotation_mode)):
            path = pb.path_from_id() + '.' + field
            paths.append((rig, path, name, field))
    consumers = styles._consumers(paths)
    writers = [r for r in consumers if r['kind'] not in ('driver_reader', 'driver_reader_alias')]
    if writers:
        _fail('POSE_WRITER', 'Native animation or driver writes selected mode/rotation', {'consumers': writers})


def native_capture(context, rig, bones):
    """Return stable selected original-RNA semantics, raising on invalid domain."""
    bones = contract.canonical_bones(bones)
    _domain(context, rig, bones)
    rows = {}
    for name in bones:
        pb, bone = rig.pose.bones[name], rig.data.bones[name]
        fields = ('rotation_mode', 'rotation_quaternion', 'rotation_euler', 'rotation_axis_angle',
                  'location', 'scale', 'lock_location', 'lock_rotation', 'lock_rotation_w',
                  'lock_rotations_4d', 'lock_scale')
        rows[name] = {
            'pose': {f: _plain(getattr(pb, f)) for f in fields},
            'parent': bone.parent.name if bone.parent else None,
            'rest': {'matrix_local': [_plain(list(row)) for row in bone.matrix_local],
                     'use_connect': bone.use_connect, 'use_inherit_rotation': bone.use_inherit_rotation,
                     'inherit_scale': bone.inherit_scale, 'use_local_location': bone.use_local_location,
                     'use_relative_parent': bone.use_relative_parent},
            # Blender 5.2.1 exposes selection on PoseBone, not Bone. This is
            # directly observed in the v007 v3 original-RNA inventory.
            'selected': pb.select,
            'constraints': [_constraint(c) for c in pb.constraints],
        }
    return {'schema': CAPTURE_SCHEMA, 'rig': styles.identity(rig), 'armature': styles.identity(rig.data),
            'scene': styles.identity(context.scene), 'view_layer': context.view_layer.name,
            'frame': context.scene.frame_current, 'subframe': context.scene.frame_subframe,
            'context_active_object': styles.identity(context.view_layer.objects.active),
            'context_selected_objects': sorted(o.name_full for o in context.selected_objects),
            'active_bone': rig.data.bones.active.name if rig.data.bones.active else None,
            'bones': rows, 'scope': CAPTURE_SCOPE}


def _observe(context, rig, bones):
    snap = native_capture(context, rig, bones)
    fields = {}
    for name, row in snap['bones'].items():
        mode = row['pose']['rotation_mode']
        value = row['pose'][_field(mode)]
        # Inspect exposes the exact native values, never normalization/sign flips.
        contract.canonical_rotation(mode, value, 'native.' + name)
        fields[name] = {'rotation_mode': mode, 'value': value}
    return {'ok': True, 'fields': fields, 'snapshot': snap, 'snapshot_sha256': contract.digest(snap)}


def _error(exc):
    return {'ok': False, 'changed': False, 'error': {'code': getattr(exc, 'code', 'POSE_INVALID'),
            'message': str(exc), 'details': getattr(exc, 'details', {})}}


def inspect_rotations(context, rig, bones):
    try:
        return _observe(context, rig, contract.canonical_bones(bones))
    except Exception as exc:
        return _error(exc)


def _write_rotation(rig, bone, field, value):
    """Injectable mutation seam; rollback deliberately bypasses this function."""
    setattr(rig.pose.bones[bone], field, value)


def apply_rotations(context, rig, request):
    backup = None
    try:
        req = contract.canonical_request(request)
        bones = [r['bone'] for r in req['rotations']]
        observed = _observe(context, rig, bones)
        if observed['snapshot_sha256'] != req['expected_snapshot_sha256']:
            _fail('SNAPSHOT_CONFLICT', 'Selected native semantics changed')
        writes = []
        for row in req['rotations']:
            name, mode = row['bone'], row['rotation_mode']
            current = observed['fields'][name]
            if current['rotation_mode'] != mode:
                _fail('POSE_MODE_CONFLICT', 'Request must retain native rotation mode', {'bone': name})
            if current['value'] != row['before']:
                _fail('POSE_BEFORE_CONFLICT', 'Before rotation differs from native value', {'bone': name})
            if row['before'] == row['after']:
                continue
            pb = rig.pose.bones[name]
            if any(pb.lock_rotation) or pb.lock_rotation_w:
                _fail('POSE_LOCKED', 'Rotation locks prevent absolute rotation replacement', {'bone': name})
            writes.append({'bone': name, 'field': _field(mode), 'value': row['after']})
        backup = [(w['bone'], w['field'], list(getattr(rig.pose.bones[w['bone']], w['field']))) for w in writes]
        for w in writes:
            _write_rotation(rig, w['bone'], w['field'], w['value'])
        for w in writes:
            actual = list(getattr(rig.pose.bones[w['bone']], w['field']))
            if actual != w['value']:
                _fail('WRITE_MISMATCH', 'Native rotation did not store canonical value',
                      {'bone': w['bone'], 'field': w['field'], 'expected': w['value'], 'actual': actual})
        return {'ok': True, 'changed': bool(writes), 'writes': writes}
    except Exception as exc:
        result = _error(exc)
        if backup is not None:
            failures = []
            for bone, field, value in reversed(backup):
                try:
                    setattr(rig.pose.bones[bone], field, value)
                    if list(getattr(rig.pose.bones[bone], field)) != value:
                        raise RuntimeError('Native restored value differs')
                except Exception as restore:
                    failures.append({'bone': bone, 'field': field, 'message': str(restore)})
            result['rollback'] = {'ok': not failures, 'failures': failures}
            if failures:
                result['changed'] = True
        return result


def _location_domain(context, rig, bones):
    if tuple(bpy.app.version) != (5, 2, 1):
        _fail('DOMAIN', 'Requires verified Blender 5.2.1')
    if context.mode != 'OBJECT' or rig.mode != 'OBJECT':
        _fail('DOMAIN', 'Object mode required')
    if (rig.type != 'ARMATURE' or rig.library or rig.override_library
            or rig.data.library or rig.data.override_library):
        _fail('DOMAIN', 'Local non-override Armature required')
    if context.scene.objects.get(rig.name) != rig or context.view_layer.objects.get(rig.name) != rig:
        _fail('DOMAIN', 'Rig must belong to explicit scene and view layer')
    owners = [o for o in bpy.data.objects if o.type == 'ARMATURE' and o.data == rig.data]
    if owners != [rig] or rig.data.users != 1 + int(rig.data.use_fake_user):
        _fail('SHARED_ARMATURE', 'Single real Armature owner required')
    paths = []
    for name in bones:
        pb = rig.pose.bones.get(name)
        if pb is None:
            _fail('BINDING_MISSING', 'Selected bone missing', {'bone': name})
        if pb.bone.use_connect:
            _fail('LOCATION_CONNECTED', 'Connected bones do not expose independent local translation', {'bone': name})
        if pb.is_property_readonly('location'):
            _fail('LOCATION_READONLY', 'Selected location is read-only', {'bone': name})
        paths.append((rig, pb.path_from_id('location'), name, 'location'))
    consumers = styles._consumers(paths)
    writers = [row for row in consumers if row['kind'] not in ('driver_reader', 'driver_reader_alias')]
    if writers:
        _fail('LOCATION_WRITER', 'Animation or driver writes selected location', {'consumers': writers})
    return consumers


def native_location_capture(context, rig, bones):
    bones = contract.canonical_bones(bones)
    consumers = _location_domain(context, rig, bones)
    rows = {}
    for name in bones:
        pb, bone = rig.pose.bones[name], rig.data.bones[name]
        fields = ('rotation_mode', 'rotation_quaternion', 'rotation_euler', 'rotation_axis_angle',
                  'location', 'scale', 'lock_location', 'lock_rotation', 'lock_rotation_w',
                  'lock_rotations_4d', 'lock_scale')
        rows[name] = {'pose': {field: _plain(getattr(pb, field)) for field in fields},
            'parent': bone.parent.name if bone.parent else None,
            'rest': {'matrix_local': [_plain(list(row)) for row in bone.matrix_local],
                'use_connect': bone.use_connect, 'use_inherit_rotation': bone.use_inherit_rotation,
                'inherit_scale': bone.inherit_scale, 'use_local_location': bone.use_local_location,
                'use_relative_parent': bone.use_relative_parent},
            'selected': pb.select, 'constraints': [_constraint(c) for c in pb.constraints]}
    return {'schema': 'hrig.pose-locations.snapshot.v1', 'rig': styles.identity(rig),
        'armature': styles.identity(rig.data), 'scene': styles.identity(context.scene),
        'view_layer': context.view_layer.name, 'frame': context.scene.frame_current,
        'subframe': context.scene.frame_subframe, 'context_active_object': styles.identity(context.view_layer.objects.active),
        'context_selected_objects': sorted(o.name_full for o in context.selected_objects),
        'active_bone': rig.data.bones.active.name if rig.data.bones.active else None,
        'bones': rows, 'consumers': consumers,
        'scope': 'Original local locations and selected native LRS/rest/locks/constraints; writer scan is bounded native coverage, not arbitrary handlers or final solver reachability'}


def _location_error(error):
    return {'ok': False, 'changed': False, 'error': {'code': getattr(error, 'code', 'LOCATION_INVALID'),
        'message': str(error), 'details': getattr(error, 'details', {})}}


def inspect_locations(context, rig, bones):
    try:
        snap = native_location_capture(context, rig, bones)
        return {'ok': True, 'fields': {name: {'value': contract.canonical_location_vector(
            row['pose']['location'], 'native.' + name)} for name, row in snap['bones'].items()},
            'snapshot': snap, 'snapshot_sha256': contract.digest(snap)}
    except Exception as error:
        return _location_error(error)


def _write_location(rig, bone, field, value):
    if field != 'location':
        _fail('LOCATION_VALUE', 'Only local location may be written')
    setattr(rig.pose.bones[bone], field, value)


def apply_locations(context, rig, request):
    backup = None
    try:
        req = contract.canonical_location_request(request)
        observed = inspect_locations(context, rig, [row['bone'] for row in req['locations']])
        if not observed['ok']:
            error = observed['error']
            _fail(error['code'], error['message'], error['details'])
        if observed['snapshot_sha256'] != req['expected_snapshot_sha256']:
            _fail('SNAPSHOT_CONFLICT', 'Selected native location semantics changed')
        writes = []
        for row in req['locations']:
            name = row['bone'];pb = rig.pose.bones[name]
            if list(pb.location) != row['before']:
                _fail('POSE_BEFORE_CONFLICT', 'Before location differs from native value', {'bone': name})
            if row['before'] == row['after']:
                continue
            if any(lock and row['before'][index] != row['after'][index]
                   for index, lock in enumerate(pb.lock_location)):
                _fail('LOCATION_LOCKED', 'A changing location axis is locked', {'bone': name})
            writes.append({'bone': name, 'field': 'location', 'value': row['after']})
        backup = [(row['bone'], list(rig.pose.bones[row['bone']].location)) for row in writes]
        for row in writes:
            _write_location(rig, row['bone'], row['field'], row['value'])
        for row in writes:
            if list(rig.pose.bones[row['bone']].location) != row['value']:
                _fail('WRITE_MISMATCH', 'Native location did not store canonical value', {'bone': row['bone']})
        return {'ok': True, 'changed': bool(writes), 'writes': writes}
    except Exception as error:
        result = _location_error(error)
        if backup is not None:
            failures = []
            for name, value in reversed(backup):
                try:
                    rig.pose.bones[name].location = value
                    if list(rig.pose.bones[name].location) != value:
                        raise RuntimeError('Native restored location differs')
                except Exception as restore:
                    failures.append({'bone': name, 'field': 'location', 'message': str(restore)})
            result['rollback'] = {'ok': not failures, 'failures': failures}
            if failures:
                result['changed'] = True
        return result
