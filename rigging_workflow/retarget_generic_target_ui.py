# SPDX-License-Identifier: GPL-3.0-or-later
"""Configured target bones and explicit detached-FK fallback, shared rt core."""
import json
import bpy
from bpy.props import BoolProperty,FloatProperty,IntProperty,PointerProperty,StringProperty
from . import retarget_ui as body,retarget_action as rt,retarget_source as sources
from . import retarget_generic_target as generic

OPTIONS='c94_retarget_generic_target_options_v1'
_CLASSES=[]
_OWNED=False

def require(value,message):
    if not value:raise ValueError(message)

def _ack(self,context):self.ack_target=getattr(context.scene,body.OPTIONS).target if self.acknowledged else None
def _mapping_changed(self,_context):self.acknowledged=False

class C94GENERIC_Options(bpy.types.PropertyGroup):
    target_mapping:StringProperty(name='目标语义 → 实际骨名 JSON',default='{}',update=_mapping_changed)
    acknowledged:BoolProperty(name='明确回退为骨骼动作（不是控制器动画）',default=False,update=_ack)
    ack_target:PointerProperty(type=bpy.types.Object)
    isolate:BoolProperty(name='明确另建原静置姿态派生FK骨架，不绑定原网格',default=False)
    start:FloatProperty(name='源起帧',default=1)
    end:FloatProperty(name='源止帧',default=161)
    output_start:IntProperty(name='目标起帧',default=1)
    message:StringProperty(default='已适配控制器用原入口；未知控制器仅明确骨骼回退')
    report_text:StringProperty(name='检查报告文本数据块',default='')
    contact_mesh:PointerProperty(name='原 FK 蒙皮网格', type=bpy.types.Object,
                                 poll=lambda _self, obj: obj.type == 'MESH')
    contact_calibration:PointerProperty(name='脚跟支撑校准', type=bpy.types.Text)
    contact_candidate:PointerProperty(name='独立 FK 支撑动作', type=bpy.types.Action)

class C94GENERIC_OT_action(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_generic_target'
    bl_label='普通FK目标骨骼回退'
    operation:StringProperty(default='INSPECT')
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS);current=getattr(context.scene,body.OPTIONS)
        try:
            require(self.operation in ('INSPECT','MIXAMO_TARGET_MAP','CONVERT','ISOLATE','PREVIEW','STOP','FK_CONTACT'),'未知通用目标操作')
            require(context.mode in ('OBJECT','POSE'),'请先退出编辑模式')
            target=sources.armature(rt.preview_target(context.scene) if self.operation=='STOP' else current.target)
            if self.operation=='INSPECT':
                conflicts=generic.conflicts(target)
                report={'controller_tier':'needs_controller_adapter' if conflicts else 'configured_fk_candidate',
                    'conflicts':conflicts,'controller_animation_supported':False,'fallback_requires_explicit_confirmation':True}
                text=bpy.data.texts.new('通用目标检查');text.use_fake_user=True;text.write(json.dumps(report,ensure_ascii=False,indent=2));opts.report_text=text.name
                opts.message='发现未适配控制图：原rig拒绝写入，可明确另建FK骨架' if conflicts else '普通FK骨候选；仍需完整映射与明确骨骼回退确认'
            elif self.operation=='MIXAMO_TARGET_MAP':
                proposed=sources.mapping(target)['bones']
                # Source semantics restrict this proposal to the requested
                # source profile; it never infers arbitrary target controllers.
                source=sources.armature(current.source)
                source_map=sources.exact_cmu_mapping(source) if current.source_kind=='CMU' else sources.mapping(source)
                opts.target_mapping=json.dumps({k:v for k,v in proposed.items() if k in source_map['bones']},ensure_ascii=False)
                opts.message='已填已知Mixamo骨名建议；请核对后明确选择骨骼回退'
            elif self.operation=='ISOLATE':
                require(opts.isolate,'请明确选择另建独立FK骨架；不改原Mesh引用')
                result=generic.isolate_target(target,acknowledged=True);current.target=result['rig']
                opts.isolate=False;opts.acknowledged=False
                opts.message='已另建原rest派生骨架；原图和Mesh保持，新骨架没有原控制器行为'
            elif self.operation=='CONVERT':
                require(opts.acknowledged and opts.ack_target==target,'请对当前目标明确确认骨骼Action回退')
                require(current.source_kind in ('MIXAMO','CMU'),'首版回退界面只接已验Mixamo/CMU来源；其他源须先适配')
                source=sources.armature(current.source)
                if current.source_kind=='CMU':mapped=sources.exact_cmu_mapping(source)
                else:mapped=sources.mapping(source,json.loads(current.mapping_json) or None)
                mapping=json.loads(opts.target_mapping)
                require(isinstance(mapping,dict),'目标映射须为JSON对象')
                converted=generic.convert(source,target,source_mapping=mapped,target_mapping=mapping,
                    source_fps=current.source_fps if current.override_fps else None,start=opts.start,end=opts.end,
                    output_start=opts.output_start,motion=current.motion,acknowledged=True)
                from . import retarget_contact_detection_ui as detection_ui
                detection_ui.complete_from_context(context,source,target,converted)
                current.candidate=converted['action'];opts.acknowledged=False
                opts.message='已生成未绑定可编辑骨骼Action；需查看姿态/蒙皮，不代表控制器适配'
            elif self.operation=='FK_CONTACT':
                from . import retarget_fk_contact as contact
                require(opts.contact_mesh is not None and opts.contact_calibration is not None,
                        '请选择该角色的蒙皮网格和脚跟支撑校准')
                document = json.loads(opts.contact_calibration.as_string())
                opts.contact_candidate = contact.build(target, opts.contact_mesh, document)
                opts.message = f'已保留原 FK 槽中的独立动作 {opts.contact_candidate.name}'
            elif self.operation=='PREVIEW':
                require(current.candidate and generic.META in current.candidate,'请选择本入口通用目标候选')
                rt.begin_preview(target,current.candidate);opts.message='同一共核临时预览中；保存前恢复原状态'
            else:rt.stop_preview(target);opts.message='已通过原共核恢复旧动作与输入'
            return {'FINISHED'}
        except Exception as exc:opts.message=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94GENERIC_PT_panel(bpy.types.Panel):
    bl_idname='C94GENERIC_PT_panel';bl_label='未知控制器：骨骼动作回退'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向';bl_parent_id='C94RETARGET_PT_body'
    def draw(self,context):
        layout=self.layout;opts=getattr(context.scene,OPTIONS)
        layout.label(text='控制器已适配：使用原身体入口')
        layout.label(text='普通FK：明确映射后只输出骨骼动作')
        layout.label(text='未知控制图：原骨架拒绝写入，可另建隔离骨架')
        row=layout.row(align=True)
        row.operator('c94_workflow.retarget_generic_target',text='检查目标与冲突').operation='INSPECT'
        row.operator('c94_workflow.retarget_generic_target',text='读取Mixamo骨名建议').operation='MIXAMO_TARGET_MAP'
        layout.prop(opts,'target_mapping')
        row=layout.row(align=True);row.prop(opts,'start');row.prop(opts,'end');layout.prop(opts,'output_start')
        layout.prop(opts,'acknowledged')
        layout.operator('c94_workflow.retarget_generic_target',text='生成未绑定骨骼动作').operation='CONVERT'
        row=layout.row(align=True)
        row.operator('c94_workflow.retarget_generic_target',text='临时预览候选').operation='PREVIEW'
        row.operator('c94_workflow.retarget_generic_target',text='停止并恢复').operation='STOP'
        box=layout.box();box.prop(opts,'isolate')
        box.operator('c94_workflow.retarget_generic_target',text='另建隔离FK骨架').operation='ISOLATE'
        box.label(text='沿用原静置姿态，生成单独的试动骨架')
        layout.label(text=opts.message[:72],icon='INFO')
        if opts.report_text:layout.prop(opts,'report_text')
        contact = layout.box()
        contact.label(text='原 FK 脚跟支撑')
        contact.prop(opts,'contact_mesh')
        contact.prop(opts,'contact_calibration')
        contact.operator('c94_workflow.retarget_generic_target',text='生成独立支撑动作').operation='FK_CONTACT'
        if opts.contact_candidate:
            contact.label(text=opts.contact_candidate.name)

def register():
    global _OWNED
    require(hasattr(bpy.types.Scene,body.OPTIONS),'请先注册身体重定向界面')
    require(not hasattr(bpy.types.Scene,OPTIONS),'通用目标界面已注册，原实例保持')
    try:
        for cls in (C94GENERIC_Options,C94GENERIC_OT_action,C94GENERIC_PT_panel):bpy.utils.register_class(cls);_CLASSES.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94GENERIC_Options));_OWNED=True
    except Exception:unregister();raise

def unregister():
    global _OWNED
    if _OWNED and hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _OWNED=False
    for cls in reversed(_CLASSES):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
