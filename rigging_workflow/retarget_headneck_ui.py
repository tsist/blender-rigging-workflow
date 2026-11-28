# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared head/neck preview button and reversible Operator."""
import bpy
from bpy.props import StringProperty
from . import retarget_action as rt,retarget_headneck as headneck


class C94RETARGET_OT_headneck_preview(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_headneck_preview'
    bl_label='生成头颈分担预览'
    bl_description='从当前生成基础动作的原始控件意图分担头颈转动，保留原动作和原生限位，待视觉检查'
    bl_options={'REGISTER'}
    target_name:StringProperty(name='目标骨架',default='',options={'HIDDEN'})
    source_action_name:StringProperty(name='输入基础动作',default='',options={'HIDDEN'})

    def execute(self,context):
        rig=bpy.data.objects.get(self.target_name) if self.target_name else context.active_object
        try:
            if not isinstance(rig,bpy.types.Object) or rig.type!='ARMATURE':raise ValueError('请选择目标角色骨架')
            action=bpy.data.actions.get(self.source_action_name) if self.source_action_name else (rig.animation_data.action if rig.animation_data else None)
            if action is None:raise ValueError('请指定已有生成基础Action')
            if rt.has_preview(rig):rt.stop_preview(rig)
            result=headneck.rebalance_action(rig,action=action)
            rt.begin_preview(rig,result['action'],slot=result['slot'])
            for name in ('c94_retarget_options_v1','c94_retarget_mmd_options_v1'):
                options=getattr(context.scene,name,None)
                if options is not None and options.target==rig and (options.candidate is None or options.candidate==action):
                    options.candidate=result['action'];options.summary='头颈分担候选预览中；请检查自然性，停止可恢复原动作'
            self.report({'INFO'},'已生成独立头颈分担预览；原动作保留，请检查运动自然性')
            return {'FINISHED'}
        except headneck.HeadNeckRestorationError as exc:
            self.report({'ERROR'},str(exc));raise
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


CLASSES=(C94RETARGET_OT_headneck_preview,)
_REGISTERED=[]


def draw_button(layout,target,candidate):
    action=candidate
    if action is None and target is not None and target.animation_data is not None:
        action=target.animation_data.action
    row=layout.row()
    row.enabled=target is not None and action is not None
    button=row.operator(C94RETARGET_OT_headneck_preview.bl_idname)
    button.target_name=target.name if target is not None else ''
    button.source_action_name=action.name if action is not None else ''


def register():
    if _REGISTERED:raise RuntimeError('头颈预览操作符已注册')
    try:
        for cls in CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
    except Exception:
        unregister();raise


def unregister():
    for cls in reversed(_REGISTERED):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
