"""Record three native Pose Mode poses on a new, explicitly bounded Action."""

import json
import math

import bpy
from bpy.props import EnumProperty, FloatProperty, PointerProperty, StringProperty

from . import control_panel, retarget_action
from .retarget_contact_correction import action_fingerprint

OPTIONS = "c94_three_beats_options_v1"
_REGISTERED = []
_BEATS = ("迈步", "接重", "伸手")


def _active_rig(context):
    obj = context.active_object
    if not obj or obj.type != "ARMATURE" or context.mode != "POSE" or obj.mode != "POSE":
        raise ValueError("请在姿态模式激活一个本地角色骨架")
    if obj.library or obj.override_library or obj.data.library or obj.data.override_library:
        raise ValueError("请使用本地骨架工作副本")
    return obj


def _channels(rig, bone):
    if rig.get(control_panel.RIG_KEY):
        if not control_panel._is_control(bone) or not control_panel._visible(bone):
            raise ValueError(f"{bone.name} 不是可见的 C94 原控制骨")
    elif (not bone.bone.use_deform or bone.bone.hide_select or bone.hide or bone.bone.hide
          or any(not cons.mute and cons.influence != 0 for cons in bone.constraints)):
        raise ValueError(f"{bone.name} 不是可编辑的普通 FK 输出骨")
    driven = {(fc.data_path, fc.array_index) for fc in (rig.animation_data.drivers if rig.animation_data else ())}
    result = [(prop, index) for prop, index in control_panel._channels(bone)
              if (bone.path_from_id(prop), index) not in driven]
    if not result:
        raise ValueError(f"{bone.name} 没有可记录的原生通道")
    return result


def _contract(rig, names):
    rows = []
    for name in names:
        bone = rig.pose.bones.get(name)
        if bone is None:
            raise ValueError(f"约定控制骨 {name} 已不存在")
        rows.append({
            "name": name, "rotation_mode": bone.rotation_mode,
            "parent": bone.parent.name if bone.parent else None,
            "rest": [list(row) for row in bone.bone.matrix_local],
            "channels": [list(item) for item in _channels(rig, bone)],
        })
    return rows


def _pose(rig, contract):
    values = [[float(getattr(rig.pose.bones[bone["name"]], prop)[index])
               for prop, index in bone["channels"]] for bone in contract]
    if any(not math.isfinite(value) for row in values for value in row):
        raise ValueError("控制通道中有无效数值")
    return values


def _restore_pose(rig, contract, values):
    for row, part in zip(contract, values, strict=True):
        pb = rig.pose.bones.get(row["name"])
        if pb and pb.rotation_mode == row["rotation_mode"]:
            for (prop, index), value in zip(row["channels"], part, strict=True):
                getattr(pb, prop)[index] = value


def _read_draft(context, opts):
    rig = _active_rig(context)
    if not opts.draft or opts.rig != rig:
        raise ValueError("请回到记录开始时的原角色")
    draft = json.loads(opts.draft)
    ad = rig.animation_data
    if not ad or ad.action is not None or ad.use_nla or ad.use_tweak_mode:
        raise ValueError("角色的动作绑定已经改变，请恢复记录会话状态")
    return rig, draft


def _name_free(name):
    if not name or len(name.encode("utf-8")) > 63 or name in bpy.data.actions:
        raise ValueError("请输入唯一的新动作名，最多 63 个 UTF-8 字节")


def _start(context, opts):
    rig = _active_rig(context)
    if opts.draft:
        raise ValueError("先完成或取消正在记录的三拍")
    if retarget_action.has_preview(rig):
        raise ValueError("请先结束当前角色的重定向预览")
    frames = [float(opts.step_frame), float(opts.transfer_frame), float(opts.reach_frame)]
    if not all(math.isfinite(f) for f in frames) or not frames[0] < frames[1] < frames[2]:
        raise ValueError("三个目标帧必须严格递增")
    name = opts.action_name.strip()
    _name_free(name)
    names = sorted(pb.name for pb in (context.selected_pose_bones_from_active_object or ()))
    if not names:
        raise ValueError("请先选择本次要记录的原控制骨")
    contract = _contract(rig, names)
    before = retarget_action.binding(rig)
    ad = rig.animation_data
    if ad and (ad.use_tweak_mode or (
            ad.use_nla and any(not track.mute for track in ad.nla_tracks))):
        raise ValueError("请退出 NLA 微调并静音活动 NLA 后开始记录")
    action, slot = before["action"], before["slot"]
    source_fields = before["fields"]
    if action is not None and slot is None:
        raise ValueError("源 Action 缺少明确的原生 Slot")
    draft = {
        "name": name, "frames": frames, "controls": contract, "poses": [],
        "initial_pose": _pose(rig, contract), "binding_fields": source_fields,
        "binding_identity": {key: before[key] for key in ("exists", "handle", "last")},
        "source_slot": slot.identifier if slot else None,
        "source_fingerprint": action_fingerprint(action) if action else None,
        "source_fake_user": action.use_fake_user if action else None,
        "fps": context.scene.render.fps / context.scene.render.fps_base,
        "autokey": context.scene.tool_settings.use_keyframe_insert_auto,
    }
    opts.rig, opts.source = rig, action
    opts.draft = json.dumps(draft, ensure_ascii=False)
    if action is not None:
        action.use_fake_user = True
    ad = rig.animation_data_create()
    ad.action = None
    ad.use_nla = False
    context.scene.tool_settings.use_keyframe_insert_auto = False
    return f"已锁定 {len(names)} 根原控制骨；摆好{_BEATS[0]}后记录"


def _cleanup(context, opts, draft):
    context.scene.tool_settings.use_keyframe_insert_auto = draft["autokey"]
    opts.draft = ""
    opts.rig = None
    opts.source = None


def _source_slot(opts, draft):
    if not opts.source:
        if draft["source_slot"] is not None:
            raise ValueError("来源 Action 已不存在")
        return None
    slot = opts.source.slots.get(draft["source_slot"])
    if slot is None:
        raise ValueError("来源 Action 的原 Slot 已不存在")
    return slot


def _cancel(context, opts):
    rig, draft = _read_draft(context, opts)
    slot = _source_slot(opts, draft)
    _restore_pose(rig, draft["controls"], draft["initial_pose"])
    retarget_action.restore_binding(rig, {**draft["binding_identity"], "action": opts.source,
                                         "slot": slot, "fields": draft["binding_fields"]})
    if opts.source is not None and draft.get("source_fake_user") is not None:
        opts.source.use_fake_user = draft["source_fake_user"]
    _cleanup(context, opts, draft)
    context.scene.frame_set(context.scene.frame_current, subframe=context.scene.frame_subframe)
    return "已取消三拍，恢复开始时的角色绑定"


def _write_action(rig, draft, slot):
    _name_free(draft["name"])
    action = bpy.data.actions.new(draft["name"])
    try:
        slot_out = action.slots.new("OBJECT", slot.name_display if slot else rig.name)
        bag = action.layers.new("三拍粗动").strips.new(type="KEYFRAME").channelbag(slot_out, ensure=True)
        for i, row in enumerate(draft["controls"]):
            bone = rig.pose.bones[row["name"]]
            group = bag.groups.new(bone.name)
            for j, (prop, index) in enumerate(row["channels"]):
                curve = bag.fcurves.new(bone.path_from_id(prop), index=index)
                curve.group = group
                for frame, pose in zip(draft["frames"], draft["poses"], strict=True):
                    point = curve.keyframe_points.insert(frame, pose[i][j], options={"FAST"})
                    point.interpolation = "LINEAR"
                curve.update()
        action.use_fake_user = True
        return action, slot_out
    except Exception:
        bpy.data.actions.remove(action)
        raise


def _capture(context, opts):
    rig, draft = _read_draft(context, opts)
    if context.scene.tool_settings.use_keyframe_insert_auto:
        raise ValueError("录制期间请关闭自动插键")
    if context.scene.render.fps / context.scene.render.fps_base != draft["fps"]:
        raise ValueError("帧率在记录期间发生改变，请重新开始")
    if _contract(rig, [row["name"] for row in draft["controls"]]) != draft["controls"]:
        raise ValueError("原控制骨的轴向、模式或可写通道已变化")
    draft["poses"].append(_pose(rig, draft["controls"]))
    count = len(draft["poses"])
    if count < len(_BEATS):
        opts.draft = json.dumps(draft, ensure_ascii=False)
        return f"已记录{_BEATS[count - 1]}；摆好{_BEATS[count]}后记录"

    source = opts.source
    slot = _source_slot(opts, draft)
    if source is not None and action_fingerprint(source) != draft["source_fingerprint"]:
        raise ValueError("来源 Action 的曲线已变化，请取消后重新开始")
    action, out_slot = _write_action(rig, draft, slot)
    ad = rig.animation_data
    try:
        ad.action = action
        ad.action_slot = out_slot
        ad.action_blend_type = "REPLACE"
        ad.action_influence = 1.0
        ad.action_extrapolation = "HOLD"
        ad.use_nla = draft["binding_fields"].get("use_nla", False)
        if ad.action != action or ad.action_slot != out_slot:
            raise ValueError("新动作无法绑定到原控制骨槽")
    except Exception:
        ad.action = None
        ad.use_nla = False
        bpy.data.actions.remove(action)
        raise
    if source is not None:
        source.use_fake_user = True
    opts.result = action
    _cleanup(context, opts, draft)
    whole = math.floor(draft["frames"][0])
    context.scene.frame_set(whole, subframe=draft["frames"][0] - whole)
    return f"已生成三拍动作 {action.name}"


class C94BEAT_Options(bpy.types.PropertyGroup):
    action_name: StringProperty(name="新动作", default="三拍粗动")
    step_frame: FloatProperty(name="迈步帧", default=1)
    transfer_frame: FloatProperty(name="接重帧", default=13)
    reach_frame: FloatProperty(name="伸手帧", default=31)
    rig: PointerProperty(type=bpy.types.Object, options={"HIDDEN"})
    source: PointerProperty(type=bpy.types.Action, options={"HIDDEN"})
    draft: StringProperty(options={"HIDDEN"})
    result: PointerProperty(type=bpy.types.Action, options={"HIDDEN"})


class C94BEAT_OT_Record(bpy.types.Operator):
    bl_idname = "c94_workflow.three_beats"
    bl_label = "录制三拍"
    bl_options = {"REGISTER", "UNDO"}

    operation: EnumProperty(items=(("START", "开始", ""), ("CAPTURE", "记录当前姿态", ""), ("CANCEL", "取消", "")))

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.active_object and context.mode == "POSE")

    def execute(self, context):
        opts = getattr(context.scene, OPTIONS)
        try:
            status = {"START": _start, "CAPTURE": _capture, "CANCEL": _cancel}[self.operation](context, opts)
        except (ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, status)
        return {"FINISHED"}


class C94BEAT_PT_Record(bpy.types.Panel):
    bl_idname = "C94BEAT_PT_record"
    bl_label = "三拍摆姿"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "角色绑定"

    @classmethod
    def poll(cls, context):
        return C94BEAT_OT_Record.poll(context)

    def draw(self, context):
        opts = getattr(context.scene, OPTIONS)
        layout = self.layout
        if opts.draft:
            draft = json.loads(opts.draft)
            index = len(draft["poses"])
            layout.label(text=f"{_BEATS[index]} · F{draft['frames'][index]:g} · {len(draft['controls'])} 根控制")
            layout.operator(C94BEAT_OT_Record.bl_idname, text="记录当前姿态", icon="KEY_HLT").operation = "CAPTURE"
            layout.operator(C94BEAT_OT_Record.bl_idname, text="取消并恢复源动作").operation = "CANCEL"
        else:
            for prop in ("action_name", "step_frame", "transfer_frame", "reach_frame"):
                layout.prop(opts, prop)
            layout.operator(C94BEAT_OT_Record.bl_idname, text="用所选控制骨开始").operation = "START"
            if opts.result:
                layout.label(text=opts.result.name, icon="ACTION")


_CLASSES = (C94BEAT_Options, C94BEAT_OT_Record, C94BEAT_PT_Record)


def register():
    if _REGISTERED or hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError("三拍面板已由其他入口注册")
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94BEAT_Options))
    except Exception:
        unregister()
        raise


def unregister():
    if _REGISTERED and hasattr(bpy.types.Scene, OPTIONS):
        delattr(bpy.types.Scene, OPTIONS)
    while _REGISTERED:
        cls = _REGISTERED[-1]
        bpy.utils.unregister_class(cls)
        _REGISTERED.pop()
