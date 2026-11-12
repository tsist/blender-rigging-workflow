# SPDX-License-Identifier: GPL-3.0-or-later
"""Narrow UI for pure-JSON retarget profiles and existing-core calibration reuse."""

import json
import uuid
from pathlib import Path

import bpy
from bpy_extras.io_utils import ImportHelper
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty,
    StringProperty,
)

from . import retarget_action as action_api
from . import retarget_bvh as bvh
from . import retarget_profile as rp
from . import retarget_source as source_api


OPTIONS = "c94_retarget_profile_options_v1"
TEXT_OWNER = "c94_retarget_profile_ui_v1"
TEXT_KIND = "c94_retarget_profile_text_kind_v1"
TEXT_BASE_REQUEST = "c94_retarget_profile_text_base_v1"
_REGISTERED = []
_OWNED = False


def _armature(self, obj):
    return obj.type == "ARMATURE"


def _options(context):
    return getattr(context.scene, OPTIONS)


def _unique_text_name(name):
    if name not in bpy.data.texts:
        return name
    index = 1
    while f"{name}.{index:03d}" in bpy.data.texts:
        index += 1
    return f"{name}.{index:03d}"


def _is_owned_text(item, name, kind):
    return (item.get(TEXT_OWNER) == TEXT_OWNER
            and item.get(TEXT_BASE_REQUEST) == name
            and item.get(TEXT_KIND) == str(kind))


def _find_owned_text(name, kind):
    text = bpy.data.texts.get(name)
    if text is not None and _is_owned_text(text, name, kind):
        return text
    # Do not adopt arbitrary same-prefix tool Text: only an exact logical
    # base-request + kind pair proves it is the same product.
    candidates = [item for item in bpy.data.texts
                  if _is_owned_text(item, name, kind)]
    return sorted(candidates, key=lambda item: item.name)[0] if candidates else None


def _write_text(name, content, *, kind="managed"):
    """Write only tool-owned Text; never overwrite an unrelated user Text."""
    kind = str(kind)
    requested = str(name)
    text = _find_owned_text(requested, kind)
    if text is None:
        name = _unique_text_name(requested)
        text = bpy.data.texts.new(name)
        text[TEXT_OWNER] = TEXT_OWNER
    else:
        name = text.name
        text.clear()
    text.write(content)
    text.use_fake_user = True
    text[TEXT_KIND] = kind
    text[TEXT_BASE_REQUEST] = requested
    return text


def _read_profile(opts):
    if not opts.profile_text_name:
        raise ValueError("请先加载或导出profile")
    text = bpy.data.texts.get(opts.profile_text_name)
    if text is None:
        raise ValueError("profile Text已不存在；请重新加载")
    return rp.profile_from_json(text.as_string())


def _store_profile(opts, profile):
    text_name = "C94_RetargetProfile_" + str(profile.get("id") or uuid.uuid4().hex[:8])
    text = _write_text(text_name, rp.profile_to_json(profile), kind="profile")
    opts.profile_text_name = text.name
    opts.mapping_json = json.dumps(
        profile["mapping"]["bones"], ensure_ascii=False, sort_keys=True)
    opts.profile_id = profile.get("id") or ""
    opts.tier = profile.get("tier") or ""
    opts.summary = "profile已加载：" + opts.tier
    return text_name


def _parse_mapping(opts):
    text = (opts.mapping_json or "").strip()
    if not text or text == "{}":
        return None
    value = json.loads(text)
    if not isinstance(value, dict) or not value:
        raise ValueError("mapping_json必须是非空JSON对象")
    return value


def _source_profile(source, mapping, source_fps=None):
    try:
        saved = json.loads(source.get(bvh.SOURCE_KEY, ""))
    except (ValueError, TypeError):
        saved = None
    if isinstance(saved, dict) and saved.get("schema") == "c94.retarget.bvh_source.v1":
        record = saved.get("record") or {}
        try:
            exact = source_api.exact_cmu_mapping(source)
        except ValueError:
            exact = None
        if mapping is None and exact is not None:
            return rp.from_cmu(source, import_record=record,
                               source_fps=source_fps)
        use = mapping or (exact["bones"] if exact else None)
        if use is None:
            raise ValueError("BVH来源需要明确mapping_json")
        return rp.from_bvh(source, use, import_record=record,
                           source_fps=source_fps)
    try:
        saved = json.loads(source.get(source_api.SOURCE_KEY, ""))
    except (ValueError, TypeError):
        saved = None
    if isinstance(saved, dict) and saved.get("format") == "FBX":
        if mapping is None:
            return rp.from_mixamo(source, import_record=saved,
                                  source_fps=source_fps)
        try:
            mixamo_bones = source_api.mapping(source)["bones"]
        except ValueError:
            mixamo_bones = None
        if mixamo_bones is not None and mapping == mixamo_bones:
            return rp.from_mixamo(source, import_record=saved,
                                  source_fps=source_fps)
        return rp.from_fbx(source, mapping, import_record=saved,
                           source_fps=source_fps)
    if mapping is None:
        raise ValueError("场景Action来源需要明确mapping_json")
    return rp.from_scene(source, mapping, source_fps=source_fps)


class C94PROFILE_Options(bpy.types.PropertyGroup):
    source: PointerProperty(name="源骨架", type=bpy.types.Object, poll=_armature)
    target: PointerProperty(name="C94目标", type=bpy.types.Object, poll=_armature)
    profile_path: StringProperty(name="Profile JSON", subtype="FILE_PATH", default="")
    profile_text_name: StringProperty(default="", options={"HIDDEN"})
    profile_id: StringProperty(name="Profile ID", default="")
    mapping_json: StringProperty(name="语义映射JSON", default="{}")
    tier: StringProperty(name="等级", default="")
    calibration_state: StringProperty(name="校准复用", default="")
    status: StringProperty(name="状态", default="")
    summary: StringProperty(name="摘要", default="")
    candidate: PointerProperty(name="候选Action", type=bpy.types.Action)
    motion: EnumProperty(
        name="根运动",
        items=(("PRESERVE", "保留移动与转向", ""), ("IN_PLACE", "原地", "")),
        default="PRESERVE")
    sample_step: IntProperty(name="采样步长", min=1, max=60, default=12)
    range_start: IntProperty(name="起始帧（0=自动）", min=0, default=0)
    range_end: IntProperty(name="结束帧（0=自动）", min=0, default=0)
    override_fps: BoolProperty(name="覆盖源帧率", default=False)
    source_fps: FloatProperty(name="源帧率", min=0.01, max=1000.0, default=30.0)
    replace_existing: BoolProperty(
        name="允许覆盖已有Profile", default=False,
        description="导出的profile文件已存在时必须显式勾选；不会静默覆盖")


def _checked_write(profile, path, replace_existing):
    target = Path(bpy.path.abspath(path))
    if target.exists() and not replace_existing:
        raise ValueError(
            "Profile文件已存在，未勾选'允许覆盖已有Profile'；"
            "请换路径或显式勾选覆盖：" + str(target))
    return rp.write_profile(profile, str(target))


class C94PROFILE_OT_import_source(bpy.types.Operator, ImportHelper):
    bl_idname = "c94_workflow.profile_import_source"
    bl_label = "导入通用FBX / BVH动画"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ""
    filter_glob: StringProperty(default="*.fbx;*.bvh", options={"HIDDEN"})
    scale: FloatProperty(name="BVH每源单位对应Blender单位", default=.01,
                         min=.000001, max=100)
    forward: EnumProperty(name="BVH源前向", default="Z",
                          items=[(v,v,"") for v in ("X","Y","Z","-X","-Y","-Z")])
    up: EnumProperty(name="BVH源上向", default="Y",
                     items=[(v,v,"") for v in ("X","Y","Z","-X","-Y","-Z")])

    def execute(self, context):
        try:
            suffix = Path(self.filepath).suffix.lower()
            if suffix == ".bvh":
                imported = bvh.import_file(self.filepath, scale=self.scale,
                                           forward=self.forward, up=self.up)
            elif suffix == ".fbx":
                imported = source_api.import_fbx(self.filepath)
            else:
                raise ValueError("请选择FBX或BVH动画文件")
            opts = _options(context)
            opts.source = imported["rig"]
            # Reset staging references, retaining previous Text and Action IDs.
            opts.profile_text_name = ""; opts.profile_id = ""
            opts.tier = ""; opts.calibration_state = ""; opts.status = ""
            opts.candidate = None
            try:
                proposal = (source_api.exact_cmu_mapping(opts.source)
                            if suffix == ".bvh" else source_api.mapping(opts.source))
            except ValueError:
                proposal = None
            opts.mapping_json = json.dumps(proposal["bones"] if proposal else {},
                                             ensure_ascii=False, sort_keys=True)
            opts.summary = ("来源已导入；已识别映射，可导出Profile后校验"
                            if proposal else "来源已导入；请加载已有Profile或填写语义映射后导出")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94PROFILE_OT_export(bpy.types.Operator):
    bl_idname = "c94_workflow.profile_export"
    bl_label = "导出来源Profile"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        opts = _options(context)
        try:
            if opts.source is None:
                raise ValueError("请选择源骨架")
            mapping = _parse_mapping(opts)
            profile = _source_profile(
                opts.source, mapping,
                source_fps=opts.source_fps if opts.override_fps else None)
            if not opts.profile_path:
                raise ValueError("请指定profile JSON路径")
            written = _checked_write(profile, opts.profile_path,
                                     opts.replace_existing)
            _store_profile(opts, profile)
            opts.status = "已导出：" + written["file"]
            opts.summary = "导出SHA " + written["sha256"][:12]
            return {"FINISHED"}
        except Exception as exc:
            opts.summary = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94PROFILE_OT_load(bpy.types.Operator):
    bl_idname = "c94_workflow.profile_load"
    bl_label = "加载Profile JSON"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        opts = _options(context)
        try:
            if not opts.profile_path:
                raise ValueError("请指定profile JSON路径")
            profile = rp.load_profile(opts.profile_path)
            _store_profile(opts, profile)
            opts.status = "已加载：" + opts.profile_path
            return {"FINISHED"}
        except Exception as exc:
            opts.summary = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94PROFILE_OT_validate(bpy.types.Operator):
    bl_idname = "c94_workflow.profile_validate"
    bl_label = "校验Profile与复用状态"
    bl_options = {"REGISTER"}

    def execute(self, context):
        opts = _options(context)
        try:
            profile = _read_profile(opts)
            validation = rp.validate_profile(profile, opts.source, opts.target)
            status = rp.calibration_status(profile, opts.source, opts.target) \
                if opts.source is not None and opts.target is not None else {"state": "not_checked"}
            opts.tier = validation["tier"]
            opts.calibration_state = status.get("state", "not_checked")
            opts.status = "校验：" + ("可应用" if validation["applies"] else "不可应用")
            opts.mapping_json = json.dumps(
                profile["mapping"]["bones"], ensure_ascii=False, sort_keys=True)
            text = _write_text(
                "C94_RetargetProfile_Validation",
                json.dumps({"validation": validation, "calibration": status},
                           ensure_ascii=False, indent=2), kind="validation")
            opts.summary = "校验结果见Text：" + text.name
            if not validation["applies"]:
                self.report({"ERROR"}, "; ".join(validation["errors"]))
                return {"CANCELLED"}
            return {"FINISHED"}
        except Exception as exc:
            opts.summary = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94PROFILE_OT_convert(bpy.types.Operator):
    bl_idname = "c94_workflow.profile_convert"
    bl_label = "按Profile生成候选"
    bl_options = {"REGISTER"}

    def execute(self, context):
        opts = _options(context)
        try:
            if opts.source is None or opts.target is None:
                raise ValueError("请选择源和目标")
            profile = _read_profile(opts)
            start = opts.range_start or None
            end = opts.range_end or None
            wm = context.window_manager
            wm.progress_begin(0, 100)
            try:
                result = rp.convert(
                    opts.source, opts.target, profile,
                    start=start, end=end, output_start=1,
                    source_fps=opts.source_fps if opts.override_fps else None,
                    motion=opts.motion, sample_step=opts.sample_step,
                    progress=lambda phase, i, total: wm.progress_update(
                        (0 if phase == "SOURCE" else 50) + 50 * i / total))
            finally:
                wm.progress_end()
            from . import retarget_contact_detection_ui as detection_ui
            detection_ui.complete_from_context(context,opts.source,opts.target,result)
            opts.candidate = result["action"]
            opts.tier = (result["report"].get("profile") or {}).get("tier", opts.tier)
            opts.calibration_state = "reused" if result["report"].get(
                "profile_calibration_reused") else "fresh"
            action_api.begin_preview(opts.target, result["action"], slot=result["slot"])
            text = _write_text(
                "C94_RetargetProfile_Report_" + result["action"].name,
                json.dumps(result["report"], ensure_ascii=False, indent=2), kind="report")
            opts.status = "候选预览中：" + result["action"].name
            opts.summary = "报告Text：" + text.name
            return {"FINISHED"}
        except Exception as exc:
            opts.summary = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94PROFILE_OT_stop(bpy.types.Operator):
    bl_idname = "c94_workflow.profile_stop"
    bl_label = "停止预览并恢复"
    bl_options = {"REGISTER"}

    def execute(self, context):
        opts = _options(context)
        try:
            # Resolve the one active preview from the shared _preview store;
            # stop_preview keeps its external-binding refusal.
            action_api.stop_scene_preview(context.scene)
            opts.status = "已恢复原动作，候选保留"
            return {"FINISHED"}
        except Exception as exc:
            opts.summary = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94PROFILE_OT_remember(bpy.types.Operator):
    bl_idname = "c94_workflow.profile_remember"
    bl_label = "将候选校准写回Profile"
    bl_options = {"REGISTER"}

    def execute(self, context):
        opts = _options(context)
        try:
            if opts.candidate is None:
                raise ValueError("请选择候选Action")
            if not opts.profile_path:
                raise ValueError("请指定profile JSON路径")
            profile = _read_profile(opts)
            updated = rp.remember_from_action(profile, opts.candidate)
            written = _checked_write(updated, opts.profile_path,
                                     opts.replace_existing)
            _store_profile(opts, updated)
            opts.status = "校准已写回：" + written["file"]
            opts.calibration_state = "stored"
            return {"FINISHED"}
        except Exception as exc:
            opts.summary = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94PROFILE_PT_panel(bpy.types.Panel):
    bl_idname = "C94PROFILE_PT_panel"
    bl_label = "通用来源重定向Profile"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "动画重定向"

    def draw(self, context):
        opts = _options(context)
        layout = self.layout
        workspace = getattr(context.scene, 'c94_workspace_options_v1', None)
        advanced = workspace is None or workspace.level != 'DAILY'
        layout.operator("c94_workflow.profile_import_source", text="导入通用FBX / BVH")
        layout.prop(opts, "source")
        layout.prop(opts, "target")
        layout.prop(opts, "profile_path")
        if advanced:
            layout.prop(opts, "replace_existing")
        row = layout.row(align=True)
        row.operator("c94_workflow.profile_export", text="导出")
        row.operator("c94_workflow.profile_load", text="加载")
        if advanced:
            layout.prop(opts, "mapping_json")
            layout.label(text="等级：" + (opts.tier or "未校验"))
            layout.label(text="校准：" + (opts.calibration_state or "未检查"))
        else:
            layout.label(text='未知骨名与映射可切换进阶配置')
        layout.prop(opts, "motion")
        layout.prop(opts, "sample_step")
        layout.prop(opts, "range_start")
        layout.prop(opts, "range_end")
        layout.prop(opts, "override_fps")
        if opts.override_fps:
            layout.prop(opts, "source_fps")
        row = layout.row(align=True)
        row.operator("c94_workflow.profile_validate", text="校验/复用状态")
        row.operator("c94_workflow.profile_convert", text="生成候选")
        layout.prop(opts, "candidate")
        row = layout.row(align=True)
        row.operator("c94_workflow.profile_stop", text="停止恢复")
        if advanced:
            row.operator("c94_workflow.profile_remember", text="写回校准")
        layout.label(text=opts.status[:72], icon="INFO")
        layout.label(text=opts.summary[:72], icon="TEXT")
        if advanced:
            layout.label(text="纯JSON配置；不执行Python预设")
            layout.label(text="REST朝向 / 片段起点 / 腿长比例")
        layout.label(text="仅使用选定动作；未合成来源NLA轨道")


_CLASSES = (
    C94PROFILE_Options, C94PROFILE_OT_import_source, C94PROFILE_OT_export, C94PROFILE_OT_load,
    C94PROFILE_OT_validate, C94PROFILE_OT_convert, C94PROFILE_OT_stop,
    C94PROFILE_OT_remember, C94PROFILE_PT_panel,
)


def register():
    global _OWNED
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError("通用来源Profile面板已由其他实例注册，原实例保持")
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94PROFILE_Options))
        _OWNED = True
    except Exception:
        unregister()
        raise


def unregister():
    global _OWNED
    if _OWNED and hasattr(bpy.types.Scene, OPTIONS):
        delattr(bpy.types.Scene, OPTIONS)
    _OWNED = False
    for cls in reversed(_REGISTERED):
        if getattr(cls, "is_registered", False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
