"""C94 anatomical position transport, retaining evaluated source translations.

This changes calibrated translations only. Native rotations, scales, constraints,
parents and approved rest frames stay intact. Relations describe C94 semantics;
they are never inferred from geometric proximity.
"""
import bpy
from mathutils import Matrix

POSITION = '绑定位置-'


def anatomical_relations():
    """Return canonical child -> anchor names, including authored digit links."""
    relations = {}
    for side in ('.L', '.R'):
        def add(child, anchor):
            relations['形变-' + child + side] = '形变-' + anchor + side

        relations['形变-肩膀' + side] = '形变-胸腔2'
        add('扭转-大臂4', '肩膀')
        add('扭转-大腿4', '骨盆')
        for limb in ('大臂', '大腿'):
            for index in (3, 2, 1):
                add(f'扭转-{limb}{index}', f'扭转-{limb}{index + 1}')
        add('扭转-小臂5', '扭转-大臂1')
        for index in (4, 3, 2, 1):
            add(f'扭转-小臂{index}', f'扭转-小臂{index + 1}')
        add('手掌', '扭转-小臂1')
        add('膝盖', '扭转-大腿1')
        add('扭转-小腿4', '膝盖')
        for index in (3, 2, 1):
            add(f'扭转-小腿{index}', f'扭转-小腿{index + 1}')
        add('脚掌', '扭转-小腿1')
        add('跖骨', '脚掌')
        add('脚趾', '跖骨')
        for index, finger in enumerate(('大拇指', '食指', '中指', '无名指', '小拇指'), 1):
            add(f'掌骨{index}', '掌骨2' if index == 1 else '手掌')
            add(f'{finger}1', f'掌骨{index}')
            add(f'{finger}2', f'{finger}1')
            if index > 1:
                add(f'{finger}3', f'{finger}2')
        for index, toe in enumerate(('大脚趾', '第二趾', '第三趾', '第四趾', '第五趾'), 1):
            add(f'跖骨{index}', '跖骨')
            # Native toe connection is conditional. Keep the source evaluation
            # relative to the toe master, not a fixed metatarsal-tail offset.
            add(f'{toe}1', '脚趾')
            add(f'{toe}2', f'{toe}1')
            if index > 1:
                add(f'{toe}3', f'{toe}2')
    return relations


def build(rig, desired, outputs, mechanisms, calibrated_prefix, collection):
    """Install native CUSTOM/LOCAL position residuals on unconnected outputs.

    H(t) = Pout(t) @ (q0 + inverse(Psource(t)) @ Hsource(t) - r0).
    q0 retains the approved child head in the output anchor frame. r0 removes
    the bind residual; all subsequent source translation/constraint motion is
    retained. Existing connected outputs keep Blender's native shared joint.
    Source-local residual coordinates are transported in the output anchor's
    frame; this is retargeted motion, not world-vector identity. This is a
    private generation step: generation.generate owns failure cleanup.
    """
    relations = {child: anchor for child, anchor in anatomical_relations().items()
                 if child in outputs}
    dependencies = {}
    for child, anchor in relations.items():
        if anchor not in outputs:
            raise ValueError(f'C94位置关系缺少输出锚点：{child} → {anchor}')
        dependencies[child] = anchor
    # Validate this semantic DAG before creating anything. Native connected
    # children participate as dependencies even though they need no helper.
    for child in dependencies:
        seen = set()
        current = child
        while current in dependencies:
            if current in seen:
                raise ValueError('C94位置关系包含循环：' + child)
            seen.add(current)
            current = dependencies[current]
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    rows, connected = [], []
    for child, anchor in relations.items():
        output = rig.data.bones[outputs[child]]
        if output.use_connect:
            if not output.parent or output.parent.name != outputs[anchor]:
                raise ValueError('已连接输出与C94位置关系不一致：' + child)
            connected.append(outputs[child])
            continue
        name = POSITION + child
        if len(name.encode('utf-8')) > 63 or name in rig.data.bones:
            raise ValueError('位置机制骨名冲突或过长：' + child)
        parent0 = evaluated.pose.bones[outputs[anchor]].matrix.copy()
        source_parent0 = evaluated.pose.bones[mechanisms[anchor]].matrix.copy()
        if abs(parent0.determinant()) < 1e-9 or abs(source_parent0.determinant()) < 1e-9:
            raise ValueError('C94位置锚点绑定帧不可逆：' + anchor)
        q0 = parent0.inverted() @ desired[child]['matrix'].translation
        r0 = source_parent0.inverted() @ evaluated.pose.bones[mechanisms[child]].head
        rest = rig.data.bones[outputs[anchor]].matrix_local @ Matrix.Translation(q0)
        rows.append({'child': child, 'anchor': anchor, 'name': name,
                     'q0': q0, 'r0': r0, 'rest': rest})
    bpy.ops.object.mode_set(mode='EDIT')
    for row in rows:
        helper = rig.data.edit_bones.new(row['name'])
        helper.length = desired[row['child']]['length']
        helper.matrix = row['rest']
        helper.parent = rig.data.edit_bones[outputs[row['anchor']]]
        helper.use_deform = False
        helper.use_connect = False
        helper.inherit_scale = 'FULL'
        helper.use_inherit_rotation = True
        helper.use_local_location = True
    bpy.ops.object.mode_set(mode='OBJECT')
    for row in rows:
        helper = rig.pose.bones[row['name']]
        helper.location = -row['r0']
        helper.bone.hide_select = True
        collection.assign(helper.bone)
        residual = helper.constraints.new('COPY_LOCATION')
        residual.name = '源机制相对位置残差'
        residual.target = rig
        residual.subtarget = mechanisms[row['child']]
        residual.target_space = 'CUSTOM'
        residual.space_object = rig
        residual.space_subtarget = mechanisms[row['anchor']]
        residual.owner_space = 'LOCAL'
        residual.use_offset = True
        follow = rig.pose.bones[calibrated_prefix + row['child']].constraints.new('COPY_LOCATION')
        follow.name = '解剖关节位置传递'
        follow.target = rig
        follow.subtarget = row['name']
        follow.owner_space = follow.target_space = 'WORLD'
        follow.use_offset = False
    rig.update_tag(refresh={'OBJECT', 'DATA'})
    bpy.context.view_layer.update()
    return {'method': 'native_parent_relative_source_delta_v1',
            'relations': [{k: (list(v) if k in ('q0', 'r0') else v)
                           for k, v in row.items() if k != 'rest'} for row in rows],
            'native_connected': connected}
