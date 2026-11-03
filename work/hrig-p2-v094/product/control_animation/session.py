"""One explicit GUI/CLI session over the same native control/animation cores."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import traceback
import uuid

import bpy
import numpy as np

import character_state as C
from native_controls.native import AtomicControls
from native_controls.contract import require
from .native import AnimationTransaction, SCHEMA


_SESSION = None


def get_session(required=True):
    if required:
        require(_SESSION is not None, 'C_SESSION_NOT_INITIALIZED')
    return _SESSION


def _digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skin(body_name):
    body = bpy.data.objects.get(body_name)
    require(body is not None and body.type == 'MESH', 'BODY_IDENTITY')
    evaluated = body.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        points = np.empty(len(mesh.vertices) * 3, dtype='<f4')
        mesh.vertices.foreach_get('co', points)
        points = points.reshape(-1, 3)
        world = np.asarray(evaluated.matrix_world, dtype='<f4')
        points = points @ world[:3, :3].T + world[:3, 3]
        require(bool(np.isfinite(points).all()), 'SKIN_NONFINITE')
        return points
    finally:
        evaluated.to_mesh_clear()


class ControlAnimationSession:
    def __init__(self, profile_path, source_identity, body_name, outdir, clip=None, catalog_path=None):
        self.profile_path = str(Path(profile_path).resolve())
        self.profile = json.loads(Path(self.profile_path).read_bytes())
        self.source = deepcopy(source_identity)
        self.core = AtomicControls(self.profile)
        # Constructor-only source/profile validation: no inspect/capture and no
        # input, Action or frame writes. Author still revalidates its own source.
        AnimationTransaction(self.profile, self.source, preview_core=self.core)
        self.body_name = body_name
        require(bpy.data.objects.get(body_name) is not None and bpy.data.objects[body_name].type == 'MESH',
                'BODY_IDENTITY')
        self.outdir = Path(outdir).resolve()
        require(self.outdir.is_absolute(), 'OUTPUT_DIRECTORY')
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.preset = deepcopy(clip)
        self.catalog = json.loads(Path(catalog_path).read_bytes()) if catalog_path else None
        if self.catalog is not None:
            require(self.catalog.get('schema') == 'hrig.fullbody-catalog.v1'
                    and self.catalog.get('profile_id') == self.profile['profile_id'], 'CATALOG_IDENTITY')
        self.id = uuid.uuid4().hex[:12]
        self.seq = 0
        self.preview_token = None
        self.preview_skin = None
        self.animation = None
        self.undo_token = None
        self.save_attempted = False
        self.last_result = None
        self.last_error = None
        self.last_log_error = None
        self._roles = {row['role_id']: row for row in self.profile['roles']}
        self._mode_roles = {(r['bone'], r['property']): r['role_id'] for r in self.profile['roles']
                            if r['kind'] == 'mode_enum'}
        self.frame_domain, self.preset_observation_frame = self._frame_domain()
        self._role_enum_cache = {}
        self._group_enum = [('all', '全部控制', '完整显式角色名单')]
        if self.catalog is not None:
            for group in self.catalog.get('groups', []):
                require(type(group) is dict and type(group.get('id')) is str
                        and type(group.get('label')) is str and type(group.get('roles')) is list,
                        'CATALOG_GROUP_SCHEMA')
                require(all(role in self._roles for role in group['roles']), 'CATALOG_ROLE_MISSING')
                self._group_enum.append((group['id'], group['label'], group['label']))
        self.log('initialize', {'status': 'ready', 'profile': self.profile_path, 'profile_id': self.profile['profile_id'],
                                'source': self.source, 'body': body_name, 'roles': len(self._roles),
                                'frame': bpy.context.scene.frame_current_final,
                                'allowed_observation_frames': sorted(self.frame_domain)})

    def _frame_domain(self):
        """Fixed C1 frames plus a finite, explicit preset-derived frame table."""
        allowed = {0, 6, 12}
        if self.preset is None:
            return frozenset(allowed), None
        require(type(self.preset) is dict and self.preset.get('schema') == SCHEMA,
                'PRESET_CLIP_SCHEMA')
        rows = self.preset.get('keys')
        require(type(rows) is list and 2 <= len(rows) <= 256, 'PRESET_KEY_GRID')
        frames = []
        modes = []
        for row in rows:
            require(type(row) is dict and set(row) == {'frame', 'operations'}, 'PRESET_KEY_SCHEMA')
            frame = row['frame']
            require(type(frame) in (int, float) and math.isfinite(frame)
                    and int(frame) == frame and 0 <= frame <= 1048573, 'PRESET_INTEGER_FRAME')
            frames.append(int(frame))
            require(len(frames) < 2 or frames[-1] > frames[-2], 'PRESET_FRAME_ORDER')
            require(type(row['operations']) is list and 1 <= len(row['operations']) <= 128,
                    'PRESET_OPERATIONS')
            values = {}
            for operation in row['operations']:
                require(type(operation) is dict and set(operation) == {'role', 'value'}
                        and operation['role'] in self._roles and operation['role'] not in values,
                        'PRESET_ROLE_SCHEMA')
                values[operation['role']] = operation['value']
            current_modes = {name: value for name, value in values.items()
                             if self._roles[name]['kind'] == 'mode_enum'}
            require(all(type(value) in (int, float) and value in (0, 1)
                        for value in current_modes.values()), 'PRESET_MODE_VALUE')
            require(not modes or set(current_modes) == set(modes[0]), 'PRESET_MODE_ROSTER')
            modes.append(current_modes)
        allowed.update(frames)
        allowed.update((left + right) // 2 for left, right in zip(frames, frames[1:]))
        for index in range(1, len(frames)):
            if modes[index] != modes[index - 1]:
                frame = frames[index]
                allowed.update(f for f in (frame - 1, frame, frame + 1)
                               if 0 <= f <= frames[-1] + 1)
        return frozenset(allowed), frames[-2]

    @property
    def preview_active(self):
        return self.core.pending is not None

    @property
    def animation_active(self):
        return self.animation is not None and self.animation.pending is not None

    def role(self, name):
        require(name in self._roles, 'ROLE_UNSUPPORTED', name)
        return self._roles[name]

    def group_enum(self):
        return self._group_enum

    def role_enum(self, group='all'):
        if group not in self._role_enum_cache:
            names = list(self._roles)
            if group != 'all':
                found = next((row for row in self.catalog.get('groups', []) if row['id'] == group), None) if self.catalog else None
                require(found is not None, 'CATALOG_GROUP_MISSING', group)
                names = found['roles']
            self._role_enum_cache[group] = [(name, self._roles[name]['label'], name) for name in names]
        # Keep tuple/string storage alive for Blender's dynamic enum callback.
        return self._role_enum_cache[group]

    def default_value(self, role_id):
        role = self.role(role_id)
        desired = 0.003 if role['kind'] == 'location_delta' else 0.8 if role['kind'] == 'scale_set' else 0.0 if role['kind'] == 'mode_enum' else 0.12
        return min(max(desired, role['bounds'][0]), role['bounds'][1])

    def status(self):
        return {'session_id': self.id, 'preview_pending': self.preview_active,
                'animation_pending': self.animation_active,
                'frame': bpy.context.scene.frame_current_final,
                'last_error': self.last_error, 'log_error': self.last_log_error}

    def log(self, event, result):
        """Logging never retries a domain mutation or turns success into retry advice."""
        self.seq += 1
        record = {'session_id': self.id, 'sequence': self.seq,
                  'time_utc': datetime.now(timezone.utc).isoformat(), 'event': event,
                  'result': deepcopy(result)}
        self.last_result = record
        try:
            payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            path = self.outdir / ('result-%s-%04d-%s.json' % (self.id, self.seq, event))
            with path.open('x', encoding='utf8') as stream:
                stream.write(payload)
            with (self.outdir / 'events.jsonl').open('a', encoding='utf8') as stream:
                stream.write(payload + '\n')
        except Exception as error:
            self.last_log_error = str(error)
        return deepcopy(result)

    def run(self, event, callback):
        try:
            answer = callback()
            self.last_error = None
            return self.log(event, answer)
        except Exception as error:
            detail = getattr(error, 'detail', None)
            try:
                if len(json.dumps(detail, ensure_ascii=False, default=str)) > 12000:
                    detail = str(detail)[:12000]
            except Exception:
                detail = str(detail)[:12000]
            self.last_error = getattr(error, 'code', type(error).__name__)
            self.log(event, {'status': 'error', 'code': self.last_error, 'detail': detail,
                             'message': str(error), 'traceback': traceback.format_exc(limit=8),
                             'state': self.status()})
            raise

    def _require_idle(self):
        require(not self.preview_active, 'RESTORE_PREVIEW_FIRST')
        require(not self.animation_active, 'UNDO_ANIMATION_FIRST')

    def _guard_operations(self, operations):
        modes = {}
        explicit = {op['role']: op['value'] for op in operations}
        for operation in operations:
            for guard in self.role(operation['role'])['requires']:
                key = guard['bone'], guard['property']
                require(key in self._mode_roles, 'MODE_ROLE_MISSING', key)
                name = self._mode_roles[key]
                require(name not in modes or modes[name] == guard['value'], 'MODE_GUARD_CONFLICT', name)
                require(name not in explicit or explicit[name] == guard['value'], 'MODE_GUARD_CONFLICT', name)
                modes[name] = guard['value']
        return [{'role': name, 'value': value} for name, value in modes.items() if name not in explicit] + deepcopy(operations)

    def _artifact(self, label, **arrays):
        # Large arrays belong in NPZ evidence, never JSON or the UI response.
        try:
            path = self.outdir / ('witness-%s-%s-%s.npz' % (self.id, label, uuid.uuid4().hex[:8]))
            with path.open('xb') as stream:
                np.savez_compressed(stream, **arrays)
            return {'file': str(path), 'sha256': _digest_file(path), 'bytes': path.stat().st_size}
        except Exception as error:
            self.last_log_error = 'witness artifact: ' + str(error)
            return {'status': 'artifact_write_failed', 'error': str(error)}

    def preview(self, role_id, value):
        def perform():
            self._require_idle()
            operations = self._guard_operations([{'role': role_id, 'value': value}])
            original_skin = skin(self.body_name)
            inspected = self.core.inspect(operations)
            self.preview_skin = original_skin
            applied = self.core.apply(operations, inspected['snapshot_token'])
            # Set session recovery state before any observation/log I/O can fail.
            self.preview_token = applied['restore_token']
            self.preview_skin = original_skin
            actual_skin = skin(self.body_name)
            delta = np.linalg.norm(actual_skin - original_skin, axis=1)
            artifact = self._artifact('preview',
                bones_before=np.asarray(list(applied['witness_before'].values()), dtype='<f4'),
                bones_after=np.asarray(list(applied['witness_after'].values()), dtype='<f4'),
                skin_before=original_skin, skin_after=actual_skin)
            return {'status': 'previewed', 'operations': operations, 'restore_token': self.preview_token,
                    'bone_response_count': len(applied['responding']),
                    'skin_changed': int(np.count_nonzero(delta)), 'skin_max_delta': float(delta.max()),
                    'author_after_sha256': applied.get('author_after_sha256'), 'artifact': artifact}
        return self.run('preview', perform)

    def restore_preview(self):
        def perform():
            require(self.preview_active, 'NO_PREVIEW_PENDING')
            result = self.core.restore(self.core.pending['token'])
            points = skin(self.body_name)
            exact = self.preview_skin is not None and np.array_equal(points, self.preview_skin)
            self.preview_token = None
            self.preview_skin = None
            require(bool(exact), 'PREVIEW_SKIN_RESTORE_DRIFT')
            return {'status': 'preview_restored', 'native': result, 'skin_restored_exact': True}
        return self.run('restore_preview', perform)

    def selected_clip(self, role_id, value):
        role = self.role(role_id)
        require(bpy.context.scene.frame_current_final == 0, 'AUTHOR_REQUIRES_FRAME_ZERO')
        if role['kind'] == 'scale_set':
            neutral = float(self.core.rig.pose.bones[role['bone']].scale[role['index']])
        elif role['kind'] == 'mode_enum':
            neutral = self.core.rig.pose.bones[role['bone']][role['property']]
        else:
            neutral = 0.0
        return {'schema': SCHEMA, 'name': 'HRIG_C1_GUI_' + uuid.uuid4().hex[:16], 'replace_targets': True,
                'keys': [{'frame': frame, 'operations': self._guard_operations([{'role': role_id, 'value': desired}])}
                         for frame, desired in ((0, neutral), (12, value), (24, neutral))]}

    def author(self, role_id=None, value=None, use_preset=False):
        def perform():
            self._require_idle()
            clip = deepcopy(self.preset) if use_preset else self.selected_clip(role_id, value)
            require(clip is not None, 'PRESET_CLIP_MISSING')
            transaction = AnimationTransaction(self.profile, self.source, preview_core=self.core)
            self.animation = transaction
            inspected = transaction.inspect(clip)
            authored = transaction.author(inspected['snapshot_token'])
            self.undo_token = authored['undo_token']
            return {'status': 'authored', 'action': authored['action'], 'slot': authored['slot'],
                    'archive_key': authored['archive_key'], 'undo_token': self.undo_token,
                    'curve_count': len(authored['curves']),
                    'frames': [key['frame'] for key in clip['keys']],
                    'author_after_sha256': authored['author_after_sha256'],
                    'protection': {key: val for key, val in authored['protection'].items()
                                   if key != 'allowed_native_leaf_paths'}}
        return self.run('author_preset' if use_preset else 'author_selected', perform)

    def show_frame(self, frame):
        def perform():
            require(not self.preview_active, 'RESTORE_PREVIEW_FIRST')
            require(type(frame) is int and frame in self.frame_domain, 'UI_FRAME',
                    {'requested': frame, 'allowed': sorted(self.frame_domain)})
            bpy.context.scene.frame_set(frame)
            self.core.rig.update_tag(refresh={'OBJECT', 'DATA'})
            bpy.context.view_layer.update()
            witnesses = self.core.observe()
            points = skin(self.body_name)
            bones = np.asarray(list(witnesses.values()), dtype='<f4')
            artifact = self._artifact('frame-' + str(frame), bones=bones, skin=points)
            return {'status': 'frame_observed', 'frame': frame, 'bone_count': len(witnesses),
                    'skin_points': len(points), 'bone_sha256': hashlib.sha256(bones.tobytes()).hexdigest(),
                    'skin_sha256': hashlib.sha256(points.tobytes()).hexdigest(), 'artifact': artifact}
        return self.run('frame_' + str(frame), perform)

    def undo_animation(self):
        def perform():
            require(not self.preview_active and self.animation_active, 'NO_ANIMATION_PENDING')
            # This UI operation explicitly promises return-to-author-frame + undo.
            frame = self.animation.pending['frame']
            self.animation._set_frame(frame)
            result = self.animation.undo(self.animation.pending['token'])
            self.animation = None
            self.undo_token = None
            return {'status': 'animation_undone', 'frame': frame, 'native': result}
        return self.run('undo_animation', perform)

    def save_copy(self):
        def perform():
            require(not self.preview_active and self.animation_active, 'AUTHOR_ANIMATION_BEFORE_SAVE')
            path = self.outdir / 'output-C-gui.blend'
            require(not self.save_attempted, 'SAVE_COPY_ALREADY_ATTEMPTED')
            require(not path.exists() and not path.is_symlink(), 'OUTPUT_COPY_ALREADY_EXISTS', str(path))
            require(path.resolve() != Path(bpy.data.filepath).resolve(), 'OUTPUT_IS_SOURCE')
            main_before = bpy.data.filepath
            self.core.observe()
            before = C.capture()
            self.save_attempted = True
            outcome = bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, relative_remap=True, check_existing=False)
            require('FINISHED' in outcome and path.is_file(), 'SAVE_COPY_FAILED')
            require(bpy.data.filepath == main_before, 'SAVE_CHANGED_MAIN')
            self.core.observe()
            after = C.capture()
            require(after == before, 'SAVE_CHANGED_DECLARED_AUTHOR', C.diff(before, after, limit=20))
            require(_digest_file(Path(self.source['file'])) == self.source['expected_sha256'], 'SOURCE_SHA_CHANGED')
            return {'status': 'copy_saved', 'file': str(path), 'sha256': _digest_file(path),
                    'bytes': path.stat().st_size, 'current_main_unchanged': True,
                    'full_declared_author_exact': True, 'frame': bpy.context.scene.frame_current_final}
        return self.run('save_copy', perform)


def initialize(profile_path, source_identity, body_name, outdir, clip=None, catalog_path=None):
    """Explicitly create one session and register the minimal panel.

    Reinitializing a session with pending preview/animation is refused. No
    automatic scene operation, selection, frame jump or benchmark is performed.
    """
    global _SESSION
    require(_SESSION is None or not (_SESSION.preview_active or _SESSION.animation_active),
            'C_SESSION_HAS_PENDING_WORK')
    session = ControlAnimationSession(profile_path, source_identity, body_name, outdir, clip, catalog_path)
    _SESSION = session
    from . import ui
    ui.register()
    state = getattr(bpy.context.window_manager, 'hrig_c_controls', None)
    if state is not None:
        state.group = 'all'
        state.role = session.profile['roles'][0]['role_id']
        state.value = session.default_value(state.role)
    return session
