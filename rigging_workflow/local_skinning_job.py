# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct volume samples for an existing binding; shared protected local commit."""
from copy import deepcopy
import hashlib,json,shutil,subprocess,time,uuid
from pathlib import Path
import bpy
import numpy as np
from . import candidate_state,skeleton_gate,skinning,skinning_job,skinning_policy as policy
from .volume.contract import normalize
from .volume.geometry import validate_closed

_require=skinning_job._require
SELECTED_START_OPERATION = 'local_skinning_job.start_selected'
SELECTED_APPLY_OPERATION = policy.SELECTED_APPLY_OPERATION
SELECTED_SCOPE_SCHEMA = 'c94.local-selected-volume-scope.v1'


def _collect(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,*,
             write_mode=policy.PRESERVE_MASS,context_bone_names=None):
    scope=policy.local_scope(target,rig,editable_bone_names,vertex_indices,alpha,write_mode=write_mode)
    solve_names=scope['editable']
    if write_mode==policy.SELECTED_DIRECT:
        _require(isinstance(context_bone_names,list) and len(context_bone_names)>=2,
                 '选骨重算至少需要两根解算骨；只选一根写入骨时，请另指定只读的邻近解算骨')
        policy._rig(rig,context_bone_names)
        _require(set(solve_names)<=set(context_bone_names),'解算骨列表必须包含所有可写选择骨')
        solve_names=list(context_bone_names)
    else:
        _require(context_bone_names is None,'只在选骨直接写入模式设置解算上下文骨')
    policy._object(proxy,'MESH')
    _require(proxy!=target and proxy.data!=target.data and not proxy.modifiers and not proxy.data.shape_keys,
             '选择独立无修改器/Key的明确闭合代理，不改原生产拓扑')
    clean=json.loads(json.dumps(settings,allow_nan=False))
    _require(isinstance(clean,dict) and 'partitions' not in clean and not clean.get('return_domain')
             and clean.get('require_all_bones_seeded',True) is True,
             '局部体积使用明确单闭域设置，保持全部可写骨有有效种子')
    scale=scope['scale'];vertices,triangles=skinning.mesh_arrays(proxy)
    points,_=skinning.mesh_arrays(target)
    bones=skinning.bone_segments(rig,solve_names)
    for bone in bones:
        for field in ('head','tail'):bone[field]=(np.asarray(bone[field])*scale).tolist()
    vertices=vertices*scale;points=points[scope['ids'][scope['active']]]*scale
    normalize([{'id':'proxy','vertices':vertices,'triangles':triangles}],bones,clean)
    closed=validate_closed(vertices,triangles)
    summary={'schema':'c94.local-volume-plan.v1','source':target.name,'rig':rig.name,'proxy':proxy.name,
        **scope['summary'],'application_points':len(points),'settings':clean,'closed_surface':closed,
        'coordinates':'meters','scene_scale_length':scale,'method':'local_volume_diffusion',
        'scope':'local RAW Basis samples; surrounding proxy geometry never expands write scope'}
    if write_mode==policy.SELECTED_DIRECT:
        from .workflow_entry import _durable_source

        active=scope['active']
        summary['context_bone_names']=solve_names
        summary['selected_write_scope']={
            'schema':SELECTED_SCOPE_SCHEMA,'write_mode':write_mode,'rig':rig.name,
            'source':_durable_source(target),'proxy':_durable_source(proxy),
            'scene':bpy.context.scene.name,'view_layer':bpy.context.view_layer.name,
            'frame':[bpy.context.scene.frame_current,bpy.context.scene.frame_subframe],
            'scene_scale_length':scale,'settings':clean,
            'selected_bone_names':list(editable_bone_names),'editable_bone_names':list(scope['editable']),
            'context_bone_names':solve_names,'vertex_indices':scope['ids'][active].tolist(),
            'alpha':scope['alpha'][active].tolist(),
        }
    return scope,bones,clean,vertices,triangles,points,summary


def check(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,*,
          write_mode=policy.PRESERVE_MASS,context_bone_names=None):
    return _collect(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,
                    write_mode=write_mode,context_bone_names=context_bone_names)[-1]


def selected_apply_scope(scope,*,name):
    """Combine the checked solve scope with the exact output object name."""
    _require(isinstance(scope,dict) and scope.get('schema')==SELECTED_SCOPE_SCHEMA,
             '先取得选骨局部重算的作用域')
    _require(isinstance(name,str) and name.strip(),'需要明确的局部结果对象名称')
    return {**deepcopy(scope),'name':name}


class LocalVolumeJob(skinning_job.Job):
    def __init__(self,target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,root,*,
                 write_mode=policy.PRESERVE_MASS,context_bone_names=None):
        if write_mode==policy.PRESERVE_MASS:
            self._gate_token=skeleton_gate.require_pass(rig,operation='local_skinning_job.LocalVolumeJob')
        collected=_collect(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,
                           write_mode=write_mode,context_bone_names=context_bone_names)
        self._write_mode=write_mode
        self._selected_write_scope=deepcopy(collected[-1].get('selected_write_scope'))
        if self._selected_write_scope is None:
            self._initialize(target,rig,proxy,editable_bone_names,root,collected)
        else:
            self._selected_scope_sha256=candidate_state.scope_digest(self._selected_write_scope)
            with skeleton_gate._scoped_operation(rig,operation=SELECTED_START_OPERATION,
                                                 scope_sha256=self._selected_scope_sha256) as permit:
                self._gate_token=permit
                self._initialize(target,rig,proxy,editable_bone_names,root,collected)

    def _initialize(self,target,rig,proxy,editable_bone_names,root,collected):
        scope,bones,settings,vertices,triangles,points,summary=collected
        self.source,self.rig,self.proxy=target,rig,proxy;self.session_scene=bpy.context.scene
        self._source_mesh,self._rig_data,self._proxy_mesh=target.data,rig.data,proxy.data
        self._before=policy._state(target,rig);self._proxy_before=skinning_job._proxy_state(proxy)
        self._editable_bone_names=list(editable_bone_names);self.bone_names=[bone['name'] for bone in bones]
        self._ids=scope['ids'].copy();self._alpha=scope['alpha'].copy();self._active=scope['active'].copy()
        self._preview_alpha=scope['preview_alpha'].copy()
        self.scale=scope['scale'];self.summary=summary
        self._cancelled,self._applied=False,False;self._process=None;self._terminal=None
        self._progress,self._offset,self._pending={'phase':'starting'},0,b''
        root.mkdir(parents=True,exist_ok=True)
        self.directory=root/uuid.uuid4().hex;self.directory.mkdir(exist_ok=False)
        runtime=self.directory/'runtime'/'rigging_workflow';(runtime/'volume').mkdir(parents=True)
        (runtime/'__init__.py').write_text('',encoding='utf8')
        package=Path(__file__).resolve().parent
        files=[runtime/'__init__.py',*skinning_job._copy_volume_runtime(package,runtime)]
        worker=self.directory/'worker.py';shutil.copyfile(package/'skinning_job_worker.py',worker);files.append(worker)
        gate_copy=runtime/'skeleton_collection.py';shutil.copyfile(package/'skeleton_collection.py',gate_copy);files.append(gate_copy)
        channel_copy=runtime/'worker_channel.py';shutil.copyfile(package/'worker_channel.py',channel_copy);files.append(channel_copy)
        archive=self.directory/'input.npz';np.savez_compressed(archive,vertices=vertices,triangles=triangles,points=points);files.append(archive)
        manifest={'schema':'c94.local-volume-job.v1','bones':bones,'bone_names':self.bone_names,'settings':settings,
            'source_vertex_count':len(points),'coordinates':'meters','summary':summary,
            'files':[{'relative_path':str(p.relative_to(self.directory)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
        manifest['quality_gate']=skeleton_gate.worker_proof(rig,self._gate_token,manifest)
        (self.directory/'input.json').write_text(json.dumps(manifest,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf8')
        self._input_hash=hashlib.sha256((self.directory/'input.json').read_bytes()).hexdigest()
        (self.directory/'source-state.json').write_text(json.dumps({'state':self._before,'proxy':self._proxy_before,
            'vertex_indices':self._ids.tolist(),'alpha':self._alpha.tolist()},ensure_ascii=False,indent=2),encoding='utf8')
        self._check_inputs()
        with (self.directory/'worker.log').open('wb') as log:
            self._process=skeleton_gate.spawn_worker(rig,self._gate_token,manifest,[bpy.app.binary_path,'--background','--factory-startup','--disable-autoexec',
                '--offline-mode','--threads','2','--python-exit-code','1','--python',str(worker),'--',str(self.directory)],
                stdout=log,stderr=subprocess.STDOUT,shell=False,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self._deadline=time.monotonic()+float(settings.get('max_seconds',60))+30

    def _check_inputs(self):
        if self._selected_write_scope is None:
            skeleton_gate.check_pass(self.rig,self._gate_token,operation='local_skinning_job.LocalVolumeJob')
        else:
            permit=skeleton_gate.require_pass(self.rig,operation=SELECTED_START_OPERATION,
                                               write_scope_sha256=self._selected_scope_sha256)
            _require(permit is self._gate_token,'局部求解许可已更换，请重新启动')
        policy._object(self.source,'MESH');policy._object(self.rig,'ARMATURE');policy._object(self.proxy,'MESH')
        _require(bpy.context.scene==self.session_scene and self.source.data==self._source_mesh
            and self.rig.data==self._rig_data and self.proxy.data==self._proxy_mesh,'局部任务场景或真实数据身份改变')
        _require(policy._state(self.source,self.rig)==self._before and skinning_job._proxy_state(self.proxy)==self._proxy_before,
                 '局部源、权重/锁、Key、骨架、代理、帧或单位已变，请重新求解')

    def preview(self,*,name):
        self._check_inputs()
        if self._selected_write_scope is None:
            return policy.create_scope_preview(self.source,self._preview_alpha,name=name,rig=self.rig)
        with skeleton_gate._scoped_operation(self.rig, operation=SELECTED_START_OPERATION,
                                             scope_sha256=self._selected_scope_sha256):
            return policy.create_scope_preview(self.source,self._preview_alpha,name=name,rig=self.rig)

    def apply_scope(self,*,name):
        return selected_apply_scope(self._selected_write_scope,name=name)

    def apply(self,*,name):
        if self._selected_write_scope is None:
            skeleton_gate.check_pass(self.rig,self._gate_token,operation='local_skinning_job.LocalVolumeJob')
            return self._apply(name=name)
        scope=self.apply_scope(name=name)
        with skeleton_gate._scoped_operation(self.rig,operation=SELECTED_APPLY_OPERATION,
                                             scope_sha256=candidate_state.scope_digest(scope)) as permit:
            _require(permit is self._gate_token,'局部采用许可已更换，请重新启动求解')
            return self._apply(name=name)

    def _apply(self,*,name):
        state=self.status();_require(state['status']=='succeeded' and not self._cancelled,'需要完整成功且未取消的局部volume结果')
        self._check_inputs()
        weights=self._read_result_weights(state)
        _require(weights.shape==(int(self._active.sum()),len(self.bone_names)) and np.isfinite(weights).all()
            and (weights>=0).all() and np.max(abs(weights.sum(axis=1)-1),initial=0)<=1e-5,'局部volume完整字段校验失败')
        full=np.zeros((len(self._ids),len(self.bone_names)));full[self._active]=weights
        plan=policy.prepare_local_field(self.source,self.rig,full,self.bone_names,self._ids,self._alpha,self._editable_bone_names,
            method='local_volume_diffusion',write_mode=self._write_mode,
            details={'job_directory':str(self.directory),'weights_sha256':state['weights_sha256'],
                'statistics':state['statistics'],'warnings':state['warnings'],'plan':self.summary})
        candidate=plan.apply(name=name)
        try:
            receipt={'ok':True,'candidate':candidate.name,'source_unchanged':True,
                     'weights_sha256':state['weights_sha256'],'write_vertices':int(self._active.sum())}
            if self._selected_write_scope is not None:
                receipt.update(write_mode=self._write_mode,written_bone_names=list(plan.names),
                               context_bone_names=self.bone_names,
                               write_scope_sha256=candidate_state.scope_digest(self.apply_scope(name=name)))
            (self.directory/'apply-receipt.json').write_text(json.dumps(receipt,
                ensure_ascii=False,indent=2),encoding='utf8')
        except Exception:
            mesh=candidate.data;bpy.data.objects.remove(candidate,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
            raise
        self._applied=True
        return candidate


def start(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,*,jobs_directory,
          write_mode=policy.PRESERVE_MASS,context_bone_names=None):
    if write_mode==policy.PRESERVE_MASS:
        skeleton_gate.require_pass(rig,operation='local_skinning_job.start')
    root=Path(jobs_directory).expanduser();_require(root.is_absolute(),'局部任务目录必须是明确绝对路径')
    return skinning_job._monitor(LocalVolumeJob(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,
        root.resolve(),write_mode=write_mode,context_bone_names=context_bone_names))
