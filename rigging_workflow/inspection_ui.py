"""Native positioning inspection UI; domain edits are shared with CLI callers."""
import json
from contextlib import contextmanager

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty
from mathutils import Vector
from bpy_extras import view3d_utils

from . import editing, positioning, inspection_workspace as views

_REGISTERED = []
_BUSY = False
_LAST_ERROR = ''


@contextmanager
def object_mode(context):
    original = context.object
    mode = original.mode if original else 'OBJECT'
    if original and mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    try:
        yield
    finally:
        if original and original.name in context.view_layer.objects and mode != 'OBJECT':
            context.view_layer.objects.active = original
            original.select_set(True)
            if original.mode != mode:
                bpy.ops.object.mode_set(mode=mode)


def settings(context):
    return context.scene.c94_inspection


def target_keys(context):
    # Geometry is selected after the destination points have been remembered.
    # Do not ask the endpoint selector to interpret mesh Edit Mode.
    selected = [] if context.mode == 'EDIT_MESH' else editing.selection(context)
    keys = selected or json.loads(settings(context).targets_json or '[]')
    if not keys:
        raise ValueError('先选定位标记或参考骨端点，再点击“记住所选定位点”')
    return keys


def _helper(scene, kind, location=None, slot=0):
    state = positioning.read_state(scene)
    obj = next((o for o in scene.objects if o.get('c94_inspection_helper') == kind
                and o.get('c94_inspection_slot', 0) == slot
                and o.get(positioning.OWNER_KEY) == state['owner']), None)
    if obj is None:
        obj = bpy.data.objects.new('定位检查-'+kind, None)
        bpy.data.collections[state['work_collection']].objects.link(obj)
        obj['c94_inspection_helper'] = kind
        obj['c94_inspection_slot'] = slot
        obj[positioning.OWNER_KEY] = state['owner']
        obj.show_in_front = True
        obj.hide_render = True
    if location is not None:
        obj.location = location
    obj.hide_set(False)
    return obj


def _hide_helpers(scene, *kinds):
    state = positioning.read_state(scene)
    for obj in scene.objects:
        if obj.get('c94_inspection_helper') in kinds and obj.get(positioning.OWNER_KEY) == state['owner']:
            obj.hide_set(True)


def radius_preview(context, keys, points=None):
    props = settings(context)
    _hide_helpers(context.scene, 'radius_preview')
    if props.radius <= 0:
        return
    points = points if points is not None else editing.positions(context.scene, keys)
    state = positioning.read_state(context.scene)
    _, lookup, _ = editing._groups(context.scene, state)
    grouped = {}
    for key, point in points.items():
        grouped.setdefault(lookup[key], tuple(point))
    centers = set(grouped.values())
    if props.realtime_mirror:
        plane = state['editing']['plane']
        origin, normal = Vector(plane['origin']), Vector(plane['normal']).normalized()
        centers.update(tuple(Vector(p)-2*normal*(Vector(p)-origin).dot(normal))
                       for p in tuple(centers))
    for slot, point in enumerate(sorted(centers)):
        obj = _helper(context.scene, 'radius_preview', point, slot)
        obj.empty_display_type = 'SPHERE'
        obj.empty_display_size = props.radius
        obj.color = (1.0, .65, .1, 1.0)


def _surface(context):
    from .inspection_geometry import SurfaceTarget
    props = settings(context)
    state = positioning.read_state(context.scene)
    obj = props.surface or context.scene.objects.get(state['target_mesh'])
    if not obj:
        raise ValueError('选择明确的目标表面')
    return SurfaceTarget(obj, selected_only=props.surface_selected,
                         depsgraph=context.evaluated_depsgraph_get())


def _run(operator, context, action):
    global _BUSY, _LAST_ERROR
    _BUSY = True
    try:
        with object_mode(context):
            result = action()
        _LAST_ERROR = ''
        skipped = result.get('skipped_locked_or_unpaired', [])
        if skipped:
            operator.report({'WARNING'}, f'已应用 {result.get("changed",0)} 处；跳过 {len(skipped)} 个锁定或无对应点')
        else:
            operator.report({'INFO'}, '完成：'+str(result.get('label', result.get('changed', '定位检查'))))
        return {'FINISHED'}
    except (ValueError, RuntimeError, KeyError) as exc:
        _LAST_ERROR = str(exc)
        operator.report({'ERROR'}, str(exc))
        return {'CANCELLED'}
    finally:
        _BUSY = False


class C94INS_Properties(bpy.types.PropertyGroup):
    preset: EnumProperty(name='检查部位', items=views.PRESETS, default='FACE')
    surface: PointerProperty(name='贴合模型', type=bpy.types.Object, poll=lambda s,o:o.type=='MESH')
    surface_selected: BoolProperty(name='仅选中面区域', default=False)
    offset: FloatProperty(name='表面内外偏移', subtype='DISTANCE', unit='LENGTH', default=0.0)
    center_source: PointerProperty(name='取中心的网格', type=bpy.types.Object, poll=lambda s,o:o.type=='MESH')
    center_selected: BoolProperty(name='仅选中几何', default=True)
    center_mode: EnumProperty(name='中心方式', items=[('MEAN','平均中心','所选唯一顶点的平均位置'),('BBOX','包围盒中心','所选几何外侧范围的中心')])
    targets_json: StringProperty(default='[]', options={'HIDDEN'})
    preview_json: StringProperty(default='', options={'HIDDEN'})
    realtime_mirror: BoolProperty(name='实时镜像联动', default=False)
    auto_sync: BoolProperty(name='对象模式自动同步', default=True)
    radius: FloatProperty(name='邻域范围（0为局部）', min=0.0, default=0.0, subtype='DISTANCE', unit='LENGTH')
    strength: FloatProperty(name='邻域强度', min=0.0, max=1.0, default=1.0)
    plane_origin: FloatVectorProperty(name='镜像平面位置', subtype='TRANSLATION', size=3)
    plane_normal: FloatVectorProperty(name='镜像平面法线', size=3, default=(1,0,0))
    isolate: BoolProperty(name='局部隔离', default=True)
    xray: BoolProperty(name='骨骼透视', default=True)


class C94INS_OT_Workspace(bpy.types.Operator):
    bl_idname = 'c94_workflow.inspection_workspace'
    bl_label = '进入定位检查'
    action: EnumProperty(items=[('ENTER','进入',''),('LEAVE','返回','')])

    def execute(self, context):
        def action():
            if self.action == 'LEAVE':
                return views.leave(context)
            with object_mode(context):
                editing.ensure(context.scene)
                editing.sync(context.scene)
            state = positioning.read_state(context.scene)
            plane = state.get('editing',{}).get('plane',{})
            settings(context).plane_origin = plane.get('origin',(state['mirror_plane_x'],0,0))
            settings(context).plane_normal = plane.get('normal',(1,0,0))
            result = views.enter(context)
            # Native workspace switching completes on the next UI event loop;
            # enter schedules its first focus/display setup in that context.
            return result
        # Workspaces own restoration of the original context.
        try:
            action()
            return {'FINISHED'}
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}


class C94INS_OT_Focus(bpy.types.Operator):
    bl_idname = 'c94_workflow.inspection_focus'
    bl_label = '聚焦检查部位'
    def execute(self, context):
        try:
            views.focus(context, settings(context).preset)
            views.set_isolate(context, settings(context).isolate)
            views.set_xray(context, settings(context).xray)
            return {'FINISHED'}
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}


class C94INS_OT_Edit(bpy.types.Operator):
    bl_idname = 'c94_workflow.inspection_edit'
    bl_label = '定位调整'
    bl_options = {'REGISTER','UNDO'}
    action: EnumProperty(items=[(s,s,'') for s in ('REMEMBER','SYNC','LOCK','UNLOCK','MIRROR_L','MIRROR_R','PLANE','RESTORE','RADIUS')])
    def execute(self, context):
        keys = []
        if self.action not in {'SYNC','RESTORE','PLANE'}:
            try: keys = target_keys(context)
            except ValueError as exc:
                self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        def action():
            props = settings(context)
            if self.action == 'REMEMBER':
                props.targets_json = json.dumps(keys,ensure_ascii=False)
                radius_preview(context,keys)
                return {'label':f'已记住{len(keys)}个定位端点'}
            if self.action == 'RESTORE': return editing.restore(context.scene)
            if self.action == 'PLANE':
                return editing.set_mirror_plane(context.scene,props.plane_origin,props.plane_normal)
            editing.sync(context.scene,mirror=props.realtime_mirror)
            if self.action == 'SYNC': return {'label':'标记与参考骨已同步'}
            if self.action in {'LOCK','UNLOCK'}:
                return editing.set_locked(context.scene,keys,self.action=='LOCK')
            if self.action.startswith('MIRROR_'):
                return editing.mirror(context.scene,keys,'L_TO_R' if self.action=='MIRROR_L' else 'R_TO_L')
            if self.action=='RADIUS':
                radius_preview(context,keys);return {'label':'已显示邻域范围'}
        return _run(self,context,action)


class C94INS_OT_Nearest(bpy.types.Operator):
    bl_idname = 'c94_workflow.inspection_nearest'
    bl_label = '吸附最近表面'
    bl_options = {'REGISTER','UNDO'}
    def execute(self, context):
        try:
            keys = target_keys(context)
            surface = _surface(context)  # Capture Edit Mode face selection before changing mode.
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        def action():
            props = settings(context)
            editing.sync(context.scene,mirror=props.realtime_mirror)
            targets = {key:surface.nearest(point,props.offset)['position']
                       for key,point in editing.positions(context.scene,keys).items()}
            return editing.apply(context.scene,targets,mirror=props.realtime_mirror,
                radius=props.radius,strength=props.strength,label='最近表面贴合')
        return _run(self,context,action)


class C94INS_OT_Center(bpy.types.Operator):
    bl_idname = 'c94_workflow.inspection_center'
    bl_label = '几何中心预览'
    action: EnumProperty(items=[('PREVIEW','预览',''),('APPLY','应用',''),('CANCEL','取消','')])
    bl_options = {'REGISTER','UNDO'}
    def execute(self, context):
        props = settings(context)
        if self.action == 'CANCEL':
            props.preview_json='';_hide_helpers(context.scene,'center_preview');return {'FINISHED'}
        try:
            if self.action == 'PREVIEW':
                from .inspection_geometry import selection_center
                objects = [props.center_source] if props.center_source else [o for o in context.selected_objects if o.type=='MESH']
                result = selection_center(objects,props.center_mode,props.center_selected,context.evaluated_depsgraph_get())
                props.preview_json=json.dumps(result,ensure_ascii=False)
                obj=_helper(context.scene,'center_preview',result['position'])
                obj.empty_display_type='PLAIN_AXES';obj.empty_display_size=.015
                obj.color=(1,.5,.05,1)
                return {'FINISHED'}
            result=json.loads(props.preview_json or '{}')
            if 'position' not in result:raise ValueError('先预览选中几何的中心')
            keys=target_keys(context)
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        def action():
            editing.sync(context.scene,mirror=props.realtime_mirror)
            positions=editing.positions(context.scene,keys)
            # Translate a multi-selection as a group; never collapse multiple bone ends.
            pivot=sum((Vector(p) for p in positions.values()),Vector())/len(positions)
            delta=Vector(result['position'])-pivot
            report=editing.apply(context.scene,{k:list(Vector(p)+delta) for k,p in positions.items()},
                mirror=props.realtime_mirror,radius=props.radius,strength=props.strength,label='选中几何中心')
            props.preview_json='';_hide_helpers(context.scene,'center_preview')
            return report
        return _run(self,context,action)


def _mouse_view(context,event):
    for area in context.screen.areas:
        if area.type!='VIEW_3D':continue
        for region in area.regions:
            if region.type=='WINDOW' and region.x<=event.mouse_x<region.x+region.width and region.y<=event.mouse_y<region.y+region.height:
                return area,region,area.spaces.active.region_3d,(event.mouse_x-region.x,event.mouse_y-region.y)
    return None


class C94INS_OT_Drag(bpy.types.Operator):
    bl_idname='c94_workflow.inspection_drag'
    bl_label='拖动定位点'
    bl_options={'REGISTER','UNDO','BLOCKING'}
    surface:BoolProperty(default=False)

    def invoke(self,context,event):
        global _BUSY
        try:
            self.keys=target_keys(context)
            self.target=_surface(context) if self.surface else None
            self.original_object=context.object
            self.original_mode=context.object.mode if context.object else 'OBJECT'
            if self.original_mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
            editing.sync(context.scene,mirror=settings(context).realtime_mirror)
            self.saved=editing.snapshot(context.scene)
            self.initial=editing.positions(context.scene,self.keys)
            self.pivot=sum((Vector(p) for p in self.initial.values()),Vector())/len(self.initial)
            self.dragging=False;self.pending=None;self.anchor=None
            _BUSY=True
            radius_preview(context,self.keys,self.initial)
            context.window_manager.modal_handler_add(self)
            context.workspace.status_text_set('在三维视图中左键拖动；释放应用；Esc/右键取消')
            return {'RUNNING_MODAL'}
        except (ValueError,RuntimeError) as exc:
            if getattr(self,'original_object',None) and getattr(self,'original_mode','OBJECT') != 'OBJECT':
                context.view_layer.objects.active=self.original_object
                if self.original_object.mode!=self.original_mode:
                    bpy.ops.object.mode_set(mode=self.original_mode)
            _BUSY=False;self.report({'ERROR'},str(exc));return {'CANCELLED'}

    def _finish(self,context,cancel=False):
        global _BUSY
        try:
            editing.restore(context.scene,self.saved)
            if not cancel and self.pending:
                props=settings(context)
                editing.apply(context.scene,self.pending,mirror=props.realtime_mirror,
                    radius=props.radius,strength=props.strength,label='表面拖动' if self.surface else '局部拖动')
            if self.original_object and self.original_object.name in context.view_layer.objects and self.original_mode!='OBJECT':
                context.view_layer.objects.active=self.original_object
                self.original_object.select_set(True)
                bpy.ops.object.mode_set(mode=self.original_mode)
        finally:
            _hide_helpers(context.scene, 'radius_preview')
            context.workspace.status_text_set(None)
            _BUSY=False
        return {'CANCELLED'} if cancel else {'FINISHED'}

    def modal(self,context,event):
        if event.type in {'ESC','RIGHTMOUSE'}:return self._finish(context,True)
        view=_mouse_view(context,event)
        if event.type=='LEFTMOUSE' and event.value=='PRESS' and view:
            self.dragging=True
            _,region,rv3d,coord=view
            self.anchor=view3d_utils.region_2d_to_location_3d(region,rv3d,coord,self.pivot)
        if event.type=='LEFTMOUSE' and event.value=='RELEASE' and self.dragging:
            return self._finish(context,self.pending is None)
        if view and self.dragging and event.type in {'MOUSEMOVE','LEFTMOUSE'}:
            _,region,rv3d,coord=view
            try:
                if self.surface:
                    ray_origin=view3d_utils.region_2d_to_origin_3d(region,rv3d,coord,clamp=100)
                    ray_dir=view3d_utils.region_2d_to_vector_3d(region,rv3d,coord)
                    hit=self.target.ray(ray_origin,ray_dir,settings(context).offset)
                    if hit is None:return {'RUNNING_MODAL'}
                    delta=Vector(hit['position'])-self.pivot
                else:
                    delta=view3d_utils.region_2d_to_location_3d(region,rv3d,coord,self.pivot)-self.anchor
                self.pending={k:list(Vector(p)+delta) for k,p in self.initial.items()}
                editing.restore(context.scene,self.saved)
                props=settings(context)
                editing.apply(context.scene,self.pending,mirror=props.realtime_mirror,
                    radius=props.radius,strength=props.strength,label='拖动预览',checkpoint=False)
                radius_preview(context,self.keys,self.initial)
                for area in context.screen.areas:area.tag_redraw()
            except (ValueError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc));return self._finish(context,True)
        return {'RUNNING_MODAL'}


class C94INS_OT_Compare(bpy.types.Operator):
    bl_idname='c94_workflow.inspection_compare'
    bl_label='调整前后对照'
    action:EnumProperty(items=[('CAPTURE','设为对照',''),('TOGGLE','显示/隐藏','')])
    def execute(self,context):
        def action():
            state=editing.ensure(context.scene)
            existing=[o for o in context.scene.objects if o.get('c94_inspection_helper')=='comparison'
                      and o.get(positioning.OWNER_KEY)==state['owner']]
            if self.action=='TOGGLE':
                if not existing:raise ValueError('先把当前参考骨设为对照')
                for obj in existing:obj.hide_set(not obj.hide_get())
                return {'label':'对照显隐已切换'}
            for obj in existing:
                data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
                if data.users==0:bpy.data.armatures.remove(data)
            source=context.scene.objects[state['fitted_reference']]
            obj=source.copy();obj.data=source.data.copy()
            bpy.data.collections[state['work_collection']].objects.link(obj)
            obj.name='定位检查-调整前对照';obj['c94_inspection_helper']='comparison'
            obj.show_in_front=True;obj.hide_render=True;obj.hide_select=True
            for pb in obj.pose.bones:
                pb.color.palette='CUSTOM'
                pb.color.custom.normal=(1.0,.45,.05)
                pb.color.custom.select=(1.0,.45,.05)
                pb.color.custom.active=(1.0,.45,.05)
            obj.hide_set(False)
            return {'label':'已保存橙色对照骨'}
        return _run(self,context,action)


class C94INS_PT_Inspection(bpy.types.Panel):
    bl_label='定位检查与精调'
    bl_idname='C94INS_PT_inspection'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='角色绑定';bl_order=5
    def draw(self,context):
        layout=self.layout
        if not positioning.read_state(context.scene):
            layout.label(text='先准备角色定位工作副本');return
        props=settings(context)
        view_status=views.status(context)
        if view_status.get('pending'):
            layout.label(text='正在准备检查工作区…',icon='TIME')
        if view_status.get('error'):
            layout.label(text='工作区：'+str(view_status['error'])[:48],icon='ERROR')
        row=layout.row(align=True)
        row.operator('c94_workflow.inspection_workspace',text='进入检查').action='ENTER'
        row.operator('c94_workflow.inspection_workspace',text='返回原工作区').action='LEAVE'
        row=layout.row(align=True);row.prop(props,'preset',text='');row.operator('c94_workflow.inspection_focus',text='聚焦')
        row=layout.row(align=True);row.prop(props,'isolate');row.prop(props,'xray')
        layout.label(text='切换显示选项后点击聚焦')
        box=layout.box();box.label(text='1 选择标记或参考骨端点')
        box.operator('c94_workflow.inspection_edit',text='记住所选定位点').action='REMEMBER'
        box.label(text=f'已记住 {len(json.loads(props.targets_json or "[]"))} 个端点')
        row=box.row(align=True)
        row.operator('c94_workflow.inspection_edit',text='同步手调').action='SYNC'
        row.operator('c94_workflow.inspection_drag',text='自由拖动').surface=False
        box.prop(props,'auto_sync')
        if context.mode!='OBJECT':box.label(text='原生骨编辑完成后同步，或使用自由拖动')
        row=box.row(align=True)
        row.operator('c94_workflow.inspection_edit',text='锁定').action='LOCK'
        row.operator('c94_workflow.inspection_edit',text='解锁').action='UNLOCK'
        box.prop(props,'radius');box.prop(props,'strength')
        box.operator('c94_workflow.inspection_edit',text='预览联动范围').action='RADIUS'
        box=layout.box();box.label(text='2 镜像')
        row=box.row(align=True)
        row.operator('c94_workflow.inspection_edit',text='左 → 右').action='MIRROR_L'
        row.operator('c94_workflow.inspection_edit',text='右 → 左').action='MIRROR_R'
        box.prop(props,'realtime_mirror')
        box.prop(props,'plane_origin');box.prop(props,'plane_normal')
        box.operator('c94_workflow.inspection_edit',text='应用镜像平面').action='PLANE'
        box=layout.box();box.label(text='3 贴合明确的表面')
        box.prop(props,'surface');box.prop(props,'surface_selected');box.prop(props,'offset')
        row=box.row(align=True)
        row.operator('c94_workflow.inspection_nearest',text='最近表面')
        row.operator('c94_workflow.inspection_drag',text='点击 / 拖动贴合').surface=True
        box=layout.box();box.label(text='4 几何中心')
        box.label(text='先记住定位点，再选择网格区域')
        box.prop(props,'center_source');box.prop(props,'center_selected');box.prop(props,'center_mode')
        row=box.row(align=True)
        row.operator('c94_workflow.inspection_center',text='预览').action='PREVIEW'
        row.operator('c94_workflow.inspection_center',text='应用').action='APPLY'
        row.operator('c94_workflow.inspection_center',text='取消').action='CANCEL'
        box=layout.box();box.label(text='5 对照与恢复')
        row=box.row(align=True)
        row.operator('c94_workflow.inspection_compare',text='当前设为对照').action='CAPTURE'
        row.operator('c94_workflow.inspection_compare',text='显示对照').action='TOGGLE'
        box.operator('c94_workflow.inspection_edit',text='恢复上次调整').action='RESTORE'
        if _LAST_ERROR:
            layout.label(text='同步提示：'+_LAST_ERROR[:48],icon='ERROR')


def _watch():
    global _LAST_ERROR, _BUSY
    if not _REGISTERED:return None
    context=bpy.context
    if _BUSY or not context.scene or context.mode!='OBJECT' or not hasattr(context.scene,'c94_inspection'):
        return .3
    props=settings(context)
    if not props.auto_sync or not views.is_active(context):return .3
    state=positioning.read_state(context.scene)
    if not state or not state.get('editing'):return .3
    try:
        _BUSY=True
        editing.sync(context.scene,mirror=props.realtime_mirror)
    except (ValueError,RuntimeError) as exc:
        _LAST_ERROR=str(exc);props.auto_sync=False
    finally:_BUSY=False
    return .3


_CLASSES=(C94INS_Properties,C94INS_OT_Workspace,C94INS_OT_Focus,C94INS_OT_Edit,
          C94INS_OT_Nearest,C94INS_OT_Center,C94INS_OT_Drag,C94INS_OT_Compare,C94INS_PT_Inspection)


def register():
    if _REGISTERED:return
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls);_REGISTERED.append(cls)
        bpy.types.Scene.c94_inspection=PointerProperty(type=C94INS_Properties)
        if not bpy.app.background:bpy.app.timers.register(_watch,first_interval=.3,persistent=True)
    except Exception:
        unregister();raise


def unregister():
    if bpy.app.timers.is_registered(_watch):bpy.app.timers.unregister(_watch)
    if hasattr(bpy.types.Scene,'c94_inspection'):del bpy.types.Scene.c94_inspection
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
