# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded rotation-only DLS inverse of an existing evaluated controller graph.

Design reference only: Blender IK_QJacobian.cpp at ca56249b6affcc1b79dcde5a7a7feaf6dd630e32.
No upstream code or solver dependency copied. Existing constraints stay active.
"""
import math
from mathutils import Matrix,Vector,Quaternion

def rotation_vector(q):
    q=q.normalized()
    if q.w<0:q.negate()
    vector=Vector((q.x,q.y,q.z));length=vector.length
    return vector*(2*math.atan2(length,q.w)/length) if length>1e-12 else Vector((0,0,0))

def inverse_rotation(rig,control,observed,desired,update,*,iterations=6):
    pb=rig.pose.bones[control]
    def read_input():
        return pb.rotation_quaternion.copy() if pb.rotation_mode=='QUATERNION' else pb.rotation_euler.to_quaternion()
    def write(q):
        if pb.rotation_mode=='QUATERNION':pb.rotation_quaternion=q
        else:pb.rotation_euler=q.to_euler(pb.rotation_mode,pb.rotation_euler)
    def actual():return update(rig).pose.bones[observed].matrix.to_quaternion()
    goal=desired.to_quaternion();epsilon=0.001
    q=read_input();output=actual();count=0
    for count in range(iterations):
        residual=rotation_vector(goal@output.inverted())
        if residual.length<math.radians(0.03):break
        columns=[]
        for axis in (Vector((1,0,0)),Vector((0,1,0)),Vector((0,0,1))):
            write(q@Quaternion(axis,epsilon))
            columns.append(rotation_vector(actual()@output.inverted())/epsilon)
        write(q)
        jac=Matrix(columns).transposed();jt=jac.transposed()
        normal=jt@jac
        for i in range(3):normal[i][i]+=0.0001
        step=normal.inverted()@(jt@residual)
        if step.length<1e-7:break
        if step.length>0.35:step*=0.35/step.length
        proposal=q@Quaternion(step.normalized(),step.length)
        write(proposal);candidate=actual()
        if rotation_vector(goal@candidate.inverted()).length>=residual.length:
            proposal=q@Quaternion(step.normalized(),step.length*0.5)
            write(proposal);candidate=actual()
            if rotation_vector(goal@candidate.inverted()).length>=residual.length:
                write(q);output=actual();break
        q,output=proposal,candidate
    write(q)
    return q,{'iterations':count+1,'rotation_degrees':math.degrees(rotation_vector(goal@output.inverted()).length)}
