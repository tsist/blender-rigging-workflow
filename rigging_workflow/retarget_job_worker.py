# SPDX-License-Identifier: GPL-3.0-or-later
"""Worker process for one transparent C94 retarget batch snapshot.

The worker is intentionally a thin driver.  It loads the job-local runtime
package, opens only the frozen Scene snapshot, binds each selected source
Action/Slot, and calls the shared retarget conversion core.  It owns no
production file and never discovers or kills other processes.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

import bpy

sys.dont_write_bytecode = True


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.05)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _verify_local_package(root, request):
    runtime = root / "runtime" / "rigging_workflow"
    if not runtime.is_dir():
        raise RuntimeError("运行包目录缺失")
    manifest_path = runtime / request["package"]["manifest"]
    if not manifest_path.is_file():
        raise RuntimeError("运行包清单缺失")
    if _sha256_file(manifest_path) != request["package"]["manifest_sha256"]:
        raise RuntimeError("运行包清单哈希不符")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise RuntimeError("运行包清单内容无效")
    checked = 0
    for relative, expected in manifest["files"].items():
        relative = relative.replace("\\", "/")
        path = runtime.joinpath(*relative.split("/"))
        if not path.is_file() or _sha256_file(path) != expected:
            raise RuntimeError("运行包文件哈希不符：" + relative)
        checked += 1
    extra_checked = 0
    for relative, expected in request["package"]["extra_files"].items():
        path = runtime.joinpath(*relative.split("/"))
        if not path.is_file() or _sha256_file(path) != expected:
            raise RuntimeError("运行包附加文件哈希不符：" + relative)
        extra_checked += 1
    return {
        "runtime_root": str(runtime),
        "manifest_sha256": request["package"]["manifest_sha256"],
        "manifest_files_verified": checked,
        "extra_files_verified": extra_checked,
    }


def _progress(root, **record):
    record["at_unix"] = time.time()
    try:
        with (root / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    except OSError:
        pass


def _ledger(root):
    return _read_json(root / "ledger.json")


def _write_ledger(root, ledger):
    ledger["updated_at_unix"] = time.time()
    _atomic_json(root / "ledger.json", ledger)


def _item_by_index(ledger, index):
    for item in ledger["items"]:
        if item["index"] == index:
            return item
    raise RuntimeError("ledger缺少item")


def _counts(items):
    counts = {
        "not_started": 0, "running": 0, "succeeded": 0,
        "failed": 0, "cancelled": 0, "total": len(items),
    }
    for item in items:
        state = item.get("state", "not_started")
        counts[state] = counts.get(state, 0) + 1
    return counts


def _set_item(root, ledger, item, **fields):
    target = _item_by_index(ledger, item["index"])
    target.update(fields)
    _write_ledger(root, ledger)


def _result(root, *, status, ledger, runtime, started_at, cancel_reason, error, deadline):
    items = ledger["items"] if isinstance(ledger, dict) else []
    payload = {
        "schema": "c94.retarget.job.result.v1",
        "job_id": ledger.get("job_id") if isinstance(ledger, dict) else None,
        "status": status,
        "terminal": True,
        "cancel_reason": cancel_reason,
        "error": error,
        "counts": _counts(items),
        "items": items,
        "runtime": runtime,
        "started_at_unix": started_at,
        "finished_at_unix": time.time(),
        "deadline_unix": deadline,
        "process": {"pid": os.getpid()},
    }
    _atomic_json(root / "result.json", payload)
    return payload


def _failure_result(root, ledger, runtime, started_at, deadline, code, message, traceback_text=None):
    if traceback_text:
        try:
            (root / "traceback.txt").write_text(traceback_text, encoding="utf-8")
        except OSError:
            pass
    return _result(
        root, status="failed", ledger=ledger, runtime=runtime,
        started_at=started_at, cancel_reason=None,
        error={"code": code, "message": message}, deadline=deadline,
    )


def _module_info(runtime, extra=()):
    names = (
        "rigging_workflow", "rigging_workflow.retarget_action",
        "rigging_workflow.retarget_source", "rigging_workflow.retarget_bvh",
        "rigging_workflow.retarget_calibration", "rigging_workflow.retarget_job",
        "rigging_workflow.pose_switching",
    )
    rows = {}
    root = Path(runtime).resolve()
    for name in tuple(names) + tuple(extra):
        module = sys.modules.get(name)
        if module is None or not getattr(module, "__file__", None):
            raise RuntimeError("运行包模块未加载：" + name)
        path = Path(module.__file__).resolve()
        if root not in path.parents:
            raise RuntimeError("模块未从job运行包加载：" + str(path))
        rows[name] = str(path)
    return rows


def _find_target(scene, meta):
    uid = meta["target"]["uid"]
    matches = [
        obj for obj in scene.objects
        if obj.type == "ARMATURE" and obj.get("c94_retarget_target_uid_v1") == uid
    ]
    if len(matches) != 1:
        raise RuntimeError("快照目标UID不唯一或缺失")
    target = matches[0]
    if target.name not in bpy.context.view_layer.objects:
        raise RuntimeError("快照目标不在当前视图层")
    if target.library or target.override_library or target.data.library:
        raise RuntimeError("快照目标不是本地可编辑骨架")
    return target


def _find_source(scene, meta):
    name = meta["source"]["name"]
    source = scene.objects.get(name)
    if source is None or source.type != "ARMATURE":
        raise RuntimeError("快照源骨架缺失")
    if source.name not in bpy.context.view_layer.objects:
        raise RuntimeError("快照源骨架不在当前视图层")
    if source.library or source.override_library or source.data.library:
        raise RuntimeError("快照源骨架不是本地可编辑骨架")
    return source


def _binding_key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=repr)


def run(root):
    root = Path(root)
    started_at = time.time()
    request = _read_json(root / "request.json")
    if request.get("schema") != "c94.retarget.job.request.v1":
        raise RuntimeError("request schema无效")
    ledger = _ledger(root)
    if not isinstance(ledger, dict) or ledger.get("schema") != "c94.retarget.job.ledger.v1":
        raise RuntimeError("ledger缺失或schema无效")
    if ledger.get("job_id") != request["job_id"] or \
            len(ledger.get("items", [])) != len(request["items"]):
        raise RuntimeError("ledger与request不一致")
    for item, requested in zip(ledger["items"], request["items"]):
        if item.get("id") != requested["id"] or item.get("index") != requested["index"]:
            raise RuntimeError("ledger item顺序不一致")
    worker_hash = _sha256_file(Path(__file__))
    if worker_hash != request["worker"]["sha256"]:
        raise RuntimeError("worker自身哈希与request不符")
    snapshot_path = root / request["snapshot"]["file"]
    if not snapshot_path.is_file() or _sha256_file(snapshot_path) != request["snapshot"]["sha256"]:
        raise RuntimeError("快照文件缺失或哈希不符")
    meta_path = root / request["snapshot"]["meta"]
    if not meta_path.is_file() or _sha256_file(meta_path) != request["snapshot"]["meta_sha256"]:
        raise RuntimeError("快照元数据缺失或哈希不符")
    meta = _read_json(meta_path)
    if meta.get("job_id") != request["job_id"] or \
            meta.get("snapshot_revision") != request["snapshot"]["revision"]:
        raise RuntimeError("快照身份与request不一致")
    runtime = _verify_local_package(root, request)
    runtime["worker_sha256"] = worker_hash
    runtime["worker_pid"] = os.getpid()
    runtime["user_resources"] = os.environ.get("BLENDER_USER_RESOURCES")
    runtime["cwd"] = os.getcwd()
    sys.path.insert(0, str(root / "runtime"))
    existing = sys.modules.get("rigging_workflow")
    if existing is not None:
        raise RuntimeError("rigging_workflow已在运行包之外加载")
    from rigging_workflow import retarget_action as rt
    from rigging_workflow import retarget_bvh as bvh
    from rigging_workflow import retarget_calibration as calibration
    from rigging_workflow import retarget_general_job as general
    from rigging_workflow import retarget_job as api
    from rigging_workflow import retarget_source as sources
    extra_modules = []
    if request.get("target_mode") == general.TARGET_MODE_GENERIC:
        from rigging_workflow import retarget_generic_target  # noqa: F401
        extra_modules.append("rigging_workflow.retarget_generic_target")
    if request.get("preset") == general.PRESET_PROFILE:
        from rigging_workflow import retarget_profile  # noqa: F401
        extra_modules.append("rigging_workflow.retarget_profile")
    runtime["module_files"] = _module_info(
        runtime["runtime_root"], extra_modules)
    runtime["blender"] = {
        "version": list(bpy.app.version),
        "version_string": bpy.app.version_string,
        "binary": bpy.app.binary_path,
    }
    _atomic_json(root / "worker-info.json", runtime)
    deadline_unix = started_at + float(request["settings"]["max_seconds"])
    cancel_state = {"reason": None}

    def cancelled():
        if (root / "cancel.request").is_file():
            cancel_state["reason"] = "user"
            return True
        if time.time() > deadline_unix:
            cancel_state["reason"] = "deadline"
            return True
        return False

    def progress(phase, i, n):
        _progress(root, item_index=progress.current_index,
                  item_id=progress.current_id, phase=phase, done=i, total=n)

    progress.current_index = None
    progress.current_id = None

    try:
        if cancelled():
            reason = cancel_state["reason"]
            return _result(
                root, status="cancelled", ledger=ledger, runtime=runtime,
                started_at=started_at, cancel_reason=reason,
                error={"code": "CANCELLED", "message": "启动前已取消"},
                deadline=deadline_unix,
            )
        bpy.ops.wm.open_mainfile(filepath=str(snapshot_path),
                                 load_ui=False, use_scripts=False)
        scene = bpy.context.scene
        if int(scene.render.fps) != int(meta["scene"]["fps"]) or \
                abs(float(scene.render.fps_base) - float(meta["scene"]["fps_base"])) > 1e-9:
            raise RuntimeError("快照场景帧率与记录不符")
        target = _find_target(scene, meta)
        source = _find_source(scene, meta)
        current_target_fp = api._rig_static_fingerprint(target)
        current_source_fp = api._rig_static_fingerprint(source)
        if current_target_fp["static_sha256"] != meta["target"]["fingerprint"]["static_sha256"]:
            raise RuntimeError("快照目标静态指纹与捕获记录不符")
        frame_matches = (
            int(scene.frame_current) == int(meta["scene"]["frame"]) and
            abs(float(scene.frame_subframe) - float(meta["scene"].get("subframe", 0.0))) < 1e-9
        )
        if (not current_target_fp["matrix_world_dynamic"] or frame_matches) and \
                current_target_fp["matrix_world"] != meta["target"]["fingerprint"]["matrix_world"]:
            raise RuntimeError("快照目标世界变换与捕获记录不符")
        if current_source_fp["static_sha256"] != meta["source"]["fingerprint"]["static_sha256"]:
            raise RuntimeError("快照源静态指纹与捕获记录不符")
        source_binding = rt.binding(source)
        target_binding = rt.binding(target)
        source_action = source.animation_data.action if source.animation_data else None
        target_action = target.animation_data.action if target.animation_data else None
        if calibration.action_signature(source_action) != \
                meta["state_guard"]["source_action_sha256"]:
            raise RuntimeError("快照源Action与捕获记录不符")
        if calibration.action_signature(target_action) != \
                meta["state_guard"]["target_action_sha256"]:
            raise RuntimeError("快照旧目标Action与捕获记录不符")
        target_compact = api._compact_fingerprint(current_target_fp)
        target_mode = general.validate_worker_payload(target, request)
        if target_mode == general.TARGET_MODE_PROFILE:
            general.validate_profile_job(
                source, target, request["source_profile"])
        protected = {
            action.as_pointer()
            for action in (source_action, target_action)
            if action is not None
        }
        selected_actions = [
            action for action in bpy.data.actions
            if any(action.name == item["action_name"] for item in request["items"])
        ]
        protected.update(action.as_pointer() for action in selected_actions)
        for item in request["items"]:
            if item.get("validation_error"):
                ledger_item = _item_by_index(ledger, item["index"])
                _set_item(
                    root, ledger, ledger_item, state="failed",
                    finished_at_unix=time.time(), error=item["validation_error"],
                )
                _progress(root, item_index=item["index"], item_id=item["id"],
                          phase="ITEM_INVALID", error=item["validation_error"])
        for item in request["items"]:
            ledger_item = _item_by_index(ledger, item["index"])
            if ledger_item.get("state") != "not_started":
                continue
            if item.get("validation_error"):
                continue
            if cancelled():
                break
            progress.current_index = item["index"]
            progress.current_id = item["id"]
            item_started = time.time()
            _set_item(root, ledger, ledger_item, state="running",
                      started_at_unix=item_started)
            _progress(root, item_index=item["index"], item_id=item["id"],
                      phase="ITEM_START")
            created = None
            owned_mmd_actions = []
            mmd_transport = None
            temporary = None
            final_path = None
            published = False
            committed = False
            try:
                action = api._find_action_by_name(item["action_name"])
                if action.as_pointer() not in protected:
                    protected.add(action.as_pointer())
                if calibration.action_signature(action) != item["action_signature"]:
                    raise RuntimeError("源Action内容与快照记录不符")
                if item.get("kind") == "CONTACT_DETECTION":
                    from rigging_workflow import retarget_contact_detection as contact_detection
                    report = contact_detection.detect_item(source, target, item, progress=progress, cancelled=cancelled)
                    report["background_job"] = {"job_id":request["job_id"], "item_id":item["id"],
                        "snapshot_revision":request["snapshot"]["revision"], "artifact_type":"CONTACT_REPORT",
                        "target_uid":meta["target"]["uid"], "target_fingerprint":target_compact}
                    if cancelled(): raise InterruptedError("已取消，未发布检测报告")
                    outputs = root / "outputs"; outputs.mkdir(exist_ok=True)
                    final_path = outputs / (item["id"] + ".json")
                    temporary = outputs / ("." + item["id"] + "." + uuid.uuid4().hex + ".part.json")
                    if final_path.exists(): raise RuntimeError("报告已存在，拒绝覆盖")
                    _atomic_json(temporary, report)
                    contact_detection.validate_report(_read_json(temporary), item=item, job_id=request["job_id"])
                    output_sha = _sha256_file(temporary)
                    if cancelled(): raise InterruptedError("已取消，未发布检测报告")
                    os.replace(temporary, final_path); published = True
                    if _sha256_file(final_path) != output_sha: raise RuntimeError("检测报告发布后hash不同")
                    receipt = {"state":"succeeded", "output":f"outputs/{final_path.name}",
                        "output_sha256":output_sha, "artifact_type":"CONTACT_REPORT", "action_name":None,
                        "action_signature":None, "report_status":report["status"], "issue_count":report.get("issue_count",0),
                        "target_fingerprint_sha256":target_compact["static_sha256"],
                        "snapshot_revision":request["snapshot"]["revision"],
                        "finished_at_unix":time.time(), "seconds":time.time()-item_started}
                    _set_item(root, ledger, ledger_item, **receipt); committed = True
                    _progress(root, item_index=item["index"], item_id=item["id"], phase="ITEM_DONE",
                        output=receipt["output"], output_sha256=output_sha, report_status=report["status"])
                    continue
                slot = api._find_slot(action, item["slot_identifier"])
                if request["preset"] not in ("MMD", "CONTACT_CORRECTION"):
                    animation = source.animation_data_create()
                    animation.action = action
                    animation.action_slot = slot
                    if animation.action != action or animation.action_slot != slot:
                        raise RuntimeError("无法绑定所选源Action/Slot")
                kwargs = {
                    "source_fps": item["fps"],
                    "start": item["start"],
                    "end": item["end"],
                    "output_start": item["output_start"],
                    "action_name": item["output_action_name"],
                    "progress": progress,
                    "cancelled": cancelled,
                }
                if request["preset"] == general.PRESET_PROFILE:
                    converted = general.convert_profile(
                        source, target, request["source_profile"],
                        action=action, slot=slot, item=item,
                        settings=request["settings"], progress=progress,
                        cancelled=cancelled,
                    )
                elif request.get("target_mode") == general.TARGET_MODE_GENERIC:
                    converted = general.convert_generic(
                        source, target, request["mapping"],
                        request["target_policy"], item=item,
                        settings=request["settings"], progress=progress,
                        cancelled=cancelled,
                    )
                elif request["preset"] == "MIXAMO":
                    kwargs.update({
                        "mapping": request["mapping_overrides"] or None,
                        "motion": request["settings"]["motion"],
                        "scale_motion": request["settings"]["scale_motion"],
                        "include_fingers": request["settings"]["include_fingers"],
                        "sample_step": 1,
                    })
                    converted = rt.convert(source, target, **kwargs)
                elif request["preset"] == "CONTACT_CORRECTION":
                    from rigging_workflow import retarget_contact_job as contact_job
                    converted = contact_job.convert_item(target, item, progress=progress, cancelled=cancelled)
                elif request["preset"] == "MMD":
                    from rigging_workflow import retarget_mmd_job as mmd_job
                    converted = mmd_job.convert_item(source, target, request["source_profile"], item,
                        request["settings"], progress=progress, cancelled=cancelled, protected=protected)
                    owned_mmd_actions = converted["_owned_mmd_actions"]
                else:
                    kwargs["motion"] = request["settings"]["motion"]
                    kwargs["sample_step"] = 1
                    converted = bvh.convert(source, target, **kwargs)
                created = converted["action"]
                if created.as_pointer() in protected:
                    raise RuntimeError("转换返回了受保护Action")
                if cancelled():
                    raise InterruptedError("已取消，未提交输出")
                if request["preset"] != "CONTACT_CORRECTION":
                    from rigging_workflow import retarget_pipeline
                    retarget_pipeline.complete_detection(source,target,converted,
                        calibration=request["settings"].get("contact_detection_calibration",{}),
                        progress=progress,cancelled=cancelled)
                general.validate_candidate_policy(
                    target, created, None, request)
                if request["preset"] == "MMD":
                    mmd_transport = mmd_job.detach_worker_hub(created, target)
                report = json.loads(created.get(rt.META, "{}"))
                if request["preset"] == "MMD":
                    report["mmd_registered_clip"] = converted["_mmd_clip"]
                marker = {
                    "schema": "c94.retarget.job.action.v1",
                    "job_id": request["job_id"],
                    "item_id": item["id"],
                    "item_index": item["index"],
                    "snapshot_revision": request["snapshot"]["revision"],
                    "preset": request["preset"],
                    "target_mode": request.get("target_mode", "C94"),
                    "target_policy_sha256": request.get("target_policy_sha256"),
                    "source_profile_sha256": request.get("source_profile_sha256"),
                    "kind": item.get("kind", "RETARGET"),
                    "source_action": item["action_name"],
                    "source_slot": item["slot_identifier"],
                    "target_uid": meta["target"]["uid"],
                    "target_fingerprint": target_compact,
                    "output": f"outputs/{item['id']}.blend",
                    "status": "needs_review",
                }
                if request["preset"] == "MMD":
                    marker["mmd_clip_id"] = item["mmd"]["clip_id"]
                    marker["mmd_profile_sha256"] = request["source_profile"]["profile_sha256"]
                elif request["preset"] == general.PRESET_PROFILE:
                    marker["source_profile_document_sha256"] = request["source_profile"]["profile_sha256"]
                report["background_job"] = {
                    **marker,
                    "guarded_target_fingerprint": target_compact,
                    "preview_check_hook": (
                        "Before preview, compare the current target UID and "
                        "c94_retarget.job.rig_static.v1 static SHA with this "
                        "marker; this slice does not alter begin_preview."
                    ),
                }
                if request["preset"] != "CONTACT_CORRECTION":
                    created[rt.META] = json.dumps(report, ensure_ascii=False)
                created[api.JOB_KEY] = json.dumps(marker, ensure_ascii=False)
                created.use_fake_user = True
                action_signature = calibration.action_signature(created)
                stats = calibration.action_stats(created, converted["slot"])
                outputs = root / "outputs"
                outputs.mkdir(exist_ok=True)
                final_path = outputs / (item["id"] + ".blend")
                temporary = outputs / (
                    "." + item["id"] + "." + uuid.uuid4().hex + ".part.blend"
                )
                if final_path.exists():
                    raise RuntimeError("输出文件已存在，拒绝覆盖")
                bpy.data.libraries.write(
                    str(temporary), {created}, path_remap="ABSOLUTE",
                    fake_user=True, compress=False,
                )
                if not temporary.is_file() or temporary.stat().st_size <= 0:
                    raise RuntimeError("输出Action文件未写出")
                with bpy.data.libraries.load(str(temporary), link=False) as (source_data, _):
                    names = list(source_data.actions)
                    if request["preset"] == "CONTACT_CORRECTION":
                        contact_job.action_only_inventory(source_data)
                    elif request["preset"] == "MMD":
                        mmd_job.action_only_inventory(source_data)
                if len(names) != 1:
                    raise RuntimeError("输出文件不是单个独立Action")
                output_sha = _sha256_file(temporary)
                if cancelled():
                    raise InterruptedError("已取消，未提交输出")
                os.replace(temporary, final_path)
                published = True
                if _sha256_file(final_path) != output_sha:
                    raise RuntimeError("输出文件发布后哈希不符")
                receipt = {
                    "state": "succeeded",
                    "output": f"outputs/{final_path.name}",
                    "output_sha256": output_sha,
                    "action_name": created.name,
                    "action_signature": action_signature,
                    "target_fingerprint_sha256": target_compact["static_sha256"],
                    "snapshot_revision": request["snapshot"]["revision"],
                    "stats": stats,
                    "report": report,
                    "finished_at_unix": time.time(),
                    "seconds": time.time() - item_started,
                }
                if request["preset"] == "MMD":
                    receipt.update(mmd=item["mmd"], mmd_transport=mmd_transport)
                _set_item(root, ledger, ledger_item, **receipt)
                committed = True
                _progress(root, item_index=item["index"], item_id=item["id"],
                          phase="ITEM_DONE", output=receipt["output"],
                          output_sha256=output_sha)
            except InterruptedError:
                if temporary is not None and temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
                if published and not committed and final_path is not None and final_path.exists():
                    try:
                        final_path.unlink()
                    except OSError:
                        pass
                if cancel_state["reason"] is None:
                    cancel_state["reason"] = "user"
                _set_item(
                    root, ledger, ledger_item, state="cancelled",
                    error={"code": "CANCELLED", "message": "转换已取消"},
                    finished_at_unix=time.time(),
                    seconds=time.time() - item_started,
                )
                _progress(root, item_index=item["index"], item_id=item["id"],
                          phase="ITEM_CANCELLED",
                          reason=cancel_state["reason"])
                break
            except Exception as exc:
                if temporary is not None and temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
                if published and not committed and final_path is not None and final_path.exists():
                    try:
                        final_path.unlink()
                    except OSError:
                        pass
                error = {"code": "ITEM_FAILED", "message": str(exc)}
                trace = traceback.format_exc()
                try:
                    (root / ("traceback-" + item["id"] + ".txt")).write_text(
                        trace, encoding="utf-8",
                    )
                except OSError:
                    pass
                _set_item(
                    root, ledger, ledger_item, state="failed", error=error,
                    finished_at_unix=time.time(),
                    seconds=time.time() - item_started,
                )
                _progress(root, item_index=item["index"], item_id=item["id"],
                          phase="ITEM_FAILED", error=error)
            finally:
                for owned in reversed(owned_mmd_actions):
                    if owned != created and owned.as_pointer() not in protected:
                        try:bpy.data.actions.remove(owned)
                        except (ReferenceError, RuntimeError):pass
                if created is not None and created.as_pointer() not in protected:
                    try:
                        bpy.data.actions.remove(created)
                    except (ReferenceError, RuntimeError):
                        pass
                try:
                    rt.restore_binding(source, source_binding)
                except Exception:
                    pass
                try:
                    rt.restore_binding(target, target_binding)
                except Exception:
                    pass
        # Remaining unstarted items stay not_started by design.
        reason = cancel_state["reason"]
        counts = _counts(ledger["items"])
        if reason == "user":
            status = "cancelled"
            error = {"code": "CANCELLED", "message": "用户请求取消"}
        elif reason == "deadline":
            status = "partial" if counts["succeeded"] else "failed"
            error = {"code": "JOB_DEADLINE", "message": "后台job超过配置的运行预算"}
        elif counts["failed"]:
            status = "partial" if counts["succeeded"] else "failed"
            error = None
        else:
            status = "succeeded"
            error = None
        return _result(
            root, status=status, ledger=ledger, runtime=runtime,
            started_at=started_at, cancel_reason=reason, error=error,
            deadline=deadline_unix,
        )
    except BaseException as exc:
        return _failure_result(
            root, ledger, runtime, started_at, deadline_unix,
            "WORKER_ERROR", str(exc), traceback.format_exc(),
        )


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        directory = sys.argv[sys.argv.index("--") + 1]
    except (ValueError, IndexError):
        raise SystemExit(2)
    try:
        run(directory)
    except BaseException as exc:
        root = Path(directory)
        try:
            (root / "traceback.txt").write_text(traceback.format_exc(),
                                                encoding="utf-8")
        except OSError:
            pass
        ledger = _read_json(root / "ledger.json") if (root / "ledger.json").is_file() else None
        if not isinstance(ledger, dict):
            ledger = {"job_id": None, "items": []}
        _atomic_json(root / "result.json", {
            "schema": "c94.retarget.job.result.v1",
            "job_id": ledger.get("job_id"),
            "status": "failed",
            "terminal": True,
            "cancel_reason": None,
            "error": {"code": "WORKER_START_FAILED", "message": str(exc)},
            "counts": _counts(ledger.get("items", [])),
            "items": ledger.get("items", []),
            "runtime": None,
            "started_at_unix": time.time(),
            "finished_at_unix": time.time(),
            "deadline_unix": None,
            "process": {"pid": os.getpid()},
        })
