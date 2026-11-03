"""Guarded application facade over frozen native control/Action transactions."""
from copy import deepcopy
from pathlib import Path
import hashlib, json, math, sys, uuid
import bpy
import numpy as np
import character_state as C
from native_controls.contract import require, digest
from native_controls.native import AtomicControls
from control_animation.native import AnimationTransaction, SCHEMA

FIELDS = ('arm_l', 'arm_r', 'leg_l', 'leg_r')
ROLE_IDS = ('proportion.arm.L.upper', 'proportion.arm.R.upper', 'proportion.leg.L.thigh', 'proportion.leg.R.thigh')
MODE_ROLES = ('arm.L.mode', 'arm.R.mode', 'leg.L.mode', 'leg.R.mode')
FRAMES = (0, 6, 12, 18, 24)
_SESSION = None

def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1048576), b''): h.update(data)
    return h.hexdigest()

def descriptor(value):
    require(type(value) is dict and set(value) == {'file','expected_sha256'}, 'PROPORTION_FILE_DESCRIPTOR')
    require(type(value['file']) is str and Path(value['file']).is_absolute(), 'PROPORTION_ABSOLUTE_FILE')
    sha = value['expected_sha256']
    require(type(sha) is str and len(sha) == 64 and all(c in '0123456789abcdef' for c in sha), 'PROPORTION_SHA256')
    return deepcopy(value)

def verify(value):
    require(file_sha(value['file']) == value['expected_sha256'], 'PROPORTION_FROZEN_INPUT_CHANGED', value['file'])

def canonical_value(value):
    require(type(value) in (float,int) and math.isfinite(value), 'PROPORTION_FINITE_VALUE')
    # Native FloatProperty endpoints are f32; compile only their exact stored
    # encodings back to the declared decimal boundary, without widening it.
    if float(value) == float(np.float32(.9)): return .9
    if float(value) == float(np.float32(1.1)): return 1.1
    require(.9 <= value <= 1.1, 'PROPORTION_INPUT_BOUNDS', value)
    return float(value)

def get_session(required=True):
    require(not required or _SESSION is not None, 'PROPORTION_SESSION_REQUIRED')
    return _SESSION

class BodyAnimationSession:
    def __init__(self, source, profile, controls, neutral_clip, body, outdir, *, idle_guard,
                 source_resources, persistence_writer=None):
        require(callable(idle_guard), 'PROPORTION_IDLE_GUARD_REQUIRED')
        require(persistence_writer is None or callable(persistence_writer), 'PROPORTION_WRITER')
        self.id = 'body_animation_' + uuid.uuid4().hex[:12]
        self.idle_guard, self.writer = idle_guard, persistence_writer
        self.source = descriptor(source)
        self.documents = [descriptor(row) for row in (profile, controls, neutral_clip)]
        require(type(source_resources) is list, 'PROPORTION_SOURCE_RESOURCES_REQUIRED')
        self.resources = [descriptor(row) for row in source_resources]
        self.core = self.animation = self.inspection = self.authored = None
        self._inputs = dict.fromkeys(FIELDS, 1.)
        self.save_attempted = False
        self.last_error = self.last_log_error = self.last_result = self.last_observation = None
        self.sequence = 0
        self.outdir = Path(outdir).resolve()
        require(Path(outdir).is_absolute(), 'PROPORTION_OUTPUT_ABSOLUTE')
        self._dispatch('initialize')
        legacy = sys.modules.get('control_animation.session')
        require(legacy is None or legacy.get_session(False) is None, 'LEGACY_BODY_SESSION_PRESENT')
        self.profile = json.loads(Path(profile['file']).read_bytes())
        self.controls = json.loads(Path(controls['file']).read_bytes())
        require(self.controls['profile'] == profile and self.controls['source'] == self.source, 'PROPORTION_CONTROL_BINDING')
        source_profile = descriptor(self.controls['source_profile']); verify(source_profile)
        original = json.loads(Path(source_profile['file']).read_bytes())
        require(len(original['roles']) == 118 and len(self.profile['roles']) == 122
                and self.profile['roles'][:118] == original['roles'], 'PROPORTION_ORIGINAL_ROLES')
        require({k:v for k,v in original.items() if k not in ('roles','profile_id','revision')}
                == {k:v for k,v in self.profile.items() if k not in ('roles','profile_id','revision')}, 'PROPORTION_ORIGINAL_BINDING')
        self.role_records = deepcopy(self.controls['new_roles'])
        require(tuple(row['role_id'] for row in self.role_records) == ROLE_IDS, 'PROPORTION_ROLE_ROSTER')
        roles = self.profile['roles'][118:]
        for role, record in zip(roles, self.role_records):
            require(role['role_id'] == record['role_id'] and role['bone'] == record['bone']
                and role['requires'] == record['requires'] and role['kind'] == 'scale_set'
                and role['index'] == 1 and role['bounds'] == [.9,1.1]
                and all(record[key] == 1 for key in ('default','neutral','off','reset')), 'PROPORTION_ROLE_SEMANTICS')
        self.neutral = deepcopy(json.loads(Path(neutral_clip['file']).read_bytes())['keys'][0]['operations'])
        require(len(self.neutral) == len({r['role'] for r in self.neutral}) == 35, 'PROPORTION_NEUTRAL_ROSTER')
        modes = {row['role']:row['value'] for row in self.neutral}
        require(all(modes.get(role) == 0 for role in ('arm.L.mode','arm.R.mode','leg.L.mode','leg.R.mode')), 'PROPORTION_FK_MODES')
        self.mode_neutral = [deepcopy(row) for row in self.neutral if row['role'] in MODE_ROLES]
        require(len(self.mode_neutral) == 4 and all(row['value'] == 0 for row in self.mode_neutral), 'PROPORTION_FOUR_FK_MODES')
        self.core = AtomicControls(self.profile)
        self.body = bpy.data.objects.get(body)
        require(self.body is not None and self.body.type == 'MESH', 'PROPORTION_BODY')
        self.outdir.mkdir(parents=True, exist_ok=True)

    @property
    def pending(self):
        return (self.core is not None and self.core.pending is not None) or (self.animation is not None and self.animation.pending is not None)

    @property
    def inputs(self): return deepcopy(self._inputs)

    def status(self):
        # Pure session metadata: never calls Workspace recursively or dereferences
        # scene RNA while another component owns the shared scene.
        return {'session_id':self.id, 'pending':self.pending,
            'preview_pending':self.core is not None and self.core.pending is not None,
            'animation_pending':self.animation is not None and self.animation.pending is not None,
            'inspected':self.inspection is not None, 'inputs':self.inputs,
            'save_attempted':self.save_attempted, 'last_error':self.last_error,
            'log_error':self.last_log_error, 'last_observation':deepcopy(self.last_observation)}

    def workspace_available(self):
        try: return self.idle_guard(operation='body_animation.ui_state', owner=self.id) is True
        except Exception: return False

    def _dispatch(self, operation):
        require(self.idle_guard(operation='body_animation.'+operation, owner=self.id) is True, 'PROPORTION_OTHER_COMPONENT_PENDING')
        require(Path(bpy.data.filepath).resolve() == Path(self.source['file']).resolve(), 'PROPORTION_SOURCE_MAIN')
        for row in [self.source, *self.documents, *self.resources]: verify(row)

    def _log(self, event, result):
        self.sequence += 1; self.last_result = deepcopy(result)
        try:
            path = self.outdir / ('body-%s-%04d-%s.json' % (self.id,self.sequence,event))
            with path.open('x',encoding='utf8') as stream:
                json.dump({'session':self.id,'event':event,'result':result},stream,ensure_ascii=False,allow_nan=False)
        except Exception as error: self.last_log_error = str(error)
        return deepcopy(result)

    def _run(self, event, callback):
        try:
            self._dispatch(event)
            result = callback(); self.last_error = None
            return self._log(event,result)
        except Exception as error:
            self.last_error = getattr(error,'code',type(error).__name__)
            self._log(event,{'status':'error','code':self.last_error,'detail':str(getattr(error,'detail',str(error)))[:12000]})
            raise

    def _idle(self): require(not self.pending, 'PROPORTION_UNDO_FIRST')

    def _invalidate(self):
        require(not self.pending, 'PROPORTION_UNDO_FIRST')
        self.inspection = self.animation = self.authored = None
        self.save_attempted = False

    def set_inputs(self, values):
        def perform():
            self._idle()
            require(type(values) is dict and set(values) == set(FIELDS), 'PROPORTION_INPUT_FIELDS')
            clean = {key:canonical_value(values[key]) for key in FIELDS}
            if clean != self._inputs:
                self._invalidate(); self._inputs = clean
            return {'status':'inputs_set','inputs':self.inputs,'inspection_invalidated':self.inspection is None}
        return self._run('set_inputs',perform)

    def _clip(self):
        require(any(value != 1. for value in self._inputs.values()), 'PROPORTION_NEUTRAL_USE_RESET')
        return {'schema':SCHEMA,'name':'HRIG127_Proportion_'+uuid.uuid4().hex[:12], 'replace_targets':True,
            'keys':[{'frame':frame,'operations':deepcopy(self.mode_neutral)+[
                {'role':role,'value':self._inputs[field] if frame == 12 else 1.}
                for field,role in zip(FIELDS,ROLE_IDS)]} for frame in (0,12,24)]}

    def inspect(self):
        def perform():
            self._idle(); self._invalidate()
            transaction = AnimationTransaction(self.profile,self.source,preview_core=self.core)
            self.animation = transaction
            checked = transaction.inspect(self._clip())
            self.inspection = {'token':checked['snapshot_token'],'inputs':digest(self._inputs)}
            return checked
        return self._run('inspect',perform)

    def _observe_inputs(self):
        return {'frame':bpy.context.scene.frame_current_final,'scaleY':{
            record['role_id']:float(self.core.rig.pose.bones[record['bone']].scale[1]) for record in self.role_records}}

    def author(self):
        def perform():
            self._idle()
            require(self.animation is not None and self.inspection is not None
                and self.inspection['inputs'] == digest(self._inputs), 'PROPORTION_INSPECT_CURRENT_INPUTS')
            token = self.inspection['token']; self.inspection = None
            result = self.animation.author(token)
            self.authored = deepcopy(result); self.save_attempted = False
            self.last_observation = self._observe_inputs()
            return result
        return self._run('author',perform)

    def show_frame(self, frame):
        def perform():
            require(self.animation is not None and self.animation.pending is not None
                and self.core.pending is None, 'PROPORTION_NO_ACTION_PENDING')
            require(type(frame) is int and 0 <= frame <= 24, 'PROPORTION_FRAME')
            self.animation._set_frame(frame)
            p = self.animation.pending
            self.animation._verify_inputs(p['action'],p['slot'],p['plan'])
            self.animation._check_original_actions()
            self.last_observation = self._observe_inputs()
            return {'status':'frame_observed',**deepcopy(self.last_observation),'declared_inputs_exact':True}
        return self._run('show_frame',perform)

    def _undo_owned(self):
        require(self.core.pending is None, 'PROPORTION_RESTORE_BODY_PREVIEW_FIRST')
        require(self.animation is not None and self.animation.pending is not None, 'PROPORTION_NO_ACTION_PENDING')
        self.animation._set_frame(self.animation.pending['frame'])
        result = self.animation.undo(self.animation.pending['token'])
        self.animation = self.inspection = self.authored = None
        self.save_attempted = False; self.last_observation = self._observe_inputs()
        return {'status':'animation_undone','native':result,'source_restored':True}

    def undo(self): return self._run('undo',self._undo_owned)

    def reset_off(self):
        def perform():
            native = self._undo_owned() if self.pending else None
            self._invalidate(); self._inputs = dict.fromkeys(FIELDS,1.)
            return {'status':'proportion_off','inputs':self.inputs,'native':native,
                'semantics':'four local proportions reset to 1; original body Action restored when owned pending existed'}
        return self._run('reset_off',perform)

    def save_copy(self):
        def perform():
            require(self.core.pending is None and self.animation is not None
                and self.animation.pending is not None and not self.save_attempted, 'PROPORTION_SAVE_STATE')
            p = self.animation.pending
            self.animation._set_frame(p['frame'])
            require(bpy.context.scene.frame_current_final == 0, 'PROPORTION_SAVE_FRAME_ZERO')
            self.animation._verify_inputs(p['action'],p['slot'],p['plan'])
            self.animation._verify_target_curves(p['action'],p['slot'],p['plan'])
            self.animation._check_original_actions(); self.core.observe()
            before = C.capture()
            require(before == p['author_after'], 'PROPORTION_SAVE_EXTERNAL_DRIFT')
            path = self.outdir / 'output-local-proportion.blend'
            require(not path.exists() and not path.is_symlink() and path.resolve() != Path(self.source['file']).resolve(), 'PROPORTION_OUTPUT_EXISTS_OR_SOURCE')
            writer = self.writer
            if writer is None:
                from persistence import save_candidate
                writer = save_candidate
            main = bpy.data.filepath; self.save_attempted = True
            saved = writer(path,self.outdir)
            require(Path(saved['file']).resolve() == path.resolve() and path.is_file()
                and file_sha(path) == saved['expected_sha256'], 'PROPORTION_SAVE_RECEIPT')
            require(bpy.data.filepath == main and C.capture() == before, 'PROPORTION_SAVE_CHANGED_AUTHOR_OR_MAIN')
            verify(self.source)
            return {'status':'copy_saved','candidate':{**saved,'bytes':path.stat().st_size},
                'archive_key':p['archive_key'],'clip':deepcopy(p['plan']['clip']),
                'frame':0,'qualification':'saved_requires_independent_cold_storage_and_dynamic_checks'}
        return self._run('save_copy',perform)

def initialize(source, profile, controls, neutral_clip, body, outdir, *, idle_guard,
               source_resources, persistence_writer=None, register_ui=True):
    global _SESSION
    require(_SESSION is None or not _SESSION.pending, 'PROPORTION_SESSION_PENDING')
    session = BodyAnimationSession(source,profile,controls,neutral_clip,body,outdir,
        idle_guard=idle_guard,source_resources=source_resources,persistence_writer=persistence_writer)
    _SESSION = session
    if register_ui:
        from . import ui
        ui.register(); ui.sync_from_session()
    return session
