"""Draft fixed operation: Anion deform-hip data Bone segments 12 -> 1.

Only the two bbone_segments leaves may change. Source hip bones, handles,
constraints, rest, Mesh/weights/Key and all other declared author fields remain
strict. This module does not establish deformation-quality acceptance.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import uuid

import bpy

import character_state as C
import native_control as N
from native_controls.native import AtomicControls
from native_controls.contract import require, ControlError


TARGETS = ('形变-髋部.L', '形变-髋部.R')
SCHEMA = 'hrig.anion-hip-deform-segments.v1'


class HipDeformRepair:
    """One-process inspect/apply/restore, with a mandatory caller idle guard.

    idle_guard() must return True only after the caller has explicitly checked
    that its preview and animation sessions have no pending edits. There is no
    global Python-session registry or permission to ignore another core's guard.
    """
    def __init__(self, profile, source_identity, idle_guard):
        require(type(source_identity) is dict and set(source_identity) == {'file', 'expected_sha256'},
                'HIP_SOURCE_SCHEMA')
        path, sha = source_identity['file'], source_identity['expected_sha256']
        require(type(path) is str and Path(path).is_absolute(), 'HIP_SOURCE_PATH')
        require(type(sha) is str and len(sha) == 64 and all(c in '0123456789abcdef' for c in sha),
                'HIP_SOURCE_SHA')
        require(callable(idle_guard), 'HIP_CALLER_IDLE_GUARD_REQUIRED')
        self.source = deepcopy(source_identity)
        self.core = AtomicControls(profile)
        self.rig = self.core.rig
        require(self.core.profile['character_id'] == 'anion' and len(self.core.profile['roles']) == 118,
                'HIP_PROFILE_SCOPE')
        self.idle_guard = idle_guard
        self.snapshot = None
        self.pending = None
        self.write_count = 0
        self._guard(12)

    def _guard(self, expected_segments):
        require(self.idle_guard() is True, 'HIP_OTHER_EDIT_PENDING')
        require(Path(bpy.data.filepath).resolve() == Path(self.source['file']).resolve(), 'HIP_MAIN_MISMATCH')
        require(hashlib.sha256(Path(self.source['file']).read_bytes()).hexdigest()
                == self.source['expected_sha256'], 'HIP_SOURCE_CHANGED')
        require(AtomicControls(self.core.profile).rig == self.rig, 'HIP_RIG_IDENTITY')
        bones = self.rig.data.bones
        observed = []
        for side in ('L', 'R'):
            source = bones.get('髋部.' + side)
            target = bones.get('形变-髋部.' + side)
            start = bones.get('髋部-底部.' + side)
            end = bones.get('髋部-顶部.' + side)
            require(all(isinstance(item, bpy.types.Bone) and item.id_data == self.rig.data
                        for item in (source, target, start, end)), 'HIP_BONE_HANDLE_IDENTITY', side)
            for bone in (source, target):
                field = bone.bl_rna.properties.get('bbone_segments')
                require(field is not None and field.type == 'INT' and not field.is_array
                        and not field.is_readonly and field.hard_min <= 1 and field.hard_max >= 12,
                        'HIP_SEGMENTS_RNA', bone.name)
                require(type(bone.bbone_segments) is int
                        and bone.bbone_segments == (12 if bone == source else expected_segments),
                        'HIP_SEGMENTS_DRIFT', bone.name)
                require(bone.bbone_custom_handle_start == start and bone.bbone_custom_handle_end == end
                        and bone.bbone_handle_type_start == 'TANGENT'
                        and bone.bbone_handle_type_end == 'TANGENT', 'HIP_HANDLE_DRIFT', bone.name)
                observed.append({'bone': bone.name, 'segments': bone.bbone_segments,
                                 'start': start.name, 'end': end.name,
                                 'start_type': bone.bbone_handle_type_start,
                                 'end_type': bone.bbone_handle_type_end})
        return observed

    def _capture(self):
        self.core.observe()  # force lazy evaluation before the declared observer
        return C.capture()

    def inspect(self):
        require(self.pending is None, 'HIP_APPLY_ALREADY_PENDING')
        handles = self._guard(12)
        before = self._capture()
        matches = [index for index, block in enumerate(before['blocks'])
                   if block['type'] == 'Armature' and block['name'] == self.rig.data.name
                   and block['library'] is None]
        require(len(matches) == 1, 'HIP_ARMATURE_SNAPSHOT')
        block_index = matches[0]
        paths = [('blocks', block_index, 'character_extra', 'bone_settings', name,
                  'settings', 'bbone_segments') for name in TARGETS]
        expected = deepcopy(before)
        for path in paths:
            tree = expected
            for key in path[:-1]:
                tree = tree[key]
            require(tree[path[-1]] == 12, 'HIP_SNAPSHOT_SEGMENTS')
            tree[path[-1]] = 1
        token = 'hip_snap_' + uuid.uuid4().hex
        self.snapshot = {'token': token, 'before': before, 'expected': expected,
                         'context': N.context(), 'originals': {name: 12 for name in TARGETS}, 'paths': paths}
        return {'schema': SCHEMA, 'status': 'inspected', 'snapshot_token': token,
                'source': deepcopy(self.source), 'handles': handles,
                'operation': {'bones': list(TARGETS), 'field': 'bbone_segments', 'before': 12, 'after': 1},
                'exact_changed_paths': [list(path) for path in paths],
                'author_sha256': C.digest(before), 'write_count': self.write_count}

    def _write(self, values):
        for name, value in values.items():
            self.rig.data.bones[name].bbone_segments = value
            self.write_count += 1
        self.core._update()

    def apply(self, snapshot_token):
        require(self.pending is None and self.snapshot is not None
                and self.snapshot['token'] == snapshot_token, 'HIP_STALE_SNAPSHOT')
        self._guard(12)
        snapshot = self.snapshot
        require(N.context() == snapshot['context'] and self._capture() == snapshot['before'],
                'HIP_LIVE_STATE_CHANGED')
        self.pending = {'token': 'hip_restore_' + uuid.uuid4().hex, 'after': None}
        try:
            self._write({name: 1 for name in TARGETS})
            handles = self._guard(1)
            after = self._capture()
            expected_sha = C.digest(snapshot['expected'])
            require(after == snapshot['expected'] and C.digest(after) == expected_sha,
                    'HIP_AUTHOR_PROTECTION_CHANGED',
                    {'differences': C.diff(snapshot['expected'], after, limit=25),
                     'expected_sha256': expected_sha, 'actual_sha256': C.digest(after)})
            require(N.context() == snapshot['context'], 'HIP_CONTEXT_CHANGED')
            self.pending['after'] = after
            return {'schema': SCHEMA, 'status': 'applied_draft', 'restore_token': self.pending['token'],
                    'full_expected_author_exact': True, 'author_sha256': C.digest(after),
                    'changed_paths': [list(path) for path in snapshot['paths']],
                    'handles': handles, 'write_count': self.write_count,
                    'quality_qualification': 'not_certified; neutral/inner-thigh/surface checks remain'}
        except Exception as error:
            try:
                recovery = self._restore()
            except Exception as recovery_error:
                raise ControlError('HIP_ROLLBACK_FAILED',
                    {'cause': getattr(error, 'code', str(error)),
                     'recovery': getattr(recovery_error, 'detail', str(recovery_error)),
                     'restore_token': self.pending['token'] if self.pending else None}) from error
            if isinstance(error, ControlError):
                error.detail = {'cause': error.detail, 'rollback': recovery}
            raise

    def _restore(self):
        require(self.pending is not None and self.snapshot is not None, 'HIP_NO_PENDING_RESTORE')
        self._write(self.snapshot['originals'])
        handles = self._guard(12)
        after = self._capture()
        before = self.snapshot['before']
        require(after == before and C.digest(after) == C.digest(before), 'HIP_RESTORE_AUTHOR_DRIFT',
                C.diff(before, after, limit=25))
        require(N.context() == self.snapshot['context'], 'HIP_RESTORE_CONTEXT_DRIFT')
        answer = {'schema': SCHEMA, 'status': 'restored_exact', 'full_author_exact': True,
                  'full_author_sha256_exact': True, 'context_exact': True,
                  'author_sha256': C.digest(after), 'handles': handles, 'write_count': self.write_count}
        self.pending = None
        self.snapshot = None
        return answer

    def restore(self, restore_token):
        require(self.pending is not None and self.pending['token'] == restore_token,
                'HIP_RESTORE_TOKEN')
        require(self.pending['after'] is not None, 'HIP_FAILED_ROLLBACK_REQUIRES_REVIEW')
        self._guard(1)
        require(N.context() == self.snapshot['context'] and self._capture() == self.pending['after'],
                'HIP_APPLIED_STATE_CHANGED')
        return self._restore()
