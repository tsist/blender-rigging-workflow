# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure numerical core for the rigging weight-repair suite.

This module deliberately has no Blender dependency: it consumes frozen numeric
arrays and returns a float32 payload plus a JSON-serialisable report.  The bpy
layer remains responsible for object/mesh identity, locks, consumers, source
snapshots and the actual write transaction.

FIELD accepts additional non-consumed audit keys (for example donor,
bone_mapping, transfer_receipt, pairing, group_mapping or mode); those keys are
ignored by the algorithm and their names are listed in the receipt.  Region
metadata such as target/binding/capture is likewise ignored by this numeric
layer.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from itertools import product

import numpy as np

__all__ = ["compile_weights", "grow_region", "mirror_pairs", "WeightMathError"]


_EPS64 = 2.0 ** -52
_KINDS = frozenset(
    {"ASSIGN", "TRANSFER_AMOUNT", "REBALANCE", "SMOOTH", "FIELD"}
)
_COMMON_OPERATION_KEYS = frozenset({"region", "kind"})
_OPERATION_KEYS = {
    "ASSIGN": _COMMON_OPERATION_KEYS | {"groups", "values", "remainder"},
    "TRANSFER_AMOUNT": _COMMON_OPERATION_KEYS | {"groups", "amount", "mode"},
    "REBALANCE": _COMMON_OPERATION_KEYS | {"groups", "factors"},
    "SMOOTH": _COMMON_OPERATION_KEYS | {"iterations", "groups"},
    "FIELD": _COMMON_OPERATION_KEYS | {"groups", "field"},
}
_CONSUMED_OPERATION_FIELDS = {
    "ASSIGN": ("region", "kind", "groups", "values", "remainder"),
    "TRANSFER_AMOUNT": ("region", "kind", "groups", "amount", "mode"),
    "REBALANCE": ("region", "kind", "groups", "factors"),
    "SMOOTH": ("region", "kind", "iterations"),
    "FIELD": ("region", "kind", "groups", "field"),
}


class WeightMathError(ValueError):
    """Raised when the frozen numeric contract is violated."""


def _fail(message):
    raise WeightMathError(message)


def _is_int_scalar(value):
    return isinstance(value, (int, np.integer)) and not isinstance(
        value, (bool, np.bool_)
    )


def _is_real_scalar(value):
    return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(
        value, (bool, np.bool_)
    )


def _strict_int(value, label, *, lo=None, hi=None):
    if not _is_int_scalar(value):
        _fail(f"{label} 必须是严格整数，不能是 bool/float")
    result = int(value)
    if lo is not None and result < lo:
        _fail(f"{label} 必须 >= {lo}")
    if hi is not None and result >= hi:
        _fail(f"{label} 必须 < {hi}")
    return result


def _real_scalar(value, label, *, lo=None, hi=None):
    if not _is_real_scalar(value):
        _fail(f"{label} 必须是有限实数标量")
    result = float(value)
    if not math.isfinite(result):
        _fail(f"{label} 必须有限")
    if lo is not None and result < lo:
        _fail(f"{label} 必须 >= {lo}")
    if hi is not None and result > hi:
        _fail(f"{label} 必须 <= {hi}")
    return result


def _as_sequence(value, label):
    if not isinstance(value, (list, tuple, np.ndarray)):
        _fail(f"{label} 必须是列表/元组/一维数组")
    if isinstance(value, np.ndarray) and value.ndim != 1:
        _fail(f"{label} 必须是一维")
    if isinstance(value, (str, bytes)):
        _fail(f"{label} 必须是序列，不能是字符串")
    return value


def _strict_id_array(value, limit, label, *, allow_empty=False):
    if not isinstance(value, (list, tuple, np.ndarray)):
        _fail(f"{label} 必须是整数 ID 序列")
    if isinstance(value, np.ndarray):
        if value.ndim != 1:
            _fail(f"{label} 必须是一维")
        if value.dtype.kind not in "iu":
            _fail(f"{label} 必须使用整型 dtype，不能是 bool/float/object")
        if value.size == 0:
            if not allow_empty:
                _fail(f"{label} 不能为空")
            return np.empty(0, dtype=np.int64)
        if (value < 0).any() or (value >= limit).any():
            _fail(f"{label} 含越界 ID（要求 0 <= id < {limit}）")
        result = value.astype(np.int64, copy=False)
    else:
        items = list(value)
        if not items and not allow_empty:
            _fail(f"{label} 不能为空")
        result = np.empty(len(items), dtype=np.int64)
        for index, item in enumerate(items):
            if not _is_int_scalar(item):
                _fail(f"{label}[{index}] 必须是严格整数，不能是 bool/float")
            item_int = int(item)
            if item_int < 0 or item_int >= limit:
                _fail(f"{label}[{index}] 越界（要求 0 <= id < {limit}）")
            result[index] = item_int
    if len(result) != len(np.unique(result)):
        _fail(f"{label} 含重复 ID")
    return result


def _strict_edge_array(value, vertex_count, label):
    if isinstance(value, np.ndarray):
        if value.ndim != 2 or value.shape[1] != 2:
            _fail(f"{label} 必须是 P×2 边数组")
        if value.dtype.kind not in "iu":
            _fail(f"{label} 必须使用整型 dtype，不能是 bool/float/object")
        if value.size and ((value < 0).any() or (value >= vertex_count).any()):
            _fail(f"{label} 含越界顶点 ID")
        result = value.astype(np.int64, copy=False)
    else:
        if not isinstance(value, (list, tuple)):
            _fail(f"{label} 必须是 P×2 边序列")
        rows = list(value)
        result = np.empty((len(rows), 2), dtype=np.int64)
        for row_index, row in enumerate(rows):
            if not isinstance(row, (list, tuple, np.ndarray)) or len(row) != 2:
                _fail(f"{label}[{row_index}] 必须是两个严格整数")
            for column, item in enumerate(row):
                if not _is_int_scalar(item):
                    _fail(f"{label}[{row_index}][{column}] 必须是严格整数")
                item_int = int(item)
                if item_int < 0 or item_int >= vertex_count:
                    _fail(f"{label}[{row_index}][{column}] 越界")
                result[row_index, column] = item_int
    if result.size and (result[:, 0] == result[:, 1]).any():
        _fail(f"{label} 不能包含自环边")
    return result


def _string_list(value, label, *, nonempty=True, unique=True):
    if isinstance(value, np.ndarray):
        if value.ndim != 1:
            _fail(f"{label} 必须是一维字符串列表")
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        _fail(f"{label} 必须是字符串列表")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            _fail(f"{label}[{index}] 必须是非空字符串")
        result.append(str(item))
    if nonempty and not result:
        _fail(f"{label} 不能为空")
    if unique and len(result) != len(set(result)):
        _fail(f"{label} 含重复名称")
    return result


def _real_vector(value, expected, label, *, lo=None, hi=None):
    if not isinstance(value, (list, tuple, np.ndarray)):
        _fail(f"{label} 必须是一维实数数组")
    array = np.asarray(value)
    if array.ndim != 1:
        _fail(f"{label} 必须是一维")
    if array.dtype.kind == "b" or array.dtype.kind not in "iuf":
        _fail(f"{label} 必须使用有限实数 dtype")
    if array.shape != (expected,):
        _fail(f"{label} 形状必须是 ({expected},)，实际 {array.shape}")
    result = array.astype(np.float64, copy=False)
    if not np.isfinite(result).all():
        _fail(f"{label} 含非有限值")
    if lo is not None and (result < lo).any():
        _fail(f"{label} 含小于 {lo} 的值")
    if hi is not None and (result > hi).any():
        _fail(f"{label} 含大于 {hi} 的值")
    return result


def _real_matrix(value, shape, label, *, lo=None, hi=None):
    if not isinstance(value, (list, tuple, np.ndarray)):
        _fail(f"{label} 必须是二维实数数组")
    array = np.asarray(value)
    if array.ndim != 2:
        _fail(f"{label} 必须是二维")
    if array.dtype.kind == "b" or array.dtype.kind not in "iuf":
        _fail(f"{label} 必须使用有限实数 dtype")
    if array.shape != shape:
        _fail(f"{label} 形状必须是 {shape}，实际 {array.shape}")
    result = array.astype(np.float64, copy=False)
    if not np.isfinite(result).all():
        _fail(f"{label} 含非有限值")
    if lo is not None and (result < lo).any():
        _fail(f"{label} 含小于 {lo} 的值")
    if hi is not None and (result > hi).any():
        _fail(f"{label} 含大于 {hi} 的值")
    return result


def _seq_sum_rows(array):
    """Sum columns in fixed E order (no pairwise/vector reduction)."""
    if array.shape[1] == 0:
        return np.zeros(array.shape[0], dtype=np.float64)
    total = array[:, 0].astype(np.float64, copy=True)
    for column in range(1, array.shape[1]):
        total += array[:, column]
    return total


def _mass(field):
    return _seq_sum_rows(np.asarray(field, dtype=np.float64))


def _r64(group_count, mass):
    return 32.0 * _EPS64 * float(group_count) * np.maximum(1.0, mass)


def _half_ulp32_scalar(value):
    value32 = np.float32(value)
    upper = np.nextafter(value32, np.float32(np.inf), dtype=np.float32)
    lower = np.nextafter(value32, np.float32(-np.inf), dtype=np.float32)
    value64 = float(value32)
    return 0.5 * max(float(upper) - value64, value64 - float(lower))


def _half_ulp32_rows(values32):
    values32 = np.asarray(values32, dtype=np.float32)
    values64 = values32.astype(np.float64)
    upper = np.nextafter(values32, np.float32(np.inf), dtype=np.float32)
    lower = np.nextafter(values32, np.float32(-np.inf), dtype=np.float32)
    return 0.5 * np.maximum(
        upper.astype(np.float64) - values64,
        values64 - lower.astype(np.float64),
    )


def _sum_half_ulp_rows(values32):
    half = _half_ulp32_rows(values32)
    if half.shape[1] == 0:
        return np.zeros(half.shape[0], dtype=np.float64)
    total = half[:, 0].copy()
    for column in range(1, half.shape[1]):
        total += half[:, column]
    return total


def _quality_arrays(values32, mass):
    values32 = np.asarray(values32, dtype=np.float32)
    residual = np.abs(_seq_sum_rows(values32.astype(np.float64)) - mass)
    budget = _r64(values32.shape[1], mass) + _sum_half_ulp_rows(values32)
    return residual, budget


def _require_finite_range(array, label):
    if not np.isfinite(array).all():
        _fail(f"{label} 含非有限值")
    if (array < 0.0).any():
        _fail(f"{label} 含负值")
    if (array > 1.0).any():
        _fail(f"{label} 含大于 1 的值")


def _validate_names(names, group_count):
    result = _string_list(names, "names", nonempty=True, unique=True)
    if len(result) != group_count:
        _fail(f"names 数量必须等于权重列数 {group_count}")
    return result


def _prepare_weights(weights, names):
    try:
        array = np.asarray(weights)
    except Exception as exc:  # ragged/object input
        _fail(f"weights 不能转换为数值矩阵：{exc}")
    if array.ndim != 2:
        _fail("weights 必须是 N×B 二维矩阵")
    if array.dtype.kind == "b" or array.dtype.kind not in "iuf":
        _fail("weights 必须使用有限实数 dtype")
    if array.shape[0] <= 0 or array.shape[1] <= 0:
        _fail("weights 的 N 和 B 都必须大于 0")
    array64 = array.astype(np.float64, copy=False)
    _require_finite_range(array64, "weights")
    weights32 = array64.astype(np.float32)
    _require_finite_range(weights32, "weights(float32)")
    weights64 = weights32.astype(np.float64)
    quantization = {
        "exact_float32_input": bool(np.array_equal(weights64, array64)),
        "changed_cells": int(np.count_nonzero(weights64 != array64)),
        "max_abs_error": float(
            np.max(np.abs(weights64 - array64), initial=0.0)
        ),
    }
    return weights32, weights64, quantization


def _prepare_edges(edges, vertex_count):
    raw = _strict_edge_array(edges, vertex_count, "edges")
    if raw.size == 0:
        return raw.reshape((0, 2)), frozenset()
    normalized = np.sort(raw, axis=1)
    unique = np.unique(normalized, axis=0)
    edge_set = frozenset((int(a), int(b)) for a, b in unique)
    return unique.astype(np.int64, copy=False), edge_set


def _prepare_regions(regions, vertex_count, edge_set):
    if not isinstance(regions, dict):
        _fail("regions 必须是字典")
    prepared = {}
    for name, raw in regions.items():
        if not isinstance(name, str):
            _fail("regions 的键必须是字符串名")
        if not isinstance(raw, dict):
            _fail(f"region[{name}] 必须是字典")
        for required in ("ids", "alpha", "read_ids", "barriers"):
            if required not in raw:
                _fail(f"region[{name}] 缺少字段 {required!r}")
        ids = _strict_id_array(raw["ids"], vertex_count, f"region[{name}].ids")
        alpha = _real_vector(
            raw["alpha"], len(ids), f"region[{name}].alpha", lo=0.0, hi=1.0
        )
        read_ids = _strict_id_array(
            raw["read_ids"], vertex_count, f"region[{name}].read_ids"
        )
        if not np.isin(ids, read_ids).all():
            _fail(f"region[{name}].read_ids 必须包含全部 ids")
        barriers = _strict_edge_array(
            raw["barriers"], vertex_count, f"region[{name}].barriers"
        )
        if barriers.size:
            normalized_barriers = np.sort(barriers, axis=1)
            unique_barriers = np.unique(normalized_barriers, axis=0)
            for left, right in unique_barriers:
                if (int(left), int(right)) not in edge_set:
                    _fail(
                        f"region[{name}].barriers 中 [{int(left)}, {int(right)}] "
                        "不是 edges 中的真实边"
                    )
        else:
            unique_barriers = np.empty((0, 2), dtype=np.int64)
        prepared[name] = {
            "name": name,
            "ids": ids,
            "alpha": alpha,
            "read_ids": read_ids,
            "barriers": unique_barriers.astype(np.int64, copy=False),
            "barrier_set": frozenset(
                (int(a), int(b)) for a, b in unique_barriers
            ),
            "write_ids": ids[alpha > 0.0].copy(),
        }
    return prepared


def _name_indices(groups, name_to_index, label):
    indices = []
    seen = set()
    for group in groups:
        if group in seen:
            _fail(f"{label} 含重复组 {group!r}")
        seen.add(group)
        index = name_to_index.get(group)
        if index is None:
            _fail(f"{label} 引用了不存在的组 {group!r}")
        indices.append(index)
    return indices


def _parse_assign(raw, region, group_names, name_to_index):
    groups = _string_list(raw["groups"], "ASSIGN.groups")
    if set(groups) != set(group_names):
        _fail("ASSIGN.groups 必须完整覆盖全部可写组（N1 完整集合覆盖）")
    values = raw["values"]
    if not isinstance(values, dict):
        _fail("ASSIGN.values 必须是 name→scalar/数组 的字典")
    for key in values:
        if not isinstance(key, str) or key not in name_to_index:
            _fail(f"ASSIGN.values 含未知组 {key!r}")
    remainder = _string_list(
        raw["remainder"], "ASSIGN.remainder", nonempty=False, unique=True
    )
    if set(values) & set(remainder):
        _fail("ASSIGN.values 与 remainder 不能重叠")
    if set(values) | set(remainder) != set(groups):
        _fail("ASSIGN.values ∪ remainder 必须等于 groups")
    count = len(region["ids"])
    parsed_values = {}
    for name, value in values.items():
        if isinstance(value, (list, tuple, np.ndarray)):
            parsed_values[name] = _real_vector(
                value, count, f"ASSIGN.values[{name}]", lo=0.0, hi=1.0
            )
        else:
            parsed_values[name] = np.full(
                count,
                _real_scalar(value, f"ASSIGN.values[{name}]", lo=0.0, hi=1.0),
                dtype=np.float64,
            )
    return {
        "groups": groups,
        "group_indices": _name_indices(groups, name_to_index, "ASSIGN.groups"),
        "values": parsed_values,
        "remainder": remainder,
        "remainder_indices": _name_indices(
            remainder, name_to_index, "ASSIGN.remainder"
        ),
        "complete": len(remainder) == 0,
    }


def _parse_transfer(raw, region, name_to_index):
    groups = _string_list(raw["groups"], "TRANSFER_AMOUNT.groups")
    if len(groups) != 2:
        _fail("TRANSFER_AMOUNT.groups 必须恰为 [source, receiver]")
    indices = _name_indices(groups, name_to_index, "TRANSFER_AMOUNT.groups")
    if indices[0] == indices[1]:
        _fail("TRANSFER_AMOUNT source 与 receiver 必须是不同组")
    mode = raw["mode"]
    if not isinstance(mode, str) or mode not in {"ABSOLUTE", "FRACTION"}:
        _fail("TRANSFER_AMOUNT.mode 必须是 ABSOLUTE 或 FRACTION")
    count = len(region["ids"])
    amount = raw["amount"]
    if isinstance(amount, (list, tuple, np.ndarray)):
        high = 1.0 if mode == "FRACTION" else None
        parsed_amount = _real_vector(
            amount, count, "TRANSFER_AMOUNT.amount", lo=0.0, hi=high
        )
    else:
        high = 1.0 if mode == "FRACTION" else None
        parsed_amount = np.full(
            count,
            _real_scalar(
                amount, "TRANSFER_AMOUNT.amount", lo=0.0, hi=high
            ),
            dtype=np.float64,
        )
    return {
        "groups": groups,
        "source_index": indices[0],
        "receiver_index": indices[1],
        "mode": mode,
        "amount": parsed_amount,
    }


def _parse_rebalance(raw, group_names, name_to_index):
    groups = _string_list(raw["groups"], "REBALANCE.groups")
    group_indices = _name_indices(groups, name_to_index, "REBALANCE.groups")
    factors = raw["factors"]
    if not isinstance(factors, dict):
        _fail("REBALANCE.factors 必须是 name→非负scalar 的字典")
    if set(factors) != set(groups):
        _fail("REBALANCE.factors 的键必须恰好覆盖 groups")
    parsed = np.empty(len(groups), dtype=np.float64)
    for index, name in enumerate(groups):
        parsed[index] = _real_scalar(
            factors[name], f"REBALANCE.factors[{name}]", lo=0.0
        )
    return {
        "groups": groups,
        "group_indices": group_indices,
        "factors": parsed,
    }


def _parse_smooth(raw, region, group_names, edges, name_to_index):
    if "groups" in raw:
        groups = _string_list(raw["groups"], "SMOOTH.groups")
        if set(groups) != set(group_names):
            _fail("SMOOTH.groups 若提供，必须完整覆盖全部可写组 E")
    else:
        groups = list(group_names)
    iterations = _strict_int(raw["iterations"], "SMOOTH.iterations", lo=1, hi=101)
    read_ids = region["read_ids"]
    read_set = set(int(value) for value in read_ids)
    read_pos = {int(value): index for index, value in enumerate(read_ids)}
    edge_u = []
    edge_v = []
    for left, right in edges:
        left_int = int(left)
        right_int = int(right)
        if left_int not in read_set or right_int not in read_set:
            continue
        if (left_int, right_int) in region["barrier_set"]:
            continue
        edge_u.append(read_pos[left_int])
        edge_v.append(read_pos[right_int])
    degree = np.zeros(len(read_ids), dtype=np.int64)
    if edge_u:
        np.add.at(degree, np.asarray(edge_u, dtype=np.int64), 1)
        np.add.at(degree, np.asarray(edge_v, dtype=np.int64), 1)
    return {
        "groups": groups,
        "iterations": iterations,
        "read_ids": read_ids,
        "read_pos": read_pos,
        "edge_u": np.asarray(edge_u, dtype=np.int64),
        "edge_v": np.asarray(edge_v, dtype=np.int64),
        "degree": degree,
    }


def _parse_field(raw, region, name_to_index):
    groups = _string_list(raw["groups"], "FIELD.groups")
    indices = _name_indices(groups, name_to_index, "FIELD.groups")
    field = _real_matrix(
        raw["field"],
        (len(region["ids"]), len(groups)),
        "FIELD.field",
        lo=0.0,
    )
    return {
        "groups": groups,
        "group_indices": indices,
        "field": field,
    }


def _prepare_stages(stages, group_names, regions, edges):
    if not isinstance(stages, (list, tuple)):
        _fail("stages 必须是顺序列表")
    name_to_index = {name: index for index, name in enumerate(group_names)}
    prepared = []
    for stage_index, raw_stage in enumerate(stages):
        if not isinstance(raw_stage, dict):
            _fail(f"stage[{stage_index}] 必须是字典")
        if set(raw_stage) != {"label", "operations"}:
            _fail(f"stage[{stage_index}] 必须恰好含 label/operations")
        label = raw_stage["label"]
        if not isinstance(label, str):
            _fail(f"stage[{stage_index}].label 必须是字符串")
        raw_operations = raw_stage["operations"]
        if not isinstance(raw_operations, (list, tuple)):
            _fail(f"stage[{stage_index}].operations 必须是列表")
        operations = []
        for operation_index, raw in enumerate(raw_operations):
            if not isinstance(raw, dict):
                _fail(
                    f"stage[{stage_index}].operations[{operation_index}] 必须是字典"
                )
            kind = raw.get("kind")
            if not isinstance(kind, str) or kind not in _KINDS:
                _fail(
                    f"stage[{stage_index}].operations[{operation_index}] "
                    f"kind 无效：{kind!r}"
                )
            allowed_keys = _OPERATION_KEYS[kind]
            required_keys = (
                allowed_keys
                if kind != "SMOOTH"
                else allowed_keys - {"groups"}
            )
            extra_keys = set(raw) - required_keys
            if kind == "FIELD":
                if not required_keys <= set(raw):
                    consumed = ", ".join(sorted(required_keys))
                    _fail(
                        f"stage[{stage_index}].operations[{operation_index}] "
                        f"FIELD 缺少实际消费字段；必须含：{consumed}"
                    )
                if not all(isinstance(key, str) for key in extra_keys):
                    _fail(
                        f"stage[{stage_index}].operations[{operation_index}] "
                        "FIELD 额外审计键必须是字符串键"
                    )
                audit_keys = sorted(extra_keys)
            else:
                if not (
                    set(raw) == allowed_keys
                    or set(raw) == required_keys
                ):
                    consumed = ", ".join(sorted(_CONSUMED_OPERATION_FIELDS[kind]))
                    _fail(
                        f"stage[{stage_index}].operations[{operation_index}] "
                        f"{kind} 字段必须恰为实际消费字段：{consumed}"
                    )
                audit_keys = []
            region_name = raw["region"]
            if not isinstance(region_name, str) or region_name not in regions:
                _fail(
                    f"stage[{stage_index}].operations[{operation_index}] "
                    f"引用了不存在的 region {region_name!r}"
                )
            region = regions[region_name]
            operation = {
                "kind": kind,
                "region_name": region_name,
                "region": region,
                "ids": region["ids"],
                "alpha": region["alpha"],
                "write_ids": region["write_ids"],
                "audit_keys": audit_keys,
            }
            if kind == "ASSIGN":
                operation.update(
                    _parse_assign(raw, region, group_names, name_to_index)
                )
            elif kind == "TRANSFER_AMOUNT":
                operation.update(
                    _parse_transfer(raw, region, name_to_index)
                )
            elif kind == "REBALANCE":
                operation.update(
                    _parse_rebalance(raw, group_names, name_to_index)
                )
            elif kind == "SMOOTH":
                operation.update(
                    _parse_smooth(
                        raw, region, group_names, edges, name_to_index
                    )
                )
            else:
                operation.update(
                    _parse_field(raw, region, name_to_index)
                )
            operations.append(operation)

        write_sets = [
            set(int(value) for value in operation["write_ids"])
            for operation in operations
        ]
        for left_index in range(len(write_sets)):
            for right_index in range(left_index + 1, len(write_sets)):
                overlap = write_sets[left_index] & write_sets[right_index]
                if overlap:
                    sample = sorted(overlap)[:8]
                    _fail(
                        f"stage[{stage_index}] {label!r} 的 operation "
                        f"{left_index}/{right_index} 写域重叠（alpha>0）：{sample}"
                    )
        prepared.append(
            {
                "index": stage_index,
                "label": label,
                "operations": operations,
            }
        )
    return prepared


def _validate_q(q, mass, group_count, label):
    if q.shape != (len(mass), group_count):
        _fail(f"{label} 内部 Q 形状错误")
    if not np.isfinite(q).all():
        _fail(f"{label} Q 含非有限值")
    if (q < 0.0).any():
        _fail(f"{label} Q 含负值")
    if (q > 1.0).any():
        _fail(f"{label} Q 含大于 1 的值")
    qsum = _seq_sum_rows(q)
    tolerance = _r64(group_count, mass)
    bad = np.abs(qsum - mass) > tolerance
    if bad.any():
        first = int(np.flatnonzero(bad)[0])
        _fail(
            f"{label} 的 Q 质量和不等于本步 M "
            f"(index={first}, sum={qsum[first]!r}, M={mass[first]!r}, "
            f"r64={tolerance[first]!r})"
        )


def _mix_rows(old, q, alpha, label):
    if old.shape != q.shape:
        _fail(f"{label} 内部混合形状不一致")
    alpha_column = np.asarray(alpha, dtype=np.float64).reshape((-1, 1))
    difference = q != old
    mixed = old.copy()
    if difference.any():
        calculated = (1.0 - alpha_column) * old + alpha_column * q
        mixed[difference] = calculated[difference]
    if not np.isfinite(mixed).all():
        _fail(f"{label} 混合结果含非有限值")
    if (mixed < 0.0).any():
        _fail(f"{label} 混合结果落到不可表示的负数")
    if (mixed > 1.0).any():
        _fail(f"{label} 混合结果落到大于 1 的值")
    return mixed


def _q_assign(operation, old, mass, group_count, region_name):
    count = old.shape[0]
    q = old.copy()
    assigned_sum = np.zeros(count, dtype=np.float64)
    for name, values in operation["values"].items():
        group_index = operation["group_indices"][
            operation["groups"].index(name)
        ]
        q[:, group_index] = values
    # Assignments are summed in the fixed E order, not dict order.
    assigned_indices = set(
        operation["group_indices"][operation["groups"].index(name)]
        for name in operation["values"]
    )
    for group_index in range(group_count):
        if group_index in assigned_indices:
            assigned_sum += q[:, group_index]
    residual = mass - assigned_sum
    tolerance = _r64(group_count, mass)
    remainder_indices = operation["remainder_indices"]
    if remainder_indices:
        negative = residual < -tolerance
        if negative.any():
            first = int(np.flatnonzero(negative)[0])
            _fail(
                f"ASSIGN[{region_name}] 指定值超过本步 M "
                f"(index={first}, assigned={assigned_sum[first]!r}, "
                f"M={mass[first]!r})"
            )
        residual = np.where(residual < 0.0, 0.0, residual)
        remainder_old = old[:, remainder_indices]
        remainder_sum = _seq_sum_rows(
            old[:, sorted(remainder_indices)]
        )
        positive = residual > 0.0
        if positive.any() and (remainder_sum[positive] <= 0.0).any():
            first = int(np.flatnonzero(positive & (remainder_sum <= 0.0))[0])
            _fail(
                f"ASSIGN[{region_name}] 正余量但 remainder 原和为零 "
                f"(index={first}, residual={residual[first]!r})"
            )
        ratio = np.zeros(count, dtype=np.float64)
        ratio[positive] = residual[positive] / remainder_sum[positive]
        q[:, remainder_indices] = remainder_old * ratio[:, None]
    # Both complete and partial ASSIGN Q are checked against the float64 r64
    # mass gate by the caller; the quantised float32 payload is then checked
    # additionally against R.
    return q


def _q_transfer(operation, old, mass, region_name):
    source_index = operation["source_index"]
    receiver_index = operation["receiver_index"]
    source = old[:, source_index]
    receiver = old[:, receiver_index]
    amount = operation["amount"]
    if operation["mode"] == "FRACTION":
        transfer = source * amount
    else:
        transfer = amount.astype(np.float64, copy=True)
    if (transfer < 0.0).any():
        _fail(f"TRANSFER_AMOUNT[{region_name}] 转移量含负值")
    over_source = transfer > source
    if over_source.any():
        first = int(np.flatnonzero(over_source)[0])
        _fail(
            f"TRANSFER_AMOUNT[{region_name}] 转移量超过 source "
            f"(index={first}, transfer={transfer[first]!r}, "
            f"source={source[first]!r})；不 clamp"
        )
    over_receiver = receiver + transfer > 1.0
    if over_receiver.any():
        first = int(np.flatnonzero(over_receiver)[0])
        _fail(
            f"TRANSFER_AMOUNT[{region_name}] 接收组将超过 1 "
            f"(index={first}, receiver={receiver[first]!r}, "
            f"transfer={transfer[first]!r})；不 clamp"
        )
    q = old.copy()
    q[:, source_index] = source - transfer
    q[:, receiver_index] = receiver + transfer
    return q


def _q_rebalance(operation, old, mass, group_count, region_name):
    factors = operation["factors"]
    if np.all(factors == factors[0]) and factors[0] > 0.0:
        return old.copy()
    indices = operation["group_indices"]
    listed_old = old[:, indices]
    listed_mass = _seq_sum_rows(listed_old)
    if (listed_mass == 0.0).all():
        return old.copy()
    weighted = listed_old * factors[None, :]
    if not np.isfinite(weighted).all():
        _fail(f"REBALANCE[{region_name}] 加权场溢出")
    denominator = _seq_sum_rows(weighted)
    zero = (denominator == 0.0) & (listed_mass > 0.0)
    if zero.any():
        first = int(np.flatnonzero(zero)[0])
        _fail(
            f"REBALANCE[{region_name}] 正 M 点加权分母为零 "
            f"(index={first}, M={mass[first]!r})"
        )
    if not np.isfinite(denominator).all():
        _fail(f"REBALANCE[{region_name}] 加权分母溢出")
    scale = np.zeros_like(listed_mass)
    positive_mass = listed_mass > 0.0
    scale[positive_mass] = (
        listed_mass[positive_mass] / denominator[positive_mass]
    )
    q = old.copy()
    q[:, indices] = weighted * scale[:, None]
    return q


def _q_field(operation, old, mass, group_count, region_name):
    indices = operation["group_indices"]
    field = operation["field"]
    field_sum = _seq_sum_rows(field)
    zero = field_sum <= 0.0
    if zero.any():
        first = int(np.flatnonzero(zero)[0])
        _fail(
            f"FIELD[{region_name}] 正 M 点 field 总和为零 "
            f"(index={first}, M={mass[first]!r})"
        )
    if not np.isfinite(field_sum).all():
        _fail(f"FIELD[{region_name}] field 总和非有限")
    listed = set(indices)
    unlisted = [index for index in range(group_count) if index not in listed]
    if unlisted and (old[:, unlisted] > 0.0).any():
        row, column = np.argwhere(old[:, unlisted] > 0.0)[0]
        _fail(
            f"FIELD[{region_name}] 未列出组存在正质量，拒绝隐式清零 "
            f"(row={int(row)}, group_index={unlisted[int(column)]})"
        )
    q = np.zeros_like(old)
    q[:, indices] = field * (mass / field_sum)[:, None]
    return q


def _smooth_stage(
    operation, old, mass, group_count, active_ids, active_positions, label
):
    region = operation["region"]
    read_ids = operation["read_ids"]
    read_field = old.copy()
    active_pos = active_positions
    degree = operation["degree"]
    if active_pos.size and (degree[active_pos] == 0).any():
        first = int(np.flatnonzero(degree[active_pos] == 0)[0])
        _fail(
            f"{label} 孤立且正 M 写点拒绝 "
            f"(vertex={int(active_ids[first])})"
        )
    edge_u = operation["edge_u"]
    edge_v = operation["edge_v"]
    iteration_reports = []
    for iteration in range(1, operation["iterations"] + 1):
        if active_pos.size == 0:
            break
        neighbor_sum = np.zeros((len(read_ids), group_count), dtype=np.float64)
        if edge_u.size:
            np.add.at(neighbor_sum, edge_u, read_field[edge_v])
            np.add.at(neighbor_sum, edge_v, read_field[edge_u])
        averaged = np.zeros_like(neighbor_sum)
        if edge_u.size:
            np.divide(
                neighbor_sum,
                degree[:, None],
                out=averaged,
                where=degree[:, None] > 0,
            )
        neighbor_average = averaged[active_pos]
        average_sum = _seq_sum_rows(neighbor_average)
        no_coverage = average_sum <= 0.0
        if no_coverage.any():
            first = int(np.flatnonzero(no_coverage)[0])
            _fail(
                f"{label} 邻接均值无覆盖 "
                f"(vertex={int(active_ids[first])})"
            )
        q = neighbor_average * (mass / average_sum)[:, None]
        _validate_q(q, mass, group_count, f"{label} 第{iteration}轮")
        previous = read_field[active_pos].copy()
        mixed = _mix_rows(
            previous,
            q,
            operation["_active_alpha"],
            f"{label} 第{iteration}轮",
        )
        read_field[active_pos] = mixed
        current_mass = _seq_sum_rows(mixed)
        iteration_reports.append(
            {
                "iteration": iteration,
                "max_abs_change": float(
                    np.max(np.abs(mixed - previous), initial=0.0)
                ),
                "max_abs_mass_residual": float(
                    np.max(np.abs(current_mass - mass), initial=0.0)
                ),
            }
        )
    return read_field[active_pos] if active_pos.size else np.empty(
        (0, group_count), dtype=np.float64
    ), iteration_reports


def _membership_summary(before32, after32, group_names):
    added = (before32 == 0.0) & (after32 > 0.0)
    removed = (before32 > 0.0) & (after32 == 0.0)
    by_group = {}
    for group_index in range(before32.shape[1]):
        count_added = int(np.count_nonzero(added[:, group_index]))
        count_removed = int(np.count_nonzero(removed[:, group_index]))
        if count_added or count_removed:
            by_group[group_names[group_index]] = {
                "added": count_added,
                "removed": count_removed,
            }
    return {
        "added_cells": int(np.count_nonzero(added)),
        "removed_cells": int(np.count_nonzero(removed)),
        "by_group": by_group,
    }


def _delta_mass(before32, after32, group_names):
    delta = after32.astype(np.float64).sum(axis=0) - before32.astype(
        np.float64
    ).sum(axis=0)
    return {
        name: float(delta[index]) for index, name in enumerate(group_names)
    }


def compile_weights(weights, names, edges, regions, stages):
    """Compile a frozen weight-repair recipe into a float32 payload.

    Parameters follow API-CONTRACT.md.  The input arrays are never mutated.
    The report contains only JSON-safe Python scalars, lists and dictionaries.
    """

    started = time.perf_counter()
    try:
        raw_names = _string_list(
            names, "names", nonempty=True, unique=True
        )
        weights32, weights64, quantization = _prepare_weights(
            weights, raw_names
        )
        vertex_count, group_count = weights64.shape
        if len(raw_names) != group_count:
            _fail(f"names 数量必须等于权重列数 {group_count}")
        edges_unique, edge_set = _prepare_edges(edges, vertex_count)
        prepared_regions = _prepare_regions(
            regions, vertex_count, edge_set
        )
        prepared_stages = _prepare_stages(
            stages, raw_names, prepared_regions, edges_unique
        )
        validation_seconds = time.perf_counter() - started

        current32 = weights32.copy()
        current64 = weights64.copy()
        initial32 = weights32.copy()
        initial_mass = _mass(weights64)
        zero_mass_vertices = int(np.count_nonzero(initial_mass == 0.0))
        written = np.zeros(vertex_count, dtype=bool)
        cumulative_budget = np.zeros(vertex_count, dtype=np.float64)
        cumulative_max_drift = 0.0
        cumulative_max_budget = 0.0
        stage_reports = []
        global_max_residual = 0.0
        global_max_budget = 0.0
        total_stage_changed = 0

        for stage in prepared_stages:
            stage_started = time.perf_counter()
            frozen64 = current64
            mass = _mass(frozen64)
            stage64 = current64.copy()
            before32 = current32
            op_runtime = []
            active_union = np.zeros(vertex_count, dtype=bool)

            for operation_index, operation in enumerate(stage["operations"]):
                operation_started = time.perf_counter()
                ids = operation["ids"]
                alpha = operation["alpha"]
                mass_ids = mass[ids]
                active = (alpha > 0.0) & (mass_ids > 0.0)
                active_ids = ids[active]
                active_alpha = alpha[active]
                requested = alpha > 0.0
                zero_mass = requested & (mass_ids == 0.0)
                label = (
                    f"stage[{stage['index']}] {stage['label']!r} "
                    f"operation[{operation_index}] {operation['kind']}"
                )
                operation["_active_alpha"] = active_alpha
                smooth_report = None
                if active_ids.size:
                    old = frozen64[active_ids]
                    active_mass = mass_ids[active]
                    active_operation = operation
                    if operation["kind"] in {
                        "ASSIGN",
                        "TRANSFER_AMOUNT",
                        "FIELD",
                    }:
                        active_operation = dict(operation)
                        if operation["kind"] == "ASSIGN":
                            active_operation["values"] = {
                                name: values[active]
                                for name, values in operation["values"].items()
                            }
                        elif operation["kind"] == "TRANSFER_AMOUNT":
                            active_operation["amount"] = operation["amount"][
                                active
                            ]
                        else:
                            active_operation["field"] = operation["field"][
                                active
                            ]
                    if operation["kind"] == "ASSIGN":
                        q = _q_assign(
                            active_operation,
                            old,
                            active_mass,
                            group_count,
                            operation["region_name"],
                        )
                    elif operation["kind"] == "TRANSFER_AMOUNT":
                        q = _q_transfer(
                            active_operation,
                            old,
                            active_mass,
                            operation["region_name"],
                        )
                    elif operation["kind"] == "REBALANCE":
                        q = _q_rebalance(
                            operation,
                            old,
                            active_mass,
                            group_count,
                            operation["region_name"],
                        )
                    elif operation["kind"] == "FIELD":
                        q = _q_field(
                            active_operation,
                            old,
                            active_mass,
                            group_count,
                            operation["region_name"],
                        )
                    else:
                        if "read_pos" not in operation:
                            _fail(f"{label} 内部 SMOOTH 图缺失")
                        active_positions = np.asarray(
                            [
                                operation["read_pos"][int(value)]
                                for value in active_ids
                            ],
                            dtype=np.int64,
                        )
                        smoothed, smooth_report = _smooth_stage(
                            operation,
                            frozen64[operation["read_ids"]],
                            active_mass,
                            group_count,
                            active_ids,
                            active_positions,
                            label,
                        )
                        stage64[active_ids] = smoothed
                        q = None
                    if q is not None:
                        _validate_q(
                            q,
                            active_mass,
                            group_count,
                            label,
                        )
                        if operation["kind"] == "ASSIGN":
                            q32 = q.astype(np.float32)
                            q_residual, q_budget = _quality_arrays(
                                q32, active_mass
                            )
                            if (q_residual > q_budget).any():
                                first = int(
                                    np.flatnonzero(
                                        q_residual > q_budget
                                    )[0]
                                )
                                _fail(
                                    f"{label} 的 float32 ASSIGN 目标质量超出预算 "
                                    f"(vertex={int(active_ids[first])}, "
                                    f"residual={q_residual[first]!r}, "
                                    f"budget={q_budget[first]!r})"
                                )
                        stage64[active_ids] = _mix_rows(
                            old, q, active_alpha, label
                        )
                active_union[ids[active]] = True
                op_runtime.append(
                    {
                        "operation": operation,
                        "active_ids": active_ids,
                        "active_alpha": active_alpha,
                        "active_mask": active,
                        "requested_mask": requested,
                        "zero_mass_mask": zero_mass,
                        "smooth_report": smooth_report,
                        "compute_seconds": time.perf_counter()
                        - operation_started,
                    }
                )

            if not np.isfinite(stage64).all():
                _fail(f"stage[{stage['index']}] 混合场含非有限值")
            if (stage64 < 0.0).any():
                _fail(f"stage[{stage['index']}] 混合场落到负数")
            if (stage64 > 1.0).any():
                _fail(f"stage[{stage['index']}] 混合场落到大于 1")
            after32 = stage64.astype(np.float32)
            _require_finite_range(after32, f"stage[{stage['index']}] 输出")
            expected_unchanged = ~active_union
            if expected_unchanged.any() and (
                after32[expected_unchanged] != before32[expected_unchanged]
            ).any():
                _fail(
                    f"stage[{stage['index']}] 改动了写域外的权重"
                )

            operation_reports = []
            stage_added = 0
            stage_removed = 0
            stage_added_by_group = {
                name: 0 for name in raw_names
            }
            stage_removed_by_group = {
                name: 0 for name in raw_names
            }
            stage_delta = np.zeros(group_count, dtype=np.float64)
            stage_residual = 0.0
            stage_budget = 0.0
            stage_margin = None
            stage_zero_mass = 0
            stage_requested = 0
            stage_active = 0
            stage_iterations = []
            operation_counts = {}

            for runtime in op_runtime:
                operation = runtime["operation"]
                kind = operation["kind"]
                operation_counts[kind] = operation_counts.get(kind, 0) + 1
                active_ids = runtime["active_ids"]
                requested_mask = runtime["requested_mask"]
                zero_mass_mask = runtime["zero_mass_mask"]
                requested_ids = operation["ids"][requested_mask]
                alpha_zero = int(
                    np.count_nonzero(operation["alpha"] == 0.0)
                )
                stage_requested += len(requested_ids)
                stage_zero_mass += int(np.count_nonzero(zero_mass_mask))
                stage_active += len(active_ids)
                finalize_started = time.perf_counter()
                if active_ids.size:
                    rows_before = before32[active_ids]
                    rows_after = after32[active_ids]
                    active_mass = mass[active_ids]
                    residual_vector, budget_vector = _quality_arrays(
                        rows_after, active_mass
                    )
                    failures = residual_vector > budget_vector
                    if failures.any():
                        first = int(np.flatnonzero(failures)[0])
                        _fail(
                            f"{kind}[{operation['region_name']}] 量化后质量超出预算 "
                            f"(vertex={int(active_ids[first])}, "
                            f"residual={residual_vector[first]!r}, "
                            f"budget={budget_vector[first]!r})"
                        )
                    operation_residual = float(
                        np.max(residual_vector, initial=0.0)
                    )
                    operation_budget = float(
                        np.max(budget_vector, initial=0.0)
                    )
                    operation_margin = float(
                        np.min(budget_vector - residual_vector)
                    )
                    changed = int(
                        np.count_nonzero(rows_before != rows_after)
                    )
                    membership = _membership_summary(
                        rows_before, rows_after, raw_names
                    )
                    delta = _delta_mass(
                        rows_before, rows_after, raw_names
                    )
                    written[active_ids] = True
                    cumulative_budget[active_ids] += budget_vector
                else:
                    operation_residual = 0.0
                    operation_budget = 0.0
                    operation_margin = 0.0
                    changed = 0
                    membership = {
                        "added_cells": 0,
                        "removed_cells": 0,
                        "by_group": {},
                    }
                    delta = {name: 0.0 for name in raw_names}
                inactive_requested_ids = operation["ids"][
                    runtime["requested_mask"] & ~runtime["active_mask"]
                ]
                if inactive_requested_ids.size:
                    requested_rows_before = before32[
                        inactive_requested_ids
                    ]
                    requested_rows_after = after32[
                        inactive_requested_ids
                    ]
                    if (
                        requested_rows_before != requested_rows_after
                    ).any():
                        _fail(
                            f"{kind}[{operation['region_name']}] 改动了 M0/alpha0 写域"
                        )
                stage_added += membership["added_cells"]
                stage_removed += membership["removed_cells"]
                for name, pair in membership["by_group"].items():
                    stage_added_by_group[name] += pair["added"]
                    stage_removed_by_group[name] += pair["removed"]
                for name, value in delta.items():
                    stage_delta[raw_names.index(name)] += value
                stage_residual = max(stage_residual, operation_residual)
                stage_budget = max(stage_budget, operation_budget)
                if active_ids.size:
                    stage_margin = (
                        operation_margin
                        if stage_margin is None
                        else min(stage_margin, operation_margin)
                    )
                global_max_residual = max(
                    global_max_residual, operation_residual
                )
                global_max_budget = max(
                    global_max_budget, operation_budget
                )
                operation_report = {
                    "index": len(operation_reports),
                    "kind": kind,
                    "region": operation["region_name"],
                    "groups": list(operation["groups"]),
                    "requested_vertices": int(len(requested_ids)),
                    "write_vertices": int(len(active_ids)),
                    "zero_mass_preserved": int(
                        np.count_nonzero(zero_mass_mask)
                    ),
                    "alpha_zero_skipped": alpha_zero,
                    "changed_cells": changed,
                    "add_remove": membership,
                    "added_cells": membership["added_cells"],
                    "removed_cells": membership["removed_cells"],
                    "delta_mass": delta,
                    "residual": operation_residual,
                    "budget": operation_budget,
                    "min_margin": operation_margin,
                    "consumed_fields": list(
                        _CONSUMED_OPERATION_FIELDS[kind]
                    ),
                    "optional_consumed_fields": (
                        ["groups"] if kind == "SMOOTH" else []
                    ),
                    "audit_keys": list(operation.get("audit_keys", [])),
                    "timings": {
                        "seconds": float(
                            runtime["compute_seconds"]
                            + time.perf_counter()
                            - finalize_started
                        )
                    },
                }
                if kind == "SMOOTH":
                    operation_report["iterations"] = operation["iterations"]
                    operation_report["iteration_reports"] = runtime[
                        "smooth_report"
                    ] or []
                    stage_iterations.extend(
                        operation_report["iteration_reports"]
                    )
                if kind == "ASSIGN":
                    operation_report["complete_assignment"] = operation[
                        "complete"
                    ]
                if kind == "TRANSFER_AMOUNT":
                    operation_report["mode"] = operation["mode"]
                if kind == "FIELD":
                    operation_report["field_cells"] = int(
                        operation["field"].size
                    )
                operation_reports.append(operation_report)

            stage_changed_count = int(
                np.count_nonzero(after32 != before32)
            )
            current32 = after32
            current64 = after32.astype(np.float64)
            total_stage_changed += stage_changed_count
            if written.any():
                current_mass = _mass(current64)
                drift = np.abs(current_mass[written] - initial_mass[written])
                cumulative_max_drift = max(
                    cumulative_max_drift,
                    float(np.max(drift, initial=0.0)),
                )
                cumulative_max_budget = max(
                    cumulative_max_budget,
                    float(
                        np.max(
                            cumulative_budget[written], initial=0.0
                        )
                    ),
                )
                if (drift > cumulative_budget[written]).any():
                    first = int(
                        np.flatnonzero(written)[
                            np.flatnonzero(
                                drift > cumulative_budget[written]
                            )[0]
                        ]
                    )
                    _fail(
                        f"stage[{stage['index']}] 累积质量漂移超过累计预算 "
                        f"(vertex={first})"
                    )
            delta_mass = {
                name: float(stage_delta[index])
                for index, name in enumerate(raw_names)
            }
            stage_report = {
                "index": stage["index"],
                "label": stage["label"],
                "operation_count": len(operation_reports),
                "operation_counts": operation_counts,
                "requested_vertices": int(stage_requested),
                "write_vertices": int(stage_active),
                "zero_mass_preserved": int(stage_zero_mass),
                "changed_cells": stage_changed_count,
                "add_remove": {
                    "added_cells": int(stage_added),
                    "removed_cells": int(stage_removed),
                    "by_group": {
                        name: {
                            "added": int(stage_added_by_group[name]),
                            "removed": int(stage_removed_by_group[name]),
                        }
                        for name in raw_names
                        if stage_added_by_group[name]
                        or stage_removed_by_group[name]
                    },
                },
                "added_cells": int(stage_added),
                "removed_cells": int(stage_removed),
                "delta_mass": delta_mass,
                "residual": stage_residual,
                "budget": stage_budget,
                "min_margin": float(
                    0.0 if stage_margin is None else stage_margin
                ),
                "operations": operation_reports,
                "timings": {
                    "seconds": float(time.perf_counter() - stage_started)
                },
                "cumulative_max_abs_mass_drift": float(
                    cumulative_max_drift
                ),
                "cumulative_max_abs_drift": float(
                    cumulative_max_drift
                ),
                "cumulative_max_budget": float(cumulative_max_budget),
            }
            if "SMOOTH" in operation_counts:
                stage_report["smooth_iteration_reports"] = stage_iterations
            stage_reports.append(stage_report)

        output = np.array(current32, dtype=np.float32, copy=True, order="C")
        final_changed = int(np.count_nonzero(output != initial32))
        written_vertices = int(np.count_nonzero(written))
        region_report = {}
        for name, region in prepared_regions.items():
            region_report[name] = {
                "vertex_count": int(len(region["ids"])),
                "read_vertex_count": int(len(region["read_ids"])),
                "write_vertex_count": int(
                    np.count_nonzero(region["alpha"] > 0.0)
                ),
                "alpha_zero_count": int(
                    np.count_nonzero(region["alpha"] == 0.0)
                ),
                "barrier_count": int(len(region["barriers"])),
            }
        total_seconds = time.perf_counter() - started
        report = {
            "schema": "rigging.weight-repair.math-report.v1",
            "vertex_count": int(vertex_count),
            "group_count": int(group_count),
            "group_names": list(raw_names),
            "edge_count": int(len(edges_unique)),
            "input": {
                "quantized_to_float32": True,
                "exact_float32_input": quantization[
                    "exact_float32_input"
                ],
                "quantization_changed_cells": quantization[
                    "changed_cells"
                ],
                "quantization_max_abs_error": quantization[
                    "max_abs_error"
                ],
                "zero_mass_vertices": zero_mass_vertices,
                "mass_min": float(np.min(initial_mass)),
                "mass_max": float(np.max(initial_mass)),
            },
            "regions": region_report,
            "steps": stage_reports,
            "output": {
                "dtype": "float32",
                "shape": [int(vertex_count), int(group_count)],
                "sha256": hashlib.sha256(output.tobytes(order="C")).hexdigest(),
                "changed_cells_from_input": final_changed,
                "changed_cells_total": int(total_stage_changed),
                "written_vertices": written_vertices,
                "cumulative_max_abs_mass_drift": float(
                    cumulative_max_drift
                ),
                "cumulative_max_abs_drift": float(
                    cumulative_max_drift
                ),
                "cumulative_max_budget": float(cumulative_max_budget),
            },
            "scope": {
                "pure_numeric": True,
                "bpy_imported": False,
                "locks_consumers_source_identity": (
                    "handled by bpy adapter; not evaluated by this module"
                ),
                "float32_payload": (
                    "adapter must write these precompiled float32 values exactly"
                ),
            },
            "quality": {
                "max_residual": float(global_max_residual),
                "max_budget": float(global_max_budget),
                "residual_within_budget": True,
            },
            "timings": {
                "total_seconds": float(total_seconds),
                "validation_seconds": float(validation_seconds),
                "stage_seconds": [
                    stage["timings"]["seconds"] for stage in stage_reports
                ],
            },
        }
        return output, report
    except WeightMathError:
        raise
    except Exception as exc:
        raise WeightMathError(f"数值编译失败：{exc}") from exc


def grow_region(
    vertex_count, edges, seeds, *, rings, barriers=(), allowed_ids=None
):
    """Grow a write region and one read-only outer ring from explicit seeds."""

    vertex_count_int = _strict_int(
        vertex_count, "vertex_count", lo=1
    )
    rings_int = _strict_int(rings, "rings", lo=0)
    edges_array, _ = _prepare_edges(edges, vertex_count_int)
    seed_array = _strict_id_array(
        seeds, vertex_count_int, "seeds", allow_empty=False
    )
    if allowed_ids is None:
        allowed = np.arange(vertex_count_int, dtype=np.int64)
    else:
        allowed = _strict_id_array(
            allowed_ids,
            vertex_count_int,
            "allowed_ids",
            allow_empty=True,
        )
    allowed_set = set(int(value) for value in allowed)
    if not set(int(value) for value in seed_array) <= allowed_set:
        _fail("seeds 必须全部位于 allowed_ids 内")
    barrier_array = _strict_edge_array(
        barriers, vertex_count_int, "barriers"
    )
    if barrier_array.size:
        edge_set = frozenset(
            (int(a), int(b)) for a, b in edges_array
        )
        normalized_barriers = np.sort(barrier_array, axis=1)
        unique_barriers = np.unique(normalized_barriers, axis=0)
        for left, right in unique_barriers:
            if (int(left), int(right)) not in edge_set:
                _fail(
                    f"barriers 中 [{int(left)}, {int(right)}] "
                    "不是 edges 中的真实边"
                )
    else:
        unique_barriers = np.empty((0, 2), dtype=np.int64)
    barrier_set = frozenset(
        (int(a), int(b)) for a, b in unique_barriers
    )

    adjacency = [[] for _ in range(vertex_count_int)]
    for left, right in edges_array:
        left_int = int(left)
        right_int = int(right)
        if (left_int, right_int) in barrier_set:
            continue
        adjacency[left_int].append(right_int)
        adjacency[right_int].append(left_int)
    for neighbors in adjacency:
        neighbors.sort()

    distance = np.full(vertex_count_int, -1, dtype=np.int64)
    queue = deque()
    for seed in seed_array:
        seed_int = int(seed)
        distance[seed_int] = 0
        queue.append(seed_int)
    while queue:
        current = queue.popleft()
        current_distance = int(distance[current])
        if current_distance >= rings_int:
            continue
        for neighbor in adjacency[current]:
            if neighbor not in allowed_set:
                continue
            if distance[neighbor] != -1:
                continue
            distance[neighbor] = current_distance + 1
            queue.append(neighbor)

    ids = [
        index
        for index in range(vertex_count_int)
        if 0 <= int(distance[index]) <= rings_int
    ]
    ids.sort()
    denominator = float(rings_int + 1)
    alpha = [
        1.0 - float(int(distance[index])) / denominator for index in ids
    ]
    ids_set = set(ids)
    read_set = set(ids)
    for vertex in ids:
        for neighbor in adjacency[vertex]:
            if neighbor in allowed_set and neighbor not in ids_set:
                read_set.add(neighbor)
    read_ids = sorted(read_set)
    barrier_list = [
        [int(left), int(right)] for left, right in unique_barriers
    ]
    barrier_list.sort(key=lambda pair: (pair[0], pair[1]))
    return {
        "ids": [int(value) for value in ids],
        "alpha": [float(value) for value in alpha],
        "read_ids": [int(value) for value in read_ids],
        "barriers": barrier_list,
    }


def _mirror_cell_key(coordinate, cell):
    try:
        quotient = float(coordinate) / cell
        if not math.isfinite(quotient):
            quotient = math.copysign(1.0e300, quotient)
        return math.floor(quotient)
    except (OverflowError, ValueError):
        return 0


def mirror_pairs(
    points, left_ids, right_ids, *, axis=0, plane=0.0, tolerance
):
    """Build one-to-one mutual mirror candidates; ambiguity is never forced."""

    try:
        array = np.asarray(points)
    except Exception as exc:
        _fail(f"points 不能转换为数组：{exc}")
    if array.ndim != 2 or array.shape[1] != 3:
        _fail("points 必须是 N×3 的米制共同空间坐标")
    if array.dtype.kind == "b" or array.dtype.kind not in "iuf":
        _fail("points 必须使用有限实数 dtype")
    points64 = array.astype(np.float64, copy=False)
    if not np.isfinite(points64).all():
        _fail("points 含非有限坐标")
    axis_int = _strict_int(axis, "axis", lo=0, hi=3)
    plane_value = _real_scalar(plane, "plane")
    tolerance_value = _real_scalar(tolerance, "tolerance", lo=0.0)
    point_count = points64.shape[0]
    left = _strict_id_array(
        left_ids, point_count, "left_ids", allow_empty=True
    )
    right = _strict_id_array(
        right_ids, point_count, "right_ids", allow_empty=True
    )
    overlap = set(int(value) for value in left) & set(
        int(value) for value in right
    )
    if overlap:
        _fail(
            "left_ids 与 right_ids 不能重叠；中心点由 bpy adapter 显式处理"
        )

    reflected_left = points64[left].copy()
    if reflected_left.size:
        try:
            with np.errstate(over='raise', invalid='raise'):
                reflected_left[:, axis_int] = plane_value + (plane_value - reflected_left[:, axis_int])
        except FloatingPointError:
            _fail('对称反射超出float64有限范围，不能当作模型未配对')
        if not np.isfinite(reflected_left).all():
            _fail('对称反射必须有限')

    left_candidates = {int(value): [] for value in left}
    right_candidates = {int(value): [] for value in right}
    if left.size and right.size:
        if tolerance_value == 0.0:
            grid = {}
            for right_id in right:
                key = tuple(
                    float(value) for value in points64[int(right_id)]
                )
                grid.setdefault(key, []).append(int(right_id))
            for left_index, left_id in enumerate(left):
                key = tuple(
                    float(value) for value in reflected_left[left_index]
                )
                for right_id in grid.get(key, ()):
                    distance = 0.0
                    left_candidates[int(left_id)].append(
                        (int(right_id), distance)
                    )
        else:
            cell = float(tolerance_value)
            grid = {}
            for right_id in right:
                key = tuple(
                    _mirror_cell_key(value, cell)
                    for value in points64[int(right_id)]
                )
                grid.setdefault(key, []).append(int(right_id))
            offsets = tuple(product((-1, 0, 1), repeat=3))
            for left_index, left_id in enumerate(left):
                reflected = reflected_left[left_index]
                base = tuple(
                    _mirror_cell_key(value, cell) for value in reflected
                )
                seen = set()
                for offset in offsets:
                    key = (
                        base[0] + offset[0],
                        base[1] + offset[1],
                        base[2] + offset[2],
                    )
                    for right_id in grid.get(key, ()):
                        if right_id in seen:
                            continue
                        seen.add(right_id)
                        difference = points64[right_id] - reflected
                        distance = math.hypot(*(float(value) for value in difference))
                        if distance <= tolerance_value:
                            left_candidates[int(left_id)].append(
                                (
                                    int(right_id),
                                    distance,
                                )
                            )
        for left_id, candidates in left_candidates.items():
            for right_id, distance in candidates:
                right_candidates[right_id].append(
                    (left_id, distance)
                )
        for candidates in left_candidates.values():
            candidates.sort(key=lambda item: item[0])
        for candidates in right_candidates.values():
            candidates.sort(key=lambda item: item[0])

    pairs = []
    paired_left = set()
    paired_right = set()
    for left_id in left:
        candidates = left_candidates[int(left_id)]
        if len(candidates) != 1:
            continue
        right_id, distance = candidates[0]
        if len(right_candidates[right_id]) != 1:
            continue
        if right_candidates[right_id][0][0] != int(left_id):
            continue
        pairs.append((int(left_id), int(right_id), float(distance)))
        paired_left.add(int(left_id))
        paired_right.add(right_id)
        left_candidates[int(left_id)][0] = (
            right_id,
            float(distance),
        )
    pairs.sort(key=lambda item: (item[0], item[1]))

    unmatched_left = sorted(
        int(value)
        for value in left
        if int(value) not in paired_left
        and not left_candidates[int(value)]
    )
    unmatched_right = sorted(
        int(value)
        for value in right
        if int(value) not in paired_right
        and not right_candidates[int(value)]
    )
    ambiguous_left = sorted(
        int(value)
        for value in left
        if int(value) not in paired_left
        and left_candidates[int(value)]
    )
    ambiguous_right = sorted(
        int(value)
        for value in right
        if int(value) not in paired_right
        and right_candidates[int(value)]
    )
    ambiguous_details = []
    for side, identifiers, candidates_map in (
        ("left", ambiguous_left, left_candidates),
        ("right", ambiguous_right, right_candidates),
    ):
        for identifier in identifiers:
            candidates = [
                candidate_id
                for candidate_id, _ in candidates_map[identifier]
            ]
            ambiguous_details.append(
                {
                    "side": side,
                    "id": int(identifier),
                    "candidates": candidates[:64],
                    "candidate_count": len(candidates),
                    "candidates_truncated": len(candidates) > 64,
                }
            )
    complete = (
        left.size > 0
        and right.size > 0
        and len(pairs) == int(len(left))
        and len(pairs) == int(len(right))
        and not unmatched_left
        and not unmatched_right
        and not ambiguous_left
        and not ambiguous_right
    )
    max_distance = (
        max(item[2] for item in pairs) if pairs else 0.0
    )
    all_candidate_distances = [
        distance
        for candidates in left_candidates.values()
        for _, distance in candidates
    ]
    max_candidate_distance = (
        max(all_candidate_distances)
        if all_candidate_distances
        else 0.0
    )
    result = {
        "schema": "rigging.weight-repair.mirror-pairs.v1",
        "pairs": [[left_id, right_id] for left_id, right_id, _ in pairs],
        "unmatched": sorted(unmatched_left + unmatched_right),
        "ambiguous": sorted(ambiguous_left + ambiguous_right),
        "unmatched_left": unmatched_left,
        "unmatched_right": unmatched_right,
        "ambiguous_left": ambiguous_left,
        "ambiguous_right": ambiguous_right,
        "ambiguous_details": ambiguous_details,
        "max_distance": float(max_distance),
        "max_candidate_distance": float(max_candidate_distance),
        "tolerance": float(tolerance_value),
        "axis": int(axis_int),
        "plane": float(plane_value),
        "left_count": int(len(left)),
        "right_count": int(len(right)),
        "pair_count": int(len(pairs)),
        "complete": bool(complete),
        "confirmed": bool(complete and len(pairs) > 0),
        "pairing_requires_explicit_bpy_confirmation": True,
    }
    return result
