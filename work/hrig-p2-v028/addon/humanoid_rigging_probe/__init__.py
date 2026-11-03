"""P2 research probe. Importing this module does not register the add-on."""

bl_info = {
    "name": "Humanoid Rigging Probe — P2研究原型",
    "author": "Blender AI research",
    "version": (0, 22, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > P2 Rig Probe",
    "description": "头发控制选择与Action归属；CLI提供有限绑定编辑",
    "category": "Rigging",
    "warning": "研究候选：头发控制和动作仅限固定爱音分支；完整角色绑定未完成",
}


def register():
    from . import ui,hair_controls
    ui.register()
    try:hair_controls.register()
    except Exception:
        ui.unregister()
        raise


def unregister():
    from . import ui,hair_controls
    hair_controls.unregister()
    ui.unregister()
