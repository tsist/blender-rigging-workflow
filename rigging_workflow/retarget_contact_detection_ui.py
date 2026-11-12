# SPDX-License-Identifier: GPL-3.0-or-later
"""A narrow read-only detection frontend over the existing job and report Text."""
import json
from pathlib import Path
import bpy
from bpy.props import PointerProperty,StringProperty,IntProperty,FloatProperty,EnumProperty
from bpy.app.handlers import persistent
from . import retarget_contact_detection as core,retarget_job as jobs,retarget_job_ui as shared
from . import retarget_action as rt

OPTIONS='c94_contact_detection_options_v1'
_owned=False;_owns_options=False;_classes=[]
def _opts(context):return getattr(context.scene,OPTIONS)
def _armature(_self,obj):return obj.type=='ARMATURE'
def _require(value,message):core.require(value,message)
def _session(context):
    value=shared.session_for(context.scene)
    _require(value and value.get('entry')==core.KIND,'没有本次足底检测/报告会话')
    _require(value['target']==_opts(context).target,'目标选择已变化，请选回原目标')
    return value
def _report(text):
    _require(text is not None,'请先收集或选择只读报告Text')
    raw=text.get(core.TEXT_MARKER);_require(isinstance(raw,str),'该Text没有已收集检测报告标记')
    marker=json.loads(raw);document=json.loads(text.as_string());core.validate_report(document)
    _require(core.digest(document)==marker['document_sha256'],'报告Text被修改，保留现内容但不能当原精确报告传给修正')
    return document
def calibration_from_context(context):
    """Freeze only the explicitly selected JSON; no live GUI reads in worker."""
    opts=getattr(context.scene,OPTIONS,None)
    if opts is None or opts.calibration_text is None:return {}
    doc=json.loads(opts.calibration_text.as_string())
    _require(isinstance(doc,dict),'检测校准必须为JSON对象')
    return core.pure(doc)

def automatic_report_text(action):
    document=json.loads(action.get(rt.META,'{}')).get('contact_detection')
    core.validate_report(document)
    marker={'automatic_action':action.name,'document_sha256':core.digest(document)}
    text=bpy.data.texts.new('C94_自动足底只读报告_'+action.name)
    text.write(json.dumps(document,ensure_ascii=False,indent=2));text[core.TEXT_MARKER]=json.dumps(marker)
    return text

def complete_from_context(context,source,target,converted):
    from . import retarget_pipeline
    try:doc=calibration_from_context(context)
    except (ValueError,TypeError) as exc:
        doc={'schema':'invalid_selected_calibration','error':str(exc)}
    retarget_pipeline.complete_detection(source,target,converted,calibration=doc)
    opts=getattr(context.scene,OPTIONS,None)
    if opts is not None:
        opts.source=source;opts.target=target;opts.target_action=converted['action']
        opts.target_slot=converted['slot'].identifier
        opts.report_text=automatic_report_text(converted['action'])
        opts.message='转换后已检测：'+converted['report']['contact_detection']['status']+'；无批准权'
    return converted

def _inherit(context,kind):
    from . import retarget_ui as body
    options=getattr(context.scene,body.OPTIONS,None)
    if kind=='MMD':
        from . import retarget_mmd_ui as mmd
        options=getattr(context.scene,mmd.OPTIONS,None)
    _require(options and options.source and options.target and options.candidate,'原面板需明确source/target/基础候选')
    opts=_opts(context);opts.source=options.source;opts.target=options.target;opts.target_action=options.candidate
    metadata=json.loads(options.candidate.get(rt.META,'{}'));name=metadata.get('source_action')
    _require(isinstance(name,str) and name,'候选缺少源Action记录；拒绝继承，请手工选择或重新转换')
    source_action=bpy.data.actions.get(name)
    _require(source_action is not None,
             '候选记录的源Action已不存在（%s）；拒绝回退到当前不同Action，请手工选择或重新转换'%name)
    opts.source_action=source_action
    source_slot=metadata.get('source_slot')
    if not source_slot and options.source.animation_data and options.source.animation_data.action==opts.source_action:
        source_slot=options.source.animation_data.action_slot.identifier
    _require(source_slot,'需明确源Action Slot，不猜多Slot')
    opts.source_slot=source_slot
    slots=[s for s in opts.target_action.slots if s.target_id_type=='OBJECT'];_require(len(slots)==1,'需手工明确目标Slot')
    opts.target_slot=slots[0].identifier
    if metadata.get('contact_detection',{}).get('schema')==core.SCHEMA:opts.report_text=automatic_report_text(options.candidate)
    opts.time_summary=json.dumps({k:metadata.get(k) for k in ('source_fps','source_range','target_fps','output_range','motion')},ensure_ascii=False)
    opts.message='已读取身份/时基；不会猜Mesh、地面、足底索引或帧秒对齐'

class C94CONTACTDETECT_Options(bpy.types.PropertyGroup):
    source:PointerProperty(name='明确源骨架',type=bpy.types.Object,poll=_armature)
    source_action:PointerProperty(name='源Action',type=bpy.types.Action)
    source_slot:StringProperty(name='源Slot标识')
    target:PointerProperty(name='候选目标骨架',type=bpy.types.Object,poll=_armature)
    target_action:PointerProperty(name='目标基础候选Action',type=bpy.types.Action)
    target_slot:StringProperty(name='目标Slot标识')
    calibration_text:PointerProperty(name='双方表面或三骨锚点/地面/帧秒校准JSON',type=bpy.types.Text)
    report_text:PointerProperty(name='只读报告Text',type=bpy.types.Text)
    jobs_root:StringProperty(name='后台工作目录',subtype='DIR_PATH')
    max_seconds:FloatProperty(name='预算秒',default=600,min=1,max=86400)
    selected:IntProperty(name='报告结果序号',default=0,min=0)
    selected_issue:IntProperty(name='问题序号',default=0,min=0)
    issue_id:StringProperty(name='精确问题ID')
    time_summary:StringProperty(name='候选时基说明')
    message:StringProperty(name='状态',default='尚未检测：需明确测量模式、双方校准、地面与帧秒对齐')

class C94CONTACTDETECT_OT_run(bpy.types.Operator):
    bl_idname='c94_workflow.contact_detection_background'
    bl_label='基础候选足底只读检测'
    operation:EnumProperty(items=[(s,s,'') for s in ('USE_BODY','USE_MMD','ENQUEUE','REFRESH','CANCEL','COLLECT','READ_AUTOMATIC','VIEW_REPORT','SELECT_ISSUE','TRANSFER','RELEASE')])
    def execute(self,context):
        opts=_opts(context)
        try:
            if self.operation in {'USE_BODY','USE_MMD'}:_inherit(context,self.operation[4:])
            elif self.operation=='ENQUEUE':
                _require(shared.session_for(context.scene) is None,'当前Scene已有后台/专项会话，请先完成并释放')
                _require(opts.source and opts.target and opts.source_action and opts.target_action,'明确source/target与双方Action')
                root=Path(bpy.path.abspath(opts.jobs_root)) if opts.jobs_root.strip() else None;_require(root and root.is_absolute(),'请明确绝对后台目录')
                doc=json.loads(opts.calibration_text.as_string()) if opts.calibration_text else {}
                item={'kind':core.KIND,'source_action':opts.source_action.name,'source_slot':opts.source_slot,
                    'target_action':opts.target_action.name,'target_slot':opts.target_slot,'calibration':doc}
                job=jobs.enqueue_contact_detection(opts.source,opts.target,[item],settings={'max_seconds':opts.max_seconds},jobs_root=str(root))
                shared._SESSIONS[shared._scene_key(context.scene)]={'scene':context.scene,'source':opts.source,'target':opts.target,
                    'job':job,'cache':jobs.peek_status(job),'selected':0,'collected':{},'entry':core.KIND,'report_cache':None}
                opts.message='已提交只读检测；原preview/绑定保持，不生成修正Action'
            elif self.operation=='READ_AUTOMATIC':
                _require(opts.target_action is not None,'先选择带自动检测结果的基础候选Action')
                opts.report_text=automatic_report_text(opts.target_action);opts.message='已从候选Action读取自动检测报告；可直接查看和精确转交'
            elif self.operation=='VIEW_REPORT':
                document=_report(opts.report_text);session=shared.session_for(context.scene)
                if session is None:
                    session={'scene':context.scene,'source':opts.source,'target':opts.target,'job':None,
                        'cache':{'terminal':True,'status':'report_view','items':[]},'selected':0,'collected':{},'entry':core.KIND}
                    shared._SESSIONS[shared._scene_key(context.scene)]=session
                _require(session.get('entry')==core.KIND,'当前会话由另一个专项管理')
                session['report_cache']=document;opts.message='只读报告：'+document['status']+'；无批准权'
            else:
                session=_session(context);job=session['job']
                if self.operation in {'REFRESH','CANCEL','COLLECT'}:_require(job is not None,'这是保存报告的查看会话，没有后台Job')
                if self.operation=='REFRESH':session['cache']=jobs.peek_status(job)
                elif self.operation=='CANCEL':jobs.cancel(job);opts.message='已请求取消；已成功报告保留'
                elif self.operation=='COLLECT':
                    receipt=jobs.collect_successes(job)
                    for row in receipt['collected']:
                        _require(row.get('artifact_type')=='CONTACT_REPORT','报告入口拒绝把Action冒充报告')
                        session['collected'][row['item_id']]=bpy.data.texts[row['text']]
                    values=list(session['collected'].values());_require(values and opts.selected<len(values),'没有所选成功报告')
                    opts.report_text=values[opts.selected];session['report_cache']=_report(opts.report_text)
                    opts.message='已收集JSON报告Text；Action数量/绑定保持，未批准问题'
                elif self.operation=='SELECT_ISSUE':
                    document=_report(opts.report_text);rows=(document.get('detection_report') or {}).get('issues',[])
                    _require(opts.selected_issue<len(rows),'问题序号超出范围');opts.issue_id=rows[opts.selected_issue]['id']
                elif self.operation=='TRANSFER':
                    from . import retarget_contact_job_ui as correction_ui
                    _require(hasattr(context.scene,correction_ui.OPTIONS),'先注册既有足底修正专项界面')
                    _require(session['cache'].get('terminal'),'检测仍在运行，不能转交/释放检测会话')
                    document=_report(opts.report_text);report,calibration,frames=core.correction_inputs(opts.target,document,opts.issue_id)
                    dest=getattr(context.scene,correction_ui.OPTIONS)
                    report_text=bpy.data.texts.new('C94_精确足底检测_'+opts.issue_id[:12]);report_text.write(json.dumps(report,ensure_ascii=False,indent=2))
                    calibration_text=bpy.data.texts.new('C94_精确足底修正校准_'+opts.issue_id[:12]);calibration_text.write(json.dumps(calibration,ensure_ascii=False,indent=2))
                    dest.target=opts.target;dest.report_text=report_text;dest.calibration_text=calibration_text;dest.issue_id=opts.issue_id
                    dest.start=min(frames);dest.end=max(frames)
                    shared._SESSIONS.pop(shared._scene_key(context.scene),None)
                    opts.message='精确报告/问题/校准已传原修正面板；尚未生成修正或批准，可另行后台生成'
                else:
                    _require(session['cache'].get('terminal'),'检测仍运行，先取消或等结束')
                    shared._SESSIONS.pop(shared._scene_key(context.scene),None);opts.message='已释放任务；报告/原动作保留'
            shared._redraw();return {'FINISHED'}
        except Exception as exc:opts.message=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94CONTACTDETECT_PT_panel(bpy.types.Panel):
    bl_idname='C94CONTACTDETECT_PT_panel';bl_label='基础候选 · 足底只读检测'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向'
    def draw(self,context):
        layout=self.layout;opts=_opts(context)
        row=layout.row(align=True)
        for op,label in [('USE_BODY','读取身体候选'),('USE_MMD','读取MMD候选')]:row.operator(C94CONTACTDETECT_OT_run.bl_idname,text=label).operation=op
        for name in ('source','source_action','source_slot','target','target_action','target_slot','calibration_text'):layout.prop(opts,name)
        if opts.time_summary:layout.label(text=opts.time_summary[:110])
        layout.label(text='表面模式需完整sole；骨锚点模式需每侧heel/ball/toe三点')
        layout.label(text='骨锚点是明确骨局部标记，不是皮肤足底；地面须显式校准')
        layout.label(text='手部/身体接触未分析；本入口不写修正曲线')
        layout.prop(opts,'jobs_root');layout.prop(opts,'max_seconds')
        session=shared.session_for(context.scene)
        if session is None:layout.operator(C94CONTACTDETECT_OT_run.bl_idname,text='后台只读检测').operation='ENQUEUE'
        elif session.get('entry')==core.KIND:
            cache=session['cache'];layout.label(text=str(cache.get('status','读取中')))
            if session['job'] is not None:
                for op,label in [('REFRESH','刷新进度'),('CANCEL','取消检测'),('COLLECT','收集报告')]:layout.operator(C94CONTACTDETECT_OT_run.bl_idname,text=label).operation=op
            document=session.get('report_cache')
            if document:
                layout.label(text='报告状态：'+document['status'])
                if document.get('measurement_mode')=='bone_anchors':
                    layout.label(text='骨锚点报告：declared_bone_anchors；未采样皮肤')
                for reason in document.get('reasons',[])[:8]:layout.label(text=str(reason.get('role',''))+' '+reason['code'])
                rows=(document.get('detection_report') or {}).get('issues',[])
                for i,row in enumerate(rows[:20]):
                    prefix='骨锚点 ' if row.get('measurement_mode')=='bone_anchors' else ''
                    layout.label(text=f'{i}: {prefix}{row["kind"]}/{row["side"]}/{row["region"]} {row["id"]}')
                layout.prop(opts,'selected_issue');layout.operator(C94CONTACTDETECT_OT_run.bl_idname,text='读取精确问题ID').operation='SELECT_ISSUE'
                layout.prop(opts,'issue_id');layout.operator(C94CONTACTDETECT_OT_run.bl_idname,text='传给原逐问题修正入口').operation='TRANSFER'
            layout.operator(C94CONTACTDETECT_OT_run.bl_idname,text='释放检测/查看任务').operation='RELEASE'
        else:layout.label(text='当前任务请在对应专项面板管理')
        layout.operator(C94CONTACTDETECT_OT_run.bl_idname,text='读取候选内自动报告').operation='READ_AUTOMATIC'
        layout.prop(opts,'report_text');layout.operator(C94CONTACTDETECT_OT_run.bl_idname,text='查看保留报告（无批准权）').operation='VIEW_REPORT'
        layout.label(text=opts.message[:130])

def _timer():
    if not _owned:return None
    if not shared._OWNED:shared.poll_runtime_once()
    return .75
@persistent
def _invalidate(*_args):
    for key,value in list(shared._SESSIONS.items()):
        if value.get('entry')==core.KIND:shared._SESSIONS.pop(key,None)
def register():
    global _owned,_owns_options
    _require(not _owned,'足底只读检测界面已注册');_require(not hasattr(bpy.types.Scene,OPTIONS),'检测Scene属性已占用')
    try:
        for cls in (C94CONTACTDETECT_Options,C94CONTACTDETECT_OT_run,C94CONTACTDETECT_PT_panel):bpy.utils.register_class(cls);_classes.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94CONTACTDETECT_Options));_owns_options=True
        bpy.app.handlers.load_post.append(_invalidate);_owned=True;bpy.app.timers.register(_timer,first_interval=.75)
    except Exception:unregister();raise
def unregister():
    global _owned,_owns_options
    _owned=False
    if bpy.app.timers.is_registered(_timer):bpy.app.timers.unregister(_timer)
    if _invalidate in bpy.app.handlers.load_post:bpy.app.handlers.load_post.remove(_invalidate)
    if _owns_options and hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _owns_options=False
    for cls in reversed(_classes):bpy.utils.unregister_class(cls)
    _classes.clear()
