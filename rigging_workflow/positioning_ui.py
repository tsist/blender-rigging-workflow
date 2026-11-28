"""Native UI for the shared offline marker/reference domain core.

This module only delegates to positioning.py. It offers no generation, skinning,
confirmation, or animation operations and never changes a user's object mode.
"""
import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from . import positioning

_REGISTERED = []
DOMAIN_LABELS = {'body':'身体', 'hands':'双手', 'face':'面部'}


def _state(context):
    if context.scene is None:
        return None
    return positioning.read_state(context.scene)


def _can_write(context):
    if context.mode != 'OBJECT' or context.scene is None:
        return False
    try:
        return isinstance(_state(context), dict)
    except ValueError:
        return False


def _selected_markers(context, state):
    names = {row.get('object') for row in state.get('markers', ()) if isinstance(row, dict)}
    return [obj for obj in context.selected_objects if obj.name in names]


def _selection_snapshot(context):
    return list(context.selected_objects), context.view_layer.objects.active


def _restore_selection(context, snapshot):
    """Preserve surviving input objects; do not pick replacement objects by name."""
    old_selected, old_active = snapshot
    current = context.view_layer.objects

    def survives(obj):
        if obj is None:
            return False
        try:
            return current.get(obj.name) == obj
        except ReferenceError:
            return False

    for obj in list(context.selected_objects):
        obj.select_set(False, view_layer=context.view_layer)
    for obj in old_selected:
        if survives(obj):
            obj.select_set(True, view_layer=context.view_layer)
    context.view_layer.objects.active = old_active if survives(old_active) else None


def _execute_core(operator, context, callback, message):
    if context.mode != 'OBJECT':
        operator.report({'ERROR'}, '请在对象模式中执行定位操作')
        return {'CANCELLED'}
    try:
        if not isinstance(_state(context), dict):
            raise ValueError('当前场景尚无角色定位记录')
    except ValueError as exc:
        operator.report({'ERROR'}, str(exc))
        return {'CANCELLED'}
    snapshot = _selection_snapshot(context)
    try:
        callback()
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        operator.report({'ERROR'}, str(exc))
        return {'CANCELLED'}
    finally:
        _restore_selection(context, snapshot)
    operator.report({'INFO'}, message)
    return {'FINISHED'}


def _layer_collection(context, state):
    """Find the explicitly named marker collection in this scene/view layer."""
    name = state.get('marker_collection')
    if not isinstance(name, str):
        raise ValueError('定位记录尚未指定标记集合')

    def find(layer):
        if layer.collection.name == name:
            return layer
        for child in layer.children:
            result = find(child)
            if result:
                return result
        return None

    result = find(context.view_layer.layer_collection)
    if result is None:
        raise ValueError('标记集合不在当前场景的视图层中：' + name)
    return result


class C94POS_OT_Estimate(bpy.types.Operator):
    bl_idname = 'c94_workflow.estimate_markers'
    bl_label = '估计定位标记'
    bl_description = '调用离线定位核心，重算指定且未锁定的范围'
    bl_options = {'REGISTER', 'UNDO'}

    domain: EnumProperty(name='范围', items=(
        ('ALL','整套','身体、双手与面部'),
        ('BODY','身体','仅身体标记'),
        ('HANDS','双手','仅手部标记'),
        ('FACE','面部','仅面部标记')), default='ALL')

    @classmethod
    def poll(cls, context):
        return _can_write(context)

    def execute(self, context):
        domains = list(DOMAIN_LABELS) if self.domain == 'ALL' else [self.domain.lower()]
        return _execute_core(self, context,
            lambda: positioning.estimate(context.scene, domains),
            '定位候选已更新，请检查标记与未识别项')


def _face_ruler_bounds(context, atlas_name, target_name):
    """Read the two explicit rulers only; never capture markers or mutate state."""
    objects,bounds=[],[]
    for name in (atlas_name,target_name):
        obj=context.scene.objects.get(name) if name else None
        if obj is None or obj.type!='MESH':
            raise ValueError('请选择当前Scene中精确的中立Mesh范围参照：'+name)
        points=positioning.mesh_points(obj)
        if len(points)<4:
            raise ValueError('范围参照没有足够的三维点：'+name)
        objects.append(obj);bounds.append([points.min(0).tolist(),points.max(0).tolist()])
    return objects,bounds


def _face_domain_preview(context, atlas_name, target_name):
    """One cached read-only preview; validation and ROI remain in the core."""
    preview={'names':(atlas_name,target_name),'scene':context.scene,'bounds':None,'counts':{},'error':None}
    try:
        objects,bounds=_face_ruler_bounds(context,atlas_name,target_name)
        preview.update(objects=objects,bounds=bounds)
        config=positioning._face_config(dict(mode='bounds_v1',atlas_bounds=bounds[0],target_bounds=bounds[1]))
        state=positioning._state(context.scene);arrays=positioning._geometry(context.scene,state)
        # Domain counts are diagnostic even when the shared lip guard rejects.
        for role,field,label in (('atlas_mesh','atlas_bounds','atlas_points'),('target_mesh','target_bounds','target_points')):
            lo,hi=config[field];a=arrays[role]
            preview['counts'][label]=int(((a>=lo)&(a<=hi)).all(axis=1).sum())
        pair=positioning._face_arrays(arrays,dict(state,face_domain=config))
        preview['counts'].update(pair[2])
    except (ValueError,RuntimeError,KeyError,TypeError,ReferenceError) as error:
        preview['error']=str(error)
    return preview


class C94POS_OT_FaceDomain(bpy.types.Operator):
    bl_idname = 'c94_workflow.set_face_domain'
    bl_label = '指定面部范围'
    bl_description = '从明确中立几何捕获世界范围；配准仍使用原图谱和目标点'
    bl_options = {'REGISTER', 'UNDO'}

    atlas_object: StringProperty(name='图谱面部范围参照')
    target_object: StringProperty(name='角色面部范围参照')
    use_default: BoolProperty(name='恢复旧范围',default=False,options={'HIDDEN'})

    @classmethod
    def poll(cls,context):
        return _can_write(context)

    def invoke(self,context,event):
        if self.use_default:
            return self.execute(context)
        self._dialog_invoked=True
        self._refresh_preview(context)
        return context.window_manager.invoke_props_dialog(self,width=480)

    def _refresh_preview(self,context):
        self._preview_key=(self.atlas_object,self.target_object,context.scene)
        self._preview_cache=_face_domain_preview(context,self.atlas_object,self.target_object)

    def check(self,context):
        key=(self.atlas_object,self.target_object,context.scene)
        if getattr(self,'_preview_key',None)!=key:
            self._refresh_preview(context)
            return True
        return False

    def draw(self,context):
        layout=self.layout
        layout.prop_search(self,'atlas_object',context.scene,'objects')
        layout.prop_search(self,'target_object',context.scene,'objects')
        preview=getattr(self,'_preview_cache',None)
        if preview is not None:
            box=layout.box();box.label(text='应用前预览（世界坐标）')
            if preview['bounds'] is not None:
                for label,(lo,hi) in zip(('图谱','角色'),preview['bounds']):
                    box.label(text=label+'：'+', '.join('%.4g'%v for v in lo)+' → '+', '.join('%.4g'%v for v in hi))
            counts=preview['counts']
            box.label(text='域点数：%s / %s'%(counts.get('atlas_points','未计算'),counts.get('target_points','未计算')))
            box.label(text='口区点数：%s / %s'%(counts.get('lip_source_points','预检未通过'),counts.get('lip_target_points','预检未通过')))
            if preview['error']:
                box.label(text=preview['error'],icon='ERROR')
        layout.label(text='捕获静态世界范围；实际配准仍读取原中立表面')
        layout.label(text='移动参照不会随动，修改后需重新应用')
        layout.label(text='预览后参照改变，请重新打开对话框检查')

    def execute(self,context):
        def call():
            if self.use_default:
                return positioning.set_face_domain(context.scene,atlas_bounds=None,target_bounds=None)
            objects,bounds=_face_ruler_bounds(context,self.atlas_object,self.target_object)
            if getattr(self,'_dialog_invoked',False):
                preview=getattr(self,'_preview_cache',None)
                if (preview is None or preview['names']!=(self.atlas_object,self.target_object)
                        or preview['scene']!=context.scene or preview.get('objects')!=objects or preview['bounds']!=bounds):
                    raise ValueError('预览后范围参照或选择已改变；请重新打开对话框检查')
                if preview['error']:
                    raise ValueError('面部范围预检未通过：'+preview['error'])
            return positioning.set_face_domain(context.scene,atlas_bounds=bounds[0],target_bounds=bounds[1])
        return _execute_core(self,context,call,'已恢复旧面部范围' if self.use_default else '已捕获面部范围，旧自动标记需重估')


class C94POS_OT_Sync(bpy.types.Operator):
    bl_idname = 'c94_workflow.sync_markers'
    bl_label = '同步手调标记'
    bl_description = '将当前标记对象的位置同步回角色定位记录'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _can_write(context)

    def execute(self, context):
        return _execute_core(self, context,
            lambda: positioning.sync_markers(context.scene), '已同步手动调整的标记')


class C94POS_OT_Lock(bpy.types.Operator):
    bl_idname = 'c94_workflow.lock_markers'
    bl_label = '设置选中标记锁定'
    bl_description = '只对当前选中的本角色标记设置锁定，不处理其它对象'
    bl_options = {'REGISTER', 'UNDO'}

    locked: BoolProperty(name='锁定', default=True)

    @classmethod
    def poll(cls, context):
        if not _can_write(context):
            return False
        return bool(_selected_markers(context, _state(context)))

    def execute(self, context):
        def call():
            selected = _selected_markers(context, _state(context))
            if not selected:
                raise ValueError('请先选择本角色的定位标记')
            return positioning.set_locked(context.scene, selected, self.locked)
        return _execute_core(self, context, call,
                             '已锁定选中标记' if self.locked else '已解锁选中标记')


class C94POS_OT_Mirror(bpy.types.Operator):
    bl_idname = 'c94_workflow.mirror_markers'
    bl_label = '镜像选中标记'
    bl_description = '调用角色定位核心处理选中标记的左右镜像'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _can_write(context):
            return False
        return bool(_selected_markers(context, _state(context)))

    def execute(self, context):
        def call():
            selected = _selected_markers(context, _state(context))
            if not selected:
                raise ValueError('请先选择本角色的定位标记')
            return positioning.mirror_selected(context.scene, selected)
        return _execute_core(self, context, call, '选中标记的镜像操作已完成')


class C94POS_OT_Restore(bpy.types.Operator):
    bl_idname = 'c94_workflow.restore_markers'
    bl_label = '恢复上一定位候选'
    bl_description = '通过定位核心恢复上一份候选记录与标记'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _can_write(context)

    def execute(self, context):
        return _execute_core(self, context,
            lambda: positioning.restore_previous(context.scene), '已恢复上一定位候选')


class C94POS_OT_Fit(bpy.types.Operator):
    bl_idname = 'c94_workflow.fit_reference'
    bl_label = '拟合完整参考骨'
    bl_description = '用当前定位标记拟合爱音完整参考层，供骨位叠图检查'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _can_write(context)

    def execute(self, context):
        return _execute_core(self, context,
            lambda: positioning.fit(context.scene), '参考骨候选已拟合，请检查叠图')


class C94POS_OT_MarkerVisibility(bpy.types.Operator):
    bl_idname = 'c94_workflow.marker_visibility'
    bl_label = '设置标记集合显隐'
    bl_description = '只改变本角色标记集合在当前场景视图层中的显隐'
    bl_options = {'REGISTER', 'UNDO'}

    visible: BoolProperty(name='显示', default=True)

    @classmethod
    def poll(cls, context):
        return _can_write(context)

    def execute(self, context):
        def call():
            layer = _layer_collection(context, _state(context))
            if self.visible and layer.exclude:
                raise ValueError('标记集合已从当前视图层排除，请先在原生大纲中包含该集合')
            layer.hide_viewport = not self.visible
        return _execute_core(self, context, call,
                             '已显示当前角色标记集合' if self.visible else '已隐藏当前角色标记集合')


class C94POS_OT_SelectReference(bpy.types.Operator):
    bl_idname = 'c94_workflow.select_fitted_reference'
    bl_label = '选择拟合参考骨'
    bl_description = '添加选择本角色拟合参考骨，保留其它已选对象'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _can_write(context):
            return False
        state = _state(context)
        name = state.get('fitted_reference')
        return isinstance(name, str) and context.scene.objects.get(name) is not None

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, '请在对象模式中选择参考骨')
            return {'CANCELLED'}
        try:
            state = _state(context)
            if not isinstance(state, dict):
                raise ValueError('当前场景尚无角色定位记录')
            obj = context.scene.objects.get(state.get('fitted_reference', ''))
            if obj is None or obj.type != 'ARMATURE':
                raise ValueError('本角色尚无可选的拟合参考骨')
            if obj.name not in context.view_layer.objects or obj.hide_select or not obj.visible_get(view_layer=context.view_layer):
                raise ValueError('拟合参考骨不在当前可见可选视图层中')
            obj.select_set(True, view_layer=context.view_layer)
            context.view_layer.objects.active = obj
        except (ValueError, RuntimeError, KeyError, TypeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


def _status_label(value):
    if isinstance(value, dict):value=value.get('status',value.get('state','待检查'))
    if not isinstance(value,str):return '尚未估计'
    return {'needs_review':'需人工检查','estimated':'已估计，待检查','accepted':'已确认',
            'manual':'手动定位','failed':'估计失败','not_estimated':'尚未估计',
            'unestimated':'尚未估计','locked':'已锁定','pending':'待检查',
            'initialized':'已初始化，待估计'}.get(value,value)


class C94POS_PT_Positioning(bpy.types.Panel):
    bl_label = '离线定位与参考骨'
    bl_idname = 'C94POS_PT_positioning'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_order = 10

    def draw(self, context):
        layout = self.layout
        advanced=getattr(getattr(context.scene,'c94_workspace_options_v1',None),'level','ADVANCED')!='DAILY'
        try:
            state = _state(context)
        except ValueError as exc:
            layout.label(text=str(exc), icon='ERROR')
            return
        if not isinstance(state, dict):
            layout.label(text='当前场景尚无角色定位记录', icon='INFO')
            return
        markers = [m for m in state.get('markers', ()) if isinstance(m, dict)]
        locked = sum(bool(m.get('locked')) for m in markers)
        layout.label(text=f'定位标记：{len(markers)} 个 · 已锁定 {locked} 个')
        if context.mode != 'OBJECT':
            layout.label(text='定位写入需使用对象模式', icon='INFO')
        column = layout.column()
        column.enabled = context.mode == 'OBJECT'
        row=column.row(align=True)
        row.operator('c94_workflow.set_face_domain',text='指定面部范围',icon='SELECT_SET').use_default=False
        row.operator('c94_workflow.set_face_domain',text='恢复旧范围',icon='LOOP_BACK').use_default=True
        try:
            face=positioning._face_config(state.get('face_domain'))
        except ValueError as exc:
            column.label(text=str(exc),icon='ERROR')
            face=None
        else:
            if face is None:
                column.label(text='面部范围：旧全身先验',icon='INFO')
            else:
                box=column.box();box.label(text='显式面部范围（世界坐标）')
                if advanced:
                    for key,label in (('atlas_bounds','图谱'),('target_bounds','角色')):
                        lo,hi=face[key]
                        box.label(text=label+'：'+', '.join('%.4g'%v for v in lo)+' → '+', '.join('%.4g'%v for v in hi))
                    counts=state.get('face_domain_counts',{})
                    if isinstance(counts,dict):
                        box.label(text='应用时域点数：%s / %s'%(counts.get('atlas_points','?'),counts.get('target_points','?')))
                        box.label(text='应用时口区点数：%s / %s'%(counts.get('lip_source_points','?'),counts.get('lip_target_points','?')))
                box.label(text='范围捕获后，移动参照需重新应用')
        column.operator('c94_workflow.estimate_markers', text='估计整套定位', icon='VIEWZOOM').domain = 'ALL'
        row = column.row(align=True)
        for value, label in (('BODY','身体'),('HANDS','双手'),('FACE','面部')):
            row.operator('c94_workflow.estimate_markers', text=label).domain = value
        box = column.box()
        box.label(text='分区候选状态')
        status = state.get('estimate_status', {})
        status = status if isinstance(status, dict) else {}
        for key, label in DOMAIN_LABELS.items():
            box.label(text=label + '：' + _status_label(status.get(key)))
        column.operator('c94_workflow.sync_markers', icon='FILE_REFRESH')
        row = column.row(align=True)
        row.operator('c94_workflow.lock_markers', text='锁定选中', icon='LOCKED').locked = True
        row.operator('c94_workflow.lock_markers', text='解锁选中', icon='UNLOCKED').locked = False
        column.operator('c94_workflow.mirror_markers', icon='MOD_MIRROR')
        column.operator('c94_workflow.restore_markers', icon='LOOP_BACK')
        row = column.row(align=True)
        row.operator('c94_workflow.marker_visibility', text='显示标记', icon='HIDE_OFF').visible = True
        row.operator('c94_workflow.marker_visibility', text='隐藏标记', icon='HIDE_ON').visible = False
        column.separator()
        column.operator('c94_workflow.fit_reference', icon='ARMATURE_DATA')
        column.operator('c94_workflow.select_fitted_reference', icon='RESTRICT_SELECT_OFF')
        layout.label(text='标记与参考骨仍需人工检查', icon='INFO')
        layout.label(text='参考骨确认后，使用生成面板继续')


_CLASSES = (C94POS_OT_Estimate, C94POS_OT_FaceDomain, C94POS_OT_Sync, C94POS_OT_Lock,
            C94POS_OT_Mirror, C94POS_OT_Restore, C94POS_OT_Fit,
            C94POS_OT_MarkerVisibility, C94POS_OT_SelectReference,
            C94POS_PT_Positioning)


def register():
    if _REGISTERED:
        return
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
    except Exception:
        unregister()
        raise


def unregister():
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
