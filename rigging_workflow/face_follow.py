# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned C94 candidates only: eyelid follow relative to its KEY neutral input.

The supported graph is four native COPY_LOCATION constraints with four simple
scripted/SINGLE_PROP influence drivers. Generation applies it only to its new,
owned candidate before freezing the output bind frame.
This calibrates a follow delta; it does not certify full expressions, closure,
attachments, or exact preservation of every old gaze response.
"""
import json
import math

import bpy

from . import corrective_authoring as sig, driver_hygiene, face_modes, pose_switching

RECORD_KEY = 'c94_face_follow_delta_v1'
SCHEMA = 'c94.face-follow-delta.v1'
COLLECTION = '面部跟随校准'
_SIDES = ('L', 'R')
_AXES = ('x', 'z')
_EXPRESSIONS = {'x': 'var*0.20000000298023224*bone', 'z': 'var*0.699999988079071*bone'}
_SELECT = {'select', 'select_head', 'select_tail'}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _names(side):
    return ('机制-眼部.' + side, '机制-眼部_track.' + side,
            '接续-眼部.' + side, '面部跟随输入.' + side)


def _fields(owner):
    values = {}
    for prop in owner.bl_rna.properties:
        if prop.is_readonly or prop.identifier == 'rna_type':
            continue
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            item = getattr(owner, prop.identifier)
            values[prop.identifier] = sig._plain(item)
    # Validate finite numeric fields before storing any rollback authority.
    json.dumps(values, allow_nan=False)
    return values


def _set_fields(owner, values, *, first=(), omit=()):
    for key in (*first, *(k for k in values if k not in first)):
        if key in values and key not in omit:
            setattr(owner, key, values[key])


def _plain_modifier(modifier):
    _require(modifier.type == 'GENERATOR', '跟随驱动只支持无修改器或默认线性GENERATOR')
    fields = _fields(modifier)
    _require(modifier.mode == 'POLYNOMIAL' and modifier.poly_order == 1
             and tuple(modifier.coefficients) == (0.0, 1.0) and not modifier.use_additive,
             '跟随驱动GENERATOR不是默认恒等映射')
    for prop in modifier.bl_rna.properties:
        key = prop.identifier
        if key not in fields or key in {'name', 'active', 'show_expanded', 'mode', 'poly_order',
                                        'coefficients', 'use_additive'}:
            continue
        expected = list(prop.default_array) if getattr(prop, 'is_array', False) else prop.default
        _require(fields[key] == expected, '跟随驱动修改器含非默认字段：' + key)
    return {'type': modifier.type, 'fields': fields}


def _driver_record(rig, curve, side, axis):
    _require(curve.array_index == 0 and not curve.keyframe_points and not curve.sampled_points
             and not curve.mute and curve.group is None and len(curve.modifiers) <= 1,
             '跟随驱动只支持标量、未静音、无关键帧/采样/分组的简单曲线')
    driver = curve.driver
    _require(driver.type == 'SCRIPTED' and not driver.use_self
             and driver.expression == _EXPRESSIONS[axis], '跟随驱动不是已验C94表达式')
    expected = {
        'var': 'pose.bones[' + json.dumps('眼部.' + side, ensure_ascii=False) + ']['
               + json.dumps(('左' if side == 'L' else '右') + '眼睑跟随', ensure_ascii=False) + ']',
        'bone': 'pose.bones[' + json.dumps(face_modes.MODE_BONE, ensure_ascii=False) + ']['
                + json.dumps(face_modes.MODE_PROPERTY, ensure_ascii=False) + ']',
    }
    _require(len(driver.variables) == 2 and {v.name for v in driver.variables} == set(expected),
             '跟随驱动变量范围不符')
    variables = []
    for variable in driver.variables:
        _require(variable.type == 'SINGLE_PROP' and len(variable.targets) == 1,
                 '跟随驱动只支持两个单目标SINGLE_PROP变量')
        target = variable.targets[0]
        _require(target.id == rig and target.id_type == 'OBJECT'
                 and target.data_path == expected[variable.name], '跟随驱动必须自引用已知眼睑/模式属性')
        value = rig.path_resolve(target.data_path)
        _require(type(value) in (bool, int, float) and math.isfinite(value), '跟随驱动属性不是有限数')
        variables.append({'fields': _fields(variable), 'target': _fields(target), 'id': 'SELF'})
    return {'curve': _fields(curve), 'driver': _fields(driver), 'variables': variables,
            'modifiers': [_plain_modifier(m) for m in curve.modifiers]}


def _restore_driver(rig, constraint, record):
    curve = constraint.driver_add('influence')
    # Blender creates two default mapping points even when the original driver
    # had none. The supported source record explicitly forbids authored points.
    curve.keyframe_points.clear()
    for modifier in list(curve.modifiers):
        curve.modifiers.remove(modifier)
    _set_fields(curve, record['curve'])
    _set_fields(curve.driver, record['driver'], first=('type',))
    for variable in list(curve.driver.variables):
        curve.driver.variables.remove(variable)
    for saved in record['variables']:
        variable = curve.driver.variables.new()
        _set_fields(variable, saved['fields'], first=('type', 'name'))
        target = variable.targets[0]
        target.id_type = 'OBJECT'
        target.id = rig
        _set_fields(target, saved['target'], omit=('id_type',))
    for saved in record['modifiers']:
        modifier = curve.modifiers.new(saved['type'])
        _set_fields(modifier, saved['fields'], first=('mode', 'poly_order'))
    return curve


def _constraint_record(constraint):
    # Both supported old/new graphs use exactly these two self-reference IDs.
    return {'type': constraint.type, 'fields': _fields(constraint),
            'target': 'SELF', 'space_object': 'SELF'}


def _restore_constraint(rig, owner, record):
    constraint = owner.constraints.new(record['type'])
    constraint.target = rig
    constraint.space_object = rig
    _set_fields(constraint, record['fields'], first=('name', 'target_space', 'owner_space'))
    return constraint


def _local_candidate(rig):
    _require(isinstance(rig, bpy.types.Object) and rig.type == 'ARMATURE'
             and not rig.library and not rig.override_library and not rig.data.library
             and not rig.data.override_library and rig.is_editable and rig.data.is_editable,
             '仅处理调用者拥有的本地可编辑骨架候选')
    _require(bpy.context.mode == 'OBJECT' and rig.mode == 'OBJECT'
             and rig.name in bpy.context.scene.objects and rig.name in bpy.context.view_layer.objects,
             '校准需要当前Scene/ViewLayer内的Object模式候选')
    _require(rig.visible_get(view_layer=bpy.context.view_layer) and not rig.hide_select,
             '候选须在当前视图层可见可选；校准不更改对象显隐/选择权限')
    _require(rig.data.users == 1 and sum(o.type == 'ARMATURE' and o.data == rig.data for o in bpy.data.objects) == 1
             and sum(rig.name in scene.objects for scene in bpy.data.scenes) == 1,
             '候选不能共享Armature Data或跨Scene复用同一Object')
    scale = float(bpy.context.scene.unit_settings.scale_length)
    _require(math.isfinite(scale) and scale > 0 and abs(rig.matrix_world.determinant()) > 1e-12
             and all(math.isfinite(v) for row in rig.matrix_world for v in row), '需要有限可逆世界矩阵和正单位比例')
    return scale


def _curves(rig, paths):
    ad = rig.animation_data
    _require(ad is not None, '已知面部跟随图缺少驱动')
    result = {}
    for path in paths:
        matches = [f for f in ad.drivers if f.data_path == path]
        _require(len(matches) == 1, '跟随影响量必须恰有一条标量驱动：' + path)
        result[path] = matches[0]
    return result


def _reject_other_target_animation(rig, paths):
    prefixes = {p.rsplit('.', 1)[0] + '.' for p in paths}
    ad = rig.animation_data
    for curve in ad.drivers:
        _require(not any(curve.data_path.startswith(p) for p in prefixes) or curve.data_path in paths,
                 '跟随约束还有未知属性驱动，拒绝部分升级')
    if ad.action:
        curves = face_modes._curves(ad.action, ad.action_slot)
    else:
        curves = []
    def visit(strips):
        for strip in strips:
            if strip.action:
                curves.extend(face_modes._curves(strip.action, strip.action_slot))
            if strip.type == 'META':
                visit(strip.strips)
    for track in ad.nla_tracks:
        visit(track.strips)
    _require(not any(any(c.data_path.startswith(p) for p in prefixes) for c in curves),
             '跟随约束含Action/NLA动画，原动画保持')


def _preflight(rig):
    _require(face_modes.MODE_BONE in rig.pose.bones
             and rig.pose.bones[face_modes.MODE_BONE].get(face_modes.MODE_PROPERTY) == 0,
             '校准必须处于原生KEY模式，不能关闭跟随或改操作者输入强过')
    rows = []
    for side in _SIDES:
        parent, owner_name, space, helper = _names(side)
        _require(all(name in rig.data.bones for name in (parent, owner_name, space, '眼部.' + side)),
                 '面部模块部分缺失，拒绝升级：' + side)
        owner = rig.pose.bones[owner_name]
        _require([c.name for c in owner.constraints] == ['lid_follow_x', 'lid_follow_z'],
                 '眼部track不是已验的双约束顺序，拒绝改动未知图')
        for axis in _AXES:
            constraint = owner.constraints['lid_follow_' + axis]
            _require(constraint.type == 'COPY_LOCATION' and constraint.target == rig
                     and constraint.subtarget == parent and constraint.space_object == rig
                     and constraint.space_subtarget == space and constraint.target_space == 'CUSTOM'
                     and constraint.owner_space == 'CUSTOM' and constraint.head_tail == 1.0
                     and not constraint.use_bbone_shape and not constraint.use_offset
                     and not constraint.mute and constraint.influence == 0.0,
                     '跟随约束不是已验KEY基线COPY_LOCATION图：' + side + '/' + axis)
            _require(all(getattr(constraint, 'use_' + a) == (a == axis)
                         and not getattr(constraint, 'invert_' + a) for a in 'xyz'),
                     '跟随约束轴或反转配置不符')
            rows.append({'side': side, 'axis': axis, 'owner': owner_name,
                         'name': constraint.name, 'path': constraint.path_from_id('influence'),
                         'constraint': _constraint_record(constraint)})
    curves = _curves(rig, {r['path'] for r in rows})
    _reject_other_target_animation(rig, set(curves))
    for row in rows:
        row['driver'] = _driver_record(rig, curves[row['path']], row['side'], row['axis'])
    return rows


def _protected(rig, names, paths):
    bones = []
    for name in names:
        bone, pb = rig.data.bones[name], rig.pose.bones[name]
        fields = _fields(bone)
        for key in _SELECT:
            fields.pop(key, None)
        pose_fields = _fields(pb)
        for key in ('matrix', 'matrix_basis', 'matrix_channel', 'select'):
            pose_fields.pop(key, None)
        bones.append([name, sig._plain(bone.matrix_local), bone.parent.name if bone.parent else None,
                      fields, pose_fields, sig._plain(dict(bone.items())), sig._plain(dict(pb.items())),
                      sorted(c.name for c in bone.collections),
                      [[sig._rna(c), [sig._rna(t) for t in getattr(c, 'targets', ())]]
                       for c in pb.constraints if c.path_from_id('influence') not in paths]])
    properties = dict(rig.items())
    properties.pop(RECORD_KEY, None)
    drivers = [(f.data_path, f.array_index, driver_hygiene.signature(f))
               for f in rig.animation_data.drivers if f.data_path not in paths]
    return sig._hash({'bones': bones, 'drivers': drivers,
                      'properties': sig._plain(properties), 'data_properties': sig._plain(dict(rig.data.items())),
                      'world': sig._plain(rig.matrix_world), 'object_basis': sig._plain(rig.matrix_basis),
                      'object_constraints': [sig._rna(c) for c in rig.constraints]})


def _context(rig):
    return {'active': bpy.context.view_layer.objects.active,
            'selection': list(bpy.context.selected_objects), 'mirror': rig.data.use_mirror_x,
            'active_bone': rig.data.bones.active.name if rig.data.bones.active else None,
            'bone_selection': {b.name: {f: getattr(b, f) for f in _SELECT if hasattr(b, f)} for b in rig.data.bones},
            'active_collection': rig.data.collections.active_index,
            'collections': [(c.name, c.is_visible, c.is_solo, sorted(b.name for b in c.bones))
                            for c in rig.data.collections_all]}


def _activate(rig):
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig


def _restore_context(rig, before):
    if rig.mode != 'OBJECT':
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode='OBJECT')
    rig.data.use_mirror_x = before['mirror']
    for name, values in before['bone_selection'].items():
        for field, value in values.items():
            setattr(rig.data.bones[name], field, value)
    rig.data.bones.active = rig.data.bones.get(before['active_bone']) if before['active_bone'] else None
    rig.data.collections.active_index = before['active_collection']
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in before['selection']:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = before['active']


def _add_helpers(rig, scale, created):
    _activate(rig)
    rig.data.use_mirror_x = False
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        for side in _SIDES:
            parent_name, _, _, helper_name = _names(side)
            parent = rig.data.edit_bones[parent_name]
            direction = (parent.tail - parent.head).normalized()
            world_length = (rig.matrix_world.to_3x3() @ direction).length * scale
            _require(world_length > 1e-12, '眼部父骨方向无效')
            helper = rig.data.edit_bones.new(helper_name)
            created.append(helper.name)
            _require(helper.name == helper_name, '面部跟随helper名称发生冲突')
            helper.head = parent.tail
            helper.tail = parent.tail + direction * (0.01 / world_length)
            helper.parent = parent
            helper.use_connect = False
            helper.use_deform = False
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    collection = rig.data.collections.new(COLLECTION)
    _require(collection.name == COLLECTION, '自有面部跟随集合名称冲突')
    collection.is_visible = False
    for name in created:
        bone = rig.data.bones[name]
        for owner in list(bone.collections):
            owner.unassign(bone)
        collection.assign(bone)
        bone.hide_select = True
        for key in _SELECT:
            if hasattr(bone, key):
                setattr(bone, key, False)
    face_modes._update(rig)


def _calibration(rig, scale):
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    result = {}
    for side in _SIDES:
        parent, _, space, helper = _names(side)
        difference = evaluated.pose.bones[helper].head - evaluated.pose.bones[parent].tail
        error = (rig.matrix_world.to_3x3() @ difference).length * scale
        _require(error <= 1e-6, 'helper实际头位置未跟随眼部父骨tail：' + side)
        q = evaluated.pose.bones[space].matrix.inverted() @ evaluated.pose.bones[helper].head
        _require(all(math.isfinite(v) for v in q), '自定义空间中立坐标无效')
        result[side] = {'neutral_target_custom': list(q), 'helper_matches_target_tail_m': error}
    return result


def _replace_constraints(rig, rows, calibration):
    for side in _SIDES:
        _, owner_name, space, helper = _names(side)
        owner = rig.pose.bones[owner_name]
        for old in list(owner.constraints):
            owner.constraints.remove(old)
        for row in (r for r in rows if r['side'] == side):
            constraint = owner.constraints.new('TRANSFORM')
            constraint.name = row['name']
            constraint.target = rig
            constraint.subtarget = helper
            constraint.target_space = 'CUSTOM'
            constraint.owner_space = 'CUSTOM'
            constraint.space_object = rig
            constraint.space_subtarget = space
            constraint.map_from = 'LOCATION'
            constraint.map_to = 'LOCATION'
            _require('ADD' in constraint.bl_rna.properties['mix_mode'].enum_items.keys(),
                     '当前TRANSFORM位置混合接口不支持ADD')
            constraint.mix_mode = 'ADD'
            constraint.use_motion_extrapolate = True
            for index, axis in enumerate('xyz'):
                q = calibration[side]['neutral_target_custom'][index]
                setattr(constraint, 'from_min_' + axis, q)
                setattr(constraint, 'from_max_' + axis, q + 1)
                setattr(constraint, 'to_min_' + axis, 0)
                setattr(constraint, 'to_max_' + axis, 1 if axis == row['axis'] else 0)
            # Preserve the old display/mute state and driven influence baseline.
            for field in ('show_expanded', 'mute', 'influence'):
                if field in row['constraint']['fields']:
                    setattr(constraint, field, row['constraint']['fields'][field])
            _restore_driver(rig, constraint, row['driver'])
    face_modes._update(rig)


def _owned_state(rig, calibration):
    helpers, constraints, drivers = [], [], []
    collection = rig.data.collections_all.get(COLLECTION)
    _require(collection is not None and not collection.is_visible and not collection.is_solo
             and sorted(b.name for b in collection.bones) == sorted(_names(s)[3] for s in _SIDES),
             '已校准集合缺失、可见或成员冲突')
    for side in _SIDES:
        parent, owner_name, space, helper_name = _names(side)
        _require(all(n in rig.data.bones for n in (parent, owner_name, space, helper_name)), '已校准图不完整')
        helper = rig.data.bones[helper_name]
        pb = rig.pose.bones[helper_name]
        _require(helper.parent == rig.data.bones[parent] and not helper.use_deform and not helper.use_connect
                 and helper.hide_select and not pb.custom_shape and not pb.constraints
                 and sorted(c.name for c in helper.collections) == [COLLECTION], '同名helper不是已校准非控件机制骨')
        helpers.append([helper_name, sig._plain(helper.matrix_local), helper.length,
                        pose_switching._channels(pb)])
        owner = rig.pose.bones[owner_name]
        _require([c.name for c in owner.constraints] == ['lid_follow_x', 'lid_follow_z'], '已校准约束顺序改变')
        for axis in _AXES:
            constraint = owner.constraints['lid_follow_' + axis]
            _require(constraint.type == 'TRANSFORM' and constraint.target == rig and constraint.subtarget == helper_name
                     and constraint.space_object == rig and constraint.space_subtarget == space
                     and constraint.target_space == 'CUSTOM' and constraint.owner_space == 'CUSTOM'
                     and constraint.map_from == 'LOCATION' and constraint.map_to == 'LOCATION'
                     and constraint.mix_mode == 'ADD' and constraint.use_motion_extrapolate and not constraint.mute,
                     '已校准TRANSFORM图被改变')
            q = calibration[side]['neutral_target_custom']
            for index, a in enumerate('xyz'):
                _require(abs(getattr(constraint, 'from_min_' + a) - q[index]) <= 1e-7
                         and abs(getattr(constraint, 'from_max_' + a) - (q[index] + 1)) <= 1e-7
                         and getattr(constraint, 'to_min_' + a) == 0
                         and getattr(constraint, 'to_max_' + a) == (1 if a == axis else 0), '已校准中立映射范围改变')
            constraints.append(_constraint_record(constraint))
            path = constraint.path_from_id('influence')
            curve = _curves(rig, {path})[path]
            drivers.append(_driver_record(rig, curve, side, axis))
    return {'helpers': helpers, 'constraints': constraints, 'drivers': drivers}


def _rollback(rig, rows, created, *, touched):
    if rig.mode != 'OBJECT':
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode='OBJECT')
    if touched:
        for side in _SIDES:
            owner = rig.pose.bones[_names(side)[1]]
            for constraint in list(owner.constraints):
                owner.constraints.remove(constraint)
            for row in (r for r in rows if r['side'] == side):
                constraint = _restore_constraint(rig, owner, row['constraint'])
                _restore_driver(rig, constraint, row['driver'])
    if created:
        _activate(rig)
        rig.data.use_mirror_x = False
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            for name in created:
                if name in rig.data.edit_bones:
                    rig.data.edit_bones.remove(rig.data.edit_bones[name])
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
    collection = rig.data.collections_all.get(COLLECTION)
    if collection:
        _require(not collection.bones, '回滚时自有集合仍有骨，不删除未知内容')
        rig.data.collections.remove(collection)
    if RECORD_KEY in rig:
        del rig[RECORD_KEY]
    face_modes._update(rig)


def calibrate_owned(rig):
    """Upgrade only an explicitly owned candidate; failure restores its old graph."""
    scale = _local_candidate(rig)
    markers = {n for side in _SIDES for n in _names(side)[:3]}
    face_collections = {'面部', '面部-主控', '面部-次要', '面部形变', '表情面板'}
    has_face = bool(markers.intersection(rig.data.bones.keys())) or any(
        c.name in face_collections and len(c.bones) for c in rig.data.collections_all)
    if not has_face:
        _require(RECORD_KEY not in rig and not any(_names(s)[3] in rig.data.bones for s in _SIDES)
                 and rig.data.collections_all.get(COLLECTION) is None, '无面部图却残留升级记录/helper')
        return {'status': 'not_applicable', 'changed': False, 'reason': 'face module absent'}
    _require(rig.pose.bones.get(face_modes.MODE_BONE) is not None
             and rig.pose.bones[face_modes.MODE_BONE].get(face_modes.MODE_PROPERTY) == 0, '必须在KEY模式调用')
    if RECORD_KEY in rig:
        try:
            record = json.loads(rig[RECORD_KEY])
            _require(record['schema'] == SCHEMA and record['status'] == 'calibrated', '升级记录版本或状态无效')
            state = _owned_state(rig, record['calibration'])
            _require(sig._hash(state) == record['owned_state_sha256'], '升级记录与实际图不一致，不重复写入')
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('面部跟随升级记录损坏，不按同名helper推断成功') from exc
        return dict(record['report'], status='already_calibrated', changed=False)
    _require(not any(_names(s)[3] in rig.data.bones for s in _SIDES)
             and rig.data.collections_all.get(COLLECTION) is None, '同名helper/集合已存在但没有正式升级记录')
    rows = _preflight(rig)
    paths = {row['path'] for row in rows}
    names = list(rig.data.bones.keys())
    before = _context(rig)
    protected_before = _protected(rig, names, paths)
    created, touched = [], False
    try:
        _add_helpers(rig, scale, created)
        calibration = _calibration(rig, scale)
        touched = True
        _replace_constraints(rig, rows, calibration)
        for row in rows:
            curve = _curves(rig, {row['path']})[row['path']]
            _require(_driver_record(rig, curve, row['side'], row['axis']) == row['driver'], '原跟随driver未完整恢复')
        _restore_context(rig, before)
        _require(_protected(rig, names, paths) == protected_before, '原骨rest/输入或非目标图发生变化')
        _require([(c.name, c.is_visible, c.is_solo, sorted(b.name for b in c.bones))
                  for c in rig.data.collections_all if c.name != COLLECTION] == before['collections'],
                 '已有集合成员或显隐改变')
        state = _owned_state(rig, calibration)
        report = {'status': 'calibrated', 'changed': True, 'schema': SCHEMA,
                  'helpers': created[:], 'replaced_constraints': [r['path'] for r in rows],
                  'preserved_influence_drivers': len(rows), 'calibration': calibration,
                  'protected_before_after_equal': True,
                  'before_evidence': {'original_bone_count': len(names),
                                      'protected_sha256': protected_before,
                                      'constraint_and_driver_snapshot': rows},
                  'scope': 'KEY-neutral-relative eyelid follow on the known C94 graph; not complete facial closure or all-motion exactness'}
        record = {'schema': SCHEMA, 'status': 'calibrated', 'calibration': calibration,
                  'owned_state_sha256': sig._hash(state), 'report': report}
        rig[RECORD_KEY] = json.dumps(record, ensure_ascii=False, allow_nan=False)
        return report
    except Exception as error:
        try:
            _rollback(rig, rows, created, touched=touched)
            _restore_context(rig, before)
            _require(_protected(rig, names, paths) == protected_before, '回滚后非目标保护核对失败')
            restored = _preflight(rig)
            _require(restored == rows, '回滚后原四约束/driver定义不一致')
        except Exception as recovery:
            raise RuntimeError('面部跟随校准失败且回滚未完全核实；保留候选供恢复：' + str(recovery)) from error
        raise
