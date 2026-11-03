"""One new rotation-only layered Action; preserves existing driver-only AnimData."""
import math
import bpy
from . import motion_contract as contract
from . import pose_rotations as pose
from . import styles

CAPTURE_SCHEMA = 'hrig.motion-clip.snapshot.v1'
SLOT_NAME = 'HRIG Rotation'
LAYER_NAME = 'Rotation Clip'
BINDING_FIELDS = ('action', 'action_slot', 'action_slot_handle', 'last_slot_identifier')


def _fail(code, message, details=None):
    raise contract.MotionError(code, message, details)


def _plain(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail('MOTION_NATIVE_VALUE', 'Nonfinite native authoring value')
        return value
    if isinstance(value, bpy.types.ID):
        return styles.identity(value)
    if getattr(getattr(value, 'bl_rna', None), 'identifier', None) == 'ActionSlot':
        return {'identifier': value.identifier, 'handle': value.handle,
                'target_id_type': value.target_id_type}
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    try:
        return [_plain(v) for v in value]
    except TypeError:
        if hasattr(value, 'bl_rna'):
            result = {'rna': value.bl_rna.identifier}
            if hasattr(value, 'name'):
                result['name'] = value.name
            return result
        _fail('MOTION_NATIVE_VALUE', 'Unsupported native authoring value', {'type': type(value).__name__})


def _settings(item, exclude=()):
    """Writable scalar/array/pointer authoring RNA, excluding runtime getters."""
    return {prop.identifier: _plain(getattr(item, prop.identifier))
            for prop in item.bl_rna.properties
            if prop.identifier not in ('rna_type', *exclude) and not prop.is_readonly
            and prop.type != 'COLLECTION'}


def _curve_state(curve):
    result = {'settings': _settings(curve, ('driver', 'group')),
              'group': curve.group.name if curve.group else None,
              'keyframes': [_settings(point) for point in curve.keyframe_points],
              'sampled_points': [list(point.co) for point in curve.sampled_points],
              'modifiers': [{'type': item.type, 'settings': _settings(item)} for item in curve.modifiers]}
    if curve.driver:
        result['driver'] = {'settings': _settings(curve.driver),
                            'variables': [{'settings': _settings(var),
                                           'targets': [_settings(t) for t in var.targets]}
                                          for var in curve.driver.variables]}
    return result


def animation_state(rig):
    """All writable AnimData fields plus exact driver declarations; no evaluation."""
    ad = rig.animation_data
    if ad is None:
        return None
    return {'settings': _settings(ad), 'drivers': [_curve_state(c) for c in ad.drivers],
            'nla_track_count': len(ad.nla_tracks)}


def clip_state(action):
    """Declared Action authoring observation; producer/cold may compare it exactly."""
    return {'schema': 'hrig.motion-clip.state.v1', 'name': action.name,
            'users': action.users, 'use_fake_user': action.use_fake_user,
            'use_extra_user': action.use_extra_user,
            'settings': _settings(action, ('tag', 'is_runtime_data')),
            'custom_properties': {key: _plain(value) for key, value in action.items()},
            'slots': [{'identifier': slot.identifier, 'handle': slot.handle,
                       'target_id_type': slot.target_id_type, 'name_display': slot.name_display,
                       'settings': _settings(slot)} for slot in action.slots],
            'layers': [{'settings': _settings(layer),
                        'strips': [{'type': strip.type, 'settings': _settings(strip),
                                    'bags': [{'slot': _plain(bag.slot), 'slot_handle': bag.slot_handle,
                                              'groups': [_settings(group) for group in bag.groups],
                                              'curves': [_curve_state(c) for c in bag.fcurves]}
                                             for bag in strip.channelbags]}
                                   for strip in layer.strips]} for layer in action.layers]}


def _field(mode):
    return 'rotation_quaternion' if mode == 'QUATERNION' else 'rotation_euler'


def _domain(context, rig, bones, action_name):
    if context.scene.frame_current != 0 or context.scene.frame_subframe != 0:
        _fail('MOTION_FRAME_CONTEXT', 'Clip creation requires explicit frame 0 and subframe 0')
    ad = rig.animation_data
    if ad is not None:
        if ad.action is not None or len(ad.nla_tracks) or ad.use_tweak_mode:
            _fail('MOTION_ANIMATION_CONFLICT', 'Rig must have no Action, NLA tracks or tweak mode')
        if ad.action_blend_type != 'REPLACE' or ad.action_extrapolation != 'HOLD' or ad.action_influence != 1.0:
            _fail('MOTION_BLEND_CONFLICT', 'Existing AnimData must already use REPLACE/HOLD/influence 1')
    if bpy.data.actions.get(action_name) is not None:
        _fail('MOTION_NAME_CONFLICT', 'Requested Action name already exists')
    try:
        snapshot = pose.native_capture(context, rig, bones)
    except Exception as exc:
        raise contract.MotionError(getattr(exc, 'code', 'MOTION_DOMAIN').replace('POSE_', 'MOTION_'),
                                   str(exc), getattr(exc, 'details', {})) from exc
    for name in bones:
        pb = rig.pose.bones[name]
        if any(pb.lock_rotation) or pb.lock_rotation_w:
            _fail('MOTION_LOCKED', 'Rotation locks prevent clip authoring', {'bone': name})
    return snapshot


def native_capture(context, rig, bones, action_name):
    bones, action_name = contract.canonical_bones(bones), contract.canonical_name(action_name)
    snapshot = _domain(context, rig, bones, action_name)
    return {'schema': CAPTURE_SCHEMA, 'action_name': action_name, 'pose': snapshot,
            'animation': animation_state(rig),
            'scope': 'Selected original pose/rest/context and driver-only AnimData authoring; not arbitrary scripts, solver/cache or GUI certification'}


def _error(exc):
    return {'ok': False, 'changed': False, 'error': {'code': getattr(exc, 'code', 'MOTION_INVALID'),
            'message': str(exc), 'details': getattr(exc, 'details', {})}}


def inspect_clip(context, rig, bones, action_name):
    try:
        capture = native_capture(context, rig, bones, action_name)
        fields = {}
        for name, row in capture['pose']['bones'].items():
            mode = row['pose']['rotation_mode']
            vector = row['pose'][_field(mode)]
            contract.canonical_rotation(mode, vector, 'native.' + name)
            fields[name] = {'rotation_mode': mode, 'value': vector}
        return {'ok': True, 'fields': fields, 'snapshot': capture,
                'snapshot_sha256': contract.digest(capture)}
    except Exception as exc:
        return _error(exc)


def _mutation_seam(stage, rig, action):
    """Injectable observation seam, called only after the named mutation completed."""
    return None


def _action_summary(action, slot):
    return {'name': action.name, 'slot_identifier': slot.identifier, 'slot_handle': slot.handle,
            'target_id_type': slot.target_id_type, 'layer_name': LAYER_NAME}


def _verify(context, rig, request):
    """Independently reconstruct every required path/component/key from request."""
    req = contract.canonical_request(request)
    action = bpy.data.actions.get(req['action_name'])
    ad = rig.animation_data
    if (action is None or ad is None or ad.action != action or action.library or action.override_library
            or action.use_fake_user or action.use_extra_user or action.users != 1):
        _fail('MOTION_VERIFY', 'Expected one local Action with exactly its real rig user')
    if len(action.slots) != 1 or len(action.layers) != 1:
        _fail('MOTION_VERIFY', 'Expected exactly one slot and one layer')
    slot, layer = action.slots[0], action.layers[0]
    if (slot.target_id_type != 'OBJECT' or slot.name_display != SLOT_NAME
            or ad.action_slot != slot or ad.action_slot_handle != slot.handle
            or ad.last_slot_identifier != slot.identifier or layer.name != LAYER_NAME
            or len(layer.strips) != 1):
        _fail('MOTION_VERIFY', 'Action slot/layer binding differs from declared clip')
    if (ad.action_blend_type != 'REPLACE' or ad.action_extrapolation != 'HOLD'
            or ad.action_influence != 1.0 or ad.use_tweak_mode or len(ad.nla_tracks)):
        _fail('MOTION_VERIFY', 'Action binding evaluation flags differ')
    strip = layer.strips[0]
    if strip.type != 'KEYFRAME' or len(strip.channelbags) != 1:
        _fail('MOTION_VERIFY', 'Expected one KEYFRAME strip and one channelbag')
    bag = strip.channelbag(slot)
    if bag is None or bag.slot != slot or bag.slot_handle != slot.handle or len(bag.groups):
        _fail('MOTION_VERIFY', 'Channelbag binding/group declaration differs')
    expected = {}
    for track in req['tracks']:
        pb = rig.pose.bones.get(track['bone'])
        if pb is None or pb.rotation_mode != track['rotation_mode']:
            _fail('MOTION_VERIFY', 'Target mode or bone changed')
        path = pb.path_from_id(_field(track['rotation_mode']))
        for index in range(len(track['before'])):
            expected[path, index] = [[float(k['frame']), k['value'][index]] for k in track['keys']]
    seen = set()
    for curve in bag.fcurves:
        key = curve.data_path, curve.array_index
        if key in seen or key not in expected:
            _fail('MOTION_VERIFY', 'Unexpected or duplicate clip curve', {'path': list(key)})
        seen.add(key)
        if (curve.extrapolation != 'CONSTANT' or curve.mute or curve.lock or curve.group is not None
                or len(curve.modifiers) or len(curve.sampled_points)):
            _fail('MOTION_VERIFY', 'Curve authoring flags differ from rotation-only declaration')
        actual = [list(point.co) for point in curve.keyframe_points]
        if actual != expected[key] or any(point.interpolation != 'LINEAR' for point in curve.keyframe_points):
            _fail('MOTION_VERIFY', 'Exact native curve keys/interpolation differ', {'path': list(key)})
    if seen != set(expected):
        _fail('MOTION_VERIFY', 'Expected rotation component curves are missing')
    return {'ok': True, 'action': _action_summary(action, slot), 'state': clip_state(action)}


def verify_clip(context, rig, request):
    try:
        return _verify(context, rig, request)
    except Exception as exc:
        return _error(exc)


def _raw_ad(ad):
    if ad is None:
        return None
    return {prop.identifier: getattr(ad, prop.identifier) for prop in ad.bl_rna.properties
            if prop.identifier != 'rna_type' and not prop.is_readonly and prop.type != 'COLLECTION'}


def _restore(rig, action, original_ad, original_state, rotations):
    failures = []
    try:
        ad = rig.animation_data
        if original_ad is None:
            if ad is not None:
                if len(ad.drivers) or len(ad.nla_tracks) or (ad.action is not None and ad.action != action):
                    raise RuntimeError('Unexpected consumer in newly created AnimData')
                rig.animation_data_clear()
        else:
            if ad is None:
                raise RuntimeError('Preexisting AnimData disappeared')
            ad.action = None
            for field, value in original_ad.items():
                if field in BINDING_FIELDS:
                    continue
                if getattr(ad, field) != value:
                    setattr(ad, field, value)
            # Native unbind preserves cached identifier and clears handle;
            # restore both original values even when action/slot are null.
            ad.last_slot_identifier = original_ad['last_slot_identifier']
            ad.action_slot_handle = original_ad['action_slot_handle']
        if animation_state(rig) != original_state:
            raise RuntimeError('Restored AnimData fields/drivers differ')
    except Exception as exc:
        failures.append({'part': 'animation_data', 'message': str(exc)})
    for name, mode, field, value in rotations:
        try:
            pb = rig.pose.bones[name]
            if pb.rotation_mode != mode:
                pb.rotation_mode = mode
            if list(getattr(pb, field)) != value:
                setattr(pb, field, value)
            if pb.rotation_mode != mode or list(getattr(pb, field)) != value:
                raise RuntimeError('Active channel restoration differs')
        except Exception as exc:
            failures.append({'part': 'rotation', 'bone': name, 'message': str(exc)})
    if action is not None:
        try:
            if action.users != 0 or action.use_fake_user or action.use_extra_user:
                raise RuntimeError('New Action has unexpected users; refusing forced unlink')
            bpy.data.actions.remove(action)
        except Exception as exc:
            failures.append({'part': 'new_action', 'message': str(exc)})
    return {'ok': not failures, 'failures': failures}


def create_clip(context, rig, request):
    action, backup, rotations = None, None, []
    original_state = None
    begun = False
    try:
        req = contract.canonical_request(request)
        bones = [row['bone'] for row in req['tracks']]
        observed = inspect_clip(context, rig, bones, req['action_name'])
        if not observed['ok']:
            return observed
        if observed['snapshot_sha256'] != req['expected_snapshot_sha256']:
            _fail('SNAPSHOT_CONFLICT', 'Native clip inputs changed')
        for row in req['tracks']:
            native = observed['fields'][row['bone']]
            if native['rotation_mode'] != row['rotation_mode']:
                _fail('MOTION_MODE_CONFLICT', 'Track must retain original active rotation mode')
            if native['value'] != row['before']:
                _fail('MOTION_BEFORE_CONFLICT', 'Track before differs from original native rotation')
            rotations.append((row['bone'], row['rotation_mode'], _field(row['rotation_mode']), native['value']))
        original_state, backup = animation_state(rig), _raw_ad(rig.animation_data)
        begun = True
        action = bpy.data.actions.new(req['action_name'])
        if action.name != req['action_name']:
            _fail('MOTION_NAME_CONFLICT', 'Blender changed requested Action name')
        _mutation_seam('after_action_create', rig, action)
        slot = action.slots.new(id_type='OBJECT', name=SLOT_NAME)
        layer = action.layers.new(LAYER_NAME)
        strip = layer.strips.new(type='KEYFRAME')
        bag = strip.channelbag(slot, ensure=True)
        first = True
        for row in req['tracks']:
            path = rig.pose.bones[row['bone']].path_from_id(_field(row['rotation_mode']))
            for index in range(len(row['before'])):
                curve = bag.fcurves.new(data_path=path, index=index)
                curve.extrapolation = 'CONSTANT'
                curve.keyframe_points.add(len(row['keys']))
                curve.keyframe_points.foreach_set('co', [v for key in row['keys'] for v in (key['frame'], key['value'][index])])
                for point in curve.keyframe_points:
                    point.interpolation = 'LINEAR'
                curve.update()
                if first:
                    first = False
                    _mutation_seam('after_first_curve', rig, action)
        ad = rig.animation_data_create()
        ad.action = action
        ad.action_slot = slot
        _mutation_seam('after_bind', rig, action)
        result = _verify(context, rig, req)
        after_ad = animation_state(rig)
        if original_state is not None:
            expected = {k: v for k, v in original_state['settings'].items() if k not in BINDING_FIELDS}
            actual = {k: v for k, v in after_ad['settings'].items() if k not in BINDING_FIELDS}
            if expected != actual or original_state['drivers'] != after_ad['drivers']:
                _fail('MOTION_WRITE_MISMATCH', 'Existing AnimData settings/drivers changed')
        if context.scene.frame_current != 0 or context.scene.frame_subframe != 0:
            _fail('MOTION_WRITE_MISMATCH', 'Clip creation changed the frame context')
        for name, mode, field, value in rotations:
            if rig.pose.bones[name].rotation_mode != mode or list(getattr(rig.pose.bones[name], field)) != value:
                _fail('MOTION_WRITE_MISMATCH', 'Clip creation changed original active channels')
        result['changed'] = True
        return result
    except Exception as exc:
        result = _error(exc)
        if begun:
            result['rollback'] = _restore(rig, action, backup, original_state, rotations)
            if not result['rollback']['ok']:
                result['changed'] = True
        return result
