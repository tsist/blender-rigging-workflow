# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit contact-report items for the existing queue; no solve or approval.

Coordinates come from explicitly calibrated surface or bone-local markers.
Schedules, measurement coverage, planes and scales are supplied explicitly.
"""
import json
import math
import bpy
from . import retarget_action as rt,retarget_contact_samples as sampler
from . import retarget_contact as analysis,retarget_contact_correction as correction
from . import retarget_contact_anchors as anchors

KIND='CONTACT_DETECTION'
SCHEMA='c94.contact.detection.job.v1'
CALIBRATION_SCHEMA='c94.contact.detection.calibration.v1'
TEXT_MARKER='c94_contact_report_job_v1'

class ContactRestorationError(RuntimeError):
    """The existing detection restore/guard failed; never downgrade to a report."""

def require(value,message):
    if not value:raise ValueError(message)
def pure(value):return json.loads(json.dumps(value,allow_nan=False))
def digest(value):return correction.digest(value)
def _action(rig,action,slot):
    from . import retarget_job as jobs
    if isinstance(action,str):action=bpy.data.actions.get(action)
    require(isinstance(action,bpy.types.Action),'必须明确已存在的源/目标Action')
    slot=jobs._find_slot(action,slot)
    require(slot.target_id_type=='OBJECT','检测只接受明确OBJECT Slot')
    return action,slot
def _owner(rig,action,slot):
    return {'rig':rig.name,'action':action.name,'slot':slot.identifier,
            'action_fingerprint':correction.action_fingerprint(action)}
def _mesh_identity(doc):
    obj=bpy.context.scene.objects.get(doc.get('mesh',''))
    return correction._mesh_signature(obj) if obj and obj.type=='MESH' else None

def _anchor_identity(row):
    rig=bpy.context.scene.objects.get(row.get('rig',''))
    return anchors.identity(bpy.context.scene,rig,row.get('anchors'))

def resolve_items(source,target,raw_items,job_id):
    from . import retarget_job as jobs
    if isinstance(raw_items,dict):raw_items=[raw_items]
    require(isinstance(raw_items,(list,tuple)) and 1<=len(raw_items)<=64,'明确1..64个CONTACT_DETECTION条目')
    items=[];dependencies=[]
    for i,raw in enumerate(raw_items):
        require(isinstance(raw,dict) and not(set(raw)-{'kind','source_action','source_slot','target_action','target_slot','calibration','label'}),'检测条目含未知字段')
        require(raw.get('kind')==KIND,'检测条目必须明确CONTACT_DETECTION')
        sa,ss=_action(source,raw.get('source_action'),raw.get('source_slot'))
        ta,ts=_action(target,raw.get('target_action'),raw.get('target_slot'))
        require(rt.META in ta,'目标必须为现有基础候选Action')
        report=json.loads(ta[rt.META]);require(report.get('target_uid')==target.get(rt.TARGET_UID),'目标候选UID不同')
        require(correction.META not in ta,'先检测独立基础动作，不把修正版本伪装成初次结果')
        doc=pure(raw.get('calibration',{}));require(isinstance(doc,dict),'校准必须为纯JSON对象')
        measurement_mode=anchors.mode(doc)
        require(doc.get('schema') in (None,CALIBRATION_SCHEMA),'校准schema不支持')
        data={'calibration':doc,'source':_owner(source,sa,ss),'target':_owner(target,ta,ts),
              'mesh_identity':{} if measurement_mode==anchors.MODE else
                  {role:_mesh_identity(doc.get(role,{})) for role in ('source','target')},
              'object_input_identity':{}}
        if measurement_mode==anchors.MODE:
            data['mesh_identity']={}
            data['measurement_identity']={}
            data['object_input_identity']={}
            for role,rig,action,slot in (('source',source,sa,ss),('target',target,ta,ts)):
                data['object_input_identity'][role]=correction._object_input_stamp(rig,action,slot)
                row=doc.get(role,{})
                # Missing/invalid calibration remains a genuine needs_calibration report.
                try:
                    require(row.get('rig')==rig.name,'骨锚点校准rig与所选角色不同')
                    data['measurement_identity'][role]=_anchor_identity(row)
                except (ValueError,TypeError,AttributeError):
                    data['measurement_identity'][role]=None
        else:
            data['object_input_identity']={
                role:correction._object_input_stamp(rig,action,slot)
                for role,rig,action,slot in (('source',source,sa,ss),('target',target,ta,ts))}
        items.append({'index':i,'id':f'item-{i:03d}','kind':KIND,'contact_detection':data,'state':'not_started',
            'action_name':sa.name,'action_signature':jobs.calibration.action_signature(sa),'slot_identifier':ss.identifier,
            'slot_handle':int(ss.handle),'fps':None,'clip_range':None,'start':None,'end':None,'output_start':1,
            'output_action_name':f'足底只读报告_{job_id[:8]}_{i:03d}','label':raw.get('label') or '明确足底只读检测','validation_error':None})
        dependencies.extend((sa,ta))
    return items,list(dict.fromkeys(dependencies))

def _calibration_reasons(doc):
    reasons=[];partial=False
    try:measurement_mode=anchors.mode(doc)
    except ValueError:return [{'code':'measurement_mode_invalid'}],False
    if doc.get('schema')!=CALIBRATION_SCHEMA:reasons.append({'code':'calibration_schema_missing'})
    for role in ('source','target'):
        row=doc.get(role,{})
        if not isinstance(row,dict):reasons.append({'code':'subject_calibration_missing','role':role});continue
        if measurement_mode==anchors.MODE:
            try:
                require(row.get('coverage_status')==anchors.COVERAGE,'需要显式三骨锚点校准')
                _anchor_identity(row)
                sampler._plane_doc(row.get('plane'))
            except (ValueError,TypeError,AttributeError,sampler.SamplingError) as exc:
                reasons.append({'code':'bone_anchor_calibration_invalid','role':role,'message':str(exc)})
            if row.get('plane',{}).get('status')!='explicit':reasons.append({'code':'plane_not_explicit','role':role})
            for key in ('frames','times_s','scales','root_motion'):
                if key not in row:reasons.append({'code':'calibration_field_missing','role':role,'field':key})
            continue
        mesh=bpy.context.scene.objects.get(row.get('mesh',''))
        if mesh is None:reasons.append({'code':'source_mesh_missing' if role=='source' else 'target_mesh_missing','role':role,'message':'需要真实正权Mesh；BVH骨架或骨tail不能替代皮肤'})
        elif mesh.type!='MESH':reasons.append({'code':'not_a_mesh','role':role})
        elif mesh.data.shape_keys is not None:reasons.append({'code':'shape_key_mesh_requires_adapter','role':role,'message':'当前窄入口不承诺其他Key所有者raw恢复'})
        coverage=row.get('coverage_status')
        if coverage!='explicit_calibrated_complete':
            partial=True
            if not doc.get('allow_partial_evidence',False):reasons.append({'code':'coverage_incomplete','role':role,'message':'完整sole未校准；不使用隐藏P4/零权顶点或最低点补足'})
        if row.get('plane',{}).get('status')!='explicit':reasons.append({'code':'plane_not_explicit','role':role})
        for key in ('patches','group_whitelist','frames','times_s','scales','root_motion'):
            if key not in row:reasons.append({'code':'calibration_field_missing','role':role,'field':key})
        patches=row.get('patches',{})
        if set(patches)!=set(sampler.SIDES):reasons.append({'code':'both_sides_required_by_analysis_kernel','role':role})
    if 'alignment' not in doc:reasons.append({'code':'alignment_missing'})
    if 'settings' not in doc:reasons.append({'code':'explicit_detection_settings_missing'})
    return reasons,partial

def _sample(scene,rig,action,slot,row,*,progress,cancelled,phase,measurement_mode='surface'):
    if measurement_mode==anchors.MODE:require(row.get('rig')==rig.name,'骨锚点校准rig不同')
    ad=rig.animation_data_create();ad.action=action;ad.action_slot=slot
    ad.use_nla=False;ad.action_blend_type='REPLACE';ad.action_influence=1.
    mesh=scene.objects[row['mesh']] if measurement_mode=='surface' else None
    frames,times=row['frames'],row['times_s']
    sampler._validate_frames(frames,times)
    sampler._plane_doc(row['plane'])
    combined={s:{r:{'mode':'points','samples':[],'patch_min':[]} for r in sampler.REGIONS} for s in sampler.SIDES}
    guards=[];n=len(frames);start=0
    # Existing sampler has no cancellation callback. Bounded eight-frame
    # calls reuse it unchanged; only concatenate its returned measurements.
    while start<n:
        if cancelled():raise InterruptedError('足底采样已取消')
        end=min(start+8,n);begin=start if end-start>=2 else start-1
        if measurement_mode==anchors.MODE:
            part=anchors.sample_schedule(scene,rig,row['anchors'],frames[begin:end],times[begin:end],
                plane=row['plane'],state_core=rt if '骨架设置' in rig.pose.bones else None)
        else:
            part=sampler.sample_mesh_schedule(scene,mesh,rig,row['patches'],row['group_whitelist'],
                frames[begin:end],times[begin:end],plane=row['plane'],emit='points',
                state_core=rt if '骨架设置' in rig.pose.bones else None,state_core_name='existing_rt_or_sampler_declared_generic')
        skip=start-begin
        for side in sampler.SIDES:
            for region in sampler.REGIONS:
                src=part['samples'][side][region];dst=combined[side][region]
                dst['samples'].extend(src['samples'][skip:]);dst['patch_min'].extend(src['patch_min'][skip:])
        guards.append(part['state_guard']);start=end;progress(phase,start,n)
    if cancelled():raise InterruptedError('足底采样已取消')
    reference=_owner(rig,action,slot);reference.update(target_uid=rig.get(rt.TARGET_UID))
    if measurement_mode==anchors.MODE:
        reference.update(measurement_mode=anchors.MODE,anchor_identity=anchors.identity(scene,rig,row['anchors']),
                         object_inputs=correction._object_input_stamp(rig,action,slot))
    else:reference.update(mesh=mesh.name,object_inputs=correction._object_input_stamp(rig,action,slot))
    unit=scene.unit_settings.scale_length;require(math.isfinite(unit) and unit>0,'Scene米制单位无效')
    for regions in combined.values():
        for patch in regions.values():
            patch['samples']=[[[v*unit for v in point] for point in points] for points in patch['samples']]
            patch['patch_min']=[v*unit for v in patch['patch_min']]
    metric_plane={**row['plane'],'d':row['plane']['d']*unit}
    reference['coordinate_units']='meters_from_scene_world_units'
    return {'reference':reference,'timestamps_s':times,'scales':row['scales'],'plane':metric_plane,
            'root_motion':row['root_motion'],'sides':combined},guards

def detect_item(source,target,item,*,progress,cancelled):
    from . import retarget_job as jobs
    data=item['contact_detection'];doc=data['calibration'];reasons,partial=_calibration_reasons(doc)
    measurement_mode=anchors.mode(doc)
    result={'schema':SCHEMA,'kind':KIND,'input_sha256':digest(data),'status':'needs_calibration','read_only':True,'full_sole_pass':False,
        'identity':pure({'source':data['source'],'target':data['target'],'target_uid':target.get(rt.TARGET_UID)}),
        'calibration':doc,'calibration_sha256':digest(doc),'mesh_identity':data['mesh_identity'],
        'object_input_identity':data.get('object_input_identity',{}),'reasons':reasons,'detection_report':None,
        'hand_contact':{'status':'not_analyzed','message':'本入口未分析手部接触，不自动修正'},
        'body_contact':{'status':'not_analyzed','message':'本入口未分析身体接触，不自动修正'},
        'approval':None,'writes_action':False,'writes_rest':False,'writes_weights':False,
        'limitations':['只读显式表面有限采样，不代表连续接触/美术质量','缺校准不猜测地面或足底','报告不授修正或发布批准']}
    if measurement_mode==anchors.MODE:
        result.update(measurement_mode=anchors.MODE,coverage_scope=anchors.SCOPE,
            anchors_calibrated=False,full_sole_calibrated=False,skin_surface_evaluated=False,
            measurement_identity=data.get('measurement_identity',{}),
            object_input_identity=data.get('object_input_identity',{}),
            limitations=['仅明确骨局部标记，不代表皮肤/完整足底或连续接触','缺地面/锚点不推断','报告不授批准'])
    if reasons:
        if all(r['code']=='coverage_incomplete' for r in reasons):result['status']='coverage_incomplete'
        return result
    sa,ss=_action(source,data['source']['action'],data['source']['slot']);ta,ts=_action(target,data['target']['action'],data['target']['slot'])
    for rig,action,slot,role in ((source,sa,ss,'source'),(target,ta,ts,'target')):
        require(_owner(rig,action,slot)==data[role],'Action/Slot内容与捕获输入不符')
        if measurement_mode==anchors.MODE:
            require(doc[role]['rig']==rig.name and _anchor_identity(doc[role])==data['measurement_identity'][role],
                    '骨锚点/骨架rest与捕获校准不同')
            require(correction._object_input_stamp(rig,action,slot)==data['object_input_identity'].get(role),
                    '骨锚点对象输入/父空间已变化，请重新捕获检测输入')
        else:require(_mesh_identity(doc[role])==data['mesh_identity'][role],'Mesh/权重/拓扑与捕获校准不同')
    source_state=sampler._capture_state(bpy.context.scene,source,state_core=rt if '骨架设置' in source.pose.bones else None)
    target_capture=sampler._capture_state(bpy.context.scene,target,state_core=rt)
    target_state=target_capture['state']
    before_actions={a.name:correction.action_fingerprint(a) for a in (sa,ta)}
    before_graphs={r.name:jobs._rig_static_fingerprint(r)['static_sha256'] for r in (source,target)}
    try:
        src,sg=_sample(bpy.context.scene,source,sa,ss,doc['source'],progress=progress,cancelled=cancelled,phase='CONTACT_SOURCE',measurement_mode=measurement_mode)
        dst,tg=_sample(bpy.context.scene,target,ta,ts,doc['target'],progress=progress,cancelled=cancelled,phase='CONTACT_TARGET',measurement_mode=measurement_mode)
        if cancelled():raise InterruptedError('检测分析前取消')
        report=analysis.analyze_contact({'schema_version':'1.0','settings':doc.get('settings',{}),
            'alignment':doc['alignment'],'source':src,'target':dst})
        if measurement_mode==anchors.MODE:
            report.update(measurement_mode=anchors.MODE,coverage_scope=anchors.SCOPE,
                calibration_sha256=digest(doc),skin_surface_evaluated=False,full_sole_pass=False)
            for issue in report['issues']:
                issue.update(measurement_mode=anchors.MODE,coverage_scope=anchors.SCOPE,calibration_sha256=digest(doc))
        result.update(status='coverage_incomplete' if partial else 'analyzed',detection_report=report,
            source_sampling_guards=sg,target_sampling_guards=tg,full_sole_calibrated=not partial and measurement_mode=='surface',
            issue_count=len(report['issues']))
        # Even complete, successful sampling does not assert defect-free feet.
        result['full_sole_pass']=False
        if measurement_mode==anchors.MODE:result.update(anchors_calibrated=True,full_sole_calibrated=False)
    except anchors.AnchorRestorationError as exc:
        raise ContactRestorationError('骨锚点采样恢复失败') from exc
    except sampler.SamplingError as exc:
        result.update(status='needs_calibration',reasons=[exc.as_dict()])
    finally:
        try:
            sampler._restore_state(source_state)
            rt.restore(target,target_state,set_scene_time=False)
            source_guard=sampler._declared_state_equal(source_state);target_guard=sampler._declared_state_equal(target_capture)
            require(all(source_guard.values()) and all(target_guard.values()),'原source/target声明raw或绑定恢复失败')
            require({a.name:correction.action_fingerprint(a) for a in (sa,ta)}==before_actions,'检测改变了原Action内容')
            require({r.name:jobs._rig_static_fingerprint(r)['static_sha256'] for r in (source,target)}==before_graphs,'检测改变了控制图/rest')
        except Exception as exc:
            raise ContactRestorationError('只读检测恢复或原状态保护失败') from exc
    result['state_guard']={'action_content_equal':True,'native_graph_equal':True,'source':source_guard,'target':target_guard,'restored_by':'existing_sampler_and_rt','whole_scene_restore_claimed':False}
    return result

def validate_report(document,*,item=None,job_id=None):
    require(isinstance(document,dict) and document.get('schema')==SCHEMA and document.get('kind')==KIND,'不是本入口只读报告')
    require(document.get('read_only') is True and document.get('approval') is None and document.get('writes_action') is False,'报告不满足只读/无授权合同')
    require(digest(document['calibration'])==document['calibration_sha256'],'报告校准hash不符')
    if anchors.mode(document)==anchors.MODE:
        require(anchors.mode(document['calibration'])==anchors.MODE and
            document.get('coverage_scope')==anchors.SCOPE and document.get('skin_surface_evaluated') is False and
            document.get('full_sole_pass') is False and document.get('full_sole_calibrated') is False,
            '骨锚点报告不能声称完整皮肤足底或跨用表面模式')
    marker=document.get('background_job',{})
    if job_id is not None:require(marker.get('job_id')==job_id,'报告job身份不同')
    if item is not None:
        expected=digest(item['contact_detection']) if 'contact_detection' in item else item['detection_input_sha256']
        require(marker.get('item_id')==item['id'] and document['input_sha256']==expected,'报告item/明确采样输入不同')
    return document

def collect_report(document,job_id,item):
    validate_report(document,item=item,job_id=job_id)
    marker={'job_id':job_id,'item_id':item['id'],'document_sha256':digest(document)}
    found=[]
    for text in bpy.data.texts:
        raw=text.get(TEXT_MARKER)
        if not isinstance(raw,str):continue
        row=json.loads(raw)
        if row.get('job_id')==job_id and row.get('item_id')==item['id']:found.append(text)
    require(len(found)<=1,'同一报告存在多个本次标记，拒绝猜测')
    if found:
        text=found[0];same=text.as_string()==json.dumps(document,ensure_ascii=False,indent=2)
        return text,True,'matches' if same else 'modified_existing_retained'
    text=bpy.data.texts.new('C94_足底只读报告_'+job_id[:8]+'_'+item['id']);text.write(json.dumps(document,ensure_ascii=False,indent=2));text[TEXT_MARKER]=json.dumps(marker)
    return text,False,'matches'

def correction_inputs(target,document,issue_id):
    validate_report(document)
    measurement_mode=anchors.mode(document)
    if measurement_mode==anchors.MODE:
        require(document.get('status')=='analyzed' and document.get('anchors_calibrated') is True and
            document.get('coverage_scope')==anchors.SCOPE and document.get('skin_surface_evaluated') is False and
            document.get('full_sole_calibrated') is False,'只有明确三骨锚点就绪报告可转该模式修正')
    else:require(document.get('status')=='analyzed' and document.get('full_sole_calibrated') is True,'不完整/未校准报告不能传为完整sole修正')
    report=document['detection_report'];require(isinstance(report,dict),'报告无分析结果')
    rows=[r for r in report['issues'] if r['id']==issue_id];require(len(rows)==1 and rows[0]['defect'] and rows[0]['kind'] in correction.KINDS,'需选择精确可修缺陷issue_id')
    reference=report['target']['reference'];binding=rt.binding(target)
    require(reference['target_uid']==target.get(rt.TARGET_UID) and binding['action'] and binding['action'].name==reference['action'] and binding['slot'].identifier==reference['slot'],'先明确绑定检测报告的原基础Action/Slot，不自动覆盖当前工作')
    require(correction.action_fingerprint(binding['action'])==reference['action_fingerprint'],'原基础Action已变化，请重新检测')
    row=document['calibration']['target'];side=rows[0]['side']
    if measurement_mode==anchors.MODE:
        require(anchors.mode(document['calibration'])==anchors.MODE and report.get('measurement_mode')==anchors.MODE and
            report.get('calibration_sha256')==document['calibration_sha256'] and
            rows[0].get('measurement_mode')==anchors.MODE and rows[0].get('calibration_sha256')==document['calibration_sha256'],
            '骨锚点报告/问题/校准模式或摘要不一致')
        require(row['rig']==target.name and _anchor_identity(row)==document['measurement_identity']['target'],
                '目标骨锚点身份已变化，请重新检测')
        object_inputs=document.get('object_input_identity',{}).get('target')
        require(isinstance(object_inputs,dict) and reference.get('object_inputs')==object_inputs and
                correction._object_input_stamp(target,binding['action'],binding['slot'])==object_inputs,
                '目标对象输入/父空间与检测时不同或旧报告缺身份，请重新检测')
        calibration={'measurement_mode':anchors.MODE,'coverage_status':anchors.COVERAGE,
            'target_uid':target.get(rt.TARGET_UID),'side':side,'rig':target.name,'anchors':row['anchors'],
            'anchor_identity':document['measurement_identity']['target'],
            'detection_calibration_sha256':document['calibration_sha256'],
            'detection_object_inputs':object_inputs,
            'plane':row['plane'],'frame_time_pairs':[(f,t) for f,t in zip(row['frames'],row['times_s']) if type(f) is int]}
    else:
        require(report.get('measurement_mode','surface')=='surface' and rows[0].get('measurement_mode','surface')=='surface',
                '骨锚点问题不可作为表面修正传递')
        mesh=bpy.context.scene.objects.get(row['mesh'])
        detection_mesh=document.get('mesh_identity',{}).get('target')
        require(isinstance(detection_mesh,dict) and detection_mesh,
                '检测报告缺少目标Mesh检测时身份，请重新检测；不能用当前Mesh补签旧报告')
        object_inputs=document.get('object_input_identity',{}).get('target')
        require(isinstance(object_inputs,dict),
                '检测报告缺少目标对象输入身份，请重新检测；不能用当前对象补签旧报告')
        require(reference.get('mesh')==row['mesh'],'检测报告目标Mesh与校准Mesh不同，请重新检测')
        require(reference.get('object_inputs')==object_inputs,'检测报告目标对象输入身份与检测外层不同，请重新检测')
        require(mesh and correction._mesh_signature(mesh)==detection_mesh,'目标皮肤校准已变化，请重新检测')
        require(correction._object_input_stamp(target,binding['action'],binding['slot'])==object_inputs,
                '目标对象输入/父空间自检测后变化，请重新检测')
        calibration={'coverage_status':'explicit_calibrated_complete','target_uid':target.get(rt.TARGET_UID),
            'side':side,'mesh':row['mesh'],'patches':row['patches'][side],
            'group_whitelist':row['group_whitelist'].get(side,row['group_whitelist']) if isinstance(row['group_whitelist'],dict) else row['group_whitelist'],
            'plane':row['plane'],'frame_time_pairs':list(zip(row['frames'],row['times_s'])),
            'detection_mesh_identity':detection_mesh,'detection_object_inputs':object_inputs,
            'detection_calibration_sha256':document['calibration_sha256']}
    interval=rows[0].get('evidence',{}).get('target_interval_s',[rows[0]['time_start_s'],rows[0]['time_end_s']])
    frames=[int(f) for f,t in zip(row['frames'],row['times_s']) if type(f) is int and interval[0]-1e-8<=t<=interval[1]+1e-8]
    require(len(frames)>=2 and frames==list(range(min(frames),max(frames)+1)),'问题采样未覆盖至少两帧连续整数修正域；先补明确校准')
    return report,calibration,frames
