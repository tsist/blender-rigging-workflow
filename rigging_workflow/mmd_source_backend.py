# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, inspectable PMX/VMD source backend for the C94 retarget core.

The module intentionally does not ship a PMX/VMD interpreter.  It uses the
*installed* MMD Tools add-on only while importing a new source, records every
piece of source metadata needed by the shared retarget core as plain JSON, and
then supports cold reopen/validation/conversion without registering MMD Tools.

Public API:

``inspect_backend``
    Static version/licence/source identity inspection.  No import, enable,
    install, preference write or scene mutation.

``capabilities``
    Explicit capability/limitation declaration for UI/API consumers.

``import_source``
    Background-only PMX+VMD import into a new Scene.  Writes a native .blend,
    a pure-JSON source profile, a persistent Scene source descriptor and a
    descriptor binding the artifact hashes.  Never writes PMX/VMD/original
    .blend inputs.

``validate_persisted_source`` / ``mapping_from_profile`` /
``convert_from_profile``
    Cold path that never imports or registers MMD Tools.  The persisted
    mapping/profile is treated as data, validated against real object IDs,
    rest/parent signatures and action/slot timebases, then handed to
    :func:`rigging_workflow.retarget_mmd.convert_mmd`.
"""

from __future__ import annotations

import bpy
import addon_utils
import hashlib
import importlib
import importlib.util
import inspect
import json
import math
import os
import sys
import tomllib
import unicodedata
import uuid
import zipfile
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path

from mathutils import Matrix, Vector

from . import pose_switching as pose


SCHEMA = "c94.retarget.mmd_source.v1"
PROFILE_SCHEMA = "c94.retarget.mmd_profile.v1"
SCENE_SCHEMA = "c94.retarget.mmd_source_scene.v1"
DESCRIPTOR_SCHEMA = "c94.retarget.mmd_source_descriptor.v1"
MAPPING_SCHEMA = "c94.retarget.mapping.v1"
MAPPING_PROFILE_SCHEMA = "c94.retarget.mmd.v1"

SCENE_KEY = "c94_mmd_source_v1"
TIME_KEY = "c94_retarget_timebase_v1"
UID_KEY = "c94_mmd_source_uid_v1"
ROLE_KEY = "c94_mmd_source_role_v1"
PROFILE_SHA_KEY = "c94_mmd_source_profile_sha256_v1"
BACKEND_KEY = "c94_mmd_source_backend_v1"

BACKEND_ID = "mmd_tools"
BACKEND_EXTENSION_ID = "bl_ext.blender_org.mmd_tools"
VERIFIED_BACKEND_VERSION = "4.5.14"
VERIFIED_BACKEND_LICENSE = "GPL-3.0-or-later"
SUPPORTED_AXIS = "MMD_XZY_SWIZZLE"
SUPPORTED_SOURCE_FPS = 30.0
SUPPORTED_SCALE = 0.08

GAZE_NAMES = ("両目", "両目先", "左目", "左目先", "右目", "右目先")
FOOT_CHAIN_NAMES = (
    "左足", "左足D", "左ひざ", "左ひざD", "左足首", "左足首D",
    "右足", "右足D", "右ひざ", "右ひざD", "右足首", "右足首D",
)
FINGER_WORDS = ("thumb", "index", "middle", "ring", "pinky")
FINGER_SIDES = ("L", "R")

BACKEND_FILES = (
    "__init__.py",
    "blender_manifest.toml",
    "LICENSE",
    "operators/fileio.py",
    "core/pmx/__init__.py",
    "core/pmx/importer.py",
    "core/vmd/__init__.py",
    "core/vmd/importer.py",
)


def require(value, message):
    if not value:
        raise ValueError(message)


def nfkc(value):
    return unicodedata.normalize("NFKC", value or "").strip()


def canonical_hash(value):
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def matrix_list(matrix):
    return [[float(matrix[r][c]) for c in range(4)] for r in range(4)]


def vector_list(vector):
    return [float(vector[0]), float(vector[1]), float(vector[2])]


def _optional_list(value):
    return list(value) if value is not None else None


def _file_descriptor(path):
    path = Path(path).resolve(strict=True)
    require(path.is_file(), "输入不是文件：" + str(path))
    return {
        "file": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _safe_extract_wheel(wheel, destination):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            candidate = (destination / name).resolve()
            require(candidate.is_relative_to(destination), "wheel包含越界路径：" + name)
        archive.extractall(destination)
    return destination


def _wheel_metadata(wheel):
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        license_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/LICENSE.txt")
        ]
        require(len(metadata_names) == 1, "OpenCC wheel元数据数量异常")
        parsed = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        license_text = (
            archive.read(license_names[0]).decode("utf-8", errors="replace")
            if license_names
            else ""
        )
        return {
            "name": parsed.get("Name"),
            "version": parsed.get("Version"),
            "license": parsed.get("License"),
            "license_text_sha256": hashlib.sha256(
                license_text.encode("utf-8")
            ).hexdigest()
            if license_text
            else None,
            "metadata": metadata_names[0],
            "license_file": license_names[0] if license_names else None,
        }


def default_addon_path():
    explicit = os.environ.get("C94_MMD_TOOLS_PATH")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    binary = Path(bpy.app.binary_path)
    candidates.append(
        binary.parent
        / "portable"
        / "extensions"
        / "blender_org"
        / "mmd_tools"
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _registration_state():
    result = {}
    for name in (BACKEND_EXTENSION_ID, "mmd_tools"):
        try:
            default, state = addon_utils.check(name)
            result[name] = {"default": bool(default), "state": bool(state)}
        except Exception:
            result[name] = {"default": False, "state": False, "error": True}
    return result


def _active_registration_ids(registration_state):
    """Return every known registered add-on id, including legacy mmd_tools."""
    return sorted(
        name for name, row in (registration_state or {}).items()
        if isinstance(row, dict) and row.get("state")
    )


def inspect_backend(addon_path=None, opencc_wheel=None):
    """Inspect the installed backend without importing or enabling it."""

    path = Path(addon_path) if addon_path else default_addon_path()
    base = {
        "schema": "c94.retarget.mmd_backend_inspection.v1",
        "backend_id": BACKEND_ID,
        "extension_id": BACKEND_EXTENSION_ID,
        "verified_version": VERIFIED_BACKEND_VERSION,
        "verified_license": VERIFIED_BACKEND_LICENSE,
        "only_verified_backend": True,
        "self_contained_pmx_interpreter": False,
        "implicit_install_enable": False,
        "registration_state": _registration_state(),
    }
    if path is None:
        return {
            **base,
            "available": False,
            "verified": False,
            "addon_path": None,
            "error": "未指定MMD Tools路径，且未在Blender便携扩展目录发现",
        }
    path = path.resolve()
    manifest_path = path / "blender_manifest.toml"
    if not manifest_path.is_file():
        return {
            **base,
            "available": False,
            "verified": False,
            "addon_path": str(path),
            "error": "缺少blender_manifest.toml",
        }
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    wheel = Path(opencc_wheel) if opencc_wheel else None
    if wheel is None:
        wheels = list((path / "wheels").glob("opencc*.whl"))
        require(len(wheels) == 1, "应恰好发现一个OpenCC wheel，实际：" + str(len(wheels)))
        wheel = wheels[0]
    wheel = wheel.resolve(strict=True)
    files = {}
    missing = []
    for relative in BACKEND_FILES:
        candidate = path / relative
        if candidate.is_file():
            files[relative] = {
                "file": str(candidate),
                "bytes": candidate.stat().st_size,
                "sha256": sha256_file(candidate),
            }
        else:
            missing.append(relative)
    wheel_info = _wheel_metadata(wheel)
    version = manifest.get("version")
    licences = manifest.get("license") or []
    verified = (
        not missing
        and version == VERIFIED_BACKEND_VERSION
        and any("GPL-3.0-or-later" in str(item) for item in licences)
        and wheel_info.get("version") == "0.1.7"
    )
    return {
        **base,
        "available": not missing,
        "verified": bool(verified),
        "addon_path": str(path),
        "manifest": str(manifest_path),
        "version": version,
        "name": manifest.get("name"),
        "maintainer": manifest.get("maintainer"),
        "license": licences,
        "copyright": manifest.get("copyright"),
        "blender_version_min": manifest.get("blender_version_min"),
        "wheels": manifest.get("wheels"),
        "wheel": {
            "file": str(wheel),
            "bytes": wheel.stat().st_size,
            "sha256": sha256_file(wheel),
            **wheel_info,
        },
        "files": files,
        "missing": missing,
        "source_identity_sha256": canonical_hash(
            {"manifest": manifest, "files": files, "wheel": sha256_file(wheel)}
        ),
        "notes": [
            "MMD Tools 4.5.14是本任务当前唯一已验的PMX/VMD导入后端。",
            "本模块不自带PMX/VMD解释器；首次导入必须显式提供安装路径。",
            "inspect_backend只读清单/版本/许可与文件哈希，不导入、不启用、不安装。",
        ],
    }


def capabilities(addon_path=None, opencc_wheel=None):
    backend = inspect_backend(addon_path=addon_path, opencc_wheel=opencc_wheel)
    return {
        "schema": "c94.retarget.mmd_source_capabilities.v1",
        "backend": backend,
        "supported": {
            "pmx_model_import": bool(backend["available"]),
            "vmd_motion_import": bool(backend["available"]),
            "cold_profile_validation_without_backend": True,
            "body_and_complete_finger_mapping": True,
            "existing_morph_channels": True,
            "gaze_bone_channels": True,
            "optional_camera_import_without_active_camera_replace": True,
            "physics_auto_import": False,
            "audio_import": False,
            "texture_download": False,
        },
        "parameters": {
            "scale": SUPPORTED_SCALE,
            "axis": SUPPORTED_AXIS,
            "source_fps": SUPPORTED_SOURCE_FPS,
            "source_fps_global_fallback": False,
        },
        "axis_conversion": {
            "id": SUPPORTED_AXIS,
            "conversion": "MMD(x,y,z) -> Blender(x,z,y), scale applied explicitly",
        },
        "support_scope": "source_body_plus_existing_morph_gaze_channels",
        "not_claimed": [
            "自含PMX/VMD解释器",
            "完整MMD播放器逐帧一致",
            "自动修复缺失morph或新建表情曲线",
            "物理/贴图/音乐/音频自动导入",
            "任意Action/Slot在无显式fps记录时自动推断",
            "艺术质量或目标变形质量通过",
        ],
    }


def load_backend(addon_path=None, opencc_wheel=None, runtime_dir=None,
                 allow_existing_registration=False):
    """Register the installed add-on for this process only."""

    inspection = inspect_backend(addon_path=addon_path, opencc_wheel=opencc_wheel)
    require(inspection["available"], "MMD Tools不可用：" + str(inspection.get("error")))
    require(inspection["verified"], "仅接受已验MMD Tools 4.5.14/GPL-3.0-or-later")
    registration_state = inspection.get("registration_state", {})
    active_ids = _active_registration_ids(registration_state)
    if active_ids and not allow_existing_registration:
        raise ValueError(
            "当前进程已注册MMD Tools（" + ", ".join(active_ids) + "）；"
            "源导入要求在独立factory后台进程运行，拒绝重复注册或修改现有注册状态"
        )
    runtime_dir = Path(runtime_dir or (Path(bpy.app.tempdir) / "c94-mmd-runtime"))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _safe_extract_wheel(inspection["wheel"]["file"], runtime_dir)
    addon_parent = str(Path(inspection["addon_path"]).parent)
    for entry in (str(runtime_dir), addon_parent):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    existing = sys.modules.get("mmd_tools")
    if existing is not None:
        existing_file = Path(getattr(existing, "__file__", "")).resolve()
        require(
            existing_file.is_relative_to(Path(inspection["addon_path"]).resolve()),
            "mmd_tools已从其他路径导入，拒绝混用后端：" + str(existing_file),
        )
    module = importlib.import_module("mmd_tools")
    module_file = Path(module.__file__).resolve()
    require(
        module_file.is_relative_to(Path(inspection["addon_path"]).resolve()),
        "导入的mmd_tools路径与检查结果不一致",
    )
    registered_by_us = False
    if not active_ids and not getattr(module, "_c94_source_backend_registered", False):
        module.register()
        module._c94_source_backend_registered = True
        registered_by_us = True
    return {
        "module": module,
        "module_file": str(module_file),
        "version": getattr(module, "MMD_TOOLS_VERSION", None),
        "inspection": inspection,
        "runtime_dir": str(runtime_dir),
        "runtime_only_registration": not bool(active_ids),
        "registered_by_us": registered_by_us,
        "existing_registration_ids": active_ids,
        "preferences_saved": False,
    }


def _parse_vmd(module, path):
    vmd = importlib.import_module("mmd_tools.core.vmd")
    motion = vmd.File()
    motion.load(filepath=str(path))
    bone_counts = {
        name: len(items) for name, items in (motion.boneAnimation or {}).items()
    }
    morph_counts = {
        name: len(items) for name, items in (motion.shapeKeyAnimation or {}).items()
    }
    ik_states = {}
    for frame in motion.propertyAnimation or []:
        for name, state in frame.ik_states:
            ik_states.setdefault(name, []).append(
                {"frame": int(frame.frame_number), "enabled": bool(state)}
            )
    frame_values = []
    for values in (motion.boneAnimation or {}).values():
        frame_values.extend(int(item.frame_number) for item in values)
    for values in (motion.shapeKeyAnimation or {}).values():
        frame_values.extend(int(item.frame_number) for item in values)
    for key in motion.cameraAnimation or []:
        frame_values.append(int(key.frame_number))
    for key in motion.lightAnimation or []:
        frame_values.append(int(key.frame_number))
    for key in motion.propertyAnimation or []:
        frame_values.append(int(key.frame_number))
    return {
        "header_model_name": motion.header.model_name if motion.header else None,
        "bone_key_counts": dict(sorted(bone_counts.items())),
        "morph_key_counts": dict(sorted(morph_counts.items())),
        "bone_channels": sorted(bone_counts),
        "morph_channels": sorted(morph_counts),
        "camera_keys": len(motion.cameraAnimation or []),
        "light_keys": len(motion.lightAnimation or []),
        "property_keys": len(motion.propertyAnimation or []),
        "ik_states": dict(sorted(ik_states.items())),
        "frame_range": [min(frame_values), max(frame_values)] if frame_values else None,
        "bone_keys": sum(bone_counts.values()),
        "morph_keys": sum(morph_counts.values()),
    }


def _parse_pmx(module, path):
    pmx = importlib.import_module("mmd_tools.core.pmx")
    model = pmx.load(str(path))
    bones = []
    for index, bone in enumerate(model.bones):
        parent = getattr(bone, "parent", -1)
        display = getattr(bone, "displayConnection", -1)
        bones.append(
            {
                "index": index,
                "name_j": bone.name,
                "name_j_nfkc": nfkc(bone.name),
                "name_e": bone.name_e,
                "parent": int(parent),
                "parent_name": model.bones[parent].name
                if isinstance(parent, int) and 0 <= parent < len(model.bones)
                else None,
                "location": _optional_list(getattr(bone, "location", None)),
                "transform_order": int(getattr(bone, "transform_order", 0)),
                "is_ik": bool(getattr(bone, "isIK", False)),
                "ik_target": int(getattr(bone, "target", -1))
                if getattr(bone, "isIK", False)
                else None,
                "ik_links": [
                    {
                        "target": int(link.target),
                        "minimum_angle": _optional_list(
                            getattr(link, "minimumAngle", None)
                        ),
                        "maximum_angle": _optional_list(
                            getattr(link, "maximumAngle", None)
                        ),
                    }
                    for link in getattr(bone, "ik_links", [])
                ],
                "additional_transform": _optional_list(
                    getattr(bone, "additionalTransform", None)
                ),
                "has_additional_rotate": bool(
                    getattr(bone, "hasAdditionalRotate", False)
                ),
                "has_additional_location": bool(
                    getattr(bone, "hasAdditionalLocation", False)
                ),
                "fixed_axis": _optional_list(getattr(bone, "axis", None)),
                "local_axis_x": list(bone.localCoordinate.x_axis)
                if getattr(bone, "localCoordinate", None) is not None
                else None,
                "local_axis_z": list(bone.localCoordinate.z_axis)
                if getattr(bone, "localCoordinate", None) is not None
                else None,
                "display_connection": display
                if isinstance(display, int) or display is None
                else list(display),
                "is_rotatable": bool(getattr(bone, "isRotatable", True)),
                "is_movable": bool(getattr(bone, "isMovable", True)),
                "visible": bool(getattr(bone, "visible", True)),
                "is_controllable": bool(getattr(bone, "isControllable", True)),
                "trans_after_physics": bool(getattr(bone, "transAfterPhis", False)),
            }
        )
    morphs = []
    for index, morph in enumerate(model.morphs):
        morphs.append(
            {
                "index": index,
                "name": morph.name,
                "name_e": morph.name_e,
                "category": int(getattr(morph, "category", -1)),
                "class": type(morph).__name__,
            }
        )
    return {
        "name": model.name,
        "name_e": model.name_e,
        "comment_sha256": hashlib.sha256(
            (model.comment or "").encode("utf-8")
        ).hexdigest(),
        "comment_e_sha256": hashlib.sha256(
            (model.comment_e or "").encode("utf-8")
        ).hexdigest(),
        "vertices": len(model.vertices),
        "faces": len(model.faces) if hasattr(model, "faces") else None,
        "textures": [texture.path for texture in model.textures],
        "materials": len(model.materials),
        "bones": bones,
        "morphs": morphs,
    }


def _action_fcurves(action):
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag:
                    for fcurve in bag.fcurves:
                        if fcurve.keyframe_points:
                            rows.append((slot, fcurve))
    return rows


def action_signature(action):
    if action is None:
        return None
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag:
                    rows.append(
                        {
                            "slot": slot.identifier,
                            "curves": [
                                [
                                    fcurve.data_path,
                                    fcurve.array_index,
                                    [
                                        [
                                            float(key.co[0]),
                                            float(key.co[1]),
                                            key.interpolation,
                                        ]
                                        for key in fcurve.keyframe_points
                                    ],
                                ]
                                for fcurve in bag.fcurves
                            ],
                        }
                    )
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _slot_record(slot):
    return {
        "identifier": slot.identifier,
        "target_id_type": slot.target_id_type,
        "name_display": getattr(slot, "name_display", None),
    }


def _action_record(action):
    if action is None:
        return None
    fcurves = _action_fcurves(action)
    keys = [key for _slot, fcurve in fcurves for key in fcurve.keyframe_points]
    return {
        "name": action.name,
        "signature": action_signature(action),
        "slots": [_slot_record(slot) for slot in action.slots],
        "users": int(action.users),
        "fake_user": bool(action.use_fake_user),
        "fcurve_count": len(fcurves),
        "key_count": len(keys),
        "range": [min(key.co[0] for key in keys), max(key.co[0] for key in keys)]
        if keys
        else None,
        "fcurves": [
            {
                "slot": slot.identifier,
                "data_path": fcurve.data_path,
                "array_index": fcurve.array_index,
                "keys": len(fcurve.keyframe_points),
                "range": [
                    float(fcurve.keyframe_points[0].co[0]),
                    float(fcurve.keyframe_points[-1].co[0]),
                ]
                if fcurve.keyframe_points
                else None,
            }
            for slot, fcurve in fcurves
        ],
    }


def _bone_metadata(rig):
    rows = []
    for pbone in rig.pose.bones:
        data_bone = pbone.bone
        metadata = getattr(pbone, "mmd_bone", None)
        available = metadata is not None
        name_j = metadata.name_j if available else ""
        row = {
            "blender_name": pbone.name,
            "metadata_available": available,
            "name_j_raw": name_j,
            "name_j_nfkc": nfkc(name_j),
            "name_e": metadata.name_e if available else "",
            "bone_id": int(metadata.bone_id) if available else None,
            "is_tip": bool(metadata.is_tip) if available else bool(
                getattr(data_bone, "use_connect", False)
            ),
            "transform_order": int(metadata.transform_order) if available else None,
            "additional_transform_bone": metadata.additional_transform_bone
            if available
            else None,
            "additional_transform_bone_id": int(
                metadata.additional_transform_bone_id
            )
            if available
            else None,
            "additional_transform_influence": float(
                metadata.additional_transform_influence
            )
            if available
            else None,
            "has_additional_rotation": bool(metadata.has_additional_rotation)
            if available
            else None,
            "has_additional_location": bool(metadata.has_additional_location)
            if available
            else None,
            "enabled_fixed_axis": bool(metadata.enabled_fixed_axis)
            if available
            else None,
            "fixed_axis": _optional_list(metadata.fixed_axis) if available else None,
            "local_axis_x": _optional_list(metadata.local_axis_x)
            if available
            else None,
            "local_axis_z": _optional_list(metadata.local_axis_z)
            if available
            else None,
            "ik_toggle_initial": bool(getattr(pbone, "mmd_ik_toggle", True)),
            "parent": pbone.parent.name if pbone.parent else None,
            "parent_bone_id": int(pbone.parent.mmd_bone.bone_id)
            if pbone.parent is not None and hasattr(pbone.parent, "mmd_bone")
            else None,
            "use_connect": bool(data_bone.use_connect),
            "use_deform": bool(data_bone.use_deform),
            "inherit_rotation": bool(data_bone.use_inherit_rotation),
            "inherit_scale": data_bone.inherit_scale,
            "matrix_local": matrix_list(data_bone.matrix_local),
            "head_local": vector_list(data_bone.head_local),
            "tail_local": vector_list(data_bone.tail_local),
            "length": float(data_bone.length),
            "constraints": [
                {
                    "name": constraint.name,
                    "type": constraint.type,
                    "target": constraint.target.name
                    if getattr(constraint, "target", None)
                    else None,
                    "subtarget": getattr(constraint, "subtarget", None),
                    "owner_space": getattr(constraint, "owner_space", None),
                    "target_space": getattr(constraint, "target_space", None),
                    "chain_count": getattr(constraint, "chain_count", None),
                    "mute": bool(constraint.mute),
                    "influence": float(constraint.influence),
                }
                for constraint in pbone.constraints
            ],
        }
        rows.append(row)
    return rows


def _nfkc_duplicates(bones):
    by_name = {}
    for row in bones:
        normalized = row["name_j_nfkc"]
        if normalized:
            by_name.setdefault(normalized, []).append(row["blender_name"])
    return {
        name: values for name, values in sorted(by_name.items()) if len(values) > 1
    }


def rest_signature(rig):
    rows = []
    for bone in sorted(rig.data.bones, key=lambda item: item.name):
        rows.append(
            {
                "name": bone.name,
                "matrix_local": matrix_list(bone.matrix_local),
                "head_local": list(bone.head_local),
                "tail_local": list(bone.tail_local),
                "parent": bone.parent.name if bone.parent else None,
                "connected": bone.use_connect,
                "deform": bone.use_deform,
                "inherit_rotation": bone.use_inherit_rotation,
                "inherit_scale": bone.inherit_scale,
            }
        )
    return canonical_hash(rows)


def parent_signature(rig):
    rows = [
        {"name": bone.name, "parent": bone.parent.name if bone.parent else None}
        for bone in sorted(rig.data.bones, key=lambda item: item.name)
    ]
    return canonical_hash(rows)


def constraint_signature(rig):
    rows = []
    for pbone in sorted(rig.pose.bones, key=lambda item: item.name):
        for constraint in pbone.constraints:
            rows.append(
                {
                    "bone": pbone.name,
                    "name": constraint.name,
                    "type": constraint.type,
                    "target": constraint.target.name
                    if getattr(constraint, "target", None)
                    else None,
                    "subtarget": getattr(constraint, "subtarget", None),
                    "owner_space": getattr(constraint, "owner_space", None),
                    "target_space": getattr(constraint, "target_space", None),
                    "chain_count": getattr(constraint, "chain_count", None),
                    "mute": bool(constraint.mute),
                    "influence": float(constraint.influence),
                }
            )
    return canonical_hash(rows)


def constraint_structure_signature(rig):
    """Immutable constraint topology; influence/mute are raw author inputs."""

    rows = []
    for pbone in sorted(rig.pose.bones, key=lambda item: item.name):
        for constraint in pbone.constraints:
            rows.append(
                {
                    "bone": pbone.name,
                    "name": constraint.name,
                    "type": constraint.type,
                    "target": constraint.target.name
                    if getattr(constraint, "target", None)
                    else None,
                    "subtarget": getattr(constraint, "subtarget", None),
                    "owner_space": getattr(constraint, "owner_space", None),
                    "target_space": getattr(constraint, "target_space", None),
                    "chain_count": getattr(constraint, "chain_count", None),
                }
            )
    return canonical_hash(
        sorted(
            rows,
            key=lambda row: (
                row["bone"],
                row["name"],
                row["type"],
                row["target"] or "",
                row["subtarget"] or "",
                row["chain_count"] if row["chain_count"] is not None else -1,
            ),
        )
    )


def _constraint_structure_signature_from_rows(rows):
    normalized = [
            {
                "bone": row["bone"],
                "name": row["name"],
                "type": row["type"],
                "target": row.get("target"),
                "subtarget": row.get("subtarget"),
                "owner_space": row.get("owner_space"),
                "target_space": row.get("target_space"),
                "chain_count": row.get("chain_count"),
            }
            for row in rows
        ]
    return canonical_hash(
        sorted(
            normalized,
            key=lambda row: (
                row["bone"],
                row["name"],
                row["type"],
                row["target"] or "",
                row["subtarget"] or "",
                row["chain_count"] if row["chain_count"] is not None else -1,
            ),
        )
    )


def _mesh_signature(mesh):
    shape_keys = (
        [key.name for key in mesh.data.shape_keys.key_blocks]
        if mesh.data.shape_keys
        else []
    )
    return canonical_hash(
        {
            "vertices": len(mesh.data.vertices),
            "polygons": len(mesh.data.polygons),
            "vertex_groups": sorted(group.name for group in mesh.vertex_groups),
            "materials": [
                slot.material.name if slot.material else None
                for slot in mesh.material_slots
            ],
            "shape_keys": shape_keys,
            "armature_modifiers": [
                [modifier.name, modifier.object.name if modifier.object else None]
                for modifier in mesh.modifiers
                if modifier.type == "ARMATURE"
            ],
        }
    )


def _weight_signature(mesh):
    rows = []
    for vertex in mesh.data.vertices:
        for group in vertex.groups:
            if group.weight > 0:
                rows.append(
                    [vertex.index, group.group, round(float(group.weight), 9)]
                )
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def object_signature(root, rig, meshes):
    rows = [
        {
            "role": "ROOT",
            "name": root.name,
            "type": root.type,
            "data": root.data.name if root.data else None,
        },
        {
            "role": "ARMATURE",
            "name": rig.name,
            "type": rig.type,
            "data": rig.data.name,
        },
        *[
            {
                "role": "MESH",
                "name": mesh.name,
                "type": mesh.type,
                "data": mesh.data.name,
                "mesh_signature": _mesh_signature(mesh),
            }
            for mesh in sorted(meshes, key=lambda item: item.name)
        ],
    ]
    return canonical_hash(rows)


def _driver_records(id_data):
    animation_data = getattr(id_data, "animation_data", None)
    if animation_data is None:
        return []
    rows = []
    for driver in animation_data.drivers:
        rows.append(
            {
                "id": id_data.name,
                "data_path": driver.data_path,
                "array_index": driver.array_index,
                "type": driver.driver.type,
                "expression": driver.driver.expression,
                "variables": [
                    {
                        "name": variable.name,
                        "type": variable.type,
                        "targets": [
                            {
                                "id": target.id.name if target.id else None,
                                "data_path": target.data_path,
                                "bone_target": target.bone_target,
                                "transform_type": target.transform_type,
                                "transform_space": target.transform_space,
                            }
                            for target in variable.targets
                        ],
                    }
                    for variable in driver.driver.variables
                ],
            }
        )
    return rows


def _shape_keys(mesh):
    if not mesh.data.shape_keys:
        return []
    return [
        {
            "index": index,
            "name": key.name,
            "relative_key": key.relative_key.name if key.relative_key else None,
            "value": float(key.value),
            "slider_min": float(key.slider_min),
            "slider_max": float(key.slider_max),
            "vertex_group": key.vertex_group,
        }
        for index, key in enumerate(mesh.data.shape_keys.key_blocks)
    ]


def _mesh_weight_sums(mesh):
    sums = {}
    for vertex in mesh.data.vertices:
        for group in vertex.groups:
            if group.weight > 0:
                name = mesh.vertex_groups[group.group].name
                sums[name] = sums.get(name, 0.0) + float(group.weight)
    return sums


def _weight_records(meshes):
    result = {}
    for mesh in meshes:
        result[mesh.name] = {
            name: float(value)
            for name, value in sorted(_mesh_weight_sums(mesh).items())
        }
    return result


def _merged_weights(weights):
    merged = {}
    for sums in weights.values():
        for name, value in sums.items():
            merged[name] = merged.get(name, 0.0) + float(value)
    return merged


def _finger_records(rig, mapping, mapping_profile):
    rows = []
    for side in FINGER_SIDES:
        for finger in FINGER_WORDS:
            key = finger + "." + side
            complete = key in mapping["fingers"]["complete"]
            names = []
            for position in range(1, 5):
                semantic = finger + str(position) + "." + side
                if semantic in mapping["bones"]:
                    names.append(mapping["bones"][semantic])
            parent_links = []
            true_end = False
            for parent_name, child_name in zip(names, names[1:]):
                child = rig.data.bones.get(child_name)
                parent_links.append(
                    {
                        "parent": parent_name,
                        "child": child_name,
                        "actual_parent": child.parent.name
                        if child is not None and child.parent
                        else None,
                        "direct": bool(
                            child is not None
                            and child.parent is not None
                            and child.parent.name == parent_name
                        ),
                    }
                )
            if len(names) == 4:
                true_end = all(link["direct"] for link in parent_links)
            terminal_semantic = finger + "4." + side
            terminal_record = mapping_profile.get("semantics", {}).get(
                terminal_semantic
            )
            rows.append(
                {
                    "finger": finger,
                    "side": side,
                    "complete": bool(complete),
                    "chain": names,
                    "parent_links": parent_links,
                    "three_section_plus_true_end": bool(true_end),
                    "terminal_semantic": terminal_semantic,
                    "terminal_real_bone": bool(
                        terminal_record
                        and not terminal_record.get("owned_terminal_helper")
                    ),
                    "terminal_positive_weight": bool(
                        terminal_record
                        and terminal_record.get("positive_weight")
                    ),
                    "terminal_weight_sum": float(
                        terminal_record.get("positive_weight_sum", 0.0)
                    )
                    if terminal_record
                    else 0.0,
                }
            )
    return rows


def _foot_records(weights):
    merged = _merged_weights(weights)
    rows = {name: float(merged.get(name, 0.0)) for name in FOOT_CHAIN_NAMES}
    d_positive = [
        name for name in FOOT_CHAIN_NAMES if name.endswith("D") and rows[name] > 0
    ]
    non_d_positive = [
        name
        for name in ("左足", "左ひざ", "左足首", "右足", "右ひざ", "右足首")
        if rows[name] > 0
    ]
    return {
        "weight_sums": rows,
        "d_positive_weights": d_positive,
        "non_d_positive": non_d_positive,
        "ordinary_chain_d_zero": not d_positive,
    }


def _root_metadata(root):
    metadata = getattr(root, "mmd_root", None)
    if metadata is None:
        return {"available": False}
    morphs = {}
    for attribute in (
        "vertex_morphs",
        "uv_morphs",
        "bone_morphs",
        "material_morphs",
        "group_morphs",
    ):
        values = getattr(metadata, attribute, []) or []
        morphs[attribute] = [
            {"name": item.name, "name_e": getattr(item, "name_e", "")}
            for item in values
        ]
    return {
        "available": True,
        "name": getattr(metadata, "name", None),
        "name_e": getattr(metadata, "name_e", None),
        "show_meshes": bool(getattr(metadata, "show_meshes", True)),
        "morphs": morphs,
        "morph_counts": {
            attribute: len(values) for attribute, values in morphs.items()
        },
    }


def _new_objects(before):
    return [obj for obj in bpy.data.objects if obj not in before]


def _find_root(rig, new_objects):
    candidates = [
        obj
        for obj in new_objects
        if getattr(obj, "mmd_type", None) == "ROOT"
    ]
    if len(candidates) == 1:
        return candidates[0]
    root = rig
    while root.parent is not None:
        root = root.parent
    return root


def _select_only(root):
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    root.select_set(True)
    bpy.context.view_layer.objects.active = root


def _import_optional_camera(module, root, vmd_path, source_scene, scale,
                            inventory, fps):
    """Import camera channels without replacing the Scene active camera."""

    if not inventory["camera_keys"]:
        return {
            "requested": True,
            "imported": False,
            "reason": "VMD没有camera通道",
            "active_camera_replaced": False,
            "camera_object": None,
            "actions": [],
        }
    camera_data = bpy.data.cameras.new("C94_MMD_SOURCE_CAMERA")
    camera = bpy.data.objects.new("C94_MMD_SOURCE_CAMERA", camera_data)
    source_scene.collection.objects.link(camera)
    old_camera = source_scene.camera
    scene_objects_before = set(source_scene.objects)
    try:
        _select_only(camera)
        status = bpy.ops.mmd_tools.import_vmd(
            filepath=str(vmd_path),
            scale=float(scale),
            margin=0,
            use_pose_mode=False,
            update_scene_settings=False,
            create_new_action=True,
            use_nla=False,
            save_log=False,
            bone_mapper="PMX",
            detect_camera_changes=True,
            detect_light_changes=False,
        )
        require(status == {"FINISHED"}, "可选相机VMD导入未完成")
        new_objects = [
            obj for obj in source_scene.objects if obj not in scene_objects_before
        ]
        bound_actions = []
        for owner in (camera, camera.data, *new_objects):
            animation_data = getattr(owner, "animation_data", None)
            if animation_data is not None and animation_data.action is not None:
                bound_actions.append(animation_data.action)
        bound_actions = list(
            {action.name: action for action in bound_actions}.values()
        )
        for action in bound_actions:
            action.use_fake_user = True
            action[TIME_KEY] = json.dumps(
                {
                    "fps": float(fps),
                    "slots": [slot.identifier for slot in action.slots],
                    "clip_id": "camera",
                    "source": "MMD_VMD_CAMERA_EXPLICIT",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return {
            "requested": True,
            "imported": True,
            "reason": None,
            "active_camera_replaced": False,
            "camera_object": camera.name,
            "new_objects": [
                {"name": obj.name, "type": obj.type} for obj in new_objects
            ],
            "actions": [_action_record(action) for action in bound_actions],
            "action_names": sorted(action.name for action in bound_actions),
        }
    finally:
        source_scene.camera = old_camera
        _select_only(root)


def _bind_action(owner, action, slot_identifier=None):
    if owner.animation_data is None:
        owner.animation_data_create()
    owner.animation_data.action = action
    if slot_identifier is not None:
        slot = next(
            (slot for slot in action.slots if slot.identifier == slot_identifier),
            None,
        )
        require(slot is not None, "Action缺少请求Slot：" + slot_identifier)
        owner.animation_data.action_slot = slot
    elif action is not None and action.slots:
        owner.animation_data.action_slot = action.slots[0]
    owner.animation_data.action_blend_type = "REPLACE"
    owner.animation_data.action_influence = 1.0
    owner.animation_data.action_extrapolation = "HOLD"
    owner.animation_data.use_nla = False


def _reset_pose_and_morphs(rig, meshes):
    for pbone in rig.pose.bones:
        pbone.location = (0.0, 0.0, 0.0)
        pbone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pbone.rotation_euler = (0.0, 0.0, 0.0)
        pbone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        pbone.scale = (1.0, 1.0, 1.0)
    for mesh in meshes:
        if mesh.data.shape_keys is None:
            continue
        for key in mesh.data.shape_keys.key_blocks:
            if key.name != "Basis":
                key.value = 0.0


def _bind_clip(rig, meshes, clip):
    _reset_pose_and_morphs(rig, meshes)
    for pbone in rig.pose.bones:
        state = clip.get("ik_toggle_state", {}).get(pbone.name)
        if state is not None and hasattr(pbone, "mmd_ik_toggle"):
            pbone.mmd_ik_toggle = bool(state)
    armature = clip.get("armature_action")
    if armature and armature.get("name"):
        action = bpy.data.actions.get(armature["name"])
        require(action is not None, "源缺少Armature Action：" + armature["name"])
        _bind_action(rig, action, armature.get("slot"))
    for record in clip.get("shape_key_actions", []):
        mesh = bpy.data.objects.get(record["mesh"])
        require(mesh is not None, "源缺少morph目标Mesh：" + record["mesh"])
        require(mesh.data.shape_keys is not None, "源Mesh缺少Shape Keys：" + mesh.name)
        action = bpy.data.actions.get(record["action"]["name"])
        require(action is not None, "源缺少morph Action：" + record["action"]["name"])
        _bind_action(mesh.data.shape_keys, action, record["action"].get("slot"))
    for key, value in clip.get("constraint_influences", {}).items():
        bone_name, separator, constraint_name = key.partition("::")
        if not separator:
            continue
        pbone = rig.pose.bones.get(bone_name)
        if pbone is None:
            continue
        constraint = pbone.constraints.get(constraint_name)
        if constraint is not None:
            constraint.influence = float(value["influence"])
            constraint.mute = bool(value["mute"])


def _snapshot_bindings(rig, meshes, root=None):
    def one(owner):
        animation_data = getattr(owner, "animation_data", None)
        if animation_data is None:
            return {
                "exists": False,
                "action": None,
                "slot": None,
                "blend_type": None,
                "influence": None,
                "extrapolation": None,
                "use_nla": None,
            }
        return {
            "exists": True,
            "action": animation_data.action.name if animation_data.action else None,
            "slot": animation_data.action_slot.identifier
            if animation_data.action_slot
            else None,
            "blend_type": animation_data.action_blend_type,
            "influence": float(animation_data.action_influence),
            "extrapolation": animation_data.action_extrapolation,
            "use_nla": bool(animation_data.use_nla),
        }

    return {
        "root": one(root) if root is not None else None,
        "rig": one(rig),
        "shape_keys": {
            mesh.name: one(mesh.data.shape_keys)
            for mesh in meshes
            if mesh.data.shape_keys is not None
        },
        "scene": bpy.context.scene.name,
        "frame": float(
            bpy.context.scene.frame_current + bpy.context.scene.frame_subframe
        ),
        "fps": float(
            bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        ),
        "fps_int": int(bpy.context.scene.render.fps),
        "fps_base": float(bpy.context.scene.render.fps_base),
        "frame_start": int(bpy.context.scene.frame_start),
        "frame_end": int(bpy.context.scene.frame_end),
        "camera": bpy.context.scene.camera.name
        if bpy.context.scene.camera
        else None,
        "ik_toggle_state": {
            pbone.name: bool(pbone.mmd_ik_toggle)
            for pbone in rig.pose.bones
            if hasattr(pbone, "mmd_ik_toggle")
        },
        "constraint_influences": {
            pbone.name + "::" + constraint.name: {
                "influence": float(constraint.influence),
                "mute": bool(constraint.mute),
            }
            for pbone in rig.pose.bones
            for constraint in pbone.constraints
        },
        "rotation_modes": {
            pbone.name: pbone.rotation_mode for pbone in rig.pose.bones
        },
        "pose_channels": {
            pbone.name: pose._channels(pbone) for pbone in rig.pose.bones
        },
        "pose_basis": {
            pbone.name: matrix_list(pbone.matrix_basis)
            for pbone in rig.pose.bones
        },
        "shape_values": {
            mesh.name: {
                key.name: float(key.value)
                for key in mesh.data.shape_keys.key_blocks
            }
            for mesh in meshes
            if mesh.data.shape_keys is not None
        },
    }


def _restore_one(owner, record):
    # ``exists`` was added after the first persisted selections.  A missing
    # value is a legacy record whose AnimData presence is unknown; never
    # delete AnimData for such a record.
    exists = record.get("exists")
    animation_data = getattr(owner, "animation_data", None)
    if animation_data is None:
        if exists is False:
            return
        if exists is None and record.get("action") is None:
            return
        animation_data = owner.animation_data_create()
    if record["action"] is None:
        animation_data.action = None
        if record.get("blend_type") is not None:
            animation_data.action_blend_type = record["blend_type"]
        if record.get("influence") is not None:
            animation_data.action_influence = record["influence"]
        if record.get("extrapolation") is not None:
            animation_data.action_extrapolation = record["extrapolation"]
        if record.get("use_nla") is not None:
            animation_data.use_nla = record["use_nla"]
        if exists is False and not animation_data.drivers and not animation_data.nla_tracks:
            owner.animation_data_clear()
        return
    action = bpy.data.actions.get(record["action"])
    require(action is not None, "恢复Action不存在：" + record["action"])
    _bind_action(owner, action, record["slot"])
    if animation_data is not None:
        if record["blend_type"] is not None:
            animation_data.action_blend_type = record["blend_type"]
        if record["influence"] is not None:
            animation_data.action_influence = record["influence"]
        if record["extrapolation"] is not None:
            animation_data.action_extrapolation = record["extrapolation"]
        if record["use_nla"] is not None:
            animation_data.use_nla = record["use_nla"]


def _restore_scene_time(snapshot):
    scene = bpy.data.scenes.get(snapshot["scene"])
    if scene is None:
        scene = bpy.context.scene
    window = bpy.context.window
    if window is not None and window.scene != scene:
        window.scene = scene
    scene.render.fps = snapshot["fps_int"]
    scene.render.fps_base = snapshot["fps_base"]
    scene.frame_start = snapshot["frame_start"]
    scene.frame_end = snapshot["frame_end"]
    if snapshot["camera"]:
        camera = bpy.data.objects.get(snapshot["camera"])
        if camera is not None:
            scene.camera = camera
    else:
        scene.camera = None
    _set_scene_frame(scene, snapshot["frame"])
    return scene


def _restore_bindings(snapshot, rig, meshes, root=None, *, set_scene_time=True):
    scene = bpy.data.scenes.get(snapshot["scene"])
    if scene is None:
        scene = bpy.context.scene
    window = bpy.context.window
    if window is not None and window.scene != scene:
        window.scene = scene
    if set_scene_time:
        _restore_scene_time(snapshot)
    if root is not None and snapshot.get("root") is not None:
        _restore_one(root, snapshot["root"])
    _restore_one(rig, snapshot["rig"])
    for mesh in meshes:
        if mesh.name in snapshot["shape_keys"]:
            _restore_one(mesh.data.shape_keys, snapshot["shape_keys"][mesh.name])
    # Action assignment can queue evaluation even for the same Action/Slot.
    # Flush it before writing raw author inputs; do not update afterwards.
    bpy.context.view_layer.update()
    pose_channels = snapshot.get("pose_channels", {})
    if pose_channels:
        for pbone in rig.pose.bones:
            values = pose_channels.get(pbone.name)
            if values is None:
                continue
            pbone.rotation_mode = values["rotation_mode"]
            for field in pose._CHANNELS:
                if tuple(getattr(pbone, field)) != values[field]:
                    setattr(pbone, field, values[field])
    else:
        # Legacy snapshots only carried a decomposed matrix; keep compatibility
        # but do not use this path for newly captured raw author inputs.
        for pbone in rig.pose.bones:
            mode = snapshot.get("rotation_modes", {}).get(pbone.name)
            if mode is not None:
                pbone.rotation_mode = mode
            basis = snapshot.get("pose_basis", {}).get(pbone.name)
            if basis is not None:
                pbone.matrix_basis = Matrix(basis)
    for mesh in meshes:
        values = snapshot.get("shape_values", {}).get(mesh.name, {})
        if mesh.data.shape_keys is None:
            continue
        for key in mesh.data.shape_keys.key_blocks:
            if key.name in values:
                key.value = float(values[key.name])
    for pbone in rig.pose.bones:
        state = snapshot.get("ik_toggle_state", {}).get(pbone.name)
        if state is not None and hasattr(pbone, "mmd_ik_toggle"):
            pbone.mmd_ik_toggle = bool(state)
    for key, value in snapshot.get("constraint_influences", {}).items():
        bone_name, separator, constraint_name = key.partition("::")
        if not separator:
            continue
        pbone = rig.pose.bones.get(bone_name)
        if pbone is None:
            continue
        constraint = pbone.constraints.get(constraint_name)
        if constraint is not None:
            constraint.influence = float(value["influence"])
            constraint.mute = bool(value["mute"])


def _scene_state(scene):
    return {
        "name": scene.name,
        "fps": float(scene.render.fps / scene.render.fps_base),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "frame_current": int(scene.frame_current),
        "frame_subframe": float(scene.frame_subframe),
        "camera": scene.camera.name if scene.camera else None,
        "objects": sorted(obj.name for obj in scene.objects),
    }


def _action_bones(action):
    found = set()
    if action is None:
        return found
    for _slot, fcurve in _action_fcurves(action):
        prefix = 'pose.bones["'
        if fcurve.data_path.startswith(prefix):
            tail = fcurve.data_path[len(prefix):]
            name, separator, _rest = tail.partition('"]')
            if separator:
                found.add(name)
    return found


def _action_shape_keys(action):
    found = set()
    if action is None:
        return found
    for _slot, fcurve in _action_fcurves(action):
        prefix = 'key_blocks["'
        if fcurve.data_path.startswith(prefix):
            tail = fcurve.data_path[len(prefix):]
            name, separator, _rest = tail.partition('"]')
            if separator:
                found.add(name)
    return found


def _action_ik_toggles(action):
    found = set()
    if action is None:
        return found
    for _slot, fcurve in _action_fcurves(action):
        if fcurve.data_path.endswith(".mmd_ik_toggle"):
            prefix = 'pose.bones["'
            if fcurve.data_path.startswith(prefix):
                tail = fcurve.data_path[len(prefix):]
                name, separator, _rest = tail.partition('"]')
                if separator:
                    found.add(name)
    return found


def _frame_samples(frame_range, count=5):
    if not frame_range:
        return []
    start, end = float(frame_range[0]), float(frame_range[1])
    if abs(end - start) < 1e-9:
        return [start]
    values = {start, end}
    for index in range(1, count - 1):
        values.add(round(start + (end - start) * index / (count - 1), 6))
    return sorted(values)


def _set_scene_frame(scene, frame):
    frame = float(frame)
    base = math.floor(frame)
    scene.frame_set(base, subframe=frame - base)


def _sample_pose(rig, meshes, mapping, gaze_names, frame_range, frames):
    observations = []
    scene = bpy.context.scene
    old_frame = scene.frame_current
    old_subframe = scene.frame_subframe
    try:
        for frame in frames:
            _set_scene_frame(scene, frame)
            bpy.context.view_layer.update()
            evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
            bones = {
                semantic: vector_list(
                    evaluated.matrix_world
                    @ evaluated.pose.bones[name].matrix.translation
                )
                for semantic, name in mapping["bones"].items()
            }
            gaze = {}
            for name in gaze_names:
                pbone = evaluated.pose.bones.get(name)
                if pbone is not None:
                    gaze[name] = {
                        "translation": vector_list(
                            evaluated.matrix_world @ pbone.matrix.translation
                        ),
                        "matrix": matrix_list(evaluated.matrix_world @ pbone.matrix),
                    }
            mesh_points = {}
            shape_values = {}
            for mesh in meshes:
                evaluated_mesh = mesh.evaluated_get(
                    bpy.context.evaluated_depsgraph_get()
                )
                count = len(evaluated_mesh.data.vertices)
                if count:
                    indices = sorted(
                        set(int(index * (count - 1) / 47) for index in range(48))
                    )
                    mesh_points[mesh.name] = {
                        str(index): vector_list(
                            evaluated_mesh.matrix_world
                            @ evaluated_mesh.data.vertices[index].co
                        )
                        for index in indices
                    }
                if mesh.data.shape_keys:
                    shape_values[mesh.name] = {
                        key.name: float(key.value)
                        for key in mesh.data.shape_keys.key_blocks
                    }
            observations.append(
                {
                    "frame": float(frame),
                    "bones": bones,
                    "gaze": gaze,
                    "meshes": mesh_points,
                    "shape_keys": shape_values,
                }
            )
    finally:
        scene.frame_set(old_frame, subframe=old_subframe)
    return observations


def _variation(observations):
    morph_variation = {}
    for sample in observations:
        for mesh_name, values in sample["shape_keys"].items():
            for name, value in values.items():
                key = mesh_name + "::" + name
                entry = morph_variation.setdefault(key, [value, value])
                entry[0] = min(entry[0], value)
                entry[1] = max(entry[1], value)
    changing = {
        key: value[1] - value[0]
        for key, value in sorted(morph_variation.items())
        if value[1] - value[0] > 1e-7
    }
    gaze_variation = {}
    if observations:
        names = sorted(observations[0]["gaze"])
        for name in names:
            points = [sample["gaze"][name]["translation"] for sample in observations]
            matrices = [sample["gaze"][name]["matrix"] for sample in observations]
            translation_delta = max(
                (Vector(points[i]) - Vector(points[0])).length
                for i in range(len(points))
            )
            matrix_delta = 0.0
            for matrix in matrices[1:]:
                for row in range(4):
                    for column in range(4):
                        matrix_delta = max(
                            matrix_delta,
                            abs(matrix[row][column] - matrices[0][row][column]),
                        )
            gaze_variation[name] = {
                "translation_delta": float(translation_delta),
                "matrix_element_delta": float(matrix_delta),
            }
    body_variation = {}
    if observations:
        for semantic in observations[0]["bones"]:
            points = [sample["bones"][semantic] for sample in observations]
            body_variation[semantic] = max(
                (Vector(points[i]) - Vector(points[0])).length
                for i in range(len(points))
            )
    return {
        "changing_morphs": changing,
        "gaze_variation": gaze_variation,
        "body_translation_variation": body_variation,
    }


def _resolve_slot(action, slot_identifier):
    return next(
        (slot for slot in action.slots if slot.identifier == slot_identifier), None
    )


NATIVE_IK_SCHEMA = "c94.retarget.mmd_native_ik.v1"


def _ik_events(rows):
    """Explicit VMD events; conflicting duplicates are not silently flattened."""
    events = {}
    for row in rows:
        frame = row["frame"]
        require(type(frame) is int and frame >= 0 and type(row["enabled"]) is bool,
                "IK事件必须是非负整数帧和明确Bool")
        value = row["enabled"]
        require(frame not in events or events[frame] == value,
                "同一IK通道同帧存在冲突事件")
        events[frame] = value
    return [{"frame": frame, "enabled": value} for frame, value in sorted(events.items())]


def _has_dynamic_ik(clip):
    for rows in clip.get("inventory", {}).get("ik_states", {}).values():
        events = _ik_events(rows)
        if events and (len({row["enabled"] for row in events}) > 1
                       or events[0]["frame"] > 0):
            return True
    return False


def _ik_curve_payload(action, excluded=()):
    return [
        {"slot": slot.identifier, "path": fc.data_path, "index": fc.array_index,
         "extrapolation": fc.extrapolation, "mute": fc.mute, "lock": fc.lock,
         "keys": [{"co": list(key.co), "left": list(key.handle_left),
                   "right": list(key.handle_right), "interpolation": key.interpolation,
                   "left_type": key.handle_left_type, "right_type": key.handle_right_type,
                   "easing": key.easing} for key in fc.keyframe_points]}
        for slot, fc in _action_fcurves(action)
        if fc.data_path not in excluded
    ]


def _native_ik_ready(clip, action=None):
    """Cold read guard: a dynamic legacy clip must be re-imported."""
    proof = clip.get("native_ik")
    if proof is None:
        require(not _has_dynamic_ik(clip),
                "动态MMD IK片段缺少原生编译证明；请重新导入来源")
        return
    require(proof.get("schema") == NATIVE_IK_SCHEMA, "native IK版本不支持；请重新导入")
    require(proof.get("raw_events") == clip["inventory"]["ik_states"],
            "native IK原始事件与VMD inventory不一致")
    require(not any(row.get("dynamic") for row in proof.get("unmatched_channels", [])),
            "native IK有未匹配动态通道；请重新导入/适配来源")
    require(proof["action"]["name"] == clip["armature_action"]["name"]
            and proof["action"]["slot"] == clip["armature_action"]["slot"]
            and proof["action"]["signature"] == clip["armature_action"]["signature"],
            "native IK Action/Slot/签名证明不符")
    if action is None:
        return
    require(action_signature(action) == proof["action"]["signature"],
            "native IK实际Action已改变")
    actual = {(slot.identifier, fc.data_path, fc.array_index): fc
              for slot, fc in _action_fcurves(action)}
    for channel in proof["channels"]:
        for output in channel["outputs"]:
            fc = actual.get((proof["action"]["slot"], output["path"], 0))
            require(fc is not None and not fc.modifiers and not fc.mute,
                    "native IK原生曲线缺失或被修改")
            require([[float(k.co[0]), float(k.co[1])] for k in fc.keyframe_points]
                    == output["keys"]
                    and all(k.interpolation == "CONSTANT" for k in fc.keyframe_points)
                    and fc.extrapolation == "CONSTANT",
                    "native IK必须保持VMD事件台阶曲线；请重新导入")


def _compile_native_ik(rig, meshes, root, inventory, action, slot_id,
                       initial_defaults, owned_actions):
    """Use installed MMD Tools to discover dependencies, then native curves.

    No MMD parser, IK solver, or upstream callback implementation is copied.
    Only the just-imported Action is writable. The temporary official mapping
    is removed before native channels are created.
    """
    raw_events = inventory["ik_states"]
    require(action is not None and action in owned_actions,
            "IK编译仅允许本次新导入owned Armature Action")
    slot = _resolve_slot(action, slot_id)
    require(slot is not None and slot.target_id_type == "OBJECT", "IK编译需要真实OBJECT Slot")
    proof = {"schema": NATIVE_IK_SCHEMA, "method": "OFFICIAL_PROPERTY_DRIVER_DEPENDENCIES_TO_CONSTANT_INFLUENCE",
             "raw_events": deepcopy(raw_events), "initial_defaults": deepcopy(initial_defaults),
             "channels": [], "unmatched_channels": [], "unrequested_outputs": [],
             "action": {"name": action.name, "slot": slot_id}}
    if not raw_events:
        proof["status"] = "NO_IK_PROPERTY_CHANNELS"
        proof["action"]["signature"] = action_signature(action)
        return proof
    events_by_name = {name: _ik_events(rows) for name, rows in raw_events.items()}
    require(all(events_by_name.values()), "IK属性通道不能是空事件")
    constraints = {c.path_from_id("influence"): c for pb in rig.pose.bones
                   for c in pb.constraints if c.type in {"IK", "LIMIT_ROTATION"}}
    all_influence_paths = {c.path_from_id("influence") for pb in rig.pose.bones for c in pb.constraints}
    rig_drivers = list(rig.animation_data.drivers) if rig.animation_data else []
    require(not any(fc.data_path in all_influence_paths for fc in rig_drivers),
            "IK映射拒绝覆盖已有约束influence driver")
    for mesh in meshes:
        require(not any(fc.data_path in {"hide_viewport", "hide_render"}
                        for fc in (mesh.animation_data.drivers if mesh.animation_data else [])),
                "IK映射拒绝覆盖已有Mesh显隐driver")
    require(not root.mmd_root.use_property_driver,
            "IK映射仅适用本次新源，已有property driver状态不符")
    toggle_paths = {pb.path_from_id("mmd_ik_toggle"): pb.name for pb in rig.pose.bones}
    before = _snapshot_bindings(rig, meshes, root=root)
    dependencies = {}
    try:
        bpy.context.view_layer.update()
        root.mmd_root.use_property_driver = True
        for fc in rig.animation_data.drivers:
            if fc in rig_drivers:
                continue
            require(fc.data_path in constraints and fc.array_index == 0,
                    "官方IK映射产生不可解析原生输出")
            driver = fc.driver
            require(driver.type == "SCRIPTED" and len(driver.variables) == 1,
                    "官方IK映射不是单一SINGLE_PROP依赖")
            var = driver.variables[0]
            require(var.type == "SINGLE_PROP" and len(var.targets) == 1
                    and driver.expression == var.name,
                    "官方IK映射存在复合或未知表达式")
            target = var.targets[0]
            require(target.id == rig and target.data_path in toggle_paths,
                    "官方IK映射指向外部或未知属性")
            dependencies[fc.data_path] = toggle_paths[target.data_path]
    finally:
        if root.mmd_root.use_property_driver:
            root.mmd_root.use_property_driver = False
        _restore_bindings(before, rig, meshes, root=root, set_scene_time=True)
    require(list(rig.animation_data.drivers) == rig_drivers,
            "官方临时IK映射未完整移除，拒绝编译")
    curves = {(sl.identifier, fc.data_path, fc.array_index): fc
              for sl, fc in _action_fcurves(action)}
    requested = {}
    for vmd_name, events in events_by_name.items():
        dynamic = _has_dynamic_ik({"inventory": {"ik_states": {vmd_name: events}}})
        def unmatched(reason):
            require(not dynamic, "未匹配动态IK通道需来源适配：" + vmd_name + "；" + reason)
            proof["unmatched_channels"].append({"vmd_name": vmd_name,
                "dynamic": False, "reason": reason, "events": deepcopy(events),
                "default": None, "treatment": "EXPLICIT_STATIC_PARTIAL_NO_CONSUMER"})
        matches = [pb for pb in rig.pose.bones
                   if pb.mmd_bone.name_j == vmd_name or pb.name == vmd_name]
        if len(matches) != 1:
            unmatched("未唯一匹配真实PMX骨")
            continue
        pb = matches[0]; path = pb.path_from_id("mmd_ik_toggle")
        if (slot_id, path, 0) not in curves:
            unmatched("导入器未生成真实属性曲线")
            continue
        require(pb.name in initial_defaults, "IK初始default缺失：" + pb.name)
        outputs = [p for p, name in dependencies.items() if name == pb.name]
        if not outputs:
            unmatched("官方未产生约束消费映射")
            continue
        require(pb.name not in requested, "多个VMD通道争用同一实际IK属性：" + pb.name)
        requested[pb.name] = vmd_name
        effective = deepcopy(events)
        if effective[0]["frame"] > 0:
            effective.insert(0, {"frame": 0, "enabled": bool(initial_defaults[pb.name])})
        proof["channels"].append({"vmd_name": vmd_name, "bone": pb.name,
            "toggle_path": path, "default": bool(initial_defaults[pb.name]),
            "events": deepcopy(events), "effective_events": effective,
            "outputs": [{"path": p, "constraint_type": constraints[p].type,
                         "keys": [[float(e["frame"]), float(e["enabled"])] for e in effective]}
                        for p in sorted(outputs)]})
    proof["unrequested_outputs"] = [
        {"path": path, "bone": name, "default": bool(initial_defaults[name])}
        for path, name in sorted(dependencies.items()) if name not in requested]
    output_paths = {o["path"] for ch in proof["channels"] for o in ch["outputs"]}
    require(not any(fc.data_path in output_paths for sl, fc in _action_fcurves(action)),
            "本次Action已有目标约束influence曲线，拒绝归属冲突")
    for path in output_paths:
        require(not any(fc.data_path == path for fc in rig_drivers), "原生IK输出有foreign driver")
    toggle_set = {ch["toggle_path"] for ch in proof["channels"]}
    preserved_before = canonical_hash(_ik_curve_payload(action, toggle_set))
    bags = [strip.channelbag(slot) for layer in action.layers for strip in layer.strips
            if strip.channelbag(slot) is not None]
    require(len(bags) == 1, "IK编译仅支持唯一真实OBJECT channelbag")
    bag = bags[0]
    for channel in proof["channels"]:
        for output in [{"path": channel["toggle_path"], "keys": [[float(e["frame"]), float(e["enabled"])] for e in channel["effective_events"]]}, *channel["outputs"]]:
            fc = curves.get((slot_id, output["path"], 0))
            if fc is None:
                fc = bag.fcurves.new(data_path=output["path"], index=0)
            require(not fc.modifiers and not fc.mute, "IK曲线有未知modifier/mute")
            fc.extrapolation = "CONSTANT"
            fc.keyframe_points.clear();fc.keyframe_points.add(len(output["keys"]))
            # extrapolation's RNA update deduplicates frame-zero placeholders:
            # set it BEFORE add, then bulk-fill distinct coordinates.
            fc.keyframe_points.foreach_set("co", [v for pair in output["keys"] for v in pair])
            for key in fc.keyframe_points:
                key.interpolation = "CONSTANT"
            fc.update()
            require([[float(k.co[0]), float(k.co[1])] for k in fc.keyframe_points]
                    == output["keys"], "原生IK曲线写入未保留全部事件")
    preserved_after = canonical_hash(_ik_curve_payload(action, toggle_set | output_paths))
    require(preserved_before == preserved_after, "IK编译改变了原body曲线内容")
    proof["non_ik_curves_sha256_before"] = preserved_before
    proof["non_ik_curves_sha256_after"] = preserved_after
    proof["status"] = "STATIC_PARTIAL" if proof["unmatched_channels"] else "COMPILED"
    proof["action"]["signature"] = action_signature(action)
    return proof


def _import_clip(module, rig, meshes, root, clip_spec, scale, source_scene,
                 mapping, pmx_bones_by_name, native_ik_defaults):
    vmd_path = Path(clip_spec["vmd"]).resolve(strict=True)
    fps = float(clip_spec["fps"])
    require(abs(fps - SUPPORTED_SOURCE_FPS) < 1e-9,
            "当前MMD VMD时基只支持显式30fps")
    inventory = _parse_vmd(module, vmd_path)
    actions_before_import = set(bpy.data.actions)
    _select_only(root)
    source_scene.frame_set(0)
    status = bpy.ops.mmd_tools.import_vmd(
        filepath=str(vmd_path),
        scale=scale,
        margin=0,
        bone_mapper="PMX",
        use_pose_mode=False,
        update_scene_settings=False,
        create_new_action=True,
        use_nla=False,
        save_log=False,
        detect_camera_changes=True,
        detect_light_changes=True,
    )
    require(status == {"FINISHED"}, "VMD导入未完成：" + str(status))
    bound_actions = []

    def add_bound_action(owner):
        animation_data = getattr(owner, "animation_data", None)
        if animation_data is not None and animation_data.action is not None:
            bound_actions.append(animation_data.action)

    add_bound_action(rig)
    add_bound_action(root)
    for mesh in meshes:
        if mesh.data.shape_keys is not None:
            add_bound_action(mesh.data.shape_keys)
    bound_actions = list(
        {action.name: action for action in bound_actions}.values()
    )
    for action in bound_actions:
        action.use_fake_user = True
        slots = [slot.identifier for slot in action.slots]
        action[TIME_KEY] = json.dumps(
            {
                "fps": fps,
                "slots": slots,
                "clip_id": clip_spec["id"],
                "source": "MMD_VMD_EXPLICIT",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    armature_action = rig.animation_data.action if rig.animation_data else None
    armature_slot = (
        rig.animation_data.action_slot.identifier
        if rig.animation_data is not None and rig.animation_data.action_slot
        else None
    )
    native_ik = _compile_native_ik(
        rig, meshes, root, inventory, armature_action, armature_slot,
        native_ik_defaults, set(bpy.data.actions) - actions_before_import)
    shape_records = []
    for mesh in meshes:
        if mesh.data.shape_keys is None:
            continue
        animation_data = mesh.data.shape_keys.animation_data
        action = animation_data.action if animation_data else None
        if action is None:
            continue
        slot = (
            animation_data.action_slot.identifier
            if animation_data.action_slot
            else (action.slots[0].identifier if action.slots else None)
        )
        require(slot is not None, "morph Action缺少Slot：" + action.name)
        shape_records.append(
            {
                "mesh": mesh.name,
                "action": {**_action_record(action), "slot": slot},
            }
        )
    all_clip_actions = set(bound_actions)
    ranges = [
        record["action"]["range"] for record in shape_records
        if record["action"]["range"]
    ]
    armature_record = _action_record(armature_action)
    if armature_record is not None:
        armature_record["slot"] = armature_slot
    if armature_record and armature_record["range"]:
        ranges.append(armature_record["range"])
    if ranges:
        clip_range = [
            min(float(value[0]) for value in ranges),
            max(float(value[1]) for value in ranges),
        ]
    else:
        clip_range = [float(value) for value in inventory["frame_range"]] if inventory["frame_range"] else None
    frames = _frame_samples(clip_range)
    gaze_names = [
        name for name in GAZE_NAMES if rig.pose.bones.get(name) is not None
    ]
    observations = (
        _sample_pose(rig, meshes, mapping, gaze_names, clip_range, frames)
        if clip_range
        else []
    )
    variation = _variation(observations)
    requested_bones = set(inventory["bone_channels"])
    matched_bones = (
        _action_bones(armature_action) if armature_action is not None else set()
    )
    missing_bones = sorted(requested_bones - matched_bones)
    requested_morphs = set(inventory["morph_channels"])
    matched_morphs = set()
    for record in shape_records:
        action = bpy.data.actions.get(record["action"]["name"])
        matched_morphs |= _action_shape_keys(action)
    missing_morphs = sorted(requested_morphs - matched_morphs)
    shape_keys_available = {
        mesh.name: [key.name for key in mesh.data.shape_keys.key_blocks]
        if mesh.data.shape_keys
        else []
        for mesh in meshes
    }
    requested_ik = set(inventory["ik_states"])
    matched_ik = (
        _action_ik_toggles(armature_action) if armature_action is not None else set()
    )
    root_meta = _root_metadata(root)
    model_registered_morphs = set()
    if root_meta.get("available"):
        for values in root_meta["morphs"].values():
            model_registered_morphs.update(item["name"] for item in values)
    missing_but_model_registered = sorted(
        (requested_morphs & model_registered_morphs) - matched_morphs
    )
    source_ad = {}
    if rig.animation_data is not None and rig.animation_data.action is not None:
        source_ad = {
            "action": rig.animation_data.action.name,
            "slot": rig.animation_data.action_slot.identifier
            if rig.animation_data.action_slot
            else None,
            "blend_type": rig.animation_data.action_blend_type,
            "influence": float(rig.animation_data.action_influence),
            "use_nla": bool(rig.animation_data.use_nla),
        }
    ik_toggle_state = {
        pbone.name: bool(pbone.mmd_ik_toggle)
        for pbone in rig.pose.bones
        if hasattr(pbone, "mmd_ik_toggle")
    }
    constraint_influences = {
        pbone.name + "::" + constraint.name: {
            "influence": float(constraint.influence),
            "mute": bool(constraint.mute),
        }
        for pbone in rig.pose.bones
        for constraint in pbone.constraints
    }
    clip = {
        "id": clip_spec["id"],
        "vmd": {
            "file": str(vmd_path),
            "bytes": vmd_path.stat().st_size,
            "sha256": sha256_file(vmd_path),
        },
        "fps": fps,
        "timebase": "EXPLICIT_VMD_30FPS",
        "inventory": inventory,
        "range": clip_range,
        "sample_frames": frames,
        "armature_action": armature_record,
        "shape_key_actions": shape_records,
        "other_actions": [
            _action_record(action)
            for action in sorted(all_clip_actions, key=lambda item: item.name)
            if action is not armature_action
            and action.name not in {record["action"]["name"] for record in shape_records}
        ],
        "requested": {
            "bone_channels": sorted(requested_bones),
            "morph_channels": sorted(requested_morphs),
            "ik_property_channels": sorted(requested_ik),
            "camera_keys": inventory["camera_keys"],
            "light_keys": inventory["light_keys"],
        },
        "matched": {
            "bone_channels": sorted(requested_bones & matched_bones),
            "morph_channels": sorted(requested_morphs & matched_morphs),
            "ik_property_channels": sorted(requested_ik & matched_ik),
        },
        "missing": {
            "bone_channels": missing_bones,
            "morph_channels": missing_morphs,
            "morph_channels_model_registered_but_unassembled":
                missing_but_model_registered,
        },
        "shape_keys_available": shape_keys_available,
        "model_registered_morphs": root_meta.get("morphs", {}),
        "gaze": {
            "bones_present": gaze_names,
            "channels_present": sorted(requested_bones & set(gaze_names)),
            "matched_bones": sorted(matched_bones & set(gaze_names)),
            "variation": variation["gaze_variation"],
            "readable": bool(gaze_names and (matched_bones & set(gaze_names))),
        },
        "morph_variation": variation["changing_morphs"],
        "body_translation_variation": variation["body_translation_variation"],
        "observations": observations,
        "source_ad_at_import": source_ad,
        "body_convertible": bool(
            armature_record
            and armature_record.get("range")
            and float(armature_record["range"][0])
            < float(armature_record["range"][1])
        ),
        "native_ik": native_ik,
        "ik_toggle_state": ik_toggle_state,
        "constraint_influences": constraint_influences,
        "binding": {
            "armature_action": armature_action.name if armature_action else None,
            "armature_slot": rig.animation_data.action_slot.identifier
            if rig.animation_data is not None and rig.animation_data.action_slot
            else None,
            "shape_key_actions": [
                {
                    "mesh": record["mesh"],
                    "action": record["action"]["name"],
                    "slot": record["action"]["slot"],
                }
                for record in shape_records
            ],
        },
        "owned_actions": sorted(action.name for action in all_clip_actions),
    }
    return clip


def _scene_state_preserved(before, after):
    return before == after


def _path_identity(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _paths_alias(left, right):
    if _path_identity(left) == _path_identity(right):
        return True
    try:
        return os.path.exists(left) and os.path.exists(right) and os.path.samefile(left, right)
    except OSError:
        return False


def _reject_artifact_collisions(outputs, inputs):
    for label, path in outputs:
        for input_path in inputs:
            require(not _paths_alias(path, input_path),
                    f"输出{label}不得指向输入文件：{path}")
    for index, (left_label, left) in enumerate(outputs):
        for right_label, right in outputs[index + 1:]:
            require(not _paths_alias(left, right),
                    f"输出{left_label}与{right_label}路径冲突：{left}")


def _new_scene_name():
    base = "C94_MMD_SOURCE"
    if base not in bpy.data.scenes:
        return base
    index = 1
    while f"{base}.{index:03d}" in bpy.data.scenes:
        index += 1
    return f"{base}.{index:03d}"


def import_source(pmx, vmd, *, output_dir, clips=None, primary_clip_id=None,
                  scale=SUPPORTED_SCALE, axis=SUPPORTED_AXIS,
                  source_fps=SUPPORTED_SOURCE_FPS, addon_path=None,
                  opencc_wheel=None, import_camera=False,
                  require_verified=True, output_stem="output-source",
                  blend_name=None, profile_name=None, descriptor_name=None,
                  batch_identity=None, overwrite=False,
                  expected_pmx_bones=None, expected_pmx_vertices=None,
                  require_complete_fingers=True):
    """Import PMX+VMD in an isolated background Scene and persist a profile."""

    require(bpy.app.background,
            "import_source只允许在后台factory工作副本运行；GUI请调用公共CLI后台任务")
    require(abs(float(scale) - SUPPORTED_SCALE) < 1e-12,
            "scale必须显式保持0.08")
    require(axis == SUPPORTED_AXIS, "只支持显式MMD_XZY_SWIZZLE轴转换")
    require(abs(float(source_fps) - SUPPORTED_SOURCE_FPS) < 1e-9,
            "MMD VMD源时基必须显式30fps，不使用Scene全局fps回退")
    pmx_path = Path(pmx).resolve(strict=True)
    vmd_path = Path(vmd).resolve(strict=True)
    require(pmx_path.suffix.lower() == ".pmx", "主入口当前只接受PMX")
    require(vmd_path.suffix.lower() == ".vmd", "主入口当前只接受VMD")
    output_dir = Path(output_dir).resolve()
    require(output_dir.is_dir(), "source output目录必须已存在且显式：" + str(output_dir))
    blend_path = output_dir / (
        blend_name or (output_stem + ".blend")
    )
    profile_path = output_dir / (
        profile_name or (output_stem + "-profile.json")
    )
    descriptor_path = output_dir / (
        descriptor_name or (output_stem + "-descriptor.json")
    )
    for artifact in (blend_path, profile_path, descriptor_path):
        if artifact.exists() and not overwrite:
            raise ValueError("输出已存在，拒绝覆盖；显式overwrite=True才可替换：" + str(artifact))
    require(
        blend_path.resolve() not in {pmx_path, vmd_path},
        "输出blend不得指向输入文件",
    )

    clip_specs = []
    seen_paths = set()
    primary_spec = {
        "id": primary_clip_id or vmd_path.stem,
        "vmd": str(vmd_path),
        "fps": float(source_fps),
    }
    clip_specs.append(primary_spec)
    seen_paths.add(vmd_path)
    if clips:
        for index, spec in enumerate(clips):
            require(isinstance(spec, dict), "clips项必须为纯数据字典")
            path = Path(spec["vmd"]).resolve(strict=True)
            require(path.suffix.lower() == ".vmd", "额外clip必须为VMD")
            require(path not in seen_paths,
                    "同一VMD仅登记一次，重复请求请移除：" + str(path))
            seen_paths.add(path)
            fps = float(spec.get("fps", source_fps))
            require(abs(fps - SUPPORTED_SOURCE_FPS) < 1e-9,
                    "额外VMD clip必须显式30fps")
            clip_specs.append(
                {
                    "id": spec.get("id") or path.stem,
                    "vmd": str(path),
                    "fps": fps,
                }
            )
    ids = [spec["id"] for spec in clip_specs]
    require(len(ids) == len(set(ids)), "clip id必须唯一")
    primary_clip_id = primary_spec["id"]

    input_paths = [pmx_path, *[Path(spec["vmd"]) for spec in clip_specs]]
    _reject_artifact_collisions(
        [("blend", blend_path), ("profile", profile_path), ("descriptor", descriptor_path)],
        input_paths,
    )
    inputs_before = {
        str(path): {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in input_paths
    }
    original_scenes = {scene.name: _scene_state(scene) for scene in bpy.data.scenes}
    original_scene = bpy.context.scene
    original_selected = list(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active

    inspection = inspect_backend(addon_path=addon_path, opencc_wheel=opencc_wheel)
    require(inspection["available"], "MMD Tools不可用：" + str(inspection.get("error")))
    if require_verified:
        require(inspection["verified"],
                "仅接受已验MMD Tools 4.5.14/GPL-3.0-or-later")
    backend = load_backend(
        addon_path=inspection["addon_path"],
        opencc_wheel=inspection["wheel"]["file"],
        runtime_dir=output_dir / "runtime-deps",
    )
    module = backend["module"]
    pmx_data = _parse_pmx(module, pmx_path)
    if expected_pmx_bones is not None:
        require(len(pmx_data["bones"]) == int(expected_pmx_bones),
                "PMX骨数与显式期望不符，实际" + str(len(pmx_data["bones"])))
    if expected_pmx_vertices is not None:
        require(pmx_data["vertices"] == int(expected_pmx_vertices),
                "PMX顶点数与显式期望不符，实际" + str(pmx_data["vertices"]))
    pmx_bones_by_name = {
        row["name_j_nfkc"]: row for row in pmx_data["bones"]
    }

    source_scene = bpy.data.scenes.new(_new_scene_name())
    source_scene.render.fps = 30
    source_scene.render.fps_base = 1.0
    source_scene.frame_start = 0
    source_scene.frame_end = 1
    window = bpy.context.window
    require(window is not None, "后台进程缺少window，无法建立隔离源Scene")
    window.scene = source_scene
    source_scene.view_layers[0].update()

    source_scene_objects_before = set(source_scene.objects)
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    status = bpy.ops.mmd_tools.import_model(
        filepath=str(pmx_path),
        types={"MESH", "ARMATURE", "MORPHS"},
        scale=float(scale),
        clean_model=False,
        remove_doubles=False,
        rename_bones=False,
        fix_ik_links=False,
        save_log=False,
    )
    require(status == {"FINISHED"}, "PMX导入未完成：" + str(status))
    new_objects = [
        obj for obj in source_scene.objects
        if obj not in source_scene_objects_before
    ]
    rigs = [obj for obj in new_objects if obj.type == "ARMATURE"]
    meshes = [
        obj
        for obj in new_objects
        if obj.type == "MESH"
        and any(
            modifier.type == "ARMATURE" and modifier.object == rigs[0]
            for modifier in obj.modifiers
        )
    ] if len(rigs) == 1 else []
    require(len(rigs) == 1, "PMX应恰好返回一个Armature，实际" + str(len(rigs)))
    rig = rigs[0]
    require(meshes, "PMX导入未返回Armature绑定的Mesh")
    minimum_vertices = (
        int(expected_pmx_vertices) if expected_pmx_vertices is not None else 1
    )
    require(
        sum(len(mesh.data.vertices) for mesh in meshes) >= minimum_vertices,
        "导入Mesh顶点总数少于PMX源",
    )
    root = _find_root(rig, new_objects)
    require(root is not None, "未找到MMD根对象")
    require(root.type == "EMPTY" or root == rig, "MMD根对象类型异常：" + root.type)
    mesh_vertex_counts = {mesh.name: len(mesh.data.vertices) for mesh in meshes}
    require(
        sum(mesh_vertex_counts.values()) >= minimum_vertices,
        "导入后Armature Mesh顶点数不足",
    )

    from . import retarget_mmd as mmd

    weights = mmd.weight_sums(rig)
    mapping, mapping_profile = mmd.build_mapping(
        rig, weights=weights, pmx_bones=pmx_data["bones"]
    )
    require(mapping["schema"] == MAPPING_SCHEMA, "共享mapping schema不符")
    require(mapping_profile["schema"] == MAPPING_PROFILE_SCHEMA,
            "共享mapping profile schema不符")
    require(len(mapping["bones"]) >= 59,
            "身体/肢/指mapping应至少59项，实际" + str(len(mapping["bones"])))
    if require_complete_fingers:
        require(len(mapping["fingers"]["complete"]) == 10,
                "应完整解析10条四节点手指链，实际"
                + str(len(mapping["fingers"]["complete"])))
    require(not mapping_profile["terminal_helpers"],
            "默认源导入不得修改源骨架添加helper")

    bones = _bone_metadata(rig)
    name_j_duplicates = _nfkc_duplicates(bones)
    require(not name_j_duplicates,
            "源name_j NFKC重复，拒绝猜测：" + json.dumps(name_j_duplicates,
                                                         ensure_ascii=False,
                                                         sort_keys=True))
    finger_records = _finger_records(rig, mapping, mapping_profile)
    true_ends = [
        row for row in finger_records if row["three_section_plus_true_end"]
    ]
    if require_complete_fingers:
        require(len(true_ends) == 10,
                "应验证10条三节+真端手指链，实际" + str(len(true_ends)))
    weight_records = _weight_records(meshes)
    foot_records = _foot_records(weight_records)
    require(foot_records["ordinary_chain_d_zero"],
            "普通脚链D骨出现正权，拒绝继续：" + json.dumps(
                foot_records["d_positive_weights"], ensure_ascii=False
            ))
    require(
        all(
            foot_records["weight_sums"][name] > 0
            for name in ("左足", "左ひざ", "左足首", "右足", "右ひざ", "右足首")
        ),
        "普通脚链存在无正权骨，无法确认实际皮肤消费",
    )

    driver_rows = []
    for id_data in (rig, rig.data, *meshes, *[
        mesh.data.shape_keys for mesh in meshes if mesh.data.shape_keys
    ]):
        driver_rows.extend(_driver_records(id_data))
    driver_rows = sorted(
        driver_rows,
        key=lambda row: (
            str(row["id"]),
            row["data_path"],
            int(row["array_index"]),
        ),
    )

    if import_camera:
        primary_inventory = _parse_vmd(module, Path(primary_spec["vmd"]))
        camera_import = _import_optional_camera(
            module,
            root,
            Path(primary_spec["vmd"]),
            source_scene,
            float(scale),
            primary_inventory,
            float(primary_spec["fps"]),
        )
    else:
        camera_import = {
            "requested": False,
            "imported": False,
            "reason": "显式import_camera=False",
            "active_camera_replaced": False,
            "camera_object": None,
            "actions": [],
        }

    native_ik_defaults = {pb.name: bool(pb.mmd_ik_toggle) for pb in rig.pose.bones}
    clip_records = []
    for spec in clip_specs:
        clip_record = _import_clip(
            module,
            rig,
            meshes,
            root,
            spec,
            float(scale),
            source_scene,
            mapping,
            pmx_bones_by_name,
            native_ik_defaults,
        )
        clip_records.append(clip_record)
    primary_clip = next(
        clip for clip in clip_records if clip["id"] == primary_clip_id
    )
    _bind_clip(rig, meshes, primary_clip)
    source_scene.frame_start = int(round(primary_clip["range"][0]))
    source_scene.frame_end = int(round(primary_clip["range"][1]))
    source_scene.render.fps = 30
    source_scene.render.fps_base = 1.0
    source_scene.frame_set(0)
    source_scene.camera = None

    owned_action_names = set()
    for clip in clip_records:
        owned_action_names.update(clip.get("owned_actions", []))
    owned_action_names.update(camera_import.get("action_names", []))

    source_uid = uuid.uuid4().hex
    for obj, role in (
        (root, "ROOT"),
        (rig, "ARMATURE"),
        *[(mesh, "MESH") for mesh in meshes],
    ):
        obj[UID_KEY] = source_uid
        obj[ROLE_KEY] = role
    for action_name in sorted(owned_action_names):
        action = bpy.data.actions.get(action_name)
        require(action is not None, "已绑定源Action丢失：" + action_name)
        action[UID_KEY] = source_uid
        action[ROLE_KEY] = "ACTION"
    for mesh in meshes:
        if mesh.data.shape_keys is not None:
            mesh.data.shape_keys[UID_KEY] = source_uid
            mesh.data.shape_keys[ROLE_KEY] = "SHAPE_KEYS"
    source_scene[UID_KEY] = source_uid
    source_scene[ROLE_KEY] = "SOURCE_SCENE"
    source_scene[BACKEND_KEY] = json.dumps(
        {
            "id": BACKEND_ID,
            "version": inspection["version"],
            "verified": inspection["verified"],
            "only_verified_backend": True,
            "self_contained_pmx_interpreter": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    objects = {
        "root": {
            "name": root.name,
            "type": root.type,
            "data": root.data.name if root.data else None,
            "uid": source_uid,
            "role": "ROOT",
        },
        "armature": {
            "name": rig.name,
            "type": rig.type,
            "data": rig.data.name,
            "uid": source_uid,
            "role": "ARMATURE",
            "bone_count": len(rig.data.bones),
            "pose_bone_count": len(rig.pose.bones),
        },
        "meshes": [
            {
                "name": mesh.name,
                "type": mesh.type,
                "data": mesh.data.name,
                "uid": source_uid,
                "role": "MESH",
                "vertex_count": len(mesh.data.vertices),
                "polygon_count": len(mesh.data.polygons),
                "shape_key_count": len(mesh.data.shape_keys.key_blocks)
                if mesh.data.shape_keys
                else 0,
                "mesh_signature": _mesh_signature(mesh),
            }
            for mesh in sorted(meshes, key=lambda item: item.name)
        ],
        "other_new_objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "data": obj.data.name if obj.data else None,
                "parent": obj.parent.name if obj.parent else None,
                "mmd_type": getattr(obj, "mmd_type", None),
            }
            for obj in sorted(new_objects, key=lambda item: item.name)
            if obj not in {root, rig, *meshes}
        ],
    }
    actions = [
        _action_record(bpy.data.actions[action_name])
        for action_name in sorted(owned_action_names)
    ]
    actions_by_name = {record["name"]: record for record in actions}
    for clip in clip_records:
        for record in clip["shape_key_actions"]:
            slot = record["action"].get("slot")
            full = actions_by_name.get(record["action"]["name"])
            if full is not None:
                full = deepcopy(full)
                full["slot"] = slot
                record["action"] = full
        if clip["armature_action"]:
            slot = clip["armature_action"].get("slot")
            full = actions_by_name.get(clip["armature_action"]["name"])
            if full is not None:
                full = deepcopy(full)
                full["slot"] = slot
                clip["armature_action"] = full

    shape_keys = {
        mesh.name: _shape_keys(mesh) for mesh in meshes
    }
    pmx_bone_names = {row["name_j"] for row in pmx_data["bones"]}
    extra_bone_names = sorted(
        {bone.name for bone in rig.pose.bones} - pmx_bone_names
    )
    source_graph = {
        "source_uid": source_uid,
        "source_bones": len(rig.pose.bones),
        "pmx_bones": len(pmx_data["bones"]),
        "blender_bones": len(rig.data.bones),
        "extra_blender_bones": extra_bone_names,
        "extra_blender_bone_details": [
            {
                "blender_name": name,
                "name_j_raw": next(
                    row["name_j_raw"]
                    for row in bones
                    if row["blender_name"] == name
                ),
                "parent": rig.data.bones[name].parent.name
                if rig.data.bones[name].parent
                else None,
                "use_deform": bool(rig.data.bones[name].use_deform),
                "bone_id": next(
                    row["bone_id"]
                    for row in bones
                    if row["blender_name"] == name
                ),
                "source": "MMD_TOOLS_IMPORT_AUXILIARY",
            }
            for name in extra_bone_names
        ],
        "name_j_empty_bones": [
            row["blender_name"] for row in bones if not row["name_j_nfkc"]
        ],
        "name_j_nfkc_unique": not name_j_duplicates,
        "name_j_duplicates": name_j_duplicates,
        "bones": bones,
        "bones_by_pmx_name": pmx_data["bones"],
        "parent_chain": {
            bone.name: bone.parent.name if bone.parent else None
            for bone in sorted(rig.data.bones, key=lambda item: item.name)
        },
        "rest_signature": rest_signature(rig),
        "parent_signature": parent_signature(rig),
        "constraint_signature": constraint_signature(rig),
        "object_signature": object_signature(root, rig, meshes),
        "constraints": [
            {
                "bone": pbone.name,
                "name": constraint.name,
                "type": constraint.type,
                "target": constraint.target.name
                if getattr(constraint, "target", None)
                else None,
                "subtarget": getattr(constraint, "subtarget", None),
                "owner_space": getattr(constraint, "owner_space", None),
                "target_space": getattr(constraint, "target_space", None),
                "chain_count": getattr(constraint, "chain_count", None),
                "influence": float(constraint.influence),
            }
            for pbone in rig.pose.bones
            for constraint in pbone.constraints
        ],
        "drivers": driver_rows,
        "driver_signature": canonical_hash(driver_rows),
        "fingers": finger_records,
        "finger_complete_count": len(mapping["fingers"]["complete"]),
        "finger_true_end_count": len(true_ends),
        "foot": foot_records,
        "weight_sums": weight_records,
        "shape_keys": shape_keys,
        "gaze_bones": {
            name: {
                "present": rig.pose.bones.get(name) is not None,
                "bone_id": next(
                    (
                        row["bone_id"]
                        for row in bones
                        if row["blender_name"] == name
                    ),
                    None,
                ),
            }
            for name in GAZE_NAMES
        },
        "root_metadata": _root_metadata(root),
        "pmx": {
            "name": pmx_data["name"],
            "name_e": pmx_data["name_e"],
            "vertices": pmx_data["vertices"],
            "faces": pmx_data["faces"],
            "materials": pmx_data["materials"],
            "textures": pmx_data["textures"],
            "morphs": pmx_data["morphs"],
        },
    }
    mapping_sha256 = canonical_hash(mapping)
    mapping_profile_sha256 = canonical_hash(mapping_profile)
    scene_config = {
        "schema": SCENE_SCHEMA,
        "source_uid": source_uid,
        "source_blend": str(blend_path),
        "profile_path": str(profile_path),
        "backend": {
            "id": BACKEND_ID,
            "version": inspection["version"],
            "verified": inspection["verified"],
            "only_verified_backend": True,
            "self_contained_pmx_interpreter": False,
            "source_identity_sha256": inspection["source_identity_sha256"],
        },
        "primary_clip": primary_clip_id,
        "camera_import": camera_import,
        "objects": objects,
        "clips": [
            {
                "id": clip["id"],
                "fps": clip["fps"],
                "range": clip["range"],
                "armature_action": clip["binding"]["armature_action"],
                "armature_slot": clip["binding"]["armature_slot"],
                "shape_key_actions": clip["binding"]["shape_key_actions"],
            }
            for clip in clip_records
        ],
        "immutable": {
            "rest_signature": source_graph["rest_signature"],
            "parent_signature": source_graph["parent_signature"],
            "object_signature": source_graph["object_signature"],
            "mapping_sha256": mapping_sha256,
        },
        "created_utc": utc_now(),
    }
    scene_config_sha256 = canonical_hash(scene_config)
    source_scene[SCENE_KEY] = json.dumps(
        scene_config, ensure_ascii=False, sort_keys=True
    )

    checks = {
        "name_j_nfkc_unique": not name_j_duplicates,
        "finger_complete_count": len(mapping["fingers"]["complete"]),
        "finger_true_end_count": len(true_ends),
        "terminal_helpers": len(mapping_profile["terminal_helpers"]),
        "ordinary_foot_chain_d_zero": foot_records["ordinary_chain_d_zero"],
        "mmdtools_auxiliary_bone_count": len(extra_bone_names),
        "all_clips_explicit_timebase": all(
            clip["fps"] == SUPPORTED_SOURCE_FPS for clip in clip_records
        ),
        "morph_missing_channels_explicit": all(
            clip["missing"]["morph_channels"] is not None for clip in clip_records
        ),
        "physics_imported": False,
        "textures_downloaded": False,
        "camera_auto_active_replace": camera_import["active_camera_replaced"],
    }
    clip_checks = {}
    for clip in clip_records:
        clip_checks[clip["id"]] = {
            "changing_morph_count": len(clip["morph_variation"]),
            "gaze_readable": clip["gaze"]["readable"],
            "missing_bone_channel_count": len(clip["missing"]["bone_channels"]),
            "missing_morph_channel_count": len(clip["missing"]["morph_channels"]),
            "missing_morph_channels_model_registered_but_unassembled":
                len(clip["missing"]["morph_channels_model_registered_but_unassembled"]),
            "body_convertible": clip["body_convertible"],
        }
    profile = {
        "schema": PROFILE_SCHEMA,
        "source_schema": SCHEMA,
        "created_utc": utc_now(),
        "batch_identity": batch_identity or {},
        "backend": {
            "id": BACKEND_ID,
            "extension_id": BACKEND_EXTENSION_ID,
            "version": inspection["version"],
            "verified": inspection["verified"],
            "verified_version": VERIFIED_BACKEND_VERSION,
            "license": inspection["license"],
            "copyright": inspection["copyright"],
            "only_verified_backend": True,
            "self_contained_pmx_interpreter": False,
            "implicit_install_enable": False,
            "source_identity_sha256": inspection["source_identity_sha256"],
            "files": inspection["files"],
            "wheel": inspection["wheel"],
            "runtime_registration": {
                "runtime_only": True,
                "preferences_saved": False,
                "addon_import_name": "mmd_tools",
                "module_file": backend["module_file"],
            },
        },
        "inputs": {
            "pmx": {
                "file": str(pmx_path),
                "bytes": pmx_path.stat().st_size,
                "sha256": sha256_file(pmx_path),
            },
            "vmds": [
                {
                    "file": spec["vmd"],
                    "bytes": Path(spec["vmd"]).stat().st_size,
                    "sha256": sha256_file(spec["vmd"]),
                    "clip_id": spec["id"],
                }
                for spec in clip_specs
            ],
        },
        "import_parameters": {
            "mode": "BACKGROUND_NEW_SCENE",
            "scale": float(scale),
            "axis": axis,
            "axis_conversion": "MMD(x,y,z) -> Blender(x,z,y)",
            "source_fps": float(source_fps),
            "source_fps_global_fallback": False,
            "mmd_tools_types": ["MESH", "ARMATURE", "MORPHS"],
            "clean_model": False,
            "remove_doubles": False,
            "rename_bones": False,
            "fix_ik_links": False,
            "import_camera": bool(import_camera),
            "import_physics": False,
            "import_audio": False,
            "download_textures": False,
            "scene_name": source_scene.name,
        },
        "objects": objects,
        "actions": actions,
        "source_graph": source_graph,
        "mapping": mapping,
        "mapping_sha256": mapping_sha256,
        "mapping_profile": mapping_profile,
        "mapping_profile_sha256": mapping_profile_sha256,
        "clips": clip_records,
        "camera_import": camera_import,
        "primary_clip": primary_clip_id,
        "scene_config": scene_config,
        "scene_config_sha256": scene_config_sha256,
        "checks": checks,
        "clip_checks": clip_checks,
        "support": "body_plus_complete_four_node_fingers_plus_existing_morph_gaze_channels",
        "complete_face_conversion": False,
        "limitations": [
            "MMD Tools 4.5.14是首次导入的显式后端；本模块不自含PMX/VMD解释器。",
            "未逐帧对照原MMD播放器；姿态/IK求值一致性只限已记录的源采样。",
            "缺失morph通道逐项记录，未自动修复、补键或新建表情曲线。",
            "物理、贴图、音乐/音频未自动导入。",
            (
                "相机可选导入已执行；未替换active camera，但未做相机视觉一致验收。"
                if camera_import.get("imported")
                else "相机通道在样本中为0或显式关闭；相机可选导入路径未执行。"
            ),
            "冷开验证不注册MMD Tools，只读取persisted profile与原生数据。",
        ],
    }
    profile_sha256 = profile_digest(profile)
    profile["profile_sha256"] = profile_sha256
    for obj, role in (
        (root, "ROOT"),
        (rig, "ARMATURE"),
        *[(mesh, "MESH") for mesh in meshes],
    ):
        obj[PROFILE_SHA_KEY] = profile_sha256
    for action_name in sorted(owned_action_names):
        bpy.data.actions[action_name][PROFILE_SHA_KEY] = profile_sha256
    for mesh in meshes:
        if mesh.data.shape_keys is not None:
            mesh.data.shape_keys[PROFILE_SHA_KEY] = profile_sha256
    source_scene[PROFILE_SHA_KEY] = profile_sha256

    atomic_json(profile_path, profile)
    source_scene.frame_set(0)
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False)

    inputs_after = {
        str(path): {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in input_paths
    }
    scenes_after = {
        scene.name: _scene_state(scene)
        for scene in bpy.data.scenes
        if scene.name != source_scene.name
    }
    expected_original = {
        name: state for name, state in original_scenes.items()
        if name != source_scene.name
    }
    preserved = {
        "inputs_unchanged": inputs_before == inputs_after,
        "non_source_scenes_unchanged": scenes_after == expected_original,
        "source_file_not_written": sha256_file(pmx_path)
        == inputs_before[str(pmx_path)]["sha256"],
        "source_scene_camera_not_replaced": source_scene.camera is None,
    }
    require(all(preserved.values()), "导入保护检查失败：" + json.dumps(preserved))
    blend_sha256 = sha256_file(blend_path)
    profile_sha256_on_disk = sha256_file(profile_path)
    descriptor = {
        "schema": DESCRIPTOR_SCHEMA,
        "source_uid": source_uid,
        "batch_identity": batch_identity or {},
        "backend": {
            "id": BACKEND_ID,
            "version": inspection["version"],
            "verified": inspection["verified"],
            "only_verified_backend": True,
            "self_contained_pmx_interpreter": False,
            "source_identity_sha256": inspection["source_identity_sha256"],
        },
        "inputs": profile["inputs"],
        "artifacts": {
            "blend": {
                "file": str(blend_path),
                "bytes": blend_path.stat().st_size,
                "sha256": blend_sha256,
            },
            "profile": {
                "file": str(profile_path),
                "bytes": profile_path.stat().st_size,
                "sha256": profile_sha256_on_disk,
                "profile_sha256": profile_sha256,
            },
            "descriptor": {
                "file": str(descriptor_path),
            },
        },
        "source_scene": source_scene.name,
        "primary_clip": primary_clip_id,
        "camera_import": camera_import,
        "objects": {
            "root": objects["root"],
            "armature": objects["armature"],
            "meshes": objects["meshes"],
            "other_new_objects": objects["other_new_objects"],
        },
        "actions": [
            {
                "name": record["name"],
                "slots": record["slots"],
                "signature": record["signature"],
                "range": record["range"],
                "fcurve_count": record["fcurve_count"],
                "key_count": record["key_count"],
            }
            for record in actions
        ],
        "clips": [
            {
                "id": clip["id"],
                "vmd": clip["vmd"],
                "fps": clip["fps"],
                "range": clip["range"],
                "armature_action": clip["binding"]["armature_action"],
                "armature_slot": clip["binding"]["armature_slot"],
                "shape_key_actions": clip["binding"]["shape_key_actions"],
                "body_convertible": clip["body_convertible"],
                "owned_actions": clip.get("owned_actions", []),
                "requested": clip["requested"],
                "matched": clip["matched"],
                "missing": clip["missing"],
                "gaze": {
                    "bones_present": clip["gaze"]["bones_present"],
                    "channels_present": clip["gaze"]["channels_present"],
                    "matched_bones": clip["gaze"]["matched_bones"],
                    "readable": clip["gaze"]["readable"],
                    "variation": clip["gaze"]["variation"],
                },
                "morph_variation": clip["morph_variation"],
            }
            for clip in clip_records
        ],
        "checks": checks,
        "clip_checks": clip_checks,
        "preserved": preserved,
        "scene_config_sha256": scene_config_sha256,
        "profile_sha256": profile_sha256,
        "limitations": profile["limitations"],
    }
    atomic_json(descriptor_path, descriptor)

    if window is not None:
        window.scene = original_scene
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in original_selected:
        if obj.name in bpy.context.view_layer.objects:
            obj.select_set(True)
    bpy.context.view_layer.objects.active = original_active
    return {
        "ok": True,
        "profile": profile,
        "descriptor": descriptor,
        "profile_path": str(profile_path),
        "descriptor_path": str(descriptor_path),
        "blend_path": str(blend_path),
        "backend": inspection,
        "source_scene": source_scene.name,
        "primary_clip": primary_clip_id,
    }


def profile_digest(profile):
    payload = {
        key: value for key, value in profile.items() if key != "profile_sha256"
    }
    return canonical_hash(payload)


def _profile_bones_by_name(profile):
    return {
        row["blender_name"]: row for row in profile["source_graph"]["bones"]
    }


def validate_profile_document(profile):
    """Validate pure-JSON profile invariants without touching Blender data."""

    profile = load_profile_document(profile)
    require(isinstance(profile, dict), "profile必须为JSON对象")
    forbidden = [
        key
        for key in profile
        if any(token in key.lower() for token in ("python", "exec", "eval", "script"))
    ]
    require(not forbidden,
            "profile拒绝任意Python预设字段：" + json.dumps(forbidden, ensure_ascii=False))
    require(profile.get("schema") == PROFILE_SCHEMA, "profile schema不符")
    require(
        profile.get("profile_sha256") == profile_digest(profile),
        "profile_sha256不符，拒绝被修改的profile",
    )
    require(
        profile.get("mapping_sha256") == canonical_hash(profile["mapping"]),
        "mapping_sha256不符",
    )
    require(
        profile.get("mapping_profile_sha256")
        == canonical_hash(profile["mapping_profile"]),
        "mapping_profile_sha256不符",
    )
    require(
        profile.get("scene_config_sha256")
        == canonical_hash(profile["scene_config"]),
        "scene_config_sha256不符",
    )
    graph = profile["source_graph"]
    bones = graph["bones"]
    require(len(bones) == graph["source_bones"], "source bone记录数量不符")
    require(
        len({row["blender_name"] for row in bones}) == len(bones),
        "source bone名称重复",
    )
    missing_constraint_space = [
        row["blender_name"] + "::" + constraint.get("name", "?")
        for row in bones
        for constraint in row.get("constraints", [])
        if "owner_space" not in constraint or "target_space" not in constraint
    ]
    require(
        not missing_constraint_space,
        "profile约束缺owner_space/target_space，需重新捕获源metadata："
        + json.dumps(missing_constraint_space, ensure_ascii=False),
    )
    by_name = {row["blender_name"]: row for row in bones}
    require(
        graph["rest_signature"] == _rest_signature_from_rows(bones),
        "source_graph rest_signature与骨rest数据不符",
    )
    require(
        graph["parent_signature"] == _parent_signature_from_rows(bones),
        "source_graph parent_signature与父链数据不符",
    )
    require(
        graph["object_signature"] == _object_signature_from_records(profile["objects"]),
        "object_signature与对象记录不符",
    )
    require(graph["source_uid"] == profile["scene_config"]["source_uid"],
            "source_uid与Scene配置不符")
    config = profile["scene_config"]
    require(config["schema"] == SCENE_SCHEMA, "Scene配置schema不符")
    require(
        config["immutable"]["rest_signature"] == graph["rest_signature"]
        and config["immutable"]["parent_signature"] == graph["parent_signature"]
        and config["immutable"]["object_signature"] == graph["object_signature"]
        and config["immutable"]["mapping_sha256"] == profile["mapping_sha256"],
        "Scene配置immutable签名与profile不符",
    )
    require(
        config["objects"]["root"]["name"] == profile["objects"]["root"]["name"]
        and config["objects"]["armature"]["name"]
        == profile["objects"]["armature"]["name"]
        and [mesh["name"] for mesh in config["objects"]["meshes"]]
        == [mesh["name"] for mesh in profile["objects"]["meshes"]],
        "Scene配置对象引用与profile不符",
    )
    require(graph["name_j_nfkc_unique"] and not graph["name_j_duplicates"],
            "profile记录name_j NFKC重复")
    mapping = profile["mapping"]
    mapping_profile = profile["mapping_profile"]
    require(mapping["schema"] == MAPPING_SCHEMA, "mapping schema不符")
    require(mapping_profile["schema"] == MAPPING_PROFILE_SCHEMA,
            "mapping profile schema不符")
    semantics = mapping_profile["semantics"]
    for semantic, bone_name in mapping["bones"].items():
        require(bone_name in by_name, "mapping目标骨不存在：" + bone_name)
        record = semantics.get(semantic)
        if record is not None:
            require(
                record["blender_bone"] == bone_name,
                "mapping与mapping_profile语义不一致：" + semantic,
            )
            require(
                record["name_j_nfkc"] == by_name[bone_name]["name_j_nfkc"],
                "mapping语义name_j不一致：" + semantic,
            )
    def is_ancestor(ancestor, child):
        current = by_name.get(child, {}).get("parent")
        while current is not None:
            if current == ancestor:
                return True
            current = by_name.get(current, {}).get("parent")
        return False

    for parent_semantic, child_semantic in (
        ("neck", "head"),
        *(("thigh." + side, "shin." + side) for side in FINGER_SIDES),
        *(("shin." + side, "foot." + side) for side in FINGER_SIDES),
    ):
        parent = mapping["bones"][parent_semantic]
        child = mapping["bones"][child_semantic]
        require(
            child in by_name and by_name[child]["parent"] == parent,
            "mapping父子链与persisted source graph不符："
            + parent_semantic + "->" + child_semantic,
        )
    for side in FINGER_SIDES:
        for parent_semantic, child_semantic in (
            ("upper_arm." + side, "forearm." + side),
            ("forearm." + side, "hand." + side),
        ):
            require(
                is_ancestor(
                    mapping["bones"][parent_semantic],
                    mapping["bones"][child_semantic],
                ),
                "mapping手臂链不是源层级后代："
                + parent_semantic + "->" + child_semantic,
            )
    require(
        is_ancestor(mapping["bones"]["spine"], mapping["bones"]["chest"]),
        "mapping上半身到上胸不是源层级后代",
    )
    require(len(mapping["fingers"]["complete"]) == 10, "完整手指链不是10条")
    for row in graph["fingers"]:
        if row["complete"]:
            require(row["three_section_plus_true_end"],
                    "三节+真端验证未通过：" + row["finger"] + "." + row["side"])
            require(row["terminal_real_bone"],
                    "终端不是真实PMX骨：" + row["finger"] + "." + row["side"])
    require(not graph["foot"]["d_positive_weights"],
            "普通脚链D骨有正权")
    require(graph["foot"]["ordinary_chain_d_zero"] is True,
            "普通脚链D骨零权标记不符")
    require(profile["primary_clip"] in {clip["id"] for clip in profile["clips"]},
            "primary_clip不存在")
    for clip in profile["clips"]:
        require(clip["fps"] == SUPPORTED_SOURCE_FPS,
                "clip缺少显式30fps时基")
        require(clip["armature_action"] is not None, "clip缺少armature action记录")
        if clip.get("native_ik") is not None:
            _native_ik_ready(clip)
        action = clip["armature_action"]
        require(action["slots"], "clip action缺少slot")
        require(action["signature"], "clip action缺少signature")
        require(
            clip["range"] is not None and len(clip["range"]) == 2,
            "clip缺少范围",
        )
        require(
            clip["missing"]["morph_channels"] is not None,
            "morph缺项必须显式记录",
        )
        require(
            isinstance(clip.get("ik_toggle_state"), dict)
            and isinstance(clip.get("constraint_influences"), dict),
            "clip缺少IK开关/约束影响持久记录",
        )
        require(
            clip["armature_action"]["name"]
            in {record["name"] for record in profile["actions"]},
            "clip armature action未列入profile actions",
        )
        for record in clip["shape_key_actions"]:
            require(
                record["action"]["name"]
                in {action["name"] for action in profile["actions"]},
                "clip morph action未列入profile actions",
            )
    return True


def _rest_signature_from_rows(bones):
    rows = []
    for row in sorted(bones, key=lambda item: item["blender_name"]):
        rows.append(
            {
                "name": row["blender_name"],
                "matrix_local": row["matrix_local"],
                "head_local": row["head_local"],
                "tail_local": row["tail_local"],
                "parent": row["parent"],
                "connected": row["use_connect"],
                "deform": row["use_deform"],
                "inherit_rotation": row["inherit_rotation"],
                "inherit_scale": row["inherit_scale"],
            }
        )
    return canonical_hash(rows)


def _parent_signature_from_rows(bones):
    rows = [
        {"name": row["blender_name"], "parent": row["parent"]}
        for row in sorted(bones, key=lambda item: item["blender_name"])
    ]
    return canonical_hash(rows)


def _object_signature_from_records(objects):
    rows = [
        {
            "role": "ROOT",
            "name": objects["root"]["name"],
            "type": objects["root"]["type"],
            "data": objects["root"]["data"],
        },
        {
            "role": "ARMATURE",
            "name": objects["armature"]["name"],
            "type": objects["armature"]["type"],
            "data": objects["armature"]["data"],
        },
        *[
            {
                "role": "MESH",
                "name": mesh["name"],
                "type": mesh["type"],
                "data": mesh["data"],
                "mesh_signature": mesh["mesh_signature"],
            }
            for mesh in sorted(objects["meshes"], key=lambda item: item["name"])
        ],
    ]
    return canonical_hash(rows)


def _load_profile_file(profile_path):
    path = Path(bpy.path.abspath(os.fspath(profile_path))).resolve(strict=True)
    return json.loads(path.read_text(encoding="utf-8")), path


def load_profile_document(source):
    """Load a profile dict/path/Blender Text/JSON string as pure data.

    No Python evaluation is performed.  This is the stable entry UI code may
    use for the persisted profile Text or JSON file.
    """

    if isinstance(source, dict):
        return deepcopy(source)
    if hasattr(source, "as_string"):
        text = source.as_string()
    elif isinstance(source, (str, Path)):
        text = str(source)
        if not text.lstrip().startswith("{"):
            text = Path(bpy.path.abspath(text)).resolve(strict=True).read_text(encoding="utf-8")
    else:
        raise ValueError("profile必须是dict、路径、Blender Text或JSON字符串")
    document = json.loads(text)
    require(isinstance(document, dict), "profile JSON根必须为对象")
    return document


def _scene_config(scene):
    raw = scene.get(SCENE_KEY)
    require(isinstance(raw, str) and raw, "Scene缺少持久MMD来源配置")
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Scene来源配置不是有效JSON") from exc
    return config


def _uid_objects(uid):
    return [obj for obj in bpy.data.objects if obj.get(UID_KEY) == uid]


def _uid_actions(uid):
    return [action for action in bpy.data.actions if action.get(UID_KEY) == uid]


def validate_persisted_source(profile, *, scene=None, require_scene_config=True,
                              expected_rig=None, require_all_actions=True):
    """Validate a persisted profile against the real, currently loaded data."""

    profile = load_profile_document(profile)
    validate_profile_document(profile)
    scene = scene or bpy.context.scene
    objects = profile["objects"]
    config = _scene_config(scene) if require_scene_config else profile["scene_config"]
    if require_scene_config:
        require(
            canonical_hash(config) == profile["scene_config_sha256"],
            "Scene来源配置与profile不一致，拒绝被修改的配置",
        )
        require(config["source_uid"] == profile["scene_config"]["source_uid"],
                "Scene来源UID不符")
        require(config["source_blend"] == profile["scene_config"]["source_blend"],
                "Scene来源blend路径不符")
    require(config["immutable"]["rest_signature"]
            == profile["source_graph"]["rest_signature"],
            "Scene配置rest签名不符")
    require(config["immutable"]["parent_signature"]
            == profile["source_graph"]["parent_signature"],
            "Scene配置parent签名不符")
    require(config["immutable"]["object_signature"]
            == profile["source_graph"]["object_signature"],
            "Scene配置object签名不符")
    require(config["immutable"]["mapping_sha256"]
            == profile["mapping_sha256"],
            "Scene配置mapping签名不符")

    uid = profile["scene_config"]["source_uid"]
    expected_names = {
        objects["root"]["name"],
        objects["armature"]["name"],
        *[mesh["name"] for mesh in objects["meshes"]],
    }
    uid_objects = _uid_objects(uid)
    require(
        {obj.name for obj in uid_objects} == expected_names
        and len(uid_objects) == len(expected_names),
        "源UID对象集合不符或存在复制UID："
        + json.dumps([obj.name for obj in uid_objects], ensure_ascii=False),
    )
    if require_all_actions:
        expected_actions = {record["name"] for record in profile["actions"]}
        uid_actions = _uid_actions(uid)
        require(
            {action.name for action in uid_actions} == expected_actions,
            "源UID Action集合不符或存在复制UID",
        )
    else:
        primary = next(
            clip for clip in profile["clips"]
            if clip["id"] == profile["primary_clip"]
        )
        expected = {primary["armature_action"]["name"]}
        for record in primary["shape_key_actions"]:
            expected.add(record["action"]["name"])
        uid_actions = _uid_actions(uid)
        require(
            expected.issubset({action.name for action in uid_actions}),
            "追加源缺少primary Action UID",
        )

    root = bpy.data.objects.get(objects["root"]["name"])
    rig = bpy.data.objects.get(objects["armature"]["name"])
    meshes = [bpy.data.objects.get(mesh["name"]) for mesh in objects["meshes"]]
    require(root is not None, "源Root对象不存在：" + objects["root"]["name"])
    require(rig is not None, "源Armature对象不存在：" + objects["armature"]["name"])
    require(rig.type == "ARMATURE", "源Armature对象类型错误：" + rig.type)
    require(
        rig.data.name == objects["armature"]["data"],
        "源Armature data身份不符",
    )
    require(
        rig.get(UID_KEY) == uid and rig.get(ROLE_KEY) == "ARMATURE",
        "源Armature UID/角色不符",
    )
    require(
        root.get(UID_KEY) == uid and root.get(ROLE_KEY) == "ROOT",
        "源Root UID/角色不符",
    )
    require(
        root.type == objects["root"]["type"],
        "源Root类型不符",
    )
    for record, mesh in zip(objects["meshes"], meshes):
        require(mesh is not None, "源Mesh不存在：" + record["name"])
        require(mesh.type == "MESH", "源Mesh类型错误：" + record["name"])
        require(mesh.data.name == record["data"], "源Mesh data身份不符：" + record["name"])
        require(mesh.get(UID_KEY) == uid and mesh.get(ROLE_KEY) == "MESH",
                "源Mesh UID/角色不符：" + record["name"])
        require(len(mesh.data.vertices) == record["vertex_count"],
                "源Mesh顶点数变化：" + record["name"])
        require(_mesh_signature(mesh) == record["mesh_signature"],
                "源Mesh结构签名变化：" + record["name"])
    if expected_rig is not None:
        require(expected_rig == rig, "传入骨架不是profile记录的真实源骨架")
    require(expected_rig is None or expected_rig.name == rig.name,
            "expected_rig不是源Armature")
    require(
        len(rig.data.bones) == profile["source_graph"]["blender_bones"],
        "源骨架骨数变化",
    )
    require(
        parent_signature(rig) == profile["source_graph"]["parent_signature"],
        "源骨父链被修改，拒绝转换",
    )
    for bone_name, parent_name in profile["source_graph"]["parent_chain"].items():
        bone = rig.data.bones.get(bone_name)
        require(bone is not None, "源骨缺失：" + bone_name)
        require(
            (bone.parent.name if bone.parent else None) == parent_name,
            "源骨父链不符：" + bone_name,
        )
    require(
        rest_signature(rig) == profile["source_graph"]["rest_signature"],
        "源骨rest被修改，拒绝转换",
    )
    # Current profiles persist per-bone constraint metadata (including
    # owner_space/target_space). Flatten those rows so older captures remain
    # usable without weakening the immutable structure check.
    profile_constraint_count = sum(
        len(bone.get("constraints", []))
        for bone in profile["source_graph"]["bones"]
    )
    actual_constraint_count = sum(
        len(pbone.constraints) for pbone in rig.pose.bones
    )
    require(
        profile_constraint_count > 0 or actual_constraint_count == 0,
        "profile缺少逐骨约束metadata，需重新捕获源metadata",
    )
    constraint_rows = [
        {**row, "bone": bone["blender_name"]}
        for bone in profile["source_graph"]["bones"]
        for row in bone.get("constraints", [])
    ]
    missing_space = [
        row.get("bone", "?") + "::" + row.get("name", "?")
        for row in constraint_rows
        if "owner_space" not in row or "target_space" not in row
    ]
    require(
        not missing_space,
        "profile约束缺owner_space/target_space，需重新捕获源metadata："
        + json.dumps(missing_space, ensure_ascii=False),
    )
    require(
        constraint_structure_signature(rig)
        == _constraint_structure_signature_from_rows(constraint_rows),
        "源约束结构被修改，拒绝转换（influence/mute作为raw作者输入允许）",
    )
    for semantic, bone_name in profile["mapping"]["bones"].items():
        require(bone_name in rig.data.bones,
                "persisted mapping目标骨缺失：" + bone_name)
        record = profile["mapping_profile"]["semantics"].get(semantic)
        if record is not None:
            require(record["blender_bone"] == bone_name,
                    "persisted mapping语义不符：" + semantic)
    clips_to_check = (
        profile["clips"]
        if require_all_actions
        else [
            next(
                clip
                for clip in profile["clips"]
                if clip["id"] == profile["primary_clip"]
            )
        ]
    )
    for clip in clips_to_check:
        action = bpy.data.actions.get(clip["armature_action"]["name"])
        require(action is not None, "源Action不存在：" + clip["armature_action"]["name"])
        require(action.get(UID_KEY) == uid, "源Action UID不符：" + action.name)
        require(action_signature(action) == clip["armature_action"]["signature"],
                "源Action内容变化：" + action.name)
        slot = _resolve_slot(action, clip["armature_action"]["slot"])
        require(slot is not None, "源Action Slot不存在：" + clip["armature_action"]["slot"])
        for record in clip["shape_key_actions"]:
            mesh = bpy.data.objects.get(record["mesh"])
            require(mesh is not None and mesh.data.shape_keys is not None,
                    "morph目标缺失：" + record["mesh"])
            shape_action = bpy.data.actions.get(record["action"]["name"])
            require(shape_action is not None, "morph Action不存在")
            require(shape_action.get(UID_KEY) == uid, "morph Action UID不符")
            require(
                action_signature(shape_action) == record["action"]["signature"],
                "morph Action内容变化",
            )
    return {
        "ok": True,
        "scene": scene.name,
        "root": root,
        "rig": rig,
        "meshes": meshes,
        "scene_config": config,
    }


def clip_timebase(profile, clip_id, source_fps=None):
    profile = load_profile_document(profile)
    clip = next((row for row in profile["clips"] if row["id"] == clip_id), None)
    require(clip is not None, "未知source clip：" + str(clip_id))
    if source_fps is not None:
        require(
            abs(float(source_fps) - float(clip["fps"])) < 1e-9,
            "显式source fps与持久clip时基不符；不会回退Scene fps",
        )
    action = bpy.data.actions.get(clip["armature_action"]["name"])
    require(action is not None, "clip Action不存在")
    _native_ik_ready(clip, action)
    raw = action.get(TIME_KEY)
    require(isinstance(raw, str) and raw, "Action缺少可信时基记录")
    timebase = json.loads(raw)
    require(
        abs(float(timebase["fps"]) - float(clip["fps"])) < 1e-9,
        "Action时基与profile clip fps不符",
    )
    require(
        clip["armature_action"]["slot"] in timebase["slots"],
        "Action/Slot没有已记录时基；请重新导入该来源或显式重新记录",
    )
    return {
        "clip": clip,
        "fps": float(clip["fps"]),
        "action": action,
        "slot": _resolve_slot(action, clip["armature_action"]["slot"]),
        "timebase": timebase,
    }


def select_clip(profile, clip_id, *, source_fps=None, require_scene_config=True):
    profile = load_profile_document(profile)
    resolved = validate_persisted_source(
        profile,
        scene=bpy.context.scene,
        require_scene_config=require_scene_config,
    )
    ctx = clip_timebase(profile, clip_id, source_fps=source_fps)
    clip = ctx["clip"]
    root = bpy.data.objects.get(profile["objects"]["root"]["name"])
    old = _snapshot_bindings(resolved["rig"], resolved["meshes"], root=root)
    try:
        _bind_clip(resolved["rig"], resolved["meshes"], clip)
    except BaseException:
        _restore_bindings(
            old,
            resolved["rig"],
            resolved["meshes"],
            root=root,
            set_scene_time=True,
        )
        raise
    return {
        "schema": "c94.retarget.mmd_selection.v1",
        **resolved,
        **ctx,
        "root": root,
        "old_bindings": old,
        "restored": False,
    }


def restore_selected_clip(selection):
    """Restore the complete pre-selection source state from ``select_clip``."""

    require(isinstance(selection, dict), "selection必须为select_clip返回字典")
    require(selection.get("schema") == "c94.retarget.mmd_selection.v1",
            "selection schema不符")
    if selection.get("restored"):
        return {
            "ok": True,
            "restored": False,
            "already_restored": True,
            "clip": selection["clip"]["id"],
            "scene": selection["old_bindings"]["scene"],
        }
    _restore_bindings(
        selection["old_bindings"],
        selection["rig"],
        selection["meshes"],
        root=selection.get("root"),
    )
    selection["restored"] = True
    return {
        "ok": True,
        "restored": True,
        "already_restored": False,
        "clip": selection["clip"]["id"],
        "scene": selection["old_bindings"]["scene"],
    }


class SourceClipContext:
    """Stable context manager for a selected source clip and full restore."""

    def __init__(self, profile, clip_id, *, source_fps=None,
                 require_scene_config=True):
        self.profile = load_profile_document(profile)
        self.clip_id = clip_id
        self.source_fps = source_fps
        self.require_scene_config = require_scene_config
        self.selection = None

    def __enter__(self):
        self.selection = select_clip(
            self.profile,
            self.clip_id,
            source_fps=self.source_fps,
            require_scene_config=self.require_scene_config,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        self.restore()
        return False

    def restore(self):
        require(self.selection is not None, "source_clip_context尚未进入")
        return restore_selected_clip(self.selection)

    @property
    def clip(self):
        return self.selection["clip"]

    @property
    def fps(self):
        return self.selection["fps"]

    @property
    def rig(self):
        return self.selection["rig"]

    @property
    def root(self):
        return self.selection["root"]

    @property
    def meshes(self):
        return self.selection["meshes"]

    @property
    def action(self):
        return self.selection["action"]

    @property
    def slot(self):
        return self.selection["slot"]

    @property
    def scene(self):
        return bpy.context.scene


def source_clip_context(profile, clip_id, *, source_fps=None,
                        require_scene_config=True):
    """Public context manager: select a clip, then fully restore on exit."""

    return SourceClipContext(
        profile,
        clip_id,
        source_fps=source_fps,
        require_scene_config=require_scene_config,
    )


def mapping_from_profile(rig, profile, *, validate=True):
    profile = load_profile_document(profile)
    if validate:
        validate_persisted_source(
            profile,
            scene=bpy.context.scene,
            require_scene_config=False,
            expected_rig=rig,
        )
    return deepcopy(profile["mapping"]), deepcopy(profile["mapping_profile"])


def _target_weight_signatures(target, target_meshes):
    return {mesh.name: _weight_signature(mesh) for mesh in target_meshes}


def _origin_hip_world(rig, mapping):
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return vector_list(
        evaluated.matrix_world
        @ evaluated.pose.bones[mapping["bones"]["hips"]].matrix.translation
    )


def convert_from_profile(source, target, profile, *, clip_id=None, start=None,
                         end=None, output_start=1, motion="PRESERVE",
                         action_name=None, require_scene_config=True,
                         target_meshes=None, progress=None, cancelled=None,
                         sample_step=1):
    """Cold-capable conversion through the existing shared C94 core."""

    profile = load_profile_document(profile)
    validate_persisted_source(
        profile,
        scene=bpy.context.scene,
        require_scene_config=require_scene_config,
        require_all_actions=require_scene_config,
        expected_rig=source,
    )
    clip_id = clip_id or profile["primary_clip"]
    ctx = clip_timebase(profile, clip_id)
    clip = ctx["clip"]
    clip_start, clip_end = float(clip["range"][0]), float(clip["range"][1])
    effective_start = clip_start if start is None else float(start)
    effective_end = clip_end if end is None else float(end)
    require(
        clip_start <= effective_start < effective_end <= clip_end,
        "actual start/end须在源clip内且时长为正",
    )
    source_meshes = source_meshes_from_profile(profile)
    source_root = bpy.data.objects.get(profile["objects"]["root"]["name"])
    source_old = _snapshot_bindings(source, source_meshes, root=source_root)
    scene = bpy.context.scene
    from . import retarget_action as rt

    # Capture target author state before any frame_set or source binding work.
    target_state = rt.snapshot(target)
    target_old_action = (
        target.animation_data.action if target.animation_data else None
    )
    target_old_slot = (
        target.animation_data.action_slot if target.animation_data else None
    )
    target_old_action_signature = action_signature(target_old_action)
    target_rest = rest_signature(target)
    target_constraints = constraint_signature(target)
    if target_meshes is None:
        target_meshes = [
            obj
            for obj in bpy.context.scene.objects
            if obj.type == "MESH"
            and any(
                modifier.type == "ARMATURE" and modifier.object == target
                for modifier in obj.modifiers
            )
        ]
    target_weights = _target_weight_signatures(target, target_meshes)
    expected_origin = None
    response = None
    try:
        _bind_clip(source, source_meshes, clip)
        _set_scene_frame(scene, effective_start)
        bpy.context.view_layer.update()
        expected_origin = _origin_hip_world(source, profile["mapping"])
        source_action_signature = action_signature(
            source.animation_data.action if source.animation_data else None
        )
        source_rest = rest_signature(source)
        source_constraints = constraint_signature(source)

        from . import retarget_mmd as mmd

        conversion_kwargs = {
            "mapping": profile["mapping"],
            "profile": profile["mapping_profile"],
            "source_fps": float(clip["fps"]),
            "start": effective_start,
            "end": effective_end,
            "output_start": output_start,
            "motion": motion,
            "action_name": action_name,
            "progress": progress,
            "cancelled": cancelled,
        }
        core_parameters = inspect.signature(mmd.convert_mmd).parameters
        if "sample_step" in core_parameters:
            conversion_kwargs["sample_step"] = sample_step
        else:
            # The 0370 snapshot still hardcodes sample_step=1 inside
            # convert_mmd. Keep default behavior working, but never silently
            # discard an explicit non-default request.
            require(
                sample_step == 1,
                "当前mmd.convert_mmd不支持sample_step；请使用已合并该参数的"
                "共享核心，不能静默忽略",
            )
        result = mmd.convert_mmd(source, target, **conversion_kwargs)
        calibration = result["calibration_record"]
        origin = calibration["clip"]
        require(
            abs(float(origin["origin_frame"]) - effective_start) < 1e-9,
            "转换原点不是实际start",
        )
        require(origin["origin_mode"] == "ACTUAL_START",
                "转换原点模式不是ACTUAL_START")
        require(
            Vector(origin["source_hip_world"]).length >= 0
            and max(
                abs(origin["source_hip_world"][index] - expected_origin[index])
                for index in range(3)
            )
            < 1e-6,
            "转换原点source hip与转换前采样不一致",
        )
        ad = origin["source_ad_at_factory"]
        require(ad["action"] == clip["armature_action"]["name"],
                "转换工厂source AD action不符")
        require(ad["slot"] == clip["armature_action"]["slot"],
                "转换工厂source AD slot不符")
        require(ad["action_blend_type"] == "REPLACE"
                and abs(ad["action_influence"] - 1.0) < 1e-9
                and not ad["use_nla"],
                "转换工厂source AD隔离未生效")
        require(origin["source_range"] == [effective_start, effective_end],
                "转换source range不符")
        preserved = {
            "source_action_content": action_signature(
                source.animation_data.action if source.animation_data else None
            ) == source_action_signature,
            "source_rest": rest_signature(source) == source_rest,
            "source_constraints": constraint_signature(source) == source_constraints,
            "target_old_action_content": action_signature(
                target.animation_data.action if target.animation_data else None
            ) == target_old_action_signature,
            "target_old_binding": (
                target.animation_data.action if target.animation_data else None
            ) == target_old_action
            and (
                target.animation_data.action_slot if target.animation_data else None
            ) == target_old_slot,
            "target_rest": rest_signature(target) == target_rest,
            "target_constraints": constraint_signature(target) == target_constraints,
            "target_weights": _target_weight_signatures(target, target_meshes)
            == target_weights,
        }
        require(all(preserved.values()), "转换保护检查失败：" + json.dumps(preserved))
        response = {
            "ok": True,
            "result": result,
            "clip": clip,
            "expected_origin_hip_world": expected_origin,
            "origin": origin,
            "preserved": preserved,
            "target_meshes": [mesh.name for mesh in target_meshes],
            "target_protection": {
                "target_state_captured_before_any_frame_set": True,
                "restore_time_policy": "SINGLE_SCENE_TIME_THEN_SOURCE_RAW_THEN_TARGET_RAW",
                "target_raw_pose_captured": True,
                "source_raw_pose_morph_ik_restored": True,
            },
        }
        return response
    finally:
        restore_errors = []
        try:
            _restore_scene_time(source_old)
        except BaseException as exc:
            restore_errors.append(exc)
        try:
            _restore_bindings(
                source_old,
                source,
                source_meshes,
                root=source_root,
                set_scene_time=False,
            )
        except BaseException as exc:
            restore_errors.append(exc)
        try:
            rt.restore(target, target_state, set_scene_time=False)
        except BaseException as exc:
            restore_errors.append(exc)
        if response is not None:
            try:
                response["target_protection"].update(
                    {
                        "target_old_binding_restored": (
                            target.animation_data.action
                            if target.animation_data
                            else None
                        )
                        == target_old_action
                        and (
                            target.animation_data.action_slot
                            if target.animation_data
                            else None
                        )
                        == target_old_slot,
                        "target_old_action_content": action_signature(
                            bpy.data.actions.get(target_old_action.name)
                            if target_old_action
                            else None
                        )
                        == target_old_action_signature,
                    }
                )
            except BaseException as exc:
                restore_errors.append(exc)
        if restore_errors:
            active = sys.exc_info()[1]
            if active is not None:
                raise restore_errors[0] from active
            raise restore_errors[0]


def source_meshes_from_profile(profile):
    return [
        bpy.data.objects[mesh["name"]]
        for mesh in profile["objects"]["meshes"]
        if bpy.data.objects.get(mesh["name"]) is not None
    ]


def append_source_from_profile(source_blend, profile, *, scene=None,
                               clip_id=None):
    """Append exactly the persisted source objects/Action; no MMD Tools needed."""

    validate_profile_document(profile)
    scene = scene or bpy.context.scene
    objects = profile["objects"]
    clip = next(
        (row for row in profile["clips"] if row["id"] == (clip_id or profile["primary_clip"])),
        None,
    )
    require(clip is not None, "profile clip不存在")
    requested = [
        objects["root"]["name"],
        objects["armature"]["name"],
        *[mesh["name"] for mesh in objects["meshes"]],
    ]
    action_names = {clip["armature_action"]["name"]}
    for record in clip["shape_key_actions"]:
        action_names.add(record["action"]["name"])
    existing = {obj.name for obj in bpy.data.objects}
    existing_actions = {action.name for action in bpy.data.actions}
    require(
        not (set(requested) & existing),
        "追加源对象与当前文件重名，拒绝猜测：" + json.dumps(
            sorted(set(requested) & existing), ensure_ascii=False
        ),
    )
    require(
        not (action_names & existing_actions),
        "追加源Action与当前文件重名，拒绝猜测：" + json.dumps(
            sorted(action_names & existing_actions), ensure_ascii=False
        ),
    )
    source_path = Path(source_blend).resolve(strict=True)
    with bpy.data.libraries.load(str(source_path), link=False) as (from_data, to_data):
        missing = [name for name in requested if name not in from_data.objects]
        require(not missing, "源blend缺少请求对象：" + json.dumps(missing, ensure_ascii=False))
        missing_actions = [
            name for name in sorted(action_names) if name not in from_data.actions
        ]
        require(not missing_actions,
                "源blend缺少请求Action：" + json.dumps(missing_actions, ensure_ascii=False))
        to_data.objects = requested
        to_data.actions = sorted(action_names)
    loaded = [obj for obj in to_data.objects if obj is not None]
    require(len(loaded) == len(requested), "追加对象未全部取得")
    loaded_actions = [action for action in to_data.actions if action is not None]
    require(len(loaded_actions) == len(action_names), "追加Action未全部取得")
    for obj in loaded:
        if obj.name not in scene.objects:
            scene.collection.objects.link(obj)
    rig = bpy.data.objects[objects["armature"]["name"]]
    meshes = [bpy.data.objects[mesh["name"]] for mesh in objects["meshes"]]
    _bind_clip(rig, meshes, clip)
    bpy.context.view_layer.update()
    return {
        "ok": True,
        "rig": rig,
        "meshes": meshes,
        "objects": loaded,
        "actions": loaded_actions,
        "clip": clip,
    }
