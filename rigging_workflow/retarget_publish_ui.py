# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit base-candidate publication UI; no correction approval is inferred."""
import json
import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty,EnumProperty,PointerProperty,StringProperty
from . import retarget_ui as body,retarget_mmd_ui as mmd,retarget_action as rt,retarget_publish as publish

OPTIONS='c94_retarget_publish_options_v1'
CORRECTION_META='c94_retarget_contact_correction_v1'
_REGISTERED=[]
_OWNED=False

def _require(value,message):
    if not value:raise ValueError(message)

def _body(context):return getattr(context.scene,body.OPTIONS)
def _opts(context):return getattr(context.scene,OPTIONS)

def _current(context):
    kind=_opts(context).input_kind
    if kind=='PROFILE':
        from . import retarget_profile_ui
        key=retarget_profile_ui.OPTIONS
    else:
        key=mmd.OPTIONS if kind=='MMD' else body.OPTIONS
    _require(hasattr(context.scene,key),'请先注册对应的重定向面板')
    return getattr(context.scene,key)

def _review_changed(self,context):
    if not self.reviewed:
        self.reviewed_action=None;self.reviewed_target=None;self.reviewed_report='';self.reviewed_uid=''
        return
    current=_current(context)
    self.reviewed_action=current.candidate;self.reviewed_target=current.target
    self.reviewed_report=current.candidate.get(rt.META,'') if current.candidate else ''
    self.reviewed_uid=current.target.get(rt.TARGET_UID,'') if current.target else ''

def _mode_changed(self,_context):self.reviewed=False

@persistent
def _clear_reviews(*_args):
    for scene in bpy.data.scenes:
        if hasattr(scene,OPTIONS):getattr(scene,OPTIONS).reviewed=False

def _clear_reviews_after_enable():
    # Native addon enable calls register inside RestrictBlend. The first
    # timer tick runs after Main is restored; real cleanup errors propagate.
    if _OWNED:_clear_reviews()
    return None

def _initialize_reviews():
    if hasattr(bpy.data,'scenes'):
        _clear_reviews()
    else:
        bpy.app.timers.register(_clear_reviews_after_enable,first_interval=0.0)

def _base_candidate(action):
    _require(isinstance(action,bpy.types.Action),'请先选择已复核的基础候选Action')
    # The separate correction confirmation kernel owns issue IDs. This UI
    # never fills them from detected issues or a general review checkbox.
    _require(CORRECTION_META not in action,
             '这是接触修正候选，请使用修正专项确认入口明确问题项；基础绑定入口不代确认')
    report=json.loads(action.get(rt.META,'{}'))
    _require(not report.get('contact_correction') and not report.get('correction'),
             '修正候选须走专项确认入口，基础复核勾选不能替代问题项批准')
    return report

class C94RETARGETPUB_Options(bpy.types.PropertyGroup):
    input_kind:EnumProperty(name='候选来源',items=(
        ('BODY','身体 / 通用','使用身体面板选定的目标与候选Action'),
        ('MMD','MMD','使用MMD面板选定的目标与候选Action'),
        ('PROFILE','通用Profile','使用通用Profile面板选定的目标与候选Action')),default='BODY',update=_mode_changed)
    mode:EnumProperty(name='正式使用方式',items=(
        ('ACTION','持久绑定Action','保存与重开后仍使用此基础动作'),
        ('NLA','可选：追加NLA','新建独有REPLACE轨道；原未静音/独奏轨道会拒绝')),default='ACTION',update=_mode_changed)
    reviewed:BoolProperty(name='我已检查此基础候选和已知限制',default=False,update=_review_changed,options={'SKIP_SAVE'})
    isolate_nla:BoolProperty(name='明确隔离当前正在求值的原NLA',description='仅Action模式：use_nla关闭，原track mute保持；撤回恢复',default=False)
    reviewed_action:PointerProperty(type=bpy.types.Action,options={'SKIP_SAVE'})
    reviewed_target:PointerProperty(type=bpy.types.Object,options={'SKIP_SAVE'})
    reviewed_report:StringProperty(options={'SKIP_SAVE'})
    reviewed_uid:StringProperty(options={'SKIP_SAVE'})
    message:StringProperty(name='状态',default='先复核基础候选，再明确正式使用方式')
    receipt_text:StringProperty(name='本次回执Text',default='')

class C94RETARGETPUB_OT_action(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_publish'
    bl_label='正式使用基础候选'
    operation:EnumProperty(items=[(key,key,'') for key in ('COMMIT','INSPECT','WITHDRAW','STOP_PREVIEW')])
    def execute(self,context):
        opts=_opts(context)
        try:
            if self.operation!='INSPECT':
                _require(context.mode in ('OBJECT','POSE'),'请退出编辑模式后正式绑定或撤回')
            if self.operation=='STOP_PREVIEW':
                target=rt.preview_target(context.scene)
            else:
                current=_current(context);target=current.target
            _require(isinstance(target,bpy.types.Object) and target.type=='ARMATURE','请先选择目标角色骨架')
            if self.operation=='COMMIT':
                action=current.candidate;_base_candidate(action)
                _require(opts.reviewed and opts.reviewed_action==action and opts.reviewed_target==target
                    and opts.reviewed_report==action.get(rt.META,'') and opts.reviewed_uid==target.get(rt.TARGET_UID,''),
                    '目标或候选已改变，请重新勾选对此候选的明确复核')
                result=publish.commit(target,action,mode=opts.mode,reviewed=True,isolate_nla=opts.isolate_nla)
                record=result['receipt'];opts.receipt_text=record['commit_receipt']
                opts.message=('已持久绑定基础Action' if opts.mode=='ACTION' else '已追加基础NLA，旧Base未叠入')+'；可检查回执或撤回'
                opts.reviewed=False
            elif self.operation=='INSPECT':
                record=publish.inspect(target)
                _require(record is not None,'当前目标没有本入口持久绑定回执')
                opts.receipt_text=record['commit_receipt']
                opts.message=f"{record['status']} / {record['mode']} / {record['candidate']}；回执仅记录，不表示当前状态仍有效"
            elif self.operation=='WITHDRAW':
                result=publish.withdraw(target);opts.receipt_text=result['receipt_text']
                opts.message='已撤回正式使用，恢复旧动作、输入与动态开关；候选保留';opts.reviewed=False
            else:
                rt.stop_preview(target)
                opts.message='已通过原预览共核停止并恢复临时预览';opts.reviewed=False
            return {'FINISHED'}
        except Exception as exc:
            opts.message=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94RETARGETPUB_PT_panel(bpy.types.Panel):
    bl_idname='C94RETARGETPUB_PT_panel'
    bl_label='正式使用基础候选'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向'
    bl_parent_id='C94RETARGET_PT_body'
    def draw(self,context):
        layout=self.layout;opts=_opts(context)
        if not hasattr(context.scene, 'c94_workspace_options_v1'):
            layout.prop(opts,'input_kind')
        # This resolver raises when the Scene has no unique preview; it is
        # not a nullable query. Drawing an idle publication panel is normal.
        try:scene_preview=rt.preview_target(context.scene)
        except ValueError:scene_preview=None
        if scene_preview is not None:
            layout.operator('c94_workflow.retarget_publish',text='停止临时预览并恢复').operation='STOP_PREVIEW'
        try:current=_current(context)
        except ValueError as exc:
            layout.label(text=str(exc),icon='INFO');return
        layout.label(text='目标：'+(current.target.name if current.target else '未选择'))
        layout.label(text='候选：'+(current.candidate.name if current.candidate else '未选择'))
        layout.prop(opts,'mode')
        layout.prop(opts,'reviewed')
        if opts.mode=='ACTION':layout.prop(opts,'isolate_nla')
        else:
            layout.label(text='NLA须先明确隔离原未静音/独奏轨道',icon='INFO')
            layout.label(text='本入口不自动关闭原track，不叠入旧Base')
        row=layout.row(align=True);row.enabled=opts.reviewed
        row.operator('c94_workflow.retarget_publish',text='正式使用已复核基础候选').operation='COMMIT'
        row=layout.row(align=True)
        row.operator('c94_workflow.retarget_publish',text='检查绑定回执').operation='INSPECT'
        row.operator('c94_workflow.retarget_publish',text='撤回并恢复原状态').operation='WITHDRAW'
        layout.label(text=opts.message[:72],icon='INFO')
        if opts.receipt_text:layout.prop(opts,'receipt_text')
        layout.label(text='正式绑定会保存在文件；旧动作与候选保留')
        layout.label(text='旧辅助动态保留并暂关；新基础动作须重捕获')
        layout.label(text='接触修正候选须走专项问题项确认')

_CLASSES=(C94RETARGETPUB_Options,C94RETARGETPUB_OT_action,C94RETARGETPUB_PT_panel)

def register():
    global _OWNED
    _require(hasattr(bpy.types.Scene,body.OPTIONS),'请先注册身体重定向界面')
    _require(not hasattr(bpy.types.Scene,OPTIONS),'正式绑定界面已由其他实例注册，原实例保持')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94RETARGETPUB_Options));_OWNED=True
        for handlers in (bpy.app.handlers.load_post,bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):handlers.append(_clear_reviews)
        _initialize_reviews()
    except Exception:unregister();raise

def unregister():
    global _OWNED
    if bpy.app.timers.is_registered(_clear_reviews_after_enable):
        bpy.app.timers.unregister(_clear_reviews_after_enable)
    for handlers in (bpy.app.handlers.load_post,bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):
        if _clear_reviews in handlers:handlers.remove(_clear_reviews)
    if _OWNED and hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _OWNED=False
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
