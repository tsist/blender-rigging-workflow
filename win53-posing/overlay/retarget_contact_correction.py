# SPDX-License-Identifier: GPL-3.0-or-later
"""One explicitly selected contact issue -> separate native-C94 Action.

No automatic publication or approval. Preview is reversible through the
existing retarget_action core. A RAM-only review capability is issued by the
explicit confirmation operator; JSON/Action metadata never restores authority.
This module does not repair rig topology, weights or the native matching core.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass

import bpy
import numpy as np
from mathutils import Vector, Matrix

from . import retarget_action as rt
from . import retarget_dynamic as dynamic
from . import pose_switching as pose
from . import pose_switching_leg as leg
from . import retarget_contact_samples as samples
from . import retarget_observed as observed
from . import retarget_contact_anchors as anchors
from .retarget_contact import Aligner

META = 'c94_retarget_contact_correction_v1'
VERSION = '0.1.0'
_plans = {}
_approvals = {}
KINDS = {'target_slip', 'target_penetration', 'contact_transfer_gap'}


class CorrectionError(ValueError):
    pass


def require(value, message):
    if not value:
        raise CorrectionError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _rna_scalar_fields(value, *, skip=()):
    out = {}
    for prop in value.bl_rna.properties:
        name = prop.identifier
        if name == 'rna_type' or name in skip or prop.is_readonly:
            continue
        current = getattr(value, name)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'}:
            if getattr(prop, 'is_array', False):
                out[name] = list(current)
            elif isinstance(current, (str, int, float, bool)):
                out[name] = current
            elif isinstance(current, set):
                out[name] = sorted(current)
    return out


def action_data(action):
    """Full authored FCurve keys/handles/modifiers, not sampled motion equality."""
    rows = []
    for layer in action.layers:
        strips = []
        for strip in layer.strips:
            bags = []
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag is None:
                    continue
                curves = []
                for fc in bag.fcurves:
                    curves.append({'path': fc.data_path, 'index': fc.array_index,
                        'group': {'name':fc.group.name,'mute':bool(getattr(fc.group,'mute',False))} if fc.group else None,
                        'settings': _rna_scalar_fields(fc, skip={'color', 'select', 'lock'}),
                        'keys': [_rna_scalar_fields(k, skip={'select_control_point', 'select_left_handle', 'select_right_handle'})
                                 for k in fc.keyframe_points],
                        'sampled': [list(k.co) for k in fc.sampled_points],
                        'modifiers': [{**_rna_scalar_fields(modifier),
                            'envelope_points': [_rna_scalar_fields(p) for p in modifier.control_points]
                                if hasattr(modifier, 'control_points') else []} for modifier in fc.modifiers]})
                bags.append({'slot': slot.identifier, 'curves': curves})
            strips.append({'type': strip.type, 'settings': _rna_scalar_fields(strip), 'bags': bags})
        rows.append({'name': layer.name, 'settings': _rna_scalar_fields(layer), 'strips': strips})
    return {'slots': [{'identifier': s.identifier, 'type': s.target_id_type} for s in action.slots], 'layers': rows,
            'range_settings': {key:getattr(action,key) for key in ('use_frame_range','frame_start','frame_end','use_cyclic') if hasattr(action,key)}}


def action_fingerprint(action):
    return digest(action_data(action))


def _curve_index(action):
    return {(bag['slot'], c['path'], c['index']): c
        for layer in action_data(action)['layers'] for strip in layer['strips']
        for bag in strip['bags'] for c in bag['curves']}


def _action_diff(before, after):
    a, b = _curve_index(before), _curve_index(after)
    return [{'slot': key[0], 'path': key[1], 'index': key[2],
             'before': a.get(key), 'after': b.get(key)}
            for key in sorted(a.keys() | b.keys()) if a.get(key) != b.get(key)]


def _mesh_signature(mesh):
    return {'object': mesh.name, 'data': mesh.data.name,
        'matrix_world': [list(row) for row in mesh.matrix_world],
        'topology': samples._topology_identity(mesh.data),
        'vertices': digest([list(v.co) for v in mesh.data.vertices]),
        'weights': samples._weight_digest(mesh),
        'shape_keys': bool(mesh.data.shape_keys),
        'modifiers': [{'type': m.type, 'object': m.object.name if m.type == 'ARMATURE' and m.object else None,
                       'viewport': m.show_viewport, 'render': m.show_render} for m in mesh.modifiers]}


def _read_patch(plan):
    if anchors.mode(plan.calibration)==anchors.MODE:
        point=anchors.read_points(plan.scene,plan.target,plan.calibration['anchors'])[plan.calibration['side']][plan.region]
        normal=Vector(plan.calibration['plane']['normal'])
        return point,normal.dot(point)-plan.calibration['plane']['d']
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = plan.mesh.evaluated_get(graph)
    indices = plan.calibration['patches'][plan.region]
    require(len(evaluated.data.vertices) == len(plan.mesh.data.vertices), '求值拓扑改变，拒绝足底修正')
    points = [evaluated.matrix_world @ evaluated.data.vertices[i].co for i in indices]
    center = sum(points, Vector()) / len(points)
    n = Vector(plan.calibration['plane']['normal'])
    minimum = min(n.dot(p) - plan.calibration['plane']['d'] for p in points)
    return center, minimum


def _measurement_fingerprint(plan):
    if anchors.mode(plan.calibration)==anchors.MODE:
        return {'measurement_mode':anchors.MODE,
            'anchor_identity':anchors.identity(plan.scene,plan.target,plan.calibration['anchors'])}
    return {'mesh':_mesh_signature(plan.mesh)}


def _prepare_measurement(scene,target,calibration):
    if anchors.mode(calibration)==anchors.MODE:
        require(calibration.get('coverage_status')==anchors.COVERAGE and calibration.get('rig')==target.name,
                '骨锚点修正需要明确目标与三点校准，不能使用表面sole状态')
        current=anchors.identity(scene,target,calibration.get('anchors'))
        require(current==calibration.get('anchor_identity'),'骨锚点/rest身份已变化，请重新检测')
        return None
    require(calibration.get('coverage_status')=='explicit_calibrated_complete',
            'coverage_incomplete：必须提供完整且显式校准的可见/夹具脚底，不能自动推断')
    mesh=bpy.data.objects.get(calibration['mesh'])
    require(mesh and mesh.type=='MESH' and mesh.name in scene.objects and mesh.data.shape_keys is None,
            '当前修正仅支持同Scene的显式无Key蒙皮夹具/足底')
    patches=samples._normalize_patches({calibration['side']:calibration['patches']})
    whitelist=samples._normalize_whitelist(calibration['group_whitelist'],patches)
    samples._inspect_mesh(scene,mesh,target)
    samples._validate_indices_and_weights(mesh,patches,whitelist)
    return mesh


def _validate_surface_detection_identity(target, base, base_slot, calibration,
                                         detection_report=None):
    """Bind a surface calibration to the exact detection-time mesh/object.

    Old records without this identity are rejected; the current mesh is never
    used to sign them retroactively.
    """
    require(anchors.mode(calibration) == 'surface', '不是表面模式校准')
    detection_mesh = calibration.get('detection_mesh_identity')
    require(isinstance(detection_mesh, dict) and detection_mesh,
            '旧表面校准缺少检测时Mesh身份，请重新检测；不能用当前Mesh补签')
    object_inputs = calibration.get('detection_object_inputs')
    require(isinstance(object_inputs, dict),
            '旧表面校准缺少检测时对象输入身份，请重新检测')
    mesh_name = calibration.get('mesh')
    require(isinstance(mesh_name, str) and mesh_name, '表面校准缺少明确Mesh名称')
    mesh = bpy.data.objects.get(mesh_name)
    require(mesh is not None and mesh.type == 'MESH' and mesh.name in bpy.context.scene.objects,
            '目标表面Mesh不存在或不在当前Scene，请重新检测')
    require(_mesh_signature(mesh) == detection_mesh,
            '目标Mesh顶点/权重/拓扑与检测时不同，请重新检测')
    require(_object_input_stamp(target, base, base_slot) == object_inputs,
            '目标对象输入/父空间与检测时不同，请重新检测')
    if detection_report is not None:
        reference = detection_report.get('target', {}).get('reference', {})
        require(reference.get('mesh') == mesh_name,
                '检测报告Mesh与校准Mesh不同，请重新检测')
        require(reference.get('object_inputs') == object_inputs,
                '检测报告对象输入身份与校准不同或旧报告缺身份，请重新检测')
    return mesh


def _bind(target, action, slot):
    ad = target.animation_data_create()
    ad.action = action
    ad.action_slot = slot
    ad.action_blend_type = 'REPLACE'
    ad.action_influence = 1.0
    ad.action_extrapolation = 'HOLD'
    ad.use_nla = False


def _set_frame(frame):
    bpy.context.scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))


def _active_curves(action, slot):
    return [fc for layer in action.layers for strip in layer.strips
            for bag in [strip.channelbag(slot)] if bag is not None for fc in bag.fcurves
            if not fc.mute and not (fc.group and getattr(fc.group,'mute',False))]


def _object_input_stamp(target, action, slot):
    """Frame-independent Object inputs; animated components are guarded by
    the complete Action and by frame-local evaluation in Plan.validate.
    Object drivers and animated parent spaces need a separate adapter.
    """
    animated={(fc.data_path,fc.array_index) for fc in _active_curves(action,slot)}
    return {'rotation_mode':target.rotation_mode,
        'parent':target.parent.name if target.parent else None,
        'parent_world':[list(row) for row in target.parent.matrix_world] if target.parent else None,
        'parent_type':target.parent_type,'parent_bone':target.parent_bone,
        'parent_inverse':[list(row) for row in target.matrix_parent_inverse],
        'unanimated_channels':{field:[None if (field,index) in animated else float(value)
            for index,value in enumerate(getattr(target,field))] for field in samples._OBJECT_CHANNELS}}


def _validate_frame_inputs(plan):
    if plan.status in {'preview','applied'}:
        current,slot=plan.action,plan.slot
    else:
        current,slot=plan.base,plan.base_slot
    curves={(fc.data_path,fc.array_index):fc for fc in _active_curves(current,slot)}
    frame=bpy.context.scene.frame_current_final
    settings=plan.target.pose.bones['骨架设置']
    for key,value in plan.base_mode_inputs.items():
        path=settings.path_from_id()+'['+json.dumps(key,ensure_ascii=False)+']'
        expected=curves[(path,0)].evaluate(frame) if (path,0) in curves else value
        require(abs(float(settings[key])-expected) <= 1e-6, '显式基础模式输入被外部改变，旧确认失效：'+key)
    for field in samples._OBJECT_CHANNELS:
        for index,value in enumerate(getattr(plan.target,field)):
            curve=curves.get((field,index))
            if curve:
                require(abs(float(value)-curve.evaluate(frame)) <= 1e-6,
                        'Object Action输入被外部改写，旧确认失效：'+field)


def _move_main_without_rotation_scale(main, matrix, channels):
    main.matrix = matrix
    for field in ('rotation_euler','rotation_quaternion','rotation_axis_angle','scale'):
        if tuple(getattr(main,field)) != channels[field]:
            setattr(main,field,channels[field])
    main.rotation_mode = channels['rotation_mode']


def solve_fk_point(target, *, side, read_point, desired, desired_foot_matrix,
                   tolerance_m=.0001, max_step_radians=.35, iterations=6):
    """Bounded native FK rotation inverse; no FK/IK switch or locations.

    read_point observes one explicit world-space point/patch center. Native
    limits stay active. A failed solve returns unreachable; caller rolls back
    through the existing shared snapshot core.
    """
    spec, settings, mode = leg._validate(target, side)
    require(abs(mode) < 1e-6, 'FK反解只接受当前完整FK端点')
    controls = list(spec['fk'][:3])
    unit = bpy.context.scene.unit_settings.scale_length
    def read_q(name):
        pb=target.pose.bones[name]
        return pb.rotation_quaternion.copy() if pb.rotation_mode=='QUATERNION' else pb.rotation_euler.to_quaternion()
    def write_q(name,q):
        pb=target.pose.bones[name]
        if pb.rotation_mode=='QUATERNION':pb.rotation_quaternion=q
        else:pb.rotation_euler=q.to_euler(pb.rotation_mode,pb.rotation_euler)
    from mathutils import Quaternion
    history=[];orientation=None;epsilon=.001
    for iteration in range(iterations):
        current=read_point();residual=desired-current
        history.append({'iteration':iteration,'residual_m':residual.length*unit})
        if residual.length*unit <= tolerance_m:
            return {'status':'reachable','mode':'FK','history':history,'orientation':orientation,
                    'residual_m':residual.length*unit,'controls':[*controls,spec['fk'][3]]}
        values={n:read_q(n) for n in controls};columns=[]
        for name in controls:
            for axis in (Vector((1,0,0)),Vector((0,1,0)),Vector((0,0,1))):
                write_q(name,values[name]@Quaternion(axis,epsilon));pose._update(target)
                columns.append(np.array((read_point()-current)/epsilon))
                write_q(name,values[name]);pose._update(target)
        jac=np.stack(columns,axis=1)
        step=jac.T@np.linalg.solve(jac@jac.T+np.eye(3)*.0001,np.array(residual))
        length=float(np.linalg.norm(step))
        require(math.isfinite(length), 'FK逆解产生非有限增量')
        if length>max_step_radians:step*=max_step_radians/length
        def proposal(fraction):
            for i,name in enumerate(controls):
                vector=Vector(step[i*3:i*3+3]);q=values[name]
                write_q(name,q@Quaternion(vector.normalized(),vector.length*fraction) if vector.length>1e-10 else q)
            pose._update(target)
            return observed.inverse_rotation(target,spec['fk'][3],spec['mixed'][3],desired_foot_matrix,pose._update)[1]
        orientation=proposal(1)
        if (desired-read_point()).length >= residual.length:
            orientation=proposal(.5)
            if (desired-read_point()).length >= residual.length:
                break
    residual=(desired-read_point()).length*unit
    return {'status':'reachable' if residual<=tolerance_m else 'unreachable','mode':'FK',
            'history':history,'orientation':orientation,'residual_m':residual,
            'controls':[*controls,spec['fk'][3]]}


@dataclass(frozen=True)
class _Approval:
    token: str


class Plan:
    def _fingerprint(self):
        require(self.scene == bpy.context.scene and self.target.name in self.scene.objects,
                '候选Scene/目标身份已改变，重新准备')
        require(sum(o.get(rt.TARGET_UID) == self.target.get(rt.TARGET_UID) for o in self.scene.objects) == 1,
                '目标身份被副本共享，重新准备')
        return digest({'scene': self.scene.name, 'meters_per_unit': self.scene.unit_settings.scale_length,
            'target_identity': self.target.get(rt.TARGET_UID),
            'base_action': action_fingerprint(self.base), 'candidate_action': action_fingerprint(self.action),
            'shared_preview_metadata':[self.base.get(rt.META),self.action.get(rt.META)],
            'graph': self.graph_fingerprint(self.target), **_measurement_fingerprint(self),
            'calibration': self.calibration, 'detection_report': self.detection_report,
            'base_binding_fields': self.base_binding_fields,
            'base_mode_inputs': self.base_mode_inputs,
            'issue_id': self.issue_id, 'frames': self.frames, 'parameters': self.parameters,
            'fps': [self.scene.render.fps, self.scene.render.fps_base],
            'object_inputs': _object_input_stamp(self.target,self.base,self.base_slot)})

    def validate(self):
        require(self.status not in {'withdrawn', 'cancelled'}, '该RAM候选已结束')
        require(self._fingerprint() == self.review_fingerprint, '候选、源曲线、校准或控制图已变化，旧确认失效')
        require(json.loads(self.action[META]) == self.report, '专项问题/审阅记录已被外部修改，旧确认失效')
        self._validate_binding()
        _validate_frame_inputs(self)
        return True

    def _validate_binding(self):
        expected = self.base_binding_fields if self.status == 'needs_review' else (
            self.preview_binding_fields if self.status == 'preview' else self.applied_binding_fields)
        binding = rt.binding(self.target)
        action = self.base if self.status == 'needs_review' else self.action
        slot = self.base_slot if self.status == 'needs_review' else self.slot
        require(binding['action'] == action and binding['slot'] == slot and binding['fields'] == expected,
                '绑定/混合/影响或NLA设置已被外部改变，不能覆盖当前工作')

    def preview(self):
        self.validate()
        require(self.status == 'needs_review', '先停止或撤回当前预览/应用')
        require(self.target.animation_data.action == self.base and self.target.animation_data.action_slot == self.base_slot,
                '目标已被外部改绑，保留当前绑定并重新准备')
        self.preview_state = rt.snapshot(self.target)
        rt.begin_preview(self.target, self.action, slot=self.slot)
        self.status = 'preview'
        self.preview_binding_fields = dict(rt.binding(self.target)['fields'])
        return {'ok': True, 'status': 'preview', 'issue_ids': [self.issue_id], 'applied': False,
                'review_fingerprint': self.review_fingerprint}

    def stop_preview(self):
        require(self.status in {'preview', 'needs_review'}, '没有本候选预览')
        if self.status == 'needs_review':
            binding = rt.binding(self.target)
            require(binding['action'] == self.base and binding['slot'] == self.base_slot
                    and binding['fields'] == self.base_binding_fields,
                    '候选已不是预览，但当前绑定不是其原基础；保留当前工作')
            return {'ok': True, 'candidate_retained': True,
                    'status': self.status, 'already_stopped': True}
        self._validate_binding()
        rt.stop_preview(self.target, notify=False)
        self.status = 'needs_review'
        return {'ok': True, 'candidate_retained': True, 'status': self.status}

    def apply(self, approval, *, approved_issue_ids, review_fingerprint):
        self.validate()
        require(self.status == 'preview', '正式应用前必须独立预览')
        require(isinstance(approved_issue_ids,(list,tuple)), '必须给出精确已确认问题ID列表')
        require(type(approval) is _Approval and _approvals.get(approval.token) ==
                (self.token, self.review_fingerprint, tuple(approved_issue_ids)), '缺少有效RAM逐问题确认，不能自动应用')
        require(list(approved_issue_ids) == [self.issue_id] and review_fingerprint == self.review_fingerprint,
                '只能应用用户确认的精确问题ID与当前指纹')
        require(self.target.animation_data.action == self.action and self.target.animation_data.action_slot == self.slot,
                '预览已被外部改绑，保留当前工作')
        rt.stop_preview(self.target, notify=False)
        self.applied_state = rt.snapshot(self.target)
        self.old_fake_user = self.base.use_fake_user
        self.base.use_fake_user = True
        _bind(self.target, self.action, self.slot)
        dynamic.suspend(self.target, self.applied_state['dynamic'])
        _set_frame(self.applied_state['frame'] + self.applied_state['subframe'])
        self.status = 'applied'
        self.applied_binding_fields = dict(rt.binding(self.target)['fields'])
        _approvals.pop(approval.token, None)
        self.report['review_state'] = 'explicit_issue_applied'
        self.report['applied'] = True
        self.action[META] = json.dumps(self.report, ensure_ascii=False)
        return {'ok': True, 'status': self.status, 'issue_ids': [self.issue_id], 'original_action_retained': True,
                'dependent_dynamics': dynamic.report(self.applied_state['dynamic'])}

    def withdraw(self):
        self.validate()
        require(self.status == 'applied', '没有本RAM会话应用可撤回')
        require(self.target.animation_data.action == self.action and self.target.animation_data.action_slot == self.slot,
                '应用后已被外部改绑，不能覆盖当前工作')
        dynamic.restore(self.target, self.applied_state['dynamic'], guard=True)
        rt.restore(self.target, self.applied_state)
        self.base.use_fake_user = self.old_fake_user
        self.status = 'withdrawn'
        self.report['review_state'] = 'withdrawn_candidate_retained'
        self.report['applied'] = False
        self.action[META] = json.dumps(self.report, ensure_ascii=False)
        return {'ok': True, 'status': self.status, 'candidate_retained': True}


def _user_approval(plan, *, exact_issue_ids, review_fingerprint):
    """Private UI callback only after the user accepts the visible exact issue.

    This is a host trust boundary, not an OS security sandbox. An agent must
    never call this callback to manufacture consent. Tests may simulate it
    solely on disposable fixtures; simulation is not user approval.
    """
    plan.validate()
    require(plan.status == 'preview' and list(exact_issue_ids) == [plan.issue_id]
            and review_fingerprint == plan.review_fingerprint, '逐问题确认内容已过期')
    approval = _Approval(uuid.uuid4().hex)
    _approvals[approval.token] = (plan.token, plan.review_fingerprint, tuple(exact_issue_ids))
    return approval


def _sync_shared_preview_stop(target, session):
    """A legitimate shared STOP restored base; retire only the owning Plan.

    This never resets a Plan whose current binding is not its own base
    action/slot/fields.  Such an external rebind remains a validation error.
    """
    try:
        binding = rt.binding(target)
    except Exception:
        return
    for plan in list(_plans.values()):
        if plan.status != 'preview' or plan.target != target:
            continue
        if session.get('action') != plan.action or session.get('slot') != plan.slot:
            continue
        if (binding['action'] == plan.base and binding['slot'] == plan.base_slot
                and binding['fields'] == plan.base_binding_fields):
            plan.status = 'needs_review'
            plan.preview_state = None
            plan.preview_binding_fields = None
            for token, value in list(_approvals.items()):
                if value[0] == plan.token:
                    _approvals.pop(token, None)


def validate_application(target, action, approved_issue_ids):
    """Publication guard: exact IDs alone never manufacture review authority.

    Requires an already explicitly applied, current RAM Plan. A saved marker
    or list of IDs is insufficient. Raises CorrectionError on any mismatch.
    """
    require(META in action, '不是本专项修正候选')
    plans = [p for p in _plans.values() if p.target == target and p.action == action and p.status == 'applied']
    require(len(plans) == 1, '缺少当前RAM逐问题应用确认；先在专项入口重新预览并明确确认')
    plan = plans[0]
    plan.validate()
    require(isinstance(approved_issue_ids,(list,tuple)), '发布需精确已确认问题ID列表')
    require(list(approved_issue_ids) == [plan.issue_id], '发布的问题ID与专项确认不一致')
    require(target.animation_data and target.animation_data.action == action
            and target.animation_data.action_slot == plan.slot, '已应用候选被外部改绑')
    return {'ok': True, 'issue_ids': [plan.issue_id], 'review_fingerprint': plan.review_fingerprint,
            'authority': 'current_ram_explicit_issue_application'}


def reopen_for_review(target, action, *, graph_fingerprint):
    """Fresh review Plan from a retained candidate; restores no approval token."""
    require(callable(graph_fingerprint) and META in action, '缺少明确候选与宿主图核')
    row = json.loads(action[META])
    base = bpy.data.actions.get(row['base_action'])
    require(base and target.animation_data and target.animation_data.action == base,
            '重新审阅前请明确绑定该候选原基础Action；不自动替换当前工作')
    require(action_fingerprint(base) == row['base_action_fingerprint'], '原基础曲线已变化，重新检测')
    plan = Plan()
    plan.token, plan.scene, plan.target = uuid.uuid4().hex, bpy.context.scene, target
    plan.base, plan.base_slot = base, target.animation_data.action_slot
    plan.base_binding_fields = row['base_binding_fields']
    plan.base_mode_inputs = row['base_mode_inputs']
    plan.action = action
    slots = [s for s in action.slots if s.identifier == row['slot_identifier']]
    require(len(slots) == 1 and plan.base_slot.identifier == row['slot_identifier'], 'Slot身份已变化')
    plan.slot = slots[0]
    plan.calibration, plan.detection_report = row['calibration'], row['detection_report']
    if anchors.mode(plan.calibration)==anchors.MODE:
        require(row.get('measurement_mode')==anchors.MODE and row.get('coverage_scope')==anchors.SCOPE,
                '骨锚点候选模式/覆盖语义不符')
        object_inputs=plan.calibration.get('detection_object_inputs')
        require(isinstance(object_inputs,dict) and
                plan.detection_report.get('target',{}).get('reference',{}).get('object_inputs')==object_inputs and
                _object_input_stamp(target,base,plan.base_slot)==object_inputs,
                '骨锚点候选缺检测时对象身份或对象输入已变化，请重新检测')
        plan.mesh=_prepare_measurement(plan.scene,target,plan.calibration)
    else:
        require(row.get('measurement_mode','surface')=='surface','不能将骨锚点候选重开为表面模式')
        plan.mesh = _validate_surface_detection_identity(
            target, base, plan.base_slot, plan.calibration, plan.detection_report)
        _prepare_measurement(plan.scene, target, plan.calibration)
    plan.graph_fingerprint = graph_fingerprint
    plan.issue_id, plan.kind, plan.region = row['issue_ids'][0], row['kind'], row['region']
    plan.frames, plan.parameters = row['frames'], row['parameters']
    plan.status, plan.report, plan.review_fingerprint = 'needs_review', row, row['review_fingerprint']
    plan.validate()
    _plans[plan.token] = plan
    return plan


def _checked_support_window(window, report, issue, measurement_mode, frames, mapping):
    """Admit the declared touchdown only when the base report supports locking."""
    require(measurement_mode == anchors.MODE and issue['kind'] == 'target_penetration'
            and issue['region'] == 'heel', '联合试样需要显式脚跟骨锚点及穿透问题')
    require(isinstance(window, dict) and set(window) <= {'touchdown_frame', 'tangent_release'},
            '联合试样参数无效')
    touchdown = window.get('touchdown_frame')
    require(type(touchdown) is int and frames[0] <= touchdown < frames[-1]
            and frames[0] - 1 in mapping, '需显式进入帧、着地帧及后续稳定支撑')
    require(report['source']['root_motion']['mode'] == report['target']['root_motion']['mode'],
            '源与目标根运动模式不同，不能自动锁定切向')
    require(any(row['side'] == issue['side'] and row['region'] == issue['region']
                and row['source_kind'] == 'planted' and row['classification'] == 'target_penetration'
                and _has_tangent_drift(row, report['settings'])
                and row['target_interval_s'][0] <= mapping[touchdown + 1] + 1e-8
                and row['target_interval_s'][1] >= mapping[frames[-1]] - 1e-8
                for row in report['comparison']['items']),
            '检测报告未证实着地后稳定支撑及超过门槛的切向路径')
    release = window.get('tangent_release')
    if release is not None:
        require(isinstance(release, dict) and set(release) == {'start', 'end', 'blend_frames'}
                and all(type(v) is int for v in release.values()), '切向旁路需明确整数范围及过渡帧数')
        start, end, ramp = (release[key] for key in ('start', 'end', 'blend_frames'))
        require(ramp >= 1 and touchdown <= start - ramp < start <= end < end + ramp <= frames[-1],
                '切向旁路及过渡须在已确认的支撑范围内')
    return {'touchdown_frame': touchdown, 'tangent_release': dict(release) if release is not None else None}


def _has_tangent_drift(row, settings):
    path = row['target_path_length_star']
    start, end = row['target_interval_s']
    return path > settings['slip_eps'] or (end > start and path / (end - start) > settings['slip_rate'])


def gait_support_window(report, issue, calibration, frames, *, tangent_release=None):
    """Map a measured source support onset to an exact target contact frame."""
    require(anchors.mode(calibration) == anchors.MODE and issue['kind'] == 'target_penetration'
            and issue['region'] == 'heel' and issue['defect'],
            '自动联合修正需已检测的脚跟穿透及显式骨锚校准')
    mapping = dict(calibration['frame_time_pairs'])
    align = Aligner(report['alignment'])
    heel_support = [segment for segment in report['source']['segments']['support']
                    if segment['side'] == issue['side'] and segment['region'] == 'heel'
                    and segment['stable']]
    starts = []
    for segment in heel_support:
        start = align.source_to_target(segment['time_start_s'])
        end = align.source_to_target(segment['time_end_s'])
        if start is not None and end is not None and end >= mapping[frames[-1]] - 1e-8:
            starts.extend(frame for frame in frames[:-1] if abs(mapping[frame] - start) <= 1e-8)
    require(len(starts) == 1, '找不到覆盖选段且与目标采样精确对应的着地帧；请逐问题审阅')
    return _checked_support_window({'touchdown_frame': starts[0], 'tangent_release': tangent_release},
                                   report, issue, anchors.MODE, frames, mapping)


def _tangent_weight(frame, release):
    if release is None:
        return 1.0
    distance = max(release['start'] - frame, frame - release['end'], 0)
    factor = min(1.0, distance / release['blend_frames'])
    return factor * factor * (3 - 2 * factor)


def prepare_issue(target, detection_report, *, issue_id, frames, calibration,
                  graph_fingerprint, clearance_m=0.0, max_delta_m=0.05,
                  match_tolerance_m=0.001, solve_tolerance_m=0.0001,
                  cancelled=None, support_window=None):
    """Build one reviewable Action, preserving the currently bound base Action.

    A required host graph_fingerprint callback must cover the reviewed native
    control graph/dependencies, with driven outputs separated from inputs.
    Never infer a missing sole. Explicit complete calibration is mandatory.
    """
    require(callable(graph_fingerprint), '需要宿主已验证的控制图指纹入口')
    require(bpy.app.background, '当前修正候选生成仅允许可丢弃后台工作副本；GUI接入需宿主后台作业')
    require(not rt.has_preview(target), '请先停止现有重定向预览')
    pose._metadata(target)
    require(target.library is None and target.data.library is None, '链接骨架需先明确可编辑工作副本')
    require(target.get(rt.TARGET_UID), '目标缺少既有重定向身份，请通过正式重定向入口准备')
    scene = bpy.context.scene
    binding = rt.binding(target)
    base, base_slot = binding['action'], binding['slot']
    require(base is not None and base_slot is not None, '需要明确的基础Action和Slot')
    mode_paths={target.pose.bones['骨架设置'].path_from_id()+'['+json.dumps(key,ensure_ascii=False)+']' for key in rt.c94.MODES}
    require(not any(fc.data_path in {*samples._OBJECT_CHANNELS,*mode_paths} for fc in target.animation_data.drivers),
            'Object/模式输入由driver占用，当前修正需明确适配')
    require(len(base.layers) == 1 and len(base.layers[0].strips) == 1,
            '当前修正只接受单层单Strip基础Action；不扁平化复杂动画')
    require(not binding['fields']['use_nla'] or not any(not track.mute for track in target.animation_data.nla_tracks),
            '当前入口不合成NLA，请显式使用独立基础Action')
    require(META not in base, '当前入口不叠加未验修正候选，请显式重新检测所选基础动作')
    values = [clearance_m, max_delta_m, match_tolerance_m, solve_tolerance_m]
    require(all(type(x) in (int, float) and math.isfinite(x) for x in values)
            and clearance_m >= 0 and min(values[1:]) > 0, '修正参数必须为有限合法米制值')
    calibration = json.loads(json.dumps(calibration, allow_nan=False))
    measurement_mode=anchors.mode(calibration)
    if measurement_mode=='surface':
        require(calibration.get('coverage_status') == 'explicit_calibrated_complete',
                'coverage_incomplete：必须提供完整且显式校准的可见/夹具脚底，不能自动推断')
    require(calibration.get('target_uid') == target.get(rt.TARGET_UID), '脚底校准目标不同')
    require(calibration.get('side') in {'L', 'R'}, '脚侧无效')
    require(calibration.get('plane', {}).get('status') == 'explicit', '地面必须显式校准')
    samples._plane_doc(calibration['plane'])
    rows = [row for row in detection_report.get('issues', []) if row['id'] == issue_id]
    require(len(rows) == 1 and rows[0]['kind'] in KINDS and rows[0]['defect'],
            '只能选择报告中明确的脚滑/穿地/悬空问题；复核观察不自动修正')
    issue = rows[0]
    require(detection_report.get('measurement_mode','surface')==measurement_mode and
        issue.get('measurement_mode','surface')==measurement_mode,'检测/问题/修正测量模式不同')
    if measurement_mode==anchors.MODE:
        expected=calibration.get('detection_calibration_sha256')
        require(isinstance(expected,str) and len(expected)==64 and
            detection_report.get('calibration_sha256')==expected and issue.get('calibration_sha256')==expected and
            detection_report.get('coverage_scope')==anchors.SCOPE and issue.get('coverage_scope')==anchors.SCOPE,
            '骨锚点检测/问题校准摘要不一致')
        require(all(isinstance(pair,(list,tuple)) and len(pair)==2 and type(pair[0]) is int
                    for pair in calibration.get('frame_time_pairs',[])),
                '骨锚点修正帧秒表仅接明确整数窗口；小数尾不折叠为整数')
    require(issue['side'] == calibration['side'] and issue['region'] in samples.REGIONS, '问题侧/片区与校准不符')
    reference = detection_report['target']['reference']
    if measurement_mode==anchors.MODE:
        require(reference.get('measurement_mode')==anchors.MODE and
            reference.get('anchor_identity')==calibration.get('anchor_identity'),'报告的骨锚点身份不同')
        object_inputs=calibration.get('detection_object_inputs')
        require(isinstance(object_inputs,dict) and reference.get('object_inputs')==object_inputs and
                _object_input_stamp(target,base,base_slot)==object_inputs,
                '对象输入/父空间与原骨锚点检测不同或旧报告缺身份，请重新检测')
    require(reference.get('target_uid') == target.get(rt.TARGET_UID)
            and reference.get('action_fingerprint') == action_fingerprint(base),
            '检测报告不属于当前基础Action/目标，请重新采样')
    if measurement_mode=='surface':
        _validate_surface_detection_identity(target, base, base_slot, calibration,
                                             detection_report)
    require(frames and len(frames) >= 2 and all(type(f) is int for f in frames)
            and list(frames) == list(range(min(frames), max(frames) + 1)), '修正帧域必须是至少两帧的明确连续整数帧')
    mapping = {int(f): float(t) for f,t in calibration['frame_time_pairs']}
    target_interval = issue.get('evidence', {}).get('target_interval_s', [issue['time_start_s'], issue['time_end_s']])
    require(all(f in mapping and target_interval[0]-1e-8 <= mapping[f] <= target_interval[1]+1e-8 for f in frames),
            '选定帧域超出该问题明确的目标时间区间')
    if support_window is not None:
        support_window = _checked_support_window(support_window, detection_report, issue,
                                                 measurement_mode, frames, mapping)
        require(base.frame_range[0] <= frames[0] - 1 and frames[-1] <= base.frame_range[1],
                '联合试样不得扩展基础动作时间')
    mesh = _prepare_measurement(scene,target,calibration)
    plan = Plan()
    plan.token = uuid.uuid4().hex
    plan.scene, plan.target, plan.mesh = scene, target, mesh
    plan.base, plan.base_slot = base, base_slot
    plan.base_binding_fields = dict(binding['fields'])
    plan.calibration, plan.detection_report = calibration, json.loads(json.dumps(detection_report))
    plan.graph_fingerprint = graph_fingerprint
    plan.issue_id, plan.kind, plan.region, plan.frames = issue_id, issue['kind'], issue['region'], list(frames)
    plan.parameters = dict(clearance_m=clearance_m, max_delta_m=max_delta_m,
        match_tolerance_m=match_tolerance_m, solve_tolerance_m=solve_tolerance_m)
    if support_window is not None:
        plan.parameters['support_window'] = support_window
    initial = rt.snapshot(target)
    plan.base_mode_inputs = dict(initial['modes'])
    plan.status = 'needs_review'
    # rt.snapshot owns pose/modes; it intentionally does not promise arbitrary
    # raw Object overrides on animated channels. Reject those before frame_set
    # instead of losing them and then claiming to have restored them.
    _validate_frame_inputs(plan)
    original_hash = action_fingerprint(base)
    graph_before = graph_fingerprint(target)
    candidate = None
    unit = scene.unit_settings.scale_length
    require(math.isfinite(unit) and unit > 0, '场景米制单位无效')
    boundary_keys = [frames[0]-1, frames[-1]+1]
    if support_window is not None and boundary_keys[-1] > base.frame_range[1]:
        boundary_keys.pop()
    frames_to_write = [boundary_keys[0], *frames, *boundary_keys[1:]]
    approach_clearance = None
    writes, measurements, anchor = {}, [], None
    spec = leg._spec(calibration['side'])
    mutable = [*spec['fk'], spec['main'], spec['pole'], spec['foot'], spec['toe_ik'], spec['heel_ik'], spec['toe']]
    try:
        for frame in frames_to_write:
            if cancelled and cancelled():
                raise CorrectionError('cancelled：取消候选，基础数据保持')
            rt.restore(target, initial)
            dynamic.suspend(target, initial['dynamic'])
            _set_frame(frame)
            before_channels = {n: pose._channels(target.pose.bones[n]) for n in mutable}
            old_mode = target.pose.bones['骨架设置'][spec['key']]
            baseline, minimum_before = _read_patch(plan)
            if (support_window is not None and frame == boundary_keys[0]
                    and frames[0] < support_window['touchdown_frame']):
                approach_clearance = minimum_before
                require(approach_clearance >= clearance_m / unit, '着地前的边界帧需要高于研究支撑面')
            anchor_before=anchors.read_points(scene,target,calibration['anchors']) if measurement_mode==anchors.MODE else None
            matched = None
            if frame in frames:
                _,_,base_mode = leg._validate(target,calibration['side'])
                desired_foot_matrix = pose._update(target).pose.bones[spec['mixed'][3]].matrix.copy()
                if abs(base_mode-1) < 1e-6:
                    matched = leg.switch(target, side=calibration['side'], target='IK', max_error_meters=match_tolerance_m)
                else:
                    matched = {'mode':'FK','status':'native_fk_rotation_inverse_no_switch'}
                normal = Vector(calibration['plane']['normal'])
                if support_window is not None:
                    touchdown = support_window['touchdown_frame']
                    target_clearance = clearance_m / unit
                    if frame < touchdown:
                        remaining = (mapping[touchdown] - mapping[frame]) / (
                            mapping[touchdown] - mapping[boundary_keys[0]])
                        target_clearance += (approach_clearance - target_clearance) * remaining ** 2
                    delta = normal * (target_clearance - minimum_before)
                    if frame >= touchdown:
                        if anchor is None:
                            anchor = baseline.copy()
                        tangent = anchor - baseline
                        tangent -= normal * tangent.dot(normal)
                        delta += tangent * _tangent_weight(frame, support_window['tangent_release'])
                elif plan.kind == 'target_slip':
                    if anchor is None:
                        anchor = baseline.copy()
                    delta = anchor - baseline
                    delta -= normal * delta.dot(normal)
                else:
                    delta = normal * (clearance_m/unit - minimum_before)
                require(delta.length * unit <= max_delta_m + 1e-10, '所需足底位移超出显式修正上限，拒绝扩大改动')
                desired = baseline + delta
                if abs(base_mode) < 1e-6:
                    matched = solve_fk_point(target,side=calibration['side'],read_point=lambda:_read_patch(plan)[0],
                        desired=desired,desired_foot_matrix=desired_foot_matrix,tolerance_m=solve_tolerance_m)
                    require(matched['status']=='reachable', 'unreachable：当前FK原生旋转域不能满足该显式脚点，未改模式/root/位置')
                solve_history = []
                for _ in range(4 if abs(base_mode-1)<1e-6 else 0):
                    current, current_min = _read_patch(plan)
                    residual = desired - current
                    solve_history.append({'current': list(current), 'residual_m': residual.length*unit})
                    if residual.length * unit <= solve_tolerance_m:
                        break
                    main = target.pose.bones[spec['main']]
                    matrix = main.matrix.copy()
                    fixed_channels = pose._channels(main)
                    # Observe the actual native skin response. The native
                    # constraint/deform graph need not translate 1:1 with its
                    # main target, so a fixed residual add can oscillate.
                    epsilon = .0001 / unit
                    columns = []
                    world_to_rig = target.matrix_world.inverted().to_3x3()
                    for axis in (Vector((1,0,0)),Vector((0,1,0)),Vector((0,0,1))):
                        probe = matrix.copy()
                        probe.translation += world_to_rig @ (axis * epsilon)
                        _move_main_without_rotation_scale(main,probe,fixed_channels)
                        pose._update(target)
                        point, _ = _read_patch(plan)
                        columns.append((point-current)/epsilon)
                    _move_main_without_rotation_scale(main,matrix,fixed_channels)
                    pose._update(target)
                    jacobian = Matrix(columns).transposed()
                    require(abs(jacobian.determinant()) > 1e-6, '原生脚底响应奇异，拒绝自动修正')
                    step = jacobian.inverted() @ residual
                    require(step.length * unit <= max_delta_m, '原生控制逆解步长超出显式上限')
                    matrix.translation += world_to_rig @ step
                    _move_main_without_rotation_scale(main,matrix,fixed_channels)
                    pose._update(target)
                after, minimum_after = _read_patch(plan)
                require((after-desired).length * unit <= solve_tolerance_m,
                        '原生IK限制下修正残差超限，候选回滚：'+json.dumps({'frame':frame,'history':solve_history,
                            'after':list(after),'desired':list(desired),'residual_m':(after-desired).length*unit}))
                measurements.append({'frame': frame, 'before_center': list(baseline), 'after_center': list(after),
                    'before_min_world': minimum_before, 'after_min_world': minimum_after,
                    'requested_delta_m': delta.length*unit, 'residual_m': (after-desired).length*unit,
                    'native_matching': matched})
                if measurement_mode==anchors.MODE:
                    anchor_after=anchors.read_points(scene,target,calibration['anchors'])
                    for check_side in samples.SIDES:
                        for region in samples.REGIONS:
                            before_point=anchor_before[check_side][region];after_point=anchor_after[check_side][region]
                            if check_side!=calibration['side']:
                                require((after_point-before_point).length*unit<=solve_tolerance_m,
                                        '骨锚点修正改变另脚，候选拒绝')
                            elif region!=plan.region:
                                old_clear=normal.dot(before_point)-calibration['plane']['d']
                                new_clear=normal.dot(after_point)-calibration['plane']['d']
                                require(new_clear>=min(0.,old_clear)-solve_tolerance_m/unit,
                                        '修正一个骨锚点使同脚另一锚点新增/加重越面，候选拒绝')
                    measurements[-1].update(anchor_before={s:{r:list(p) for r,p in ps.items()} for s,ps in anchor_before.items()},
                        anchor_after={s:{r:list(p) for r,p in ps.items()} for s,ps in anchor_after.items()})
            writes[frame] = {'channels': {n: pose._channels(target.pose.bones[n]) for n in mutable},
                'mode': target.pose.bones['骨架设置'][spec['key']], 'before': before_channels, 'before_mode': old_mode}
        changed = {(name,field) for frame in frames for name in mutable for field in pose._CHANNELS
            if writes[frame]['channels'][name][field] != writes[frame]['before'][name][field]}
        mode_changed = any(writes[f]['mode'] != writes[f]['before_mode'] for f in frames)
        require(changed or mode_changed, '该明确问题无需额外曲线变更')
        planned_paths = {target.pose.bones[n].path_from_id(field) for n,field in changed}
        if mode_changed:
            planned_paths.add(target.pose.bones['骨架设置'].path_from_id()+'['+json.dumps(spec['key'],ensure_ascii=False)+']')
        require(all(not row['modifiers'] and not row['sampled'] for row in _curve_index(base).values()
                    if row['path'] in planned_paths), '拟改写曲线含modifier/采样点，当前入口需显式适配，基础数据保持')
        candidate = base.copy()
        candidate.name = 'C94_足底修正_'+plan.token[:8]
        candidate.use_fake_user = True
        slots = [s for s in candidate.slots if s.identifier == base_slot.identifier]
        require(len(slots) == 1, '复制Action的Slot身份不唯一')
        plan.action, plan.slot = candidate, slots[0]
        _bind(target, candidate, plan.slot)
        for frame in frames_to_write:
            for name,field in sorted(changed):
                pb = target.pose.bones[name]
                setattr(pb, field, writes[frame]['channels'][name][field])
                pb.keyframe_insert(data_path=field, frame=frame, group='足底修正_'+plan.token[:8])
            if mode_changed:
                settings = target.pose.bones['骨架设置']
                settings[spec['key']] = writes[frame]['mode']
                settings.keyframe_insert(data_path='['+json.dumps(spec['key'],ensure_ascii=False)+']', frame=frame)
        affected = {target.pose.bones[n].path_from_id(field) for n,field in changed}
        if mode_changed:
            affected.add(target.pose.bones['骨架设置'].path_from_id()+'['+json.dumps(spec['key'],ensure_ascii=False)+']')
        for layer in candidate.layers:
            for strip in layer.strips:
                bag = strip.channelbag(plan.slot)
                if bag is None: continue
                for fc in bag.fcurves:
                    if fc.data_path in affected:
                        for key in fc.keyframe_points:
                            if key.co.x in frames_to_write:
                                key.interpolation = 'CONSTANT' if mode_changed and fc.data_path.endswith(']') else 'LINEAR'
                        fc.update()
        difference = _action_diff(base, candidate)
        require(all(row['path'] in affected for row in difference), '出现未声明控制通道改动，候选拒绝')
        # Pose-solving success alone does not prove the copied Action/Slot
        # actually replays that pose. Read the authored candidate back before
        # exposing it for review, with the original base still protected.
        for row in measurements:
            _set_frame(row['frame'])
            pose._update(target)
            actual,minimum = _read_patch(plan)
            replay_error = (actual-Vector(row['after_center'])).length*unit
            require(replay_error <= solve_tolerance_m, '独立Action写入后的实际求值与拟议姿态不符，候选拒绝')
            row.update(candidate_readback_center=list(actual),candidate_readback_min_world=minimum,
                       candidate_readback_delta_m=replay_error)
            if measurement_mode==anchors.MODE:
                actual_anchors=anchors.read_points(scene,target,calibration['anchors'])
                require(all((p-Vector(row['anchor_after'][s][r])).length*unit<=solve_tolerance_m
                    for s,ps in actual_anchors.items() for r,p in ps.items()),'独立Action骨锚点全量回读不符')
        plan.status = 'needs_review'
        plan.report = {'schema':'c94.contact.correction.v1','version':VERSION,'issue_ids':[issue_id],
            'kind':plan.kind,'side':calibration['side'],'region':plan.region,'frames':list(frames),
            'boundary_keys':boundary_keys,'parameters':plan.parameters,
            'review_state':'needs_review','applied':False,'measurements':measurements,
            'curve_changes':difference,'writes_weights':False,'writes_rest':False,'writes_constraints':False,
            'base_action':base.name,'base_action_fingerprint':original_hash,'slot_identifier':base_slot.identifier,
            'base_binding_fields':plan.base_binding_fields,
            'base_mode_inputs':plan.base_mode_inputs,
            'calibration':calibration,'detection_report':plan.detection_report,
            'original_action_written':False,'no_automatic_approval':True,
            'limits':['one issue per candidate','explicit complete sole calibration required',
                      'native matching rejection is not relaxed','background work-copy generation only',
                      'only sampled integer-frame effect; no continuous-contact guarantee']}
        if measurement_mode==anchors.MODE:
            plan.report.update(measurement_mode=anchors.MODE,coverage_scope=anchors.SCOPE,
                anchors_calibrated=True,skin_surface_evaluated=False,full_sole_calibrated=False,full_sole_pass=False,
                detection_calibration_sha256=calibration['detection_calibration_sha256'],
                limits=['one issue per candidate','declared bone-local markers only; not skin sole',
                    'explicit heel/ball/toe and plane; no automatic approval',
                    'native solve limits unchanged; sampled integer frames only'])
        require(rt.META in base, '基础动作缺少当前共享预览元数据')
        candidate[rt.META] = base[rt.META]
        candidate[META] = json.dumps(plan.report, ensure_ascii=False)
        require(action_fingerprint(base) == original_hash, '基础曲线意外改变')
        require(graph_fingerprint(target) == graph_before, '原生控制依赖图意外改变')
    except BaseException:
        if candidate is not None:
            rt.restore(target, initial)
            bpy.data.actions.remove(candidate)
        raise
    finally:
        rt.restore(target, initial)
    plan.review_fingerprint = plan._fingerprint()
    plan.report['review_fingerprint'] = plan.review_fingerprint
    candidate[META] = json.dumps(plan.report, ensure_ascii=False)
    _plans[plan.token] = plan
    return plan


rt.register_preview_stop_callback(_sync_shared_preview_stop)
