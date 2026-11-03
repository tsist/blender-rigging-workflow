"""Four independent FK length inputs; all scene work goes through the facade."""
import bpy
from bpy.props import FloatProperty, IntProperty, PointerProperty
from native_controls.contract import require
from .session import FIELDS, FRAMES, get_session

_SYNC = False

def sync_from_session():
    global _SYNC
    s = get_session(False)
    if s is None: return
    props = bpy.context.window_manager.hrig_local_proportion
    _SYNC = True
    try:
        for name,value in s.inputs.items(): setattr(props,name,value)
    finally: _SYNC = False

def changed(self, context):
    if _SYNC: return
    s = get_session(False)
    if s is None: return
    try: s.set_inputs({name:float(getattr(self,name)) for name in FIELDS})
    except Exception: sync_from_session()

class HRIGProportionProperties(bpy.types.PropertyGroup):
    arm_l: FloatProperty(name='左臂FK链比例',default=1.,min=.9,max=1.1,precision=3,update=changed)
    arm_r: FloatProperty(name='右臂FK链比例',default=1.,min=.9,max=1.1,precision=3,update=changed)
    leg_l: FloatProperty(name='左腿FK链比例',default=1.,min=.9,max=1.1,precision=3,update=changed)
    leg_r: FloatProperty(name='右腿FK链比例',default=1.,min=.9,max=1.1,precision=3,update=changed)

class _Run:
    def run(self, callback):
        s = get_session()
        try:
            result = callback(s); sync_from_session()
            self.report({'INFO'},str(result.get('status','完成')))
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},getattr(error,'code',type(error).__name__)+': '+str(error))
            return {'CANCELLED'}

def ready():
    s = get_session(False)
    return s is not None and s.workspace_available()

class HRIGProportionInspect(_Run,bpy.types.Operator):
    bl_idname = 'hrig_proportion.inspect'; bl_label = '检查比例动作'
    @classmethod
    def poll(cls,context):
        s=get_session(False); return bool(ready() and not s.pending and any(v!=1. for v in s.inputs.values()))
    def execute(self,context): return self.run(lambda s:s.inspect())

class HRIGProportionAuthor(_Run,bpy.types.Operator):
    bl_idname = 'hrig_proportion.author'; bl_label = '写入比例预览动作'
    bl_description = '复制并保留其它动作曲线；四肢设为FK，在0/12/24帧写入比例1/峰值/1'
    @classmethod
    def poll(cls,context):
        s=get_session(False); return bool(ready() and not s.pending and s.inspection is not None)
    def execute(self,context): return self.run(lambda s:s.author())

class HRIGProportionFrame(_Run,bpy.types.Operator):
    bl_idname = 'hrig_proportion.frame'; bl_label = '观察比例时刻'
    frame: IntProperty(default=12,min=0,max=24)
    @classmethod
    def poll(cls,context):
        s=get_session(False); return bool(ready() and s.pending)
    def execute(self,context): return self.run(lambda s:s.show_frame(self.frame))

class HRIGProportionUndo(_Run,bpy.types.Operator):
    bl_idname = 'hrig_proportion.undo'; bl_label = '归零并撤销比例动作'
    @classmethod
    def poll(cls,context):
        s=get_session(False); return bool(ready() and s.pending)
    def execute(self,context): return self.run(lambda s:s.undo())

class HRIGProportionOff(_Run,bpy.types.Operator):
    bl_idname = 'hrig_proportion.reset_off'; bl_label = '关闭比例并重置为1'
    @classmethod
    def poll(cls,context): return ready()
    def execute(self,context): return self.run(lambda s:s.reset_off())

class HRIGProportionSave(_Run,bpy.types.Operator):
    bl_idname = 'hrig_proportion.save_copy'; bl_label = '回第0帧并保存独立副本'
    bl_description = '保存待独立重开验证的工作副本；源文件和原动作不覆盖'
    @classmethod
    def poll(cls,context):
        s=get_session(False); return bool(ready() and s.pending and not s.save_attempted)
    def execute(self,context): return self.run(lambda s:s.save_copy())

class HRIGProportionPanel(bpy.types.Panel):
    bl_idname = 'HRIG_PT_local_proportion'; bl_label = 'HRIG 局部比例'
    bl_space_type='VIEW_3D'; bl_region_type='UI'; bl_category='HRIG D'
    def draw(self,context):
        s=get_session(False); layout=self.layout
        if s is None: layout.label(text='请在受管工作区初始化比例会话'); return
        layout.label(text='FK链长度；含原子级，默认/关闭 = 1')
        layout.label(text='其余动作保留；第12帧为比例峰值')
        fields=layout.column(); fields.enabled=not s.pending and s.workspace_available()
        for name in FIELDS: fields.prop(context.window_manager.hrig_local_proportion,name)
        row=layout.row(); row.operator('hrig_proportion.inspect'); row.operator('hrig_proportion.author')
        row=layout.row(align=True)
        for frame in FRAMES: row.operator('hrig_proportion.frame',text=str(frame)).frame=frame
        layout.operator('hrig_proportion.undo'); layout.operator('hrig_proportion.reset_off')
        layout.operator('hrig_proportion.save_copy')
        layout.label(text='身体动作：'+('待撤销' if s.pending else '空闲'))
        if s.last_observation:
            layout.label(text='最近核验帧：%g'%s.last_observation['frame'])
        if s.last_error: layout.label(text=s.last_error,icon='ERROR')
        if s.last_log_error: layout.label(text='证据写入异常；操作未重试',icon='ERROR')

CLASSES=(HRIGProportionProperties,HRIGProportionInspect,HRIGProportionAuthor,HRIGProportionFrame,
    HRIGProportionUndo,HRIGProportionOff,HRIGProportionSave,HRIGProportionPanel)

def register():
    for cls in CLASSES:
        if not cls.is_registered: bpy.utils.register_class(cls)
    if not hasattr(bpy.types.WindowManager,'hrig_local_proportion'):
        bpy.types.WindowManager.hrig_local_proportion=PointerProperty(type=HRIGProportionProperties)

def unregister():
    s=get_session(False); require(s is None or not s.pending,'PROPORTION_SESSION_PENDING')
    if hasattr(bpy.types.WindowManager,'hrig_local_proportion'): del bpy.types.WindowManager.hrig_local_proportion
    for cls in reversed(CLASSES):
        if cls.is_registered: bpy.utils.unregister_class(cls)
