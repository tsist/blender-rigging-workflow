"""Minimal actual D3 panel. Registration never opens a file or changes a frame."""
import json
import uuid
import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty, PointerProperty
from .session import get_session
from .contract import SCHEMA, CLIP_SCHEMA, decode_request, require


def changed(self, context):
    s = get_session(False)
    if s is not None:
        s.invalidate()


def controls(self, context):
    s = get_session(False)
    return s._control_items if s else [('blink.both','双眼闭合','')]


def presets(self, context):
    s = get_session(False)
    return s._preset_items if s else [('manual','手动控制','')]


class HRIGD3Properties(bpy.types.PropertyGroup):
    control: EnumProperty(name='控制', items=controls, update=changed)
    preset: EnumProperty(name='预设', items=presets, update=changed)
    value: FloatProperty(name='归一化输入', default=.5, min=-1., max=1., update=changed)
    strength: FloatProperty(name='整体强度', default=1., min=0., max=1., update=changed)
    combination: StringProperty(name='组合 JSON', description='手动模式：控制ID到归一化值的JSON对象', default='', update=changed)
    observe_frame: IntProperty(name='观察帧', default=12, min=0, max=1048573)


def request_from_ui():
    p = bpy.context.window_manager.hrig_d3_face
    if p.combination.strip():
        require(p.preset == 'manual', 'UI_PRESET_COMBINATION_CONFLICT')
        # Feed original JSON through the strict duplicate-key parser.
        return decode_request('{"schema":'+json.dumps(SCHEMA)+',"mode":"independent","preset":"manual","strength":'
                              +json.dumps(p.strength)+',"values":'+p.combination+'}')
    return {'schema':SCHEMA, 'mode':'independent', 'preset':p.preset,
            'strength':p.strength, 'values':{p.control:p.value} if p.preset == 'manual' else {}}


def sync_from_request(request):
    p = bpy.context.window_manager.hrig_d3_face
    p.preset = request['preset']
    p.strength = request['strength']
    p.combination = ''
    if request['preset'] == 'manual' and len(request['values']) == 1:
        p.control, p.value = next(iter(request['values'].items()))


class _Run:
    def run(self, event, callback):
        s = get_session()
        try:
            result = callback(s)
            s.last_error = None
            self.report({'INFO'}, str(result.get('status', '完成')))
            return {'FINISHED'}
        except Exception as error:
            s.last_error = getattr(error,'code',type(error).__name__)
            s.log(event+'_error', {'status':'error','code':s.last_error,'detail':str(getattr(error,'detail',str(error)))[:12000],
                                  'state':s.status()})
            self.report({'ERROR'}, s.last_error)
            return {'CANCELLED'}


class HRIGD3Inspect(_Run, bpy.types.Operator):
    bl_idname='hrig_d3.inspect'; bl_label='检查'
    def execute(self, context):
        def perform(s):
            s.set_request(request_from_ui())
            return s.inspect()
        return self.run('inspect', perform)


class HRIGD3Preview(_Run, bpy.types.Operator):
    bl_idname='hrig_d3.preview'; bl_label='预览'
    @classmethod
    def poll(cls, context):
        s=get_session(False)
        return bool(s and s.inspection and not s.preview_pending and not s.animation_pending)
    def execute(self, context):
        return self.run('preview', lambda s:s.preview())


class HRIGD3Restore(_Run, bpy.types.Operator):
    bl_idname='hrig_d3.restore'; bl_label='恢复预览'
    @classmethod
    def poll(cls, context):
        s=get_session(False); return bool(s and s.preview_pending)
    def execute(self, context):
        return self.run('restore', lambda s:s.restore())


class HRIGD3Author(_Run, bpy.types.Operator):
    bl_idname='hrig_d3.author'; bl_label='写入独立表情动画'
    @classmethod
    def poll(cls, context):
        s=get_session(False); return bool(s and not s.preview_pending and not s.animation_pending)
    def execute(self, context):
        def perform(s):
            desired=request_from_ui(); s.set_request(desired)
            neutral={'schema':SCHEMA,'mode':'independent','preset':'neutral','strength':1.,'values':{}}
            clip={'schema':CLIP_SCHEMA,'name':'HRIG_D3_'+uuid.uuid4().hex[:16],
                  'keys':[{'frame':f,'request':r} for f,r in [(0,neutral),(12,desired),(24,neutral)]]}
            inspected=s.inspect_animation(clip)
            return s.author_animation(inspected['snapshot_token'])
        return self.run('author', perform)


class HRIGD3Frame(_Run, bpy.types.Operator):
    bl_idname='hrig_d3.frame'; bl_label='观察动画帧'
    @classmethod
    def poll(cls, context):
        s=get_session(False); return bool(s and s.animation_pending)
    def execute(self, context):
        return self.run('frame',lambda s:s.show_frame(context.window_manager.hrig_d3_face.observe_frame))


class HRIGD3Undo(_Run, bpy.types.Operator):
    bl_idname='hrig_d3.undo_animation'; bl_label='回到创作帧并撤销动画'
    @classmethod
    def poll(cls, context):
        s=get_session(False); return bool(s and s.animation_pending)
    def execute(self, context):
        return self.run('undo_animation',lambda s:s.undo_animation(return_to_author_frame=True))


class HRIGD3Save(_Run, bpy.types.Operator):
    bl_idname='hrig_d3.save_copy'; bl_label='保存独立副本'
    @classmethod
    def poll(cls, context):
        s=get_session(False)
        return bool(s and (s.preview_pending or s.animation_pending) and not s.save_attempted)
    def execute(self, context):
        return self.run('save',lambda s:s.save_copy())


class HRIGD3Panel(bpy.types.Panel):
    bl_idname='HRIG_D3_PT_facial_controls'; bl_label='HRIG 面部控制'
    bl_space_type='VIEW_3D'; bl_region_type='UI'; bl_category='HRIG D3'
    def draw(self, context):
        s=get_session(False); layout=self.layout
        if s is None:
            layout.label(text='尚未初始化面部会话'); return
        p=context.window_manager.hrig_d3_face
        inputs=layout.column();inputs.enabled=not (s.preview_pending or s.animation_pending)
        inputs.prop(p,'preset');inputs.prop(p,'strength')
        if p.preset=='manual':
            inputs.prop(p,'control');inputs.prop(p,'value');inputs.prop(p,'combination')
        layout.label(text='独立口型模式')
        row=layout.row();row.enabled=not(s.preview_pending or s.animation_pending)
        row.operator('hrig_d3.inspect');row.operator('hrig_d3.preview')
        layout.operator('hrig_d3.restore');layout.operator('hrig_d3.author')
        row=layout.row();row.prop(p,'observe_frame');row.operator('hrig_d3.frame')
        layout.operator('hrig_d3.undo_animation');layout.operator('hrig_d3.save_copy')
        if s.last_error:layout.label(text=s.last_error,icon='ERROR')
        if s.log_error:layout.label(text='证据写入失败：'+s.log_error[:80],icon='ERROR')


CLASSES=(HRIGD3Properties,HRIGD3Inspect,HRIGD3Preview,HRIGD3Restore,HRIGD3Author,HRIGD3Frame,HRIGD3Undo,HRIGD3Save,HRIGD3Panel)


def register():
    for cls in CLASSES:
        if not hasattr(bpy.types,cls.__name__):bpy.utils.register_class(cls)
    if not hasattr(bpy.types.WindowManager,'hrig_d3_face'):
        bpy.types.WindowManager.hrig_d3_face=PointerProperty(type=HRIGD3Properties)


def unregister():
    s=get_session(False)
    require(s is None or not(s.preview_pending or s.animation_pending),'FACE_SESSION_PENDING')
    if hasattr(bpy.types.WindowManager,'hrig_d3_face'):del bpy.types.WindowManager.hrig_d3_face
    for cls in reversed(CLASSES):
        if hasattr(bpy.types,cls.__name__):bpy.utils.unregister_class(cls)
