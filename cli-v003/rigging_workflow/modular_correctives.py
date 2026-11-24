# SPDX-License-Identifier: GPL-3.0-or-later
"""Small optional shoulder/hip helpers, independently authored from C94 study.

C94 supplies design evidence (separate local deforms and mechanisms), not
private bone data or its dense constraint graph. Helpers consume bounded local
primary mass through helper_weights instead of becoming global heat sources.
"""
import copy
import bpy
from mathutils import Vector


def install(rig, reference, metadata):
    if (bpy.context.mode != 'OBJECT' or rig.type != 'ARMATURE' or rig.library
            or metadata.get('schema') != 'rigging.modular.generated.v1'):
        raise ValueError('辅助模块仅安装到本次明确的新模块化骨架')
    if metadata.get('auxiliary_specs'):
        raise ValueError('辅助模块已存在；保留当前骨架并重新生成新候选')
    definitions = []
    for region, limb_root, torso in [('shoulder', 'upper_arm', 'spine.003'), ('hip', 'thigh', 'spine')]:
        for side in ('L', 'R'):
            limb_name, torso_name = 'DEF-' + limb_root + '.' + side, 'DEF-' + torso
            if limb_name not in rig.data.bones or torso_name not in rig.data.bones:
                continue
            limb, trunk = rig.data.bones[limb_name], rig.data.bones[torso_name]
            center = limb.head_local.lerp(trunk.head_local, .28 if region == 'shoulder' else .25)
            axis = (limb.tail_local - limb.head_local).normalized()
            length = max(limb.length * .18, .01)
            radius = max(limb.length * (.5 if region == 'shoulder' else .28), length * 1.1)
            # A short quadruped femur does not measure the surrounding hip
            # thickness. Keep the local donor domain tied to the hip spacing.
            other = rig.data.bones.get('DEF-' + limb_root + ('.R' if side == 'L' else '.L'))
            if region == 'hip' and other:
                radius = max(radius, (limb.head_local-other.head_local).length * .5)
            definitions.append({'name': 'DEF-CORR-' + region + '.' + side,
                'head': list(center), 'tail': list(center + axis * length),
                'parent': torso_name, 'source': limb_name, 'donors': [torso_name, limb_name],
                'radius': radius,
                'radius_basis': 'proximal_limb_and_half_bilateral_hip_spacing' if region == 'hip' and other else 'proximal_limb_scale',
                'strength': .3, 'side': side, 'region': region})
    if not definitions:
        raise ValueError('当前组合没有可安装肩/髋辅助骨的有效肢体与躯干端口')
    old_active, selected = bpy.context.view_layer.objects.active, list(bpy.context.selected_objects)
    made = []
    try:
        for obj in selected: obj.select_set(False)
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode='EDIT')
        for row in definitions:
            if row['name'] in rig.data.edit_bones:
                raise ValueError('已有同名辅助骨，拒绝覆盖：' + row['name'])
            bone = rig.data.edit_bones.new(row['name']); made.append(row['name'])
            bone.head, bone.tail = row['head'], row['tail']
            bone.parent = rig.data.edit_bones[row['parent']]
            bone.use_connect = False
            # LOCAL rotation is meaningful only with the same rest axes as
            # its source. Matching only Y direction leaves an arbitrary roll.
            bone.align_roll(rig.data.edit_bones[row['source']].matrix.to_3x3().col[2])
        bpy.ops.object.mode_set(mode='OBJECT')
        collection = rig.data.collections.get('局部修正形变') or rig.data.collections.new('局部修正形变')
        for row in definitions:
            bone = rig.data.bones[row['name']]
            bone.use_deform = True
            collection.assign(bone)
            constraint = rig.pose.bones[row['name']].constraints.new('COPY_ROTATION')
            constraint.name = '局部关节旋转分担'
            constraint.target, constraint.subtarget = rig, row['source']
            constraint.owner_space = constraint.target_space = 'LOCAL'
            constraint.mix_mode = 'OFFSET'
            constraint.influence = .5
        result_metadata = copy.deepcopy(metadata)
        result_metadata['primary_deforms'] = list(metadata['deforms'])
        result_metadata['auxiliary_specs'] = definitions
        for row in definitions:
            result_metadata['deforms'].append(row['name'])
            result_metadata['mechanism_mapping'][row['name']] = row['source']
        bpy.context.view_layer.update()
        return result_metadata
    except Exception:
        if rig.mode != 'EDIT': bpy.ops.object.mode_set(mode='EDIT')
        for name in made:
            bone = rig.data.edit_bones.get(name)
            if bone: rig.data.edit_bones.remove(bone)
        bpy.ops.object.mode_set(mode='OBJECT')
        raise
    finally:
        if rig.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(bpy.context.selected_objects): obj.select_set(False)
        for obj in selected:
            if obj.name in bpy.context.view_layer.objects: obj.select_set(True)
        bpy.context.view_layer.objects.active = old_active


def weight_specs(rig, metadata):
    world = rig.matrix_world
    scale = max(world.to_3x3().col[i].length for i in range(3))
    rows = []
    for spec in metadata.get('auxiliary_specs', []):
        row = copy.deepcopy(spec)
        bone = rig.data.bones[row['name']]
        root = row['source'][:-2]
        left, right_bone = rig.data.bones.get(root + '.L'), rig.data.bones.get(root + '.R')
        if left and right_bone:
            middle = (left.head_local + right_bone.head_local) * .5
            lateral = world.to_3x3() @ (left.head_local - right_bone.head_local)
            if lateral.length <= 1e-10: raise ValueError('左右关节重合，无法确定辅助骨中线')
            lateral.normalize()
        else:
            middle = rig.data.bones[row['parent']].head_local
            lateral = world.to_3x3().col[0].normalized()
        row['head'], row['tail'] = list(world @ bone.head_local), list(world @ bone.tail_local)
        row['radius'] *= scale
        row['support_plane'] = {'origin': list(world @ middle),
            'normal': list(lateral if row['side'] == 'L' else -lateral),
            'transition': row['radius'] * .2}
        rows.append(row)
    return rows
