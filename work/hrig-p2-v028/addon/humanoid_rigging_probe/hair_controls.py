"""Opt-in v028 fixed-Aon control picker; importing does not register or edit.

Only selection, Object/Pose mode and explicitly journaled visibility leaves change.
No selector, transform, Action, physical setting, global preference or file writes.
"""
import bpy
from bpy.props import EnumProperty

from .hair_pair_isolation import BOUNDARIES, bone_names

ORIGINAL = '千早爱音-素体骨架'
BRANCHES = {
    'front': ('HRIG_front_hair', '千早爱音- 赋权前发网格', 245),
    'rear': ('HRIG_rear_hair', '千早爱音- 赋权后发网格', 305),
}
PRESETS = {
    ('front', 'FK'): '前发-02', ('front', 'IK'): '前发-01-IK目标',
    ('rear', 'FK'): '后发-02', ('rear', 'IK'): '后发-01-IK目标',
}
LAST_SELECTION_REPORT = None
_REGISTERED = []


def _need(value, message):
    if not value:
        raise ValueError(message)


def _local(obj, kind):
    _need(obj is not None and obj.type == kind, '缺少预期的本地' + kind + '对象')
    for item in (obj, obj.data):
        _need(item.library is None and item.override_library is None and item.is_editable,
              obj.name + '含链接、库覆盖或不可编辑数据')
    _need(obj.mode in {'OBJECT', 'POSE'}, obj.name + '处于不允许的模式')


def _writable(owner, field):
    prop = owner.bl_rna.properties.get(field)
    _need(prop is not None and prop.type == 'BOOLEAN' and not owner.is_property_readonly(field),
          '当前RNA未提供可写布尔属性：' + field)


def _visible_collection_path(layer, target):
    """One direct membership must have an unblocked complete layer ancestor path."""
    pending = [(layer.layer_collection, True)]
    visited = 0
    while pending:
        item, ancestors_visible = pending.pop()
        visited += 1
        _need(visited <= 16384, 'LayerCollection路径检查超过范围')
        collection = item.collection
        clear = (ancestors_visible and not item.exclude and not item.hide_viewport
                 and not collection.hide_viewport)
        if clear and collection.objects.get(target.name) == target:
            return True
        pending.extend((child, clear) for child in item.children)
    return False


def _preflight(context, part, control):
    """All domain checks and the exact visibility plan precede any setter."""
    _need(part in {'front', 'rear', 'settings'} and control in {'FK', 'IK'}, '未知控制预设')
    _need(context.window is not None and context.area is not None
          and context.area.type == 'VIEW_3D' and context.space_data.type == 'VIEW_3D',
          '请在3D视图内使用头发控制选择')
    _need(context.mode in {'OBJECT', 'POSE'}, '仅允许Object/Pose模式；请自行退出当前编辑或雕刻模式')
    layer = context.view_layer
    active = layer.objects.active
    pose_objects = [o for o in layer.objects if o.mode == 'POSE']
    _need(len(pose_objects) <= 1 and (not pose_objects or pose_objects[0] == active),
          '多对象Pose状态不在此选择器范围内')
    if context.mode == 'POSE':
        _local(active, 'ARMATURE')
        _need(pose_objects == [active], '活动对象与Pose上下文不一致')
    original = bpy.data.objects.get(ORIGINAL)
    _local(original, 'ARMATURE')
    _need(len(original.data.bones) == 1733, '原rig必须保留固定1733骨')
    _need(original.pose.bones.get('骨架设置') is not None, '原rig缺少骨架设置')
    rigs = {'settings': original}
    for key, (rig_name, mesh_name, count) in BRANCHES.items():
        rig, mesh = bpy.data.objects.get(rig_name), bpy.data.objects.get(mesh_name)
        _local(rig, 'ARMATURE')
        _local(mesh, 'MESH')
        _need(rig != original and rig.data != original.data and rig.data.users == 1,
              rig_name + '必须使用独立本地Armature数据')
        _need(rig.data.name == rig_name + '_data' and len(rig.data.bones) == count
              and set(rig.data.bones.keys()) == bone_names(key), rig_name + '不符合固定分支骨集合')
        _need(layer.objects.get(mesh.name) == mesh, mesh_name + '不属于当前View Layer')
        _need(len(mesh.modifiers) > 1 and mesh.modifiers[1].type == 'ARMATURE'
              and mesh.modifiers[1].object == rig, mesh_name + '的实际Armature指针不指向新rig')
        for name in BOUNDARIES:
            _need(original.pose.bones.get(name) is not None, '原rig缺少边界：' + name)
            constraints = rig.pose.bones[name].constraints
            _need(len(constraints) == 1, rig_name + '/' + name + '边界约束数量异常')
            con = constraints[0]
            _need(con.type == 'COPY_TRANSFORMS' and con.name == 'HRIG original head boundary'
                  and con.target == original and con.subtarget == name
                  and con.target_space == 'WORLD' and con.owner_space == 'WORLD'
                  and con.mix_mode == 'REPLACE' and not con.mute and con.influence == 1.,
                  rig_name + '/' + name + '边界目标或空间异常')
        rigs[key] = rig
    target = rigs[part]
    name = '骨架设置' if part == 'settings' else PRESETS[(part, control)]
    _need(layer.objects.get(target.name) == target, '目标rig不属于当前View Layer')
    _need(not target.hide_viewport and not target.hide_select, '目标rig全局隐藏或禁止选择')
    target_hidden = bool(target.hide_get(view_layer=layer))
    if part == 'settings':
        _need(context.space_data.local_view is None or target.local_view_get(context.space_data),
              '原rig不在当前局部视图中；不自动改变局部视图')
        _need(_visible_collection_path(layer, target),
              '原rig没有未排除且非隐藏的Collection/LayerCollection祖先路径')
        _need(target_hidden or target.visible_get(view_layer=layer, viewport=context.space_data),
              '原rig因其他视图条件不可见')
    else:
        _need(not target_hidden and target.visible_get(view_layer=layer, viewport=context.space_data),
              '目标rig隐藏、被排除或不在当前局部视图中')
    _need(target.data.users == 1 and target.data.pose_position == 'POSE',
          '目标Armature必须独立且处于Pose Position')
    _need(not target.data.collections.is_solo_active
          and not any(c.is_solo for c in target.data.collections_all),
          '请先自行解除目标rig的骨集合Solo；选择器不会改Solo')
    pb = target.pose.bones.get(name)
    _need(pb is not None and not pb.bone.hide_select, '目标控制骨缺失或禁止选择')
    _writable(pb.bone, 'hide')
    # Blender 5.2.1 selection lives on PoseBone (see pose_rotations.native_capture).
    touched_rigs = [target]
    if context.mode == 'POSE' and active != target:
        touched_rigs.append(active)
    for rig in touched_rigs:
        for p in rig.pose.bones:
            _writable(p, 'select')
    collections, seen = [], set()
    _need(len(pb.bone.collections) > 0, '目标骨没有可明确展开的所属骨集合')
    for collection in pb.bone.collections:
        chain_seen = set()
        while collection is not None:
            pointer = collection.as_pointer()
            _need(pointer not in chain_seen, '目标骨集合父链循环')
            chain_seen.add(pointer)
            if pointer not in seen:
                _writable(collection, 'is_visible')
                collections.append(collection)
                seen.add(pointer)
            collection = collection.parent
    return {'target': target, 'bone': pb, 'collections': collections,
            'touched_rigs': touched_rigs, 'part': part, 'control': control,
            'unhide_target_in_layer': part == 'settings' and target_hidden,
            'validated_rigs': {k: v.name for k, v in rigs.items()}}


def _snapshot(context, plan):
    target = plan['target']
    return {'active': context.view_layer.objects.active, 'mode': context.mode,
            'view_layer': context.view_layer,
            'target_hide_get': (target, bool(target.hide_get(view_layer=context.view_layer))),
            'object_selection': [(o, bool(o.select_get(view_layer=context.view_layer)))
                                 for o in context.view_layer.objects],
            'rig_selection': [(rig, rig.data.bones.active,
                               [(p, bool(p.select)) for p in rig.pose.bones])
                              for rig in plan['touched_rigs']],
            'visibility': [(c, 'is_visible', bool(c.is_visible)) for c in plan['collections']]
                          + [(plan['bone'].bone, 'hide', bool(plan['bone'].bone.hide))],
            'all_collections': [(c, bool(c.is_visible), bool(c.is_solo))
                                for c in target.data.collections_all],
            'all_bone_hide': [(b, bool(b.hide)) for b in target.data.bones]}


def _mode(context, obj, mode):
    _need(context.mode in {'OBJECT', 'POSE'} and obj.mode in {'OBJECT', 'POSE'},
          '模式意外改变，拒绝强退Edit/Sculpt')
    _need(context.view_layer.objects.active == obj, '模式操作的活动对象不一致')
    if obj.mode == mode:
        return
    with context.temp_override(object=obj, active_object=obj,
                               selected_objects=[obj], selected_editable_objects=[obj]):
        _need(bpy.ops.object.mode_set.poll(), '当前上下文无法切换Object/Pose')
        _need(bpy.ops.object.mode_set(mode=mode) == {'FINISHED'}, 'Object/Pose切换失败')


def _set_leaf(owner, field, value):
    """Single bounded visibility setter; also a native fault-injection seam."""
    setattr(owner, field, value)


def _restore(context, snapshot):
    """Restore only this operation's selection/mode and exact display leaves."""
    layer = context.view_layer
    _need(layer == snapshot['view_layer'], 'View Layer意外改变；不向其他层写回隐藏状态')
    failures = []
    def attempt(label, callback):
        try:
            callback()
        except Exception as exc:
            failures.append(label + ': ' + type(exc).__name__ + ': ' + str(exc))
    if context.mode == 'POSE':
        attempt('退出本轮Pose', lambda: _mode(context, layer.objects.active, 'OBJECT'))
    if context.mode != 'OBJECT':
        failures.append('回滚遇到非Object模式；未强退Edit/Sculpt')
    hidden_target, was_hidden = snapshot['target_hide_get']
    attempt('原rig逐层对象隐藏', lambda: hidden_target.hide_set(was_hidden, view_layer=layer))
    for owner, field, value in reversed(snapshot['visibility']):
        # Bypass _set_leaf so injected setter failure cannot suppress rollback.
        attempt('显示叶子 ' + owner.name + '/' + field, lambda: setattr(owner, field, value))
    for rig, active_bone, selections in snapshot['rig_selection']:
        for pb, selected in selections:
            attempt('骨选择 ' + rig.name + '/' + pb.name, lambda: setattr(pb, 'select', selected))
        attempt('活动骨 ' + rig.name, lambda: setattr(rig.data.bones, 'active', active_bone))
    for obj, _selected in snapshot['object_selection']:
        attempt('清理本轮对象选择 ' + obj.name, lambda: obj.select_set(False, view_layer=layer))
    old_active = snapshot['active']
    attempt('原活动对象', lambda: setattr(layer.objects, 'active', old_active))
    if snapshot['mode'] == 'POSE' and context.mode == 'OBJECT':
        attempt('原活动对象选择', lambda: old_active.select_set(True, view_layer=layer))
        attempt('原Pose模式', lambda: _mode(context, old_active, 'POSE'))
    for obj, selected in snapshot['object_selection']:
        attempt('原对象选择 ' + obj.name, lambda: obj.select_set(selected, view_layer=layer))
    attempt('恢复后更新', layer.update)
    _need(not failures, '；'.join(failures))
    _need(layer.objects.active == old_active and context.mode == snapshot['mode'], '活动对象/模式回滚不一致')
    _need(all(bool(o.select_get(view_layer=layer)) == s for o, s in snapshot['object_selection']),
          '对象选择回滚不一致')
    for rig, active_bone, selections in snapshot['rig_selection']:
        _need(rig.data.bones.active == active_bone and all(bool(p.select) == s for p, s in selections),
              '骨选择/活动骨回滚不一致')
    _need(all(bool(getattr(owner, field)) == value for owner, field, value in snapshot['visibility']),
          '显示叶子回滚不一致')
    _need(bool(hidden_target.hide_get(view_layer=layer)) == was_hidden,
          '对象逐层hide_get回滚不一致')


def _apply(context, plan, snapshot, changes):
    layer, target, pb = context.view_layer, plan['target'], plan['bone']
    if plan['unhide_target_in_layer']:
        _need(plan['part'] == 'settings' and snapshot['target_hide_get'] == (target, True),
              '仅允许原rig设置入口解除已记录的当前层对象隐藏')
        changes.append({'kind': 'visibility', 'owner': target.name, 'field': 'hide_get',
                        'setter': 'hide_set', 'scope': 'VIEW_LAYER', 'view_layer': layer.name,
                        'before': True, 'after': False})
        target.hide_set(False, view_layer=layer)
        _need(not target.hide_get(view_layer=layer), '未能解除原rig当前层对象隐藏')
    if context.mode == 'POSE':
        old_active = layer.objects.active
        _mode(context, old_active, 'OBJECT')
        changes.append({'kind': 'mode', 'object': old_active.name, 'before': 'POSE', 'after': 'OBJECT'})
    for obj, selected in snapshot['object_selection']:
        if selected:
            obj.select_set(False, view_layer=layer)
    target.select_set(True, view_layer=layer)
    layer.objects.active = target
    for owner, field, old in snapshot['visibility']:
        desired = field == 'is_visible'
        if old != desired:
            changes.append({'kind': 'visibility', 'owner': owner.name,
                            'field': field, 'before': old, 'after': desired})
            _set_leaf(owner, field, desired)
    _mode(context, target, 'POSE')
    for p in target.pose.bones:
        p.select = p == pb
    target.data.bones.active = pb.bone
    layer.update()
    _need(layer.objects.active == target and context.mode == 'POSE'
          and target.data.bones.active == pb.bone and pb.select, '未能明确选中目标控制')
    _need(all(bool(o.select_get(view_layer=layer)) == (o == target)
              for o in layer.objects), '对象选择结果不唯一')
    _need(all(bool(p.select) == (p == pb) for p in target.pose.bones), '目标rig骨选择结果不唯一')
    for rig, active_bone, selections in snapshot['rig_selection']:
        if rig != target:
            _need(rig.data.bones.active == active_bone and all(bool(p.select) == s for p, s in selections),
                  '非目标rig的骨选择发生变化')
    _need(target.visible_get(view_layer=layer, viewport=context.space_data)
          and not pb.bone.hide and any(c.is_visible_effectively for c in pb.bone.collections),
          '目标仍不可见；已拒绝保留部分结果')
    allowed = {c.as_pointer() for c in plan['collections']}
    for c, visible, solo in snapshot['all_collections']:
        _need(bool(c.is_solo) == solo and (c.as_pointer() in allowed or bool(c.is_visible) == visible),
              '非目标骨集合显示发生变化')
    _need(all(b == pb.bone or bool(b.hide) == hidden for b, hidden in snapshot['all_bone_hide']),
          '非目标骨隐藏标志发生变化')


class HRIG_P2_OT_select_hair_control(bpy.types.Operator):
    bl_idname = 'hrig_p2.select_hair_control'
    bl_label = '选择爱音头发控制'
    bl_description = '仅选择并显示固定新rig控制；不切IK/FK、不改物理、姿态或Action'
    bl_options = {'REGISTER', 'UNDO'}

    part: EnumProperty(name='归属', items=(
        ('front', '前/侧发新rig', 'HRIG_front_hair'),
        ('rear', '后发新rig', 'HRIG_rear_hair'),
        ('settings', '原rig设置', '原rig的骨架设置；忽略FK/IK预设')), default='front')
    control: EnumProperty(name='控制预设', items=(
        ('FK', '普通手调骨', '选择普通头发骨，不改变selector'),
        ('IK', 'IK目标骨', '选择IK目标，不改变selector')), default='FK')

    @classmethod
    def poll(cls, context):
        return (context.mode in {'OBJECT', 'POSE'} and context.area is not None
                and context.area.type == 'VIEW_3D')

    def execute(self, context):
        global LAST_SELECTION_REPORT
        snapshot, changes = None, []
        try:
            plan = _preflight(context, self.part, self.control)
            snapshot = _snapshot(context, plan)
            _apply(context, plan, snapshot, changes)
            LAST_SELECTION_REPORT = {
                'schema': 'hrig.hair-control-selection.v1', 'status': 'selected',
                'part': self.part, 'control': self.control, 'object': plan['target'].name,
                'bone': plan['bone'].name, 'visibility_changes': [c for c in changes if c['kind']=='visibility'],
                'mode_changes': [c for c in changes if c['kind']=='mode'] + [
                    {'kind':'mode', 'object':plan['target'].name, 'before':'OBJECT', 'after':'POSE'}],
                'selection_before': {'active': snapshot['active'].name if snapshot['active'] else None,
                                     'mode': snapshot['mode'],
                                     'objects': [o.name for o, selected in snapshot['object_selection'] if selected],
                                     'bones': {r.name: [p.name for p, s in rows if s]
                                               for r, _active, rows in snapshot['rig_selection']}},
                'selection_after': {'active': plan['target'].name, 'mode': 'POSE',
                                    'objects': [plan['target'].name], 'bone': plan['bone'].name},
                'scope': 'Control picker only; no native acceptance/FPS/default adoption claim'}
            self.report({'INFO'}, '已选择 ' + plan['target'].name + ' / ' + plan['bone'].name)
            return {'FINISHED'}
        except Exception as exc:
            rollback_error = None
            if snapshot is not None:
                try:
                    _restore(context, snapshot)
                except Exception as restore_exc:
                    rollback_error = type(restore_exc).__name__ + ': ' + str(restore_exc)
            LAST_SELECTION_REPORT = {
                'schema': 'hrig.hair-control-selection.v1',
                'status': 'rejected' if snapshot is None else ('rollback_failed' if rollback_error else 'rolled_back'),
                'error': type(exc).__name__ + ': ' + str(exc),
                'attempted_changes': changes, 'rollback_error': rollback_error}
            message = '选择未完成：' + str(exc)
            if rollback_error:
                message += '；恢复未完全通过，请检查现场：' + rollback_error
            self.report({'ERROR'}, message)
            return {'CANCELLED'}


def _action_labels(layout, obj):
    if obj is None:
        layout.label(text='对象缺失', icon='ERROR')
        return
    ad = obj.animation_data
    action = ad.action if ad else None
    slot = ad.action_slot if ad else None
    layout.label(text='Action：' + (action.name if action else '无'))
    layout.label(text='槽位：' + (slot.identifier if slot else '无'))


class HRIG_P2_PT_hair_controls(bpy.types.Panel):
    bl_label = '爱音头发控制 · v028候选'
    bl_idname = 'HRIG_P2_PT_hair_controls'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'P2 Rig Probe'

    def draw(self, context):
        layout = self.layout
        layout.label(text='按钮仅选择控制，不切IK/FK模式', icon='INFO')
        layout.label(text='全身/头部、模式与物理：仍在原rig')
        layout.label(text='原rig同名发骨不直接驱动已迁移Mesh')
        for part, caption in (('front', '前/侧发 → HRIG_front_hair'), ('rear', '后发 → HRIG_rear_hair')):
            box = layout.box()
            box.label(text=caption)
            row = box.row(align=True)
            for control, label in (('FK', '选手调骨'), ('IK', '选IK目标')):
                op = row.operator(HRIG_P2_OT_select_hair_control.bl_idname, text=label)
                op.part, op.control = part, control
            _action_labels(box, bpy.data.objects.get(BRANCHES[part][0]))
        box = layout.box()
        box.label(text='设置 → 千早爱音-素体骨架')
        op = box.operator(HRIG_P2_OT_select_hair_control.bl_idname, text='选原rig骨架设置')
        op.part, op.control = 'settings', 'FK'
        _action_labels(box, bpy.data.objects.get(ORIGINAL))
        layout.label(text='选中后可编辑姿态或插入关键帧', icon='INFO')
        if LAST_SELECTION_REPORT is not None:
            layout.label(text='上次选择：' + LAST_SELECTION_REPORT['status'])


CLASSES = (HRIG_P2_OT_select_hair_control, HRIG_P2_PT_hair_controls)


def register():
    _need(not _REGISTERED, '头发控制模块已经注册')
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
    except Exception:
        for cls in reversed(_REGISTERED):
            bpy.utils.unregister_class(cls)
        _REGISTERED.clear()
        raise


def unregister():
    global LAST_SELECTION_REPORT
    for cls in reversed(_REGISTERED):
        bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
    LAST_SELECTION_REPORT = None
