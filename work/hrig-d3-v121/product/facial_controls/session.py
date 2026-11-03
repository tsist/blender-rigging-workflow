"""Explicit D3 session with shared guarded preview and copied-Action transactions."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid
import bpy
import numpy as np
import character_state as C
import native_control as N
from .catalog import load, index
from .contract import require, compile_request, compile_clip, digest, SCHEMA
from .control_core.native import AtomicControls
from .animation_core.native import AnimationTransaction
from .native import observe_faces, exact_faces

_SESSION = None


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def get_session(required=True):
    require(not required or _SESSION is not None, 'FACE_SESSION_NOT_INITIALIZED')
    return _SESSION


class FacialSession:
    def __init__(self, profile, source_identity, outdir, *, idle_guard=None, persistence_writer=None):
        require(idle_guard is None or callable(idle_guard), 'FACE_IDLE_GUARD')
        require(persistence_writer is None or callable(persistence_writer), 'FACE_PERSISTENCE_WRITER')
        self.id = 'facial_' + uuid.uuid4().hex[:12]
        self.idle_guard = idle_guard
        self.persistence_writer = persistence_writer
        self.profile = json.loads(Path(profile).read_bytes()) if isinstance(profile, (str, Path)) else deepcopy(profile)
        self.catalog = load()
        self.controls = index(self.catalog)
        self.source = deepcopy(source_identity)
        require(type(self.source) is dict and set(self.source) == {'file', 'expected_sha256'}
                and Path(self.source['file']).is_absolute(), 'FACE_SOURCE_IDENTITY')
        require(Path(bpy.data.filepath).resolve() == Path(self.source['file']).resolve()
                and file_sha(self.source['file']) == self.source['expected_sha256'], 'FACE_SOURCE_MAIN')
        require(self.profile['facial_binding']['source'] == self.source, 'FACE_PROFILE_SOURCE_IDENTITY')
        self._entry('initialize')
        self.core = AtomicControls(self.profile)
        self.outdir = Path(outdir)
        require(self.outdir.is_absolute(), 'FACE_OUTPUT_ABSOLUTE')
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.request = None
        self.inspection = None
        self.before_faces = None
        self.before_arrays = None
        self.animation = None
        self.animation_inspection = None
        self.animation_frames = None
        self.approved_value_pending = None
        self.save_attempted = False
        self.last_error = None
        self.last_result = None
        self.log_error = None
        self._control_items = [(c['id'], c['label'], c['id']) for c in self.catalog['controls']]
        self._preset_items = [('manual', '手动控制', '单项或显式组合')] + [
            (k, {'neutral':'中性','smile':'微笑','happy':'开心','sad':'难过','angry':'生气',
                 'surprised':'惊讶','confused':'困惑','smug':'轻蔑','half_lid':'半目',
                 'anime_ha':'哈？','anime_tang':'夸张笑'}.get(k, k), k)
            for k in self.catalog['presets']]
        self.set_request({'schema': SCHEMA, 'mode':'independent', 'preset':'manual',
                          'strength':1.0, 'values':{'blink.both':.5}})
        self._mode_guard()

    @property
    def preview_pending(self):
        return self.core.pending is not None or self.approved_value_pending is not None

    @property
    def animation_pending(self):
        return self.animation is not None and self.animation.pending is not None

    def status(self):
        return {'session_id':self.id, 'preview_pending':self.preview_pending,
                'animation_pending':self.animation_pending,
                'approved_value_pending':self.approved_value_pending is not None,
                'inspected':self.inspection is not None,
                'frame':bpy.context.scene.frame_current_final,
                'last_error':self.last_error, 'log_error':self.log_error}

    def _mode_guard(self):
        rig = self.core.rig
        guard = self.catalog['mode_guard']
        require(rig.pose.bones[guard['bone']][guard['property']] == guard['value'], 'FACE_MODE_CHANGED')
        require(rig.pose.bones['眼部.L']['左眼睑跟随'] == 1
                and rig.pose.bones['眼部.R']['右眼睑跟随'] == 1, 'FACE_LID_FOLLOW_MODE_CHANGED')
        for bone in self.catalog['required_zero_pads']:
            require(list(rig.pose.bones[bone].location) == [0.0, 0.0, 0.0], 'FACE_PAD_MODE_CONFLICT', bone)

    def _entry(self, operation):
        require(Path(bpy.data.filepath).resolve() == Path(self.source['file']).resolve()
                and file_sha(self.source['file']) == self.source['expected_sha256'], 'FACE_SOURCE_MAIN')
        if self.idle_guard is not None:
            require(self.idle_guard(operation=operation, owner=self.id) is True,
                    'FACE_OTHER_MODULE_PENDING')

    def _baseline_guard(self, operations):
        self._mode_guard()
        roles = {r['role_id']:r for r in self.profile['roles']}
        for op in operations:
            r = roles[op['role']]
            require(r['role_id'].startswith('d3.') and r['kind'] == 'location_delta'
                    and self.core.rig.pose.bones[r['bone']].location[r['index']] == 0.0,
                    'FACE_INPUT_REQUIRES_BASELINE_ZERO', r['role_id'])

    def _idle(self):
        require(not self.preview_pending and not self.animation_pending, 'FACE_RESTORE_OR_UNDO_FIRST')

    def _preview_current_fence(self):
        pending = self.core.pending
        require(pending is not None and not pending.get('recovery_only')
                and 'after' in pending and 'after_witness' in pending,
                'FACE_PREVIEW_NOT_COMMITTED')
        expected = pending['after'] if self.approved_value_pending is None else self.approved_value_pending['expected']
        require(C.capture() == expected and N.context() == pending['context']
                and self.core.observe() == pending['after_witness'], 'FACE_PREVIEW_CURRENT_DRIFT')

    def set_request(self, request):
        self._entry('set_request')
        compile_request(request, self.catalog)
        self.request = deepcopy(request)
        self.inspection = None
        if not self.animation_pending:
            self.animation_inspection = None
            if self.animation is not None:
                self.animation.snapshot = None
        # Never discard pending native recovery state when UI inputs change.
        if not self.preview_pending:
            self.core.snapshot = None
        return self.status()

    def invalidate(self):
        self._entry('invalidate')
        self.inspection = None
        if not self.animation_pending:
            self.animation_inspection = None
            if self.animation is not None:
                self.animation.snapshot = None
        if not self.preview_pending:
            self.core.snapshot = None

    def log(self, event, value):
        self.seq += 1
        row = {'session_id':self.id, 'sequence':self.seq, 'event':event,
               'time_utc':datetime.now(timezone.utc).isoformat(), 'result':value}
        self.last_result = row
        try:
            p = self.outdir / ('face-%s-%04d-%s.json' % (self.id, self.seq, event))
            with p.open('x', encoding='utf8') as stream:
                json.dump(row, stream, ensure_ascii=False, allow_nan=False)
        except Exception as error:
            self.log_error = str(error)
        return value

    def _arrays(self, label, arrays):
        path = self.outdir / ('face-%s-%s-%s.npz' % (self.id, label, uuid.uuid4().hex[:8]))
        with path.open('xb') as stream:
            np.savez_compressed(stream, **arrays)
        return {'file':str(path), 'sha256':file_sha(path), 'bytes':path.stat().st_size}

    def inspect(self):
        self._entry('inspect')
        self._idle()
        plan = compile_request(self.request, self.catalog)
        self._baseline_guard(plan['operations'])
        if not plan['operations']:
            require(all(self.core.rig.pose.bones[c['bone']].location[c['index']] == 0.0
                        for c in self.catalog['controls']), 'FACE_NEUTRAL_NOT_BASELINE')
            self.inspection = None
            return self.log('inspect_neutral', {'status':'neutral_no_write', 'preview_enabled':False})
        native = self.core.inspect(plan['operations'])
        faces, arrays = observe_faces(self.catalog)
        self.before_faces, self.before_arrays = faces, arrays
        self.inspection = {'token':'face_snap_'+uuid.uuid4().hex, 'request_sha256':digest(self.request),
                           'plan':plan, 'native_token':native['snapshot_token']}
        return self.log('inspect', {'status':'inspected', 'snapshot_token':self.inspection['token'],
            'request_sha256':self.inspection['request_sha256'], 'operations':plan['operations'],
            'native':native, 'faces':faces, 'artifact':self._arrays('before', arrays)})

    def preview(self, snapshot_token=None):
        self._entry('preview')
        self._idle()
        inspected = self.inspection
        require(inspected is not None and inspected['request_sha256'] == digest(self.request)
                and (snapshot_token is None or snapshot_token == inspected['token']), 'FACE_STALE_SNAPSHOT')
        self._baseline_guard(inspected['plan']['operations'])
        # Core establishes pending before any fallible observation/artifact I/O.
        applied = self.core.apply(inspected['plan']['operations'], inspected['native_token'])
        self.inspection = None
        faces, arrays = observe_faces(self.catalog)
        return self.log('preview', {'status':'previewed', 'native':applied, 'faces':faces,
            'artifact':self._arrays('after', arrays), 'qualification':'native result only; visual quality pending'})

    def restore(self):
        self._entry('restore')
        require(self.preview_pending, 'FACE_NO_PREVIEW_PENDING')
        if self.approved_value_pending is not None:
            self.restore_approved_key_value()
        restored = self.core.restore(self.core.pending['token'])
        faces, arrays = observe_faces(self.catalog)
        exact = exact_faces(self.before_faces, faces, self.before_arrays, arrays)
        self.inspection = None
        require(exact, 'FACE_OUTPUT_RESTORE_DRIFT')
        self.before_faces = self.before_arrays = None
        return self.log('restore', {'status':'restored', 'native':restored,
            'all_face_key_values_and_skin_exact':True, 'faces':faces,
            'artifact':self._arrays('restored', arrays)})

    def preview_approved_key_value(self, data, target, value):
        """One receipt-approved undriven scalar on top of an active face preview."""
        self._entry('preview_approved_key_value')
        require(self.core.pending is not None and not self.animation_pending
                and self.approved_value_pending is None, 'FACE_APPROVED_VALUE_ORDER')
        self._preview_current_fence()
        require(type(value) in (int, float) and np.isfinite(value)
                and 0.0 <= value <= 1.0, 'FACE_APPROVED_VALUE_RANGE')
        from .key_receipts import load_approvals
        from .control_core.bindings import validate_native_signatures
        from facial_key_edit.native import author_rows, witness
        validate_native_signatures(self.profile)
        matches = [a for a in load_approvals(self.profile['facial_binding'], self.catalog)
                   if a['owner']['data'] == data and a['target'] == target]
        require(len(matches) == 1, 'FACE_APPROVED_VALUE_TARGET')
        owner = matches[0]['owner']; obj = bpy.data.objects[owner['object']]
        key = obj.data.shape_keys; block = key.key_blocks[target]; ad = key.animation_data
        require(ad is None or (ad.action is None and not ad.nla_tracks
                and not any(f.data_path == block.path_from_id('value') for f in ad.drivers)),
                'FACE_APPROVED_VALUE_HAS_WRITER')
        before = C.capture(); expected = deepcopy(before)
        row = next(r for r in author_rows(expected, obj)[1]['character_extra']['blocks'] if r['name'] == target)
        wanted = float(np.float32(value)); row['settings']['value'] = wanted
        faces, arrays = observe_faces(self.catalog)
        pending = {'object':obj, 'block':block, 'before':before, 'expected':expected,
                   'context':N.context(), 'witness':witness(self.profile),
                   'value':float(block.value), 'faces':faces, 'arrays':arrays}
        self.approved_value_pending = pending
        block.value = wanted; obj.update_tag(refresh={'OBJECT','DATA'}); key.update_tag()
        bpy.context.view_layer.update()
        actual = C.capture()
        require(actual == expected and N.context() == pending['context']
                and witness(self.profile).tobytes() == pending['witness'].tobytes(),
                'FACE_APPROVED_VALUE_FORWARD_DRIFT', C.diff(expected, actual, limit=20))
        validate_native_signatures(self.profile)
        return self.log('approved_key_value', {'status':'previewed', 'data':data,
            'target':target, 'value':wanted, 'full_C_single_scalar_forward_exact':True,
            'body_bones_context_and_Key_coordinates_exact':True})

    def restore_approved_key_value(self):
        self._entry('restore_approved_key_value')
        from facial_key_edit.native import witness
        from .control_core.bindings import validate_native_signatures
        pending = self.approved_value_pending
        require(pending is not None, 'FACE_NO_APPROVED_VALUE_PENDING')
        validate_native_signatures(self.profile)
        actual = C.capture()
        require(actual == pending['expected'] and N.context() == pending['context']
                and witness(self.profile).tobytes() == pending['witness'].tobytes(),
                'FACE_APPROVED_VALUE_RESTORE_DRIFT', C.diff(pending['expected'], actual, limit=20))
        pending['block'].value = pending['value']
        pending['object'].update_tag(refresh={'OBJECT','DATA'})
        pending['object'].data.shape_keys.update_tag(); bpy.context.view_layer.update()
        faces, arrays = observe_faces(self.catalog)
        require(C.capture() == pending['before'] and N.context() == pending['context']
                and witness(self.profile).tobytes() == pending['witness'].tobytes()
                and exact_faces(pending['faces'],faces,pending['arrays'],arrays),
                'FACE_APPROVED_VALUE_RESTORE_FAILED')
        self.approved_value_pending = None
        return self.log('approved_key_value_restore', {'status':'restored',
            'full_C_context_witness_faces_exact':True})

    def inspect_animation(self, clip):
        self._entry('inspect_animation')
        self._idle()
        native_clip = compile_clip(clip, self.catalog)
        self._baseline_guard(native_clip['keys'][0]['operations'])
        transaction = AnimationTransaction(self.profile, self.source, preview_core=self.core)
        inspected = transaction.inspect(native_clip)
        self.animation = transaction
        self.animation_inspection = inspected
        frames = [int(k['frame']) for k in native_clip['keys']]
        self.animation_frames = frozenset(frames + [(a+b)//2 for a,b in zip(frames,frames[1:])])
        self.before_faces, self.before_arrays = observe_faces(self.catalog)
        return self.log('inspect_animation', inspected)

    def author_animation(self, snapshot_token):
        self._entry('author_animation')
        self._idle()
        require(self.animation is not None and self.animation_inspection is not None
                and snapshot_token == self.animation_inspection['snapshot_token'], 'FACE_ANIMATION_SNAPSHOT')
        self._mode_guard()
        authored = self.animation.author(snapshot_token)
        self.inspection = None
        return self.log('author_animation', authored)

    def show_frame(self, frame):
        self._entry('show_frame')
        require(self.animation_pending and not self.preview_pending and type(frame) is int
                and frame in self.animation_frames, 'FACE_OBSERVATION_FRAME')
        bpy.context.scene.frame_set(frame)
        self.core.rig.update_tag(refresh={'OBJECT','DATA'})
        bpy.context.view_layer.update()
        faces, arrays = observe_faces(self.catalog)
        return self.log('frame', {'status':'observed', 'frame':frame, 'faces':faces,
            'artifact':self._arrays('frame-%d'%frame, arrays)})

    def undo_animation(self, *, return_to_author_frame=False):
        self._entry('undo_animation')
        require(self.animation_pending and not self.preview_pending, 'FACE_NO_ANIMATION_PENDING')
        frame = self.animation.pending['frame']
        if return_to_author_frame:
            bpy.context.scene.frame_set(int(frame), subframe=frame-int(frame))
            self.core.rig.update_tag(refresh={'OBJECT','DATA'})
            bpy.context.view_layer.update()
        undone = self.animation.undo(self.animation.pending['token'])
        faces, arrays = observe_faces(self.catalog)
        require(exact_faces(self.before_faces, faces, self.before_arrays, arrays), 'FACE_ANIMATION_OUTPUT_RESTORE_DRIFT')
        self.animation = self.animation_inspection = self.animation_frames = None
        self.before_faces = self.before_arrays = None
        return self.log('undo_animation', {'status':'animation_undone', 'native':undone,
            'all_face_key_values_and_skin_exact':True, 'faces':faces})

    def save_copy(self):
        self._entry('save_copy')
        require(self.preview_pending or self.animation_pending, 'FACE_NOTHING_TO_SAVE')
        if self.core.pending is not None:
            self._preview_current_fence()
        require(not self.save_attempted, 'FACE_SAVE_ALREADY_ATTEMPTED')
        suffix = uuid.uuid4().hex[:10]
        blend = self.outdir / ('output-D3-%s.blend' % suffix)
        profile = self.outdir / ('output-D3-%s-profile.json' % suffix)
        manifest = self.outdir / ('output-D3-%s-manifest.json' % suffix)
        require(not any(p.exists() or p.is_symlink() for p in (blend, profile, manifest)), 'FACE_SAVE_EXISTS')
        require(blend.resolve() != Path(bpy.data.filepath).resolve(), 'FACE_SAVE_IS_SOURCE')
        main_before = bpy.data.filepath
        self.core.observe()
        before = C.capture()
        self.save_attempted = True
        writer = self.persistence_writer
        if writer is None:
            from persistence import save_candidate
            writer = save_candidate
        result = writer(blend, self.outdir)
        require(result.get('route') == 'native_copy_runtime_extra_user_retention'
                and result.get('runtime_bookkeeping_restored') is True and blend.is_file()
                and bpy.data.filepath == main_before, 'FACE_SAVE_FAILED')
        self.core.observe()
        after = C.capture()
        require(before == after, 'FACE_SAVE_AUTHOR_CHANGED', C.diff(before, after, limit=20))
        require(file_sha(self.source['file']) == self.source['expected_sha256'], 'FACE_SOURCE_DISK_CHANGED')
        with profile.open('x', encoding='utf8') as stream:
            json.dump(self.profile, stream, ensure_ascii=False, allow_nan=False)
        faces, arrays = observe_faces(self.catalog)
        receipt = {'schema':'hrig.facial-candidate-manifest.v1',
            'source':self.source, 'candidate':{'file':str(blend),'expected_sha256':file_sha(blend)},
            'profile':{'file':str(profile),'expected_sha256':file_sha(profile)},
            'frame':bpy.context.scene.frame_current_final, 'faces':faces,
            'request':self.request, 'preview_pending':self.preview_pending,
            'animation_pending':self.animation_pending,
            'approved_key_value':None if self.approved_value_pending is None else {
                'data':self.approved_value_pending['object'].data.name,
                'target':self.approved_value_pending['block'].name,
                'value':float(self.approved_value_pending['block'].value)},
            'animation_clip':self.animation.snapshot['plan']['clip'] if self.animation_pending else None,
            'author_sha256':C.digest(after), 'full_declared_author_exact':True,
            'persistence':result,
            'witness_bone_count':len(self.profile['rig']['witness_bones']),
            'witness_artifact':self._arrays('saved-witness', {
                'bones':np.asarray(list(self.core.observe().values()),dtype='<f4')}),
            'artifact':self._arrays('saved', arrays), 'cold_qualified':False,
            'scope':'editable local candidate; independent reopen/visual/runtime verification still required'}
        with manifest.open('x', encoding='utf8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, allow_nan=False)
        return self.log('save_copy', {'status':'copy_saved', 'manifest':str(manifest), **receipt})


def initialize(profile, source_identity, outdir, *, register_ui=True,
               idle_guard=None, persistence_writer=None):
    global _SESSION
    require(_SESSION is None or not (_SESSION.preview_pending or _SESSION.animation_pending), 'FACE_SESSION_PENDING')
    session = FacialSession(profile, source_identity, outdir, idle_guard=idle_guard,
                            persistence_writer=persistence_writer)
    _SESSION = session
    if register_ui:
        from . import ui
        ui.register()
        ui.sync_from_request(session.request)
    return session
