"""One existing FaceKeyTransaction with explicit raw selection and frozen inputs."""
from copy import deepcopy
from pathlib import Path
import json,uuid
import bpy,numpy as np
from facial_key_edit.contract import require,digest,validate_plan
from facial_key_edit.native import FaceKeyTransaction,FACE_OWNERS,FACE_BOX,coords,co_sha,file_sha

_SESSION=None

def get_session(required=True):
    require(not required or _SESSION is not None,'KEY_UI_NOT_INITIALIZED')
    return _SESSION

class KeyEditSession:
    def __init__(self,source,profile,outdir,*,idle_guard=None,persistence_writer=None):
        self.source=deepcopy(source)
        self.profile=json.loads(Path(profile).read_bytes()) if isinstance(profile,(str,Path)) else deepcopy(profile)
        self.tx=FaceKeyTransaction(source,self.profile,idle_guard=idle_guard)
        self.id=self.tx.id;self.idle_guard=idle_guard;self.writer=persistence_writer
        require(persistence_writer is None or callable(persistence_writer),'KEY_UI_WRITER')
        self._dispatch('face_key_ui.initialize')
        self.outdir=Path(outdir);require(self.outdir.is_absolute(),'KEY_UI_OUTPUT_ABSOLUTE')
        self.outdir.mkdir(parents=True,exist_ok=True)
        self.items=[]
        for name,(mesh,key) in FACE_OWNERS.items():
            obj=bpy.data.objects.get(name)
            if obj is not None and obj.type=='MESH' and obj.data.name==mesh and obj.data.shape_keys and obj.data.shape_keys.name==key:
                self.items.append((name,name,'真实原始网格面部对象'))
        require(bool(self.items),'KEY_UI_NO_FACE_OWNERS')
        body='千早爱音- 赋权躯干网格'
        self.inputs={'owner':body if any(x[0]==body for x in self.items) else self.items[0][0],
                     'operation':'append','target':'HRIG_D3_UI_Cheek','delta':[0.,-.001,0.],'strength':.5}
        self.selected_ids=None;self.inspection=None;self.sequence=0;self.last_error=None
        self.last_result=None;self.save_attempted=False
    @property
    def pending(self):return self.tx.pending is not None
    @property
    def value_pending(self):return self.tx.value_pending is not None
    def status(self):
        return {'owner_id':self.id,'pending':self.pending,'value_pending':self.value_pending,
                'inspected':self.inspection is not None,'selection_count':len(self.selected_ids or ()),
                'witness_bone_count':len(self.profile['rig']['witness_bones']),'last_error':self.last_error}
    def _dispatch(self,operation):
        require(Path(bpy.data.filepath).resolve()==Path(self.source['file']).resolve()
                and file_sha(self.source['file'])==self.source['expected_sha256'],'KEY_UI_SOURCE_DRIFT')
        if self.idle_guard is not None:
            require(self.idle_guard(operation=operation,owner=self.id) is True,'KEY_UI_OTHER_MODULE_PENDING')
    def _idle(self):require(not self.pending and not self.value_pending,'KEY_UI_RESTORE_FIRST')
    def log(self,event,result):
        self.sequence+=1;self.last_result=deepcopy(result)
        path=self.outdir/('key-ui-%s-%04d-%s.json'%(self.id,self.sequence,event))
        with path.open('x',encoding='utf8') as stream:
            json.dump({'session_id':self.id,'event':event,'result':result},stream,ensure_ascii=False,allow_nan=False)
        return result
    def set_inputs(self,values):
        self._dispatch('face_key_ui.inputs');self._idle()
        require(type(values) is dict and set(values)==set(self.inputs),'KEY_UI_INPUT_FIELDS')
        require(values['owner'] in [x[0] for x in self.items] and values['operation'] in ('append','edit'),'KEY_UI_INPUT_OWNER')
        require(type(values['target']) is str and values['target'] and '\x00' not in values['target']
                and len(values['target'].encode('utf8'))<=63,'KEY_UI_TARGET')
        delta=np.asarray(values['delta'],dtype=np.float64)
        require(delta.shape==(3,) and bool(np.isfinite(delta).all()) and float(np.linalg.norm(delta))<=.01,'KEY_UI_DELTA_LIMIT')
        require(type(values['strength']) in (float,int) and np.isfinite(values['strength'])
                and 0<=values['strength']<=1,'KEY_UI_STRENGTH')
        clean=deepcopy(values);clean['delta']=delta.tolist();clean['strength']=float(values['strength'])
        if clean!=self.inputs:
            if clean['owner']!=self.inputs['owner']:self.selected_ids=None
            self.inspection=None;self.tx.snapshot=None;self.inputs=clean
        return self.status()
    def _owner(self):
        obj=bpy.data.objects.get(self.inputs['owner'])
        require(obj is not None and obj.type=='MESH' and obj.mode=='OBJECT' and not obj.data.is_editmode,
                'KEY_UI_OBJECT_MODE_REQUIRED')
        require(obj.library is None and obj.override_library is None and obj.data.library is None
                and obj.data.override_library is None and [o for o in bpy.data.objects if o.type=='MESH' and o.data==obj.data]==[obj],
                'KEY_UI_OWNER_SHARED_OR_LINKED')
        mesh,key_id=FACE_OWNERS[obj.name]
        require(obj.data.name==mesh and obj.data.shape_keys is not None and obj.data.shape_keys.name==key_id,'KEY_UI_OWNER_IDENTITY')
        return obj
    def _selection(self,obj):
        require(bpy.context.view_layer.objects.active==obj,'KEY_UI_ACTIVATE_FACE_OWNER')
        return tuple(v.index for v in obj.data.vertices if v.select)
    def read_selection(self):
        self._dispatch('face_key_ui.read_selection');self._idle();obj=self._owner();ids=self._selection(obj)
        require(1<=len(ids)<=8192,'KEY_UI_SELECTION_COUNT')
        raw=coords(obj.data.vertices);lo=np.asarray(FACE_BOX[0]);hi=np.asarray(FACE_BOX[1]);ix=np.asarray(ids,dtype=np.int32)
        require(bool(((raw[ix]>=lo)&(raw[ix]<=hi)).all()),'KEY_UI_SELECTION_NOT_FACE')
        if obj.name=='千早爱音- 赋权躯干网格':
            group=obj.vertex_groups.get('形变-头颅');require(group is not None and group.index==175,'KEY_UI_HEAD_MASK')
            require(all(any(g.group==175 and g.weight>0 for g in obj.data.vertices[i].groups) for i in ids),'KEY_UI_HEAD_MEMBERSHIP')
        self.selected_ids=ids;self.inspection=None;self.tx.snapshot=None
        return self.log('selection',{'status':'selection_read','object':obj.name,'raw_vertex_ids':list(ids),
                                    'raw_vertex_count':len(obj.data.vertices),'basis_untouched':True})
    def build_plan(self):
        self._dispatch('face_key_ui.build_plan');self._idle();obj=self._owner()
        require(self.selected_ids is not None and self._selection(obj)==self.selected_ids,'KEY_UI_SELECTION_CHANGED')
        key=obj.data.shape_keys;reference=key.reference_key;target=key.key_blocks.get(self.inputs['target'])
        append=self.inputs['operation']=='append'
        require(target is None if append else target is not None and target!=reference,'KEY_UI_TARGET_IDENTITY')
        raw=coords(obj.data.vertices);ref=coords(reference.data);original=ref if append else coords(target.data)
        ix=np.asarray(self.selected_ids,dtype=np.int32)
        wanted=(original[ix].astype(np.float64)+np.asarray(self.inputs['delta'])).astype('<f4')
        require(float(np.linalg.norm(wanted.astype(np.float64)-original[ix].astype(np.float64),axis=1).max())<=.01,
                'KEY_UI_STORED_DELTA_LIMIT')
        return validate_plan({'schema':'hrig.face-coordinate-edit.v1','operation':self.inputs['operation'],
            'object':obj.name,'mesh':obj.data.name,'key_id':key.name,'reference':reference.name,'target':self.inputs['target'],
            'raw_mesh_sha256':co_sha(raw),'reference_sha256':co_sha(ref),
            'target_before_sha256':None if append else co_sha(original),
            'region_vertices':list(self.selected_ids),'coordinates':wanted.tolist()})
    def inspect(self):
        plan=self.build_plan();result=self.tx.inspect(plan)
        self.inspection={'token':result['snapshot_token'],'inputs_sha256':digest(self.inputs),
                         'plan_sha256':digest(plan),'selected_ids':self.selected_ids}
        return self.log('inspect',result)
    def apply(self):
        self._dispatch('face_key_ui.apply');self._idle()
        checked=self.inspection
        require(checked is not None and checked['inputs_sha256']==digest(self.inputs)
                and checked['selected_ids']==self.selected_ids and digest(self.build_plan())==checked['plan_sha256'],
                'KEY_UI_STALE_INSPECTION')
        result=self.tx.apply(checked['token']);self.inspection=None;self.save_attempted=False
        return self.log('apply',result)
    def preview_value(self):
        return self.log('preview_value',self.tx.preview_value(self.inputs['strength']))
    def restore_value(self):
        return self.log('restore_value',self.tx.restore_value())
    def save_copy(self):
        self._dispatch('face_key_ui.save_copy')
        require(self.pending and not self.value_pending and not self.save_attempted,'KEY_UI_SAVE_STATE')
        self.save_attempted=True
        writer=self.writer
        if writer is None:
            from persistence import save_candidate
            writer=save_candidate
        path=self.outdir/('output-Key-UI-%s.blend'%uuid.uuid4().hex[:12])
        return self.log('save_copy',self.tx.save_copy(str(path),str(self.outdir),writer))
    def restore(self):
        self._dispatch('face_key_ui.restore')
        require(not self.value_pending,'KEY_UI_RESTORE_VALUE_FIRST')
        result=self.tx.restore();self.inspection=None;self.save_attempted=False
        return self.log('restore',{'status':'restored','native':result,'full_C_context_witness_exact':True,
                                   'witness_bone_count':len(self.profile['rig']['witness_bones'])})

def initialize(source,profile,outdir,*,idle_guard=None,persistence_writer=None,register_ui=True):
    global _SESSION
    require(_SESSION is None or not _SESSION.pending,'KEY_UI_SESSION_PENDING')
    session=KeyEditSession(source,profile,outdir,idle_guard=idle_guard,persistence_writer=persistence_writer)
    _SESSION=session
    if register_ui:
        from . import ui
        ui.register();ui.sync_from_session()
    return session
