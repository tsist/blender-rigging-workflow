"""Generate a private C94 character rig without changing its approved reference.

The original constraint graph remains in one armature. Skin outputs retain the
approved rest frames and calibrated linear transforms, including affine shear.
Native connections and semantic source-relative translations retain new pivots.
"""
import json
import math
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector
from .skeleton_neutral_53 import verify_neutral

RIG_KEY = 'c94_generated_rig_v1'
MECHANISM = '绑定机制-'
ANCHOR = '绑定零点-'
CALIBRATED = '绑定校准-'
BONE_FIELDS = (
    'bbone_segments', 'bbone_x', 'bbone_z', 'bbone_mapping_mode',
    'bbone_handle_type_start', 'bbone_handle_type_end',
    'bbone_easein', 'bbone_easeout', 'bbone_curveinx', 'bbone_curveinz',
    'bbone_curveoutx', 'bbone_curveoutz', 'bbone_rollin', 'bbone_rollout',
    'bbone_scalein', 'bbone_scaleout', 'use_endroll_as_inroll', 'use_scale_easing',
    'bbone_handle_use_ease_start', 'bbone_handle_use_ease_end',
    'bbone_handle_use_scale_start', 'bbone_handle_use_scale_end',
)
POSE_FIELDS = (
    'bbone_easein', 'bbone_easeout', 'bbone_curveinx', 'bbone_curveinz',
    'bbone_curveoutx', 'bbone_curveoutz', 'bbone_rollin', 'bbone_rollout',
    'bbone_scalein', 'bbone_scaleout',
)
HANDLE_FIELDS = ('bbone_custom_handle_start', 'bbone_custom_handle_end')


def _value(value):
    return list(value) if not isinstance(value, str) and hasattr(value, '__len__') else value


def _update(rig):
    rig.update_tag(refresh={'OBJECT', 'DATA'})
    bpy.context.view_layer.update()
    return rig.evaluated_get(bpy.context.evaluated_depsgraph_get())


def _factor_affine(matrix):
    """TRS × rotation represents a general invertible affine bind offset."""
    affine = np.array(matrix, dtype=float)
    u, singular, vt = np.linalg.svd(affine[:3, :3])
    if min(abs(singular)) < 1e-9:
        raise ValueError('绑定帧不可逆，不能生成校准骨')
    if np.linalg.det(u) < 0:
        u[:, -1] *= -1
        singular[-1] *= -1
    if np.linalg.det(vt) < 0:
        vt[-1, :] *= -1
        singular[-1] *= -1
    first = np.eye(4)
    first[:3, :3] = u @ np.diag(singular)
    first[:3, 3] = affine[:3, 3]
    second = np.eye(4)
    second[:3, :3] = vt
    return Matrix(first.tolist()), Matrix(second.tolist())


def _rest_record(bone):
    return {'matrix': bone.matrix_local.copy(), 'length': bone.length,
            'parent': bone.parent.name if bone.parent else None,
            'connected': bone.use_connect}


def _set_rest(edit_bone, record):
    # A fresh EditBone has zero length: matrix assignment alone loses direction.
    edit_bone.length = record['length']
    edit_bone.matrix = record['matrix']


def _calibrate_poles(rig):
    """Measure native pole response; refine only an unsuccessful first step."""
    report = []
    for owner in rig.pose.bones:
        for constraint in owner.constraints:
            if constraint.type != 'IK' or not constraint.pole_target:
                continue
            chain = [owner]
            while chain[-1].parent and len(chain) < constraint.chain_count:
                chain.append(chain[-1].parent)
            chain.reverse()
            if len(chain) < 2:
                raise ValueError('极向校准要求至少两根链骨：' + owner.name)
            first = chain[0]
            rest_start = first.bone.head_local
            rest_joint = first.bone.tail_local
            rest_end = chain[-1].bone.tail_local
            rest_axis = (rest_end - rest_start).normalized()
            rest_radial = rest_joint - rest_start - rest_axis * (rest_joint - rest_start).dot(rest_axis)
            length = sum(pb.bone.length for pb in chain)
            if rest_radial.length < length * 1e-5:
                raise ValueError('完全直链缺少稳定弯曲方向，请先调整参考骨：' + owner.name)
            initial = _update(rig)
            parent_delta = Matrix.Identity(4)
            if first.parent:
                parent_delta = initial.pose.bones[first.parent.name].matrix @ first.parent.bone.matrix_local.inverted()
            intended = parent_delta @ rest_joint
            old = constraint.pole_angle

            def sample(theta):
                constraint.pole_angle = theta
                evaluated = _update(rig)
                start = evaluated.pose.bones[first.name].head.copy()
                end = evaluated.pose.bones[chain[-1].name].tail.copy()
                joint = evaluated.pose.bones[first.name].tail.copy()
                axis = (end - start).normalized()
                radial = joint - start - axis * (joint - start).dot(axis)
                wanted = intended - start - axis * (intended - start).dot(axis)
                return axis, radial, wanted

            def signed(axis, actual, wanted):
                if min(actual.length, wanted.length) < 1e-8:
                    raise ValueError('实际IK极向响应不足，停止生成：' + owner.name)
                value = math.atan2(axis.dot(actual.cross(wanted)), actual.dot(wanted))
                if not math.isfinite(value):
                    raise ValueError('实际IK极向响应不足，停止生成：' + owner.name)
                return value

            def wrap(value):
                return (value + math.pi) % (2 * math.pi) - math.pi

            try:
                # Keep the established first estimate and successful angles.
                axis, baseline, wanted = sample(0.0)
                _, perturbed, _ = sample(0.05)
                derivative = math.atan2(axis.dot(baseline.cross(perturbed)), baseline.dot(perturbed)) / 0.05
                if not math.isfinite(derivative) or abs(derivative) < 0.5 or min(baseline.length, wanted.length) < 1e-8:
                    raise ValueError('实际IK极向响应不足，停止生成：' + owner.name)
                delta = math.atan2(axis.dot(baseline.cross(wanted)), baseline.dot(wanted))
                theta = wrap(delta / derivative)
                axis, actual, wanted = sample(theta)
                residual = signed(axis, actual, wanted)
                initial_error = error = abs(residual)
                refinements = 0
                for _ in range(8):
                    if error <= 0.02:
                        break
                    # Differentiate the complete signed residual: native IK
                    # also moves its evaluated axis and desired radial vector.
                    epsilon = 0.01
                    probe = signed(*sample(wrap(theta + epsilon)))
                    slope = wrap(probe - residual) / epsilon
                    if not math.isfinite(slope) or abs(slope) < 0.5:
                        break
                    step = max(-0.2, min(0.2, -residual / slope))
                    accepted = False
                    for scale in (1.0, 0.5, 0.25, 0.125):
                        trial = wrap(theta + step * scale)
                        trial_residual = signed(*sample(trial))
                        trial_error = abs(trial_residual)
                        if trial_error <= 0.02 or trial_error < error - 1e-6:
                            theta, residual, error = trial, trial_residual, trial_error
                            refinements += 1
                            accepted = True
                            break
                    if not accepted:
                        break
                # Restore the chosen angle after derivative/backtracking probes
                # and qualify the actual native result, not a saved score.
                if refinements or error > 0.02:
                    residual = signed(*sample(theta))
                    error = abs(residual)
                if error > 0.02:
                    raise ValueError('实际IK极向校准未收敛：' + owner.name)
                report.append({'bone': owner.name, 'old_angle': old, 'angle': constraint.pole_angle,
                               'radial_error_radians': error, 'bend_height': rest_radial.length,
                               'initial_radial_error_radians': initial_error,
                               'refinement_iterations': refinements})
            except Exception:
                constraint.pole_angle = old
                _update(rig)
                raise
    return report


def validate_references(rig):
    """Validate actual RNA consumers after Blender has remapped renamed bones."""
    issues = []
    for pb in rig.pose.bones:
        for c in pb.constraints:
            pairs = [(getattr(c, a, None), getattr(c, b, '')) for a, b in (
                ('target', 'subtarget'), ('pole_target', 'pole_subtarget'),
                ('space_object', 'space_subtarget'))]
            pairs += [(t.target, t.subtarget) for t in getattr(c, 'targets', ())]
            for target, name in pairs:
                if name and target is None:
                    issues.append(f'{pb.name}/{c.name}: missing target for {name}')
                if name and target and target.type == 'ARMATURE' and name not in target.data.bones:
                    issues.append(f'{pb.name}/{c.name}: {name}')
            if not c.mute and c.influence > 0 and not c.is_valid:
                issues.append(f'{pb.name}/{c.name}: invalid active constraint')
    driver_count = 0
    for owner in (rig, rig.data):
        for fcurve in owner.animation_data.drivers if owner.animation_data else ():
            driver_count += 1
            if not fcurve.mute and not fcurve.driver.is_valid:
                issues.append('invalid active driver: ' + fcurve.data_path)
            try:
                owner.path_resolve(fcurve.data_path)
            except (ValueError, KeyError):
                issues.append('driver output: ' + fcurve.data_path)
            for variable in fcurve.driver.variables:
                for target in variable.targets:
                    if target.id is None:
                        issues.append('driver missing ID: ' + fcurve.data_path)
                    elif target.data_path:
                        try:
                            target.id.path_resolve(target.data_path)
                        except (ValueError, KeyError):
                            issues.append('driver input: ' + target.data_path)
                    elif target.bone_target and hasattr(target.id, 'pose'):
                        if target.bone_target not in target.id.pose.bones:
                            issues.append('driver bone: ' + target.bone_target)
    return {'issues': issues, 'drivers': driver_count}


def validate_input(scene, reference):
    """Reject stale/unsynchronised positioning, without mutating its reference.

    The atlas fingerprint belongs to the source template, not this fitted rig.
    The current fitted signature records exactly what the caller chose to use.
    """
    from . import positioning, editing
    state = positioning.read_state(scene)
    if state and state.get('fitted_reference') == reference.name:
        if reference.get(positioning.OWNER_KEY) != state.get('owner'):
            raise ValueError('参考骨与当前定位记录身份不匹配')
        if state.get('fit_stale'):
            raise ValueError('定位候选已更新，请先重新拟合完整参考骨再生成')
        if state.get('editing'):
            if editing._native_changes(scene, state):
                raise ValueError('标记或参考骨有未同步手改，请先点击“同步手调”再生成')
        else:
            markers = positioning._marker_objects(scene, state)
            if any((markers[m['key']].matrix_world.translation - Vector(m['position'])).length > 1e-7
                   for m in state['markers']):
                raise ValueError('定位标记有未同步修改，请先更新参考骨再生成')
    return positioning.reference_signature(reference)


def _link_bbone_channels(rig, names, renamed):
    # Native pose B-Bone channels are connected separately from bone transforms.
    # The neutral offset is calibrated against the evaluated mechanism pose.
    evaluated = _update(rig)
    source_values = {name: {field: _value(getattr(evaluated.pose.bones[renamed[name]], field))
                            for field in POSE_FIELDS} for name in names}
    driven_paths = {fc.data_path for fc in rig.animation_data.drivers} if rig.animation_data else set()
    rows = []
    for name in names:
        out = rig.pose.bones[name]
        if out.bone.bbone_segments <= 1:
            continue
        for field in POSE_FIELDS:
            neutral = source_values[name][field]
            bone_value = _value(getattr(out.bone, field))
            multiplicative = field in {'bbone_scalein', 'bbone_scaleout'}
            size = len(neutral) if isinstance(neutral, list) else 0
            if multiplicative and any(abs(x) < 1e-9 for x in neutral):
                raise ValueError('B-Bone绑定缩放不可为零：' + name)
            if size:
                adjusted = [a * b if multiplicative else a + b for a, b in zip(bone_value, neutral)]
                setattr(out.bone, field, adjusted)
                setattr(out, field, [1.0 if multiplicative else 0.0] * size)
            else:
                setattr(out.bone, field, bone_value + neutral)
                setattr(out, field, 0.0)
            path = rig.pose.bones[renamed[name]].path_from_id(field)
            # Handles carry the other B-Bone motion. Keep only the channels with
            # native animation consumers; do not add thousands of idle drivers.
            if path not in driven_paths:
                continue
            for index in range(size or 1):
                fc = out.driver_add(field, index) if size else out.driver_add(field)
                variable = fc.driver.variables.new()
                variable.name = 'channel'
                variable.type = 'SINGLE_PROP'
                variable.targets[0].id = rig
                variable.targets[0].data_path = path + (f'[{index}]' if size else '')
                base = neutral[index] if size else neutral
                fc.driver.expression = f'channel / ({base!r})' if multiplicative else f'channel - ({base!r})'
            rows.append({'bone': name, 'field': field, 'source_neutral': neutral})
    return rows


def _build(rig, reference, collection, label, *, enabled_modules=None):
    source_names = list(rig.data.bones.keys())
    missing = set(source_names) - set(reference.data.bones.keys())
    if missing:
        raise ValueError('参考骨缺少模板骨：' + ', '.join(sorted(missing)[:8]))
    lengths = {b.name: b.length for b in rig.data.bones}
    desired = {name: _rest_record(reference.data.bones[name]) for name in source_names}
    # The separate expression board keeps its rigid layout, following the head.
    panel = rig.data.collections.get('表情面板')
    panel_names = [b.name for b in panel.bones] if panel else []
    if panel_names:
        source_head = rig.data.bones['头颅'].matrix_local.copy()
        target_head = reference.data.bones['头颅'].matrix_local.copy()
        transform = target_head @ source_head.inverted()
        for name in panel_names:
            desired[name] = _rest_record(rig.data.bones[name])
            desired[name]['matrix'] = transform @ desired[name]['matrix']
    deforms = [b.name for b in rig.data.bones if b.use_deform]
    handles = {getattr(rig.data.bones[n], field).name for n in deforms for field in HANDLE_FIELDS
               if getattr(rig.data.bones[n], field)}
    needed = set(deforms) | handles
    # Some source bones are BOTH direct deformers and visible daily controls.
    # Keep those control names/widgets and give only their skin output a prefix.
    direct_controls = {name for name in deforms if rig.pose.bones[name].custom_shape
                       and not rig.data.bones[name].hide_select}
    renamed = {name: name if name in direct_controls else MECHANISM + name for name in deforms}
    outputs = {name: '蒙皮-' + name if name in direct_controls else name for name in deforms}
    for name in needed:
        if len((CALIBRATED + name).encode('utf-8')) > 63 or len((ANCHOR + name).encode('utf-8')) > 63:
            raise ValueError('校准骨名超过Blender上限：' + name)
    for old, new in renamed.items():
        if (new != old and new in rig.data.bones) or len(new.encode('utf-8')) > 63:
            raise ValueError('机制骨命名冲突或过长：' + old)
    existing_after_rename=(set(source_names)-set(renamed)) | set(renamed.values())
    planned=list(outputs.values())+[ANCHOR+n for n in needed]+[CALIBRATED+n for n in needed]
    if len(set(planned))!=len(planned) or set(planned)&existing_after_rename:
        raise ValueError('最终形变或校准骨命名冲突；不能接受原生自动追加名称')
    memberships = {n: [c.name for c in rig.data.bones[n].collections] for n in deforms}
    settings = {n: {f: _value(getattr(rig.data.bones[n], f)) for f in BONE_FIELDS} for n in deforms}
    handle_map = {n: {f: getattr(rig.data.bones[n], f).name if getattr(rig.data.bones[n], f) else None
                      for f in HANDLE_FIELDS} for n in deforms}
    pose_values = {n: {f: _value(getattr(rig.pose.bones[n], f)) for f in POSE_FIELDS} for n in deforms}
    rig.matrix_world = reference.matrix_world.copy()
    # Procedural writes must never invoke the user's editing mirror mode.
    source_mirror = rig.data.use_mirror_x
    rig.data.use_mirror_x = False
    bpy.ops.object.mode_set(mode='EDIT')
    for bone in rig.data.edit_bones:
        bone.use_connect = False
    for bone in rig.data.edit_bones:
        _set_rest(bone, desired[bone.name])
    for bone in rig.data.edit_bones:
        bone.use_connect = desired[bone.name]['connected']
    bpy.ops.object.mode_set(mode='OBJECT')
    for pb in rig.pose.bones:
        for constraint in pb.constraints:
            if constraint.type == 'STRETCH_TO':
                constraint.rest_length *= pb.bone.length / lengths[pb.name]
    poles = _calibrate_poles(rig)
    # Retargeted eye-tail positions need a neutral-relative follow before the
    # output bind frame is frozen. Keep the native gaze/follow inputs active.
    if enabled_modules is None or enabled_modules['face']:
        from . import face_follow
        face_follow_report = face_follow.calibrate_owned(rig)
    else:
        face_follow_report = {'status': 'disabled_by_module_configuration', 'changed': False}
    evaluated = _update(rig)
    bind = {pb.name: pb.matrix.copy() for pb in evaluated.pose.bones}
    for old, new in renamed.items():
        if old != new:
            rig.data.bones[old].name = new
    bpy.ops.object.mode_set(mode='EDIT')
    for name in deforms:
        rig.data.edit_bones[renamed[name]].use_deform = False
        out = rig.data.edit_bones.new(outputs[name])
        _set_rest(out, desired[name])
        out.use_deform = True
    for name in deforms:
        parent = desired[name]['parent']
        if parent:
            rig.data.edit_bones[outputs[name]].parent = rig.data.edit_bones[outputs.get(parent, parent)]
            # A bind-frame offset preserves neutral coordinates, not a joint's
            # dynamic pivot. Retain the authored native connection when both
            # ends belong to the output chain. A mechanism parent is not an
            # interchangeable anatomical output parent.
            if parent in outputs and desired[name]['connected']:
                rig.data.edit_bones[outputs[name]].use_connect = True
    for name in sorted(needed):
        parent = rig.data.edit_bones[renamed.get(name, name)]
        frame = {'length': desired[name]['length'], 'matrix': parent.matrix.copy()}
        anchor = rig.data.edit_bones.new(ANCHOR + name)
        _set_rest(anchor, frame)
        anchor.parent = parent
        anchor.use_deform = False
        helper = rig.data.edit_bones.new(CALIBRATED + name)
        _set_rest(helper, frame)
        helper.parent = anchor
        helper.use_deform = False
    bpy.ops.object.mode_set(mode='OBJECT')
    helper_collection = rig.data.collections.new('绑定校准机制')
    helper_collection.is_visible = False
    for name in sorted(needed):
        helper = rig.pose.bones[CALIBRATED + name]
        anchor = helper.parent
        for pb in (anchor, helper):
            pb.bone.inherit_scale = 'FULL'
            pb.bone.use_inherit_rotation = True
            pb.bone.use_local_location = True
            pb.bone.hide_select = True
            helper_collection.assign(pb.bone)
        first, second = _factor_affine(bind[name].inverted() @ desired[name]['matrix'])
        anchor.matrix_basis = first
        helper.matrix_basis = second
    for name in deforms:
        out = rig.data.bones[outputs[name]]
        pb = rig.pose.bones[outputs[name]]
        mechanism = rig.data.bones[renamed[name]]
        if name not in direct_controls:
            for c in list(mechanism.collections):
                c.unassign(mechanism)
            helper_collection.assign(mechanism)
            mechanism.hide_select = True
        output_memberships = ['躯干形变'] if name in direct_controls else memberships[name]
        for cname in output_memberships:
            rig.data.collections[cname].assign(out)
        for field, value in settings[name].items():
            setattr(out, field, value)
        for field, handle in handle_map[name].items():
            setattr(out, field, rig.data.bones[CALIBRATED + handle] if handle else None)
        for field, value in pose_values[name].items():
            setattr(pb, field, value)
        constraint = pb.constraints.new('COPY_TRANSFORMS')
        constraint.name = '机制绑定帧校准'
        constraint.target = rig
        constraint.subtarget = CALIBRATED + name
    output_names = [outputs[name] for name in deforms]
    mechanism_mapping = {outputs[name]: renamed[name] for name in deforms}
    reference_mapping = {outputs[name]: name for name in deforms}
    connected_outputs = [outputs[name] for name in deforms
                         if rig.data.bones[outputs[name]].use_connect]
    connected_to_mechanism = [outputs[name] for name in deforms
                             if desired[name]['connected'] and desired[name]['parent'] not in outputs]
    channels = _link_bbone_channels(rig, output_names, mechanism_mapping)
    from . import joint_positions
    positions = joint_positions.build(rig, desired, outputs, renamed, CALIBRATED, helper_collection)
    evaluated = _update(rig)
    mechanism_error = max(max(abs(bind[n][i][j] - evaluated.pose.bones[renamed.get(n, n)].matrix[i][j])
                              for i in range(4) for j in range(4)) for n in source_names)
    refs = validate_references(rig)
    if refs['issues']:
        raise ValueError('生成后引用失效：' + '; '.join(refs['issues'][:8]))
    neutral = verify_neutral(rig, reference, output_names, reference_mapping)
    if neutral['sample_max'] > 1e-5 or neutral['head_tail_max'] > 1e-5 or mechanism_error > 1e-5:
        raise ValueError('绑定中立校验失败：' + json.dumps(neutral, ensure_ascii=False))
    metadata = {'schema': 'c94.generated.v1', 'label': label, 'reference': reference.name,
                'reference_object': reference.name,
                'mechanism_mapping': mechanism_mapping, 'deforms': output_names,
                'reference_mapping': reference_mapping, 'direct_controls': sorted(direct_controls),
                'connected_outputs': connected_outputs,
                'reference_connections_to_mechanism': connected_to_mechanism,
                'joint_positions': positions,
                'source_bones': len(source_names), 'bind_method': 'affine_linear_with_native_joint_positions',
                'source_edit_mirror': source_mirror, 'panel_bones': panel_names,
                'face_follow_neutral': face_follow_report,
                'skinning': 'not_bound', 'limitations': ['arm/leg pose matching limited to validated poses; other spaces not matched', 'character skinning pending']}
    rig[RIG_KEY] = json.dumps(metadata, ensure_ascii=False)
    removed_settings = []
    shared_settings = []
    settings = rig.pose.bones.get('骨架设置')
    for module, key in (('face', '脸部控制形态键-骨骼'), ('glute', '臀部跟随')):
        if enabled_modules is None or enabled_modules[module] or settings is None or key not in settings:
            continue
        path = settings.path_from_id() + '[' + json.dumps(key, ensure_ascii=False) + ']'
        consumers = [curve.data_path for owner in (rig, rig.data) if owner.animation_data
                     for curve in owner.animation_data.drivers
                     if any(t.id == rig and t.data_path == path for variable in curve.driver.variables for t in variable.targets)]
        if consumers and module == 'glute':
            shared_settings.append({'key': key, 'reason': 'retained_core_driver_consumers', 'outputs': sorted(consumers)})
            continue
        if consumers:
            raise ValueError('关闭模块仍有其专属属性消费者：' + key)
        del settings[key]
        removed_settings.append(key)
    from . import control_panel
    control_panel.install(rig, label=label)
    return {'source_bones': len(source_names), 'bones': len(rig.data.bones),
            'deforms': len(deforms), 'calibrated_frames': len(needed),
            'bbone_deforms': sum(rig.data.bones[n].bbone_segments > 1 for n in output_names),
            'bbone_channels': len(channels), 'references': refs, 'neutral': neutral,
            'mechanism_change_max': mechanism_error, 'panel_layout': 'rigid_head_frame',
            'mechanism_change_scope': 'source mechanism before/after output creation in neutral pose',
            'pole_calibration': poles, 'face_follow_neutral': face_follow_report,
            'direct_controls_preserved': sorted(direct_controls),
            'connected_outputs': connected_outputs,
            'reference_connections_to_mechanism': connected_to_mechanism,
            'joint_position_helpers': len(positions['relations']),
            'joint_position_method': positions['method'],
            'disabled_module_settings_removed': removed_settings,
            'disabled_module_shared_settings_retained': shared_settings,
            'rig': rig.name, 'skinning': 'not_bound'}


def _returned_import_ids(roots, before, library_path):
    """Adopt returned native append roots and their actual new ID dependencies.

    This runs at the import boundary, before generation/evaluation callbacks;
    unrelated later IDs are never added to the transaction's ownership list.
    """
    from . import module_configuration
    fresh = module_configuration._ids() - before
    owned = {item for item in roots if isinstance(item, bpy.types.ID) and item in fresh}
    users = bpy.data.user_map()
    pending = set(fresh) - owned
    changed = True
    while changed:
        changed = False
        for item in tuple(pending):
            if users.get(item, set()) & owned:
                owned.add(item); pending.remove(item); changed = True
    # Native append may retain a bookkeeping Library without a user_map edge.
    for item in fresh:
        if isinstance(item, bpy.types.Library):
            try:
                if Path(bpy.path.abspath(item.filepath)).resolve() == library_path:
                    owned.add(item)
            except (ValueError, OSError):
                pass
    return owned


def generate(scene, reference, template_file, label='Character', *, configuration=None, owned_ids=None):
    """Create a new owned rig. Failure discards only this call's imported IDs."""
    if owned_ids is not None and not isinstance(owned_ids, list):
        raise ValueError('owned_ids须为调用者的可写list或None')
    if scene != bpy.context.scene or not reference or reference.type != 'ARMATURE':
        raise ValueError('生成需要当前场景中的有效参考骨架')
    if reference.name not in scene.objects or (bpy.context.object and bpy.context.object.mode != 'OBJECT'):
        raise ValueError('请先回到对象模式并确认参考骨架属于当前场景')
    signature = validate_input(scene, reference)
    from . import module_configuration
    configured = None
    if configuration is not None:
        configured = module_configuration.validate(scene, reference, template_file, configuration)
        path = Path(configured['configured']['file']).resolve(strict=True)
    else:
        path = Path(template_file).resolve(strict=True)
    if path.suffix.lower() != '.blend':
        raise ValueError('模板必须是已提取的C94私有blend库')
    label = str(label).strip()
    name = label + '__控制骨架'
    if (not label or any(len(n.encode('utf8'))>63 for n in (name,label+'__绑定生成'))
            or name in bpy.data.objects or name in bpy.data.armatures
            or label+'__绑定生成' in bpy.data.collections):
        raise ValueError('角色名为空或已存在同名控制骨架；请选择新名称')
    loaded_sha = configured['configured']['expected_sha256'] if configured else module_configuration._hash(path)
    old_active = bpy.context.view_layer.objects.active
    old_selected = list(bpy.context.selected_objects)
    before_ids = module_configuration._ids()
    rig = None
    imported_ids = set()
    import_before = module_configuration._ids()
    target = None
    try:
        with bpy.data.libraries.load(str(path), link=False) as (source, target):
            names = [n for n in source.collections if n.endswith('__模板')]
            if len(names) != 1:
                raise ValueError('模板库必须包含一个已验证的C94模板集合')
            target.collections = names
        imported_ids = _returned_import_ids(target.collections, import_before, path)
        if owned_ids is not None:
            owned_ids.extend(imported_ids)
        collection = target.collections[0]
        if not collection.get('c94_private_template'):
            raise ValueError('该集合没有C94私有模板身份')
        if configured is None and module_configuration.DEFINITION_KEY in collection:
            raise ValueError('该库含模块配置定义；请显式传入对应configuration记录')
        scene.collection.children.link(collection)
        rigs = [o for o in collection.all_objects if o.type == 'ARMATURE']
        if len(rigs) != 1:
            raise ValueError('模板集合必须恰有一个完整控制骨架')
        rig = rigs[0]
        if configured is not None:
            module_configuration.validate_collection(collection, rig, configured)
        rig.name = name
        rig.data.name = name
        collection.name = label + '__绑定生成'
        rig.hide_set(False)
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        report = _build(rig, reference, collection, label,
                        enabled_modules=configured['definition']['enabled_modules'] if configured else None)
        from . import positioning
        if positioning.reference_signature(reference) != signature:
            raise ValueError('生成期间参考骨发生变化，候选已撤回')
        metadata = json.loads(rig[RIG_KEY])
        if configured is None and module_configuration._hash(path) != loaded_sha:
            raise ValueError('生成期间模板库字节改变，候选已撤回')
        metadata.update(reference_signature=signature, template_file=str(path),
                        template_sha256=loaded_sha,
                        base_template_sha256=(configured['definition']['base_template']['expected_sha256']
                                              if configured else loaded_sha))
        if configured is not None:
            module_configuration.validate(scene, reference, template_file, configured)
            metadata.update(module_configuration=configured, configuration_id=configured['id'],
                            base_template_file=configured['definition']['base_template']['file'])
            report['configuration_id'] = configured['id']
        from . import neutral_binding
        metadata[neutral_binding.KEY] = neutral_binding.capture_verified(
            rig, metadata, report['neutral'], report['mechanism_change_max'],
            float(scene.unit_settings.scale_length))
        rig[RIG_KEY] = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        report['reference_signature'] = signature
        rig.show_in_front = True
        return {'rig': rig, 'collection': collection, 'report': report}
    except Exception:
        if not imported_ids and target is not None:
            imported_ids = _returned_import_ids(getattr(target, 'collections', ()), import_before, path)
            if owned_ids is not None:
                owned_ids.extend(imported_ids)
        module_configuration._cleanup(before_ids, old_active, old_selected, owned_ids=imported_ids)
        raise


def install_shoulder_girdle(scene, reference, generated, landmarks, *, owned_ids=None):
    """Complete an optional generation component before review identity freezes."""
    from . import neutral_binding, shoulder_girdle

    rig, report = generated['rig'], generated['report']
    metadata = json.loads(rig[RIG_KEY])
    deforms = metadata['deforms']
    scale = float(scene.unit_settings.scale_length)
    neutral_binding.check_current(rig, metadata, deforms, scale)
    assembly = shoulder_girdle.install(rig, landmarks, owned_ids=owned_ids)
    verification = verify_neutral(rig, reference, deforms, metadata['reference_mapping'])
    # The original generation verifies source mechanisms while creating outputs.
    # Assembly records its own all-bone matrix comparison and final output probes.
    metadata[neutral_binding.KEY] = neutral_binding.capture_verified(
        rig, metadata, verification, report['mechanism_change_max'], scale)
    rig[RIG_KEY] = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    report.update(shoulder_girdle=assembly, assembled_neutral=verification)
