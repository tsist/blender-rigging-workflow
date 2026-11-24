# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared daily graph-board read/values/layout/switch/record.

Creation and graphics remain in face_graph_board. This module keeps the
installed revision hub readable by the portable daily closure.
"""
import json
import math

import bpy

from . import face_expression_core as controls, face_modes as fm, mode_animation as anim

STATE_KEY = 'c94_face_graph_board_v1'
SCHEMA = 'c94.face-graph-board.v1'
_require = controls._require
_bone_path = fm.bone_location_path


def _read(hub):
    group, rig, members = controls._read(hub)
    try:
        board = json.loads(hub[STATE_KEY])
        _require(set(board) == {'schema', 'root', 'collection', 'travel', 'channels'}
                 and board['schema'] == SCHEMA, '图形板记录无效')
        _require(math.isfinite(board['travel']) and board['travel'] > 0
                 and len(board['channels']) == len(group['channels']), '图形板行程/通道无效')
        _require(board['root'] in rig.pose.bones and not rig.data.bones[board['root']].use_deform,
                 '图形板根缺失或已改为变形骨')
        drivers = list(hub.animation_data.drivers) if hub.animation_data else []
        for row, channel in zip(board['channels'], group['channels']):
            _require(set(row) == {'bone', 'anchor', 'section'}
                     and all(n in rig.pose.bones and not rig.data.bones[n].use_deform
                             for n in [row['bone'], row['anchor']]), '图形控件缺失/记录损坏')
            _require(rig.data.bones[row['bone']].parent == rig.data.bones[row['anchor']]
                     and rig.data.bones[row['anchor']].parent == rig.data.bones[board['root']],
                     '图形板层级已改变')
            fs = [f for f in drivers if f.data_path == controls._path(channel['property']) and f.array_index == 0]
            _require(len(fs) == 1 and not fs[0].mute and controls._identity_mapping(fs[0]),
                     '图形输入驱动缺失或已手改（含曲线映射）')
            d = fs[0].driver
            expression = 'min(1,max(0,v/' + repr(board['travel']) + '))'
            _require(d.type == 'SCRIPTED' and d.expression == expression and len(d.variables) == 1,
                     '图形输入表达式已手改')
            var = d.variables[0]
            _require(var.type == 'SINGLE_PROP' and var.name == 'v'
                     and var.targets[0].id == rig
                     and var.targets[0].data_path == _bone_path(row['bone']) + '[1]',
                     '图形输入目标已改变')
        return group, board, rig, members
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('图形板记录或引用损坏，保留工程检查') from exc


def _no_target_animation(hub, paths, *, allow_owned_drivers=False):
    from . import face_mouth_core
    return face_mouth_core._no_target_animation(hub, paths, allow_owned_drivers=allow_owned_drivers)


def inspect(hub):
    group, board, rig, members = _read(hub)
    return dict(channels=group['channels'],
                values={r['name']: float(hub[r['property']]) for r in group['channels']},
                rig=rig.name, members=[o.name for o in members], mode=fm._mode(rig),
                sliders=[dict(name=ch['name'], property=ch['property'], bone=row['bone'],
                              anchor=row['anchor'], section=row['section'])
                         for row, ch in zip(board['channels'], group['channels'])],
                board=board)


def set_values(hub, values):
    group, board, rig, _ = _read(hub)
    controls._baseline(rig, group['neutral_fingerprint'])
    names = {r['name']: i for i, r in enumerate(group['channels'])}
    _require(isinstance(values, dict) and values and set(values) <= names.keys()
             and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in values.values()),
             '明确已有表情与0..1强度')
    rows = [dict(path=_bone_path(board['channels'][names[n]]['bone']), index=1,
                 value=anim._f32(v * board['travel']), interpolation='LINEAR') for n, v in values.items()]
    anim._preflight(rig, anim._binding(rig), rows)
    before = {n: rig.pose.bones[board['channels'][names[n]]['bone']].location.y for n in values}
    try:
        for n, v in values.items():
            rig.pose.bones[board['channels'][names[n]]['bone']].location.y = v * board['travel']
        controls._update(hub, rig)
        return inspect(hub)
    except Exception:
        for n, v in before.items():
            rig.pose.bones[board['channels'][names[n]]['bone']].location.y = v
        controls._update(hub, rig)
        raise


def set_layout(hub, *, index=None, location=None, scale=None, section=None):
    group, board, rig, _ = _read(hub)
    _require(index is None or type(index) is int and 0 <= index < len(board['channels']), '布局索引无效')
    name = board['root'] if index is None else board['channels'][index]['anchor']
    pb = rig.pose.bones[name]
    values = {k: v for k, v in [('location', location), ('scale', scale)] if v is not None}
    _require(values or section is not None, '明确布局调整项')
    _require(all(isinstance(v, (list, tuple)) and len(v) == 3
                 and all(type(x) in (int, float) and math.isfinite(x) and (x > 0 if k == 'scale' else True)
                         for x in v) for k, v in values.items()), '布局位置/缩放无效')
    _require(section is None or index is not None and isinstance(section, str) and section.strip(),
             '分区仅用于指定通道')
    fm._static(rig, {pb.path_from_id(k) for k in values})
    before = {k: list(getattr(pb, k)) for k in values}
    old = hub[STATE_KEY]
    weights = inspect(hub)['values']
    try:
        for k, v in values.items():
            setattr(pb, k, v)
        if section is not None:
            board['channels'][index]['section'] = section
            hub[STATE_KEY] = json.dumps(board, ensure_ascii=False)
        controls._update(hub, rig)
        _require(inspect(hub)['values'] == weights, '布局改变了表情输入')
    except Exception:
        for k, v in before.items():
            setattr(pb, k, v)
        hub[STATE_KEY] = old
        controls._update(hub, rig)
        raise
    return {'layout_updated': True, 'inputs_unchanged': True}


def switch(hub, *, target, reset=False):
    group, board, rig, _ = _read(hub)
    _require(target in {'KEY', 'BONE'} and type(reset) is bool, '明确模式与复位选项')
    fm._static(rig, {_bone_path(r['bone']) for r in board['channels']})
    values = inspect(hub)['values']
    _require(reset or not any(values.values()), '先清零表情或明确复位后切换')
    before = {r['bone']: rig.pose.bones[r['bone']].location.y for r in board['channels']}
    try:
        if reset:
            for n in before:
                rig.pose.bones[n].location.y = 0
            controls._update(hub, rig)
        result = fm.switch(rig, target=target, reset=reset)
        controls._update(hub, rig)
        return result
    except Exception:
        for n, v in before.items():
            rig.pose.bones[n].location.y = v
        controls._update(hub, rig)
        raise


def _restore_inputs(rig, before):
    for n, v in before['bones'].items():
        pb = rig.pose.bones[n]
        for f in anim.pose._CHANNELS:
            setattr(pb, f, v[f])
        pb.rotation_mode = v['rotation_mode']
    for (n, k), v in before['modes'].items():
        rig.pose.bones[n][k] = v
    for f, v in before['object'].items():
        setattr(rig, f, v)
    for (p, i), v in before['animated'].items():
        anim._restore_value(rig, p, i, v)


def record_current(hub):
    group, board, rig, _ = _read(hub)
    controls._baseline(rig, group['neutral_fingerprint'])
    scene = bpy.context.scene
    _require(scene.render.frame_map_old == scene.render.frame_map_new, '不支持时间重映射')
    raw = scene.frame_current + scene.frame_subframe
    frame = anim._f32(raw)
    rows = [dict(path=_bone_path(r['bone']), index=1,
                 value=anim._f32(rig.pose.bones[r['bone']].location.y), interpolation='LINEAR')
            for r in board['channels']]
    binding = anim._binding(rig)
    anim._preflight(rig, binding, rows)
    before = anim._snapshot(rig)
    action = None
    fake = binding['action'].use_fake_user if binding['action'] else None
    try:
        action, slot = anim._make_action(rig, binding)
        anim._write_rows(action, slot, rows, frame)
        ad = rig.animation_data_create()
        ad.action, ad.action_slot = action, slot
        if not binding['action']:
            ad.action_blend_type, ad.action_influence, ad.action_extrapolation = 'REPLACE', 1., 'HOLD'
        controls._update(hub, rig)
        anim._check_inputs(rig, before, {(r['path'], 1) for r in rows})
        _require(all(anim._f32(rig.path_resolve(r['path'])[1]) == r['value'] for r in rows)
                 and scene.frame_current + scene.frame_subframe == raw, '记录后输入/帧不一致')
        if binding['action']:
            binding['action'].use_fake_user = True
        return dict(action=action.name, slot=slot.identifier, frame=frame, channels=len(rows),
                    original_action_retained=True)
    except Exception as error:
        recovery_errors = []
        def attempt(label, operation):
            try:
                operation()
            except Exception as recovery:
                recovery_errors.append(label + ': ' + str(recovery))
        attempt('恢复rig Action绑定', lambda: anim._restore_binding(rig, binding))
        attempt('刷新恢复后的旧Action', lambda: controls._update(hub, rig))
        attempt('恢复rig输入', lambda: _restore_inputs(rig, before))
        attempt('恢复旧fake user',
                lambda: setattr(binding['action'], 'use_fake_user', fake) if binding['action'] else None)
        attempt('移除本次候选Action',
                lambda: controls._remove_owned_action(action, recovery_errors))
        attempt('刷新恢复状态', lambda: controls._update(hub, rig))
        if recovery_errors:
            raise RuntimeError(str(error) + '；图形记录恢复未完整核实：' + '; '.join(recovery_errors)) from error
        raise
