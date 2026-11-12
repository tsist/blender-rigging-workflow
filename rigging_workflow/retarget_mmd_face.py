# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit MMD morph tracks -> existing C94 MOUTH inputs, without new Keys.

All mappings are caller decisions. No Japanese-name heuristic assigns a whole
face preset to an isolated brow/eye morph. Unsupported pad mixtures fail before
any Action is created. The original shared Action preview owns restoration.
"""
import bisect,hashlib,json,math,struct,uuid
from pathlib import Path
import bpy
from . import face_expression_core as core,face_modes as fm,face_mouth_core as mouth
from . import retarget_action as rt

META='c94_retarget_mmd_face_v1'
HUB_REF='c94_retarget_mmd_face_hub_v1'
_require=fm._require

def tracks_from_vmd(path):
    """Read through the user's existing MMD Tools, never install/register it."""
    path=Path(path).resolve(strict=True)
    _require(path.is_file() and path.stat().st_size<=64*1024*1024,'VMD输入须为不大于64MiB的本地文件')
    data=path.read_bytes()
    _require(len(data)>=54 and data.startswith(b'Vocaloid Motion Data 0002'),'VMD头/骨骼段不完整')
    bone_count=struct.unpack_from('<I',data,50)[0];offset=54+111*bone_count
    _require(bone_count<=500000 and offset+4<=len(data),'VMD骨骼段不完整或超预算')
    morph_count=struct.unpack_from('<I',data,offset)[0]
    _require(morph_count<=500000 and offset+4+23*morph_count<=len(data),'VMD表情段不完整或超预算')
    from mmd_tools.core import vmd
    doc=vmd.File();doc.load(filepath=str(path))
    tracks={name:[(int(k.frame_number),float(k.weight)) for k in keys] for name,keys in doc.shapeKeyAnimation.items()}
    duplicates=sum(len(rows)-len(set(rows)) for rows in tracks.values())
    return tracks,{'file':str(path),'sha256':hashlib.sha256(data).hexdigest(),'format':'VMD morph linear','source_fps':30.0,
                  'duplicate_identical_keys':duplicates}

def _tracks(tracks):
    _require(isinstance(tracks,dict) and tracks,'需要明确的MMD表情轨道')
    out={};count=0
    for name,rows in tracks.items():
        _require(isinstance(name,str) and name and isinstance(rows,(tuple,list)) and rows,'表情轨道名/键为空')
        points=[]
        for row in rows:
            _require(isinstance(row,(tuple,list)) and len(row)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in row)
                     and row[0]>=0 and 0<=row[1]<=1,'MMD morph只接受非负帧与有限0..1权重，不静默截断')
            points.append((float(row[0]),float(row[1])))
        points=sorted(set(points));_require(len({p[0] for p in points})==len(points),'同轨道同帧不同权重有歧义')
        out[name]=points;count+=len(points)
    _require(count<=500000,'表情键超出500000预算')
    return out

def _value(points,frame):
    i=bisect.bisect_right(points,(frame,math.inf))
    if i==0:return points[0][1]
    if i==len(points):return points[-1][1]
    a,b=points[i-1],points[i];return a[1]+(b[1]-a[1])*(frame-a[0])/(b[0]-a[0])

def pad_outputs(record,x,y):
    return {r['name']:max(0,1-abs(x-r['x']))*max(0,1-abs(2*y-r['y'])) for r in record['grid']}

def inverse_pad(record,wanted):
    """Exact bilinear-grid inverse, including implicit Basis at (0,0)."""
    _require(sum(wanted.values())<=1+1e-6,'二维口型权重总和超过1，不可表达')
    x=sum(wanted[r['name']]*r['x'] for r in record['grid'])
    y=sum(wanted[r['name']]*r['y'] for r in record['grid'])/2
    actual=pad_outputs(record,x,y)
    error=max(abs(actual[n]-v) for n,v in wanted.items())
    _require(error<=1e-6,'二维口型混合不可表达；保留原图，不近似反解。最大通道差='+str(error))
    return x,y

def _mapping(mapping,tracks,channels):
    _require(isinstance(mapping,list) and mapping,'需要明确的源→已有目标表情映射')
    rows=[]
    for row in mapping:
        _require(isinstance(row,dict) and set(row)=={'source','target','quality','reason'} and row['source'] in tracks
                 and row['target'] in channels and row['quality'] in {'semantic_review','fixture_exact'}
                 and isinstance(row['reason'],str) and row['reason'],'映射必须有已有源/目标、语义审阅级别及理由')
        rows.append(dict(row))
    _require(len({r['source'] for r in rows})==len(rows) and len({r['target'] for r in rows})==len(rows),
             '当前入口只接受一对一显式映射，不自动合并叠加')
    return rows

def validate_preview(target,action,slot):
    """Shared preview calls this before binding; mode is required, never set."""
    report=json.loads(action[META]);hub=action.get(HUB_REF)
    _require(isinstance(hub,bpy.types.Object),'面部候选丢失实际Hub ID引用')
    group,record,rig,_=mouth._read(hub)
    _require(rig==target and report['group_id']==group['group_id'] and report['mouth_record']==record,'面部候选的实际Hub/控制图已改变')
    core._baseline(target,group['neutral_fingerprint'])
    curves=fm._curves(action,slot);keys={(f.data_path,f.array_index) for f in curves}
    _require(set(map(tuple,report['write_paths']))<=keys,'面部候选缺少声明输入曲线')
    return True

def create_candidate(hub,tracks,*,mapping,source_metadata,omitted_reasons=None,source_fps=30.0,
                     source_range=None,output_start=1,base_action=None,base_slot=None,action_name=None):
    """Create a face-only or body+face candidate; never rebind or change inputs.

    A base Action must be a shared-core candidate of this exact target. Its
    curves are copied unchanged and cannot overlap the 17 MOUTH inputs. Source
    key events and every target integer frame are included. Pad interpolation
    is accepted only if the native bilinear outputs remain linear per segment.
    """
    group,record,target,members=mouth._read(hub)
    _require(not rt.has_preview(target),'请先停止该目标预览')
    core._baseline(target,group['neutral_fingerprint'])
    tracks=_tracks(tracks);channels={r['name'] for r in group['channels']};mapping=_mapping(mapping,tracks,channels)
    _require(type(source_fps) in (int,float) and math.isfinite(source_fps) and source_fps>0 and type(output_start) is int,'帧率/输出起始帧无效')
    scene=bpy.context.scene;fps=scene.render.fps/scene.render.fps_base
    _require(scene.render.frame_map_old==scene.render.frame_map_new,'面部转换不支持场景时间重映射')
    domain=[p[0] for rows in tracks.values() for p in rows]
    start,end=(min(domain),max(domain)) if source_range is None else source_range
    _require(all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) and min(domain)<=start<end<=max(domain),'源表情片段帧域无效')
    last=output_start+(end-start)*fps/source_fps
    frames={float(output_start),last,*map(float,range(output_start,math.floor(last)+1))}
    for row in mapping:
        frames.update(output_start+(f-start)*fps/source_fps for f,_ in tracks[row['source']] if start<=f<=end)
    frames=sorted(frames);_require(len(frames)<=50000,'面部目标采样点超出50000预算')
    input_rows=mouth._input_rows(record,target);write_paths={(r['path'],r['index']) for r in input_rows}
    _require(len(write_paths)==17,'口型板必须有17个明确原生输入')
    curves={key:[] for key in write_paths};expected=[];previous=None
    for frame in frames:
        sf=start+(frame-output_start)*source_fps/fps
        values={name:0.0 for name in channels}
        for row in mapping:values[row['target']]=_value(tracks[row['source']],sf)
        grid={r['name']:values[r['name']] for r in record['grid']};x,y=inverse_pad(record,grid)
        if previous is not None:
            a=pad_outputs(record,(previous['x']+x)/2,(previous['y']+y)/2)
            _require(max(abs(a[n]-(previous['grid'][n]+grid[n])/2) for n in grid)<=1e-6,
                     '二维口型插值段不可精确表达，不烘焙近似')
        values_by_path={(mouth._path(record['pad']),0):x*record['travel'],(mouth._path(record['pad']),1):y*record['travel']}
        values_by_path.update({(mouth._path(r['bone']),1):values[r['name']]*record['travel'] for r in record['sliders']})
        for key,value in values_by_path.items():curves[key].append((frame,value))
        expected.append({'frame':frame,'source_frame':sf,'values':values});previous={'x':x,'y':y,'grid':grid}
    original_uid=target.get(rt.TARGET_UID);created=None
    try:
        if base_action is not None:
            _require(rt.META in base_action,'合成基础必须是共享核心候选')
            base_report=json.loads(base_action[rt.META])
            _require(base_report.get('target_uid') and base_report['target_uid']==target.get(rt.TARGET_UID) and abs(base_report['target_fps']-fps)<1e-6,'基础候选目标身份/帧率不符')
            slots=[s for s in base_action.slots if s.target_id_type=='OBJECT']
            _require(len(slots)==1 and (base_slot is None or base_slot==slots[0]),'基础候选必须有唯一明确OBJECT Slot')
            _require(not any((f.data_path,f.array_index) in write_paths for f in fm._curves(base_action,slots[0])),'基础候选与面部写域重叠')
            created=base_action.copy();created.name=action_name or base_action.name+'_表情';slot=created.slots[0]
            report=json.loads(created[rt.META]);report['output_range']=[min(report['output_range'][0],output_start),max(report['output_range'][1],last)]
        else:
            created=bpy.data.actions.new(action_name or target.name+'_MMD表情候选');slot=created.slots.new(id_type='OBJECT',name=target.name)
            report={'schema':'c94.retarget.action.v1','id':uuid.uuid4().hex,'adapter':'mmd_existing_mouth_inputs_v1','status':'needs_review',
                    'target':target.name,'target_uid':rt._target_uid(target),'target_fps':fps,'output_range':[output_start,last]}
        if created.layers:
            _require(len(created.layers)==1 and len(created.layers[0].strips)==1 and created.layers[0].strips[0].type=='KEYFRAME','基础候选须有唯一KEYFRAME层/条带')
            bag=created.layers[0].strips[0].channelbag(slot,ensure=True)
        else:bag=created.layers.new('MMD已有表情输入').strips.new(type='KEYFRAME').channelbag(slot,ensure=True)
        for (path,index),rows in curves.items():
            fc=bag.fcurves.new(path,index=index);fc.keyframe_points.add(len(rows));fc.keyframe_points.foreach_set('co',[v for row in rows for v in row])
            for key in fc.keyframe_points:key.interpolation='LINEAR'
            fc.update()
        active={n for n,rows in tracks.items() if any(v>1e-6 for _,v in rows)};mapped={r['source'] for r in mapping}
        face_report={'schema':'c94.retarget.mmd-face.v1','group_id':group['group_id'],'mouth_record':record,'required_mode':'KEY',
                     'source':source_metadata,'source_fps':source_fps,'source_range':[start,end],'output_range':[output_start,last],
                     'mapping':mapping,'samples':len(frames),'write_paths':[list(k) for k in sorted(write_paths)],
                     'omitted_active':{n:(omitted_reasons or {}).get(n,'无显式已有目标映射') for n in sorted(active-mapped)},
                     'status':'needs_review','preservation':{'new_keys':False,'keys_written':False,'drivers_changed':False,'mode_changed':False,'previous_action_written':False}}
        created[META]=json.dumps(face_report,ensure_ascii=False);created[HUB_REF]=hub
        report['mmd_face']={'mapping':mapping,'omitted_active':face_report['omitted_active'],'required_mode':'KEY'}
        report['id']=uuid.uuid4().hex;created[rt.META]=json.dumps(report,ensure_ascii=False);created.use_fake_user=True
        validate_preview(target,created,slot)
        return {'action':created,'slot':slot,'report':face_report,'expected':expected}
    except BaseException:
        if created is not None:bpy.data.actions.remove(created)
        if target.get(rt.TARGET_UID)!=original_uid:
            if original_uid is None:del target[rt.TARGET_UID]
            else:target[rt.TARGET_UID]=original_uid
        raise
