# SPDX-License-Identifier: GPL-3.0-or-later
"""Raw-mesh binding with explicit proxies and the existing background solver."""
import hashlib
import json
from pathlib import Path
import uuid
import bpy
import numpy as np
from . import skeleton_gate, skinning, skinning_job, helper_weights, rig_identity


def _source(source):
    if (not isinstance(source, bpy.types.Object) or bpy.context.mode != 'OBJECT'
            or source.name not in bpy.context.view_layer.objects or source.type != 'MESH'
            or source.library or source.data.library or source.data.shape_keys
            or source.vertex_groups or source.parent or source.constraints or source.animation_data
            or source.data.animation_data or any(m.type != 'SUBSURF' for m in source.modifiers)
            or any(v.groups for v in source.data.vertices)):
        raise ValueError('请选择无绑定、无形态键/动画的原始中立Mesh；保留已有角色并建立明确的新输入')


def _body_sources(source, body_sources):
    sources=[source] if body_sources is None else list(body_sources)
    if (not 1 <= len(sources) <= 64 or not all(isinstance(o,bpy.types.Object) for o in sources)
            or len({o.as_pointer() for o in sources}) != len(sources) or source not in sources):
        raise ValueError('身体来源需为1–64个不同Mesh，并包含当前要绑定的原始部件')
    for obj in sources: _source(obj)
    return sources


def _field_samples(proxy, rig, names, settings):
    """Select a supported proxy transfer surface without accepting missing production rows."""
    from .volume.contract import normalize, Budget, VolumeError
    from .volume.geometry import Domain
    from .volume.solver import _sample
    vertices,triangles=skinning.mesh_arrays(proxy)
    scale=float(bpy.context.scene.unit_settings.scale_length)
    vertices=vertices*scale
    bones=skinning.bone_segments(rig,names)
    for bone in bones:
        for field in ('head','tail'):bone[field]=(np.asarray(bone[field])*scale).tolist()
    _,_,cfg,_=normalize([{'id':'proxy','vertices':vertices,'triangles':triangles}],bones,settings)
    h=cfg['cell_size'] or float(np.ptp(vertices,axis=0).max())/cfg['cells_longest']
    domain=Domain(vertices,triangles,h,cfg,Budget(cfg));domain.occupy();domain.graph()
    supported=np.ones(len(vertices),dtype=bool)
    constant=np.ones((len(domain.centers),1),dtype=float)
    # Each block is bounded by the solver's 32-example diagnostic contract, so
    # every unsupported index is enumerated rather than silently truncated.
    for offset in range(0,len(vertices),32):
        try:_sample(domain,vertices[offset:offset+32],constant)
        except VolumeError as exc:
            if exc.code!='UNCOVERED_VERTICES':raise
            missing=domain.stats.get('sample_uncovered_examples',[])+domain.stats.get('sample_ambiguous_examples',[])
            if len(missing)!=domain.stats.get('sample_uncovered_vertices',0)+domain.stats.get('sample_ambiguous_vertices',0):
                raise ValueError('代理表面支持报告不完整，拒绝建立转移场')
            supported[offset+np.asarray(missing,dtype=int)]=False
    indices=np.flatnonzero(supported)
    faces=triangles[supported[triangles].all(axis=1)]
    if not len(faces):raise ValueError('代理没有可转移的完整体素支持表面，请明确处理薄片或修正内部代理')
    indices.setflags(write=False)
    return indices,{'method':'explicit_supported_proxy_surface; full_volume_kept',
        'proxy_vertices':len(vertices),'supported_proxy_vertices':len(indices),
        'unsupported_proxy_vertices':int((~supported).sum()),'transfer_triangles':len(faces),
        'unsupported_vertex_indices':np.flatnonzero(~supported).tolist(),
        'production_rows_must_all_pass_bounded_surface_transfer':True}


def prepare_body_proxy(sources, *, mode='AUTO', cells_longest=96):
    """Combine only calculation geometry; retain each production Mesh and UV."""
    sources=list(sources)
    if not sources: raise ValueError('请明确身体部件')
    sources=_body_sources(sources[0],sources)
    if len(sources)==1: return prepare_proxy(sources[0],mode=mode,cells_longest=cells_longest)
    before=[skinning_job._proxy_state(o) for o in sources]
    vertices=[];triangles=[];offset=0
    for obj in sources:
        points,faces=skinning.mesh_arrays(obj)
        vertices.append(points);triangles.append(faces+offset);offset+=len(points)
    points=np.concatenate(vertices);faces=np.concatenate(triangles)
    mesh=bpy.data.meshes.new('身体分件_内部计算输入')
    mesh.vertices.add(len(points));mesh.vertices.foreach_set('co',points.astype(np.float32).ravel())
    mesh.loops.add(faces.size);mesh.loops.foreach_set('vertex_index',faces.astype(np.int32).ravel())
    mesh.polygons.add(len(faces));mesh.polygons.foreach_set('loop_start',np.arange(len(faces),dtype=np.int32)*3)
    mesh.polygons.foreach_set('loop_total',np.full(len(faces),3,dtype=np.int32));mesh.update(calc_edges=True)
    joined=bpy.data.objects.new('身体分件_内部计算输入',mesh)
    bpy.context.scene.collection.objects.link(joined)
    proxy=None
    try:
        proxy,report=prepare_proxy(joined,mode=mode,cells_longest=cells_longest)
        if before != [skinning_job._proxy_state(o) for o in sources]:
            raise ValueError('创建组合代理期间身体来源改变，请重新准备')
        report.update(calculation_sources=[o.name for o in sources],production_topology_changed=False,
                      workflow='shared_body_proxy; bind each production part independently')
        proxy['rigging_calculation_proxy_v1']=json.dumps(report,ensure_ascii=False)
        return proxy,report
    except Exception:
        if proxy is not None:
            data=proxy.data;bpy.data.objects.remove(proxy,do_unlink=True)
            if data.users==0:bpy.data.meshes.remove(data)
        raise
    finally:
        bpy.data.objects.remove(joined,do_unlink=True)
        if mesh.users==0:bpy.data.meshes.remove(mesh)


def prepare_proxy(source, *, mode='AUTO', cells_longest=96):
    """Create owned calculation geometry, never replace the production Mesh."""
    _source(source)
    if mode not in {'AUTO', 'RAW', 'VOXEL'} or type(cells_longest) is not int or not 16 <= cells_longest <= 256:
        raise ValueError('代理模式或明确分辨率无效')
    source_fingerprint = skinning.geometry_fingerprint(source)
    proxy = source.copy(); proxy.data = source.data.copy()
    proxy.name = source.name[:38] + '_求解代理_' + uuid.uuid4().hex[:8]
    for modifier in list(proxy.modifiers): proxy.modifiers.remove(modifier)
    bpy.context.scene.collection.objects.link(proxy)
    report = {'source': source.name, 'source_geometry': source_fingerprint,
              'production_topology_changed': False, 'requested_mode': mode}
    try:
        from .volume.geometry import validate_closed
        vertices, triangles = skinning.mesh_arrays(proxy)
        try:
            closed = validate_closed(vertices, triangles)
        except Exception as exc:
            from .volume.contract import VolumeError
            if not isinstance(exc, VolumeError): raise
            closed = None
            report['raw_surface_error'] = {'code': exc.code, 'message': str(exc)}
        if mode == 'RAW' and closed is None:
            raise ValueError('原始表面不能定义完整计算体积；请明确选择内部体素代理：' + str(report['raw_surface_error']))
        if mode == 'VOXEL' or closed is None:
            span = float(np.max(np.ptp(vertices, axis=0)))
            if not np.isfinite(span) or span <= 1e-8: raise ValueError('网格范围退化')
            # Remesh operates in local space. Non-uniform transformed sources
            # are represented by a private world-space proxy, not applied to source.
            proxy.data.transform(proxy.matrix_world)
            proxy.matrix_world.identity()
            modifier = proxy.modifiers.new('临时闭体代理', 'REMESH')
            modifier.mode = 'VOXEL'; modifier.voxel_size = span / cells_longest
            if hasattr(modifier, 'use_remove_disconnected'): modifier.use_remove_disconnected = False
            evaluated = proxy.evaluated_get(bpy.context.evaluated_depsgraph_get())
            remeshed = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=False,
                                                       depsgraph=bpy.context.evaluated_depsgraph_get())
            old_mesh = proxy.data
            proxy.modifiers.clear(); proxy.data = remeshed
            if old_mesh.users == 0: bpy.data.meshes.remove(old_mesh)
            vertices, triangles = skinning.mesh_arrays(proxy)
            closed = validate_closed(vertices, triangles)
            report.update(method='temporary_voxel_remesh', voxel_size=span / cells_longest,
                          limitation='temporary sampling can close narrow gaps; inspect pose and original silhouette')
        else:
            report['method'] = 'raw_closed_surface_copy'
        report.update(proxy=proxy.name, proxy_vertices=len(vertices), closed_surface=closed)
        if skinning.geometry_fingerprint(source) != source_fingerprint:
            raise RuntimeError('源网格保护检查失败')
        proxy['rigging_calculation_proxy_v1'] = json.dumps(report, ensure_ascii=False)
        proxy.hide_render = True
        proxy.display_type = 'WIRE'
        # Retain the editable calculation evidence without obscuring the
        # production surface. Raw geometry sampling does not require display.
        proxy.hide_set(True)
        return proxy, report
    except Exception:
        mesh = proxy.data
        bpy.data.objects.remove(proxy, do_unlink=True)
        if mesh.users == 0: bpy.data.meshes.remove(mesh)
        raise


class BindingJob:
    """One live authority wraps the established worker and optional local layer."""
    def __init__(self, source, rig, proxy, settings, jobs_directory, *, calculation_sources=None, transfer_distance_cells=3):
        self._gate_token = skeleton_gate.require_pass(rig, operation='modular_workflow.BindingJob')
        metadata = rig_identity.read(rig)
        from . import modular_correctives
        self.specs = modular_correctives.weight_specs(rig, metadata)
        self.primary = list(metadata.get('primary_deforms', metadata['deforms']))
        if set(self.primary) & {s['name'] for s in self.specs}:
            raise ValueError('辅助骨不能同时列为主体全局扩散源')
        self.source, self.rig, self.proxy = source, rig, proxy
        self.calculation_sources = list(calculation_sources or [source])
        # The native job already freezes and checks the selected production
        # source. This guard covers only additional body parts.
        self._source_states = [(obj,obj.data,skinning_job._proxy_state(obj))
                               for obj in self.calculation_sources if obj != source]
        self._field_indices,self._field_report=_field_samples(proxy,rig,self.primary,settings)
        self._field_sha=hashlib.sha256(self._field_indices.tobytes()).hexdigest()
        vertices,triangles=skinning.mesh_arrays(proxy)
        from . import modular_proxy_ownership
        self._source_triangles,self._ownership_report=modular_proxy_ownership.source_triangles(
            source,self.calculation_sources,vertices,triangles)
        self._ownership_sha=hashlib.sha256(self._source_triangles.tobytes()).hexdigest()
        scale=float(bpy.context.scene.unit_settings.scale_length)
        span=float(np.ptp(vertices,axis=0).max())
        self._transfer_distance=transfer_distance_cells*float(settings['cell_size']/scale if settings.get('cell_size') else span/settings.get('cells_longest',96))
        self._policy_sha = hashlib.sha256(json.dumps([self.specs,self._transfer_distance], sort_keys=True, allow_nan=False).encode('utf8')).hexdigest()
        self.native = skinning_job.start(source, rig, proxy, self.primary, settings,
                                          jobs_directory=jobs_directory, _sample_proxy=True,
                                          _input_guard=self._check_calculation_sources,
                                          _sample_proxy_indices=self._field_indices)
        try:
            self._support_path=self.native.directory/'field-support.json'
            support={**self._field_report,'proxy_sample_indices_sha256':self._field_sha,
                'source':source.name,'source_state_file':'source-state.json',
                'input_manifest_sha256':self.native._input_hash,
                'input_arrays_sha256':hashlib.sha256((self.native.directory/'input.npz').read_bytes()).hexdigest(),
                'source_state_sha256':hashlib.sha256((self.native.directory/'source-state.json').read_bytes()).hexdigest(),
                'proxy_geometry_sha256':skinning.geometry_fingerprint(proxy),
                'allocation_and_distance_policy_sha256':self._policy_sha,
                'source_surface_ownership':self._ownership_report,
                'source_triangle_mask_sha256':self._ownership_sha,
                'calculation_sources':[{'name':o.name,'object_pointer':o.as_pointer(),'data_pointer':o.data.as_pointer()}
                                       for o in self.calculation_sources],
                'additional_source_states':[{'name':o.name,'state':state} for o,mesh,state in self._source_states],
                'transfer_distance_world_units':self._transfer_distance,
                'no_saved_file_restores_submission_authority':True}
            self._support_path.write_text(json.dumps(support,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf8')
            self._support_file_sha=hashlib.sha256(self._support_path.read_bytes()).hexdigest()
        except Exception:
            # Construction has not returned its job handle to the caller yet.
            # Retire this exact child here, including when cancellation IO fails.
            try:
                self.native.cancel()
            finally:
                process = self.native._process
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except skinning_job.subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise
        self.candidate, self.failure = None, None

    def _check_calculation_sources(self):
        for obj,mesh,state in self._source_states:
            _source(obj)
            if obj.data != mesh or skinning_job._proxy_state(obj) != state:
                raise ValueError('分件身体计算来源已改变，请重新准备完整代理与任务')
        if hasattr(self,'_support_file_sha') and hashlib.sha256(self._support_path.read_bytes()).hexdigest()!=self._support_file_sha:
            raise ValueError('计算表面支持域记录已改变，请重新准备任务')

    def status(self):
        result = self.native.status()
        if self.failure: result.update(status='failed', error=self.failure)
        result['candidate'] = self.candidate.name if self.candidate else None
        result['method'] = 'volume_proxy_surface_transfer_with_local_correctives' if self.specs else 'volume_proxy_surface_transfer'
        return result

    def cancel(self):
        return self.native.cancel()

    def apply(self, *, name):
        skeleton_gate.check_pass(self.rig,self._gate_token,operation='modular_workflow.BindingJob')
        if self.candidate is not None or self.failure:
            raise ValueError('本次候选已处理；重新求解须建立新的任务')
        candidate = None
        try:
            if hashlib.sha256(json.dumps([self.specs,self._transfer_distance], sort_keys=True, allow_nan=False).encode('utf8')).hexdigest() != self._policy_sha:
                raise ValueError('辅助骨分配方案已改变，请重新准备完整任务')
            if hashlib.sha256(self._field_indices.tobytes()).hexdigest()!=self._field_sha:
                raise ValueError('代理表面采样映射已改变，请重新准备任务')
            if hashlib.sha256(self._source_triangles.tobytes()).hexdigest()!=self._ownership_sha:
                raise ValueError('身体部件来源覆盖已改变，请重新准备任务')
            def postprocess(primary_weights, primary_names, report):
                skeleton_gate.check_pass(self.rig,self._gate_token,operation='modular_workflow.BindingJob')
                points, _ = skinning.mesh_arrays(self.source)
                vertices, triangles = skinning.mesh_arrays(self.proxy)
                remap=np.full(len(vertices),-1,dtype=np.int64)
                remap[self._field_indices]=np.arange(len(self._field_indices))
                triangles=remap[triangles[self._source_triangles]]
                triangles=triangles[(triangles>=0).all(axis=1)]
                if not len(triangles):
                    raise ValueError('此部件没有同时满足体素支持与原表面来源的代理三角；请明确分域，未提交权重')
                distance=self._transfer_distance
                def record_projection(ids,barycentric,details):
                    projected=np.einsum('ij,ijk->ik',barycentric,vertices[self._field_indices][ids])
                    distances=np.linalg.norm(projected-points,axis=1)
                    path=self.native.directory/'transfer-projection.npz'
                    with path.open('xb') as stream:
                        np.savez_compressed(stream,sample_vertex_ids=ids,
                                            proxy_vertex_ids=self._field_indices[ids],
                                            sample_to_original_proxy_vertex=self._field_indices,
                                            barycentric=barycentric,distances_world_units=distances,
                                            transaction_state=np.asarray('projection_only_not_a_submission_receipt'))
                    details['projection_artifact']={'file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                        'rows':len(points),'source_row_order':'unchanged original RAW vertices',
                        'sample_vertex_ids_order':'input.json proxy_sample_indices',
                        'proxy_vertex_ids_order':'original proxy RAW vertices',
                        'transaction_state':'projection_only_not_a_submission_receipt',
                        'distance_bound_world_units':distance,'created_before_candidate_commit':True}
                transferred, transfer = skinning.transfer_surface_field(points,vertices[self._field_indices],triangles,
                    primary_weights,max_distance=distance,projection_recorder=record_projection)
                transfer.update(coordinate_space='Blender world units',distance_bound_meters=distance*self.native.scale)
                weights, names, allocation = helper_weights.allocate(points, transferred, primary_names, self.specs)
                method='volume_proxy_surface_transfer_with_local_correctives' if self.specs else 'volume_proxy_surface_transfer'
                report.update(corrective_allocation=allocation, final_bone_names=names,
                              proxy_field_transfer=transfer,proxy_surface_support=self._field_report,
                              proxy_surface_ownership=self._ownership_report,
                              calculation_sources=[o.name for o in self.calculation_sources])
                return {'weights': weights, 'bone_names': names, 'report': report, 'method': method}
            candidate = self.native.apply(name=name, _postprocess=postprocess)
            self.candidate = candidate
            return candidate
        except Exception as exc:
            self.failure = {'code': 'CANDIDATE_REJECTED', 'message': str(exc)}
            if candidate is not None:
                mesh = candidate.data
                bpy.data.objects.remove(candidate, do_unlink=True)
                if mesh.users == 0: bpy.data.meshes.remove(mesh)
            raise


def start_binding(source, rig, *, proxy=None, proxy_mode='AUTO', settings=None, jobs_directory, body_sources=None,
                  transfer_distance_cells=3):
    skeleton_gate.require_pass(rig, operation='modular_workflow.start_binding')
    root = Path(jobs_directory)
    if not root.is_absolute(): raise ValueError('请明确本次后台作业的绝对目录')
    if (type(transfer_distance_cells) not in (int,float) or not np.isfinite(transfer_distance_cells)
            or not .5<=transfer_distance_cells<=4):
        raise ValueError('请明确0.5–4格的代理表面回写距离，不自动扩大上限')
    cfg = {'cells_longest': 96, 'max_cells': 2000000, 'max_memory_mb': 2048,
           'max_seconds': 1200, 'neighbor_count': 26, 'require_all_bones_seeded': True}
    if settings is not None:
        if 'cell_size' in settings: cfg.pop('cells_longest',None)
        cfg.update(settings)
    sources=_body_sources(source,body_sources)
    made = proxy is None
    if made:
        proxy, _ = prepare_body_proxy(sources, mode=proxy_mode, cells_longest=cfg.get('cells_longest', 96))
    try:
        job=BindingJob(source, rig, proxy, cfg, root, calculation_sources=sources,
                       transfer_distance_cells=transfer_distance_cells)
        return job
    except Exception:
        if made:
            mesh = proxy.data
            bpy.data.objects.remove(proxy, do_unlink=True)
            if mesh.users == 0: bpy.data.meshes.remove(mesh)
        raise
