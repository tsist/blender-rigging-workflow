"""Public Win53 semantic map, limb matching and secondary motion operations."""

import math

OPERATIONS = {"semantic-map", "match-ik-fk", "space-switch", "elastic-bake"}
READ_ONLY = {"semantic-map"}
FIELDS = {
    "semantic-map": ({"rig"}, {"mapping"}),
    "match-ik-fk": ({"rig", "limb", "side", "mode", "start", "action_name"}, {"end", "max_error_meters"}),
    "space-switch": ({"rig", "role", "space", "frame"}, {"prop", "max_error_meters"}),
    "elastic-bake": ({"rig", "start", "end", "action_name", "target_groups"}, {"settings_control"}),
}


def validate(operation, parameters):
    required, optional = FIELDS[operation]
    if (not isinstance(parameters, dict) or not required <= parameters.keys() <= required | optional
            or not isinstance(parameters.get("rig"), str) or not parameters["rig"]):
        raise ValueError(f"{operation} requires {sorted(required)} and allows {sorted(optional)}")
    for field in ("start", "end", "frame"):
        if field in parameters and (type(parameters[field]) is not int or abs(parameters[field]) > 100000):
            raise ValueError("Frame bounds must be finite integers")
    if "max_error_meters" in parameters:
        error = parameters["max_error_meters"]
        if type(error) not in (int, float) or not math.isfinite(error) or not 0 < error <= .001:
            raise ValueError("max_error_meters must be positive and at most 0.001")


def guard_inputs(operation, parameters, held):
    return parameters


def _rig(bpy, parameters):
    rig = bpy.context.view_layer.objects.get(parameters["rig"])
    if rig is None or rig.type != "ARMATURE" or rig.library or rig.override_library:
        raise ValueError("Choose an exact local armature from the specified view layer")
    return rig


def _points(bpy, rig, frames, names):
    scene = bpy.context.scene
    old = scene.frame_current, scene.frame_subframe
    result = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            ev = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
            result.append([list(ev.matrix_world @ position) for name in names
                           for position in (ev.pose.bones[name].head, ev.pose.bones[name].tail)])
    finally:
        scene.frame_set(old[0], subframe=old[1])
    return result


def _samples(bpy, rig, operation, result):
    from rigging_workflow import retarget_action

    if operation == "space-switch":
        from rigging_workflow import semantic_rig_map

        names = [semantic_rig_map.describe(rig)["controls"][result["role"]]]
        frames = (result["frame"], result["frame"] + 1)
    elif operation == "elastic-bake":
        names = result["deforms"]
        frames = range(result["start"], result["end"] + 1)
    else:
        names = [bone.name for bone in rig.data.bones if bone.use_deform]
        frames = range(result["frame_start"], result["frame_end"] + 1)
        binding = retarget_action.binding(rig)
        action = bpy.data.actions.get(result["action"])
        if action is None or action.slots.get(result["slot"]) is None:
            raise ValueError("The stored matching candidate is missing its Action/Slot")
        try:
            rig.animation_data.action = action
            rig.animation_data.action_slot = action.slots[result["slot"]]
            return _points(bpy, rig, frames, names)
        finally:
            retarget_action.restore_binding(rig, binding)
    return _points(bpy, rig, frames, names)


def execute(bpy, operation, parameters, job_dir):
    from rigging_workflow import elastic, pose_matching, semantic_rig_map, space_switch

    rig = _rig(bpy, parameters)
    options = {key: value for key, value in parameters.items() if key != "rig"}
    if operation == "semantic-map":
        name = job_dir / "semantic_rig_map.json"
        return {"semantic_map": semantic_rig_map.write(rig, name, options.get("mapping")), "document": str(name)}
    if operation == "match-ik-fk":
        options["name"] = options.pop("action_name")
        result = pose_matching.bake_mode(rig, **options)
    elif operation == "space-switch":
        if "prop" in options:
            prop = bpy.context.view_layer.objects.get(options["prop"])
            if prop is None:
                raise ValueError("The specified prop is absent")
            options["prop"] = prop
        result = space_switch.switch(rig, **options)
    else:
        installed = elastic.install(rig, target_groups=options["target_groups"],
                                    settings_control=options.get("settings_control"))
        result = elastic.capture(rig, start=options["start"], end=options["end"]).bake(name=options["action_name"])
        result["deforms"] = [name for item in installed["groups"].values() for name in item["targets"]]
    result["sample_points"] = _samples(bpy, rig, operation, result)
    return result


def verify(bpy, operation, parameters, result):
    rig = _rig(bpy, parameters)
    expected = result["sample_points"]
    actual = _samples(bpy, rig, operation, result)
    error = max(math.dist(before, after) for base, cold in zip(expected, actual, strict=True)
                for before, after in zip(base, cold, strict=True))
    error *= bpy.context.scene.unit_settings.scale_length
    if error >= .0001:
        raise ValueError(f"Cold reopen pose changed by {error:.6g} m")
    return {"max_pose_error_meters": error, "frames": len(actual)}
