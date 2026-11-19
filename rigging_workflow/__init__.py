"""Native character workflow: one workspace, shared cores, owned lifecycle."""
import importlib
import bpy

bl_info = {
    'name': '角色绑定工作台',
    'author': 'Blender AI Project',
    'version': (0, 40, 1),
    'blender': (5, 2, 0),
    'location': 'View3D > Sidebar > 角色工作台',
    'category': 'Rigging',
    'description': '角色准备、绑定、控制、面部、辅助动态与动画重定向',
}

_PACKAGE_REGISTERED = False
_REGISTERED_MODULES = []
_MODULE_ORDER = (
    'ui', 'workspace_setup', 'positioning_ui', 'inspection_ui', 'control_panel', 'accessory_ui',
    'generation_ui', 'workflow_ui', 'corrective_ui', 'skinning_ui',
    'skinning_plan_ui', 'skinning_policy_ui', 'local_skinning_ui', 'skin_parts_ui',
    'face_modes_ui', 'face_daily_ui', 'face_capture_ui', 'face_pose_library_ui',
    'face_expression_controls_ui', 'face_key_mapping_ui', 'face_key_revision_ui',
    'elastic', 'elastic_ui', 'surface_dynamic', 'surface_dynamic_ui',
    'native_maintenance_ui', 'author_rework_ui', 'retarget_ui', 'retarget_headneck_ui',
    'retarget_mmd_ui', 'retarget_mmd_import_ui', 'retarget_job_ui', 'retarget_mmd_job_ui',
    'retarget_publish_ui', 'retarget_generic_target_ui', 'retarget_profile_ui',
    'retarget_general_job_ui', 'retarget_contact_correction_ui', 'retarget_contact_job_ui',
    'retarget_contact_detection_ui', 'retarget_contact_hints', 'retarget_mmd_camera_ui',
)


def _preflight(modules):
    """Reject pre-existing owners before registering or removing anything."""
    for module in modules:
        if getattr(module, '_REGISTERED', ()):
            raise RuntimeError(f'{module.__name__}已由其他入口启用；原实例保持')
        name = getattr(module, 'OPTIONS', getattr(module, 'OPTIONS_ATTR', None))
        if isinstance(name, str) and hasattr(bpy.types.Scene, name):
            raise RuntimeError(f'场景属性{name}已由其他实例持有；原属性保持')
    from . import elastic, surface_dynamic
    pairs = ((bpy.app.handlers.load_pre, elastic._clear),
             (bpy.app.handlers.save_pre, elastic._clear),
             (bpy.app.handlers.frame_change_pre, surface_dynamic._frame),
             (bpy.app.handlers.load_pre, surface_dynamic._clear),
             (bpy.app.handlers.save_pre, surface_dynamic._save_preview))
    if any(fn in handlers for handlers, fn in pairs):
        raise RuntimeError('辅助动态已由其他入口启用；原实例保持')
    for name in dir(bpy.app.handlers):
        callbacks = getattr(bpy.app.handlers, name)
        if isinstance(callbacks, list) and any(
            getattr(fn, '__module__', '').startswith(__name__ + '.') for fn in callbacks):
            raise RuntimeError('本工作流已有单独启用的回调；请从原入口停止，原会话保持')


def register():
    global _PACKAGE_REGISTERED
    if _PACKAGE_REGISTERED or _REGISTERED_MODULES:
        raise RuntimeError('当前绑定插件实例已注册；原实例保持，不重复注册或卸载')
    modules = [importlib.import_module('.' + name, __name__) for name in _MODULE_ORDER]
    _preflight(modules)
    from . import workflow_navigation
    try:
        _REGISTERED_MODULES.append(workflow_navigation)
        workflow_navigation.register()
        workflow_navigation.route_panels(modules)
        for module in modules:
            _REGISTERED_MODULES.append(module)
            module.register()
        _PACKAGE_REGISTERED = True
    except Exception as exc:
        try:
            unregister()
        except Exception as cleanup_error:
            exc.add_note('部分清理未完成：' + str(cleanup_error))
        raise


def unregister():
    global _PACKAGE_REGISTERED
    errors = []
    # Only visited acquisition steps may unwind. Keep the parent workspace
    # until all child registrations are gone so partial disable can be retried.
    for module in tuple(reversed(_REGISTERED_MODULES)):
        if module.__name__.endswith('.workflow_navigation') and len(_REGISTERED_MODULES) > 1:
            continue
        try:
            module.unregister()
        except Exception as exc:
            errors.append(f'{module.__name__}: {exc}')
        else:
            _REGISTERED_MODULES.remove(module)
    _PACKAGE_REGISTERED = bool(_REGISTERED_MODULES)
    if errors:
        raise RuntimeError('插件部分停用失败，可重试：' + '; '.join(errors))
