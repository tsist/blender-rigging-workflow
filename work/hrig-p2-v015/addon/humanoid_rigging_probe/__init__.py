"""P2 research probe. Importing this module does not register the add-on."""

bl_info = {
    "name": "Humanoid Rigging Probe — P2研究原型",
    "author": "Blender AI research",
    "version": (0, 15, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > P2 Rig Probe",
    "description": "只读面板；CLI提供局部旋转、动作片段、肢体及精确权重编辑",
    "category": "Rigging",
    "warning": "P2研究原型：GUI/FPS暂缓，未完成完整角色绑定",
}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
