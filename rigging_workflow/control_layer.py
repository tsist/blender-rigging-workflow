"""Consistent animation controls over verified C94 and Rigify human mechanisms."""
import json
from contextlib import contextmanager
from math import cos, isfinite, pi, sin
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

CONTROL_KEY = "rigging_agent_controls_v1"
SOURCE_ACTION_KEY = "rigging_agent_control_source_action_v1"
_ACTION_BINDING_KEY = "rigging_agent_control_action_source_v1"
_PREFIX = "Animator · "
_DRIVEN_PATHS = frozenset({"location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle",
                           "rotation_mode", "scale", "matrix_basis"})
_RETARGET_INPUTS_ACTIVE = set()
_INPUT_BRIDGE_TOLERANCE_METERS = 1e-5
_POSE_FIELDS = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale")
_ROTATION = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}

# Source names describe proven controls, including the foot target that owns C94's roll hierarchy.
_BODY = (
    ("Root", "根骨骼", "root", "root"),
    ("Torso", "躯干", "torso", "torso"),
    ("Hips", "盆骨控制器", "hips", "hips"),
    ("Chest", "胸腔控制器", "chest", "chest"),
    ("Head", "头颅控制器", "head", "head"),
)
_LIMBS = (
    ("Hand IK", "手掌IK-目标", "hand_ik", "hand", "Arm", "IK"),
    ("Elbow Pole", "大臂IK-极点", "upper_arm_ik_target", "pole", "Arm", "IK"),
    ("Upper Arm FK", "大臂FK", "upper_arm_fk", "fk", "Arm", "FK"),
    ("Forearm FK", "小臂FK", "forearm_fk", "fk", "Arm", "FK"),
    ("Hand FK", "手掌FK", "hand_fk", "fk", "Arm", "FK"),
    ("Foot IK", "腿部IK-目标", "foot_ik", "foot", "Leg", "IK"),
    ("Knee Pole", "腿部IK-极点", "thigh_ik_target", "pole", "Leg", "IK"),
    ("Foot Roll", "脚部滚转IK", "foot_heel_ik", "roll", "Leg", "IK"),
    ("Thigh FK", "大腿FK", "thigh_fk", "fk", "Leg", "FK"),
    ("Shin FK", "小腿FK", "shin_fk", "fk", "Leg", "FK"),
    ("Foot FK", "脚掌FK", "foot_fk", "fk", "Leg", "FK"),
    ("Toe IK", "脚部-枢轴-趾根", "toe_ik", "toe", "Leg", "IK"),
    ("Toe FK", "脚趾", "toe_fk", "toe", "Leg", "FK"),
)
_FINGERS = (("Thumb", "大拇指"), ("Index", "食指"), ("Middle", "中指"), ("Ring", "无名指"), ("Pinky", "小拇指"))
_C94_EYELIDS = (
    ("Upper Eyelid", "眼睑-03.T", "upper_lid"),
    ("Lower Eyelid", "眼睑-05.B", "lower_lid"),
)

_PALETTE = {
    "L": (0.18, 0.69, 0.92),
    "R": (0.97, 0.36, 0.28),
    "C": (0.97, 0.72, 0.28),
}
# Sizes are fractions of the actual character height, with ring size also bounded by segment length.
_DIMENSIONS = {
    "root": (.16, .10), "torso": (.10, .045), "hips": (.085, .035),
    "chest": (.11, .035), "head": (.055, .043), "hand": (.033, .026),
    "foot": (.032, .070), "pole": (.017, .017), "roll": (.028, .022),
    "fk": (.032, .032), "eye": (.016, .014), "toe": (.021, .023), "finger": (.012, .011),
    "upper_lid": (.009, .003), "lower_lid": (.009, .003), "toe_fk": (.016, .018),
}
_SHAPES = {
    "root": ((-1, -.62), (-.77, -1), (.77, -1), (1, -.62), (1, .62), (.77, 1),
             (.26, 1), (0, 1.32), (-.26, 1), (-.77, 1), (-1, .62)),
    "torso": ((-1, -.53), (-.77, -1), (.77, -1), (1, -.53), (1, .53),
              (.77, 1), (-.77, 1), (-1, .53)),
    "hand": ((-1, -.6), (-.6, -1), (.6, -1), (1, -.6), (1, .6), (.6, 1),
             (-.6, 1), (-1, .6)),
    "foot": ((-.54, -.65), (.54, -.65), (.88, -.2), (1, .95), (.65, 1.3),
             (-.65, 1.3), (-1, .95), (-.88, -.2)),
    "pole": ((-1, 0), (0, -1), (1, 0), (0, 1)),
    "toe": ((-1, -.7), (0, -1), (1, -.7), (1, .45), (0, 1), (-1, .45)),
    "toe_fk": ((-1, .20), (-.54, -.70), (.54, -.70), (1, .20), (.50, .70), (-.50, .70)),
    "upper_lid": ((-1, -.50), (-.50, .58), (0, 1), (.50, .58), (1, -.50),
                  (.50, .12), (0, .46), (-.50, .12)),
    "lower_lid": ((-1, .50), (-.50, -.58), (0, -1), (.50, -.58), (1, .50),
                  (.50, -.12), (0, -.46), (-.50, -.12)),
    "roll": ((-1, -.52), (-.63, -1), (.63, -1), (1, -.52), (.65, .6), (0, 1), (-.65, .6)),
}

def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _metadata(rig, key, schema):
    info = json.loads(rig.get(key, "{}"))
    _require(isinstance(info, dict) and info.get("schema") == schema, f"骨架缺少有效的来源身份 {key}")
    return info


def _controller_specs(rig, family, sections):
    _require(family in {"c94", "rigify_human"}, "控制层需要明确的 c94 或 rigify_human 骨架来源")
    _require(sections and len(set(sections)) == len(sections)
             and set(sections) <= {"body", "arms", "legs", "hands", "face", "eyelids", "toes"},
             "控制组有空项、重复或未知项")
    if family == "c94":
        _metadata(rig, "c94_generated_rig_v1", "c94.generated.v1")
    modules = None
    if family == "rigify_human":
        meta = _metadata(rig, "rigging_modular_v1", "rigging.modular.generated.v1")
        _require(meta.get("kind") == "HUMAN", "这套 Rigify 控制映射只适用于独立模块人形")
        modules = meta.get("configuration", {}).get("enabled_modules")
        _require(isinstance(modules, list) and "body" in modules, "独立模块人形须声明 body 模块")
        _require(not {"face", "hands"}.intersection(sections), "已验证的 Rigify 模块人形没有独立脸部或精细手指骨控")
        _require(not {"eyelids", "toes"}.intersection(sections), "眼睑和独立脚趾选件使用已验证的 C94 原生控制")
    index = 1 if family == "c94" else 2
    rows = {}
    if "body" in sections:
        for row in _BODY:
            if row[0] == "Head" and modules is not None and "head" not in modules:
                continue
            rows[row[0]] = (row[index], row[3], "身体", "C", None)
    for side in ("L", "R"):
        if "face" in sections:
            rows[f"Eye Master.{side}"] = (f"眼部_master.{side}", "eye", "面部选件", side, None)
        if "eyelids" in sections:
            for role, source, shape in _C94_EYELIDS:
                rows[f"{role}.{side}"] = (f"{source}.{side}", shape, "眼睑选件", side, None)
        if "hands" in sections:
            for role, source in _FINGERS:
                rows[f"{role}.{side}"] = (f"{source}.{side}", "finger", f"手指选件.{side}", side, None)
        for row in _LIMBS:
            role, _, _, shape, limb, mode = row
            section = "arms" if limb == "Arm" else "legs"
            if family == "c94" and role == "Toe FK":
                section, shape = "toes", "toe_fk"
            if section not in sections:
                continue
            module = ("forelimb" if limb == "Arm" else "hindlimb") + "." + side
            if modules is not None and module not in modules:
                continue
            label = "手臂" if limb == "Arm" else "腿部"
            selector, prop, ik_value = _mode_spec(family, limb, side)
            mode_bone = rig.pose.bones.get(selector)
            _require(mode_bone is not None and prop in mode_bone, f"{label}.{side} 的原生模式参数不存在")
            group = f"{label} {mode}.{side}"
            if section == "toes":
                group = f"脚趾 FK.{side}"
            if shape == "pole":
                group += " 极点"
            pole_property = "pole_vector" if shape == "pole" and family == "rigify_human" else None
            _require(pole_property is None or pole_property in mode_bone, f"{label}.{side} 的原生极点参数不存在")
            rows[f"{role}.{side}"] = (f"{row[index]}.{side}", shape, group, side,
                                      (selector, prop, ik_value, mode, pole_property))
    _require(rows, "选择的控制组与实际安装模块没有交集")
    return rows


def _mode_spec(family, limb, side):
    if family == "c94":
        direction = "左" if side == "L" else "右"
        label = "臂" if limb == "Arm" else "腿"
        return "骨架设置", f"{direction}{label}FK-IK", 1
    return ("upper_arm_parent" if limb == "Arm" else "thigh_parent") + "." + side, "IK_FK", 0


def _mode_visible(rig, spec):
    if spec is None:
        return True
    selector, prop, ik_value, mode, *extra = spec
    mode_bone = rig.pose.bones[selector]
    if extra and extra[0] is not None and not mode_bone[extra[0]]:
        return False
    value = float(mode_bone[prop])
    if not (0 <= value <= 1):
        raise ValueError(f"{selector}.{prop} 超出原生 IK/FK 范围")
    ik = value == ik_value
    fk = value == 1 - ik_value
    return not (ik or fk) or (mode == "IK" if ik else mode == "FK")


def _channels(pose):
    return ("location", _ROTATION.get(pose.rotation_mode, "rotation_euler"), "scale")


def _path_map(rig, mapping):
    paths = {}
    blocked = set()
    for role, source in mapping.items():
        pb = rig.pose.bones[source]
        old = pb.path_from_id()
        new = 'pose.bones["CTRL-' + bpy.utils.escape_identifier(role) + '"]'
        supported = _channels(pb)
        paths.update({old + "." + field: new + "." + field for field in supported})
        blocked.update(old + "." + field for field in _DRIVEN_PATHS if field not in supported)
    return paths, blocked


def _bag(action, slot_identifier):
    _require(isinstance(action, bpy.types.Action), "需要明确的 Blender Action")
    slot = action.slots.get(slot_identifier)
    _require(slot is not None and slot.target_id_type == "OBJECT", "所选 Action Slot 不是有效的对象 Slot")
    _require(len(action.layers) == 1 and len(action.layers[0].strips) == 1
             and action.layers[0].strips[0].type == "KEYFRAME", "仅转换单层单 Keyframe Strip 的 Action")
    bag = action.layers[0].strips[0].channelbag(slot)
    _require(bag is not None, "指定 Slot 没有动画通道")
    return slot, bag


def _action_plan(rig, action, slot_identifier, mapping):
    slot, bag = _bag(action, slot_identifier)
    paths, blocked = _path_map(rig, mapping)
    destination = set()
    count = 0
    for curve in bag.fcurves:
        path = curve.data_path
        _require(path not in blocked, f"Action 通道使用不支持的旋转表示或骨架模式 {path}")
        try:
            rig.path_resolve(path)
        except ValueError as exc:
            raise ValueError(f"Action 通道不属于明确目标 {path}") from exc
        mapped = paths.get(path, path)
        identity = (mapped, curve.array_index)
        _require(identity not in destination, f"转换后出现重复通道 {mapped}")
        destination.add(identity)
        count += mapped != path
    _require(count > 0, "这个 Slot 没有可映射到本控制层的原生变换通道")
    return slot.identifier, paths, count


def _copy_action(source, slot_identifier, paths):
    copy = source.copy()
    try:
        copy.name = source.name + " · 动画控制层"
        copy.use_fake_user = True
        copy[_ACTION_BINDING_KEY] = source.name
        slot, bag = _bag(copy, slot_identifier)
        for curve in bag.fcurves:
            curve.data_path = paths.get(curve.data_path, curve.data_path)
        return copy, slot
    except Exception:
        bpy.data.actions.remove(copy)
        raise


def read(rig):
    """Return the persistent controls and exact family mapping from an adopted rig."""
    return _metadata(rig, CONTROL_KEY, "rigging.agent.controls.v1")


def remap_action(rig, action, slot):
    """Return (independent Action, copied Slot, mapped count) without binding either."""
    record = read(rig)
    _require(isinstance(action, bpy.types.Action), "需要明确的 Blender Action")
    if isinstance(slot, bpy.types.ActionSlot):
        _require(action.slots.get(slot.identifier) == slot, "Slot 不属于指定 Action")
        slot = slot.identifier
    _require(isinstance(slot, str) and slot, "指定 Action Slot 或其标识")
    mapping = {role: item["source"] for role, item in record["controls"].items()}
    identifier, paths, count = _action_plan(rig, action, slot, mapping)
    candidate, copied_slot = _copy_action(action, identifier, paths)
    return candidate, copied_slot, count


def refresh_visibility(rig, *, limb=None, side=None):
    """Refresh collections after the original rig's mode parameters change."""
    record = read(rig)
    selected = _mode_spec(record["family"], limb, side)[:2] if limb else None
    visible = {}
    for item in record["controls"].values():
        spec = item["mode"]
        if spec is not None and (selected is None or tuple(spec[:2]) == selected):
            group = item["collection"]
            collection = rig.data.collections_all.get(group)
            _require(collection is not None, f"动画控制集合缺失 {group}")
            collection.is_visible = _mode_visible(rig, spec)
            visible[group] = bool(collection.is_visible)
    return visible


def set_legacy_visibility(rig, *, visible):
    """Show the original supplementary groups in their saved arrangement."""
    _require(type(visible) is bool, "原生补充集合显隐须使用 bool")
    record = read(rig)
    groups = record.get("legacy_collections", {})
    _require(all(name in rig.data.collections_all for name in groups), "原生补充集合身份已改变")
    for name, prior in groups.items():
        rig.data.collections_all[name].is_visible = bool(visible and prior)
    return {name: bool(visible and prior) for name, prior in groups.items()}


def _limb(rig, limb, side):
    record = read(rig)
    _require(limb in {"Arm", "Leg"} and side in {"L", "R"}, "请使用 Arm 或 Leg、L 或 R")
    sections = {"arms"} if limb == "Arm" else {"legs", "toes"}
    _require(not sections.isdisjoint(record["sections"]), "本控制层未覆盖该肢体")
    selector, prop, ik_value = _mode_spec(record["family"], limb, side)
    groups = [item["collection"] for item in record["controls"].values()
              if item["mode"] is not None and item["mode"][:2] == [selector, prop]]
    _require(groups, "本骨架未声明该侧肢体模块")
    _require(all(group in rig.data.collections_all for group in groups), "动画控制集合缺失")
    return record["family"], rig.pose.bones[selector], prop, ik_value


def _assign_manual_mode(rig, owner, prop, value):
    path = owner.path_from_id() + '["' + bpy.utils.escape_identifier(prop) + '"]'
    animation = rig.animation_data
    if animation:
        _require(not animation.nla_tracks and not any(curve.data_path == path for curve in animation.drivers),
                 "模式由 NLA 或驱动管理，须从其动画来源切换")
        if animation.action:
            _require(animation.action_slot is not None, "当前 Action 没有明确 Slot")
            _, bag = _bag(animation.action, animation.action_slot.identifier)
            _require(not any(curve.data_path == path for curve in bag.fcurves),
                     "模式已有关键帧，通过原生 Action 管理")
    owner[prop] = type(owner[prop])(value)
    rig.update_tag()
    bpy.context.view_layer.update()



def _pose_update(rig):
    rig.update_tag()
    bpy.context.view_layer.update()
    return rig.evaluated_get(bpy.context.evaluated_depsgraph_get())


def _pose_channels(bone):
    return (bone.rotation_mode, *(tuple(getattr(bone, field)) for field in _POSE_FIELDS))


def _restore_pose(bone, state):
    rotation, *values = state
    bone.rotation_mode = rotation
    for field, value in zip(_POSE_FIELDS, values):
        if tuple(getattr(bone, field)) != value:
            setattr(bone, field, value)


def _measure_points(rig, names):
    evaluated = _pose_update(rig)
    world = evaluated.matrix_world
    positions = {}
    for name in names:
        bone = evaluated.pose.bones[name]
        radius = min(.002, bone.bone.length * .1)
        matrix = world @ bone.matrix
        points = [world @ bone.head, world @ bone.tail, matrix @ Vector((radius, 0, 0)),
                  matrix @ Vector((0, 0, radius))]
        segments = bone.bone.bbone_segments
        if segments > 1:
            for index in range(segments + 1):
                frame = matrix @ bone.bbone_segment_matrix(index, rest=False)
                points.extend((frame.translation, frame @ Vector((radius, 0, 0)),
                               frame @ Vector((0, 0, radius))))
        _require(all(isfinite(v) for point in points for v in point), f"求值异常 {name}")
        positions[name] = points
    return positions


def _measure_errors(before, after, scale_length):
    errors = {name: max((a - b).length for a, b in zip(points, after[name], strict=True)) * scale_length
              for name, points in before.items()}
    worst = max(errors, key=errors.get)
    return {"sample_max_meters": errors[worst], "worst_bone": worst, "bone_count": len(errors),
            "sample_count": sum(map(len, before.values())), "per_bone_max_meters": errors}


def _c94_matching(rig, limb, side, target, align_fingers):
    from . import pose_switching as native
    native._metadata(rig)
    if limb == "Arm":
        spec, _, _ = native._arm(rig, side)
        names = (*spec["fk"], spec["target"], spec["pole"], *(p[0] for p in spec["fingers"]))

        def solve():
            if target == "IK":
                native._to_ik(rig, spec, _pose_update(rig), align_fingers)
                rig.pose.bones["骨架设置"][spec["mode_key"]] = 1.0
            else:
                native._to_fk(rig, spec)
    else:
        from . import pose_switching_leg as native_leg
        spec, _, start = native_leg._validate(rig, side)
        names = (*spec["fk"], *(spec[key] for key in ("main", "pole", "foot", "toe_ik", "heel_ik", "toe")))

        def solve():
            evaluated = _pose_update(rig)
            native_leg._blend_values(rig, spec, evaluated, start)
            _require(all(native_leg._unit_matrix(evaluated.pose.bones[n].matrix) for n in (*spec["mixed"], spec["toe"]))
                     and all(abs(v - 1) <= 1e-3 for n in names for v in rig.pose.bones[n].scale),
                     "腿部含拉伸、剪切或非单位缩放")
            if target == "IK":
                native_leg._to_ik(rig, spec, native._channels(rig.pose.bones[spec["main"]]))
            else:
                native_leg._to_fk(rig, spec)
            native_leg._blend_values(rig, spec, _pose_update(rig), 1 if target == "IK" else 0)
    return names, spec["mixed"], solve


def _rigify_tools():
    import math
    import rigify
    root = Path(bpy.utils.system_resource("SCRIPTS", path="addons_core")).resolve()
    _require(tuple(rigify.bl_info["version"]) == (0, 6, 10)
             and Path(rigify.__file__).resolve().is_relative_to(root / "rigify"),
             "保姿态匹配需要随 Blender 发行的已验证 Rigify 0.6.10")
    from rigify import rig_ui_template
    from rigify.rigs.limbs import limb_rigs
    from rigify.utils import animation
    # These are frozen utility sources shipped by Blender. The scene's UI Text and its operators
    # are never executed. Stop at the base solver so the host's operators are never registered.
    env = {"bpy": bpy, "math": math, "pi": pi, "Vector": Vector, "Matrix": Matrix,
           "StringProperty": bpy.props.StringProperty,
           "set_transform_from_matrix": animation.set_transform_from_matrix}
    blocks = [rig_ui_template.UI_BASE_UTILITIES % "rigging_agent",
              *limb_rigs.SCRIPT_UTILITIES_OP_SNAP_IK_FK[:-1],
              limb_rigs.SCRIPT_UTILITIES_OP_SNAP_IK_FK[-1].split("class POSE_OT_", 1)[0]]
    for index, block in enumerate(blocks):
        exec(compile(block, f"<Blender Rigify utility {index}>", "exec"), env)
    return animation, env["RigifyLimbIk2FkBase"]


def _rigify_matching(rig, limb, side, target, owner):
    from . import pose_switching as native
    info = _metadata(rig, "rigging_modular_v1", "rigging.modular.generated.v1")
    _require(info.get("rigify", {}).get("version") == [0, 6, 10], "原生 Rigify 生成版本未经验证")
    suffix = "." + side
    if limb == "Arm":
        fk_names = ("upper_arm_fk", "forearm_fk", "hand_fk")
        ik_names = ("upper_arm_ik", "MCH-forearm_ik", "MCH-upper_arm_ik_target")
        control_names = ("upper_arm_ik", "upper_arm_ik_target", "hand_ik")
        tail_names, extra_names = (), ()
        mixed_names = ("ORG-upper_arm", "ORG-forearm", "ORG-hand")
    else:
        fk_names = ("thigh_fk", "shin_fk", "foot_fk", "toe_fk")
        ik_names = ("thigh_ik", "MCH-shin_ik", "MCH-thigh_ik_target")
        control_names = ("thigh_ik", "thigh_ik_target", "foot_ik")
        tail_names = ("toe_ik",)
        extra_names = ("foot_heel_ik", "foot_spin_ik")
        mixed_names = ("ORG-thigh", "ORG-shin", "ORG-foot", "ORG-toe")
    fk, ik, controls, tail, extra, mixed = (tuple(n + suffix for n in chain) for chain in
                                            (fk_names, ik_names, control_names, tail_names, extra_names, mixed_names))
    names = (*fk, *controls, *tail, *extra)
    _require(all(n in rig.pose.bones for n in (*names, *ik, *mixed))
             and "pole_vector" in owner, "缺少已验证 Rigify 匹配链")
    constraints = [c for c in rig.pose.bones[ik[1]].constraints if c.type == "IK"]
    _require(len(constraints) == 2 and all(c.target == rig and c.chain_count == 2 for c in constraints)
             and any(c.pole_target == rig and c.pole_subtarget == controls[1] for c in constraints),
             "Rigify IK 两骨链的目标或极点已改变")
    animation, solver_type = _rigify_tools()

    def solve():
        ev = _pose_update(rig)
        # A straight chain has no stable bend plane. Refuse before resizing or moving controls.
        native._plane(ev.pose.bones[mixed[0]].head, ev.pose.bones[mixed[1]].head,
                      ev.pose.bones[mixed[1]].tail)
        if target == "FK":
            desired = [ev.pose.bones[n].matrix.copy() for n in (*ik, *tail)]
            for n, matrix in zip(fk, desired, strict=True):
                animation.set_transform_from_matrix(rig, n, matrix, undo_copy_scale=True)
                _pose_update(rig)
            owner["IK_FK"] = 1.0
        else:
            desired = [ev.pose.bones[n].matrix.copy() for n in fk]
            solver = solver_type()
            solver.prop_bone, solver.pole_prop = owner.name, "pole_vector"
            solver.fk_bone_list, solver.ik_bone_list, solver.ctrl_bone_list = fk, ik, controls
            solver.tail_bone_list, solver.extra_ctrl_list = tail, extra
            solver.keyflags = None
            solver.apply_frame_state(bpy.context, rig, desired)
            if limb == "Arm" and not owner["pole_vector"] and rig.pose.bones[controls[0]].rotation_mode == "ZXY":
                from .pose_match_pair import refine_base_direction
                refine_base_direction(rig, "upper_arm", side, desired[0])
            owner["IK_FK"] = 0.0
    return names, mixed, solve


def _identity_driver_curve(curve):
    if curve.modifiers or curve.sampled_points or curve.extrapolation != "LINEAR":
        return False
    keys = curve.keyframe_points
    return (len(keys) == 2 and all(tuple(key.co) == (i, i)
            and key.interpolation in {"LINEAR", "BEZIER"}
            and (key.interpolation == "LINEAR"
                 or all(abs(p.x - p.y) < 1e-8 for p in (key.handle_left, key.handle_right)))
            for i, key in enumerate(keys)))


def _controlled_links(rig, names, *, allow_muted=False):
    selected = {item["source"]: item["bone"] for item in read(rig)["controls"].values()
                if item["source"] in names}
    curves = {(fc.data_path, fc.array_index): fc for fc in rig.animation_data.drivers}
    links, owned_curves = {}, []
    for old_name, new_name in selected.items():
        source, control = rig.pose.bones[old_name], rig.pose.bones.get(new_name)
        _require(control is not None and source.rotation_mode == control.rotation_mode,
                 f"控制骨的旋转表示已改变 {old_name}")
        links[old_name] = control
        for field in _channels(source):
            for index in range(len(getattr(source, field))):
                curve = curves.get((source.path_from_id(field), index))
                _require(curve is not None and (allow_muted or not curve.mute) and curve.driver.type == "AVERAGE"
                         and _identity_driver_curve(curve)
                         and len(curve.driver.variables) == 1, f"统一控件驱动身份已改变 {old_name}")
                var = curve.driver.variables[0]
                target = var.targets[0]
                _require(var.type == "SINGLE_PROP" and target.id == rig
                         and target.data_path == f"{control.path_from_id(field)}[{index}]",
                         f"统一控件的实际输入已改变 {old_name}")
                owned_curves.append(curve)
    _require(links, "切换没有找到实际驱动的统一控件")
    return links, owned_curves



def verified_retarget_driver_keys(rig):
    """Object driver's exact keys owned by the target's verified control layer."""
    if CONTROL_KEY not in rig:
        return frozenset()
    sources = {item["source"] for item in read(rig)["controls"].values()}
    _, curves = _controlled_links(rig, sources, allow_muted=rig.session_uid in _RETARGET_INPUTS_ACTIVE)
    return frozenset((curve.data_path, curve.array_index) for curve in curves)


@contextmanager
def native_retarget_inputs(rig):
    """Expose native inputs to existing solve code, then restore the owned bridge.

    The caller owns frame, modes, Action/Slot, and ordinary pose/dynamic restoration.
    The path projection is used when writing the single durable candidate Action.
    """
    if CONTROL_KEY not in rig:
        yield {}, frozenset()
        return
    _require(rig.session_uid not in _RETARGET_INPUTS_ACTIVE, "同一角色的重定向输入已被另一转换占用")
    sources = {item["source"] for item in read(rig)["controls"].values()}
    links, curves = _controlled_links(rig, sources)
    projection = {rig.pose.bones[name].path_from_id(field): ctrl.path_from_id(field)
                  for name, ctrl in links.items() for field in _channels(ctrl)}
    owned = frozenset((curve.data_path, curve.array_index) for curve in curves)
    _RETARGET_INPUTS_ACTIVE.add(rig.session_uid)
    try:
        for curve in curves:
            curve.mute = True
        for name, ctrl in links.items():
            bone = rig.pose.bones[name]
            for field in _channels(ctrl):
                setattr(bone, field, tuple(getattr(ctrl, field)))
        _pose_update(rig)
        _require(all(tuple(getattr(rig.pose.bones[name], field)) == tuple(getattr(ctrl, field))
                     for name, ctrl in links.items() for field in _channels(ctrl)),
                 "重定向不能从经验证的统一控件姿态启动")
        yield projection, owned
    finally:
        _RETARGET_INPUTS_ACTIVE.discard(rig.session_uid)
        for curve in curves:
            curve.mute = False
        _pose_update(rig)


def _match_mode(rig, limb, side, target, owner, prop, value, family, align_fingers, limit):
    from . import pose_switching as native
    _require(type(limit) in {int, float} and isfinite(limit) and limit > 0, "匹配误差限值须为正数米值")
    _require(type(align_fingers) is bool, "手指匹配选项须使用布尔值")
    scale = bpy.context.scene.unit_settings.scale_length
    _require(isfinite(scale) and scale > 0, "场景米制比例无效")
    _require(rig.data.pose_position == "POSE" and rig.mode in {"POSE", "OBJECT"} and not rig.library
             and not rig.data.library and rig.data.users == 1
             and rig.name in bpy.context.view_layer.objects, "需使用当前视图层内可编辑的独立姿态骨架")
    animation = rig.animation_data
    _require(animation and animation.action is None and not animation.nla_tracks and not animation.use_tweak_mode,
             "保姿态切换当前仅支持无已绑定 Action 或 NLA 的静态姿态")
    original_mode = owner[prop]
    _require(float(original_mode) in {0, 1}, "须先把模式设在完整 IK 或 FK 端点")
    path = owner.path_from_id() + '["' + bpy.utils.escape_identifier(prop) + '"]'
    _require(not any(curve.data_path == path for curve in animation.drivers), "模式已由其他驱动管理")
    if family == "c94":
        names, mixed, solve = _c94_matching(rig, limb, side, target, align_fingers)
    else:
        names, mixed, solve = _rigify_matching(rig, limb, side, target, owner)
    names = tuple(dict.fromkeys(names))
    links, curves = _controlled_links(rig, names)
    input_paths = {rig.pose.bones[n].path_from_id(field) for n in names
                   for field in _channels(rig.pose.bones[n])}
    allowed_curves = {(curve.data_path, curve.array_index) for curve in curves}
    _require(not any(curve.data_path in input_paths and (curve.data_path, curve.array_index) not in allowed_curves
                     for curve in animation.drivers), "匹配输入由其他驱动管理")
    witness = tuple(dict.fromkeys((*[bone.name for bone in rig.data.bones if bone.use_deform], *mixed)))
    before = _measure_points(rig, witness)
    originals = {n: _pose_channels(rig.pose.bones[n]) for n in names}
    controls = {n: _pose_channels(bone) for n, bone in links.items()}
    # Older Bézier driver keys can round inputs close to their endpoints.
    # Hand over the inputs consumed by the same evaluation as the pose witness.
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    native_inputs = {n: {field: tuple(getattr(evaluated.pose.bones[n], field)) for field in _channels(ctrl)}
                     for n, ctrl in links.items()}
    report = {"ok": False, "status": "rejected", "rig": rig.name, "limb": limb.upper(), "side": side,
              "target": target, "mode_before": float(original_mode), "max_error_meters": limit,
              "auto_keyed": False, "rest_modified": False, "restored": False}
    try:
        for curve in curves:
            curve.mute = True
        for n, channels in native_inputs.items():
            source = rig.pose.bones[n]
            for field, values in channels.items():
                setattr(source, field, values)
        bridge = _measure_errors(before, _measure_points(rig, witness), scale)
        report["input_bridge"] = {key: item for key, item in bridge.items() if key != "per_bone_max_meters"}
        evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        _require(all(tuple(getattr(evaluated.pose.bones[n], field)) == values
                     for n, channels in native_inputs.items() for field, values in channels.items()),
                 "临时接管改变了原生求值通道")
        _require(bridge["sample_max_meters"] <= _INPUT_BRIDGE_TOLERANCE_METERS,
                 f"接管旧通道后 {bridge['worst_bone']} 偏移 {bridge['sample_max_meters']:.9f} 米")
        solve()
        _pose_update(rig)
        changed = []
        for n, ctrl in links.items():
            source = rig.pose.bones[n]
            _require(source.rotation_mode == ctrl.rotation_mode, f"求解改变受管控件的旋转表示 {n}")
            for field, previous in native_inputs[n].items():
                authored = tuple(getattr(ctrl, field))
                solved = tuple(getattr(source, field))
                values = tuple(new if new != initial else raw
                               for new, initial, raw in zip(solved, previous, authored, strict=True))
                if values != authored:
                    setattr(ctrl, field, values)
            if _pose_channels(ctrl) != controls[n]:
                changed.append(ctrl.name)
        for curve in curves:
            curve.mute = False
        result = _measure_errors(before, _measure_points(rig, witness), scale)
        report["metrics"] = result
        report["changed_inputs"] = changed
        report["native_helper_inputs"] = [n for n in names if n not in links
                                           and _pose_channels(rig.pose.bones[n]) != originals[n]]
        _require(float(_pose_update(rig).pose.bones[owner.name][prop]) == value, "新模式求值未采用")
        _require(changed or result["sample_max_meters"] <= 1e-6, "求解未迁入有效的新控制通道")
        _require(result["sample_max_meters"] <= limit,
                 f"姿态或同侧骨链偏移 {result['sample_max_meters']:.6f} 米，超出误差限值")
        report.update(ok=True, status="switched", mode_after=value)
        return report
    except Exception as exc:
        owner[prop] = original_mode
        for curve in curves:
            curve.mute = True
        for n in names:
            _restore_pose(rig.pose.bones[n], originals[n])
        for n, ctrl in links.items():
            _restore_pose(ctrl, controls[n])
        for curve in curves:
            curve.mute = False
        restored = _measure_errors(before, _measure_points(rig, witness), scale)["sample_max_meters"]
        if (restored > 1e-6 or owner[prop] != original_mode or any(curve.mute for curve in curves)
                or any(_pose_channels(ctrl) != controls[n] for n, ctrl in links.items())
                or any(_pose_channels(rig.pose.bones[n]) != originals[n] for n in names)):
            raise RuntimeError("匹配失败且旧姿态恢复未通过，请使用上次检查点") from exc
        report.update(restored=True, restore_error_meters=restored)
        raise native.PoseSwitchError(f"{exc}。已恢复切换前姿态", report) from exc


def set_limb_mode(rig, *, limb, side, mode, preserve_pose=True, align_fingers=True, max_error_meters=.001):
    """Switch the native selector with an optional measured static-pose transaction."""
    _require(mode in {"FK", "IK"}, "模式须为 FK 或 IK")
    _require(type(preserve_pose) is bool, "保姿态选项须使用布尔值")
    family, owner, prop, ik_value = _limb(rig, limb, side)
    value = ik_value if mode == "IK" else 1 - ik_value
    report = {"ok": True, "status": "no_change", "auto_keyed": False}
    if float(owner[prop]) != value:
        if preserve_pose:
            report = _match_mode(rig, limb, side, mode, owner, prop, value, family,
                                 align_fingers, max_error_meters)
        else:
            _assign_manual_mode(rig, owner, prop, value)
            report["status"] = "switched"
    return {**report, "mode": mode, "collections": refresh_visibility(rig, limb=limb, side=side)}


def set_pole_enabled(rig, *, limb, side, enabled, preserve_pose=True):
    """Toggle Rigify's native pole vector; C94's mapped pole is always enabled."""
    family, owner, _, _ = _limb(rig, limb, side)
    _require(type(enabled) is bool, "极点开关须使用 bool")
    if family == "c94":
        _require(enabled, "C94 的原生 IK 使用已配置的极点，没有独立开关")
    elif bool(owner["pole_vector"]) != enabled:
        _require(not preserve_pose, "极点保姿态切换尚未验证；仅可明确选择直接切换")
        _assign_manual_mode(rig, owner, "pole_vector", enabled)
    return {"enabled": enabled, "collections": refresh_visibility(rig, limb=limb, side=side)}


def _geometry(kind):
    if kind in _SHAPES:
        return _SHAPES[kind]
    return tuple((cos(i * pi / 12), sin(i * pi / 12)) for i in range(24))


def _widget(bucket, kind, owned):
    xy = _geometry(kind)
    ring = [(i, (i + 1) % len(xy)) for i in range(len(xy))]
    if kind in {"root", "foot"}:
        # A shallow wireframe keeps floor handles readable from floor-level views.
        vertices = [(x, y, z) for z in (-.008, .008) for x, y in xy]
        edges = ring + [(len(xy) + a, len(xy) + b) for a, b in ring]
        edges += [(i, i + len(xy)) for i in range(0, len(xy), max(1, len(xy) // 4))]
    else:
        vertices = [(x, y, 0) for x, y in xy]
        edges = ring
    mesh = bpy.data.meshes.new("Animation Widget · " + kind)
    owned["meshes"].append(mesh)
    mesh.from_pydata(vertices, edges, [])
    obj = bpy.data.objects.new(mesh.name, mesh)
    owned["objects"].append(obj)
    bucket.objects.link(obj)
    obj.hide_render = True
    obj.hide_set(True)
    return obj


def _configure_widget(pose, widget, kind, side, height, anchor):
    pose.custom_shape = widget
    pose.use_custom_shape_bone_size = False
    size_x, size_y = _DIMENSIONS[kind]
    if kind == "fk":
        size = min(size_x * height, max(.014 * height, .28 * pose.bone.length))
        pose.custom_shape_scale_xyz = (size, size, size)
    else:
        pose.custom_shape_scale_xyz = (size_x * height, size_y * height, height)
    flat = Matrix.Identity(3)
    vertical = Matrix.Rotation(pi / 2, 3, "X")
    if kind == "fk":
        orientation = vertical
    else:
        desired = flat if kind in {"root", "foot", "hips", "chest", "toe", "toe_fk"} else vertical
        orientation = pose.bone.matrix_local.to_3x3().normalized().inverted() @ desired
    pose.custom_shape_rotation_euler = orientation.to_euler("XYZ")
    if anchor is not None:
        pose.custom_shape_translation = pose.bone.matrix_local.inverted() @ anchor
    pose.bone.color.palette = "CUSTOM"
    colors = pose.bone.color.custom
    colors.normal = _PALETTE[side]
    colors.select = tuple(min(1, channel * .65 + .35) for channel in colors.normal)
    colors.active = (1, .98, .82)


def _anchors(rig, family, specs, ground, height):
    anchors = {}
    if "Torso" in specs:
        hips, chest = (("形变-骶骨", "形变-胸腔1") if family == "c94" else ("DEF-spine", "DEF-spine.002"))
        base, top = (rig.data.bones.get(name) for name in (hips, chest))
        _require(base is not None and top is not None, "无法从已验证的身躯形变骨确定视觉锚点")
        a = base.head_local
        b = top.head_local.lerp(top.tail_local, .50)
        anchors = {"Hips": a.copy(), "Torso": a.lerp(b, .50), "Chest": b}
    if "Root" in specs and any(role.startswith("Foot IK.") for role in specs):
        root = rig.data.bones[specs["Root"][0]].head_local
        anchors["Root"] = Vector((root.x, root.y, ground - .018 * height))
    for role, (source, shape, _, _, _) in specs.items():
        if shape in {"upper_lid", "lower_lid"}:
            direction = 1 if shape == "upper_lid" else -1
            anchors[role] = rig.data.bones[source].head_local + Vector((0, 0, direction * .006 * height))
    return anchors


def _add_bones(rig, specs, owned):
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for role, (source, _, _, _, _) in specs.items():
            edit = rig.data.edit_bones[source]
            new = rig.data.edit_bones.new("CTRL-" + role)
            owned["bones"].append(new.name)
            new.head = edit.head
            new.tail = edit.tail
            new.roll = edit.roll
            new.parent = edit.parent
            new.use_connect = edit.use_connect
            new.inherit_scale = edit.inherit_scale
            new.use_inherit_rotation = edit.use_inherit_rotation
            new.use_local_location = edit.use_local_location
            new.use_deform = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def _wire(rig, source, control, owned):
    control.rotation_mode = source.rotation_mode
    for field in ("lock_location", "lock_rotation", "lock_scale", "lock_rotations_4d", "lock_rotation_w"):
        setattr(control, field, getattr(source, field))
    for field in _channels(source):
        values = tuple(getattr(source, field))
        setattr(control, field, values)
        for index in range(len(values)):
            curve = source.driver_add(field, index)
            owned["drivers"].append((source.name, field, index))
            driver = curve.driver
            driver.type = "AVERAGE"
            var = driver.variables.new()
            var.name = "input"
            var.type = "SINGLE_PROP"
            target = var.targets[0]
            target.id = rig
            target.data_path = f"{control.path_from_id(field)}[{index}]"


def _max_error(left, right):
    return max(abs(a - b) for r1, r2 in zip(left, right) for a, b in zip(r1, r2))


def _matrix_snapshot(rig, names):
    rig.update_tag()
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return {name: evaluated.matrix_world @ evaluated.pose.bones[name].matrix for name in names}



def _nla_channel_value(rig, path, index):
    try:
        value = rig.path_resolve(path)
        if isinstance(value, (int, float, bool)):
            return index == 0
        return index >= 0 and isinstance(value[index], (int, float, bool))
    except (ValueError, TypeError, IndexError, KeyError, AttributeError):
        return False


def _validate_preserved_nla(rig, animation, bridge_paths):
    if animation is None:
        return None
    _require(not animation.use_tweak_mode, "NLA Tweak 模式需要先结束后再准备统一控件")
    tracks = animation.nla_tracks
    if not tracks:
        return None
    # Preserve a simple, fully readable clip, even when muted. A muted clip
    # might be enabled later, and its channels must remain independent too.
    _require(len(tracks) == 1 and not tracks[0].is_solo and len(tracks[0].strips) == 1,
             "NLA 只支持一个可验证的普通条带，未知组合需要单独交接")
    track = tracks[0]
    strip = track.strips[0]
    _require(strip.type == "CLIP" and strip.blend_type == "REPLACE"
             and strip.extrapolation in {"NOTHING", "HOLD", "HOLD_FORWARD"}
             and not strip.use_animated_influence and not strip.use_animated_time
             and not strip.use_animated_time_cyclic and not strip.use_auto_blend
             and not strip.use_reverse and not strip.fcurves and not strip.modifiers
             and strip.repeat == strip.scale == 1 and strip.blend_in == strip.blend_out == 0,
             "NLA 条带的混合、重映时间或修饰设置未经验证")
    span = (strip.frame_start, strip.frame_end, strip.action_frame_start, strip.action_frame_end)
    _require(all(isfinite(value) for value in span) and span[0] < span[1] and span[2] < span[3]
             and abs((span[1] - span[0]) - (span[3] - span[2])) <= 1e-5,
             "NLA 条带时域未经验证")
    action = strip.action
    slot = strip.action_slot
    _require(action is not None and slot is not None and slot.handle == strip.action_slot_handle
             and action.slots.get(slot.identifier) == slot, "NLA 缺少有效的实际 Action Slot")
    try:
        _, bag = _bag(action, slot.identifier)
    except ValueError as exc:
        raise ValueError(f"NLA Action Slot 无法验证 {strip.name}：{exc}") from exc
    _require(bag.fcurves, "NLA Action Slot 没有可验证的动画通道")
    channels = [(curve.data_path, curve.array_index) for curve in bag.fcurves]
    _require(len(set(channels)) == len(channels), "NLA Action Slot 含重复通道")
    conflict = next((path for path, _ in channels if path in bridge_paths), None)
    _require(conflict is None, f"NLA 通道与统一控件写域重叠：{conflict}")
    invalid = next((path for path, index in channels if not _nla_channel_value(rig, path, index)), None)
    _require(invalid is None, f"NLA Action Slot 含无法验证的目标通道：{invalid}")
    return {"track": track.name, "strip": strip.name, "action": action.name,
            "slot": slot.identifier, "channel_count": len(channels)}


def build(rig, *, family, sections=("body", "arms", "legs"), action_policy="reject", owned_ids=None):
    """Build real controls on an independent local rig in Object Mode.

    Only an explicit remap policy can replace the active Action binding, using
    a separate Action and the same Slot while keeping the original as a native
    datablock reference. A simple verified NLA strip can be retained if
    its channels are disjoint from the bridge. Blended active Actions need a separate handoff.
    """
    context = bpy.context
    _require(owned_ids is None or isinstance(owned_ids, list), "owned_ids 须为调用者可写的 list 或 None")
    _require(isinstance(rig, bpy.types.Object) and rig.type == "ARMATURE", "需要明确的骨架对象")
    data_users = bpy.data.user_map(subset=(rig.data,)).get(rig.data, set())
    owners = {obj for obj in data_users if isinstance(obj, bpy.types.Object) and obj.data == rig.data}
    _require(rig.library is None and rig.data.library is None and owners == {rig},
             "需使用本地独立骨架副本")
    _require(context.mode == "OBJECT" and context.view_layer.objects.get(rig.name) == rig
             and rig.visible_get() and not rig.hide_select, "请在对象模式使用当前视图层可见、可选的骨架")
    _require(CONTROL_KEY not in rig and SOURCE_ACTION_KEY not in rig, "当前候选已经有动画控制层")
    _require(action_policy in {"reject", "remap"}, "Action 策略须明确为 reject 或 remap")
    sections = tuple(sections)
    specs = _controller_specs(rig, family, sections)
    originals = {}
    for role, (source, shape, _, side, mode) in specs.items():
        control_name = "CTRL-" + role
        pb = rig.pose.bones.get(source)
        # The verified native C94 toe FK has no custom shape of its own.
        widgetless_toe = family == "c94" and shape == "toe_fk" and source == f"脚趾.{side}"
        _require(pb is not None and not pb.bone.use_deform and (pb.custom_shape is not None or widgetless_toe)
                 and not pb.bone.hide_select and control_name not in rig.data.bones,
                 f"原生控制骨与已验证映射不符或目标同名 {source}")
        _mode_visible(rig, mode)
        originals[source] = bool(pb.bone.hide)
    groups = tuple(dict.fromkeys(_PREFIX + item[2] for item in specs.values()))
    root_name = _PREFIX + "控制集合"
    _require(not any(name in rig.data.collections_all for name in (*groups, root_name)), "动画控制集合名称冲突")
    ad = rig.animation_data
    action = ad.action if ad else None
    mapping = {role: item[0] for role, item in specs.items()}
    paths, blocked = _path_map(rig, mapping)
    nla = _validate_preserved_nla(rig, ad, set(paths) | blocked | set(paths.values()))
    _require(ad is None or not any(fc.data_path in paths for fc in ad.drivers), "原生变换通道已有驱动")
    old_slot, action_plan = None, None
    if action is not None:
        _require(action_policy == "remap" and ad.action_slot is not None and ad.action_blend_type == "REPLACE"
                 and ad.action_influence == 1, "现有 Action 需确认独立转换，混合动作须另行交接")
        old_slot = ad.action_slot
        action_plan = _action_plan(rig, action, old_slot.identifier, mapping)
    deformation = tuple(b.name for b in rig.data.bones if b.use_deform)
    all_z = [rest.z for name in deformation for rest in (rig.data.bones[name].head_local, rig.data.bones[name].tail_local)]
    _require(all_z and max(all_z) - min(all_z) > 1e-5, "无法从形变骨读取角色高度")
    height = max(all_z) - min(all_z)
    anchors = _anchors(rig, family, specs, min(all_z), height)
    before = _matrix_snapshot(rig, deformation)
    legacy = {c.name: bool(c.is_visible) for c in rig.data.collections if c.parent is None}
    old_active = context.view_layer.objects.active
    old_selection = tuple(context.selected_objects)
    candidate, widget_collection = None, None
    owned = {"bones": [], "drivers": [], "collections": [], "objects": [], "meshes": []}
    try:
        if action_plan:
            identifier, paths, count = action_plan
            candidate, slot = _copy_action(action, identifier, paths)
        for obj in old_selection:
            obj.select_set(False)
        rig.select_set(True)
        context.view_layer.objects.active = rig
        _add_bones(rig, specs, owned)
        root = rig.data.collections.new(root_name)
        owned["collections"].append(root)
        sub = {}
        for group in groups:
            sub[group] = rig.data.collections.new(group, parent=root)
            owned["collections"].append(sub[group])
        widget_collection = bpy.data.collections.new(rig.name + " · 动画控件图形")
        context.scene.collection.children.link(widget_collection)
        widget_collection.hide_render = True
        widgets = {}
        record = {"schema": "rigging.agent.controls.v1", "family": family, "sections": list(sections),
                  "controls": {}, "action": None, "legacy_collections": legacy}
        if nla is not None:
            record["preserved_nla"] = nla
        for role, (source, shape, group, side, mode) in specs.items():
            control_name = "CTRL-" + role
            collection = sub[_PREFIX + group]
            pose = rig.pose.bones[control_name]
            for inherited in list(pose.bone.collections):
                inherited.unassign(pose.bone)
            collection.assign(pose.bone)
            collection.is_visible = _mode_visible(rig, mode)
            if shape not in widgets:
                widgets[shape] = _widget(widget_collection, shape, owned)
            _configure_widget(pose, widgets[shape], shape, side, height, anchors.get(role))
            src = rig.pose.bones[source]
            _wire(rig, src, pose, owned)
            if shape == "pole":
                pose.lock_rotation = (True, True, True)
                pose.lock_scale = (True, True, True)
            if shape == "toe" and family == "c94":
                pose.lock_location = (True, True, True)
                pose.lock_scale = (True, True, True)
            src.bone.hide = True
            record["controls"][role] = {"source": source, "bone": control_name,
                                         "collection": collection.name, "widget": shape, "mode": mode}
        if candidate is not None:
            rig[SOURCE_ACTION_KEY] = action
            ad.action = candidate
            ad.action_slot = slot
            record["action"] = {"source": action.name, "candidate": candidate.name,
                                "slot": slot.identifier, "mapped_curves": count}
        for name in legacy:
            rig.data.collections_all[name].is_visible = False
        after = _matrix_snapshot(rig, deformation)
        max_error = max(_max_error(before[n], after[n]) for n in deformation)
        _require(max_error <= max(2e-5, height * 2e-5), f"控制层接入改变基础求值 {max_error:.8g}")
        new_paths = {rig.pose.bones[n].path_from_id(p) for n, p, _ in owned["drivers"]}
        _require(all(fc.driver.is_valid for fc in rig.animation_data.drivers if fc.data_path in new_paths),
                 "新控制驱动存在依赖图求值错误")
        record["reference_pose_max_error"] = max_error
        rig[CONTROL_KEY] = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if owned_ids is not None:
            owned_ids.extend((widget_collection, *owned["objects"], *owned["meshes"]))
            if candidate is not None:
                owned_ids.append(candidate)
        return record
    except Exception:
        if candidate is not None:
            ad.action = action
            ad.action_slot = old_slot
            if SOURCE_ACTION_KEY in rig:
                del rig[SOURCE_ACTION_KEY]
            bpy.data.actions.remove(candidate)
        for name, field, index in reversed(owned["drivers"]):
            pose = rig.pose.bones[name]
            pose.driver_remove(field, index)
        if ad is None and rig.animation_data is not None:
            rig.animation_data_clear()
        for name, state in legacy.items():
            rig.data.collections_all[name].is_visible = state
        for name, hidden in originals.items():
            rig.pose.bones[name].bone.hide = hidden
        if owned["bones"]:
            bpy.ops.object.mode_set(mode="EDIT")
            try:
                for name in reversed(owned["bones"]):
                    rig.data.edit_bones.remove(rig.data.edit_bones[name])
            finally:
                bpy.ops.object.mode_set(mode="OBJECT")
        for collection in reversed(owned["collections"]):
            rig.data.collections.remove(collection)
        for obj in owned["objects"]:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in owned["meshes"]:
            bpy.data.meshes.remove(mesh)
        if widget_collection is not None:
            bpy.data.collections.remove(widget_collection)
        raise
    finally:
        rig.select_set(False)
        for obj in old_selection:
            obj.select_set(True)
        context.view_layer.objects.active = old_active
