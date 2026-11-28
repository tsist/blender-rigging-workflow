# SPDX-License-Identifier: GPL-3.0-or-later
"""Human and quadruped authoring in the same character workbench."""
import json
from pathlib import Path
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy.app.handlers import persistent

OPTIONS = 'rigging_modular_options_v1'
_REGISTERED = []
_SESSION = None


def _mesh(_self, obj): return obj.type == 'MESH'
def _armature(_self, obj): return obj.type == 'ARMATURE'
def options(context): return getattr(context.scene, OPTIONS)


class MODULAR_Options(bpy.types.PropertyGroup):
    kind: EnumProperty(name='基础模板', items=(('HUMAN', '极简人形', '身体、四肢、整掌与整体头部'),
        ('QUADRUPED', '猫狗类四足', '独立前爪与后爪机制')), default='HUMAN')
    label: StringProperty(name='新角色名称', default='新角色', maxlen=40)
    source: PointerProperty(name='原始模型', type=bpy.types.Object, poll=_mesh)
    body_collection: PointerProperty(name='分件身体集合（可选）', type=bpy.types.Collection)
    reference: PointerProperty(name='参考骨', type=bpy.types.Object, poll=_armature)
    rig: PointerProperty(name='生成骨架', type=bpy.types.Object, poll=_armature)
    head: BoolProperty(name='颈头', default=True)
    fore_l: BoolProperty(name='左臂 / 左前肢', default=True)
    fore_r: BoolProperty(name='右臂 / 右前肢', default=True)
    hind_l: BoolProperty(name='左腿 / 左后肢', default=True)
    hind_r: BoolProperty(name='右腿 / 右后肢', default=True)
    tail: BoolProperty(name='尾巴', default=False)
    shoulder_girdle: BoolProperty(name='人形肩带与肩胛控制', default=True,
        description='区分胸锁、肩锁与盂肱位置；生成独立肩胛载体，仍须逐角色检查骨架')
    correctives: BoolProperty(name='肩胯辅助骨（需逐角色验收）', default=False)
    fingers_l: BoolProperty(name='左手五指', default=False)
    fingers_r: BoolProperty(name='右手五指', default=False)
    face: BoolProperty(name='面部控制', default=False)
    segmented_limbs: BoolProperty(name='两段肢体 B-Bone', default=False)
    forward: EnumProperty(name='模型正面', items=(('-Y', '-Y', '正面朝-Y，Z向上'),
        ('Y', '+Y', '正面朝+Y，Z向上'), ('+X', '+X', '正面朝+X，Z向上'),
        ('-X', '-X', '正面朝-X，Z向上')), default='-Y')
    proxy_mode: EnumProperty(name='计算代理', items=(('AUTO', '自动检查闭体', '闭合表面保原样；不闭合时生成内部体素代理'),
        ('RAW', '原表面副本', '原始网格必须能定义闭合体积'),
        ('VOXEL', '内部体素代理', '只重建计算代理，生产几何保持')), default='AUTO')
    native_proxy: PointerProperty(name='已检查的计算代理', type=bpy.types.Object, poll=_mesh,
        description='独立骨架使用明确的单域代理；不自动创建代理或扩大已验收范围')
    resolution: IntProperty(name='最长边格数', default=96, min=16, max=256)
    transfer_cells: FloatProperty(name='代理回写距离（格）', default=3, min=.5, max=4,
        description='从有体素支撑的计算表面回写原网格；超距拒绝，不自动放大')
    jobs_directory: StringProperty(name='作业目录', subtype='DIR_PATH')
    gate_reference: PointerProperty(name='已验收的静态参照', type=bpy.types.Object, poll=_mesh)
    gate_receipt: StringProperty(name='骨架验收文件', subtype='FILE_PATH',
        description='选择对应本角色、当前结构和用途范围的完整通过回执')
    message: StringProperty(name='状态', default='选择原始模型，创建参考骨并检查位置')
    candidate: PointerProperty(name='蒙皮候选', type=bpy.types.Object, poll=_mesh)
    surface_candidate: PointerProperty(name='平滑候选', type=bpy.types.Object, poll=_mesh)
    surface_pin_boundary: BoolProperty(name='固定开放边界', default=True,
        description='保留原网格开放边界；从中立蒙皮建立独立原生平滑候选')
    motion_source: PointerProperty(name='动作来源骨架', type=bpy.types.Object, poll=_armature)
    motion_source_kind: EnumProperty(name='动作来源类型', items=(
        ('MIXAMO', 'Mixamo / 明确骨名映射', '使用已导入的动作骨架及可选骨名映射'),
        ('CMU', 'CMU BVH', '核对现有CMU精确骨名与层级'),
        ('PROFILE', '已保存的来源配置 / MMD', '使用通用来源配置或MMD导入时的配置文件')),
        default='MIXAMO')
    source_profile_file: StringProperty(name='来源配置文件', subtype='FILE_PATH')
    source_clip_id: StringProperty(name='MMD动作片段名称',
        description='使用MMD导入配置时，选择该配置中明确的动作片段')
    source_fps: IntProperty(name='来源帧率', default=30, min=1, max=240)
    mapping_file: StringProperty(name='可选骨名映射JSON', subtype='FILE_PATH')
    motion_in_place: BoolProperty(name='原地动作', default=False)
    motion_use_range: BoolProperty(name='使用动作片段', default=False,
        description='只转换指定来源帧；关闭时使用完整片段')
    motion_start: FloatProperty(name='来源起始帧', default=1.0, precision=3)
    motion_end: FloatProperty(name='来源结束帧', default=250.0, precision=3)
    motion_action: PointerProperty(name='动作候选', type=bpy.types.Action)


def _configuration(props):
    modules = ['body']
    for key, field in [('head', 'head'), ('forelimb.L', 'fore_l'), ('forelimb.R', 'fore_r'),
                       ('hindlimb.L', 'hind_l'), ('hindlimb.R', 'hind_r'), ('tail', 'tail'), ('correctives', 'correctives')]:
        if getattr(props, field): modules.append(key)
    if props.kind == 'HUMAN':
        for key, active in (('shoulder_girdle', props.shoulder_girdle), ('face', props.face and props.head),
                            ('fingers.L', props.fingers_l and props.fore_l), ('fingers.R', props.fingers_r and props.fore_r)):
            if active:
                modules.append(key)
    segments = 2 if props.kind == 'HUMAN' and props.segmented_limbs else 1
    return {'kind': props.kind, 'enabled_modules': modules, 'segments': segments, 'bbones': segments}


def _body_sources(props):
    if props.body_collection:
        sources=[o for o in props.body_collection.all_objects if o.type=='MESH']
        if props.source not in sources: raise ValueError('原始模型须属于指定身体集合；服装和刚性附件请用进阶权重策略')
        return sources
    return [props.source]


def _native_selected(props):
    # Layout hint only. StartBind reads and validates the actual identity.
    from . import rig_identity
    return bool(props.rig and rig_identity.NATIVE_KEY in props.rig)


def _check_position_sources(props):
    from . import modular_positioning
    if any('positioning' in str(k) for k in props.reference.keys()):
        state=modular_positioning.require_current(props.reference)
        if {r['object_pointer'] for r in state['targets']} != {int(o.as_pointer()) for o in _body_sources(props)}:
            raise ValueError('当前身体来源与定位候选不同，请重新提议并检查参考骨')


def _clear_results(props, *, clear_rig=False):
    action=props.motion_action
    if action is not None and action.users<=1:
        action.use_fake_user=True
    props.motion_action=None
    props.candidate=None
    props.surface_candidate=None
    if clear_rig:props.rig=None


def _select(context, obj, stage=None, *, role=False):
    if context.mode != 'OBJECT':
        if context.object != obj: raise ValueError('先结束当前对象的编辑，再切换目标')
        bpy.ops.object.mode_set(mode='OBJECT')
    for old in context.selected_objects: old.select_set(False)
    obj.hide_set(False); obj.select_set(True)
    context.view_layer.objects.active = obj
    from . import workflow_navigation
    nav = workflow_navigation.options(context)
    if nav:
        nav.target = obj if role else None
        if stage: nav.stage = stage


def _error(operator, props, exc):
    props.message = str(exc)
    operator.report({'ERROR'}, str(exc))
    return {'CANCELLED'}


class MODULAR_OT_Prepare(bpy.types.Operator):
    bl_idname = 'rigging_modular.prepare_reference'
    bl_label = '创建独立参考骨'
    bl_description = '创建新候选；必要时仅在本次会话启用Blender自带Rigify，不保存全局偏好'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = options(context)
        try:
            from . import modular_rig
            reference = modular_rig.prepare_reference(context.scene, kind=props.kind, label=props.label,
                configuration=_configuration(props), allow_runtime_enable=True)
            props.reference = reference
            _clear_results(props,clear_rig=True)
            _select(context, reference, 'PREPARE')
            props.message = '已创建参考；先自动估计并检查，或手动编辑后明确确认'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_Compose(bpy.types.Operator):
    bl_idname = 'rigging_modular.compose_reference'
    bl_label = '按模块创建新参考候选'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = options(context)
        try:
            from . import modular_rig
            reference = modular_rig.compose_reference(context.scene, props.reference,
                configuration=_configuration(props), label=props.label, allow_runtime_enable=True)
            props.reference = reference
            _clear_results(props,clear_rig=True)
            _select(context, reference, 'PREPARE')
            props.message = '新结构候选已创建；旧角色和动画保留，蒙皮与动作尚未迁移，重新检查并确认'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_Estimate(bpy.types.Operator):
    bl_idname = 'rigging_modular.estimate'
    bl_label = '自动提议关节位置'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = options(context)
        try:
            from . import modular_positioning
            report = modular_positioning.estimate(props.reference, _body_sources(props), forward_axis=props.forward, up_axis='Z')
            props.message = '定位候选待人工检查：' + str(report.get('status', 'proposed_needs_review'))
            _select(context, props.reference, 'PREPARE')
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_EditReference(bpy.types.Operator):
    bl_idname = 'rigging_modular.edit_reference'
    bl_label = '手调参考骨'

    def execute(self, context):
        props = options(context)
        try:
            _select(context, props.reference, 'PREPARE')
            props.reference.data.use_mirror_x = True
            bpy.ops.object.mode_set(mode='EDIT')
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_Confirm(bpy.types.Operator):
    bl_idname = 'rigging_modular.confirm_reference'
    bl_label = '检查完成，确认参考骨'

    def execute(self, context):
        props = options(context)
        try:
            from . import modular_rig, modular_positioning
            _select(context, props.reference, 'PREPARE')
            # If an estimate was used, its source must still match. A completely
            # manual reference remains an explicit user-authored confirmation.
            _check_position_sources(props)
            modular_rig.confirm_reference(props.reference, allow_runtime_enable=True)
            props.message = '参考已确认；继续生成正式骨架。后续编辑会使确认过期'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_Generate(bpy.types.Operator):
    bl_idname = 'rigging_modular.generate'
    bl_label = '生成新角色骨架'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = options(context)
        try:
            from . import modular_rig, workbench_ui
            _check_position_sources(props)
            before = set(bpy.data.collections)
            with workbench_ui.ensure_object_mode(context):
                output = modular_rig.generate(context.scene, props.reference, label=props.label, allow_runtime_enable=True)
            workbench_ui.theme_created_collections(before)
            props.rig = output['rig']
            _clear_results(props)
            _select(context, props.rig, 'BIND', role=True)
            props.message = '骨架已生成；原参考与旧角色保留，继续绑定原始模型'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


def _jobs_path(props):
    if props.jobs_directory:
        path = Path(bpy.path.abspath(props.jobs_directory))
    elif bpy.data.filepath:
        path = Path(bpy.data.filepath).parent / 'rigging-jobs'
    else:
        raise ValueError('请先保存工作副本，或在进阶中指定本次作业的绝对目录')
    if not path.is_absolute(): raise ValueError('需要明确的绝对作业目录')
    return path


def _timer():
    if _SESSION is None: return None
    scene = _SESSION['scene']
    if scene.name not in bpy.data.scenes: return None
    try:
        state = _SESSION['job'].status()
        props = getattr(scene, OPTIONS)
        from . import workbench_ui
        statuses = {'running': '正在求解', 'succeeded': '求解完成，可创建候选',
                    'failed': '求解失败', 'cancelled': '已取消', 'applied': '已创建候选'}
        props.message = statuses.get(state.get('status'), '正在读取作业状态')
        error = state.get('error')
        if error:
            props.message += ' · ' + str(error.get('message', error) if isinstance(error, dict) else error)
        workbench_ui.VOXEL_PROGRESS.observe(scene, state)
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == 'VIEW_3D': area.tag_redraw()
        return .4 if state.get('status') == 'running' else None
    except Exception as exc:
        getattr(scene, OPTIONS).message = str(exc)
        from . import workbench_ui
        workbench_ui.VOXEL_PROGRESS.observe(scene, {'status': 'failed'})
        return None


def _binding_running():
    if _SESSION is None: return False
    try: return _SESSION['job'].status().get('status') == 'running'
    except Exception: return True  # An unreadable active task cannot be reauthorized.


def _authorization_block_reason(rig):
    if _SESSION is None: return None
    try:
        job = _SESSION['job']; state = job.status()
        if state.get('status') == 'running': return '请先完成或取消当前求解'
        if job.rig == rig and state.get('status') == 'succeeded' and not state.get('candidate'):
            return '本骨架有待提交结果，请先创建候选或放弃结果'
    except Exception: return '无法确认现有作业状态，请先处理该作业'
    return None


class MODULAR_OT_AuthorizeSkeleton(bpy.types.Operator):
    bl_idname = 'rigging_modular.authorize_skeleton'
    bl_label = '登记当前骨架验收'
    bl_description = '核对已审核文件与当前骨架、静态参照和动作；不自动批准骨架质量'

    def execute(self, context):
        props = options(context)
        try:
            blocked = _authorization_block_reason(props.rig)
            if blocked: raise ValueError(blocked)
            from . import skeleton_collection, skeleton_gate
            skeleton_gate.revoke(props.rig)
            if not props.gate_receipt: raise ValueError('请选择完整骨架验收文件')
            if props.gate_reference is None: raise ValueError('请选择验收时使用的静态参照')
            path = Path(bpy.path.abspath(props.gate_receipt))
            if not path.is_absolute(): raise ValueError('骨架验收文件需要绝对路径')
            # Only recover the exact author-supplied region map here. Authority
            # comes solely from authorize, which validates the complete closure.
            receipt = skeleton_collection.read(path)
            evidence = skeleton_collection.read_verified(receipt.get('evidence'))
            envelope = skeleton_collection.read_verified(evidence.get('identity_source'))
            regions = envelope.get('regions')
            if (not isinstance(regions, dict) or props.rig is None
                    or set(regions) != set(props.rig.data.bones.keys())):
                raise ValueError('验收文件缺少当前完整骨架的分区映射')
            skeleton_gate.authorize(props.rig, props.gate_reference, regions, path)
            props.message = '已登记限定骨架验收；各项操作仍按验收范围核对，重开文件需重新登记'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_RevokeSkeleton(bpy.types.Operator):
    bl_idname = 'rigging_modular.revoke_skeleton'
    bl_label = '撤销本次登记'

    def execute(self, context):
        props = options(context)
        try:
            blocked = _authorization_block_reason(props.rig)
            if blocked: raise ValueError(blocked)
            from . import skeleton_gate
            skeleton_gate.revoke(props.rig)
            props.message = '已撤销本次骨架验收登记；蒙皮前需重新核对并登记'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_DiscardBinding(bpy.types.Operator):
    bl_idname = 'rigging_modular.discard_binding_result'
    bl_label = '放弃本次待提交结果'
    bl_description = '仅结束本工作台的已停止作业；保留磁盘记录，之后可重新登记验收'

    def execute(self, context):
        global _SESSION
        props = options(context)
        try:
            if _SESSION is None: raise ValueError('当前工作台没有待处理作业')
            if _binding_running(): raise ValueError('请先取消或等待求解结束，再放弃结果')
            _SESSION = None
            if bpy.app.timers.is_registered(_timer): bpy.app.timers.unregister(_timer)
            props.message = '已放弃本次待提交结果，磁盘记录保留；可重新登记验收并开始新任务'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_StartBind(bpy.types.Operator):
    bl_idname = 'rigging_modular.start_binding'
    bl_label = '开始后台体素蒙皮'

    def execute(self, context):
        global _SESSION
        props = options(context)
        try:
            if _SESSION and _SESSION['job'].status().get('status') == 'running':
                raise ValueError('本工作台已有求解任务，请先完成或取消')
            from . import modular_workflow, rig_identity, workbench_ui
            metadata = rig_identity.read(props.rig)
            native_mode = metadata.get('schema') == 'rigging.native-skeleton.v1'
            if native_mode and props.body_collection:
                raise ValueError('当前独立骨架只支持单件身体，请先清空身体集合')
            sources=_body_sources(props)
            if native_mode:
                if props.native_proxy is None: raise ValueError('请选择已检查的单域计算代理')
                from . import native_binding
                settings = {'cells_longest': props.resolution, 'max_cells': 600000,
                    'max_memory_mb': 1024, 'max_seconds': 300, 'max_iterations': 3000,
                    'tolerance': 1e-6, 'neighbor_count': 26,
                    'require_all_bones_seeded': True}
                job = native_binding.start(props.source, props.rig, props.native_proxy,
                    list(metadata['deforms']), settings, jobs_directory=_jobs_path(props),
                    transfer_distance_cells=props.transfer_cells)
            else:
                job = modular_workflow.start_binding(props.source, props.rig, proxy_mode=props.proxy_mode,
                    settings={'cells_longest': props.resolution}, jobs_directory=_jobs_path(props), body_sources=sources,
                    transfer_distance_cells=props.transfer_cells)
            props.candidate=None
            props.surface_candidate=None
            _SESSION = {'scene': context.scene, 'job': job,'body_collection':props.body_collection,
                        'source_ids':{o.as_pointer() for o in sources}, 'native_mode':native_mode}
            workbench_ui.VOXEL_PROGRESS.observe(context.scene, {'status': 'running', 'phase': 'starting'})
            if not bpy.app.timers.is_registered(_timer): bpy.app.timers.register(_timer, first_interval=.2)
            props.message = '后台求解已开始；成功后仍需显式创建与检查候选'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_CancelBind(bpy.types.Operator):
    bl_idname = 'rigging_modular.cancel_binding'
    bl_label = '取消本次求解'

    def execute(self, context):
        props = options(context)
        try:
            if not _SESSION or _SESSION['scene'] != context.scene: raise ValueError('当前场景没有本次任务')
            _SESSION['job'].cancel()
            props.message = '已请求取消；未应用任何不完整权重'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_ApplyBind(bpy.types.Operator):
    bl_idname = 'rigging_modular.apply_binding'
    bl_label = '创建独立蒙皮候选'

    def execute(self, context):
        props = options(context)
        try:
            if not _SESSION or _SESSION['scene'] != context.scene: raise ValueError('保存重开后需重新准备活任务')
            job=_SESSION['job']
            if (props.source!=job.source or props.rig!=job.rig
                    or props.body_collection!=_SESSION['body_collection']
                    or (_SESSION.get('native_mode') and props.native_proxy!=job.proxy)
                    or {o.as_pointer() for o in _body_sources(props)}!=_SESSION['source_ids']):
                raise ValueError('当前身体集合/部件/骨架已与本任务不同，请恢复原选择或重新求解')
            import uuid
            from . import workbench_ui
            before = set(bpy.data.collections)
            with workbench_ui.ensure_object_mode(context):
                props.candidate = job.apply(name=props.label[:35] + '_蒙皮_' + uuid.uuid4().hex[:6])
            workbench_ui.theme_created_collections(before)
            workbench_ui.VOXEL_PROGRESS.observe(context.scene, {'status': 'applied'})
            _select(context, props.candidate, 'CONTROL')
            from . import workflow_navigation
            nav = workflow_navigation.options(context)
            if nav:
                nav.target = props.rig
            props.message = '候选已创建；原始模型保留，请检查实际姿态与周边权重'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_Pose(bpy.types.Operator):
    bl_idname = 'rigging_modular.pose'
    bl_label = '进入骨架姿态模式'

    def execute(self, context):
        props = options(context)
        try:
            _select(context, props.rig, 'CONTROL', role=True)
            bpy.ops.object.mode_set(mode='POSE')
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_Surface(bpy.types.Operator):
    bl_idname = 'rigging_modular.create_surface_candidate'
    bl_label = '创建原生平滑候选'
    bl_description = '保留原蒙皮；需先恢复中立，创建可关闭的姿态修正平滑副本，不重算权重'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = options(context)
        try:
            from . import modular_surface, workflow_navigation
            import uuid
            if context.mode == 'POSE' and context.object == props.rig:
                bpy.ops.object.mode_set(mode='OBJECT')
            output = modular_surface.create_candidate(props.candidate, props.rig,
                name=props.label[:30] + '_平滑_' + uuid.uuid4().hex[:6],
                pin_boundary=props.surface_pin_boundary)
            props.surface_candidate = output['candidate']
            _select(context, props.surface_candidate, 'CONTROL')
            nav = workflow_navigation.options(context)
            if nav:
                nav.target = props.rig
            props.message = '平滑候选已创建；原蒙皮保留，关闭新修饰器可恢复对照'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_Retarget(bpy.types.Operator):
    bl_idname = 'rigging_modular.retarget'
    bl_label = '生成独立人形动作候选'

    def execute(self, context):
        props = options(context)
        try:
            from . import modular_retarget, retarget_profile
            mapping, profile = None, None
            fps = props.source_fps
            if props.motion_source_kind == 'CMU':
                profile = retarget_profile.from_cmu(props.motion_source, source_fps=fps)
            elif props.motion_source_kind == 'PROFILE':
                if not props.source_profile_file: raise ValueError('请选择来源配置文件')
                path = Path(bpy.path.abspath(props.source_profile_file))
                document = json.loads(path.read_text(encoding='utf-8-sig'))
                schema = document.get('schema') if isinstance(document,dict) else None
                if schema == retarget_profile.PROFILE_SCHEMA:
                    profile = document
                elif schema == 'c94.retarget.mmd_profile.v1':
                    if not props.source_clip_id:
                        clips=', '.join(str(row.get('id')) for row in document.get('clips',[]) if row.get('body_convertible',True))
                        raise ValueError('请填写MMD动作片段名称：'+clips)
                    profile = retarget_profile.from_mmd(props.motion_source, metadata_profile=path,
                        clip_id=props.source_clip_id)
                else: raise ValueError('文件不是已支持的来源配置或MMD导入配置')
                fps = None  # The selected profile owns its recorded timebase.
            else:
                mapping = json.loads(Path(bpy.path.abspath(props.mapping_file)).read_text(encoding='utf-8-sig')) if props.mapping_file else None
            output = modular_retarget.convert(props.motion_source, props.rig, profile=profile, mapping=mapping,
                source_fps=fps, motion='IN_PLACE' if props.motion_in_place else 'PRESERVE',
                start=props.motion_start if props.motion_use_range else None,
                end=props.motion_end if props.motion_use_range else None)
            props.motion_action = output['action']
            props.message = '人形动作候选已创建，待实际预览；原动作保留'
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_OT_PreviewMotion(bpy.types.Operator):
    bl_idname = 'rigging_modular.preview_motion'
    bl_label = '预览 / 恢复动作'

    def execute(self, context):
        props = options(context)
        try:
            from . import retarget_action
            if retarget_action.has_preview(props.rig): retarget_action.stop_preview(props.rig)
            else: retarget_action.begin_preview(props.rig, props.motion_action)
            return {'FINISHED'}
        except Exception as exc: return _error(self, props, exc)


class MODULAR_PT_Prepare(bpy.types.Panel):
    bl_idname = 'MODULAR_PT_prepare'
    bl_label = '独立人形与四足模板'
    bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = '角色工作台'
    workflow_stage = 'PREPARE'
    workflow_title = '独立人形与四足 · 参考骨'

    def draw(self, context):
        p, layout = options(context), self.layout
        layout.prop(p, 'kind'); layout.prop(p, 'label'); layout.prop(p, 'source')
        layout.prop(p, 'body_collection')
        box = layout.box(); box.label(text='本次组合')
        for field in ('head', 'fore_l', 'fore_r', 'hind_l', 'hind_r', 'tail', 'correctives'): box.prop(p, field)
        if p.kind == 'HUMAN':
            for field in ('shoulder_girdle', 'fingers_l', 'fingers_r', 'face', 'segmented_limbs'):
                box.prop(p, field)
        layout.operator(MODULAR_OT_Prepare.bl_idname)
        layout.prop(p, 'reference')
        layout.operator(MODULAR_OT_Compose.bl_idname)
        layout.prop(p, 'forward')
        layout.operator(MODULAR_OT_Estimate.bl_idname)
        if p.kind == 'HUMAN':
            layout.label(text='请重点从正侧面核对髋关节位置')
        layout.operator(MODULAR_OT_EditReference.bl_idname)
        layout.operator(MODULAR_OT_Confirm.bl_idname)
        layout.operator(MODULAR_OT_Generate.bl_idname)
        layout.label(text=p.message)


class MODULAR_PT_Bind(bpy.types.Panel):
    bl_idname = 'MODULAR_PT_bind'
    bl_label = '原始模型体素绑定'
    bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = '角色工作台'
    workflow_stage = 'BIND'; workflow_title = '模块化角色 · 原始模型绑定'

    def draw(self, context):
        p, layout = options(context), self.layout
        layout.prop(p, 'source'); layout.prop(p, 'rig')
        native_mode = _native_selected(p)
        if not native_mode or p.body_collection: layout.prop(p, 'body_collection')
        if native_mode and p.body_collection: layout.label(text='独立骨架使用单件身体，请清空上方集合')
        if p.body_collection: layout.label(text='共用完整身体计算体积；逐件选择原始模型创建候选')
        box = layout.box(); box.label(text='先验收骨架，再进行蒙皮')
        box.prop(p, 'gate_reference'); box.prop(p, 'gate_receipt')
        blocked = _authorization_block_reason(p.rig)
        row = box.row(align=True); row.enabled = not blocked
        row.operator(MODULAR_OT_AuthorizeSkeleton.bl_idname)
        row.operator(MODULAR_OT_RevokeSkeleton.bl_idname)
        if blocked: box.label(text=blocked)
        if _SESSION is not None:
            row = box.row(); row.enabled = not _binding_running()
            row.operator(MODULAR_OT_DiscardBinding.bl_idname)
        box.label(text='先完成骨位、分区多视图与运动检查')
        box.label(text='登记限当前会话；重新打开文件后需再次核对')
        if native_mode:
            layout.prop(p, 'native_proxy')
            layout.label(text='单件身体与已检查代理；保留全部变形骨')
        else: layout.prop(p, 'proxy_mode')
        layout.prop(p, 'resolution')
        layout.prop(p, 'transfer_cells')
        layout.prop(p, 'jobs_directory')
        layout.operator(MODULAR_OT_StartBind.bl_idname)
        layout.operator(MODULAR_OT_CancelBind.bl_idname)
        layout.operator(MODULAR_OT_ApplyBind.bl_idname)
        layout.prop(p, 'candidate'); layout.label(text=p.message)
        layout.label(text='服装/薄片：进阶权重策略转移；附件：明确刚性归属')


class MODULAR_PT_Control(bpy.types.Panel):
    bl_idname = 'MODULAR_PT_control'
    bl_label = '简洁身体控制'
    bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = '角色工作台'
    workflow_stage = 'CONTROL'; workflow_title = '模块化角色 · 身体与四肢'

    @classmethod
    def poll(cls, context): return bool(options(context).rig and options(context).rig.get('rigging_modular_v1'))

    def draw(self, context):
        p, layout = options(context), self.layout
        layout.prop(p, 'rig'); layout.operator(MODULAR_OT_Pose.bl_idname)
        for root, title in [('upper_arm', '臂 / 前肢'), ('thigh', '腿 / 后肢')]:
            for side, label in [('L', '左'), ('R', '右')]:
                bone = p.rig.pose.bones.get(root + '_parent.' + side)
                if bone and 'IK_FK' in bone: layout.prop(bone, '["IK_FK"]', text=label + title + ' IK/FK', slider=True)
        layout.label(text='选择原生控件调整身体、四肢或尾巴')
        if p.candidate and p.kind == 'HUMAN':
            box = layout.box()
            box.label(text='人形可选：原生姿态修正平滑')
            box.prop(p, 'surface_pin_boundary')
            box.operator(MODULAR_OT_Surface.bl_idname)
            box.prop(p, 'surface_candidate')
            box.label(text='先恢复中立；只显示一个候选以对照')
        layout.label(text=p.message)


class MODULAR_PT_Motion(bpy.types.Panel):
    bl_idname = 'MODULAR_PT_motion'
    bl_label = '人形动作复用'
    bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = '角色工作台'
    workflow_stage = 'MOTION'; workflow_title = '模块化人形 · 动作重定向'

    @classmethod
    def poll(cls, context): return bool(options(context).rig and options(context).rig.get('rigging_modular_v1'))

    def draw(self, context):
        p, layout = options(context), self.layout
        layout.prop(p, 'motion_source'); layout.prop(p, 'rig'); layout.prop(p, 'motion_source_kind')
        if p.motion_source_kind == 'PROFILE':
            layout.prop(p, 'source_profile_file'); layout.prop(p, 'source_clip_id')
        else:
            layout.prop(p, 'source_fps')
            if p.motion_source_kind == 'MIXAMO':layout.prop(p, 'mapping_file')
        layout.prop(p, 'motion_in_place')
        layout.prop(p, 'motion_use_range')
        if p.motion_use_range:
            row = layout.row(align=True)
            row.prop(p, 'motion_start'); row.prop(p, 'motion_end')
        layout.operator(MODULAR_OT_Retarget.bl_idname)
        layout.prop(p, 'motion_action'); layout.operator(MODULAR_OT_PreviewMotion.bl_idname)
        layout.label(text='四足首版使用手工动画'); layout.label(text=p.message)


@persistent
def _load_pre(_unused):
    global _SESSION
    if _SESSION:
        try:
            if _SESSION['job'].status().get('status') == 'running': _SESSION['job'].cancel()
        finally: _SESSION = None
    if bpy.app.timers.is_registered(_timer): bpy.app.timers.unregister(_timer)


CLASSES = (MODULAR_Options, MODULAR_OT_Prepare, MODULAR_OT_Compose, MODULAR_OT_Estimate,
    MODULAR_OT_EditReference, MODULAR_OT_Confirm, MODULAR_OT_Generate, MODULAR_OT_StartBind,
    MODULAR_OT_AuthorizeSkeleton, MODULAR_OT_RevokeSkeleton,
    MODULAR_OT_DiscardBinding,
    MODULAR_OT_CancelBind, MODULAR_OT_ApplyBind, MODULAR_OT_Pose, MODULAR_OT_Retarget,
    MODULAR_OT_Surface,
    MODULAR_OT_PreviewMotion, MODULAR_PT_Prepare, MODULAR_PT_Bind, MODULAR_PT_Control, MODULAR_PT_Motion)


def register():
    if _REGISTERED or hasattr(bpy.types.Scene, OPTIONS): raise RuntimeError('模块化工作台已注册')
    for cls in CLASSES:
        bpy.utils.register_class(cls); _REGISTERED.append(cls)
    setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=MODULAR_Options))
    bpy.app.handlers.load_pre.append(_load_pre)


def unregister():
    _load_pre(None)
    if _load_pre in bpy.app.handlers.load_pre: bpy.app.handlers.load_pre.remove(_load_pre)
    if hasattr(bpy.types.Scene, OPTIONS): delattr(bpy.types.Scene, OPTIONS)
    for cls in reversed(tuple(_REGISTERED)):
        bpy.utils.unregister_class(cls); _REGISTERED.remove(cls)
