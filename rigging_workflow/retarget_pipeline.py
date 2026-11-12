# SPDX-License-Identifier: GPL-3.0-or-later
"""Conversion completion orchestration; detection remains read-only and unapproved."""
import json
import math
import bpy
from . import retarget_action as rt, retarget_contact_detection as detection


def _failed(calibration, source, target, action, status, code, message):
    doc=detection.pure(calibration or {})
    return {'schema':detection.SCHEMA,'kind':detection.KIND,'status':status,
        'input_sha256':detection.digest({'source':source.name,'target':target.name,'action':action.name,'calibration':doc}),
        'identity':{'source':{'rig':source.name},'target':{'rig':target.name,'action':action.name},'target_uid':target.get(rt.TARGET_UID)},
        'calibration':doc,'calibration_sha256':detection.digest(doc),'mesh_identity':{},
        'read_only':True,'full_sole_pass':False,'detection_report':None,
        'reasons':[{'code':code,'message':message}], 'approval':None,
        'writes_action':False,'writes_rest':False,'writes_weights':False,
        'hand_contact':{'status':'not_analyzed'},'body_contact':{'status':'not_analyzed'},
        'limitations':['报告不授修正或发布批准','有限采样不代表连续接触或美术质量']}


def _time_contract(report, doc):
    # A conversion owns a bounded source clip and output window. Explicit
    # schedules may choose fewer samples but cannot extrapolate or change fps.
    for role,range_key,fps_key in (('source','source_range','source_fps'),('target','output_range','target_fps')):
        row=doc.get(role,{})
        if not isinstance(row,dict) or 'frames' not in row or 'times_s' not in row:continue
        frames,times=detection.sampler._validate_frames(row['frames'],row['times_s'])
        bounds=report.get(range_key);fps=report.get(fps_key)
        detection.require(isinstance(bounds,(list,tuple)) and len(bounds)==2,'转换报告缺明确'+range_key)
        detection.require(isinstance(fps,(int,float)) and math.isfinite(fps) and fps>0,'转换报告缺有效'+fps_key)
        detection.require(all(bounds[0]<=f<=bounds[1] for f in frames),role+'校准帧越过所选转换域')
        detection.require(all(abs((f-frames[0])/fps-(t-times[0]))<=1e-7 for f,t in zip(frames,times)),role+'校准帧秒时基与转换fps不同')


def complete_detection(source,target,converted,calibration=None,progress=None,cancelled=None):
    """Attach one genuine detection result to the final converted Action/report.

    Explicit invalid calibration or analysis errors retain a valid base as
    analysis_failed. Cancellation and any frozen-core restoration failure
    propagate to the caller's existing ownership/cleanup path.
    """
    progress=progress or (lambda *_:None);cancelled=cancelled or (lambda:False)
    if cancelled():raise InterruptedError('转换后检测已取消')
    # Reject non-JSON/NaN/type-invalid external calibration before touching
    # the already successful base or attempting to construct a report hash.
    calibration_doc=detection.pure({} if calibration is None else calibration)
    detection.require(isinstance(calibration_doc,dict),'校准必须为纯JSON对象')
    action=converted['action'];report=json.loads(action.get(rt.META,'{}'))
    if '骨架设置' not in target.pose.bones:
        result=_failed(calibration_doc,source,target,action,'not_supported','generic_target_state_core_not_supported','普通FK检测尚无目标状态适配器；保留成功基础动作')
    else:
        try:
            doc=calibration_doc
            raw={'kind':detection.KIND,'source_action':report.get('source_action'),'source_slot':report.get('source_slot'),
                 'target_action':action,'target_slot':converted['slot'].identifier,'calibration':doc}
            items,_=detection.resolve_items(source,target,[raw],'automatic')
            # No calibration still enters detect_item and yields its full,
            # named needs_calibration report rather than a cosmetic status.
            reasons,_=detection._calibration_reasons(doc)
            if not reasons:_time_contract(report,doc)
            result=detection.detect_item(source,target,items[0],progress=progress,cancelled=cancelled)
        except (InterruptedError,detection.ContactRestorationError):raise
        except Exception as exc:
            result=_failed(calibration_doc,source,target,action,'analysis_failed',type(exc).__name__,str(exc))
    if cancelled():raise InterruptedError('转换后检测已取消')
    result=detection.pure(result)
    result['automatic_pipeline']={'schema':'c94.retarget.complete_detection.v1','after_final_action':True,'approval':None}
    report['contact_detection']=result
    action[rt.META]=json.dumps(report,ensure_ascii=False,allow_nan=False)
    converted['report']=report
    return converted
