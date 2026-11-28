# SPDX-License-Identifier: GPL-3.0-or-later
"""Additive queue integration for explicit ordinary-FK targets and profiles.

This module contains no solver, process supervisor, preview store or progress
engine.  ``retarget_job`` remains the owner of the snapshot, ledger, worker,
cancel and collect lifecycle.  The functions here only normalise the two
explicit job payloads and dispatch to the already reviewed shared cores:

* ``retarget_generic_target.convert`` for Mixamo/CMU -> explicit FK target;
* ``retarget_profile.convert`` for a pure-JSON source profile -> C94.

The combination "generic FK target + profile" is deliberately refused until
``retarget_profile.convert`` supports a non-C94 target adapter.
"""
from __future__ import annotations

import json

from . import retarget_action as rt
from . import retarget_calibration as calibration
from . import retarget_source as sources


PRESET_PROFILE = "PROFILE"
TARGET_MODE_C94 = "C94"
TARGET_MODE_MMD = "C94_MMD"
TARGET_MODE_CONTACT = "CONTACT"
TARGET_MODE_GENERIC = "GENERIC_FK"
TARGET_MODE_PROFILE = "C94_PROFILE"
SUPPORTED_PRESETS = ("MIXAMO", "CMU", PRESET_PROFILE)


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=repr,
    )


def sha256_json(value):
    import hashlib

    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _generic_api():
    from . import retarget_generic_target as generic

    return generic


def _profile_api():
    try:
        from . import retarget_profile as profile
    except ImportError as exc:
        raise ValueError(
            "当前包缺少最终revision3 retarget_profile；PROFILE队列拒绝，"
            "不能退回Mixamo/CMU或静默换求解"
        ) from exc
    return profile


def target_mode(preset, target_policy):
    if preset == "MMD":
        return TARGET_MODE_MMD
    if preset in ("CONTACT_CORRECTION", "CONTACT_DETECTION"):
        return TARGET_MODE_CONTACT
    if preset == PRESET_PROFILE:
        return TARGET_MODE_PROFILE
    if target_policy is not None:
        return TARGET_MODE_GENERIC
    return TARGET_MODE_C94


def normalize_target_policy(target, policy, *, source_mapping=None):
    """Validate one explicit, acknowledged JSON target policy."""
    require(isinstance(policy, dict), "target_policy必须为纯JSON对象")
    generic = _generic_api()
    generic.validate(target, policy)
    require(policy.get("acknowledged") is True,
            "普通FK目标必须明确确认：输出骨骼Action，不是原控制器动画")
    if source_mapping is not None:
        require(set(policy.get("mapping") or {}) <= set(source_mapping["bones"]),
                "target_policy含当前来源没有的语义；请为当前来源建立明确策略")
    return policy


def normalize_source_profile(profile):
    """Load a pure-JSON profile and verify its content hash; never execute it."""
    profile_api = _profile_api()
    if isinstance(profile, str):
        text = profile
    elif isinstance(profile, dict):
        text = json.dumps(profile, ensure_ascii=False, allow_nan=False)
    else:
        raise ValueError("source_profile必须为纯JSON对象或JSON字符串")
    return profile_api.profile_from_json(text, verify_hash=True)


def validate_profile_job(source, target, profile):
    profile_api = _profile_api()
    validation = profile_api.validate_profile(profile, source, target)
    require(validation.get("applies"),
            "profile不可应用：" + "; ".join(validation.get("errors") or []))
    return validation


def profile_clip(action, slot, fps, start, end):
    """The worker's explicit selected Action/Slot/fps/range, not profile fallback."""
    return {
        "action": action,
        "slot": slot,
        "fps": float(fps),
        "range": (float(start), float(end)),
    }


def convert_generic(source, target, mapping, target_policy, *, item, settings,
                    progress=None, cancelled=None):
    """Call the reviewed generic target entry and verify the same policy."""
    generic = _generic_api()
    generic.validate(target, target_policy)
    result = generic.convert(
        source, target,
        source_mapping=mapping,
        target_mapping=dict(target_policy["mapping"]),
        source_fps=item["fps"], start=item["start"], end=item["end"],
        output_start=item["output_start"], motion=settings["motion"],
        sample_step=1, acknowledged=True,
        action_name=item["output_action_name"],
        progress=progress, cancelled=cancelled,
    )
    fresh = (result.get("report") or {}).get("target_policy")
    require(isinstance(fresh, dict),
            "通用目标转换未返回target_policy报告")
    require(fresh.get("signature") == target_policy.get("signature"),
            "转换重建的target_policy与入队记录不同；拒绝把结果当同一目标")
    generic.validate(target, fresh)
    return result


def convert_profile(source, target, profile, *, action, slot, item, settings,
                    progress=None, cancelled=None):
    """Call the final shared profile entry with selected Action/Slot explicit."""
    profile_api = _profile_api()
    clip = profile_clip(action, slot, item["fps"], item["start"], item["end"])
    result = profile_api.convert(
        source, target, profile, clip=clip, source_fps=item["fps"],
        start=item["start"], end=item["end"],
        output_start=item["output_start"], motion=settings["motion"],
        sample_step=1, action_name=item["output_action_name"],
        progress=progress, cancelled=cancelled,
    )
    action_object = result["action"]
    require(action_object.get(profile_api.CALIBRATION_KEY),
            "profile转换缺少校准记录，拒绝把结果当profile输出")
    require((result.get("report") or {}).get("profile") is not None,
            "profile转换缺少profile报告")
    return result


def _mode_from(value):
    return (value or {}).get("target_mode") or TARGET_MODE_C94


def _validated_profile_payload(payload):
    profile = normalize_source_profile(payload.get("source_profile"))
    expected = payload.get("source_profile_sha256")
    require(isinstance(expected, str) and sha256_json(profile) == expected,
            "source_profile内容哈希与提交快照不符")
    return profile


def validate_worker_payload(target, payload):
    """Worker-side gate before any item mutates source/target binding."""
    mode = _mode_from(payload)
    if mode in (TARGET_MODE_MMD, TARGET_MODE_CONTACT):
        return mode
    if mode == TARGET_MODE_GENERIC:
        policy = payload.get("target_policy")
        require(isinstance(policy, dict), "GENERIC_FK缺少target_policy")
        generic = _generic_api()
        generic.validate(target, policy)
        expected = payload.get("target_policy_sha256")
        require(isinstance(expected, str) and sha256_json(policy) == expected,
                "target_policy内容哈希与request不符")
    elif mode == TARGET_MODE_PROFILE:
        _validated_profile_payload(payload)
    else:
        require(payload.get("target_policy") is None,
                "C94默认路径不接受target_policy；不静默切换目标模式")
        require(payload.get("source_profile") is None,
                "C94默认路径不接受source_profile；不静默切换来源模式")
    return mode


def validate_collect_target(target, meta):
    """Collect-side revalidation independent of the worker process."""
    mode = _mode_from(meta)
    if mode in (TARGET_MODE_MMD, TARGET_MODE_CONTACT):
        return mode
    if mode == TARGET_MODE_GENERIC:
        policy = meta.get("target_policy")
        require(isinstance(policy, dict), "快照缺少普通FK target_policy")
        generic = _generic_api()
        generic.validate(target, policy)
        expected = meta.get("target_policy_sha256")
        require(isinstance(expected, str) and sha256_json(policy) == expected,
                "target_policy内容哈希与快照不符")
    elif mode == TARGET_MODE_PROFILE:
        from . import retarget_c94 as c94

        settings = target.pose.bones.get("骨架设置")
        require(settings is not None and all(key in settings.keys()
                                             for key in c94.MODES),
                "PROFILE结果只能归入当前C94目标；目标控制图不符")
        _validated_profile_payload(meta)
    return mode


def validate_candidate_policy(target, action, marker=None, meta=None):
    """Preview/collect candidate policy gate shared with the existing core."""
    report = json.loads(action.get(rt.META, "{}"))
    source = marker or meta or {}
    mode = source.get("target_mode") or report.get("target_mode") or TARGET_MODE_C94
    if mode in (TARGET_MODE_MMD, TARGET_MODE_CONTACT):
        return mode
    if mode == TARGET_MODE_GENERIC:
        policy = report.get("target_policy")
        require(isinstance(policy, dict), "候选缺少普通FK target_policy")
        generic = _generic_api()
        generic.validate(target, policy)
        expected = source.get("target_policy_sha256")
        if expected:
            require(sha256_json(policy) == expected,
                    "候选target_policy与job/snapshot记录不同")
    elif mode == TARGET_MODE_PROFILE:
        profile_api = _profile_api()
        summary = report.get("profile")
        require(isinstance(summary, dict), "候选缺少profile报告")
        record = profile_api.load_calibration_from_action(action)
        require(record.get("captured_from_profile_sha256") == summary.get("profile_sha256")
                and record.get("profile_id") == summary.get("id")
                and record.get("mapping_sha256") == summary.get("mapping_sha256"),
                "候选profile报告与实际校准记录不一致")
        require(record.get("record_sha256") == report.get("profile_calibration_record_sha256")
                and record.get("identity_sha256") == report.get("profile_identity_sha256"),
                "候选校准记录与转换报告身份不一致")
        expected_intrinsic = source.get("source_profile_document_sha256")
        if source.get("source_profile") is not None:
            profile = _validated_profile_payload(source)
            expected_intrinsic = profile["profile_sha256"]
            require(summary.get("id") == profile.get("id")
                    and summary.get("mapping_sha256") == profile.get("mapping_sha256"),
                    "候选profile语义与提交快照不符")
        if marker and meta and meta.get("source_profile") is not None:
            profile = _validated_profile_payload(meta)
            require(marker.get("source_profile_sha256") == meta.get("source_profile_sha256"),
                    "候选profile marker与快照内容身份不符")
            require(expected_intrinsic == profile["profile_sha256"],
                    "候选profile marker与快照文档身份不符")
        require(isinstance(expected_intrinsic, str)
                and summary.get("profile_sha256") == expected_intrinsic,
                "候选profile与提交文档不一致；请重新生成")
        from . import retarget_c94 as c94

        settings = target.pose.bones.get("骨架设置")
        require(settings is not None and all(key in settings.keys()
                                             for key in c94.MODES),
                "PROFILE候选只能预览/收集到C94目标")
    return mode


def marker_fields(payload):
    mode = _mode_from(payload)
    return {
        "target_mode": mode,
        "target_policy_sha256": payload.get("target_policy_sha256"),
        "source_profile_sha256": payload.get("source_profile_sha256"),
    }
