# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit face-input mode and collection controls on the current managed rig."""
import textwrap

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from . import control_panel


_REGISTERED = []
_VISIBILITY = (
    ('panel', '表情面板', '表情面板'),
    ('basic', '面部', '基础面部'),
    ('main', '面部-主控', '面部主控'),
    ('secondary', '面部-次要', '面部次要'),
)
_ERRORS = (ValueError, RuntimeError, TypeError, KeyError, AttributeError, ReferenceError)


def _lines(layout, text):
    for line in textwrap.wrap(str(text), width=30) or ['']:
        layout.label(text=line)


def _button(layout, operator, rig, *, text, **values):
    options = {key: values.pop(key) for key in tuple(values) if key in {'icon', 'depress'}}
    button = layout.operator(operator, text=text, **options)
    button.rig_name = rig.name
    for key, value in values.items():
        setattr(button, key, value)
    return button


class _FaceOperator:
    rig_name: StringProperty(options={'HIDDEN'})

    def run(self, context, operation, message):
        try:
            rig = control_panel.current_rig(context, self.rig_name)
            operation(rig)
            self.report({'INFO'}, message)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FACE_ManualKeyOption(bpy.types.PropertyGroup):
    object_name: StringProperty(name='对象', options={'HIDDEN'})
    key_name: StringProperty(name='形态键', options={'HIDDEN'})
    selected: BoolProperty(name='纳入面部输入', default=False)


class C94FACE_UL_ManualKeys(bpy.types.UIList):
    bl_idname = 'C94FACE_UL_manual_keys'

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0):
        layout.prop(item, 'selected', text=item.object_name + ' · ' + item.key_name)


class C94FACE_OT_CaptureNeutral(_FaceOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_capture_neutral'
    bl_label = '记录当前面部中立输入'
    bl_description = '由操作者认定当前输入为中立并明确记录；不自动证明无表情，不制作角色表情形态键'
    bl_options = {'REGISTER', 'UNDO'}
    manual_keys: CollectionProperty(type=C94FACE_ManualKeyOption, options={'SKIP_SAVE'})
    active_manual_key: IntProperty(default=0, min=0, options={'HIDDEN', 'SKIP_SAVE'})
    selection_ready: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        try:
            from . import face_modes
            rig = control_panel.current_rig(context, self.rig_name)
            self.rig_name = rig.name
            self.selection_ready = False
            self.manual_keys.clear()
            self.active_manual_key = 0
            options = face_modes.manual_key_options(rig)
            if not options:
                return self.execute(context)
            for option in options:
                item = self.manual_keys.add()
                item.object_name = option['object']
                item.key_name = option['key']
                item.selected = option['selected']
            self.selection_ready = True
            return context.window_manager.invoke_props_dialog(self, width=560)
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

    def draw(self, context):
        layout = self.layout
        _lines(layout, '只勾选需要纳入面部中立检查和定向复位的手工Key；驱动Key由控件管理')
        if self.selection_ready:
            layout.template_list('C94FACE_UL_manual_keys', '', self, 'manual_keys',
                                 self, 'active_manual_key', rows=8)
            layout.label(text='已勾选：' + str(sum(item.selected for item in self.manual_keys)))
        _lines(layout, '未勾选的手工Key不纳入本块面部输入；不自动选择身体Key')
        _lines(layout, '当前输入由操作者认定中立，不自动证明无表情')

    def execute(self, context):
        from . import face_modes
        manual_keys = ([(item.object_name, item.key_name) for item in self.manual_keys if item.selected]
                       if self.selection_ready else None)
        return self.run(context, lambda rig: face_modes.capture_neutral(rig, manual_keys=manual_keys),
                        '已记录操作者认定的当前面部中立输入')


class C94FACE_OT_Switch(_FaceOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_switch_mode'
    bl_label = '安全切换面部模式'
    bl_description = ('骨控模式仍可受形态键影响。安全切换会保留当前输入，'
                      '存在冲突时会提示。复位以已记录的面部中立状态为准。')
    bl_options = {'REGISTER', 'UNDO'}
    target: EnumProperty(name='目标模式', items=(('KEY', '形态键模式', ''), ('BONE', '骨控模式', '')))
    reset: BoolProperty(name='先复位面部输入', default=False,
                        description='仅将本块管理的面部输入恢复到已记录的中立值，再切换模式；不全身复位')
    follow_visibility: BoolProperty(name='同步控件空间显隐', default=True,
                                    description='切换时同步相关面部控件集合的显隐；可随后单独调整')

    def execute(self, context):
        from . import face_modes
        mode = 'Key模式' if self.target == 'KEY' else '骨控模式'
        message = ('已复位面部输入并切换至' if self.reset else '已安全切换至') + mode
        return self.run(context, lambda rig: face_modes.switch(
            rig, target=self.target, reset=self.reset, follow_visibility=self.follow_visibility), message)


class C94FACE_OT_Visibility(_FaceOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_collection_visibility'
    bl_label = '面部控件集合显隐'
    bl_description = '只改变指定面部控件集合的显隐，不切换模式、不复位或修改面部输入'
    bl_options = {'REGISTER', 'UNDO'}
    collection: EnumProperty(name='控件集合', items=tuple((key, label, '') for key, _, label in _VISIBILITY))
    visible: BoolProperty(name='显示', default=True)

    def execute(self, context):
        from . import face_modes
        return self.run(context, lambda rig: face_modes.set_visibility(rig, **{self.collection: self.visible}),
                        '已更新面部控件集合显隐')


class C94FACE_OT_Restore(_FaceOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_restore_previous'
    bl_label = '恢复上次切换/复位'
    bl_description = '恢复本块记录的上次面部切换或复位前状态；不代表全身撤销或表情作者历史'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import face_modes
        return self.run(context, face_modes.restore_previous, '已恢复上次面部切换/复位前状态')


class C94FACE_PT_Modes(bpy.types.Panel):
    bl_idname = 'C94FACE_PT_modes'
    bl_label = '面部模式与控件空间'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '面部'
    bl_order = 0
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        try:
            control_panel.current_rig(context)
            return True
        except (ValueError, ReferenceError):
            return False

    def draw(self, context):
        layout = self.layout
        try:
            from . import face_modes
            rig = control_panel.current_rig(context)
            state = face_modes.inspect(rig)
        except _ERRORS as exc:
            _lines(layout, str(exc))
            return
        mode = {'KEY': 'Key模式', 'BONE': '骨控模式'}.get(state['mode'], str(state['mode']))
        layout.label(text='当前：' + mode)
        layout.label(text=f"面部控件：{state['control_count']}；手动形态键：{state['manual_key_count']}")
        if state['conflicts']:
            box = layout.box()
            box.label(text='当前输入冲突：' + str(len(state['conflicts'])) + '项', icon='ERROR')
            for conflict in state['conflicts'][:4]:
                _lines(box, conflict)
            if len(state['conflicts']) > 4:
                box.label(text='另有' + str(len(state['conflicts']) - 4) + '项输入冲突')
        else:
            layout.label(text='未发现本块定义的输入冲突', icon='INFO')
        _button(layout, 'c94_workflow.face_capture_neutral', rig, text='记录当前面部中立输入')
        if not state['configured']:
            _lines(layout, '先由操作者确认中立输入，再明确记录；不会自动捕获')
        switch = layout.column(align=True)
        switch.enabled = state['configured']
        for target, label in (('KEY', 'Key模式'), ('BONE', '骨控模式')):
            _button(switch, 'c94_workflow.face_switch_mode', rig, text='安全切换至' + label,
                    target=target, reset=False)
        reset = layout.box()
        reset.label(text='明确复位本块面部输入后切换')
        actions = reset.column(align=True)
        actions.enabled = state['configured']
        for target, label in (('KEY', 'Key模式'), ('BONE', '骨控模式')):
            _button(actions, 'c94_workflow.face_switch_mode', rig, text='复位面部输入后切换至' + label,
                    target=target, reset=True)
        visibility = layout.box()
        visibility.label(text='控件集合显隐（独立于模式）')
        for key, name, label in _VISIBILITY:
            row = visibility.row()
            available = name in state['visibility']
            row.enabled = available
            visible = bool(state['visibility'].get(name, False))
            _button(row, 'c94_workflow.face_collection_visibility', rig,
                    text=label if available else label + '（集合缺失）',
                    collection=key, visible=not visible, depress=visible,
                    icon='HIDE_OFF' if visible else 'HIDE_ON')
        _button(layout, 'c94_workflow.face_restore_previous', rig, text='恢复上次切换/复位')
        if state['note']:
            _lines(layout, state['note'])
        _lines(layout, '原生开关仅部分门控；骨控模式仍可能消费Key')
        _lines(layout, '中立输入由操作者认定，不自动证明无表情')
        _lines(layout, '本块不证明全脸互斥或角色完整表情已完成')


_CLASSES = (C94FACE_ManualKeyOption, C94FACE_UL_ManualKeys,
            C94FACE_OT_CaptureNeutral, C94FACE_OT_Switch, C94FACE_OT_Visibility,
            C94FACE_OT_Restore, C94FACE_PT_Modes)


def register():
    if _REGISTERED:
        return
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
    except Exception:
        unregister()
        raise


def unregister():
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
