# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared daily Key-control core: read, set, mode switch and frame recording.

The authoring entry points import this module through face_expression_controls;
no second implementation of the daily expressions is kept here. Creation and
sampling remain in the authoring modules.
"""
import json
import math

import bpy

from . import face_modes as fm, mode_animation as animation

STATE_KEY = 'c94_face_expression_controls_v1'
SCHEMA = 'c94.face-expression-controls.v2'
LEGACY_SCHEMA = 'c94.face-expression-controls.v1'
EXPRESSION = 'w if m < 0.5 else 0'
_require = fm._require


class _HashFacade:
    """Compatibility for authoring callers that used controls.sig._hash."""

    @staticmethod
    def _hash(value):
        return fm.hash_record(value)


sig = _HashFacade()


def _path(key):
    return '[' + json.dumps(key, ensure_ascii=False) + ']'


def _identity_mapping(curve):
    """Accept direct output and the exact legacy two-point identity mapping.

    Old Blender driver_add stored a straight Bezier from (0,0) to (1,1).
    Collinear handles, monotone X and linear extrapolation prove y=x over
    its whole domain; reading an old project must not rewrite those curves.
    """
    if curve.sampled_points or curve.modifiers:
        return False
    points = list(curve.keyframe_points)
    if not points:
        return True
    if (len(points) != 2 or curve.extrapolation != 'LINEAR'
            or [tuple(point.co) for point in points] != [(0., 0.), (1., 1.)]):
        return False
    for point in points:
        if point.interpolation not in {'BEZIER', 'LINEAR'}:
            return False
        if not all(math.isfinite(v) for handle in (point.handle_left, point.handle_right) for v in handle):
            return False
        if any(handle.x != handle.y for handle in (point.handle_left, point.handle_right)):
            return False
        if not point.handle_left.x < point.co.x < point.handle_right.x:
            return False
    return 0. < points[0].handle_right.x <= points[1].handle_left.x < 1.


def _clear_new_driver_mapping(curve):
    """Clear the default 0..1 mapping created by driver_add on a new curve."""
    while curve.keyframe_points:
        curve.keyframe_points.remove(curve.keyframe_points[-1], fast=True)
    for modifier in list(curve.modifiers):
        curve.modifiers.remove(modifier)
    curve.update()


def _remove_owned_action(action, errors=None, label='本次候选Action'):
    """Remove only an action owned by this call, even when it has a fake user."""
    if action is None:
        return True
    try:
        # A fake user is not a consumer. Any other user means the action is no
        # longer solely ours and must be preserved for inspection.
        _require(action.users == int(action.use_fake_user),
                 label + '已有真实消费者，保留供检查')
        action.use_fake_user = False
        bpy.data.actions.remove(action)
        return True
    except Exception as error:
        if errors is None:
            raise
        errors.append(label + ': ' + str(error))
        return False


def _key_name(row, index):
    return row['keys'][index] if 'keys' in row else row['name']


def _control_type(hub):
    graph = 'c94_face_graph_board_v1' in hub
    mouth = 'c94_face_mouth_pad_v1' in hub
    _require(not (graph and mouth), '控制对象同时声明两种图形板，保留工程检查；不猜写入路线')
    return 'MOUTH' if mouth else 'GRAPH' if graph else 'HUB'


def _update(hub, rig):
    hub.update_tag(refresh={'OBJECT'})
    if _control_type(hub) != 'HUB':
        # TIME would reapply the rig Action and erase a freshly edited slider.
        # RNA/Action binding and scene.frame_set already tag actual time changes.
        rig.update_tag(refresh={'OBJECT','DATA'})
        bpy.context.view_layer.update()
    else:
        fm._update(rig)


def _local(obj, kind):
    _require(isinstance(obj, bpy.types.Object) and obj.type == kind
             and obj.name in bpy.context.scene.objects and obj.name in bpy.context.view_layer.objects
             and obj.is_editable and not obj.library and not obj.override_library,
             '需要当前场景/视图层可编辑的本地对象')


def _face_static(rig):
    # Physical controls must never acquire even a same-valued driver/Action.
    owned = set(fm._owned_face_bones(rig))
    paths = {rig.pose.bones[n].path_from_id(f) for n in owned
             for f in (*fm._FIELDS, 'rotation_mode')}
    paths.update(fm._property_path(rig.pose.bones[n], k) for n, k in fm._properties(rig))
    paths.add(fm._property_path(rig.pose.bones[fm.MODE_BONE], fm.MODE_PROPERTY))
    fm._static(rig, paths)
    # Other writable face-board inputs must also be static. Existing derived
    # board driver channels are outputs; they are not authored by this tool.
    others = {rig.pose.bones[n].path_from_id(f) for n in fm._bone_names(rig)
              if n not in owned for f in (*fm._FIELDS, 'rotation_mode')}
    driven = {f.data_path for f in rig.animation_data.drivers} if rig.animation_data else set()
    fm._static(rig, others - driven)
    record = fm._read(rig)
    for item in fm._resolve_identity_items(rig, record['identity']):
        fm._static(item['keys'], {item['key'].path_from_id('value')})


def _baseline(rig, fingerprint, *, mode=True):
    fm._rig(rig)
    _require(rig.data.pose_position == 'POSE', '骨架须为POSE求值')
    neutral = fm._read(rig)['neutral']
    _require(fm.hash_record(neutral) == fingerprint, '中立定义已改变，请保留原控制并重新建立映射')
    _require(not fm._conflicts(fm._inputs(rig), neutral), '面部输入偏离所记中立，请明确复位后使用Key控制')
    _face_static(rig)
    if mode:
        _require(fm._mode(rig) == 'KEY', '请先切到形态键模式；骨控模式下本组Key输出为0')


def _maintenance_member_references(rig, obj):
    """Identify the pack's two explicit ownership references for this member."""
    from . import native_maintenance as maintenance

    state = rig.get(maintenance.STATE_KEY)
    if (not state or state.get('version') != maintenance._VERSION
            or state.get('rig') != rig or state.get('owned_armature') != rig.data):
        return False, False
    members = state.get('objects')
    if members is None:
        return False, False
    indices = [index for index, name in enumerate(sorted(members.keys())) if members[name] == obj]
    if len(indices) != 1:
        return False, False
    index = indices[0]
    owned_data = state.get('owned_data', {})
    owned_keys = state.get('owned_keys', {})
    return (owned_data.get('member_%06d' % (index + 1)) == obj.data,
            owned_keys.get('member_%06d' % index) == obj.data.shape_keys)


def _read(hub):
    _local(hub, 'EMPTY')
    try:
        record = json.loads(hub[STATE_KEY])
        _require(set(record) == {'schema', 'group_id', 'channels', 'member_count', 'neutral_fingerprint'}
                 and record['schema'] in {SCHEMA, LEGACY_SCHEMA}, '控制对象记录字段或版本无效')
        rows = record['channels']
        fields = {'name', 'property', 'keys'} if record['schema'] == SCHEMA else {'name', 'property'}
        _require(isinstance(rows, list) and rows and all(set(r) == fields for r in rows)
                 and len({r['name'] for r in rows}) == len(rows)
                 and [r['property'] for r in rows] == ['表情_%02d' % i for i in range(len(rows))],
                 '控制通道记录无效')
        rig = hub['rig']
        _local(rig, 'ARMATURE')
        members = [hub['member_%03d' % i] for i in range(record['member_count'])]
        _require(members and len(set(members)) == len(members), '同步成员重复或缺失')
        if record['schema'] == SCHEMA:
            _require(all(isinstance(r['keys'], list) and len(r['keys']) == len(members)
                         and all(isinstance(n,str) and n for n in r['keys']) for r in rows), '每成员Key映射无效')
        _require(all(len({_key_name(r,i) for r in rows}) == len(rows) for i in range(len(members))),
                 '同一Key不能被多个通道同时管理')
        for row in rows:
            value = hub[row['property']]
            _require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1,
                     '表情参数必须在0..1内')
        for index, obj in enumerate(members):
            _local(obj, 'MESH')
            keys = obj.data.shape_keys
            mesh_reference = key_reference = False
            if obj.data.users != 1 or (keys and keys.users != 1):
                mesh_reference, key_reference = _maintenance_member_references(rig, obj)
            _require(obj.data.is_editable and not obj.data.library and not obj.data.override_library
                     and obj.data.users == 1 + mesh_reference, '同步Mesh必须本地可编辑且单用户')
            _require(keys is not None and keys.user == obj.data and keys.users == 1 + key_reference
                     and keys.animation_data is not None and keys.animation_data.action is None
                     and not keys.animation_data.nla_tracks, 'Key所有权或动画绑定已改变')
            curves = list(keys.animation_data.drivers)
            _require(len(curves) == len(rows), 'Key驱动数量已改变，拒绝覆盖其它驱动')
            for row in rows:
                key = keys.key_blocks.get(_key_name(row, index))
                _require(key is not None, '映射Key缺失')
                matched = [f for f in curves if f.data_path == key.path_from_id('value') and f.array_index == 0]
                _require(len(matched) == 1, '映射Key驱动身份无效')
                f = matched[0]; d = f.driver
                _require(not f.mute and not f.modifiers and d.type == 'SCRIPTED' and d.expression == EXPRESSION
                         and len(d.variables) == 2 and _identity_mapping(f),
                         '映射Key驱动已手改（含曲线映射），保留手改不覆盖')
                for var, (name, owner, path) in zip(d.variables, [('w', hub, _path(row['property'])),
                        ('m', rig, fm._property_path(rig.pose.bones[fm.MODE_BONE], fm.MODE_PROPERTY))]):
                    _require(var.name == name and var.type == 'SINGLE_PROP' and var.targets[0].id == owner
                             and var.targets[0].data_path == path, '映射Key驱动目标已改变')
        return record, rig, members
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('表情控制记录或引用损坏；保留工程检查映射') from exc


def inspect(hub):
    record, rig, members = _read(hub)
    return dict(channels=record['channels'], values={r['name']:float(hub[r['property']]) for r in record['channels']},
                rig=rig.name, members=[o.name for o in members], mode=fm._mode(rig),
                note='本组Key只在KEY模式输出；KEY要求所记中立。多参数线性叠加效果须另查。')


def set_values(hub, values):
    kind = _control_type(hub)
    if kind == 'MOUTH':
        from . import face_mouth_core
        return face_mouth_core.set_values(hub, values)
    if kind == 'GRAPH':
        from . import face_graph_core
        return face_graph_core.set_values(hub, values)
    record, rig, _ = _read(hub); _baseline(rig, record['neutral_fingerprint'])
    names = {r['name']:r['property'] for r in record['channels']}
    _require(isinstance(values, dict) and values and set(values) <= names.keys()
             and all(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1 for v in values.values()),
             '明确指定已有表情名称和0..1数值')
    rows = [dict(path=_path(names[n]), index=0, value=animation._f32(v), interpolation='LINEAR') for n,v in values.items()]
    animation._preflight(hub, animation._binding(hub), rows)
    before = {names[n]:hub[names[n]] for n in values}
    try:
        for n,v in values.items(): hub[names[n]]=float(v)
        _update(hub, rig)
        return inspect(hub)
    except Exception:
        for prop,value in before.items():hub[prop]=value
        _update(hub, rig);raise


def _preflight_mode_change(rig, *, target):
    """One rig has one mode: every owned hub must permit a static transition.

    Hidden/excluded same-rig hubs remain participants. An excluded or damaged
    hub that cannot pass its normal reader blocks the transition by name.
    Revision aliases share input paths; they are validated, never skipped.
    """
    _require(target in {'KEY', 'BONE'}, '明确目标面部模式')
    if fm._mode(rig) == target:
        return {'mode_change': False, 'hubs': [], 'input_paths': 0}
    markers = {STATE_KEY, 'c94_face_graph_board_v1', 'c94_face_mouth_pad_v1'}
    hubs = sorted((obj for obj in bpy.data.objects if obj.get('rig') == rig
                   and any(key in obj for key in markers)), key=lambda obj: obj.name)
    if not hubs:
        return {'mode_change': True, 'hubs': [], 'input_paths': 0}
    # Read fresh native driver outputs without TIME overwriting unkeyed controls.
    for hub in hubs: hub.update_tag(refresh={'OBJECT'})
    rig.update_tag(refresh={'OBJECT', 'DATA'})
    bpy.context.view_layer.update()
    fingerprint = fm.hash_record(fm._read(rig)['neutral'])
    inputs = {}
    for hub in hubs:
        try:
            kind = _control_type(hub)
            if kind == 'MOUTH':
                from . import face_mouth_core as pad
                group, board, actual_rig, _ = pad._read(hub)
                bones = [board['pad']] + [r['bone'] for r in board['sliders']]
                pad_input = rig.pose.bones[board['pad']].location
                _require(pad_input.x == 0 and pad_input.y == 0
                         and all(rig.pose.bones[r['bone']].location.y == 0 for r in board['sliders']),
                         '二维板/滑杆原始输入尚未归0')
                inputs.setdefault(rig, set()).update(pad._path(n) for n in bones)
            elif kind == 'GRAPH':
                try:
                    from . import face_graph_core as graph
                except ImportError as exc:
                    raise ValueError('图形板日常读取未包含在本便携切片；请保留制作包处理') from exc
                from . import face_mouth_core as pad
                group, board, actual_rig, _ = graph._read(hub)
                _require(all(rig.pose.bones[r['bone']].location.y == 0 for r in board['channels']),
                         '图形板滑杆原始输入尚未归0')
                inputs.setdefault(rig, set()).update(graph._bone_path(r['bone']) for r in board['channels'])
                # Its expected native drivers are legal; a second Action owner is not.
                pad._no_target_animation(hub, {_path(r['property']) for r in group['channels']}, allow_owned_drivers=True)
            else:
                group, actual_rig, _ = _read(hub)
                inputs.setdefault(hub, set()).update(_path(r['property']) for r in group['channels'])
            _require(actual_rig == rig and group['neutral_fingerprint'] == fingerprint,
                     '角色引用或中立定义不匹配')
            _require(all(hub[r['property']] == 0 for r in group['channels']), '本组表情参数尚未归0')
        except (ValueError, RuntimeError, KeyError, TypeError, AttributeError, ReferenceError) as error:
            raise ValueError('同rig表情控制“' + hub.name + '”阻止模式切换：' + str(error)) from error
    # Deduplicate actual owner/path pairs, including shared revision controls.
    for owner, paths in inputs.items():
        try:
            fm._static(owner, paths)
        except (ValueError, RuntimeError) as error:
            raise ValueError('同rig表情输入存在动画/driver；保持原模式（' + owner.name + '）：' + str(error)) from error
    return {'mode_change': True, 'hubs': [h.name for h in hubs], 'input_paths': sum(len(p) for p in inputs.values())}


def switch(hub, *, target, reset=False):
    kind = _control_type(hub)
    if kind == 'MOUTH':
        from . import face_mouth_core
        return face_mouth_core.switch(hub, target=target, reset=reset)
    if kind == 'GRAPH':
        from . import face_graph_core
        return face_graph_core.switch(hub, target=target, reset=reset)
    record, rig, _ = _read(hub)
    _require(type(reset) is bool and target in {'KEY','BONE'}, '明确选择模式与复位选项')
    _require(reset or all(hub[r['property']]==0 for r in record['channels']), '先清零本组表情，或明确选择复位后切换')
    # Animated control values cannot be cleared by a static route transition.
    props = {_path(r['property']) for r in record['channels']}
    fm._static(hub, props)
    _require(fm.hash_record(fm._read(rig)['neutral']) == record['neutral_fingerprint'], '中立定义改变，请重新建立映射')
    old = {r['property']:hub[r['property']] for r in record['channels']}
    try:
        if reset:
            for prop in old:hub[prop]=0.0
        result = fm.switch(rig, target=target, reset=reset)
        return result
    except Exception:
        for prop,value in old.items():hub[prop]=value
        _update(hub, rig);raise


def record_current(hub):
    """Current-frame keys on a copied Object Action; original curves retained."""
    kind = _control_type(hub)
    if kind == 'MOUTH':
        from . import face_mouth_core
        return face_mouth_core.record_current(hub)
    if kind == 'GRAPH':
        from . import face_graph_core
        return face_graph_core.record_current(hub)
    record, rig, _ = _read(hub); _baseline(rig, record['neutral_fingerprint'])
    scene = bpy.context.scene
    _require(scene.render.frame_map_old == scene.render.frame_map_new, '不支持场景时间重映射')
    frame_raw = scene.frame_current + scene.frame_subframe; frame = animation._f32(frame_raw)
    rows = [dict(path=_path(r['property']),index=0,value=animation._f32(hub[r['property']]),interpolation='LINEAR') for r in record['channels']]
    binding = animation._binding(hub); animation._preflight(hub, binding, rows)
    bag = animation._bag(binding['action'],binding['slot']) if binding['action'] else None
    paths = {(f.data_path,f.array_index) for f in bag.fcurves} if bag else set()
    paths.update((r['path'],0) for r in rows)
    paths.update((field,i) for field in animation._OBJECT_CHANNELS for i in range(len(getattr(hub,field))))
    before = {p:animation._animated_value(hub,*p) for p in paths}
    rig_before = fm._snapshot(rig); action = None
    old_fake = binding['action'].use_fake_user if binding['action'] else None
    try:
        action,slot = animation._make_action(hub,binding)
        animation._write_rows(action,slot,rows,frame)
        ad=hub.animation_data_create();ad.action=action;ad.action_slot=slot
        if not binding['action']:
            ad.action_blend_type='REPLACE';ad.action_influence=1.;ad.action_extrapolation='HOLD'
        _update(hub, rig)
        allowed={(r['path'],0) for r in rows}
        _require(all(animation._f32(animation._animated_value(hub,r['path'],0))==r['value'] for r in rows), '绑定Action后表情值不一致')
        _require(all(animation._animated_value(hub,*p)==v for p,v in before.items() if p not in allowed), '非目标控制输入被动画覆盖')
        _require(fm._snapshot(rig)==rig_before and scene.frame_current+scene.frame_subframe==frame_raw, '记录改变了rig输入或当前帧')
        if binding['action']:binding['action'].use_fake_user=True
        return dict(action=action.name,slot=slot.identifier,frame=frame,channels=len(rows),original_action_retained=True)
    except Exception as error:
        recovery_errors=[]
        def attempt(label,operation):
            try:operation()
            except Exception as recovery:recovery_errors.append(label+': '+str(recovery))
        attempt('恢复hub Action绑定',lambda:animation._restore_binding(hub,binding))
        attempt('刷新恢复后的旧Action',lambda:fm._update(rig))
        attempt('恢复hub输入',lambda:[animation._restore_value(hub,*p,v) for p,v in before.items()])
        attempt('恢复旧fake user',lambda:setattr(binding['action'],'use_fake_user',old_fake) if binding['action'] else None)
        attempt('移除本次候选Action',lambda:_remove_owned_action(action,recovery_errors))
        attempt('刷新恢复状态',lambda:fm._update(rig))
        if recovery_errors:
            raise RuntimeError(str(error)+'；表情记录恢复未完整核实：'+'; '.join(recovery_errors)) from error
        raise
