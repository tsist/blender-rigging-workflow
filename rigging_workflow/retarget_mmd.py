# SPDX-License-Identifier: GPL-3.0-or-later
"""Slim MMD / PMX source profile for the frozen C94 retarget core.

This module deliberately does not add a second Action, preview, snapshot or
cancellation implementation.  It resolves explicit PMX name_j semantics,
measures the actual MMD source, creates an isolated source work copy when a
terminal helper is required, and delegates all target writing to
``rigging_workflow.retarget_action.convert_resolved``.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
import uuid
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import pose_switching as pose
from . import retarget_action as action_api
from . import retarget_c94 as c94
from . import retarget_calibration as cal
from . import retarget_source as source_api


SCHEMA = "c94.retarget.mmd.v1"
MAPPING_SCHEMA = "c94.retarget.mapping.v1"
ACTION_CALIBRATION_KEY = "c94_retarget_mmd_calibration_v1"
PRESET = "mmd_pmx_exact_body_four_node_fingers_v1"
OWNED_COPY_KEY = "c94_mmd_owned_source_copy_v1"
OWNED_TOKEN_KEY = "c94_mmd_owned_source_token_v1"
OWNED_SHA_KEY = "c94_mmd_owned_source_sha256_v1"
HELPER_PREFIX = "MMD_HELPER_"

MMD_BODY = {
    "hips": "下半身",
    "spine": "上半身",
    "chest": "上半身2",
    "neck": "首",
    "head": "頭",
}
MMD_SIDES = {
    "L": {
        "upper_arm": "左腕", "forearm": "左ひじ", "hand": "左手首",
        "thigh": "左足", "shin": "左ひざ", "foot": "左足首", "toes": "左つま先",
        "thumb": ("左親指０", "左親指１", "左親指２", "左親指先"),
        "index": ("左人指１", "左人指２", "左人指３", "左人指先"),
        "middle": ("左中指１", "左中指２", "左中指３", "左中指先"),
        "ring": ("左薬指１", "左薬指２", "左薬指３", "左薬指先"),
        "pinky": ("左小指１", "左小指２", "左小指３", "左小指先"),
    },
    "R": {
        "upper_arm": "右腕", "forearm": "右ひじ", "hand": "右手首",
        "thigh": "右足", "shin": "右ひざ", "foot": "右足首", "toes": "右つま先",
        "thumb": ("右親指０", "右親指１", "右親指２", "右親指先"),
        "index": ("右人指１", "右人指２", "右人指３", "右人指先"),
        "middle": ("右中指１", "右中指２", "右中指３", "右中指先"),
        "ring": ("右薬指１", "右薬指２", "右薬指３", "右薬指先"),
        "pinky": ("右小指１", "右小指２", "右小指３", "右小指先"),
    },
}
FINGER_WORDS = ("thumb", "index", "middle", "ring", "pinky")


def require(value, message):
    if not value:
        raise ValueError(message)


def nfkc(value):
    return unicodedata.normalize("NFKC", value or "").strip()


def canonical_hash(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def matrix_list(matrix):
    return [[float(matrix[r][c]) for c in range(4)] for r in range(4)]


def source_meshes(rig):
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH"
            and any(mod.type == "ARMATURE" and mod.object == rig for mod in obj.modifiers)]


def weight_sums(rig):
    result = {}
    for mesh in source_meshes(rig):
        sums = {}
        for vertex in mesh.data.vertices:
            for group in vertex.groups:
                if group.weight > 0:
                    name = mesh.vertex_groups[group.group].name
                    sums[name] = sums.get(name, 0.0) + float(group.weight)
        result[mesh.name] = sums
    return result


def _all_weights(weights):
    merged = {}
    for sums in weights.values():
        for name, value in sums.items():
            merged[name] = merged.get(name, 0.0) + value
    return merged


def bone_rest_signature(rig, exclude_prefixes=()):
    rows = []
    for bone in sorted(rig.data.bones, key=lambda item: item.name):
        if any(bone.name.startswith(prefix) for prefix in exclude_prefixes):
            continue
        rows.append({
            "name": bone.name,
            "matrix_local": matrix_list(bone.matrix_local),
            "head_local": list(bone.head_local),
            "tail_local": list(bone.tail_local),
            "parent": bone.parent.name if bone.parent else None,
            "connected": bone.use_connect,
            "deform": bone.use_deform,
            "inherit_rotation": bone.use_inherit_rotation,
            "inherit_scale": bone.inherit_scale,
        })
    return canonical_hash(rows)


def constraint_signature(rig, exclude_prefixes=()):
    rows = []
    for pbone in sorted(rig.pose.bones, key=lambda item: item.name):
        if any(pbone.name.startswith(prefix) for prefix in exclude_prefixes):
            continue
        for constraint in pbone.constraints:
            rows.append({
                "bone": pbone.name,
                "name": constraint.name,
                "type": constraint.type,
                "target": constraint.target.name if getattr(constraint, "target", None) else None,
                "subtarget": getattr(constraint, "subtarget", None),
                "owner_space": getattr(constraint, "owner_space", None),
                "target_space": getattr(constraint, "target_space", None),
                "chain_count": getattr(constraint, "chain_count", None),
                "mute": constraint.mute,
                "influence": float(constraint.influence),
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
                if bag:
                    rows.append({
                        "slot": slot.identifier,
                        "curves": [
                            [fcurve.data_path, fcurve.array_index,
                             [[float(key.co[0]), float(key.co[1]), key.interpolation]
                              for key in fcurve.keyframe_points]]
                            for fcurve in bag.fcurves
                        ],
                    })
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _name_index(rig):
    index = {}
    records = []
    for pbone in rig.pose.bones:
        require(hasattr(pbone, "mmd_bone"), "源骨架没有已注册的MMD Tools mmd_bone元数据")
        raw = pbone.mmd_bone.name_j
        normalized = nfkc(raw)
        records.append({
            "blender_name": pbone.name,
            "name_j_raw": raw,
            "name_j_nfkc": normalized,
            "name_e": pbone.mmd_bone.name_e,
            "bone_id": int(pbone.mmd_bone.bone_id),
            "is_tip": bool(pbone.mmd_bone.is_tip),
            "display_connection_bone": pbone.mmd_bone.display_connection_bone,
            "display_connection_type": pbone.mmd_bone.display_connection_type,
            "parent": pbone.parent.name if pbone.parent else None,
            "use_deform": bool(pbone.bone.use_deform),
        })
        if normalized:
            index.setdefault(normalized, []).append(pbone)
    duplicates = {name: [item.name for item in values] for name, values in index.items()
                  if len(values) > 1}
    require(not duplicates,
            "NFKC后源name_j重复，拒绝猜测：" + json.dumps(duplicates, ensure_ascii=False, sort_keys=True))
    return {name: values[0] for name, values in index.items()}, records


def _descendant(rig, ancestor_name, child_name):
    child = rig.data.bones.get(child_name)
    require(child is not None, "缺少源骨：" + child_name)
    return ancestor_name in {bone.name for bone in child.parent_recursive}


def _chain(rig, names, *, direct=True, label=""):
    for parent_name, child_name in zip(names, names[1:]):
        if direct:
            actual = rig.data.bones[child_name].parent
            require(actual is not None and actual.name == parent_name,
                    f"{label}层级不符：{parent_name} → {child_name}，实际父级 "
                    f"{actual.name if actual else None}")
        else:
            require(_descendant(rig, parent_name, child_name),
                    f"{label}层级不符：{parent_name} 不是 {child_name} 的祖先")


def _resolve(index, canonical, semantic):
    found = index.get(nfkc(canonical))
    require(found is not None, f"源缺少{canonical}（{semantic}）")
    return found


def build_mapping(rig, *, weights=None, pmx_bones=None, allow_helpers=False,
                  owned_work_copy=None):
    """Resolve exact MMD name_j semantics and positive-weight chains.

    The default path is read-only.  Missing fourth terminal nodes are rejected
    unless the caller explicitly permits helpers on a work copy created and
    marked by :func:`append_source_work_copy`.
    """
    source_api.armature(rig)
    source_bone_count = len(rig.pose.bones)
    index, records = _name_index(rig)
    weights = weights or weight_sums(rig)
    merged_weights = _all_weights(weights)
    pmx_by_name = {}
    for row in pmx_bones or []:
        pmx_by_name.setdefault(row["name_j_nfkc"], row)

    bones = {}
    semantic_records = {}
    used = {}
    helper_plan = []
    skipped_fingers = {}
    complete_keys = set()

    def record_semantic(semantic, pbone, canonical, *, owned_helper=False):
        pmx = pmx_by_name.get(nfkc(canonical), {})
        require(pbone.name not in used,
                f"源骨被多个语义复用：{pbone.name} ({used.get(pbone.name)} / {semantic})")
        used[pbone.name] = semantic
        bones[semantic] = pbone.name
        semantic_records[semantic] = {
            "canonical_name_j": canonical,
            "name_j_raw": pbone.mmd_bone.name_j if hasattr(pbone, "mmd_bone") else None,
            "name_j_nfkc": nfkc(pbone.mmd_bone.name_j) if hasattr(pbone, "mmd_bone") else None,
            "blender_bone": pbone.name,
            "name_e": pbone.mmd_bone.name_e if hasattr(pbone, "mmd_bone") else None,
            "bone_id": int(pbone.mmd_bone.bone_id) if hasattr(pbone, "mmd_bone") else None,
            "parent": pbone.parent.name if pbone.parent else None,
            "use_deform": bool(pbone.bone.use_deform),
            "is_tip": bool(pbone.mmd_bone.is_tip) if hasattr(pbone, "mmd_bone") else None,
            "display_connection_bone": pbone.mmd_bone.display_connection_bone if hasattr(pbone, "mmd_bone") else None,
            "display_connection_type": pbone.mmd_bone.display_connection_type if hasattr(pbone, "mmd_bone") else None,
            "pmx_display_connection": pmx.get("display_connection_raw"),
            "pmx_display_connection_name": pmx.get("display_connection_name"),
            "positive_weight_sum": float(merged_weights.get(pbone.name, 0.0)),
            "positive_weight": bool(merged_weights.get(pbone.name, 0.0) > 0),
            "owned_terminal_helper": bool(owned_helper),
        }
        return pbone

    def put(semantic, canonical):
        return record_semantic(semantic, _resolve(index, canonical, semantic), canonical)

    for semantic, canonical in MMD_BODY.items():
        put(semantic, canonical)
    for side, spec in MMD_SIDES.items():
        for semantic in ("upper_arm", "forearm", "hand", "thigh", "shin", "foot", "toes"):
            put(semantic + "." + side, spec[semantic])

    for side, spec in MMD_SIDES.items():
        hand = bones["hand." + side]
        for finger in FINGER_WORDS:
            names = spec[finger]
            present = {}
            missing = []
            for position, canonical in enumerate(names, 1):
                pbone = index.get(nfkc(canonical))
                if pbone is None:
                    missing.append((position, canonical))
                else:
                    present[position] = pbone
            if any(position in (1, 2, 3) for position, _ in missing):
                skipped_fingers[finger + "." + side] = [name for _, name in missing]
                continue
            chain = [hand]
            for position in (1, 2, 3):
                pbone = present[position]
                record_semantic(finger + str(position) + "." + side, pbone, names[position - 1])
                chain.append(pbone.name)
            _chain(rig, chain, direct=True, label=finger + "." + side)
            weighted = [semantic_records[finger + str(position) + "." + side]["positive_weight"]
                        for position in (1, 2, 3)]
            if not all(weighted):
                skipped_fingers[finger + "." + side] = [
                    finger + str(position) + "." + side
                    for position, value in zip((1, 2, 3), weighted) if not value]
                for position in (1, 2, 3):
                    semantic = finger + str(position) + "." + side
                    bones.pop(semantic, None)
                    semantic_records.pop(semantic, None)
                    used.pop(present[position].name, None)
                continue
            if 4 in present:
                pbone = present[4]
                record_semantic(finger + "4." + side, pbone, names[3])
                _chain(rig, [present[3].name, pbone.name], direct=True,
                       label=finger + "." + side)
                complete_keys.add(finger + "." + side)
                continue
            if not allow_helpers:
                raise ValueError(
                    "源缺少第四终端且默认mapping只读，拒绝修改传入骨架："
                    + finger + "." + side + "；显式owned work copy+allow_helpers才可补helper")
            require(owned_work_copy is not None
                    and rig.get(OWNED_COPY_KEY) is True
                    and rig.get(OWNED_TOKEN_KEY) == owned_work_copy,
                    "helper只允许本模块创建并标记的owned源工作副本：" + finger + "." + side)
            helper_name = HELPER_PREFIX + finger + "_" + side + "_END"
            require(helper_name not in rig.data.bones,
                    "terminal helper名称冲突：" + helper_name)
            require(helper_name not in {plan["helper_name"] for plan in helper_plan},
                    "terminal helper计划重复：" + helper_name)
            third = present[3]
            direction = third.bone.tail_local - third.bone.head_local
            require(direction.length > 1e-8, "末节骨方向退化：" + third.name)
            head = third.bone.tail_local.copy()
            tail = head + direction.normalized() * max(direction.length * 0.1, 0.001)
            helper_plan.append({
                "semantic": finger + "4." + side,
                "canonical_name_j": names[3],
                "finger": finger,
                "side": side,
                "third_name": third.name,
                "helper_name": helper_name,
                "head_local": list(head),
                "tail_local": list(tail),
            })

    for side in ("L", "R"):
        _chain(rig, [bones["thigh." + side], bones["shin." + side],
                     bones["foot." + side], bones["toes." + side]], direct=True,
               label="leg." + side)
        require(bones["thigh." + side] in {b.name for b in rig.data.bones[bones["hips"]].children}
                or _descendant(rig, bones["hips"], bones["thigh." + side]),
                "腿语义不属于下半身源骨层级")
        require(_descendant(rig, bones["upper_arm." + side], bones["forearm." + side]),
                "前臂不是上臂的源层级后代")
        require(_descendant(rig, bones["forearm." + side], bones["hand." + side]),
                "手不是前臂的源层级后代")
        _chain(rig, [bones["neck"], bones["head"]], direct=True, label="head")
    require(_descendant(rig, bones["spine"], bones["chest"]),
            "上胸不是上半身的源层级后代")
    require(_descendant(rig, bones["chest"], bones["neck"]),
            "颈不是上胸的源层级后代")
    for semantic in ("hips", "spine", "chest", "neck", "head"):
        require(semantic_records[semantic]["positive_weight"],
                "身体语义缺少实际正权消费：" + semantic)
    for side in ("L", "R"):
        for semantic in ("upper_arm", "forearm", "hand", "thigh", "shin", "foot"):
            require(semantic_records[semantic + "." + side]["positive_weight"],
                    "肢体语义缺少实际正权消费：" + semantic + "." + side)

    terminal_helpers = []
    if helper_plan:
        require(allow_helpers is True, "helper计划需要显式allow_helpers")
        require(owned_work_copy is not None
                and rig.get(OWNED_COPY_KEY) is True
                and rig.get(OWNED_TOKEN_KEY) == owned_work_copy,
                "helper只允许本模块创建并标记的owned源工作副本")
        action_before = action_signature(rig.animation_data.action
                                         if rig.animation_data else None)
        rest_before = bone_rest_signature(rig, exclude_prefixes=(HELPER_PREFIX,))
        constraints_before = constraint_signature(rig, exclude_prefixes=(HELPER_PREFIX,))
        terminal_helpers = _add_terminal_helpers(rig, helper_plan)
        require(action_signature(rig.animation_data.action
                                 if rig.animation_data else None) == action_before,
                "helper修改了源Action")
        require(bone_rest_signature(rig, exclude_prefixes=(HELPER_PREFIX,)) == rest_before,
                "helper修改了原有源骨rest")
        require(constraint_signature(rig, exclude_prefixes=(HELPER_PREFIX,)) == constraints_before,
                "helper修改了原有源约束")
        for plan, helper_record in zip(helper_plan, terminal_helpers):
            pbone = rig.pose.bones[plan["helper_name"]]
            record_semantic(plan["semantic"], pbone, plan["canonical_name_j"],
                            owned_helper=True)
            semantic_records[plan["semantic"]]["owned_work_copy"] = owned_work_copy
            complete_keys.add(plan["finger"] + "." + plan["side"])

    order = [(finger + "." + side) for side in ("L", "R") for finger in FINGER_WORDS]
    complete_fingers = [key for key in order if key in complete_keys]

    intermediate_weighted = []
    for parent_semantic, child_semantic in (
            ("spine", "chest"), ("upper_arm.L", "forearm.L"), ("forearm.L", "hand.L"),
            ("upper_arm.R", "forearm.R"), ("forearm.R", "hand.R")):
        child_name = bones[child_semantic]
        for ancestor in rig.data.bones[child_name].parent_recursive:
            if ancestor.name in bones.values():
                break
            if merged_weights.get(ancestor.name, 0) > 0 and ancestor.name not in intermediate_weighted:
                intermediate_weighted.append(ancestor.name)
    for side in ("L", "R"):
        for ancestor in rig.data.bones[bones["upper_arm." + side]].parent_recursive:
            if ancestor.name in bones.values():
                break
            if merged_weights.get(ancestor.name, 0) > 0 and ancestor.name not in intermediate_weighted:
                intermediate_weighted.append(ancestor.name)

    mapping = {
        "schema": MAPPING_SCHEMA,
        "preset": PRESET,
        "bones": bones,
        "fingers": {"complete": complete_fingers, "skipped": skipped_fingers},
        "missing_optional": ["shoulder", "upper_body_intermediate", "wrist_twist",
                             "ankle_D_reference", "morph", "gaze", "camera"],
        "unmapped_channels": [
            "morph", "gaze", "camera", "physics", "shoulder_independent",
            "upper_body_intermediate", "mmd_twist_bones", "accessories",
        ],
        "support": "body_waist_upper_chest_root_and_complete_four_node_fingers_development_candidate",
        "owned_terminal_helpers": len(terminal_helpers),
    }
    profile = {
        "schema": SCHEMA,
        "preset": PRESET,
        "name_j_nfkc_unique": True,
        "name_j_duplicates": {},
        "source_bones": source_bone_count,
        "mapped_bones": len(bones),
        "semantics": semantic_records,
        "weight_sums": weights,
        "terminal_helpers": terminal_helpers,
        "owned_helper_count": len(terminal_helpers),
        "real_terminal_bones": [
            semantic for semantic, row in semantic_records.items()
            if semantic.startswith(tuple(word + "4." for word in FINGER_WORDS))
            and row.get("positive_weight") is False and not row.get("owned_terminal_helper")
        ],
        "intermediate_weighted_bones": intermediate_weighted,
        "limitations": [
            "MMD中间上半身、腕捩/手捩只通过世界矩阵累计，不在C94独立重现",
            "源指尖骨为真实PMX末端骨但正权为0，仅作为四节点方向末端",
            "源足趾正权为0，脚趾语义仅为方向末端",
            "morph、视线、镜头、物理、配件未在本片转换",
        ],
    }
    return mapping, profile


def _add_terminal_helpers(rig, plans):
    """Add preflighted owned terminal helpers, restoring editor state."""
    require(plans and bpy.context.mode == "OBJECT", "helper修改要求OBJECT模式")
    active_before = bpy.context.view_layer.objects.active
    selected_before = list(bpy.context.selected_objects)
    added = []
    records = []
    try:
        bpy.context.view_layer.objects.active = rig
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        rig.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        edit = rig.data.edit_bones
        for plan in plans:
            name = plan["helper_name"]
            require(name not in edit, "terminal helper名称冲突：" + name)
            helper = edit.new(name)
            helper.head = Vector(plan["head_local"])
            helper.tail = Vector(plan["tail_local"])
            helper.parent = edit[plan["third_name"]]
            helper.use_connect = False
            helper.use_deform = False
            helper.use_inherit_rotation = True
            added.append(name)
        bpy.ops.object.mode_set(mode="OBJECT")
        for plan in plans:
            pbone = rig.pose.bones[plan["helper_name"]]
            pbone["c94_mmd_owned_terminal_helper"] = True
            pbone["c94_mmd_helper_for"] = plan["third_name"]
            pbone["c94_mmd_helper_owned"] = True
            records.append({
                "bone": plan["helper_name"],
                "head_local": list(plan["head_local"]),
                "tail_local": list(plan["tail_local"]),
                "parent": plan["third_name"],
                "use_deform": False,
                "use_inherit_rotation": True,
                "owned": True,
                "purpose": f"explicit terminal fourth node for {plan['finger']}.{plan['side']}; no weight or Action curve",
            })
        return records
    except BaseException:
        try:
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.context.view_layer.objects.active = rig
            rig.select_set(True)
            bpy.ops.object.mode_set(mode="EDIT")
            for name in reversed(added):
                if name in rig.data.edit_bones:
                    rig.data.edit_bones.remove(rig.data.edit_bones[name])
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        raise
    finally:
        try:
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in selected_before:
            if obj.name in bpy.context.view_layer.objects:
                obj.select_set(True)
        bpy.context.view_layer.objects.active = active_before


def append_source_work_copy(source_blend, *, object_names, action_name, prefix="MMD_SOURCE_"):
    """Append exactly requested source objects and Action by explicit refs."""
    scene = bpy.context.scene
    source_path = Path(source_blend).resolve(strict=True)
    require(source_path.is_file(), "源blend不存在")
    requested = list(object_names)
    require(requested and len(requested) == len(set(requested)),
            "object_names必须非空且不重复")
    partial = []
    active_before = bpy.context.view_layer.objects.active
    selected_before = list(bpy.context.selected_objects)
    try:
        with bpy.data.libraries.load(str(source_path), link=False) as (from_data, to_data):
            missing = [name for name in requested if name not in from_data.objects]
            require(not missing, "源缺少请求对象：" + json.dumps(missing, ensure_ascii=False))
            require(action_name in from_data.actions, "源缺少准确请求Action：" + action_name)
            to_data.objects = list(requested)
            to_data.actions = [action_name]
        loaded = [obj for obj in to_data.objects if obj is not None]
        loaded_actions = [action for action in to_data.actions if action is not None]
        require(len(loaded) == len(requested), "请求对象未全部取得")
        require(len(loaded_actions) == 1, "准确请求Action未取得恰好一个")
        objects = {}
        saved_world = {}
        for original, obj in zip(requested, loaded):
            require(obj is not None, "请求对象加载失败：" + original)
            if obj.name not in scene.objects:
                scene.collection.objects.link(obj)
            partial.append(obj.name)
            objects[original] = obj
            saved_world[original] = obj.matrix_world.copy()
        bpy.context.view_layer.update()
        loaded_set = set(loaded)
        rigs = [obj for obj in loaded if obj.type == "ARMATURE"]
        meshes = [obj for obj in loaded if obj.type == "MESH"]
        require(len(rigs) == 1, "源工作副本须恰好有一个Armature，实际 " + str(len(rigs)))
        rig = rigs[0]
        external = []
        for obj in loaded:
            if obj.parent is not None and obj.parent not in loaded_set:
                external.append(f"object:{obj.name}:parent:{obj.parent.name}")
        modifier_remapped = 0
        for mesh in meshes:
            for modifier in mesh.modifiers:
                if modifier.type != "ARMATURE":
                    continue
                target = modifier.object
                if target is None or target not in loaded_set:
                    external.append(
                        f"mesh:{mesh.name}:armature_modifier:{modifier.name}:target:"
                        + (target.name if target else "None"))
                elif target != rig and target.type == "ARMATURE" and target.data == rig.data:
                    modifier.object = rig
                    modifier_remapped += 1
        constraint_remapped = 0
        for pbone in rig.pose.bones:
            for constraint in pbone.constraints:
                target = getattr(constraint, "target", None)
                if target is None:
                    continue
                if target not in loaded_set:
                    external.append(
                        f"bone:{pbone.name}:constraint:{constraint.name}:target:{target.name}")
                elif target != rig and target.type == "ARMATURE" and target.data == rig.data:
                    constraint.target = rig
                    constraint_remapped += 1
        require(not external,
                "源工作副本含未知外部引用，未认领/未修改；partial="
                + json.dumps(partial, ensure_ascii=False)
                + " external=" + json.dumps(external, ensure_ascii=False))
        for original, obj in objects.items():
            if not obj.name.startswith(prefix):
                obj.name = prefix + original
            obj.hide_render = False
            obj.hide_viewport = False
        for original, obj in objects.items():
            obj.matrix_world = saved_world[original]
        action = loaded_actions[0]
        slots = [slot for slot in action.slots if slot.target_id_type == "OBJECT"]
        require(len(slots) == 1, "源Action须恰好有一个OBJECT Slot")
        source_api.armature(rig)
        if rig.animation_data is None:
            rig.animation_data_create()
        rig.animation_data.action = action
        rig.animation_data.action_slot = slots[0]
        rig.animation_data.action_blend_type = "REPLACE"
        rig.animation_data.action_influence = 1.0
        rig.animation_data.action_extrapolation = "HOLD"
        rig.animation_data.use_nla = False
        token = uuid.uuid4().hex
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        rig[OWNED_COPY_KEY] = True
        rig[OWNED_TOKEN_KEY] = token
        rig[OWNED_SHA_KEY] = source_sha
        record = {
            "source_blend": str(source_path),
            "source_sha256": source_sha,
            "requested_objects": requested,
            "source_objects_loaded": {original: obj.name for original, obj in objects.items()},
            "object_types": {original: obj.type for original, obj in objects.items()},
            "rig": rig.name,
            "meshes": [mesh.name for mesh in meshes],
            "action_source_requested": action_name,
            "action_loaded_actual": action.name,
            "action_name_changed": action.name != action_name,
            "action_slot": slots[0].identifier,
            "owned_copy": True,
            "owned_token": token,
            "constraint_targets_remapped": constraint_remapped,
            "modifier_targets_remapped": modifier_remapped,
            "external_references": [],
            "source_file_written": False,
        }
        return {"rig": rig, "meshes": meshes, "action": action, "slot": slots[0],
                "objects": objects, "record": record}
    except BaseException as exc:
        if "partial" not in str(exc):
            suffix = "；partial=" + json.dumps(partial, ensure_ascii=False)
            if isinstance(exc, ValueError):
                raise ValueError(str(exc) + suffix) from exc
        raise
    finally:
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in selected_before:
            if obj.name in bpy.context.view_layer.objects:
                obj.select_set(True)
        bpy.context.view_layer.objects.active = active_before


def capture_clip(source, mapping, *, clip=None, source_fps=30.0, start=None, end=None):
    """Capture an already-isolated source start without changing Scene.

    This function intentionally never calls ``frame_set``.  It is called only
    from the shared-core adapter factory after the core has set the actual
    requested start and isolated the source Action.
    """
    clip = clip or source_api.active_clip(source, source_fps=source_fps)
    clip_start, clip_end = float(clip["range"][0]), float(clip["range"][1])
    actual_start = clip_start if start is None else float(start)
    actual_end = clip_end if end is None else float(end)
    require(clip_start <= actual_start < actual_end <= clip_end,
            "actual start/end须在源clip内且时长为正")
    scene = bpy.context.scene
    require(abs(scene.frame_current + scene.frame_subframe - actual_start) < 1e-9,
            "capture_clip不得改变Scene；必须在共享core实际start处调用")
    evaluated = pose._update(source)
    pose_matrices = {semantic: evaluated.matrix_world @ evaluated.pose.bones[name].matrix
                     for semantic, name in mapping["bones"].items()}
    rest = {semantic: source.matrix_world @ source.data.bones[name].matrix_local
            for semantic, name in mapping["bones"].items()}
    source_legs = {}
    for side in ("L", "R"):
        points = [rest["thigh." + side].translation, rest["shin." + side].translation,
                  rest["foot." + side].translation]
        source_legs[side] = (points[1] - points[0]).length + (points[2] - points[1]).length
        require(source_legs[side] > 1e-8, "源腿长退化：" + side)
    foot_points = []
    for side in ("L", "R"):
        for semantic in ("foot." + side, "toes." + side):
            pbone = evaluated.pose.bones[mapping["bones"][semantic]]
            foot_points.extend((evaluated.matrix_world @ pbone.head,
                                evaluated.matrix_world @ pbone.tail))
    extent = min(point.z for point in foot_points)
    ad = source.animation_data
    require(ad is not None and ad.action is not None and ad.action_slot is not None,
            "共享core未建立源Action隔离")
    require(ad.action_blend_type == "REPLACE" and abs(ad.action_influence - 1.0) < 1e-9
            and not ad.use_nla, "共享core REPLACE/满影响/NLA隔离未生效")
    hip = pose_matrices["hips"].translation.copy()
    record = {
        "action": clip["action"].name,
        "slot": clip["slot"].identifier,
        "fps": float(clip["fps"]),
        "clip_range": [clip_start, clip_end],
        "source_range": [actual_start, actual_end],
        "origin_frame": actual_start,
        "origin_mode": "ACTUAL_START",
        "origin_is_clip_start": abs(actual_start - clip_start) < 1e-9,
        "action_signature": action_signature(clip["action"]),
        "source_hip_world": list(hip),
        "foot_joint_extent_z_not_floor": float(extent),
        "floor_detected": False,
        "contact_usable": False,
        "source_leg_lengths_world": {side: float(value) for side, value in source_legs.items()},
        "source_pose_signature": canonical_hash(
            {semantic: matrix_list(matrix) for semantic, matrix in sorted(pose_matrices.items())}),
        "source_ad_at_factory": {
            "action": ad.action.name,
            "slot": ad.action_slot.identifier,
            "action_blend_type": ad.action_blend_type,
            "action_influence": float(ad.action_influence),
            "action_extrapolation": ad.action_extrapolation,
            "use_nla": bool(ad.use_nla),
        },
    }
    return {"clip": clip, "record": record, "pose_matrices": pose_matrices,
            "rest": rest, "source_legs": source_legs, "source_hip": hip}


def prepare_adapter(source, target, mapping, *, clip_record, source_legs, profile,
                    motion="PRESERVE"):
    """Build the frozen C94 Adapter with explicit MMD calibration."""
    require(target.animation_data is None or target.animation_data.action is None,
            "共享转换核解绑目标Action后才能测量neutral")
    neutral = cal._target_neutral(source, target, mapping)
    scale_info = cal.motion_scale_for_legs(source_legs, neutral["target_legs"],
                                           neutral["target_object_unit"])
    adapter = c94.Adapter(
        source, target, mapping["bones"], motion=motion, scale_motion=False,
        include_fingers=True, world_motion_scale=scale_info["world_leg_ratio_average"],
        source_hip=Vector(clip_record["source_hip_world"]),
        orientation_reference="REST",
    )
    require(abs(adapter.scale - scale_info["motion_scale"]) <= 1e-9,
            "适配器比例与单次target-unit除法不一致")
    expected_rest = {semantic: source.matrix_world @ source.data.bones[name].matrix_local
                     for semantic, name in mapping["bones"].items()}
    expected_rest_hash = canonical_hash(
        {semantic: matrix_list(matrix) for semantic, matrix in sorted(expected_rest.items())})
    actual_rest_hash = canonical_hash(
        {semantic: matrix_list(matrix) for semantic, matrix in sorted(adapter.rest.items())})
    require(actual_rest_hash == expected_rest_hash, "适配器REST与源REST不一致")
    record = {
        "schema": SCHEMA,
        "preset": PRESET,
        "mapping": mapping,
        "mapping_sha256": canonical_hash(mapping),
        "profile_summary": {
            "mapped_bones": profile["mapped_bones"],
            "source_bones": profile["source_bones"],
            "terminal_helpers": profile["terminal_helpers"],
            "real_terminal_bones": profile["real_terminal_bones"],
            "intermediate_weighted_bones": profile["intermediate_weighted_bones"],
            "semantics": profile["semantics"],
            "name_j_duplicates": profile["name_j_duplicates"],
        },
        "clip": clip_record,
        "orientation_reference": "REST",
        "translation_origin": "ACTUAL_START",
        "residual_attribution": "UNEXPLAINED",
        "motion_scale_basis": "MMD_LEG_LENGTH",
        "source_leg_lengths_world": {side: float(value) for side, value in source_legs.items()},
        "target_leg_lengths_world": neutral["target_legs"],
        "target_leg_points_world": neutral["target_leg_points_world"],
        "target_object_unit": neutral["target_object_unit"],
        "leg_ratio": scale_info["ratios"],
        "world_leg_ratio_average": scale_info["world_leg_ratio_average"],
        "motion_scale": scale_info["motion_scale"],
        "adapter_armature_scale": float(adapter.scale),
        "motion_scale_semantics": scale_info["motion_scale_semantics"],
        "unit_division_count": 1,
        "target_neutral_hash": canonical_hash(
            {name: matrix_list(matrix) for name, matrix in sorted(adapter.neutral.items())}),
        "target_neutral_root_world": list(
            target.matrix_world @ adapter.neutral["根骨骼"].translation),
        "target_neutral_hip_world": list(
            target.matrix_world @ adapter.neutral["盆骨控制器"].translation),
        "calibration_hash": canonical_hash(
            {name: matrix_list(matrix) for name, matrix in sorted(adapter.calibration.items())}),
        "source_rest_world_sha256": actual_rest_hash,
        "finger_chains": list(adapter.finger_chains),
        "palm_calibration_sides": list(adapter.palm_calibration_sides),
        "controls": list(adapter.names),
        "preservation": {
            "source_action_written": False,
            "source_rest_changed": False,
            "source_constraints_changed": False,
            "target_rest_changed": False,
            "target_constraints_changed": False,
            "target_weights_changed": False,
            "automatic_contact_correction": False,
            "additional_trajectory_correction": False,
        },
        "limitations": list(profile["limitations"]) + [
            "姿态残差未逐帧归因；无requested local/actual limit因果证据时不称原生限制",
            "本片不转换morph、视线、镜头、物理或配件",
        ],
    }
    record["record_sha256"] = canonical_hash(
        {key: value for key, value in record.items() if key != "record_sha256"})
    return adapter, record


def convert_mmd(source, target, *, mapping, profile, source_fps=30.0,
                start=None, end=None, output_start=1,
                motion="PRESERVE", action_name=None, progress=None, cancelled=None,
                sample_step=1):
    """Convert the MMD clip through the single shared retarget Action core."""
    clip = source_api.active_clip(source, source_fps=source_fps)
    clip_start, clip_end = float(clip["range"][0]), float(clip["range"][1])
    effective_start = clip_start if start is None else float(start)
    effective_end = clip_end if end is None else float(end)
    require(clip_start <= effective_start < effective_end <= clip_end,
            "actual start/end须在源clip内且时长为正")
    holder = {}

    def factory(current_source, current_target, current_mapping):
        scene = bpy.context.scene
        require(abs(scene.frame_current + scene.frame_subframe - effective_start) < 1e-9,
                "adapter_factory未在共享core实际start处调用")
        captured = capture_clip(
            current_source, current_mapping, clip=clip, source_fps=source_fps,
            start=effective_start, end=effective_end)
        prepared = prepare_adapter(
            current_source, current_target, current_mapping,
            clip_record=captured["record"], source_legs=captured["source_legs"],
            profile=profile, motion=motion)
        holder["captured"] = captured
        holder["prepared"] = prepared
        return prepared

    result = action_api.convert_resolved(
        source, target, mapped=mapping, clip=clip, start=effective_start, end=effective_end,
        output_start=output_start, motion=motion, scale_motion=False,
        include_fingers=True, sample_step=sample_step, action_name=action_name,
        progress=progress, cancelled=cancelled, adapter_factory=factory)
    require("prepared" in holder, "MMD适配器工厂未被共享转换核调用")
    adapter, record = holder["prepared"]
    result["calibration_record"] = record
    result["adapter_record"] = record
    report = result["report"]
    report.update({
        "schema": SCHEMA,
        "source_profile": record["profile_summary"],
        "calibration_schema": SCHEMA,
        "calibration_record_sha256": record["record_sha256"],
        "orientation_reference": "REST",
        "translation_origin": "ACTUAL_START",
        "origin_frame": record["clip"]["origin_frame"],
        "origin_mode": "ACTUAL_START",
        "residual_attribution": "UNEXPLAINED",
        "motion_scale_basis": "MMD_LEG_LENGTH",
        "motion_scale": record["motion_scale"],
        "world_leg_ratio_average": record["world_leg_ratio_average"],
        "adapter_armature_scale": record["adapter_armature_scale"],
        "target_object_unit": record["target_object_unit"],
        "leg_ratio": record["leg_ratio"],
        "source_leg_lengths_world": record["source_leg_lengths_world"],
        "target_leg_lengths_world": record["target_leg_lengths_world"],
        "source_fps": float(clip["fps"]),
        "source_range": record["clip"]["source_range"],
        "source_clip_range": [clip_start, clip_end],
        "output_fps": float(bpy.context.scene.render.fps / bpy.context.scene.render.fps_base),
        "output_samples_expected": int(report["samples"]),
        "output_range": list(report["output_range"]),
        "source_ad_at_factory": record["clip"]["source_ad_at_factory"],
        "foot_joint_extent_z_not_floor": record["clip"]["foot_joint_extent_z_not_floor"],
        "floor_detected": False,
        "contact_usable": False,
        "terminal_helpers": profile["terminal_helpers"],
        "real_terminal_bones": profile["real_terminal_bones"],
        "intermediate_weighted_bones": profile["intermediate_weighted_bones"],
        "unmapped_channels": mapping["unmapped_channels"],
        "limitations": list(report.get("limitations", [])) + [
            "源指尖/足趾可能只有方向骨而无皮肤正权；逐项记录，不伪造皮肤消费",
            "上半身M、腕捩/手捩的真实中间层未在C94独立重现",
            "MMD表情、视线、镜头、物理、贴图/材质不在本片范围",
            "MMD源IK在求值后采样，不复制IK控制曲线",
            "未检测地面；foot_joint_extent_z_not_floor仅供关节范围诊断，不可用于接触",
        ],
    })
    action = result["action"]
    action[action_api.META] = json.dumps(report, ensure_ascii=False)
    action[ACTION_CALIBRATION_KEY] = json.dumps(record, ensure_ascii=False)
    result["report"] = report
    return result


def profile_summary(profile):
    return {
        "schema": profile["schema"],
        "preset": profile["preset"],
        "source_bones": profile["source_bones"],
        "mapped_bones": profile["mapped_bones"],
        "name_j_duplicates": profile["name_j_duplicates"],
        "terminal_helpers": profile["terminal_helpers"],
        "real_terminal_bones": profile["real_terminal_bones"],
        "intermediate_weighted_bones": profile["intermediate_weighted_bones"],
        "limitations": profile["limitations"],
    }


def build_mapping_from_profile(rig, profile):
    """Use persisted pure-JSON source metadata after actual-object checks."""
    from . import mmd_source_backend
    return mmd_source_backend.mapping_from_profile(rig, profile)
