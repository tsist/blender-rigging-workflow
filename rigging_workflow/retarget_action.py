# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent body Action candidates and reversible native preview.

This module owns body/finger Action generation and temporary preview.
Publication, MMD face/camera, contact review and queues use separate modules.
"""
import json,math,time,uuid
import bpy
from . import retarget_source as source_api, retarget_c94 as c94
from . import pose_switching as pose

META='c94_retarget_action_v1'
TARGET_UID='c94_retarget_target_uid_v1'
_preview={}
_preview_stop_callbacks=[]
_AD_FIELDS=('action_blend_type','action_influence','action_extrapolation','use_nla')

def register_preview_stop_callback(callback):
    """Register a non-owning stop observer; stopping does not consult it."""
    if not callable(callback):
        raise ValueError('预览停止回调必须可调用')
    if callback not in _preview_stop_callbacks:
        _preview_stop_callbacks.append(callback)

def unregister_preview_stop_callback(callback):
    if callback in _preview_stop_callbacks:
        _preview_stop_callbacks.remove(callback)

def _notify_preview_stopped(target, session):
    errors=[]
    for callback in tuple(_preview_stop_callbacks):
        try:
            callback(target, session)
        except Exception as exc:
            # The legitimate restore has already completed.  Report observer
            # failure instead of turning a successful stop into a false failure.
            errors.append({'observer':getattr(callback,'__name__',repr(callback)),
                           'error':f'{type(exc).__name__}: {exc}'})
    return errors

def _preview_entry_alive(entry):
    """Check ID validity without reading session_uid from a removed target."""
    try:
        target=entry.get('target')
        scene=entry.get('state',{}).get('scene')
        if target is None or scene is None:
            return False
        target_name=target.name
        scene_name=scene.name
        return target_name in bpy.data.objects and scene_name in bpy.data.scenes
    except (ReferenceError,KeyError,AttributeError,TypeError):
        return False

def _purge_dead_previews():
    for key,entry in list(_preview.items()):
        if not _preview_entry_alive(entry):
            _preview.pop(key,None)

def bone_direction_report(diagnostics):
    """Keep complete orientation residuals; separately report observed bone Y."""
    rows=[r for r in diagnostics if 'direction_degrees' in r]
    report={'status':'available' if rows and len(rows)==len(diagnostics) else 'partial' if rows else 'not_available',
            'definition':'angle_between_observed_and_desired_local_y_in_target_armature_space',
            'observation_rows':len(rows),'total_observation_rows':len(diagnostics),
            'complete_orientation_required':True}
    if rows:
        report.update(max_degrees=max(r['direction_degrees'] for r in rows),
            by_semantic={s:max(r['direction_degrees'] for r in rows if r['semantic']==s)
                         for s in {r['semantic'] for r in rows}},
            worst_residuals=sorted(rows,key=lambda r:r['direction_degrees'],reverse=True)[:24])
    return report

def has_preview(target):
    if target is None:
        return False
    _purge_dead_previews()
    try:
        key=target.session_uid
    except ReferenceError:
        return False
    session=_preview.get(key)
    return session is not None and session.get('target')==target

def preview_target(scene=None):
    """Resolve the one active scene preview independently of mutable UI picks."""
    _purge_dead_previews()
    scene=scene or bpy.context.scene
    sessions=[entry for entry in _preview.values()
              if entry.get('state',{}).get('scene')==scene and _preview_entry_alive(entry)]
    source_api.require(len(sessions)==1,'当前场景没有唯一活动预览')
    return sessions[0]['target']

def stop_scene_preview(scene=None):
    return stop_preview(preview_target(scene))

def _target_uid(target):
    value=target.get(TARGET_UID)
    duplicate=value and any(o!=target and o.get(TARGET_UID)==value for o in bpy.data.objects)
    if not isinstance(value,str) or not value or duplicate:
        value=uuid.uuid4().hex;target[TARGET_UID]=value
    return value

def binding(rig):
    ad=rig.animation_data
    source_api.require(not ad or not ad.use_tweak_mode,'请退出目标NLA Tweak模式')
    return {'exists':ad is not None,'action':ad.action if ad else None,'slot':ad.action_slot if ad else None,
            'handle':ad.action_slot_handle if ad else 0,'last':ad.last_slot_identifier if ad else '',
            'fields':{k:getattr(ad,k) for k in _AD_FIELDS} if ad else {}}

def restore_binding(rig,b):
    if not b['exists']:
        if rig.animation_data:
            source_api.require(not rig.animation_data.drivers and not rig.animation_data.nla_tracks,'恢复期间出现外来动画数据，保留并停止')
            rig.animation_data_clear()
    else:
        ad=rig.animation_data_create(); ad.action=b['action']
        if b['slot'] is not None: ad.action_slot=b['slot']
        ad.action_slot_handle=b['handle'];ad.last_slot_identifier=b['last']
        for k,v in b['fields'].items(): setattr(ad,k,v)

def snapshot(target,*,target_policy=None):
    from . import retarget_dynamic
    if target_policy is not None:
        from .retarget_target_policy import implementation
        policy_impl=implementation(target_policy)
        policy_impl.validate(target,target_policy)
    else:policy_impl=None
    s=bpy.context.scene
    return {'scene':s,'view_layer':bpy.context.view_layer,'binding':binding(target),'pose':{p.name:pose._channels(p) for p in target.pose.bones},
            'modes':{k:target.pose.bones['骨架设置'][k] for k in c94.MODES} if target_policy is None else {},
            'target_policy':target_policy,
            'target_properties':policy_impl.snapshot_properties(target,target_policy)
                if policy_impl and hasattr(policy_impl,'snapshot_properties') else {},
            'dynamic':retarget_dynamic.capture(target),
            'frame':s.frame_current,'subframe':s.frame_subframe,'range':(s.frame_start,s.frame_end)}

def restore(target,state,*,set_scene_time=True):
    restore_binding(target,state['binding'])
    scene=state['scene']
    with bpy.context.temp_override(scene=scene,view_layer=state['view_layer']):
        scene.frame_start,scene.frame_end=state['range']
        if set_scene_time:
            scene.frame_set(state['frame'],subframe=state['subframe'])
        else:
            # Complete pending Action/Slot evaluation before restoring raw
            # inputs when the caller already restored the shared Scene time.
            bpy.context.view_layer.update()
        for name,values in state['pose'].items():
            pb=target.pose.bones[name]
            for field in pose._CHANNELS:
                if tuple(getattr(pb,field))!=values[field]: setattr(pb,field,values[field])
            pb.rotation_mode=values['rotation_mode']
        for k,v in state['modes'].items(): target.pose.bones['骨架设置'][k]=v
        if state.get('target_properties'):
            from .retarget_target_policy import implementation
            implementation(state['target_policy']).restore_properties(target,state['target_properties'])
        if 'dynamic' in state:
            from . import retarget_dynamic
            retarget_dynamic.restore(target,state['dynamic'])
        if set_scene_time:pose._update(target)

def _channels(target,names,*,target_policy=None):
    if target_policy is not None:
        from .retarget_target_policy import implementation
        return implementation(target_policy).channels(target,target_policy)
    values={}
    for name in names:
        pb=target.pose.bones[name]
        rotation='rotation_quaternion' if pb.rotation_mode=='QUATERNION' else 'rotation_euler'
        for field in ('location',rotation,'scale'):
            for i,v in enumerate(getattr(pb,field)):
                source_api.require(math.isfinite(v),'转换产生非有限控件值')
                values[(pb.path_from_id(field),i)]=float(v)
    settings=target.pose.bones['骨架设置']
    for key in c94.MODES: values[(settings.path_from_id()+'['+json.dumps(key,ensure_ascii=False)+']',0)]=0.0
    return values

def convert_resolved(source,target,*,mapped,clip,start=None,end=None,output_start=1,
                     motion='PRESERVE',scale_motion=True,include_fingers=True,sample_step=1,
                     action_name=None,progress=None,cancelled=None,adapter_factory=None,
                     calibration_record=None,calibration_key=None,target_policy=None):
    """Shared conversion core for a resolved mapping and source clip.

    Mixamo ``convert`` resolves its mapping/clip and calls this function.  A
    calibrated source supplies its exact mapping, clip and adapter factory so
    that preview tokens, target-state snapshots and cancellation handling stay
    in one place.  ``adapter_factory(source,target,mapped)`` returns
    ``(adapter, adapter_record_or_None)``; it is called after source NLA is
    isolated and the target Action is detached.
    """
    source_api.armature(source);source_api.armature(target)
    source_api.require(source!=target,'源和目标不能是同一骨架')
    source_api.require(not target.library and not target.data.library,'请先建立本地目标工作副本')
    source_api.require(source.data.pose_position==target.data.pose_position=='POSE','源与目标骨架须使用姿态求值模式')
    source_api.require(bpy.context.scene.render.frame_map_old==bpy.context.scene.render.frame_map_new,
                       '当前转换不支持场景时间重映射，请在独立工作场景中使用原时基')
    source_api.require(type(sample_step) is int and 1<=sample_step<=60,'采样步长无效')
    source_api.require(type(output_start) is int,'输出起始帧必须为整数')
    source_api.require(type(include_fingers) is bool,'手指转换开关必须为布尔值')
    start=clip['range'][0] if start is None else float(start)
    end=clip['range'][1] if end is None else float(end)
    source_api.require(clip['range'][0]<=start<end<=clip['range'][1],'片段须在源Action帧域内且时长为正')
    scene=bpy.context.scene; fps=scene.render.fps/scene.render.fps_base
    last=output_start+(end-start)*fps/clip['fps']
    frames=list(range(output_start,math.floor(last)+1,sample_step))
    if abs(frames[-1]-last)>1e-6: frames.append(last)
    source_api.require(len(frames)<=50000,'单次片段超出本入口的50000目标采样点预算')
    source_state=binding(source);target_state=snapshot(target,target_policy=target_policy)
    source_api.require(not has_preview(target),'请先停止该目标的重定向预览')
    uid_previous=target.get(TARGET_UID);uid_touched=False
    previous_action=target_state['binding']['action']
    previous_fake_user=previous_action.use_fake_user if previous_action else None
    action=None; began=time.perf_counter()
    try:
        if previous_action:previous_action.use_fake_user=True
        source.animation_data.use_nla=False
        source.animation_data.action_blend_type='REPLACE';source.animation_data.action_influence=1.0
        source.animation_data.action_extrapolation='HOLD'
        target.animation_data_create().action=None;target.animation_data.use_nla=False
        from . import retarget_dynamic
        retarget_dynamic.suspend(target,target_state['dynamic'])
        # Object animation is sampled at an explicit source start, rather than
        # making calibration depend on the unrelated current production frame.
        scene.frame_set(math.floor(start),subframe=start-math.floor(start))
        from . import control_layer
        with control_layer.native_retarget_inputs(target) as (projection,owned_drivers):
            if adapter_factory is None:
                if target_policy is None:
                    adapter=c94.Adapter(source,target,mapped['bones'],motion=motion,scale_motion=scale_motion,include_fingers=include_fingers)
                else:
                    from .retarget_target_policy import implementation
                    adapter=implementation(target_policy).Adapter(source,target,mapped,target_policy,motion=motion)
                adapter_record=None
            else:
                prepared=adapter_factory(source,target,mapped)
                source_api.require(isinstance(prepared,(tuple,list)) and len(prepared)==2,
                                   '适配器工厂必须返回(adapter, record)')
                adapter,adapter_record=prepared
            native_paths=_channels(target,adapter.names,target_policy=target_policy)
            destination={(path,index):(projection.get(path,path),index) for path,index in native_paths}
            source_api.require(len(set(destination.values()))==len(destination),'统一控件动作通道在目标上重叠')
            all_driver_keys={(fc.data_path,fc.array_index) for fc in target.animation_data.drivers}
            native_conflicts=set(native_paths)&(all_driver_keys-owned_drivers)
            projected_conflicts=set(destination.values())&all_driver_keys
            source_api.require(not (native_conflicts or projected_conflicts),'目标控制写域由非桥接driver占用，需要适配')
            samples=[]
            for i,frame in enumerate(frames):
                if cancelled and cancelled(): raise InterruptedError('已取消，原动作和输入恢复')
                sf=start+(frame-output_start)*clip['fps']/fps
                scene.frame_set(math.floor(sf),subframe=sf-math.floor(sf))
                er=source.evaluated_get(bpy.context.evaluated_depsgraph_get())
                samples.append({semantic:er.matrix_world@er.pose.bones[name].matrix for semantic,name in mapped['bones'].items()})
                if progress: progress('SOURCE',i+1,len(frames))
            curves={key:[] for key in destination.values()};diagnostics=[]
            for i,(frame,sample) in enumerate(zip(frames,samples)):
                if cancelled and cancelled(): raise InterruptedError('已取消，原动作和输入恢复')
                scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame))
                errors=adapter.solve(sample)
                for row in errors:
                    row['frame']=frame
                diagnostics.extend(errors)
                current=_channels(target,adapter.names,target_policy=target_policy)
                source_api.require(current.keys()==native_paths.keys(),'逐帧转换过程中原生动作通道集合改变')
                for key,value in current.items(): curves[destination[key]].append((frame,value))
                if progress: progress('TARGET',i+1,len(frames))
        if cancelled and cancelled():raise InterruptedError('已取消，原动作和输入恢复')
        action=bpy.data.actions.new(action_name or target.name+'_重定向_'+clip['action'].name)
        slot=action.slots.new(id_type='OBJECT',name=target.name)
        strip=action.layers.new('重定向身体控件').strips.new(type='KEYFRAME')
        bag=strip.channelbag(slot,ensure=True)
        for (path,index),values in curves.items():
            if cancelled and cancelled():raise InterruptedError('已取消，原动作和输入恢复')
            fc=bag.fcurves.new(path,index=index);fc.keyframe_points.add(len(values))
            fc.keyframe_points.foreach_set('co',[v for point in values for v in point])
            mode='CONSTANT' if path.endswith('FK-IK"]') else 'LINEAR'
            for key in fc.keyframe_points: key.interpolation=mode
            fc.update()
        target_uid=_target_uid(target);uid_touched=target_uid!=uid_previous
        report={'schema':'c94.retarget.action.v1','id':uuid.uuid4().hex,'adapter':'c94_observed_chest_and_native_z_capacity_fingers_v4_development',
                'status':'needs_review','source':source.name,'source_action':clip['action'].name,
                'source_slot':clip['slot'].identifier,'source_fps':clip['fps'],'source_range':[start,end],
                'target':target.name,'target_uid':target_uid,'target_fps':fps,'output_range':[output_start,last],'samples':len(frames),
                'sample_step':sample_step,'sparse_preview':sample_step!=1,'motion':motion,'motion_scale':adapter.scale,
                'source_mapping':mapped,'controls':adapter.names,'seconds':time.perf_counter()-began,
                'observed_bones':adapter.observed,'finger_chains':adapter.finger_chains,'include_fingers':include_fingers,
                'palm_calibration_sides':adapter.palm_calibration_sides,
                'finger_orientation_reference':'source_rest_child_joint_directions_with_palm_roll',
                'dependent_dynamics':retarget_dynamic.report(target_state['dynamic']),
                'finger_input_allocation':'native_master_z_and_scale_when_local_z_exceeds_original_capacity; native_limits_unchanged',
                'source_object_reference_frame':start,
                'skipped_fingers':mapped['fingers']['skipped'] if include_fingers else {'all':'disabled'},
                'spine_distribution':'waist plus native upper-chest master distribution; Spine1 not independently reproduced',
                'rotation_residual_max_degrees':max(r['rotation_degrees'] for r in diagnostics),
                'bone_direction_residual':bone_direction_report(diagnostics),
                'rotation_residual_by_semantic':{s:max(r['rotation_degrees'] for r in diagnostics if r['semantic']==s)
                                                 for s in {r['semantic'] for r in diagnostics}},
                'worst_residuals':sorted(diagnostics,key=lambda r:r['rotation_degrees'],reverse=True)[:24],
                'contact_detection':{'status':'not_run','reason':'needs_calibration',
                    'message':'足底未检测：先明确source/target真实Mesh、sole片区、地面与帧秒对齐，再从只读检测入口运行',
                    'hand_contact':'not_analyzed','body_contact':'not_analyzed','automatic_correction':False},
                'preservation':{'source_action_written':False,'target_previous_action_written':False,'constraints_removed':False,
                                'rest_changed':False,'automatic_contact_correction':False,'nla_published':False},
                'limitations':['身体与完整四节点手指链开发候选，需实际视觉复核','肩部保持目标原生自动跟随','原生约束可产生姿态残差',
                               '手指保留原生自由度与限制，不承诺每节全轴精确还原','Spine1未独立重现，表情/视线/镜头未转换',
                               '未执行足底接触检测或修正','辅助动态需依新动作重捕获']}
        if target_policy is not None:report.update(adapter.report_fields())
        if projection:
            report['control_layer_channels']={path:output[0] for (path,index),output in destination.items()
                                              if path!=output[0]}
        action[META]=json.dumps(report,ensure_ascii=False);action.use_fake_user=True
        if calibration_record is not None:
            source_api.require(calibration_key,'校准记录必须给出存储键')
            action[calibration_key]=json.dumps(calibration_record,ensure_ascii=False)
        return {'action':action,'slot':slot,'report':report,'adapter_record':adapter_record,
                'calibration_record':calibration_record}
    except BaseException:
        if action is not None: bpy.data.actions.remove(action)
        if uid_touched:
            if uid_previous is None:del target[TARGET_UID]
            else:target[TARGET_UID]=uid_previous
        raise
    finally:
        try:restore_binding(source,source_state)
        finally:
            try:restore(target,target_state)
            finally:
                if previous_action:previous_action.use_fake_user=previous_fake_user

def convert(source,target,*,mapping=None,source_fps=None,start=None,end=None,output_start=1,
            motion='PRESERVE',scale_motion=True,include_fingers=True,sample_step=1,
            action_name=None,progress=None,cancelled=None):
    """Mixamo/default entry: resolve names and clip, then use the shared core."""
    source_api.armature(source);source_api.armature(target)
    mapped=source_api.mapping(source,mapping)
    clip=source_api.active_clip(source,source_fps)
    return convert_resolved(source,target,mapped=mapped,clip=clip,start=start,end=end,
                            output_start=output_start,motion=motion,scale_motion=scale_motion,
                            include_fingers=include_fingers,sample_step=sample_step,
                            action_name=action_name,progress=progress,cancelled=cancelled)

def begin_preview(target,action,*,slot=None):
    _purge_dead_previews()
    source_api.require(not has_preview(target),'该角色已有预览；请先停止')
    source_api.require(not any(s['state']['scene']==bpy.context.scene for s in _preview.values()),'当前开发入口每场景同时预览一个角色；请先停止现有预览')
    source_api.require(META in action,'只接受本入口保存的重定向候选Action')
    report=json.loads(action[META])
    source_api.require(report.get('target_uid') and report['target_uid']==target.get(TARGET_UID),'候选目标身份不同或开发版本过旧；请重新转换')
    source_api.require(sum(o.get(TARGET_UID)==report['target_uid'] for o in bpy.data.objects)==1,'角色标识被其他场景或角色副本共享；请重新转换该目标')
    fps=bpy.context.scene.render.fps/bpy.context.scene.render.fps_base
    source_api.require(abs(fps-report['target_fps'])<1e-6,'场景帧率已改变；请按当前帧率重新转换')
    slots=[s for s in action.slots if s.target_id_type=='OBJECT']
    if slot is None:
        source_api.require(len(slots)==1,'请明确选择候选Slot');slot=slots[0]
    source_api.require(slot in slots,'候选OBJECT Slot不存在')
    from . import control_layer
    if control_layer.CONTROL_KEY in target:
        owned=control_layer.verified_retarget_driver_keys(target)
        paths={(fc.data_path,fc.array_index) for layer in action.layers for strip in layer.strips
               if strip.type=='KEYFRAME' and (bag:=strip.channelbag(slot)) is not None
               for fc in bag.fcurves}
        source_api.require(not (owned&paths),'候选还在写入旧驱动通道，请按当前控件重新转换')
    if 'background_job' in report:
        from . import retarget_job
        retarget_job.validate_candidate_target(target,action)
    if 'c94_retarget_mmd_face_v1' in action:
        from . import retarget_mmd_face
        retarget_mmd_face.validate_preview(target,action,slot)
    if 'c94_retarget_mmd_gaze_v1' in action:
        from . import retarget_mmd_gaze
        retarget_mmd_gaze.validate_preview(target,action,slot)
    state=snapshot(target,target_policy=report.get('target_policy'))
    old=state['binding']['action']
    state['old_fake_user']=old.use_fake_user if old else None
    if old:old.use_fake_user=True
    try:
        ad=target.animation_data_create();ad.action=action;ad.action_slot=slot
        ad.action_blend_type='REPLACE';ad.action_influence=1.0;ad.action_extrapolation='HOLD';ad.use_nla=False
        fields=dict(binding(target)['fields'])
        from . import retarget_dynamic
        retarget_dynamic.suspend(target,state['dynamic'])
        _preview[target.session_uid]={'target':target,'action':action,'slot':slot,'state':state,
                                      'fields':fields}
        bpy.context.scene.frame_start=math.floor(report['output_range'][0])
        bpy.context.scene.frame_end=math.ceil(report['output_range'][1])
        bpy.context.scene.frame_set(math.floor(report['output_range'][0]))
        return {'ok':True,'status':'preview','range':report['output_range'],'temporary_binding':True,'production_published':False,
                'dependent_dynamics':retarget_dynamic.report(state['dynamic'])}
    except Exception:
        _preview.pop(target.session_uid,None);restore(target,state)
        if old:old.use_fake_user=state['old_fake_user']
        raise

def stop_preview(target, *, notify=True):
    _purge_dead_previews()
    try:
        key=target.session_uid
    except ReferenceError:
        source_api.require(False,'目标已删除，不存在可恢复预览')
    session=_preview.get(key)
    source_api.require(session is not None and session['target']==target,'没有该角色的当前预览')
    ad=target.animation_data
    source_api.require(ad and ad.action==session['action'] and ad.action_slot==session['slot'],
                       '预览绑定已被外部修改，不能用旧状态覆盖；请保留当前工作后处理')
    source_api.require(isinstance(session.get('fields'),dict),
                       '旧预览缺少绑定字段记录，不能安全停止；请保留当前工作')
    current_fields={k:getattr(ad,k) for k in _AD_FIELDS}
    source_api.require(current_fields==session['fields'],
                       '预览混合/影响/NLA字段已被外部修改，不能用旧状态覆盖；请保留当前工作后处理')
    from . import retarget_dynamic
    retarget_dynamic.restore(target,session['state']['dynamic'],guard=True)
    restore(target,session['state'])
    old=session['state']['binding']['action']
    if old:old.use_fake_user=session['state']['old_fake_user']
    del _preview[key]
    observer_errors=[]
    if notify:
        observer_errors=_notify_preview_stopped(target, session)
    return {'ok':True,'status':'restored','candidate_retained':True,
            'observer_errors':observer_errors}

def retain_conflict(session,reason):
    """Keep user changes and old Action; export recovery data, never force it."""
    target=session['target'];state=session['state'];b=state['binding'];old=b['action']
    if old:old.use_fake_user=True
    record={'schema':'c94.retarget.recovery.v1','target':target.name,'target_uid':target.get(TARGET_UID),
            'reason':str(reason),'previous_action':old.name if old else None,
            'previous_slot':b['slot'].identifier if b['slot'] else None,'fields':b['fields'],
            'frame':state['frame'],'subframe':state['subframe'],'range':state['range'],
            'pose':state['pose'],'modes':state['modes'],'dependent_dynamics':state.get('dynamic'),'automatic_restore':False}
    text=bpy.data.texts.new('C94_重定向恢复_'+uuid.uuid4().hex[:8]);text.use_fake_user=True
    text.write(json.dumps(record,ensure_ascii=False))
    _preview.pop(target.session_uid,None)
    return text.name

def finish_previews():
    _purge_dead_previews()
    failures=[]
    for key,session in list(_preview.items()):
        try:
            report=stop_preview(session['target'])
            if report.get('observer_errors'):
                failures.append({'reason':'observer_errors','recovery_text':None,
                                 'observer_errors':report['observer_errors']})
        except Exception as exc:
            try:recovery=retain_conflict(session,exc)
            except (ReferenceError,KeyError):
                _preview.pop(key,None);recovery=None
            failures.append({'reason':str(exc),'recovery_text':recovery})
    return failures
