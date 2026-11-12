# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent, independent editable Key drafts and new controlled revisions.

Only explicitly selected Key coordinates are editable. Source Objects are real
ID references; Mesh/Key identity uses saved names plus content (no extra data
users or source UUID writes). New revisions deliberately share old board inputs.
"""
import copy
import json
import math
import uuid

import bpy
import numpy as np

from . import corrective_authoring as sig, face_expression_controls as core
from . import face_graph_board as graph, face_mouth_pad as pad, face_modes as fm
from . import face_mouth_core as mouth_core
from . import mode_animation as anim

STATE_KEY = 'c94_face_key_revision_v1'
SCHEMA = 'c94.face-key-revision.v1'
_require = core._require


def _coordinates(block):
    points = np.empty((len(block.data), 3), dtype=np.float32)
    block.data.foreach_get('co', points.ravel())
    _require(np.isfinite(points).all(), 'Key坐标含非有限值')
    return sig._array_hash(points)


def _curve(curve):
    return [sig._rna(curve), sig._rna(curve.driver) if curve.driver else None,
            [[sig._rna(v), [sig._rna(t) for t in v.targets]] for v in curve.driver.variables] if curve.driver else [],
            [sig._rna(k) for k in curve.keyframe_points], [list(k.co) for k in curve.sampled_points],
            [sig._rna(m) for m in curve.modifiers], curve.group.name if curve.group else None]


def _action(action):
    if action is None:
        return None
    layers = []
    for layer in action.layers:
        strips = []
        for strip in layer.strips:
            _require(strip.type == 'KEYFRAME', '当前修正作者不能冻结非Keyframe Action条带')
            bags = []
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag is not None:
                    bags.append([slot.identifier, slot.handle, [_curve(f) for f in bag.fcurves]])
            strips.append([sig._rna(strip), bags])
        layers.append([sig._rna(layer), strips])
    return [action.name, action.use_fake_user, layers]


def _animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    def strips(items):
        return [[sig._rna(s), _action(s.action), strips(s.strips) if s.type == 'META' else []] for s in items]
    return [sig._rna(ad), _action(ad.action), [_curve(f) for f in ad.drivers],
            [[sig._rna(t), strips(t.strips)] for t in ad.nla_tracks]]


def _object(obj):
    return {'world': sig._plain(obj.matrix_world), 'basis': sig._plain(obj.matrix_basis),
            'rotation_mode': obj.rotation_mode,
            'channels': {f: sig._plain(getattr(obj, f)) for f in anim._OBJECT_CHANNELS},
            'parent': sig._plain(obj.parent), 'parent_type': obj.parent_type, 'parent_bone': obj.parent_bone,
            'parent_inverse': sig._plain(obj.matrix_parent_inverse),
            'modifiers': [sig._rna(m) for m in obj.modifiers],
            'properties': sig._plain(dict(obj.items())),
            'material_slots': [[s.link, sig._plain(s.material)] for s in obj.material_slots]}


def _keys(obj, editable=()):
    keys = obj.data.shape_keys
    _require(keys is not None and keys.use_relative, '需要完整普通relative Key数据')
    rows = []
    for block in keys.key_blocks:
        fields = {k: sig._plain(getattr(block, k)) for k in
                  ('name', 'value', 'mute', 'slider_min', 'slider_max', 'interpolation', 'vertex_group')}
        rows.append([fields, block.relative_key.name,
                     None if block.name in editable and block != keys.reference_key else _coordinates(block)])
    settings = sig._rna(keys)
    # ID names are validated against each role's saved reference, not compared
    # between independent copies. Animation is deliberately rebound only on new copies.
    for field in ('name', 'name_full', 'user', 'animation_data'):
        settings.pop(field, None)
    return [settings, rows, sig._plain(dict(keys.items()))]


def _source_fingerprint(obj):
    return sig._hash({'mesh': sig._mesh_signature(obj.data), 'weights': sig._weights(obj),
                      'locks': [g.lock_weight for g in obj.vertex_groups], 'object': _object(obj),
                      'mesh_properties': sig._plain(dict(obj.data.items())), 'keys': _keys(obj),
                      'key_animation': _animation(obj.data.shape_keys),
                      'show_only_shape_key': obj.show_only_shape_key})


def _draft_fingerprint(obj, editable):
    return sig._hash({'mesh': sig._mesh_signature(obj.data, coordinates=False),
                      'weights': sig._weights(obj), 'locks': [g.lock_weight for g in obj.vertex_groups],
                      'object': _object(obj), 'mesh_properties': sig._plain(dict(obj.data.items())),
                      'keys': _keys(obj, editable)})


def _hub_fingerprint(hub):
    kind = core._control_type(hub)
    if kind == 'MOUTH': pad._read(hub)
    if kind == 'GRAPH': graph._read(hub)
    return sig._hash({'object': _object(hub), 'animation': _animation(hub), 'kind': kind})


def _member(obj, *, draft=False):
    core._local(obj, 'MESH')
    _require(obj.data.users == 1 and obj.data.is_editable and not obj.data.library and not obj.data.override_library
             and obj.animation_data is None and obj.data.animation_data is None and not obj.constraints,
             '修正作者需要本地单用户Mesh，无Object/Mesh动画或对象约束；源Key受管驱动保留')
    keys = obj.data.shape_keys
    _require(keys is not None and keys.use_relative and keys.users == 1 and keys.user == obj.data
             and not keys.library and not keys.override_library, 'ShapeKeys所有权必须独立且本地')
    if draft:
        _require(keys.animation_data is None, '草稿不能增加driver、Action或NLA')


def _zero(group, hub, members):
    _require(all(hub[r['property']] == 0 for r in group['channels']), '所有源hub参数须为0')
    for obj in members:
        _require(not obj.show_only_shape_key and all(math.isfinite(k.value) and k.value == 0
                 for k in obj.data.shape_keys.key_blocks if k != obj.data.shape_keys.reference_key),
                 '提交前关闭仅显示活动Key，所有非Basis Key须归0')


def _label(name):
    _require(isinstance(name, str) and name.strip() and len(name.encode('utf8')) <= 63
             and name not in bpy.data.objects, '名称必须非空、唯一且不超过63 UTF-8字节')
    return name


def _copy(source, name):
    obj = mesh = None
    try:
        obj = source.copy()
        mesh = source.data.copy()
        obj.data = mesh
        obj.name = name
        _require(obj.name == name and mesh != source.data and mesh.shape_keys != source.data.shape_keys,
                 '副本名称或Mesh/Key隔离失败')
        mesh.use_fake_user = False
        keys = mesh.shape_keys
        keys.use_fake_user = False
        keys.animation_data_clear()  # Only this independently copied Key ID.
        for key in keys.key_blocks:
            if key != keys.reference_key: key.value = 0.0
        obj.show_only_shape_key = False
        bpy.context.scene.collection.objects.link(obj)
        return obj
    except Exception:
        if obj is not None: bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == int(mesh.use_fake_user): bpy.data.meshes.remove(mesh)
        raise


def _member_name(prefix, index, token):
    suffix = '_%03d_' % index + token
    while len((prefix + suffix).encode('utf8')) > 63: prefix = prefix[:-1]
    name = prefix + suffix
    _require(name not in bpy.data.objects, '新成员名称冲突')
    return name


def _cleanup(objects, meshes):
    errors = []
    keys = [(mesh.shape_keys.name, mesh.shape_keys) for mesh in meshes if mesh.shape_keys is not None]
    for obj in reversed(objects):
        try: bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as error: errors.append('移除本次Object: ' + str(error))
    for mesh in reversed(meshes):
        try:
            _require(mesh.users == int(mesh.use_fake_user), '本次Mesh仍有真实消费者，保留供检查：' + mesh.name)
            bpy.data.meshes.remove(mesh)
        except Exception as error: errors.append('移除本次Mesh: ' + str(error))
    for name, key in keys:
        try:
            remaining = bpy.data.shape_keys.get(name)
            if remaining is None:
                continue  # The native Mesh destructor already released its Key.
            _require(remaining == key and key.users == int(key.use_fake_user) and key.user is None,
                     '本次Key仍有真实消费者，保留供检查：' + name)
            bpy.data.batch_remove(ids=(key,))
        except Exception as error: errors.append('移除本次Key: ' + str(error))
    return errors


def _load(controller):
    core._local(controller, 'EMPTY')
    try:
        record = json.loads(controller[STATE_KEY])
        _require(isinstance(record, dict) and set(record) == {'schema', 'revision_id', 'status', 'channels',
            'group', 'neutral_fingerprint', 'hub_fingerprint', 'sources', 'drafts', 'frame', 'unit_scale'}
            and record['schema'] == SCHEMA and record['status'] in {'draft', 'committed'}, '修正草稿记录字段/版本无效')
        hub, rig, scene = controller['source_hub'], controller['rig'], controller['scene']
        _require(isinstance(hub, bpy.types.Object) and isinstance(rig, bpy.types.Object)
                 and isinstance(scene, bpy.types.Scene) and isinstance(record['sources'], list)
                 and isinstance(record['drafts'], list) and len(record['sources']) == len(record['drafts']) > 0,
                 '修正草稿源/rig/scene引用无效')
        sources = [controller['source_%03d' % i] for i in range(len(record['sources']))]
        drafts = [controller['draft_%03d' % i] for i in range(len(record['drafts']))]
        _require(len(set(sources)) == len(sources) and len(set(drafts)) == len(drafts)
                 and not set(sources).intersection(drafts), '源/草稿引用重复或交叠')
        for obj, row in zip([*sources, *drafts], [*record['sources'], *record['drafts']]):
            _require(isinstance(row, dict) and set(row) == {'object', 'mesh', 'keys', 'fingerprint', 'editable'}
                     and isinstance(obj, bpy.types.Object) and obj.type == 'MESH' and obj.name == row['object']
                     and obj.data.name == row['mesh'] and obj.data.shape_keys is not None
                     and obj.data.shape_keys.name == row['keys'], '源/草稿Object或Mesh/Key身份已改变')
        return record, hub, rig, scene, sources, drafts
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise ValueError('修正草稿记录或ID引用损坏，原数据保留') from error


def _verify(controller):
    record, hub, rig, scene, sources, drafts = _load(controller)
    _require(record['status'] == 'draft' and 'committed_hub' not in controller, '该草稿已经提交，不重复创建受管候选')
    _require(bpy.context.scene == scene and [scene.frame_current, scene.frame_subframe] == record['frame']
             and scene.unit_settings.scale_length == record['unit_scale'], '场景、帧或单位已改变，请回到准备状态')
    group, actual_rig, members = core._read(hub)
    _require(actual_rig == rig and members == sources and group == record['group'], '源hub成员顺序或通道映射已改变')
    core._baseline(rig, record['neutral_fingerprint'])
    _zero(group, hub, sources)
    _require(_hub_fingerprint(hub) == record['hub_fingerprint'], '源hub/控制引用/动画已改变，不覆盖')
    channels = record['channels']
    _require(isinstance(channels, list) and channels and len(set(channels)) == len(channels)
             and set(channels) <= {r['name'] for r in group['channels']}, '允许修正的语义通道记录无效')
    for index, (source, draft, before_s, before_d) in enumerate(zip(sources, drafts, record['sources'], record['drafts'])):
        _member(source); _member(draft, draft=True)
        _require(source.mode == 'OBJECT' and draft.mode == 'OBJECT', '提交前请结束源/草稿的Edit或Sculpt模式')
        _zero(group, hub, [draft])
        expected = [core._key_name(r, index) for r in group['channels'] if r['name'] in channels]
        _require(before_s['editable'] == expected and before_d['editable'] == expected, '草稿可编辑Key范围已改变')
        _require(_source_fingerprint(source) == before_s['fingerprint'], '源Mesh/Key/权重/变换/动画改变，不提交')
        _require(_draft_fingerprint(draft, expected) == before_d['fingerprint'],
                 '草稿修改超出目标Key坐标；Basis/其它Key/拓扑UV/权重/属性/变换须保持')
        for name in expected: _coordinates(draft.data.shape_keys.key_blocks[name])
    return record, hub, rig, sources, drafts


def inspect(controller):
    record, hub, rig, _, sources, drafts = _load(controller)
    committed = controller.get('committed_hub')
    return dict(source_hub=hub.name, rig=rig.name, members=[o.name for o in drafts],
                source_members=[o.name for o in sources], channels=record['channels'], status=record['status'],
                committed_hub=committed.name if isinstance(committed, bpy.types.Object) else None,
                note='仅报告持久草稿状态；commit会重新检查源/编辑范围/基线。只编辑已列Key坐标。')


def prepare(hub, channels, *, name='表情Key修正草稿'):
    group, rig, sources = core._read(hub)
    core._baseline(rig, group['neutral_fingerprint'])
    _require(isinstance(channels, (list, tuple)) and channels and all(isinstance(n, str) for n in channels)
             and len(set(channels)) == len(channels) and set(channels) <= {r['name'] for r in group['channels']},
             '显式指定非空、不重复的实际语义通道')
    name = _label(name)
    _zero(group, hub, sources)
    for source in sources:
        _member(source)
        _require(source.mode == 'OBJECT', '准备前请结束源网格编辑/绘制模式')
    token = uuid.uuid4().hex
    scene = bpy.context.scene
    source_rows = []
    for index, source in enumerate(sources):
        editable = [core._key_name(r, index) for r in group['channels'] if r['name'] in channels]
        source_rows.append(dict(object=source.name, mesh=source.data.name, keys=source.data.shape_keys.name,
                                fingerprint=_source_fingerprint(source), editable=editable))
    hub_fingerprint = _hub_fingerprint(hub)
    controller, drafts, meshes = None, [], []
    try:
        for index, source in enumerate(sources):
            draft = _copy(source, _member_name(name, index, token[:8]))
            drafts.append(draft); meshes.append(draft.data)
        controller = bpy.data.objects.new(name, None)
        scene.collection.objects.link(controller)
        controller.empty_display_type = 'PLAIN_AXES'; controller.hide_render = True
        controller['source_hub'], controller['rig'], controller['scene'] = hub, rig, scene
        draft_rows = []
        for index, (source, draft) in enumerate(zip(sources, drafts)):
            controller['source_%03d' % index] = source
            controller['draft_%03d' % index] = draft
            editable = source_rows[index]['editable']
            draft_rows.append(dict(object=draft.name, mesh=draft.data.name, keys=draft.data.shape_keys.name,
                                   fingerprint=_draft_fingerprint(draft, editable), editable=editable))
        record = dict(schema=SCHEMA, revision_id=token, status='draft', channels=list(channels), group=copy.deepcopy(group),
                      neutral_fingerprint=group['neutral_fingerprint'], hub_fingerprint=hub_fingerprint,
                      sources=source_rows, drafts=draft_rows, frame=[scene.frame_current, scene.frame_subframe],
                      unit_scale=float(scene.unit_settings.scale_length))
        controller[STATE_KEY] = json.dumps(record, ensure_ascii=False, allow_nan=False)
        bpy.context.view_layer.update()
        _verify(controller)
        return controller
    except Exception as error:
        errors = _cleanup(([controller] if controller else []) + drafts, meshes)
        if errors: raise RuntimeError(str(error) + '; 草稿清理不完整：' + '; '.join(errors)) from error
        raise


def _revision_display(source, token):
    """A distinct Collection owned by the new hub; source objects stay shared.

    The board control bones (and therefore their custom shapes) are shared with
    the source revision. Duplicating the display objects would repoint those
    shared custom shapes and break the original board, so ownership is made
    explicit with a new collection while the source keeps its own collection.
    """
    _require(isinstance(source, bpy.types.Collection) and source.library is None and source.users > 0,
             '源显示集合不是本地有效集合')
    base = '修正显示_' + token
    name, index = base, 2
    while name in bpy.data.collections:
        name = base + '_' + str(index)
        index += 1
    display = bpy.data.collections.new(name)
    try:
        parents = [bpy.context.scene.collection] + list(bpy.data.collections)
        linked = False
        for parent in parents:
            if source.name in parent.children:
                parent.children.link(display)
                linked = True
        if not linked:
            bpy.context.scene.collection.children.link(display)
        for field in ('hide_viewport', 'hide_render', 'hide_select'):
            if hasattr(source, field) and hasattr(display, field):
                setattr(display, field, getattr(source, field))
        for obj in list(source.objects):
            _require(isinstance(obj, bpy.types.Object), '源显示集合含非Object引用')
            display.objects.link(obj)
        return display
    except Exception:
        bpy.data.collections.remove(display)
        raise


def _borrow(source, new_hub, rig, group, *, token=None):
    token = token or uuid.uuid4().hex[:8]
    new_group, new_rig, _ = core._read(new_hub)
    _require(new_rig == rig and [(r['name'], r['property']) for r in new_group['channels']] ==
             [(r['name'], r['property']) for r in group['channels']], '新旧通道名称/property/顺序不一致')
    if 'c94_expression_sidebar_v1' in source:
        new_hub['c94_expression_sidebar_v1'] = source['c94_expression_sidebar_v1']
    kind = core._control_type(source)
    display = None
    if kind == 'MOUTH':
        _, board, source_rig, _ = pad._read(source)
        _require(source_rig == rig, '借用的口型板属于其它rig')
        display = _revision_display(source['board_display'], token)
        try:
            new_hub[pad.STATE_KEY] = source[pad.STATE_KEY]
            new_hub['board_display'] = display
            for row in board['grid']:
                pad._driver(new_hub, row['property'], rig,
                            mouth_core._expression(row, board['travel']),
                            [('x', mouth_core._path(board['pad']) + '[0]'),
                             ('y', mouth_core._path(board['pad']) + '[1]')])
            for row in board['sliders']:
                pad._driver(new_hub, row['property'], rig,
                            mouth_core._slider_expression(board['travel']),
                            [('v', mouth_core._path(row['bone']) + '[1]')])
            core._update(new_hub, rig); pad._read(new_hub)
        except Exception:
            try:
                if new_hub.get('board_display') == display:
                    del new_hub['board_display']
                for key in (pad.STATE_KEY, graph.STATE_KEY):
                    if key in new_hub:
                        del new_hub[key]
            except Exception:
                pass
            bpy.data.collections.remove(display)
            raise
    elif kind == 'GRAPH':
        _, board, source_rig, _ = graph._read(source)
        _require(source_rig == rig and len(board['channels']) == len(group['channels']), '借用的图形板身份/通道不一致')
        display = _revision_display(source['board_display'], token)
        try:
            new_hub[graph.STATE_KEY] = source[graph.STATE_KEY]
            new_hub['board_display'] = display
            for channel, row in zip(group['channels'], board['channels']):
                pad._driver(new_hub, channel['property'], rig,
                            mouth_core._slider_expression(board['travel']),
                            [('v', graph._bone_path(row['bone']) + '[1]')])
            core._update(new_hub, rig); graph._read(new_hub)
        except Exception:
            try:
                if new_hub.get('board_display') == display:
                    del new_hub['board_display']
                for key in (pad.STATE_KEY, graph.STATE_KEY):
                    if key in new_hub:
                        del new_hub[key]
            except Exception:
                pass
            bpy.data.collections.remove(display)
            raise
    new_hub['controls_source'] = source
    new_hub['c94_key_revision_control_semantics'] = 'shared original native control bones/Action; no new bones or copied hub Action' if kind != 'HUB' else 'plain new hub at zero; original hub Action not copied'
    return display


def _refresh(rig, *hubs):
    for hub in hubs: hub.update_tag(refresh={'OBJECT'})
    rig.update_tag(refresh={'OBJECT', 'DATA'})
    bpy.context.view_layer.update()


def commit(controller, *, name='修正后表情控制'):
    record, source, rig, sources, drafts = _verify(controller)
    name = _label(name)
    before = anim._snapshot(rig)
    old = controller[STATE_KEY]
    new_members, meshes, new_hub, new_display = [], [], None, None
    try:
        for index, draft in enumerate(drafts):
            obj = _copy(draft, _member_name(name, index, record['revision_id'][:8]))
            new_members.append(obj); meshes.append(obj.data)
        channels = [dict(name=row['name'], property=row['property'],
                         keys=[core._key_name(row, i) for i in range(len(new_members))]) for row in record['group']['channels']]
        new_hub = core._bind(rig, new_members, channels, group_id='revision-' + record['revision_id'],
                             fingerprint=record['neutral_fingerprint'], name=name)
        new_display = _borrow(source, new_hub, rig, record['group'],
                              token=record['revision_id'][:8])
        # _bind uses the plain-hub update before the board marker exists. Keep
        # unkeyed operator inputs intact if that TIME tag reapplied the old rig Action.
        if anim._snapshot(rig) != before:
            graph._restore_inputs(rig, before)
        _refresh(rig, source, new_hub)
        _require(anim._snapshot(rig) == before, '创建修正控制改变了原rig输入')
        _verify(controller)
        _require(all(_draft_fingerprint(new, row['editable']) == row['fingerprint']
                     for new, row in zip(new_members, record['drafts'])), '新候选改变了草稿不可编辑域')
        for index, (new, draft) in enumerate(zip(new_members, drafts)):
            _require(_keys(new)[1] == _keys(draft)[1], '修正后候选没有保留全部Key坐标/关系')
        controller['committed_hub'] = new_hub
        committed = dict(record, status='committed')
        controller[STATE_KEY] = json.dumps(committed, ensure_ascii=False, allow_nan=False)
        return new_hub, new_members
    except Exception as error:
        errors = []
        def attempt(label, operation):
            try: operation()
            except Exception as recovery: errors.append(label + ': ' + str(recovery))
        def restore_metadata():
            if 'committed_hub' in controller: del controller['committed_hub']
            controller[STATE_KEY] = old
        attempt('恢复草稿controller', restore_metadata)
        if new_hub is None:
            partial = bpy.data.objects.get(name)
            if partial is not None:
                if partial.type == 'EMPTY' and partial.get('rig') == rig:
                    new_hub = partial
                else:
                    errors.append('同名新Object未能证明属于本次bind，保留供检查')
        errors += _cleanup(([new_hub] if new_hub else []) + new_members, meshes)
        if new_display is not None:
            attempt('移除本次修正显示集合', lambda: bpy.data.collections.remove(new_display))
        attempt('恢复实际rig输入', lambda: graph._restore_inputs(rig, before))
        attempt('刷新恢复输入', lambda: _refresh(rig, source))
        attempt('检查源/草稿仍有效', lambda: _verify(controller))
        attempt('检查rig输入', lambda: _require(anim._snapshot(rig) == before, 'rig输入未恢复'))
        if errors: raise RuntimeError(str(error) + '; 提交恢复未完整核实：' + '; '.join(errors)) from error
        raise
