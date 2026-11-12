# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit ordinary-FK target policy for the shared retarget Action core.

No controller inference, C94 generation, curve/contact repair or separate
preview/recovery engine. Unknown control graphs are refused on the original.
"""
import json,math,uuid
import bpy
from mathutils import Matrix,Vector
from . import retarget_action as rt,retarget_calibration as cal,retarget_source as sources
from . import pose_switching as pose

KIND='GENERIC_FK_V1'
META='c94_retarget_generic_target_v1'
DETACHED='c94_retarget_detached_fk_v1'
CHILD={'hips':'spine','spine':'chest','chest':'neck','neck':'head',
    **{a+'.'+side:b+'.'+side for side in ('L','R') for a,b in (
        ('shoulder','upper_arm'),('upper_arm','forearm'),('forearm','hand'),
        ('thigh','shin'),('shin','foot'),('foot','toes'))}}

def require(value,message):
    if not value:raise ValueError(message)

def _matrix(matrix):return [[float(value) for value in row] for row in matrix]

def _world(rig):
    matrix=rig.matrix_world;cols=[matrix.to_3x3().col[i] for i in range(3)];sizes=[v.length for v in cols]
    require(all(math.isfinite(v) for row in matrix for v in row) and min(sizes)>1e-8,'目标对象变换非有限或退化')
    require(matrix.determinant()>0 and max(sizes)-min(sizes)<max(sizes)*1e-5,'通用FK首版不接负/非均匀缩放')
    require(all(abs(cols[i].dot(cols[j]))<sizes[i]*sizes[j]*1e-5 for i in range(3) for j in range(i)),
            '通用FK首版不接剪切变换')
    return sizes[0]

def conflicts(target):
    """Concrete read-only conflicts; no muting/removal on the original rig."""
    sources.armature(target);rows=[]
    if target.library or target.data.library:rows.append({'kind':'linked_target'})
    if target.parent:rows.append({'kind':'object_parent','object':target.parent.name})
    for constraint in target.constraints:rows.append({'kind':'object_constraint','name':constraint.name,'type':constraint.type})
    for pb in target.pose.bones:
        for constraint in pb.constraints:rows.append({'kind':'pose_constraint','bone':pb.name,'name':constraint.name,'type':constraint.type})
    for owner,label in ((target,'OBJECT'),(target.data,'ARMATURE')):
        ad=owner.animation_data
        for curve in ad.drivers if ad else ():
            rows.append({'kind':'driver','owner':label,'path':curve.data_path,'index':curve.array_index})
    ad=target.animation_data
    if ad:
        if ad.use_tweak_mode:rows.append({'kind':'nla_tweak'})
        actions=[ad.action] if ad.action else []
        for track in ad.nla_tracks:
            actions.extend(strip.action for strip in track.strips if strip.action)
        for action in actions:
            for layer in action.layers:
                for strip in layer.strips:
                    for slot in action.slots:
                        if strip.type!='KEYFRAME':continue
                        bag=strip.channelbag(slot)
                        for curve in bag.fcurves if bag else ():
                            if curve.data_path in {'location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale','delta_location','delta_rotation_euler','delta_rotation_quaternion','delta_scale'}:
                                rows.append({'kind':'object_transform_action','action':action.name,'path':curve.data_path})
    for key in ('c94_elastic_v1','c94_surface_dynamic_v1'):
        if key in target:rows.append({'kind':'managed_dynamic_needs_adapter','key':key})
    return rows

def _fingerprint(target):
    return cal.canonical_hash({'rest':cal.bone_rest_signature(target),'world':_matrix(target.matrix_world),
        'raw_object_transform':{field:list(getattr(target,field)) for field in ('location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale','delta_location','delta_rotation_euler','delta_rotation_quaternion','delta_scale')},
        'object_rotation_mode':target.rotation_mode,
        'constraints':conflicts(target),'pose_position':target.data.pose_position})

def _fixed(target,names):
    return {name:{'rotation_mode':target.pose.bones[name].rotation_mode,
            'location':list(target.pose.bones[name].location),'scale':list(target.pose.bones[name].scale)}
        for name in names}

def policy(target,mapping,*,acknowledged=False):
    require(acknowledged is True,'请明确确认：未知控制器回退输出骨骼Action，不是控制器动画')
    sources.armature(target);_world(target)
    found=conflicts(target)
    require(not found,'原目标含未适配控制图；不修改原约束/driver。冲突：'+json.dumps(found[:12],ensure_ascii=False))
    require(target.data.pose_position=='POSE','目标须使用姿态求值模式')
    require(isinstance(mapping,dict) and sources.REQUIRED<=set(mapping),'请明确完整目标身体语义到实际骨名映射')
    require(all(isinstance(k,str) and k in sources.BODY and isinstance(v,str) and v in target.pose.bones for k,v in mapping.items()),'目标映射含无效语义或骨引用')
    require(len(set(mapping.values()))==len(mapping),'目标语义不能复用同一根骨')
    for side in ('L','R'):
        for first,second in (('upper_arm','forearm'),('forearm','hand'),('thigh','shin'),('shin','foot')):
            require(mapping[first+'.'+side] in {p.name for p in target.data.bones[mapping[second+'.'+side]].parent_recursive},'目标肢体层级不符：'+second+'.'+side)
    closure=set(mapping.values())
    for name in tuple(closure):closure.update(p.name for p in target.data.bones[name].parent_recursive)
    names=sorted(closure,key=lambda name:(len(target.data.bones[name].parent_recursive),name))
    root=mapping['hips']
    while target.data.bones[root].parent is not None:root=target.data.bones[root].parent.name
    for name in names:
        bone=target.data.bones[name];pb=target.pose.bones[name]
        require(pb.rotation_mode in ('QUATERNION','XYZ','XZY','YXZ','YZX','ZXY','ZYX'),'不支持该目标rotation mode：'+name)
        require(not any(pb.lock_rotation) and not pb.lock_rotation_w and not (name==root and any(pb.lock_location)), '目标写域有作者锁，不绕过：'+name)
        require(bone.bbone_segments==1,'通用FK首版不接B-Bone：'+name)
        require(bone.use_inherit_rotation and bone.inherit_scale=='FULL','目标特殊继承尚未适配：'+name)
        require(max(abs(v-1) for v in pb.scale)<1e-5,'目标写域已有缩放输入，首版不重写：'+name)
        if name!=root:require(pb.location.length<1e-5,'目标非根骨已有位移输入，首版不重写：'+name)
    fixed=_fixed(target,names);fixed[root]['location']=None
    record={'schema':'c94.retarget.target_policy.v1','kind':KIND,'version':1,'mapping':dict(mapping),
        'root':root,'write_bones':names,'neutral_ancestors':sorted(closure-set(mapping.values())),
        'fixed_inputs':fixed,'target_fingerprint':_fingerprint(target),'acknowledged':True,
        'detached_candidate':DETACHED in target,'body_only':True}
    record['signature']=cal.canonical_hash(record)
    return record

def validate(target,record):
    require(isinstance(record,dict) and record.get('schema')=='c94.retarget.target_policy.v1' and record.get('kind')==KIND and record.get('version')==1,'未知或损坏的目标策略')
    require(record.get('signature')==cal.canonical_hash({k:v for k,v in record.items() if k!='signature'}),'目标策略记录被修改，请重新建立')
    require(_fingerprint(target)==record['target_fingerprint'],'目标rest/对象变换/控制图已改变，请重新转换')
    current=_fixed(target,record['write_bones'])
    for name,expected in record['fixed_inputs'].items():
        require(current[name]['rotation_mode']==expected['rotation_mode'],'目标rotation mode已改变：'+name)
        for field in ('location','scale'):
            if expected[field] is not None:
                require(max(abs(a-b) for a,b in zip(current[name][field],expected[field]))<1e-5,'目标非写入输入已改变：'+name+'/'+field)
    return record

def channels(target,record):
    values={}
    for name in record['write_bones']:
        pb=target.pose.bones[name];field='rotation_quaternion' if pb.rotation_mode=='QUATERNION' else 'rotation_euler'
        for i,value in enumerate(getattr(pb,field)):
            require(math.isfinite(value),'目标旋转产生非有限值');values[(pb.path_from_id(field),i)]=float(value)
    root=target.pose.bones[record['root']]
    for i,value in enumerate(root.location):values[(root.path_from_id('location'),i)]=float(value)
    return values

def isolate_target(target,*,acknowledged=False):
    require(acknowledged is True,'需明确选择隔离FK骨架候选；它不保原控制器效果，也不会改原Mesh绑定')
    sources.armature(target);identifier=uuid.uuid4().hex
    clone=target.copy();clone.data=target.data.copy();clone.name=target.name+'_独立FK_'+identifier[:8]
    clone.animation_data_clear();clone.data.animation_data_clear();clone.parent=None;clone.matrix_world=target.matrix_world.copy()
    for c in list(clone.constraints):clone.constraints.remove(c)
    for pb in clone.pose.bones:
        for c in list(pb.constraints):pb.constraints.remove(c)
        pb.matrix_basis=Matrix.Identity(4);pb.custom_shape=None
        pb.lock_rotation=(False,False,False);pb.lock_rotation_w=False;pb.lock_location=(False,False,False)
    for key in list(clone.keys()):del clone[key]
    collection=bpy.data.collections.new('独立FK候选_'+identifier[:8]);bpy.context.scene.collection.children.link(collection);collection.objects.link(clone)
    clone[DETACHED]=json.dumps({'schema':'c94.retarget.detached_fk.v1','source_object':target.name,
        'source_rest_signature':cal.bone_rest_signature(target),'source_conflicts':conflicts(target),
        'original_graph_changed':False,'original_mesh_bindings_changed':False,'mesh_consumers_added':False,
        'original_controller_behavior_retained':False,'new_c94_generated':False},ensure_ascii=False)
    pose._update(clone)
    return {'rig':clone,'collection':collection,'report':json.loads(clone[DETACHED])}

class Adapter:
    def __init__(self,source,target,mapped,record,*,motion='PRESERVE'):
        validate(target,record);require(motion in ('PRESERVE','IN_PLACE'),'根运动模式无效')
        self.source=source;self.target=target;self.record=record;self.mapping=record['mapping'];self.names=record['write_bones'];self.motion=motion
        self.observed=dict(self.mapping);self.finger_chains=[];self.palm_calibration_sides=[]
        self.source_rest={key:source.matrix_world@source.data.bones[name].matrix_local for key,name in mapped['bones'].items()}
        self.target_rest={key:target.matrix_world@target.data.bones[name].matrix_local for key,name in self.mapping.items()}
        self.root_rest=target.matrix_world@target.data.bones[record['root']].matrix_local
        def body_frame(values):
            x=(values['thigh.L'].translation-values['thigh.R'].translation).normalized()
            z=values['chest'].translation-values['hips'].translation;z-=x*z.dot(x)
            require(z.length>1e-7,'人体REST参考框架退化')
            z.normalize();y=z.cross(x).normalized();x=y.cross(z).normalized()
            return Matrix((x,y,z)).transposed().to_quaternion()
        self.global_alignment=body_frame(self.source_rest)@body_frame(self.target_rest).inverted()
        self.calibration={};self.joint_calibration={};self.skipped_joint_references={}
        for semantic,matrix in self.target_rest.items():
            aligned=self.global_alignment@matrix.to_quaternion();child=CHILD.get(semantic)
            if child in self.source_rest and child in self.target_rest:
                source_direction=self.source_rest[child].translation-self.source_rest[semantic].translation
                target_direction=self.global_alignment@(self.target_rest[child].translation-matrix.translation)
                if min(source_direction.length,target_direction.length)<=1e-7:
                    require(semantic in ('hips','spine','chest','neck'),'真实肢体child参考关节退化：'+semantic)
                    self.calibration[semantic]=aligned
                    self.skipped_joint_references[semantic]={'child':child,'reason':'coincident_rest_joint; retained globally aligned native rest frame'}
                    continue
                swing=target_direction.rotation_difference(source_direction)
                self.calibration[semantic]=swing@aligned
                self.joint_calibration[semantic]={'child':child,'rest_swing_degrees':math.degrees(swing.angle)}
            else:self.calibration[semantic]=aligned
        def leg(values,side):return (values['thigh.'+side].translation-values['shin.'+side].translation).length+(values['shin.'+side].translation-values['foot.'+side].translation).length
        ratios=[leg(self.target_rest,side)/leg(self.source_rest,side) for side in ('L','R')]
        require(all(math.isfinite(v) and v>0 for v in ratios),'源/目标腿长退化')
        self.scale=sum(ratios)/2;self.origin=(source.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world@source.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones[mapped['bones']['hips']].matrix).translation.copy()
        self.previous={};self.max_nonroot_basis_location=0.0;self.max_basis_scale_error=0.0;self.joint_residual_max=0.0
    def solve(self,sample):
        target=self.target;record=self.record;inverse=target.matrix_world.inverted();world_rotation=target.matrix_world.to_quaternion()
        reverse={name:semantic for semantic,name in self.mapping.items()};solved={};desired_rotations={}
        for name in self.names:
            pb=target.pose.bones[name];bone=pb.bone
            kwargs={'parent_matrix':solved[bone.parent.name],'parent_matrix_local':bone.parent.matrix_local} if bone.parent else {}
            natural=bone.convert_local_to_pose(Matrix.Identity(4),bone.matrix_local,**kwargs)
            if name in reverse:
                semantic=reverse[name]
                orientation=(sample[semantic].to_quaternion()@self.source_rest[semantic].to_quaternion().inverted()@self.calibration[semantic]).normalized()
                # Intermediate source joints may animate between mapped
                # semantics. Consume their evaluated child-head direction,
                # keeping the REST-derived roll as the twist reference.
                if semantic in self.joint_calibration:
                    child=self.joint_calibration[semantic]['child']
                    local_child=self.target_rest[semantic].to_quaternion().inverted()@(self.target_rest[child].translation-self.target_rest[semantic].translation)
                    expected_child=sample[child].translation-sample[semantic].translation
                    orientation=(orientation@local_child).rotation_difference(expected_child)@orientation
                desired_rotations[semantic]=orientation
                desired=(world_rotation.inverted()@orientation).to_matrix().to_4x4();desired.translation=natural.translation
            else:desired=natural.copy()
            if name==record['root']:
                delta=(sample['hips'].translation-self.origin)*self.scale
                if self.motion=='IN_PLACE':delta.x=delta.y=0.0
                desired.translation=inverse@(self.root_rest.translation+delta)
            basis=bone.convert_local_to_pose(desired,bone.matrix_local,invert=True,**kwargs)
            location,rotation,scale=basis.decompose()
            self.max_basis_scale_error=max(self.max_basis_scale_error,max(abs(v-1) for v in scale))
            require(max(abs(v-1) for v in scale)<1e-4,'局部旋转不能以单位尺度表达：'+name)
            if name==record['root']:pb.location=location
            else:
                self.max_nonroot_basis_location=max(self.max_nonroot_basis_location,location.length)
                require(location.length<1e-4,'非根位移不能保持目标骨长：'+name)
            if pb.rotation_mode=='QUATERNION':
                if name in self.previous and rotation.dot(self.previous[name])<0:rotation.negate()
                pb.rotation_quaternion=rotation;self.previous[name]=rotation.copy()
            else:
                rotation=rotation.to_euler(pb.rotation_mode,self.previous[name]) if name in self.previous else rotation.to_euler(pb.rotation_mode)
                pb.rotation_euler=rotation;self.previous[name]=rotation.copy()
            solved[name]=bone.convert_local_to_pose(pb.matrix_basis,bone.matrix_local,**kwargs)
        evaluated=pose._update(target);errors=[]
        for semantic,orientation in desired_rotations.items():
            actual=(evaluated.matrix_world@evaluated.pose.bones[self.mapping[semantic]].matrix).to_quaternion()
            delta=actual.rotation_difference(orientation);angle=math.degrees(abs(delta.angle));angle=min(angle,360-angle)
            row={'semantic':semantic,'rotation_degrees':angle}
            if semantic in self.joint_calibration:
                child=self.joint_calibration[semantic]['child']
                expected=sample[child].translation-sample[semantic].translation
                actual_direction=(evaluated.matrix_world@evaluated.pose.bones[self.mapping[child]].head)-(evaluated.matrix_world@evaluated.pose.bones[self.mapping[semantic]].head)
                direction_angle=math.degrees(actual_direction.angle(expected))
                row['joint_direction_degrees']=direction_angle;self.joint_residual_max=max(self.joint_residual_max,direction_angle)
            errors.append(row)
        return errors
    def report_fields(self):
        return {'adapter':KIND,'target_policy':self.record,'target_mode':'GENERIC_FK','controller_animation':False,
            'fallback_acknowledged':True,'finger_orientation_reference':None,'finger_input_allocation':None,
            'spine_distribution':'explicit mapped target rotations; unmapped ancestors have declared identity rotation',
            'root_translation_reference':'CLIP_START','motion_scale_basis':'MEAN_WORLD_LEG_LENGTH',
            'orientation_reference':'REST_ACTUAL_CHILD_JOINTS_WITH_REPORTED_GAPS','body_frame_alignment':list(self.global_alignment),
            'rest_joint_calibration':self.joint_calibration,'joint_direction_residual_max_degrees':self.joint_residual_max,
            'skipped_joint_references':self.skipped_joint_references,
            'pose_joint_reference':'evaluated semantic child-head direction; REST-derived roll retained, no extra correction curves',
            'nonroot_translation_written':False,'target_scale_written':False,
            'max_nonroot_basis_location':self.max_nonroot_basis_location,'max_basis_scale_error':self.max_basis_scale_error,
            'limitations':['普通FK目标骨骼Action，不是原控制器动画','手指/表情/视线/接触修正未转换',
                '未映射非父链骨保留其当前作者输入','原骨架限制不解除；隔离候选不代表原控制器或原Mesh效果']}

def convert(source,target,*,source_mapping,target_mapping,source_fps=None,start=None,end=None,output_start=1,
            motion='PRESERVE',sample_step=1,acknowledged=False,action_name=None,progress=None,cancelled=None):
    cfg=policy(target,target_mapping,acknowledged=acknowledged)
    require(set(target_mapping)<=set(source_mapping['bones']),'目标映射语义缺少源输入')
    clip=sources.active_clip(source,source_fps)
    result=rt.convert_resolved(source,target,mapped=source_mapping,clip=clip,start=start,end=end,output_start=output_start,
        motion=motion,include_fingers=False,sample_step=sample_step,action_name=action_name,progress=progress,
        cancelled=cancelled,target_policy=cfg)
    result['action'][META]=json.dumps(cfg,ensure_ascii=False)
    return result
