"""Both new-character entrances share one live, transactional entry session."""
import json
import traceback
from pathlib import Path
import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

OPTIONS='c94_entry_options_v1'
TARGETS='c94_entry_targets_ui_v1'
RESULT='c94_entry_result_ui_v1'
CANDIDATE='c94_entry_candidate_ui_v1'
_REGISTERED=[]
_PROPERTY_OWNED=False
_SESSION=None


def _targets(scene):
    raw=scene.get(TARGETS,'[]')
    try:rows=json.loads(raw)
    except (ValueError,TypeError):raise ValueError('角色输入名单损坏')
    if not isinstance(rows,list) or not rows or any(not isinstance(n,str) for n in rows):raise ValueError('先捕获明确角色网格名单')
    objects=[scene.objects.get(n) for n in rows]
    if any(o is None or o.type!='MESH' for o in objects):raise ValueError('角色输入名单中有对象已丢失')
    return objects


def _session(context, *, capture=False):
    if _SESSION is None:raise ValueError('先准备本次新角色')
    if _SESSION['scene']!=context.scene:raise ValueError('切回本次角色所属场景')
    if _SESSION.get('aborting'):raise ValueError('正在撤回本次制作，等待后台释放')
    if capture and _SESSION['plan'] is None:
        from . import workflow_entry
        plan=workflow_entry.capture_reviewed(context.scene,_SESSION['candidate_id'])
        _SESSION.update(plan=plan,source_objects=list(plan._staging),summary=plan.pending)
    return _SESSION


def _forget_candidate(session):
    scene=session['scene']
    if scene==bpy.context.scene and scene.get(CANDIDATE)==session['candidate_id']:
        del scene[CANDIDATE]


def _redraw():
    wm=getattr(bpy.context,'window_manager',None)
    for win in wm.windows if wm else ():
        for area in win.screen.areas if win.screen else ():
            if area.type=='VIEW_3D':area.tag_redraw()


def _monitor():
    global _SESSION
    if _SESSION is None:return None
    try:
        if _SESSION.get('aborting'):
            state=_SESSION['plan'].abort();_SESSION['summary']=state
            if state.get('cleanup_complete'):
                if _SESSION.get('discard'): _forget_candidate(_SESSION)
                from . import workbench_ui
                workbench_ui.VOXEL_PROGRESS.observe(_SESSION['scene'], {'status': 'cancelled'})
                _SESSION=None;_redraw();return None
        else:
            for job in _SESSION['jobs'].values():job.status()
            if _SESSION['plan'] is not None:
                _SESSION['summary']=_SESSION['plan'].pending
                from . import workbench_ui
                progress = workbench_ui.VOXEL_PROGRESS
                rows = _SESSION['summary']
                active = next((row for row in rows if row.get('status') == 'running'), None)
                if active:
                    progress.observe(_SESSION['scene'], {**(active.get('detail') or {}), 'status': 'running'},
                                     cancel_operator='c94_workflow.abort_character_entry')
                elif progress.cancel_operator == 'c94_workflow.abort_character_entry':
                    terminal = 'failed' if any(row.get('status') == 'failed' for row in rows) else 'succeeded'
                    progress.observe(_SESSION['scene'], {'status': terminal})
    except Exception as exc:
        _SESSION['error']=str(exc)
    _redraw();return .25


def _start_monitor():
    if not bpy.app.timers.is_registered(_monitor):bpy.app.timers.register(_monitor,first_interval=.25,persistent=True)


class C94ENTRY_Options(bpy.types.PropertyGroup):
    mode:EnumProperty(name='输入路线',items=[('AUTO','自动识别已有绑定','原资产保留，在副本中新建角色'),('UNBOUND','未绑定','输入尚未绑定的几何'),('EXISTING','已有绑定重建','保留旧绑定；不兼容旧驱动逐项交接重做')])
    index:IntProperty(name='当前部件序号',default=0,min=0)
    proxy:StringProperty(name='体积代理')
    donor:StringProperty(name='已绑定供体')
    rigid_bone:StringProperty(name='刚性目标形变骨')
    cells_longest:IntProperty(name='最长轴格数',default=32,min=4,max=512)
    neighbor_count:EnumProperty(name='邻接',items=[('6','6邻接',''),('26','26邻接','')],default='26')
    max_cells:IntProperty(name='最大格数',default=250000,min=8,max=2000000)
    max_memory_mb:FloatProperty(name='内存预算（MB）',default=512,min=1,max=32768)
    max_seconds:FloatProperty(name='时间预算（秒）',default=60,min=.01,max=3600)
    jobs_directory:StringProperty(name='本次任务目录',subtype='DIR_PATH')
    transfer_distance:FloatProperty(name='供体覆盖距离（米）',default=.02,min=.000001)
    use_current_neutral_world:BoolProperty(name='明确采用当前中立对象框架',default=False,
        description='将当前帧的对象世界矩阵用于新绑定；请先确认角色处于中立状态，旧骨架和动作保留')


class C94ENTRY_OT_Targets(bpy.types.Operator):
    bl_idname='c94_workflow.capture_character_inputs'
    bl_label='捕获选中网格为角色输入'
    def execute(self,context):
        try:
            if _SESSION is not None:raise ValueError('先完成或撤回当前角色，再更换输入')
            names=[o.name for o in context.selected_objects if o.type=='MESH']
            if not names:raise ValueError('明确选择本次身体和部件网格')
            if context.scene.get(CANDIDATE):del context.scene[CANDIDATE]
            context.scene[TARGETS]=json.dumps(names,ensure_ascii=False)
            self.report({'INFO'},'已捕获%d件输入；代理和供体另行选择'%len(names))
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94ENTRY_OT_Prepare(bpy.types.Operator):
    bl_idname='c94_workflow.prepare_character_entry'
    bl_label='准备新角色副本与骨架'
    @classmethod
    def poll(cls,context):return context.mode=='OBJECT'
    def execute(self,context):
        global _SESSION
        try:
            if _SESSION is not None:raise ValueError('本次角色尚未结束，先完成或撤回')
            from . import workflow_entry,generation_ui,accessory_ui,workbench_ui
            _start_monitor()  # Fail before any character IDs are allocated.
            candidate_id=context.scene.get(CANDIDATE)
            if candidate_id:
                result=workflow_entry.inspect_candidate(context.scene,candidate_id)
            else:
                options=getattr(context.scene,OPTIONS);gen=getattr(context.scene,generation_ui.OPTIONS)
                ref=generation_ui.reference(context);base=Path(bpy.path.abspath(gen.template_file))
                config=generation_ui._configuration(context,ref,base)
                if config is None:raise ValueError('先准备明确模块配置模板')
                objects=_targets(context.scene)
                neutral_world={ob.name:[list(row) for row in ob.matrix_world] for ob in objects} if options.use_current_neutral_world else None
                before = set(bpy.data.collections)
                result=workflow_entry.prepare_candidate(context.scene,ref,str(base),config,objects,label=gen.label,
                    mode=options.mode,legacy_policy='SNAPSHOT_FRESH_REBIND',
                    chain_record=accessory_ui.reference_record(context.scene,ref,validate=True),
                    neutral_world=neutral_world)
                workbench_ui.theme_created_collections(before)
                candidate_id=result['candidate']['id']
                context.scene[CANDIDATE]=candidate_id
            _SESSION={'plan':None,'scene':context.scene,'rig':result['rig'],'candidate_id':candidate_id,
                      'jobs':{},'summary':[], 'error':None,
                      'source_counts':[len(o.data.vertices) for o in result['sources']],
                      'source_objects':list(result['sources'])}
        except Exception as exc:self.report({'ERROR'},str(exc));traceback.print_exc();return {'CANCELLED'}
        self.report({'INFO'},'待审骨架已就绪；保存候选并登记同一骨架与精确范围后继续逐件绑定')
        return {'FINISHED'}


class C94ENTRY_OT_Volume(bpy.types.Operator):
    bl_idname='c94_workflow.start_character_volume'
    bl_label='身体：启动体积扩散'
    def execute(self,context):
        try:
            s=_session(context,capture=True);o=getattr(context.scene,OPTIONS)
            proxy=context.scene.objects.get(o.proxy)
            if proxy is None or proxy.type!='MESH':raise ValueError('选择本次明确的闭合体积代理')
            if not o.jobs_directory.strip():raise ValueError('填写明确的本次后台任务目录')
            from .generation import RIG_KEY
            meta=json.loads(s['rig'][RIG_KEY]);bones=meta.get('core_deforms',meta['deforms'])
            settings={k:getattr(o,k) for k in ('cells_longest','max_cells','max_memory_mb','max_seconds')};settings['neighbor_count']=int(o.neighbor_count)
            job=s['plan'].start_volume(o.index,proxy,bones,settings,jobs_directory=bpy.path.abspath(o.jobs_directory))
            s['jobs'][o.index]=job;_start_monitor()
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94ENTRY_OT_Apply(bpy.types.Operator):
    bl_idname='c94_workflow.apply_character_volume'
    bl_label='应用当前部件的体积候选'
    def execute(self,context):
        try:
            s=_session(context);o=getattr(context.scene,OPTIONS);job=s['jobs'].get(o.index)
            if job is None or not job.status().get('can_apply'):raise ValueError('当前部件没有成功且可应用的本会话求解')
            from .generation_ui import OPTIONS as GEN
            job.apply(name=getattr(context.scene,GEN).label+'_部件_%02d'%o.index)
            s['summary']=s['plan'].pending
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94ENTRY_OT_PlanSurface(bpy.types.Operator):
    bl_idname='c94_workflow.prepare_character_plan_surface'
    bl_label='为本部件准备多域表面'
    def execute(self,context):
        try:
            from . import skinning_plan_ui
            s=_session(context,capture=True);o=getattr(context.scene,OPTIONS);plan_options=skinning_plan_ui._setup(context)
            source=s['source_objects'][o.index]
            from .generation import RIG_KEY
            label=json.loads(s['rig'][RIG_KEY])['label']
            surface=s['plan'].prepare_surface(o.index,name=label+'_域面_%02d'%o.index)
            plan_options.source=source;plan_options.rig=s['rig'];plan_options.surface=surface
            self.report({'INFO'},'已接入多域蒙皮计划面板；在那里配置代理、骨组与明确点域，再从本入口启动')
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94ENTRY_OT_MultiVolume(bpy.types.Operator):
    bl_idname='c94_workflow.start_character_multidomain'
    bl_label='从当前多域方案启动本部件'
    def execute(self,context):
        try:
            from . import skinning_plan_ui
            s=_session(context,capture=True);o=getattr(context.scene,OPTIONS);plan_options=skinning_plan_ui._setup(context)
            if plan_options.source!=s['source_objects'][o.index] or plan_options.rig!=s['rig']:
                raise ValueError('多域方案不属于本次当前部件，请先准备该部件的多域表面')
            if not o.jobs_directory.strip():raise ValueError('填写明确的本次后台任务目录')
            domains,rigid=skinning_plan_ui._payload(plan_options)
            job=s['plan'].start_plan(o.index,plan_options.surface,domains,rigid,jobs_directory=bpy.path.abspath(o.jobs_directory),
                transfer_distance_meters=plan_options.transfer_distance_meters)
            s['jobs'][o.index]=job;_start_monitor()
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));traceback.print_exc();return {'CANCELLED'}


class C94ENTRY_OT_Finish(bpy.types.Operator):
    bl_idname='c94_workflow.finish_character_entry'
    bl_label='完成本次角色'
    def execute(self,context):
        global _SESSION
        try:
            s=_session(context)
            if s['plan'] is None:raise ValueError('先验收骨架并完成本次明确部件绑定')
            result=s['plan'].finish()
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
        warnings=[]
        try:
            context.scene[RESULT]=json.dumps({'schema':'c94.character-entry-ui-result.v1','rig':result['rig'].name,'objects':[o.name for o in result['objects']],'report':result['report']},ensure_ascii=False,allow_nan=False)
            _forget_candidate(s)
        except Exception as exc:warnings.append(str(exc))
        _SESSION=None
        self.report({'WARNING'} if warnings else {'INFO'},'新角色已完成，原资产保留'+('；结果记录提示：'+'；'.join(warnings) if warnings else ''))
        return {'FINISHED'}


class C94ENTRY_OT_Part(bpy.types.Operator):
    bl_idname='c94_workflow.bind_character_part'
    bl_label='绑定当前部件'
    method:EnumProperty(items=[('TRANSFER','读取身体现用权重',''),('RIGID','刚性绑定','')])
    def execute(self,context):
        try:
            s=_session(context,capture=True);o=getattr(context.scene,OPTIONS)
            from .generation_ui import OPTIONS as GEN
            name=getattr(context.scene,GEN).label+'_部件_%02d'%o.index
            if self.method=='RIGID':
                if not 0<=o.index<len(s['source_counts']):raise ValueError('当前部件序号越界')
                assignments=[{'bone_name':o.rigid_bone,'vertex_indices':list(range(s['source_counts'][o.index]))}]
                s['plan'].bind_rigid(o.index,assignments,name=name)
            else:
                donor=context.scene.objects.get(o.donor)
                if donor is None:raise ValueError('选择本会话已实际绑定的身体候选')
                from .generation import RIG_KEY
                meta=json.loads(s['rig'][RIG_KEY]);bones=[n for n in meta['deforms'] if n in donor.vertex_groups]
                s['plan'].bind_transfer(o.index,donor,max_distance_meters=o.transfer_distance,name=name,bone_names=bones)
            s['summary']=s['plan'].pending
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94ENTRY_OT_Abort(bpy.types.Operator):
    bl_idname='c94_workflow.abort_character_entry'
    bl_label='撤回本次新建'
    def execute(self,context):
        global _SESSION
        try:
            if _SESSION is None:raise ValueError('没有本次新建会话')
            if _SESSION['scene']!=context.scene:raise ValueError('切回会话所属场景')
            _SESSION.update(aborting=True,discard=True)
            plan=_SESSION['plan']
            result=plan.abort() if plan is not None else {'cleanup_complete':True}
            if result.get('cleanup_complete'):
                _forget_candidate(_SESSION)
                _SESSION=None
            else:_start_monitor()
            self.report({'INFO'},'已撤回绑定会话，保留待审骨架' if _SESSION is None else '已取消求解；等待后台释放后清理本次副本')
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94ENTRY_PT_Entry(bpy.types.Panel):
    bl_label='角色新建与已有绑定重建'
    bl_idname='C94ENTRY_PT_entry';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='角色绑定';bl_order=2
    def draw(self,context):
        layout=self.layout;o=getattr(context.scene,OPTIONS)
        workspace=getattr(context.scene,'c94_workspace_options_v1',None)
        advanced=workspace is None or workspace.level!='DAILY'
        if _SESSION is None:
            layout.prop(o,'mode');layout.prop(o,'use_current_neutral_world');layout.operator('c94_workflow.capture_character_inputs')
            try:
                for obj in _targets(context.scene):layout.label(text=obj.name,icon='MESH_DATA')
            except ValueError as exc:layout.label(text=str(exc),icon='INFO')
            layout.operator('c94_workflow.prepare_character_entry')
            layout.label(text='先完成上方模板与模块配置，再准备本次角色')
            return
        if _SESSION['scene']!=context.scene:layout.label(text='另一个场景有新建会话，先完成或撤回',icon='INFO');return
        layout.label(text='当前骨架：'+_SESSION['rig'].name)
        labels={'running':'处理中','succeeded':'求解完成，可应用','failed':'失败',
                'cancelled':'已取消','applied':'已应用','unstarted':'未开始'}
        for row in _SESSION.get('summary',[]) if isinstance(_SESSION.get('summary'),list) else []:
            box=layout.box()
            box.label(text='%d · %s · %s'%(row['index'],row['source'],labels.get(row['status'],'等待处理')))
            detail=row.get('detail') or {}
            if detail.get('total') and isinstance(detail.get('done'),(int,float)):
                box.progress(factor=max(0.,min(1.,detail['done']/detail['total'])),type='BAR',text='当前任务')
            error=detail.get('error')
            if isinstance(error,dict) and error.get('message'):
                box.label(text=str(error['message'])[:65],icon='ERROR')
        if _SESSION.get('error'):layout.label(text=_SESSION['error'],icon='ERROR')
        if _SESSION.get('aborting'):layout.label(text='正在取消并释放后台任务');return
        layout.prop(o,'index');layout.prop_search(o,'proxy',context.scene,'objects');layout.prop(o,'jobs_directory')
        if advanced:
            for key in ('cells_longest','neighbor_count','max_cells','max_memory_mb','max_seconds'):layout.prop(o,key)
        layout.operator('c94_workflow.start_character_volume');layout.operator('c94_workflow.apply_character_volume')
        if advanced:
            layout.operator('c94_workflow.prepare_character_plan_surface')
            layout.label(text='多域蒙皮计划面板配置后，从这里统一启动和提交')
            layout.operator('c94_workflow.start_character_multidomain')
        layout.label(text='服装与刚性/附属部件按明确策略逐件绑定')
        layout.prop_search(o,'donor',context.scene,'objects');layout.prop(o,'transfer_distance')
        layout.operator('c94_workflow.bind_character_part',text='服装：读取本角色身体现用权重').method='TRANSFER'
        layout.prop_search(o,'rigid_bone',_SESSION['rig'].data,'bones')
        layout.operator('c94_workflow.bind_character_part',text='附件：明确主骨刚性绑定').method='RIGID'
        layout.operator('c94_workflow.finish_character_entry');layout.operator('c94_workflow.abort_character_entry')


@persistent
def _load_pre(*args):
    global _SESSION
    if _SESSION is not None:
        if _SESSION['plan'] is None:
            _SESSION=None
            return
        _SESSION['aborting']=True
        try:
            result=_SESSION['plan'].abort()
            if result.get('cleanup_complete'):_SESSION=None
        except Exception as exc:_SESSION['error']=str(exc)
        if _SESSION is not None:_start_monitor()


_CLASSES=(C94ENTRY_Options,C94ENTRY_OT_Targets,C94ENTRY_OT_Prepare,C94ENTRY_OT_Volume,C94ENTRY_OT_Apply,
          C94ENTRY_OT_PlanSurface,C94ENTRY_OT_MultiVolume,
          C94ENTRY_OT_Part,C94ENTRY_OT_Finish,C94ENTRY_OT_Abort,C94ENTRY_PT_Entry)
def register():
    global _PROPERTY_OWNED
    if _REGISTERED:return
    if hasattr(bpy.types.Scene,OPTIONS):raise RuntimeError('角色入口已由其它实例注册')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94ENTRY_Options))
        _PROPERTY_OWNED=True
        bpy.app.handlers.load_pre.append(_load_pre)
    except Exception:unregister();raise
def unregister():
    global _PROPERTY_OWNED
    if _SESSION is not None:_load_pre()
    if _SESSION is None and bpy.app.timers.is_registered(_monitor):bpy.app.timers.unregister(_monitor)
    if _load_pre in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(_load_pre)
    if _PROPERTY_OWNED and hasattr(bpy.types.Scene,OPTIONS):
        prop=bpy.types.Scene.bl_rna.properties.get(OPTIONS)
        if prop and prop.fixed_type.identifier==C94ENTRY_Options.bl_rna.identifier:delattr(bpy.types.Scene,OPTIONS)
    _PROPERTY_OWNED=False
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
