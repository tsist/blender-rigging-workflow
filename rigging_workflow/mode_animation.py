# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit current-frame recording on isolated native Action candidates.

No frame changes, automatic previous-frame keys, NLA edits or rest edits.
The pure matching API remains unkeyed. Old curves are never edited/deleted;
successful replacement retains the old Action with Blender's fake-user flag.
"""
import json
import math
import re
import struct

import bpy

from . import mode_semantics, pose_switching as pose

_AD_FIELDS = ('action_blend_type', 'action_influence', 'action_extrapolation', 'use_nla')
_DISCRETE = {'左臂FK-IK', '右臂FK-IK', '左腿FK-IK', '右腿FK-IK',
             '肩膀IK跟随', '脸部控制形态键-骨骼', '眼部父级切换', '前发物理', '后发物理'}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _f32(value):
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


def _property_path(bone, key):
    return bone.path_from_id() + '[' + json.dumps(key, ensure_ascii=False) + ']'


def _property_rows(rig, properties):
    available = {(r['bone'], r['property']) for r in mode_semantics.available(rig)}
    requested = sorted(available) if properties is None else list(properties)
    _require(requested and all(isinstance(p, (tuple, list)) and len(p) == 2
                             and all(isinstance(x, str) for x in p) for p in requested), '明确选择要记录的原生模式属性')
    requested = [tuple(p) for p in requested]
    _require(len(set(requested)) == len(requested) and set(requested) <= available,
             '属性重复、缺失或不属于当前角色的原生控制')
    rows = []
    for bone, key in requested:
        pb = rig.pose.bones[bone]; value = pb[key]
        _require(type(value) in (bool, int, float) and math.isfinite(value), '模式属性必须为有限数值')
        ui = pb.id_properties_ui(key).as_dict()
        _require(ui.get('min', -math.inf) <= value <= ui.get('max', math.inf), '模式属性超出原生范围：' + key)
        rows.append({'path': _property_path(pb, key), 'index': 0, 'value': _f32(value),
                     'interpolation': 'CONSTANT' if key in _DISCRETE or type(value) in (int, bool) else 'LINEAR'})
    return rows


def _limb_scope(rig, limb, side):
    _require(limb in {'ARM', 'LEG'} and side in {'L', 'R'}, '请选择手臂/腿及左/右侧')
    word = '左' if side == 'L' else '右'
    if limb == 'ARM':
        s, _, _ = pose._arm(rig, side)
        names = [*s['fk'], s['target'], s['main_target'], s['pole'],
                 *(p[0] for p in s['fingers']), *(f + '.' + side for f in pose.FINGERS), '肩膀.' + side]
        properties = [('骨架设置', s['mode_key']), ('骨架设置', word + '臂跟随'),
                      ('骨架设置', '肩膀IK跟随'), *((p[0], '手指IK') for p in s['fingers'])]
    else:
        from . import pose_switching_leg as leg
        s, _, _ = leg._validate(rig, side)
        names = [*s['fk'], s['main'], s['pole'], s['foot'], s['toe_ik'], s['heel_ik'], s['toe'], s['roll'],
                 *(p.name for p in rig.pose.bones if p.name.endswith('.' + side) and p.name.startswith(leg._TOES))]
        properties = [('骨架设置', s['key']), ('骨架设置', word + '腿跟随'), ('骨架设置', s['connect'])]
    _require(all(name in rig.pose.bones for name in names), '本肢体的原生输入不完整')
    return list(dict.fromkeys(names)), properties


def _rows(rig, names, properties):
    rows = _property_rows(rig, properties) if properties != [] else []
    for name in names:
        pb = rig.pose.bones[name]
        rotation = 'rotation_quaternion' if pb.rotation_mode == 'QUATERNION' else 'rotation_axis_angle' if pb.rotation_mode == 'AXIS_ANGLE' else 'rotation_euler'
        for field in ('location', rotation, 'scale'):
            for index, value in enumerate(getattr(pb, field)):
                _require(math.isfinite(value), '肢体输入包含非有限值')
                rows.append({'path': pb.path_from_id(field), 'index': index,
                             'value': _f32(value), 'interpolation': 'LINEAR'})
    return rows


def _bag(action, slot, ensure=False):
    _require(slot is not None and slot in action.slots[:] and slot.target_id_type == 'OBJECT', '活动动作没有明确的OBJECT Slot')
    _require(len(action.layers) == 1 and len(action.layers[0].strips) == 1
             and action.layers[0].strips[0].type == 'KEYFRAME', '当前仅支持单层、单关键帧条带的活动Action')
    bag = action.layers[0].strips[0].channelbag(slot, ensure=ensure)
    if bag:
        keys = [(f.data_path, f.array_index) for f in bag.fcurves]
        _require(len(set(keys)) == len(keys), '活动Slot存在重复曲线')
    return bag


def _nla_write_scope(rig, keys, paths=()):
    """Prove every strip's actual assigned slot is disjoint; never mute NLA."""
    ad = rig.animation_data
    if ad is None:return
    _require(not any((f.data_path, f.array_index) in keys or f.data_path in paths for f in ad.drivers), '写域包含driver输入')
    def visit(strips):
        for strip in strips:
            if strip.type == 'META':
                _require(strip.action is None, 'META条带含未知Action')
                visit(strip.strips)
                continue
            _require(strip.type == 'CLIP' and strip.action is not None, '未知或无Action的NLA条带不能证明写域隔离')
            action, slot = strip.action, strip.action_slot
            _require(slot is not None and slot in action.slots[:] and slot.target_id_type == 'OBJECT', 'NLA条带Slot身份未知')
            _require(len(action.layers)>0, 'NLA Action无可证明的层')
            for layer in action.layers:
                _require(len(layer.strips)>0, 'NLA Action层无可证明的条带')
                for native in layer.strips:
                    _require(native.type == 'KEYFRAME', '未知NLA Action条带类型')
                    bag = native.channelbag(slot)
                    _require(bag is not None, 'NLA Slot无明确channelbag')
                    _require(not any((f.data_path,f.array_index) in keys or f.data_path in paths for f in bag.fcurves), 'NLA与明确写域重叠')
    # Muted and disabled tracks are checked too: toggling them later cannot
    # silently change a scoped mouth record's authority.
    for track in ad.nla_tracks:visit(track.strips)


def _binding(rig, *, read_only_nla_paths=None, write_keys=None):
    _require(read_only_nla_paths is None or write_keys is None, '绑定不能同时只读与可写')
    if write_keys is not None:
        _require(isinstance(write_keys,(set,tuple,list)) and write_keys and all(isinstance(k,(tuple,list)) and len(k)==2 and isinstance(k[0],str) and k[0] and type(k[1]) is int and k[1]>=0 for k in write_keys), '可写绑定须明确完整(path,index)集合')
        write_keys = frozenset(tuple(k) for k in write_keys)
        _nla_write_scope(rig, write_keys)
    if read_only_nla_paths is not None:
        _require(isinstance(read_only_nla_paths, (set, tuple, list)) and read_only_nla_paths
                 and all(isinstance(p, str) and p for p in read_only_nla_paths), '只读NLA绑定须明确完整写域')
        from . import face_modes
        _nla_write_scope(rig,set(),set(read_only_nla_paths))
        face_modes._static(rig, set(read_only_nla_paths))
    scope = {}
    if read_only_nla_paths is not None:
        scope['read_only_nla'] = True
    if write_keys is not None:
        scope['write_keys'] = write_keys
    ad = rig.animation_data
    if ad is None:
        return {'exists': False, 'action': None, 'slot': None, 'handle': 0, 'last': '', 'fields': {}, **scope}
    _require(not ad.use_tweak_mode, '请先退出NLA Tweak模式')
    if read_only_nla_paths is None and write_keys is None:
        _require(not (ad.use_nla and any(not t.mute and len(t.strips) for t in ad.nla_tracks)),
                 '请先静音所有含条带的NLA轨道或停用NLA；本入口不单独判断条带时间或影响，原轨道保持')
    if ad.action:
        _require(not ad.action.library and not ad.action.override_library and not ad.action.asset_data,
                 '请先为链接、覆盖或资产Action建立本地普通工作副本')
        _require(ad.action_blend_type == 'REPLACE' and ad.action_influence == 1.0
                 and ad.action_extrapolation == 'HOLD', '活动Action须为REPLACE、满影响和HOLD外推')
        _bag(ad.action, ad.action_slot)
    binding = {'exists': True, 'action': ad.action, 'slot': ad.action_slot,
            'handle': ad.action_slot_handle, 'last': ad.last_slot_identifier,
            'fields': {k: getattr(ad, k) for k in _AD_FIELDS}}
    binding.update(scope)
    return binding


def _preflight(rig, binding, rows):
    keys = {(r['path'], r['index']) for r in rows}
    _require(not binding.get('read_only_nla',False), '只读NLA绑定不能进入写入预检')
    if 'write_keys' in binding:
        _require(keys and keys <= binding['write_keys'], '实际写入超出绑定明确写域')
        _nla_write_scope(rig,binding['write_keys'])
    ad = rig.animation_data
    _require(not ad or not any((f.data_path, f.array_index) in keys for f in ad.drivers),
             '目标输入由driver管理，不能同时记录该输入的Action关键帧')
    bag = _bag(binding['action'], binding['slot']) if binding['action'] else None
    for row in rows:
        fc = bag.fcurves.find(row['path'], index=row['index']) if bag else None
        if fc:
            _require(not fc.modifiers and not fc.sampled_points and not fc.mute and not fc.lock,
                     '目标曲线含修改器、采样、静音或锁定，原曲线保持：' + row['path'])
            if row['interpolation'] == 'CONSTANT':
                _require(all(k.interpolation == 'CONSTANT' for k in fc.keyframe_points),
                         '离散模式的已有键须先明确设为CONSTANT；不会静默改写旧插值')


def _make_action(rig, binding, rows=None):
    _require(not binding.get('read_only_nla', False), '只读NLA绑定不能用于写入或复制Action')
    if 'write_keys' in binding:
        _require(rows is not None, '明确写域绑定必须提供实际写入行')
        _preflight(rig,binding,rows)
    if binding['action']:
        action = binding['action'].copy()
        try:
            slots = [s for s in action.slots if s.handle == binding['handle']
                     and s.identifier == binding['slot'].identifier and s.target_id_type == 'OBJECT']
            _require(len(slots) == 1, 'Action副本的原Slot身份未保持')
            slot = slots[0]
        except Exception:
            bpy.data.actions.remove(action)
            raise
    else:
        action = bpy.data.actions.new(rig.name + '_控制动画')
        try:
            slot = action.slots.new(id_type='OBJECT', name=rig.name)
            action.layers.new('角色控制记录').strips.new(type='KEYFRAME')
        except Exception:
            bpy.data.actions.remove(action)
            raise
    return action, slot


def _write_rows(action, slot, rows, frame):
    bag = _bag(action, slot, ensure=True)
    for row in rows:
        fc = bag.fcurves.find(row['path'], index=row['index'])
        if fc is None:
            fc = bag.fcurves.new(row['path'], index=row['index'])
        points = [k for k in fc.keyframe_points if k.co.x == frame]
        _require(len(points) <= 1, '目标帧存在重复关键帧')
        if points:
            point = points[0]
            point.co_ui = (frame, row['value'])
        else:
            fc.keyframe_points.add(1)
            point = fc.keyframe_points[-1]
            point.co = (frame, row['value'])
            point.interpolation = row['interpolation']
        # FCurve.update() also merges near timestamps in 5.2. Keep exact f32 keys.
        fc.keyframe_points.sort()
        fc.keyframe_points.handles_recalc()
        points = [k for k in fc.keyframe_points if k.co.x == frame]
        _require(len(points) == 1 and points[0].co.y == row['value'], '原生关键帧写入未保持帧/值身份')


def _restore_binding(rig, binding):
    ad = rig.animation_data
    if binding['exists']:
        _require(ad is not None, '动画数据在操作中消失')
        ad.action = binding['action']
        if binding['slot'] is not None:
            ad.action_slot = binding['slot']
        ad.action_slot_handle = binding['handle']
        ad.last_slot_identifier = binding['last']
        for key, value in binding['fields'].items():
            setattr(ad, key, value)
    elif ad is not None:
        _require(not ad.drivers and not ad.nla_tracks, '新动画数据出现非本次内容，保留并要求恢复检查点')
        rig.animation_data_clear()


_OBJECT_CHANNELS = (*pose._CHANNELS, 'delta_location', 'delta_rotation_euler',
                    'delta_rotation_quaternion', 'delta_scale')


def _animated_value(rig, path, index):
    # RNA scalar resolution coerces double ID properties to float32. Capture the
    # original ID value so an unrelated unkeyed edit can be restored exactly.
    custom = re.fullmatch(r'(.*)\[("(?:[^"\\]|\\.)*")\]', path)
    if custom:
        owner = rig.path_resolve(custom[1]) if custom[1] else rig
        value = owner[json.loads(custom[2])]
    else:
        value = rig.path_resolve(path)
    value = value if type(value) in (bool, int, float) else value[index]
    _require(type(value) in (bool, int, float) and math.isfinite(value),
             '活动Action含当前记录入口不支持的非数值输入：' + path)
    return value


def _restore_value(rig, path, index, value):
    custom = re.fullmatch(r'(.*)\[("(?:[^"\\]|\\.)*")\]', path)
    if custom:
        owner = rig.path_resolve(custom[1]) if custom[1] else rig
        key = json.loads(custom[2])
        if type(owner[key]) in (bool, int, float):
            owner[key] = value
        else:
            owner[key][index] = value
    else:
        target = rig.path_resolve(path)
        if type(target) not in (bool, int, float):
            target[index] = value
            return
        owner_path, _, field = path.rpartition('.')
        owner = rig.path_resolve(owner_path) if owner_path else rig
        setattr(owner, field, value)


def _snapshot(rig, extra_paths=None):
    if extra_paths is None:
        ad = rig.animation_data
        bag = _bag(ad.action, ad.action_slot) if ad and ad.action else None
        extra_paths = [(f.data_path, f.array_index) for f in bag.fcurves] if bag else []
    return {'bones': {p.name: pose._channels(p) for p in rig.pose.bones},
            'modes': {(r['bone'], r['property']): rig.pose.bones[r['bone']][r['property']]
                      for r in mode_semantics.available(rig)},
            'object': {'rotation_mode': rig.rotation_mode,
                       **{field: tuple(getattr(rig, field)) for field in _OBJECT_CHANNELS}},
            'animated': {(path, index): _animated_value(rig, path, index) for path, index in extra_paths}}


def _check_inputs(rig, snapshot, allowed):
    """Check native inputs, excluding only explicitly keyed or driven channels."""
    driven = {(f.data_path, f.array_index) for f in rig.animation_data.drivers} if rig.animation_data else set()
    excluded = allowed | driven
    inputs, properties = snapshot['bones'], snapshot['modes']
    _require(rig.rotation_mode == snapshot['object']['rotation_mode'], '记录改变了骨架对象旋转模式')
    for field in _OBJECT_CHANNELS:
        for index, value in enumerate(snapshot['object'][field]):
            if (field, index) not in excluded:
                _require(getattr(rig, field)[index] == value, '非目标骨架对象输入被动画覆盖：' + field)
    for (path, index), value in snapshot['animated'].items():
        if (path, index) not in excluded:
            _require(_animated_value(rig, path, index) == value, '非目标动画输入被覆盖：' + path)
    for name, values in inputs.items():
        pb = rig.pose.bones[name]
        _require(pb.rotation_mode == values['rotation_mode'], '记录改变了旋转模式：' + name)
        for field in pose._CHANNELS:
            path = pb.path_from_id(field)
            for i, value in enumerate(values[field]):
                if (path, i) not in excluded:
                    _require(getattr(pb, field)[i] == value, '非目标输入被动画覆盖：' + name + '.' + field)
    for (name, key), value in properties.items():
        if (_property_path(rig.pose.bones[name], key), 0) not in excluded:
            _require(rig.pose.bones[name][key] == value, '非目标模式被动画覆盖：' + key)


def _record(rig, names, properties, switch=None):
    metadata = pose._metadata(rig)
    _require(rig.data.pose_position == 'POSE', '请在姿态求值状态记录动画')
    frame_raw = bpy.context.scene.frame_current + bpy.context.scene.frame_subframe
    frame = _f32(frame_raw)
    _require(bpy.context.scene.render.frame_map_old == bpy.context.scene.render.frame_map_new,
             '当前入口不支持场景时间重映射')
    binding = _binding(rig)
    initial_rows = _rows(rig, names, properties)
    _preflight(rig, binding, initial_rows)
    if switch is not None:
        bag = _bag(binding['action'], binding['slot']) if binding['action'] else None
        _require(bag is not None and all(
            (fc := bag.fcurves.find(r['path'], index=r['index'])) is not None
            and any(k.co.x < frame for k in fc.keyframe_points) for r in initial_rows),
            '请先在较早帧记录本肢体源姿态，再匹配切换并记录；不会自动补旧帧')
    snapshot = _snapshot(rig)
    inputs, all_properties = snapshot['bones'], snapshot['modes']
    old_fake = binding['action'].use_fake_user if binding['action'] else None
    action = None
    matching = None
    witness = None
    try:
        if rig.animation_data:
            rig.animation_data.action = None
            pose._update(rig)
        if switch is not None:
            matching = pose.switch(rig, **switch)
        rows = _rows(rig, names, properties)
        keys = {(r['path'], r['index']) for r in rows}
        _check_inputs(rig, snapshot, keys)
        matched_inputs = _snapshot(rig, snapshot['animated'])
        witness = pose._Witness(rig, metadata['deforms'])
        before_binding = witness.read()
        action, slot = _make_action(rig, binding)
        _write_rows(action, slot, rows, frame)
        ad = rig.animation_data_create()
        ad.action = action
        ad.action_slot = slot
        if binding['action'] is None:
            ad.action_blend_type = 'REPLACE'; ad.action_influence = 1.0; ad.action_extrapolation = 'HOLD'
        evaluated = pose._update(rig)
        for row in rows:
            value = evaluated.path_resolve(row['path'])
            value = value if type(value) in (int, float, bool) else value[row['index']]
            _require(_f32(value) == row['value'], '绑定后原生求值与记录值不符')
        _check_inputs(rig, matched_inputs, keys)
        binding_metrics = witness.compare(before_binding, witness.read())
        _require(max(binding_metrics['weighted_max_meters'], binding_metrics['endpoint_max_meters']) <= 1e-6,
                 '动画绑定后未复现已记录的姿态，已恢复')
        _require(bpy.context.scene.frame_current + bpy.context.scene.frame_subframe == frame_raw, '记录过程中帧已改变')
        if binding['action']:
            binding['action'].use_fake_user = True
        return {'ok': True, 'action': action.name, 'slot': slot.identifier, 'frame': frame,
                'keyed_channels': len(rows), 'original_action': binding['action'].name if binding['action'] else None,
                'original_action_retained': True, 'original_retention': 'fake user on replaced Action; curves unchanged',
                'matching': matching, 'binding_metrics': binding_metrics,
                'scope': 'current frame only; no previous-frame key or NLA bake'}
    except Exception as exc:
        recovery_errors = []
        try:
            _restore_binding(rig, binding)
            # Flush the restored Action before putting back unkeyed manual inputs.
            pose._update(rig)
        except Exception as recovery:
            recovery_errors.append(str(recovery))
        try:
            for name, values in inputs.items():
                pb = rig.pose.bones[name]
                for field in pose._CHANNELS:
                    setattr(pb, field, values[field])
                pb.rotation_mode = values['rotation_mode']
            for (name, key), value in all_properties.items():
                rig.pose.bones[name][key] = value
            for field in _OBJECT_CHANNELS:
                setattr(rig, field, snapshot['object'][field])
            rig.rotation_mode = snapshot['object']['rotation_mode']
            for (path, index), value in snapshot['animated'].items():
                _restore_value(rig, path, index, value)
            if binding['action']:
                binding['action'].use_fake_user = old_fake
            rig.update_tag(refresh={'OBJECT'})
            bpy.context.view_layer.update()
            _require(_snapshot(rig) == snapshot, '记录失败后的输入恢复未通过')
        except Exception as recovery:
            recovery_errors.append(str(recovery))
        if action is not None:
            if action.users == int(action.use_fake_user):
                bpy.data.actions.remove(action)
            else:
                recovery_errors.append('候选Action仍有消费者，已保留')
        if recovery_errors:
            raise RuntimeError('恢复不完整，请使用先前检查点：' + '; '.join(recovery_errors)) from exc
        raise
    finally:
        if witness is not None:
            witness.close()


def record_modes(rig, *, properties=None):
    """Record available native properties, or an explicit subset, at this frame."""
    return _record(rig, [], properties)


def record_limb(rig, *, limb='ARM', side='L'):
    """Record current limb inputs before making a later keyed switch."""
    names, properties = _limb_scope(rig, limb, side)
    return _record(rig, names, properties)


def switch_and_key(rig, *, limb='ARM', side='L', target='IK', max_error_meters=.001):
    """Explicit match and key; existing unkeyed matching remains unchanged."""
    names, properties = _limb_scope(rig, limb, side)
    return _record(rig, names, properties,
                   switch={'limb': limb, 'side': side, 'target': target, 'max_error_meters': max_error_meters})
