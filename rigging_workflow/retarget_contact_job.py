# SPDX-License-Identifier: GPL-3.0-or-later
"""Contact-correction item adapters for the sole existing job lifecycle.

The frozen correction module owns input semantics, solve, review and approval.
This module transports pure JSON input and supplies the accepted native graph
fingerprint; collect never reopens review or manufactures user authority.
"""
import json
import math
import bpy
from . import retarget_action as rt,retarget_contact_correction as core
from . import retarget_contact_anchors as anchors

KIND='CONTACT_CORRECTION'
PARAMETERS={'clearance_m','max_delta_m','match_tolerance_m','solve_tolerance_m'}
FIELDS={'kind','detection_report','issue_id','frames','calibration','parameters','action_name','label','support_window'}

def graph_fingerprint(target):
    from . import retarget_job as jobs
    return {'scope':'accepted_native_static_with_core_mesh_action_object_mode_guards_v1',
            'static_sha256':jobs._rig_static_fingerprint(target)['static_sha256']}

def resolve_items(target,raw_items,job_id):
    from . import retarget_job as jobs
    if isinstance(raw_items,dict):raw_items=[raw_items]
    core.require(isinstance(raw_items,(list,tuple)) and 1<=len(raw_items)<=64,'需明确1..64个足底问题条目')
    binding=rt.binding(target);base,slot=binding['action'],binding['slot']
    core.require(base is not None and slot is not None,'先明确绑定检测报告原基础Action/Slot')
    core.require(core.META not in base,'不在修正候选上叠加另一修正；先绑定原基础动作')
    items=[]
    for index,raw in enumerate(raw_items):
        core.require(isinstance(raw,dict) and not(set(raw)-FIELDS),'足底条目包含未知字段')
        core.require(raw.get('kind')==KIND,'每个足底条目必须明确kind=CONTACT_CORRECTION')
        doc=json.loads(json.dumps(raw,allow_nan=False))
        for key in ('detection_report','calibration','parameters'):
            core.require(isinstance(doc.get(key,{}),dict),key+'必须为纯JSON对象')
        calibration=doc.get('calibration',{})
        measurement_mode=anchors.mode(calibration)
        if measurement_mode==anchors.MODE:
            core.require(calibration.get('coverage_status')==anchors.COVERAGE and
                calibration.get('rig')==target.name,'骨锚点修正需要明确同目标三点校准')
            core.require(anchors.identity(bpy.context.scene,target,calibration.get('anchors'))==calibration.get('anchor_identity'),
                         '骨锚点/rest身份不同，请重新检测')
            report=doc.get('detection_report',{})
            core.require(report.get('measurement_mode')==anchors.MODE and
                report.get('calibration_sha256')==calibration.get('detection_calibration_sha256'),
                '骨锚点模式或检测校准摘要不同')
        else:
            core.require(calibration.get('coverage_status')=='explicit_calibrated_complete',
                         'coverage_incomplete：必须显式完整sole校准，不自动推断生产足底')
            core.require(doc.get('detection_report',{}).get('measurement_mode','surface')=='surface',
                         '骨锚点报告不能进入表面修正模式')
            core._validate_surface_detection_identity(
                target, base, slot, calibration, doc.get('detection_report', {}))
        core.require(isinstance(doc.get('issue_id'),str) and doc['issue_id'],'必须明确报告中的精确issue_id')
        frames=doc.get('frames');core.require(isinstance(frames,list) and len(frames)>=2 and
            all(type(v) is int for v in frames) and frames==list(range(min(frames),max(frames)+1)),
            '必须明确连续整数frames且至少两帧')
        core.require(not(set(doc.get('parameters',{}))-PARAMETERS),'包含未知修正参数')
        core.require(all(type(v) in (int,float) and math.isfinite(v) for v in doc.get('parameters',{}).values()),'修正参数需有限数值')
        window = doc.get('support_window')
        if window is not None:
            rows = [row for row in doc['detection_report'].get('issues', ()) if row['id'] == doc['issue_id']]
            core.require(len(rows) == 1, '联合支撑修正需要精确的问题')
            checked = core.gait_support_window(doc['detection_report'], rows[0], calibration, frames,
                                               tangent_release=window.get('tangent_release') if isinstance(window, dict) else None)
            core.require(window == checked, '联合支撑窗口与已检测着地及旁路设置不同')
        reference=doc['detection_report'].get('target',{}).get('reference',{})
        if measurement_mode==anchors.MODE:
            object_inputs=calibration.get('detection_object_inputs')
            core.require(isinstance(object_inputs,dict) and reference.get('object_inputs')==object_inputs and
                         core._object_input_stamp(target,base,slot)==object_inputs,
                         '对象输入/父空间与原骨锚点检测不同或旧报告缺身份，请重新检测')
        core.require(reference.get('target_uid')==target.get(rt.TARGET_UID) and
            reference.get('action_fingerprint')==core.action_fingerprint(base),'检测报告不属于当前明确基础动作/目标')
        prefix='骨锚点候选' if measurement_mode==anchors.MODE else '足底候选'
        name=doc.pop('action_name',None) or f'C94_{prefix}_{job_id[:8]}_{index:03d}'
        core.require(isinstance(name,str) and name.strip(),'候选名称无效')
        items.append({'index':index,'id':f'item-{index:03d}','kind':KIND,'contact_correction':doc,
            'state':'not_started','action_name':base.name,'action_signature':jobs.calibration.action_signature(base),
            'slot_identifier':slot.identifier,'slot_handle':int(slot.handle),
            'fps':bpy.context.scene.render.fps/bpy.context.scene.render.fps_base,
            'clip_range':[min(frames)-1,max(frames)+1],'start':min(frames),'end':max(frames),'output_start':min(frames),
            'output_action_name':name,'label':doc.get('label') or doc['issue_id'],'validation_error':None})
    return items,[base]

def convert_item(target,item,*,progress,cancelled):
    data=item['contact_correction']
    progress('CONTACT_CORRECTION',0,len(data['frames']))
    plan=None
    try:
        plan=core.prepare_issue(target,data['detection_report'],issue_id=data['issue_id'],
            frames=data['frames'],calibration=data['calibration'],graph_fingerprint=graph_fingerprint,
            cancelled=cancelled,support_window=data.get('support_window'),**data.get('parameters',{}))
        plan.action.name=item['output_action_name']
        progress('CONTACT_CORRECTION',len(data['frames']),len(data['frames']))
        return {'action':plan.action,'slot':plan.slot,'report':plan.report}
    except core.CorrectionError:
        if cancelled():raise InterruptedError('足底候选生成已取消')
        raise
    finally:
        # RAM plan and approvals cannot cross a worker process boundary.
        if plan is not None:core._plans.pop(plan.token,None)

def reopen_for_review(target,action):
    from . import retarget_job as jobs
    raw=action.get(jobs.JOB_KEY);marker=json.loads(raw) if isinstance(raw,str) else None
    core.require(isinstance(marker,dict) and marker.get('kind')==KIND,'请选择本后台入口保留的独立修正候选')
    jobs.validate_candidate_target(target,action)
    return core.reopen_for_review(target,action,graph_fingerprint=graph_fingerprint)

def action_only_inventory(library):
    counts={name:len(getattr(library,name,())) for name in ('objects','meshes','armatures','scenes','collections','shape_keys')}
    core.require(not any(counts.values()),'足底输出必须为Action-only；拒绝隐式附带对象依赖')
    return counts
