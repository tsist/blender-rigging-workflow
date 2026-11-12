# SPDX-License-Identifier: GPL-3.0-or-later
"""Narrow queue UI for explicit FK targets and pure-JSON source profiles.

It only prepares the two job payloads and calls the existing
``retarget_job``/``retarget_job_ui`` lifecycle.  Status, cancel, collect and
preview are delegated to the existing queue operator; no second supervisor,
progress store or preview/recovery engine exists here.
"""
from __future__ import annotations

import json

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from . import retarget_action as actions
from . import retarget_generic_target as generic
from . import retarget_job as jobs
from . import retarget_job_ui as job_ui
from . import retarget_ui as body_ui


OPTIONS = "c94_retarget_general_job_options_v1"
_CLASSES = []
_OWNED = False


def require(value, message):
    if not value:
        raise ValueError(message)


def _opts(context):
    return getattr(context.scene, OPTIONS)


def _profile_text_changed(self, context):
    if (self.profile_text or "").strip():
        self.profile_source = "TEXT"
        self.message = "Profile来源：本面板显式Text名称 " + self.profile_text


def _profile_path_changed(self, context):
    if (self.profile_path or "").strip():
        self.profile_source = "PATH"
        self.message = "Profile来源：本面板显式JSON路径（不再静默使用旧Text）"


def _body(context):
    return getattr(context.scene, body_ui.OPTIONS)


def _job_options(context):
    return getattr(context.scene, job_ui.OPTIONS)


def _profile_api():
    try:
        from . import retarget_profile as profile
    except ImportError as exc:
        raise ValueError(
            "当前包缺少最终revision3 retarget_profile；"
            "Profile→C94队列拒绝，不能退回其它求解"
        ) from exc
    return profile


def _generic_ui_options(context):
    try:
        from . import retarget_generic_target_ui as generic_ui
    except ImportError:
        return None
    if not hasattr(bpy.types.Scene, generic_ui.OPTIONS):
        return None
    return getattr(context.scene, generic_ui.OPTIONS)


def _profile_ui_options(context):
    try:
        from . import retarget_profile_ui as profile_ui
    except ImportError:
        return None
    return getattr(context.scene, profile_ui.OPTIONS, None)


def _source(context):
    return _body(context).source


def _target(context):
    return _body(context).target


def _jobs_root(context):
    value = _job_options(context).jobs_root.strip()
    require(value, "请先在后台动作队列中明确选择绝对工作目录")
    from pathlib import Path

    root = Path(bpy.path.abspath(value))
    require(root.is_absolute(), "后台工作目录必须为绝对路径")
    return str(root)


def _settings(context):
    job_options = _job_options(context)
    from . import retarget_contact_detection_ui as detection_ui
    return {
        "motion": _body(context).motion,
        "max_seconds": job_options.max_seconds,
        "threads": job_options.threads,
        "contact_detection_calibration": detection_ui.calibration_from_context(context),
    }


def _queue_items(context, source):
    """Reuse the existing queue; only reject a queue from another source."""
    job_options = _job_options(context)
    if not job_options.items:
        return None
    require(job_options.source == source,
            "队列来自另一源骨架；请先清空队列或选回原来源")
    rows = []
    for item in job_options.items:
        require(item.action is not None, "队列中存在未选择Action的条目")
        rows.append(job_ui._item_request(item))
    return rows


def _policy(context):
    target = _target(context)
    require(target is not None, "请选择普通FK目标骨架")
    text = (_opts(context).target_policy_json or "").strip()
    require(text, "请先建立或粘贴明确target_policy")
    try:
        policy = json.loads(text)
    except ValueError as exc:
        raise ValueError("target_policy必须为纯JSON对象") from exc
    generic.validate(target, policy)
    require(policy.get("acknowledged") is True,
            "target_policy未明确确认骨骼Action回退")
    return policy


def _read_profile(context):
    profile_api = _profile_api()
    opts = _opts(context)
    source = opts.profile_source
    if source == "PANEL":
        profile_ui = _profile_ui_options(context)
        require(profile_ui is not None, "Profile面板界面未注册")
        require(profile_ui.profile_text_name, "Profile面板尚未加载Profile Text")
        text = bpy.data.texts.get(profile_ui.profile_text_name)
        require(text is not None, "Profile面板的Text已不存在；请重新加载")
        return profile_api.profile_from_json(text.as_string())
    if source == "TEXT":
        require(opts.profile_text.strip(), "来源选择为显式Text，但Text名称为空")
        text = bpy.data.texts.get(opts.profile_text)
        require(text is not None, "profile Text已不存在；请重新加载")
        return profile_api.profile_from_json(text.as_string())
    if source == "PATH":
        require(opts.profile_path.strip(), "来源选择为显式路径，但JSON路径为空")
        return profile_api.load_profile(opts.profile_path)
    raise ValueError("未知Profile来源：" + str(source))


def _register_job(context, target, source, job, preset):
    job_ui.register_session(context.scene, target, source, job)
    job_options = _job_options(context)
    job_options.last_directory = str(job.directory)
    job_options.source = source
    # The queue owns source Action selections, independently of which solver
    # consumes them. PROFILE is an execution mode, not an Action source kind.
    if not job_options.items:
        job_options.preset = _body(context).source_kind


class C94GENERALJOB_Options(bpy.types.PropertyGroup):
    mode: EnumProperty(
        name="后台目标范围",
        items=(
            ("GENERIC_FK", "普通FK目标", "Mixamo/CMU + 明确target_policy"),
            ("PROFILE_C94", "Profile → C94", "纯JSON source_profile + 现有C94"),
        ),
        default="GENERIC_FK",
    )
    profile_source: EnumProperty(
        name="Profile来源",
        description="明确本面板提交使用哪个profile来源；双来源不会静默择一",
        items=(
            ("PANEL", "Profile面板当前Text", "只读Profile面板当前Text；忽略本面板Text/路径"),
            ("TEXT", "本面板显式Text", "使用profile_text指定的Text"),
            ("PATH", "本面板显式JSON路径", "使用profile_path指定的JSON文件"),
        ),
        default="PANEL",
    )
    target_policy_json: StringProperty(
        name="target_policy JSON",
        description="显式、已确认的普通FK目标策略；不从C94失败静默回退",
        default="",
    )
    profile_text: StringProperty(name="Profile Text", default="", update=_profile_text_changed)
    profile_path: StringProperty(name="Profile JSON", subtype="FILE_PATH", default="",
                                 update=_profile_path_changed)
    acknowledged: BoolProperty(
        name="已核对目标策略/Profile范围",
        description="明确确认这是骨骼Action或profile范围，不是控制器动画",
        default=False,
    )
    message: StringProperty(default="通用队列只复用现有后台生命周期")


class C94GENERALJOB_OT_prepare(bpy.types.Operator):
    bl_idname = "c94_workflow.retarget_general_job_prepare"
    bl_label = "准备通用后台载荷"
    operation: EnumProperty(items=[
        (value, value, "") for value in (
            "POLICY_FROM_GENERIC", "VALIDATE_POLICY",
            "PROFILE_FROM_UI", "VALIDATE_PROFILE",
        )
    ])

    def execute(self, context):
        opts = _opts(context)
        try:
            if self.operation == "POLICY_FROM_GENERIC":
                generic_ui = _generic_ui_options(context)
                require(generic_ui is not None, "通用目标界面未注册")
                target = _target(context)
                require(target is not None, "请选择普通FK目标骨架")
                require(generic_ui.acknowledged and
                        generic_ui.ack_target == target,
                        "请先在通用目标面板对当前目标明确确认骨骼Action回退")
                mapping = json.loads(generic_ui.target_mapping)
                require(isinstance(mapping, dict) and mapping,
                        "通用目标映射必须为非空JSON对象")
                policy = generic.policy(target, mapping, acknowledged=True)
                opts.target_policy_json = json.dumps(
                    policy, ensure_ascii=False, sort_keys=True)
                opts.acknowledged = True
                opts.message = "已建立显式target_policy签名：" + policy["signature"][:12]
            elif self.operation == "VALIDATE_POLICY":
                policy = _policy(context)
                found = generic.conflicts(_target(context))
                require(not found, "普通FK目标含未适配控制图")
                opts.message = "target_policy有效；签名 " + policy["signature"][:12]
            elif self.operation == "PROFILE_FROM_UI":
                profile_ui = _profile_ui_options(context)
                require(profile_ui is not None, "Profile界面未注册")
                require(profile_ui.profile_text_name, "Profile面板尚未加载Profile")
                _body(context).source = profile_ui.source
                _body(context).target = profile_ui.target
                opts.mode = "PROFILE_C94"
                opts.profile_text = profile_ui.profile_text_name
                opts.profile_path = profile_ui.profile_path
                opts.profile_source = "TEXT" if opts.profile_text.strip() else (
                    "PATH" if opts.profile_path.strip() else "PANEL")
                opts.acknowledged = True
                opts.message = ("已从Profile面板填入；当前来源=" + opts.profile_source +
                                "：" + (opts.profile_text or opts.profile_path))
            else:
                profile = _read_profile(context)
                validation = _profile_api().validate_profile(
                    profile, _source(context), _target(context))
                require(validation.get("applies"),
                        "profile不可应用：" + "; ".join(validation.get("errors") or []))
                opts.message = "profile可应用；tier=" + validation.get("tier", "")
            return {"FINISHED"}
        except Exception as exc:
            opts.message = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94GENERALJOB_OT_enqueue(bpy.types.Operator):
    bl_idname = "c94_workflow.retarget_general_job_enqueue"
    bl_label = "提交通用后台队列"
    operation: EnumProperty(items=[
        ("ENQUEUE_GENERIC", "ENQUEUE_GENERIC", ""),
        ("ENQUEUE_PROFILE", "ENQUEUE_PROFILE", ""),
    ])

    def execute(self, context):
        opts = _opts(context)
        body = _body(context)
        try:
            require(job_ui.session_for(context.scene) is None,
                    "请先收集结果并释放当前任务；磁盘输出始终保留")
            source, target = _source(context), _target(context)
            require(source is not None and target is not None, "请选择源和目标")
            items = _queue_items(context, source)
            settings = _settings(context)
            root = _jobs_root(context)
            if self.operation == "ENQUEUE_GENERIC":
                require(body.source_kind in ("MIXAMO", "CMU"),
                        "普通FK目标只接已验Mixamo/CMU来源；Profile组合未验")
                policy = _policy(context)
                mapping = (json.loads(body.mapping_json)
                           if body.source_kind == "MIXAMO"
                           and body.mapping_json.strip() not in ("", "{}") else None)
                job = jobs.enqueue(
                    source, target, items=items, preset=body.source_kind,
                    mapping=mapping, settings=settings, jobs_root=root,
                    target_policy=policy,
                )
            else:
                profile = _read_profile(context)
                job = jobs.enqueue(
                    source, target, items=items, preset="PROFILE",
                    mapping=None, settings=settings, jobs_root=root,
                    source_profile=profile,
                )
            _register_job(
                context, target, source, job,
                "PROFILE" if self.operation == "ENQUEUE_PROFILE" else body.source_kind,
            )
            opts.message = "已提交；状态/取消/收集复用下方后台动作队列"
            return {"FINISHED"}
        except Exception as exc:
            if isinstance(exc,jobs.JobError) and exc.code == "ENQUEUE_FAILED" and isinstance(exc.details,dict):
                _job_options(context).last_directory = exc.details.get("job_directory", "")
            opts.message = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class C94GENERALJOB_PT_panel(bpy.types.Panel):
    bl_idname = "C94GENERALJOB_PT_panel"
    bl_label = "通用目标 / Profile 后台队列"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "动画重定向"
    bl_parent_id = "C94RETARGET_PT_body"

    def draw(self, context):
        layout = self.layout
        opts = _opts(context)
        session = job_ui.session_for(context.scene)
        if session is not None and session.get("entry") is not None:
            layout.label(text="当前任务请在对应专项面板管理", icon="INFO")
            return
        layout.prop(opts, "mode")
        if opts.mode == "GENERIC_FK":
            layout.label(text="源：Mixamo/CMU；目标：已核对普通FK policy")
            layout.prop(opts, "target_policy_json")
            row = layout.row(align=True)
            row.operator(
                "c94_workflow.retarget_general_job_prepare",
                text="从通用目标面板建立policy",
            ).operation = "POLICY_FROM_GENERIC"
            row.operator(
                "c94_workflow.retarget_general_job_prepare",
                text="校验policy",
            ).operation = "VALIDATE_POLICY"
        else:
            layout.label(text="源：Profile JSON；目标：现有C94")
            layout.prop(opts, "profile_source")
            layout.prop(opts, "profile_text")
            layout.prop(opts, "profile_path")
            layout.label(text="来源=" + opts.profile_source +
                         "；双来源且未明确选择时会拒绝，不静默取旧Text", icon="INFO")
            row = layout.row(align=True)
            row.operator(
                "c94_workflow.retarget_general_job_prepare",
                text="从Profile面板填入",
            ).operation = "PROFILE_FROM_UI"
            row.operator(
                "c94_workflow.retarget_general_job_prepare",
                text="校验Profile",
            ).operation = "VALIDATE_PROFILE"
        job_options = _job_options(context)
        body = _body(context)
        layout.prop(body, "source", text="队列来源骨架")
        layout.prop(body, "target", text="队列目标骨架")
        layout.prop(body, "motion")
        layout.prop(job_options, "jobs_root")
        budget = layout.row(align=True)
        budget.prop(job_options, "max_seconds")
        budget.prop(job_options, "threads")
        job_ui.draw_queue_editor(layout, context)
        layout.label(text="队列条目：%d（同来源；空=当前Action）" %
                     len(job_options.items))
        row = layout.row()
        row.enabled = session is None
        operation = "ENQUEUE_GENERIC" if opts.mode == "GENERIC_FK" else "ENQUEUE_PROFILE"
        row.operator("c94_workflow.retarget_general_job_enqueue",
                     text="提交普通FK队列" if opts.mode == "GENERIC_FK" else "提交Profile→C94"
                     ).operation = operation
        if session is not None:
            job_ui.draw_job_session(layout, context, session)
        if job_options.last_directory:
            layout.prop(job_options, "last_directory", text="保留的输出目录")
        layout.label(text=opts.message[:72], icon="INFO")
        layout.label(text="结果保留为独立Action，收集后可预览")


_CLASSES = (
    C94GENERALJOB_Options,
    C94GENERALJOB_OT_prepare,
    C94GENERALJOB_OT_enqueue,
    C94GENERALJOB_PT_panel,
)


def register():
    global _OWNED
    require(hasattr(bpy.types.Scene, body_ui.OPTIONS),
            "请先注册身体重定向界面")
    require(hasattr(bpy.types.Scene, job_ui.OPTIONS),
            "请先注册后台动作队列界面")
    require(not hasattr(bpy.types.Scene, OPTIONS),
            "通用后台队列界面已注册，原实例保持")
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
        setattr(bpy.types.Scene, OPTIONS,
                bpy.props.PointerProperty(type=C94GENERALJOB_Options))
        _OWNED = True
    except Exception:
        unregister()
        raise


def unregister():
    global _OWNED
    if _OWNED and hasattr(bpy.types.Scene, OPTIONS):
        delattr(bpy.types.Scene, OPTIONS)
    _OWNED = False
    for cls in reversed(_CLASSES):
        if getattr(cls, "is_registered", False):
            bpy.utils.unregister_class(cls)
