# SPDX-License-Identifier: GPL-3.0-or-later
"""Non-deforming controls in the same rig; shared sidebar/native bone input.

Only static or plain LINEAR/CONSTANT hub channels migrate. Old Actions and
unrelated curves are retained. Labels/rails are native bone-parented objects.
"""
from contextlib import contextmanager
import json, math, uuid
import bpy
from mathutils import Vector, Matrix
from . import face_expression_controls as core, face_modes as fm, mode_animation as anim
from . import face_graph_core as daily
from . import face_expression_core as expression_core

STATE_KEY=daily.STATE_KEY
_require=core._require
_read=daily._read
set_values=daily.set_values
set_layout=daily.set_layout
switch=daily.switch
record_current=daily.record_current
_no_target_animation=daily._no_target_animation


def _display_points(obj, depsgraph=None):
    """Actual evaluated CURVE/FONT vertices in world space, never loose bounds.

    This Blender build can expand legacy Curve.bound_box by about one unit on
    every side while its visible bevel geometry is millimetres wide.
    """
    name = getattr(obj, 'name', '<missing>')
    _require(isinstance(obj, bpy.types.Object) and obj.type in {'CURVE', 'FONT'},
             '需要真实CURVE/FONT显示对象：' + name)
    depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
        _require(mesh is not None and len(mesh.vertices) > 0, '没有可用的实际显示几何：' + name)
        points = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
        _require(all(math.isfinite(value) for point in points for value in point),
                 '实际显示几何含非有限坐标：' + name)
        return points
    except Exception as error:
        raise ValueError('无法读取实际显示几何“' + name + '”：' + str(error)) from error
    finally:
        evaluated.to_mesh_clear()


@contextmanager
def _edit(rig):
    _require(bpy.context.mode in {'OBJECT','POSE'}, '先退出编辑/绘制模式')
    active=bpy.context.view_layer.objects.active;selected=list(bpy.context.selected_objects);mode=bpy.context.mode;hidden=rig.hide_get()
    if mode=='POSE':bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.selected_objects:o.select_set(False)
    rig.hide_set(False);rig.select_set(True);bpy.context.view_layer.objects.active=rig
    bpy.ops.object.mode_set(mode='EDIT')
    try:yield rig.data.edit_bones
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        for o in bpy.context.selected_objects:o.select_set(False)
        for o in selected:
            if o.name in bpy.context.view_layer.objects:o.select_set(True)
        bpy.context.view_layer.objects.active=active
        if mode=='POSE' and active is not None:bpy.ops.object.mode_set(mode='POSE')
        rig.hide_set(hidden)


def _bone_path(name):return 'pose.bones['+json.dumps(name,ensure_ascii=False)+'].location'


def _parent(obj,rig,bone,offset=(0,0,0)):
    obj.parent=rig;obj.parent_type='BONE';obj.parent_bone=bone
    obj.matrix_parent_inverse=Matrix.Translation((0,-rig.data.bones[bone].length,0));obj.location=offset


def _visibility(obj,rig,collection):
    for field in ['hide_viewport','hide_render']:
        d=obj.driver_add(field).driver;d.type='SCRIPTED';d.expression='not (a and b)'
        for name,col in [('a',fm.SPACES['panel']),('b',collection)]:
            v=d.variables.new();v.name=name;v.type='SINGLE_PROP';v.targets[0].id_type='ARMATURE';v.targets[0].id=rig.data
            v.targets[0].data_path='collections_all['+json.dumps(col,ensure_ascii=False)+'].is_visible'


def _shape(label,verts,edges,rig,bone,collection,objects,*,font=None,text=None,color=(.25,.7,.9,1)):
    if text is None:
        mesh=bpy.data.meshes.new(label);mesh.from_pydata(verts,edges,[]);mesh.update()
        widget=bpy.data.objects.new(label+'_widget',mesh);collection.objects.link(widget);objects.append(widget)
        widget.hide_render=True;widget.hide_set(True)
        pb=rig.pose.bones[bone];pb.custom_shape=widget;pb.use_custom_shape_bone_size=False
        data=bpy.data.curves.new(label+'_display','CURVE');data.dimensions='3D';data.bevel_depth=.00065;data.bevel_resolution=0
        for a,b in edges:
            spline=data.splines.new('POLY');spline.points.add(1)
            spline.points[0].co=(*verts[a],1);spline.points[1].co=(*verts[b],1)
    else:
        data=bpy.data.curves.new(label,'FONT');data.body=text;data.size=.012
        if font is not None:data.font=font
    obj=bpy.data.objects.new(label+'_图形',data);collection.objects.link(obj);objects.append(obj);obj.color=color;obj.hide_select=True
    _parent(obj,rig,bone);_visibility(obj,rig,collection.name)
    return obj


def _rect(x0,y0,x1,y1):
    return [(x0,y0,0),(x1,y0,0),(x1,y1,0),(x0,y1,0)],[(0,1),(1,2),(2,3),(3,0)]


def _destroy(objects):
    for obj in reversed(objects):
        data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
        if data is not None and data.users==0:
            if isinstance(data,bpy.types.Mesh):bpy.data.meshes.remove(data)
            elif isinstance(data,bpy.types.Curve):bpy.data.curves.remove(data)


def _restore_inputs(rig,before):
    for n,v in before['bones'].items():
        pb=rig.pose.bones[n]
        for f in anim.pose._CHANNELS:setattr(pb,f,v[f])
        pb.rotation_mode=v['rotation_mode']
    for (n,k),v in before['modes'].items():rig.pose.bones[n][k]=v
    for f,v in before['object'].items():setattr(rig,f,v)
    for (p,i),v in before['animated'].items():anim._restore_value(rig,p,i,v)


def create(hub,*,travel=.055,font=None):
    group,rig,members=core._read(hub);core._baseline(rig,group['neutral_fingerprint'])
    _require(STATE_KEY not in hub and 'c94_face_mouth_pad_v1' not in hub and rig.data.users==1 and rig.data.is_editable and not rig.data.library and not rig.data.override_library,'图形板已存在或Armature Data不是本地单用户')
    _require(type(travel) in (int,float) and math.isfinite(travel) and travel>0,'行程必须有限且为正')
    _require('表情面板' in rig.data.bones,'缺少C94图形板父根')
    travel=float(travel);binding_h=anim._binding(hub);binding_r=anim._binding(rig)
    prop_paths={core._path(r['property']) for r in group['channels']}
    _require(not hub.animation_data or not any(f.data_path in prop_paths for f in hub.animation_data.drivers),'表情参数已有其它驱动')
    source_bag=anim._bag(binding_h['action'],binding_h['slot']) if binding_h['action'] else None
    curves={f.data_path:f for f in source_bag.fcurves if f.data_path in prop_paths} if source_bag else {}
    for f in curves.values():
        _require(f.array_index==0 and f.extrapolation=='CONSTANT' and not f.modifiers and not f.sampled_points and not f.mute and not f.lock
                 and all(k.interpolation in {'LINEAR','CONSTANT'} and 0<=k.co.y<=1 for k in f.keyframe_points), '迁移仅支持普通0..1 LINEAR/CONSTANT参数曲线，原动画保留')
    token=uuid.uuid4().hex[:8];collection_name='角色Key板_'+token;root='角色Key板根_'+token
    rows=[dict(bone='Key滑杆_%02d_'%i+token,anchor='Key布局_%02d_'%i+token,section='表情') for i in range(len(group['channels']))]
    names=[root]+[n for r in rows for n in [r['anchor'],r['bone']]]
    _require(all(n not in rig.data.bones for n in names) and collection_name not in rig.data.collections_all
             and collection_name not in bpy.data.collections, '图形板身份名称冲突，请重试新候选')
    old_values={r['property']:hub[r['property']] for r in group['channels']}
    old_types={r['property']:type(hub[r['property']]) for r in group['channels']}
    old_rig=fm._snapshot(rig)
    rig_inputs=anim._snapshot(rig)
    hub_paths={(f.data_path,f.array_index) for f in source_bag.fcurves} if source_bag else set()
    hub_paths.update((field,i) for field in anim._OBJECT_CHANNELS for i in range(len(getattr(hub,field))))
    hub_inputs={p:anim._animated_value(hub,*p) for p in hub_paths}
    objects=[];display=bc=None;new_rig_action=new_hub_action=None;added=[]
    fake_h=binding_h['action'].use_fake_user if binding_h['action'] else None;fake_r=binding_r['action'].use_fake_user if binding_r['action'] else None
    try:
        # Scripted drivers need floating IDProperties; old mapped hubs may
        # store integer 0 flags. Preserve the numeric value while fixing type.
        for prop in old_values:
            if old_types[prop] is int:
                hub[prop]=float(hub[prop])
        bc=rig.data.collections.new(collection_name,parent=rig.data.collections_all[fm.SPACES['panel']])
        with _edit(rig) as bones:
            origin=bones['表情面板'].head.copy()+Vector((.27,0,-.09))
            b=bones.new(root);b.head=origin;b.tail=origin+Vector((0,0,.02));b.parent=bones['表情面板'];b.use_deform=False;bc.assign(b)
            for i,row in enumerate(rows):
                a=bones.new(row['anchor']);a.head=origin+Vector((i*.105,0,.015));a.tail=a.head+Vector((0,0,.015));a.parent=bones[root];a.use_deform=False;bc.assign(a)
                b=bones.new(row['bone']);b.head=a.head+Vector((0,-.001,.02));b.tail=b.head+Vector((0,0,.018));b.parent=a;b.use_deform=False;bc.assign(b)
        for row,ch in zip(rows,group['channels']):
            p=rig.pose.bones[row['bone']];p.lock_location=(True,False,True);p.lock_rotation=(True,True,True);p.lock_scale=(True,True,True)
            c=p.constraints.new('LIMIT_LOCATION');c.owner_space='LOCAL'
            for axis in 'xyz':setattr(c,'use_min_'+axis,True);setattr(c,'use_max_'+axis,True)
            c.max_y=travel;p.location.y=old_values[ch['property']]*travel
            rig.data.bones[row['anchor']].hide_select=True
            p.color.palette='THEME04'
        # Convert only owned parameter curves; preserve all unrelated hub/rig data.
        if curves:
            new_rig_action,slot=anim._make_action(rig,binding_r);bag=anim._bag(new_rig_action,slot,ensure=True)
            for row,ch in zip(rows,group['channels']):
                f=curves.get(core._path(ch['property']))
                if f is None:continue
                out=bag.fcurves.new(_bone_path(row['bone']),index=1)
                for key in f.keyframe_points:
                    k=out.keyframe_points.insert(key.co.x,key.co.y*travel,options={'FAST'});k.interpolation=key.interpolation
                out.update()
            ad=rig.animation_data_create();ad.action=new_rig_action;ad.action_slot=slot
            if not binding_r['action']:ad.action_blend_type='REPLACE';ad.action_influence=1.;ad.action_extrapolation='HOLD'
            new_hub_action,hslot=anim._make_action(hub,binding_h);hbag=anim._bag(new_hub_action,hslot)
            for f in list(hbag.fcurves):
                if f.data_path in prop_paths:hbag.fcurves.remove(f)
            hub.animation_data.action=new_hub_action;hub.animation_data.action_slot=hslot
        for row,ch in zip(rows,group['channels']):
            path=core._path(ch['property']);added.append(path)
            curve=hub.driver_add(path)
            # driver_add creates a default mapping curve. This board uses the
            # direct expression output, so clear only this newly created map.
            expression_core._clear_new_driver_mapping(curve)
            d=curve.driver;d.type='SCRIPTED';d.expression='min(1,max(0,v/'+repr(travel)+'))'
            v=d.variables.new();v.name='v';v.type='SINGLE_PROP';v.targets[0].id=rig;v.targets[0].data_path=_bone_path(row['bone'])+'[1]'
        display=bpy.data.collections.new(collection_name);bpy.context.scene.collection.children.link(display)
        verts,edges=_rect(-.13,-.04,max(.12,(len(rows)-1)*.105+.07),.17)
        # A small schematic face belongs to the panel frame, not character geometry.
        off=len(verts);face=[(-.082+.027*math.cos(i*math.tau/32),.068+.043*math.sin(i*math.tau/32),0) for i in range(32)]
        verts+=face;edges += [(off+i,off+(i+1)%32) for i in range(32)]
        for x,y,width in [(-.094,.078,.012),(-.071,.078,.012),(-.089,.047,.014)]:
            n=len(verts);verts += [(x,y,0),(x+width,y,0)];edges.append((n,n+1))
        _shape('表情板框_'+token,verts,edges,rig,root,display,objects)
        title=_shape('表情板标题_'+token,None,None,rig,root,display,objects,font=font,text='角色表情 · KEY',color=(.9,.9,.95,1));title.location=(-.11,.14,.002)
        for row,ch in zip(rows,group['channels']):
            verts,edges=_rect(-.005,.02,.005,.02+travel);_shape('轨道_'+row['bone'],verts,edges,rig,row['anchor'],display,objects,color=(.35,.4,.5,1))
            verts,edges=_rect(-.012,-.006,.012,.006);_shape('手柄_'+row['bone'],verts,edges,rig,row['bone'],display,objects,color=(.25,.8,.65,1))
            label=_shape('标签_'+row['bone'],None,None,rig,row['anchor'],display,objects,font=font,text=ch['name'],color=(.9,.9,.95,1));label.location=(-.023,-.022,.002)
        hub['board_display']=display
        hub[STATE_KEY]=json.dumps(dict(schema='c94.face-graph-board.v1',root=root,collection=collection_name,travel=travel,channels=rows),ensure_ascii=False)
        core._update(hub,rig);_read(hub)
        anim._check_inputs(rig,rig_inputs,set())
        _require(all(anim._animated_value(hub,*p)==v for p,v in hub_inputs.items() if p[0] not in prop_paths),'迁移覆盖了非目标控制输入')
        _require(fm._snapshot(rig)==old_rig and all(abs(hub[p]-v)<1e-6 for p,v in old_values.items()),'图形板迁移改变了原面部输入或当前参数')
        if binding_h['action']:binding_h['action'].use_fake_user=True
        if binding_r['action']:binding_r['action'].use_fake_user=True
        return dict(root=root,controls=[r['bone'] for r in rows],collection=collection_name,migrated_channels=len(curves),old_actions_retained=True)
    except Exception as error:
        recovery_errors=[]
        for path in added:hub.driver_remove(path)
        anim._restore_binding(hub,binding_h);anim._restore_binding(rig,binding_r)
        for p,v in old_values.items():hub[p]=v
        for prop,kind in old_types.items():
            if kind is int:
                hub[prop]=int(old_values[prop])
        _restore_inputs(rig,rig_inputs)
        for p,v in hub_inputs.items():anim._restore_value(hub,*p,v)
        for key in [STATE_KEY,'board_display']:
            if key in hub:del hub[key]
        _destroy(objects)
        if display is not None:bpy.data.collections.remove(display)
        with _edit(rig) as bones:
            for n in reversed(names):
                if n in bones:bones.remove(bones[n])
        if bc is not None:rig.data.collections.remove(bc)
        for action in [new_hub_action,new_rig_action]:
            expression_core._remove_owned_action(action,recovery_errors)
        if binding_h['action']:binding_h['action'].use_fake_user=fake_h
        if binding_r['action']:binding_r['action'].use_fake_user=fake_r
        core._update(hub,rig)
        if recovery_errors:
            raise RuntimeError(str(error)+'；图形板创建恢复未完整核实：'+'; '.join(recovery_errors)) from error
        raise
