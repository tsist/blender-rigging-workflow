# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit rigid/garment/local weight candidates; no source weight writes.

Transfers read applied RAW vertex groups on the same neutral rig. Local edits
redistribute the existing editable mass, preserving locks and every other value.
"""
import json
import hashlib
import math
import re
import bpy
import numpy as np
from . import skeleton_gate, skinning, skinning_job, corrective_authoring as sig, pose_switching

_require = skinning_job._require


def _object(obj, kind):
    _require(bpy.context.mode == 'OBJECT' and obj and obj.type == kind
             and obj.name in bpy.context.view_layer.objects and not obj.library
             and not obj.override_library and not obj.data.library,
             '请选择对象模式中当前视图层的本地' + kind + '对象')
    matrix = np.asarray(obj.matrix_world, dtype=float)
    _require(np.isfinite(matrix).all() and abs(np.linalg.det(matrix[:3, :3])) > 1e-12,
             '对象世界矩阵必须有限且可逆')


def _rig(rig, names):
    from . import rig_identity
    _object(rig, 'ARMATURE'); data = rig_identity.read(rig)
    _require(isinstance(names, list) and names and len(set(names)) == len(names)
             and all(isinstance(n, str) and n in data['deforms'] for n in names),
             '明确指定当前生成角色的最终形变输出骨，不能使用控制/机制骨')
    skinning.bone_segments(rig, names)
    scale = float(bpy.context.scene.unit_settings.scale_length)
    _require(math.isfinite(scale) and scale > 0 and rig.data.pose_position == 'POSE', '需要姿态显示及有效米制比例')
    from . import neutral_binding
    if data.get('schema') == 'rigging.native-skeleton.v1':
        from . import native_skeleton
        native_skeleton.check_current(rig, data, names, scale)
        return scale
    if data.get('schema') == 'rigging.modular.generated.v1':
        from . import modular_neutral
        modular_neutral.check_current(rig, data, names, scale)
        return scale
    if neutral_binding.KEY in data:
        neutral_binding.check_current(rig, data, names, scale)
        return scale
    er = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    for name in names:
        p, b = er.pose.bones[name], rig.data.bones[name]
        delta = np.asarray(p.matrix @ b.matrix_local.inverted())
        error = max((rig.matrix_world.to_3x3() @ (a-c)).length * scale
                    for a, c in ((p.head, b.head_local), (p.tail, b.tail_local)))
        _require(np.isfinite(delta).all() and np.max(abs(delta[:3,:3]-np.eye(3))) <= 1e-5 and error <= 1e-5,
                 '参与骨不在校准中立状态，先恢复中立再准备权重策略')
    return scale


def _ids(value, count):
    ids = np.asarray(value)
    _require(ids.ndim == 1 and ids.dtype.kind in 'iu' and len(ids)
             and (ids >= 0).all() and (ids < count).all() and len(np.unique(ids)) == len(ids),
             '顶点范围必须明确、非空、无重复且在RAW网格内')
    return ids.astype(np.int64)


def _new_target(obj):
    _object(obj, 'MESH')
    _require(not obj.vertex_groups and not any(v.groups for v in obj.data.vertices)
             and not obj.data.shape_keys and not obj.parent and not obj.constraints and not obj.animation_data and not obj.data.animation_data
             and all(m.type == 'SUBSURF' for m in obj.modifiers),
             '新绑定需要无旧权重/Key/父级/约束/动画的网格，只保留原Subsurf；已有绑定走局部维护')


def _bound(obj, rig, *, donor=False):
    _object(obj, 'MESH')
    armatures = [m for m in obj.modifiers if m.type == 'ARMATURE']
    _require(len(armatures) == 1 and armatures[0].object == rig
             and armatures[0].show_viewport and armatures[0].use_vertex_groups
             and not armatures[0].use_bone_envelopes and not armatures[0].vertex_group
             and all(m.type in {'ARMATURE','SUBSURF'} for m in obj.modifiers)
             and obj.modifiers[0] == armatures[0] and not obj.constraints and not obj.animation_data and not obj.data.animation_data,
             '需要同一角色的普通组权重Armature→可选Subsurf绑定，无额外约束或动画')
    keys = obj.data.shape_keys
    _require(not keys or not donor, '供体RAW场不接形态键；请使用明确的中立RAW供体')
    if keys:
        _key_animation_state(keys)


def _key_animation_state(key):
    """Freeze neutral native property drivers without migrating their authorship.

    Weight maintenance copies a Key datablock, not its external control hub.
    Only plain scalar-property drivers are accepted; Action/NLA, self and
    geometry/transform-reading drivers retain the existing rejection boundary.
    """
    ad=key.animation_data
    if ad is None:return None
    _require(ad.action is None and not ad.nla_tracks and not ad.use_tweak_mode,
             '局部权重维护不接Key Action/NLA；请先保留动画并建立中立维护副本')
    _require(key.use_relative and all(k==key.reference_key or k.value==0. for k in key.key_blocks),
             '带驱动Key须为relative并全部非Basis值为0；请先复位表情，不删除原Key/驱动')
    paths={k.path_from_id('value') for k in key.key_blocks if k!=key.reference_key}
    identities=set();dependencies=[];mappings=[]
    for curve in ad.drivers:
        identity=(curve.data_path,curve.array_index)
        driver=curve.driver
        _require(identity not in identities and curve.data_path in paths and curve.array_index==0
                 and not curve.mute and not curve.modifiers and driver.is_valid and not driver.use_self
                 and ((driver.type=='SCRIPTED' and driver.is_simple_expression)
                      or driver.type in {'AVERAGE','SUM','MIN','MAX'}),
                 '仅支持有效、无修饰器且不读取self的原生简单Key值驱动')
        identities.add(identity)
        mappings.append({'keys':[sig._rna(point) for point in curve.keyframe_points],
                         'samples':[list(point.co) for point in curve.sampled_points]})
        for variable in driver.variables:
            _require(variable.type=='SINGLE_PROP', '局部维护的Key驱动仅支持外部标量属性，不接几何/变换查询')
            target=variable.targets[0];owner=target.id;path=target.data_path
            _require(isinstance(owner,bpy.types.Object) and not owner.library
                     and re.fullmatch(r'(?:pose\.bones\["(?:[^"\\]|\\.)*"\])?\["(?:[^"\\]|\\.)*"\]',path),
                     'Key驱动须读取本地对象的明确自定义标量属性')
            value=owner.path_resolve(path)
            _require(type(value) in (bool,int,float) and math.isfinite(value), 'Key驱动输入必须为有限标量')
            dependencies.append([owner.session_uid,path,value])
    return {'animation':sig._rna(ad),'drivers':sig._drivers(key),'inputs':dependencies,'mappings':mappings}


def _shape_state(obj):
    key = obj.data.shape_keys
    if not key: return None
    animation=_key_animation_state(key)
    rows = []
    for block in key.key_blocks:
        points = np.empty((len(block.data),3),dtype=np.float32);block.data.foreach_get('co',points.ravel())
        rows.append([block.name,sig._array_hash(points),block.relative_key.name,block.value,
                     block.mute,block.slider_min,block.slider_max,block.interpolation,block.vertex_group,block.frame])
    return [key.use_relative,key.eval_time,rows,animation,sig._plain(dict(key.items()))]


def _state(obj, rig):
    return {'source':sig._state(obj,rig),'object':skinning_job._proxy_state(obj),
            'shape_keys':_shape_state(obj),'group_active':obj.vertex_groups.active_index,
            'binding_record':obj.get('c94_skinning_v1'),'policy_record':obj.get('c94_skinning_policy_v1')}


def _field(donor, rig, names, region):
    _bound(donor,rig,donor=True)
    vertices, triangles = skinning.mesh_arrays(donor)
    if region is not None:
        selected = np.zeros(len(vertices),dtype=bool);selected[_ids(region,len(vertices))]=True
        triangles = triangles[selected[triangles].all(axis=1)]
    _require(len(triangles), '供体明确范围没有完整三角，不能转移')
    if names is None:
        from . import rig_identity
        available = set(rig_identity.read(rig)['deforms'])
        names = [g.name for g in donor.vertex_groups if g.name in available]
    scale=_rig(rig,names)
    indices = {g.index:i for i,n in enumerate(names) if (g:=donor.vertex_groups.get(n)) is not None}
    excluded = {g.index for g in donor.vertex_groups if g.name not in names
                and (b:=rig.data.bones.get(g.name)) is not None and b.use_deform}
    weights = np.zeros((len(vertices),len(names)),dtype=np.float64)
    omitted = np.zeros(len(vertices),dtype=np.float64)
    for vertex in donor.data.vertices:
        for group in vertex.groups:
            if group.group in indices: weights[vertex.index,indices[group.group]]=group.weight
            elif group.group in excluded: omitted[vertex.index]+=group.weight
    used = np.unique(triangles)
    _require(not omitted[used].any(),'供体范围包含未声明的实际形变骨正权重，不能丢列转移')
    _require(np.isfinite(weights).all() and (weights>=0).all()
             and np.max(abs(weights[used].sum(axis=1)-1))<=1e-5,
             '供体范围的实际已应用骨权重必须完整、非负且和为一；不补骨或归一化兜底')
    return vertices,triangles,weights,list(names),scale


class PreparedWeights:
    def __init__(self, target, rig, names, weights, report, *, donor=None, local_ids=None, alpha=None,
                 write_scope_sha256=None):
        self._gate_token = skeleton_gate.require_pass(
            rig, operation='skinning_policy.PreparedWeights', write_scope_sha256=write_scope_sha256)
        self._prepare_scope_sha256 = write_scope_sha256
        self.target,self.rig,self.donor=target,rig,donor
        self._scene=bpy.context.scene;self._mesh=target.data;self._rig_data=rig.data
        self._before=_state(target,rig);self._donor_before=_state(donor,rig) if donor else None
        self._donor_mesh=donor.data if donor else None
        self.names=tuple(names);self._weights=np.asarray(weights,dtype=np.float32).copy();self._weights.setflags(write=False)
        self._ids=None if local_ids is None else np.asarray(local_ids,dtype=np.int64).copy()
        self._alpha=np.ones(len(target.data.vertices),dtype=float) if alpha is None else np.asarray(alpha,dtype=float).copy()
        self.report=report;self._done=False;self._preview=None

    def _check_permission(self, scope_sha256):
        permit = skeleton_gate.require_pass(
            self.rig, operation='skinning_policy.PreparedWeights', write_scope_sha256=scope_sha256)
        _require(permit is self._gate_token, '准备后运行许可已更换，不能提交旧计划')

    def _check(self):
        self._check_permission(self._prepare_scope_sha256)
        _object(self.target,'MESH');_object(self.rig,'ARMATURE')
        if self.donor:_object(self.donor,'MESH')
        _require(not self._done and bpy.context.scene==self._scene and self.target.data==self._mesh
                 and self.rig.data==self._rig_data,'计划已结束或真实场景/对象数据身份已改变')
        _require(_state(self.target,self.rig)==self._before,'源网格、权重/锁组、骨架、帧或单位已改变，请重新准备')
        if self.donor:
            _require(self.donor.data==self._donor_mesh and _state(self.donor,self.rig)==self._donor_before,
                     '已应用供体场发生变化，请重新准备')

    def cancel(self): self._done=True

    def preview(self, *, name, write_scope_sha256=None):
        self._check()
        obj=create_scope_preview(self.target,self._alpha,name=name,rig=self.rig,write_scope_sha256=write_scope_sha256)
        try:
            self._check();self._preview=obj
            return obj
        except Exception:
            mesh=obj.data
            bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
            raise

    def apply(self, *, name, write_scope_sha256=None):
        self._check();_require(isinstance(name,str) and name.strip() and name not in bpy.data.objects,'候选名称已使用或为空')
        if self._prepare_scope_sha256 is not None:
            _require(write_scope_sha256 is not None and write_scope_sha256 != self._prepare_scope_sha256,
                     '原生准备回执不能用于应用，须登记独立准确的应用范围')
            self._check_permission(write_scope_sha256)
        before_points=skinning_job._points(self.target);candidate=None;mesh=None
        try:
            if self._ids is None:
                candidate=skinning.create_bound_copy(self.target,self.rig,self._weights,list(self.names),name=name,
                    report=self.report,method=self.report['method']);mesh=candidate.data
            else:
                candidate=self.target.copy();mesh=self.target.data.copy();candidate.data=mesh;candidate.name=name
                bpy.context.scene.collection.objects.link(candidate)
                for j,n in enumerate(self.names):
                    group=candidate.vertex_groups[n]
                    group.remove(self._ids.tolist())
                    for row,i in enumerate(self._ids):
                        if self._weights[row,j]>0:group.add([int(i)],float(self._weights[row,j]),'REPLACE')
                _verify_local(self.target,candidate,self.names,self._ids,self._weights)
                candidate['c94_skinning_policy_v1']=json.dumps(self.report,ensure_ascii=False,allow_nan=False)
                candidate['c94_skinning_v1']=json.dumps({'schema':'c94.skinning.v1','source':self.target.name,
                    'rig':self.rig.name,'method':self.report['method'],
                    'parent_binding_record_sha256':hashlib.sha256(self._before['binding_record'].encode('utf8')).hexdigest()
                    if isinstance(self._before['binding_record'],str) else None,
                    'report':self.report},ensure_ascii=False,allow_nan=False)
            _require(sig._mesh_signature(candidate.data)==sig._mesh_signature(self.target.data)
                     and _shape_state(candidate)==_shape_state(self.target),'候选改变了非权重网格或形态键')
            bpy.context.view_layer.update();points=skinning_job._points(candidate)
            scale=bpy.context.scene.unit_settings.scale_length
            _require(points.shape==before_points.shape and np.linalg.norm(points-before_points,axis=1).max(initial=0)*scale<=1e-5,
                     '候选中立求值跳变，已撤回')
            self._check();self._done=True
            return candidate
        except Exception:
            if candidate is not None:bpy.data.objects.remove(candidate,do_unlink=True)
            if mesh is not None and mesh.users==0:bpy.data.meshes.remove(mesh)
            raise


def create_scope_preview(target, full_alpha, *, name, rig=None, write_scope_sha256=None):
    """Independent display only; no weight proposal or submission authority."""
    if rig is None:
        armatures=[m.object for m in getattr(target,'modifiers',()) if m.type=='ARMATURE' and m.object] if target is not None else []
        if len(armatures)!=1:
            raise skeleton_gate.GateError('骨架质量门：范围预览需要明确rig；target须有唯一Armature修改器')
        rig=armatures[0]
    skeleton_gate.require_pass(rig, operation='skinning_policy.create_scope_preview', write_scope_sha256=write_scope_sha256)
    _object(target,'MESH')
    blend=np.asarray(full_alpha,dtype=float)
    _require(blend.shape==(len(target.data.vertices),) and np.isfinite(blend).all()
             and (blend>=0).all() and (blend<=1).all(),'范围预览需要每个RAW点的0–1系数')
    _require(isinstance(name,str) and name.strip() and name not in bpy.data.objects,'预览名称已使用或为空')
    obj=target.copy();mesh=target.data.copy();obj.data=mesh;obj.name=name
    try:
        bpy.context.scene.collection.objects.link(obj)
        group=obj.vertex_groups.new(name='C94_计划写入范围')
        for i in np.flatnonzero(blend>0):group.add([int(i)],float(blend[i]),'REPLACE')
        obj.vertex_groups.active_index=group.index;obj['c94_policy_preview_v1']=True
        color=mesh.color_attributes.new(name='C94_计划范围颜色',type='FLOAT_COLOR',domain='POINT')
        values=np.column_stack((blend,1-abs(2*blend-1),1-blend,np.ones(len(blend))))
        color.data.foreach_set('color',values.astype(np.float32).ravel())
        mesh.color_attributes.active_color=color;obj.show_in_front=True
        return obj
    except Exception:
        bpy.data.objects.remove(obj,do_unlink=True)
        if mesh.users==0:bpy.data.meshes.remove(mesh)
        raise


def _verify_local(source, candidate, names, ids, values, *, check_locks=True):
    source_names,rows=sig._weights(source);new_names,new_rows=sig._weights(candidate)
    _require(new_names==source_names,'已有顶点组集合或顺序改变')
    mutable=set(names);selected=set(ids.tolist())
    for i,(old,new) in enumerate(zip(rows,new_rows)):
        old={source_names[g]:w for g,w in old};new={new_names[g]:w for g,w in new}
        protected=lambda row:{g:w for g,w in row.items() if i not in selected or g not in mutable}
        _require(protected(old)==protected(new),'非目标点/组权重改变')
    _require(not check_locks or [g.lock_weight for g in candidate.vertex_groups]==[g.lock_weight for g in source.vertex_groups],
             '锁组元数据改变')
    for j,n in enumerate(names):
        g=candidate.vertex_groups[n]
        for row,i in enumerate(ids):
            actual=next((v.weight for v in candidate.data.vertices[int(i)].groups if v.group==g.index),0.)
            _require(actual==float(values[row,j]),'局部权重写入未保持原生float32值')


def prepare_rigid(target,rig,assignments):
    skeleton_gate.require_pass(rig,operation='skinning_policy.prepare_rigid')
    _new_target(target)
    _require(isinstance(assignments,list) and assignments and all(isinstance(row,dict)
             and set(row)=={'bone_name','vertex_indices'} and isinstance(row['bone_name'],str) for row in assignments),
             '明确指定刚性顶点域和最终骨')
    names=list(dict.fromkeys(row['bone_name'] for row in assignments));_rig(rig,names)
    weights=np.zeros((len(target.data.vertices),len(names)),dtype=np.float32)
    for row in assignments:
        ids=_ids(row['vertex_indices'],len(weights));_require(not weights[ids].any(),'刚性域重叠')
        weights[ids,names.index(row['bone_name'])]=1
    _require((weights.sum(axis=1)==1).all(),'纯刚性计划必须明确覆盖全部RAW顶点')
    return PreparedWeights(target,rig,names,weights,{'schema':'c94.weight-policy.v1','method':'explicit_rigid',
        'target':target.name,'rig':rig.name,'vertices':len(weights),'bones':names,'source_unchanged':True})


def prepare_transfer(target,rig,donor,*,max_distance_meters,bone_names=None,donor_vertex_indices=None,
                     local_vertex_indices=None,alpha=None,editable_bone_names=None,write_scope_sha256=None):
    local=local_vertex_indices is not None
    _require(write_scope_sha256 is None or local, '准确范围的转移入口只接明确的局部顶点计划')
    skeleton_gate.require_pass(rig, operation='skinning_policy.prepare_transfer', write_scope_sha256=write_scope_sha256)
    if local:_bound(target,rig)
    else:_new_target(target)
    _require(target!=donor,'目标和供体必须是不同对象，不能把自身未修场当新提案')
    _require(type(max_distance_meters) in (int,float) and math.isfinite(max_distance_meters) and max_distance_meters>0,
             '明确指定有限正米制转移距离')
    vertices,triangles,field,names,scale=_field(donor,rig,bone_names,donor_vertex_indices)
    points,_=skinning.mesh_arrays(target)
    ids=_ids(local_vertex_indices,len(points)) if local else np.arange(len(points),dtype=np.int64)
    weights,transfer=skinning.transfer_surface_field(points[ids]*scale,vertices*scale,triangles,field,max_distance=max_distance_meters)
    transfer['method']='barycentric_applied_raw_weight_transfer'
    report={'schema':'c94.weight-policy.v1','method':'applied_raw_surface_transfer','target':target.name,'rig':rig.name,
        'donor':donor.name,'donor_domain':'RAW current vertex groups; all complete triangles' if donor_vertex_indices is None
            else 'RAW current vertex groups; complete triangles within explicit vertex IDs',
        'donor_bone_names':names,'donor_region':'ALL_RAW' if donor_vertex_indices is None else 'EXPLICIT_VERTEX_IDS',
        'donor_triangles':len(triangles),'donor_surface_vertices':int(len(np.unique(triangles))),
        'donor_geometry_sha256':hashlib.sha256(vertices.tobytes()+triangles.tobytes()).hexdigest(),
        'donor_weight_field_sha256':hashlib.sha256(field.tobytes()).hexdigest(),
        'transfer':transfer,'max_distance_meters':max_distance_meters,'vertices':len(ids),'source_unchanged':True}
    if local:
        report['method']='local_applied_field_redistribution'
        return _local_field_plan(target,rig,ids,alpha,editable_bone_names,weights,names,report,donor=donor,
                                 write_scope_sha256=write_scope_sha256)
    return PreparedWeights(target,rig,names,weights,report,donor=donor)


PRESERVE_MASS = 'preserve_mass'
SELECTED_DIRECT = 'selected_direct'
SELECTED_APPLY_OPERATION = 'local_skinning_job.apply_selected'


def local_scope(target,rig,editable_bone_names,vertex_indices,alpha,*,write_mode=PRESERVE_MASS):
    """Validate a local write scope without creating any weight proposal."""
    _require(write_mode in (PRESERVE_MASS, SELECTED_DIRECT), '局部权重写入模式无效')
    _bound(target,rig)
    _require(isinstance(editable_bone_names,list) and editable_bone_names,'明确指定局部可写最终骨组')
    scale=_rig(rig,editable_bone_names)
    ids=_ids(vertex_indices,len(target.data.vertices));blend=np.asarray(alpha,dtype=float)
    _require(blend.shape==(len(ids),) and np.isfinite(blend).all() and (blend>=0).all() and (blend<=1).all()
             and (blend>0).any(),'每个局部顶点需要明确0–1过渡系数')
    locked=[n for n in editable_bone_names if (g:=target.vertex_groups.get(n)) is not None and g.lock_weight]
    editable=[n for n in editable_bone_names if n not in locked]
    _require(editable,'全部声明骨组已锁定，没有可写组')
    _require(all(target.vertex_groups.get(n) is not None for n in editable),
             '局部维护只写已有目标骨组，不自动创建组；请先明确整理缺失顶点组')
    _require(not target.data.shape_keys or not any(k.vertex_group in editable for k in target.data.shape_keys.key_blocks),
             '可写骨组同时用作形态键遮罩，改权重会改变Key效果；请先整理独立Key遮罩组')
    old=np.zeros((len(ids),len(editable)),dtype=float)
    for j,n in enumerate(editable):
        group=target.vertex_groups[n]
        for row,i in enumerate(ids):old[row,j]=next((v.weight for v in target.data.vertices[int(i)].groups if v.group==group.index),0.)
    mass=old.sum(axis=1)
    active=blend>0
    if write_mode==PRESERVE_MASS:
        active=active&(mass>0)
        _require(active.any(),'明确写域内没有可分配的原可写组权重质量；不能从锁组或其它组取值')
    preview=np.zeros(len(target.data.vertices));preview[ids]=np.where(active,blend,0)
    summary={'editable_bones':editable,'locked_bones_preserved':locked,'transition_vertices':int((active&(blend<1)).sum()),
        'requested_vertices':int((blend>0).sum()),'write_vertices':int(active.sum()),
        'zero_editable_mass_vertices':int(((blend>0)&(mass==0)).sum()),
        'writable_mass':'original editable sum per vertex; no other-group normalization'}
    if write_mode==SELECTED_DIRECT:
        summary.update(write_mode=write_mode,
                       writable_mass='blend selected solution values directly; no other-group normalization')
    keys=target.data.shape_keys
    if keys and keys.animation_data and keys.animation_data.drivers:
        summary['retained_key_drivers']=len(keys.animation_data.drivers)
        summary['warnings']=['保留原Key及外部控制引用；不改作者控制组成员。改权重后须重采受影响的骨控表情Key。']
    return {'ids':ids.copy(),'alpha':blend.copy(),'editable':editable,'old':old,'mass':mass,
            'active':active,'preview_alpha':preview,'summary':summary,'scale':scale}


def _local_field_plan(target,rig,ids,alpha,editable_bone_names,weights,names,report,donor=None,*,
                      write_mode=PRESERVE_MASS,write_scope_sha256=None):
    scope=local_scope(target,rig,editable_bone_names,ids,alpha,write_mode=write_mode)
    _require(set(scope['editable'])<=set(names),
             '可写目标骨组缺少明确供体列，不能静默清零；请明确完整提案骨列')
    proposal=np.asarray(weights,dtype=float)[:,[names.index(n) for n in scope['editable']]]
    active=scope['active']
    if write_mode==SELECTED_DIRECT:
        wanted=proposal
    else:
        total=proposal.sum(axis=1);mass=scope['mass']
        _require((total[active]>0).all(),'可写原权重质量有正值但提案在这些骨组无覆盖，不能兜底')
        wanted=scope['old'].copy();wanted[active]=proposal[active]*(mass[active]/total[active])[:,None]
    blend=scope['alpha'][:,None]
    output=((1-blend)*scope['old']+blend*wanted).astype(np.float32)
    report.update(scope['summary'])
    return PreparedWeights(target,rig,scope['editable'],output[active],report,donor=donor,
                           local_ids=scope['ids'][active],alpha=scope['preview_alpha'],write_scope_sha256=write_scope_sha256)


def prepare_local_field(target,rig,weights,bone_names,vertex_indices,alpha,editable_bone_names,*,method,details=None,
                        write_mode=PRESERVE_MASS):
    """Validated numeric proposal shared by direct volume and native painting."""
    permit=skeleton_gate.require_pass(rig,operation='skinning_policy.prepare_local_field')
    _rig(rig,bone_names)
    ids=_ids(vertex_indices,len(target.data.vertices));values=np.asarray(weights,dtype=float)
    _require(values.shape==(len(ids),len(bone_names)) and np.isfinite(values).all() and (values>=0).all(),
             '局部提案矩阵形状/有限性/非负校验失败')
    _require(method in {'local_volume_diffusion','native_weight_paint'},'局部提案方法无效')
    if write_mode==SELECTED_DIRECT:
        _require(skeleton_gate._active_write.get()==(permit,SELECTED_APPLY_OPERATION),
                 '选骨局部重算需要正在采用的精确写入许可')
        _require(method=='local_volume_diffusion' and len(bone_names)>=2 and (values<=1).all(),
                 '选骨局部重算只接受有上下文骨的0–1体积权重提案')
    report={'schema':'c94.weight-policy.v1','method':method,'target':target.name,'rig':rig.name,
            'vertices':len(ids),'source_unchanged':True,'proposal':details or {}}
    return _local_field_plan(target,rig,ids,alpha,editable_bone_names,values,list(bone_names),report,
                             write_mode=write_mode)
