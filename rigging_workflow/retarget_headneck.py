# SPDX-License-Identifier: GPL-3.0-or-later
"""Source-independent C94 head/neck base-Action allocation preview.

The fixed allocator is the project's GPL v012 visual candidate, copied without
parameter changes into retarget_headneck_allocation.py. Intent is reconstructed
from the selected Action's raw head basis and its current evaluated parent,
before the original local Limit Rotation. No source rig, source motion, Adapter
or overlay correction is read or created. Quality remains subject to review.
"""
import json
import math
import uuid
import bpy
from mathutils import Matrix,Euler
from . import retarget_action as rt,pose_switching as pose,pose_switching_leg as leg
from . import retarget_contact_samples as sampler
from . import retarget_headneck_allocation as allocation
from . import retarget_dynamic

META='c94_retarget_headneck_allocation_v1'
BASE_REFERENCE='c94_headneck_baseline_action_reference'
CONTROLS=allocation.CONTROLS


class HeadNeckRestorationError(RuntimeError):
    """Target restore failed; never turn this failure into a usable candidate."""


def _require(value,message):
    if not value:raise ValueError(message)


def _validate(rig,action,frames):
    metadata=pose._metadata(rig)
    _require(rig.data.pose_position=='POSE','头颈分担需要姿态求值模式')
    _require(not rt.has_preview(rig),'请先停止当前目标的重定向预览')
    scene=bpy.context.scene
    _require(scene.render.frame_map_old==scene.render.frame_map_new,'头颈分担不支持场景时间重映射')
    ad=rig.animation_data
    _require(ad is None or not ad.use_tweak_mode,'请先退出NLA Tweak模式')
    if action is None:
        _require(ad is not None and ad.action is not None and ad.action_slot is not None,'请明确绑定生成Action及其Slot，或显式提供生成Action')
        selected=ad.action;slot=ad.action_slot
    else:
        selected=action
        _require(isinstance(selected,bpy.types.Action),'action须为明确存在的生成Action')
        if ad is not None and ad.action==selected and ad.action_slot is not None:slot=ad.action_slot
        else:
            slots=[s for s in selected.slots if s.target_id_type=='OBJECT']
            _require(len(slots)==1,'未绑定的显式Action须只有一个明确OBJECT Slot')
            slot=slots[0]
    _require(not selected.library,'请先建立本地Action工作副本')
    _require(slot.target_id_type=='OBJECT' and slot in list(selected.slots),'当前OBJECT Slot不属于所选Action')
    try:report=json.loads(selected[rt.META])
    except (KeyError,TypeError,ValueError) as exc:raise ValueError('当前Action没有有效C94重定向记录') from exc
    # Both are emitted by this same frozen core: retarget_mmd.convert updates
    # the shared rt.META schema after convert_resolved builds the C94 Action.
    _require(isinstance(report,dict) and report.get('schema') in {'c94.retarget.action.v1','c94.retarget.mmd.v1'},'当前Action不是受支持的生成基础动作')
    _require(report.get('target_uid') and report['target_uid']==rig.get(rt.TARGET_UID),'当前Action与目标角色身份不符')
    _require(not report.get('target_policy'),'当前入口只支持原生C94目标')
    _require('c94_retarget_contact_correction_v1' not in selected,'请使用基础Action，不将overlay修正作为分担输入')
    fps=scene.render.fps/scene.render.fps_base
    _require(isinstance(report.get('target_fps'),(int,float)) and abs(report['target_fps']-fps)<1e-6,'场景帧率与生成Action记录不符')
    _require(sum(o.get(rt.TARGET_UID)==report['target_uid'] for o in scene.objects)==1,'角色标识被副本共享，请明确唯一C94目标')
    if 'background_job' in report:
        from . import retarget_job
        retarget_job.validate_candidate_target(rig,selected)
    if 'c94_retarget_mmd_face_v1' in selected:
        from . import retarget_mmd_face
        retarget_mmd_face.validate_preview(rig,selected,slot)
    if 'c94_retarget_mmd_gaze_v1' in selected:
        from . import retarget_mmd_gaze
        retarget_mmd_gaze.validate_preview(rig,selected,slot)
    _require(all(name in rig.pose.bones for name in CONTROLS),'缺少原生头颈控制器')
    _require(rig.pose.bones[CONTROLS[0]].rotation_mode=='QUATERNION' and rig.pose.bones[CONTROLS[1]].rotation_mode=='XYZ','本入口仅支持已验颈椎QUATERNION、头颅XYZ原模式')
    _require(all(name in metadata['deforms'] for name in ('形变-颈椎','形变-头颅')),'C94实际头颈输出记录缺失')
    bounds={};paths={};all_rotation_paths=set()
    for name in CONTROLS:
        pb=rig.pose.bones[name]
        _require(pb.parent is not None,'头颈原生父链缺失：'+name)
        _require(len(pb.constraints)==1,'头颈需保持原生单一LOCAL旋转限位：'+name)
        bounds[name]=allocation._configuration(pb)
        _require(all(math.isfinite(v) and lo<=hi for lo,hi in bounds[name] for v in (lo,hi)),'头颈限位无效')
        paths[name]=pb.path_from_id('rotation_quaternion' if pb.rotation_mode=='QUATERNION' else 'rotation_euler')
        all_rotation_paths.update(pb.path_from_id(field) for field in ('rotation_euler','rotation_quaternion','rotation_axis_angle','rotation_mode'))
        prefix=pb.path_from_id()+'.constraints['
        _require(not any(fc.data_path in all_rotation_paths or fc.data_path.startswith(prefix) for fc in (ad.drivers if ad else ())), '头颈旋转或限位由直接driver占用，需明确适配')
    # Check both controls together, after collecting the complete write domain.
    _require(not any(fc.data_path in all_rotation_paths for fc in (ad.drivers if ad else ())), '头颈旋转由直接driver占用')
    bags=[strip.channelbag(slot) for layer in selected.layers for strip in layer.strips if strip.type=='KEYFRAME' and strip.channelbag(slot) is not None]
    _require(len(bags)==1,'当前Slot需只有一个明确KEYFRAME channelbag')
    forbidden_prefixes=tuple(rig.pose.bones[n].path_from_id()+'.constraints[' for n in CONTROLS)
    mode_paths={rig.pose.bones[n].path_from_id('rotation_mode') for n in CONTROLS}
    _require(not any(fc.data_path in mode_paths or fc.data_path.startswith(forbidden_prefixes) for fc in bags[0].fcurves),'头颈rotation_mode或限位参数被动画化，需明确适配')
    curves=[fc for fc in bags[0].fcurves if fc.data_path in set(paths.values())]
    expected={(paths[CONTROLS[0]],i) for i in range(4)}|{(paths[CONTROLS[1]],i) for i in range(3)}
    _require(len(curves)==7 and {(fc.data_path,fc.array_index) for fc in curves}==expected,'头颈原生七条旋转通道不完整或重复')
    _require(all(not fc.modifiers and not fc.sampled_points and len(fc.keyframe_points)>0 and not fc.mute for fc in curves),'头颈旋转曲线含未支持修饰器、采样曲线、静音或空通道')
    native_times=sorted({float(k.co.x) for fc in curves for k in fc.keyframe_points})
    _require(native_times and all(math.isfinite(f) for f in native_times),'头颈key时间无效')
    if frames is None:times=native_times
    else:
        _require(isinstance(frames,(list,tuple)) and frames,'frames须为明确非空时间列表')
        _require(all(type(f) in (int,float) and math.isfinite(f) for f in frames),'frames包含非法时间')
        times=[float(f) for f in frames]
        _require(all(a<b for a,b in zip(times,times[1:])),'frames须严格递增且不重复')
        _require(times[0]==native_times[0] and times[-1]==native_times[-1],'frames须覆盖原旋转Action完整首末时间；短段请先建立明确独立clip，避免范围外HOLD')
    _require(len(times)<=50000,'超出既有50000采样点预算')
    return selected,slot,paths,bounds,times


def _bind(rig,action,slot):
    ad=rig.animation_data_create()
    ad.action=action;ad.action_slot=slot;ad.use_nla=False
    ad.action_blend_type='REPLACE';ad.action_influence=1.;ad.action_extrapolation='HOLD'


def _legal_rotations(rig,evaluated,bounds):
    result={}
    for name in CONTROLS:
        pb=rig.pose.bones[name]
        q=leg._basis(pb,evaluated.pose.bones[name].matrix,evaluated).to_quaternion()
        e=q.to_euler('XYZ')
        legal=Euler([max(lo,min(hi,float(e[i]))) for i,(lo,hi) in enumerate(bounds[name])],'XYZ')
        if pb.rotation_mode=='QUATERNION':
            value=legal.to_quaternion()
            if value.dot(pb.rotation_quaternion)<0:value.negate()
        else:value=legal
        _require(all(math.isfinite(v) for v in value),'头颈求值产生非有限旋转')
        result[name]=list(value)
    return result


def rebalance_action(rig,*,action=None,frames=None,action_name=None):
    """Return ``{'action','slot','policy'}``; restore inputs, do not apply it.

    Default sample times are exactly the union of the selected Slot's seven
    head/neck rotation-key times. Explicit ``frames`` must cover that complete
    first/last domain; short segments need a separate explicit clip first.
    This never establishes a new source time mapping or silently truncates it.
    An explicit unbound Action must have one OBJECT Slot; the true current
    binding is restored afterwards. Existing rt/sampler recovery covers target
    declared inputs and Scene time;
    arbitrary external callbacks/IDs are outside that existing contract.
    """
    old,slot,paths,bounds,times=_validate(rig,action,frames)
    _require(action_name is None or isinstance(action_name,str) and action_name.strip(),'action_name须为非空字符串')
    capture=sampler._capture_state(bpy.context.scene,rig,state_core=rt)
    new=None;failure=None;rows=[];samples={name:[] for name in CONTROLS};previous={}
    def set_rotation(pb,q):
        prior=previous.get(pb.name)
        if pb.rotation_mode=='QUATERNION':
            if prior is not None and q.dot(prior)<0:q.negate()
            pb.rotation_quaternion=q;previous[pb.name]=q.copy()
        else:
            e=q.to_euler(pb.rotation_mode,prior if prior is not None else pb.rotation_euler)
            pb.rotation_euler=e;previous[pb.name]=e.copy()
    try:
        retarget_dynamic.suspend(rig,capture['state']['dynamic'])
        for frame in times:
            _bind(rig,old,slot)
            scene=bpy.context.scene;scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame))
            evaluated=pose._update(rig);head=rig.pose.bones[CONTROLS[1]]
            # Native pre-LIMIT pose from the original raw Action basis and
            # actual evaluated parent. The already-limited C is not intent.
            goal=head.bone.convert_local_to_pose(head.matrix_basis,head.bone.matrix_local,
                parent_matrix=evaluated.pose.bones[head.parent.name].matrix,
                parent_matrix_local=head.parent.bone.matrix_local)
            _require(all(math.isfinite(v) for row in goal for v in row),'未限位头部目标矩阵含非有限值')
            rig.animation_data.action=None;rig.animation_data.use_nla=False
            diagnostic=allocation.allocate(rig,goal,pose._update,leg._basis,set_rotation)
            values=_legal_rotations(rig,pose._update(rig),bounds)
            for name in CONTROLS:samples[name].append(values[name])
            rows.append({'frame':frame,'diagnostic':diagnostic})
        new=old.copy();new.name=action_name or old.name+'_头颈分担预览';new.use_fake_user=True
        new[BASE_REFERENCE]=old
        newslot=next(s for s in new.slots if s.identifier==slot.identifier)
        bags=[strip.channelbag(newslot) for layer in new.layers for strip in layer.strips if strip.type=='KEYFRAME' and strip.channelbag(newslot) is not None]
        bag=bags[0];extrap={}
        for fc in list(bag.fcurves):
            if fc.data_path in set(paths.values()):
                extrap[(fc.data_path,fc.array_index)]=fc.extrapolation;bag.fcurves.remove(fc)
        for name in CONTROLS:
            for component in range(len(samples[name][0])):
                fc=bag.fcurves.new(paths[name],index=component);fc.extrapolation=extrap[(paths[name],component)]
                fc.keyframe_points.add(len(times));fc.keyframe_points.foreach_set('co',[v for f,value in zip(times,samples[name]) for v in (f,value[component])])
                for key in fc.keyframe_points:key.interpolation='LINEAR'
                fc.update()
        policy={'schema':'c94.headneck.base_allocation.v1','id':uuid.uuid4().hex,'policy':'half_head_Y_overflow_neck_cap_15deg','source_independent':True,'intent':'native_pre_LIMIT_control_from_raw_basis_and_evaluated_parent','input_action':old.name,'slot':slot.identifier,'samples':len(times),'frames':times,'frames_origin':'original_rotation_key_union' if frames is None else 'explicit_complete_original_key_domain','applied_frames':[r['frame'] for r in rows if r['diagnostic'].get('applied')],'diagnostics':rows,'replaced_channels':7,'overlay_correction':False,'original_limits_active':True,'inherited_retarget_metadata':'unchanged historical baseline report; inherited residuals/quality are not new measured results','approval':None,'visual_review_status':'pending','production_published':False,'allocation_source':'project-owned GPL-3.0-or-later v012 headneck_visual.py, fixed half overflow and15deg cap'}
        policy['dependent_dynamics']=retarget_dynamic.report(capture['state']['dynamic'])
        new[META]=json.dumps(policy,ensure_ascii=False,allow_nan=False)
    except BaseException as exc:failure=exc
    try:
        rt.restore(rig,capture['state'])
        guard=sampler._declared_state_equal(capture)
        _require(all(guard.values()),'头颈生成后原始raw/绑定恢复保护失败')
    except BaseException as exc:
        if new is not None:
            try:bpy.data.actions.remove(new)
            except Exception as cleanup:raise HeadNeckRestorationError('头颈基础分担恢复及本次候选清理失败；保留现场检查') from cleanup
        raise HeadNeckRestorationError('头颈基础分担恢复失败；没有可采纳候选') from exc
    if failure is not None:
        if new is not None:bpy.data.actions.remove(new)
        raise failure
    return {'action':new,'slot':newslot,'policy':policy}
