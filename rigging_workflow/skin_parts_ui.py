# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UI for explicit, independent parts from currently bound skin.

Captured RAW IDs are selection records only. A prepared core plan is live and
is discarded on parameter changes, save, file switching and unregister.
"""
import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty

from . import skinning_plan_ui as selection_ui
from .skinning import topology_fingerprint


OPTIONS = 'c94_skin_parts_options_v1'
_REGISTERED = []
_SESSION = None


def _options(context):
    return getattr(context.scene, OPTIONS)


def _discard():
    global _SESSION
    session, _SESSION = _SESSION, None
    if session is not None:
        session['plan'].cancel()


def _changed(_self, context):
    _discard()
    if context and getattr(context, 'scene', None):
        try:
            options = _options(context)
            options.report_json = ''
            options.summary = '参数已修改，请重新准备；已有预览保留'
        except (ReferenceError, AttributeError):
            pass


def _mesh(_self, obj):
    return obj.type == 'MESH'


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _configuration(options):
    return (options.source, options.rig, options.candidate_name,
            options.use_plane, tuple(options.point_world), tuple(options.normal_world),
            options.offset_meters, options.keep_positive,
            options.captured_source, options.captured_mesh, options.captured_topology,
            options.captured_indices, options.captured_count)


def _captured_ids(options):
    source = options.source
    if (source is None or source != options.captured_source
            or source.data != options.captured_mesh
            or topology_fingerprint(source.data) != options.captured_topology):
        raise ValueError('源对象、真实网格或RAW拓扑已改变，请重新捕获可见选点')
    try:
        ids = json.loads(options.captured_indices)
    except (ValueError, TypeError) as exc:
        raise ValueError('选点记录无效，请重新捕获') from exc
    if (not isinstance(ids, list) or not ids or len(ids) != options.captured_count
            or len(set(ids)) != len(ids)
            or any(type(i) is not int or not 0 <= i < len(source.data.vertices) for i in ids)):
        raise ValueError('选点记录为空、重复或过时，请重新捕获')
    return ids


def _current_plan(context):
    if _SESSION is None or _SESSION['scene'] != context.scene:
        raise ValueError('没有本场景的实时皮肤派生计划，请先准备')
    if _SESSION['configuration'] != _configuration(_options(context)):
        _changed(None, context)
        raise ValueError('计划参数或选点记录已改变，请重新准备')
    if context.mode != 'OBJECT':
        raise ValueError('先结束编辑、姿态或雕刻模式，再预览或创建部件')
    _SESSION['plan']._check()
    return _SESSION


def _report(options, plan):
    report = dict(plan.report)
    options.report_json = json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


def _preview_name(base):
    name, index = base + '__裁剪预览', 2
    while name in bpy.data.objects:
        name = base + '__裁剪预览_' + str(index)
        index += 1
    return name


def _end_for_file(reason):
    _discard()
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None and hasattr(scene, OPTIONS):
        options = getattr(scene, OPTIONS)
        options.report_json = ''
        options.summary = reason + '；源及已有副本保留，须重新准备'


@persistent
def _load_pre(_unused):
    _end_for_file('文件切换已结束实时派生计划')


@persistent
def _save_pre(_unused):
    _end_for_file('保存已结束实时派生计划')


class C94PART_Options(bpy.types.PropertyGroup):
    source: PointerProperty(name='已绑定皮肤源', type=bpy.types.Object, poll=_mesh, update=_changed)
    rig: PointerProperty(name='校准中立骨架', type=bpy.types.Object, poll=_armature, update=_changed)
    candidate_name: StringProperty(name='独立部件名称', default='皮肤部件_独立候选', update=_changed)
    use_plane: BoolProperty(name='使用显式平面裁剪', default=False, update=_changed)
    point_world: FloatVectorProperty(name='平面原点（世界BU）', size=3, default=(0., 0., 0.),
                                    precision=6, update=_changed)
    normal_world: FloatVectorProperty(name='平面法线（世界方向）', size=3, default=(1., 0., 0.),
                                     precision=6, update=_changed)
    offset_meters: FloatProperty(name='沿法线偏移（米）', default=0., precision=6, update=_changed)
    keep_positive: BoolProperty(name='保留法线正侧', default=True, update=_changed,
                               description='关闭时保留法线负侧；不根据骨骼或部件名称猜测方向')
    captured_source: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    captured_mesh: PointerProperty(type=bpy.types.Mesh, options={'HIDDEN'})
    captured_topology: StringProperty(options={'HIDDEN'})
    captured_indices: StringProperty(default='[]', options={'HIDDEN'})
    captured_count: IntProperty(default=0, options={'HIDDEN'})
    report_json: StringProperty(options={'HIDDEN'})
    summary: StringProperty(default='先在源网格明确选点，再捕获并准备派生计划', options={'HIDDEN'})


class C94PART_OT_Capture(bpy.types.Operator):
    bl_idname = 'c94_workflow.skin_parts_capture'
    bl_label = '捕获可见选点并结束编辑'
    bl_description = '捕获指定源的可见RAW选点；单对象Edit模式会结束并保留选点，不改其它对象'
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            options = _options(context)
            ids = selection_ui._visible_selected_ids(context, options.source)
            topology = topology_fingerprint(options.source.data)
            _changed(None, context)
            options.captured_source = options.source
            options.captured_mesh = options.source.data
            options.captured_topology = topology
            options.captured_indices = json.dumps(ids)
            options.captured_count = len(ids)
            options.summary = f'已捕获{len(ids)}个RAW选点；仅保留选区内的完整面'
            return {'FINISHED'}
        except (ValueError, RuntimeError, TypeError, AttributeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94PART_OT_Plan(bpy.types.Operator):
    bl_idname = 'c94_workflow.skin_parts_plan'
    bl_label = '皮肤派生计划'
    action: EnumProperty(items=(('PREPARE', '准备并检查派生计划', ''),
                               ('CANCEL', '取消实时计划', '保留源、已有预览及独立部件')))
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _SESSION
        options = _options(context)
        if self.action == 'CANCEL':
            _discard()
            options.report_json = ''
            options.summary = '已取消实时计划；源、已有预览及独立部件保留'
            return {'FINISHED'}
        _discard()
        options.report_json = ''
        try:
            from . import skin_parts
            if context.mode != 'OBJECT':
                raise ValueError('准备需要对象模式；先用捕获按钮明确结束单对象Edit选点')
            source = selection_ui._local_mesh(context, options.source, '已绑定皮肤源')
            name = options.candidate_name.strip()
            if not name or name in bpy.data.objects:
                raise ValueError('独立部件名称为空或已被使用，请填写新名称')
            ids = _captured_ids(options)
            plane = None
            if options.use_plane:
                plane = {'point_world': list(options.point_world), 'normal_world': list(options.normal_world),
                         'offset_meters': float(options.offset_meters), 'keep_positive': bool(options.keep_positive)}
            plan = skin_parts.prepare(source, options.rig, ids, plane=plane)
            _SESSION = {'scene': context.scene, 'configuration': _configuration(options), 'plan': plan}
            report = _report(options, plan)
            options.summary = (f'已准备{report["selected_vertices"]}个选点、'
                               f'{report["complete_faces"]}个完整源面；尚未创建部件')
            return {'FINISHED'}
        except (ValueError, RuntimeError, TypeError, KeyError, AttributeError, ReferenceError) as exc:
            _discard()
            options.summary = '准备未完成：' + str(exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94PART_OT_Preview(bpy.types.Operator):
    bl_idname = 'c94_workflow.skin_parts_preview'
    bl_label = '创建实际裁剪预览'
    bl_description = '从冻结源创建真实裁剪的独立显示提案；不改源、不删除旧预览，预览自身没有提交权'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            session = _current_plan(context)
            options = _options(context)
            obj = session['plan'].preview(name=_preview_name(options.candidate_name.strip()))
            _report(options, session['plan'])
            options.summary = '已创建裁剪预览：' + obj.name + '；创建部件仍会重新核对源'
            return {'FINISHED'}
        except (ValueError, RuntimeError, TypeError, KeyError, AttributeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94PART_OT_Apply(bpy.types.Operator):
    bl_idname = 'c94_workflow.skin_parts_apply'
    bl_label = '创建独立皮肤部件'
    bl_description = '从实时计划和冻结源重新创建独立Mesh；不消费手改预览、不替换原源或生产候选'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _SESSION
        try:
            session = _current_plan(context)
            options = _options(context)
            obj = session['plan'].apply(name=options.candidate_name.strip())
            _SESSION = None
            _report(options, session['plan'])
            options.summary = '已创建独立部件：' + obj.name + '；实际动作、外观及接缝待复核'
            return {'FINISHED'}
        except (ValueError, RuntimeError, TypeError, KeyError, AttributeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94PART_PT_Parts(bpy.types.Panel):
    bl_idname = 'C94PART_PT_skin_parts'
    bl_label = '已绑定皮肤部件派生'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        options = _options(context)
        layout = self.layout
        layout.prop(options, 'source')
        layout.prop(options, 'rig')
        layout.prop(options, 'candidate_name')
        layout.operator('c94_workflow.skin_parts_capture')
        layout.label(text=f'已捕获RAW选点：{options.captured_count}')
        layout.label(text='仅保留选区内完整面；不自动猜测部位')
        layout.prop(options, 'use_plane')
        if options.use_plane:
            box = layout.box()
            box.prop(options, 'point_world')
            box.prop(options, 'normal_world')
            box.prop(options, 'offset_meters')
            box.prop(options, 'keep_positive')
        prepare = layout.row()
        prepare.enabled = context.mode == 'OBJECT'
        prepare.operator('c94_workflow.skin_parts_plan', text='准备并检查派生计划').action = 'PREPARE'
        ready = _SESSION is not None and _SESSION['scene'] == context.scene
        try:
            report = json.loads(options.report_json) if options.report_json else None
        except (ValueError, TypeError):
            report = None
        if isinstance(report, dict):
            box = layout.box()
            if 'selected_vertices' in report and 'complete_faces' in report:
                box.label(text=f'明确选点：{report["selected_vertices"]}；完整源面：{report["complete_faces"]}')
            if all(key in report for key in ('retained_source_vertices', 'new_interpolated_vertices', 'total_vertices', 'faces')):
                box.label(text=f'原点保留：{report["retained_source_vertices"]}；新插值点：{report["new_interpolated_vertices"]}')
                box.label(text=f'最终点：{report["total_vertices"]}；面：{report["faces"]}')
            box.label(text='新交点为边插值；不是原始顶点exact')
        controls = layout.column()
        controls.enabled = ready and context.mode == 'OBJECT'
        controls.operator('c94_workflow.skin_parts_preview')
        controls.operator('c94_workflow.skin_parts_apply')
        layout.operator('c94_workflow.skin_parts_plan', text='取消实时计划（保留预览）').action = 'CANCEL'
        layout.label(text=options.summary)
        layout.label(text='预览为独立显示提案，不修改源或自动替换生产')
        layout.label(text='保存、切换文件后须重新准备；既有副本保留')


_CLASSES = (C94PART_Options, C94PART_OT_Capture, C94PART_OT_Plan,
            C94PART_OT_Preview, C94PART_OT_Apply, C94PART_PT_Parts)


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
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94PART_Options))
        _C94_OPTIONS_OWNED = True
        for handlers, callback in ((bpy.app.handlers.load_pre, _load_pre), (bpy.app.handlers.save_pre, _save_pre)):
            if callback not in handlers:
                handlers.append(callback)
    except Exception:
        unregister()
        raise


def unregister():
    _discard()
    for handlers, callback in ((bpy.app.handlers.load_pre, _load_pre), (bpy.app.handlers.save_pre, _save_pre)):
        if callback in handlers:
            handlers.remove(callback)
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
            and prop.fixed_type == C94PART_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
