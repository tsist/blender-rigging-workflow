# SPDX-License-Identifier: GPL-3.0-or-later
"""Local volume/paint UI; live core sessions own all solving and submission.

Draw reads cached summaries only. No disk path can recreate write authority.
One volume, native paint or prepared-paint plan occupies this UI at a time.
"""
import json
import os
import re
import tempfile

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import skinning_plan_ui as selection_ui, skinning_policy_ui as scope_ui
from .skinning import topology_fingerprint

OPTIONS = 'c94_local_skinning_options_v1'
_REGISTERED = []
_SESSION = None
_CHECK = None
_INTERVAL = .25
_STATUSES = {'running', 'succeeded', 'failed', 'cancelled', 'applied'}
_LABELS = {'running': '局部体积求解中', 'succeeded': '求解成功，尚未创建候选',
           'failed': '求解失败，原权重保持', 'cancelled': '任务已取消', 'applied': '独立候选已创建'}


def _mesh(_self, obj):
    return obj.type == 'MESH'


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _options(scene):
    return getattr(scene, OPTIONS)


def _changed(_self, context):
    global _CHECK
    _CHECK = None
    try:
        options = _options(context.scene)
        options.check_json = ''
        options.summary = ('参数已改变，活动会话保留；先结束后重新检查' if _SESSION is not None
                           else '参数已改变，请检查明确范围与锁组')
    except (ReferenceError, AttributeError):
        pass


def _configuration(options):
    return (options.workflow, options.target, options.rig, options.proxy, options.editable_bones,
            options.active_bone, options.candidate_name, options.alpha, options.use_transition_group,
            options.transition_group, options.captured_target, options.captured_mesh,
            options.captured_topology, options.captured_indices, options.captured_count,
            options.cells_longest, options.neighbor_count, options.max_cells,
            options.max_memory_mb, options.max_seconds, options.jobs_directory)


def _setup(context):
    if _SESSION is not None:
        raise ValueError('已有局部体积、绘制或待提交计划；先结束本次会话')
    return _options(context.scene)


def _payload(options):
    target = options.target
    if target is None or target.type != 'MESH':
        raise ValueError('先指定已有绑定目标网格')
    names = [name.strip() for name in re.split(r'[;；\n]+', options.editable_bones) if name.strip()]
    if not names or len(set(names)) != len(names):
        raise ValueError('明确填写无重复的已有可写骨组，以分号或换行分隔')
    missing = [name for name in names if target.vertex_groups.get(name) is None]
    if missing:
        raise ValueError('目标缺少声明骨组，不自动创建：' + '、'.join(missing))
    if options.use_transition_group:
        ids, alpha = scope_ui._group_scope(target, options.transition_group)
        scope = '过渡组全部正值点；不叠加捕获选区'
    else:
        ids = scope_ui._captured_ids(options)
        alpha = [float(options.alpha)] * len(ids)
        scope = '捕获的RAW索引及统一过渡系数'
    locked = [name for name in names if target.vertex_groups[name].lock_weight]
    if len(locked) == len(names):
        raise ValueError('声明骨组全部锁定，没有可写组')
    if not any(value > 0 for value in alpha):
        raise ValueError('明确范围内没有正过渡系数')
    return {'names': names, 'ids': ids, 'alpha': alpha, 'locked': locked, 'scope': scope}


def _settings(options):
    return {'cells_longest': int(options.cells_longest), 'neighbor_count': int(options.neighbor_count),
            'max_cells': int(options.max_cells), 'max_memory_mb': float(options.max_memory_mb),
            'max_seconds': float(options.max_seconds), 'require_all_bones_seeded': True}


def _checked(context):
    if _CHECK is None or _CHECK['scene'] != context.scene:
        raise ValueError('请先检查明确范围、锁组和本次配置')
    options = _options(context.scene)
    payload = _payload(options)
    if _CHECK['configuration'] != _configuration(options) or payload != _CHECK['payload']:
        raise ValueError('配置、范围系数或锁组已改变，请重新检查')
    if context.mode != 'OBJECT':
        raise ValueError('启动需要对象模式；可先用捕获按钮完成单对象Edit选择')
    from . import skinning_policy
    if skinning_policy._state(options.target,options.rig)!=_CHECK['source_state']:
        raise ValueError('检查后的源权重、几何、骨架或帧已改变，请重新检查范围')
    _candidate_name(options)
    return options, payload


def _candidate_name(options):
    name = options.candidate_name.strip()
    if not name or name in bpy.data.objects:
        raise ValueError('请填写尚未使用的独立候选名称')
    return name


def _session(context, *, configuration=True):
    if _SESSION is None or _SESSION['scene'] != context.scene:
        raise ValueError('没有当前场景拥有的局部会话')
    if configuration and _SESSION['configuration'] != _configuration(_options(context.scene)):
        raise ValueError('活动会话的配置已改变；请先取消或结束，不应用旧结果')
    return _SESSION


def _redraw():
    manager = getattr(bpy.context, 'window_manager', None)
    for window in tuple(manager.windows) if manager else ():
        try:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        except (ReferenceError, AttributeError):
            continue


def _poll(session):
    if session['kind'] != 'VOLUME':
        raise ValueError('当前不是后台局部体积会话')
    try:
        state = session['job'].status()
        if not isinstance(state, dict) or state.get('status') not in _STATUSES:
            raise ValueError('后台返回未知状态')
        session['status'], session['poll_error'] = dict(state), None
        options = _options(session['scene'])
        text = _LABELS[state['status']]
        if session.get('cancel_requested'):
            text = '已请求取消，禁止提交；' + text
        if state.get('phase'):
            text += '；阶段：' + str(state['phase'])
        if state.get('error'):
            error = state['error']
            text += '；' + str(error.get('message', error) if isinstance(error, dict) else error)
        options.summary = text
        options.job_directory = str(state.get('job_directory') or '')
        return state
    except Exception as exc:
        session['poll_error'] = str(exc)
        try:
            _options(session['scene']).summary = '状态读取失败，会话保留：' + str(exc)
        except (ReferenceError, AttributeError):
            pass
        raise
    finally:
        _redraw()


def _timer():
    if _SESSION is None or _SESSION['kind'] != 'VOLUME':
        return None
    try:
        return _INTERVAL if _poll(_SESSION)['status'] == 'running' else None
    except Exception:
        return _INTERVAL


def _ensure_timer():
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=_INTERVAL, persistent=False)


def _remove_timer():
    try:
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
    except Exception as exc:
        print('[C94局部蒙皮] 状态timer清理异常：' + str(exc))


def _release(context, reason, *, force=False):
    global _SESSION, _CHECK
    session, _CHECK = _SESSION, None
    _remove_timer()
    if session is None:
        return None
    error = None
    try:
        if session['kind'] == 'VOLUME' and session.get('candidate') is None:
            session['job'].cancel()
        elif session['kind'] == 'PAINT':
            session['paint'].cancel(context)
        elif session['kind'] == 'PLAN' and session.get('candidate') is None:
            session['plan'].cancel()
    except Exception as exc:
        error = str(exc)
        print('[C94局部蒙皮] 会话清理异常：' + error)
        # An interactive cancellation may fail while restoring paint context.
        # Keep its live owner reachable so the user can retry. File loading and
        # unregistering must detach before Blender invalidates these RNA IDs.
        if not force:
            try:
                _options(session['scene']).summary = '结束未完成，会话保留以便重试：' + error
            except (ReferenceError, AttributeError):
                pass
            _redraw()
            return error
    try:
        options = _options(session['scene'])
        options.check_json = ''
        options.summary = reason + ('；清理异常，请检查：' + error if error else '')
    except (ReferenceError, AttributeError):
        pass
    _SESSION = None
    _redraw()
    return error


def _select(context, obj):
    for selected in tuple(context.selected_objects):
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _unique_preview(base):
    name, index = base + '__范围预览', 2
    while name in bpy.data.objects:
        name = base + '__范围预览_' + str(index)
        index += 1
    return name


@persistent
def _load_pre(_unused):
    _release(bpy.context, '文件切换：结束UI会话；任务、源和提案副本保留', force=True)


@persistent
def _save_pre(_unused):
    if _SESSION is not None and _SESSION['kind'] in {'PAINT','PLAN'} and _SESSION.get('candidate') is None:
        _release(bpy.context,'保存：结束未提交绘制会话；提案副本保留，不恢复提交权',force=True)


class C94LOCAL_Options(bpy.types.PropertyGroup):
    workflow: EnumProperty(name='局部操作', items=(('VOLUME', '局部体积重算', '明确闭合代理上的真正体积扩散'),
        ('PAINT', '原生权重绘制', '在独立提案副本绘制，结束后受保护提交')), default='VOLUME', update=_changed)
    target: PointerProperty(name='已有绑定目标', type=bpy.types.Object, poll=_mesh, update=_changed)
    rig: PointerProperty(name='校准中立骨架', type=bpy.types.Object, poll=_armature, update=_changed)
    proxy: PointerProperty(name='明确闭合代理', type=bpy.types.Object, poll=_mesh, update=_changed)
    editable_bones: StringProperty(name='已有可写骨组（分号/换行）', update=_changed)
    active_bone: StringProperty(name='绘制活动骨组（可选）', update=_changed)
    candidate_name: StringProperty(name='独立候选名称', default='局部权重_独立候选', update=_changed)
    alpha: FloatProperty(name='捕获选区过渡系数', default=1., min=0., max=1., update=_changed)
    use_transition_group: BoolProperty(name='改用过渡组全部正值点定义明确范围', default=False, update=_changed)
    transition_group: StringProperty(name='范围及逐点过渡组', update=_changed)
    captured_target: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    captured_mesh: PointerProperty(type=bpy.types.Mesh, options={'HIDDEN'})
    captured_topology: StringProperty(options={'HIDDEN'})
    captured_indices: StringProperty(default='[]', options={'HIDDEN'})
    captured_count: IntProperty(default=0, options={'HIDDEN'})
    cells_longest: IntProperty(name='最长轴格数', default=32, min=4, max=512, update=_changed)
    neighbor_count: EnumProperty(name='邻接', items=(('6', '6邻接', ''), ('26', '26邻接', '')), default='6', update=_changed)
    max_cells: IntProperty(name='最大格数', default=250000, min=8, max=2000000, update=_changed)
    max_memory_mb: FloatProperty(name='内存预算（MB）', default=512, min=1, max=32768, update=_changed)
    max_seconds: FloatProperty(name='求解预算（秒）', default=60, min=.01, max=3600, update=_changed)
    jobs_directory: StringProperty(name='绝对任务目录', subtype='DIR_PATH',
        default=os.path.join(bpy.app.tempdir or tempfile.gettempdir(), 'C94LocalVolumeJobs'), update=_changed)
    check_json: StringProperty(options={'HIDDEN'})
    summary: StringProperty(default='明确目标、可写骨组和范围，再检查', options={'HIDDEN'})
    job_directory: StringProperty(options={'HIDDEN'})


class C94LOCAL_OT_Capture(bpy.types.Operator):
    bl_idname = 'c94_workflow.local_skinning_capture'
    bl_label = '捕获目标可见RAW选点'
    bl_description = '记录原生Object/Edit可见选点的RAW索引；拓扑改变后须重新捕获'
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            options = _setup(context)
            ids = selection_ui._visible_selected_ids(context, options.target)
            _changed(None, context)
            options.captured_target, options.captured_mesh = options.target, options.target.data
            options.captured_topology = topology_fingerprint(options.target.data)
            options.captured_indices, options.captured_count = json.dumps(ids), len(ids)
            options.summary = f'已捕获{len(ids)}个RAW索引；检查时使用当前几何'
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94LOCAL_OT_Action(bpy.types.Operator):
    bl_idname = 'c94_workflow.local_skinning_action'
    bl_label = '局部重算与绘制操作'
    action: EnumProperty(items=[(key, label, '') for key, label in (
        ('CHECK', '检查范围与锁组'), ('START', '启动局部体积求解'), ('POLL', '刷新求解状态'),
        ('CANCEL', '请求取消求解'), ('APPLY', '创建局部体积候选'), ('FINISH', '结束本次会话'),
        ('BEGIN_PAINT', '进入独立提案绘制'), ('FINISH_PAINT', '结束绘制并检查提案'),
        ('CANCEL_PAINT', '取消绘制，保留副本'), ('PLAN_PREVIEW', '创建独立范围预览'),
        ('PLAN_APPLY', '创建绘制权重候选'))])

    def execute(self, context):
        global _SESSION, _CHECK
        try:
            options = _options(context.scene)
            if self.action == 'CHECK':
                options = _setup(context)
                _CHECK = None
                options.check_json = ''
                if context.mode != 'OBJECT':
                    raise ValueError('先完成单对象选点捕获，检查需要对象模式')
                _candidate_name(options)
                payload = _payload(options)
                from . import skinning_policy
                if options.workflow == 'VOLUME':
                    from . import local_skinning_job
                    report = local_skinning_job.check(options.target, options.rig, options.proxy,
                        payload['names'], payload['ids'], payload['alpha'], _settings(options))
                else:
                    scope = skinning_policy.local_scope(options.target, options.rig,
                        payload['names'], payload['ids'], payload['alpha'])
                    report = {'method': 'native_weight_paint_scope_check',
                        **scope['summary']}
                    active = options.active_bone.strip()
                    if active and active not in scope['editable']:
                        raise ValueError('绘制活动骨组必须属于本次未锁定的可写骨组')
                if not isinstance(report, dict):
                    raise ValueError('核心检查没有返回明确报告')
                report = dict(report)
                report['ui_scope'] = payload['scope']
                report['ui_requested_vertices'] = sum(value > 0 for value in payload['alpha'])
                report['ui_locked_bones'] = payload['locked']
                options.check_json = json.dumps(report, ensure_ascii=False, allow_nan=False)
                options.summary = (f'已检查实际可写{report["write_vertices"]}点；'
                    f'零可写质量保持{report.get("zero_editable_mass_vertices", 0)}点；尚未启动')
                _CHECK = {'scene': context.scene, 'configuration': _configuration(options),
                          'payload': payload, 'report': report,
                          'source_state': skinning_policy._state(options.target,options.rig)}
            elif self.action == 'START':
                _setup(context)
                options, payload = _checked(context)
                if options.workflow != 'VOLUME':
                    raise ValueError('先选择局部体积重算')
                from . import local_skinning_job
                directory = bpy.path.abspath(options.jobs_directory)
                if not options.jobs_directory.strip() or not os.path.isabs(directory):
                    raise ValueError('任务目录须解析为明确绝对路径')
                job = local_skinning_job.start(options.target, options.rig, options.proxy,
                    payload['names'], payload['ids'], payload['alpha'], _settings(options), jobs_directory=directory)
                _SESSION = {'kind': 'VOLUME', 'scene': context.scene, 'job': job,
                    'configuration': _configuration(options), 'candidate_name': options.candidate_name.strip(),
                    'status': {'status': 'running'}, 'cancel_requested': False, 'poll_error': None, 'candidate': None}
                options.summary = '局部体积后台任务已启动；原权重保持'
                try:
                    _ensure_timer()
                except Exception as exc:
                    self.report({'WARNING'}, '任务已启动，请手动刷新：' + str(exc))
            elif self.action == 'POLL':
                _poll(_session(context, configuration=False))
            elif self.action == 'CANCEL':
                session = _session(context, configuration=False)
                if session['kind'] != 'VOLUME' or session.get('candidate') is not None:
                    raise ValueError('没有可取消的本次后台任务')
                session['cancel_requested'] = True
                session['job'].cancel()
                options.summary = '已请求取消；结果禁止提交，后台可能仍在结束'
                try:
                    _ensure_timer()
                except Exception as exc:
                    self.report({'WARNING'}, '请手动刷新取消终态：' + str(exc))
            elif self.action == 'BEGIN_PAINT':
                _setup(context)
                options, payload = _checked(context)
                if options.workflow != 'PAINT':
                    raise ValueError('先选择原生权重绘制')
                from . import weight_paint
                paint = weight_paint.begin(context, options.target, options.rig, payload['names'],
                    payload['ids'], payload['alpha'], active_bone=options.active_bone.strip() or None)
                _SESSION = {'kind': 'PAINT', 'scene': context.scene, 'paint': paint,
                    'configuration': _configuration(options), 'candidate_name': options.candidate_name.strip(),
                    'candidate': None}
                options.summary = '已进入独立提案副本；结束绘制后检查并创建候选'
            elif self.action == 'FINISH_PAINT':
                session = _session(context)
                if session['kind'] != 'PAINT':
                    raise ValueError('当前没有本工具的原生绘制会话')
                plan = session['paint'].finish(context)
                session['kind'], session['plan'] = 'PLAN', plan
                options.summary = '绘制已结束并恢复工具设置；尚未提交原源候选'
            elif self.action == 'CANCEL_PAINT':
                session = _session(context, configuration=False)
                if session['kind'] != 'PAINT':
                    raise ValueError('当前没有可取消的绘制会话')
                error = _release(context, '绘制已取消；原源及未提交副本保留')
                if error:
                    self.report({'WARNING'}, error)
            elif self.action == 'PLAN_PREVIEW':
                session = _session(context)
                if context.mode != 'OBJECT' or session.get('candidate') is not None:
                    raise ValueError('请在对象模式且未提交候选时预览')
                if session['kind'] == 'VOLUME':
                    if session['cancel_requested'] or _poll(session)['status'] != 'succeeded':
                        raise ValueError('需要完整成功且未取消的体积结果')
                    owner = session['job']
                elif session['kind'] == 'PLAN':
                    owner = session['plan']
                else:
                    raise ValueError('先结束绘制并检查提案')
                obj = owner.preview(name=_unique_preview(session['candidate_name']))
                _select(context, obj)
                if context.area and context.area.type == 'VIEW_3D':
                    context.space_data.shading.type, context.space_data.shading.color_type = 'SOLID', 'VERTEX'
                options.summary = '独立范围预览已创建；原源保持，预览副本保留'
            elif self.action in {'APPLY', 'PLAN_APPLY'}:
                session = _session(context)
                if context.mode != 'OBJECT' or session.get('candidate') is not None:
                    raise ValueError('请在对象模式且尚未提交时创建候选')
                if session['kind'] == 'VOLUME' and self.action == 'APPLY':
                    if session['cancel_requested'] or _poll(session)['status'] != 'succeeded':
                        raise ValueError('需要完整成功且未取消的体积结果')
                    owner = session['job']
                elif session['kind'] == 'PLAN' and self.action == 'PLAN_APPLY':
                    owner = session['plan']
                else:
                    raise ValueError('操作与当前体积/绘制计划不匹配')
                candidate = owner.apply(name=session['candidate_name'])
                session['candidate'] = candidate
                if session['kind'] == 'VOLUME':
                    session['status'] = {'status': 'applied'}
                _remove_timer()
                _select(context, candidate)
                options.summary = '独立候选已创建：' + candidate.name + '；变形/美术效果待复核'
            elif self.action == 'FINISH':
                _session(context, configuration=False)
                error = _release(context, 'UI会话已结束；任务、源、提案和候选保留')
                if error:
                    self.report({'WARNING'}, error)
            _redraw()
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _button(layout, label, action, *, enabled=True):
    row = layout.row()
    row.enabled = enabled
    row.operator('c94_workflow.local_skinning_action', text=label).action = action


class C94LOCAL_PT_Local(bpy.types.Panel):
    bl_idname = 'C94LOCAL_PT_local_skinning'
    bl_label = '局部体积重算与刷权重'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        options = _options(context.scene)
        layout = self.layout
        setup = layout.column()
        setup.enabled = _SESSION is None
        setup.prop(options, 'workflow')
        setup.prop(options, 'target')
        setup.prop(options, 'rig')
        setup.prop(options, 'candidate_name')
        setup.prop(options, 'editable_bones')
        setup.prop(options, 'use_transition_group')
        if options.use_transition_group:
            if options.target:
                setup.prop_search(options, 'transition_group', options.target, 'vertex_groups')
            else:
                setup.prop(options, 'transition_group')
            setup.label(text='明确范围＝该组全部正值点，组值为逐点过渡')
        else:
            setup.operator('c94_workflow.local_skinning_capture')
            setup.label(text=f'已捕获RAW点：{options.captured_count}')
            setup.prop(options, 'alpha')
        if options.workflow == 'VOLUME':
            setup.prop(options, 'proxy')
            setup.prop(options, 'cells_longest')
            setup.prop(options, 'neighbor_count')
            setup.prop(options, 'max_cells')
            setup.prop(options, 'max_memory_mb')
            setup.prop(options, 'max_seconds')
            setup.prop(options, 'jobs_directory')
        else:
            if options.target:
                setup.prop_search(options, 'active_bone', options.target, 'vertex_groups')
            else:
                setup.prop(options, 'active_bone')
            setup.label(text='仅校准中立状态；绘制独立提案，不直接写源')
        _button(setup, '检查明确范围与锁组', 'CHECK')
        if _CHECK is not None and _CHECK['scene'] == context.scene:
            box = layout.box()
            payload = _CHECK['payload']
            report = _CHECK['report']
            box.label(text=f'实际可写范围：{report["write_vertices"]}点')
            box.label(text=f'零可写质量保持：{report.get("zero_editable_mass_vertices", 0)}点')
            if report.get('retained_key_drivers'):
                box.label(text='保留原Key驱动与外部控制引用', icon='INFO')
                box.label(text='改权重后须重采受影响的表情Key', icon='ERROR')
            box.label(text=f'声明骨组：{len(payload["names"])}；锁定保护：{len(payload["locked"])}')
            box.label(text=payload['scope'])
            for name in payload['locked']:
                box.label(text='锁定：' + name)
        ready = _CHECK is not None and _CHECK['scene'] == context.scene and _SESSION is None
        if _SESSION is None:
            _button(layout, '启动局部体积求解' if options.workflow == 'VOLUME' else '进入独立提案绘制',
                    'START' if options.workflow == 'VOLUME' else 'BEGIN_PAINT', enabled=ready)
        elif _SESSION['scene'] != context.scene:
            layout.label(text='另一个场景拥有本次会话，请切回原场景')
        else:
            session = _SESSION
            if session['kind'] == 'VOLUME':
                _button(layout, '刷新求解状态', 'POLL')
                _button(layout, '请求取消求解', 'CANCEL', enabled=session.get('candidate') is None)
                can_apply = (session['status'].get('status') == 'succeeded' and not session['cancel_requested']
                             and not session.get('poll_error') and session.get('candidate') is None)
                _button(layout, '创建独立范围预览', 'PLAN_PREVIEW', enabled=can_apply)
                _button(layout, '创建局部体积候选', 'APPLY', enabled=can_apply)
            elif session['kind'] == 'PAINT':
                _button(layout, '结束绘制并检查提案', 'FINISH_PAINT')
                _button(layout, '取消绘制，保留副本', 'CANCEL_PAINT')
            elif session['kind'] == 'PLAN':
                _button(layout, '创建独立范围预览', 'PLAN_PREVIEW', enabled=session.get('candidate') is None)
                _button(layout, '创建绘制权重候选', 'PLAN_APPLY', enabled=session.get('candidate') is None)
            _button(layout, '结束本次会话（保留已有副本）', 'FINISH')
        layout.label(text=options.summary)
        layout.label(text='只写明确范围/原可写质量；锁组与其它点组保持')
        layout.label(text='扩大既有写域须先重新展示并确认')
        layout.label(text='保存会结束未提交绘制；副本保留，重开不恢复提交权')


_CLASSES = (C94LOCAL_Options, C94LOCAL_OT_Capture, C94LOCAL_OT_Action, C94LOCAL_PT_Local)


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
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94LOCAL_Options))
        _C94_OPTIONS_OWNED = True
        if _load_pre not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(_load_pre)
        if _save_pre not in bpy.app.handlers.save_pre:
            bpy.app.handlers.save_pre.append(_save_pre)
    except Exception:
        unregister()
        raise


def unregister():
    _release(bpy.context, '插件卸载：结束本次UI会话；任务和副本保留', force=True)
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)
    if _save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_save_pre)
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
            and prop.fixed_type == C94LOCAL_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
