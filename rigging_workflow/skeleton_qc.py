"""Bone-only evidence and a fail-closed review receipt; never performs skinning."""
import bpy,json,hashlib,math,re
from pathlib import Path
import numpy as np

SCHEMA='rigging.skeleton-quality.v1'
CHECKS=('anatomical_placement','depth_and_views','chains_and_axes','body_connections',
        'isolated_fk','ik_and_support','intermediate_and_extension','bilateral','neutral_restore')
PALETTE={
 'trunk':{'label':'躯干与骨盆','color':[.82,.65,.12]},
 'head_neck':{'label':'头颈','color':[.70,.30,.85]},
 'fore_left':{'label':'左上肢/前肢','color':[.16,.62,.95]},
 'fore_right':{'label':'右上肢/前肢','color':[.95,.32,.18]},
 'hind_left':{'label':'左下肢/后肢','color':[.16,.78,.50]},
 'hind_right':{'label':'右下肢/后肢','color':[.94,.38,.65]},
 'tail_accessory':{'label':'尾与附属','color':[.55,.68,.92]},
}

def _native_color(color):
    # ThemeBoneColorSet stores COLOR_GAMMA as 8-bit channels in Blender 5.2.
    # Canonicalize before assignment, then compare the same representable bytes.
    return tuple(round(float(c)*255)/255 for c in color)

_RNA_SKIP = frozenset({'rna_type','select','select_head','select_tail','hide','hide_select','show_expanded'})
_RNA_SCALAR = frozenset({'BOOLEAN','INT','FLOAT','STRING','ENUM'})
_RNA_FIELD_CACHE = {}

def _plain(v):
    t = type(v)
    if v is None or t is str or t is int or t is float or t is bool:return v
    if isinstance(v,bpy.types.ID):return {'type':v.bl_rna.identifier,'name':v.name_full,'library':v.library.filepath if v.library else None}
    if hasattr(v,'to_dict'):return _plain(v.to_dict())
    if t is dict:return {k:_plain(x) for k,x in v.items()}
    return [_plain(x) for x in v]

def _rna_fields(bl_rna):
    key = bl_rna.identifier
    cached = _RNA_FIELD_CACHE.get(key)
    if cached is not None:
        return cached
    fields = []
    for p in bl_rna.properties:
        if p.identifier in _RNA_SKIP or p.is_readonly:continue
        if p.type in _RNA_SCALAR:
            fields.append((p.identifier, False, getattr(p, 'is_array', False)))
        elif p.type == 'POINTER':
            fields.append((p.identifier, True, False))
    result = tuple(fields)
    _RNA_FIELD_CACHE[key] = result
    return result

def _rna(obj):
    out={}
    for ident, is_ptr, is_arr in _rna_fields(obj.bl_rna):
        v=getattr(obj,ident)
        if is_ptr:
            if isinstance(v,bpy.types.ID):
                out[ident]={'type':v.bl_rna.identifier,'name':v.name_full,'library':v.library.filepath if v.library else None}
        elif is_arr:
            out[ident]=[_plain(x) for x in v]
        else:
            t = type(v)
            out[ident]=v if (v is None or t is str or t is int or t is float or t is bool) else _plain(v)
    return out

def digest(doc):
    return hashlib.sha256(json.dumps(doc,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()

def file_sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def write_new(path,doc):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf8') as stream:json.dump(doc,stream,ensure_ascii=False,indent=2,allow_nan=False)

def _constraints(owner):
    return [{'type':c.type,'rna':_rna(c),'targets':[_rna(t) for t in getattr(c,'targets',())]} for c in owner.constraints]

def _drivers(owner):
    ad=owner.animation_data
    return [] if not ad else [{'rna':_rna(f),'type':f.driver.type,'expression':f.driver.expression,
       'use_self':f.driver.use_self,'variables':[{'rna':_rna(v),'targets':[_rna(t) for t in v.targets]} for v in f.driver.variables],
       'keys':[_rna(k) for k in f.keyframe_points],'modifiers':[_rna(m) for m in f.modifiers]} for f in ad.drivers]

def _action(action):
    if action is None:return None
    layers=[]
    for layer in action.layers:
        strips=[]
        for strip in layer.strips:
            bags=[]
            for slot in action.slots:
                bag=strip.channelbag(slot)
                if bag is not None:
                    bags.append({'slot':slot.identifier,'curves':[{'rna':_rna(f),
                        'keys':[_rna(k) for k in f.keyframe_points],
                        'samples':[_plain(k.co) for k in f.sampled_points],
                        'modifiers':[_rna(m) for m in f.modifiers]} for f in bag.fcurves]})
            strips.append({'rna':_rna(strip),'bags':bags})
        layers.append({'rna':_rna(layer),'strips':strips})
    return {'identity':_plain(action),'rna':_rna(action),'custom':_plain(dict(action.items())),
      'slots':[{'identifier':s.identifier,'handle':s.handle,'rna':_rna(s)} for s in action.slots],'layers':layers}

def _animation(owner):
    ad=owner.animation_data
    if not ad:return None
    # NLA needs explicit sampling/identity support rather than silent omission.
    if len(ad.nla_tracks):raise ValueError('NLA质检范围尚未实现；门保持关闭，请显式准备骨架检查Action')
    return {'action':_action(ad.action),'slot':ad.action_slot.identifier if ad.action_slot else None,'settings':_rna(ad)}

def _dependency_objects(rig):
    """Follow declared transform/armature inputs; unsupported geometry stays shut."""
    objects={rig};scenes=set();queue=[rig]
    while queue:
        obj=queue.pop()
        targets=[obj.parent] if obj.parent else []
        owners=[obj]+(list(obj.pose.bones) if obj.type=='ARMATURE' else [])
        for owner in owners:
            for c in owner.constraints:
                if c.type in {'SHRINKWRAP','SPLINE_IK','FOLLOW_PATH','CLAMP_TO','TRANSFORM_CACHE'}:
                    raise ValueError('几何/缓存约束输入质检身份尚未覆盖，门关闭：'+c.type)
                for field in ('target','pole_target','space_object'):
                    target=getattr(c,field,None)
                    if isinstance(target,bpy.types.Object):
                        subfield={'target':'subtarget','pole_target':'pole_subtarget','space_object':'space_subtarget'}[field]
                        if getattr(c,subfield,'') and target.type!='ARMATURE':raise ValueError('几何subtarget依赖尚未覆盖，门关闭：'+c.name)
                        targets.append(target)
                for target in getattr(c,'targets',()):
                    if target.target:
                        if target.subtarget and target.target.type!='ARMATURE':raise ValueError('非骨架subtarget依赖尚未覆盖，门关闭：'+c.name)
                        targets.append(target.target)
        for owner in (obj,obj.data):
            if owner is None or not getattr(owner,'animation_data',None):continue
            for curve in owner.animation_data.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        data=target.id
                        if data is None or data==rig.data:continue
                        if isinstance(data,bpy.types.Object):
                            if variable.type=='SINGLE_PROP':
                                path=target.data_path
                                safe=path.startswith('["') or any(path==k or path.startswith(k+'[') for k in
                                     ('location','scale','rotation_euler','rotation_quaternion','rotation_axis_angle'))
                                safe=safe or data.type=='ARMATURE' and path.startswith(('pose.bones[','data.bones['))
                                if not safe:raise ValueError('未支持的Object属性驱动依赖路径，门关闭：'+path)
                            targets.append(data)
                        elif isinstance(data,bpy.types.Scene):
                            if variable.type!='SINGLE_PROP' or not re.fullmatch(r'\["[^"\]]+"\](?:\[\d+\])?',target.data_path):
                                raise ValueError('Scene驱动仅支持明确的顶层自定义属性，其他RNA路径门关闭：'+target.data_path)
                            key=target.data_path.split('"')[1]
                            value=data.get(key)
                            if not isinstance(value,(bool,int,float,str)) and not hasattr(value,'to_list'):
                                raise ValueError('Scene自定义依赖类型尚未支持，门关闭：'+key)
                            scenes.add(data)
                        else:raise ValueError('未支持的外部驱动依赖类型，门关闭：'+data.bl_rna.identifier)
        for target in targets:
            if target not in objects:objects.add(target);queue.append(target)
    return objects,scenes

def _runtime_state(rig):
    objects,scenes=_dependency_objects(rig)
    fields=('location','scale','rotation_mode','rotation_euler','rotation_quaternion','rotation_axis_angle')
    state=[]
    for obj in objects:
        for owner in [obj]+(list(obj.pose.bones) if obj.type=='ARMATURE' else []):
            channels={k:_plain(getattr(owner,k)) for k in fields}
            custom={k:_plain(v) for k,v in owner.items() if isinstance(v,(bool,int,float,str)) or hasattr(v,'to_list')}
            state.append((owner,channels,custom))
    for scene in scenes:
        custom={k:_plain(v) for k,v in scene.items() if isinstance(v,(bool,int,float,str)) or hasattr(v,'to_list')}
        state.append((scene,{},custom))
    return state

def _restore_runtime(state):
    for owner,channels,custom in state:
        if channels:owner.rotation_mode=channels['rotation_mode']
        for k,v in channels.items():
            if k!='rotation_mode':setattr(owner,k,v)
        for k,v in custom.items():owner[k]=v
    bpy.context.view_layer.update()

def _external_dependencies(rig):
    objects,scenes=_dependency_objects(rig)
    out=[]
    for obj in sorted(objects-{rig},key=lambda x:x.name_full):
        row={'identity':_plain(obj),'matrix':_plain(obj.matrix_world),'parent':_plain(obj.parent),
             'parent_inverse':_plain(obj.matrix_parent_inverse),'rna':_rna(obj),'custom':_plain(dict(obj.items())),
             'animation':_animation(obj),'drivers':_drivers(obj),'constraints':_constraints(obj)}
        if obj.type=='ARMATURE':
            row['bones']=[{'name':b.name,'parent':b.parent.name if b.parent else None,'rna':_rna(b),
                'rest':_plain(b.matrix_local),'pose':_rna(obj.pose.bones[b.name]),'custom':_plain(dict(obj.pose.bones[b.name].items())),
                'constraints':_constraints(obj.pose.bones[b.name])} for b in obj.data.bones]
            row['data_custom']=_plain(dict(obj.data.items()));row['data_drivers']=_drivers(obj.data);row['data_animation']=_animation(obj.data)
        out.append(row)
    return {'objects':out,'scene_inputs':[{'identity':_plain(s),'custom':_plain(dict(s.items())),
        'animation':_animation(s),'drivers':_drivers(s)} for s in sorted(scenes,key=lambda x:x.name_full)],
        'limits':'Only declared dependencies; no acceptance for arbitrary Python reading undeclared global data.'}

def validate(rig,reference,regions,palette=None):
    palette=palette or PALETTE
    if rig is None or not isinstance(rig,bpy.types.Object) or rig.type!='ARMATURE':raise ValueError('NOT_READY：没有待验收骨架，禁止进入蒙皮')
    if rig.name not in bpy.context.scene.objects or rig.library or rig.data.library:raise ValueError('需要当前Scene内的本地骨架候选')
    if not len(rig.data.bones):raise ValueError('NOT_READY：空骨架')
    if reference is None or reference.type!='MESH' or reference.name not in bpy.context.scene.objects:raise ValueError('需要同Scene内的静态外形参照')
    if reference.parent or reference.animation_data or reference.data.shape_keys or len(reference.modifiers) or len(reference.vertex_groups):
        raise ValueError('质检参照须为无父骨架/动画/Key/修改器/权重组的静态模型')
    names=set(rig.data.bones.keys())
    if set(regions)!=names:raise ValueError('分区必须精确覆盖全部骨：缺少='+str(sorted(names-set(regions)))+' 多出='+str(sorted(set(regions)-names)))
    for name,region in regions.items():
        if region not in palette:raise ValueError('没有区域颜色：'+name)
        color=palette[region]['color']
        if len(color)!=3 or any(not math.isfinite(c) or c<0 or c>1 for c in color):raise ValueError('区域颜色无效：'+region)
        for owner in (rig.data.bones[name],rig.pose.bones[name]):
            if owner.color.palette!='CUSTOM' or max(abs(a-b) for a,b in zip(owner.color.custom.normal,_native_color(color)))>1e-6:
                raise ValueError('实际骨颜色未按分区配置，先在工作副本配色：'+name)
    for bone in rig.data.bones:
        values=list(bone.head_local)+list(bone.tail_local)+[x for row in bone.matrix_local for x in row]
        if not all(math.isfinite(x) for x in values) or bone.length<=0:raise ValueError('非有限或零长骨：'+bone.name)
    return palette

def apply_region_colors(rig,regions,palette=None):
    """Explicit authoring action; changes colors only and invalidates old identity."""
    palette=palette or PALETTE
    if set(regions)!=set(rig.data.bones.keys()):raise ValueError('分区未完整覆盖')
    for name,region in regions.items():
        color=_native_color(palette[region]['color'])
        for owner in (rig.data.bones[name],rig.pose.bones[name]):
            owner.color.palette='CUSTOM'
            owner.color.custom.normal=color
            owner.color.custom.select=tuple(min(1,c*.6+.4) for c in color)
            owner.color.custom.active=(1,1,1)
    rig['skeleton_quality_status']='PENDING_REVIEW'

def _identity_at_current_frame(rig,reference,regions,palette):
    vertices=np.empty(len(reference.data.vertices)*3,dtype=np.float32)
    reference.data.vertices.foreach_get('co',vertices)
    loops=np.empty(len(reference.data.loops),dtype=np.int32);reference.data.loops.foreach_get('vertex_index',loops)
    polygons=np.empty((len(reference.data.polygons),2),dtype=np.int32)
    for i,field in enumerate(('loop_start','loop_total')):
        column=np.empty(len(reference.data.polygons),dtype=np.int32);reference.data.polygons.foreach_get(field,column);polygons[:,i]=column
    return {'rig':_plain(rig),'rig_matrix':_plain(rig.matrix_world),'rig_custom':{k:_plain(v) for k,v in rig.items() if k!='skeleton_quality_status'},
      'rig_data_custom':_plain(dict(rig.data.items())),
      'bones':[{'name':b.name,'parent':b.parent.name if b.parent else None,'matrix':_plain(b.matrix_local),
        'head':list(b.head_local),'tail':list(b.tail_local),'rna':_rna(b),
        'bone_color':{'palette':b.color.palette,'normal':list(b.color.custom.normal),'select':list(b.color.custom.select),'active':list(b.color.custom.active)},
        'bbone_handles':{end:getattr(b,'bbone_custom_handle_'+end).name if getattr(b,'bbone_custom_handle_'+end,None) else None for end in ('start','end')},
        'pose_settings':{k:v for k,v in _rna(rig.pose.bones[b.name]).items() if k not in {'location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale'}},
        'custom':_plain(dict(rig.pose.bones[b.name].items())),
        'rigify_parameters':_rna(rig.pose.bones[b.name].rigify_parameters) if hasattr(rig.pose.bones[b.name],'rigify_parameters') else None,
        'constraints':_constraints(rig.pose.bones[b.name])} for b in rig.data.bones],
      'object_constraints':_constraints(rig),'object_drivers':_drivers(rig),'data_drivers':_drivers(rig.data),
      'animation':_animation(rig),'data_animation':_animation(rig.data),
      'reference':_plain(reference),'reference_matrix':_plain(reference.matrix_world),
      'reference_vertices_sha256':hashlib.sha256(vertices.tobytes()).hexdigest(),
      'reference_loops_sha256':hashlib.sha256(loops.tobytes()).hexdigest(),
      'reference_polygons_sha256':hashlib.sha256(polygons.tobytes()).hexdigest(),
      'external_dependencies':_external_dependencies(rig),
      'regions':regions,'palette':palette}

def identity(rig,reference,regions,palette):
    # Animated custom properties and object motion are compared at one canonical
    # frame. Ordinary timeline playback must not manufacture a stale receipt.
    scene=bpy.context.scene;frame,subframe=scene.frame_current,scene.frame_subframe
    runtime=_runtime_state(rig)
    try:
        scene.frame_set(1,subframe=0);bpy.context.view_layer.update()
        return _identity_at_current_frame(rig,reference,regions,palette)
    finally:
        scene.frame_set(frame,subframe=subframe);_restore_runtime(runtime)

def capture(rig,reference,regions,states,outdir,*,grid=9,palette=None):
    palette=validate(rig,reference,regions,palette)
    if grid not in (9,16):raise ValueError('只支持9宫格或16宫格')
    if not states or states[0].get('id')!='rest' or not states[0].get('rest'):raise ValueError('首个状态必须明确为rest')
    if len({s['id'] for s in states})!=len(states):raise ValueError('状态ID重复')
    if not any(not s.get('rest') for s in states):raise ValueError('仅静态图不能完成骨架质量检验；缺少实际动作状态')
    outdir=Path(outdir)
    if outdir.exists():raise FileExistsError('质检输出必须用新目录：'+str(outdir))
    outdir.mkdir(parents=True)
    scene=bpy.context.scene;frame=scene.frame_current;subframe=scene.frame_subframe;position=rig.data.pose_position
    runtime=_runtime_state(rig)
    fingerprint=identity(rig,reference,regions,palette)
    snapshots=[]
    try:
        for state in states:
            rig.data.pose_position='REST' if state.get('rest') else 'POSE'
            scene.frame_set(int(state['frame']),subframe=float(state.get('subframe',0)))
            bpy.context.view_layer.update()
            ev=rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
            bones=[]
            for pb in ev.pose.bones:
                matrix=ev.matrix_world@pb.matrix
                bones.append({'name':pb.name,'region':regions[pb.name],
                    'head_world':list(ev.matrix_world@pb.head),'tail_world':list(ev.matrix_world@pb.tail),
                    'matrix_world':_plain(matrix),'scale':list(matrix.to_scale()),
                    'bbone_segments':pb.bone.bbone_segments,
                    'segments_world':[_plain(matrix@pb.bbone_segment_matrix(i,rest=False))
                       for i in range(pb.bone.bbone_segments+1)] if pb.bone.bbone_segments>1 else []})
            snapshots.append({'id':state['id'],'frame':state['frame'],'rest':bool(state.get('rest')),'bones':bones})
    finally:
        rig.data.pose_position=position;scene.frame_set(frame,subframe=subframe);_restore_runtime(runtime)
    if digest(identity(rig,reference,regions,palette))!=digest(fingerprint):raise RuntimeError('采集期间骨架/参照身份发生变化')
    report={'schema':SCHEMA,'status':'PENDING_REVIEW','grid':grid,'identity':fingerprint,'identity_sha256':digest(fingerprint),
      'states':states,'snapshots':snapshots,'palette':palette,'views':[],
      'required_checks':list(CHECKS),'main_review':None,'independent_review':None,
      'limits':['结构诊断/图像存在均不自动证明解剖或运动质量','尚未签发蒙皮许可']}
    write_new(outdir/'capture.json',report)
    return report

def require_pass(rig,reference,regions,receipt_path,*,palette=None):
    """Consumption boundary: absent/stale/pending evidence always rejects."""
    palette=validate(rig,reference,regions,palette)
    receipt=json.loads(Path(receipt_path).read_text(encoding='utf8'))
    if receipt.get('schema')!=SCHEMA or receipt.get('status')!='PASS':raise ValueError('骨架质检未通过，禁止任何蒙皮操作')
    evidence=receipt.get('evidence_report')
    if not evidence or not Path(evidence['path']).is_file() or file_sha(evidence['path'])!=evidence['sha256']:
        raise ValueError('缺少或失效的完整质检证据报告')
    evidence_doc=json.loads(Path(evidence['path']).read_text(encoding='utf8'))
    if evidence_doc.get('status')!='PENDING_REVIEW' or evidence_doc.get('schema')!=SCHEMA:raise ValueError('质检证据格式无效')
    if evidence_doc.get('identity_sha256')!=receipt.get('identity_sha256'):raise ValueError('回执与实际质检报告身份不符')
    capture=evidence_doc.get('capture')
    if not capture or not Path(capture['path']).is_file() or file_sha(capture['path'])!=capture['sha256']:raise ValueError('原生骨骼/动作采集缺失或已改变')
    if receipt.get('identity_sha256')!=digest(identity(rig,reference,regions,palette)):raise ValueError('骨架质检回执已失效，先复验，禁止蒙皮')
    for role in ('main_review','independent_review'):
        review=receipt.get(role)
        if not review or not review.get('reviewer') or not review.get('evidence_seen'):raise ValueError('缺少实际看图/运动审核：'+role)
        if any(review.get('checks',{}).get(k)!='pass' for k in CHECKS):raise ValueError('骨架质量尚有失败或未验项：'+role)
    if receipt['main_review']['reviewer']==receipt['independent_review']['reviewer']:raise ValueError('主审与独审不得同人')
    artifacts=receipt.get('artifacts',[])
    if artifacts!=evidence_doc.get('artifacts'):raise ValueError('回执证据清单与已验报告不符')
    if not artifacts or any(not any(a.get('kind')==kind for a in artifacts) for kind in ('rest_grid','motion_grid','rest_reference_grid')):raise ValueError('缺少rest/运动骨骼拼图或静态参照叠图')
    for a in artifacts:
        if not Path(a['path']).is_file() or file_sha(a['path'])!=a['sha256']:raise ValueError('质检证据缺失或已改变')
        for source in a.get('native_sources',[]):
            if not Path(source['path']).is_file() or file_sha(source['path'])!=source['sha256']:raise ValueError('近景/参照叠图的原生来源缺失或已改变')
    return {'status':'PASS','identity_sha256':receipt['identity_sha256'],'scope':receipt.get('scope')}

def finalize_evidence(capture_path,gallery,mosaics,outfile,*,supplements=(),part=None):
    """Freeze render products without approving anatomy or motion quality."""
    capture_path=Path(capture_path)
    doc=json.loads(capture_path.read_text(encoding='utf8'))
    if doc.get('schema')!=SCHEMA or doc.get('status')!='PENDING_REVIEW':raise ValueError('需要真实待审采集')
    expected={s['id'] for s in doc['snapshots']}
    snapshots={s['id']:s for s in doc['snapshots']}
    if len(gallery)!=len(expected) or {g['state_id'] for g in gallery}!=expected or set(mosaics)!=expected:raise ValueError('rest/动作图组不完整')
    artifacts=[]
    for g in gallery:
        if g.get('source_snapshot_sha256')!=digest(snapshots[g['state_id']]):raise ValueError('渲染图组与实际骨架采集身份不符')
        if len(g['views'])!=doc['grid'] or len({v['name'] for v in g['views']})!=doc['grid']:raise ValueError('视角数量/唯一性不符')
        directions=[]
        for view in g['views']:
            if not view.get('camera_matrix'):raise ValueError('没有实际相机矩阵')
            if view.get('proxy_projection_bounds',{}).get('fits') is not True:raise ValueError('缺少完整显示几何的取景通过证据')
            matrix=np.asarray(view['camera_matrix'],dtype=np.float64)
            if matrix.shape!=(4,4) or not np.isfinite(matrix).all():raise ValueError('相机矩阵无效')
            direction=-matrix[:3,2];length=np.linalg.norm(direction)
            if length<=0:raise ValueError('相机方向无效')
            direction/=length
            if any(float(direction@old)>1-1e-6 for old in directions):raise ValueError('重复方向不能冒充多视角')
            directions.append(direction)
            path=Path(view['path'])
            if not path.is_file():raise FileNotFoundError(path)
            artifacts.append({'kind':'bone_view','state':g['state_id'],'view':view['name'],'path':str(path),'sha256':file_sha(path)})
        path=Path(mosaics[g['state_id']])
        artifacts.append({'kind':'rest_grid' if g['state_id']=='rest' else 'motion_grid',
           'state':g['state_id'],'path':str(path),'sha256':file_sha(path)})
    for supplement in supplements:
        if supplement['kind'] not in {'rest_reference_grid','bone_detail_grid'}:raise ValueError('未知补充质检证据类型')
        parent=supplement['parent_state']
        if parent not in snapshots or supplement['source_snapshot_sha256']!=digest(snapshots[parent]):raise ValueError('补充图与原生骨数据不匹配')
        if supplement['identity_sha256']!=doc['identity_sha256']:raise ValueError('补充图与骨架/参照身份不匹配')
        if supplement['kind']=='rest_reference_grid' and parent!='rest':raise ValueError('静态参照叠图只能用于rest定位')
        if supplement['kind']=='bone_detail_grid':
            selected=set(supplement.get('selected_bones',[]));known={b['name'] for b in snapshots[parent]['bones']}
            if not selected or not selected<=known:raise ValueError('近景骨选择无效')
        sources=supplement.get('native_sources',[])
        if not sources:raise ValueError('补充图缺少原生图像来源')
        for source in sources:
            if file_sha(source['path'])!=source['sha256']:raise ValueError('补充图原生来源已改变')
        row=dict(supplement);row['sha256']=file_sha(row['path']);artifacts.append(row)
    result={'schema':SCHEMA,'status':'PENDING_REVIEW','identity_sha256':doc['identity_sha256'],
       'capture':{'path':str(capture_path),'sha256':file_sha(capture_path)},
       'grid':doc['grid'],'gallery':gallery,'artifacts':artifacts,
       'required_checks':list(CHECKS),'quality_approved':False}
    if part is not None:
        if not isinstance(part,dict) or set(part)!={'id','plan'} or not re.fullmatch(r'[A-Za-z0-9_-]+',part['id']):raise ValueError('分包身份无效')
        binding=part['plan']
        if not isinstance(binding,dict) or set(binding)!={'path','sha256'} or file_sha(binding['path'])!=binding['sha256']:raise ValueError('分包计划缺失或已改变')
        plan=json.loads(Path(binding['path']).read_text(encoding='utf8'))
        expected_parts=[p for p in plan.get('packs',[]) if p.get('id')==part['id']]
        if len(expected_parts)!=1 or set(expected_parts[0]['states'])!=expected or expected_parts[0]['grid']!=doc['grid']:raise ValueError('分包状态/图组与计划不符')
        result.update(status='PENDING_PART_ONLY',evidence_part=part,
            limit='One evidence part, never a character PASS. record_review/require_pass reject this status.')
    write_new(outfile,result)
    return result

def record_review(evidence_path,main_review,independent_review,scope,outfile):
    """Only call after actual main/independent inspection; no automatic PASS."""
    evidence_path=Path(evidence_path);doc=json.loads(evidence_path.read_text(encoding='utf8'))
    if doc.get('schema')!=SCHEMA or doc.get('status')!='PENDING_REVIEW':raise ValueError('质检报告无效')
    for review in (main_review,independent_review):
        if not review.get('reviewer') or not review.get('evidence_seen') or not review.get('reason'):
            raise ValueError('必须记录实际审核者、已看证据及结论依据')
        if any(review.get('checks',{}).get(k)!='pass' for k in CHECKS):raise ValueError('存在未验或未通过项，不能签发PASS')
    if main_review['reviewer']==independent_review['reviewer']:raise ValueError('独审必须独立')
    if not scope:raise ValueError('必须限定角色/结构/用途/运动范围')
    if any(not any(a.get('kind')==kind for a in doc['artifacts']) for kind in ('rest_grid','motion_grid','rest_reference_grid')):
        raise ValueError('定位/运动的完整图像包不齐，禁止签发PASS')
    for artifact in doc['artifacts']:
        if file_sha(artifact['path'])!=artifact['sha256']:raise ValueError('图像证据已改变')
        for source in artifact.get('native_sources',[]):
            if file_sha(source['path'])!=source['sha256']:raise ValueError('补充图原生来源已改变')
    if file_sha(doc['capture']['path'])!=doc['capture']['sha256']:raise ValueError('原生骨数据采集已改变')
    receipt={'schema':SCHEMA,'status':'PASS','identity_sha256':doc['identity_sha256'],
      'evidence_report':{'path':str(evidence_path),'sha256':file_sha(evidence_path)},
      'main_review':main_review,'independent_review':independent_review,'scope':scope,'artifacts':doc['artifacts']}
    write_new(outfile,receipt)
    return receipt
