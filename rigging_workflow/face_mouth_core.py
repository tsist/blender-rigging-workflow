# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared daily 2D mouth pad: read, axes, sliders, layout, switch and record.

Creation, graphics and frame fitting remain in face_mouth_pad. The author UI
and the embedded daily Text call these same daily functions.
"""
import json
import math

import bpy

from . import face_expression_core as core, face_modes as fm, mode_animation as anim

STATE_KEY = 'c94_face_mouth_pad_v1'
SCHEMA = 'c94.face-mouth-pad.v1'
_require = core._require
_CELLS = {(x, y) for x in (-1, 0, 1) for y in (0, 1, 2)} - {(0, 0)}
_SLIDER_COLUMNS = 5
_SLIDER_ROW = .115
SLIDER_COUNT = 15
INPUT_COUNT = 2 + SLIDER_COUNT


def _path(bone):
    return fm.bone_location_path(bone)


def _grid(channels, rows):
    names = {row['name']: row['property'] for row in channels}
    _require(isinstance(rows, list) and len(rows) == 8, '口型网格需要明确8行')
    result = []
    for row in rows:
        _require(isinstance(row, dict) and set(row) == {'name', 'x', 'y'}
                 and isinstance(row['name'], str) and row['name'] in names
                 and type(row['x']) in (int, float) and type(row['y']) in (int, float)
                 and (row['x'], row['y']) in _CELLS, '口型网格名称或格点无效；(0,0)保留为Basis')
        result.append(dict(name=row['name'], property=names[row['name']], x=int(row['x']), y=int(row['y'])))
    _require(len({r['name'] for r in result}) == 8 and {(r['x'], r['y']) for r in result} == _CELLS,
             '口型网格必须不重复地覆盖3x3除(0,0)的8格')
    return result


def _require_standard_slider_count(count):
    # The daily mouth contract is fixed: 8 grid cells + 15 sliders = 17 raw
    # inputs. Do not invent a dynamic-N mode that would silently accept boards
    # this core cannot edit or record.
    _require(type(count) is int and count == SLIDER_COUNT,
             '口型板须为标准8格点+15滑杆能力；不支持其它滑杆数量')


def _expression(row, travel):
    x = 'min(1,max(-1,x/' + repr(travel) + '))'
    y = 'min(1,max(0,y/' + repr(travel) + '))'
    return 'max(0,1-abs(' + x + '-(' + str(row['x']) + ')))*max(0,1-abs(2*' + y + '-(' + str(row['y']) + ')))'


def _slider_expression(travel):
    return 'min(1,max(0,v/' + repr(travel) + '))'


def _grid_basis(group, grid, members):
    channels = {r['name']: r for r in group['channels']}
    for index, obj in enumerate(members):
        keys = obj.data.shape_keys
        for row in grid:
            key = keys.key_blocks[core._key_name(channels[row['name']], index)]
            _require(key != keys.reference_key and key.relative_key == keys.reference_key,
                     '二维口型格点必须直接相对Basis；不接管relative Key链：' + obj.name + '/' + row['name'])


def _no_target_animation(hub, paths, *, allow_owned_drivers=False):
    ad = hub.animation_data
    if ad is None:
        return
    _require(allow_owned_drivers or not any(f.data_path in paths for f in ad.drivers), 'hub参数已有driver，不接管')
    curves = fm._curves(ad.action, ad.action_slot) if ad.action else []
    def visit(strips):
        for strip in strips:
            if strip.action:
                curves.extend(fm._curves(strip.action, strip.action_slot))
            if strip.type == 'META':
                visit(strip.strips)
    for track in ad.nla_tracks:
        visit(track.strips)
    _require(not any(f.data_path in paths for f in curves), 'hub参数已有Action/NLA曲线，不迁移或覆盖')


def _read(hub):
    group, rig, members = core._read(hub)
    try:
        record = json.loads(hub[STATE_KEY])
        _require(isinstance(record, dict) and set(record) == {
            'schema', 'root', 'anchor', 'pad', 'collection', 'travel', 'grid', 'sliders'}
            and record['schema'] == SCHEMA and 'c94_face_graph_board_v1' not in hub, '口型板记录字段或版本无效')
        travel = record['travel']
        _require(type(travel) in (int, float) and math.isfinite(travel) and travel > 0, '口型板行程无效')
        grid = record['grid']
        _require(isinstance(grid, list) and all(isinstance(r, dict) and set(r) == {'name', 'property', 'x', 'y'} for r in grid),
                 '口型网格记录损坏')
        expected = _grid(group['channels'], [{k: r[k] for k in ('name', 'x', 'y')} for r in grid])
        _require(grid == expected, '口型网格property映射已改变')
        _grid_basis(group, grid, members)
        sliders = record['sliders']
        names = {r['name']: r['property'] for r in group['channels']}
        grid_names = {r['name'] for r in grid}
        _require(isinstance(sliders, list) and all(isinstance(r, dict) and set(r) == {'name', 'property', 'bone', 'anchor'}
                 and r['name'] in names and r['property'] == names[r['name']] for r in sliders)
                 and len({r['name'] for r in sliders}) == len(sliders)
                 and {r['name'] for r in sliders} == set(names) - grid_names, '独立滑杆未完整且唯一覆盖其余通道')
        _require_standard_slider_count(len(sliders))
        hierarchy = [(record['root'], '表情面板'), (record['anchor'], record['root']),
                     (record['pad'], record['anchor'])]
        hierarchy += [(r['anchor'], record['root']) for r in sliders]
        hierarchy += [(r['bone'], r['anchor']) for r in sliders]
        owned = [name for name, _ in hierarchy]
        _require(len(set(owned)) == len(owned), '口型板骨身份重复')
        collection = rig.data.collections_all.get(record['collection'])
        _require(collection is not None and collection.parent == rig.data.collections_all.get(fm.SPACES['panel'])
                 and set(b.name for b in collection.bones) == set(owned), '口型板子集合或骨成员已改变')
        for name, parent in hierarchy:
            _require(isinstance(name, str) and name in rig.pose.bones and parent in rig.data.bones
                     and not rig.data.bones[name].use_deform
                     and rig.data.bones[name].parent == rig.data.bones[parent], '口型板非变形父链已改变')
        display = hub.get('board_display')
        _require(isinstance(display, bpy.types.Collection) and display.library is None and display.users > 0,
                 '口型板显示集合ID已失效或不是本地集合')
        _require(not any(other is not hub and other.get('board_display') == display for other in bpy.data.objects),
                 '口型板显示集合被其它控制对象同时占用')
        owned_set = set(owned)
        display_objects = set(display.objects)
        widgets = {rig.pose.bones[name].custom_shape for name in owned
                   if rig.pose.bones[name].custom_shape is not None}
        for obj in display_objects:
            if obj in widgets or (obj.type == 'MESH' and obj.get('c94_board_art_v1')):
                continue
            _require(obj.parent == rig and obj.parent_type == 'BONE' and obj.parent_bone in owned_set,
                     '口型板显示集合含非本rig板骨层级的对象：' + obj.name)
        frame = rig.pose.bones[record['root']].custom_shape
        _require(frame is not None and frame in display_objects and frame.type == 'MESH'
                 and len(frame.data.vertices) == 4 and len(frame.data.edges) == 4,
                 '口型板显示集合未包含当前rig矩形帧widget，拒绝按旧全局名认领')
        drivers = list(hub.animation_data.drivers) if hub.animation_data else []
        for row in [*grid, *sliders]:
            is_grid = row['name'] in grid_names
            expression = _expression(row, travel) if is_grid else _slider_expression(travel)
            variables = [('x', _path(record['pad']) + '[0]'), ('y', _path(record['pad']) + '[1]')] if is_grid else [('v', _path(row['bone']) + '[1]')]
            matches = [f for f in drivers if f.data_path == core._path(row['property'])]
            _require(len(matches) == 1, '口型参数驱动缺失或重复')
            curve = matches[0]
            driver = curve.driver
            _require(curve.array_index == 0 and not curve.mute and not curve.modifiers
                     and not curve.keyframe_points and not curve.sampled_points and driver.type == 'SCRIPTED'
                     and not driver.use_self and driver.expression == expression and len(driver.variables) == len(variables),
                     '口型参数原生表达式或曲线已手改')
            for var, (name, path) in zip(driver.variables, variables):
                _require(var.name == name and var.type == 'SINGLE_PROP' and len(var.targets) == 1
                         and var.targets[0].id_type == 'OBJECT' and var.targets[0].id == rig
                         and var.targets[0].data_path == path, '口型驱动变量/ID/输入路径所有权已改变')
        _no_target_animation(hub, {core._path(r['property']) for r in group['channels']}, allow_owned_drivers=True)
        return group, record, rig, members
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('口型板记录或引用损坏；保留工程检查') from exc


def inspect(hub):
    group, record, rig, members = _read(hub)
    pb = rig.pose.bones[record['pad']]
    return dict(axes={'x': max(-1., min(1., pb.location.x / record['travel'])),
                      'y': max(0., min(1., pb.location.y / record['travel']))},
                grid=record['grid'], sliders=record['sliders'], root=record['root'], anchor=record['anchor'], pad=record['pad'],
                channels=group['channels'], values={r['name']: float(hub[r['property']]) for r in group['channels']},
                rig=rig.name, members=[o.name for o in members], mode=fm._mode(rig))


def _set_inputs(hub, group, rig, rows):
    core._baseline(rig, group['neutral_fingerprint'])
    _, record, current_rig, _ = _read(hub)
    _require(current_rig == rig, '口型目标骨架已改变')
    scope = {(r['path'], r['index']) for r in _input_rows(record, rig)}
    _require(len(scope) == INPUT_COUNT, '口型编辑须为当前板%d个明确输入' % INPUT_COUNT)
    anim._preflight(rig, anim._binding(rig, write_keys=scope), rows)
    before = anim._snapshot(rig)
    try:
        for row in rows:
            anim._restore_value(rig, row['path'], row['index'], row['value'])
        core._update(hub, rig)
        anim._check_inputs(rig, before, {(r['path'], r['index']) for r in rows})
        _require(all(anim._f32(anim._animated_value(rig, r['path'], r['index'])) == r['value'] for r in rows),
                 '更新后控件输入被动画或其它求值覆盖')
        return inspect(hub)
    except Exception as error:
        try:
            _restore_snapshot(rig, before)
            _refresh_restored(hub, rig)
            _require(anim._snapshot(rig) == before, '口型编辑失败后的全部输入恢复不一致')
        except Exception as recovery:
            raise RuntimeError(str(error) + '; 口型编辑恢复未完整核实：' + str(recovery)) from error
        raise


def _row(bone, index, value):
    return dict(path=_path(bone), index=index, value=anim._f32(value), interpolation='LINEAR')


def _refresh_restored(hub, rig):
    # The failed create may already have removed the pad marker. Avoid TIME
    # reapplying the restored Action over the operator's unkeyed manual input.
    hub.update_tag(refresh={'OBJECT'})
    rig.update_tag(refresh={'OBJECT', 'DATA'})
    bpy.context.view_layer.update()


def _restore_snapshot(rig, before):
    for n,v in before['bones'].items():
        pb=rig.pose.bones[n]
        for f in anim.pose._CHANNELS:setattr(pb,f,v[f])
        pb.rotation_mode=v['rotation_mode']
    for (n,k),v in before['modes'].items():rig.pose.bones[n][k]=v
    for f,v in before['object'].items():setattr(rig,f,v)
    for (p,i),v in before['animated'].items():anim._restore_value(rig,p,i,v)


def set_axes(hub, *, x, y):
    group, record, rig, _ = _read(hub)
    _require(type(x) in (int, float) and type(y) in (int, float) and math.isfinite(x) and math.isfinite(y)
             and -1 <= x <= 1 and 0 <= y <= 1, '二维输入须为有限X[-1,1]、Y[0,1]')
    return _set_inputs(hub, group, rig, [_row(record['pad'], 0, x * record['travel']), _row(record['pad'], 1, y * record['travel'])])


def set_axis(hub, *, axis, value):
    """Edit one raw input without normalizing or rewriting the other axis."""
    group, record, rig, _ = _read(hub)
    _require(axis in {'x', 'y'} and type(value) in (int, float) and math.isfinite(value)
             and (-1 if axis == 'x' else 0) <= value <= 1,
             '单轴输入须为有限X[-1,1]或Y[0,1]')
    return _set_inputs(hub, group, rig, [_row(record['pad'], 0 if axis == 'x' else 1, value * record['travel'])])


def set_values(hub, values):
    group, record, rig, _ = _read(hub)
    _require(isinstance(values, dict) and values and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in values.values()),
             '明确提供已有滑杆名称和0..1强度')
    names = {r['name'] for r in group['channels']}
    sliders = {r['name']: r['bone'] for r in record['sliders']}
    reset_all = set(values) == names and all(v == 0 for v in values.values())
    _require(reset_all or set(values) <= sliders.keys(), '二维口型输出不能任意反解；请用set_axes，或全部通道明确归0')
    rows = [_row(bone, 1, (0 if reset_all else values[name]) * record['travel'])
            for name, bone in sliders.items() if reset_all or name in values]
    if reset_all:
        rows += [_row(record['pad'], 0, 0), _row(record['pad'], 1, 0)]
    return _set_inputs(hub, group, rig, rows)


def set_layout(hub, *, index=None, location=None, scale=None):
    group, record, rig, _ = _read(hub)
    _require(index is None or type(index) is int and -1 <= index < len(record['sliders']), '布局索引应为None、-1或已有滑杆序号')
    name = record['root'] if index is None else record['anchor'] if index == -1 else record['sliders'][index]['anchor']
    pb = rig.pose.bones[name]
    values = {k: v for k, v in (('location', location), ('scale', scale)) if v is not None}
    _require(values and all(isinstance(v, (list, tuple)) and len(v) == 3
             and all(type(x) in (int, float) and math.isfinite(x) and (x > 0 if k == 'scale' else True) for x in v)
             for k, v in values.items()), '明确有限位置或正缩放')
    fm._static(rig, {pb.path_from_id(k) for k in values})
    rows = [dict(path=pb.path_from_id(k), index=i, value=anim._f32(v), interpolation='LINEAR')
            for k, vs in values.items() for i, v in enumerate(vs)]
    layout_keys = {(r['path'], r['index']) for r in rows}
    anim._preflight(rig, anim._binding(rig, write_keys=layout_keys), rows)
    before = anim._snapshot(rig)
    strengths = {r['property']: hub[r['property']] for r in group['channels']}
    try:
        for field, value in values.items(): setattr(pb, field, value)
        core._update(hub, rig)
        anim._check_inputs(rig, before, layout_keys)
        _require(all(anim._f32(anim._animated_value(rig, r['path'], r['index'])) == r['value'] for r in rows),
                 '布局输入被其它求值覆盖')
        _require(all(hub[p] == value for p, value in strengths.items()), '布局改变了参数强度')
        return dict(layout_updated=True, inputs_unchanged=True)
    except Exception as error:
        try:
            _restore_snapshot(rig, before)
            _refresh_restored(hub, rig)
            _require(anim._snapshot(rig) == before, '布局编辑失败后的全部输入恢复不一致')
        except Exception as recovery:
            raise RuntimeError(str(error) + '; 布局恢复未完整核实：' + str(recovery)) from error
        raise


def _input_rows(record, rig):
    return [_row(record['pad'], i, rig.pose.bones[record['pad']].location[i]) for i in (0, 1)] + [
        _row(r['bone'], 1, rig.pose.bones[r['bone']].location.y) for r in record['sliders']]


def switch(hub, *, target, reset=False):
    group, record, rig, _ = _read(hub)
    _require(target in {'KEY', 'BONE'} and type(reset) is bool, '明确模式和复位选项')
    rows = _input_rows(record, rig)
    fm._static(rig, {r['path'] for r in rows})
    _require(reset or not any(hub[r['property']] for r in group['channels']), '先归零参数或明确复位后切换')
    before = {(r['path'], r['index']): anim._animated_value(rig, r['path'], r['index']) for r in rows}
    try:
        if reset:
            for path, index in before: anim._restore_value(rig, path, index, 0)
            core._update(hub, rig)
        result = fm.switch(rig, target=target, reset=reset)
        core._update(hub, rig)
        return result
    except Exception:
        for (path, index), value in before.items(): anim._restore_value(rig, path, index, value)
        core._update(hub, rig)
        raise


def record_current(hub):
    group, record, rig, _ = _read(hub)
    core._baseline(rig, group['neutral_fingerprint'])
    scene = bpy.context.scene
    _require(scene.render.frame_map_old == scene.render.frame_map_new, '不支持场景时间重映射')
    raw = scene.frame_current + scene.frame_subframe
    frame, rows = anim._f32(raw), _input_rows(record, rig)
    pad = rig.pose.bones[record['pad']].location
    normalized = [(pad.x / record['travel'], -1, 1), (pad.y / record['travel'], 0, 1)]
    normalized += [(rig.pose.bones[r['bone']].location.y / record['travel'], 0, 1) for r in record['sliders']]
    _require(all(math.isfinite(v) and lower - 1e-6 <= v <= upper + 1e-6 for v, lower, upper in normalized),
             '原始pad/slider输入越界，拒绝将夹后显示值混记；端点仅容许1e-6漂移，保留原raw')
    _require(len(rows) == INPUT_COUNT and len({(r['path'], r['index']) for r in rows}) == INPUT_COUNT,
             '口型记录须为当前板%d个明确输入' % INPUT_COUNT)
    binding = anim._binding(rig, write_keys={(r['path'], r['index']) for r in rows})
    anim._preflight(rig, binding, rows)
    before = anim._snapshot(rig)
    fake = binding['action'].use_fake_user if binding['action'] else None
    action = None
    try:
        action, slot = anim._make_action(rig, binding, rows=rows)
        anim._write_rows(action, slot, rows, frame)
        ad = rig.animation_data_create()
        ad.action, ad.action_slot = action, slot
        if not binding['action']:
            ad.action_blend_type, ad.action_influence, ad.action_extrapolation = 'REPLACE', 1., 'HOLD'
        core._update(hub, rig)
        anim._check_inputs(rig, before, {(r['path'], r['index']) for r in rows})
        _require(all(anim._f32(anim._animated_value(rig, r['path'], r['index'])) == r['value'] for r in rows)
                 and scene.frame_current + scene.frame_subframe == raw, '插帧后输入/帧不一致')
        if binding['action']: binding['action'].use_fake_user = True
        return dict(action=action.name, slot=slot.identifier, frame=frame, channels=len(rows), original_action_retained=True,
                    raw_inputs_preserved=True, normalized_endpoint_tolerance=1e-6)
    except Exception as error:
        recovery_errors = []
        def attempt(label, operation):
            try: operation()
            except Exception as recovery: recovery_errors.append(label + ': ' + str(recovery))
        attempt('restore Action binding', lambda: anim._restore_binding(rig, binding))
        attempt('flush restored Action', lambda: core._update(hub, rig))
        attempt('restore actual inputs', lambda: _restore_snapshot(rig, before))
        if binding['action']:
            attempt('restore old fake user', lambda: setattr(binding['action'], 'use_fake_user', fake))
        attempt('refresh restored inputs without TIME', lambda: _refresh_restored(hub, rig))
        def verify_recovery():
            _require(anim._snapshot(rig) == before and scene.frame_current + scene.frame_subframe == raw,
                     '插帧失败后的输入或当前帧恢复不一致')
        attempt('verify recovered inputs', verify_recovery)
        if action is not None:
            def remove_action():
                _require(action.users == int(action.use_fake_user), '候选Action仍有真实消费者，已保留')
                bpy.data.actions.remove(action)
            attempt('remove unused candidate Action', remove_action)
        if recovery_errors:
            raise RuntimeError(str(error) + '; 插帧恢复未完整核实：' + '; '.join(recovery_errors)) from error
        raise
