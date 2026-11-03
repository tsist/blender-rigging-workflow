"""Minimal native roll editor over the shared explicit session."""
import bpy
from bpy.props import EnumProperty,FloatProperty,FloatVectorProperty,PointerProperty,StringProperty
from .session import get_session
_ASSIGNING=False
def choices(self,context):
    s=get_session(False);return s.bone_items if s else [('uninitialized','未初始化','')]
def changed(self,context):
    if _ASSIGNING:return
    s=get_session(False)
    if s:
        if s.pending:
            assign_request(self,s.tx._pending['request']);s.last_error='REST_RESTORE_FIRST'
        else:s.invalidate()
def assign_request(p,request):
    global _ASSIGNING
    _ASSIGNING=True
    try:
        p.bone=request['bone'];p.edit_mode={'hrig.rest-roll-delta.v1':'ROLL','hrig.rest-tail-delta.v1':'TAIL','hrig.rest-head-delta.v1':'HEAD','hrig.rest-parent-change.v1':'PARENT'}[request['schema']]
        if p.edit_mode=='TAIL':p.tail_delta=request['delta']
        elif p.edit_mode=='HEAD':p.head_delta=request['delta']
        elif p.edit_mode=='PARENT':p.from_parent=request['from_parent'];p.to_parent=request['to_parent']
        else:p.delta=request['delta']
    finally:_ASSIGNING=False
class HRIG_D2_Properties(bpy.types.PropertyGroup):
    bone:EnumProperty(name='目标骨',items=choices,update=changed)
    edit_mode:EnumProperty(name='编辑方式',items=[('ROLL','Roll 骨轴','仅修改原生Roll'),('TAIL','尾端位置','单侧Armature局部空间位移'),('HEAD','头端位置','单侧Armature局部空间位移'),('PARENT','父级关系','将叶骨改挂到已有祖父骨，保留编辑端点和Roll')],update=changed)
    delta:FloatProperty(name='Roll 偏移（弧度）',default=.05,soft_min=-.5,soft_max=.5,precision=4,update=changed)
    tail_delta:FloatVectorProperty(name='尾端位移（骨架局部）',size=3,default=(0.,.001,0.),precision=6,update=changed)
    head_delta:FloatVectorProperty(name='头端位移（骨架局部）',size=3,default=(0.,.001,0.),precision=6,update=changed)
    from_parent:StringProperty(name='当前父骨',default='',update=changed)
    to_parent:StringProperty(name='目标父骨',default='',update=changed)
def execute(op,fn):
    try:
        s=get_session()
        with bpy.context.temp_override(object=s.tx.rig,active_object=s.tx.rig):r=fn()
        op.report({'INFO'},r.get('status','完成'));return {'FINISHED'}
    except Exception as e:op.report({'ERROR'},getattr(e,'code',type(e).__name__)+': '+str(e));return {'CANCELLED'}
class HRIG_D2_OT_inspect(bpy.types.Operator):
    bl_idname='hrig_d2.rest_inspect';bl_label='1 检查与原生参考'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and not s.pending and context.mode=='OBJECT'
    def execute(self,context):return execute(self,lambda:get_session().inspect(context.window_manager.hrig_d2_rest))
class HRIG_D2_OT_preview(bpy.types.Operator):
    bl_idname='hrig_d2.rest_preview';bl_label='2 预览单侧编辑'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and not s.pending and s.snapshot_token is not None and context.mode=='OBJECT'
    def execute(self,context):return execute(self,lambda:get_session().preview())
class HRIG_D2_OT_restore(bpy.types.Operator):
    bl_idname='hrig_d2.rest_restore';bl_label='3 完整恢复原骨架'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and s.pending
    def execute(self,context):return execute(self,lambda:get_session().restore())
class HRIG_D2_OT_save(bpy.types.Operator):
    bl_idname='hrig_d2.rest_save';bl_label='第0帧保存副本与 Profile'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and s.pending and s.tx.pending['phase']=='applied' and not s.save_attempted and context.mode=='OBJECT' and context.scene.frame_current_final==0
    def execute(self,context):return execute(self,lambda:get_session().save_copy())
class HRIG_D2_PT_rest(bpy.types.Panel):
    bl_label='D2 局部骨骼编辑';bl_idname='HRIG_D2_PT_rest';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='HRIG D2'
    def draw(self,context):
        s=get_session(False);l=self.layout
        if s is None:l.label(text='请先初始化编辑会话');return
        p=context.window_manager.hrig_d2_rest;c=l.column();c.enabled=not s.pending;c.prop(p,'edit_mode');c.prop(p,'bone')
        if p.edit_mode in ('TAIL','HEAD'):
            c.prop(p,'tail_delta' if p.edit_mode=='TAIL' else 'head_delta');bone=s.tx.original_data.bones.get(p.bone)
            if bone:
                c.label(text='源 head: '+', '.join(format(v,'.6g') for v in bone.head_local))
                c.label(text='源 tail: '+', '.join(format(v,'.6g') for v in bone.tail_local))
            c.label(text='仅尾端；保留 head、Roll 与父级' if p.edit_mode=='TAIL' else '仅头端；保留 tail、Roll 与父级')
        elif p.edit_mode=='PARENT':
            c.prop(p,'from_parent');c.prop(p,'to_parent')
            c.label(text='仅可改挂已有祖父骨')
            c.label(text='保留编辑端点、Roll及连接状态')
            c.label(text='新父级会改变控制传递，请检查预览')
        else:c.prop(p,'delta')
        l.label(text='显式单侧编辑，保留原镜像设置')
        for op in ('inspect','preview','restore','save'):l.operator('hrig_d2.rest_'+op)
        pending=s.tx.pending if s.pending else None
        l.label(text='状态：'+(pending['phase'] if pending else '空闲'))
        if pending and pending['phase'] in ('recovery_edit','recovery_required'):
            l.label(text='编辑已中断，请先完整恢复',icon='ERROR')
            l.label(text='检测到外部改动时会保留现场')
        l.label(text='新 Profile 需独立重开验证')
        if s.last_error:l.label(text=s.last_error,icon='ERROR')
        if s.log_error:l.label(text='证据写入异常，请查看日志',icon='ERROR')
_CLASSES=(HRIG_D2_Properties,HRIG_D2_OT_inspect,HRIG_D2_OT_preview,HRIG_D2_OT_restore,HRIG_D2_OT_save,HRIG_D2_PT_rest)
def register():
    for cls in _CLASSES:
        if not cls.is_registered:bpy.utils.register_class(cls)
    if not hasattr(bpy.types.WindowManager,'hrig_d2_rest'):bpy.types.WindowManager.hrig_d2_rest=PointerProperty(type=HRIG_D2_Properties)
def unregister():
    s=get_session(False)
    if s and s.pending:raise RuntimeError('Restore owned rest preview first')
    if hasattr(bpy.types.WindowManager,'hrig_d2_rest'):del bpy.types.WindowManager.hrig_d2_rest
    for cls in reversed(_CLASSES):
        if cls.is_registered:bpy.utils.unregister_class(cls)
