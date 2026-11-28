# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit weight-policy preparation, range preview and independent candidates."""
import json
import math
import re

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import skinning_plan_ui as selection_ui
from .skinning import topology_fingerprint as _topology_signature


OPTIONS = 'c94_skinning_policy_options_v1'
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
            options.scope_count = 0
            options.summary = '参数已修改，请重新准备计划'
        except (ReferenceError, AttributeError):
            pass


def _mesh(_self, obj):
    return obj.type == 'MESH'


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _configuration(options):
    return (options.method, options.target, options.rig, options.donor, options.rigid_bone,
            options.max_distance_meters, options.candidate_name, options.editable_bones,
            options.alpha, options.use_transition_group, options.transition_group,
            options.captured_target, options.captured_mesh, options.captured_topology,
            options.captured_indices, options.captured_count, options.use_donor_region, options.donor_region_group)


def _captured_ids(options):
    target = options.target
    if (options.captured_target != target or target is None
            or options.captured_mesh != target.data
            or options.captured_topology != _topology_signature(target.data)):
        raise ValueError('目标或RAW拓扑已改变，请重新捕获可见选点')
    try:
        ids = json.loads(options.captured_indices)
    except (TypeError, ValueError) as exc:
        raise ValueError('选点记录无效，请重新捕获') from exc
    if (not isinstance(ids, list) or not ids or len(ids) != options.captured_count
            or any(type(i) is not int or not 0 <= i < len(target.data.vertices) for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('选点范围为空、重复或过时，请重新捕获')
    return ids


def _group_scope(target, name):
    group = target.vertex_groups.get(name)
    if group is None:
        raise ValueError('目标没有指定的逐点过渡顶点组')
    ids, values = [], []
    for vertex in target.data.vertices:
        weight = next((entry.weight for entry in vertex.groups if entry.group == group.index), 0.)
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError('过渡组值必须为有限0–1数值，原数据保持')
        if weight > 0:
            ids.append(vertex.index)
            values.append(weight)
    if not ids:
        raise ValueError('过渡组没有正值点；本方法明确使用该组全部正值点作为写域')
    return ids, values


def _current_plan(context):
    if _SESSION is None or _SESSION['scene'] != context.scene:
        raise ValueError('没有本场景准备的权重策略计划，请先准备')
    if _SESSION['configuration'] != _configuration(_options(context)):
        _changed(None, context)
        raise ValueError('计划参数已改变，请重新准备')
    if context.mode != 'OBJECT':
        raise ValueError('先结束编辑、姿态或雕刻模式，再预览或创建候选')
    return _SESSION


def _select(context, obj):
    for selected in context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _preview_name(base):
    name = base + '__写域预览'
    index = 2
    while name in bpy.data.objects:
        name = base + '__写域预览_' + str(index)
        index += 1
    return name


@persistent
def _load_pre(_unused):
    _discard()


class C94POLICY_Options(bpy.types.PropertyGroup):
    method: EnumProperty(name='权重策略', items=(
        ('RIGID', '纯刚性跟随', '整目标RAW顶点明确绑定到一个最终形变骨'),
        ('TRANSFER', '服装／部件转移', '读取同一角色实际已应用的供体RAW骨权重场'),
        ('LOCAL', '已有权重局部维护', '只重分配明确可写骨组的原权重质量，保护锁组及其它值')),
        default='RIGID', update=_changed)
    target: PointerProperty(name='目标网格', type=bpy.types.Object, poll=_mesh, update=_changed)
    rig: PointerProperty(name='角色骨架', type=bpy.types.Object, poll=_armature, update=_changed)
    donor: PointerProperty(name='已应用权重供体', type=bpy.types.Object, poll=_mesh, update=_changed)
    use_donor_region: BoolProperty(name='限制供体到指定组的正值点', default=False, update=_changed,
        description='只使用该组全部正值点内的完整RAW三角；未启用时明确使用供体全部RAW三角')
    donor_region_group: StringProperty(name='供体范围组', update=_changed)
    rigid_bone: StringProperty(name='最终形变骨', update=_changed)
    max_distance_meters: FloatProperty(name='最大转移距离（米）', default=.01,
                                      min=.000001, precision=6, update=_changed)
    candidate_name: StringProperty(name='独立候选名称', default='权重策略_独立候选', update=_changed)
    editable_bones: StringProperty(name='可写骨名（分号或换行分隔）',
        description='填写最终形变骨名，用分号或换行分隔；已有锁定骨组自动保护，不会写入', update=_changed)
    alpha: FloatProperty(name='选区统一过渡系数', default=1., min=0., max=1., update=_changed)
    use_transition_group: BoolProperty(name='改用顶点组全部正值点作为明确写域', default=False,
        description='不叠加已捕获选区；该组全部正值点定义写域，组值直接作为逐点过渡系数', update=_changed)
    transition_group: StringProperty(name='明确写域及逐点过渡组', update=_changed)
    captured_target: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    captured_mesh: PointerProperty(type=bpy.types.Mesh, options={'HIDDEN'})
    captured_topology: StringProperty(options={'HIDDEN'})
    captured_indices: StringProperty(default='[]', options={'HIDDEN'})
    captured_count: IntProperty(default=0, options={'HIDDEN'})
    scope_count: IntProperty(default=0, options={'HIDDEN'})
    report_json: StringProperty(options={'HIDDEN'})
    summary: StringProperty(default='选择策略和对象，明确范围后准备计划', options={'HIDDEN'})


class C94POLICY_OT_Capture(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_policy_capture'
    bl_label = '捕获目标可见选点'
    bl_description = '记录可见选点的RAW索引；移动几何不改索引，准备计划时冻结当前位置，拓扑改变须重捕'
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            options = _options(context)
            ids = selection_ui._visible_selected_ids(context, options.target)
            topology = _topology_signature(options.target.data)
            _changed(None, context)
            options.captured_target = options.target
            options.captured_mesh = options.target.data
            options.captured_topology = topology
            options.captured_indices = json.dumps(ids)
            options.captured_count = len(ids)
            options.summary = f'已按RAW索引捕获{len(ids)}点；准备时使用当前几何并核对拓扑'
            return {'FINISHED'}
        except (ValueError, RuntimeError, AttributeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94POLICY_OT_Plan(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_policy_plan'
    bl_label = '权重策略计划'
    action: EnumProperty(items=(('PREPARE', '准备并检查计划', ''),
                               ('CANCEL', '放弃计划缓存', '保留源、预览及已创建候选')))
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _SESSION
        options = _options(context)
        if self.action == 'CANCEL':
            _discard()
            options.report_json = ''
            options.scope_count = 0
            options.summary = '已放弃计划缓存；源、预览及已创建候选均保留'
            return {'FINISHED'}
        _discard()
        options.report_json = ''
        options.scope_count = 0
        try:
            from . import skinning_policy
            if context.mode != 'OBJECT':
                raise ValueError('准备计划需要对象模式；选点可先使用捕获按钮结束单对象Edit模式')
            target = selection_ui._local_mesh(context, options.target, '目标网格')
            if not options.candidate_name.strip():
                raise ValueError('请先填写独立候选名称')
            donor_ids = None
            if options.method != 'RIGID' and options.use_donor_region:
                if options.donor is None: raise ValueError('先指定已应用权重供体')
                donor_ids, _ = _group_scope(options.donor, options.donor_region_group)
            if options.method == 'RIGID':
                plan = skinning_policy.prepare_rigid(target, options.rig, [{
                    'bone_name': options.rigid_bone.strip(),
                    'vertex_indices': list(range(len(target.data.vertices)))}])
                scope = '整目标RAW顶点'
            elif options.method == 'TRANSFER':
                plan = skinning_policy.prepare_transfer(target, options.rig, options.donor,
                    max_distance_meters=float(options.max_distance_meters),donor_vertex_indices=donor_ids)
                scope = '整目标RAW顶点；供体实际全部可用最终骨场'
            else:
                names = [line.strip() for line in re.split(r'[;；\n]+', options.editable_bones) if line.strip()]
                if options.use_transition_group:
                    ids, alpha = _group_scope(target, options.transition_group)
                    scope = f'过渡组“{options.transition_group}”全部正值点；不叠加捕获选区'
                else:
                    ids = _captured_ids(options)
                    alpha = [float(options.alpha)] * len(ids)
                    scope = '已捕获可见选点中的正过渡系数点'
                plan = skinning_policy.prepare_transfer(target, options.rig, options.donor,
                    max_distance_meters=float(options.max_distance_meters),
                    local_vertex_indices=ids, alpha=alpha, editable_bone_names=names,donor_vertex_indices=donor_ids)
            report = dict(plan.report)
            count = int(report.get('write_vertices', report['vertices']))
            report['ui_scope'] = scope
            options.report_json = json.dumps(report, ensure_ascii=False, allow_nan=False)
            options.scope_count = count
            options.summary = f'已准备：{count}个明确写域顶点；尚未创建权重候选'
            _SESSION = {'scene': context.scene, 'plan': plan, 'configuration': _configuration(options),
                        'scope': scope, 'report': report}
            self.report({'INFO'}, options.summary)
            return {'FINISHED'}
        except (ValueError, RuntimeError, TypeError, KeyError, AttributeError, ReferenceError) as exc:
            options.summary = '准备未完成：' + str(exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94POLICY_OT_Preview(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_policy_preview'
    bl_label = '创建独立写域预览'
    bl_description = '创建并选择独立范围权重副本；不改源选点，不自动删除已有预览'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            session = _current_plan(context)
            options = _options(context)
            obj = session['plan'].preview(name=_preview_name(options.candidate_name.strip()))
            _select(context, obj)
            if context.area and context.area.type == 'VIEW_3D':
                context.space_data.shading.type = 'SOLID'
                context.space_data.shading.color_type = 'VERTEX'
            options.summary = '已创建范围预览：蓝=范围外，红=1，绿=过渡；源保持'
            return {'FINISHED'}
        except (ValueError, RuntimeError, KeyError, AttributeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94POLICY_OT_Apply(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_policy_apply'
    bl_label = '创建独立权重候选'
    bl_description = '通过共享计划的过时检查后创建独立Mesh候选；不写回源权重'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _SESSION
        try:
            session = _current_plan(context)
            options = _options(context)
            obj = session['plan'].apply(name=options.candidate_name.strip())
            _SESSION = None
            _select(context, obj)
            options.summary = '已创建独立候选：' + obj.name + '；实际变形与艺术质量待复核'
            return {'FINISHED'}
        except (ValueError, RuntimeError, KeyError, AttributeError, ReferenceError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94POLICY_PT_Policy(bpy.types.Panel):
    bl_idname = 'C94POLICY_PT_weight_policy'
    bl_label = '权重策略与局部维护'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        options = _options(context)
        layout = self.layout
        layout.prop(options, 'method')
        layout.prop(options, 'target')
        layout.prop(options, 'rig')
        layout.prop(options, 'candidate_name')
        if options.method == 'RIGID':
            if options.rig:
                layout.prop_search(options, 'rigid_bone', options.rig.data, 'bones')
            else:
                layout.prop(options, 'rigid_bone')
            layout.label(text='整目标RAW顶点 → 一个明确最终形变骨')
        else:
            layout.prop(options, 'donor')
            layout.prop(options, 'max_distance_meters')
            layout.label(text='只读实际已应用供体权重；不自动补骨或改源')
            layout.prop(options, 'use_donor_region')
            if options.use_donor_region and options.donor:
                layout.prop_search(options, 'donor_region_group', options.donor, 'vertex_groups')
            else:
                layout.label(text='供体范围：全部RAW完整三角及实际最终骨组')
        if options.method == 'LOCAL':
            box = layout.box()
            box.prop(options, 'editable_bones')
            names = [line.strip() for line in re.split(r'[;；\n]+', options.editable_bones) if line.strip()]
            for name in names:
                box.label(text=name)
            box.prop(options, 'use_transition_group')
            if options.use_transition_group:
                if options.target:
                    box.prop_search(options, 'transition_group', options.target, 'vertex_groups')
                else:
                    box.prop(options, 'transition_group')
                box.label(text='明确写域＝该组全部正值点，组值即逐点过渡')
                box.label(text='准备后显示数量；不叠加已捕获选区')
            else:
                box.operator('c94_workflow.skinning_policy_capture')
                box.label(text=f'捕获选点：{options.captured_count}；准备时核对真实拓扑')
                box.prop(options, 'alpha')
            box.label(text='锁组及未声明骨组保持；不扩展已有写域')
        layout.operator('c94_workflow.skinning_policy_plan', text='准备并检查计划').action = 'PREPARE'
        ready = _SESSION is not None and _SESSION['scene'] == context.scene
        if ready:
            box = layout.box()
            box.label(text=f'明确写域：{options.scope_count}个RAW顶点')
            box.label(text=_SESSION['scope'])
            report = _SESSION['report']
            if 'donor_triangles' in report:
                box.label(text=f'供体完整三角：{report["donor_triangles"]}；骨组：{len(report["donor_bone_names"])}')
            box.label(text='方法：' + {'explicit_rigid':'纯刚性跟随',
                'applied_raw_surface_transfer':'读取已应用RAW权重转移',
                'local_applied_field_redistribution':'局部可写组权重维护'}.get(report['method'], '权重策略'))
            if 'transition_vertices' in report:
                box.label(text=f'其中过渡点：{report["transition_vertices"]}')
                box.label(text=f'可写骨：{len(report["editable_bones"])}；保护锁组：{len(report["locked_bones_preserved"])}')
                if report.get('zero_editable_mass_vertices'):
                    box.label(text=f'原可写质量为零、保持不写：{report["zero_editable_mass_vertices"]}点')
        controls = layout.column()
        controls.enabled = ready and context.mode == 'OBJECT'
        controls.operator('c94_workflow.skinning_policy_preview')
        controls.operator('c94_workflow.skinning_policy_apply')
        layout.operator('c94_workflow.skinning_policy_plan', text='放弃计划缓存（保留预览）').action = 'CANCEL'
        layout.label(text=options.summary)
        layout.label(text='仅创建独立候选；原源和已有预览不自动删除')


_CLASSES = (C94POLICY_Options, C94POLICY_OT_Capture, C94POLICY_OT_Plan,
            C94POLICY_OT_Preview, C94POLICY_OT_Apply, C94POLICY_PT_Policy)


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
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94POLICY_Options))
        _C94_OPTIONS_OWNED = True
        if _load_pre not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(_load_pre)
    except Exception:
        unregister()
        raise


def unregister():
    _discard()
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)
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
            and prop.fixed_type == C94POLICY_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
