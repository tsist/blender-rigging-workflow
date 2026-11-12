"""Explicit accessory centerlines -> full reference copy -> same C94 FK/deform rig.

Native FK/Copy Transforms design reference: Rigify SimpleChainRig (GPL-2.0-or-
later); no upstream code, generator or second armature is incorporated.
Centerline parameter weights are not a volume solver and never choose a chain.
"""
import copy
import hashlib
import json
import math
import uuid

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.geometry import interpolate_bezier

from . import control_panel, generation, module_configuration, positioning
from . import corrective_authoring as sig, skinning_job

KEY = 'c94_chain_extensions_v1'
SCHEMA = 'c94.chain-extensions.v1'
KINDS = {'hair': '头发', 'skirt': '裙摆', 'generic': '附属'}


def _need(value, message):
    if not value:
        raise ValueError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def _finish(record):
    result = copy.deepcopy(record);result.pop('id', None);result['id'] = _digest(result)
    return result


def _record(value):
    _need(isinstance(value, dict) and value.get('schema') == SCHEMA and isinstance(value.get('chains'), list) and value['chains'], '扩展链记录无效')
    row = copy.deepcopy(value);identity = row.pop('id', None)
    _need(identity == _digest(row), '扩展链记录与id不一致')
    return copy.deepcopy(value)


def _context(scene, obj, kind):
    _need(scene == bpy.context.scene and bpy.context.mode == 'OBJECT', '链操作需要当前Scene的对象模式')
    _need(isinstance(obj, bpy.types.Object) and obj.type == kind and scene.objects.get(obj.name) == obj
          and bpy.context.view_layer.objects.get(obj.name) == obj and not obj.library and not obj.override_library
          and not obj.data.library and not obj.data.override_library and obj.is_editable and obj.data.is_editable, '需要当前视图层的本地独立对象：'+kind)
    a = np.asarray(obj.matrix_world, dtype=float)
    _need(np.isfinite(a).all() and abs(np.linalg.det(a[:3,:3])) > 1e-12, '对象世界矩阵必须有限且可逆')
    bpy.context.view_layer.update()


def _name(value):
    _need(isinstance(value, str) and value and len(value.encode('utf8')) <= 63, '骨/对象名称为空或超过63字节：'+str(value))
    return value


def _vector(value):
    a = np.asarray(value, dtype=float)
    _need(a.shape == (3,) and np.isfinite(a).all(), '坐标/方向必须为有限三维值')
    return Vector(a)


def _bones(obj, names=None):
    names = list(obj.data.bones.keys()) if names is None else names
    return [[n, sig._plain(obj.data.bones[n].matrix_local), obj.data.bones[n].length,
             obj.data.bones[n].parent.name if obj.data.bones[n].parent else None,
             obj.data.bones[n].use_connect, obj.data.bones[n].use_deform] for n in names]


def _base_rig(obj, names):
    return _digest({'bones':_bones(obj, names), 'pose':[[n, sig._rna(obj.pose.bones[n]), sig._plain(dict(obj.pose.bones[n].items())),
                    [sig._rna(c) for c in obj.pose.bones[n].constraints]] for n in names], 'drivers':sig._drivers(obj), 'world':sig._plain(obj.matrix_world)})


def _activate(obj):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.selected_objects:o.select_set(False)
    obj.hide_set(False);obj.select_set(True);bpy.context.view_layer.objects.active=obj


def _restore(active, selected):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.context.selected_objects):obj.select_set(False)
    for obj in selected:
        if bpy.context.view_layer.objects.get(obj.name) == obj:obj.select_set(True)
    bpy.context.view_layer.objects.active=active


def _resample(points, segments):
    values = np.asarray(points, dtype=float);lengths=np.linalg.norm(np.diff(values,axis=0),axis=1)
    _need(len(values)>=2 and np.isfinite(values).all() and np.all(lengths>1e-8), '中心线含退化或重复相邻点')
    cumulative=np.r_[0., np.cumsum(lengths)];targets=np.linspace(0., cumulative[-1], segments+1)
    return [[float(np.interp(t,cumulative,values[:,axis])) for axis in range(3)] for t in targets]


def _source_points(scene, source):
    _need(isinstance(source,dict), '每条链须明确source')
    kind=source.get('type')
    if kind=='POINTS':
        _need(source.get('space')=='WORLD', 'POINTS输入须明确WORLD空间')
        values=[list(_vector(p)) for p in source.get('points',[])]
        return values, {'type':kind,'space':'WORLD','points':values}
    obj=scene.objects.get(source.get('object',''));_context(scene,obj,'CURVE' if kind=='CURVE' else 'ARMATURE')
    _need(not obj.animation_data and not obj.data.animation_data and not obj.constraints, '源中心线只接静态对象')
    if kind=='CURVE':
        index=source.get('spline_index');_need(type(index) is int and 0<=index<len(obj.data.splines), '明确选择一个spline_index')
        spline=obj.data.splines[index];_need(not spline.use_cyclic_u and source.get('root') in {'FIRST','LAST'}, '开放Curve须明确FIRST/LAST根部')
        _need(not obj.modifiers, 'Curve输入先应用其几何修改器到独立中心线')
        if spline.type=='POLY':values=[p.co.xyz for p in spline.points]
        elif spline.type=='BEZIER':
            values=[];p=list(spline.bezier_points)
            for a,b in zip(p,p[1:]):values.extend(interpolate_bezier(a.co,a.handle_right,b.handle_left,b.co,17)[:-1])
            if p:values.append(p[-1].co)
        else:raise ValueError('当前中心线支持POLY/BEZIER；NURBS先转为独立POLY中心线')
        if source['root']=='LAST':values=list(reversed(values))
        points=[list(obj.matrix_world@p) for p in values]
        return points, {'type':kind,'object':obj.name,'data':obj.data.name,'spline_index':index,'root':source['root'],'world_points_sha256':_digest(points)}
    _need(kind=='BONES' and isinstance(source.get('bones'),list) and source['bones'], 'BONES须明确REST有序骨名列表')
    names=source['bones'];_need(len(set(names))==len(names) and all(n in obj.data.bones for n in names), '手工骨链存在缺骨或重复')
    bones=[obj.data.bones[n] for n in names]
    for a,b in zip(bones,bones[1:]):_need(b.parent==a and (b.head_local-a.tail_local).length<1e-6, '手工骨链必须按实际父链首尾连续排列')
    points=[list(obj.matrix_world@b.head_local) for b in bones]+[list(obj.matrix_world@bones[-1].tail_local)]
    return points, {'type':kind,'object':obj.name,'data':obj.data.name,'bones':names,'space':'REST_WORLD','world_points_sha256':_digest(points)}


def prepare_reference(scene, base_reference, specs, label, *, owned_ids=None):
    """Return a new full reference; inherited base approval is never extended."""
    module_configuration._own(owned_ids)
    owned = [] if owned_ids is None else owned_ids
    _context(scene,base_reference,'ARMATURE')
    _need(base_reference.parent is None and not base_reference.constraints and not base_reference.animation_data and not base_reference.data.animation_data, '完整参考须无父级、约束和动画')
    _need(KEY not in base_reference, '请从明确基底参考准备，已扩展参考用capture_reference同步')
    _need(isinstance(specs,list) and specs and isinstance(label,str) and label.strip(), '须明确链列表及新参考label')
    name=_name(label.strip()+'__完整链参考');_need(name not in bpy.data.objects and name not in bpy.data.armatures and name not in bpy.data.collections, '新完整参考名称已存在')
    before_sig=positioning.reference_signature(base_reference);base_names=list(base_reference.data.bones.keys());chains=[];ids=set();all_names=set(base_names)
    inverse=base_reference.matrix_world.inverted()
    for spec in specs:
        _need(isinstance(spec,dict) and set(spec)=={'id','kind','side','parent_bone','source','segments','group','roll_axis'}, '链spec字段不明确')
        identity=spec['id'];_name(identity);_need(identity not in ids and spec['kind'] in KINDS and spec['side'] in {'L','R','C'}, '链id重复或kind/side无效');ids.add(identity)
        _need(spec['parent_bone'] in base_reference.data.bones, '显式父骨不在基底参考：'+str(spec['parent_bone']))
        _need(type(spec['segments']) is int and 1<=spec['segments']<=256, '节数须为1–256整数')
        _name(spec['group']);world,provenance=_source_points(scene,spec['source'])
        points=_resample([list(inverse@_vector(p)) for p in world],spec['segments']);axis=_vector(spec['roll_axis']);_need(axis.length>1e-8,'roll_axis不可为零');axis.normalize()
        for a,b in zip(points,points[1:]):_need((_vector(b)-_vector(a)).normalized().cross(axis).length>1e-5, 'roll_axis与链段平行；明确其它滚转方向')
        suffix='.'+spec['side'] if spec['side']!='C' else ''
        names={role:[_name(prefix+identity+'-'+str(k+1).zfill(2)+suffix) for k in range(spec['segments'])] for role,prefix in [('reference','参考-'),('control','控制-'),('deform','形变-')]}
        for role_names in names.values():
            _need(not all_names.intersection(role_names), '扩展链名称冲突');all_names.update(role_names)
        chains.append({k:copy.deepcopy(spec[k]) for k in ['id','kind','side','parent_bone','segments','group']}|{'points_local':points,'roll_axis_local':list(axis),'source':provenance,'names':names})
    before=module_configuration._ids();active=bpy.context.view_layer.objects.active;selected=list(bpy.context.selected_objects)
    try:
        collection=module_configuration._own(owned,bpy.data.collections.new(name));scene.collection.children.link(collection)
        ref=module_configuration._own(owned,base_reference.copy());ref.data=module_configuration._own(owned,base_reference.data.copy());ref.name=name;ref.data.name=name;collection.objects.link(ref);_activate(ref)
        mirror=ref.data.use_mirror_x;ref.data.use_mirror_x=False;bpy.ops.object.mode_set(mode='EDIT')
        for chain in chains:
            for i,bone_name in enumerate(chain['names']['reference']):
                bone=ref.data.edit_bones.new(bone_name);bone.head=chain['points_local'][i];bone.tail=chain['points_local'][i+1];bone.align_roll(Vector(chain['roll_axis_local']));bone.use_deform=False
                bone.parent=ref.data.edit_bones[chain['names']['reference'][i-1] if i else chain['parent_bone']];bone.use_connect=bool(i)
        bpy.ops.object.mode_set(mode='OBJECT');ref.data.use_mirror_x=mirror
        coll=ref.data.collections.new('新增链参考')
        for chain in chains:
            for n in chain['names']['reference']:
                bone=ref.data.bones[n]
                for previous in list(bone.collections):previous.unassign(bone)
                coll.assign(bone)
        record=_finish({'schema':SCHEMA,'base':{'object':base_reference.name,'data':base_reference.data.name,'signature':before_sig,'owner':base_reference.get(positioning.OWNER_KEY),'bones':_bones(base_reference),'world':sig._plain(base_reference.matrix_world)},
             'reference':{'object':ref.name,'data':ref.data.name,'signature':positioning.reference_signature(ref)},'chains':chains,
             'approval':{'base':'inherited_not_reapproved','extension':'deferred_technical_candidate'},'enabled':{kind:any(c['kind']==kind for c in chains) for kind in KINDS},'dynamics':'P4_not_implemented'})
        ref[KEY]=_json(record)
        _need(positioning.reference_signature(base_reference)==before_sig and _bones(ref,base_names)==record['base']['bones'], '基底参考保护失败')
        return {'reference':ref,'collection':collection,'record':record}
    except Exception:
        module_configuration._cleanup(before,active,selected,owned_ids=owned);raise
    finally:_restore(active,selected)


def capture_reference(reference, record):
    """Explicitly capture edited extension rest frames while protecting base."""
    row=_record(record);_context(bpy.context.scene,reference,'ARMATURE')
    _need(reference.name==row['reference']['object'] and reference.data.name==row['reference']['data'], '参考身份已改变')
    _need(sig._plain(reference.matrix_world)==row['base']['world'] and _bones(reference,[r[0] for r in row['base']['bones']])==row['base']['bones'], '基底骨或世界框架改变；不在链手调域')
    expected={r[0] for r in row['base']['bones']}|{n for c in row['chains'] for n in c['names']['reference']}
    _need(set(reference.data.bones.keys())==expected,'完整参考含未知增删骨')
    for chain in row['chains']:
        bones=[reference.data.bones[n] for n in chain['names']['reference']]
        for i,b in enumerate(bones):
            _need(b.parent and b.parent.name==(bones[i-1].name if i else chain['parent_bone']) and b.length>1e-8 and not b.use_deform, '扩展参考父链/长度/非形变身份改变')
            if i:_need((b.head_local-bones[i-1].tail_local).length<1e-6,'扩展参考链不连续')
        chain['points_local']=[list(b.head_local) for b in bones]+[list(bones[-1].tail_local)]
        chain['frames_local']=[sig._plain(b.matrix_local) for b in bones]
    row['reference']['signature']=positioning.reference_signature(reference);row=_finish(row);reference[KEY]=_json(row);return row


def _validated(reference, record):
    row=_record(record)
    _need(reference.name==row['reference']['object'] and reference.data.name==row['reference']['data'] and positioning.reference_signature(reference)==row['reference']['signature'], '完整链参考已改；先capture_reference显式同步')
    _need(json.loads(reference.get(KEY,'null'))==row,'参考内扩展记录与提交不一致')
    return row


def validate_reference(scene, reference, record):
    """No scene pointer/approval edits; verify both base and full extension."""
    _context(scene,reference,'ARMATURE');row=_validated(reference,record)
    base=scene.objects.get(row['base']['object']);_context(scene,base,'ARMATURE')
    _need(base.data.name==row['base']['data'] and positioning.reference_signature(base)==row['base']['signature']
          and base.get(positioning.OWNER_KEY)==row['base']['owner'],'原完整参考身份/骨位/owner已改变')
    _need(_bones(reference,[b[0] for b in row['base']['bones']])==row['base']['bones']
          and sig._plain(reference.matrix_world)==row['base']['world'],'扩展副本中的基底静置或世界框架已改变')
    _need(row['approval']=={'base':'inherited_not_reapproved','extension':'deferred_technical_candidate'},'扩展记录不能伪造用户效果批准')
    return row


def install(rig, reference, record, *, owned_ids=None):
    """Append FK controls and deform chain to the one existing C94 armature."""
    module_configuration._own(owned_ids)
    owned = [] if owned_ids is None else owned_ids
    _context(bpy.context.scene,rig,'ARMATURE');row=validate_reference(bpy.context.scene,reference,record)
    _need(rig.data.users==1 and KEY not in rig, '同一rig只接一次明确扩展，且Armature数据不可共享')
    _need(rig.visible_get(view_layer=bpy.context.view_layer) and not rig.hide_select,'安装目标rig须在当前视图层可见可选；不改隐藏状态')
    metadata=json.loads(rig.get(generation.RIG_KEY,'null'))
    _need(isinstance(metadata,dict) and metadata.get('reference_signature')==positioning.reference_signature(reference) and metadata.get('reference_object',metadata.get('reference'))==reference.name,'生成rig不属于当前完整链参考')
    from . import neutral_binding
    has_neutral_baseline=neutral_binding.KEY in metadata
    if has_neutral_baseline:
        neutral_binding.check_current(rig,metadata,metadata['deforms'],float(bpy.context.scene.unit_settings.scale_length))
    _need(np.max(abs(np.asarray(rig.matrix_world)-np.asarray(reference.matrix_world)))<1e-7,'生成rig与完整参考世界框架不一致')
    reverse={v:k for k,v in metadata['reference_mapping'].items()};_need(len(reverse)==len(metadata['reference_mapping']),'原生成语义映射不唯一')
    names=[n for c in row['chains'] for key in ['control','deform'] for n in c['names'][key]]
    _need(not set(names)&set(rig.data.bones.keys()),'扩展输出与现有rig骨重名')
    for chain in row['chains']:
        _need(chain['parent_bone'] in reverse,'显式父语义无保留形变输出；请先明确主父骨：'+chain['parent_bone'])
        parent=rig.data.bones[reverse[chain['parent_bone']]];pose=generation._update(rig).pose.bones[parent.name]
        _need(parent.use_deform,'显式父语义的实际生成输出已不再是形变骨')
        delta=pose.matrix@parent.matrix_local.inverted()
        _need(max(abs(delta[i][j]-(1 if i==j else 0)) for i in range(4) for j in range(4))<=1e-5,'真实父骨含未校准绑定/shear偏差，不能直接安装FK链')
    before=module_configuration._ids();active=bpy.context.view_layer.objects.active;selected=list(bpy.context.selected_objects);base_names=list(rig.data.bones.keys());base_contract=_base_rig(rig,base_names)
    old_marker=rig[generation.RIG_KEY];old_layout=rig.get(control_panel.LAYOUT_KEY);old_collections=set(c.name for c in rig.data.collections_all);mirror=rig.data.use_mirror_x
    try:
        _activate(rig);rig.data.use_mirror_x=False;bpy.ops.object.mode_set(mode='EDIT')
        for chain in row['chains']:
            for role in ['control','deform']:
                for i,n in enumerate(chain['names'][role]):
                    ref=reference.data.bones[chain['names']['reference'][i]];bone=rig.data.edit_bones.new(n);bone.length=ref.length;bone.matrix=ref.matrix_local
                    bone.use_deform=(role=='deform');bone.parent=rig.data.edit_bones[chain['names'][role][i-1] if i else reverse[chain['parent_bone']]];bone.use_connect=bool(i)
        bpy.ops.object.mode_set(mode='OBJECT');rig.data.use_mirror_x=mirror
        shapes=module_configuration._own(owned,bpy.data.collections.new('链控件_'+uuid.uuid4().hex[:12]));bpy.context.scene.collection.children.link(shapes);shapes.hide_render=True
        mesh=module_configuration._own(owned,bpy.data.meshes.new('链FK控件_'+uuid.uuid4().hex[:12]));mesh.from_pydata([(-.3,0,0),(0,.7,0),(.3,0,0),(0,0,.3)],[(0,1),(1,2),(2,3),(3,0)],[])
        widget=module_configuration._own(owned,bpy.data.objects.new(mesh.name,mesh));shapes.objects.link(widget);widget.hide_render=True;widget.hide_set(True)
        layout=control_panel.read_layout(rig);installed=[]
        for chain in row['chains']:
            cname=_name(KINDS[chain['kind']]+'链-'+chain['group']);_need(cname not in old_collections,'扩展分组与原集合重名')
            coll=rig.data.collections.get(cname) or rig.data.collections.new(cname)
            deform_coll=rig.data.collections.get('新增链形变') or rig.data.collections.new('新增链形变');deform_coll.is_visible=False
            for ctrl,deform,ref in zip(chain['names']['control'],chain['names']['deform'],chain['names']['reference']):
                pb=rig.pose.bones[ctrl];pb.rotation_mode='XYZ';pb.lock_location=(True,True,True);pb.lock_scale=(True,True,True);pb.custom_shape=widget
                pb.bone['c94_accessory_control_v1']=chain['id']
                # New EditBones can inherit the active collection. Exclusive
                # own membership makes independent visibility real, while the
                # persisted marker + RIG_KEY gives shared panel eligibility.
                for previous in list(pb.bone.collections):previous.unassign(pb.bone)
                coll.assign(pb.bone)
                db=rig.data.bones[deform]
                for previous in list(db.collections):previous.unassign(db)
                deform_coll.assign(db);db.hide_select=True
                constraint=rig.pose.bones[deform].constraints.new('COPY_TRANSFORMS');constraint.name='显式链FK';constraint.target=rig;constraint.subtarget=ctrl;constraint.owner_space='POSE';constraint.target_space='POSE'
                metadata['deforms'].append(deform);metadata['reference_mapping'][deform]=ref;metadata['mechanism_mapping'][deform]=ctrl
                layout['defaults'][ctrl]=control_panel._defaults(pb)
            if not any(r.get('kind')=='collection' and r.get('collection')==cname for r in layout['rows']):layout['rows'].append({'id':uuid.uuid4().hex,'kind':'collection','group':KINDS[chain['kind']]+'链','label':chain['group'],'visible':True,'collection':cname})
            installed.append({'id':chain['id'],'parent_output':reverse[chain['parent_bone']],'controls':chain['names']['control'],'deforms':chain['names']['deform'],'collection':cname})
        bpy.context.view_layer.update();neutral=generation.verify_neutral(rig,reference,metadata['deforms'],metadata['reference_mapping'])
        _need(neutral['sample_max']<=1e-5 and neutral['head_tail_max']<=1e-5,'完整rig新增链后真实中立校验失败')
        _need(_base_rig(rig,base_names)==base_contract,'链安装改变了基底静置/约束/驱动/姿态')
        if has_neutral_baseline:
            # This verified generation-extension transaction seals its new
            # output set. Ordinary reads and binding checks never recapture.
            metadata[neutral_binding.KEY]=neutral_binding.capture_verified(
                rig,metadata,neutral,metadata[neutral_binding.KEY]['verified']['mechanism_max'],
                float(bpy.context.scene.unit_settings.scale_length))
        # Native EditBone matrices can round differently from the reference
        # through a deep parent frame. Identity is the actual, neutral-verified
        # written rest state, not a second tighter ideal-matrix tolerance.
        metadata['chain_extensions']=row
        metadata['chain_installation']={'record_id':row['id'],'rest_digest':_digest(_bones(rig,names))}
        rig[generation.RIG_KEY]=_json(metadata);control_panel._save(rig,layout);rig[KEY]=_json(row)
        return {'schema':'c94.chain-install.v1','record_id':row['id'],'chains':installed,'neutral':neutral,'base_unchanged':True,'effect':'deferred','dynamics':'P4_not_implemented'}
    except Exception:
        _activate(rig);bpy.ops.object.mode_set(mode='EDIT')
        for n in names:
            if n in rig.data.edit_bones:rig.data.edit_bones.remove(rig.data.edit_bones[n])
        bpy.ops.object.mode_set(mode='OBJECT');rig.data.use_mirror_x=mirror
        for c in list(rig.data.collections_all):
            if c.name not in old_collections:rig.data.collections.remove(c)
        rig[generation.RIG_KEY]=old_marker
        if old_layout is not None:rig[control_panel.LAYOUT_KEY]=old_layout
        elif control_panel.LAYOUT_KEY in rig:del rig[control_panel.LAYOUT_KEY]
        if KEY in rig:del rig[KEY]
        module_configuration._cleanup(before,active,selected,owned_ids=owned);raise
    finally:_restore(active,selected)


def _parameter(point, points, maximum):
    """Closest parameter on the EXPLICIT one centerline, never choose a bone/chain."""
    p=np.asarray(point,dtype=float);v=np.asarray(points,dtype=float);best=None
    lengths=np.linalg.norm(np.diff(v,axis=0),axis=1);cum=np.r_[0.,np.cumsum(lengths)]
    for i,(a,b) in enumerate(zip(v,v[1:])):
        t=float(np.clip(np.dot(p-a,b-a)/np.dot(b-a,b-a),0,1));distance=float(np.linalg.norm(p-(a+t*(b-a))))
        if best is None or distance<best[0]:best=(distance,float((cum[i]+t*lengths[i])/cum[-1]))
    _need(best[0]<=maximum,'显式中心线距离超出界限；不换链或猜其它骨')
    return best[1]


def _segment_weights(parameter, points, names):
    """Piecewise linear field at actual WORLD arc-length segment centers."""
    values=np.asarray(points,dtype=float);lengths=np.linalg.norm(np.diff(values,axis=0),axis=1)
    _need(len(names)==len(lengths) and len(names)>0 and np.isfinite(lengths).all() and np.all(lengths>1e-8),'世界中心线有退化段')
    cumulative=np.r_[0.,np.cumsum(lengths)];centers=(cumulative[:-1]+cumulative[1:])/(2*cumulative[-1])
    if parameter<=centers[0]:return {names[0]:1.}
    if parameter>=centers[-1]:return {names[-1]:1.}
    right=int(np.searchsorted(centers,parameter,side='right'));left=right-1
    factor=float((parameter-centers[left])/(centers[right]-centers[left]))
    return {names[left]:1-factor,names[right]:factor}


def bind_copies(scene, rig, record, bindings, label, *, owned_ids=None):
    """Explicit Mesh/vertex partition to a chain; preserve all unselected weights.

    part={chain,vertex_ids,method:CENTERLINE,max_distance_m} or
         {chain,vertex_ids,method:PARAMETERS,parameters:[0..1]} or
         {chain,vertex_ids,method:RIGID,segment:int}.
    CENTERLINE is a documented thin-strip field, not volumetric skinning.
    """
    module_configuration._own(owned_ids)
    owned = [] if owned_ids is None else owned_ids
    _context(scene,rig,'ARMATURE');row=_record(record);_need(json.loads(rig.get(KEY,'null'))==row,'rig链记录不匹配')
    reference=scene.objects.get(row['reference']['object']);validate_reference(scene,reference,row)
    metadata=json.loads(rig.get(generation.RIG_KEY,'null'))
    _need(isinstance(metadata,dict) and metadata.get('chain_extensions')==row
          and set(metadata.get('deforms',[]))==set(metadata.get('reference_mapping',{})), '生成rig扩展/形变记录已改变')
    installed=metadata.get('chain_installation',{});names=[n for c in row['chains'] for key in ['control','deform'] for n in c['names'][key]]
    _need(all(n in rig.data.bones for n in names) and installed.get('record_id')==row['id']
          and installed.get('rest_digest')==_digest(_bones(rig,names)), '实际链rest已改变；请在完整参考回改后重建')
    for chain in row['chains']:
        for ctrl,deform,ref in zip(chain['names']['control'],chain['names']['deform'],chain['names']['reference']):
            _need(ctrl in rig.pose.bones and deform in rig.pose.bones and rig.data.bones[deform].use_deform
                  and not rig.data.bones[ctrl].use_deform and metadata['reference_mapping'].get(deform)==ref, '实际链骨或语义映射已改变')
            constraints=list(rig.pose.bones[deform].constraints)
            _need(len(constraints)==1 and constraints[0].type=='COPY_TRANSFORMS' and constraints[0].target==rig
                  and constraints[0].subtarget==ctrl and constraints[0].influence==1 and not constraints[0].mute
                  and constraints[0].target_space=='POSE' and constraints[0].owner_space=='POSE', '实际链约束已改变')
    _need(isinstance(bindings,list) and bindings,'明确绑定物件列表');_name(label)
    chains={c['id']:c for c in row['chains']};scale=float(scene.unit_settings.scale_length);_need(math.isfinite(scale) and scale>0,'无效单位比例')
    from . import neutral_binding
    if neutral_binding.KEY in metadata:
        neutral_binding.check_current(rig,metadata,metadata['deforms'],scale)
    plans=[];used_objects=set()
    for i,binding in enumerate(bindings):
        obj=scene.objects.get(binding.get('object',''));_context(scene,obj,'MESH');_need(obj.name not in used_objects,'绑定物件重复');used_objects.add(obj.name)
        _need(obj.data.shape_keys is None and not obj.animation_data and not obj.data.animation_data and not obj.constraints, '链绑定副本只接无Key/driver/Action/对象约束的Mesh')
        mods=list(obj.modifiers);arms=[m for m in mods if m.type=='ARMATURE'];_need(len(arms)<=1 and all(m.type in {'ARMATURE','SUBSURF'} for m in mods)
            and (not arms or mods[0]==arms[0]),'附件修改器只支持首位普通Armature及其后的Subsurf')
        for m in arms:_need(m.object and m.use_vertex_groups and not m.use_bone_envelopes and not m.use_deform_preserve_volume and not m.vertex_group and not m.use_multi_modifier,'附件原绑定不是普通组LBS')
        _need(obj.parent is None or (arms and obj.parent==arms[0].object and obj.parent_type=='OBJECT'),'附件父级不在明确旧rig范围')
        managed={k for k in obj.keys() if str(k).startswith('c94_')};_need(managed<= {'c94_skinning_v1','c94_skinning_policy_v1','c94_production_input_v1'} and not any(str(k).startswith('c94_') for k in obj.data.keys()),'附件含未知/活动受管身份')
        name=_name(label+'__链绑定_'+str(i+1).zfill(2));_need(name not in bpy.data.objects and name+'__网格' not in bpy.data.meshes and len((name+'__网格').encode('utf8'))<=63,'附件副本名称冲突')
        all_ids=set();weights={};parts=binding.get('parts');_need(isinstance(parts,list) and parts,'每个附件明确部件分区')
        for part in parts:
            chain=chains.get(part.get('chain'));_need(chain is not None,'部件须指定唯一已安装chain id');ids=part.get('vertex_ids')
            _need(isinstance(ids,list) and ids and len(set(ids))==len(ids) and all(type(k) is int and 0<=k<len(obj.data.vertices) for k in ids) and not all_ids.intersection(ids),'部件点域重复/越界或跨链重叠');all_ids.update(ids)
            method=part.get('method');segments=chain['segments']
            world=[list(rig.matrix_world@Vector(p)) for p in chain['points_local']]
            if method=='PARAMETERS':
                values=part.get('parameters');_need(isinstance(values,list) and len(values)==len(ids) and all(type(v) in (float,int) and math.isfinite(v) and 0<=v<=1 for v in values),'逐点参数须与点域等长且在0–1')
            elif method=='CENTERLINE':
                maximum=part.get('max_distance_m');_need(type(maximum) in (int,float) and math.isfinite(maximum) and maximum>0,'CENTERLINE须明确max_distance_m')
                values=[_parameter(obj.matrix_world@obj.data.vertices[k].co,world,maximum/scale) for k in ids]
            else:
                _need(method=='RIGID' and type(part.get('segment')) is int and 0<=part['segment']<segments,'RIGID须明确段索引')
                values=[None]*len(ids)
            for k,t in zip(ids,values):
                if method=='RIGID':weights[k]={chain['names']['deform'][part['segment']]:1.}
                else:weights[k]=_segment_weights(t,world,chain['names']['deform'])
        _need(not arms or arms[0].object==rig or len(all_ids)==len(obj.data.vertices),'更换旧rig的附件必须明确覆盖全Mesh；部分点域仅接同rig')
        old_deforms={b.name for b in arms[0].object.data.bones if b.use_deform} if arms else set()
        # With no old Armature, a leftover group can still become a live body
        # influence when the NEW modifier is added. Retire those selected-point
        # influences explicitly too; keep ordinary masks and unselected rows.
        affected=old_deforms|set(metadata['deforms'])
        _need(not any(g.lock_weight and g.name in affected for g in obj.vertex_groups),'受影响组已锁定')
        for mod in mods:
            for prop in mod.bl_rna.properties:
                if prop.type=='STRING' and 'vertex_group' in prop.identifier:_need(getattr(mod,prop.identifier) not in affected,'修改器mask消费受影响组')
        plans.append({'source':obj,'name':name,'weights':weights,'affected':affected,'managed':managed,'state':_mesh_state(obj),'points':skinning_job._points(obj),'parts':copy.deepcopy(parts)})
    before=module_configuration._ids();active=bpy.context.view_layer.objects.active;selected=list(bpy.context.selected_objects)
    old_rig_marker=rig[generation.RIG_KEY]
    try:
        collection=module_configuration._own(owned,bpy.data.collections.new(label+'__链绑定副本'));scene.collection.children.link(collection);objects=[];reports=[]
        for plan in plans:
            source=plan['source'];obj=module_configuration._own(owned,source.copy());obj.data=module_configuration._own(owned,source.data.copy());obj.name=plan['name'];obj.data.name=plan['name']+'__网格';collection.objects.link(obj)
            world=source.matrix_world.copy()
            if source.parent:obj.parent=rig;obj.parent_type='OBJECT';obj.parent_bone=''
            obj.matrix_world=world
            arms=[m for m in obj.modifiers if m.type=='ARMATURE']
            arm=arms[0] if arms else obj.modifiers.new('显式附件链','ARMATURE');arm.object=rig
            if not arms:
                _activate(obj)
                while obj.modifiers[0]!=arm:bpy.ops.object.modifier_move_up(modifier=arm.name)
            for group in obj.vertex_groups:
                if group.name in plan['affected']:group.remove(list(plan['weights']))
            for index,weights in plan['weights'].items():
                for n,w in weights.items():
                    if w>0:(obj.vertex_groups.get(n) or obj.vertex_groups.new(name=n)).add([index],float(np.float32(w)),'REPLACE')
            for k in plan['managed']:del obj[k]
            obj['c94_skinning_v1']=_json({'schema':'c94.skinning.v1','rig':rig.name,'source':source.name,'method':'explicit_accessory_centerline_parameter','chain_record':row['id'],'parts':plan['parts'],'effect':'deferred','not_volume_solver':True})
            bpy.context.view_layer.update();actual=skinning_job._points(obj);error=float(np.linalg.norm(actual-plan['points'],axis=1).max(initial=0))*scale
            _need(actual.shape==plan['points'].shape and error<=1e-5,'附件绑定中立世界表面改变')
            _need(_mesh_state(source)==plan['state'],'附件源或权重改变')
            original=_weight_rows(source);observed=_weight_rows(obj)
            for index,(a,b) in enumerate(zip(original,observed)):
                if index not in plan['weights']:_need(a==b,'未选点权重改变')
                else:
                    expected={n:w for n,w in a.items() if n not in plan['affected']};expected.update({n:float(np.float32(w)) for n,w in plan['weights'][index].items() if w>0});_need(expected==b,'选定点写回或非目标组不一致')
            _need(sig._mesh_signature(obj.data)==sig._mesh_signature(source.data),'附件几何/UV/拓扑改变')
            positive=sum(any(n in metadata['deforms'] and w>0 for n,w in row_weights.items()) for row_weights in observed)
            objects.append(obj);reports.append({'source':source.name,'object':obj.name,'selected_vertices':len(plan['weights']),
                'total_vertices':len(observed),'unselected_vertices':len(observed)-len(plan['weights']),
                'positive_deform_vertices':positive,'unweighted_deform_vertices':len(observed)-positive,
                'status':'partial_draft' if positive<len(observed) else 'technical_candidate_bound',
                'write_domain':'full_mesh' if len(plan['weights'])==len(observed) else 'explicit_partial_vertices',
                'neutral_surface_error_meters':error,'unselected_weights_exact':True,'source_unchanged':True,'parts':plan['parts']})
        stage=metadata.get('accessory_binding',{'schema':'c94.accessory-binding-state.v1','copies':[]})
        _need(isinstance(stage,dict) and stage.get('schema')=='c94.accessory-binding-state.v1' and isinstance(stage.get('copies'),list),'已有附件绑定阶段记录无效')
        stage=copy.deepcopy(stage);stage.update(status='technical_candidates_bound',chain_record_id=row['id'],body_skinning_state=metadata.get('skinning','unknown'),effect='deferred')
        stage['copies'].extend([{k:r[k] for k in ['object','selected_vertices','total_vertices','unweighted_deform_vertices','status','write_domain']} for r in reports])
        metadata['accessory_binding']=stage;rig[generation.RIG_KEY]=_json(metadata)
        return {'objects':objects,'collection':collection,'report':{'schema':'c94.chain-binding.v1','record_id':row['id'],'meshes':reports,'effect':'deferred','not_volume_solver':True}}
    except Exception:
        rig[generation.RIG_KEY]=old_rig_marker
        module_configuration._cleanup(before,active,selected,owned_ids=owned);raise
    finally:_restore(active,selected)


def _weight_rows(obj):
    names=[g.name for g in obj.vertex_groups]
    return [{names[g.group]:float(g.weight) for g in v.groups} for v in obj.data.vertices]


def _mesh_state(obj):
    return _digest({'geometry':sig._mesh_signature(obj.data),'weights':_weight_rows(obj),'groups':[[g.name,g.lock_weight] for g in obj.vertex_groups],
                    'proxy':skinning_job._proxy_state(obj),'properties':sig._plain(dict(obj.items()))})
