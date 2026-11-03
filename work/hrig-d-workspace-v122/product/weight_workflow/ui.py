"""One panel for all local weight planners; every writer goes through session."""
import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from .session import get_session


def changed(self, context):
    session = get_session(False)
    if session:
        session.invalidate()


class HRIG_W_Group(bpy.types.PropertyGroup):
    name: StringProperty(name='组名')
    enabled: BoolProperty(name='编辑', default=False, update=changed)
    frozen: BoolProperty(default=False)


class HRIG_W_VertexPair(bpy.types.PropertyGroup):
    source: IntProperty(name='来源顶点', min=0, update=changed)
    target: IntProperty(name='目标顶点', min=0, update=changed)


class HRIG_W_GroupPair(bpy.types.PropertyGroup):
    source: StringProperty(name='来源组', update=changed)
    target: StringProperty(name='目标组', update=changed)


class HRIG_W_Properties(bpy.types.PropertyGroup):
    operation: EnumProperty(name='操作', items=[
        ('SMOOTH', '局部平滑', ''), ('MIRROR', '明确映射镜像', ''), ('NORMALIZE', '归一为 1', ''),
        ('LIMIT_INFLUENCES', '限制正权影响数', ''), ('GENERATE_FROM_NEIGHBORS', '从邻居生成', '')], update=changed)
    groups: CollectionProperty(type=HRIG_W_Group)
    active_group: IntProperty(default=0)
    iterations: IntProperty(name='迭代', default=1, min=1, max=20, update=changed)
    factor: FloatProperty(name='平滑比例', default=.25, min=.0001, max=1., precision=4, update=changed)
    maximum: IntProperty(name='正权影响上限', default=4, min=1, max=64, update=changed)
    max_hops: IntProperty(name='邻接层数', default=1, min=1, max=4, update=changed)
    max_donors: IntProperty(name='来源点上限', default=32, min=1, max=256, update=changed)
    vertex_pairs: CollectionProperty(type=HRIG_W_VertexPair)
    group_pairs: CollectionProperty(type=HRIG_W_GroupPair)
    active_vertex_pair: IntProperty(default=0, min=0)
    active_group_pair: IntProperty(default=0, min=0)


class HRIG_W_UL_groups(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row()
        row.enabled = not item.frozen
        row.prop(item, 'enabled', text='')
        row.label(text=item.name, icon='LOCKED' if item.frozen else 'GROUP_VERTEX')


class HRIG_W_UL_vertex_pairs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row(align=True)
        row.prop(item, 'source', text='源')
        row.prop(item, 'target', text='目标')


class HRIG_W_UL_group_pairs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        session = get_session(False)
        row = layout.row(align=True)
        if session:
            row.prop_search(item, 'source', session.tx.obj, 'vertex_groups', text='源')
            row.prop_search(item, 'target', session.tx.obj, 'vertex_groups', text='目标')


def execute(operator, event, fn):
    try:
        result = get_session().run(event, fn)
        operator.report({'INFO'}, result.get('status', '完成'))
        return {'FINISHED'}
    except Exception as error:
        operator.report({'ERROR'}, getattr(error, 'code', type(error).__name__) + ': ' + str(error)[:400])
        return {'CANCELLED'}


class HRIG_W_OT_refresh(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.refresh'
    bl_label = '刷新原生组锁与保护状态'

    @classmethod
    def poll(cls, context):
        session = get_session(False)
        return session is not None and not session.pending and context.mode == 'OBJECT'

    def execute(self, context):
        return execute(self, 'refresh_groups', lambda: get_session().refresh_groups(context.window_manager.hrig_weight_workflow))


class HRIG_W_OT_pair(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.pair'
    bl_label = '编辑明确映射'
    kind: EnumProperty(items=[('VERTEX', '顶点', ''), ('GROUP', '组', '')])
    remove: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        session = get_session(False)
        return session is not None and not session.pending and context.mode == 'OBJECT'

    def execute(self, context):
        def perform():
            session = get_session()
            session.tx._guard()
            props = context.window_manager.hrig_weight_workflow
            items = props.vertex_pairs if self.kind == 'VERTEX' else props.group_pairs
            index_name = 'active_vertex_pair' if self.kind == 'VERTEX' else 'active_group_pair'
            if self.remove:
                index = getattr(props, index_name)
                if not 0 <= index < len(items):
                    raise ValueError('请选择要移除的映射')
                items.remove(index)
                setattr(props, index_name, max(0, min(index, len(items) - 1)))
            else:
                items.add()
                setattr(props, index_name, len(items) - 1)
            session.invalidate()
            return {'status': 'mapping_changed', 'count': len(items)}
        return execute(self, 'mapping', perform)


class HRIG_W_OT_selection(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.selection'
    bl_label = '读取目标顶点'
    donors: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.pending and context.mode == 'OBJECT'

    def execute(self, context):
        return execute(self, 'donors' if self.donors else 'targets', lambda: get_session().selection(donors=self.donors))


class HRIG_W_OT_inspect(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.inspect'
    bl_label = '1 检查'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.pending and context.mode == 'OBJECT'

    def execute(self, context):
        return execute(self, 'inspect', lambda: get_session().inspect(context.window_manager.hrig_weight_workflow))


class HRIG_W_OT_preview(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.preview'
    bl_label = '2 预览'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.pending and s.snapshot_token is not None and context.mode == 'OBJECT'

    def execute(self, context):
        return execute(self, 'preview', lambda: get_session().preview(context.window_manager.hrig_weight_workflow))


class HRIG_W_OT_restore(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.restore'
    bl_label = '3 完整恢复'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and s.pending and context.mode == 'OBJECT'

    def execute(self, context):
        return execute(self, 'restore', lambda: get_session().restore())


class HRIG_W_OT_save(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.save'
    bl_label = '另存候选'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and s.pending and not s.save_attempted and context.mode == 'OBJECT' and context.scene.frame_current_final == 0

    def execute(self, context):
        return execute(self, 'save', lambda: get_session().save_copy())


class HRIG_W_OT_zero(bpy.types.Operator):
    bl_idname = 'hrig_weight_workflow.zero'
    bl_label = '返回第 0 帧'

    @classmethod
    def poll(cls, context):
        s = get_session(False)
        return s is not None and not s.pending and context.mode == 'OBJECT'

    def execute(self, context):
        return execute(self, 'frame_zero', lambda: get_session().return_zero())


class HRIG_W_PT_panel(bpy.types.Panel):
    bl_label = '局部权重编辑'
    bl_idname = 'HRIG_W_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'HRIG D1'

    def draw(self, context):
        layout = self.layout
        s = get_session(False)
        if s is None:
            layout.label(text='请先初始化权重会话')
            return
        p = context.window_manager.hrig_weight_workflow
        layout.label(text=s.tx.obj.name)
        edit = layout.column()
        edit.enabled = not s.pending
        edit.prop(p, 'operation')
        edit.label(text='目标顶点：' + str(len(s.vertices)))
        edit.operator('hrig_weight_workflow.selection')
        edit.template_list('HRIG_W_UL_groups', '', p, 'groups', p, 'active_group', rows=6)
        edit.operator('hrig_weight_workflow.refresh')
        edit.label(text='组锁使用 Blender 原生顶点组锁')
        if p.operation == 'SMOOTH':
            edit.prop(p, 'iterations')
            edit.prop(p, 'factor')
        elif p.operation == 'MIRROR':
            edit.label(text='明确指定来源与目标；不自动猜对应')
            for kind, collection, active, ui_list in (
                ('VERTEX', 'vertex_pairs', 'active_vertex_pair', 'HRIG_W_UL_vertex_pairs'),
                ('GROUP', 'group_pairs', 'active_group_pair', 'HRIG_W_UL_group_pairs')):
                edit.template_list(ui_list, '', p, collection, p, active, rows=3)
                row = edit.row(align=True)
                add = row.operator('hrig_weight_workflow.pair', text='添加映射', icon='ADD')
                add.kind, add.remove = kind, False
                remove = row.operator('hrig_weight_workflow.pair', text='移除映射', icon='REMOVE')
                remove.kind, remove.remove = kind, True
            edit.label(text='目标顶点须与读取的选区完全对应')
        elif p.operation == 'LIMIT_INFLUENCES':
            edit.prop(p, 'maximum')
            edit.label(text='旧成员保留；裁剪记录为零')
        elif p.operation == 'GENERATE_FROM_NEIGHBORS':
            edit.label(text='已声明来源点：' + str(len(s.donors)))
            edit.operator('hrig_weight_workflow.selection', text='读取来源顶点').donors = True
            edit.prop(p, 'max_hops')
            edit.prop(p, 'max_donors')
        else:
            edit.label(text='只调整可编辑组；锁与遮罩保持')
        layout.operator('hrig_weight_workflow.inspect')
        layout.operator('hrig_weight_workflow.preview')
        layout.operator('hrig_weight_workflow.restore')
        layout.operator('hrig_weight_workflow.save')
        layout.operator('hrig_weight_workflow.zero')
        layout.label(text='状态：' + (s.tx.pending['phase'] if s.pending else '空闲'))
        if s.last_error:
            layout.label(text=s.last_error, icon='ERROR')
        if s.log_error:
            layout.label(text='证据写入失败，请检查输出目录', icon='ERROR')


CLASSES = (HRIG_W_Group, HRIG_W_VertexPair, HRIG_W_GroupPair, HRIG_W_Properties,
    HRIG_W_UL_groups, HRIG_W_UL_vertex_pairs, HRIG_W_UL_group_pairs, HRIG_W_OT_refresh, HRIG_W_OT_pair, HRIG_W_OT_selection,
    HRIG_W_OT_inspect, HRIG_W_OT_preview, HRIG_W_OT_restore, HRIG_W_OT_save, HRIG_W_OT_zero, HRIG_W_PT_panel)


def register():
    for cls in CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)
    if not hasattr(bpy.types.WindowManager, 'hrig_weight_workflow'):
        bpy.types.WindowManager.hrig_weight_workflow = PointerProperty(type=HRIG_W_Properties)


def unregister():
    session = get_session(False)
    if session and session.pending:
        raise RuntimeError('Restore pending weights before unregister')
    if hasattr(bpy.types.WindowManager, 'hrig_weight_workflow'):
        del bpy.types.WindowManager.hrig_weight_workflow
    for cls in reversed(CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
