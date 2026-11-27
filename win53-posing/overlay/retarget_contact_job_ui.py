# SPDX-License-Identifier: GPL-3.0-or-later
"""Visible contact-correction queue and fresh-review flow; no approval shortcut."""
import json
from pathlib import Path
import bpy
from bpy.props import PointerProperty,StringProperty,IntProperty,FloatProperty,EnumProperty,BoolProperty
from bpy.app.handlers import persistent
from . import retarget_job as jobs,retarget_job_ui as shared,retarget_contact_job as adapter
from . import retarget_contact_correction as core,retarget_action as rt

OPTIONS='c94_contact_job_options_v1'
_registered=[];_owned=False;_owns_options=False
def _opts(context):return getattr(context.scene,OPTIONS)
def _rig(_self,obj):return obj.type=='ARMATURE'
def _document(text):
    core.require(text is not None,'请选择明确JSON Text')
    value=json.loads(text.as_string());core.require(isinstance(value,dict),'JSON Text须为对象');return value
def _session(context):
    session=shared.session_for(context.scene)
    core.require(session and session.get('entry')==adapter.KIND,'当前没有本次足底后台任务RAM句柄')
    core.require(session['target']==_opts(context).target,'目标选择已变化；请选回本任务目标')
    return session
def _candidate(context,session):
    rows=session['cache'].get('items',[]);index=_opts(context).selected
    core.require(index<len(rows),'候选序号超出范围')
    action=session['collected'].get(rows[index]['id']);core.require(action is not None,'先收集该成功条目')
    return action
def _plan(session):
    plan=session.get('contact_plan');core.require(plan is not None,'先明确重新审阅所选候选，不从磁盘恢复批准');return plan
def _ended_plan(session):
    plan=session.get('contact_plan')
    core.require(plan is None or plan.status not in {'preview','applied'},'先停止预览或撤回已应用候选')


def submit_items(context, target, items, root, max_seconds):
    """Use the existing correction queue and publish a session after enqueue."""
    job = jobs.enqueue_contact_corrections(target, items, settings={'max_seconds': max_seconds}, jobs_root=str(root))
    shared._SESSIONS[shared._scene_key(context.scene)] = {
        'scene': context.scene, 'source': target, 'target': target, 'job': job,
        'cache': jobs.peek_status(job), 'selected': 0, 'collected': {},
        'entry': adapter.KIND, 'contact_plan': None,
    }
    return job


class C94CONTACTJOB_Options(bpy.types.PropertyGroup):
    target:PointerProperty(name='检测报告原目标',type=bpy.types.Object,poll=_rig)
    retained_action:PointerProperty(name='保留的独立修正候选',type=bpy.types.Action)
    report_text:PointerProperty(name='检测报告JSON',type=bpy.types.Text)
    calibration_text:PointerProperty(name='表面sole或三骨锚点校准JSON',type=bpy.types.Text)
    issue_id:StringProperty(name='精确问题ID')
    start:IntProperty(name='修正开始帧',default=3)
    end:IntProperty(name='修正结束帧',default=4)
    gait_joint:BoolProperty(name='按走跑支撑联合修正', default=False)
    tangent_release:BoolProperty(name='保留片段水平滑步', default=False)
    release_start:IntProperty(name='滑步开始帧', default=0)
    release_end:IntProperty(name='滑步结束帧', default=0)
    release_blend:IntProperty(name='两端过渡帧', default=5, min=1)
    clearance_m:FloatProperty(name='离地米',default=0.,min=0)
    max_delta_m:FloatProperty(name='最大位移米',default=.05,min=.00001)
    match_tolerance_m:FloatProperty(name='匹配误差米',default=.001,min=.000001)
    solve_tolerance_m:FloatProperty(name='解算误差米',default=.0001,min=.000001)
    jobs_root:StringProperty(name='后台工作目录',subtype='DIR_PATH')
    max_seconds:FloatProperty(name='预算秒',default=600,min=1,max=86400)
    selected:IntProperty(name='结果序号',default=0,min=0)
    message:StringProperty(name='状态',default='明确测量模式校准及一个问题；后台生成独立候选')

class C94CONTACTJOB_OT_run(bpy.types.Operator):
    bl_idname='c94_workflow.contact_correction_background'
    bl_label='足底逐问题后台修正'
    operation:EnumProperty(items=[(s,s,'') for s in ('ENQUEUE','REFRESH','CANCEL','COLLECT','REVIEW','REOPEN_SELECTED','PREVIEW','STOP','WITHDRAW','RELEASE')])
    def execute(self,context):
        opts=_opts(context)
        try:
            if self.operation=='ENQUEUE':
                core.require(shared.session_for(context.scene) is None,'当前Scene已有后台任务；先完成或取消并释放')
                core.require(opts.target is not None,'请明确原目标与原基础Action')
                core.require(opts.end>opts.start,'至少两帧，结束帧需大于开始帧')
                root=Path(bpy.path.abspath(opts.jobs_root)) if opts.jobs_root.strip() else None
                core.require(root and root.is_absolute(),'请选择明确绝对后台工作目录')
                item={'kind':adapter.KIND,'detection_report':_document(opts.report_text),'calibration':_document(opts.calibration_text),
                    'issue_id':opts.issue_id,'frames':list(range(opts.start,opts.end+1)),
                    'parameters':{name:getattr(opts,name) for name in adapter.PARAMETERS}}
                if opts.gait_joint:
                    rows = [row for row in item['detection_report'].get('issues', ()) if row['id'] == opts.issue_id]
                    core.require(len(rows) == 1, '联合支撑修正需要精确的问题')
                    release = {'start': opts.release_start, 'end': opts.release_end,
                               'blend_frames': opts.release_blend} if opts.tangent_release else None
                    item['support_window'] = core.gait_support_window(
                        item['detection_report'], rows[0], item['calibration'], item['frames'], tangent_release=release)
                submit_items(context, opts.target, [item], root, opts.max_seconds)
                opts.message='已提交原队列；当前基础Action保持，不批准问题'
            elif self.operation=='REOPEN_SELECTED':
                core.require(shared.session_for(context.scene) is None,'先结束/释放现有同Scene任务')
                target=opts.target;core.require(target is not None,'请明确绑定原基础Action的目标')
                action=opts.retained_action;core.require(action is not None,'请选择保留的独立修正候选Action')
                plan=adapter.reopen_for_review(target,action)
                shared._SESSIONS[shared._scene_key(context.scene)]={'scene':context.scene,'source':target,'target':target,
                    'job':None,'cache':{'terminal':True,'status':'fresh_review','items':[]},'selected':0,
                    'collected':{},'entry':adapter.KIND,'contact_plan':plan,'review_only':True}
                opts.message='已建立新的RAM审阅；没有恢复磁盘批准或后台收集权'
            else:
                session=_session(context);job=session['job']
                if self.operation in {'REFRESH','CANCEL','COLLECT'}:core.require(job is not None,'这是保留候选的独立审阅，没有后台Job')
                if self.operation=='REFRESH':session['cache']=jobs.peek_status(job)
                elif self.operation=='CANCEL':jobs.cancel(job);opts.message='已请求取消；成功候选保留'
                elif self.operation=='COLLECT':
                    receipt=jobs.collect_successes(job)
                    for row in receipt['collected']:session['collected'][row['item_id']]=jobs._existing_marker(job.id,row['item_id'])
                    opts.message='成功Action已收集；尚无审阅Plan或批准，当前绑定不变'
                elif self.operation=='REVIEW':
                    _ended_plan(session);action=_candidate(context,session)
                    session['contact_plan']=adapter.reopen_for_review(opts.target,action)
                    opts.message='已新建RAM审阅Plan；原基础Action必须保持明确绑定'
                elif self.operation=='PREVIEW':_plan(session).preview();opts.message='预览这个问题；需另行可见确认才正式应用'
                elif self.operation=='STOP':_plan(session).stop_preview();opts.message='已停止并恢复原基础动作'
                elif self.operation=='WITHDRAW':_plan(session).withdraw();opts.message='已撤回本次明确应用；独立候选保留'
                else:
                    core.require(session['cache'].get('terminal'),'后台任务仍运行，请先取消或等结束')
                    _ended_plan(session);shared._SESSIONS.pop(shared._scene_key(context.scene),None);opts.message='已释放RAM任务，磁盘候选保留'
            shared._redraw();return {'FINISHED'}
        except Exception as exc:opts.message=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94CONTACTJOB_PT_panel(bpy.types.Panel):
    bl_idname='C94CONTACTJOB_PT_panel';bl_label='足底逐问题修正'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向'
    def draw(self,context):
        layout=self.layout;opts=_opts(context)
        layout.prop(opts,'target');layout.prop(opts,'report_text');layout.prop(opts,'calibration_text')
        layout.label(text='表面需完整sole；骨锚点需显式三点/地面，不签皮肤足底')
        layout.prop(opts,'issue_id');row=layout.row(align=True);row.prop(opts,'start');row.prop(opts,'end')
        layout.prop(opts,'gait_joint')
        if opts.gait_joint:
            layout.prop(opts,'tangent_release')
            if opts.tangent_release:
                row=layout.row(align=True);row.prop(opts,'release_start');row.prop(opts,'release_end')
                layout.prop(opts,'release_blend')
        for field in ('clearance_m','max_delta_m','match_tolerance_m','solve_tolerance_m'):layout.prop(opts,field)
        layout.prop(opts,'jobs_root');layout.prop(opts,'max_seconds')
        session=shared.session_for(context.scene)
        if session is None:
            layout.operator(C94CONTACTJOB_OT_run.bl_idname,text='后台生成这个问题的独立候选').operation='ENQUEUE'
            layout.prop(opts,'retained_action')
            layout.operator(C94CONTACTJOB_OT_run.bl_idname,text='重新审阅保留候选（不恢复批准）').operation='REOPEN_SELECTED'
        elif session.get('entry')==adapter.KIND:
            layout.label(text=str(session['cache'].get('status','读取中')))
            for row in session['cache'].get('items',[]):layout.label(text=f'{row["index"]}: {row.get("contact_issue_id","")} {row.get("state","")}')
            if not session.get('review_only'):
                layout.prop(opts,'selected')
                for op,label in [('REFRESH','刷新进度'),('CANCEL','取消并保留成功'),('COLLECT','收集成功候选'),('REVIEW','重新审阅所选候选')]:
                    layout.operator(C94CONTACTJOB_OT_run.bl_idname,text=label).operation=op
            for op,label in [('PREVIEW','预览此问题'),('STOP','停止并恢复')]:
                layout.operator(C94CONTACTJOB_OT_run.bl_idname,text=label).operation=op
            plan=session.get('contact_plan')
            if plan:
                layout.label(text=f'{plan.issue_id} / {plan.kind} / {plan.status}')
                window = plan.parameters.get('support_window')
                if window:
                    layout.label(text=f"联合修正：第 {window['touchdown_frame']} 帧着地")
                    if window['tangent_release']:
                        release = window['tangent_release']
                        layout.label(text=f"第 {release['start']}–{release['end']} 帧保留滑步")
                if plan.calibration.get('measurement_mode')=='bone_anchors':
                    layout.label(text='骨锚点模式（非皮肤足底）；只审阅此精确问题')
                    layout.label(text='检测校准：'+plan.calibration['detection_calibration_sha256'])
                row=layout.row();row.enabled=plan.status=='preview';row.operator('c94.contact_correction_confirm',text='确认应用这个问题').plan_token=plan.token
                row=layout.row();row.enabled=plan.status=='applied';row.operator(C94CONTACTJOB_OT_run.bl_idname,text='撤回本次应用').operation='WITHDRAW'
            layout.operator(C94CONTACTJOB_OT_run.bl_idname,text='释放任务').operation='RELEASE'
        else:layout.label(text='当前Scene已有其他后台任务；沿用同一队列')
        layout.label(text=opts.message)

def _timer():
    if not _owned:return None
    if not shared._OWNED:shared.poll_runtime_once()
    return .75
@persistent
def _invalidate(*_args):
    for key,value in list(shared._SESSIONS.items()):
        if value.get('entry')==adapter.KIND:shared._SESSIONS.pop(key,None)
def register():
    global _owned,_owns_options
    core.require(not _owned,'足底后台界面已注册')
    core.require(not hasattr(bpy.types.Scene,OPTIONS),'足底后台Scene属性已被占用')
    try:
        for cls in (C94CONTACTJOB_Options,C94CONTACTJOB_OT_run,C94CONTACTJOB_PT_panel):bpy.utils.register_class(cls);_registered.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94CONTACTJOB_Options));_owns_options=True
        bpy.app.handlers.load_post.append(_invalidate);_owned=True;bpy.app.timers.register(_timer,first_interval=.75)
    except Exception:unregister();raise
def unregister():
    global _owned,_owns_options
    _owned=False
    if bpy.app.timers.is_registered(_timer):bpy.app.timers.unregister(_timer)
    if _invalidate in bpy.app.handlers.load_post:bpy.app.handlers.load_post.remove(_invalidate)
    if _owns_options and hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _owns_options=False
    for cls in reversed(_registered):bpy.utils.unregister_class(cls)
    _registered.clear()
