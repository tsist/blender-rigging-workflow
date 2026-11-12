# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct volume samples for an existing binding; shared protected local commit."""
import hashlib,json,shutil,subprocess,time,uuid
from pathlib import Path
import bpy
import numpy as np
from . import skinning,skinning_job,skinning_policy as policy
from .volume.contract import normalize
from .volume.geometry import validate_closed

_require=skinning_job._require


def _collect(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings):
    scope=policy.local_scope(target,rig,editable_bone_names,vertex_indices,alpha)
    policy._object(proxy,'MESH')
    _require(proxy!=target and proxy.data!=target.data and not proxy.modifiers and not proxy.data.shape_keys,
             '选择独立无修改器/Key的明确闭合代理，不改原生产拓扑')
    clean=json.loads(json.dumps(settings,allow_nan=False))
    _require(isinstance(clean,dict) and 'partitions' not in clean and not clean.get('return_domain')
             and clean.get('require_all_bones_seeded',True) is True,
             '局部体积使用明确单闭域设置，保持全部可写骨有有效种子')
    scale=scope['scale'];vertices,triangles=skinning.mesh_arrays(proxy)
    points,_=skinning.mesh_arrays(target)
    bones=skinning.bone_segments(rig,scope['editable'])
    for bone in bones:
        for field in ('head','tail'):bone[field]=(np.asarray(bone[field])*scale).tolist()
    vertices=vertices*scale;points=points[scope['ids'][scope['active']]]*scale
    normalize([{'id':'proxy','vertices':vertices,'triangles':triangles}],bones,clean)
    closed=validate_closed(vertices,triangles)
    summary={'schema':'c94.local-volume-plan.v1','source':target.name,'rig':rig.name,'proxy':proxy.name,
        **scope['summary'],'application_points':len(points),'settings':clean,'closed_surface':closed,
        'coordinates':'meters','scene_scale_length':scale,'method':'local_volume_diffusion',
        'scope':'local RAW Basis samples; surrounding proxy geometry never expands write scope'}
    return scope,bones,clean,vertices,triangles,points,summary


def check(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings):
    return _collect(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings)[-1]


class LocalVolumeJob(skinning_job.Job):
    def __init__(self,target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,root):
        scope,bones,settings,vertices,triangles,points,summary=_collect(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings)
        self.source,self.rig,self.proxy=target,rig,proxy;self.session_scene=bpy.context.scene
        self._source_mesh,self._rig_data,self._proxy_mesh=target.data,rig.data,proxy.data
        self._before=policy._state(target,rig);self._proxy_before=skinning_job._proxy_state(proxy)
        self._editable_bone_names=list(editable_bone_names);self.bone_names=list(scope['editable'])
        self._ids=scope['ids'].copy();self._alpha=scope['alpha'].copy();self._active=scope['active'].copy()
        self._preview_alpha=scope['preview_alpha'].copy()
        self.scale=scope['scale'];self.summary=summary
        self._cancelled,self._applied=False,False;self._process=None;self._terminal=None
        self._progress,self._offset,self._pending={'phase':'starting'},0,b''
        self.directory=root/uuid.uuid4().hex;self.directory.mkdir(exist_ok=False)
        runtime=self.directory/'runtime'/'rigging_workflow';(runtime/'volume').mkdir(parents=True)
        (runtime/'__init__.py').write_text('',encoding='utf8')
        package=Path(__file__).resolve().parent;files=[runtime/'__init__.py']
        for path in sorted((package/'volume').glob('*.py')):
            dest=runtime/'volume'/path.name;shutil.copyfile(path,dest);files.append(dest)
        worker=self.directory/'worker.py';shutil.copyfile(package/'skinning_job_worker.py',worker);files.append(worker)
        archive=self.directory/'input.npz';np.savez_compressed(archive,vertices=vertices,triangles=triangles,points=points);files.append(archive)
        manifest={'schema':'c94.local-volume-job.v1','bones':bones,'bone_names':self.bone_names,'settings':settings,
            'source_vertex_count':len(points),'coordinates':'meters','summary':summary,
            'files':[{'relative_path':str(p.relative_to(self.directory)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
        (self.directory/'input.json').write_text(json.dumps(manifest,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf8')
        self._input_hash=hashlib.sha256((self.directory/'input.json').read_bytes()).hexdigest()
        (self.directory/'source-state.json').write_text(json.dumps({'state':self._before,'proxy':self._proxy_before,
            'vertex_indices':self._ids.tolist(),'alpha':self._alpha.tolist()},ensure_ascii=False,indent=2),encoding='utf8')
        self._check_inputs()
        with (self.directory/'worker.log').open('wb') as log:
            self._process=subprocess.Popen([bpy.app.binary_path,'--background','--factory-startup','--disable-autoexec',
                '--offline-mode','--threads','2','--python-exit-code','1','--python',str(worker),'--',str(self.directory)],
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,shell=False,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self._deadline=time.monotonic()+float(settings.get('max_seconds',60))+30

    def _check_inputs(self):
        policy._object(self.source,'MESH');policy._object(self.rig,'ARMATURE');policy._object(self.proxy,'MESH')
        _require(bpy.context.scene==self.session_scene and self.source.data==self._source_mesh
            and self.rig.data==self._rig_data and self.proxy.data==self._proxy_mesh,'局部任务场景或真实数据身份改变')
        _require(policy._state(self.source,self.rig)==self._before and skinning_job._proxy_state(self.proxy)==self._proxy_before,
                 '局部源、权重/锁、Key、骨架、代理、帧或单位已变，请重新求解')

    def preview(self,*,name):
        self._check_inputs()
        return policy.create_scope_preview(self.source,self._preview_alpha,name=name)

    def apply(self,*,name):
        state=self.status();_require(state['status']=='succeeded' and not self._cancelled,'需要完整成功且未取消的局部volume结果')
        self._check_inputs()
        _require(hashlib.sha256((self.directory/'input.json').read_bytes()).hexdigest()==self._input_hash,'局部任务记录被更改')
        path=self.directory/'weights.npz'
        _require(state.get('bone_names')==self.bone_names and hashlib.sha256(path.read_bytes()).hexdigest()==state.get('weights_sha256'),
                 '局部结果骨列或内容身份不匹配')
        with np.load(path,allow_pickle=False) as arrays:weights=arrays['weights'].copy()
        _require(weights.shape==(int(self._active.sum()),len(self.bone_names)) and np.isfinite(weights).all()
            and (weights>=0).all() and np.max(abs(weights.sum(axis=1)-1),initial=0)<=1e-5,'局部volume完整字段校验失败')
        full=np.zeros((len(self._ids),len(self.bone_names)));full[self._active]=weights
        plan=policy.prepare_local_field(self.source,self.rig,full,self.bone_names,self._ids,self._alpha,self._editable_bone_names,
            method='local_volume_diffusion',details={'job_directory':str(self.directory),'weights_sha256':state['weights_sha256'],
                'statistics':state['statistics'],'warnings':state['warnings'],'plan':self.summary})
        candidate=plan.apply(name=name)
        try:
            (self.directory/'apply-receipt.json').write_text(json.dumps({'ok':True,'candidate':candidate.name,
                'source_unchanged':True,'weights_sha256':state['weights_sha256'],'write_vertices':int(self._active.sum())},
                ensure_ascii=False,indent=2),encoding='utf8')
        except Exception:
            mesh=candidate.data;bpy.data.objects.remove(candidate,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
            raise
        self._applied=True
        return candidate


def start(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,*,jobs_directory):
    root=Path(jobs_directory).expanduser();_require(root.is_absolute(),'局部任务目录必须是明确绝对路径')
    root.mkdir(parents=True,exist_ok=True)
    return skinning_job._monitor(LocalVolumeJob(target,rig,proxy,editable_bone_names,vertex_indices,alpha,settings,root.resolve()))
