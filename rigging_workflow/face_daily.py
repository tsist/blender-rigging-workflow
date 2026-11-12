# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin daily facade over the shared face cores; no second expression algorithm.

Used by face_daily_ui and the embedded portable Text. Authoring still imports
face_expression_controls / face_mouth_pad, which re-export these same cores.
"""
import bpy

from . import control_panel, face_expression_core as expression
from . import face_expression_layout as layout
from . import face_graph_core as graph, face_modes, face_mouth_core as mouth

_require = face_modes._require
MARKERS = (expression.STATE_KEY, mouth.STATE_KEY, 'c94_face_graph_board_v1')


def hub_rig(hub):
    _require(isinstance(hub, bpy.types.Object) and hub.type == 'EMPTY', '明确选择一个表情控制对象')
    rig = hub.get('rig')
    _require(isinstance(rig, bpy.types.Object) and rig.type == 'ARMATURE', '控制对象缺少真实rig引用')
    _require(any(key in hub for key in MARKERS), '对象不是已安装的面部控制')
    return rig


def resolve_hub(rig, hub):
    _require(isinstance(rig, bpy.types.Object) and rig.type == 'ARMATURE', '先激活一个角色骨架')
    _require(hub_rig(hub) == rig, '控制对象属于其它角色；不跨角色写入')
    kind = expression._control_type(hub)
    _require(kind in {'MOUTH', 'GRAPH', 'HUB'}, '本便携切片覆盖已安装口型板/图形板/普通表情控制')
    return kind


def hubs_for_rig(rig):
    return sorted((obj for obj in bpy.data.objects if obj.type == 'EMPTY'
                   and obj.get('rig') == rig and any(key in obj for key in MARKERS)),
                  key=lambda obj: obj.name)


def inspect(rig, hub):
    kind = resolve_hub(rig, hub)
    if kind == 'MOUTH':
        state = mouth.inspect(hub)
        state['kind'] = 'MOUTH'
    elif kind == 'GRAPH':
        state = graph.inspect(hub)
        state.update(kind='GRAPH', axes=None, grid=[])
    else:
        state = expression.inspect(hub)
        state.update(kind='HUB', axes=None, grid=[], sliders=[
            {'name': r['name'], 'property': r['property']} for r in state['channels']])
    state['layout'] = layout.read(hub)
    state['mode_state'] = face_modes.inspect(rig)
    return state


def set_axis(rig, hub, *, axis, value):
    kind = resolve_hub(rig, hub)
    _require(kind == 'MOUTH', '只有二维口型板有轴输入')
    return mouth.set_axis(hub, axis=axis, value=value)


def set_axes(rig, hub, *, x, y):
    kind = resolve_hub(rig, hub)
    _require(kind == 'MOUTH', '只有二维口型板有轴输入')
    return mouth.set_axes(hub, x=x, y=y)


def set_value(rig, hub, channel, value):
    kind = resolve_hub(rig, hub)
    if kind == 'MOUTH':
        names = {r['name'] for r in mouth.inspect(hub)['sliders']}
        _require(channel in names, '二维格点通道不能任意反解；请用单轴/格点输入')
        return mouth.set_values(hub, {channel: value})
    if kind == 'GRAPH':
        return graph.set_values(hub, {channel: value})
    return expression.set_values(hub, {channel: value})


def reset(rig, hub):
    kind = resolve_hub(rig, hub)
    state = mouth.inspect(hub) if kind == 'MOUTH' else graph.inspect(hub) if kind == 'GRAPH' else expression.inspect(hub)
    names = [r['name'] for r in state['channels']]
    _require(names, '控制对象没有表情通道')
    if kind == 'MOUTH':
        return mouth.set_values(hub, {name: 0.0 for name in names})
    if kind == 'GRAPH':
        return graph.set_values(hub, {name: 0.0 for name in names})
    return expression.set_values(hub, {name: 0.0 for name in names})


def record(rig, hub):
    resolve_hub(rig, hub)
    return expression.record_current(hub)


def switch(rig, hub, *, target, reset_inputs=False):
    resolve_hub(rig, hub)
    return expression.switch(hub, target=target, reset=reset_inputs)


def set_mode(rig, *, target, reset=False):
    return face_modes.switch(rig, target=target, reset=reset)


def set_visibility(rig, **values):
    return face_modes.set_visibility(rig, **values)


def restore_previous(rig):
    return face_modes.restore_previous(rig)


def capture_neutral(rig, *, manual_keys=None):
    return face_modes.capture_neutral(rig, manual_keys=manual_keys)


def manual_key_options(rig):
    return face_modes.manual_key_options(rig)


def migrate_identity(rig):
    return face_modes.migrate_legacy_identity(rig)


def mode_state(rig):
    return face_modes.inspect(rig)


def read_layout(hub):
    return layout.read(hub)


def configure_layout(hub, **values):
    return layout.configure(hub, **values)


def current_rig(context):
    return control_panel.current_rig(context)
