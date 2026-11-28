# SPDX-License-Identifier: GPL-3.0-or-later
"""A nonblocking queue UI over retarget_job; the scene is never evaluated here."""
import json
from pathlib import Path
import bpy
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty,CollectionProperty,EnumProperty,FloatProperty,
                       IntProperty,PointerProperty,StringProperty)
from . import retarget_action as actions,retarget_job as jobs,retarget_ui as body_ui

OPTIONS='c94_retarget_job_options_v1'
_REGISTERED=[]
_OWNED=False
_SESSIONS={}
_SLOT_ITEMS=[]
_STATUS={'not_started':'未开始','running':'处理中','succeeded':'成功','failed':'失败',
         'cancelled':'已取消','cancelling':'取消中','partial':'部分完成','exited':'进程结束'}
_ICONS={'not_started':'TIME','running':'TIME','succeeded':'CHECKMARK','failed':'ERROR',
        'cancelled':'CANCEL','cancelling':'TIME','partial':'INFO'}

def _require(value,message):
    if not value:raise ValueError(message)

def _opts(context):return getattr(context.scene,OPTIONS)
def _body(context):return getattr(context.scene,body_ui.OPTIONS)
def _scene_key(scene):return (int(scene.session_uid),int(scene.as_pointer()))

def session_for(scene):
    """RAM handle only; a saved directory never reconstructs collect authority."""
    session=_SESSIONS.get(_scene_key(scene))
    if session is not None:
        try:
            if session['scene']!=scene:return None
        except ReferenceError:return None
    return session

def register_session(scene,target,source,job):
    """Register one already-owned retarget_job for the existing queue UI."""
    _require(scene is not None and target is not None and source is not None and job is not None,
             '会话参数不完整')
    cache=jobs.peek_status(job)
    _SESSIONS[_scene_key(scene)]={'scene':scene,'target':target,'source':source,
        'job':job,'cache':cache,'selected':0,'collected':{}}
    return cache

def _session(context):
    value=session_for(context.scene)
    _require(value is not None,'当前场景没有本次会话的后台任务；磁盘输出仍保留')
    _require(value.get('entry') is None,'当前任务由专项面板管理；请在对应专项入口审阅、停止或撤回')
    _require(_body(context).target==value['target'],'当前目标与此后台任务不同，请选回原目标')
    return value

def _set_message(context,message):
    # User actions may update authoring state. Progress itself is RAM-only.
    _opts(context).message=str(message)

def _redraw():
    for manager in bpy.data.window_managers:
        for window in manager.windows:
            for area in window.screen.areas:
                if area.type=='VIEW_3D':area.tag_redraw()

def poll_runtime_once():
    """Timer-facing status read. No bpy pose/frame/Action or graph writes."""
    for key,session in list(_SESSIONS.items()):
        try:
            scene=session['scene']
            if _scene_key(scene)!=key or scene.name not in bpy.data.scenes:
                _SESSIONS.pop(key,None);continue
            if not session['cache'].get('terminal'):
                session['cache']=jobs.peek_status(session['job'])
        except ReferenceError:
            # Deleting a Scene invalidates its RNA handle. Keep the owned job
            # and disk outputs independent; discard this unreachable UI cache.
            _SESSIONS.pop(key,None)
        except (RuntimeError,ValueError,OSError) as exc:
            session['cache']={'status':'failed','terminal':True,'items':[],
                              'error':{'message':str(exc)}}
    _redraw()

def _timer():
    if not _OWNED:return None
    poll_runtime_once()
    return .75

@persistent
def _invalidate(*_args):
    # Jobs own their Popen/watchdog. Invalidating UI handles never kills an
    # unrelated process and does not discard already written outputs.
    _SESSIONS.clear();_SLOT_ITEMS.clear()

def guard_candidate(target,action):
    """Use the same persistent graph/transform guard as Agent preview."""
    return jobs.validate_candidate_target(target,action)


def _queue_owner(opts,body):
    if opts.items:
        _require(opts.source==body.source and opts.preset==body.source_kind,
                 '队列来自另一源骨架或预设，请先清空队列')
    else:opts.source=body.source;opts.preset=body.source_kind

def _selected_item(context):
    opts=_opts(context)
    _require(opts.items and 0<=opts.index<len(opts.items),'请先选择队列条目')
    return opts.items[opts.index]

def _slot_items(_operator,context):
    global _SLOT_ITEMS
    try:action=_selected_item(context).action
    except ValueError:action=None
    _SLOT_ITEMS=[(slot.identifier,slot.identifier,'') for slot in action.slots
                 if slot.target_id_type=='OBJECT'] if action else []
    return _SLOT_ITEMS

def _read_item_clip(item,slot_identifier=None):
    action=item.action
    _require(action is not None,'该条目未选择源Action')
    identifier=slot_identifier or item.slot_identifier
    candidates=[slot for slot in action.slots if slot.target_id_type=='OBJECT']
    slot=next((slot for slot in candidates if slot.identifier==identifier),None)
    if slot is None:
        _require(len(candidates)==1,'此Action有多个OBJECT Slot，请明确选择')
        slot=candidates[0]
    clip=jobs._clip_record(action,slot,item.fps if item.fps_mode=='EXPLICIT' else None)
    item.slot_identifier=slot.identifier;item.fps=clip['fps']
    item.start,item.end=clip['range']

def _item_request(item):
    value={'action':item.action.name if item.action else '',
           'slot':item.slot_identifier,'start':item.start,'end':item.end,
           'output_start':item.output_start,'label':item.label}
    if item.fps_mode=='EXPLICIT':value['fps']=item.fps
    if item.output_name.strip():value['action_name']=item.output_name.strip()
    return value

class C94RETARGETJOB_Item(bpy.types.PropertyGroup):
    action:PointerProperty(name='源动作',type=bpy.types.Action)
    slot_identifier:StringProperty(name='来源动作槽标识')
    fps_mode:EnumProperty(name='来源时基',items=(('RECORDED','使用该动作槽已登记时基','无记录的条目会单独失败'),
        ('EXPLICIT','明确指定源帧率','仅用于已核实的来源帧率')),default='RECORDED')
    fps:FloatProperty(name='源帧率',default=30,min=.01,max=1000)
    start:FloatProperty(name='源起帧',default=1)
    end:FloatProperty(name='源止帧',default=2)
    output_start:IntProperty(name='目标起帧',default=1)
    output_name:StringProperty(name='新动作名称',default='')
    label:StringProperty(name='备注',default='')

class C94RETARGETJOB_Options(bpy.types.PropertyGroup):
    items:CollectionProperty(type=C94RETARGETJOB_Item)
    index:IntProperty(default=0,min=0)
    source:PointerProperty(type=bpy.types.Object)
    preset:StringProperty(default='MIXAMO')
    jobs_root:StringProperty(name='后台工作目录',subtype='DIR_PATH',default='')
    max_seconds:FloatProperty(name='最长处理时间（秒）',default=1800,min=1,max=86400)
    threads:IntProperty(name='后台CPU线程',default=2,min=1,max=8)
    last_directory:StringProperty(name='上次输出目录',default='')
    message:StringProperty(name='状态',default='队列只记录源动作；后台结果单独收集')

class C94RETARGETJOB_UL_queue(bpy.types.UIList):
    def draw_item(self,context,layout,data,item,icon,active_data,active_propname,index):
        row=layout.row(align=True)
        row.label(text=item.action.name if item.action else '未选择动作',icon='ACTION')
        row.label(text=f'{item.start:g} → {item.end:g}')

class C94RETARGETJOB_OT_queue(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_queue'
    bl_label='编辑重定向队列'
    operation:EnumProperty(items=[(value,value,'') for value in ('ADD','REMOVE','CLEAR','READ_CLIP')])
    def execute(self,context):
        opts=_opts(context);body=_body(context)
        try:
            if self.operation=='ADD':
                _require(body.source_kind in ('MIXAMO','CMU'),'此后台队列尚未接入所选来源预设')
                _require(len(opts.items)<64,'队列最多64条')
                _queue_owner(opts,body)
                clip=body_ui.sources.active_clip(body.source,body.source_fps if body.override_fps else None)
                item=opts.items.add();item.action=clip['action'];item.slot_identifier=clip['slot'].identifier
                item.fps=clip['fps'];item.fps_mode='EXPLICIT' if body.override_fps else 'RECORDED'
                item.start,item.end=clip['range'];opts.index=len(opts.items)-1
            elif self.operation=='REMOVE':
                _selected_item(context);opts.items.remove(opts.index)
                opts.index=min(opts.index,max(0,len(opts.items)-1))
            elif self.operation=='CLEAR':
                opts.items.clear();opts.index=0;opts.source=None;opts.preset='MIXAMO'
            else:_read_item_clip(_selected_item(context))
            _set_message(context,'队列已更新，源Action与当前姿态保持')
            return {'FINISHED'}
        except Exception as exc:
            _set_message(context,str(exc));self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94RETARGETJOB_OT_slot(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_queue_slot'
    bl_label='选择来源动作的动作槽'
    slot:EnumProperty(name='来源动作槽',items=_slot_items)
    def invoke(self,context,_event):return context.window_manager.invoke_props_dialog(self)
    def execute(self,context):
        try:
            _read_item_clip(_selected_item(context),self.slot)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94RETARGETJOB_OT_run(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_background'
    bl_label='后台重定向'
    operation:EnumProperty(items=[(value,value,'') for value in
        ('ENQUEUE_CURRENT','ENQUEUE_QUEUE','REFRESH','CANCEL','COLLECT_ALL','COLLECT_SELECTED','SELECT','PREVIEW','RELEASE')])
    item_index:IntProperty(default=0,min=0)
    def execute(self,context):
        opts=_opts(context);body=_body(context)
        try:
            if self.operation.startswith('ENQUEUE_'):
                _require(session_for(context.scene) is None,'请先收集结果并释放当前任务；磁盘输出始终保留')
                root=Path(bpy.path.abspath(opts.jobs_root)) if opts.jobs_root.strip() else None
                _require(root is not None and root.is_absolute(),'请明确选择绝对后台工作目录')
                _require(body.source_kind in ('MIXAMO','CMU'),'当前来源预设尚未接后台队列')
                if self.operation=='ENQUEUE_QUEUE':
                    _require(opts.items,'队列为空')
                    _queue_owner(opts,body)
                    items=[_item_request(item) for item in opts.items]
                else:
                    items=[{'fps':body.source_fps}] if body.override_fps else None
                settings={'motion':body.motion,'max_seconds':opts.max_seconds,'threads':opts.threads}
                if body.source_kind=='MIXAMO':settings.update(scale_motion=body.scale_motion,include_fingers=body.include_fingers)
                from . import retarget_contact_detection_ui as detection_ui
                settings['contact_detection_calibration']=detection_ui.calibration_from_context(context)
                mapping=json.loads(body.mapping_json)
                job=jobs.enqueue(body.source,body.target,items=items,preset=body.source_kind,
                    mapping=mapping or None,settings=settings,jobs_root=str(root))
                cache=jobs.peek_status(job)
                _SESSIONS[_scene_key(context.scene)]={'scene':context.scene,'target':body.target,'source':body.source,
                    'job':job,'cache':cache,'selected':0,'collected':{}}
                opts.last_directory=str(job.directory)
                _set_message(context,'已提交后台处理；当前动作与预览保持')
            else:
                session=_session(context);cache=session['cache'];job=session['job']
                if self.operation=='REFRESH':session['cache']=jobs.peek_status(job)
                elif self.operation=='CANCEL':
                    response=jobs.cancel(job)
                    if response.get('status')=='cancelling':session['cache']={**cache,'status':'cancelling'}
                    _set_message(context,'已请求取消；已完成动作保留')
                elif self.operation=='SELECT':
                    _require(self.item_index<len(cache.get('items',[])),'结果条目不存在')
                    session['selected']=self.item_index
                elif self.operation in ('COLLECT_ALL','COLLECT_SELECTED'):
                    selected=None
                    if self.operation=='COLLECT_SELECTED':
                        row=cache['items'][session['selected']];selected=[row['id']]
                    receipt=jobs.collect_successes(job,items=selected)
                    for row in receipt['collected']:
                        action=jobs._existing_marker(job.id,row['item_id'])
                        _require(action is not None,'已收集动作身份未找到，请保留结果检查')
                        session['collected'][row['item_id']]=action
                    _set_message(context,'已追加成功动作；当前角色动作保持。选择结果后可预览')
                elif self.operation=='PREVIEW':
                    row=cache['items'][session['selected']]
                    action=session['collected'].get(row['id'])
                    _require(action is not None,'请先收集该成功动作')
                    guard_candidate(body.target,action)
                    if actions.has_preview(body.target):actions.stop_preview(body.target)
                    actions.begin_preview(body.target,action)
                    body.candidate=action
                    _set_message(context,'正在预览已收集候选；停止并恢复可返回原动作')
                else:
                    _require(cache.get('terminal'),'任务仍在处理，请先取消或等待结束')
                    _SESSIONS.pop(_scene_key(context.scene),None)
                    _set_message(context,'已释放界面任务；已保存输出与收集动作保留')
            _redraw();return {'FINISHED'}
        except Exception as exc:
            if isinstance(exc,jobs.JobError) and exc.code=='ENQUEUE_FAILED' and isinstance(exc.details,dict):
                opts.last_directory=exc.details.get('job_directory','')
            _set_message(context,str(exc));self.report({'ERROR'},str(exc));return {'CANCELLED'}

def draw_queue_editor(layout,context):
    """Shared queue form; all source-specific panels keep the same inputs."""
    opts=_opts(context)
    layout.template_list('C94RETARGETJOB_UL_queue','',opts,'items',opts,'index',rows=3,maxrows=6)
    layout.operator('c94_workflow.retarget_queue',text='添加当前动作').operation='ADD'
    row=layout.row(align=True)
    for value,label in (('REMOVE','移除条目'),('CLEAR','清空队列')):
        row.operator('c94_workflow.retarget_queue',text=label).operation=value
    if opts.items and opts.index<len(opts.items):
        item=opts.items[opts.index];box=layout.box();box.prop(item,'action');box.prop(item,'slot_identifier')
        row=box.row(align=True)
        row.operator('c94_workflow.retarget_queue_slot',text='选择动作槽并读取范围')
        row.operator('c94_workflow.retarget_queue',text='读取范围').operation='READ_CLIP'
        box.prop(item,'fps_mode')
        if item.fps_mode=='EXPLICIT':box.prop(item,'fps')
        row=box.row(align=True);row.prop(item,'start');row.prop(item,'end')
        box.prop(item,'output_start');box.prop(item,'output_name');box.prop(item,'label')


def draw_job_session(layout,context,session):
    """One result selector and lifecycle form for all shared queue consumers."""
    cache=session['cache'];box=layout.box();status=cache.get('status','running')
    box.label(text=_STATUS.get(status,status),icon=_ICONS.get(status,'INFO'))
    counts=cache.get('counts',{})
    box.label(text='成功 %s / 失败 %s / 取消 %s / 未开始 %s'%tuple(counts.get(k,0) for k in ('succeeded','failed','cancelled','not_started')))
    progress=cache.get('progress') or {}
    if progress.get('total'):
        box.progress(factor=max(0,min(1,progress.get('done',0)/progress['total'])),type='BAR',text='当前动作')
    if cache.get('error'):box.label(text=str(cache['error'].get('message',''))[:70],icon='ERROR')
    for index,item in enumerate(cache.get('items',[])):
        row=box.row(align=True);state=item.get('state','not_started')
        select=row.operator('c94_workflow.retarget_background',text=str(index+1),depress=index==session['selected'])
        select.operation='SELECT';select.item_index=index
        row.label(text=item.get('action_name') or '无效条目',icon=_ICONS.get(state,'INFO'))
        row.label(text=_STATUS.get(state,state))
    selected=cache.get('items',[])
    if selected and session['selected']<len(selected):
        detail=selected[session['selected']]
        if detail.get('error'):box.label(text=str(detail['error'].get('message',''))[:80],icon='INFO')
    row=box.row(align=True)
    row.operator('c94_workflow.retarget_background',text='刷新').operation='REFRESH'
    cancel=row.row();cancel.enabled=not cache.get('terminal',False)
    cancel.operator('c94_workflow.retarget_background',text='取消处理').operation='CANCEL'
    row=box.row(align=True)
    row.operator('c94_workflow.retarget_background',text='收集全部成功').operation='COLLECT_ALL'
    row.operator('c94_workflow.retarget_background',text='收集所选').operation='COLLECT_SELECTED'
    row=box.row(align=True)
    row.operator('c94_workflow.retarget_background',text='预览已收集所选').operation='PREVIEW'
    row.operator('c94_workflow.retarget_action',text='停止并恢复').operation='STOP'
    release=box.row();release.enabled=cache.get('terminal',False)
    release.operator('c94_workflow.retarget_background',text='释放当前任务').operation='RELEASE'


class C94RETARGETJOB_PT_queue(bpy.types.Panel):
    bl_idname='C94RETARGETJOB_PT_queue'
    bl_label='后台动作队列'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向'
    bl_parent_id='C94RETARGET_PT_body'
    def draw(self,context):
        opts=_opts(context);layout=self.layout;session=session_for(context.scene)
        if session is not None and session.get('entry') is not None:
            layout.label(text='当前任务请在对应专项面板管理',icon='INFO')
            return
        layout.prop(opts,'jobs_root')
        row=layout.row(align=True);row.prop(opts,'max_seconds');row.prop(opts,'threads')
        draw_queue_editor(layout,context)
        row=layout.row(align=True);row.enabled=session is None
        row.operator('c94_workflow.retarget_background',text='后台处理当前动作').operation='ENQUEUE_CURRENT'
        row.operator('c94_workflow.retarget_background',text='后台处理队列').operation='ENQUEUE_QUEUE'
        if session is not None:
            draw_job_session(layout,context,session)
        layout.label(text=opts.message[:70],icon='INFO')
        if opts.last_directory:layout.prop(opts,'last_directory')
        layout.label(text='成功结果只追加独立动作；取消不会删除成功项')
        layout.label(text='换文件或撤销后界面任务失效，磁盘输出保留')

_CLASSES=(C94RETARGETJOB_Item,C94RETARGETJOB_Options,C94RETARGETJOB_UL_queue,
          C94RETARGETJOB_OT_queue,C94RETARGETJOB_OT_slot,C94RETARGETJOB_OT_run,C94RETARGETJOB_PT_queue)

def register():
    global _OWNED
    _require(hasattr(bpy.types.Scene,body_ui.OPTIONS),'请先注册重定向主界面')
    _require(not hasattr(bpy.types.Scene,OPTIONS),'后台队列界面已由其他实例注册，原实例保持')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94RETARGETJOB_Options));_OWNED=True
        for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):handlers.append(_invalidate)
        bpy.app.timers.register(_timer,first_interval=.75,persistent=True)
    except Exception:unregister();raise

def unregister():
    global _OWNED
    if bpy.app.timers.is_registered(_timer):bpy.app.timers.unregister(_timer)
    for handlers in (bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        if _invalidate in handlers:handlers.remove(_invalidate)
    _invalidate()
    if _OWNED and hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _OWNED=False
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
