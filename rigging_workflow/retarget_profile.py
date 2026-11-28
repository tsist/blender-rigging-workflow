# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable JSON retarget profiles for generic sources -> current C94 targets.

This is a staging layer over the frozen shared core.  It does not implement a
second source/preview state, Action publisher or solver.  The owner of those
operations remains ``retarget_action.convert_resolved``.

The profile is a pure JSON document.  It contains an explicit semantic ->
actual source bone mapping, source/clip/axis/unit evidence and persistent
calibration fingerprints.  Python is never read from a profile and no preset is
executed.  Unknown bone names are accepted when a hand-complete mapping passes
the structural checks; semantics are never guessed from names or positions.

The adapter factory is called by ``convert_resolved`` only after the shared
core has snapshotted the target, isolated source NLA/blend/influence and set
the explicit clip start.  Target neutral clearing is limited to the C94
Adapter write domain plus the four FK modes; non-target inputs such as face
controls are not cleared by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from . import pose_switching as pose
from . import retarget_action as action_api
from . import retarget_c94 as c94
from . import retarget_calibration as cal
from . import retarget_source as source_api


PROFILE_SCHEMA = "c94.retarget.profile.v1"
PROFILE_VERSION = 1
CALIBRATION_SCHEMA = "c94.retarget.profile.calibration.v1"
CALIBRATION_KEY = "c94_retarget_profile_calibration_v1"
PROFILE_REPORT_KEY = "c94_retarget_profile_v1"

ORIENTATION_REFERENCE = "REST"
TRANSLATION_ORIGIN_CLIP_START = "CLIP_START"
TRANSLATION_ORIGIN_REST_HIP = "REST_HIP"
MOTION_SCALE_LEG_LENGTH = "LEG_LENGTH"
MOTION_SCALE_REST_HIP_HEIGHT = "REST_HIP_HEIGHT"
MOTION_SCALE_NONE = "NONE"

TIER_VALIDATED_PRESET = "validated_preset"
TIER_CONFIGURED = "configured"
TIER_NEEDS_ADAPTATION = "needs_adaptation"
TIERS = (TIER_VALIDATED_PRESET, TIER_CONFIGURED, TIER_NEEDS_ADAPTATION)

VALIDATED_PRESETS = {
    "mixamo_names_candidate",
    "cmu_exact_31_joint_sample_v1",
}

REQUIRED_BODY = {
    "hips", "chest", "head",
    *{"%s.%s" % (item, side)
      for side in ("L", "R")
      for item in ("upper_arm", "forearm", "hand", "thigh", "shin", "foot")},
}
OPTIONAL_BODY = {
    "spine", "spine1", "neck", "shoulder.L", "shoulder.R",
    "toes.L", "toes.R",
}
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
SIDES = ("L", "R")
FINGER_SEGMENTS = tuple(
    "%s%d.%s" % (finger, index, side)
    for side in SIDES for finger in FINGERS for index in range(1, 5)
)
CONFIGURED_CHANNELS = {"spine1", "shoulder.L", "shoulder.R"}

_FORBIDDEN_KEYS = {
    "python", "exec", "eval", "compile", "lambda", "subprocess",
    "__import__", "code", "script",
}


def require(value, message):
    if not value:
        raise ValueError(message)


def _hash(value):
    return cal.canonical_hash(value)


def _finite_float(value, message):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    require(math.isfinite(result), message)
    return result


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_only(value, path="profile"):
    """Reject non-JSON data and executable-looking keys; no code is evaluated."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        require(math.isfinite(value), "%s 包含非有限数值" % path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_only(item, "%s[%d]" % (path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            require(isinstance(key, str), "%s 的键必须为字符串" % path)
            require(key not in _FORBIDDEN_KEYS,
                    "%s 含不可执行配置禁止键：%s" % (path, key))
            _json_only(item, "%s.%s" % (path, key))
        return
    raise ValueError("%s 含非JSON值：%s" % (path, type(value).__name__))


def _uniform_positive(obj, matrix=None):
    matrix = obj.matrix_world.copy() if matrix is None else matrix.copy()
    columns = [matrix.to_3x3().col[i] for i in range(3)]
    lengths = [column.length for column in columns]
    reasons = []
    if not all(math.isfinite(value) for row in matrix for value in row):
        reasons.append("world_matrix_non_finite")
    if min(lengths) <= 0:
        reasons.append("world_column_non_positive")
    if max(lengths) - min(lengths) > max(lengths) * 1e-4:
        reasons.append("world_non_uniform")
    if matrix.determinant() <= 0:
        reasons.append("world_determinant_non_positive")
    if any(abs(columns[i].dot(columns[j])) > lengths[i] * lengths[j] * 1e-5
           for i in range(3) for j in range(i)):
        reasons.append("world_shear")
    return {
        "matrix_world": cal.matrix_list(matrix),
        "determinant": float(matrix.determinant()),
        "column_lengths": [float(value) for value in lengths],
        "object_unit": float(lengths[0]) if lengths and math.isfinite(lengths[0]) else None,
        "uniform_positive": not reasons,
        "reasons": reasons,
    }


def _upright_z(obj):
    column = obj.matrix_world.to_3x3().col[2]
    if column.length <= 0:
        return False
    return (column.normalized() - Vector((0.0, 0.0, 1.0))).length <= 1e-5


def _target_world_matrix(target, frame=1):
    """Canonical target object world without touching the scene clock.

    Some C94 production targets carry an object-level location Action.  The
    calibration identity must not depend on whichever frame the file happened
    to be saved at, but it must also not call ``frame_set`` before the shared
    conversion snapshot.  Evaluate the object transform fcurves at frame 1
    directly, without changing scene.frame_current or the depsgraph.
    """
    ad = target.animation_data
    if ad is None or ad.action is None or ad.action_slot is None:
        return target.matrix_world.copy()
    object_paths = {
        "location", "rotation_quaternion", "rotation_euler", "scale",
        "delta_location", "delta_rotation_quaternion",
        "delta_rotation_euler", "delta_scale",
    }
    curves = {}
    for layer in ad.action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(ad.action_slot)
            if bag is None:
                continue
            for curve in bag.fcurves:
                if curve.data_path in object_paths:
                    curves.setdefault(curve.data_path, {})[
                        curve.array_index] = float(curve.evaluate(frame))
    if not curves:
        return target.matrix_world.copy()

    def values(path, default):
        result = list(default)
        for index, value in curves.get(path, {}).items():
            if 0 <= index < len(result):
                result[index] = value
        return result

    location = Vector(values("location", target.location))
    delta_location = Vector(values("delta_location", target.delta_location))
    scale = Vector(values("scale", target.scale))
    delta_scale = Vector(values("delta_scale", target.delta_scale))
    if target.rotation_mode == "QUATERNION":
        rotation = target.rotation_quaternion.copy()
        for index, value in curves.get("rotation_quaternion", {}).items():
            if 0 <= index < 4:
                rotation[index] = value
        delta_rotation = target.delta_rotation_quaternion.copy()
        for index, value in curves.get("delta_rotation_quaternion", {}).items():
            if 0 <= index < 4:
                delta_rotation[index] = value
        rotation_matrix = rotation.to_matrix().to_4x4()
        delta_rotation_matrix = delta_rotation.to_matrix().to_4x4()
    elif target.rotation_mode == "XYZ":
        rotation = target.rotation_euler.copy()
        for index, value in curves.get("rotation_euler", {}).items():
            if 0 <= index < 3:
                rotation[index] = value
        delta_rotation = target.delta_rotation_euler.copy()
        for index, value in curves.get("delta_rotation_euler", {}).items():
            if 0 <= index < 3:
                delta_rotation[index] = value
        rotation_matrix = rotation.to_matrix().to_4x4()
        delta_rotation_matrix = delta_rotation.to_matrix().to_4x4()
    else:
        return target.matrix_world.copy()
    scale_matrix = Matrix.Diagonal((
        scale.x * delta_scale.x, scale.y * delta_scale.y,
        scale.z * delta_scale.z, 1.0))
    basis = (Matrix.Translation(location + delta_location)
             @ rotation_matrix @ delta_rotation_matrix @ scale_matrix)
    if target.parent is not None:
        return target.parent.matrix_world @ target.matrix_parent_inverse @ basis
    return basis


def _content_fingerprint(rig, world_matrix=None):
    require(isinstance(rig, bpy.types.Object) and rig.type == "ARMATURE",
            "内容指纹需要骨架对象")
    world = _uniform_positive(rig, matrix=world_matrix)
    return {
        "rest_signature": cal.bone_rest_signature(rig),
        "bone_count": len(rig.data.bones),
        "matrix_world": world["matrix_world"],
        "determinant": world["determinant"],
        "uniform_positive": world["uniform_positive"],
        "object_unit": world["object_unit"],
        "pose_position": rig.data.pose_position,
        "linked": bool(rig.library or rig.data.library),
    }


def _world_fingerprint(rig, world_matrix=None):
    world = _uniform_positive(rig, matrix=world_matrix)
    return _hash({
        "matrix_world": world["matrix_world"],
        "determinant": world["determinant"],
        "uniform_positive": world["uniform_positive"],
        "object_unit": world["object_unit"],
    })


def _scene_unit():
    scene = bpy.context.scene
    return {
        "system": scene.unit_settings.system,
        "scale_length": float(scene.unit_settings.scale_length),
        "length_unit": scene.unit_settings.length_unit,
    }


def _scene_snapshot():
    scene = bpy.context.scene
    return {
        "scene": scene,
        "fps": scene.render.fps,
        "fps_base": scene.render.fps_base,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_current": scene.frame_current,
        "frame_subframe": scene.frame_subframe,
        "camera": scene.camera,
        "selected": [obj.name for obj in bpy.context.selected_objects],
        "active": (bpy.context.view_layer.objects.active.name
                   if bpy.context.view_layer.objects.active else None),
    }


def _restore_scene_snapshot(state):
    scene = state["scene"]
    scene.render.fps = state["fps"]
    scene.render.fps_base = state["fps_base"]
    scene.frame_start = state["frame_start"]
    scene.frame_end = state["frame_end"]
    scene.camera = state["camera"]
    scene.frame_set(state["frame_current"], subframe=state["frame_subframe"])
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for name in state["selected"]:
        obj = scene.objects.get(name)
        if obj is not None:
            obj.select_set(True)
    active = scene.objects.get(state["active"]) if state["active"] else None
    bpy.context.view_layer.objects.active = active


@contextmanager
def preserve_scene_state():
    """Sampling a profile clip must not leave the production scene frame changed."""
    state = _scene_snapshot()
    try:
        yield
    finally:
        _restore_scene_snapshot(state)


def _mapping_payload(mapping):
    require(isinstance(mapping, dict), "映射必须是JSON对象")
    bones = mapping.get("bones")
    require(isinstance(bones, dict) and bones, "映射缺少明确的bones对象")
    fingers = mapping.get("fingers") or {"complete": [], "skipped": {}}
    return {
        "schema": mapping.get("schema", "c94.retarget.mapping.v1"),
        "preset": mapping.get("preset"),
        "bones": dict(sorted(bones.items())),
        "fingers": {
            "complete": sorted(fingers.get("complete") or []),
            "skipped": {key: sorted(value) if isinstance(value, list) else value
                        for key, value in sorted((fingers.get("skipped") or {}).items())},
        },
        "missing_optional": sorted(mapping.get("missing_optional") or []),
        "unmapped_channels": sorted(mapping.get("unmapped_channels") or []),
        "support": mapping.get("support"),
    }


def _coerce_mapping(mapping, preset):
    """Accept either a full mapping object or a plain semantic->bone JSON object."""
    require(isinstance(mapping, dict) and mapping, "映射必须是非空JSON对象")
    if "bones" in mapping:
        result = dict(mapping)
        result["schema"] = result.get("schema", "c94.retarget.mapping.v1")
        result["preset"] = result.get("preset", preset)
        result["fingers"] = result.get("fingers") or {"complete": [], "skipped": {}}
        result["missing_optional"] = list(result.get("missing_optional") or [])
        result["unmapped_channels"] = list(result.get("unmapped_channels") or [])
        return result
    require(all(isinstance(key, str) and isinstance(value, str) and value
                for key, value in mapping.items()),
            "mapping_json必须是semantic -> 骨名的JSON对象")
    return {
        "schema": "c94.retarget.mapping.v1",
        "preset": preset,
        "bones": dict(mapping),
        "fingers": {"complete": [], "skipped": {}},
        "missing_optional": [],
        "unmapped_channels": [],
    }


def _mapping_sha256(mapping):
    return _hash(_mapping_payload(mapping))


def _canonical_preset_bones(source, preset):
    """Return actual canonical preset bones, never a caller-declared string."""
    if source is None or preset not in VALIDATED_PRESETS:
        return None
    try:
        if preset == "cmu_exact_31_joint_sample_v1":
            return dict(source_api.exact_cmu_mapping(source)["bones"])
        if preset == "mixamo_names_candidate":
            return dict(source_api.mapping(source)["bones"])
    except (ValueError, KeyError):
        return None
    return None


def _preset_bones_sha256(bones):
    if not isinstance(bones, dict) or not bones:
        return None
    return _hash(dict(sorted(bones.items())))


def _axis_identity_payload(axis):
    """Only transform/unit semantics enter calibration identity.

    File/path/SHA/importer provenance, source Action/Slot and MMD metadata
    hashes are deliberately excluded; those are evidence, not calibration
    semantics.
    """
    fields = (
        "mode", "forward", "up", "scale", "scale_semantics",
        "target_forward", "target_up", "axis_mapping_function",
        "object_rotation_quaternion", "object_rotation_matrix",
        "object_rotation_determinant", "object_world",
    )
    return {key: axis[key] for key in fields
            if isinstance(axis, dict) and key in axis}


def mapped_mapping(profile):
    """Return a core-compatible mapping object without adding state."""
    mapping = profile["mapping"]
    return {
        "schema": "c94.retarget.mapping.v1",
        "preset": mapping.get("preset") or profile.get("preset") or "generic_profile",
        "bones": dict(mapping["bones"]),
        "fingers": mapping.get("fingers") or {"complete": [], "skipped": {}},
        "missing_optional": list(mapping.get("missing_optional") or []),
        "unmapped_channels": list(mapping.get("unmapped_channels") or []),
        "support": mapping.get("support") or "profile_configured",
        "profile_id": profile.get("id"),
    }


def _finger_chains(mapping):
    result = {"complete": [], "skipped": {}}
    bones = mapping.get("bones") or {}
    for side in SIDES:
        for finger in FINGERS:
            chain = [finger + str(index) + "." + side for index in range(1, 5)]
            missing = [semantic for semantic in chain if semantic not in bones]
            if missing:
                result["skipped"][finger + "." + side] = missing
            else:
                result["complete"].append(finger + "." + side)
    return result


def _is_descendant(bone, ancestor):
    return ancestor.name in {item.name for item in bone.parent_recursive}


def _share_common_ancestor(first, second):
    ancestors = {first.name, *(item.name for item in first.parent_recursive)}
    return any(name in ancestors
               for name in (second.name, *(item.name for item in second.parent_recursive)))


def _rest_point(source, bone_name):
    return source.matrix_world @ source.data.bones[bone_name].matrix_local


def validate_mapping(source, mapping, *, include_fingers=None):
    """Explicit mapping checks; unknown names are fine, guessed semantics are not."""
    errors = []
    warnings = []
    checks = {}
    bones = mapping.get("bones") if isinstance(mapping, dict) else None
    if not isinstance(bones, dict) or not bones:
        return {
            "ok": False, "tier": TIER_NEEDS_ADAPTATION,
            "errors": ["映射缺少明确的semantic -> bone引用"],
            "warnings": [], "checks": {}, "finger_chains": {"complete": [], "skipped": {}},
        }
    values = list(bones.values())
    checks["mapping_explicit"] = all(isinstance(key, str) and isinstance(value, str) and value
                                     for key, value in bones.items())
    if not checks["mapping_explicit"]:
        errors.append("映射键值必须是非空字符串")
    checks["unique_bones"] = len(values) == len(set(values))
    if not checks["unique_bones"]:
        errors.append("多个语义复用了同一源骨")
    missing_required = sorted(REQUIRED_BODY - set(bones))
    checks["required_semantics"] = not missing_required
    if missing_required:
        errors.append("缺少必需身体语义：" + ", ".join(missing_required))
    unknown = sorted(set(bones) - REQUIRED_BODY - OPTIONAL_BODY - set(FINGER_SEGMENTS))
    if unknown:
        warnings.append("映射含未被当前C94适配器消费的语义：" + ", ".join(unknown))

    chains = _finger_chains(mapping)
    requested_fingers = bool(include_fingers) if include_fingers is not None else True
    if requested_fingers:
        checks["finger_chains_complete"] = bool(chains["complete"]) and not chains["skipped"]
        if not checks["finger_chains_complete"]:
            errors.append("要求手指转换，但四节点手指链不完整")
    else:
        checks["finger_chains_complete"] = None

    checks["bone_names_exist"] = None
    checks["parent_chains"] = None
    checks["left_right_distinct"] = None
    checks["child_joints_positive"] = None
    checks["side_geometry"] = None
    parent_checks = []
    child_distances = {}
    side_checks = {}
    if source is not None:
        source_api.armature(source)
        existing = {bone.name for bone in source.data.bones}
        missing = sorted(name for name in values if name not in existing)
        checks["bone_names_exist"] = not missing
        if missing:
            errors.append("映射骨不存在：" + ", ".join(missing))
        if checks["bone_names_exist"]:
            pairs = []
            for side in SIDES:
                pairs.extend([
                    ("hips", "thigh." + side, "descendant"),
                    ("thigh." + side, "shin." + side, "descendant"),
                    ("shin." + side, "foot." + side, "descendant"),
                    ("foot." + side, "toes." + side, "descendant"),
                    ("upper_arm." + side, "forearm." + side, "descendant"),
                    ("forearm." + side, "hand." + side, "descendant"),
                ])
            if "spine" in bones:
                # MMD's lower/upper body are siblings under a common root; CMU
                # and Mixamo are direct descendants.  Require connectivity, not
                # a single hard-coded parent edge.
                pairs.append(("hips", "spine", "connected"))
                if "spine1" in bones:
                    pairs.append(("spine", "spine1", "descendant"))
                    pairs.append(("spine1", "chest", "descendant"))
                pairs.append(("spine", "chest", "descendant"))
            elif "chest" in bones:
                pairs.append(("hips", "chest", "connected"))
            if "neck" in bones:
                pairs.append(("chest", "neck", "descendant"))
                pairs.append(("neck", "head", "descendant"))
            elif "chest" in bones and "head" in bones:
                pairs.append(("chest", "head", "descendant"))
            for side in SIDES:
                if "shoulder." + side in bones:
                    pairs.append(("chest", "shoulder." + side, "descendant"))
                    pairs.append(("shoulder." + side,
                                  "upper_arm." + side, "descendant"))
                pairs.append(("chest", "upper_arm." + side, "descendant"))
            for side in SIDES:
                for finger in FINGERS:
                    chain = ["hand." + side] + [
                        finger + str(index) + "." + side for index in range(1, 5)]
                    pairs.extend((parent, child, "descendant")
                                 for parent, child in zip(chain, chain[1:]))
            for parent, child, mode in pairs:
                if parent not in bones or child not in bones:
                    continue
                parent_bone = source.data.bones[bones[parent]]
                child_bone = source.data.bones[bones[child]]
                if mode == "connected":
                    # Same-chain or true siblings under a shared torso root
                    # (MMD 下半身/上半身) are acceptable; the reverse chain is
                    # not (spine must not be an ancestor of hips).
                    ok = (_is_descendant(child_bone, parent_bone)
                          or (_share_common_ancestor(parent_bone, child_bone)
                              and not _is_descendant(parent_bone, child_bone)))
                else:
                    ok = _is_descendant(child_bone, parent_bone)
                parent_checks.append({"parent": parent, "child": child, "ok": ok,
                                      "mode": mode,
                                      "parent_bone": parent_bone.name,
                                      "child_bone": child_bone.name})
                if not ok:
                    if mode == "connected":
                        errors.append("源躯干/层级与显式语义不连通：%s -> %s"
                                      % (parent, child))
                    else:
                        errors.append("源层级与显式语义不符：%s -> %s"
                                      % (parent, child))
                distance = (_rest_point(source, child_bone.name).translation
                            - _rest_point(source, parent_bone.name).translation).length
                child_distances[parent + "->" + child] = float(distance)
                base = parent.split(".")[0]
                critical = (base in {"thigh", "shin", "foot", "toes",
                                     "upper_arm", "forearm", "hand"}
                            or any(parent.startswith(finger + str(index) + ".")
                                   for finger in FINGERS for index in range(1, 5)))
                if not math.isfinite(distance) or distance <= 1e-7:
                    if critical:
                        errors.append("真实child关节重合或退化：%s -> %s" % (parent, child))
                    else:
                        warnings.append("非肢体child关节重合（按原层级继续）：%s -> %s"
                                        % (parent, child))
            checks["parent_chains"] = all(row["ok"] for row in parent_checks)
            critical_distances = [
                value for key, value in child_distances.items()
                if (key.split(".")[0].split("->")[0] in {
                    "thigh", "shin", "foot", "toes", "upper_arm", "forearm", "hand"}
                    or any(key.startswith(finger + str(index) + ".")
                           for finger in FINGERS for index in range(1, 5)))
            ]
            checks["child_joints_positive"] = all(
                math.isfinite(value) and value > 1e-7 for value in critical_distances)

            # Left/right assignment is explicit.  Disjointness is structural;
            # geometric mirror evidence is reported but never used to assign.
            side_errors = []
            side_evidence = {}
            hips = source.data.bones[bones["hips"]]
            hips_world = _rest_point(source, hips.name).translation
            for semantic in ("thigh", "hand", "upper_arm", "foot"):
                left, right = semantic + ".L", semantic + ".R"
                if left not in bones or right not in bones:
                    continue
                left_bone = source.data.bones[bones[left]]
                right_bone = source.data.bones[bones[right]]
                cross = (_is_descendant(left_bone, right_bone)
                         or _is_descendant(right_bone, left_bone))
                if cross:
                    side_errors.append("%s/%s 位于同一父子链" % (left, right))
                left_point = _rest_point(source, left_bone.name).translation
                right_point = _rest_point(source, right_bone.name).translation
                delta = left_point - right_point
                axis = max(range(3), key=lambda index: abs(delta[index]))
                same_side = ((left_point[axis] - hips_world[axis])
                             * (right_point[axis] - hips_world[axis]) >= 0)
                side_evidence[semantic] = {
                    "left": left_bone.name, "right": right_bone.name,
                    "separating_axis": axis,
                    "opposite_sides": not same_side,
                    "distance": float((left_point - right_point).length),
                }
                if same_side:
                    warnings.append("左右几何证据不一致（%s），按显式映射继续" % semantic)
            checks["left_right_distinct"] = not side_errors
            checks["side_geometry"] = all(row["opposite_sides"]
                                          for row in side_evidence.values())
            if side_errors:
                errors.extend(side_errors)
            side_checks = side_evidence

    tier = TIER_NEEDS_ADAPTATION if errors else TIER_CONFIGURED
    return {
        "ok": not errors,
        "tier": tier,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "finger_chains": chains,
        "parent_checks": parent_checks,
        "child_distances": child_distances,
        "side_checks": side_checks,
        "required_missing": missing_required,
        "unknown_semantics": unknown,
    }


def _fbx_axis(record, source):
    return {
        "mode": "FBX_IMPORT_NATIVE",
        "source": "wm.fbx_import; armature is already in the current Blender world",
        "importer": (record or {}).get("importer", "wm.fbx_import"),
        "file": (record or {}).get("file"),
        "sha256": (record or {}).get("sha256"),
        "scale": 1.0,
        "scale_semantics": "Blender units per imported FBX unit after native import conversion",
        "forward": "BLENDER_NATIVE",
        "up": "Z",
        "target_forward": "-Y",
        "target_up": "Z",
        "object_world": _uniform_positive(source),
        "evidence": "native FBX importer metadata; source axes are not re-guessed",
    }


def _bvh_axis(record):
    require(isinstance(record, dict), "BVH profile需要导入metadata")
    return {
        "mode": "BVH_EXPLICIT",
        "source": "accepted_bvh_source.import_bvh metadata",
        "importer": record.get("importer"),
        "file": record.get("file"),
        "sha256": record.get("sha256"),
        "forward": record.get("forward"),
        "up": record.get("up"),
        "scale": record.get("scale"),
        "scale_semantics": record.get("scale_semantics"),
        "target_forward": record.get("target_forward"),
        "target_up": record.get("target_up"),
        "axis_mapping_function": record.get("axis_mapping_function"),
        "object_rotation_quaternion": record.get("object_rotation_quaternion"),
        "object_rotation_matrix": record.get("object_rotation_matrix"),
        "object_rotation_determinant": record.get("object_rotation_determinant"),
        "fps": record.get("fps"),
        "frame_count": record.get("frame_count"),
        "joint_count": record.get("joint_count"),
        "channel_count": record.get("channel_count"),
    }


def _scene_axis(source):
    return {
        "mode": "SCENE_NATIVE",
        "source": "current Blender scene object; no import axis conversion inferred",
        "forward": "BLENDER_NATIVE",
        "up": "Z",
        "scale": 1.0,
        "scale_semantics": "Blender units after existing scene import",
        "object_world": _uniform_positive(source),
    }


def _mmd_axis(source, metadata):
    source_profile = metadata.get("_source_profile") or {}
    pmx = metadata.get("pmx") or {}
    if not pmx and isinstance(metadata.get("source_graph"), dict):
        graph = metadata["source_graph"]
        pmx = {
            "bones": graph.get("pmx_bones"),
            "vertices": (graph.get("pmx") or {}).get("vertices"),
        }
    action = metadata.get("action") or {}
    return {
        "mode": "MMD_NATIVE",
        "source": ("mmd_source_backend persisted profile; no MMD Tools runtime "
                   "or MMD RNA is used for evaluation"),
        "forward": "BLENDER_NATIVE",
        "up": "Z",
        "scale": 1.0,
        "scale_semantics": "Blender units in the existing native source file",
        "object_world": _uniform_positive(source),
        "metadata_profile_sha256": metadata.get("_profile_sha256"),
        "metadata_profile_file_sha256": metadata.get("_profile_file_sha256"),
        "source_profile_schema": source_profile.get("schema") or metadata.get("schema"),
        "source_profile_origin": source_profile.get("origin"),
        "mapping_sha256": source_profile.get("mapping_sha256"),
        "mapping_profile_sha256": source_profile.get("mapping_profile_sha256"),
        "pmx_bones": pmx.get("bones") or source_profile.get("pmx_bones"),
        "pmx_vertices": pmx.get("vertices") or source_profile.get("pmx_vertices"),
        "source_action": action.get("name"),
        "source_slot": action.get("slot"),
    }


def _capture_clip_no_toes(source, mapping, *, clip, origin_frame=None):
    start, end = float(clip["range"][0]), float(clip["range"][1])
    origin = start if origin_frame is None else float(origin_frame)
    require(math.isfinite(origin) and start <= origin <= end,
            "clip origin必须位于源Action帧域内")
    scene = bpy.context.scene
    scene.frame_set(math.floor(origin), subframe=origin - math.floor(origin))
    evaluated = pose._update(source)
    world = evaluated.matrix_world
    pose_matrices = {
        semantic: world @ evaluated.pose.bones[name].matrix
        for semantic, name in mapping["bones"].items()
    }
    rest_matrices = cal.source_rest_world(source, mapping)
    differences = {
        semantic: cal._angle_degrees(rest_matrices[semantic], pose_matrices[semantic])
        for semantic in mapping["bones"]
    }
    max_difference = max(differences.values())
    foot_points = []
    for side in SIDES:
        pose_bone = evaluated.pose.bones[mapping["bones"]["foot." + side]]
        foot_points.extend((world @ pose_bone.head, world @ pose_bone.tail))
    ground_z = min(point.z for point in foot_points)
    hip = pose_matrices["hips"].translation.copy()
    record = {
        "action": clip["action"].name,
        "slot": clip["slot"].identifier,
        "slot_handle": getattr(clip["slot"], "handle", None),
        "fps": float(clip["fps"]),
        "range": [start, end],
        "origin_frame": origin,
        "origin_mode": TRANSLATION_ORIGIN_CLIP_START,
        "origin_is_clip_start": abs(origin - start) <= 1e-9,
        "action_signature": cal.action_signature(clip["action"]),
        "source_hip_world": list(hip),
        "source_ground_z_world": float(ground_z),
        "source_hip_height_world": float(hip.z - ground_z),
        "source_clip_start_to_rest_degrees": dict(sorted(differences.items())),
        "max_clip_start_to_rest_degrees": float(max_difference),
        "clip_start_is_source_rest_pose": bool(max_difference <= 0.1),
        "first_frame_is_neutral": bool(max_difference <= 0.1),
        "source_pose_sha256": _hash(
            {semantic: cal.matrix_list(matrix)
             for semantic, matrix in sorted(pose_matrices.items())}),
        "source_leg_lengths_at_clip_start_world": {
            side: float(value) for side, value in cal._source_pose_legs(pose_matrices).items()},
    }
    return {"clip": clip, "record": record, "pose_matrices": pose_matrices,
            "rest_matrices": rest_matrices, "source_hip": hip}


def capture_clip(source, mapping, *, clip=None, source_fps=None, origin_frame=None,
                 translation_origin=TRANSLATION_ORIGIN_CLIP_START,
                 selected_start=None, selected_end=None):
    """Capture selected Action/Slot/fps/origin; never cache a stale origin."""
    if clip is None:
        clip = source_api.active_clip(source, source_fps)
    full_start, full_end = float(clip["range"][0]), float(clip["range"][1])
    selected_start = full_start if selected_start is None else float(selected_start)
    selected_end = full_end if selected_end is None else float(selected_end)
    require(full_start <= selected_start < selected_end <= full_end,
            "选择的clip范围必须位于源Action帧域内且时长为正")
    require(isinstance(translation_origin, str)
            and translation_origin in (TRANSLATION_ORIGIN_CLIP_START,
                                       TRANSLATION_ORIGIN_REST_HIP),
            "位移原点模式无效")
    has_toes = all("toes." + side in mapping["bones"] for side in SIDES)
    if has_toes:
        captured = cal.capture_clip(source, mapping, clip=clip,
                                    source_fps=None, origin_frame=origin_frame)
        record = dict(captured["record"])
    else:
        captured = _capture_clip_no_toes(source, mapping, clip=clip,
                                         origin_frame=origin_frame)
        record = dict(captured["record"])
    record["clip_start_hip_world"] = list(captured["record"]["source_hip_world"])
    record["selected_range"] = [selected_start, selected_end]
    record["translation_origin"] = translation_origin
    record["origin_mode"] = translation_origin
    if translation_origin == TRANSLATION_ORIGIN_REST_HIP:
        rest = cal.source_rest_world(source, mapping)
        rest_hip = rest["hips"].translation.copy()
        record["origin_is_clip_start"] = False
        record["source_hip_world"] = list(rest_hip)
        record["source_hip_height_world"] = float(
            rest_hip.z - record["source_ground_z_world"])
    else:
        record["origin_is_clip_start"] = abs(
            float(record["origin_frame"]) - selected_start) <= 1e-9
    record["clip_start_pose_matches_rest"] = bool(
        record["clip_start_is_source_rest_pose"])
    return captured | {"record": record}


def _rest_hip_clip_record(source, mapping, clip, origin_frame=None,
                          selected_start=None, selected_end=None):
    """REST_HIP preset fidelity: no source pose sampling/frame_set in the factory.

    The displacement origin is source REST, so clip-start pose values cannot
    affect calibration.  Only explicit Action/Slot/fps/range and REST evidence
    are recorded.  This also avoids an extra depsgraph update on rigs whose IK
    constraint state is sensitive to evaluation order.
    """
    start, end = float(clip["range"][0]), float(clip["range"][1])
    selected_start = start if selected_start is None else float(selected_start)
    selected_end = end if selected_end is None else float(selected_end)
    require(start <= selected_start < selected_end <= end,
            "选择的clip范围必须位于源Action帧域内且时长为正")
    origin = start if origin_frame is None else float(origin_frame)
    require(math.isfinite(origin) and start <= origin <= end,
            "clip origin必须位于源Action帧域内")
    rest = cal.source_rest_world(source, mapping)
    rest_hip = rest["hips"].translation.copy()
    ground_points = []
    world = source.matrix_world
    for side in SIDES:
        for semantic in ("foot." + side, "toes." + side):
            if semantic not in mapping["bones"]:
                continue
            bone = source.data.bones[mapping["bones"][semantic]]
            ground_points.extend((world @ bone.head_local, world @ bone.tail_local))
    ground_z = min(point.z for point in ground_points)
    record = {
        "action": clip["action"].name,
        "slot": clip["slot"].identifier,
        "slot_handle": getattr(clip["slot"], "handle", None),
        "fps": float(clip["fps"]),
        "range": [start, end],
        "selected_range": [selected_start, selected_end],
        "origin_frame": origin,
        "origin_mode": TRANSLATION_ORIGIN_REST_HIP,
        "origin_is_clip_start": False,
        "translation_origin": TRANSLATION_ORIGIN_REST_HIP,
        "action_signature": cal.action_signature(clip["action"]),
        "source_hip_world": list(rest_hip),
        "source_hip_world_rest": list(rest_hip),
        "source_clip_start_hip_world": None,
        "source_ground_z_world": float(ground_z),
        "source_hip_height_world": float(rest_hip.z - ground_z),
        "source_clip_start_to_rest_degrees": None,
        "max_clip_start_to_rest_degrees": None,
        "clip_start_is_source_rest_pose": None,
        "first_frame_is_neutral": None,
        "source_pose_sha256": None,
        "source_leg_lengths_at_clip_start_world": None,
        "source_rest_leg_lengths_world": {
            side: float(value) for side, value in cal._source_rest_legs(source, mapping).items()},
        "clip_start_pose_sampled": False,
        "clip_start_pose_note": (
            "REST_HIP preset-native calibration; clip-start pose is not the "
            "displacement origin and is not sampled in the target factory"),
    }
    return {"clip": clip, "record": record, "pose_matrices": None,
            "rest_matrices": rest, "source_hip": rest_hip}


def _source_nla_summary(source):
    ad = source.animation_data
    if ad is None:
        return {
            "exists": False, "action": None, "slot": None, "use_nla": False,
            "use_tweak_mode": False, "blend_type": None, "influence": None,
            "extrapolation": None, "nla_tracks": [],
        }
    tracks = []
    for track in ad.nla_tracks:
        strips = []
        for strip in track.strips:
            action = getattr(strip, "action", None)
            slot = getattr(strip, "action_slot", None)
            strips.append({
                "name": strip.name,
                "frame_start": float(strip.frame_start),
                "frame_end": float(strip.frame_end),
                "action": action.name if action else None,
                "slot": slot.identifier if slot else None,
                "blend_type": getattr(strip, "blend_type", None),
                "influence": float(getattr(strip, "influence", 0.0)),
                "mute": bool(getattr(strip, "mute", False)),
            })
        tracks.append({
            "name": track.name,
            "mute": bool(getattr(track, "mute", False)),
            "solo": bool(getattr(track, "is_solo", False)),
            "strips": strips,
        })
    return {
        "exists": True,
        "action": ad.action.name if ad.action else None,
        "slot": ad.action_slot.identifier if ad.action_slot else None,
        "use_nla": bool(ad.use_nla),
        "use_tweak_mode": bool(ad.use_tweak_mode),
        "blend_type": ad.action_blend_type,
        "influence": float(ad.action_influence),
        "extrapolation": ad.action_extrapolation,
        "nla_tracks": tracks,
    }


def _source_evidence(*, source, format_name, file_path=None, sha256=None,
                     bytes_value=None, importer=None, import_record=None,
                     provenance=None, axis=None, fps=None):
    source_api.armature(source)
    world = _uniform_positive(source)
    clip = source_api.active_clip(source, fps)
    axis = axis or _scene_axis(source)
    if file_path:
        resolved = Path(file_path).resolve()
        if resolved.is_file():
            if sha256 is None:
                sha256 = _file_sha256(resolved)
            if bytes_value is None:
                bytes_value = resolved.stat().st_size
    binding = _source_nla_summary(source)
    return {
        "format": format_name,
        "file": str(Path(file_path).resolve()) if file_path else None,
        "sha256": sha256,
        "bytes": bytes_value,
        "importer": importer,
        "import_record": import_record,
        "provenance": provenance or {},
        "axis": axis,
        "object_world": world,
        "object_unit": world["object_unit"],
        "uniform_positive": world["uniform_positive"],
        "scene_unit": _scene_unit(),
        "fps": float(fps if fps is not None else clip["fps"]),
        "action_timebase": {"fps": float(clip["fps"]),
                            "slot": clip["slot"].identifier,
                            "action": clip["action"].name},
        "action_binding": binding,
        "nla_policy": {
            "mode": "ACTIVE_ACTION_ONLY",
            "nla_composition_implemented": False,
            "nla_tracks_present": bool(binding["nla_tracks"]),
            "isolation": (
                "shared convert_resolved sets use_nla=False, "
                "action_blend_type=REPLACE, action_influence=1.0; "
                "NLA tracks are not composed by this profile layer"),
        },
        "object_name_hint": source.name,
        "data_name_hint": source.data.name if source.data else None,
        "bone_count": len(source.data.bones),
        "rest_fingerprint": cal.bone_rest_signature(source),
        "world_fingerprint": _world_fingerprint(source),
    }


def _axis_checks(profile, source=None):
    errors = []
    warnings = []
    checks = {}
    axis = (profile.get("source") or {}).get("axis")
    checks["axis_metadata_present"] = isinstance(axis, dict) and bool(axis.get("mode"))
    if not checks["axis_metadata_present"]:
        errors.append("来源轴/单位metadata缺失")
        return {"ok": False, "errors": errors, "warnings": warnings, "checks": checks}
    mode = axis.get("mode")
    if mode == "BVH_EXPLICIT":
        required = ("forward", "up", "scale", "target_forward", "target_up")
        missing = [key for key in required if key not in axis]
        checks["bvh_explicit_fields"] = not missing
        if missing:
            errors.append("BVH轴/比例字段缺失：" + ", ".join(missing))
        try:
            scale = _finite_float(axis.get("scale"), "BVH比例无效")
            checks["bvh_positive_scale"] = scale > 0
            if scale <= 0:
                errors.append("BVH比例必须为正数")
        except ValueError as exc:
            checks["bvh_positive_scale"] = False
            errors.append(str(exc))
        determinant = axis.get("object_rotation_determinant")
        if determinant is not None:
            checks["bvh_rotation_determinant"] = abs(float(determinant) - 1.0) <= 1e-6
            if not checks["bvh_rotation_determinant"]:
                errors.append("BVH轴旋转不是纯旋转（determinant != 1）")
    elif mode in ("FBX_IMPORT_NATIVE", "SCENE_NATIVE", "MMD_NATIVE"):
        checks["native_import_evidence"] = bool(axis.get("source"))
        if not checks["native_import_evidence"]:
            errors.append("原生导入轴证据缺失")
        if mode == "MMD_NATIVE":
            checks["mmd_metadata_identity"] = bool(axis.get("metadata_profile_sha256"))
            if not checks["mmd_metadata_identity"]:
                errors.append("MMD源缺少骨元数据profile指纹")
    else:
        errors.append("未知来源轴模式：" + str(mode))
    if source is not None:
        world = _uniform_positive(source)
        checks["source_world_uniform_positive"] = world["uniform_positive"]
        if not world["uniform_positive"]:
            errors.append("来源世界变换非均匀/负缩放/剪切：" + ", ".join(world["reasons"]))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}


def _target_summary(target, mapping, include_fingers, source=None):
    errors = []
    warnings = []
    checks = {}
    source_api.armature(target)
    if source is not None:
        require(target != source, "源和目标不能是同一骨架")
    checks["target_local"] = not target.library and not target.data.library
    if not checks["target_local"]:
        errors.append("目标须为本地可编辑工作副本")
    checks["target_pose_position"] = target.data.pose_position == "POSE"
    if not checks["target_pose_position"]:
        errors.append("目标骨架须使用姿态求值模式")
    world = _uniform_positive(target)
    checks["target_world_uniform_positive"] = world["uniform_positive"]
    if not world["uniform_positive"]:
        errors.append("目标世界变换非均匀/负缩放/剪切：" + ", ".join(world["reasons"]))
    checks["target_upright_z"] = _upright_z(target)
    if not checks["target_upright_z"]:
        errors.append("当前根运动适配要求目标世界Z向上")

    try:
        pose._metadata(target)
        checks["c94_generated_record"] = True
    except Exception as exc:
        checks["c94_generated_record"] = False
        errors.append("目标不是有效C94生成骨架：" + str(exc))

    required = [
        "根骨骼", "躯干", "骶骨控制器", "盆骨控制器", "腰椎控制器",
        "胸腔控制器", "颈椎控制器", "头颅控制器",
        "大臂FK.L", "小臂FK.L", "手掌FK.L", "大腿FK.L", "膝盖FK.L",
        "小腿FK.L", "脚掌FK.L", "脚趾.L",
        "大臂FK.R", "小臂FK.R", "手掌FK.R", "大腿FK.R", "膝盖FK.R",
        "小腿FK.R", "脚掌FK.R", "脚趾.R",
    ]
    if include_fingers:
        required.extend(["食指.L", "中指.L", "无名指.L", "小拇指.L", "大拇指.L",
                         "食指.R", "中指.R", "无名指.R", "小拇指.R", "大拇指.R"])
        required.extend("掌骨%d.%s" % (index, side)
                        for side in SIDES for index in range(1, 6))
        for side in SIDES:
            for finger in FINGERS:
                word = c94.FINGERS[finger]
                segments = ((word, word + "1", word + "2") if finger == "thumb"
                            else tuple(word + str(index) for index in range(1, 4)))
                required.extend(segment + "." + side for segment in segments)
    missing = sorted(name for name in required if name not in target.pose.bones)
    checks["target_controls_present"] = not missing
    if missing:
        errors.append("目标缺少C94控件：" + ", ".join(missing))
    checks["settings_present"] = "骨架设置" in target.pose.bones
    if not checks["settings_present"]:
        errors.append("目标缺少骨架设置")
    if checks["settings_present"]:
        settings = target.pose.bones["骨架设置"]
        missing_modes = [mode for mode in c94.MODES if mode not in settings]
        checks["mode_properties_present"] = not missing_modes
        if missing_modes:
            errors.append("目标缺少FK-IK模式属性：" + ", ".join(missing_modes))

    meshes = []
    scene = bpy.context.scene
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        modifiers = [m for m in obj.modifiers
                     if m.type == "ARMATURE" and m.object == target
                     and m.show_viewport and m.show_render]
        if modifiers:
            meshes.append({"name": obj.name, "vertices": len(obj.data.vertices),
                           "visible": obj.visible_get(), "hide_render": obj.hide_render})
    checks["target_skinned_meshes"] = len(meshes)
    if not meshes:
        warnings.append("目标没有可见/渲染Armature蒙皮Mesh；仅生成本地Action")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "world": world,
        "required_missing": missing,
        "meshes": meshes,
        "mesh_count": len(meshes),
        "mesh_vertices": sum(row["vertices"] for row in meshes),
    }


def build_identity(profile, source, target, *, target_world_matrix=None):
    """Content-only identity; object/data/bone display names are not cache keys."""
    mapping = profile["mapping"]
    calibration_settings = profile["calibration"]
    if target_world_matrix is None:
        target_world_matrix = _target_world_matrix(target)
    payload = {
        "schema": "c94.retarget.profile.identity.v1",
        # v2 deliberately excludes file/path/Action/Slot provenance from the
        # persisted axis metadata.  Records produced by the previous raw-axis
        # identity therefore cannot masquerade as reusable.
        "identity_version": 2,
        "calibration_version": 1,
        "source_content": _content_fingerprint(source),
        "source_axis_sha256": _hash(
            _axis_identity_payload(
                (profile.get("source") or {}).get("axis") or {})),
        "mapping_sha256": _mapping_sha256(mapping),
        "target_content": _content_fingerprint(
            target, world_matrix=target_world_matrix),
        "orientation_reference": calibration_settings["orientation_reference"],
        "translation_origin": calibration_settings["translation_origin"],
        "motion_scale_basis": calibration_settings["motion_scale_basis"],
        "include_fingers": bool(calibration_settings["include_fingers"]),
        "finger_orientation_reference": calibration_settings.get(
            "finger_orientation_reference"),
    }
    return {**payload, "identity_sha256": _hash(payload)}


def _profile_hash(profile):
    return _hash({key: value for key, value in profile.items()
                  if key != "profile_sha256"})


def _record_hash(record):
    return _hash({key: value for key, value in record.items()
                  if key != "record_sha256"})


def _profile_summary(profile):
    source = profile.get("source") or {}
    clip = profile.get("clip") or {}
    calibration = profile.get("calibration") or {}
    return {
        "schema": profile.get("schema"),
        "profile_version": profile.get("profile_version"),
        "id": profile.get("id"),
        "name": profile.get("name"),
        "tier": profile.get("tier"),
        "preset": profile.get("preset"),
        "profile_sha256": profile.get("profile_sha256"),
        "mapping_sha256": profile.get("mapping_sha256"),
        "source": {
            "format": source.get("format"),
            "file": source.get("file"),
            "sha256": source.get("sha256"),
            "fps": source.get("fps"),
            "bone_count": source.get("bone_count"),
            "rest_fingerprint": source.get("rest_fingerprint"),
            "axis_mode": (source.get("axis") or {}).get("mode"),
            "nla_tracks": len((source.get("action_binding") or {}).get("nla_tracks") or []),
            "nla_policy": (source.get("nla_policy") or {}).get("mode"),
        },
        "clip": {
            "action": clip.get("action"),
            "slot": clip.get("slot"),
            "fps": clip.get("fps"),
            "range": clip.get("range"),
            "origin_frame": clip.get("origin_frame"),
            "origin_mode": clip.get("origin_mode"),
        },
        "calibration": {
            "orientation_reference": calibration.get("orientation_reference"),
            "translation_origin": calibration.get("translation_origin"),
            "motion_scale_basis": calibration.get("motion_scale_basis"),
            "include_fingers": calibration.get("include_fingers"),
            "unit_division": calibration.get("unit_division"),
        },
        "calibrations": len(profile.get("calibrations") or []),
    }


def _new_profile(*, source, format_name, mapping, preset, tier, clip,
                 source_evidence, name=None, profile_id=None,
                 translation_origin=TRANSLATION_ORIGIN_CLIP_START,
                 motion_scale_basis=MOTION_SCALE_LEG_LENGTH,
                 include_fingers=None):
    mapping = dict(mapping)
    mapping["schema"] = mapping.get("schema", "c94.retarget.mapping.v1")
    mapping["preset"] = preset
    chains = _finger_chains(mapping)
    if include_fingers is None:
        include_fingers = bool(chains["complete"]) and not chains["skipped"]
    mapping["fingers"] = chains
    mapping["missing_optional"] = sorted(
        set(mapping.get("missing_optional") or [])
        | ((REQUIRED_BODY | OPTIONAL_BODY) - set(mapping["bones"])))
    mapping["unmapped_channels"] = sorted(
        set(mapping.get("unmapped_channels") or [])
        | ({"fingers"} if not include_fingers else set()))
    if preset in VALIDATED_PRESETS:
        mapping.setdefault("support", "validated_preset_body"
                           + ("_complete_fingers" if include_fingers else ""))
    else:
        mapping.setdefault("support", "configured_profile")
    canonical_bones = _canonical_preset_bones(source, preset)
    canonical_match = (canonical_bones is not None
                       and dict(mapping["bones"]) == canonical_bones)
    preset_evidence = {
        "preset": preset,
        "verified_against_source": bool(canonical_bones is not None),
        "canonical_match": bool(canonical_match),
        "canonical_bones_sha256": _preset_bones_sha256(canonical_bones),
        "source_format": source_evidence.get("format"),
        "source_rest_fingerprint": source_evidence.get("rest_fingerprint"),
        "source_world_fingerprint": source_evidence.get("world_fingerprint"),
    }
    mapping_checks = validate_mapping(source, mapping, include_fingers=include_fingers)
    axis_checks = _axis_checks({"source": source_evidence}, source)
    if mapping_checks["errors"] or axis_checks["errors"]:
        tier = TIER_NEEDS_ADAPTATION
    elif canonical_match:
        tier = TIER_VALIDATED_PRESET
    else:
        tier = TIER_CONFIGURED
    profile = {
        "schema": PROFILE_SCHEMA,
        "profile_version": PROFILE_VERSION,
        "id": profile_id or ("profile-" + _hash({
            "source": source_evidence.get("rest_fingerprint"),
            "world": source_evidence.get("world_fingerprint"),
            "mapping": _mapping_sha256(mapping),
            "clip": [clip.get("action"), clip.get("slot"), clip.get("fps")],
        })[:16]),
        "name": name or (source.name + " / " + str(clip.get("action"))),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tier": tier,
        "preset": preset,
        "preset_mapping_sha256": (
            _preset_bones_sha256(canonical_bones) if canonical_bones else None),
        "preset_evidence": preset_evidence,
        "source": source_evidence,
        "mapping": mapping,
        "mapping_sha256": _mapping_sha256(mapping),
        "mapping_checks": {
            "ok": mapping_checks["ok"],
            "tier": mapping_checks["tier"],
            "errors": mapping_checks["errors"],
            "warnings": mapping_checks["warnings"],
            "checks": mapping_checks["checks"],
            "finger_chains": mapping_checks["finger_chains"],
        },
        "clip": clip,
        "calibration": {
            "schema": "c94.retarget.profile.calibration-settings.v1",
            "calibration_version": 1,
            "orientation_reference": ORIENTATION_REFERENCE,
            "translation_origin": translation_origin,
            "motion_scale_basis": motion_scale_basis,
            "include_fingers": bool(include_fingers),
            "finger_orientation_reference":
                "source_rest_child_joint_directions_with_palm_roll",
            "unit_division": {"source": 0, "target": 1},
            "rest_hip_division": False,
        },
        "calibrations": [],
    }
    profile["profile_sha256"] = _profile_hash(profile)
    _json_only(profile)
    return profile


def from_mixamo(source, *, import_record=None, mapping_overrides=None,
                name=None, include_fingers=True,
                translation_origin=TRANSLATION_ORIGIN_REST_HIP,
                motion_scale_basis=MOTION_SCALE_REST_HIP_HEIGHT,
                source_fps=None):
    """Export the accepted Mixamo name preset as a pure-JSON profile.

    The default calibration semantics reproduce the existing Mixamo exact
    output (rest-hip displacement origin and rest-hip-height scale).  A user may
    explicitly select CLIP_START/LEG_LENGTH for the generic calibration path.
    """
    source_api.armature(source)
    record = import_record
    if record is None:
        try:
            record = json.loads(source.get(source_api.SOURCE_KEY, ""))
        except (ValueError, TypeError):
            record = None
    mapping = source_api.mapping(source, mapping_overrides)
    preset = "mixamo_names_candidate"
    expected = source_api.mapping(source)["bones"]
    tier = (TIER_VALIDATED_PRESET if mapping["bones"] == expected
            and isinstance(record, dict) and record.get("format") == "FBX"
            else TIER_CONFIGURED)
    clip = source_api.active_clip(source, source_fps)
    axis = _fbx_axis(record, source)
    evidence = _source_evidence(
        source=source, format_name="FBX",
        file_path=(record or {}).get("file"), sha256=(record or {}).get("sha256"),
        bytes_value=(record or {}).get("bytes"), importer=(record or {}).get("importer"),
        import_record=record, provenance=(record or {}).get("provenance"),
        axis=axis, fps=clip["fps"])
    with preserve_scene_state():
        captured = capture_clip(source, mapping, clip=clip,
                                translation_origin=translation_origin)
    return _new_profile(
        source=source, format_name="FBX", mapping=mapping, preset=preset,
        tier=tier, clip=captured["record"], source_evidence=evidence,
        name=name, translation_origin=translation_origin,
        motion_scale_basis=motion_scale_basis, include_fingers=include_fingers)


def from_cmu(source, *, import_record=None, name=None, source_fps=None):
    """Export the accepted CMU exact hierarchy as a pure-JSON profile."""
    source_api.armature(source)
    record = import_record
    if record is None:
        try:
            saved = json.loads(source.get(cal_source_key(), ""))
            record = saved.get("record") if isinstance(saved, dict) else None
        except (ValueError, TypeError):
            record = None
    mapping = source_api.exact_cmu_mapping(source)
    clip = source_api.active_clip(source, source_fps)
    axis = _bvh_axis(record) if record else {
        "mode": "BVH_EXPLICIT",
        "source": "explicit profile; no import record available",
        "forward": "UNKNOWN",
        "up": "UNKNOWN",
        "scale": None,
    }
    evidence = _source_evidence(
        source=source, format_name="BVH",
        file_path=(record or {}).get("file"), sha256=(record or {}).get("sha256"),
        bytes_value=(record or {}).get("bytes"), importer=(record or {}).get("importer"),
        import_record=record, axis=axis, fps=clip["fps"])
    with preserve_scene_state():
        captured = capture_clip(source, mapping, clip=clip,
                                translation_origin=TRANSLATION_ORIGIN_CLIP_START)
    return _new_profile(
        source=source, format_name="BVH", mapping=mapping,
        preset="cmu_exact_31_joint_sample_v1", tier=TIER_VALIDATED_PRESET,
        clip=captured["record"], source_evidence=evidence, name=name,
        translation_origin=TRANSLATION_ORIGIN_CLIP_START,
        motion_scale_basis=MOTION_SCALE_LEG_LENGTH, include_fingers=False)


def cal_source_key():
    """Delayed import avoids a circular dependency at module import time."""
    from . import retarget_bvh
    return retarget_bvh.SOURCE_KEY


def from_bvh(source, mapping, *, import_record, name=None, source_fps=None,
             include_fingers=None):
    """Generic explicit BVH mapping; CMU exact mapping remains a validated preset."""
    source_api.armature(source)
    mapping = _coerce_mapping(mapping, "generic_bvh_profile")
    preset = mapping["preset"]
    tier = TIER_CONFIGURED
    try:
        exact_cmu = source_api.exact_cmu_mapping(source)
    except ValueError:
        exact_cmu = None
    if exact_cmu is not None and mapping.get("bones") == exact_cmu["bones"]:
        preset = "cmu_exact_31_joint_sample_v1"
        mapping["preset"] = preset
        tier = TIER_VALIDATED_PRESET
    clip = source_api.active_clip(source, source_fps)
    axis = _bvh_axis(import_record)
    evidence = _source_evidence(
        source=source, format_name="BVH",
        file_path=import_record.get("file"), sha256=import_record.get("sha256"),
        bytes_value=import_record.get("bytes"), importer=import_record.get("importer"),
        import_record=import_record, axis=axis, fps=clip["fps"])
    with preserve_scene_state():
        captured = capture_clip(source, mapping, clip=clip,
                                translation_origin=TRANSLATION_ORIGIN_CLIP_START)
    return _new_profile(
        source=source, format_name="BVH", mapping=mapping, preset=preset,
        tier=tier, clip=captured["record"], source_evidence=evidence, name=name,
        translation_origin=TRANSLATION_ORIGIN_CLIP_START,
        motion_scale_basis=MOTION_SCALE_LEG_LENGTH,
        include_fingers=include_fingers)


def from_fbx(source, mapping, *, import_record=None, name=None, source_fps=None,
             include_fingers=None):
    """Generic explicit FBX mapping; unknown names are accepted when complete."""
    source_api.armature(source)
    record = import_record
    if record is None:
        try:
            record = json.loads(source.get(source_api.SOURCE_KEY, ""))
        except (ValueError, TypeError):
            record = None
    mapping = _coerce_mapping(mapping, "generic_fbx_profile")
    preset = mapping["preset"]
    tier = TIER_CONFIGURED
    try:
        mixamo_mapping = source_api.mapping(source)
    except ValueError:
        mixamo_mapping = None
    if record and mixamo_mapping is not None \
            and mapping.get("bones") == mixamo_mapping["bones"]:
        preset = "mixamo_names_candidate"
        mapping["preset"] = preset
        tier = TIER_VALIDATED_PRESET
    clip = source_api.active_clip(source, source_fps)
    axis = _fbx_axis(record, source)
    evidence = _source_evidence(
        source=source, format_name="FBX",
        file_path=(record or {}).get("file"), sha256=(record or {}).get("sha256"),
        bytes_value=(record or {}).get("bytes"), importer=(record or {}).get("importer"),
        import_record=record, provenance=(record or {}).get("provenance"),
        axis=axis, fps=clip["fps"])
    with preserve_scene_state():
        captured = capture_clip(source, mapping, clip=clip,
                                translation_origin=TRANSLATION_ORIGIN_CLIP_START)
    return _new_profile(
        source=source, format_name="FBX", mapping=mapping, preset=preset,
        tier=tier, clip=captured["record"], source_evidence=evidence, name=name,
        translation_origin=TRANSLATION_ORIGIN_CLIP_START,
        motion_scale_basis=MOTION_SCALE_LEG_LENGTH,
        include_fingers=include_fingers)


def from_scene(source, mapping, *, name=None, source_fps=None,
               include_fingers=None):
    """Scene Action source already in Blender; axes/unit come from scene metadata."""
    source_api.armature(source)
    mapping = _coerce_mapping(mapping, "generic_scene_action_profile")
    clip = source_api.active_clip(source, source_fps)
    evidence = _source_evidence(
        source=source, format_name="SCENE_ACTION",
        file_path=bpy.data.filepath or None,
        sha256=(_file_sha256(bpy.data.filepath) if bpy.data.filepath else None),
        bytes_value=(os.path.getsize(bpy.data.filepath) if bpy.data.filepath else None),
        importer="scene_action", axis=_scene_axis(source), fps=clip["fps"])
    with preserve_scene_state():
        captured = capture_clip(source, mapping, clip=clip,
                                translation_origin=TRANSLATION_ORIGIN_CLIP_START)
    return _new_profile(
        source=source, format_name="SCENE_ACTION", mapping=mapping,
        preset=mapping["preset"], tier=TIER_CONFIGURED,
        clip=captured["record"], source_evidence=evidence, name=name,
        translation_origin=TRANSLATION_ORIGIN_CLIP_START,
        motion_scale_basis=MOTION_SCALE_LEG_LENGTH,
        include_fingers=include_fingers)


def from_mmd(source, metadata_profile=None, *, mapping=None, mapping_profile=None,
             source_blend=None, clip_id=None, source_profile_origin=None,
             name=None, source_fps=None, backend_module=None):
    """Export an already-evaluated MMD source from a persisted profile.

    Accepted inputs:

    * ``c94.retarget.mmd_profile.v1`` (the mmd_source_backend persisted profile);
      its ``mapping``/``mapping_profile`` are consumed as data, preferably via
      an explicit result from ``mmd_source_backend.mapping_from_profile``;
    * explicit ``mapping`` + ``mapping_profile`` from that backend result;
    * legacy ``c94.retarget.mmd.source_profile.v1`` metadata for existing
      development profiles, explicitly marked as legacy/not backend-integrated.

    This reads no MMD RNA and runs no MMD Tools operator.  The actual source
    bones are still checked by this module's mapping validation.
    """
    source_api.armature(source)
    document = None
    profile_path = None
    file_sha = None
    if metadata_profile is not None:
        if isinstance(metadata_profile, bpy.types.Text):
            document = json.loads(metadata_profile.as_string())
            file_sha = _hash(document)
        elif isinstance(metadata_profile, (str, os.PathLike)):
            profile_path = Path(bpy.path.abspath(os.fspath(metadata_profile))).resolve(strict=True)
            document = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            file_sha = _file_sha256(profile_path)
        elif isinstance(metadata_profile, dict):
            document = dict(metadata_profile)
            file_sha = _hash(document)
        else:
            raise ValueError("MMD profile必须是路径、Text或JSON对象")
    schema = document.get("schema") if isinstance(document, dict) else None
    backend_mapping_result = None
    if schema == "c94.retarget.mmd_profile.v1":
        if backend_module is None:
            try:
                from . import mmd_source_backend as backend_module
            except ImportError:
                try:
                    import mmd_source_backend as backend_module
                except ImportError as exc:
                    raise ValueError(
                        "backend MMD profile需要mmd_source_backend；"
                        "请传入backend_module或先集成backend模块") from exc
        require(getattr(backend_module, "PROFILE_SCHEMA", None)
                == "c94.retarget.mmd_profile.v1",
                "backend_module PROFILE_SCHEMA不符")
        backend_module.validate_profile_document(document)
        require(document.get("mapping_sha256")
                == backend_module.canonical_hash(document["mapping"]),
                "MMD backend mapping_sha256 canonical哈希不符")
        require(document.get("mapping_profile_sha256")
                == backend_module.canonical_hash(document["mapping_profile"]),
                "MMD backend mapping_profile_sha256 canonical哈希不符")
        # Consume the backend's actual verified mapping result instead of
        # trusting a caller-provided mapping that merely looks similar.
        backend_mapping_result = backend_module.mapping_from_profile(
            source, document, validate=True)
        require(isinstance(backend_mapping_result, (tuple, list))
                and len(backend_mapping_result) == 2,
                "backend.mapping_from_profile未返回(mapping, mapping_profile)")
    explicit_mapping = mapping is not None
    if explicit_mapping:
        require(mapping_profile is not None,
                "显式MMD mapping必须同时给出mapping_profile")
        mapping = _coerce_mapping(
            mapping, "mmd_pmx_exact_body_four_node_fingers_v1")
        mapping_profile = dict(mapping_profile)
        if backend_mapping_result is not None:
            expected_mapping = _coerce_mapping(
                backend_mapping_result[0],
                "mmd_pmx_exact_body_four_node_fingers_v1")
            require(
                _hash(_mapping_payload(mapping))
                == _hash(_mapping_payload(expected_mapping)),
                "显式MMD mapping与backend.mapping_from_profile结果不符")
            require(
                backend_module.canonical_hash(mapping_profile)
                == backend_module.canonical_hash(backend_mapping_result[1]),
                "显式mapping_profile与backend.mapping_from_profile结果不符")
            profile_origin = source_profile_origin or "backend.mapping_from_profile"
        else:
            profile_origin = (source_profile_origin
                              or "backend.mapping_from_profile")
    elif schema == "c94.retarget.mmd_profile.v1":
        mapping = _coerce_mapping(
            backend_mapping_result[0],
            "mmd_pmx_exact_body_four_node_fingers_v1")
        mapping_profile = dict(backend_mapping_result[1])
        require(mapping_profile, "MMD backend profile缺少mapping_profile")
        profile_origin = (source_profile_origin
                          or "backend.mapping_from_profile")
    elif schema == "c94.retarget.mmd.source_profile.v1":
        mapping = _coerce_mapping(
            document.get("mapping") or {},
            "mmd_pmx_exact_body_four_node_fingers_v1")
        mapping_profile = dict(
            document.get("profile") or document.get("mapping_profile") or {})
        profile_origin = (source_profile_origin
                          or "legacy_metadata_profile")
    else:
        raise ValueError(
            "MMD profile schema不受支持；接受 c94.retarget.mmd_profile.v1、"
            "legacy c94.retarget.mmd.source_profile.v1，或显式mapping+mapping_profile")
    mapping["schema"] = "c94.retarget.mapping.v1"
    mapping.setdefault("preset", "mmd_pmx_exact_body_four_node_fingers_v1")
    mapping.setdefault("support", "configured_from_mmd_profile")

    backend_clip = None
    if schema == "c94.retarget.mmd_profile.v1":
        require(clip_id is not None,
                "backend MMD profile必须显式clip_id，不接受自动primary")
        clips = document.get("clips") or []
        chosen_id = clip_id
        backend_clip = next(
            (row for row in clips if row.get("id") == chosen_id), None)
        require(backend_clip is not None,
                "MMD backend profile没有指定clip：" + str(chosen_id))
        require(backend_clip.get("body_convertible", True),
                "MMD backend clip不可做身体转换：" + str(chosen_id))
        action_record = backend_clip.get("armature_action") or {}
        action_name = action_record.get("name")
        slot_id = action_record.get("slot")
        clip_fps = float(backend_clip.get("fps"))
        ad = source.animation_data
        require(ad is not None and ad.action is not None
                and ad.action.name == action_name
                and ad.action_slot is not None
                and ad.action_slot.identifier == slot_id,
                "请显式选择backend clip的Action/Slot：" + str(chosen_id))
        if source_fps is not None:
            require(abs(float(source_fps) - clip_fps) <= 1e-9,
                    "显式source fps与backend clip fps不符")
        clip = source_api.active_clip(source, clip_fps)
    else:
        clip = source_api.active_clip(source, source_fps)
        clip_fps = float(clip["fps"])
    require(clip["action"].name == (backend_clip.get("armature_action") or {}).get("name")
            if backend_clip else True,
            "当前源Action与backend clip不符")
    require(clip["slot"].identifier == (backend_clip.get("armature_action") or {}).get("slot")
            if backend_clip else True,
            "当前源Slot与backend clip不符")

    source_graph = document.get("source_graph") if isinstance(document, dict) else {}
    pmx = (document.get("pmx") if isinstance(document, dict) else None) or {}
    if isinstance(source_graph, dict):
        pmx = dict(pmx)
        pmx.setdefault("bones", source_graph.get("pmx_bones"))
        pmx.setdefault("vertices", (source_graph.get("pmx") or {}).get("vertices"))
    profile_sha = (document.get("profile_sha256")
                   if isinstance(document, dict) and document.get("profile_sha256")
                   else _hash({"mapping": mapping, "mapping_profile": mapping_profile}))
    source_profile = {
        "schema": schema,
        "origin": profile_origin,
        "backend_integration": profile_origin.startswith("backend"),
        "file": str(profile_path) if profile_path else None,
        "file_sha256": file_sha,
        "profile_sha256": profile_sha,
        "source_schema": (document.get("source_schema")
                          if isinstance(document, dict) else None),
        "primary_clip": (document.get("primary_clip")
                         if isinstance(document, dict) else None),
        "clip_id": backend_clip.get("id") if backend_clip else None,
        "mapping_sha256": (document.get("mapping_sha256")
                           if isinstance(document, dict) else None),
        "mapping_profile_sha256": (document.get("mapping_profile_sha256")
                                   if isinstance(document, dict) else None),
        "pmx_bones": pmx.get("bones"),
        "pmx_vertices": pmx.get("vertices"),
        "backend": {
            "id": (document.get("backend") or {}).get("id"),
            "version": (document.get("backend") or {}).get("version"),
            "license": (document.get("backend") or {}).get("license"),
        } if isinstance(document, dict) else None,
        "backend_validation": {
            "validate_profile_document": bool(backend_mapping_result is not None),
            "mapping_from_profile": bool(backend_mapping_result is not None),
            "clip_id_explicit": bool(schema == "c94.retarget.mmd_profile.v1"
                                     and clip_id is not None),
        } if schema == "c94.retarget.mmd_profile.v1" else None,
        "backend_module": (str(getattr(backend_module, "__file__", ""))
                           if backend_module is not None else None),
        "inputs": document.get("inputs") if isinstance(document, dict) else None,
        "objects": document.get("objects") if isinstance(document, dict) else None,
        "scene_config_sha256": (document.get("scene_config_sha256")
                                if isinstance(document, dict) else None),
        "limitations": document.get("limitations") if isinstance(document, dict) else None,
    }
    metadata = dict(document or {})
    metadata["_profile_sha256"] = profile_sha
    metadata["_profile_file_sha256"] = file_sha
    metadata["_source_profile"] = source_profile
    metadata["pmx"] = pmx
    metadata["action"] = {
        "name": clip["action"].name, "slot": clip["slot"].identifier}
    axis = _mmd_axis(source, metadata)
    if source_blend is None and isinstance(document, dict):
        source_blend = ((document.get("scene_config") or {}).get("source_blend")
                        or (document.get("source") or {}).get("blend"))
    evidence = _source_evidence(
        source=source, format_name="MMD_PMX_NATIVE",
        file_path=source_blend, sha256=None, bytes_value=None,
        importer=("mmd_source_backend.persisted_profile"
                  if source_profile["backend_integration"]
                  else "mmd_tools_imported_native_source(legacy_metadata)"),
        import_record=None, axis=axis, fps=clip_fps)
    evidence["source_profile"] = source_profile
    sem = (mapping_profile.get("semantics")
           or (document.get("profile") or {}).get("semantics") or {})
    evidence["bone_metadata"] = {
        semantic: {
            "parent": (sem.get(semantic) or {}).get("parent"),
            "use_deform": (sem.get(semantic) or {}).get("use_deform"),
            "is_tip": (sem.get(semantic) or {}).get("is_tip"),
            "positive_weight": (sem.get(semantic) or {}).get("positive_weight"),
            "name_j_nfkc": ((sem.get(semantic) or {}).get("name_j_nfkc")
                            or (sem.get(semantic) or {}).get("canonical_name_j")),
            "blender_bone": (sem.get(semantic) or {}).get("blender_bone"),
        }
        for semantic in mapping["bones"] if semantic in sem
    }
    evidence["metadata_profile"] = {
        "schema": schema,
        "sha256": profile_sha,
        "file_sha256": file_sha,
        "source_profile_origin": profile_origin,
        "backend_integration": source_profile["backend_integration"],
        "pmx": pmx,
        "action": metadata["action"],
        "source": (document.get("source") if isinstance(document, dict) else None),
        "limitations": (document.get("limitations")
                        if isinstance(document, dict) else None),
    }
    with preserve_scene_state():
        captured = capture_clip(
            source, mapping, clip=clip,
            translation_origin=TRANSLATION_ORIGIN_CLIP_START)
    return _new_profile(
        source=source, format_name="MMD_PMX_NATIVE", mapping=mapping,
        preset=mapping.get("preset") or "mmd_pmx_exact_body_four_node_fingers_v1",
        tier=TIER_CONFIGURED, clip=captured["record"],
        source_evidence=evidence, name=name,
        translation_origin=TRANSLATION_ORIGIN_CLIP_START,
        motion_scale_basis=MOTION_SCALE_LEG_LENGTH,
        include_fingers=True)


def _axis_checks_impl(profile, source=None):
    return _axis_checks(profile, source)


def validate_profile(profile, source=None, target=None, *, clip=None,
                     check_files=True):
    """Validate schema, mapping, source/target metadata and calibration reuse."""
    errors = []
    warnings = []
    checks = {}
    _json_only(profile)
    checks["schema"] = profile.get("schema") == PROFILE_SCHEMA
    if not checks["schema"]:
        errors.append("profile schema不受支持")
    checks["profile_version"] = profile.get("profile_version") == PROFILE_VERSION
    if not checks["profile_version"]:
        errors.append("profile版本不受支持")
    stored_hash = profile.get("profile_sha256")
    checks["profile_hash"] = bool(stored_hash) and stored_hash == _profile_hash(profile)
    if not checks["profile_hash"]:
        errors.append("profile内容哈希不匹配")
    checks["tier_declared"] = profile.get("tier") in TIERS
    if not checks["tier_declared"]:
        errors.append("profile分级无效")

    mapping = profile.get("mapping") or {}
    calibration = profile.get("calibration") or {}
    include_fingers = bool(calibration.get("include_fingers"))
    checks["calibration_settings"] = all(
        key in calibration for key in (
            "orientation_reference", "translation_origin", "motion_scale_basis",
            "include_fingers", "unit_division"))
    if not checks["calibration_settings"]:
        errors.append("profile校准设置不完整")
    checks["orientation_reference"] = (
        calibration.get("orientation_reference") == ORIENTATION_REFERENCE)
    if not checks["orientation_reference"]:
        errors.append("朝向参考必须为REST")
    mapping_checks = validate_mapping(source, mapping, include_fingers=include_fingers)
    checks["mapping"] = mapping_checks["ok"]
    errors.extend(mapping_checks["errors"])
    warnings.extend(mapping_checks["warnings"])
    if source is None:
        stored_checks = profile.get("mapping_checks") or {}
        checks["stored_mapping_checks"] = stored_checks.get("ok") is True
        if not checks["stored_mapping_checks"]:
            errors.append("未提供源骨架，且profile没有通过的映射检查记录")
    checks["mapping_sha256"] = (
        profile.get("mapping_sha256") == _mapping_sha256(mapping))
    if not checks["mapping_sha256"]:
        errors.append("mapping内容哈希不匹配")
    checks["mapping_explicit"] = mapping_checks["checks"].get("mapping_explicit", False)
    checks["required_semantics"] = mapping_checks["checks"].get("required_semantics", False)
    checks["unique_bones"] = mapping_checks["checks"].get("unique_bones", False)
    checks["bone_names_exist"] = mapping_checks["checks"].get("bone_names_exist")
    checks["parent_chains"] = mapping_checks["checks"].get("parent_chains")
    checks["left_right_distinct"] = mapping_checks["checks"].get("left_right_distinct")
    checks["child_joints_positive"] = mapping_checks["checks"].get("child_joints_positive")
    checks["side_geometry"] = mapping_checks["checks"].get("side_geometry")
    if checks["side_geometry"] is False:
        warnings.append("左右几何镜像证据不足；仍按显式映射")

    axis_checks = _axis_checks(profile, source)
    checks["axis_metadata"] = axis_checks["ok"]
    errors.extend(axis_checks["errors"])
    warnings.extend(axis_checks["warnings"])

    source_evidence = profile.get("source") or {}
    checks["source_evidence"] = bool(source_evidence.get("format"))
    if not checks["source_evidence"]:
        errors.append("来源证据缺失")
    checks["source_unit"] = (isinstance(source_evidence.get("object_unit"), (int, float))
                             and math.isfinite(float(source_evidence["object_unit"]))
                             and float(source_evidence["object_unit"]) > 0)
    if not checks["source_unit"]:
        errors.append("来源对象unit无效")
    binding = source_evidence.get("action_binding") or {}
    nla_tracks = binding.get("nla_tracks") or []
    checks["nla_tracks_present"] = bool(nla_tracks)
    checks["nla_composition_implemented"] = bool(
        (source_evidence.get("nla_policy") or {}).get("nla_composition_implemented"))
    if nla_tracks:
        warnings.append(
            "源含NLA轨；当前profile只隔离并取活动Action/Slot，"
            "不合成NLA（显式合成采样 not_run）")
    source_file = source_evidence.get("file")
    if check_files and source_file and os.path.isfile(source_file):
        current_sha = _file_sha256(source_file)
        checks["source_file_sha"] = current_sha == source_evidence.get("sha256")
        if not checks["source_file_sha"]:
            warnings.append("来源文件SHA已变化；以rest/world/mapping内容指纹判定校准复用")
    else:
        checks["source_file_sha"] = None

    clip_record = profile.get("clip") or {}
    checks["clip_explicit"] = all(key in clip_record and clip_record[key] not in (None, "", [])
                                  for key in ("action", "slot", "fps", "range"))
    if not checks["clip_explicit"]:
        errors.append("源Action/Slot/fps/range必须明确")
    if clip_record:
        try:
            fps = _finite_float(clip_record.get("fps"), "源fps无效")
            checks["clip_fps_positive"] = fps > 0
            if fps <= 0:
                errors.append("源fps必须为正数")
        except ValueError as exc:
            checks["clip_fps_positive"] = False
            errors.append(str(exc))
        checks["clip_origin_mode"] = clip_record.get("origin_mode") in (
            TRANSLATION_ORIGIN_CLIP_START, TRANSLATION_ORIGIN_REST_HIP)
        if not checks["clip_origin_mode"]:
            errors.append("clip位移原点模式无效")
        checks["calibration_settings_origin"] = calibration.get("translation_origin") in (
            TRANSLATION_ORIGIN_CLIP_START, TRANSLATION_ORIGIN_REST_HIP)
        if not checks["calibration_settings_origin"]:
            errors.append("校准位移原点设置无效")
        checks["calibration_settings_scale"] = calibration.get("motion_scale_basis") in (
            MOTION_SCALE_LEG_LENGTH, MOTION_SCALE_REST_HIP_HEIGHT, MOTION_SCALE_NONE)
        if not checks["calibration_settings_scale"]:
            errors.append("校准比例设置无效")
        checks["unit_division_once"] = (
            (calibration.get("unit_division") or {}) == {"source": 0, "target": 1})
        if not checks["unit_division_once"]:
            errors.append("单位除法契约必须为source=0,target=1")

    current_clip = clip
    checks["clip_timebase_available"] = None
    if current_clip is None and source is not None and source.animation_data \
            and source.animation_data.action is not None:
        try:
            current_clip = source_api.active_clip(source, None)
            checks["clip_timebase_available"] = True
        except ValueError as exc:
            current_clip = None
            checks["clip_timebase_available"] = False
            warnings.append(
                "当前源Action/Slot没有可信TIME_KEY；必须显式source_fps，"
                "不会回退profile旧时基：" + str(exc))
    elif current_clip is not None:
        checks["clip_timebase_available"] = True
    checks["clip_matches_selected"] = None
    if current_clip is not None:
        checks["clip_matches_selected"] = (
            current_clip["action"].name == clip_record.get("action")
            and current_clip["slot"].identifier == clip_record.get("slot"))
        if not checks["clip_matches_selected"]:
            warnings.append("当前源Action/Slot与profile记录不同；将重新求clip原点")

    target_checks = None
    if target is not None:
        require(source is not None, "同时检查目标时必须给出源骨架")
        require(source != target, "源和目标不能是同一骨架")
        target_checks = _target_summary(target, mapping, include_fingers, source=source)
        errors.extend(target_checks["errors"])
        warnings.extend(target_checks["warnings"])
        checks["target_controls"] = target_checks["checks"].get("target_controls_present")
        checks["target_world_uniform_positive"] = target_checks["checks"].get(
            "target_world_uniform_positive")
        checks["target_upright_z"] = target_checks["checks"].get("target_upright_z")
        checks["target_c94_record"] = target_checks["checks"].get("c94_generated_record")
        checks["target_skinned_meshes"] = target_checks["checks"].get("target_skinned_meshes")

    identity = None
    calibration_info = {"state": "not_checked", "records": len(profile.get("calibrations") or [])}
    if source is not None and target is not None and not errors:
        identity = build_identity(profile, source, target)
        matching = _find_calibration(profile, identity)
        if matching is None:
            calibration_info.update({"state": "missing", "identity_sha256": identity["identity_sha256"]})
        else:
            calibration_info.update({
                "state": "candidate",
                "identity_sha256": identity["identity_sha256"],
                "record_sha256": matching.get("record_sha256"),
                "reuse_verified_on_conversion": True,
            })
    elif source is not None and target is not None:
        calibration_info.update({"state": "blocked_by_errors"})

    # Tier: a hand-edited mapping is never silently promoted to a validated preset.
    declared = profile.get("tier")
    preset = profile.get("preset")
    preset_mapping = profile.get("preset_mapping_sha256")
    canonical_bones = _canonical_preset_bones(source, preset)
    canonical_match = (canonical_bones is not None
                       and dict(mapping.get("bones") or {}) == canonical_bones)
    preset_evidence = profile.get("preset_evidence") or {}
    mapping_bones_sha = _preset_bones_sha256(mapping.get("bones"))
    known_source_formats = {
        "mixamo_names_candidate": "FBX",
        "cmu_exact_31_joint_sample_v1": "BVH",
    }
    evidence_match = (
        preset in VALIDATED_PRESETS
        and preset_evidence.get("source_format")
        == known_source_formats.get(preset)
        and preset_evidence.get("preset") == preset
        and preset_evidence.get("verified_against_source") is True
        and preset_evidence.get("canonical_bones_sha256") is not None
        and preset_evidence.get("canonical_bones_sha256") == preset_mapping
        == mapping_bones_sha
    )
    checks["preset_canonical_match"] = bool(canonical_match)
    checks["preset_evidence"] = bool(evidence_match)
    checks["validated_without_source"] = False
    if mapping_checks["errors"] or axis_checks["errors"]:
        tier = TIER_NEEDS_ADAPTATION
    elif source is not None and canonical_match:
        tier = TIER_VALIDATED_PRESET
    elif source is None and declared == TIER_VALIDATED_PRESET and evidence_match:
        # No live source: retain a previously source-verified known preset only.
        tier = TIER_VALIDATED_PRESET
        checks["validated_without_source"] = True
    else:
        tier = TIER_CONFIGURED
    if source is None and declared == TIER_VALIDATED_PRESET and not evidence_match:
        warnings.append("无source实际canonical验证；不提升validated，降级configured")
    if source is not None and preset in VALIDATED_PRESETS and not canonical_match:
        warnings.append("preset名称不能提升validated；实际source canonical preset不匹配")
    if declared == TIER_VALIDATED_PRESET and tier != TIER_VALIDATED_PRESET:
        warnings.append("profile声明为已验预设，但映射/轴证据不再匹配；已降级")
    checks["tier_matches_content"] = (declared == tier)
    if declared != tier:
        warnings.append("profile分级将由%s重算为%s" % (declared, tier))

    applies = (not errors) and tier != TIER_NEEDS_ADAPTATION
    return {
        "ok": not errors,
        "applies": applies,
        "tier": tier,
        "declared_tier": declared,
        "preset": preset,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "checks": checks,
        "mapping_checks": mapping_checks,
        "axis_checks": axis_checks,
        "target_checks": target_checks,
        "source_fingerprint": (source_evidence.get("rest_fingerprint")),
        "source_world_fingerprint": (source_evidence.get("world_fingerprint")),
        "mapping_sha256": _mapping_sha256(mapping),
        "profile_sha256": profile.get("profile_sha256"),
        "identity_sha256": identity["identity_sha256"] if identity else None,
        "calibration": calibration_info,
        "profile_summary": _profile_summary(profile),
    }


def write_profile(profile, path):
    """Write a pure-JSON profile atomically; returns file/profile hashes."""
    path = Path(bpy.path.abspath(os.fspath(path))).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = dict(profile)
    document["profile_sha256"] = _profile_hash(document)
    _json_only(document)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)
    return {
        "file": str(path),
        "sha256": _file_sha256(path),
        "profile_sha256": document["profile_sha256"],
        "bytes": path.stat().st_size,
    }


def load_profile(path, *, verify_hash=True):
    """Load pure JSON; no Python preset or expression is evaluated."""
    path = Path(bpy.path.abspath(os.fspath(path))).resolve(strict=True)
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    _json_only(document)
    require(document.get("schema") == PROFILE_SCHEMA, "profile schema不受支持")
    require(document.get("profile_version") == PROFILE_VERSION, "profile版本不受支持")
    if verify_hash:
        require(document.get("profile_sha256") == _profile_hash(document),
                "profile内容哈希不匹配")
    return document


def profile_from_json(text, *, verify_hash=True):
    document = json.loads(text)
    _json_only(document)
    require(document.get("schema") == PROFILE_SCHEMA, "profile schema不受支持")
    require(document.get("profile_version") == PROFILE_VERSION, "profile版本不受支持")
    if verify_hash:
        require(document.get("profile_sha256") == _profile_hash(document),
                "profile内容哈希不匹配")
    return document


def profile_to_json(profile, *, indent=2):
    document = dict(profile)
    document["profile_sha256"] = _profile_hash(document)
    _json_only(document)
    return json.dumps(document, ensure_ascii=False, indent=indent, sort_keys=True)


def _find_calibration(profile, identity):
    for record in profile.get("calibrations") or []:
        if not isinstance(record, dict):
            continue
        if record.get("schema") != CALIBRATION_SCHEMA:
            continue
        if record.get("record_sha256") != _record_hash(record):
            continue
        if record.get("identity_sha256") == identity["identity_sha256"]:
            return record
    return None


def seed_profile_calibration(profile, source, target, *, target_world_matrix=None):
    """Seed the shared skeleton cache only from a content-matching record."""
    identity = build_identity(
        profile, source, target, target_world_matrix=target_world_matrix)
    record = _find_calibration(profile, identity)
    if record is None:
        return {"seeded": False, "state": "missing",
                "identity_sha256": identity["identity_sha256"]}
    skeleton = record.get("skeleton")
    require(isinstance(skeleton, dict), "校准记录缺少skeleton")
    require(skeleton.get("adapter_kind") == "profile", "校准记录不属于profile适配器")
    cal._SKELETON_CACHE[identity["identity_sha256"]] = dict(skeleton)
    return {
        "seeded": True,
        "state": "seeded",
        "identity_sha256": identity["identity_sha256"],
        "record_sha256": record.get("record_sha256"),
    }


def calibration_status(profile, source=None, target=None):
    if source is None or target is None:
        return {"state": "not_checked", "reason": "需要显式source和target"}
    try:
        validation = validate_profile(profile, source, target)
    except Exception as exc:
        return {"state": "invalid", "reason": str(exc)}
    if not validation["applies"]:
        return {"state": "invalid", "reason": "; ".join(validation["errors"]),
                "validation": validation["calibration"]}
    identity = build_identity(profile, source, target)
    record = _find_calibration(profile, identity)
    if record is None:
        return {"state": "missing", "identity_sha256": identity["identity_sha256"],
                "validation": validation["calibration"]}
    skeleton = record.get("skeleton") or {}
    return {
        "state": "reusable",
        "identity_sha256": identity["identity_sha256"],
        "record_sha256": record.get("record_sha256"),
        "adapter_armature_scale": skeleton.get("adapter_armature_scale"),
        "motion_scale_basis": skeleton.get("motion_scale_basis"),
        "translation_origin": skeleton.get("translation_origin"),
        "clip_recorded_at": record.get("clip"),
        "reuse_verified_on_conversion": True,
    }


def remember_calibration(profile, record):
    """Return a new profile JSON object with a content-matching calibration."""
    require(record.get("schema") == CALIBRATION_SCHEMA, "校准记录schema不受支持")
    require(record.get("record_sha256") == _record_hash(record), "校准记录哈希不匹配")
    document = json.loads(json.dumps(profile, ensure_ascii=False))
    records = [item for item in document.get("calibrations") or []
               if item.get("identity_sha256") != record.get("identity_sha256")]
    records.append(record)
    document["calibrations"] = records
    document["profile_sha256"] = _profile_hash(document)
    _json_only(document)
    return document


def _profile_calibration_record(profile, identity, skeleton, clip_record,
                                source, target, target_world_matrix=None,
                                target_conversion_world=None):
    source_evidence = profile.get("source") or {}
    if target_world_matrix is None:
        target_world_matrix = _target_world_matrix(target)
    if target_conversion_world is None:
        target_conversion_world = target.matrix_world.copy()
    record = {
        "schema": CALIBRATION_SCHEMA,
        "calibration_version": 1,
        "profile_id": profile.get("id"),
        # This is the profile BEFORE the record is appended.  Appending a
        # record necessarily changes the top-level profile_sha256, so naming
        # this field "profile_sha256" would falsely imply post-append equality.
        "captured_from_profile_sha256": profile.get("profile_sha256"),
        "captured_before_calibration_append": True,
        "identity_sha256": identity["identity_sha256"],
        "mapping_sha256": _mapping_sha256(profile["mapping"]),
        "source_rest_signature": cal.bone_rest_signature(source),
        "source_world_fingerprint": _world_fingerprint(source),
        "source_axis_sha256": _hash(
            _axis_identity_payload(source_evidence.get("axis") or {})),
        "target_rest_signature": cal.bone_rest_signature(target),
        "target_world_fingerprint": _world_fingerprint(
            target, world_matrix=target_world_matrix),
        "target_conversion_world": cal.matrix_list(target_conversion_world),
        "target_uniform_positive": _uniform_positive(
            target, matrix=target_world_matrix)["uniform_positive"],
        "orientation_reference": profile["calibration"]["orientation_reference"],
        "translation_origin": profile["calibration"]["translation_origin"],
        "motion_scale_basis": profile["calibration"]["motion_scale_basis"],
        "include_fingers": bool(profile["calibration"]["include_fingers"]),
        "unit_division": {"source": 0, "target": 1},
        "skeleton": dict(skeleton),
        "clip": dict(clip_record),
        "reuse_note": "clip/action selection is informational; origin is recaptured every conversion",
    }
    record["record_sha256"] = _record_hash(record)
    return record


def load_calibration_from_action(action):
    require(isinstance(action, bpy.types.Action), "需要Action")
    require(CALIBRATION_KEY in action, "候选Action没有profile校准记录")
    value = json.loads(action[CALIBRATION_KEY])
    _json_only(value)
    require(value.get("schema") == CALIBRATION_SCHEMA, "profile校准schema不受支持")
    require(value.get("record_sha256") == _record_hash(value),
            "profile校准记录哈希不匹配")
    return value


def remember_from_action(profile, action):
    return remember_calibration(profile, load_calibration_from_action(action))


def _profile_clip_from_current(source, profile, source_fps=None):
    """Use only the current binding, with an explicit fps when no TIME_KEY."""
    try:
        return source_api.active_clip(source, source_fps)
    except ValueError as exc:
        raise ValueError(
            "当前源Action/Slot没有可信时基；必须显式source_fps，"
            "不会回退profile旧时基：" + str(exc)) from exc


def _validate_explicit_clip(source, clip, source_fps=None):
    """A caller-supplied clip must be the exact current Action/Slot binding."""
    require(isinstance(clip, dict), "显式clip必须是source_api.active_clip结果")
    action = clip.get("action")
    slot = clip.get("slot")
    ad = source.animation_data
    require(ad is not None and ad.action is not None and ad.action_slot is not None,
            "显式clip需要当前源Action/Slot绑定")
    require(action == ad.action and slot == ad.action_slot,
            "显式clip的Action/Slot与当前源绑定不一致；拒绝自动切换绑定")
    require(isinstance(clip.get("fps"), (int, float))
            and math.isfinite(float(clip["fps"])) and float(clip["fps"]) > 0,
            "显式clip的fps无效")
    if source_fps is not None:
        require(abs(float(source_fps) - float(clip["fps"])) <= 1e-9,
                "显式source_fps与显式clip fps不一致")
    require(isinstance(clip.get("range"), (tuple, list))
            and len(clip["range"]) == 2
            and float(clip["range"][0]) < float(clip["range"][1]),
            "显式clip range无效")
    return clip


def _require_factory_isolation(source, target):
    ad = source.animation_data
    require(ad is not None and ad.action is not None and ad.action_slot is not None,
            "适配器工厂调用前必须明确源Action/Slot")
    require(not ad.use_tweak_mode, "请退出源NLA Tweak模式")
    require(ad.use_nla is False, "共享转换核尚未隔离源NLA")
    require(ad.action_blend_type == "REPLACE" and abs(ad.action_influence - 1.0) <= 1e-9,
            "共享转换核尚未隔离源blend/influence")
    require(target.animation_data is not None and target.animation_data.action is None,
            "共享转换核尚未解绑目标Action")


def _cached_profile_skeleton(source, target, mapping, identity, clip_record,
                            *, include_fingers, motion):
    cached = cal.lookup_skeleton_calibration(identity)
    if cached is None or cached.get("adapter_kind") != "profile":
        return None
    if bool(cached.get("include_fingers")) != bool(include_fingers):
        return None
    try:
        source_hip = (Vector(clip_record["source_hip_world"])
                      if cached.get("translation_origin") == TRANSLATION_ORIGIN_CLIP_START
                      else None)
        adapter = c94.Adapter(
            source, target, mapping["bones"], motion=motion, scale_motion=False,
            include_fingers=include_fingers,
            world_motion_scale=cached.get("world_motion_scale"),
            source_hip=source_hip, orientation_reference=ORIENTATION_REFERENCE)
        if abs(adapter.scale - cached["adapter_armature_scale"]) > 1e-9:
            raise ValueError("骨架校准缓存scale不一致")
        if cal._adapter_neutral_hash(adapter) != cached["target_neutral_hash"]:
            raise ValueError("骨架校准缓存neutral不一致")
        if cal._adapter_calibration_hash(adapter) != cached["calibration_hash"]:
            raise ValueError("骨架校准缓存calibration不一致")
        adapter.calibration = {
            name: cal._matrix_from_list(value)
            for name, value in cached["calibration_matrices"].items()}
        adapter.scale = cached["adapter_armature_scale"]
        skeleton = dict(cached)
        skeleton["cache_hit"] = True
        skeleton["calibration_reused"] = True
        skeleton["source_rest_world"] = {
            semantic: cal.matrix_list(matrix)
            for semantic, matrix in sorted(adapter.rest.items())}
        return adapter, skeleton
    except Exception:
        cal._SKELETON_CACHE.pop(identity["identity_sha256"], None)
        return None


def _build_profile_skeleton(source, target, mapping, identity, clip_record,
                            *, include_fingers, motion, scale_basis,
                            translation_origin):
    source_hip = (Vector(clip_record["source_hip_world"])
                  if translation_origin == TRANSLATION_ORIGIN_CLIP_START else None)
    # Construct the final Adapter once.  The old calibration helper uses a
    # separate probe Adapter to measure target legs; that extra evaluation can
    # perturb driver/constraint state in a production rig.  Reading the target
    # legs from this same Adapter's neutral pose keeps the write domain and
    # update sequence identical to the current preset path.
    adapter = c94.Adapter(
        source, target, mapping["bones"], motion=motion, scale_motion=False,
        include_fingers=include_fingers, world_motion_scale=None,
        source_hip=source_hip, orientation_reference=ORIENTATION_REFERENCE)
    world = target.matrix_world
    target_unit = float(world.to_3x3().col[0].length)
    target_legs = {}
    target_leg_points = {}
    for side in SIDES:
        hip = world @ adapter.neutral["大腿FK." + side].translation
        knee = world @ adapter.neutral["膝盖FK." + side].translation
        ankle = world @ adapter.neutral["脚掌FK." + side].translation
        target_leg_points[side] = {
            "hip": list(hip), "knee": list(knee), "ankle": list(ankle)}
        target_legs[side] = cal._leg_length([hip, knee, ankle])
        require(target_legs[side] > 1e-8, "目标neutral腿长退化：" + side)
    source_legs = cal._source_rest_legs(source, mapping)
    scale_info = cal.motion_scale_for_legs(source_legs, target_legs, target_unit)
    if scale_basis == MOTION_SCALE_LEG_LENGTH:
        world_motion_scale = float(scale_info["world_leg_ratio_average"])
    elif scale_basis == MOTION_SCALE_REST_HIP_HEIGHT:
        target_hip_z = float((world @ adapter.neutral["盆骨控制器"].translation).z)
        target_world_z = float(target.matrix_world.translation.z)
        source_rest_hip_z = float(adapter.rest["hips"].translation.z)
        source_world_z = float(source.matrix_world.translation.z)
        denominator = source_rest_hip_z - source_world_z
        require(abs(denominator) > 1e-12, "源REST髋高零点退化，无法重现预设比例")
        world_motion_scale = (target_hip_z - target_world_z) / denominator
    elif scale_basis == MOTION_SCALE_NONE:
        world_motion_scale = None
    else:
        raise ValueError("未知运动比例模式：" + str(scale_basis))
    adapter.scale = (1.0 / target_unit if world_motion_scale is None
                     else float(world_motion_scale) / target_unit)
    rest_hash = cal._adapter_rest_hash(adapter)
    expected_rest_hash = _hash({
        semantic: cal.matrix_list(matrix)
        for semantic, matrix in sorted(cal.source_rest_world(source, mapping).items())})
    require(rest_hash == expected_rest_hash, "适配器REST参考与源REST矩阵不一致")
    neutral_hash = cal._adapter_neutral_hash(adapter)
    calibration_hash = cal._adapter_calibration_hash(adapter)
    if world_motion_scale is not None:
        expected_scale = float(world_motion_scale) / target_unit
        require(abs(adapter.scale - expected_scale) <= 1e-9,
                "适配器armature比例与单次unit除法约定不一致")
    else:
        require(abs(adapter.scale - 1.0 / target_unit) <= 1e-9,
                "适配器无比例模式与单次unit除法约定不一致")
    skeleton = {
        "identity_sha256": identity["identity_sha256"],
        "adapter_kind": "profile",
        "profile_schema": PROFILE_SCHEMA,
        "calibration_schema": CALIBRATION_SCHEMA,
        "orientation_reference": ORIENTATION_REFERENCE,
        "translation_origin": translation_origin,
        "motion_scale_basis": scale_basis,
        "include_fingers": bool(include_fingers),
        "source_rest_signature": cal.bone_rest_signature(source),
        "target_rest_signature": cal.bone_rest_signature(target),
        "source_rest_world": {
            semantic: cal.matrix_list(matrix)
            for semantic, matrix in sorted(adapter.rest.items())},
        "source_rest_world_sha256": rest_hash,
        "target_neutral_controls_hash": neutral_hash,
        "target_neutral_hash": neutral_hash,
        "target_neutral_controls": {
            name: cal.matrix_list(matrix) for name, matrix in sorted(adapter.neutral.items())},
        "target_neutral_leg_points_world": target_leg_points,
        "target_neutral_hip_world": list(world @ adapter.neutral["盆骨控制器"].translation),
        "target_neutral_root_world": list(world @ adapter.neutral["根骨骼"].translation),
        "source_leg_lengths_world": {
            side: float(value) for side, value in source_legs.items()},
        "target_leg_lengths_world": {
            side: float(value) for side, value in target_legs.items()},
        "leg_ratio": {side: float(value) for side, value in scale_info["ratios"].items()},
        "average_leg_ratio": float(scale_info["average_ratio"]),
        "world_leg_ratio_average": float(scale_info["world_leg_ratio_average"]),
        "world_motion_scale": (None if world_motion_scale is None
                               else float(world_motion_scale)),
        "motion_scale": float(adapter.scale),
        "motion_scale_formula": scale_info["formula"],
        "target_object_unit": target_unit,
        "adapter_armature_scale": float(adapter.scale),
        "calibration_hash": calibration_hash,
        "calibration_matrices": {
            name: cal.matrix_list(matrix) for name, matrix in sorted(adapter.calibration.items())},
        "palm_calibration_sides": list(adapter.palm_calibration_sides),
        "finger_chains": list(adapter.finger_chains),
        "control_names": list(adapter.names),
        "observed": dict(adapter.observed),
        "unit_division_count": 1,
        "source_unit_division_count": 0,
        "rest_hip_z_division": False,
        "cache_hit": False,
        "calibration_reused": False,
    }
    cal._SKELETON_CACHE[identity["identity_sha256"]] = skeleton
    return adapter, skeleton


def prepare_profile_adapter(source, target, mapping, *, profile, clip=None,
                            origin_frame=None, motion="PRESERVE",
                            source_fps=None, selected_start=None,
                            selected_end=None, target_world_matrix=None,
                            target_conversion_world=None):
    """Adapter factory used inside ``retarget_action.convert_resolved``."""
    source_api.armature(source)
    source_api.armature(target)
    require(source != target, "源和目标不能是同一骨架")
    _require_factory_isolation(source, target)
    if clip is None:
        clip = _profile_clip_from_current(source, profile, source_fps)
    calibration = profile["calibration"]
    translation_origin = calibration["translation_origin"]
    if translation_origin not in (TRANSLATION_ORIGIN_CLIP_START,
                                  TRANSLATION_ORIGIN_REST_HIP):
        raise ValueError("profile位移原点模式无效")
    # CLIP_START must be sampled before the adapter is built because it is the
    # source displacement origin.  REST_HIP preset fidelity must preserve the
    # original Mixamo construction order: adapter first, clip metadata after.
    if translation_origin == TRANSLATION_ORIGIN_CLIP_START:
        captured = capture_clip(
            source, mapping, clip=clip, origin_frame=origin_frame,
            translation_origin=translation_origin,
            selected_start=selected_start, selected_end=selected_end)
        clip_record = captured["record"]
    else:
        captured = None
        clip_record = {}
    if target_world_matrix is None:
        target_world_matrix = _target_world_matrix(target)
    identity = build_identity(
        profile, source, target, target_world_matrix=target_world_matrix)
    seed = seed_profile_calibration(
        profile, source, target, target_world_matrix=target_world_matrix)
    include_fingers = bool(calibration["include_fingers"])
    prepared = _cached_profile_skeleton(
        source, target, mapping, identity, clip_record,
        include_fingers=include_fingers, motion=motion)
    if prepared is None:
        adapter, skeleton = _build_profile_skeleton(
            source, target, mapping, identity, clip_record,
            include_fingers=include_fingers, motion=motion,
            scale_basis=calibration["motion_scale_basis"],
            translation_origin=translation_origin)
    else:
        adapter, skeleton = prepared
    if captured is None:
        if translation_origin == TRANSLATION_ORIGIN_REST_HIP:
            captured = _rest_hip_clip_record(
                source, mapping, clip, origin_frame=origin_frame,
                selected_start=selected_start, selected_end=selected_end)
        else:
            captured = capture_clip(
                source, mapping, clip=clip, origin_frame=origin_frame,
                translation_origin=translation_origin,
                selected_start=selected_start, selected_end=selected_end)
        clip_record = captured["record"]
    skeleton = dict(skeleton)
    skeleton["cache_hit"] = bool(prepared is not None)
    skeleton["calibration_reused"] = bool(prepared is not None)
    record = _profile_calibration_record(
        profile, identity, skeleton, clip_record, source, target,
        target_world_matrix=target_world_matrix,
        target_conversion_world=target_conversion_world)
    return {
        "adapter": adapter,
        "record": record,
        "identity": identity,
        "clip_record": clip_record,
        "captured": captured,
        "seed": seed,
    }


def convert(source, target, profile, *, clip=None, source_fps=None, start=None,
            end=None, output_start=1, motion="PRESERVE", sample_step=1,
            action_name=None, progress=None, cancelled=None, root_rotation="HEADING"):
    """Convert through the shared ``convert_resolved`` Action/preview core."""
    source_api.armature(source)
    source_api.armature(target)
    require(root_rotation in ("HEADING", "FULL"), "根骨旋转模式无效")
    validation = validate_profile(profile, source, target)
    require(validation["applies"],
            "profile不可应用：" + "; ".join(validation["errors"]))
    mapping = mapped_mapping(profile)
    if clip is not None:
        clip = _validate_explicit_clip(source, clip, source_fps=source_fps)
    else:
        clip = _profile_clip_from_current(source, profile, source_fps)
    calibration = profile["calibration"]
    translation_origin = calibration["translation_origin"]
    origin_frame = (float(start) if start is not None
                    else float(clip["range"][0]))
    selected_start = (float(start) if start is not None
                      else float(clip["range"][0]))
    selected_end = (float(end) if end is not None
                    else float(clip["range"][1]))
    holder = {}
    target_world_before = target.matrix_world.copy()
    target_world_matrix = _target_world_matrix(target)
    identity_for_selection = build_identity(
        profile, source, target, target_world_matrix=target_world_matrix)
    matching_record = _find_calibration(profile, identity_for_selection)
    target_conversion_world = target_world_before.copy()
    if matching_record and matching_record.get("target_conversion_world"):
        target_conversion_world = cal._matrix_from_list(
            matching_record["target_conversion_world"])

    def factory(current_source, current_target, current_mapping):
        prepared = prepare_profile_adapter(
            current_source, current_target, current_mapping,
            profile=profile, clip=clip, origin_frame=origin_frame,
            motion=motion, source_fps=source_fps,
            selected_start=selected_start, selected_end=selected_end,
            target_world_matrix=target_world_matrix,
            target_conversion_world=target_conversion_world)
        prepared["adapter"].root_rotation = root_rotation
        holder["prepared"] = prepared
        return prepared["adapter"], prepared["record"]

    # Evaluate/build against the canonical (frame-1) target object transform,
    # then restore the production object transform even on failure.
    target.matrix_world = target_conversion_world
    bpy.context.view_layer.update()
    try:
        result = action_api.convert_resolved(
            source, target, mapped=mapping, clip=clip, start=start, end=end,
            output_start=output_start, motion=motion, scale_motion=False,
            include_fingers=bool(calibration["include_fingers"]),
            sample_step=sample_step, action_name=action_name, progress=progress,
            cancelled=cancelled, adapter_factory=factory)
    finally:
        target.matrix_world = target_world_before
        bpy.context.view_layer.update()
    require("prepared" in holder, "profile适配器工厂未被共享转换核调用")
    prepared = holder["prepared"]
    record = prepared["record"]
    action = result["action"]
    action[CALIBRATION_KEY] = json.dumps(record, ensure_ascii=False)
    report = result["report"]
    report["profile"] = _profile_summary(profile)
    report["source_action_binding"] = (
        (profile.get("source") or {}).get("action_binding"))
    report["nla_policy"] = (profile.get("source") or {}).get("nla_policy") or {
        "mode": "ACTIVE_ACTION_ONLY",
        "nla_composition_implemented": False,
    }
    report["profile_validation"] = {
        "tier": validation["tier"],
        "applies": validation["applies"],
        "warnings": validation["warnings"],
        "calibration": validation["calibration"],
    }
    report["calibration_schema"] = CALIBRATION_SCHEMA
    report["orientation_reference"] = calibration["orientation_reference"]
    report["root_rotation"] = root_rotation
    report["translation_origin"] = translation_origin
    report["motion_scale_basis"] = calibration["motion_scale_basis"]
    report["profile_identity_sha256"] = prepared["identity"]["identity_sha256"]
    report["profile_calibration_record_sha256"] = record["record_sha256"]
    report["profile_cache_seed"] = prepared["seed"]
    report["profile_cache_hit"] = bool(
        prepared["record"]["skeleton"].get("cache_hit"))
    report["profile_calibration_reused"] = bool(
        prepared["record"]["skeleton"].get("calibration_reused"))
    report["source_clip_start_hip_world"] = prepared["clip_record"].get(
        "clip_start_hip_world")
    report["source_hip_origin_world"] = prepared["clip_record"].get("source_hip_world")
    report["selected_clip"] = prepared["clip_record"]
    report["clip_origin_frame"] = prepared["clip_record"].get("origin_frame")
    report["selected_range"] = prepared["clip_record"].get("selected_range")
    report["preservation"].update({
        "automatic_contact_correction": False,
        "additional_trajectory_correction": False,
        "profile_python_executed": False,
    })
    action[action_api.META] = json.dumps(report, ensure_ascii=False)
    result["report"] = report
    result["profile"] = profile
    result["profile_validation"] = validation
    result["calibration_record"] = record
    result["adapter_record"] = record
    result["profile_with_calibration"] = remember_calibration(profile, record)
    return result


def find_matching_source(profile, candidates=None):
    """Find sources by content fingerprints, never by mutable object name."""
    candidates = list(candidates or bpy.context.scene.objects)
    expected_rest = (profile.get("source") or {}).get("rest_fingerprint")
    expected_world = (profile.get("source") or {}).get("world_fingerprint")
    matches = []
    for obj in candidates:
        if obj.type != "ARMATURE":
            continue
        if cal.bone_rest_signature(obj) != expected_rest:
            continue
        if _world_fingerprint(obj) != expected_world:
            continue
        matches.append(obj)
    return matches


def find_matching_targets(profile, candidates=None):
    """Return C94 targets whose rest/world match a persisted calibration."""
    candidates = list(candidates or bpy.context.scene.objects)
    matches = []
    for obj in candidates:
        if obj.type != "ARMATURE":
            continue
        try:
            pose._metadata(obj)
        except Exception:
            continue
        rest = cal.bone_rest_signature(obj)
        world = _world_fingerprint(obj, world_matrix=_target_world_matrix(obj))
        for record in profile.get("calibrations") or []:
            if record.get("target_rest_signature") == rest \
                    and record.get("target_world_fingerprint") == world:
                matches.append(obj)
                break
    return matches


def action_signature(action):
    return cal.action_signature(action)


def report_summary(report):
    """Small projection for operator/report use."""
    return {
        "profile_id": (report.get("profile") or {}).get("id"),
        "profile_tier": (report.get("profile") or {}).get("tier"),
        "profile_sha256": (report.get("profile") or {}).get("profile_sha256"),
        "identity_sha256": report.get("profile_identity_sha256"),
        "calibration_record_sha256": report.get("profile_calibration_record_sha256"),
        "cache_hit": report.get("profile_cache_hit"),
        "calibration_reused": report.get("profile_calibration_reused"),
        "translation_origin": report.get("translation_origin"),
        "motion_scale_basis": report.get("motion_scale_basis"),
        "source_action": report.get("source_action"),
        "source_slot": report.get("source_slot"),
        "source_fps": report.get("source_fps"),
        "clip_origin_frame": report.get(
            "clip_origin_frame",
            (report.get("profile") or {}).get("clip", {}).get("origin_frame")),
        "selected_range": report.get("selected_range"),
    }
