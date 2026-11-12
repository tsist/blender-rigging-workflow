# SPDX-License-Identifier: GPL-3.0-or-later
"""Transparent background retarget batch jobs for the accepted C94 core.

This module deliberately contains no second retarget solver.  A job captures the
current Scene with ``bpy.data.libraries.write`` (never ``save_as_mainfile``),
copies the running package into an isolated runtime, and starts one owned
Blender process.  Items use the existing ``retarget_action`` / ``retarget_bvh``
conversion entries and write independent Action-only blends.

The shared queue also dispatches explicit MMD, Profile, ordinary FK and
contact items to their dedicated adapters. External Library/cache closure
is reported as partial. The historical GLB exchange platform is separate.
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

import bpy

from . import retarget_action as rt
from . import retarget_bvh as bvh
from . import retarget_c94 as c94
from . import retarget_calibration as calibration
from . import retarget_source as sources
from . import retarget_general_job as general_job


JOB_SCHEMA = "c94.retarget.job.v1"
SNAPSHOT_SCHEMA = "c94.retarget.job.snapshot.v1"
REQUEST_SCHEMA = "c94.retarget.job.request.v1"
LEDGER_SCHEMA = "c94.retarget.job.ledger.v1"
RESULT_SCHEMA = "c94.retarget.job.result.v1"
ACTION_SCHEMA = "c94.retarget.job.action.v1"
COLLECT_SCHEMA = "c94.retarget.job.collect.v1"
JOB_KEY = "c94_retarget_job_v1"

_ITEM_FIELDS = {
    "action", "source_action", "slot", "slot_identifier", "fps", "source_fps",
    "start", "end", "output_start", "action_name", "output_name",
    "output_action_name", "label",
}
_SETTINGS_FIELDS = {
    "motion", "scale_motion", "include_fingers", "max_seconds",
    "startup_grace_seconds", "cancel_grace_seconds", "threads", "contact_detection_calibration",
}
_STATES = ("not_started", "running", "succeeded", "failed", "cancelled")


class JobError(RuntimeError):
    def __init__(self, message, *, code="JOB_ERROR", details=None):
        self.code = code
        self.details = details
        super().__init__(message)

    def as_dict(self):
        value = {"code": self.code, "message": str(self)}
        if self.details is not None:
            value["details"] = self.details
        return value


def _require(value, message, *, code="INVALID_REQUEST"):
    if not value:
        raise JobError(message, code=code)
    return value


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=repr,
    )


def _sha256_json(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _atomic_replace(source, target):
    for attempt in range(12):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(0.05)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _round(value, digits=9):
    return round(float(value), digits)


def _matrix_list(matrix):
    return [[_round(value) for value in row] for row in matrix]


def _finite_number(value, message):
    _require(type(value) in (int, float) and math.isfinite(float(value)), message)
    return float(value)


def _binding_descriptor(rig):
    animation = rig.animation_data
    if animation is None:
        return {"exists": False}
    fields = {}
    for name in rt._AD_FIELDS:
        try:
            fields[name] = getattr(animation, name)
        except AttributeError:
            fields[name] = None
    action = animation.action
    slot = animation.action_slot
    return {
        "exists": True,
        "action": action.name if action else None,
        "action_pointer": action.as_pointer() if action else None,
        "slot": slot.identifier if slot else None,
        "slot_handle": int(getattr(slot, "handle", 0)) if slot else 0,
        "last_slot_identifier": animation.last_slot_identifier,
        "fields": fields,
    }


def _binding_key(value):
    return _canonical(value)


_CONSTRAINT_COMMON_FIELDS = (
    "mute", "enabled", "influence", "owner_space", "target_space",
    "space_subtarget", "subtarget", "pole_subtarget", "chain_count",
    "mix_mode", "use_offset",
)
_CONSTRAINT_TYPE_FIELDS = {
    "LIMIT_ROTATION": (
        "use_limit_x", "use_limit_y", "use_limit_z",
        "min_x", "max_x", "min_y", "max_y", "min_z", "max_z",
        "euler_order", "use_transform_limit", "use_legacy_behavior",
    ),
    "COPY_ROTATION": (
        "use_x", "use_y", "use_z", "invert_x", "invert_y", "invert_z",
        "euler_order", "mix_mode", "use_offset",
    ),
    "COPY_TRANSFORMS": (
        "head_tail", "use_bbone_shape", "remove_target_shear", "mix_mode",
    ),
    "STRETCH_TO": (
        "head_tail", "use_bbone_shape", "volume", "keep_axis",
        "rest_length", "bulge", "use_bulge_min", "use_bulge_max",
        "bulge_min", "bulge_max", "bulge_smooth",
    ),
    "DAMPED_TRACK": ("head_tail", "use_bbone_shape", "track_axis"),
    "TRANSFORM": (
        "from_min_x", "from_max_x", "from_min_y", "from_max_y",
        "from_min_z", "from_max_z", "to_min_x", "to_max_x",
        "to_min_y", "to_max_y", "to_min_z", "to_max_z",
        "from_min_x_rot", "from_max_x_rot", "from_min_y_rot",
        "from_max_y_rot", "from_min_z_rot", "from_max_z_rot",
        "to_min_x_rot", "to_max_x_rot", "to_min_y_rot",
        "to_max_y_rot", "to_min_z_rot", "to_max_z_rot",
        "from_min_x_scale", "from_max_x_scale", "from_min_y_scale",
        "from_max_y_scale", "from_min_z_scale", "from_max_z_scale",
        "to_min_x_scale", "to_max_x_scale", "to_min_y_scale",
        "to_max_y_scale", "to_min_z_scale", "to_max_z_scale",
        "map_from", "map_to", "map_to_x_from", "map_to_y_from",
        "map_to_z_from", "use_motion_extrapolate", "from_rotation_mode",
        "to_euler_order", "mix_mode", "mix_mode_rot", "mix_mode_scale",
    ),
    "LIMIT_LOCATION": (
        "use_min_x", "use_max_x", "use_min_y", "use_max_y",
        "use_min_z", "use_max_z", "min_x", "max_x", "min_y",
        "max_y", "min_z", "max_z", "use_transform_limit",
    ),
    "COPY_LOCATION": (
        "head_tail", "use_bbone_shape", "use_x", "use_y", "use_z",
        "invert_x", "invert_y", "invert_z", "use_offset",
    ),
    "IK": (
        "iterations", "pole_subtarget", "pole_angle", "weight",
        "orient_weight", "chain_count", "use_tail", "reference_axis",
        "use_location", "lock_location_x", "lock_location_y",
        "lock_location_z", "use_rotation", "lock_rotation_x",
        "lock_rotation_y", "lock_rotation_z", "use_stretch", "ik_type",
        "limit_mode", "distance",
    ),
    "LIMIT_SCALE": (
        "use_min_x", "use_max_x", "use_min_y", "use_max_y",
        "use_min_z", "use_max_z", "min_x", "max_x", "min_y",
        "max_y", "min_z", "max_z", "use_transform_limit",
    ),
    "COPY_SCALE": (
        "use_x", "use_y", "use_z", "power", "use_make_uniform",
        "use_offset", "use_add",
    ),
    "LOCKED_TRACK": (
        "head_tail", "use_bbone_shape", "track_axis", "lock_axis",
    ),
    "LIMIT_DISTANCE": (
        "head_tail", "use_bbone_shape", "distance", "limit_mode",
        "use_transform_limit",
    ),
    "ARMATURE": (
        "use_deform_preserve_volume", "use_bone_envelopes",
        "use_current_location",
    ),
}
_CONSTRAINT_REFERENCE_FIELDS = ("target", "pole_target", "space_object")
_CONTROL_OUTPUT_KEYS = set(c94.MODES)
_CONTROL_FACE_MARKERS = ("眼", "嘴", "脸")


def _simple_property(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return _round(value)
    if isinstance(value, (list, tuple)):
        rows = [_simple_property(item) for item in value]
        return rows if all(item is not None for item in rows) else None
    return None


def _reference(value, rig, live_refs=None, key=None):
    if not isinstance(value, bpy.types.ID):
        return None
    if value == rig:
        return "SELF"
    data = getattr(value, "data", None)
    descriptor = {
        "ref": "EXTERNAL",
        "type": getattr(value, "type", value.__class__.__name__),
        "object": getattr(value, "name", None),
        "data": data.name if data is not None else None,
    }
    if live_refs is not None and key is not None:
        live_refs[key] = {
            "object_session_uid": int(value.session_uid),
            "object_pointer": int(value.as_pointer()),
            "data_pointer": int(data.as_pointer()) if data is not None else None,
        }
    return descriptor


def _action_curve_paths(action):
    paths = set()
    if action is None:
        return paths
    for layer in action.layers:
        for strip in layer.strips:
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag is None:
                    continue
                for curve in bag.fcurves:
                    paths.add(curve.data_path)
    return paths


def _animation_paths(rig):
    paths = set()
    animation = rig.animation_data
    if animation is None:
        return paths
    paths.update(_action_curve_paths(animation.action))
    for track in animation.nla_tracks:
        for strip in track.strips:
            paths.update(_action_curve_paths(getattr(strip, "action", None)))
    return paths


def _driver_curve_paths(rig):
    paths = set()
    for owner in (rig, getattr(rig, "data", None), *rig.pose.bones):
        if owner is None:
            continue
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            continue
        for curve in animation.drivers:
            paths.add(curve.data_path)
    return paths


def _constraint_rows(rig, driven_paths=None, live_refs=None):
    driven_paths = set() if driven_paths is None else driven_paths
    live_refs = {} if live_refs is None else live_refs
    rows = []
    owners = [(None, rig, rig.constraints)]
    for pose_bone in sorted(rig.pose.bones, key=lambda item: item.name):
        owners.append((pose_bone.name, pose_bone, pose_bone.constraints))
    for bone_name, owner, constraints in owners:
        for constraint in constraints:
            base = f'constraints["{constraint.name}"]'
            if bone_name is not None:
                base = f'pose.bones["{bone_name}"].{base}'
            row = {
                "bone": bone_name or "",
                "name": constraint.name,
                "type": constraint.type,
            }
            fields = tuple(dict.fromkeys(
                _CONSTRAINT_COMMON_FIELDS +
                _CONSTRAINT_TYPE_FIELDS.get(constraint.type, ())))
            for field_name in fields:
                if not hasattr(constraint, field_name):
                    continue
                path = base + "." + field_name
                if path in driven_paths:
                    continue
                value = getattr(constraint, field_name)
                if isinstance(value, bpy.types.ID):
                    value = _reference(value, rig, live_refs, path)
                elif isinstance(value, float):
                    value = _round(value)
                elif value is not None and not isinstance(value, (str, bool, int)):
                    continue
                if value is not None:
                    row[field_name] = value
            for field_name in _CONSTRAINT_REFERENCE_FIELDS:
                value = getattr(constraint, field_name, None)
                if not isinstance(value, bpy.types.ID):
                    continue
                path = base + "." + field_name
                if path in driven_paths:
                    continue
                row[field_name + "_ref"] = _reference(
                    value, rig, live_refs, path)
            targets = getattr(constraint, "targets", None)
            if targets is not None:
                target_rows = []
                for index, target in enumerate(targets):
                    path = base + f".targets[{index}]"
                    target_row = {"subtarget": target.subtarget}
                    if path + ".weight" not in driven_paths:
                        target_row["weight"] = _round(target.weight)
                    if path + ".target" not in driven_paths:
                        target_row["target_ref"] = _reference(
                            target.target, rig, live_refs, path + ".target")
                    target_rows.append(target_row)
                row["targets"] = target_rows
            rows.append(row)
    return rows


_DRIVER_KEYFRAME_FIELDS = (
    "interpolation", "easing", "back", "amplitude", "period",
    "handle_left", "handle_right", "handle_left_type", "handle_right_type",
)
_DRIVER_MODIFIER_COMMON_FIELDS = (
    "mute", "use_influence", "influence", "use_restricted_range",
    "frame_start", "frame_end", "blend_in", "blend_out",
)
_DRIVER_MODIFIER_FIELDS = {
    "GENERATOR": ("mode", "poly_order", "coefficients", "use_additive"),
    "FNGENERATOR": (
        "function_type", "amplitude", "phase_multiplier", "phase_offset",
        "value_offset", "use_additive",
    ),
    "ENVELOPE": ("reference_value", "default_min", "default_max"),
    "CYCLES": ("mode_before", "mode_after", "cycles_before", "cycles_after"),
    "NOISE": (
        "blend_type", "depth", "lacunarity", "offset", "phase",
        "roughness", "scale", "strength", "use_legacy_noise",
    ),
    "LIMITS": (
        "min_x", "max_x", "min_y", "max_y",
        "use_min_x", "use_max_x", "use_min_y", "use_max_y",
    ),
    "STEPPED": (
        "frame_step", "frame_offset", "use_frame_start", "use_frame_end",
    ),
    "SMOOTH": ("filter_width", "sigma"),
}


def _plain_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return _round(value)
    if hasattr(value, "__len__") and not isinstance(value, str):
        try:
            return [_plain_value(item) for item in value]
        except TypeError:
            pass
    return None


def _driver_keyframe_rows(curve):
    rows = []
    for point in curve.keyframe_points:
        row = {"co": [_round(point.co[0]), _round(point.co[1])]}
        for field_name in _DRIVER_KEYFRAME_FIELDS:
            if not hasattr(point, field_name):
                continue
            value = _plain_value(getattr(point, field_name))
            if value is not None:
                row[field_name] = value
        rows.append(row)
    return rows


def _driver_modifier_rows(curve):
    rows = []
    for modifier in curve.modifiers:
        row = {"type": modifier.type}
        fields = _DRIVER_MODIFIER_COMMON_FIELDS + _DRIVER_MODIFIER_FIELDS.get(
            modifier.type, ())
        for field_name in fields:
            if not hasattr(modifier, field_name):
                continue
            value = getattr(modifier, field_name)
            if field_name == "control_points":
                row[field_name] = [
                    {
                        key: _plain_value(getattr(point, key))
                        for key in ("frame", "min", "max")
                    }
                    for point in value
                ]
                continue
            value = _plain_value(value)
            if value is not None:
                row[field_name] = value
        rows.append(row)
    return rows


def _driver_rows(rig, live_refs=None):
    live_refs = {} if live_refs is None else live_refs
    rows = []
    owners = [("OBJECT", rig)]
    if getattr(rig, "data", None) is not None:
        owners.append(("DATA", rig.data))
    for pose_bone in sorted(rig.pose.bones, key=lambda item: item.name):
        owners.append((pose_bone.name, pose_bone))
    for owner_name, owner in owners:
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            continue
        for curve in animation.drivers:
            driver = curve.driver
            variables = []
            for variable in driver.variables:
                targets = []
                for index, item in enumerate(variable.targets):
                    key = (
                        f"driver:{owner_name}:{curve.data_path}:"
                        f"{curve.array_index}:{variable.name}:{index}"
                    )
                    reference = _reference(item.id, rig, live_refs, key)
                    targets.append({
                        "self": reference == "SELF",
                        "reference": reference,
                        "data_path": item.data_path,
                        "bone_target": item.bone_target,
                        "transform_type": item.transform_type,
                        "transform_space": item.transform_space,
                        "rotation_mode": getattr(item, "rotation_mode", None),
                    })
                variables.append({
                    "name": variable.name,
                    "type": variable.type,
                    "targets": targets,
                })
            rows.append({
                "owner": owner_name,
                "data_path": curve.data_path,
                "array_index": int(curve.array_index),
                "type": driver.type,
                "expression": driver.expression,
                "use_self": bool(driver.use_self) if hasattr(driver, "use_self") else None,
                "mute": bool(curve.mute),
                "extrapolation": curve.extrapolation,
                "auto_smoothing": getattr(curve, "auto_smoothing", None),
                "keyframes": _driver_keyframe_rows(curve),
                "modifiers": _driver_modifier_rows(curve),
                "variables": variables,
            })
    return rows


def _solve_control_key(key):
    if key in ("手指IK", "躯干拉伸"):
        return True
    if key.endswith("跟随") and not any(
            marker in key for marker in _CONTROL_FACE_MARKERS):
        return True
    return key.endswith("连接")


def _control_rows(rig, driven_paths=None):
    driven_paths = set() if driven_paths is None else driven_paths
    rows = []
    owners = [(None, rig)]
    for pose_bone in sorted(rig.pose.bones, key=lambda item: item.name):
        owners.append((pose_bone.name, pose_bone))
    for owner_name, owner in owners:
        for key in sorted(owner.keys()):
            if key == "_RNA_UI" or key in _CONTROL_OUTPUT_KEYS:
                continue
            if not _solve_control_key(key):
                continue
            path = f'["{key}"]' if owner_name is None else \
                f'pose.bones["{owner_name}"]["{key}"]'
            if path in driven_paths:
                continue
            value = _simple_property(owner[key])
            if value is None:
                continue
            rows.append({
                "owner": owner_name or "OBJECT",
                "key": key,
                "value": value,
            })
    return rows


_OBJECT_TRANSFORM_CHANNELS = {
    "location": 3,
    "rotation_euler": 3,
    "rotation_quaternion": 4,
    "rotation_axis_angle": 4,
    "scale": 3,
    "delta_location": 3,
    "delta_rotation_euler": 3,
    "delta_rotation_quaternion": 4,
    "delta_scale": 3,
}


def _is_object_matrix_path(path):
    return path == "matrix_world" or path.startswith("matrix_")


def _slot_object_transform_curves(action, slot):
    curves = []
    unsupported = False
    for layer in action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(slot)
            if bag is None:
                continue
            for curve in bag.fcurves:
                path = curve.data_path
                if path in _OBJECT_TRANSFORM_CHANNELS:
                    if 0 <= int(curve.array_index) < _OBJECT_TRANSFORM_CHANNELS[path]:
                        curves.append(curve)
                elif _is_object_matrix_path(path):
                    unsupported = True
    return curves, unsupported


def _action_has_object_transform(action):
    for slot in action.slots:
        curves, unsupported = _slot_object_transform_curves(action, slot)
        if curves or unsupported:
            return True
    return False


def _action_object_transform_status(obj):
    animation = obj.animation_data
    if animation is None or animation.action is None:
        return {"curves": [], "unsupported": False}
    action = animation.action
    slot = animation.action_slot
    if slot is None:
        return {
            "curves": [],
            "unsupported": _action_has_object_transform(action),
        }
    curves, unsupported = _slot_object_transform_curves(action, slot)
    if not curves and not unsupported:
        return {"curves": [], "unsupported": False}
    if animation.action_blend_type != "REPLACE" or \
            abs(float(animation.action_influence) - 1.0) > 1e-6:
        return {"curves": [], "unsupported": True}
    return {
        "curves": [] if unsupported else curves,
        "unsupported": unsupported,
    }


def _object_transform_driver_present(obj):
    animation = obj.animation_data
    if animation is None:
        return False
    return any(
        curve.data_path in _OBJECT_TRANSFORM_CHANNELS or
        _is_object_matrix_path(curve.data_path)
        for curve in animation.drivers
    )


def _nla_object_transform_present(obj):
    animation = obj.animation_data
    if animation is None:
        return False
    for track in animation.nla_tracks:
        if track.mute:
            continue
        for strip in track.strips:
            if strip.mute:
                continue
            if not getattr(strip, "use_animated_influence", False) and \
                    abs(float(strip.influence)) <= 1e-9:
                continue
            action = strip.action
            if action is None:
                continue
            slot = getattr(strip, "action_slot", None)
            if slot is None:
                if _action_has_object_transform(action):
                    return True
                continue
            curves, unsupported = _slot_object_transform_curves(action, slot)
            if curves or unsupported:
                return True
    return False


def _object_transform_classification(rig):
    unsupported = bool(rig.constraints) or \
        _object_transform_driver_present(rig) or \
        _nla_object_transform_present(rig)
    action_status = _action_object_transform_status(rig)
    if action_status["unsupported"]:
        unsupported = True
    parent = rig.parent
    while parent is not None:
        if parent.constraints or _object_transform_driver_present(parent) or \
                _nla_object_transform_present(parent):
            unsupported = True
        parent_status = _action_object_transform_status(parent)
        if parent_status["curves"] or parent_status["unsupported"]:
            unsupported = True
        parent = parent.parent
    if unsupported:
        return "UNSUPPORTED"
    if action_status["curves"]:
        return "ACTION"
    return "NONE"


def _object_transform_animated(rig):
    return _object_transform_classification(rig) != "NONE"


def _object_transform_witness(rig):
    rows = {}
    for name in _OBJECT_TRANSFORM_CHANNELS:
        value = getattr(rig, name, None)
        if value is not None:
            rows[name] = [_round(item) for item in value]
    rows["rotation_mode"] = rig.rotation_mode
    parents = []
    parent = rig.parent
    while parent is not None:
        parents.append({
            "name": parent.name,
            "matrix_world": _matrix_list(parent.matrix_world),
        })
        parent = parent.parent
    rows["parents"] = parents
    return rows


def _expected_action_object_transform(rig, frame, captured):
    expected = {
        key: list(value) if isinstance(value, list) else value
        for key, value in (captured or {}).items()
    }
    status = _action_object_transform_status(rig)
    for curve in status["curves"]:
        path = curve.data_path
        values = expected.get(path)
        if not isinstance(values, list):
            continue
        index = int(curve.array_index)
        if 0 <= index < len(values):
            values[index] = _round(curve.evaluate(float(frame)))
    return expected


def _rig_static_fingerprint(rig):
    """Session-independent enough for snapshot/rename-safe collect checks.

    Mutable object/data names are deliberately not the identity.  Bone names,
    rest transforms, parent graph, pose rotation modes, full editable
    constraint configuration, driver graph, solve-affecting non-output
    controls, object world transform and scene timing/units are.  The four
    FK/IK mode switches written by the conversion and ordinary pose channels
    are outputs, not structural identity.  External object references carry a
    static label; their live session pointers are kept outside the static hash
    for the same-session collect gate.
    """
    live_refs = {}
    driven_paths = _driver_curve_paths(rig) | _animation_paths(rig)
    drivers = _driver_rows(rig, live_refs)
    constraints = _constraint_rows(rig, driven_paths, live_refs)
    controls = _control_rows(rig, driven_paths)
    bones = []
    for bone in sorted(rig.data.bones, key=lambda item: item.name):
        bones.append({
            "name": bone.name,
            "parent": bone.parent.name if bone.parent else None,
            "matrix_local": _matrix_list(bone.matrix_local),
            "head_local": [_round(value) for value in bone.head_local],
            "tail_local": [_round(value) for value in bone.tail_local],
            "connected": bool(bone.use_connect),
            "deform": bool(bone.use_deform),
            "inherit_scale": bone.inherit_scale,
            "inherit_rotation": bool(bone.use_inherit_rotation),
            "local_location": bool(bone.use_local_location),
            "bbone_segments": int(bone.bbone_segments),
        })
    rotation_modes = {
        pose_bone.name: pose_bone.rotation_mode
        for pose_bone in sorted(rig.pose.bones, key=lambda item: item.name)
    }
    modes = {}
    settings_bone = rig.pose.bones.get("骨架设置")
    if settings_bone is not None:
        for key in c94.MODES:
            if key in settings_bone.keys():
                modes[key] = settings_bone[key]
    scene = bpy.context.scene
    matrix_world = _matrix_list(rig.matrix_world)
    object_transform_kind = _object_transform_classification(rig)
    object_transform = _object_transform_witness(rig)
    matrix_world_dynamic = object_transform_kind != "NONE"
    payload = {
        "schema": "c94.retarget.job.rig_static.v2",
        "type": rig.type,
        "data_type": rig.data.__class__.__name__ if rig.data else None,
        "pose_position": rig.data.pose_position,
        "matrix_world": matrix_world,
        "matrix_world_dynamic": matrix_world_dynamic,
        "object_transform_kind": object_transform_kind,
        "object_transform": object_transform,
        "object_rotation_mode": rig.rotation_mode,
        "matrix_world_frame": [int(scene.frame_current), _round(scene.frame_subframe)],
        "matrix_parent_inverse": _matrix_list(rig.matrix_parent_inverse),
        "parent_type": rig.parent_type,
        "parent_bone": rig.parent_bone,
        "bone_count": len(rig.data.bones),
        "bones": bones,
        "rotation_modes": rotation_modes,
        "constraints": constraints,
        "drivers": drivers,
        "controls": controls,
        "modes": modes,
        "session_refs": live_refs,
        "target_uid": rig.get(rt.TARGET_UID),
        "scene": {
            "fps": int(scene.render.fps),
            "fps_base": _round(scene.render.fps_base),
            "frame_map": [int(scene.render.frame_map_old), int(scene.render.frame_map_new)],
            "unit": [
                scene.unit_settings.system,
                _round(scene.unit_settings.scale_length),
                scene.unit_settings.length_unit,
            ],
        },
    }
    static_payload = dict(payload)
    static_payload.pop("matrix_world_frame", None)
    static_payload.pop("session_refs", None)
    static_payload.pop("modes", None)
    static_payload.pop("object_transform", None)
    if matrix_world_dynamic:
        static_payload.pop("matrix_world", None)
    payload["static_sha256"] = _sha256_json(static_payload)
    return payload


def _compact_fingerprint(fingerprint):
    return {
        "schema": "c94.retarget.job.rig_fingerprint.v2",
        "static_sha256": fingerprint["static_sha256"],
        "rest_sha256": _sha256_json(fingerprint["bones"]),
        "rotation_modes_sha256": _sha256_json(fingerprint["rotation_modes"]),
        "constraints_sha256": _sha256_json(fingerprint["constraints"]),
        "drivers_sha256": _sha256_json(fingerprint["drivers"]),
        "controls_sha256": _sha256_json(fingerprint["controls"]),
        "modes_sha256": _sha256_json(fingerprint["modes"]),
        "scene_sha256": _sha256_json(fingerprint["scene"]),
        "matrix_world": fingerprint["matrix_world"],
        "matrix_parent_inverse": fingerprint["matrix_parent_inverse"],
        "matrix_world_dynamic": fingerprint["matrix_world_dynamic"],
        "object_transform_kind": fingerprint["object_transform_kind"],
        "object_transform": fingerprint["object_transform"],
        "matrix_world_frame": fingerprint["matrix_world_frame"],
        "bone_count": fingerprint["bone_count"],
        "pose_position": fingerprint["pose_position"],
        "target_uid": fingerprint["target_uid"],
    }


def _pose_hash(rig):
    rows = []
    for pose_bone in sorted(rig.pose.bones, key=lambda item: item.name):
        rows.append({
            "name": pose_bone.name,
            "mode": pose_bone.rotation_mode,
            "location": [_round(value) for value in pose_bone.location],
            "rotation_quaternion": [_round(value) for value in pose_bone.rotation_quaternion],
            "rotation_euler": [_round(value) for value in pose_bone.rotation_euler],
            "scale": [_round(value) for value in pose_bone.scale],
        })
    return _sha256_json(rows)


def _weights_witness(target):
    digest = hashlib.sha256()
    rows = 0
    for obj in sorted(bpy.context.scene.objects, key=lambda item: item.name):
        if obj.type != "MESH":
            continue
        modifiers = [modifier for modifier in obj.modifiers
                     if modifier.type == "ARMATURE" and modifier.object == target]
        if not modifiers:
            continue
        rows += 1
        digest.update(("object:" + obj.name + "\n").encode("utf-8"))
        digest.update(("data:" + obj.data.name + "\n").encode("utf-8"))
        for modifier in modifiers:
            digest.update(("modifier:" + modifier.name + "\n").encode("utf-8"))
        for index, vertex in enumerate(obj.data.vertices):
            groups = sorted((group.group, _round(group.weight, 12)) for group in vertex.groups)
            digest.update((str(index) + ":" + _canonical(groups) + "\n").encode("utf-8"))
        for group in obj.vertex_groups:
            digest.update(("group:" + group.name + ":" + str(group.index) + "\n").encode("utf-8"))
    return {"rows": rows, "sha256": digest.hexdigest()}


def _action_inventory():
    rows = []
    for action in sorted(bpy.data.actions, key=lambda item: item.name):
        rows.append({
            "name": action.name,
            "users": int(action.users),
            "fake_user": bool(action.use_fake_user),
            "slots": len(action.slots),
        })
    return rows


def _scene_identity(scene):
    return {
        "name": scene.name,
        "session_uid": int(scene.session_uid),
        "pointer": int(scene.as_pointer()),
        "view_layer": bpy.context.view_layer.name,
    }


def _object_runtime(obj, scene):
    if obj is None:
        return None
    return {
        "name": obj.name,
        "type": obj.type,
        "session_uid": int(obj.session_uid),
        "pointer": int(obj.as_pointer()),
        "data_name": obj.data.name if obj.data else None,
        "data_pointer": int(obj.data.as_pointer()) if obj.data else None,
        "scene_session_uid": int(scene.session_uid),
        "scene_pointer": int(scene.as_pointer()),
    }


def _selection_state():
    return {
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name
        if bpy.context.view_layer.objects.active else None,
        "mode": bpy.context.mode,
    }


def _state_witness(scene, source, target, target_uid):
    source_binding = _binding_descriptor(source)
    target_binding = _binding_descriptor(target)
    source_action = source.animation_data.action if source.animation_data else None
    target_action = target.animation_data.action if target.animation_data else None
    return {
        "filepath": str(bpy.data.filepath),
        "dirty": bool(bpy.data.is_dirty),
        "scene": _scene_identity(scene),
        "scene_frame": [int(scene.frame_current), _round(scene.frame_subframe)],
        "scene_range": [int(scene.frame_start), int(scene.frame_end)],
        "fps": [int(scene.render.fps), _round(scene.render.fps_base)],
        "frame_map": [int(scene.render.frame_map_old), int(scene.render.frame_map_new)],
        "camera": scene.camera.name if scene.camera else None,
        "unit": [
            scene.unit_settings.system,
            _round(scene.unit_settings.scale_length),
            scene.unit_settings.length_unit,
        ],
        "selection": _selection_state(),
        "target_uid": target_uid,
        "source_binding": source_binding,
        "target_binding": target_binding,
        "source_pose_sha256": _pose_hash(source),
        "target_pose_sha256": _pose_hash(target),
        "source_static_sha256": _rig_static_fingerprint(source)["static_sha256"],
        "target_static_sha256": _rig_static_fingerprint(target)["static_sha256"],
        "source_matrix_world": _matrix_list(source.matrix_world),
        "target_matrix_world": _matrix_list(target.matrix_world),
        "source_matrix_parent_inverse": _matrix_list(source.matrix_parent_inverse),
        "target_matrix_parent_inverse": _matrix_list(target.matrix_parent_inverse),
        "source_action_sha256": calibration.action_signature(source_action),
        "target_action_sha256": calibration.action_signature(target_action),
        "weights": _weights_witness(target),
        "actions": _action_inventory(),
    }


def _state_diff(before, after):
    changed = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed[key] = {"before": before.get(key), "after": after.get(key)}
    return changed


def _find_action_by_name(name):
    if not isinstance(name, str) or not name:
        raise JobError("源Action名称必须为非空字符串", code="INVALID_ITEM")
    matches = [action for action in bpy.data.actions if action.name == name]
    if len(matches) != 1:
        raise JobError("源Action不存在或有同名歧义：" + name, code="INVALID_ITEM")
    return matches[0]


def _find_slot(action, value):
    if value is None:
        return None
    if isinstance(value, bpy.types.ActionSlot):
        matches = [slot for slot in action.slots if slot == value]
    elif isinstance(value, str):
        matches = [slot for slot in action.slots if slot.identifier == value]
    else:
        raise JobError("Slot必须是ActionSlot或identifier字符串", code="INVALID_ITEM")
    if len(matches) != 1:
        raise JobError("Action Slot不存在或有歧义：" + str(value), code="INVALID_ITEM")
    slot = matches[0]
    if slot.target_id_type != "OBJECT":
        raise JobError("当前入口只接受OBJECT Action Slot", code="INVALID_ITEM")
    return slot


def _clip_record(action, slot, source_fps):
    _require(action is not None and slot is not None, "缺少源Action或Slot")
    _require(slot in action.slots[:], "源Action Slot身份无效")
    _require(slot.target_id_type == "OBJECT", "源Action Slot不是OBJECT")
    times = []
    for layer in action.layers:
        for strip in layer.strips:
            _require(strip.type == "KEYFRAME",
                     "当前源Action含未验证条带类型", code="INVALID_ITEM")
            bag = strip.channelbag(slot)
            if not bag:
                continue
            for curve in bag.fcurves:
                times.extend(float(point.co.x) for point in curve.keyframe_points)
                times.extend(float(point.co.x) for point in curve.sampled_points)
    _require(times and min(times) < max(times),
             "所选源Slot没有有效动画片段", code="INVALID_ITEM")
    if source_fps is None:
        try:
            timebase = json.loads(action.get(sources.TIME_KEY, ""))
            _require(slot.identifier in timebase["slots"],
                     "当前Slot没有已记录时基；请明确源fps", code="INVALID_ITEM")
            source_fps = timebase["fps"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise JobError("当前Action/Slot没有可信时基记录；请明确源fps",
                           code="INVALID_ITEM") from exc
    _require(type(source_fps) in (int, float) and 0 < float(source_fps) <= 1000,
             "源fps必须明确且有效", code="INVALID_ITEM")
    return {
        "action": action,
        "slot": slot,
        "fps": float(source_fps),
        "range": (min(times), max(times)),
    }


def _validate_settings(preset, settings, *, target_policy=None):
    if settings is None:
        settings = {}
    _require(isinstance(settings, dict), "settings必须为纯数据对象")
    unknown = sorted(set(settings) - _SETTINGS_FIELDS)
    _require(not unknown, "settings含未授权字段：" + ", ".join(unknown))
    value = {
        "motion": settings.get("motion", "PRESERVE"),
        "scale_motion": settings.get("scale_motion", True),
        "include_fingers": settings.get("include_fingers", True),
        "max_seconds": settings.get("max_seconds", 1800.0),
        "startup_grace_seconds": settings.get("startup_grace_seconds", 60.0),
        "cancel_grace_seconds": settings.get("cancel_grace_seconds", 15.0),
        "threads": settings.get("threads", 2),
        "contact_detection_calibration": json.loads(json.dumps(settings.get("contact_detection_calibration", {}),allow_nan=False)),
    }
    _require(isinstance(value["contact_detection_calibration"],dict),"contact_detection_calibration必须为纯JSON对象")
    _require(value["motion"] in ("PRESERVE", "IN_PLACE"), "根运动模式无效")
    _require(isinstance(value["scale_motion"], bool), "scale_motion必须为布尔值")
    _require(isinstance(value["include_fingers"], bool), "include_fingers必须为布尔值")
    _finite_number(value["max_seconds"], "max_seconds必须为有限正数")
    _require(0 < float(value["max_seconds"]) <= 86400, "max_seconds超出合理范围")
    _finite_number(value["startup_grace_seconds"], "startup_grace_seconds必须为有限数")
    _require(0 <= float(value["startup_grace_seconds"]) <= 600,
             "startup_grace_seconds超出合理范围")
    _finite_number(value["cancel_grace_seconds"], "cancel_grace_seconds必须为有限数")
    _require(0 <= float(value["cancel_grace_seconds"]) <= 300,
             "cancel_grace_seconds超出合理范围")
    _require(type(value["threads"]) is int and 1 <= value["threads"] <= 8,
             "threads必须为1..8整数")
    if preset in ("CMU", "MMD", "CONTACT_CORRECTION", "CONTACT_DETECTION",
                  general_job.PRESET_PROFILE):
        _require("scale_motion" not in settings and "include_fingers" not in settings,
                 preset + "预设不接受Mixamo专用设置")
    if target_policy is not None:
        _require("scale_motion" not in settings and "include_fingers" not in settings,
                 "普通FK目标不接受scale_motion/include_fingers；通用适配器不消费该设置")
    return value


def _validate_mapping(source, preset, mapping):
    if preset == "MIXAMO":
        if mapping is None:
            record = sources.mapping(source)
            overrides = {}
        elif isinstance(mapping, dict):
            if "bones" in mapping:
                _require(mapping.get("schema") in (None, "c94.retarget.mapping.v1"),
                         "Mixamo映射schema无效")
                overrides = dict(mapping["bones"])
            else:
                overrides = dict(mapping)
            _require(all(isinstance(key, str) for key in overrides),
                     "映射必须为语义到骨名的纯数据")
            record = sources.mapping(source, overrides)
        else:
            raise JobError("mapping必须为纯数据对象或None")
        return record, overrides
    exact = sources.exact_cmu_mapping(source)
    if mapping is not None:
        _require(isinstance(mapping, dict), "CMU mapping必须为纯数据对象或None")
        provided = mapping.get("bones", mapping)
        _require(provided == exact["bones"],
                 "CMU预设只接受已核精确映射，不能注入自定义映射")
    bvh.source_context(source)
    return exact, {}


def _resolve_items(source, target, raw_items, preset, job_id):
    if raw_items is None or raw_items == []:
        raw_items = [{}]
    elif isinstance(raw_items, dict):
        raw_items = [raw_items]
    _require(isinstance(raw_items, (list, tuple)), "items必须为对象数组")
    _require(1 <= len(raw_items) <= 64, "items必须为1..64项")
    source_binding = _binding_descriptor(source)
    current_action = source.animation_data.action if source.animation_data else None
    current_slot = source.animation_data.action_slot if source.animation_data else None
    if current_slot is not None and current_slot not in current_action.slots[:]:
        current_slot = None
    items = []
    selected_actions = []
    for index, raw in enumerate(raw_items):
        errors = []
        item = {
            "index": index,
            "id": f"item-{index:03d}",
            "state": "not_started",
            "action_name": None,
            "action_signature": None,
            "slot_identifier": None,
            "slot_handle": None,
            "fps": None,
            "clip_range": None,
            "start": None,
            "end": None,
            "output_start": 1,
            "output_action_name": None,
            "label": None,
            "validation_error": None,
        }
        try:
            if not isinstance(raw, dict):
                raise JobError("item必须为对象", code="INVALID_ITEM")
            unknown = sorted(set(raw) - _ITEM_FIELDS)
            if unknown:
                errors.append("item含未授权字段：" + ", ".join(unknown))
            action_value = raw.get("action", raw.get("source_action"))
            if action_value is None:
                action = current_action
                if action is None:
                    raise JobError("当前源骨架没有活动Action，且item未指定Action",
                                   code="INVALID_ITEM")
            elif isinstance(action_value, bpy.types.Action):
                action = action_value
            elif isinstance(action_value, str):
                action = _find_action_by_name(action_value)
            else:
                raise JobError("action必须为Action或名称字符串", code="INVALID_ITEM")
            if action.library is not None:
                raise JobError("当前入口只接受本地源Action", code="INVALID_ITEM")
            if action.name not in bpy.data.actions:
                raise JobError("源Action不属于当前文件", code="INVALID_ITEM")
            item["action_name"] = action.name
            slot_value = raw.get("slot", raw.get("slot_identifier"))
            if slot_value is None:
                if action == current_action and current_slot is not None:
                    slot = current_slot
                else:
                    object_slots = [slot for slot in action.slots
                                    if slot.target_id_type == "OBJECT"]
                    if len(object_slots) != 1:
                        raise JobError("多Slot Action必须显式指定slot",
                                       code="INVALID_ITEM")
                    slot = object_slots[0]
            else:
                slot = _find_slot(action, slot_value)
            _require(slot in action.slots[:] and slot.target_id_type == "OBJECT",
                     "源Action Slot身份无效", code="INVALID_ITEM")
            item["slot_identifier"] = slot.identifier
            item["slot_handle"] = int(getattr(slot, "handle", 0))
            fps_value = raw.get("fps", raw.get("source_fps"))
            if fps_value is not None:
                _finite_number(fps_value, "item fps必须为有限数")
                if not 0 < float(fps_value) <= 1000:
                    raise JobError("item fps必须在0..1000", code="INVALID_ITEM")
            clip = _clip_record(action, slot, fps_value)
            item["fps"] = clip["fps"]
            item["clip_range"] = [clip["range"][0], clip["range"][1]]
            item["action_signature"] = calibration.action_signature(action)
            start = raw.get("start", clip["range"][0])
            end = raw.get("end", clip["range"][1])
            start = _finite_number(start, "item start必须为有限数")
            end = _finite_number(end, "item end必须为有限数")
            if not (clip["range"][0] <= start < end <= clip["range"][1]):
                raise JobError("片段须在源Action帧域内且时长为正", code="INVALID_ITEM")
            item["start"] = start
            item["end"] = end
            output_start = raw.get("output_start", 1)
            if type(output_start) is not int:
                raise JobError("output_start必须为整数", code="INVALID_ITEM")
            item["output_start"] = output_start
            output_name = raw.get("action_name", raw.get("output_name",
                                                          raw.get("output_action_name")))
            if output_name is not None and (not isinstance(output_name, str) or not output_name.strip()):
                raise JobError("输出Action名称必须为非空字符串", code="INVALID_ITEM")
            item["output_action_name"] = output_name or (
                f"{target.name}_后台_{job_id[:8]}_{index:03d}")
            label = raw.get("label")
            if label is not None and not isinstance(label, str):
                raise JobError("label必须为字符串", code="INVALID_ITEM")
            item["label"] = label
            selected_actions.append(action)
        except JobError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            item["validation_error"] = {
                "code": "INVALID_ITEM",
                "message": "；".join(errors),
            }
        items.append(item)
    return items, selected_actions


def _validate_objects(source, target, *, same_correction_target=False,
                      target_policy=None):
    sources.armature(source)
    sources.armature(target)
    _require(source != target or same_correction_target, "源和目标不能是同一骨架")
    for obj in (source, target):
        _require(not obj.library and not obj.override_library,
                 "当前入口只支持当前Scene本地骨架", code="UNSUPPORTED_SCOPE")
        _require(not obj.data.library and not obj.data.override_library,
                 "当前入口只支持当前Scene本地骨架数据", code="UNSUPPORTED_SCOPE")
        _require(obj.data.pose_position == "POSE", "源与目标骨架须使用姿态求值模式")
    _require(bpy.context.scene.render.frame_map_old == bpy.context.scene.render.frame_map_new,
             "当前转换不支持场景时间重映射")
    if target_policy is None:
        settings_bone = target.pose.bones.get("骨架设置")
        _require(settings_bone is not None, "目标骨架不是当前C94控制器目标；"
                 "普通FK目标必须显式提供target_policy，不能静默回退")
        for key in c94.MODES:
            _require(key in settings_bone.keys(), "目标骨架缺少C94模式开关：" + key)
    else:
        general_job.normalize_target_policy(target, target_policy)
        from . import retarget_generic_target as generic
        found = generic.conflicts(target)
        _require(not found, "普通FK目标含未适配控制图：" +
                 json.dumps(found[:12], ensure_ascii=False))
    rt.binding(source)
    rt.binding(target)


def _dependency_scope(scene, source, target):
    libraries = sorted({
        library.filepath for library in bpy.data.libraries if library.filepath
    })
    external_files = 0
    for collection_name in ("images", "sounds", "movieclips", "cache_files", "volumes", "fonts"):
        collection = getattr(bpy.data, collection_name, ())
        for item in collection:
            filepath = getattr(item, "filepath", "")
            if filepath and not getattr(item, "packed_file", None):
                external_files += 1
    local_objects = not any(
        obj.library or obj.override_library or (obj.data and
        (obj.data.library or obj.data.override_library))
        for obj in (source, target)
    )
    return {
        "source_target_local": local_objects,
        "external_libraries": libraries,
        "external_file_datablocks": external_files,
        "partial": bool(libraries or external_files),
        "scope": "current local humanoid controller conversion; external Library/cache closure is not claimed",
    }


def _scene_meta(scene):
    return {
        "name": scene.name,
        "session_uid": int(scene.session_uid),
        "pointer": int(scene.as_pointer()),
        "filepath": str(bpy.data.filepath),
        "frame": int(scene.frame_current),
        "subframe": _round(scene.frame_subframe),
        "range": [int(scene.frame_start), int(scene.frame_end)],
        "fps": int(scene.render.fps),
        "fps_base": _round(scene.render.fps_base),
        "frame_map": [int(scene.render.frame_map_old), int(scene.render.frame_map_new)],
        "unit": [
            scene.unit_settings.system,
            _round(scene.unit_settings.scale_length),
            scene.unit_settings.length_unit,
        ],
        "camera": scene.camera.name if scene.camera else None,
        "view_layer": bpy.context.view_layer.name,
        "selection": _selection_state(),
    }


def _package_relative(root, relative):
    _require(isinstance(relative, str) and relative and "\x00" not in relative,
             "包清单路径无效", code="PACKAGE_INVALID")
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    parts = pure.parts
    _require(not pure.is_absolute() and parts and
             all(part not in ("", ".", "..") for part in parts) and
             ":" not in parts[0],
             "包清单路径越界：" + str(relative), code="PACKAGE_INVALID")
    candidate = root / Path(*parts)
    _require(not candidate.is_symlink(), "包清单不接受符号链接：" + str(relative),
             code="PACKAGE_INVALID")
    path = candidate.resolve()
    _require(path != root and root in path.parents,
             "包清单路径越界：" + str(relative), code="PACKAGE_INVALID")
    return pure.as_posix(), path


def _package_manifest(package_root):
    root = Path(package_root).resolve()
    manifest_path = root / "exchange-runtime-manifest.json"
    _require(manifest_path.is_file(), "当前包缺少exchange-runtime-manifest.json",
             code="PACKAGE_INVALID")
    _require(not manifest_path.is_symlink() and manifest_path.resolve().parent == root,
             "包清单必须位于包根且不能是符号链接", code="PACKAGE_INVALID")
    manifest = _read_json(manifest_path)
    _require(isinstance(manifest, dict) and
             manifest.get("schema") == "c94.exchange-runtime.package.v1" and
             isinstance(manifest.get("files"), dict),
             "exchange-runtime-manifest.json schema无效", code="PACKAGE_INVALID")
    actual_files = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
            continue
        actual_files.add(relative.as_posix())
    entries = {}
    for relative, expected in manifest["files"].items():
        normalized, path = _package_relative(root, relative)
        _require(isinstance(expected, str) and len(expected) == 64,
                 "包清单哈希无效：" + normalized, code="PACKAGE_INVALID")
        _require(normalized not in entries,
                 "包清单规范化后路径重复：" + normalized, code="PACKAGE_INVALID")
        _require(path.is_file(), "包清单文件缺失：" + normalized, code="PACKAGE_INVALID")
        _require(normalized in actual_files,
                 "包清单文件不属于真实文件集合：" + normalized, code="PACKAGE_INVALID")
        actual = _sha256_file(path)
        _require(actual == expected, "包清单文件哈希不符：" + normalized,
                 code="PACKAGE_INVALID")
        entries[normalized] = expected
    _require("exchange-runtime-manifest.json" in actual_files,
             "包真实文件集合缺少清单自身", code="PACKAGE_INVALID")
    return manifest_path, entries, actual_files


def _copy_runtime(job_directory, package_root):
    package_root = Path(package_root).resolve()
    manifest_path, entries, actual_files = _package_manifest(package_root)
    runtime = job_directory / "runtime" / "rigging_workflow"
    runtime.mkdir(parents=True, exist_ok=False)
    copied = {}
    for relative in sorted(actual_files):
        path = package_root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink():
            raise JobError("包内符号链接不受支持：" + str(relative), code="PACKAGE_INVALID")
        destination = runtime.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        copied[relative] = _sha256_file(destination)
    for relative, expected in entries.items():
        destination = runtime.joinpath(*relative.split("/"))
        _require(destination.is_file(), "运行包缺少清单文件：" + relative,
                 code="PACKAGE_INVALID")
        _require(_sha256_file(destination) == expected,
                 "运行包清单文件哈希不符：" + relative, code="PACKAGE_INVALID")
    manifest_copy = runtime / manifest_path.name
    _require(manifest_copy.is_file(), "运行包缺少包清单", code="PACKAGE_INVALID")
    extra = {
        relative: digest for relative, digest in copied.items()
        if relative not in entries
    }
    runtime_file = job_directory / "package-runtime.json"
    _atomic_json(runtime_file, {
        "schema": "c94.retarget.job.package-runtime.v1",
        "source_package": str(package_root),
        "manifest": manifest_path.name,
        "manifest_sha256": _sha256_file(manifest_copy),
        "manifest_files": entries,
        "extra_files": extra,
        "actual_file_count": len(actual_files),
        "verified_manifest_entries": len(entries),
        "copied_files": len(copied),
    })
    return {
        "runtime_root": str(runtime),
        "manifest": manifest_path.name,
        "manifest_sha256": _sha256_file(manifest_copy),
        "entries": entries,
        "extra_files": extra,
        "copied_files": copied,
    }


def _copy_worker(job_directory, package_root):
    source = package_root / "retarget_job_worker.py"
    _require(source.is_file(), "当前包缺少retarget_job_worker.py",
             code="PACKAGE_INVALID")
    destination = job_directory / "worker.py"
    shutil.copyfile(source, destination)
    return {
        "file": "worker.py",
        "sha256": _sha256_file(destination),
        "source_sha256": _sha256_file(source),
        "bytes": destination.stat().st_size,
    }


def _snapshot_write(job_directory, scene, actions, witness_before):
    staged = job_directory / (".snapshot-" + uuid.uuid4().hex + ".part.blend")
    snapshot = job_directory / "snapshot.blend"
    _require(not snapshot.exists(), "快照目标已存在；拒绝覆盖", code="TARGET_EXISTS")
    identifiers = {scene}
    for action in actions:
        identifiers.add(action)
    try:
        bpy.data.libraries.write(
            str(staged), identifiers, path_remap="ABSOLUTE",
            fake_user=False, compress=False,
        )
        _require(staged.is_file() and staged.stat().st_size > 0,
                 "Blender未写出有效快照", code="SNAPSHOT_WRITE_FAILED")
        witness_after = _state_witness(
            scene, witness_before["_source"], witness_before["_target"],
            witness_before["target_uid"],
        )
        diff = _state_diff(
            {key: value for key, value in witness_before.items()
             if not key.startswith("_")},
            witness_after,
        )
        if diff:
            raise JobError(
                "快照写入改变了当前作者状态；未发布快照",
                code="SNAPSHOT_STATE_CHANGED",
                details={"changed": sorted(diff), "diff": diff, "staged": str(staged)},
            )
        _atomic_replace(staged, snapshot)
    except BaseException:
        if staged.exists():
            try:
                staged.unlink()
            except OSError:
                pass
        raise
    return snapshot, _sha256_file(snapshot), witness_after


def _item_request(item):
    return {
        key: item[key] for key in (
            "index", "id", "action_name", "action_signature", "slot_identifier",
            "slot_handle", "fps", "clip_range", "start", "end", "output_start",
            "output_action_name", "label", "validation_error", "mmd", "kind", "contact_correction", "contact_detection",
        ) if key in item
    }


def _initial_ledger(job_id, snapshot_revision, items):
    return {
        "schema": LEDGER_SCHEMA,
        "job_id": job_id,
        "snapshot_revision": snapshot_revision,
        "updated_at_unix": time.time(),
        "items": [
            {
                "index": item["index"],
                "id": item["id"],
                "state": "not_started",
                "action_name": item["action_name"],
                "output_action_name": item["output_action_name"],
                "output": None,
                "output_sha256": None,
                "action_signature": None,
                "error": item["validation_error"],
                **({"mmd": item["mmd"]} if "mmd" in item else {}),
                **({"kind": item["kind"]} if "kind" in item else {}),
                **({"contact_issue_id": item["contact_correction"]["issue_id"]} if item.get("kind") == "CONTACT_CORRECTION" else {}),
                **({"detection_input_sha256": _sha256_json(item["contact_detection"])} if item.get("kind") == "CONTACT_DETECTION" else {}),
            }
            for item in items
        ],
    }


def _counts(items):
    counts = {state: 0 for state in _STATES}
    for item in items:
        state = item.get("state", "not_started")
        counts[state] = counts.get(state, 0) + 1
    counts["total"] = len(items)
    return counts


def _public_item(item):
    value = {
        key: item.get(key) for key in (
            "index", "id", "state", "action_name", "output_action_name",
            "output", "output_sha256", "action_signature", "error",
            "started_at_unix", "finished_at_unix", "seconds",
            "target_fingerprint_sha256", "snapshot_revision", "mmd", "kind", "contact_issue_id", "artifact_type", "report_status", "issue_count",
        )
        if key in item
    }
    stats = item.get("stats")
    if isinstance(stats, dict):
        value["stats"] = {
            key: stats.get(key) for key in (
                "fcurve_count", "key_count", "unique_key_times",
                "key_time_min", "key_time_max", "slots",
            )
        }
    report = item.get("report")
    if isinstance(report, dict):
        value["report_summary"] = {
            key: report.get(key) for key in (
                "id", "status", "source", "source_action", "source_slot",
                "target", "target_uid", "source_fps", "source_range",
                "target_fps", "output_range", "samples", "sparse_preview",
                "rotation_residual_max_degrees",
            )
        }
        direction = report.get("bone_direction_residual")
        if isinstance(direction, dict):
            value["report_summary"]["bone_direction_residual"] = {
                key: direction.get(key) for key in (
                    "status", "max_degrees", "observation_rows", "total_observation_rows",
                )
            }
    return value


def _launch_worker(job_directory, settings):
    worker = job_directory / "worker.py"
    user = job_directory / "user"
    for name in ("config", "scripts", "extensions", "datafiles"):
        (user / name).mkdir(parents=True, exist_ok=True)
    temporary = job_directory / "tmp"
    temporary.mkdir(exist_ok=True)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("BLENDER_") and key not in (
            "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
        )
    }
    for name in ("CONFIG", "SCRIPTS", "EXTENSIONS", "DATAFILES"):
        environment["BLENDER_USER_" + name] = str(user / name.lower())
    environment.update({
        "BLENDER_USER_RESOURCES": str(user),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "TMPDIR": str(temporary),
    })
    command = [
        bpy.app.binary_path,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--offline-mode",
        "--threads", str(settings["threads"]),
        "--python-exit-code", "23",
        "--python", str(worker),
        "--", str(job_directory),
    ]
    _atomic_json(job_directory / "command.json", command)
    log_path = job_directory / "worker.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command, cwd=str(job_directory), env=environment,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return process, command


def _cleanup_uid(target, before, after):
    if before == after:
        return
    try:
        if before is None:
            if rt.TARGET_UID in target:
                del target[rt.TARGET_UID]
        else:
            target[rt.TARGET_UID] = before
    except (ReferenceError, KeyError):
        pass


def _watchdog(job):
    while not job._watch_stop.wait(0.25):
        process = job._process
        if process is None or process.poll() is not None:
            break
        now = time.monotonic()
        reason = None
        if job._cancel_requested and now >= job._cancel_terminate_at:
            reason = "cancel_grace"
        elif now >= job._hard_deadline:
            reason = "deadline"
        if reason and not job._terminate_requested:
            job._terminate_requested = True
            job._forced_reason = reason
            try:
                process.terminate()
                _atomic_json(job.directory / "watchdog.json", {
                    "schema": "c94.retarget.job.watchdog.v1",
                    "job_id": job.id,
                    "reason": reason,
                    "at_unix": time.time(),
                    "pid": process.pid,
                })
            except Exception:
                pass
    job._watch_stop.set()


class Job:
    def __init__(self, *, job_id, directory, process, settings, snapshot_revision,
                 snapshot_meta, request, items, target_uid):
        self.id = job_id
        self.directory = Path(directory)
        self._process = process
        self.settings = settings
        self.snapshot_revision = snapshot_revision
        self.snapshot_meta = snapshot_meta
        self.request = request
        self.items = items
        self.target_uid = target_uid
        self.created_at = time.time()
        self._cancel_requested = False
        self._cancel_requested_at = None
        self._cancel_terminate_at = None
        self._terminate_requested = False
        self._forced_reason = None
        self._watch_stop = threading.Event()
        self._hard_deadline = (
            time.monotonic() + float(settings["max_seconds"]) +
            float(settings["startup_grace_seconds"]) + 5.0
        )
        self._watchdog = threading.Thread(
            target=_watchdog, args=(self,), daemon=True,
            name="c94-retarget-job-" + job_id[:8],
        )
        self._watchdog.start()

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def _ledger(self):
        return _read_json(self.directory / "ledger.json")

    def _result(self):
        return _read_json(self.directory / "result.json")

    def _progress(self):
        path = self.directory / "progress.jsonl"
        if not path.is_file():
            return None
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 4096))
                lines = stream.read().splitlines()
            for line in reversed(lines):
                if line:
                    try:
                        return json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
        except OSError:
            return None
        return None

    def _payload(self, *, peek=False):
        exit_code = self._process.poll() if self._process is not None else None
        ledger = self._ledger()
        result = self._result()
        if isinstance(ledger, dict):
            items = ledger.get("items", [])
        else:
            items = []
        counts = _counts(items)
        public_items = [_public_item(item) for item in items]
        status = "running" if exit_code is None else "exited"
        error = None
        terminal = False
        if exit_code is None:
            status = "cancelling" if self._cancel_requested else "running"
        else:
            if isinstance(result, dict) and result.get("job_id") == self.id and \
                    result.get("status") in ("succeeded", "partial", "failed", "cancelled"):
                status = result["status"]
                terminal = True
                error = result.get("error")
                if status == "succeeded" and (items and counts["succeeded"] != len(items)):
                    status = "failed"
                    error = {"code": "LEDGER_MISMATCH",
                             "message": "worker结果与ledger成功数不一致"}
            elif self._cancel_requested:
                status = "cancelled"
                terminal = True
                if self._forced_reason == "cancel_grace":
                    error = {"code": "CANCEL_GRACE_EXPIRED",
                             "message": "取消宽限到期，已终止本job自有进程"}
            elif self._forced_reason == "deadline":
                status = "partial" if counts["succeeded"] else "failed"
                terminal = True
                error = {"code": "JOB_DEADLINE",
                         "message": "后台job超过配置的运行预算"}
            else:
                status = "failed"
                terminal = True
                error = {"code": "WORKER_EXIT",
                         "message": "后台worker未正常完成；请查看worker.log"}
        if not isinstance(ledger, dict):
            status = "failed" if exit_code is not None else status
            terminal = exit_code is not None
            error = error or {"code": "LEDGER_INVALID",
                              "message": "ledger缺失或损坏"}
        data = {
            "schema": JOB_SCHEMA,
            "job_id": self.id,
            "job_directory": str(self.directory),
            "snapshot_revision": self.snapshot_revision,
            "status": status,
            "terminal": terminal and exit_code is not None,
            "cancel_requested": self._cancel_requested,
            "forced_reason": self._forced_reason,
            "error": error,
            "counts": counts,
            "items": public_items,
            "progress": self._progress(),
            "process": {
                "pid": self.pid,
                "exit_code": exit_code,
            },
            "settings": self.settings,
            "deadline": {
                "max_seconds": self.settings["max_seconds"],
                "startup_grace_seconds": self.settings["startup_grace_seconds"],
                "cancel_grace_seconds": self.settings["cancel_grace_seconds"],
            },
        }
        if isinstance(result, dict):
            data["result"] = {
                key: value for key, value in result.items()
                if key != "items"
            }
            data["result"]["items"] = [
                _public_item(item) for item in result.get("items", [])
            ]
        return data

    def status(self):
        return self._payload(peek=False)

    def peek_status(self):
        return self._payload(peek=True)

    def cancel(self):
        if self._process is None or self._process.poll() is not None:
            return self.status()
        if not self._cancel_requested:
            self._cancel_requested = True
            self._cancel_requested_at = time.monotonic()
            self._cancel_terminate_at = (
                self._cancel_requested_at + float(self.settings["cancel_grace_seconds"])
            )
            try:
                _atomic_json(self.directory / "cancel.request", {
                    "schema": "c94.retarget.job.cancel.v1",
                    "job_id": self.id,
                    "requested_at_unix": time.time(),
                })
            except OSError:
                # The owned process still gets the bounded grace; the watchdog
                # terminates it if the sentinel cannot be written.
                self._cancel_terminate_at = time.monotonic() + 1.0
        return {
            "schema": JOB_SCHEMA,
            "job_id": self.id,
            "job_directory": str(self.directory),
            "status": "cancelling",
            "cancel_requested": True,
            "terminal": False,
        }

    def collect_successes(self, items=None):
        return collect_successes(self, items=items)


def _enqueue_internal(source, target, items, preset, mapping, settings,
                      jobs_root, source_profile=None, target_policy=None):
    _require(isinstance(jobs_root, (str, os.PathLike)), "jobs_root必须为明确绝对路径")
    root = Path(jobs_root).expanduser()
    _require(root.is_absolute(), "jobs_root必须为明确绝对路径")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    _require(root.is_dir(), "jobs_root不是目录")
    _require(preset in ("MIXAMO", "CMU", "MMD", "CONTACT_CORRECTION", "CONTACT_DETECTION",
                        general_job.PRESET_PROFILE), "未知后台预设")
    _require(preset != "CONTACT_CORRECTION" or source == target, "足底修正仅处理当前同一明确目标")
    if isinstance(target_policy, str):
        try:
            target_policy = json.loads(target_policy)
        except ValueError as exc:
            raise JobError("target_policy必须为纯JSON对象",
                           code="INVALID_REQUEST") from exc
    _require(not (preset == general_job.PRESET_PROFILE and target_policy is not None),
             "PROFILE与普通FK target_policy组合尚未验证；已拒绝，不能静默换求解")
    _validate_objects(
        source, target, same_correction_target=preset in ("CONTACT_CORRECTION", "CONTACT_DETECTION"),
        target_policy=target_policy,
    )
    normalized_settings = _validate_settings(
        preset, settings, target_policy=target_policy)
    profile = None
    profile_validation = None
    if preset in ("CONTACT_CORRECTION", "CONTACT_DETECTION"):
        _require(mapping is None and source_profile is None and target_policy is None,
                 "足底问题不接受来源映射/profile/target_policy")
        mapping_record, mapping_overrides = {"kind":preset}, {}
    elif preset == "MMD":
        _require(target_policy is None, "MMD预设不接普通FK target_policy")
        from . import retarget_mmd_job as mmd_job
        source_profile = mmd_job.prepare_profile(source, source_profile, mapping)
        mapping_record, mapping_overrides = source_profile["mapping"], {}
    elif preset == general_job.PRESET_PROFILE:
        _require(source_profile is not None,
                 "PROFILE必须携带纯JSON source_profile")
        _require(mapping is None,
                 "PROFILE的来源映射由profile明确提供，不接受额外mapping")
        profile = general_job.normalize_source_profile(source_profile)
        profile_validation = general_job.validate_profile_job(source, target, profile)
        mapping_record, mapping_overrides = profile["mapping"], {}
    else:
        _require(source_profile is None, "source_profile仅用于PROFILE或MMD预设")
        mapping_record, mapping_overrides = _validate_mapping(source, preset, mapping)
        if target_policy is not None:
            general_job.normalize_target_policy(
                target, target_policy, source_mapping=mapping_record)
    target_mode = general_job.target_mode(preset, target_policy)
    target_policy_sha = (general_job.sha256_json(target_policy)
                         if target_policy is not None else None)
    source_profile_sha = (general_job.sha256_json(profile)
                          if profile is not None else None)
    source_runtime = _object_runtime(source, bpy.context.scene)
    uid_before = target.get(rt.TARGET_UID)
    uid = rt._target_uid(target)
    uid_created = uid != uid_before
    job_id = uuid.uuid4().hex
    process = None
    job_directory = None
    enqueue_phase = "resolve_items"
    try:
        if preset == "CONTACT_DETECTION":
            from . import retarget_contact_detection as contact_detection
            items, selected_actions = contact_detection.resolve_items(source, target, items, job_id)
        elif preset == "CONTACT_CORRECTION":
            from . import retarget_contact_job as contact_job
            items, selected_actions = contact_job.resolve_items(target, items, job_id)
        elif preset == "MMD":
            items, selected_actions = mmd_job.resolve_items(source, target, items, source_profile, job_id)
        else:
            items, selected_actions = _resolve_items(
                source, target, items, preset, job_id,
            )
        allocated_directory = root / job_id
        enqueue_phase = "create_directory"
        allocated_directory.mkdir(parents=False, exist_ok=False)
        job_directory = allocated_directory
        enqueue_phase = "snapshot_preflight"
        scene = bpy.context.scene
        target_runtime = _object_runtime(target, scene)
        target_fingerprint = _rig_static_fingerprint(target)
        source_fingerprint = _rig_static_fingerprint(source)
        target_compact = _compact_fingerprint(target_fingerprint)
        source_compact = _compact_fingerprint(source_fingerprint)
        dependency_scope = _dependency_scope(scene, source, target)
        witness = _state_witness(scene, source, target, uid)
        witness["_source"] = source
        witness["_target"] = target
        enqueue_phase = "snapshot_write"
        snapshot, snapshot_sha, witness_after = _snapshot_write(
            job_directory, scene, selected_actions, witness,
        )
        enqueue_phase = "runtime_copy"
        package = _copy_runtime(
            job_directory, Path(__file__).resolve().parent,
        )
        enqueue_phase = "worker_copy"
        worker = _copy_worker(
            job_directory, Path(__file__).resolve().parent,
        )
        item_request = [_item_request(item) for item in items]
        action_dependencies = []
        seen_actions = set()
        for action in selected_actions:
            pointer = action.as_pointer()
            if pointer in seen_actions:
                continue
            seen_actions.add(pointer)
            action_dependencies.append({
                "name": action.name,
                "signature": calibration.action_signature(action),
            })
        snapshot_descriptor = {
            "job_id": job_id,
            "preset": preset,
            "settings": normalized_settings,
            "mapping_sha256": _sha256_json(mapping_record),
            "mapping_overrides": mapping_overrides,
            "target": target_compact,
            "source": source_compact,
            "items": item_request,
            "actions": action_dependencies,
            "scene": {
                "fps": int(scene.render.fps),
                "fps_base": _round(scene.render.fps_base),
                "range": [int(scene.frame_start), int(scene.frame_end)],
                "unit": list(witness["unit"]),
            },
            "package": {
                "manifest_sha256": package["manifest_sha256"],
                "extra_files": package["extra_files"],
            },
        }
        snapshot_descriptor.update({
            "target_mode": target_mode,
            "target_policy_sha256": target_policy_sha,
            "source_profile_sha256": source_profile_sha,
            "source_profile_id": (profile or {}).get("id"),
            "source_profile_preset": (profile or {}).get("preset"),
        })
        if preset == "MMD":
            snapshot_descriptor["mmd_profile_sha256"] = source_profile["profile_sha256"]
        snapshot_revision = _sha256_json(snapshot_descriptor)
        snapshot_meta = {
            "schema": SNAPSHOT_SCHEMA,
            "job_id": job_id,
            "snapshot_revision": snapshot_revision,
            "snapshot_file": "snapshot.blend",
            "snapshot_sha256": snapshot_sha,
            "created_at_unix": time.time(),
            "preset": preset,
            "target_mode": target_mode,
            "target_policy": target_policy,
            "target_policy_sha256": target_policy_sha,
            "source_profile_sha256": source_profile_sha,
            "profile_validation": profile_validation,
            "settings": normalized_settings,
            "mapping": mapping_record,
            "mapping_overrides": mapping_overrides,
            "blender": {
                "version": list(bpy.app.version),
                "version_string": bpy.app.version_string,
                "binary": bpy.app.binary_path,
            },
            "scene": _scene_meta(scene),
            "source": {
                "name": source.name,
                "data_name": source.data.name,
                "runtime": source_runtime,
                "fingerprint": source_compact,
                "binding": witness["source_binding"],
            },
            "target": {
                "name": target.name,
                "data_name": target.data.name,
                "uid": uid,
                "uid_created": uid_created,
                "uid_before": uid_before,
                "runtime": target_runtime,
                "fingerprint": target_compact,
                "session_refs": target_fingerprint.get("session_refs", {}),
                "object_transform_kind": target_fingerprint.get(
                    "object_transform_kind"),
                "object_transform": target_fingerprint.get(
                    "object_transform"),
                "binding": witness["target_binding"],
            },
            "items": item_request,
            "actions": action_dependencies,
            "dependency_scope": dependency_scope,
            "state_guard": {
                "unchanged": True,
                "before_sha256": _sha256_json({
                    key: value for key, value in witness.items()
                    if not key.startswith("_") and key != "_source" and key != "_target"
                }),
                "after_sha256": _sha256_json(witness_after),
                "target_uid_created": uid_created,
                "target_uid_before": uid_before,
                "target_uid_after": uid,
                "dirty": witness["dirty"],
                "frame": witness["scene_frame"],
                "selection": witness["selection"],
                "source_pose_sha256": witness["source_pose_sha256"],
                "target_pose_sha256": witness["target_pose_sha256"],
                "source_static_sha256": witness["source_static_sha256"],
                "target_static_sha256": witness["target_static_sha256"],
                "source_action_sha256": witness["source_action_sha256"],
                "target_action_sha256": witness["target_action_sha256"],
                "weights": witness["weights"],
            },
        }
        if preset == "MMD":
            snapshot_meta["source_profile"] = source_profile
        elif preset == general_job.PRESET_PROFILE:
            snapshot_meta["source_profile"] = profile
        enqueue_phase = "snapshot_metadata_write"
        _atomic_json(job_directory / "snapshot-meta.json", snapshot_meta)
        snapshot_meta_sha = _sha256_file(job_directory / "snapshot-meta.json")
        request = {
            "schema": REQUEST_SCHEMA,
            "job_id": job_id,
            "created_at_unix": time.time(),
            "preset": preset,
            "target_mode": target_mode,
            "target_policy": target_policy,
            "target_policy_sha256": target_policy_sha,
            "source_profile_sha256": source_profile_sha,
            "profile_validation": profile_validation,
            "settings": normalized_settings,
            "mapping": mapping_record,
            "mapping_overrides": mapping_overrides,
            "snapshot": {
                "file": "snapshot.blend",
                "sha256": snapshot_sha,
                "meta": "snapshot-meta.json",
                "meta_sha256": snapshot_meta_sha,
                "revision": snapshot_revision,
            },
            "package": {
                "manifest": package["manifest"],
                "manifest_sha256": package["manifest_sha256"],
                "extra_files": package["extra_files"],
            },
            "worker": worker,
            "items": item_request,
            "source": snapshot_meta["source"],
            "target": snapshot_meta["target"],
            "scene": snapshot_meta["scene"],
            "snapshot_meta": snapshot_meta,
            "deadline": {
                "max_seconds": normalized_settings["max_seconds"],
            },
        }
        if preset == "MMD":
            request["source_profile"] = source_profile
        elif preset == general_job.PRESET_PROFILE:
            request["source_profile"] = profile
        ledger = _initial_ledger(job_id, snapshot_revision, items)
        enqueue_phase = "ledger_write"
        _atomic_json(job_directory / "ledger.json", ledger)
        enqueue_phase = "request_write"
        _atomic_json(job_directory / "request.json", request)
        enqueue_phase = "worker_launch"
        process, command = _launch_worker(job_directory, normalized_settings)
        enqueue_phase = "job_handle"
        job = Job(
            job_id=job_id, directory=job_directory, process=process,
            settings=normalized_settings, snapshot_revision=snapshot_revision,
            snapshot_meta=snapshot_meta, request=request, items=items,
            target_uid=uid,
        )
        return job
    except BaseException as exc:
        cleanup_error = None
        terminate_requested = False
        if process is not None and process.poll() is None:
            try:
                terminate_requested = True
                process.terminate()
            except OSError as cleanup_exc:
                cleanup_error = str(cleanup_exc)
        _cleanup_uid(target, uid_before, uid)
        if job_directory is not None:
            # Keep owned snapshots and partial runtime files for recovery. A
            # dedicated receipt never replaces request/ledger or old evidence.
            # Its write may fail under ENOSPC; the exception still carries the
            # exact allocated directory and original cause in that case.
            directory = str(job_directory)
            failure = {
                "schema": "c94.retarget.job.enqueue_failure.v1",
                "job_id": job_id, "job_directory": directory,
                "status": "enqueue_failed", "phase": enqueue_phase,
                "error": {"type": type(exc).__name__, "message": str(exc),
                          "code": getattr(exc, "code", None)},
                "process_started": process is not None,
                "owned_process_terminate_requested": terminate_requested,
                "cleanup_error": cleanup_error,
                "retained_outputs": True,
            }
            diagnostic = job_directory / ("enqueue-failure-" + uuid.uuid4().hex + ".json")
            try:
                _atomic_json(diagnostic, failure)
                failure["diagnostic_file"] = str(diagnostic)
            except Exception as diagnostic_exc:
                failure["diagnostic_file"] = None
                failure["diagnostic_write_error"] = str(diagnostic_exc)
            raise JobError(
                "后台提交在" + enqueue_phase + "阶段失败；已保留本任务目录：" +
                directory + "；原因：" + str(exc),
                code="ENQUEUE_FAILED", details=failure,
            ) from exc
        raise


def enqueue(source, target, items=None, preset="MIXAMO", mapping=None,
            settings=None, jobs_root=None, source_profile=None,
            target_policy=None):
    """Capture the current Scene and start one owned background batch job.

    ``items`` entries accept ``action``/``source_action`` (name or Action),
    ``slot``/``slot_identifier``, ``fps``/``source_fps``, ``start``, ``end``,
    ``output_start`` and ``action_name``/``output_name`` (the independent
    output Action name).  An omitted/empty item uses the current source
    Action/Slot.  Up to 64 items are accepted.
    """
    return _enqueue_internal(
        source, target, items, preset, mapping, settings, jobs_root,
        source_profile=source_profile, target_policy=target_policy,
    )


def enqueue_contact_corrections(target, items, *, settings=None, jobs_root=None):
    """Capture one explicit base target; existing queue generates review-only Actions."""
    return enqueue(target, target, items=items, preset="CONTACT_CORRECTION",
                   settings=settings, jobs_root=jobs_root)


def enqueue_contact_detection(source, target, items, *, settings=None, jobs_root=None):
    """Snapshot explicit source/target Actions and calibration; report JSON only."""
    return enqueue(source, target, items=items, preset="CONTACT_DETECTION",
                   settings=settings, jobs_root=jobs_root)


def status(job):
    _require(isinstance(job, Job), "status需要enqueue返回的Job")
    return job.status()


def peek_status(job):
    _require(isinstance(job, Job), "peek_status需要enqueue返回的Job")
    return job.peek_status()


def cancel(job):
    _require(isinstance(job, Job), "cancel需要enqueue返回的Job")
    return job.cancel()


def _collect_context(job):
    if isinstance(job, Job):
        return {
            "directory": job.directory,
            "job_id": job.id,
            "snapshot_meta": job.snapshot_meta,
        }
    directory = Path(job)
    meta = _read_json(directory / "snapshot-meta.json")
    _require(isinstance(meta, dict) and meta.get("schema") == SNAPSHOT_SCHEMA,
             "collect需要Job或有效job目录")
    return {
        "directory": directory,
        "job_id": meta["job_id"],
        "snapshot_meta": meta,
    }


def _target_identity_error(expected, current, target):
    keys = (
        "static_sha256", "rest_sha256", "rotation_modes_sha256",
        "constraints_sha256", "drivers_sha256", "controls_sha256",
        "scene_sha256", "bone_count", "pose_position", "target_uid",
    )
    changed = [key for key in keys if expected.get(key) != current.get(key)]
    if expected.get("matrix_world") != current.get("matrix_world"):
        changed.append("matrix_world")
    if expected.get("matrix_parent_inverse") != current.get("matrix_parent_inverse"):
        changed.append("matrix_parent_inverse")
    raise JobError(
        "目标身份已改变；拒绝把结果归入当前目标",
        code="TARGET_IDENTITY_CHANGED",
        details={
            "changed": changed,
            "target": target.name,
            "target_uid": target.get(rt.TARGET_UID),
            "expected_static_sha256": expected.get("static_sha256"),
            "current_static_sha256": current.get("static_sha256"),
        },
    )


def _validate_object_transform(target, current, expected, captured, scene_state):
    """One transform witness check for collect and public preview."""
    scene = bpy.context.scene
    captured_frame = int(scene_state["frame"])
    captured_subframe = _round(scene_state.get("subframe", 0.0))
    current_frame = [int(scene.frame_current), _round(scene.frame_subframe)]
    frame_matches = current_frame == [captured_frame, captured_subframe]
    transform_kind = captured.get("object_transform_kind")
    if transform_kind is None:
        transform_kind = expected.get("object_transform_kind")
    if transform_kind is None:
        transform_kind = "UNSUPPORTED" if expected.get(
            "matrix_world_dynamic") else "NONE"
    if not frame_matches and transform_kind == "ACTION":
        expected_transform = _expected_action_object_transform(
            target, current_frame[0] + current_frame[1],
            captured.get("object_transform"),
        )
        if current.get("object_transform") != expected_transform:
            raise JobError(
                "目标对象变换与捕获Action在当前帧不一致；拒绝collect",
                code="TARGET_IDENTITY_CHANGED",
                details={
                    "changed": ["matrix_world"],
                    "reason": "object_transform_mismatch",
                    "target": target.name,
                    "target_uid": target.get(rt.TARGET_UID),
                    "captured_frame": captured_frame,
                    "current_frame": current_frame,
                    "expected_transform": expected_transform,
                    "current_transform": current.get("object_transform"),
                },
            )
    elif not frame_matches and transform_kind == "UNSUPPORTED":
        raise JobError(
            "目标对象变换为driver/NLA/约束/父级动态，当前collect范围无法可靠跨帧核验；拒绝",
            code="TARGET_IDENTITY_CHANGED",
            details={
                "changed": ["matrix_world"],
                "reason": "dynamic_object_transform_unsupported",
                "target": target.name,
                "target_uid": target.get(rt.TARGET_UID),
                "captured_frame": captured_frame,
                "current_frame": current_frame,
                "object_transform_kind": transform_kind,
            },
        )
    if transform_kind == "NONE" or frame_matches:
        # matrix_world can still reflect the previous dependency-graph state.
        # Check captured raw channels before evaluating or changing anything.
        captured_raw = captured.get("object_transform")
        if captured_raw is not None and current.get("object_transform") != captured_raw:
            raise JobError(
                "目标对象原始变换已改变；请保留当前工作并重新转换",
                code="TARGET_IDENTITY_CHANGED",
                details={"changed": ["object_transform"],
                         "reason": "object_transform_mismatch",
                         "target": target.name,
                         "frame_matches": frame_matches},
            )
        if current["matrix_world"] != expected.get("matrix_world"):
            raise JobError(
                "目标世界变换已改变；拒绝把结果归入当前目标",
                code="TARGET_IDENTITY_CHANGED",
                details={
                    "changed": ["matrix_world"],
                    "target": target.name,
                    "target_uid": target.get(rt.TARGET_UID),
                    "matrix_world_dynamic": expected.get("matrix_world_dynamic"),
                    "object_transform_kind": transform_kind,
                    "frame_matches": frame_matches,
                },
            )


def _find_collect_target(snapshot_meta):
    captured = snapshot_meta["target"]
    runtime = captured["runtime"]
    scene = bpy.context.scene
    if int(scene.session_uid) != int(runtime["scene_session_uid"]) or \
            int(scene.as_pointer()) != int(runtime["scene_pointer"]):
        raise JobError(
            "当前Scene与快照捕获Scene不同；拒绝collect",
            code="TARGET_SCENE_CHANGED",
            details={
                "expected_scene_session_uid": runtime["scene_session_uid"],
                "current_scene_session_uid": int(scene.session_uid),
            },
        )
    uid = captured["uid"]
    matches = [
        obj for obj in scene.objects
        if obj.type == "ARMATURE" and obj.get(rt.TARGET_UID) == uid
    ]
    if len(matches) != 1:
        raise JobError(
            "目标已删除或UID不唯一；拒绝collect",
            code="TARGET_NOT_FOUND",
            details={"target_uid": uid, "matches": len(matches)},
        )
    target = matches[0]
    if int(target.session_uid) != int(runtime["session_uid"]) or \
            int(target.data.as_pointer()) != int(runtime["data_pointer"]):
        raise JobError(
            "当前目标对象/数据身份与快照不同；拒绝collect",
            code="TARGET_REPLACED",
            details={
                "expected_object_session_uid": runtime["session_uid"],
                "current_object_session_uid": int(target.session_uid),
                "expected_data_pointer": runtime["data_pointer"],
                "current_data_pointer": int(target.data.as_pointer()),
            },
        )
    current = _rig_static_fingerprint(target)
    expected = captured["fingerprint"]
    if current["static_sha256"] != expected["static_sha256"]:
        _target_identity_error(expected, _compact_fingerprint(current), target)
    expected_refs = captured.get("session_refs") or {}
    current_refs = current.get("session_refs") or {}
    changed_refs = []
    for key, expected_ref in expected_refs.items():
        current_ref = current_refs.get(key)
        if not isinstance(current_ref, dict) or any(
                current_ref.get(field) != expected_ref.get(field)
                for field in (
                    "object_session_uid", "object_pointer", "data_pointer",
                )):
            changed_refs.append(key)
    if changed_refs:
        raise JobError(
            "目标约束/驱动引用的外部对象已替换；拒绝collect",
            code="TARGET_REFERENCE_CHANGED",
            details={
                "changed": changed_refs,
                "target": target.name,
                "target_uid": target.get(rt.TARGET_UID),
            },
        )
    _validate_object_transform(
        target, current, expected, captured, snapshot_meta["scene"])
    return target, current


def validate_candidate_target(target, action):
    """Validate an appended background candidate before any binding mutation.

    This persistent check uses the captured graph and object-transform witness.
    Runtime Scene/object-pointer ownership remains a separate collect boundary.
    """
    _require(isinstance(action, bpy.types.Action), "请选择独立候选动作")
    report = json.loads(action.get(rt.META, "{}"))
    marker = report.get("background_job")
    if "c94_retarget_contact_correction_v1" in action:
        raw = action.get(JOB_KEY)
        own_marker = json.loads(raw) if isinstance(raw, str) else None
        if isinstance(own_marker, dict) and own_marker.get("kind") == "CONTACT_CORRECTION":
            marker = own_marker
    if marker is None:
        return True
    _require(isinstance(marker, dict), "后台候选身份记录损坏")
    _require(isinstance(target, bpy.types.Object) and target.type == "ARMATURE",
             "请选择目标骨架")
    uid = marker.get("target_uid")
    _require(uid and target.get(rt.TARGET_UID) == uid,
             "后台候选属于其他目标", code="TARGET_NOT_FOUND")
    _require(sum(obj.get(rt.TARGET_UID) == uid
                 for obj in bpy.context.scene.objects) == 1,
             "目标标识被副本共享", code="TARGET_NOT_FOUND")
    expected = marker.get("target_fingerprint") or marker.get("guarded_target_fingerprint")
    _require(isinstance(expected, dict) and expected.get("static_sha256"),
             "后台候选缺少目标结构记录，请重新生成")
    current = _rig_static_fingerprint(target)
    if current["static_sha256"] != expected["static_sha256"]:
        _target_identity_error(expected, _compact_fingerprint(current), target)
    captured_frame = expected.get("matrix_world_frame")
    _require(isinstance(captured_frame, list) and len(captured_frame) == 2,
             "后台候选缺少捕获时刻，请重新生成")
    _validate_object_transform(target, current, expected, expected,
                              {"frame": captured_frame[0], "subframe": captured_frame[1]})
    general_job.validate_candidate_policy(target, action, marker, report)
    return True


def _source_live_state(snapshot_meta):
    source_meta = snapshot_meta["source"]
    source = bpy.context.scene.objects.get(source_meta["name"])
    if source is None or source.type != "ARMATURE":
        return {"available": False, "live_matches_snapshot": None}
    current_static = _rig_static_fingerprint(source)["static_sha256"]
    action_rows = []
    changed = current_static != source_meta["fingerprint"]["static_sha256"]
    for item in snapshot_meta["items"]:
        if not isinstance(item.get("action_name"), str):
            action_rows.append({"item": item["id"], "available": False,
                                "reason": "item_has_no_validated_source_action"})
            continue
        action = bpy.data.actions.get(item["action_name"])
        if action is None:
            action_rows.append({"item": item["id"], "available": False})
            changed = True
            continue
        current = calibration.action_signature(action)
        same = current == item["action_signature"]
        changed = changed or not same
        action_rows.append({
            "item": item["id"],
            "available": True,
            "matches_snapshot": same,
        })
    profile = snapshot_meta.get("source_profile")
    profile_state = None
    mode = snapshot_meta.get("target_mode")
    if profile is not None and (
            mode == general_job.TARGET_MODE_MMD or
            (mode is None and snapshot_meta.get("preset") == "MMD")):
        from . import retarget_mmd_job as mmd_job
        profile_state = mmd_job.live_source_state(source, profile)
        changed = changed or not profile_state["matches_snapshot"]
    elif profile is not None:
        profile_state = {
            "schema": "c94.retarget.profile.source_state.v1",
            "available": True,
            "matches_snapshot": None,
            "source_profile_sha256": snapshot_meta.get(
                "source_profile_sha256"),
            "edits_included": False,
            "note": "source profile is validated at enqueue/worker; "
                    "live revalidation is not claimed here",
        }
    return {
        "available": True,
        "live_matches_snapshot": not changed,
        "static_matches_snapshot": current_static == source_meta["fingerprint"]["static_sha256"],
        "actions": action_rows,
        "registered_mmd_source": profile_state,
        "edits_included": False,
    }


def _ledger_items(ledger):
    if not isinstance(ledger, dict) or ledger.get("schema") != LEDGER_SCHEMA or \
            not isinstance(ledger.get("items"), list):
        raise JobError("job ledger缺失或损坏", code="LEDGER_INVALID")
    return ledger["items"]


def _validate_output(job_directory, item):
    output = item.get("output")
    _require(isinstance(output, str) and output, "成功项缺少输出路径",
             code="OUTPUT_INVALID")
    relative = Path(output)
    _require(not relative.is_absolute() and ".." not in relative.parts,
             "输出路径逃逸job目录", code="OUTPUT_INVALID")
    path = (job_directory / relative).resolve()
    root = job_directory.resolve()
    _require(root == path.parent or root in path.parents,
             "输出路径逃逸job目录", code="OUTPUT_INVALID")
    _require(path.is_file() and not path.is_symlink(), "输出文件缺失",
             code="OUTPUT_MISSING")
    actual = _sha256_file(path)
    _require(actual == item.get("output_sha256"),
             "输出文件SHA不匹配；拒绝损坏结果", code="OUTPUT_TAMPERED")
    if item.get("kind") == "CONTACT_DETECTION":
        from . import retarget_contact_detection as contact_detection
        _require(path.suffix.lower() == ".json", "只读检测产物必须是JSON报告", code="OUTPUT_INVALID")
        contact_detection.validate_report(_read_json(path), item=item)
        return path, None
    try:
        with bpy.data.libraries.load(str(path), link=False) as (source, destination):
            names = list(source.actions)
            if item.get("kind") == "CONTACT_CORRECTION":
                from . import retarget_contact_job as contact_job
                contact_job.action_only_inventory(source)
            elif item.get("mmd") is not None:
                from . import retarget_mmd_job as mmd_job
                mmd_job.action_only_inventory(source)
    except Exception as exc:
        raise JobError("输出Action文件无法读取", code="OUTPUT_INVALID") from exc
    _require(len(names) == 1, "输出文件必须只含一个独立Action",
             code="OUTPUT_INVALID")
    return path, names[0]


def _existing_marker(job_id, item_id):
    for action in bpy.data.actions:
        raw = action.get(JOB_KEY)
        if not isinstance(raw, str):
            continue
        try:
            marker = json.loads(raw)
        except ValueError:
            continue
        if marker.get("job_id") == job_id and marker.get("item_id") == item_id:
            return action
    return None


def collect_successes(job, items=None):
    """Append intact successful Actions to the current session, never bind them."""
    context = _collect_context(job)
    directory = context["directory"]
    job_id = context["job_id"]
    snapshot_meta = context["snapshot_meta"]
    ledger = _read_json(directory / "ledger.json")
    ledger_items = _ledger_items(ledger)
    target, current_fingerprint = _find_collect_target(snapshot_meta)
    general_job.validate_collect_target(target, snapshot_meta)
    success_rows = [item for item in ledger_items if item.get("state") == "succeeded"]
    all_by_index = {item["index"]: item for item in ledger_items}
    all_by_id = {item["id"]: item for item in ledger_items}
    if items is None:
        requested = list(success_rows)
        requested_skipped = [
            item for item in ledger_items if item.get("state") != "succeeded"
        ]
    else:
        _require(isinstance(items, (list, tuple)), "collect items必须为数组")
        requested = []
        requested_skipped = []
        for value in items:
            if isinstance(value, int) and not isinstance(value, bool):
                item = all_by_index.get(value)
            elif isinstance(value, str):
                item = all_by_id.get(value)
            else:
                item = None
            _require(item is not None, "collect请求包含未知item：" + str(value))
            if item.get("state") == "succeeded":
                requested.append(item)
            else:
                requested_skipped.append(item)
    if not requested:
        return {
            "schema": COLLECT_SCHEMA,
            "job_id": job_id,
            "job_directory": str(directory),
            "snapshot_revision": ledger.get("snapshot_revision"),
            "status": "no_successes",
            "target": {
                "uid": snapshot_meta["target"]["uid"],
                "name": target.name,
                "renamed": target.name != snapshot_meta["target"]["name"],
            },
            "collected": [],
            "skipped": [
                {"item_index": item["index"], "item_id": item["id"],
                 "state": item.get("state"), "reason": item.get("error")}
                for item in requested_skipped
            ],
            "scene": {"preview_started": False, "binding_changed": False},
            "source": _source_live_state(snapshot_meta),
        }
    preflight = []
    for item in requested:
        path, action_name = _validate_output(directory, item)
        transport = item.get("mmd_transport")
        if transport:
            from . import retarget_mmd_job as mmd_job
            mmd_job.resolve_transport_hub(target, transport)
        preflight.append((item, path, action_name))
    binding_before = rt.binding(target)
    pose_before = _pose_hash(target)
    frame_before = (bpy.context.scene.frame_current, bpy.context.scene.frame_subframe)
    selection_before = _selection_state()
    filepath_before = str(bpy.data.filepath)
    dirty_before = bool(bpy.data.is_dirty)
    results = []
    owned_loaded = []
    owned_reports = []
    try:
        for item, path, action_name in preflight:
            if item.get("kind") == "CONTACT_DETECTION":
                from . import retarget_contact_detection as contact_detection
                text, existing, integrity = contact_detection.collect_report(_read_json(path), job_id, item)
                if not existing: owned_reports.append(text)
                results.append({"item_index":item["index"], "item_id":item["id"],
                    "artifact_type":"CONTACT_REPORT", "text":text.name, "action":None,
                    "output":item["output"], "output_sha256":item["output_sha256"],
                    "already_present":existing, "integrity":integrity, "bound":False,
                    "report_status":item.get("report_status"), "read_only":True})
                continue
            existing = _existing_marker(job_id, item["id"])
            if existing is not None:
                general_job.validate_candidate_policy(
                    target, existing, None, snapshot_meta)
                same = calibration.action_signature(existing) == item.get("action_signature")
                results.append({
                    "item_index": item["index"],
                    "item_id": item["id"],
                    "action": existing.name,
                    "output": item["output"],
                    "output_sha256": item["output_sha256"],
                    "already_present": True,
                    "integrity": "matches" if same else "modified_existing_retained",
                    "bound": False,
                    "fake_user": bool(existing.use_fake_user),
                })
                continue
            with bpy.data.libraries.load(str(path), link=False) as (source, destination):
                _require(action_name in source.actions, "输出Action名称已变化",
                         code="OUTPUT_INVALID")
                destination.actions = [action_name]
            loaded = list(destination.actions or [])
            _require(len(loaded) == 1 and loaded[0] is not None,
                     "输出Action导入引用异常",
                     code="OUTPUT_INVALID")
            action = loaded[0]
            owned_loaded.append(action)
            marker_raw = action.get(JOB_KEY)
            try:
                marker = json.loads(marker_raw) if isinstance(marker_raw, str) else None
            except ValueError:
                marker = None
            if not isinstance(marker, dict) or marker.get("job_id") != job_id or \
                    marker.get("item_id") != item["id"]:
                raise JobError("输出Action缺少本job身份标记", code="OUTPUT_INVALID")
            if calibration.action_signature(action) != item.get("action_signature"):
                raise JobError("输出Action内容与ledger签名不符", code="OUTPUT_TAMPERED")
            if item.get("mmd") is not None:
                from . import retarget_mmd_job as mmd_job
                mmd_job.restore_collected_hub(target, action, item.get("mmd_transport"))
            general_job.validate_candidate_policy(
                target, action, marker, snapshot_meta)
            action.use_fake_user = True
            results.append({
                "item_index": item["index"],
                "item_id": item["id"],
                "action": action.name,
                "output": item["output"],
                "output_sha256": item["output_sha256"],
                "already_present": False,
                "integrity": "matches",
                "bound": False,
                "fake_user": True,
            })
        if rt.binding(target) != binding_before:
            raise JobError("collect改变了目标Action绑定；已回滚新增Action",
                           code="COLLECT_CHANGED_TARGET")
        if _pose_hash(target) != pose_before:
            raise JobError("collect改变了目标姿态；已回滚新增Action",
                           code="COLLECT_CHANGED_TARGET")
        if (bpy.context.scene.frame_current, bpy.context.scene.frame_subframe) != frame_before:
            raise JobError("collect改变了当前帧；已回滚新增Action",
                           code="COLLECT_CHANGED_SCENE")
        if _selection_state() != selection_before:
            raise JobError("collect改变了选择/活动对象/模式；已回滚新增Action",
                           code="COLLECT_CHANGED_SCENE")
        if str(bpy.data.filepath) != filepath_before:
            raise JobError("collect改变了当前文件路径；已回滚新增Action",
                           code="COLLECT_CHANGED_SCENE")
    except BaseException:
        for text in reversed(owned_reports):
            try: bpy.data.texts.remove(text)
            except (ReferenceError, RuntimeError): pass
        for action in reversed(owned_loaded):
            try:
                bpy.data.actions.remove(action)
            except (ReferenceError, RuntimeError):
                pass
        raise
    scene_after = {
        "binding_unchanged": rt.binding(target) == binding_before,
        "pose_unchanged": _pose_hash(target) == pose_before,
        "frame_unchanged": (bpy.context.scene.frame_current,
                            bpy.context.scene.frame_subframe) == frame_before,
        "selection_unchanged": _selection_state() == selection_before,
        "filepath_unchanged": str(bpy.data.filepath) == filepath_before,
        "dirty_before": dirty_before,
        "dirty_after": bool(bpy.data.is_dirty),
        "preview_started": False,
        "binding_changed": False,
    }
    receipt = {
        "schema": COLLECT_SCHEMA,
        "job_id": job_id,
        "job_directory": str(directory),
        "snapshot_revision": ledger.get("snapshot_revision"),
        "status": "collected",
        "target": {
            "uid": snapshot_meta["target"]["uid"],
            "name": target.name,
            "renamed": target.name != snapshot_meta["target"]["name"],
            "static_sha256": current_fingerprint["static_sha256"],
        },
        "collected": results,
        "skipped": [
            {"item_index": item["index"], "item_id": item["id"],
             "state": item.get("state"), "reason": item.get("error")}
            for item in requested_skipped
        ],
        "scene": scene_after,
        "source": _source_live_state(snapshot_meta),
    }
    receipt_path = directory / ("collect-" + uuid.uuid4().hex + ".json")
    try:
        _atomic_json(receipt_path, receipt)
        receipt["receipt"] = str(receipt_path)
    except OSError as exc:
        receipt["receipt_error"] = str(exc)
    return receipt


def describe():
    return {
        "schema": JOB_SCHEMA,
        "entry": "enqueue(source,target,items=None,preset='MIXAMO'|'CMU'|'MMD'|'PROFILE',"
                 "mapping=None,settings=None,jobs_root=None,source_profile=None,"
                 "target_policy=None)",
        "contact_correction_entry": "enqueue_contact_corrections(target,items,settings=None,jobs_root=None)",
        "follow_up": ["status(job)", "peek_status(job)", "cancel(job)",
                      "collect_successes(job,items=None)"],
        "presets": ["MIXAMO", "CMU", "MMD", "CONTACT_CORRECTION", "CONTACT_DETECTION", "PROFILE"],
        "target_modes": {
            "C94": "MIXAMO/CMU/MMD/CONTACT without target_policy; existing C94 cores",
            "C94_MMD": "MMD registered source profile; existing MMD queue path",
            "CONTACT": "CONTACT_CORRECTION review queue / CONTACT_DETECTION read-only JSON reports",
            "GENERIC_FK": "MIXAMO/CMU with explicit acknowledged target_policy",
            "C94_PROFILE": "PROFILE with pure-JSON source_profile",
        },
        "max_items": 64,
        "snapshot": "bpy.data.libraries.write({scene, selected actions}, path_remap='ABSOLUTE')",
        "worker": "owned background Blender, isolated user resources, runtime package copy",
        "outputs": "Action-only .blend or CONTACT_DETECTION JSON per success; explicit artifact type, atomic output + hash + ledger",
        "contact_detection_entry": "enqueue_contact_detection(source,target,items,settings=None,jobs_root=None)",
        "not_supported": [
            "automatic contact correction approval", "old GLB exchange platform",
            "arbitrary Python presets", "automatic preview/binding on collect",
            "external Library/cache closure claim",
        ],
    }
