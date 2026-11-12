"""Explicit atlas intake and neutral positioning preparation for a fresh Scene.

Only existing reference/snapshot/positioning mechanisms are exposed here. The
atlas is a private user-selected asset and is never bundled or overwritten.
"""
import json
from pathlib import Path
import uuid

import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty
from mathutils import Matrix

from . import generation, module_configuration as ownership, positioning

OPTIONS = 'c94_workspace_setup_options'
_REGISTERED = []
_OPTIONS_TYPE = None


def _need(value, message):
    if not value:
        raise ValueError(message)


def _context(context):
    scene = context.scene
    _need(scene is not None and scene == bpy.context.scene and context.mode == 'OBJECT',
          '请在当前场景的对象模式中准备角色')
    _need(not scene.library and not scene.override_library, '需要本地场景工作副本')
    return scene


def _object(scene, obj, kind):
    _need(isinstance(obj, bpy.types.Object) and obj.type == kind
          and scene.objects.get(obj.name) == obj
          and bpy.context.view_layer.objects.get(obj.name) == obj,
          '请明确选择当前场景与视图层中的' + ('完整参考骨架' if kind == 'ARMATURE' else '网格'))
    _need(not obj.library and not obj.override_library and not obj.data.library
          and not obj.data.override_library, '准备入口需要本地对象；先追加独立工作副本')


def append_atlas(context, filepath, collection_name, reference_name, atlas_name):
    """Append the named native atlas; its imported geometry alone is cleaned."""
    scene = _context(context)
    _need(all(isinstance(v, str) and v.strip() for v in
              (filepath, collection_name, reference_name, atlas_name)),
          '请填写图谱库、集合、完整参考与中立表面的明确名称')
    path = Path(bpy.path.abspath(filepath)).expanduser()
    _need(path.is_absolute() and path.suffix.lower() == '.blend' and path.is_file(),
          '请选择存在的图谱.blend绝对路径；图谱与生成模板库是不同资源')
    path = path.resolve()
    _need(collection_name not in bpy.data.collections and reference_name not in bpy.data.objects
          and atlas_name not in bpy.data.objects,
          '场景已有同名图谱对象或集合；请直接选择已有对象，或先为本次输入指定独立名称')
    before = ownership._ids()
    active, selected = context.view_layer.objects.active, list(context.selected_objects)
    owned, target = [], None
    options = getattr(scene, OPTIONS, None)
    previous = (options.reference, options.atlas_mesh, options.last_report) if options else None
    try:
        try:
            with bpy.data.libraries.load(str(path), link=False) as (source, target):
                _need(collection_name in source.collections, '图谱库中没有指定集合：' + collection_name)
                _need(reference_name in source.objects and atlas_name in source.objects,
                      '图谱库中没有指定完整参考或中立表面；请核对名称')
                target.collections = [collection_name]
        finally:
            if target is not None:
                owned.extend(generation._returned_import_ids(
                    getattr(target, 'collections', ()), before, path))
        collection = target.collections[0]
        _need(collection is not None, '原生图谱集合追加失败')
        scene.collection.children.link(collection)
        context.view_layer.update()
        reference, atlas = collection.all_objects.get(reference_name), collection.all_objects.get(atlas_name)
        _object(scene, reference, 'ARMATURE')
        _object(scene, atlas, 'MESH')
        _need(bool(reference.get('c94_reference_kind')), '指定骨架不是完整C94参考层，不能用旧目标骨架替代')
        _need(not atlas.modifiers and not atlas.data.shape_keys and not atlas.parent
              and not atlas.animation_data and not atlas.data.animation_data and not atlas.constraints,
              '图谱表面须为中立原始网格，无修改器、Key、父级、约束或动画')
        # Some historical atlas libraries contain unused dvert columns. Detach
        # the imported Mesh first: no source library or other consumer is edited.
        if atlas.vertex_groups or any(v.groups for v in atlas.data.vertices):
            old_mesh = atlas.data
            atlas.data = ownership._own(owned, old_mesh.copy())
            maximum = max((g.group for v in atlas.data.vertices for g in v.groups), default=-1)
            while len(atlas.vertex_groups) <= maximum:
                atlas.vertex_groups.new(name='C94_图谱旧列_' + uuid.uuid4().hex[:8])
            atlas.vertex_groups.clear()
            _need(not any(v.groups for v in atlas.data.vertices), '图谱独立副本仍残留旧权重列，未提交图谱')
            if old_mesh in owned and old_mesh.users == 0:
                bpy.data.meshes.remove(old_mesh)
        positioning.mesh_points(atlas)
        if options:
            options.reference = reference
            options.atlas_mesh = atlas
            options.last_report = json.dumps({'operation': 'append_atlas', 'file': str(path),
                'reference': reference.name, 'atlas_mesh': atlas.name,
                'source_library_overwritten': False}, ensure_ascii=False)
        return {'collection': collection, 'reference': reference, 'atlas_mesh': atlas}
    except Exception:
        if options and previous is not None:
            options.reference, options.atlas_mesh, options.last_report = previous
        ownership._cleanup(before, active, selected, owned_ids=owned)
        raise


def _snapshot_target(context, source, owned):
    """Neutral Basis plus existing Subsurf only, on independent temporary IDs."""
    _object(context.scene, source, 'MESH')
    _need(not source.constraints and not source.animation_data and not source.data.animation_data,
          '目标对象或Mesh有动画/约束，不能推断中立世界框架；请先准备明确的中立副本')
    _need(not source.parent or source.parent_type == 'OBJECT',
          '骨父级目标须先准备明确的中立世界框架')
    _need(all(m.type in {'ARMATURE', 'SUBSURF'} for m in source.modifiers),
          '目标快照只接Basis与原有Subsurf；其它几何修改器请先在独立副本明确处理')
    clone = ownership._own(owned, source.copy())
    clone.data = ownership._own(owned, source.data.copy())
    _need(not source.data.shape_keys or clone.data.shape_keys != source.data.shape_keys,
          '原生Mesh.copy未隔离Key，原资产保持')
    context.scene.collection.objects.link(clone)
    world = source.matrix_world.copy()
    clone.parent = None
    clone.matrix_parent_inverse = Matrix.Identity(4)
    clone.delta_location = (0, 0, 0)
    clone.delta_scale = (1, 1, 1)
    clone.delta_rotation_euler = (0, 0, 0)
    clone.delta_rotation_quaternion = (1, 0, 0, 0)
    clone.matrix_world = world
    clone.animation_data_clear()
    clone.data.animation_data_clear()
    if clone.data.shape_keys:
        basis = [point.co.copy() for point in clone.data.shape_keys.reference_key.data]
        clone.shape_key_clear()
        for vertex, point in zip(clone.data.vertices, basis):
            vertex.co = point
    # Group names and dverts live on different native owners; clear both on the
    # private copy before evaluating the geometry-only snapshot.
    maximum = max((g.group for v in clone.data.vertices for g in v.groups), default=-1)
    while len(clone.vertex_groups) <= maximum:
        clone.vertex_groups.new(name='C94_快照旧列_' + uuid.uuid4().hex[:8])
    clone.vertex_groups.clear()
    for modifier in list(clone.modifiers):
        if modifier.type != 'SUBSURF':
            clone.modifiers.remove(modifier)
    clone.hide_set(False)
    clone.hide_viewport = False
    context.view_layer.update()
    graph = context.evaluated_depsgraph_get()
    evaluated = clone.evaluated_get(graph)
    mesh = ownership._own(owned, bpy.data.meshes.new_from_object(
        evaluated, preserve_all_data_layers=True, depsgraph=graph))
    mesh.materials.clear()
    output = ownership._own(owned, bpy.data.objects.new('C94_中立定位输入_' + uuid.uuid4().hex[:8], mesh))
    context.scene.collection.objects.link(output)
    output.matrix_world = evaluated.matrix_world.copy()
    output['c94_positioning_snapshot_v1'] = json.dumps({'source': source.name,
        'capture': 'neutral Basis plus existing Subsurf; current declared world frame',
        'old_binding_animation_key_inputs': 'not consumed', 'source_unchanged': True}, ensure_ascii=False)
    old_mesh = clone.data
    bpy.data.objects.remove(clone, do_unlink=True)
    if old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)
    positioning.mesh_points(output)
    return output


def prepare_positioning(context, reference, atlas_mesh, target_mesh, *, snapshot_target=True):
    scene = _context(context)
    _need(type(snapshot_target) is bool, '目标快照选项须为明确布尔值')
    _need(positioning.read_state(scene) is None, '当前场景已有定位任务，请继续该任务；新角色请使用独立场景工作副本')
    _object(scene, reference, 'ARMATURE')
    _object(scene, atlas_mesh, 'MESH')
    _object(scene, target_mesh, 'MESH')
    before = ownership._ids()
    active, selected = context.view_layer.objects.active, list(context.selected_objects)
    owned = []
    old_present, old_record = positioning.STATE_KEY in scene, scene.get(positioning.STATE_KEY)
    options = getattr(scene, OPTIONS, None)
    previous_report = options.last_report if options else None
    try:
        target = _snapshot_target(context, target_mesh, owned) if snapshot_target else target_mesh
        state = positioning.prepare(scene, reference, atlas_mesh, target, owned_ids=owned)
        if options:
            options.last_report = json.dumps({'operation': 'prepare_positioning', 'reference': reference.name,
                'atlas_mesh': atlas_mesh.name, 'source_target': target_mesh.name,
                'positioning_target': target.name, 'snapshot_target': snapshot_target,
                'markers': len(state['markers']), 'estimate_status': 'unestimated'}, ensure_ascii=False)
        return state
    except Exception:
        if old_present:
            scene[positioning.STATE_KEY] = old_record
        elif positioning.STATE_KEY in scene:
            del scene[positioning.STATE_KEY]
        if options:
            options.last_report = previous_report
        ownership._cleanup(before, active, selected, owned_ids=owned)
        raise


def _mesh_poll(_self, obj):
    return obj.type == 'MESH'


def _rig_poll(_self, obj):
    return obj.type == 'ARMATURE'


class C94SETUP_Options(bpy.types.PropertyGroup):
    atlas_file: StringProperty(name='私有定位图谱库',
        description='选择你本机的C94定位图谱.blend；不是只有骨架的生成模板库，原文件不会改写')
    collection_name: StringProperty(name='图谱集合', default='C94定位图谱')
    reference_name: StringProperty(name='库内完整参考', default='爱音-完整未拟合参考')
    atlas_name: StringProperty(name='库内中立表面', default='C94-中立表面')
    reference: PointerProperty(name='完整参考', type=bpy.types.Object, poll=_rig_poll)
    atlas_mesh: PointerProperty(name='中立图谱表面', type=bpy.types.Object, poll=_mesh_poll)
    target_mesh: PointerProperty(name='目标角色网格', type=bpy.types.Object, poll=_mesh_poll)
    snapshot_target: BoolProperty(name='创建目标中立几何副本', default=True,
        description='目标须已具明确中立世界框架；独立捕获Basis与原有Subsurf，保留原Key、绑定、权重和动画')
    last_report: StringProperty(name='最近准备结果', options={'HIDDEN'})


class C94SETUP_OT_ChooseAtlas(bpy.types.Operator):
    bl_idname = 'c94_workflow.choose_positioning_atlas'
    bl_label = '选择定位图谱库'
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default='*.blend', options={'HIDDEN'})

    def invoke(self, context, event):
        self.filepath = getattr(context.scene, OPTIONS).atlas_file
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        getattr(context.scene, OPTIONS).atlas_file = bpy.path.abspath(self.filepath)
        return {'FINISHED'}


class C94SETUP_OT_AppendAtlas(bpy.types.Operator):
    bl_idname = 'c94_workflow.append_positioning_atlas'
    bl_label = '追加独立图谱副本'
    bl_description = '按明确集合及对象名称追加本机私有图谱；不覆盖库或场景已有对象'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == 'OBJECT'

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            append_atlas(context, options.atlas_file, options.collection_name,
                         options.reference_name, options.atlas_name)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, '图谱独立副本已追加，请明确选择目标角色网格')
        return {'FINISHED'}


class C94SETUP_OT_Prepare(bpy.types.Operator):
    bl_idname = 'c94_workflow.prepare_positioning_workspace'
    bl_label = '建立定位工作区'
    bl_description = '使用明确参考、图谱和中立目标建立可编辑定位标记；原角色资产保留'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == 'OBJECT'

    def execute(self, context):
        options = getattr(context.scene, OPTIONS)
        try:
            state = prepare_positioning(context, options.reference, options.atlas_mesh,
                options.target_mesh, snapshot_target=options.snapshot_target)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'已建立{len(state["markers"])}个定位标记，下一步估计并检查参考骨')
        return {'FINISHED'}


class C94SETUP_PT_Prepare(bpy.types.Panel):
    bl_label = '新角色准备'
    bl_idname = 'C94SETUP_PT_prepare'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        options = getattr(context.scene, OPTIONS)
        workspace = getattr(context.scene, 'c94_workspace_options_v1', None)
        advanced = workspace is None or workspace.level != 'DAILY'
        try:
            state = positioning.read_state(context.scene)
        except (TypeError, ValueError) as exc:
            layout.label(text=str(exc), icon='ERROR')
            return
        if state:
            layout.label(text='定位任务已准备', icon='CHECKMARK')
            layout.label(text='继续下方的估计、检查与参考拟合')
            if not advanced:
                return
        layout.label(text='先准备图谱，再选择角色中立网格', icon='INFO')
        box = layout.box()
        box.label(text='1 · 选择定位图谱')
        row = box.row(align=True)
        row.prop(options, 'atlas_file', text='图谱库')
        row.operator('c94_workflow.choose_positioning_atlas', text='', icon='FILE_FOLDER')
        if advanced:
            box.prop(options, 'collection_name')
            box.prop(options, 'reference_name')
            box.prop(options, 'atlas_name')
        box.operator('c94_workflow.append_positioning_atlas', text='载入独立图谱', icon='APPEND_BLEND')
        if options.reference and options.atlas_mesh:
            box.label(text='完整参考与中立图谱已就绪', icon='CHECKMARK')
        box = layout.box()
        box.label(text='2 · 选择角色中立网格')
        if advanced:
            box.label(text='也可直接指定场景中的已有输入')
            box.prop(options, 'reference')
            box.prop(options, 'atlas_mesh')
        box.prop(options, 'target_mesh')
        if advanced:
            box.prop(options, 'snapshot_target')
            box.label(text='中立副本只取Basis与原有Subsurf')
        else:
            box.label(text='原网格保留，使用独立中立副本')
        row = box.row()
        row.enabled = context.mode == 'OBJECT'
        row.operator('c94_workflow.prepare_positioning_workspace', text='开始定位', icon='ARMATURE_DATA')


_CLASSES = (C94SETUP_Options, C94SETUP_OT_ChooseAtlas, C94SETUP_OT_AppendAtlas,
            C94SETUP_OT_Prepare, C94SETUP_PT_Prepare)


def register():
    global _OPTIONS_TYPE
    if _REGISTERED or hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('角色准备入口已注册或属性被占用；不替换已有实例')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94SETUP_Options))
        _OPTIONS_TYPE = bpy.types.Scene.bl_rna.properties[OPTIONS].fixed_type.as_pointer()
    except Exception:
        unregister()
        raise


def unregister():
    global _OPTIONS_TYPE
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if _OPTIONS_TYPE is not None and prop is not None and prop.fixed_type.as_pointer() == _OPTIONS_TYPE:
        delattr(bpy.types.Scene, OPTIONS)
    _OPTIONS_TYPE = None
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
