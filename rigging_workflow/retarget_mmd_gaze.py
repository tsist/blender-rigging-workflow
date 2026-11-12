# SPDX-License-Identifier: GPL-3.0-or-later
"""C94 tracking-target adapter from evaluated MMD eye directions in head space."""
import json,math,uuid
import bpy
from mathutils import Matrix,Vector
from . import pose_switching as pose,pose_switching_leg as leg
from . import retarget_action as rt,retarget_source as source_api,face_modes as fm,face_expression_core as core

META='c94_retarget_mmd_gaze_v1'

def _target_graph(target):
    names=['头颅控制器','眼部控制器']+[stem+side for side in ('L','R') for stem in ('眼部_master.','眼部.','机制-眼部.','形变-眼部.')]
    fields=('type','influence','mute','owner_space','target_space','subtarget','track_axis','head_tail',
            'use_limit_x','use_limit_y','use_limit_z','min_x','max_x','min_y','max_y','min_z','max_z','use_transform_limit','mix_mode')
    out={}
    for name in names:
        pb=target.pose.bones[name];constraints=[]
        for c in pb.constraints:
            row={n:getattr(c,n) for n in fields if hasattr(c,n)}
            if hasattr(c,'target'):
                if c.target!=target:raise ValueError('视线图出现外来约束目标：'+name)
                row['target']='SELF'
            constraints.append(row)
        out[name]={'parent':pb.parent.name if pb.parent else None,'rest':[list(r) for r in pb.bone.matrix_local],
                   'constraints':constraints,'lock_location':list(pb.lock_location),'rotation_mode':pb.rotation_mode}
    return out

def _unit(vector,label):
    if vector.length<1e-8:raise ValueError('视线参考退化：'+label)
    return vector.normalized()

def _frame(left,right,up):
    y=_unit(up,'head up');x=left-right;x=x-y*x.dot(y);x=_unit(x,'eye across')
    z=x.cross(y).normalized();return Matrix((x,y,z)).transposed()

class GazeAdapter:
    """Matrices supplied to solve are evaluated source ARMATURE-space matrices.

    Head-local transport removes object orientation, translation and positive
    uniform scale. No bone-tail direction is guessed: explicit eye endpoints
    must be real children of the corresponding eye bones.
    """
    def __init__(self,source,target,source_names=None):
        source_api.armature(source);source_api.armature(target)
        self.source=source;self.target=target
        self.target_graph=_target_graph(target)
        self.names=source_names or {'head':'頭','L':'左目','R':'右目','L_end':'左目先','R_end':'右目先'}
        if len(set(self.names.values()))!=len(self.names):raise ValueError('视线源语义重复')
        if any(name not in source.data.bones for name in self.names.values()):raise ValueError('视线源缺少头/双眼/实际末端')
        for rig in (source,target):
            scale=rig.matrix_world.to_scale()
            if rig.matrix_world.determinant()<=0 or min(scale)<=0 or max(scale)-min(scale)>1e-5:
                raise ValueError('当前视线适配只验证正均匀对象缩放')
            rotation=rig.matrix_world.to_quaternion().to_matrix()
            if max(abs(rig.matrix_world.to_3x3()[i][j]-rotation[i][j]*scale[0]) for i in range(3) for j in range(3))>1e-5:
                raise ValueError('当前视线适配未验证对象剪切')
        if any(source.data.bones[self.names[s+'_end']].parent!=source.data.bones[self.names[s]] for s in ('L','R')):
            raise ValueError('视线末端不是对应眼骨的实际子骨')
        self.source_head_rest=source.data.bones[self.names['head']].matrix_local.to_quaternion()
        sr={k:source.data.bones[v].matrix_local for k,v in self.names.items()}
        self.source_frame_rest=_frame(sr['L'].translation,sr['R'].translation,sr['head'].to_3x3().col[1])
        self.source_rest_directions={side:self.source_frame_rest.transposed()@_unit(sr[side+'_end'].translation-sr[side].translation,'source '+side) for side in ('L','R')}
        er=pose._update(target)
        head=target.pose.bones['头颅控制器']
        self.target_head_rest=head.bone.matrix_local.to_quaternion()
        self.target_frame_rest=_frame(target.data.bones['眼部_master.L'].head_local,target.data.bones['眼部_master.R'].head_local,head.bone.matrix_local.to_3x3().col[1])
        frame=self.target_frame(er)
        self.distance={};self.target_neutral_directions={}
        for side in ('L','R'):
            pb=target.pose.bones['眼部.'+side];mechanism=target.pose.bones['机制-眼部.'+side]
            if pb.constraints or any(pb.lock_location):raise ValueError('目标眼部位置输入不是已验无约束可写控件：'+pb.name)
            cs=mechanism.constraints
            if len(cs)!=2 or cs[0].type!='DAMPED_TRACK' or cs[1].type!='LIMIT_ROTATION':raise ValueError('目标眼机制图不同：'+mechanism.name)
            track=cs[0]
            if not(track.target==target and track.subtarget==pb.name and track.track_axis=='TRACK_Y' and track.head_tail==0 and not track.mute and abs(track.influence-1)<1e-6):
                raise ValueError('目标眼部tracking关系不同：'+pb.name)
            center=er.pose.bones[mechanism.name].matrix.translation
            point=er.pose.bones[pb.name].matrix.translation
            self.distance[side]=(point-center).length
            if self.distance[side]<1e-5:raise ValueError('目标眼部目标距离退化')
            self.target_neutral_directions[side]=frame.transposed()@er.pose.bones[mechanism.name].matrix.to_3x3().col[1].normalized()

    def target_frame(self,er):
        delta=er.pose.bones['头颅控制器'].matrix.to_quaternion()@self.target_head_rest.inverted()
        return delta.to_matrix()@self.target_frame_rest

    def source_directions(self,matrices):
        head=matrices['head'].to_quaternion();frame=(head@self.source_head_rest.inverted()).to_matrix()@self.source_frame_rest
        out={}
        for side in ('L','R'):
            direction=frame.transposed()@_unit(matrices[side+'_end'].translation-matrices[side].translation,'evaluated '+side)
            delta=self.source_rest_directions[side].rotation_difference(direction)
            out[side]=(delta@self.target_neutral_directions[side]).normalized()
        return out

    def solve(self,source_matrices):
        directions=self.source_directions(source_matrices);er=pose._update(self.target);frame=self.target_frame(er)
        wanted={}
        for side in ('L','R'):
            name='眼部.'+side;pb=self.target.pose.bones[name];center=er.pose.bones['机制-眼部.'+side].matrix.translation
            direction=frame@directions[side];wanted[side]=direction
            aim=er.pose.bones[name].matrix.copy();aim.translation=center+direction*self.distance[side]
            pb.location=leg._basis(pb,aim,er).translation
        er=pose._update(self.target);diagnostics={}
        for side in ('L','R'):
            actual=er.pose.bones['机制-眼部.'+side].matrix.to_3x3().col[1].normalized()
            angle=math.degrees(actual.angle(wanted[side]))
            diagnostics[side]={'direction_residual_degrees':angle,'head_space_direction':list(directions[side]),'actual_armature_direction':list(actual)}
        return diagnostics

def validate_preview(target,action,slot):
    report=json.loads(action[META])
    source_api.require(_target_graph(target)==report['target_graph'],'视线候选的原生目标图/静置骨轴已改变，请重新转换')
    core._baseline(target,report['neutral_fingerprint'])
    keys={(f.data_path,f.array_index) for f in fm._curves(action,slot)}
    source_api.require(set(map(tuple,report['write_paths']))<=keys,'视线候选缺少声明原生眼控输入')
    return True

def create_candidate(source,target,*,base_action,source_fps=30.0,start=None,end=None,
                     output_start=1,source_names=None,source_kind='real_clip',action_name=None,progress=None,cancelled=None):
    """Append gaze to a copied shared-core body/face candidate, keeping originals.

    This is control/constraint verification. Existing target eye skin weighting
    is not repaired and must be checked separately from direction residuals.
    """
    source_api.require(not rt.has_preview(target),'请先停止目标预览')
    neutral=fm._read(target)['neutral'];fingerprint=fm.hash_record(neutral);core._baseline(target,fingerprint)
    clip=source_api.active_clip(source,source_fps)
    start=clip['range'][0] if start is None else float(start);end=clip['range'][1] if end is None else float(end)
    source_api.require(clip['range'][0]<=start<end<=clip['range'][1] and type(output_start) is int,'源视线片段/起始帧无效')
    scene=bpy.context.scene;fps=scene.render.fps/scene.render.fps_base
    source_api.require(scene.render.frame_map_old==scene.render.frame_map_new,'视线转换不支持场景时间重映射')
    source_api.require(rt.META in base_action,'视线合成必须给出共享核心身体候选')
    report=json.loads(base_action[rt.META]);slots=[s for s in base_action.slots if s.target_id_type=='OBJECT']
    source_api.require(len(slots)==1 and report.get('target_uid')==target.get(rt.TARGET_UID)
                       and abs(report['target_fps']-fps)<1e-6,'基础候选目标身份/Slot/帧率不符')
    paths={(target.pose.bones['眼部.'+s].path_from_id('location'),i) for s in ('L','R') for i in range(3)}
    source_api.require(not any((f.data_path,f.array_index) in paths for f in fm._curves(base_action,slots[0])),'基础候选已占用眼控写域')
    fm._static(target,{p for p,_ in paths})
    last=output_start+(end-start)*fps/clip['fps'];frames=list(map(float,range(output_start,math.floor(last)+1)))
    if abs(frames[-1]-last)>1e-6:frames.append(last)
    source_api.require(len(frames)<=50000,'视线采样超出50000预算')
    sb=rt.binding(source);state=rt.snapshot(target);created=None
    try:
        source.animation_data.use_nla=False;source.animation_data.action_blend_type='REPLACE';source.animation_data.action_influence=1.;source.animation_data.action_extrapolation='HOLD'
        ad=target.animation_data_create();ad.action=base_action;ad.action_slot=slots[0];ad.use_nla=False;ad.action_blend_type='REPLACE';ad.action_influence=1.;ad.action_extrapolation='HOLD'
        scene.frame_set(output_start);adapter=GazeAdapter(source,target,source_names)
        samples=[]
        for i,frame in enumerate(frames):
            if cancelled and cancelled():raise InterruptedError('视线转换已取消')
            sf=start+(frame-output_start)*clip['fps']/fps;scene.frame_set(math.floor(sf),subframe=sf%1);er=pose._update(source)
            samples.append({k:er.pose.bones[n].matrix.copy() for k,n in adapter.names.items()})
            if progress:progress('GAZE_SOURCE',i+1,len(frames))
        curves={key:[] for key in paths};diagnostics=[]
        for i,(frame,sample) in enumerate(zip(frames,samples)):
            if cancelled and cancelled():raise InterruptedError('视线转换已取消')
            scene.frame_set(math.floor(frame),subframe=frame%1);diag=adapter.solve(sample);diagnostics.append({'frame':frame,'eyes':diag})
            for path,index in paths:curves[path,index].append((frame,float(target.path_resolve(path)[index])))
            if progress:progress('GAZE_TARGET',i+1,len(frames))
        if cancelled and cancelled():raise InterruptedError('视线转换已取消')
        created=base_action.copy();created.name=action_name or base_action.name+'_视线';slot=created.slots[0]
        source_api.require(len(created.layers)==1 and len(created.layers[0].strips)==1 and created.layers[0].strips[0].type=='KEYFRAME','基础候选须有唯一KEYFRAME层/条带')
        bag=created.layers[0].strips[0].channelbag(slot,ensure=True)
        for (path,index),rows in curves.items():
            if cancelled and cancelled():raise InterruptedError('视线转换已取消')
            fc=bag.fcurves.new(path,index=index);fc.keyframe_points.add(len(rows));fc.keyframe_points.foreach_set('co',[v for point in rows for v in point])
            for key in fc.keyframe_points:key.interpolation='LINEAR'
            fc.update()
        if cancelled and cancelled():raise InterruptedError('视线转换已取消')
        gaze_report={'schema':'c94.retarget.mmd-gaze.v1','status':'needs_review','source_kind':source_kind,
                     'source':source.name,'source_action':clip['action'].name,'source_slot':clip['slot'].identifier,
                     'source_mapping':adapter.names,'source_fps':clip['fps'],'source_range':[start,end],'output_range':[output_start,last],
                     'samples':len(frames),'source_matrix_space':'ARMATURE','required_mode':'KEY','neutral_fingerprint':fingerprint,
                     'target_graph':adapter.target_graph,
                     'write_paths':[list(k) for k in sorted(paths)],'direction_residual_max_degrees':max(v['direction_residual_degrees'] for row in diagnostics for v in row['eyes'].values()),
                     'source_head_space_direction_span_degrees':{side:max(math.degrees(Vector(a).angle(Vector(b))) for a in [d['eyes'][side]['head_space_direction'] for d in diagnostics] for b in [diagnostics[0]['eyes'][side]['head_space_direction']]) for side in ('L','R')},
                     'limitations':['native limits retained; nonzero residuals are reported','eye skin consumption requires independent positive-weight test','integer target samples do not establish subframe eye interpolation accuracy'],
                     'preservation':{'source_action_written':False,'previous_target_action_written':False,'constraints_changed':False,'weights_changed':False,'new_keys':False,'mode_changed':False}}
        created[META]=json.dumps(gaze_report,ensure_ascii=False);report['id']=uuid.uuid4().hex;report['mmd_gaze']=gaze_report
        report['output_range']=[min(report['output_range'][0],output_start),max(report['output_range'][1],last)]
        created[rt.META]=json.dumps(report,ensure_ascii=False);created.use_fake_user=True
        return {'action':created,'slot':slot,'report':gaze_report,'diagnostics':diagnostics}
    except BaseException:
        if created is not None:bpy.data.actions.remove(created)
        raise
    finally:
        rt.restore_binding(source,sb);rt.restore(target,state)
