"""Object-owned basic C94 character controls and editable native sidebar.

The generated-object marker is supplied by the generator.  No global active-rig
cache or author's Text code. Matching actions dispatch to the shared cores.
"""
import json
import math
import uuid
from contextlib import contextmanager

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from mathutils import Quaternion
from . import mode_semantics

RIG_KEY = 'c94_generated_rig_v1'
LAYOUT_KEY = 'c94_control_layout_v1'
SCHEMA = 'c94.control-layout.v1'
CONTROL_COLLECTIONS = frozenset({
    '根骨骼', 'FK', 'IK', '躯干控制', '四肢', '矫正控制', '拉伸骨骼',
    '面部', '面部-主控', '面部-次要', '头发', '表情面板',
})
TECHNICAL_WORDS = ('机制', '形变', '物理', '接续')
_REGISTERED = []


def _managed(rig):
    if not rig or rig.type != 'ARMATURE':
        raise ValueError('选择本工作流生成的角色骨架')
    raw = rig.get(RIG_KEY)
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError as exc:
        raise ValueError('角色生成记录无法解析') from exc
    if not isinstance(value, dict) or not value:
        raise ValueError('当前对象没有有效的角色生成记录')
    if value.get('schema', 'c94.generated.v1') != 'c94.generated.v1':
        raise ValueError('不支持此角色生成记录版本')
    return rig


def _writable(rig, data=False):
    _managed(rig)
    if rig.library or rig.is_library_indirect:
        raise ValueError('链接角色只读，请使用本地工作副本')
    if data and (rig.data.library or rig.data.users != 1):
        raise ValueError('骨架数据正在共享，请先建立独立骨架副本再改变集合显隐')
    return rig


def current_rig(context, name=''):
    if name:
        rig = context.scene.objects.get(name)
        return _managed(rig)
    active = context.view_layer.objects.active
    if active and active.type == 'ARMATURE' and active.get(RIG_KEY):
        return _managed(active)
    rigs = [o for o in context.selected_objects if o.type == 'ARMATURE' and o.get(RIG_KEY)]
    if len(rigs) != 1:
        raise ValueError('请激活一个生成角色；多个角色共存时以活动骨架为准')
    return _managed(rigs[0])


def _is_control(pb):
    collections = {c.name for c in pb.bone.collections}
    basic = not pb.bone.use_deform and not pb.bone.hide_select and pb.custom_shape is not None
    if not basic:
        return False
    identity = pb.bone.get('c94_accessory_control_v1')
    if isinstance(identity,str) and identity:
        try:
            metadata=json.loads(pb.id_data.get(RIG_KEY,''))
            chains=metadata.get('chain_extensions',{}).get('chains',[])
            if any(c.get('id')==identity and pb.name in c.get('names',{}).get('control',[]) for c in chains):
                return True
        except (ValueError,TypeError,AttributeError):
            pass
    return (basic
            and bool(collections & CONTROL_COLLECTIONS)
            and not any(word in name for name in collections for word in TECHNICAL_WORDS)
            and not pb.name.startswith(('形变-', '接续-')))


def eligible_controls(rig):
    _managed(rig)
    return [pb.name for pb in rig.pose.bones if _is_control(pb)]


def _defaults(pb):
    # Preserve authored channel representation, including quaternion sign and
    # scale signs. A decomposition alone can change partially locked channels.
    return {'location': list(pb.location), 'scale': list(pb.scale),
            'rotation_mode': pb.rotation_mode,
            'rotation_quaternion': list(pb.rotation_quaternion),
            'rotation_euler': list(pb.rotation_euler),
            'rotation_axis_angle': list(pb.rotation_axis_angle),
            'basis_quaternion': list(pb.matrix_basis.to_quaternion().normalized())}


def _public_settings(rig):
    pb = rig.pose.bones.get('骨架设置')
    if not pb:
        return []
    has_hair = any('头发' in c.name and len(c.bones) for c in rig.data.collections_all)
    return [(key, value) for key, value in pb.items()
            if isinstance(value, (bool, int, float)) and math.isfinite(float(value))
            and (has_hair or key not in {'头发FK-IK', '前发物理', '后发物理'})
            and not key.startswith(('_', 'wiggle_', 'constraint_', 'rigify_', 'HRIG'))]


def _default_collections(rig):
    """Show actual control groups; keep technical/empty groups out of defaults."""
    def has_controls(collection):
        return (any(_is_control(rig.pose.bones[bone.name]) for bone in collection.bones)
                or any(has_controls(child) for child in collection.children))
    return [collection for collection in rig.data.collections_all
            if not any(word in collection.name for word in TECHNICAL_WORDS)
            and has_controls(collection)]


def read_layout(rig):
    _managed(rig)
    raw = rig.get(LAYOUT_KEY)
    if not isinstance(raw, str) or not raw:
        raise ValueError('该角色尚未安装基础面板布局')
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError('角色面板布局无法解析；原记录保留') from exc
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or not isinstance(value.get('rows'), list):
        raise ValueError('角色面板布局版本或行记录无效；原记录保留')
    ids = [r.get('id') for r in value['rows'] if isinstance(r, dict)]
    if len(ids) != len(value['rows']) or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('角色面板行标识缺失或重复；原记录保留')
    return value


def _save(rig, layout):
    rig[LAYOUT_KEY] = json.dumps(layout, ensure_ascii=False, allow_nan=False)


def install(rig, label='Character'):
    """Call after generator neutral calibration; keep any valid existing layout."""
    _writable(rig)
    if rig.get(LAYOUT_KEY):
        layout = read_layout(rig)
        return {'installed': False, 'preserved': True, 'rows': len(layout['rows']), 'rig': rig.name}
    rows = []
    def add(kind, group, row_label, **fields):
        rows.append({'id': uuid.uuid4().hex, 'kind': kind, 'group': group,
                     'label': row_label, 'visible': True, **fields})
    for collection in _default_collections(rig):
        add('collection', '骨集合', collection.name, collection=collection.name)
    for key, _ in _public_settings(rig):
        add('property', '骨架设置', key, bone='骨架设置', property=key)
    for action, row_label in [('RESET', '复位选中控件'), ('KEY', '选中控件插关键帧'),
                              ('DELETE_KEY', '删除选中控件当前帧关键帧')]:
        add('action', '选中控件', row_label, action=action)
    controls = eligible_controls(rig)
    layout = {'schema': SCHEMA, 'label': str(label), 'editing': False, 'rows': rows,
              'defaults': {name: _defaults(rig.pose.bones[name]) for name in controls},
              'control_policy': {'collections': sorted(CONTROL_COLLECTIONS),
                 'requires': ['non_deform', 'selectable', 'custom_shape'],
                 'excluded_collection_words': list(TECHNICAL_WORDS),
                 'scope': 'selected visible controls; transforms only, no mode-property reset'}}
    _save(rig, layout)
    add_native_property_controls(rig)
    actions = add_arm_switch_actions(rig)
    actions = add_leg_switch_actions(rig)
    actions = add_mode_animation_actions(rig)
    return {'installed': True, 'preserved': False, 'rows': actions['rows'],
            'controls': len(controls), 'rig': rig.name}


def missing_native_property_controls(rig):
    layout = read_layout(rig)
    existing = {(r.get('bone'), r.get('property')) for r in layout['rows'] if r.get('kind') == 'property'}
    return [r for r in mode_semantics.available(rig) if (r['bone'], r['property']) not in existing]


def add_native_property_controls(rig):
    """Append available native inputs; never reorder or relabel existing rows."""
    _writable(rig)
    missing = missing_native_property_controls(rig)
    if missing:
        layout = read_layout(rig)
        for row in missing:
            layout['rows'].append({'id': uuid.uuid4().hex, 'kind': 'property',
                'group': row['group'], 'label': row['label'], 'visible': True,
                'bone': row['bone'], 'property': row['property']})
        _save(rig, layout)
    return {'added': len(missing), 'rows': len(read_layout(rig)['rows'])}


def add_arm_switch_actions(rig):
    """Append the new daily actions without changing any existing layout row."""
    _writable(rig)
    layout = read_layout(rig)
    existing = {(r.get('side'), r.get('target')) for r in layout['rows']
                if r.get('kind') == 'action' and r.get('action') == 'SWITCH_ARM'}
    added = 0
    for side, label in (('L', '左臂'), ('R', '右臂')):
        for target in ('FK', 'IK'):
            if (side, target) in existing:
                continue
            layout['rows'].append({'id': uuid.uuid4().hex, 'kind': 'action',
                'group': '手臂保姿态切换', 'label': label + ' → ' + target,
                'visible': True, 'action': 'SWITCH_ARM', 'side': side, 'target': target})
            added += 1
    if added:
        _save(rig, layout)
    return {'added': added, 'rows': len(layout['rows'])}


def add_leg_switch_actions(rig):
    """Append daily leg actions while preserving every existing layout row."""
    _writable(rig)
    layout = read_layout(rig)
    existing = {(r.get('side'), r.get('target')) for r in layout['rows']
                if r.get('kind') == 'action' and r.get('action') == 'SWITCH_LEG'}
    added = 0
    for side, label in (('L', '左腿'), ('R', '右腿')):
        for target in ('FK', 'IK'):
            if (side, target) in existing:
                continue
            layout['rows'].append({'id': uuid.uuid4().hex, 'kind': 'action',
                'group': '腿部保姿态切换', 'label': label + ' → ' + target,
                'visible': True, 'action': 'SWITCH_LEG', 'side': side, 'target': target})
            added += 1
    if added:
        _save(rig, layout)
    return {'added': added, 'rows': len(layout['rows'])}


def _mode_animation_specs():
    specs = [{'action': 'RECORD_MODES', 'label': '记录当前角色模式'}]
    for limb, word in (('ARM', '臂'), ('LEG', '腿')):
        for side, label in (('L', '左'), ('R', '右')):
            name = label + word
            specs.append({'action': 'RECORD_LIMB', 'limb': limb, 'side': side,
                          'label': name + '：记录源姿态'})
            for target in ('FK', 'IK'):
                specs.append({'action': 'SWITCH_AND_KEY', 'limb': limb, 'side': side,
                              'target': target, 'label': name + ' → ' + target + '：匹配并记录'})
    return specs


def _mode_animation_key(row):
    return tuple(row.get(key) for key in ('action', 'limb', 'side', 'target'))


def missing_mode_animation_actions(rig):
    existing = {_mode_animation_key(row) for row in read_layout(rig)['rows']
                if row.get('kind') == 'action'}
    return [spec for spec in _mode_animation_specs() if _mode_animation_key(spec) not in existing]


def add_mode_animation_actions(rig):
    """Append explicit recording actions; preserve all existing layout rows."""
    _writable(rig)
    missing = missing_mode_animation_actions(rig)
    if missing:
        layout = read_layout(rig)
        for spec in missing:
            layout['rows'].append({'id': uuid.uuid4().hex, 'kind': 'action',
                                  'group': '模式与切换动画', 'visible': True, **spec})
        _save(rig, layout)
    return {'added': len(missing), 'rows': len(read_layout(rig)['rows'])}


def _collection(rig, name):
    collection = rig.data.collections_all.get(name)
    if collection is None:
        raise ValueError('骨集合不存在：' + name)
    return collection


def set_collection_visible(rig, name, visible):
    _writable(rig, data=True)
    collection = _collection(rig, name)
    collection.is_visible = bool(visible)
    if visible:
        parent = collection.parent
        while parent:
            parent.is_visible = True
            parent = parent.parent
    return {'collection': name, 'visible': bool(collection.is_visible)}


def _pose(context, rig):
    if rig.name not in context.view_layer.objects or not rig.visible_get(view_layer=context.view_layer) or rig.hide_select:
        raise ValueError('当前视图层中的角色不可见或不可选')
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in context.view_layer.objects:
        obj.select_set(obj == rig, view_layer=context.view_layer)
    context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='POSE')


def select_collection(context, rig, name):
    _writable(rig, data=True)
    collection = _collection(rig, name)
    selected = [b.name for b in collection.bones if _is_control(rig.pose.bones[b.name])]
    if not selected:
        raise ValueError('该集合没有符合控制层规则的可选控件')
    _pose(context, rig)
    set_collection_visible(rig, name, True)
    for pb in rig.pose.bones:
        pb.select = pb.name in selected
        if pb.name in selected:
            pb.hide = False
            pb.bone.hide = False
    rig.data.bones.active = rig.data.bones[selected[0]]
    return {'selected': selected, 'rig': rig.name, 'mode': rig.mode}


@contextmanager
def _preserve_context(context, rig):
    active = context.view_layer.objects.active
    mode = active.mode if active else 'OBJECT'
    selection = [o.name for o in context.selected_objects]
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    try:
        if rig.name not in context.view_layer.objects:
            raise ValueError('角色不在当前视图层')
        for obj in context.view_layer.objects:
            obj.select_set(obj == rig, view_layer=context.view_layer)
        context.view_layer.objects.active = rig
        yield
    finally:
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.view_layer.objects:
            obj.select_set(obj.name in selection, view_layer=context.view_layer)
        context.view_layer.objects.active = active
        if active and mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=mode)


def _visible(pb):
    if pb.hide or pb.bone.hide:
        return False
    def shown(collection):
        while collection:
            if not collection.is_visible:
                return False
            collection = collection.parent
        return True
    return any(shown(c) for c in pb.bone.collections)


def _channels(pb):
    for path, locks in [('location', pb.lock_location), ('scale', pb.lock_scale)]:
        for index, locked in enumerate(locks):
            if not locked:
                yield path, index
    if pb.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        locks = [bool(pb.lock_rotations_4d and pb.lock_rotation_w), *pb.lock_rotation]
        path = 'rotation_quaternion' if pb.rotation_mode == 'QUATERNION' else 'rotation_axis_angle'
    else:
        locks, path = pb.lock_rotation, 'rotation_euler'
    for index, locked in enumerate(locks):
        if not locked:
            yield path, index


def _single_user_action(rig):
    animation = rig.animation_data
    if animation and animation.action and (animation.action.users > 1 or animation.action.library):
        slot_handle = animation.action_slot_handle
        animation.action = animation.action.copy()
        slot = next((s for s in animation.action.slots if s.handle == slot_handle), None)
        if slot:
            animation.action_slot = slot


def pose_action(context, rig, action):
    _writable(rig)
    if action not in {'RESET', 'KEY', 'DELETE_KEY'}:
        raise ValueError('未知控件操作')
    layout = read_layout(rig)
    selected = [pb for pb in rig.pose.bones if pb.select and _is_control(pb) and _visible(pb)]
    if not selected:
        raise ValueError('先选择可见控制骨；形变、机制、物理和不可选骨不会被操作')
    if action == 'RESET' and any(pb.name not in layout.get('defaults', {}) for pb in selected):
        raise ValueError('所选控件没有生成后的中立默认值，请重新建立明确的控件默认记录')
    changed = 0
    with _preserve_context(context, rig):
        if action in {'KEY', 'DELETE_KEY'}:
            _single_user_action(rig)
        for pb in selected:
            channels = list(_channels(pb))
            if action == 'RESET':
                default = layout['defaults'][pb.name]
                q = Quaternion(default.get('basis_quaternion', default['rotation_quaternion']))
                axis, angle = q.to_axis_angle()
                values = {'location': default['location'], 'scale': default['scale'],
                          'rotation_quaternion': list(q), 'rotation_axis_angle': [angle, *axis],
                          'rotation_euler': list(q.to_euler(pb.rotation_mode if pb.rotation_mode not in {'QUATERNION', 'AXIS_ANGLE'} else 'XYZ'))}
                if pb.rotation_mode == default.get('rotation_mode'):
                    for path in ('rotation_quaternion', 'rotation_euler', 'rotation_axis_angle'):
                        if path in default:
                            values[path] = default[path]
                for path, index in channels:
                    getattr(pb, path)[index] = values[path][index]
                    changed += 1
            else:
                frame = context.scene.frame_current + context.scene.frame_subframe
                for path, index in channels:
                    if action == 'KEY':
                        changed += bool(pb.keyframe_insert(data_path=path, index=index, frame=frame, group=pb.name))
                    elif rig.animation_data and rig.animation_data.action:
                        try:
                            changed += bool(pb.keyframe_delete(data_path=path, index=index, frame=frame))
                        except RuntimeError as exc:
                            # A selected channel need not have a key at this frame.
                            if 'not found' not in str(exc).lower():
                                raise
        rig.update_tag(refresh={'OBJECT'})
        context.view_layer.update()
    return {'rig': rig.name, 'controls': [pb.name for pb in selected],
            'action': action, 'changed_channels': changed}


def edit_layout(rig, row_id='', action='UP', label=''):
    _writable(rig)
    layout = read_layout(rig)
    if action == 'EDIT_MODE':
        layout['editing'] = not layout.get('editing', False)
    else:
        index = next((i for i, row in enumerate(layout['rows']) if row['id'] == row_id), None)
        if index is None:
            raise ValueError('布局行不存在，可能已被其它操作修改')
        row = layout['rows'][index]
        if action in {'UP', 'DOWN'}:
            other = index + (-1 if action == 'UP' else 1)
            if 0 <= other < len(layout['rows']):
                layout['rows'][index], layout['rows'][other] = layout['rows'][other], row
        elif action == 'TOGGLE':
            row['visible'] = not row.get('visible', True)
        elif action == 'LABEL':
            if not label.strip():
                raise ValueError('标签不能为空')
            row['label'] = label.strip()
        else:
            raise ValueError('未知布局编辑操作')
    _save(rig, layout)
    return {'rows': len(layout['rows']), 'editing': layout.get('editing', False)}


class _RigOperator:
    # Blender operators reject data-block PointerProperty. Keep the real
    # session UID (not a reusable address) plus Data UID; ptr fields remain
    # diagnostics/legacy fallback, and rig_name serves old immediate scripts.
    rig_uid: StringProperty(options={'HIDDEN'})
    rig_data_uid: StringProperty(options={'HIDDEN'})
    rig_ptr: StringProperty(options={'HIDDEN'})
    rig_data_ptr: StringProperty(options={'HIDDEN'})
    rig_name: StringProperty(options={'HIDDEN'})

    def bind_rig(self, rig):
        self.rig_uid = str(rig.session_uid)
        self.rig_data_uid = str(rig.data.session_uid)
        self.rig_ptr = str(rig.as_pointer())
        self.rig_data_ptr = str(rig.data.as_pointer())
        self.rig_name = rig.name
        return rig

    def resolve_rig(self, context):
        uid = str(getattr(self, 'rig_uid', '') or '')
        if uid:
            matches = [obj for obj in bpy.data.objects
                       if obj.type == 'ARMATURE' and str(getattr(obj, 'session_uid', '')) == uid]
            if len(matches) != 1:
                raise ValueError('按钮绑定的角色会话ID已失效；不按名称或地址回退')
            rig = matches[0]
            if context is not None and rig.name not in context.scene.objects:
                raise ValueError('按钮绑定的角色不在当前Scene；不跨Scene回退')
            data_uid = str(getattr(self, 'rig_data_uid', '') or '')
            if data_uid and str(getattr(rig.data, 'session_uid', '')) != data_uid:
                raise ValueError('按钮绑定的角色数据会话ID已改变；不按名称回退')
            return _managed(rig)
        pointer = str(getattr(self, 'rig_ptr', '') or '')
        if pointer:
            matches = [obj for obj in bpy.data.objects
                       if obj.type == 'ARMATURE' and str(obj.as_pointer()) == pointer]
            if len(matches) != 1:
                raise ValueError('按钮绑定的角色已不存在；请重新打开面板')
            rig = matches[0]
            if context is not None and rig.name not in context.scene.objects:
                raise ValueError('按钮绑定的角色不在当前Scene；不跨Scene回退')
            data_pointer = str(getattr(self, 'rig_data_ptr', '') or '')
            if data_pointer and str(rig.data.as_pointer()) != data_pointer:
                raise ValueError('按钮绑定的角色数据身份已改变；请重新打开面板')
            return _managed(rig)
        return current_rig(context, self.rig_name)

    def run(self, context, fn):
        try:
            fn(self.resolve_rig(context))
            return {'FINISHED'}
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94CTRL_OT_Collection(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.control_collection'
    bl_label = '角色骨集合'
    bl_options = {'REGISTER', 'UNDO'}
    collection_name: StringProperty()
    action: EnumProperty(items=[('VISIBLE', '显隐', ''), ('SELECT', '选择控件', '')])
    visible: BoolProperty(default=True)

    def execute(self, context):
        return self.run(context, lambda rig: select_collection(context, rig, self.collection_name)
                        if self.action == 'SELECT' else set_collection_visible(rig, self.collection_name, self.visible))


class C94CTRL_OT_Pose(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.control_pose'
    bl_label = '选中角色控件'
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[('RESET', '复位', ''), ('KEY', '插关键帧', ''), ('DELETE_KEY', '删当前帧关键帧', '')])

    def execute(self, context):
        return self.run(context, lambda rig: pose_action(context, rig, self.action))


class C94CTRL_OT_SwitchArm(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.switch_arm'
    bl_label = '手臂保姿态切换'
    bl_description = '匹配手臂原生控制并切换FK/IK；保留手指输入，不能匹配时恢复当前姿态'
    bl_options = {'REGISTER', 'UNDO'}
    side: EnumProperty(items=[('L', '左臂', ''), ('R', '右臂', '')])
    target: EnumProperty(items=[('FK', 'FK', ''), ('IK', 'IK', '')])

    def execute(self, context):
        try:
            from . import pose_switching
            report = pose_switching.switch(self.resolve_rig(context), side=self.side, target=self.target)
            if report['status'] == 'no_change':
                self.report({'INFO'}, '当前已是' + self.target)
            else:
                self.report({'INFO'}, '已切换到' + self.target)
            return {'FINISHED'}
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94CTRL_OT_AddSwitchActions(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.add_arm_switch_actions'
    bl_label = '添加手臂保姿态切换按钮'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return self.run(context, add_arm_switch_actions)


class C94CTRL_OT_SwitchLeg(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.switch_leg'
    bl_label = '腿部保姿态切换'
    bl_description = '匹配原生腿部控制并切换FK/IK；保留足底滚转、子趾弯曲和连接；基趾参与匹配，失败时恢复'
    bl_options = {'REGISTER', 'UNDO'}
    side: EnumProperty(items=[('L', '左腿', ''), ('R', '右腿', '')])
    target: EnumProperty(items=[('FK', 'FK', ''), ('IK', 'IK', '')])

    def execute(self, context):
        try:
            from . import pose_switching
            report = pose_switching.switch(self.resolve_rig(context),
                limb='LEG', side=self.side, target=self.target)
            self.report({'INFO'}, '当前已是' + self.target if report['status'] == 'no_change'
                        else '已切换到' + self.target)
            return {'FINISHED'}
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94CTRL_OT_AddLegSwitchActions(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.add_leg_switch_actions'
    bl_label = '添加腿部保姿态切换按钮'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return self.run(context, add_leg_switch_actions)


class C94CTRL_OT_ModeAnimation(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.mode_animation'
    bl_label = '记录角色控制动画'
    bl_description = '仅记录当前帧；匹配切换前请先在较早帧记录源姿态，不自动补前一帧或烘焙'
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[('RECORD_MODES', '记录角色模式', ''),
                               ('RECORD_LIMB', '记录源姿态', ''),
                               ('SWITCH_AND_KEY', '匹配切换并记录', '')])
    limb: EnumProperty(items=[('ARM', '手臂', ''), ('LEG', '腿', '')])
    side: EnumProperty(items=[('L', '左侧', ''), ('R', '右侧', '')])
    target: EnumProperty(items=[('FK', 'FK', ''), ('IK', 'IK', '')])

    def execute(self, context):
        try:
            from . import mode_animation
            rig = self.resolve_rig(context)
            if self.action == 'RECORD_MODES':
                result = mode_animation.record_modes(rig)
                message = '已记录角色模式'
            elif self.action == 'RECORD_LIMB':
                result = mode_animation.record_limb(rig, limb=self.limb, side=self.side)
                message = '已记录源姿态'
            else:
                result = mode_animation.switch_and_key(rig, limb=self.limb, side=self.side, target=self.target)
                message = '已匹配到' + self.target + '并记录'
            self.report({'INFO'}, message + '，当前帧 ' + format(result['frame'], '.9g'))
            return {'FINISHED'}
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94CTRL_OT_AddModeAnimationActions(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.add_mode_animation_actions'
    bl_label = '添加模式动画记录按钮'
    bl_description = '只追加缺失的模式和肢体动画记录动作，保留现有布局'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            result = add_mode_animation_actions(self.resolve_rig(context))
            self.report({'INFO'}, '已添加' + str(result['added']) + '项动画记录动作')
            return {'FINISHED'}
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94CTRL_OT_Layout(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.control_layout'
    bl_label = '编辑面板行'
    bl_options = {'REGISTER', 'UNDO'}
    row_id: StringProperty(options={'HIDDEN'})
    action: EnumProperty(items=[(key, label, '') for key, label in [
        ('UP', '上移',), ('DOWN', '下移'), ('TOGGLE', '显隐'), ('LABEL', '标签'), ('EDIT_MODE', '编辑布局')]])
    label: StringProperty(name='行标签')

    def invoke(self, context, event):
        if self.action != 'LABEL':
            return self.execute(context)
        try:
            rig = self.resolve_rig(context)
            # F3/script INVOKE_DEFAULT may not pass _button args; bind the
            # resolved target immediately so a later rename cannot redirect.
            self.bind_rig(rig)
            self.label = next(row['label'] for row in read_layout(rig)['rows'] if row['id'] == self.row_id)
        except (ValueError, StopIteration) as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, 'label')

    def execute(self, context):
        return self.run(context, lambda rig: edit_layout(rig, self.row_id, self.action, self.label))


def _button(layout, operator, rig, **values):
    button = layout.operator(operator, **{k: values.pop(k) for k in list(values) if k in {'text', 'icon', 'depress'}})
    button.rig_uid = str(rig.session_uid)
    button.rig_data_uid = str(rig.data.session_uid)
    button.rig_ptr = str(rig.as_pointer())
    button.rig_data_ptr = str(rig.data.as_pointer())
    button.rig_name = rig.name
    for key, value in values.items():
        setattr(button, key, value)
    return button


class C94CTRL_OT_AddNativeProperties(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.add_native_property_controls'
    bl_label = '补齐角色模式属性'
    bl_description = '只追加当前角色已有但面板缺失的原生属性，保留现有布局'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            result = add_native_property_controls(self.resolve_rig(context))
            self.report({'INFO'}, '已补齐' + str(result['added']) + '项属性')
            return {'FINISHED'}
        except (ValueError, KeyError) as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}


class C94CTRL_OT_NativePropertyInfo(_RigOperator, bpy.types.Operator):
    bl_idname = 'c94_workflow.native_property_info'
    bl_label = '原生控制说明'
    bl_description = '查看此控制的实际含义、联动条件和操作边界'
    bone_name: StringProperty()
    property_name: StringProperty()

    def execute(self, context):
        try:
            import textwrap
            rig = self.resolve_rig(context)
            lines = mode_semantics.info_lines(rig, self.bone_name, self.property_name)
            def draw(menu, _context):
                for text in lines:
                    for part in textwrap.wrap(text, width=32):
                        menu.layout.label(text=part)
            context.window_manager.popup_menu(draw, title=self.bl_label, icon='INFO')
            return {'FINISHED'}
        except (ValueError, KeyError) as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}


class C94CTRL_PT_Character(bpy.types.Panel):
    bl_label = '角色控制'
    bl_idname = 'C94CTRL_PT_character'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_order = 0

    @classmethod
    def poll(cls, context):
        try:
            current_rig(context)
            return True
        except ValueError:
            return False

    def draw(self, context):
        layout = self.layout
        try:
            rig = current_rig(context)
            declaration = read_layout(rig)
        except ValueError as exc:
            layout.label(text=str(exc), icon='ERROR'); return
        layout.label(text=str(declaration.get('label', rig.name)) + ' · ' + rig.name, icon='ARMATURE_DATA')
        workspace = getattr(context.scene, 'c94_workspace_options_v1', None)
        advanced = workspace is None or workspace.level != 'DAILY'
        if advanced:
            _button(layout, 'c94_workflow.control_layout', rig, text='完成布局编辑' if declaration.get('editing') else '编辑布局',
                    icon='PREFERENCES', action='EDIT_MODE')
        if advanced and missing_native_property_controls(rig):
            _button(layout, 'c94_workflow.add_native_property_controls', rig, text='补齐角色模式属性')
        arm_actions = {(row.get('side'), row.get('target')) for row in declaration['rows']
                       if row.get('kind') == 'action' and row.get('action') == 'SWITCH_ARM'}
        if advanced and not {('L', 'FK'), ('L', 'IK'), ('R', 'FK'), ('R', 'IK')} <= arm_actions:
            _button(layout, 'c94_workflow.add_arm_switch_actions', rig, text='添加手臂保姿态切换按钮')
        leg_actions = {(row.get('side'), row.get('target')) for row in declaration['rows']
                       if row.get('kind') == 'action' and row.get('action') == 'SWITCH_LEG'}
        if advanced and not {('L', 'FK'), ('L', 'IK'), ('R', 'FK'), ('R', 'IK')} <= leg_actions:
            _button(layout, 'c94_workflow.add_leg_switch_actions', rig, text='添加腿部保姿态切换按钮')
        if advanced and missing_mode_animation_actions(rig):
            _button(layout, 'c94_workflow.add_mode_animation_actions', rig, text='添加模式动画记录按钮')
        edit = advanced and declaration.get('editing', False)
        current_group, box = None, layout
        for row in declaration['rows']:
            if not row.get('visible', True) and not edit:
                continue
            group = row.get('group', '角色控制')
            if group != current_group:
                box = layout.box(); box.label(text=group); current_group = group
            line = box.row(align=True)
            if edit:
                _button(line, 'c94_workflow.control_layout', rig, text='', icon='TRIA_UP', row_id=row['id'], action='UP')
                _button(line, 'c94_workflow.control_layout', rig, text='', icon='TRIA_DOWN', row_id=row['id'], action='DOWN')
                _button(line, 'c94_workflow.control_layout', rig, text='', icon='GREASEPENCIL', row_id=row['id'], action='LABEL')
                _button(line, 'c94_workflow.control_layout', rig, text='', icon='HIDE_OFF' if row.get('visible', True) else 'HIDE_ON', row_id=row['id'], action='TOGGLE')
            content = line.row(align=True)
            content.enabled = row.get('visible', True)
            kind = row.get('kind')
            if kind == 'collection':
                collection = rig.data.collections_all.get(row.get('collection', ''))
                if collection is None:
                    content.label(text=row.get('label', '') + '（集合缺失）', icon='INFO'); continue
                _button(content, 'c94_workflow.control_collection', rig, text=row.get('label', collection.name),
                        icon='HIDE_OFF' if collection.is_visible else 'HIDE_ON', action='VISIBLE',
                        collection_name=collection.name, visible=not collection.is_visible)
                selector = content.row(align=True)
                selector.enabled = any(_is_control(rig.pose.bones[b.name]) for b in collection.bones)
                _button(selector, 'c94_workflow.control_collection', rig, text='选择', icon='RESTRICT_SELECT_OFF',
                        action='SELECT', collection_name=collection.name)
            elif kind == 'property':
                pb = rig.pose.bones.get(row.get('bone', ''))
                key = row.get('property', '')
                if pb and key in pb and isinstance(pb[key], (bool, int, float)):
                    content.prop(pb, '[' + json.dumps(key, ensure_ascii=False) + ']', text=row.get('label', key))
                    if mode_semantics.describe(pb.name, key):
                        _button(content, 'c94_workflow.native_property_info', rig, text='', icon='INFO',
                                bone_name=pb.name, property_name=key)
                else:
                    content.label(text=row.get('label', '') + '（属性缺失）', icon='INFO')
            elif kind == 'action':
                if row.get('action') == 'SWITCH_ARM':
                    _button(content, 'c94_workflow.switch_arm', rig, text=row.get('label', ''),
                            side=row.get('side', 'L'), target=row.get('target', 'IK'))
                elif row.get('action') == 'SWITCH_LEG':
                    _button(content, 'c94_workflow.switch_leg', rig, text=row.get('label', ''),
                            side=row.get('side', 'L'), target=row.get('target', 'IK'))
                elif row.get('action') in {'RECORD_MODES', 'RECORD_LIMB', 'SWITCH_AND_KEY'}:
                    _button(content, 'c94_workflow.mode_animation', rig, text=row.get('label', ''),
                            action=row['action'], limb=row.get('limb', 'ARM'),
                            side=row.get('side', 'L'), target=row.get('target', 'IK'))
                else:
                    _button(content, 'c94_workflow.control_pose', rig, text=row.get('label', ''), action=row.get('action', 'RESET'))
            else:
                content.label(text=row.get('label', '') + '（未支持的行）', icon='INFO')
        layout.label(text='属性改变模式/权重；保姿态请用匹配按钮', icon='INFO')
        layout.label(text='记录只写当前帧；先在较早帧记录源姿态', icon='INFO')
        if advanced:
            layout.label(text='复位、关键帧按钮只处理选中控件的变换；模式可右键单独插帧', icon='INFO')
            layout.label(text='动画记录不自动补前一帧或烘焙', icon='INFO')


_CLASSES = (C94CTRL_OT_Collection, C94CTRL_OT_Pose, C94CTRL_OT_SwitchArm,
            C94CTRL_OT_AddSwitchActions, C94CTRL_OT_SwitchLeg, C94CTRL_OT_AddLegSwitchActions,
            C94CTRL_OT_ModeAnimation, C94CTRL_OT_AddModeAnimationActions,
            C94CTRL_OT_Layout, C94CTRL_OT_AddNativeProperties,
            C94CTRL_OT_NativePropertyInfo, C94CTRL_PT_Character)


def register():
    if _REGISTERED:
        return
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
    except Exception:
        unregister()
        raise


def unregister():
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
