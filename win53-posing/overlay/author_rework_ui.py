# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit live author-rework planning. Disk impact JSON is not commit authority."""
import json
import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty, StringProperty

OPTIONS='c94_author_rework_options_v1'
_REGISTERED=[]
_PROPERTY_OWNED=False
_PLAN=None


@persistent
def _load_pre(_unused):
    global _PLAN
    _PLAN=None


def _armature(_self,obj):return obj.type=='ARMATURE'


def _changed(options,_context):
    global _PLAN
    _PLAN=None
    options.summary='输入已改变，请重新准备返工影响计划'
    options.impact=''


class C94REWORK_Options(bpy.types.PropertyGroup):
    source_rig:PointerProperty(name='旧作者骨架',type=bpy.types.Object,poll=_armature,update=_changed)
    old_reference:PointerProperty(name='旧原参考',type=bpy.types.Object,poll=_armature,update=_changed)
    reference:PointerProperty(name='新参考',type=bpy.types.Object,poll=_armature,update=_changed)
    template_file:StringProperty(name='原模板库',subtype='FILE_PATH',update=_changed)
    label:StringProperty(name='独立新角色名',default='作者返工候选',update=_changed)
    objects_json:StringProperty(options={'HIDDEN'},update=_changed)
    candidate_id:StringProperty(name='保存的返工候选 ID')
    impact:StringProperty(options={'SKIP_SAVE'})
    summary:StringProperty(default='捕获明确的生产Mesh/Key成员，再准备影响计划',options={'SKIP_SAVE'})


class C94REWORK_OT_Action(bpy.types.Operator):
    bl_idname='c94_workflow.author_rework'
    bl_label='作者返工'
    action:StringProperty(options={'HIDDEN'})
    @classmethod
    def poll(cls,context):return context.mode=='OBJECT'
    def execute(self,context):
        global _PLAN
        options=getattr(context.scene,OPTIONS)
        try:
            from . import author_rework as core,module_configuration
            if self.action=='CAPTURE':
                objects=[o for o in context.selected_objects if o.type=='MESH']
                if not objects:raise ValueError('明确选择要迁移的生产Mesh/Key成员')
                options.objects_json=json.dumps([{'name':o.name,'uid':o.session_uid} for o in objects],ensure_ascii=False)
                options.summary='已捕获%d个明确成员；一个P1组的皮肤/附件须全部纳入'%len(objects)
            elif self.action=='PREPARE':
                _PLAN=None
                options.impact=''
                rows=json.loads(options.objects_json or '[]');objects=[]
                for row in rows:
                    obj=context.scene.objects.get(row['name'])
                    if not obj or obj.session_uid!=row['uid']:raise ValueError('捕获成员已改变，重新捕获明确对象')
                    objects.append(obj)
                configuration=module_configuration.read(context.scene)
                if configuration is None:raise ValueError('先针对新参考准备实际模块配置库')
                _PLAN=core.prepare(context.scene,options.source_rig,objects,options.reference,options.template_file,
                    configuration,label=options.label,old_reference=options.old_reference)
                options.impact=json.dumps(_PLAN.preview(),ensure_ascii=False)
                options.summary='影响计划已准备；候选效果待评估，原作者成果保留'
            elif self.action=='EXECUTE':
                raise ValueError('旧整步返工入口已停用；先生成候选骨架，保存并审核范围后再采用')
            elif self.action=='PREPARE_CANDIDATE':
                if _PLAN is None or _PLAN.scene != context.scene:
                    raise ValueError('先准备当前场景的影响计划')
                current = module_configuration.read(context.scene)
                if current is None or current['id'] != _PLAN.configuration['id']:
                    raise ValueError('当前模块配置选择已改变，先重新准备影响计划')
                output = _PLAN.prepare_candidate()
                options.candidate_id = output['candidate_id']
                options.impact = json.dumps(output['report'], ensure_ascii=False)
                options.summary = '已生成无权重最终候选；保存后按实际迁权范围双审'
                _PLAN = None
            elif self.action=='INSPECT_CANDIDATE':
                from . import candidate_state
                _, rig, _, scope, payload, _, _ = core.inspect_candidate(context.scene, options.candidate_id)
                options.impact = json.dumps(payload['report'], ensure_ascii=False)
                options.summary = '待审 ' + rig.name + ' · ' + candidate_state.scope_digest(scope)
            elif self.action=='APPLY_CANDIDATE':
                output = core.apply_candidate(context.scene, options.candidate_id)
                options.impact = json.dumps(output['report'], ensure_ascii=False)
                options.summary = '已创建独立返工副本：' + output['rig'].name
            elif self.action=='CANCEL':
                if _PLAN is not None:
                    if _PLAN.scene!=context.scene:
                        raise ValueError('请切回返工计划所在场景')
                    options.impact=''
                    _PLAN=None
                options.summary='已结束计划；原源与已生成候选保留'
            else:raise ValueError('未知返工操作')
            self.report({'INFO'},options.summary);return {'FINISHED'}
        except Exception as exc:
            if self.action in {'PREPARE', 'PREPARE_CANDIDATE'}:
                _PLAN = None
                options.impact = ''
            options.summary='操作未完成：'+str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94REWORK_PT_Rework(bpy.types.Panel):
    bl_label='已有C94作者返工'
    bl_idname='C94REWORK_PT_rework'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='角色绑定';bl_order=9
    def draw(self,context):
        layout=self.layout;options=getattr(context.scene,OPTIONS)
        for field in ('source_rig','old_reference','reference','template_file','label'):layout.prop(options,field)
        layout.operator('c94_workflow.author_rework',text='捕获所选生产Mesh/Key成员').action='CAPTURE'
        layout.operator('c94_workflow.author_rework',text='准备并查看返工影响').action='PREPARE'
        if options.impact:
            try:
                impact=json.loads(options.impact);box=layout.box()
                box.label(text='参考变化：%d骨；关闭形变模块：%d骨'%(len(impact['changed_reference_bones']),len(impact['closed_deform_semantics'])))
                box.label(text='原Key库独立保留；变空间动作需重定向')
                box.label(text='受影响表情需重采；动态需重烘')
                for name in impact['changed_reference_bones'][:8]:box.label(text='参考：'+name)
                if impact.get('action'):box.label(text='Action：'+impact['action']['status'])
                if impact.get('face_contract'):box.label(text='表情：'+impact['face_contract'])
            except (ValueError,KeyError,TypeError):layout.label(text='影响摘要无效，请重新准备',icon='ERROR')
        row=layout.row();row.enabled=_PLAN is not None and _PLAN.scene==context.scene
        row.operator('c94_workflow.author_rework',text='生成无权重审核骨架').action='PREPARE_CANDIDATE'
        layout.prop(options, 'candidate_id')
        layout.operator('c94_workflow.author_rework', text='核对候选审核范围').action = 'INSPECT_CANDIDATE'
        layout.operator('c94_workflow.author_rework', text='按双审范围创建迁权副本').action = 'APPLY_CANDIDATE'
        layout.operator('c94_workflow.author_rework',text='结束本次计划').action='CANCEL'
        layout.label(text=options.summary)
        layout.label(text='原源不覆盖；本功能不承诺任意旧动作无损')


_CLASSES=(C94REWORK_Options,C94REWORK_OT_Action,C94REWORK_PT_Rework)
def register():
    global _PROPERTY_OWNED
    if _REGISTERED:return
    if hasattr(bpy.types.Scene,OPTIONS):raise RuntimeError('作者返工选项已由其它实例注册')
    for base,identity in ((bpy.types.PropertyGroup,C94REWORK_Options.__name__),
                          (bpy.types.Operator,'C94_WORKFLOW_OT_author_rework'),
                          (bpy.types.Panel,C94REWORK_PT_Rework.bl_idname)):
        if base.bl_rna_get_subclass_py(identity,None) is not None:raise RuntimeError('作者返工类型已由其它实例注册')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94REWORK_Options));_PROPERTY_OWNED=True
        if _load_pre not in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.append(_load_pre)
    except Exception:unregister();raise
def unregister():
    global _PROPERTY_OWNED,_PLAN
    _PLAN=None
    if _load_pre in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(_load_pre)
    if _PROPERTY_OWNED and hasattr(bpy.types.Scene,OPTIONS):
        prop=bpy.types.Scene.bl_rna.properties.get(OPTIONS)
        if prop and prop.fixed_type==C94REWORK_Options.bl_rna:delattr(bpy.types.Scene,OPTIONS)
    _PROPERTY_OWNED=False
    for cls in reversed(_REGISTERED):
        if cls.is_registered:bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
