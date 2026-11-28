# SPDX-License-Identifier: GPL-3.0-or-later
"""Daily explicit-group controls; native properties and isolated key recording."""
import bpy
from bpy.props import PointerProperty, CollectionProperty, StringProperty, IntProperty, BoolProperty, FloatProperty, FloatVectorProperty
from . import face_expression_controls as core, face_expression_layout as sidebar

OPTIONS='c94_face_expression_controls_options_v1'
_REGISTERED=[]
_ERRORS=(ValueError, RuntimeError, ReferenceError, KeyError, TypeError, AttributeError, IndexError)


class C94FEC_Member(bpy.types.PropertyGroup):
    source: PointerProperty(type=bpy.types.Object, poll=lambda self,obj:obj.type=='MESH')


class C94FEC_GridSlot(bpy.types.PropertyGroup):
    channel:StringProperty(name='口型语义')
    x:IntProperty()
    y:IntProperty()


class C94FEC_Section(bpy.types.PropertyGroup):
    expanded:BoolProperty(default=True)


def _mouth_axis_get(opts,axis):
    try:
        from . import face_mouth_pad
        return face_mouth_pad.inspect(opts.hub)['axes'][axis]
    except _ERRORS:return 0.


def _mouth_axis_set(opts,axis,value):
    try:
        from . import face_mouth_pad
        face_mouth_pad.set_axis(opts.hub,axis=axis,value=value);opts.message=''
    except _ERRORS as exc:opts.message=str(exc)


def _strength_get(row):
    try:return float(row.hub[row.prop_key])
    except (KeyError,TypeError,ReferenceError):return 0.


def _strength_set(row,value):
    try:
        core.set_values(row.hub,{row.channel:value})
        getattr(row.id_data,OPTIONS).message=''
    except _ERRORS as exc:getattr(row.id_data,OPTIONS).message=str(exc)


class C94FEC_Row(bpy.types.PropertyGroup):
    hub:PointerProperty(type=bpy.types.Object)
    channel:StringProperty()
    prop_key:StringProperty()
    strength:FloatProperty(name='强度',min=0.,max=1.,get=_strength_get,set=_strength_set)


def _refresh(opts,context):
    expanded={r.name:r.expanded for r in opts.sections}
    opts.sections.clear()
    opts.channels.clear()
    if opts.hub:
        try:
            for r in core.inspect(opts.hub)['channels']:
                row=opts.channels.add();row.name=r['name'];row.hub=opts.hub;row.channel=r['name'];row.prop_key=r['property']
            for name in dict.fromkeys(r['section'] for r in sidebar.read(opts.hub)['rows']):
                row=opts.sections.add();row.name=name;row.expanded=expanded.get(name,True)
            if hasattr(context.scene,'c94_face_pose_library_options_v1'):
                getattr(context.scene,'c94_face_pose_library_options_v1').rig=opts.hub['rig']
        except _ERRORS as exc:opts.message=str(exc)


class C94FEC_Options(bpy.types.PropertyGroup):
    rig: PointerProperty(name='角色骨架',type=bpy.types.Object,poll=lambda self,obj:obj.type=='ARMATURE')
    members: CollectionProperty(type=C94FEC_Member)
    hub: PointerProperty(name='表情同步控制',type=bpy.types.Object,poll=lambda self,obj:core.STATE_KEY in obj,update=_refresh)
    channels:CollectionProperty(type=C94FEC_Row)
    sections:CollectionProperty(type=C94FEC_Section)
    layout_editing:BoolProperty(name='编辑侧栏布局',default=False)
    font:PointerProperty(name='图形板标签字体',type=bpy.types.VectorFont)
    mouth_slots:CollectionProperty(type=C94FEC_GridSlot)
    mouth_x:FloatProperty(name='圆口 ↔ 宽口',min=-1.,max=1.,get=lambda self:_mouth_axis_get(self,'x'),set=lambda self,v:_mouth_axis_set(self,'x',v))
    mouth_y:FloatProperty(name='开口',min=0.,max=1.,get=lambda self:_mouth_axis_get(self,'y'),set=lambda self,v:_mouth_axis_set(self,'y',v))
    message:StringProperty(options={'SKIP_SAVE'})


class C94FEC_OT_SidebarLayout(bpy.types.Operator):
    bl_idname='c94_workflow.expression_sidebar_layout'
    bl_label='编辑表情侧栏布局'
    bl_options={'REGISTER','UNDO'}
    action:StringProperty(default='EDIT',options={'HIDDEN'})
    channel:StringProperty(options={'HIDDEN'})
    label:StringProperty(name='显示名称')
    section:StringProperty(name='分区')
    columns:IntProperty(name='列数',default=1,min=1,max=3)
    def invoke(self,context,event):
        if self.action!='EDIT':return self.execute(context)
        try:
            row=next(r for r in sidebar.read(getattr(context.scene,OPTIONS).hub)['rows'] if r['channel']==self.channel)
            self.label=row['label'];self.section=row['section']
            return context.window_manager.invoke_props_dialog(self)
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    def draw(self,context):self.layout.prop(self,'label');self.layout.prop(self,'section')
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        try:
            if self.action=='COLUMNS':sidebar.configure(opts.hub,columns=self.columns)
            else:
                rows=sidebar.read(opts.hub)['rows'];index=next(i for i,r in enumerate(rows) if r['channel']==self.channel)
                if self.action=='EDIT':sidebar.configure(opts.hub,channel=self.channel,label=self.label,section=self.section)
                elif self.action=='VISIBLE':sidebar.configure(opts.hub,channel=self.channel,visible=not rows[index]['visible'])
                elif self.action in {'UP','DOWN'}:sidebar.configure(opts.hub,channel=self.channel,position=max(0,min(len(rows)-1,index+(-1 if self.action=='UP' else 1))))
                else:raise ValueError('未知布局操作')
            _refresh(opts,context);return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_Focus(bpy.types.Operator):
    bl_idname='c94_workflow.face_focus'
    bl_label='面部检查视图'
    action:StringProperty(default='ENTER',options={'HIDDEN'})
    space:StringProperty(default='AUTO',options={'HIDDEN'})
    secondary:BoolProperty(name='显示次要骨',default=False)
    def execute(self,context):
        from . import face_focus
        try:
            if self.action=='ENTER':face_focus.enter(context,getattr(context.scene,OPTIONS).hub,space=self.space,secondary=self.secondary)
            elif self.action=='LEAVE':face_focus.leave(context)
            elif self.action=='SPACE':face_focus.set_space(context,space=self.space,secondary=self.secondary)
            else:raise ValueError('未知检查视图操作')
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_FitFrame(bpy.types.Operator):
    bl_idname='c94_workflow.mouth_fit_frame'
    bl_label='按控件布局适配板框'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        from . import face_mouth_pad
        try:face_mouth_pad.fit_frame(getattr(context.scene,OPTIONS).hub);return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_BoardArt(bpy.types.Operator):
    bl_idname='c94_workflow.expression_board_art'
    bl_label='添加示意脸和分区标题'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        try:
            from . import face_board_art
            face_board_art.install(opts.hub,font=opts.font)
            return {'FINISHED'}
        except _ERRORS as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_PrepareCapture(bpy.types.Operator):
    bl_idname='c94_workflow.expression_prepare_capture'
    bl_label='从本角色继续骨控采样'
    bl_description='填入本角色明确保存的原生采样源；不改变模式、姿态或已有形态键'
    def execute(self,context):
        from . import face_capture_ui,face_capture_group
        opts=getattr(context.scene,OPTIONS);target=getattr(context.scene,face_capture_ui.OPTIONS)
        try:
            try:face_capture_group.get_session(target.token)
            except ValueError:pass
            else:raise ValueError('已有捕获会话，请先结束；不改写其源列表')
            group,rig,_=core._read(opts.hub)
            sources=[opts.hub.get('author_source_%03d'%i) for i in range(group['member_count'])]
            if not all(isinstance(o,bpy.types.Object) and o.type=='MESH' for o in sources):raise ValueError('本组没有明确采样源，请在采样面板逐项选择')
            target.rig=rig;target.source=sources[0];target.attachments.clear()
            for obj in sources[1:]:target.attachments.add().source=obj
            target.message='已填明确原始源；在中立KEY态开始，切骨控后设计并采样'
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_MouthGrid(bpy.types.Operator):
    bl_idname='c94_workflow.mouth_grid_initialize'
    bl_label='设置二维口型映射'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        try:
            names={r['name'] for r in core.inspect(opts.hub)['channels']}
            opts.mouth_slots.clear()
            preferred={(-1,0):'元音-U',(1,0):'元音-I',(-1,1):'元音-O',(0,1):'中开口',(1,1):'元音-E',(-1,2):'大圆口',(0,2):'元音-A',(1,2):'宽大口'}
            for (x,y),name in preferred.items():
                row=opts.mouth_slots.add();row.x=x;row.y=y;row.channel=name if name in names else ''
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_MouthCreate(bpy.types.Operator):
    bl_idname='c94_workflow.mouth_pad_create'
    bl_label='建立二维口型板与独立滑杆'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        from . import face_mouth_pad
        opts=getattr(context.scene,OPTIONS)
        try:
            face_mouth_pad.create(opts.hub,[dict(name=r.channel,x=r.x,y=r.y) for r in opts.mouth_slots],font=opts.font,preserve_disjoint_nla=True)
            _refresh(opts,context);return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_MouthSet(bpy.types.Operator):
    bl_idname='c94_workflow.mouth_pad_set'
    bl_label='定位二维口型'
    bl_description='移动到采样格点；独立辅音及表情滑杆保持'
    bl_options={'REGISTER','UNDO'}
    x:FloatProperty(min=-1.,max=1.)
    y:FloatProperty(min=0.,max=1.)
    def execute(self,context):
        from . import face_mouth_pad
        try:face_mouth_pad.set_axes(getattr(context.scene,OPTIONS).hub,x=self.x,y=self.y);return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_Refresh(bpy.types.Operator):
    bl_idname='c94_workflow.expression_refresh'
    bl_label='刷新表情列表'
    def execute(self,context):_refresh(getattr(context.scene,OPTIONS),context);return {'FINISHED'}


class C94FEC_OT_Board(bpy.types.Operator):
    bl_idname='c94_workflow.expression_board_create'
    bl_label='建立三维滑杆板（保留动画）'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        from . import face_graph_board as board
        opts=getattr(context.scene,OPTIONS)
        try:board.create(opts.hub,font=opts.font);_refresh(opts,context);return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_SelectBoard(bpy.types.Operator):
    bl_idname='c94_workflow.expression_board_select'
    bl_label='选择图形板控骨'
    def execute(self,context):
        from . import face_graph_board as board
        try:
            hub=getattr(context.scene,OPTIONS).hub
            if 'c94_face_mouth_pad_v1' in hub:
                from . import face_mouth_pad
                _,record,rig,_=face_mouth_pad._read(hub);first_name=record['pad']
            else:
                _,record,rig,_=board._read(hub);first_name=record['channels'][0]['bone']
            if context.mode not in {'OBJECT','POSE'}:raise ValueError('先退出编辑/绘制模式')
            rig.hide_set(False);core.fm.panel._pose(context,rig)
            core.fm.set_visibility(rig,panel=True)
            for pb in rig.pose.bones:pb.select=False
            first=rig.pose.bones[first_name];first.select=True;first.hide=False;first.bone.hide=False;rig.data.bones.active=first.bone
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_Layout(bpy.types.Operator):
    bl_idname='c94_workflow.expression_board_layout'
    bl_label='调整图形板布局'
    bl_options={'REGISTER','UNDO'}
    index:IntProperty(default=-1)
    location:FloatVectorProperty(name='相对位置',size=3)
    scale:FloatVectorProperty(name='缩放',size=3,default=(1,1,1),min=.001)
    section:StringProperty(name='分区',default='表情')
    def invoke(self,context,event):
        from . import face_graph_board as board
        try:
            hub=getattr(context.scene,OPTIONS).hub
            if 'c94_face_mouth_pad_v1' in hub:
                from . import face_mouth_pad
                _,r,rig,_=face_mouth_pad._read(hub)
                name=r['root'] if self.index==-2 else r['anchor'] if self.index==-1 else r['sliders'][self.index]['anchor']
            else:
                _,r,rig,_=board._read(hub)
                name=r['root'] if self.index<0 else r['channels'][self.index]['anchor']
            self.location=rig.pose.bones[name].location;self.scale=rig.pose.bones[name].scale
            if self.index>=0 and 'channels' in r:self.section=r['channels'][self.index]['section']
            return context.window_manager.invoke_props_dialog(self)
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    def execute(self,context):
        from . import face_graph_board as board
        try:
            hub=getattr(context.scene,OPTIONS).hub
            if 'c94_face_mouth_pad_v1' in hub:
                from . import face_mouth_pad
                face_mouth_pad.set_layout(hub,index=None if self.index==-2 else self.index,location=tuple(self.location),scale=tuple(self.scale))
            else:board.set_layout(hub,index=None if self.index<0 else self.index,location=tuple(self.location),scale=tuple(self.scale),section=self.section if self.index>=0 else None)
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_Members(bpy.types.Operator):
    bl_idname='c94_workflow.expression_members'
    bl_label='添加所选表情网格'
    remove_index:IntProperty(default=-1)
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        if self.remove_index>=0:
            if self.remove_index<len(opts.members):opts.members.remove(self.remove_index)
        else:
            existing={r.source for r in opts.members}
            for obj in context.selected_objects:
                if obj.type=='MESH' and obj not in existing:opts.members.add().source=obj;existing.add(obj)
        return {'FINISHED'}


class C94FEC_OT_Create(bpy.types.Operator):
    bl_idname='c94_workflow.expression_controls_create'
    bl_label='建立同组表情控制'
    bl_description='给明确同组皮肤/附件创建独立控制对象和原生形态键驱动；所有表情先清零'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        opts=getattr(context.scene,OPTIONS)
        try:
            import uuid
            opts.hub=core.create(opts.rig,[r.source for r in opts.members],name='角色表情同步控制_'+uuid.uuid4().hex[:8])
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_Activate(bpy.types.Operator):
    bl_idname='c94_workflow.expression_activate'
    bl_label='单独使用该表情'
    bl_options={'REGISTER','UNDO'}
    expression:StringProperty()
    def execute(self,context):
        try:
            hub=getattr(context.scene,OPTIONS).hub;info=core.inspect(hub)
            core.set_values(hub,{r['name']:float(r['name']==self.expression) for r in info['channels']})
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_Record(bpy.types.Operator):
    bl_idname='c94_workflow.expression_record'
    bl_label='记录当前帧全部表情'
    bl_description='以新动作副本记录本控制对象当前帧；旧动作曲线保留，不自动补前帧'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            result=core.record_current(getattr(context.scene,OPTIONS).hub)
            self.report({'INFO'},'已记录帧 '+str(result['frame']))
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_OT_Switch(bpy.types.Operator):
    bl_idname='c94_workflow.expression_switch'
    bl_label='切换表情路线'
    bl_options={'REGISTER','UNDO'}
    target:StringProperty(default='KEY')
    reset:BoolProperty(default=False)
    def execute(self,context):
        try:
            core.switch(getattr(context.scene,OPTIONS).hub,target=self.target,reset=self.reset)
            return {'FINISHED'}
        except _ERRORS as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94FEC_PT_Controls(bpy.types.Panel):
    bl_idname='C94FEC_PT_controls'
    bl_label='角色表情同步控制'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='面部';bl_order=1
    def draw(self,context):
        layout=self.layout;opts=getattr(context.scene,OPTIONS)
        layout.prop(opts,'hub')
        if opts.hub:
            try:
                record,rig,members=core._read(opts.hub)
                from . import face_focus
                focus=face_focus.status(context)
                if focus.get('active'):
                    if not focus.get('layer_active',True):
                        layout.label(text='点击显示形态键板或骨控重新激活检查层')
                    line=layout.row(align=True)
                    line.operator('c94_workflow.face_focus',text='恢复原视图').action='LEAVE'
                    for space,label in [('KEY','显示Key板'),('BONE','显示骨控')]:
                        op=line.operator('c94_workflow.face_focus',text=label);op.action='SPACE';op.space=space
                else:
                    op=layout.operator('c94_workflow.face_focus',text='进入干净面部检查')
                    op.action='ENTER';op.space='AUTO';op.secondary=False
                mouth=None
                if 'c94_face_mouth_pad_v1' in opts.hub:
                    from . import face_mouth_pad
                    mouth=face_mouth_pad.inspect(opts.hub)
                layout.label(text='同步：'+'、'.join(o.name for o in members))
                edit=layout.column()
                try:core._baseline(rig,record['neutral_fingerprint'])
                except ValueError as exc:edit.enabled=False;layout.label(text=str(exc))
                declaration=sidebar.read(opts.hub);labels={r['channel']:r for r in declaration['rows']}
                if len(opts.channels)!=len(record['channels']) or not opts.sections:edit.operator('c94_workflow.expression_refresh')
                pad_names={r['name'] for r in mouth['grid']} if mouth else set()
                if mouth:
                    edit.prop(opts,'mouth_x',slider=True);edit.prop(opts,'mouth_y',slider=True)
                    for y in [2,1,0]:
                        line=edit.row(align=True)
                        for x in [-1,0,1]:
                            r=next((r for r in mouth['grid'] if r['x']==x and r['y']==y),None)
                            if r and not labels[r['name']]['visible']:line.label(text='');continue
                            op=line.operator('c94_workflow.mouth_pad_set',text=labels[r['name']]['label'] if r else '中立');op.x=x;op.y=y/2
                    edit.label(text='二维格点插值；独立滑杆叠加须另查')
                current_section=None;content=edit
                for declared in declaration['rows']:
                    if declared['channel'] in pad_names or not declared['visible']:continue
                    row=opts.channels.get(declared['channel'])
                    if row is None:continue
                    if declared['section']!=current_section:
                        current_section=declared['section'];box=edit.box();section=opts.sections.get(current_section)
                        if section:box.prop(section,'expanded',text=current_section,icon='TRIA_DOWN' if section.expanded else 'TRIA_RIGHT',emboss=False)
                        else:box.label(text=current_section)
                        content=box.grid_flow(columns=declaration['columns'],row_major=True,align=True)
                    section=opts.sections.get(current_section)
                    if section and not section.expanded:continue
                    line=content.row(align=True);line.prop(row,'strength',text=declared['label'],slider=True)
                    if not mouth:line.operator('c94_workflow.expression_activate',text='',icon='PLAY').expression=row.channel
                edit.operator('c94_workflow.expression_activate',text='清零全部表情').expression=''
                edit.operator('c94_workflow.expression_record')
                layout.prop(opts,'layout_editing')
                if opts.layout_editing:
                    line=layout.row(align=True)
                    for cols in [1,2,3]:
                        op=line.operator('c94_workflow.expression_sidebar_layout',text=str(cols)+'列',depress=declaration['columns']==cols);op.action='COLUMNS';op.columns=cols
                    for declared in declaration['rows']:
                        line=layout.row(align=True);line.label(text=declared['label'])
                        for action,icon in [('UP','TRIA_UP'),('DOWN','TRIA_DOWN'),('EDIT','GREASEPENCIL'),('VISIBLE','HIDE_OFF' if declared['visible'] else 'HIDE_ON')]:
                            op=line.operator('c94_workflow.expression_sidebar_layout',text='',icon=icon);op.action=action;op.channel=declared['channel']
                line=layout.row(align=True)
                for target,label in [('KEY','→形态键'),('BONE','→骨控')]:
                    op=line.operator('c94_workflow.expression_switch',text=label);op.target=target
                line=layout.row(align=True)
                for target,label in [('KEY','复位→形态键'),('BONE','复位→骨控')]:
                    op=line.operator('c94_workflow.expression_switch',text=label);op.target=target;op.reset=True
                layout.label(text='骨控模式本组形态键为0；切换不匹配任意表情')
                layout.label(text='多滑杆线性叠加效果须另查')
                layout.label(text='旧动作保留；只记录当前帧')
                from . import face_graph_board as board
                if mouth:
                    layout.operator('c94_workflow.expression_board_select',text='选择二维口型控骨')
                    layout.operator('c94_workflow.expression_board_layout',text='口型板位置/大小').index=-2
                    layout.operator('c94_workflow.expression_board_layout',text='二维区域位置/大小').index=-1
                    layout.operator('c94_workflow.mouth_fit_frame')
                    if not any(o.get('c94_board_art_v1') for o in opts.hub['board_display'].objects):
                        layout.operator('c94_workflow.expression_board_art')
                    for i,r in enumerate(mouth['sliders']):layout.operator('c94_workflow.expression_board_layout',text=r['name']+' 布局').index=i
                elif board.STATE_KEY in opts.hub:
                    layout.operator('c94_workflow.expression_board_select')
                    layout.operator('c94_workflow.expression_board_layout',text='图形板位置/大小').index=-1
                    _,b,_,_=board._read(opts.hub)
                    for i,r in enumerate(record['channels']):
                        layout.operator('c94_workflow.expression_board_layout',text=b['channels'][i]['section']+' · '+r['name']+' 布局').index=i
                    layout.label(text='表情动画在同一骨架的滑杆骨；旧动作保留')
                else:
                    layout.prop(opts,'font');layout.operator('c94_workflow.expression_board_create')
                    layout.operator('c94_workflow.mouth_grid_initialize')
                    for r in opts.mouth_slots:
                        line=layout.row(align=True);line.label(text='(%d, %.1f)'%(r.x,r.y/2))
                        line.prop_search(r,'channel',opts,'channels',text='')
                    if opts.mouth_slots:layout.operator('c94_workflow.mouth_pad_create')
                if opts.message:layout.label(text=opts.message)
                layout.operator('c94_workflow.expression_prepare_capture')
            except _ERRORS as exc:layout.label(text=str(exc))
        else:
            layout.prop(opts,'rig')
            for i,row in enumerate(opts.members):
                line=layout.row(align=True);line.prop(row,'source',text='同步网格')
                line.operator('c94_workflow.expression_members',text='',icon='X').remove_index=i
            layout.operator('c94_workflow.expression_members').remove_index=-1
            layout.operator('c94_workflow.expression_controls_create')


_CLASSES=(C94FEC_Member,C94FEC_GridSlot,C94FEC_Section,C94FEC_Row,C94FEC_Options,C94FEC_OT_SidebarLayout,C94FEC_OT_Focus,C94FEC_OT_FitFrame,C94FEC_OT_BoardArt,C94FEC_OT_PrepareCapture,C94FEC_OT_MouthGrid,C94FEC_OT_MouthCreate,C94FEC_OT_MouthSet,C94FEC_OT_Refresh,C94FEC_OT_Board,C94FEC_OT_SelectBoard,C94FEC_OT_Layout,C94FEC_OT_Members,C94FEC_OT_Create,C94FEC_OT_Activate,C94FEC_OT_Record,C94FEC_OT_Switch,C94FEC_PT_Controls)
def register():
    global _C94_OPTIONS_OWNED
    if _REGISTERED:return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('该面板属性已由其他实例持有；原属性保持')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94FEC_Options))
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
            and prop.fixed_type == C94FEC_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
