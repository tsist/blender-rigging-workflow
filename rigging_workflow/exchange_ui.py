# SPDX-License-Identifier: GPL-3.0-or-later
"""Hand-operated exchange forms; GUI/AI share snapshot inspection and worker jobs."""
from pathlib import Path
import bpy
from bpy.props import StringProperty,EnumProperty,IntProperty,FloatProperty,BoolProperty
from . import exchange_api as api
from .exchange_runtime.io_support import Failure,read_json,digest
_OWNED=[]
_LAST={}
def _directory(value,context):
    raw=value or context.window_manager.c94_exchange_jobs
    if not raw:raw=str(Path(bpy.app.tempdir)/'C94Exchange')
    path=Path(bpy.path.abspath(raw))
    if not path.is_absolute():raise Failure('INVALID_REQUEST','请选择绝对输出目录')
    return str(path)
def _selected(context):
    if context.mode not in {'OBJECT','POSE'}:raise Failure('UNSUPPORTED','请先结束编辑模式，再选择骨架和网格')
    objects=tuple(context.selected_objects);rigs=[o for o in objects if o.type=='ARMATURE'];meshes=[o for o in objects if o.type=='MESH']
    if len(rigs)!=1 or not 1<=len(meshes)<=16 or len(objects)!=len(meshes)+1:raise Failure('INVALID_REQUEST','请明确选择一副骨架和1至16个目标网格')
    if any(o.library or o.data.library for o in objects):raise Failure('UNSUPPORTED','交换仅支持本地骨架与网格')
    return (rigs[0],*meshes)
def _session_uid(value,label):
    uid=getattr(value,'session_uid',None)
    if uid is None or not str(uid):raise Failure('UNSUPPORTED','当前'+label+'缺少原生session_uid；不使用名称或地址回退')
    return str(uid)
def _selection_identity(objects,scene):
    return {'scene':_session_uid(scene,'Scene'),'objects':sorted([
        {'type':o.type,'object':_session_uid(o,'Object'),'data':_session_uid(o.data,'Data')}
        for o in objects],key=lambda row:(row['type'],row['object'],row['data']))}
def _confirmed_selection(self,context):
    objects=_selected(context)
    identity=_selection_identity(objects,context.scene)
    if hasattr(self,'_selection_identity') and self._selection_identity!=identity:
        raise Failure('CONFLICT','选择、数据或Scene已改变，请重新打开导出对话框')
    return objects
def _store(job):
    _LAST.clear();_LAST.update(job)
class C94EX_OT_export(bpy.types.Operator):
    bl_idname='c94_workflow.exchange_export';bl_label='导出选中角色片段';bl_description='保存当前明确选择的受保护快照，检查驱动与资源后在后台导出'
    output_directory:StringProperty(name='输出目录',subtype='DIR_PATH')
    format:EnumProperty(name='格式',items=[('GLB','glTF（二进制 .glb）',''),('FBX','FBX','')],default='GLB')
    frame_start:IntProperty(name='开始帧',default=1,min=1,max=100000)
    frame_end:IntProperty(name='结束帧',default=4,min=1,max=100000)
    witness_substeps:EnumProperty(name='验证采样/帧',items=[('4','4',''),('8','8',''),('32','32','')],default='32')
    format_substeps:EnumProperty(name='文件采样/帧',items=[('4','4',''),('8','8',''),('32','32','')],default='32')
    max_position_error:FloatProperty(name='位置误差预算（米）',default=1e-4,min=1e-7,max=.001,precision=6)
    dispatch=staticmethod(api.start_export_prepared)
    def invoke(self,context,event):
        try:
            objects=_selected(context);scene=context.scene
            self._selection_identity=_selection_identity(objects,scene)
            if scene.render.fps_base!=1:raise Failure('UNSUPPORTED','当前仅支持fps_base=1的整数帧率')
            self.frame_start=max(1,scene.frame_start);self.frame_end=max(1,scene.frame_end);self.output_directory=context.window_manager.c94_exchange_jobs
            return context.window_manager.invoke_props_dialog(self,width=480)
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    def draw(self,context):
        layout=self.layout;layout.prop(self,'output_directory');layout.prop(self,'format');row=layout.row();row.prop(self,'frame_start');row.prop(self,'frame_end');layout.label(text='源帧率：%d fps'%context.scene.render.fps);layout.prop(self,'witness_substeps');layout.prop(self,'format_substeps');layout.prop(self,'max_position_error');layout.label(text='Key值与固定映射门保持1e-5；提交后使用独立快照')
    def execute(self,context):
        try:
            objects=_confirmed_selection(self,context)
            scene=context.scene
            if scene.render.fps_base!=1:raise Failure('UNSUPPORTED','当前仅支持fps_base=1的整数帧率')
            directory=_directory(self.output_directory,context)
            spec={'format':self.format,'scene':scene.name,'view_layer':context.view_layer.name,'objects':[o.name for o in objects],'mode':'ANIMATION','frame_start':self.frame_start,'frame_end':self.frame_end,'meters_per_unit':1,'materials':'NONE','rig_adapter':'BBONE_SHARED_CONTINUOUS_V1','rig_sampling':{'seed_substeps':8,'max_depth':8,'max_evaluations':4096,'max_vertex_samples':256000000,'max_affine_joints':2048,'max_output_bytes':268435456,'max_position_error':self.max_position_error,'witness_substeps':int(self.witness_substeps),'format_substeps':int(self.format_substeps)}}
            api.contract.normalize_export(spec)
            snapshot=api.capture_snapshot(directory);job=self.dispatch(snapshot,spec,jobs_root=directory);_store(job);self.report({'INFO'},'当前选择已保存为快照；检查通过后自动后台导出');return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
class C94EX_OT_import(bpy.types.Operator):
    bl_idname='c94_workflow.exchange_import';bl_label='导入原时间片段';bl_description='从实际GLB/FBX读取原时钟，保存新候选，不替换当前场景'
    filepath:StringProperty(name='文件',subtype='FILE_PATH')
    filter_glob:StringProperty(default='*.glb;*.fbx',options={'HIDDEN'})
    output_directory:StringProperty(name='输出目录',subtype='DIR_PATH')
    format:EnumProperty(name='格式',items=[('AUTO','按扩展名',''),('GLB','glTF（二进制 .glb）',''),('FBX','FBX','')],default='AUTO')
    fps:IntProperty(name='帧率',default=24,min=1,max=240)
    frame:IntProperty(name='打开时帧',default=1,min=1,max=100000)
    dispatch=staticmethod(api.start_import)
    def invoke(self,context,event):
        self.fps=context.scene.render.fps;self.frame=max(1,context.scene.frame_current);self.output_directory=context.window_manager.c94_exchange_jobs;context.window_manager.fileselect_add(self);return {'RUNNING_MODAL'}
    def draw(self,context):
        for name in ('output_directory','format','fps','frame'):self.layout.prop(self,name)
    def execute(self,context):
        try:
            path=Path(bpy.path.abspath(self.filepath));kind=self.format
            if kind=='AUTO':kind={'.glb':'GLB','.fbx':'FBX'}.get(path.suffix.lower())
            if kind not in {'GLB','FBX'}:raise Failure('UNSUPPORTED','请选择.glb或.fbx文件')
            spec={'format':kind,'scene':context.scene.name,'frame':self.frame,'fps':self.fps,'scale_to_meters':1,'mode':'ANIMATION','rig_adapter':'SHARED_RIG_NATIVE_TIME_V1'}
            job=self.dispatch({'file':str(path),'expected_sha256':digest(path)},spec,resources=[],jobs_root=_directory(self.output_directory,context));_store(job);self.report({'INFO'},'原时间导入已在后台启动；结果会保存为新文件');return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
class C94EX_OT_advanced(bpy.types.Operator):
    bl_idname='c94_workflow.exchange_advanced_request';bl_label='提交高级请求'
    operation:EnumProperty(items=[('inspect','检查',''),('export','导出',''),('import','导入','')],default='inspect')
    def execute(self,context):
        try:
            r=read_json(context.window_manager.c94_exchange_request)
            if self.operation=='export':job=api.start_export(r['input'],r['spec'],resources=r.get('resources',[]),profile=r['profile'],jobs_root=r['jobs_root'])
            elif self.operation=='import':job=api.start_import(r['input'],r['spec'],resources=r.get('resources',[]),jobs_root=r['jobs_root'])
            else:job=api.inspect_snapshot(r['input'],resources=r.get('resources',[]),jobs_root=r['jobs_root'],auto_resources=True)
            _store(job);self.report({'INFO'},'高级请求已提交');return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
class C94EX_OT_status(bpy.types.Operator):
    bl_idname='c94_workflow.exchange_status';bl_label='查看交换结果'
    def execute(self,context):
        try:
            row=api.status(_LAST['job_id']);_LAST['last_status']=row
            if row['state']=='succeeded':
                value=api.result(_LAST['job_id']);_LAST['candidate']=value['report'].get('candidate');self.report({'INFO'},'任务通过，结果目录：'+row['directory'])
            elif row['state']=='failed':self.report({'ERROR'},str(row.get('error','任务失败，请查看结果目录')))
            else:self.report({'INFO'},'任务状态：'+row['state']+' / '+row.get('phase',''))
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
class C94EX_OT_cancel(bpy.types.Operator):
    bl_idname='c94_workflow.exchange_cancel';bl_label='取消本次交换'
    def execute(self,context):
        try:api.cancel(_LAST['job_id']);self.report({'INFO'},'已处理本次任务取消');return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
class C94EX_PT_exchange(bpy.types.Panel):
    bl_label='角色交换';bl_idname='C94EX_PT_exchange';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='角色绑定'
    def draw(self,context):
        layout=self.layout;layout.prop(context.window_manager,'c94_exchange_jobs',text='输出目录');layout.operator(C94EX_OT_export.bl_idname);layout.operator(C94EX_OT_import.bl_idname);row=layout.row();row.operator(C94EX_OT_status.bl_idname);row.operator(C94EX_OT_cancel.bl_idname)
        if _LAST.get('job_id'):
            state=api.peek_status(_LAST['job_id']);layout.label(text='状态：'+state['state']+(' / '+state['phase'] if state.get('phase') else ''))
            if state.get('error'):layout.label(text=str(state['error'].get('message','任务失败')),icon='ERROR')
            if _LAST.get('candidate'):layout.label(text='候选：'+Path(_LAST['candidate']).name)
        layout.prop(context.window_manager,'c94_exchange_advanced',text='高级请求')
        if context.window_manager.c94_exchange_advanced:
            layout.prop(context.window_manager,'c94_exchange_request',text='JSON请求文件')
            for operation,label in (('inspect','检查'),('export','导出'),('import','导入')):op=layout.operator(C94EX_OT_advanced.bl_idname,text=label);op.operation=operation
_CLASSES=(C94EX_OT_export,C94EX_OT_import,C94EX_OT_advanced,C94EX_OT_status,C94EX_OT_cancel,C94EX_PT_exchange)
_PROPS=('c94_exchange_request','c94_exchange_jobs','c94_exchange_advanced')
def register():
    if _OWNED:raise RuntimeError('本交换界面已注册，保持原实例')
    for cls in _CLASSES:
        if issubclass(cls,bpy.types.Operator):
            family,name=cls.bl_idname.split('.')
            try:getattr(getattr(bpy.ops,family),name).get_rna_type()
            except (AttributeError,KeyError,RuntimeError):pass
            else:raise RuntimeError('交换operator已被占用：'+cls.bl_idname)
        elif bpy.types.Panel.bl_rna_get_subclass_py(cls.__name__,None) is not None:raise RuntimeError('交换Panel已被占用')
    if any(hasattr(bpy.types.WindowManager,name) for name in _PROPS):raise RuntimeError('交换界面属性已被占用')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_OWNED.append(cls)
        bpy.types.WindowManager.c94_exchange_request=StringProperty(name='交换请求',subtype='FILE_PATH');bpy.types.WindowManager.c94_exchange_jobs=StringProperty(name='输出目录',subtype='DIR_PATH');bpy.types.WindowManager.c94_exchange_advanced=BoolProperty(name='高级请求',default=False)
    except Exception:unregister();raise
def unregister():
    if _OWNED:
        for name in _PROPS:
            if hasattr(bpy.types.WindowManager,name):delattr(bpy.types.WindowManager,name)
    for cls in reversed(_OWNED):
        if cls.is_registered:bpy.utils.unregister_class(cls)
    _OWNED.clear()
