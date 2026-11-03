"""Minimal explicit C-stage panel; domain operations live in session.py."""
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty

from .session import get_session


_EMPTY_GROUP = [('all', '全部控制', '')]
_EMPTY_ROLE = [('uninitialized', '未初始化', '')]


def _groups(self, context):
    session = get_session(False)
    return session.group_enum() if session else _EMPTY_GROUP


def _roles(self, context):
    session = get_session(False)
    if session is None:
        return _EMPTY_ROLE
    return session.role_enum(self.group or 'all')


def _group_changed(self, context):
    session = get_session(False)
    if session is not None:
        roles = session.role_enum(self.group or 'all')
        if roles:
            self.role = roles[0][0]
            self.value = session.default_value(self.role)


def _role_changed(self, context):
    session = get_session(False)
    if session is not None and self.role in session._roles:
        self.value = session.default_value(self.role)


class HRIG_C_Properties(bpy.types.PropertyGroup):
    group: EnumProperty(name='分组', items=_groups, update=_group_changed)
    role: EnumProperty(name='控制', items=_roles, update=_role_changed)
    value: FloatProperty(name='输入值', default=0.12, precision=4)


def _execute(operator, callback):
    try:
        result = callback()
        session = get_session()
        text = result.get('status', '完成')
        if session.last_log_error:
            text += '；日志写入异常，操作未重试'
        operator.report({'INFO'}, text)
        return {'FINISHED'}
    except Exception as error:
        operator.report({'ERROR'}, getattr(error, 'code', type(error).__name__) + ': ' + str(error))
        return {'CANCELLED'}


class HRIG_C_OT_preview(bpy.types.Operator):
    bl_idname = 'hrig_c.preview'
    bl_label = '预览所选控制'
    bl_description = '设置该角色及必要模式端点；未知动画/驱动写入冲突将明确拒绝'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.preview_active and not s.animation_active

    def execute(self, context):
        p = context.window_manager.hrig_c_controls
        return _execute(self, lambda: get_session().preview(p.role, p.value))


class HRIG_C_OT_restore(bpy.types.Operator):
    bl_idname = 'hrig_c.restore'
    bl_label = '完整恢复预览'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and s.preview_active

    def execute(self, context):
        return _execute(self, lambda: get_session().restore_preview())


class HRIG_C_OT_author(bpy.types.Operator):
    bl_idname = 'hrig_c.author'
    bl_label = '写入三关键帧动作'
    bl_description = '独立作者事务：复制原Action，保留非目标曲线；所选输入在0/12/24帧往返'
    use_preset: BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.preview_active and not s.animation_active

    def execute(self, context):
        p = context.window_manager.hrig_c_controls
        return _execute(self, lambda: get_session().author(p.role, p.value, use_preset=self.use_preset))


class HRIG_C_OT_frame(bpy.types.Operator):
    bl_idname = 'hrig_c.frame'
    bl_label = '观察动作时刻'
    frame: IntProperty(name='观察帧', default=12, min=0, max=1048574)

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.preview_active

    def execute(self, context):
        return _execute(self, lambda: get_session().show_frame(self.frame))


class HRIG_C_OT_undo(bpy.types.Operator):
    bl_idname = 'hrig_c.undo_animation'
    bl_label = '归零并撤销动作'
    bl_description = '先回作者原时刻，再核对保护状态并恢复原Action/输入'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.preview_active and s.animation_active

    def execute(self, context):
        return _execute(self, lambda: get_session().undo_animation())


class HRIG_C_OT_save(bpy.types.Operator):
    bl_idname = 'hrig_c.save_copy'
    bl_label = '保存动作工作副本'
    bl_description = '另存output-C-gui.blend，不覆盖已有文件，不改变当前Main路径'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.preview_active and s.animation_active and not s.save_attempted

    def execute(self, context):
        return _execute(self, lambda: get_session().save_copy())


class HRIG_C_PT_controls(bpy.types.Panel):
    bl_label = 'C 角色控制与动作'
    bl_idname = 'HRIG_C_PT_controls'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'HRIG C'

    def draw(self, context):
        layout = self.layout
        session = get_session(False)
        if session is None:
            layout.label(text='请先显式初始化 C 会话')
            return
        state = context.window_manager.hrig_c_controls
        layout.label(text='角色：' + session.profile['character_id'])
        layout.prop(state, 'group')
        layout.prop(state, 'role')
        if state.role in session._roles:
            role = session.role(state.role)
            layout.label(text=role['bone'])
            layout.label(text='%s  [%g, %g]' % (role['kind'], *role['bounds']))
        layout.prop(state, 'value')
        column = layout.column(align=True)
        column.operator('hrig_c.preview')
        column.operator('hrig_c.restore')
        layout.separator()
        layout.operator('hrig_c.author')
        if session.preset is not None:
            layout.operator('hrig_c.author', text='写入预置示范动作').use_preset = True
        row = layout.row(align=True)
        row.operator('hrig_c.frame', text='观察第6帧').frame = 6
        row.operator('hrig_c.frame', text='观察第12帧').frame = 12
        if session.preset_observation_frame is not None:
            row = layout.row()
            row.enabled = session.animation_active
            row.operator('hrig_c.frame', text='观察全身示范').frame = session.preset_observation_frame
        layout.operator('hrig_c.frame', text='返回第0帧').frame = 0
        layout.operator('hrig_c.undo_animation')
        layout.operator('hrig_c.save_copy')
        layout.separator()
        layout.label(text='预览：' + ('待恢复' if session.preview_active else '空闲'))
        layout.label(text='动作：' + ('已写入，可撤销' if session.animation_active else '未写入'))
        if session.last_error:
            layout.label(text=session.last_error, icon='ERROR')
        if session.last_log_error:
            layout.label(text='证据写入异常；请查看会话结果', icon='ERROR')


_CLASSES = (HRIG_C_Properties, HRIG_C_OT_preview, HRIG_C_OT_restore, HRIG_C_OT_author,
            HRIG_C_OT_frame, HRIG_C_OT_undo, HRIG_C_OT_save, HRIG_C_PT_controls)


def register():
    for cls in _CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)
    if not hasattr(bpy.types.WindowManager, 'hrig_c_controls'):
        bpy.types.WindowManager.hrig_c_controls = PointerProperty(type=HRIG_C_Properties)


def unregister():
    s = get_session(False)
    if s is not None and (s.preview_active or s.animation_active):
        raise RuntimeError('Restore pending C session before unregister')
    if hasattr(bpy.types.WindowManager, 'hrig_c_controls'):
        del bpy.types.WindowManager.hrig_c_controls
    for cls in reversed(_CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
