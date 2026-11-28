# SPDX-License-Identifier: GPL-3.0-or-later
"""Narrow explicit-source UI for the shared native face capture core."""
import bpy
from bpy.props import CollectionProperty, IntProperty, PointerProperty, StringProperty

from . import face_capture, face_capture_group as group

OPTIONS = 'c94_face_capture_options_v1'
_REGISTERED = []
_ERRORS = (ValueError, RuntimeError, ReferenceError, KeyError, TypeError, AttributeError)


class C94FC_Member(bpy.types.PropertyGroup):
    source: PointerProperty(name='同步附件网格', type=bpy.types.Object,
                            poll=lambda self, obj: obj.type == 'MESH')


class C94FC_Options(bpy.types.PropertyGroup):
    source: PointerProperty(name='源面部网格', type=bpy.types.Object,
                            poll=lambda self, obj: obj.type == 'MESH')
    rig: PointerProperty(name='表情采样骨架', type=bpy.types.Object,
                         poll=lambda self, obj: obj.type == 'ARMATURE')
    sample_name: StringProperty(name='表情名称', default='闭眼')
    attachments: CollectionProperty(type=C94FC_Member)
    candidates: CollectionProperty(type=C94FC_Member)
    candidate_sources: CollectionProperty(type=C94FC_Member)
    bound_hub: PointerProperty(name='候选同步控制',type=bpy.types.Object)
    token: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    message: StringProperty(options={'SKIP_SAVE'})


def _session(context):
    return group.get_session(getattr(context.scene, OPTIONS).token)


class C94FC_OT_AddMembers(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_capture_add_members'
    bl_label = '添加所选附件网格'
    bl_description = '将所选网格加入同步采样列表；皮肤与附件生成同名表情形态键'

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            try:
                _session(context)
            except ValueError:
                pass
            else:
                raise ValueError('请结束捕获后再改同步列表')
            existing = {r.source for r in options.attachments} | {options.source}
            for obj in context.selected_objects:
                if obj.type == 'MESH' and obj not in existing:
                    options.attachments.add().source = obj
                    existing.add(obj)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FC_OT_RemoveMember(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_capture_remove_member'
    bl_label = '移出同步列表'
    index: IntProperty(min=0)

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            _session(context)
        except ValueError:
            if self.index < len(options.attachments):
                options.attachments.remove(self.index)
            return {'FINISHED'}
        self.report({'ERROR'}, '请结束捕获后再改同步列表')
        return {'CANCELLED'}


class C94FC_OT_Begin(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_capture_begin'
    bl_label = '开始面部形态键捕获'
    bl_description = '从已记录、已校准的面部中立姿态开始；明确选择源网格与骨架'

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            if options.token:
                try:
                    group.get_session(options.token)
                except ValueError:
                    options.token = ''
                else:
                    raise ValueError('请先取消现有会话；不会覆盖已采样内容')
            session = group.begin([options.source] + [r.source for r in options.attachments], options.rig)
            options.token = session.token
            options.message = '已捕获中立；在骨控模式摆出面部姿态后采样'
            self.report({'INFO'}, options.message)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FC_OT_Sample(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_capture_sample'
    bl_label = '采样当前骨控表情'
    bl_description = '记录当前骨控表情；开始前请关闭已有表情形态键，原网格保持'

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            report = group.sample(_session(context), options.sample_name)
            options.message = '已记录' + report['name'] + '；共' + str(report['sample_count']) + '个表情'
            self.report({'INFO'}, options.message)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FC_OT_Create(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_capture_create'
    bl_label = '创建整组独立形态键候选'
    bl_description = '临时恢复已声明面部中立并逐形态键原生重放；成功或失败均恢复操作者面部输入'
    bl_options = {'REGISTER','UNDO'}

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            session=_session(context)
            sources=[m.source for m in session.members]
            candidates = group.create_candidates(session)
            options.candidates.clear();options.candidate_sources.clear();options.bound_hub=None
            for obj in candidates:options.candidates.add().source=obj
            for obj in sources:options.candidate_sources.add().source=obj
            options.token = ''
            options.message = '已创建整组' + str(len(candidates)) + '个独立候选；新Key默认0'
            self.report({'INFO'}, options.message)
            return {'FINISHED'}
        except _ERRORS as exc:
            options.token = ''  # A submission attempt ends the complete group.
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FC_OT_BindCandidates(bpy.types.Operator):
    bl_idname='c94_workflow.face_capture_bind_candidates'
    bl_label='复位到形态键中立并建立候选控制'
    bl_description='明确复位面部输入后，给本次完整候选组建立同步控制；已有控制/动画不覆盖'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        from . import face_expression_controls as controls,face_expression_controls_ui as ui,face_modes as fm,face_focus
        options=getattr(context.scene,OPTIONS);rig=options.rig;before=old=None
        try:
            if options.bound_hub:raise ValueError('本批候选已经受管，请保留现有控制')
            candidates=[r.source for r in options.candidates];sources=[r.source for r in options.candidate_sources]
            if not candidates or len(candidates)!=len(sources) or not all(candidates+sources):raise ValueError('需要本次完整候选及其明确原始源')
            if face_focus.status(context).get('active'):face_focus.leave(context)
            before=fm._snapshot(rig);old=rig.get(fm.STATE_KEY)
            fm.switch(rig,target='KEY',reset=True)
            import uuid
            hub=controls.create(rig,candidates,name='采样表情控制_'+uuid.uuid4().hex[:8])
        except _ERRORS as exc:
            if before is not None:
                try:
                    fm._apply_inputs(rig,before['inputs']);fm._set_mode(rig,before['mode']);fm._apply_visibility(rig,before['visibility'])
                    if old is not None:rig[fm.STATE_KEY]=old
                    fm._update(rig)
                except _ERRORS as recovery:
                    self.report({'ERROR'},str(exc)+'；恢复失败：'+str(recovery));return {'CANCELLED'}
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        try:
            for i,obj in enumerate(sources):hub['author_source_%03d'%i]=obj
            options.bound_hub=hub;getattr(context.scene,ui.OPTIONS).hub=hub
            options.message='已建立同步控制；可建三维板并进入干净检查'
        except _ERRORS as exc:
            self.report({'WARNING'},'同步控制已创建：'+hub.name+'；界面书签需检查：'+str(exc))
        return {'FINISHED'}


class C94FC_OT_Cancel(bpy.types.Operator):
    bl_idname = 'c94_workflow.face_capture_cancel'
    bl_label = '结束当前捕获会话'
    bl_description = '只清内存采样；不删除原工程或已创建候选'

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            try:
                session = _session(context)
            except ValueError:
                session = None
            if session:
                group.cancel(session)
            options.token = ''
            options.message = '捕获会话已结束，工程与已创建候选保留'
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class C94FC_PT_Capture(bpy.types.Panel):
    bl_idname = 'C94FC_PT_capture'
    bl_label = '面部骨控姿态→形态键候选'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '面部'
    bl_order = 3
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        options = getattr(context.scene, OPTIONS)
        try:
            session = _session(context)
        except ValueError:
            session = None
        inputs = layout.column()
        inputs.enabled = session is None
        inputs.prop(options, 'source')
        inputs.prop(options, 'rig')
        for index, member in enumerate(options.attachments):
            row = inputs.row(align=True)
            row.prop(member, 'source', text='同步附件')
            row.operator('c94_workflow.face_capture_remove_member', text='', icon='X').index = index
        inputs.operator('c94_workflow.face_capture_add_members')
        if session:
            try:
                source_name = '、'.join(m.source.name for m in session.members)
                session.rig.name
            except ReferenceError:
                layout.label(text='源或骨架已删除，请结束当前会话')
                layout.operator('c94_workflow.face_capture_cancel')
                return
            layout.label(text='当前源：' + source_name)
            layout.label(text='形态键重放中立模式：' + session.members[0]._neutral_mode)
            layout.label(text='已采样：' + str(len(session._samples)))
            for name in session._samples:
                layout.label(text=name)
            layout.prop(options, 'sample_name')
            layout.operator('c94_workflow.face_capture_sample')
            layout.operator('c94_workflow.face_capture_create')
            layout.operator('c94_workflow.face_capture_cancel')
        else:
            layout.operator('c94_workflow.face_capture_begin')
        if options.message:
            layout.label(text=options.message)
        if options.candidates:
            box=layout.box();box.label(text='本次完整候选组')
            for row in options.candidates:
                if row.source:box.label(text=row.source.name)
            if not options.bound_hub:box.operator('c94_workflow.face_capture_bind_candidates')
            else:box.label(text='已建立：'+options.bound_hub.name)
        layout.label(text='请先创建候选，再保存或换文件')
        layout.label(text='创建后请检查表情与附件效果')
        layout.label(text='组合形态键效果须另行检查')
        layout.label(text='静止成员保留同名零位移形态键')


_CLASSES = (C94FC_Member, C94FC_Options, C94FC_OT_AddMembers, C94FC_OT_RemoveMember, C94FC_OT_Begin, C94FC_OT_Sample,C94FC_OT_BindCandidates,
            C94FC_OT_Create, C94FC_OT_Cancel, C94FC_PT_Capture)


def register():
    global _C94_OPTIONS_OWNED, _C94_CAPTURE_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('该面板属性已由其他实例持有；原属性保持')
    if (group._SESSIONS or face_capture._SESSIONS or any(
            callback in handlers
            for handlers in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre)
            for callback in (group._clear_sessions, face_capture._clear_sessions))):
        raise RuntimeError('面部捕获已由其他入口启用；原会话保持')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94FC_Options))
        _C94_OPTIONS_OWNED = True
        _C94_CAPTURE_OWNED = True
        group.register()
    except Exception:
        unregister()
        raise


def unregister():
    global _C94_CAPTURE_OWNED
    if _C94_CAPTURE_OWNED:
        group.unregister()
        face_capture.unregister()
        _C94_CAPTURE_OWNED = False
    if hasattr(bpy.types.Scene, OPTIONS):
        _release_workspace_options()
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()


_C94_OPTIONS_OWNED = False
_C94_CAPTURE_OWNED = False

def _release_workspace_options():
    global _C94_OPTIONS_OWNED
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if (_C94_OPTIONS_OWNED and prop and prop.type == 'POINTER'
            and prop.fixed_type == C94FC_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
