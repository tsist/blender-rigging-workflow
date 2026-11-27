# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional MMD VMD camera UI child panel.

The GUI only edits explicit paths, submits a public ``blenderctl extension.run``
job, reads its lifecycle status, cancels through the same CLI, and appends the
exact resulting profile through the frozen native camera core.

MMD Tools is never imported or registered in the GUI.  The extension worker
owns the explicit background register/import/unregister sequence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper


OPTIONS = "c94_mmd_camera_options_v1"
UI_VERSION = "0.3.0"

_OWNED = False
_REGISTERED = []
_TIMER_REGISTERED = False
_CORE = None
_CORE_PATH = None

_UI_DIR = Path(__file__).resolve().parent
_WORKER_NAME = "retarget_mmd_camera_worker.py"
_DEFAULT_CLI = ""
_DEFAULT_ADDON = ""
_DEFAULT_OPENCC = ""

_ACTIVE = {"QUEUED", "RUNNING", "CANCELLING"}
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
_CLI_TERMINAL = {
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "timed_out",
    "partial",
}
_STATUS_LABEL = {
    "IDLE": "未提交",
    "QUEUED": "后台排队",
    "RUNNING": "后台处理中",
    "CANCELLING": "取消中",
    "SUCCEEDED": "构建成功",
    "FAILED": "失败",
    "CANCELLED": "已取消",
    "UNKNOWN": "状态未知",
}
_STATUS_ICON = {
    "IDLE": "TIME",
    "QUEUED": "TIME",
    "RUNNING": "TIME",
    "CANCELLING": "TIME",
    "SUCCEEDED": "CHECKMARK",
    "FAILED": "ERROR",
    "CANCELLED": "CANCEL",
    "UNKNOWN": "QUESTION",
}


def _require(value, message):
    if not value:
        raise ValueError(message)


def _opts(context):
    opts = getattr(context.scene, OPTIONS, None)
    if opts is None:
        raise RuntimeError("MMD 可选相机 UI 未注册或当前 Scene 无选项")
    return opts


def _scene_fps(scene):
    fps = float(scene.render.fps)
    base = float(scene.render.fps_base) if scene.render.fps_base else 1.0
    value = fps / base
    _require(math.isfinite(value) and value > 0.0, "当前 Scene fps 不是有限正数")
    return value


def _abs_path(value, kind, label):
    _require(isinstance(value, str) and value.strip(), f"请明确{label}")
    path = Path(bpy.path.abspath(value.strip())).expanduser()
    _require(path.is_absolute(), f"{label}必须是绝对路径：{value}")
    path = path.resolve()
    if kind == "file":
        _require(path.is_file(), f"{label}文件不存在：{path}")
    elif kind == "dir":
        _require(path.is_dir(), f"{label}目录不存在：{path}")
    return path


def _small_json(path, limit=32768):
    """Read a small CLI status/progress JSON without buffering a large file.

    This is a minimal adaptation of the verified share-compatible reader in
    ``work/retarget-mmd-file-entry-v001/staging/rigging_workflow/
    retarget_mmd_import_ui.py``.  It only reads; it does not delete or write.
    """
    path = Path(path)
    _require(path.is_file(), f"状态文件不存在：{path}")
    _require(path.stat().st_size <= limit, f"状态文件超过读取预算：{path}")
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateFileW(
            str(path.resolve()), 0x80000000, 7, None, 3, 0x80, None
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel32.CloseHandle(handle)
            raise
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(limit + 1)
    else:
        raw = path.read_bytes()
    _require(len(raw) <= limit, f"状态文件超过读取预算：{path}")
    return json.loads(raw.decode("utf-8-sig"))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _record(path):
    path = Path(path).resolve(strict=True)
    return {
        "file": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _powershell_exe():
    for name in ("pwsh.exe", "pwsh", "powershell.exe", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path(os.getenv("SystemRoot", "")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    if fallback.is_file():
        return str(fallback)
    raise RuntimeError("找不到 PowerShell，可显式修改 cli_path 所在环境的 PATH")


def _parse_cli_json(stdout, stderr=""):
    text = (stdout or "").lstrip("\ufeff").strip()
    if text:
        try:
            return json.loads(text)
        except Exception:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    pass
    detail = (stderr or "").strip()
    raise RuntimeError(
        "CLI 返回不是可解析 JSON"
        + (f"；stderr={detail[-300:]}" if detail else "")
        + (f"；stdout={text[-300:]}" if text else "")
    )


def _run_cli(cli_path, args, timeout=60):
    cli_path = Path(cli_path).resolve(strict=True)
    command = [
        _powershell_exe(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(cli_path),
        *[str(value) for value in args],
    ]
    kwargs = {
        "cwd": str(cli_path.parent),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": float(timeout),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"CLI 调用超时（{timeout}s）。本 UI 不杀进程；请检查同一 jobs-dir 的作业"
        ) from exc
    if completed.returncode != 0:
        try:
            parsed = _parse_cli_json(completed.stdout, completed.stderr)
        except Exception:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"CLI 失败（exit {completed.returncode}）：{detail[-500:]}")
        error = parsed.get("error")
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(
            f"CLI 失败（exit {completed.returncode}）：{message or 'unknown'}"
        )
    return _parse_cli_json(completed.stdout, completed.stderr)


def _cli_submit(cli_path, jobs_root, request_path, max_seconds):
    response = _run_cli(
        cli_path,
        [
            "--jobs-dir",
            str(jobs_root),
            "--timeout",
            str(int(max_seconds)),
            "--async",
            "request",
            str(request_path),
        ],
        timeout=60,
    )
    _require(response.get("ok"), "CLI 未接受后台提交")
    _require(response.get("job_id"), "CLI 未返回 job_id")
    return response


def _cli_status(cli_path, jobs_root, job_id):
    response = _run_cli(
        cli_path,
        ["--jobs-dir", str(jobs_root), "job", "status", str(job_id)],
        timeout=30,
    )
    _require(response.get("ok"), "CLI 状态查询失败")
    data = dict(response.get("data") or {})
    artifacts = response.get("artifacts") or {}
    if artifacts.get("job_directory"):
        data["job_directory"] = artifacts["job_directory"]
    return data


def _cli_cancel(cli_path, jobs_root, job_id):
    response = _run_cli(
        cli_path,
        ["--jobs-dir", str(jobs_root), "job", "cancel", str(job_id)],
        timeout=30,
    )
    _require(response.get("ok"), "CLI 取消请求失败")
    return response


def _load_module_from_path(path, name):
    path = Path(path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _core_module():
    global _CORE, _CORE_PATH
    if _CORE is not None and _CORE_PATH is not None:
        return _CORE, _CORE_PATH
    if __package__:
        try:
            from . import retarget_mmd_camera as core

            _CORE = core
            _CORE_PATH = Path(core.__file__).resolve(strict=True)
            return _CORE, _CORE_PATH
        except Exception:
            pass
    candidates = [
        _UI_DIR / "runtime" / "rigging_workflow" / "retarget_mmd_camera.py",
        Path(__file__).with_name("retarget_mmd_camera.py"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            core = _load_module_from_path(
                candidate, "c94_retarget_mmd_camera_ui_core"
            )
            if not callable(getattr(core, "load_camera_profile", None)):
                continue
            _CORE = core
            _CORE_PATH = candidate.resolve(strict=True)
            return _CORE, _CORE_PATH
    raise RuntimeError(
        "找不到相机核心模块；请把 runtime/rigging_workflow/retarget_mmd_camera.py "
        "与 UI 一起交付，或由 root 作为包模块安装"
    )


def _worker_path(opts=None):
    if opts is not None and getattr(opts, "worker_path", "").strip():
        path = _abs_path(opts.worker_path, "file", "Worker 脚本")
        if path.name != _WORKER_NAME:
            raise RuntimeError(f"Worker 脚本名必须是 {_WORKER_NAME}：{path}")
        return path
    candidates = [
        Path(__file__).with_name(_WORKER_NAME),
        _UI_DIR / _WORKER_NAME,
    ]
    seen = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise RuntimeError(
        "找不到 retarget_mmd_camera_worker.py；请与 UI 同目录交付或在高级路径中显式选择"
    )


def _add_resource(resources, seen, path):
    path = Path(path).resolve(strict=True)
    key = str(path)
    if key in seen:
        return
    seen.add(key)
    resources.append({"file": key, "expected_sha256": _sha256(path)})


def _identity():
    return {
        "batch_id": "c94-mmd-camera-20261111",
        "role": "implementer",
        "shard": "optional-native-camera",
        "attempt": 1,
        "ui_entry": "C94CAMERA_PT_panel",
    }


def _request_document(opts, scene):
    vmd = _abs_path(opts.vmd_path, "file", "VMD 文件")
    _require(vmd.suffix.lower() == ".vmd", f"VMD 文件扩展名不是 .vmd：{vmd}")
    addon = _abs_path(opts.backend_path, "dir", "MMD Tools 目录")
    opencc = _abs_path(opts.opencc_path, "file", "OpenCC wheel")
    cli_path = _abs_path(opts.cli_path, "file", "blenderctl.ps1")
    jobs_root = _abs_path(opts.jobs_root, "dir", "后台作业目录")
    if opts.worker_path.strip():
        worker = _worker_path(opts)
    else:
        worker = _worker_path()
    core, core_path = _core_module()
    target_fps = _scene_fps(scene)

    resources = []
    seen = set()
    required_backend = [
        addon / "blender_manifest.toml",
        addon / "LICENSE",
        addon / "__init__.py",
        addon / "core" / "vmd" / "__init__.py",
        addon / "core" / "vmd" / "importer.py",
        addon / "core" / "camera.py",
        addon / "properties" / "camera.py",
        addon / "bpyutils.py",
    ]
    for path in [worker, core_path, vmd, opencc, *required_backend]:
        _add_resource(resources, seen, path)

    source_note = ""
    if opts.artificial_note:
        source_note = (
            "ARTIFICIAL_FIXTURE declared by UI; generated/selected for technical "
            "verification only, not original MMD camera footage."
        )
    params = {
        "core_module": str(core_path),
        "vmd": str(vmd),
        "addon": str(addon),
        "opencc_wheel": str(opencc),
        "source_fps": 30.0,
        "target_fps": target_fps,
        "target_frame_origin": 0.0,
        "source_time_origin": 0.0,
        "scale": 0.08,
        "detect_camera_changes": True,
        "camera_name": "C94_MMD_CAMERA",
        "library_scene_name": "C94_MMD_CAMERA",
        "batch_identity": {
            **_identity(),
            "ui_target_fps": target_fps,
            "ui_artificial_fixture": bool(opts.artificial_note),
        },
        "source_note": source_note,
        "ui_entry": "C94CAMERA_PT_panel",
        "ui_artificial_fixture": bool(opts.artificial_note),
    }
    manifest = {
        "trust": "execute_local_python_without_sandbox",
        "script": {"file": str(worker), "expected_sha256": _sha256(worker)},
        "params": params,
        "resource_max_bytes": 1073741824,
        "outputs": [
            "output-backend.json",
            "output-vmd-inspection.json",
            "output-progress.json",
            "output-build.json",
            "output-camera.blend",
            "output-camera-profile.json",
            "output-camera-descriptor.json",
        ],
    }
    document = {
        "schema_version": "1.0",
        "command": "extension.run",
        "params": {"resources": resources, "manifest": manifest},
    }
    return document, {
        "vmd": vmd,
        "addon": addon,
        "opencc": opencc,
        "cli": cli_path,
        "jobs_root": jobs_root,
        "worker": worker,
        "core": core_path,
        "target_fps": target_fps,
    }


def _write_request(opts, scene):
    document, info = _request_document(opts, scene)
    requests_dir = info["jobs_root"] / "requests"
    requests_dir.mkdir(parents=True, exist_ok=True)
    name = f"camera-{int(time.time())}-{uuid.uuid4().hex[:10]}.json"
    request_path = requests_dir / name
    request_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return request_path, info


def _read_job_error(job_dir):
    if not job_dir or not Path(job_dir).is_dir():
        return ""
    job_dir = Path(job_dir)
    text_path = job_dir / "output-build-error.txt"
    if text_path.is_file():
        try:
            with text_path.open("rb") as fh:
                raw = fh.read(32768)
            return raw.decode("utf-8", errors="replace")[-1000:]
        except Exception:
            pass
    for name in ("output-build.json", "result.json"):
        path = job_dir / name
        if not path.is_file():
            continue
        try:
            document = _small_json(path, 4 * 1024 * 1024)
            error = document.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:1000]
        except Exception:
            pass
    return ""


def _read_progress(job_dir):
    if not job_dir:
        return ""
    path = Path(job_dir) / "output-progress.json"
    if not path.is_file():
        return ""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return str(doc.get("phase", ""))[:120]
    except Exception:
        return ""


def _collect_success(opts):
    task = _frozen_task(opts)
    _require(
        opts.job_terminal and opts.job_state == "succeeded",
        "任务不是成功终态；不能收集 profile",
    )
    opts.profile_collected = False
    job_dir = task["directory"]
    receipt_path = job_dir / "output-build.json"
    _require(receipt_path.is_file(), f"构建 receipt 不存在：{receipt_path}")
    receipt = _small_json(receipt_path, 4 * 1024 * 1024)
    _require(receipt.get("ok") is True, "构建 receipt 不是 ok=true")
    _require(receipt.get("job_id") == task["job_id"],
             "构建 receipt 属于其他作业；冻结任务保持，请核对该作业输出")
    checks = receipt.get("checks") or {}
    comparison = checks.get("native_vs_official") or {}
    _require(checks.get("source_sha256_unchanged"), "源 VMD SHA 未保持")
    _require(checks.get("source_bytes_unchanged"), "源 VMD bytes 未保持")
    _require(checks.get("native_driver_count") == 0, "原生相机含 driver")
    _require(checks.get("native_has_mmd_rna") is False, "原生相机含 MMD RNA")
    _require(comparison.get("matrix_max_abs_error", 1e9) < 1e-5, "matrix 误差超限")
    _require(comparison.get("lens_max_abs_error", 1e9) < 1e-5, "lens 误差超限")
    _require(
        comparison.get("ortho_scale_max_abs_error", 1e9) < 1e-5,
        "ortho_scale 误差超限",
    )
    _require(comparison.get("type_mismatch_count") == 0, "type mismatch")
    _require(checks.get("existing_scenes_preserved"), "已有 Scene 被修改")
    _require(checks.get("runtime_unregistered"), "MMD Tools 未注销")
    _require(
        checks.get("mmd_camera_rna_absent_after_build"),
        "构建后仍有 mmd_camera RNA",
    )
    artifacts = receipt.get("artifacts") or {}
    for key in ("blend", "profile", "descriptor"):
        _require(key in artifacts, f"receipt 缺 artifact {key}")
        artifact_path = Path(artifacts[key]["file"]).resolve(strict=True)
        expected_name = {"blend": "output-camera.blend", "profile": "output-camera-profile.json",
                         "descriptor": "output-camera-descriptor.json"}[key]
        _require(artifact_path == job_dir / expected_name,
                 f"artifact {key} 不属于冻结作业的明确输出；拒绝跨作业收集")
        actual = _record(artifact_path)
        _require(
            actual["sha256"] == artifacts[key]["sha256"]
            and actual["bytes"] == artifacts[key]["bytes"],
            f"artifact {key} SHA/bytes 不一致",
        )
    profile_path = Path(artifacts["profile"]["file"])
    profile_sha = receipt.get("profile_sha256")
    core, _ = _core_module()
    profile = core.load_camera_profile(str(profile_path))
    _require(profile.get("profile_sha256") == profile_sha, "profile hash 不一致")
    _require(
        abs(float(profile["mapping"]["target_fps"]) - float(opts.job_target_fps))
        < 1e-6,
        "profile target_fps 与提交时当前 Scene fps 不一致",
    )
    opts.profile_path = str(profile_path)
    opts.library_path = str(Path(artifacts["blend"]["file"]))
    opts.descriptor_path = str(Path(artifacts["descriptor"]["file"]))
    opts.profile_sha256 = profile_sha
    opts.library_sha256 = str(artifacts["blend"]["sha256"])
    opts.migration_expected_library_sha256 = str(artifacts["blend"]["sha256"])
    opts.migration_expected_profile_sha256 = str(artifacts["profile"]["sha256"])
    opts.build_summary = json.dumps(
        {
            "target_fps": profile["mapping"]["target_fps"],
            "sample_count": profile["mapping"]["sample_count"],
            "key_frames": profile["mapping"]["key_target_frames"],
            "mid_frames": profile["mapping"]["mid_target_frames"],
            "matrix_error": comparison.get("matrix_max_abs_error"),
            "lens_error": comparison.get("lens_max_abs_error"),
            "ortho_error": comparison.get("ortho_scale_max_abs_error"),
            "type_mismatch": comparison.get("type_mismatch_count"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    opts.status = "SUCCEEDED"
    opts.profile_collected = True
    opts.status_message = "成功结果已收集；profile/hash 已核对；可 Append"
    opts.error = ""


def _frozen_task(opts, require_cli=False):
    """Return frozen identity; CLI/root execution availability is checked only
    when an operation actually executes the CLI (cancel).

    Status, collect and release read the frozen job directory/status and must
    remain usable when the recorded CLI path has since disappeared.
    """

    _require(opts.job_id.strip(), "没有冻结的 job_id")
    _require(bool(re.fullmatch(r"[0-9a-f]{32}", opts.job_id)), "冻结 job_id 格式不符")
    _require(opts.job_directory.strip(), "没有冻结的 job_directory")
    _require(opts.job_jobs_root.strip(), "没有冻结的 jobs root")
    cli_text = opts.job_cli_path.strip()
    cli = (
        Path(bpy.path.abspath(cli_text)).expanduser().resolve()
        if cli_text
        else None
    )
    jobs_root = Path(bpy.path.abspath(opts.job_jobs_root)).expanduser().resolve()
    directory = Path(bpy.path.abspath(opts.job_directory)).expanduser().resolve()
    if cli is not None:
        _require(cli.is_absolute(), f"冻结 CLI 必须是绝对路径：{cli}")
    _require(jobs_root.is_absolute(), f"冻结 jobs root 必须是绝对路径：{jobs_root}")
    _require(directory.is_absolute() and directory.is_dir(), f"冻结 job 目录不存在：{directory}")
    _require(
        directory.parent == jobs_root and directory.name == opts.job_id,
        "冻结 job_directory 与冻结 jobs root/job id 不符",
    )
    if require_cli:
        _require(cli is not None, "没有冻结的 job CLI")
        _require(cli is not None and cli.is_file(), f"冻结 CLI 不存在：{cli}")
        _require(jobs_root.is_dir(), f"冻结 jobs root 不存在：{jobs_root}")
    return {
        "job_id": opts.job_id,
        "directory": directory,
        "cli": cli,
        "jobs_root": jobs_root,
    }


def poll_task(opts, include_error=False, force=False):
    """Lightweight frozen status/progress read; no process, hash or profile parse."""

    if not opts.job_id:
        return None
    if opts.job_terminal and not force:
        return {
            "state": opts.job_state,
            "terminal": True,
            "phase": opts.phase,
            "job_id": opts.job_id,
            "job_directory": opts.job_directory,
        }
    try:
        task = _frozen_task(opts)
        status = _small_json(task["directory"] / "status.json")
        _require(
            status.get("job_id") == task["job_id"],
            "冻结 job 的 status.json job_id 不符",
        )
        state = str(status.get("state", "")).lower()
        terminal = state in _CLI_TERMINAL
        opts.job_state = state
        opts.job_terminal = terminal
        opts.job_status_read_error = ""

        # Progress is optional decoration.  A damaged/oversized progress file
        # must not override a valid status, especially a terminal one.
        phase = ""
        progress_error = ""
        progress_path = task["directory"] / "output-progress.json"
        if progress_path.is_file():
            try:
                progress = _small_json(progress_path)
                phase = str(progress.get("phase", "") or "")[:120]
            except Exception as exc:  # progress is best-effort only
                progress_error = str(exc)[:300]
        opts.progress_read_error = progress_error
        if progress_error:
            opts.phase = "阶段未知"
        elif phase:
            opts.phase = phase
        else:
            opts.phase = ""
        progress_note = "；progress 不可读，阶段未知" if progress_error else ""
        detail = phase or "阶段未知"
        if progress_error:
            detail = "阶段未知（progress 不可读）"
        if state == "queued":
            if opts.cancel_requested:
                opts.status = "CANCELLING"
                opts.status_message = "取消请求已提交，等待 CLI 收尾" + progress_note
            else:
                opts.status = "QUEUED"
                opts.status_message = "后台已排队" + progress_note
        elif state == "running":
            if opts.cancel_requested:
                opts.status = "CANCELLING"
                opts.status_message = "取消请求已提交，等待 CLI 收尾" + progress_note
            else:
                opts.status = "RUNNING"
                opts.status_message = f"后台处理中：{detail}"
        elif state in {"cancelling", "cancel_requested"}:
            opts.status = "CANCELLING"
            opts.status_message = "取消请求已提交，等待 CLI 收尾" + progress_note
        elif state == "succeeded":
            opts.status = "SUCCEEDED"
            if opts.profile_collected:
                opts.status_message = "构建成功；profile 已收集；可 Append" + progress_note
            else:
                opts.status_message = "后台成功；请点“收集成功结果”" + progress_note
            opts.error = ""
        elif state == "cancelled":
            opts.status = "CANCELLED"
            opts.status_message = "已取消；成功输出不可用" + progress_note
        elif state in {"failed", "interrupted", "timed_out", "partial"}:
            opts.status = "FAILED"
            opts.status_message = (
                f"后台终态：{state}；请点“收集/查看结果”" + progress_note
            )
        else:
            opts.status = "UNKNOWN"
            opts.status_message = (
                f"未知 CLI 状态：{state or 'empty'}；保留冻结 job，不能释放"
                + progress_note
            )
        if include_error and terminal and state != "succeeded":
            opts.error = _read_job_error(task["directory"])
        return {
            "state": state,
            "terminal": terminal,
            "phase": phase,
            "progress_error": progress_error,
            "job_id": task["job_id"],
            "job_directory": str(task["directory"]),
        }
    except Exception as exc:
        opts.job_status_read_error = str(exc)[:300]
        opts.job_terminal = False
        opts.progress_read_error = ""
        opts.status = "UNKNOWN"
        opts.status_message = "状态读取失败；冻结 job 保留，不能释放"
        raise


def _poll_job(opts, force=False):
    """Compatibility alias for explicit refresh; the timer must call poll_task."""

    return poll_task(opts, include_error=True, force=force)


def _collect_result(opts):
    _require(opts.job_id.strip(), "没有冻结的 job")
    opts.profile_collected = False
    poll_task(opts, include_error=True, force=True)
    if opts.job_state != "succeeded":
        if opts.job_terminal:
            opts.error = _read_job_error(opts.job_directory)
            opts.status_message = f"任务终态 {opts.job_state}；未收集 profile"
            return {
                "ok": False,
                "state": opts.job_state,
                "error": opts.error,
            }
        raise RuntimeError("任务尚未成功终态；不能收集 profile")
    _collect_success(opts)
    opts.profile_collected = True
    opts.status_message = "成功结果已收集；profile/hash 已核对；可 Append"
    opts.error = ""
    return {
        "ok": True,
        "state": opts.job_state,
        "profile": opts.profile_path,
        "profile_sha256": opts.profile_sha256,
    }


def _action_signature(action):
    if action is None:
        return ""
    payload = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for fcurve in bag.fcurves:
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
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _rollback_owned_append(
    camera_name,
    camera_data_name,
    object_action_name,
    data_action_name,
    before_objects,
    before_cameras,
    before_actions,
):
    """Remove only exact profile-named IDs that did not exist before Append."""

    if camera_name not in before_objects:
        obj = bpy.data.objects.get(camera_name)
        if obj is not None:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
    # Remove the owned camera data before actions: while camera data still
    # exists it keeps its Data Action at users>0, which would otherwise leave
    # an orphaned owned Action behind.
    if camera_data_name not in before_cameras:
        data = bpy.data.cameras.get(camera_data_name)
        if data is not None and data.users == 0:
            try:
                bpy.data.cameras.remove(data)
            except Exception:
                pass
    for action_name in (object_action_name, data_action_name):
        if action_name in before_actions:
            continue
        action = bpy.data.actions.get(action_name)
        if action is None:
            continue
        try:
            action.use_fake_user = False
        except Exception:
            pass
        if action.users == 0:
            try:
                bpy.data.actions.remove(action)
            except Exception:
                pass


def _start_build(opts, scene):
    if opts.job_id:
        raise RuntimeError(
            "已有冻结后台作业；请先收集/取消/等待并释放，不能覆盖旧 job"
        )
    request_path, info = _write_request(opts, scene)
    response = _cli_submit(
        info["cli"], info["jobs_root"], request_path, opts.max_seconds
    )
    job_id = str(response.get("job_id", ""))
    _require(bool(re.fullmatch(r"[0-9a-f]{32}", job_id)), "CLI 未返回确切 job ID")
    jobs_root = Path(info["jobs_root"]).resolve(strict=True)
    cli_path = Path(info["cli"]).resolve(strict=True)
    job_directory = Path(
        (response.get("artifacts") or {}).get("job_directory", "")
    ).resolve(strict=True)
    _require(
        job_directory == (jobs_root / job_id).resolve(),
        "CLI 返回的 job_directory 与冻结 jobs root/job id 不符",
    )
    opts.job_id = job_id
    opts.job_directory = str(job_directory)
    opts.job_cli_path = str(cli_path)
    opts.job_jobs_root = str(jobs_root)
    opts.request_path = str(request_path)
    opts.job_target_fps = float(info["target_fps"])
    opts.status = "QUEUED"
    opts.job_state = "queued"
    opts.job_terminal = False
    opts.job_status_read_error = ""
    opts.progress_read_error = ""
    opts.cancel_requested = False
    opts.status_message = (
        f"已提交后台构建；冻结 job {job_id}；目标 fps={info['target_fps']:g}（当前 Scene，未修改）"
    )
    opts.profile_path = ""
    opts.library_path = ""
    opts.descriptor_path = ""
    opts.profile_sha256 = ""
    opts.library_sha256 = ""
    opts.build_summary = ""
    opts.error = ""
    opts.phase = "SUBMITTED"
    opts.profile_collected = False
    try:
        poll_task(opts, include_error=False, force=True)
    except Exception:
        pass
    return response


def _inspect_paths(opts):
    core, _ = _core_module()
    results = []
    if opts.vmd_path.strip():
        vmd = _abs_path(opts.vmd_path, "file", "VMD 文件")
        inspection = core.inspect_vmd_camera(
            str(vmd), addon_path=opts.backend_path, opencc_wheel=opts.opencc_path
        )
        opts.vmd_status = (
            f"camera keys={inspection['camera_key_count']} "
            f"range={inspection.get('camera_frame_range')} "
            f"persp_events={len(inspection.get('perspective_events', []))} "
            f"cuts={len(inspection.get('source_cuts', []))}"
        )
        results.append(opts.vmd_status)
    else:
        opts.vmd_status = "未选择 VMD"
    backend = core.inspect_backend(
        addon_path=opts.backend_path, opencc_wheel=opts.opencc_path
    )
    opts.backend_status = (
        f"{backend.get('version')} verified={backend.get('verified')} "
        f"license={','.join(backend.get('license', []))}"
    )
    results.append(opts.backend_status)
    return "；".join(results)


def _cancel_job(opts):
    task = _frozen_task(opts, require_cli=True)
    if opts.job_terminal:
        raise RuntimeError("任务已终态，无需取消")
    response = _cli_cancel(task["cli"], task["jobs_root"], task["job_id"])
    data = response.get("data") or {}
    state = str(data.get("state", "cancelling")).lower()
    if state == "cancelled":
        opts.status = "CANCELLED"
        opts.job_state = "cancelled"
        opts.job_terminal = True
        opts.status_message = "已取消；CLI 已收尾"
    else:
        opts.status = "CANCELLING"
        opts.job_state = "cancelling"
        opts.job_terminal = False
        opts.status_message = "取消请求已提交；由 CLI 生命周期收尾，本 UI 不杀进程"
    opts.cancel_requested = True
    opts.error = ""
    try:
        poll_task(opts, include_error=True, force=True)
    except Exception:
        pass
    return response


def _release_job(opts):
    poll_task(opts, include_error=True, force=True)
    _require(
        opts.job_terminal,
        "状态非明确终态（可能仍在活动或读取失败）；不能释放冻结 job",
    )
    opts.job_id = ""
    opts.job_directory = ""
    opts.job_cli_path = ""
    opts.job_jobs_root = ""
    opts.job_state = ""
    opts.job_terminal = False
    opts.job_status_read_error = ""
    opts.progress_read_error = ""
    opts.cancel_requested = False
    opts.request_path = ""
    opts.status = "IDLE"
    opts.status_message = "已释放界面任务；磁盘输出与已 Append 相机保留"
    opts.error = ""
    opts.phase = ""


def _forget_job(opts):
    """Forget an unreadable UI-only handle; never cancel or delete anything."""
    _require(bool(opts.job_id.strip()), "没有可遗忘的冻结 job 句柄")
    try:
        poll_task(opts, include_error=False, force=True)
    except Exception:
        pass
    else:
        raise RuntimeError(
            "冻结 job 状态仍可读取；请使用取消或释放界面任务，不能遗忘活动/可读任务"
        )
    opts.job_id = ""
    opts.job_directory = ""
    opts.job_cli_path = ""
    opts.job_jobs_root = ""
    opts.job_state = ""
    opts.job_terminal = False
    opts.job_status_read_error = ""
    opts.progress_read_error = ""
    opts.cancel_requested = False
    opts.request_path = ""
    opts.job_target_fps = 0.0
    opts.status = "UNKNOWN"
    opts.phase = ""
    opts.error = ""
    opts.status_message = (
        "已仅遗忘丢失的界面句柄；未取消进程、未删除目录/输出。"
        "若后台仍在运行，请用外部 CLI 核对 job 状态"
    )


def _append_profile(opts, scene):
    if opts.job_id and not opts.job_terminal:
        raise RuntimeError("后台构建仍在运行；请等待终态后再 Append")
    _require(
        opts.profile_collected,
        "成功结果尚未收集；请先点“收集成功结果”，不会在 timer 中收集",
    )
    _require(opts.profile_path.strip(), "没有可 Append 的 profile；请先成功构建")
    _require(opts.library_path.strip(), "没有 camera library blend")
    profile_path = _abs_path(opts.profile_path, "file", "profile")
    library_path = _abs_path(opts.library_path, "file", "camera library")
    if opts.library_sha256.strip():
        _require(
            _sha256(library_path) == opts.library_sha256,
            "camera library SHA 与成功 job 记录不一致；拒绝 Append",
        )
    scene_fps = _scene_fps(scene)
    _require(
        opts.job_target_fps > 0.0,
        "提交时未记录目标 fps；请重新构建，不猜测 Scene fps",
    )
    _require(
        abs(scene_fps - float(opts.job_target_fps)) < 1e-6,
        f"当前 Scene fps={scene_fps:g} 与构建目标 fps={opts.job_target_fps:g} 不符；"
        "本 UI 不修改 Scene fps，请恢复 fps 或按当前 fps 重新构建",
    )
    core, _ = _core_module()
    profile = core.load_camera_profile(str(profile_path))
    _require(
        profile.get("profile_sha256") == opts.profile_sha256,
        "profile 与 UI 记录不一致",
    )
    _require(
        abs(float(profile["mapping"]["target_fps"]) - scene_fps) < 1e-6,
        "profile target_fps 与当前 Scene fps 不符",
    )
    camera_name = profile["library"]["camera_object"]
    camera_data_name = profile["library"]["camera_data"]
    object_action_name = profile["library"]["object_action"]["name"]
    data_action_name = profile["library"]["data_action"]["name"]
    _require(
        camera_name not in bpy.data.objects,
        f"当前文件已存在 Object {camera_name!r}；拒绝二次 Append/覆盖旧相机",
    )
    _require(
        camera_data_name not in bpy.data.cameras,
        f"当前文件已存在 Camera data {camera_data_name!r}；拒绝二次 Append",
    )
    _require(
        object_action_name not in bpy.data.actions
        and data_action_name not in bpy.data.actions,
        "当前文件已存在同名 camera Action；拒绝二次 Append/覆盖旧 Action",
    )
    before_camera = scene.camera.name if scene.camera else ""
    before_camera_obj = scene.camera
    before_objects = set(bpy.data.objects.keys())
    before_cameras = set(bpy.data.cameras.keys())
    before_actions = set(bpy.data.actions.keys())
    old_object_action = (
        before_camera_obj.animation_data.action
        if before_camera_obj is not None and before_camera_obj.animation_data
        else None
    )
    old_data_action = (
        before_camera_obj.data.animation_data.action
        if before_camera_obj is not None and before_camera_obj.data.animation_data
        else None
    )
    old_object_signature = _action_signature(old_object_action)
    old_data_signature = _action_signature(old_data_action)
    try:
        result = core.append_camera_library(
            str(library_path),
            profile,
            scene=scene,
            camera_name=camera_name,
        )
        _require(
            result.get("old_actions_preserved") is True,
            "core 未确认旧 Action 保持；回滚本次新增相机",
        )
        _require(
            result.get("scene_state_preserved") is True,
            "core 未确认 Scene 状态保持；回滚本次新增相机",
        )
        _require(
            bool(result.get("camera_object")),
            "core 未返回 camera_object；回滚本次新增相机",
        )
        after_camera = scene.camera.name if scene.camera else ""
        _require(
            before_camera == after_camera,
            "append 后 Scene.camera 发生变化；本 UI 拒绝自动替换活动镜头",
        )
        _require(
            _action_signature(old_object_action) == old_object_signature
            and _action_signature(old_data_action) == old_data_signature,
            "Append 修改了旧相机 Action；拒绝签收并回滚本次新增相机",
        )
    except BaseException:
        # Restore the active camera before removing the newly appended object so
        # Scene.camera never dangles.  Roll back only exact, profile-named IDs
        # that were absent before this append.
        if scene.camera is not before_camera_obj:
            scene.camera = before_camera_obj
        _rollback_owned_append(
            camera_name,
            camera_data_name,
            object_action_name,
            data_action_name,
            before_objects,
            before_cameras,
            before_actions,
        )
        raise
    opts.appended_camera = result["camera_object"]
    opts.previous_active_camera = ""
    opts.restore_owner_camera = ""
    opts.restore_owner_scene = ""
    opts.status_message = (
        f"已 Append {result['camera_object']}；旧活动镜头仍为 "
        f"{before_camera or 'None'}（未自动替换）"
    )
    opts.error = ""
    return result


def _restore_available(opts, scene):
    """True only when restore is safe for the current explicit camera state."""

    if not opts.appended_camera.strip() or not opts.previous_active_camera.strip():
        return False
    if opts.restore_owner_camera.strip() != opts.appended_camera.strip():
        return False
    if opts.restore_owner_scene.strip() != scene.name:
        return False
    appended = bpy.data.objects.get(opts.appended_camera)
    previous = bpy.data.objects.get(opts.previous_active_camera)
    return (
        appended is not None
        and appended.type == "CAMERA"
        and appended.name in scene.objects
        and previous is not None
        and previous.type == "CAMERA"
        and previous.name in scene.objects
        and previous is not appended
        and scene.camera is appended
    )


def _set_active_camera(opts, scene):
    _require(opts.appended_camera.strip(), "尚未 Append 相机")
    camera = bpy.data.objects.get(opts.appended_camera)
    _require(camera is not None and camera.type == "CAMERA", "Append 相机不存在")
    _require(camera.name in scene.objects, "Append 相机不在当前 Scene")
    if scene.camera is not camera:
        previous_name = scene.camera.name if scene.camera else ""
        opts.previous_active_camera = previous_name
        opts.restore_owner_camera = camera.name if previous_name else ""
        opts.restore_owner_scene = scene.name if previous_name else ""
    scene.camera = camera
    if opts.previous_active_camera:
        opts.status_message = (
            f"已手动设为活动镜头：{camera.name}；此前 "
            f"{opts.previous_active_camera} 可一键恢复"
        )
    else:
        opts.status_message = f"已手动设为活动镜头：{camera.name}；没有此前镜头记录"
    opts.error = ""


def _restore_active_camera(opts, scene):
    _require(opts.appended_camera.strip(), "没有 Append 相机记录")
    _require(opts.previous_active_camera.strip(), "没有记录此前活动镜头")
    _require(
        opts.restore_owner_camera.strip() == opts.appended_camera.strip(),
        "restore 归属记录无效；为保护用户后续选择，拒绝恢复",
    )
    _require(
        opts.restore_owner_scene.strip() == scene.name,
        "restore 归属 Scene 不符；为保护用户后续选择，拒绝恢复",
    )
    appended = bpy.data.objects.get(opts.appended_camera)
    _require(
        appended is not None and appended.type == "CAMERA",
        "Append 相机不存在",
    )
    _require(appended.name in scene.objects, "Append 相机不在当前 Scene")
    current = scene.camera
    current_name = current.name if current else "None"
    _require(
        current is appended,
        f"当前 Scene.camera={current_name} 不是本次 Append 相机 "
        f"{opts.appended_camera}；为保护用户后续选择，拒绝恢复",
    )
    previous = bpy.data.objects.get(opts.previous_active_camera)
    _require(
        previous is not None and previous.type == "CAMERA",
        "此前活动镜头对象已不存在",
    )
    _require(previous.name in scene.objects, "此前活动镜头不在当前 Scene")
    _require(previous is not appended, "restore 记录指向 Append 相机自身；拒绝恢复")
    scene.camera = previous
    restored = previous.name
    opts.previous_active_camera = ""
    opts.restore_owner_camera = ""
    opts.restore_owner_scene = ""
    opts.status_message = f"已恢复此前活动镜头：{restored}；restore 记录已清理"
    opts.error = ""


def _redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


def _tick():
    if not _OWNED:
        return None
    try:
        scene = bpy.context.scene
        opts = getattr(scene, OPTIONS, None) if scene is not None else None
        if opts is not None and opts.job_id and not opts.job_terminal:
            try:
                # Lightweight frozen-status read only: no subprocess, no hash,
                # no camera profile/receipt collection.
                poll_task(opts, include_error=False, force=False)
            except Exception as exc:  # noqa: BLE001
                opts.job_status_read_error = str(exc)[:300]
                opts.job_terminal = False
                opts.status = "UNKNOWN"
                opts.status_message = "状态读取失败；冻结 job 保留，不能释放"
            _redraw()
    except Exception:
        pass
    return 2.0


class C94CAMERA_Options(bpy.types.PropertyGroup):
    vmd_path: StringProperty(name="VMD 文件", subtype="FILE_PATH", default="")
    backend_path: StringProperty(
        name="MMD Tools 目录", subtype="DIR_PATH", default=_DEFAULT_ADDON
    )
    opencc_path: StringProperty(
        name="OpenCC wheel", subtype="FILE_PATH", default=_DEFAULT_OPENCC
    )
    cli_path: StringProperty(
        name="blenderctl.ps1", subtype="FILE_PATH", default=_DEFAULT_CLI
    )
    worker_path: StringProperty(
        name="Worker 脚本（留空自动）", subtype="FILE_PATH", default=""
    )
    jobs_root: StringProperty(
        name="后台作业目录", subtype="DIR_PATH", default=""
    )
    max_seconds: IntProperty(name="超时秒", default=900, min=30, max=86400)
    artificial_note: BoolProperty(
        name="人工 fixture（明示）",
        description="仅用于技术验证的人工 VMD；真实素材不要勾选",
        default=False,
    )
    status: StringProperty(name="状态", default="IDLE")
    status_message: StringProperty(name="状态说明", default="未提交")
    phase: StringProperty(name="阶段", default="")
    error: StringProperty(name="错误", default="")
    job_id: StringProperty(name="Job ID", default="")
    job_directory: StringProperty(name="Job 目录", default="")
    job_cli_path: StringProperty(name="冻结 Job CLI", default="")
    job_jobs_root: StringProperty(name="冻结 Job Root", default="")
    job_state: StringProperty(name="CLI 原始状态", default="")
    job_terminal: BoolProperty(name="明确终态", default=False)
    job_status_read_error: StringProperty(name="状态读取错误", default="")
    progress_read_error: StringProperty(name="Progress 读取错误", default="")
    cancel_requested: BoolProperty(name="已请求取消", default=False)
    request_path: StringProperty(name="Request", default="")
    job_target_fps: FloatProperty(name="构建目标 fps", default=0.0, min=0.0)
    profile_path: StringProperty(name="Profile", default="")
    library_path: StringProperty(name="Camera library", default="")
    descriptor_path: StringProperty(name="Descriptor", default="")
    profile_sha256: StringProperty(name="Profile SHA", default="")
    library_sha256: StringProperty(name="Library SHA", default="")
    migration_output_path: StringProperty(
        name="新迁移Profile JSON", subtype="FILE_PATH", default="")
    migration_expected_library_sha256: StringProperty(
        name="旧Library SHA256", default="")
    migration_expected_profile_sha256: StringProperty(
        name="旧Profile文件 SHA256", default="")
    profile_collected: BoolProperty(name="成功结果已收集", default=False)
    build_summary: StringProperty(name="构建摘要", default="")
    appended_camera: StringProperty(name="已 Append 相机", default="")
    previous_active_camera: StringProperty(name="此前活动镜头", default="")
    restore_owner_camera: StringProperty(name="Restore 归属相机", default="")
    restore_owner_scene: StringProperty(name="Restore 归属 Scene", default="")
    backend_status: StringProperty(name="后端检查", default="")
    vmd_status: StringProperty(name="VMD 检查", default="")


class C94CAMERA_OT_pick_vmd(bpy.types.Operator, ImportHelper):
    bl_idname = "c94_camera.pick_vmd"
    bl_label = "选择 VMD 相机文件"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".vmd"
    filter_glob: StringProperty(default="*.vmd", options={"HIDDEN"})

    def execute(self, context):
        try:
            opts = _opts(context)
            opts.vmd_path = self.filepath
            opts.status_message = f"已选择 VMD（未导入）：{Path(self.filepath).name}"
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_inspect(bpy.types.Operator):
    bl_idname = "c94_camera.inspect_paths"
    bl_label = "检查路径/后端/VMD"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            message = _inspect_paths(opts)
            opts.status_message = message[:240]
            opts.error = ""
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"检查失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_build(bpy.types.Operator):
    bl_idname = "c94_camera.build_native_library"
    bl_label = "后台构建原生相机库"
    bl_description = (
        "按当前 Scene fps 通过公共 CLI extension.run 构建原生相机库；"
        "不修改 Scene，不注册 MMD Tools"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            _start_build(opts, context.scene)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.status = "FAILED"
                opts.status_message = f"提交失败：{str(exc)[:180]}"
                opts.error = str(exc)[:1000]
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_refresh(bpy.types.Operator):
    bl_idname = "c94_camera.refresh_job"
    bl_label = "刷新后台状态"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            poll_task(opts, include_error=True, force=True)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.status_message = f"刷新失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_collect(bpy.types.Operator):
    bl_idname = "c94_camera.collect_result"
    bl_label = "收集成功结果/查看错误"
    bl_description = (
        "显式一次性收集：读取冻结 job 的 receipt、hash 和精确 camera profile；"
        "周期 timer 不做这些"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            result = _collect_result(opts)
            if not result.get("ok"):
                opts.status_message = (
                    f"任务终态 {result.get('state')}；未收集 profile"
                )
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"收集失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_cancel(bpy.types.Operator):
    bl_idname = "c94_camera.cancel_job"
    bl_label = "取消后台构建"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            _cancel_job(opts)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"取消失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_release(bpy.types.Operator):
    bl_idname = "c94_camera.release_job"
    bl_label = "释放界面任务"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            _release_job(opts)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"释放失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_forget_lost(bpy.types.Operator):
    bl_idname = "c94_camera.forget_lost_job"
    bl_label = "遗忘丢失的任务句柄"
    bl_description = (
        "仅在冻结 job 状态不可读时，确认后清除本界面的 job 绑定；"
        "不取消外部进程、不删除目录或输出"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        opts = getattr(context.scene, OPTIONS, None)
        return bool(opts and opts.job_id.strip() and (
            opts.job_status_read_error.strip() or opts.job_terminal))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            opts = _opts(context)
            _forget_job(opts)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"遗忘失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_migrate_legacy(bpy.types.Operator):
    bl_idname = "c94_camera.migrate_legacy_profile"
    bl_label = "高级：验证旧相机库并生成新配置副本"
    bl_description = (
        "显式只读验证旧library/旧profile，生成不存在的新JSON sidecar；"
        "旧文件绝不写回，不自动Append或改活动镜头"
    )
    bl_options = {"REGISTER"}
    confirm: BoolProperty(name="确认只读验证并写入新侧车（旧文件保留）", default=False)

    def invoke(self, context, event):
        self.confirm = False
        return context.window_manager.invoke_props_dialog(self, width=640)

    def draw(self, context):
        layout = self.layout
        opts = _opts(context)
        layout.label(text="旧 profile：" + (opts.profile_path or "<未设置>"))
        layout.label(text="旧 library：" + (opts.library_path or "<未设置>"))
        layout.label(text="旧 profile SHA256：" + (opts.migration_expected_profile_sha256 or "<未设置>"))
        layout.label(text="旧 library SHA256：" + (opts.migration_expected_library_sha256 or "<未设置>"))
        layout.label(text="新输出 JSON：" + (opts.migration_output_path or "<未设置>"))
        layout.label(text="旧 library/profile 只读保留；不会写回旧文件", icon="INFO")
        layout.label(text="只生成新sidecar，不自动Append、不激活镜头", icon="INFO")
        layout.prop(self, "confirm")

    def execute(self, context):
        opts = _opts(context)
        try:
            _require(self.confirm, "请先在对话框中明确确认迁移")
            _require(opts.profile_path.strip(), "缺少旧profile路径")
            _require(opts.library_path.strip(), "缺少旧library路径")
            _require(opts.migration_expected_profile_sha256.strip(),
                     "缺少旧profile文件SHA256")
            _require(opts.migration_expected_library_sha256.strip(),
                     "缺少旧library SHA256")
            _require(opts.migration_output_path.strip(), "缺少新迁移JSON输出路径")
            core, _ = _core_module()
            result = core.migrate_legacy_camera_profile(
                opts.library_path,
                opts.profile_path,
                opts.migration_output_path,
                expected_library_sha256=opts.migration_expected_library_sha256,
                expected_profile_sha256=opts.migration_expected_profile_sha256,
            )
            new_profile = core.load_camera_profile(result["profile_path"])
            _require(new_profile.get("profile_sha256") == result["profile_sha256"],
                     "迁移新profile回读hash不一致")
            for role in ("object_action", "data_action"):
                record = new_profile["library"][role]
                _require(record.get("evaluation_signature_version") ==
                         core.CAMERA_ACTION_EVAL_SIGNATURE_VERSION
                         and isinstance(record.get("evaluation_signature"), str),
                         f"迁移新profile {role}缺少v2求值签名")
            opts.profile_path = result["profile_path"]
            opts.profile_sha256 = result["profile_sha256"]
            opts.library_path = result["library_path"]
            opts.library_sha256 = result["library_sha256"]
            opts.descriptor_path = ""
            opts.profile_collected = True
            opts.job_target_fps = float(new_profile["mapping"]["target_fps"])
            opts.status = "UNKNOWN"
            opts.status_message = (
                "旧库只读验证完成；新sidecar已写入并回读；未Append/未改活动镜头"
            )
            opts.build_summary = (
                "migration source_profile=" + opts.migration_expected_profile_sha256[:12]
                + " source_library=" + opts.migration_expected_library_sha256[:12]
                + " new_profile=" + opts.profile_sha256[:12]
            )
            opts.error = ""
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts.error = str(exc)[:1000]
            opts.status_message = f"迁移失败：{str(exc)[:180]}"
            _redraw()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_append(bpy.types.Operator):
    bl_idname = "c94_camera.append_profile"
    bl_label = "Append 精确 profile 到当前 Scene"
    bl_description = "使用后台成功 job 的精确 profile；不修改 Scene fps，不自动替换活动镜头"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            _append_profile(opts, context.scene)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"Append 失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_set_active(bpy.types.Operator):
    bl_idname = "c94_camera.set_active_camera"
    bl_label = "设为活动镜头"
    bl_description = "显式把已 Append 的新相机设为 Scene.camera；不会随构建/Append 自动执行"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            _set_active_camera(opts, context.scene)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"设置活动镜头失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_OT_restore_active(bpy.types.Operator):
    bl_idname = "c94_camera.restore_active_camera"
    bl_label = "恢复此前活动镜头"
    bl_description = (
        "仅当当前活动镜头仍是已 Append 相机且 restore 归属记录有效时，"
        "显式恢复此前镜头；否则拒绝并保留当前用户选择"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            opts = _opts(context)
            _restore_active_camera(opts, context.scene)
            _redraw()
            return {"FINISHED"}
        except Exception as exc:  # noqa: BLE001
            opts = getattr(context.scene, OPTIONS, None)
            if opts is not None:
                opts.error = str(exc)[:1000]
                opts.status_message = f"恢复活动镜头失败：{str(exc)[:180]}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94CAMERA_PT_panel(bpy.types.Panel):
    bl_idname = "C94CAMERA_PT_panel"
    bl_label = "MMD 可选镜头 · VMD camera"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "动画重定向"
    bl_parent_id = "C94RETARGET_PT_body"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        opts = getattr(scene, OPTIONS, None)
        if opts is None:
            layout.label(text="相机 UI 未注册", icon="ERROR")
            return
        try:
            fps = _scene_fps(scene)
        except Exception:
            fps = 0.0
        layout.label(text=f"当前 Scene fps：{fps:g}（构建时使用，不修改）")

        box = layout.box()
        box.label(text="输入（显式路径）", icon="FILE")
        box.prop(opts, "vmd_path")
        row = box.row(align=True)
        row.operator("c94_camera.pick_vmd", text="选择 VMD…")
        row.operator("c94_camera.inspect_paths", text="检查")
        box.prop(opts, "backend_path")
        box.prop(opts, "opencc_path")
        box.prop(opts, "cli_path")
        box.prop(opts, "jobs_root")
        box.label(text="后台作业目录必须已存在；requests/ 与 job 子目录只写此目录", icon="INFO")
        box.label(text="上面 CLI/jobs 路径只用于下一次提交；运行中 job 使用冻结路径", icon="INFO")
        box.prop(opts, "max_seconds")
        box.prop(opts, "artificial_note")
        if opts.backend_status:
            box.label(text=opts.backend_status[:88], icon="INFO")
        if opts.vmd_status:
            box.label(text=opts.vmd_status[:88], icon="INFO")
        box.prop(opts, "worker_path")

        box = layout.box()
        box.label(text="后台构建（public CLI extension.run）", icon="PLAY")
        build = box.row()
        build.enabled = not bool(opts.job_id)
        build.operator("c94_camera.build_native_library", text="按当前 Scene fps 构建")
        status = opts.status or "IDLE"
        box.label(
            text=f"{_STATUS_LABEL.get(status, status)} · {opts.status_message[:72]}",
            icon=_STATUS_ICON.get(status, "INFO"),
        )
        if opts.phase:
            box.label(text=f"阶段：{opts.phase}", icon="TIME")
        if opts.job_id:
            box.label(text=f"Job：{opts.job_id}")
            box.label(text=f"冻结 CLI：{opts.job_cli_path[-70:]}", icon="INFO")
            box.label(text=f"冻结 root：{opts.job_jobs_root[-70:]}", icon="INFO")
            box.label(text=f"冻结 job：{opts.job_directory[-70:]}", icon="INFO")
        if opts.job_status_read_error:
            box.label(text=opts.job_status_read_error[:88], icon="ERROR")
        if opts.job_id and (opts.job_status_read_error or opts.job_terminal):
            forget = box.row()
            forget.alert = True
            forget.operator(
                "c94_camera.forget_lost_job",
                text="遗忘丢失句柄（仅清界面，不取消/不删目录）",
            )
            box.label(
                text="确认前请核对：遗忘只清本界面绑定，后台进程和磁盘输出保持原样",
                icon="ERROR",
            )
        if opts.progress_read_error:
            box.label(
                text=f"Progress 不可读（阶段未知）：{opts.progress_read_error[:72]}",
                icon="INFO",
            )
        if opts.error:
            box.label(text=opts.error[:96], icon="ERROR")
        row = box.row(align=True)
        row.operator("c94_camera.refresh_job", text="刷新状态")
        collect = row.row()
        collect.enabled = opts.job_terminal and (
            opts.job_state != "succeeded" or not opts.profile_collected
        )
        collect.operator(
            "c94_camera.collect_result",
            text=(
                "收集成功结果"
                if opts.job_state == "succeeded"
                else "查看终态错误"
            ),
        )
        cancel = row.row()
        cancel.enabled = bool(opts.job_id) and not opts.job_terminal
        cancel.operator("c94_camera.cancel_job", text="取消")
        release = row.row()
        release.enabled = opts.job_terminal
        release.operator("c94_camera.release_job", text="释放界面任务")

        box = layout.box()
        box.label(text="精确 profile → 当前 Scene", icon="ACTION")
        if opts.profile_collected and opts.profile_path:
            box.label(text=f"Profile：{Path(opts.profile_path).name}")
            box.label(text=f"SHA：{opts.profile_sha256[:24]}…")
            append = box.row()
            append.enabled = (
                not (opts.job_id and not opts.job_terminal)
                and bool(opts.library_path)
            )
            append.operator("c94_camera.append_profile", text="Append 原生相机与 Object/Data Action")
        elif opts.job_state == "succeeded" and opts.job_terminal:
            box.label(text="成功终态；请先点“收集成功结果”", icon="INFO")
        else:
            box.label(text="构建成功后在此提供精确 profile Append", icon="INFO")
        if opts.build_summary:
            box.label(text=opts.build_summary[:96], icon="INFO")

        box = layout.box()
        box.label(text="高级：旧弱profile只读验证 → 新JSON sidecar", icon="FILE")
        box.label(text="旧library/旧profile保留且绝不写回；不自动Append/激活镜头", icon="INFO")
        box.prop(opts, "profile_path", text="旧 Profile JSON")
        box.prop(opts, "library_path", text="旧 Camera library")
        box.prop(opts, "migration_output_path")
        box.prop(opts, "migration_expected_library_sha256")
        box.prop(opts, "migration_expected_profile_sha256")
        migrate = box.row()
        migrate.enabled = bool(
            opts.profile_path.strip() and opts.library_path.strip()
            and opts.migration_output_path.strip()
            and opts.migration_expected_library_sha256.strip()
            and opts.migration_expected_profile_sha256.strip()
        )
        migrate.operator(
            "c94_camera.migrate_legacy_profile",
            text="验证旧库并生成新配置副本",
        )

        box = layout.box()
        box.label(text="活动镜头（单独显式操作）", icon="CAMERA_DATA")
        current = scene.camera.name if scene.camera else "None"
        box.label(text=f"当前 Scene.camera：{current}")
        if opts.appended_camera:
            box.label(text=f"已 Append：{opts.appended_camera}")
            restore_available = _restore_available(opts, scene)
            row = box.row(align=True)
            row.operator("c94_camera.set_active_camera", text="设为活动镜头")
            restore = row.row()
            restore.enabled = restore_available
            restore.operator("c94_camera.restore_active_camera", text="恢复此前镜头")
            if opts.previous_active_camera and not restore_available:
                if current != opts.appended_camera:
                    box.label(
                        text=(
                            f"restore 已禁用：当前 {current} 不是 Append 相机；"
                            "保留用户选择"
                        ),
                        icon="INFO",
                    )
                else:
                    box.label(
                        text="restore 已禁用：归属记录无效或此前镜头不可用",
                        icon="INFO",
                    )
            box.label(
                text="Append/构建不自动改；set-active/restore 才是显式改镜头",
                icon="INFO",
            )
        else:
            box.label(text="尚未 Append 新相机", icon="INFO")

        layout.label(text="成功结果先收集，再追加到当前场景", icon="INFO")
        layout.label(
            text="构建/Append 保持旧 Scene.camera；set-active/restore 为显式改镜头",
            icon="INFO",
        )


_CLASSES = (
    C94CAMERA_Options,
    C94CAMERA_OT_pick_vmd,
    C94CAMERA_OT_inspect,
    C94CAMERA_OT_build,
    C94CAMERA_OT_refresh,
    C94CAMERA_OT_collect,
    C94CAMERA_OT_cancel,
    C94CAMERA_OT_release,
    C94CAMERA_OT_forget_lost,
    C94CAMERA_OT_migrate_legacy,
    C94CAMERA_OT_append,
    C94CAMERA_OT_set_active,
    C94CAMERA_OT_restore_active,
    C94CAMERA_PT_panel,
)


def register():
    global _OWNED, _TIMER_REGISTERED
    if _OWNED:
        raise RuntimeError("MMD 可选相机 UI 已注册，原实例保持")
    if not hasattr(bpy.types, "C94RETARGET_PT_body"):
        raise RuntimeError("请先注册 C94RETARGET_PT_body，再注册 MMD 可选相机 UI")
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError("MMD 可选相机 UI 选项已由其他实例注册，原实例保持")
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, bpy.props.PointerProperty(type=C94CAMERA_Options))
        _OWNED = True
        bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)
        _TIMER_REGISTERED = True
    except Exception:
        unregister()
        raise


def unregister():
    global _OWNED, _TIMER_REGISTERED, _CORE, _CORE_PATH
    if _TIMER_REGISTERED:
        try:
            bpy.app.timers.unregister(_tick)
        except Exception:
            pass
        _TIMER_REGISTERED = False
    if _OWNED and hasattr(bpy.types.Scene, OPTIONS):
        delattr(bpy.types.Scene, OPTIONS)
    _OWNED = False
    for cls in reversed(_REGISTERED):
        if getattr(cls, "is_registered", False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
    _CORE = None
    _CORE_PATH = None


__all__ = (
    "OPTIONS",
    "UI_VERSION",
    "C94CAMERA_Options",
    "C94CAMERA_PT_panel",
    "register",
    "unregister",
)
