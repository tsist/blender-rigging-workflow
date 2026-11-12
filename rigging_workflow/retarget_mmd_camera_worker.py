# SPDX-License-Identifier: GPL-3.0-or-later
"""Executed only by public blenderctl extension.run for the camera UI.

The GUI never registers MMD Tools.  This worker loads the frozen camera core
copy from ``ui-delivery/runtime`` and lets that core perform the explicit
background MMD Tools register/import/unregister sequence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
import time
import traceback
from pathlib import Path

sys.dont_write_bytecode = True

import bpy


J = Path(job_directory)
P = parameters


def _write(name, value):
    (J / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _phase(value, **extra):
    _write(
        "output-progress.json",
        {
            "schema": "c94.retarget.mmd_camera_ui_progress.v1",
            "job_id": J.name,
            "phase": value,
            "timestamp": time.time(),
            **extra,
        },
    )


def _record(path):
    path = Path(path).resolve(strict=True)
    return {
        "file": str(path),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _load_core(path):
    path = Path(path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location(
        "c94_retarget_mmd_camera_ui_runtime", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载相机核心模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    _require(bpy.app.background, "相机 UI worker 只允许公共 CLI 后台作业")
    _phase("VALIDATE")
    core, core_path = _load_core(P["core_module"])
    worker_path = Path(__file__).resolve()
    vmd_path = Path(P["vmd"]).resolve(strict=True)
    addon_path = Path(P["addon"]).resolve(strict=True)
    opencc_path = Path(P["opencc_wheel"]).resolve(strict=True)
    target_fps = float(P["target_fps"])
    _require(
        math.isfinite(target_fps) and target_fps > 0.0,
        "target_fps 必须是有限正数",
    )

    backend = core.inspect_backend(
        addon_path=str(addon_path), opencc_wheel=str(opencc_path)
    )
    _write(
        "output-backend.json",
        {
            "schema": "c94.retarget.mmd_camera_ui_backend.v1",
            "available": backend.get("available"),
            "verified": backend.get("verified"),
            "version": backend.get("version"),
            "license": backend.get("license", []),
            "source_identity_sha256": backend.get("source_identity_sha256"),
            "checks": backend.get("checks", {}),
            "registration_state_before": backend.get("registration_state", {}),
            "gui_registration": False,
        },
    )
    _require(backend.get("available"), "MMD Tools 后端目录不可用")
    _require(backend.get("verified"), "MMD Tools 版本/许可/关键文件哈希未通过")

    _phase("INSPECT_VMD")
    vmd_inspection = core.inspect_vmd_camera(
        str(vmd_path), addon_path=str(addon_path), opencc_wheel=str(opencc_path)
    )
    _write(
        "output-vmd-inspection.json",
        {
            "schema": "c94.retarget.mmd_camera_ui_vmd_inspection.v1",
            "file": vmd_inspection.get("file"),
            "header": vmd_inspection.get("header"),
            "has_camera": bool(vmd_inspection.get("has_camera")),
            "camera_key_count": vmd_inspection.get("camera_key_count"),
            "camera_frame_range": vmd_inspection.get("camera_frame_range"),
            "perspective_events": vmd_inspection.get("perspective_events", []),
            "source_cuts": vmd_inspection.get("source_cuts", []),
            "structure": vmd_inspection.get("structure"),
            "gui_registration": False,
        },
    )
    _require(
        vmd_inspection.get("has_camera"),
        "VMD camera 通道为空（0 keys）；拒绝创建相机，未修改任何 Scene/Action/输入",
    )

    _phase("BUILD", target_fps=target_fps)
    built = core.build_camera_library(
        str(vmd_path),
        str(J),
        addon_path=str(addon_path),
        opencc_wheel=str(opencc_path),
        source_fps=float(P.get("source_fps", 30.0)),
        target_fps=target_fps,
        target_frame_origin=float(P.get("target_frame_origin", 0.0)),
        source_time_origin=float(P.get("source_time_origin", 0.0)),
        scale=float(P.get("scale", 0.08)),
        detect_camera_changes=bool(P.get("detect_camera_changes", True)),
        camera_name=P.get("camera_name", "C94_MMD_CAMERA"),
        library_scene_name=P.get("library_scene_name", "C94_MMD_CAMERA"),
        output_stem="output-camera",
        blend_name="output-camera.blend",
        profile_name="output-camera-profile.json",
        descriptor_name="output-camera-descriptor.json",
        overwrite=False,
        require_verified=True,
        batch_identity=P.get("batch_identity", {}),
        source_note=P.get("source_note"),
    )
    checks = built["checks"]
    comparison = checks["native_vs_official"]
    _require(built.get("ok"), "相机库构建未返回 ok")
    _require(checks["source_sha256_unchanged"], "源 VMD SHA 在构建后变化")
    _require(checks["source_bytes_unchanged"], "源 VMD bytes 在构建后变化")
    _require(checks["source_not_written"], "源 VMD 被写入")
    _require(checks["camera_channel_nonempty"], "camera 通道为空")
    _require(checks["camera_key_count_matches_structure"], "camera 数量不一致")
    _require(checks["camera_keys_sorted"], "camera keys 未排序")
    _require(checks["official_camera_import_used"], "未使用官方相机导入")
    _require(checks["official_imported_bezier_curve_count"] > 0, "未产生 Bezier 曲线")
    _require(checks["official_imported_perspective_constant"], "透视曲线不是 CONSTANT")
    _require(comparison["matrix_max_abs_error"] < 1e-5, "native/official matrix 误差超限")
    _require(comparison["lens_max_abs_error"] < 1e-5, "native/official lens 误差超限")
    _require(
        comparison["ortho_scale_max_abs_error"] < 1e-5,
        "native/official ortho_scale 误差超限",
    )
    _require(comparison["type_mismatch_count"] == 0, "native/official type 不一致")
    _require(
        checks["native_object_slot_target_id_type"] == "OBJECT",
        "Object Action slot 不是 OBJECT",
    )
    _require(
        checks["native_data_slot_target_id_type"] == "CAMERA",
        "Data Action slot 不是 CAMERA",
    )
    _require(checks["native_slot_types_distinct"], "Object/Data slot 类型未分离")
    _require(checks["native_camera_object_parent"] is None, "原生相机有 parent")
    _require(checks["native_driver_count"] == 0, "原生相机有 driver")
    _require(checks["native_has_mmd_rna"] is False, "原生相机有 MMD RNA")
    _require(
        checks["native_fcurve_paths_have_mmd_camera"] is False,
        "原生相机含 mmd_camera fcurve",
    )
    _require(checks["library_scene_active_camera_is_none"], "库 Scene 自动设置了活动镜头")
    _require(checks["existing_scenes_preserved"], "已有 Scene 被修改")
    _require(checks["selection_restored"], "selection 未恢复")
    _require(checks["active_object_restored"], "active object 未恢复")
    _require(checks["runtime_unregistered"], "MMD Tools 未注销")
    _require(checks["mmd_camera_rna_absent_after_build"], "构建后仍有 mmd_camera RNA")
    _require(
        abs(checks["target_fps_effective"] - target_fps) < 1e-6,
        "目标 Scene fps 与请求 target_fps 不一致",
    )
    _require(checks["timebase_mapping"] == "SECONDS", "时基不是 SECONDS")

    profile_path = Path(built["profile_path"]).resolve(strict=True)
    library_path = Path(built["blend_path"]).resolve(strict=True)
    descriptor_path = Path(built["descriptor_path"]).resolve(strict=True)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    _require(profile.get("profile_sha256") == built["profile_sha256"], "profile hash 不一致")
    _require(
        abs(float(profile["mapping"]["target_fps"]) - target_fps) < 1e-6,
        "profile target_fps 不一致",
    )
    receipt = {
        "schema": "c94.retarget.mmd_camera_ui_build_receipt.v1",
        "ok": True,
        "job_id": J.name,
        "worker": {
            "file": str(worker_path),
            "bytes": worker_path.stat().st_size,
            "sha256": hashlib.sha256(worker_path.read_bytes()).hexdigest(),
            "blender": bpy.app.version_string,
            "background": bool(bpy.app.background),
        },
        "core": {
            "file": str(core_path),
            "bytes": core_path.stat().st_size,
            "sha256": hashlib.sha256(core_path.read_bytes()).hexdigest(),
        },
        "backend": {
            "version": backend.get("version"),
            "license": backend.get("license", []),
            "verified": True,
            "source_identity_sha256": backend.get("source_identity_sha256"),
        },
        "vmd": {
            "file": vmd_inspection["file"],
            "header": vmd_inspection.get("header"),
            "camera_key_count": vmd_inspection.get("camera_key_count"),
            "camera_frame_range": vmd_inspection.get("camera_frame_range"),
            "perspective_events": vmd_inspection.get("perspective_events", []),
            "source_cuts": vmd_inspection.get("source_cuts", []),
            "structure": vmd_inspection.get("structure"),
        },
        "target_fps": target_fps,
        "mapping": built["mapping"],
        "library": built["library"],
        "checks": checks,
        "profile_sha256": built["profile_sha256"],
        "artifacts": {
            "blend": _record(library_path),
            "profile": _record(profile_path),
            "descriptor": _record(descriptor_path),
        },
        "current_gui_scene_accessed": False,
        "gui_mmd_tools_registration": False,
        "limitations": built["limitations"],
    }
    _write("output-build.json", receipt)
    _phase("COMPLETE", profile_sha256=built["profile_sha256"])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        error = traceback.format_exc()
        (J / "output-build-error.txt").write_text(error, encoding="utf-8", newline="\n")
        try:
            _write(
                "output-build.json",
                {
                    "schema": "c94.retarget.mmd_camera_ui_build_receipt.v1",
                    "ok": False,
                    "job_id": J.name,
                    "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
                },
            )
        except Exception:
            pass
        try:
            _phase("FAILED", error=str(exc)[:500])
        except Exception:
            pass
        raise
