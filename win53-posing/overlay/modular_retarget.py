# SPDX-License-Identifier: GPL-3.0-or-later
"""Modular human entry and explicit retarget-profile bridge.

``convert(source, target, mapping=None, ...)`` preserves the original modular
entry used by UI/AI code: it resolves ``retarget_source.mapping`` and
``active_clip`` and calls the shared ``retarget_action.convert_resolved``
directly.  ``convert(source, target, profile=..., ...)`` is the explicit
profile bridge: the existing ``retarget_profile`` module remains the owner of
profile identity, source evidence, mapping and clip records.  The two paths are
mutually exclusive; neither adds a second conversion/preview engine.

Supported profile calibration in this candidate:
  * orientation_reference = REST
  * translation_origin = CLIP_START
  * motion_scale_basis = LEG_LENGTH
  * unit_division = {source: 0, target: 1}

REST_HIP / REST_HIP_HEIGHT profiles are rejected explicitly, never silently
rewritten.  Finger and face channels are not applicable to the modular body
target: source finger mappings may be validated, but no finger Action channel
is written.
"""

from __future__ import annotations

import json
import math

import bpy

from . import retarget_action as rt
from . import retarget_modular_target as mod
from . import retarget_profile as profile_api
from . import retarget_source as source_api


BRIDGE_SCHEMA = 'rigging.modular.retarget_profile_bridge.v1'
SOURCE_CALIBRATION_SCHEMA = 'rigging.modular.source_calibration.v1'
SUPPORTED_ORIGIN = 'CLIP_START'
SUPPORTED_SCALE = 'LEG_LENGTH'
FINGER_POLICY = 'NOT_APPLICABLE_MODULAR_TARGET_HAS_NO_FINGER_CONTROLS'
FACE_POLICY = 'NOT_APPLICABLE_MODULAR_TARGET_HAS_NO_FACE_MORPH_GAZE_CONTROLS'


def require(value, message):
    if not value:
        raise ValueError(message)


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _current_clip(source, profile, *, clip=None, source_fps=None):
    if clip is not None:
        return profile_api._validate_explicit_clip(
            source, clip, source_fps=source_fps)
    return profile_api._profile_clip_from_current(
        source, profile, source_fps=source_fps)


def _current_clip_without_profile(source, *, clip=None, source_fps=None):
    if clip is not None:
        return profile_api._validate_explicit_clip(
            source, clip, source_fps=source_fps)
    return source_api.active_clip(source, source_fps)


def _profile_clip_range(profile, current_clip):
    record = profile.get('clip') or {}
    action = record.get('action')
    slot = record.get('slot')
    fps = record.get('fps')
    value = record.get('range')
    require(isinstance(value, (list, tuple)) and len(value) == 2,
            'profile.clip.range必须明确为两元素范围')
    start, end = float(value[0]), float(value[1])
    require(math.isfinite(start) and math.isfinite(end) and start < end,
            'profile.clip.range无效')
    require(action == current_clip['action'].name,
            '当前源Action与profile记录不符：%r != %r'
            % (current_clip['action'].name, action))
    require(slot == current_clip['slot'].identifier,
            '当前源Slot与profile记录不符：%r != %r'
            % (current_clip['slot'].identifier, slot))
    require(fps is not None and abs(float(fps) - float(current_clip['fps'])) <= 1e-9,
            '当前源fps与profile记录不符')
    current_start, current_end = (float(current_clip['range'][0]),
                                  float(current_clip['range'][1]))
    require(current_start <= start < end <= current_end,
            'profile.clip.range不在当前Action范围内：%s..%s vs %s..%s'
            % (start, end, current_start, current_end))
    return start, end


def _bridge_mapping(profile, validation, current_clip, start, end):
    mapped = dict(profile_api.mapped_mapping(profile))
    bones = mapped.get('bones')
    require(isinstance(bones, dict) and bones, 'profile mapping必须提供bones')
    missing = sorted(set(profile_api.REQUIRED_BODY) - set(bones))
    require(not missing,
            '模块化人形源映射缺少必需身体语义，拒绝猜补：' + ', '.join(missing))
    for semantic, name in bones.items():
        require(isinstance(name, str) and name,
                'profile mapping含空骨名：' + str(semantic))
    calibration = profile.get('calibration') or {}
    require(calibration.get('orientation_reference') == profile_api.ORIENTATION_REFERENCE,
            'profile校准朝向参考必须为REST')
    origin = calibration.get('translation_origin')
    scale = calibration.get('motion_scale_basis')
    require(origin == SUPPORTED_ORIGIN,
            '模块化Profile桥当前只支持CLIP_START；profile请求=%r，'
            '需要主线程决定是否扩展Adapter' % origin)
    require(scale == SUPPORTED_SCALE,
            '模块化Profile桥当前只支持LEG_LENGTH；profile请求=%r，'
            '需要主线程决定是否扩展Adapter' % scale)
    require(calibration.get('unit_division') == {'source': 0, 'target': 1},
            'profile unit_division必须为source=0,target=1')
    clip_record = profile.get('clip') or {}
    require(clip_record.get('origin_mode') == SUPPORTED_ORIGIN,
            'profile.clip.origin_mode必须为CLIP_START')
    origin_world = clip_record.get('source_hip_world')
    require(isinstance(origin_world, (list, tuple)) and len(origin_world) == 3,
            'profile.clip.source_hip_world缺失，不能静默冒充CLIP_START')
    source_calibration = {
        'schema': SOURCE_CALIBRATION_SCHEMA,
        'profile_schema': profile.get('schema'),
        'profile_preset': profile.get('preset'),
        'profile_tier': validation.get('tier'),
        'orientation_reference': 'REST',
        'translation_origin': SUPPORTED_ORIGIN,
        'motion_scale_basis': SUPPORTED_SCALE,
        'unit_division': {'source': 0, 'target': 1},
        'source_origin_frame': float(start),
        'source_origin_world': [float(value) for value in origin_world],
        'source_rest_fingerprint': (profile.get('source') or {}).get(
            'rest_fingerprint'),
        'source_mapping_sha256': profile.get('mapping_sha256'),
        'include_fingers': False,
        'finger_policy': FINGER_POLICY,
        'face_policy': FACE_POLICY,
        'morph_policy': FACE_POLICY,
        'gaze_policy': FACE_POLICY,
        'target_calibration_reuse': False,
    }
    profile_record = {
        'schema': BRIDGE_SCHEMA,
        'profile_schema': profile.get('schema'),
        'profile_preset': profile.get('preset'),
        'profile_tier': validation.get('tier'),
        'profile_id': profile.get('id'),
        'mapping_preset': mapped.get('preset'),
        'mapping_sha256': profile.get('mapping_sha256'),
        'source_rest_fingerprint': (profile.get('source') or {}).get(
            'rest_fingerprint'),
        'clip': {
            'action': clip_record.get('action'),
            'slot': clip_record.get('slot'),
            'fps': clip_record.get('fps'),
            'range': [float(start), float(end)],
            'origin_mode': SUPPORTED_ORIGIN,
            'source_origin_frame': float(start),
        },
        'source_fps': float(current_clip['fps']),
        'target_fps': float(bpy.context.scene.render.fps
                            / bpy.context.scene.render.fps_base),
        'timebase_policy': (
            'source profile fps -> target scene fps via shared '
            'convert_resolved frame ratio; no source retime/write'),
        'target_calibration_reuse': False,
        'finger_policy': FINGER_POLICY,
        'face_policy': FACE_POLICY,
        'morph_policy': FACE_POLICY,
        'gaze_policy': FACE_POLICY,
        'include_fingers': False,
        'limitations': list(profile.get('limitations') or []),
    }
    mapped['modular_source_calibration'] = source_calibration
    mapped['modular_profile'] = profile_record
    mapped['modular_include_fingers'] = False
    mapped['fingers'] = {'complete': [], 'skipped': {
        'all': FINGER_POLICY}}
    return mapped, profile_record, source_calibration


def validate(source, target, profile, *, clip=None, source_fps=None,
             check_files=True):
    """Validate a profile for the modular target without converting."""
    source_api.armature(source)
    source_api.armature(target)
    target_record = mod.policy(target)
    mod.validate(target, target_record)
    profile_fps = (profile.get('clip') or {}).get('fps')
    if source_fps is None:
        source_fps = profile_fps
    current_clip = _current_clip(source, profile, clip=clip,
                                 source_fps=source_fps)
    validation = profile_api.validate_profile(
        profile, source, target=None, clip=current_clip,
        check_files=check_files)
    require(validation.get('ok') and validation.get('applies'),
            'profile不可应用：' + '; '.join(validation.get('errors') or []))
    start, end = _profile_clip_range(profile, current_clip)
    mapped, profile_record, source_calibration = _bridge_mapping(
        profile, validation, current_clip, start, end)
    return {
        'schema': BRIDGE_SCHEMA,
        'profile_validation': validation,
        'target_policy': target_record,
        'clip': current_clip,
        'range': [start, end],
        'mapping': mapped,
        'profile': profile_record,
        'source_calibration': source_calibration,
        'finger_policy': FINGER_POLICY,
    }


def _convert_mapping(source, target, *, mapping=None, source_fps=None,
                     start=None, end=None, output_start=1,
                     motion='PRESERVE', sample_step=1, action_name=None,
                     progress=None, cancelled=None, clip=None,
                     adapter_factory=None):
    """Original modular entry: no profile, no profile calibration."""
    source_api.armature(source)
    source_api.armature(target)
    target_record = mod.policy(target)
    mapped = source_api.mapping(source, mapping)
    current_clip = _current_clip_without_profile(
        source, clip=clip, source_fps=source_fps)
    kwargs = {}
    if adapter_factory is not None:
        kwargs['adapter_factory'] = adapter_factory
    return rt.convert_resolved(
        source, target, mapped=mapped, clip=current_clip,
        start=start, end=end, output_start=output_start, motion=motion,
        include_fingers=False, sample_step=sample_step,
        action_name=action_name, progress=progress, cancelled=cancelled,
        target_policy=target_record, **kwargs)


def _convert_profile_bridge(source, target, profile, *, clip=None,
                            source_fps=None, start=None, end=None,
                            output_start=1, motion='PRESERVE',
                            sample_step=1, action_name=None, progress=None,
                            cancelled=None, adapter_factory=None,
                            check_files=True):
    prepared = validate(source, target, profile, clip=clip,
                        source_fps=source_fps, check_files=check_files)
    mapped = prepared['mapping']
    current_clip = prepared['clip']
    profile_start, profile_end = prepared['range']
    if start is None:
        start = profile_start
    if end is None:
        end = profile_end
    start = float(start)
    end = float(end)
    require(start < end, '采样范围必须为正时长')
    require(profile_start <= start < end <= profile_end,
            '采样范围必须落在profile.clip.range内')
    selected = start != profile_start or end != profile_end
    current_factory = adapter_factory
    captured_selection = {}
    if selected:
        def selected_factory(current_source, current_target, current_mapping):
            # The shared core owns Action/Slot/REPLACE/no-NLA and source time.
            captured = profile_api.capture_clip(
                current_source, current_mapping, clip=current_clip,
                origin_frame=start, selected_start=start, selected_end=end,
                translation_origin=SUPPORTED_ORIGIN)
            runtime_profile = dict(profile, clip=captured['record'])
            calibrated, profile_record, source_calibration = _bridge_mapping(
                runtime_profile, prepared['profile_validation'],
                current_clip, start, end)
            profile_record['clip'].update({
                'action_range': [float(v) for v in current_clip['range']],
                'profile_range': [profile_start, profile_end],
                'selected_range': [start, end],
                'source_hip_world': list(captured['record']['source_hip_world']),
            })
            current_mapping['modular_source_calibration'] = source_calibration
            current_mapping['modular_profile'] = profile_record
            prepared['profile'] = profile_record
            prepared['source_calibration'] = source_calibration
            captured_selection.update(captured['record'])
            if adapter_factory is not None:
                return adapter_factory(current_source, current_target, current_mapping)
            return mod.Adapter(current_source, current_target, current_mapping,
                               prepared['target_policy'], motion=motion), None
        current_factory = selected_factory
    # Preserve the original IEEE zero signs without weakening snapshot hashes.
    zero_inputs = [(bone, axis, value) for bone in target.pose.bones
                   for axis, value in enumerate(bone.rotation_euler) if value == 0.0]
    conversion_error = None
    try:
        result = rt.convert_resolved(
            source, target, mapped=mapped, clip=current_clip,
            start=start, end=end, output_start=output_start, motion=motion,
            scale_motion=False, include_fingers=False, sample_step=sample_step,
            action_name=action_name, progress=progress, cancelled=cancelled,
            adapter_factory=current_factory,
            target_policy=prepared['target_policy'])
    except BaseException as exc:
        conversion_error = exc
        raise
    finally:
        try:
            changed = False
            for bone, axis, previous in zero_inputs:
                current = bone.rotation_euler[axis]
                if current == 0.0 and math.copysign(1.0, current) != math.copysign(1.0, previous):
                    bone.rotation_euler[axis] = previous
                    changed = True
            if changed:
                bpy.context.view_layer.update()
        except Exception as restoration_error:
            if conversion_error is not None:
                raise RuntimeError('转换与输入恢复均失败；原错误：%s；恢复错误：%s'
                                   % (conversion_error, restoration_error)) from conversion_error
            raise
    # The adapter's report_fields already carries source_profile /
    # source_calibration / finger_policy into the shared Action metadata.
    result['report']['profile_bridge'] = prepared['profile']
    result['profile_bridge'] = prepared['profile']
    if selected:
        result['report']['selected_clip'] = captured_selection
        result['report']['selected_range'] = [start, end]
        result['action'][rt.META] = json.dumps(result['report'], ensure_ascii=False)
    return result


def convert(source, target, profile=None, *, mapping=None, clip=None,
            source_fps=None, start=None, end=None, output_start=1,
            motion='PRESERVE', sample_step=1, action_name=None, progress=None,
            cancelled=None, adapter_factory=None, check_files=True):
    """Shared entry compatible with both original mapping and profile calls.

    ``mapping`` and ``profile`` are mutually exclusive.  With ``profile=None``
    the original ``retarget_source.mapping`` + ``active_clip`` semantics are
    preserved exactly; with a profile the explicit validation bridge is used.
    """
    if profile is None:
        require(mapping is None or isinstance(mapping, (dict, str)),
                'mapping必须是骨名映射/预设名或None')
        return _convert_mapping(
            source, target, mapping=mapping, source_fps=source_fps,
            start=start, end=end, output_start=output_start, motion=motion,
            sample_step=sample_step, action_name=action_name,
            progress=progress, cancelled=cancelled, clip=clip,
            adapter_factory=adapter_factory)
    require(mapping is None, 'profile与mapping互斥；请明确使用Profile或mapping')
    return _convert_profile_bridge(
        source, target, profile, clip=clip, source_fps=source_fps,
        start=start, end=end, output_start=output_start, motion=motion,
        sample_step=sample_step, action_name=action_name,
        progress=progress, cancelled=cancelled,
        adapter_factory=adapter_factory, check_files=check_files)


def convert_profile(source, target, profile, **kwargs):
    """Explicit-name alias for the profile bridge; behavior is ``convert``."""
    return convert(source, target, profile=profile, **kwargs)
