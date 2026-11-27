"""Surface-driven joint candidates for modular human/quadruped references.

SPDX-License-Identifier: GPL-3.0-or-later

This module is deliberately independent of the C94 atlas and of any old target
armature/weights.  It reads only:

* the new modular reference record (``rigging_modular_reference_v1``) for the
  module/bone layout and as a declared neutral proportion seed;
* the current raw target mesh objects for their actual surface.

The result is a *candidate* only.  Every estimate is reported as
``proposed_needs_review``; this module never calls ``confirm_reference`` and
never marks the authoring rest as confirmed.

Public API
----------
estimate(reference, target_objects, *, forward_axis='-Y', up_axis='Z')
    Write proposed head/tail positions into ``reference`` and return a JSON
    report.  Only Z-up with horizontal +/-X or +/-Y forward is supported.
    The production target is never rotated; a fresh unconfirmed reference is
    yaw-aligned to the requested forward before the candidate is committed.
require_current(reference)
    Revalidate the exact source Object IDs, mesh geometry/topology/UV/weights,
    reference transform and reference pose captured by the last estimate.
mirror(reference, side='L')
    Copy one side's local-X-mirrored joint positions to the other side.
    ``side`` is the source side: 'L' copies L -> R, 'R' copies R -> L.

All target mesh data is read-only.  World geometry is mapped into the
reference with ``reference.matrix_world.inverted()`` before any measurement.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import struct
from contextlib import contextmanager
from datetime import datetime, timezone

import bpy
import numpy as np
from mathutils import Matrix, Vector


REFERENCE_KEY = "rigging_modular_reference_v1"
REFERENCE_SCHEMA = "rigging.modular.reference.v1"
POSITIONING_KEY = "rigging_modular_positioning_v1"
POSITIONING_SCHEMA = "rigging.modular.positioning.v1"
__all__ = ("estimate", "require_current", "mirror")
KINDS = ("HUMAN", "QUADRUPED")
MODULES = (
    "body",
    "head",
    "shoulder_girdle",
    "forelimb.L",
    "forelimb.R",
    "hindlimb.L",
    "hindlimb.R",
    "tail",
    "correctives",
)

_IDENTITY = Matrix.Identity(4)
_EPS = 1e-9


class PositioningError(ValueError):
    """A user-facing positioning precondition/validation failure."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _jsonable(value):
    """Recursively convert Blender/numpy scalar containers to JSON values."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (np.generic,)):
        return _jsonable(value.item())
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, Matrix):
        return [[_jsonable(value[i][j]) for j in range(4)] for i in range(4)]
    if isinstance(value, Vector):
        return [_jsonable(number) for number in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        number = float(value)
        if not math.isfinite(number):
            raise PositioningError("报告包含非有限数值")
        return number
    raise PositioningError("报告包含不可序列化类型：" + type(value).__name__)


def _deep(value):
    return copy.deepcopy(value)


def _finite_vector(value, size=3):
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (size,) or not np.isfinite(array).all():
        raise PositioningError("需要 %d 个有限实数" % size)
    return array


def _vec(value) -> list[float]:
    return [float(number) for number in _finite_vector(value, len(value))]


def _vec_hex(value) -> list[str]:
    return [float(number).hex() for number in _finite_vector(value, len(value))]


def _matrix_hex(value: Matrix) -> list[list[str]]:
    return [[float(value[i][j]).hex() for j in range(4)] for i in range(4)]


def _sha_bytes(*parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, str):
            part = part.encode("utf-8")
        digest.update(part)
    return digest.hexdigest()


def _array_digest(array) -> str:
    array = np.ascontiguousarray(array)
    meta = struct.pack("<cI", array.dtype.byteorder.encode("ascii"), array.dtype.itemsize)
    shape = struct.pack("<I", array.ndim) + b"".join(
        struct.pack("<Q", int(size)) for size in array.shape
    )
    return _sha_bytes(meta, shape, array.tobytes(order="C"))


def _json_hex(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalize_axis(value, *, name: str) -> tuple[str, Vector]:
    text = str(value or "").strip().upper().replace(" ", "")
    match = re.fullmatch(r"([+-]?)([XYZ])", text) or re.fullmatch(r"([XYZ])([+-])", text)
    if match is None:
        raise PositioningError(
            "%s 必须是 X/Y/Z 轴（可带 +/-）：%r" % (name, value)
        )
    if match.group(2) in "XYZ":
        sign, axis = match.group(1), match.group(2)
    else:
        axis, sign = match.group(1), match.group(2)
    sign = sign or "+"
    if name == "up_axis" and (axis != "Z" or sign != "+"):
        raise PositioningError(
            "当前仅支持 Z 向上。先把角色对齐为 Z-up 后，再以 up_axis='Z' 重试。"
        )
    if name == "forward_axis" and axis not in {"X", "Y"}:
        raise PositioningError(
            "当前仅支持水平 +/-X 或 +/-Y 朝向（Z-up）。"
            "请先把角色朝向对齐到 ±X/±Y，再重试。"
        )
    vector = Vector((0.0, 0.0, 0.0))
    vector["XYZ".index(axis)] = 1.0 if sign == "+" else -1.0
    return sign + axis, vector


def _normalize_kind(kind) -> str:
    if not isinstance(kind, str):
        raise PositioningError("参考 kind 必须是 HUMAN 或 QUADRUPED")
    normalized = kind.strip().upper()
    if normalized not in KINDS:
        raise PositioningError("参考 kind 必须是 HUMAN 或 QUADRUPED：" + repr(kind))
    return normalized


def _normalize_modules(value) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise PositioningError("参考 configuration.enabled_modules 必须是列表")
    selected = []
    for item in value:
        if not isinstance(item, str) or item not in MODULES:
            raise PositioningError("参考 configuration 含未知模块：" + repr(item))
        if item in selected:
            raise PositioningError("参考 configuration 含重复模块：" + item)
        selected.append(item)
    if "body" not in selected:
        raise PositioningError("参考 configuration 缺少 body 模块")
    return [name for name in MODULES if name in selected]


def _module_bones(kind: str, enabled) -> dict[str, list[str]]:
    enabled_set = set(enabled)
    body = [
        "spine",
        "spine.001",
        "spine.002",
        "spine.003",
        "shoulder.L",
        "shoulder.R",
        "pelvis.L",
        "pelvis.R",
    ]
    if kind == "QUADRUPED":
        body.append("pelvis.C")
    result = {"body": sorted(body)}
    if "shoulder_girdle" in enabled_set:
        if kind != "HUMAN":
            raise PositioningError("shoulder_girdle 仅适用于人形肩带")
        result["shoulder_girdle"] = ["scapula.L", "scapula.R"]
    if "head" in enabled_set:
        result["head"] = sorted(["spine.004", "spine.005", "spine.006"])
    for side in ("L", "R"):
        fore = ["upper_arm." + side, "forearm." + side, "hand." + side]
        hind = ["thigh." + side, "shin." + side, "foot." + side]
        if kind == "QUADRUPED":
            fore.append("f_toe." + side)
            hind.append("r_toe." + side)
        else:
            hind.extend(["toe." + side, "heel.02." + side])
        if "forelimb." + side in enabled_set:
            result["forelimb." + side] = sorted(fore)
        if "hindlimb." + side in enabled_set:
            result["hindlimb." + side] = sorted(hind)
    if "tail" in enabled_set:
        result["tail"] = sorted(
            ["tail.001", "tail.002", "tail.003", "tail.004"]
        )
    if "correctives" in enabled_set:
        result["correctives"] = []
    if set(result) != enabled_set:
        raise PositioningError("内部模块表与 configuration 不一致")
    return result


def _parse_reference(reference) -> tuple[dict, list[str], dict[str, list[str]]]:
    if not isinstance(reference, bpy.types.Object) or reference.type != "ARMATURE":
        raise PositioningError("estimate/require_current/mirror 需要 ARMATURE Object")
    scene = bpy.context.scene
    if scene.objects.get(reference.name) != reference:
        raise PositioningError("参考必须是当前 Scene 中的对象")
    if reference.library is not None or reference.data.library is not None:
        raise PositioningError("参考必须本地可编辑，不接受链接库对象")
    raw = reference.get(REFERENCE_KEY)
    if raw is None:
        raise PositioningError("Object 不是模块化参考：缺少 " + REFERENCE_KEY)
    if not isinstance(raw, str):
        raise PositioningError("模块化参考记录不是 JSON 字符串")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PositioningError("模块化参考记录 JSON 损坏") from error
    if not isinstance(record, dict):
        raise PositioningError("模块化参考记录必须是对象")
    if record.get("schema") != REFERENCE_SCHEMA:
        raise PositioningError("模块化参考 schema 无效")
    kind = _normalize_kind(record.get("kind"))
    if kind != record.get("kind"):
        raise PositioningError("模块化参考 kind 未规范化")
    configuration = record.get("configuration")
    if not isinstance(configuration, dict):
        raise PositioningError("模块化参考 configuration 无效")
    if _normalize_kind(configuration.get("kind")) != kind:
        raise PositioningError("模块化参考 configuration.kind 与记录不一致")
    enabled = _normalize_modules(configuration.get("enabled_modules"))
    module_bones = record.get("module_bones")
    if not isinstance(module_bones, dict) or set(module_bones) != set(enabled):
        raise PositioningError("模块化参考 module_bones 键与 configuration 不一致")
    expected = _module_bones(kind, enabled)
    for name, bones in expected.items():
        stored = module_bones.get(name)
        if not isinstance(stored, list) or stored != sorted(bones):
            raise PositioningError("模块化参考 module_bones 内容无效：" + name)
    expected_union = {name for bones in expected.values() for name in bones}
    actual = set(reference.data.bones.keys())
    if actual != expected_union:
        raise PositioningError(
            "参考骨集合与模块配置不一致：缺少="
            + str(sorted(expected_union - actual))
            + " 多出="
            + str(sorted(actual - expected_union))
        )
    for bone in reference.data.bones:
        if bone.length <= 1e-7:
            raise PositioningError("参考骨长度为零：" + bone.name)
    if type(record.get("confirmed")) is not bool:
        raise PositioningError("模块化参考 confirmed 字段无效")
    return _deep(record), enabled, expected


def _pose_snapshot(reference) -> dict:
    rows = []
    for pose_bone in sorted(reference.pose.bones, key=lambda item: item.name):
        rows.append(
            {
                "name": pose_bone.name,
                "matrix_basis": _matrix_hex(pose_bone.matrix_basis),
                "location": _vec_hex(pose_bone.location),
                "rotation_mode": pose_bone.rotation_mode,
                "rotation_quaternion": _vec_hex(pose_bone.rotation_quaternion),
                "rotation_euler": _vec_hex(pose_bone.rotation_euler),
                "scale": _vec_hex(pose_bone.scale),
            }
        )
    return {
        "pose_position": reference.data.pose_position,
        "bones": rows,
    }


def _pose_is_identity(reference) -> tuple[bool, float]:
    maximum = 0.0
    for pose_bone in reference.pose.bones:
        matrix = pose_bone.matrix_basis
        for row in range(4):
            for column in range(4):
                maximum = max(
                    maximum,
                    abs(float(matrix[row][column]) - float(_IDENTITY[row][column])),
                )
    return maximum <= 1e-6, maximum


def _require_reference_pose(reference) -> dict:
    if reference.constraints:
        raise PositioningError("参考 Object 不允许附加约束")
    if reference.animation_data is not None:
        raise PositioningError("参考 Object 不允许动画/驱动器；请先清除姿态动画")
    if reference.data.animation_data is not None:
        raise PositioningError("参考 Armature 不允许动画/驱动器；请先清除姿态动画")
    for pose_bone in reference.pose.bones:
        if pose_bone.constraints:
            raise PositioningError("参考 PoseBone 不允许约束：" + pose_bone.name)
    identity, error = _pose_is_identity(reference)
    if not identity:
        raise PositioningError(
            "参考当前不是静止姿态（最大 matrix_basis 误差 %.9g）。"
            "请先清空姿态后再估计；本工具不承诺任意姿态识别。" % error
        )
    return _pose_snapshot(reference)


def _topology_snapshot(reference) -> dict:
    rows = []
    for bone in sorted(reference.data.bones, key=lambda item: item.name):
        collections = []
        try:
            collections = sorted(collection.name for collection in bone.collections)
        except (AttributeError, TypeError, RuntimeError):
            collections = []
        rows.append(
            {
                "name": bone.name,
                "parent": bone.parent.name if bone.parent else None,
                "use_connect": bool(bone.use_connect),
                "use_deform": bool(bone.use_deform),
                "collections": collections,
            }
        )
    return {"bones": rows, "sha256": _json_hex(rows)}


def _reference_identity(reference, record: dict) -> dict:
    return {
        "name": reference.name,
        "object_pointer": int(reference.as_pointer()),
        "data_name": reference.data.name,
        "data_pointer": int(reference.data.as_pointer()),
        "matrix_world": _matrix_hex(reference.matrix_world),
        "schema": record.get("schema"),
        "kind": record.get("kind"),
        "enabled_modules": list(
            record.get("configuration", {}).get("enabled_modules", [])
        ),
        "module_bones": _deep(record.get("module_bones")),
    }


def _mesh_array(mesh, attribute: str, count: int, dtype):
    array = np.empty(count, dtype=dtype)
    if count:
        mesh.foreach_get(attribute, array)
    return array


def _mesh_vertices(mesh) -> np.ndarray:
    count = len(mesh.vertices)
    flat = np.empty(count * 3, dtype=np.float32)
    if count:
        mesh.vertices.foreach_get("co", flat)
    return flat.reshape(-1, 3).astype(np.float64)


def _mesh_topology_hashes(mesh) -> dict:
    vertex_count = len(mesh.vertices)
    edge_count = len(mesh.edges)
    loop_count = len(mesh.loops)
    polygon_count = len(mesh.polygons)
    edges = np.empty(edge_count * 2, dtype=np.int32)
    loops = np.empty(loop_count, dtype=np.int32)
    loop_start = np.empty(polygon_count, dtype=np.int32)
    loop_total = np.empty(polygon_count, dtype=np.int32)
    if edge_count:
        mesh.edges.foreach_get("vertices", edges)
    if loop_count:
        mesh.loops.foreach_get("vertex_index", loops)
    if polygon_count:
        mesh.polygons.foreach_get("loop_start", loop_start)
        mesh.polygons.foreach_get("loop_total", loop_total)
    return {
        "vertex_count": vertex_count,
        "edge_count": edge_count,
        "loop_count": loop_count,
        "polygon_count": polygon_count,
        "edges_sha256": _array_digest(edges.reshape(-1, 2)),
        "loops_sha256": _array_digest(loops),
        "polygons_sha256": _array_digest(
            np.column_stack((loop_start, loop_total))
        ),
    }


def _uv_hashes(mesh) -> list[dict]:
    result = []
    for layer in sorted(mesh.uv_layers, key=lambda item: item.name):
        data = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        if len(mesh.loops):
            layer.data.foreach_get("uv", data)
        result.append(
            {
                "name": layer.name,
                "active_render": bool(layer.active_render),
                "sha256": _array_digest(data.reshape(-1, 2)),
            }
        )
    return result


def _vertex_group_hash(obj) -> dict:
    mesh = obj.data
    digest = hashlib.sha256()
    total = 0
    for vertex in mesh.vertices:
        entries = sorted(
            (int(item.group), float(item.weight)) for item in vertex.groups
        )
        total += len(entries)
        digest.update(struct.pack("<I", len(entries)))
        for group, weight in entries:
            digest.update(struct.pack("<If", group, weight))
    return {
        "groups": [group.name for group in obj.vertex_groups],
        "assignment_count": total,
        "weights_sha256": digest.hexdigest(),
    }


def _shape_key_hash(mesh) -> dict | None:
    keys = mesh.shape_keys
    if keys is None:
        return None
    blocks = []
    for block in keys.key_blocks:
        data = np.empty(len(block.data) * 3, dtype=np.float32)
        if len(block.data):
            block.data.foreach_get("co", data)
        blocks.append(
            {
                "name": block.name,
                "relative_key": block.relative_key.name if block.relative_key else None,
                "value": float(block.value),
                "sha256": _array_digest(data.reshape(-1, 3)),
            }
        )
    return {
        "reference_key": keys.reference_key.name if keys.reference_key else None,
        "blocks": blocks,
    }


def _modifier_snapshot(obj) -> list[dict]:
    rows = []
    for modifier in obj.modifiers:
        row = {
            "name": modifier.name,
            "type": modifier.type,
            "show_viewport": bool(modifier.show_viewport),
            "show_render": bool(modifier.show_render),
        }
        if modifier.type == "ARMATURE":
            armature = getattr(modifier, "object", None)
            row["object"] = armature.name if armature else None
            row["object_pointer"] = int(armature.as_pointer()) if armature else None
            row["pose"] = _pose_snapshot(armature) if armature else None
        rows.append(row)
    return rows


def _object_fingerprint(obj) -> dict:
    if not isinstance(obj, bpy.types.Object) or obj.type != "MESH":
        raise PositioningError("target_objects 只接受 MESH Object")
    mesh = obj.data
    if mesh is None:
        raise PositioningError("目标 Object 缺少 Mesh 数据：" + obj.name)
    vertices = _mesh_vertices(mesh)
    if len(vertices) < 4:
        raise PositioningError("目标 Mesh 少于 4 个顶点：" + obj.name)
    if not np.isfinite(vertices).all():
        raise PositioningError("目标 Mesh 含非有限顶点：" + obj.name)
    span = np.ptp(vertices, axis=0)
    if float(np.min(span)) <= 1e-9:
        raise PositioningError("目标 Mesh 没有三维范围：" + obj.name)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    world = vertices @ matrix[:3, :3].T + matrix[:3, 3]
    topology = _mesh_topology_hashes(mesh)
    return {
        "name": obj.name,
        "object_pointer": int(obj.as_pointer()),
        "data_name": mesh.name,
        "data_pointer": int(mesh.as_pointer()),
        "type": obj.type,
        "matrix_world": _matrix_hex(obj.matrix_world),
        "local_vertices_sha256": _array_digest(vertices),
        "world_vertices_sha256": _array_digest(world),
        "topology": topology,
        "uv_layers": _uv_hashes(mesh),
        "vertex_groups": _vertex_group_hash(obj),
        "shape_keys": _shape_key_hash(mesh),
        "materials": [
            slot.material.name if slot.material else None for slot in obj.material_slots
        ],
        "modifiers": _modifier_snapshot(obj),
        "modifier_count": len(obj.modifiers),
    }


def _fingerprint_differences(before: dict, after: dict, path="") -> list[str]:
    differences = []
    keys = sorted(set(before) | set(after))
    for key in keys:
        left, right = before.get(key), after.get(key)
        label = path + "." + key if path else key
        if isinstance(left, dict) and isinstance(right, dict):
            differences.extend(_fingerprint_differences(left, right, label))
        elif left != right:
            differences.append(label)
    return differences


def _normalize_targets(target_objects) -> list:
    if isinstance(target_objects, bpy.types.Object):
        objects = [target_objects]
    elif isinstance(target_objects, (list, tuple)):
        objects = list(target_objects)
    else:
        raise PositioningError("target_objects 必须是 MESH Object 或 Object 列表")
    if not objects:
        raise PositioningError("target_objects 不能为空")
    seen = set()
    result = []
    for obj in objects:
        if not isinstance(obj, bpy.types.Object) or obj.type != "MESH":
            raise PositioningError("target_objects 只接受 MESH Object")
        pointer = int(obj.as_pointer())
        if pointer in seen:
            raise PositioningError("target_objects 含重复对象：" + obj.name)
        seen.add(pointer)
        if bpy.context.scene.objects.get(obj.name) != obj:
            raise PositioningError("目标对象必须是当前 Scene 中的对象：" + obj.name)
        result.append(obj)
    return result


_LEFT_PAIR_PRIORITY = (
    ("thigh.L", "thigh.R"),
    ("pelvis.L", "pelvis.R"),
    ("shoulder.L", "shoulder.R"),
    ("upper_arm.L", "upper_arm.R"),
    ("foot.L", "foot.R"),
    ("hand.L", "hand.R"),
    ("f_toe.L", "f_toe.R"),
    ("r_toe.L", "r_toe.R"),
    ("toe.L", "toe.R"),
)


def _reference_semantic_axes(reference, up_world: Vector) -> dict:
    """Derive the reference's own left/forward/up in world space.

    No four-axis heuristic: anatomical left comes from the named `.L/.R`
    pair with the clearest horizontal separation, up is the requested world up,
    and forward is the orthogonal completion ``left x up``.  This works for
    any yaw of a Z-up reference and is the single source of the alignment
    rotation used for +X/-X/+Y/-Y requests.
    """
    up = Vector(up_world)
    if up.length <= _EPS:
        raise PositioningError("up_axis 退化")
    up.normalize()
    selected = None
    fallback = False

    def pair_vector(left_name, right_name):
        left_bone = reference.data.bones.get(left_name)
        right_bone = reference.data.bones.get(right_name)
        if left_bone is None or right_bone is None:
            return None
        left = (
            reference.matrix_world @ left_bone.head_local
            + reference.matrix_world @ left_bone.tail_local
        ) * 0.5
        right = (
            reference.matrix_world @ right_bone.head_local
            + reference.matrix_world @ right_bone.tail_local
        ) * 0.5
        vector = left - right
        vector = vector - up * vector.dot(up)
        if vector.length <= _EPS:
            return None
        return vector

    for left_name, right_name in _LEFT_PAIR_PRIORITY:
        vector = pair_vector(left_name, right_name)
        if vector is not None:
            selected = (left_name, right_name, vector)
            break
    if selected is None:
        # Last resort for a reference with no side-named bones: its local X is
        # the conventional left axis, but this is explicitly reported.
        local_x = reference.matrix_world.to_3x3() @ Vector((1.0, 0.0, 0.0))
        local_x = local_x - up * local_x.dot(up)
        if local_x.length <= _EPS:
            raise PositioningError("参考没有可用的左右骨或水平 X 轴，无法推导向前")
        selected = (None, None, local_x)
        fallback = True
    left_world = selected[2].normalized()
    forward_world = left_world.cross(up)
    if forward_world.length <= _EPS:
        raise PositioningError("参考左右/向上方向无法推导水平向前")
    forward_world.normalize()
    return {
        "left": left_world,
        "forward": forward_world,
        "up": up,
        "pair": [selected[0], selected[1]] if selected[0] else None,
        "fallback_local_x": fallback,
        "method": "named_LR_pair_x_up_orthogonal_completion",
    }


def _signed_z_angle(from_vector: Vector, to_vector: Vector) -> float:
    a = Vector((from_vector.x, from_vector.y, 0.0))
    b = Vector((to_vector.x, to_vector.y, 0.0))
    if a.length <= _EPS or b.length <= _EPS:
        raise PositioningError("前向轴退化，无法计算参考对齐角度")
    a.normalize()
    b.normalize()
    cross_z = float(a.x * b.y - a.y * b.x)
    dot = float(a.dot(b))
    return math.atan2(cross_z, dot)


def _compute_reference_alignment(
    reference,
    record: dict,
    target_forward_world: Vector,
    up_world: Vector,
) -> tuple[dict, Matrix, Matrix]:
    """Compute (but do not commit) a yaw alignment for a fresh reference.

    The production target is never rotated.  Existing confirmed/positioning-
    edited references are never silently moved; if they are not aligned the
    caller gets a clear error and should start from a fresh reference.
    The candidate matrix is temporarily applied only to verify the semantic
    axes, then the original matrix is restored.  The caller commits the
    returned aligned matrix only after the whole candidate is validated.
    """
    before = reference.matrix_world.copy()
    semantic = _reference_semantic_axes(reference, up_world)
    forward_before = semantic["forward"]
    target = Vector(target_forward_world)
    target = target - semantic["up"] * target.dot(semantic["up"])
    if target.length <= _EPS:
        raise PositioningError("请求前向与 up_axis 不正交")
    target.normalize()
    angle = _signed_z_angle(forward_before, target)
    info = {
        "applied": False,
        "angle_deg": math.degrees(angle),
        "target_forward_world": _vec(target),
        "reference_forward_world_before": _vec(forward_before),
        "reference_forward_world_after": _vec(forward_before),
        "reference_up_world": _vec(semantic["up"]),
        "left_pair": semantic["pair"],
        "fallback_local_x": semantic["fallback_local_x"],
        "method": "single_yaw_from_named_LR_x_up_to_requested_forward",
        "target_mesh_transform_modified": False,
    }
    if abs(angle) <= 1e-7:
        return info, before, before
    fresh = (
        not bool(record.get("confirmed"))
        and reference.get(POSITIONING_KEY) is None
    )
    if not fresh:
        raise PositioningError(
            "参考已有确认或定位编辑，且朝向未对齐请求的 ±X/±Y。"
            "为避免意外移动已编辑参考，请用新的模块参考重试；生产目标网格无需旋转。"
        )
    location = reference.matrix_world.translation.copy()
    rotation = Matrix.Rotation(angle, 4, "Z")
    candidate = (
        Matrix.Translation(location)
        @ rotation
        @ Matrix.Translation(-location)
        @ reference.matrix_world
    )
    reference.matrix_world = candidate
    try:
        bpy.context.view_layer.update()
        after = _reference_semantic_axes(reference, up_world)
        alignment = float(after["forward"].dot(target))
        target_left = semantic["up"].cross(target)
        alignment_left = float(after["left"].dot(target_left))
        if alignment < 0.999 or alignment_left < 0.999:
            raise PositioningError(
                "参考朝向自动对齐后仍未对齐请求前向/左向"
                "（forward=%.6f, left=%.6f）" % (alignment, alignment_left)
            )
    except Exception:
        reference.matrix_world = before
        try:
            bpy.context.view_layer.update()
        except RuntimeError:
            pass
        raise
    reference.matrix_world = before
    try:
        bpy.context.view_layer.update()
    except RuntimeError:
        pass
    info.update(
        {
            "applied": True,
            "angle_deg": math.degrees(angle),
            "reference_forward_world_after": _vec(after["forward"]),
            "alignment_dot": alignment,
            "alignment_left_dot": alignment_left,
            "reference_matrix_before": _matrix_hex(before),
            "reference_matrix_after": _matrix_hex(candidate),
        }
    )
    return info, before, candidate


def _frame_from_vectors(
    reference,
    forward_world: Vector,
    up_world: Vector,
    *,
    forward_name: str | None = None,
    up_name: str | None = None,
    inverse: Matrix | None = None,
) -> dict:
    if inverse is None:
        inverse = reference.matrix_world.inverted()
    rotation = inverse.to_3x3()
    up = (rotation @ up_world)
    if up.length <= _EPS:
        raise PositioningError("参考变换退化了 up_axis")
    up.normalize()
    forward = rotation @ forward_world
    forward = forward - up * forward.dot(up)
    if forward.length <= _EPS:
        raise PositioningError("参考变换退化了 forward_axis")
    forward.normalize()
    left = up.cross(forward)
    if left.length <= _EPS:
        raise PositioningError("参考变换无法建立左右轴")
    left.normalize()
    return {
        "forward_name": forward_name,
        "up_name": up_name,
        "forward": forward,
        "up": up,
        "left": left,
        "matrix_world_inverse": inverse,
    }


def _axis_frame(reference, forward_axis: str, up_axis: str) -> dict:
    forward_name, forward_world = _normalize_axis(forward_axis, name="forward_axis")
    up_name, up_world = _normalize_axis(up_axis, name="up_axis")
    return _frame_from_vectors(
        reference,
        forward_world,
        up_world,
        forward_name=forward_name,
        up_name=up_name,
    )


def _project(local_points: np.ndarray, frame: dict) -> np.ndarray:
    left = np.array(frame["left"], dtype=np.float64)
    forward = np.array(frame["forward"], dtype=np.float64)
    up = np.array(frame["up"], dtype=np.float64)
    return np.column_stack(
        (
            local_points @ left,
            local_points @ forward,
            local_points @ up,
        )
    )


def _unproject(coords: np.ndarray, frame: dict) -> np.ndarray:
    coords = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
    left = np.array(frame["left"], dtype=np.float64)
    forward = np.array(frame["forward"], dtype=np.float64)
    up = np.array(frame["up"], dtype=np.float64)
    return (
        coords[:, 0:1] * left
        + coords[:, 1:2] * forward
        + coords[:, 2:3] * up
    )


def _local_point(a: float, f: float, c: float, frame: dict) -> Vector:
    local = (
        float(a) * frame["left"]
        + float(f) * frame["forward"]
        + float(c) * frame["up"]
    )
    return Vector(local)


def _coords_row(a: float, f: float, c: float) -> np.ndarray:
    return np.array([float(a), float(f), float(c)], dtype=np.float64)


def _median(values, default=0.0) -> float:
    array = np.asarray(values, dtype=np.float64)
    if not len(array):
        return float(default)
    return float(np.median(array))


def _clusters_1d(values, gap: float) -> list[np.ndarray]:
    values = np.asarray(values, dtype=np.float64)
    if not len(values):
        return []
    order = np.argsort(values)
    sorted_values = values[order]
    split = np.where(np.diff(sorted_values) > max(float(gap), _EPS))[0] + 1
    return [values[order[start:stop]] for start, stop in zip(
        np.r_[0, split], np.r_[split, len(values)]
    )]


def _central_cluster(values, center: float, gap: float):
    clusters = _clusters_1d(values, gap)
    if not clusters:
        return None
    containing = [
        cluster
        for cluster in clusters
        if float(np.min(cluster)) <= center <= float(np.max(cluster))
    ]
    if containing:
        return max(containing, key=len)
    return min(
        clusters,
        key=lambda cluster: abs(float(np.median(cluster)) - center),
    )


def _section_metrics(
    coords: np.ndarray,
    level: float,
    half: float,
    center_a: float,
    *,
    require_center: bool = False,
    gap: float | None = None,
):
    mask = np.abs(coords[:, 2] - level) <= half
    points = coords[mask]
    if len(points) < 6:
        return None
    span = float(np.ptp(coords[:, 2]))
    if gap is None:
        gap = max(span * 0.012, 1e-5)
    gap = float(gap)
    cluster = _central_cluster(points[:, 0], center_a, gap)
    if cluster is None or len(cluster) < 4:
        return None
    if require_center and not (
        float(np.min(cluster)) - gap <= center_a <= float(np.max(cluster)) + gap
    ):
        return None
    lo, hi = float(np.min(cluster)), float(np.max(cluster))
    if hi - lo <= 1e-7:
        return None
    selected = points[
        (points[:, 0] >= lo - 1e-7) & (points[:, 0] <= hi + 1e-7)
    ]
    if len(selected) < 4:
        return None
    return {
        "level": float(level),
        "half": float(half),
        "count": int(len(selected)),
        "a_min": lo,
        "a_max": hi,
        "width": hi - lo,
        "center_a": float((lo + hi) * 0.5),
        "center_f": _median(selected[:, 1]),
        "f_min": float(np.min(selected[:, 1])),
        "f_max": float(np.max(selected[:, 1])),
        "c_min": float(np.min(selected[:, 2])),
        "c_max": float(np.max(selected[:, 2])),
    }


def _section_metrics_at_f(coords: np.ndarray, level: float, half: float, center_a: float):
    mask = np.abs(coords[:, 1] - level) <= half
    points = coords[mask]
    if len(points) < 6:
        return None
    width = float(np.ptp(coords[:, 0]))
    band = max(width * 0.30, 1e-5)
    central = points[np.abs(points[:, 0] - center_a) <= band]
    if len(central) < 4:
        central = points
    return {
        "level": float(level),
        "half": float(half),
        "count": int(len(central)),
        "center_a": _median(central[:, 0]),
        "center_c": _median(central[:, 2]),
        "c_min": float(np.min(central[:, 2])),
        "c_max": float(np.max(central[:, 2])),
        "a_min": float(np.min(central[:, 0])),
        "a_max": float(np.max(central[:, 0])),
        "width": float(np.ptp(central[:, 0])),
        "height": float(np.ptp(central[:, 2])),
    }


def _longitudinal_body_range(coords: np.ndarray, bins: int = 36):
    if not len(coords):
        return None
    f_min, f_max = float(np.min(coords[:, 1])), float(np.max(coords[:, 1]))
    if f_max - f_min <= 1e-9:
        return None
    edges = np.linspace(f_min, f_max, bins + 1)
    index = np.clip(np.searchsorted(edges, coords[:, 1], side="right") - 1, 0, bins - 1)
    counts = np.bincount(index, minlength=bins)
    widths = np.zeros(bins, dtype=np.float64)
    heights = np.zeros(bins, dtype=np.float64)
    for number in range(bins):
        points = coords[index == number]
        if len(points):
            widths[number] = float(np.ptp(points[:, 0]))
            heights[number] = float(np.ptp(points[:, 2]))
    max_count = float(np.max(counts)) if len(counts) else 0.0
    max_width = float(np.max(widths)) if len(widths) else 0.0
    max_height = float(np.max(heights)) if len(heights) else 0.0
    if max_count <= 0:
        return None
    solid = (
        (counts >= max(3.0, max_count * 0.06))
        & (
            (widths >= max_width * 0.22)
            | (heights >= max_height * 0.38)
        )
    )
    # Fill one-bin gaps between solid bins so a thin neck/snout does not split
    # the body range, while a long thin tail remains outside the main run.
    indices = np.where(solid)[0]
    if not len(indices):
        return [f_min, f_max, "fallback_full_range"]
    groups = []
    start = previous = int(indices[0])
    for number in indices[1:]:
        number = int(number)
        if number - previous <= 2:
            previous = number
        else:
            groups.append((start, previous))
            start = previous = number
    groups.append((start, previous))
    center = float(np.median(coords[:, 1]))
    containing = [
        group for group in groups if edges[group[0]] <= center <= edges[group[1] + 1]
    ]
    chosen = max(containing or groups, key=lambda group: group[1] - group[0])
    lo = float(edges[chosen[0]])
    hi = float(edges[chosen[1] + 1])
    if hi - lo < (f_max - f_min) * 0.25:
        return [f_min, f_max, "fallback_short_body_range"]
    return [lo, hi, "surface_longitudinal_bins"]


def _reference_bone_frame(reference, frame: dict) -> tuple[dict, dict]:
    result = {}
    for bone in reference.data.bones:
        head = _project(
            np.array([bone.head_local], dtype=np.float64), frame
        )[0]
        tail = _project(
            np.array([bone.tail_local], dtype=np.float64), frame
        )[0]
        result[bone.name] = {
            "head": head,
            "tail": tail,
            "length": float(bone.length),
            "parent": bone.parent.name if bone.parent else None,
            "use_connect": bool(bone.use_connect),
        }
    left_values = []
    right_values = []
    for name, row in result.items():
        if name.endswith(".L"):
            left_values.extend([row["head"][0], row["tail"][0]])
        elif name.endswith(".R"):
            right_values.extend([row["head"][0], row["tail"][0]])
    left_sign = 1.0
    if left_values and right_values:
        difference = _median(left_values) - _median(right_values)
        if abs(difference) > 1e-9:
            left_sign = 1.0 if difference > 0 else -1.0
    forward_sign = 1.0
    rear = result.get("spine", {}).get("head")
    front_name = "spine.006" if "spine.006" in result else "spine.003"
    front = result.get(front_name, {}).get("tail")
    if rear is not None and front is not None:
        difference = float(front[1] - rear[1])
        if abs(difference) > 1e-9:
            forward_sign = 1.0 if difference > 0 else -1.0
    if left_sign < 0:
        for row in result.values():
            row["head"][0] *= -1.0
            row["tail"][0] *= -1.0
    if forward_sign < 0:
        for row in result.values():
            row["head"][1] *= -1.0
            row["tail"][1] *= -1.0
    return result, {
        "semantic_left_flipped": bool(left_sign < 0),
        "semantic_forward_flipped": bool(forward_sign < 0),
        "method": "normalize reference rest seeds to the requested anatomical left/front frame",
    }


def _reference_bounds(reference_bones: dict, kind: str) -> dict:
    points = []
    for name, row in reference_bones.items():
        if kind == "QUADRUPED" and name.startswith("tail."):
            continue
        points.append(row["head"])
        points.append(row["tail"])
    if not points:
        raise PositioningError("参考没有可用于比例初值的骨点")
    array = np.asarray(points, dtype=np.float64)
    return {
        "min": np.min(array, axis=0),
        "max": np.max(array, axis=0),
    }


class _Mapper:
    def __init__(self, source_min, source_max, target_min, target_max):
        self.source_min = np.asarray(source_min, dtype=np.float64)
        self.source_max = np.asarray(source_max, dtype=np.float64)
        self.target_min = np.asarray(target_min, dtype=np.float64)
        self.target_max = np.asarray(target_max, dtype=np.float64)
        self.source_span = self.source_max - self.source_min
        self.target_span = self.target_max - self.target_min
        if np.any(self.source_span <= 1e-9):
            raise PositioningError("参考比例初值范围退化")

    def __call__(self, point):
        point = np.asarray(point, dtype=np.float64)
        unit = (point - self.source_min) / self.source_span
        return self.target_min + unit * self.target_span

    def as_dict(self) -> dict:
        return {
            "source_bounds": [self.source_min.tolist(), self.source_max.tolist()],
            "target_bounds": [self.target_min.tolist(), self.target_max.tolist()],
            "method": "reference_rest_proportions_mapped_to_target_bounds",
        }


def _mapped_bone(mapper: _Mapper, reference_bones: dict, name: str) -> dict:
    row = reference_bones[name]
    return {
        "name": name,
        "head": mapper(row["head"]),
        "tail": mapper(row["tail"]),
        "length": float(row["length"]),
        "parent": row["parent"],
        "use_connect": row["use_connect"],
    }


def _chain_length(reference_bones: dict, names: list[str]) -> float:
    total = 0.0
    for name in names:
        total += float(reference_bones[name]["length"])
    return total


def _read_state(reference) -> dict:
    raw = reference.get(POSITIONING_KEY)
    if raw is None:
        raise PositioningError("参考尚无自动定位候选；请先调用 estimate")
    if not isinstance(raw, str):
        raise PositioningError("定位状态不是 JSON 字符串")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PositioningError("定位状态 JSON 损坏") from error
    if not isinstance(state, dict) or state.get("schema") != POSITIONING_SCHEMA:
        raise PositioningError("定位状态 schema 无效")
    if state.get("version") != 1:
        raise PositioningError("定位状态版本无效")
    if state.get("status") != "proposed_needs_review":
        raise PositioningError("定位状态不是 proposed_needs_review")
    return state


def _write_state(reference, state: dict) -> None:
    state["updated_at"] = _now()
    clean = _jsonable(state)
    raw = _canonical(clean)
    state.clear()
    state.update(clean)
    reference[POSITIONING_KEY] = raw


def _candidate_rest(reference) -> dict:
    rows = {}
    for bone in sorted(reference.data.bones, key=lambda item: item.name):
        rows[bone.name] = {
            "head": _vec_hex(bone.head_local),
            "tail": _vec_hex(bone.tail_local),
        }
    return rows


def _candidate_rest_sha(rows: dict) -> str:
    return _json_hex(rows)


def _configuration_record(record):
    return record.get("configuration", {})


def _enabled_modules(record) -> list[str]:
    return list(_normalize_modules(_configuration_record(record).get("enabled_modules")))


def _polyline_distance(points: np.ndarray, polyline: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    polyline = np.asarray(polyline, dtype=np.float64)
    if len(points) == 0:
        return np.zeros(0, dtype=np.float64)
    if len(polyline) == 0:
        return np.full(len(points), np.inf, dtype=np.float64)
    if len(polyline) == 1:
        return np.linalg.norm(points - polyline[0], axis=1)
    best = np.full(len(points), np.inf, dtype=np.float64)
    for start, end in zip(polyline[:-1], polyline[1:]):
        segment = end - start
        length2 = float(np.dot(segment, segment))
        if length2 <= _EPS:
            distance = np.linalg.norm(points - start, axis=1)
        else:
            unit = np.clip(((points - start) @ segment) / length2, 0.0, 1.0)
            closest = start + unit[:, None] * segment
            distance = np.linalg.norm(points - closest, axis=1)
        best = np.minimum(best, distance)
    return best


def _extended_polyline(polyline: np.ndarray, start: float, end: float) -> np.ndarray:
    polyline = np.asarray(polyline, dtype=np.float64)
    if len(polyline) < 2:
        return polyline.copy()
    points = []
    first = polyline[0] - (polyline[1] - polyline[0]) * max(0.0, float(start))
    last_vector = polyline[-1] - polyline[-2]
    last = polyline[-1] + last_vector * max(0.0, float(end))
    points.append(first)
    points.extend(polyline)
    points.append(last)
    return np.asarray(points, dtype=np.float64)


def _extended_polyline_absolute(polyline: np.ndarray, start: float, end: float) -> np.ndarray:
    polyline = np.asarray(polyline, dtype=np.float64)
    if len(polyline) < 2:
        return polyline.copy()
    points = []
    first_direction = polyline[0] - polyline[1]
    first_length = float(np.linalg.norm(first_direction))
    if first_length > _EPS:
        points.append(polyline[0] + first_direction / first_length * float(start))
    points.extend(polyline)
    last_direction = polyline[-1] - polyline[-2]
    last_length = float(np.linalg.norm(last_direction))
    if last_length > _EPS:
        points.append(polyline[-1] + last_direction / last_length * float(end))
    return np.asarray(points, dtype=np.float64)


def _fit_centerline(
    points: np.ndarray,
    chord_start: np.ndarray,
    chord_end: np.ndarray,
    *,
    bins: int = 18,
    min_points: int = 16,
    min_bins: int = 4,
    min_bin_points: int = 2,
):
    """PCA-guided curved section centerline of one isolated limb cloud."""
    points = np.asarray(points, dtype=np.float64)
    if len(points) < min_points or not np.isfinite(points).all():
        return None
    origin = np.median(points, axis=0)
    centered = points - origin
    try:
        _, _, vectors = np.linalg.svd(centered, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    axis = vectors[0]
    hint = np.asarray(chord_end, dtype=np.float64) - np.asarray(
        chord_start, dtype=np.float64
    )
    if float(np.linalg.norm(hint)) <= _EPS:
        return None
    hint = hint / float(np.linalg.norm(hint))
    if float(np.dot(axis, hint)) < 0.0:
        axis = -axis
    projection = centered @ axis
    if float(np.ptp(projection)) <= _EPS:
        return None
    low, high = float(np.min(projection)), float(np.max(projection))
    edges = np.linspace(low, high, bins + 1)
    index = np.clip(
        np.searchsorted(edges, projection, side="right") - 1, 0, bins - 1
    )
    rows = []
    for number in range(bins):
        selected = points[index == number]
        if len(selected) < min_bin_points:
            continue
        center = np.median(selected, axis=0)
        rows.append(
            {
                "t": float(np.median(projection[index == number])),
                "point": center,
                "count": int(len(selected)),
            }
        )
    if len(rows) < min_bins:
        return None
    rows.sort(key=lambda row: row["t"])
    # Fill at most small gaps by linear interpolation between real sections.
    filled = [rows[0]]
    for previous, current in zip(rows[:-1], rows[1:]):
        step = current["t"] - previous["t"]
        if step > (high - low) / bins * 2.5:
            interpolated = (previous["point"] + current["point"]) * 0.5
            filled.append(
                {
                    "t": (previous["t"] + current["t"]) * 0.5,
                    "point": interpolated,
                    "count": 0,
                }
            )
        filled.append(current)
    centerline = np.asarray([row["point"] for row in filled], dtype=np.float64)
    counts = np.asarray([row["count"] for row in filled], dtype=np.int64)
    lengths = np.linalg.norm(np.diff(centerline, axis=0), axis=1)
    cumulative = np.r_[0.0, np.cumsum(lengths)]
    total = float(cumulative[-1])
    if not np.isfinite(total) or total <= _EPS:
        return None
    return {
        "points": centerline,
        "counts": counts,
        "cumulative": cumulative,
        "length": total,
        "axis": axis,
        "support_points": int(sum(row["count"] for row in rows)),
        "filled_bins": int(len(rows)),
        "method": "pca_surface_section_centerline",
    }


def _centerline_point(centerline: dict, fraction: float) -> np.ndarray:
    fraction = float(np.clip(fraction, 0.0, 1.0))
    target = centerline["cumulative"][-1] * fraction
    cumulative = centerline["cumulative"]
    points = centerline["points"]
    if target <= 0.0:
        return points[0].copy()
    if target >= cumulative[-1]:
        return points[-1].copy()
    index = int(np.searchsorted(cumulative, target, side="right") - 1)
    index = max(0, min(index, len(points) - 2))
    span = cumulative[index + 1] - cumulative[index]
    if span <= _EPS:
        return points[index].copy()
    unit = (target - cumulative[index]) / span
    return points[index] * (1.0 - unit) + points[index + 1] * unit


def _centerline_at_c(centerline: dict, level: float):
    points = centerline["points"]
    for index in range(len(points) - 1):
        low, high = points[index][2], points[index + 1][2]
        if (low - level) * (high - level) <= 0.0 and abs(high - low) > _EPS:
            unit = (level - low) / (high - low)
            return points[index] * (1.0 - unit) + points[index + 1] * unit
    nearest = int(np.argmin(np.abs(points[:, 2] - level)))
    return points[nearest].copy()


def _reference_fraction(reference_bounds: dict, section: dict, endpoint: str) -> float:
    low, high = reference_bounds["min"][2], reference_bounds["max"][2]
    if high - low <= _EPS:
        return 0.5
    return float((section[endpoint][2] - low) / (high - low))


def _section_level(target_c_min: float, height: float, fraction: float) -> float:
    return float(target_c_min + float(fraction) * height)


def _best_section(
    coords: np.ndarray,
    level: float,
    half: float,
    center_a: float,
    *,
    widen: float = 2.0,
    require_center: bool = False,
    gap: float | None = None,
):
    section = _section_metrics(
        coords, level, half, center_a, require_center=require_center, gap=gap
    )
    if section is not None:
        return section
    return _section_metrics(
        coords,
        level,
        half * widen,
        center_a,
        require_center=require_center,
        gap=gap,
    )


def _bone_length_fractions(reference_bones: dict, names: list[str]) -> list[float]:
    lengths = np.asarray([reference_bones[name]["length"] for name in names])
    total = float(np.sum(lengths))
    if total <= _EPS:
        raise PositioningError("参考肢体骨长度退化")
    return (np.cumsum(lengths) / total).tolist()


def _nearest_polyline_owner(
    points: np.ndarray,
    first: np.ndarray,
    second: np.ndarray,
) -> np.ndarray:
    """Return 0/1 by nearest polyline; geometry only, seeds are neutral."""
    first_distance = _polyline_distance(points, first)
    second_distance = _polyline_distance(points, second)
    return (second_distance < first_distance).astype(np.int8)


def _paw_cluster_2means(values, iterations: int = 24):
    values = np.asarray(values, dtype=np.float64)
    if len(values) < 2:
        return None, None
    low = float(np.percentile(values, 25))
    high = float(np.percentile(values, 75))
    if abs(high - low) <= 1e-9:
        return None, None
    centers = np.array([low, high], dtype=np.float64)
    labels = np.zeros(len(values), dtype=np.int8)
    for _ in range(iterations):
        distance = np.abs(values[:, None] - centers[None, :])
        labels = np.argmin(distance, axis=1).astype(np.int8)
        updated = centers.copy()
        for label in (0, 1):
            selected = values[labels == label]
            if len(selected):
                updated[label] = float(np.mean(selected))
        if np.max(np.abs(updated - centers)) <= 1e-7:
            centers = updated
            break
        centers = updated
    first = values[labels == 0]
    second = values[labels == 1]
    if not len(first) or not len(second):
        return None, None
    return (first, second) if float(np.mean(first)) <= float(np.mean(second)) else (
        second,
        first,
    )


def _bone_position_rows(
    reference,
    positions: dict,
    bone_reports: dict,
) -> dict:
    result = {}
    for name, row in positions.items():
        bone = reference.data.bones.get(name)
        if bone is None:
            continue
        result[name] = {
            "head_local": _vec(bone.head_local),
            "tail_local": _vec(bone.tail_local),
            "head_world": _vec(reference.matrix_world @ bone.head_local),
            "tail_world": _vec(reference.matrix_world @ bone.tail_local),
        }
    return result


def _plan_row(method: str, *, seed=None, actual=None, status="proposed_needs_review", **extra):
    row = {
        "status": status,
        "method": method,
        "geometry_basis": (
            "actual_surface_section"
            if method.startswith("surface") or "centerline" in method or "extremes" in method
            else "proportion_or_interpolation"
        ),
    }
    if seed is not None:
        row["seed"] = seed
    if actual is not None:
        row["actual"] = actual
    row.update(extra)
    return row


def _add_position(
    positions,
    bone_reports,
    name: str,
    *,
    head=None,
    tail=None,
    method: str,
    chain: str,
    side: str,
    seed=None,
    actual=None,
    **extra,
):
    positions[name] = {
        "head": None if head is None else np.asarray(head, dtype=np.float64),
        "tail": None if tail is None else np.asarray(tail, dtype=np.float64),
    }
    row = _plan_row(method, seed=seed, actual=actual, **extra)
    row.update({"chain": chain, "side": side})
    bone_reports[name] = row


def _mark_unestimated(bone_reports, name: str, chain: str, side: str, reason: str, **extra):
    row = {
        "status": "unestimated",
        "method": "unestimated",
        "geometry_basis": "none",
        "chain": chain,
        "side": side,
        "reason": reason,
    }
    row.update(extra)
    bone_reports[name] = row


def _bounds_from_coords(coords: np.ndarray) -> dict:
    low = np.min(coords, axis=0)
    high = np.max(coords, axis=0)
    span = high - low
    return {
        "min": low,
        "max": high,
        "span": span,
        "center": (low + high) * 0.5,
        "height": float(span[2]),
    }


def _human_torso_range(
    coords: np.ndarray,
    target_bounds: dict,
    center_a: float,
):
    height = target_bounds["height"]
    valid = []
    # Human chest/pelvis live below the neck; scanning into the head would
    # mistake the skull/neck for a torso run.
    for fraction in np.linspace(0.38, 0.84, 24):
        level = target_bounds["min"][2] + float(fraction) * height
        section = _best_section(
            coords,
            level,
            height * 0.018,
            center_a,
            widen=1.6,
            require_center=True,
            gap=height * 0.045,
        )
        if section is None:
            continue
        if section["count"] < 6:
            continue
        if not (height * 0.02 <= section["width"] <= height * 0.55):
            continue
        valid.append((float(fraction), section))
    if not valid:
        return None
    valid.sort(key=lambda item: item[0])
    groups = []
    previous = valid[0][0]
    current = [valid[0]]
    for fraction, section in valid[1:]:
        if fraction - previous <= 0.035:
            current.append((fraction, section))
            previous = fraction
        else:
            groups.append(current)
            current = [(fraction, section)]
            previous = fraction
    groups.append(current)
    center = 0.70
    containing = [
        group
        for group in groups
        if group[0][0] <= center <= group[-1][0]
    ]
    chosen = max(containing or groups, key=len)
    return {
        "status": "actual_longitudinal_torso_run",
        "fraction_min": float(chosen[0][0]),
        "fraction_max": float(chosen[-1][0]),
        "sections": [section for _, section in chosen],
    }


def _human_trunk(
    coords: np.ndarray,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
) -> dict:
    height = target_bounds["height"]
    center_a = float(target_bounds["center"][0])
    # First pass: median trunk center from sections that visibly contain the
    # body axis.
    probe_centers = []
    for fraction in np.linspace(0.55, 0.86, 8):
        level = target_bounds["min"][2] + float(fraction) * height
        section = _section_metrics(
            coords,
            level,
            height * 0.018,
            center_a,
            require_center=True,
            gap=height * 0.045,
        )
        if section is not None and section["width"] <= height * 0.55:
            probe_centers.append(section["center_a"])
    if probe_centers:
        candidate = _median(probe_centers, center_a)
        if abs(candidate - float(target_bounds["center"][0])) <= float(
            target_bounds["span"][0]
        ) * 0.06:
            center_a = candidate

    spine_head = reference_bones.get("spine")
    chest_bone = reference_bones.get("spine.003")
    if spine_head is None or chest_bone is None:
        raise PositioningError("参考缺少 spine/spine.003，无法建立躯干分区")
    pelvis_fraction = _reference_fraction(reference_bounds, spine_head, "head")
    chest_fraction = _reference_fraction(reference_bounds, chest_bone, "head")
    torso_range = _human_torso_range(coords, target_bounds, center_a)
    effective_pelvis_fraction = float(pelvis_fraction)
    effective_chest_fraction = float(chest_fraction)
    if torso_range is not None:
        low = float(torso_range["fraction_min"])
        high = float(torso_range["fraction_max"])
        effective_pelvis_fraction = min(
            max(float(pelvis_fraction), low + 0.02), low + 0.12
        )
        effective_chest_fraction = min(
            max(float(chest_fraction), high - 0.14), high - 0.02
        )
        if effective_chest_fraction <= effective_pelvis_fraction + 0.10:
            effective_chest_fraction = min(high - 0.02, effective_pelvis_fraction + 0.20)

    def widths(fraction_low, fraction_high, label):
        sections = []
        rejected = []
        for fraction in np.linspace(fraction_low, fraction_high, 5):
            level = target_bounds["min"][2] + float(fraction) * height
            section = _best_section(
                coords,
                level,
                height * 0.018,
                center_a,
                widen=1.8,
                require_center=True,
                gap=height * 0.045,
            )
            if section is None:
                rejected.append(
                    {"fraction": float(fraction), "reason": "no_central_section"}
                )
                continue
            # A section that spans almost the full model is an arm/pose
            # contamination, not a central trunk width.
            if section["width"] > height * 0.55:
                rejected.append(
                    {
                        "fraction": float(fraction),
                        "reason": "width_exceeds_central_trunk_limit",
                        "width": float(section["width"]),
                    }
                )
                continue
            sections.append(section)
        if not sections:
            return {
                "label": label,
                "status": "unestimated",
                "width": None,
                "center_a": None,
                "sections": [],
                "rejected": rejected,
            }
        return {
            "label": label,
            "status": "actual_section_estimate",
            "width": _median([row["width"] for row in sections]),
            "center_a": _median([row["center_a"] for row in sections]),
            "center_f": _median([row["center_f"] for row in sections]),
            "level_min": float(min(row["level"] for row in sections)),
            "level_max": float(max(row["level"] for row in sections)),
            "sample_points": int(sum(row["count"] for row in sections)),
            "sections": sections,
            "rejected": rejected,
        }

    pelvis = widths(
        max(0.20, effective_pelvis_fraction - 0.020),
        max(0.22, effective_pelvis_fraction + 0.020),
        "pelvis_width",
    )
    chest = widths(
        max(0.30, effective_chest_fraction - 0.025),
        max(0.32, effective_chest_fraction + 0.020),
        "chest_width",
    )
    valid = [row for row in (chest, pelvis) if row["width"] is not None]
    if valid:
        candidate = _median([row["center_a"] for row in valid], center_a)
        if abs(candidate - float(target_bounds["center"][0])) <= float(
            target_bounds["span"][0]
        ) * 0.06:
            center_a = candidate
        center_f = _median([row["center_f"] for row in valid], float(target_bounds["center"][1]))
        torso_half = 0.5 * _median([row["width"] for row in valid])
        width_source = (
            "actual_surface_cross_sections"
            if chest["width"] is not None and pelvis["width"] is not None
            else "actual_surface_cross_sections_partial"
        )
    else:
        center_f = float(target_bounds["center"][1])
        torso_half = float(target_bounds["span"][0]) * 0.20
        width_source = "bbox_fallback_width_not_an_estimate"
    neck_bone = reference_bones.get("spine.005")
    if neck_bone is not None:
        head_bottom_fraction = _reference_fraction(reference_bounds, neck_bone, "head")
    else:
        head_bottom_fraction = 0.86
    return {
        "center_a": float(center_a),
        "center_f": float(center_f),
        "torso_half_width": float(torso_half),
        "width_source": width_source,
        "chest": chest,
        "pelvis": pelvis,
        "torso_range": torso_range,
        "effective_pelvis_fraction": float(effective_pelvis_fraction),
        "effective_chest_fraction": float(effective_chest_fraction),
        "pelvis_fraction": float(pelvis_fraction),
        "chest_fraction": float(chest_fraction),
        "head_bottom_fraction": float(head_bottom_fraction),
        "head_bottom_c": float(target_bounds["min"][2] + head_bottom_fraction * height),
    }


def _human_arm_candidates(
    coords: np.ndarray,
    *,
    side: int,
    center_a: float,
    torso_half: float,
    head_bottom_c: float,
    seed_polyline: np.ndarray,
    target_bounds: dict,
) -> tuple[np.ndarray, dict]:
    height = target_bounds["height"]
    side_mask = side * (coords[:, 0] - center_a) > height * 0.015
    outside = side * (coords[:, 0] - center_a) > torso_half * 1.0
    upper = min(head_bottom_c, target_bounds["max"][2] + height * 0.02)
    lower = target_bounds["min"][2] + height * 0.22
    c_mask = (coords[:, 2] >= lower) & (coords[:, 2] <= upper)
    extended = _extended_polyline_absolute(
        seed_polyline, height * 0.05, height * 0.35
    )
    distance = _polyline_distance(coords, extended)
    radius = max(height * 0.13, torso_half * 1.15)
    near_seed = distance <= radius
    # A horizontal T-pose arm has most of its surface at the shoulder height.
    shoulder_c = float(seed_polyline[0][2])
    shoulder_band = np.abs(coords[:, 2] - shoulder_c) <= height * 0.10
    mask = side_mask & outside & c_mask & (near_seed | shoulder_band)
    selected = coords[mask]
    diagnostics = {
        "selection": "actual_surface outside central torso",
        "side": "L" if side > 0 else "R",
        "side_mask": True,
        "outside_torso_fraction": float(torso_half / max(height, 1e-9)),
        "seed_polyline_extension": [0.15, 0.55],
        "radius": float(radius),
        "count": int(len(selected)),
        "c_range_seed": [float(lower), float(upper)],
        "seed": {
            "method": "reference_rest_proportions_mapped_to_target_bounds",
            "polyline": seed_polyline.tolist(),
        },
    }
    return selected, diagnostics


def _human_arm_separation(
    coords: np.ndarray,
    arm_points: np.ndarray,
    *,
    side: int,
    center_a: float,
    torso_half: float,
    height: float,
) -> dict:
    if len(arm_points) < 6:
        return {"independent": False, "reason": "too_few_arm_surface_points"}
    mid_c = float(np.median(arm_points[:, 2]))
    section = _section_metrics(
        coords,
        mid_c,
        max(height * 0.035, 1e-4),
        center_a,
        require_center=True,
        gap=height * 0.045,
    )
    if section is None:
        return {
            "independent": True,
            "gap": None,
            "reason": "no_torso_section_at_arm_midpoint",
        }
    band = np.abs(arm_points[:, 2] - mid_c) <= height * 0.04
    values = arm_points[band, 0] if np.any(band) else arm_points[:, 0]
    if not len(values):
        return {"independent": False, "reason": "no_arm_points_at_midpoint"}
    # The torso section can merge into a T-pose arm; use the measured trunk
    # half width as the separation boundary instead of the possibly merged
    # section extent.
    torso_edge = center_a + side * torso_half * 1.05
    if side > 0:
        gap = float(np.min(values) - torso_edge)
        median_gap = float(np.median(values) - torso_edge)
        outside = np.mean(values > torso_edge)
    else:
        gap = float(torso_edge - np.max(values))
        median_gap = float(torso_edge - np.median(values))
        outside = np.mean(values < torso_edge)
    lateral_span = float(np.ptp(arm_points[:, 0]))
    return {
        "independent": bool(
            median_gap >= height * 0.02
            and outside >= 0.40
            and lateral_span >= height * 0.05
        ),
        "gap": gap,
        "median_gap": median_gap,
        "lateral_span": lateral_span,
        "torso_edge": float(torso_edge),
        "outside_fraction": float(outside),
        "torso_section": section,
        "mid_c": mid_c,
    }


def _plan_human_arm(
    *,
    coords: np.ndarray,
    reference_bones: dict,
    mapper: _Mapper,
    side: str,
    center_a: float,
    torso_half: float,
    head_bottom_c: float,
    target_bounds: dict,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> None:
    sign = 1 if side == "L" else -1
    names = ["upper_arm." + side, "forearm." + side, "hand." + side]
    if any(name not in reference_bones for name in names):
        for name in names:
            if name in reference_bones:
                _mark_unestimated(
                    bone_reports, name, "arm." + side, side, "参考缺少完整手臂链"
                )
        chain_reports["arm." + side] = {"status": "unestimated", "reason": "missing_reference_chain"}
        return
    mapped = [_mapped_bone(mapper, reference_bones, name) for name in names]
    seed_polyline = np.asarray(
        [mapped[0]["head"]] + [row["tail"] for row in mapped], dtype=np.float64
    )
    points, diagnostics = _human_arm_candidates(
        coords,
        side=sign,
        center_a=center_a,
        torso_half=torso_half,
        head_bottom_c=head_bottom_c,
        seed_polyline=seed_polyline,
        target_bounds=target_bounds,
    )
    height = target_bounds["height"]
    if len(points) < 24:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "arm." + side,
                side,
                "找不到与躯干分开的独立手臂表面（%d点）" % len(points),
                selection=diagnostics,
            )
        chain_reports["arm." + side] = {
            "status": "unestimated",
            "reason": "insufficient_independent_surface",
            "diagnostics": diagnostics,
        }
        return
    separation = _human_arm_separation(
        coords,
        points,
        side=sign,
        center_a=center_a,
        torso_half=torso_half,
        height=height,
    )
    if not separation.get("independent"):
        reason = "手臂与躯干表面无法独立分离"
        if separation.get("reason"):
            reason += "：" + str(separation["reason"])
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "arm." + side,
                side,
                reason,
                selection=diagnostics,
                separation=separation,
            )
        chain_reports["arm." + side] = {
            "status": "unestimated",
            "reason": "not_independent",
            "diagnostics": diagnostics,
            "separation": separation,
        }
        return
    centerline = _fit_centerline(
        points,
        seed_polyline[0],
        seed_polyline[-1],
        bins=20,
        min_points=24,
        min_bins=4,
        min_bin_points=2,
    )
    if centerline is None:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "arm." + side,
                side,
                "手臂截面中心线不足，无法定位肩/肘/腕",
                selection=diagnostics,
            )
        chain_reports["arm." + side] = {
            "status": "unestimated",
            "reason": "centerline_failed",
            "diagnostics": diagnostics,
        }
        return
    fractions = _bone_length_fractions(reference_bones, names)
    elbow_fraction = 0.02 + fractions[0] * 0.96
    wrist_fraction = 0.02 + fractions[1] * 0.96
    shoulder = _centerline_point(centerline, 0.025)
    elbow = _centerline_point(centerline, elbow_fraction)
    wrist = _centerline_point(centerline, wrist_fraction)
    hand_tip = _centerline_point(centerline, 0.985)
    distal_axis = np.asarray(centerline["axis"], dtype=np.float64)
    distal_hint = seed_polyline[-1] - seed_polyline[0]
    if float(np.dot(distal_axis, distal_hint)) < 0.0:
        distal_axis = -distal_axis
    projection = (points - shoulder) @ distal_axis
    cutoff = float(np.percentile(projection, 97.0))
    far_points = points[projection >= cutoff]
    hand_tip_method = "surface_centerline_distal"
    if len(far_points) >= 3:
        candidate_tip = np.median(far_points, axis=0)
        if float(np.linalg.norm(candidate_tip - wrist)) > float(
            np.linalg.norm(hand_tip - wrist)
        ):
            hand_tip = candidate_tip
            hand_tip_method = "surface_distal_quantile_center"
    if any(
        float(np.linalg.norm(b - a)) <= height * 0.008
        for a, b in ((shoulder, elbow), (elbow, wrist), (wrist, hand_tip))
    ):
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "arm." + side,
                side,
                "手臂中心线关节过近或退化",
                selection=diagnostics,
            )
        chain_reports["arm." + side] = {
            "status": "unestimated",
            "reason": "degenerate_centerline",
            "diagnostics": diagnostics,
        }
        return
    shoulder_section = _best_section(
        coords,
        float(shoulder[2]),
        height * 0.025,
        center_a,
        widen=1.8,
        require_center=True,
        gap=height * 0.045,
    )
    if shoulder_section is None:
        shoulder_root = np.array(
            [
                center_a + sign * torso_half * 0.12,
                float(shoulder[1]),
                float(shoulder[2]),
            ],
            dtype=np.float64,
        )
    else:
        shoulder_root = np.array(
            [
                float(shoulder_section["center_a"]) + sign * torso_half * 0.10,
                float(shoulder_section["center_f"]),
                float(shoulder[2]),
            ],
            dtype=np.float64,
        )
    method = "surface_limb_centerline_with_reference_length_fraction"
    seed = {
        "method": "reference_rest_proportions_mapped_to_target_bounds",
        "chain_bones": names,
        "elbow_fraction": float(fractions[0]),
        "wrist_fraction": float(fractions[1]),
    }
    actual = {
        "centerline_bins": centerline["filled_bins"],
        "support_points": centerline["support_points"],
        "centerline_length": centerline["length"],
        "shoulder_section": shoulder_section,
        "separation": separation,
        "selection": diagnostics,
        "hand_tip_method": hand_tip_method,
    }
    _add_position(
        positions,
        bone_reports,
        "shoulder." + side,
        head=shoulder_root,
        tail=shoulder,
        method="surface_torso_section_shoulder_root",
        chain="arm." + side,
        side=side,
        seed=seed,
        actual=actual,
    )
    _add_position(
        positions,
        bone_reports,
        "upper_arm." + side,
        head=shoulder,
        tail=elbow,
        method=method,
        chain="arm." + side,
        side=side,
        seed=seed,
        actual=actual,
    )
    _add_position(
        positions,
        bone_reports,
        "forearm." + side,
        head=elbow,
        tail=wrist,
        method=method,
        chain="arm." + side,
        side=side,
        seed=seed,
        actual=actual,
    )
    _add_position(
        positions,
        bone_reports,
        "hand." + side,
        head=wrist,
        tail=hand_tip,
        method=method,
        chain="arm." + side,
        side=side,
        seed=seed,
        actual=actual,
    )
    chain_reports["arm." + side] = {
        "status": "proposed_needs_review",
        "geometry_basis": "actual_surface_centerline",
        "bones": ["shoulder." + side] + names,
        "support_points": centerline["support_points"],
        "centerline_bins": centerline["filled_bins"],
        "shoulder": shoulder.tolist(),
        "elbow": elbow.tolist(),
        "wrist": wrist.tolist(),
        "hand_tip": hand_tip.tolist(),
        "hand_tip_method": hand_tip_method,
        "separation": separation,
        "selection": diagnostics,
    }


def _human_leg_candidates(
    coords: np.ndarray,
    *,
    side: int,
    center_a: float,
    seed_polyline: np.ndarray,
    arm_polyline: np.ndarray | None,
    target_bounds: dict,
    hip_seed_c: float,
) -> tuple[np.ndarray, dict]:
    height = target_bounds["height"]
    side_mask = side * (coords[:, 0] - center_a) > height * 0.012
    lower = target_bounds["min"][2] - height * 0.01
    upper = min(target_bounds["max"][2], hip_seed_c + height * 0.08)
    c_mask = (coords[:, 2] >= lower) & (coords[:, 2] <= upper)
    extended = _extended_polyline_absolute(
        seed_polyline, height * 0.05, height * 0.15
    )
    distance = _polyline_distance(coords, extended)
    radius = max(height * 0.12, float(np.ptp(coords[:, 0])) * 0.16)
    near_seed = distance <= radius
    if arm_polyline is not None and len(arm_polyline) >= 2:
        arm_distance = _polyline_distance(
            coords,
            _extended_polyline_absolute(arm_polyline, height * 0.05, height * 0.30),
        )
        near_arm = arm_distance <= radius * 1.25
        nearer_leg = (~near_arm) | (distance <= arm_distance + height * 0.005)
    else:
        arm_distance = np.full(len(coords), np.inf)
        nearer_leg = np.ones(len(coords), dtype=bool)
    mask = side_mask & c_mask & near_seed & nearer_leg
    selected = coords[mask]
    return selected, {
        "selection": "actual_surface side/leg seed exclusion",
        "radius": float(radius),
        "count": int(len(selected)),
        "c_range": [float(lower), float(upper)],
        "seed_polyline_extension": [0.08, 0.25],
        "arm_exclusion_used": arm_polyline is not None,
        "mean_arm_distance": float(np.mean(arm_distance[mask])) if np.any(mask) else None,
        "mask_counts": {
            "side": int(np.sum(side_mask)),
            "c_range": int(np.sum(c_mask)),
            "near_seed": int(np.sum(near_seed)),
            "nearer_leg": int(np.sum(nearer_leg)),
            "combined": int(np.sum(mask)),
        },
        "seed_bounds": [
            np.min(seed_polyline, axis=0).tolist(),
            np.max(seed_polyline, axis=0).tolist(),
        ],
        "arm_bounds": None
        if arm_polyline is None
        else [
            np.min(arm_polyline, axis=0).tolist(),
            np.max(arm_polyline, axis=0).tolist(),
        ],
    }


def _human_foot(
    coords: np.ndarray,
    leg_points: np.ndarray,
    *,
    side: int,
    ankle_estimate_c: float,
    center_a: float,
    target_bounds: dict,
) -> dict | None:
    height = target_bounds["height"]
    if len(leg_points) < 10:
        return None
    ground = float(np.min(leg_points[:, 2]))
    for band in (0.018, 0.035, 0.060):
        mask = (
            (leg_points[:, 2] <= ankle_estimate_c + height * band)
            & (leg_points[:, 2] >= ground - height * 0.005)
        )
        points = leg_points[mask]
        if len(points) >= 8:
            break
    else:
        return None
    bottom = points[points[:, 2] <= ground + height * 0.035]
    if len(bottom) < 4:
        bottom = points
    if len(bottom) < 4:
        return None
    toe_index = int(np.argmax(points[:, 1]))
    heel_f = float(np.min(bottom[:, 1]))
    toe_f = float(np.max(points[:, 1]))
    if toe_f - heel_f <= height * 0.01:
        return None
    # Toe tip: actual forwardmost surface neighbourhood, not a bbox corner.
    tip_band = points[points[:, 1] >= toe_f - height * 0.02]
    toe_tip = np.array(
        [
            _median(bottom[:, 0]),
            toe_f,
            float(np.min(tip_band[:, 2])),
        ],
        dtype=np.float64,
    )
    # Ball/ toe base at ~55% of the actual foot length from the heel.
    base_f = heel_f + (toe_f - heel_f) * 0.55
    base_band = bottom[np.abs(bottom[:, 1] - base_f) <= height * 0.035]
    if len(base_band) < 4:
        base_band = bottom
    toe_base = np.array(
        [
            _median(base_band[:, 0]),
            base_f,
            ground + height * 0.012,
        ],
        dtype=np.float64,
    )
    heel_band = bottom[np.abs(bottom[:, 1] - heel_f) <= height * 0.035]
    if len(heel_band) < 4:
        heel_band = bottom
    heel_center_a = _median(heel_band[:, 0])
    heel_half = max(float(np.ptp(heel_band[:, 0])) * 0.5, height * 0.012)
    heel_head = np.array(
        [heel_center_a - heel_half * side * 0.65, heel_f + height * 0.004, ground],
        dtype=np.float64,
    )
    heel_tail = np.array(
        [heel_center_a + heel_half * side * 0.65, heel_f + height * 0.004, ground],
        dtype=np.float64,
    )
    return {
        "points": points,
        "ground": ground,
        "heel_f": heel_f,
        "toe_f": toe_f,
        "toe_tip": toe_tip,
        "toe_base": toe_base,
        "heel_head": heel_head,
        "heel_tail": heel_tail,
        "foot_length": toe_f - heel_f,
        "bottom_count": int(len(bottom)),
        "point_count": int(len(points)),
    }


def _plan_human_leg(
    *,
    coords: np.ndarray,
    reference_bones: dict,
    mapper: _Mapper,
    side: str,
    center_a: float,
    target_bounds: dict,
    arm_polyline_by_side: dict,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> None:
    sign = 1 if side == "L" else -1
    names = ["thigh." + side, "shin." + side, "foot." + side, "toe." + side]
    optional = ["heel.02." + side]
    if any(name not in reference_bones for name in names):
        for name in names + optional:
            if name in reference_bones:
                _mark_unestimated(
                    bone_reports, name, "leg." + side, side, "参考缺少完整腿/脚链"
                )
        chain_reports["leg." + side] = {
            "status": "unestimated",
            "reason": "missing_reference_chain",
        }
        return
    mapped = [_mapped_bone(mapper, reference_bones, name) for name in names]
    thigh, shin, foot, toe = mapped
    seed_polyline = np.asarray(
        [thigh["head"], shin["head"], foot["head"]], dtype=np.float64
    )
    leg_points, diagnostics = _human_leg_candidates(
        coords,
        side=sign,
        center_a=center_a,
        seed_polyline=seed_polyline,
        arm_polyline=arm_polyline_by_side.get(side),
        target_bounds=target_bounds,
        hip_seed_c=float(thigh["head"][2]),
    )
    height = target_bounds["height"]
    if len(leg_points) < 24:
        for name in names + optional:
            if name in reference_bones:
                _mark_unestimated(
                    bone_reports,
                    name,
                    "leg." + side,
                    side,
                    "找不到与另一腿分开的独立腿表面（%d点）" % len(leg_points),
                    selection=diagnostics,
                )
        chain_reports["leg." + side] = {
            "status": "unestimated",
            "reason": "insufficient_independent_surface",
            "diagnostics": diagnostics,
        }
        return
    ground = float(np.min(leg_points[:, 2]))
    ankle_estimate_c = float(
        np.clip(
            foot["head"][2],
            ground + height * 0.025,
            thigh["head"][2] - height * 0.12,
        )
    )
    upper_points = leg_points[
        leg_points[:, 2] >= ankle_estimate_c - height * 0.01
    ]
    centerline = _fit_centerline(
        upper_points,
        thigh["head"],
        np.array(
            [foot["head"][0], foot["head"][1], ankle_estimate_c],
            dtype=np.float64,
        ),
        bins=18,
        min_points=20,
        min_bins=4,
        min_bin_points=2,
    )
    if centerline is None:
        for name in names + optional:
            if name in reference_bones:
                _mark_unestimated(
                    bone_reports,
                    name,
                    "leg." + side,
                    side,
                    "腿部截面中心线不足，无法定位髋/膝/踝",
                    selection=diagnostics,
                )
        chain_reports["leg." + side] = {
            "status": "unestimated",
            "reason": "centerline_failed",
            "diagnostics": diagnostics,
        }
        return
    fractions = _bone_length_fractions(reference_bones, ["thigh." + side, "shin." + side])
    knee_fraction = 0.02 + float(fractions[0]) * 0.96
    hip = _centerline_point(centerline, 0.02)
    knee = _centerline_point(centerline, knee_fraction)
    ankle = _centerline_point(centerline, 0.985)
    if (
        float(np.linalg.norm(knee - hip)) <= height * 0.01
        or float(np.linalg.norm(ankle - knee)) <= height * 0.01
    ):
        for name in names + optional:
            if name in reference_bones:
                _mark_unestimated(
                    bone_reports,
                    name,
                    "leg." + side,
                    side,
                    "腿部中心线关节过近或退化",
                    selection=diagnostics,
                )
        chain_reports["leg." + side] = {
            "status": "unestimated",
            "reason": "degenerate_centerline",
            "diagnostics": diagnostics,
        }
        return
    foot_info = _human_foot(
        coords,
        leg_points,
        side=sign,
        ankle_estimate_c=float(ankle[2]),
        center_a=center_a,
        target_bounds=target_bounds,
    )
    seed = {
        "method": "reference_rest_proportions_mapped_to_target_bounds",
        "chain_bones": names,
        "knee_fraction": float(fractions[0]),
        "ankle_estimate_c": ankle_estimate_c,
    }
    actual = {
        "centerline_bins": centerline["filled_bins"],
        "support_points": centerline["support_points"],
        "centerline_length": centerline["length"],
        "selection": diagnostics,
        "foot": None
        if foot_info is None
        else {
            key: value
            for key, value in foot_info.items()
            if key not in {"points"}
        },
    }
    method = "surface_leg_centerline_with_reference_length_fraction"
    _add_position(
        positions,
        bone_reports,
        "thigh." + side,
        head=hip,
        tail=knee,
        method=method,
        chain="leg." + side,
        side=side,
        seed=seed,
        actual=actual,
    )
    _add_position(
        positions,
        bone_reports,
        "shin." + side,
        head=knee,
        tail=ankle,
        method=method,
        chain="leg." + side,
        side=side,
        seed=seed,
        actual=actual,
    )
    if foot_info is not None:
        _add_position(
            positions,
            bone_reports,
            "foot." + side,
            head=ankle,
            tail=foot_info["toe_base"],
            method="surface_foot_extremes",
            chain="leg." + side,
            side=side,
            seed=seed,
            actual=actual,
        )
        _add_position(
            positions,
            bone_reports,
            "toe." + side,
            head=foot_info["toe_base"],
            tail=foot_info["toe_tip"],
            method="surface_foot_extremes",
            chain="leg." + side,
            side=side,
            seed=seed,
            actual=actual,
        )
        if "heel.02." + side in reference_bones:
            _add_position(
                positions,
                bone_reports,
                "heel.02." + side,
                head=foot_info["heel_head"],
                tail=foot_info["heel_tail"],
                method="surface_foot_extremes_heel_attachment",
                chain="leg." + side,
                side=side,
                seed=seed,
                actual=actual,
                parent_preserved=reference_bones["heel.02." + side]["parent"],
                use_connect_preserved=reference_bones["heel.02." + side]["use_connect"],
            )
    else:
        for name in ("foot." + side, "toe." + side, "heel.02." + side):
            if name in reference_bones:
                _mark_unestimated(
                    bone_reports,
                    name,
                    "leg." + side,
                    side,
                    "脚部表面不足，未估计；髋/膝/踝仍为实际腿中心线候选",
                    selection=diagnostics,
                )
    chain_reports["leg." + side] = {
        "status": "proposed_needs_review"
        if foot_info is not None
        else "partial",
        "geometry_basis": "actual_surface_centerline_and_foot_extremes",
        "bones": ["pelvis." + side] + names + optional,
        "support_points": centerline["support_points"],
        "centerline_bins": centerline["filled_bins"],
        "hip": hip.tolist(),
        "knee": knee.tolist(),
        "ankle": ankle.tolist(),
        "foot": None
        if foot_info is None
        else {key: value for key, value in foot_info.items() if key not in {"points"}},
        "selection": diagnostics,
    }


def _human_spine_joints(
    coords: np.ndarray,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
    trunk: dict,
    enabled: list[str],
) -> tuple[dict | None, dict]:
    height = target_bounds["height"]
    names = ["spine", "spine.001", "spine.002", "spine.003"]
    if "head" in enabled:
        names.extend(["spine.004", "spine.005", "spine.006"])
    if any(name not in reference_bones for name in names):
        return None, {"status": "unestimated", "reason": "missing_spine_chain"}
    joints = []
    for index, name in enumerate(names):
        bone = reference_bones[name]
        for endpoint in ("head", "tail"):
            section = bone[endpoint]
            fraction = _reference_fraction(reference_bounds, bone, endpoint)
            joints.append(
                {
                    "name": f"{name}/{endpoint}",
                    "source_bone": name,
                    "endpoint": endpoint,
                    "fraction": float(fraction),
                    "seed_c": _section_level(
                        target_bounds["min"][2], height, fraction
                    ),
                }
            )
    # A head/tail pair on each connected bone shares the same joint.  Keep one
    # entry per actual spatial joint so the chain is not collapsed twice.
    deduplicated = []
    for joint in joints:
        if deduplicated and abs(joint["seed_c"] - deduplicated[-1]["seed_c"]) <= height * 1e-5:
            deduplicated[-1]["aliases"].append(joint["name"])
            continue
        joint["aliases"] = [joint["name"]]
        deduplicated.append(joint)
    # Final joint for a human is the actual head top, not a bbox-derived point.
    if "head" in enabled:
        top_mask = coords[:, 2] >= target_bounds["max"][2] - height * 0.008
        top = coords[top_mask]
        if len(top) >= 4:
            deduplicated[-1]["actual"] = {
                "a": _median(top[:, 0]),
                "f": _median(top[:, 1]),
                "c": float(target_bounds["max"][2]),
                "count": int(len(top)),
                "method": "surface_head_top_extremum",
            }
    sections = []
    valid = []
    for joint in deduplicated:
        if "actual" in joint:
            sections.append(joint["actual"])
            valid.append(len(sections) - 1)
            continue
        section = _best_section(
            coords,
            joint["seed_c"],
            height * 0.022,
            trunk["center_a"],
            widen=1.9,
            require_center=True,
            gap=height * 0.045,
        )
        if section is not None and section["width"] <= target_bounds["span"][0] * 0.80:
            joint["actual"] = {
                "a": float(section["center_a"]),
                "f": float(section["center_f"]),
                "c": float(joint["seed_c"]),
                "count": int(section["count"]),
                "width": float(section["width"]),
                "method": "surface_torso_section_center_with_neutral_height_seed",
            }
            sections.append(joint["actual"])
            valid.append(len(sections) - 1)
        else:
            sections.append(None)
    if not valid:
        return None, {"status": "unestimated", "reason": "no_torso_sections"}
    # Interpolate only between two real sections; extrapolation at the ends is
    # reported explicitly and is not called a geometry estimate.
    for index, section in enumerate(sections):
        if section is not None:
            continue
        lower = max((item for item in valid if item < index), default=None)
        upper = min((item for item in valid if item > index), default=None)
        if lower is not None and upper is not None:
            low, high = sections[lower], sections[upper]
            unit = (index - lower) / (upper - lower)
            sections[index] = {
                "a": low["a"] * (1.0 - unit) + high["a"] * unit,
                "f": low["f"] * (1.0 - unit) + high["f"] * unit,
                "c": deduplicated[index]["seed_c"],
                "count": 0,
                "method": "interpolated_between_actual_sections",
            }
        else:
            nearest = min(valid, key=lambda item: abs(item - index))
            base = sections[nearest]
            sections[index] = {
                "a": float(trunk["center_a"] if base is None else base["a"]),
                "f": float(trunk["center_f"] if base is None else base["f"]),
                "c": deduplicated[index]["seed_c"],
                "count": 0,
                "method": "proportion_extrapolation_not_geometry_estimate",
            }
    for joint, section in zip(deduplicated, sections):
        joint["actual"] = section
    lengths = [
        float(
            np.linalg.norm(
                np.array(
                    [sections[index + 1]["a"], sections[index + 1]["f"], sections[index + 1]["c"]]
                )
                - np.array([sections[index]["a"], sections[index]["f"], sections[index]["c"]])
            )
        )
        for index in range(len(sections) - 1)
    ]
    if any(length <= height * 0.008 for length in lengths):
        return None, {
            "status": "unestimated",
            "reason": "degenerate_spine_segments",
            "segment_lengths": lengths,
        }
    return {
        "names": names,
        "joints": [
            {
                "name": joint["name"],
                "aliases": joint["aliases"],
                "fraction": joint["fraction"],
                "seed_c": joint["seed_c"],
                "actual": joint["actual"],
            }
            for joint in deduplicated
        ],
        "segment_lengths": lengths,
    }, {"status": "proposed_needs_review"}


def _plan_human_spine(
    *,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
    trunk: dict,
    enabled: list[str],
    coords: np.ndarray,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> None:
    plan, status = _human_spine_joints(
        coords,
        reference_bones,
        reference_bounds,
        target_bounds,
        trunk,
        enabled,
    )
    names = ["spine", "spine.001", "spine.002", "spine.003"]
    if "head" in enabled:
        names.extend(["spine.004", "spine.005", "spine.006"])
    if plan is None:
        for name in names:
            _mark_unestimated(
                bone_reports, name, "torso", "CENTER", status.get("reason", "躯干截面不足")
            )
        chain_reports["torso"] = status
        return
    joints = plan["joints"]
    actuals = [
        np.array(
            [row["actual"]["a"], row["actual"]["f"], row["actual"]["c"]],
            dtype=np.float64,
        )
        for row in joints
    ]
    seed = {
        "method": "reference_rest_proportions_mapped_to_target_height",
        "joint_fractions": [row["fraction"] for row in joints],
        "declared_not_geometry": True,
    }
    for index, name in enumerate(names):
        head = actuals[index]
        tail = actuals[index + 1]
        section = joints[index + 1]["actual"]
        head_method = joints[index]["actual"].get("method", "")
        tail_method = section.get("method", "")
        basis = (
            "actual_surface_section"
            if not str(head_method).startswith("proportion")
            and not str(tail_method).startswith("proportion")
            else "proportion_extrapolation_not_geometry_estimate"
        )
        _add_position(
            positions,
            bone_reports,
            name,
            head=head,
            tail=tail,
            method="surface_torso_section_center_with_neutral_height_seed",
            chain="torso",
            side="CENTER",
            seed=seed,
            actual={
                "joint_head": joints[index]["actual"],
                "joint_tail": section,
                "segment_length": float(np.linalg.norm(tail - head)),
            },
            geometry_basis=basis,
        )
    chain_reports["torso"] = {
        "status": "proposed_needs_review",
        "geometry_basis": "actual_surface_cross_sections_plus_declared_neutral_height_seeds",
        "bones": names,
        "joints": [
            {
                "name": row["name"],
                "aliases": row["aliases"],
                "fraction": row["fraction"],
                "seed_c": row["seed_c"],
                "actual": row["actual"],
            }
            for row in joints
        ],
        "segment_lengths": plan["segment_lengths"],
    }


def _plan_human_pelvis(
    *,
    reference_bones: dict,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> None:
    spine = positions.get("spine")
    for side in ("L", "R"):
        name = "pelvis." + side
        if name not in reference_bones:
            continue
        hip_row = positions.get("thigh." + side)
        if spine is None or spine.get("head") is None or hip_row is None:
            _mark_unestimated(
                bone_reports,
                name,
                "leg." + side,
                side,
                "髋关节未由独立腿部几何估计，pelvis 不替换为比例初值",
            )
            continue
        head = spine["head"].copy()
        hip = hip_row["head"]
        tail = np.array(
            [
                float(hip[0]),
                float(hip[1]),
                float(hip[2]) + 0.0,
            ],
            dtype=np.float64,
        )
        _add_position(
            positions,
            bone_reports,
            name,
            head=head,
            tail=tail,
            method="surface_pelvis_root_attachment_to_actual_hip",
            chain="leg." + side,
            side=side,
            actual={
                "spine_root": head.tolist(),
                "hip_joint": hip.tolist(),
                "parent_preserved": reference_bones[name]["parent"],
                "use_connect_preserved": reference_bones[name]["use_connect"],
            },
        )


def _tail_candidates(
    coords: np.ndarray,
    *,
    center_a: float,
    body_f_range,
    kind: str,
    target_bounds: dict,
) -> np.ndarray:
    height = target_bounds["height"]
    width = float(target_bounds["span"][0])
    if kind == "QUADRUPED":
        body_length = float(body_f_range[1] - body_f_range[0])
        for margin, c_low, a_band in (
            (0.002, 0.14, 0.24),
            (0.010, 0.12, 0.30),
            (0.025, 0.10, 0.36),
        ):
            mask = (
                (coords[:, 1] < body_f_range[0] - body_length * margin)
                & (coords[:, 2] >= target_bounds["min"][2] + height * c_low)
                & (np.abs(coords[:, 0] - center_a) <= width * a_band)
            )
            selected = coords[mask]
            if len(selected) >= 12:
                return selected
    else:
        body_length = float(body_f_range[1] - body_f_range[0])
        for margin, c_low, c_high, a_band in (
            (0.002, 0.22, 0.82, 0.20),
            (0.012, 0.18, 0.86, 0.26),
        ):
            mask = (
                (coords[:, 1] < body_f_range[0] - body_length * margin)
                & (coords[:, 2] >= target_bounds["min"][2] + height * c_low)
                & (coords[:, 2] <= target_bounds["min"][2] + height * c_high)
                & (np.abs(coords[:, 0] - center_a) <= width * a_band)
            )
            selected = coords[mask]
            if len(selected) >= 12:
                return selected
    return np.zeros((0, 3), dtype=np.float64)


def _plan_tail(
    *,
    coords: np.ndarray,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
    body_f_range,
    center_a: float,
    trunk_center_f: float,
    kind: str,
    base_point: np.ndarray | None,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> None:
    names = ["tail.001", "tail.002", "tail.003", "tail.004"]
    if any(name not in reference_bones for name in names):
        return
    if base_point is None:
        for name in names:
            _mark_unestimated(
                bone_reports, name, "tail", "CENTER", "躯干尾端锚点未估计"
            )
        chain_reports["tail"] = {"status": "unestimated", "reason": "no_body_tail_anchor"}
        return
    candidates = _tail_candidates(
        coords,
        center_a=center_a,
        body_f_range=body_f_range,
        kind=kind,
        target_bounds=target_bounds,
    )
    if len(candidates) < 12:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "tail",
                "CENTER",
                "找不到独立尾巴表面；保留原参考骨并明确未估计",
                support_points=int(len(candidates)),
            )
        chain_reports["tail"] = {
            "status": "unestimated",
            "reason": "no_independent_tail_surface",
            "support_points": int(len(candidates)),
        }
        return
    base = np.asarray(base_point, dtype=np.float64)
    distances = np.linalg.norm(candidates - base, axis=1)
    tip = candidates[int(np.argmax(distances))]
    axis = tip - base
    length = float(np.linalg.norm(axis))
    body_length = float(body_f_range[1] - body_f_range[0])
    if length <= max(body_length * 0.02, target_bounds["height"] * 0.02):
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "tail",
                "CENTER",
                "尾巴表面跨度不足，无法建立四段连续链",
                support_points=int(len(candidates)),
            )
        chain_reports["tail"] = {
            "status": "unestimated",
            "reason": "tail_span_too_short",
            "support_points": int(len(candidates)),
        }
        return
    axis_unit = axis / length
    projection = (candidates - base) @ axis_unit
    low, high = float(np.min(projection)), float(np.max(projection))
    if high - low <= length * 0.5:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "tail",
                "CENTER",
                "尾巴表面投影退化，不猜测弯曲尾巴",
                support_points=int(len(candidates)),
            )
        chain_reports["tail"] = {
            "status": "unestimated",
            "reason": "degenerate_tail_projection",
            "support_points": int(len(candidates)),
        }
        return
    bins = 10
    edges = np.linspace(low, high, bins + 1)
    index = np.clip(np.searchsorted(edges, projection, side="right") - 1, 0, bins - 1)
    rows = []
    for number in range(bins):
        selected = candidates[index == number]
        if len(selected) >= 3:
            rows.append(
                {
                    "t": float(np.median(projection[index == number])),
                    "point": np.median(selected, axis=0),
                    "count": int(len(selected)),
                }
            )
    if len(rows) < 3:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "tail",
                "CENTER",
                "尾巴截面中心不足，不猜测尾巴",
                support_points=int(len(candidates)),
            )
        chain_reports["tail"] = {
            "status": "unestimated",
            "reason": "tail_centerline_failed",
            "support_points": int(len(candidates)),
        }
        return
    rows.sort(key=lambda row: row["t"])
    centerline = np.asarray([row["point"] for row in rows], dtype=np.float64)
    # Ensure the chain starts at the actual body attachment and ends at the
    # farthest actual tail surface point.
    centerline = np.vstack((base, centerline, tip))
    segment_lengths = np.linalg.norm(np.diff(centerline, axis=0), axis=1)
    cumulative = np.r_[0.0, np.cumsum(segment_lengths)]
    total = float(cumulative[-1])
    if total <= max(body_length * 0.02, target_bounds["height"] * 0.02):
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "tail",
                "CENTER",
                "尾巴中心线长度不足",
                support_points=int(len(candidates)),
            )
        chain_reports["tail"] = {
            "status": "unestimated",
            "reason": "tail_centerline_too_short",
            "support_points": int(len(candidates)),
        }
        return
    fractions = (0.25, 0.50, 0.75, 1.0)
    points = []
    for fraction in fractions:
        target = total * fraction
        if target <= 0:
            points.append(centerline[0].copy())
            continue
        if target >= total:
            points.append(centerline[-1].copy())
            continue
        section = int(np.searchsorted(cumulative, target, side="right") - 1)
        section = max(0, min(section, len(centerline) - 2))
        span = cumulative[section + 1] - cumulative[section]
        unit = 0.0 if span <= _EPS else (target - cumulative[section]) / span
        points.append(
            centerline[section] * (1.0 - unit) + centerline[section + 1] * unit
        )
    minimum = max(target_bounds["height"] * 0.008, body_length * 0.006)
    previous = base
    for name, point in zip(names, points):
        if float(np.linalg.norm(point - previous)) <= minimum:
            for item in names:
                _mark_unestimated(
                    bone_reports,
                    item,
                    "tail",
                    "CENTER",
                    "尾巴四段链长度退化，保留原参考骨",
                    support_points=int(len(candidates)),
                )
            chain_reports["tail"] = {
                "status": "unestimated",
                "reason": "degenerate_tail_chain",
                "support_points": int(len(candidates)),
            }
            return
        previous = point
    for name, point in zip(names, points):
        _add_position(
            positions,
            bone_reports,
            name,
            head=None if name != "tail.001" else base,
            tail=point,
            method="surface_tail_centerline",
            chain="tail",
            side="CENTER",
            seed={"method": "reference_tail_four_segments", "declared_not_geometry": True},
            actual={
                "support_points": int(len(candidates)),
                "centerline_bins": int(len(rows)),
                "tail_length": total,
                "parent_preserved": reference_bones[name]["parent"],
                "use_connect_preserved": reference_bones[name]["use_connect"],
            },
        )
    chain_reports["tail"] = {
        "status": "proposed_needs_review",
        "geometry_basis": "actual_surface_tail_projection_centerline",
        "bones": names,
        "support_points": int(len(candidates)),
        "tail_length": total,
        "base_attachment_preserved": True,
    }


def _plan_human(
    *,
    reference,
    record: dict,
    enabled: list[str],
    frame: dict,
    coords: np.ndarray,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
    body_f_range,
) -> dict:
    trunk = _human_trunk(
        coords, reference_bones, reference_bounds, target_bounds
    )
    mapper = _Mapper(
        reference_bounds["min"],
        reference_bounds["max"],
        target_bounds["min"],
        target_bounds["max"],
    )
    positions = {}
    bone_reports = {}
    chain_reports = {}
    arm_polyline_by_side = {}
    for side in ("L", "R"):
        names = ["upper_arm." + side, "forearm." + side, "hand." + side]
        if all(name in reference_bones for name in names):
            rows = [_mapped_bone(mapper, reference_bones, name) for name in names]
            arm_polyline_by_side[side] = np.asarray(
                [rows[0]["head"]] + [row["tail"] for row in rows],
                dtype=np.float64,
            )
    for side in ("L", "R"):
        if "forelimb." + side in enabled:
            _plan_human_arm(
                coords=coords,
                reference_bones=reference_bones,
                mapper=mapper,
                side=side,
                center_a=trunk["center_a"],
                torso_half=trunk["torso_half_width"],
                head_bottom_c=trunk["head_bottom_c"],
                target_bounds=target_bounds,
                positions=positions,
                bone_reports=bone_reports,
                chain_reports=chain_reports,
            )
        else:
            chain_reports["arm." + side] = {
                "status": "disabled",
                "reason": "module_not_enabled",
            }
    for side in ("L", "R"):
        if "hindlimb." + side in enabled:
            _plan_human_leg(
                coords=coords,
                reference_bones=reference_bones,
                mapper=mapper,
                side=side,
                center_a=trunk["center_a"],
                target_bounds=target_bounds,
                arm_polyline_by_side=arm_polyline_by_side,
                positions=positions,
                bone_reports=bone_reports,
                chain_reports=chain_reports,
            )
        else:
            chain_reports["leg." + side] = {
                "status": "disabled",
                "reason": "module_not_enabled",
            }
    _plan_human_spine(
        reference_bones=reference_bones,
        reference_bounds=reference_bounds,
        target_bounds=target_bounds,
        trunk=trunk,
        enabled=enabled,
        coords=coords,
        positions=positions,
        bone_reports=bone_reports,
        chain_reports=chain_reports,
    )
    _plan_human_pelvis(
        reference_bones=reference_bones,
        positions=positions,
        bone_reports=bone_reports,
        chain_reports=chain_reports,
    )
    if "tail" in enabled:
        spine = positions.get("spine")
        base = None if spine is None else spine.get("head")
        _plan_tail(
            coords=coords,
            reference_bones=reference_bones,
            reference_bounds=reference_bounds,
            target_bounds=target_bounds,
            body_f_range=body_f_range,
            center_a=trunk["center_a"],
            trunk_center_f=trunk["center_f"],
            kind="HUMAN",
            base_point=base,
            positions=positions,
            bone_reports=bone_reports,
            chain_reports=chain_reports,
        )
    initialization = mapper.as_dict()
    initialization.update(
        {
            "reference_bbox_basis": "reference_rest_non_tail_bone_points",
            "used_for": [
                "joint length fractions along actual limb centerlines",
                "limb region selection",
                "neutral torso height seeds",
            ],
            "not_claimed_as_estimate": True,
        }
    )
    return {
        "positions": positions,
        "bone_reports": bone_reports,
        "chain_reports": chain_reports,
        "trunk": trunk,
        "initialization": initialization,
        "body_f_range": body_f_range,
    }


def _quad_paw_clusters(coords: np.ndarray, center_a: float, target_bounds: dict):
    height = target_bounds["height"]
    width = float(target_bounds["span"][0])
    diagnostics = {"attempts": [], "missing": []}

    def make_paw(side, label, cluster):
        ground = float(np.min(cluster[:, 2]))
        bottom = cluster[cluster[:, 2] <= ground + height * 0.018]
        if len(bottom) < 4:
            bottom = cluster
        return {
            "side": side,
            "limb": label,
            "points": cluster,
            "ground": ground,
            "paw_top": float(np.max(cluster[:, 2])),
            "center_a": _median(bottom[:, 0]),
            "center_f": _median(bottom[:, 1]),
            "toe_tip": cluster[int(np.argmax(cluster[:, 1]))].copy(),
            "heel_f": float(np.min(cluster[:, 1])),
            "toe_f": float(np.max(cluster[:, 1])),
            "width": float(np.ptp(cluster[:, 0])),
            "depth": float(np.ptp(cluster[:, 1])),
            "count": int(len(cluster)),
        }

    best = {}
    best_diagnostics = diagnostics
    for low_fraction in (0.070, 0.100, 0.140, 0.180):
        low = coords[
            coords[:, 2] <= target_bounds["min"][2] + height * low_fraction
        ]
        attempt = {
            "low_fraction": low_fraction,
            "points": int(len(low)),
            "sides": {},
        }
        diagnostics["attempts"].append(attempt)
        if len(low) < 16:
            attempt["reason"] = "too_few_low_points"
            continue
        result = {}
        for side, sign in (("L", 1), ("R", -1)):
            side_points = low[sign * (low[:, 0] - center_a) >= -width * 0.01]
            side_info = {"points": int(len(side_points))}
            attempt["sides"][side] = side_info
            if len(side_points) < 8:
                side_info["reason"] = "too_few_side_points"
                continue
            first, second = _paw_cluster_2means(side_points[:, 1])
            clusters = [
                cluster
                for cluster in (first, second)
                if cluster is not None and len(cluster) >= 8
            ]
            if not clusters:
                side_info["reason"] = "no_paw_cluster"
                continue
            assignments = []
            if len(clusters) == 2:
                low_cluster, high_cluster = sorted(
                    clusters, key=lambda cluster: float(np.mean(cluster))
                )
                separation = float(
                    np.mean(high_cluster) - np.mean(low_cluster)
                )
                side_info["separation"] = separation
                if separation <= height * 0.10:
                    side_info["reason"] = "front_rear_not_separated"
                    continue
                assignments = [("rear", low_cluster), ("front", high_cluster)]
            else:
                cluster = clusters[0]
                side_center = float(np.median(side_points[:, 1]))
                label = "front" if float(np.mean(cluster)) > side_center else "rear"
                side_info["single_cluster_assigned"] = label
                assignments = [(label, cluster)]
            for label, values in assignments:
                mask = np.isin(side_points[:, 1], values)
                if not np.any(mask):
                    mask = np.min(
                        np.abs(
                            side_points[:, 1, None]
                            - np.asarray(values, dtype=np.float64)[None, :]
                        ),
                        axis=1,
                    ) <= max(float(np.ptp(values)) * 0.25, height * 1e-6)
                cluster = side_points[mask]
                if len(cluster) < 8:
                    side_info.setdefault("rejected", []).append(label)
                    continue
                result[(side, label)] = make_paw(side, label, cluster)
                side_info.setdefault("found", []).append(label)
        if len(result) > len(best):
            best = result
            best_diagnostics = {
                "attempts": list(diagnostics["attempts"]),
                "status": "partial_surface_paw_clusters"
                if len(result) < 4
                else "four_surface_paw_clusters",
                "low_fraction": low_fraction,
                "missing": [
                    "%s.%s" % (side, label)
                    for side in ("L", "R")
                    for label in ("front", "rear")
                    if (side, label) not in result
                ],
            }
        if len(result) == 4:
            return result, best_diagnostics
    best_diagnostics["status"] = "unestimated" if not best else best_diagnostics["status"]
    return best, best_diagnostics


def _quad_leg_candidates(
    coords: np.ndarray,
    *,
    side_sign: int,
    center_a: float,
    seed_polyline: np.ndarray,
    other_polyline: np.ndarray,
    paw: dict,
    target_bounds: dict,
    body_f_range,
    limb: str,
) -> tuple[np.ndarray, dict]:
    height = target_bounds["height"]
    body_length = float(body_f_range[1] - body_f_range[0])
    body_center_f = (body_f_range[0] + body_f_range[1]) * 0.5
    side_mask = side_sign * (coords[:, 0] - center_a) >= -float(target_bounds["span"][0]) * 0.01
    upper = float(seed_polyline[0][2]) + height * 0.07
    lower = float(paw["paw_top"]) - height * 0.025
    c_mask = (coords[:, 2] >= lower) & (coords[:, 2] <= upper)
    half_mask = (
        coords[:, 1] >= body_center_f - body_length * 0.05
        if limb == "front"
        else coords[:, 1] <= body_center_f + body_length * 0.05
    )
    distance = _polyline_distance(
        coords,
        _extended_polyline_absolute(seed_polyline, height * 0.05, height * 0.12),
    )
    other_distance = _polyline_distance(
        coords,
        _extended_polyline_absolute(other_polyline, height * 0.05, height * 0.12),
    )
    radius = max(height * 0.13, body_length * 0.07)
    mask = (
        side_mask
        & c_mask
        & half_mask
        & (distance <= radius)
        & (distance <= other_distance + body_length * 0.005)
    )
    selected = coords[mask]
    return selected, {
        "selection": "quadruped low-paw side/front-rear cluster with seed projection",
        "limb": limb,
        "side": "L" if side_sign > 0 else "R",
        "count": int(len(selected)),
        "radius": float(radius),
        "c_range": [float(lower), float(upper)],
        "body_center_f": float(body_center_f),
        "paw": {
            key: value
            for key, value in paw.items()
            if key not in {"points", "toe_tip"}
        },
    }


def _anchored_quad_seed(
    seed_polyline: np.ndarray,
    paw: dict,
    *,
    body_center_f: float,
    center_a: float,
    height: float,
    proximal_c: float | None = None,
) -> np.ndarray:
    """Neutral seed re-anchored to the actual low paw cluster.

    The reference-to-target proportion map can be biased by a long head/tail.
    The four low paw clusters are actual geometry, so the distal end and the
    inward-leaning proximal end are derived from them.  Joint fractions still
    use the reference rest lengths, and the final head/tail positions still
    come from the actual leg centerline.
    """
    seed_polyline = np.asarray(seed_polyline, dtype=np.float64)
    if len(seed_polyline) < 2:
        return seed_polyline.copy()
    lengths = np.linalg.norm(np.diff(seed_polyline, axis=0), axis=1)
    total = float(np.sum(lengths))
    if total <= _EPS:
        fractions = np.linspace(0.0, 1.0, len(seed_polyline))
    else:
        fractions = np.r_[0.0, np.cumsum(lengths) / total]
    paw_f = float(paw["center_f"])
    paw_a = float(paw["center_a"])
    ground = float(paw["ground"])
    proximal = np.array(
        [
            center_a + (paw_a - center_a) * 0.95,
            body_center_f + (paw_f - body_center_f) * 0.92,
            float(seed_polyline[0][2] if proximal_c is None else proximal_c),
        ],
        dtype=np.float64,
    )
    distal = np.array(
        [paw_a, paw_f, ground + height * 0.008],
        dtype=np.float64,
    )
    return np.asarray(
        [proximal + (distal - proximal) * fraction for fraction in fractions],
        dtype=np.float64,
    )


def _quad_bend_prior(
    reference_bones: dict,
    chain: list[str],
    proximal: np.ndarray,
    middle: np.ndarray,
    distal: np.ndarray,
    paw_base: np.ndarray,
    leg_points: np.ndarray,
    height: float,
    limb: str,
):
    """Keep the declared cat stifle/elbow bend direction on real surfaces.

    A limb surface centerline can be almost straight even when the internal
    skeleton has a dog/cat zig-zag (rear stifle forward, hock rear; front elbow
    rear, wrist forward).  The cat reference rest is the declared neutral
    anatomical prior.  When the actual centerline contradicts that prior, keep
    the actual joint height/lateral position but move the forward coordinate to
    the cat-seed chord offset and flag the joint for manual review.  This is a
    declared prior correction, never presented as an unqualified geometry fit.
    """
    ref_prox = np.asarray(reference_bones[chain[0]]["head"], dtype=np.float64)
    ref_middle = np.asarray(reference_bones[chain[1]]["head"], dtype=np.float64)
    ref_distal = np.asarray(reference_bones[chain[2]]["head"], dtype=np.float64)
    ref_paw = np.asarray(reference_bones[chain[2]]["tail"], dtype=np.float64)

    def chord_forward(start, end, point):
        if abs(float(end[2] - start[2])) <= 1e-9:
            return float(start[1])
        unit = (float(point[2]) - float(start[2])) / float(end[2] - start[2])
        return float(start[1] + unit * (end[1] - start[1]))

    ref_middle_delta = float(ref_middle[1]) - chord_forward(
        ref_prox, ref_paw, ref_middle
    )
    ref_distal_delta = float(ref_distal[1]) - chord_forward(
        ref_prox, ref_paw, ref_distal
    )
    actual_middle_delta = float(middle[1]) - chord_forward(
        proximal, paw_base, middle
    )
    actual_distal_delta = float(distal[1]) - chord_forward(
        proximal, paw_base, distal
    )
    ref_chord = float(np.linalg.norm(ref_paw - ref_prox))
    actual_chord = float(np.linalg.norm(paw_base - proximal))
    if ref_chord <= 1e-9:
        return middle, distal, {
            "status": "reference_chord_degenerate",
            "low_confidence": False,
            "applied": False,
        }
    scale = actual_chord / ref_chord
    tolerance = max(ref_chord * 0.008, 1e-6)
    labels = (
        ("elbow", "wrist") if limb == "front" else ("knee", "hock")
    )

    def evaluate(joint, reference_delta, actual_delta, label):
        info = {
            "joint": label,
            "reference_delta_forward": float(reference_delta),
            "actual_centerline_delta_forward": float(actual_delta),
            "scaled_prior_delta": float(reference_delta * scale),
        }
        if abs(float(reference_delta)) <= tolerance:
            info["status"] = "reference_seed_bend_not_defined"
            return joint.copy(), info, False, False
        expected_forward = float(reference_delta) > 0.0
        agrees = (
            actual_delta >= 0.0 if expected_forward else actual_delta <= 0.0
        )
        if agrees:
            info["status"] = "actual_centerline_agrees_with_cat_prior"
            return joint.copy(), info, False, False
        target_f = chord_forward(proximal, paw_base, joint) + float(
            reference_delta * scale
        )
        band = leg_points[
            np.abs(leg_points[:, 2] - float(joint[2]))
            <= max(height * 0.035, 1e-5)
        ]
        clamped = False
        if len(band) >= 4:
            low = float(np.min(band[:, 1])) + height * 0.004
            high = float(np.max(band[:, 1])) - height * 0.004
            if low <= high:
                clipped = float(np.clip(target_f, low, high))
                clamped = abs(clipped - target_f) > 1e-9
                target_f = clipped
        corrected = joint.copy()
        corrected[1] = target_f
        corrected_delta = float(corrected[1]) - chord_forward(
            proximal, paw_base, corrected
        )
        restored = (
            corrected_delta >= 0.0 if expected_forward else corrected_delta <= 0.0
        )
        info.update(
            {
                "status": (
                    "declared_cat_prior_bend_applied"
                    if restored
                    else "prior_could_not_restore_bend_direction"
                ),
                "clamped_to_actual_leg_forward_extent": bool(clamped),
                "corrected_forward_delta": corrected_delta,
                "review_required": True,
            }
        )
        # Even if clamping prevented the prior direction, keep the actual
        # centerline point and surface the low confidence for manual editing.
        return (corrected, info, True, True) if restored else (
            joint.copy(),
            info,
            False,
            True,
        )

    middle_final, middle_info, middle_review, middle_low = evaluate(
        middle, ref_middle_delta, actual_middle_delta, labels[0]
    )
    distal_final, distal_info, distal_review, distal_low = evaluate(
        distal, ref_distal_delta, actual_distal_delta, labels[1]
    )
    return middle_final, distal_final, {
        "reference_source": "cat_reference_rest",
        "reference_chord_length": ref_chord,
        "actual_chord_length": actual_chord,
        "scale": scale,
        "middle": middle_info,
        "distal": distal_info,
        "applied": bool(middle_review or distal_review),
        "low_confidence": bool(middle_low or distal_low),
        "review_required_joints": [
            info["joint"]
            for info in (middle_info, distal_info)
            if info.get("review_required")
        ],
    }


def _plan_quad_leg(
    *,
    coords: np.ndarray,
    reference_bones: dict,
    mapper: _Mapper,
    side: str,
    limb: str,
    paw: dict,
    front_polyline: np.ndarray,
    rear_polyline: np.ndarray,
    center_a: float,
    target_bounds: dict,
    body_f_range,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> None:
    sign = 1 if side == "L" else -1
    if limb == "front":
        chain = ["upper_arm." + side, "forearm." + side, "hand." + side]
        toe_name = "f_toe." + side
        chain_label = "forelimb." + side
        seed_polyline = front_polyline
        other_polyline = rear_polyline
    else:
        chain = ["thigh." + side, "shin." + side, "foot." + side]
        toe_name = "r_toe." + side
        chain_label = "hindlimb." + side
        seed_polyline = rear_polyline
        other_polyline = front_polyline
    names = chain + [toe_name]
    if any(name not in reference_bones for name in names):
        for name in names:
            _mark_unestimated(
                bone_reports, name, chain_label, side, "参考缺少完整四足肢体链"
            )
        chain_reports[chain_label] = {
            "status": "unestimated",
            "reason": "missing_reference_chain",
        }
        return
    mapped = [_mapped_bone(mapper, reference_bones, name) for name in chain]
    points, diagnostics = _quad_leg_candidates(
        coords,
        side_sign=sign,
        center_a=center_a,
        seed_polyline=seed_polyline,
        other_polyline=other_polyline,
        paw=paw,
        target_bounds=target_bounds,
        body_f_range=body_f_range,
        limb=limb,
    )
    height = target_bounds["height"]
    if len(points) < 20:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                chain_label,
                side,
                "四足低部足端已定位，但该腿独立表面不足（%d点）" % len(points),
                selection=diagnostics,
            )
        chain_reports[chain_label] = {
            "status": "unestimated",
            "reason": "insufficient_leg_surface",
            "limb": limb,
            "selection": diagnostics,
        }
        return
    upper_points = points[points[:, 2] >= float(paw["paw_top"]) - height * 0.01]
    centerline = _fit_centerline(
        upper_points,
        seed_polyline[0],
        seed_polyline[-1],
        bins=12,
        min_points=18,
        min_bins=3,
        min_bin_points=2,
    )
    if centerline is None:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                chain_label,
                side,
                "四足腿截面中心线不足",
                selection=diagnostics,
            )
        chain_reports[chain_label] = {
            "status": "unestimated",
            "reason": "centerline_failed",
            "limb": limb,
            "selection": diagnostics,
        }
        return
    fractions = _bone_length_fractions(reference_bones, chain)
    joint_fraction = 0.02 + float(fractions[0]) * 0.96
    proximal = _centerline_point(centerline, 0.02)
    middle = _centerline_point(centerline, joint_fraction)
    distal = _centerline_point(centerline, 0.985)
    if (
        float(np.linalg.norm(middle - proximal)) <= height * 0.012
        or float(np.linalg.norm(distal - middle)) <= height * 0.012
    ):
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                chain_label,
                side,
                "四足腿中心线关节过近或退化",
                selection=diagnostics,
            )
        chain_reports[chain_label] = {
            "status": "unestimated",
            "reason": "degenerate_centerline",
            "limb": limb,
            "selection": diagnostics,
        }
        return
    paw_base = np.array(
        [
            float(paw["center_a"]),
            float(paw["center_f"]),
            float(paw["ground"]) + height * 0.008,
        ],
        dtype=np.float64,
    )
    toe_tip = np.array(paw["toe_tip"], dtype=np.float64)
    if toe_tip[1] <= paw_base[1] + height * 0.003:
        toe_tip = np.array(
            [float(paw["center_a"]), float(paw["center_f"]) + height * 0.015, float(paw["ground"])],
            dtype=np.float64,
        )
    middle_prior, distal_prior, bend_prior = _quad_bend_prior(
        reference_bones,
        chain,
        proximal,
        middle,
        distal,
        paw_base,
        points,
        height,
        limb,
    )
    low_middle = bool(bend_prior["middle"].get("review_required"))
    low_distal = bool(bend_prior["distal"].get("review_required"))
    any_bend_review = bool(low_middle or low_distal)
    seed = {
        "method": "reference_rest_proportions_mapped_to_target_bounds",
        "chain_bones": chain,
        "joint_fraction": float(fractions[0]),
        "quadruped_limb": limb,
        "not_a_human_leg_rotation": True,
        "cat_bend_prior_used": bool(bend_prior.get("applied")),
    }
    actual = {
        "centerline_bins": centerline["filled_bins"],
        "support_points": centerline["support_points"],
        "centerline_length": centerline["length"],
        "paw": {
            key: value for key, value in paw.items() if key not in {"points", "toe_tip"}
        },
        "selection": diagnostics,
        "bend_prior": bend_prior,
    }
    method = (
        "surface_quadruped_leg_centerline_with_declared_cat_bend_prior"
        if any_bend_review
        else "surface_quadruped_leg_centerline_with_actual_paw"
    )

    def bend_extra(review):
        return {
            "confidence": "low_centerline_bend_prior" if review else "normal",
            "review_required": bool(review),
            "geometry_basis": (
                "actual_surface_centerline_with_declared_cat_bend_prior"
                if review
                else "actual_surface_centerline"
            ),
        }

    _add_position(
        positions,
        bone_reports,
        chain[0],
        head=proximal,
        tail=middle_prior,
        method=method,
        chain=chain_label,
        side=side,
        seed=seed,
        actual=actual,
        **bend_extra(low_middle),
    )
    _add_position(
        positions,
        bone_reports,
        chain[1],
        head=middle_prior,
        tail=distal_prior,
        method=method,
        chain=chain_label,
        side=side,
        seed=seed,
        actual=actual,
        **bend_extra(low_middle or low_distal),
    )
    _add_position(
        positions,
        bone_reports,
        chain[2],
        head=distal_prior,
        tail=paw_base,
        method="surface_quadruped_paw_base",
        chain=chain_label,
        side=side,
        seed=seed,
        actual=actual,
        **bend_extra(low_distal),
    )
    _add_position(
        positions,
        bone_reports,
        toe_name,
        head=paw_base,
        tail=toe_tip,
        method="surface_quadruped_paw_toe_extremum",
        chain=chain_label,
        side=side,
        seed=seed,
        actual=actual,
        toe_direction="forward",
    )
    chain_reports[chain_label] = {
        "status": "proposed_needs_review",
        "geometry_basis": (
            "quadruped_actual_surface_centerline_with_declared_cat_bend_prior"
            if any_bend_review
            else "quadruped_actual_surface_centerline_and_paw"
        ),
        "limb": limb,
        "bones": names,
        "support_points": centerline["support_points"],
        "centerline_bins": centerline["filled_bins"],
        "proximal": proximal.tolist(),
        "middle": middle_prior.tolist(),
        "distal": distal_prior.tolist(),
        "actual_centerline_middle": middle.tolist(),
        "actual_centerline_distal": distal.tolist(),
        "paw_base": paw_base.tolist(),
        "toe_tip": toe_tip.tolist(),
        "selection": diagnostics,
        "bend_prior": bend_prior,
        "confidence": "low_centerline_bend_prior" if any_bend_review else "normal",
        "review_required_joints": bend_prior.get("review_required_joints", []),
        "front_rear_model": "quadruped_%s_chain" % limb,
    }


def _quad_head_tip(coords: np.ndarray, target_bounds: dict, body_f_range):
    height = target_bounds["height"]
    body_length = float(body_f_range[1] - body_f_range[0])
    high = coords[coords[:, 2] >= target_bounds["min"][2] + height * 0.70]
    if not len(high):
        high = coords
    f_norm = (high[:, 1] - body_f_range[0]) / max(body_length, 1e-9)
    c_norm = (high[:, 2] - target_bounds["min"][2]) / max(height, 1e-9)
    score = f_norm + c_norm * 0.6
    count = max(5, int(len(high) * 0.01))
    chosen = high[np.argsort(score)[-count:]]
    return np.array(
        [
            _median(chosen[:, 0]),
            _median(chosen[:, 1]),
            float(np.max(chosen[:, 2])),
        ],
        dtype=np.float64,
    ), {
        "method": "surface_quadruped_head_front_top_extremum",
        "count": int(len(chosen)),
        "score": "normalized_forward + 0.6 * normalized_height",
    }


def _quad_spine_joints(
    coords: np.ndarray,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
    enabled: list[str],
    body_f_range,
    center_a: float,
) -> tuple[dict | None, dict]:
    names = ["spine", "spine.001", "spine.002", "spine.003"]
    if "head" in enabled:
        names.extend(["spine.004", "spine.005", "spine.006"])
    if any(name not in reference_bones for name in names):
        return None, {"status": "unestimated", "reason": "missing_spine_chain"}
    body_length = float(body_f_range[1] - body_f_range[0])
    height = target_bounds["height"]
    joints = []
    for name in names:
        bone = reference_bones[name]
        for endpoint in ("head", "tail"):
            f_fraction = (
                (bone[endpoint][1] - reference_bounds["min"][1])
                / max(float(reference_bounds["max"][1] - reference_bounds["min"][1]), 1e-9)
            )
            c_fraction = _reference_fraction(reference_bounds, bone, endpoint)
            joints.append(
                {
                    "name": f"{name}/{endpoint}",
                    "source_bone": name,
                    "endpoint": endpoint,
                    "f_fraction": float(f_fraction),
                    "c_fraction": float(c_fraction),
                    "seed_f": float(body_f_range[0] + f_fraction * body_length),
                    "seed_c": float(
                        target_bounds["min"][2] + c_fraction * height
                    ),
                }
            )
    deduplicated = []
    for joint in joints:
        if deduplicated and (
            abs(joint["seed_f"] - deduplicated[-1]["seed_f"]) <= body_length * 1e-5
            and abs(joint["seed_c"] - deduplicated[-1]["seed_c"]) <= height * 1e-5
        ):
            deduplicated[-1]["aliases"].append(joint["name"])
            continue
        joint["aliases"] = [joint["name"]]
        deduplicated.append(joint)
    if "head" in enabled:
        tip, tip_meta = _quad_head_tip(coords, target_bounds, body_f_range)
        deduplicated[-1]["actual"] = {
            "a": float(tip[0]),
            "f": float(tip[1]),
            "c": float(tip[2]),
            "count": int(tip_meta["count"]),
            "method": tip_meta["method"],
        }
    sections = []
    valid = []
    for joint in deduplicated:
        if "actual" in joint:
            sections.append(joint["actual"])
            valid.append(len(sections) - 1)
            continue
        section = _section_metrics_at_f(
            coords,
            joint["seed_f"],
            max(body_length * 0.025, height * 0.025),
            center_a,
        )
        if section is not None:
            joint["actual"] = {
                "a": float(section["center_a"]),
                "f": float(joint["seed_f"]),
                "c": float(section["center_c"]),
                "count": int(section["count"]),
                "width": float(section["width"]),
                "height": float(section["height"]),
                "method": "surface_quadruped_longitudinal_section_center",
            }
            sections.append(joint["actual"])
            valid.append(len(sections) - 1)
        else:
            sections.append(None)
    if not valid:
        return None, {"status": "unestimated", "reason": "no_longitudinal_sections"}
    for index, section in enumerate(sections):
        if section is not None:
            continue
        lower = max((item for item in valid if item < index), default=None)
        upper = min((item for item in valid if item > index), default=None)
        if lower is not None and upper is not None:
            low, high = sections[lower], sections[upper]
            unit = (index - lower) / (upper - lower)
            sections[index] = {
                "a": low["a"] * (1.0 - unit) + high["a"] * unit,
                "f": deduplicated[index]["seed_f"],
                "c": low["c"] * (1.0 - unit) + high["c"] * unit,
                "count": 0,
                "method": "interpolated_between_actual_sections",
            }
        else:
            nearest = min(valid, key=lambda item: abs(item - index))
            base = sections[nearest]
            sections[index] = {
                "a": float(base["a"]),
                "f": deduplicated[index]["seed_f"],
                "c": deduplicated[index]["seed_c"],
                "count": 0,
                "method": "proportion_extrapolation_not_geometry_estimate",
            }
    for joint, section in zip(deduplicated, sections):
        joint["actual"] = section
    f_values = [section["f"] for section in sections]
    if any(
        f_values[index + 1] <= f_values[index] + body_length * 0.002
        for index in range(len(f_values) - 1)
    ):
        return None, {
            "status": "unestimated",
            "reason": "quadruped_spine_not_forward_monotonic",
            "f_values": f_values,
        }
    lengths = []
    for index in range(len(sections) - 1):
        delta = np.array(
            [
                sections[index + 1]["a"] - sections[index]["a"],
                sections[index + 1]["f"] - sections[index]["f"],
                sections[index + 1]["c"] - sections[index]["c"],
            ]
        )
        lengths.append(float(np.linalg.norm(delta)))
    if any(length <= body_length * 0.01 for length in lengths):
        return None, {
            "status": "unestimated",
            "reason": "degenerate_quadruped_spine_segments",
            "segment_lengths": lengths,
        }
    return {
        "names": names,
        "joints": [
            {
                "name": joint["name"],
                "aliases": joint["aliases"],
                "f_fraction": joint["f_fraction"],
                "c_fraction": joint["c_fraction"],
                "seed_f": joint["seed_f"],
                "seed_c": joint["seed_c"],
                "actual": joint["actual"],
            }
            for joint in deduplicated
        ],
        "segment_lengths": lengths,
    }, {"status": "proposed_needs_review"}


def _plan_quad_spine(
    *,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
    enabled: list[str],
    coords: np.ndarray,
    body_f_range,
    center_a: float,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> dict | None:
    plan, status = _quad_spine_joints(
        coords,
        reference_bones,
        reference_bounds,
        target_bounds,
        enabled,
        body_f_range,
        center_a,
    )
    names = ["spine", "spine.001", "spine.002", "spine.003"]
    if "head" in enabled:
        names.extend(["spine.004", "spine.005", "spine.006"])
    if plan is None:
        for name in names:
            _mark_unestimated(
                bone_reports,
                name,
                "torso",
                "CENTER",
                status.get("reason", "四足躯干纵向截面不足"),
            )
        chain_reports["torso"] = status
        return None
    joints = plan["joints"]
    actuals = [
        np.array(
            [row["actual"]["a"], row["actual"]["f"], row["actual"]["c"]],
            dtype=np.float64,
        )
        for row in joints
    ]
    seed = {
        "method": "reference_rest_four_legged_proportions_mapped_to_body_bounds",
        "joint_f_fractions": [row["f_fraction"] for row in joints],
        "joint_c_fractions": [row["c_fraction"] for row in joints],
        "declared_not_geometry": True,
    }
    for index, name in enumerate(names):
        head = actuals[index]
        tail = actuals[index + 1]
        head_method = joints[index]["actual"].get("method", "")
        tail_method = joints[index + 1]["actual"].get("method", "")
        basis = (
            "actual_surface_section"
            if not str(head_method).startswith("proportion")
            and not str(tail_method).startswith("proportion")
            else "proportion_extrapolation_not_geometry_estimate"
        )
        _add_position(
            positions,
            bone_reports,
            name,
            head=head,
            tail=tail,
            method="surface_quadruped_longitudinal_section_center",
            chain="torso",
            side="CENTER",
            seed=seed,
            actual={
                "joint_head": joints[index]["actual"],
                "joint_tail": joints[index + 1]["actual"],
                "segment_length": float(np.linalg.norm(tail - head)),
            },
            geometry_basis=basis,
        )
    chain_reports["torso"] = {
        "status": "proposed_needs_review",
        "geometry_basis": "actual_longitudinal_surface_sections_with_neutral_four_legged_seeds",
        "bones": names,
        "joints": [
            {
                "name": row["name"],
                "aliases": row["aliases"],
                "f_fraction": row["f_fraction"],
                "seed_f": row["seed_f"],
                "seed_c": row["seed_c"],
                "actual": row["actual"],
            }
            for row in joints
        ],
        "segment_lengths": plan["segment_lengths"],
    }
    return {
        "joints": joints,
        "actuals": actuals,
        "segment_lengths": plan["segment_lengths"],
    }


def _quad_trunk_sections(
    coords: np.ndarray,
    *,
    reference_bones: dict,
    mapper: _Mapper,
    target_bounds: dict,
    body_f_range,
    center_a: float,
) -> dict:
    body_length = float(body_f_range[1] - body_f_range[0])
    height = target_bounds["height"]
    result = {}
    for label, bone_name in (
        ("chest_width", "spine.003"),
        ("pelvis_width", "spine"),
    ):
        if bone_name not in reference_bones:
            result[label] = {"status": "unestimated"}
            continue
        mapped = _mapped_bone(mapper, reference_bones, bone_name)
        level = float(mapped["head"][1])
        section = _section_metrics_at_f(
            coords, level, max(body_length * 0.030, height * 0.020), center_a
        )
        if section is None:
            result[label] = {"status": "unestimated", "level_f": level}
        else:
            result[label] = {
                "status": "actual_section_estimate",
                "level_f": level,
                "width": float(section["width"]),
                "height": float(section["height"]),
                "center_a": float(section["center_a"]),
                "center_c": float(section["center_c"]),
                "sample_points": int(section["count"]),
            }
    return result


def _plan_quad_attachments(
    *,
    coords: np.ndarray,
    reference_bones: dict,
    target_bounds: dict,
    body_f_range,
    center_a: float,
    positions: dict,
    bone_reports: dict,
    chain_reports: dict,
) -> None:
    body_length = float(body_f_range[1] - body_f_range[0])
    spine = positions.get("spine")
    if spine is None or spine.get("head") is None:
        for name in ("pelvis.L", "pelvis.R", "pelvis.C"):
            if name in reference_bones:
                _mark_unestimated(
                    bone_reports, name, "torso", "CENTER", "躯干尾端锚点未估计"
                )
        return
    spine_head = spine["head"]
    hips = {}
    shoulders = {}
    for side in ("L", "R"):
        thigh = positions.get("thigh." + side)
        upper_arm = positions.get("upper_arm." + side)
        if thigh is not None and thigh.get("head") is not None:
            hips[side] = thigh["head"]
        if upper_arm is not None and upper_arm.get("head") is not None:
            shoulders[side] = upper_arm["head"]
        pelvis_name = "pelvis." + side
        if pelvis_name in reference_bones:
            if side in hips:
                hip = hips[side]
                _add_position(
                    positions,
                    bone_reports,
                    pelvis_name,
                    head=spine_head,
                    tail=hip,
                    method="surface_quadruped_pelvis_root_to_actual_hip",
                    chain="hindlimb." + side,
                    side=side,
                    actual={
                        "spine_root": spine_head.tolist(),
                        "hip_joint": hip.tolist(),
                        "parent_preserved": reference_bones[pelvis_name]["parent"],
                        "use_connect_preserved": reference_bones[pelvis_name]["use_connect"],
                    },
                )
            else:
                _mark_unestimated(
                    bone_reports,
                    pelvis_name,
                    "hindlimb." + side,
                    side,
                    "该侧髋关节未由独立后腿表面估计，pelvis 不填比例初值",
                )
        shoulder_name = "shoulder." + side
        if shoulder_name in reference_bones:
            if side in shoulders:
                joint = shoulders[side]
                section = _section_metrics_at_f(
                    coords,
                    float(joint[1]),
                    max(body_length * 0.035, target_bounds["height"] * 0.02),
                    center_a,
                )
                if section is None:
                    root = np.array(
                        [center_a, float(joint[1]), float(joint[2])],
                        dtype=np.float64,
                    )
                else:
                    root = np.array(
                        [section["center_a"], float(section["level"]), float(joint[2])],
                        dtype=np.float64,
                    )
                _add_position(
                    positions,
                    bone_reports,
                    shoulder_name,
                    head=root,
                    tail=joint,
                    method="surface_quadruped_torso_section_shoulder_root",
                    chain="forelimb." + side,
                    side=side,
                    actual={
                        "shoulder_joint": joint.tolist(),
                        "root": root.tolist(),
                        "torso_section": section,
                        "parent_preserved": reference_bones[shoulder_name]["parent"],
                        "use_connect_preserved": reference_bones[shoulder_name]["use_connect"],
                    },
                )
            else:
                _mark_unestimated(
                    bone_reports,
                    shoulder_name,
                    "forelimb." + side,
                    side,
                    "该侧肩关节未由独立前腿表面估计，shoulder 不填比例初值",
                )
    if "pelvis.C" in reference_bones:
        if len(hips) == 2:
            midpoint = (hips["L"] + hips["R"]) * 0.5
            _add_position(
                positions,
                bone_reports,
                "pelvis.C",
                head=spine_head,
                tail=midpoint,
                method="surface_quadruped_center_pelvis_from_actual_hips",
                chain="torso",
                side="CENTER",
                actual={
                    "left_hip": hips["L"].tolist(),
                    "right_hip": hips["R"].tolist(),
                    "parent_preserved": reference_bones["pelvis.C"]["parent"],
                    "use_connect_preserved": reference_bones["pelvis.C"]["use_connect"],
                },
            )
        else:
            _mark_unestimated(
                bone_reports,
                "pelvis.C",
                "torso",
                "CENTER",
                "左右髋关节未全部由几何估计，pelvis.C 不填比例初值",
            )


def _plan_quadruped(
    *,
    reference,
    record: dict,
    enabled: list[str],
    frame: dict,
    coords: np.ndarray,
    reference_bones: dict,
    reference_bounds: dict,
    target_bounds: dict,
    body_f_range,
) -> dict:
    center_a = float(target_bounds["center"][0])
    # Refine the median plane from central sections.
    centers = []
    body_length = float(body_f_range[1] - body_f_range[0])
    for fraction in np.linspace(0.25, 0.80, 7):
        level = body_f_range[0] + body_length * float(fraction)
        section = _section_metrics_at_f(
            coords, level, max(body_length * 0.03, 1e-4), center_a
        )
        if section is not None:
            centers.append(section["center_a"])
    if centers:
        center_a = _median(centers, center_a)
    mapper = _Mapper(
        reference_bounds["min"],
        reference_bounds["max"],
        np.array(
            [target_bounds["min"][0], body_f_range[0], target_bounds["min"][2]],
            dtype=np.float64,
        ),
        np.array(
            [target_bounds["max"][0], body_f_range[1], target_bounds["max"][2]],
            dtype=np.float64,
        ),
    )
    positions = {}
    bone_reports = {}
    chain_reports = {}
    paws, paw_diagnostics = _quad_paw_clusters(coords, center_a, target_bounds)
    seed_polylines = {}
    for side in ("L", "R"):
        for limb, chain in (
            ("front", ["upper_arm." + side, "forearm." + side, "hand." + side]),
            ("rear", ["thigh." + side, "shin." + side, "foot." + side]),
        ):
            if all(name in reference_bones for name in chain):
                rows = [_mapped_bone(mapper, reference_bones, name) for name in chain]
                seed_polylines[(side, limb)] = np.asarray(
                    [rows[0]["head"]] + [row["tail"] for row in rows],
                    dtype=np.float64,
                )
    body_center_f = float((body_f_range[0] + body_f_range[1]) * 0.5)
    for (side, limb), seed in list(seed_polylines.items()):
        paw = paws.get((side, limb))
        if paw is not None:
            section_f = body_center_f + (
                float(paw["center_f"]) - body_center_f
            ) * 0.35
            section = _section_metrics_at_f(
                coords,
                section_f,
                max(body_length * 0.035, target_bounds["height"] * 0.03),
                center_a,
            )
            proximal_c = (
                float(seed[0][2]) if section is None else float(section["center_c"])
            )
            proximal_c = float(
                np.clip(
                    proximal_c + target_bounds["height"] * 0.02,
                    float(paw["paw_top"]) + target_bounds["height"] * 0.18,
                    target_bounds["max"][2] - target_bounds["height"] * 0.02,
                )
            )
            seed_polylines[(side, limb)] = _anchored_quad_seed(
                seed,
                paw,
                body_center_f=body_center_f,
                center_a=center_a,
                height=target_bounds["height"],
                proximal_c=proximal_c,
            )
    for side in ("L", "R"):
        for limb, module in (
            ("front", "forelimb." + side),
            ("rear", "hindlimb." + side),
        ):
            paw = paws.get((side, limb))
            front_polyline = seed_polylines.get((side, "front"))
            rear_polyline = seed_polylines.get((side, "rear"))
            if module not in enabled:
                continue
            chain = (
                ["upper_arm." + side, "forearm." + side, "hand." + side]
                if limb == "front"
                else ["thigh." + side, "shin." + side, "foot." + side]
            )
            toe = ("f_toe." if limb == "front" else "r_toe.") + side
            all_names = chain + [toe]
            if (
                paw is None
                or front_polyline is None
                or rear_polyline is None
            ):
                for name in all_names:
                    if name in reference_bones:
                        _mark_unestimated(
                            bone_reports,
                            name,
                            module,
                            side,
                            "低部表面未形成该侧前后独立足端聚类；不做比例凑数",
                            paw_diagnostics=paw_diagnostics,
                        )
                chain_reports[module] = {
                    "status": "unestimated",
                    "reason": "missing_independent_paw_cluster",
                    "limb": limb,
                    "side": side,
                    "paw_diagnostics": paw_diagnostics,
                }
                continue
            _plan_quad_leg(
                coords=coords,
                reference_bones=reference_bones,
                mapper=mapper,
                side=side,
                limb=limb,
                paw=paw,
                front_polyline=front_polyline,
                rear_polyline=rear_polyline,
                center_a=center_a,
                target_bounds=target_bounds,
                body_f_range=body_f_range,
                positions=positions,
                bone_reports=bone_reports,
                chain_reports=chain_reports,
            )
    spine_plan = _plan_quad_spine(
        reference_bones=reference_bones,
        reference_bounds=reference_bounds,
        target_bounds=target_bounds,
        enabled=enabled,
        coords=coords,
        body_f_range=body_f_range,
        center_a=center_a,
        positions=positions,
        bone_reports=bone_reports,
        chain_reports=chain_reports,
    )
    _plan_quad_attachments(
        coords=coords,
        reference_bones=reference_bones,
        target_bounds=target_bounds,
        body_f_range=body_f_range,
        center_a=center_a,
        positions=positions,
        bone_reports=bone_reports,
        chain_reports=chain_reports,
    )
    if "tail" in enabled:
        spine = positions.get("spine")
        base = None if spine is None else spine.get("head")
        _plan_tail(
            coords=coords,
            reference_bones=reference_bones,
            reference_bounds=reference_bounds,
            target_bounds=target_bounds,
            body_f_range=body_f_range,
            center_a=center_a,
            trunk_center_f=float((body_f_range[0] + body_f_range[1]) * 0.5),
            kind="QUADRUPED",
            base_point=base,
            positions=positions,
            bone_reports=bone_reports,
            chain_reports=chain_reports,
        )
    trunk_sections = _quad_trunk_sections(
        coords,
        reference_bones=reference_bones,
        mapper=mapper,
        target_bounds=target_bounds,
        body_f_range=body_f_range,
        center_a=center_a,
    )
    trunk = {
        "center_a": center_a,
        "center_f": float((body_f_range[0] + body_f_range[1]) * 0.5),
        "body_f_range": [float(body_f_range[0]), float(body_f_range[1])],
        "body_f_range_method": body_f_range[2],
        "paws": {
            "%s.%s" % (side, limb): {
                key: value
                for key, value in paw.items()
                if key not in {"points", "toe_tip"}
            }
            for (side, limb), paw in paws.items()
        },
        "paw_diagnostics": paw_diagnostics,
        **trunk_sections,
    }
    initialization = mapper.as_dict()
    initialization.update(
        {
            "reference_bbox_basis": "reference_rest_non_tail_bone_points",
            "target_forward_bounds": "actual_longitudinal_body_range",
            "used_for": [
                "front/rear limb seed separation",
                "joint length fractions along actual leg centerlines",
                "spine longitudinal proportion seeds",
            ],
            "not_claimed_as_estimate": True,
        }
    )
    return {
        "positions": positions,
        "bone_reports": bone_reports,
        "chain_reports": chain_reports,
        "trunk": trunk,
        "initialization": initialization,
        "body_f_range": body_f_range,
        "paws": paws,
    }


def _bone_depth(bone) -> int:
    depth = 0
    current = bone
    while current.parent is not None:
        depth += 1
        current = current.parent
    return depth


@contextmanager
def _edit_session(reference):
    if bpy.context.mode != "OBJECT":
        raise PositioningError("定位编辑需要当前为 Object Mode")
    view = bpy.context.view_layer
    if view.objects.get(reference.name) != reference:
        raise PositioningError("参考不在当前 View Layer，无法编辑")
    active = view.objects.active
    selected = list(bpy.context.selected_objects)
    hidden = reference.hide_get()
    viewport_hidden = bool(reference.hide_viewport)
    try:
        if viewport_hidden:
            reference.hide_viewport = False
        if hidden:
            reference.hide_set(False)
        for obj in selected:
            try:
                obj.select_set(False)
            except ReferenceError:
                pass
        reference.select_set(True)
        view.objects.active = reference
        if not bpy.ops.object.mode_set.poll():
            raise PositioningError("无法进入参考编辑模式")
        bpy.ops.object.mode_set(mode="EDIT")
        yield reference.data.edit_bones
    finally:
        try:
            if reference.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except (RuntimeError, ReferenceError):
            pass
        try:
            reference.select_set(False)
        except ReferenceError:
            pass
        try:
            reference.hide_viewport = viewport_hidden
            reference.hide_set(hidden)
        except (RuntimeError, ReferenceError):
            pass
        for obj in selected:
            try:
                if view.objects.get(obj.name) == obj:
                    obj.select_set(True)
            except (ReferenceError, AttributeError):
                pass
        try:
            view.objects.active = active
        except (ReferenceError, AttributeError):
            view.objects.active = None
        try:
            view.update()
        except RuntimeError:
            pass


def _capture_edit_snapshot(edit_bones) -> dict:
    return {
        bone.name: (bone.head.copy(), bone.tail.copy(), float(bone.roll))
        for bone in edit_bones
    }


def _restore_edit_snapshot(edit_bones, snapshot: dict) -> None:
    for name in sorted(
        snapshot,
        key=lambda item: _bone_depth(edit_bones[item]) if item in edit_bones else 0,
    ):
        if name not in edit_bones:
            continue
        head, tail, roll = snapshot[name]
        bone = edit_bones[name]
        bone.tail = tail
        if not bone.use_connect:
            bone.head = head
        bone.roll = roll


def _verify_chain_continuity(edit_bones, height: float) -> dict:
    errors = []
    maximum = 0.0
    for bone in edit_bones:
        if not bone.use_connect or bone.parent is None:
            continue
        error = float((bone.head - bone.parent.tail).length)
        maximum = max(maximum, error)
        if error > max(height * 1e-5, 1e-7):
            errors.append({"bone": bone.name, "parent": bone.parent.name, "error": error})
    return {"max_error": maximum, "errors": errors}


def _apply_positions(
    reference,
    local_positions: dict,
    *,
    height: float,
    roll_overrides: dict | None = None,
) -> dict:
    """Transactional edit of head/tail (and optional roll) only."""
    if not local_positions and not roll_overrides:
        return {"changed": 0, "max_continuity_error": 0.0, "continuity_errors": []}
    with _edit_session(reference) as edit_bones:
        snapshot = _capture_edit_snapshot(edit_bones)
        changed = 0
        try:
            # Parents before children so connected heads follow the planned
            # parent tails.  Only tails of connected bones are written; their
            # heads are owned by the parent.
            ordered = sorted(
                set(local_positions) | set(roll_overrides or {}),
                key=lambda name: _bone_depth(edit_bones[name]),
            )
            for name in ordered:
                bone = edit_bones[name]
                row = local_positions.get(name)
                if row is not None:
                    head = row.get("head")
                    tail = row.get("tail")
                    if tail is not None:
                        bone.tail = Vector(tail)
                        changed += 1
                    if head is not None and not bone.use_connect:
                        bone.head = Vector(head)
                if roll_overrides and name in roll_overrides:
                    bone.roll = float(roll_overrides[name])
            minimum = max(float(height) * 1e-6, 1e-8)
            for name in local_positions:
                bone = edit_bones[name]
                if float(bone.length) <= minimum:
                    raise PositioningError("关节候选产生零长骨：" + name)
            continuity = _verify_chain_continuity(edit_bones, float(height))
            if continuity["errors"]:
                raise PositioningError(
                    "关节候选破坏连续链："
                    + _canonical(continuity["errors"][:4])
                )
        except Exception:
            _restore_edit_snapshot(edit_bones, snapshot)
            raise
    return {
        "changed": changed,
        "max_continuity_error": continuity["max_error"],
        "continuity_errors": continuity["errors"],
    }


def _restore_positions(reference, snapshot: dict, height: float) -> None:
    with _edit_session(reference) as edit_bones:
        _restore_edit_snapshot(edit_bones, snapshot)
    _ = height


def _topology_equal(left: dict, right: dict) -> tuple[bool, list[str]]:
    if left.get("sha256") == right.get("sha256"):
        return True, []
    left_rows = {row["name"]: row for row in left.get("bones", [])}
    right_rows = {row["name"]: row for row in right.get("bones", [])}
    differences = []
    for name in sorted(set(left_rows) | set(right_rows)):
        if left_rows.get(name) != right_rows.get(name):
            differences.append(name)
    return False, differences


def _finalize_bone_reports(reference, bone_reports: dict, chain_reports: dict) -> dict:
    result = _deep(bone_reports)
    for bone in reference.data.bones:
        name = bone.name
        if name in result:
            continue
        reason = "当前估计器未覆盖该骨"
        chain_label = None
        for label, chain in chain_reports.items():
            if name in chain.get("bones", []):
                chain_label = label
                if chain.get("status") == "unestimated":
                    reason = chain.get("reason", reason)
                break
        if name.startswith("shoulder."):
            side = name.rsplit(".", 1)[-1]
            chain = chain_reports.get("arm." + side)
            if chain is not None and chain.get("status") == "unestimated":
                reason = chain.get("reason", "肩关节所属手臂未独立估计")
        if name.startswith("pelvis."):
            side = name.rsplit(".", 1)[-1]
            if side in ("L", "R"):
                chain = chain_reports.get("leg." + side)
                if chain is not None and chain.get("status") == "unestimated":
                    reason = chain.get("reason", "髋关节所属腿未独立估计")
        result[name] = {
            "status": "unestimated",
            "method": "unestimated",
            "geometry_basis": "none",
            "chain": chain_label,
            "side": "CENTER"
            if not name.endswith((".L", ".R"))
            else name.rsplit(".", 1)[-1],
            "reason": reason,
        }
    return result


def _summary_from_reports(
    reference, bone_reports: dict, chain_reports: dict
) -> dict:
    estimated = [
        name
        for name in reference.data.bones.keys()
        if bone_reports.get(name, {}).get("status") == "proposed_needs_review"
    ]
    unestimated = [
        name
        for name in reference.data.bones.keys()
        if bone_reports.get(name, {}).get("status") == "unestimated"
    ]
    low_confidence = [
        name
        for name in reference.data.bones.keys()
        if bone_reports.get(name, {}).get("review_required")
    ]
    return {
        "estimated_bone_count": len(estimated),
        "unestimated_bone_count": len(unestimated),
        "low_confidence_bone_count": len(low_confidence),
        "low_confidence_bones": sorted(low_confidence),
        "total_bone_count": len(reference.data.bones),
        "partial": bool(unestimated),
        "estimated_bones": sorted(estimated),
        "unestimated_bones": sorted(unestimated),
        "chains": {
            label: chain.get("status", "unknown")
            for label, chain in sorted(chain_reports.items())
        },
    }


def _world_and_local_cloud(reference, targets, inverse=None):
    if inverse is None:
        inverse = reference.matrix_world.inverted()
    rotation = np.array(inverse.to_3x3(), dtype=np.float64)
    translation = np.array(inverse.translation, dtype=np.float64)
    world_parts = []
    local_parts = []
    target_rows = []
    for obj in targets:
        mesh = obj.data
        vertices = _mesh_vertices(mesh)
        matrix = np.array(obj.matrix_world, dtype=np.float64)
        world = vertices @ matrix[:3, :3].T + matrix[:3, 3]
        local = world @ rotation.T + translation
        world_parts.append(world)
        local_parts.append(local)
        target_rows.append(
            {
                "name": obj.name,
                "object_pointer": int(obj.as_pointer()),
                "data_name": mesh.name,
                "data_pointer": int(mesh.as_pointer()),
                "vertex_count": int(len(vertices)),
                "polygon_count": int(len(mesh.polygons)),
                "matrix_world": _matrix_hex(obj.matrix_world),
            }
        )
    return (
        np.vstack(world_parts),
        np.vstack(local_parts),
        target_rows,
    )


def _public_axes(frame: dict, forward_axis: str, up_axis: str) -> dict:
    return {
        "forward": forward_axis,
        "up": up_axis,
        "local_forward": _vec(frame["forward"]),
        "local_up": _vec(frame["up"]),
        "local_left": _vec(frame["left"]),
        "coordinate_transform": "reference.matrix_world.inverted() @ target world vertices",
        "supported": "Z-up and horizontal +/-X or +/-Y forward",
    }


def _require_reference_orientation(reference_bones: dict, kind: str) -> None:
    rear = reference_bones.get("spine", {}).get("head")
    front_name = "spine.006" if "spine.006" in reference_bones else "spine.003"
    front = reference_bones.get(front_name, {}).get("tail")
    if rear is None or front is None:
        raise PositioningError("参考缺少 spine 方向，不能确定朝向")
    direction = np.asarray(front, dtype=np.float64) - np.asarray(rear, dtype=np.float64)
    length = float(np.linalg.norm(direction))
    if length <= 1e-8:
        raise PositioningError("参考 spine 方向退化，不能确定朝向")
    if kind == "HUMAN" and abs(float(direction[2])) < length * 0.40:
        raise PositioningError(
            "人形参考自身不是直立方向；请先把参考与目标对齐到 Z-up 和请求的 ±X/±Y 前向。"
        )
    if kind == "QUADRUPED" and abs(float(direction[1])) < length * 0.40:
        raise PositioningError(
            "四足参考自身不是水平前后方向；请先把参考与目标对齐到 Z-up 和请求的 ±X/±Y 前向。"
        )
    for toe_name in ("toe.L", "f_toe.L", "r_toe.L"):
        toe = reference_bones.get(toe_name)
        if toe is None:
            continue
        toe_direction = np.asarray(toe["tail"], dtype=np.float64) - np.asarray(
            toe["head"], dtype=np.float64
        )
        horizontal = float(np.linalg.norm(toe_direction[:2]))
        if horizontal > 1e-4 and abs(float(toe_direction[1])) / horizontal < 0.30:
            raise PositioningError(
                "参考脚趾前向与请求的 ±X/±Y 不一致；请先把参考与目标对齐到同一前向。"
            )
        break


def _require_target_neutral_pose(record_kind: str, target_bounds: dict) -> None:
    span = target_bounds["span"]
    if record_kind == "HUMAN" and span[2] < max(span[0], span[1]) * 0.55:
        raise PositioningError(
            "人形目标不是明显直立的 A-like 中性姿态；请先对齐，"
            "当前估计不承诺任意姿态识别。"
        )
    if record_kind == "QUADRUPED" and span[1] < span[2] * 0.55:
        raise PositioningError(
            "四足目标不是明显站立的前后展开姿态；请先对齐，"
            "当前估计不承诺任意姿态识别。"
        )


def _localize_positions(frame: dict, positions: dict) -> dict:
    result = {}
    for name, row in positions.items():
        head = row.get("head")
        tail = row.get("tail")
        if head is None and tail is None:
            continue
        result[name] = {
            "head": None
            if head is None
            else np.array(_local_point(head[0], head[1], head[2], frame)),
            "tail": None
            if tail is None
            else np.array(_local_point(tail[0], tail[1], tail[2], frame)),
        }
    return result


def _validation_warnings(targets, coords, target_bounds, kind, plan) -> list[str]:
    warnings = []
    span = target_bounds["span"]
    if kind == "HUMAN" and span[2] < max(span[0], span[1]) * 0.8:
        warnings.append(
            "人形输入不是明显直立的中性姿态；当前估计只承诺直立/A-like 中性候选，"
            "请先对齐或人工复核。"
        )
    if kind == "QUADRUPED" and span[1] < span[2] * 0.7:
        warnings.append(
            "四足输入的前后跨度不占主导；可能是坐姿/俯视姿态，当前估计只承诺站立四足。"
        )
    for obj in targets:
        if obj.modifiers:
            warnings.append(
                "目标 %s 有修改器；估计使用原始 Mesh 数据，不应用修改器（不改源）。"
                % obj.name
            )
        if obj.vertex_groups:
            warnings.append(
                "目标 %s 有顶点组；只做只读 fingerprint 保护，不读取旧权重作为定位答案。"
                % obj.name
            )
        if obj.data.shape_keys is not None:
            active_keys = [
                block.name
                for block in obj.data.shape_keys.key_blocks
                if block != obj.data.shape_keys.reference_key
                and abs(float(block.value)) > 1e-8
            ]
            if active_keys:
                warnings.append(
                    "目标 %s 有非零 Shape Key（%s）；估计使用原始 basis Mesh，"
                    "不评估 Shape Key。"
                    % (obj.name, ", ".join(active_keys[:4]))
                )
    if plan.get("trunk", {}).get("width_source") == "bbox_fallback_width_not_an_estimate":
        warnings.append("胸/盆宽横截面不足，肢体分离用了明确标注的 bbox fallback 宽度。")
    body_range = plan.get("body_f_range")
    if body_range is not None and len(body_range) >= 3 and str(body_range[2]).startswith("fallback"):
        warnings.append("四足纵向身体范围使用了 fallback：" + str(body_range[2]))
    return warnings


def estimate(reference, target_objects, *, forward_axis="-Y", up_axis="Z") -> dict:
    """Estimate proposed joint positions from the current raw target surface.

    The function edits only the modular reference's bone head/tail rest values.
    It never reads a target armature/old weights as an answer and never calls
    ``confirm_reference``.
    """
    record, enabled, expected = _parse_reference(reference)
    if "shoulder_girdle" in enabled:
        raise PositioningError("独立肩胛模块需要按真实解剖手动定位AC/GH及肩胛平面；当前表面estimate不能代替此项，未修改候选")
    _require_object_mode = bpy.context.mode == "OBJECT"
    if not _require_object_mode:
        raise PositioningError("estimate 需要当前为 Object Mode")
    targets = _normalize_targets(target_objects)
    pose_before = _require_reference_pose(reference)
    forward_name, forward_world = _normalize_axis(
        forward_axis, name="forward_axis"
    )
    up_name, up_world = _normalize_axis(up_axis, name="up_axis")
    alignment_info, reference_matrix_before, reference_matrix_aligned = (
        _compute_reference_alignment(
            reference, record, forward_world, up_world
        )
    )
    aligned_inverse = reference_matrix_aligned.inverted()
    frame = _frame_from_vectors(
        reference,
        forward_world,
        up_world,
        forward_name=forward_name,
        up_name=up_name,
        inverse=aligned_inverse,
    )
    raw_record_before = reference.get(REFERENCE_KEY)
    topology_before = _topology_snapshot(reference)
    rest_before = _candidate_rest(reference)
    source_before = [_object_fingerprint(obj) for obj in targets]
    world, local, target_rows = _world_and_local_cloud(
        reference, targets, inverse=aligned_inverse
    )
    if len(local) < 4 or not np.isfinite(local).all():
        raise PositioningError("目标表面点不足或含非有限坐标")
    coords = _project(local, frame)
    target_bounds = _bounds_from_coords(coords)
    if target_bounds["height"] <= 1e-8:
        raise PositioningError("目标表面高度退化")
    _require_target_neutral_pose(record["kind"], target_bounds)
    body_f_range = _longitudinal_body_range(coords)
    if body_f_range is None:
        body_f_range = [
            float(target_bounds["min"][1]),
            float(target_bounds["max"][1]),
            "fallback_full_range",
        ]
    reference_bones, reference_normalization = _reference_bone_frame(
        reference, frame
    )
    _require_reference_orientation(reference_bones, record["kind"])
    reference_bounds = _reference_bounds(reference_bones, record["kind"])
    try:
        if record["kind"] == "HUMAN":
            plan = _plan_human(
                reference=reference,
                record=record,
                enabled=enabled,
                frame=frame,
                coords=coords,
                reference_bones=reference_bones,
                reference_bounds=reference_bounds,
                target_bounds=target_bounds,
                body_f_range=body_f_range,
            )
        else:
            plan = _plan_quadruped(
                reference=reference,
                record=record,
                enabled=enabled,
                frame=frame,
                coords=coords,
                reference_bones=reference_bones,
                reference_bounds=reference_bounds,
                target_bounds=target_bounds,
                body_f_range=body_f_range,
            )
    except PositioningError:
        raise
    except Exception as error:
        raise PositioningError("候选几何计算失败；未写入参考：" + str(error)) from error
    bone_reports = _finalize_bone_reports(
        reference, plan["bone_reports"], plan["chain_reports"]
    )
    positions = plan["positions"]
    # Drop plan rows that ended up with no coordinate at all.
    positions = {
        name: row
        for name, row in positions.items()
        if row.get("head") is not None or row.get("tail") is not None
    }
    local_positions = _localize_positions(frame, positions)
    # Validate that every written bone exists and that all numeric values are
    # finite before touching the reference.
    for name, row in local_positions.items():
        bone = reference.data.bones.get(name)
        if bone is None:
            raise PositioningError("候选产生未知参考骨：" + name)
        if row.get("head") is not None and not np.isfinite(row["head"]).all():
            raise PositioningError("候选 head 含非有限值：" + name)
        if row.get("tail") is not None and not np.isfinite(row["tail"]).all():
            raise PositioningError("候选 tail 含非有限值：" + name)
        if row.get("tail") is None:
            raise PositioningError("候选缺少 tail：" + name)
        if not bone.use_connect and row.get("head") is None:
            raise PositioningError("未连接骨候选缺少 head：" + name)
    warnings = _validation_warnings(
        targets, coords, target_bounds, record["kind"], plan
    )
    if alignment_info.get("applied"):
        warnings.append(
            "新参考已自动绕世界 Z 旋转 %.3f° 以匹配请求的 ±X/±Y 前向；"
            "生产目标网格及其变换未旋转。"
            % float(alignment_info.get("angle_deg", 0.0))
        )
    reference_scale = [
        abs(float(value)) for value in reference.matrix_world.to_scale()
    ]
    if min(reference_scale) > 1e-9 and (
        max(reference_scale) / min(reference_scale) > 1.001
    ):
        warnings.append(
            "参考对象含非均匀缩放；坐标用逆矩阵映射，候选长度/位置需人工复核。"
        )
    # Capture the exact pre-edit state before the transactional write.
    with _edit_session(reference) as edit_bones:
        edit_snapshot = _capture_edit_snapshot(edit_bones)
    old_state_raw = reference.get(POSITIONING_KEY)
    apply_meta = None
    try:
        if alignment_info.get("applied"):
            if _matrix_hex(reference.matrix_world) != _matrix_hex(
                reference_matrix_before
            ):
                raise PositioningError(
                    "参考朝向在估计期间被外部改变；拒绝覆盖，未写入候选"
                )
            try:
                reference.matrix_world = reference_matrix_aligned
                bpy.context.view_layer.update()
            except Exception as error:
                try:
                    reference.matrix_world = reference_matrix_before
                    bpy.context.view_layer.update()
                except Exception:
                    pass
                raise PositioningError(
                    "无法提交参考朝向对齐；已恢复原参考变换：" + str(error)
                ) from error
        identity_before = _reference_identity(reference, record)
        apply_meta = _apply_positions(
            reference,
            local_positions,
            height=target_bounds["height"],
        )
        topology_after = _topology_snapshot(reference)
        topology_ok, topology_diff = _topology_equal(topology_before, topology_after)
        if not topology_ok:
            raise PositioningError(
                "候选编辑改变了模块拓扑：" + ", ".join(topology_diff[:8])
            )
        if reference.get(REFERENCE_KEY) != raw_record_before:
            raise PositioningError("估计期间模块参考记录被外部改变")
        identity_after = _reference_identity(reference, record)
        if _canonical(identity_after) != _canonical(identity_before):
            raise PositioningError("估计期间参考 Object/变换/模块身份被外部改变")
        pose_after = _pose_snapshot(reference)
        if _canonical(pose_after) != _canonical(pose_before):
            raise PositioningError("估计期间参考姿态被外部改变")
        source_after = [_object_fingerprint(obj) for obj in targets]
        source_differences = []
        for before, after in zip(source_before, source_after):
            differences = _fingerprint_differences(before, after)
            if differences:
                source_differences.append(
                    {"target": before["name"], "fields": differences[:16]}
                )
        if source_differences:
            raise PositioningError(
                "估计期间目标源几何/变换/UV/权重发生变化："
                + _canonical(source_differences)
            )
        rest_after = _candidate_rest(reference)
        rest_changed = _canonical(rest_before) != _canonical(rest_after)
        actual_bones = {}
        for bone in reference.data.bones:
            row = bone_reports.get(
                bone.name,
                {
                    "status": "unestimated",
                    "method": "unestimated",
                },
            )
            row = _deep(row)
            before_row = rest_before.get(bone.name, {})
            row["before"] = {
                "head_local": list(before_row.get("head", [])),
                "tail_local": list(before_row.get("tail", [])),
            }
            row["after"] = {
                "head_local": _vec(bone.head_local),
                "tail_local": _vec(bone.tail_local),
                "head_world": _vec(reference.matrix_world @ bone.head_local),
                "tail_world": _vec(reference.matrix_world @ bone.tail_local),
            }
            if bone.name in local_positions:
                row["edited"] = True
            else:
                row["edited"] = False
            actual_bones[bone.name] = row
        confirmed_before = bool(record.get("confirmed"))
        confirmation = {
            "was_confirmed": confirmed_before,
            "rest_signature_before": record.get("rest_signature"),
            "rest_changed": bool(rest_changed),
            "rest_changed_in_this_call": bool(rest_changed),
            "confirmation_expired": _confirmation_state(reference)["confirmation_expired"],
            "auto_confirmed": False,
            "required_action": (
                "检查候选后由作者显式 confirm_reference(reference)；本模块不会自动确认"
            ),
        }
        summary = _summary_from_reports(reference, actual_bones, plan["chain_reports"])
        if summary.get("low_confidence_bone_count"):
            warnings.append(
                "以下关节的实际表面中心线与声明的 cat 弯折先验冲突，"
                "已保留先验方向/标记低置信，需人工确认："
                + "、".join(summary["low_confidence_bones"])
            )
        reference_rotation = reference.matrix_world.to_3x3()
        local_x_world = reference_rotation @ Vector((1.0, 0.0, 0.0))
        if local_x_world.length <= _EPS:
            raise PositioningError("参考本地 X 轴退化，无法校验镜像语义")
        local_x_world.normalize()
        anatomical_left_world = reference_rotation @ Vector(frame["left"])
        if anatomical_left_world.length <= _EPS:
            raise PositioningError("参考目标左轴退化，无法校验镜像语义")
        anatomical_left_world.normalize()
        mirror_dot = float(local_x_world.dot(anatomical_left_world))
        mirror_axis = {
            "reference_local_x_world": _vec(local_x_world),
            "anatomical_left_world": _vec(anatomical_left_world),
            "dot": mirror_dot,
            "local_x_is_semantic_left": bool(mirror_dot >= 0.999),
            "native_use_mirror_x": bool(
                getattr(reference.data, "use_mirror_x", False)
            ),
            "note": (
                "reference semantic frame is aligned to the requested ±X/±Y front; "
                "local X remains the semantic left/right axis, so native "
                "use_mirror_x and mirror() are valid"
                if mirror_dot >= 0.999
                else "local X is not the semantic left axis; do not use local-X mirroring"
            ),
        }
        if mirror_dot < 0.999:
            warnings.append(
                "参考本地 X 轴与请求语义左轴不一致；不得继续使用 local-X 镜像，"
                "请重新对齐参考或改用语义词镜像。"
            )
        geometry = {
            "world_bounds_min": _vec(np.min(world, axis=0)),
            "world_bounds_max": _vec(np.max(world, axis=0)),
            "reference_local_bounds_min": _vec(target_bounds["min"]),
            "reference_local_bounds_max": _vec(target_bounds["max"]),
            "height": float(target_bounds["height"]),
            "surface_point_count": int(len(local)),
            "body_forward_range": [
                float(body_f_range[0]),
                float(body_f_range[1]),
                str(body_f_range[2]),
            ],
        }
        protection = {
            "target_meshes_modified": False,
            "target_fingerprints_unchanged": True,
            "reference_record_unchanged": True,
            "reference_topology_unchanged": True,
            "source_fingerprints_checked_before_and_after": True,
            "old_target_armature_or_weights_used_for_inference": False,
            "target_mesh_transform_modified": False,
            "reference_orientation_adjusted": bool(
                alignment_info.get("applied")
            ),
        }
        report = {
            "schema": POSITIONING_SCHEMA,
            "status": "proposed_needs_review",
            "kind": record["kind"],
            "axes": _public_axes(frame, forward_name, up_name),
            "reference": {
                "name": reference.name,
                "schema": record.get("schema"),
                "kind": record.get("kind"),
                "confirmed_before": confirmed_before,
                "rest_signature_before": record.get("rest_signature"),
            },
            "reference_orientation_alignment": alignment_info,
            "targets": target_rows,
            "coordinate_space": {
                "reference_object": reference.name,
                "transform": "reference.matrix_world.inverted()",
                "geometry_source": "original_mesh_datablock_not_evaluated_or_posed",
                "units": "Blender scene units",
            },
            "geometry": geometry,
            "reference_frame_normalization": reference_normalization,
            "mirror_axis": mirror_axis,
            "trunk": plan["trunk"],
            "chains": plan["chain_reports"],
            "bones": actual_bones,
            "summary": summary,
            "initialization": plan["initialization"],
            "authoring_confirmation": confirmation,
            "protection": protection,
            "warnings": warnings,
            "limitations": [
                "只支持 Z-up 与水平 ±X/±Y forward；其他方向请先对齐后再重试。",
                "生产目标网格不会被旋转；仅 fresh（未确认且无定位状态）参考会绕世界 Z "
                "自动对齐到请求前向。已有确认/定位编辑且未对齐时会拒绝，避免移动已编辑参考。",
                "只承诺直立/A-like 中性人形与站立四足；不承诺任意姿态识别。",
                "候选一律 proposed_needs_review，不自动 confirm_reference。",
                "关节高度可含明确声明的中性比例初值；报告中已区分实际截面/中心线证据与比例/bbox初值。",
                "找不到独立肢体/足端时对应骨保持未估计，不用初始化凑数。",
                "不读取旧目标骨架/旧权重作为答案；只读目标 Mesh 几何并保护 UV/权重/变换。",
            ],
        }
        report = _jsonable(report)
        state = {
            "schema": POSITIONING_SCHEMA,
            "version": 1,
            "status": "proposed_needs_review",
            "kind": record["kind"],
            "reference": identity_before,
            "axes": _public_axes(frame, forward_name, up_name),
            "source_ids": [
                {
                    "name": row["name"],
                    "object_pointer": row["object_pointer"],
                    "data_name": row["data_name"],
                    "data_pointer": row["data_pointer"],
                }
                for row in target_rows
            ],
            "source_fingerprints": source_before,
            "reference_fingerprint": {
                "identity": identity_before,
                "pose": pose_before,
                "topology": topology_before,
            },
            "reference_orientation_alignment": alignment_info,
            "mirror_axis": mirror_axis,
            "candidate_rest": rest_after,
            "candidate_rest_sha256": _candidate_rest_sha(rest_after),
            "report": report,
            "created_at": _now(),
            "updated_at": _now(),
        }
        _write_state(reference, state)
        return report
    except Exception as error:
        recovery_errors = []
        if alignment_info.get("applied"):
            try:
                reference.matrix_world = reference_matrix_before
                bpy.context.view_layer.update()
            except Exception as recovery:
                recovery_errors.append("恢复参考变换：" + str(recovery))
        try:
            _restore_positions(
                reference, edit_snapshot, float(target_bounds["height"])
            )
        except Exception as recovery:
            recovery_errors.append("恢复参考骨：" + str(recovery))
        try:
            if old_state_raw is None:
                if POSITIONING_KEY in reference:
                    del reference[POSITIONING_KEY]
            else:
                reference[POSITIONING_KEY] = old_state_raw
        except Exception as recovery:
            recovery_errors.append("恢复定位状态：" + str(recovery))
        if recovery_errors:
            raise PositioningError(
                str(error) + "；" + "；".join(recovery_errors)
            ) from error
        if isinstance(error, PositioningError):
            raise
        raise PositioningError(
            "定位写入失败；已恢复参考骨/参考变换：" + str(error)
        ) from error


def _confirmation_state(reference):
    from . import modular_rig
    return modular_rig.reference_confirmation(reference)


def require_current(reference) -> dict:
    """Validate that the estimated source geometry and reference pose are current.

    Raises PositioningError on source/vs reference drift.  Manual rest edits made
    during review are allowed; they are reported as ``candidate_rest_drift`` so
    the caller can decide whether to re-estimate or re-confirm.
    """
    record, enabled, expected = _parse_reference(reference)
    state = _read_state(reference)
    if state.get("kind") != record.get("kind"):
        raise PositioningError("定位状态 kind 与当前参考不一致")
    identity = _reference_identity(reference, record)
    stored_identity = state.get("reference_fingerprint", {}).get("identity", {})
    differences = _fingerprint_differences(stored_identity, identity)
    if differences:
        raise PositioningError(
            "参考身份/变换/模块记录已改变，候选过期："
            + ", ".join(differences[:12])
        )
    current_pose = _pose_snapshot(reference)
    stored_pose = state.get("reference_fingerprint", {}).get("pose", {})
    pose_differences = _fingerprint_differences(stored_pose, current_pose)
    if pose_differences:
        raise PositioningError(
            "参考姿态已改变，候选过期："
            + ", ".join(pose_differences[:12])
        )
    current_topology = _topology_snapshot(reference)
    stored_topology = state.get("reference_fingerprint", {}).get("topology", {})
    topology_ok, topology_diff = _topology_equal(stored_topology, current_topology)
    if not topology_ok:
        raise PositioningError(
            "参考模块拓扑已改变，候选过期：" + ", ".join(topology_diff[:12])
        )
    stored_sources = state.get("source_fingerprints", [])
    if not isinstance(stored_sources, list) or not stored_sources:
        raise PositioningError("定位状态缺少源 Mesh fingerprint")
    current_targets = []
    for stored in stored_sources:
        name = stored.get("name")
        obj = bpy.data.objects.get(name) if isinstance(name, str) else None
        if obj is None:
            raise PositioningError("源 Object 已丢失：" + repr(name))
        if not isinstance(obj, bpy.types.Object) or obj.type != "MESH":
            raise PositioningError("源 Object 已不是 MESH：" + name)
        if bpy.context.scene.objects.get(obj.name) != obj:
            raise PositioningError("源 Object 已不在当前 Scene：" + name)
        if int(obj.as_pointer()) != int(stored.get("object_pointer", -1)):
            raise PositioningError(
                "同名但源 Object ID 已改变（object pointer）：" + name
            )
        if int(obj.data.as_pointer()) != int(stored.get("data_pointer", -1)):
            raise PositioningError(
                "同名但源 Mesh ID 已改变（data pointer）：" + name
            )
        current = _object_fingerprint(obj)
        source_differences = _fingerprint_differences(stored, current)
        if source_differences:
            raise PositioningError(
                "源几何/变换/拓扑/UV/权重已改变，候选过期："
                + name
                + " -> "
                + ", ".join(source_differences[:16])
            )
        current_targets.append(
            {
                "name": obj.name,
                "object_pointer": int(obj.as_pointer()),
                "data_pointer": int(obj.data.as_pointer()),
            }
        )
    current_rest = _candidate_rest(reference)
    stored_rest = state.get("candidate_rest", {})
    drift = [
        name
        for name in sorted(set(current_rest) | set(stored_rest))
        if current_rest.get(name) != stored_rest.get(name)
    ]
    confirmation = _confirmation_state(reference)
    confirmed = confirmation["current"]
    return {
        "schema": "rigging.modular.positioning.current.v1",
        "current": True,
        "status": "proposed_needs_review",
        "kind": record.get("kind"),
        "reference": {
            "name": reference.name,
            "object_pointer": int(reference.as_pointer()),
            "data_pointer": int(reference.data.as_pointer()),
            "confirmed_now": confirmed,
            "confirmation_expired": confirmation["confirmation_expired"],
            "confirmation_record_present": confirmation["confirmation_record_present"],
            "rest_signature_now": record.get("rest_signature"),
        },
        "targets": current_targets,
        "verified": {
            "source_geometry": True,
            "source_topology": True,
            "source_uv": True,
            "source_weights": True,
            "source_transforms": True,
            "reference_transform": True,
            "reference_pose": True,
            "reference_topology": True,
        },
        "candidate_rest_changed": bool(drift),
        "candidate_rest_drift": drift,
        "authoring_confirmation": {
            "confirmed_now": confirmed,
            "candidate_rest_changed_since_estimate": bool(drift),
            "auto_confirmed": False,
        },
    }


def _mirror_pairs(reference, side: str) -> list[tuple[str, str]]:
    suffix = "." + side
    other = ".R" if side == "L" else ".L"
    result = []
    for bone in sorted(reference.data.bones, key=lambda item: item.name):
        if not bone.name.endswith(suffix):
            continue
        counterpart = bone.name[: -len(suffix)] + other
        if counterpart in reference.data.bones:
            result.append((bone.name, counterpart))
    return result


def mirror(reference, side="L") -> dict:
    """Mirror local-X bone positions from one side to the other side.

    ``side='L'`` copies every ``.L`` head/tail/roll to its ``.R`` counterpart,
    negating local X and roll.  Topology, parents, constraints and the other
    side are not changed.  A prior estimate state is required so the operation
    remains tied to validated source geometry.  For a reference that went
    through the estimate yaw alignment, local X is still the anatomical
    left/right axis (the reference object, never the production target, was
    yaw-aligned).
    """
    if not isinstance(side, str) or side.strip().upper() not in {"L", "R"}:
        raise PositioningError("mirror side 必须是 'L' 或 'R'")
    side = side.strip().upper()
    record, enabled, expected = _parse_reference(reference)
    state = _read_state(reference)
    require_current(reference)
    call_rest_before = _candidate_rest(reference)
    axes = state.get("axes")
    if not isinstance(axes, dict) or not isinstance(axes.get("local_left"), list):
        raise PositioningError(
            "定位状态缺少语义左轴，无法保证 local-X 镜像为语义中线镜像"
        )
    local_left = Vector(axes["local_left"])
    if local_left.length <= _EPS:
        raise PositioningError("定位状态语义左轴退化，拒绝 local-X 镜像")
    reference_rotation = reference.matrix_world.to_3x3()
    local_x_world = reference_rotation @ Vector((1.0, 0.0, 0.0))
    world_left = reference_rotation @ local_left
    if local_x_world.length <= _EPS or world_left.length <= _EPS:
        raise PositioningError("参考本地 X 或语义左轴退化，拒绝镜像")
    local_x_world.normalize()
    world_left.normalize()
    mirror_semantic_dot = float(local_x_world.dot(world_left))
    if mirror_semantic_dot < 0.999:
        raise PositioningError(
            "参考本地 X 不是当前请求前向的语义左轴（dot=%.6f）；"
            "不得继续使用 local-X 镜像。请使用新的 yaw 对齐参考或改用语义词镜像。"
            % mirror_semantic_dot
        )
    pairs = _mirror_pairs(reference, side)
    if not pairs:
        raise PositioningError("参考没有可镜像的 .L/.R 成对骨")
    raw_record_before = reference.get(REFERENCE_KEY)
    old_state_raw = reference.get(POSITIONING_KEY)
    topology_before = _topology_snapshot(reference)
    mirror_points = []
    for bone in reference.data.bones:
        mirror_points.append(list(bone.head_local))
        mirror_points.append(list(bone.tail_local))
    mirror_scale = float(
        np.linalg.norm(np.ptp(np.asarray(mirror_points, dtype=np.float64), axis=0))
    )
    if mirror_scale <= 1e-7:
        raise PositioningError("参考骨空间跨度退化，无法镜像")
    # Require source fingerprints to remain unchanged across the edit too.
    stored_sources = state.get("source_fingerprints", [])
    source_before = []
    for stored in stored_sources:
        obj = bpy.data.objects.get(stored.get("name", ""))
        if obj is None:
            raise PositioningError("镜像前源 Object 丢失：" + str(stored.get("name")))
        source_before.append((obj, _object_fingerprint(obj)))
    with _edit_session(reference) as edit_bones:
        snapshot = _capture_edit_snapshot(edit_bones)
        try:
            for source_name, destination_name in pairs:
                source = edit_bones[source_name]
                destination = edit_bones[destination_name]
                head = Vector((-source.head.x, source.head.y, source.head.z))
                tail = Vector((-source.tail.x, source.tail.y, source.tail.z))
                destination.tail = tail
                if not destination.use_connect:
                    destination.head = head
                destination.roll = -float(source.roll)
            for source_name, destination_name in pairs:
                bone = edit_bones[destination_name]
                if float(bone.length) <= mirror_scale * 1e-6:
                    raise PositioningError("镜像产生零长骨：" + destination_name)
                if abs(float(bone.roll) + float(edit_bones[source_name].roll)) > 1e-6:
                    raise PositioningError("镜像 roll 未关于本地 X 对称：" + destination_name)
            continuity = _verify_chain_continuity(
                edit_bones,
                mirror_scale,
            )
            if continuity["errors"]:
                raise PositioningError(
                    "镜像破坏连续链：" + _canonical(continuity["errors"][:4])
                )
        except Exception:
            _restore_edit_snapshot(edit_bones, snapshot)
            raise
    try:
        topology_after = _topology_snapshot(reference)
        topology_ok, topology_diff = _topology_equal(topology_before, topology_after)
        if not topology_ok:
            raise PositioningError(
                "镜像改变了模块拓扑：" + ", ".join(topology_diff[:8])
            )
        if reference.get(REFERENCE_KEY) != raw_record_before:
            raise PositioningError("镜像期间模块参考记录被外部改变")
        for obj, fingerprint in source_before:
            current = _object_fingerprint(obj)
            differences = _fingerprint_differences(fingerprint, current)
            if differences:
                raise PositioningError(
                    "镜像期间源 Mesh 发生变化：" + obj.name + " -> "
                    + ", ".join(differences[:12])
                )
        current_rest = _candidate_rest(reference)
        before_rest = call_rest_before
        bone_reports = state.get("report", {}).get("bones", {})
        mirror_errors = []
        for source_name, destination_name in pairs:
            source = reference.data.bones[source_name]
            destination = reference.data.bones[destination_name]
            expected_tail = Vector(
                (-source.tail_local.x, source.tail_local.y, source.tail_local.z)
            )
            tail_error = float((destination.tail_local - expected_tail).length)
            head_error = 0.0
            if not destination.use_connect:
                expected_head = Vector(
                    (-source.head_local.x, source.head_local.y, source.head_local.z)
                )
                head_error = float((destination.head_local - expected_head).length)
            if max(tail_error, head_error) > mirror_scale * 1e-5:
                mirror_errors.append(
                    {
                        "source": source_name,
                        "destination": destination_name,
                        "head_error": head_error,
                        "tail_error": tail_error,
                    }
                )
        if mirror_errors:
            raise PositioningError(
                "镜像结果未通过对称/连续校验："
                + _canonical(mirror_errors[:6])
            )
        for source_name, destination_name in pairs:
            source = reference.data.bones[source_name]
            destination = reference.data.bones[destination_name]
            row = _deep(
                bone_reports.get(
                    destination_name,
                    {
                        "status": "unestimated",
                        "method": "unestimated",
                    },
                )
            )
            row.update(
                {
                    "status": "proposed_needs_review",
                    "method": "manual_mirror_local_x",
                    "geometry_basis": "manual_mirror",
                    "chain": row.get("chain"),
                    "side": destination_name.rsplit(".", 1)[-1],
                    "mirrored_from": source_name,
                    "before": {
                        "head_local": before_rest.get(destination_name, {}).get("head", []),
                        "tail_local": before_rest.get(destination_name, {}).get("tail", []),
                    },
                    "after": {
                        "head_local": _vec(destination.head_local),
                        "tail_local": _vec(destination.tail_local),
                        "head_world": _vec(reference.matrix_world @ destination.head_local),
                        "tail_world": _vec(reference.matrix_world @ destination.tail_local),
                    },
                    "edited": True,
                }
            )
            bone_reports[destination_name] = row
        chain_reports = state.get("report", {}).get("chains", {})
        summary = _summary_from_reports(reference, bone_reports, chain_reports)
        rest_changed = _canonical(before_rest) != _canonical(current_rest)
        confirmed = bool(record.get("confirmed"))
        confirmation = {
            "was_confirmed": confirmed,
            "rest_signature_before": record.get("rest_signature"),
            "rest_changed": bool(rest_changed),
            "rest_changed_in_this_call": bool(rest_changed),
            "confirmation_expired": _confirmation_state(reference)["confirmation_expired"],
            "auto_confirmed": False,
            "required_action": (
                "检查镜像候选后由作者显式 confirm_reference(reference)；本模块不会自动确认"
            ),
        }
        history = list(state.get("mirror_history", []))
        history.append(
            {
                "at": _now(),
                "side": side,
                "pairs": [[source_name, destination_name] for source_name, destination_name in pairs],
            }
        )
        report = _deep(state.get("report", {}))
        report["status"] = "proposed_needs_review"
        report["bones"] = bone_reports
        report["summary"] = summary
        report["authoring_confirmation"] = confirmation
        report["mirror"] = {
            "last_side": side,
            "pairs": [[source_name, destination_name] for source_name, destination_name in pairs],
            "method": "local_x_negated_head_tail_roll",
            "local_x_world": _vec(local_x_world),
            "anatomical_left_world": _vec(world_left),
            "semantic_dot": mirror_semantic_dot,
            "native_use_mirror_x": bool(
                getattr(reference.data, "use_mirror_x", False)
            ),
        }
        state["candidate_rest"] = current_rest
        state["candidate_rest_sha256"] = _candidate_rest_sha(current_rest)
        state["report"] = report
        state["mirror_history"] = history
        _write_state(reference, state)
        return {
            "schema": POSITIONING_SCHEMA,
            "status": "proposed_needs_review",
            "kind": record.get("kind"),
            "side": side,
            "mirrored_count": len(pairs),
            "mirrored": [
                {
                    "from": source_name,
                    "to": destination_name,
                    "method": "local_x_negated_head_tail_roll",
                }
                for source_name, destination_name in pairs
            ],
            "source_unchanged": True,
            "reference_record_unchanged": True,
            "topology_unchanged": True,
            "mirror_axis": {
                "local_x_world": _vec(local_x_world),
                "anatomical_left_world": _vec(world_left),
                "dot": mirror_semantic_dot,
                "native_use_mirror_x": bool(
                    getattr(reference.data, "use_mirror_x", False)
                ),
            },
            "authoring_confirmation": confirmation,
            "summary": summary,
        }
    except Exception as error:
        try:
            _restore_positions(reference, snapshot, 1.0)
        except Exception as recovery:
            raise PositioningError(
                str(error) + "；恢复参考骨失败：" + str(recovery)
            ) from error
        try:
            if old_state_raw is None:
                if POSITIONING_KEY in reference:
                    del reference[POSITIONING_KEY]
            else:
                reference[POSITIONING_KEY] = old_state_raw
        except Exception:
            pass
        if isinstance(error, PositioningError):
            raise
        raise PositioningError("镜像失败；已恢复参考骨：" + str(error)) from error
