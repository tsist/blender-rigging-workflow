# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional shoulder carriers for a fitted C94 rig with a frozen neutral pose."""

import json
import math

import bpy
from mathutils import Vector

KEY = "rigging_agent_shoulder_girdle_v1"
ENABLE_KEY = "rigging_agent_shoulder_girdle_enable_v1"
SCHEMA = "rigging.agent.shoulder-girdle.v1"
_COLLECTION = "Animator · 肩带"
_FIT_TOLERANCE = 1e-5
_NEUTRAL_TOLERANCE = 3e-5
_SIDES = ("L", "R")
_LANDMARKS = {"SC", "AC", "GH", "INF"}
_BRANCHES = ("机制-大臂", "机制-大臂FK-父级-肩膀", "大臂IK", "大臂IK-极点", "扭转-大臂4")


class _NeutralDrift(ValueError):
    def __init__(self, difference):
        self.difference = difference
        super().__init__(f"安装改变已有中立骨矩阵：{difference:.6g}")


def _matrix_delta(before, after):
    return max(abs(a - b) for r1, r2 in zip(before, after) for a, b in zip(r1, r2))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _anchors(rig, landmarks):
    _require(isinstance(landmarks, dict) and set(landmarks) == set(_SIDES),
             "肩带拟合须显式提供左、右世界坐标")
    result = {}
    for side in _SIDES:
        row = landmarks[side]
        _require(isinstance(row, dict) and set(row) == _LANDMARKS,
                 f"肩带 {side} 须包含 SC、AC、GH、INF 四个标点")
        points = {}
        for key, value in row.items():
            if isinstance(value, (str, bytes)):
                raise ValueError(f"肩带 {side} {key} 须为三维坐标")
            try:
                coordinate = Vector(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"肩带 {side} {key} 须为三维坐标") from error
            _require(len(coordinate) == 3 and all(math.isfinite(v) for v in coordinate),
                     f"肩带 {side} {key} 须为有限三维坐标")
            points[key] = coordinate
        expected = rig.matrix_world @ rig.data.bones[f"机制-大臂.{side}"].head_local
        _require((expected - points["GH"]).length <= _FIT_TOLERANCE,
                 f"肩带 {side} 的 GH 须保留当前原生上臂起点")
        ac_to_inf = points["INF"] - points["AC"]
        ac_to_gh = points["GH"] - points["AC"]
        _require((points["AC"] - points["SC"]).length > _FIT_TOLERANCE
                 and ac_to_inf.length > _FIT_TOLERANCE and ac_to_gh.length > _FIT_TOLERANCE
                 and ac_to_inf.normalized().cross(ac_to_gh.normalized()).length > .01,
                 f"肩带 {side} 的锁骨或肩胛标点退化")
        result[side] = points
    return result


def _topology(rig):
    bones = rig.data.bones
    _require(_COLLECTION not in rig.data.collections_all, "肩带控件集合已存在")
    result = {}
    for side in _SIDES:
        source = bones.get(f"肩膀.{side}")
        _require(source is not None and not source.use_deform
                 and source.parent is not None and source.parent.name == "胸腔2",
                 f"肩带 {side} 的原生肩父级不匹配")
        controls = [f"CTRL-Shoulder SC.{side}", f"CTRL-Scapula.{side}"]
        space = f"MCH-Shoulder Carrier Space.{side}"
        _require(all(name not in bones for name in (*controls, space)), f"肩带 {side} 新骨名称冲突")
        children = [f"{name}.{side}" for name in _BRANCHES]
        pole = f"CTRL-Elbow Pole.{side}"
        if pole in bones:
            children.append(pole)
        for name in children:
            bone = bones.get(name)
            _require(bone is not None and bone.parent == source and not bone.use_connect,
                     f"原生分支 {name} 不在预期独立肩父级下，无法安全介入")
        result[side] = {"legacy": source.name, "controls": controls, "space": space, "children": children}
    return result


def _appearance(rig, layout, collection):
    for side, row in layout.items():
        color = (.18, .69, .92) if side == "L" else (.97, .36, .28)
        for name in row["controls"]:
            control = rig.pose.bones[name]
            control.rotation_mode = "QUATERNION"
            control.lock_location = (True, True, True)
            control.lock_scale = (True, True, True)
            control.bone.color.palette = "CUSTOM"
            control.bone.color.custom.normal = color
            control.bone.color.custom.select = (.96, .82, .36)
            control.bone.color.custom.active = (1., .92, .58)
            collection.assign(control.bone)


def _snapshot(rig, names):
    rig.update_tag()
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return {name: evaluated.matrix_world @ evaluated.pose.bones[name].matrix for name in names}


def _check_neutral(rig, before, layout, amount):
    rig[ENABLE_KEY] = amount
    after = _snapshot(rig, before)
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    for row in layout.values():
        influence = evaluated.pose.bones[row["space"]].constraints[0].influence
        _require(abs(influence - (1. - amount)) < 1e-6, "肩带旁路驱动未实际求值")
    drift = max(_matrix_delta(before[name], after[name]) for name in before)
    _require(math.isfinite(drift), "安装后的中立骨矩阵包含非有限值")
    if drift > _NEUTRAL_TOLERANCE:
        raise _NeutralDrift(drift)
    return drift


def _align_neutral_carrier(rig, layout):
    # A new parent chain adds a float round trip. The native two-bone IK can
    # amplify it near extension even when the solved arm is currently hidden.
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    targets = {row["space"]: evaluated.pose.bones[row["legacy"]].matrix.copy()
               for row in layout.values()}
    differences = {}
    for name, target in targets.items():
        carrier = rig.pose.bones[name]
        basis = carrier.matrix_basis.copy()
        carrier.matrix = target
        differences[name] = _matrix_delta(basis, carrier.matrix_basis)
    _require(all(delta <= _NEUTRAL_TOLERANCE for delta in differences.values()),
             "肩带载体需要超出中立门限的父空间校准")
    return differences


def install(rig, landmarks, *, owned_ids=None):
    """Install explicit SC/AC and scapular controls over existing C94 arm branches.

    Supply L and R dictionaries of world-space SC, AC, GH and INF positions.
    The existing humeral center is kept as a rest offset from the scapular
    control. The legacy native shoulder remains the upstream carrier and is
    directly restored by setting the rig's enable property to zero.

    Use this during generation on a disposable candidate before freezing its
    identity. Blender can reorder native bones when an edit session restores
    their original parents.

    The controls use native bones, collections and constraints on the supplied
    rig. No new Blender IDs are allocated. No action data is changed.
    """
    scene, layer = bpy.context.scene, bpy.context.view_layer
    _require(isinstance(rig, bpy.types.Object) and rig.type == "ARMATURE"
             and scene.objects.get(rig.name) == rig and layer.objects.get(rig.name) == rig
             and bpy.context.mode == "OBJECT", "肩带安装需要当前视图层的本地对象模式骨架")
    _require(rig.is_editable and not rig.library and not rig.override_library
             and rig.data.is_editable and not rig.data.library and not rig.data.override_library
             and rig.data.users == 1, "肩带安装需要独立可编辑的本地 Armature 数据")
    _require(owned_ids is None or isinstance(owned_ids, list), "owned_ids 须为 list 或 None")
    _require(KEY not in rig and ENABLE_KEY not in rig, "当前骨架已有肩带安装记录")
    try:
        generated = json.loads(rig.get("c94_generated_rig_v1", "{}"))
    except (TypeError, ValueError) as error:
        raise ValueError("C94 来源身份损坏") from error
    _require(isinstance(generated, dict) and generated.get("schema") == "c94.generated.v1"
             and isinstance(generated.get("reference_signature"), str), "当前骨架不具备已拟合 C94 来源身份")

    layout = _topology(rig)
    points = _anchors(rig, landmarks)
    reference = {key: generated[key] for key in ("reference_signature", "configuration_id")
                 if key in generated}
    result = {"schema": SCHEMA, "reference": reference, "enable_property": ENABLE_KEY, "sides": layout}
    for side, row in points.items():
        layout[side]["landmarks_world"] = {key: list(vector) for key, vector in sorted(row.items())}

    selected = list(bpy.context.selected_objects)
    active = layer.objects.active
    active_bone = rig.data.bones.active.name if rig.data.bones.active is not None else None
    before = _snapshot(rig, tuple(rig.data.bones.keys()))
    local = rig.matrix_world.inverted()
    collection = None
    created, reparented, constraints = [], [], []
    try:
        for obj in selected:
            obj.select_set(False)
        rig.select_set(True)
        layer.objects.active = rig
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            edit = rig.data.edit_bones
            for side, row in layout.items():
                old = edit[row["legacy"]]
                p = {key: local @ value for key, value in points[side].items()}
                clavicle = edit.new(row["controls"][0])
                created.append(clavicle.name)
                clavicle.head, clavicle.tail = p["SC"], p["AC"]
                clavicle.align_roll(old.matrix.to_3x3().col[2])
                clavicle.parent = old

                scapula = edit.new(row["controls"][1])
                created.append(scapula.name)
                scapula.head, scapula.tail = p["AC"], p["INF"]
                plane_normal = (p["GH"] - p["AC"]).cross(p["INF"] - p["AC"]).normalized()
                scapula.align_roll(plane_normal)
                scapula.parent = clavicle

                offset = edit.new(row["space"])
                created.append(offset.name)
                offset.head, offset.tail, offset.roll = old.head.copy(), old.tail.copy(), old.roll
                offset.parent = scapula
                for bone in (clavicle, scapula, offset):
                    bone.use_deform = False
                    bone.use_connect = False
                for name in row["children"]:
                    edit[name].parent = offset
                    reparented.append((name, old.name))
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")

        collection = rig.data.collections.new(_COLLECTION)
        _appearance(rig, layout, collection)
        rig[ENABLE_KEY] = 0.
        rig.id_properties_ui(ENABLE_KEY).update(min=0., max=1.,
            description="0 使用原 C94 肩部父空间，1 使用胸锁和肩胛控件")
        for row in layout.values():
            constraint = rig.pose.bones[row["space"]].constraints.new("COPY_TRANSFORMS")
            constraints.append((row["space"], constraint))
            constraint.name = "Legacy shoulder space"
            constraint.target = rig
            constraint.subtarget = row["legacy"]
            constraint.owner_space = constraint.target_space = "WORLD"
            constraint.mix_mode = "REPLACE"
            driver = constraint.driver_add("influence").driver
            driver.type, driver.expression = "SCRIPTED", "1.0 - enabled"
            variable = driver.variables.new()
            variable.name, variable.type = "enabled", "SINGLE_PROP"
            variable.targets[0].id = rig
            variable.targets[0].data_path = f'["{ENABLE_KEY}"]'
        legacy = _check_neutral(rig, before, layout, 0.)
        try:
            enabled = _check_neutral(rig, before, layout, 1.)
        except _NeutralDrift as error:
            correction = _align_neutral_carrier(rig, layout)
            _check_neutral(rig, before, layout, 1.)
            legacy_after = _check_neutral(rig, before, layout, 0.)
            enabled = _check_neutral(rig, before, layout, 1.)
            result["neutral_calibration"] = {
                "initial_matrix_delta": {"legacy": legacy, "enabled": error.difference},
                "carrier_basis_delta": correction,
            }
            legacy = legacy_after
        result["neutral_matrix_delta"] = {"legacy": legacy, "enabled": enabled}
        rig[KEY] = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return result
    except Exception:
        for space, constraint in constraints:
            constraint.driver_remove("influence")
            rig.pose.bones[space].constraints.remove(constraint)
        if collection is not None:
            rig.data.collections.remove(collection)
        if created:
            bpy.ops.object.mode_set(mode="EDIT")
            try:
                edit = rig.data.edit_bones
                for name, parent in reparented:
                    edit[name].parent = edit[parent]
                for name in reversed(created):
                    edit.remove(edit[name])
            finally:
                bpy.ops.object.mode_set(mode="OBJECT")
        for key in (KEY, ENABLE_KEY):
            if key in rig:
                del rig[key]
        rig.update_tag()
        layer.update()
        raise
    finally:
        rig.data.bones.active = rig.data.bones.get(active_bone) if active_bone else None
        rig.select_set(False)
        for obj in selected:
            obj.select_set(True)
        layer.objects.active = active
