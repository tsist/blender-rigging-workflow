# SPDX-License-Identifier: GPL-3.0-or-later
"""Chinese pose-draft controls sharing the persistent bone-input core."""
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from . import face_pose_library as bank

OPTIONS = 'c94_face_pose_library_options_v1'
_REGISTERED = []
_ERRORS = (ValueError, RuntimeError, ReferenceError, TypeError, KeyError, AttributeError)


class C94FPL_Options(bpy.types.PropertyGroup):
    rig: PointerProperty(name='角色骨架', type=bpy.types.Object,
                         poll=lambda self, obj: obj.type == 'ARMATURE')
    name: StringProperty(name='当前姿态名称', default='姿态草稿', maxlen=128)
    replace: BoolProperty(name='更新同名姿态', default=False)
    pose_a: StringProperty(name='姿态A')
    pose_b: StringProperty(name='姿态B')
    weight_a: FloatProperty(name='A强度', default=1, min=0, max=1, subtype='FACTOR')
    weight_b: FloatProperty(name='B强度', default=1, min=0, max=1, subtype='FACTOR')
    message: StringProperty(options={'SKIP_SAVE'})


class C94FPL_OT_Capture(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_pose_store'
    bl_label = '保存当前骨控姿态'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            report = bank.capture(options.rig, options.name, replace=options.replace)
            options.message = '已保存：' + report['name']
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FPL_OT_Select(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_pose_pick'
    bl_label = '选作混合输入'
    pose_name: StringProperty()
    slot: EnumProperty(items=[('A', 'A', ''), ('B', 'B', '')])

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        setattr(options, 'pose_a' if self.slot == 'A' else 'pose_b', self.pose_name)
        return {'FINISHED'}


class C94FPL_OT_Remove(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_pose_remove'
    bl_label = '删除此姿态草稿'
    bl_options = {'REGISTER', 'UNDO'}
    pose_name: StringProperty()

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            bank.remove(options.rig, self.pose_name)
            for field in ('pose_a', 'pose_b'):
                if getattr(options, field) == self.pose_name:
                    setattr(options, field, '')
            options.message = '已删除：' + self.pose_name
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FPL_OT_Mix(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_pose_mix'
    bl_label = '混合到当前骨控'
    bl_description = '从中立按A、B顺序混合骨输入；不直接叠加Key，不自动创建Key'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            weights = [(name, weight) for name, weight in
                       ((options.pose_a, options.weight_a), (options.pose_b, options.weight_b)) if name]
            bank.mix(options.rig, weights)
            options.message = '已应用骨控混合，可继续调整或采样完整姿态'
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FPL_OT_Restore(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_pose_restore'
    bl_label = '恢复本次混合前输入'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            bank.restore_previous(options.rig)
            options.message = '已恢复混合前的面部骨输入'
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FPL_PT_Library(bpy.types.Panel):
    bl_idname = 'C94FPL_PT_library'
    bl_label = '面部骨控姿态草稿与混合'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '面部'
    bl_order = 2
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        options = getattr(context.scene, OPTIONS)
        layout.label(text='骨控设计与混合；已接受的权重限制不阻塞')
        layout.prop(options, 'rig')
        if options.rig is None:
            return
        try:
            report = bank.inspect(options.rig)
        except _ERRORS as exc:
            layout.label(text=str(exc), icon='INFO')
            return
        layout.prop(options, 'name')
        layout.prop(options, 'replace')
        layout.operator('c94_workflow.face_pose_store')
        for name in report['pose_names']:
            row = layout.row(align=True)
            row.label(text=name)
            for slot in ('A', 'B'):
                op = row.operator('c94_workflow.face_pose_pick', text=slot)
                op.pose_name, op.slot = name, slot
            op = row.operator('c94_workflow.face_pose_remove', text='', icon='X')
            op.pose_name = name
        for slot in ('a', 'b'):
            row = layout.row(align=True)
            row.prop(options, 'pose_' + slot)
            row.prop(options, 'weight_' + slot)
        layout.operator('c94_workflow.face_pose_mix')
        row = layout.row()
        row.enabled = report['has_previous']
        row.operator('c94_workflow.face_pose_restore')
        layout.label(text='属性与Key保持中立；混合顺序为A→B')
        layout.label(text='当前结果用姿态→Key入口整姿态采样')
        if options.message:
            layout.label(text=options.message)


_CLASSES = (C94FPL_Options, C94FPL_OT_Capture, C94FPL_OT_Select,
            C94FPL_OT_Remove, C94FPL_OT_Mix, C94FPL_OT_Restore, C94FPL_PT_Library)


def register():
    global _C94_OPTIONS_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('该面板属性已由其他实例持有；原属性保持')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94FPL_Options))
        _C94_OPTIONS_OWNED = True
    except Exception:
        unregister()
        raise


def unregister():
    if hasattr(bpy.types.Scene, OPTIONS):
        _release_workspace_options()
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()


_C94_OPTIONS_OWNED = False

def _release_workspace_options():
    global _C94_OPTIONS_OWNED
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if (_C94_OPTIONS_OWNED and prop and prop.type == 'POINTER'
            and prop.fixed_type == C94FPL_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
