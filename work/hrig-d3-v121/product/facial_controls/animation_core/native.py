"""Independent author transactions; never insert keys into AtomicControls.pending.

The input is a fixed role roster on a shared frame grid. Transform values are
offsets from the inspected original input, except scale_set/mode_enum. The
transaction copies the original Action before replacing exact target curves.
It does not invoke keyframe_insert or temporarily bind a half-authored Action.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import struct
import uuid

import bpy
from mathutils import Quaternion

import character_state as C
import inspection as I
import native_control as S1
from ..control_core.native import AtomicControls, CHANNELS, get
from ..control_core.contract import ControlError, require, digest
from ..control_core import outputs as O


SCHEMA = 'hrig.facial-control-animation.v3'


def _f32(value):
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


def _identity(item):
    return I.identity(item) if item is not None else None


def _block_key(block):
    return digest({name: block[name] for name in ('type', 'name', 'library', 'owner') if name in block})


def _set(tree, path, value):
    for key in path[:-1]:
        tree = tree[key]
    tree[path[-1]] = deepcopy(value)


def _action_definition(action):
    # users is checked by the complete character snapshot, not discarded here.
    return {'record': I.record(action), 'extra': C.extras(action)}


def _action_shape(action, slot):
    require(action.library is None and not action.is_library_indirect, 'ACTION_NOT_LOCAL')
    require(not action.asset_data, 'ACTION_ASSET_REQUIRES_SEPARATE_COPY')
    require(slot is not None and len(action.slots) == 1 and slot == action.slots[0]
            and slot.target_id_type == 'OBJECT', 'ACTION_SLOT_UNSUPPORTED')
    require(len(action.layers) == 1 and len(action.layers[0].strips) == 1,
            'ACTION_LAYERS_UNSUPPORTED')
    strip = action.layers[0].strips[0]
    require(strip.type == 'KEYFRAME' and len(strip.channelbags) <= 1, 'ACTION_STRIP_UNSUPPORTED')
    bag = strip.channelbag(slot)
    require(not strip.channelbags or bag is not None, 'ACTION_BAG_UNSUPPORTED')
    if bag is not None:
        keys = [(fc.data_path, fc.array_index) for fc in bag.fcurves]
        require(len(keys) == len(set(keys)), 'DUPLICATE_ACTION_CURVE')
    return bag


def _curve_record(fc):
    return {'curve': I.curve_content(fc), 'keyfields': C.curve_keyframes(fc),
            'settings': I.properties(fc)}


def _binding(rig):
    ad = rig.animation_data
    if ad is None:
        return {'exists': False, 'action': None, 'slot': None, 'settings': None}
    require(not ad.use_tweak_mode, 'NLA_TWEAK_MODE')
    return {'exists': True, 'action': ad.action, 'slot': ad.action_slot,
            'settings': {key: getattr(ad, key) for key in
                         ('action_blend_type', 'action_influence', 'action_extrapolation', 'use_nla')}}


def _binding_view(binding):
    return {'exists': binding['exists'], 'action': _identity(binding['action']),
            'slot': C.slot_reference(binding['slot']), 'settings': binding['settings']}


class AnimationTransaction:
    """In-process inspect -> author -> undo, with a source-bound native profile.

    A preview_core, when supplied, must be the caller's existing AtomicControls
    instance and must have no pending preview. Passing it makes the boundary
    check explicit; this module never changes that preview core's guards.
    """
    def __init__(self, profile, source_identity, preview_core=None):
        require(type(source_identity) is dict and set(source_identity) == {'file', 'expected_sha256'},
                'SOURCE_IDENTITY_SCHEMA')
        path, expected = source_identity['file'], source_identity['expected_sha256']
        require(type(path) is str and Path(path).is_absolute(), 'SOURCE_PATH')
        require(type(expected) is str and len(expected) == 64
                and all(c in '0123456789abcdef' for c in expected), 'SOURCE_SHA')
        require(Path(bpy.data.filepath).resolve() == Path(path).resolve(), 'SOURCE_MAIN_MISMATCH')
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected, 'SOURCE_SHA_MISMATCH')
        self.core = AtomicControls(profile)
        self.profile = self.core.profile
        self.rig = self.core.rig
        self.source = deepcopy(source_identity)
        self.preview_core = preview_core
        self.snapshot = None
        self.pending = None
        self.write_count = 0
        self._guard_preview()

    def _guard_preview(self):
        require(self.preview_core is None or
                (self.preview_core.rig == self.rig and self.preview_core.pending is None),
                'PREVIEW_MUST_BE_RESTORED')
        self.core._context_guard()

    def _plan(self, clip):
        self._guard_preview()
        # Revalidate the public binding at each new inspection; the original
        # core's construction is not a permanent rest/consumer identity lease.
        require(AtomicControls(self.profile).rig == self.rig, 'RIG_IDENTITY_CHANGED')
        require(type(clip) is dict and set(clip) == {'schema', 'name', 'replace_targets', 'keys'},
                'CLIP_SCHEMA')
        require(clip['schema'] == SCHEMA and type(clip['name']) is str
                and 0 < len(clip['name']) <= 63 and '\x00' not in clip['name'], 'CLIP_NAME')
        require(type(clip['replace_targets']) is bool, 'REPLACE_POLICY')
        require(clip['name'] not in bpy.data.actions, 'ACTION_NAME_EXISTS')
        rows = clip['keys']
        require(type(rows) is list and 2 <= len(rows) <= 256, 'KEY_GRID')
        role_map = {role['role_id']: role for role in self.profile['roles']}
        roster = None
        previous = -math.inf
        previous_native = -math.inf
        resolved = []
        for row in rows:
            require(type(row) is dict and set(row) == {'frame', 'operations'}, 'KEY_SCHEMA')
            frame = row['frame']
            require(type(frame) in (int, float) and math.isfinite(frame) and -1048574 <= frame <= 1048574
                    and frame > previous and _f32(frame) > previous_native, 'KEY_FRAME')
            previous = frame
            previous_native = _f32(frame)
            operations = row['operations']
            require(type(operations) is list and 1 <= len(operations) <= 128, 'KEY_OPERATIONS')
            names, values = [], []
            for operation in operations:
                require(type(operation) is dict and set(operation) == {'role', 'value'}, 'OPERATION_SCHEMA')
                role_id, value = operation['role'], operation['value']
                require(type(role_id) is str and role_id in role_map and role_id not in names, 'ROLE_SCHEMA')
                role = role_map[role_id]
                require(type(value) in (int, float) and math.isfinite(value)
                        and role['bounds'][0] <= value <= role['bounds'][1], 'INPUT_BOUNDS', role_id)
                if role['kind'] == 'mode_enum':
                    require(value in (0, 1), 'MODE_ENUM')
                names.append(role_id)
                values.append(float(value))
            if roster is None:
                roster = names
            require(names == roster, 'KEY_ROLE_GRID_MISMATCH')
            resolved.append({'frame': float(frame), 'values': values})
        roles = [deepcopy(role_map[name]) for name in roster]
        transform_keys = set()
        paths, scan_paths, baselines = set(), set(), {}
        mode_roles = {}
        for role in roles:
            bone = self.rig.pose.bones.get(role['bone'])
            require(bone is not None and bone.rotation_mode == role['rotation_mode']
                    and (bone.parent.name if bone.parent else None) == role['parent'],
                    'BONE_BINDING_DRIFT', role['role_id'])
            if role['kind'] == 'mode_enum':
                prop = role['property']
                require(prop in bone and type(bone[prop]) in (int, float), 'PROPERTY_SCHEMA')
                path = self.core._prop_path(bone.name, prop)
                key = (path, 0)
                require(key not in paths, 'OVERLAPPING_INPUT', key)
                paths.add(key)
                baselines[key] = bone[prop]
                mode_roles[(bone.name, prop)] = roles.index(role)
                continue
            channel = CHANNELS[role['kind']]
            if channel == 'rotation_quaternion':
                require(bone.rotation_mode == 'QUATERNION' and not any(bone.lock_rotation)
                        and not bone.lock_rotation_w, 'COMPONENT_LOCKED', role['role_id'])
                # Multiple axes are intentionally composed in fixed roster order.
                # Mixed quaternion and other rotation representations are rejected.
                key = (bone.name, channel, tuple(role['axis']))
                require(key not in transform_keys, 'OVERLAPPING_INPUT', key)
                transform_keys.add(key)
                path = bone.path_from_id(channel)
                for index in range(4):
                    paths.add((path, index))
                    baselines[(path, index)] = float(bone.rotation_quaternion[index])
            else:
                index = role['index']
                if channel == 'location':
                    require(not bone.bone.use_connect, 'CONNECTED_LOCATION', role['role_id'])
                if channel == 'rotation_euler':
                    require(bone.rotation_mode in ('XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'),
                            'ROTATION_REPRESENTATION')
                locks = bone.lock_location if channel == 'location' else bone.lock_scale if channel == 'scale' else bone.lock_rotation
                require(not locks[index], 'COMPONENT_LOCKED', role['role_id'])
                key = (bone.path_from_id(channel), index)
                require(key not in paths, 'OVERLAPPING_INPUT', key)
                paths.add(key)
                baselines[key] = float(getattr(bone, channel)[index])
            scan_paths.update((bone.path_from_id('matrix_basis'), index) for index in range(16))
        scan_paths.update(paths)
        for role in roles:
            for guard in role['requires']:
                bone = self.rig.pose.bones.get(guard['bone'])
                require(bone is not None and guard['property'] in bone, 'MODE_ASSERTION')
                guard_key = (bone.name, guard['property'])
                scan_paths.add((self.core._prop_path(*guard_key), 0))
                for row in resolved:
                    actual = row['values'][mode_roles[guard_key]] if guard_key in mode_roles else bone[guard['property']]
                    require(type(actual) in (int, float) and actual == guard['value'], 'MODE_ASSERTION', guard)
        binding = _binding(self.rig)
        action, slot = binding['action'], binding['slot']
        if action is not None:
            _action_shape(action, slot)
            require(binding['settings']['action_blend_type'] == 'REPLACE'
                    and binding['settings']['action_influence'] == 1.0, 'ACTION_BLEND_UNSUPPORTED')
        scan = S1.writer_scan(self.rig, scan_paths)
        # writer_scan records owner names; rig and Armature may share a name.
        # Check the data owner directly before admitting rig active-action rows.
        data_ad = self.rig.data.animation_data
        if data_ad is not None:
            data_curves = list(data_ad.drivers) + S1.curves(data_ad.action, data_ad.action_slot)
            require(not any((fc.data_path, fc.array_index) in scan_paths for fc in data_curves),
                    'ARMATURE_DATA_WRITER')
        rejected = []
        for writer in scan['writers']:
            if writer['kind'] == 'ACTION_constraint':
                continue
            allowed_copy = (writer['kind'] == 'active_action' and writer['owner'] == self.rig.name
                            and (writer['path'], writer['index']) in paths and clip['replace_targets'])
            if not allowed_copy:
                rejected.append(writer)
        require(not rejected, 'WRITER_CONFLICT', rejected)
        curves = []
        quaternion_bones = {role['bone'] for role in roles if role['kind'] == 'quaternion_delta'}
        for path, index in sorted(paths):
            is_mode = any(role['kind'] == 'mode_enum' and self.core._prop_path(role['bone'], role['property']) == path
                          for role in roles)
            curves.append({'path': path, 'index': index, 'interpolation': 'CONSTANT' if is_mode else 'LINEAR', 'keys': []})
        curve_map = {(row['path'], row['index']): row for row in curves}
        for row in resolved:
            quaternions = {bone: Quaternion(self.rig.pose.bones[bone].rotation_quaternion) for bone in quaternion_bones}
            for role, value in zip(roles, row['values']):
                bone = self.rig.pose.bones[role['bone']]
                if role['kind'] == 'quaternion_delta':
                    quaternions[bone.name] = quaternions[bone.name] @ Quaternion(role['axis'], value)
                    quaternions[bone.name].normalize()
                    continue
                if role['kind'] == 'mode_enum':
                    path, index = self.core._prop_path(bone.name, role['property']), 0
                    wanted = type(bone[role['property']])(value)
                else:
                    path, index = bone.path_from_id(CHANNELS[role['kind']]), role['index']
                    wanted = value if role['kind'] == 'scale_set' else baselines[(path, index)] + value
                curve_map[(path, index)]['keys'].append([row['frame'], _f32(wanted)])
            for bone, quaternion in quaternions.items():
                path = self.rig.pose.bones[bone].path_from_id('rotation_quaternion')
                for index in range(4):
                    curve_map[(path, index)]['keys'].append([row['frame'], _f32(quaternion[index])])
        return {'clip': deepcopy(clip), 'roles': roles, 'curves': curves, 'paths': paths,
                'binding': binding, 'scan': scan, 'baselines': baselines}

    def inspect(self, clip):
        require(self.pending is None, 'AUTHOR_ALREADY_PENDING')
        plan = self._plan(clip)
        bpy.context.view_layer.update()
        self.core.observe()  # ensure lazy native evaluation precedes capture
        before = C.capture()
        token = 'anim_snap_' + uuid.uuid4().hex
        self.snapshot = {'token': token, 'plan': plan, 'before': before,
                         'context': S1.context(), 'outputs': O.declare(before, self.core.consumer_rigs, self.core.output_keys),
                         'actions': {a.name: _action_definition(a) for a in bpy.data.actions}}
        return deepcopy({'status': 'inspected', 'snapshot_token': token,
                         'source': self.source, 'profile_sha256': digest(self.profile),
                         'author_sha256': C.digest(before), 'binding': _binding_view(plan['binding']),
                         'writer_scan': plan['scan'], 'curves': plan['curves'], 'writes': self.write_count})

    def _make_action(self, plan):
        original = plan['binding']['action']
        action = None
        try:
            if original is not None:
                action = original.copy()
                action.name = plan['clip']['name']
                slot = next(s for s in action.slots if s.identifier == plan['binding']['slot'].identifier)
            else:
                action = bpy.data.actions.new(plan['clip']['name'])
                slot = action.slots.new(id_type='OBJECT', name=self.rig.name)
                action.layers.new('HRIG Control Animation').strips.new(type='KEYFRAME')
            require(action.name == plan['clip']['name'], 'ACTION_NAME_CHANGED')
            action.use_fake_user = True
            return action, slot
        except Exception:
            if action is not None and action.users == int(action.use_fake_user):
                bpy.data.actions.remove(action)
            raise

    def _write_curves(self, action, slot, plan):
        bag = action.layers[0].strips[0].channelbag(slot, ensure=True)
        preserved = {(fc.data_path, fc.array_index): _curve_record(fc) for fc in bag.fcurves
                     if (fc.data_path, fc.array_index) not in plan['paths']}
        for curve in plan['curves']:
            old = bag.fcurves.find(curve['path'], index=curve['index'])
            if old is not None:
                require(plan['clip']['replace_targets'], 'TARGET_CURVE_EXISTS')
                bag.fcurves.remove(old)
            fc = bag.fcurves.new(curve['path'], index=curve['index'])
            fc.extrapolation = 'CONSTANT'
            fc.keyframe_points.add(len(curve['keys']))
            for point, (frame, value) in zip(fc.keyframe_points, curve['keys']):
                point.co = frame, value
                point.interpolation = curve['interpolation']
                point.handle_left_type = point.handle_right_type = 'VECTOR'
            fc.update()
            self.write_count += 1
        actual = {(fc.data_path, fc.array_index): _curve_record(fc) for fc in bag.fcurves
                  if (fc.data_path, fc.array_index) not in plan['paths']}
        require(actual == preserved, 'UNRELATED_CURVE_CHANGED')
        self._verify_target_curves(action, slot, plan)

    def _verify_target_curves(self, action, slot, plan):
        bag = _action_shape(action, slot)
        require(bag is not None, 'ACTION_BAG_MISSING')
        for curve in plan['curves']:
            fc = bag.fcurves.find(curve['path'], index=curve['index'])
            require(fc is not None and not fc.modifiers and not fc.sampled_points and not fc.mute
                    and len(fc.keyframe_points) == len(curve['keys']) and fc.extrapolation == 'CONSTANT',
                    'TARGET_CURVE_DRIFT')
            for point, pair in zip(fc.keyframe_points, curve['keys']):
                require(list(point.co) == [_f32(pair[0]), pair[1]]
                        and point.interpolation == curve['interpolation'], 'TARGET_KEY_DRIFT')

    def _check_original_actions(self):
        for name, original in self.snapshot['actions'].items():
            action = bpy.data.actions.get(name)
            require(action is not None and _action_definition(action) == original, 'ORIGINAL_ACTION_CHANGED', name)

    def _protected_capture(self, after, pending):
        """Exact expected snapshot, changing only declared authorship/output leaves.

        The newly created Action is independently verified, then admitted as one
        complete owned block. Original IDs, users and edges are not masked.
        """
        before = self.snapshot['before']
        expected = deepcopy(before)
        plan = pending['plan']
        action, slot = pending['action'], pending['slot']
        before_map = {_block_key(b): i for i, b in enumerate(before['blocks'])}
        after_map = {_block_key(b): b for b in after['blocks']}
        new_key = digest(_identity(action))
        require(set(after_map) == set(before_map) | {new_key}, 'ID_ROSTER_CHANGED')
        rig_index = next(i for i, b in enumerate(before['blocks'])
                         if b['type'] == 'Object' and b['name'] == self.rig.name and b['library'] is None)
        actual_rig = after_map[_block_key(before['blocks'][rig_index])]
        expected_rig = expected['blocks'][rig_index]
        if plan['binding']['exists']:
            expected_rig['animation']['action'] = _identity(action)
            expected_rig['animation']['slot'] = slot.identifier
            expected_rig['character_extra']['animation_fields']['action_slot'] = C.slot_reference(slot)
        else:
            # This newly-created AnimData must have exactly the intended action,
            # slot and no driver/NLA payload. No arbitrary prior data is admitted.
            require(not actual_rig['animation']['drivers'] and not actual_rig['animation']['nla'], 'NEW_ANIMDATA_PAYLOAD')
            expected_rig['animation'] = deepcopy(actual_rig['animation'])
            expected_rig['character_extra']['animation_fields'] = deepcopy(actual_rig['character_extra']['animation_fields'])
        if pending['archive_key'] is not None:
            archive = pending['archive_key']
            require(self.rig.get(archive) == plan['binding']['action'], 'ACTION_ARCHIVE_DRIFT')
            expected_rig['custom_properties'][archive] = _identity(plan['binding']['action'])
        allowed = []
        for role in plan['roles']:
            if role['kind'] == 'mode_enum':
                allowed += self.core._property_paths(before, role['bone'], role['property'])
            else:
                channel = CHANNELS[role['kind']]
                indices = range(4) if channel == 'rotation_quaternion' else (role['index'],)
                for index in indices:
                    allowed += self.core._pose_paths(before, role['bone'], channel, index)
                allowed += self.core._matrix_paths(before, role['bone'])
        for output in self.snapshot['outputs']:
            if O.source_values(output['sources']) != output['source_before']:
                require(output['fc'].driver.is_valid, 'DERIVED_DRIVER_INVALID')
                allowed += output['paths']
        aligned_after = deepcopy(after)
        aligned_after['blocks'] = [after_map[_block_key(b)] for b in before['blocks']]
        for path in set(allowed):
            require(get(aligned_after, path) is not None, 'OBSERVATION_PATH_MISSING', path)
            _set(expected, path, get(aligned_after, path))
        expected['blocks'].append(deepcopy(after_map[new_key]))
        expected['blocks'].sort(key=lambda b: I.key({k: b[k] for k in ('type', 'name', 'library', 'owner') if k in b}))
        new_edge = {'user': _identity(self.rig), 'target': _identity(action)}
        require(new_edge not in before['edges'], 'ACTION_EDGE_PREEXISTS')
        expected['edges'] = sorted(before['edges'] + [new_edge], key=I.key)
        require(after == expected, 'AUTHOR_PROTECTION_CHANGED', C.diff(expected, after, limit=30))
        return {'full_expected_snapshot_exact': True, 'allowed_native_leaf_paths': [list(p) for p in set(allowed)],
                'archive_key': pending['archive_key'], 'new_action': _identity(action),
                'original_action_retained': plan['binding']['action'] is None or self.rig.get(pending['archive_key']) == plan['binding']['action']}

    def author(self, snapshot_token, fault=None):
        require(fault in (None, 'after_curves', 'after_bind'), 'FAULT_SCHEMA')
        require(self.pending is None and self.snapshot is not None
                and snapshot_token == self.snapshot['token'], 'STALE_ANIMATION_SNAPSHOT')
        self._guard_preview()
        snapshot = self.snapshot
        require(S1.context() == snapshot['context'], 'CONTEXT_CHANGED')
        self.core.observe()
        require(C.capture() == snapshot['before'], 'LIVE_STATE_CHANGED')
        # Repeat public preflight against current bindings, without relaxing any
        # legacy preview guard or mutating the old active Action.
        plan = self._plan(snapshot['plan']['clip'])
        require(plan['curves'] == snapshot['plan']['curves'], 'PLAN_CHANGED')
        action = None
        try:
            action, slot = self._make_action(plan)
            archive = ('HRIG_C1_SOURCE_ACTION_' + uuid.uuid4().hex) if plan['binding']['action'] else None
            self.pending = {'token': 'anim_undo_' + uuid.uuid4().hex, 'plan': plan, 'action': action, 'slot': slot,
                            'archive_key': archive, 'frame': snapshot['context']['frame'],
                            'baselines': deepcopy(plan['baselines']), 'bound': False}
            self._write_curves(action, slot, plan)
            if fault == 'after_curves':
                raise ControlError('INJECTED_AFTER_CURVES')
            if archive:
                require(archive not in self.rig, 'ARCHIVE_KEY_COLLISION')
                self.rig[archive] = plan['binding']['action']
            ad = self.rig.animation_data_create()
            ad.action = action
            ad.action_slot = slot
            if not plan['binding']['exists'] or plan['binding']['action'] is None:
                ad.action_blend_type = 'REPLACE'
                ad.action_influence = 1.0
                ad.action_extrapolation = 'HOLD'
            self.pending['bound'] = True
            self._set_frame(snapshot['context']['frame'])
            if fault == 'after_bind':
                raise ControlError('INJECTED_AFTER_BIND')
            self._check_original_actions()
            self._verify_target_curves(action, slot, plan)
            self._verify_inputs(action, slot, plan)
            self.core.observe()
            after = C.capture()
            protection = self._protected_capture(after, self.pending)
            require(S1.context() == snapshot['context'], 'CONTEXT_CHANGED')
            self.pending['author_after'] = after
            return deepcopy({'status': 'authored', 'undo_token': self.pending['token'], 'action': _identity(action),
                             'slot': C.slot_reference(slot), 'source': self.source, 'curves': plan['curves'],
                             'archive_key': archive, 'protection': protection,
                             'author_after_sha256': C.digest(after), 'writes': self.write_count})
        except Exception as error:
            if self.pending is not None:
                rollback = self._restore()
            elif action is not None:
                # Only the Action made by this call can be removed.
                bpy.data.actions.remove(action)
                rollback = {'restored': True, 'before_bind': True}
            else:
                rollback = {'restored': True, 'before_creation': True}
            if isinstance(error, ControlError):
                error.detail = {'cause': error.detail, 'rollback': rollback}
            raise

    def _set_frame(self, frame):
        whole = math.floor(frame)
        bpy.context.scene.frame_set(whole, subframe=frame - whole)
        self.rig.update_tag(refresh={'OBJECT', 'DATA'})
        bpy.context.view_layer.update()

    def _verify_inputs(self, action, slot, plan):
        bag = action.layers[0].strips[0].channelbag(slot)
        frame = bpy.context.scene.frame_current_final
        for curve in plan['curves']:
            fc = bag.fcurves.find(curve['path'], index=curve['index'])
            value = self.rig.path_resolve(curve['path'])
            actual = value if type(value) in (int, float) else value[curve['index']]
            expected = _f32(fc.evaluate(frame))
            if curve['interpolation'] == 'CONSTANT' and type(value) is int:
                expected = int(expected)
            require(actual == expected, 'ANIMATED_INPUT_READBACK', {'path': curve['path'], 'index': curve['index'], 'actual': actual, 'expected': expected})

    def _restore(self):
        pending = self.pending
        require(pending is not None, 'UNDO_NOT_PENDING')
        binding = pending['plan']['binding']
        ad = self.rig.animation_data
        if binding['exists']:
            require(ad is not None, 'ANIMDATA_DISAPPEARED')
            ad.action = binding['action']
            if binding['slot'] is not None:
                ad.action_slot = binding['slot']
            for key, value in binding['settings'].items():
                setattr(ad, key, value)
        elif ad is not None:
            self.rig.animation_data_clear()
        archive = pending['archive_key']
        if archive is not None and archive in self.rig:
            require(self.rig[archive] == binding['action'], 'ACTION_ARCHIVE_DRIFT')
            del self.rig[archive]
        # Newly animated channels may not have had original Action writers.
        # Restore those exact original scalars before reevaluating original time.
        for (path, index), value in pending['baselines'].items():
            roles = pending['plan']['roles']
            mode = next((r for r in roles if r['kind'] == 'mode_enum'
                         and self.core._prop_path(r['bone'], r['property']) == path), None)
            if mode is not None:
                self.rig.pose.bones[mode['bone']][mode['property']] = value
            else:
                self.rig.path_resolve(path)[index] = value
        self._set_frame(pending['frame'])
        generated = pending['action']
        require(generated.users == int(generated.use_fake_user), 'GENERATED_ACTION_HAS_EXTERNAL_USERS')
        bpy.data.actions.remove(generated)
        self.core.observe()
        self._check_original_actions()
        actual = C.capture()
        require(actual == self.snapshot['before'], 'ANIMATION_ROLLBACK_INCOMPLETE', C.diff(self.snapshot['before'], actual, limit=30))
        require(S1.context() == self.snapshot['context'], 'ANIMATION_CONTEXT_RESTORE_INCOMPLETE')
        self.pending = None
        self.snapshot = None
        return {'restored': True, 'full_declared_author_exact': True, 'context_exact': True,
                'author_sha256': C.digest(actual)}

    def undo(self, undo_token):
        require(self.pending is not None and undo_token == self.pending['token'], 'UNDO_TOKEN')
        self._guard_preview()
        pending = self.pending
        require(self.rig.animation_data is not None and self.rig.animation_data.action == pending['action']
                and self.rig.animation_data.action_slot == pending['slot'], 'ACTION_BINDING_DRIFT')
        self._check_original_actions()
        self._verify_target_curves(pending['action'], pending['slot'], pending['plan'])
        # Return to the author frame before undo. An interactive caller should
        # explicitly seek there; no unreviewed frame jump is hidden by this API.
        require(bpy.context.scene.frame_current_final == pending['frame'], 'UNDO_REQUIRES_AUTHOR_FRAME', pending['frame'])
        self.core.observe()
        require(C.capture() == pending['author_after'], 'LIVE_AUTHOR_CHANGED_BEFORE_UNDO')
        return self._restore()
