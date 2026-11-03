"""Compile actual native rotation modes against the changed full-tree rest."""
from copy import deepcopy
from mathutils import Vector
from .contract import need

def compile_roles(profile,rig,original,bone_name):
    result=deepcopy(profile);evidence=[]
    old=original.bones[bone_name].matrix_local.to_3x3()
    new=rig.data.bones[bone_name].matrix_local.to_3x3()
    actual_mode=rig.pose.bones[bone_name].rotation_mode
    for role in result['roles']:
        if role['bone']!=bone_name:continue
        need(role['rotation_mode']==actual_mode,'ROLE_MODE_DRIFT',role['role_id'])
        original_axis=Vector(role['axis'])
        if role['kind']=='quaternion_delta':
            intended=old@original_axis
            compiled=(new.inverted()@intended).normalized()
            policy='preserve_declared_armature_axis_in_new_local_basis'
            role['axis']=list(compiled)
        else:
            need(role['kind']=='euler_delta' and actual_mode in ('XYZ','XZY','YXZ','YZX','ZXY','ZYX'),'ROLE_PRIMITIVE_UNSUPPORTED',role['role_id'])
            # Euler primitives author native indexed channels. Retain that
            # explicit primitive and declare its NEW rest-local axis semantics;
            # do not pretend arbitrary rotated axes fit an Euler channel or
            # silently change rotation mode under preserved source Actions.
            compiled=Vector(tuple(1. if i==role['index'] else 0. for i in range(3)))
            role['axis']=list(compiled);intended=new@compiled
            policy='new_rest_local_indexed_euler_axis_no_old_motion_equivalence'
        role['label']=role['role_id']+'（新静置姿态局部轴）'
        evidence.append({'role':role['role_id'],'kind':role['kind'],'rotation_mode':actual_mode,'index':role['index'],
            'old_local_axis':list(original_axis),'compiled_local_axis':list(compiled),
            'old_armature_axis':list(old@original_axis),'new_intended_armature_axis':list(intended),
            'actual_compiled_armature_axis':list(new@compiled),'policy':policy})
    need(bool(evidence),'ROLE_NOT_FOUND',bone_name)
    return result,evidence
