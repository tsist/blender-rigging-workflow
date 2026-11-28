# SPDX-License-Identifier: GPL-3.0-or-later
"""Small MMD frontend to the existing queue, worker, ledger and RAM handles."""
import json
from pathlib import Path
import bpy
from bpy.props import PointerProperty,StringProperty,IntProperty,FloatProperty,EnumProperty
from bpy.app.handlers import persistent
from . import retarget_job as jobs,retarget_job_ui as shared,retarget_action as rt
from . import mmd_source_backend as backend

OPTIONS='c94_retarget_mmd_job_options_v1'
_registered=[]
_owned=False
_owns_options=False

def _opts(context):return getattr(context.scene,OPTIONS)
def _require(value,message):
    if not value:raise ValueError(message)
def _armature(_self,obj):return obj.type=='ARMATURE'

def _session(context):
    session=shared.session_for(context.scene)
    _require(session and session.get('entry')=='MMD','当前Scene没有本次MMD后台任务；磁盘目录不恢复RAM收集权')
    _require(session['target']==_opts(context).target,'所选目标已变化，请选回本任务原目标')
    return session

def _inherit(context):
    from . import retarget_mmd_ui as source_ui
    source=getattr(context.scene,source_ui.OPTIONS,None)
    _require(source and source.profile_text and source.source and source.target,'先在已登记MMD来源面板选择profile Text、源与目标')
    opts=_opts(context);opts.source=source.source;opts.target=source.target;opts.profile_text=source.profile_text
    opts.mapping_json=source.mapping_json
    opts.motion=source.motion
    item={'clip_id':source.clip_id,'fps':source.source_fps,'start':source.start,'end':source.end,'output_start':source.output_start}
    if source.include_face:
        from . import face_mouth_core as mouth
        group,record,rig,_=mouth._read(source.face_hub)
        _require(rig==source.target,'所选MOUTH实际rig与目标不同')
        item['face']={'group_id':group['group_id'],'mapping':json.loads(source.face_mapping_json),
                      'omitted_reasons':json.loads(source.face_omitted_json)}
    if source.include_gaze:item['gaze']=True
    opts.items_json=json.dumps([item],ensure_ascii=False)
    profile=backend.load_profile_document(opts.profile_text)
    opts.clip_summary='；'.join(f'{c["id"]}: {c["range"]} @{c["fps"]:g}fps'+(' 身体' if c['body_convertible'] else '仅源表情/视线') for c in profile['clips'])

class C94MMDJOB_Options(bpy.types.PropertyGroup):
    source:PointerProperty(name='已登记MMD源',type=bpy.types.Object,poll=_armature)
    target:PointerProperty(name='目标角色',type=bpy.types.Object,poll=_armature)
    profile_text:PointerProperty(name='已持久来源配置 JSON',type=bpy.types.Text)
    mapping_json:StringProperty(name='显式映射核对',default='')
    motion:EnumProperty(name='根运动',items=(('PRESERVE','保留移动与转向',''),('IN_PLACE','原地，保留转向','')),default='PRESERVE')
    items_json:StringProperty(name='明确片段/帧域/30fps列表JSON',default='[{"clip_id":"dance","fps":30,"start":225,"end":230}]')
    jobs_root:StringProperty(name='后台工作目录',subtype='DIR_PATH')
    max_seconds:FloatProperty(name='预算秒',default=1800,min=1,max=86400)
    threads:IntProperty(name='线程',default=2,min=1,max=8)
    selected:IntProperty(name='结果序号',default=0,min=0)
    message:StringProperty(name='状态',default='只读取已登记来源；长片在现有后台worker处理')
    clip_summary:StringProperty(name='登记片段',default='')

class C94MMDJOB_OT_run(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_mmd_background'
    bl_label='MMD后台动作'
    operation:EnumProperty(items=[(x,x,'') for x in ('USE_SELECTED','ENQUEUE','REFRESH','CANCEL','COLLECT','PREVIEW','STOP','RELEASE')])
    def execute(self,context):
        opts=_opts(context)
        try:
            if self.operation=='USE_SELECTED':
                _inherit(context);opts.message='已读取当前MMD选择；批量条目仍可用明确JSON编辑'
            elif self.operation=='ENQUEUE':
                _require(shared.session_for(context.scene) is None,'先完成/取消并释放当前同Scene后台任务')
                _require(opts.source and opts.target and opts.profile_text,'请明确已登记source/target/profile Text')
                root=Path(bpy.path.abspath(opts.jobs_root)) if opts.jobs_root.strip() else None
                _require(root and root.is_absolute(),'请选择明确绝对后台工作目录')
                profile=backend.load_profile_document(opts.profile_text)
                mapping=json.loads(opts.mapping_json) if opts.mapping_json.strip() else None
                from . import retarget_contact_detection_ui as detection_ui
                detection_calibration=detection_ui.calibration_from_context(context)
                job=jobs.enqueue(opts.source,opts.target,items=json.loads(opts.items_json),preset='MMD',
                    mapping=mapping,source_profile=profile,jobs_root=str(root),
                    settings={'max_seconds':opts.max_seconds,'threads':opts.threads,'motion':opts.motion,'contact_detection_calibration':detection_calibration})
                shared._SESSIONS[shared._scene_key(context.scene)]={'scene':context.scene,'source':opts.source,'target':opts.target,
                    'job':job,'cache':jobs.peek_status(job),'selected':0,'collected':{},'entry':'MMD'}
                opts.message='已提交同一后台队列；当前动作保持'
            else:
                session=_session(context);job=session['job']
                if self.operation=='REFRESH':session['cache']=jobs.peek_status(job)
                elif self.operation=='CANCEL':jobs.cancel(job);opts.message='已请求取消；成功产物保留'
                elif self.operation=='COLLECT':
                    receipt=jobs.collect_successes(job)
                    for row in receipt['collected']:
                        session['collected'][row['item_id']]=jobs._existing_marker(job.id,row['item_id'])
                    opts.message='已收集成功Action；Hub按实际ID恢复，当前绑定不变'
                elif self.operation=='PREVIEW':
                    rows=session['cache'].get('items',[]);_require(opts.selected<len(rows),'结果序号超出列表')
                    row=rows[opts.selected];action=session['collected'].get(row['id'])
                    _require(action is not None,'先收集所选成功条目')
                    jobs.validate_candidate_target(opts.target,action)
                    _require(not rt.has_preview(opts.target),'先停止当前预览')
                    rt.begin_preview(opts.target,action);opts.message='同一已登记clip候选预览中'
                elif self.operation=='STOP':rt.stop_preview(opts.target);opts.message='已停止并恢复原动作'
                else:
                    _require(session['cache'].get('terminal'),'任务仍在运行，先取消或等结束')
                    shared._SESSIONS.pop(shared._scene_key(context.scene),None);opts.message='已释放RAM界面句柄，磁盘输出保留'
            shared._redraw();return {'FINISHED'}
        except Exception as exc:
            opts.message=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94MMDJOB_PT_panel(bpy.types.Panel):
    bl_idname='C94MMDJOB_PT_panel';bl_label='MMD后台批量（已登记源）'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向'
    def draw(self,context):
        layout=self.layout;opts=_opts(context)
        layout.operator(C94MMDJOB_OT_run.bl_idname,text='读取已登记MMD面板当前选择').operation='USE_SELECTED'
        layout.prop(opts,'source');layout.prop(opts,'target');layout.prop(opts,'profile_text')
        if opts.clip_summary:layout.label(text=opts.clip_summary)
        layout.prop(opts,'items_json');layout.prop(opts,'motion');layout.prop(opts,'jobs_root')
        row=layout.row(align=True);row.prop(opts,'max_seconds');row.prop(opts,'threads')
        session=shared.session_for(context.scene)
        if not session:
            layout.operator(C94MMDJOB_OT_run.bl_idname,text='后台生成独立MMD候选').operation='ENQUEUE'
        elif session.get('entry')=='MMD':
            cache=session['cache'];layout.label(text=str(cache.get('status','读取中')))
            progress=cache.get('progress')
            if progress:layout.label(text=f'{progress.get("phase","")} {progress.get("done",0)}/{progress.get("total",0)}')
            for row in cache.get('items',[]):layout.label(text=f'{row["index"]}: {(row.get("mmd") or {}).get("clip_id","")} {row.get("state","")}')
            layout.prop(opts,'selected')
            for op,label in [('REFRESH','刷新进度'),('CANCEL','取消并保留成功'),('COLLECT','收集成功'),('PREVIEW','预览所选'),('STOP','停止并恢复'),('RELEASE','释放界面任务')]:
                layout.operator(C94MMDJOB_OT_run.bl_idname,text=label).operation=op
        else:layout.label(text='当前场景已有其他预设后台任务；沿用同一队列，请先完成或释放')
        layout.label(text=opts.message)

def _timer():
    if not _owned:return None
    if not shared._OWNED:shared.poll_runtime_once()
    return .75

@persistent
def _invalidate(*_args):
    for key,value in list(shared._SESSIONS.items()):
        if value.get('entry')=='MMD':shared._SESSIONS.pop(key,None)

def register():
    global _owned,_owns_options
    if _owned:raise RuntimeError('MMD后台界面已注册')
    if hasattr(bpy.types.Scene,OPTIONS):raise RuntimeError('MMD后台Scene属性已被其他注册占用')
    try:
        for cls in (C94MMDJOB_Options,C94MMDJOB_OT_run,C94MMDJOB_PT_panel):
            bpy.utils.register_class(cls);_registered.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94MMDJOB_Options))
        _owns_options=True
        bpy.app.handlers.load_post.append(_invalidate);_owned=True
        bpy.app.timers.register(_timer,first_interval=.75)
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
