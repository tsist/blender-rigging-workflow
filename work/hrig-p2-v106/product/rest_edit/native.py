"""Owned native reference/replay for one explicit deform-leaf rest edit.

inspect is a reversible native projection, not a read-only query.  It freezes
an independent full after-state only after the original source is restored.
No source ID enters Edit mode and no old control profile is re-signed here.
Parent changes freeze both complete hierarchy orders before their setter;
only expected Bone/Pose rows are reassembled, never actual RNA observations.
Transition receipts certify a synchronous native-return boundary only. Unknown
reentrant Edit-raw writers inside native calls or before receipt capture are
not authenticated by this finite recovery model.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct
import uuid

import bpy
import numpy as np

import character_state as C
import native_control as N
from native_controls.native import AtomicControls
from native_controls.contract import digest as control_digest
from .contract import RestEditError, require, validate_request, plan_roll, plan_tail, plan_head, plan_parent, operation, digest

_TRANSITION_SCHEMAS = {'roll': 'hrig.rest-transition.v1', 'tail': 'hrig.rest-transition.v3', 'head': 'hrig.rest-transition.v4', 'parent': 'hrig.rest-transition.v5'}
_EVIDENCE_SCHEMAS = {'roll': 'hrig.rest-edit-binding-evidence.v1', 'tail': 'hrig.rest-edit-binding-evidence.v3', 'head': 'hrig.rest-edit-binding-evidence.v4', 'parent': 'hrig.rest-edit-binding-evidence.v5'}


def error_record(error):
    return {'code': getattr(error, 'code', type(error).__name__),
            'detail': deepcopy(getattr(error, 'detail', str(error)))}


def identity(value):
    return {'type': value.bl_rna.identifier, 'name': value.name,
            'library': None if value.library is None else value.library.filepath}


def data_rest_signature(data):
    # Same field contract as AtomicControls.rest_signature, read from the
    # retained original/owned Armature directly, without swapping it to query.
    return control_digest([{'name': b.name, 'parent': b.parent.name if b.parent else None,
        'matrix': [list(row) for row in b.matrix_local], 'head': list(b.head_local),
        'tail': list(b.tail_local), 'connected': b.use_connect, 'deform': b.use_deform}
        for b in sorted(data.bones, key=lambda item: item.name)])


def bone_topology(data):
    return [{'name': b.name, 'parent': b.parent.name if b.parent else None,
        'use_connect': bool(b.use_connect), 'use_deform': bool(b.use_deform),
        'bbone_segments': int(b.bbone_segments)} for b in sorted(data.bones, key=lambda b: b.name)]


def parent_forest_order(raw_order, topology):
    """Predict native childbase append + preorder; never sort actual RNA lists."""
    require(type(raw_order) is list and len(set(raw_order)) == len(raw_order), 'PARENT_RAW_ORDER_SCHEMA')
    rows = {r['name']: r for r in topology}
    require(len(rows) == len(topology) == len(raw_order) and set(rows) == set(raw_order), 'PARENT_TOPOLOGY_ROSTER')
    children = {None: []}
    children.update({n: [] for n in raw_order})
    for n in raw_order:
        p = rows[n]['parent']
        require(p in children and p != n, 'PARENT_TOPOLOGY_GRAPH')
        children[p].append(n)
    answer, seen, stack = [], set(), list(reversed(children[None]))
    while stack:
        n = stack.pop()
        require(n not in seen, 'PARENT_TOPOLOGY_CYCLE')
        seen.add(n); answer.append(n); stack.extend(reversed(children[n]))
    require(len(answer) == len(raw_order), 'PARENT_TOPOLOGY_CYCLE')
    return answer


def block(author, value):
    ident = identity(value)
    found = [item for item in author['blocks']
             if all(item.get(key) == val for key, val in ident.items())]
    require(len(found) == 1, 'AUTHOR_IDENTITY_AMBIGUOUS', ident)
    return found[0]


def rest_arrays(data):
    bones = sorted(data.bones, key=lambda item: item.name)
    return {'names': [bone.name for bone in bones],
        'matrix_local': np.asarray([[list(row) for row in b.matrix_local] for b in bones], dtype='<f4'),
        'matrix_parent_relative': np.asarray([[list(row) for row in b.matrix] for b in bones], dtype='<f4'),
        'head_tail_local': np.asarray([[*b.head_local, *b.tail_local] for b in bones], dtype='<f4')}


def bone_lengths(data):
    return np.asarray([b.length for b in sorted(data.bones, key=lambda b: b.name)], dtype='<f4')


def rest_digest(rest):
    return digest({key: value if key == 'names' else value.tolist() for key, value in rest.items()})


def array_digest(value):
    return hashlib.sha256(value.tobytes(order='C')).hexdigest()


def raw_digest(rows):
    value = hashlib.sha256(b'hrig.rest-edit.ordered-native-f32rows.v1\0')
    value.update(struct.pack('<I', len(rows)))
    for index, row in enumerate(rows):
        value.update(struct.pack('<II', index, len(row)))
        for group, weight in row:
            value.update(struct.pack('<I', group)); value.update(weight)
    return value.hexdigest()


def same_array(left, right):
    return left.dtype == right.dtype and left.shape == right.shape and left.tobytes() == right.tobytes()


def same_rest(left, right):
    return left['names'] == right['names'] and all(same_array(left[key], right[key])
        for key in ('matrix_local', 'matrix_parent_relative', 'head_tail_local'))


class _RecoveryCall:
    """Stack-local lifetime. Never retained by a transaction or public result."""
    def __init__(self, owner):
        self.owner, self.active, self.used = owner, False, False

    def __enter__(self):
        require(not self.used, 'RECOVERY_CALL_REENTRY')
        self.active, self.used = True, True
        return self

    def __exit__(self, *_):
        self.active = False


@dataclass(frozen=True, slots=True)
class _TransitionProof:
    call: object
    pending: object
    receipt: object
    checkpoint: object
    token: str
    seq: int
    op: str
    owned_uid: int
    mode: str
    writes: int
    generation: int


class RestEditTransaction:
    """inspect -> preview -> validate_applied/binding_evidence -> restore.

    core, rig, original_data and body are trusted in-process integration
    references. Returned records and pending are detached JSON-sized data.
    Private state retains large C/arrays for trusted producer/cold integration.
    """
    def __init__(self, profile, source_identity, body_name, idle_guard):
        require(type(source_identity) is dict and set(source_identity) == {'file', 'expected_sha256'}, 'SOURCE_IDENTITY_SCHEMA')
        path, expected = source_identity['file'], source_identity['expected_sha256']
        require(type(path) is str and Path(path).is_absolute(), 'SOURCE_PATH', path)
        require(type(expected) is str and len(expected) == 64 and all(c in '0123456789abcdef' for c in expected), 'SOURCE_SHA256')
        require(type(body_name) is str and bool(body_name), 'BODY_NAME')
        require(callable(idle_guard), 'IDLE_GUARD_REQUIRED')
        self._source = deepcopy(source_identity)
        self._profile = deepcopy(profile)
        self._profile_digest = digest(profile)
        self._idle_guard = idle_guard
        self.core = AtomicControls(deepcopy(profile))
        self.rig = self.core.rig
        self.original_data = self.rig.data
        self.body = bpy.data.objects.get(body_name)
        require(self.body is not None and self.body.type == 'MESH', 'BODY_IDENTITY', body_name)
        self._body_mesh = self.body.data
        self._rig_name, self._original_name = self.rig.name, self.original_data.name
        self._body_name, self._mesh_name = body_name, self._body_mesh.name
        require(all(hasattr(value, 'session_uid') for value in (self.rig, self.original_data, self.body, self._body_mesh)),
                'ID_LIFETIME_IDENTITY_UNAVAILABLE')
        self._source_uids = {key: int(value.session_uid) for key, value in
            (('rig', self.rig), ('original', self.original_data), ('body', self.body), ('mesh', self._body_mesh))}
        self._original_users = self.original_data.users
        self._protected_roles = sorted({row['bone'] for row in profile['roles']} |
            {guard['bone'] for row in profile['roles'] for guard in row['requires']})
        self._snapshot = None
        self._pending = None
        self.write_count = 0
        # Only an invalidation counter, never a cached proof/witness.
        self._proof_generation = 0
        self._guard()

    @property
    def pending(self):
        if self._pending is None:
            return None
        pending = self._pending
        return deepcopy({'token': pending['token'], 'phase': pending['phase'],
            'kind': pending['kind'], 'owned_name': pending['owned_name'],
            'checkpoint_kind': None if pending.get('checkpoint') is None else pending['checkpoint']['kind'],
            'transition_kind': None if pending.get('transition') is None else pending['transition']['op'],
            'receipt_complete': bool((pending.get('transition') or {}).get('post_complete')),
            'recovery_qualification': (pending.get('transition') or {}).get('qualification', 'verified_checkpoint'),
            'exit_cancelled_fixture': bool(pending.get('exit_blocked'))})

    def _transition_gap_hook(self, name):
        """Private fixture hook after complete synchronous native post capture."""
        return None

    def _call_transition_gap_hook(self, name):
        self._proof_generation += 1
        return self._transition_gap_hook(name)

    def transition_receipt(self):
        """Detached diagnostic receipt, excluding all large/private witnesses."""
        receipt = None if self._pending is None else self._pending.get('transition')
        if receipt is None:
            return None
        return deepcopy({key: receipt[key] for key in ('schema', 'op', 'kind', 'seq',
            'last_checkpoint_kind', 'last_checkpoint_sha256', 'intent', 'ids', 'native',
            'post_complete', 'qualification', 'trust_model')})

    def _checkpoint_digest(self, checkpoint):
        if checkpoint['kind'] == 'object':
            value = checkpoint['state']
            return digest({'kind': 'object', 'author': digest(value['author']), 'context': digest(value['context']),
                'rest': rest_digest(value['rest']), 'rows': raw_digest(value['rows']),
                'lengths': array_digest(value['lengths']),
                'bones': array_digest(value['bones']), 'skin': array_digest(value['skin'])})
        return digest({'kind': 'edit', 'author': digest(checkpoint['author']), 'context': digest(checkpoint['context']),
            'edit_raw': digest(checkpoint['edit_raw']), 'rows': raw_digest(checkpoint['rows']),
            'original_lengths': array_digest(checkpoint['original_lengths']),
            'original_rest': rest_digest(checkpoint['original_rest'])})

    def _transition_begin(self, op):
        pending = self._pending
        edit_op = operation(pending['request'])
        require(op in ('enter', edit_op, 'exit'), 'TRANSITION_OPERATION_SCHEMA', op)
        require(pending.get('transition') is None, 'TRANSITION_ALREADY_PENDING')
        self._assert_checkpoint()
        checkpoint = pending['checkpoint']
        pending['transition_seq'] += 1
        pending['transition'] = {'schema': _TRANSITION_SCHEMAS[edit_op], 'op': op, 'kind': pending['kind'],
            'seq': pending['transition_seq'], 'last_checkpoint_kind': checkpoint['kind'],
            'last_checkpoint_sha256': self._checkpoint_digest(checkpoint),
            'checkpoint': deepcopy(checkpoint),
            'intent': {'request': deepcopy(pending['request']), 'plan': deepcopy(pending.get('plan')),
                'from_mode': bpy.context.mode, 'to_mode': 'OBJECT' if op == 'exit' else 'EDIT_ARMATURE',
                'mirror_before': bool(pending['owned'].use_mirror_x),
                'source_author_scope': 'explicit_relink_only',
                'owned_rest_scope': {'roll': 'one_target_matrix_only', 'tail': 'one_target_tail_and_native_derivatives', 'head': 'one_target_head_and_native_derivatives', 'parent': 'one_parent_edge_and_source_predicted_hierarchy_representation'}[edit_op]},
            'ids': {'source': deepcopy(self._source), 'source_uids': deepcopy(self._source_uids),
                'owned_name': pending['owned_name'], 'owned_uid': pending['owned_session_uid']},
            'native': None, 'post': None, 'post_complete': False, 'qualification': 'post_witness_incomplete',
            'trust_model': 'synchronous native return; unknown reentrant Edit-raw writes before receipt are not certified',
            'tail_mirror': None, 'discard_exit': None}

    def _transition_observe(self):
        """Native post observation is a drift fence, never an expected state."""
        self._proof_generation += 1
        self._guard(self._pending['baseline']['context']['frame'], allow_edit=True)
        original = rest_arrays(self.original_data)
        if bpy.context.mode == 'OBJECT':
            return {'mode': 'OBJECT', 'state': self._collect(), 'original_rest': original, 'original_lengths': bone_lengths(self.original_data)}
        require(bpy.context.mode == 'EDIT_ARMATURE', 'TRANSITION_MODE_UNOBSERVABLE')
        return {'mode': 'EDIT_ARMATURE', 'author': C.capture(), 'context': self._context(),
            'rest': rest_arrays(self._pending['owned']), 'lengths': bone_lengths(self._pending['owned']), 'rows': self._raw_all(),
            'edit_raw': self._edit_raw(), 'original_rest': original, 'original_lengths': bone_lengths(self.original_data)}

    def _transition_post(self, operator_result=None):
        receipt = self._pending['transition']
        receipt['native'] = {'operator_result': [] if operator_result is None else sorted(operator_result),
            'actual_mode': bpy.context.mode, 'bound_owned': self.rig.data == self._pending['owned'],
            'original_is_editmode': bool(self.original_data.is_editmode)}
        if receipt['op'] == 'roll':
            receipt['native']['roll_readback'] = float(self._pending['owned'].edit_bones[self._pending['request']['bone']].roll)
        elif receipt['op'] == 'parent':
            bone = self._pending['owned'].edit_bones[self._pending['request']['bone']]
            receipt['native']['parent_readback'] = bone.parent.name if bone.parent else None
            receipt['native']['parent_owned_uid'] = int(self._pending['owned'].session_uid)
            receipt['native']['raw_length_readback'] = float(bone.length)
        elif receipt['op'] in ('tail', 'head'):
            bone = self._pending['owned'].edit_bones[self._pending['request']['bone']]
            receipt['native'][receipt['op'] + '_readback'] = list(getattr(bone, receipt['op']))
            receipt['native']['raw_length_readback'] = float(self._pending['owned'].edit_bones[self._pending['request']['bone']].length)
        # If any getter fails, post_complete remains False; no recovery proof
        # adopts a later failure scene to repair an incomplete receipt.
        receipt['post'] = self._transition_observe()
        receipt['post_complete'] = True
        receipt['qualification'] = 'scope_validation_required'

    def _transition_parts(self, witness):
        return witness['state'] if witness['mode'] == 'OBJECT' else witness

    def _transition_compare(self, actual, expected):
        require(actual['mode'] == expected['mode'], 'TRANSITION_POST_FENCE_CHANGED', {'mode_exact': False})
        require(same_rest(actual['original_rest'], expected['original_rest']), 'TRANSITION_POST_FENCE_CHANGED', {'original_rest_exact': False})
        require(same_array(actual['original_lengths'], expected['original_lengths']), 'TRANSITION_POST_FENCE_CHANGED', {'original_lengths_exact': False})
        if actual['mode'] == 'OBJECT':
            return self._assert_state(actual['state'], expected['state'], 'TRANSITION_POST_FENCE_CHANGED')
        checks = {'author_exact': actual['author'] == expected['author'], 'context_exact': actual['context'] == expected['context'],
            'edit_raw_exact': actual['edit_raw'] == expected['edit_raw'] and (operation(self._pending['request']) != 'head' or list(actual['edit_raw']) == list(expected['edit_raw'])), 'rest_exact': same_rest(actual['rest'], expected['rest']),
            'lengths_exact': same_array(actual['lengths'], expected['lengths']),
            'raw_rows_exact': actual['rows'] == expected['rows']}
        if operation(self._pending['request']) == 'parent':
            checks['author_exact'] = checks['author_exact'] and digest(actual['author']) == digest(expected['author'])
            checks['edit_raw_exact'] = checks['edit_raw_exact'] and list(actual['edit_raw']) == list(expected['edit_raw']) and digest(actual['edit_raw']) == digest(expected['edit_raw'])
        require(all(checks.values()), 'TRANSITION_POST_FENCE_CHANGED', checks)
        return checks

    def _transition_effective_post(self, receipt, witness=None):
        tail = receipt.get('tail_mirror')
        if witness is None and tail is None:
            # Private read-only consumer below; no public alias is returned.
            return receipt['post']
        expected = deepcopy(receipt['post'] if witness is None else witness)
        if tail is not None:
            require(expected['mode'] == 'OBJECT' and receipt['op'] == 'exit', 'TRANSITION_TAIL_SCHEMA')
            owned = block(expected['state']['author'], self._pending['owned'])
            require(owned['character_extra']['settings']['use_mirror_x'] == tail['before'], 'TRANSITION_MIRROR_PRECONDITION')
            owned['character_extra']['settings']['use_mirror_x'] = tail['after']
        return expected

    def _transition_raw_scope(self, before, after, plan):
        bone = self._pending['request']['bone']
        op = operation(self._pending['request'])
        if op == 'parent':
            projection = self._parent_projection()
            require(list(before) == projection['raw_order_before'] == list(after), 'PARENT_RAW_BONE_ORDER_CHANGED')
            require(before == projection['raw_before'] and digest(before) == digest(projection['raw_before']), 'PARENT_RAW_BEFORE_CHANGED')
            expected = deepcopy(before)
            require(expected[bone]['parent'] == plan['raw_before'], 'PARENT_RAW_FROM_MISMATCH')
            expected[bone]['parent'] = plan['raw_after']
            require(after == expected and digest(after) == digest(expected), 'PARENT_NON_TARGET_RAW_CHANGED')
            return
        field = op
        if op == 'head':
            require(list(before) == list(after), 'HEAD_RAW_BONE_ORDER_CHANGED')
        require(digest(after[bone]['fields'][field]) == digest(plan['raw_after']), 'TRANSITION_' + op.upper() + '_READBACK')
        if op in ('tail', 'head'):
            protected = plan['protected']
            other = 'head' if op == 'tail' else 'tail'
            checks = {other: digest(after[bone]['fields'][other]) == digest(protected[other]),
                'roll': digest(after[bone]['fields']['roll']) == digest(protected['raw_roll']),
                'parent': after[bone]['parent'] == protected['parent'],
                'connect': after[bone]['fields']['use_connect'] is protected['use_connect']}
            require(all(checks.values()), op.upper() + '_PROTECTED_RAW_CHANGED', checks)
        left, right = deepcopy(before), deepcopy(after)
        for value in (left[bone], right[bone]):
            value['fields'].pop(field, None)
            value['fields'].pop('matrix', None)  # native derivative, not a new authored input
            if op in ('tail', 'head'):
                value['fields'].pop('length', None)  # derived from the declared head/tail
        require(digest(left) == digest(right), 'TRANSITION_NON_TARGET_EDIT_DRIFT')

    def _transition_scope(self, receipt, witness, *, discarded=False):
        """Prove only discard eligibility from the original immutable domain.

        Reference has no pre-existing after oracle: this proof never accepts a
        new asset/profile. Preview additionally requires independent step trace.
        """
        pending = self._pending
        value = self._transition_parts(witness)
        op = receipt['op']
        edit_op = operation(pending['request'])
        require(op in ('enter', edit_op, 'exit'), 'TRANSITION_OPERATION_SCHEMA')
        written = op != 'enter'
        expected_mode = 'OBJECT' if discarded or op == 'exit' else 'EDIT_ARMATURE'
        require(witness['mode'] == expected_mode, 'TRANSITION_SCOPE_MODE')
        require(value['context'] == self._owned_context(active=True, edit=expected_mode == 'EDIT_ARMATURE'), 'TRANSITION_SCOPE_CONTEXT')
        require(value['rows'] == pending['baseline']['rows'] and same_rest(witness['original_rest'], pending['baseline']['rest']),
                'TRANSITION_ORIGINAL_RAW_OR_REST_CHANGED')
        require(same_array(witness['original_lengths'], pending['baseline']['lengths']), 'TRANSITION_ORIGINAL_LENGTHS_CHANGED')
        if written:
            require(receipt['intent']['plan'] == pending['plan'] and pending['plan'] is not None, 'TRANSITION_PLAN_CHANGED')
            self._check_rest_scope(value['rest'], lengths=value['lengths'], require_changed=expected_mode == 'OBJECT', parent_finalized=expected_mode == 'OBJECT')
            target_matrix = next(row['matrix_local'] for row in block(value['author'], pending['owned'])['bones']
                                 if row['name'] == pending['request']['bone'])
            target_tail = block(value['author'], pending['owned'])['character_extra']['bone_settings'][pending['request']['bone']]['tail'] if edit_op == 'tail' else None
            target_head = block(value['author'], pending['owned'])['character_extra']['bone_settings'][pending['request']['bone']]['head'] if edit_op == 'head' else None
            if edit_op == 'parent':
                if expected_mode == 'OBJECT':
                    fields = block(value['author'], pending['owned'])['character_extra']['bone_settings'][pending['request']['bone']]
                    target_head, target_tail = fields['head'], fields['tail']
                else:
                    target_matrix = target_head = target_tail = None
        else:
            require(same_rest(value['rest'], pending['baseline']['rest']), 'TRANSITION_ENTER_REST_CHANGED')
            require(same_array(value['lengths'], pending['baseline']['lengths']), 'TRANSITION_ENTER_LENGTHS_CHANGED')
            target_matrix = None
            target_tail = None
            target_head = None
        mirror = receipt['intent']['mirror_before']
        if not discarded and receipt.get('tail_mirror') is not None:
            mirror = receipt['tail_mirror']['after']
        self._assert_declared_author(value['author'], mirror=mirror, target_matrix=target_matrix, target_tail=target_tail, target_head=target_head,
                                     parent_finalized=edit_op == 'parent' and written and expected_mode == 'OBJECT')
        if expected_mode == 'EDIT_ARMATURE' and op == edit_op:
            self._transition_raw_scope(receipt['checkpoint']['edit_raw'], value['edit_raw'], pending['plan'])
            if edit_op in ('tail', 'head', 'parent'):
                length = value['edit_raw'][pending['request']['bone']]['fields']['length']
                require(math.isfinite(length) and length > 0 and struct.pack('<f', length) == struct.pack('<f', receipt['native']['raw_length_readback']), 'TRANSITION_RAW_LENGTH_READBACK')
        if op == 'exit':
            checkpoint = receipt['checkpoint']
            require(checkpoint['kind'] == 'edit', 'TRANSITION_EXIT_PRECONDITION')
            plan = pending['plan']
            raw = checkpoint['edit_raw'][pending['request']['bone']]
            field = edit_op
            actual_value = raw['parent'] if edit_op == 'parent' else raw['fields'][field]
            require(digest(actual_value) == digest(plan['raw_after']), 'TRANSITION_EXIT_RAW_PLAN')
            if edit_op in ('tail', 'head', 'parent'):
                self._transition_raw_scope(pending['step_trace']['enter']['edit_raw'], checkpoint['edit_raw'], plan)

    def _prove_transition(self, *, _call=None):
        pending = self._pending
        receipt = pending['transition']
        edit_op = operation(pending['request'])
        require(receipt['op'] in ('enter', edit_op, 'exit'), 'TRANSITION_OPERATION_SCHEMA')
        require(receipt['schema'] == _TRANSITION_SCHEMAS[edit_op], 'TRANSITION_VERSION')
        require(receipt['post_complete'] and receipt['post'] is not None and receipt['native'] is not None,
                'TRANSITION_POST_WITNESS_INCOMPLETE')
        self._guard(pending['baseline']['context']['frame'], allow_edit=True)
        require(receipt['ids']['source'] == self._source and receipt['ids']['source_uids'] == self._source_uids
                and receipt['ids']['owned_uid'] == int(pending['owned'].session_uid)
                and receipt['ids']['owned_name'] == pending['owned_name']
                and receipt['intent']['request'] == pending['request'], 'TRANSITION_IDENTITY_CHANGED')
        require(receipt['native']['bound_owned'] and not receipt['native']['original_is_editmode'], 'TRANSITION_NATIVE_BINDING_UNPROVEN')
        require(not self.original_data.is_editmode, 'ORIGINAL_EDIT_MODE_FORBIDDEN')
        if receipt['op'] in ('enter', 'exit'):
            require('FINISHED' in receipt['native']['operator_result']
                    and receipt['native']['actual_mode'] == receipt['intent']['to_mode'], 'TRANSITION_NATIVE_RESULT_UNPROVEN')
        elif receipt['op'] == edit_op:
            field = edit_op + '_readback'
            require(receipt['native']['actual_mode'] == 'EDIT_ARMATURE'
                    and digest(receipt['native'][field]) == digest(pending['plan']['raw_after']), 'TRANSITION_NATIVE_' + edit_op.upper() + '_UNPROVEN')
            if edit_op == 'parent':
                require(receipt['native']['parent_owned_uid'] == int(pending['owned'].session_uid), 'PARENT_TRANSITION_OWNER_CHANGED')
        discard = receipt.get('discard_exit')
        if discard is not None:
            require(discard['post_complete'] and 'FINISHED' in discard['operator_result']
                    and discard['actual_mode'] == 'OBJECT' and discard['bound_owned'], 'DISCARD_EXIT_UNPROVEN')
            self._transition_scope(receipt, discard['post'], discarded=True)
            actual = self._transition_observe()
            self._transition_compare(actual, discard['post'])
        else:
            effective = self._transition_effective_post(receipt)
            self._transition_scope(receipt, effective)
            actual = self._transition_observe()
            self._transition_compare(actual, effective)
        if pending['kind'] == 'preview':
            require(self._snapshot is not None and receipt['op'] in self._snapshot['reference_steps'], 'REFERENCE_STEP_TRACE_REQUIRED')
            independent = self._snapshot['reference_steps'][receipt['op']]
            # The trace comes from the separate completed reference lifetime.
            self._transition_compare(receipt['post'], independent)
            if discard is not None:
                current = self._transition_parts(actual)
                if receipt['op'] == 'enter':
                    expected = self._predicted_copy_state(assigned=True, mirror=receipt['intent']['mirror_before'], active=True)
                else:
                    expected = deepcopy(self._snapshot['after'])
                    block(expected['author'], pending['owned'])['character_extra']['settings']['use_mirror_x'] = receipt['intent']['mirror_before']
                self._assert_state(current, expected, 'REFERENCE_DISCARD_EXIT_MISMATCH')
        receipt['qualification'] = 'discard_proven_reference' if pending['kind'] == 'reference' else 'discard_proven_independent_replay'
        if _call is not None:
            require(_call.active and _call.owner is self, 'RECOVERY_CALL_EXPIRED')
            return _TransitionProof(_call, pending, receipt, pending['checkpoint'], pending['token'],
                receipt['seq'], receipt['op'], int(pending['owned'].session_uid),
                bpy.context.mode, self.write_count, self._proof_generation)
        return {'status': receipt['qualification'], 'op': receipt['op']}

    def _require_transition_proof(self, proof, call):
        """No callbacks, evaluation or writes between proof and native exit.

        A full _guard (including idle callback) and current fence already ran
        inside the proof. The pre-write guard below rechecks SHA/native bindings
        while reusing only that idle result, without calling external code again.
        """
        require(type(proof) is _TransitionProof and proof.call is call
                and call.active and call.owner is self, 'RECOVERY_CALL_EXPIRED')
        pending = self._pending
        require(pending is proof.pending and pending is not None
                and pending.get('transition') is proof.receipt
                and pending.get('checkpoint') is proof.checkpoint
                and pending['token'] == proof.token
                and proof.receipt['seq'] == proof.seq and proof.receipt['op'] == proof.op,
                'TRANSITION_PROOF_IDENTITY_CHANGED')
        require(proof.generation == self._proof_generation and proof.writes == self.write_count,
                'TRANSITION_PROOF_INVALIDATED')
        # Lookup live IDs before dereferencing the retained owned RNA.
        require(Path(bpy.data.filepath).resolve() == Path(self._source['file']).resolve()
                and bpy.data.objects.get(self._rig_name) == self.rig
                and bpy.data.armatures.get(pending['owned_name']) == pending['owned'],
                'TRANSITION_PROOF_MAIN_CHANGED')
        require(int(pending['owned'].session_uid) == proof.owned_uid
                and self.rig.data == pending['owned'] and not self.original_data.is_editmode
                and bpy.context.mode == proof.mode, 'TRANSITION_PROOF_BINDING_CHANGED')
        if proof.mode == 'EDIT_ARMATURE':
            require(bpy.context.view_layer.objects.active == self.rig, 'OWNED_EDIT_CONTEXT_REQUIRED')

    def _transition_commit(self, op):
        receipt = self._pending.get('transition')
        require(receipt is not None and receipt['op'] == op and receipt['post_complete'], 'TRANSITION_COMMIT_SCHEMA')
        self._pending['step_trace'][op] = deepcopy(receipt['post'])
        self._pending['transition'] = None

    def _discard_transition_exit(self, *, _proof, _call):
        """Rollback-only exit; never commits checkpoint, reference or applied."""
        pending = self._pending
        receipt = pending['transition']
        self._require_transition_proof(_proof, _call)
        require(not pending.get('exit_unproven') and not pending.get('exit_blocked'), 'DISCARD_EXIT_REQUIRES_EXPLICIT_RESTORE')
        self._guard(pending['baseline']['context']['frame'], allow_edit=True,
                    _proof=_proof, _call=_call)
        require(bpy.context.mode == 'EDIT_ARMATURE' and self.rig.data == pending['owned'], 'DISCARD_EXIT_BINDING')
        receipt['discard_exit'] = {'operator_result': [], 'actual_mode': 'EDIT_ARMATURE',
                                  'bound_owned': True, 'post_complete': False, 'post': None}
        self.write_count += 1
        outcome = bpy.ops.object.mode_set(mode='OBJECT')
        discard = receipt['discard_exit']
        discard.update(operator_result=sorted(outcome), actual_mode=bpy.context.mode, bound_owned=self.rig.data == pending['owned'])
        if not ('FINISHED' in outcome and bpy.context.mode == 'OBJECT' and self.rig.data == pending['owned']):
            pending['exit_unproven'] = True
            raise RestEditError('DISCARD_EXIT_UNPROVEN', self.transition_receipt())
        discard['post'] = self._transition_observe()
        discard['post_complete'] = True
        self._prove_transition()

    def _guard(self, frame=None, *, allow_edit=False, _proof=None, _call=None):
        if _proof is not None:
            self._require_transition_proof(_proof, _call)
        # An external idle callback is a proof barrier, even when it returns True.
        # A guarded write also consumes the old proof; no later reuse is allowed.
        self._proof_generation += 1
        # Main/SHA checks precede every potentially stale RNA dereference.
        require(Path(bpy.data.filepath).resolve() == Path(self._source['file']).resolve(), 'SOURCE_MAIN_MISMATCH', bpy.data.filepath)
        require(hashlib.sha256(Path(self._source['file']).read_bytes()).hexdigest() == self._source['expected_sha256'], 'SOURCE_SHA_CHANGED')
        if _proof is None:
            require(self._idle_guard() is True, 'OTHER_EDIT_PENDING')
        require(digest(self.core.profile) == self._profile_digest and self.core.rig == self.rig, 'SOURCE_PROFILE_CHANGED')
        live = {'rig': bpy.data.objects.get(self._rig_name), 'original': bpy.data.armatures.get(self._original_name),
                'body': bpy.data.objects.get(self._body_name), 'mesh': bpy.data.meshes.get(self._mesh_name)}
        require(all(value is not None and int(value.session_uid) == self._source_uids[key] for key, value in live.items()),
                'SOURCE_MAIN_ID_LIFETIME_CHANGED')
        require(live['rig'] == self.rig and live['original'] == self.original_data,
                'ORIGINAL_RNA_IDENTITY_CHANGED')
        require(self.rig.name == self._rig_name and self.original_data.name == self._original_name, 'ORIGINAL_ID_RENAMED')
        require(bpy.data.objects.get(self._body_name) == self.body and self.body.data == self._body_mesh
                and self._body_mesh.name == self._mesh_name, 'BODY_BINDING_CHANGED')
        require(bpy.context.view_layer.objects.get(self._rig_name) == self.rig
                and bpy.context.view_layer.objects.get(self._body_name) == self.body, 'TARGET_NOT_IN_VIEW_LAYER')
        runtime = self._profile['runtime']
        require(bpy.context.scene.name == runtime['scene'] and bpy.context.view_layer.name == runtime['view_layer'], 'CONTEXT_MISMATCH')
        require(all(x.library is None and x.override_library is None and x.is_editable
                    for x in (self.rig, self.original_data, self.body, self._body_mesh)), 'LOCAL_EDITABLE_REQUIRED')
        expected_data = self.original_data
        pending = self._pending
        if pending is not None and pending.get('bound_owned'):
            expected_data = pending['owned']
            require(expected_data is not None and bpy.data.armatures.get(pending['owned_name']) == expected_data,
                    'OWNED_RNA_IDENTITY_CHANGED')
        require(self.rig.data == expected_data, 'RIG_DATA_BINDING_CHANGED')
        require(self.original_data.users == self._original_users - int(expected_data != self.original_data), 'ORIGINAL_USER_EDGE_CHANGED')
        original_owners = [obj for obj in bpy.data.objects if obj.type == 'ARMATURE' and obj.data == self.original_data]
        require(original_owners == ([self.rig] if expected_data == self.original_data else []), 'ORIGINAL_OWNER_CHANGED')
        if expected_data != self.original_data:
            require(expected_data.library is None and expected_data.override_library is None and expected_data.is_editable
                    and expected_data.name == pending['owned_name'] and not expected_data.use_fake_user
                    and expected_data.users == 1
                    and [obj for obj in bpy.data.objects if obj.type == 'ARMATURE' and obj.data == expected_data] == [self.rig], 'OWNED_DOMAIN_CHANGED')
        require(data_rest_signature(self.original_data) == self._profile['rig']['rest_sha256'], 'ORIGINAL_REST_CHANGED')
        require([obj for obj in bpy.data.objects if obj.type == 'MESH' and obj.data == self._body_mesh] == [self.body], 'SHARED_BODY_MESH')
        modifiers = [modifier for modifier in self.body.modifiers if modifier.type == 'ARMATURE']
        require(len(modifiers) == 1 and modifiers[0].object == self.rig and modifiers[0].use_vertex_groups
                and modifiers[0].show_viewport and modifiers[0].show_render, 'BODY_ARMATURE_BINDING')
        allowed = {'OBJECT', 'EDIT_ARMATURE'} if allow_edit else {'OBJECT'}
        require(bpy.context.mode in allowed, 'OBJECT_MODE_REQUIRED', bpy.context.mode)
        if bpy.context.mode == 'EDIT_ARMATURE':
            require(expected_data != self.original_data and bpy.context.view_layer.objects.active == self.rig
                    and self.rig.data == pending['owned'], 'OWNED_EDIT_CONTEXT_REQUIRED')
        if frame is not None:
            require(bpy.context.scene.frame_current_final == frame, 'OBSERVATION_FRAME_CHANGED', {'expected': frame, 'actual': bpy.context.scene.frame_current_final})
        if pending is None:
            require(AtomicControls(deepcopy(self._profile)).rig == self.rig, 'SOURCE_PROFILE_BINDING_CHANGED')

    def _raw_all(self):
        return tuple(tuple((int(member.group), struct.pack('<f', float(member.weight))) for member in vertex.groups)
                     for vertex in self._body_mesh.vertices)

    def _context(self):
        context = N.context()
        layer = bpy.context.view_layer
        # selected_objects is visibility-filtered. Preserve the actual Base
        # selection flags too, including a rig hidden in this view layer.
        context['selected_raw'] = sorted(obj.name for obj in layer.objects if obj.select_get(view_layer=layer))
        context['rig_visible'] = bool(self.rig.visible_get(view_layer=layer))
        return context

    def _collect(self):
        # depsgraph updates/evaluation may run handlers. Prior proofs expire.
        self._proof_generation += 1
        require(bpy.context.mode == 'OBJECT', 'OBJECT_OBSERVATION_REQUIRED')
        bpy.context.view_layer.update()
        bones = np.asarray(list(self.core.observe().values()), dtype='<f4')
        evaluated = self.body.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            points = np.empty(len(mesh.vertices) * 3, dtype='<f4')
            mesh.vertices.foreach_get('co', points)
            world = np.asarray(evaluated.matrix_world, dtype='<f4')
            points = points.reshape(-1, 3) @ world[:3, :3].T + world[:3, 3]
        finally:
            evaluated.to_mesh_clear()
        require(bool(np.isfinite(bones).all()) and bool(np.isfinite(points).all()), 'NONFINITE_EVALUATED_RESULT')
        return {'author': C.capture(), 'context': self._context(), 'rest': rest_arrays(self.rig.data),
                'lengths': bone_lengths(self.rig.data),
                'bones': bones, 'skin': points, 'rows': self._raw_all()}

    def _assert_state(self, actual, expected, code):
        checks = {'full_author_exact': actual['author'] == expected['author'],
            'full_author_digest_exact': digest(actual['author']) == digest(expected['author']),
            'context_exact': actual['context'] == expected['context'],
            'rest_exact': same_rest(actual['rest'], expected['rest']),
            'lengths_exact': same_array(actual['lengths'], expected['lengths']),
            'bones_exact': same_array(actual['bones'], expected['bones']),
            'skin_exact': same_array(actual['skin'], expected['skin']),
            'raw_rows_exact': actual['rows'] == expected['rows']}
        if not checks['context_exact']:
            require(False, code, {**checks, 'context_diff': C.diff(expected['context'], actual['context'], limit=12)})
        require(all(checks.values()), code, checks)
        return checks

    def _parent_preflight(self, request, consumers):
        """All unsupported parent requests refuse before _start/copy/relink."""
        bone = self.original_data.bones.get(request['bone'])
        require(bone is not None, 'PARENT_TARGET_MISSING')
        parent = bone.parent
        candidate = self.original_data.bones.get(request['to_parent'])
        require(candidate is not None, 'PARENT_CANDIDATE_MISSING')
        require(parent is not None and parent.name == request['from_parent'], 'PARENT_FROM_MISMATCH')
        require(candidate != bone and candidate != parent, 'PARENT_NO_STORED_EFFECT')
        require(parent.parent == candidate, 'PARENT_GRANDPARENT_ONLY')
        require(not bone.children and not bone.use_connect and bone.bbone_segments == 1, 'PARENT_LEAF_SCOPE')
        require(consumers['qualifiers']['rig_object_aliases'] == [self.rig.name], 'PARENT_SINGLE_ALIAS_REQUIRED')
        # This build exposes the three relevant flags only on EditBone.
        # PoseBone.select is a different flag domain and cannot certify them.
        # Full selection eligibility is checked in the owned Edit phase while
        # the complete enter receipt is still available for safe discard.
        names, current = set(), candidate
        while current is not None:
            require(current.name not in names and current != bone, 'PARENT_CYCLE')
            names.add(current.name); current = current.parent
        order = [b.name for b in self.original_data.bones]
        require(parent_forest_order(order, bone_topology(self.original_data)) == order
                and [b.name for b in self.rig.pose.bones] == order, 'PARENT_SOURCE_ORDER_UNSUPPORTED')

    def _freeze_parent_projection(self, raw, author):
        pending = self._pending; request = pending['request']; name = request['bone']
        row = raw[name]
        require(row['parent'] == request['from_parent'] and row['fields']['use_connect'] is False, 'PARENT_RAW_FROM_MISMATCH')
        parent = raw[request['from_parent']]
        require(parent['parent'] == request['to_parent'], 'PARENT_RAW_GRANDPARENT_MISMATCH')
        selection = {f: raw[request['to_parent']]['fields'][f] for f in ('select', 'select_head', 'select_tail')}
        require(all(v is False for v in selection.values()), 'PARENT_SELECTION_UNSUPPORTED')
        before = bone_topology(self.original_data)
        require(len(before) == len(raw) and {r['name']: r['parent'] for r in before} == {n: r['parent'] for n, r in raw.items()}, 'PARENT_RAW_TOPOLOGY_MISMATCH')
        for r in before:
            fields = raw[r['name']]['fields']
            require(all(fields[k] == r[k] for k in ('use_connect', 'use_deform', 'bbone_segments')), 'PARENT_RAW_TOPOLOGY_FIELDS')
        after = deepcopy(before)
        next(r for r in after if r['name'] == name)['parent'] = request['to_parent']
        raw_order = list(raw); old_order = parent_forest_order(raw_order, before)
        new_order = parent_forest_order(raw_order, after)
        pose_order = [r['name'] for r in block(author, self.rig)['pose']]
        require([r['name'] for r in block(author, pending['owned'])['bones']] == old_order == raw_order == pose_order, 'PARENT_SOURCE_ORDER_UNSUPPORTED')
        require([n for n in raw_order if n != name] == [n for n in new_order if n != name], 'PARENT_NON_TARGET_ORDER_CHANGED')
        snapshot = {'schema': 'hrig.rest-parent-snapshot.v1', 'bone': name,
            'head': deepcopy(row['fields']['head']), 'tail': deepcopy(row['fields']['tail']),
            'raw_roll': row['fields']['roll'], 'parent': row['parent'], 'use_connect': False,
            'grandparent': parent['parent'], 'grandparent_selection': selection}
        projection = {'raw_before': deepcopy(raw), 'raw_snapshot': snapshot, 'topology_before': before, 'topology_after': after,
            'raw_order_before': raw_order, 'source_order': old_order, 'reference_order': new_order,
            'source_pose_order': pose_order, 'reference_pose_order': list(new_order)}
        if pending['kind'] == 'preview':
            expected = self._snapshot['parent_projection']
            require(projection == expected and digest(projection) == digest(expected), 'PARENT_REFERENCE_PROJECTION_CHANGED')
        pending['parent_projection'] = deepcopy(projection)
        pending['parent_projection_sha256'] = digest(projection)
        return snapshot

    def _parent_projection(self):
        pending = self._pending; value = pending.get('parent_projection')
        require(type(value) is dict and digest(value) == pending.get('parent_projection_sha256'), 'PARENT_PROJECTION_CHANGED')
        return value

    def _owned_author(self, *, assigned, mirror=None, target_matrix=None, target_tail=None, target_head=None, parent_finalized=False):
        """Build complete expected C from source, with explicit typed writes.

        This first contract rejects original Armature outgoing ID references;
        it never infers arbitrary copied-ID user deltas or masks a whole tree.
        """
        pending = self._pending
        before = pending['baseline']['author']
        result = deepcopy(before)
        original = block(result, self.original_data)
        new_ident = {'type': 'Armature', 'name': pending['owned_name'], 'library': None}
        owned = deepcopy(original)
        owned['name'] = pending['owned_name']
        owned['fake_user'] = False
        owned['native_user_accounting']['users'] = int(assigned)
        settings = owned['character_extra']['settings']
        require(settings.get('name') == self._original_name, 'ARMATURE_NAME_OBSERVER_SCHEMA')
        settings['name'] = pending['owned_name']
        if 'use_fake_user' in settings:
            settings['use_fake_user'] = False
        if mirror is not None:
            require('use_mirror_x' in settings, 'MIRROR_OBSERVER_SCHEMA')
            settings['use_mirror_x'] = bool(mirror)
        if target_matrix is not None:
            rows = [row for row in owned['bones'] if row['name'] == pending['request']['bone']]
            require(len(rows) == 1, 'TARGET_AUTHOR_SCHEMA')
            rows[0]['matrix_local'] = deepcopy(target_matrix)
        parent_op = operation(pending['request']) == 'parent'
        if parent_op and parent_finalized:
            projection = self._parent_projection()
            require(assigned and target_matrix is not None and target_head is not None and target_tail is not None, 'PARENT_AUTHOR_SCOPE')
            name = pending['request']['bone']
            row = next(r for r in owned['bones'] if r['name'] == name)
            require(row['parent'] == pending['request']['from_parent'], 'PARENT_AUTHOR_FROM')
            row['parent'] = pending['request']['to_parent']
            fields = owned['character_extra']['bone_settings'][name]
            require(fields['parent'] == pending['request']['from_parent'], 'PARENT_AUTHOR_FROM')
            fields.update(parent=pending['request']['to_parent'], head=deepcopy(target_head), tail=deepcopy(target_tail))
            for record, key in ((owned, 'bones'), (block(result, self.rig), 'pose')):
                rows = record[key]
                require([r['name'] for r in rows] == projection['source_order'], 'PARENT_AUTHOR_SOURCE_ORDER')
                by_name = {r['name']: r for r in rows}
                order = projection['reference_order']
                require(len(by_name) == len(rows) == len(order) and set(by_name) == set(order), 'PARENT_AUTHOR_ROSTER')
                record[key] = [by_name[n] for n in order]
        elif parent_op:
            require(target_matrix is None and target_head is None and target_tail is None, 'PARENT_EDIT_STORED_AUTHOR_SCOPE')
        if target_tail is not None and not parent_op:
            require(operation(pending['request']) == 'tail', 'TAIL_AUTHOR_SCOPE')
            owned['character_extra']['bone_settings'][pending['request']['bone']]['tail'] = deepcopy(target_tail)
        if target_head is not None and not parent_op:
            require(operation(pending['request']) == 'head', 'HEAD_AUTHOR_SCOPE')
            owned['character_extra']['bone_settings'][pending['request']['bone']]['head'] = deepcopy(target_head)
        original['native_user_accounting']['users'] -= int(assigned)
        old_ident = identity(self.original_data)
        rig_record = block(result, self.rig)
        if assigned:
            require(rig_record['object']['data'] == old_ident
                    and rig_record['character_extra']['stored_settings'].get('data') == old_ident, 'RIG_DATA_TYPED_REFERENCE_SCHEMA')
            rig_record['object']['data'] = deepcopy(new_ident)
            rig_record['character_extra']['stored_settings']['data'] = deepcopy(new_ident)
            edge = {'user': identity(self.rig), 'target': old_ident}
            require(result['edges'].count(edge) == 1, 'RIG_DATA_EDGE_SCHEMA')
            result['edges'].remove(edge)
            result['edges'].append({'user': identity(self.rig), 'target': new_ident})
            result['edges'].sort(key=C.inspection.key)
        result['blocks'].append(owned)
        result['blocks'].sort(key=lambda row: C.inspection.key({key: row[key] for key in ('type', 'name', 'library', 'owner') if key in row}))
        return result

    def _owned_context(self, *, active=False, edit=False):
        context = deepcopy(self._pending['baseline']['context'])
        context['active_bones'][self._pending['owned_name']] = context['active_bones'][self._original_name]
        if active:
            context['active'] = self._rig_name
            context['selected'] = [self._rig_name] if context['rig_visible'] else []
            context['selected_raw'] = [self._rig_name] if context['rig_visible'] else []
        context['mode'] = 'EDIT_ARMATURE' if edit else 'OBJECT'
        return context

    def _predicted_copy_state(self, *, assigned, mirror=None, active=False):
        before = self._pending['baseline']
        return {**before, 'author': self._owned_author(assigned=assigned, mirror=mirror),
                'context': self._owned_context(active=active)}

    def _object_checkpoint(self, state):
        self._pending['checkpoint'] = {'kind': 'object', 'state': state}

    def _edit_raw(self):
        require(bpy.context.mode == 'EDIT_ARMATURE' and self.rig.data == self._pending['owned'], 'OWNED_EDIT_CONTEXT_REQUIRED')
        return {bone.name: {'fields': C.inspection.configuration(bone),
            'parent': bone.parent.name if bone.parent else None,
            'collections': sorted(item.name for item in getattr(bone, 'collections', ())),
            'bbone_custom_handle_start': getattr(getattr(bone, 'bbone_custom_handle_start', None), 'name', None),
            'bbone_custom_handle_end': getattr(getattr(bone, 'bbone_custom_handle_end', None), 'name', None)}
            for bone in self._pending['owned'].edit_bones}

    def _edit_checkpoint(self, author, context, raw):
        self._pending['checkpoint'] = {'kind': 'edit', 'author': author, 'context': context,
            'edit_raw': raw, 'rows': self._raw_all(), 'original_rest': rest_arrays(self.original_data), 'original_lengths': bone_lengths(self.original_data)}

    def _assert_checkpoint(self, *, _call=None):
        pending = self._pending
        require(pending is not None and pending.get('checkpoint') is not None, 'RECOVERY_CHECKPOINT_REQUIRED')
        if pending.get('transition') is not None:
            return self._prove_transition(_call=_call)
        self._guard(pending['baseline']['context']['frame'], allow_edit=True)
        checkpoint = pending['checkpoint']
        if checkpoint['kind'] == 'object':
            require(bpy.context.mode == 'OBJECT', 'RECOVERY_MODE_CHANGED')
            return self._assert_state(self._collect(), checkpoint['state'], 'CHECKPOINT_CHANGED')
        require(bpy.context.mode == 'EDIT_ARMATURE', 'RECOVERY_MODE_CHANGED')
        actual_context = self._context()
        author = C.capture()
        raw = self._edit_raw()
        checks = {'author_exact': author == checkpoint['author'], 'context_exact': actual_context == checkpoint['context'],
            'edit_raw_exact': raw == checkpoint['edit_raw'] and (operation(pending['request']) != 'head' or list(raw) == list(checkpoint['edit_raw'])), 'raw_rows_exact': self._raw_all() == checkpoint['rows'],
            'original_rest_exact': same_rest(rest_arrays(self.original_data), checkpoint['original_rest'])}
        checks['original_lengths_exact'] = same_array(bone_lengths(self.original_data), checkpoint['original_lengths'])
        if operation(pending['request']) == 'parent':
            checks['author_exact'] = checks['author_exact'] and digest(author) == digest(checkpoint['author'])
            checks['edit_raw_exact'] = checks['edit_raw_exact'] and list(raw) == list(checkpoint['edit_raw']) and digest(raw) == digest(checkpoint['edit_raw'])
        if not checks['context_exact']:
            require(False, 'EDIT_CHECKPOINT_CHANGED', {**checks,
                'context_diff': C.diff(checkpoint['context'], actual_context, limit=12)})
        require(all(checks.values()), 'EDIT_CHECKPOINT_CHANGED', checks)
        return checks

    def _check_rest_scope(self, actual, *, require_changed, lengths=None, parent_finalized=True):
        before = self._pending['baseline']['rest']
        require(actual['names'] == before['names'], 'REST_BONE_ROSTER_CHANGED')
        target = before['names'].index(self._pending['request']['bone'])
        edit_op = operation(self._pending['request'])
        if edit_op == 'parent':
            projection = self._parent_projection()
            require(lengths is not None and same_array(np.delete(lengths, target), np.delete(self._pending['baseline']['lengths'], target)), 'NON_TARGET_LENGTH_CHANGED')
            require(bool(np.isfinite(lengths).all()) and bool(lengths[target] > 0), 'NATIVE_LENGTH_INVALID')
            for key in ('head_tail_local', 'matrix_local', 'matrix_parent_relative'):
                require(bool(np.isfinite(actual[key]).all()), 'PARENT_NONFINITE_REST')
                require(same_array(np.delete(actual[key], target, axis=0), np.delete(before[key], target, axis=0)), 'PARENT_NON_TARGET_REST_CHANGED', key)
            if parent_finalized:
                require(bone_topology(self._pending['owned']) == projection['topology_after'], 'PARENT_FINAL_TOPOLOGY_CHANGED')
                require(self._pending['request']['bone'] not in self._protected_roles, 'ROLE_SEMANTIC_TARGET_UNSUPPORTED')
            else:
                require(same_rest(actual, before) and same_array(lengths, self._pending['baseline']['lengths']), 'PARENT_EDIT_STORED_REST_CHANGED')
            return
        if edit_op == 'roll':
            require(same_array(actual['head_tail_local'], before['head_tail_local']), 'HEAD_TAIL_CHANGED')
        else:
            require(lengths is not None and same_array(np.delete(lengths, target), np.delete(self._pending['baseline']['lengths'], target)), 'NON_TARGET_LENGTH_CHANGED')
            require(bool(np.isfinite(lengths[target])) and bool(lengths[target] > 0), 'NATIVE_LENGTH_INVALID')
            moving, protected = (slice(3,6), slice(0,3)) if edit_op == 'tail' else (slice(0,3), slice(3,6))
            require(same_array(actual['head_tail_local'][:, protected], before['head_tail_local'][:, protected]), 'TAIL_HEAD_CHANGED' if edit_op == 'tail' else 'HEAD_TAIL_CHANGED')
            require(same_array(np.delete(actual['head_tail_local'][:, moving], target, axis=0),
                np.delete(before['head_tail_local'][:, moving], target, axis=0)), 'NON_TARGET_' + edit_op.upper() + '_CHANGED')
            require(bone_topology(self._pending['owned']) == bone_topology(self.original_data), edit_op.upper() + '_TOPOLOGY_CHANGED')
            if require_changed:
                require(actual['head_tail_local'][target, moving].tobytes() != before['head_tail_local'][target, moving].tobytes(), 'NO_FINAL_' + edit_op.upper() + '_EFFECT')
        for key in ('matrix_local', 'matrix_parent_relative'):
            different = np.any((actual[key].view('<u4') != before[key].view('<u4')).reshape(len(before['names']), -1), axis=1)
            require(not bool(np.any(np.delete(different, target))), 'NON_TARGET_REST_CHANGED', {'field': key, 'names': [before['names'][i] for i in np.flatnonzero(different)]})
            if require_changed:
                require(bool(different[target]), 'NO_FINAL_REST_EFFECT', key)
        for name in self._protected_roles:
            require(name != self._pending['request']['bone'], 'ROLE_SEMANTIC_TARGET_UNSUPPORTED', name)
            index = before['names'].index(name)
            require(all(actual[key][index].tobytes() == before[key][index].tobytes()
                        for key in ('matrix_local', 'matrix_parent_relative', 'head_tail_local')), 'ROLE_REST_CHANGED', name)

    def _assert_declared_author(self, author, *, mirror, target_matrix=None, target_tail=None, target_head=None, parent_finalized=False):
        expected = self._owned_author(assigned=True, mirror=mirror, target_matrix=target_matrix, target_tail=target_tail, target_head=target_head, parent_finalized=parent_finalized)
        require(author == expected and digest(author) == digest(expected), 'NON_TARGET_AUTHOR_CHANGED', C.diff(expected, author, limit=35))

    def _certify_final_tail(self, state):
        """Narrow, qualified endpoint policy; matrices/lengths use native forward.

        A new input with a different native endpoint representation is rejected,
        not silently normalized or taken as general evidence of raw/final equality.
        Discard-only recovery does not call this applied-state certificate.
        """
        pending = self._pending
        if operation(pending['request']) != 'tail':
            return
        name = pending['request']['bone'];plan = pending['plan']
        i = state['rest']['names'].index(name)
        endpoints = np.asarray(plan['protected']['head'] + plan['raw_after'], dtype='<f4')
        require(state['rest']['head_tail_local'][i].tobytes() == endpoints.tobytes(), 'TAIL_FINAL_ENDPOINT_REPRESENTATION_UNSUPPORTED')
        authored = block(state['author'], pending['owned'])['character_extra']['bone_settings'][name]
        require(np.asarray(authored['head'] + authored['tail'], dtype='<f4').tobytes() == endpoints.tobytes(), 'TAIL_FINAL_AUTHOR_ENDPOINT_MISMATCH')

    def _certify_final_head(self, state):
        """Head-only applied policy established by its own finite native probe.

        Recovery/discard does not depend on this application certificate.
        """
        pending = self._pending
        if operation(pending['request']) != 'head':
            return
        name = pending['request']['bone']; plan = pending['plan']
        i = state['rest']['names'].index(name)
        endpoints = np.asarray(plan['raw_after'] + plan['protected']['tail'], dtype='<f4')
        require(state['rest']['head_tail_local'][i].tobytes() == endpoints.tobytes(), 'HEAD_FINAL_ENDPOINT_REPRESENTATION_UNSUPPORTED')
        authored = block(state['author'], pending['owned'])['character_extra']['bone_settings'][name]
        require(np.asarray(authored['head'] + authored['tail'], dtype='<f4').tobytes() == endpoints.tobytes(), 'HEAD_FINAL_AUTHOR_ENDPOINT_MISMATCH')

    def _certify_final_parent(self, state):
        if operation(self._pending['request']) != 'parent':
            return
        projection = self._parent_projection()
        self._check_rest_scope(state['rest'], lengths=state['lengths'], require_changed=False)
        author = state['author']
        require([r['name'] for r in block(author, self._pending['owned'])['bones']] == projection['reference_order']
                and [r['name'] for r in block(author, self.rig)['pose']] == projection['reference_pose_order'], 'PARENT_FINAL_ORDER_CHANGED')
        name = self._pending['request']['bone']; index = state['rest']['names'].index(name)
        record = block(author, self._pending['owned'])
        authored = record['character_extra']['bone_settings'][name]
        require(np.asarray(authored['head'] + authored['tail'], dtype='<f4').tobytes()
                == state['rest']['head_tail_local'][index].tobytes(), 'PARENT_FINAL_AUTHOR_ENDPOINT_MISMATCH')
        matrix = next(r['matrix_local'] for r in record['bones'] if r['name'] == name)
        require(np.asarray(matrix, dtype='<f4').tobytes() == state['rest']['matrix_local'][index].tobytes(), 'PARENT_FINAL_AUTHOR_MATRIX_MISMATCH')
        require(data_rest_signature(self._pending['owned']) != self._profile['rig']['rest_sha256'], 'PARENT_NO_STORED_EFFECT')

    def _create_owned(self):
        pending = self._pending
        self._guard(pending['baseline']['context']['frame'])
        require(bpy.data.armatures.get(pending['owned_name']) is None, 'OWNED_NAME_EXISTS')
        self.write_count += 1
        owned = self.original_data.copy()
        pending['owned'] = owned
        require(hasattr(owned, 'session_uid'), 'ID_LIFETIME_IDENTITY_UNAVAILABLE')
        pending['owned_session_uid'] = int(owned.session_uid)
        owned.name = pending['owned_name']
        owned.use_fake_user = False
        require(owned.name == pending['owned_name'], 'OWNED_NAME_CHANGED')
        pending['phase'] = 'copied'
        predicted = self._predicted_copy_state(assigned=False)
        self._object_checkpoint(predicted)
        actual = self._collect()
        self._assert_state(actual, predicted, 'OWNED_COPY_CHANGED_SOURCE')
        self._object_checkpoint(actual)
        self._guard(pending['baseline']['context']['frame'])
        self.write_count += 1
        self.rig.data = owned
        pending['bound_owned'] = True
        pending['phase'] = 'relinked'
        predicted = self._predicted_copy_state(assigned=True)
        self._object_checkpoint(predicted)
        self._guard(pending['baseline']['context']['frame'])
        actual = self._collect()
        self._assert_state(actual, predicted, 'OWNED_RELINK_CHANGED_SOURCE')
        self._object_checkpoint(actual)

    def _enter_and_write(self, frozen_plan=None):
        pending = self._pending
        # _create_owned just captured/verified this complete Object state.
        # Recheck the mutable boundary, without recapturing the same C twice.
        self._guard(pending['baseline']['context']['frame'])
        owned = pending['owned']
        owned.use_mirror_x = False
        require(owned.use_mirror_x is False, 'MIRROR_DISABLE_FAILED')
        layer = bpy.context.view_layer
        for obj in layer.objects:
            if obj.select_get(view_layer=layer):
                obj.select_set(False, view_layer=layer)
        if pending['baseline']['context']['rig_visible']:
            self.rig.select_set(True, view_layer=layer)
        bpy.context.view_layer.objects.active = self.rig
        predicted = self._predicted_copy_state(assigned=True, mirror=False, active=True)
        self._object_checkpoint(predicted)
        self._assert_state(self._collect(), predicted, 'EDIT_PREPARATION_CHANGED_SOURCE')
        self._guard(pending['baseline']['context']['frame'])
        require(self.rig.data == owned and owned != self.original_data, 'OWNED_EDIT_CONTEXT_REQUIRED')
        self._transition_begin('enter')
        self.write_count += 1
        outcome = bpy.ops.object.mode_set(mode='EDIT')
        self._transition_post(outcome)
        if 'FINISHED' in outcome and bpy.context.mode == 'EDIT_ARMATURE' and self.rig.data == owned:
            self._call_transition_gap_hook('enter')
        require('FINISHED' in outcome and bpy.context.mode == 'EDIT_ARMATURE' and self.rig.data == owned,
                'EDIT_ENTER_FAILED', {'operator': sorted(outcome), 'mode': bpy.context.mode})
        self._guard(pending['baseline']['context']['frame'], allow_edit=True)
        require(owned.use_mirror_x is False, 'MIRROR_REENABLED')
        author, context = C.capture(), self._context()
        self._assert_declared_author(author, mirror=False)
        require(context == self._owned_context(active=True, edit=True), 'EDIT_CONTEXT_CHANGED')
        raw_before = self._edit_raw()
        edit_op = operation(pending['request'])
        parent_plan = None
        if edit_op == 'parent':
            # Do not commit the enter transition before every Edit-only
            # eligibility check succeeds. A refusal here has zero parent
            # assignments and can use the unchanged enter discard proof.
            parent_snapshot = self._freeze_parent_projection(raw_before, author)
            parent_plan = plan_parent(parent_snapshot, pending['request'])
        self._edit_checkpoint(author, context, raw_before)
        self._transition_commit('enter')
        edit_bone = owned.edit_bones[pending['request']['bone']]
        edit_op = operation(pending['request'])
        if edit_op == 'roll':
            plan = plan_roll({'schema': 'hrig.rest-roll-snapshot.v1', 'bone': edit_bone.name, 'raw_roll': float(edit_bone.roll)}, pending['request'])
        elif edit_op == 'tail':
            plan = plan_tail({'schema': 'hrig.rest-tail-snapshot.v1', 'bone': edit_bone.name,
                'head': list(edit_bone.head), 'tail': list(edit_bone.tail), 'raw_roll': float(edit_bone.roll),
                'parent': edit_bone.parent.name if edit_bone.parent else None, 'use_connect': bool(edit_bone.use_connect)}, pending['request'])
        elif edit_op == 'head':
            plan = plan_head({'schema': 'hrig.rest-head-snapshot.v1', 'bone': edit_bone.name,
                'head': list(edit_bone.head), 'tail': list(edit_bone.tail), 'raw_roll': float(edit_bone.roll),
                'parent': edit_bone.parent.name if edit_bone.parent else None, 'use_connect': bool(edit_bone.use_connect)}, pending['request'])
        elif edit_op == 'parent':
            require(not edit_bone.children and not edit_bone.use_connect, 'PARENT_LEAF_SCOPE')
            plan = parent_plan
        if frozen_plan is not None:
            require(plan == frozen_plan, 'REFERENCE_RAW_PLAN_CHANGED')
        pending['plan'] = deepcopy(plan)
        self._guard(pending['baseline']['context']['frame'], allow_edit=True)
        self._transition_begin(edit_op)
        self.write_count += 1
        if edit_op == 'roll':
            edit_bone.roll = plan['raw_after']
        elif edit_op == 'tail':
            edit_bone.tail = plan['raw_after']
        elif edit_op == 'head':
            edit_bone.head = plan['raw_after']
        elif edit_op == 'parent':
            candidate = owned.edit_bones.get(plan['raw_after'])
            require(candidate is not None and candidate.id_data == owned and edit_bone.id_data == owned, 'PARENT_SAME_OWNED_REQUIRED')
            require(edit_bone.parent is not None and edit_bone.parent.name == plan['raw_before']
                    and edit_bone.parent.parent == candidate, 'PARENT_NATIVE_FROM_CHANGED')
            require(all(getattr(candidate, f) is False for f in ('select', 'select_head', 'select_tail')), 'PARENT_SELECTION_UNSUPPORTED')
            edit_bone.parent = candidate
            require(edit_bone.parent == candidate and edit_bone.parent.name == plan['raw_after'], 'PARENT_NATIVE_SET_REJECTED')
        self._transition_post()
        self._call_transition_gap_hook(edit_op)
        raw_after = self._edit_raw()
        self._transition_raw_scope(raw_before, raw_after, plan)
        author, context = C.capture(), self._context()
        matrix = block(author, owned)['bones']
        matrix = next(row['matrix_local'] for row in matrix if row['name'] == edit_bone.name)
        tail = block(author, owned)['character_extra']['bone_settings'][edit_bone.name]['tail'] if edit_op == 'tail' else None
        head = block(author, owned)['character_extra']['bone_settings'][edit_bone.name]['head'] if edit_op == 'head' else None
        if edit_op == 'parent':
            matrix = None  # stored Bone/Pose author is unchanged while in Edit.
        self._assert_declared_author(author, mirror=False, target_matrix=matrix, target_tail=tail, target_head=head)
        require(context == self._owned_context(active=True, edit=True), 'EDIT_CONTEXT_CHANGED')
        require(self._raw_all() == pending['baseline']['rows'], 'BODY_RAW_CHANGED')
        self._check_rest_scope(rest_arrays(owned), lengths=bone_lengths(owned), require_changed=False, parent_finalized=False)
        self._edit_checkpoint(author, context, raw_after)
        self._transition_commit(edit_op)
        pending['phase'] = edit_op + '_written'
        return plan

    def _exit_edit(self, *, discard_only=False):
        pending = self._pending
        require(type(discard_only) is bool and (not discard_only or operation(pending['request']) in ('head', 'parent')),
                'PARENT_DISCARD_OPERATION_REQUIRED' if operation(pending['request']) == 'parent' else 'HEAD_DISCARD_OPERATION_REQUIRED')
        self._assert_checkpoint()
        self._guard(pending['baseline']['context']['frame'], allow_edit=True)
        require(bpy.context.mode == 'EDIT_ARMATURE' and self.rig.data == pending['owned'], 'OWNED_EDIT_CONTEXT_REQUIRED')
        if pending.get('exit_blocked'):
            pending['phase'] = 'recovery_edit'
            raise RestEditError('EDIT_EXIT_CANCELLED', {'operator': ['CANCELLED'], 'mode': bpy.context.mode,
                'owned_name': pending['owned_name'], 'private_fixture': True})
        require(not pending.get('exit_unproven'), 'EDIT_EXIT_REQUIRES_EXPLICIT_RESTORE',
                'An unproven native exit cannot be retried by automatic cleanup')
        self._transition_begin('exit')
        self.write_count += 1
        outcome = bpy.ops.object.mode_set(mode='OBJECT')
        self._transition_post(outcome)
        if 'FINISHED' in outcome and bpy.context.mode == 'OBJECT' and self.rig.data == pending['owned']:
            self._call_transition_gap_hook('exit')
        if not ('FINISHED' in outcome and bpy.context.mode == 'OBJECT' and self.rig.data == pending['owned']):
            pending['exit_unproven'] = True
            raise RestEditError('EDIT_EXIT_UNPROVEN', {'operator': sorted(outcome), 'mode': bpy.context.mode})
        pending['normal_exit'] = {'operator_result': sorted(outcome), 'context_mode': bpy.context.mode, 'still_owned': True}
        self._guard(pending['baseline']['context']['frame'])
        pending['transition']['tail_mirror'] = {'before': bool(pending['owned'].use_mirror_x), 'after': pending['mirror_flag']}
        pending['owned'].use_mirror_x = pending['mirror_flag']
        require(bool(pending['owned'].use_mirror_x) == pending['mirror_flag'], 'MIRROR_RESTORE_FAILED')
        after = self._collect()
        self._check_rest_scope(after['rest'], lengths=after['lengths'], require_changed=True)
        target = next(row['matrix_local'] for row in block(after['author'], pending['owned'])['bones']
                      if row['name'] == pending['request']['bone'])
        tail = block(after['author'], pending['owned'])['character_extra']['bone_settings'][pending['request']['bone']]['tail'] if operation(pending['request']) == 'tail' else None
        head = block(after['author'], pending['owned'])['character_extra']['bone_settings'][pending['request']['bone']]['head'] if operation(pending['request']) == 'head' else None
        parent_op = operation(pending['request']) == 'parent'
        if parent_op:
            fields = block(after['author'], pending['owned'])['character_extra']['bone_settings'][pending['request']['bone']]
            head, tail = fields['head'], fields['tail']
        self._assert_declared_author(after['author'], mirror=pending['mirror_flag'], target_matrix=target, target_tail=tail, target_head=head, parent_finalized=parent_op)
        self._certify_final_tail(after)
        if not discard_only:
            self._certify_final_head(after)
            self._certify_final_parent(after)
        require(after['context'] == self._owned_context(active=True), 'AFTER_EDIT_CONTEXT_CHANGED')
        require(after['rows'] == pending['baseline']['rows'], 'BODY_RAW_CHANGED')
        require(after['skin'].shape == pending['baseline']['skin'].shape, 'SKIN_TOPOLOGY_CHANGED')
        self._object_checkpoint(after)
        self._transition_commit('exit')
        pending['phase'] = 'discard_ready' if discard_only else 'applied'
        return after

    def _restore_context(self, expected):
        require(bpy.context.mode == 'OBJECT', 'RESTORE_CONTEXT_MODE')
        layer = bpy.context.view_layer
        for obj in layer.objects:
            if obj.select_get(view_layer=layer):
                obj.select_set(False, view_layer=layer)
        for name in expected['selected_raw']:
            require(layer.objects.get(name) is not None, 'RESTORE_SELECTION_IDENTITY', name)
            layer.objects[name].select_set(True, view_layer=layer)
        bpy.context.view_layer.objects.active = bpy.data.objects.get(expected['active']) if expected['active'] else None
        bpy.context.view_layer.update()

    def _rollback(self, *, _proof=None, _call=None):
        if _call is None:
            require(_proof is None, 'RECOVERY_CALL_REQUIRED')
            with _RecoveryCall(self) as call:
                return self._rollback(_call=call)
        require(_call.active and _call.owner is self, 'RECOVERY_CALL_EXPIRED')
        pending = self._pending
        require(pending is not None, 'RESTORE_TOKEN_REQUIRED')
        if pending.get('transition') is not None:
            if _proof is None:
                _proof = self._prove_transition(_call=_call)
            self._require_transition_proof(_proof, _call)
            if bpy.context.mode == 'EDIT_ARMATURE':
                self._discard_transition_exit(_proof=_proof, _call=_call)
            # The previous verified checkpoint remains unchanged. Eligibility
            # above is solely a bounded discard proof, never an applied state.
        elif bpy.context.mode == 'EDIT_ARMATURE':
            # _exit_edit verifies the complete Edit checkpoint before writing.
            # Head/parent discard retains scope/current fences without granting
            # application qualification for this transient exit.
            self._exit_edit(discard_only=operation(pending['request']) in ('head', 'parent'))
        else:
            self._assert_checkpoint()  # External Object/author/array drift.
        self._guard(pending['baseline']['context']['frame'])
        require(bpy.context.mode == 'OBJECT', 'ORIGINAL_SWAP_MODE_UNPROVEN')
        owned = pending['owned']
        if pending['bound_owned']:
            require(self.rig.data == owned and owned != self.original_data, 'ORIGINAL_SWAP_BINDING_UNPROVEN')
            self.write_count += 1
            self.rig.data = self.original_data
            pending['bound_owned'] = False
        require(self.rig.data == self.original_data and bpy.context.mode == 'OBJECT', 'ORIGINAL_SWAP_FAILED')
        self._guard(pending['baseline']['context']['frame'])
        if owned is not None:
            require(bpy.data.armatures.get(pending['owned_name']) == owned and owned != self.original_data
                    and owned.users == 0, 'OWNED_CLEANUP_DOMAIN')
            self.write_count += 1
            bpy.data.armatures.remove(owned)
            pending['owned'] = None
        self._guard(pending['baseline']['context']['frame'])
        self._restore_context(pending['baseline']['context'])
        self._guard(pending['baseline']['context']['frame'])
        restored = self._collect()
        proof = self._assert_state(restored, pending['baseline'], 'RESTORE_NOT_EXACT')
        self._pending = None
        return {'status': 'restored_exact', **proof, 'author_sha256': digest(restored['author']),
                'frame': restored['context']['frame'], 'write_count': self.write_count}

    def _fail_and_restore(self, error):
        try:
            restored = self._rollback()
        except Exception as recovery:
            if self._pending is not None:
                self._pending['phase'] = 'recovery_edit' if bpy.context.mode == 'EDIT_ARMATURE' else 'recovery_required'
                # Preserve the last verified checkpoint; never replace it with
                # a new snapshot of untrusted/external drift during failure.
            raise RestEditError('RESTORE_REQUIRED', {'cause': error_record(error), 'recovery': error_record(recovery),
                'pending': self.pending}) from error
        self._snapshot = None
        raise RestEditError(getattr(error, 'code', 'REST_EDIT_FAILED'), {'cause': error_record(error), 'rollback': restored}) from error

    def _start(self, kind, baseline, request, owned_name):
        require(self._pending is None, 'PREVIEW_ALREADY_PENDING')
        # Established BEFORE copy/relink/Edit or any original user-edge write.
        self._pending = {'token': 'rest_restore_' + uuid.uuid4().hex, 'phase': 'starting', 'kind': kind,
            'baseline': baseline, 'request': deepcopy(request), 'owned_name': owned_name, 'owned': None,
            'bound_owned': False, 'mirror_flag': bool(self.original_data.use_mirror_x),
            'checkpoint': {'kind': 'object', 'state': baseline}, 'exit_blocked': False, 'plan': None,
            'transition': None, 'transition_seq': 0, 'step_trace': {}}

    def _old_profile_rejections(self):
        try:
            AtomicControls(deepcopy(self._profile))
        except Exception as error:
            require(getattr(error, 'code', None) == 'RIG_DATA_IDENTITY', 'OLD_PROFILE_REJECTION_CHANGED', error_record(error))
            identity_code = error.code
        else:
            raise RestEditError('OLD_PROFILE_ACCEPTED_NEW_DATA')
        try:
            self.core.preflight([{'role': self._profile['roles'][0]['role_id'], 'value': 0.0}])
        except Exception as error:
            require(getattr(error, 'code', None) == 'REST_BINDING_DRIFT', 'OLD_REST_PROFILE_REJECTION_CHANGED', error_record(error))
            rest_code = error.code
        else:
            raise RestEditError('OLD_PROFILE_ACCEPTED_NEW_REST')
        return identity_code, rest_code

    def inspect(self, request):
        require(self._pending is None, 'PREVIEW_ALREADY_PENDING')
        request = validate_request(request)
        self._snapshot = None
        self._guard()
        require(bpy.context.scene.frame_current_final == bpy.context.scene.frame_current, 'INTEGER_OBSERVATION_FRAME_REQUIRED')
        require(request['bone'] not in self._protected_roles, 'ROLE_SEMANTIC_TARGET_UNSUPPORTED', request['bone'])
        from .consumers import scan
        consumers = scan(self.rig, self.body, deepcopy(self._profile), request['bone'])
        require(consumers.get('admissible') is True and consumers.get('blockers') == [], 'REST_EDIT_CONSUMER_BLOCKED', consumers)
        if operation(request) in ('tail', 'head', 'parent'):
            skins = consumers['skin_consumers']
            require(bool(skins) and all(row['object'] == self.body.name and row['mesh'] == self._body_mesh.name
                and row['use_vertex_groups'] is True and row['use_bone_envelopes'] is False for row in skins),
                operation(request).upper() + '_UNDECLARED_SKIN_CONSUMER', skins)
        if operation(request) == 'parent':
            self._parent_preflight(request, consumers)
        before = self._collect()
        old_id = identity(self.original_data)
        outgoing = [edge for edge in before['author']['edges'] if edge['user'] == old_id]
        require(not outgoing, 'ARMATURE_REFERENCE_COPY_UNSUPPORTED', outgoing)
        name = 'HRIG-rest-owned-' + uuid.uuid4().hex
        self._start('reference', before, request, name)
        try:
            self._create_owned()
            reference_session_uid = self._pending['owned_session_uid']
            plan = self._enter_and_write()
            after = self._exit_edit()
            identity_code, rest_code = self._old_profile_rejections()
            reference_new_rest = data_rest_signature(self.rig.data)
            require(reference_new_rest != self._profile['rig']['rest_sha256'], 'NO_REST_SIGNATURE_EFFECT')
            reference_plan = deepcopy(plan)
            reference_steps = deepcopy(self._pending['step_trace'])
            reference_parent_projection = deepcopy(self._pending.get('parent_projection'))
            restored = self._rollback()
            self._guard(before['context']['frame'])
        except Exception as error:
            if self._pending is not None:
                self._fail_and_restore(error)
            raise
        token = 'rest_snapshot_' + uuid.uuid4().hex
        self._snapshot = {'token': token, 'before': before, 'after': after, 'request': deepcopy(request),
            'plan': reference_plan, 'owned_name': name, 'consumers': deepcopy(consumers),
            'reference_session_uid': reference_session_uid, 'reference_restored': restored,
            'reference_steps': reference_steps,
            'new_rest_sha256': reference_new_rest,
            'old_profile_rejection': identity_code, 'old_bound_profile_rest_rejection': rest_code}
        if operation(request) == 'parent':
            self._snapshot['parent_projection'] = reference_parent_projection
        return deepcopy({'schema': 'hrig.rest-edit-inspection.v1', 'status': 'inspected', 'snapshot_token': token,
            'plan': reference_plan, 'evidence': self._evidence(actual_exact=False),
            'consumer_scan': consumers, 'qualification': 'inspect executed and exactly restored an independent owned native reference projection'})

    def preview(self, snapshot_token, fault=None):
        require(fault in (None, 'after_roll', 'after_tail', 'after_head', 'after_parent', 'exit_cancelled'), 'FAULT_SCHEMA')
        require(self._pending is None and self._snapshot is not None and snapshot_token == self._snapshot['token'], 'STALE_SNAPSHOT')
        snapshot = self._snapshot
        edit_op = operation(snapshot['request'])
        require(fault not in ('after_roll', 'after_tail', 'after_head', 'after_parent') or fault == 'after_' + edit_op, 'FAULT_OPERATION_MISMATCH')
        self._guard(snapshot['before']['context']['frame'])
        self._assert_state(self._collect(), snapshot['before'], 'LIVE_SNAPSHOT_CHANGED')
        self._start('preview', snapshot['before'], snapshot['request'], snapshot['owned_name'])
        try:
            self._create_owned()
            # Native session_uid distinguishes ID lifetimes even if an address
            # allocator reuses the removed reference's memory for the new copy.
            require(self._pending['owned_session_uid'] != snapshot['reference_session_uid'], 'REFERENCE_RNA_REUSED')
            self._pending['reference_id_was_removed'] = True
            self._enter_and_write(snapshot['plan'])
            if fault == 'after_' + edit_op:
                raise RestEditError('INJECTED_AFTER_' + edit_op.upper(), 'Private fixture after actual owned EditBone ' + edit_op + ' write')
            if fault == 'exit_cancelled':
                self._pending['exit_blocked'] = True
            after = self._exit_edit()
            self._assert_state(after, snapshot['after'], 'REFERENCE_REPLAY_MISMATCH')
            self._old_profile_rejections()
            self._pending['actual_reference_exact'] = True
            delta = np.linalg.norm(after['skin'] - snapshot['before']['skin'], axis=1)
            return deepcopy({'schema': 'hrig.rest-edit-preview.v1', 'status': 'previewed',
                'restore_token': self._pending['token'], 'author_sha256': digest(after['author']),
                'rest_array_sha256': rest_digest(after['rest']), 'new_rest_sha256': snapshot['new_rest_sha256'],
                'actual_reference_exact': True, 'roles_rest_exact': True, 'original_data_protected': True,
                'skin_finite': True, 'skin_changed': int(np.count_nonzero(delta)),
                'skin_max_delta': float(delta.max()) if delta.size else 0., 'write_count': self.write_count,
                'quality_qualification': 'observed rest edit only; no artistic, cross-rest rebind or performance certificate'})
        except Exception as error:
            self._fail_and_restore(error)

    def validate_applied(self):
        require(self._pending is not None and self._pending['phase'] == 'applied'
                and self._pending.get('actual_reference_exact') is True and self._snapshot is not None, 'NO_APPLIED_PREVIEW')
        self._guard(self._snapshot['before']['context']['frame'])
        actual = self._collect()
        self._assert_state(actual, self._snapshot['after'], 'APPLIED_STATE_CHANGED')
        self._certify_final_tail(actual)
        self._certify_final_head(actual)
        self._certify_final_parent(actual)
        require(data_rest_signature(self.rig.data) == self._snapshot['new_rest_sha256'], 'APPLIED_REST_CHANGED')
        return deepcopy({'status': 'applied_valid', 'author_sha256': digest(actual['author']),
            'rest_array_sha256': rest_digest(actual['rest']), 'bones_sha256': array_digest(actual['bones']),
            'skin_sha256': array_digest(actual['skin']), 'raw_rows_sha256': raw_digest(actual['rows']),
            'frame': actual['context']['frame'], 'actual_reference_exact': True})

    def _evidence(self, actual_exact):
        snapshot = self._snapshot
        after, before = snapshot['after'], snapshot['before']
        original_record = block(before['author'], self.original_data)
        edit_op = operation(snapshot['request'])
        value = {'schema': _EVIDENCE_SCHEMAS[edit_op], 'source': deepcopy(self._source),
            'source_profile': {'profile_id': self._profile['profile_id'], 'semantic_sha256': self._profile_digest,
                               'rest_sha256': self._profile['rig']['rest_sha256']},
            'request': deepcopy(snapshot['request']), 'request_sha256': digest(snapshot['request']),
            'rig': {'object': self._rig_name, 'original_data': self._original_name, 'owned_data': snapshot['owned_name']},
            'body': self._body_name, 'frame': int(before['context']['frame']),
            'old_rest_sha256': self._profile['rig']['rest_sha256'], 'new_rest_sha256': snapshot['new_rest_sha256'],
            'roles_rest_exact': True, 'role_count': len(self._profile['roles']),
            'unique_role_bones': len({row['bone'] for row in self._profile['roles']}),
            'source_original_armature_sha256': digest(original_record),
            'source_original_native_users': original_record['native_user_accounting']['users'],
            'target_bones': [snapshot['request']['bone']], 'role_rest_protected_bones': list(self._protected_roles),
            'plan': deepcopy(snapshot['plan']) if edit_op in ('tail', 'head', 'parent') else {key: deepcopy(snapshot['plan'][key]) for key in ('raw_before', 'raw_after', 'stored_delta', 'units', 'snapshot_sha256')},
            'reference': {'author_sha256': digest(after['author']), 'rest_array_sha256': rest_digest(after['rest']),
                'bones_sha256': array_digest(after['bones']), 'skin_sha256': array_digest(after['skin']), 'raw_rows_sha256': raw_digest(after['rows'])},
            'proof': {'reference_restored_exact': True, 'actual_reference_exact': bool(actual_exact),
                'original_data_protected': True, 'mirror_flag_restored': True,
                'old_profile_rejection': snapshot['old_profile_rejection'],
                'old_bound_profile_rest_rejection': snapshot['old_bound_profile_rest_rejection'], 'scan_admissible': True},
            'qualification': 'one declared owned DEF-leaf ' + edit_op + ' intent; explicit new control identity requires independent saved/cold validation; not cross-rest skin rebind'}
        if edit_op == 'tail':
            index = after['rest']['names'].index(snapshot['request']['bone'])
            value['tail_protection'] = {'schema': 'hrig.rest-tail-protection.v2',
                'endpoint_policy': 'qualified_bit_exact_raw_endpoints',
                'topology': bone_topology(self.original_data),
                'reference_target_head_tail_local': after['rest']['head_tail_local'][index].tolist(),
                'raw_target_length_after': float(snapshot['reference_steps']['tail']['edit_raw'][snapshot['request']['bone']]['fields']['length']),
                'reference_target_length': float(after['lengths'][index]),
                'source_lengths_sha256': array_digest(before['lengths']),
                'reference_lengths_sha256': array_digest(after['lengths']),
                'authored_write': 'one EditBone.tail in ARMATURE_LOCAL; raw head/roll/parent/connect exact',
                'derived_fields': ['EditBone.length', 'EditBone.matrix', 'Bone.tail_local', 'Bone.length', 'Bone.matrix_local', 'Bone.matrix']}
        elif edit_op == 'head':
            index = after['rest']['names'].index(snapshot['request']['bone'])
            plan = snapshot['plan']; protected = plan['protected']
            value['raw_snapshot'] = {'schema': 'hrig.rest-head-snapshot.v1', 'bone': snapshot['request']['bone'],
                'head': deepcopy(plan['raw_before']), 'tail': deepcopy(protected['tail']), 'raw_roll': protected['raw_roll'],
                'parent': protected['parent'], 'use_connect': protected['use_connect']}
            value['head_protection'] = {'schema': 'hrig.rest-head-protection.v1',
                'endpoint_policy': 'qualified_bit_exact_raw_endpoints',
                'topology': bone_topology(self.original_data),
                'reference_target_head_tail_local': after['rest']['head_tail_local'][index].tolist(),
                'raw_target_length_after': float(snapshot['reference_steps']['head']['edit_raw'][snapshot['request']['bone']]['fields']['length']),
                'reference_target_length': float(after['lengths'][index]),
                'source_lengths_sha256': array_digest(before['lengths']),
                'reference_lengths_sha256': array_digest(after['lengths']),
                'authored_write': 'one EditBone.head in ARMATURE_LOCAL; raw tail/roll/parent/connect exact',
                'derived_fields': ['EditBone.length', 'EditBone.matrix', 'Bone.head_local', 'Bone.length', 'Bone.matrix_local', 'Bone.matrix']}
        elif edit_op == 'parent':
            projection = snapshot['parent_projection']; name = snapshot['request']['bone']
            index = after['rest']['names'].index(name)
            value['raw_snapshot'] = deepcopy(projection['raw_snapshot'])
            value['parent_protection'] = {
                'schema': 'hrig.rest-parent-protection.v1',
                'topology_before': deepcopy(projection['topology_before']),
                'topology_after': deepcopy(projection['topology_after']),
                'source_topology_sha256': digest(projection['topology_before']),
                'reference_topology_sha256': digest(projection['topology_after']),
                'raw_order_before': list(projection['raw_order_before']),
                'reference_order': list(projection['reference_order']),
                'source_pose_order': list(projection['source_pose_order']),
                'reference_pose_order': list(projection['reference_pose_order']),
                'raw_target_length_after': float(snapshot['reference_steps']['parent']['edit_raw'][name]['fields']['length']),
                'reference_target_head_tail_local': after['rest']['head_tail_local'][index].tolist(),
                'reference_target_length': float(after['lengths'][index]),
                'source_lengths_sha256': array_digest(before['lengths']),
                'reference_lengths_sha256': array_digest(after['lengths']),
                'authored_write': {'bone': name, 'parent_before': snapshot['request']['from_parent'], 'parent_after': snapshot['request']['to_parent']},
                'derived_fields': ['native_target_rest', 'native_forest_order:armature.bones', 'native_forest_order:object.pose']}
        return value

    def binding_evidence(self):
        self.validate_applied()
        return deepcopy(self._evidence(actual_exact=True))

    def restore(self, restore_token):
        require(self._pending is not None and restore_token == self._pending['token'], 'RESTORE_TOKEN_REQUIRED')
        try:
            with _RecoveryCall(self) as call:
                proof = self._assert_checkpoint(_call=call)
                # Only explicit recovery releases cancelled fixture metadata.
                # These two fields do not change native/author evidence.
                self._pending['exit_blocked'] = False
                self._pending['exit_unproven'] = False
                restored = self._rollback(_proof=proof if self._pending.get('transition') is not None else None,
                                          _call=call)
        except Exception as error:
            if self._pending is not None:
                self._pending['phase'] = 'recovery_edit' if bpy.context.mode == 'EDIT_ARMATURE' else 'recovery_required'
            raise RestEditError('RESTORE_REQUIRED', {'cause': error_record(error), 'pending': self.pending}) from error
        self._snapshot = None
        return deepcopy(restored)
