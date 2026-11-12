# SPDX-License-Identifier: GPL-3.0-or-later
"""First body-retargeting slice; GUI and Agent call the same core."""
import json
import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty,StringProperty,EnumProperty,IntProperty,BoolProperty,FloatProperty
from bpy_extras.io_utils import ImportHelper
from . import retarget_source as sources,retarget_action as actions,retarget_bvh as bvh

OPTIONS='c94_retarget_options_v1'
_REGISTERED=[]
_OWNED=False

def _armature(self,obj):return obj.type=='ARMATURE'
def _options(context):return getattr(context.scene,OPTIONS)

class C94RETARGET_Options(bpy.types.PropertyGroup):
    source_kind:EnumProperty(name='来源预设',items=(('MIXAMO','FBX / Mixamo','已映射人形FBX或场景Action'),
        ('CMU','CMU BVH（已验骨架）','显式轴/比例、REST朝向与每动作位移原点')),default='MIXAMO')
    source:PointerProperty(name='源动画骨架',type=bpy.types.Object,poll=_armature)
    target:PointerProperty(name='C94目标角色',type=bpy.types.Object,poll=_armature)
    candidate:PointerProperty(name='独立候选动作',type=bpy.types.Action)
    mapping_json:StringProperty(name='语义映射（JSON）',default='{}')
    motion:EnumProperty(name='根运动',items=(('PRESERVE','保留移动与转向','按来源运动'),('IN_PLACE','原地，保留转向和跳跃','移除水平轨迹')),default='PRESERVE')
    scale_motion:BoolProperty(name='按目标身高缩放位移',default=True)
    include_fingers:BoolProperty(name='转换完整手指链',description='仅转换具有三节及明确末端节点的已映射链，保留原控件限制',default=True)
    override_fps:BoolProperty(name='明确覆盖源帧率',default=False)
    source_fps:FloatProperty(name='源帧率',default=30,min=0.01,max=1000)
    preview_step:IntProperty(name='快速预览采样间隔',min=1,max=60,default=12)
    summary:StringProperty(name='状态',default='先生成独立身体候选；表情、视线与接触复核见对应专项面板')

class C94RETARGET_OT_import(bpy.types.Operator,ImportHelper):
    bl_idname='c94_workflow.retarget_import_fbx'
    bl_label='导入源FBX动画'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.fbx'
    filter_glob:StringProperty(default='*.fbx',options={'HIDDEN'})

    def execute(self,context):
        try:
            imported=sources.import_fbx(self.filepath)
            opts=_options(context);opts.source=imported['rig'];opts.source_kind='MIXAMO';opts.mapping_json='{}'
            opts.summary='已导入源动画（'+str(imported['record']['fps'])+'fps）；生产场景设置保持'
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94RETARGET_OT_import_bvh(bpy.types.Operator,ImportHelper):
    bl_idname='c94_workflow.retarget_import_bvh'
    bl_label='导入CMU BVH动画（已验骨架）'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.bvh'
    filter_glob:StringProperty(default='*.bvh',options={'HIDDEN'})
    scale:FloatProperty(name='每源单位对应Blender单位',default=.01,min=.000001,max=100)
    forward:EnumProperty(name='源前向',items=[(v,v,'') for v in ('X','Y','Z','-X','-Y','-Z')],default='Z')
    up:EnumProperty(name='源上向',items=[(v,v,'') for v in ('X','Y','Z','-X','-Y','-Z')],default='Y')

    def execute(self,context):
        try:
            imported=bvh.import_cmu(self.filepath,scale=self.scale,forward=self.forward,up=self.up)
            opts=_options(context);opts.source=imported['rig'];opts.source_kind='CMU';opts.mapping_json='{}'
            opts.summary='CMU BVH源已导入；源fps和Slot已登记，生产场景时基保持'
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94RETARGET_OT_action(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_action'
    bl_label='动画重定向'
    bl_options={'REGISTER'}
    operation:EnumProperty(items=[(x,x,'') for x in ('TARGET','MAP','PREVIEW_BUILD','CONVERT','PREVIEW','STOP')])

    def execute(self,context):
        opts=_options(context)
        try:
            if self.operation=='TARGET':
                rigs={m.object for o in context.scene.objects if o.type=='MESH' and o.visible_get() and not o.hide_render
                      for m in o.modifiers if m.type=='ARMATURE' and m.object and m.show_viewport and m.show_render
                      and 'c94_generated_rig_v1' in m.object}
                if len(rigs)!=1:raise ValueError('生产蒙皮存在多个或没有C94骨架，请明确选择目标')
                opts.target=next(iter(rigs));opts.summary='已按可见生产Mesh的Armature引用选择目标'
            elif self.operation=='MAP':
                proposal=sources.exact_cmu_mapping(opts.source) if opts.source_kind=='CMU' else sources.mapping(opts.source)
                opts.mapping_json=json.dumps(proposal['bones'],ensure_ascii=False)
                opts.summary='身体映射已检查；可在映射配置中调整'
            elif self.operation in ('PREVIEW_BUILD','CONVERT'):
                if actions.has_preview(opts.target): actions.stop_preview(opts.target)
                overrides=json.loads(opts.mapping_json)
                wm=context.window_manager
                wm.progress_begin(0,100)
                try:
                    arguments=dict(mapping=overrides,source_fps=opts.source_fps if opts.override_fps else None,
                        motion=opts.motion,sample_step=opts.preview_step if self.operation=='PREVIEW_BUILD' else 1,
                        progress=lambda phase,i,n:wm.progress_update((0 if phase=='SOURCE' else 50)+50*i/n))
                    if opts.source_kind=='CMU':converted=bvh.convert(opts.source,opts.target,**arguments)
                    else:converted=actions.convert(opts.source,opts.target,scale_motion=opts.scale_motion,
                        include_fingers=opts.include_fingers,**arguments)
                finally:wm.progress_end()
                from . import retarget_contact_detection_ui as detection_ui
                detection_ui.complete_from_context(context,opts.source,opts.target,converted)
                opts.candidate=converted['action']
                report=converted['report']
                text=bpy.data.texts.new('重定向报告-'+report['id'][:8])
                text.write(json.dumps(report,ensure_ascii=False,indent=2))
                actions.begin_preview(opts.target,opts.candidate,slot=converted['slot'])
                opts.summary='候选预览中；检查原生约束残差。停止预览可恢复原动作'
            elif self.operation=='PREVIEW':
                actions.begin_preview(opts.target,opts.candidate)
                opts.summary='候选预览中'
            else:
                actions.stop_scene_preview(context.scene);opts.summary='已恢复当前场景预览的原动作，候选Action仍保留'
            return {'FINISHED'}
        except Exception as exc:
            opts.summary=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94RETARGET_PT_body(bpy.types.Panel):
    bl_idname='C94RETARGET_PT_body'
    bl_label='动画重定向 · 身体与手指候选'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向'

    def draw(self,context):
        opts=_options(context);layout=self.layout
        workspace=getattr(context.scene,'c94_workspace_options_v1',None)
        advanced=workspace is None or workspace.level!='DAILY'
        layout.operator('c94_workflow.retarget_import_fbx')
        layout.operator('c94_workflow.retarget_import_bvh')
        layout.prop(opts,'source_kind')
        layout.prop(opts,'source');layout.prop(opts,'target')
        layout.operator('c94_workflow.retarget_action',text='按生产蒙皮选择目标').operation='TARGET'
        layout.operator('c94_workflow.retarget_action',text='检查身体映射').operation='MAP'
        if advanced:layout.prop(opts,'mapping_json')
        layout.prop(opts,'motion')
        if advanced:layout.prop(opts,'preview_step')
        if opts.source_kind=='CMU':
            layout.label(text='按腿长缩放位移；REST朝向 / 片段起点原点')
            layout.label(text='本CMU源无手指；表情、视线未转换')
        else:
            layout.prop(opts,'scale_motion');layout.prop(opts,'include_fingers')
        if advanced:layout.prop(opts,'override_fps')
        if opts.override_fps:layout.prop(opts,'source_fps')
        layout.operator('c94_workflow.retarget_action',text='生成快速预览').operation='PREVIEW_BUILD'
        layout.operator('c94_workflow.retarget_action',text='生成逐帧独立Action（同步）').operation='CONVERT'
        layout.prop(opts,'candidate')
        from . import retarget_headneck_ui
        retarget_headneck_ui.draw_button(layout,opts.target,opts.candidate)
        row=layout.row(align=True)
        row.operator('c94_workflow.retarget_action',text='预览候选').operation='PREVIEW'
        row.operator('c94_workflow.retarget_action',text='停止并恢复').operation='STOP'
        layout.label(text=opts.summary[:56],icon='INFO')
        layout.label(text='原动作保留；保存前自动停止临时预览')
        layout.label(text='手指保原限制；MMD表情和足底问题使用专项入口')
        layout.label(text='较长动作可切换进阶，使用后台队列')

@persistent
def _load(*args):
    failures=actions.finish_previews()
    if failures:print('C94换文件前预览恢复信息',failures)
    actions._preview.clear()

@persistent
def _save(*args):
    failures=actions.finish_previews()
    if failures:print('C94重定向：外部改动已保留，恢复信息见Text',failures)

@persistent
def _undo(*args):_save()

_CLASSES=(C94RETARGET_Options,C94RETARGET_OT_import,C94RETARGET_OT_import_bvh,C94RETARGET_OT_action,C94RETARGET_PT_body)

def register():
    global _OWNED
    if hasattr(bpy.types.Scene,OPTIONS):raise RuntimeError('重定向界面已由其他实例注册，原实例保持')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94RETARGET_Options));_OWNED=True
        bpy.app.handlers.load_pre.append(_load);bpy.app.handlers.save_pre.append(_save)
        bpy.app.handlers.undo_pre.append(_undo);bpy.app.handlers.redo_pre.append(_undo)
    except Exception:unregister();raise

def unregister():
    global _OWNED
    for handler,fn in ((bpy.app.handlers.load_pre,_load),(bpy.app.handlers.save_pre,_save),
                       (bpy.app.handlers.undo_pre,_undo),(bpy.app.handlers.redo_pre,_undo)):
        if fn in handler:handler.remove(fn)
    _save()
    if _OWNED and hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _OWNED=False
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
