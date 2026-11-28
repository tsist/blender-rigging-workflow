# SPDX-License-Identifier: GPL-3.0-or-later
"""C94 body adapter. Native limits/follow/constraints stay active and editable."""
import math
import bpy
from mathutils import Matrix,Vector
from . import pose_switching as pose, pose_switching_leg as leg
from .retarget_observed import inverse_rotation
from .retarget_finger_inputs import NativeZInputs

GROUPS=(('hips',),('spine',),('chest',),('neck',),('head',),('upper_arm','thigh'),('forearm','knee'),
        ('hand','shin'),('foot',),('toes',),
        ('thumb1','index1','middle1','ring1','pinky1'),('thumb2','index2','middle2','ring2','pinky2'),
        ('thumb3','index3','middle3','ring3','pinky3'))
MODES=('左臂FK-IK','右臂FK-IK','左腿FK-IK','右腿FK-IK')
FINGERS={'thumb':'大拇指','index':'食指','middle':'中指','ring':'无名指','pinky':'小拇指'}

def _palm_frame(wrist,index,middle,pinky):
    """Signed anatomical palm frame, not the arbitrary FBX bone roll."""
    y=(middle-wrist).normalized();across=index-pinky
    x=across-y*across.dot(y)
    if x.length<1e-7 or y.length<1e-7:raise ValueError('手掌参考关节重合或共线，需要显式校准')
    x.normalize();z=x.cross(y).normalized();x=y.cross(z).normalized()
    return Matrix((x,y,z)).transposed().to_4x4()

def _world_supported(obj,*,upright=False):
    matrix=obj.matrix_world;cols=[matrix.to_3x3().col[i] for i in range(3)];lengths=[c.length for c in cols]
    if (not all(math.isfinite(v) for row in matrix for v in row) or min(lengths)<=0 or
        max(lengths)-min(lengths)>max(lengths)*1e-4 or matrix.determinant()<=0 or
        any(abs(cols[i].dot(cols[j]))>lengths[i]*lengths[j]*1e-5 for i in range(3) for j in range(i))):
        raise ValueError('非均匀、负缩放或剪切世界变换需单独适配，原变换保持')
    if upright and (cols[2].normalized()-Vector((0,0,1))).length>1e-5:
        raise ValueError('当前根运动适配要求目标对象保持世界Z向上；倾斜目标需单独校准')

def rows(mapping,include_fingers=True):
    result=[('hips','hips','盆骨控制器',None),('spine','spine','腰椎控制器',None),('chest','chest','胸腔控制器',None),
            ('neck','neck','颈椎控制器',None),('head','head','头颅控制器',None)]
    for side in ('L','R'):
        result += [(semantic+'.'+side,src+'.'+side,dst+'.'+side,child+'.'+side if child else None)
                   for semantic,src,dst,child in (
                       ('upper_arm','upper_arm','大臂FK','forearm'),('forearm','forearm','小臂FK','hand'),
                       ('hand','hand','手掌FK',None),('thigh','thigh','大腿FK','shin'),
                       ('knee','shin','膝盖FK','foot'),('shin','shin','小腿FK','foot'),
                       ('foot','foot','脚掌FK',None),('toes','toes','脚趾',None))]
        hand_child=next((f+str(1)+'.'+side for f in ('middle','index') if f+str(1)+'.'+side in mapping),None)
        if hand_child:
            result=[(a,b,c,hand_child if a=='hand.'+side else d) for a,b,c,d in result]
        if include_fingers:
            for finger,word in FINGERS.items():
                if not all(finger+str(i)+'.'+side in mapping for i in range(1,5)):continue
                targets=(word,word+'1',word+'2') if finger=='thumb' else tuple(word+str(i) for i in range(1,4))
                result.extend((finger+str(i)+'.'+side,finger+str(i)+'.'+side,dst+'.'+side,finger+str(i+1)+'.'+side)
                              for i,dst in enumerate(targets,1))
    return [r for r in result if r[1] in mapping and (not r[3] or r[3] in mapping)]

class Adapter:
    def __init__(self,source,target,mapping,*,motion='PRESERVE',scale_motion=True,include_fingers=True,
                 world_motion_scale=None,source_hip=None,orientation_reference='REST'):
        pose._metadata(target)
        _world_supported(source);_world_supported(target,upright=True)
        if motion not in ('PRESERVE','IN_PLACE'): raise ValueError('根运动模式无效')
        if orientation_reference!='REST': raise ValueError('当前适配器只支持REST朝向参考')
        self.source,self.target,self.mapping=source,target,mapping
        self.rows=rows(mapping,include_fingers); self.motion=motion
        self.root_rotation = "HEADING"
        self.finger_chains=[finger+'.'+side for side in ('L','R') for finger in FINGERS
                            if any(r[0]==finger+'1.'+side for r in self.rows)]
        masters=[FINGERS[f.split('.')[0]]+'.'+f.split('.')[1] for f in self.finger_chains if not f.startswith('thumb.')]
        palms=['掌骨'+str(i)+'.'+side for side in ('L','R') if any(f.endswith('.'+side) for f in self.finger_chains) for i in range(1,6)]
        self.names=['根骨骼','躯干','骶骨控制器',*masters,*palms,*[r[2] for r in self.rows]]
        if not any(r[0]=='spine' for r in self.rows):self.names.append('腰椎控制器')
        self.observed={r[2]:r[2] for r in self.rows}
        self.observed['胸腔控制器']='拉伸-胸腔2'
        for side in ('L','R'):
            if 'thumb.'+side in self.finger_chains:self.observed['大拇指.'+side]='掌骨1.'+side
        if len(self.names)!=len(set(self.names)): raise ValueError('目标控制写域重复')
        if any(n not in target.pose.bones for n in self.names+list(self.observed.values())): raise ValueError('目标缺少C94身体/分段手指原生控件')
        for n in self.names:
            if target.pose.bones[n].rotation_mode=='AXIS_ANGLE': raise ValueError('当前控件轴角模式尚未验证：'+n)
        settings=target.pose.bones['骨架设置']
        for key in MODES: settings[key]=0.0
        for n in self.names: target.pose.bones[n].matrix_basis=Matrix.Identity(4)
        er=pose._update(target)
        for chain in self.finger_chains:
            finger,side=chain.split('.');word=FINGERS[finger]
            segments=(word+'1',word+'2') if finger=='thumb' else tuple(word+str(i) for i in range(1,4))
            for i,segment in enumerate(segments):
                p=target.pose.bones[segment+'.'+side]
                expected=('LIMIT_ROTATION','TRANSFORM') if finger=='thumb' else ('LIMIT_ROTATION','COPY_ROTATION','TRANSFORM') if i==0 else ('LIMIT_ROTATION','TRANSFORM')
                if i==len(segments)-1:expected+=('IK',)
                if tuple(c.type for c in p.constraints)!=expected:raise ValueError('目标手指约束图需单独适配：'+p.name)
                for c in p.constraints:
                    if c.type in ('TRANSFORM','COPY_ROTATION') and not (c.target==target and c.subtarget==word+'.'+side and
                        c.owner_space==c.target_space=='LOCAL' and not c.mute and abs(c.influence-1)<1e-6):
                        raise ValueError('目标手指主控关系需单独适配：'+p.name)
                    if c.type=='COPY_ROTATION' and c.mix_mode!='ADD':raise ValueError('手指旋转混合已改变：'+p.name)
                    if c.type=='TRANSFORM' and (c.map_from!='SCALE' or c.map_to!='ROTATION' or c.mix_mode_rot!='ADD'):
                        raise ValueError('手指卷曲机制已改变：'+p.name)
                    if c.type=='IK' and (c.chain_count!=len(segments) or abs(er.pose.bones[p.name].constraints[c.name].influence)>1e-6):
                        raise ValueError('手指IK未处于已验FK链状态：'+p.name)
        self.neutral={n:er.pose.bones[n].matrix.copy() for n in self.names}
        self.observation_neutral={dst:er.pose.bones[n].matrix.copy() for dst,n in self.observed.items()}
        self.finger_offsets={dst:leg._basis(target.pose.bones[self.observed[dst]],self.observation_neutral[dst],er).to_quaternion()
                             for semantic,_,dst,_ in self.rows if any(semantic.startswith(f+str(i)+'.') for f in FINGERS for i in range(1,4))}
        self.rest={semantic:source.matrix_world @ source.data.bones[name].matrix_local for semantic,name in mapping.items()}
        self.align=target.matrix_world.to_quaternion().to_matrix().to_4x4().inverted()
        self.source_hip=(source_hip.copy() if source_hip is not None
                         else self.rest['hips'].translation.copy())
        target_hip=(target.matrix_world@self.neutral['盆骨控制器']).translation
        unit=target.matrix_world.to_3x3().col[0].length
        if world_motion_scale is not None:
            # Explicit world leg-length ratio.  The target object unit is
            # applied exactly once below, never in the caller as well.
            self.scale=float(world_motion_scale)
        elif scale_motion:
            # Mixamo default only.  Calibrated sources pass motion_scale
            # explicitly because a REST root Z of zero is not a usable origin.
            self.scale=(target_hip.z-target.matrix_world.translation.z)/(
                self.rest['hips'].translation.z-source.matrix_world.translation.z)
        else:
            self.scale=1.0
        if not math.isfinite(self.scale) or self.scale<=0: raise ValueError('无法从当前直立静置骨架计算位移比例')
        self.scale/=unit
        self.calibration={}
        palm_align={}
        for side in ('L','R'):
            semantics=['hand.'+side,*[f+'1.'+side for f in ('index','middle','pinky')]]
            targets=['手掌FK.'+side,'食指1.'+side,'中指1.'+side,'小拇指1.'+side]
            if all(s in self.rest for s in semantics) and all(n in er.pose.bones for n in targets):
                source_frame=_palm_frame(*[self.align.to_3x3()@self.rest[s].translation for s in semantics])
                target_frame=_palm_frame(*[er.pose.bones[n].matrix.translation for n in targets])
                palm_align[side]=source_frame@target_frame.inverted()
        self.palm_calibration_sides=sorted(palm_align)
        if any(chain.rsplit('.',1)[-1] not in palm_align for chain in self.finger_chains):
            raise ValueError('手指转换需要完整的手腕/食指/中指/小指参考来确认手掌方向')
        for semantic,src,dst,child in self.rows:
            neutral=self.observation_neutral[dst]
            side=semantic.rsplit('.',1)[-1]
            is_hand=semantic.startswith('hand.') or any(semantic.startswith(f+str(i)+'.') for f in FINGERS for i in range(1,4))
            aligned=palm_align[side]@neutral if is_hand and side in palm_align else neutral
            swing=Matrix.Identity(4)
            # Palm alignment establishes handedness/roll. Each finger must also
            # match its actual source child-joint direction: native neutral curl
            # and spread are not the source rest pose. Imported FBX bone tails
            # are artificial, so only mapped child joints are used here.
            if child and not (semantic.startswith('hand.') and side in palm_align):
                source_dir=self.align.to_3x3()@(self.rest[child].translation-self.rest[src].translation)
                if source_dir.length<1e-8: raise ValueError('源语义关节重合：'+src)
                swing=aligned.to_3x3().col[1].rotation_difference(source_dir).to_matrix().to_4x4()
            self.calibration[dst]=swing@aligned
            self.calibration[dst].translation=neutral.translation
        self.previous={}
        self.inverse_diagnostics={}
        self.finger_inputs=NativeZInputs(target,self.finger_chains,FINGERS)

    def solve(self,source_pose):
        target=self.target
        self.finger_inputs.reset(self.set_rotation)
        desired={dst:self.align@source_pose[src]@self.rest[src].inverted()@self.align.inverted()@self.calibration[dst]
                 for _,src,dst,_ in self.rows}
        delta=source_pose['hips']@self.rest['hips'].inverted()
        neutral_root = self.neutral['根骨骼']
        if self.root_rotation == "FULL":
            align = self.align.to_3x3()
            rotation = align @ delta.to_quaternion().to_matrix() @ align.inverted()
            root = rotation.to_4x4() @ neutral_root
            # The source rotation is about the pelvis. Compensate for the
            # target root sitting at the feet before adding source travel.
            pelvis_offset = self.neutral['盆骨控制器'].translation - neutral_root.translation
            root.translation = neutral_root.translation + pelvis_offset - rotation @ pelvis_offset
        else:
            facing = self.align.to_3x3() @ (delta.to_3x3() @ Vector((0, -1, 0)))
            yaw = math.atan2(facing.x, -facing.y)
            root = Matrix.Rotation(yaw, 4, 'Z') @ neutral_root
            root.translation = neutral_root.translation
        displacement=source_pose['hips'].translation-self.source_hip
        if self.motion=='IN_PLACE': displacement.x=displacement.y=0.0
        displacement=self.align.to_3x3()@displacement*self.scale
        root.translation+=displacement
        target.pose.bones['根骨骼'].matrix=root
        for group in GROUPS:
            er=pose._update(target)
            if self.finger_inputs.chains and group==GROUPS[10]:
                self.finger_inputs.prepare(desired,er,self.set_rotation)
                er=pose._update(target)
            for semantic,_,dst,_ in self.rows:
                if semantic.split('.')[0] not in group: continue
                pb=target.pose.bones[dst]
                observation=self.observed[dst]
                aim=desired[dst].copy(); aim.translation=er.pose.bones[observation].matrix.translation
                if dst=='胸腔控制器':
                    q,diagnostic=inverse_rotation(target,dst,observation,aim,pose._update)
                    self.inverse_diagnostics[dst]=diagnostic
                else:
                    q=leg._basis(target.pose.bones[observation],aim,er).to_quaternion()
                    if dst in self.finger_inputs.inputs:q=self.finger_inputs.rotation(dst,q)
                    elif dst in self.finger_offsets:q=q@self.finger_offsets[dst].inverted()
                self.set_rotation(pb,q)
        er=pose._update(target)
        errors=[]
        for semantic,_,dst,_ in self.rows:
            actual=er.pose.bones[self.observed[dst]].matrix
            angle=math.degrees(actual.to_quaternion().rotation_difference(desired[dst].to_quaternion()).angle)
            # Bone direction and full orientation answer different questions:
            # local-Y twist leaves the bone direction unchanged.
            direction=math.degrees(actual.to_3x3().col[1].angle(desired[dst].to_3x3().col[1]))
            errors.append({'semantic':semantic,'control':dst,'observed':self.observed[dst],
                           'rotation_degrees':min(angle,360-angle),'direction_degrees':direction})
        # Keep native Euler modes. The chosen representation follows the prior
        # output frame, rather than introducing an arbitrary +/-360 jump.
        root_pb=target.pose.bones['根骨骼']
        self.set_rotation(root_pb,root_pb.rotation_quaternion.copy() if root_pb.rotation_mode=='QUATERNION' else root_pb.rotation_euler.to_quaternion())
        return errors

    def set_rotation(self,pb,q):
        previous=self.previous.get(pb.name)
        if pb.rotation_mode=='QUATERNION':
            if previous is not None and q.dot(previous)<0: q.negate()
            pb.rotation_quaternion=q
            self.previous[pb.name]=q.copy()
        else:
            e=q.to_euler(pb.rotation_mode,previous if previous is not None else pb.rotation_euler)
            pb.rotation_euler=e
            self.previous[pb.name]=e.copy()
