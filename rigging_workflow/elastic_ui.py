# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual chest/glute elastic workflow; the shared core owns RAM clips.

Draw reads the UI's last summary and native rig properties only. It never
captures, validates a source signature, inspects a rig or evaluates a session.
"""
import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty,
                       StringProperty)

OPTIONS = 'c94_elastic_options_v1'
_REGISTERED = []
_PROPERTY_OWNED = False
_GROUPS = {'BREAST': ('胸部', '胸部弹性启用', '胸部弹性混合'),
           'GLUTE': ('臀部', '臀部弹性启用', '臀部弹性混合')}
# These are this plugin's presets, not conversions of Wiggle's stiffness units.
_PRESETS = {'SOFT': (2.0, .25, 18.0, .65),
            'DEFAULT': (3.0, .4, 12.0, .6),
            'FIRM': (5.0, .7, 8.0, .45)}


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _options(context):
    return getattr(context.scene, OPTIONS)


def _selection_changed(options, _context):
    options.loaded_rig = None
    options.loaded_group = ''
    options.parameters_dirty = True
    options.summary = '选择已改变；先安装或读取当前组参数'
    options.clip_summary = ''


def _parameters_changed(options, _context):
    options.parameters_dirty = True


def _rig(context):
    rig = _options(context).rig
    if (rig is None or rig.type != 'ARMATURE'
            or context.scene.objects.get(rig.name) != rig
            or context.view_layer.objects.get(rig.name) != rig):
        raise ValueError('选择当前场景与视图层内的明确角色骨架')
    return rig


def _set_parameters(options, params):
    # Property callbacks only mark the form dirty; core writes happen on Apply.
    options.frequency_hz = params['frequency_hz']
    options.damping_ratio = params['damping_ratio']
    options.gravity = params['gravity']
    options.max_angle_degrees = params['max_angle_degrees']
    options.strength = params['strength']
    options.parameters_dirty = False


def _read_current(context, core, rig, *, message='已读取当前组参数'):
    state = core.inspect(rig)
    options = _options(context)
    group = state.get('groups', {}).get(options.group)
    if not isinstance(group, dict) or not isinstance(group.get('params'), dict):
        options.loaded_rig = None
        options.loaded_group = ''
        options.parameters_dirty = True
        raise ValueError('当前组尚未安装；先安装胸臀弹性模块')
    _set_parameters(options, group['params'])
    options.loaded_rig = rig
    options.loaded_group = options.group
    options.summary = message + '：' + _GROUPS[options.group][0]
    return state


def _applied(context, rig):
    options = _options(context)
    if options.loaded_rig != rig or options.loaded_group != options.group:
        raise ValueError('先读取当前角色与当前组的参数')
    if options.parameters_dirty:
        raise ValueError('表单参数尚未应用；请应用参数，随后重新采样基础动作')
    return options


def _capture(core, rig, options):
    return core.capture(rig, start=options.start, end=options.end,
                        preroll=options.preroll, substeps=int(options.substeps),
                        max_samples=options.max_samples)


def _capture_text(options):
    return '%d–%d帧；预卷%d帧；每帧%d子步' % (
        options.start, options.end, options.preroll, int(options.substeps))


def _output_name(result):
    if not isinstance(result, dict):
        return ''
    value = result.get('action') or result.get('action_name') or result.get('name')
    return getattr(value, 'name', value) if value is not None else ''


class C94ELASTIC_Options(bpy.types.PropertyGroup):
    rig: PointerProperty(name='角色骨架', type=bpy.types.Object,
                         poll=_armature, update=_selection_changed)
    group: EnumProperty(name='参数分组', items=(
        ('BREAST', '胸部', '胸部附属骨弹性参数'),
        ('GLUTE', '臀部', '臀部附属骨弹性参数')),
        default='BREAST', update=_selection_changed)
    frequency_hz: FloatProperty(name='回正频率（Hz）', default=3., min=.1,
                               max=30., soft_max=15., update=_parameters_changed)
    damping_ratio: FloatProperty(name='阻尼比', default=.4, min=0.,
                                max=3., soft_max=2., update=_parameters_changed)
    gravity: FloatVectorProperty(name='世界重力（米/秒²）', size=3,
                                 default=(0., 0., 0.), update=_parameters_changed)
    max_angle_degrees: FloatProperty(name='最大摆角（度）', default=12.,
                                    min=0., max=75., update=_parameters_changed)
    strength: FloatProperty(name='求解强度', default=.6, min=0., max=1.,
                            update=_parameters_changed)
    preset: EnumProperty(name='本插件预设', items=(
        ('SOFT', '柔和', '较低频率、较小阻尼的自定预设'),
        ('DEFAULT', '默认', '本插件中等回正与阻尼预设'),
        ('FIRM', '偏硬', '较高频率、阻尼和较小摆角的自定预设')),
        default='DEFAULT')
    start: IntProperty(name='开始帧', default=1)
    end: IntProperty(name='结束帧', default=72)
    preroll: IntProperty(name='预卷帧数', default=12, min=0)
    substeps: EnumProperty(name='每帧固定子步', items=(
        ('1', '1', '每帧1个固定子步'), ('2', '2', '每帧2个固定子步'),
        ('4', '4', '每帧4个固定子步'), ('8', '8', '每帧8个固定子步')), default='4')
    max_samples: IntProperty(name='最多采样数', default=10000, min=1)
    reuse_clip: BoolProperty(name='复用当前临时预览片段烘焙', default=True,
        description='按上次捕获的帧域烘焙；关闭时按当前采样表单重新捕获后烘焙')
    action_name: StringProperty(name='新Action名称', default='胸臀弹性_烘焙')
    loaded_rig: PointerProperty(type=bpy.types.Object, options={'HIDDEN', 'SKIP_SAVE'})
    loaded_group: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    parameters_dirty: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    summary: StringProperty(default='先选择角色，安装或读取当前组参数', options={'SKIP_SAVE'})
    clip_summary: StringProperty(options={'SKIP_SAVE'})


class C94ELASTIC_OT_Action(bpy.types.Operator):
    bl_idname = 'c94_workflow.elastic_action'
    bl_label = '胸臀弹性操作'
    bl_description = '调用共享弹性核心；基础动作保留，输出只写受管附属骨'
    action: EnumProperty(items=(
        ('INSTALL', '安装胸臀弹性', ''),
        ('READ', '读取当前组参数', ''),
        ('PRESET', '载入本插件预设', ''),
        ('CONFIGURE', '应用当前组参数', ''),
        ('CAPTURE', '采样基础动作并计算预览片段', ''),
        ('BAKE', '烘焙新原生Action', ''),
        ('FREEZE', '冻结当前弹性增量', ''),
        ('STOP', '停止临时预览（恢复原轨道）', ''),
        ('RESET', '重新初始化弹性', '')))

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'POSE'}

    def execute(self, context):
        try:
            from . import elastic
            options = _options(context)
            rig = _rig(context)
            if self.action == 'INSTALL':
                installed = elastic.install(rig)
                if options.group not in installed['groups']:
                    options.group = next(iter(installed['groups']))
                _read_current(context, elastic, rig, message='弹性模块已安装并读取参数')
            elif self.action == 'READ':
                _read_current(context, elastic, rig)
            elif self.action == 'PRESET':
                if options.loaded_rig != rig or options.loaded_group != options.group:
                    raise ValueError('先读取当前组参数，再载入预设')
                frequency, damping, angle, strength = _PRESETS[options.preset]
                options.frequency_hz = frequency
                options.damping_ratio = damping
                options.max_angle_degrees = angle
                options.strength = strength
                options.gravity = (0., 0., 0.)
                options.summary = '预设已载入表单；应用后重新采样基础动作'
            elif self.action == 'CONFIGURE':
                if options.loaded_rig != rig or options.loaded_group != options.group:
                    raise ValueError('先读取当前角色与当前组的参数')
                elastic.configure(rig, options.group,
                    frequency_hz=options.frequency_hz, damping_ratio=options.damping_ratio,
                    gravity=tuple(options.gravity), max_angle_degrees=options.max_angle_degrees,
                    strength=options.strength)
                _read_current(context, elastic, rig, message='参数已应用；须重新采样基础动作')
                options.clip_summary = '原片段不可代表新参数；请重新捕获'
            elif self.action == 'CAPTURE':
                _applied(context, rig)
                clip = _capture(elastic, rig, options)
                clip.preview()
                options.clip_summary = '本次临时预览片段：' + _capture_text(options)
                options.summary = '已完整采样并计算；时间轴回放临时预览片段，非逐帧实时求解'
            elif self.action == 'BAKE':
                _applied(context, rig)
                name = options.action_name.strip()
                if not name:
                    raise ValueError('填写新原生Action名称')
                if options.reuse_clip:
                    clip = elastic.session(rig)
                    if clip is None:
                        raise ValueError('没有当前临时预览片段；先捕获，或关闭“复用”以重新采样烘焙')
                else:
                    clip = _capture(elastic, rig, options)
                    options.clip_summary = '本次临时预览片段：' + _capture_text(options)
                result = clip.bake(name=name)
                label = _output_name(result)
                options.summary = '已烘焙新原生Action；基础动作保留，可停预览后手改'
                if label:
                    options.summary += '：' + str(label)
            elif self.action == 'FREEZE':
                elastic.freeze(rig)
                options.summary = '已冻结当前相对基础动作的弹性增量'
            elif self.action == 'STOP':
                elastic.stop_preview(rig)
                options.summary = '已停止临时预览；已烘焙轨道按原启用状态播放'
            elif self.action == 'RESET':
                elastic.reset(rig)
                options.summary = '已重新初始化弹性；主控姿态保留，须重新捕获'
                options.clip_summary = '重新初始化后，请重新采样基础动作'
            else:
                raise ValueError('未知弹性操作')
            self.report({'INFO'}, options.summary)
            return {'FINISHED'}
        except Exception as exc:
            try:
                _options(context).summary = '操作未完成：' + str(exc)
            except (AttributeError, ReferenceError):
                pass
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _button(layout, text, action, *, enabled=True):
    row = layout.row()
    row.enabled = enabled
    row.operator('c94_workflow.elastic_action', text=text).action = action


class C94ELASTIC_PT_Elastic(bpy.types.Panel):
    bl_label = '胸臀弹性动态'
    bl_idname = 'C94ELASTIC_PT_elastic'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_order = 8

    def draw(self, context):
        layout = self.layout
        options = _options(context)
        workspace = getattr(context.scene, 'c94_workspace_options_v1', None)
        advanced = workspace is None or workspace.level != 'DAILY'
        layout.prop(options, 'rig')
        layout.prop(options, 'group')
        row = layout.row(align=True)
        row.operator('c94_workflow.elastic_action', text='安装弹性模块').action = 'INSTALL'
        row.operator('c94_workflow.elastic_action', text='读取/刷新').action = 'READ'
        rig = options.rig
        settings = rig.pose.bones.get('骨架设置') if rig and rig.type == 'ARMATURE' else None
        if settings is not None:
            native = layout.box()
            native.label(text='原生组开关与混合（烘焙后仍可用）')
            for _name, enabled, mix in _GROUPS.values():
                row = native.row(align=True)
                if enabled in settings:
                    row.prop(settings, '["' + enabled + '"]', text=enabled)
                if mix in settings:
                    row.prop(settings, '["' + mix + '"]', text=mix, slider=True)
        form = layout.box()
        form.label(text='选择预设并应用到当前组')
        form.prop(options, 'preset')
        _button(form, '载入本插件预设', 'PRESET')
        if advanced:
            form.prop(options, 'frequency_hz')
            form.prop(options, 'damping_ratio')
            form.prop(options, 'gravity')
            form.prop(options, 'max_angle_degrees')
            form.prop(options, 'strength')
        _button(form, '应用当前组参数', 'CONFIGURE')
        if options.parameters_dirty:
            form.label(text='表单尚未应用；捕获前先应用参数', icon='INFO')
        sampling = layout.box()
        sampling.label(text='采样基础动作，生成临时预览')
        row = sampling.row(align=True)
        row.prop(options, 'start')
        row.prop(options, 'end')
        if advanced:
            sampling.prop(options, 'preroll')
            sampling.prop(options, 'substeps')
            sampling.prop(options, 'max_samples')
        _button(sampling, '采样基础动作并计算预览片段', 'CAPTURE')
        sampling.label(text='修改基础动作或参数后须重新采样')
        output = layout.box()
        output.prop(options, 'action_name')
        output.prop(options, 'reuse_clip')
        if options.reuse_clip:
            output.label(text='复用时按上次临时预览片段帧域烘焙')
        _button(output, '烘焙新原生Action', 'BAKE')
        if advanced:
            _button(layout, '冻结当前弹性增量', 'FREEZE')
            _button(layout, '重新初始化弹性', 'RESET')
        _button(layout, '停止预览并恢复原轨道', 'STOP')
        summary = layout.box()
        summary.label(text='上次操作摘要')
        summary.label(text=options.summary)
        if options.clip_summary:
            summary.label(text=options.clip_summary)
        layout.label(text='保存前烘焙；动作或参数变化后重新采样')
        if advanced:
            layout.label(text='只作用受管胸臀附属骨；发/裙碰撞另行制作')
            layout.label(text='捕获与烘焙后仍需检查实际动态效果')


_CLASSES = (C94ELASTIC_Options, C94ELASTIC_OT_Action, C94ELASTIC_PT_Elastic)


def register():
    global _PROPERTY_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('胸臀弹性选项已由其它实例注册')
    for base, identifier in (
            (bpy.types.PropertyGroup, C94ELASTIC_Options.__name__),
            (bpy.types.Operator, 'C94_WORKFLOW_OT_elastic_action'),
            (bpy.types.Panel, C94ELASTIC_PT_Elastic.bl_idname)):
        if base.bl_rna_get_subclass_py(identifier, None) is not None:
            raise RuntimeError('胸臀弹性同名类型已由其它实例注册：' + identifier)
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94ELASTIC_Options))
        _PROPERTY_OWNED = True
    except Exception:
        unregister()
        raise


def unregister():
    global _PROPERTY_OWNED
    if _PROPERTY_OWNED and hasattr(bpy.types.Scene, OPTIONS):
        prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
        if prop and prop.fixed_type == C94ELASTIC_Options.bl_rna:
            delattr(bpy.types.Scene, OPTIONS)
    _PROPERTY_OWNED = False
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
