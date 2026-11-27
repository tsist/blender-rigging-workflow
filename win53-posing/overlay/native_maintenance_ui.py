# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit original-rig work copies, native mappings and local repair candidates."""
import json
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import (PointerProperty,CollectionProperty,StringProperty,EnumProperty,FloatProperty,
                       BoolProperty,FloatVectorProperty)

OPTIONS='rigging_native_maintenance_options_v1'
_REGISTERED=[]
_PROPERTY_OWNED=False
_MEMBER_DATA={}


def _armature(_self,obj):return obj.type=='ARMATURE'
def _mesh(_self,obj):return obj.type=='MESH'
def _options(context):return getattr(context.scene,OPTIONS)


def _json(value,label):
    try:return json.loads(value)
    except (TypeError,ValueError) as exc:raise ValueError(label+'JSON无效') from exc


def _source(options,context):
    rig=options.source_rig
    if not rig or rig.type!='ARMATURE' or context.view_layer.objects.get(rig.name)!=rig:
        raise ValueError('选择当前视图层内的明确原骨架')
    collection=options.mesh_collection
    if collection is None:raise ValueError('明确指定要复制的皮肤/部件集合')
    objects=[o for o in collection.all_objects if o.type=='MESH']
    if not objects:raise ValueError('部件集合没有Mesh')
    return rig,objects


def _current(options,context,core):
    rig=options.current_rig
    if not rig or context.view_layer.objects.get(rig.name)!=rig:
        raise ValueError('先建立或选中本工具维护工作副本')
    core.get_layout(rig)
    return rig


def _mapping(options):
    value=_json(options.mapping_json,'控件映射')
    if not isinstance(value,dict):raise ValueError('控件映射须为三类列表对象')
    return value


def _summary(options,result):
    options.report_json=json.dumps(result,ensure_ascii=False,default=str,indent=2)


def _load_members(options,objects):
    options.members.clear()
    for obj in objects:
        options.members.add().object=obj
        keys=obj.data.shape_keys
        _MEMBER_DATA[(options.current_rig.session_uid,obj.session_uid)]=(obj.data,keys,obj.data.users,keys.users if keys else 0)
    options.loaded_rig=options.current_rig


class NATIVE_MAINTENANCE_Member(bpy.types.PropertyGroup):
    object:PointerProperty(type=bpy.types.Object,poll=_mesh)


class NATIVE_MAINTENANCE_Options(bpy.types.PropertyGroup):
    source_rig:PointerProperty(name='保留的原骨架',type=bpy.types.Object,poll=_armature)
    mesh_collection:PointerProperty(name='明确皮肤/部件集合',type=bpy.types.Collection)
    dependency_collection:PointerProperty(name='另需复制的依赖对象',type=bpy.types.Collection)
    current_rig:PointerProperty(name='当前维护副本',type=bpy.types.Object,poll=_armature)
    loaded_rig:PointerProperty(type=bpy.types.Object,options={'HIDDEN','SKIP_SAVE'})
    members:CollectionProperty(type=NATIVE_MAINTENANCE_Member,options={'HIDDEN','SKIP_SAVE'})
    label:StringProperty(name='新副本名称',default='原骨架维护')
    mapping_json:StringProperty(name='映射配置',default='{"controls":[],"properties":[],"keys":[]}')
    control_bone:StringProperty(name='实际控制骨')
    row_label:StringProperty(name='显示标签',default='原生控制')
    row_group:StringProperty(name='显示分组',default='原骨架控件')
    control_property:StringProperty(name='原生自定义属性')
    key_object:PointerProperty(name='实际Key对象',type=bpy.types.Object,poll=_mesh)
    key_name:StringProperty(name='实际形态键')
    edit_kind:EnumProperty(name='局部修复类型',items=(('CONSTRAINT','约束','只改明确约束字段'),
        ('BONE','非连接骨位','生成独立待复核骨位候选'),('WEIGHT','选中点权重转移','只改明确点和两组')))
    constraint_bone:StringProperty(name='约束所属骨')
    constraint_name:StringProperty(name='实际约束')
    constraint_field:EnumProperty(name='调整字段',items=(('influence','影响','明确影响权重'),('mute','静音','仅当前约束')))
    influence:FloatProperty(name='影响',default=1.,min=0.,max=1.)
    mute:BoolProperty(name='静音',default=False)
    bone_name:StringProperty(name='明确目标骨')
    bone_head:FloatVectorProperty(name='骨架局部头点',size=3)
    bone_tail:FloatVectorProperty(name='骨架局部尾点',size=3,default=(0.,.1,0.))
    bone_roll:FloatProperty(name='Roll（弧度）',default=0.)
    acknowledge_bone_change:BoolProperty(name='已查看目标及影响范围',default=False)
    weight_object:PointerProperty(name='维护副本皮肤',type=bpy.types.Object,poll=_mesh)
    indices_json:StringProperty(name='明确点索引',default='[]')
    from_group:StringProperty(name='转出组')
    to_group:StringProperty(name='转入组')
    transfer:FloatProperty(name='逐点转移量',default=.05,min=0.,max=1.)
    reason:StringProperty(name='本次局部修改说明',default='明确局部维护')
    summary:StringProperty(default='检查原骨架，建立独立副本后维护',options={'SKIP_SAVE'})
    report_json:StringProperty(name='上次检查/影响报告',options={'SKIP_SAVE'})


class NATIVE_MAINTENANCE_OT_Action(bpy.types.Operator):
    bl_idname='c94_workflow.native_maintenance_action'
    bl_label='原骨架维护操作'
    action:EnumProperty(items=tuple((s,l,'') for s,l in (
        ('INSPECT','检查明确输入'),('CLONE','建立保原骨架工作副本'),('READ','读取当前映射'),
        ('APPLY_LAYOUT','应用映射布局'),('ADD_CONTROL','加入实际控制骨'),
        ('ADD_PROPERTY','加入实际属性'),('ADD_KEY','加入实际形态键'),
        ('READ_BONE','读取原生骨位'),('READ_VERTICES','采集已选点域'),
        ('REPAIR','生成局部修复候选'),('SELECT_PACK','选择当前副本'),('SELECT_CONTROL','选择所列控骨'),('KEY_CONTROLS','记录所列控骨'))))

    @classmethod
    def poll(cls,context):return context.mode in {'OBJECT','POSE'}

    def execute(self,context):
        try:
            from . import native_maintenance as core
            options=_options(context)
            if self.action in {'INSPECT','CLONE'}:
                rig,objects=_source(options,context)
                if self.action=='INSPECT':
                    report=core.inspect(rig,objects);_summary(options,report)
                    options.summary='原架构已检查；控制含义按实际映射，不生成C94参考骨'
                else:
                    if context.mode!='OBJECT':raise ValueError('建立副本前请退出姿态模式')
                    deps=list(options.dependency_collection.all_objects) if options.dependency_collection else []
                    result=core.clone(rig,objects,label=options.label.strip(),mapping=_mapping(options),dependencies=deps)
                    options.current_rig=result['rig'];_summary(options,result['report'])
                    _load_members(options,result['objects'])
                    options.mapping_json=json.dumps(core.get_layout(result['rig']),ensure_ascii=False)
                    options.summary='独立原骨架副本已建立，原资产和原架构保留'
            elif self.action in {'ADD_CONTROL','ADD_PROPERTY','ADD_KEY'}:
                rows=_mapping(options);base={'id':uuid.uuid4().hex,'label':options.row_label,
                    'group':options.row_group,'visible':True}
                if self.action=='ADD_CONTROL':
                    base['bone']=options.control_bone;rows['controls'].append(base)
                elif self.action=='ADD_PROPERTY':
                    base.update(bone=options.control_bone,property=options.control_property);rows['properties'].append(base)
                else:
                    rig=_current(options,context,core)
                    obj=options.key_object
                    members=[m.object for m in options.members] if options.loaded_rig==rig else []
                    if obj not in members:raise ValueError('Key对象须属于当前维护pack；先读取当前映射')
                    base.update(object_index=members.index(obj),key=options.key_name);rows['keys'].append(base)
                options.mapping_json=json.dumps(rows,ensure_ascii=False)
                options.summary='映射已加入表单；应用映射后检查实际引用'
            elif self.action in {'READ','APPLY_LAYOUT'}:
                rig=_current(options,context,core)
                if self.action=='APPLY_LAYOUT':core.set_layout(rig,_mapping(options))
                options.mapping_json=json.dumps(core.get_layout(rig),ensure_ascii=False)
                report=core.inspect(rig);names=[m['object']['name'] for m in report['meshes']]
                _load_members(options,[context.scene.objects[n] for n in names])
                options.summary='当前真实映射已读取；未重置原布局'
            elif self.action=='READ_BONE':
                rig=_current(options,context,core);bone=rig.data.bones.get(options.bone_name)
                if not bone:raise ValueError('选择实际存在的骨')
                options.bone_head=bone.head_local;options.bone_tail=bone.tail_local
                _axis,options.bone_roll=bpy.types.Bone.AxisRollFromMatrix(bone.matrix_local.to_3x3())
                options.summary='已读取骨架局部端点与Roll；生成候选前确认影响范围'
            elif self.action=='READ_VERTICES':
                obj=options.weight_object
                if not obj:raise ValueError('选择当前维护副本中的明确皮肤')
                indices = [v.index for v in obj.data.vertices if v.select and not v.hide]
                options.indices_json = json.dumps(indices)
                options.summary = '已读取%d个可见选中点；先确认范围再生成候选' % len(indices)
            elif self.action=='REPAIR':
                if context.mode!='OBJECT':raise ValueError('局部修复前请退出姿态模式')
                rig=_current(options,context,core);constraint_edits=[];bone_edits=[];weight_edits=[]
                if options.edit_kind=='CONSTRAINT':
                    field=options.constraint_field
                    constraint_edits=[{'bone':options.constraint_bone,'constraint':options.constraint_name,
                        'field':field,'value':options.influence if field=='influence' else options.mute}]
                elif options.edit_kind=='BONE':
                    bone_edits=[{'bone':options.bone_name,'head':list(options.bone_head),
                                 'tail':list(options.bone_tail),'roll':options.bone_roll}]
                else:
                    obj=options.weight_object
                    members=[m.object for m in options.members] if options.loaded_rig==rig else []
                    if obj not in members:raise ValueError('目标皮肤不属于当前维护pack；先读取映射')
                    indices=_json(options.indices_json,'点域')
                    if not isinstance(indices,list) or not indices:raise ValueError('先采集明确非空点域')
                    if options.from_group==options.to_group:raise ValueError('转入/转出组不能相同')
                    groups=[obj.vertex_groups.get(n) for n in (options.from_group,options.to_group)]
                    if any(g is None for g in groups):raise ValueError('明确选择两个实际权重组')
                    assignments={g.name:[] for g in groups}
                    for i in indices:
                        if type(i) is not int or not 0<=i<len(obj.data.vertices):raise ValueError('点索引越界')
                        weights={x.group:x.weight for x in obj.data.vertices[i].groups}
                        left=weights.get(groups[0].index,0.);right=weights.get(groups[1].index,0.)
                        if left<options.transfer or right+options.transfer>1.:raise ValueError('明确转移量超出某点原组质量；未截断或归一化')
                        assignments[groups[0].name].append(left-options.transfer)
                        assignments[groups[1].name].append(right+options.transfer)
                    weight_edits=[{'object_index':members.index(obj),'indices':indices,'assignments':assignments}]
                result=core.repair(rig,label=options.label.strip(),reason=options.reason,
                    constraint_edits=constraint_edits,bone_edits=bone_edits,weight_edits=weight_edits,
                    acknowledge_bone_change=options.acknowledge_bone_change)
                options.current_rig=result['rig'];_summary(options,result['report'])
                _load_members(options,result['objects'])
                options.mapping_json=json.dumps(core.get_layout(result['rig']),ensure_ascii=False)
                options.summary='独立局部修复候选已生成；原检查点保留，效果待复核'
            else:
                rig=_current(options,context,core);mapping=core.get_layout(rig)
                if self.action=='SELECT_PACK':
                    if options.loaded_rig!=rig:raise ValueError('先读取当前副本映射')
                    if context.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
                    for obj in context.selected_objects:obj.select_set(False)
                    for obj in [rig]+[m.object for m in options.members]:obj.select_set(True)
                    context.view_layer.objects.active=rig
                    options.summary='已选择当前骨架和皮肤；可用Blender原生局部视图隔离检查'
                elif self.action=='SELECT_CONTROL':
                    for obj in context.selected_objects:obj.select_set(False)
                    rig.select_set(True);context.view_layer.objects.active=rig
                    if context.mode!='POSE':bpy.ops.object.mode_set(mode='POSE')
                    wanted={r['bone'] for r in mapping['controls'] if r.get('visible',True)}
                    for pb in rig.pose.bones:pb.select=pb.name in wanted
                    if wanted:rig.data.bones.active=rig.data.bones[next(iter(wanted))]
                    options.summary='已选择本映射中的实际控骨'
                elif self.action=='KEY_CONTROLS':
                    result=core.record_controls(rig,[r['id'] for r in mapping['controls'] if r.get('visible',True)])
                    _summary(options,result)
                    options.summary='当前维护副本所列控骨已记录原生关键帧'
                else:raise ValueError('未知维护操作')
            self.report({'INFO'},options.summary);return {'FINISHED'}
        except Exception as exc:
            _options(context).summary='操作未完成：'+str(exc)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


def _button(layout,text,action):layout.operator('c94_workflow.native_maintenance_action',text=text).action=action


class NATIVE_MAINTENANCE_PT_Panel(bpy.types.Panel):
    bl_label='保留原骨架维护'
    bl_idname='NATIVE_MAINTENANCE_PT_panel'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='角色绑定';bl_order=10

    def draw(self,context):
        options=_options(context);layout=self.layout
        advanced=getattr(getattr(context.scene,'c94_workspace_options_v1',None),'level','ADVANCED')!='DAILY'
        layout.prop(options,'source_rig');layout.prop(options,'mesh_collection');layout.prop(options,'dependency_collection')
        layout.prop(options,'label')
        if advanced:layout.prop(options,'mapping_json')
        row=layout.row(align=True);_button(row,'检查明确输入','INSPECT');_button(row,'建立独立副本','CLONE')
        layout.prop(options,'current_rig');row=layout.row(align=True)
        _button(row,'读取映射','READ')
        if advanced:_button(row,'应用映射','APPLY_LAYOUT')
        _button(layout,'选择当前副本（骨架与皮肤）','SELECT_PACK')
        rig=options.current_rig or options.source_rig
        if advanced:
            editor=layout.box();editor.label(text='按实际引用补充映射')
            if rig:editor.prop_search(options,'control_bone',rig.pose,'bones')
            else:editor.prop(options,'control_bone')
            editor.prop(options,'row_label');editor.prop(options,'row_group');_button(editor,'加入控制骨','ADD_CONTROL')
            editor.prop(options,'control_property');_button(editor,'加入原生属性','ADD_PROPERTY')
            editor.prop(options,'key_object')
            if options.key_object and options.key_object.data.shape_keys:
                editor.prop_search(options,'key_name',options.key_object.data.shape_keys,'key_blocks')
            else:editor.prop(options,'key_name')
            _button(editor,'加入已有形态键','ADD_KEY')
        current=options.current_rig
        if current and 'rigging_native_maintenance_v1' in current:
            try:
                mapping=_json(options.mapping_json,'映射')
                if not isinstance(mapping, dict) or any(
                    not isinstance(mapping.get(group, []), list)
                    or any(not isinstance(item, dict) for item in mapping.get(group, []))
                    for group in ('controls', 'properties', 'keys')):
                    raise ValueError('映射应为包含控件、属性和Key列表的对象')
                controls=layout.box();controls.label(text='当前维护副本的原生控件')
                for row in mapping.get('controls',[]):
                    pb=current.pose.bones.get(row.get('bone',''))
                    if pb and row.get('visible',True):
                        controls.label(text=row.get('group','')+' / '+row.get('label',pb.name))
                        controls.prop(pb,'location');controls.prop(pb,'scale')
                        controls.prop(pb,'rotation_quaternion' if pb.rotation_mode=='QUATERNION' else 'rotation_axis_angle' if pb.rotation_mode=='AXIS_ANGLE' else 'rotation_euler')
                for row in mapping.get('properties',[]):
                    pb=current.pose.bones.get(row.get('bone',''))
                    if pb and row.get('property') in pb and row.get('visible',True):
                        controls.prop(pb,'['+json.dumps(row['property'])+']',text=row.get('label',row['property']))
                if options.loaded_rig==current:
                    for row in mapping.get('keys',[]):
                        index=row.get('object_index',-1)
                        obj=options.members[index].object if type(index) is int and 0<=index<len(options.members) else None
                        keys=obj.data.shape_keys if obj and obj.type=='MESH' else None
                        key=keys.key_blocks.get(row.get('key','')) if keys else None
                        if key and row.get('visible',True):
                            identity=_MEMBER_DATA.get((current.session_uid,obj.session_uid))
                            writable=bool(identity and obj.data==identity[0] and keys==identity[1]
                                and obj.data.users==identity[2] and keys.users==identity[3])
                            slot=controls.row();slot.enabled=writable
                            slot.prop(key,'value',text=row.get('label',key.name))
                            if not writable:controls.label(text='数据引用/共享已改变；重新检查当前副本')
                r=controls.row(align=True);_button(r,'选择所列控骨','SELECT_CONTROL');_button(r,'记录所列控骨','KEY_CONTROLS')
            except (ValueError,TypeError,KeyError):layout.label(text='映射表单无效，请修正后重新应用',icon='ERROR')
        if advanced:
            repair=layout.box();repair.label(text='局部修改生成新检查点候选');repair.prop(options,'edit_kind')
            if options.edit_kind=='CONSTRAINT':
                if current:repair.prop_search(options,'constraint_bone',current.pose,'bones')
                else:repair.prop(options,'constraint_bone')
                pb=current.pose.bones.get(options.constraint_bone) if current else None
                if pb:repair.prop_search(options,'constraint_name',pb,'constraints')
                else:repair.prop(options,'constraint_name')
                repair.prop(options,'constraint_field');repair.prop(options,'influence' if options.constraint_field=='influence' else 'mute')
            elif options.edit_kind=='BONE':
                if current:repair.prop_search(options,'bone_name',current.data,'bones')
                else:repair.prop(options,'bone_name')
                _button(repair,'读取目标骨位','READ_BONE')
                for field in ('bone_head','bone_tail','bone_roll','acknowledge_bone_change'):repair.prop(options,field)
            else:
                repair.prop(options,'weight_object');_button(repair,'采集已选点域','READ_VERTICES');repair.prop(options,'indices_json')
                for field in ('from_group','to_group'):
                    if options.weight_object:repair.prop_search(options,field,options.weight_object,'vertex_groups')
                    else:repair.prop(options,field)
                repair.prop(options,'transfer')
            repair.prop(options,'reason');_button(repair,'生成独立局部修复候选','REPAIR')
        else:layout.label(text='映射编辑与局部修复位于进阶',icon='INFO')
        layout.label(text=options.summary);layout.label(text='原架构与原资产保留；候选效果评估暂缓')


_CLASSES=(NATIVE_MAINTENANCE_Member,NATIVE_MAINTENANCE_Options,NATIVE_MAINTENANCE_OT_Action,NATIVE_MAINTENANCE_PT_Panel)


@persistent
def _clear(*_args):_MEMBER_DATA.clear()


def register():
    global _PROPERTY_OWNED
    if _REGISTERED:return
    if hasattr(bpy.types.Scene,OPTIONS):raise RuntimeError('原骨架维护选项已被其它实例注册')
    for base,name in ((bpy.types.PropertyGroup,NATIVE_MAINTENANCE_Options.__name__),
                      (bpy.types.PropertyGroup,NATIVE_MAINTENANCE_Member.__name__),
                      (bpy.types.Operator,'C94_WORKFLOW_OT_native_maintenance_action'),
                      (bpy.types.Panel,NATIVE_MAINTENANCE_PT_Panel.bl_idname)):
        if base.bl_rna_get_subclass_py(name,None) is not None:raise RuntimeError('原骨架维护同名类型已被其它实例注册')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=NATIVE_MAINTENANCE_Options));_PROPERTY_OWNED=True
        bpy.app.handlers.load_pre.append(_clear)
    except Exception:unregister();raise


def unregister():
    global _PROPERTY_OWNED
    if _PROPERTY_OWNED and hasattr(bpy.types.Scene,OPTIONS):
        prop=bpy.types.Scene.bl_rna.properties.get(OPTIONS)
        if prop and prop.fixed_type==NATIVE_MAINTENANCE_Options.bl_rna:delattr(bpy.types.Scene,OPTIONS)
    _PROPERTY_OWNED=False
    if _clear in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(_clear)
    _MEMBER_DATA.clear()
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
