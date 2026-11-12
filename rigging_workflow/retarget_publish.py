# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit persistent binding/NLA append for reviewed base candidates.

No curve editing, repair or automatic rebake. Persistent recovery references
live on the target; appended Text receipts retain the reviewable history.
"""
import json,math,uuid
import bpy
from . import retarget_action as rt, retarget_dynamic as dynamic
from . import pose_switching as pose, retarget_source as sources

META='c94_retarget_publish_v1'
OLD='c94_retarget_publish_previous_action_v1'
NEW='c94_retarget_publish_candidate_v1'
_TRACK_FIELDS=('name','mute','is_solo','lock','select')
_STRIP_FIELDS=('name','type','mute','action_slot_handle','action_slot','frame_start','frame_end',
    'action_frame_start','action_frame_end','scale','repeat','blend_type','extrapolation',
    'influence','blend_in','blend_out','use_auto_blend','use_reverse','use_sync_length',
    'use_animated_influence','use_animated_time','strip_time','use_animated_time_cyclic')

def _require(value,message):
    if not value:raise ValueError(message)

def _plain(value):
    if value is None or isinstance(value,(bool,int,float,str)):return value
    if isinstance(value,bpy.types.ActionSlot):return value.identifier
    try:return list(value)
    except TypeError:return str(value)

def _curves(action,slot):
    values=[]
    for layer in action.layers:
        for strip in layer.strips:
            if strip.type=='KEYFRAME':
                bag=strip.channelbag(slot)
                if bag:
                    for fc in bag.fcurves:
                        values.append((fc.data_path,fc.array_index))
    return set(values)

def _nla(target):
    rows=[]
    for track in target.animation_data.nla_tracks if target.animation_data else ():
        row={key:_plain(getattr(track,key)) for key in _TRACK_FIELDS}
        row['strips']=[]
        for strip in track.strips:
            values={key:_plain(getattr(strip,key)) for key in _STRIP_FIELDS if hasattr(strip,key)}
            values['action']=strip.action.name if strip.action else None
            values['curves']=[{'path':fc.data_path,'index':fc.array_index,
                'keys':[[*kp.co,kp.interpolation] for kp in fc.keyframe_points]}
                for fc in strip.fcurves]
            values['modifiers']=[{p.identifier:_plain(getattr(mod,p.identifier)) for p in mod.bl_rna.properties
                if p.identifier!='rna_type' and p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and not p.is_array}
                for mod in strip.modifiers]
            row['strips'].append(values)
        rows.append(row)
    return rows

def _inputs(target,action,slot):
    """Guard author inputs, excluding only explicit candidate/driver scalars."""
    driven=_curves(action,slot)
    if target.animation_data:
        driven.update((fc.data_path,fc.array_index) for fc in target.animation_data.drivers)
    rows={}
    for pb in target.pose.bones:
        rows[pb.path_from_id('rotation_mode')]=pb.rotation_mode
        for field in pose._CHANNELS:
            path=pb.path_from_id(field)
            for index,value in enumerate(getattr(pb,field)):
                if (path,index) not in driven:rows[path+'#'+str(index)]=float(value)
        for key in pb.keys():
            value=pb[key]
            if isinstance(value,(bool,int,float,str)):
                path=pb.path_from_id()+'['+json.dumps(key,ensure_ascii=False)+']'
                if (path,0) not in driven:rows[path]=value
    return rows

def _encode_state(state):
    binding=state['binding']
    return {'scene':state['scene'].name,'view_layer':state['view_layer'].name,
        'binding':{'exists':binding['exists'],'slot':binding['slot'].identifier if binding['slot'] else None,
            'handle':binding['handle'],'last':binding['last'],'fields':binding['fields']},
        'pose':state['pose'],'modes':state['modes'],'dynamic':state['dynamic'],
        'frame':state['frame'],'subframe':state['subframe'],'range':state['range'],
        'old_fake_user':state['old_fake_user']}

def _decode_state(target,record):
    state=record['previous'];scene=bpy.data.scenes.get(state['scene'])
    _require(scene is not None and scene==bpy.context.scene,'请在原发布场景撤回')
    layer=scene.view_layers.get(state['view_layer'])
    _require(layer is not None,'原发布View Layer不存在，保留恢复回执')
    old=target.get(OLD);binding=dict(state['binding']);identifier=binding.pop('slot')
    slot=None
    if identifier is not None:
        _require(isinstance(old,bpy.types.Action),'原Action引用已改变，保留恢复回执')
        slot=next((s for s in old.slots if s.identifier==identifier),None)
        _require(slot is not None and slot.target_id_type=='OBJECT','原OBJECT Slot已改变，保留恢复回执')
    if record['previous_action'] is not None:
        _require(isinstance(old,bpy.types.Action),'原Action引用已丢失，保留恢复回执')
    binding.update(action=old,slot=slot)
    return {**state,'scene':scene,'view_layer':layer,'binding':binding,
        'pose':{name:{**values,**{field:tuple(values[field]) for field in pose._CHANNELS}}
            for name,values in state['pose'].items()}}

def _receipt(record,event):
    text=bpy.data.texts.new('C94_基础动作_'+event+'_'+record['id'][:8])
    text.use_fake_user=True;text.write(json.dumps({**record,'event':event},ensure_ascii=False,indent=2))
    return text.name

def inspect(target):
    """Return JSON-only persistent receipt, without implying validity."""
    value=target.get(META)
    return json.loads(value) if isinstance(value,str) else None

def _candidate(target,action,slot):
    sources.armature(target)
    _require(not target.library and not target.data.library,'请先建立本地目标工作副本')
    _require(isinstance(action,bpy.types.Action),'请选择基础候选Action')
    _require('c94_retarget_contact_correction_v1' not in action,
             '额外接触修正请走逐问题确认入口；基础发布入口不接管修正确认')
    report=json.loads(action.get(rt.META,'{}'))
    # MMD conversion preserves the shared Action report fields but records its
    # own source semantics in this existing schema. Both use begin_preview.
    _require(report.get('schema') in {'c94.retarget.action.v1','c94.retarget.mmd.v1'},
             '只接受本流程基础候选')
    _require(not report.get('sparse_preview') and report.get('sample_step')==1,
             '快速稀疏预览不可正式绑定，请先逐帧转换')
    _require(report.get('samples',0)>=2,'候选缺少有效采样记录')
    # rt.begin_preview owns the background graph/transform boundary.
    slots=[s for s in action.slots if s.target_id_type=='OBJECT']
    if slot is None:
        _require(len(slots)==1,'请明确选择候选OBJECT Slot');slot=slots[0]
    _require(slot in slots,'候选OBJECT Slot不存在')
    _require(_curves(action,slot),'候选OBJECT Slot没有曲线')
    return report,slot

def commit(target,action,*,slot=None,mode='ACTION',reviewed=False,isolate_nla=False):
    """Persist a reviewed candidate; NLA appends one owned REPLACE strip.

    ACTION keeps old tracks intact and requires explicit isolation if they
    currently evaluate. NLA refuses unmuted/solo foreign tracks altogether.
    No auto contact/curve repair or auxiliary rebake is performed.
    """
    _require(reviewed is True,'请明确复核候选与限制后再正式绑定')
    _require(mode in ('ACTION','NLA'),'发布模式只能为ACTION或NLA')
    _require(not rt.has_preview(target),'请先停止现有预览再正式绑定')
    _require(inspect(target) is None,'目标已有持久绑定，请先撤回或保留恢复回执处理')
    report,slot=_candidate(target,action,slot)
    binding=rt.binding(target);before_nla=_nla(target)
    active=[row for row in before_nla if not row['mute'] or row['is_solo']]
    if mode=='NLA':_require(not active,'原NLA有未静音或独奏轨道，需先明确隔离；本入口不会静默禁用')
    elif binding['fields'].get('use_nla') and active:
        _require(isolate_nla is True,'原NLA正在求值；请显式选择隔离，原轨道保持')
    identifier=uuid.uuid4().hex;track=None;record=None
    rt.begin_preview(target,action,slot=slot)
    session=rt._preview[target.session_uid];state=session['state'];old=state['binding']['action']
    try:
        ad=target.animation_data
        if mode=='NLA':
            ad.action=None;track=ad.nla_tracks.new();track.name='C94_基础_'+identifier
            strip=track.strips.new('C94_基础_'+identifier,math.floor(report['output_range'][0]),action)
            strip.action_slot=slot
            strip.action_frame_start,strip.action_frame_end=report['output_range']
            strip.frame_start,strip.frame_end=report['output_range']
            strip.blend_type='REPLACE';strip.extrapolation='NOTHING';strip.influence=1.0
            ad.use_nla=True
            bpy.context.scene.frame_set(math.floor(report['output_range'][0]))
        after_binding=rt.binding(target)
        record={'schema':'c94.retarget.publish.v1','id':identifier,'mode':mode,'status':'committed',
            'target_uid':target.get(rt.TARGET_UID),'candidate_id':report['id'],
            'candidate':action.name,'candidate_slot':slot.identifier,'target_fps':report['target_fps'],
            'previous_action':old.name if old else None,'previous':_encode_state(state),
            'before_nla':before_nla,'expected_nla':_nla(target),
            'owned_track':track.name if track else None,
            'expected_binding':{'action':action.name if mode=='ACTION' else None,
                'slot':after_binding['slot'].identifier if after_binding['slot'] else None,
                'fields':after_binding['fields']},
            'expected_inputs':_inputs(target,action,slot),
            'expected_scene_range':[bpy.context.scene.frame_start,bpy.context.scene.frame_end],
            'dependent_dynamics':dynamic.report(state['dynamic']),
            'reviewed_status':report.get('status'),'candidate_curves_written':False,
            'nla_isolation_explicit':isolate_nla is True,'automatic_rebake':False}
        # Persist ID references, not just names, so saving/reopening retains
        # the exact old Action and candidate without a RAM preview session.
        if old is not None:target[OLD]=old
        target[NEW]=action
        record['commit_receipt']=_receipt(record,'COMMIT')
        target[META]=json.dumps(record,ensure_ascii=False)
        _require(rt._preview.get(target.session_uid) is session,'预览句柄已变，停止持久化')
        del rt._preview[target.session_uid]
        return {'ok':True,'status':'committed','persistent':True,'receipt':record,
                'dependent_dynamics':record['dependent_dynamics']}
    except Exception:
        if track is not None and target.animation_data and track.name in target.animation_data.nla_tracks:
            target.animation_data.nla_tracks.remove(track)
        # Restore the exact binding expected by stop_preview, then ask the
        # shared core to restore inputs/dynamics/fake-user state.
        ad=target.animation_data_create();ad.action=action;ad.action_slot=slot
        for key,value in (('action_blend_type','REPLACE'),('action_influence',1.0),('action_extrapolation','HOLD'),('use_nla',False)):
            setattr(ad,key,value)
        if rt.has_preview(target):rt.stop_preview(target)
        for key in (META,OLD,NEW):
            if key in target:del target[key]
        raise

def withdraw(target):
    """Restore the previous binding only while owned state still matches."""
    record=inspect(target)
    _require(record is not None and record['status']=='committed','目标没有本入口的持久绑定')
    _require(not rt.has_preview(target),'请先停止临时预览再撤回持久绑定')
    _require(target.get(rt.TARGET_UID)==record['target_uid'],'目标身份已改变，保留恢复回执')
    _require(sum(o.get(rt.TARGET_UID)==record['target_uid'] for o in bpy.context.scene.objects)==1,
             '目标标识被副本共享，保留恢复回执')
    action=target.get(NEW)
    _require(isinstance(action,bpy.types.Action),'候选引用已丢失，保留恢复回执')
    report=json.loads(action.get(rt.META,'{}'))
    _require(report.get('id')==record['candidate_id'],'候选身份已改变，保留恢复回执')
    slot=next((s for s in action.slots if s.identifier==record['candidate_slot']),None)
    _require(slot is not None and slot.target_id_type=='OBJECT','候选OBJECT Slot已改变，保留恢复回执')
    binding=rt.binding(target);expected=record['expected_binding']
    _require((binding['action']==action if record['mode']=='ACTION' else binding['action'] is None)
        and (binding['slot'].identifier if binding['slot'] else None)==expected['slot']
        and binding['fields']==expected['fields'],'当前绑定已被外部修改，不能用旧状态覆盖')
    _require(_nla(target)==record['expected_nla'],'NLA已被外部修改，不能用旧状态覆盖')
    _require(_inputs(target,action,slot)==record['expected_inputs'],
             '未由候选驱动的作者输入已改变，不能用旧状态覆盖')
    _require([bpy.context.scene.frame_start,bpy.context.scene.frame_end]==record.get('expected_scene_range'),
             '场景帧域已被外部修改，不能用旧状态覆盖')
    # Decode all restoration references before changing any author input.
    state=_decode_state(target,record)
    track=None
    if record['mode']=='NLA':
        track=target.animation_data.nla_tracks.get(record['owned_track'])
        _require(track is not None,'本次NLA轨道已丢失，保留恢复回执')
    dynamic.restore(target,record['previous']['dynamic'],guard=True)
    if track is not None:
        target.animation_data.nla_tracks.remove(track)
    rt.restore(target,state)
    old=state['binding']['action']
    if old:old.use_fake_user=state['old_fake_user']
    output={**record,'status':'withdrawn'};receipt=_receipt(output,'WITHDRAW')
    for key in (META,OLD,NEW):
        if key in target:del target[key]
    return {'ok':True,'status':'restored','candidate_retained':True,'receipt_text':receipt,
            'previous_commit_receipt':record['commit_receipt']}
