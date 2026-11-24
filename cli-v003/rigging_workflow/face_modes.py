# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit C94 face input baselines, mode changes and display-only spaces.

The native selector is partial gating, not a new all-face exclusivity solver.
An input baseline is declared by the operator; it is not an anatomical test.
"""
import hashlib
import json
import sys
import math

import bpy

from . import control_panel as panel, mode_semantics

STATE_KEY = 'c94_face_modes_v1'
SCHEMA = 'c94.face-modes.v1'
SCHEMA_V2 = 'c94.face-modes.v2'
IDENTITY_SCHEMA = 'c94.face-modes.identity.v1'
REF_PREFIX = 'c94_face_modes_ref_'
MODE_BONE = '骨架设置'
MODE_PROPERTY = '脸部控制形态键-骨骼'
SPACES = {'panel': '表情面板', 'basic': '面部', 'main': '面部-主控', 'secondary': '面部-次要'}
_FIELDS = ('location', 'rotation_quaternion', 'rotation_euler', 'rotation_axis_angle', 'scale')
_NOTE = '原生模式只门控部分路径；骨控模式仍可消费Key。输入基线不证明实际无表情或完整面部已制作。'


def hash_record(value):
    """Same canonical hash used by the authoring core for neutral fingerprints."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _rig(rig, *, data=False):
    panel._writable(rig, data=data)
    _require(rig.mode in {'OBJECT', 'POSE'}, '先退出骨架编辑模式')
    settings = rig.pose.bones.get(MODE_BONE)
    _require(settings is not None and MODE_PROPERTY in settings, '当前角色没有C94面部模式属性')
    value = settings[MODE_PROPERTY]
    _require(type(value) in (bool, int, float) and value in (0, 1), '面部模式须为完整0或1端点')
    return rig


def _mode(rig):
    return 'KEY' if rig.pose.bones[MODE_BONE][MODE_PROPERTY] == 0 else 'BONE'


def _bone_names(rig):
    groups = [rig.data.collections_all.get(name) for name in SPACES.values()]
    _require(all(c is not None and len(c.bones) for c in groups), '当前角色没有完整的两套面部控件空间')
    # The board root is placement, not an expression input. Include nonselectable
    # combination parents: they can affect the selectable controls downstream.
    return sorted({b.name for c in groups for b in c.bones if b.name != '表情面板'})


def _properties(rig):
    return sorted((r['bone'], r['property']) for r in mode_semantics.available(rig)
                  if r['group'] == '面部' and r['property'] != MODE_PROPERTY)


def _property_path(pb, key):
    return pb.path_from_id() + '[' + json.dumps(key, ensure_ascii=False) + ']'


def bone_location_path(name):
    return 'pose.bones[' + json.dumps(name, ensure_ascii=False) + '].location'


def _related_meshes(rig):
    result = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        keys = obj.data.shape_keys
        driven = keys and keys.animation_data and any(
            target.id == rig for f in keys.animation_data.drivers
            for variable in f.driver.variables for target in variable.targets)
        if driven or any(m.type == 'ARMATURE' and m.object == rig for m in obj.modifiers):
            result.append(obj)
    return result


def _owned_face_bones(rig):
    """Physical face-control layers, shared with the daily Key facade."""
    groups = [rig.data.collections_all.get(SPACES[k]) for k in ('basic', 'main', 'secondary')]
    _require(all(c is not None and len(c.bones) for c in groups), '缺少贴脸骨控分层')
    return sorted({b.name for c in groups for b in c.bones})


def _record(rig):
    raw = rig.get(STATE_KEY)
    _require(isinstance(raw, str), '先显式记录当前面部中立输入')
    try:
        record = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('面部中立记录损坏；原记录保留') from exc
    _require(isinstance(record, dict) and record.get('schema') in {SCHEMA, SCHEMA_V2}
             and isinstance(record.get('neutral'), dict), '面部中立记录版本无效')
    return record


def needs_migration(rig):
    raw = rig.get(STATE_KEY)
    if not isinstance(raw, str):
        return False
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(record, dict) and record.get('schema') == SCHEMA


def _ref_props(index):
    return (REF_PREFIX + '%03d_object' % index, REF_PREFIX + '%03d_key' % index)


def _identity_snapshot(rig):
    return {name: rig[name] for name in list(rig.keys()) if name.startswith(REF_PREFIX)}


def _restore_identity(rig, snapshot):
    for name in [name for name in list(rig.keys()) if name.startswith(REF_PREFIX)]:
        del rig[name]
    for name, value in snapshot.items():
        rig[name] = value


def _write_identity(rig, items):
    """Store persistent Object/Key ID pointers; names remain display metadata."""
    snapshot = _identity_snapshot(rig)
    rows, keep = [], set()
    try:
        for index, item in enumerate(items):
            obj_prop, key_prop = _ref_props(index)
            keep.update((obj_prop, key_prop))
            rig[obj_prop] = item['object']
            rig[key_prop] = item['keys']
            key = item['key']
            blocks = list(item['keys'].key_blocks)
            rows.append({'ref': index, 'object': obj_prop, 'key': key_prop,
                         'key_block': key.name, 'key_index': blocks.index(key),
                         'relative_key': key.relative_key.name if key.relative_key else None,
                         'vertex_group': key.vertex_group,
                         'slider_min': float(key.slider_min), 'slider_max': float(key.slider_max)})
        for name in [name for name in list(rig.keys()) if name.startswith(REF_PREFIX) and name not in keep]:
            del rig[name]
        return {'schema': IDENTITY_SCHEMA, 'manual_keys': rows}
    except Exception:
        _restore_identity(rig, snapshot)
        raise


def _base_block_check(keys, key, label):
    _require(keys.use_relative and key is not None and key != keys.reference_key,
             '请选择存在的相对表情Key，不含基型：' + label)
    _require(not keys.animation_data or not any(
        f.data_path == key.path_from_id('value') for f in keys.animation_data.drivers),
        '驱动Key由其控件管理，不能纳入直接复位范围：' + label)


def _resolve_identity_items(rig, identity):
    _require(isinstance(identity, dict) and identity.get('schema') == IDENTITY_SCHEMA
             and isinstance(identity.get('manual_keys'), list), '面部中立身份索引无效')
    related = set(_related_meshes(rig))
    items, seen = [], set()
    for row in identity['manual_keys']:
        _require(isinstance(row, dict) and set(row) >= {
            'ref', 'object', 'key', 'key_block', 'key_index'}, '面部中立身份行无效')
        obj = rig.get(row['object'])
        keys = rig.get(row['key'])
        _require(isinstance(obj, bpy.types.Object) and obj.type == 'MESH' and obj in related,
                 '手工面部Key对象ID已失效或不关联当前角色')
        _require(isinstance(keys, bpy.types.Key) and obj.data.shape_keys == keys,
                 '手工面部Key数据ID已失效或不属于该对象')
        key = keys.key_blocks.get(row['key_block'])
        _require(key is not None,
                 '声明的手工面部Key块名已丢失；请显式更新/重绑声明，不按索引认领新块')
        _base_block_check(keys, key, obj.name + '/' + row['key_block'])
        if row.get('relative_key') is not None:
            _require(key.relative_key is not None and key.relative_key.name == row['relative_key'],
                     '手工面部Key块相对基型已改变')
        _require(key.vertex_group == row.get('vertex_group', key.vertex_group)
                 and abs(float(key.slider_min) - float(row.get('slider_min', key.slider_min))) < 1e-7
                 and abs(float(key.slider_max) - float(row.get('slider_max', key.slider_max))) < 1e-7,
                 '手工面部Key块内容身份已改变')
        ref = row['ref']
        _require(type(ref) is int and ref not in seen, '手工面部Key身份索引重复')
        seen.add(ref)
        _require(math.isfinite(key.value), '手工形态键值不是有限数')
        items.append({'ref': ref, 'object': obj, 'keys': keys, 'key': key, 'value': float(key.value)})
    return items


def rebind_manual_key_block(rig, *, ref, new_name):
    """Explicitly rename one declared ShapeKey block after its real old block is proven."""
    _rig(rig)
    _require(type(ref) is int and isinstance(new_name, str) and new_name.strip()
             and len(new_name.encode('utf8')) <= 63, '明确手工Key身份ref和新块名')
    record = _record(rig)
    _require(record['schema'] == SCHEMA_V2 and isinstance(record.get('identity'), dict),
             '先显式迁移到面部身份v2')
    rows = record['identity'].get('manual_keys')
    _require(isinstance(rows, list), '面部身份索引无效')
    positions = [index for index, row in enumerate(rows) if row.get('ref') == ref]
    _require(len(positions) == 1, '手工Key身份ref不存在或重复')
    position = positions[0]
    row = rows[position]
    obj = rig.get(row['object'])
    keys = rig.get(row['key'])
    _require(isinstance(obj, bpy.types.Object) and obj.type == 'MESH'
             and isinstance(keys, bpy.types.Key) and obj.data.shape_keys == keys,
             '手工Key对象或Key数据ID已失效')
    old_name = row['key_block']
    _require(old_name in keys.key_blocks,
             '原声明Key块名已丢失；不能按索引推断，请重新记录中立')
    _require(new_name == old_name or new_name not in keys.key_blocks,
             '目标ShapeKey块名已存在')
    key = keys.key_blocks[old_name]
    neutral = record.get('neutral')
    manual = neutral.get('manual_keys') if isinstance(neutral, dict) else None
    _require(isinstance(manual, list) and len(manual) == len(rows), '面部中立手工Key记录与身份索引不一致')
    neutral_row = manual[position]
    _require(isinstance(neutral_row, dict) and set(neutral_row) == {'object', 'data', 'key', 'value'}
             and neutral_row['object'] == obj.name and neutral_row['data'] == keys.name
             and neutral_row['key'] == old_name, '面部中立手工Key记录与真实身份不一致')
    old_state = rig[STATE_KEY]
    old_block = key.name
    old_fingerprint = hash_record(neutral)
    hub_records = []
    for obj_candidate in bpy.data.objects:
        raw = obj_candidate.get('c94_face_expression_controls_v1')
        if raw and obj_candidate.get('rig') == rig:
            hub_records.append((obj_candidate, raw))
    revision_records = []
    for obj_candidate in bpy.data.objects:
        raw = obj_candidate.get('c94_face_key_revision_v1')
        if raw and obj_candidate.get('rig') == rig:
            revision_records.append((obj_candidate, raw))
    # Authoring is an optional consumer of this shared daily core. Never pull
    # its construction closure into the self-contained portable runtime.
    revision_module = sys.modules.get(__package__ + '.face_key_revision')
    old_hub_fingerprints = {}
    revision_gates = {}
    _require(not revision_records or revision_module is not None,
             '存在Key修订草稿，请加载完整Key作者工具后重绑；原记录保持')
    if revision_module is not None:
        for hub, _raw in hub_records:
            try:
                old_hub_fingerprints[hub] = revision_module._hub_fingerprint(hub)
            except Exception:
                pass
        for controller, _raw in revision_records:
            try:
                value, source_hub, _controller_rig, _controller_scene, sources, drafts = revision_module._load(controller)
            except Exception:
                continue
            if value.get('status') != 'draft' or source_hub not in old_hub_fingerprints:
                continue
            group_value = value.get('group')
            if (value.get('neutral_fingerprint') != old_fingerprint
                    or not isinstance(group_value, dict)
                    or group_value.get('neutral_fingerprint') != old_fingerprint
                    or value.get('hub_fingerprint') != old_hub_fingerprints[source_hub]):
                continue
            if any(source is obj for source in sources) or any(draft is obj for draft in drafts):
                # This rebind changes a Key on a revision source/draft itself;
                # its saved non-editable fingerprint will change. Do not
                # re-sign the prepared revision merely because the pre-change
                # fingerprints still match.
                continue
            safe = True
            try:
                for source, source_row in zip(sources, value.get('sources', [])):
                    if revision_module._source_fingerprint(source) != source_row['fingerprint']:
                        safe = False
                        break
                if safe:
                    for draft, draft_row in zip(drafts, value.get('drafts', [])):
                        if (revision_module._draft_fingerprint(draft, draft_row['editable'])
                                != draft_row['fingerprint']):
                            safe = False
                            break
            except Exception:
                safe = False
            if safe:
                revision_gates[controller] = (value, source_hub, _raw)
    pose_record = rig.get('c94_face_pose_library_v1')
    written = []
    try:
        if key.name != new_name:
            key.name = new_name
        row['key_block'] = key.name
        row['key_index'] = list(keys.key_blocks).index(key)
        neutral_row['key'] = key.name
        new_fingerprint = hash_record(neutral)
        updated = dict(record)
        updated['identity'] = record['identity']
        updated['neutral'] = neutral
        rig[STATE_KEY] = json.dumps(updated, ensure_ascii=False, allow_nan=False)
        written.append((rig, STATE_KEY, old_state))
        for hub, raw in hub_records:
            value = json.loads(raw)
            if value.get('neutral_fingerprint') != old_fingerprint:
                continue
            value['neutral_fingerprint'] = new_fingerprint
            hub['c94_face_expression_controls_v1'] = json.dumps(value, ensure_ascii=False, allow_nan=False)
            written.append((hub, 'c94_face_expression_controls_v1', raw))
        new_hub_fingerprints = {}
        if revision_module is not None:
            for hub in old_hub_fingerprints:
                try:
                    new_hub_fingerprints[hub] = revision_module._hub_fingerprint(hub)
                except Exception:
                    pass
        for controller in revision_gates:
            value, source_hub, raw = revision_gates[controller]
            new_hub_fingerprint = new_hub_fingerprints.get(source_hub)
            if new_hub_fingerprint is None:
                continue
            value['neutral_fingerprint'] = new_fingerprint
            value['group']['neutral_fingerprint'] = new_fingerprint
            value['hub_fingerprint'] = new_hub_fingerprint
            controller['c94_face_key_revision_v1'] = json.dumps(value, ensure_ascii=False, allow_nan=False)
            written.append((controller, 'c94_face_key_revision_v1', raw))
        if pose_record is not None:
            value = json.loads(pose_record)
            if value.get('neutral_fingerprint') == old_fingerprint:
                value['neutral_fingerprint'] = new_fingerprint
                previous = value.get('previous')
                if isinstance(previous, dict):
                    for item in (previous.get('before'), previous.get('after')):
                        if not isinstance(item, dict):
                            continue
                        for manual_row in item.get('manual_keys', []):
                            if (isinstance(manual_row, dict) and manual_row.get('object') == obj.name
                                    and manual_row.get('data') == keys.name and manual_row.get('key') == old_name):
                                manual_row['key'] = key.name
                rig['c94_face_pose_library_v1'] = json.dumps(value, ensure_ascii=False, allow_nan=False)
                written.append((rig, 'c94_face_pose_library_v1', pose_record))
        _read(rig)
        return {'schema': SCHEMA_V2, 'ref': ref, 'old_name': old_block,
                'new_name': key.name, 'key_index': row['key_index'],
                'neutral_preserved': True,
                'neutral_fingerprint': new_fingerprint,
                'updated_hub_count': sum(1 for owner, name, _old in written
                                         if name == 'c94_face_expression_controls_v1'),
                'updated_revision_count': sum(1 for owner, name, _old in written
                                              if name == 'c94_face_key_revision_v1'),
                'pose_library_updated': any(name == 'c94_face_pose_library_v1' for _owner, name, _old in written)}
    except Exception as error:
        recovery_errors = []
        try:
            if key.name != old_block:
                key.name = old_block
        except Exception as recovery:
            recovery_errors.append('恢复Key块名: ' + str(recovery))
        for owner, name, raw in reversed(written):
            try:
                owner[name] = raw
            except Exception as recovery:
                recovery_errors.append('恢复记录 ' + name + ': ' + str(recovery))
        try:
            rig[STATE_KEY] = old_state
        except Exception as recovery:
            recovery_errors.append('恢复面部中立记录: ' + str(recovery))
        if recovery_errors:
            raise RuntimeError(str(error) + '；手工Key重绑恢复未完整核实：' + '; '.join(recovery_errors)) from error
        raise


def _resolve_name_items(rig, declarations):
    _require(isinstance(declarations, (list, tuple)), '手工面部Key范围须为明确列表')
    related = {o.name: o for o in _related_meshes(rig)}
    items, seen = [], set()
    for index, row in enumerate(declarations):
        if isinstance(row, (tuple, list)) and len(row) == 2:
            obj_name, key_name = row
            data_name = None
        else:
            _require(isinstance(row, dict), '手工面部Key范围记录无效')
            obj_name, key_name, data_name = row.get('object'), row.get('key'), row.get('data')
        _require(isinstance(obj_name, str) and isinstance(key_name, str), '手工面部Key名称无效')
        obj = related.get(obj_name)
        _require(obj is not None and obj.data.shape_keys is not None,
                 '手工面部Key对象已不关联当前角色，请重录')
        keys = obj.data.shape_keys
        _require(data_name is None or keys.name == data_name,
                 '手工面部Key数据名称与当前对象不一致，请重录')
        key = keys.key_blocks.get(key_name)
        _base_block_check(keys, key, obj.name + '/' + key_name)
        identity = (obj.as_pointer(), keys.as_pointer(), key.name)
        _require(identity not in seen, '手工面部Key范围重复')
        seen.add(identity)
        _require(math.isfinite(key.value), '手工形态键值不是有限数')
        items.append({'ref': index, 'object': obj, 'keys': keys, 'key': key, 'value': float(key.value)})
    return items


def _resolve_legacy_items(rig, declarations):
    """Explicit migration only: names may prove identity when they are unique."""
    _require(isinstance(declarations, (list, tuple)), '旧手工面部Key范围须为明确列表')
    related = {o.name: o for o in _related_meshes(rig)}
    other_rigs = [obj for obj in bpy.data.objects if obj != rig and obj.type == 'ARMATURE'
                  and obj.get('c94_generated_rig_v1')]
    items, seen = [], set()
    for index, row in enumerate(declarations):
        _require(isinstance(row, dict), '旧手工面部Key记录字段无效')
        obj_name, data_name, key_name = row.get('object'), row.get('data'), row.get('key')
        obj = related.get(obj_name)
        _require(obj is not None and obj.data.shape_keys is not None,
                 '旧名称无法唯一解析手工面部Key对象：' + str(obj_name))
        keys = obj.data.shape_keys
        _require(keys.name == data_name, '旧名称无法证明Key数据身份；请重新记录中立')
        key = keys.key_blocks.get(key_name)
        _base_block_check(keys, key, str(obj_name) + '/' + str(key_name))
        owners = [m.object for m in obj.modifiers if m.type == 'ARMATURE' and m.object]
        driven_by_rig = keys.animation_data and any(
            target.id == rig for f in keys.animation_data.drivers
            for variable in f.driver.variables for target in variable.targets)
        _require(owners == [rig] or (not owners and driven_by_rig),
                 '旧名称对象受多个骨架影响或未明确绑定本rig，身份歧义')
        for other in other_rigs:
            _require(obj not in _related_meshes(other),
                     '旧名称对象同时关联其它角色，身份歧义：' + other.name)
        identity = (obj.as_pointer(), keys.as_pointer(), key.name)
        _require(identity not in seen, '旧手工面部Key范围重复')
        seen.add(identity)
        _require(math.isfinite(key.value), '手工形态键值不是有限数')
        items.append({'ref': index, 'object': obj, 'keys': keys, 'key': key, 'value': float(key.value)})
    return items


def _serialize_items(items):
    return [{'ref': item['ref'], 'object': item['object'].name, 'data': item['keys'].name,
             'key': item['key'].name, 'value': float(item['value'])} for item in items]


def _build_neutral(rig, items):
    bones = {}
    for name in _bone_names(rig):
        pb = rig.pose.bones[name]
        row = {field: list(getattr(pb, field)) for field in _FIELDS}
        _require(all(math.isfinite(v) for field in _FIELDS for v in row[field]), '面部控件含非有限输入')
        row['rotation_mode'] = pb.rotation_mode
        bones[name] = row
    properties = []
    for bone, key in _properties(rig):
        value = rig.pose.bones[bone][key]
        _require(type(value) in (bool, int, float) and math.isfinite(value), '面部属性含非有限输入')
        properties.append({'bone': bone, 'key': key, 'value': value})
    manual_keys = [{'object': item['object'].name, 'data': item['keys'].name,
                    'key': item['key'].name, 'value': float(item['value'])} for item in items]
    return {'bones': bones, 'properties': properties, 'manual_keys': manual_keys}


def _declarations(rig):
    raw = rig.get(STATE_KEY)
    if raw is None:
        return []
    return _record(rig)['neutral']['manual_keys']


def _manual_keys(rig, declarations=None):
    if declarations is None:
        record = _read(rig)
        return _serialize_items(_resolve_identity_items(rig, record['identity']))
    return _serialize_items(_resolve_name_items(rig, declarations))


def manual_key_options(rig):
    _rig(rig)
    record = _record(rig)
    if record['schema'] == SCHEMA_V2:
        items = _resolve_identity_items(rig, record['identity'])
        selected = {(item['object'].as_pointer(), item['keys'].as_pointer(), item['key'].name)
                    for item in items}
    else:
        selected = {(r.get('object'), r.get('data'), r.get('key'))
                    for r in record['neutral']['manual_keys'] if isinstance(r, dict)}
    result, seen = [], set()
    for obj in sorted(_related_meshes(rig), key=lambda o: o.name):
        keys = obj.data.shape_keys
        if not keys or not keys.use_relative or keys.as_pointer() in seen:
            continue
        seen.add(keys.as_pointer())
        driven = {f.data_path for f in keys.animation_data.drivers} if keys.animation_data else set()
        for key in keys.key_blocks:
            if key != keys.reference_key and key.path_from_id('value') not in driven:
                result.append({'object': obj.name, 'data': keys.name, 'key': key.name,
                               'selected': ((obj.as_pointer(), keys.as_pointer(), key.name) in selected
                                            or (obj.name, keys.name, key.name) in selected)})
    return result


def migrate_legacy_identity(rig):
    """Explicit v1->v2 identity migration; ambiguous or renamed names are refused."""
    _rig(rig)
    record = _record(rig)
    if record['schema'] == SCHEMA_V2:
        return {'migrated': False, 'schema': SCHEMA_V2,
                'manual_key_count': len(record.get('identity', {}).get('manual_keys', []))}
    declarations = record['neutral'].get('manual_keys')
    items = _resolve_legacy_items(rig, declarations)
    old_state = rig.get(STATE_KEY)
    identity_snapshot = _identity_snapshot(rig)
    try:
        identity = _write_identity(rig, items)
        migrated = dict(record)
        migrated['schema'] = SCHEMA_V2
        migrated['identity'] = identity
        rig[STATE_KEY] = json.dumps(migrated, ensure_ascii=False, allow_nan=False)
        _read(rig)
        return {'migrated': True, 'from': SCHEMA, 'to': SCHEMA_V2,
                'manual_key_count': len(items), 'neutral_preserved': True,
                'neutral_fingerprint': hash_record(migrated['neutral'])}
    except Exception:
        rig[STATE_KEY] = old_state
        _restore_identity(rig, identity_snapshot)
        raise


def _visibility(rig):
    result = {}
    for name in SPACES.values():
        collection = rig.data.collections_all.get(name)
        while collection:
            result[collection.name] = bool(collection.is_visible)
            collection = collection.parent
    return result


def _inputs(rig, declarations=None):
    bones = {}
    for name in _bone_names(rig):
        pb = rig.pose.bones[name]
        row = {field: list(getattr(pb, field)) for field in _FIELDS}
        _require(all(math.isfinite(v) for field in _FIELDS for v in row[field]), '面部控件含非有限输入')
        row['rotation_mode'] = pb.rotation_mode
        bones[name] = row
    properties = []
    for bone, key in _properties(rig):
        value = rig.pose.bones[bone][key]
        _require(type(value) in (bool, int, float) and math.isfinite(value), '面部属性含非有限输入')
        properties.append({'bone': bone, 'key': key, 'value': value})
    if declarations is None:
        record = _read(rig)
        items = _resolve_identity_items(rig, record['identity'])
    else:
        items = _resolve_name_items(rig, declarations)
    return {'bones': bones, 'properties': properties, 'manual_keys': _serialize_items(items)}


def _snapshot(rig):
    return {'inputs': _inputs(rig), 'mode': _mode(rig), 'visibility': _visibility(rig)}


def _validate_inputs(rig, values, reference=None, *, identity=None):
    """Validate the whole persisted write scope before zip-based planning."""
    try:
        _require(isinstance(values, dict) and set(values) == {'bones', 'properties', 'manual_keys'}, '面部输入记录字段无效')
        _require(sorted(values['bones']) == _bone_names(rig), '面部控件集合已改变，请重新记录中立输入')
        _require(isinstance(values['properties'], list) and
                 [(p['bone'], p['key']) for p in values['properties']] == _properties(rig),
                 '面部属性恢复范围无效，请重新记录')
        for name, row in values['bones'].items():
            _require(set(row) == {*_FIELDS, 'rotation_mode'}, '面部骨通道字段无效')
            _require(row['rotation_mode'] in rig.pose.bones[name].bl_rna.properties['rotation_mode'].enum_items.keys(),
                     '中立旋转模式无效')
            for field in _FIELDS:
                _require(len(row[field]) == len(getattr(rig.pose.bones[name], field)) and
                         all(type(v) in (int, float) and math.isfinite(v) for v in row[field]), '中立通道记录无效')
        for row in values['properties']:
            _require(set(row) == {'bone', 'key', 'value'}, '面部属性记录字段无效')
            pb = rig.pose.bones[row['bone']]
            value = row['value']
            limits = pb.id_properties_ui(row['key']).as_dict()
            _require(type(value) is type(pb[row['key']]) and math.isfinite(value) and
                     limits.get('min', -math.inf) <= value <= limits.get('max', math.inf), '面部属性数值或类型无效')
        keys = values['manual_keys']
        _require(isinstance(keys, list), '面部Key范围无效')
        if identity is not None:
            items = _resolve_identity_items(rig, identity)
            _require(len(items) == len(keys), '手工Key身份数量与中立记录不一致')
            if reference is not None:
                refs = [row.get('ref') for row in reference['manual_keys']]
                _require(len(items) == len(reference['manual_keys']) and
                         (all(ref is None for ref in refs) or
                          [item['ref'] for item in items] == refs),
                         '上次恢复的手工Key范围超出中立记录')
        else:
            _require(all(isinstance(row, dict) and set(row) >= {'object', 'key', 'value'} for row in keys),
                     '手工Key记录字段无效')
            items = _resolve_name_items(rig, [(row.get('object'), row.get('key')) for row in keys])
            if reference is not None:
                _require(len(items) == len(reference['manual_keys']),
                         '上次恢复的手工Key范围超出中立记录')
        for index, (row, item) in enumerate(zip(keys, items)):
            _require(set(row) in ({'object', 'data', 'key', 'value'},
                                  {'object', 'data', 'key', 'value', 'ref'}), '手工Key记录字段无效')
            key = item['key']
            _require(item['ref'] == (row.get('ref') if row.get('ref') is not None else index),
                     '手工Key身份索引不一致')
            _require(type(row['value']) in (int, float) and math.isfinite(row['value']) and
                     key.slider_min <= row['value'] <= key.slider_max, '手工Key恢复数值无效')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('面部输入恢复记录损坏；原数据保持') from exc


def _read(rig):
    record = _record(rig)
    _require(record['schema'] == SCHEMA_V2,
             '面部中立记录仍为旧名称schema；请显式迁移身份后再读取，歧义时拒绝')
    try:
        _require(isinstance(record.get('identity'), dict), '面部中立身份索引缺失')
        _validate_inputs(rig, record['neutral'], identity=record['identity'])
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('面部中立记录损坏；原记录保留') from exc
    return record


def _conflicts(current, neutral):
    changed = []
    for name, row in neutral['bones'].items():
        if current['bones'][name] != row:
            changed.append(name)
    for a, b in zip(current['properties'], neutral['properties']):
        if a['value'] != b['value']:
            changed.append(a['bone'] + ' / ' + a['key'])
    if len(current['manual_keys']) != len(neutral['manual_keys']):
        changed.append('手工Key范围')
    else:
        for a, b in zip(current['manual_keys'], neutral['manual_keys']):
            if a['value'] != b['value']:
                changed.append(a['data'] + ' / ' + a['key'])
    return changed


def _curves(action, slot):
    if not action:
        return []
    _require(slot is not None, '动画没有明确Action Slot，不能安全判断面部输入')
    result = []
    for layer in action.layers:
        for strip in layer.strips:
            _require(strip.type == 'KEYFRAME', '当前动画条带类型无法判断面部输入')
            bag = strip.channelbag(slot)
            if bag:
                result.extend(bag.fcurves)
    return result


def _static(id_block, paths):
    if not paths or not id_block.animation_data:
        return
    ad = id_block.animation_data
    _require(not any(f.data_path in paths for f in ad.drivers), '待写面部输入由driver管理，原驱动保持')
    curves = _curves(ad.action, ad.action_slot) if ad.action else []
    if ad.use_nla:
        def visit(strips):
            for strip in strips:
                if strip.mute:
                    continue
                if strip.action:
                    curves.extend(_curves(strip.action, strip.action_slot))
                elif strip.type == 'META':
                    visit(strip.strips)
        for track in ad.nla_tracks:
            if not track.mute:
                visit(track.strips)
    _require(not any(f.data_path in paths for f in curves), '待写面部输入有动画，请使用无冲突输入；原Action/NLA保持')


def _key_blocks(rig):
    return {obj.data.shape_keys.name: obj.data.shape_keys for obj in _related_meshes(rig) if obj.data.shape_keys}


def _identity_items_by_ref(rig):
    record = _read(rig)
    return {item['ref']: item for item in _resolve_identity_items(rig, record['identity'])}


def _preflight(rig, target, *, mode=None):
    current, paths = _inputs(rig), set()
    for name, row in target['bones'].items():
        for field in (*_FIELDS, 'rotation_mode'):
            if current['bones'][name][field] != row[field]:
                paths.add(rig.pose.bones[name].path_from_id(field))
    for before, after in zip(current['properties'], target['properties']):
        if before['value'] != after['value']:
            paths.add(_property_path(rig.pose.bones[before['bone']], before['key']))
    if mode is not None and mode != _mode(rig):
        paths.add(_property_path(rig.pose.bones[MODE_BONE], MODE_PROPERTY))
    _static(rig, paths)
    items = _identity_items_by_ref(rig)
    for index, (before, after) in enumerate(zip(current['manual_keys'], target['manual_keys'])):
        if before['value'] == after['value']:
            continue
        ref = after.get('ref', before.get('ref', index))
        item = items.get(ref)
        _require(item is not None, '手工Key身份索引已改变，请重新记录中立')
        data = item['keys']
        _require(not data.library and not data.override_library, '关联形态键只读，原数据保持')
        owners = [o for o in bpy.data.objects if o.type == 'MESH' and o.data.shape_keys == data]
        related = set(_related_meshes(rig))
        _require(all(o in related and not any(m.type == 'ARMATURE' and m.object and m.object != rig
                                             for m in o.modifiers) for o in owners), '手工形态键与其它角色共享，先隔离其数据')
        _static(data, {item['key'].path_from_id('value')})


def _apply_inputs(rig, target):
    for name, row in target['bones'].items():
        pb = rig.pose.bones[name]
        for field in _FIELDS:
            if list(getattr(pb, field)) != row[field]:
                setattr(pb, field, row[field])
        if pb.rotation_mode != row['rotation_mode']:
            pb.rotation_mode = row['rotation_mode']
    for row in target['properties']:
        pb = rig.pose.bones[row['bone']]
        if pb[row['key']] != row['value']:
            pb[row['key']] = row['value']
    items = _identity_items_by_ref(rig)
    for index, row in enumerate(target['manual_keys']):
        ref = row.get('ref', index)
        item = items.get(ref)
        _require(item is not None, '手工Key身份索引已改变，请重新记录中立')
        key = item['key']
        if key.value != row['value']:
            key.value = row['value']


def _apply_visibility(rig, values):
    for name, value in values.items():
        rig.data.collections_all[name].is_visible = bool(value)


def _visibility_preflight(rig, values):
    _static(rig.data, {rig.data.collections_all[name].path_from_id('is_visible') for name in values})


def _mode_visibility(rig, target):
    values = {'表情面板': target == 'KEY', '面部': target == 'BONE',
              '面部-主控': target == 'BONE', '面部-次要': False}
    for name, value in list(values.items()):
        if value:
            parent = rig.data.collections_all[name].parent
            while parent:
                values[parent.name] = True
                parent = parent.parent
    return values


def _set_mode(rig, mode):
    rig.pose.bones[MODE_BONE][MODE_PROPERTY] = 0 if mode == 'KEY' else 1


def _update(rig):
    rig.update_tag(refresh={'OBJECT', 'DATA', 'TIME'})
    bpy.context.view_layer.update()


def capture_neutral(rig, *, manual_keys=None):
    """The operator declares these current inputs neutral; no pose inference."""
    _rig(rig)
    old_state = rig.get(STATE_KEY)
    identity_snapshot = _identity_snapshot(rig)
    try:
        if manual_keys is None:
            if old_state is None:
                items = []
            else:
                old = _record(rig)
                if old['schema'] == SCHEMA_V2:
                    items = _resolve_identity_items(rig, old['identity'])
                else:
                    items = _resolve_legacy_items(rig, old['neutral']['manual_keys'])
        else:
            items = _resolve_name_items(rig, manual_keys)
        neutral = _build_neutral(rig, items)
        identity = _write_identity(rig, items)
        record = {'schema': SCHEMA_V2, 'neutral': neutral, 'identity': identity,
                  'captured_mode': _mode(rig),
                  'baseline_semantics': 'declared_current_inputs_shared_across_native_modes_not_output_geometry',
                  'note': _NOTE}
        rig[STATE_KEY] = json.dumps(record, ensure_ascii=False, allow_nan=False)
        _read(rig)
        return {'configured': True, 'schema': SCHEMA_V2,
                'control_count': len(record['neutral']['bones']),
                'manual_key_count': len(record['neutral']['manual_keys']),
                'mode': _mode(rig), 'note': _NOTE}
    except Exception:
        if old_state is None:
            if STATE_KEY in rig:
                del rig[STATE_KEY]
        else:
            rig[STATE_KEY] = old_state
        _restore_identity(rig, identity_snapshot)
        raise


def inspect(rig):
    _rig(rig)
    names = _bone_names(rig)
    raw = rig.get(STATE_KEY)
    if not raw:
        return {'configured': False, 'needs_migration': False, 'mode': _mode(rig),
                'conflicts': [], 'control_count': len(names), 'manual_key_count': 0,
                'visibility': {name: bool(rig.data.collections_all[name].is_visible) for name in SPACES.values()},
                'has_previous': False, 'note': _NOTE}
    if needs_migration(rig):
        old = _record(rig)
        return {'configured': True, 'needs_migration': True, 'mode': _mode(rig),
                'conflicts': [], 'control_count': len(names),
                'manual_key_count': len(old['neutral'].get('manual_keys', [])),
                'visibility': {name: bool(rig.data.collections_all[name].is_visible) for name in SPACES.values()},
                'has_previous': bool(old.get('previous')),
                'note': '面部中立记录为旧名称schema；请显式迁移Object/Key身份后再操作。' + _NOTE}
    record = _read(rig)
    return {'configured': True, 'needs_migration': False, 'mode': _mode(rig),
            'conflicts': _conflicts(_inputs(rig), record['neutral']),
            'control_count': len(names), 'manual_key_count': len(record['identity']['manual_keys']),
            'visibility': {name: bool(rig.data.collections_all[name].is_visible) for name in SPACES.values()},
            'has_previous': bool(record.get('previous')), 'note': _NOTE}


def _transaction(rig, record, operation):
    before, old_record = _snapshot(rig), rig.get(STATE_KEY)
    try:
        operation()
        _update(rig)
        after = _snapshot(rig)
        record['previous'] = {'before': before, 'after': after}
        rig[STATE_KEY] = json.dumps(record, ensure_ascii=False, allow_nan=False)
        return inspect(rig)
    except Exception:
        _apply_inputs(rig, before['inputs'])
        _set_mode(rig, before['mode'])
        _apply_visibility(rig, before['visibility'])
        if old_record is None:
            if STATE_KEY in rig:
                del rig[STATE_KEY]
        else:
            rig[STATE_KEY] = old_record
        _update(rig)
        raise


def switch(rig, *, target='KEY', reset=False, follow_visibility=True):
    _require(target in {'KEY', 'BONE'}, '请选择形态键或骨控模式')
    _require(type(reset) is bool and type(follow_visibility) is bool, '明确复位与显隐选项')
    _rig(rig, data=follow_visibility)
    record = _read(rig)
    conflicts = _conflicts(_inputs(rig), record['neutral'])
    _require(reset or not conflicts, '面部输入未回到所记中立；请复位后切换：' + '、'.join(conflicts[:4]))
    desired = record['neutral'] if reset else _inputs(rig)
    _preflight(rig, desired, mode=target)
    from . import face_expression_core
    face_expression_core._preflight_mode_change(rig, target=target)
    visibility = _mode_visibility(rig, target) if follow_visibility else {}
    _visibility_preflight(rig, visibility)
    def apply():
        if reset:
            _apply_inputs(rig, desired)
        _set_mode(rig, target)
        if follow_visibility:
            _apply_visibility(rig, visibility)
    return _transaction(rig, record, apply)


def set_visibility(rig, *, panel=None, basic=None, main=None, secondary=None):
    """Display only: no mode property, pose inputs or keys are written."""
    _rig(rig, data=True)
    _bone_names(rig)
    requested = dict(panel=panel, basic=basic, main=main, secondary=secondary)
    _require(all(v is None or type(v) is bool for v in requested.values()), '显隐选项须为布尔值')
    old, values = _visibility(rig), {}
    for key, value in requested.items():
        if value is None:
            continue
        name = SPACES[key]
        values[name] = value
        if value:
            parent = rig.data.collections_all[name].parent
            while parent:
                values[parent.name] = True
                parent = parent.parent
    _visibility_preflight(rig, values)
    try:
        _apply_visibility(rig, values)
    except Exception:
        _apply_visibility(rig, old)
        raise
    return {'visibility': {name: bool(rig.data.collections_all[name].is_visible) for name in SPACES.values()},
            'mode': _mode(rig), 'inputs_unchanged': True}


def restore_previous(rig):
    _rig(rig, data=True)
    record = _read(rig)
    previous = record.get('previous')
    _require(previous is not None, '没有上次切换/复位可恢复')
    _require(_snapshot(rig) == previous['after'], '上次切换后面部输入或显隐已手改，请先保留手改，不覆盖恢复')
    target = previous['before']
    _require(set(target) == {'inputs', 'mode', 'visibility'} and target['mode'] in {'KEY', 'BONE'} and
             set(target['visibility']) == set(_visibility(rig)) and
             all(type(value) is bool for value in target['visibility'].values()),
             '上次切换恢复范围无效，原数据保持')
    _validate_inputs(rig, target['inputs'], reference=record['neutral'], identity=record['identity'])
    _preflight(rig, target['inputs'], mode=target['mode'])
    from . import face_expression_core
    face_expression_core._preflight_mode_change(rig, target=target['mode'])
    _visibility_preflight(rig, target['visibility'])
    def apply():
        _apply_inputs(rig, target['inputs'])
        _set_mode(rig, target['mode'])
        _apply_visibility(rig, target['visibility'])
    return _transaction(rig, record, apply)
