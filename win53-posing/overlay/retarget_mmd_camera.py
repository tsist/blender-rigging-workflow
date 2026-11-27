"""Optional MMD/VMD camera import as native Blender camera candidates.

Scope
-----
This module builds a native Blender camera from a local VMD camera channel
without needing a PMX model.  MMD Tools 4.5.14 is used explicitly at build
time (parsing + official camera import), but the delivered camera and its
Object/Data Actions contain only native Blender data and evaluate without the
add-on being installed, imported or registered.

The source VMD is never modified.  The module refuses empty camera channels,
malformed/truncated VMD files, unsupported backend versions and output
collisions.  It does not change an existing Scene's fps, frame range, current
frame, active camera, selection or old Actions.  A library build creates a new
scene; the explicit append helper never assigns ``Scene.camera``.

Runtime dependency
------------------
MMD Tools 4.5.14, SPDX:GPL-3.0-or-later, installed at the explicit extension
path in this project.  The module imports and registers it only inside the
background build process.  No add-on code is copied into this module and no
add-on code is embedded in the delivered .blend.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import math
import os
import struct
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector


SCHEMA = "c94.retarget.mmd_camera.v1"
PROFILE_SCHEMA = "c94.retarget.mmd_camera_profile.v1"
DESCRIPTOR_SCHEMA = "c94.retarget.mmd_camera_descriptor.v1"
VALIDATION_SCHEMA = "c94.retarget.mmd_camera_validation.v1"
MIGRATION_SCHEMA = "c94.retarget.mmd_camera.migration.v1"

BACKEND_ID = "mmd_tools"
VERIFIED_BACKEND_VERSION = "4.5.14"
VERIFIED_BACKEND_LICENSE = "GPL-3.0-or-later"
SOURCE_FPS = 30.0
DEFAULT_SCALE = 0.08

DEFAULT_ADDON_PATH = ""
DEFAULT_OPENCC_WHEEL = ""

PROFILE_TEXT_NAME = "C94_MMD_CAMERA_PROFILE"
LIBRARY_SCENE_NAME = "C94_MMD_CAMERA"
DEFAULT_CAMERA_NAME = "C94_MMD_CAMERA"

EXPECTED_BACKEND_FILES = {
    "blender_manifest.toml": "da7bd4c627d8ec877c3e8e227454f350ead17d5a46219a84f87d578b763f673f",
    "LICENSE": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
    "__init__.py": "8d519537dc321e6dc5b6c54a060c2966983b358ec7f81bb281f6829d9186228f",
    "core/vmd/__init__.py": "cdf395d556219a66250a7193bb737e11535c15583b8d5c4d2170b632188f6131",
    "core/vmd/importer.py": "18983eaa5cbd7d5830613390b7a9a774ea74cf971cedaeee77128fe5cae5eb5c",
    "core/camera.py": "28c06ea0f4047dcc807972deef7bb3c1302d2a68bcd64d97a88dcf4d098fb8d8",
    "properties/camera.py": "aa9bdda214c38f418928daca52225904a1b218e6fb0bca287d6b5d90243e6ef6",
    "bpyutils.py": None,  # recorded, not pinned: not part of the camera numeric core
    "wheels/opencc_python_reimplemented-0.1.7-py2.py3-none-any.whl": (
        "41b3b92943c7bed291f448e9c7fad4b577c8c2eae30fcfe5a74edf8818493aa6"
    ),
}


class CameraImportError(RuntimeError):
    """Base error for camera import/validation failures."""


class CameraInputError(CameraImportError):
    """The VMD or request is invalid; no camera was created."""


class CameraUnavailableError(CameraImportError):
    """The VMD is valid but has no camera channel; no camera was created."""


class CameraBackendError(CameraImportError):
    """The explicit MMD Tools backend is missing, unsupported or inconsistent."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(document) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(document) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def profile_sha256(document) -> str:
    clone = dict(document)
    clone.pop("profile_sha256", None)
    return canonical_sha256(clone)


def _file_record(path: str) -> dict:
    return {
        "file": os.path.abspath(path),
        "bytes": os.path.getsize(path),
        "sha256": sha256_file(path),
    }


def _resolve_file(path: str, suffix: str | None = None) -> str:
    if not path or not isinstance(path, str):
        raise CameraInputError("输入路径必须是非空字符串")
    resolved = os.path.abspath(os.path.normpath(path))
    if not os.path.isfile(resolved):
        raise CameraInputError(f"输入文件不存在或不是文件：{resolved}")
    if suffix and not resolved.lower().endswith(suffix.lower()):
        raise CameraInputError(f"输入文件扩展名不是 {suffix}：{resolved}")
    return resolved


def _resolve_dir(path: str) -> str:
    if not path or not isinstance(path, str):
        raise CameraInputError("输出目录必须是非空字符串")
    resolved = os.path.abspath(os.path.normpath(path))
    if not os.path.isdir(resolved):
        raise CameraInputError(f"输出目录不存在：{resolved}")
    return resolved


def _path_identity(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _paths_alias(left: str, right: str) -> bool:
    if _path_identity(left) == _path_identity(right):
        return True
    try:
        return os.path.exists(left) and os.path.exists(right) and os.path.samefile(left, right)
    except OSError:
        return False


def _reject_output_collisions(outputs, inputs):
    for label, path in outputs:
        for input_path in inputs:
            if _paths_alias(path, input_path):
                raise CameraInputError(
                    f"输出{label}不得指向输入文件（含别名/硬链接）：{path}"
                )
    for index, (left_label, left) in enumerate(outputs):
        for right_label, right in outputs[index + 1:]:
            if _paths_alias(left, right):
                raise CameraInputError(
                    f"输出{left_label}与{right_label}路径冲突：{left}"
                )


def _registration_state() -> dict:
    state = {}
    try:
        import addon_utils

        for module_name in ("bl_ext.blender_org.mmd_tools", "mmd_tools"):
            try:
                state[module_name] = list(addon_utils.check(module_name))
            except Exception as exc:  # noqa: BLE001 - evidence only
                state[module_name] = {"error": repr(exc)}
    except Exception as exc:  # noqa: BLE001
        state["addon_utils"] = {"error": repr(exc)}
    state["mmd_tools_in_sys_modules"] = "mmd_tools" in sys.modules
    state["mmd_camera_rna_present"] = hasattr(bpy.types.Object, "mmd_camera")
    state["mmd_type_rna_present"] = hasattr(bpy.types.Object, "mmd_type")
    module = sys.modules.get("mmd_tools")
    state["mmd_tools_module_file"] = getattr(module, "__file__", None)
    state["mmd_tools_version"] = getattr(module, "MMD_TOOLS_VERSION", None)
    return state


def inspect_backend(addon_path: str | None = None, opencc_wheel: str | None = None) -> dict:
    """Inspect the explicit installed MMD Tools without importing/registering it.

    The returned record is evidence only; it does not modify Blender data.
    """

    addon_path = os.path.abspath(addon_path or DEFAULT_ADDON_PATH)
    wheel_path = os.path.abspath(opencc_wheel or DEFAULT_OPENCC_WHEEL)
    result = {
        "schema": "c94.retarget.mmd_camera_backend.v1",
        "backend_id": BACKEND_ID,
        "extension_id": "bl_ext.blender_org.mmd_tools",
        "verified_version": VERIFIED_BACKEND_VERSION,
        "verified_license": VERIFIED_BACKEND_LICENSE,
        "only_verified_backend": True,
        "self_contained_vmd_parser": False,
        "implicit_install_enable": False,
        "addon_path": addon_path,
        "opencc_wheel": wheel_path,
        "registration_state": _registration_state(),
        "files": {},
    }
    manifest_path = os.path.join(addon_path, "blender_manifest.toml")
    result["manifest"] = os.path.abspath(manifest_path)
    result["available"] = os.path.isdir(addon_path) and os.path.isfile(manifest_path)
    if not result["available"]:
        result.update(
            {
                "verified": False,
                "version": None,
                "license": [],
                "reason": "显式 MMD Tools 扩展目录或 blender_manifest.toml 不存在",
            }
        )
        return result

    import tomllib

    with open(manifest_path, "rb") as fh:
        manifest = tomllib.load(fh)
    result["manifest_id"] = manifest.get("id")
    result["version"] = manifest.get("version")
    result["name"] = manifest.get("name")
    result["maintainer"] = manifest.get("maintainer")
    result["license"] = list(manifest.get("license", []))
    result["copyright"] = list(manifest.get("copyright", []))

    file_records = {}
    for relative_path, expected_sha in EXPECTED_BACKEND_FILES.items():
        absolute = os.path.join(addon_path, *relative_path.split("/"))
        if not os.path.isfile(absolute):
            file_records[relative_path] = {
                "file": os.path.abspath(absolute),
                "exists": False,
                "expected_sha256": expected_sha,
            }
            continue
        record = _file_record(absolute)
        record["exists"] = True
        record["expected_sha256"] = expected_sha
        record["matches_expected"] = expected_sha is None or record["sha256"] == expected_sha
        file_records[relative_path] = record
    result["files"] = file_records

    wheel_record = None
    if os.path.isfile(wheel_path):
        wheel_record = _file_record(wheel_path)
        wheel_record["license_note"] = "Apache License (wheel metadata)"
    result["wheel"] = wheel_record

    identity_payload = {
        "version": result["version"],
        "license": result["license"],
        "files": {
            name: rec.get("sha256")
            for name, rec in file_records.items()
            if rec.get("exists")
        },
        "wheel": wheel_record["sha256"] if wheel_record else None,
    }
    result["source_identity_sha256"] = canonical_sha256(identity_payload)
    result["checks"] = {
        "manifest_id_is_mmd_tools": result.get("manifest_id") == "mmd_tools",
        "version_is_verified": result["version"] == VERIFIED_BACKEND_VERSION,
        "license_is_verified": any(
            value in (VERIFIED_BACKEND_LICENSE, f"SPDX:{VERIFIED_BACKEND_LICENSE}")
            for value in result["license"]
        ),
        "all_required_files_present": all(
            rec.get("exists") for rec in file_records.values()
        ),
        "all_pinned_hashes_match": all(
            rec.get("matches_expected", False)
            for rec in file_records.values()
            if rec.get("expected_sha256") is not None
        ),
        "wheel_present": wheel_record is not None,
    }
    result["verified"] = all(result["checks"].values())
    if not result["verified"]:
        result["reason"] = "MMD Tools 版本/许可/关键文件哈希与已验后端不符"
    return result


def _ensure_opencc(wheel_path: str, target_dir: str) -> str:
    marker = os.path.join(target_dir, "opencc", "opencc.py")
    if os.path.isfile(marker):
        return target_dir
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(wheel_path) as zf:
        zf.extractall(target_dir)
    if not os.path.isfile(marker):
        raise CameraBackendError(f"OpenCC wheel 解包后缺少 opencc/opencc.py：{target_dir}")
    return target_dir


def _load_vmd_parser(addon_path: str | None = None):
    """Load the installed official VMD reader without importing the add-on."""

    addon_path = os.path.abspath(addon_path or DEFAULT_ADDON_PATH)
    parser_path = os.path.join(addon_path, "core", "vmd", "__init__.py")
    if not os.path.isfile(parser_path):
        raise CameraBackendError(f"MMD Tools VMD parser 不存在：{parser_path}")
    module_name = "c94_mmd_tools_vmd_parser"
    existing = sys.modules.get(module_name)
    if existing is not None and os.path.abspath(getattr(existing, "__file__", "")) == parser_path:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, parser_path)
    if spec is None or spec.loader is None:
        raise CameraBackendError(f"无法加载 MMD Tools VMD parser：{parser_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[module_name] = module
    module._c94_source_file = parser_path
    module._c94_source_sha256 = sha256_file(parser_path)
    return module


def _import_mmd_tools_camera_parsers():
    from mmd_tools.core import vmd as vmd_module
    from mmd_tools.core.camera import MMDCamera
    from mmd_tools.core.vmd.importer import VMDImporter
    return vmd_module, MMDCamera, VMDImporter


def _load_mmd_tools_runtime_classes(mmd_tools, registered_by_us, *, importer=None,
                                    require_camera_rna=True, has_camera_rna=None):
    """Finish post-registration setup; roll back only our own registration."""
    importer = importer or _import_mmd_tools_camera_parsers
    has_camera_rna = has_camera_rna or (
        lambda: hasattr(bpy.types.Object, "mmd_camera"))
    try:
        if require_camera_rna and not has_camera_rna():
            raise CameraBackendError(
                "MMD Tools注册后仍缺少 Object.mmd_camera RNA")
        return importer()
    except BaseException as exc:
        if registered_by_us:
            try:
                mmd_tools.unregister()
            except BaseException as unregister_exc:  # noqa: BLE001 - preserve original
                try:
                    exc.add_note(
                        "MMD Tools parser导入失败后本函数注册回滚失败："
                        + repr(unregister_exc)
                    )
                except AttributeError:
                    pass
        raise


def _load_mmd_tools(
    addon_path: str,
    opencc_wheel: str,
    runtime_deps_dir: str,
    *,
    require_verified: bool = True,
):
    inspection = inspect_backend(addon_path=addon_path, opencc_wheel=opencc_wheel)
    if require_verified and not inspection["verified"]:
        raise CameraBackendError(
            "MMD Tools 后端未通过版本/许可/关键文件校验："
            + json.dumps(inspection.get("checks", {}), ensure_ascii=False, sort_keys=True)
        )
    _ensure_opencc(os.path.abspath(opencc_wheel), os.path.abspath(runtime_deps_dir))
    sys.dont_write_bytecode = True
    extension_parent = os.path.dirname(os.path.abspath(addon_path))
    for path in (runtime_deps_dir, extension_parent):
        path = os.path.abspath(path)
        if path not in sys.path:
            sys.path.insert(0, path)
    import mmd_tools

    module_file = os.path.abspath(getattr(mmd_tools, "__file__", ""))
    if not module_file.startswith(os.path.abspath(addon_path) + os.sep):
        raise CameraBackendError(
            f"已导入的 mmd_tools 不是显式后端：{module_file}；期望 {addon_path}"
        )
    registered_by_us = False
    if not hasattr(bpy.types.Object, "mmd_camera"):
        mmd_tools.register()
        registered_by_us = True

    vmd_module, MMDCamera, VMDImporter = _load_mmd_tools_runtime_classes(
        mmd_tools, registered_by_us)

    return {
        "module": mmd_tools,
        "version": getattr(mmd_tools, "MMD_TOOLS_VERSION", None),
        "registered_by_us": registered_by_us,
        "MMDCamera": MMDCamera,
        "VMDImporter": VMDImporter,
        "vmd": vmd_module,
        "inspection": inspection,
    }


class _MMDToolsRuntime:
    def __init__(self, payload: dict):
        self.payload = payload
        self.unregistered = False
        self.unregister_attempted = False
        self.unregister_error = None
        self.registration_left_in_place = False

    def unregister(self):
        if self.unregister_attempted:
            return self.unregistered
        self.unregister_attempted = True
        if not self.payload.get("registered_by_us"):
            # External registration is not ours to remove; lifecycle is
            # complete for this build but the external state is left intact.
            self.registration_left_in_place = True
            self.unregistered = True
            return True
        try:
            self.payload["module"].unregister()
        except BaseException as exc:  # noqa: BLE001 - recorded and blocks publish
            self.unregister_error = repr(exc)
            self.unregistered = False
            return False
        self.unregistered = True
        return True


def _require_runtime_unregistered(runtime_handle):
    """A failed rollback of our own registration must block final publication."""
    if runtime_handle is None:
        return
    if (runtime_handle.payload.get("registered_by_us")
            and not runtime_handle.unregistered):
        raise CameraBackendError(
            "MMD Tools本次注册回滚失败，拒绝写最终library/profile/descriptor；"
            "保留诊断，外部注册不卸载："
            + str(runtime_handle.unregister_error or "unknown unregister failure")
        )


def _read_exact(fh, size: int, label: str) -> bytes:
    data = fh.read(size)
    if len(data) != size:
        raise CameraInputError(
            f"VMD 截断：{label} 需要 {size} 字节，实际只有 {len(data)} 字节"
        )
    return data


def _skip_exact(fh, size: int, label: str):
    data = fh.read(size)
    if len(data) != size:
        raise CameraInputError(
            f"VMD 截断：{label} 需要 {size} 字节，实际只有 {len(data)} 字节"
        )


def _finite(*values) -> bool:
    return all(math.isfinite(float(v)) for v in values)


def validate_vmd_structure(path: str) -> dict:
    """Validate the VMD container structure and return camera records.

    This is a guard for the official parser (which intentionally swallows
    ``struct.error`` for trailing optional sections).  Camera values are still
    read through the installed official parser afterwards.
    """

    path = _resolve_file(path, ".vmd")
    size = os.path.getsize(path)
    result = {
        "file": path,
        "bytes": size,
        "header_ok": False,
        "header_model_name": None,
        "bone_keys": 0,
        "morph_keys": 0,
        "camera_keys": 0,
        "camera_records": [],
        "light_keys": 0,
        "self_shadow_keys": 0,
        "property_keys": 0,
        "trailing_bytes": 0,
        "trailing_all_zero": None,
    }
    with open(path, "rb") as fh:
        signature = _read_exact(fh, 30, "header signature")
        if not signature.startswith(b"Vocaloid Motion Data 0002"):
            raise CameraInputError(f"不是 VMD 签名：{signature!r}")
        result["header_ok"] = True
        raw_model = _read_exact(fh, 20, "header model name")
        result["header_model_name"] = raw_model.split(b"\x00", 1)[0].decode(
            "cp932", errors="replace"
        )

        (bone_count,) = struct.unpack("<L", _read_exact(fh, 4, "bone count"))
        if bone_count > 5_000_000:
            raise CameraInputError(f"VMD 骨关键帧数量异常：{bone_count}")
        result["bone_keys"] = int(bone_count)
        for index in range(bone_count):
            _skip_exact(fh, 111, f"bone key {index}")

        (morph_count,) = struct.unpack("<L", _read_exact(fh, 4, "morph count"))
        if morph_count > 5_000_000:
            raise CameraInputError(f"VMD morph 关键帧数量异常：{morph_count}")
        result["morph_keys"] = int(morph_count)
        for index in range(morph_count):
            _skip_exact(fh, 23, f"morph key {index}")

        (camera_count,) = struct.unpack("<L", _read_exact(fh, 4, "camera count"))
        if camera_count > 1_000_000:
            raise CameraInputError(f"VMD 相机关键帧数量异常：{camera_count}")
        result["camera_keys"] = int(camera_count)
        seen_frames = set()
        for index in range(camera_count):
            raw = _read_exact(fh, 61, f"camera key {index}")
            (frame_number,) = struct.unpack_from("<L", raw, 0)
            (distance,) = struct.unpack_from("<f", raw, 4)
            location = struct.unpack_from("<fff", raw, 8)
            rotation = struct.unpack_from("<fff", raw, 20)
            interp = struct.unpack_from("<24b", raw, 32)
            (angle,) = struct.unpack_from("<L", raw, 56)
            (persp_raw,) = struct.unpack_from("<b", raw, 60)
            if frame_number in seen_frames:
                raise CameraInputError(f"VMD 相机关键帧重复：frame {frame_number}")
            seen_frames.add(frame_number)
            if not _finite(distance, *location, *rotation, angle):
                raise CameraInputError(f"VMD 相机关键帧含非有限数值：frame {frame_number}")
            if angle < 1 or angle > 180:
                raise CameraInputError(
                    f"VMD 相机 FOV angle 超出 1..180 度：frame {frame_number} angle={angle}"
                )
            if persp_raw not in (0, 1):
                raise CameraInputError(
                    f"VMD 相机 perspective 标记非法：frame {frame_number} value={persp_raw}"
                )
            result["camera_records"].append(
                {
                    "frame": int(frame_number),
                    "distance": float(distance),
                    "location": [float(v) for v in location],
                    "rotation_rad": [float(v) for v in rotation],
                    "interp": [int(v) for v in interp],
                    "angle_deg": int(angle),
                    "persp": bool(persp_raw == 0),
                }
            )

        # Optional sections.  They are not needed for camera import, but
        # walking them distinguishes a complete file from a truncated one.
        remaining = fh.read()
        position = 0

        def take(count: int, label: str) -> bytes:
            nonlocal position
            if position + count > len(remaining):
                raise CameraInputError(
                    f"VMD 截断：{label} 需要 {count} 字节，剩余 {len(remaining) - position} 字节"
                )
            chunk = remaining[position : position + count]
            position += count
            return chunk

        if len(remaining) >= 4:
            (light_count,) = struct.unpack("<L", take(4, "light count"))
            if light_count > 1_000_000:
                raise CameraInputError(f"VMD 灯光关键帧数量异常：{light_count}")
            result["light_keys"] = int(light_count)
            for index in range(light_count):
                take(28, f"light key {index}")

            if len(remaining) - position >= 4:
                (self_shadow_count,) = struct.unpack("<L", take(4, "self shadow count"))
                if self_shadow_count > 1_000_000:
                    raise CameraInputError(
                        f"VMD 自阴影关键帧数量异常：{self_shadow_count}"
                    )
                result["self_shadow_keys"] = int(self_shadow_count)
                for index in range(self_shadow_count):
                    take(9, f"self shadow key {index}")

                if len(remaining) - position >= 4:
                    (property_count,) = struct.unpack("<L", take(4, "property count"))
                    if property_count > 1_000_000:
                        raise CameraInputError(
                            f"VMD 属性关键帧数量异常：{property_count}"
                        )
                    result["property_keys"] = int(property_count)
                    for index in range(property_count):
                        header = take(9, f"property key {index} header")
                        (ik_count,) = struct.unpack_from("<L", header, 5)
                        if ik_count > 1_000_000:
                            raise CameraInputError(
                                f"VMD 属性 IK 数量异常：frame {index} count={ik_count}"
                            )
                        take(21 * ik_count, f"property key {index} ik states")

        result["trailing_bytes"] = len(remaining) - position
        if result["trailing_bytes"]:
            trailing = remaining[position:]
            result["trailing_all_zero"] = all(byte == 0 for byte in trailing)
        result["camera_records"].sort(key=lambda record: record["frame"])
    return result


def _official_camera_keys(parser_module, vmd_path: str) -> tuple[dict, list[dict]]:
    vmd_file = parser_module.File()
    try:
        vmd_file.load(filepath=vmd_path)
    except Exception as exc:  # noqa: BLE001
        raise CameraInputError(f"MMD Tools VMD parser 拒绝文件：{exc!r}") from exc
    header = vmd_file.header
    keys = []
    for key in vmd_file.cameraAnimation:
        keys.append(
            {
                "frame": int(key.frame_number),
                "distance": float(key.distance),
                "location": [float(v) for v in key.location],
                "rotation_rad": [float(v) for v in key.rotation],
                "interp": [int(v) for v in key.interp],
                "angle_deg": int(key.angle),
                "persp": bool(key.persp),
            }
        )
    keys.sort(key=lambda record: record["frame"])
    header_record = {
        "signature": bytes(getattr(header, "signature", b"") or b"").decode(
            "ascii", errors="replace"
        ),
        "model_name": getattr(header, "model_name", None),
    }
    return header_record, keys


def _interp_groups(interp: list[int]) -> dict:
    """Return the six camera VMD interpolation byte groups.

    The installed MMD Tools layout is [x1, x2, y1, y2] per group, in the order
    X location, Y location, Z location, rotation, distance, FOV.
    """

    if len(interp) != 24:
        raise CameraInputError(f"VMD 相机插值块必须是 24 字节，实际 {len(interp)}")
    names = ("x", "y", "z", "rotation", "distance", "fov")
    return {
        name: {
            "bytes": interp[index * 4 : index * 4 + 4],
            "x1_x2_y1_y2": interp[index * 4 : index * 4 + 4],
            "linear_default": interp[index * 4 : index * 4 + 4] == [20, 107, 20, 107],
        }
        for index, name in enumerate(names)
    }


def inspect_vmd_camera(
    vmd: str,
    *,
    addon_path: str | None = None,
    opencc_wheel: str | None = None,
) -> dict:
    """Read-only camera-channel inspection using the installed official parser."""

    vmd = _resolve_file(vmd, ".vmd")
    structure = validate_vmd_structure(vmd)
    parser = _load_vmd_parser(addon_path)
    header, official_keys = _official_camera_keys(parser, vmd)
    if len(official_keys) != structure["camera_keys"]:
        raise CameraInputError(
            "MMD Tools parser 相机数量与 VMD 容器结构不一致："
            f"{len(official_keys)} != {structure['camera_keys']}"
        )
    structure_keys = structure["camera_records"]
    if [
        (key["frame"], key["angle_deg"], key["persp"]) for key in official_keys
    ] != [
        (key["frame"], key["angle_deg"], key["persp"]) for key in structure_keys
    ]:
        raise CameraInputError("MMD Tools parser 相机关键帧与 VMD 容器结构不一致")
    for key in official_keys:
        key["interp_groups"] = _interp_groups(key["interp"])
        key["rotation_deg"] = [math.degrees(v) for v in key["rotation_rad"]]
    for key in structure_keys:
        key["interp_groups"] = _interp_groups(key["interp"])
        key["rotation_deg"] = [math.degrees(v) for v in key["rotation_rad"]]
    persp_events = []
    previous = None
    for key in official_keys:
        if previous is None or key["persp"] != previous:
            persp_events.append({"frame": key["frame"], "persp": key["persp"]})
        previous = key["persp"]
    cuts = []
    for left, right in zip(official_keys, official_keys[1:]):
        if right["frame"] - left["frame"] <= 1:
            cuts.append(
                {
                    "from_frame": left["frame"],
                    "to_frame": right["frame"],
                    "gap": right["frame"] - left["frame"],
                }
            )
    return {
        "schema": "c94.retarget.mmd_camera_vmd_inspection.v1",
        "file": _file_record(vmd),
        "header": header,
        "structure": {
            "header_ok": structure["header_ok"],
            "bone_keys": structure["bone_keys"],
            "morph_keys": structure["morph_keys"],
            "camera_keys": structure["camera_keys"],
            "light_keys": structure["light_keys"],
            "self_shadow_keys": structure["self_shadow_keys"],
            "property_keys": structure["property_keys"],
            "trailing_bytes": structure["trailing_bytes"],
            "trailing_all_zero": structure["trailing_all_zero"],
        },
        "parser": {
            "source_file": getattr(parser, "_c94_source_file", None),
            "source_sha256": getattr(parser, "_c94_source_sha256", None),
            "official_module": "mmd_tools.core.vmd",
            "addon_path": os.path.abspath(addon_path or DEFAULT_ADDON_PATH),
        },
        "camera_keys": official_keys,
        "camera_key_count": len(official_keys),
        "camera_frame_range": (
            [official_keys[0]["frame"], official_keys[-1]["frame"]]
            if official_keys
            else None
        ),
        "perspective_events": persp_events,
        "source_cuts": cuts,
        "has_camera": bool(official_keys),
    }


def _effective_fps(scene: bpy.types.Scene) -> float:
    fps = float(scene.render.fps)
    base = float(scene.render.fps_base) if scene.render.fps_base else 1.0
    return fps / base


def _set_scene_fps(scene: bpy.types.Scene, fps: float) -> dict:
    fps = float(fps)
    if not math.isfinite(fps) or fps <= 0:
        raise CameraInputError(f"target fps 必须是有限正数：{fps}")
    rounded = int(round(fps))
    if abs(fps - rounded) < 1e-9:
        scene.render.fps = rounded
        scene.render.fps_base = 1.0
    else:
        # Blender stores fps as int / fps_base.  Use a 1000-based rational
        # representation and verify it below.
        base = 1000.0
        numerator = int(round(fps * base))
        scene.render.fps = numerator
        scene.render.fps_base = base
    actual = _effective_fps(scene)
    if abs(actual - fps) > 1e-6:
        raise CameraInputError(
            f"无法在 Blender 中精确表示 target fps={fps}；实际 {actual}"
        )
    return {"fps": actual, "render_fps": int(scene.render.fps), "fps_base": float(scene.render.fps_base)}


def _map_source_to_target(
    source_frame: float,
    *,
    source_fps: float,
    target_fps: float,
    target_frame_origin: float,
    source_time_origin: float,
) -> float:
    return target_frame_origin + (
        (float(source_frame) - source_time_origin) * target_fps / source_fps
    )


def _map_target_to_source(
    target_frame: float,
    *,
    source_fps: float,
    target_fps: float,
    target_frame_origin: float,
    source_time_origin: float,
) -> float:
    return source_time_origin + (
        (float(target_frame) - target_frame_origin) * source_fps / target_fps
    )


def _unique_frames(values: list[float], tolerance: float = 1e-7) -> list[float]:
    result = []
    for value in sorted(float(v) for v in values):
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
    return result


def _build_sample_plan(
    keys: list[dict],
    *,
    source_fps: float,
    target_fps: float,
    target_frame_origin: float,
    source_time_origin: float,
) -> dict:
    if not keys:
        raise CameraUnavailableError("VMD camera 通道为空（0 keys）")
    if source_fps != SOURCE_FPS:
        raise CameraInputError(
            f"VMD camera 源时基必须显式 30fps；收到 {source_fps!r}（不回退 Scene fps）"
        )
    if not math.isfinite(target_fps) or target_fps <= 0:
        raise CameraInputError(f"target fps 必须是有限正数：{target_fps!r}")
    if not math.isfinite(target_frame_origin):
        raise CameraInputError("target_frame_origin 必须是有限数值")
    if not math.isfinite(source_time_origin):
        raise CameraInputError("source_time_origin 必须是有限数值")
    if keys[0]["frame"] < source_time_origin:
        raise CameraInputError(
            f"source_time_origin={source_time_origin} 晚于首个相机键 {keys[0]['frame']}"
        )

    key_targets = [
        _map_source_to_target(
            key["frame"],
            source_fps=source_fps,
            target_fps=target_fps,
            target_frame_origin=target_frame_origin,
            source_time_origin=source_time_origin,
        )
        for key in keys
    ]
    key_midpoints = []
    for left, right in zip(keys, keys[1:]):
        source_mid = (left["frame"] + right["frame"]) / 2.0
        key_midpoints.append(
            {
                "left_source_frame": left["frame"],
                "right_source_frame": right["frame"],
                "source_frame": source_mid,
                "target_frame": _map_source_to_target(
                    source_mid,
                    source_fps=source_fps,
                    target_fps=target_fps,
                    target_frame_origin=target_frame_origin,
                    source_time_origin=source_time_origin,
                ),
            }
        )
    first_target = key_targets[0]
    last_target = key_targets[-1]
    integer_frames = []
    start = int(math.ceil(first_target - 1e-9))
    end = int(math.floor(last_target + 1e-9))
    if end >= start:
        integer_frames = [float(value) for value in range(start, end + 1)]
    all_samples = _unique_frames(
        integer_frames
        + key_targets
        + [item["target_frame"] for item in key_midpoints]
    )
    return {
        "schema": "c94.retarget.mmd_camera_mapping.v1",
        "mode": "SECONDS",
        "formula": (
            "target = target_frame_origin + "
            "(source - source_time_origin) * target_fps / source_fps"
        ),
        "source_fps": float(source_fps),
        "target_fps": float(target_fps),
        "target_frame_origin": float(target_frame_origin),
        "source_time_origin": float(source_time_origin),
        "source_key_frames": [key["frame"] for key in keys],
        "source_key_target_frames": key_targets,
        "source_key_midpoints": key_midpoints,
        "target_first": first_target,
        "target_last": last_target,
        "target_range_integer": [
            int(math.floor(first_target + 1e-9)),
            int(math.ceil(last_target - 1e-9)),
        ],
        "sample_frames": all_samples,
        "sample_count": len(all_samples),
        "integer_sample_count": len(integer_frames),
        "key_target_frames": _unique_frames(key_targets),
        "mid_target_frames": _unique_frames(
            [item["target_frame"] for item in key_midpoints]
        ),
    }


def _frame_set_exact(scene: bpy.types.Scene, frame: float):
    frame = float(frame)
    if not math.isfinite(frame):
        raise CameraInputError(f"非法求值帧：{frame}")
    base = int(math.floor(frame + 1e-9))
    subframe = frame - base
    if subframe < -1e-9:
        subframe = 0.0
    if subframe >= 1.0 - 1e-9:
        base += 1
        subframe = 0.0
    scene.frame_set(base, subframe=float(subframe))


def _scene_snapshot() -> dict:
    scenes = {}
    for scene in bpy.data.scenes:
        scenes[scene.name] = {
            "fps": _effective_fps(scene),
            "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "frame_current": int(scene.frame_current),
            "frame_subframe": float(scene.frame_subframe),
            "camera": scene.camera.name if scene.camera else None,
            "objects": sorted(obj.name for obj in scene.objects),
            "collections": sorted(collection.name for collection in scene.collection.children),
        }
    selected = sorted(obj.name for obj in bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    return {
        "scenes": scenes,
        "selected_objects": selected,
        "active_object": active.name if active else None,
        "active_scene": bpy.context.scene.name if bpy.context.scene else None,
    }


def _restore_selection(selected_objects, active_object):
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in selected_objects:
        try:
            obj.select_set(True)
        except ReferenceError:
            pass
    try:
        bpy.context.view_layer.objects.active = active_object
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def _scene_context(scene: bpy.types.Scene):
    window = bpy.context.window
    previous_scene = window.scene if window is not None else bpy.context.scene
    if window is not None:
        window.scene = scene
    view_layer = scene.view_layers[0] if len(scene.view_layers) else None
    try:
        if view_layer is not None:
            with bpy.context.temp_override(
                scene=scene, collection=scene.collection, view_layer=view_layer
            ):
                yield
        else:
            yield
    finally:
        if window is not None and previous_scene is not None:
            window.scene = previous_scene


def _new_temp_scene(source_last_frame: int) -> bpy.types.Scene:
    name = f"C94_MMD_CAMERA_TMP_{uuid.uuid4().hex[:8]}"
    scene = bpy.data.scenes.new(name)
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    scene.frame_start = 0
    scene.frame_end = max(120, int(math.ceil(source_last_frame)) + 10)
    scene.frame_set(0)
    return scene


def _create_official_camera(
    temp_scene: bpy.types.Scene,
    runtime: dict,
    vmd_path: str,
    *,
    scale: float,
    action_name: str,
    detect_camera_changes: bool,
):
    MMDCamera = runtime["MMDCamera"]
    VMDImporter = runtime["VMDImporter"]
    cam_data = bpy.data.cameras.new("C94_MMD_CAMERA_IMPORT_DATA")
    cam_obj = bpy.data.objects.new("C94_MMD_CAMERA_IMPORT", cam_data)
    temp_scene.collection.objects.link(cam_obj)
    with _scene_context(temp_scene):
        rig = MMDCamera.convertToMMDCamera(cam_obj, scale)
        root = rig.object()
        camera = rig.camera()
        importer = VMDImporter(
            vmd_path,
            scale=scale,
            convert_mmd_camera=True,
            convert_mmd_light=False,
            frame_margin=0,
            use_nla=False,
            detect_camera_changes=detect_camera_changes,
        )
        importer.assign(camera, action_name=action_name)
    return root, camera


def _evaluated_camera_snapshot(scene: bpy.types.Scene, camera: bpy.types.Object) -> dict:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = camera.evaluated_get(depsgraph)
    matrix_world = evaluated.matrix_world.copy()
    data = evaluated.data
    lens = float(data.lens)
    camera_type = str(data.type)
    ortho_scale = float(data.ortho_scale)
    sensor_width = float(data.sensor_width)
    sensor_height = float(data.sensor_height)
    sensor_fit = str(data.sensor_fit)
    root = evaluated.parent
    root_location = list(root.location) if root else None
    root_rotation = list(root.rotation_euler) if root else None
    root_rotation_mode = root.rotation_mode if root else None
    root_angle = None
    root_persp = None
    if root is not None and hasattr(root, "mmd_camera"):
        try:
            root_angle = float(root.mmd_camera.angle)
            root_persp = bool(root.mmd_camera.is_perspective)
        except Exception:  # noqa: BLE001
            root_angle = None
            root_persp = None
    quat = matrix_world.to_quaternion().normalized()
    location = matrix_world.translation.copy()
    scale_vec = matrix_world.to_scale()
    view = -(matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0)))
    view.normalize()
    target_vec = Vector(root_location) - location if root_location is not None else None
    target_distance = target_vec.length if target_vec is not None else None
    look_dot = None
    if target_vec is not None and target_distance and target_distance > 0:
        look_dot = view.dot(target_vec.normalized())
    fov_vertical = 2.0 * math.atan(sensor_height / (2.0 * lens)) if lens > 0 else None
    fov_horizontal = 2.0 * math.atan(sensor_width / (2.0 * lens)) if lens > 0 else None
    return {
        "frame": int(scene.frame_current),
        "subframe": float(scene.frame_subframe),
        "matrix_world": [list(row) for row in matrix_world],
        "location": [float(v) for v in location],
        "quaternion_wxyz": [float(v) for v in quat],
        "scale": [float(v) for v in scale_vec],
        "lens": lens,
        "type": camera_type,
        "ortho_scale": ortho_scale,
        "sensor_fit": sensor_fit,
        "sensor_width": sensor_width,
        "sensor_height": sensor_height,
        "fov_vertical_rad": fov_vertical,
        "fov_vertical_deg": math.degrees(fov_vertical) if fov_vertical is not None else None,
        "fov_horizontal_rad": fov_horizontal,
        "fov_horizontal_deg": math.degrees(fov_horizontal) if fov_horizontal is not None else None,
        "camera_local_y": float(evaluated.location.y),
        "root_location": root_location,
        "root_rotation_euler_rad": root_rotation,
        "root_rotation_euler_deg": [math.degrees(v) for v in root_rotation] if root_rotation else None,
        "root_rotation_mode": root_rotation_mode,
        "root_mmd_angle_rad": root_angle,
        "root_mmd_angle_deg": math.degrees(root_angle) if root_angle is not None else None,
        "root_persp": root_persp,
        "view_direction": [float(v) for v in view],
        "target_vector": [float(v) for v in target_vec] if target_vec is not None else None,
        "target_distance": float(target_distance) if target_distance is not None else None,
        "look_dot": float(look_dot) if look_dot is not None else None,
    }


def _sample_official_camera(
    temp_scene: bpy.types.Scene,
    camera: bpy.types.Object,
    sample_frames: list[float],
    mapping: dict,
    cut_intervals: list[tuple[float, float]] | None = None,
) -> tuple[list[dict], list[dict]]:
    cut_intervals = list(cut_intervals or [])
    samples = []
    previous_quat = None
    for target_frame in sample_frames:
        source_frame = _map_target_to_source(
            target_frame,
            source_fps=mapping["source_fps"],
            target_fps=mapping["target_fps"],
            target_frame_origin=mapping["target_frame_origin"],
            source_time_origin=mapping["source_time_origin"],
        )
        _frame_set_exact(temp_scene, source_frame)
        record = _evaluated_camera_snapshot(temp_scene, camera)
        quat = Quaternion(record["quaternion_wxyz"])
        if previous_quat is not None and quat.dot(previous_quat) < 0:
            quat.negate()
        previous_quat = quat.copy()
        record["quaternion_wxyz"] = [float(v) for v in quat]
        record["target_frame"] = float(target_frame)
        record["source_frame"] = float(source_frame)
        record["cut_hold"] = any(
            left <= source_frame < right for left, right in cut_intervals
        )
        record["cut_hold_interval"] = next(
            (
                [float(left), float(right)]
                for left, right in cut_intervals
                if left <= source_frame < right
            ),
            None,
        )
        samples.append(record)

    reference = []
    for left, right in zip(sample_frames, sample_frames[1:]):
        if right <= left:
            continue
        target_mid = (left + right) / 2.0
        source_mid = _map_target_to_source(
            target_mid,
            source_fps=mapping["source_fps"],
            target_fps=mapping["target_fps"],
            target_frame_origin=mapping["target_frame_origin"],
            source_time_origin=mapping["source_time_origin"],
        )
        _frame_set_exact(temp_scene, source_mid)
        record = _evaluated_camera_snapshot(temp_scene, camera)
        reference.append(
            {
                "left_target_frame": float(left),
                "right_target_frame": float(right),
                "target_frame": float(target_mid),
                "source_frame": float(source_mid),
                "matrix_world": record["matrix_world"],
                "location": record["location"],
                "quaternion_wxyz": record["quaternion_wxyz"],
                "lens": record["lens"],
                "type": record["type"],
                "ortho_scale": record["ortho_scale"],
            }
        )
    return samples, reference


def _fcurve_signature(fcurves) -> str:
    payload = []
    for fcurve in fcurves:
        payload.append(
            {
                "path": fcurve.data_path,
                "index": fcurve.array_index,
                "keys": [
                    {
                        "frame": round(float(kp.co[0]), 9),
                        "value": round(float(kp.co[1]), 12),
                        "interpolation": kp.interpolation,
                    }
                    for kp in fcurve.keyframe_points
                ],
            }
        )
    return canonical_sha256(payload)


def _iter_action_fcurves(action: bpy.types.Action):
    """Iterate native 5.2 channelbag fcurves (with a legacy fallback).

    Blender 5.2 does not expose ``Action.fcurves`` natively.  MMD Tools
    monkey-patches that attribute while registered, so using it after
    unregister would fail.  The channelbag path is the real native API.
    """

    layers = getattr(action, "layers", None)
    if layers is not None:
        for layer in layers:
            for strip in layer.strips:
                for channelbag in strip.channelbags:
                    for fcurve in channelbag.fcurves:
                        yield fcurve
        return
    for fcurve in action.fcurves:  # pragma: no cover - legacy Blender only
        yield fcurve


def _curve_summary(action: bpy.types.Action) -> dict:
    curves = []
    interpolation_counts = {}
    midpoint_deltas = []
    bezier_midpoint_deltas = []
    constant_midpoint_deltas = []
    action_fcurves = list(_iter_action_fcurves(action))
    for fcurve in action_fcurves:
        keys = list(fcurve.keyframe_points)
        counts = {}
        for key in keys:
            counts[key.interpolation] = counts.get(key.interpolation, 0) + 1
            interpolation_counts[key.interpolation] = (
                interpolation_counts.get(key.interpolation, 0) + 1
            )
        curve_midpoints = []
        for left, right in zip(keys, keys[1:]):
            mid_frame = (left.co[0] + right.co[0]) / 2.0
            value = float(fcurve.evaluate(mid_frame))
            linear = (left.co[1] + right.co[1]) / 2.0
            delta = value - linear
            curve_midpoints.append(
                {
                    "mid_frame": mid_frame,
                    "bezier_value": value,
                    "linear_value": linear,
                    "delta": delta,
                }
            )
            midpoint_deltas.append(abs(delta))
        curves.append(
            {
                "path": fcurve.data_path,
                "index": fcurve.array_index,
                "key_count": len(keys),
                "interpolation_counts": counts,
                "first_frame": float(keys[0].co[0]) if keys else None,
                "last_frame": float(keys[-1].co[0]) if keys else None,
                "midpoint_deltas": curve_midpoints,
                "midpoint_max_abs_delta": max(
                    (abs(item["delta"]) for item in curve_midpoints), default=0.0
                ),
            }
        )
        if counts.get("BEZIER", 0):
            bezier_midpoint_deltas.extend(abs(item["delta"]) for item in curve_midpoints)
        if counts.get("CONSTANT", 0):
            constant_midpoint_deltas.extend(
                abs(item["delta"]) for item in curve_midpoints
            )
    return {
        "name": action.name,
        "fcurve_count": len(action_fcurves),
        "slots": [
            {
                "identifier": slot.identifier,
                "name_display": slot.name_display,
                "target_id_type": slot.target_id_type,
            }
            for slot in action.slots
        ],
        "interpolation_counts": interpolation_counts,
        "midpoint_max_abs_delta": max(midpoint_deltas) if midpoint_deltas else 0.0,
        "bezier_midpoint_max_abs_delta": max(bezier_midpoint_deltas)
        if bezier_midpoint_deltas
        else 0.0,
        "constant_midpoint_max_abs_delta": max(constant_midpoint_deltas)
        if constant_midpoint_deltas
        else 0.0,
        "curves": curves,
        "signature": _fcurve_signature(action_fcurves),
    }


def _inspect_imported_actions(
    root: bpy.types.Object,
    camera: bpy.types.Object,
    keys: list[dict],
    *,
    detect_camera_changes: bool,
) -> dict:
    root_action = root.animation_data.action if root.animation_data else None
    camera_action = camera.animation_data.action if camera.animation_data else None
    if root_action is None or camera_action is None:
        raise CameraImportError("MMD Tools 相机导入未产生 root/camera Action")
    root_summary = _curve_summary(root_action)
    camera_summary = _curve_summary(camera_action)
    bezier_curves = [
        curve
        for curve in root_summary["curves"] + camera_summary["curves"]
        if curve["interpolation_counts"].get("BEZIER", 0) > 0
    ]
    persp_curve = next(
        (
            curve
            for curve in root_summary["curves"]
            if curve["path"] == "mmd_camera.is_perspective"
        ),
        None,
    )
    cut_checks = []
    if detect_camera_changes:
        for left, right in zip(keys, keys[1:]):
            if right["frame"] - left["frame"] <= 1:
                matching = []
                # Look at the actual fcurve rather than the summarized values.
                for action in (root_action, camera_action):
                    for fcurve in _iter_action_fcurves(action):
                        for key_point in fcurve.keyframe_points:
                            if abs(key_point.co[0] - left["frame"]) < 1e-6:
                                matching.append(
                                    {
                                        "path": fcurve.data_path,
                                        "index": fcurve.array_index,
                                        "interpolation": key_point.interpolation,
                                    }
                                )
                cut_checks.append(
                    {
                        "from_frame": left["frame"],
                        "to_frame": right["frame"],
                        "all_constant": bool(matching)
                        and all(item["interpolation"] == "CONSTANT" for item in matching),
                        "keys": matching,
                    }
                )
    return {
        "root_action": root_summary,
        "camera_distance_action": camera_summary,
        "bezier_curve_count": len(bezier_curves),
        "bezier_midpoint_max_abs_delta": max(
            root_summary["bezier_midpoint_max_abs_delta"],
            camera_summary["bezier_midpoint_max_abs_delta"],
        ),
        "perspective_constant_midpoint_max_abs_delta": (
            persp_curve["midpoint_max_abs_delta"] if persp_curve else 0.0
        ),
        "perspective_curve": persp_curve,
        "perspective_constant": bool(
            persp_curve
            and persp_curve["interpolation_counts"].get("CONSTANT", 0)
            == persp_curve["key_count"]
        ),
        "cut_checks": cut_checks,
        "detect_camera_changes": bool(detect_camera_changes),
    }


def _cleanup_known_temp(
    temp_scene: bpy.types.Scene,
    root: bpy.types.Object | None,
    camera: bpy.types.Object | None,
    actions: list[bpy.types.Action],
) -> dict:
    removed = {"objects": [], "actions": [], "cameras": [], "scene": None, "errors": []}
    for obj in (root, camera):
        if obj is None:
            continue
        obj_name = obj.name
        camera_data = obj.data if obj.type == "CAMERA" else None
        camera_data_name = camera_data.name if camera_data is not None else None
        try:
            if obj.animation_data:
                obj.animation_data_clear()
        except Exception as exc:  # noqa: BLE001
            removed["errors"].append(f"animation_data_clear {obj_name}: {exc!r}")
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed["objects"].append(obj_name)
        except Exception as exc:  # noqa: BLE001
            removed["errors"].append(f"remove object {obj_name}: {exc!r}")
            continue
        if camera_data_name is not None:
            remaining = bpy.data.cameras.get(camera_data_name)
            if remaining is None or getattr(remaining, "users", 0) != 0:
                continue
            try:
                bpy.data.cameras.remove(remaining)
                removed["cameras"].append(camera_data_name)
            except Exception as exc:  # noqa: BLE001
                removed["errors"].append(f"remove camera data {camera_data_name}: {exc!r}")
    for action in actions:
        if action is None:
            continue
        name = action.name
        try:
            if action.users == 0:
                bpy.data.actions.remove(action)
                removed["actions"].append(name)
            else:
                removed["errors"].append(
                    f"action {name} still has {action.users} users; left in place"
                )
        except Exception as exc:  # noqa: BLE001
            removed["errors"].append(f"remove action {name}: {exc!r}")
    try:
        if temp_scene.name in {scene.name for scene in bpy.data.scenes}:
            name = temp_scene.name
            bpy.data.scenes.remove(temp_scene)
            removed["scene"] = name
    except Exception as exc:  # noqa: BLE001
        removed["errors"].append(f"remove temp scene: {exc!r}")
    return removed


def _require_publish_cleanup(cleanup, temp_scene_name):
    """Refuse final publication after any owned-temp cleanup failure."""
    errors = list((cleanup or {}).get("errors") or [])
    residue = bool(temp_scene_name and bpy.data.scenes.get(temp_scene_name) is not None)
    if errors or residue:
        details = {
            "cleanup_errors": errors,
            "owned_temp_scene": temp_scene_name,
            "owned_temp_scene_residue": residue,
            "diagnostics": cleanup,
        }
        raise CameraImportError(
            "临时清理失败，拒绝写最终library/profile/descriptor；"
            "保留诊断，不删除未知用户数据：" + canonical_json(details)
        )


def _new_action_with_slot(name: str, id_type: str):
    action = bpy.data.actions.new(name)
    slot = action.slots.new(id_type=id_type, name=name)
    layer = action.layers.new(name="Layer")
    strip = layer.strips.new(type="KEYFRAME")
    bag = strip.channelbags.new(slot)
    for field, value in (
        ("blend_type", "REPLACE"),
        ("extrapolation", "HOLD"),
        ("influence", 1.0),
        ("mute", False),
        ("use_animated_time", False),
        ("use_animated_influence", False),
    ):
        try:
            setattr(strip, field, value)
        except (AttributeError, TypeError, ValueError):
            pass
    return action, slot, bag


def _add_fcurve(
    bag,
    data_path: str,
    index: int,
    values: list[tuple[float, float]],
    *,
    group_name: str,
    interpolation: str,
    interpolations: list[str] | None = None,
):
    fcurve = bag.fcurves.new(data_path, index=index, group_name=group_name)
    fcurve.mute = False
    try:
        fcurve.extrapolation = "CONSTANT"
    except (AttributeError, TypeError, ValueError):
        pass
    fcurve.keyframe_points.add(len(values))
    for key_index, (key_point, (frame, value)) in enumerate(
        zip(fcurve.keyframe_points, values)
    ):
        key_point.co = (float(frame), float(value))
        key_point.interpolation = (
            interpolations[key_index] if interpolations is not None else interpolation
        )
    fcurve.update()
    return fcurve


CAMERA_ACTION_EVAL_SIGNATURE_VERSION = 2


def _eval_scalar_fields(value, *, skip=()):
    """Serialize scalar/array RNA values that can affect evaluation."""
    out = {}
    for prop in value.bl_rna.properties:
        name = prop.identifier
        if name == "rna_type" or name in skip or prop.type in {"POINTER", "COLLECTION"}:
            continue
        if prop.type not in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}:
            continue
        try:
            current = getattr(value, name)
        except Exception:  # noqa: BLE001 - property may be unavailable on this build
            continue
        if getattr(prop, "is_array", False):
            try:
                out[name] = list(current)
            except TypeError:
                out[name] = str(current)
        elif isinstance(current, (bool, int, float, str)) or current is None:
            out[name] = current
        elif isinstance(current, set):
            out[name] = sorted(current)
    return out


def _keyframe_eval_record(key):
    return {
        "co": [float(key.co[0]), float(key.co[1])],
        "interpolation": key.interpolation,
        "easing": getattr(key, "easing", None),
        "back": getattr(key, "back", None),
        "amplitude": getattr(key, "amplitude", None),
        "period": getattr(key, "period", None),
        "handle_left": [float(v) for v in key.handle_left],
        "handle_right": [float(v) for v in key.handle_right],
        "handle_left_type": getattr(key, "handle_left_type", None),
        "handle_right_type": getattr(key, "handle_right_type", None),
        "type": getattr(key, "type", None),
    }


def _fcurve_eval_record(fcurve):
    group = getattr(fcurve, "group", None)
    return {
        "path": fcurve.data_path,
        "index": int(fcurve.array_index),
        "mute": bool(fcurve.mute),
        "extrapolation": getattr(fcurve, "extrapolation", None),
        "auto_smoothing": getattr(fcurve, "auto_smoothing", None),
        "group": (
            {"mute": bool(getattr(group, "mute", False))}
            if group is not None
            else None
        ),
        "modifiers": [
            {"type": getattr(modifier, "type", None), "name": modifier.name}
            for modifier in fcurve.modifiers
        ],
        "driver": bool(getattr(fcurve, "driver", None)),
        "keys": [_keyframe_eval_record(key) for key in fcurve.keyframe_points],
    }


def _camera_action_canonical_error(action, slot):
    layers = list(getattr(action, "layers", ()) or ())
    if len(layers) != 1:
        return "相机Action必须是单Layer"
    layer = layers[0]
    if bool(getattr(layer, "mute", False)):
        return "相机Action Layer不能mute"
    strips = list(layer.strips)
    if len(strips) != 1:
        return "相机Action每个Layer必须只有一个Strip"
    strip = strips[0]
    strip_type = getattr(strip, "type", None)
    if strip_type is not None and strip_type != "KEYFRAME":
        return "相机Action Strip必须是KEYFRAME"
    if bool(getattr(strip, "mute", False)):
        return "相机Action Strip不能mute"
    if bool(getattr(strip, "use_animated_time", False)):
        return "相机Action Strip不能启用animated time"
    if bool(getattr(strip, "use_animated_influence", False)):
        return "相机Action Strip不能启用animated influence"
    try:
        influence = float(getattr(strip, "influence", 1.0))
    except (TypeError, ValueError):
        return "相机Action Strip influence不可读"
    if abs(influence - 1.0) > 1e-9:
        return "相机Action Strip influence必须为1.0"
    if getattr(strip, "blend_type", "REPLACE") != "REPLACE":
        return "相机Action Strip blend_type必须为REPLACE"
    if getattr(strip, "extrapolation", "HOLD") not in (None, "HOLD"):
        return "相机Action Strip extrapolation必须为HOLD"
    bags = list(strip.channelbags)
    if len(bags) != 1:
        return "相机Action Strip必须只有一个ChannelBag"
    bag = bags[0]
    if getattr(bag, "slot_handle", slot.handle) != slot.handle:
        return "相机Action ChannelBag Slot与profile Slot不同"
    try:
        direct = strip.channelbag(slot)
    except Exception:  # noqa: BLE001 - legacy Blender fallback
        direct = None
    if (direct is not None
            and getattr(direct, "slot_handle", slot.handle)
            != getattr(bag, "slot_handle", slot.handle)):
        return "相机Action Strip ChanelBag不是请求Slot"
    curves = list(bag.fcurves)
    if not curves:
        return "相机Action Slot没有FCurve"
    for fcurve in curves:
        if bool(fcurve.mute):
            return "相机Action FCurve不能mute"
        if getattr(fcurve, "driver", None) is not None:
            return "相机Action FCurve不能含driver"
        if len(fcurve.modifiers):
            return "相机Action FCurve不能含modifier/generator"
        if getattr(fcurve, "extrapolation", None) not in (None, "CONSTANT"):
            return "相机Action FCurve extrapolation必须为CONSTANT"
        group = getattr(fcurve, "group", None)
        if group is not None and bool(getattr(group, "mute", False)):
            return "相机Action FCurve Group不能mute"
        for key in fcurve.keyframe_points:
            if key.interpolation not in {"LINEAR", "CONSTANT"}:
                return "相机Action只接受LINEAR/CONSTANT关键帧"
    return None


def _camera_action_evaluation_payload(action, slot):
    error = _camera_action_canonical_error(action, slot)
    if error:
        raise CameraImportError(error)
    action_settings = {
        field: getattr(action, field)
        for field in ("use_frame_range", "frame_start", "frame_end", "use_cyclic")
        if hasattr(action, field)
    }
    payload = {
        "signature_version": CAMERA_ACTION_EVAL_SIGNATURE_VERSION,
        "action_settings": action_settings,
        "slot": {"identifier": slot.identifier, "target_id_type": slot.target_id_type},
        "layers": [],
    }
    for layer in action.layers:
        layer_row = _eval_scalar_fields(layer, skip={"name"})
        layer_row["strips"] = []
        for strip in layer.strips:
            strip_row = _eval_scalar_fields(strip, skip={"name"})
            strip_row["channelbags"] = []
            for bag in strip.channelbags:
                bag_row = {
                    "fcurves": [],
                }
                for fcurve in sorted(
                    bag.fcurves, key=lambda item: (item.data_path, item.array_index)
                ):
                    bag_row["fcurves"].append(_fcurve_eval_record(fcurve))
                strip_row["channelbags"].append(bag_row)
            layer_row["strips"].append(strip_row)
        payload["layers"].append(layer_row)
    return payload


def _camera_action_evaluation_signature(action, slot):
    return canonical_sha256(_camera_action_evaluation_payload(action, slot))


def _create_native_camera(
    library_scene: bpy.types.Scene,
    samples: list[dict],
    *,
    camera_name: str,
    camera_data_name: str,
    object_action_name: str,
    data_action_name: str,
    target_fps: float,
    target_range: list[int],
    scale: float,
) -> dict:
    if not samples:
        raise CameraImportError("没有可写入原生相机的样本")
    first = samples[0]
    data = bpy.data.cameras.new(camera_data_name)
    data.type = "PERSP" if first["type"] == "PERSP" else "ORTHO" if first["type"] == "ORTHO" else "PERSP"
    data.lens = float(first["lens"])
    data.sensor_fit = "VERTICAL"
    data.sensor_width = float(first["sensor_width"])
    data.sensor_height = float(first["sensor_height"])
    data.ortho_scale = float(first["ortho_scale"])
    data.clip_start = 0.1
    data.clip_end = 500.0 * float(scale)
    if hasattr(data, "display_size"):
        data.display_size = 5.0 * float(scale)
    if hasattr(data, "dof"):
        data.dof.use_dof = False
        try:
            data.dof.focus_object = None
        except Exception:  # noqa: BLE001
            pass

    camera = bpy.data.objects.new(camera_name, data)
    library_scene.collection.objects.link(camera)
    camera.rotation_mode = "QUATERNION"
    camera.location = Vector(first["location"])
    camera.rotation_quaternion = Quaternion(first["quaternion_wxyz"])
    camera.scale = (1.0, 1.0, 1.0)
    camera.hide_render = False
    camera.hide_viewport = False

    object_action, object_slot, object_bag = _new_action_with_slot(
        object_action_name, "OBJECT"
    )
    data_action, data_slot, data_bag = _new_action_with_slot(
        data_action_name, "CAMERA"
    )
    transform_interpolations = [
        "CONSTANT" if sample.get("cut_hold") else "LINEAR" for sample in samples
    ]
    for axis in range(3):
        _add_fcurve(
            object_bag,
            "location",
            axis,
            [(sample["target_frame"], sample["location"][axis]) for sample in samples],
            group_name="Object Transform",
            interpolation="LINEAR",
            interpolations=transform_interpolations,
        )
    for axis in range(4):
        _add_fcurve(
            object_bag,
            "rotation_quaternion",
            axis,
            [
                (sample["target_frame"], sample["quaternion_wxyz"][axis])
                for sample in samples
            ],
            group_name="Object Transform",
            interpolation="LINEAR",
            interpolations=transform_interpolations,
        )
    _add_fcurve(
        data_bag,
        "lens",
        0,
        [(sample["target_frame"], sample["lens"]) for sample in samples],
        group_name="Camera Data",
        interpolation="LINEAR",
        interpolations=transform_interpolations,
    )
    _add_fcurve(
        data_bag,
        "ortho_scale",
        0,
        [(sample["target_frame"], sample["ortho_scale"]) for sample in samples],
        group_name="Camera Data",
        interpolation="LINEAR",
        interpolations=transform_interpolations,
    )
    _add_fcurve(
        data_bag,
        "type",
        0,
        [
            (
                sample["target_frame"],
                0.0 if sample["type"] == "PERSP" else 1.0 if sample["type"] == "ORTHO" else 0.0,
            )
            for sample in samples
        ],
        group_name="Camera Data",
        interpolation="CONSTANT",
    )
    for action in (object_action, data_action):
        action.use_fake_user = True
        try:
            action.use_frame_range = True
            action.frame_start = float(target_range[0])
            action.frame_end = float(target_range[1])
        except Exception:  # noqa: BLE001
            pass

    camera.animation_data_create()
    camera.animation_data.action = object_action
    camera.animation_data.action_slot = object_slot
    data.animation_data_create()
    data.animation_data.action = data_action
    data.animation_data.action_slot = data_slot

    _set_scene_fps(library_scene, target_fps)
    library_scene.frame_start = int(target_range[0])
    library_scene.frame_end = int(target_range[1])
    library_scene.frame_set(int(target_range[0]))
    library_scene.camera = None
    library_scene.render.resolution_x = 1920
    library_scene.render.resolution_y = 1080
    library_scene.render.resolution_percentage = 100
    library_scene.render.pixel_aspect_x = 1.0
    library_scene.render.pixel_aspect_y = 1.0

    return {
        "camera": camera,
        "data": data,
        "object_action": object_action,
        "object_slot": object_slot,
        "data_action": data_action,
        "data_slot": data_slot,
        "object_fcurves": len(object_bag.fcurves),
        "data_fcurves": len(data_bag.fcurves),
        "object_signature": _fcurve_signature(object_bag.fcurves),
        "data_signature": _fcurve_signature(data_bag.fcurves),
    }


def _evaluate_native_snapshot(scene: bpy.types.Scene, camera: bpy.types.Object) -> dict:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = camera.evaluated_get(depsgraph)
    matrix_world = evaluated.matrix_world.copy()
    data = evaluated.data
    return {
        "matrix_world": [list(row) for row in matrix_world],
        "location": [float(v) for v in matrix_world.translation],
        "quaternion_wxyz": [
            float(v) for v in matrix_world.to_quaternion().normalized()
        ],
        "lens": float(data.lens),
        "type": str(data.type),
        "ortho_scale": float(data.ortho_scale),
    }


def _matrix_max_abs_error(left, right) -> float:
    maximum = 0.0
    for row_left, row_right in zip(left, right):
        for value_left, value_right in zip(row_left, row_right):
            maximum = max(maximum, abs(float(value_left) - float(value_right)))
    return maximum


def _compare_native_samples(
    scene: bpy.types.Scene,
    camera: bpy.types.Object,
    samples: list[dict],
    reference: list[dict],
) -> dict:
    matrix_error = 0.0
    lens_error = 0.0
    ortho_error = 0.0
    type_mismatches = []
    for sample in samples:
        _frame_set_exact(scene, sample["target_frame"])
        native = _evaluate_native_snapshot(scene, camera)
        matrix_error = max(
            matrix_error, _matrix_max_abs_error(native["matrix_world"], sample["matrix_world"])
        )
        lens_error = max(lens_error, abs(native["lens"] - sample["lens"]))
        ortho_error = max(ortho_error, abs(native["ortho_scale"] - sample["ortho_scale"]))
        if native["type"] != sample["type"]:
            type_mismatches.append(
                {
                    "target_frame": sample["target_frame"],
                    "native": native["type"],
                    "official": sample["type"],
                }
            )
    ref_matrix_error = 0.0
    ref_lens_error = 0.0
    ref_ortho_error = 0.0
    ref_type_mismatches = []
    for sample in reference:
        _frame_set_exact(scene, sample["target_frame"])
        native = _evaluate_native_snapshot(scene, camera)
        ref_matrix_error = max(
            ref_matrix_error,
            _matrix_max_abs_error(native["matrix_world"], sample["matrix_world"]),
        )
        ref_lens_error = max(ref_lens_error, abs(native["lens"] - sample["lens"]))
        ref_ortho_error = max(
            ref_ortho_error, abs(native["ortho_scale"] - sample["ortho_scale"])
        )
        if native["type"] != sample["type"]:
            ref_type_mismatches.append(
                {
                    "target_frame": sample["target_frame"],
                    "native": native["type"],
                    "official": sample["type"],
                }
            )
    return {
        "sample_count": len(samples),
        "matrix_max_abs_error": matrix_error,
        "lens_max_abs_error": lens_error,
        "ortho_scale_max_abs_error": ortho_error,
        "type_mismatch_count": len(type_mismatches),
        "type_mismatches": type_mismatches[:20],
        "interpolation_reference_count": len(reference),
        "interpolation_matrix_max_abs_error": ref_matrix_error,
        "interpolation_lens_max_abs_error": ref_lens_error,
        "interpolation_ortho_scale_max_abs_error": ref_ortho_error,
        "interpolation_type_mismatch_count": len(ref_type_mismatches),
        "interpolation_type_mismatches": ref_type_mismatches[:20],
    }


def _semantic_checks(keys: list[dict], samples: list[dict], scale: float) -> dict:
    by_source = {round(sample["source_frame"], 7): sample for sample in samples}
    location_error = 0.0
    rotation_error = 0.0
    distance_error = 0.0
    lens_error = 0.0
    ortho_error = 0.0
    look_error = 0.0
    scale_error = 0.0
    evaluated_keys = 0
    for key in keys:
        sample = by_source.get(round(float(key["frame"]), 7))
        if sample is None:
            continue
        evaluated_keys += 1
        expected_root = [
            key["location"][0] * scale,
            key["location"][2] * scale,
            key["location"][1] * scale,
        ]
        if sample["root_location"] is not None:
            location_error = max(
                location_error,
                max(
                    abs(float(a) - float(b))
                    for a, b in zip(sample["root_location"], expected_root)
                ),
            )
        expected_rot = [
            key["rotation_rad"][0],
            key["rotation_rad"][2],
            key["rotation_rad"][1],
        ]
        if sample["root_rotation_euler_rad"] is not None:
            rotation_error = max(
                rotation_error,
                max(
                    abs(float(a) - float(b))
                    for a, b in zip(sample["root_rotation_euler_rad"], expected_rot)
                ),
            )
        expected_distance = abs(float(key["distance"])) * scale
        if sample["camera_local_y"] is not None:
            distance_error = max(
                distance_error,
                abs(abs(float(sample["camera_local_y"])) - expected_distance),
            )
        expected_lens = sample["sensor_height"] / (
            2.0 * math.tan(math.radians(float(key["angle_deg"])) / 2.0)
        )
        lens_error = max(lens_error, abs(float(sample["lens"]) - expected_lens))
        expected_ortho = 25.0 * expected_distance / 45.0
        ortho_error = max(ortho_error, abs(float(sample["ortho_scale"]) - expected_ortho))
        if sample["look_dot"] is not None:
            look_error = max(look_error, abs(1.0 - float(sample["look_dot"])))
        if sample.get("scale"):
            scale_error = max(
                scale_error,
                max(abs(float(v) - 1.0) for v in sample["scale"]),
            )
    return {
        "evaluated_key_count": evaluated_keys,
        "root_location_max_abs_error": location_error,
        "root_rotation_max_abs_error": rotation_error,
        "distance_max_abs_error": distance_error,
        "lens_max_abs_error": lens_error,
        "ortho_scale_max_abs_error": ortho_error,
        "look_at_dot_max_error": look_error,
        "world_scale_max_error": scale_error,
    }


def _camera_has_mmd_rna(camera: bpy.types.Object) -> bool:
    return hasattr(camera, "mmd_camera") or hasattr(camera.data, "mmd_camera")


def _driver_count(camera: bpy.types.Object) -> int:
    count = 0
    if camera.animation_data:
        count += len(camera.animation_data.drivers)
    if camera.data.animation_data:
        count += len(camera.data.animation_data.drivers)
    return count


def _fcurve_paths(camera: bpy.types.Object) -> list[str]:
    paths = []
    for owner in (camera, camera.data):
        animation_data = getattr(owner, "animation_data", None)
        action = getattr(animation_data, "action", None) if animation_data else None
        if action is None:
            continue
        for fcurve in _iter_action_fcurves(action):
            paths.append(fcurve.data_path)
    return paths


def _copy_existing_scene_snapshot(snapshot: dict, excluded_scene_names: set[str]) -> dict:
    return {
        name: state
        for name, state in snapshot["scenes"].items()
        if name not in excluded_scene_names
    }


def _scene_state_changed(before: dict, after: dict) -> list[str]:
    changes = []
    for name in sorted(set(before) | set(after)):
        if name not in before or name not in after:
            changes.append(f"scene {name} presence changed")
            continue
        left, right = before[name], after[name]
        for field in ("fps", "frame_start", "frame_end", "frame_current", "frame_subframe", "camera"):
            if left.get(field) != right.get(field):
                changes.append(f"{name}.{field}: {left.get(field)!r} -> {right.get(field)!r}")
        if left.get("objects") != right.get("objects"):
            changes.append(f"{name}.objects changed")
        if left.get("collections") != right.get("collections"):
            changes.append(f"{name}.collections changed")
    return changes


def _write_json(path: str, document) -> dict:
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return _file_record(path)


def _write_profile_text(profile_json: str, profile_hash: str):
    existing = bpy.data.texts.get(PROFILE_TEXT_NAME)
    if existing is not None:
        if not existing.get("c94_mmd_camera_v1"):
            raise CameraInputError(
                f"Text {PROFILE_TEXT_NAME!r} 已存在且不属于本模块；拒绝覆盖"
            )
        existing.clear()
        text = existing
    else:
        text = bpy.data.texts.new(PROFILE_TEXT_NAME)
    text.write(profile_json)
    text["c94_mmd_camera_v1"] = canonical_json(
        {"schema": SCHEMA, "role": "CAMERA_PROFILE_TEXT", "profile_sha256": profile_hash}
    )
    return text


def build_camera_library(
    vmd: str,
    output_dir: str,
    *,
    addon_path: str | None = None,
    opencc_wheel: str | None = None,
    runtime_deps_dir: str | None = None,
    source_fps: float = SOURCE_FPS,
    target_fps: float = 24.0,
    target_frame_origin: float = 0.0,
    source_time_origin: float = 0.0,
    scale: float = DEFAULT_SCALE,
    detect_camera_changes: bool = True,
    camera_name: str = DEFAULT_CAMERA_NAME,
    library_scene_name: str = LIBRARY_SCENE_NAME,
    output_stem: str = "output-camera",
    blend_name: str | None = None,
    profile_name: str | None = None,
    descriptor_name: str | None = None,
    overwrite: bool = False,
    require_verified: bool = True,
    batch_identity: dict | None = None,
    source_note: str | None = None,
) -> dict:
    """Build a standalone native camera library .blend from a VMD camera channel.

    The function must run in ``bpy.app.background`` because it temporarily
    registers MMD Tools and creates a temporary scene.  Existing Scenes and
    Actions are never used as a global delta and are not deleted.
    """

    if not bpy.app.background:
        raise CameraInputError(
            "相机库构建只允许 bpy.app.background；GUI 请走后台工作副本"
        )
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise CameraInputError(f"scale 必须是有限正数：{scale!r}")
    if not camera_name or not isinstance(camera_name, str):
        raise CameraInputError("camera_name 必须是非空字符串")
    output_dir = _resolve_dir(output_dir)
    vmd = _resolve_file(vmd, ".vmd")
    addon_path = os.path.abspath(addon_path or DEFAULT_ADDON_PATH)
    opencc_wheel = os.path.abspath(opencc_wheel or DEFAULT_OPENCC_WHEEL)
    runtime_deps_dir = os.path.abspath(
        runtime_deps_dir or os.path.join(output_dir, "runtime-deps", "opencc-0.1.7")
    )
    blend_name = blend_name or f"{output_stem}.blend"
    profile_name = profile_name or f"{output_stem}-profile.json"
    descriptor_name = descriptor_name or f"{output_stem}-descriptor.json"
    for name in (blend_name, profile_name, descriptor_name):
        if os.path.basename(name) != name:
            raise CameraInputError(f"输出文件名不能包含目录：{name!r}")
    blend_path = os.path.join(output_dir, blend_name)
    profile_path = os.path.join(output_dir, profile_name)
    descriptor_path = os.path.join(output_dir, descriptor_name)
    _reject_output_collisions(
        [("blend", blend_path), ("profile", profile_path), ("descriptor", descriptor_path)],
        [vmd],
    )
    if not overwrite:
        for path in (blend_path, profile_path, descriptor_path):
            if os.path.exists(path):
                raise CameraInputError(f"输出已存在（overwrite=False）：{path}")
    if bpy.data.scenes.get(library_scene_name) is not None:
        raise CameraInputError(
            f"已存在 Scene {library_scene_name!r}；拒绝覆盖。请使用新的 library_scene_name"
        )
    camera_data_name = f"{camera_name}_Data"
    object_action_name = f"{camera_name}_OBJECT"
    data_action_name = f"{camera_name}_DATA"
    if bpy.data.objects.get(camera_name) is not None:
        raise CameraInputError(f"已存在 Object {camera_name!r}；拒绝覆盖")
    if bpy.data.cameras.get(camera_data_name) is not None:
        raise CameraInputError(f"已存在 Camera data {camera_data_name!r}；拒绝覆盖")
    if bpy.data.actions.get(object_action_name) is not None or bpy.data.actions.get(
        data_action_name
    ) is not None:
        raise CameraInputError("目标 Action 名称已存在；拒绝覆盖旧 Action")

    source_before = _file_record(vmd)
    structure = validate_vmd_structure(vmd)
    if structure["camera_keys"] == 0:
        raise CameraUnavailableError(
            "VMD camera 通道为空（0 keys）；拒绝创建相机，未修改任何 Scene/Action/输入"
        )
    inspection = inspect_vmd_camera(vmd, addon_path=addon_path, opencc_wheel=opencc_wheel)
    keys = inspection["camera_keys"]
    if not keys:
        raise CameraUnavailableError(
            "VMD camera 通道为空（0 keys）；拒绝创建相机，未修改任何 Scene/Action/输入"
        )
    backend = inspect_backend(addon_path=addon_path, opencc_wheel=opencc_wheel)
    if require_verified and not backend["verified"]:
        raise CameraBackendError(
            "MMD Tools 后端未通过版本/许可/关键文件校验；不创建相机"
        )
    mapping = _build_sample_plan(
        keys,
        source_fps=float(source_fps),
        target_fps=float(target_fps),
        target_frame_origin=float(target_frame_origin),
        source_time_origin=float(source_time_origin),
    )
    sample_frames = mapping["sample_frames"]
    pre_snapshot = _scene_snapshot()
    selected_before = list(bpy.context.selected_objects)
    active_before = bpy.context.view_layer.objects.active

    temp_scene = None
    root = None
    camera = None
    imported_actions = []
    runtime = None
    runtime_handle = None
    cleanup = None
    temp_scene_name = None
    try:
        temp_scene = _new_temp_scene(keys[-1]["frame"])
        temp_scene_name = temp_scene.name
        runtime_payload = _load_mmd_tools(
            addon_path,
            opencc_wheel,
            runtime_deps_dir,
            require_verified=require_verified,
        )
        runtime = runtime_payload
        runtime_handle = _MMDToolsRuntime(runtime_payload)
        root, camera = _create_official_camera(
            temp_scene,
            runtime,
            vmd,
            scale=float(scale),
            action_name="C94_MMD_CAMERA_IMPORT",
            detect_camera_changes=bool(detect_camera_changes),
        )
        imported_actions = [
            action
            for action in (
                root.animation_data.action if root.animation_data else None,
                camera.animation_data.action if camera.animation_data else None,
            )
            if action is not None
        ]
        imported_curves = _inspect_imported_actions(
            root,
            camera,
            keys,
            detect_camera_changes=bool(detect_camera_changes),
        )
        cut_intervals = [
            (float(left["frame"]), float(right["frame"]))
            for left, right in zip(keys, keys[1:])
            if right["frame"] - left["frame"] <= 1
        ]
        with _scene_context(temp_scene):
            samples, reference = _sample_official_camera(
                temp_scene, camera, sample_frames, mapping, cut_intervals
            )
    except Exception:
        if runtime_handle is not None:
            runtime_handle.unregister()
        if temp_scene is not None:
            cleanup = _cleanup_known_temp(
                temp_scene, root, camera, imported_actions
            )
        _restore_selection(selected_before, active_before)
        raise
    finally:
        if temp_scene is not None and cleanup is None:
            if runtime_handle is not None:
                runtime_handle.unregister()
            cleanup = _cleanup_known_temp(temp_scene, root, camera, imported_actions)
            _restore_selection(selected_before, active_before)

    if runtime_handle is not None:
        runtime_handle.unregister()
    _require_runtime_unregistered(runtime_handle)
    registration_after_build = _registration_state()

    source_after_sampling = _file_record(vmd)
    if (
        source_after_sampling["sha256"] != source_before["sha256"]
        or source_after_sampling["bytes"] != source_before["bytes"]
    ):
        raise CameraInputError("源 VMD 在构建过程中被外部修改；拒绝继续")

    _require_publish_cleanup(cleanup, temp_scene_name)
    library_scene = bpy.data.scenes.new(library_scene_name)
    native = _create_native_camera(
        library_scene,
        samples,
        camera_name=camera_name,
        camera_data_name=camera_data_name,
        object_action_name=object_action_name,
        data_action_name=data_action_name,
        target_fps=float(target_fps),
        target_range=mapping["target_range_integer"],
        scale=float(scale),
    )
    object_evaluation_signature = _camera_action_evaluation_signature(
        native["object_action"], native["object_slot"]
    )
    data_evaluation_signature = _camera_action_evaluation_signature(
        native["data_action"], native["data_slot"]
    )
    window = bpy.context.window
    previous_scene = window.scene if window is not None else bpy.context.scene
    if window is not None:
        window.scene = library_scene
    try:
        native_comparison = _compare_native_samples(
            library_scene, native["camera"], samples, reference
        )
    finally:
        library_scene.frame_set(int(mapping["target_range_integer"][0]))
        if window is not None and previous_scene is not None:
            window.scene = previous_scene

    post_snapshot = _scene_snapshot()
    other_scene_changes = _scene_state_changed(
        _copy_existing_scene_snapshot(pre_snapshot, {library_scene_name, temp_scene_name or ""}),
        _copy_existing_scene_snapshot(post_snapshot, {library_scene_name, temp_scene_name or ""}),
    )
    selection_restored = sorted(obj.name for obj in bpy.context.selected_objects) == sorted(
        obj.name for obj in selected_before
    )
    active_now = bpy.context.view_layer.objects.active
    active_restored = (active_now.name if active_now else None) == (
        active_before.name if active_before else None
    )
    semantic = _semantic_checks(keys, samples, float(scale))
    camera_fcurve_paths = _fcurve_paths(native["camera"])
    native_has_mmd = _camera_has_mmd_rna(native["camera"])
    driver_count = _driver_count(native["camera"])
    source_after = _file_record(vmd)
    source_unchanged = (
        source_before["sha256"] == source_after["sha256"]
        and source_before["bytes"] == source_after["bytes"]
    )
    slot_object_type = native["object_slot"].target_id_type
    slot_data_type = native["data_slot"].target_id_type
    object_action_frames = [
        float(kp.co[0])
        for fcurve in _iter_action_fcurves(native["object_action"])
        for kp in fcurve.keyframe_points
    ]
    data_action_frames = [
        float(kp.co[0])
        for fcurve in _iter_action_fcurves(native["data_action"])
        for kp in fcurve.keyframe_points
    ]
    native_key_frames = sorted(set(object_action_frames) | set(data_action_frames))
    profile = {
        "schema": PROFILE_SCHEMA,
        "created_utc": _utc_now(),
        "identity": dict(batch_identity or {}),
        "backend": {
            **inspection.get("parser", {}),
            "id": BACKEND_ID,
            "version": backend.get("version"),
            "verified_version": VERIFIED_BACKEND_VERSION,
            "license": backend.get("license", []),
            "copyright": backend.get("copyright", []),
            "verified": backend.get("verified"),
            "only_verified_backend": True,
            "self_contained_vmd_parser": False,
            "implicit_install_enable": False,
            "addon_path": addon_path,
            "opencc_wheel": opencc_wheel,
            "files": backend.get("files", {}),
            "wheel": backend.get("wheel"),
            "source_identity_sha256": backend.get("source_identity_sha256"),
            "runtime_registration": {
                "registered_accessor": runtime.get("version") if runtime else None,
                "registered_by_module": bool(runtime and runtime.get("registered_by_us")),
                "unregistered_after_build": bool(runtime_handle and runtime_handle.unregistered),
                "unregister_attempted": bool(runtime_handle and runtime_handle.unregister_attempted),
                "registration_left_in_place": bool(
                    runtime_handle and runtime_handle.registration_left_in_place),
                "unregister_error": runtime_handle.unregister_error if runtime_handle else None,
                "not_addon_utils_enabled": True,
                "registration_state_after_build": registration_after_build,
                "mmd_camera_rna_absent_after_build": not registration_after_build.get(
                    "mmd_camera_rna_present", False
                ),
            },
        },
        "source": {
            "vmd": source_before,
            "source_note": source_note,
            "header": inspection.get("header"),
            "structure": inspection.get("structure"),
            "source_fps": float(source_fps),
            "scale": float(scale),
            "camera_keys": keys,
            "camera_key_count": len(keys),
            "camera_frame_range": inspection.get("camera_frame_range"),
            "perspective_events": inspection.get("perspective_events", []),
            "source_cuts": inspection.get("source_cuts", []),
            "detect_camera_changes": bool(detect_camera_changes),
        },
        "mapping": mapping,
        "library": {
            "scene": library_scene_name,
            "camera_object": camera_name,
            "camera_data": camera_data_name,
            "object_action": {
                "name": native["object_action"].name,
                "slot_identifier": native["object_slot"].identifier,
                "slot_target_id_type": slot_object_type,
                "fcurve_count": native["object_fcurves"],
                "key_count_per_curve": len(samples),
                "signature": native["object_signature"],
                "evaluation_signature_version": CAMERA_ACTION_EVAL_SIGNATURE_VERSION,
                "evaluation_signature": object_evaluation_signature,
            },
            "data_action": {
                "name": native["data_action"].name,
                "slot_identifier": native["data_slot"].identifier,
                "slot_target_id_type": slot_data_type,
                "fcurve_count": native["data_fcurves"],
                "key_count_per_curve": len(samples),
                "signature": native["data_signature"],
                "evaluation_signature_version": CAMERA_ACTION_EVAL_SIGNATURE_VERSION,
                "evaluation_signature": data_evaluation_signature,
            },
            "object_curves": [
                "location[0..2]",
                "rotation_quaternion[0..3]",
            ],
            "data_curves": ["lens[0]", "ortho_scale[0]", "type[0]"],
            "scene_fps": _effective_fps(library_scene),
            "scene_frame_start": int(library_scene.frame_start),
            "scene_frame_end": int(library_scene.frame_end),
            "scene_frame_current": int(library_scene.frame_current),
            "scene_active_camera": None,
            "native_key_frames": native_key_frames,
            "native_key_count": len(native_key_frames),
            "bake_interpolation": {
                "transform_and_lens": "LINEAR except CONSTANT hold across source cuts",
                "camera_type": "CONSTANT",
            },
            "cut_hold_key_count": sum(1 for sample in samples if sample.get("cut_hold")),
            "fake_user": True,
        },
        "semantics": {
            "coordinate_mapping": "MMD (x,y,z) -> Blender (x,z,y), multiplied by scale",
            "vmd_camera_location": "MMD look-at target; MMD Tools root EMPTY location",
            "vmd_camera_rotation_unit": "radians (MMD Tools reads/writes camera rotation directly as radians)",
            "blender_root_rotation_mapping": "root Euler XYZ=(mmd_x, mmd_z, mmd_y), rotation_mode=YXZ",
            "vmd_distance": "signed local Y of the MMD Tools camera child; camera view points from child to root/look-at target",
            "perspective": "VMD persp=True -> Blender PERSP; persp=False -> ORTHO (MMD Tools driver `not is_perspective`)",
            "fov": "VMD angle is degrees -> radians in mmd_camera.angle; vertical FOV equals that angle",
            "lens": "lens = sensor_height / (2*tan(angle/2)); sensor_fit=VERTICAL, sensor_height=24mm",
            "ortho_scale": "MMD Tools driver: 25*abs(camera_local_y)/45 = 25*abs(distance*scale)/45",
            "radius_to_look_at": "camera world position + view_direction * (-distance*scale) = root/look-at target",
            "native_bake": "world matrix -> native object location + quaternion; lens/type/ortho_scale -> native CAMERA Action",
            "scale": float(scale),
            "measured": semantic,
        },
        "samples": samples,
        "interpolation_reference": reference,
        "imported_curves": imported_curves,
        "checks": {
            "source_sha256_unchanged": source_unchanged,
            "source_bytes_unchanged": source_before["bytes"] == source_after["bytes"],
            "source_not_written": source_unchanged,
            "camera_channel_nonempty": len(keys) > 0,
            "camera_key_count_matches_structure": len(keys) == structure["camera_keys"],
            "camera_keys_sorted": [k["frame"] for k in keys] == sorted(k["frame"] for k in keys),
            "camera_perspective_events": inspection.get("perspective_events", []),
            "source_camera_cuts": inspection.get("source_cuts", []),
            "official_camera_import_used": True,
            "official_imported_root_action": imported_curves["root_action"]["name"],
            "official_imported_camera_action": imported_curves["camera_distance_action"]["name"],
            "official_imported_bezier_curve_count": imported_curves["bezier_curve_count"],
            "official_imported_bezier_midpoint_max_abs_delta": imported_curves[
                "bezier_midpoint_max_abs_delta"
            ],
            "official_imported_perspective_constant_midpoint_max_abs_delta": imported_curves[
                "perspective_constant_midpoint_max_abs_delta"
            ],
            "official_imported_perspective_constant": imported_curves["perspective_constant"],
            "official_imported_cut_checks": imported_curves["cut_checks"],
            "native_vs_official": native_comparison,
            "native_object_slot_target_id_type": slot_object_type,
            "native_data_slot_target_id_type": slot_data_type,
            "native_slot_types_distinct": slot_object_type == "OBJECT"
            and slot_data_type == "CAMERA",
            "native_camera_object_parent": None,
            "native_driver_count": driver_count,
            "native_has_mmd_rna": native_has_mmd,
            "native_fcurve_paths": sorted(set(camera_fcurve_paths)),
            "native_fcurve_paths_have_mmd_camera": any(
                "mmd_camera" in path for path in camera_fcurve_paths
            ),
            "native_uses_mmd_tools_at_cold_open": False,
            "library_scene_active_camera_is_none": library_scene.camera is None,
            "existing_scene_changes": other_scene_changes,
            "existing_scenes_preserved": not other_scene_changes,
            "selection_restored": selection_restored,
            "active_object_restored": active_restored,
            "temp_cleanup": cleanup,
            "backend_verified": bool(backend.get("verified")),
            "runtime_unregistered": bool(runtime_handle and runtime_handle.unregistered),
            "mmd_camera_rna_absent_after_build": not registration_after_build.get(
                "mmd_camera_rna_present", False
            ),
            "source_fixture_artificial": bool(source_note),
            "target_fps": float(target_fps),
            "target_fps_effective": _effective_fps(library_scene),
            "source_fps": float(source_fps),
            "timebase_mapping": "SECONDS",
            "key_target_frames": mapping["key_target_frames"],
            "key_mid_target_frames": mapping["mid_target_frames"],
            "sample_count": len(samples),
            "interpolation_reference_count": len(reference),
            "native_key_count": len(native_key_frames),
            "native_cut_hold_key_count": sum(
                1 for sample in samples if sample.get("cut_hold")
            ),
        },
        "limitations": [
            "本交付的默认 fixture 是人工生成的真实 VMD 格式；现有三个真实样本 VMD 的 camera 通道均为 0，不能据此声称真实镜头覆盖。",
            "原生相机在 build 时按目标整数帧 + 源关键帧/中点的秒时基采样；关键帧之间对变换/lens/ortho_scale 使用 LINEAR，对 type 使用 CONSTANT。插值参考误差已写入 checks，不应解释为原 MMD 播放器逐帧一致。",
            "MMD Tools 4.5.14 只在 build 进程内注册；候选 .blend 的冷开/求值不依赖 MMD Tools、mmd_camera RNA 或 addon driver。",
            "未与原 MMD 播放器的画面/艺术效果逐帧对照；数值语义与视觉艺术效果是两件事。",
            "VMD camera 的 rotation 在 MMD Tools 中按 radians 读取；angle 按 degrees 读取后转为 radians。不同第三方 VMD 写入器的单位约定未穷举。",
            "未验证其他 MMD Tools 版本、其他 VMD 版本、真实 camera VMD 素材、物理/贴图/音频、渲染质量或跨 Blender 版本一致性。",
        ],
    }
    profile["profile_sha256"] = profile_sha256(profile)
    profile_file = _write_json(profile_path, profile)
    profile_json = canonical_json(profile)
    _write_profile_text(profile_json, profile["profile_sha256"])

    library_scene["c94_mmd_camera_scene_v1"] = canonical_json(
        {
            "schema": SCHEMA,
            "profile_sha256": profile["profile_sha256"],
            "scene": library_scene_name,
            "camera_object": camera_name,
            "camera_data": camera_data_name,
            "object_action": object_action_name,
            "data_action": data_action_name,
            "source_sha256": source_before["sha256"],
            "target_fps": float(target_fps),
            "source_fps": float(source_fps),
            "active_camera": None,
        }
    )
    native["camera"]["c94_mmd_camera_v1"] = canonical_json(
        {
            "schema": SCHEMA,
            "role": "CAMERA_OBJECT",
            "camera_name": camera_name,
            "source_sha256": source_before["sha256"],
            "profile_sha256": profile["profile_sha256"],
            "slot_identifier": native["object_slot"].identifier,
            "slot_target_id_type": slot_object_type,
            "mmd_tools_required": False,
        }
    )
    native["data"]["c94_mmd_camera_v1"] = canonical_json(
        {
            "schema": SCHEMA,
            "role": "CAMERA_DATA",
            "camera_name": camera_name,
            "source_sha256": source_before["sha256"],
            "profile_sha256": profile["profile_sha256"],
            "slot_identifier": native["data_slot"].identifier,
            "slot_target_id_type": slot_data_type,
            "mmd_tools_required": False,
        }
    )
    native["object_action"]["c94_mmd_camera_v1"] = canonical_json(
        {
            "schema": SCHEMA,
            "role": "CAMERA_OBJECT_ACTION",
            "profile_sha256": profile["profile_sha256"],
            "slot_identifier": native["object_slot"].identifier,
            "slot_target_id_type": slot_object_type,
        }
    )
    native["data_action"]["c94_mmd_camera_v1"] = canonical_json(
        {
            "schema": SCHEMA,
            "role": "CAMERA_DATA_ACTION",
            "profile_sha256": profile["profile_sha256"],
            "slot_identifier": native["data_slot"].identifier,
            "slot_target_id_type": slot_data_type,
        }
    )
    if window is not None:
        window.scene = library_scene
    bpy.ops.wm.save_as_mainfile(filepath=blend_path, check_existing=False)
    blend_record = _file_record(blend_path)
    descriptor = {
        "schema": DESCRIPTOR_SCHEMA,
        "created_utc": profile["created_utc"],
        "identity": dict(batch_identity or {}),
        "backend": {
            "id": BACKEND_ID,
            "version": backend.get("version"),
            "license": backend.get("license", []),
            "verified": backend.get("verified"),
            "only_verified_backend": True,
            "self_contained_vmd_parser": False,
            "implicit_install_enable": False,
            "source_identity_sha256": backend.get("source_identity_sha256"),
        },
        "inputs": {"vmd": source_before},
        "mapping": {
            "mode": mapping["mode"],
            "source_fps": mapping["source_fps"],
            "target_fps": mapping["target_fps"],
            "target_frame_origin": mapping["target_frame_origin"],
            "source_time_origin": mapping["source_time_origin"],
            "target_range_integer": mapping["target_range_integer"],
            "sample_count": mapping["sample_count"],
            "key_target_frames": mapping["key_target_frames"],
            "mid_target_frames": mapping["mid_target_frames"],
        },
        "artifacts": {
            "blend": blend_record,
            "profile": profile_file,
        },
        "objects": {
            "camera_object": camera_name,
            "camera_data": camera_data_name,
            "object_action": object_action_name,
            "data_action": data_action_name,
            "object_slot": native["object_slot"].identifier,
            "data_slot": native["data_slot"].identifier,
        },
        "checks": profile["checks"],
        "profile_sha256": profile["profile_sha256"],
        "limitations": profile["limitations"],
    }
    descriptor_file = _write_json(descriptor_path, descriptor)
    return {
        "ok": True,
        "schema": SCHEMA,
        "blend_path": blend_path,
        "profile_path": profile_path,
        "descriptor_path": descriptor_path,
        "blend": blend_record,
        "profile_file": profile_file,
        "descriptor_file": descriptor_file,
        "profile_sha256": profile["profile_sha256"],
        "identity": dict(batch_identity or {}),
        "backend": backend,
        "source": source_before,
        "mapping": {
            "mode": mapping["mode"],
            "source_fps": mapping["source_fps"],
            "target_fps": mapping["target_fps"],
            "target_frame_origin": mapping["target_frame_origin"],
            "source_time_origin": mapping["source_time_origin"],
            "target_range_integer": mapping["target_range_integer"],
            "sample_count": mapping["sample_count"],
            "key_target_frames": mapping["key_target_frames"],
            "mid_target_frames": mapping["mid_target_frames"],
        },
        "library": profile["library"],
        "checks": profile["checks"],
        "limitations": profile["limitations"],
        "cleanup": cleanup,
    }


def load_camera_profile(source) -> dict:
    """Load a camera profile from dict/path/Blender Text/JSON string."""

    if isinstance(source, dict):
        document = dict(source)
    elif isinstance(source, bpy.types.Text):
        document = json.loads(source.as_string())
    elif isinstance(source, str) and os.path.isfile(source):
        with open(source, "r", encoding="utf-8") as fh:
            document = json.load(fh)
    elif isinstance(source, str):
        document = json.loads(source)
    else:
        raise CameraInputError(f"不支持的 profile 来源：{type(source)!r}")
    validate_camera_profile(document)
    return document


def validate_camera_profile(profile: dict) -> dict:
    if not isinstance(profile, dict) or profile.get("schema") != PROFILE_SCHEMA:
        raise CameraInputError("profile schema 不是 c94.retarget.mmd_camera_profile.v1")
    expected = profile.get("profile_sha256")
    if not expected or expected != profile_sha256(profile):
        raise CameraInputError("profile_sha256 不符，拒绝被修改的 profile")
    for forbidden in ("python", "exec", "eval", "script"):
        if forbidden in profile:
            raise CameraInputError(f"profile 拒绝任意 Python 预设字段：{forbidden}")
    return profile


def _find_slot(action: bpy.types.Action, target_id_type: str, preferred_identifier: str | None = None):
    if preferred_identifier:
        for slot in action.slots:
            if slot.identifier == preferred_identifier:
                return slot
    for slot in action.slots:
        if slot.target_id_type == target_id_type:
            return slot
    return None


def _action_key_signature(action: bpy.types.Action) -> str:
    payload = []
    for fcurve in _iter_action_fcurves(action):
        payload.append(
            {
                "path": fcurve.data_path,
                "index": fcurve.array_index,
                "keys": [
                    [
                        round(float(kp.co[0]), 9),
                        round(float(kp.co[1]), 12),
                        kp.interpolation,
                    ]
                    for kp in fcurve.keyframe_points
                ],
            }
        )
    return canonical_sha256(payload)


def _slot_fcurve_signature(action: bpy.types.Action, slot) -> str:
    curves = []
    layers = getattr(action, "layers", None)
    for layer in layers or []:
        for strip in layer.strips:
            bag = strip.channelbag(slot)
            if bag is not None:
                curves.extend(bag.fcurves)
    if layers is None:
        curves = list(_iter_action_fcurves(action))
    return _fcurve_signature(curves)


def _remove_owned_datablock(data):
    """Remove only the exact loaded ID, dispatching by its real RNA type."""
    if data is None:
        return False
    for type_name, collection_name in (
        ("Camera", "cameras"),
        ("Mesh", "meshes"),
        ("Light", "lights"),
        ("Curve", "curves"),
        ("Armature", "armatures"),
        ("Lattice", "lattices"),
        ("Speaker", "speakers"),
        ("Volume", "volumes"),
    ):
        type_class = getattr(bpy.types, type_name, None)
        collection = getattr(bpy.data, collection_name, None)
        if type_class is None or collection is None or not isinstance(data, type_class):
            continue
        try:
            collection.remove(data, do_unlink=True)
            return True
        except Exception:  # noqa: BLE001 - exact loaded ID cleanup is best-effort
            return False
    return False


def _discard_loaded_camera(camera, object_action, data_action, *, extra_actions=()):
    """Remove only exact IDs loaded by the failed append; no global delta."""
    data = camera.data if camera is not None else None
    if camera is not None:
        try:
            bpy.data.objects.remove(camera, do_unlink=True)
        except Exception:  # noqa: BLE001 - best-effort cleanup of our own ID
            pass
    _remove_owned_datablock(data)
    for action in (object_action, data_action, *extra_actions):
        if action is not None:
            try:
                bpy.data.actions.remove(action, do_unlink=True)
            except Exception:  # noqa: BLE001
                pass

def _validate_loaded_camera_library(profile, camera, object_action, data_action,
                                    object_slot, data_slot):
    library = profile["library"]
    recorded_checks = profile.get("checks") or {}
    if isinstance(recorded_checks, dict):
        if recorded_checks.get("native_driver_count", 0) not in (0, None):
            raise CameraImportError("profile记录原生相机含 driver；拒绝 Appending")
        if recorded_checks.get("native_has_mmd_rna") is True:
            raise CameraImportError("profile记录原生相机含 MMD RNA；拒绝 Appending")
    checks = (
        ("object", object_action, object_slot, library["object_action"]),
        ("data", data_action, data_slot, library["data_action"]),
    )
    for role, action, slot, record in checks:
        version = record.get("evaluation_signature_version")
        expected_eval = record.get("evaluation_signature")
        if version != CAMERA_ACTION_EVAL_SIGNATURE_VERSION or not isinstance(expected_eval, str) or len(expected_eval) != 64:
            raise CameraImportError(
                f"profile的{role} Action只有旧弱签名或缺少v2完整求值签名；"
                "请重新构建camera library生成完整签名，不能静默补签"
            )
        expected_slot = record.get("slot_identifier")
        if expected_slot is not None and slot.identifier != expected_slot:
            raise CameraImportError(
                f"append 的{role} Action Slot 与 profile 记录不同："
                f"{slot.identifier!r} != {expected_slot!r}"
            )
        expected_id_type = record.get("slot_target_id_type")
        if expected_id_type is not None and slot.target_id_type != expected_id_type:
            raise CameraImportError(
                f"append 的{role} Action Slot 类型与 profile 记录不同"
            )
        canonical_error = _camera_action_canonical_error(action, slot)
        if canonical_error:
            raise CameraImportError(
                f"append 的{role} Action不是规范相机结构：{canonical_error}"
            )
        actual_eval = _camera_action_evaluation_signature(action, slot)
        if actual_eval != expected_eval:
            raise CameraImportError(
                f"append 的{role} Action 完整求值签名与 profile 不同；拒绝错误镜头"
            )
        expected_weak = record.get("signature")
        if expected_weak is not None:
            if not isinstance(expected_weak, str) or _slot_fcurve_signature(action, slot) != expected_weak:
                raise CameraImportError(
                    f"append 的{role} Action 弱曲线签名与 profile 不一致"
                )
    if _driver_count(camera) != 0:
        raise CameraImportError("append 的相机含 driver；拒绝非原生库")
    if _camera_has_mmd_rna(camera):
        raise CameraImportError("append 的相机含 MMD RNA；拒绝非原生库")
    if any("mmd_camera" in path for path in _fcurve_paths(camera)):
        raise CameraImportError("append 的相机 Action 含 mmd_camera 路径；拒绝非原生库")


def append_camera_library(
    blend_path: str,
    profile,
    *,
    scene: bpy.types.Scene | None = None,
    camera_name: str | None = None,
    allow_fps_mismatch: bool = False,
) -> dict:
    """Append the camera object/data/actions into a Scene without activating it.

    Existing Scene fps/range/current frame/active camera/selection and old
    Actions are preserved.  The appended camera becomes the only new camera
    object; it is never assigned to ``Scene.camera``.
    """

    blend_path = _resolve_file(blend_path, ".blend")
    profile = load_camera_profile(profile)
    scene = scene or bpy.context.scene
    if scene is None:
        raise CameraInputError("没有可用的目标 Scene")
    library = profile["library"]
    camera_name = camera_name or library["camera_object"]
    camera_data_name = library["camera_data"]
    object_action_name = library["object_action"]["name"]
    data_action_name = library["data_action"]["name"]
    if camera_name != library["camera_object"]:
        raise CameraInputError(
            f"append 只支持库内相机名 {library['camera_object']!r}；收到 {camera_name!r}"
        )
    target_fps = float(profile["mapping"]["target_fps"])
    scene_fps = _effective_fps(scene)
    if abs(scene_fps - target_fps) > 1e-6 and not allow_fps_mismatch:
        raise CameraInputError(
            f"目标 Scene fps={scene_fps} 与相机库 target_fps={target_fps} 不符；"
            "为避免按秒时基错位，拒接 append（不会修改 Scene fps）"
        )
    if camera_name in bpy.data.objects:
        raise CameraInputError(f"目标文件已存在 Object {camera_name!r}；拒绝覆盖")
    for name in (object_action_name, data_action_name):
        if name in bpy.data.actions:
            raise CameraInputError(f"目标文件已存在 Action {name!r}；拒绝覆盖")
    if camera_data_name in bpy.data.cameras:
        raise CameraInputError(f"目标文件已存在 Camera data {camera_data_name!r}；拒绝覆盖")

    before_scene = {
        "name": scene.name,
        "fps": scene_fps,
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "frame_current": int(scene.frame_current),
        "frame_subframe": float(scene.frame_subframe),
        "camera": scene.camera.name if scene.camera else None,
    }
    selected_before = list(bpy.context.selected_objects)
    active_before = bpy.context.view_layer.objects.active
    old_action_signatures = {
        action.name: _action_key_signature(action)
        for action in bpy.data.actions
        if action.name not in {object_action_name, data_action_name}
    }
    with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
        for required in (camera_name,):
            if required not in data_from.objects:
                raise CameraInputError(f"相机库缺少 Object {required!r}")
        for required in (object_action_name, data_action_name):
            if required not in data_from.actions:
                raise CameraInputError(f"相机库缺少 Action {required!r}")
        data_to.objects = [camera_name]
        data_to.actions = [object_action_name, data_action_name]
    camera = bpy.data.objects.get(camera_name)
    object_action = bpy.data.actions.get(object_action_name)
    data_action = bpy.data.actions.get(data_action_name)
    try:
        if camera is None or object_action is None or data_action is None:
            raise CameraImportError("append 未加载完整的 camera/object action/data action")
        if camera.type != "CAMERA":
            raise CameraImportError(f"append 的 {camera_name!r} 不是 CAMERA")
        if camera.data.name != camera_data_name:
            raise CameraImportError(
                f"append 的 camera data 名称不符：{camera.data.name!r} != {camera_data_name!r}"
            )
        if camera.name not in {obj.name for obj in scene.objects}:
            scene.collection.objects.link(camera)
        object_slot = _find_slot(
            object_action,
            "OBJECT",
            library["object_action"].get("slot_identifier"),
        )
        data_slot = _find_slot(
            data_action,
            "CAMERA",
            library["data_action"].get("slot_identifier"),
        )
        if object_slot is None or data_slot is None:
            raise CameraImportError("append 的 Action slot 类型不完整")
        camera.animation_data_create()
        camera.animation_data.action = object_action
        camera.animation_data.action_slot = object_slot
        camera.data.animation_data_create()
        camera.data.animation_data.action = data_action
        camera.data.animation_data.action_slot = data_slot
        camera.parent = None
        camera.rotation_mode = "QUATERNION"
        _validate_loaded_camera_library(
            profile, camera, object_action, data_action, object_slot, data_slot
        )
    except BaseException:
        _discard_loaded_camera(camera, object_action, data_action)
        raise

    _restore_selection(selected_before, active_before)
    after_scene = {
        "name": scene.name,
        "fps": _effective_fps(scene),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "frame_current": int(scene.frame_current),
        "frame_subframe": float(scene.frame_subframe),
        "camera": scene.camera.name if scene.camera else None,
    }
    old_actions_after = {
        action.name: _action_key_signature(action)
        for action in bpy.data.actions
        if action.name in old_action_signatures
    }
    return {
        "ok": True,
        "appended": True,
        "library_blend": blend_path,
        "profile_sha256": profile["profile_sha256"],
        "scene": scene.name,
        "camera_object": camera.name,
        "camera_data": camera.data.name,
        "object_action": object_action.name,
        "object_slot": object_slot.identifier,
        "object_slot_target_id_type": object_slot.target_id_type,
        "data_action": data_action.name,
        "data_slot": data_slot.identifier,
        "data_slot_target_id_type": data_slot.target_id_type,
        "scene_before": before_scene,
        "scene_after": after_scene,
        "scene_state_preserved": before_scene == after_scene,
        "old_action_signatures_before": old_action_signatures,
        "old_action_signatures_after": old_actions_after,
        "old_actions_preserved": old_action_signatures == old_actions_after,
        "active_camera_replaced": before_scene["camera"] != after_scene["camera"],
        "selection_restored": sorted(obj.name for obj in bpy.context.selected_objects)
        == sorted(obj.name for obj in selected_before),
        "active_object_restored": (
            (bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None)
            == (active_before.name if active_before else None)
        ),
        "driver_count": _driver_count(camera),
        "mmd_rna_present": _camera_has_mmd_rna(camera),
    }


def _migration_sha256(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value.lower())):
        raise CameraInputError(f"{label} 必须是64位十六进制SHA256")
    return value.lower()


def _migration_data_name_exists(name):
    for collection_name in (
        "cameras", "meshes", "lights", "curves", "armatures",
        "lattices", "speakers", "volumes",
    ):
        collection = getattr(bpy.data, collection_name, None)
        if collection is not None and name in collection:
            return True
    return False


def _migration_scene_state():
    scene = bpy.context.scene
    return {
        "scene": scene.name,
        "camera": scene.camera.name if scene.camera else None,
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": (bpy.context.view_layer.objects.active.name
                   if bpy.context.view_layer.objects.active else None),
        "frame": int(scene.frame_current),
        "subframe": float(scene.frame_subframe),
    }


def _camera_returned_import_ids(roots, before, library_path):
    """Adopt only returned roots and their fresh dependency closure."""
    from . import module_configuration

    fresh = module_configuration._ids() - before
    owned = {item for item in roots
             if isinstance(item, bpy.types.ID) and item in fresh}
    users = bpy.data.user_map()
    pending = set(fresh) - owned
    changed = True
    while changed:
        changed = False
        for item in tuple(pending):
            if users.get(item, set()) & owned:
                owned.add(item)
                pending.remove(item)
                changed = True
    wanted_library = Path(library_path).resolve()
    for item in fresh:
        if isinstance(item, bpy.types.Library):
            try:
                if Path(bpy.path.abspath(item.filepath)).resolve() == wanted_library:
                    owned.add(item)
            except (ValueError, OSError):
                pass
    return owned


def _resolve_legacy_camera_slot(action, record, expected_type, role):
    """Resolve a declared legacy slot exactly; never fallback on wrong data."""
    declared_type = record.get("slot_target_id_type")
    if declared_type is not None and declared_type != expected_type:
        raise CameraImportError(
            f"旧profile {role} slot_target_id_type 与Action类型不符")
    slots = list(action.slots)
    identifier = record.get("slot_identifier")
    if identifier is not None:
        matches = [slot for slot in slots if slot.identifier == identifier]
        if len(matches) != 1:
            raise CameraImportError(
                f"旧profile {role} slot_identifier 不存在或不唯一；拒绝fallback")
        slot = matches[0]
        source = "declared_identifier"
    else:
        # Legacy profiles without a slot identifier are only accepted when
        # the action has one unambiguous slot of the declared type.
        if len(slots) != 1:
            raise CameraImportError(
                f"旧profile {role} 缺少slot_identifier且Action不是单Slot；拒绝fallback")
        slot = slots[0]
        source = "legacy_missing_identifier_single_slot"
    if slot.target_id_type != expected_type:
        raise CameraImportError(
            f"旧profile {role} 实际Slot类型不是{expected_type}")
    return slot, source


def _verify_legacy_camera_library(profile, camera, object_action, data_action,
                                  object_slot, data_slot, slot_sources=None):
    """Strict legacy evaluation-path checks before a v2 sidecar is written."""
    library = profile["library"]
    camera_name = library["camera_object"]
    data_name = library["camera_data"]
    if camera.type != "CAMERA":
        raise CameraImportError("legacy库camera根不是CAMERA")
    if camera.name != camera_name:
        raise CameraImportError("legacy库camera根名称与profile不符")
    if not isinstance(camera.data, bpy.types.Camera) or camera.data.name != data_name:
        raise CameraImportError("legacy库camera data不是精确Camera data")
    if camera.parent is not None:
        raise CameraImportError("legacy库camera根含parent求值路径")
    if len(camera.constraints):
        raise CameraImportError("legacy库camera根含constraint求值路径")
    if len(getattr(camera, "modifiers", ())):
        raise CameraImportError("legacy库camera根含modifier求值路径")
    if _camera_has_mmd_rna(camera):
        raise CameraImportError("legacy库camera含MMD RNA")
    if _driver_count(camera) != 0:
        raise CameraImportError("legacy库camera含driver求值路径")

    for role, action, slot, expected_type in (
        ("object", object_action, object_slot, "OBJECT"),
        ("data", data_action, data_slot, "CAMERA"),
    ):
        if len(list(action.slots)) != 1:
            raise CameraImportError(f"legacy库{role} Action必须只有一个Slot")
        if (action.slots[0].identifier != slot.identifier
                or action.slots[0].handle != slot.handle
                or slot.target_id_type != expected_type):
            raise CameraImportError(f"legacy库{role} Action Slot类型/身份不符")
        record = library[f"{role}_action"]
        expected_weak = record.get("signature")
        if (not isinstance(expected_weak, str) or len(expected_weak) != 64
                or _slot_fcurve_signature(action, slot) != expected_weak):
            raise CameraImportError(f"legacy库{role} Action弱曲线签名不符")
        canonical_error = _camera_action_canonical_error(action, slot)
        if canonical_error:
            raise CameraImportError(
                f"legacy库{role} Action不是规范单layer/strip/bag结构："
                + canonical_error
            )

    def check_animation_data(owner, action, slot, label):
        ad = getattr(owner, "animation_data", None)
        if ad is None:
            return "absent"
        if len(ad.drivers):
            raise CameraImportError(f"legacy库{label}含driver求值路径")
        if len(ad.nla_tracks):
            raise CameraImportError(f"legacy库{label}含NLA求值路径")
        if getattr(ad, "action_blend_type", None) != "REPLACE":
            raise CameraImportError(f"legacy库{label} action_blend_type非REPLACE")
        if abs(float(getattr(ad, "action_influence", 1.0)) - 1.0) > 1e-9:
            raise CameraImportError(f"legacy库{label} action_influence非1.0")
        if getattr(ad, "action_extrapolation", None) != "HOLD":
            raise CameraImportError(f"legacy库{label} action_extrapolation非HOLD")
        if ad.action is not None:
            if (ad.action.name != action.name or ad.action_slot is None
                    or ad.action_slot.identifier != slot.identifier):
                raise CameraImportError(f"legacy库{label}已绑定其他Action/Slot")
        return {
            "status": "canonical",
            "use_nla": bool(ad.use_nla),
            "nla_tracks": len(ad.nla_tracks),
        }

    object_ad = check_animation_data(camera, object_action, object_slot, "camera object")
    data_ad = check_animation_data(camera.data, data_action, data_slot, "camera data")
    data_constraints = getattr(camera.data, "constraints", ())
    if len(data_constraints):
        raise CameraImportError("legacy库camera data含constraint求值路径")
    return {
        "camera_type": "CAMERA",
        "camera_data_type": "Camera",
        "camera_object": camera.name,
        "camera_data": camera.data.name,
        "object_slot": object_slot.identifier,
        "data_slot": data_slot.identifier,
        "slot_resolution": slot_sources or {},
        "weak_signatures": {
            "object": library["object_action"]["signature"],
            "data": library["data_action"]["signature"],
        },
        "parent": "none",
        "constraints": "none",
        "modifiers": "none",
        "drivers": "none",
        "nla": "none",
        "object_animation_data": object_ad,
        "data_animation_data": data_ad,
        "fcurves": "canonical_linear_or_constant",
    }


def migrate_legacy_camera_profile(
    blend_path,
    profile_path,
    output_profile_path,
    *,
    expected_library_sha256,
    expected_profile_sha256,
):
    """Explicitly verify an old weak camera profile and write a new v2 sidecar.

    The old .blend and old profile are opened read-only and are never written.
    The caller must provide exact **file-byte** SHA256 values (not just the
    profile document hash).  The output JSON must not already exist.  Only the
    loaded object/data/actions are cleaned; no global data diff, callback list
    or external user data is touched.
    """
    expected_library_sha256 = _migration_sha256(
        expected_library_sha256, "expected_library_sha256")
    expected_profile_sha256 = _migration_sha256(
        expected_profile_sha256, "expected_profile_sha256")
    library_path = _resolve_file(
        bpy.path.abspath(os.fspath(blend_path)), ".blend")
    profile_path = _resolve_file(
        bpy.path.abspath(os.fspath(profile_path)), ".json")
    output_path = Path(bpy.path.abspath(os.fspath(output_profile_path))).resolve()
    if output_path.suffix.lower() != ".json":
        raise CameraInputError("迁移输出必须是新的 .json profile路径")
    if output_path == Path(library_path).resolve() or output_path == Path(profile_path).resolve():
        raise CameraInputError("迁移输出不得覆盖旧library或旧profile")
    if output_path.exists():
        raise CameraInputError(f"迁移输出已存在，拒绝覆盖：{output_path}")
    if not output_path.parent.is_dir():
        raise CameraInputError(f"迁移输出父目录不存在：{output_path.parent}")

    try:
        profile_bytes = Path(profile_path).read_bytes()
    except OSError as exc:
        raise CameraInputError("旧profile读取失败：" + str(exc)) from exc
    profile_before = hashlib.sha256(profile_bytes).hexdigest()
    if profile_before != expected_profile_sha256:
        raise CameraInputError("旧profile SHA256与调用者expected不一致")
    try:
        profile = json.loads(profile_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CameraInputError("旧profile不是有效UTF-8 JSON") from exc
    validate_camera_profile(profile)
    library = profile.get("library") or {}
    for role in ("object_action", "data_action"):
        record = library.get(role) or {}
        if record.get("evaluation_signature_version") is not None:
            raise CameraInputError("目标不是旧弱签名profile；已是v2或未知版本，拒绝迁移")
        weak = record.get("signature")
        if not isinstance(weak, str) or len(weak) != 64:
            raise CameraInputError(f"旧profile缺少{role}弱签名，拒绝迁移")
    camera_name = library.get("camera_object")
    data_name = library.get("camera_data")
    object_action_name = (library.get("object_action") or {}).get("name")
    data_action_name = (library.get("data_action") or {}).get("name")
    for label, value in (
        ("camera_object", camera_name), ("camera_data", data_name),
        ("object_action", object_action_name), ("data_action", data_action_name),
    ):
        if not isinstance(value, str) or not value:
            raise CameraInputError(f"旧profile缺少明确的{label}")
    for role, expected_type in (("object_action", "OBJECT"),
                                ("data_action", "CAMERA")):
        declared_type = (library.get(role) or {}).get("slot_target_id_type")
        if declared_type is not None and declared_type != expected_type:
            raise CameraInputError(
                f"旧profile {role} slot_target_id_type 与期望{expected_type}不符")

    library_before = sha256_file(library_path)
    if library_before != expected_library_sha256:
        raise CameraInputError("旧library SHA256与调用者expected不一致")
    if output_path.exists():
        raise CameraInputError(f"迁移输出已存在，拒绝覆盖：{output_path}")

    selected_before = list(bpy.context.selected_objects)
    active_before = bpy.context.view_layer.objects.active
    scene_before = _migration_scene_state()
    old_actions_before = {
        action.name: _action_key_signature(action)
        for action in bpy.data.actions
        if action.name not in {object_action_name, data_action_name}
    }
    for existing_name, collection in (
        (camera_name, bpy.data.objects),
        (object_action_name, bpy.data.actions),
        (data_action_name, bpy.data.actions),
    ):
        if existing_name in collection:
            raise CameraInputError(
                f"当前文件已有同名ID {existing_name!r}；拒绝用同名认领迁移")
    if _migration_data_name_exists(data_name):
        raise CameraInputError(
            f"当前文件已有同名data ID {data_name!r}；拒绝用同名认领迁移")

    from . import module_configuration
    before_ids = module_configuration._ids()
    camera = None
    object_action = None
    data_action = None
    object_slot = None
    data_slot = None
    object_slot_id = None
    data_slot_id = None
    object_slot_type = None
    data_slot_type = None
    slot_sources = {}
    verification = None
    object_eval = None
    data_eval = None
    loaded_objects = []
    loaded_actions = []
    try:
        with bpy.data.libraries.load(library_path, link=False) as (data_from, data_to):
            for required in (camera_name,):
                if required not in data_from.objects:
                    raise CameraInputError(f"旧library缺少Object {required!r}")
            for required in (object_action_name, data_action_name):
                if required not in data_from.actions:
                    raise CameraInputError(f"旧library缺少Action {required!r}")
            data_to.objects = [camera_name]
            data_to.actions = [object_action_name, data_action_name]
        loaded_objects = [item for item in data_to.objects
                          if isinstance(item, bpy.types.Object)]
        loaded_actions = [item for item in data_to.actions
                          if isinstance(item, bpy.types.Action)]
        camera = next((item for item in loaded_objects if item.name == camera_name), None)
        object_action = next(
            (item for item in loaded_actions if item.name == object_action_name), None)
        data_action = next(
            (item for item in loaded_actions if item.name == data_action_name), None)
        if camera is None or object_action is None or data_action is None:
            raise CameraImportError(
                "旧library没有返回完整camera/object/data Action引用")
        object_slot, object_slot_source = _resolve_legacy_camera_slot(
            object_action, library.get("object_action") or {}, "OBJECT",
            "object_action")
        data_slot, data_slot_source = _resolve_legacy_camera_slot(
            data_action, library.get("data_action") or {}, "CAMERA",
            "data_action")
        if object_slot is None or data_slot is None:
            raise CameraImportError("旧library Action Slot缺失")
        object_slot_id = object_slot.identifier
        data_slot_id = data_slot.identifier
        object_slot_type = object_slot.target_id_type
        data_slot_type = data_slot.target_id_type
        slot_sources = {"object_action": object_slot_source,
                        "data_action": data_slot_source}
        verification = _verify_legacy_camera_library(
            profile, camera, object_action, data_action, object_slot, data_slot,
            slot_sources=slot_sources)
        object_eval = _camera_action_evaluation_signature(object_action, object_slot)
        data_eval = _camera_action_evaluation_signature(data_action, data_slot)
    finally:
        loaded_camera = camera
        loaded_object_action = object_action
        loaded_data_action = data_action
        try:
            roots = [
                loaded_camera,
                getattr(loaded_camera, "data", None),
                loaded_object_action,
                loaded_data_action,
                *loaded_objects,
                *loaded_actions,
            ]
            owned_ids = _camera_returned_import_ids(
                roots, before_ids, library_path)
            module_configuration._cleanup(
                before_ids, active_before, selected_before, owned_ids=owned_ids)
            live_ids = module_configuration._ids()
            remaining = []
            for item in owned_ids:
                try:
                    if item in live_ids:
                        remaining.append(
                            f"{type(item).__name__}:{getattr(item, 'name', '?')}")
                except ReferenceError:
                    pass
            if remaining:
                raise CameraImportError(
                    "本次迁移临时ID清理失败，拒绝发布sidecar："
                    + json.dumps(sorted(remaining), ensure_ascii=False))
        except Exception:
            _restore_selection(selected_before, active_before)
            raise

    if sha256_file(library_path) != library_before or sha256_file(profile_path) != profile_before:
        raise CameraInputError("旧library/profile在迁移验证期间发生变化")
    if _migration_scene_state() != scene_before:
        raise CameraInputError("迁移验证改变了Scene/selection/active camera状态")
    old_actions_after = {
        action.name: _action_key_signature(action)
        for action in bpy.data.actions
        if action.name in old_actions_before
    }
    if old_actions_after != old_actions_before:
        raise CameraImportError("迁移验证改变了外部既有Action")
    if output_path.exists():
        raise CameraInputError(f"迁移输出已存在，拒绝覆盖：{output_path}")

    new_profile = json.loads(json.dumps(profile))
    new_record = new_profile["library"]
    new_record["object_action"]["evaluation_signature_version"] = CAMERA_ACTION_EVAL_SIGNATURE_VERSION
    new_record["object_action"]["evaluation_signature"] = object_eval
    new_record["object_action"]["slot_identifier"] = object_slot_id
    new_record["object_action"]["slot_target_id_type"] = object_slot_type
    new_record["data_action"]["evaluation_signature_version"] = CAMERA_ACTION_EVAL_SIGNATURE_VERSION
    new_record["data_action"]["evaluation_signature"] = data_eval
    new_record["data_action"]["slot_identifier"] = data_slot_id
    new_record["data_action"]["slot_target_id_type"] = data_slot_type
    new_profile["migration"] = {
        "schema": MIGRATION_SCHEMA,
        "source_library": {"file": str(Path(library_path).resolve()),
                           "sha256": library_before},
        "source_profile": {"file": str(Path(profile_path).resolve()),
                           "sha256": profile_before,
                           "document_sha256": profile["profile_sha256"]},
        "source_vmd_recompared": False,
        "source_vmd_note": "迁移未重新对照原始VMD；仅验证旧库与旧profile的明确求值范围",
        "verified_scope": verification,
        "weak_curve_signatures": {
            "object": new_record["object_action"]["signature"],
            "data": new_record["data_action"]["signature"],
        },
        "evaluation_signature_version": CAMERA_ACTION_EVAL_SIGNATURE_VERSION,
    }
    new_profile["profile_sha256"] = profile_sha256(new_profile)

    created = False
    try:
        with output_path.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            handle.write(json.dumps(new_profile, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        loaded = load_camera_profile(str(output_path))
        if loaded.get("profile_sha256") != new_profile.get("profile_sha256"):
            raise CameraImportError("迁移新profile回读hash不一致")
        for role in ("object_action", "data_action"):
            record = loaded["library"][role]
            if (record.get("evaluation_signature_version") != CAMERA_ACTION_EVAL_SIGNATURE_VERSION
                    or record.get("evaluation_signature") !=
                    (object_eval if role == "object_action" else data_eval)):
                raise CameraImportError("迁移新profile回读v2签名不一致")
        if sha256_file(library_path) != library_before or sha256_file(profile_path) != profile_before:
            raise CameraInputError("迁移写后旧library/profile发生变化")
    except BaseException:
        if created:
            try:
                output_path.unlink()
            except OSError:
                pass
        raise

    return {
        "ok": True,
        "profile_path": str(output_path),
        "profile_sha256": new_profile["profile_sha256"],
        "library_path": str(Path(library_path).resolve()),
        "library_sha256": library_before,
        "source_profile_path": str(Path(profile_path).resolve()),
        "source_profile_sha256": profile_before,
        "source_vmd_recompared": False,
        "verification": verification,
        "object_evaluation_signature": object_eval,
        "data_evaluation_signature": data_eval,
        "migration": new_profile["migration"],
    }
