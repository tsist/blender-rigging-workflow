# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit source-reference calibration for the shared retarget Action core.

The three axes are deliberately separate:

* ``orientation_reference=REST``: source REST matrices define bone directions.
  A non-neutral Action frame is never relabelled as the source rest pose.
* ``translation_origin=CLIP_START``: the source hip at the selected clip start
  is sampled for each clip; only translation deltas use it.
* ``motion_scale=LEG_LENGTH``: mean(target/source thigh-shin-foot length ratio)
  divided by the target object unit.  No REST hip-Z division is used.

``retarget_action.convert_resolved`` remains the owner of snapshots, preview
tokens, cancellation and target restoration.  This module supplies an exact
source mapping, a clip record and an adapter factory; it does not publish a
second Action or preview implementation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from . import pose_switching as pose
from . import retarget_action as action_api
from . import retarget_c94 as c94
from . import retarget_source as source_api


CALIBRATION_SCHEMA = "c94.retarget.calibration.v1"
ACTION_REPORT_KEY = action_api.META
ACTION_CALIBRATION_KEY = "c94_retarget_calibration_v1"
ORIENTATION_REFERENCE = "REST"
TRANSLATION_ORIGIN = "CLIP_START"
MOTION_SCALE_BASIS = "LEG_LENGTH"

_SKELETON_CACHE: dict[str, dict] = {}


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def matrix_list(matrix):
    return [[float(matrix[row][column]) for column in range(4)] for row in range(4)]


def _matrix_from_list(value):
    require(isinstance(value, list) and len(value) == 4
            and all(isinstance(row, list) and len(row) == 4 for row in value),
            "校准矩阵记录无效")
    return Matrix(value)


def _angle_degrees(first, second):
    rotation = first.to_quaternion().inverted() @ second.to_quaternion()
    angle = math.degrees(abs(rotation.angle))
    return min(angle, 360.0 - angle)


def _update(rig):
    return pose._update(rig)


def scene_fingerprint(scene):
    camera = scene.camera.name if scene.camera else None
    return {
        "fps": int(scene.render.fps),
        "fps_base": float(scene.render.fps_base),
        "range": [int(scene.frame_start), int(scene.frame_end)],
        "frame": int(scene.frame_current),
        "subframe": float(scene.frame_subframe),
        "camera": camera,
        "unit": [scene.unit_settings.system, float(scene.unit_settings.scale_length),
                 scene.unit_settings.length_unit],
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name
        if bpy.context.view_layer.objects.active else None,
    }


def _same_scene(before, after):
    return (before["fps"] == after["fps"]
            and abs(before["fps_base"] - after["fps_base"]) < 1e-12
            and before["range"] == after["range"]
            and before["frame"] == after["frame"]
            and abs(before["subframe"] - after["subframe"]) < 1e-12
            and before["camera"] == after["camera"]
            and before["unit"] == after["unit"]
            and before["selected"] == after["selected"]
            and before["active"] == after["active"])


def load_bvh_module(path, module_name="accepted_bvh_source"):
    """Load the accepted importer by path without modifying its source."""
    source = Path(path).resolve(strict=True)
    existing = sys.modules.get(module_name)
    if existing is not None and Path(existing.__file__).resolve() == source:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, source)
    require(spec is not None and spec.loader is not None, "无法加载BVH导入模块")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves decorated classes through sys.modules while running.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def import_bvh(filepath, *, importer_path, scale=0.01, forward="Z", up="Y", name=None):
    """Import a BVH source with explicit axes/scale and verify Scene restore."""
    scene = bpy.context.scene
    before = scene_fingerprint(scene)
    module = load_bvh_module(importer_path)
    result = module.import_bvh(filepath, scale=scale, forward=forward, up=up, name=name)
    after = scene_fingerprint(scene)
    require(_same_scene(before, after), "BVH导入未保留Scene设置")
    metadata = result.metadata
    require(metadata.get("forward") == str(forward).strip().upper()
            and metadata.get("up") == str(up).strip().upper()
            and abs(float(metadata.get("scale", 0.0)) - float(scale)) <= 1e-12,
            "BVH导入元数据与显式轴/比例不符")
    require(isinstance(result.fps, (int, float)) and math.isfinite(result.fps)
            and result.fps > 0, "BVH源fps无效")
    timebase = {"fps": float(result.fps), "slots": [result.slot.identifier]}
    result.action[source_api.TIME_KEY] = json.dumps(timebase, ensure_ascii=False)
    record = {
        "importer": "accepted_bvh_source.import_bvh",
        "file": str(Path(filepath).resolve(strict=True)),
        "sha256": metadata.get("sha256"),
        "bytes": metadata.get("bytes"),
        "frame_count": metadata.get("frame_count"),
        "joint_count": metadata.get("joint_count"),
        "channel_count": metadata.get("total_channels"),
        "fps": float(result.fps),
        "scale": float(scale),
        "scale_semantics": "Blender units per source unit",
        "forward": str(forward).strip().upper(),
        "up": str(up).strip().upper(),
        "target_forward": metadata.get("target_forward"),
        "target_up": metadata.get("target_up"),
        "axis_mapping_function": metadata.get("axis_mapping_function"),
        "object_rotation_quaternion": list(metadata.get("object_rotation_quaternion", [])),
        "object_rotation_matrix": metadata.get("object_rotation_matrix"),
        "object_rotation_determinant": metadata.get("object_rotation_determinant"),
        "action": result.action.name,
        "action_slot": result.slot.identifier,
        "timebase": timebase,
        "armature_object": result.rig.name,
        "scene_preserved": True,
        "scene_before": before,
        "scene_after": after,
    }
    return {"rig": result.rig, "action": result.action, "slot": result.slot,
            "fps": float(result.fps), "metadata": metadata, "record": record}


def object_identity(rig):
    require(rig is not None and isinstance(rig, bpy.types.Object), "缺少骨架对象")
    return {
        "object": rig.name,
        "data": rig.data.name if rig.data else None,
        "matrix_world": matrix_list(rig.matrix_world),
        "determinant": float(rig.matrix_world.determinant()),
        "bone_count": len(rig.data.bones),
        "rest_signature": bone_rest_signature(rig),
        "pose_position": rig.data.pose_position,
        "library": rig.library.filepath if rig.library else None,
        "data_library": rig.data.library.filepath if rig.data and rig.data.library else None,
    }


def bone_rest_signature(rig):
    rows = []
    for bone in sorted(rig.data.bones, key=lambda item: item.name):
        rows.append({
            "name": bone.name,
            "matrix_local": matrix_list(bone.matrix_local),
            "head_local": list(bone.head_local),
            "tail_local": list(bone.tail_local),
            "parent": bone.parent.name if bone.parent else None,
            "connected": bone.use_connect,
            "deform": bone.use_deform,
            "inherit_scale": bone.inherit_scale,
            "inherit_rotation": bone.use_inherit_rotation,
            "local_location": bone.use_local_location,
            "bbone_segments": bone.bbone_segments,
        })
    return canonical_hash(rows)


def action_signature(action):
    if action is None:
        return None
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag is None:
                    continue
                for curve in bag.fcurves:
                    rows.append({
                        "slot": slot.identifier,
                        "path": curve.data_path,
                        "index": curve.array_index,
                        "keys": [[float(key.co[0]), float(key.co[1]), key.interpolation]
                                 for key in curve.keyframe_points],
                    })
    return canonical_hash(rows)


def action_stats(action, slot=None):
    require(action is not None, "缺少Action")
    fcurves = keys = 0
    times = set()
    paths = set()
    for layer in action.layers:
        for strip in layer.strips:
            for current in ([slot] if slot is not None else list(action.slots)):
                bag = strip.channelbag(current)
                if bag is None:
                    continue
                for curve in bag.fcurves:
                    fcurves += 1
                    keys += len(curve.keyframe_points)
                    paths.add((curve.data_path, curve.array_index))
                    times.update(float(point.co[0]) for point in curve.keyframe_points)
    return {
        "fcurve_count": fcurves,
        "key_count": keys,
        "unique_key_times": len(times),
        "key_time_min": min(times) if times else None,
        "key_time_max": max(times) if times else None,
        "paths": [{"path": path, "index": index} for path, index in sorted(paths)],
        "slots": [slot.identifier for slot in action.slots],
    }


def constraint_signature(rig):
    rows = []
    for pose_bone in sorted(rig.pose.bones, key=lambda item: item.name):
        for constraint in pose_bone.constraints:
            fields = {}
            for name in ("type", "mute", "influence", "target", "subtarget",
                         "pole_target", "pole_subtarget", "chain_count",
                         "owner_space", "target_space", "mix_mode", "use_offset"):
                if not hasattr(constraint, name):
                    continue
                value = getattr(constraint, name)
                if isinstance(value, bpy.types.ID):
                    value = value.name
                if value is None or isinstance(value, (str, bool, int)) or type(value) is float:
                    fields[name] = value
            rows.append({"bone": pose_bone.name, "name": constraint.name, **fields})
    return canonical_hash(rows)


def source_rest_world(source, mapping):
    return {
        semantic: source.matrix_world @ source.data.bones[name].matrix_local
        for semantic, name in mapping["bones"].items()
    }


def _leg_length(points):
    hip, knee, ankle = points
    return (knee - hip).length + (ankle - knee).length


def _source_rest_legs(source, mapping):
    rest = source_rest_world(source, mapping)
    result = {}
    for side in ("L", "R"):
        points = [rest["thigh." + side].translation, rest["shin." + side].translation,
                  rest["foot." + side].translation]
        result[side] = _leg_length(points)
        require(result[side] > 1e-8, "源REST腿长退化：" + side)
    return result


def _source_pose_legs(pose_matrices):
    result = {}
    for side in ("L", "R"):
        points = [pose_matrices["thigh." + side].translation,
                  pose_matrices["shin." + side].translation,
                  pose_matrices["foot." + side].translation]
        result[side] = _leg_length(points)
    return result


def motion_scale_for_legs(source_legs, target_legs, target_object_unit):
    """World leg-length ratio and final armature scale; one unit division."""
    require(target_object_unit > 0 and math.isfinite(target_object_unit),
            "目标对象单位必须为有限正数")
    ratios = {side: float(target_legs[side]) / float(source_legs[side])
              for side in ("L", "R")}
    require(all(math.isfinite(value) and value > 0 for value in ratios.values()),
            "腿长比无效")
    average = (ratios["L"] + ratios["R"]) / 2.0
    return {
        "ratios": ratios,
        "average_ratio": average,
        "world_leg_ratio_average": average,
        "target_object_unit": float(target_object_unit),
        "motion_scale": average / float(target_object_unit),
        "motion_scale_semantics": "final armature-space scale after one target-unit division",
        "formula": "world_ratio = mean(target/source thigh-shin-foot length); armature_scale = world_ratio / target_object_unit",
        "rest_hip_z_division": False,
    }


def _target_neutral(source, target, mapping):
    """Measure neutral through a probe Adapter; never clear the whole rig.

    The probe resets only the shared Adapter write domain and the four FK
    modes.  Non-target controls (face, gaze, etc.) keep their current input,
    matching the public conversion/preview behavior.
    """
    if target.mode == "EDIT":
        raise ValueError("目标骨架必须退出编辑模式")
    probe = c94.Adapter(
        source, target, mapping["bones"], motion="PRESERVE",
        scale_motion=False, include_fingers=False,
        orientation_reference=ORIENTATION_REFERENCE,
    )
    neutral_controls_hash = _adapter_neutral_hash(probe)
    world = target.matrix_world
    legs = {}
    points = {}
    for side in ("L", "R"):
        hip = world @ probe.neutral["大腿FK." + side].translation
        knee = world @ probe.neutral["膝盖FK." + side].translation
        ankle = world @ probe.neutral["脚掌FK." + side].translation
        points[side] = {"hip": list(hip), "knee": list(knee), "ankle": list(ankle)}
        legs[side] = _leg_length([hip, knee, ankle])
        require(legs[side] > 1e-8, "目标neutral腿长退化：" + side)
    unit = world.to_3x3().col[0].length
    return {
        "probe_names": list(probe.names),
        "neutral_controls_hash": neutral_controls_hash,
        "target_legs": legs,
        "target_leg_points_world": points,
        "neutral_hip_world": list(world @ probe.neutral["盆骨控制器"].translation),
        "neutral_root_world": list(world @ probe.neutral["根骨骼"].translation),
        "target_object_unit": float(unit),
    }


def _adapter_neutral_hash(adapter):
    return canonical_hash({name: matrix_list(matrix)
                           for name, matrix in sorted(adapter.neutral.items())})


def _adapter_calibration_hash(adapter):
    return canonical_hash({name: matrix_list(matrix)
                           for name, matrix in sorted(adapter.calibration.items())})


def _adapter_rest_hash(adapter):
    return canonical_hash({name: matrix_list(matrix)
                           for name, matrix in sorted(adapter.rest.items())})


def capture_clip(source, mapping, *, clip=None, source_fps=None, origin_frame=None):
    """Capture per-clip timing/origin; orientation remains source REST."""
    if clip is None:
        clip = source_api.active_clip(source, source_fps)
    require(clip["action"].name == source.animation_data.action.name,
            "源Action在clip捕获前已改变")
    require(clip["slot"].identifier == source.animation_data.action_slot.identifier,
            "源Slot在clip捕获前已改变")
    start, end = float(clip["range"][0]), float(clip["range"][1])
    origin = start if origin_frame is None else float(origin_frame)
    require(math.isfinite(origin) and start <= origin <= end,
            "clip origin必须位于源Action帧域内")
    scene = bpy.context.scene
    scene.frame_set(math.floor(origin), subframe=origin - math.floor(origin))
    evaluated = _update(source)
    world = evaluated.matrix_world
    pose_matrices = {
        semantic: world @ evaluated.pose.bones[name].matrix
        for semantic, name in mapping["bones"].items()
    }
    rest_matrices = source_rest_world(source, mapping)
    differences = {
        semantic: _angle_degrees(rest_matrices[semantic], pose_matrices[semantic])
        for semantic in mapping["bones"]
    }
    max_difference = max(differences.values())
    foot_points = []
    for side in ("L", "R"):
        for semantic in ("foot." + side, "toes." + side):
            pose_bone = evaluated.pose.bones[mapping["bones"][semantic]]
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
        "origin_mode": TRANSLATION_ORIGIN,
        "origin_is_clip_start": abs(origin - start) <= 1e-9,
        "action_signature": action_signature(clip["action"]),
        "source_hip_world": list(hip),
        "source_ground_z_world": float(ground_z),
        "source_hip_height_world": float(hip.z - ground_z),
        "source_clip_start_to_rest_degrees": dict(sorted(differences.items())),
        "max_clip_start_to_rest_degrees": float(max_difference),
        "clip_start_is_source_rest_pose": bool(max_difference <= 0.1),
        "first_frame_is_neutral": bool(max_difference <= 0.1),
        "source_pose_sha256": canonical_hash(
            {semantic: matrix_list(matrix) for semantic, matrix in sorted(pose_matrices.items())}),
        "source_leg_lengths_at_clip_start_world": {
            side: float(value) for side, value in _source_pose_legs(pose_matrices).items()},
    }
    return {"clip": clip, "record": record, "pose_matrices": pose_matrices,
            "rest_matrices": rest_matrices, "source_hip": hip}


def build_identity(source, target, mapping, bvh_import):
    """Skeleton/axis/object/map identity; Action/Slot/fps/origin stay in clip."""
    require(isinstance(mapping, dict) and mapping.get("schema") == "c94.retarget.mapping.v1",
            "映射schema无效")
    require(isinstance(bvh_import, dict) and bvh_import.get("record"), "缺少BVH导入记录")
    import_record = bvh_import["record"]
    axis = {
        "forward": import_record["forward"],
        "up": import_record["up"],
        "scale": import_record["scale"],
        "target_forward": import_record.get("target_forward"),
        "target_up": import_record.get("target_up"),
        "object_rotation_matrix": import_record.get("object_rotation_matrix"),
        "object_rotation_quaternion": import_record.get("object_rotation_quaternion"),
        "object_rotation_determinant": import_record.get("object_rotation_determinant"),
    }
    mapping_payload = {
        "preset": mapping.get("preset"),
        "bones": mapping["bones"],
        "missing_optional": mapping.get("missing_optional", []),
        "unmapped_channels": mapping.get("unmapped_channels", []),
    }
    target_unit = float(target.matrix_world.to_3x3().col[0].length)
    payload = {
        "schema": CALIBRATION_SCHEMA,
        "calibration_version": 1,
        "source_object": object_identity(source),
        "source_axis": axis,
        "target_object": object_identity(target),
        "target_object_unit": target_unit,
        "target_scene_unit": {
            "system": bpy.context.scene.unit_settings.system,
            "scale_length": float(bpy.context.scene.unit_settings.scale_length),
            "length_unit": bpy.context.scene.unit_settings.length_unit,
        },
        "mapping": mapping_payload,
        "mapping_sha256": canonical_hash(mapping_payload),
        "include_fingers": False,
        "orientation_reference": ORIENTATION_REFERENCE,
        "motion_scale_basis": MOTION_SCALE_BASIS,
    }
    return {**payload, "identity_sha256": canonical_hash(payload)}


def lookup_skeleton_calibration(identity):
    return _SKELETON_CACHE.get(identity["identity_sha256"])


def clear_skeleton_cache():
    _SKELETON_CACHE.clear()


def skeleton_cache_size():
    return len(_SKELETON_CACHE)


def _build_skeleton(source, target, mapping, identity, clip_record):
    neutral = _target_neutral(source, target, mapping)
    source_legs = _source_rest_legs(source, mapping)
    scale_info = motion_scale_for_legs(source_legs, neutral["target_legs"],
                                       neutral["target_object_unit"])
    adapter = c94.Adapter(
        source, target, mapping["bones"], motion="PRESERVE",
        scale_motion=False, include_fingers=False,
        world_motion_scale=scale_info["world_leg_ratio_average"],
        source_hip=Vector(clip_record["source_hip_world"]),
        orientation_reference=ORIENTATION_REFERENCE,
    )
    rest_hash = _adapter_rest_hash(adapter)
    expected_rest_hash = canonical_hash(
        {semantic: matrix_list(matrix)
         for semantic, matrix in sorted(source_rest_world(source, mapping).items())})
    require(rest_hash == expected_rest_hash, "适配器REST参考与源REST矩阵不一致")
    neutral_hash = _adapter_neutral_hash(adapter)
    calibration_hash = _adapter_calibration_hash(adapter)
    target_unit = float(target.matrix_world.to_3x3().col[0].length)
    require(abs(adapter.scale - scale_info["motion_scale"]) <= 1e-9,
            "适配器armature比例与单次unit除法约定不一致")
    skeleton = {
        "identity_sha256": identity["identity_sha256"],
        "orientation_reference": ORIENTATION_REFERENCE,
        "motion_scale_basis": MOTION_SCALE_BASIS,
        "source_rest_signature": identity["source_object"]["rest_signature"],
        "target_rest_signature": identity["target_object"]["rest_signature"],
        "source_rest_world": {semantic: matrix_list(matrix)
                              for semantic, matrix in sorted(adapter.rest.items())},
        "source_rest_world_sha256": rest_hash,
        "target_neutral_controls_hash": neutral["neutral_controls_hash"],
        "target_neutral_hash": neutral_hash,
        "target_neutral_controls": {
            name: matrix_list(adapter.neutral[name]) for name in sorted(adapter.names)},
        "target_neutral_leg_points_world": neutral["target_leg_points_world"],
        "target_neutral_hip_world": neutral["neutral_hip_world"],
        "target_neutral_root_world": neutral["neutral_root_world"],
        "source_leg_lengths_world": {side: float(value) for side, value in source_legs.items()},
        "target_leg_lengths_world": {side: float(value) for side, value in neutral["target_legs"].items()},
        "leg_ratio": {side: float(value) for side, value in scale_info["ratios"].items()},
        "average_leg_ratio": float(scale_info["average_ratio"]),
        "world_leg_ratio_average": float(scale_info["world_leg_ratio_average"]),
        "motion_scale": float(scale_info["motion_scale"]),
        "motion_scale_formula": scale_info["formula"],
        "target_object_unit": target_unit,
        "adapter_armature_scale": float(adapter.scale),
        "calibration_hash": calibration_hash,
        "calibration_matrices": {name: matrix_list(matrix)
                                 for name, matrix in sorted(adapter.calibration.items())},
        "rest_hip_z_division": False,
        "cache_hit": False,
        "calibration_reused": False,
    }
    _SKELETON_CACHE[identity["identity_sha256"]] = skeleton
    return adapter, skeleton


def _cached_skeleton(source, target, mapping, identity, clip_record):
    cached = lookup_skeleton_calibration(identity)
    if cached is None:
        return None
    try:
        adapter = c94.Adapter(
            source, target, mapping["bones"], motion="PRESERVE",
            scale_motion=False, include_fingers=False,
            world_motion_scale=cached["world_leg_ratio_average"],
            source_hip=Vector(clip_record["source_hip_world"]),
            orientation_reference=ORIENTATION_REFERENCE,
        )
        if (abs(adapter.scale - cached["adapter_armature_scale"]) > 1e-9
                or _adapter_neutral_hash(adapter) != cached["target_neutral_hash"]
                or _adapter_calibration_hash(adapter) != cached["calibration_hash"]):
            raise ValueError("骨架校准缓存与当前scale/neutral/calibration不一致")
        adapter.calibration = {
            name: _matrix_from_list(value)
            for name, value in cached["calibration_matrices"].items()}
        adapter.scale = cached["adapter_armature_scale"]
        skeleton = dict(cached)
        skeleton["source_rest_world"] = {
            semantic: matrix_list(matrix) for semantic, matrix in sorted(adapter.rest.items())}
        skeleton["cache_hit"] = True
        skeleton["calibration_reused"] = True
        return adapter, skeleton
    except Exception:
        _SKELETON_CACHE.pop(identity["identity_sha256"], None)
        return None


def prepare_adapter(source, target, mapping, *, clip, origin_frame=None, bvh_import):
    """Return a frozen-0.35.1 Adapter plus the split calibration record."""
    require(mapping.get("preset") == "cmu_exact_31_joint_sample_v1",
            "本校准入口只接受精确CMU映射")
    require(target.animation_data is None or target.animation_data.action is None,
            "校准前须由共享转换核解绑目标Action")
    require(bvh_import.get("rig") == source, "BVH导入记录不属于当前源骨架")
    require(bvh_import.get("action") == source.animation_data.action,
            "BVH导入记录不属于当前源Action")
    captured = capture_clip(source, mapping, clip=clip, origin_frame=origin_frame)
    clip_record = captured["record"]
    identity = build_identity(source, target, mapping, bvh_import)
    prepared = _cached_skeleton(source, target, mapping, identity, clip_record)
    cache_hit = prepared is not None
    if prepared is None:
        adapter, skeleton = _build_skeleton(source, target, mapping, identity, clip_record)
    else:
        adapter, skeleton = prepared
    skeleton = dict(skeleton)
    skeleton["cache_hit"] = bool(cache_hit)
    skeleton["calibration_reused"] = bool(cache_hit)
    record = {
        "schema": CALIBRATION_SCHEMA,
        "calibration_version": 1,
        "identity": identity,
        "identity_sha256": identity["identity_sha256"],
        "skeleton": skeleton,
        "clip": clip_record,
        "separation": {
            "orientation_reference": {
                "mode": ORIENTATION_REFERENCE,
                "source": "source.data.bones[name].matrix_local through matrix_world",
                "frame_1_role": "clip data only; not an orientation reference",
            },
            "translation_origin": {
                "mode": TRANSLATION_ORIGIN,
                "frame": clip_record["origin_frame"],
                "source_hip_world": clip_record["source_hip_world"],
                "root_z_zero_division": False,
            },
            "motion_scale": {
                "mode": MOTION_SCALE_BASIS,
                "source_leg_lengths_world": skeleton["source_leg_lengths_world"],
                "target_leg_lengths_world": skeleton["target_leg_lengths_world"],
                "leg_ratio": skeleton["leg_ratio"],
                "world_leg_ratio_average": skeleton["world_leg_ratio_average"],
                "target_object_unit": skeleton["target_object_unit"],
                "motion_scale": skeleton["motion_scale"],
                "adapter_armature_scale": skeleton["adapter_armature_scale"],
                "unit_division_count": 1,
            },
        },
        "preservation": {
            "source_action_written": False,
            "target_rest_changed": False,
            "target_constraints_changed": False,
            "target_weights_changed": False,
            "automatic_contact_correction": False,
            "additional_trajectory_correction": False,
        },
        "limitations": [
            "CMU 02_01显式参考校准开发切片，不是通用BVH支持声明",
            "源clip第1帧不是源REST；其偏差按clip数据记录，不作朝向neutral",
            "原生目标约束可产生姿态残差；未添加补偿轨迹或接触修正",
            "手指、表情、视线、镜头未转换",
        ],
    }
    record["record_sha256"] = canonical_hash(
        {key: value for key, value in record.items() if key != "record_sha256"})
    return {"adapter": adapter, "record": record, "identity": identity,
            "clip_record": clip_record, "captured": captured}


def record_summary(record):
    skeleton = record["skeleton"]
    clip = record["clip"]
    return {
        "schema": record["schema"],
        "identity_sha256": record["identity_sha256"],
        "record_sha256": record["record_sha256"],
        "orientation_reference": record["separation"]["orientation_reference"]["mode"],
        "translation_origin": record["separation"]["translation_origin"]["mode"],
        "motion_scale_basis": record["separation"]["motion_scale"]["mode"],
        "motion_scale": skeleton["motion_scale"],
        "world_leg_ratio_average": skeleton["world_leg_ratio_average"],
        "adapter_armature_scale": skeleton["adapter_armature_scale"],
        "source_leg_lengths_world": skeleton["source_leg_lengths_world"],
        "target_leg_lengths_world": skeleton["target_leg_lengths_world"],
        "leg_ratio": skeleton["leg_ratio"],
        "target_object_unit": skeleton["target_object_unit"],
        "source_rest_world_sha256": skeleton["source_rest_world_sha256"],
        "target_neutral_hash": skeleton["target_neutral_hash"],
        "calibration_hash": skeleton["calibration_hash"],
        "cache_hit": skeleton["cache_hit"],
        "calibration_reused": skeleton["calibration_reused"],
        "clip": {
            "action": clip["action"],
            "slot": clip["slot"],
            "fps": clip["fps"],
            "range": clip["range"],
            "origin_frame": clip["origin_frame"],
            "max_clip_start_to_rest_degrees": clip["max_clip_start_to_rest_degrees"],
            "source_clip_start_to_rest_degrees": clip["source_clip_start_to_rest_degrees"],
            "clip_start_is_source_rest_pose": clip["clip_start_is_source_rest_pose"],
            "first_frame_is_neutral": clip["first_frame_is_neutral"],
            "source_hip_world": clip["source_hip_world"],
            "source_ground_z_world": clip["source_ground_z_world"],
        },
    }


def load_calibration_from_action(action):
    require(ACTION_CALIBRATION_KEY in action, "候选Action没有校准记录")
    value = json.loads(action[ACTION_CALIBRATION_KEY])
    require(value.get("schema") == CALIBRATION_SCHEMA, "校准schema不受支持")
    require(value.get("record_sha256") == canonical_hash(
        {key: item for key, item in value.items() if key != "record_sha256"}),
        "校准记录哈希不匹配")
    require(value.get("identity_sha256")
            == value["identity"]["identity_sha256"], "校准身份哈希不匹配")
    require(value["separation"]["orientation_reference"]["mode"] == ORIENTATION_REFERENCE,
            "朝向参考不是REST")
    require(value["separation"]["translation_origin"]["mode"] == TRANSLATION_ORIGIN,
            "位移原点不是CLIP_START")
    require(value["separation"]["motion_scale"]["mode"] == MOTION_SCALE_BASIS,
            "位移比例不是LEG_LENGTH")
    require(value["clip"]["origin_mode"] == TRANSLATION_ORIGIN,
            "clip origin模式不是CLIP_START")
    return value


def convert_cmu_bvh(source, target, *, bvh_import, mapping=None, source_fps=None,
                    start=None, end=None, output_start=1, action_name=None,
                    progress=None, cancelled=None, motion='PRESERVE', sample_step=1):
    """Full CMU clip conversion through the shared retarget_action core."""
    source_api.armature(source)
    source_api.armature(target)
    require(motion in ('PRESERVE','IN_PLACE'), '根运动模式无效')
    exact = mapping or source_api.exact_cmu_mapping(source)
    require(exact.get("preset") == "cmu_exact_31_joint_sample_v1",
            "本入口只接受精确CMU映射")
    clip = source_api.active_clip(source, source_fps)
    origin_frame = float(start) if start is not None else float(clip["range"][0])
    holder = {}

    def factory(current_source, current_target, current_mapping):
        prepared = prepare_adapter(
            current_source, current_target, current_mapping, clip=clip,
            origin_frame=origin_frame, bvh_import=bvh_import)
        holder["prepared"] = prepared
        prepared['adapter'].motion = motion
        return prepared["adapter"], prepared["record"]

    result = action_api.convert_resolved(
        source, target, mapped=exact, clip=clip, start=start, end=end,
        output_start=output_start, motion=motion, scale_motion=False,
        include_fingers=False, sample_step=sample_step, action_name=action_name,
        progress=progress, cancelled=cancelled, adapter_factory=factory)
    require("prepared" in holder, "校准适配器工厂未被调用")
    record = holder["prepared"]["record"]
    action = result["action"]
    action[ACTION_CALIBRATION_KEY] = json.dumps(record, ensure_ascii=False)
    report = result["report"]
    report.update(record_summary(record))
    report["calibration_schema"] = CALIBRATION_SCHEMA
    report["orientation_reference"] = ORIENTATION_REFERENCE
    report["translation_origin"] = TRANSLATION_ORIGIN
    report["motion_scale_basis"] = MOTION_SCALE_BASIS
    report["calibration_record_sha256"] = record["record_sha256"]
    action[ACTION_REPORT_KEY] = json.dumps(report, ensure_ascii=False)
    result["report"] = report
    result["calibration_record"] = record
    result["adapter_record"] = record
    return result
