"""Native C94 template-review sidebar.

Configuration is a declaration for the next authoring stage. No operator here
generates, deletes, fits, binds, simulates, or switches the source rig.
"""
import json

import bpy
from bpy.props import BoolProperty, PointerProperty

STATE_KEY = 'c94_workflow_state_v1'
OPTIONS_ATTR = 'c94_workflow_options_v1'
OPTION_KEYS = ('face', 'hair', 'breast', 'glute', 'toes', 'skirt')
_REGISTERED = []
_PROPERTY_OWNED = False


def read_state(scene):
    """Read only the explicitly owned JSON state; never infer the active rig."""
    raw = scene.get(STATE_KEY)
    if raw is None:
        return None, '当前场景尚无模板审查记录'
    if not isinstance(raw, str):
        return None, '模板审查记录格式错误：需要 JSON 文本'
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None, '模板审查记录无法解析'
    if not isinstance(value, dict):
        return None, '模板审查记录需要对象结构'
    return value, None


def main_rig(context, state):
    name = state.get('rig_object') if state else None
    obj = context.scene.objects.get(name) if isinstance(name, str) else None
    return obj if obj and obj.type == 'ARMATURE' else None


def reference_objects(context, state):
    """Resolve this scene's explicit reference names, excluding production rig."""
    values = state.get('reference_objects', []) if state else []
    if not isinstance(values, list):
        return [], ['参考层名单不是数组']
    result, missing, seen = [], [], set()
    for name in values:
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        if name == state.get('rig_object'):
            missing.append(name + '（主骨架不属于参考层）')
            continue
        obj = context.scene.objects.get(name)
        if obj is None:
            missing.append(name)
        else:
            result.append(obj)
    return result, missing


def _has_state(context):
    return bool(context.scene and read_state(context.scene)[0])


def _object_mode(context):
    if context.mode == 'OBJECT':
        return True
    if context.view_layer.objects.active is None or not bpy.ops.object.mode_set.poll():
        return False
    bpy.ops.object.mode_set(mode='OBJECT')
    return context.mode == 'OBJECT'


def _select(context, objects):
    available = [o for o in objects if o.name in context.view_layer.objects
                 and o.visible_get(view_layer=context.view_layer) and not o.hide_select]
    if not available:
        return 0
    for obj in context.view_layer.objects:
        if obj.select_get(view_layer=context.view_layer):
            obj.select_set(False, view_layer=context.view_layer)
    for obj in available:
        obj.select_set(True, view_layer=context.view_layer)
    context.view_layer.objects.active = available[0]
    return len(available)


class C94WF_Options(bpy.types.PropertyGroup):
    face: BoolProperty(name='面部与表情面板', default=True,
                       description='方案声明：启用贴脸控骨与独立表情面板；当前不改变骨架')
    hair: BoolProperty(name='头发附属链', default=True,
                       description='方案声明：启用按角色配置的头发链；当前不改变骨架')
    breast: BoolProperty(name='胸部附属', default=True,
                         description='方案声明：保留胸部附属；关闭后的依赖处理尚待生成阶段')
    glute: BoolProperty(name='臀部附属', default=True,
                        description='方案声明：保留臀部附属；基本骨盆和髋关节机制始终保留')
    toes: BoolProperty(name='独立脚趾', default=True,
                       description='方案声明：保留独立脚趾附属；不影响四肢基本机制')
    skirt: BoolProperty(name='裙摆附属链', default=False,
                        description='为当前角色另行配置裙摆链')


class C94WF_OT_SaveConfiguration(bpy.types.Operator):
    bl_idname = 'c94_workflow.save_configuration'
    bl_label = '保存模块方案'
    bl_description = '只保存模块选择，供后续模板生成使用；不增删或修改当前骨骼'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _has_state(context)

    def execute(self, context):
        state, error = read_state(context.scene)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        opts = getattr(context.scene, OPTIONS_ATTR)
        state['configuration'] = {key: bool(getattr(opts, key)) for key in OPTION_KEYS}
        context.scene[STATE_KEY] = json.dumps(state, ensure_ascii=False, allow_nan=False)
        self.report({'INFO'}, '模块方案已保存；当前骨架未改变')
        return {'FINISHED'}


class C94WF_OT_LoadConfiguration(bpy.types.Operator):
    bl_idname = 'c94_workflow.load_configuration'
    bl_label = '载入已保存方案'
    bl_description = '将本场景已保存的模块选择载入编辑控件'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        state, _ = read_state(context.scene)
        return isinstance(state.get('configuration'), dict) if state else False

    def execute(self, context):
        state, _ = read_state(context.scene)
        config = state['configuration']
        if any(key in config and not isinstance(config[key], bool) for key in OPTION_KEYS):
            self.report({'ERROR'}, '保存的模块方案含非布尔值')
            return {'CANCELLED'}
        opts = getattr(context.scene, OPTIONS_ATTR)
        for key in OPTION_KEYS:
            if key in config:
                setattr(opts, key, config[key])
        self.report({'INFO'}, '已载入本场景模块方案')
        return {'FINISHED'}


class C94WF_OT_SelectMain(bpy.types.Operator):
    bl_idname = 'c94_workflow.select_main'
    bl_label = '选择主骨架'
    bl_description = '选择审查记录点名的主骨架；必要时退出当前编辑模式'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        state, _ = read_state(context.scene)
        return bool(context.scene and context.view_layer and main_rig(context, state))

    def execute(self, context):
        state, _ = read_state(context.scene)
        rig = main_rig(context, state)
        if not rig or not _object_mode(context):
            self.report({'ERROR'}, '当前上下文不能退出模式并选择主骨架')
            return {'CANCELLED'}
        if not _select(context, [rig]):
            self.report({'WARNING'}, '主骨架不在当前可见可选视图层中')
            return {'CANCELLED'}
        return {'FINISHED'}


class C94WF_OT_SelectReferences(bpy.types.Operator):
    bl_idname = 'c94_workflow.select_references'
    bl_label = '选择参考层'
    bl_description = '只选择本场景记录中点名的可见参考对象；必要时退出当前模式'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        state, _ = read_state(context.scene)
        return bool(context.scene and context.view_layer and reference_objects(context, state)[0])

    def execute(self, context):
        state, _ = read_state(context.scene)
        objects, missing = reference_objects(context, state)
        if not _object_mode(context):
            self.report({'ERROR'}, '当前上下文不能退出模式并选择参考层')
            return {'CANCELLED'}
        count = _select(context, objects)
        if not count:
            self.report({'WARNING'}, '参考层当前不可见或不可选，请先显示参考层')
            return {'CANCELLED'}
        if missing:
            self.report({'WARNING'}, f'已选择 {count} 个参考对象；另有 {len(missing)} 项缺失或被保护')
        return {'FINISHED'}


class C94WF_OT_ReferenceVisibility(bpy.types.Operator):
    bl_idname = 'c94_workflow.reference_visibility'
    bl_label = '设置参考层显隐'
    bl_description = '仅改变本场景记录点名的参考对象在当前视图层的显隐'
    bl_options = {'REGISTER', 'UNDO'}

    visible: BoolProperty(name='显示', default=True)

    @classmethod
    def poll(cls, context):
        state, _ = read_state(context.scene)
        return bool(context.scene and context.view_layer and reference_objects(context, state)[0])

    def execute(self, context):
        state, _ = read_state(context.scene)
        objects, missing = reference_objects(context, state)
        count = 0
        for obj in objects:
            if obj.name not in context.view_layer.objects:
                missing.append(obj.name + '（当前视图层之外）')
                continue
            obj.hide_set(not self.visible, view_layer=context.view_layer)
            count += 1
        if not count:
            self.report({'WARNING'}, '当前视图层没有可调整的参考对象')
            return {'CANCELLED'}
        if missing:
            self.report({'WARNING'}, f'已调整 {count} 个参考对象；另有 {len(missing)} 项未调整')
        return {'FINISHED'}


def _module_rows(state):
    summary = state.get('module_summary')
    if not isinstance(summary, dict):
        return [], '详见模块方案Text'
    labels = {'core':'核心', 'shared':'共享机制', 'face':'面部', 'hair':'头发',
              'breast':'胸部附属', 'glute':'臀部附属', 'toes':'独立脚趾',
              'skirt':'裙摆', 'unclassified':'待归属'}
    rows = []
    for key, count in list(summary.items())[:10]:
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        label = labels.get(key, str(key))
        rows.append(f'{label}：{count} 骨')
    return rows, None if rows else '详见模块方案Text'


class C94WF_PT_TemplateReview(bpy.types.Panel):
    bl_label = '爱音模板审查'
    bl_idname = 'C94WF_PT_template_review'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'

    def draw(self, context):
        layout = self.layout
        state, error = read_state(context.scene)
        layout.label(text='阶段：模板方案审查', icon='ARMATURE_DATA')
        if error:
            layout.label(text=error, icon='INFO')
            return
        rig = main_rig(context, state)
        layout.label(text='主骨架：' + str(state.get('rig_object', '未指定')))
        if not rig:
            layout.label(text='记录中的主骨架在本场景不存在', icon='ERROR')
            return
        layout.operator('c94_workflow.select_main', icon='RESTRICT_SELECT_OFF')

        box = layout.box()
        box.label(text='模块方案 · 仅声明配置')
        opts = getattr(context.scene, OPTIONS_ATTR)
        for key in OPTION_KEYS:
            box.prop(opts, key)
        box.operator('c94_workflow.save_configuration', icon='FILE_TICK')
        if isinstance(state.get('configuration'), dict):
            box.operator('c94_workflow.load_configuration', icon='LOOP_BACK')
        box.label(text='保存配置不增删当前骨骼', icon='INFO')

        refs, missing = reference_objects(context, state)
        box = layout.box()
        box.label(text=f'参考层：{len(refs)} 个对象')
        row = box.row(align=True)
        row.operator('c94_workflow.reference_visibility', text='显示', icon='HIDE_OFF').visible = True
        row.operator('c94_workflow.reference_visibility', text='隐藏', icon='HIDE_ON').visible = False
        box.operator('c94_workflow.select_references', icon='RESTRICT_SELECT_OFF')
        if missing:
            box.label(text=f'{len(missing)} 项缺失或不属于参考层', icon='INFO')

        box = layout.box()
        box.label(text='主骨架集合显隐')
        for collection in rig.data.collections_all:
            # These are native display flags only: no pose or solver input writes.
            row = box.row()
            row.prop(collection, 'is_visible', text=collection.name)

        box = layout.box()
        box.label(text='依赖方案摘要')
        rows, note = _module_rows(state)
        for value in rows:
            box.label(text=value)
        if note:
            box.label(text=note, icon='INFO')
        box.label(text='方案 Text：' + str(state.get('module_plan_text', '未指定')))


_CLASSES = (C94WF_Options, C94WF_OT_SaveConfiguration, C94WF_OT_LoadConfiguration,
            C94WF_OT_SelectMain, C94WF_OT_SelectReferences, C94WF_OT_ReferenceVisibility,
            C94WF_PT_TemplateReview)


def register():
    global _PROPERTY_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS_ATTR):
        raise RuntimeError('角色配置属性已注册，请先卸载原有同名模块')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS_ATTR, PointerProperty(type=C94WF_Options))
        _PROPERTY_OWNED = True
    except Exception:
        unregister()
        raise


def unregister():
    global _PROPERTY_OWNED
    if _PROPERTY_OWNED:
        prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS_ATTR)
        if prop and prop.fixed_type.identifier == C94WF_Options.bl_rna.identifier:
            delattr(bpy.types.Scene, OPTIONS_ATTR)
        _PROPERTY_OWNED = False
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
