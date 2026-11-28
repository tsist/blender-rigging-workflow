# SPDX-License-Identifier: GPL-3.0-or-later
"""局部权重修复套组人工界面。

只读调用同核 ``weight_repair`` 公共接口；不另写算法、不直接改源权重、骨架或
动画。所有提交权只存在于当前 Scene 拥有的内存 Plan 中。参数、配方、目标映射、
高级 Text 或源输入改变后，旧 Plan 立即失效并需重新 CHECK。
"""
import bmesh
import copy
import hashlib
import json
import math
import os
import re
import textwrap

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from . import weight_repair

OPTIONS = 'rigging_weight_repair_options_v1'
_REGISTERED = []
_SESSION = None
_OPTIONS_OWNED = False
_QUIET = 0

_SCHEMA = getattr(weight_repair, 'SCHEMA', 'rigging.weight-repair.recipe.v1')
_KIND_LABELS = {
    'ASSIGN': '完整/部分赋值',
    'TRANSFER_AMOUNT': '定量转移',
    'REBALANCE': '比例重平衡',
    'SMOOTH': '屏障邻接Smooth',
    'SURFACE_TRANSFER': '有界表面转移',
    'MIRROR': '对称镜像',
    'FIELD': '高级显式场',
}
_STATUS_LABELS = {
    'prepared': '已准备，尚未创建候选',
    'applied': '独立候选已创建',
    'cancelled': '计划已取消',
    'failed_no_candidates': '失败且未保留候选',
    'rollback_incomplete': '失败且回退未闭合（检查cleanup_errors）',
    'cancelled_between_roles': '批次在角色间取消',
}
_KEY_LABELS = {
    'status': '状态',
    'source_unchanged': '源未改动',
    'source': '源对象',
    'binding': '输入绑定',
    'steps': '步骤',
    'step': '步骤',
    'label': '名称',
    'operations': '操作',
    'operation_count': '操作数',
    'write_vertices': '写入点',
    'changed_vertices': '变更点',
    'changed_cells': '变更cell',
    'changed': '变更',
    'residual': '质量残差',
    'budget': '质量预算',
    'max_residual': '最大质量残差',
    'max_budget': '最大质量预算',
    'quality': '质量',
    'failures': '失败点',
    'failed_points': '失败点',
    'zero_editable_mass_vertices': '零可写质量保持点',
    'zero_mass_vertices': '零质量点',
    'locked_groups': '锁定组',
    'write_groups': '写组',
    'memberships': 'membership增删',
    'membership': 'membership增删',
    'added': '新增',
    'removed': '移除',
    'added_cells': '新增cell',
    'removed_cells': '移除cell',
    'max_delta': '最大差值',
    'delta': '差值',
    'deform_mass_max_drift': '形变质量和最大漂移',
    'numeric_field_extractions': '数值场提取次数',
    'protection_snapshots_separate': '独立保护快照',
    'candidate_clones': '候选克隆次数',
    'output_sha256': '输出摘要',
    'output': '候选对象',
    'timing': '计时',
    'timings': '分段计时',
    'seconds': '秒',
    'prepare_seconds': '准备秒数',
    'apply_seconds': '提交秒数',
    'recipe_sha256': '配方摘要',
    'targets': '目标',
    'target': '目标',
    'donor': '供体',
    'transfer_receipt': '表面转移回执',
    'pairs': '配对数',
    'unmatched': '未配',
    'ambiguous': '歧义',
    'max_distance': '最大配对距离',
}
_SKIP_REPORT_KEYS = {'field', 'values', 'weights', 'recipe', 'binding', 'pairing', 'ids', 'read_ids', 'alpha'}


def _options(context):
    scene = getattr(context, 'scene', None)
    if scene is None:
        raise ValueError('没有当前场景')
    options = getattr(scene, OPTIONS, None)
    if options is None:
        raise ValueError('权重修复选项尚未注册')
    return options


def _mesh(_self, obj):
    return getattr(obj, 'type', None) == 'MESH'


def _armature(_self, obj):
    return getattr(obj, 'type', None) == 'ARMATURE'


class _quiet:
    def __enter__(self):
        global _QUIET
        _QUIET += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        global _QUIET
        _QUIET -= 1
        return False


def _redraw():
    manager = getattr(bpy.context, 'window_manager', None)
    for window in tuple(manager.windows) if manager else ():
        try:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        except (ReferenceError, AttributeError):
            continue


def _set_summary(context, text):
    try:
        _options(context).summary = str(text)
    except (ReferenceError, AttributeError, ValueError):
        try:
            scene = getattr(context, 'scene', None) or bpy.context.scene
            getattr(scene, OPTIONS).summary = str(text)
        except (ReferenceError, AttributeError, ValueError):
            pass


def _session(context, *, create=False):
    global _SESSION
    scene = getattr(context, 'scene', None)
    if scene is None:
        raise ValueError('没有当前场景')
    if _SESSION is not None and _SESSION.get('scene') != scene:
        raise ValueError('另一个场景拥有权重修复UI会话；请切回原场景或先结束该会话')
    if _SESSION is None and create:
        _SESSION = {
            'scene': scene,
            'plan': None,
            'recipe': None,
            'targets': {},
            'rig': None,
            'signature': None,
            'report': None,
            'report_lines': [],
            'preflight': [],
            'pairings': {},
            'pairing_lines': {},
            'pairing_preview_names': [],
            'pairing_preview_objects': [],
            'preview_names': [],
            'candidate_names': [],
            'error': '',
        }
    return _SESSION


def _drop_plan(reason='', *, cancel=True, clear_view=True):
    global _SESSION
    session = _SESSION
    if session is None:
        return
    plan = session.get('plan')
    if plan is not None and cancel:
        try:
            plan.cancel()
        except Exception as exc:
            print('[权重修复UI] 取消live plan异常：' + str(exc))
    session['plan'] = None
    session['signature'] = None
    if clear_view:
        session['report'] = None
        session['report_lines'] = []
        session['preflight'] = []
        session['error'] = ''
        session['preview_names'] = []
    if reason:
        session['error'] = str(reason)


def _release_session(reason, *, force=True, cancel=True):
    global _SESSION
    session = _SESSION
    if session is None:
        return
    _clear_pairing_previews(session)
    plan = session.get('plan')
    if plan is not None and cancel:
        try:
            plan.cancel()
        except Exception as exc:
            print('[权重修复UI] 会话清理取消Plan异常：' + str(exc))
    if force:
        _SESSION = None
    else:
        session['plan'] = None
        session['signature'] = None
        session['report'] = None
        session['report_lines'] = []
        session['preflight'] = []
        session['preview_names'] = []
        session['error'] = str(reason)


def _changed(_self, context):
    if _QUIET:
        return
    _drop_plan(cancel=True, clear_view=True)
    if context is not None:
        _set_summary(context, '参数已改变：旧live plan已撤销；请重新CHECK')
    _redraw()


def _transfer_amount_changed(options, context):
    if not _QUIET:
        with _quiet():
            options.transfer_amount_json = ''
    _changed(options, context)


def _surface_distance_changed(options, context):
    if not _QUIET:
        options.surface_distance_exact = ''
    _changed(options, context)


@persistent
def _load_pre(_unused):
    _release_session('文件载入：UI会话与实际提交权结束；已创建预览/候选保留', force=True)


@persistent
def _save_pre(_unused):
    _release_session('文件保存：live plan已结束；配方与配对保留，重开需重新CHECK', force=False)
    try:
        _set_summary(bpy.context, '文件保存：live plan已结束；配方与已创建副本保留')
    except Exception:
        pass


def _reject_constant(value):
    raise ValueError('JSON含非有限数值：' + str(value))


def _json_loads_strict(text, label='JSON'):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(label + '含重复键：' + str(key))
            result[key] = value
        return result

    try:
        return json.loads(str(text), object_pairs_hook=unique, parse_constant=_reject_constant)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(label + '解析失败：' + str(exc)) from exc


def _json_safe(value, label='配方'):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(label + '包含无法保存或非有限的数据：' + str(exc)) from exc


def _hash_json(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf8')
    return hashlib.sha256(payload).hexdigest()


def _split_names(text):
    return [part.strip() for part in re.split(r'[,，;；\n]+', str(text or ''))
            if part.strip()]


def _parse_scalar(text, label):
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(label + '不是有效数值：' + str(text)) from exc
    if not math.isfinite(value):
        raise ValueError(label + '必须有限')
    return value


def _parse_name_scalars(text, label):
    result = {}
    for chunk in re.split(r'[;；\n]+', str(text or '')):
        chunk = chunk.strip()
        if not chunk:
            continue
        tokens = re.split(r'[,，]+', chunk) if '=' in chunk else [chunk]
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            if '=' not in token:
                raise ValueError(label + '每项须为 名称=数值：' + token)
            name, raw = token.split('=', 1)
            name = name.strip()
            if not name:
                raise ValueError(label + '含空名称')
            if name in result:
                raise ValueError(label + '含重复名称：' + name)
            result[name] = _parse_scalar(raw, label + '数值')
    if not result:
        raise ValueError(label + '为空')
    return result


def _parse_mapping(text, label='骨组映射'):
    result = {}
    destinations = set()
    for chunk in re.split(r'[;；\n,，]+', str(text or '')):
        chunk = chunk.strip()
        if not chunk:
            continue
        if '=' not in chunk:
            raise ValueError(label + '每行须为 源组=目标组：' + chunk)
        source, target = (part.strip() for part in chunk.split('=', 1))
        if not source or not target:
            raise ValueError(label + '含空骨组名')
        if source in result:
            raise ValueError(label + '含重复源组：' + source)
        if target in destinations:
            raise ValueError(label + '要求目标组唯一，重复：' + target)
        result[source] = target
        destinations.add(target)
    if not result:
        raise ValueError(label + '为空')
    return result


def _parse_barriers(text):
    tokens = [token.strip() for token in re.split(r'[;；\n,，]+', str(text or ''))
              if token.strip()]
    result = []
    for token in tokens:
        match = re.fullmatch(r'(\d+)\s*[-:]\s*(\d+)', token)
        if match is None:
            raise ValueError('屏障边须写为 a-b（例如 12-45）：' + token)
        pair = [int(match.group(1)), int(match.group(2))]
        if pair[0] == pair[1]:
            raise ValueError('屏障边两端不能相同：' + token)
        result.append(pair)
    return result


def _parse_int_list_json(text, label):
    if not str(text or '').strip():
        return None
    value = _json_loads_strict(text, label)
    if not isinstance(value, list) or not value:
        raise ValueError(label + '须为非空JSON整数数组')
    if any(type(item) is not int for item in value):
        raise ValueError(label + '只接受严格整数，不接受bool/float')
    if len(set(value)) != len(value):
        raise ValueError(label + '含重复索引')
    return value


def _short(value, limit=120):
    text = str(value).replace('\n', ' ')
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _count_list(value):
    return len(value) if isinstance(value, (list, tuple, dict)) else 1


def _clear_collection(collection):
    while len(collection):
        collection.remove(len(collection) - 1)


def _target_map(options, context=None):
    result, objects = {}, {}
    for entry in options.targets:
        key = entry.key.strip()
        if not key:
            raise ValueError('目标登记含空逻辑key；请修正')
        if key in result:
            raise ValueError('目标逻辑key重复：' + key)
        obj = entry.obj
        if obj is None:
            raise ValueError('目标逻辑key未指定对象：' + key)
        if obj.type != 'MESH':
            raise ValueError('目标必须是网格对象：' + key)
        if context is not None and context.scene.objects.get(obj.name) != obj:
            raise ValueError('目标不在当前场景：' + key + ' → ' + obj.name)
        if obj in objects:
            raise ValueError('同一对象不能用多个逻辑key重复绑定：' + obj.name)
        result[key], objects[obj] = obj, key
    if not result:
        raise ValueError('先显式登记至少一个写入目标逻辑key→对象映射')
    return result


def _rig_object(context, options):
    rig = options.rig
    if rig is None or rig.type != 'ARMATURE':
        raise ValueError('先指定明确骨架')
    if context.scene.objects.get(rig.name) != rig:
        raise ValueError('骨架不在当前场景：' + rig.name)
    return rig


def _normalize_region(region, label='区域'):
    if not isinstance(region, dict):
        raise ValueError(label + '必须是JSON字典')
    target = region.get('target')
    if not isinstance(target, str) or not target.strip():
        raise ValueError(label + '缺少显式target逻辑key')
    binding = region.get('binding')
    if not isinstance(binding, dict):
        raise ValueError(label + '缺少RAW输入binding')
    ids = region.get('ids')
    read_ids = region.get('read_ids')
    alpha = region.get('alpha')
    barriers = region.get('barriers')
    if not all(isinstance(value, list) for value in (ids, read_ids, alpha, barriers)):
        raise ValueError(label + '的ids/read_ids/alpha/barriers须为列表')
    if not ids:
        raise ValueError(label + '写域为空')
    if len(alpha) != len(ids):
        raise ValueError(label + '的alpha与ids长度不一致')
    if any(type(value) is not int for value in ids + read_ids):
        raise ValueError(label + '索引须为严格整数')
    if any(type(pair) is not list or len(pair) != 2 for pair in barriers):
        raise ValueError(label + '屏障边须为[a,b]列表')
    if any(type(item) is not int for pair in barriers for item in pair):
        raise ValueError(label + '屏障边索引须为严格整数')
    if len(set(ids)) != len(ids) or len(set(read_ids)) != len(read_ids):
        raise ValueError(label + '索引重复')
    if not set(ids) <= set(read_ids):
        raise ValueError(label + 'read_ids必须包含全部ids')
    for value in alpha:
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
            raise ValueError(label + 'alpha须为0..1有限数值')
    return copy.deepcopy(region)


def _region_metrics(region):
    alpha = [float(value) for value in region.get('alpha', [])]
    return {
        'write_count': sum(1 for value in alpha if value > 0),
        'id_count': len(region.get('ids', [])),
        'read_count': len(region.get('read_ids', [])),
        'barrier_count': len(region.get('barriers', [])),
        'alpha_min': min(alpha, default=0.0),
        'alpha_max': max(alpha, default=0.0),
    }


def _region_entry_dict(entry):
    region = _json_loads_strict(entry.region_json, '区域JSON')
    region = _normalize_region(region, '区域“' + entry.region_name + '”')
    if region.get('target') != entry.target_key:
        raise ValueError('区域“' + entry.region_name + '”的target与登记key不一致')
    return region


def _find_region_entry(options, name):
    for entry in options.regions:
        if entry.region_name == name:
            return entry
    return None


def _read_stages(options):
    raw = getattr(options, 'stages_json', '[]') or '[]'
    stages = _json_loads_strict(raw, 'Stage配方')
    if not isinstance(stages, list):
        raise ValueError('Stage配方必须是列表')
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError('Stage ' + str(index + 1) + ' 不是字典')
        if not isinstance(stage.get('label', ''), str):
            raise ValueError('Stage ' + str(index + 1) + ' 名称不是字符串')
        operations = stage.get('operations', [])
        if not isinstance(operations, list):
            raise ValueError('Stage ' + str(index + 1) + ' 的operations不是列表')
        for op_index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise ValueError('Stage ' + str(index + 1) + ' 操作 ' +
                                 str(op_index + 1) + ' 不是字典')
    return copy.deepcopy(stages)


def _write_stages(options, stages):
    with _quiet():
        options.stages_json = json.dumps(_json_safe(stages, 'Stage配方'),
                                         ensure_ascii=False, allow_nan=False)


def _build_recipe(context, options, *, require_targets=False, require_nonempty=True):
    regions = {}
    required_keys = []
    for entry in options.regions:
        name = entry.region_name.strip()
        if not name:
            raise ValueError('区域库含空名称')
        if name in regions:
            raise ValueError('区域名称重复：' + name)
        region = _region_entry_dict(entry)
        regions[name] = region
        if region['target'] not in required_keys:
            required_keys.append(region['target'])
    stages = _read_stages(options)
    if require_nonempty and not regions:
        raise ValueError('配方尚未捕获任何region')
    if require_nonempty and not stages:
        raise ValueError('配方尚未加入任何操作Stage')
    recipe = {'schema': _SCHEMA, 'regions': regions, 'stages': stages}
    _json_safe(recipe)
    targets = None
    if require_targets:
        targets = _target_map(options, context)
        missing = [key for key in required_keys if key not in targets]
        if missing:
            raise ValueError('区域引用的目标逻辑key未显式映射：' + '、'.join(missing))
    return recipe, targets


def _signature(context, options, recipe, targets, rig):
    advanced = ''
    text = getattr(options, 'advanced_text', None)
    if text is not None:
        try:
            advanced = text.as_string()
        except (ReferenceError, AttributeError):
            advanced = '<Text已失效>'
    payload = {
        'recipe': recipe,
        'targets': sorted([key, obj.name, obj.data.name, obj.data.as_pointer()]
                          for key, obj in targets.items()),
        'rig': [rig.name, rig.data.name, rig.data.as_pointer()],
        'unit_scale': float(bpy.context.scene.unit_settings.scale_length),
        'advanced_text': advanced,
    }
    return _hash_json(payload)


def _zero_mass(target, ids, groups, budget=250000):
    existing = {name: target.vertex_groups.get(name) for name in groups}
    indices = {group.index for group in existing.values() if group is not None}
    if not indices:
        return None, '写组均不存在'
    if len(ids) * len(indices) > budget:
        return None, '区域较大，UI未预检'
    zero = 0
    for index in ids:
        if not 0 <= index < len(target.data.vertices):
            return None, '索引超出目标'
        total = 0.0
        for member in target.data.vertices[index].groups:
            if member.group in indices:
                total += float(member.weight)
        if total == 0.0:
            zero += 1
    return zero, ''


def _preflight_lines(context, options, recipe, targets, rig):
    lines = ['UI只读预检（不作为核心通过依据；权威结果见CHECK报告）']
    lines.append('骨架：' + getattr(rig, 'name', '未指定')
                 + ' / ' + getattr(getattr(rig, 'data', None), 'name', '?'))
    for key, target in targets.items():
        region_count = sum(1 for region in recipe['regions'].values()
                           if region.get('target') == key)
        operation_count = sum(
            1 for stage in recipe['stages'] for operation in stage.get('operations', [])
            if recipe['regions'].get(operation.get('region'), {}).get('target') == key)
        warning = ''
        if not region_count or not operation_count:
            warning = '；核心将拒绝该目标（缺少区域或操作）'
        lines.append(f'目标 {key} → {target.name} / {target.data.name}：'
                     f'{len(target.data.vertices)}点 · 区域{region_count} · '
                     f'参考操作{operation_count}{warning}')
    fingerprints = {}
    region_targets = {}
    for name, region in recipe['regions'].items():
        key = region['target']
        region_targets[name] = key
        target = targets.get(key)
        if target is None:
            lines.append(f'区域 {name}：目标key“{key}”未显式映射对象')
            continue
        if key not in fingerprints:
            try:
                fingerprints[key] = weight_repair.mesh_fingerprint(target)
            except Exception as exc:
                fingerprints[key] = {'error': str(exc)}
        metrics = _region_metrics(region)
        current = fingerprints[key]
        binding_ok = isinstance(current, dict) and 'error' not in current \
            and region.get('binding') == current
        mesh_users = sum(1 for obj in bpy.data.objects
                         if obj.type == 'MESH' and obj.data == target.data)
        shared = target.data.users != 1 or mesh_users != 1
        alpha = [float(value) for value in region.get('alpha', [])]
        lines.append(
            f'区域 {name} → {key}({target.name})：写域{metrics["write_count"]}点'
            f'（ids {metrics["id_count"]}）· 读域{metrics["read_count"]}点'
            f' · 屏障{metrics["barrier_count"]}条 · alpha {metrics["alpha_min"]:.6g}..'
            f'{metrics["alpha_max"]:.6g} · 绑定' + ('一致' if binding_ok else '不一致/需重捕或重绑定')
            + (' · 共享Mesh（核心将拒绝）' if shared else '')
        )
    for stage_index, stage in enumerate(recipe['stages'], 1):
        label = stage.get('label', '') or '(未命名)'
        lines.append(f'Stage {stage_index}「{label}」：{len(stage.get("operations", []))}个操作')
        for op_index, operation in enumerate(stage.get('operations', []), 1):
            kind = operation.get('kind', '?')
            region_name = operation.get('region', '')
            region = recipe['regions'].get(region_name, {})
            key = region_targets.get(region_name, region.get('target', '?'))
            target = targets.get(key)
            groups = list(operation.get('groups', []))
            locks, missing = [], []
            zero_text = '无法预检'
            if target is not None:
                for group_name in groups:
                    group = target.vertex_groups.get(group_name)
                    if group is None:
                        missing.append(group_name)
                    elif group.lock_weight:
                        locks.append(group_name)
                zero, note = _zero_mass(target, region.get('ids', []), groups)
                if zero is None:
                    zero_text = note
                else:
                    zero_text = f'{zero}/{len(region.get("ids", []))}点'
            lines.append(
                f'  操作{op_index} {_KIND_LABELS.get(kind, kind)} · 区域{region_name}'
                f' → {key} · 写组{len(groups)} · 锁定' + ('：' + '、'.join(locks) if locks else '：无')
                + ' · 缺失' + ('：' + '、'.join(missing) if missing else '：无')
                + ' · 零质量' + zero_text
            )
    lines.append('实际差值：需CHECK核心报告（changed_cells/changed_vertices/质量残差与预算）。')
    lines.append('实际deform、受保护consumer、锁与source身份由同核prepare再次核对。')
    return lines


def _key_label(key):
    return _KEY_LABELS.get(str(key), str(key))


def _value_text(value, limit=10):
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, float):
        return f'{value:.6g}'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _short(value, 80)
    if isinstance(value, (list, tuple)):
        if not value:
            return '空'
        if all(isinstance(item, (int, float, str, bool)) for item in value):
            shown = '、'.join(_value_text(item, limit) for item in value[:limit])
            return shown + (f' …共{len(value)}项' if len(value) > limit else '')
        return f'共{len(value)}项'
    if isinstance(value, dict):
        return f'共{len(value)}键'
    return _short(value)


def _join_label(label, key):
    text = str(label)
    core = text.rstrip('. ')
    if core.strip():
        return core + '.' + _key_label(key)
    return text + _key_label(key)


def _flatten_value(lines, label, value, depth=0, budget=None):
    if budget is None:
        budget = [160]
    if budget[0] <= 0:
        return
    if depth > 4:
        lines.append(f'{label}：{_value_text(value)}')
        budget[0] -= 1
        return
    if isinstance(value, dict):
        if not value:
            lines.append(f'{label}：空')
            budget[0] -= 1
            return
        for key, child in value.items():
            if key in _SKIP_REPORT_KEYS:
                lines.append(f'{_join_label(label, key)}：内部大型数据已省略')
                budget[0] -= 1
                continue
            _flatten_value(lines, _join_label(label, key), child, depth + 1, budget)
            if budget[0] <= 0:
                return
    elif isinstance(value, list):
        if not value:
            lines.append(f'{label}：空')
            budget[0] -= 1
        elif all(not isinstance(item, (dict, list)) for item in value):
            lines.append(f'{label}：{_value_text(value)}')
            budget[0] -= 1
        else:
            lines.append(f'{label}：共{len(value)}项')
            budget[0] -= 1
            for index, child in enumerate(value[:2], 1):
                _flatten_value(lines, f'{label}[{index}]', child, depth + 1, budget)
    else:
        lines.append(f'{label}：{_value_text(value)}')
        budget[0] -= 1


def _append_receipt(lines, prefix, receipt):
    if not isinstance(receipt, dict):
        lines.append(prefix + _value_text(receipt))
        return
    remaining = dict(receipt)
    quality = remaining.pop('quality', None)
    memberships = remaining.pop('memberships', remaining.pop('membership', None))
    operations = remaining.pop('operations', None)
    if isinstance(quality, dict):
        residual = quality.get('residual', quality.get('max_residual', '未提供'))
        budget = quality.get('budget', quality.get('max_budget', '未提供'))
        failures = quality.get('failures', quality.get('failed_points', '未提供'))
        lines.append(f'{prefix}质量残差：{_value_text(residual)} · '
                     f'质量预算：{_value_text(budget)} · '
                     f'失败点：{_value_text(failures)}')
        extras = {key: value for key, value in quality.items()
                  if key not in {'residual', 'max_residual', 'budget', 'max_budget',
                                 'failures', 'failed_points'}}
        if extras:
            _flatten_value(lines, prefix + '质量', extras)
    elif quality is not None:
        _flatten_value(lines, prefix + '质量', quality)
    if isinstance(memberships, dict):
        lines.append(prefix + 'membership增删：新增 '
                     + _value_text(memberships.get('added', '未提供'))
                     + ' · 移除 '
                     + _value_text(memberships.get('removed', '未提供')))
        extras = {key: value for key, value in memberships.items()
                  if key not in {'added', 'removed'}}
        if extras:
            _flatten_value(lines, prefix + 'membership', extras)
    elif memberships is not None:
        _flatten_value(lines, prefix + 'membership', memberships)
    if isinstance(operations, list) and operations \
            and all(isinstance(item, dict) for item in operations):
        lines.append(prefix + f'操作回执{len(operations)}项：')
        for index, operation in enumerate(operations, 1):
            lines.append(prefix + f'  操作{index}：')
            _append_receipt(lines, prefix + '    ', operation)
    elif operations is not None:
        lines.append(prefix + '操作/操作数：' + _value_text(operations))
    _flatten_value(lines, prefix, remaining)


def _format_report_lines(report):
    if not isinstance(report, dict):
        return ['核心报告不是字典：' + _short(report)]
    lines = ['核心CHECK报告（只读回执）']
    status = report.get('status')
    lines.append('状态：' + _STATUS_LABELS.get(status, str(status)))
    if 'source_unchanged' in report:
        lines.append('源未改动：' + ('是' if report.get('source_unchanged') else '否'))
    if report.get('recipe_sha256'):
        lines.append('配方摘要：' + str(report['recipe_sha256'])[:16] + '…')
    targets = report.get('targets')
    if isinstance(targets, dict):
        for key, data in targets.items():
            lines.append(f'目标 {key}：源 {data.get("source", "?")}')
            for field in ('changed_vertices', 'changed_cells', 'deform_mass_max_drift',
                          'numeric_field_extractions', 'protection_snapshots_separate',
                          'candidate_clones'):
                if field in data:
                    lines.append('  ' + _key_label(field) + '：' + _value_text(data[field]))
            if any(field in data for field in ('changed_vertices', 'changed_cells',
                                               'deform_mass_max_drift')):
                lines.append(
                    '  差值与质量：变更点 ' + _value_text(data.get('changed_vertices', '未提供'))
                    + ' · 变更cell ' + _value_text(data.get('changed_cells', '未提供'))
                    + ' · 形变质量和最大漂移 '
                    + _value_text(data.get('deform_mass_max_drift', '未提供')))
            output = data.get('output')
            if output:
                lines.append('  独立候选：' + str(output))
            if data.get('output_sha256'):
                lines.append('  输出摘要：' + str(data['output_sha256'])[:16] + '…')
            steps = data.get('steps')
            if isinstance(steps, list) and steps:
                for step_index, step in enumerate(steps, 1):
                    lines.append(f'  第{step_index}个Stage回执：')
                    _append_receipt(lines, '    ', step)
            else:
                _flatten_value(lines, '  ', {k: v for k, v in data.items()
                                             if k not in {'source', 'binding', 'steps'}})
    else:
        _flatten_value(lines, '报告', {k: v for k, v in report.items()
                                       if k not in {'targets'}})
    if isinstance(targets, dict):
        extra = {key: value for key, value in report.items()
                 if key not in {'schema', 'status', 'source_unchanged', 'recipe_sha256',
                                'targets', 'timing'}}
        if extra:
            _flatten_value(lines, '报告附加', extra)
    timing = report.get('timing')
    if isinstance(timing, dict):
        for key, value in timing.items():
            lines.append('计时.' + _key_label(key) + '：' + _value_text(value))
    if len(lines) > 180:
        lines = lines[:180] + ['报告较长：其余字段已截断显示。']
    return lines


def _report_json_text(report):
    try:
        text = json.dumps(_json_safe(report, '核心报告'), ensure_ascii=False,
                          allow_nan=False)
        return text if len(text) <= 200000 else ''
    except Exception:
        return ''


def _safe_report_lines(report):
    try:
        return _format_report_lines(report)
    except Exception as exc:
        return ['报告格式化失败（原始报告仍可能可用）：' + str(exc)]


def _format_pairing_lines(name, entry):
    pairing = entry.get('pairing', {})
    lines = [f'配对「{name}」：目标key {entry.get("target_key", "?")}'
             f' → {entry.get("target_name", "?")}']
    lines.append(f'  对数 {_count_list(pairing.get("pairs", []))} · '
                 f'左侧域 {_count_list(pairing.get("left_ids", []))} · '
                 f'右侧域 {_count_list(pairing.get("right_ids", []))} · '
                 f'中心域 {_count_list(pairing.get("center_ids", []))} · '
                 f'最大距离 {_value_text(pairing.get("max_distance", 0.0))}m')
    lines.append(f'  未配 {_count_list(pairing.get("unmatched", []))} · '
                 f'歧义 {_count_list(pairing.get("ambiguous", []))} · '
                 + ('已明确确认' if pairing.get('confirmed') else '尚未确认，不能加入MIRROR'))
    unmatched = pairing.get('unmatched')
    if unmatched:
        lines.append('  未配示例：' + _value_text(unmatched))
    ambiguous = pairing.get('ambiguous')
    if ambiguous:
        lines.append('  歧义示例：' + _value_text(ambiguous))
    try:
        axis_value = int(pairing.get('axis', 0))
    except (TypeError, ValueError):
        axis_value = pairing.get('axis', 0)
    axis = {0: 'X', 1: 'Y', 2: 'Z'}.get(axis_value, str(axis_value))
    lines.append('  轴 ' + axis + ' · 平面 '
                 + _value_text(pairing.get('plane_meters', 0.0)) + 'm · 容差 '
                 + _value_text(pairing.get('tolerance_meters', 0.0)) + 'm')
    return lines


def _clear_pairing_previews(session):
    if not session:
        return
    for obj, mesh in list(session.get('pairing_preview_objects', [])):
        try:
            if bpy.data.objects.get(obj.name) == obj:
                bpy.data.objects.remove(obj, do_unlink=True)
        except ReferenceError:
            pass
        try:
            if bpy.data.meshes.get(mesh.name) == mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        except ReferenceError:
            pass
    session['pairing_preview_names'] = []
    session['pairing_preview_objects'] = []


def _pairing_preview(context, target, pairing, name):
    pairs = pairing.get('pairs') or []
    if not pairs:
        raise ValueError('配对没有可预览的线段')
    vertices, edges = [], []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            raise ValueError('配对项不是[left,right]')
        left, right = int(pair[0]), int(pair[1])
        if left == right:
            continue
        if not 0 <= left < len(target.data.vertices) \
                or not 0 <= right < len(target.data.vertices):
            raise ValueError('配对索引超出目标网格')
        left_world = target.matrix_world @ target.data.vertices[left].co
        right_world = target.matrix_world @ target.data.vertices[right].co
        vertices.extend((tuple(left_world), tuple(right_world)))
        edge_index = len(edges)
        edges.append((edge_index * 2, edge_index * 2 + 1))
    if not edges:
        raise ValueError('配对只有中心自配点，没有跨侧线段可预览')
    mesh = bpy.data.meshes.new('RWR_PairingPreviewMesh')
    mesh.from_pydata(vertices, edges, [])
    mesh.update()
    obj = bpy.data.objects.new(_unique_preview_name('RWR_配对预览_' + name), mesh)
    context.scene.collection.objects.link(obj)
    obj.display_type = 'WIRE'
    obj.show_in_front = True
    obj.hide_render = True
    obj['rigging_weight_repair_ui_preview'] = True
    obj['source_target'] = target.name
    obj['pairing_name'] = name
    return obj


def _select_objects(context, objects):
    if not objects:
        return
    for selected in tuple(context.selected_objects):
        try:
            selected.select_set(False)
        except RuntimeError:
            pass
    for obj in objects:
        try:
            obj.select_set(True)
        except RuntimeError:
            pass
    context.view_layer.objects.active = objects[0]
    _redraw()


def _select_region_vertices(context, target, ids, label):
    if context.mode != 'OBJECT':
        if context.object is None:
            raise ValueError('先切回对象模式再预览区域')
        bpy.ops.object.mode_set(mode='OBJECT')
    _select_objects(context, [target])
    try:
        bpy.ops.object.mode_set(mode='EDIT')
    except RuntimeError as exc:
        raise ValueError('无法进入目标编辑模式；请检查可见性与选择锁：' + str(exc)) from exc
    bm = bmesh.from_edit_mesh(target.data)
    wanted = set(ids)
    if bm.verts:
        bm.select_mode = {'VERT'}
    for vertex in bm.verts:
        vertex.select = vertex.index in wanted
    bmesh.update_edit_mesh(target.data)
    _redraw()
    return len(wanted)


def _unique_preview_name(base):
    stem = (base or '权重区域预览').strip() or '权重区域预览'
    name, index = stem, 2
    while name in bpy.data.objects:
        name = stem + '_' + str(index)
        index += 1
    return name


class RWR_TargetEntry(bpy.types.PropertyGroup):
    key: StringProperty(name='逻辑key')
    obj: PointerProperty(name='写入目标对象', type=bpy.types.Object, poll=_mesh)
    hint: StringProperty(name='配方绑定对象名（仅提示，不自动绑定）', options={'HIDDEN'})


class RWR_RegionEntry(bpy.types.PropertyGroup):
    region_name: StringProperty(name='区域名称')
    target_key: StringProperty(name='目标逻辑key')
    region_json: StringProperty(options={'HIDDEN'})
    source_label: StringProperty(name='捕获来源', options={'HIDDEN'})
    write_count: IntProperty(default=0, options={'HIDDEN'})
    id_count: IntProperty(default=0, options={'HIDDEN'})
    read_count: IntProperty(default=0, options={'HIDDEN'})
    barrier_count: IntProperty(default=0, options={'HIDDEN'})
    alpha_min: FloatProperty(default=0.0, options={'HIDDEN'})
    alpha_max: FloatProperty(default=0.0, options={'HIDDEN'})


class RWR_Options(bpy.types.PropertyGroup):
    # 当前写入目标与骨架。逻辑key不会自动猜测：必须先登记。
    target: PointerProperty(name='当前写入目标', type=bpy.types.Object, poll=_mesh,
                            update=_changed,
                            description='用于捕获区域与镜像配对；不直接修改网格')
    target_key: StringProperty(name='目标标识', update=_changed,
                               description='给该写入目标一个明确名称，便于复用和重新对应')
    rig: PointerProperty(name='明确骨架', type=bpy.types.Object, poll=_armature,
                         update=_changed)
    candidate_prefix: StringProperty(name='候选名称前缀', default='局部权重候选',
                                     description='只影响预览/候选命名，不改变权重计算')
    targets: CollectionProperty(type=RWR_TargetEntry)
    target_active: IntProperty(default=-1, options={'HIDDEN'})

    regions: CollectionProperty(type=RWR_RegionEntry)
    stages_json: StringProperty(default='[]', options={'HIDDEN'})
    stage_selected: IntProperty(default=-1, options={'HIDDEN'})
    op_selected: IntProperty(default=-1, options={'HIDDEN'})
    merge_stage: IntProperty(name='加入步骤组（0=新建）', default=0, min=0, max=1000,
                             update=_changed,
                             description='默认每操作独立Stage；填已有1基编号则追加到该Stage，冲突交给同核prepare拒绝')
    stage_label: StringProperty(name='步骤组名称（空则自动）', update=_changed)

    # 区域捕获表单。
    region_label: StringProperty(name='新区域名称', update=_changed)
    capture_source: EnumProperty(
        name='捕获来源',
        items=(('SELECTION', '当前可见选点', '从目标的可见RAW选点捕获'),
               ('MASK', '正值遮罩组', '取指定顶点组全部正权重点')),
        default='SELECTION', update=_changed)
    mask_group: StringProperty(name='遮罩组（可选搜索）', update=_changed)
    rings: IntProperty(name='RAW边扩圈层数', default=0, min=0, max=100, update=_changed)
    barriers: StringProperty(name='屏障边 a-b（逗号/换行）', update=_changed)
    allowed_ids_json: StringProperty(name='允许点RAW索引（可选JSON）', update=_changed)

    # 操作编辑器。
    op_region: StringProperty(name='操作区域名称', update=_changed)
    op_kind: EnumProperty(
        name='操作类型',
        items=(
            ('ASSIGN', '完整/部分赋值', '显式逐组scalar或逐点数组，余量只给显式remainder'),
            ('TRANSFER_AMOUNT', '定量转移', '源组→接收组固定量或源值比例'),
            ('REBALANCE', '比例重平衡', '写组非负倍率，仅重分配原可写质量'),
            ('SMOOTH', '屏障邻接Smooth', '按区域read_ids与屏障边同步平滑'),
            ('SURFACE_TRANSFER', '有界表面转移', '显式供体、米制距离与完整骨映射'),
            ('MIRROR', '对称镜像', '使用已明确确认的配对与显式组映射'),
            ('FIELD', '高级显式场', '复杂逐点场JSON，仅高级入口'),
        ),
        default='ASSIGN', update=_changed)
    op_groups: StringProperty(name='可写骨组（逗号/分号）', update=_changed)

    assign_values: StringProperty(name='赋值 组=数值（每行）', update=_changed)
    assign_values_json: StringProperty(name='高级：values JSON（逐点数组）', update=_changed)
    assign_remainder: StringProperty(name='显式剩余供体组（逗号）', update=_changed)

    transfer_source: StringProperty(name='源组', update=_changed)
    transfer_receiver: StringProperty(name='接收组', update=_changed)
    transfer_amount: FloatProperty(name='转移量（标量）', default=0.05, update=_transfer_amount_changed)
    transfer_amount_json: StringProperty(name='高级：逐点转移量JSON', update=_changed)
    transfer_mode: EnumProperty(
        name='转移模式',
        items=(('ABSOLUTE', '绝对量', '从源组扣除固定量并加到接收组'),
               ('FRACTION', '源值比例', '转移源组当前值的指定比例')),
        default='ABSOLUTE', update=_changed)

    rebalance_factors: StringProperty(name='倍率 组=数值（每行）', update=_changed)
    smooth_iterations: IntProperty(name='Smooth迭代次数', default=1, min=1, max=100,
                                   update=_changed)

    surface_donor: PointerProperty(name='表面供体对象（不能是写入target）',
                                   type=bpy.types.Object, poll=_mesh, update=_changed)
    surface_max_distance: FloatProperty(name='最大米制距离', default=0.001,
                                        precision=7, update=_surface_distance_changed)
    surface_distance_exact: StringProperty(options={'HIDDEN'})
    surface_bone_mapping: StringProperty(name='供体组=目标组（每行）', update=_changed)
    surface_donor_ids_json: StringProperty(name='高级：供体RAW索引JSON（可空）', update=_changed)

    mirror_pairing_name: StringProperty(name='已确认配对名称', update=_changed)
    mirror_mode: EnumProperty(
        name='镜像模式',
        items=(('COPY', '单向复制（左→右）', '写域必须在右侧配对点'),
               ('AVERAGE', '对称平均', '组映射必须是双向对合')),
        default='COPY', update=_changed)
    mirror_group_mapping: StringProperty(name='源组=目标组（每行）', update=_changed)
    field_json: StringProperty(name='FIELD矩阵JSON（len(ids)×len(groups)）', update=_changed)

    # 镜像配对表单。
    pairing_name: StringProperty(name='配对名称', default='左→右', update=_changed)
    pairing_left_region: StringProperty(name='左侧点域region', update=_changed)
    pairing_right_region: StringProperty(name='右侧点域region', update=_changed)
    pairing_center_region: StringProperty(name='中心点域region（可选）', update=_changed)
    pairing_axis: EnumProperty(
        name='对称轴',
        items=(('0', 'X轴', ''), ('1', 'Y轴', ''), ('2', 'Z轴', '')),
        default='0', update=_changed)
    pairing_plane: FloatProperty(name='对称平面（米）', default=0.0, update=_changed)
    pairing_tolerance: FloatProperty(name='配对容差（米）', default=0.001,
                                     precision=7, update=_changed)

    # 人工文件与高级Text。
    recipe_path: StringProperty(name='配方JSON路径', subtype='FILE_PATH')
    advanced_text: PointerProperty(name='高级配方JSON Text', type=bpy.types.Text,
                                   update=_changed,
                                   description='仅用于复杂逐点数组/整配方审阅；常规操作在面板完成')

    summary: StringProperty(default='先登记目标逻辑key与骨架，再捕获区域、加入Stage并CHECK',
                            options={'HIDDEN'})
    report_json: StringProperty(options={'HIDDEN'})


def _parse_assign_values(options, region):
    ids = region['ids']
    raw = options.assign_values_json.strip()
    if raw:
        values = _json_loads_strict(raw, 'ASSIGN values JSON')
        if not isinstance(values, dict):
            raise ValueError('ASSIGN高级values必须是 组→scalar/数组 的JSON对象')
    else:
        values = _parse_name_scalars(options.assign_values, 'ASSIGN赋值')
    result = {}
    for name, value in values.items():
        if isinstance(value, bool):
            raise ValueError('ASSIGN数值不接受bool：' + name)
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise ValueError('ASSIGN标量必须有限：' + name)
            result[name] = float(value)
        elif isinstance(value, list):
            if len(value) != len(ids):
                raise ValueError('ASSIGN逐点数组长度必须等于region ids：' + name)
            if any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(float(item)) for item in value):
                raise ValueError('ASSIGN逐点数组须为有限数值：' + name)
            result[name] = [float(item) for item in value]
        else:
            raise ValueError('ASSIGN值只接受scalar或逐点数组：' + name)
    return result


def _parse_rebalance_factors(options, groups):
    factors = _parse_name_scalars(options.rebalance_factors, 'REBALANCE倍率')
    missing = sorted(set(groups) - set(factors))
    if missing:
        raise ValueError('REBALANCE倍率必须覆盖全部可写骨组；缺少：'
                         + '、'.join(missing))
    if any(value < 0 for value in factors.values()):
        raise ValueError('REBALANCE倍率必须非负')
    return factors


def _parse_transfer_amount(options, region):
    raw = options.transfer_amount_json.strip()
    if raw:
        amount = _json_loads_strict(raw, 'TRANSFER_AMOUNT逐点量JSON')
        if isinstance(amount, list):
            if len(amount) != len(region['ids']):
                raise ValueError('逐点转移量长度必须等于region ids')
            if any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(float(item)) for item in amount):
                raise ValueError('逐点转移量须为有限数值')
            return [float(item) for item in amount]
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) \
                and math.isfinite(float(amount)):
            return float(amount)
        raise ValueError('转移量高级JSON须为scalar或逐点数组')
    value = float(options.transfer_amount)
    if not math.isfinite(value):
        raise ValueError('转移量必须有限')
    return value


def _operation_from_form(context, options):
    kind = options.op_kind
    region_name = options.op_region.strip()
    if not region_name:
        raise ValueError('先指定操作使用的region名称')
    entry = _find_region_entry(options, region_name)
    if entry is None:
        raise ValueError('操作引用的region不存在：' + region_name)
    region = _region_entry_dict(entry)
    ids = region['ids']

    if kind == 'TRANSFER_AMOUNT':
        source = options.transfer_source.strip()
        receiver = options.transfer_receiver.strip()
        if not source or not receiver:
            raise ValueError('定量转移必须明确源组与接收组')
        if source == receiver:
            raise ValueError('定量转移源组与接收组必须不同')
        groups = [source, receiver]
    else:
        groups = _split_names(options.op_groups)
        if not groups:
            raise ValueError('明确填写至少一个可写骨组')
        if len(set(groups)) != len(groups):
            raise ValueError('可写骨组含重复名称')

    operation = {'region': region_name, 'kind': kind, 'groups': groups}

    if kind == 'ASSIGN':
        values = _parse_assign_values(options, region)
        remainder = _split_names(options.assign_remainder)
        if len(set(remainder)) != len(remainder):
            raise ValueError('ASSIGN remainder含重复组')
        keys, remaining = set(values), set(remainder)
        if keys & remaining:
            raise ValueError('ASSIGN values与remainder不能重叠：'
                             + '、'.join(sorted(keys & remaining)))
        if keys | remaining != set(groups):
            missing = sorted(set(groups) - keys - remaining)
            extra = sorted((keys | remaining) - set(groups))
            raise ValueError('ASSIGN必须显式覆盖全部groups；缺少：'
                             + ('、'.join(missing) or '无') + '；多出：'
                             + ('、'.join(extra) or '无'))
        operation.update(values=values, remainder=remainder)
    elif kind == 'TRANSFER_AMOUNT':
        operation.update(amount=_parse_transfer_amount(options, region),
                         mode=options.transfer_mode)
    elif kind == 'REBALANCE':
        operation['factors'] = _parse_rebalance_factors(options, groups)
    elif kind == 'SMOOTH':
        iterations = int(options.smooth_iterations)
        if not 1 <= iterations <= 100:
            raise ValueError('Smooth迭代次数必须是1..100整数')
        operation['iterations'] = iterations
    elif kind == 'SURFACE_TRANSFER':
        donor = options.surface_donor
        if donor is None or donor.type != 'MESH':
            raise ValueError('选择明确表面供体网格对象')
        if bpy.data.objects.get(donor.name) != donor:
            raise ValueError('供体对象已失效')
        if context.scene.objects.get(donor.name) != donor:
            raise ValueError('供体必须位于当前场景：' + donor.name)
        if any(entry.obj == donor for entry in options.targets):
            raise ValueError('供体不能是本次任何写入target')
        mapping = _parse_mapping(options.surface_bone_mapping, '表面转移骨映射')
        missing = sorted(set(groups) - set(mapping.values()))
        if missing:
            raise ValueError('可写目标组缺少显式供体映射：' + '、'.join(missing))
        distance = float(options.surface_distance_exact) if options.surface_distance_exact \
            else float(options.surface_max_distance)
        if not math.isfinite(distance) or distance <= 0:
            raise ValueError('最大表面距离必须是有限正米制数值')
        donor_ids = _parse_int_list_json(options.surface_donor_ids_json, '供体RAW索引')
        operation.update(donor=donor.name, donor_ids=donor_ids,
                         max_distance_meters=distance, bone_mapping=mapping)
    elif kind == 'MIRROR':
        pairing_name = options.mirror_pairing_name.strip()
        if not pairing_name:
            raise ValueError('先选择已明确确认的镜像配对')
        session = _session(context, create=False)
        pairing_entry = session['pairings'].get(pairing_name) if session else None
        if pairing_entry is None:
            raise ValueError('当前会话没有该配对：' + pairing_name)
        pairing = pairing_entry.get('pairing', {})
        if not pairing.get('confirmed'):
            raise ValueError('镜像配对尚未CONFIRM，不能加入Stage')
        if pairing_entry.get('target_key') != entry.target_key:
            raise ValueError('镜像配对与操作region不属于同一目标逻辑key')
        mode = options.mirror_mode
        mapping = _parse_mapping(options.mirror_group_mapping, '镜像骨组映射')
        if set(mapping.values()) != set(groups):
            raise ValueError('镜像映射目标组必须与可写骨组完全一致')
        if mode == 'AVERAGE' and any(mapping.get(mapping[name]) != name
                                     for name in mapping):
            raise ValueError('对称平均的组映射必须是双向对合')
        pairs = pairing.get('pairs', [])
        if mode == 'COPY':
            right_ids = {int(right) for left, right in pairs}
            if not set(ids) <= right_ids:
                raise ValueError('COPY镜像固定左→右；写域region必须全部位于'
                                 '实际已配对的右侧点')
        else:
            both = {int(item) for pair in pairs for item in pair[:2]}
            if not set(ids) <= both:
                raise ValueError('AVERAGE镜像写域含未配对点')
        operation.update(mode=mode, pairing=copy.deepcopy(pairing),
                         group_mapping=mapping)
    elif kind == 'FIELD':
        raw = options.field_json.strip()
        if not raw:
            raise ValueError('FIELD必须提供显式JSON矩阵')
        field = _json_loads_strict(raw, 'FIELD矩阵')
        if not isinstance(field, list) or len(field) != len(ids):
            raise ValueError('FIELD行数必须等于region ids')
        for row in field:
            if not isinstance(row, list) or len(row) != len(groups):
                raise ValueError('FIELD每行列数必须等于可写骨组数')
            if any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(float(item)) or float(item) < 0
                   for item in row):
                raise ValueError('FIELD只接受有限非负数值')
        operation['field'] = field
    else:
        raise ValueError('未知操作类型：' + str(kind))
    return _json_safe(operation, '操作')


def _registered_object(options, key, *, label='目标'):
    for entry in options.targets:
        if entry.key == key:
            if entry.obj is None:
                raise ValueError(label + '逻辑key尚未指定对象：' + key)
            return entry.obj
    raise ValueError(label + '逻辑key未登记：' + key)


def _stage_references_region(options, region_name):
    try:
        stages = _read_stages(options)
    except Exception:
        return False
    return any(operation.get('region') == region_name
               for stage in stages for operation in stage.get('operations', []))


def _stage_references_target(options, key):
    try:
        stages = _read_stages(options)
    except Exception:
        return False
    region_keys = {}
    for entry in options.regions:
        region_keys[entry.region_name] = entry.target_key
    return any(region_keys.get(operation.get('region')) == key
               for stage in stages for operation in stage.get('operations', []))


class RWR_OT_Target(bpy.types.Operator):
    bl_idname = 'rigging_weight_repair.target'
    bl_label = '目标逻辑key登记与选择'
    bl_description = '显式登记写入目标；不自动按对象名猜测逻辑key'
    bl_options = {'REGISTER'}

    action: EnumProperty(items=(
        ('REGISTER', '登记/更新目标', ''),
        ('SELECT', '设为当前目标', ''),
        ('MAP_NAME', '用对象名填入key', ''),
        ('REMOVE', '删除目标登记', ''),
    ))
    index: IntProperty(default=-1)

    def execute(self, context):
        options = _options(context)
        try:
            if self.action == 'REGISTER':
                target, key = options.target, options.target_key.strip()
                if target is None or target.type != 'MESH':
                    raise ValueError('先选择当前写入目标网格')
                if context.scene.objects.get(target.name) != target:
                    raise ValueError('目标不在当前场景：' + target.name)
                if not key:
                    raise ValueError('明确填写目标逻辑key；不会自动猜测')
                existing = None
                remapped = False
                for entry in options.targets:
                    if entry.key == key:
                        existing = entry
                    elif entry.obj == target:
                        raise ValueError('该对象已用另一个逻辑key登记：' + entry.key)
                if existing is not None:
                    remapped = existing.obj not in {None, target}
                    existing.obj = target
                    existing.hint = target.name
                    if remapped:
                        session = _session(context, create=False)
                        if session:
                            for name in [name for name, item in session['pairings'].items()
                                         if item.get('target_key') == key]:
                                session['pairings'].pop(name, None)
                                session['pairing_lines'].pop(name, None)
                            _clear_pairing_previews(session)
                else:
                    entry = options.targets.add()
                    entry.key, entry.obj, entry.hint = key, target, target.name
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'目标逻辑key已显式登记：{key} → {target.name}；'
                                      '配方目标映射已改变，请重新CHECK'
                                      + ('；已更换对象，旧region需先同输入重绑定'
                                         if remapped else ''))
                self.report({'INFO'}, '已登记目标：' + key)
            elif self.action == 'SELECT':
                if not 0 <= self.index < len(options.targets):
                    raise ValueError('先选择有效目标登记项')
                entry = options.targets[self.index]
                if entry.obj is None:
                    raise ValueError('该登记尚未指定对象；先选择网格并登记')
                with _quiet():
                    options.target, options.target_key = entry.obj, entry.key
                    options.target_active = self.index
                _set_summary(context, f'当前目标已切换为 {entry.key} → {entry.obj.name}；'
                                      '仅切换编辑焦点，现有配方未改')
            elif self.action == 'MAP_NAME':
                target = options.target
                if target is None:
                    raise ValueError('先选择当前目标对象')
                with _quiet():
                    options.target_key = target.name
                _set_summary(context, f'已把对象名“{target.name}”填入逻辑key；'
                                      '仍需点“登记/更新目标”完成显式映射')
            elif self.action == 'REMOVE':
                if not 0 <= self.index < len(options.targets):
                    raise ValueError('先选择有效目标登记项')
                entry = options.targets[self.index]
                key = entry.key
                if _stage_references_target(options, key):
                    raise ValueError('目标仍被Stage操作引用；先删除相关操作')
                for region in options.regions:
                    if region.target_key == key:
                        raise ValueError('目标仍被区域库引用；先删除区域：'
                                         + region.region_name)
                session = _session(context, create=False)
                if session:
                    for name in [name for name, item in session['pairings'].items()
                                 if item.get('target_key') == key]:
                        session['pairings'].pop(name, None)
                        session['pairing_lines'].pop(name, None)
                active = options.target_key == key
                options.targets.remove(self.index)
                if active:
                    with _quiet():
                        options.target = None
                        options.target_key = ''
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'已删除目标逻辑key登记：{key}；配方目标映射已改变，'
                                      '请重新CHECK')
            else:
                raise ValueError('未知目标操作')
            _redraw()
            return {'FINISHED'}
        except Exception as exc:
            _set_summary(context, '目标操作未完成：' + str(exc))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class RWR_OT_Region(bpy.types.Operator):
    bl_idname = 'rigging_weight_repair.region'
    bl_label = '区域捕获、预览与重绑定'
    bl_description = '捕获后保存RAW输入binding；预览只改变选择，不改权重或几何'
    bl_options = {'REGISTER'}

    action: EnumProperty(items=(
        ('CAPTURE', '捕获并保存区域', ''),
        ('PREVIEW_WRITE', '选择写域', ''),
        ('PREVIEW_READ', '选择读域', ''),
        ('USE_IN_OPERATION', '设为操作区域', ''),
        ('SET_LEFT_PAIRING', '设为左侧配对点域', ''),
        ('SET_RIGHT_PAIRING', '设为右侧配对点域', ''),
        ('SET_CENTER_PAIRING', '设为中心配对点域', ''),
        ('DELETE', '删除区域', ''),
        ('REBIND', '同输入显式重绑定', ''),
    ))
    index: IntProperty(default=-1)

    def execute(self, context):
        options = _options(context)
        try:
            if self.action == 'CAPTURE':
                key = options.target_key.strip()
                target = options.target
                if target is None or target.type != 'MESH':
                    raise ValueError('先选择当前写入目标网格')
                if context.scene.objects.get(target.name) != target:
                    raise ValueError('目标不在当前场景：' + target.name)
                if not key:
                    raise ValueError('先明确目标逻辑key并登记')
                registered = _registered_object(options, key)
                if registered != target:
                    raise ValueError('当前目标与逻辑key登记对象不一致；先重新登记')
                name = options.region_label.strip()
                if not name:
                    raise ValueError('明确填写新区域名称')
                if _find_region_entry(options, name) is not None:
                    raise ValueError('区域名称已存在；请换名或先删除旧区域')
                barriers = _parse_barriers(options.barriers)
                allowed = _parse_int_list_json(options.allowed_ids_json, '允许点RAW索引')
                mask = None
                if options.capture_source == 'MASK':
                    mask = options.mask_group.strip()
                    if not mask:
                        raise ValueError('遮罩组来源需要明确组名')
                    if target.vertex_groups.get(mask) is None:
                        raise ValueError('遮罩组不存在：' + mask)
                region = weight_repair.capture_region(
                    target,
                    indices=None,
                    mask_group=mask,
                    rings=int(options.rings),
                    barriers=barriers,
                    allowed_ids=allowed,
                )
                if not isinstance(region, dict):
                    raise ValueError('核心没有返回区域字典')
                region = copy.deepcopy(region)
                region['target'] = key
                region = _normalize_region(region, '捕获结果“' + name + '”')
                entry = options.regions.add()
                entry.region_name = name
                entry.target_key = key
                entry.region_json = json.dumps(region, ensure_ascii=False, allow_nan=False)
                source = '遮罩组 ' + (mask or '') if mask else '当前可见选点'
                entry.source_label = source + f'；扩圈{int(options.rings)}'
                metrics = _region_metrics(region)
                for field, value in metrics.items():
                    setattr(entry, field, value)
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(
                    context,
                    f'已捕获区域“{name}”：写域{metrics["write_count"]}点，'
                    f'读域{metrics["read_count"]}点，屏障{metrics["barrier_count"]}条；'
                    '已保存RAW binding，配方已改变，请重新CHECK')
                self.report({'INFO'}, '区域已捕获：' + name)
            elif self.action in {'PREVIEW_WRITE', 'PREVIEW_READ'}:
                if not 0 <= self.index < len(options.regions):
                    raise ValueError('先选择有效区域')
                if context.mode != 'OBJECT':
                    if context.object is None:
                        raise ValueError('先切回对象模式再预览区域')
                    bpy.ops.object.mode_set(mode='OBJECT')
                entry = options.regions[self.index]
                region = _region_entry_dict(entry)
                target = _registered_object(options, entry.target_key)
                if context.scene.objects.get(target.name) != target:
                    raise ValueError('区域目标不在当前场景：' + target.name)
                try:
                    current = weight_repair.mesh_fingerprint(target)
                except Exception as exc:
                    raise ValueError('无法读取目标binding：' + str(exc)) from exc
                if region.get('binding') != current:
                    raise ValueError('区域绑定与当前RAW输入不一致；请重新捕获或先做'
                                     '“同输入显式重绑定”')
                ids = [i for i, alpha in zip(region['ids'], region['alpha']) if alpha > 0] \
                    if self.action == 'PREVIEW_WRITE' else region['read_ids']
                scope = '写域' if self.action == 'PREVIEW_WRITE' else '读域'
                count = _select_region_vertices(context, target, ids, scope)
                _set_summary(context, f'已在 {target.name} 选择{scope}{count}点；'
                                      '仅改变选择状态，未修改权重/几何')
            elif self.action == 'USE_IN_OPERATION':
                if not 0 <= self.index < len(options.regions):
                    raise ValueError('先选择有效区域')
                entry = options.regions[self.index]
                with _quiet():
                    options.op_region = entry.region_name
                _set_summary(context, f'操作区域已设为“{entry.region_name}”；'
                                      '尚未加入Stage')
            elif self.action in {'SET_LEFT_PAIRING', 'SET_RIGHT_PAIRING',
                                 'SET_CENTER_PAIRING'}:
                if not 0 <= self.index < len(options.regions):
                    raise ValueError('先选择有效区域')
                entry = options.regions[self.index]
                with _quiet():
                    if self.action == 'SET_LEFT_PAIRING':
                        options.pairing_left_region = entry.region_name
                    elif self.action == 'SET_RIGHT_PAIRING':
                        options.pairing_right_region = entry.region_name
                    else:
                        options.pairing_center_region = entry.region_name
                _set_summary(context, f'镜像配对点域已设为“{entry.region_name}”；'
                                      '仍需BUILD→审阅→CONFIRM')
            elif self.action == 'DELETE':
                if not 0 <= self.index < len(options.regions):
                    raise ValueError('先选择有效区域')
                entry = options.regions[self.index]
                if _stage_references_region(options, entry.region_name):
                    raise ValueError('区域仍被Stage操作引用；先删除相关操作')
                name = entry.region_name
                options.regions.remove(self.index)
                with _quiet():
                    if options.op_region == name:
                        options.op_region = ''
                    if options.pairing_left_region == name:
                        options.pairing_left_region = ''
                    if options.pairing_right_region == name:
                        options.pairing_right_region = ''
                    if options.pairing_center_region == name:
                        options.pairing_center_region = ''
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'已删除区域“{name}”；配方已改变，请重新CHECK')
            elif self.action == 'REBIND':
                if context.mode != 'OBJECT':
                    raise ValueError('同输入重绑定需要对象模式；先结束编辑/姿态模式')
                recipe, targets = _build_recipe(context, options,
                                                require_targets=True,
                                                require_nonempty=False)
                if not recipe['regions']:
                    raise ValueError('区域库为空，没有可重绑定的区域')
                bound = weight_repair.bind_recipe(recipe, targets)
                for entry in options.regions:
                    region = bound['regions'].get(entry.region_name)
                    if region is None:
                        raise ValueError('核心重绑定结果缺少区域：' + entry.region_name)
                    region = _normalize_region(region, '重绑定区域“' + entry.region_name + '”')
                    entry.region_json = json.dumps(region, ensure_ascii=False,
                                                   allow_nan=False)
                    metrics = _region_metrics(region)
                    for field, value in metrics.items():
                        setattr(entry, field, value)
                session = _session(context, create=False)
                if session:
                    session['pairings'].clear()
                    session['pairing_lines'].clear()
                    _clear_pairing_previews(session)
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, '已按当前同几何输入显式重绑定全部区域；'
                                      '会话镜像配对已清空，需重新BUILD/CONFIRM；'
                                      'Stage内已嵌入的MIRROR配对若过时会由核心拒绝，需重建操作；'
                                      '核心不会把旧RAW索引套到不同拓扑/变换/组上；请重新CHECK')
            else:
                raise ValueError('未知区域操作')
            _redraw()
            return {'FINISHED'}
        except Exception as exc:
            _set_summary(context, '区域操作未完成：' + str(exc))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class RWR_OT_Pairing(bpy.types.Operator):
    bl_idname = 'rigging_weight_repair.pairing'
    bl_label = '镜像配对构建与确认'
    bl_description = '先构建并审阅配对报告，再明确CONFIRM；未确认配对不能加入MIRROR'
    bl_options = {'REGISTER'}

    action: EnumProperty(items=(
        ('BUILD', '构建配对（未确认）', ''),
        ('PREVIEW', '预览配对线段', ''),
        ('CONFIRM', '确认当前配对', ''),
        ('SELECT', '设为当前配对', ''),
        ('DELETE', '删除会话配对', ''),
    ))
    index: IntProperty(default=-1)

    def execute(self, context):
        options = _options(context)
        try:
            session = _session(context, create=True)
            if self.action == 'BUILD':
                if context.mode != 'OBJECT':
                    raise ValueError('镜像配对BUILD需要对象模式；先结束编辑/姿态模式')
                key = options.target_key.strip()
                target = options.target
                if target is None or target.type != 'MESH':
                    raise ValueError('先选择当前写入目标网格')
                if context.scene.objects.get(target.name) != target:
                    raise ValueError('目标不在当前场景：' + target.name)
                if _registered_object(options, key) != target:
                    raise ValueError('当前目标与逻辑key登记对象不一致')
                name = options.pairing_name.strip()
                if not name:
                    raise ValueError('明确填写配对名称')
                if name in session['pairings']:
                    raise ValueError('同名配对已存在；先删除或换名')
                left_name = options.pairing_left_region.strip()
                right_name = options.pairing_right_region.strip()
                left_entry = _find_region_entry(options, left_name)
                right_entry = _find_region_entry(options, right_name)
                if left_entry is None or right_entry is None:
                    raise ValueError('先选择明确的左侧/右侧region')
                if left_entry.target_key != key or right_entry.target_key != key:
                    raise ValueError('左右region必须属于当前目标逻辑key')
                left = _region_entry_dict(left_entry)
                right = _region_entry_dict(right_entry)
                current = weight_repair.mesh_fingerprint(target)
                if left.get('binding') != current or right.get('binding') != current:
                    raise ValueError('左右region的RAW binding与当前目标不一致；'
                                     '请重新捕获或显式重绑定')
                center_ids = []
                center_name = options.pairing_center_region.strip()
                if center_name:
                    center_entry = _find_region_entry(options, center_name)
                    if center_entry is None:
                        raise ValueError('中心点域region不存在：' + center_name)
                    if center_entry.target_key != key:
                        raise ValueError('中心点域region必须属于当前目标逻辑key')
                    center = _region_entry_dict(center_entry)
                    if center.get('binding') != current:
                        raise ValueError('中心点域region的RAW binding与当前目标不一致')
                    center_ids = center['ids']
                tolerance = float(options.pairing_tolerance)
                if not math.isfinite(tolerance) or tolerance <= 0:
                    raise ValueError('配对容差必须是有限正米制数值')
                axis = int(options.pairing_axis)
                arguments = {
                    'axis': axis,
                    'plane_meters': float(options.pairing_plane),
                    'tolerance_meters': tolerance,
                }
                if center_ids:
                    arguments['center_ids'] = center_ids
                try:
                    pairing = weight_repair.build_mirror_pairs(
                        target, left['ids'], right['ids'], **arguments)
                except TypeError as exc:
                    if center_ids:
                        raise ValueError('当前核心不支持中心点域配对参数 center_ids：'
                                         + str(exc)) from exc
                    raise
                if not isinstance(pairing, dict):
                    raise ValueError('核心没有返回配对字典')
                entry = {
                    'pairing': _json_safe(pairing, '配对'),
                    'target_key': key,
                    'target_name': target.name,
                }
                session['pairings'][name] = entry
                lines = _format_pairing_lines(name, entry)
                session['pairing_lines'][name] = lines
                _clear_pairing_previews(session)
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'配对“{name}”已构建：'
                                      f'{_count_list(pairing.get("pairs", []))}对，'
                                      f'未配{_count_list(pairing.get("unmatched", []))}，'
                                      f'歧义{_count_list(pairing.get("ambiguous", []))}，'
                                      f'最大距离{_value_text(pairing.get("max_distance", 0.0))}m；'
                                      '尚未确认，不能加入MIRROR')
            elif self.action == 'PREVIEW':
                name = options.pairing_name.strip()
                entry = session['pairings'].get(name)
                if entry is None:
                    raise ValueError('没有该会话配对：' + name)
                target = _registered_object(options, entry['target_key'])
                if context.scene.objects.get(target.name) != target:
                    raise ValueError('配对目标不在当前场景')
                _clear_pairing_previews(session)
                obj = _pairing_preview(context, target, entry['pairing'], name)
                session['pairing_preview_names'] = [obj.name]
                session['pairing_preview_objects'] = [(obj, obj.data)]
                _select_objects(context, [obj])
                _set_summary(context, f'已创建配对线段预览「{obj.name}」：'
                                      f'{_count_list(entry["pairing"].get("pairs", []))}条；'
                                      'owned显示副本，重新BUILD/SAVE/LOAD/卸载会清理，'
                                      '确认前不能加入MIRROR')
            elif self.action == 'CONFIRM':
                if context.mode != 'OBJECT':
                    raise ValueError('镜像配对CONFIRM需要对象模式；先结束编辑/姿态模式')
                name = options.pairing_name.strip()
                entry = session['pairings'].get(name)
                if entry is None:
                    raise ValueError('没有该会话配对：' + name)
                target = _registered_object(options, entry['target_key'])
                confirmed = weight_repair.confirm_pairing(target, entry['pairing'])
                if not isinstance(confirmed, dict) or not confirmed.get('confirmed'):
                    raise ValueError('核心未返回confirmed配对')
                entry['pairing'] = _json_safe(confirmed, '确认配对')
                entry['target_name'] = target.name
                lines = _format_pairing_lines(name, entry)
                session['pairing_lines'][name] = lines
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'配对“{name}”已明确确认：'
                                      f'{_count_list(confirmed.get("pairs", []))}对；'
                                      '可以加入MIRROR，冲突仍由同核prepare拒绝')
            elif self.action == 'SELECT':
                names = sorted(session['pairings'])
                if not 0 <= self.index < len(names):
                    raise ValueError('先选择有效配对')
                name = names[self.index]
                with _quiet():
                    options.pairing_name = name
                    entry = session['pairings'][name]
                    options.mirror_pairing_name = name
                _set_summary(context, f'当前配对已切换为“{name}”；'
                                      '已确认状态：'
                                      + ('是' if entry['pairing'].get('confirmed') else '否'))
            elif self.action == 'DELETE':
                name = options.pairing_name.strip()
                if name not in session['pairings']:
                    raise ValueError('没有该会话配对：' + name)
                session['pairings'].pop(name, None)
                session['pairing_lines'].pop(name, None)
                _clear_pairing_previews(session)
                with _quiet():
                    if options.mirror_pairing_name == name:
                        options.mirror_pairing_name = ''
                _set_summary(context, f'会话配对“{name}”已删除；'
                                      '已加入Stage的MIRROR操作保留其冻结配对')
            else:
                raise ValueError('未知配对操作')
            _redraw()
            return {'FINISHED'}
        except Exception as exc:
            _set_summary(context, '镜像配对未完成：' + str(exc))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _auto_stage_label(stages):
    used = {stage.get('label', '') for stage in stages}
    index = len(stages) + 1
    while 'Stage ' + str(index) in used:
        index += 1
    return 'Stage ' + str(index)


def _clear_operation_form(options):
    with _quiet():
        options.assign_values = ''
        options.assign_values_json = ''
        options.assign_remainder = ''
        options.transfer_source = ''
        options.transfer_receiver = ''
        options.transfer_amount = 0.05
        options.transfer_amount_json = ''
        options.transfer_mode = 'ABSOLUTE'
        options.rebalance_factors = ''
        options.smooth_iterations = 1
        options.surface_donor = None
        options.surface_max_distance = 0.001
        options.surface_distance_exact = ''
        options.surface_bone_mapping = ''
        options.surface_donor_ids_json = ''
        options.mirror_pairing_name = ''
        options.mirror_mode = 'COPY'
        options.mirror_group_mapping = ''
        options.field_json = ''


def _unique_pairing_name(session, base):
    name, index = base, 2
    while name in session['pairings']:
        name = base + ' ' + str(index)
        index += 1
    return name


def _load_operation_into_form(context, options, operation, stage_index, op_index):
    session = _session(context, create=True)
    _clear_operation_form(options)
    kind = operation.get('kind', 'ASSIGN')
    region_name = operation.get('region', '')
    entry = _find_region_entry(options, region_name)
    if entry is None:
        raise ValueError('操作引用的region已不存在：' + str(region_name))
    region = _region_entry_dict(entry)
    groups = list(operation.get('groups', []))
    with _quiet():
        options.op_region = region_name
        options.op_kind = kind if kind in _KIND_LABELS else 'ASSIGN'
        options.op_groups = ', '.join(groups)
    if kind == 'ASSIGN':
        values = operation.get('values', {})
        if not isinstance(values, dict):
            raise ValueError('ASSIGN values不是字典')
        with _quiet():
            if any(isinstance(value, list) for value in values.values()):
                options.assign_values_json = json.dumps(values, ensure_ascii=False,
                                                         allow_nan=False)
            else:
                options.assign_values = '\n'.join(f'{name}={repr(value)}'
                                                   for name, value in values.items())
            options.assign_remainder = ', '.join(operation.get('remainder', []))
    elif kind == 'TRANSFER_AMOUNT':
        amount = operation.get('amount', 0.05)
        with _quiet():
            if isinstance(amount, list):
                options.transfer_amount_json = json.dumps(amount, ensure_ascii=False,
                                                          allow_nan=False)
            else:
                options.transfer_amount = float(amount)
                options.transfer_amount_json = json.dumps(amount, allow_nan=False)
            options.transfer_mode = operation.get('mode', 'ABSOLUTE')
            if len(groups) >= 2:
                options.transfer_source, options.transfer_receiver = groups[0], groups[1]
    elif kind == 'REBALANCE':
        factors = operation.get('factors', {})
        with _quiet():
            options.rebalance_factors = '\n'.join(f'{name}={repr(value)}'
                                                   for name, value in factors.items())
    elif kind == 'SMOOTH':
        with _quiet():
            options.smooth_iterations = int(operation.get('iterations', 1))
    elif kind == 'SURFACE_TRANSFER':
        donor = bpy.data.objects.get(str(operation.get('donor', '')))
        mapping = operation.get('bone_mapping', {})
        with _quiet():
            options.surface_donor = donor if donor is not None and donor.type == 'MESH' else None
            options.surface_max_distance = float(operation.get('max_distance_meters', 0.001))
            options.surface_distance_exact = json.dumps(operation.get('max_distance_meters', 0.001), allow_nan=False)
            options.surface_bone_mapping = '\n'.join(f'{source}={target}'
                                                      for source, target in mapping.items())
            if operation.get('donor_ids') is not None:
                options.surface_donor_ids_json = json.dumps(
                    operation.get('donor_ids'), ensure_ascii=False, allow_nan=False)
    elif kind == 'MIRROR':
        pairing = operation.get('pairing', {})
        if not isinstance(pairing, dict) or not pairing.get('confirmed'):
            raise ValueError('载入的MIRROR操作缺少confirmed配对')
        base = f'载入配对 {stage_index + 1}-{op_index + 1}'
        name = _unique_pairing_name(session, base)
        target = _registered_object(options, entry.target_key)
        session['pairings'][name] = {
            'pairing': copy.deepcopy(pairing),
            'target_key': entry.target_key,
            'target_name': target.name,
        }
        session['pairing_lines'][name] = _format_pairing_lines(
            name, session['pairings'][name])
        mapping = operation.get('group_mapping', {})
        with _quiet():
            options.pairing_name = name
            options.mirror_pairing_name = name
            options.mirror_mode = operation.get('mode', 'COPY')
            options.mirror_group_mapping = '\n'.join(
                f'{source}={target}' for source, target in mapping.items())
    elif kind == 'FIELD':
        with _quiet():
            options.field_json = json.dumps(operation.get('field', []),
                                            ensure_ascii=False, allow_nan=False)
    else:
        raise ValueError('未知操作类型：' + str(kind))
    return entry


class RWR_OT_Stage(bpy.types.Operator):
    bl_idname = 'rigging_weight_repair.stage'
    bl_label = 'Stage与操作编排'
    bl_description = '默认每操作独立Stage；可指定已有编号合并，不在此处掩盖同Stage冲突'
    bl_options = {'REGISTER'}

    action: EnumProperty(items=(
        ('ADD_OPERATION', '加入Stage', ''),
        ('UPDATE_OPERATION', '更新所选操作', ''),
        ('LOAD_OPERATION', '载入所选操作', ''),
        ('DELETE_OPERATION', '删除所选操作', ''),
        ('DELETE_STAGE', '删除Stage', ''),
        ('MOVE_UP', '上移Stage', ''),
        ('MOVE_DOWN', '下移Stage', ''),
        ('RENAME', '重命名Stage', ''),
        ('SELECT_STAGE', '选择Stage', ''),
        ('SELECT_OPERATION', '选择操作', ''),
    ))
    stage_index: IntProperty(default=-1)
    op_index: IntProperty(default=-1)

    def execute(self, context):
        options = _options(context)
        try:
            stages = _read_stages(options)
            action = self.action
            if action == 'ADD_OPERATION':
                operation = _operation_from_form(context, options)
                merge = int(options.merge_stage)
                if merge < 0 or merge > len(stages):
                    raise ValueError(f'合并Stage编号必须在0..{len(stages)}；'
                                     '0表示新建独立Stage')
                if merge == 0:
                    label = options.stage_label.strip() or _auto_stage_label(stages)
                    stages.append({'label': label, 'operations': [operation]})
                    stage_index = len(stages) - 1
                else:
                    stage_index = merge - 1
                    stages[stage_index].setdefault('operations', []).append(operation)
                _write_stages(options, stages)
                with _quiet():
                    options.stage_selected = stage_index
                    options.op_selected = len(stages[stage_index]['operations']) - 1
                    options.merge_stage = 0
                    options.stage_label = ''
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'已加入Stage {stage_index + 1}'
                                      f'「{stages[stage_index]["label"]}」；'
                                      '同Stage写域冲突不在UI静默解决，交同核prepare拒绝；请重新CHECK')
            elif action == 'UPDATE_OPERATION':
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                stage = stages[self.stage_index]
                if not 0 <= self.op_index < len(stage.get('operations', [])):
                    raise ValueError('先选择有效操作')
                operation = _operation_from_form(context, options)
                stage['operations'][self.op_index] = operation
                _write_stages(options, stages)
                with _quiet():
                    options.stage_selected, options.op_selected = self.stage_index, self.op_index
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'已更新Stage {self.stage_index + 1}的第'
                                      f'{self.op_index + 1}个操作；请重新CHECK')
            elif action == 'LOAD_OPERATION':
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                stage = stages[self.stage_index]
                if not 0 <= self.op_index < len(stage.get('operations', [])):
                    raise ValueError('先选择有效操作')
                _load_operation_into_form(context, options,
                                          stage['operations'][self.op_index],
                                          self.stage_index, self.op_index)
                with _quiet():
                    options.stage_selected, options.op_selected = self.stage_index, self.op_index
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, '已把所选操作载入参数表单；修改后点“更新所选操作”'
                                      '并重新CHECK')
            elif action == 'DELETE_OPERATION':
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                stage = stages[self.stage_index]
                if not 0 <= self.op_index < len(stage.get('operations', [])):
                    raise ValueError('先选择有效操作')
                removed = stage['operations'].pop(self.op_index)
                if not stage['operations']:
                    stages.pop(self.stage_index)
                _write_stages(options, stages)
                with _quiet():
                    options.stage_selected = min(self.stage_index, len(stages) - 1)
                    options.op_selected = -1
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, '已删除操作'
                                      f'（{_KIND_LABELS.get(removed.get("kind"), removed.get("kind"))}）；'
                                      '空Stage已移除；请重新CHECK')
            elif action == 'DELETE_STAGE':
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                removed = stages.pop(self.stage_index)
                _write_stages(options, stages)
                with _quiet():
                    options.stage_selected = min(self.stage_index, len(stages) - 1)
                    options.op_selected = -1
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'已删除Stage「{removed.get("label", "")}」及其'
                                      f'{len(removed.get("operations", []))}个操作；请重新CHECK')
            elif action in {'MOVE_UP', 'MOVE_DOWN'}:
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                other = self.stage_index - 1 if action == 'MOVE_UP' else self.stage_index + 1
                if not 0 <= other < len(stages):
                    raise ValueError('Stage已在边界')
                stages[self.stage_index], stages[other] = stages[other], stages[self.stage_index]
                _write_stages(options, stages)
                with _quiet():
                    options.stage_selected = other
                    options.op_selected = -1
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'Stage顺序已调整：现在位置{other + 1}；请重新CHECK')
            elif action == 'RENAME':
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                label = options.stage_label.strip()
                if not label:
                    raise ValueError('先在“Stage名称”填写新名称')
                stages[self.stage_index]['label'] = label
                _write_stages(options, stages)
                with _quiet():
                    options.stage_label = ''
                _drop_plan(cancel=True, clear_view=True)
                _set_summary(context, f'Stage {self.stage_index + 1}已重命名为“{label}”；'
                                      '请重新CHECK')
            elif action == 'SELECT_STAGE':
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                with _quiet():
                    options.stage_selected = self.stage_index
                    options.op_selected = -1
                _set_summary(context, f'已选择Stage {self.stage_index + 1}')
            elif action == 'SELECT_OPERATION':
                if not 0 <= self.stage_index < len(stages):
                    raise ValueError('先选择有效Stage')
                if not 0 <= self.op_index < len(stages[self.stage_index].get('operations', [])):
                    raise ValueError('先选择有效操作')
                with _quiet():
                    options.stage_selected = self.stage_index
                    options.op_selected = self.op_index
                _set_summary(context, f'已选择Stage {self.stage_index + 1}的'
                                      f'操作{self.op_index + 1}')
            else:
                raise ValueError('未知Stage操作')
            _redraw()
            return {'FINISHED'}
        except Exception as exc:
            _set_summary(context, 'Stage操作未完成：' + str(exc))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _normalize_stages_for_recipe(stages, region_names):
    if not isinstance(stages, list):
        raise ValueError('recipe.stages必须是列表')
    result = []
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError('Stage ' + str(index + 1) + ' 不是字典')
        label = stage.get('label', '')
        if not isinstance(label, str):
            raise ValueError('Stage ' + str(index + 1) + ' 名称不是字符串')
        operations = stage.get('operations', [])
        if not isinstance(operations, list):
            raise ValueError('Stage ' + str(index + 1) + ' operations不是列表')
        clean_operations = []
        for op_index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise ValueError('Stage ' + str(index + 1) + ' 操作' +
                                 str(op_index + 1) + ' 不是字典')
            region = operation.get('region')
            if not isinstance(region, str) or region not in region_names:
                raise ValueError('Stage ' + str(index + 1) + ' 操作' +
                                 str(op_index + 1) + ' 引用未知region：' + str(region))
            kind = operation.get('kind')
            if not isinstance(kind, str) or not kind:
                raise ValueError('操作缺少kind')
            groups = operation.get('groups')
            if not isinstance(groups, list) or not groups \
                    or any(not isinstance(name, str) or not name for name in groups):
                raise ValueError('操作groups必须为非空字符串列表')
            clean_operations.append(copy.deepcopy(operation))
        result.append({'label': label, 'operations': clean_operations})
    return _json_safe(result, 'Stage')


def _apply_loaded_recipe(context, options, recipe, *, path_label=''):
    if not isinstance(recipe, dict):
        raise ValueError('recipe必须是JSON字典')
    if recipe.get('schema') != _SCHEMA:
        raise ValueError('recipe schema不匹配：' + str(recipe.get('schema')))
    raw_regions = recipe.get('regions')
    if not isinstance(raw_regions, dict) or not raw_regions:
        raise ValueError('recipe需要非空regions')
    normalized = {}
    required_keys = []
    hints = {}
    for name, raw_region in raw_regions.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('recipe含空区域名')
        region = _normalize_region(raw_region, '区域“' + name + '”')
        normalized[name] = region
        key = region['target']
        if key not in required_keys:
            required_keys.append(key)
        binding = region.get('binding', {})
        hint = binding.get('object') if isinstance(binding, dict) else ''
        if hint and key not in hints:
            hints[key] = str(hint)
    stages = _normalize_stages_for_recipe(recipe.get('stages'), set(normalized))
    if not stages:
        raise ValueError('recipe需要至少一个Stage')

    # Validation completed before any UI state is changed.
    session = _session(context, create=True)
    _drop_plan(cancel=True, clear_view=True)
    old = {entry.key: (entry.obj, entry.hint) for entry in options.targets}
    _clear_collection(options.regions)
    for name, region in normalized.items():
        entry = options.regions.add()
        entry.region_name = name
        entry.target_key = region['target']
        entry.region_json = json.dumps(region, ensure_ascii=False, allow_nan=False)
        source = region.get('capture', {}).get('source', '') if isinstance(
            region.get('capture'), dict) else ''
        entry.source_label = str(source or 'recipe载入')
        metrics = _region_metrics(region)
        for field, value in metrics.items():
            setattr(entry, field, value)
    _write_stages(options, stages)
    _clear_collection(options.targets)
    for key in required_keys:
        entry = options.targets.add()
        entry.key = key
        previous_obj, previous_hint = old.get(key, (None, ''))
        entry.obj = previous_obj if previous_obj is not None and previous_obj.type == 'MESH' else None
        entry.hint = hints.get(key, previous_hint or '')
    _clear_pairing_previews(session)
    with _quiet():
        options.stage_selected = -1
        options.op_selected = -1
        options.merge_stage = 0
        options.stage_label = ''
        keep_target = old.get(options.target_key)
        if keep_target is not None and keep_target[0] is not None \
                and options.target_key in required_keys:
            options.target = keep_target[0]
        else:
            options.target = None
            options.target_key = ''
    session['pairings'].clear()
    session['pairing_lines'].clear()
    session['preview_names'] = []
    session['candidate_names'] = []
    session['error'] = ''
    options.report_json = ''
    if path_label:
        options.recipe_path = path_label
    _set_summary(
        context,
        f'已载入配方：{len(normalized)}个region、{len(stages)}个Stage；'
        '目标逻辑key必须显式指定对象（不会按名称自动绑定）。'
        '修改/确认后请重新CHECK')
    return options


def _require_live_plan(context, options):
    session = _session(context, create=False)
    if session is None or session.get('plan') is None:
        raise ValueError('没有当前live plan；请先CHECK')
    if session.get('scene') != context.scene:
        raise ValueError('另一个场景拥有该live plan；请切回原场景')
    if context.mode != 'OBJECT':
        raise ValueError('预览或创建候选需要对象模式；先结束编辑/姿态模式')
    plan = session['plan']
    try:
        targets = _target_map(options, context)
        rig = _rig_object(context, options)
        recipe, _ = _build_recipe(context, options, require_targets=True,
                                  require_nonempty=True)
        signature = _signature(context, options, recipe, targets, rig)
    except Exception as exc:
        _drop_plan(cancel=True, clear_view=True)
        raise ValueError('配方/目标映射已改变，旧live plan已撤销；请重新CHECK：'
                         + str(exc)) from exc
    if signature != session.get('signature'):
        _drop_plan(cancel=True, clear_view=True)
        raise ValueError('配方、目标映射、单位或高级Text已改变；旧live plan已撤销，'
                         '请重新CHECK')
    if not isinstance(plan.report, dict) or plan.report.get('status') != 'prepared':
        _drop_plan(cancel=True, clear_view=True)
        raise ValueError('live plan已结束或状态不是prepared；请重新CHECK')
    return session, plan


class RWR_OT_Plan(bpy.types.Operator):
    bl_idname = 'rigging_weight_repair.plan'
    bl_label = '权重修复检查、预览与提交'
    bl_description = 'CHECK只准备内存Plan；PREVIEW/APPLY只经同核创建副本，源保持不动'
    bl_options = {'REGISTER'}

    action: EnumProperty(items=(
        ('CHECK', '检查并准备', ''),
        ('PREVIEW', '预览范围', ''),
        ('APPLY', '创建候选', ''),
        ('CANCEL', '取消计划', ''),
        ('SAVE', '保存配方', ''),
        ('LOAD', '加载配方', ''),
        ('EXPORT_REPORT', '导出完整报告', ''),
        ('RELEASE', '结束UI会话', ''),
    ))
    filepath: StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'})
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})

    def invoke(self, context, event):
        if self.action in {'SAVE', 'LOAD', 'EXPORT_REPORT'}:
            options = _options(context)
            if not self.filepath:
                if options.recipe_path:
                    self.filepath = bpy.path.abspath(options.recipe_path)
                elif self.action in {'SAVE', 'EXPORT_REPORT'}:
                    directory = os.path.dirname(bpy.data.filepath) if bpy.data.filepath \
                        else (bpy.app.tempdir or os.getcwd())
                    self.filepath = os.path.join(directory, 'weight_repair_report.json' if self.action == 'EXPORT_REPORT'
                                                 else 'weight_repair_recipe.json')
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def _path(self, options):
        raw = self.filepath or options.recipe_path
        if not str(raw or '').strip():
            raise ValueError('先选择配方JSON文件路径')
        path = bpy.path.abspath(str(raw))
        if not path or not os.path.isabs(path):
            raise ValueError('配方路径必须解析为绝对路径')
        return os.path.normpath(path)

    def _check(self, context, options):
        if context.mode != 'OBJECT':
            raise ValueError('CHECK需要对象模式；先结束编辑/姿态模式（区域捕获可先完成）')
        _drop_plan(cancel=True, clear_view=True)
        session = _session(context, create=True)
        session['error'] = ''
        targets = _target_map(options, context)
        rig = _rig_object(context, options)
        recipe, _ = _build_recipe(context, options, require_targets=True,
                                  require_nonempty=True)
        try:
            preflight = _preflight_lines(context, options, recipe, targets, rig)
        except Exception as exc:
            preflight = ['UI只读预检失败：' + str(exc)]
        signature = _signature(context, options, recipe, targets, rig)
        session['preflight'] = preflight
        session['report_lines'] = []
        session['error'] = ''
        try:
            plan = weight_repair.prepare(targets, rig, recipe)
        except Exception as exc:
            session['preflight'] = preflight
            session['error'] = 'CHECK核心拒绝：' + str(exc)
            session['report_lines'] = ['CHECK核心拒绝：' + str(exc)]
            options.report_json = ''
            raise
        if not isinstance(plan.report, dict):
            raise ValueError('核心prepare没有返回明确report')
        session['plan'] = plan
        session['recipe'] = copy.deepcopy(recipe)
        session['targets'] = dict(targets)
        session['rig'] = rig
        session['signature'] = signature
        session['report'] = plan.report
        session['report_lines'] = _safe_report_lines(plan.report)
        session['preflight'] = preflight
        options.report_json = _report_json_text(plan.report)
        operations = sum(len(stage.get('operations', []))
                         for stage in recipe['stages'])
        zero_notes = sum(1 for line in preflight if '零质量' in line and '0/' not in line)
        lock_notes = sum(1 for line in preflight if '锁定：' in line
                         and '锁定：无' not in line)
        _set_summary(
            context,
            f'CHECK完成：{len(targets)}个写入目标、{len(recipe["stages"])}个Stage、'
            f'{operations}个操作；live plan已准备但尚未创建候选。'
            f'UI预检：锁组提示{lock_notes}项，零质量提示{zero_notes}项；'
            '实际锁/consumer/质量预算以核心报告为准')
        self.report({'INFO'}, 'CHECK完成，live plan已准备')

    def _preview(self, context, options):
        session, plan = _require_live_plan(context, options)
        prefix = options.candidate_prefix.strip() or '局部权重候选'
        previews = plan.preview(name_prefix=prefix + '_区域预览')
        if not isinstance(previews, (list, tuple)):
            raise ValueError('核心preview没有返回副本列表')
        session['preview_names'].extend(obj.name for obj in previews)
        _select_objects(context, list(previews))
        _set_summary(context, f'已创建{len(previews)}个独立区域预览副本；'
                              '源权重/骨架/动画未改；预览不是提交权')

    def _apply(self, context, options):
        session, plan = _require_live_plan(context, options)
        prefix = options.candidate_prefix.strip() or '局部权重候选'
        try:
            outputs = plan.apply(name_prefix=prefix)
        except Exception as exc:
            try:
                plan.cancel()
            except Exception as cleanup_error:
                print('[权重修复UI] APPLY失败后清理显示副本异常：' + str(cleanup_error))
            session['report'] = plan.report
            session['report_lines'] = _safe_report_lines(plan.report)
            session['plan'] = None
            session['signature'] = None
            session['preview_names'] = []
            session['error'] = 'APPLY失败：' + str(exc)
            raise
        if not isinstance(outputs, (list, tuple)) or not outputs:
            raise ValueError('核心apply没有返回候选对象')
        session['candidate_names'] = [obj.name for obj in outputs]
        session['report'] = plan.report
        session['report_lines'] = _safe_report_lines(plan.report)
        session['plan'] = None
        session['signature'] = None
        session['preview_names'] = []
        session['error'] = ''
        options.report_json = _report_json_text(plan.report)
        _select_objects(context, list(outputs))
        _set_summary(
            context,
            f'APPLY完成：已创建{len(outputs)}个独立候选（'
            + '、'.join(obj.name for obj in outputs)
            + '）；原源未改。请在同姿态下复核实际皮肤与差值，'
              '技术通过不等于美术通过')

    def _cancel(self, context, options):
        session = _session(context, create=False)
        if session is None or session.get('plan') is None:
            raise ValueError('当前没有可取消的live plan')
        plan = session['plan']
        report = plan.report
        try:
            plan.cancel()
        finally:
            session['plan'] = None
            session['signature'] = None
            session['report'] = report
            session['report_lines'] = _safe_report_lines(report)
            session['preview_names'] = []
            session['error'] = ''
            options.report_json = _report_json_text(report)
        _set_summary(context, '计划已取消；本次范围预览已清理，已创建候选保留，'
                              '源未改')

    def _save(self, context, options):
        path = self._path(options)
        recipe, _ = _build_recipe(context, options, require_targets=False,
                                  require_nonempty=True)
        if os.path.exists(path):
            raise ValueError('目标文件已存在；核心save_recipe不覆盖。请选择新路径')
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            raise ValueError('目标目录不存在：' + directory)
        _drop_plan(cancel=True, clear_view=True)
        try:
            saved = weight_repair.save_recipe(recipe, path)
        except Exception as exc:
            raise ValueError('live plan已结束；源未改；写入失败：' + str(exc)) from exc
        with _quiet():
            options.recipe_path = str(saved or path)
        session = _session(context, create=True)
        session['error'] = ''
        _set_summary(context, f'配方已保存：{saved or path}；live plan已结束，'
                              '重开不恢复提交权，必须重新捕获/映射并CHECK')
        self.report({'INFO'}, '配方已保存')

    def _load(self, context, options):
        path = self._path(options)
        if not os.path.isfile(path):
            raise ValueError('配方文件不存在：' + path)
        recipe = weight_repair.load_recipe(path)
        _apply_loaded_recipe(context, options, recipe, path_label=path)
        self.report({'INFO'}, '配方已加载')

    def _release_ui(self, context, options):
        session = _SESSION
        if session is None:
            raise ValueError('当前没有UI会话')
        _release_session('UI会话已结束；配方/区域/Stage保留，配对与owned预览清空，'
                         '已创建候选保留', force=True)
        _set_summary(context, 'UI会话已结束；配方/区域/Stage保留，配对与owned预览清空，'
                              '已创建候选保留；再次CHECK会新建当前Scene会话')

    def execute(self, context):
        options = _options(context)
        try:
            if self.action == 'CHECK':
                self._check(context, options)
            elif self.action == 'PREVIEW':
                self._preview(context, options)
            elif self.action == 'APPLY':
                self._apply(context, options)
            elif self.action == 'CANCEL':
                self._cancel(context, options)
            elif self.action == 'SAVE':
                self._save(context, options)
            elif self.action == 'LOAD':
                self._load(context, options)
            elif self.action == 'EXPORT_REPORT':
                if not options.report_json:
                    raise ValueError('先检查配方或创建候选，再导出完整报告')
                report = _json_loads_strict(options.report_json, '完整报告')
                with open(self._path(options), 'x', encoding='utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                _set_summary(context, '完整报告已导出')
            elif self.action == 'RELEASE':
                self._release_ui(context, options)
            else:
                raise ValueError('未知计划操作')
            _redraw()
            return {'FINISHED'}
        except Exception as exc:
            _set_summary(context, {
                'CHECK': 'CHECK未完成：',
                'PREVIEW': 'PREVIEW未完成：',
                'APPLY': 'APPLY未完成：',
                'CANCEL': 'CANCEL未完成：',
                'SAVE': '保存配方未完成：',
                'LOAD': '加载配方未完成：',
                'RELEASE': '结束UI会话未完成：',
            }.get(self.action, '操作未完成：') + str(exc))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class RWR_OT_Advanced(bpy.types.Operator):
    bl_idname = 'rigging_weight_repair.advanced_text'
    bl_label = '高级配方JSON Text'
    bl_description = '仅用于复杂逐点数组和整配方审阅；常规region/Stage操作不依赖JSON'
    bl_options = {'REGISTER'}

    action: EnumProperty(items=(
        ('IMPORT_TEXT', '从Text导入配方', ''),
        ('EXPORT_TEXT', '导出配方到Text', ''),
    ))

    def execute(self, context):
        options = _options(context)
        try:
            if self.action == 'IMPORT_TEXT':
                text = options.advanced_text
                if text is None:
                    raise ValueError('先选择高级JSON Text')
                content = text.as_string()
                if not content.strip():
                    raise ValueError('高级Text为空')
                recipe = _json_loads_strict(content, '高级Text配方')
                _apply_loaded_recipe(context, options, recipe)
                _set_summary(context, '已从高级Text导入配方；目标逻辑key需显式映射对象，'
                                      '不会自动按名称绑定。修改/确认后请重新CHECK')
            elif self.action == 'EXPORT_TEXT':
                recipe, _ = _build_recipe(context, options, require_targets=False,
                                          require_nonempty=True)
                text = options.advanced_text
                if text is None:
                    name, index = 'weight_repair_recipe_高级', 2
                    while name in bpy.data.texts:
                        name = 'weight_repair_recipe_高级_' + str(index)
                        index += 1
                    text = bpy.data.texts.new(name)
                    with _quiet():
                        options.advanced_text = text
                text.clear()
                text.write(json.dumps(recipe, ensure_ascii=False, indent=2,
                                     allow_nan=False))
                _set_summary(context, f'配方已导出到Text「{text.name}」；'
                                      'Text仅高级编辑入口，导入后仍须显式目标映射并CHECK')
            else:
                raise ValueError('未知高级Text操作')
            _redraw()
            return {'FINISHED'}
        except Exception as exc:
            _set_summary(context, '高级Text操作未完成：' + str(exc))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _lines(layout, text, width=38):
    region = getattr(bpy.context, 'region', None)
    if region is not None and getattr(region, 'type', None) == 'UI':
        try:
            scale = bpy.context.preferences.system.ui_scale
            width = min(width, max(12, int((region.width - 48) / (15 * scale))))
        except Exception:
            pass
    for paragraph in str(text).splitlines():
        if not paragraph:
            layout.label(text='')
            continue
        for line in textwrap.wrap(paragraph, width=width, break_long_words=True,
                                  break_on_hyphens=False):
            layout.label(text=line)


def _button(row, idname, text, enabled=True, icon=None, **properties):
    target = row if enabled else row.column()
    if not enabled:
        target.enabled = False
    operator = target.operator(idname, text=text, icon=icon) if icon \
        else target.operator(idname, text=text)
    for key, value in properties.items():
        setattr(operator, key, value)
    return operator


def _draw_targets(layout, options):
    box = layout.box()
    box.label(text='目标与骨架（逻辑key必须显式）', icon='OUTLINER_OB_MESH')
    box.prop(options, 'target')
    row = box.row(align=True)
    row.prop(options, 'target_key')
    _button(row, 'rigging_weight_repair.target', '用对象名填入key', action='MAP_NAME')
    box.prop(options, 'rig')
    _button(box, 'rigging_weight_repair.target', '登记/更新目标逻辑key', action='REGISTER')
    if not len(options.targets):
        box.label(text='尚未登记写入目标；CHECK会拒绝未映射key', icon='INFO')
    for index, entry in enumerate(options.targets):
        row = box.row(align=True)
        name = entry.obj.name if entry.obj is not None else '未指定对象'
        row.label(text=f'{index + 1}. {entry.key} → {name}')
        _button(row, 'rigging_weight_repair.target', '当前', action='SELECT', index=index)
        _button(row, 'rigging_weight_repair.target', '删除', action='REMOVE', index=index)
        if entry.obj is None and entry.hint:
            _lines(box, f'配方绑定对象名提示：{entry.hint}（不会自动绑定）', width=34)


def _draw_regions(layout, options):
    box = layout.box()
    box.label(text='区域库（捕获后保存RAW binding）', icon='MESH_DATA')
    box.prop(options, 'region_label')
    box.prop(options, 'capture_source')
    if options.capture_source == 'MASK':
        if options.target is not None:
            box.prop_search(options, 'mask_group', options.target, 'vertex_groups')
        else:
            box.prop(options, 'mask_group')
        box.label(text='遮罩组取全部正权重点；不叠加可见选点', icon='INFO')
    else:
        box.label(text='使用目标的当前可见RAW选点；可在Edit模式先选点', icon='INFO')
    box.prop(options, 'rings')
    box.prop(options, 'barriers')
    box.prop(options, 'allowed_ids_json')
    _button(box, 'rigging_weight_repair.region', '捕获并保存区域binding',
            action='CAPTURE', icon='ADD')
    if not len(options.regions):
        box.label(text='尚未捕获region', icon='INFO')
    for index, entry in enumerate(options.regions):
        row = box.row(align=True)
        row.label(text=f'{index + 1}. {entry.region_name} · {entry.target_key} · '
                       f'写{entry.write_count}/{entry.id_count} · 读{entry.read_count} · '
                       f'屏障{entry.barrier_count}')
        _button(row, 'rigging_weight_repair.region', '写域',
                action='PREVIEW_WRITE', index=index)
        _button(row, 'rigging_weight_repair.region', '读域',
                action='PREVIEW_READ', index=index)
        _button(row, 'rigging_weight_repair.region', '用于操作',
                action='USE_IN_OPERATION', index=index)
        _button(row, 'rigging_weight_repair.region', '删除',
                action='DELETE', index=index)
        pair_row = box.row(align=True)
        pair_row.label(text='    镜像点域：')
        _button(pair_row, 'rigging_weight_repair.region', '设为左域',
                action='SET_LEFT_PAIRING', index=index)
        _button(pair_row, 'rigging_weight_repair.region', '设为右域',
                action='SET_RIGHT_PAIRING', index=index)
        _button(pair_row, 'rigging_weight_repair.region', '设为中心',
                action='SET_CENTER_PAIRING', index=index)
        if entry.source_label:
            _lines(box, '   来源：' + entry.source_label, width=34)
    _button(box, 'rigging_weight_repair.region', '同输入显式重绑定全部区域',
            action='REBIND')
    _lines(box, '写域/读域按钮只改变选择状态；改权重只能通过CHECK→PREVIEW/APPLY。',
           width=34)


def _draw_pairings(layout, options, session):
    box = layout.box()
    box.label(text='镜像配对（BUILD→审阅→CONFIRM）', icon='MOD_MIRROR')
    box.prop(options, 'pairing_name')
    box.prop(options, 'pairing_left_region')
    box.prop(options, 'pairing_right_region')
    box.prop(options, 'pairing_center_region')
    row = box.row(align=True)
    row.prop(options, 'pairing_axis')
    row.prop(options, 'pairing_plane')
    box.prop(options, 'pairing_tolerance')
    row = box.row(align=True)
    _button(row, 'rigging_weight_repair.pairing', '构建配对', action='BUILD')
    _button(row, 'rigging_weight_repair.pairing', '预览线段', action='PREVIEW')
    row = box.row(align=True)
    _button(row, 'rigging_weight_repair.pairing', '确认当前配对', action='CONFIRM')
    _button(row, 'rigging_weight_repair.pairing', '删除会话配对', action='DELETE')
    if session is None or not session.get('pairings'):
        box.label(text='本Scene会话尚无配对；未确认配对不能加入MIRROR', icon='INFO')
        return
    names = sorted(session['pairings'])
    for index, name in enumerate(names):
        entry = session['pairings'][name]
        pairing = entry.get('pairing', {})
        row = box.row(align=True)
        status = '已确认' if pairing.get('confirmed') else '未确认'
        row.label(text=f'{index + 1}. {name} · {status} · '
                       f'{_count_list(pairing.get("pairs", []))}对 · '
                       f'未配{_count_list(pairing.get("unmatched", []))} · '
                       f'歧义{_count_list(pairing.get("ambiguous", []))}')
        _button(row, 'rigging_weight_repair.pairing', '设为当前',
                action='SELECT', index=index)
    current = options.pairing_name.strip()
    if current in session.get('pairing_lines', {}):
        for line in session['pairing_lines'][current]:
            _lines(box, line, width=34)
    else:
        box.label(text='当前配对名没有审阅报告；BUILD或CONFIRM后显示', icon='INFO')


def _draw_operation(layout, options):
    box = layout.box()
    box.label(text='操作编辑器（常用scalar不写JSON）', icon='TOOL_SETTINGS')
    box.prop(options, 'op_region')
    box.prop(options, 'op_kind')
    kind = options.op_kind
    if kind != 'TRANSFER_AMOUNT':
        box.prop(options, 'op_groups')
    if kind == 'ASSIGN':
        box.prop(options, 'assign_values')
        box.prop(options, 'assign_remainder')
        box.label(text='values与remainder必须互斥且并集等于groups', icon='INFO')
        box.prop(options, 'assign_values_json')
        box.label(text='高级values JSON仅用于逐点数组；填了就优先于标量行', icon='INFO')
    elif kind == 'TRANSFER_AMOUNT':
        box.prop(options, 'transfer_source')
        box.prop(options, 'transfer_receiver')
        box.prop(options, 'transfer_amount')
        box.prop(options, 'transfer_mode')
        box.prop(options, 'transfer_amount_json')
        box.label(text='groups自动为[源组,接收组]；超源预算由核心拒绝，不clamp', icon='INFO')
    elif kind == 'REBALANCE':
        box.prop(options, 'rebalance_factors')
        box.label(text='倍率必须覆盖全部groups且非负', icon='INFO')
    elif kind == 'SMOOTH':
        box.prop(options, 'smooth_iterations')
        box.label(text='读取region的read_ids与屏障边；同步更新，不自动扩域', icon='INFO')
    elif kind == 'SURFACE_TRANSFER':
        box.prop(options, 'surface_donor')
        box.prop(options, 'surface_max_distance')
        box.prop(options, 'surface_bone_mapping')
        box.prop(options, 'surface_donor_ids_json')
        box.label(text='供体不能是写入target；映射须覆盖正权供体列与全部写组', icon='INFO')
    elif kind == 'MIRROR':
        box.prop(options, 'mirror_pairing_name')
        box.prop(options, 'mirror_mode')
        box.prop(options, 'mirror_group_mapping')
        box.label(text='COPY固定左→右，写域必须是右侧配对点；AVERAGE映射须对合', icon='INFO')
    elif kind == 'FIELD':
        box.prop(options, 'field_json')
        box.label(text='FIELD仅高级逐点数组入口；常规操作请用上面五种GUI', icon='INFO')
    box.prop(options, 'merge_stage')
    box.prop(options, 'stage_label')
    _button(box, 'rigging_weight_repair.stage', '加入阶段（默认新建）',
            action='ADD_OPERATION', icon='ADD')
    _button(box, 'rigging_weight_repair.stage', '更新所选操作',
            action='UPDATE_OPERATION',
            stage_index=options.stage_selected, op_index=options.op_selected)
    _lines(box, '可填1..N把操作合并到已有Stage；同Stage写域冲突不在UI重排，'
                '交给同核prepare拒绝。', width=34)


def _draw_stages(layout, options, stages, error):
    box = layout.box()
    box.label(text='阶段与操作顺序', icon='SEQUENCE')
    if error:
        alert = box.box()
        alert.alert = True
        _lines(alert, 'Stage配方JSON无效：' + error, width=34)
        return
    if not stages:
        box.label(text='尚无阶段；请先加入操作', icon='INFO')
        return
    for stage_index, stage in enumerate(stages):
        label = stage.get('label', '') or '(未命名)'
        operations = stage.get('operations', [])
        row = box.row(align=True)
        selected = '▶ ' if stage_index == options.stage_selected else ''
        row.label(text=f'{selected}阶段 {stage_index + 1} · {label} · {len(operations)}操作')
        _button(row, 'rigging_weight_repair.stage', '选择', action='SELECT_STAGE',
                stage_index=stage_index)
        _button(row, 'rigging_weight_repair.stage', '上移', action='MOVE_UP',
                stage_index=stage_index)
        _button(row, 'rigging_weight_repair.stage', '下移', action='MOVE_DOWN',
                stage_index=stage_index)
        row = box.row(align=True)
        _button(row, 'rigging_weight_repair.stage', '用上方名称重命名', action='RENAME',
                stage_index=stage_index)
        _button(row, 'rigging_weight_repair.stage', '删除阶段', action='DELETE_STAGE',
                stage_index=stage_index)
        for op_index, operation in enumerate(operations):
            kind = operation.get('kind', '?')
            region = operation.get('region', '?')
            groups = operation.get('groups', [])
            row = box.row(align=True)
            selected = '  ▶ ' if (stage_index == options.stage_selected
                                    and op_index == options.op_selected) else '    '
            row.label(text=f'{selected}{op_index + 1}. {_KIND_LABELS.get(kind, kind)} · '
                           f'{region} · 组{len(groups)}')
            _button(row, 'rigging_weight_repair.stage', '选择', action='SELECT_OPERATION',
                    stage_index=stage_index, op_index=op_index)
            _button(row, 'rigging_weight_repair.stage', '载入', action='LOAD_OPERATION',
                    stage_index=stage_index, op_index=op_index)
            _button(row, 'rigging_weight_repair.stage', '删除', action='DELETE_OPERATION',
                    stage_index=stage_index, op_index=op_index)
        box.separator()


def _draw_plan(layout, options, session):
    box = layout.box()
    box.label(text='计划与配方文件', icon='PLAY')
    box.prop(options, 'candidate_prefix')
    has_plan = bool(session is not None and session.get('plan') is not None)
    live = bool(has_plan and session.get('report', {}).get('status') == 'prepared')
    row = box.row(align=True)
    _button(row, 'rigging_weight_repair.plan', '检查并准备', action='CHECK')
    _button(row, 'rigging_weight_repair.plan', '预览范围', action='PREVIEW',
            enabled=live)
    row = box.row(align=True)
    _button(row, 'rigging_weight_repair.plan', '创建候选', action='APPLY',
            enabled=live)
    _button(row, 'rigging_weight_repair.plan', '取消计划', action='CANCEL',
            enabled=has_plan)
    if has_plan:
        status = session.get('report', {}).get('status', 'prepared')
        box.label(text='当前计划：' + _STATUS_LABELS.get(status, str(status)),
                  icon='CHECKMARK')
    else:
        box.label(text='尚无计划；编辑后请重新检查', icon='INFO')
    if session and session.get('preview_names'):
        _lines(box, '已创建预览：' + '、'.join(session['preview_names']), width=34)
    if session and session.get('candidate_names'):
        _lines(box, '已创建候选：' + '、'.join(session['candidate_names']), width=34)
    box.prop(options, 'recipe_path')
    row = box.row(align=True)
    _button(row, 'rigging_weight_repair.plan', '保存配方', action='SAVE')
    _button(row, 'rigging_weight_repair.plan', '加载配方', action='LOAD')
    _button(box, 'rigging_weight_repair.plan', '导出完整报告', action='EXPORT_REPORT',
            enabled=bool(options.report_json))
    _button(box, 'rigging_weight_repair.plan', '结束本Scene UI会话（保留已创建副本）',
            action='RELEASE', enabled=session is not None)
    _lines(box, '保存配方会结束当前计划。加载后请确认目标映射，再重新检查。',
           width=34)


def _draw_reports(layout, session, scene):
    box = layout.box()
    box.label(text='范围、锁与零质量（UI只读预检）', icon='VIEWZOOM')
    if session is not None and session.get('scene') == scene:
        lines = session.get('preflight') or []
        if lines:
            for line in lines:
                _lines(box, line, width=36)
        else:
            box.label(text='尚未CHECK', icon='INFO')
    else:
        box.label(text='尚未CHECK', icon='INFO')
    report = layout.box()
    report.label(text='CHECK报告：质量残差/预算、增删与差值', icon='TEXT')
    if session is not None and session.get('scene') == scene:
        lines = session.get('report_lines') or []
        if lines:
            for line in lines:
                _lines(report, line, width=36)
        else:
            report.label(text='尚未CHECK或报告已因参数改变撤销', icon='INFO')
        if session.get('error'):
            alert = report.box()
            alert.alert = True
            _lines(alert, session['error'], width=34)
    else:
        report.label(text='尚未CHECK', icon='INFO')
    _lines(report, '报告区分机械求值事实与视觉判断；技术通过不等于美术通过。',
           width=36)


class RWR_PT_WeightRepair(bpy.types.Panel):
    bl_idname = 'RWR_PT_weight_repair'
    bl_label = '局部权重修复套组'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色工作台'
    bl_options = {'DEFAULT_CLOSED'}
    workflow_stage = 'BIND'
    workflow_title = '09 · 局部权重修复套组'

    def draw(self, context):
        layout = self.layout
        try:
            options = _options(context)
        except Exception as exc:
            layout.label(text='权重修复选项未注册：' + str(exc), icon='ERROR')
            return
        session = _SESSION
        foreign = session is not None and session.get('scene') != context.scene
        if foreign:
            alert = layout.box()
            alert.alert = True
            _lines(alert, '另一个场景拥有权重修复UI会话；请切回原场景或先结束该会话。',
                   width=34)
            alert.operator('rigging_weight_repair.plan', text='结束旧的修复会话').action = 'RELEASE'
            return
        top = layout.box()
        _lines(top, options.summary, width=36)
        if context.mode != 'OBJECT':
            _lines(top, '当前不是对象模式：区域捕获可先完成；CHECK/PREVIEW/APPLY需要对象模式。',
                   width=34)
        _draw_targets(layout, options)
        _draw_regions(layout, options)
        _draw_pairings(layout, options, session)
        _draw_operation(layout, options)
        try:
            stages = _read_stages(options)
            stage_error = ''
        except Exception as exc:
            stages, stage_error = [], str(exc)
        _draw_stages(layout, options, stages, stage_error)
        advanced = layout.box()
        advanced.label(text='高级配方编辑', icon='TEXT')
        advanced.prop(options, 'advanced_text')
        row = advanced.row(align=True)
        _button(row, 'rigging_weight_repair.advanced_text', '从Text导入配方',
                action='IMPORT_TEXT')
        _button(row, 'rigging_weight_repair.advanced_text', '导出配方到Text',
                action='EXPORT_TEXT')
        _lines(advanced, '复杂逐点数值可通过配方导入。', width=34)
        _draw_plan(layout, options, session)
        _draw_reports(layout, session, context.scene)
        footer = layout.column()
        _lines(footer, '只读取源并创建独立候选；锁组、零质量、consumer、过时Plan由同核拒绝。'
                       '原源、骨架、Action与其它属性保持不动。', width=36)
        _lines(footer, '登记：Scene ' + context.scene.name + '；UI会话按Scene唯一。', width=36)


_CLASSES = (
    RWR_TargetEntry,
    RWR_RegionEntry,
    RWR_Options,
    RWR_OT_Target,
    RWR_OT_Region,
    RWR_OT_Pairing,
    RWR_OT_Stage,
    RWR_OT_Plan,
    RWR_OT_Advanced,
    RWR_PT_WeightRepair,
)


def _release_workspace_options():
    global _OPTIONS_OWNED
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if _OPTIONS_OWNED and prop is not None and prop.type == 'POINTER' \
            and prop.fixed_type == RWR_Options.bl_rna:
        delattr(bpy.types.Scene, OPTIONS)
    _OPTIONS_OWNED = False


def register():
    global _OPTIONS_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('权重修复面板属性已由其他实例持有；原属性保持')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=RWR_Options))
        _OPTIONS_OWNED = True
        if _load_pre not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(_load_pre)
        if _save_pre not in bpy.app.handlers.save_pre:
            bpy.app.handlers.save_pre.append(_save_pre)
    except Exception:
        unregister()
        raise


def unregister():
    global _SESSION, _OPTIONS_OWNED
    try:
        try:
            _release_session('插件卸载：UI会话与提交权结束；已创建副本保留', force=True)
        except Exception as exc:
            print('[权重修复UI] 卸载会话清理异常：' + str(exc))
        if _load_pre in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.remove(_load_pre)
        if _save_pre in bpy.app.handlers.save_pre:
            bpy.app.handlers.save_pre.remove(_save_pre)
        _release_workspace_options()
        for cls in reversed(tuple(_REGISTERED)):
            if getattr(cls, 'is_registered', False):
                bpy.utils.unregister_class(cls)
        _REGISTERED.clear()
    finally:
        _SESSION = None
        try:
            weight_repair.release()
        except AttributeError as exc:
            print('[权重修复UI] weight_repair.release不可用：' + str(exc))
        except Exception as exc:
            print('[权重修复UI] weight_repair.release异常：' + str(exc))

