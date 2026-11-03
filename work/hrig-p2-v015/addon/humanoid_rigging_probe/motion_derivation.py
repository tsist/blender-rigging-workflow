"""Explicit copy-and-append rotation Action derivation; native probe still required.

The no-Action guard in motion_clips.create_clip is untouched. This module verifies
the original declaration, copies its native Action, appends new-bone tracks to
the copied bag, then binds the rig directly. It does not save files or bake Cloth.
"""
import copy
import bpy
from . import motion_derivation_contract as contract
from . import motion_contract
from . import motion_clips as motion
from . import pose_rotations as pose
from . import styles

CAPTURE_SCHEMA = 'hrig.motion-clip.derivation-snapshot.v1'
POSE_FIELDS = ('rotation_mode', 'location', 'rotation_quaternion', 'rotation_euler', 'rotation_axis_angle', 'scale')
BINDING_FIELDS = motion.BINDING_FIELDS


def _fail(code, message, details=None):
    raise contract.DerivationError(code, message, details)


def _error(exc):
    return {'ok': False, 'changed': False, 'error': {'code': getattr(exc, 'code', 'DERIVATION_INVALID'),
            'message': str(exc), 'details': getattr(exc, 'details', {})}}


def _users(action):
    return sorted((styles.identity(item) for item in bpy.data.user_map().get(action, set())),
                  key=lambda row: (row['type'], row['name'], str(row.get('library'))))


def _one_rig_user(action, rig):
    if action.users != 1 or action.use_fake_user or action.use_extra_user or _users(action) != [styles.identity(rig)]:
        _fail('DERIVATION_USERS', 'Action requires exactly this real rig user without fake/extra/other consumers')


def _no_users(action):
    if action.users != 0 or action.use_fake_user or action.use_extra_user or _users(action):
        _fail('DERIVATION_USERS', 'Original/copied orphan Action has an unexpected consumer or fake/extra user')


def _copy_safe_metadata(action):
    # Action.copy can duplicate additional ID-reference edges through its own
    # AnimData, asset metadata or ID properties. v1 refuses those domains rather
    # than allowing unexplained user-count changes on other IDs.
    if getattr(action, 'animation_data', None) is not None or action.asset_data is not None:
        _fail('DERIVATION_COPY_REFERENCES', 'Source/copied Action own AnimData or asset data is outside v1')
    def check(value, depth=0):
        if isinstance(value, bpy.types.ID):
            _fail('DERIVATION_COPY_REFERENCES', 'ID-valued Action custom properties are outside v1')
        if value is None or type(value) in (str, bool, int, float):
            return
        # The old clip observer represents nested IDPropertyGroup iteration as
        # keys, not a full mapping. Reject it instead of claiming exact content.
        if hasattr(value, 'items') or depth > 16:
            _fail('DERIVATION_COPY_REFERENCES', 'Nested/custom mapping metadata requires a future exact observer')
        try:
            for child in value:
                check(child, depth + 1)
        except TypeError:
            _fail('DERIVATION_COPY_REFERENCES', 'Unsupported Action custom-property value')
    for value in action.values():
        check(value)


def _pose_state(rig):
    return {pb.name: {field: motion._plain(getattr(pb, field)) for field in POSE_FIELDS} for pb in rig.pose.bones}


def _cloth_state(context):
    """All active-Scene Cloth, including hidden objects: no disk/external/baked input."""
    rows = []
    for obj in context.scene.objects:
        for index, modifier in enumerate(obj.modifiers):
            if modifier.type != 'CLOTH':
                continue
            cache = modifier.point_cache
            if cache is None or cache.is_baked or cache.is_baking or cache.use_disk_cache or cache.use_external:
                _fail('DERIVATION_CACHE_STATE', 'Active Scene Cloth must retain fresh unbaked embedded authoring',
                      {'object': obj.name, 'modifier': modifier.name, 'index': index})
            rows.append({'object': styles.identity(obj), 'index': index, 'modifier': modifier.name,
                'modifier_settings': motion._settings(modifier), 'settings': motion._settings(modifier.settings),
                'collision': motion._settings(modifier.collision_settings), 'effectors': motion._settings(modifier.settings.effector_weights),
                'cache_authoring': motion._settings(cache)})
    return sorted(rows, key=lambda row: (row['object']['name'], row['index']))


def _binding(rig):
    ad = rig.animation_data
    return {key: motion._plain(getattr(ad, key)) for key in BINDING_FIELDS} if ad else None


def _bag(action):
    if len(action.slots) != 1 or len(action.layers) != 1:
        _fail('DERIVATION_LAYOUT', 'Original/copied Action requires exactly its one existing slot and layer')
    slot, layer = action.slots[0], action.layers[0]
    if slot.target_id_type != 'OBJECT' or slot.name_display != motion.SLOT_NAME or layer.name != motion.LAYER_NAME or len(layer.strips) != 1:
        _fail('DERIVATION_LAYOUT', 'Original slot/layer identity differs from the old clip contract')
    strip = layer.strips[0]
    if strip.type != 'KEYFRAME' or len(strip.channelbags) != 1:
        _fail('DERIVATION_LAYOUT', 'One existing KEYFRAME strip/channelbag required')
    bag = strip.channelbag(slot)
    if bag is None or bag.slot != slot or bag.slot_handle != slot.handle or len(bag.groups):
        _fail('DERIVATION_LAYOUT', 'Copied channelbag binding/group declaration differs')
    return slot, bag


def _native_capture(context, rig, bones, action_name, base_clip):
    base = contract.canonical_base(base_clip)
    bones, action_name = contract.canonical_bones(bones), contract.canonical_name(action_name)
    if context.scene.frame_current != 0 or context.scene.frame_subframe != 0:
        _fail('DERIVATION_FRAME_CONTEXT', 'Derivation requires frame0/subframe0')
    if action_name == base['action_name'] or bpy.data.actions.get(action_name) is not None:
        _fail('DERIVATION_NAME_CONFLICT', 'Derived Action name must be distinct and unused')
    if set(bones) & {track['bone'] for track in base['tracks']}:
        _fail('DERIVATION_OVERLAP', 'New selected bones overlap original clip tracks')
    if len(bones) + len(base['tracks']) > 32:
        _fail('DERIVATION_LIMIT', 'Combined clip exceeds32 tracks')
    verified = motion.verify_clip(context, rig, base)
    if not verified.get('ok'):
        _fail('DERIVATION_BASE_VERIFY', 'Original native clip declaration did not verify exactly', {'verification': verified})
    original = rig.animation_data.action
    _one_rig_user(original, rig)
    _copy_safe_metadata(original)
    # Only unanimated newly-selected bones go through the old native capture;
    # existing base bones intentionally have their declared Action writers.
    selected = pose.native_capture(context, rig, bones)
    for name in bones:
        pb = rig.pose.bones[name]
        if any(pb.lock_rotation) or pb.lock_rotation_w:
            _fail('DERIVATION_LOCKED', 'Original native rotation locks prevent added tracks', {'bone': name})
    cloth = _cloth_state(context)
    return {'schema': CAPTURE_SCHEMA, 'action_name': action_name, 'base_clip': base,
        'pose': selected, 'all_pose_channels': _pose_state(rig), 'animation': motion.animation_state(rig),
        'original_action': verified['state'], 'original_action_users': _users(original), 'binding': _binding(rig),
        'cloth_authoring': cloth,
        'scope': 'explicit original Action declaration and additive selected native rotations; no arbitrary script/cache/visual certification'}


def inspect_derivation(context, rig, bones, action_name, base_clip):
    try:
        snapshot = _native_capture(context, rig, bones, action_name, base_clip)
        fields = {}
        for name, row in snapshot['pose']['bones'].items():
            mode = row['pose']['rotation_mode']
            value = row['pose'][motion._field(mode)]
            motion_contract.canonical_rotation(mode, value, 'native.' + name)
            fields[name] = {'rotation_mode': mode, 'value': value}
        return {'ok': True, 'fields': fields, 'snapshot': snapshot, 'snapshot_sha256': contract.digest(snapshot),
                'original_action_before': snapshot['original_action'], 'binding': snapshot['binding']}
    except Exception as exc:
        return _error(exc)


def _renamed_state(original, name, users):
    expected = copy.deepcopy(original)
    expected['name'], expected['users'] = name, users
    # Action name is represented both at the top level and in writable RNA.
    expected['settings']['name'] = name
    return expected


def _inherited_state(state, original):
    """Project only the old curves while retaining every other observed setting."""
    result = copy.deepcopy(state)
    old_curves = original['layers'][0]['strips'][0]['bags'][0]['curves']
    keys = {(row['settings']['data_path'], row['settings']['array_index']) for row in old_curves}
    bag = result['layers'][0]['strips'][0]['bags'][0]
    bag['curves'] = [row for row in bag['curves'] if (row['settings']['data_path'], row['settings']['array_index']) in keys]
    return result


def _baseline(snapshot):
    if snapshot is None:
        return None, None
    if type(snapshot) is not dict:
        _fail('DERIVATION_BASELINE', 'Baseline must be an explicit derivation snapshot or clip state')
    if snapshot.get('schema') == CAPTURE_SCHEMA:
        return snapshot['original_action'], snapshot
    if snapshot.get('schema') == 'hrig.motion-clip.state.v1':
        return snapshot, None
    _fail('DERIVATION_BASELINE', 'Unsupported baseline state schema')


def _verify_unbound_original(rig, action, base):
    """Check the explicit old declaration without temporarily binding its Action."""
    if action is None or action.library or action.override_library:
        _fail('DERIVATION_ORIGINAL', 'Original local Action must still exist')
    _no_users(action)
    _copy_safe_metadata(action)
    slot, bag = _bag(action)
    expected = {}
    for track in base['tracks']:
        pb = rig.pose.bones.get(track['bone'])
        if pb is None or pb.rotation_mode != track['rotation_mode']:
            _fail('DERIVATION_ORIGINAL', 'Original bone/mode changed')
        path = pb.path_from_id(motion._field(track['rotation_mode']))
        for index in range(len(track['before'])):
            expected[path, index] = [[float(key['frame']), key['value'][index]] for key in track['keys']]
    seen = set()
    for curve in bag.fcurves:
        key = curve.data_path, curve.array_index
        if key in seen or key not in expected:
            _fail('DERIVATION_ORIGINAL', 'Unexpected/duplicate original curve')
        seen.add(key)
        if (curve.extrapolation != 'CONSTANT' or curve.mute or curve.lock or curve.group is not None
                or len(curve.modifiers) or len(curve.sampled_points)
                or [list(point.co) for point in curve.keyframe_points] != expected[key]
                or any(point.interpolation != 'LINEAR' for point in curve.keyframe_points)):
            _fail('DERIVATION_ORIGINAL', 'Original native curve declaration changed')
    if seen != set(expected):
        _fail('DERIVATION_ORIGINAL', 'Original component curve missing')
    return motion.clip_state(action)


def verify_derivation(context, rig, request, baseline_state=None):
    try:
        req = contract.canonical_request(request)
        combined = contract.combined_request(req)
        verified = motion.verify_clip(context, rig, combined)
        if not verified.get('ok'):
            _fail('DERIVATION_VERIFY', 'Derived combined native clip did not verify', {'verification': verified})
        current = rig.animation_data.action
        _one_rig_user(current, rig)
        _copy_safe_metadata(current)
        original = bpy.data.actions.get(req['base_clip']['action_name'])
        original_after = _verify_unbound_original(rig, original, req['base_clip'])
        original_before, snapshot = _baseline(baseline_state)
        if original_before is not None:
            if (original_before['name'] != req['base_clip']['action_name'] or original_before['users'] != 1
                    or original_before['use_fake_user'] or original_before['use_extra_user']):
                _fail('DERIVATION_BASELINE', 'Original baseline must declare exactly its one real author rig user')
            if original_after != _renamed_state(original_before, original_before['name'], 0):
                _fail('DERIVATION_ORIGINAL_CHANGED', 'Original Action changed beyond the explicit real-user1-to0 edge')
        inherited = _inherited_state(verified['state'], original_after)
        if inherited != _renamed_state(original_after, req['action_name'], 1):
            _fail('DERIVATION_INHERITANCE', 'Copied Action changed original curves/custom properties/slot/layer/settings')
        if snapshot is not None:
            if contract.digest(snapshot) != req['expected_snapshot_sha256']:
                _fail('DERIVATION_BASELINE', 'Original native baseline does not match the explicit request snapshot')
            if snapshot['base_clip'] != req['base_clip'] or snapshot['action_name'] != req['action_name']:
                _fail('DERIVATION_BASELINE', 'Baseline declaration/name differs')
            if _pose_state(rig) != snapshot['all_pose_channels']:
                _fail('DERIVATION_POSE_CHANGED', 'Binding/curve creation changed original pose channels')
            after_animation = motion.animation_state(rig)
            before_settings = {k: v for k, v in snapshot['animation']['settings'].items() if k not in BINDING_FIELDS}
            after_settings = {k: v for k, v in after_animation['settings'].items() if k not in BINDING_FIELDS}
            if (before_settings != after_settings or after_animation['drivers'] != snapshot['animation']['drivers']
                    or after_animation['nla_track_count'] != snapshot['animation']['nla_track_count']):
                _fail('DERIVATION_ANIMDATA_CHANGED', 'Nonbinding AnimData/drivers/NLA changed')
            if _cloth_state(context) != snapshot['cloth_authoring']:
                _fail('DERIVATION_CLOTH_CHANGED', 'Original Cloth/cache authoring changed')
        else:
            _cloth_state(context)
        return {'ok': True, 'combined_request': combined, 'action': verified['action'], 'state': verified['state'],
                'original_action_before': original_before, 'original_action_after': original_after, 'binding': _binding(rig),
                'original_content_exact_against_baseline': original_before is not None,
                'original_reference_change': {'before_real_users': 1 if original_before is not None else None,
                    'after_real_users': original.users, 'after_consumers': _users(original)},
                'scope': 'original and combined declarations verified; historical unchanged content only when baseline supplied'}
    except Exception as exc:
        return _error(exc)


def _mutation_seam(stage, rig, action):
    """Called after native copy, first completed curve and binding; fixtures may raise."""
    return None


def _restore(context, rig, created, raw_animation, original_animation, original_action, original_state,
             original_pose, original_actions, original_cloth):
    failures = []
    try:
        ad = rig.animation_data
        if ad is None:
            raise RuntimeError('Original AnimData disappeared; refusing reconstruction that could discard drivers')
        # Directly restore the real original binding, never detach to invoke the
        # old no-Action create path. Every cached native binding value is restored.
        ad.action = raw_animation['action']
        ad.action_slot = raw_animation['action_slot']
        for name, value in raw_animation.items():
            if name not in BINDING_FIELDS and getattr(ad, name) != value:
                setattr(ad, name, value)
        ad.action_slot_handle = raw_animation['action_slot_handle']
        ad.last_slot_identifier = raw_animation['last_slot_identifier']
        if motion.animation_state(rig) != original_animation:
            raise RuntimeError('Exact original AnimData/binding/drivers restoration differs')
    except Exception as exc:
        failures.append({'part': 'animation_data_binding_drivers', 'message': str(exc)})
    for name, fields in original_pose.items():
        try:
            pb = rig.pose.bones.get(name)
            if pb is None:
                raise RuntimeError('Original pose bone disappeared')
            if pb.rotation_mode != fields['rotation_mode']:
                pb.rotation_mode = fields['rotation_mode']
            for field in POSE_FIELDS:
                if field != 'rotation_mode' and motion._plain(getattr(pb, field)) != fields[field]:
                    setattr(pb, field, fields[field])
        except Exception as exc:
            failures.append({'part': 'pose_channels', 'bone': name, 'message': str(exc)})
    try:
        if _pose_state(rig) != original_pose:
            raise RuntimeError('Complete original raw pose channel restoration differs')
        if motion.clip_state(original_action) != original_state:
            raise RuntimeError('Original Action content/users restoration differs')
        _one_rig_user(original_action, rig)
        _copy_safe_metadata(original_action)
    except Exception as exc:
        failures.append({'part': 'original_pose_action', 'message': str(exc)})
    if created is not None:
        try:
            _no_users(created)
            if created == original_action or created.as_pointer() in original_actions:
                raise RuntimeError('Refusing to remove an Action not owned by this derivation')
            bpy.data.actions.remove(created, do_unlink=False)
        except Exception as exc:
            failures.append({'part': 'owned_copy_cleanup', 'message': str(exc)})
    try:
        if {action.as_pointer() for action in bpy.data.actions} != original_actions:
            raise RuntimeError('Action inventory restoration differs; unknown IDs were not deleted')
    except Exception as exc:
        failures.append({'part': 'action_inventory', 'message': str(exc)})
    try:
        if context.scene.frame_current != 0 or context.scene.frame_subframe != 0:
            raise RuntimeError('Original frame context differs after restoration')
        if _cloth_state(context) != original_cloth:
            raise RuntimeError('Original Cloth/cache authoring differs; no unknown authoring changes were overwritten')
    except Exception as exc:
        failures.append({'part': 'cloth_authoring_or_context', 'message': str(exc)})
    return {'ok': not failures, 'failures': failures}


def derive_clip(context, rig, request):
    created, original_action, snapshot, raw_animation, inventory = None, None, None, None, None
    begun = False
    try:
        req = contract.canonical_request(request)
        observed = inspect_derivation(context, rig, [row['bone'] for row in req['tracks']], req['action_name'], req['base_clip'])
        if not observed.get('ok'):
            return observed
        if observed['snapshot_sha256'] != req['expected_snapshot_sha256']:
            _fail('SNAPSHOT_CONFLICT', 'Native derivation inputs changed; original Action remains bound')
        snapshot = observed['snapshot']
        for row in req['tracks']:
            field = observed['fields'][row['bone']]
            if field['rotation_mode'] != row['rotation_mode'] or field['value'] != row['before']:
                _fail('DERIVATION_BEFORE_CONFLICT', 'New track before/mode differs from original native channels', {'bone': row['bone']})
        original_action = rig.animation_data.action
        raw_animation = motion._raw_ad(rig.animation_data)
        inventory = {action.as_pointer() for action in bpy.data.actions}
        begun = True
        created = original_action.copy()
        _mutation_seam('after_action_copy', rig, created)
        if created == original_action or created.as_pointer() in inventory:
            _fail('DERIVATION_COPY', 'Native copy did not produce a distinct owned Action')
        _no_users(created)
        created.name = req['action_name']
        if created.name != req['action_name']:
            _fail('DERIVATION_NAME_CONFLICT', 'Blender changed the explicit new Action name')
        if motion.clip_state(created) != _renamed_state(snapshot['original_action'], req['action_name'], 0):
            _fail('DERIVATION_COPY', 'Native Action copy did not exactly retain source slot/layer/curves/settings')
        slot, bag = _bag(created)
        first = True
        for row in req['tracks']:
            path = rig.pose.bones[row['bone']].path_from_id(motion._field(row['rotation_mode']))
            for index in range(len(row['before'])):
                curve = bag.fcurves.new(data_path=path, index=index)
                curve.extrapolation = 'CONSTANT'
                curve.keyframe_points.add(len(row['keys']))
                curve.keyframe_points.foreach_set('co', [value for key in row['keys'] for value in (key['frame'], key['value'][index])])
                for point in curve.keyframe_points:
                    point.interpolation = 'LINEAR'
                curve.update()
                if first:
                    first = False
                    _mutation_seam('after_first_curve', rig, created)
        ad = rig.animation_data
        ad.action = created
        ad.action_slot = slot
        _mutation_seam('after_bind', rig, created)
        result = verify_derivation(context, rig, req, snapshot)
        if not result.get('ok'):
            _fail('DERIVATION_POSTCONDITION', 'Native copy/add/binding verification failed', {'verification': result})
        if context.scene.frame_current != 0 or context.scene.frame_subframe != 0:
            _fail('DERIVATION_FRAME_CONTEXT', 'Derivation changed original frame context')
        if {action.as_pointer() for action in bpy.data.actions} != inventory | {created.as_pointer()}:
            _fail('DERIVATION_INVENTORY', 'Action inventory changed beyond the one explicit copy')
        result['changed'] = True
        return result
    except Exception as exc:
        result = _error(exc)
        if begun:
            result['rollback'] = _restore(context, rig, created, raw_animation, snapshot['animation'], original_action,
                snapshot['original_action'], snapshot['all_pose_channels'], inventory, snapshot['cloth_authoring'])
            if not result['rollback']['ok']:
                result['changed'] = True
        return result
