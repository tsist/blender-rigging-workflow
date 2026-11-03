"""Minimal bpy panel for the existing explicit face-coordinate transaction."""
import bpy
from bpy.props import EnumProperty,FloatProperty,FloatVectorProperty,StringProperty,PointerProperty
from facial_key_edit.contract import require
from .session import get_session

_SYNC=False
def owners(self,context):
    s=get_session(False)
    return s.items if s else [('千早爱音- 赋权躯干网格','千早爱音- 赋权躯干网格','')]
def values(p):
    return {'owner':p.owner,'operation':p.operation,'target':p.target,'delta':list(p.delta),'strength':float(p.strength)}
def sync_from_session():
    global _SYNC
    s=get_session(False)
    if s is None:return
    p=bpy.context.window_manager.hrig_d3_key_edit
    _SYNC=True
    try:
        for key,value in s.inputs.items():setattr(p,key,value)
    finally:_SYNC=False
def changed(self,context):
    if _SYNC:return
    s=get_session(False)
    if s is None:return
    try:s.set_inputs(values(self));s.last_error=None
    except Exception as error:
        s.last_error=getattr(error,'code',type(error).__name__)
        sync_from_session()
        try:s.log('input_refused',{'status':'refused','code':s.last_error,'state':s.status()})
        except Exception:pass

class HRIGD3KeyProperties(bpy.types.PropertyGroup):
    owner:EnumProperty(name='面部对象',items=owners,update=changed)
    operation:EnumProperty(name='操作',items=[('append','新增形态键','保留全部原有形态键'),
                                              ('edit','编辑具名形态键','只修改明确选区内坐标')],default='append',update=changed)
    target:StringProperty(name='形态键名称',default='HRIG_D3_UI_Cheek',update=changed)
    delta:FloatVectorProperty(name='局部XYZ增量',description='Blender局部单位；向量长度最多0.01',
                             size=3,default=(0.,-.001,0.),min=-.01,max=.01,precision=6,update=changed)
    strength:FloatProperty(name='预览强度',description='应用后以该权重预览；先恢复权重才能保存或撤销坐标',
                           default=.5,min=0.,max=1.,precision=3,update=changed)

class _Run:
    def run(self,event,callback):
        s=get_session()
        try:
            result=callback(s);s.last_error=None
            self.report({'INFO'},str(result.get('status','完成')))
            return {'FINISHED'}
        except Exception as error:
            s.last_error=getattr(error,'code',type(error).__name__)
            try:s.log(event+'_error',{'status':'error','code':s.last_error,'detail':str(getattr(error,'detail',str(error)))[:10000],
                                     'state':s.status()})
            except Exception:pass
            self.report({'ERROR'},s.last_error);return {'CANCELLED'}

class HRIGD3KeyRead(_Run,bpy.types.Operator):
    bl_idname='hrig_d3_key.read_selection';bl_label='读取实际网格选区'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return bool(s and not s.pending)
    def execute(self,context):return self.run('read_selection',lambda s:s.read_selection())
class HRIGD3KeyInspect(_Run,bpy.types.Operator):
    bl_idname='hrig_d3_key.inspect';bl_label='检查坐标计划'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return bool(s and not s.pending and s.selected_ids)
    def execute(self,context):return self.run('inspect',lambda s:s.inspect())
class HRIGD3KeyApply(_Run,bpy.types.Operator):
    bl_idname='hrig_d3_key.apply';bl_label='应用到形态键'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return bool(s and not s.pending and s.inspection)
    def execute(self,context):return self.run('apply',lambda s:s.apply())
class HRIGD3KeyPreview(_Run,bpy.types.Operator):
    bl_idname='hrig_d3_key.preview_value';bl_label='预览强度'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return bool(s and s.pending and not s.value_pending)
    def execute(self,context):return self.run('preview_value',lambda s:s.preview_value())
class HRIGD3KeyRestoreValue(_Run,bpy.types.Operator):
    bl_idname='hrig_d3_key.restore_value';bl_label='恢复预览权重'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return bool(s and s.value_pending)
    def execute(self,context):return self.run('restore_value',lambda s:s.restore_value())
class HRIGD3KeySave(_Run,bpy.types.Operator):
    bl_idname='hrig_d3_key.save_copy';bl_label='保存独立副本'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return bool(s and s.pending and not s.value_pending and not s.save_attempted)
    def execute(self,context):return self.run('save_copy',lambda s:s.save_copy())
class HRIGD3KeyRestore(_Run,bpy.types.Operator):
    bl_idname='hrig_d3_key.restore';bl_label='恢复坐标及新增键'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return bool(s and s.pending and not s.value_pending)
    def execute(self,context):return self.run('restore',lambda s:s.restore())

class HRIGD3KeyPanel(bpy.types.Panel):
    bl_idname='HRIG_D3_PT_key_edit';bl_label='HRIG 面部形态键编辑'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='HRIG D3'
    def draw(self,context):
        s=get_session(False);layout=self.layout
        if s is None:layout.label(text='尚未初始化形态键会话');return
        p=context.window_manager.hrig_d3_key_edit
        layout.label(text='编辑模式选点后，切回物体模式')
        fields=layout.column();fields.enabled=not s.pending
        for name in ('owner','operation','target','delta','strength'):fields.prop(p,name)
        layout.label(text='已读取原始顶点：%d'%len(s.selected_ids or ()))
        layout.operator('hrig_d3_key.read_selection')
        row=layout.row();row.operator('hrig_d3_key.inspect');row.operator('hrig_d3_key.apply')
        row=layout.row();row.operator('hrig_d3_key.preview_value');row.operator('hrig_d3_key.restore_value')
        layout.operator('hrig_d3_key.save_copy');layout.operator('hrig_d3_key.restore')
        if s.last_error:layout.label(text=s.last_error,icon='ERROR')

CLASSES=(HRIGD3KeyProperties,HRIGD3KeyRead,HRIGD3KeyInspect,HRIGD3KeyApply,HRIGD3KeyPreview,
         HRIGD3KeyRestoreValue,HRIGD3KeySave,HRIGD3KeyRestore,HRIGD3KeyPanel)
def register():
    for cls in CLASSES:
        if not cls.is_registered:bpy.utils.register_class(cls)
    if not hasattr(bpy.types.WindowManager,'hrig_d3_key_edit'):
        bpy.types.WindowManager.hrig_d3_key_edit=PointerProperty(type=HRIGD3KeyProperties)
def unregister():
    s=get_session(False);require(s is None or not s.pending,'KEY_UI_SESSION_PENDING')
    if hasattr(bpy.types.WindowManager,'hrig_d3_key_edit'):del bpy.types.WindowManager.hrig_d3_key_edit
    for cls in reversed(CLASSES):
        if cls.is_registered:bpy.utils.unregister_class(cls)
