# SPDX-License-Identifier: GPL-3.0-or-later
"""Authoring create/bind entry over the shared daily expression core.

Daily read/set/switch/record are re-exported from face_expression_core so the
author UI and the embedded daily Text execute the same native operations.
Creation and capture-group binding remain authoring-only.
"""
import json

import bpy

from . import face_expression_core as core, face_modes as fm

STATE_KEY = core.STATE_KEY
SCHEMA = core.SCHEMA
LEGACY_SCHEMA = core.LEGACY_SCHEMA
EXPRESSION = core.EXPRESSION
sig = core.sig
_require = core._require
_path = core._path
_key_name = core._key_name
_control_type = core._control_type
_update = core._update
_local = core._local
_face_static = core._face_static
_baseline = core._baseline
_read = core._read
inspect = core.inspect
set_values = core.set_values
_preflight_mode_change = core._preflight_mode_change
switch = core.switch
record_current = core.record_current


def _driver(key, hub, rig, prop):
    curve = key.driver_add('value')
    # Shape Key driver_add creates a 0..1 mapping whose endpoint epsilon
    # snaps small expression values. No mapping means direct expression output.
    while curve.keyframe_points:
        curve.keyframe_points.remove(curve.keyframe_points[-1], fast=True)
    curve.update()
    driver = curve.driver
    driver.type = 'SCRIPTED'
    driver.expression = EXPRESSION
    for name, obj, path in [('w', hub, _path(prop)),
                            ('m', rig, fm._property_path(rig.pose.bones[fm.MODE_BONE], fm.MODE_PROPERTY))]:
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id_type = 'OBJECT'
        variable.targets[0].id = obj
        variable.targets[0].data_path = path
    return curve


def create(rig, members, *, name='角色表情同步控制'):
    """Bind only explicitly supplied coherent KEY-neutral capture candidates."""
    _local(rig, 'ARMATURE')
    fingerprint = fm.hash_record(fm._read(rig)['neutral'])
    _baseline(rig, fingerprint)
    _require(isinstance(members, (list, tuple)) and members and len(set(members)) == len(members),
             '明确提供不重复的同步Mesh列表')
    _require(isinstance(name, str) and name.strip() and len(name.encode('utf8')) <= 63
             and name not in bpy.data.objects, '控制对象名称须非空、唯一且不超过63 UTF-8字节')
    records = []
    for obj in members:
        _local(obj, 'MESH')
        _require(obj.mode == 'OBJECT' and obj.data.users == 1 and obj.data.is_editable
                 and not obj.data.library and not obj.data.override_library, '同步Mesh须为本地单用户对象模式')
        record = json.loads(obj.get('c94_face_capture_group_v1', '{}'))
        _require(record.get('schema') == 'c94.face-capture-group.v1' and record.get('neutral_mode') == 'KEY'
                 and record.get('rig') == rig.name and len(record.get('sources', [])) == len(members),
                 '需要本rig明确KEY中立的完整捕获组；不从名称猜测其它对象')
        keys = obj.data.shape_keys
        _require(keys is not None and keys.use_relative and keys.animation_data is None
                 and all(k.value == 0 for k in keys.key_blocks if k != keys.reference_key),
                 '所有非Basis Key先清零；已有Key动画/driver保持，不覆盖')
        _require(all(n in keys.key_blocks for n in record.get('samples', [])) and record.get('samples'),
                 '捕获组Key列表不完整')
        _require([m.object for m in obj.modifiers if m.type == 'ARMATURE'] == [rig], '同步Mesh须绑定明确同rig')
        records.append(record)
    first = records[0]
    _require(all(r['group_id'] == first['group_id'] and r['samples'] == first['samples'] for r in records),
             '成员不属于同一采样组或表情列表不一致')
    channels = [{'name':n, 'property':'表情_%02d'%i, 'keys':[n]*len(members)} for i,n in enumerate(first['samples'])]
    return _bind(rig, members, channels, group_id=first['group_id'], fingerprint=fingerprint, name=name)


def _bind(rig, members, channels, *, group_id, fingerprint, name):
    """Shared writer after caller validates explicit sources/mapping; no inference."""
    _require(name not in bpy.data.objects, '控制对象名称已存在')
    _require(channels and len({r['name'] for r in channels}) == len(channels)
             and all(len(r['keys'])==len(members) for r in channels)
             and all(len({r['keys'][i] for r in channels})==len(channels) for i in range(len(members))),
             '映射通道或每成员Key重复/缺失')
    record = fm._read(rig)
    manual = {(item['keys'].as_pointer(), item['key'].name)
              for item in fm._resolve_identity_items(rig, record['identity'])}
    managed = {(obj.data.shape_keys.as_pointer(), row['keys'][i])
               for i,obj in enumerate(members) for row in channels}
    _require(not (managed & manual),
             '目标Key仍在手工Key中立范围内；请保留原记录，明确重录不含这些驱动Key的中立后再建立控制')
    hub = None; added = []
    try:
        hub = bpy.data.objects.new(name, None)
        bpy.context.scene.collection.objects.link(hub)
        hub.empty_display_type = 'PLAIN_AXES'; hub.empty_display_size = .03; hub.hide_render = True
        hub['rig'] = rig
        for i, obj in enumerate(members): hub['member_%03d' % i] = obj
        for row in channels:
            prop = row['property']; hub[prop] = 0.0
            hub.id_properties_ui(prop).update(min=0., max=1., soft_min=0., soft_max=1., description=row['name'])
            for i,obj in enumerate(members):
                key = obj.data.shape_keys.key_blocks[row['keys'][i]]
                added.append((obj.data.shape_keys, key))
                _driver(key, hub, rig, prop)
        hub[STATE_KEY] = json.dumps(dict(schema=SCHEMA, group_id=group_id, channels=channels,
                member_count=len(members), neutral_fingerprint=fingerprint), ensure_ascii=False)
        _update(hub, rig); _read(hub)
        from . import face_expression_layout
        face_expression_layout.install(hub)
        return hub
    except Exception:
        for keys, key in reversed(added): key.driver_remove('value'); key.value = 0
        for obj in members:
            keys = obj.data.shape_keys
            if keys.animation_data is not None and not keys.animation_data.drivers:
                keys.animation_data_clear()
        if hub is not None: bpy.data.objects.remove(hub, do_unlink=True)
        fm._update(rig)
        raise
