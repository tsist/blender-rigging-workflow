"""Left and right FK shoulder pose authoring on mapped control graphs."""
import json
import math
from pathlib import Path

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from mathutils import Matrix, Quaternion, Vector

from . import pose_switching, retarget_action, retarget_contact_correction, retarget_job
from .posing_rom import multi_axis_review, shoulder_response

SETTINGS = "rigging_phase2_shoulder_rom"
OPTIONS = SETTINGS
MARKER = "rigging_phase2_shoulder_pose_v1"
ADVISORY = {"elevation": (0.0, 100.0), "move_plane": (-15.0, 105.0)}
HIP_LIMITS = {"forward_flexion": (-25.0, 105.0), "abduction": (-20.0, 55.0)}
TORSO_LIMITS = {"forward_flexion": (-25.0, 45.0), "side_bend": (-25.0, 25.0)}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def profile(rig, side="L", semantic_map=None):
    require(side in {"L", "R"}, "明确指定 L 或 R 侧")
    suffix = "." + side
    other = ".R" if side == "L" else ".L"
    if rig.get(pose_switching.RIG_KEY):
        pose_switching._metadata(rig)
        arms, _, mode = pose_switching._arm(rig, side)
        require(mode == 0., "目标手臂须处于原生 FK 端点")
        require("肩膀"+suffix in rig.pose.bones and "大臂FK"+other in rig.pose.bones, "缺少肩带或对侧参考骨")
        return {"family":f"C94_{'LEFT' if side == 'L' else 'RIGHT'}_FK","shoulder":"肩膀"+suffix,
                "upper":arms["fk"][0],"elbow":arms["fk"][1],"chest":"胸腔2","other":"大臂FK"+other,
                "side":side}
    from . import semantic_rig_map

    mapping = semantic_rig_map.describe(rig, semantic_map)
    controls = mapping["controls"]
    fields = {"upper": "upper_arm"+suffix, "elbow": "forearm"+suffix,
              "other": "upper_arm"+other, "chest": "chest"}
    if "rigging_modular_v1" in rig:
        owner = rig.pose.bones.get("upper_arm_parent"+suffix)
        require(owner is not None and owner.get("IK_FK") == 1., "目标手臂须处于原生 FK 端点")
    require(all(role in controls for role in fields.values()), "语义映射缺少肩带或对侧参考骨")
    spec = {key: controls[value] for key, value in fields.items()}
    if "shoulder"+suffix in controls:
        spec["shoulder"] = controls["shoulder"+suffix]
    spec.update(family=mapping["kind"]+"_SEMANTIC_FK", side=side)
    return spec


def world_matrices(rig, spec):
    evaluated = pose_switching._update(rig)
    return {key:evaluated.matrix_world @ evaluated.pose.bones[name].matrix
            for key, name in spec.items() if key not in {"family", "side"}}


def assign_rotation(bone, rotation):
    if bone.rotation_mode == "QUATERNION":
        bone.rotation_quaternion = rotation
        return "rotation_quaternion"
    require(bone.rotation_mode in {"XYZ","XZY","YXZ","YZX","ZXY","ZYX"}, "当前控制仅支持欧拉或四元数通道")
    bone.rotation_euler = rotation.to_euler(bone.rotation_mode, bone.rotation_euler.copy())
    return "rotation_euler"


def set_upper(rig, name, desired, allow_location):
    evaluated = pose_switching._update(rig)
    current = evaluated.pose.bones[name]
    parent = ({"parent_matrix": current.parent.matrix, "parent_matrix_local": current.parent.bone.matrix_local}
              if current.parent else {})
    basis = current.bone.convert_local_to_pose(evaluated.matrix_world.inverted() @ desired,
        current.bone.matrix_local, invert=True, **parent)
    original = rig.pose.bones[name]
    fields = [assign_rotation(original, basis.to_quaternion())]
    if allow_location:
        original.location = basis.translation
        fields.append("location")
    return [(original, field) for field in fields]


def shoulder_pose(rig, spec, elevation_deg, plane_deg, strength, manual, advisory):
    baseline = world_matrices(rig, spec)
    up = Vector((0,0,1))
    side = baseline["upper"].translation - baseline["other"].translation
    side.z = 0.
    require(side.length > .02, "左、右上臂中心不足以确定摆姿朝向")
    side.normalize()
    forward = side.cross(up).normalized() * (1 if spec["side"] == "L" else -1)
    planar = math.radians(plane_deg)
    elevation = math.radians(elevation_deg)
    lift, protract = shoulder_response(elevation_deg, max(0., math.cos(planar)), strength, manual)
    motion = Quaternion(side.cross(up), math.radians(lift)) @ Quaternion(side.cross(forward), math.radians(protract))
    new_keys = []
    native_name = spec.get("shoulder")
    if native_name:
        bone = rig.pose.bones[native_name]
        evaluated_q = baseline["shoulder"].to_quaternion()
        field = assign_rotation(bone, bone.matrix_basis.to_quaternion() @
                                (evaluated_q.inverted() @ motion @ evaluated_q))
        new_keys.append((bone, field))
        source_upper = world_matrices(rig, spec)["upper"]
        origin = source_upper.translation
    else:
        source_upper = baseline["upper"]
        chest = baseline["chest"].translation
        origin = chest + motion @ (source_upper.translation - chest)
    direction = -up * math.cos(elevation) + (forward * math.cos(planar) + side * math.sin(planar)) * math.sin(elevation)
    old_direction = baseline["elbow"].translation - baseline["upper"].translation
    rotation = old_direction.rotation_difference(direction) @ baseline["upper"].to_quaternion()
    new_keys.extend(set_upper(rig, spec["upper"],
        Matrix.LocRotScale(origin, rotation, source_upper.to_scale()), native_name is None))
    return new_keys, (side, forward, up), {"lift_deg":lift,"protract_deg":protract,
                                          "follow_strength":strength,"manual_deg":manual,"advisory_strength":advisory}


def report_pose(rig, spec, axes, strength):
    matrices = world_matrices(rig, spec)
    side, forward, up = axes
    upper = matrices["upper"].translation
    direction = (matrices["elbow"].translation - upper).normalized()
    result = multi_axis_review({
        "elevation":math.degrees(math.acos(max(-1., min(1., direction.dot(-up))))),
        "move_plane":math.degrees(math.atan2(direction.dot(side), direction.dot(forward))),
    }, ADVISORY, strength)
    return result, list(upper)


def build_action(context, rig, frame, direction, elevation, follow, manual, advisory, *, side="L", semantic_map=None):
    require(isinstance(rig, bpy.types.Object) and rig.type == "ARMATURE" and rig.library is None
            and rig.data.library is None and rig.mode != "EDIT" and rig.name in context.view_layer.objects,
            "请选择当前视图层可编辑的骨架")
    require(not retarget_action.has_preview(rig), "请结束当前动作预览")
    scene = context.scene
    ad = rig.animation_data
    require(ad is not None and ad.action is not None and ad.action_slot is not None
            and not ad.use_tweak_mode and ad.action_blend_type == "REPLACE"
            and ad.action_influence == 1. and not (ad.use_nla and any(not t.mute for t in ad.nla_tracks)),
            "请选择独立、满影响、无活动 NLA 混合的原始 Action")
    basis, slot = ad.action, ad.action_slot
    require(len(basis.slots) == 1 and len(basis.layers) == 1 and len(basis.layers[0].strips) == 1
            and slot.target_id_type == "OBJECT", "当前摆姿只支持单槽单层单条带")
    require(basis.frame_range[0] <= frame <= basis.frame_range[1], "原摆姿帧在基础 Action 范围外")
    require(direction in {"FORWARD","ABDUCTION"} and 0. <= elevation <= 135.
            and -15. <= manual <= 15., "摆姿方向、角度或手动偏移超出当前范围")
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    original = retarget_contact_correction.action_fingerprint(basis)
    action = None
    raw_before = []
    try:
        scene.frame_set(frame)
        spec = profile(rig, side, semantic_map)
        for role in ("shoulder", "upper"):
            if role not in spec:
                continue
            bone = rig.pose.bones[spec[role]]
            field = "rotation_quaternion" if bone.rotation_mode == "QUATERNION" else "rotation_euler"
            raw_before.append((bone, field, getattr(bone, field).copy()))
            if role == "upper" and "shoulder" not in spec:
                raw_before.append((bone, "location", bone.location.copy()))
        changed_paths = {bone.path_from_id(field) for bone, field, _ in raw_before}
        require(not any(fc.data_path in changed_paths for fc in ad.drivers), "当前目标控制被驱动器占用")
        action = basis.copy()
        action.name = f"肩带姿态 {frame:03d} {direction}"
        action.use_fake_user = True
        for marker in (retarget_action.META, retarget_contact_correction.META, retarget_job.JOB_KEY, MARKER):
            if marker in action:
                del action[marker]
        target_slot, = [item for item in action.slots if item.identifier == slot.identifier]
        ad.action, ad.action_slot = action, target_slot
        scene.frame_set(frame)
        changed, axes, settings = shoulder_pose(rig, spec, elevation, 10. if direction=="FORWARD" else 80.,
                                           follow, manual, advisory)
        paths = {bone.path_from_id(field) for bone, field in changed}
        _, proposed_head = report_pose(rig,spec,axes,advisory)
        bag = action.layers[0].strips[0].channelbag(target_slot)
        require(all(not fc.modifiers and not fc.sampled_points for fc in bag.fcurves if fc.data_path in paths),
                "候选目标曲线含采样点或修改器，需要人工接续")
        for bone, field in changed:
            require(bone.keyframe_insert(data_path=field, frame=frame), "原控制关键帧写入失败")
        scene.frame_set(frame)
        review, head = report_pose(rig,spec,axes,advisory)
        require((Vector(head) - Vector(proposed_head)).length < 1e-5,
                "候选关键帧求值与拟议上臂中心不同")
        result = {"family":spec["family"],"side":side,"pose_frame":frame,"plane":direction,"requested_deg":elevation,
                  "base_action":basis.name,"base_fingerprint":original,"slot":slot.identifier,"settings":settings,
                  "changed_paths":sorted(paths),"study_reference":"当前直立角色的世界上方及肩关节中心",
                  "review":review,"upper_head":head}
        differences = retarget_contact_correction._action_diff(basis, action)
        require(all(row["path"] in paths for row in differences), "出现未声明的原控制曲线改动")
        require(retarget_contact_correction.action_fingerprint(basis) == original, "基础动作内容意外变化")
        action[MARKER] = json.dumps(result, ensure_ascii=False)
        return action, result
    except BaseException:
        ad.action, ad.action_slot = basis, slot
        if action is not None:
            bpy.data.actions.remove(action)
        raise
    finally:
        ad.action, ad.action_slot = basis, slot
        # Rebinding an Action cannot restore native inputs that had no base curve.
        for bone, field, value in raw_before:
            setattr(bone, field, value)
        scene.frame_set(old_frame, subframe=old_subframe)


def armature_poll(_, value):
    return value.type == "ARMATURE"


def _body_axes(rig, spec):
    landmarks = world_matrices(rig, spec)
    up = Vector((0, 0, 1))
    side = landmarks["upper"].translation - landmarks["other"].translation
    side.z = 0.
    require(side.length > .02, "肩带中心不足以确定本次直立姿态参考")
    side.normalize()
    return side, side.cross(up).normalized() * (1 if spec["side"] == "L" else -1), up


def _rom_landmarks(rig, spec, semantic_map=None):
    suffix = "." + spec["side"]
    if spec["family"].startswith("C94_"):
        names = {"hip": "大腿FK"+suffix, "knee": "膝盖FK"+suffix, "chest": "拉伸-胸腔2"}
    else:
        from . import semantic_rig_map

        mapping = semantic_rig_map.describe(rig, semantic_map)["bones"]
        names = {field: mapping.get(role) for field, role in
                 {"hip":"thigh"+suffix, "knee":"shin"+suffix, "chest":"chest"}.items()}
    require(all(name and name in rig.pose.bones for name in names.values()), "缺少已验关节观测点")
    return world_matrices(rig, names)


def capture_rom_reference(rig, *, side="L", semantic_map=None):
    spec = profile(rig, side, semantic_map)
    arm_side = side
    side, forward, up = _body_axes(rig, spec)
    torso = _rom_landmarks(rig, spec, semantic_map)["chest"].to_quaternion()
    return {"rig": rig.name, "data": rig.data.name, "family": spec["family"], "side": list(side),
            "forward": list(forward), "up": list(up), "torso": list(torso), "arm_side":arm_side}


def inspect_rom(rig, reference, strength, *, semantic_map=None):
    spec = profile(rig, reference.get("arm_side", "L"), semantic_map)
    require((rig.name, rig.data.name, spec["family"]) == (reference["rig"], reference["data"], reference["family"]),
            "请为当前骨架先记录直立参考")
    side, forward, up = (Vector(reference[key]) for key in ("side", "forward", "up"))
    observed = _rom_landmarks(rig, spec, semantic_map)
    thigh = (observed["knee"].translation - observed["hip"].translation).normalized()
    tilt = (observed["chest"].to_quaternion() @ Quaternion(reference["torso"]).inverted()) @ up
    hip = {"forward_flexion": math.degrees(math.atan2(thigh.dot(forward), -thigh.dot(up))),
           "abduction": math.degrees(math.atan2(thigh.dot(side), -thigh.dot(up)))}
    torso = {"forward_flexion": math.degrees(math.atan2(tilt.dot(forward), tilt.dot(up))),
             "side_bend": math.degrees(math.atan2(tilt.dot(side), tilt.dot(up)))}
    return {"hip": multi_axis_review(hip, HIP_LIMITS, strength),
            "thoracolumbar": multi_axis_review(torso, TORSO_LIMITS, strength)}


class PHASE2SHOULDER_Settings(bpy.types.PropertyGroup):
    target: PointerProperty(name="角色骨架", type=bpy.types.Object, poll=armature_poll)
    semantic_map_file: StringProperty(name="语义骨骼映射", subtype="FILE_PATH",
        description="自定义骨名的角色可选用 Contract C4 语义映射 JSON")
    pose_frame: IntProperty(name="原摆姿帧", default=1)
    side: EnumProperty(name="侧", items=(("L","左",""),("R","右","")), default="L")
    direction: EnumProperty(name="摆姿方向", items=(("FORWARD","前举",""),("ABDUCTION","外展","")))
    elevation_deg: FloatProperty(name="上臂抬举（度）", default=65.0, min=0.0, max=135.0)
    follow_strength: FloatProperty(name="肩带跟随", description="0 时逐姿旁路自动肩带", default=1., min=0., max=1.)
    manual_deg: FloatProperty(name="耸肩或压肩（度）", default=0., min=-15., max=15.)
    advisory_strength: FloatProperty(name="参考提示强度", description="0 时旁路多轴范围提示",
                                     default=1., min=0., max=1.)
    latest: PointerProperty(name="保留候选", type=bpy.types.Action)
    message: StringProperty(name="最近读数")
    rom_reference: StringProperty(options={"HIDDEN"})
    rom_results: StringProperty(options={"HIDDEN"})


class PHASE2SHOULDER_OT_rom(bpy.types.Operator):
    bl_idname = "c94_workflow.inspect_human_rom"
    bl_label = "髋与胸腰范围提示"
    operation: EnumProperty(items=(("REFERENCE","记录直立参考",""),("READ","读取当前姿态","")))

    def execute(self, context):
        settings = getattr(context.scene, SETTINGS)
        try:
            require(settings.target is not None, "先选择角色骨架")
            mapping = _panel_mapping(settings)
            if self.operation == "REFERENCE":
                settings.rom_reference = json.dumps(capture_rom_reference(settings.target, side=settings.side,
                                                                          semantic_map=mapping), ensure_ascii=False)
                settings.rom_results = ""
            else:
                require(settings.rom_reference, "先记录该角色的直立参考")
                settings.rom_results = json.dumps(inspect_rom(settings.target, json.loads(settings.rom_reference),
                    settings.advisory_strength, semantic_map=mapping), ensure_ascii=False)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class PHASE2SHOULDER_OT_build(bpy.types.Operator):
    bl_idname = "c94_workflow.shoulder_rom_candidate"
    bl_label = "保留肩带姿势候选动作"
    bl_options = {"REGISTER","UNDO"}

    def execute(self, context):
        settings = getattr(context.scene, SETTINGS)
        try:
            action, result = build_action(context, settings.target, settings.pose_frame, settings.direction,
                settings.elevation_deg, settings.follow_strength, settings.manual_deg, settings.advisory_strength,
                side=settings.side, semantic_map=_panel_mapping(settings))
        except (OSError, ValueError, RuntimeError) as exc:
            settings.message = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        settings.latest = action
        value = result["review"]["elevation"]
        settings.message = f"实际抬举 {value['measured_deg']:.1f}°，参考超量 {value['advisory_deg']:.1f}°"
        self.report({"INFO"}, settings.message)
        return {"FINISHED"}


class PHASE2SHOULDER_PT_panel(bpy.types.Panel):
    bl_idname = "PHASE2SHOULDER_PT_panel"
    bl_label = "肩带与关节参考"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "动画重定向"

    def draw(self, context):
        settings = getattr(context.scene, SETTINGS)
        layout = self.layout
        for name in ("target","semantic_map_file","side","pose_frame","direction","elevation_deg","follow_strength","manual_deg","advisory_strength"):
            layout.prop(settings, name)
        layout.operator(PHASE2SHOULDER_OT_build.bl_idname)
        if settings.latest:
            layout.label(text=settings.latest.name)
        if settings.message:
            layout.label(text=settings.message)
        row = layout.row(align=True)
        row.operator(PHASE2SHOULDER_OT_rom.bl_idname, text="记录直立参考").operation = "REFERENCE"
        row.operator(PHASE2SHOULDER_OT_rom.bl_idname, text="读取当前髋与胸腰").operation = "READ"
        if settings.rom_results:
            labels = {"hip": "髋", "thoracolumbar": "胸腰", "forward_flexion": "前屈",
                      "abduction": "外展", "side_bend": "侧弯"}
            for group, axes in json.loads(settings.rom_results).items():
                for axis, result in axes.items():
                    lo, hi = result["reference_deg"]
                    layout.label(text=f"{labels[group]}{labels[axis]} {result['measured_deg']:.1f}°"
                                      f" / 参考 {lo:g}…{hi:g}° / 提示 {result['advisory_deg']:.1f}°")


_CLASSES = (PHASE2SHOULDER_Settings, PHASE2SHOULDER_OT_build, PHASE2SHOULDER_OT_rom, PHASE2SHOULDER_PT_panel)
_REGISTERED = []
_owns_options = False


def _panel_mapping(settings):
    filename = settings.semantic_map_file.strip()
    if not filename:
        return None
    path = Path(bpy.path.abspath(filename))
    return json.loads(path.read_text(encoding="utf-8-sig"))


def register():
    global _owns_options
    require(not _REGISTERED and not hasattr(bpy.types.Scene, SETTINGS), "摆姿面板已有注册")
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
        _REGISTERED.append(cls)
    setattr(bpy.types.Scene, SETTINGS, PointerProperty(type=PHASE2SHOULDER_Settings))
    _owns_options = True


def unregister():
    global _owns_options
    if _owns_options and hasattr(bpy.types.Scene, SETTINGS):
        delattr(bpy.types.Scene, SETTINGS)
    _owns_options = False
    for cls in reversed(_REGISTERED):
        bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
