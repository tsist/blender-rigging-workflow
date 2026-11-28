"""Native keyed transform spaces for managed hand, foot and head controls."""

from math import isfinite

import bpy
from mathutils import Matrix, Vector

from . import semantic_rig_map

PREFIX = "RW Space"
ROLES = {"head", "ik_hand.L", "ik_hand.R", "ik_foot.L", "ik_foot.R"}
SPACES = ("World", "Root", "Chest", "Head", "Prop", "Release")


def _sample(rig, bone):
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.matrix_world @ evaluated.pose.bones[bone.name].matrix


def _key(owner, path, frame, interpolation):
    if not owner.keyframe_insert(data_path=path, frame=frame):
        raise ValueError("Could not key the managed space input")
    ad = owner.id_data.animation_data
    curve_path = owner.path_from_id(path)
    for layer in ad.action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(ad.action_slot)
            if bag:
                for curve in bag.fcurves:
                    if curve.data_path == curve_path:
                        for point in curve.keyframe_points:
                            if point.co.x == frame:
                                point.interpolation = interpolation


def _target(rig, mapping, role, space, prop):
    if space == "Prop":
        if (prop is None or prop == rig or prop.library or prop.override_library
                or prop.name not in bpy.context.view_layer.objects
                or prop.constraints):
            raise ValueError("Prop must be an independent local scene object")
        parent = prop.parent
        while parent is not None:
            if parent == rig or parent.constraints:
                raise ValueError("Prop hierarchy depends on the driven rig or another constraint")
            parent = parent.parent
        return prop, ""
    if prop is not None:
        raise ValueError("Choose a prop only for Prop space")
    if space in {"World", "Release"}:
        return None, ""
    anchor = space.lower()
    if role == "head" and space == "Head":
        anchor = "neck"
    name = mapping["controls"].get(anchor)
    if not name:
        raise ValueError("The rig lacks the requested space anchor: " + space)
    return rig, name


def _anchors(proxy):
    choices = {}
    for item in proxy.constraints:
        label = item.name.removeprefix(PREFIX + " ")
        is_prop = label == "Prop" or label.startswith("Prop ")
        key = ("Prop", item.target) if is_prop else (label, None)
        if (not item.name.startswith(PREFIX + " ") or item.type != "CHILD_OF" or item.target is None
                or (not is_prop and label not in {"Root", "Chest", "Head"}) or key in choices):
            raise ValueError("The managed proxy contains unexpected constraints")
        choices[key] = item
    return choices


def _key_transform(owner, fields, frame):
    for field in fields:
        _key(owner, field, frame, "LINEAR")


def _pose_fields(bone):
    rotation = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}
    return ("location", rotation.get(bone.rotation_mode, "rotation_euler"), "scale")


def switch(rig, *, role, space, frame, prop=None, max_error_meters=0.0001):
    """Key a separate editable proxy and preserve the current control transform."""
    if (role not in ROLES or space not in SPACES or type(frame) is not int
            or not isfinite(max_error_meters) or max_error_meters <= 0):
        raise ValueError("Choose a supported control role, space and integer frame")
    if rig.library or rig.override_library or rig.data.users != 1 or rig.mode == "EDIT":
        raise ValueError("The space switch requires an independently editable pose rig")
    bpy.context.view_layer.update()
    scene = bpy.context.scene
    mapping = semantic_rig_map.describe(rig)
    bone = rig.pose.bones.get(mapping["controls"].get(role, ""))
    if bone is None:
        raise ValueError("The rig does not expose this control role")
    target, subtarget = _target(rig, mapping, role, space, prop)
    releasing = space == "Release"
    constraint_name = f"{PREFIX} {role}"
    output = bone.constraints.get(constraint_name)
    created = output is None
    if created and releasing:
        raise ValueError("This control has no managed space to release")
    if output:
        proxy = output.target
        if (output.type != "COPY_TRANSFORMS" or proxy is None
                or proxy.get("rigging_space_owner") != f"{rig.name}:{role}"
                or proxy.parent is not None or output != bone.constraints[-1]):
            raise ValueError("The existing managed space proxy has changed")
        choices = _anchors(proxy)
    else:
        choices = {}
    choice = (space, target if space == "Prop" else None)
    selected = choices.get(choice)
    if selected is not None and (selected.target != target or selected.subtarget != subtarget):
        raise ValueError("This space already refers to a different anchor")

    current_frame = scene.frame_current, scene.frame_subframe
    scene.frame_set(frame - 1)
    previous_output = output.influence if output else 0.0
    previous_influences = {key: item.influence for key, item in choices.items()}
    previous_proxy = None if created else proxy.matrix_basis.copy()
    pose_fields = _pose_fields(bone)
    previous_pose = {field: tuple(getattr(bone, field)) for field in pose_fields}
    scene.frame_set(frame)
    desired = _sample(rig, bone)
    if created:
        proxy = bpy.data.objects.new(f"{rig.name} {role} Space", None)
        scene.collection.objects.link(proxy)
        proxy.empty_display_size = .08
        proxy.rotation_mode = "QUATERNION"
        proxy["rigging_space_owner"] = f"{rig.name}:{role}"
        output = bone.constraints.new("COPY_TRANSFORMS")
        output.name = constraint_name
        output.target = proxy
        output.target_space = output.owner_space = "WORLD"
        output.influence = 0
    new_anchor = selected is None and target is not None
    if new_anchor:
        selected = proxy.constraints.new("CHILD_OF")
        selected.name = f"{PREFIX} {space}" + (f" {target.name}" if space == "Prop" else "")
        selected.target, selected.subtarget = target, subtarget
        selected.inverse_matrix = Matrix.Identity(4)
        selected.influence = 0
        choices[choice] = selected
    old_output = float(output.influence)
    old_influences = {name: item.influence for name, item in choices.items()}
    old_matrix = proxy.matrix_basis.copy()
    old_pose = {field: tuple(getattr(bone, field)) for field in pose_fields}
    try:
        for item in choices.values():
            item.influence = float(item == selected)
        if releasing:
            if old_output == 0.0:
                raise ValueError("This control has already been released")
            output.influence = 0.0
            rig_world = rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world
            bone.matrix = rig_world.inverted() @ desired
            released_pose = {field: tuple(getattr(bone, field)) for field in pose_fields}
        else:
            if target == rig:
                anchor = _sample(rig, rig.pose.bones[subtarget])
            elif target is not None:
                bpy.context.view_layer.update()
                anchor = target.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
            else:
                anchor = Matrix.Identity(4)
            local = anchor.inverted() @ desired
            proxy.matrix_basis = local
            output.influence = 1.0
        actual = _sample(rig, bone)
        error = max((actual @ Vector(point) - desired @ Vector(point)).length for point in
                    ((0, 0, 0), (bone.length, 0, 0), (0, bone.length, 0), (0, 0, bone.length)))
        error *= scene.unit_settings.scale_length
        if error > max_error_meters:
            raise ValueError(f"Space switch moved the control by {error:.6g} m")
        output.influence = previous_output
        _key(output, "influence", frame - 1, "CONSTANT")
        for name, item in choices.items():
            item.influence = previous_influences.get(name, 0.0)
            _key(item, "influence", frame - 1, "CONSTANT")
        output.influence = 0.0 if releasing else 1.0
        _key(output, "influence", frame, "CONSTANT")
        for item in choices.values():
            item.influence = float(item == selected)
            _key(item, "influence", frame, "CONSTANT")
        if releasing:
            for field, values in previous_pose.items():
                setattr(bone, field, values)
            _key_transform(bone, pose_fields, frame - 1)
            for field, values in released_pose.items():
                setattr(bone, field, values)
            _key_transform(bone, pose_fields, frame)
        else:
            fields = ("location", "rotation_quaternion", "scale")
            if previous_proxy is not None:
                proxy.matrix_basis = previous_proxy
                _key_transform(proxy, fields, frame - 1)
            proxy.matrix_basis = local
            _key_transform(proxy, fields, frame)
        return {"rig": rig.name, "role": role, "space": space, "frame": frame,
                "proxy": proxy.name, "prop": prop.name if prop else None, "max_pose_error_meters": error}
    except BaseException:
        if created:
            bone.constraints.remove(output)
            bpy.data.objects.remove(proxy, do_unlink=True)
        else:
            output.influence = old_output
            if new_anchor:
                proxy.constraints.remove(selected)
            for name, item in choices.items():
                if not new_anchor or name != choice:
                    item.influence = old_influences[name]
            proxy.matrix_basis = old_matrix
            for field, values in old_pose.items():
                setattr(bone, field, values)
        raise
    finally:
        scene.frame_set(current_frame[0], subframe=current_frame[1])
