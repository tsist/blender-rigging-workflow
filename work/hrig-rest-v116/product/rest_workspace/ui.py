"""Native per-atom fields over the shared batch transaction; no raw JSON UI."""
import json
import bpy
from bpy.props import CollectionProperty,FloatProperty,FloatVectorProperty,PointerProperty,StringProperty,BoolProperty
from .session import get_session
_ASSIGNING=False
def changed(self,context):
    if _ASSIGNING:return
    s=get_session(False)
    if s:
        if s.pending:assign(context.window_manager.hrig_d2_batch,s.tx.request)
        else:s.invalidate()
class Atom(bpy.types.PropertyGroup):
    template:StringProperty()
    bone:StringProperty(name='目标骨',update=changed)
    donor:StringProperty(name='复制来源',update=changed)
    parent:StringProperty(name='父骨（空为根）',update=changed)
    collections:StringProperty(name='骨集合',update=changed)
    head:FloatVectorProperty(name='头端位置',size=3,precision=6,update=changed)
    tail:FloatVectorProperty(name='尾端位置',size=3,precision=6,update=changed)
    roll:FloatProperty(name='骨轴 Roll',precision=5,update=changed)
    use_connect:BoolProperty(name='连接父骨尾端',update=changed)
class SidePair(bpy.types.PropertyGroup):
    left:StringProperty(name='左侧映射',update=changed)
    right:StringProperty(name='右侧映射',update=changed)
class Properties(bpy.types.PropertyGroup):
    atoms:CollectionProperty(type=Atom)
    side_pairs:CollectionProperty(type=SidePair)
def assign(p,request):
    global _ASSIGNING
    _ASSIGNING=True
    try:
        p.atoms.clear();p.side_pairs.clear()
        for a in request['atoms']:
            item=p.atoms.add();item.template=json.dumps(a,ensure_ascii=False);item.bone=a['bone']
            if a['op']=='create':item.donor=a['donor'];item.parent=a['parent'] or '';item.collections=';'.join(a['collections'])
            for k,v in a['fields'].items():setattr(item,k,v if v is not None else '')
        for names in request['side_pairs']:
            item=p.side_pairs.add();item.left,item.right=names
    finally:_ASSIGNING=False
def execute(op,fn):
    try:
        s=get_session()
        with bpy.context.temp_override(object=s.tx.base.rig,active_object=s.tx.base.rig):r=fn()
        op.report({'INFO'},r['status']);return {'FINISHED'}
    except Exception as e:op.report({'ERROR'},getattr(e,'code',type(e).__name__)+': '+str(e));return {'CANCELLED'}
class Inspect(bpy.types.Operator):
    bl_idname='hrig_batch.inspect';bl_label='1 检查调整方案'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and not s.pending and context.mode=='OBJECT'
    def execute(self,c):return execute(self,lambda:get_session().inspect(c.window_manager.hrig_d2_batch))
class Preview(bpy.types.Operator):
    bl_idname='hrig_batch.preview';bl_label='2 预览骨架调整'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and not s.pending and s.snapshot_token is not None
    def execute(self,c):return execute(self,lambda:get_session().preview())
class Restore(bpy.types.Operator):
    bl_idname='hrig_batch.restore';bl_label='3 恢复原骨架'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and s.pending
    def execute(self,c):return execute(self,lambda:get_session().restore())
class Save(bpy.types.Operator):
    bl_idname='hrig_batch.save';bl_label='保存调整副本'
    @classmethod
    def poll(cls,context):
        s=get_session(False);return s is not None and s.pending and s.tx.applied is not None and not s.save_attempted
    def execute(self,c):return execute(self,lambda:get_session().save_copy())
class Panel(bpy.types.Panel):
    bl_label='成组骨架调整';bl_idname='HRIG_BATCH_PT_rest';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='HRIG 骨架'
    def draw(self,c):
        s=get_session(False);l=self.layout
        if s is None:l.label(text='请选择骨架调整方案');return
        controls=l.column();controls.enabled=not s.pending;p=c.window_manager.hrig_d2_batch
        for item in p.atoms:
            a=json.loads(item.template);box=controls.box();box.label(text={'set':'修改','create':'新增','replace':'同名替换'}[a['op']])
            box.prop(item,'bone')
            if a['op']=='create':
                for k in ('donor','parent','collections'):box.prop(item,k)
            for k in a['fields']:box.prop(item,k)
        if p.side_pairs:
            box=controls.box();box.label(text='明确左右映射')
            for pair in p.side_pairs:box.prop(pair,'left');box.prop(pair,'right')
        for op in ('inspect','preview','restore','save'):l.operator('hrig_batch.'+op)
        l.label(text='预览中' if s.pending else '空闲')
        if s.last_error:l.label(text=s.last_error,icon='ERROR')
CLASSES=(Atom,SidePair,Properties,Inspect,Preview,Restore,Save,Panel)
def register():
    for cls in CLASSES:
        if not cls.is_registered:bpy.utils.register_class(cls)
    if not hasattr(bpy.types.WindowManager,'hrig_d2_batch'):bpy.types.WindowManager.hrig_d2_batch=PointerProperty(type=Properties)
