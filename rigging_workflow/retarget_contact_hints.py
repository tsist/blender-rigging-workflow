# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual hand/body review notes, never collision detection or correction input.

Only a new JSON Text and this panel's options are written. Validation reads
the explicitly selected target/candidate; it never binds, samples or approves.
"""
import json
import math
import textwrap
import uuid
from datetime import datetime, timezone

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, IntProperty, PointerProperty, StringProperty
from . import retarget_action as rt, retarget_calibration as calibration
from . import retarget_contact_correction as signatures

SCHEMA = 'c94.contact.review_hint.v1'
OPTIONS = 'c94_contact_review_hint_options_v1'
KINDS = {'HAND_BODY', 'BODY_BODY'}
_REGISTERED = []
_OWNS_OPTIONS = False


def require(value, message):
    if not value:
        raise ValueError(message)


def _required_text(value, label, limit=2000):
    require(isinstance(value, str) and 0 < len(value.strip()) <= limit,
            label + '须为非空文字，最多%d字' % limit)
    return value.strip()


def _identity(target, candidate, scene, slot=None):
    require(isinstance(target, bpy.types.Object) and target.type == 'ARMATURE'
            and scene.objects.get(target.name) == target, '请选择本Scene的明确目标ARMATURE')
    require(target.mode != 'EDIT', '请退出目标编辑模式后记录或校验rest身份')
    uid = target.get(rt.TARGET_UID)
    require(isinstance(uid, str) and uid, '目标缺少既有重定向UID；本入口不会创建或改写身份')
    require(sum(obj.get(rt.TARGET_UID) == uid for obj in scene.objects) == 1,
            '目标UID被副本共享，不能把人工观察提示复用到不同角色')
    require(isinstance(candidate, bpy.types.Action), '请选择独立基础候选Action')
    require(signatures.META not in candidate, '人工提示仅登记基础候选，不接收足部修正版本')
    raw = candidate.get(rt.META)
    require(isinstance(raw, str), '候选缺少既有重定向报告')
    metadata = json.loads(raw)
    require(isinstance(metadata, dict) and metadata.get('schema') in
            {'c94.retarget.action.v1', 'c94.retarget.mmd.v1'}, '只接受已登记的基础候选schema')
    require(metadata.get('target_uid') == uid, '基础候选与所选目标UID不一致')
    candidate_id = _required_text(metadata.get('id'), '候选ID', 256)
    slots = [value for value in candidate.slots if value.target_id_type == 'OBJECT']
    require(len(slots) == 1, '基础候选须有唯一OBJECT Slot；不猜测多Slot')
    selected = slots[0]
    require(slot is None or slot == selected or slot == selected.identifier, '指定Slot不是候选的唯一OBJECT Slot')
    require(any(strip.channelbag(selected) is not None and len(strip.channelbag(selected).fcurves)
                for layer in candidate.layers for strip in layer.strips if strip.type == 'KEYFRAME'),
            '候选OBJECT Slot没有实际原生曲线')
    bounds = metadata.get('output_range')
    require(isinstance(bounds, list) and len(bounds) == 2 and all(type(v) in (int, float)
            and math.isfinite(v) for v in bounds) and bounds[0] <= bounds[1], '候选缺少有效目标帧域')
    fps = metadata.get('target_fps')
    require(type(fps) in (int, float) and math.isfinite(fps) and fps > 0, '候选缺少可信目标fps')
    actual = [float(v) for v in candidate.frame_range]
    require(all(math.isfinite(v) for v in actual), '实际Action帧域不是有限数值')
    return {'target': {'object': target.name, 'armature': target.data.name, 'target_uid': uid,
                       'rest_fingerprint': calibration.bone_rest_signature(target)},
            'candidate': {'action': candidate.name, 'slot': selected.identifier, 'candidate_id': candidate_id,
                          'action_fingerprint': signatures.action_fingerprint(candidate),
                          'metadata_fingerprint': signatures.digest(metadata), 'output_range': list(bounds),
                          'actual_frame_range': actual, 'fps': float(fps)}}


def _observation(target, kind, bones, start, end, description, basis, identity):
    require(isinstance(kind, str) and kind in KINDS, '提示类型仅为HAND_BODY或BODY_BODY人工复核')
    require(isinstance(bones, (list, tuple)) and len(bones) == 2 and all(isinstance(n, str)
            and n in target.data.bones and n in target.pose.bones for n in bones)
            and bones[0] != bones[1], '请明确两根不同且实际存在的目标骨名')
    require(type(start) is int and type(end) is int and start <= end, '起止帧须为有序整数，不自动取整')
    candidate = identity['candidate']
    lo = max(candidate['output_range'][0], candidate['actual_frame_range'][0])
    hi = min(candidate['output_range'][1], candidate['actual_frame_range'][1])
    require(lo <= start <= end <= hi, '人工观察帧域须位于候选报告及实际Action帧域内')
    return {'kind': kind, 'bones': list(bones), 'time': {'start': start, 'end': end,
            'units': 'candidate_target_frames'}, 'origin': {'kind': 'manual_observation',
            'description': _required_text(description, '人工观察描述'),
            'basis': _required_text(basis, '人工观察依据/来源')}}


def create_hint(target, candidate, *, kind, bone_a, bone_b, start, end, description, basis, slot=None, scene=None):
    """Create one new Text; no existing note, Action, rig or Scene time is written."""
    scene = scene or bpy.context.scene
    identity = _identity(target, candidate, scene, slot)
    observation = _observation(target, kind, [bone_a, bone_b], start, end, description, basis, identity)
    document = {'schema': SCHEMA, 'id': uuid.uuid4().hex,
                'created_utc': datetime.now(timezone.utc).isoformat(), **identity, **observation,
                'automatic_detection': False, 'approval': None, 'correction_allowed': False,
                'foot_transfer_allowed': False}
    raw = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
    text = bpy.data.texts.new('C94_人工复核提示_' + kind + '_' + document['id'][:8])
    text.write(raw)
    text.use_fake_user = True
    return text


def _read_hint(text):
    require(isinstance(text, bpy.types.Text), '请选择已有人工提示JSON Text')
    raw = text.as_string()
    require(len(raw.encode('utf-8')) <= 65536, '人工提示Text超过64KiB')
    document = json.loads(raw)
    fields = {'schema', 'id', 'created_utc', 'target', 'candidate', 'kind', 'bones', 'time',
              'origin', 'automatic_detection', 'approval', 'correction_allowed', 'foot_transfer_allowed'}
    require(isinstance(document, dict) and set(document) == fields and document['schema'] == SCHEMA,
            '该Text不是本入口人工复核提示schema；保留原Text')
    require(document['automatic_detection'] is False and document['approval'] is None
            and document['correction_allowed'] is False and document['foot_transfer_allowed'] is False,
            '人工提示不能携带自动检测、批准或修正/足部转交权')
    for key in ('id', 'created_utc'):
        _required_text(document[key], key, 256)
    require(isinstance(document['target'], dict) and set(document['target']) ==
            {'object', 'armature', 'target_uid', 'rest_fingerprint'}, '提示目标身份字段不完整')
    require(isinstance(document['candidate'], dict) and set(document['candidate']) ==
            {'action', 'slot', 'candidate_id', 'action_fingerprint', 'metadata_fingerprint',
             'output_range', 'actual_frame_range', 'fps'}, '提示候选身份字段不完整')
    require(isinstance(document['time'], dict) and set(document['time']) == {'start', 'end', 'units'}
            and document['time']['units'] == 'candidate_target_frames', '提示必须使用候选目标帧域')
    require(isinstance(document['origin'], dict) and set(document['origin']) ==
            {'kind', 'description', 'basis'} and document['origin']['kind'] == 'manual_observation',
            '提示缺少明确人工来源，不能解释成自动碰撞检测')
    return document


def validate_hint(text, *, target, candidate, scene=None):
    """Return fresh/stale for manual review only. A stale Text is never edited."""
    document = None
    try:
        document = _read_hint(text)
        current = _identity(target, candidate, scene or bpy.context.scene)
        _observation(target, document['kind'], document['bones'], document['time']['start'],
                     document['time']['end'], document['origin']['description'], document['origin']['basis'], current)
        reasons = []
        if any(document['target'][k] != current['target'][k] for k in ('object', 'armature', 'target_uid')):
            reasons.append('目标身份已变化')
        if document['target']['rest_fingerprint'] != current['target']['rest_fingerprint']:
            reasons.append('骨架rest已变化')
        if any(document['candidate'][k] != current['candidate'][k] for k in ('action', 'slot', 'candidate_id')):
            reasons.append('候选Action/Slot身份已变化')
        if document['candidate']['action_fingerprint'] != current['candidate']['action_fingerprint']:
            reasons.append('候选实际曲线/设置已变化')
        if any(document['candidate'][k] != current['candidate'][k]
               for k in ('metadata_fingerprint', 'output_range', 'actual_frame_range', 'fps')):
            reasons.append('候选报告或时基已变化')
    except (ValueError, TypeError, KeyError, AttributeError, ReferenceError) as exc:
        reasons = [str(exc)]
    return {'schema': SCHEMA, 'status': 'stale' if reasons else 'fresh', 'reasons': reasons,
            'document': document, 'usable_for_manual_review': not reasons,
            'automatic_detection': False, 'approval': None, 'correction_allowed': False,
            'foot_transfer_allowed': False}


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _reset_view(self, _context):
    self.validation_status = 'unchecked'
    self.message = '当前选择待校验；不会自动重算碰撞或改动作'
    self.display_summary = self.display_description = self.display_basis = ''


@persistent
def _clear_cached_validation(*_args):
    # Scene PropertyGroup SKIP_SAVE alone does not clear a stored display
    # cache in this Blender host. Reopen/undo/redo always require revalidation.
    for scene in bpy.data.scenes:
        if hasattr(scene, OPTIONS):
            _reset_view(getattr(scene, OPTIONS), None)


def _clear_validation_after_enable():
    # Native enable restricts Main during register; clear on its first tick.
    # Do not hide genuine cache cleanup errors behind an enable fallback.
    if _OWNS_OPTIONS:
        _clear_cached_validation()
    return None


def _initialize_validation():
    if hasattr(bpy.data, 'scenes'):
        _clear_cached_validation()
    else:
        bpy.app.timers.register(_clear_validation_after_enable, first_interval=0.0)


class C94CONTACTHINT_Options(bpy.types.PropertyGroup):
    target: PointerProperty(name='目标骨架', type=bpy.types.Object, poll=_armature, update=_reset_view)
    candidate: PointerProperty(name='独立基础候选', type=bpy.types.Action, update=_reset_view)
    kind: EnumProperty(name='人工复核类型', items=[('HAND_BODY', '手与身体', '人工观察提醒'),
                                               ('BODY_BODY', '身体与身体', '人工观察提醒')])
    bone_a: StringProperty(name='骨名A')
    bone_b: StringProperty(name='骨名B')
    start: IntProperty(name='起始目标帧', default=1)
    end: IntProperty(name='结束目标帧', default=1)
    description: StringProperty(name='人工观察描述', maxlen=2000)
    basis: StringProperty(name='人工依据/来源（帧、视角等）', maxlen=2000)
    hint_text: PointerProperty(name='已有人工提示Text', type=bpy.types.Text, update=_reset_view)
    validation_status: StringProperty(default='unchecked', options={'SKIP_SAVE'})
    message: StringProperty(default='仅人工提醒；尚未记录或校验', options={'SKIP_SAVE'})
    display_summary: StringProperty(options={'SKIP_SAVE'})
    display_description: StringProperty(options={'SKIP_SAVE'})
    display_basis: StringProperty(options={'SKIP_SAVE'})


def _display(opts, result):
    opts.validation_status = result['status']
    opts.message = ' / '.join(result['reasons']) if result['reasons'] else '身份、实际曲线及骨rest匹配，仅可供人工复核'
    doc = result['document']
    if doc and isinstance(doc.get('origin'), dict) and isinstance(doc.get('time'), dict):
        bones = doc.get('bones')
        labels = ' / '.join(str(n) for n in bones) if isinstance(bones, (list, tuple)) else '无有效骨列表'
        opts.display_summary = '%s | %s | 帧%s..%s' % (doc.get('kind'), labels, doc['time'].get('start'), doc['time'].get('end'))
        opts.display_description = str(doc['origin'].get('description', ''))
        opts.display_basis = str(doc['origin'].get('basis', ''))
    else:
        opts.display_summary = opts.display_description = opts.display_basis = ''


class C94CONTACTHINT_OT_review(bpy.types.Operator):
    bl_idname = 'c94_workflow.contact_review_hint'
    bl_label = '手与身体人工复核提示'
    operation: EnumProperty(items=[('RECORD', '记录人工提示', ''), ('VALIDATE', '校验所选提示', '')])

    def execute(self, context):
        opts = getattr(context.scene, OPTIONS)
        try:
            if self.operation == 'RECORD':
                opts.hint_text = create_hint(opts.target, opts.candidate, kind=opts.kind, bone_a=opts.bone_a,
                    bone_b=opts.bone_b, start=opts.start, end=opts.end, description=opts.description,
                    basis=opts.basis, scene=context.scene)
            _display(opts, validate_hint(opts.hint_text, target=opts.target, candidate=opts.candidate, scene=context.scene))
            return {'FINISHED'}
        except (ValueError, TypeError, KeyError) as exc:
            opts.validation_status = 'stale'
            opts.message = str(exc) + '；原Text保持'
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94CONTACTHINT_PT_panel(bpy.types.Panel):
    bl_idname = 'C94CONTACTHINT_PT_panel'
    bl_label = '手与身体·人工复核提示'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '动画重定向'

    def draw(self, context):
        layout = self.layout
        opts = getattr(context.scene, OPTIONS)
        layout.label(text='人工观察标注；未自动检测碰撞', icon='INFO')
        layout.label(text='仅提醒，无批准/修正或足部转交')
        for field in ('target', 'candidate', 'kind'):
            layout.prop(opts, field)
        for field in ('bone_a', 'bone_b'):
            if opts.target:
                layout.prop_search(opts, field, opts.target.data, 'bones')
            else:
                layout.prop(opts, field)
        row = layout.row(align=True)
        row.prop(opts, 'start'); row.prop(opts, 'end')
        layout.prop(opts, 'description'); layout.prop(opts, 'basis')
        layout.operator(C94CONTACTHINT_OT_review.bl_idname, text='记录为新人工提示Text').operation = 'RECORD'
        layout.separator()
        layout.prop(opts, 'hint_text')
        layout.operator(C94CONTACTHINT_OT_review.bl_idname, text='校验所选Text并显示').operation = 'VALIDATE'
        box = layout.box()
        box.label(text='上次显式校验：' + opts.validation_status + '（非实时）')
        for label in (opts.message, opts.display_summary, opts.display_description, opts.display_basis):
            for line in textwrap.wrap(label, width=44)[:6]:
                box.label(text=line)
        box.label(text='完整记录见Text；stale保留原文，不作当前依据')


_CLASSES = (C94CONTACTHINT_Options, C94CONTACTHINT_OT_review, C94CONTACTHINT_PT_panel)


def register():
    global _OWNS_OPTIONS
    require(not _REGISTERED and not hasattr(bpy.types.Scene, OPTIONS), '人工提示入口已注册，保留原实例')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls); _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94CONTACTHINT_Options))
        _OWNS_OPTIONS = True
        for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
            handlers.append(_clear_cached_validation)
        _initialize_validation()
    except Exception:
        unregister()
        raise


def unregister():
    global _OWNS_OPTIONS
    if bpy.app.timers.is_registered(_clear_validation_after_enable):
        bpy.app.timers.unregister(_clear_validation_after_enable)
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _clear_cached_validation in handlers:
            handlers.remove(_clear_cached_validation)
    if _OWNS_OPTIONS and hasattr(bpy.types.Scene, OPTIONS):
        delattr(bpy.types.Scene, OPTIONS)
    _OWNS_OPTIONS = False
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
