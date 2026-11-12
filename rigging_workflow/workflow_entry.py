"""Live unbound/default-existing character entries using the shared skin solvers.

Existing inputs are RAW/Basis snapshots for a fresh C94 binding, never an
implicit cross-rig animation migration. Original assets remain authoritative.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import math
from pathlib import Path
import uuid

import bpy
import numpy as np
from mathutils import Matrix

from . import corrective_authoring as sig, generation, module_configuration
from . import positioning, skinning, skinning_job, skinning_plan, skinning_policy

SCHEMA = 'c94.workflow-entry.v1'
KEY = 'c94_workflow_entry_v1'


class EntryError(ValueError):
    def __init__(self, code, message, remediation=None):
        super().__init__(message)
        self.code, self.remediation = code, remediation or {}


def _need(value, code, message, remediation=None):
    if not value:raise EntryError(code, message, remediation)


def _hash(value):return sig._hash(value)


def _points(block):
    value=np.empty((len(block.data),3),dtype=np.float32)
    block.data.foreach_get('co',value.ravel())
    return value


def _animation(owner):
    ad=getattr(owner,'animation_data',None)
    if ad is None:return None
    action=ad.action;curves=[]
    if action:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for curve in bag.fcurves:
                        values=np.empty(len(curve.keyframe_points)*2,dtype=np.float32)
                        curve.keyframe_points.foreach_get('co',values)
                        curves.append([bag.slot_handle,curve.data_path,curve.array_index,
                                       sig._array_hash(values),[p.interpolation for p in curve.keyframe_points]])
    return {'action':action.name if action else None,
            'action_id':action.as_pointer() if action else None,
            'slot_handle':ad.action_slot_handle if action else None,
            'curve_count':len(curves),'curve_sha256':_hash(curves),
            'drivers':sig._drivers(owner),
            'nla':[{'name':track.name,'mute':track.mute,
                    'strips':[{'name':s.name,'action':s.action.name if s.action else None,
                               'start':s.frame_start,'end':s.frame_end} for s in track.strips]}
                   for track in ad.nla_tracks]}


def _keys(mesh):
    keys=mesh.shape_keys
    if keys is None:return None
    return {'relative':keys.use_relative,'eval_time':keys.eval_time,
            'reference':keys.reference_key.name,
            'animation':_animation(keys),
            'blocks':[{'name':k.name,'co':sig._array_hash(_points(k)),
                       'relative':k.relative_key.name,'mask':k.vertex_group,
                       'value':k.value,'mute':k.mute,'slider_min':k.slider_min,
                       'slider_max':k.slider_max,'interpolation':k.interpolation,'frame':k.frame}
                      for k in keys.key_blocks]}


def _source_state(obj):
    return {'object_id':obj.as_pointer(),'mesh_id':obj.data.as_pointer(),
            'geometry':sig._mesh_signature(obj.data),'weights':sig._weights(obj),
            'locks':[g.lock_weight for g in obj.vertex_groups],
            'world':sig._plain(obj.matrix_world),'parent':sig._plain(obj.parent),
            'parent_type':obj.parent_type,'parent_bone':obj.parent_bone,
            'parent_inverse':sig._plain(obj.matrix_parent_inverse),
            'modifiers':[sig._rna(m) for m in obj.modifiers],
            'constraints':[sig._rna(c) for c in obj.constraints],
            'animation':_animation(obj),'mesh_animation':_animation(obj.data),
            'keys':_keys(obj.data),'props':sig._plain(dict(obj.items())),
            'mesh_props':sig._plain(dict(obj.data.items()))}


def _contains_id(value):
    if isinstance(value,bpy.types.ID):return True
    if hasattr(value,'to_dict'):value=value.to_dict()
    if isinstance(value,dict):return any(_contains_id(v) for v in value.values())
    if isinstance(value,(list,tuple)):return any(_contains_id(v) for v in value)
    return False


def _strip_author_properties(owner):
    for key in list(owner.keys()):
        if str(key).startswith('c94_') or _contains_id(owner[key]):del owner[key]


def _clear_groups(obj):
    # Old deform indices can outnumber the new group's list. Cover every native
    # index before clearing, then verify instead of leaving orphan dverts.
    maximum=max((g.group for v in obj.data.vertices for g in v.groups),default=-1)
    while len(obj.vertex_groups)<=maximum:obj.vertex_groups.new(name='__ENTRY_CLEAR_'+uuid.uuid4().hex)
    obj.vertex_groups.clear()
    _need(not any(v.groups for v in obj.data.vertices),'ORPHAN_GROUPS','私有求解Mesh仍残留旧变形列')


def _isolated_keys(mesh):
    keys=mesh.shape_keys
    if keys:
        keys.animation_data_clear()
        for block in keys.key_blocks:block.value=0.
        keys.eval_time=keys.reference_key.frame


def _restore_selection(active, selected):
    bpy.context.view_layer.update()
    live={obj.as_pointer():obj for obj in bpy.context.view_layer.objects if obj is not None}
    for obj in list(bpy.context.selected_objects):obj.select_set(False)
    for pointer in selected:
        if pointer in live:live[pointer].select_set(True)
    bpy.context.view_layer.objects.active=live.get(active)


def _name(value):
    _need(isinstance(value,str) and value.strip() and not any(ord(c)<32 for c in value),
          'NAME_REQUIRED','需要明确且无控制字符的新名称')
    _need(len(value.encode('utf8'))<=63,'NAME_TOO_LONG','名称超过Blender原生字节上限')
    return value


def _identity(owner):
    return (owner.as_pointer(),getattr(owner,'session_uid',None))


class EntryJob:
    """The live session owns a real existing volume Job, not a disk token."""
    def __init__(self,entry,index,native,kind):
        self.entry,self.index,self.native,self.kind=entry,index,native,kind
        self._applied=False;self._failure=None

    def status(self):
        state=self.native.status()
        if self._failure:state={**state,'status':'failed','error':self._failure}
        if self._applied:state={**state,'status':'applied'}
        return {**state,'index':self.index,'source':self.entry._source_names[self.index],
                'can_apply':state['status']=='succeeded' and not state.get('cancel_requested',False)
                            and not self._applied and self.entry.state=='captured'}

    def cancel(self):
        self.native.cancel()
        return self.status()

    def apply(self,*,name):
        self.entry._check()
        _need(self.status()['can_apply'],'RESULT_NOT_READY','需要本会话成功且未取消的真实求解结果')
        name=_name(name)
        try:
            with self.entry._step() as owned:
                candidate=self.native.apply(name=name)
                module_configuration._own(owned,candidate,candidate.data)
                self.entry._restore_data(self.index,candidate,self.kind,owned_ids=owned)
                self.entry._check()
                self.entry._results[self.index]=candidate
                self.entry._output_state[self.index]=_source_state(candidate)
            self._applied=True
            return candidate
        except Exception as error:
            self._failure={'code':getattr(error,'code','APPLY_FAILED'),'message':str(error)}
            raise


class PreparedEntry:
    def __init__(self,scene,reference,template_file,configuration,objects,*,label,mode,
                 legacy_policy,chain_record,neutral_world):
        _need(scene==bpy.context.scene and bpy.context.mode=='OBJECT','OBJECT_MODE_REQUIRED','入口需要当前Scene对象模式')
        _need(isinstance(objects,(list,tuple)) and objects and len(set(objects))==len(objects),
              'EXPLICIT_MESHES','逐项指定当前角色明确且无重复的Mesh，不扫描历史QA副本')
        _need(mode in {'AUTO','UNBOUND','EXISTING'},'ENTRY_MODE','入口模式须AUTO/UNBOUND/EXISTING')
        _need(legacy_policy=='SNAPSHOT_FRESH_REBIND','LEGACY_POLICY','此入口只做明确RAW快照重绑；旧架构局修另走维护')
        self.scene,self.reference=scene,reference;self._scene_identity=_identity(scene);self.template_file=str(Path(template_file).resolve())
        self.configuration=module_configuration.validate(scene,reference,template_file,configuration)
        self.chain_record=copy.deepcopy(chain_record)
        if chain_record is not None:
            from . import accessory_chains
            accessory_chains.validate_reference(scene,reference,chain_record)
        self.label=_name(label.strip() if isinstance(label,str) else label)
        self._sources=list(objects);self._source_names=[o.name for o in objects]
        self._state='prepared';self._owned={};self._jobs={};self._results={};self._output_state={}
        self._preserved=[];self._staging=[];self._surfaces=[];self.rig=None;self.collection=None
        self._frame=(scene.frame_current,scene.frame_subframe);self._scale=scene.unit_settings.scale_length
        self._reference_signature=positioning.reference_signature(reference)
        self._selected=[o.as_pointer() for o in bpy.context.selected_objects]
        self._active=bpy.context.view_layer.objects.active.as_pointer() if bpy.context.view_layer.objects.active else None
        self._world=[];self._legacy=[];self._before=[];old_rigs={}
        overrides=neutral_world or {}
        _need(isinstance(overrides,dict) and set(overrides)<=set(o.name for o in objects),
              'NEUTRAL_WORLD','neutral_world须只映射本次明确对象名到已批准中立4×4')
        has_binding=False
        for index,obj in enumerate(objects):
            _need(isinstance(obj,bpy.types.Object) and obj.type=='MESH' and scene.objects.get(obj.name)==obj
                  and bpy.context.view_layer.objects.get(obj.name)==obj and obj.mode=='OBJECT'
                  and not obj.library and not obj.override_library and not obj.data.library and not obj.data.override_library,
                  'LOCAL_SOURCE','需要明确本地可读RAW Mesh：'+getattr(obj,'name','?'))
            arms=[m.object for m in obj.modifiers if m.type=='ARMATURE' and m.object]
            if obj.parent and obj.parent.type=='ARMATURE':arms.append(obj.parent)
            for rig in arms:old_rigs[rig.as_pointer()]=rig
            has_binding|=bool(arms)
            animated=obj.animation_data and (obj.animation_data.action or obj.animation_data.drivers or obj.animation_data.nla_tracks)
            ambiguous=bool(obj.constraints or animated or (obj.parent and obj.parent_type!='OBJECT'))
            _need(not ambiguous or obj.name in overrides,'NEUTRAL_WORLD_REQUIRED',
                  '旧骨父/对象动作或约束不提供中立RAW世界框架，请显式给neutral_world后重做prepare',
                  {'call':'prepare(..., neutral_world={source_name: approved_4x4})','source':obj.name,
                   'policy':'RAW/Basis only; never bake old pose evaluated as a new rest'})
            world=Matrix(overrides[obj.name]) if obj.name in overrides else obj.matrix_world.copy()
            array=np.asarray(world,dtype=float)
            _need(array.shape==(4,4) and np.isfinite(array).all() and abs(np.linalg.det(array[:3,:3]))>1e-12,
                  'NEUTRAL_WORLD','中立world须有限可逆4×4')
            self._world.append(world);self._before.append(_source_state(obj))
            legacy=dict(source=obj.name,object_animation=_animation(obj),mesh_animation=_animation(obj.data),keys=_keys(obj.data),
                        modifiers=[sig._rna(m) for m in obj.modifiers],constraints=[sig._rna(c) for c in obj.constraints],
                        parent=sig._plain(obj.parent),parent_type=obj.parent_type,parent_bone=obj.parent_bone,
                        props=sig._plain(dict(obj.items())),mesh_props=sig._plain(dict(obj.data.items())),
                        neutral_world=sig._plain(world),world_override=obj.name in overrides,
                        old_groups=[g.name for g in obj.vertex_groups],old_group_locks=[g.lock_weight for g in obj.vertex_groups],
                        old_weights=sig._weights(obj)[1],
                        redo=['Old bone animation/constraints/drivers remain only on original rig; new C94 animation must be authored',
                              'Copied Key inputs are neutralized and isolated; original Key drivers/actions require explicit new authoring,not migrated'])
            self._legacy.append(legacy)
        _need(mode!='UNBOUND' or not has_binding,'BOUND_SOURCE_IN_UNBOUND','无绑定入口中发现旧骨架；请使用EXISTING默认副本重建')
        self.mode='EXISTING' if mode=='AUTO' and has_binding else ('UNBOUND' if mode=='AUTO' else mode)
        self._old_rigs=list(old_rigs.values())
        self._rig_before=[(sig._rig_state(r)['contract'],_animation(r),_animation(r.data)) for r in self._old_rigs]
        self._report={'schema':SCHEMA,'mode':self.mode,'label':self.label,'legacy_policy':legacy_policy,
                      'reference':reference.name,'reference_signature':self._reference_signature,
                      'configuration_id':self.configuration['id'],'frame':list(self._frame),
                      'sources':[o.name for o in self._sources],
                      'legacy_inputs':[{k:v for k,v in row.items() if k!='old_weights'} for row in self._legacy],
                      'old_rigs':[{'rig':r.name,'animation':_animation(r),'data_animation':_animation(r.data)} for r in self._old_rigs],
                      'bindings':[], 'scope':'RAW/Basis freshC94 binding; original assets retained; no cross-rig animation or P1 author migration'}

    @property
    def state(self):return self._state

    @property
    def report(self):
        value=copy.deepcopy(self._report)
        value.update(state=self._state,submitted=self._state=='finished',
                     submission_authority='live PreparedEntry only; saved JSON never restores job apply authority')
        return value

    @property
    def pending(self):
        if self._state=='aborted':return []
        result=[]
        for index,name in enumerate(self._source_names):
            state=self._jobs[index].status() if index in self._jobs else {'status':'applied' if index in self._results else 'unstarted'}
            result.append({'index':index,'source':name,'status':state['status'],
                           'job_directory':state.get('job_directory'),'detail':state})
        return result

    def _same_scene(self):
        try:return _identity(self.scene)==self._scene_identity and bpy.context.scene==self.scene
        except ReferenceError:return False

    def _check(self):
        _need(self._state in {'prepared','captured'} and self._same_scene() and bpy.context.mode=='OBJECT',
              'SESSION_CHANGED','会话已结束或Scene/模式改变；磁盘记录不能恢复提交权')
        _need((self.scene.frame_current,self.scene.frame_subframe)==self._frame and self.scene.unit_settings.scale_length==self._scale,
              'SESSION_CHANGED','帧或单位改变，请取消本会话后重新prepare')
        _need(positioning.reference_signature(self.reference)==self._reference_signature,
              'REFERENCE_CHANGED','已批准完整参考已改变')
        module_configuration.validate(self.scene,self.reference,self.template_file,self.configuration)
        _need(all(_source_state(o)==s for o,s in zip(self._sources,self._before)),
              'SOURCE_CHANGED','原RAW几何/Key/权重/动画/对象身份改变，请重新prepare')
        _need(all((sig._rig_state(r)['contract'],_animation(r),_animation(r.data))==s for r,s in zip(self._old_rigs,self._rig_before)),
              'SOURCE_CHANGED','原骨架/动作改变；不复用过时输入')
        if self._state=='captured':
            _need(sig._rig_state(self.rig)['contract']==self._new_rig_contract,'NEW_RIG_CHANGED','新rig已改，请重做受影响求解')

    @contextmanager
    def _step(self):
        before=module_configuration._ids();active=bpy.context.view_layer.objects.active
        selected_objects=list(bpy.context.selected_objects)
        active_pointer=active.as_pointer() if active else None;selected=[o.as_pointer() for o in selected_objects]
        report_before=copy.deepcopy(self._report);owned=[]
        results_before=dict(self._results);output_before=dict(self._output_state)
        try:
            yield owned
        except Exception:
            self._report=report_before;self._results=results_before;self._output_state=output_before
            if self._same_scene():module_configuration._cleanup(before,active,selected_objects,owned_ids=owned)
            raise
        finally:
            if self._same_scene():
                live=module_configuration._ids()
                for item in owned:
                    try:
                        if item in live and item not in before:self._owned[item.as_pointer()]=(item,getattr(item,'session_uid',None))
                    except ReferenceError:pass
                _restore_selection(active_pointer,selected)

    def capture(self):
        self._check();_need(self._state=='prepared','ALREADY_CAPTURED','会话只能捕获一次')
        try:
            with self._step() as owned:
                from . import character_generation
                generated=character_generation.generate(self.scene,self.reference,self.template_file,self.label,
                    configuration=self.configuration,chain_record=self.chain_record,owned_ids=owned)
                self.rig,self.collection=generated['rig'],generated['collection']
                for index,source in enumerate(self._sources):
                    name=_name(f'{self.label}__输入_{index+1:02d}')
                    _need(name not in bpy.data.objects,'NAME_COLLISION','捕获输入名称已存在')
                    preserved=module_configuration._own(owned,source.data.copy())
                    _need(not source.data.shape_keys or preserved.shape_keys!=source.data.shape_keys,
                          'KEY_COPY_SHARED','原生Mesh.copy没有独立Key；拒绝修改原Key')
                    preserved.animation_data_clear();_strip_author_properties(preserved);_isolated_keys(preserved)
                    if preserved.shape_keys:
                        preserved.vertices.foreach_set('co',_points(preserved.shape_keys.reference_key).ravel())
                    self._preserved.append(preserved)
                    obj=module_configuration._own(owned,source.copy());obj.data=module_configuration._own(owned,preserved.copy());obj.name=name
                    self.collection.objects.link(obj)
                    obj.animation_data_clear()
                    if obj.data.shape_keys:obj.shape_key_clear()
                    obj.data.animation_data_clear();_clear_groups(obj)
                    for constraint in list(obj.constraints):obj.constraints.remove(constraint)
                    for modifier in list(obj.modifiers):
                        if modifier.type!='SUBSURF':obj.modifiers.remove(modifier)
                    obj.parent=None;obj.parent_type='OBJECT';obj.parent_bone='';obj.matrix_parent_inverse=Matrix.Identity(4)
                    obj.delta_location=(0,0,0);obj.delta_scale=(1,1,1);obj.delta_rotation_euler=(0,0,0);obj.delta_rotation_quaternion=(1,0,0,0)
                    obj.matrix_world=self._world[index];_strip_author_properties(obj)
                    obj[KEY]=json.dumps({'schema':SCHEMA,'source':source.name,'role':'neutral_RAW_solver_source',
                                         'status':'unsubmitted','submission_authority':'live session only; no cold apply token'},ensure_ascii=False)
                    obj.hide_set(False);self._staging.append(obj)
                    _need(not obj.data.shape_keys and not obj.vertex_groups and not obj.animation_data
                          and not obj.constraints and not obj.parent and all(m.type=='SUBSURF' for m in obj.modifiers),
                          'CAPTURE_NOT_ISOLATED','RAW求解源仍含旧绑定/作者求值')
                self._report['generation']=generated['report']
                self._report['rig']=self.rig.name;self._report['captured_sources']=[o.name for o in self._staging]
                self._new_rig_contract=sig._rig_state(self.rig)['contract']
                self._state='captured';self._check()
            return {'rig':self.rig,'collection':self.collection,'sources':list(self._staging),'report':self.report}
        except Exception:
            self._preserved=[];self._staging=[];self.rig=None;self.collection=None
            self._state='prepared' if self._same_scene() else 'aborting'
            raise

    def _index(self,index):
        self._check();_need(self._state=='captured' and type(index)is int and 0<=index<len(self._sources),
                            'SOURCE_INDEX','选择当前live capture中的明确输入index')
        _need(index not in self._results,'ALREADY_BOUND','此输入已应用结果；不要覆盖既有候选')
        if index in self._jobs:
            _need(self._jobs[index].status()['status'] in {'failed','cancelled'},'JOB_ACTIVE','该输入已有运行/待应用任务')
        return self._staging[index]

    def start_volume(self,index,proxy,bone_names,settings,*,jobs_directory):
        source=self._index(index)
        with self._step():native=skinning_job.start(source,self.rig,proxy,bone_names,settings,jobs_directory=jobs_directory)
        job=EntryJob(self,index,native,'volume_diffusion');self._jobs[index]=job;return job

    def prepare_surface(self,index,*,name):
        source=self._index(index);name=_name(name)
        with self._step() as owned:
            surface=skinning_plan.prepare_surface(source,name=name)
            module_configuration._own(owned,surface,surface.data)
        self._surfaces.append(surface)
        return surface

    def start_plan(self,index,surface,domains,rigid,*,jobs_directory,transfer_distance_meters=.01):
        source=self._index(index)
        with self._step():native=skinning_plan.start(source,self.rig,surface,domains,rigid,jobs_directory=jobs_directory,transfer_distance_meters=transfer_distance_meters)
        job=EntryJob(self,index,native,'explicit_multi_domain_volume');self._jobs[index]=job;return job

    def _restore_data(self,index,candidate,method,*,owned_ids=None):
        staging=self._staging[index];before=skinning_job._points(staging)
        new_names,new_rows=sig._weights(candidate);new_locks=[g.lock_weight for g in candidate.vertex_groups]
        old_mesh=candidate.data;mesh=module_configuration._own(owned_ids,self._preserved[index].copy());candidate.data=mesh
        _clear_groups(candidate)
        for name,locked in zip(new_names,new_locks):
            group=candidate.vertex_groups.new(name=name);group.lock_weight=locked
        for vertex,row in enumerate(new_rows):
            for group,value in row:candidate.vertex_groups[group].add([vertex],value,'REPLACE')
        legacy=self._legacy[index];aliases={};legacy_groups=[]
        for group,name in enumerate(legacy['old_groups']):
            alias=_name(f'LEGACY_{index:02d}_{group:04d}_{hashlib.sha256(name.encode()).hexdigest()[:8]}')
            _need(alias not in candidate.vertex_groups and alias not in self.rig.data.bones,
                  'LEGACY_GROUP_COLLISION','旧参考列命名空间碰撞')
            target=candidate.vertex_groups.new(name=alias);legacy_groups.append(target);aliases[name]=alias
        for vertex,row in enumerate(legacy['old_weights']):
            for old,value in row:
                _need(0<=old<len(legacy_groups),'LEGACY_GROUP_INVALID','原旧权重索引无组名，需先在独立RAW副本显式整理')
                legacy_groups[old].add([vertex],value,'REPLACE')
        for group,locked in zip(legacy_groups,legacy['old_group_locks']):group.lock_weight=locked
        keys=mesh.shape_keys
        if keys:
            _isolated_keys(mesh)
            for block in keys.key_blocks:
                if block.vertex_group:
                    _need(block.vertex_group in aliases,'KEY_MASK_MISSING','原Key mask没有真实旧权重列，需显式重新作者')
                    block.vertex_group=aliases[block.vertex_group]
            expected=copy.deepcopy(legacy['keys'])
            for block in expected['blocks']:
                block['value']=0.;block['mask']=aliases.get(block['mask'],block['mask'])
            expected['eval_time']=keys.reference_key.frame;expected['animation']=None
            _need(_keys(mesh)==expected,'KEY_DATA_CHANGED','Key坐标/relative/absolute关系或mask未按声明保留')
        if old_mesh.users==0:bpy.data.meshes.remove(old_mesh)
        bpy.context.view_layer.update();after=skinning_job._points(candidate)
        _need(after.shape==before.shape and np.isfinite(after).all(),'NEUTRAL_SURFACE_CHANGED','保Key后的中立求值拓扑/数值改变')
        error=float(np.linalg.norm(after-before,axis=1).max(initial=0))*self._scale
        _need(error<=1e-5,'NEUTRAL_SURFACE_CHANGED','保Key后中立世界表面跳变')
        bound=json.loads(candidate.get('c94_skinning_v1','{}'))
        bound.update(source=self._sources[index].name,rig=self.rig.name,method=method,
                     workflow_entry={'schema':SCHEMA,'configuration_id':self.configuration['id'],
                                     'legacy_policy':'SNAPSHOT_FRESH_REBIND','legacy_group_aliases':aliases,
                                     'old_input_archive':{k:v for k,v in legacy.items() if k!='old_weights'},
                                     'native_Key_inputs':'neutral on new candidate; old controls/drivers are not migrated'})
        candidate['c94_skinning_v1']=json.dumps(bound,ensure_ascii=False,allow_nan=False)
        candidate[KEY]=json.dumps({'schema':SCHEMA,'source':self._sources[index].name,'rig':self.rig.name,
                                  'configuration_id':self.configuration['id'],'status':'bound_candidate',
                                  'submission_authority':'live session only; no cold apply token'},ensure_ascii=False)
        _need(not candidate.parent and not candidate.constraints and not candidate.animation_data
              and not mesh.animation_data and (not keys or not keys.animation_data)
              and len([m for m in candidate.modifiers if m.type=='ARMATURE'])==1
              and candidate.modifiers[0].type=='ARMATURE' and candidate.modifiers[0].object==self.rig,
              'BINDING_NOT_ISOLATED','新候选仍含旧骨架/动作/驱动')
        self._report['bindings'].append({'index':index,'source':self._sources[index].name,'object':candidate.name,
            'method':method,'key_count':len(keys.key_blocks) if keys else 0,'legacy_group_aliases':aliases,
            'neutral_surface_error_meters':error,'old_animation':'retained on source; explicit new authoring required'})

    def bind_rigid(self,index,assignments,*,name):
        source=self._index(index);name=_name(name)
        with self._step() as owned:
            candidate=skinning_policy.prepare_rigid(source,self.rig,assignments).apply(name=name)
            module_configuration._own(owned,candidate,candidate.data)
            self._restore_data(index,candidate,'explicit_rigid',owned_ids=owned);self._check()
            self._results[index]=candidate;self._output_state[index]=_source_state(candidate)
        return candidate

    def bind_transfer(self,index,donor,*,max_distance_meters,name,bone_names=None,donor_vertex_indices=None):
        source=self._index(index);name=_name(name)
        _need(donor in self._results.values(),'DONOR_NOT_APPLIED','先应用本角色真实身体候选，再读取其现用权重场')
        with self._step() as owned:
            # The donor retains editable Keys, so provide a neutral keyless
            # field view with the exact already-applied RAW groups to the
            # existing transfer policy. It is temporary and never the output.
            field=module_configuration._own(owned,donor.copy());field.data=module_configuration._own(owned,donor.data.copy());self.collection.objects.link(field)
            if field.data.shape_keys:field.shape_key_clear()
            field_mesh=field.data
            try:
                candidate=skinning_policy.prepare_transfer(source,self.rig,field,max_distance_meters=max_distance_meters,
                    bone_names=bone_names,donor_vertex_indices=donor_vertex_indices).apply(name=name)
                module_configuration._own(owned,candidate,candidate.data)
            finally:
                bpy.data.objects.remove(field,do_unlink=True)
                if field_mesh.users==0:bpy.data.meshes.remove(field_mesh)
            self._restore_data(index,candidate,'applied_raw_surface_transfer',owned_ids=owned);self._check()
            self._results[index]=candidate;self._output_state[index]=_source_state(candidate)
        return candidate

    def _owned_live(self):
        live={o.as_pointer():o for o in module_configuration._ids()};owned={}
        for pointer,(original,uid) in self._owned.items():
            try:
                if pointer in live and original.as_pointer()==pointer and live[pointer]==original and getattr(live[pointer],'session_uid',None)==uid:
                    owned[pointer]=live[pointer]
            except ReferenceError:pass
        return owned

    def _remove_owned(self,pointers,*,retain_shared=False):
        owned=self._owned_live();remove={owned[p] for p in pointers if p in owned}
        if not remove:return []
        allowed={o for o in owned.values() if isinstance(o,bpy.types.Collection)}
        if self._same_scene():allowed.update((self.scene,self.scene.collection))
        users=bpy.data.user_map(subset=remove)
        foreign=[(block.name,user.name) for block,owners in users.items() for user in owners if user not in remove and user not in allowed]
        retained=[]
        if retain_shared:
            # A caller-owned plan surface legitimately references its captured
            # input Object. Keep that input and its transitive ID dependencies;
            # never take ownership of or rewrite the caller's diagnostic data.
            while True:
                shared={block:[user for user in owners if user not in remove and user not in allowed]
                        for block,owners in users.items() if block in remove}
                shared={block:owners for block,owners in shared.items() if owners}
                if not shared:break
                for block,owners in shared.items():
                    retained.append({'name':block.name,'type':block.bl_rna.identifier,
                                     'users':[user.name for user in owners]})
                remove.difference_update(shared)
        else:
            _need(not foreign,'OWNED_DATA_SHARED','本会话数据被外部对象引用；先断开该明确引用再finish/abort，避免删用户新工作',
                  {'foreign_users':foreign,'call':'finish/abort again after detaching the listed references'})
        if bpy.context.object in remove and bpy.context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        bpy.data.batch_remove(ids=tuple(remove))
        bpy.context.view_layer.update()
        return retained

    def finish(self):
        self._check();_need(self._state=='captured' and len(self._results)==len(self._sources),
                            'INCOMPLETE_BINDING','所有明确输入完成真实绑定后才能finish，不发布半角色')
        _need(all(_source_state(self._results[i])==self._output_state[i] for i in self._results),
              'CANDIDATE_CHANGED','提交前候选被编辑；请显式复核并重做该绑定')
        remove=[]
        for obj in self._staging:
            remove.extend([obj.as_pointer(),obj.data.as_pointer()])
        for obj in self._surfaces:
            remove.extend([obj.as_pointer(),obj.data.as_pointer()])
        for mesh in self._preserved:
            remove.append(mesh.as_pointer())
            if mesh.shape_keys:remove.append(mesh.shape_keys.as_pointer())
        retained=self._remove_owned(remove,retain_shared=True)
        self._report['retained_input_dependencies']=retained
        live=self._owned_live()
        for obj in self._staging:
            try:
                if obj.as_pointer() in live and KEY in obj:
                    record=json.loads(obj[KEY]);record.update(status='retained_solver_input',
                        submission_authority='none; retained only because caller-owned diagnostic data refers to this input')
                    obj[KEY]=json.dumps(record,ensure_ascii=False)
            except ReferenceError:pass
        metadata=json.loads(self.rig[generation.RIG_KEY]);metadata['skinning']='workflow_entry_bound'
        self.rig[generation.RIG_KEY]=json.dumps(metadata,ensure_ascii=False,allow_nan=False)
        self._report.update(status='complete',source_unchanged=True,objects=[self._results[i].name for i in range(len(self._sources))])
        for candidate in self._results.values():
            record=json.loads(candidate[KEY]);record['status']='complete';candidate[KEY]=json.dumps(record,ensure_ascii=False)
        self._state='finished';_restore_selection(self._active,self._selected)
        return {'rig':self.rig,'collection':self.collection,'objects':[self._results[i] for i in range(len(self._sources))],'report':self.report}

    def abort(self):
        if self._state=='aborted':return {'state':'aborted','cleanup_complete':True,'pending':[]}
        _need(self._state!='finished','FINISHED_ENTRY','已finish的交付不由旧live会话撤销')
        self._state='aborting'
        for job in self._jobs.values():
            state=job.status()
            if state['status']=='running':job.cancel()
        pending=self.pending
        if any(r['status']=='running' for r in pending):return {'state':'aborting','cleanup_complete':False,'pending':pending}
        self._remove_owned(set(self._owned));self._state='aborted'
        if self._same_scene():_restore_selection(self._active,self._selected)
        return {'state':'aborted','cleanup_complete':True,'pending':[]}

    def release_for_load(self):
        """Cancel now; caller/watchdog may reap after original RNA has gone."""
        return self.abort()


def prepare(scene,reference,template_file,configuration,objects,*,label,mode='AUTO',
            legacy_policy='SNAPSHOT_FRESH_REBIND',chain_record=None,neutral_world=None):
    """Read-only capture plan; all submit authority remains in the returned live object."""
    return PreparedEntry(scene,reference,template_file,configuration,objects,label=label,mode=mode,
                         legacy_policy=legacy_policy,chain_record=chain_record,neutral_world=neutral_world)
