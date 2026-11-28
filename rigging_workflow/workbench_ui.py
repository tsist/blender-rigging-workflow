"""Five-stage presentation for the full rigging add-on."""
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, StringProperty

from . import workflow_navigation as navigation
from ._ui_kit import (
    BackgroundJobRunner, HUDManager, IconPack, SidebarDock, apply_collection_theme,
    draw_heavy_workspace_header, ensure_object_mode, get_theme, primary_button, progress_card,
    prop_column, section_panel, status_banner,
)

DOMAIN = 'rigging'
THEME = get_theme(DOMAIN)
_ICONS = IconPack(DOMAIN)

_STAGES = (
    ('SKELETON', '骨架', '骨架构建与关节定位', 1),
    ('SKIN', '蒙皮', '体素蒙皮与权重精修', 2),
    ('CONTROL', '控制', '角色控制器与次级动态', 3),
    ('FACE', '面部', '面部表情与口型', 4),
    ('MOTION', '动作', '动作重定向与发布', 5),
)
_LEVELS = (
    ('DAILY', '日常', '显示常用制作操作', 1),
    ('ADVANCED', '进阶', '显示完整制作与维护工具', 2),
)
_FROM_LEGACY = {
    'AUTO': 'SKELETON', 'PREPARE': 'SKELETON', 'BIND': 'SKIN', 'CONTROL': 'CONTROL',
    'FACE': 'FACE', 'DYNAMIC': 'CONTROL', 'MOTION': 'MOTION', 'MAINTAIN': 'CONTROL',
}
_TO_LEGACY = {'SKELETON': 'PREPARE', 'SKIN': 'BIND', 'CONTROL': 'CONTROL', 'FACE': 'FACE', 'MOTION': 'MOTION'}
_CHAPTER = {
    'SKELETON': ('骨架构建与定位', 'rig_prepare'),
    'SKIN': ('体素蒙皮与权重精修', 'voxel_skin'),
    'CONTROL': ('控制器与次级动态', 'ik_fk_control'),
    'FACE': ('面部表情与口型', 'face_rig'),
    'MOTION': ('动作重定向与发布', 'retarget_motion'),
}


def _get_stage(opts):
    context = bpy.context
    legacy = navigation.stage(context) if context.scene == opts.id_data else opts.stage
    key = _FROM_LEGACY.get(legacy, 'SKELETON')
    return next(number for item, _, _, number in _STAGES if item == key)


def _set_stage(opts, value):
    key = next(item for item, _, _, number in _STAGES if number == value)
    opts.stage = _TO_LEGACY[key]


def _get_level(opts):
    return 1 if opts.level == 'DAILY' else 2


def _set_level(opts, value):
    opts.level = 'DAILY' if value == 1 else 'ADVANCED'


def options_properties():
    return {
        'workspace_stage': EnumProperty(name='制作阶段', items=_STAGES, get=_get_stage, set=_set_stage),
        'workspace_level': EnumProperty(name='工具档位', items=_LEVELS, get=_get_level, set=_set_level),
        'last_report': StringProperty(options={'SKIP_SAVE'}),
        'last_report_kind': StringProperty(default='INFO', options={'SKIP_SAVE'}),
    }


def route(module, legacy_stage, title):
    if module in {'ui', 'generation_ui'}:
        destination = 'SKELETON'
    elif module in {'accessory_ui', 'corrective_ui'}:
        destination = 'CONTROL'
    else:
        destination = _FROM_LEGACY[legacy_stage]
    return destination, title.partition(' · ')[2] if title[:2].isdigit() else title


def visible(opts, destination):
    return opts.workspace_stage == destination and opts.workspace_level == 'ADVANCED'


def theme_created_collections(before):
    for collection in set(bpy.data.collections) - before:
        apply_collection_theme(collection, DOMAIN)


def _workflow_status(state):
    labels = {
        'starting': '正在启动求解', 'occupancy': '正在体素化模型', 'graph': '正在建立体素邻接',
        'seeds': '正在写入骨骼种子', 'diffusion': '正在扩散权重', 'sample': '正在采样顶点权重',
        'surface_transfer': '正在将权重投射至模型', 'complete': '正在核验输出',
    }
    detail = state.get('progress') or state
    phase = labels.get(detail.get('phase'), '正在计算体素权重')
    if state.get('cancel_requested'):
        phase = '正在结束本次求解'
    done, total = detail.get('done'), detail.get('total')
    if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
        fraction = max(0., min(1., done / total))
    else:
        fraction = 0.
    return phase, fraction


class _VoxelProgress(BackgroundJobRunner):
    """Present the existing voxel subprocess, whose transaction owns cancel and apply."""

    scene_id = None
    cancel_operator = ''

    def observe(self, scene, state, cancel_operator='rigging_modular.cancel_binding'):
        opts = navigation.options(SimpleNamespace(scene=scene))
        if opts is None or not hasattr(opts, 'last_report'):
            return
        status = state.get('status')
        if status == 'running':
            self.scene_id = scene.as_pointer()
            self.cancel_operator = cancel_operator
            self.detail, self.progress = _workflow_status(state)
            self.running = True
            HUDManager.show(DOMAIN, '体素蒙皮', self.detail, progress=self.progress, kind='RUNNING')
            opts.last_report = self.detail
            opts.last_report_kind = 'RUNNING'
        else:
            if self.scene_id == scene.as_pointer():
                self.clear()
            message, level = {
                'succeeded': ('权重求解完成，可创建独立蒙皮候选', 'OK'),
                'cancelled': ('体素求解已取消', 'WARN'),
                'failed': ('体素求解失败，请查看本次作业报告', 'ERROR'),
                'applied': ('已应用蒙皮候选', 'OK'),
            }.get(status, ('', 'INFO'))
            if message:
                opts.last_report, opts.last_report_kind = message, level

    def clear(self):
        self.running = False
        self.scene_id = None
        self.cancel_operator = ''
        HUDManager.clear(DOMAIN)


VOXEL_PROGRESS = _VoxelProgress(DOMAIN)


@persistent
def _clear(_):
    VOXEL_PROGRESS.clear()


def _module_options(name, context):
    module = navigation._module(name)
    return getattr(context.scene, module.OPTIONS) if module else None


def _skeleton(layout, context, rig):
    props = _module_options('modular_ui', context)
    if props is None:
        return
    row = layout.row(align=True)
    row.prop(props, 'kind', expand=True)
    col = prop_column(layout, align=True)
    col.prop(props, 'label', text='角色名称')
    col.prop(props, 'source', text='目标网格')
    col.prop(props, 'reference', text='参考骨架')
    primary_button(layout, 'rigging_modular.prepare_reference', '创建参考骨架',
                   icon_value=_ICONS.action_id('rig_prepare'), icon='ARMATURE_DATA')
    row = layout.row(align=True)
    row.operator('rigging_modular.estimate', text='估算关节')
    row.operator('rigging_modular.edit_reference', text='编辑骨位')
    row = layout.row(align=True)
    row.operator('rigging_modular.confirm_reference', text='确认参考骨架')
    row.operator('rigging_modular.generate', text='生成角色骨架')
    if props.message:
        status_banner(layout, context, props.message, icon_pack=_ICONS)
    if navigation.options(context).workspace_level != 'DAILY':
        return
    _, standard = section_panel(layout, 'RIG_SEC_standard_skeleton', '标准人形模板', default_closed=True)
    if standard is not None:
        setup = navigation._module('workspace_setup')
        setup.C94SETUP_PT_Prepare.draw(SimpleNamespace(layout=standard), context)
        row = standard.row(align=True)
        row.operator('c94_workflow.estimate_markers', text='估算定位标记')
        row.operator('c94_workflow.fit_reference', text='拟合参考骨')
        generation = navigation._module('generation_ui')
        if generation.C94GEN_PT_Generation.poll(context):
            generation.C94GEN_PT_Generation.draw(SimpleNamespace(layout=standard), context)
            standard.operator('c94_workflow.generate_rig', text='生成标准角色骨架')


def _skin(layout, context, rig):
    entry = navigation._module('workflow_ui')
    session = entry._SESSION if entry else None
    if session is not None and session['scene'] == context.scene:
        props = _module_options('workflow_ui', context)
        col = prop_column(layout, align=True)
        col.prop(props, 'index')
        col.prop_search(props, 'proxy', context.scene, 'objects')
        col.prop(props, 'jobs_directory')
        layout.operator('c94_workflow.start_character_volume', text='后台求解身体权重', icon='PLAY')
        row = layout.row(align=True)
        row.operator('c94_workflow.apply_character_volume', text='应用身体权重')
        row.operator('c94_workflow.finish_character_entry', text='完成本次角色')
        return
    modular = navigation._module('modular_ui')
    props = _module_options('modular_ui', context)
    if props is None:
        return
    col = prop_column(layout, align=True)
    col.prop(props, 'source', text='原始模型')
    if not rig:
        col.prop(props, 'rig', text='目标骨架')
    if modular._native_selected(props):
        col.prop(props, 'native_proxy', text='计算代理')
    else:
        col.prop(props, 'proxy_mode', text='代理方式')
        col.prop(props, 'body_collection', text='身体集合')
    _, gate = section_panel(layout, 'RIG_SEC_skin_authorization', '骨架验收登记', default_closed=True)
    if gate is not None:
        col = prop_column(gate, align=True)
        col.prop(props, 'gate_reference', text='静态参照')
        col.prop(props, 'gate_receipt', text='验收回执')
        gate.operator('rigging_modular.authorize_skeleton')
    primary_button(layout, 'rigging_modular.start_binding', '后台求解体素权重',
                   icon_value=_ICONS.action_id('voxel_skin'), icon='PLAY',
                   enabled=props.source is not None and props.rig is not None and not VOXEL_PROGRESS.running)
    row = layout.row(align=True)
    apply = row.row()
    apply.enabled = modular._SESSION is not None and not VOXEL_PROGRESS.running
    apply.operator('rigging_modular.apply_binding', text='创建蒙皮候选')
    row.operator('c94_workflow.local_skinning_capture', text='捕获局部选区')
    _, standard = section_panel(layout, 'RIG_SEC_standard_skin', '从标准角色模板绑定', default_closed=True)
    if standard is not None:
        standard.operator('c94_workflow.capture_character_inputs', text='捕获选中输入')
        standard.operator('c94_workflow.prepare_character_entry', text='准备独立角色候选')
    if props.message:
        status_banner(layout, context, props.message, icon_pack=_ICONS)


def _control(layout, context, rig):
    if rig is None:
        layout.label(text='请选择要操作的角色骨架', icon='INFO')
        return
    if rig.get('rigging_modular_v1'):
        module = navigation._module('modular_ui')
        if module.options(context).rig == rig:
            module.MODULAR_PT_Control.draw(SimpleNamespace(layout=layout), context)
        else:
            layout.label(text='点击「进入角色」以绑定当前控制目标', icon='INFO')
        return
    module = navigation._module('control_panel')
    if module:
        try:
            character = module.current_rig(context)
            module._button(layout, 'c94_workflow.control_pose', character, text='进入姿态模式', icon='POSE_HLT')
            _, body = section_panel(layout, 'RIG_SEC_daily_controls', '身体与四肢控制', default_closed=False)
            if body is not None:
                module.C94CTRL_PT_Character.draw(SimpleNamespace(layout=body), context)
        except ValueError as error:
            status_banner(layout, context, str(error), kind='INFO', icon_pack=_ICONS)


def _face(layout, context, rig):
    module = navigation._module('face_daily_ui')
    if rig is None or module is None:
        layout.label(text='选择带面部控制的角色骨架', icon='INFO')
        return
    module.C94DAILY_PT_Face.draw(SimpleNamespace(layout=layout), context)


def _motion(layout, context, rig):
    if rig and rig.get('rigging_modular_v1'):
        module = navigation._module('modular_ui')
        if module.options(context).rig == rig:
            module.MODULAR_PT_Motion.draw(SimpleNamespace(layout=layout), context)
        else:
            layout.label(text='点击「进入角色」以绑定重定向目标', icon='INFO')
        return
    opts = navigation.options(context)
    layout.prop(opts, 'motion_source', text='动作来源')
    source = navigation.motion_source(context)
    if source == 'MMD':
        layout.operator('c94_workflow.retarget_mmd_import', text='导入 MMD 动作', icon='IMPORT')
        layout.operator('c94_workflow.retarget_mmd_scene', text='构建动作候选')
    elif source == 'PROFILE':
        layout.operator('c94_workflow.profile_import_source', text='导入映射来源', icon='IMPORT')
        layout.operator('c94_workflow.retarget_general_job_prepare', text='准备重定向任务')
    else:
        row = layout.row(align=True)
        row.operator('c94_workflow.retarget_import_fbx', text='导入 FBX', icon='IMPORT')
        row.operator('c94_workflow.retarget_import_bvh', text='导入 BVH', icon='IMPORT')
        layout.operator('c94_workflow.retarget_action', text='构建动作候选')
    layout.operator('c94_workflow.retarget_publish', text='正式使用候选动作', icon='CHECKMARK')


_DRAW = {'SKELETON': _skeleton, 'SKIN': _skin, 'CONTROL': _control, 'FACE': _face, 'MOTION': _motion}


def draw(layout, context, opts):
    draw_heavy_workspace_header(
        layout, DOMAIN, _ICONS, opts, stage_prop='workspace_stage',
        level_prop='workspace_level', target_prop='target', target_label='当前角色',
    )
    layout.operator(navigation.C94NAV_OT_Activate.bl_idname, text='进入角色', icon='RESTRICT_SELECT_OFF')
    if VOXEL_PROGRESS.running and VOXEL_PROGRESS.scene_id == context.scene.as_pointer():
        progress_card(layout, DOMAIN, _ICONS, text=VOXEL_PROGRESS.detail,
                      factor=VOXEL_PROGRESS.progress, cancel_operator=VOXEL_PROGRESS.cancel_operator)
    rig, warning = navigation.current_target(context)
    label, icon = _CHAPTER[opts.workspace_stage]
    layout.label(text=label, icon_value=_ICONS.action_id(icon))
    if warning:
        status_banner(layout, context, warning, kind='WARN', icon_pack=_ICONS)
    else:
        _DRAW[opts.workspace_stage](layout, context, rig)
    if opts.last_report and opts.last_report_kind != 'RUNNING':
        status_banner(layout, context, opts.last_report, kind=opts.last_report_kind, icon_pack=_ICONS)


def register():
    _ICONS.register()
    SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)
    if _clear not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_clear)


def unregister():
    VOXEL_PROGRESS.clear()
    VOXEL_PROGRESS.cleanup()
    if _clear in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_clear)
    SidebarDock.unregister_plugin(DOMAIN)
    _ICONS.unregister()
