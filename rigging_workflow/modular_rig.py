"""Modular human/quadruped reference authoring and native Rigify generation.

SPDX-License-Identifier: GPL-3.0-or-later

This module owns only the independent modular reference/generation slice:

* build a local reference metarig from the Rigify metarig modules shipped with
  the running Blender installation;
* keep the author's edited rest for bones shared by a recomposed reference;
* record an explicit rest confirmation and reject stale confirmations;
* ask the bundled Rigify generator to create a new native rig without editing
  the source reference, an older generated rig, or the Blender installation.

The bundled Rigify source is GPL-2.0-or-later.  It is invoked in-process and is
not copied into this file.  Optional corrective-bone work is delegated to the
separate ``modular_correctives.install(rig, reference, metadata)`` module owned
by the integration thread; this file never guesses an auxiliary-bone algorithm.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
import uuid
from datetime import datetime, timezone

import bpy
from mathutils import Matrix, Vector

from .modular_human_modules import MODULE_BONES as HUMAN_DETAIL_BONES


REFERENCE_KEY = "rigging_modular_reference_v1"
REFERENCE_SCHEMA = "rigging.modular.reference.v1"
GENERATED_KEY = "rigging_modular_v1"
GENERATED_SCHEMA = "rigging.modular.generated.v1"

MODULES = (
    "body",
    "head",
    "shoulder_girdle",
    "forelimb.L",
    "forelimb.R",
    "hindlimb.L",
    "hindlimb.R",
    "tail",
    "correctives",
    *HUMAN_DETAIL_BONES,
)
DEFAULT_MODULES = (
    "body",
    "head",
    "forelimb.L",
    "forelimb.R",
    "hindlimb.L",
    "hindlimb.R",
)
KINDS = ("HUMAN", "QUADRUPED")
SEGMENTS = 1
BBONES = 1

REQUIRED_RIG_TYPES = (
    "spines.basic_spine",
    "spines.super_head",
    "spines.basic_tail",
    "limbs.arm",
    "limbs.leg",
    "limbs.front_paw",
    "limbs.rear_paw",
    "basic.super_copy",
)
REQUIRED_PARAMETERS = (
    "segments",
    "bbones",
    "limb_type",
    "make_custom_pivot",
    "copy_rotation_axes",
    "connect_chain",
    "tweak_coll_refs",
    "fk_coll_refs",
    "make_control",
    "make_widget",
    "extra_ik_toe",
    "front_paw_heel_influence",
)

_TEMPLATE_MODULE = {
    "HUMAN": "rigify.metarigs.Basic.basic_human",
    "QUADRUPED": "rigify.metarigs.Animals.cat",
}
_DEFAULT_LABEL = {"HUMAN": "基础人形", "QUADRUPED": "基础四足"}
_LIMB_ROOTS = {
    "HUMAN": ("upper_arm.L", "upper_arm.R", "thigh.L", "thigh.R"),
    "QUADRUPED": ("upper_arm.L", "upper_arm.R", "thigh.L", "thigh.R"),
}
_LIMB_TYPES = {
    "limbs.arm",
    "limbs.leg",
    "limbs.front_paw",
    "limbs.rear_paw",
}
_PROTECTED_GENERATED_FIELDS = (
    "schema",
    "kind",
    "configuration",
    "reference",
    "reference_signature",
)
_REFERENCE_FIELDS = {
    "schema",
    "kind",
    "label",
    "configuration",
    "source",
    "module_bones",
    "rest_signature",
    "confirmed",
    "confirmation",
    "created_at",
    "updated_at",
}
_SOURCE_FIELDS = {
    "template",
    "blender",
    "rigify_version",
    "mode",
    "composed_from",
    "composed_from_signature",
}
_CONFIRMATION_FIELDS = {
    "mode",
    "scope",
    "auto_positioning_quality_claimed",
    "confirmed_at",
}
_HEX64_RE = None


def _sha_text(value: str) -> bool:
    import re
    global _HEX64_RE
    if _HEX64_RE is None:
        _HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
    return bool(_HEX64_RE.fullmatch(value))


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _deep(value):
    return copy.deepcopy(value)


def _float_hex(value) -> str:
    return float(value).hex()


def _vec_hex(value) -> list[str]:
    return [_float_hex(v) for v in value]


def _matrix_hex(value: Matrix) -> list[list[str]]:
    return [[_float_hex(value[i][j]) for j in range(4)] for i in range(4)]


def _normalize_kind(kind) -> str:
    if not isinstance(kind, str):
        raise ValueError("kind 必须是 HUMAN 或 QUADRUPED")
    normalized = kind.strip().upper()
    if normalized not in KINDS:
        raise ValueError("kind 必须是 HUMAN 或 QUADRUPED：" + repr(kind))
    return normalized


def _normalize_modules(value) -> list[str]:
    if isinstance(value, str):
        raise ValueError("enabled_modules 必须是明确的模块子集，不能是单个字符串")
    if isinstance(value, dict):
        unknown = set(value) - set(MODULES)
        if unknown:
            raise ValueError("enabled_modules 含未知模块：" + ", ".join(sorted(unknown)))
        for name, enabled in value.items():
            if type(enabled) is not bool:
                raise ValueError("enabled_modules 的每个值必须是 bool：" + name)
        enabled = [name for name in MODULES if value.get(name, False)]
    elif isinstance(value, (list, tuple, set, frozenset)):
        selected = []
        for name in value:
            if not isinstance(name, str):
                raise ValueError("enabled_modules 项必须是字符串")
            if name not in MODULES:
                raise ValueError("enabled_modules 含未知模块：" + repr(name))
            if name in selected:
                raise ValueError("enabled_modules 含重复模块：" + name)
            selected.append(name)
        enabled = [name for name in MODULES if name in selected]
    else:
        raise ValueError("enabled_modules 必须是明确的模块 list/tuple/set 或 bool 映射")
    if "body" not in enabled:
        raise ValueError("body 模块必需，不能关闭")
    return enabled


def _detail_template(kind, enabled):
    if any(module in HUMAN_DETAIL_BONES for module in enabled):
        return "rigify.metarigs.human"
    return _TEMPLATE_MODULE[kind]


def _detail_bones(enabled):
    return {name: row for module, bones in HUMAN_DETAIL_BONES.items()
            if module in enabled for name, row in bones.items()}


def _normalize_configuration(kind, configuration=None) -> dict:
    normalized_kind = _normalize_kind(kind)
    if configuration is None:
        enabled = list(DEFAULT_MODULES)
        segments, bbones = SEGMENTS, BBONES
    elif isinstance(configuration, dict):
        unknown = set(configuration) - {"kind", "enabled_modules", "segments", "bbones"}
        if unknown:
            raise ValueError("configuration 含未知字段：" + ", ".join(sorted(unknown)))
        if "kind" in configuration:
            configured_kind = _normalize_kind(configuration["kind"])
            if configured_kind != normalized_kind:
                raise ValueError("kind 与 configuration.kind 不一致")
        if "enabled_modules" in configuration:
            enabled = _normalize_modules(configuration["enabled_modules"])
        else:
            enabled = list(DEFAULT_MODULES)
        segments = configuration.get("segments", SEGMENTS)
        bbones = configuration.get("bbones", BBONES)
    elif isinstance(configuration, (list, tuple, set, frozenset)):
        enabled = _normalize_modules(configuration)
        segments, bbones = SEGMENTS, BBONES
    else:
        raise ValueError("configuration 必须是 dict、模块子集或 None")
    limit = 2 if normalized_kind == "HUMAN" else 1
    if type(segments) is not int or not 1 <= segments <= limit:
        raise ValueError(f"segments 必须为 1 至 {limit} 的整数")
    if type(bbones) is not int or not 1 <= bbones <= limit:
        raise ValueError(f"bbones 必须为 1 至 {limit} 的整数")
    for detail, parent in (("face", "head"), ("fingers.L", "forelimb.L"), ("fingers.R", "forelimb.R")):
        if detail in enabled and (normalized_kind != "HUMAN" or parent not in enabled):
            raise ValueError(f"{detail} 要求人形和 {parent} 模块")
    if "shoulder_girdle" in enabled and normalized_kind != "HUMAN":
        raise ValueError("shoulder_girdle 是人形肩锁/肩胛模块，不适用于四足肩带")
    return {
        "kind": normalized_kind,
        "enabled_modules": enabled,
        "segments": segments,
        "bbones": bbones,
    }


def _expected_module_bones(kind: str, enabled_modules) -> dict[str, list[str]]:
    enabled = set(enabled_modules)
    body = [
        "spine",
        "spine.001",
        "spine.002",
        "spine.003",
        "shoulder.L",
        "shoulder.R",
        "pelvis.L",
        "pelvis.R",
    ]
    head = ["spine.004", "spine.005", "spine.006"]
    forelimb = {}
    hindlimb = {}
    for side in ("L", "R"):
        chain = ["upper_arm." + side, "forearm." + side, "hand." + side]
        if kind == "QUADRUPED":
            chain.append("f_toe." + side)
        forelimb["forelimb." + side] = chain
        chain = ["thigh." + side, "shin." + side, "foot." + side]
        if kind == "QUADRUPED":
            chain.append("r_toe." + side)
        else:
            chain.extend(["toe." + side, "heel.02." + side])
        hindlimb["hindlimb." + side] = chain
    if kind == "QUADRUPED":
        body.append("pelvis.C")
    tail = ["tail.001", "tail.002", "tail.003", "tail.004"]
    result = {"body": body}
    if "shoulder_girdle" in enabled:
        result["shoulder_girdle"] = ["scapula.L", "scapula.R"]
    if "head" in enabled:
        result["head"] = head
    for key, chain in forelimb.items():
        if key in enabled:
            result[key] = chain
    for key, chain in hindlimb.items():
        if key in enabled:
            result[key] = chain
    if "tail" in enabled:
        result["tail"] = tail
    if "correctives" in enabled:
        result["correctives"] = []
    for module in enabled.intersection(HUMAN_DETAIL_BONES):
        result[module] = list(HUMAN_DETAIL_BONES[module])
    if set(result) != enabled:
        raise ValueError("内部模块表与配置不一致")
    return {name: sorted(bones) for name, bones in result.items()}


def _expected_parents(kind: str, enabled_modules) -> dict[str, str | None]:
    enabled = set(enabled_modules)
    parents = {
        "spine": None,
        "spine.001": "spine",
        "spine.002": "spine.001",
        "spine.003": "spine.002",
        "shoulder.L": "spine.003",
        "shoulder.R": "spine.003",
        "pelvis.L": "spine",
        "pelvis.R": "spine",
    }
    if "shoulder_girdle" in enabled:
        parents.update({"scapula.L": "shoulder.L", "scapula.R": "shoulder.R"})
    if kind == "QUADRUPED":
        parents["pelvis.C"] = "spine"
    if "head" in enabled:
        parents.update(
            {
                "spine.004": "spine.003",
                "spine.005": "spine.004",
                "spine.006": "spine.005",
            }
        )
    for side in ("L", "R"):
        key = "forelimb." + side
        if key in enabled:
            parents["upper_arm." + side] = ("scapula." if "shoulder_girdle" in enabled else "shoulder.") + side
            parents["forearm." + side] = "upper_arm." + side
            parents["hand." + side] = "forearm." + side
            if kind == "QUADRUPED":
                parents["f_toe." + side] = "hand." + side
        key = "hindlimb." + side
        if key in enabled:
            parents["thigh." + side] = "pelvis." + side if kind == "QUADRUPED" else "spine"
            parents["shin." + side] = "thigh." + side
            parents["foot." + side] = "shin." + side
            if kind == "QUADRUPED":
                parents["r_toe." + side] = "foot." + side
            else:
                parents["toe." + side] = "foot." + side
                parents["heel.02." + side] = "foot." + side
    if "tail" in enabled:
        parents.update(
            {
                "tail.001": "spine",
                "tail.002": "tail.001",
                "tail.003": "tail.002",
                "tail.004": "tail.003",
            }
        )
    parents.update({name: row[0] for name, row in _detail_bones(enabled).items()})
    return parents


def _expected_connections(kind: str, enabled_modules) -> dict[str, bool]:
    enabled = set(enabled_modules)
    result = {
        "spine": False,
        "spine.001": True,
        "spine.002": True,
        "spine.003": True,
        "shoulder.L": False,
        "shoulder.R": False,
        "pelvis.L": False,
        "pelvis.R": False,
    }
    if "shoulder_girdle" in enabled:
        result.update({"scapula.L": False, "scapula.R": False})
    if kind == "QUADRUPED":
        result["pelvis.C"] = False
    if "head" in enabled:
        result.update(
            {
                "spine.004": False,
                "spine.005": True,
                "spine.006": True,
            }
        )
    for side in ("L", "R"):
        key = "forelimb." + side
        if key in enabled:
            result["upper_arm." + side] = False
            result["forearm." + side] = True
            result["hand." + side] = True
            if kind == "QUADRUPED":
                result["f_toe." + side] = True
        key = "hindlimb." + side
        if key in enabled:
            result["thigh." + side] = False
            result["shin." + side] = True
            result["foot." + side] = True
            if kind == "QUADRUPED":
                result["r_toe." + side] = True
            else:
                result["toe." + side] = True
                result["heel.02." + side] = False
    if "tail" in enabled:
        result.update(
            {
                "tail.001": False,
                "tail.002": True,
                "tail.003": True,
                "tail.004": True,
            }
        )
    result.update({name: row[1] for name, row in _detail_bones(enabled).items()})
    return result


def _expected_types(kind: str, enabled_modules) -> dict[str, str]:
    enabled = set(enabled_modules)
    result = {name: "" for name in _expected_parents(kind, enabled)}
    result["spine"] = "spines.basic_spine"
    for name in ("shoulder.L", "shoulder.R", "pelvis.L", "pelvis.R"):
        result[name] = "basic.super_copy"
    if "shoulder_girdle" in enabled:
        for side in ("L", "R"):
            result["scapula." + side] = "basic.super_copy"
    if kind == "QUADRUPED":
        result["pelvis.C"] = "basic.super_copy"
    if "head" in enabled:
        result["spine.004"] = "spines.super_head"
    for side in ("L", "R"):
        if "forelimb." + side in enabled:
            result["upper_arm." + side] = (
                "limbs.front_paw" if kind == "QUADRUPED" else "limbs.arm"
            )
        if "hindlimb." + side in enabled:
            result["thigh." + side] = (
                "limbs.rear_paw" if kind == "QUADRUPED" else "limbs.leg"
            )
    if "tail" in enabled:
        result["tail.001"] = "spines.basic_tail"
    result.update({name: row[2] for name, row in _detail_bones(enabled).items()})
    return result


def _rigify_type(pose_bone) -> str:
    value = getattr(pose_bone, "rigify_type", None)
    if value is None:
        value = pose_bone.get("rigify_type", "")
    return str(value or "").replace(" ", "")


def _rigify_params(pose_bone):
    try:
        return pose_bone.rigify_parameters
    except AttributeError:
        return None


def _rigify_types_available() -> bool:
    module = sys.modules.get("rigify")
    try:
        return bool(module is not None and module.RigifyParameters.is_registered)
    except (AttributeError, RuntimeError):
        return False


def _snapshot_value(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        return _float_hex(value)
    if isinstance(value, bpy.types.ID):
        return {"id_type": value.bl_rna.identifier, "name": value.name}
    if hasattr(value, "to_list"):
        value = value.to_list()
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    try:
        items = list(value)
    except TypeError:
        return str(value)
    if all(hasattr(item, "name") for item in items):
        return [item.name for item in items]
    return [_snapshot_value(item) for item in items]


def _params_snapshot(pose_bone) -> dict:
    params = _rigify_params(pose_bone)
    if params is None:
        return {}
    result = {}
    for key in sorted(params.keys()):
        try:
            value = getattr(params, key)
        except (AttributeError, RuntimeError):
            continue
        result[key] = _snapshot_value(value)
    return result


def _rest_signature(reference, record) -> str:
    rows = []
    for bone in sorted(reference.data.bones, key=lambda item: item.name):
        pose_bone = reference.pose.bones[bone.name]
        rows.append(
            {
                "name": bone.name,
                "parent": bone.parent.name if bone.parent else None,
                "use_connect": bool(bone.use_connect),
                "use_deform": bool(bone.use_deform),
                "head": _vec_hex(bone.head_local),
                "tail": _vec_hex(bone.tail_local),
                "length": _float_hex(bone.length),
                "matrix_local": _matrix_hex(bone.matrix_local),
                "rigify_type": _rigify_type(pose_bone),
                "rigify_parameters": _params_snapshot(pose_bone),
            }
        )
    snapshot = {
        "kind": record["kind"],
        "configuration": record["configuration"],
        "module_bones": record["module_bones"],
        "object_matrix_world": _matrix_hex(reference.matrix_world),
        "bones": rows,
    }
    return hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()


def _rest_geometry(reference) -> dict:
    # Compare and copy the same DataBone representation. EditBone's rebuilt
    # matrix differs slightly even for an unchanged source on short paw bones.
    return {
        bone.name: {
            "head": tuple(float(value) for value in bone.head_local),
            "tail": tuple(float(value) for value in bone.tail_local),
            "roll": float(bpy.types.Bone.AxisRollFromMatrix(bone.matrix_local.to_3x3())[1]),
            "length": float(bone.length),
            "matrix": bone.matrix_local.copy(),
        }
        for bone in reference.data.bones
    }


def _default_label(kind: str) -> str:
    return _DEFAULT_LABEL[kind]


def _resolve_label(label, kind: str, *, fallback: str | None = None) -> str:
    if label is None:
        value = fallback if fallback is not None else _default_label(kind)
    else:
        value = str(label).strip()
        if not value:
            value = fallback if fallback is not None else _default_label(kind)
        elif kind == "QUADRUPED" and value == "基础人形":
            value = "基础四足"
    if not value:
        raise ValueError("label 不能为空")
    if len(value.encode("utf-8")) > 63:
        raise ValueError("label 超过 Blender 63 字节对象名上限")
    return value


def _unique_name(base: str, collection) -> str:
    if base not in collection:
        return base
    index = 1
    while True:
        suffix = "." + str(index).zfill(3)
        candidate = base
        while len((candidate + suffix).encode("utf-8")) > 63:
            candidate = candidate[:-1]
        candidate += suffix
        if candidate not in collection:
            return candidate
        index += 1


def _select_only(obj) -> None:
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _selection():
    return bpy.context.view_layer.objects.active, list(bpy.context.selected_objects)


def _restore_selection(active, selected) -> None:
    view = bpy.context.view_layer
    for obj in list(bpy.context.selected_objects):
        obj.select_set(False)
    for obj in selected:
        try:
            if view.objects.get(obj.name) == obj:
                obj.select_set(True)
        except ReferenceError:
            pass
    try:
        view.objects.active = (
            active
            if active is not None and view.objects.get(active.name) == active
            else None
        )
    except ReferenceError:
        view.objects.active = None


def _all_ids() -> set:
    result = set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type != "COLLECTION":
            continue
        try:
            items = getattr(bpy.data, prop.identifier)
        except (AttributeError, RuntimeError):
            continue
        for item in items:
            if isinstance(item, bpy.types.ID):
                result.add(item)
    return result


LAST_CLEANUP_REPORT = None


def _id_label(item) -> dict:
    try:
        return {"type": item.bl_rna.identifier, "name": item.name}
    except (AttributeError, ReferenceError):
        return {"type": "ReferenceError", "name": None}


def _own(owned_ids, *items) -> None:
    if owned_ids is None:
        return
    for item in items:
        try:
            if isinstance(item, bpy.types.ID) and item not in owned_ids:
                owned_ids.append(item)
        except ReferenceError:
            pass


def _publish_cleanup(report: dict) -> None:
    global LAST_CLEANUP_REPORT
    LAST_CLEANUP_REPORT = _deep(report)
    try:
        print("MODULAR_RIG cleanup: " + _canonical(report))
    except Exception:
        pass


def last_cleanup_report():
    return _deep(LAST_CLEANUP_REPORT) if LAST_CLEANUP_REPORT is not None else None


def _external_users(item, owned, user_map, scene, scene_root):
    """Live users outside this operation; used only to protect owned IDs."""
    users = set(user_map.get(item, ()))
    users -= owned
    if scene is not None:
        users.discard(scene)
    if scene_root is not None:
        users.discard(scene_root)
    if isinstance(item, bpy.types.Collection):
        # The current scene link was made by this operation. Other scenes and
        # unowned children/objects are external consumers.
        users = {user for user in users if not isinstance(user, bpy.types.Scene) or user != scene}
        users.update(obj for obj in item.objects if obj not in owned)
        users.update(child for child in item.children if child not in owned)
    users = {user for user in users if _is_live_user(user)}
    return users


def _is_live_user(user) -> bool:
    """Zero-user orphan data is not a live external consumer."""
    try:
        if getattr(user, "users", 0) != 0:
            return True
        if isinstance(user, bpy.types.Collection) and len(user.users_scene) > 0:
            return True
        if isinstance(user, bpy.types.Object) and len(user.users_collection) > 0:
            return True
    except (AttributeError, ReferenceError, RuntimeError):
        return True
    return False


def _cleanup_owned_ids(
    owned_ids,
    *,
    before,
    active,
    selected,
    groups=None,
    protected_ids=None,
    restore_selection=True,
    report_unowned=True,
) -> dict:
    """Remove only explicitly registered operation-owned IDs.

    ``before`` is diagnostics only: IDs created by callbacks are never adopted
    merely because they appeared after this operation started.  Unknown new IDs
    are retained and reported.  Externally referenced owned IDs, their product
    groups and necessary owned dependencies are retained.
    """
    live = _all_ids()
    owned = set()
    for item in owned_ids or ():
        try:
            if item in live:
                owned.add(item)
        except ReferenceError:
            pass
    protected = {item for item in (protected_ids or ()) if item in owned}
    groups = [set(group) & owned for group in (groups or ()) if set(group) & owned]
    user_map = bpy.data.user_map()
    scene = bpy.context.scene
    scene_root = scene.collection if scene is not None else None
    external = {}
    for item in owned:
        users = _external_users(item, owned, user_map, scene, scene_root)
        if users:
            external[item] = users
    retained = set(external)
    changed = True
    while changed:
        changed = False
        for group in groups:
            if group & retained and not group <= retained:
                retained |= group
                changed = True
    dependencies = {}
    for item in owned:
        deps = set()
        if isinstance(item, bpy.types.Object):
            if item.data in owned:
                deps.add(item.data)
            try:
                for pose_bone in item.pose.bones:
                    shape = pose_bone.custom_shape
                    if shape in owned:
                        deps.add(shape)
                        if shape.data in owned:
                            deps.add(shape.data)
            except (AttributeError, RuntimeError, ReferenceError):
                pass
        dependencies[item] = deps
    changed = True
    while changed:
        changed = False
        for item, deps in dependencies.items():
            if item in retained:
                new = deps - retained
                if new:
                    retained |= new
                    changed = True
    changed = True
    while changed:
        changed = False
        for item in owned:
            if isinstance(item, bpy.types.Collection):
                members = {obj for obj in item.objects if obj in owned}
                if members & retained and item not in retained:
                    retained.add(item)
                    changed = True
    retained |= protected

    def order(item):
        if isinstance(item, bpy.types.Object):
            return 0
        if isinstance(item, bpy.types.Collection):
            return 1
        if isinstance(item, bpy.types.Text):
            return 1
        if isinstance(item, bpy.types.Armature):
            return 2
        if isinstance(item, bpy.types.Mesh):
            return 2
        return 3

    try:
        current = bpy.context.object
        if current is not None and current in owned - retained and current.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except (RuntimeError, ReferenceError):
        pass

    removed = []
    failed = []
    for item in sorted(owned - retained, key=order):
        label = _id_label(item)
        try:
            if isinstance(item, bpy.types.Object):
                bpy.data.objects.remove(item, do_unlink=True)
            elif isinstance(item, bpy.types.Collection):
                bpy.data.collections.remove(item)
            elif isinstance(item, bpy.types.Text):
                bpy.data.texts.remove(item)
            elif isinstance(item, bpy.types.Armature):
                if item.users != 0:
                    retained.add(item)
                    continue
                bpy.data.armatures.remove(item)
            elif isinstance(item, bpy.types.Mesh):
                if item.users != 0:
                    retained.add(item)
                    continue
                bpy.data.meshes.remove(item)
            else:
                # Do not guess how to remove an unregistered/unknown ID type.
                retained.add(item)
                continue
            removed.append(label)
        except Exception as error:
            failed.append({**label, "error": str(error)})
    if restore_selection:
        _restore_selection(active, selected)
    try:
        bpy.context.view_layer.update()
    except RuntimeError:
        pass
    new_ids = _all_ids() - before if before is not None else set()
    unowned = new_ids - owned
    report = {
        "removed": removed,
        "retained_external": [
            {
                "id": _id_label(item),
                "users": [_id_label(user) for user in sorted(users, key=lambda value: (value.bl_rna.identifier, value.name))],
            }
            for item, users in sorted(external.items(), key=lambda pair: (pair[0].bl_rna.identifier, pair[0].name))
        ],
        "retained_dependencies_or_protected": [
            _id_label(item)
            for item in sorted(retained - set(external), key=lambda value: (value.bl_rna.identifier, value.name))
        ],
        "retained_unowned_new": [
            _id_label(item)
            for item in sorted(unowned, key=lambda value: (value.bl_rna.identifier, value.name))
        ]
        if report_unowned
        else [],
        "failed": failed,
    }
    return report


def _require_object_mode() -> None:
    if bpy.context.mode != "OBJECT":
        raise ValueError("模块化参考与生成需要当前为 Object Mode")


def _require_scene_object(scene, obj) -> None:
    if (
        scene != bpy.context.scene
        or not isinstance(obj, bpy.types.Object)
        or obj.type != "ARMATURE"
        or scene.objects.get(obj.name) != obj
    ):
        raise ValueError("需要当前 Scene 中的明确完整参考骨架")


def _rigify_module():
    return sys.modules.get("rigify")


def backend_status() -> dict:
    """Read-only report of bundled Rigify presence and registration state."""
    import addon_utils

    status = {
        "blender": bpy.app.version_string,
        "installed": False,
        "enabled": False,
        "loaded": False,
        "registered": False,
        "preferences_entry": False,
        "parameters_ready": False,
        "rig_types_ready": False,
        "ready": False,
        "rigify_version": None,
        "missing_rig_types": list(REQUIRED_RIG_TYPES),
        "missing_parameters": list(REQUIRED_PARAMETERS),
        "error": None,
    }
    try:
        enabled, loaded = addon_utils.check("rigify")
        status["enabled"] = bool(enabled)
        status["loaded"] = bool(loaded)
    except Exception as error:  # noqa: BLE001 - status must never break callers
        status["error"] = str(error)
    module = _rigify_module()
    if module is None:
        try:
            import importlib.util

            status["installed"] = importlib.util.find_spec("rigify") is not None
        except (ImportError, AttributeError, ValueError):
            status["installed"] = status["enabled"]
    else:
        status["installed"] = True
        try:
            status["rigify_version"] = list(module.bl_info.get("version", ()))
        except (AttributeError, TypeError):
            status["rigify_version"] = None
        try:
            status["registered"] = bool(module.RigifyParameters.is_registered)
        except (AttributeError, RuntimeError):
            status["registered"] = False
        try:
            parameter_names = set(module.RigifyParameters.bl_rna.properties.keys())
        except (AttributeError, RuntimeError):
            parameter_names = set()
        missing_parameters = [name for name in REQUIRED_PARAMETERS if name not in parameter_names]
        status["missing_parameters"] = missing_parameters
        status["parameters_ready"] = not missing_parameters
        try:
            rig_names = set(module.rig_lists.rigs.keys())
        except (AttributeError, RuntimeError):
            rig_names = set()
        missing_types = [name for name in REQUIRED_RIG_TYPES if name not in rig_names]
        status["missing_rig_types"] = missing_types
        status["rig_types_ready"] = not missing_types
    try:
        status["preferences_entry"] = "rigify" in bpy.context.preferences.addons
    except (AttributeError, RuntimeError):
        status["preferences_entry"] = False
    status["ready"] = bool(
        status["enabled"]
        and status["loaded"]
        and status["registered"]
        and status["preferences_entry"]
        and status["parameters_ready"]
        and status["rig_types_ready"]
    )
    return status


def _ensure_rigify(allow_runtime_enable: bool) -> dict:
    status = backend_status()
    if status["ready"]:
        return status
    half_registered = bool(
        status["loaded"]
        and status["registered"]
        and (not status["parameters_ready"] or not status["rig_types_ready"])
    )
    if half_registered:
        raise RuntimeError(
            "已加载的 Rigify 注册不完整（动态参数或 rig 类型缺失）；"
            "拒绝在半个注册状态上生成。请用全新 Blender 进程并显式启用内置生成器。"
            + _canonical(status)
        )
    if not allow_runtime_enable:
        raise RuntimeError(
            "Rigify 未就绪；需要已有本次会话的启用状态，或显式传入 "
            "allow_runtime_enable=True 临时内存启用（不保存用户偏好）。"
            + _canonical(status)
        )
    import addon_utils

    try:
        enabled_module = addon_utils.enable("rigify", default_set=True, persistent=False)
    except Exception as error:
        raise RuntimeError("Rigify 临时启用失败：" + str(error)) from error
    if enabled_module is None:
        raise RuntimeError(
            "Rigify 临时启用返回 None，注册中途失败；拒绝使用半注册状态。"
        )
    status = backend_status()
    if not status["preferences_entry"]:
        raise RuntimeError(
            "Rigify 已返回模块但当前内存缺少 addon preferences 条目，注册不完整。"
            + _canonical(status)
        )
    if not status["ready"]:
        raise RuntimeError("Rigify 临时启用后仍未就绪。" + _canonical(status))
    return status


def _load_template(kind: str, enabled=()):
    module_name = _detail_template(kind, enabled)
    return importlib.import_module(module_name)


def _new_armature_object(scene, label: str, owned_ids=None):
    object_name = _unique_name(label, bpy.data.objects)
    data_name = _unique_name(object_name + "_骨架", bpy.data.armatures)
    data = bpy.data.armatures.new(data_name)
    _own(owned_ids, data)
    obj = bpy.data.objects.new(object_name, data)
    _own(owned_ids, obj)
    scene.collection.objects.link(obj)
    _select_only(obj)
    if not bpy.ops.object.mode_set.poll():
        raise RuntimeError("无法进入新参考骨架的编辑模式")
    bpy.ops.object.mode_set(mode="EDIT")
    return obj


def _create_template_reference(scene, kind: str, label: str, owned_ids=None, *, enabled=()):
    template = _load_template(kind, enabled)
    obj = _new_armature_object(scene, label, owned_ids)
    try:
        template.create(obj)
        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if len(obj.data.rigify_colors) == 0:
            raise RuntimeError("Rigify 颜色集未注册，拒绝在半注册状态创建参考")
        bpy.context.view_layer.update()
        return obj
    except Exception:
        if obj.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                pass
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data.users == 0:
            bpy.data.armatures.remove(data)
        raise


def _depth_in_edit(edit_bones, name: str) -> int:
    depth = 0
    bone = edit_bones[name]
    while bone.parent:
        depth += 1
        bone = bone.parent
    return depth


def _delete_bones(obj, names) -> None:
    names = set(names)
    if not names:
        return
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for name in sorted(names, key=lambda item: _depth_in_edit(obj.data.edit_bones, item), reverse=True):
            edit_bone = obj.data.edit_bones.get(name)
            if edit_bone is not None:
                obj.data.edit_bones.remove(edit_bone)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def _apply_rest(obj, source_rest: dict, names) -> None:
    names = [name for name in names if name in source_rest and name in obj.data.bones]
    if not names:
        return
    depth = {}
    for name in names:
        level = 0
        bone = obj.data.bones[name]
        while bone.parent:
            level += 1
            bone = bone.parent
        depth[name] = level
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for name in sorted(names, key=lambda item: depth[item]):
            record = source_rest[name]
            edit_bone = obj.data.edit_bones[name]
            edit_bone.head = Vector(record["head"])
            edit_bone.tail = Vector(record["tail"])
            edit_bone.roll = record["roll"]
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def _collection(obj, name: str, *, ui_row: int = 0, title: str = "", color: int = 0):
    collection = obj.data.collections_all.get(name)
    if collection is None:
        collection = obj.data.collections.new(name)
    collection.rigify_ui_row = ui_row
    collection.rigify_ui_title = title
    collection.rigify_color_set_id = color
    return collection


def _set_coll_refs(params, attr: str, collection) -> None:
    refs = getattr(params, attr + "_coll_refs")
    refs.clear()
    ref = refs.add()
    ref.set_collection(collection)
    toggle = attr + "_layers_extra"
    if hasattr(params, toggle):
        setattr(params, toggle, True)


def _add_human_tail(obj) -> list[str]:
    spine = obj.data.bones["spine"]
    length = max(float(spine.length), 0.01)
    base = spine.head_local.copy()
    offsets = (
        Vector((0.0, length * 0.95, -length * 0.18)),
        Vector((0.0, length * 1.05, length * 0.08)),
        Vector((0.0, length * 1.05, length * 0.22)),
        Vector((0.0, length * 1.05, length * 0.30)),
    )
    points = [base]
    for offset in offsets:
        points.append(points[-1] + offset)
    bpy.ops.object.mode_set(mode="EDIT")
    names = []
    try:
        previous = None
        for index in range(4):
            name = "tail." + str(index + 1).zfill(3)
            edit_bone = obj.data.edit_bones.new(name)
            edit_bone.head = points[index]
            edit_bone.tail = points[index + 1]
            edit_bone.use_connect = index > 0
            edit_bone.use_deform = False
            if index == 0:
                edit_bone.parent = obj.data.edit_bones["spine"]
            else:
                edit_bone.parent = previous
            previous = edit_bone
            names.append(name)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    tail_collection = _collection(obj, "Tail", ui_row=12, color=3)
    tweak_collection = _collection(
        obj, "Tail (Tweaks)", ui_row=13, title="(Tweaks)", color=4
    )
    for name in names:
        tail_collection.assign(obj.data.bones[name])
    pose_bone = obj.pose.bones["tail.001"]
    pose_bone.rigify_type = "spines.basic_tail"
    pose_bone.rotation_mode = "QUATERNION"
    params = pose_bone.rigify_parameters
    params.copy_rotation_axes = (True, True, True)
    params.connect_chain = True
    _set_coll_refs(params, "tweak", tweak_collection)
    return names


def _add_human_shoulder_girdle(obj) -> None:
    """Build native manual carriers, not auto-fitted anatomical landmarks.

    Authors must locate AC, GH, inferior angle and scapular plane before
    confirmation. Defaults are visible provisional handles, never a QC PASS.
    """
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bones = obj.data.edit_bones
        for side in ("L", "R"):
            shoulder = bones["shoulder." + side]
            scapula = bones.get("scapula." + side)
            if scapula is None:
                scapula = bones.new("scapula." + side)
                scapula.head = shoulder.tail
                offset = (bones["spine.003"].head - scapula.head) * 0.45
                offset.z -= max(shoulder.length * 0.25, 0.005)
                scapula.tail = scapula.head + offset
                scapula.roll = shoulder.roll
            scapula.parent = shoulder
            scapula.use_connect = False
            scapula.use_deform = False
            arm = bones.get("upper_arm." + side)
            if arm is not None:
                arm.use_connect = False
                arm.parent = scapula
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    collection = _collection(obj, "Shoulder Girdle", ui_row=4, title="Scapula", color=3)
    for side in ("L", "R"):
        name = "scapula." + side
        collection.assign(obj.data.bones[name])
        bone = obj.pose.bones[name]
        bone.rigify_type = "basic.super_copy"
        bone.rotation_mode = "QUATERNION"
        bone.lock_location = (True, True, True)
        bone.lock_scale = (True, True, True)
        params = bone.rigify_parameters
        params.make_control = True
        params.make_deform = False
        params.make_widget = True
        params.super_copy_widget_type = "shoulder"


def _sanitize_coll_refs(obj) -> None:
    collection_names = set(obj.data.collections_all.keys())
    for pose_bone in obj.pose.bones:
        params = _rigify_params(pose_bone)
        if params is None:
            continue
        keys = {key for key in params.keys() if key.endswith("_coll_refs")}
        keys.update(
            prop.identifier
            for prop in params.bl_rna.properties
            if prop.identifier.endswith("_coll_refs")
        )
        for key in sorted(keys):
            refs = getattr(params, key)
            for item in list(refs):
                if item.name not in collection_names:
                    refs.remove(item)
            toggle = key[: -len("_coll_refs")] + "_layers_extra"
            if len(refs) == 0 and hasattr(params, toggle):
                setattr(params, toggle, False)


def _prune_empty_collections(obj) -> list[str]:
    referenced = set()
    for pose_bone in obj.pose.bones:
        params = _rigify_params(pose_bone)
        if params is None:
            continue
        keys = {key for key in params.keys() if key.endswith("_coll_refs")}
        keys.update(
            prop.identifier
            for prop in params.bl_rna.properties
            if prop.identifier.endswith("_coll_refs")
        )
        for key in sorted(keys):
            for item in getattr(params, key):
                referenced.add(item.name)
    removed = []
    changed = True
    while changed:
        changed = False
        for collection in list(obj.data.collections_all):
            if collection.name == "Root":
                continue
            if (
                len(collection.bones) == 0
                and collection.name not in referenced
                and not any(child.parent == collection for child in obj.data.collections_all)
            ):
                removed.append(collection.name)
                obj.data.collections.remove(collection)
                changed = True
    return removed


def _set_limb_parameters(obj, configuration) -> None:
    segments, bbones = configuration["segments"], configuration["bbones"]
    for name in _LIMB_ROOTS[configuration["kind"]]:
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is None:
            continue
        if _rigify_type(pose_bone) not in _LIMB_TYPES:
            raise ValueError("肢体根骨未绑定到明确的 Rigify 肢体类型：" + name)
        params = _rigify_params(pose_bone)
        if params is None or not hasattr(params, "segments") or not hasattr(params, "bbones"):
            raise RuntimeError("Rigify 肢体参数未注册：" + name)
        params.segments = segments
        params.bbones = bbones
        if int(params.segments) != segments or int(params.bbones) != bbones:
            raise RuntimeError(f"无法设置肢体分段 ({segments}, {bbones})：" + name)


def _fix_known_collection_refs(obj, kind: str) -> None:
    if kind == "QUADRUPED":
        spine = obj.pose.bones.get("spine")
        collection = obj.data.collections_all.get("Spine (Tweak)")
        if spine is not None and collection is not None:
            params = _rigify_params(spine)
            if params is not None:
                _set_coll_refs(params, "fk", collection)
                _set_coll_refs(params, "tweak", collection)


def _apply_modules(obj, kind: str, configuration: dict, *, source_rest=None, source_matrix=None):
    expected = _expected_module_bones(kind, configuration["enabled_modules"])
    keep = set()
    for bones in expected.values():
        keep.update(bones)
    existing = set(obj.data.bones.keys())
    _delete_bones(obj, existing - keep)
    if source_rest is not None:
        _apply_rest(obj, source_rest, [name for name in keep if name in source_rest])
    if kind == "HUMAN" and "shoulder_girdle" in configuration["enabled_modules"]:
        _add_human_shoulder_girdle(obj)
        if source_rest is not None:
            _apply_rest(obj, source_rest, ["scapula.L", "scapula.R"])
    if kind == "HUMAN" and "tail" in configuration["enabled_modules"]:
        if "tail.001" not in obj.data.bones:
            _add_human_tail(obj)
        if source_rest is not None:
            _apply_rest(
                obj,
                source_rest,
                ["tail." + str(index).zfill(3) for index in range(1, 5)],
            )
    actual = set(obj.data.bones.keys())
    if actual != keep:
        missing = sorted(keep - actual)
        extra = sorted(actual - keep)
        raise RuntimeError("模块骨架集合与配置不一致：缺少=" + str(missing) + " 多出=" + str(extra))
    _set_limb_parameters(obj, configuration)
    _fix_known_collection_refs(obj, kind)
    _sanitize_coll_refs(obj)
    _prune_empty_collections(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for edit_bone in obj.data.edit_bones:
            edit_bone.use_deform = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    if source_matrix is not None:
        obj.matrix_world = source_matrix.copy()
    obj.show_in_front = True
    obj.display_type = "WIRE"
    bpy.context.view_layer.update()
    return {name: sorted(bones) for name, bones in expected.items()}


def _verify_common_rest(obj, source_rest: dict) -> dict:
    errors = []
    maximum = 0.0
    for name, record in source_rest.items():
        if name not in obj.data.bones:
            continue
        bone = obj.data.bones[name]
        error = max(
            (Vector(bone.head_local) - Vector(record["head"])).length,
            (Vector(bone.tail_local) - Vector(record["tail"])).length,
            max(
                abs(float(bone.matrix_local[i][j]) - float(record["matrix"][i][j]))
                for i in range(4)
                for j in range(4)
            ),
        )
        maximum = max(maximum, error)
        if error > 1e-5:
            errors.append({"bone": name, "error": error})
    return {"count": len(source_rest), "max_error": maximum, "errors": errors}


def _make_reference_record(
    kind: str,
    label: str,
    configuration: dict,
    module_bones: dict,
    status: dict,
    *,
    composed_from: str | None = None,
    composed_from_signature: str | None = None,
) -> dict:
    timestamp = _now()
    mode = (
        "composed_rigify_metarig_modules"
        if composed_from is not None
        else "rigify_metarig_modules"
    )
    return {
        "schema": REFERENCE_SCHEMA,
        "kind": kind,
        "label": label,
        "configuration": _deep(configuration),
        "source": {
            "template": _detail_template(kind, configuration["enabled_modules"]),
            "blender": bpy.app.version_string,
            "rigify_version": list(status.get("rigify_version") or ()),
            "mode": mode,
            "composed_from": composed_from,
            "composed_from_signature": composed_from_signature,
        },
        "module_bones": {name: sorted(bones) for name, bones in module_bones.items()},
        "rest_signature": None,
        "confirmed": False,
        "confirmation": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _validate_configuration_record(record) -> dict:
    if not isinstance(record, dict):
        raise ValueError("参考记录损坏：configuration 不是对象")
    if set(record) != {"kind", "enabled_modules", "segments", "bbones"}:
        raise ValueError("参考记录 configuration 字段无效")
    normalized = _normalize_configuration(record["kind"], record)
    if _canonical(normalized) != _canonical(record):
        raise ValueError("参考记录 configuration 未规范化")
    return normalized


def _validate_label(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("参考 label 无效")
    if value != value.strip() or len(value.encode("utf-8")) > 63:
        raise ValueError("参考 label 未规范化或过长")
    return value


def _validate_reference_structure(reference, record: dict) -> None:
    if (
        not isinstance(reference, bpy.types.Object)
        or reference.type != "ARMATURE"
        or reference.library is not None
        or reference.data.library is not None
    ):
        raise ValueError("参考必须是本地的 ARMATURE Object")
    configuration = record["configuration"]
    kind = record["kind"]
    enabled = configuration["enabled_modules"]
    expected_bones = _expected_module_bones(kind, enabled)
    expected_union = set()
    for bones in expected_bones.values():
        expected_union.update(bones)
    actual = set(reference.data.bones.keys())
    if actual != expected_union:
        raise ValueError(
            "参考骨集合与配置不一致：缺少="
            + str(sorted(expected_union - actual))
            + " 多出="
            + str(sorted(actual - expected_union))
        )
    stored_modules = record["module_bones"]
    if not isinstance(stored_modules, dict) or set(stored_modules) != set(enabled):
        raise ValueError("参考记录 module_bones 的模块键与配置不一致")
    for name, bones in expected_bones.items():
        stored = stored_modules.get(name)
        if (
            not isinstance(stored, list)
            or stored != sorted(bones)
            or len(stored) != len(set(stored))
        ):
            raise ValueError("参考记录 module_bones 内容无效：" + name)
    parents = _expected_parents(kind, enabled)
    connections = _expected_connections(kind, enabled)
    types = _expected_types(kind, enabled)
    check_types = _rigify_types_available()
    for bone in reference.data.bones:
        if bone.name not in parents:
            raise ValueError("参考骨缺少结构定义：" + bone.name)
        actual_parent = bone.parent.name if bone.parent else None
        if actual_parent != parents[bone.name]:
            raise ValueError(
                "参考骨父级不符："
                + bone.name
                + " 期望="
                + str(parents[bone.name])
                + " 实际="
                + str(actual_parent)
            )
        if bool(bone.use_connect) != bool(connections[bone.name]):
            raise ValueError("参考骨连接状态不符：" + bone.name)
        if bool(bone.use_deform):
            raise ValueError("参考骨不允许 use_deform=True：" + bone.name)
        if bone.length <= 1e-7:
            raise ValueError("参考骨长度为零：" + bone.name)
        if check_types:
            actual_type = _rigify_type(reference.pose.bones[bone.name])
            if actual_type != types[bone.name]:
                raise ValueError(
                    "参考骨 Rigify 类型不符："
                    + bone.name
                    + " 期望="
                    + types[bone.name]
                    + " 实际="
                    + actual_type
                )
    for name in _LIMB_ROOTS[kind]:
        if name not in reference.pose.bones:
            continue
        params = _rigify_params(reference.pose.bones[name])
        if params is None:
            continue
        if int(getattr(params, "segments", -1)) != configuration["segments"]:
            raise ValueError("参考肢体 segments 与配置不一致：" + name)
        if int(getattr(params, "bbones", -1)) != configuration["bbones"]:
            raise ValueError("参考肢体 bbones 与配置不一致：" + name)
    if reference.constraints:
        raise ValueError("参考 Object 不允许附加约束")
    if hasattr(reference.data, "constraints") and reference.data.constraints:
        raise ValueError("参考 Armature 数据不允许附加约束")
    for pose_bone in reference.pose.bones:
        if pose_bone.constraints:
            raise ValueError("参考 PoseBone 不允许附加约束：" + pose_bone.name)


def read_reference(reference) -> dict:
    """Read and strictly validate the modular reference record."""
    if not isinstance(reference, bpy.types.Object) or reference.type != "ARMATURE":
        raise ValueError("read_reference 需要 ARMATURE Object")
    raw = reference.get(REFERENCE_KEY)
    if raw is None:
        raise ValueError("Object 不是模块化参考：缺少 " + REFERENCE_KEY)
    if not isinstance(raw, str):
        raise ValueError("模块化参考记录不是 JSON 字符串")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("模块化参考记录 JSON 损坏") from error
    if not isinstance(record, dict) or set(record) != _REFERENCE_FIELDS:
        raise ValueError("模块化参考记录字段集无效")
    if record["schema"] != REFERENCE_SCHEMA:
        raise ValueError("模块化参考 schema 无效")
    kind = _normalize_kind(record["kind"])
    if kind != record["kind"]:
        raise ValueError("模块化参考 kind 未规范化")
    _validate_label(record["label"])
    configuration = _validate_configuration_record(record["configuration"])
    if configuration["kind"] != kind:
        raise ValueError("模块化参考 configuration.kind 与记录不一致")
    source = record["source"]
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        raise ValueError("模块化参考 source 字段集无效")
    if source["template"] != _detail_template(kind, configuration["enabled_modules"]):
        raise ValueError("模块化参考 source.template 与 kind 不一致")
    if source["mode"] not in {"rigify_metarig_modules", "composed_rigify_metarig_modules"}:
        raise ValueError("模块化参考 source.mode 无效")
    version = source["rigify_version"]
    if not isinstance(version, list) or any(type(item) is not int for item in version):
        raise ValueError("模块化参考 source.rigify_version 无效")
    composed_from = source["composed_from"]
    composed_signature = source["composed_from_signature"]
    if composed_from is not None and (not isinstance(composed_from, str) or not composed_from):
        raise ValueError("模块化参考 source.composed_from 无效")
    if composed_signature is not None and (
        not isinstance(composed_signature, str) or not _sha_text(composed_signature)
    ):
        raise ValueError("模块化参考 source.composed_from_signature 无效")
    if composed_from is None:
        if composed_signature is not None:
            raise ValueError("原生参考不应带组合来源签名")
    elif composed_signature is None:
        raise ValueError("组合参考缺少组合来源签名")
    if (source["mode"] == "composed_rigify_metarig_modules") != (composed_from is not None):
        raise ValueError("模块化参考 source.mode 与 composed_from 不一致")
    signature = record["rest_signature"]
    confirmed = record["confirmed"]
    confirmation = record["confirmation"]
    if type(confirmed) is not bool:
        raise ValueError("模块化参考 confirmed 必须是 bool")
    if signature is not None and (
        not isinstance(signature, str) or not _sha_text(signature)
    ):
        raise ValueError("模块化参考 rest_signature 无效")
    if confirmed:
        if signature is None or not isinstance(confirmation, dict):
            raise ValueError("已确认参考缺少 rest 签名或确认记录")
        if set(confirmation) != _CONFIRMATION_FIELDS:
            raise ValueError("模块化参考确认记录字段集无效")
        if (
            confirmation["mode"] != "explicit"
            or confirmation["scope"] != "current_rest_and_generation_settings"
            or confirmation["auto_positioning_quality_claimed"] is not False
            or not isinstance(confirmation["confirmed_at"], str)
            or not confirmation["confirmed_at"]
        ):
            raise ValueError("模块化参考确认记录无效")
    elif signature is not None or confirmation is not None:
        raise ValueError("未确认参考不应带 rest 签名或确认记录")
    for field in ("created_at", "updated_at"):
        if not isinstance(record[field], str) or not record[field]:
            raise ValueError("模块化参考时间字段无效：" + field)
    _validate_reference_structure(reference, record)
    return _deep(record)


def _prepare_common(scene, *, kind, label, configuration, allow_runtime_enable):
    _require_object_mode()
    if scene != bpy.context.scene:
        raise ValueError("模块化参考需要当前 Scene")
    config = _normalize_configuration(kind, configuration)
    resolved_label = _resolve_label(label, config["kind"])
    status = _ensure_rigify(bool(allow_runtime_enable))
    return config, resolved_label, status


def prepare_reference(
    scene,
    *,
    kind="HUMAN",
    label="基础人形",
    configuration=None,
    allow_runtime_enable=False,
):
    """Create a new independent modular reference; never edits an old object."""
    config, resolved_label, status = _prepare_common(
        scene,
        kind=kind,
        label=label,
        configuration=configuration,
        allow_runtime_enable=allow_runtime_enable,
    )
    before = _all_ids()
    old_active, old_selected = _selection()
    owned_ids = []
    reference = None
    try:
        reference = _create_template_reference(
            scene, config["kind"], resolved_label, owned_ids=owned_ids, enabled=config["enabled_modules"]
        )
        module_bones = _apply_modules(reference, config["kind"], config)
        record = _make_reference_record(
            config["kind"],
            resolved_label,
            config,
            module_bones,
            status,
        )
        _validate_reference_structure(reference, record)
        reference[REFERENCE_KEY] = _canonical(record)
        _select_only(reference)
        return reference
    except Exception:
        if reference is not None:
            if reference.mode != "OBJECT":
                try:
                    bpy.ops.object.mode_set(mode="OBJECT")
                except RuntimeError:
                    pass
        cleanup = _cleanup_owned_ids(
            owned_ids,
            before=before,
            active=old_active,
            selected=old_selected,
        )
        _publish_cleanup(cleanup)
        raise


def _validate_source_reference(reference) -> dict:
    record = read_reference(reference)
    if reference.name not in bpy.context.scene.objects:
        raise ValueError("源参考不属于当前 Scene")
    return record


def compose_reference(scene, reference, *, configuration, label=None, allow_runtime_enable=False):
    """Create a new reference with a new module combination.

    Bones shared with the source reference keep the author's current local rest.
    The source reference and all existing generated rigs are left untouched.
    """
    _require_object_mode()
    _require_scene_object(scene, reference)
    if configuration is None:
        raise ValueError("compose_reference 需要明确的 configuration")
    source_record = _validate_source_reference(reference)
    config = _normalize_configuration(source_record["kind"], configuration)
    resolved_label = _resolve_label(
        label, source_record["kind"], fallback=source_record["label"]
    )
    status = _ensure_rigify(bool(allow_runtime_enable))
    source_signature = _rest_signature(reference, source_record)
    source_rest = _rest_geometry(reference)
    source_matrix = reference.matrix_world.copy()
    source_bones = set(reference.data.bones.keys())
    source_data = reference.data
    before = _all_ids()
    old_active, old_selected = _selection()
    owned_ids = []
    composed = None
    try:
        # Correctives add no reference bones. Rebuilding an unchanged metarig
        # through EditBone round-trips perturbs short-bone axes and later IK
        # frames. Preserve the actual authored datablock when physical modules
        # are unchanged, rather than compensating with looser quality budgets.
        physical = set(config["enabled_modules"]) - {"correctives"}
        source_physical = set(source_record["configuration"]["enabled_modules"]) - {"correctives"}
        if physical == source_physical:
            data = reference.data.copy()
            _own(owned_ids, data)
            composed = reference.copy()
            _own(owned_ids, composed)
            composed.data = data
            composed.name = _unique_name(resolved_label, bpy.data.objects)
            data.name = _unique_name(resolved_label, bpy.data.armatures)
            scene.collection.objects.link(composed)
            composed.matrix_world = source_matrix.copy()
            _set_limb_parameters(composed, config)
            module_bones = _expected_module_bones(config["kind"], config["enabled_modules"])
        else:
            composed = _create_template_reference(
                scene, config["kind"], resolved_label, owned_ids=owned_ids, enabled=config["enabled_modules"]
            )
            module_bones = _apply_modules(
                composed,
                config["kind"],
                config,
                source_rest=source_rest,
                source_matrix=source_matrix,
            )
        common = _verify_common_rest(composed, source_rest)
        if common["errors"]:
            raise RuntimeError("组合参考未保留全部共有骨编辑 rest：" + _canonical(common))
        record = _make_reference_record(
            config["kind"],
            resolved_label,
            config,
            module_bones,
            status,
            composed_from=reference.name,
            composed_from_signature=source_signature,
        )
        _validate_reference_structure(composed, record)
        if reference.data != source_data or set(reference.data.bones.keys()) != source_bones:
            raise RuntimeError("组合期间源参考发生变化")
        if _rest_signature(reference, source_record) != source_signature:
            raise RuntimeError("组合期间源参考 rest 或生成设置发生变化")
        composed[REFERENCE_KEY] = _canonical(record)
        _select_only(composed)
        return composed
    except Exception:
        if composed is not None and composed.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                pass
        cleanup = _cleanup_owned_ids(
            owned_ids,
            before=before,
            active=old_active,
            selected=old_selected,
        )
        _publish_cleanup(cleanup)
        raise


def reference_confirmation(reference) -> dict:
    """Compare the actual rest/settings/object matrix with the signed confirmation."""
    record = read_reference(reference)
    current = _rest_signature(reference, record)
    confirmed = bool(record.get('confirmed'))
    matches = confirmed and current == record.get('rest_signature')
    return {'confirmation_record_present':confirmed, 'current':bool(matches),
            'confirmation_expired':bool(confirmed and not matches),
            'current_signature':current, 'confirmed_signature':record.get('rest_signature')}


def confirm_reference(reference, *, allow_runtime_enable=False) -> dict:
    """Explicitly confirm the reference's current rest and generation settings."""
    _require_object_mode()
    if not isinstance(reference, bpy.types.Object) or reference.type != "ARMATURE":
        raise ValueError("confirm_reference 需要 ARMATURE Object")
    if reference.name not in bpy.context.scene.objects:
        raise ValueError("参考不属于当前 Scene")
    record = read_reference(reference)
    _ensure_rigify(bool(allow_runtime_enable))
    signature = _rest_signature(reference, record)
    timestamp = _now()
    record["rest_signature"] = signature
    record["confirmed"] = True
    record["confirmation"] = {
        "mode": "explicit",
        "scope": "current_rest_and_generation_settings",
        "auto_positioning_quality_claimed": False,
        "confirmed_at": timestamp,
    }
    record["updated_at"] = timestamp
    _validate_reference_structure(reference, record)
    reference[REFERENCE_KEY] = _canonical(record)
    return _deep(record)


def _load_correctives_module():
    package = globals().get("__package__")
    if package:
        candidates = [package + ".modular_correctives"]
    else:
        candidates = ["modular_correctives"]
    last_error = None
    for name in candidates:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name in {name, "modular_correctives"}:
                last_error = error
                continue
            raise
        if not callable(getattr(module, "install", None)):
            raise ValueError("modular_correctives 缺少可调用 install(rig, reference, metadata)")
        return module
    raise ValueError(
        "correctives 已启用但 modular_correctives 缺失；拒绝静默忽略辅助骨安装。"
    ) from last_error


def _constraint_targets(rig, pose_bone):
    priority = {
        "COPY_TRANSFORMS": 0,
        "ARMATURE": 1,
        "STRETCH_TO": 2,
        "DAMPED_TRACK": 3,
        "COPY_LOCATION": 4,
        "COPY_ROTATION": 5,
        "COPY_SCALE": 6,
        "IK": 7,
    }
    found = []
    for constraint in pose_bone.constraints:
        if constraint.type == "ARMATURE":
            for target in constraint.targets:
                if (
                    target.target == rig
                    and target.subtarget
                    and target.subtarget in rig.data.bones
                    and target.subtarget != pose_bone.name
                ):
                    found.append(
                        (
                            priority.get(constraint.type, 99),
                            -float(target.weight),
                            target.subtarget,
                            "constraint:ARMATURE",
                        )
                    )
            continue
        target = getattr(constraint, "target", None)
        subtarget = getattr(constraint, "subtarget", "")
        if (
            target == rig
            and subtarget
            and subtarget in rig.data.bones
            and subtarget != pose_bone.name
        ):
            found.append(
                (
                    priority.get(constraint.type, 99),
                    0.0,
                    subtarget,
                    "constraint:" + constraint.type,
                )
            )
    if not found:
        return None
    found.sort()
    return found[0][2], found[0][3]


def _strip_number_suffix(name: str) -> str:
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return name


def _mechanism_target(rig, name: str):
    pose_bone = rig.pose.bones[name]
    direct = _constraint_targets(rig, pose_bone)
    if direct is not None:
        return direct
    base = name[4:] if name.startswith("DEF-") else name
    for candidate_base in (base, _strip_number_suffix(base)):
        for prefix in ("ORG-", "MCH-"):
            candidate = prefix + candidate_base
            if candidate in rig.data.bones and candidate != name:
                return candidate, "prefix:" + prefix[:-1]
    return name, "self"


def _build_mechanism_mapping(rig):
    mapping = {}
    methods = {}
    for bone in sorted(rig.data.bones, key=lambda item: item.name):
        if not bone.use_deform:
            continue
        target, method = _mechanism_target(rig, bone.name)
        if target not in rig.data.bones:
            raise RuntimeError("机制映射目标不是真实骨：" + bone.name + " -> " + target)
        mapping[bone.name] = target
        methods[method] = methods.get(method, 0) + 1
    if not mapping:
        raise RuntimeError("生成骨架没有可解析的形变骨")
    return mapping, methods


def _hide_tweak_collections(rig) -> list[str]:
    hidden = []
    for collection in rig.data.collections_all:
        if "tweak" not in collection.name.lower():
            continue
        try:
            collection.is_visible = False
            hidden.append(collection.name)
        except (AttributeError, RuntimeError):
            pass
    return sorted(hidden)


def _require_control(rig, names, module: str) -> str:
    for name in names:
        if name in rig.pose.bones:
            return name
    raise RuntimeError("模块 " + module + " 缺少预期原生控制：" + ", ".join(names))


def _verify_generated_modules(rig, configuration) -> None:
    enabled = set(configuration["enabled_modules"])
    if "root" not in rig.pose.bones:
        raise RuntimeError("生成骨架缺少 root 控制")
    if "torso" not in rig.pose.bones:
        raise RuntimeError("生成骨架缺少 torso 控制")
    if "head" in enabled:
        _require_control(rig, ("head", "neck"), "head")
    if "shoulder_girdle" in enabled:
        for side in ("L", "R"):
            _require_control(rig, ("scapula." + side,), "shoulder_girdle")
            if "ORG-scapula." + side not in rig.pose.bones:
                raise RuntimeError("生成肩胛模块缺少真实载体：ORG-scapula." + side)
            if "DEF-scapula." + side in rig.pose.bones:
                raise RuntimeError("肩胛载体不得静默新增配权列：DEF-scapula." + side)
    for side in ("L", "R"):
        if "forelimb." + side in enabled:
            _require_control(
                rig,
                (
                    "hand_ik." + side,
                    "upper_arm_ik_target." + side,
                    "hand_fk." + side,
                ),
                "forelimb." + side,
            )
        if "hindlimb." + side in enabled:
            _require_control(
                rig,
                (
                    "foot_ik." + side,
                    "thigh_ik_target." + side,
                    "thigh_fk." + side,
                ),
                "hindlimb." + side,
            )
    if "tail" in enabled:
        _require_control(rig, ("tail.001", "tail_master.001", "tail_master"), "tail")
    for side in ("L", "R"):
        if "fingers." + side in enabled:
            for digit in ("thumb", "f_index", "f_middle", "f_ring", "f_pinky"):
                for index in range(1, 4):
                    name = f"DEF-{digit}.{index:02}.{side}"
                    if name not in rig.data.bones or not rig.data.bones[name].use_deform:
                        raise RuntimeError("生成手指缺少形变骨：" + name)
    if "face" in enabled:
        for candidates in (("eyes", "eye.L"), ("jaw_master", "jaw"), ("brow.T.L",), ("lips.L", "lip.T")):
            _require_control(rig, candidates, "face")
    forbidden = []
    import re

    pattern = re.compile(
        r"(^|[-_])(" + ("face|nose|ear|lip|jaw|brow|lid|tongue|" if "face" not in enabled else "")
        + ("[fr]_(palm|index|middle|ring|pinky)|" if not enabled.intersection({"fingers.L", "fingers.R"}) else "")
        + r"breast|belly)([._-]|$)",
        re.IGNORECASE,
    )
    for bone in rig.data.bones:
        if bone.use_deform and pattern.search(bone.name):
            forbidden.append(bone.name)
    if forbidden:
        raise RuntimeError("生成骨架仍含范围外面部/细趾/breast形变骨：" + ", ".join(sorted(forbidden)))


def _verify_bbone_limits(rig, maximum) -> list[str]:
    errors = []
    limb_stems = {
        "upper_arm",
        "forearm",
        "hand",
        "thigh",
        "shin",
        "foot",
        "toe",
        "f_toe",
        "r_toe",
    }
    for bone in rig.data.bones:
        if not bone.use_deform:
            continue
        base = bone.name[4:] if bone.name.startswith("DEF-") else bone.name
        base = _strip_number_suffix(base)
        if base not in limb_stems:
            continue
        if bone.bbone_segments > maximum:
            errors.append(bone.name)
    if errors:
        raise RuntimeError(
            f"生成形变骨 B-Bone 段数超过 bbones={maximum}：" + ", ".join(sorted(errors)[:12])
        )
    return []


def _validate_generated_structure(rig, record: dict) -> None:
    if (
        not isinstance(rig, bpy.types.Object)
        or rig.type != "ARMATURE"
        or rig.library is not None
        or rig.data.library is not None
    ):
        raise ValueError("生成结果必须是本地 ARMATURE Object")
    kind = _normalize_kind(record["kind"])
    configuration = _validate_configuration_record(record["configuration"])
    if configuration["kind"] != kind:
        raise ValueError("生成记录 configuration.kind 与 kind 不一致")
    if record["schema"] != GENERATED_SCHEMA:
        raise ValueError("生成记录 schema 无效")
    for key in record:
        if key == GENERATED_KEY:
            raise ValueError("生成记录不允许嵌套自身身份键")
        if "c94" in key.lower():
            raise ValueError("生成记录不允许伪造 C94 身份：" + key)
    signature = record["reference_signature"]
    if not isinstance(signature, str) or not _sha_text(signature):
        raise ValueError("生成记录 reference_signature 无效")
    reference = record["reference"]
    if not isinstance(reference, str) or not reference:
        raise ValueError("生成记录 reference 无效")
    deforms = record["deforms"]
    if (
        not isinstance(deforms, list)
        or any(not isinstance(name, str) or not name for name in deforms)
        or deforms != sorted(set(deforms))
    ):
        raise ValueError("生成记录 deforms 必须是排序去重的骨名 list")
    actual = {bone.name for bone in rig.data.bones if bone.use_deform}
    if set(deforms) != actual:
        raise ValueError(
            "生成记录 deforms 与实际 use_deform 骨不一致：缺少="
            + str(sorted(actual - set(deforms)))
            + " 多出="
            + str(sorted(set(deforms) - actual))
        )
    mapping = record["mechanism_mapping"]
    if not isinstance(mapping, dict) or set(mapping) != set(deforms):
        raise ValueError("mechanism_mapping 必须完整覆盖 deforms")
    for name, target in mapping.items():
        if not isinstance(target, str) or target not in rig.data.bones:
            raise ValueError("mechanism_mapping 目标不是真实骨：" + name)
    controls = record.get("controls")
    if controls is not None:
        if (
            not isinstance(controls, list)
            or any(not isinstance(name, str) or name not in rig.pose.bones for name in controls)
            or controls != sorted(set(controls))
        ):
            raise ValueError("生成记录 controls 无效")
    module_bones = record.get("module_bones")
    if module_bones is not None and (
        not isinstance(module_bones, dict)
        or set(module_bones) != set(configuration["enabled_modules"])
    ):
        raise ValueError("生成记录 module_bones 模块键无效")
    primary = record.get("primary_deforms")
    auxiliary = record.get("auxiliary_specs")
    correctives = record.get("correctives")
    if (not isinstance(correctives,dict) or type(correctives.get('enabled')) is not bool
            or correctives['enabled'] != ('correctives' in configuration['enabled_modules'])):
        raise ValueError('辅助模块启用状态与实际生成配置不一致')
    if correctives['enabled'] and (not isinstance(primary,list) or not primary
                                  or not isinstance(auxiliary,list) or not auxiliary):
        raise ValueError('已启用辅助模块必须保留完整主体骨与辅助骨规格，拒绝全局扩散兜底')
    if not correctives['enabled'] and (primary is not None or auxiliary is not None):
        raise ValueError('未启用辅助模块的生成记录不得带入辅助分配规格')
    if primary is not None or auxiliary is not None:
        if (
            not isinstance(primary, list)
            or any(not isinstance(name, str) or not name for name in primary)
            or primary != sorted(set(primary))
            or not set(primary) <= set(deforms)
            or not isinstance(auxiliary, list)
        ):
            raise ValueError("生成记录 primary_deforms/auxiliary_specs 结构无效")
        auxiliary_names = set()
        for spec in auxiliary:
            if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
                raise ValueError("生成记录 auxiliary_specs 项无效")
            name = spec["name"]
            if (
                not name
                or name in auxiliary_names
                or name not in rig.data.bones
                or not rig.data.bones[name].use_deform
                or name not in set(deforms)
            ):
                raise ValueError("生成记录 auxiliary_specs 骨名无效：" + str(name))
            source = spec.get("source")
            if source is not None and (not isinstance(source, str) or source not in rig.data.bones):
                raise ValueError("生成记录 auxiliary_specs source 无效：" + name)
            donors = spec.get("donors")
            if donors is not None and (
                not isinstance(donors, list)
                or any(not isinstance(donor, str) or donor not in rig.data.bones for donor in donors)
            ):
                raise ValueError("生成记录 auxiliary_specs donors 无效：" + name)
            auxiliary_names.add(name)
        if set(deforms) - set(primary) != auxiliary_names:
            raise ValueError("生成记录辅助骨集合与额外 deforms 不一致")
    hidden = record.get("tweak_collections_hidden")
    if (
        not isinstance(hidden, list)
        or any(not isinstance(name, str) or name not in rig.data.collections_all for name in hidden)
    ):
        raise ValueError("生成记录 tweak_collections_hidden 无效")
    correctives = record.get("correctives")
    if not isinstance(correctives, dict) or type(correctives.get("enabled")) is not bool:
        raise ValueError("生成记录 correctives 字段无效")
    rigify_info = record.get("rigify")
    if (
        not isinstance(rigify_info, dict)
        or not isinstance(rigify_info.get("generator"), str)
        or not isinstance(rigify_info.get("version"), list)
        or any(type(item) is not int for item in rigify_info["version"])
    ):
        raise ValueError("生成记录 rigify 字段无效")
    if not isinstance(record.get("generated_at"), str) or not record["generated_at"]:
        raise ValueError("生成记录 generated_at 无效")
    limitations = record.get("limitations")
    if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
        raise ValueError("生成记录 limitations 无效")
    _verify_generated_modules(rig, configuration)
    _verify_bbone_limits(rig, configuration["bbones"])


def read_generated(rig) -> dict:
    """Read and strictly validate a modular generated-rig record."""
    if not isinstance(rig, bpy.types.Object) or rig.type != "ARMATURE":
        raise ValueError("read_generated 需要 ARMATURE Object")
    raw = rig.get(GENERATED_KEY)
    if raw is None:
        raise ValueError("Object 不是模块化生成骨架：缺少 " + GENERATED_KEY)
    if not isinstance(raw, str):
        raise ValueError("模块化生成记录不是 JSON 字符串")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("模块化生成记录 JSON 损坏") from error
    required = {
        "schema",
        "kind",
        "label",
        "configuration",
        "reference",
        "reference_signature",
        "module_bones",
        "deforms",
        "mechanism_mapping",
        "controls",
        "tweak_collections_hidden",
        "correctives",
        "rigify",
        "generated_at",
        "limitations",
    }
    if not isinstance(record, dict) or not required <= set(record):
        raise ValueError("模块化生成记录缺少必需字段")
    if record["schema"] == "c94.generated.v1":
        raise ValueError("拒绝把 C94 记录当模块化生成记录读取")
    _validate_label(record["label"])
    _validate_generated_structure(rig, record)
    return _deep(record)


def _generated_label(reference_record, label) -> str:
    if label is None:
        base = reference_record["label"]
    else:
        base = str(label).strip()
        if not base:
            base = reference_record["label"]
    desired = base + "__模块化生成"
    if len(desired.encode("utf-8")) > 63:
        raise ValueError("生成 label 加后缀后超过 Blender 63 字节对象名上限")
    return base, desired


def _prepare_generation_metadata(
    rig,
    reference,
    reference_record,
    *,
    label,
    configuration,
    reference_signature,
    status,
    tweak_collections_hidden,
    correctives_module,
):
    mapping, mapping_methods = _build_mechanism_mapping(rig)
    deforms = sorted(mapping)
    controls = sorted(
        pose_bone.name for pose_bone in rig.pose.bones if pose_bone.custom_shape is not None
    )
    return {
        "schema": GENERATED_SCHEMA,
        "label": label,
        "kind": reference_record["kind"],
        "configuration": _deep(configuration),
        "reference": reference.name,
        "reference_signature": reference_signature,
        "module_bones": _deep(reference_record["module_bones"]),
        "deforms": deforms,
        "mechanism_mapping": mapping,
        "mechanism_mapping_methods": mapping_methods,
        "controls": controls,
        "tweak_collections_hidden": list(tweak_collections_hidden),
        "correctives": (
            {
                "enabled": True,
                "status": "installing",
                "module": correctives_module.__name__,
            }
            if correctives_module is not None
            else {"enabled": False}
        ),
        "rigify": {
            "version": list(status.get("rigify_version") or ()),
            "blender": bpy.app.version_string,
            "generator": "rigify.generate.generate_rig",
        },
        "generated_at": _now(),
        "limitations": [
            "first modular scope: no independent face or fine finger/toe controls",
            "corrective-bone algorithm owned by modular_correctives",
            "no C94 private asset identity or compatibility claim",
        ],
    }


def _install_correctives(module, rig, reference, metadata):
    protected = {
        key: _deep(metadata.get(key)) for key in _PROTECTED_GENERATED_FIELDS
    }
    primary_before = list(metadata["deforms"])
    returned = module.install(rig, reference, metadata)
    if not isinstance(returned, dict):
        raise ValueError("modular_correctives.install 必须返回 metadata dict")
    for key, value in protected.items():
        if key in returned and _canonical(returned[key]) != _canonical(value):
            raise ValueError("correctives 不得改写生成记录身份字段：" + key)
    metadata.update(_deep(returned))
    if rig.name not in bpy.data.objects:
        raise RuntimeError("correctives 安装后生成骨架不再存在")
    deforms = metadata.get("deforms")
    if (
        not isinstance(deforms, list)
        or any(not isinstance(name, str) or not name for name in deforms)
        or len(deforms) != len(set(deforms))
    ):
        raise ValueError("correctives 返回的 deforms 无效")
    actual = {bone.name for bone in rig.data.bones if bone.use_deform}
    declared = set(deforms)
    if declared != actual:
        raise ValueError(
            "correctives 返回的 deforms 与实际 use_deform 骨不完全一致：缺少="
            + str(sorted(actual - declared))
            + " 多出="
            + str(sorted(declared - actual))
        )
    primary = metadata.get("primary_deforms")
    if (
        not isinstance(primary, list)
        or any(not isinstance(name, str) or not name for name in primary)
        or len(primary) != len(set(primary))
        or set(primary) != set(primary_before)
    ):
        raise ValueError("correctives 必须返回未被改写的 primary_deforms")
    specs = metadata.get("auxiliary_specs")
    if not isinstance(specs, list):
        raise ValueError("correctives 必须返回 auxiliary_specs list")
    auxiliary_names = set()
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not spec["name"]:
            raise ValueError("auxiliary_specs 项缺少明确骨名")
        name = spec["name"]
        if name in auxiliary_names:
            raise ValueError("auxiliary_specs 含重复骨名：" + name)
        if name not in rig.data.bones or not rig.data.bones[name].use_deform:
            raise ValueError("辅助骨不是真实 use_deform 骨：" + name)
        source = spec.get("source")
        if source is not None and (not isinstance(source, str) or source not in rig.data.bones):
            raise ValueError("辅助骨 source 不是真实骨：" + name)
        donors = spec.get("donors")
        if donors is not None and (
            not isinstance(donors, list)
            or any(not isinstance(donor, str) or donor not in rig.data.bones for donor in donors)
        ):
            raise ValueError("辅助骨 donors 含非真实骨：" + name)
        auxiliary_names.add(name)
    if declared - set(primary) != auxiliary_names:
        raise ValueError(
            "correctives 的额外 deforms 与 auxiliary_specs 不一致：额外="
            + str(sorted(declared - set(primary)))
            + " 规格="
            + str(sorted(auxiliary_names))
        )
    mapping = metadata.get("mechanism_mapping")
    if not isinstance(mapping, dict) or set(mapping) != declared:
        raise ValueError("correctives 的 mechanism_mapping 未完整覆盖 deforms")
    for name, target in mapping.items():
        if not isinstance(target, str) or target not in rig.data.bones:
            raise ValueError("correctives 的 mechanism_mapping 目标不是真实骨：" + name)
    metadata["deforms"] = sorted(deforms)
    metadata["primary_deforms"] = list(primary)
    metadata["auxiliary_specs"] = _deep(specs)
    if isinstance(metadata.get("correctives"), dict):
        information = metadata["correctives"]
        information["enabled"] = True
        information["status"] = "installed"
        information["module"] = module.__name__
    else:
        metadata["correctives"] = {
            "enabled": True,
            "status": "installed",
            "module": module.__name__,
        }
    metadata["controls"] = sorted(
        pose_bone.name for pose_bone in rig.pose.bones if pose_bone.custom_shape is not None
    )


def _temporary_metarig(reference, object_name: str, owned_ids):
    data = reference.data.copy()
    _own(owned_ids, data)
    temp = reference.copy()
    _own(owned_ids, temp)
    temp.data = data
    temp.name = object_name
    data.name = object_name + "_骨架"
    bpy.context.scene.collection.objects.link(temp)
    temp[REFERENCE_KEY] = reference.get(REFERENCE_KEY, "")
    temp.animation_data_clear()
    if hasattr(temp.data, "animation_data"):
        temp.data.animation_data_clear()
    for attr in (
        "rigify_target_rig",
        "rigify_widgets_collection",
        "rigify_rig_ui",
        "rigify_finalize_script",
    ):
        if hasattr(data, attr):
            try:
                setattr(data, attr, None)
            except (AttributeError, TypeError, RuntimeError):
                pass
    data.rigify_rig_basename = object_name
    _select_only(temp)
    return temp, data


def _create_owned_widget_collection(scene, owned_ids):
    name = "WGTS_modular_" + uuid.uuid4().hex[:12]
    collection = bpy.data.collections.new(name)
    _own(owned_ids, collection)
    scene.collection.children.link(collection)
    collection.hide_viewport = True
    collection.hide_render = True
    return collection


def _register_rigify_products(temp_data, widget_collection, owned_ids, existing_ids=()) -> set:
    """Register only explicitly reachable products returned by Rigify."""
    product_ids = set()
    existing_ids = set(existing_ids)
    def register(*items):
        items = tuple(item for item in items if isinstance(item, bpy.types.ID) and item not in existing_ids)
        _own(owned_ids, *items)
        product_ids.update(items)
    try:
        generated = getattr(temp_data, "rigify_target_rig", None)
    except (AttributeError, ReferenceError):
        generated = None
    if isinstance(generated, bpy.types.Object):
        register(generated, generated.data)
    try:
        ui_text = getattr(temp_data, "rigify_rig_ui", None)
    except (AttributeError, ReferenceError):
        ui_text = None
    if isinstance(ui_text, bpy.types.Text):
        if ui_text not in existing_ids:
            # The shared workbench provides the daily controls. Keep the
            # optional generated Rigify panel as editable, manually run Text;
            # opening a native character must not request file auto-execution.
            ui_text.use_module = False
        register(ui_text)
    try:
        active_collection = getattr(temp_data, "rigify_widgets_collection", None)
    except (AttributeError, ReferenceError):
        active_collection = None
    if active_collection is not widget_collection:
        # The dedicated empty collection should be the only widget collection
        # used.  Do not claim an unproven replacement.
        active_collection = widget_collection
    register(active_collection)
    try:
        widget_objects = list(active_collection.objects)
    except (AttributeError, ReferenceError):
        widget_objects = []
    if isinstance(generated, bpy.types.Object):
        try:
            for pose_bone in generated.pose.bones:
                shape = pose_bone.custom_shape
                if (
                    isinstance(shape, bpy.types.Object)
                    and (shape in widget_objects or widget_collection in shape.users_collection)
                ):
                    register(shape, shape.data)
        except (AttributeError, RuntimeError, ReferenceError):
            pass
    return {item for item in product_ids if isinstance(item, bpy.types.ID)}


def generate(scene, reference, *, label=None, allow_runtime_enable=False, standard_controls=False):
    """Generate a new native Rigify rig from a confirmed, unchanged reference."""
    _require_object_mode()
    _require_scene_object(scene, reference)
    record = read_reference(reference)
    if not record["confirmed"] or record["rest_signature"] is None:
        raise ValueError("参考尚未明确确认，拒绝生成")
    configuration = record["configuration"]
    correctives_module = None
    status = _ensure_rigify(bool(allow_runtime_enable))
    if "correctives" in configuration["enabled_modules"]:
        correctives_module = _load_correctives_module()
    current_signature = _rest_signature(reference, record)
    if current_signature != record["rest_signature"]:
        raise ValueError("参考 rest 或生成设置已改变，确认已过期；请重新确认后再生成")
    base_label, desired_name = _generated_label(record, label)
    rig_name = _unique_name(desired_name, bpy.data.objects)
    if rig_name in bpy.data.armatures:
        rig_name = _unique_name(desired_name, bpy.data.armatures)
    prior_generated = [
        (obj.name, obj.as_pointer(), obj.data.as_pointer())
        for obj in bpy.data.objects
        if obj.type == "ARMATURE" and GENERATED_KEY in obj
    ]
    reference_data = reference.data
    reference_pointer = reference.as_pointer()
    before = _all_ids()
    old_active, old_selected = _selection()
    owned_ids = []
    product_ids = set()
    temp = temp_data = generated = widget_collection = None
    try:
        temp, temp_data = _temporary_metarig(
            reference,
            "_modular_generate_" + uuid.uuid4().hex[:12],
            owned_ids,
        )
        temp.data.rigify_rig_basename = rig_name
        widget_collection = _create_owned_widget_collection(scene, owned_ids)
        temp_data.rigify_widgets_collection = widget_collection
        from rigify.generate import generate_rig

        try:
            generate_rig(bpy.context, temp)
        finally:
            product_ids = _register_rigify_products(
                temp_data, widget_collection, owned_ids, existing_ids=before
            )
        generated = getattr(temp.data, "rigify_target_rig", None)
        if generated is None or generated.name not in bpy.data.objects:
            raise RuntimeError("Rigify 未返回新的目标骨架")
        if generated == reference or generated == temp:
            raise RuntimeError("Rigify 目标骨架身份异常")
        if generated in before:
            raise RuntimeError("生成器返回原有对象，拒绝认领或修改旧骨架")
        # Rigify restores a newly-created target's original identity matrix
        # after joining the metarig. Its bones remain in metarig local space.
        generated.matrix_world = reference.matrix_world.copy()
        bpy.context.view_layer.update()
        if reference.as_pointer() != reference_pointer or reference.data != reference_data:
            raise RuntimeError("生成期间源参考对象被替换")
        if _rest_signature(reference, record) != record["rest_signature"]:
            raise RuntimeError("生成期间源参考 rest 或生成设置发生变化，候选已撤回")
        for name, pointer, data_pointer in prior_generated:
            current = bpy.data.objects.get(name)
            if (
                current is None
                or current.as_pointer() != pointer
                or current.data.as_pointer() != data_pointer
                or GENERATED_KEY not in current
            ):
                raise RuntimeError("生成期间旧模块化候选被覆盖或删除：" + name)
        _verify_generated_modules(generated, configuration)
        _verify_bbone_limits(generated, configuration["bbones"])
        hidden = _hide_tweak_collections(generated)
        metadata = _prepare_generation_metadata(
            generated,
            reference,
            record,
            label=base_label,
            configuration=configuration,
            reference_signature=record["rest_signature"],
            status=status,
            tweak_collections_hidden=hidden,
            correctives_module=correctives_module,
        )
        # The base modular class/schema is already built here.  Optional
        # correctives extend that metadata; only the returned, validated
        # metadata is written as rigging_modular_v1 (never a C94 identity).
        if correctives_module is not None:
            _install_correctives(correctives_module, generated, reference, metadata)
            if (
                reference.as_pointer() != reference_pointer
                or reference.data != reference_data
                or _rest_signature(reference, record) != record["rest_signature"]
            ):
                raise RuntimeError("correctives 安装期间源参考发生变化，候选已撤回")
        _validate_generated_structure(generated, metadata)
        alignment = []
        alignment_exempt = []
        reversed_reference_segments = []
        primary = set(metadata.get('primary_deforms', metadata['deforms']))
        for name in sorted(primary):
            source_name = name.removeprefix('DEF-')
            reference_bone = reference.data.bones.get(source_name)
            if reference_bone is None and name.endswith(".001") and name[:-4] in primary:
                continue  # Count both native limb segments at their reference bone.
            if "face" in configuration["enabled_modules"] and (
                    source_name in HUMAN_DETAIL_BONES["face"] or reference_bone is None):
                alignment_exempt.append(name)
                continue  # super_face constructs its own connected deformation mesh.
            if reference_bone is None:
                raise ValueError('形变输出缺少可核对的参考骨：'+name)
            bone = generated.data.bones[name]
            # Rigify's super_tail deforms run toward the attachment, opposite
            # to the four authored reference segments. Preserve directed
            # checks everywhere else; arbitrary reversed limbs must fail.
            reverse = ('tail' in configuration['enabled_modules'] and
                       name in {'DEF-tail.001','DEF-tail.002','DEF-tail.003','DEF-tail.004'})
            reference_head,reference_tail = reference_bone.head_local,reference_bone.tail_local
            if reverse:
                reference_head,reference_tail = reference_tail,reference_head
                reversed_reference_segments.append(name)
            endpoints = [(bone.head_local, reference_head), (bone.tail_local, reference_tail)]
            extra = generated.data.bones.get(name + ".001") if configuration["segments"] == 2 else None
            if extra and extra.name in primary and source_name + ".001" not in reference.data.bones:
                midpoint = (reference_head + reference_tail) * 0.5
                endpoints = [(bone.head_local, reference_head), (bone.tail_local, midpoint),
                             (extra.head_local, midpoint), (extra.tail_local, reference_tail)]
            alignment.append(max((generated.matrix_world @ a - reference.matrix_world @ b).length
                                 for a, b in endpoints))
        alignment_error = max(alignment,default=0)*float(scene.unit_settings.scale_length)
        if alignment_error>1e-5:
            raise ValueError(f'生成骨与已确认参考的世界坐标不一致（{alignment_error:g}米），候选已撤回')
        metadata['reference_world_alignment']={'matched_primary_bones':len(alignment),
            'max_endpoint_error_meters':alignment_error,
            'reversed_reference_segments':reversed_reference_segments,
            'native_face_deforms':alignment_exempt,
            'endpoint_policy':'directed reference endpoints and native segment joints; super_tail reverses direction'}
        from . import modular_neutral
        neutral_report = modular_neutral.capture(generated, metadata)
        generated[GENERATED_KEY] = _canonical(metadata)
        if standard_controls:
            from . import control_layer

            control_ids = []
            control_layer.build(generated, family='rigify_human', owned_ids=control_ids)
            owned_ids.extend(control_ids)
            product_ids.update(control_ids)
        _select_only(generated)
        # Success keeps the product IDs.  Only the temporary metarig (and its
        # data) are released; products are protected from this cleanup.
        cleanup = _cleanup_owned_ids(
            owned_ids,
            before=before,
            active=generated,
            selected=[generated],
            protected_ids=product_ids,
            report_unowned=True,
        )
        _publish_cleanup(cleanup)
        report = {
            "label": base_label,
            "kind": record["kind"],
            "configuration": _deep(configuration),
            "reference": reference.name,
            "reference_signature": record["rest_signature"],
            "rig": generated.name,
            "bones": len(generated.data.bones),
            "deforms": len(metadata["deforms"]),
            "controls": len(metadata["controls"]),
            "mechanism_mapping_methods": _deep(
                metadata.get("mechanism_mapping_methods", {})
            ),
            "tweak_collections_hidden": hidden,
            "correctives_installed": correctives_module is not None,
            "correctives_module": (
                correctives_module.__name__ if correctives_module is not None else None
            ),
            "reference_preserved": True,
            "reference_world_alignment": metadata['reference_world_alignment'],
            "native_neutral_spatial_error_meters": neutral_report['spatial_rest_error_meters'],
            "native_neutral_probe_count": neutral_report['probe_count'],
            "prior_generated_preserved": True,
            "standard_controls": bool(standard_controls),
        }
        return {"rig": generated, "reference": reference, "report": report}
    except Exception:
        if temp is not None and temp.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError:
                pass
        cleanup = _cleanup_owned_ids(
            owned_ids,
            before=before,
            active=old_active,
            selected=old_selected,
            groups=[product_ids] if product_ids else None,
        )
        _publish_cleanup(cleanup)
        raise
