"""Minimal explicit evaluated-surface sampler for the contact candidate.

This adapter is deliberately narrow:

* the caller names the mesh, side, explicit vertex indices and patch labels;
* the caller supplies a positive-weight group whitelist; no foot is inferred
  from an object name or a group-name heuristic;
* the mesh must have exactly one enabled Armature consumer for the supplied
  armature and no other modifiers; unknown modifiers are rejected;
* original and evaluated topology identities must match, so an original
  vertex index has a stable evaluated counterpart;
* samples are read from ``Object.evaluated_get(depsgraph).data.vertices`` and
  transformed by the evaluated object's world matrix;
* sampling is background-only.  A live GUI session is rejected before any
  ``frame_set``;
* the requested armature's pose/object channels, binding and declared Scene
  settings are restored. No Action/keyframe/weight/rest edit is performed.
  ``frame_set`` evaluates the whole Scene: arbitrary other IDs, custom
  properties, handlers and external effects are not covered by this guard.
  Run only in disposable background snapshots and never save the result.

Source and target must be sampled separately with explicit frame and time
schedules.  This module never reads a source and a target at the same Scene
frame merely because their frame numbers happen to agree.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import struct

import bpy
from mathutils import Vector

ADAPTER_NAME = "retarget_contact_samples"
ADAPTER_VERSION = "0.1.1"

_OBJECT_CHANNELS = (
    "location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale",
    "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale",
)

SIDES = ("L", "R")
REGIONS = ("heel", "ball", "toe")


class SamplingError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)
        self.details = details

    def as_dict(self):
        out = {"ok": False, "code": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


def _error(code, message, details=None):
    raise SamplingError(code, message, details)


def require_background():
    if not bpy.app.background:
        _error(
            "live_gui_rejected",
            "evaluated-surface sampling is restricted to background snapshots/work copies",
            {"background": False},
        )


def _scene_view_layer(scene):
    if not scene.view_layers:
        _error("scene_without_view_layer", "scene has no view layer", {"scene": scene.name})
    return scene.view_layers[0]


def _scene_depsgraph(scene):
    layer = _scene_view_layer(scene)
    layer.update()
    depsgraph = getattr(layer, "depsgraph", None)
    if depsgraph is not None:
        return depsgraph
    try:
        with bpy.context.temp_override(scene=scene, view_layer=layer):
            return bpy.context.evaluated_depsgraph_get()
    except Exception as exc:
        _error(
            "depsgraph_unavailable",
            "could not obtain the evaluated depsgraph for the requested scene",
            {"scene": scene.name, "error": f"{type(exc).__name__}: {exc}"},
        )


def _topology_identity(mesh):
    counts = {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "polygons": len(mesh.polygons),
        "loops": len(mesh.loops),
    }
    digest = hashlib.sha256()
    digest.update(
        struct.pack(
            "<QQQQ",
            counts["vertices"],
            counts["edges"],
            counts["polygons"],
            counts["loops"],
        )
    )
    if counts["polygons"]:
        starts = array.array("i", [0]) * counts["polygons"]
        totals = array.array("i", [0]) * counts["polygons"]
        mesh.polygons.foreach_get("loop_start", starts)
        mesh.polygons.foreach_get("loop_total", totals)
        digest.update(starts.tobytes())
        digest.update(totals.tobytes())
    if counts["loops"]:
        indices = array.array("i", [0]) * counts["loops"]
        mesh.loops.foreach_get("vertex_index", indices)
        digest.update(indices.tobytes())
    counts["digest"] = digest.hexdigest()
    return counts


def _fcurve_count(action):
    legacy = 0
    layered = 0
    try:
        legacy = len(action.fcurves)
    except Exception:
        legacy = 0
    try:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    layered += len(bag.fcurves)
    except Exception:
        layered = 0
    return max(legacy, layered)


def _action_catalog():
    rows = []
    for action in bpy.data.actions:
        rows.append(
            {
                "name": action.name,
                "fcurves": _fcurve_count(action),
                "slots": len(action.slots) if hasattr(action, "slots") else None,
                "layers": len(action.layers) if hasattr(action, "layers") else None,
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def _constraint_catalog(armature_obj):
    rows = []
    for pose_bone in armature_obj.pose.bones:
        for constraint in pose_bone.constraints:
            rows.append(
                {
                    "bone": pose_bone.name,
                    "name": constraint.name,
                    "type": constraint.type,
                    "enabled": bool(getattr(constraint, "enabled", True)),
                }
            )
    return sorted(rows, key=lambda row: (row["bone"], row["name"], row["type"]))


def _rest_catalog(armature_obj):
    rows = []
    for bone in armature_obj.data.bones:
        rows.append(
            {
                "name": bone.name,
                "head": [float(value) for value in bone.head_local],
                "tail": [float(value) for value in bone.tail_local],
                "parent": bone.parent.name if bone.parent else None,
                "use_connect": bool(bone.use_connect),
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def _weight_digest(mesh_obj):
    digest = hashlib.sha256()
    names = [group.name for group in mesh_obj.vertex_groups]
    digest.update(json.dumps(names, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    for vertex in mesh_obj.data.vertices:
        digest.update(struct.pack("<I", vertex.index))
        for membership in sorted(vertex.groups, key=lambda item: item.group):
            digest.update(struct.pack("<Id", membership.group, float(membership.weight)))
    return digest.hexdigest()


def _snapshot_scene(scene):
    layer = _scene_view_layer(scene)
    active = layer.objects.active
    return {
        "scene": scene,
        "frame": scene.frame_current,
        "subframe": scene.frame_subframe,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "fps": scene.render.fps,
        "fps_base": scene.render.fps_base,
        "active": active,
    }


def _restore_scene(state, *, set_time=True):
    scene = state["scene"]
    scene.frame_start = state["frame_start"]
    scene.frame_end = state["frame_end"]
    scene.render.fps = state["fps"]
    scene.render.fps_base = state["fps_base"]
    if set_time:
        scene.frame_set(state["frame"], subframe=state["subframe"])
    layer = _scene_view_layer(scene)
    if state["active"] is not None and state["active"].name in layer.objects:
        layer.objects.active = state["active"]


def _snapshot_armature(armature_obj):
    animation = armature_obj.animation_data
    action = animation.action if animation else None
    slot = getattr(animation, "action_slot", None) if animation else None
    pose = {}
    for pose_bone in armature_obj.pose.bones:
        pose[pose_bone.name] = {
            "location": tuple(float(value) for value in pose_bone.location),
            "rotation_mode": pose_bone.rotation_mode,
            "rotation_quaternion": tuple(float(value) for value in pose_bone.rotation_quaternion),
            "rotation_euler": tuple(float(value) for value in pose_bone.rotation_euler),
            "rotation_axis_angle": tuple(float(value) for value in pose_bone.rotation_axis_angle),
            "scale": tuple(float(value) for value in pose_bone.scale),
        }
    return {
        "armature": armature_obj,
        "action": action,
        "action_slot": slot,
        "action_slot_handle": getattr(animation, "action_slot_handle", None) if animation else None,
        "last_slot_identifier": getattr(animation, "last_slot_identifier", None) if animation else None,
        "action_blend_type": getattr(animation, "action_blend_type", None) if animation else None,
        "action_influence": float(animation.action_influence) if animation else None,
        "action_extrapolation": getattr(animation, "action_extrapolation", None) if animation else None,
        "use_nla": bool(animation.use_nla) if animation else None,
        "had_animation_data": animation is not None,
        "pose_position": armature_obj.data.pose_position,
        "pose": pose,
        "object_channels": {field: tuple(getattr(armature_obj, field)) for field in _OBJECT_CHANNELS},
        "object_rotation_mode": armature_obj.rotation_mode,
        "driven_channels": sorted((fc.data_path, fc.array_index) for fc in animation.drivers
                                  if not fc.mute) if animation else [],
        "driver_catalog": sorted((fc.data_path, fc.array_index, bool(fc.mute))
                                 for fc in animation.drivers) if animation else [],
        "nla_tracks": sorted((track.name, bool(track.mute), bool(getattr(track, "is_solo", False)))
                             for track in animation.nla_tracks) if animation else [],
        "constraints": _constraint_catalog(armature_obj),
    }


def _restore_armature(state):
    armature_obj = state["armature"]
    if armature_obj.data.pose_position != state["pose_position"]:
        armature_obj.data.pose_position = state["pose_position"]
    animation = armature_obj.animation_data
    if animation is None:
        if not state.get("had_animation_data", False):
            for name, values in state["pose"].items():
                if name not in armature_obj.pose.bones:
                    continue
                pose_bone = armature_obj.pose.bones[name]
                pose_bone.rotation_mode = values["rotation_mode"]
                pose_bone.location = values["location"]
                pose_bone.rotation_quaternion = values["rotation_quaternion"]
                pose_bone.rotation_euler = values["rotation_euler"]
                pose_bone.rotation_axis_angle = values["rotation_axis_angle"]
                pose_bone.scale = values["scale"]
            armature_obj.update_tag()
            return
        animation = armature_obj.animation_data_create()
    if animation.action != state["action"]:
        animation.action = state["action"]
    if (state["action"] is not None and state["action_slot"] is not None
            and animation.action_slot != state["action_slot"]):
        animation.action_slot = state["action_slot"]
    for field in ("action_slot_handle", "last_slot_identifier"):
        if state.get(field) is not None:
            try:
                setattr(animation, field, state[field])
            except (AttributeError, TypeError):
                # Some Blender builds expose these as derived values.  The
                # declared guard still reports any resulting mismatch.
                pass
    if state["use_nla"] is not None and animation.use_nla != state["use_nla"]:
        animation.use_nla = state["use_nla"]
    if state.get("action_blend_type") is not None:
        animation.action_blend_type = state["action_blend_type"]
    if state.get("action_influence") is not None:
        animation.action_influence = state["action_influence"]
    if state.get("action_extrapolation") is not None:
        animation.action_extrapolation = state["action_extrapolation"]
    for name, values in state["pose"].items():
        if name not in armature_obj.pose.bones:
            continue
        pose_bone = armature_obj.pose.bones[name]
        pose_bone.rotation_mode = values["rotation_mode"]
        pose_bone.location = values["location"]
        pose_bone.rotation_quaternion = values["rotation_quaternion"]
        pose_bone.rotation_euler = values["rotation_euler"]
        pose_bone.rotation_axis_angle = values["rotation_axis_angle"]
        pose_bone.scale = values["scale"]
    # A generic sample may have created a temporary AnimData.  Remove it only
    # when the original object had none and no external driver/NLA data has
    # appeared; external animation data is preserved instead of destroyed.
    if not state.get("had_animation_data", False):
        if not animation.drivers and not animation.nla_tracks:
            armature_obj.animation_data_clear()
    armature_obj.update_tag()


def _capture_state(scene, armature_obj, state_core=None):
    scene_state = _snapshot_scene(scene)
    # Also retain declared raw channels for independent restoration checks.
    # With an existing production core these are validation data, not a
    # replacement restore implementation.
    declared = _snapshot_armature(armature_obj)
    if state_core is not None:
        with bpy.context.temp_override(scene=scene, view_layer=_scene_view_layer(scene)):
            state = state_core.snapshot(armature_obj)
            return {
                "mode": "existing_state_core",
                "core": state_core,
                "state": state,
                "armature": armature_obj,
                "scene": scene_state,
                "declared": declared,
            }
    return {
        "mode": "generic",
        "state": declared,
        "armature": armature_obj,
        "scene": scene_state,
        "declared": declared,
    }


def _restore_state(captured):
    if captured["mode"] == "existing_state_core":
        captured["core"].restore(captured["armature"], captured["state"])
        # Existing core restores time BEFORE pose; another frame_set here
        # would erase unkeyed input overrides that it just restored.
        _restore_scene(captured["scene"], set_time=False)
    else:
        _restore_scene(captured["scene"])
        _restore_armature(captured["state"])
    rig = captured["armature"]
    rig.rotation_mode = captured["declared"]["object_rotation_mode"]
    for field, values in captured["declared"]["object_channels"].items():
        setattr(rig, field, values)
    rig.update_tag()
    _scene_view_layer(captured["scene"]["scene"]).update()


def _declared_state_equal(captured):
    before = captured["declared"]
    after = _snapshot_armature(captured["armature"])
    driven = set(before["driven_channels"])
    def close(left, right, path):
        return len(left) == len(right) and all(
            (path,index) in driven or abs(a-b) <= 1e-6
            for index,(a,b) in enumerate(zip(left,right)))
    pose_equal = before["pose"].keys() == after["pose"].keys() and all(
        (values["rotation_mode"] == after["pose"][name]["rotation_mode"] and all(
            close(value, after["pose"][name][field], captured["armature"].pose.bones[name].path_from_id(field))
            for field,value in values.items() if field != "rotation_mode"))
        for name,values in before["pose"].items())
    object_equal = (before["object_rotation_mode"] == after["object_rotation_mode"] and all(
        close(values, after["object_channels"][field], field) for field,values in before["object_channels"].items()))
    binding_fields = ("action", "action_slot", "action_slot_handle", "last_slot_identifier",
                      "action_blend_type", "action_influence", "action_extrapolation",
                      "use_nla", "had_animation_data", "pose_position")
    return {"pose_channels_equal": pose_equal, "object_channels_equal": object_equal,
            "binding_equal": all(before[field] == after[field] for field in binding_fields),
            "driver_channel_catalog_equal": before["driven_channels"] == after["driven_channels"],
            "driver_catalog_equal": before["driver_catalog"] == after["driver_catalog"],
            "nla_track_catalog_equal": before["nla_tracks"] == after["nla_tracks"]}


def _normalize_patches(sides_patches):
    if not isinstance(sides_patches, dict) or not sides_patches:
        _error("invalid_patches", "sides_patches must be a nonempty object")
    out = {}
    for side, regions in sides_patches.items():
        if side not in SIDES:
            _error("invalid_side", "side must be L or R", {"side": side})
        if not isinstance(regions, dict):
            _error("invalid_patches", "each side must map region to explicit vertex indices", {"side": side})
        missing = [region for region in REGIONS if region not in regions]
        if missing:
            _error("missing_patch_region", "heel, ball and toe must be explicit", {"side": side, "missing": missing})
        out[side] = {}
        for region in REGIONS:
            values = regions[region]
            if not isinstance(values, (list, tuple)) or not values:
                _error("invalid_patch_indices", "patch must be a nonempty array", {"side": side, "region": region})
            indices = []
            for value in values:
                if isinstance(value, bool) or not isinstance(value, int):
                    _error("invalid_patch_indices", "vertex indices must be integers", {"side": side, "region": region})
                indices.append(value)
            if len(set(indices)) != len(indices):
                _error("duplicate_patch_index", "patch vertex indices must be unique", {"side": side, "region": region})
            out[side][region] = indices
    return out


def _normalize_whitelist(group_whitelist, sides_patches):
    if isinstance(group_whitelist, dict):
        out = {}
        for side in sides_patches:
            value = group_whitelist.get(side, group_whitelist)
            if isinstance(value, dict):
                per_region = {}
                for region in REGIONS:
                    names = value.get(region, [])
                    if not isinstance(names, (list, tuple)) or not names:
                        _error("invalid_group_whitelist", "each patch needs a nonempty group whitelist", {"side": side, "region": region})
                    per_region[region] = list(dict.fromkeys(str(name) for name in names))
                out[side] = per_region
            elif isinstance(value, (list, tuple)) and value:
                names = list(dict.fromkeys(str(name) for name in value))
                out[side] = {region: names for region in REGIONS}
            else:
                _error("invalid_group_whitelist", "group whitelist must be a nonempty list or region mapping", {"side": side})
        return out
    if isinstance(group_whitelist, (list, tuple)) and group_whitelist:
        names = list(dict.fromkeys(str(name) for name in group_whitelist))
        return {side: {region: names for region in REGIONS} for side in sides_patches}
    _error("invalid_group_whitelist", "group_whitelist must be a nonempty list or mapping")


def _plane_doc(plane):
    if plane is None:
        return {"status": "unknown", "normal": None, "d": None}
    if not isinstance(plane, dict) or plane.get("status") not in ("explicit", "unknown"):
        _error("invalid_plane", "plane must be omitted, unknown, or explicit with normal/d")
    if plane["status"] == "unknown":
        return {"status": "unknown", "normal": None, "d": None}
    normal = plane.get("normal")
    d = plane.get("d")
    if (
        not isinstance(normal, (list, tuple))
        or len(normal) != 3
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in normal)
        or isinstance(d, bool)
        or not isinstance(d, (int, float))
    ):
        _error("invalid_plane", "explicit plane requires three numeric normal components and numeric d")
    normal = tuple(float(value) for value in normal)
    length = math.sqrt(sum(value * value for value in normal))
    if abs(length - 1.0) > 1e-6:
        _error("invalid_plane", "explicit plane normal must be unit length", {"norm": length})
    return {"status": "explicit", "normal": normal, "d": float(d)}


def _validate_indices_and_weights(mesh_obj, sides_patches, whitelist):
    vertex_count = len(mesh_obj.data.vertices)
    groups = {group.name: group.index for group in mesh_obj.vertex_groups}
    validation = {}
    for side, regions in sides_patches.items():
        validation[side] = {}
        for region, indices in regions.items():
            allowed_names = whitelist[side][region]
            missing_groups = [name for name in allowed_names if name not in groups]
            if missing_groups:
                _error(
                    "unknown_group",
                    "group whitelist names must exist on the mesh",
                    {"side": side, "region": region, "missing_groups": missing_groups},
                )
            allowed_indices = {groups[name] for name in allowed_names}
            used_groups = set()
            for index in indices:
                if index < 0 or index >= vertex_count:
                    _error(
                        "vertex_index_out_of_range",
                        "explicit vertex index is outside the original mesh",
                        {"side": side, "region": region, "index": index, "vertices": vertex_count},
                    )
                memberships = [
                    membership
                    for membership in mesh_obj.data.vertices[index].groups
                    if membership.group in allowed_indices and membership.weight > 1e-6
                ]
                if not memberships:
                    _error(
                        "no_positive_weight_in_whitelist",
                        "explicit vertex has no positive weight in the supplied group whitelist",
                        {"side": side, "region": region, "index": index, "whitelist": allowed_names},
                    )
                used_groups.update(mesh_obj.vertex_groups[membership.group].name for membership in memberships)
            validation[side][region] = {
                "vertices": len(indices),
                "positive_weight_vertices": len(indices),
                "groups_used": sorted(used_groups),
                "whitelist": allowed_names,
            }
    return validation


def _inspect_mesh(scene, mesh_obj, armature_obj):
    if mesh_obj.type != "MESH":
        _error("not_a_mesh", "mesh object must have type MESH", {"object": mesh_obj.name, "type": mesh_obj.type})
    if armature_obj.type != "ARMATURE":
        _error("not_an_armature", "armature object must have type ARMATURE", {"object": armature_obj.name, "type": armature_obj.type})
    if mesh_obj.name not in scene.objects:
        _error("mesh_not_in_scene", "mesh is not linked into the requested scene", {"scene": scene.name, "mesh": mesh_obj.name})
    try:
        visible = bool(mesh_obj.visible_get(view_layer=_scene_view_layer(scene)))
    except TypeError:
        visible = bool(mesh_obj.visible_get())
    visibility = {
        "visible_get": visible,
        "hide_render": bool(mesh_obj.hide_render),
        "hide_viewport": bool(mesh_obj.hide_viewport),
        "hide_get": bool(mesh_obj.hide_get()),
        "in_scene": mesh_obj.name in scene.objects,
        "armature_visible_get": bool(armature_obj.visible_get()),
    }
    if not visible or visibility["hide_render"] or visibility["hide_viewport"]:
        _error(
            "not_visible",
            "the sampled mesh must be an actually visible production consumer",
            {"mesh": mesh_obj.name, **visibility},
        )
    modifiers = []
    for modifier in mesh_obj.modifiers:
        modifiers.append(
            {
                "name": modifier.name,
                "type": modifier.type,
                "show_viewport": bool(modifier.show_viewport),
                "show_render": bool(modifier.show_render),
                "object": modifier.object.name if getattr(modifier, "object", None) else None,
            }
        )
    armature_modifiers = [modifier for modifier in mesh_obj.modifiers if modifier.type == "ARMATURE"]
    if not armature_modifiers:
        _error("no_armature_consumer", "mesh has no Armature modifier", {"mesh": mesh_obj.name})
    if len(armature_modifiers) != 1:
        _error(
            "ambiguous_armature_consumer",
            "exactly one Armature consumer is required for this minimal adapter",
            {"mesh": mesh_obj.name, "armature_modifiers": [modifier.name for modifier in armature_modifiers]},
        )
    consumer = armature_modifiers[0]
    if consumer.object != armature_obj:
        _error(
            "wrong_armature_consumer",
            "Armature modifier does not point to the supplied armature",
            {"mesh": mesh_obj.name, "modifier": consumer.name, "actual": consumer.object.name if consumer.object else None, "expected": armature_obj.name},
        )
    if not consumer.show_viewport:
        _error("disabled_armature_consumer", "Armature modifier is disabled in the viewport", {"mesh": mesh_obj.name, "modifier": consumer.name})
    other_modifiers = [modifier for modifier in mesh_obj.modifiers if modifier.type != "ARMATURE"]
    if other_modifiers:
        _error(
            "unsupported_modifier",
            "this adapter accepts no modifier other than the explicit Armature consumer; it will not guess topology or deformation semantics",
            {"mesh": mesh_obj.name, "modifiers": [{"name": modifier.name, "type": modifier.type} for modifier in other_modifiers]},
        )
    depsgraph = _scene_depsgraph(scene)
    evaluated = mesh_obj.evaluated_get(depsgraph)
    original_topology = _topology_identity(mesh_obj.data)
    evaluated_topology = _topology_identity(evaluated.data)
    stable = original_topology == evaluated_topology
    if not stable:
        _error(
            "topology_unstable",
            "original and evaluated topology identities differ; original vertex indices have no stable evaluated mapping",
            {
                "mesh": mesh_obj.name,
                "original": original_topology,
                "evaluated": evaluated_topology,
            },
        )
    return {
        "visibility": visibility,
        "modifiers": modifiers,
        "armature_consumer": {
            "name": consumer.name,
            "object": armature_obj.name,
            "show_viewport": bool(consumer.show_viewport),
            "show_render": bool(consumer.show_render),
        },
        "topology": {
            "original": original_topology,
            "evaluated": evaluated_topology,
            "stable": True,
            "mapping": "same original/evaluated vertex index; topology counts and loop/polygon digests equal",
        },
    }


def _validate_frames(frames, times_s):
    if not isinstance(frames, (list, tuple)) or not isinstance(times_s, (list, tuple)):
        _error("invalid_schedule", "frames and times_s must be arrays")
    if len(frames) != len(times_s) or len(frames) < 2:
        _error("invalid_schedule", "frames and times_s must have the same length >= 2")
    frames_out = []
    times_out = []
    for index, (frame, time_s) in enumerate(zip(frames, times_s)):
        if isinstance(frame, bool) or not isinstance(frame, (int, float)):
            _error("invalid_schedule", "frame values must be numeric", {"index": index})
        if isinstance(time_s, bool) or not isinstance(time_s, (int, float)) or not math.isfinite(float(time_s)):
            _error("invalid_schedule", "time values must be finite numbers", {"index": index})
        frames_out.append(float(frame))
        times_out.append(float(time_s))
    for index in range(1, len(times_out)):
        if times_out[index] <= times_out[index - 1] + 1e-9:
            _error("times_not_strictly_increasing", "time schedule must be strictly increasing seconds", {"index": index})
        if frames_out[index] < frames_out[index - 1]:
            _error("frames_not_monotonic", "frame schedule must be monotonic", {"index": index})
    return frames_out, times_out


def _frame_set(scene, frame):
    base = int(math.floor(frame))
    subframe = float(frame - base)
    scene.frame_set(base, subframe=subframe)


def _point_list(points):
    return [[float(point[0]), float(point[1]), float(point[2])] for point in points]


def sample_mesh_schedule(
    scene,
    mesh_obj,
    armature_obj,
    sides_patches,
    group_whitelist,
    frames,
    times_s,
    *,
    plane=None,
    emit="points",
    state_core=None,
    state_core_name=None,
):
    """Sample explicit patches at explicit frames/times.

    ``sides_patches``: ``{"L": {"heel": [...], "ball": [...], "toe": [...]}, ...}``
    ``group_whitelist``: side -> list of group names, or side -> region -> list.
    ``emit``: ``points`` (actual evaluated vertices) or ``center`` (centroid and
    minimum signed clearance).
    """

    require_background()
    if emit not in ("points", "center"):
        _error("invalid_emit", "emit must be 'points' or 'center'")
    patches = _normalize_patches(sides_patches)
    whitelist = _normalize_whitelist(group_whitelist, patches)
    frames_out, times_out = _validate_frames(frames, times_s)
    plane_doc = _plane_doc(plane)
    inspected = _inspect_mesh(scene, mesh_obj, armature_obj)
    weight_validation = _validate_indices_and_weights(mesh_obj, patches, whitelist)

    actions_before = _action_catalog()
    constraints_before = _constraint_catalog(armature_obj)
    rest_before = _rest_catalog(armature_obj)
    weights_before = _weight_digest(mesh_obj)
    topology_before = inspected["topology"]["original"]
    frame_before = scene.frame_current
    captured = _capture_state(scene, armature_obj, state_core=state_core)
    sampled_frames = []
    max_delta = 0.0
    max_delta_frame = None
    nonzero_delta_frames = 0
    restore_error = None
    sampling_error = None
    try:
        for frame, time_s in zip(frames_out, times_out):
            _frame_set(scene, frame)
            layer = _scene_view_layer(scene)
            layer.update()
            depsgraph = _scene_depsgraph(scene)
            evaluated = mesh_obj.evaluated_get(depsgraph)
            if len(evaluated.data.vertices) != len(mesh_obj.data.vertices):
                _error(
                    "evaluated_vertex_count_changed",
                    "evaluated vertex count changed during sampling",
                    {"frame": frame, "original": len(mesh_obj.data.vertices), "evaluated": len(evaluated.data.vertices)},
                )
            world_matrix = evaluated.matrix_world.copy()
            rest_matrix = mesh_obj.matrix_world.copy()
            frame_entry = {"frame": frame, "time_s": time_s, "sides": {}}
            for side, regions in patches.items():
                frame_entry["sides"][side] = {}
                for region, indices in regions.items():
                    points = []
                    for index in indices:
                        evaluated_world = world_matrix @ evaluated.data.vertices[index].co
                        rest_world = rest_matrix @ mesh_obj.data.vertices[index].co
                        delta = (evaluated_world - rest_world).length
                        if delta > max_delta:
                            max_delta = delta
                            max_delta_frame = frame
                        if delta > 1e-9:
                            nonzero_delta_frames += 1
                        points.append(evaluated_world)
                    center = tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))
                    if plane_doc["status"] == "explicit":
                        normal = plane_doc["normal"]
                        d = plane_doc["d"]
                        patch_min = min(
                            normal[0] * point.x + normal[1] * point.y + normal[2] * point.z - d
                            for point in points
                        )
                    else:
                        patch_min = None
                    if emit == "points":
                        frame_entry["sides"][side][region] = {
                            "points": _point_list(points),
                            "center": [float(value) for value in center],
                            "patch_min": None if patch_min is None else float(patch_min),
                            "point_count": len(points),
                        }
                    else:
                        frame_entry["sides"][side][region] = {
                            "center": [float(value) for value in center],
                            "patch_min": None if patch_min is None else float(patch_min),
                            "point_count": len(points),
                        }
            sampled_frames.append(frame_entry)
    except Exception as exc:
        sampling_error = exc
    finally:
        try:
            _restore_state(captured)
        except Exception as exc:
            restore_error = f"{type(exc).__name__}: {exc}"

    actions_after = _action_catalog()
    constraints_after = _constraint_catalog(armature_obj)
    rest_after = _rest_catalog(armature_obj)
    weights_after = _weight_digest(mesh_obj)
    topology_after = _topology_identity(mesh_obj.data)
    frame_after = scene.frame_current
    declared_check = _declared_state_equal(captured)
    scene_before = captured["scene"]
    scene_settings_equal = (
        scene.frame_subframe == scene_before["subframe"]
        and scene.frame_start == scene_before["frame_start"]
        and scene.frame_end == scene_before["frame_end"]
        and scene.render.fps == scene_before["fps"]
        and scene.render.fps_base == scene_before["fps_base"]
        and _scene_view_layer(scene).objects.active == scene_before["active"]
    )
    restore_ok = (
        restore_error is None
        and frame_after == frame_before
        and actions_before == actions_after
        and constraints_before == constraints_after
        and rest_before == rest_after
        and weights_before == weights_after
        and topology_before == topology_after
        and all(declared_check.values())
        and scene_settings_equal
    )
    if not restore_ok:
        _error(
            "state_restore_failed",
            "sampling did not restore the scene/armature/action/rest/weight/topology state",
            {
                "restore_error": restore_error,
                "sampling_error": str(sampling_error) if sampling_error else None,
                **declared_check,
                "scene_settings_equal": scene_settings_equal,
                "frame_before": frame_before,
                "frame_after": frame_after,
                "actions_equal": actions_before == actions_after,
                "constraints_equal": constraints_before == constraints_after,
                "rest_equal": rest_before == rest_after,
                "weights_equal": weights_before == weights_after,
                "topology_equal": topology_before == topology_after,
            },
        )

    if sampling_error is not None:
        raise sampling_error

    samples = {}
    for side, regions in patches.items():
        samples[side] = {}
        for region in REGIONS:
            if emit == "points":
                samples[side][region] = {
                    "mode": "points",
                    "samples": [
                        frame["sides"][side][region]["points"]
                        for frame in sampled_frames
                    ],
                }
                if plane_doc["status"] == "explicit":
                    samples[side][region]["patch_min"] = [
                        frame["sides"][side][region]["patch_min"]
                        for frame in sampled_frames
                    ]
            else:
                samples[side][region] = {
                    "mode": "center",
                    "centers": [
                        frame["sides"][side][region]["center"]
                        for frame in sampled_frames
                    ],
                    "patch_min": [
                        frame["sides"][side][region]["patch_min"]
                        for frame in sampled_frames
                    ],
                }

    return {
        "ok": True,
        "adapter": {
            "name": ADAPTER_NAME,
            "version": ADAPTER_VERSION,
            "background_only": True,
            "writes_action": False,
            "writes_keyframes": False,
            "writes_scene": False,
            "writes_weights": False,
            "writes_modifiers": False,
            "writes_constraints": False,
        },
        "scene": scene.name,
        "mesh": mesh_obj.name,
        "armature": armature_obj.name,
        "visibility": inspected["visibility"],
        "modifiers": inspected["modifiers"],
        "armature_consumer": inspected["armature_consumer"],
        "topology": inspected["topology"],
        "group_validation": weight_validation,
        "plane": plane_doc,
        "samples": samples,
        "frames": sampled_frames,
        "deformation_evidence": {
            "source": "Object.evaluated_get(depsgraph).data.vertices transformed by evaluated matrix_world",
            "max_delta_from_rest_world": max_delta,
            "frame_of_max_delta": max_delta_frame,
            "nonzero_delta_samples": nonzero_delta_frames,
            "actual_deformation_observed": max_delta > 1e-9,
        },
        "state_guard": {
            "mode": captured["mode"],
            "state_core_name": state_core_name,
            "fallback_reason": captured.get("fallback_reason"),
            "frame_before": frame_before,
            "frame_after": frame_after,
            "actions_before": actions_before,
            "actions_after": actions_after,
            "actions_equal": actions_before == actions_after,
            "constraints_equal": constraints_before == constraints_after,
            "rest_equal": rest_before == rest_after,
            "weights_equal": weights_before == weights_after,
            "topology_equal": topology_before == topology_after,
            "weights_sha256_before": weights_before,
            "weights_sha256_after": weights_after,
            "restore_ok": True,
            **declared_check,
            "scene_settings_equal": scene_settings_equal,
            "scope": "requested armature nondriven raw pose/object inputs and binding; declared Scene time/range/fps/active object; mesh/action/rest/constraint/driver-channel catalog checks",
            "whole_scene_restore_claimed": False,
            "execution_boundary": "disposable background snapshot; never save sampled state back to input",
            "not_covered": ["other objects and IDs", "arbitrary custom properties", "driver-controlled output values", "handler/external effects", "arbitrary constraint and FCurve content edits"],
        },
        "rest_catalog": rest_before,
        "constraint_catalog": constraints_before,
        "sampling": {
            "method": "evaluated mesh vertices at explicit frames; separate schedules for source and target",
            "frames": len(frames_out),
            "time_start_s": times_out[0],
            "time_end_s": times_out[-1],
            "emit": emit,
            "background": bool(bpy.app.background),
            "no_save": True,
            "no_action_write": True,
            "no_keyframe_insert": True,
        },
    }


def sample_mesh_side(
    scene,
    mesh_obj,
    armature_obj,
    side,
    patches,
    group_whitelist,
    frames,
    times_s,
    *,
    plane=None,
    emit="points",
    state_core=None,
    state_core_name=None,
):
    if side not in SIDES:
        _error("invalid_side", "side must be L or R", {"side": side})
    return sample_mesh_schedule(
        scene,
        mesh_obj,
        armature_obj,
        {side: patches},
        group_whitelist,
        frames,
        times_s,
        plane=plane,
        emit=emit,
        state_core=state_core,
        state_core_name=state_core_name,
    )
