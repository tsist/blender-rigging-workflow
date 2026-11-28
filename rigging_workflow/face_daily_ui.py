# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight daily face sidebar over face_daily shared cores.

This UI is intentionally limited to installed daily operations: mode/visibility,
2D mouth axes, independent sliders, reset, record and sidebar layout. It does
not create boards, bones, samples or solve physics.
"""
import json

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty
from bpy.props import IntProperty, PointerProperty, StringProperty

from . import control_panel, face_daily

OPTIONS = 'c94_face_daily_options_v1'
_REGISTERED = []
_ERRORS = (ValueError, RuntimeError, ReferenceError, KeyError, TypeError, AttributeError, IndexError)
_VISIBILITY = (('panel', '表情面板'), ('basic', '基础面部'), ('main', '面部主控'), ('secondary', '面部次要'))


def _button(layout, operator, rig, hub, *, text, **values):
    options = {key: values.pop(key) for key in tuple(values) if key in {'icon', 'depress'}}
    button = layout.operator(operator, text=text, **options)
    _bind_identity(button, rig, hub)
    for key, value in values.items():
        setattr(button, key, value)
    return button


def _hub_group(hub):
    raw = hub.get('c94_face_expression_controls_v1') if hub is not None else None
    try:
        return str(json.loads(raw).get('group_id', '')) if isinstance(raw, str) else ''
    except (TypeError, ValueError):
        return ''


def _bind_identity(owner, rig, hub=None):
    owner.rig_uid = str(rig.session_uid)
    owner.rig_data_uid = str(rig.data.session_uid)
    owner.rig_ptr = str(rig.as_pointer())
    owner.rig_data_ptr = str(rig.data.as_pointer())
    owner.rig_name = rig.name
    if hub is not None:
        owner.hub_uid = str(hub.session_uid)
        owner.hub_ptr = str(hub.as_pointer())
        owner.hub_group = _hub_group(hub)
    else:
        owner.hub_uid = ''
        owner.hub_ptr = ''
        owner.hub_group = ''
    return owner


def _rig_from_props(owner, context):
    uid = str(getattr(owner, 'rig_uid', '') or '')
    if uid:
        matches = [obj for obj in bpy.data.objects
                   if obj.type == 'ARMATURE' and str(getattr(obj, 'session_uid', '')) == uid]
        if len(matches) != 1:
            raise ValueError('按钮绑定的角色会话ID已失效；不按名称或地址回退')
        rig = matches[0]
        if rig.name not in context.scene.objects:
            raise ValueError('按钮绑定的角色不在当前Scene；不跨Scene回退')
        data_uid = str(getattr(owner, 'rig_data_uid', '') or '')
        if data_uid and str(getattr(rig.data, 'session_uid', '')) != data_uid:
            raise ValueError('按钮绑定的角色数据会话ID已改变；不按名称回退')
        return control_panel._managed(rig)
    pointer = str(getattr(owner, 'rig_ptr', '') or '')
    if pointer:
        matches = [obj for obj in bpy.data.objects
                   if obj.type == 'ARMATURE' and str(obj.as_pointer()) == pointer]
        if len(matches) != 1:
            raise ValueError('按钮绑定的角色已不存在；请重新打开面板')
        rig = matches[0]
        if rig.name not in context.scene.objects:
            raise ValueError('按钮绑定的角色不在当前Scene；不跨Scene回退')
        data_pointer = str(getattr(owner, 'rig_data_ptr', '') or '')
        if data_pointer and str(rig.data.as_pointer()) != data_pointer:
            raise ValueError('按钮绑定的角色数据身份已改变；请重新打开面板')
        return control_panel._managed(rig)
    return face_daily.current_rig(context)


def _hub_from_props(owner):
    uid = str(getattr(owner, 'hub_uid', '') or '')
    if uid:
        matches = [obj for obj in bpy.data.objects
                   if obj.type == 'EMPTY' and str(getattr(obj, 'session_uid', '')) == uid]
        if len(matches) != 1:
            raise ValueError('按钮绑定的面部控制会话ID已失效；不按名称或地址回退')
        hub = matches[0]
        expected = getattr(owner, 'hub_group', '')
        if expected and _hub_group(hub) != expected:
            raise ValueError('按钮绑定的面部控制身份已改变；不按旧名称认领')
        return hub
    pointer = str(getattr(owner, 'hub_ptr', '') or '')
    if not pointer:
        return None
    matches = [obj for obj in bpy.data.objects
               if obj.type == 'EMPTY' and str(obj.as_pointer()) == pointer]
    if len(matches) != 1:
        raise ValueError('按钮绑定的面部控制已不存在；请重新打开面板')
    hub = matches[0]
    expected = getattr(owner, 'hub_group', '')
    if expected and _hub_group(hub) != expected:
        raise ValueError('按钮绑定的面部控制身份已改变；不按旧名称认领')
    return hub


class C94DAILY_SliderRow(bpy.types.PropertyGroup):
    hub: PointerProperty(type=bpy.types.Object)
    channel: StringProperty()
    prop_key: StringProperty()
    strength: FloatProperty(name='强度', min=0., max=1.,
                            get=lambda self: _strength_get(self),
                            set=lambda self, value: _strength_set(self, value))


def _mouth_axis_get(opts, axis):
    try:
        if opts.rig is None or opts.hub is None:
            return 0.
        return float(face_daily.inspect(opts.rig, opts.hub)['axes'][axis])
    except _ERRORS:
        return 0.


def _mouth_axis_set(opts, axis, value):
    try:
        face_daily.set_axis(opts.rig, opts.hub, axis=axis, value=value)
        opts.message = ''
    except _ERRORS as exc:
        opts.message = str(exc)


def _strength_get(row):
    try:
        return float(row.hub[row.prop_key])
    except (KeyError, TypeError, ReferenceError):
        return 0.


def _strength_set(row, value):
    try:
        opts = getattr(row.id_data, OPTIONS)
        face_daily.set_value(opts.rig, row.hub, row.channel, value)
        opts.message = ''
    except _ERRORS as exc:
        getattr(row.id_data, OPTIONS).message = str(exc)


def _refresh(opts):
    opts.channels.clear()
    if opts.rig is None or opts.hub is None:
        return
    try:
        state = face_daily.inspect(opts.rig, opts.hub)
        rows = state['sliders']
        for row in rows:
            item = opts.channels.add()
            item.hub = opts.hub
            item.channel = row['name']
            item.prop_key = row['property']
        opts.message = ''
    except _ERRORS as exc:
        opts.message = str(exc)


def _hub_update(opts, context):
    _refresh(opts)


class C94DAILY_Options(bpy.types.PropertyGroup):
    rig: PointerProperty(name='角色骨架', type=bpy.types.Object,
                         poll=lambda self, obj: obj.type == 'ARMATURE',
                         update=lambda self, context: _refresh(self))
    hub: PointerProperty(name='面部控制', type=bpy.types.Object,
                         poll=lambda self, obj: obj.type == 'EMPTY' and any(
                             key in obj for key in face_daily.MARKERS),
                         update=_hub_update)
    channels: CollectionProperty(type=C94DAILY_SliderRow)
    layout_editing: BoolProperty(name='编辑侧栏布局', default=False)
    mouth_x: FloatProperty(name='圆口 ↔ 宽口', min=-1., max=1.,
                           get=lambda self: _mouth_axis_get(self, 'x'),
                           set=lambda self, value: _mouth_axis_set(self, 'x', value))
    mouth_y: FloatProperty(name='开口', min=0., max=1.,
                           get=lambda self: _mouth_axis_get(self, 'y'),
                           set=lambda self, value: _mouth_axis_set(self, 'y', value))
    message: StringProperty(options={'SKIP_SAVE'})


class _DailyOperator:
    rig_uid: StringProperty(options={'HIDDEN'})
    rig_data_uid: StringProperty(options={'HIDDEN'})
    rig_ptr: StringProperty(options={'HIDDEN'})
    rig_data_ptr: StringProperty(options={'HIDDEN'})
    rig_name: StringProperty(options={'HIDDEN'})
    hub_uid: StringProperty(options={'HIDDEN'})
    hub_ptr: StringProperty(options={'HIDDEN'})
    hub_group: StringProperty(options={'HIDDEN'})

    def resolve(self, context):
        hub = _hub_from_props(self)
        if hub is not None:
            if hub.name not in context.scene.objects:
                raise ValueError('按钮绑定的面部控制不在当前Scene；不跨Scene回退')
            rig = face_daily.hub_rig(hub)
            if rig.name not in context.scene.objects:
                raise ValueError('按钮绑定的角色不在当前Scene；不跨Scene回退')
            uid = str(getattr(self, 'rig_uid', '') or '')
            if uid and str(rig.session_uid) != uid:
                raise ValueError('按钮的角色与表情控制会话ID不一致；不跨角色写入')
            data_uid = str(getattr(self, 'rig_data_uid', '') or '')
            if data_uid and str(rig.data.session_uid) != data_uid:
                raise ValueError('按钮的角色数据会话ID已改变；不跨角色写入')
            pointer = str(getattr(self, 'rig_ptr', '') or '')
            if pointer and str(rig.as_pointer()) != pointer:
                raise ValueError('按钮的角色与表情控制不属于同一角色；不跨角色写入')
            face_daily.resolve_hub(rig, hub)
            _bind_identity(self, rig, hub)
            return rig, hub
        rig = _rig_from_props(self, context)
        opts = getattr(context.scene, OPTIONS)
        if opts.hub is not None:
            try:
                if face_daily.hub_rig(opts.hub) == rig:
                    if opts.hub.name not in context.scene.objects:
                        raise ValueError('选中的面部控制不在当前Scene；不跨Scene回退')
                    face_daily.resolve_hub(rig, opts.hub)
                    _bind_identity(self, rig, opts.hub)
                    return rig, opts.hub
            except _ERRORS:
                pass
        hubs = face_daily.hubs_for_rig(rig)
        hubs = [hub for hub in hubs if hub.name in context.scene.objects]
        if len(hubs) != 1:
            raise ValueError('当前角色有多个面部控制；请在面板明确选择')
        face_daily.resolve_hub(rig, hubs[0])
        _bind_identity(self, rig, hubs[0])
        return rig, hubs[0]

    def run(self, context, operation, message):
        try:
            rig, hub = self.resolve(context)
            result = operation(rig, hub)
            self.report({'INFO'}, message)
            return {'FINISHED'}, result
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}, None


class C94DAILY_OT_Bind(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_bind'
    bl_label = '绑定当前角色面部控制'
    bl_description = '只在当前角色自己的控制对象中选择；不跨角色认领'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        try:
            rig = _rig_from_props(self, context)
            hub = _hub_from_props(self)
            if hub is not None and hub.name not in context.scene.objects:
                raise ValueError('按钮绑定的面部控制不在当前Scene；不跨Scene回退')
            if hub is None and getattr(context.scene, OPTIONS).hub is not None:
                candidate = getattr(context.scene, OPTIONS).hub
                try:
                    if candidate.name in context.scene.objects and face_daily.hub_rig(candidate) == rig:
                        hub = candidate
                except _ERRORS:
                    hub = None
            if hub is None:
                candidates = [obj for obj in face_daily.hubs_for_rig(rig)
                              if obj.name in context.scene.objects and 'c94_face_mouth_pad_v1' in obj]
                if not candidates:
                    candidates = [obj for obj in face_daily.hubs_for_rig(rig)
                                  if obj.name in context.scene.objects]
                if not candidates:
                    raise ValueError('当前角色没有已安装面部控制')
                hub = max(candidates, key=lambda obj: (len(face_daily.expression._read(obj)[0]['channels'])
                                                       if 'c94_face_expression_controls_v1' in obj else 0, obj.name))
            face_daily.resolve_hub(rig, hub)
            opts = getattr(context.scene, OPTIONS)
            opts.rig = rig
            opts.hub = hub
            _bind_identity(self, rig, hub)
            _refresh(opts)
            self.report({'INFO'}, '已绑定 ' + hub.name)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_Mode(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_mode'
    bl_label = '安全切换面部模式'
    bl_description = '只有中立/明确复位时切换；冲突时拒绝，不自动匹配动画'
    bl_options = {'REGISTER', 'UNDO'}
    target: EnumProperty(items=(('KEY', '形态键', ''), ('BONE', '骨控', '')))
    reset: BoolProperty(name='先复位已记录面部输入', default=False)
    follow_visibility: BoolProperty(name='同步控件空间显隐', default=True)

    def execute(self, context):
        try:
            rig = self.resolve(context)[0]
            face_daily.set_mode(rig, target=self.target, reset=self.reset)
            self.report({'INFO'}, '已切换至' + self.target)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_Restore(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_restore'
    bl_label = '恢复上次面部切换'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            rig = self.resolve(context)[0]
            face_daily.restore_previous(rig)
            self.report({'INFO'}, '已恢复上次面部切换')
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_Visibility(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_visibility'
    bl_label = '面部控件集合显隐'
    bl_options = {'REGISTER', 'UNDO'}
    collection: EnumProperty(items=tuple((key, label, '') for key, label in _VISIBILITY))
    visible: BoolProperty(default=True)

    def execute(self, context):
        try:
            rig = self.resolve(context)[0]
            face_daily.set_visibility(rig, **{self.collection: self.visible})
            self.report({'INFO'}, '已更新面部控件显隐')
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_SetAxis(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_axis'
    bl_label = '设置二维口型单轴'
    bl_description = '只改指定轴，保留另一轴与独立滑杆原值'
    bl_options = {'REGISTER', 'UNDO'}
    axis: EnumProperty(items=(('x', 'X', ''), ('y', 'Y', '')))
    value: FloatProperty(min=-1., max=1.)

    def execute(self, context):
        try:
            rig, hub = self.resolve(context)
            face_daily.set_axis(rig, hub, axis=self.axis, value=self.value)
            self.report({'INFO'}, '已设置二维口型' + self.axis)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_SetAxes(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_axes'
    bl_label = '设置二维口型位置'
    bl_options = {'REGISTER', 'UNDO'}
    x: FloatProperty(min=-1., max=1.)
    y: FloatProperty(min=0., max=1.)

    def execute(self, context):
        try:
            rig, hub = self.resolve(context)
            face_daily.set_axes(rig, hub, x=self.x, y=self.y)
            self.report({'INFO'}, '已设置二维口型位置')
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_SetSlider(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_slider'
    bl_label = '设置独立表情滑杆'
    bl_options = {'REGISTER', 'UNDO'}
    channel: StringProperty()
    value: FloatProperty(min=0., max=1.)

    def execute(self, context):
        try:
            rig, hub = self.resolve(context)
            face_daily.set_value(rig, hub, self.channel, self.value)
            self.report({'INFO'}, '已设置 ' + self.channel)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_Reset(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_reset'
    bl_label = '清零全部表情'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            rig, hub = self.resolve(context)
            face_daily.reset(rig, hub)
            self.report({'INFO'}, '已清零全部表情')
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_Record(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_record'
    bl_label = '记录当前帧全部表情'
    bl_description = '记录到复制的动作副本；旧动作曲线保留，不自动补前帧'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            rig, hub = self.resolve(context)
            result = face_daily.record(rig, hub)
            self.report({'INFO'}, '已记录帧 ' + str(result['frame']))
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_Migrate(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_migrate'
    bl_label = '显式迁移面部中立身份'
    bl_description = '旧名称记录只有唯一可证明时迁移为真实物体/形态键 ID；歧义拒绝'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            rig = self.resolve(context)[0]
            result = face_daily.migrate_identity(rig)
            self.report({'INFO'}, '已迁移' if result.get('migrated') else '已是新身份记录')
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94DAILY_OT_Layout(_DailyOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.face_daily_layout'
    bl_label = '编辑表情侧栏行'
    bl_options = {'REGISTER', 'UNDO'}
    channel: StringProperty(options={'HIDDEN'})
    action: EnumProperty(items=(('UP', '上移', ''), ('DOWN', '下移', ''),
                                ('EDIT', '标签/分区', ''), ('VISIBLE', '显隐', ''),
                                ('COLUMNS', '列数', '')))
    label: StringProperty(name='显示名称')
    section: StringProperty(name='分区')
    columns: IntProperty(name='列数', default=1, min=1, max=3)

    def invoke(self, context, event):
        if self.action != 'EDIT':
            return self.execute(context)
        try:
            rig, hub = self.resolve(context)
            _bind_identity(self, rig, hub)
            record = face_daily.read_layout(hub)
            row = next(r for r in record['rows'] if r['channel'] == self.channel)
            self.label, self.section = row['label'], row['section']
            return context.window_manager.invoke_props_dialog(self)
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

    def draw(self, context):
        self.layout.prop(self, 'label')
        self.layout.prop(self, 'section')

    def execute(self, context):
        try:
            rig, hub = self.resolve(context)
            if self.action == 'COLUMNS':
                face_daily.configure_layout(hub, columns=self.columns)
            elif self.action == 'EDIT':
                face_daily.configure_layout(hub, channel=self.channel, label=self.label, section=self.section)
            elif self.action == 'VISIBLE':
                record = face_daily.read_layout(hub)
                row = next(r for r in record['rows'] if r['channel'] == self.channel)
                face_daily.configure_layout(hub, channel=self.channel, visible=not row['visible'])
            elif self.action in {'UP', 'DOWN'}:
                record = face_daily.read_layout(hub)
                index = next(i for i, r in enumerate(record['rows']) if r['channel'] == self.channel)
                position = max(0, min(len(record['rows']) - 1, index + (-1 if self.action == 'UP' else 1)))
                face_daily.configure_layout(hub, channel=self.channel, position=position)
            else:
                raise ValueError('未知布局操作')
            self.report({'INFO'}, '已更新表情侧栏布局')
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _lines(layout, text):
    for line in str(text).splitlines()[:4]:
        layout.label(text=line[:120])


class C94DAILY_PT_Face(bpy.types.Panel):
    bl_idname = 'C94DAILY_PT_face'
    bl_label = '日常面部控制'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '面部'
    bl_order = 2

    @classmethod
    def poll(cls, context):
        try:
            face_daily.current_rig(context)
            return True
        except _ERRORS:
            return False

    def draw(self, context):
        layout = self.layout
        try:
            rig = face_daily.current_rig(context)
        except _ERRORS as exc:
            _lines(layout, str(exc))
            return
        opts = getattr(context.scene, OPTIONS)
        layout.label(text='角色：' + rig.name, icon='ARMATURE_DATA')
        if opts.rig != rig or opts.hub is None:
            layout.label(text='尚未绑定当前角色的面部控制', icon='INFO')
            _button(layout, 'c94_workflow.face_daily_bind', rig, None, text='绑定当前角色主控制')
            layout.prop(opts, 'hub', text='面部控制')
            return
        try:
            state = face_daily.inspect(rig, opts.hub)
        except _ERRORS as exc:
            _lines(layout, str(exc))
            layout.operator('c94_workflow.face_daily_bind', text='重新绑定')
            return
        mode_state = state['mode_state']
        if mode_state.get('needs_migration'):
            _button(layout, 'c94_workflow.face_daily_migrate', rig, opts.hub, text='显式迁移面部中立身份')
            _lines(layout, '迁移只证明唯一身份并保留中立数值；歧义拒绝')
            return
        layout.label(text='控制：' + opts.hub.name + ' · ' + state['kind'])
        layout.label(text='模式：' + ('形态键' if mode_state['mode'] == 'KEY' else '骨控'))
        if mode_state['conflicts']:
            box = layout.box()
            box.label(text='输入冲突 ' + str(len(mode_state['conflicts'])) + ' 项', icon='ERROR')
            for item in mode_state['conflicts'][:3]:
                _lines(box, item)
        else:
            layout.label(text='输入与所记中立一致', icon='INFO')
        line = layout.row(align=True)
        for target, label in (('KEY', '安全切换→Key'), ('BONE', '安全切换→骨控')):
            _button(line, 'c94_workflow.face_daily_mode', rig, opts.hub, text=label, target=target)
        line = layout.row(align=True)
        for target, label in (('KEY', '复位→Key'), ('BONE', '复位→骨控')):
            _button(line, 'c94_workflow.face_daily_mode', rig, opts.hub, text=label, target=target, reset=True)
        vis = layout.box()
        vis.label(text='三级控件显隐（只改显示）')
        names = mode_state['visibility']
        for key, label in _VISIBILITY:
            name = face_daily.face_modes.SPACES[key]
            visible = bool(names.get(name, False))
            _button(vis, 'c94_workflow.face_daily_visibility', rig, opts.hub, text=label,
                    collection=key, visible=not visible, depress=visible,
                    icon='HIDE_OFF' if visible else 'HIDE_ON')
        _button(layout, 'c94_workflow.face_daily_restore', rig, opts.hub, text='恢复上次切换')
        if state['kind'] == 'MOUTH':
            mouth = layout.box()
            mouth.label(text='二维口型（单轴保留另一轴）')
            mouth.prop(opts, 'mouth_x', slider=True)
            mouth.prop(opts, 'mouth_y', slider=True)
            grid = {(r['x'], r['y']): r for r in state['grid']}
            labels = {r['channel']: r for r in state['layout']['rows']}
            for y in (2, 1, 0):
                row = mouth.row(align=True)
                for x in (-1, 0, 1):
                    cell = grid.get((x, y))
                    if cell is None:
                        _button(row, 'c94_workflow.face_daily_axes', rig, opts.hub, text='中立', x=0, y=0)
                    elif labels.get(cell['name'], {}).get('visible', True):
                        _button(row, 'c94_workflow.face_daily_axes', rig, opts.hub,
                                text=labels.get(cell['name'], {}).get('label', cell['name']), x=x, y=y / 2)
                    else:
                        row.label(text='')
        if len(opts.channels) != len(state['sliders']):
            layout.operator('c94_workflow.face_daily_bind', text='刷新滑杆列表')
        else:
            sliders = layout.box()
            sliders.label(text='独立滑杆')
            labels = {r['channel']: r for r in state['layout']['rows']}
            for row in opts.channels:
                if labels.get(row.channel, {}).get('visible', True):
                    sliders.prop(row, 'strength', text=labels.get(row.channel, {}).get('label', row.channel), slider=True)
        line = layout.row(align=True)
        _button(line, 'c94_workflow.face_daily_reset', rig, opts.hub, text='清零全部表情')
        _button(line, 'c94_workflow.face_daily_record', rig, opts.hub, text='记录当前帧')
        workspace = getattr(context.scene, 'c94_workspace_options_v1', None)
        advanced = workspace is None or workspace.level != 'DAILY'
        if advanced:
            layout.prop(opts, 'layout_editing')
        if advanced and opts.layout_editing:
            line = layout.row(align=True)
            for columns in (1, 2, 3):
                _button(line, 'c94_workflow.face_daily_layout', rig, opts.hub, text=str(columns) + '列',
                        action='COLUMNS', columns=columns, depress=state['layout']['columns'] == columns)
            for declared in state['layout']['rows']:
                line = layout.row(align=True)
                line.label(text=declared['label'])
                for action, icon in (('UP', 'TRIA_UP'), ('DOWN', 'TRIA_DOWN'), ('EDIT', 'GREASEPENCIL'),
                                     ('VISIBLE', 'HIDE_OFF' if declared['visible'] else 'HIDE_ON')):
                    _button(line, 'c94_workflow.face_daily_layout', rig, opts.hub, text='', icon=icon,
                            action=action, channel=declared['channel'])
        if opts.message:
            _lines(layout, opts.message)
        _lines(layout, '旧动作保留；记录只写当前帧。')


_CLASSES = (C94DAILY_SliderRow, C94DAILY_Options, C94DAILY_OT_Bind, C94DAILY_OT_Mode,
            C94DAILY_OT_Restore, C94DAILY_OT_Visibility, C94DAILY_OT_SetAxis, C94DAILY_OT_SetAxes,
            C94DAILY_OT_SetSlider, C94DAILY_OT_Reset, C94DAILY_OT_Record, C94DAILY_OT_Migrate,
            C94DAILY_OT_Layout, C94DAILY_PT_Face)


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
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94DAILY_Options))
        _C94_OPTIONS_OWNED = True
    except Exception:
        unregister()
        raise


def unregister():
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
            and prop.fixed_type == C94DAILY_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
