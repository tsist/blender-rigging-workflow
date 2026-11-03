"""Atomic native input batches, preserving the original rigs and consumers."""
from copy import deepcopy
import json, struct, uuid
import bpy, numpy as np
from mathutils import Quaternion
import character_state as C
import native_control as S1
from .contract import ControlError, require, validate, validate_operations, digest
from . import outputs as O

CHANNELS = {'location_delta': 'location', 'euler_delta': 'rotation_euler', 'quaternion_delta': 'rotation_quaternion', 'scale_set': 'scale'}


def get(tree, path):
    try:
        for key in path: tree = tree[key]
        return tree
    except (KeyError, IndexError, TypeError): return None


def rest_signature(rig):
    return digest([{'name': bone.name, 'parent': None if bone.parent is None else bone.parent.name,
                    'matrix': [list(row) for row in bone.matrix_local], 'head': list(bone.head_local),
                    'tail': list(bone.tail_local), 'connected': bone.use_connect, 'deform': bone.use_deform}
                   for bone in sorted(rig.data.bones, key=lambda item: item.name)])


class AtomicControls:
    def __init__(self, profile):
        self.profile = validate(profile)
        runtime = self.profile['runtime']
        require(tuple(bpy.app.version) == tuple(runtime['blender_version']) and bpy.app.build_hash.decode() == runtime['build_hash'], 'RUNTIME_MISMATCH')
        self.rig = bpy.data.objects.get(self.profile['rig']['object'])
        require(self.rig is not None and self.rig.type == 'ARMATURE' and self.rig.library is None and self.rig.data.library is None, 'RIG_IDENTITY')
        require(self.rig.data.name == self.profile['rig']['data'], 'RIG_DATA_IDENTITY')
        require(self.rig.data.users == 1 + int(self.rig.data.use_fake_user) and [item for item in bpy.data.objects if item.type == 'ARMATURE' and item.data == self.rig.data] == [self.rig], 'SHARED_ARMATURE')
        require(sorted(self.rig.pose.bones.keys()) == sorted(self.profile['rig']['witness_bones']), 'WITNESS_ROSTER_DRIFT')
        require(rest_signature(self.rig) == self.profile['rig']['rest_sha256'], 'REST_BINDING_DRIFT')
        require(C.SCHEMA == 'hrig.character-storage.v1', 'OBSERVER_VERSION')
        self.consumer_rigs = [self.rig]
        for owner in self.profile['rig']['consumer_rigs']:
            obj = bpy.data.objects.get(owner['object'])
            require(obj is not None and obj.type == 'ARMATURE' and obj.library is None and obj.data.library is None and obj.data.name == owner['data'] and rest_signature(obj) == owner['rest_sha256'], 'CONSUMER_RIG_IDENTITY')
            require(obj not in self.consumer_rigs, 'DUPLICATE_CONSUMER')
            self.consumer_rigs.append(obj)
        self.output_keys = []
        for owner in self.profile['rig']['output_keys']:
            obj = bpy.data.objects.get(owner['object'])
            require(obj is not None and obj.type == 'MESH' and obj.library is None and obj.data.library is None and obj.data.name == owner['data'], 'OUTPUT_OWNER_IDENTITY')
            key = obj.data.shape_keys
            require(key is not None and key.name == owner['key'] and key.library is None and C.key_owner(key) == obj.data, 'OUTPUT_KEY_IDENTITY')
            reads_rig = any(target.id in self.consumer_rigs for fc in key.animation_data.drivers for variable in fc.driver.variables for target in variable.targets) if key.animation_data else False
            require(any(mod.type == 'ARMATURE' and mod.object == self.rig for mod in obj.modifiers) or reads_rig, 'OUTPUT_RIG_BINDING')
            if key not in self.output_keys: self.output_keys.append(key)
        self.pending = self.snapshot = None
        self.write_count = 0
        self._context_guard()

    def _context_guard(self):
        runtime = self.profile['runtime']
        require(bpy.context.scene.name == runtime['scene'] and bpy.context.view_layer.name == runtime['view_layer'], 'CONTEXT_MISMATCH')
        require(bpy.context.mode in ('OBJECT', 'POSE'), 'CONTEXT_MODE')
        require(bpy.context.view_layer.objects.get(self.rig.name) == self.rig, 'RIG_NOT_IN_LAYER')
        require(self.rig.data.pose_position == 'POSE', 'REST_POSE')

    def observe(self):
        evaluated = self.rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        answer = {}
        for name in self.profile['rig']['witness_bones']:
            matrix = np.asarray(evaluated.matrix_world @ evaluated.pose.bones[name].matrix, dtype='<f4')
            require(np.isfinite(matrix).all(), 'NONFINITE_WITNESS', name)
            answer[name] = matrix.tolist()
        return answer

    def _prop_path(self, bone, property):
        return self.rig.pose.bones[bone].path_from_id() + '[' + json.dumps(property, ensure_ascii=False) + ']'

    def preflight(self, operations):
        self._context_guard()
        require(rest_signature(self.rig) == self.profile['rig']['rest_sha256'], 'REST_BINDING_DRIFT')
        for owner in self.profile['rig']['consumer_rigs']:
            obj=bpy.data.objects.get(owner['object'])
            require(obj in self.consumer_rigs and obj.type=='ARMATURE' and obj.library is None and obj.data.library is None and obj.data.name==owner['data'] and rest_signature(obj)==owner['rest_sha256'], 'CONSUMER_RIG_DRIFT')
        for owner in self.profile['rig']['output_keys']:
            obj=bpy.data.objects.get(owner['object'])
            require(obj is not None and obj.type=='MESH' and obj.library is None and obj.data.library is None and obj.data.name==owner['data'] and obj.data.shape_keys in self.output_keys and obj.data.shape_keys.name==owner['key'], 'OUTPUT_OWNER_DRIFT')
        plan = validate_operations(self.profile, operations)
        paths = set(); projected_properties = {}
        for role, value in plan:
            bone = self.rig.pose.bones.get(role['bone'])
            require(bone is not None and (None if bone.parent is None else bone.parent.name) == role['parent'] and bone.rotation_mode == role['rotation_mode'], 'BONE_BINDING_DRIFT', role['role_id'])
            if role['kind'] == 'mode_enum':
                require(role['property'] in bone and type(bone[role['property']]) in (int, float), 'PROPERTY_SCHEMA')
                path = self._prop_path(bone.name, role['property']); paths.add((path, 0))
                projected_properties[(bone.name, role['property'])] = value
            else:
                channel = CHANNELS[role['kind']]
                if channel == 'rotation_quaternion':
                    require(bone.rotation_mode == 'QUATERNION', 'ROTATION_REPRESENTATION')
                    require(not any(bone.lock_rotation) and not bone.lock_rotation_w, 'COMPONENT_LOCKED', role['role_id'])
                    indices = range(4)
                else:
                    if channel == 'location': require(not bone.bone.use_connect, 'CONNECTED_LOCATION', role['role_id'])
                    if channel == 'rotation_euler': require(bone.rotation_mode in ('XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'), 'ROTATION_REPRESENTATION')
                    locks = bone.lock_location if channel == 'location' else bone.lock_scale if channel == 'scale' else bone.lock_rotation
                    require(not locks[role['index']], 'COMPONENT_LOCKED', role['role_id']); indices = (role['index'],)
                paths.update((bone.path_from_id(channel), index) for index in indices)
                paths.update((bone.path_from_id('matrix_basis'), index) for index in range(16))
        guards = []
        for role, value in plan:
            for guard in role['requires']:
                bone = self.rig.pose.bones.get(guard['bone'])
                require(bone is not None and guard['property'] in bone, 'MODE_ASSERTION')
                actual = projected_properties.get((bone.name, guard['property']), bone[guard['property']])
                require(type(actual) in (int, float) and actual == guard['value'], 'MODE_ASSERTION', guard)
                paths.add((self._prop_path(bone.name, guard['property']), 0)); guards.append(guard)
        scan = S1.writer_scan(self.rig, paths)
        # ACTION constraints compose an evaluated owner transform. They do not
        # assign the original pose RNA channel (unlike action/NLA animation).
        # Preserve them and inspect actual native response instead of treating
        # all curves in their Action as original-channel writers on every bone.
        scan['evaluated_action_constraints'] = [row for row in scan['writers'] if row['kind'] == 'ACTION_constraint']
        scan['writers'] = [row for row in scan['writers'] if row['kind'] != 'ACTION_constraint']
        require(not scan['writers'], 'WRITER_CONFLICT', scan['writers'])
        return plan, {'writer_scan': scan, 'mode_guards': guards}

    def _input_record(self, role, value):
        bone = self.rig.pose.bones[role['bone']]
        if role['kind'] == 'mode_enum':
            original = bone[role['property']]
            return {'role': role, 'bone': bone, 'channel': None, 'original': original, 'expected': type(original)(value)}
        channel = CHANNELS[role['kind']]
        if channel == 'rotation_quaternion':
            original = list(bone.rotation_quaternion)
            quaternion = bone.rotation_quaternion @ Quaternion(role['axis'], value)
            quaternion.normalize()
            expected = [float(np.float32(component)) for component in quaternion]
        else:
            original = float(getattr(bone, channel)[role['index']])
            expected = float(np.float32(value if role['kind'] == 'scale_set' else original + value))
        return {'role': role, 'bone': bone, 'channel': channel, 'original': original, 'expected': expected}

    def _read_input(self, record):
        bone, role, channel = record['bone'], record['role'], record['channel']
        if channel is None: return bone[role['property']]
        if channel == 'rotation_quaternion': return list(bone.rotation_quaternion)
        return float(getattr(bone, channel)[role['index']])

    def _write_input(self, record, value):
        bone, role, channel = record['bone'], record['role'], record['channel']
        if channel is None: bone[role['property']] = value
        elif channel == 'rotation_quaternion': bone.rotation_quaternion = value
        else: getattr(bone, channel)[role['index']] = value

    def _input_exact(self, record, value):
        actual = self._read_input(record)
        if record['channel'] is None: return type(actual) is type(value) and actual == value
        a = actual if isinstance(actual,list) else [actual]
        b = value if isinstance(value,list) else [value]
        return len(a)==len(b) and all(struct.pack('<f',x)==struct.pack('<f',y) for x,y in zip(a,b))

    def _update(self):
        self.rig.update_tag(refresh={'OBJECT', 'DATA'}); bpy.context.view_layer.update()

    def _pose_paths(self, before, bone, field, index=None):
        block_index = next(i for i, block in enumerate(before['blocks']) if block['type'] == 'Object' and block['name'] == self.rig.name and block['library'] is None)
        block = before['blocks'][block_index]
        pose_index = next(i for i, pose in enumerate(block['pose']) if pose['name'] == bone)
        paths = [('blocks', block_index, 'pose', pose_index, 'settings', field),
                 ('blocks', block_index, 'character_extra', 'pose_custom', bone, field)]
        if index is not None: paths = [path + (index,) for path in paths]
        return [path for path in paths if get(before, path) is not None]

    def _matrix_paths(self, before, bone):
        paths = self._pose_paths(before, bone, 'matrix_basis')
        i = next(i for i, block in enumerate(before['blocks']) if block['type'] == 'Object' and block['name'] == self.rig.name and block['library'] is None)
        j = next(j for j, pose in enumerate(before['blocks'][i]['pose']) if pose['name'] == bone)
        return paths + [('blocks', i, 'pose', j, 'matrix_basis')]

    def _property_paths(self, before, bone, property):
        paths = self._pose_paths(before, bone, 'custom_properties')
        i = next(i for i, block in enumerate(before['blocks']) if block['type'] == 'Object' and block['name'] == self.rig.name and block['library'] is None)
        j = next(j for j, pose in enumerate(before['blocks'][i]['pose']) if pose['name'] == bone)
        paths.append(('blocks', i, 'pose', j, 'custom_properties'))
        return [path + (property,) for path in paths if get(before, path + (property,)) is not None]

    def inspect(self, operations):
        require(self.pending is None, 'APPLY_ALREADY_PENDING')
        plan, preflight = self.preflight(operations)
        bpy.context.view_layer.update()
        witness = self.observe(); before = C.capture()
        outputs = O.declare(before, self.consumer_rigs, self.output_keys)
        token = 'snap_' + uuid.uuid4().hex
        self.snapshot = {'token': token, 'operations': deepcopy(operations), 'plan': plan, 'before': before, 'context': S1.context(),
                         'witness': witness, 'outputs': outputs, 'fingerprint': C.digest(before)}
        return deepcopy({'snapshot_token': token, 'live_fingerprint': self.snapshot['fingerprint'], 'preflight': preflight,
                'witness': witness, 'declared_outputs': [row['metadata'] for row in outputs], 'write_count': self.write_count})

    def apply(self, operations, snapshot_token, fault=None):
        require(fault in (None, 'after_first_write'), 'FAULT_SCHEMA')
        require(self.pending is None, 'APPLY_ALREADY_PENDING')
        snapshot = self.snapshot
        require(snapshot is not None and snapshot['token'] == snapshot_token and snapshot['operations'] == operations, 'STALE_SNAPSHOT')
        plan, _ = self.preflight(operations)
        bpy.context.view_layer.update()
        live_witness = self.observe()
        require(C.capture() == snapshot['before'] and S1.context() == snapshot['context'] and live_witness == snapshot['witness'], 'LIVE_STATE_CHANGED')
        records = [self._input_record(role, value) for role, value in plan]
        self.pending = {'token': 'restore_' + uuid.uuid4().hex, 'records': records, **{key: snapshot[key] for key in ('before', 'context', 'witness')}}
        try:
            for number, record in enumerate(records):
                self._write_input(record, record['expected']); self.write_count += 1
                if number == 0 and fault == 'after_first_write': self._update(); raise ControlError('INJECTED_AFTER_FIRST_WRITE')
            self._update()
            for record in records: require(self._input_exact(record, record['expected']), 'WRITE_READBACK', record['role']['role_id'])
            actual = self.observe(); after = C.capture(); allowed = []
            for record in records:
                role = record['role']
                if record['channel'] is None: allowed += self._property_paths(snapshot['before'], role['bone'], role['property'])
                else:
                    indices = range(4) if record['channel'] == 'rotation_quaternion' else (role['index'],)
                    for index in indices: allowed += self._pose_paths(snapshot['before'], role['bone'], record['channel'], index)
                    allowed += self._matrix_paths(snapshot['before'], role['bone'])
            native_outputs = []
            for output in snapshot['outputs']:
                if O.source_values(output['sources']) != output['source_before']:
                    require(output['fc'].driver.is_valid, 'DERIVED_DRIVER_INVALID')
                    allowed += output['paths']
                value = O.value(output)
                native_outputs.append({**output['metadata'], 'value_after': value})
            changed = S1.changes(snapshot['before'], after)
            unexpected = [path for path in changed if not any(path[:len(prefix)] == prefix for prefix in allowed)]
            require(not unexpected, 'PROTECTION_CHANGED', [list(path) for path in unexpected[:80]])
            require(S1.context() == snapshot['context'], 'CONTEXT_CHANGED')
            self.pending.update(after=after, after_witness=actual)
            response = [name for name in actual if actual[name] != snapshot['witness'][name]]
            return deepcopy({'status': 'applied', 'restore_token': self.pending['token'], 'write_count': self.write_count,
                    'inputs': [{'role': record['role']['role_id'], 'before': record['original'], 'after': self._read_input(record)} for record in records],
                    'witness_before': snapshot['witness'], 'witness_after': actual, 'responding': response,
                    'changed_author_paths': [list(path) for path in changed], 'allowed_prefixes': [list(path) for path in allowed],
                    'changed_author_leaves': [{'path': list(path), 'before': get(snapshot['before'], path), 'after': get(after, path)} for path in changed],
                    'after_fingerprint': C.digest(after), 'native_outputs': native_outputs})
        except Exception as error:
            pending = self.pending
            try:
                recovery = self._restore_internal()
            except Exception as restore_error:
                self.pending = pending
                self.pending['recovery_only'] = True
                self._record_recovery_state()
                raise ControlError('ROLLBACK_FAILED', {'cause': str(error), 'recovery_error': str(restore_error), 'recovery_token': pending['token']}) from error
            if not all(recovery[key] for key in ('full_author_exact', 'context_exact', 'actual_witness_exact', 'raw_original_exact')):
                self.pending['recovery_only'] = True
                self._record_recovery_state()
                raise ControlError('ROLLBACK_FAILED', {'cause': str(error), 'rollback': recovery, 'recovery_token': self.pending['token']}) from error
            if isinstance(error, ControlError): error.detail = {'cause': error.detail, 'rollback': recovery}
            raise

    def _record_recovery_state(self):
        try:
            self._update()
            self.pending['recovery_witness'] = self.observe()
            self.pending['recovery_author'] = C.capture()
        except Exception as error:
            self.pending['recovery_observation_error'] = str(error)

    def _restore_internal(self):
        pending = self.pending; require(pending is not None, 'NO_PENDING_RESTORE')
        for record in reversed(pending['records']): self._write_input(record, record['original'])
        self._update()
        witness = self.observe(); author = C.capture()
        answer = {'full_author_exact': author == pending['before'], 'context_exact': S1.context() == pending['context'],
                  'actual_witness_exact': witness == pending['witness'], 'raw_original_exact': all(self._input_exact(record, record['original']) for record in pending['records']),
                  'author_sha256': C.digest(author), 'full_author_sha256_exact': C.digest(author)==C.digest(pending['before'])}
        if all(answer[key] for key in ('full_author_exact', 'context_exact', 'actual_witness_exact', 'raw_original_exact')): self.pending = self.snapshot = None
        return answer

    def restore(self, restore_token):
        require(self.pending is not None and self.pending['token'] == restore_token, 'RESTORE_TOKEN_REQUIRED')
        live_witness=self.observe()
        if self.pending.get('recovery_only'):
            # Retry only if the live state still equals the last failed recovery.
            require(all(key in self.pending for key in ('recovery_author','recovery_witness')) and C.capture() == self.pending['recovery_author'] and S1.context() == self.pending['context'] and live_witness == self.pending['recovery_witness'], 'RECOVERY_REQUIRES_REVIEW')
        else:
            require(C.capture() == self.pending['after'] and S1.context() == self.pending['context'] and live_witness == self.pending['after_witness'], 'STALE_APPLIED_STATE')
        try:
            result = self._restore_internal()
            require(all(result[key] for key in ('full_author_exact', 'context_exact', 'actual_witness_exact', 'raw_original_exact')), 'RESTORE_FAILED', result)
        except Exception as error:
            if self.pending is not None:
                self.pending['recovery_only']=True
                self._record_recovery_state()
                raise ControlError('RESTORE_FAILED', {'cause':str(error),'recovery_token':self.pending['token']}) from error
            raise
        return {'status': 'restored_exact', **result}


class BoundBatch:
    """Trusted caller binds a fixed atomic request; UI receives detached data."""
    def __init__(self, document):
        require(type(document) is dict and set(document) == {'schema', 'binding_id', 'character_id', 'role_id', 'runtime', 'native', 'profile', 'operations'}, 'BATCH_BINDING_SCHEMA')
        require(document['schema'] == 'hrig.control-batch-binding.v2', 'BATCH_BINDING_VERSION')
        self._document = deepcopy(document)
        self._core = AtomicControls(document['profile'])
        self._operations = deepcopy(document['operations'])
        self._core.preflight(self._operations)
        require(document['character_id'] == self._core.profile['character_id'] and document['runtime'] == self._core.profile['runtime'], 'BATCH_PROFILE_IDENTITY')
        first_role = next(role for role in self._core.profile['roles'] if role['role_id'] == self._operations[0]['role'])
        require(document['native'] == {'rig': self._core.rig.name, 'data': self._core.rig.data.name, 'bone': first_role['bone']}, 'BATCH_NATIVE_IDENTITY')

    def describe(self): return deepcopy(self._document)
    def inspect(self): return deepcopy(self._core.inspect(deepcopy(self._operations)))
    def apply(self, token): return deepcopy(self._core.apply(deepcopy(self._operations), token))
    def restore(self, token): return deepcopy(self._core.restore(token))
    @property
    def write_count(self): return self._core.write_count
    @property
    def has_pending_edit(self): return self._core.pending is not None
    @property
    def recovery_token(self): return self._core.pending['token'] if self._core.pending is not None and self._core.pending.get('recovery_only') else None
    def input_state(self):
        values = []; labels = []
        for role, value in validate_operations(self._core.profile, self._operations):
            actual = self._core._read_input(self._core._input_record(role, value))
            values.extend(actual if isinstance(actual, list) else [actual])
            labels.append(role['role_id'] + ': ' + str(actual))
        return {'text': '; '.join(labels), 'value': values[0] if len(values) == 1 else values}
