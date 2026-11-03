"""Small opt-in UI. Reports live only in module memory, never a Text ID."""

import bpy
from bpy.props import PointerProperty

from .collector import collect


SESSION_REPORT = None
SESSION_DEPENDENCIES = None


def _armature_poll(_self, obj):
    return obj.type == 'ARMATURE'


class HRIG_P2_OT_inspect(bpy.types.Operator):
    bl_idname = 'hrig_p2.inspect'
    bl_label = '一次只读检查'
    bl_description = '读取原始RNA；不切帧、不求值、不保存、不修改场景'

    def execute(self, context):
        global SESSION_REPORT
        try:
            SESSION_REPORT = collect(context, context.window_manager.hrig_p2_armature)
        except Exception as exc:
            SESSION_REPORT = {'schema_version': 'p2.1', 'status': 'inspection_error',
                              'error_type': type(exc).__name__, 'error': str(exc)}
            self.report({'ERROR'}, '检查未完成：' + type(exc).__name__)
            return {'CANCELLED'}
        status = SESSION_REPORT['status']
        if status in {'budget_exceeded', 'selection_required'}:
            self.report({'ERROR'} if status == 'budget_exceeded' else {'WARNING'},
                        'P2检查未完成：' + status)
            return {'CANCELLED'}
        self.report({'INFO'}, 'P2只读检查：' + status)
        return {'FINISHED'}


class HRIG_P2_PT_probe(bpy.types.Panel):
    bl_label = 'P2研究原型 · 只读'
    bl_idname = 'HRIG_P2_PT_probe'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'P2 Rig Probe'

    def draw(self, context):
        layout = self.layout
        layout.label(text='编辑 / 模板 / FPS：均未验', icon='INFO')
        layout.prop(context.window_manager, 'hrig_p2_armature', text='目标Armature')
        layout.operator(HRIG_P2_OT_inspect.bl_idname)
        layout.operator(HRIG_P2_OT_dependencies.bl_idname)
        if SESSION_DEPENDENCIES is not None:
            summary = SESSION_DEPENDENCIES.get('summary', {})
            layout.label(text='依赖对象 %d / 引用 %d' % (summary.get('closure_objects',0), summary.get('edges',0)))
            layout.label(text='有限引用范围；不判定循环', icon='INFO')
        if SESSION_REPORT is None:
            return
        layout.label(text='状态：' + SESSION_REPORT['status'])
        counts = SESSION_REPORT.get('limits', {}).get('observed', {})
        if counts:
            layout.label(text='骨骼 %d / 约束 %d / Drivers %d' %
                         (counts['bones'], counts['constraints'], counts['drivers']))
        if SESSION_REPORT.get('context', {}).get('selection_error'):
            layout.label(text='请显式选择目标Armature', icon='QUESTION')
        if SESSION_REPORT.get('limits', {}).get('detail_accounting', {}).get('truncated'):
            layout.label(text='细节达上限；完整计数已保留', icon='INFO')


class HRIG_P2_OT_dependencies(bpy.types.Operator):
    bl_idname = 'hrig_p2.dependencies'
    bl_label = '只读依赖检查'
    bl_description = '读取约束、修改器、驱动和Cloth配置；不切帧、不求值、不保存'

    def execute(self, context):
        global SESSION_DEPENDENCIES
        from . import dependencies
        rig = context.window_manager.hrig_p2_armature
        if rig is None:
            rigs = [obj for obj in context.view_layer.objects if obj.type == 'ARMATURE']
            rig = rigs[0] if len(rigs) == 1 else None
        if rig is None:
            self.report({'WARNING'}, '请选择目标Armature')
            return {'CANCELLED'}
        try:
            SESSION_DEPENDENCIES = dependencies.collect(context, rig)
        except Exception as exc:
            SESSION_DEPENDENCIES = {'status': 'inspection_error', 'error_type': type(exc).__name__, 'error': str(exc)}
            self.report({'ERROR'}, '依赖检查未完成：' + type(exc).__name__)
            return {'CANCELLED'}
        if SESSION_DEPENDENCIES['status'] != 'ok':
            self.report({'WARNING'}, '依赖检查未完成：' + SESSION_DEPENDENCIES['status'])
            return {'CANCELLED'}
        self.report({'INFO'}, '只读依赖检查完成；覆盖范围有限')
        return {'FINISHED'}


CLASSES = (HRIG_P2_OT_inspect, HRIG_P2_OT_dependencies, HRIG_P2_PT_probe)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.hrig_p2_armature = PointerProperty(
        name='目标Armature', type=bpy.types.Object, poll=_armature_poll,
        options={'SKIP_SAVE'})


def unregister():
    global SESSION_REPORT, SESSION_DEPENDENCIES
    if hasattr(bpy.types.WindowManager, 'hrig_p2_armature'):
        del bpy.types.WindowManager.hrig_p2_armature
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    SESSION_REPORT = None
    SESSION_DEPENDENCIES = None
