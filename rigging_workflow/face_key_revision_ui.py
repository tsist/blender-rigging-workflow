# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Key edit drafts, explicit preview and source-preserving submission."""
import json,uuid
import bpy
from bpy.props import PointerProperty,StringProperty,IntProperty
from . import face_key_revision as revision,face_expression_controls_ui as controls,face_focus

OPTIONS='c94_face_key_revision_options_v1'
_REGISTERED=[]
_ERRORS=(ValueError,RuntimeError,ReferenceError,KeyError,TypeError,AttributeError,IndexError)


class C94FKR_Options(bpy.types.PropertyGroup):
    channel:StringProperty(name='修正表情')
    controller:PointerProperty(name='持久修正草稿',type=bpy.types.Object,poll=lambda self,o:revision.STATE_KEY in o)
    message:StringProperty(options={'SKIP_SAVE'})


def _preview(context,controller,index):
    record,hub,rig,_,sources,drafts=revision._load(controller)
    if record['status']!='draft' or not 0<=index<len(drafts):raise ValueError('需要有效的未提交草稿成员')
    if context.mode not in {'OBJECT','POSE'}:raise ValueError('请先结束编辑/雕刻模式')
    if face_focus.status(context).get('active'):face_focus.leave(context)
    if context.mode=='POSE':bpy.ops.object.mode_set(mode='OBJECT')
    # Re-enter preview from the previously restored state. This keeps the
    # original visibility snapshot meaningful and prevents stale records when
    # the controller changed since the last preview.
    if '_revision_preview' in controller:
        _restore_preview(controller)
    state=dict(scene=bpy.context.scene.name,layer=context.view_layer.name,
               source=[o.hide_get() for o in sources],draft=[o.hide_get() for o in drafts],
               rig_hidden=rig.hide_get(),source_names=[o.name for o in sources],
               draft_names=[o.name for o in drafts],rig_name=rig.name,
               applied_source=[True]*len(sources),applied_draft=[False]*len(drafts),
               applied_rig=True)
    controller['_revision_preview']=json.dumps(state)
    try:
        rig.hide_set(True)
        for o in sources:o.hide_set(True)
        for i,o in enumerate(drafts):
            o.hide_set(False)
            for key in o.data.shape_keys.key_blocks:
                if key!=o.data.shape_keys.reference_key:key.value=0
            channel=next(r for r in record['group']['channels'] if r['name']==record['channels'][0])
            key=o.data.shape_keys.key_blocks[revision.core._key_name(channel,i)]
            key.value=1;o.active_shape_key_index=list(o.data.shape_keys.key_blocks).index(key);o.show_only_shape_key=False
        for o in context.selected_objects:o.select_set(False)
        drafts[index].select_set(True);context.view_layer.objects.active=drafts[index]
        if context.area and context.area.type=='VIEW_3D':
            region=next((r for r in context.area.regions if r.type=='WINDOW'),None)
            if region:
                with context.temp_override(area=context.area,region=region):bpy.ops.view3d.view_selected(use_all_regions=False)
        return drafts[index]
    except Exception as error:
        try:_restore_preview(controller)
        except Exception as recovery:
            raise RuntimeError(str(error)+'；预览显隐恢复未完成，可点结束预览重试：'+str(recovery)) from error
        raise


def _restore_preview(controller):
    if '_revision_preview' not in controller:return
    state=json.loads(controller['_revision_preview'])
    scene_name=state.get('scene')
    scene=controller.get('scene')
    if not isinstance(scene,bpy.types.Scene):
        scene=bpy.data.scenes.get(scene_name) if isinstance(scene_name,str) else bpy.context.scene
    if not isinstance(scene,bpy.types.Scene):
        raise ValueError('草稿预览的原场景已不存在，保留恢复记录')
    layer=scene.view_layers.get(state['layer'])
    if layer is None:
        # The only layer this workflow modified is gone; no hidden state of
        # ours remains to restore.
        del controller['_revision_preview'];return
    source_names=state.get('source_names');draft_names=state.get('draft_names')
    rig_name=state.get('rig_name')
    pointer_fallback=False
    if not isinstance(source_names,list) or not isinstance(draft_names,list):
        try:
            _record,_hub,rig,_layer,_sources,_drafts=revision._load(controller)
            source_names=[o.name for o in _sources];draft_names=[o.name for o in _drafts];rig_name=rig.name
        except _ERRORS:
            # Legacy preview records have no names. The controller's real
            # source/draft pointer properties still identify the objects even
            # when the saved record/names are stale.
            pointer_fallback=True
            source_names=[None]*len(state['source']);draft_names=[None]*len(state['draft']);rig_name=None
    def valid(obj):
        return (isinstance(obj,bpy.types.Object) and scene.objects.get(obj.name) is obj
                and layer.objects.get(obj.name) is obj)
    def resolve(name,pointer):
        if pointer in controller:
            # The saved pointer is authoritative. A same-named new Object must
            # never be substituted after the original was renamed.
            obj=controller.get(pointer)
        else:
            obj=bpy.data.objects.get(name) if isinstance(name,str) else None
        return obj if valid(obj) else None
    if pointer_fallback:
        def pointer_object(pointer):
            obj=controller.get(pointer) if pointer in controller else None
            return obj if valid(obj) else None
        source=[pointer_object('source_%03d'%i) for i in range(len(source_names))]
        draft=[pointer_object('draft_%03d'%i) for i in range(len(draft_names))]
    else:
        source=[resolve(name,'source_%03d'%i) for i,name in enumerate(source_names)]
        draft=[resolve(name,'draft_%03d'%i) for i,name in enumerate(draft_names)]
    if len(state['source'])!=len(source) or len(state['draft'])!=len(draft):
        raise ValueError('草稿预览显隐记录损坏')
    applied_source=state.get('applied_source')
    applied_draft=state.get('applied_draft')
    if not isinstance(applied_source,list) or len(applied_source)!=len(source):applied_source=[True]*len(source)
    if not isinstance(applied_draft,list) or len(applied_draft)!=len(draft):applied_draft=[False]*len(draft)
    restore_errors=[]
    def restore(obj,original,applied):
        if obj is None:return
        # Do not overwrite a visibility change made outside this preview.
        try:
            if obj.hide_get(view_layer=layer)==bool(applied):
                obj.hide_set(bool(original),view_layer=layer)
        except Exception as error:
            restore_errors.append(getattr(obj,'name','<missing>')+': '+str(error))
    for obj,hidden,applied in zip(source,state['source'],applied_source):restore(obj,hidden,applied)
    for obj,hidden,applied in zip(draft,state['draft'],applied_draft):restore(obj,hidden,applied)
    if pointer_fallback:
        rig=controller.get('rig')
        if not valid(rig):
            rig=None
    else:
        rig=resolve(rig_name,'rig')
    if rig is not None and 'rig_hidden' in state:
        restore(rig,state['rig_hidden'],state.get('applied_rig',True))
    if restore_errors:
        raise RuntimeError('部分预览显隐未能恢复；保留记录以便重试：'+'; '.join(restore_errors))
    del controller['_revision_preview']


class C94FKR_OT_Prepare(bpy.types.Operator):
    bl_idname='c94_workflow.face_key_revision_prepare'
    bl_label='准备独立Key修正草稿'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        try:
            hub=getattr(context.scene,controls.OPTIONS).hub
            controller=revision.prepare(hub,[opts.channel],name='表情修正草稿_'+uuid.uuid4().hex[:8])
            opts.controller=controller
            for name in revision.inspect(controller)['members']:bpy.data.objects[name].hide_set(True)
            opts.message='草稿已独立保存；点预览后用原生Edit/Sculpt修正目标Key，源保持'
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FKR_OT_Preview(bpy.types.Operator):
    bl_idname='c94_workflow.face_key_revision_preview'
    bl_label='预览并选择修正成员'
    bl_options={'REGISTER','UNDO'}
    index:IntProperty(default=0,min=0)
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        try:
            obj=_preview(context,opts.controller,self.index)
            opts.message='当前活动Key已明确选择：'+obj.name+'；不要改Basis或其它Key'
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


def _preview_drafts(controller):
    """Resolve drafts by controller pointers, never by mutable names."""
    state = None
    if '_revision_preview' in controller:
        try:
            state = json.loads(controller['_revision_preview'])
        except _ERRORS:
            state = None
    names = state.get('draft_names') if isinstance(state, dict) else None
    count = len(names) if isinstance(names, list) else None
    if count is None:
        count = 0
        while count < 1024 and ('draft_%03d' % count) in controller:
            count += 1
    drafts, errors = [], []
    for index in range(count):
        pointer = 'draft_%03d' % index
        obj = controller.get(pointer) if pointer in controller else None
        if isinstance(obj, bpy.types.Object):
            drafts.append(obj)
        else:
            errors.append('草稿指针缺失或已失效：' + pointer)
    return drafts, errors


def _clear_preview_drafts(controller):
    """Clear the preview-only Key values without commit identity checks."""
    drafts, errors = _preview_drafts(controller)
    for obj in drafts:
        try:
            obj.show_only_shape_key = False
            keys = obj.data.shape_keys if isinstance(obj.data, bpy.types.Mesh) else None
            if keys is not None:
                for key in keys.key_blocks:
                    if key != keys.reference_key:
                        key.value = 0.0
        except Exception as error:
            errors.append(getattr(obj, 'name', '<draft>') + ': ' + str(error))
    return errors


class C94FKR_OT_StopPreview(bpy.types.Operator):
    bl_idname='c94_workflow.face_key_revision_stop_preview'
    bl_label='结束草稿预览（保留修正）'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        errors=[]
        try:
            if context.mode!='OBJECT':raise ValueError('请先回对象模式')
            if opts.controller is None:raise ValueError('没有可结束的修正草稿预览')
            errors.extend(_clear_preview_drafts(opts.controller))
        except _ERRORS as exc:
            errors.append(str(exc))
        finally:
            # Visibility restoration must run even when draft identity/name
            # validation or Key clearing fails.
            if opts.controller is not None:
                try:_restore_preview(opts.controller)
                except _ERRORS as exc:errors.append('预览显隐恢复：'+str(exc))
        if errors:
            self.report({'ERROR'},'; '.join(errors));return {'CANCELLED'}
        return {'FINISHED'}


class C94FKR_OT_Commit(bpy.types.Operator):
    bl_idname='c94_workflow.face_key_revision_commit'
    bl_label='结束预览并提交新的受管候选'
    bl_description='只新建修正候选，保源/草稿；沿用原控制骨与原生动画'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS);before=[]
        try:
            if context.mode!='OBJECT':raise ValueError('请先结束编辑/雕刻模式')
            if face_focus.status(context).get('active'):face_focus.leave(context)
            _,_,_,_,sources,drafts=revision._load(opts.controller)
            before=[(o,o.show_only_shape_key,[k.value for k in o.data.shape_keys.key_blocks]) for o in drafts]
            for obj in drafts:
                obj.show_only_shape_key=False
                for key in obj.data.shape_keys.key_blocks:
                    if key!=obj.data.shape_keys.reference_key:key.value=0
            hub,members=revision.commit(opts.controller,name='修正表情控制_'+uuid.uuid4().hex[:8])
        except _ERRORS as exc:
            # Commit handles its own IDs; restore the preview values on refusal.
            for obj,only,values in before:
                obj.show_only_shape_key=only
                for key,value in zip(obj.data.shape_keys.key_blocks,values):key.value=value
            message=str(exc)
            if opts.controller is not None:
                try:_restore_preview(opts.controller)
                except _ERRORS as recovery:message+='；预览显隐恢复未完成，可点结束预览重试：'+str(recovery)
            self.report({'ERROR'},message);return {'CANCELLED'}
        # Data commit is complete; a display failure must not masquerade as rollback.
        getattr(context.scene,controls.OPTIONS).hub=hub
        warnings=[]
        try:_restore_preview(opts.controller)
        except _ERRORS as exc:warnings.append(str(exc))
        try:
            for obj in sources:obj.hide_set(True)
            for obj in members:obj.hide_set(False)
        except _ERRORS as exc:warnings.append(str(exc))
        opts.message='已生成新的独立受管候选；源保留，控制骨/动画沿用'
        if warnings:self.report({'WARNING'},opts.message+'；显隐需检查：'+'；'.join(warnings))
        return {'FINISHED'}


class C94FKR_PT_Revision(bpy.types.Panel):
    bl_idname='C94FKR_PT_revision';bl_label='角色Key修正（保留原版）'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='面部';bl_order=4
    bl_options={'DEFAULT_CLOSED'}
    def draw(self,context):
        l=self.layout;opts=getattr(context.scene,OPTIONS);current=getattr(context.scene,controls.OPTIONS)
        l.prop_search(opts,'channel',current,'channels');l.operator('c94_workflow.face_key_revision_prepare')
        l.prop(opts,'controller')
        if opts.controller:
            try:
                state=revision.inspect(opts.controller);l.label(text='状态：'+state['status'])
                l.label(text='允许修正：'+'、'.join(state['channels']))
                if state['status']=='draft':
                    for i,name in enumerate(state['members']):l.operator('c94_workflow.face_key_revision_preview',text='编辑 '+name).index=i
                    l.operator('c94_workflow.face_key_revision_stop_preview');l.operator('c94_workflow.face_key_revision_commit')
                else:l.label(text='已提交：'+str(state['committed_hub']))
            except _ERRORS as exc:l.label(text=str(exc))
        if opts.message:l.label(text=opts.message)
        l.label(text='只编辑目标Key坐标；Basis/UV/权重保持')
        l.label(text='草稿可保存重开；提交前回到准备帧与KEY中立')


_CLASSES=(C94FKR_Options,C94FKR_OT_Prepare,C94FKR_OT_Preview,C94FKR_OT_StopPreview,C94FKR_OT_Commit,C94FKR_PT_Revision)
def register():
    global _C94_OPTIONS_OWNED
    if _REGISTERED:return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('该面板属性已由其他实例持有；原属性保持')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94FKR_Options))
        _C94_OPTIONS_OWNED = True
    except Exception:unregister();raise
def unregister():
    if hasattr(bpy.types.Scene,OPTIONS):_release_workspace_options()
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()


_C94_OPTIONS_OWNED = False

def _release_workspace_options():
    global _C94_OPTIONS_OWNED
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if (_C94_OPTIONS_OWNED and prop and prop.type == 'POINTER'
            and prop.fixed_type == C94FKR_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
