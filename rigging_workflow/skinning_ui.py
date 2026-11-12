# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit single-closed-proxy volume UI; geometry and worker ownership stay in core.

Only skinning_job.start/Job.status/cancel/apply are used. No external job path is
accepted for replay, no source weight is edited here, and panel draw never polls
workers or scans meshes/bones. One owned UI session is retained until explicit
finish, file load, or unregister; terminal results keep setup locked as well.
"""
import json
import math
import os
import tempfile

import bpy
from bpy.app.handlers import persistent
from bpy.props import (CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)


OPTIONS = 'c94_skinning_options_v1'
_REGISTERED = []
_SESSION = None
_INTERVAL = .25
_STATUSES = {'running', 'succeeded', 'failed', 'cancelled', 'applied'}
_LABELS = {'running': '求解中', 'succeeded': '求解成功，尚未创建候选',
           'failed': '求解失败，未创建部分权重', 'cancelled': '任务已取消',
           'applied': '独立候选已创建'}


def _mesh(_self, obj):
    return obj.type == 'MESH'


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _name(obj):
    try:
        return obj.name if obj else '（引用已失效）'
    except ReferenceError:
        return '（引用已失效）'


def _options(scene):
    return getattr(scene, OPTIONS)


def _setup_options(context):
    if _SESSION is not None:
        raise ValueError('本次任务及结果仍被保留；请先结束会话再更换配置')
    return _options(context.scene)


def _session(context):
    if _SESSION is None:
        raise ValueError('没有本工具启动的体积蒙皮任务')
    if _SESSION['job'].session_scene != context.scene:
        raise ValueError('请切回任务所属场景：' + _name(_SESSION['job'].session_scene))
    return _SESSION


def _final_outputs(rig):
    from .generation import RIG_KEY
    if not rig or rig.type != 'ARMATURE':
        raise ValueError('请选择本流程生成的骨架')
    if rig.mode == 'EDIT':
        raise ValueError('请先结束骨架编辑模式')
    try:
        data = json.loads(rig.get(RIG_KEY, ''))
    except (ValueError, TypeError) as exc:
        raise ValueError('骨架缺少有效的最终输出骨声明') from exc
    names = data.get('deforms') if isinstance(data, dict) else None
    if (not isinstance(names, list) or not names or
            any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names)):
        raise ValueError('最终输出骨声明为空或无效')
    return {name for name in names if rig.data.bones.get(name) is not None
            and rig.data.bones[name].use_deform}


def _append_bones(options, names):
    existing = {row.bone_name for row in options.bone_names}
    added = 0
    for name in names:
        if name not in existing:
            options.bone_names.add().bone_name = name
            existing.add(name)
            added += 1
    return added


def _settings(options):
    return {'cells_longest': int(options.cells_longest),
            'neighbor_count': int(options.neighbor_count),
            'max_cells': int(options.max_cells),
            'max_memory_mb': float(options.max_memory_mb),
            'max_seconds': float(options.max_seconds)}


def _error_text(value):
    if isinstance(value, dict):
        return str(value.get('message') or value.get('code') or value)
    return str(value)


def _tag_redraw():
    manager = getattr(bpy.context, 'window_manager', None)
    for window in tuple(manager.windows) if manager else ():
        try:
            if window.screen is None:
                continue
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        except (ReferenceError, AttributeError):
            continue


def _summary(session, status):
    text = _LABELS[status['status']]
    if session.get('cancel_requested') and status['status'] not in {'cancelled', 'applied'}:
        text = '已请求取消，禁止创建候选；后台状态：' + text
    phase = status.get('phase')
    if phase:
        text += '；阶段：' + str(phase)
    done, total = status.get('done'), status.get('total')
    if (isinstance(done, (int, float)) and not isinstance(done, bool)
            and isinstance(total, (int, float)) and not isinstance(total, bool)
            and math.isfinite(done) and math.isfinite(total) and total > 0 and done >= 0):
        text += f'；{done:g}/{total:g}'
    if status.get('error'):
        text += '；错误：' + _error_text(status['error'])
    return text


def _poll_session(session):
    """Poll only an owned Job; monitor errors retain the session and its refs."""
    try:
        status = session['job'].status()
        if not isinstance(status, dict) or status.get('status') not in _STATUSES:
            raise ValueError('后台返回了未知任务状态')
        session['status'] = dict(status)
        session['poll_error'] = None
        options = _options(session['job'].session_scene)
        options.summary = _summary(session, status)
        options.job_directory = str(status.get('job_directory') or '')
    except Exception as exc:
        session['poll_error'] = str(exc)
        try:
            _options(session['job'].session_scene).summary = '状态读取失败（任务仍保留）：' + str(exc)
        except (ReferenceError, AttributeError):
            pass
        raise
    finally:
        _tag_redraw()
    return status


def _timer():
    session = _SESSION
    if session is None:
        return None
    try:
        status = _poll_session(session)
    except Exception:
        return _INTERVAL
    return _INTERVAL if status['status'] == 'running' else None


def _ensure_timer():
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=_INTERVAL, persistent=False)


def _remove_timer():
    try:
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
    except Exception as exc:
        print('[C94体积蒙皮] 自有状态timer清理异常：' + str(exc))


def _release_session(reason):
    """Request cooperative cancellation, release only UI refs; never delete files."""
    global _SESSION
    session = _SESSION
    _remove_timer()
    if session is None:
        return None
    error = None
    try:
        if session.get('candidate') is None:
            session['job'].cancel()
    except Exception as exc:
        error = str(exc)
        print('[C94体积蒙皮] 请求取消时发生错误：' + error)
    try:
        options = _options(session['job'].session_scene)
        options.source = None
        options.rig = None
        options.proxy = None
        options.bone_names.clear()
        options.bone_search = ''
        options.bone_index = 0
        options.job_directory = ''
        options.summary = reason + ('；取消请求异常：' + error if error else '')
    except Exception as exc:
        print('[C94体积蒙皮] UI引用清理异常：' + str(exc))
    _SESSION = None
    _tag_redraw()
    return error


@persistent
def _load_pre(_unused):
    _release_session('文件切换：结束本次UI会话；文件和候选保留')


class C94SKIN_BoneName(bpy.types.PropertyGroup):
    bone_name: StringProperty(name='最终形变输出骨')


class C94SKIN_Options(bpy.types.PropertyGroup):
    source: PointerProperty(name='新源网格', type=bpy.types.Object, poll=_mesh)
    rig: PointerProperty(name='中立骨架', type=bpy.types.Object, poll=_armature)
    proxy: PointerProperty(name='单闭合代理', type=bpy.types.Object, poll=_mesh)
    candidate_name: StringProperty(name='独立候选名称', default='体积蒙皮_独立候选')
    bone_search: StringProperty(name='选择输出骨')
    bone_names: CollectionProperty(type=C94SKIN_BoneName)
    bone_index: IntProperty(default=0, min=0)
    cells_longest: IntProperty(name='最长轴格数', default=32, min=4, max=512)
    neighbor_count: EnumProperty(name='邻接', items=(('6', '6邻接', '六邻接体积扩散'),
                                                   ('26', '26邻接', '二十六邻接体积扩散')), default='6')
    max_cells: IntProperty(name='最大格数', default=250000, min=8, max=2000000)
    max_memory_mb: FloatProperty(name='内存预算（MB）', default=512, min=1, max=32768)
    max_seconds: FloatProperty(name='时间预算（秒）', default=60, min=.01, max=3600)
    jobs_directory: StringProperty(name='任务目录', subtype='DIR_PATH',
        default=os.path.join(bpy.app.tempdir or tempfile.gettempdir(), 'C94VolumeJobs'))
    summary: StringProperty(options={'HIDDEN'})
    job_directory: StringProperty(options={'HIDDEN'})


class C94SKIN_UL_Bones(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.bone_name, icon='BONE_DATA')


class C94SKIN_OT_Bones(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_bones'
    bl_label = '编辑明确蒙皮骨列表'
    action: EnumProperty(items=(('ADD_NAME', '添加所选名称', ''), ('ADD_SELECTED', '添加选中的输出骨', ''),
                                ('REMOVE', '删除列表项', ''), ('CLEAR', '清空', '')))

    def execute(self, context):
        try:
            options = _setup_options(context)
            if self.action == 'CLEAR':
                options.bone_names.clear()
                options.bone_index = 0
                return {'FINISHED'}
            if self.action == 'REMOVE':
                index = options.bone_index
                if not 0 <= index < len(options.bone_names):
                    raise ValueError('先选择需要移除的列表项')
                options.bone_names.remove(index)
                options.bone_index = max(0, min(index, len(options.bone_names)-1))
                return {'FINISHED'}
            allowed = _final_outputs(options.rig)
            if self.action == 'ADD_NAME':
                if options.bone_search not in allowed:
                    raise ValueError('只能添加该骨架声明的实际最终形变输出骨')
                names = [options.bone_search]
            else:
                names = [bone.name for bone in options.rig.data.bones
                         if bone.select and bone.name in allowed]
                if not names:
                    raise ValueError('先在骨架中明确选中需要参与的最终输出骨；不会自动添加全部deform骨')
            added = _append_bones(options, names)
        except Exception as exc:
            self.report({'ERROR'}, '骨列表未更新：' + str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'加入{added}个明确输出骨；控制骨和重复项未加入')
        return {'FINISHED'}


class C94SKIN_OT_Start(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_start'
    bl_label = '启动后台体积求解'
    bl_description = '仅启动一个单闭合代理任务；不创建或修改任何现有权重组'

    def execute(self, context):
        global _SESSION
        try:
            from . import skinning_job
            options = _setup_options(context)
            names = [row.bone_name for row in options.bone_names]
            allowed = _final_outputs(options.rig)
            if not names or len(names) != len(set(names)) or any(n not in allowed for n in names):
                raise ValueError('必须明确列出有效且无重复的最终形变输出骨')
            if not options.candidate_name.strip():
                raise ValueError('请填写明确的独立候选名称')
            if not options.jobs_directory.strip():
                raise ValueError('请选择明确的任务目录')
            directory = os.path.abspath(bpy.path.abspath(options.jobs_directory))
            job = skinning_job.start(options.source, options.rig, options.proxy, names, _settings(options),
                                     jobs_directory=directory)
        except Exception as exc:
            self.report({'ERROR'}, '无法启动体积求解：' + str(exc))
            return {'CANCELLED'}
        # Ownership is recorded before optional timer/UI work; errors cannot lose
        # a worker that start() has already returned to this UI.
        _SESSION = {'job': job, 'candidate_name': options.candidate_name.strip(),
                    'status': {'status': 'running'}, 'cancel_requested': False,
                    'poll_error': None, 'candidate': None}
        options.summary = '已启动后台体积求解；原网格和已有权重保持'
        options.job_directory = ''
        try:
            _ensure_timer()
        except Exception as exc:
            self.report({'WARNING'}, '任务已启动；请用刷新状态按钮查看：' + str(exc))
        _tag_redraw()
        return {'FINISHED'}


class C94SKIN_OT_Poll(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_poll'
    bl_label = '刷新任务状态'

    def execute(self, context):
        try:
            _poll_session(_session(context))
        except Exception as exc:
            self.report({'ERROR'}, '无法读取任务状态（任务仍保留）：' + str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class C94SKIN_OT_Cancel(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_cancel'
    bl_label = '请求取消求解'

    def execute(self, context):
        try:
            session = _session(context)
            if session.get('candidate') is not None:
                raise ValueError('候选已经创建；结束会话会保留候选')
            session['job'].cancel()
            session['cancel_requested'] = True
            _options(session['job'].session_scene).summary = '已请求协作取消；结果禁止创建候选，后台可能仍在结束'
        except Exception as exc:
            self.report({'ERROR'}, '取消请求未完整完成（任务仍保留）：' + str(exc))
            return {'CANCELLED'}
        try:
            _ensure_timer()
        except Exception as exc:
            self.report({'WARNING'}, '已请求取消；请用刷新状态按钮查看：' + str(exc))
        _tag_redraw()
        return {'FINISHED'}


class C94SKIN_OT_Apply(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_apply'
    bl_label = '创建独立蒙皮候选'
    bl_description = '仅从本会话完整成功且未取消的结果创建候选；源或骨架过时将拒绝'

    def execute(self, context):
        try:
            session = _session(context)
            if session['cancel_requested'] or session.get('candidate') is not None:
                raise ValueError('本次结果已取消或已经创建过候选')
            status = _poll_session(session)
            if status['status'] != 'succeeded':
                raise ValueError('需要完整成功的求解结果；运行中、失败或已取消结果不可应用')
            candidate = session['job'].apply(name=session['candidate_name'])
        except Exception as exc:
            self.report({'ERROR'}, '候选创建未完成（任务保留供检查）：' + str(exc))
            return {'CANCELLED'}
        session['candidate'] = candidate
        session['status'] = {'status': 'applied', 'job_directory': status.get('job_directory', '')}
        _options(session['job'].session_scene).summary = '独立候选已创建：' + _name(candidate) + '；尚未验收变形或艺术质量'
        _remove_timer()
        _tag_redraw()
        self.report({'INFO'}, '独立候选已创建，原源网格保持')
        return {'FINISHED'}


class C94SKIN_OT_Finish(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_finish'
    bl_label = '结束本次会话'
    bl_description = '请求取消并释放本UI引用；不删除任务文件或已创建候选'

    def execute(self, context):
        try:
            _session(context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        error = _release_session('会话已结束；任务文件和候选保留')
        if error:
            self.report({'WARNING'}, 'UI会话已释放，取消请求发生错误：' + error)
        return {'FINISHED'}


class C94SKIN_PT_SingleProxy(bpy.types.Panel):
    bl_idname = 'C94SKIN_PT_single_proxy'
    bl_label = '体积蒙皮（单代理）'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        options = _options(context.scene)
        layout = self.layout
        setup = layout.column()
        setup.enabled = _SESSION is None
        setup.prop(options, 'source')
        setup.prop(options, 'rig')
        setup.prop(options, 'proxy')
        setup.prop(options, 'candidate_name')
        if options.rig:
            row = setup.row(align=True)
            row.prop_search(options, 'bone_search', options.rig.data, 'bones')
            row.operator('c94_workflow.skinning_bones', text='加入').action = 'ADD_NAME'
        setup.operator('c94_workflow.skinning_bones', text='加入选中的最终输出骨').action = 'ADD_SELECTED'
        setup.template_list('C94SKIN_UL_Bones', '', options, 'bone_names', options, 'bone_index', rows=4)
        row = setup.row(align=True)
        row.operator('c94_workflow.skinning_bones', text='删除列表项').action = 'REMOVE'
        row.operator('c94_workflow.skinning_bones', text='清空列表').action = 'CLEAR'
        setup.label(text='仅求解明确列出的输出骨；不会默认加入全部deform骨')
        setup.prop(options, 'cells_longest')
        setup.prop(options, 'neighbor_count')
        setup.prop(options, 'max_cells')
        setup.prop(options, 'max_memory_mb')
        setup.prop(options, 'max_seconds')
        setup.prop(options, 'jobs_directory')
        setup.operator('c94_workflow.skinning_start', icon='PLAY')
        if _SESSION is not None:
            job = _SESSION['job']
            box = layout.box()
            box.label(text='任务真实引用：' + _name(job.source))
            box.label(text='骨架：' + _name(job.rig) + '；代理：' + _name(job.proxy))
            box.label(text='所属场景：' + _name(job.session_scene))
            controls = box.column()
            controls.enabled = job.session_scene == context.scene
            controls.operator('c94_workflow.skinning_poll', icon='FILE_REFRESH')
            cancel = controls.row()
            cancel.enabled = _SESSION.get('candidate') is None
            cancel.operator('c94_workflow.skinning_cancel', icon='CANCEL')
            apply = controls.row()
            apply.enabled = (_SESSION['status'].get('status') == 'succeeded'
                             and not _SESSION['cancel_requested'] and not _SESSION.get('poll_error')
                             and _SESSION.get('candidate') is None)
            apply.operator('c94_workflow.skinning_apply', icon='DUPLICATE')
            controls.operator('c94_workflow.skinning_finish', icon='X')
        if options.summary:
            layout.label(text=options.summary)
        if options.job_directory:
            layout.label(text='本次目录：' + options.job_directory)
        layout.label(text='仅单闭合代理＋当前中立骨架＋无已有绑定的新源')
        layout.label(text='失败不创建部分组；不支持此入口重绑或自动普通权重')
        layout.label(text='完整角色、多域与艺术质量尚需对应制作和验收')


_CLASSES = (C94SKIN_BoneName, C94SKIN_Options, C94SKIN_UL_Bones,
            C94SKIN_OT_Bones, C94SKIN_OT_Start, C94SKIN_OT_Poll,
            C94SKIN_OT_Cancel, C94SKIN_OT_Apply, C94SKIN_OT_Finish, C94SKIN_PT_SingleProxy)


def register():
    global _C94_OPTIONS_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('该面板属性已由其他实例持有；原属性保持')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94SKIN_Options))
        _C94_OPTIONS_OWNED = True
        if _load_pre not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(_load_pre)
    except Exception:
        unregister()
        raise


def unregister():
    _release_session('工具已卸载；任务文件和候选保留')
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)
    if hasattr(bpy.types.Scene, OPTIONS):
        _release_workspace_options()
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()


_C94_OPTIONS_OWNED = False

def _release_workspace_options():
    global _C94_OPTIONS_OWNED
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if (_C94_OPTIONS_OWNED and prop and prop.type == 'POINTER'
            and prop.fixed_type == C94SKIN_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
