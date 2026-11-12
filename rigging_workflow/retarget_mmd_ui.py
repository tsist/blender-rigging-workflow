# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene MMD profile -> shared body/face/gaze candidate operators.

Source validation, clip binding and conversion remain in the source backend;
Action preview/restoration and save handlers remain in retarget_action/UI.
"""
import json,time
from pathlib import Path
import bpy
from bpy.props import PointerProperty,StringProperty,EnumProperty,IntProperty,BoolProperty,FloatProperty
from . import retarget_action as actions
from . import retarget_mmd_face as face,retarget_mmd_gaze as gaze
from . import face_mouth_core as mouth,face_expression_core as face_core
from .retarget_mmd_morph import morph_tracks_from_profile,_read_validated_morph_tracks

OPTIONS='c94_retarget_mmd_options_v1'
_REGISTERED=[]
_OWNED=False

def _require(value,message):
    if not value:raise ValueError(message)

def _armature(self,obj):return obj.type=='ARMATURE'
def _hub(self,obj):return obj.type=='EMPTY'
def _options(context):return getattr(context.scene,OPTIONS)
def _backend():
    from . import mmd_source_backend
    return mmd_source_backend

def _json(raw,label):
    _require(isinstance(raw,str) and len(raw)<=16*1024*1024,label+'为空或超过16MiB')
    try:return json.loads(raw)
    except (ValueError,TypeError) as exc:raise ValueError(label+'不是有效JSON') from exc

def _profile(opts):
    _require(opts.profile_text is not None,'先读取已登记来源的profile JSON')
    return _json(opts.profile_text.as_string(),'来源profile')

def _resolve(opts,*,check_mapping=True):
    backend=_backend();profile=_profile(opts)
    resolved=backend.validate_persisted_source(profile,scene=bpy.context.scene,expected_rig=opts.source,
        require_scene_config=not opts.source_is_appended,require_all_actions=True)
    _require(opts.source==resolved['rig'],'请选择profile记录的实际源骨架')
    ctx=backend.clip_timebase(profile,opts.clip_id,source_fps=opts.source_fps)
    # The actual source/profile was validated immediately above; reuse that
    # invariant without a second Scene-scope decision in another wrapper.
    mapping,source_profile=backend.mapping_from_profile(opts.source,profile,validate=False)
    if check_mapping:
        _require(_json(opts.mapping_json,'显式身体映射')==mapping['bones'],'身体映射与已核来源profile不同；重新检查映射，不猜测改配')
    return backend,profile,resolved,ctx,mapping,source_profile

class C94MMD_Options(bpy.types.PropertyGroup):
    source:PointerProperty(name='已登记MMD源骨架',type=bpy.types.Object,poll=_armature)
    source_is_appended:BoolProperty(name='使用已精确追加的登记来源',description='当前Scene不是来源登记Scene；仍严格核对profile、真实对象UID、完整Actions/Slots与骨架图',default=False)
    target:PointerProperty(name='C94目标骨架',type=bpy.types.Object,poll=_armature)
    profile_path:StringProperty(name='来源profile JSON',subtype='FILE_PATH')
    profile_text:PointerProperty(name='持久来源profile',type=bpy.types.Text)
    clip_id:StringProperty(name='来源片段ID',default='dance')
    clip_choices:StringProperty(name='已登记片段',default='')
    source_fps:FloatProperty(name='来源fps（与登记时基核对）',default=30.,min=.01,max=1000.)
    mapping_json:StringProperty(name='已核身体语义映射',default='{}')
    start:FloatProperty(name='源起始帧',default=0.,min=0.)
    end:FloatProperty(name='源结束帧',default=30.,min=0.)
    output_start:IntProperty(name='目标起始帧',default=1)
    motion:EnumProperty(name='根运动',items=(('PRESERVE','保留移动与转向','按来源运动'),('IN_PLACE','原地，保留转向','移除水平移动')),default='PRESERVE')
    include_face:BoolProperty(name='合成已有表情/口型',default=False)
    face_hub:PointerProperty(name='实际已有MOUTH控制对象',type=bpy.types.Object,poll=_hub)
    face_mapping_json:StringProperty(name='显式表情映射JSON',default='[]')
    face_omitted_json:StringProperty(name='遗漏原因JSON',default='{}')
    include_gaze:BoolProperty(name='合成眼控机制（需另验眼球蒙皮）',default=False)
    gaze_source_kind:EnumProperty(name='视线证据类型',items=(('real_clip','真实来源动作','是否动态以实际方向采样为准'),('mechanism_fixture','明确人工机制fixture','不计为真实动态素材覆盖')),default='real_clip')
    candidate:PointerProperty(name='同一最终候选Action',type=bpy.types.Action)
    preview_after_build:BoolProperty(name='生成后预览',default=True)
    report_text:PointerProperty(name='本次候选报告',type=bpy.types.Text)
    summary:StringProperty(name='上次操作',default='场景入口：先读取已登记来源profile，再检查映射')



def _face_arguments(opts,profile,ctx):
    _require(opts.face_hub is not None,'请明确已有MOUTH控制对象')
    group,_,rig,_=mouth._read(opts.face_hub)
    _require(rig==opts.target,'所选表情Hub不属于实际目标骨架')
    face_core._baseline(rig,group['neutral_fingerprint'])
    mapping=_json(opts.face_mapping_json,'表情映射');omitted=_json(opts.face_omitted_json,'遗漏原因')
    _require(isinstance(omitted,dict) and all(isinstance(k,str) and isinstance(v,str) for k,v in omitted.items()),'遗漏原因须为名称→原因的JSON对象')
    tracks,metadata=_read_validated_morph_tracks(profile,ctx)
    # Use the existing face boundary rather than reconstructing its validators.
    face._mapping(mapping,face._tracks(tracks),{r['name'] for r in group['channels']})
    return tracks,dict(mapping=mapping,source_metadata=metadata,omitted_reasons=omitted,
                       source_fps=ctx['fps'],source_range=(opts.start,opts.end),
                       output_start=opts.output_start)

def _convert(opts,context):
    _require(opts.target is not None and opts.source!=opts.target,'请明确不同的MMD源骨架与C94目标')
    _require(not actions.has_preview(opts.target),'请先停止目标当前预览')
    target_state=actions.snapshot(opts.target)
    backend,profile,_,ctx,_,_=_resolve(opts)
    _require(ctx['clip']['body_convertible'] is True,'所选clip未登记为身体转换来源；可用公开morph读取函数单独映射表情')
    _require(ctx['clip']['range'][0]<=opts.start<opts.end<=ctx['clip']['range'][1],'源片段须位于登记clip帧域且时长为正')
    face_args=_face_arguments(opts,profile,ctx) if opts.include_face else None
    if opts.include_gaze:
        _require(callable(getattr(backend,'restore_selected_clip',None)),'来源后端缺少已冻结的片段恢复入口，暂不能合成视线')
    created=[];selection=None;began=time.perf_counter();wm=context.window_manager
    wm.progress_begin(0,3)
    try:
        if opts.include_gaze:
            selection=backend.select_clip(profile,opts.clip_id,source_fps=opts.source_fps,require_scene_config=not opts.source_is_appended)
        try:
            wm.progress_update(0)
            converted=backend.convert_from_profile(opts.source,opts.target,profile,clip_id=opts.clip_id,
                start=opts.start,end=opts.end,output_start=opts.output_start,motion=opts.motion,
                action_name=opts.target.name+'_MMD身体候选',require_scene_config=not opts.source_is_appended)['result']
            created.append(converted['action']);wm.progress_update(1)
            if face_args:
                converted=face.create_candidate(opts.face_hub,face_args[0],base_action=converted['action'],base_slot=converted['slot'],**face_args[1])
                created.append(converted['action'])
            wm.progress_update(2)
            if opts.include_gaze:
                converted=gaze.create_candidate(opts.source,opts.target,base_action=converted['action'],source_fps=opts.source_fps,
                    start=opts.start,end=opts.end,output_start=opts.output_start,source_kind=opts.gaze_source_kind)
                created.append(converted['action'])
            wm.progress_update(3)
        finally:
            try:
                if selection is not None:backend.restore_selected_clip(selection)
            finally:
                # The source scope restores Scene time before its raw inputs.
                # Restore target raw inputs last, without re-applying time.
                actions.restore(opts.target,target_state,set_scene_time=False)
        from . import retarget_contact_detection_ui as detection_ui
        detection_ui.complete_from_context(context,opts.source,opts.target,converted)
        action=converted['action'];report=json.loads(action[actions.META])
        text=bpy.data.texts.new('MMD重定向报告_'+report['id'][:8]);text.write(json.dumps({
            'shared':report,'face':json.loads(action[face.META]) if face.META in action else None,
            'gaze':json.loads(action[gaze.META]) if gaze.META in action else None,
            'source_clip_missing':ctx['clip'].get('missing'),
            'stages_retained':[a.name for a in created],'synchronous_seconds':time.perf_counter()-began,
            'eye_skin_status':'not_certified; current P6 eyeball does not consume gaze; original weights retained' if opts.include_gaze else 'not_requested'},ensure_ascii=False,indent=2))
        opts.candidate=action;opts.report_text=text
        if opts.preview_after_build:actions.begin_preview(opts.target,action,slot=converted['slot'])
        opts.summary='候选'+('预览中' if opts.preview_after_build else '已生成')+'；同步耗时 %.2fs'%(time.perf_counter()-began)+('；视线仅机制，蒙皮另验' if opts.include_gaze else '')
        return action
    except BaseException:
        # Only unpublished Actions created by this operation can be discarded.
        for action in reversed(created):
            if action!=opts.candidate and action.users==int(action.use_fake_user):bpy.data.actions.remove(action)
        raise
    finally:wm.progress_end()

class C94MMD_OT_scene(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_mmd_scene'
    bl_label='场景MMD重定向'
    bl_options={'REGISTER'}
    operation:EnumProperty(items=[(v,v,'') for v in ('LOAD_PROFILE','MAP','CONVERT','PREVIEW','STOP')])

    def execute(self,context):
        opts=_options(context)
        try:
            if self.operation=='LOAD_PROFILE':
                path=Path(bpy.path.abspath(opts.profile_path)).resolve(strict=True)
                _require(path.is_file() and path.stat().st_size<=16*1024*1024,'profile须为不大于16MiB的明确JSON文件')
                raw=path.read_text(encoding='utf-8-sig');profile=_json(raw,'来源profile')
                resolved=_backend().validate_persisted_source(profile,scene=context.scene,expected_rig=opts.source,
                    require_scene_config=not opts.source_is_appended,require_all_actions=True)
                text=bpy.data.texts.new('MMD来源profile_'+profile['scene_config']['source_uid'][:8]);text.write(raw)
                opts.profile_text=text;opts.source=resolved['rig'];opts.clip_id=profile['primary_clip']
                opts.source_fps=_backend().clip_timebase(profile,opts.clip_id)['fps']
                _,profile,_,ctx,mapping,_=_resolve(opts,check_mapping=False)
                opts.clip_choices=' / '.join(c['id'] for c in profile['clips'])
                opts.source_fps=ctx['fps'];opts.mapping_json=json.dumps(mapping['bones'],ensure_ascii=False)
                opts.summary='已读取明确profile及真实源；检查片段ID、源帧域与目标骨架'
            elif self.operation=='MAP':
                _,profile,_,ctx,mapping,_=_resolve(opts,check_mapping=False)
                opts.clip_choices=' / '.join(c['id'] for c in profile['clips'])
                opts.mapping_json=json.dumps(mapping['bones'],ensure_ascii=False)
                opts.summary='映射已核对：%d项；源范围%s，%.3ffps'%(len(mapping['bones']),ctx['clip']['range'],ctx['fps'])
            elif self.operation=='CONVERT':_convert(opts,context)
            elif self.operation=='PREVIEW':
                _require(opts.candidate is not None,'请先生成或选择候选')
                actions.begin_preview(opts.target,opts.candidate);opts.summary='同一最终候选预览中；可停止恢复'
            else:
                actions.stop_scene_preview(context.scene);opts.summary='原Action、输入和辅助动态开关已恢复；候选保留'
            return {'FINISHED'}
        except Exception as exc:
            opts.summary=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94MMD_PT_scene(bpy.types.Panel):
    bl_idname='C94MMD_PT_scene';bl_label='MMD · 场景来源与同一候选'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向'

    def draw(self,context):
        opts=_options(context);layout=self.layout
        workspace=getattr(context.scene,'c94_workspace_options_v1',None)
        advanced=workspace is None or workspace.level!='DAILY'
        layout.label(text='先从文件入口导入，或选择当前场景的已登记来源')
        layout.prop(opts,'source');layout.prop(opts,'source_is_appended');layout.prop(opts,'target');layout.prop(opts,'profile_path')
        layout.operator(C94MMD_OT_scene.bl_idname,text='读取已登记来源profile').operation='LOAD_PROFILE'
        layout.prop(opts,'profile_text');layout.label(text='已登记：'+opts.clip_choices[:72]);layout.prop(opts,'clip_id');layout.prop(opts,'source_fps')
        layout.operator(C94MMD_OT_scene.bl_idname,text='核对持久映射与时基').operation='MAP'
        if advanced:layout.prop(opts,'mapping_json')
        row=layout.row(align=True);row.prop(opts,'start');row.prop(opts,'end')
        layout.prop(opts,'output_start');layout.prop(opts,'motion');layout.prop(opts,'include_face')
        if opts.include_face:
            layout.prop(opts,'face_hub')
            if advanced:
                layout.prop(opts,'face_mapping_json');layout.prop(opts,'face_omitted_json')
            else:layout.label(text='首次表情映射请在进阶中配置')
            layout.label(text='读取所选clip的已有原生morph Action')
            layout.label(text='只映射已有通道；近似与遗漏写入报告')
        layout.prop(opts,'include_gaze')
        if opts.include_gaze:
            if advanced:layout.prop(opts,'gaze_source_kind')
            layout.label(text='眼控输出需单独检查眼球实际变形')
        layout.prop(opts,'preview_after_build')
        layout.operator(C94MMD_OT_scene.bl_idname,text='同步生成MMD片段候选').operation='CONVERT'
        layout.prop(opts,'candidate')
        from . import retarget_headneck_ui
        retarget_headneck_ui.draw_button(layout,opts.target,opts.candidate)
        row=layout.row(align=True)
        row.operator(C94MMD_OT_scene.bl_idname,text='预览同一候选').operation='PREVIEW'
        row.operator(C94MMD_OT_scene.bl_idname,text='停止并恢复').operation='STOP'
        if advanced:layout.prop(opts,'report_text')
        layout.label(text='临时预览：'+('进行中' if actions.has_preview(opts.target) else '未开始'))
        layout.label(text='上次：'+opts.summary[:62])
        layout.label(text='短片段可同步预览；长转换请使用MMD后台队列')
        layout.label(text='保存前停止临时预览，独立候选保留')

_CLASSES=(C94MMD_Options,C94MMD_OT_scene,C94MMD_PT_scene)

def register():
    global _OWNED
    from . import retarget_ui
    _require(hasattr(bpy.types.Scene,retarget_ui.OPTIONS),'请先注册共享重定向入口及其保存/换文件handler')
    _require(not hasattr(bpy.types.Scene,OPTIONS),'场景MMD入口已由其他实例注册，原实例保持')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94MMD_Options));_OWNED=True
    except Exception:unregister();raise

def unregister():
    global _OWNED
    if _OWNED:
        actions.finish_previews()
        if hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _OWNED=False
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
