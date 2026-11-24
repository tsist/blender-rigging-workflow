"""Workflow navigation; existing shared cores remain the only data writers."""
import textwrap
import bpy
from bpy.props import EnumProperty, PointerProperty

OPTIONS = 'c94_workspace_options_v1'
_REGISTERED = []
_ROUTED = []
_UI_MODULES = []
_AVAILABLE_STAGES = set()
STAGES = (
    ('AUTO', '自动', '依据角色与当前会话显示可继续的工作'),
    ('PREPARE', '准备与定位', '模板、模块与完整参考骨'),
    ('BIND', '绑定与蒙皮', '新建角色、身体与部件绑定'),
    ('CONTROL', '角色控制', '集合显隐、IK/FK与基础关键帧'),
    ('FACE', '面部与表情', '日常表情及角色Key制作'),
    ('DYNAMIC', '辅助动态', '胸臀、发束与裙摆'),
    ('MOTION', '动画重定向', '来源、候选、正式使用与接触复核'),
    ('MAINTAIN', '检查与维护', '原骨架维护、作者返工与诊断'),
)
LEVELS = (('DAILY', '日常', '主要操作'), ('ADVANCED', '进阶', '完整制作参数'),
          ('DEVELOPER', '开发', '详细技术状态与审查工具'))
LEVEL = {'DAILY': 0, 'ADVANCED': 1, 'DEVELOPER': 2}
# Explicit coverage of all registered panels. Retired exchange UI stays absent.
ROUTES = {
    'modular_ui': ('PREPARE', 0, '独立人形与四足模板'),
    'ui': ('MAINTAIN', 2, '模板与依赖审查'),
    'workspace_setup': ('PREPARE', 0, '01 · 准备角色与定位图谱'),
    'positioning_ui': ('PREPARE', 0, '02 · 模板与参考骨定位'),
    'accessory_ui': ('PREPARE', 1, '03 · 发束、裙摆与附属链'),
    'inspection_ui': ('PREPARE', 1, '04 · 定位检查与精调'),
    'generation_ui': ('BIND', 0, '01 · 模板与模块配置'),
    'workflow_ui': ('BIND', 0, '02 · 新建角色与部件绑定'),
    'skinning_ui': ('BIND', 1, '03 · 单代理体积蒙皮'),
    'skinning_plan_ui': ('BIND', 1, '04 · 多区域蒙皮'),
    'skinning_policy_ui': ('BIND', 1, '05 · 权重策略'),
    'local_skinning_ui': ('BIND', 1, '06 · 局部重算与刷权重'),
    'weight_repair_ui': ('BIND', 1, '局部权重修复套组'),
    'skin_parts_ui': ('BIND', 1, '07 · 皮肤部件派生'),
    'corrective_ui': ('BIND', 1, '08 · 姿态修正'),
    'control_panel': ('CONTROL', 0, '身体与四肢控制'),
    'face_daily_ui': ('FACE', 0, '01 · 日常表情控制'),
    'face_expression_controls_ui': ('FACE', 1, '02 · 表情制作与图形板'),
    'face_modes_ui': ('FACE', 1, '03 · 骨控与Key模式'),
    'face_pose_library_ui': ('FACE', 1, '04 · 骨控姿态与混合'),
    'face_capture_ui': ('FACE', 1, '05 · 采样为角色Key'),
    'face_key_revision_ui': ('FACE', 1, '06 · 修订已有Key'),
    'face_key_mapping_ui': ('FACE', 1, '07 · 显式Key映射'),
    'elastic_ui': ('DYNAMIC', 0, '01 · 胸臀弹性'),
    'surface_dynamic_ui': ('DYNAMIC', 0, '02 · 发束与裙摆'),
    'retarget_ui': ('MOTION', 0, '01 · FBX / BVH身体与手指'),
    'retarget_mmd_ui': ('MOTION', 0, '02 · MMD身体、表情与眼控'),
    'retarget_mmd_import_ui': ('MOTION', 0, 'MMD文件导入'),
    'retarget_profile_ui': ('MOTION', 0, '03 · 通用来源映射'),
    'retarget_publish_ui': ('MOTION', 0, '04 · 正式使用与撤回'),
    'retarget_contact_detection_ui': ('MOTION', 1, '05 · 足部只读检测'),
    'retarget_contact_job_ui': ('MOTION', 1, '06 · 足部逐问题修正'),
    'retarget_contact_hints': ('MOTION', 1, '07 · 手与身体复核提示'),
    'retarget_job_ui': ('MOTION', 1, '08 · FBX / BVH后台队列'),
    'retarget_mmd_job_ui': ('MOTION', 1, '09 · MMD后台队列'),
    'retarget_general_job_ui': ('MOTION', 1, '10 · 通用映射后台队列'),
    'retarget_generic_target_ui': ('MOTION', 1, '11 · 普通FK目标'),
    'retarget_mmd_camera_ui': ('MOTION', 1, '12 · 可选MMD镜头'),
    'native_maintenance_ui': ('MAINTAIN', 0, '01 · 保留原骨架维护'),
    'author_rework_ui': ('MAINTAIN', 1, '02 · C94作者返工'),
}
HINTS = {
    'PREPARE': '先明确模板和模块，完成参考骨定位，再进入绑定。',
    'BIND': '新建独立角色，按身体与部件完成绑定；原输入保留。',
    'CONTROL': '直接调整角色控制。保姿态切换使用匹配按钮。',
    'FACE': '日常调整表情；制作、采样和Key修订位于进阶。',
    'DYNAMIC': '基础动作确定后捕获动态；动作或参数改变须重新捕获。',
    'MOTION': '选择来源并制作独立候选，预览后正式使用；可撤回。',
    'MAINTAIN': '保留原骨架做局部维护；完整C94返工位于进阶。',
}


def _armature(self, obj):
    return obj.type == 'ARMATURE'


class C94NAV_Options(bpy.types.PropertyGroup):
    stage: EnumProperty(name='制作阶段', items=STAGES, default='AUTO')
    level: EnumProperty(name='显示内容', items=LEVELS, default='DAILY')
    target: PointerProperty(name='当前角色', type=bpy.types.Object, poll=_armature,
                            description='选择目标后点击进入角色；不会替换来源或修改骨架')
    motion_source: EnumProperty(name='动作来源', items=(
        ('AUTO', '沿用当前来源', '沿用正式使用面板已有来源类型'),
        ('BODY', 'FBX / BVH', 'Mixamo、CMU及场景已有动作'),
        ('MMD', 'MMD', 'PMX/VMD与已登记MMD来源'),
        ('PROFILE', '通用映射', '显式JSON Profile')),
        default='AUTO', update=lambda self, context: _motion_source_changed(self, context))


def options(context):
    return getattr(context.scene, OPTIONS, None) if context.scene else None


def _module(name):
    return next((module for module in _UI_MODULES if module.__name__.rsplit('.', 1)[-1] == name), None)


def _motion_source_changed(opts, context):
    if opts.motion_source != 'AUTO':
        publish = _module('retarget_publish_ui')
        current = getattr(context.scene, publish.OPTIONS, None) if publish else None
        if current is not None:
            current.input_kind = opts.motion_source
        general = _module('retarget_general_job_ui')
        queue = getattr(context.scene, general.OPTIONS, None) if general else None
        if queue is not None and opts.motion_source in {'BODY', 'PROFILE'}:
            queue.mode = 'PROFILE_C94' if opts.motion_source == 'PROFILE' else 'GENERIC_FK'


def motion_source(context):
    opts = options(context)
    if opts and opts.motion_source != 'AUTO':
        return opts.motion_source
    publish = _module('retarget_publish_ui')
    current = getattr(context.scene, publish.OPTIONS, None) if publish else None
    return current.input_kind if current else 'BODY'


def related_rig(obj):
    if obj is None:
        return None
    if obj.type == 'ARMATURE':
        return obj
    rigs = {m.object for m in getattr(obj, 'modifiers', ()) if m.type == 'ARMATURE' and m.object}
    return next(iter(rigs)) if len(rigs) == 1 else None


def current_target(context):
    opts = options(context)
    if opts and opts.target:
        rig = opts.target
        if context.scene.objects.get(rig.name) != rig:
            return None, '所选角色不在当前场景，请重新选择'
        other = related_rig(context.view_layer.objects.active)
        if other is not None and other != rig:
            return rig, '活动对象属于另一角色；请重新确认目标'
        return rig, ''
    rig = related_rig(context.view_layer.objects.active)
    return (rig, '') if rig and context.scene.objects.get(rig.name) == rig else (None, '')


def stage(context):
    opts = options(context)
    if not opts:
        return 'PREPARE'
    if opts.stage != 'AUTO':
        return opts.stage
    entry = _module('workflow_ui')
    session = entry._SESSION if entry else None
    if session and session['plan'].scene == context.scene:
        return 'BIND'
    rig, _ = current_target(context)
    if rig:
        result = 'CONTROL' if (rig.get('c94_generated_rig_v1') or rig.get('rigging_modular_v1')) else 'MAINTAIN'
        if rig.get('rigging_modular_reference_v1'): result = 'PREPARE'
    else:
        result = 'PREPARE'
    if result not in _AVAILABLE_STAGES:
        return 'CONTROL'
    return result


def _lines(layout, text, width=25):
    region = bpy.context.region
    if region is not None and region.type == 'UI':
        scale = bpy.context.preferences.system.ui_scale
        width = min(width, max(10, int((region.width - 48) / (16 * scale))))
    for sentence in text.replace('。', '。\n').splitlines():
        for line in textwrap.wrap(sentence, width=width, break_long_words=True):
            layout.label(text=line)


def _sync_role(context, rig):
    for module in _UI_MODULES:
        name = getattr(module, 'OPTIONS', None)
        props = getattr(context.scene, name, None) if name else None
        if props is None:
            continue
        # Mesh editors also use the name "target". A Python assignment does
        # not enforce PointerProperty.poll, so only known rig targets sync.
        rig_targets = {'retarget_ui', 'retarget_mmd_ui', 'retarget_mmd_job_ui',
                       'retarget_profile_ui', 'retarget_contact_detection_ui',
                       'retarget_contact_job_ui'}
        fields = ('rig', 'target') if module.__name__.rsplit('.', 1)[-1] in rig_targets else ('rig',)
        for field in fields:
            rna = props.bl_rna.properties.get(field)
            if rna is not None and rna.type == 'POINTER' and rna.fixed_type.identifier == 'Object':
                setattr(props, field, rig)
    daily_ui = _module('face_daily_ui')
    if daily_ui:
        daily = getattr(context.scene, daily_ui.OPTIONS)
        hubs = [hub for hub in daily_ui.face_daily.hubs_for_rig(rig)
                if context.scene.objects.get(hub.name) == hub]
        daily.hub = hubs[0] if len(hubs) == 1 else None


class C94NAV_OT_Activate(bpy.types.Operator):
    bl_idname = 'c94_workflow.workspace_activate'
    bl_label = '进入角色'
    bl_description = '激活明确目标并同步各制作面板；来源、动作和角色数据不改变'

    def execute(self, context):
        opts = options(context)
        rig = opts.target or related_rig(context.view_layer.objects.active)
        if (rig is None or context.scene.objects.get(rig.name) != rig
                or context.view_layer.objects.get(rig.name) != rig):
            self.report({'ERROR'}, '请在当前场景与视图层中选择一个明确的角色骨架')
            return {'CANCELLED'}
        if context.mode not in {'OBJECT', 'POSE'} or not rig.visible_get() or rig.hide_select:
            self.report({'ERROR'}, '先退出编辑模式，并显示、解锁目标角色骨架')
            return {'CANCELLED'}
        if context.object and context.mode == 'POSE' and context.object != rig:
            bpy.ops.object.mode_set(mode='OBJECT')
        opts.target = rig
        for obj in context.selected_objects:
            obj.select_set(False)
        rig.select_set(True)
        context.view_layer.objects.active = rig
        _sync_role(context, rig)
        self.report({'INFO'}, '已进入角色：' + rig.name)
        return {'FINISHED'}


class C94NAV_PT_Workspace(bpy.types.Panel):
    bl_idname = 'C94NAV_PT_workspace'
    bl_label = '角色工作台'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色工作台'
    bl_order = -100

    def draw(self, context):
        layout = self.layout
        opts = options(context)
        layout.prop(opts, 'target', text='角色')
        layout.operator(C94NAV_OT_Activate.bl_idname, text='进入角色', icon='RESTRICT_SELECT_OFF')
        rig, warning = current_target(context)
        if warning:
            box = layout.box(); box.alert = True; _lines(box, warning)
        elif rig:
            layout.label(text=rig.name, icon='ARMATURE_DATA')
            layout.label(text='已生成角色' if (rig.get('c94_generated_rig_v1') or rig.get('rigging_modular_v1')) else '保留原骨架', icon='CHECKMARK')
            animation = rig.animation_data
            layout.label(text='已有动作 / NLA' if animation and (animation.action or animation.nla_tracks)
                              else '当前未绑定动作', icon='ACTION')
        else:
            layout.label(text='新角色：从准备与定位开始' if 'PREPARE' in _AVAILABLE_STAGES
                              else '选择此文件中的角色骨架', icon='INFO')
            if 'c94_positioning_state_v1' in context.scene:
                layout.label(text='已有定位任务，可继续检查与拟合', icon='CHECKMARK')
        layout.prop(opts, 'stage', text='阶段')
        layout.prop(opts, 'level', expand=True)
        if stage(context) == 'MOTION':
            layout.prop(opts, 'motion_source', text='来源')
        _lines(layout, HINTS[stage(context)])
        if opts.level == 'DEVELOPER':
            layout.label(text='插件 0.40.0 · 场景 ' + context.scene.name)
            layout.label(text='模式 ' + context.mode + ' · 阶段 ' + stage(context))


class C94NAV_PT_Tools(bpy.types.Panel):
    bl_idname = 'C94NAV_PT_tools'
    bl_label = '本阶段工具'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色工作台'
    bl_order = -50

    def draw(self, context):
        opts = options(context)
        if opts.level == 'DAILY' and stage(context) in {'BIND', 'FACE', 'MOTION', 'PREPARE'}:
            self.layout.label(text='更多制作工具可切换“进阶”', icon='INFO')
        if current_target(context)[1]:
            self.layout.label(text='先确认当前角色，再继续操作', icon='ERROR')


def route_panels(modules):
    if _ROUTED:
        raise RuntimeError('工作台面板已路由')
    _UI_MODULES[:] = modules
    for module in modules:
        short = module.__name__.rsplit('.', 1)[-1]
        route = ROUTES.get(short)
        panels = [value for value in vars(module).values() if isinstance(value, type)
                  and value.__module__ == module.__name__ and issubclass(value, bpy.types.Panel)]
        if panels and route is None:
            raise RuntimeError('面板缺少工作台入口映射：' + short)
        for cls in panels:
            saved = {key: cls.__dict__.get(key) for key in ('bl_category', 'bl_parent_id', 'bl_label', 'bl_order', 'bl_options', 'poll')}
            _ROUTED.append((cls, saved))
            destination, minimum, title = route
            destination = getattr(cls, 'workflow_stage', destination)
            title = getattr(cls, 'workflow_title', title)
            original = saved['poll']
            cls.poll = classmethod(_make_poll(destination, minimum, original, short))
            cls.bl_category = '角色工作台'
            cls.bl_parent_id = C94NAV_PT_Tools.bl_idname
            cls.bl_label = title
            cls.bl_order = list(ROUTES).index(short)
            cls.bl_options = {'DEFAULT_CLOSED'} if minimum else set()


def _make_poll(destination, minimum, original, module):
    # Blender RNA requires exactly cls/context, including for wrapped methods.
    def routed_poll(panel_cls, context):
        opts = options(context)
        if opts is None or stage(context) != destination or LEVEL[opts.level] < minimum:
            return False
        if current_target(context)[1]:
            return False
        rig = current_target(context)[0]
        if rig and rig.get('rigging_modular_v1') and module not in {
                'modular_ui', 'skinning_policy_ui', 'local_skinning_ui', 'native_maintenance_ui'}:
            return False
        sources = {'retarget_ui': 'BODY', 'retarget_job_ui': 'BODY',
                   'retarget_mmd_ui': 'MMD', 'retarget_mmd_import_ui': 'MMD',
                   'retarget_mmd_job_ui': 'MMD', 'retarget_mmd_camera_ui': 'MMD',
                   'retarget_profile_ui': 'PROFILE'}
        if module == 'retarget_general_job_ui' and motion_source(context) == 'MMD':
            return False
        if module in sources and motion_source(context) != sources[module]:
            return False
        return bool(original.__get__(None, panel_cls)(context)) if original else True
    return routed_poll


def register(stage_ids=None):
    if _REGISTERED or hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('角色工作台已由其他实例注册')
    chosen = tuple(row for row in STAGES if stage_ids is None or row[0] in stage_ids)
    if not chosen or chosen[0][0] != 'AUTO' or len(chosen) < 2:
        raise ValueError('工作台必须包含自动与至少一个明确阶段')
    _AVAILABLE_STAGES.update(row[0] for row in chosen)
    C94NAV_Options.__annotations__['stage'] = EnumProperty(name='制作阶段', items=chosen, default='AUTO')
    try:
        for cls in (C94NAV_Options, C94NAV_OT_Activate, C94NAV_PT_Workspace, C94NAV_PT_Tools):
            bpy.utils.register_class(cls); _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94NAV_Options))
    except Exception:
        unregister()
        raise


def unregister():
    _UI_MODULES.clear()
    _AVAILABLE_STAGES.clear()
    for cls, saved in reversed(_ROUTED):
        for key, value in saved.items():
            if value is None:
                if key in cls.__dict__:
                    delattr(cls, key)
            else:
                setattr(cls, key, value)
    _ROUTED.clear()
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if _REGISTERED and prop and prop.type == 'POINTER' and prop.fixed_type == C94NAV_Options.bl_rna:
        delattr(bpy.types.Scene, OPTIONS)
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
