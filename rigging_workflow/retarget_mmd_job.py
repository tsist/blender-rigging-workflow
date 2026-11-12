# SPDX-License-Identifier: GPL-3.0-or-later
"""MMD data adapters for the existing retarget_job queue/worker/collect.

No scheduler, solver, profile-validation kernel or second state owner lives
here. Source data and clip scopes belong to mmd_source_backend. Face/gaze
candidates use their existing public cores and the same registered clip.
"""
import json
import math
import bpy
from . import mmd_source_backend as backend
from . import retarget_action as rt, retarget_mmd_face as face
from . import retarget_mmd_gaze as gaze, face_mouth_core as mouth

TRANSPORT = 'c94_retarget_mmd_hub_resolve_v1'
SCHEMA = 'c94.retarget.mmd.hub-resolve.v1'
FIELDS = {'clip_id','fps','source_fps','start','end','output_start','action_name','output_name','label','face','gaze'}

def require(value,message):
    if not value: raise ValueError(message)

def live_source_state(source,profile):
    """Report profile-wide live drift through the sole source-validation core."""
    try:
        backend.validate_persisted_source(profile,scene=bpy.context.scene,
            require_scene_config=False,expected_rig=source,require_all_actions=True)
    except Exception as exc:
        return {'matches_snapshot':False,'error':str(exc),'edits_included':False}
    return {'matches_snapshot':True,'edits_included':False}

def prepare_profile(source,raw,mapping=None):
    require(raw is not None,'MMD必须提供已登记纯JSON来源profile，不猜活动Action来源')
    profile=backend.load_profile_document(raw)
    resolved=backend.validate_persisted_source(profile,scene=bpy.context.scene,
        require_scene_config=False,expected_rig=source,require_all_actions=True)
    require(all(obj.name in bpy.context.scene.objects for obj in
                [resolved['rig'],resolved['root'],*resolved['meshes']]),'已登记源所有者必须位于当前Scene')
    frozen,_=backend.mapping_from_profile(source,profile,validate=False)
    require(mapping is None or mapping.get('bones',mapping)==frozen['bones'],'MMD映射必须与已登记profile相同')
    ad=source.animation_data
    require(ad and ad.action and ad.action_slot,'MMD源当前绑定没有明确Action/Slot')
    active=[clip for clip in profile['clips'] if clip['armature_action']['name']==ad.action.name
            and clip['armature_action']['slot']==ad.action_slot.identifier]
    require(len(active)==1,'当前活动Action/Slot未登记为该来源clip，不能猜测来源')
    backend.clip_timebase(profile,active[0]['id'],source_fps=30.)
    return profile

def resolve_hub(target,group_id,record_sha=None):
    matches=[]
    for hub in bpy.context.scene.objects:
        if hub.get('rig') != target or mouth.STATE_KEY not in hub:continue
        group,record,rig,_=mouth._read(hub)
        if group['group_id']==group_id:
            require(rig==target,'MOUTH实际rig ID归属不同')
            if record_sha:require(backend.canonical_hash(record)==record_sha,'MOUTH实际控制记录已变化')
            matches.append(hub)
    require(len(matches)==1,'需要当前目标唯一已有MOUTH Hub实际ID，不按名称或相似名称认领')
    return matches[0]

def _face_options(target,raw):
    if raw is None or raw is False:return None
    require(isinstance(raw,dict) and not (set(raw)-{'group_id','mapping','omitted_reasons'}),'face只接受显式group_id/mapping/omitted_reasons')
    group_id=raw.get('group_id');require(isinstance(group_id,str) and group_id,'face缺少明确MOUTH group_id')
    hub=resolve_hub(target,group_id);group,record,_,_=mouth._read(hub)
    require(isinstance(raw.get('mapping'),list),'face必须给出显式映射列表')
    omitted=raw.get('omitted_reasons',{})
    require(isinstance(omitted,dict) and all(isinstance(k,str) and isinstance(v,str) for k,v in omitted.items()),'表情遗漏原因必须为纯JSON字典')
    return {'group_id':group_id,'mouth_record_sha256':backend.canonical_hash(record),
            'mapping':json.loads(json.dumps(raw['mapping'])),'omitted_reasons':omitted}

def resolve_items(source,target,raw_items,profile,job_id):
    from . import retarget_job as api
    if isinstance(raw_items,dict):raw_items=[raw_items]
    require(isinstance(raw_items,(list,tuple)) and 1<=len(raw_items)<=64,'MMD items须为1..64个显式clip/帧域/30fps条目')
    items=[]
    # source_clip_context validates this registered Action set; explicitly
    # include those exact IDs in the existing Scene snapshot dependency set.
    actions=[bpy.data.actions[row['name']] for row in profile['actions']]
    for index,raw in enumerate(raw_items):
        item={'index':index,'id':f'item-{index:03d}','state':'not_started','action_name':None,'action_signature':None,
            'slot_identifier':None,'slot_handle':None,'fps':None,'clip_range':None,'start':None,'end':None,
            'output_start':1,'output_action_name':None,'label':None,'validation_error':None,'mmd':None}
        try:
            require(isinstance(raw,dict) and not (set(raw)-FIELDS),'MMD item含未知字段或不是纯JSON对象')
            clip_id=raw.get('clip_id');require(isinstance(clip_id,str) and clip_id,'每个MMD item必须明确clip_id')
            fps=raw.get('fps',raw.get('source_fps'))
            require(type(fps) in (int,float) and math.isfinite(fps) and float(fps)==30.,'每个MMD item必须显式30fps')
            context=backend.clip_timebase(profile,clip_id,source_fps=fps);clip=context['clip']
            require(clip.get('body_convertible') is True,'该clip未登记为身体转换来源，不拿其他动作补身体')
            start,end=raw.get('start'),raw.get('end')
            require(all(type(v) in (int,float) and math.isfinite(v) for v in (start,end))
                    and clip['range'][0]<=start<end<=clip['range'][1],'每item须明确登记clip范围内的正帧域')
            output_start=raw.get('output_start',1);require(type(output_start) is int,'output_start必须为整数')
            output_name=raw.get('action_name',raw.get('output_name')) or f'{target.name}_MMD后台_{job_id[:8]}_{index:03d}'
            require(isinstance(output_name,str) and output_name.strip(),'输出Action名称无效')
            face_opts=_face_options(target,raw.get('face'))
            gaze_opts=raw.get('gaze',False)
            require(isinstance(gaze_opts,bool),'gaze必须为明确布尔值；源语义由既有gaze核核验')
            item.update(action_name=context['action'].name,action_signature=api.calibration.action_signature(context['action']),
                slot_identifier=context['slot'].identifier,slot_handle=int(context['slot'].handle),fps=30.,
                clip_range=list(clip['range']),start=float(start),end=float(end),output_start=output_start,
                output_action_name=output_name,label=raw.get('label') or f'{clip_id} {start:g}–{end:g} @30fps',
                mmd={'clip_id':clip_id,'profile_sha256':profile['profile_sha256'],'face':face_opts,'gaze':gaze_opts})
        except Exception as exc:
            item['validation_error']={'code':'INVALID_MMD_ITEM','message':str(exc)}
        items.append(item)
    return items,actions

def convert_item(source,target,profile,item,settings,*,progress,cancelled,protected):
    created=[];selection=None;target_state=rt.snapshot(target)
    def retain(result):
        action=result['action'];require(action.as_pointer() not in protected,'转换返回受保护Action')
        created.append(action);return result
    try:
        selection=backend.source_clip_context(profile,item['mmd']['clip_id'],source_fps=30.,require_scene_config=False)
        with selection as selected:
            require(selected.action.name==item['action_name'] and selected.slot.identifier==item['slot_identifier'],
                    'clip选择与快照item Action/Slot不一致')
            converted=retain(backend.convert_from_profile(source,target,profile,
                clip_id=item['mmd']['clip_id'],start=item['start'],end=item['end'],output_start=item['output_start'],
                motion=settings['motion'],action_name=item['output_action_name'],require_scene_config=False,
                progress=progress,cancelled=cancelled,sample_step=1)['result'])
            face_opts=item['mmd']['face'];morph_metadata=None
            if face_opts:
                if cancelled():raise InterruptedError('已取消，未合成表情')
                from . import retarget_mmd_morph as morph
                tracks,morph_metadata=morph.morph_tracks_from_profile(source,profile,
                    clip_id=item['mmd']['clip_id'],source_fps=30.,require_scene_config=False)
                bounds={}
                for name,points in tracks.items():
                    frames={item['start'],item['end'],*[f for f,v in points if item['start']<=f<=item['end']]}
                    values=[face._value(points,f) for f in frames]
                    bounds[name]=[min(values),max(values)]
                morph_metadata={**morph_metadata,'selected_range':[item['start'],item['end']],
                    'static_on_selected_range':all(b-a<=1e-6 for a,b in bounds.values()),'weight_bounds':bounds}
                require(morph_metadata['clip_id']==selected.clip['id'],'表情与身体必须使用同一登记clip')
                hub=resolve_hub(target,face_opts['group_id'],face_opts['mouth_record_sha256'])
                progress('MMD_FACE',0,1)
                converted=retain(face.create_candidate(hub,tracks,mapping=face_opts['mapping'],source_metadata=morph_metadata,
                    omitted_reasons=face_opts['omitted_reasons'],source_fps=30.,source_range=[item['start'],item['end']],
                    output_start=item['output_start'],base_action=converted['action'],base_slot=converted['slot'],
                    action_name=item['output_action_name']))
                progress('MMD_FACE',1,1)
            if item['mmd']['gaze']:
                if cancelled():raise InterruptedError('已取消，未合成视线')
                converted=retain(gaze.create_candidate(source,target,base_action=converted['action'],source_fps=30.,
                    start=item['start'],end=item['end'],output_start=item['output_start'],source_kind='real_clip',
                    action_name=item['output_action_name'],progress=progress,cancelled=cancelled))
            converted['_owned_mmd_actions']=created
            converted['_mmd_clip']={'clip_id':selected.clip['id'],'fps':30.,'source_range':[item['start'],item['end']],
                'profile_sha256':profile['profile_sha256'],'morph_source':morph_metadata,
                'same_clip_for_body_face_gaze':True}
            return converted
    except BaseException:
        for action in reversed(created):
            if action.as_pointer() not in protected:bpy.data.actions.remove(action)
        raise
    finally:
        # The source context restores Scene time, then source raw. Target raw
        # belongs to the existing shared restore core and is restored last.
        rt.restore(target,target_state,set_scene_time=False)

def detach_worker_hub(action,target):
    if face.META not in action:return None
    hub=action.get(face.HUB_REF);require(isinstance(hub,bpy.types.Object),'表情候选缺少真实Hub ID')
    group,record,owner,_=mouth._read(hub);require(owner==target,'表情候选Hub实际rig不同')
    data={'schema':SCHEMA,'group_id':group['group_id'],'target_uid':target.get(rt.TARGET_UID),
        'mouth_record_sha256':backend.canonical_hash(record),'status':'resolve_existing_unique_mouth_hub'}
    del action[face.HUB_REF]
    action[TRANSPORT]=json.dumps(data,ensure_ascii=False)
    return data

def resolve_transport_hub(target,data):
    require(isinstance(data,dict) and data.get('schema')==SCHEMA,'MMD Hub运输记录无效')
    require(data['target_uid']==target.get(rt.TARGET_UID),'MMD运输候选目标UID不同')
    return resolve_hub(target,data['group_id'],data['mouth_record_sha256'])

def restore_collected_hub(target,action,data):
    if not data:
        require(face.META not in action,'表情候选缺少可核验Hub运输记录')
        return None
    hub=resolve_transport_hub(target,data)
    raw=action.get(TRANSPORT);require(isinstance(raw,str) and json.loads(raw)==data,'Action运输记录与ledger不符')
    require(face.HUB_REF not in action,'运输Action不应携带外来Hub ID依赖')
    action[face.HUB_REF]=hub
    slots=[s for s in action.slots if s.target_id_type=='OBJECT'];require(len(slots)==1,'候选需唯一OBJECT Slot')
    face.validate_preview(target,action,slots[0])
    return {'group_id':data['group_id'],'hub':hub.name,'actual_id_resolved':True}

def action_only_inventory(library):
    counts={name:len(getattr(library,name,())) for name in ('objects','meshes','armatures','scenes','collections','shape_keys')}
    require(not any(counts.values()),'MMD输出违反Action-only合同，拒绝导入依赖对象：'+json.dumps(counts))
    return counts
