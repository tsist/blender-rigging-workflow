"""GUI/CLI session for the finite roll editor; no global preference changes."""

from hrig_paths import project_path
from copy import deepcopy
from pathlib import Path
from datetime import datetime,timezone
import importlib.util,json,uuid
import bpy
from .contract import require,validate_request,digest,operation
from .native import RestEditTransaction
from .profile import derive
from .witness import arrays,write_arrays,sha
_SESSION=None
def get_session(required=True):
    if required:require(_SESSION is not None,'REST_SESSION_NOT_INITIALIZED')
    return _SESSION
def idle():
    from control_animation.session import get_session as cget
    from weight_edit.session import get_session as wget
    c,w=cget(False),wget(False)
    return (c is None or not(c.preview_active or c.animation_active)) and (w is None or not w.pending)
class RestEditSession:
    def __init__(self,profile_path,source,body,outdir,request,bone_choices=None):
        self.profile_path=str(Path(profile_path).resolve());self.profile=json.loads(Path(self.profile_path).read_bytes())
        self.source=deepcopy(source);self.body=body;self.defaults=validate_request(request);self.edit_operation=operation(self.defaults)
        self.outdir=Path(outdir).resolve();self.outdir.mkdir(parents=True,exist_ok=True)
        self.id=uuid.uuid4().hex[:12];self.seq=0;self.snapshot_token=None;self.save_attempted=False;self.saved=None;self.last_error=None;self.log_error=None
        self.bone_items=[(n,n,'') for n in (bone_choices or [request['bone']])]
        self.tx=RestEditTransaction(self.profile,self.source,body,idle_guard=idle)
        self.log('initialize',{'source':self.source,'body':body,'frame':bpy.context.scene.frame_current_final,'request':request})
    @property
    def pending(self):return self.tx.pending is not None
    def invalidate(self):
        if not self.pending:self.snapshot_token=None
    def log(self,event,result):
        self.seq+=1;r={'session_id':self.id,'sequence':self.seq,'time_utc':datetime.now(timezone.utc).isoformat(),'event':event,'result':deepcopy(result)}
        try:
            text=json.dumps(r,ensure_ascii=False,allow_nan=False)
            with (self.outdir/('result-%s-%04d-%s.json'%(self.id,self.seq,event))).open('x',encoding='utf8') as f:f.write(text)
            with (self.outdir/'events.jsonl').open('a',encoding='utf8') as f:f.write(text+'\n')
        except Exception as e:self.log_error=str(e)
        return deepcopy(result)
    def run(self,event,callback):
        try:r=callback();self.last_error=None;return self.log(event,r)
        except Exception as e:self.last_error=getattr(e,'code',type(e).__name__);self.log(event,{'status':'error','code':self.last_error,'message':str(e)[:12000]});raise
    def witness(self,label):
        try:
            path=self.outdir/('witness-'+self.id+'-'+label+'-'+uuid.uuid4().hex[:8]+'.npz')
            descriptor=write_arrays(path,arrays(self.tx.rig,self.body,self.profile['rig']['witness_bones'],self.tx.original_data,include_lengths=self.edit_operation in ('tail','head','parent')))
            if self.edit_operation=='parent':
                from .witness import write_topology
                descriptor['topology']=write_topology(path.with_suffix('.topology.json'),self.tx.rig.data,self.tx.original_data)
            return descriptor
        except Exception as e:self.log_error=str(e);return {'status':'artifact_failed','error':str(e)}
    def request(self,p):
        if p.edit_mode=='PARENT':
            return {'schema':'hrig.rest-parent-change.v1','bone':p.bone,'from_parent':p.from_parent,'to_parent':p.to_parent,
                'raw_policy':'preserve_head_tail_roll_connect','mirror_policy':'explicit_only'}
        if p.edit_mode=='HEAD':
            return {'schema':'hrig.rest-head-delta.v1','bone':p.bone,'delta':list(p.head_delta),'space':'ARMATURE_LOCAL',
                'tail_policy':'preserve','roll_policy':'preserve','mirror_policy':'explicit_only'}
        if p.edit_mode=='TAIL':
            return {'schema':'hrig.rest-tail-delta.v1','bone':p.bone,'delta':list(p.tail_delta),'space':'ARMATURE_LOCAL',
                'head_policy':'preserve','roll_policy':'preserve','mirror_policy':'explicit_only'}
        require(p.edit_mode=='ROLL','REST_EDIT_MODE')
        return {'schema':'hrig.rest-roll-delta.v1','bone':p.bone,'delta':float(p.delta),'mirror_policy':'explicit_only'}
    def inspect(self,p):
        def perform():
            require(not self.pending,'REST_RESTORE_FIRST');request=self.request(p);r=self.tx.inspect(request);self.snapshot_token=r['snapshot_token'];self.edit_operation=operation(request)
            return {'status':'inspected','native':r,'witness':self.witness('before')}
        return self.run('inspect',perform)
    def preview(self):
        def perform():
            require(self.snapshot_token is not None,'REST_INSPECT_FIRST')
            try:r=self.tx.preview(self.snapshot_token)
            finally:self.snapshot_token=None
            return {'status':'previewed','native':r,'witness':self.witness('preview')}
        return self.run('preview',perform)
    def restore(self):
        def perform():
            require(self.pending,'REST_NO_PENDING');r=self.tx.restore(self.tx.pending['token']);self.snapshot_token=None
            return {'status':'restored','native':r,'witness':self.witness('restored')}
        return self.run('restore',perform)
    def save_copy(self):
        def perform():
            require(self.pending and self.tx.pending['phase']=='applied','REST_APPLIED_REQUIRED')
            require(bpy.context.scene.frame_current_final==0,'REST_SAVE_FRAME_ZERO');require(not self.save_attempted,'REST_SAVE_ALREADY_ATTEMPTED')
            op=operation(self.tx._pending['request'])
            dest=self.outdir/('output-D2-'+op+'-edit.blend');profile_dest=self.outdir/'output-native-profile.json';binding_dest=self.outdir/'output-profile-binding.json'
            require(not any(p.exists() or p.is_symlink() for p in (dest,profile_dest,binding_dest)),'REST_OUTPUT_EXISTS')
            before=self.tx.validate_applied();evidence=self.tx.binding_evidence();main=bpy.data.filepath
            self.save_attempted=True
            path=Path(project_path('work/hrig-p2-v015/addon/humanoid_rigging_probe/persistence.py'))
            spec=importlib.util.spec_from_file_location('_hrig097_writer',path);writer=importlib.util.module_from_spec(spec);spec.loader.exec_module(writer)
            receipt=writer.save_candidate(dest,self.outdir)
            require(bpy.data.filepath==main and sha(self.source['file'])==self.source['expected_sha256'],'REST_SAVE_SOURCE_CHANGED')
            require(self.tx.validate_applied()==before,'REST_SAVE_AUTHOR_CHANGED')
            candidate={'file':str(dest),'expected_sha256':receipt['expected_sha256']}
            derived=derive(self.profile,evidence,candidate)
            with profile_dest.open('x',encoding='utf8') as f:json.dump(derived['profile'],f,ensure_ascii=False,allow_nan=False,indent=2)
            with binding_dest.open('x',encoding='utf8') as f:json.dump(derived['binding'],f,ensure_ascii=False,allow_nan=False,indent=2)
            self.saved={'candidate':candidate,'profile':{'file':str(profile_dest),'expected_sha256':sha(profile_dest)},'binding':{'file':str(binding_dest),'expected_sha256':sha(binding_dest)}}
            return {'status':'copy_saved_with_profile_proposal',**deepcopy(self.saved),'native_validation':before,'writer':receipt,
                'qualification':'finite control-identity proposal; independent cold/control checks required; not artistic adoption or changed-rest skin equivalence'}
        return self.run('save_copy',perform)
def initialize(profile_path,source,body,outdir,request,bone_choices=None):
    global _SESSION
    require(_SESSION is None or not _SESSION.pending,'REST_SESSION_HAS_PENDING')
    _SESSION=RestEditSession(profile_path,source,body,outdir,request,bone_choices)
    from . import ui
    ui.register();p=bpy.context.window_manager.hrig_d2_rest;ui.assign_request(p,request)
    return _SESSION
