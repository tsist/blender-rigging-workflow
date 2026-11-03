"""Independent native fixtures for control-space P2. No addon implementation imports.

Import under Blender, create one case, persist the JSON manifest alongside .blend.
The coordinator owns save/reopen and calls the addon; this module never saves files.
"""
import copy
import math

import bpy
from mathutils import Euler, Matrix, Vector


SCHEMA = "hrig.space-fixture.v1"
TOLERANCE = 2e-5
CASE_IDS = (
    "match_full_uniform", "match_full_nonuniform",
    "match_fix_shear_uniform", "match_fix_shear_nonuniform",
    "match_none_uniform", "match_none_nonuniform",
    "match_shared_pose", "match_negative_uniform",
    "match_reject_shear", "match_reject_connected",
    "match_reject_locked", "match_reject_animated", "match_reject_driven",
    "switch_uniform", "switch_nonuniform", "switch_reject_selector_driver",
)


def _matrix(value):
    return [[float(value[row][col]) for col in range(4)] for row in range(4)]


def _rounded(value):
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        return {str(k): _rounded(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    return value


def _props(owner):
    result = {}
    for key in sorted(owner.keys()):
        value = owner[key]
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif hasattr(value, "to_list"):
            value = value.to_list()
        result[key] = value
    return _rounded(result)


def _channels(pose):
    return {
        "location": list(pose.location), "rotation_mode": pose.rotation_mode,
        "rotation_quaternion": list(pose.rotation_quaternion),
        "rotation_euler": list(pose.rotation_euler),
        "rotation_axis_angle": list(pose.rotation_axis_angle),
        "scale": list(pose.scale), "matrix_basis": _matrix(pose.matrix_basis),
    }


def _restore_channels(pose, state):
    pose.rotation_mode = state["rotation_mode"]
    pose.location = state["location"]
    pose.rotation_quaternion = state["rotation_quaternion"]
    pose.rotation_euler = state["rotation_euler"]
    pose.rotation_axis_angle = state["rotation_axis_angle"]
    pose.scale = state["scale"]


def _trs(owner, location, euler, scale):
    owner.rotation_mode = "QUATERNION"
    owner.location = location
    owner.rotation_quaternion = Euler(euler, "XYZ").to_quaternion()
    owner.scale = scale


def _flush():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def _world(rig, bone_name):
    evaluated = rig.evaluated_get(_flush())
    return evaluated.matrix_world @ evaluated.pose.bones[bone_name].matrix


def _constraint(constraint):
    result = {"name": constraint.name, "type": constraint.type}
    for name in ("mute", "influence", "owner_space", "target_space", "mix_mode",
                 "subtarget", "head_tail", "use_bbone_shape"):
        if hasattr(constraint, name):
            result[name] = getattr(constraint, name)
    if hasattr(constraint, "target"):
        result["target"] = constraint.target.name if constraint.target else None
    return result


def _drivers(obj):
    animation = obj.animation_data
    if animation is None:
        return {"action": None, "action_slot": None, "drivers": [], "nla": []}
    result = {
        "action": animation.action.name if animation.action else None,
        "action_slot": getattr(animation.action_slot, "identifier", None),
        "nla": [{"name": track.name, "mute": track.mute,
                 "strips": [{"name": strip.name, "type": strip.type,
                              "action": strip.action.name if strip.action else None}
                             for strip in track.strips]} for track in animation.nla_tracks],
        "drivers": [],
    }
    # The animated rejection fixture must preserve key coordinates as well as
    # its Action/slot binding. Read existing channelbags without ensuring any.
    if animation.action:
        action = animation.action
        layers = []
        for layer in action.layers:
            strips = []
            for strip in layer.strips:
                bags = []
                for slot in action.slots:
                    bag = strip.channelbag(slot, ensure=False)
                    if bag is None:
                        continue
                    curves = []
                    for curve in bag.fcurves:
                        curves.append({"path": curve.data_path, "index": curve.array_index,
                                       "mute": curve.mute,
                                       "keys": [{"co": list(key.co),
                                                 "handle_left": list(key.handle_left),
                                                 "handle_right": list(key.handle_right),
                                                 "interpolation": key.interpolation}
                                                for key in curve.keyframe_points]})
                    bags.append({"slot": slot.identifier, "curves": curves})
                strips.append({"type": strip.type, "channelbags": bags})
            layers.append({"name": layer.name, "strips": strips})
        result["action_content"] = layers
    for curve in animation.drivers:
        driver = curve.driver
        variables = []
        for variable in driver.variables:
            targets = []
            for target in variable.targets:
                targets.append({"id": target.id.name if target.id else None,
                                "id_type": target.id_type,
                                "data_path": target.data_path,
                                "bone_target": target.bone_target,
                                "transform_type": target.transform_type,
                                "transform_space": target.transform_space})
            variables.append({"name": variable.name, "type": variable.type,
                              "targets": targets})
        result["drivers"].append({"path": curve.data_path, "index": curve.array_index,
                                  "mute": curve.mute, "type": driver.type,
                                  "expression": driver.expression, "variables": variables})
    return result


def _storage(manifest):
    objects = {}
    for name in manifest["object_names"]:
        obj = bpy.data.objects[name]
        item = {"type": obj.type, "data": obj.data.name if obj.data else None,
                "data_users": obj.data.users if obj.data else None,
                "parent": obj.parent.name if obj.parent else None,
                "parent_type": obj.parent_type, "parent_bone": obj.parent_bone,
                "matrix_parent_inverse": _matrix(obj.matrix_parent_inverse),
                "matrix_world": _matrix(obj.matrix_world), "props": _props(obj),
                "animation": _drivers(obj)}
        if obj.type == "ARMATURE":
            item["data_props"] = _props(obj.data)
            item["rest"] = {}
            item["pose"] = {}
            for bone in obj.data.bones:
                item["rest"][bone.name] = {
                    "parent": bone.parent.name if bone.parent else None,
                    "matrix_local": _matrix(bone.matrix_local),
                    "head": list(bone.head_local), "tail": list(bone.tail_local),
                    "use_connect": bone.use_connect, "use_deform": bone.use_deform,
                    "inherit_scale": bone.inherit_scale,
                    "use_local_location": bone.use_local_location,
                    "use_inherit_rotation": bone.use_inherit_rotation,
                    "collections": sorted(c.name for c in bone.collections),
                    "props": _props(bone),
                }
            for pose in obj.pose.bones:
                item["pose"][pose.name] = {
                    "channels": _channels(pose), "props": _props(pose),
                    "lock_location": list(pose.lock_location),
                    "lock_rotation": list(pose.lock_rotation),
                    "lock_rotation_w": pose.lock_rotation_w,
                    "lock_rotations_4d": pose.lock_rotations_4d,
                    "lock_scale": list(pose.lock_scale),
                    "constraints": [_constraint(c) for c in pose.constraints],
                }
        objects[name] = item
    return _rounded({"objects": objects, "frame": bpy.context.scene.frame_current,
                     "subframe": bpy.context.scene.frame_subframe})


def _protected_storage(manifest, full):
    protected = copy.deepcopy(full)
    rig = protected["objects"][manifest["rig_name"]]
    # Only transform channels of this object's control may change. Shared data
    # and the sibling object's separate pose remain fully protected.
    del rig["pose"][manifest["bone_name"]]["channels"]
    if manifest["request"]["action"] == "switch_space":
        prop = manifest["request"]["property"]
        rig["pose"][prop["bone_name"]]["props"].pop(prop["key"], None)
        # Driver outputs are expected to change; definitions remain protected.
        for constraint in rig["pose"][manifest["space_parent"]]["constraints"]:
            constraint.pop("influence", None)
    return protected


def observe(manifest):
    """Read actual evaluated matrices; safe to call after independent reopen."""
    _check_manifest(manifest)
    graph = _flush()
    result = {"objects": {}, "finite": True}
    for name in manifest["object_names"]:
        obj = bpy.data.objects[name]
        evaluated = obj.evaluated_get(graph)
        state = {"matrix_world": _matrix(evaluated.matrix_world)}
        if obj.type == "ARMATURE":
            state["bones"] = {}
            for pose in evaluated.pose.bones:
                world = evaluated.matrix_world @ pose.matrix
                state["bones"][pose.name] = {
                    "world": _matrix(world), "pose": _matrix(pose.matrix),
                    "raw_basis": _matrix(obj.pose.bones[pose.name].matrix_basis),
                    "props": _props(obj.pose.bones[pose.name]),
                    "evaluated_influences": {c.name: c.influence for c in pose.constraints},
                }
                result["finite"] &= all(math.isfinite(v) for row in world for v in row)
            state["driver_valid"] = [
                {"path": fc.data_path, "index": fc.array_index,
                 "valid": fc.is_valid, "simple": fc.driver.is_simple_expression}
                for fc in obj.animation_data.drivers] if obj.animation_data else []
        result["objects"][name] = state
    return result


def capture(manifest):
    """JSON snapshot with both full and scoped protected storage."""
    observation = observe(manifest)
    storage = _storage(manifest)
    return {"observation": observation, "storage": storage,
            "protected_storage": _protected_storage(manifest, storage)}


def _check_manifest(manifest):
    if manifest.get("schema") != SCHEMA or manifest.get("case_id") not in CASE_IDS:
        raise ValueError("Unknown fixture manifest")
    for name in manifest["object_names"]:
        if name not in bpy.data.objects:
            raise ValueError("Missing fixture object: " + name)


def _error(a, b):
    return max(abs(float(a[r][c]) - float(b[r][c])) for r in range(4) for c in range(4))


def validate(manifest, before, after, operation_result=None):
    """Independent oracle: actual world against saved native reachable target.

    The coordinator passes the core's result for evidence; matrix/protection
    decisions do not trust that result. Rejection also requires explicit ok=False.
    """
    _check_manifest(manifest)
    request = manifest["request"]
    rig_name, bone_name = manifest["rig_name"], manifest["bone_name"]
    old = before["observation"]["objects"][rig_name]["bones"][bone_name]["world"]
    actual = after["observation"]["objects"][rig_name]["bones"][bone_name]["world"]
    rejection = manifest["expected"] == "reject"
    desired = old if rejection or request["action"] == "switch_space" else request["target_world"]
    error = _error(actual, desired)
    checks = {"finite": after["observation"]["finite"],
              "world_matrix": error <= manifest["tolerance"],
              "protected_storage": before["protected_storage"] == after["protected_storage"]}
    if rejection:
        checks["complete_declared_storage_unchanged"] = before["storage"] == after["storage"]
        checks["explicit_reject"] = isinstance(operation_result, dict) and operation_result.get("ok") is False
    else:
        checks["explicit_success"] = isinstance(operation_result, dict) and operation_result.get("ok") is True
    if request["action"] == "switch_space" and not rejection:
        prop = request["property"]
        after_rig = after["observation"]["objects"][rig_name]
        actual_value = after_rig["bones"][prop["bone_name"]]["props"][prop["key"]]
        checks["property_value"] = actual_value == request["new_value"]
        parent_before = before["observation"]["objects"][rig_name]["bones"][manifest["space_parent"]]["world"]
        parent_after = after_rig["bones"][manifest["space_parent"]]["world"]
        checks["space_parent_actually_moved"] = _error(parent_before, parent_after) > 0.05
        checks["drivers_valid"] = all(row["valid"] and row["simple"] for row in after_rig["driver_valid"])
        influences = after_rig["bones"][manifest["space_parent"]]["evaluated_influences"]
        checks["space_influences"] = abs(influences["Space0"]) <= 1e-7 and abs(influences["Space1"] - 1) <= 1e-7
    sibling = manifest.get("sibling_name")
    if sibling:
        a = before["observation"]["objects"][sibling]["bones"]
        b = after["observation"]["objects"][sibling]["bones"]
        checks["shared_sibling_evaluated_pose"] = all(_error(a[name]["world"], b[name]["world"]) <= manifest["tolerance"] for name in a)
        checks["shared_data_identity"] = before["storage"]["objects"][rig_name]["data"] == after["storage"]["objects"][sibling]["data"]
    return {"schema": SCHEMA, "case_id": manifest["case_id"], "ok": all(checks.values()),
            "checks": checks, "world_max_abs_error": error,
            "tolerance": manifest["tolerance"], "operation_result": operation_result}


def create_case(case_id, prefix="HRIG_SPACE"):
    """Create <=3 bones per rig without clearing the scene or saving a file."""
    if case_id not in CASE_IDS:
        raise ValueError("Unknown case: " + case_id)
    if bpy.context.mode != "OBJECT":
        raise ValueError("Fixture creation requires OBJECT mode")
    root = prefix + "_" + case_id
    rig_name = root + "_Rig"
    if any(obj.name.startswith(root) for obj in bpy.data.objects):
        raise ValueError("Fixture prefix collision: " + root)
    if bpy.data.armatures.get(root + "_Data"):
        raise ValueError("Fixture data collision: " + root)
    selected = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    data = bpy.data.armatures.new(root + "_Data")
    rig = bpy.data.objects.new(rig_name, data)
    bpy.context.scene.collection.objects.link(rig)
    objects = [rig_name]
    switching = case_id.startswith("switch_")
    parent_name = "SpaceParent" if switching else "Parent"
    try:
        for obj in selected:
            obj.select_set(False)
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode="EDIT")
        parent = data.edit_bones.new(parent_name)
        parent.head, parent.tail, parent.roll = (0, 0, 0), (0, 1, 0), 0.31
        control = data.edit_bones.new("Control")
        control.head, control.tail, control.roll = (0.2, 1.1, 0.1), (0.2, 2.0, 0.3), -0.43
        control.parent = parent
        if case_id == "match_reject_connected":
            control.head = parent.tail
            control.use_connect = True
        guard = data.edit_bones.new("Settings" if switching else "Guard")
        guard.head, guard.tail = (-1, 0, 0), (-1, 0.6, 0)
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if rig.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        rig.select_set(False)
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
    inherit = "FIX_SHEAR" if "fix_shear" in case_id else "NONE" if "_none_" in case_id else "FULL"
    data.bones["Control"].inherit_scale = inherit
    for bone in data.bones:
        bone.use_deform = False
    nonuniform = "nonuniform" in case_id
    object_scale = (1.4, 0.8, 1.15) if nonuniform else (1.2, 1.2, 1.2)
    if case_id == "match_negative_uniform":
        object_scale = (-1.2, -1.2, -1.2)
    _trs(rig, (1.3, -0.6, 0.9), (0.21, -0.12, 0.37), object_scale)
    _trs(rig.pose.bones[parent_name], (0.2, -0.1, 0.3), (0.16, 0.25, -0.29),
         (1.35, 0.75, 1.1) if nonuniform else (1.1, 1.1, 1.1))
    _trs(rig.pose.bones["Control"], (-0.1, 0.05, 0.12), (0.1, -0.14, 0.07), (1, 1, 1))
    rig["fixture_sentinel"] = "unchanged"
    rig.pose.bones["Control"]["sentinel"] = 17
    manifest = {"schema": SCHEMA, "case_id": case_id, "rig_name": rig_name,
                "bone_name": "Control", "object_names": objects,
                "expected": "success", "tolerance": TOLERANCE,
                "inherit_scale": inherit,
                "request": {"action": "match_world", "object_name": rig_name,
                            "bone_name": "Control"}}
    if switching:
        settings = rig.pose.bones["Settings"]
        settings["space"] = 0.0
        for index, location in enumerate(((0.2, -0.4, 0.8), (2.1, 0.7, 1.2))):
            target = bpy.data.objects.new(root + "_Space" + str(index), None)
            bpy.context.scene.collection.objects.link(target)
            # Same orientation and scale in both nonuniform spaces keeps this
            # bounded switch representable while still exercising object scale.
            _trs(target, location, (0.0, 0.0, 0.19), (1.1, 0.9, 1.2) if nonuniform else (1, 1, 1))
            objects.append(target.name)
            constraint = rig.pose.bones[parent_name].constraints.new("COPY_TRANSFORMS")
            constraint.name = "Space" + str(index)
            constraint.target = target
            constraint.owner_space = "WORLD"
            constraint.target_space = "WORLD"
            constraint.mix_mode = "REPLACE"
            curve = constraint.driver_add("influence")
            curve.driver.type = "SCRIPTED"
            variable = curve.driver.variables.new()
            variable.name, variable.type = "space", "SINGLE_PROP"
            variable.targets[0].id = rig
            variable.targets[0].data_path = 'pose.bones["Settings"]["space"]'
            curve.driver.expression = "1.0-space" if index == 0 else "space"
        manifest["space_parent"] = parent_name
        manifest["request"] = {"action": "switch_space", "object_name": rig_name,
                               "bone_name": "Control", "property": {"bone_name": "Settings", "key": "space"},
                               "new_value": 1.0}
        if case_id == "switch_reject_selector_driver":
            selector_curve = settings.driver_add('["space"]')
            selector_curve.driver.type = "SCRIPTED"
            selector_curve.driver.expression = "0.0"
            manifest["expected"] = "reject"
        rig.update_tag()
        _flush()
    else:
        control = rig.pose.bones["Control"]
        initial = _channels(control)
        # The oracle is an actually evaluated pose generated by native channels,
        # not an inverse of the implementation's matching formula.
        _trs(control, (0.32, -0.17, 0.26), (-0.23, 0.19, 0.34), (0.91, 1.08, 1.02))
        manifest["request"]["target_world"] = _matrix(_world(rig, "Control"))
        _restore_channels(control, initial)
        if case_id == "match_reject_shear":
            rig.matrix_world = Matrix.Identity(4)
            rig.pose.bones[parent_name].matrix_basis = Matrix.Identity(4)
            # Explicit affine shear in a non-parented control is unambiguously
            # outside the LRS domain. The rest hierarchy is finalized here.
            for obj in selected:
                obj.select_set(False)
            rig.select_set(True)
            bpy.context.view_layer.objects.active = rig
            bpy.ops.object.mode_set(mode="EDIT")
            data.edit_bones["Control"].parent = None
            bpy.ops.object.mode_set(mode="OBJECT")
            rig.select_set(False)
            for obj in selected:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = active
            shear = Matrix.Identity(4)
            shear[0][1] = 0.35
            target = data.bones["Control"].matrix_local @ shear
            manifest["request"]["target_world"] = _matrix(target)
            manifest["expected"] = "reject"
        if case_id == "match_reject_connected":
            manifest["expected"] = "reject"
        if case_id == "match_reject_locked":
            control.lock_location[0] = True
            manifest["expected"] = "reject"
        if case_id == "match_reject_animated":
            control.keyframe_insert(data_path="location", index=0, frame=1,
                                    group="FixtureControl")
            manifest["expected"] = "reject"
        if case_id == "match_reject_driven":
            transform_curve = control.driver_add("location", 0)
            transform_curve.driver.type = "SCRIPTED"
            transform_curve.driver.expression = "0.0"
            transform_curve.mute = True
            manifest["expected"] = "reject"
        if case_id == "match_shared_pose":
            sibling = bpy.data.objects.new(root + "_Sibling", data)
            bpy.context.scene.collection.objects.link(sibling)
            _flush()  # Native pose is initialized by evaluation after linking.
            objects.append(sibling.name)
            _trs(sibling, (-2, 0.5, 1), (0.1, 0.3, -0.2), (1, 1, 1))
            _trs(sibling.pose.bones["Parent"], (0.1, 0.3, 0.2), (-0.2, 0.1, 0.4), (1, 1, 1))
            _trs(sibling.pose.bones["Control"], (0.4, 0.1, -0.1), (0.2, -0.3, 0.1), (1, 1, 1))
            manifest["sibling_name"] = sibling.name
    rig.update_tag()
    _flush()
    manifest["request"]["expected_current_world"] = _matrix(_world(rig, "Control"))
    return manifest
