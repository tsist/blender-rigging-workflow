# SPDX-License-Identifier: GPL-3.0-or-later
"""Allocate missing finger Z capacity through the unchanged native C94 graph."""
import math
from mathutils import Euler
from . import pose_switching as pose,pose_switching_leg as leg

def _clamp(value,low,high):return min(high,max(low,value))

def _limits(pb):
    constraints=[c for c in pb.constraints if c.type=='LIMIT_ROTATION']
    if len(constraints)!=1:raise ValueError('手指原限制需单独适配：'+pb.name)
    c=constraints[0]
    if c.owner_space!='LOCAL' or c.mute or abs(c.influence-1)>1e-6 or c.euler_order not in ('AUTO','XYZ'):
        raise ValueError('手指限制空间或混合已改变：'+pb.name)
    return [(getattr(c,'min_'+axis),getattr(c,'max_'+axis)) if getattr(c,'use_limit_'+axis) else (-math.pi,math.pi)
            for axis in 'xyz']

def _curve(pb):
    c=next(c for c in pb.constraints if c.type=='TRANSFORM')
    if (c.map_to_z_from!='Z' or c.use_motion_extrapolate or c.to_min_x_rot!=0 or c.to_max_x_rot!=0 or
        c.to_min_y_rot!=0 or c.to_max_y_rot!=0 or c.from_max_z_scale<=c.from_min_z_scale):
        raise ValueError('手指卷曲映射需单独适配：'+pb.name)
    return c.from_min_z_scale,c.from_max_z_scale,c.to_min_z_rot,c.to_max_z_rot

def _offset(curve,scale):
    low,high,a,b=curve
    return a+(b-a)*_clamp((scale-low)/(high-low),0,1)

def fit_scale(goals,ranges,curves,reference=1.0):
    """Exact piecewise quadratic Z-limit fit, then nearest neutral minimizer."""
    low=min(c[0] for c in curves);high=max(c[1] for c in curves)
    knots={low,high,_clamp(reference,low,high)}
    for goal,(lo,hi),curve in zip(goals,ranges,curves):
        x0,x1,y0,y1=curve;knots.update((x0,x1))
        if abs(y1-y0)>1e-12:
            for bound in (lo,hi):
                value=x0+(goal-bound-y0)*(x1-x0)/(y1-y0)
                if x0<value<x1:knots.add(value)
    knots=sorted(value for value in knots if low<=value<=high);candidates=list(knots)
    for left,right in zip(knots,knots[1:]):
        mid=(left+right)*.5;aa=ab=0.0
        for goal,(lo,hi),curve in zip(goals,ranges,curves):
            value=goal-_offset(curve,mid);bound=lo if value<lo else hi if value>hi else None
            if bound is None:continue
            x0,x1,y0,y1=curve
            slope=(y1-y0)/(x1-x0) if x0<mid<x1 else 0.0
            a=-slope;b=value+slope*mid-bound
            aa+=a*a;ab+=a*b
        candidates.append(_clamp(-ab/aa,left,right) if aa>1e-12 else _clamp(reference,left,right))
    def cost(scale):
        return sum((goal-_offset(curve,scale)-_clamp(goal-_offset(curve,scale),lo,hi))**2
                   for goal,(lo,hi),curve in zip(goals,ranges,curves))
    best=min(cost(value) for value in candidates)
    selected=min((value for value in candidates if cost(value)<=best+1e-12),key=lambda value:abs(value-reference))
    return selected,best

class NativeZInputs:
    def __init__(self,target,chains,fingers):
        self.target=target;self.chains=[];self.inputs={};self.diagnostics=[]
        for chain in chains:
            finger,side=chain.split('.')
            if finger=='thumb':continue
            word=fingers[finger];names=[word+str(i)+'.'+side for i in (1,2,3)]
            master=target.pose.bones[word+'.'+side];pbs=[target.pose.bones[name] for name in names]
            for pb in [master,*pbs]:
                if pb.rotation_mode not in ('QUATERNION','XYZ'):
                    raise ValueError('手指组合输入仅验证XYZ/四元数原模式：'+pb.name)
            raw=[_limits(pb) for pb in pbs];ml=_limits(master);curves=[_curve(pb) for pb in pbs]
            if tuple(c.type for c in master.constraints)!=('LIMIT_SCALE','LIMIT_ROTATION'):
                raise ValueError('手指主控约束需单独适配：'+master.name)
            sc=master.constraints[0]
            if (sc.mute or abs(sc.influence-1)>1e-6 or sc.owner_space!='LOCAL' or not sc.use_min_z or sc.use_max_z or
                sc.min_z>min(c[0] for c in curves)+1e-6):
                raise ValueError('手指主控缩放限制需单独适配：'+master.name)
            copy=next(c for c in pbs[0].constraints if c.type=='COPY_ROTATION')
            if not (copy.use_x and copy.use_y and copy.use_z and copy.euler_order in ('AUTO','XYZ') and
                    not copy.invert_x and not copy.invert_y and not copy.invert_z):
                raise ValueError('手指主控旋转轴需单独适配：'+master.name)
            self.chains.append((chain,master,names,raw,ml,curves))

    def reset(self,set_rotation):
        self.inputs={};self.diagnostics=[]
        for _,master,_,_,_,_ in self.chains:
            set_rotation(master,Euler((0,0,0),'XYZ').to_quaternion());master.scale=(1,1,1)

    def prepare(self,desired,er,set_rotation):
        for chain,master,names,raw,ml,curves in self.chains:
            goals=[]
            for i,name in enumerate(names):
                pb=self.target.pose.bones[name]
                parent=desired[names[i-1]] if i else er.pose.bones[pb.parent.name].matrix
                matrix=pb.bone.convert_local_to_pose(desired[name],pb.bone.matrix_local,parent_matrix=parent,
                    parent_matrix_local=pb.parent.bone.matrix_local,invert=True)
                goals.append(matrix.to_quaternion().to_euler('XYZ'))
            ranges=[row[2] for row in raw]
            if all(lo-1e-7<=g.z-_offset(curve,1)<=hi+1e-7 for g,(lo,hi),curve in zip(goals,ranges,curves)):
                continue
            ranges[0]=(ranges[0][0]+ml[2][0],ranges[0][1]+ml[2][1])
            scale,cost=fit_scale([g.z for g in goals],ranges,curves)
            goal=goals[0].z-_offset(curves[0],scale)
            master_z=_clamp(goal-_clamp(goal,*raw[0][2]),*ml[2])
            set_rotation(master,Euler((0,0,master_z),'XYZ').to_quaternion());master.scale=(scale,scale,scale)
            for i,name in enumerate(names):self.inputs[name]=(_offset(curves[i],scale),master_z if i==0 else 0)
            self.diagnostics.append({'chain':chain,'master_scale':scale,'master_z_degrees':math.degrees(master_z),
                                     'z_limit_squared_error':cost})

    def rotation(self,name,desired_local):
        offset,master_z=self.inputs[name]
        value=desired_local.to_euler('XYZ');value.z-=offset+master_z
        return value.to_quaternion()
