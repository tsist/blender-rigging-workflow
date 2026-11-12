# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit semantic/per-source Key selection, with source-preserving copies."""
import bpy,uuid
from bpy.props import PointerProperty,CollectionProperty,StringProperty,IntProperty
from . import face_key_mapping as core,face_expression_controls_ui as controls_ui

OPTIONS='c94_existing_key_mapping_options_v1'
_REGISTERED=[]
_ERRORS=(ValueError,RuntimeError,ReferenceError,KeyError,TypeError,AttributeError,IndexError)


class C94FKM_Source(bpy.types.PropertyGroup):
    source:PointerProperty(type=bpy.types.Object,poll=lambda self,obj:obj.type=='MESH')
    key:StringProperty(name='实际形态键')


class C94FKM_Channel(bpy.types.PropertyGroup):
    label:StringProperty(name='表情语义',default='新表情')
    targets:CollectionProperty(type=C94FKM_Source)


class C94FKM_Options(bpy.types.PropertyGroup):
    rig:PointerProperty(name='角色骨架',type=bpy.types.Object,poll=lambda self,obj:obj.type=='ARMATURE')
    sources:CollectionProperty(type=C94FKM_Source)
    channels:CollectionProperty(type=C94FKM_Channel)


class C94FKM_OT_Sources(bpy.types.Operator):
    bl_idname='c94_workflow.key_mapping_sources'
    bl_label='添加所选已有Key网格'
    remove_index:IntProperty(default=-1)
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        if self.remove_index>=0:
            if self.remove_index<len(opts.sources):opts.sources.remove(self.remove_index)
        else:
            existing={r.source for r in opts.sources}
            for o in context.selected_objects:
                if o.type=='MESH' and o not in existing:opts.sources.add().source=o;existing.add(o)
        return {'FINISHED'}


class C94FKM_OT_Channel(bpy.types.Operator):
    bl_idname='c94_workflow.key_mapping_channel'
    bl_label='添加表情映射'
    remove_index:IntProperty(default=-1)
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        if self.remove_index>=0:
            if self.remove_index<len(opts.channels):opts.channels.remove(self.remove_index)
        else:
            row=opts.channels.add()
            for src in opts.sources:row.targets.add().source=src.source
        return {'FINISHED'}


class C94FKM_OT_Create(bpy.types.Operator):
    bl_idname='c94_workflow.key_mapping_create'
    bl_label='创建保留原Key的映射副本'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        try:
            sources=[r.source for r in opts.sources];mapping=[]
            for r in opts.channels:
                if [t.source for t in r.targets]!=sources:raise ValueError('源列表已改变，请重新明确该表情的目标列表')
                mapping.append(dict(name=r.label,keys=[t.key for t in r.targets]))
            hub,copies=core.create(opts.rig,sources,mapping,name='已有Key表情控制_'+uuid.uuid4().hex[:8])
            getattr(context.scene,controls_ui.OPTIONS).hub=hub
            self.report({'INFO'},'已创建'+str(len(copies))+'个独立映射副本；原Key保持，请检查副本显示与效果')
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FKM_PT_Mapping(bpy.types.Panel):
    bl_idname='C94FKM_PT_mapping';bl_label='已有Key显式映射（副本）'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='面部';bl_order=5
    bl_options={'DEFAULT_CLOSED'}
    def draw(self,context):
        l=self.layout;opts=getattr(context.scene,OPTIONS);l.prop(opts,'rig')
        for i,r in enumerate(opts.sources):
            line=l.row(align=True);line.prop(r,'source',text='源网格');line.operator('c94_workflow.key_mapping_sources',text='',icon='X').remove_index=i
        l.operator('c94_workflow.key_mapping_sources').remove_index=-1
        for i,r in enumerate(opts.channels):
            box=l.box();line=box.row(align=True);line.prop(r,'label');line.operator('c94_workflow.key_mapping_channel',text='',icon='X').remove_index=i
            for t in r.targets:
                if t.source and t.source.data.shape_keys:box.prop_search(t,'key',t.source.data.shape_keys,'key_blocks',text=t.source.name)
                else:box.label(text='目标没有形态键，请明确源')
        l.operator('c94_workflow.key_mapping_channel').remove_index=-1
        l.operator('c94_workflow.key_mapping_create')
        l.label(text='逐对象选择实际Key，不按同名猜语义')
        l.label(text='保留原名/坐标/相对关系；不覆盖旧动画/driver')


_CLASSES=(C94FKM_Source,C94FKM_Channel,C94FKM_Options,C94FKM_OT_Sources,C94FKM_OT_Channel,C94FKM_OT_Create,C94FKM_PT_Mapping)
def register():
    global _C94_OPTIONS_OWNED
    if _REGISTERED:return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('该面板属性已由其他实例持有；原属性保持')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94FKM_Options))
        _C94_OPTIONS_OWNED = True
    except Exception:unregister();raise
def unregister():
    if hasattr(bpy.types.Scene,OPTIONS):_release_workspace_options()
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()


_C94_OPTIONS_OWNED = False

def _release_workspace_options():
    global _C94_OPTIONS_OWNED
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if (_C94_OPTIONS_OWNED and prop and prop.type == 'POINTER'
            and prop.fixed_type == C94FKM_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
