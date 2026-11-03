"""Guarded append/edit/preview/save/restore for explicit Anion front-face points.

No driven Key value assignment, no topology/Basis/source/old Action edit. Native
author projection changes only one named Mesh shape hash and exact Key row.
"""
from copy import deepcopy
import hashlib,json,uuid
from pathlib import Path
import bpy,numpy as np
import character_state as C
import native_control as N
from .contract import validate_plan,require,digest

FACE_BOX=((-0.12,-0.12,1.39),(0.12,0.04,1.62))
FACE_OWNERS={
 '千早爱音- 赋权躯干网格':('千早爱音- 赋权躯干网格','修型形态键'),
 '千早爱音- 赋权上睫毛网格':('千早爱音- 赋权上睫毛网格.L','Key.006'),
 '千早爱音- 赋权下睫毛网格':('千早爱音- 赋权下睫毛网格.L','Key.005'),
 '千早爱音- 赋权瞳孔网格.L':('千早爱音- 赋权瞳孔网格.L','Key.004'),
 '千早爱音- 赋权瞳孔网格.R':('千早爱音- 赋权瞳孔网格.R','Key.007'),
 '千早爱音- 虎牙':('千早爱音- 虎牙','Key.008')}

def coords(block):
    a=np.empty(len(block)*3,dtype='<f4');block.foreach_get('co',a);return a.reshape((-1,3))
def co_sha(a):return hashlib.sha256(np.asarray(a,dtype='<f4').tobytes()).hexdigest()
def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def author_rows(snapshot,obj):
    mesh=next(x for x in snapshot['blocks'] if x['type']=='Mesh' and x['name']==obj.data.name and x['library'] is None)
    key=next(x for x in snapshot['blocks'] if x['type']=='Key' and x['name']==obj.data.shape_keys.name and x['owner']['name']==obj.data.name and x['library'] is None)
    return mesh,key
def witness(profile):
    rig=bpy.data.objects[profile['rig']['object']];ev=rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    a=np.asarray([ev.matrix_world@ev.pose.bones[n].matrix for n in profile['rig']['witness_bones']],dtype='<f4')
    require(bool(np.isfinite(a).all()),'EDIT_WITNESS_NONFINITE');return a

class FaceKeyTransaction:
    def __init__(self,source,profile,*,idle_guard=None):
        require(type(source) is dict and set(source)=={'file','expected_sha256'} and Path(source['file']).is_absolute(),'EDIT_SOURCE')
        require(Path(bpy.data.filepath).resolve()==Path(source['file']).resolve() and file_sha(source['file'])==source['expected_sha256'],'EDIT_SOURCE_MAIN')
        require(idle_guard is None or callable(idle_guard),'EDIT_IDLE_GUARD')
        require(tuple(bpy.app.version)==tuple(profile['runtime']['blender_version'])
                and bpy.app.build_hash.decode()==profile['runtime']['build_hash']
                and bpy.context.scene.name==profile['runtime']['scene']
                and bpy.context.view_layer.name==profile['runtime']['view_layer'],'EDIT_RUNTIME_CONTEXT')
        rig=bpy.data.objects.get(profile['rig']['object'])
        require(rig is not None and rig.type=='ARMATURE' and rig.data.name==profile['rig']['data']
                and sorted(rig.pose.bones.keys())==sorted(profile['rig']['witness_bones'])
                and rig.data.pose_position=='POSE','EDIT_RIG_BINDING')
        self.id='face_key_'+uuid.uuid4().hex;self.idle_guard=idle_guard
        self.source=deepcopy(source);self.profile=deepcopy(profile);self.snapshot=None;self.pending=None;self.value_pending=None
    def _external(self,operation):
        require(Path(bpy.data.filepath).resolve()==Path(self.source['file']).resolve()
                and file_sha(self.source['file'])==self.source['expected_sha256'],'EDIT_SOURCE_DISPATCH_DRIFT')
        if self.idle_guard is not None:require(self.idle_guard(operation=operation,owner=self.id) is True,'EDIT_OTHER_MODULE_PENDING')
    def _owner(self,plan):
        obj=bpy.data.objects.get(plan['object'])
        require(obj is not None and obj.type=='MESH' and obj.mode=='OBJECT' and obj.library is None and obj.override_library is None,'EDIT_OWNER')
        mesh=obj.data;key=mesh.shape_keys
        require(plan['object'] in FACE_OWNERS and FACE_OWNERS[plan['object']]==(plan['mesh'],plan['key_id']),'EDIT_DECLARED_FACE_OWNER')
        require(mesh.name==plan['mesh'] and mesh.library is None and mesh.override_library is None and not mesh.is_editmode
                and [o for o in bpy.data.objects if o.type=='MESH' and o.data==mesh]==[obj],'EDIT_MESH_OWNER')
        require(key is not None and key.name==plan['key_id'] and key.use_relative and key.library is None and key.override_library is None
                and key.reference_key.name==plan['reference'],'EDIT_KEY_OWNER')
        require(co_sha(coords(mesh.vertices))==plan['raw_mesh_sha256'] and co_sha(coords(key.reference_key.data))==plan['reference_sha256'],'EDIT_RAW_BASIS_DRIFT')
        return obj,key
    def inspect(self,plan):
        self._external('face_keys.inspect')
        require(self.pending is None,'EDIT_ALREADY_PENDING');plan=validate_plan(plan);obj,key=self._owner(plan)
        require(max(plan['region_vertices'])<len(obj.data.vertices),'EDIT_REGION_RANGE')
        group=obj.vertex_groups.get('形变-头颅') if obj.name=='千早爱音- 赋权躯干网格' else None
        if obj.name=='千早爱音- 赋权躯干网格':require(group is not None and group.index==175,'EDIT_FACE_MASK_IDENTITY')
        raw=coords(obj.data.vertices);lo=np.asarray(FACE_BOX[0]);hi=np.asarray(FACE_BOX[1])
        allowed={v.index for v in obj.data.vertices if all(lo<=raw[v.index]) and all(raw[v.index]<=hi)
                 and (group is None or any(g.group==group.index and g.weight>0 for g in v.groups))}
        require(set(plan['region_vertices'])<=allowed,'EDIT_NON_FACE_POINT')
        target=key.key_blocks.get(plan['target'])
        require(target is None if plan['operation']=='append' else target is not None and target!=key.reference_key,'EDIT_TARGET_IDENTITY')
        original=coords(key.reference_key.data) if target is None else coords(target.data)
        if target is not None:require(co_sha(original)==plan['target_before_sha256'] and target.value==0.,'EDIT_TARGET_BASELINE')
        wanted=original.copy();indices=np.asarray(plan['region_vertices'],dtype=np.int32);wanted[indices]=np.asarray(plan['coordinates'],dtype='<f4')
        displacement=np.linalg.norm(wanted[indices].astype(np.float64)-original[indices].astype(np.float64),axis=1)
        require(bool(np.isfinite(wanted).all()) and float(displacement.max())<=.01,'EDIT_CASE_DISPLACEMENT_LIMIT')
        outside=np.ones(len(wanted),dtype=bool);outside[indices]=False
        require(wanted[outside].tobytes()==original[outside].tobytes(),'EDIT_OUTSIDE_REGION_CHANGED')
        bpy.context.view_layer.update();w=witness(self.profile);before=C.capture();ctx=N.context()
        token='face_edit_snap_'+uuid.uuid4().hex
        self.snapshot={'token':token,'plan':plan,'obj':obj,'mesh':obj.data,'key':key,'target':target,
                       'original':original,'wanted':wanted,'before':before,'context':ctx,'witness':w,
                       'active':obj.active_shape_key_index,'outside':outside}
        return {'status':'inspected','snapshot_token':token,'plan':deepcopy(plan),'plan_sha256':digest(plan),
                'target_expected_sha256':co_sha(wanted),'non_region_stored_f32_exact':True,'region_count':len(indices)}
    def _expected(self,state,target_sha,target_value=None):
        expected=deepcopy(state['before']);mesh,key=author_rows(expected,state['obj']);plan=state['plan']
        mesh['mesh']['hashes']['shape:'+plan['target']]=target_sha
        rows=key['character_extra']['blocks']
        if plan['operation']=='append':
            row=deepcopy(next(b for b in rows if b['name']==plan['reference']))
            row['name']=plan['target'];row['settings'].update(name=plan['target'],select=False,value=0.,vertex_group='',mute=False,
                                                          lock_shape=False,slider_min=0.,slider_max=1.,custom_properties={})
            row['relative_key']=plan['reference'];rows.append(row)
        if target_value is not None:next(r for r in rows if r['name']==plan['target'])['settings']['value']=target_value
        return expected
    def apply(self,token):
        self._external('face_keys.apply')
        s=self.snapshot;require(self.pending is None and s is not None and token==s['token'],'EDIT_STALE_SNAPSHOT')
        obj,key=self._owner(s['plan'])
        require(obj==s['obj'] and obj.data==s['mesh'] and key==s['key'] and C.capture()==s['before'] and N.context()==s['context'],'EDIT_LIVE_DRIFT')
        self.pending=s
        target=s['target']
        if s['plan']['operation']=='append':
            target=obj.shape_key_add(name=s['plan']['target'],from_mix=False);s['created']=target
            require(target.name==s['plan']['target'],'EDIT_NAME_REWRITE');target.relative_key=key.reference_key
            target.value=0.;target.slider_min=0.;target.slider_max=1.;target.mute=False;target.vertex_group=''
            target.select=False;target.lock_shape=False;target.interpolation=key.reference_key.interpolation
        s['live_target']=target
        target.data.foreach_set('co',s['wanted'].reshape(-1));obj.active_shape_key_index=s['active']
        obj.update_tag(refresh={'OBJECT','DATA'});bpy.context.view_layer.update()
        require(co_sha(coords(target.data))==co_sha(s['wanted']),'EDIT_COORDINATE_READBACK')
        actual=C.capture();expected=self._expected(s,co_sha(s['wanted']))
        require(actual==expected,'EDIT_AUTHOR_FORWARD_MISMATCH',C.diff(expected,actual,limit=60))
        require(N.context()==s['context'] and witness(self.profile).tobytes()==s['witness'].tobytes(),'EDIT_CONTEXT_OR_BONE_DRIFT')
        s['after']=actual
        receipt={'schema':'hrig.face-coordinate-receipt.v1','source':self.source,'operation':s['plan']['operation'],
                 'object':obj.name,'mesh':obj.data.name,'key_id':key.name,'reference':key.reference_key.name,
                 'target':target.name,'region_vertices':s['plan']['region_vertices'],
                 'target_before_sha256':s['plan']['target_before_sha256'],'target_sha256':co_sha(s['wanted']),
                 'reference_sha256':s['plan']['reference_sha256'],'raw_mesh_sha256':s['plan']['raw_mesh_sha256'],
                 'outside_region_sha256':co_sha(s['wanted'][s['outside']]),'region_definition':'explicit IDs inside bounded source face box; body additionally positive head mask175',
                 'non_region_stored_f32_exact':True,'full_C_expected_forward_exact':True,
                 'context_and_full_profile_witness_exact':True,'witness_bone_count':len(self.profile['rig']['witness_bones']),
                 'target_definition':next(r for r in author_rows(actual,obj)[1]['character_extra']['blocks'] if r['name']==target.name)}
        s['receipt']=receipt;return deepcopy(receipt)
    def preview_value(self,value):
        self._external('face_keys.preview_value')
        s=self.pending;require(s is not None and self.value_pending is None and 'after' in s,'EDIT_NO_COMMITTED_TARGET')
        require(type(value) in (float,int) and 0<=value<=1 and np.isfinite(value),'EDIT_PREVIEW_VALUE')
        target=s['live_target'];key=s['key'];ad=key.animation_data;path=target.path_from_id('value')
        require(ad is None or (ad.action is None and not ad.nla_tracks and not any(fc.data_path==path for fc in ad.drivers)),'EDIT_PREVIEW_WRITER_CONFLICT')
        require(C.capture()==s['after'] and N.context()==s['context'],'EDIT_PREVIEW_LIVE_DRIFT')
        target.value=float(value);s['obj'].update_tag(refresh={'OBJECT','DATA'});bpy.context.view_layer.update()
        actual=C.capture();expected=self._expected(s,co_sha(s['wanted']),float(np.float32(value)))
        require(actual==expected,'EDIT_PREVIEW_AUTHOR_DRIFT',C.diff(expected,actual,limit=50))
        self.value_pending={'after':actual,'value':float(target.value)}
        return {'status':'value_previewed','value':float(target.value),'full_C_expected_exact':True}
    def restore_value(self):
        self._external('face_keys.restore_value')
        s=self.pending;require(s is not None and self.value_pending is not None,'EDIT_NO_VALUE_PREVIEW')
        require(C.capture()==self.value_pending['after'] and N.context()==s['context'],'EDIT_VALUE_EXTERNAL_DRIFT')
        s['live_target'].value=0.;s['obj'].update_tag(refresh={'OBJECT','DATA'});bpy.context.view_layer.update()
        require(C.capture()==s['after'],'EDIT_VALUE_RESTORE_FAILED');self.value_pending=None
        return {'status':'value_restored','full_C_exact':True}
    def save_copy(self,path,job_directory,retained_writer):
        self._external('face_keys.save_copy')
        s=self.pending;require(s is not None and 'after' in s and self.value_pending is None,'EDIT_SAVE_STATE')
        path=Path(path);require(path.is_absolute() and not path.exists() and path.resolve()!=Path(bpy.data.filepath).resolve(),'EDIT_SAVE_PATH')
        require(C.capture()==s['after'] and N.context()==s['context'],'EDIT_SAVE_LIVE_DRIFT')
        main=bpy.data.filepath;before=C.capture()
        require(callable(retained_writer) and retained_writer.__name__=='save_candidate','EDIT_RETAINED_WRITER_REQUIRED')
        result=retained_writer(str(path),str(job_directory))
        require(result.get('route')=='native_copy_runtime_extra_user_retention' and result.get('runtime_bookkeeping_restored')
                and path.is_file() and bpy.data.filepath==main and C.capture()==before,'EDIT_SAVE_PROTECTION')
        require(file_sha(self.source['file'])==self.source['expected_sha256'],'EDIT_SOURCE_SHA_CHANGED')
        return {'file':str(path),'expected_sha256':file_sha(path),'receipt':deepcopy(s['receipt']),
                'writer':result,'full_C_save_exact':True,'cold_qualified':False}
    def restore(self):
        self._external('face_keys.restore')
        s=self.pending;require(s is not None and 'after' in s and self.value_pending is None,'EDIT_RESTORE_STATE')
        require(C.capture()==s['after'] and N.context()==s['context'],'EDIT_EXTERNAL_DRIFT')
        obj=s['obj'];target=s['live_target']
        if s['plan']['operation']=='append':
            require(s.get('created')==target and target!=s['key'].reference_key,'EDIT_OWNED_NEW_KEY')
            obj.shape_key_remove(target)
        else:target.data.foreach_set('co',s['original'].reshape(-1))
        obj.active_shape_key_index=s['active'];obj.update_tag(refresh={'OBJECT','DATA'});bpy.context.view_layer.update()
        actual=C.capture();require(actual==s['before'] and N.context()==s['context'] and witness(self.profile).tobytes()==s['witness'].tobytes(),
                                   'EDIT_FULL_RESTORE_FAILED',C.diff(s['before'],actual,limit=60))
        self.pending=None;self.snapshot=None
        return {'status':'restored','full_C_context_1733_witness_exact':True}
