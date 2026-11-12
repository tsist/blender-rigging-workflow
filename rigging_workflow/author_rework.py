# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit C94 author rework on independent generated candidates.

RAW Keys are never a promise of re-sampling fidelity. Compatible input curves,
known native P1 boards and layouts migrate by declared identity; derived physics
and changed-space channels carry explicit pending work, not inherited authority.
"""
import copy
import json
import math
from pathlib import Path
import re
import uuid

import bpy
import numpy as np

from . import (generation, module_configuration as modules, positioning,
               control_panel as panel, corrective_authoring as sig,
               workflow_entry as entry, rebuild, face_modes as modes,
               face_pose_library as library, face_expression_controls as expressions,
               face_mouth_pad as mouth, face_graph_board as graph,
               face_expression_layout as sidebar, face_capture as face, mode_semantics)

STATE_KEY = 'c94_author_rework_v1'
SCHEMA = 'c94.author-rework.v1'
_CHANNELS = ('location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale')


def _need(value, message):
    if not value:
        raise ValueError(message)


def _json(owner, key):
    value = owner.get(key)
    if value is None:
        return None
    _need(isinstance(value, str), '作者记录不是JSON文本：' + key)
    return json.loads(value)


def _coordinates(block):
    points = np.empty((len(block.data), 3), dtype=np.float32)
    block.data.foreach_get('co', points.ravel())
    return points


def _key_state(mesh):
    keys = mesh.shape_keys
    if keys is None:
        return None
    return {'use_relative': keys.use_relative, 'eval_time': keys.eval_time,
            'reference': keys.reference_key.name, 'properties': sig._plain(dict(keys.items())),
            'blocks': [[k.name, sig._array_hash(_coordinates(k)), k.relative_key.name,
                        k.vertex_group, k.mute, k.lock_shape, k.slider_min, k.slider_max,
                        k.interpolation, k.frame] for k in keys.key_blocks]}


def _source_state(obj):
    value = entry._source_state(obj)
    value['all_key_settings'] = _key_state(obj.data)
    value['animation_complete'] = face._animation(obj)
    value['mesh_animation_complete'] = face._animation(obj.data)
    value['Key_animation_complete'] = face._animation(obj.data.shape_keys) if obj.data.shape_keys else None
    return value


def _rig_state(rig):
    return {'rig': rig.session_uid, 'data': rig.data.session_uid,
            'contract': sig._rig_state(rig)['contract'], 'animation': face._animation(rig),
            'data_animation': face._animation(rig.data), 'properties': sig._plain(dict(rig.items())),
            'layout': copy.deepcopy(panel.read_layout(rig))}


def _canonical(value, rig):
    if isinstance(value, dict):
        if value.get('type') == 'Object' and value.get('name') == rig.name:
            return {'id': 'THIS_RIG'}
        return {k: _canonical(v, rig) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) == 3 and value[0] == 'Object' and value[1] == rig.name:
            return ['Object', 'THIS_RIG', value[2]]
        if len(value) == 3 and value[0] == 'Armature' and value[1] == rig.data.name:
            return ['Armature', 'THIS_RIG_DATA', value[2]]
        return [_canonical(v, rig) for v in value]
    return value


def _property_schema(owner,name):
    if name not in owner:return None
    value=owner[name]
    if type(value) not in (bool,int,float) or not math.isfinite(value):return None
    ui=owner.id_properties_ui(name).as_dict()
    return {'type':type(value).__name__,'min':ui.get('min'),'max':ui.get('max'),
            'subtype':ui.get('subtype')}


def _contract_context(rig):
    drivers={}
    for curve in rig.animation_data.drivers if rig.animation_data else ():
        match=re.match(r'pose\.bones\[("(?:[^"\\]|\\.)*")\]',curve.data_path)
        if not match:continue
        value={'path':curve.data_path,'index':curve.array_index,'mute':curve.mute,
            'extrapolation':curve.extrapolation,'driver':sig._rna(curve.driver),
            'variables':[[sig._rna(v),[sig._rna(t) for t in v.targets]] for v in curve.driver.variables],
            'modifiers':[sig._rna(m) for m in curve.modifiers],
            'keys':[sig._rna(k) for k in curve.keyframe_points],
            'samples':[list(k.co) for k in curve.sampled_points]}
        drivers.setdefault(json.loads(match.group(1)),[]).append(value)
    properties={}
    for row in mode_semantics.available(rig):
        name,key=row['bone'],row['property']
        properties.setdefault(name,{})[key]=_property_schema(rig.pose.bones[name],key)
    return {'drivers':drivers,'properties':properties}


def _bone_contract(rig, name, context=None):
    if name not in rig.pose.bones:
        return None
    context=context or _contract_context(rig)
    chain = []
    pb = rig.pose.bones[name]
    bone = pb.bone
    while bone:
        pose=rig.pose.bones[bone.name]
        constraints=[{k:v for k,v in sig._rna(c).items() if k!='show_expanded'} for c in pose.constraints]
        chain.append([bone.name, sig._plain(bone.matrix_local), bone.inherit_scale,
                      bone.use_inherit_rotation, bone.use_local_location, bone.use_connect,
                      pose.rotation_mode,constraints,context['drivers'].get(bone.name,[]),
                      context['properties'].get(bone.name,{})])
        bone = bone.parent
    return _canonical({'chain': chain}, rig)


def _equal_contract(a, b):
    """Native float32 generated rest data may differ by a few representation ULPs."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isfinite(a) and math.isfinite(b) and abs(a-b) <= 2e-6
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal_contract(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal_contract(a[k], b[k]) for k in a)
    return a == b


def _remap(value, mapping):
    if isinstance(value, bpy.types.ID):
        return mapping.get(value, value)
    if hasattr(value, 'to_dict'):
        value = value.to_dict()
    if isinstance(value, dict):
        return {k: _remap(v, mapping) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_remap(v, mapping) for v in value]
    return value


def _copy_rna(source, destination, mapping, *, skip=()):
    for prop in source.bl_rna.properties:
        name = prop.identifier
        if name == 'rna_type' or name in skip or prop.is_readonly or prop.type == 'COLLECTION':
            continue
        value = getattr(source, name)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            setattr(destination, name, value)
        elif prop.type == 'POINTER' and isinstance(value, bpy.types.ID):
            setattr(destination, name, mapping.get(value, value))
    if isinstance(source, (bpy.types.ID, bpy.types.PoseBone, bpy.types.Bone)):
        for key, value in source.items():
            destination[key] = _remap(value, mapping)


def _retarget(owner, mapping, collections=None):
    for key, value in owner.items():
        if isinstance(value, bpy.types.ID):
            owner[key] = mapping.get(value, value)
    ad = owner.animation_data
    if ad:
        for curve in ad.drivers:
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id in mapping:
                        target.id = mapping[target.id]
                    if isinstance(target.id, bpy.types.Armature):
                        for old,new in (collections or {}).items():
                            for ascii_only in (False,True):
                                prefix='collections_all['+json.dumps(old,ensure_ascii=ascii_only)+']'
                                if target.data_path.startswith(prefix):
                                    target.data_path='collections_all['+json.dumps(new,ensure_ascii=False)+']'+target.data_path[len(prefix):]


def _isolate_animation(source, target, label, own=None):
    ad=source.animation_data
    if ad is None:return None
    pending=[]
    if ad.nla_tracks:
        # Native boards in the first scope have no composed NLA. Never retain
        # a shared old Action while claiming this owner was independently moved.
        for track in list(target.animation_data.nla_tracks):target.animation_data.nla_tracks.remove(track)
        pending.append('NLA composition unsupported_pending; retained on source')
    action=ad.action
    if action is not None:
        _need(ad.action_slot is not None,'有Action但无明确slot，不能冒称独立迁移')
        copied=action.copy()
        if own:own(copied)
        copied.name=label+'__'+source.name+'__Action'
        slot=next((s for s in copied.slots if s.handle==ad.action_slot_handle),None)
        _need(slot is not None,'作者Action.copy未保原slot')
        target.animation_data.action=copied;target.animation_data.action_slot=slot
        return {'source':source.name,'target':target.name,'action':copied.name,'unsupported_pending':pending}
    return {'source':source.name,'target':target.name,'unsupported_pending':pending} if pending else None


def _mesh_supported(obj, rig):
    rebuild._local(bpy.context.scene, obj, 'MESH')
    _need(not obj.constraints and obj.animation_data is None and obj.data.animation_data is None,
          '首包Mesh只接原生Key动画、无对象/Mesh Action或对象约束；原输入保留')
    _need(obj.parent is None or (obj.parent == rig and obj.parent_type == 'OBJECT'),
          '首包仅接无父级或当前rig的OBJECT父级')
    mods = list(obj.modifiers)
    _need(mods and mods[0].type == 'ARMATURE' and mods[0].object == rig
          and all(m.type == 'SUBSURF' for m in mods[1:]), '需要唯一首位原rig Armature→可选Subsurf')
    arm = mods[0]
    _need(arm.use_vertex_groups and not arm.use_deform_preserve_volume and not arm.use_bone_envelopes
          and not arm.use_multi_modifier and not arm.vertex_group and arm.show_viewport,
          '首包只接普通LBS，不用删Key/动作绕过输入')


def _board(hub, rig, selected):
    record, actual, members = expressions._read(hub)
    _need(actual == rig and set(members) <= set(selected), '已知P1控制组的全部成员须明确纳入返工')
    if mouth.STATE_KEY in hub:
        _group, board, _rig, _members = mouth._read(hub)
        names = [board['root'], board['anchor'], board['pad']]
        names += [n for row in board['sliders'] for n in (row['anchor'], row['bone'])]
        kind = 'MOUTH'
    elif graph.STATE_KEY in hub:
        _group, board, _rig, _members = graph._read(hub)
        names = [board['root']] + [n for row in board['channels'] for n in (row['anchor'], row['bone'])]
        kind = 'GRAPH'
    else:
        board, names, kind = None, [], 'HUB'
    return {'hub': hub, 'record': copy.deepcopy(record), 'board': copy.deepcopy(board),
            'bones': names, 'kind': kind, 'members': members,
            'sidebar': copy.deepcopy(sidebar.read(hub))}


def _bag(action, slot):
    _need(len(action.layers) == 1 and len(action.layers[0].strips) == 1
          and action.layers[0].strips[0].type == 'KEYFRAME',
          'Action首包支持一个KEYFRAME层/条带；其它动画保原并列unsupported_pending')
    return action.layers[0].strips[0].channelbag(slot)


class Plan:
    def __init__(self, scene, source_rig, objects, reference, template_file, configuration, *, label, old_reference=None):
        _need(scene == bpy.context.scene and bpy.context.mode == 'OBJECT', '返工计划需要当前Scene对象模式')
        rebuild._local(scene, source_rig, 'ARMATURE'); rebuild._local(scene, reference, 'ARMATURE')
        _need(isinstance(objects, (list, tuple)) and objects and len(set(objects)) == len(objects),
              '明确给出无重复的生产Mesh/Key成员，不扫描场景接管其它角色')
        _need(isinstance(label, str) and label.strip(), '给出独立新角色名称')
        self.scene, self.source_rig, self.reference = scene, source_rig, reference
        self.objects, self.template_file, self.label = list(objects), str(Path(template_file).resolve()), label.strip()
        self.configuration = modules.validate(scene, reference, template_file, configuration)
        self.meta = rebuild._metadata(source_rig)
        old_reference = old_reference or scene.objects.get(self.meta.get('reference_object', self.meta.get('reference', '')))
        _need(old_reference is not None and old_reference.type == 'ARMATURE', '旧原参考须保留用于真实影响对照')
        self.old_reference = old_reference
        self.old_signature = positioning.reference_signature(old_reference)
        _need(self.old_signature == self.meta['reference_signature'], '旧参考与source生成记录不符；不能借新参考覆盖旧身份')
        self.new_signature = positioning.reference_signature(reference)
        old_sha = self.meta.get('base_template_sha256') or self.meta.get('template_sha256')
        new_sha = self.configuration['definition']['base_template']['expected_sha256']
        _need(old_sha is None or old_sha == new_sha, '旧模板字节身份与incoming base不同，需先解决来源兼容')
        self._state = 'prepared'; self._frame = (scene.frame_current, scene.frame_subframe)
        self._collections={};self._animations=[]
        self._owned=[]
        self._rig_before = _rig_state(source_rig)
        for obj in self.objects:
            _mesh_supported(obj, source_rig)
        self._objects_before = [_source_state(o) for o in self.objects]
        self._layout = copy.deepcopy(panel.read_layout(source_rig))
        self._hubs, self._unsupported_hubs = [], []
        for obj in bpy.data.objects:
            if obj.get('rig') == source_rig and expressions.STATE_KEY in obj:
                if all(obj.get('member_%03d' % i) in self.objects for i in range((_json(obj, expressions.STATE_KEY) or {}).get('member_count', 0))):
                    self._hubs.append(_board(obj, source_rig, self.objects))
                else:
                    self._unsupported_hubs.append(obj.name)
        self._author_sources = []
        for item in self._hubs:
            for key in item['hub'].keys():
                value = item['hub'][key]
                if key.startswith('author_source_') and isinstance(value, bpy.types.Object):
                    _mesh_supported(value, source_rig)
                    if value not in self.objects and value not in self._author_sources:
                        self._author_sources.append(value)
        self._author_before = [_source_state(o) for o in self._author_sources]
        self._hub_before = [(sig._plain(dict(h['hub'].items())), face._animation(h['hub'])) for h in self._hubs]
        old = old_reference.data.bones; new = reference.data.bones
        names = set(old.keys()) | set(new.keys())
        changed = [n for n in sorted(names) if n not in old or n not in new or
                   not _equal_contract([sig._plain(old[n].matrix_local), old[n].length,
                                        old[n].parent.name if old[n].parent else None],
                                       [sig._plain(new[n].matrix_local), new[n].length,
                                        new[n].parent.name if new[n].parent else None])]
        removed = set(self.configuration['definition']['removed_bones']) & set(self.meta['reference_mapping'].values())
        self._report = {'schema': SCHEMA, 'state': 'prepared', 'source_rig': source_rig.name,
            'old_reference': old_reference.name, 'reference': reference.name,
            'old_reference_signature': self.old_signature, 'incoming_reference_signature': self.new_signature,
            'reference_identity_equal': self.old_signature == self.new_signature,
            'changed_reference_bones': changed, 'closed_deform_semantics': sorted(removed),
            'sources': [o.name for o in self.objects], 'author_sources': [o.name for o in self._author_sources],
            'known_hubs': [h['hub'].name for h in self._hubs], 'unsupported_pending_hubs': list(self._unsupported_hubs),
            'effects': {'Keys': 'preserve native library independently; affected capture validity decided after generation',
                        'weights': 'semantic copy/explicit close merge; changed rest requires deformation review',
                        'actions': 'copy compatible channels only; changed-space channels require retarget',
                        'layout': 'retain valid original row IDs/order/labels/visible/defaults; append only new controls',
                        'dynamics': 'requires_rebake; old outputs and inputs retained, old authority not transferred'},
            'visual': 'pending_review', 'scope': 'explicit_author_rework_with_reported_incompatible_redo'}

    @property
    def report(self):
        return copy.deepcopy(self._report)

    def _own(self,block):
        self._owned.append(block)
        if isinstance(block,bpy.types.Mesh) and block.shape_keys:self._owned.append(block.shape_keys)
        return block

    def _check(self):
        _need(self._state == 'prepared' and bpy.context.scene == self.scene
              and (self.scene.frame_current, self.scene.frame_subframe) == self._frame,
              '返工计划已结束，或Scene/时刻改变；重新准备影响计划')
        _need(positioning.reference_signature(self.old_reference) == self.old_signature
              and positioning.reference_signature(self.reference) == self.new_signature,
              '旧/新参考在准备后改变；重新准备计划')
        modules.validate(self.scene, self.reference, self.template_file, self.configuration)
        _need(_rig_state(self.source_rig) == self._rig_before
              and [_source_state(o) for o in self.objects] == self._objects_before
              and [_source_state(o) for o in self._author_sources] == self._author_before
              and [(sig._plain(dict(h['hub'].items())), face._animation(h['hub'])) for h in self._hubs] == self._hub_before,
              '源作者Mesh/Key/权重/Action/面板或控制引用改变；拒绝旧计划提交')

    def preview(self):
        self._check()
        return self.report

    def _copy_mesh(self, source, rig, collection, mapping, index):
        obj = self._own(source.copy()); obj.data = self._own(source.data.copy()); mapping[source] = obj; mapping[source.data] = obj.data
        obj.name = self.label+'__作者_%02d' % index
        collection.objects.link(obj)
        if source.data.shape_keys:
            _need(obj.data.shape_keys != source.data.shape_keys, 'Mesh.copy未隔离Key，不修改原源')
            mapping[source.data.shape_keys] = obj.data.shape_keys
        if obj.parent == self.source_rig:
            world = obj.matrix_world.copy(); obj.parent = rig; obj.matrix_world = world
        obj.modifiers[0].object = rig
        old_names, old_rows = rebuild._weights(source)
        old_map = self.meta['reference_mapping']; new_map = rebuild._metadata(rig)['reference_mapping']
        inverse = {v:k for k,v in new_map.items()}; merge = self.configuration['definition']['merge_mapping']
        destinations = {}
        for name in old_names:
            bone = self.source_rig.data.bones.get(name)
            _need(not bone or not bone.use_deform or name in old_map, '存在无语义的实际旧形变组：'+name)
            if name in old_map:
                semantic = old_map[name]
                target = semantic if semantic in inverse else merge.get(semantic)
                _need(target in inverse, '关闭骨缺少显式主骨映射：'+semantic)
                destinations[name] = inverse[target]
            else:
                destinations[name] = name
        for name,target in destinations.items():
            _need(name==target or target not in old_names or target in old_map,
                  '新形变目标与原非目标组同名，不能合并其语义：'+target)
        masks = {k.vertex_group for k in source.data.shape_keys.key_blocks if k.vertex_group} if source.data.shape_keys else set()
        output = {name:np.zeros(len(old_rows), dtype=np.float64) for name in set(destinations.values())}
        changed = {name for name,dst in destinations.items() if name != dst}
        targets = {destinations[n] for n in changed}
        _need(not any(g.lock_weight and g.name in changed|targets for g in source.vertex_groups),
              '关闭归并源/目标组已锁定；原锁保留，不擅自归一化')
        for i,row in enumerate(old_rows):
            for name,value in row.items(): output[destinations[name]][i] += value
        for name in masks & changed:
            _need(name not in output, 'Key mask退休列与新deform名称冲突')
            _need(name not in rig.data.bones or not rig.data.bones[name].use_deform,
                  '退休Key mask列仍会参与新骨架形变，不能重复质量')
            output[name] = np.asarray([r.get(name,0.) for r in old_rows])
        _need(all(np.isfinite(a).all() and np.all(a.astype(np.float32) <= 1) for a in output.values()),
              '归并质量超过float32的0..1；不截断或归一化')
        for group in list(obj.vertex_groups): obj.vertex_groups.remove(group)
        order = [n for n in old_names if n in output] + sorted(set(output)-set(old_names))
        for name in order:
            group=obj.vertex_groups.new(name=name)
            for i,value in enumerate(output[name].astype(np.float32)):
                if value > 0: group.add([i], float(value), 'REPLACE')
            if name in source.vertex_groups:group.lock_weight=source.vertex_groups[name].lock_weight
        archive={k:sig._plain(source[k]) for k in source.keys() if k.startswith('c94_')}
        for key in list(obj.keys()):
            if key.startswith('c94_'):del obj[key]
        obj['c94_skinning_v1']=json.dumps({'schema':'c94.skinning.v1','rig':rig.name,'source':source.name,
            'method':'author_rework_semantic_copy','source_managed_archive':archive},ensure_ascii=False,allow_nan=False)
        _need(sig._mesh_signature(obj.data)==sig._mesh_signature(source.data)
              and _key_state(obj.data)==_key_state(source.data),'复制改变了原几何/UV/material/Key设置或mask')
        actual_names,actual_rows=rebuild._weights(obj)
        _need(actual_names==order and all(all(row.get(name,0.)==float(np.float32(output[name][i])) for name in order)
              for i,row in enumerate(actual_rows)),'实际原生权重未保持声明的float32归并结果')
        non_target=set(old_names)-changed-targets
        _need(all(all(row.get(name,0.)==old_rows[i].get(name,0.) for name in non_target)
                  for i,row in enumerate(actual_rows)),'非目标权重列改变')
        before_mass=np.asarray([sum(v for n,v in r.items() if n in old_map) for r in old_rows])
        after_mass=np.asarray([sum(v for n,v in r.items() if n in new_map) for r in actual_rows])
        report={'source':source.name,'object':obj.name,'keys':len(obj.data.shape_keys.key_blocks) if obj.data.shape_keys else 0,
                'weight_map':destinations,'retired_mask_columns':sorted(masks & changed),
                'merged_positive_mass':{n:sum(r.get(n,0.) for r in old_rows) for n in changed},
                'deform_row_mass_max_float32_difference':float(np.max(abs(before_mass-after_mass),initial=0.)),
                'non_target_columns_exact':True,'merge_float32_max_error':max((float(np.max(abs(a-a.astype(np.float32)))) for a in output.values()),default=0.)}
        return obj,report

    def _copy_board(self, item, rig, collection, mapping):
        oldrig=self.source_rig; oldhub=item['hub']; board=item['board']
        if board:
            display=oldhub['board_display']; name=self.label+'__'+item['kind']+'板'
            _need(name not in bpy.data.collections, '新显示集合名称冲突')
            newdisplay=self._own(bpy.data.collections.new(name)); collection.children.link(newdisplay);mapping[display]=newdisplay
            self._collections[board['collection']]=name
            newbc=rig.data.collections.new(name,parent=rig.data.collections_all[modes.SPACES['panel']])
            active=bpy.context.view_layer.objects.active;selected=list(bpy.context.selected_objects)
            try:
                for obj in selected:obj.select_set(False)
                rig.select_set(True);bpy.context.view_layer.objects.active=rig
                bpy.ops.object.mode_set(mode='EDIT')
                for n in item['bones']:
                    _need(n not in rig.data.edit_bones,'P1板骨名称与incoming骨架冲突：'+n)
                    old=oldrig.data.bones[n];new=rig.data.edit_bones.new(n)
                    new.matrix=old.matrix_local.copy();new.length=old.length;new.use_deform=False
                for n in item['bones']:
                    old=oldrig.data.bones[n];new=rig.data.edit_bones[n]
                    new.parent=rig.data.edit_bones[old.parent.name] if old.parent else None
                    new.use_connect=old.use_connect;new.inherit_scale=old.inherit_scale
                bpy.ops.object.mode_set(mode='OBJECT')
            finally:
                if rig.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
                rebuild._restore_selection(active,selected)
            for n in item['bones']:
                newbc.assign(rig.data.bones[n])
            for old in display.objects:
                new=self._own(old.copy());new.name=self.label+'__'+old.name
                if old.data:new.data=self._own(old.data.copy());mapping[old.data]=new.data
                newdisplay.objects.link(new);new.hide_set(old.hide_get());mapping[old]=new
                copied=_isolate_animation(old,new,self.label,self._own)
                if copied:self._animations.append(copied)
            for n in item['bones']:
                old=oldrig.pose.bones[n];new=rig.pose.bones[n]
                _copy_rna(old,new,mapping,skip=('name','custom_shape_transform'))
                for c in old.constraints:
                    target=new.constraints.new(c.type);_copy_rna(c,target,mapping)
            for old in display.objects:
                new=mapping[old]
                if new.parent in mapping:new.parent=mapping[new.parent]
                _retarget(new,mapping,self._collections)
        hub=self._own(oldhub.copy());hub.name=self.label+'__'+oldhub.name;collection.objects.link(hub);mapping[oldhub]=hub
        copied=_isolate_animation(oldhub,hub,self.label,self._own)
        if copied:self._animations.append(copied)
        if board:
            record=copy.deepcopy(board);record['collection']=mapping[oldhub['board_display']].name
            hub[mouth.STATE_KEY if item['kind']=='MOUTH' else graph.STATE_KEY]=json.dumps(record,ensure_ascii=False)
            hub['board_display']=mapping[oldhub['board_display']]
        for key,value in oldhub.items():
            if isinstance(value,bpy.types.ID):hub[key]=mapping.get(value,value)
        _retarget(hub,mapping,self._collections)
        return hub

    def _copy_action(self, rig, contracts):
        old=self.source_rig.animation_data
        if old is None or old.action is None:
            return {'status':'none','copied_channels':[],'needs_retarget':[],'unsupported_pending':[]}
        if old.use_tweak_mode:
            return {'status':'unsupported_pending','copied_channels':[],'needs_retarget':[],
                    'unsupported_pending':['NLA tweak-mode active Action retained on source; exit tweak mode and prepare again for base Action migration']}
        source=old.action; slot=old.action_slot
        bag=_bag(source,slot);compatible=[];pending=[];unsupported=[]
        if old.nla_tracks:
            unsupported.append({'status':'unsupported_pending','reason':'NLA composition retained on source; base active Action compatibility assessed independently'})
        known={(r['bone'],r['property']) for r in mode_semantics.available(self.source_rig)}
        for f in bag.fcurves if bag else ():
            match=re.fullmatch(r'pose\.bones\[("(?:[^"\\]|\\.)*")\]\.(location|scale|rotation_euler|rotation_quaternion|rotation_axis_angle)',f.data_path)
            name=json.loads(match.group(1)) if match else None
            prop=re.fullmatch(r'pose\.bones\[("(?:[^"\\]|\\.)*")\]\[("(?:[^"\\]|\\.)*")\]',f.data_path)
            prop_name=json.loads(prop.group(1)) if prop else None
            prop_key=json.loads(prop.group(2)) if prop else None
            prop_ok=(prop and (prop_name,prop_key) in known and prop_name in rig.pose.bones and f.array_index==0
                and _property_schema(self.source_rig.pose.bones[prop_name],prop_key) is not None
                and _property_schema(self.source_rig.pose.bones[prop_name],prop_key)==_property_schema(rig.pose.bones[prop_name],prop_key))
            if name in contracts and contracts[name] or prop_ok:compatible.append((f.data_path,f.array_index))
            elif match:pending.append({'path':f.data_path,'index':f.array_index,'status':'needs_retarget',
                                      'reason':'missing_or_changed_control_space'})
            else:unsupported.append({'path':f.data_path,'index':f.array_index,'status':'unsupported_pending',
                                     'reason':'unknown_or_removed_property_or_channel_not_implemented'})
        if not compatible:
            return {'status':'needs_retarget' if pending else 'unsupported_pending','copied_channels':[],'needs_retarget':pending,'unsupported_pending':unsupported}
        action=self._own(source.copy());action.name=self.label+'__作者Action'
        newslot=next((s for s in action.slots if s.handle==slot.handle),None)
        _need(newslot is not None,'Action.copy未保原slot')
        target=_bag(action,newslot)
        for curve in list(target.fcurves):
            if (curve.data_path,curve.array_index) not in compatible:target.fcurves.remove(curve)
        ad=rig.animation_data_create();ad.action=action;ad.action_slot=newslot
        for field in ('action_blend_type','action_extrapolation','action_influence'):
            setattr(ad,field,getattr(old,field))
        return {'status':'copied_compatible_subset' if pending or unsupported else 'copied_compatible',
                'action':action.name,'source_action':source.name,'slot':newslot.identifier,
                'copied_channels':[{'path':p,'index':i} for p,i in compatible],
                'needs_retarget':pending,'unsupported_pending':unsupported}

    def execute(self):
        self._check()
        before=modules._ids();active=bpy.context.view_layer.objects.active;selected=list(bpy.context.selected_objects)
        self._owned=[];self._collections={};self._animations=[]
        try:
            result=generation.generate(self.scene,self.reference,self.template_file,label=self.label,configuration=self.configuration,owned_ids=self._owned)
            rig,collection=result['rig'],result['collection'];mapping={self.source_rig:rig,self.source_rig.data:rig.data}
            copies=[];mesh_reports=[]
            for i,source in enumerate(self.objects+self._author_sources):
                obj,report=self._copy_mesh(source,rig,collection,mapping,i+1);copies.append(obj);mesh_reports.append(report)
            # Body-independent face compatibility includes every physical control's ancestors.
            board_bones={n for item in self._hubs for n in item['bones']}
            old_face=(set(modes._bone_names(self.source_rig)) | {'表情面板'})-board_bones if self.source_rig.get(modes.STATE_KEY) else set()
            # Check actual skin consumers as well as physical controls. The
            # known owned board is copied later; it is not a missing core face.
            for source in self.objects:
                if not source.data.shape_keys:continue
                names,rows=rebuild._weights(source)
                old_face.update(n for n in names if n in self.source_rig.data.bones
                    and self.source_rig.data.bones[n].use_deform and any(r.get(n,0.)>0 for r in rows))
            old_context,new_context=_contract_context(self.source_rig),_contract_context(rig)
            face_ok=bool(old_face) and all(_equal_contract(_bone_contract(self.source_rig,n,old_context),_bone_contract(rig,n,new_context)) for n in old_face)
            face_weights_same=all(not any(name!=target and any(row.get(name,0.)>0 for row in rebuild._weights(source)[1])
                                   for name,target in report['weight_map'].items())
                                  for source,report in zip(self.objects,mesh_reports) if source.data.shape_keys)
            face_ok=face_ok and face_weights_same
            hubs=[]
            if face_ok:
                for item in self._hubs:hubs.append(self._copy_board(item,rig,collection,mapping))
            known_members={o for item in self._hubs for o in item['members']}
            for source in self.objects+self._author_sources:
                obj=mapping[source]
                if obj.data.shape_keys and (not face_ok or source not in known_members):
                    # A preserved native Key library is not permission to keep
                    # an old unknown hub, surface clock or sampled Action live.
                    obj.data.shape_keys.animation_data_clear()
                    for k in obj.data.shape_keys.key_blocks:k.value=0.
                elif obj.data.shape_keys:
                    copied=_isolate_animation(source.data.shape_keys,obj.data.shape_keys,self.label,self._own)
                    if copied:self._animations.append(copied)
            for source in self.objects+self._author_sources:
                obj=mapping[source]
                _retarget(obj,mapping,self._collections)
                if obj.data.shape_keys:_retarget(obj.data.shape_keys,mapping,self._collections)
            for old in list(mapping):
                if isinstance(old,bpy.types.Object) and old not in self.objects+self._author_sources and old!=self.source_rig:
                    _retarget(mapping[old],mapping,self._collections)
            if face_ok:
                neutral=_json(self.source_rig,modes.STATE_KEY)['neutral']
                for n,row in neutral['bones'].items():
                    if n in rig.pose.bones:
                        for field in _CHANNELS:setattr(rig.pose.bones[n],field,row[field])
                        rig.pose.bones[n].rotation_mode=row['rotation_mode']
                for prop in neutral['properties']:rig.pose.bones[prop['bone']][prop['key']]=prop['value']
                declarations=[]
                for row in neutral['manual_keys']:
                    old=bpy.data.shape_keys.get(row['data'])
                    _need(old in mapping,'手工Key中立声明未包含在明确迁移成员中')
                    mapped=copy.deepcopy(row);mapped['data']=mapping[old].name;declarations.append(mapped)
                modes.capture_neutral(rig,manual_keys=declarations)
                fingerprint=sig._hash(modes._read(rig)['neutral'])
                for hub in hubs:
                    value=_json(hub,expressions.STATE_KEY);value['neutral_fingerprint']=fingerprint
                    hub[expressions.STATE_KEY]=json.dumps(value,ensure_ascii=False)
                old_library=_json(self.source_rig,library.STATE_KEY)
                if old_library:
                    value=copy.deepcopy(old_library);value['neutral_fingerprint']=fingerprint;value['previous']=None
                    rig[library.STATE_KEY]=json.dumps(value,ensure_ascii=False,allow_nan=False)
            elif self.source_rig.get(library.STATE_KEY):
                rig['c94_author_rework_pose_library_archive_v1']=json.dumps({
                    'status':'requires_rebase','source_rig':self.source_rig.name,
                    'library':_json(self.source_rig,library.STATE_KEY)},ensure_ascii=False,allow_nan=False)
            old_context,new_context=_contract_context(self.source_rig),_contract_context(rig)
            contracts={n:_equal_contract(_bone_contract(self.source_rig,n,old_context),_bone_contract(rig,n,new_context))
                       for n in self.source_rig.pose.bones.keys() if n in rig.pose.bones}
            copied_properties=[]
            incoming={(r['bone'],r['property']) for r in mode_semantics.available(rig)}
            for row in mode_semantics.available(self.source_rig):
                name,key=row['bone'],row['property']
                if (name,key) in incoming and _property_schema(self.source_rig.pose.bones[name],key)==_property_schema(rig.pose.bones[name],key):
                    rig.pose.bones[name][key]=self.source_rig.pose.bones[name][key];copied_properties.append([name,key])
            transferred=[];widgets={}
            for n,compatible in contracts.items():
                if not compatible:continue
                old=self.source_rig.pose.bones[n];new=rig.pose.bones[n]
                for field in _CHANNELS:setattr(new,field,getattr(old,field))
                for field in ('custom_shape_translation','custom_shape_rotation_euler','custom_shape_scale_xyz','use_custom_shape_bone_size'):
                    setattr(new,field,getattr(old,field))
                if old.custom_shape:
                    if old.custom_shape not in widgets:
                        if old.custom_shape in mapping:widgets[old.custom_shape]=mapping[old.custom_shape]
                        else:
                            shape=self._own(old.custom_shape.copy());shape.name=self.label+'__'+old.custom_shape.name
                            if shape.data:shape.data=self._own(old.custom_shape.data.copy())
                            collection.objects.link(shape);shape.hide_set(old.custom_shape.hide_get());widgets[old.custom_shape]=shape
                    new.custom_shape=widgets[old.custom_shape]
                if old.custom_shape_transform and old.custom_shape_transform.name in rig.pose.bones:
                    new.custom_shape_transform=rig.pose.bones[old.custom_shape_transform.name]
                transferred.append(n)
            generated=panel.read_layout(rig);layout=copy.deepcopy(self._layout);kept=[];retired=[]
            for row in layout['rows']:
                if row['kind']=='collection' and row.get('collection') in self._collections:
                    row['collection']=self._collections[row['collection']]
                valid=(row['kind']=='collection' and row.get('collection') in rig.data.collections_all
                       or row['kind']=='property' and row.get('bone') in rig.pose.bones and row.get('property') in rig.pose.bones[row['bone']]
                       or row['kind']=='action')
                if valid:kept.append(row)
                else:retired.append({'row':row,'status':'requires_rebuild','reason':'explicit_removed_module_or_missing_reference'})
            identities={(r['kind'],r.get('collection'),r.get('bone'),r.get('property'),r.get('action'),r.get('limb'),r.get('side'),r.get('target')) for r in kept}
            added=[r for r in generated['rows'] if (r['kind'],r.get('collection'),r.get('bone'),r.get('property'),r.get('action'),r.get('limb'),r.get('side'),r.get('target')) not in identities]
            layout.update(label=self.label,editing=False,rows=kept+added,
                          defaults={**generated['defaults'],**{n:v for n,v in layout['defaults'].items() if contracts.get(n)}})
            panel._save(rig,layout)
            action_report=self._copy_action(rig,contracts)
            bpy.context.view_layer.update()
            if face_ok:
                for item,hub in zip(self._hubs,hubs):
                    expressions._read(hub)
                    if item['kind']=='MOUTH':mouth._read(hub)
                    elif item['kind']=='GRAPH':graph._read(hub)
            # No driver in copied native playback may silently depend on old author IDs.
            old_playback={self.source_rig,*[h['hub'] for h in self._hubs],*self.objects,*self._author_sources}
            for obj in copies+hubs+[mapping[o] for o in mapping if isinstance(o,bpy.types.Object) and o not in self.objects+self._author_sources and o!=self.source_rig]:
                for owner in (obj,obj.data if obj.type=='MESH' else None,obj.data.shape_keys if obj.type=='MESH' else None):
                    if owner and owner.animation_data:
                        _need(all(t.id not in old_playback for f in owner.animation_data.drivers for v in f.driver.variables for t in v.targets),
                              '新候选仍消费旧rig/旧作者成员，拒绝伪迁移')
            self._check()
            report=self.report
            report.update(state='candidate',rig=rig.name,meshes=mesh_reports,source_unchanged=True,
                face_contract='compatible' if face_ok else 'requires_recapture',
                hubs=[h.name for h in hubs],
                pose_library=('copied_verified_inputs_previous_cleared' if face_ok else 'archived_independent_requires_rebase') if self.source_rig.get(library.STATE_KEY) else 'none',
                face_pending=[] if face_ok else [{'hub':h['hub'].name,'status':'requires_recapture',
                    'reason':'physical_face_control_or_Key_weight_contract_changed','board_rebuild':'unsupported_pending'} for h in self._hubs],
                control_tweaks=transferred,action=action_report,
                native_properties=copied_properties,
                owner_animations=self._animations,
                layout={'kept_row_ids':[r['id'] for r in kept],'added_row_ids':[r['id'] for r in added],'retired_rows':retired},
                dynamics={'status':'requires_rebake','source_records':[k for k in self.source_rig.keys() if k in {'c94_elastic_v1','c94_surface_dynamic_v1'}]})
            rig[STATE_KEY]=json.dumps(report,ensure_ascii=False,allow_nan=False)
            self._report=report;self._state='executed'
            return {'rig':rig,'collection':collection,'objects':copies[:len(self.objects)],
                    'author_sources':copies[len(self.objects):],'hubs':hubs,'report':copy.deepcopy(report)}
        except Exception:
            modules._cleanup(before,active,selected,owned_ids=self._owned)
            self._state='failed'
            raise
        finally:
            rebuild._restore_selection(active,selected)


def prepare(scene,source_rig,objects,reference,template_file,configuration,*,label,old_reference=None):
    return Plan(scene,source_rig,objects,reference,template_file,configuration,label=label,old_reference=old_reference)
