"""Shared batch-rest GUI/CLI session with explicit editable intent rows."""

from hrig_paths import project_path
from copy import deepcopy
from pathlib import Path
import hashlib,importlib.util,json,uuid
import bpy
from .native import BatchRestTransaction,data_rest_signature
from .contract import validate,need,digest
from .semantic import compile_roles
_SESSION=None
def get_session(required=True):
    if required:need(_SESSION is not None,'BATCH_SESSION_REQUIRED')
    return _SESSION
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
class BatchSession:
    def __init__(self,profile_path,source,body,outdir,request,*,new_controls=(),idle_guard=lambda:True):
        self.profile_path=str(Path(profile_path).resolve());self.profile_sha=sha(self.profile_path)
        self.profile=json.loads(Path(self.profile_path).read_bytes());self.source=deepcopy(source)
        self.outdir=Path(outdir).resolve();self.outdir.mkdir(parents=True,exist_ok=True)
        self.defaults=validate(request);self.new_controls=deepcopy(list(new_controls));self.external_idle=idle_guard
        self.tx=BatchRestTransaction(self.profile,source,body,self._guard)
        self.snapshot_token=None;self.saved=None;self.save_attempted=False;self.last_error=None;self.sequence=0
    @property
    def pending(self):return self.tx.pending is not None
    def _guard(self):
        need(sha(self.profile_path)==self.profile_sha,'SOURCE_PROFILE_FILE_CHANGED')
        return self.external_idle() is True
    def log(self,event,value):
        self.sequence+=1
        with (self.outdir/'events.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps({'sequence':self.sequence,'event':event,'result':value},ensure_ascii=False,allow_nan=False)+'\n')
        return value
    def run(self,event,fn):
        try:r=fn();self.last_error=None;return self.log(event,r)
        except Exception as e:self.last_error=getattr(e,'code',type(e).__name__);self.log(event,{'error':self.last_error,'detail':str(e)});raise
    def request(self,p):
        atoms=[]
        for item in p.atoms:
            a=json.loads(item.template);a['bone']=item.bone
            for k in list(a['fields']):a['fields'][k]=list(getattr(item,k)) if k in ('head','tail') else (item.parent or None) if k=='parent' else getattr(item,k)
            if a['op']=='create':a.update(donor=item.donor,parent=item.parent or None,collections=[n.strip() for n in item.collections.split(';') if n.strip()])
            atoms.append(a)
        return validate({'schema':'hrig.rest-workspace.v1','atoms':atoms,'side_pairs':[[v.left,v.right] for v in p.side_pairs]})
    def invalidate(self):
        if not self.pending:self.snapshot_token=None
    def inspect(self,p):
        def perform():
            r=self.tx.inspect(self.request(p));self.snapshot_token=r['snapshot_token'];return r
        return self.run('inspect',perform)
    def preview(self):
        def perform():
            need(self.snapshot_token is not None,'INSPECT_FIRST')
            try:return self.tx.preview(self.snapshot_token)
            finally:self.snapshot_token=None
        return self.run('preview',perform)
    def restore(self):
        def perform():
            need(self.pending,'NO_PENDING');r=self.tx.restore(self.tx.pending['token']);self.snapshot_token=None;return r
        return self.run('restore',perform)
    def save_copy(self):
        def perform():
            need(self.pending and self.tx.applied and not self.save_attempted,'SAVE_APPLIED_ONCE')
            self.tx.validate_applied();b=self.tx.base;request=self.tx.request;profile=deepcopy(self.profile);evidence=[]
            changed=set(self.tx.applied['raw_plan']['affected'])
            for bone in sorted(changed & {r['bone'] for r in profile['roles']}):
                profile,rows=compile_roles(profile,b.rig,b.original_data,bone);evidence.extend(rows)
            newnames=set(self.tx.applied['raw_plan']['born'])
            for c in self.new_controls:
                need(set(c)=={'role_id','bone','axis','index','bounds','label'} and c['bone'] in newnames,'NEW_CONTROL_DEFINITION',c)
                p=b.rig.pose.bones[c['bone']]
                profile['roles'].append({**deepcopy(c),'parent':p.parent.name if p.parent else None,'rotation_mode':p.rotation_mode,
                    'kind':'quaternion_delta' if p.rotation_mode=='QUATERNION' else 'euler_delta','property':None,'requires':[]})
            profile['profile_id']+='.rest-workspace.'+digest(request)[:12];profile['revision']+=1
            profile['rig'].update(data=b.rig.data.name,rest_sha256=data_rest_signature(b.rig.data),
                witness_bones=sorted(set(profile['rig']['witness_bones'])|newnames))
            from native_controls.contract import validate as validate_profile
            validate_profile(profile)
            self.save_attempted=True
            f=Path(project_path('work/hrig-p2-v015/addon/humanoid_rigging_probe/persistence.py'))
            spec=importlib.util.spec_from_file_location('_batch_session_writer',f);writer=importlib.util.module_from_spec(spec);spec.loader.exec_module(writer)
            path=self.outdir/('output-rest-'+uuid.uuid4().hex[:8]+'.blend');receipt=writer.save_candidate(path,self.outdir)
            self.tx.validate_applied();profilepath=path.with_suffix('.profile.json');binding=path.with_suffix('.binding.json')
            with profilepath.open('x',encoding='utf8') as out:json.dump(profile,out,ensure_ascii=False,allow_nan=False,indent=2)
            record={'request':request,'source':self.source,'candidate':{'file':str(path),'expected_sha256':sha(path)},
                'source_profile':{'file':self.profile_path,'expected_sha256':self.profile_sha},'compiled_axes':evidence,
                'raw_plan_sha256':digest(self.tx.applied['raw_plan']),'native_reference_uid':self.tx.snapshot['reference_uid'],
                'native_replay_uid':self.tx.applied['reference_uid'],'cold_required':True}
            with binding.open('x',encoding='utf8') as out:json.dump(record,out,ensure_ascii=False,allow_nan=False,indent=2)
            self.saved={'status':'copy_saved','candidate':record['candidate'],'profile':{'file':str(profilepath),'expected_sha256':sha(profilepath)},
                'binding':{'file':str(binding),'expected_sha256':sha(binding)},'writer':receipt}
            return self.saved
        return self.run('save',perform)
def initialize(profile_path,source,body,outdir,request,*,new_controls=(),idle_guard=lambda:True,register_ui=True):
    global _SESSION
    need(_SESSION is None or not _SESSION.pending,'SESSION_PENDING')
    _SESSION=BatchSession(profile_path,source,body,outdir,request,new_controls=new_controls,idle_guard=idle_guard)
    if register_ui:
        from . import ui
        ui.register();ui.assign(bpy.context.window_manager.hrig_d2_batch,request)
    return _SESSION
