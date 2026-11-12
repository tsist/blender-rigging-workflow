# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional explicit review dialog; candidate generation stays in background jobs."""
import bpy
from . import retarget_contact_correction as core

_REGISTERED = False


class C94_OT_contact_correction_confirm(bpy.types.Operator):
    bl_idname = 'c94.contact_correction_confirm'
    bl_label = '确认应用这个足底问题'
    bl_options = {'REGISTER'}
    plan_token: bpy.props.StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        try:
            plan = core._plans[self.plan_token]
            plan.validate()
            core.require(plan.status == 'preview', '先预览该独立候选')
            core.require(context.window is not None and not bpy.app.background, '需要可见用户确认窗口')
            self._armed = (plan.token, plan.review_fingerprint)
            return context.window_manager.invoke_props_dialog(self, width=590)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

    def draw(self, context):
        plan = core._plans.get(self.plan_token)
        if plan is None:
            self.layout.label(text='候选已失效，请重新预览')
            return
        self.layout.label(text='只应用下列一个问题；原基础Action保留，可从专项入口撤回。')
        self.layout.label(text='问题ID：' + plan.issue_id)
        if plan.calibration.get('measurement_mode') == 'bone_anchors':
            self.layout.label(text='测量：明确骨局部接触标记，未采样皮肤足底')
            self.layout.label(text='检测校准：' + plan.calibration['detection_calibration_sha256'])
        self.layout.label(text=f'{plan.kind} / {plan.calibration["side"]} / {plan.region}')
        self.layout.label(text=f'帧域：{plan.frames[0]}—{plan.frames[-1]}，另写两侧边界键')
        self.layout.label(text='独立候选：' + plan.action.name)
        self.layout.label(text='拟改曲线：' + str(len(plan.report['curve_changes'])))
        for path in sorted({row['path'] for row in plan.report['curve_changes']}):
            self.layout.label(text=path)
        self.layout.label(text=f'允许最大位移：{plan.parameters["max_delta_m"]*1000:g} mm；求值残差门：{plan.parameters["solve_tolerance_m"]*1000:g} mm')
        self.layout.label(text='旧辅助动态不自动重烘焙；候选参数和完整曲线差异保存在Action专项记录。')

    def execute(self, context):
        try:
            plan = core._plans[self.plan_token]
            core.require(getattr(self, '_armed', None) == (plan.token, plan.review_fingerprint),
                         '必须经本次可见逐问题确认；直接脚本execute不能自动批准')
            approval = core._user_approval(plan, exact_issue_ids=[plan.issue_id], review_fingerprint=plan.review_fingerprint)
            plan.apply(approval, approved_issue_ids=[plan.issue_id], review_fingerprint=plan.review_fingerprint)
            self._armed = None
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def register():
    global _REGISTERED
    core.require(not _REGISTERED, '足底修正确认入口已注册，原实例保持')
    bpy.utils.register_class(C94_OT_contact_correction_confirm)
    _REGISTERED = True


def unregister():
    global _REGISTERED
    if _REGISTERED:
        bpy.utils.unregister_class(C94_OT_contact_correction_confirm)
        _REGISTERED = False
