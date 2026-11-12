# SPDX-License-Identifier: GPL-3.0-or-later
"""Chinese local pose-target authoring UI; all geometry work lives in the core."""
import json

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty

OPTIONS = 'c94_corrective_authoring_options_v1'
_REGISTERED = []


def _mesh(_self, obj):
    return obj.type == 'MESH'


def _associated_source(options):
    """The target's persisted reference owns the association after capture."""
    if options.target:
        from .corrective_authoring import _SOURCE
        source = options.target.get(_SOURCE)
        if not isinstance(source, bpy.types.Object) or source.type != 'MESH':
            raise ValueError('目标的生产网格引用已失效；保留目标供检查')
        options.source = source
    return options.source


def _activate(context, obj):
    if obj.name not in context.view_layer.objects:
        raise ValueError('对象不在当前视图层')
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    obj.hide_set(False)
    for item in tuple(context.selected_objects):
        item.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _indices(source, method, group_name):
    if method == 'SELECTED':
        if source.mode == 'EDIT':
            import bmesh
            bm = bmesh.from_edit_mesh(source.data)
            bm.verts.index_update()
            return [v.index for v in bm.verts if v.select and not v.hide]
        return [v.index for v in source.data.vertices if v.select and not v.hide]
    group = source.vertex_groups.get(group_name)
    if group is None:
        raise ValueError('选择实际存在的范围顶点组')
    if source.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(source.data)
        bm.verts.index_update()
        deform = bm.verts.layers.deform.active
        if deform is None:
            return []
        return [v.index for v in bm.verts if not v.hide
                and v[deform].get(group.index, 0.0) > 0]
    return [v.index for v in source.data.vertices if not v.hide
            and any(g.group == group.index and g.weight > 0 for g in v.groups)]


def _restore_display(context, options):
    source, target = _associated_source(options), options.target
    if target and target.name in context.view_layer.objects:
        target.hide_set(True)
    if source and source.name in context.view_layer.objects:
        source.hide_set(options.source_hidden_before)
        if not options.source_hidden_before:
            _activate(context, source)
    options.editing_display = False


class C94CORRECT_Options(bpy.types.PropertyGroup):
    source: PointerProperty(name='生产网格', type=bpy.types.Object, poll=_mesh)
    target: PointerProperty(name='姿态目标', type=bpy.types.Object, poll=_mesh)
    scope_method: EnumProperty(name='修改范围', items=(
        ('SELECTED', '选中顶点', '仅使用生产网格当前选中的可见顶点'),
        ('GROUP', '顶点组', '使用所选组中有正权重的可见顶点')))
    group_name: StringProperty(name='范围顶点组')
    target_name: StringProperty(name='目标名称', default='局部姿态修正目标')
    key_name: StringProperty(name='形态键名称', default='局部姿态修正')
    max_rest_displacement_meters: FloatProperty(name='最大静置位移（米）', default=.02,
        min=1e-6, soft_max=.05, precision=4,
        description='创建目标时冻结的保护上限；不是质量评分，应用时不能临时提高')
    source_hidden_before: BoolProperty(options={'HIDDEN'})
    editing_display: BoolProperty(options={'HIDDEN'})
    summary: StringProperty(options={'HIDDEN'})


class C94CORRECT_OT_UseSource(bpy.types.Operator):
    bl_idname = 'c94_workflow.corrective_use_source'
    bl_label = '使用当前网格'

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        obj = context.object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, '先选择生产网格')
            return {'CANCELLED'}
        if options.target:
            self.report({'ERROR'}, '先删除已有目标副本，再更换生产网格')
            return {'CANCELLED'}
        options.source = obj
        return {'FINISHED'}


class C94CORRECT_OT_Begin(bpy.types.Operator):
    bl_idname = 'c94_workflow.corrective_begin'
    bl_label = '创建局部姿态目标'
    bl_description = '从当前姿态生成可编辑副本；保留生产网格和原权重'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import corrective_authoring
        options = getattr(context.scene, OPTIONS)
        source = options.source
        old_mode = source.mode if source else 'OBJECT'
        try:
            if options.target:
                raise ValueError('已有目标副本，请先检查、生成修正键或删除副本')
            if not source or source.name not in context.view_layer.objects:
                raise ValueError('选择当前视图层中的生产网格')
            if context.mode != 'OBJECT' and context.object != source:
                raise ValueError('先结束其他对象的编辑或姿态模式')
            if source.mode == 'EDIT' and len(context.objects_in_mode) != 1:
                raise ValueError('请结束多对象编辑，仅保留当前生产网格')
            ids = _indices(source, options.scope_method, options.group_name)
            if not ids:
                raise ValueError('修改范围为空；先选局部顶点或指定顶点组')
            if source.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            target = corrective_authoring.begin(source, ids, name=options.target_name,
                max_rest_displacement_meters=options.max_rest_displacement_meters)
        except Exception as exc:
            if source and old_mode == 'EDIT' and source.mode == 'OBJECT' and context.object == source:
                bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        # A valid owned target now exists. UI display failures must not disguise it.
        options.source_hidden_before = source.hide_get()
        options.target = target
        options.summary = f'已创建目标，允许修改 {len(ids)} 个顶点'
        try:
            _activate(context, target)
            source.hide_set(True)
            options.editing_display = True
        except Exception as exc:
            self.report({'WARNING'}, '目标已创建；请在大纲中选择目标：' + str(exc))
        self.report({'INFO'}, '可编辑或雕刻目标；保持当前骨架姿态，完成后检查范围')
        return {'FINISHED'}


class C94CORRECT_OT_Inspect(bpy.types.Operator):
    bl_idname = 'c94_workflow.corrective_inspect'
    bl_label = '检查目标修改范围'

    def execute(self, context):
        from . import corrective_authoring
        options = getattr(context.scene, OPTIONS)
        try:
            _associated_source(options)
            if context.object and context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            report = corrective_authoring.inspect(options.target)
            if not report['raw_count_matches']:
                raise ValueError('目标顶点数量已改变；保留目标供检查，不能生成修正键')
            options.summary = (f"已改 {report['changed_vertex_count']} / "
                f"允许 {report['allowed_vertex_count']} 个顶点；"
                f"范围外 {report['outside_changed_vertex_count']} 个")
            context.scene['c94_corrective_last_inspection_v1'] = json.dumps(report, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, options.summary)
        return {'FINISHED'}


class C94CORRECT_OT_Commit(bpy.types.Operator):
    bl_idname = 'c94_workflow.corrective_commit'
    bl_label = '生成手动修正形态键'
    bl_description = '验证实际重放后生成局部形态键，当前值为1；不会添加自动姿态驱动'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import corrective_authoring
        options = getattr(context.scene, OPTIONS)
        try:
            _associated_source(options)
            if context.object and context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            report = corrective_authoring.commit(options.target, key_name=options.key_name)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        context.scene['c94_corrective_last_commit_v1'] = json.dumps(report, ensure_ascii=False, allow_nan=False)
        options.summary = '修正键已创建：手动值1，尚未设置姿态驱动'
        try:
            _restore_display(context, options)
        except Exception as exc:
            self.report({'WARNING'}, '修正键已生成；显示恢复需手动处理：' + str(exc))
        self.report({'INFO'}, options.summary)
        return {'FINISHED'}


class C94CORRECT_OT_Display(bpy.types.Operator):
    bl_idname = 'c94_workflow.corrective_display'
    bl_label = '切换修正目标对照'
    target_visible: BoolProperty(default=True)

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        if not options.source or not options.target:
            self.report({'ERROR'}, '先创建或选择本次目标')
            return {'CANCELLED'}
        try:
            _associated_source(options)
            if self.target_visible:
                _activate(context, options.target)
                options.source.hide_set(True)
                options.editing_display = True
            else:
                _restore_display(context, options)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class C94CORRECT_OT_Cancel(bpy.types.Operator):
    bl_idname = 'c94_workflow.corrective_cancel'
    bl_label = '删除目标副本'
    bl_description = '只删除本工具拥有的编辑目标；已生成的形态键保留'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import corrective_authoring
        options = getattr(context.scene, OPTIONS)
        try:
            _associated_source(options)
            if context.object and context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            # Only restore after the core has accepted and removed its owned target.
            corrective_authoring.cancel(options.target)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        options.target = None
        try:
            _restore_display(context, options)
        except Exception as exc:
            self.report({'WARNING'}, '目标已删除；显示恢复需手动处理：' + str(exc))
        options.summary = '目标副本已删除；生产网格及已有形态键保留'
        return {'FINISHED'}


class C94CORRECT_PT_Authoring(bpy.types.Panel):
    bl_idname = 'C94CORRECT_PT_authoring'
    bl_label = '局部姿态修正作者'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        options = getattr(context.scene, OPTIONS)
        layout = self.layout
        row = layout.row(align=True)
        row.enabled = not options.target
        row.prop(options, 'source')
        row.operator('c94_workflow.corrective_use_source', text='', icon='EYEDROPPER')
        setup = layout.column()
        setup.enabled = not options.target
        setup.prop(options, 'scope_method')
        if options.scope_method == 'GROUP' and options.source:
            setup.prop_search(options, 'group_name', options.source, 'vertex_groups')
        setup.prop(options, 'target_name')
        setup.prop(options, 'max_rest_displacement_meters')
        setup.operator('c94_workflow.corrective_begin', icon='DUPLICATE')
        layout.label(text='隐藏顶点不计入；编辑目标时保持骨架姿态', icon='INFO')
        if options.target:
            layout.label(text='目标：' + options.target.name, icon='MESH_DATA')
            row = layout.row(align=True)
            row.operator('c94_workflow.corrective_display', text='看目标').target_visible = True
            row.operator('c94_workflow.corrective_display', text='看生产网格').target_visible = False
            layout.operator('c94_workflow.corrective_inspect', icon='CHECKMARK')
            layout.prop(options, 'key_name')
            layout.operator('c94_workflow.corrective_commit', icon='SHAPEKEY_DATA')
            layout.operator('c94_workflow.corrective_cancel', icon='X')
        if options.summary:
            layout.label(text=options.summary)
        layout.label(text='当前支持普通线性蒙皮、源网格无已有形态键')
        layout.label(text='生成手动形态键后，仍需配置姿态驱动并看实际效果')


_CLASSES = (C94CORRECT_Options, C94CORRECT_OT_UseSource, C94CORRECT_OT_Begin,
            C94CORRECT_OT_Inspect, C94CORRECT_OT_Commit, C94CORRECT_OT_Display,
            C94CORRECT_OT_Cancel, C94CORRECT_PT_Authoring)


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
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94CORRECT_Options))
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
            and prop.fixed_type == C94CORRECT_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
