# SPDX-License-Identifier: GPL-3.0-or-later
"""Staged multi-domain skinning plan UI; install in rigging_workflow as one module.

Geometry/worker ownership belongs to skinning_plan. Masks edit only an explicitly
validated independent sampling surface. Rigid IDs come from native source vertex
selection, never user-entered JSON. Draw uses cached summaries and native UI only.
"""
import json
import os
import tempfile
import uuid

import bpy
import numpy as np
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)

from . import skinning_ui as _single
from .skinning import topology_fingerprint


OPTIONS = 'c94_skinning_plan_options_v1'
_REGISTERED = []
_SESSION = None
_INTERVAL = .25
_STATUSES = {'running', 'succeeded', 'failed', 'cancelled', 'applied'}
_name = _single._name
_tag_redraw = _single._tag_redraw
_final_outputs = _single._final_outputs
def _settings(row):
    return {**_single._settings(row), 'seed_radius_cells': float(row.seed_radius_cells),
        'max_seed_projection_cells': float(row.max_seed_projection_cells),
        'sample_radius_cells': float(row.sample_radius_cells), 'require_all_bones_seeded': True,
        'tolerance': float(row.tolerance), 'max_iterations': int(row.max_iterations),
        'min_component_cells': int(row.min_component_cells)}


def _mesh(_self, obj):
    return obj.type == 'MESH'


def _armature(_self, obj):
    return obj.type == 'ARMATURE'


def _options(scene):
    return getattr(scene, OPTIONS)


def _changed(_self, context):
    """Constant-time cache invalidation, never geometry validation in redraw."""
    if _SESSION is None and context and getattr(context, 'scene', None):
        try:
            options = _options(context.scene)
            options.check_json = ''
            options.check_summary = ''
            options.summary = '计划已修改，请重新检查'
        except (ReferenceError, AttributeError):
            pass


def _setup(context):
    if _SESSION is not None:
        raise ValueError('当前任务及结果仍被保留；先结束会话再修改计划')
    return _options(context.scene)


def _session(context):
    if _SESSION is None:
        raise ValueError('没有本面板启动的多域蒙皮任务')
    if _SESSION['job'].session_scene != context.scene:
        raise ValueError('请回到任务所属场景：' + _name(_SESSION['job'].session_scene))
    return _SESSION


def _active(collection, index, label):
    if not 0 <= index < len(collection):
        raise ValueError('先选择一个' + label)
    return collection[index]


def _domain(options):
    return _active(options.domains, options.domain_index, '体积域')


def _rigid(options):
    return _active(options.rigid, options.rigid_index, '刚性域')


def _local_mesh(context, obj, label):
    if (not isinstance(obj, bpy.types.Object) or obj.type != 'MESH'
            or obj.name not in context.view_layer.objects
            or context.view_layer.objects[obj.name] != obj
            or obj.library or obj.override_library or obj.data.library
            or not obj.is_editable or not obj.data.is_editable):
        raise ValueError(label + '必须为当前视图层的可编辑本地网格')
    return obj


def _finish_edit(context, related):
    """Flush one explicitly related native Mesh edit; reject all other modes."""
    if context.mode == 'OBJECT':
        return
    if (context.mode != 'EDIT_MESH' or context.object not in related
            or len(context.objects_in_mode) != 1):
        raise ValueError('先结束其他对象、多对象编辑、骨架编辑、姿态或雕刻模式')
    bpy.ops.object.mode_set(mode='OBJECT')


def _visible_selected_ids(context, obj):
    _local_mesh(context, obj, '选点目标')
    if not obj.visible_get(view_layer=context.view_layer):
        raise ValueError('选点目标当前不可见，请先显示该对象')
    if context.mode == 'EDIT_MESH':
        if context.object != obj or len(context.objects_in_mode) != 1:
            raise ValueError('只能在指定目标的单对象编辑模式捕获选点')
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.index_update()
        ids = [v.index for v in bm.verts if v.select and not v.hide]
    elif context.mode == 'OBJECT' and obj.mode == 'OBJECT':
        ids = [v.index for v in obj.data.vertices if v.select and not v.hide]
    else:
        raise ValueError('先结束其他编辑、姿态或雕刻模式')
    if not ids:
        raise ValueError('没有可见选中顶点；先在指定网格上明确选点')
    # Indices are captured from live bmesh before leaving Edit. Attribute writes
    # happen only after this flush, so stale Mesh selection cannot define a mask.
    _finish_edit(context, {obj})
    return ids


def _surface(context, options):
    from . import skinning_plan
    _local_mesh(context, options.source, '生产源')
    surface = _local_mesh(context, options.surface, '采样表面')
    skinning_plan.validate_surface(options.source, surface)
    return surface


def _unique_label(base, collection):
    names = {row.name for row in collection}
    i = 1
    while f'{base}{i}' in names:
        i += 1
    return f'{base}{i}'


def _append_bones(domain, names):
    existing = {row.bone_name for row in domain.bone_names}
    added = 0
    for name in names:
        if name not in existing:
            domain.bone_names.add().bone_name = name
            existing.add(name)
            added += 1
    return added


def _rigid_ids(row):
    try:
        ids = json.loads(row.indices_json)
    except (TypeError, ValueError) as exc:
        raise ValueError('刚性选区记录无效，请重新捕获原生选点') from exc
    if (not isinstance(ids, list) or any(type(i) is not int or i < 0 for i in ids)
            or len(set(ids)) != len(ids) or len(ids) != row.count):
        raise ValueError('刚性选区记录与缓存不一致，请重新捕获')
    return ids


def _payload(options):
    allowed = _final_outputs(options.rig)
    domains, rigid = [], []
    for row in options.domains:
        names = [item.bone_name for item in row.bone_names]
        if (not row.name.strip() or not names or len(set(names)) != len(names)
                or any(name not in allowed for name in names)):
            raise ValueError('每个体积域需要名称及明确、无重复的最终输出骨')
        domains.append({'name': row.name.strip(), 'proxy': row.proxy,
                        'bone_names': names, 'mask_attribute': row.mask_attribute.strip(),
                        'settings': _settings(row)})
    all_rigid_ids = set()
    for row in options.rigid:
        ids = _rigid_ids(row)
        if (not row.name.strip() or row.bone_search not in allowed or not ids
                or row.captured_source != options.source
                or row.captured_mesh != options.source.data
                or row.captured_vertex_count != len(options.source.data.vertices)
                or row.captured_topology != topology_fingerprint(options.source.data)):
            raise ValueError('刚性域需要有效输出骨及当前原源RAW选点；更换源后必须重新捕获')
        if all_rigid_ids.intersection(ids):
            raise ValueError('刚性域RAW顶点ID不可重叠')
        all_rigid_ids.update(ids)
        rigid.append({'name': row.name.strip(), 'bone_name': row.bone_search,
                      'vertex_indices': ids})
    return domains, rigid


def _related(options):
    return {obj for obj in [options.source, options.surface] + [row.proxy for row in options.domains]
            if obj is not None}


def _mask_attribute(surface, domain, *, create):
    name = domain.mask_attribute.strip()
    attribute = surface.data.attributes.get(name) if name else None
    if attribute is not None:
        if attribute.domain != 'POINT' or attribute.data_type != 'FLOAT':
            raise ValueError('该mask属性已存在但不是POINT/FLOAT，保持原属性不变')
        return attribute, False
    if not create:
        raise ValueError('当前域还没有有效mask；先写入选中顶点')
    if not name:
        name = 'C94域mask_' + uuid.uuid4().hex[:16]
        while surface.data.attributes.get(name) is not None:
            name = 'C94域mask_' + uuid.uuid4().hex[:16]
    return surface.data.attributes.new(name=name, type='FLOAT', domain='POINT'), True


def _poll_session(session):
    try:
        status = session['job'].status()
        if not isinstance(status, dict) or status.get('status') not in _STATUSES:
            raise ValueError('后台返回未知多域任务状态')
        session['status'] = dict(status)
        session['poll_error'] = None
        session['summary'] = _single._summary(session, status)
        if status.get('domain'):
            session['summary'] += '；域：' + str(status['domain'])
        options = _options(session['job'].session_scene)
        options.summary = session['summary']
        options.job_directory = str(status.get('job_directory') or '')
    except Exception as exc:
        session['poll_error'] = str(exc)
        session['summary'] = '读取状态失败，任务仍保留：' + str(exc)
        try:
            _options(session['job'].session_scene).summary = session['summary']
        except (ReferenceError, AttributeError):
            pass
        raise
    finally:
        _tag_redraw()
    return status


def _timer():
    if _SESSION is None:
        return None
    try:
        status = _poll_session(_SESSION)
    except Exception:
        return _INTERVAL
    return _INTERVAL if status['status'] == 'running' else None


def _ensure_timer():
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=_INTERVAL, persistent=False)


def _remove_timer():
    try:
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
    except Exception as exc:
        print('[C94多域蒙皮] 自有UI timer清理异常：' + str(exc))


def _release_session(reason):
    global _SESSION
    _remove_timer()
    session = _SESSION
    if session is None:
        return None
    error = None
    try:
        if session.get('candidate') is None:
            session['job'].cancel()
    except Exception as exc:
        error = str(exc)
        print('[C94多域蒙皮] 取消请求异常，core继续持有进程：' + error)
    try:
        options = _options(session['job'].session_scene)
        options.source = None
        options.rig = None
        options.surface = None
        options.domains.clear()
        options.rigid.clear()
        options.domain_index = options.rigid_index = 0
        options.job_directory = ''
        options.check_json = options.check_summary = ''
        options.summary = reason + ('；取消异常：' + error if error else '')
    except Exception as exc:
        print('[C94多域蒙皮] UI引用清理异常：' + str(exc))
    _SESSION = None
    _tag_redraw()
    return error


@persistent
def _load_pre(_unused):
    _release_session('文件切换，结束UI会话；任务文件和已创建候选保留')


class C94PLAN_BoneName(bpy.types.PropertyGroup):
    bone_name: StringProperty(name='最终输出骨', update=_changed)


class C94PLAN_Domain(bpy.types.PropertyGroup):
    name: StringProperty(name='域名称', default='体积域', update=_changed)
    proxy: PointerProperty(name='闭合代理', type=bpy.types.Object, poll=_mesh, update=_changed)
    mask_attribute: StringProperty(name='表面mask属性', update=_changed)
    bone_search: StringProperty(name='选择输出骨')
    bone_names: CollectionProperty(type=C94PLAN_BoneName)
    bone_index: IntProperty(default=0, min=0)
    cells_longest: IntProperty(name='最长轴格数', default=32, min=4, max=512, update=_changed)
    neighbor_count: EnumProperty(name='邻接', items=(('6', '6邻接', ''), ('26', '26邻接', '')), default='6', update=_changed)
    max_cells: IntProperty(name='最大格数', default=250000, min=8, max=2000000, update=_changed)
    max_memory_mb: FloatProperty(name='内存（MB）', default=512, min=1, max=32768, update=_changed)
    max_seconds: FloatProperty(name='时间预算（秒）', default=60, min=.01, max=3600, update=_changed)
    show_advanced: BoolProperty(name='高级求解设置', default=False)
    seed_radius_cells: FloatProperty(name='种子半径（格）', default=1.25, min=.25, max=4, update=_changed)
    max_seed_projection_cells: FloatProperty(name='种子投影上限（格）', default=1.25, min=0, max=4, update=_changed)
    sample_radius_cells: FloatProperty(name='采样半径（格）', default=2, min=.5, max=4, update=_changed)
    tolerance: FloatProperty(name='收敛容差', default=1e-6, min=1e-10, max=1e-2, precision=8, update=_changed)
    max_iterations: IntProperty(name='迭代上限', default=2000, min=1, max=10000, update=_changed)
    min_component_cells: IntProperty(name='最小连通域（格）', default=8, min=1, max=4096, update=_changed)


class C94PLAN_Rigid(bpy.types.PropertyGroup):
    name: StringProperty(name='刚性域名称', default='刚性域', update=_changed)
    bone_search: StringProperty(name='刚性输出骨', update=_changed)
    indices_json: StringProperty(default='[]', options={'HIDDEN'})
    count: IntProperty(default=0, min=0, options={'HIDDEN'})
    captured_source: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    captured_mesh: PointerProperty(type=bpy.types.Mesh, options={'HIDDEN'})
    captured_vertex_count: IntProperty(default=0, min=0, options={'HIDDEN'})
    captured_topology: StringProperty(options={'HIDDEN'})


class C94PLAN_Options(bpy.types.PropertyGroup):
    source: PointerProperty(name='新生产源', type=bpy.types.Object, poll=_mesh, update=_changed)
    rig: PointerProperty(name='中立骨架', type=bpy.types.Object, poll=_armature, update=_changed)
    surface: PointerProperty(name='独立采样表面', type=bpy.types.Object, poll=_mesh, update=_changed)
    candidate_name: StringProperty(name='候选名称', default='多域体积蒙皮_独立候选', update=_changed)
    surface_capture_name: StringProperty(name='表面捕获名称', default='多域蒙皮_独立采样表面')
    jobs_directory: StringProperty(name='任务目录', subtype='DIR_PATH',
        default=os.path.join(bpy.app.tempdir or tempfile.gettempdir(), 'C94VolumePlans'))
    transfer_distance_meters: FloatProperty(name='有界转移距离（米）', default=.01, min=1e-8,
        precision=6, description='明确的米制数值，不按场景单位自动变换', update=_changed)
    domains: CollectionProperty(type=C94PLAN_Domain)
    domain_index: IntProperty(default=0, min=0)
    mask_value: FloatProperty(name='写入blend值', default=1, min=0, max=1, precision=3)
    rigid: CollectionProperty(type=C94PLAN_Rigid)
    rigid_index: IntProperty(default=0, min=0)
    check_summary: StringProperty(options={'HIDDEN'})
    check_json: StringProperty(options={'HIDDEN'})
    summary: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    job_directory: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})


class C94PLAN_UL_Domains(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon='MESH_DATA')


class C94PLAN_UL_Rigid(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=f'{item.name}（RAW {item.count}点）', icon='BONE_DATA')


class C94PLAN_UL_Bones(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.bone_name, icon='BONE_DATA')


class C94PLAN_OT_Prepare(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_prepare_surface'
    bl_label = '捕获新的独立采样表面'
    bl_description = '旧表面保留；新捕获后需重新指定域mask和刚性选区'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            from . import skinning_plan
            options = _setup(context)
            _local_mesh(context, options.source, '生产源')
            if not options.surface_capture_name.strip():
                raise ValueError('填写明确的表面捕获名称')
            _finish_edit(context, {options.source, options.surface})
            surface = skinning_plan.prepare_surface(options.source, name=options.surface_capture_name.strip())
        except Exception as exc:
            self.report({'ERROR'}, '表面捕获未完成：' + str(exc))
            return {'CANCELLED'}
        try:
            options.surface = surface
            for row in options.domains:
                row.mask_attribute = ''
            for row in options.rigid:
                row.indices_json, row.count = '[]', 0
                row.captured_source, row.captured_vertex_count = None, 0
                row.captured_mesh = None
            _changed(options, context)
            options.summary = '已捕获独立表面；旧副本保留，请重新指定mask和刚性选区'
        except Exception as exc:
            self.report({'WARNING'}, '表面已创建：' + _name(surface) + '；UI记录需检查：' + str(exc))
        return {'FINISHED'}


class C94PLAN_OT_Domain(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_domain'
    bl_label = '编辑体积域列表'
    action: EnumProperty(items=(('ADD', '新增域', ''), ('REMOVE', '移除域', '')))
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            options = _setup(context)
            if self.action == 'ADD':
                name = _unique_label('体积域', options.domains)
                options.domains.add().name = name
                options.domain_index = len(options.domains)-1
            else:
                _domain(options)
                options.domains.remove(options.domain_index)
                options.domain_index = max(0, min(options.domain_index, len(options.domains)-1))
            _changed(options, context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class C94PLAN_OT_Bones(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_bones'
    bl_label = '编辑当前域输出骨'
    action: EnumProperty(items=(('ADD_NAME', '加入所选名称', ''), ('ADD_SELECTED', '加入选中的输出骨', ''),
                               ('REMOVE', '移除列表项', ''), ('CLEAR', '清空骨列表', '')))
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            options = _setup(context)
            domain = _domain(options)
            if self.action == 'CLEAR':
                domain.bone_names.clear()
                domain.bone_index = 0
            elif self.action == 'REMOVE':
                _active(domain.bone_names, domain.bone_index, '输出骨列表项')
                domain.bone_names.remove(domain.bone_index)
                domain.bone_index = max(0, min(domain.bone_index, len(domain.bone_names)-1))
            else:
                allowed = _final_outputs(options.rig)
                if self.action == 'ADD_NAME':
                    if domain.bone_search not in allowed:
                        raise ValueError('只能加入本骨架声明的实际最终输出骨')
                    names = [domain.bone_search]
                else:
                    names = [bone.name for bone in options.rig.data.bones if bone.select and bone.name in allowed]
                    if not names:
                        raise ValueError('先明确选中最终输出骨；不会自动加入全部deform骨')
                _append_bones(domain, names)
            _changed(options, context)
        except Exception as exc:
            self.report({'ERROR'}, '骨列表未更新：' + str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class C94PLAN_OT_Mask(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_mask'
    bl_label = '编辑当前域表面mask'
    action: EnumProperty(items=(('WRITE_SELECTED', '写入可见选点', ''), ('ZERO', '清零整域mask', ''),
                               ('SHOW', '选择mask正值范围', '')))
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        created, attribute, old, old_mask_name = False, None, None, None
        try:
            options = _setup(context)
            domain = _domain(options)
            old_mask_name = domain.mask_attribute
            surface = _surface(context, options)
            value = float(options.mask_value)
            if self.action == 'WRITE_SELECTED' and (not np.isfinite(value) or not 0 <= value <= 1):
                raise ValueError('mask写入值必须为有限0..1数值')
            ids = _visible_selected_ids(context, surface) if self.action == 'WRITE_SELECTED' else None
            if ids is None:
                _finish_edit(context, {surface})
            # Native mode flush cannot turn a source/shared mesh into a writable
            # surface: recheck this cheap public ownership boundary before write.
            surface = _surface(context, options)
            attribute, created = _mask_attribute(surface, domain, create=self.action == 'WRITE_SELECTED')
            old = np.empty(len(attribute.data), dtype=np.float32)
            attribute.data.foreach_get('value', old)
            if len(old) != len(surface.data.vertices):
                raise ValueError('mask属性点数与采样表面不一致')
            if self.action != 'ZERO' and (not np.isfinite(old).all() or (old < 0).any() or (old > 1).any()):
                raise ValueError('mask必须为逐点有限0..1值，保持原数据供检查')
            if self.action == 'SHOW':
                visible = np.flatnonzero(old > 0)
                if not len(visible):
                    raise ValueError('当前域mask没有正值点')
                _activate_surface(context, surface)
                bpy.ops.object.mode_set(mode='EDIT')
                import bmesh
                bm = bmesh.from_edit_mesh(surface.data)
                bm.verts.ensure_lookup_table()
                for face in bm.faces:
                    face.select_set(False)
                for edge in bm.edges:
                    edge.select_set(False)
                for vertex in bm.verts:
                    vertex.select_set(False)
                bm.select_mode = {'VERT'}
                context.tool_settings.mesh_select_mode = (True, False, False)
                for index in visible:
                    bm.verts[int(index)].hide_set(False)
                    bm.verts[int(index)].select_set(True)
                bm.select_flush_mode()
                bmesh.update_edit_mesh(surface.data, loop_triangles=False, destructive=False)
                options.summary = f'当前域mask正值范围：{len(visible)}点；已选择独立表面几何'
                return {'FINISHED'}
            values = old.copy()
            if self.action == 'ZERO':
                values.fill(0)
            else:
                values[ids] = value
            attribute.data.foreach_set('value', values)
            surface.data.update()
            domain.mask_attribute = attribute.name
            _changed(options, context)
            options.summary = ('已清零当前域mask' if self.action == 'ZERO' else
                               f'已在独立表面写入{len(ids)}个明确选点，blend={options.mask_value:g}')
        except Exception as exc:
            if attribute is not None and self.action != 'SHOW':
                try:
                    if created:
                        surface.data.attributes.remove(attribute)
                    elif old is not None:
                        attribute.data.foreach_set('value', old)
                        surface.data.update()
                    if old_mask_name is not None:
                        domain.mask_attribute = old_mask_name
                except Exception as rollback:
                    self.report({'WARNING'}, '独立表面mask回滚需检查：' + str(rollback))
            self.report({'ERROR'}, 'mask操作未完成：' + str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


def _activate_surface(context, surface):
    surface.hide_set(False)
    surface.hide_viewport = False
    for obj in tuple(context.selected_objects):
        obj.select_set(False)
    surface.select_set(True)
    context.view_layer.objects.active = surface


class C94PLAN_OT_Rigid(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_rigid'
    bl_label = '编辑刚性RAW域'
    action: EnumProperty(items=(('ADD', '新增刚性域', ''), ('REMOVE', '移除刚性域', ''),
                               ('CAPTURE', '捕获生产源可见选点', ''), ('CLEAR', '清空选区记录', '')))
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            options = _setup(context)
            if self.action == 'ADD':
                name = _unique_label('刚性域', options.rigid)
                options.rigid.add().name = name
                options.rigid_index = len(options.rigid)-1
            elif self.action == 'REMOVE':
                _rigid(options)
                options.rigid.remove(options.rigid_index)
                options.rigid_index = max(0, min(options.rigid_index, len(options.rigid)-1))
            else:
                row = _rigid(options)
                if self.action == 'CLEAR':
                    row.indices_json, row.count = '[]', 0
                    row.captured_source, row.captured_vertex_count = None, 0
                    row.captured_mesh = None
                    row.captured_topology = ''
                else:
                    ids = _visible_selected_ids(context, options.source)
                    for other in options.rigid:
                        if other != row and other.captured_source == options.source and set(ids).intersection(_rigid_ids(other)):
                            raise ValueError('刚性选点与域“' + other.name + '”重叠，原记录保留')
                    row.indices_json, row.count = json.dumps(ids), len(ids)
                    row.captured_source = options.source
                    row.captured_mesh = options.source.data
                    row.captured_vertex_count = len(options.source.data.vertices)
                    row.captured_topology = topology_fingerprint(options.source.data)
            _changed(options, context)
        except Exception as exc:
            self.report({'ERROR'}, '刚性域未更新：' + str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class C94PLAN_OT_Check(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_check'
    bl_label = '检查计划覆盖与有界转移'

    def execute(self, context):
        options, report = None, None
        try:
            from . import skinning_plan
            options = _setup(context)
            _finish_edit(context, _related(options))
            domains, rigid = _payload(options)
            report = skinning_plan.check(options.source, options.rig, options.surface, domains, rigid,
                                         transfer_distance_meters=float(options.transfer_distance_meters))
            options.check_json = json.dumps(report, ensure_ascii=False, allow_nan=False)
            options.check_summary = f'{len(domains)}个体积域 / {len(rigid)}个刚性域；完整检查结果已缓存'
            if report.get('ok') is False:
                options.check_summary = '计划检查未通过；完整结果已缓存'
                raise ValueError(_single._error_text(report.get('error') or '请检查完整报告'))
            options.summary = '计划检查完成；未创建权重或候选'
        except Exception as exc:
            if options is not None:
                options.check_summary = '本次计划检查未通过：' + str(exc)
                if report is None:
                    options.check_json = json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)
                options.summary = '计划检查失败；没有启动求解或创建权重'
            self.report({'ERROR'}, '计划检查失败：' + str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class C94PLAN_OT_Start(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_start'
    bl_label = '启动后台多域求解'

    def execute(self, context):
        global _SESSION
        try:
            from . import skinning_plan
            options = _setup(context)
            _finish_edit(context, _related(options))
            domains, rigid = _payload(options)
            if not options.candidate_name.strip() or not options.jobs_directory.strip():
                raise ValueError('需要明确候选名称和任务目录')
            directory = os.path.abspath(bpy.path.abspath(options.jobs_directory))
            job = skinning_plan.start(options.source, options.rig, options.surface, domains, rigid,
                jobs_directory=directory, transfer_distance_meters=float(options.transfer_distance_meters))
        except Exception as exc:
            self.report({'ERROR'}, '多域任务未启动：' + str(exc))
            return {'CANCELLED'}
        _SESSION = {'job': job, 'candidate_name': options.candidate_name.strip(), 'candidate': None,
                    'status': {'status': 'running'}, 'cancel_requested': False, 'poll_error': None,
                    'summary': '已启动后台多域求解；原生产源和已有权重保持'}
        options.summary = _SESSION['summary']
        options.job_directory = ''
        try:
            _ensure_timer()
        except Exception as exc:
            self.report({'WARNING'}, '任务已启动，请用刷新按钮查看：' + str(exc))
        _tag_redraw()
        return {'FINISHED'}


class C94PLAN_OT_Poll(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_poll'
    bl_label = '刷新多域任务状态'

    def execute(self, context):
        try:
            _poll_session(_session(context))
        except Exception as exc:
            self.report({'ERROR'}, '无法读取状态，任务仍保留：' + str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class C94PLAN_OT_Cancel(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_cancel'
    bl_label = '请求取消多域求解'

    def execute(self, context):
        try:
            session = _session(context)
            if session.get('candidate') is not None:
                raise ValueError('候选已创建；结束会话会保留该候选')
            session['cancel_requested'] = True
            session['job'].cancel()
            session['summary'] = '已请求取消；结果不再允许创建候选，core继续监控自有进程'
            _options(session['job'].session_scene).summary = session['summary']
        except Exception as exc:
            self.report({'ERROR'}, '取消请求异常，任务仍保留：' + str(exc))
            return {'CANCELLED'}
        try:
            _ensure_timer()
        except Exception as exc:
            self.report({'WARNING'}, '已请求取消，请用刷新按钮查看：' + str(exc))
        _tag_redraw()
        return {'FINISHED'}


class C94PLAN_OT_Apply(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_apply'
    bl_label = '创建独立多域蒙皮候选'

    def execute(self, context):
        try:
            session = _session(context)
            if session['cancel_requested'] or session.get('candidate') is not None:
                raise ValueError('结果已取消或已经创建过候选')
            status = _poll_session(session)
            if status['status'] != 'succeeded':
                raise ValueError('只能应用完整成功且未取消的本会话结果')
            candidate = session['job'].apply(name=session['candidate_name'])
        except Exception as exc:
            self.report({'ERROR'}, '候选创建未完成，任务保留供检查：' + str(exc))
            return {'CANCELLED'}
        session['candidate'] = candidate
        session['status'] = {'status': 'applied', 'job_directory': status.get('job_directory', '')}
        session['summary'] = '已创建独立候选：' + _name(candidate) + '；变形与艺术质量尚未验收'
        _options(session['job'].session_scene).summary = session['summary']
        _remove_timer()
        _tag_redraw()
        return {'FINISHED'}


class C94PLAN_OT_Finish(bpy.types.Operator):
    bl_idname = 'c94_workflow.skinning_plan_finish'
    bl_label = '结束多域会话'

    def execute(self, context):
        try:
            _session(context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        error = _release_session('多域UI会话已结束；采样表面、任务文件和候选保留')
        if error:
            self.report({'WARNING'}, 'UI已释放，core仍管理取消中的任务：' + error)
        return {'FINISHED'}


class C94PLAN_PT_Plan(bpy.types.Panel):
    bl_idname = 'C94PLAN_PT_plan'
    bl_label = '多域蒙皮计划'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_options = {'DEFAULT_CLOSED'}
    bl_ui_units_x = 28

    def draw(self, context):
        options = _options(context.scene)
        layout = self.layout
        setup = layout.column()
        setup.enabled = _SESSION is None
        setup.prop(options, 'source')
        setup.prop(options, 'rig')
        setup.prop(options, 'surface')
        setup.prop(options, 'surface_capture_name')
        setup.operator('c94_workflow.skinning_plan_prepare_surface', icon='DUPLICATE')
        setup.prop(options, 'candidate_name')
        setup.prop(options, 'jobs_directory')
        setup.prop(options, 'transfer_distance_meters')
        setup.label(text='体积域：正mask点参与，mask值为blend')
        setup.template_list('C94PLAN_UL_Domains', '', options, 'domains', options, 'domain_index', rows=3)
        row = setup.row(align=True)
        row.operator('c94_workflow.skinning_plan_domain', text='加域').action = 'ADD'
        row.operator('c94_workflow.skinning_plan_domain', text='移域').action = 'REMOVE'
        if 0 <= options.domain_index < len(options.domains):
            domain = options.domains[options.domain_index]
            box = setup.box()
            box.prop(domain, 'name')
            box.prop(domain, 'proxy')
            box.prop(domain, 'mask_attribute')
            box.prop(options, 'mask_value')
            row = box.row(align=True)
            row.operator('c94_workflow.skinning_plan_mask', text='写入选点').action = 'WRITE_SELECTED'
            row.operator('c94_workflow.skinning_plan_mask', text='整域清零').action = 'ZERO'
            box.operator('c94_workflow.skinning_plan_mask', text='选择正mask几何').action = 'SHOW'
            if options.rig:
                row = box.row(align=True)
                row.prop_search(domain, 'bone_search', options.rig.data, 'bones')
                row.operator('c94_workflow.skinning_plan_bones', text='加入').action = 'ADD_NAME'
            box.operator('c94_workflow.skinning_plan_bones', text='加入选中的最终输出骨').action = 'ADD_SELECTED'
            box.template_list('C94PLAN_UL_Bones', '', domain, 'bone_names', domain, 'bone_index', rows=3)
            row = box.row(align=True)
            row.operator('c94_workflow.skinning_plan_bones', text='移除骨').action = 'REMOVE'
            row.operator('c94_workflow.skinning_plan_bones', text='清空骨').action = 'CLEAR'
            for name in ('cells_longest', 'neighbor_count', 'max_cells', 'max_memory_mb', 'max_seconds'):
                box.prop(domain, name)
            box.prop(domain, 'show_advanced')
            if domain.show_advanced:
                for name in ('seed_radius_cells', 'max_seed_projection_cells', 'sample_radius_cells',
                             'tolerance', 'max_iterations', 'min_component_cells'):
                    box.prop(domain, name)
        setup.label(text='刚性域：原生产源RAW顶点ID，不与其它刚性域重叠')
        setup.template_list('C94PLAN_UL_Rigid', '', options, 'rigid', options, 'rigid_index', rows=3)
        row = setup.row(align=True)
        row.operator('c94_workflow.skinning_plan_rigid', text='加刚性域').action = 'ADD'
        row.operator('c94_workflow.skinning_plan_rigid', text='移刚性域').action = 'REMOVE'
        if 0 <= options.rigid_index < len(options.rigid):
            rigid = options.rigid[options.rigid_index]
            box = setup.box()
            box.prop(rigid, 'name')
            if options.rig:
                box.prop_search(rigid, 'bone_search', options.rig.data, 'bones')
            box.label(text=f'已捕获RAW顶点：{rigid.count}（仅缓存）')
            box.operator('c94_workflow.skinning_plan_rigid', text='捕获源可见选点').action = 'CAPTURE'
            box.operator('c94_workflow.skinning_plan_rigid', text='清空选区记录').action = 'CLEAR'
        setup.label(text='体积目标=全部RAW减刚性域；正mask点blend总和须为1')
        setup.operator('c94_workflow.skinning_plan_check', icon='CHECKMARK')
        setup.operator('c94_workflow.skinning_plan_start', icon='PLAY')
        if options.check_summary:
            layout.label(text='上次检查：' + options.check_summary)
        if _SESSION is not None:
            job = _SESSION['job']
            box = layout.box()
            box.label(text='任务真实源：' + _name(job.source))
            box.label(text='骨架：' + _name(job.rig) + '；表面：' + _name(job.proxy))
            box.label(text='所属场景：' + _name(job.session_scene))
            box.label(text=_SESSION.get('summary', '等待后台状态'))
            controls = box.column()
            controls.enabled = job.session_scene == context.scene
            controls.operator('c94_workflow.skinning_plan_poll', icon='FILE_REFRESH')
            cancel = controls.row()
            cancel.enabled = _SESSION.get('candidate') is None
            cancel.operator('c94_workflow.skinning_plan_cancel', icon='CANCEL')
            apply = controls.row()
            apply.enabled = (_SESSION['status'].get('status') == 'succeeded' and not _SESSION['cancel_requested']
                             and not _SESSION.get('poll_error') and _SESSION.get('candidate') is None)
            apply.operator('c94_workflow.skinning_plan_apply', icon='DUPLICATE')
            controls.operator('c94_workflow.skinning_plan_finish', icon='X')
        elif options.summary:
            layout.label(text=options.summary)
        if options.job_directory:
            layout.label(text='本次目录：' + options.job_directory)
        layout.label(text='仅修改独立采样表面mask；不写生产源几何或已有权重')
        layout.label(text='覆盖与有界转移由core核对；失败不提交部分蒙皮')
        layout.label(text='面部、服装、便携与艺术质量仍需各自制作和验收')


_CLASSES = (C94PLAN_BoneName, C94PLAN_Domain, C94PLAN_Rigid, C94PLAN_Options,
            C94PLAN_UL_Domains, C94PLAN_UL_Rigid, C94PLAN_UL_Bones,
            C94PLAN_OT_Prepare, C94PLAN_OT_Domain, C94PLAN_OT_Bones, C94PLAN_OT_Mask,
            C94PLAN_OT_Rigid, C94PLAN_OT_Check, C94PLAN_OT_Start, C94PLAN_OT_Poll,
            C94PLAN_OT_Cancel, C94PLAN_OT_Apply, C94PLAN_OT_Finish, C94PLAN_PT_Plan)


def register():
    global _C94_OPTIONS_OWNED
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('该面板属性已由其他实例持有；原属性保持')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94PLAN_Options))
        _C94_OPTIONS_OWNED = True
        if _load_pre not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(_load_pre)
    except Exception:
        unregister()
        raise


def unregister():
    _release_session('工具卸载，UI会话结束；文件和已创建候选保留')
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)
    if hasattr(bpy.types.Scene, OPTIONS):
        _release_workspace_options()
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()


_C94_OPTIONS_OWNED = False

def _release_workspace_options():
    global _C94_OPTIONS_OWNED
    prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
    if (_C94_OPTIONS_OWNED and prop and prop.type == 'POINTER'
            and prop.fixed_type == C94PLAN_Options.bl_rna):
        delattr(bpy.types.Scene, OPTIONS)
    _C94_OPTIONS_OWNED = False
