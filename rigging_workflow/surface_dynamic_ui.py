# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit hair/skirt inputs and manual operators for the shared surface core.

Draw reads native properties and the last UI summary only. Capture, validation,
source hashing and physics are performed exclusively by explicit operators.
"""
import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)

OPTIONS = 'c94_surface_dynamic_options_v1'
_REGISTERED = []
_PROPERTY_OWNED = False
_CLEAR_CLIPS_OWNED = False
_SYNCING = False
_CLIPS = {}
_COMMON = ('quality', 'mass', 'bending_stiffness', 'collision_distance')


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _mesh(_self, obj):
    return obj.type == 'MESH'


def _options(context):
    return getattr(context.scene, OPTIONS)


def _selection_changed(options, _context):
    options.loaded_rig = None
    options.loaded_group = ''
    options.enabled_property = ''
    options.mix_property = ''
    options.parameters_dirty = True
    options.summary = '输入已改变；请安装或读取明确组'
    options.clip_summary = ''


def _kind_changed(options, context):
    _selection_changed(options, context)
    from . import cloth_backend
    _set_settings(options, cloth_backend.defaults(options.kind))
    options.parameters_dirty = True


def _json_changed(options, _context):
    global _SYNCING
    if _SYNCING:
        return
    options.parameters_dirty = True
    try:
        from . import cloth_backend
        values = json.loads(options.settings_json)
        values = cloth_backend.normalize_settings(options.kind, values)
    except (TypeError, ValueError):
        options.json_valid = False
        return
    _SYNCING = True
    try:
        for key in _COMMON:
            setattr(options, key, values[key])
        options.json_valid = True
    finally:
        _SYNCING = False


def _common_changed(options, _context):
    global _SYNCING
    if _SYNCING:
        return
    options.parameters_dirty = True
    try:
        values = json.loads(options.settings_json)
        if not isinstance(values, dict):
            raise ValueError('参数 JSON 须为对象')
    except (TypeError, ValueError):
        options.json_valid = False
        return
    for key in _COMMON:
        values[key] = getattr(options, key)
    try:
        from . import cloth_backend
        values = cloth_backend.normalize_settings(options.kind, values)
    except (TypeError, ValueError):
        options.json_valid = False
        return
    _SYNCING = True
    try:
        options.settings_json = json.dumps(values, ensure_ascii=False, sort_keys=True)
        options.json_valid = True
    finally:
        _SYNCING = False


def _set_settings(options, values):
    global _SYNCING
    from . import cloth_backend
    values = cloth_backend.normalize_settings(options.kind, values)
    _SYNCING = True
    try:
        options.settings_json = json.dumps(values, ensure_ascii=False, sort_keys=True)
        for key in _COMMON:
            setattr(options, key, values[key])
        options.json_valid = True
        options.parameters_dirty = False
    finally:
        _SYNCING = False


def _settings(options):
    from . import cloth_backend
    try:
        values = json.loads(options.settings_json)
    except (TypeError, ValueError) as exc:
        raise ValueError('完整参数 JSON 无效；请修正或载入技术预设') from exc
    return cloth_backend.normalize_settings(options.kind, values)


def _rig(context):
    rig = _options(context).rig
    if (rig is None or rig.type != 'ARMATURE' or
            context.scene.objects.get(rig.name) != rig or
            context.view_layer.objects.get(rig.name) != rig):
        raise ValueError('选择当前场景和视图层中的明确角色骨架')
    return rig


def _group(options):
    value = options.group_id.strip()
    if not value:
        raise ValueError('填写明确动态组标识')
    return value


def _read(context, core, rig, message='已读取当前组'):
    options = _options(context)
    state = core.inspect(rig)
    row = state.get('groups', {}).get(_group(options))
    if not isinstance(row, dict):
        raise ValueError('当前动态组尚未安装')
    if row.get('kind') not in {'HAIR', 'SKIRT'} or not isinstance(row.get('settings'), dict):
        raise ValueError('动态组种类或参数记录无效')
    options.kind = row['kind']
    _set_settings(options, row['settings'])
    options.enabled_property = row['enabled']
    options.mix_property = row['mix']
    options.loaded_rig = rig
    options.loaded_group = _group(options)
    options.summary = message + '：' + options.loaded_group
    return row


def _applied(options, rig):
    if options.loaded_rig != rig or options.loaded_group != _group(options):
        raise ValueError('先读取当前角色与明确动态组参数')
    if options.parameters_dirty:
        raise ValueError('参数尚未应用；请应用后重新捕获')


def _install_inputs(context, options):
    source = options.source
    if (source is None or source.type != 'MESH' or
            context.scene.objects.get(source.name) != source or
            context.view_layer.objects.get(source.name) != source):
        raise ValueError('选择当前场景和视图层内的源 Mesh')
    name = options.pin_group.strip()
    group = source.vertex_groups.get(name)
    if group is None:
        raise ValueError('明确指定源 Mesh 上的固定点顶点组，不自动猜测')
    pins = [vertex.index for vertex in source.data.vertices
            if any(member.group == group.index and member.weight > 0.0
                   for member in vertex.groups)]
    if not pins:
        raise ValueError('指定固定点组没有正权顶点')
    collection = options.collider_collection
    if collection is None:
        raise ValueError('明确选择碰撞集合')
    colliders = [obj for obj in collection.all_objects if obj.type == 'MESH']
    if not colliders:
        raise ValueError('明确碰撞集合内没有 Mesh')
    if source in colliders:
        raise ValueError('源 Mesh 不能同时作为本组碰撞体')
    return source, pins, colliders


class C94SURFACE_Options(bpy.types.PropertyGroup):
    rig: PointerProperty(name='角色骨架', type=bpy.types.Object, poll=_armature,
                         update=_selection_changed)
    source: PointerProperty(name='源发片/裙片', type=bpy.types.Object, poll=_mesh,
                            update=_selection_changed)
    kind: EnumProperty(name='动态类型', items=(
        ('HAIR', '发束', '高抗弯原生 Cloth 发片'),
        ('SKIRT', '裙摆', '较柔原生 Cloth 膜')),
        default='HAIR', update=_kind_changed)
    group_id: StringProperty(name='动态组标识', default='发束左侧', update=_selection_changed)
    pin_group: StringProperty(name='固定点顶点组', update=_selection_changed)
    collider_collection: PointerProperty(name='明确碰撞集合', type=bpy.types.Collection,
                                         update=_selection_changed)
    settings_json: StringProperty(name='完整求解参数 JSON', default='{}', update=_json_changed,
        description='完整参数对象；常用表单与有效 JSON 同步。应用后重新捕获')
    quality: IntProperty(name='求解质量', default=8, min=1, max=64, update=_common_changed)
    mass: FloatProperty(name='质量', default=.08, min=.0001, max=100., update=_common_changed)
    bending_stiffness: FloatProperty(name='抗弯刚度', default=5., min=0., max=10000., update=_common_changed)
    collision_distance: FloatProperty(name='碰撞间距（米）', default=.003,
                                      min=.001001, max=1., precision=5, update=_common_changed)
    preset: EnumProperty(name='技术预设', items=(
        ('DEFAULT', '默认', '本插件技术默认，未做材料标定'),
        ('SOFT', '柔软', '默认刚度的一半'),
        ('FIRM', '偏硬', '默认刚度的两倍')), default='DEFAULT')
    start: IntProperty(name='开始帧', default=1)
    end: IntProperty(name='结束帧', default=48)
    preroll: IntProperty(name='预卷帧数', default=12, min=0)
    max_samples: IntProperty(name='最多采样数', default=300, min=1, max=4096)
    action_name: StringProperty(name='新 Action 名称', default='发裙动态_烘焙')
    loaded_rig: PointerProperty(type=bpy.types.Object, options={'HIDDEN', 'SKIP_SAVE'})
    loaded_group: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    enabled_property: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    mix_property: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    parameters_dirty: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    json_valid: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    summary: StringProperty(default='明确选择角色、源 Mesh、固定点组与碰撞集合', options={'SKIP_SAVE'})
    clip_summary: StringProperty(options={'SKIP_SAVE'})


class C94SURFACE_OT_Action(bpy.types.Operator):
    bl_idname = 'c94_workflow.surface_dynamic_action'
    bl_label = '发裙动态操作'
    bl_description = '显式输入并调用共享原生 Cloth 核心，保留基础动作'
    action: EnumProperty(items=(
        ('INSTALL', '安装当前动态组', ''), ('READ', '读取/刷新组参数', ''),
        ('PRESET', '载入技术预设', ''), ('CONFIGURE', '应用求解参数', ''),
        ('CAPTURE', '捕获基础并求解', ''), ('PREVIEW', '预览已捕获片段', ''),
        ('BAKE', '烘焙新原生 Action', ''), ('FREEZE', '冻结当前增量', ''),
        ('RESET', '回到基础/重置', ''), ('STOP', '停止 临时预览', '')))

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'POSE'}

    def execute(self, context):
        try:
            from . import cloth_backend, surface_dynamic as core
            options = _options(context)
            rig = _rig(context)
            group = _group(options)
            if self.action == 'INSTALL':
                source, pins, colliders = _install_inputs(context, options)
                core.install(rig, source, kind=options.kind, group_id=group,
                             pins=pins, colliders=colliders, settings=_settings(options))
                _read(context, core, rig, '动态组已安装并读取')
            elif self.action == 'READ':
                _read(context, core, rig)
            elif self.action == 'PRESET':
                values = cloth_backend.defaults(options.kind)
                scale = {'DEFAULT': 1., 'SOFT': .5, 'FIRM': 2.}[options.preset]
                for name in ('tension_stiffness', 'compression_stiffness',
                             'shear_stiffness', 'bending_stiffness'):
                    values[name] *= scale
                _set_settings(options, values)
                options.parameters_dirty = True
                options.summary = '技术预设已载入表单；应用后重新捕获'
            elif self.action == 'CONFIGURE':
                if options.loaded_rig != rig or options.loaded_group != group:
                    raise ValueError('先读取当前角色和明确动态组')
                core.configure(rig, group, _settings(options))
                _CLIPS.pop((rig.session_uid, group), None)
                _read(context, core, rig, '参数已应用；请重新捕获')
                options.clip_summary = '旧 临时预览片段不能代表新参数'
            elif self.action == 'CAPTURE':
                _applied(options, rig)
                clip = core.capture(rig, group, start=options.start, end=options.end,
                                    preroll=options.preroll, max_samples=options.max_samples)
                _CLIPS[(rig.session_uid, group)] = clip
                options.clip_summary = '%s：%d–%d 帧，预卷 %d 帧' % (
                    group, options.start, options.end, options.preroll)
                options.summary = '基础已完整捕获并求解；点击预览或烘焙'
            elif self.action in {'PREVIEW', 'BAKE'}:
                _applied(options, rig)
                clip = _CLIPS.get((rig.session_uid, group)) or core.session(rig, group)
                if clip is None:
                    raise ValueError('没有当前 临时预览片段；先捕获基础并求解')
                if self.action == 'PREVIEW':
                    clip.preview()
                    options.summary = '正在回放已捕获 临时预览片段；改基础或参数后重新捕获'
                else:
                    name = options.action_name.strip()
                    if not name:
                        raise ValueError('填写新 Action 名称')
                    clip.bake(name=name)
                    options.summary = '新原生 Action 已烘焙，基础动作保留，可直接手改输出形态键与时钟曲线'
            elif self.action == 'FREEZE':
                core.freeze(rig, group)
                options.summary = '当前组动态增量已冻结'
            elif self.action == 'RESET':
                core.reset(rig, group)
                _CLIPS.pop((rig.session_uid, group), None)
                options.summary = '当前组已回到基础；重新捕获后继续'
                options.clip_summary = '组已重置，请重新捕获'
            elif self.action == 'STOP':
                core.stop_preview(rig, group)
                options.summary = '已停止当前组 临时预览并恢复源对象显示'
            else:
                raise ValueError('未知发裙动态操作')
            self.report({'INFO'}, options.summary)
            return {'FINISHED'}
        except Exception as exc:
            try:
                _options(context).summary = '操作未完成：' + str(exc)
            except (AttributeError, ReferenceError):
                pass
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _button(layout, label, action):
    layout.operator('c94_workflow.surface_dynamic_action', text=label).action = action


class C94SURFACE_PT_Dynamic(bpy.types.Panel):
    bl_label = '发束与裙摆动态'
    bl_idname = 'C94SURFACE_PT_dynamic'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_order = 9

    def draw(self, context):
        options = _options(context)
        layout = self.layout
        workspace = getattr(context.scene, 'c94_workspace_options_v1', None)
        advanced = workspace is None or workspace.level != 'DAILY'
        layout.prop(options, 'rig')
        layout.prop(options, 'source')
        layout.prop(options, 'kind')
        layout.prop(options, 'group_id')
        if options.source and options.source.type == 'MESH':
            layout.prop_search(options, 'pin_group', options.source, 'vertex_groups')
        else:
            layout.prop(options, 'pin_group')
        layout.prop(options, 'collider_collection')
        row = layout.row(align=True)
        _button(row, '安装明确动态组', 'INSTALL')
        _button(row, '读取/刷新', 'READ')
        rig = options.rig
        settings = rig.pose.bones.get('骨架设置') if rig and rig.type == 'ARMATURE' else None
        if (settings is not None and options.loaded_rig == rig and
                options.loaded_group == options.group_id.strip()):
            native = layout.box()
            native.label(text='原生组开关与混合')
            if options.enabled_property in settings:
                native.prop(settings, '[' + json.dumps(options.enabled_property) + ']', text='启用')
            if options.mix_property in settings:
                native.prop(settings, '[' + json.dumps(options.mix_property) + ']', text='混合', slider=True)
        form = layout.box()
        form.label(text='选择预设并应用到当前组')
        form.prop(options, 'preset')
        _button(form, '载入技术预设', 'PRESET')
        if advanced:
            for key in _COMMON:
                form.prop(options, key)
            form.prop(options, 'settings_json')
        if not options.json_valid:
            form.label(text='参数 JSON 无效；修正或重新载入预设', icon='ERROR')
        elif options.parameters_dirty:
            form.label(text='表单尚未应用', icon='INFO')
        _button(form, '应用当前组参数', 'CONFIGURE')
        sampling = layout.box()
        row = sampling.row(align=True)
        row.prop(options, 'start')
        row.prop(options, 'end')
        if advanced:
            sampling.prop(options, 'preroll')
            sampling.prop(options, 'max_samples')
        _button(sampling, '捕获基础并求解', 'CAPTURE')
        _button(sampling, '预览已捕获片段', 'PREVIEW')
        output = layout.box()
        output.prop(options, 'action_name')
        _button(output, '烘焙新原生 Action', 'BAKE')
        if advanced:
            _button(layout, '冻结当前组增量', 'FREEZE')
            _button(layout, '回到基础/重置', 'RESET')
        _button(layout, '停止临时预览', 'STOP')
        box = layout.box()
        box.label(text='上次操作摘要')
        box.label(text=options.summary)
        if options.clip_summary:
            box.label(text=options.clip_summary)
        layout.label(text='固定片段回放；参数变化后重新捕获')
        if advanced:
            layout.label(text='技术预设未做材料或美术标定')


_CLASSES = (C94SURFACE_Options, C94SURFACE_OT_Action, C94SURFACE_PT_Dynamic)


@persistent
def _clear_clips(*_args):
    _CLIPS.clear()


def register():
    global _PROPERTY_OWNED, _CLEAR_CLIPS_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('发裙动态选项已由其它实例注册')
    if _clear_clips in bpy.app.handlers.load_pre:
        raise RuntimeError('发裙动态UI清理handler已由其它实例注册')
    for base, identifier in (
            (bpy.types.PropertyGroup, C94SURFACE_Options.__name__),
            (bpy.types.Operator, 'C94_WORKFLOW_OT_surface_dynamic_action'),
            (bpy.types.Panel, C94SURFACE_PT_Dynamic.bl_idname)):
        if base.bl_rna_get_subclass_py(identifier, None) is not None:
            raise RuntimeError('发裙动态同名类型已由其它实例注册：' + identifier)
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94SURFACE_Options))
        _PROPERTY_OWNED = True
        bpy.app.handlers.load_pre.append(_clear_clips)
        _CLEAR_CLIPS_OWNED = True
    except Exception:
        unregister()
        raise


def unregister():
    global _PROPERTY_OWNED, _CLEAR_CLIPS_OWNED
    if _PROPERTY_OWNED and hasattr(bpy.types.Scene, OPTIONS):
        prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
        if prop and prop.fixed_type == C94SURFACE_Options.bl_rna:
            delattr(bpy.types.Scene, OPTIONS)
    _PROPERTY_OWNED = False
    if _CLEAR_CLIPS_OWNED and _clear_clips in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_clear_clips)
    _CLEAR_CLIPS_OWNED = False
    _CLIPS.clear()
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
