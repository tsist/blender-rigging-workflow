# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved workflow candidates and scope-bound authorization for live writes."""

import json
import uuid

import bpy

from . import skeleton_gate, skeleton_qc


_RECORD_PREFIX = "rig_agent_candidate_v1_"
_ANCHOR_PREFIX = "rig_agent_candidate_id_v1_"
_SCHEMA = "rigging.agent-candidate.v1"


class CandidateError(ValueError):
    pass


def _keys(candidate_id):
    if (not isinstance(candidate_id, str) or len(candidate_id) != 32
            or any(c not in "0123456789abcdef" for c in candidate_id)):
        raise CandidateError("无效的候选 ID")
    return _RECORD_PREFIX + candidate_id, _ANCHOR_PREFIX + candidate_id


def _current_scene(scene):
    if not isinstance(scene, bpy.types.Scene) or scene != bpy.context.scene:
        raise CandidateError("候选必须在其实际场景操作")


def _local_rig(scene, rig):
    if (not isinstance(rig, bpy.types.Object) or rig.type != 'ARMATURE'
            or scene.objects.get(rig.name) != rig or rig.library or rig.data.library
            or rig.override_library or not rig.is_editable or not rig.data.is_editable
            or rig.mode == 'EDIT'):
        raise CandidateError("需要当前场景中可编辑的本地骨架，且已退出编辑模式")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _structure(rig):
    # Selection, color and widget placement are view state. The exact reviewed
    # appearance, pose probes and inspected animations belong to the gate.
    qc = skeleton_qc
    ignored = {"location", "rotation_euler", "rotation_quaternion",
               "rotation_axis_angle", "scale"}
    bones = []
    for bone in rig.data.bones:
        pose = rig.pose.bones[bone.name]
        settings = {key: value for key, value in qc._rna(pose).items()
                    if key not in ignored and not key.startswith("custom_shape")}
        bones.append({
            "name": bone.name,
            "parent": bone.parent.name if bone.parent else None,
            "matrix": qc._plain(bone.matrix_local),
            "head": list(bone.head_local),
            "tail": list(bone.tail_local),
            "settings": qc._rna(bone),
            "handles": [qc._plain(getattr(bone, "bbone_custom_handle_" + end).name)
                        if getattr(bone, "bbone_custom_handle_" + end, None) else None
                        for end in ("start", "end")],
            "pose_settings": settings,
            "constraints": qc._constraints(pose),
        })
    return qc.digest({
        "bones": bones,
        "object_constraints": qc._constraints(rig),
        "object_drivers": qc._drivers(rig),
        "data_drivers": qc._drivers(rig.data),
        "parent": qc._plain(rig.parent),
        "parent_type": rig.parent_type,
        "parent_bone": rig.parent_bone,
        "world": qc._plain(rig.matrix_world),
        "pose_position": rig.data.pose_position,
    })


def _runtime():
    return {
        "blender": list(bpy.app.version),
        "build": bpy.app.build_hash.decode("ascii"),
    }


def record(scene, rig, *, kind, payload):
    """Register a finished candidate without granting permission or saving a file."""
    _current_scene(scene)
    _local_rig(scene, rig)
    if not isinstance(kind, str) or not kind.strip() or not isinstance(payload, dict):
        raise CandidateError("候选必须有明确用途与 JSON 数据")
    candidate_id = uuid.uuid4().hex
    record_key, anchor_key = _keys(candidate_id)
    result = {
        "schema": _SCHEMA,
        "id": candidate_id,
        "kind": kind,
        "rig": rig.name,
        "armature": rig.data.name,
        "runtime": _runtime(),
        "frame": [scene.frame_current, scene.frame_subframe],
        "view_layer": bpy.context.view_layer.name,
        "units": scene.unit_settings.scale_length,
        "structure_sha256": _structure(rig),
        "payload": payload,
    }
    persistable = _json(result)
    scene[anchor_key] = rig
    try:
        scene[record_key] = persistable
    except Exception:
        del scene[anchor_key]
        raise
    return json.loads(persistable)


def resolve(scene, candidate_id, *, kind):
    """Find and recheck the exact saved rig in the current process."""
    _current_scene(scene)
    record_key, anchor_key = _keys(candidate_id)
    raw = scene.get(record_key)
    if not isinstance(raw, str):
        raise CandidateError("当前工程没有该候选，打开保存的准备工程后重试")
    try:
        saved = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CandidateError("候选记录不可读") from exc
    if (not isinstance(saved, dict) or saved.get("schema") != _SCHEMA
            or saved.get("id") != candidate_id or saved.get("kind") != kind
            or not isinstance(saved.get("payload"), dict)):
        raise CandidateError("候选身份或用途不匹配")
    if (saved.get("runtime") != _runtime()
            or saved.get("view_layer") != bpy.context.view_layer.name
            or saved.get("units") != scene.unit_settings.scale_length):
        raise CandidateError("候选宿主、视图层或单位已改变")
    if saved.get("frame") != [scene.frame_current, scene.frame_subframe]:
        raise CandidateError("请返回保存候选时的制作帧再继续")
    rig = scene.get(anchor_key)
    _local_rig(scene, rig)
    if (rig.name != saved.get("rig") or rig.data.name != saved.get("armature")
            or _structure(rig) != saved.get("structure_sha256")):
        raise CandidateError("保存候选的骨架、控制、约束或驱动已改变，须重新准备并审核")
    return rig, saved


def discard(scene, candidate_id):
    """Remove workflow bookkeeping without deleting any rig or mesh."""
    _current_scene(scene)
    for key in _keys(candidate_id):
        if key in scene:
            del scene[key]


def scope_digest(scope):
    if not isinstance(scope, dict) or not scope:
        raise CandidateError("操作缺少明确的实际作用范围")
    return skeleton_qc.digest(scope)


def require_operation(rig, *, operation, scope):
    """Require an independently reviewed exact operation scope in the live gate."""
    return skeleton_gate.require_pass(
        rig, operation=operation, write_scope_sha256=scope_digest(scope))
