"""Explicit C94 module rebuild on independent bound Mesh copies.

This first slice accepts static, keyless, ordinary group-weight bindings and an
explicit prepared module configuration. It never edits the old rig or saves a
file, migrates animation, normalizes a field, or guesses a replacement bone.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import uuid

import bpy
import numpy as np
from mathutils import Matrix

from . import control_panel, corrective_authoring as signatures
from . import generation, module_configuration, positioning, skinning_job

_BINDING = 'c94_skinning_v1'
_POLICY = 'c94_skinning_policy_v1'
_PRODUCTION = 'c94_production_input_v1'
_KNOWN_RECORDS = {_BINDING, _POLICY, _PRODUCTION}


class RebuildError(ValueError):
    """A bounded rejection with details suitable for the GUI/CLI caller."""
    def __init__(self, code, message, report=None):
        super().__init__(message)
        self.code, self.report = code, report or {}


def _need(condition, code, message, report=None):
    if not condition:
        raise RebuildError(code, message, report)


def _json(raw, schema, label):
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except (ValueError, TypeError) as error:
        raise RebuildError('INVALID_RECORD', label + '不是有效JSON') from error
    _need(isinstance(value, dict) and value.get('schema') == schema,
          'INVALID_RECORD', label + '身份版本无效')
    return value


def _hash_text(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def _local(scene, obj, kind):
    _need(isinstance(obj, bpy.types.Object) and obj.type == kind
          and scene.objects.get(obj.name) == obj
          and bpy.context.view_layer.objects.get(obj.name) == obj
          and not obj.library and not obj.override_library and obj.is_editable
          and obj.data is not None and not obj.data.library and not obj.data.override_library
          and obj.data.is_editable, 'LOCAL_INPUT_REQUIRED', '需要当前场景和视图层的本地' + kind)
    matrix = np.asarray(obj.matrix_world, dtype=float)
    _need(np.isfinite(matrix).all() and abs(np.linalg.det(matrix[:3, :3])) > 1e-12,
          'INVALID_WORLD_MATRIX', '对象世界变换必须有限且可逆：' + obj.name)


def _action_state(owner, *, rig=False):
    ad = owner.animation_data
    if ad is None:
        return {'action': None, 'slots': [], 'curve_count': 0}
    _need(not ad.use_tweak_mode and not ad.nla_tracks, 'ANIMATION_UNSUPPORTED',
          '本次重建不迁移NLA或tweak状态：' + owner.name)
    _need(rig or not ad.drivers, 'ANIMATION_UNSUPPORTED', '网格对象及Mesh不能带driver：' + owner.name)
    action = ad.action
    _need(rig or action is None, 'ANIMATION_UNSUPPORTED', '网格对象及Mesh不能带Action：' + owner.name)
    curves, slots = 0, []
    if action:
        _need(not action.library and not action.override_library, 'ANIMATION_UNSUPPORTED', '旧Action必须为本地空Action')
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    curves += len(bag.fcurves)
                    slots.append({'slot_handle': bag.slot_handle, 'curves': len(bag.fcurves)})
        _need(curves == 0, 'ANIMATION_UNSUPPORTED', '旧Action存在真实槽曲线；本次不伪装迁移动作',
              {'action': action.name, 'curves': curves, 'slots': slots})
    return {'action': action.name if action else None, 'action_id': action.as_pointer() if action else None,
            'active_slot_handle': ad.action_slot_handle if action else None,
            'slots': slots, 'curve_count': curves}


def _metadata(rig):
    data = _json(rig.get(generation.RIG_KEY), 'c94.generated.v1', '生成骨架')
    mapping, deforms = data.get('reference_mapping'), data.get('deforms')
    _need(isinstance(mapping, dict) and mapping and isinstance(deforms, list)
          and set(deforms) == set(mapping) and len(deforms) == len(set(deforms))
          and all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())
          and len(set(mapping.values())) == len(mapping),
          'INVALID_SEMANTIC_MAPPING', '生成记录缺少唯一、完整的输出→源语义映射')
    _need(all((bone := rig.data.bones.get(name)) is not None and bone.use_deform for name in mapping),
          'INVALID_SEMANTIC_MAPPING', '生成记录中的实际形变输出缺失或已改')
    return data


def _records(obj, rig):
    """Known historical records are retired; unknown managed authority is rejected."""
    found = {}
    for owner in (obj, obj.data):
        for key in owner.keys():
            if str(key).startswith('c94_'):
                _need(owner == obj and key in _KNOWN_RECORDS, 'UNKNOWN_MANAGED_IDENTITY',
                      '副本带未知或活动受管身份，不能伪继承：' + obj.name + '/' + str(key))
    if _BINDING in obj:
        record = _json(obj[_BINDING], 'c94.skinning.v1', '蒙皮绑定')
        _need(record.get('rig') == rig.name and record.get('status') != 'unsubmitted'
              and record.get('method') != 'native_weight_paint_proposal',
              'INVALID_BINDING_IDENTITY', '旧绑定记录不属于当前骨架或仍是未提交提案：' + obj.name)
        _need(not record.get('display_only'), 'INVALID_BINDING_IDENTITY', '不能重建仅显示预览')
        found[_BINDING] = record
    if _POLICY in obj:
        record = _json(obj[_POLICY], 'c94.weight-policy.v1', '已应用权重策略')
        _need(record.get('rig') == rig.name, 'INVALID_BINDING_IDENTITY', '旧权重策略不属于当前骨架')
        found[_POLICY] = record
    if _PRODUCTION in obj:
        # Audited producer: production-input/prepare_production_worker.py. This
        # is historical raw-geometry provenance, not a current binding claim.
        record = _json(obj[_PRODUCTION], 'c94.production-input.v1', '生产几何来源')
        _need(all(key in record for key in ('source_file', 'source_sha256', 'source_object', 'raw_content_sha256')),
              'INVALID_BINDING_IDENTITY', '生产几何来源记录不完整')
        found[_PRODUCTION] = record
    return found


def _weights(obj):
    names = [g.name for g in obj.vertex_groups]
    rows = []
    for vertex in obj.data.vertices:
        row = {}
        for item in vertex.groups:
            _need(0 <= item.group < len(names) and math.isfinite(item.weight) and 0 <= item.weight <= 1,
                  'INVALID_WEIGHT', '原生权重索引或数值无效：' + obj.name)
            row[names[item.group]] = float(item.weight)
        rows.append(row)
    return names, rows


def _object_state(obj):
    names, rows = _weights(obj)
    return {'data': obj.data.as_pointer(), 'geometry': signatures._mesh_signature(obj.data),
            'weights': signatures._hash([names, rows, [g.lock_weight for g in obj.vertex_groups]]),
            'proxy': skinning_job._proxy_state(obj),
            'props': signatures._plain(dict(obj.items())), 'mesh_props': signatures._plain(dict(obj.data.items())),
            'materials': [(s.link, s.material.as_pointer() if s.material else None) for s in obj.material_slots]}


def _mesh_plan(obj, source_rig, metadata, definition, name):
    _local(bpy.context.scene, obj, 'MESH')
    _need(obj.data.shape_keys is None and not obj.constraints, 'MESH_STATE_UNSUPPORTED', '首切片只接无Key、无对象约束的Mesh')
    _action_state(obj); _action_state(obj.data)
    _need(obj.parent is None or (obj.parent == source_rig and obj.parent_type == 'OBJECT' and not obj.parent_bone),
          'PARENT_UNSUPPORTED', '网格父级只支持None或当前source rig的OBJECT父级')
    mods = list(obj.modifiers)
    _need(mods and mods[0].type == 'ARMATURE' and mods[0].object == source_rig
          and sum(m.type == 'ARMATURE' for m in mods) == 1
          and all(m.type == 'SUBSURF' for m in mods[1:]),
          'MODIFIER_STACK_UNSUPPORTED', '需要首位唯一source rig Armature，可选原Subsurf')
    arm = mods[0]
    _need(arm.show_viewport and arm.show_render and arm.use_vertex_groups and not arm.use_bone_envelopes
          and not arm.vertex_group and not arm.use_multi_modifier and not arm.use_deform_preserve_volume,
          'PLAIN_LBS_REQUIRED', '首切片仅支持启用的普通组LBS，无mask/envelope/DQ/multi modifier')
    records = _records(obj, source_rig)
    names, rows = _weights(obj)
    semantic = metadata['reference_mapping']
    reverse = {value: key for key, value in semantic.items()}
    merge = definition['merge_mapping']
    closed = set(names) & {reverse[s] for s in merge}
    targets = {reverse[merge[semantic[n]]] for n in closed}
    affected = closed | targets
    _need(not any(g.lock_weight for g in obj.vertex_groups if g.name in affected),
          'LOCKED_GROUP', '关闭组或归并目标组已锁定：' + obj.name)
    for mod in mods:
        for prop in mod.bl_rna.properties:
            if prop.type == 'STRING' and 'vertex_group' in prop.identifier:
                value = getattr(mod, prop.identifier)
                _need(not value or value not in affected, 'GROUP_MASK_CONSUMER',
                      '修改器组mask引用受影响输出，不能只改权重身份：' + obj.name + '/' + mod.name)
    for group in names:
        bone = source_rig.data.bones.get(group)
        _need(not (bone and bone.use_deform) or group in semantic, 'UNMAPPED_DEFORM_GROUP',
              '存在未纳入生成语义的实际形变组：' + obj.name + '/' + group)
    destination_values = {target: np.asarray([row.get(target, 0.) for row in rows], dtype=np.float64) for target in sorted(targets)}
    moved = {target: 0. for target in targets}; positive = set(); assignments = 0
    for source in sorted(closed):
        destination = reverse[merge[semantic[source]]]
        for vertex, row in enumerate(rows):
            value = row.get(source, 0.)
            destination_values[destination][vertex] += value
            if value > 0:
                moved[destination] += value;positive.add(vertex);assignments += 1
    quantization = []
    for destination, values in destination_values.items():
        rounded = values.astype(np.float32)
        bad = np.flatnonzero(rounded > 1)
        _need(not len(bad), 'MERGE_WEIGHT_OVERFLOW', '归并主组质量超出可表示的0–1范围；拒绝截断或归一化',
              {'object': obj.name, 'group': destination, 'max_theoretical': float(values.max(initial=0)),
               'vertices': bad[:32].tolist(), 'float32_max': float(rounded.max(initial=0))})
        overs = np.flatnonzero(values > 1)
        quantization.append({'group': destination, 'theoretical_max': float(values.max(initial=0)),
                             'double_above_one_rounds_to_one': len(overs),
                             'above_one_vertices': overs.tolist(),
                             'float32_rounding_max': float(np.max(abs(values-rounded.astype(np.float64)), initial=0))})
    return {'source': obj, 'name': name, 'names': names, 'rows': rows, 'closed': closed,
            'target_values': destination_values, 'records': records, 'state': _object_state(obj),
            'report': {'source': obj.name, 'vertices': len(rows), 'closed_groups': sorted(closed),
                       'positive_source_vertices': len(positive), 'positive_source_assignments': assignments,
                       'moved_mass_by_old_destination': moved, 'positive_source_mass': sum(moved.values()),
                       'quantization': quantization}}


def _new_mapping(plans, old_meta, new_meta):
    inverse = {semantic: name for name, semantic in new_meta['reference_mapping'].items()}
    old_mapping = old_meta['reference_mapping']
    for plan in plans:
        obj = plan['source']; rename = {old: inverse[semantic] for old, semantic in old_mapping.items() if semantic in inverse}
        destinations = {old: rename[old] for old in plan['target_values']}
        active_names = set(plan['names']) - plan['closed']
        final_names = [rename.get(name, name) for name in plan['names'] if name not in plan['closed']]
        _need(len(set(final_names)) == len(final_names), 'GROUP_NAME_COLLISION', '新输出与非目标组名称冲突：' + obj.name)
        for target in destinations.values():
            owners = [name for name in active_names if rename.get(name, name) == target]
            _need(not owners or owners == [next(old for old, new in destinations.items() if new == target)],
                  'GROUP_NAME_COLLISION', '归并目标名称已被非目标组占用：' + obj.name + '/' + target)
        changed = {name for name in active_names if rename.get(name, name) != name}
        _need(not any(obj.vertex_groups[n].lock_weight for n in changed), 'LOCKED_GROUP', '需要改名的输出组已锁定')
        for mod in obj.modifiers:
            for prop in mod.bl_rna.properties:
                if prop.type == 'STRING' and 'vertex_group' in prop.identifier:
                    value = getattr(mod, prop.identifier)
                    _need(not value or value not in changed, 'GROUP_MASK_CONSUMER', '修改器组mask引用需要改名的输出')
        plan['rename'], plan['destinations'] = rename, destinations


def _layout_inputs(source_rig):
    layout = copy.deepcopy(control_panel.read_layout(source_rig))
    _need(set(layout) <= {'schema', 'label', 'editing', 'rows', 'defaults', 'control_policy'}
          and isinstance(layout.get('defaults'), dict), 'LAYOUT_UNSUPPORTED', '基础面板有未知结构，不能冒称迁移')
    base = {'id', 'kind', 'group', 'label', 'visible'}
    fields = {'collection': {'collection'}, 'property': {'bone', 'property'},
              'action': {'action', 'limb', 'side', 'target'}}
    actions = {'RESET', 'KEY', 'DELETE_KEY', 'SWITCH_ARM', 'SWITCH_LEG',
               'RECORD_MODES', 'RECORD_LIMB', 'SWITCH_AND_KEY'}
    for row in layout['rows']:
        kind = row.get('kind')
        _need(kind in fields and set(row) <= base | fields[kind]
              and isinstance(row.get('label'), str) and isinstance(row.get('group'), str)
              and type(row.get('visible')) is bool, 'LAYOUT_UNSUPPORTED', '基础面板有未知行类型/字段')
        if kind == 'collection':
            _need(source_rig.data.collections_all.get(row.get('collection', '')) is not None,
                  'LAYOUT_REFERENCE_INVALID', '旧面板集合引用已失效')
        elif kind == 'property':
            pb = source_rig.pose.bones.get(row.get('bone', ''))
            _need(pb is not None and row.get('property') in pb, 'LAYOUT_REFERENCE_INVALID', '旧面板属性引用已失效')
        else:
            action = row.get('action')
            _need(action in actions, 'LAYOUT_UNSUPPORTED', '基础面板有未知动作')
            if action in {'SWITCH_ARM', 'SWITCH_LEG', 'RECORD_LIMB', 'SWITCH_AND_KEY'}:
                _need(row.get('side', 'L') in {'L', 'R'}, 'LAYOUT_REFERENCE_INVALID', '面板动作侧别无效')
            if action in {'RECORD_LIMB', 'SWITCH_AND_KEY'}:
                _need(row.get('limb', 'ARM') in {'ARM', 'LEG'}, 'LAYOUT_REFERENCE_INVALID', '面板动作肢体无效')
            if action in {'SWITCH_ARM', 'SWITCH_LEG', 'SWITCH_AND_KEY'}:
                _need(row.get('target', 'IK') in {'FK', 'IK'}, 'LAYOUT_REFERENCE_INVALID', '面板动作目标无效')
    expected = {'location': 3, 'scale': 3, 'rotation_quaternion': 4,
                'rotation_euler': 3, 'rotation_axis_angle': 4, 'basis_quaternion': 4}
    for name, values in layout['defaults'].items():
        _need(name in source_rig.pose.bones and isinstance(values, dict)
              and set(values) == set(expected) | {'rotation_mode'},
              'LAYOUT_REFERENCE_INVALID', '旧面板default结构或控件引用无效：' + name)
        for key, width in expected.items():
            _need(isinstance(values[key], list) and len(values[key]) == width
                  and all(type(v) in (int, float) and math.isfinite(v) for v in values[key]),
                  'LAYOUT_REFERENCE_INVALID', '旧面板default通道无效：' + name)
    return layout


def _collection_controls(rig, collection, eligible):
    names = {b.name for b in collection.bones}
    for child in collection.children:
        names |= _collection_controls(rig, child, eligible)
    return names & eligible


def _layout_migrate(source_rig, rig, layout, definition, label, *, removed_settings=()):
    from . import mode_animation, mode_semantics, pose_switching, pose_switching_leg
    old_eligible = set(control_panel.eligible_controls(source_rig))
    eligible = set(control_panel.eligible_controls(rig))
    removed = set(definition['removed_bones'])
    _need(not (old_eligible - eligible - removed), 'LAYOUT_REFERENCE_INVALID', '保留控件在新骨架中失去资格，不能默默丢default')
    _need(eligible <= set(layout['defaults']), 'LAYOUT_REFERENCE_INVALID', '新控件缺少已知旧default；不伪造迁移')
    generated = control_panel.read_layout(rig)
    _need(layout.get('control_policy') == generated.get('control_policy'), 'LAYOUT_UNSUPPORTED', '旧面板控制策略与新原生策略不一致')
    kept, removed_rows = [], []
    for row in layout['rows']:
        kind = row['kind']; discard = False; reason = 'explicit_closed_module_reference'
        if kind == 'collection':
            old = source_rig.data.collections_all[row['collection']]
            new = rig.data.collections_all.get(row['collection'])
            controls = _collection_controls(source_rig, old, old_eligible)
            discard = bool(controls) and controls <= removed
            _need(discard or (new is not None and _collection_controls(rig, new, eligible)),
                  'LAYOUT_REFERENCE_INVALID', '面板集合不再有已知原生控件：' + row['collection'])
        elif kind == 'property':
            discard = row['bone'] in removed
            pb = rig.pose.bones.get(row['bone'])
            value = pb.get(row['property']) if pb else None
            if (not definition['enabled_modules']['face'] and row['bone'] == '骨架设置'
                    and row['property'] == '脸部控制形态键-骨骼' and row['property'] in removed_settings):
                paths = {pb.path_from_id() + '[' + json.dumps(row['property'], ensure_ascii=ascii_only) + ']'
                         for ascii_only in (True, False)} if pb else set()
                consumers = [curve.data_path for owner in (rig, rig.data) if owner.animation_data
                             for curve in owner.animation_data.drivers
                             if any(target.id == rig and target.data_path in paths
                                    for variable in curve.driver.variables for target in variable.targets)]
                _need(pb is not None and row['property'] not in pb and not consumers,
                      'LAYOUT_REFERENCE_INVALID', '关闭face属性仍有原生引用，不能迁移时默默删除')
                discard = True; reason = 'disabled_face_property_no_consumers'
            _need(discard or (pb is not None and type(value) in (int, float, bool) and math.isfinite(float(value))),
                  'LAYOUT_REFERENCE_INVALID', '新骨架面板属性不存在或类型改变')
        else:
            action = row['action']; side = row.get('side', 'L')
            if action == 'SWITCH_ARM':
                pose_switching._arm(rig, side)
            elif action == 'SWITCH_LEG':
                pose_switching_leg._validate(rig, side)
            elif action in {'RECORD_LIMB', 'SWITCH_AND_KEY'}:
                mode_animation._limb_scope(rig, row.get('limb', 'ARM'), side)
            elif action == 'RECORD_MODES':
                _need(bool(mode_semantics.available(rig)), 'LAYOUT_REFERENCE_INVALID', '新骨架缺少实际模式属性')
            else:
                _need(bool(eligible), 'LAYOUT_REFERENCE_INVALID', '新骨架没有实际可用控件')
        if discard:
            removed_rows.append({'row': copy.deepcopy(row), 'reason': reason})
        else:
            kept.append(copy.deepcopy(row))
    defaults, removed_defaults = {}, []
    for name, values in layout['defaults'].items():
        if name in eligible:
            defaults[name] = copy.deepcopy(values)
        else:
            _need(name in removed, 'LAYOUT_REFERENCE_INVALID', 'default不是已知关闭控件：' + name)
            removed_defaults.append({'bone': name, 'reason': 'explicit_closed_module_control'})
    migrated = copy.deepcopy(layout)
    migrated.update(label=label, editing=False, rows=kept, defaults=defaults)
    control_panel._save(rig, migrated)
    _need(control_panel.read_layout(rig) == migrated, 'LAYOUT_READBACK_FAILED', '基础面板迁移回读不一致')
    return {'kept_row_ids': [r['id'] for r in kept], 'removed_rows': removed_rows,
            'kept_defaults': list(defaults), 'removed_defaults': removed_defaults,
            'policy': 'original IDs/order/labels/visibility and retained defaults; label updated; editing false',
            'not_migrated': ['P1 facial hubs', 'custom-shape author edits', 'Shape Key authoring', 'old animation']}


def _copy_mesh(plan, rig, collection, configuration, *, owned_ids=None):
    source = plan['source']; obj = module_configuration._own(owned_ids, source.copy())
    mesh = module_configuration._own(owned_ids, source.data.copy());obj.data = mesh
    obj.name = plan['name'];mesh.name = plan['name'] + '__网格'
    _need(obj.name == plan['name'] and mesh.name == plan['name'] + '__网格',
          'NAME_COLLISION', '新副本名称被原生自动改写')
    collection.objects.link(obj)
    world = source.matrix_world.copy()
    if source.parent:
        obj.parent = rig;obj.parent_type = 'OBJECT';obj.parent_bone = ''
    obj.matrix_world = world
    obj.modifiers[0].object = rig
    obj.hide_set(source.hide_get())
    for name in sorted(plan['closed']):
        obj.vertex_groups.remove(obj.vertex_groups[name])
    changing = [(obj.vertex_groups[old], new) for old, new in plan['rename'].items()
                if old in obj.vertex_groups and old != new]
    for group, new in changing:
        group.name = '__C94_REBUILD_' + uuid.uuid4().hex
    for group, new in changing:
        group.name = new
        _need(group.name == new, 'GROUP_NAME_COLLISION', '新输出组被原生自动改名')
    ids = list(range(len(mesh.vertices)));float_error = 0.
    for old, values in plan['target_values'].items():
        name = plan['destinations'][old]
        group = obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)
        _need(group.name == name and not group.lock_weight, 'GROUP_NAME_COLLISION', '归并目标组身份/锁改变')
        group.remove(ids)
        rounded = values.astype(np.float32)
        for index in np.flatnonzero(rounded > 0):
            group.add([int(index)], float(rounded[index]), 'REPLACE')
        float_error = max(float_error, float(np.max(abs(values-rounded.astype(np.float64)), initial=0)))
    expected = []
    for vertex, row in enumerate(plan['rows']):
        new = {plan['rename'].get(n, n): w for n, w in row.items() if n not in plan['closed']}
        for old, values in plan['target_values'].items():
            name = plan['destinations'][old];value = float(np.float32(values[vertex]))
            if value > 0:new[name] = value
            else:new.pop(name, None)
        expected.append(new)
    actual_names, actual = _weights(obj)
    _need(actual == expected, 'WEIGHT_WRITE_FAILED', '归并/改名未精确保留非目标组或float32目标值')
    expected_locks = {plan['rename'].get(g.name, g.name): g.lock_weight for g in source.vertex_groups if g.name not in plan['closed']}
    expected_locks.update({name: False for name in plan['destinations'].values() if name not in expected_locks})
    _need({g.name: g.lock_weight for g in obj.vertex_groups} == expected_locks,
          'LOCK_METADATA_CHANGED', '非目标锁组或目标组锁状态改变')
    ordinary = lambda o: signatures._plain({k: v for k, v in o.items() if k not in _KNOWN_RECORDS})
    _need(ordinary(obj) == ordinary(source), 'OBJECT_PROPERTIES_CHANGED', '普通非目标属性改变')
    prior = {key: {'schema': value['schema'], 'sha256': _hash_text(source[key]), 'source_object': source.name}
             for key, value in plan['records'].items()}
    for key in _KNOWN_RECORDS:
        if key in obj:del obj[key]
    report = dict(plan['report'], object=obj.name, rig=rig.name,
                  output_group_mapping={n: plan['rename'][n] for n in plan['names'] if n in plan['rename']},
                  destination_groups=plan['destinations'], float32_rounding_max=float_error,
                  unaffected_weights_exact=True, locks_preserved=True, retired_records=prior)
    provenance = {'schema': 'c94.skinning.v1', 'source': source.name, 'source_data': source.data.name,
                  'rig': rig.name, 'method': 'module_rebuild_explicit_weight_merge',
                  'configuration_id': configuration['id'], 'parent_records': prior,
                  'historical_production_input': plan['records'].get(_PRODUCTION), 'report': report}
    obj[_BINDING] = json.dumps(provenance, ensure_ascii=False, allow_nan=False)
    _need(json.loads(obj[_BINDING])['rig'] == rig.name and obj.modifiers[0].object == rig
          and (obj.parent is None if source.parent is None else obj.parent == rig),
          'BINDING_REFERENCE_FAILED', '新副本属性、Armature或父级未指向新骨架')
    for mod in obj.modifiers:
        for prop in mod.bl_rna.properties:
            if prop.type == 'POINTER':
                _need(getattr(mod, prop.identifier) != plan.get('source_rig'),
                      'BINDING_REFERENCE_FAILED', '副本修改器仍引用旧骨架')
    _need(signatures._mesh_signature(obj.data) == plan['state']['geometry']
          and signatures._plain(dict(obj.data.items())) == plan['state']['mesh_props']
          and [(s.link, s.material.as_pointer() if s.material else None) for s in obj.material_slots] == plan['state']['materials'],
          'GEOMETRY_CHANGED', '副本几何/拓扑/UV/材质或Mesh属性改变')
    expected_mods = [signatures._rna(m) for m in source.modifiers]
    actual_mods = [signatures._rna(m) for m in obj.modifiers]
    expected_mods[0]['object'] = signatures._plain(rig)
    _need(actual_mods == expected_mods, 'MODIFIER_CHANGED', 'Armature目标以外的修改器设置改变')
    _need(np.max(abs(np.asarray(obj.matrix_world)-np.asarray(world))) <= 1e-7,
          'WORLD_LAYOUT_CHANGED', '新副本世界布局改变')
    row_error = max((abs(sum(old.values())-sum(new.values())) for old, new in zip(plan['rows'], actual)), default=0.)
    report['row_mass_max_float32_difference'] = row_error
    return obj, report


def _restore_selection(active, selected):
    for obj in list(bpy.context.selected_objects):obj.select_set(False)
    for obj in selected:
        if bpy.context.view_layer.objects.get(obj.name) == obj:obj.select_set(True)
    bpy.context.view_layer.objects.active = active


def _inputs(scene, reference, template_file, configuration, source_rig, objects, *, label, rig=None):
    _need(scene == bpy.context.scene and bpy.context.mode == 'OBJECT', 'OBJECT_MODE_REQUIRED', '重建需要当前场景的对象模式')
    _local(scene, reference, 'ARMATURE');_local(scene, source_rig, 'ARMATURE')
    _need(reference != source_rig and source_rig.parent is None and not source_rig.constraints
          and np.max(abs(np.asarray(source_rig.matrix_world)-np.asarray(reference.matrix_world))) <= 1e-7,
          'SOURCE_RIG_FRAME_UNSUPPORTED', '旧骨架必须在已批准参考的原中立对象框架，无父级或对象约束')
    _need(isinstance(objects, (list, tuple)) and objects and len(set(objects)) == len(objects),
          'EXPLICIT_OBJECTS_REQUIRED', '逐项给出明确且无重复的Mesh集合，不扫描其它历史候选')
    _need(isinstance(label, str) and label.strip(), 'NAME_COLLISION', '请给出明确新角色label')
    label = label.strip()
    names = [f'{label}__重建_{i:02d}' for i in range(1, len(objects)+1)]
    _need(all(len(n.encode('utf8')) <= 63 for n in [label+'__控制骨架', label+'__绑定生成', *[n+'__网格' for n in names]])
          and (rig is not None or (label+'__控制骨架' not in bpy.data.objects
               and label+'__控制骨架' not in bpy.data.armatures and label+'__绑定生成' not in bpy.data.collections))
          and all(n not in bpy.data.objects and n+'__网格' not in bpy.data.meshes for n in names),
          'NAME_COLLISION', '新rig/集合/Object/Mesh名称冲突或超过原生字节上限')
    configured = module_configuration.validate(scene, reference, template_file, configuration)
    definition = configured['definition'];old_meta = _metadata(source_rig)
    base_sha = definition['base_template']['expected_sha256']
    source_configuration = old_meta.get('module_configuration')
    attestations = [old_meta.get('base_template_sha256')]
    if isinstance(source_configuration, dict):
        descriptor = source_configuration.get('definition', {}).get('base_template', {})
        attestations.append(descriptor.get('expected_sha256'))
        module_configuration.validate(scene, reference, template_file, source_configuration)
    attestations = [value for value in attestations if value is not None]
    _need(all(value == base_sha for value in attestations), 'SOURCE_TEMPLATE_CHANGED',
          '旧rig已保存的原模板字节身份与本次base不一致；不能仅以相同路径复用语义')
    source_template_identity = ('saved base SHA matches current descriptor' if attestations else
                                'structural mapping/reference checks; no saved byte attestation')
    signature = generation.validate_input(scene, reference)
    _need(old_meta.get('reference_signature') == signature
          and old_meta.get('reference_object', old_meta.get('reference')) == reference.name
          and Path(old_meta.get('base_template_file', old_meta.get('template_file', ''))).resolve() == Path(template_file).resolve(),
          'SOURCE_REFERENCE_CHANGED', '旧rig与本次已批准完整参考/原模板身份不对应')
    keep, removed = set(definition['retained_bones']), set(definition['removed_bones'])
    semantics = set(old_meta['reference_mapping'].values());merge = definition['merge_mapping']
    _need(semantics <= keep | removed and set(merge) == semantics & removed
          and set(merge.values()) <= semantics & keep,
          'INVALID_SEMANTIC_MAPPING', '关闭形变语义缺少可靠主形变映射，不能猜骨或丢权重')
    _need(keep | removed <= set(reference.data.bones.keys()), 'INCOMPLETE_REFERENCE', '完整参考缺少源配置骨')
    action = _action_state(source_rig, rig=True);_action_state(source_rig.data, rig=True)
    layout = _layout_inputs(source_rig)
    plans = [_mesh_plan(obj, source_rig, old_meta, definition, name) for obj, name in zip(objects, names)]
    for plan in plans:plan['source_rig'] = source_rig
    source_neutral = generation.verify_neutral(source_rig, reference, old_meta['deforms'], old_meta['reference_mapping'])
    _need(source_rig.data.pose_position == 'POSE' and source_neutral['sample_max'] <= 1e-5 and source_neutral['head_tail_max'] <= 1e-5,
          'SOURCE_NOT_NEUTRAL', '旧rig不在实际B-Bone校准中立状态')
    old_contract = signatures._rig_state(source_rig)['contract']
    before_points = [skinning_job._points(obj) for obj in objects]
    return {'configuration': configured, 'definition': definition, 'old_meta': old_meta,
            'semantics': semantics & keep, 'source_action': action, 'source_neutral': source_neutral,
            'source_template_identity': source_template_identity, 'signature': signature,
            'layout': layout, 'plans': plans, 'old_contract': old_contract, 'before_points': before_points}


def scope_source(obj):
    """Saved source address and deterministic structure/weight digest."""
    state = _object_state(obj)
    state['data'] = signatures._plain(obj.data)
    state['proxy']['shape_keys_id'] = signatures._plain(obj.data.shape_keys)
    state['proxy']['parent_id'] = signatures._plain(obj.parent)
    state['materials'] = [[s.link, signatures._plain(s.material)] for s in obj.material_slots]
    return {'object': obj.name, 'mesh': obj.data.name, 'sha256': signatures._hash(state)}


def _scope(rig, reference, source_rig, inputs):
    plans = inputs['plans']
    _new_mapping(plans, inputs['old_meta'], _metadata(rig))
    source_action = {key: value for key, value in inputs['source_action'].items() if key != 'action_id'}
    meshes = []
    for plan in plans:
        affected = plan['closed'] | set(plan['target_values']) | {
            name for name in plan['names'] if name != plan['rename'].get(name, name)}
        meshes.append({'source': scope_source(plan['source']), 'output': plan['name'],
                       'vertices': len(plan['rows']),
                       'affected_point_indices': [i for i, row in enumerate(plan['rows'])
                                                  if any(row.get(name, 0.) > 0 for name in affected)],
                       'removed_groups': sorted(plan['closed']),
                       'renames': {k: v for k, v in plan['rename'].items() if k in plan['names'] and k != v},
                       'merged_columns': {plan['destinations'][name]: signatures._array_hash(
                           values.astype('<f4')) for name, values in plan['target_values'].items()},
                       'plan': copy.deepcopy(plan['report'])})
    scene = bpy.context.scene
    return {'kind': 'rebuild', 'rig': rig.name, 'reference': reference.name,
            'reference_signature': inputs['signature'], 'source_rig': source_rig.name,
            'source_rig_sha256': inputs['old_contract'], 'source_action': source_action,
            'frame': [scene.frame_current, scene.frame_subframe],
            'view_layer': bpy.context.view_layer.name, 'unit_scale': float(scene.unit_settings.scale_length),
            'configuration_id': inputs['configuration']['id'], 'meshes': meshes}


def prepare_candidate(scene, reference, template_file, configuration, source_rig, objects, *,
                      label, control_sections=('body', 'arms', 'legs'), shoulder_landmarks=None):
    """Generate the final rig and freeze its proposed migration without writing weights."""
    from . import candidate_state, control_layer

    label = label.strip()
    inputs = _inputs(scene, reference, template_file, configuration, source_rig, objects, label=label)
    before = module_configuration._ids()
    active = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    owned = []
    try:
        generated = generation.generate(scene, reference, template_file, label=label,
                                        configuration=inputs['configuration'], owned_ids=owned)
        rig, collection = generated['rig'], generated['collection']
        _need(set(_metadata(rig)['reference_mapping'].values()) == inputs['semantics'],
              'INVALID_SEMANTIC_MAPPING', '新rig实际输出语义集合不等于配置保留集')
        layout = _layout_migrate(source_rig, rig, inputs['layout'], inputs['definition'], label,
                                 removed_settings=generated['report'].get('disabled_module_settings_removed', ()))
        control_layer.build(rig, family='c94', sections=control_sections,
                            action_policy='remap', owned_ids=owned)
        if shoulder_landmarks is not None:
            generation.install_shoulder_girdle(scene, reference, generated, shoulder_landmarks, owned_ids=owned)
        bpy.context.view_layer.update()
        scope = _scope(rig, reference, source_rig, inputs)
        report = {'schema': 'c94.module-rebuild.v1', 'state': 'awaiting_review',
                  'source_rig': source_rig.name, 'rig': rig.name, 'reference': reference.name,
                  'configuration_id': inputs['configuration']['id'], 'generation': generated['report'],
                  'layout': layout, 'meshes': [mesh['plan'] for mesh in scope['meshes']],
                  'legacy_source_template_identity': inputs['source_template_identity']}
        payload = {'label': label, 'source_rig': source_rig.name, 'reference': reference.name,
                   'objects': [obj.name for obj in objects], 'collection': collection.name,
                   'template': str(Path(template_file).resolve()), 'configuration': inputs['configuration'],
                   'scope': scope, 'report': report}
        record = candidate_state.record(scene, rig, kind='rebuild', payload=payload)
        return {'rig': rig, 'collection': collection, 'objects': [], 'candidate_id': record['id'],
                'scope': scope, 'report': report}
    except Exception:
        module_configuration._cleanup(before, active, selected, owned_ids=owned)
        raise
    finally:
        _restore_selection(active, selected)


def inspect_candidate(scene, candidate_id):
    from . import candidate_state

    rig, record = candidate_state.resolve(scene, candidate_id, kind='rebuild')
    payload = record['payload']
    source_rig = scene.objects.get(payload['source_rig'])
    reference = scene.objects.get(payload['reference'])
    objects = [scene.objects.get(name) for name in payload['objects']]
    collection = bpy.data.collections.get(payload['collection'])
    _need(collection is not None and collection.objects.get(rig.name) == rig,
          'CANDIDATE_CHANGED', '已准备骨架不再属于本次候选集合')
    inputs = _inputs(scene, reference, payload['template'], payload['configuration'],
                     source_rig, objects, label=payload['label'], rig=rig)
    _need(set(_metadata(rig)['reference_mapping'].values()) == inputs['semantics'],
          'CANDIDATE_CHANGED', '候选骨架实际输出语义改变')
    scope = _scope(rig, reference, source_rig, inputs)
    _need(scope == payload['scope'], 'SCOPE_CHANGED', '原件、定位参考或迁权范围已变，须重新准备及审核')
    return rig, collection, scope, inputs, payload


def apply_candidate(scene, candidate_id):
    from . import candidate_state

    rig, collection, scope, inputs, payload = inspect_candidate(scene, candidate_id)
    candidate_state.require_operation(rig, operation='rebuild.apply', scope=scope)
    _need(not rig.get('c94_rebuild_applied_v1'), 'ALREADY_APPLIED', '已创建本次独立候选')
    before = module_configuration._ids()
    active = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    owned = []
    source_rig = scene.objects[payload['source_rig']]
    template = payload['configuration']
    label = payload['label']
    try:
        new_objects, reports = [], []
        for plan in inputs['plans']:
            obj, report = _copy_mesh(plan, rig, collection, template, owned_ids=owned)
            new_objects.append(obj)
            reports.append(report)
        bpy.context.view_layer.update()
        scale = float(scene.unit_settings.scale_length)
        _need(math.isfinite(scale) and scale > 0, 'INVALID_UNITS', '场景米制比例必须有限为正')
        for obj, expected, report in zip(new_objects, inputs['before_points'], reports, strict=True):
            actual = skinning_job._points(obj)
            _need(actual.shape == expected.shape and np.isfinite(actual).all(),
                  'EVALUATED_SURFACE_CHANGED', '新副本求值拓扑或数值改变')
            error = float(np.linalg.norm(actual-expected, axis=1).max(initial=0)) * scale
            _need(error <= 1e-5, 'EVALUATED_SURFACE_CHANGED', '新副本中立世界求值表面跳变',
                  {'object': obj.name, 'meters': error})
            report.update(evaluated_vertices=len(actual), neutral_surface_error_meters=error)
        _need(all(_object_state(plan['source']) == plan['state'] for plan in inputs['plans'])
              and signatures._rig_state(source_rig)['contract'] == inputs['old_contract']
              and _action_state(source_rig, rig=True) == inputs['source_action'],
              'SOURCE_CHANGED', '原件几何、权重、骨架或Action改变，已撤回迁权候选')
        report = dict(payload['report'], state='applied', meshes=reports, source_unchanged=True,
                      source_neutral=inputs['source_neutral'], source_action=scope['source_action'])
        rig['c94_rebuild_applied_v1'] = json.dumps(report, ensure_ascii=False, allow_nan=False)
        return {'rig': rig, 'collection': collection, 'objects': new_objects, 'report': report,
                'candidate_id': candidate_id}
    except Exception:
        module_configuration._cleanup(before, active, selected, owned_ids=owned)
        raise
    finally:
        _restore_selection(active, selected)


def rebuild(scene, reference, template_file, configuration, source_rig, objects, *, label):
    """Reject the legacy generation-and-write path before changing the scene."""
    raise RebuildError('STAGED_REVIEW_REQUIRED', '先生成并保存无权重候选骨架，完成同一范围双审后再采用迁权')
