"""Manual planning of new hair, skirt and accessory references; shared core APIs."""
import json
import traceback
import uuid
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from . import positioning

OPTIONS='c94_accessory_options_v1'
DRAFT='c94_accessory_draft_ui_v1'
POINTS='c94_accessory_points_ui_v1'
RESULT='c94_accessory_reference_ui_v1'
BINDINGS='c94_accessory_bindings_ui_v1'
_REGISTERED=[]
_PROPERTY_OWNED=False
_CHAIN_ITEMS={}

def _chain_items(self,context):
    if not context:return []
    try:
        saved=_read(context.scene,RESULT,{})
        chains=saved.get('record',{}).get('chains',[])
        result=[]
        for chain in chains:
            identity=chain['id'];label=chain.get('group',identity)+' · '+chain.get('kind','')
            _CHAIN_ITEMS.setdefault((identity,label),(identity,label,'本次完整参考中的明确附属链'))
            result.append(_CHAIN_ITEMS[(identity,label)])
        return result
    except (ValueError,TypeError,KeyError):return []


def _read(scene,key,default=None):
    raw=scene.get(key)
    if raw is None:return default
    if not isinstance(raw,str):raise ValueError('附属方案不是JSON记录')
    try:return json.loads(raw)
    except ValueError as exc:raise ValueError('附属方案记录损坏') from exc


def _save(scene,key,value):scene[key]=json.dumps(value,ensure_ascii=False,allow_nan=False)


def _binding_source(obj):
    from . import skinning
    return {'object_uid':obj.session_uid,'mesh_uid':obj.data.session_uid,
            'geometry_world':skinning.geometry_fingerprint(obj),
            'topology':skinning.topology_fingerprint(obj.data)}


def _check_binding_source(scene,row):
    obj=scene.objects.get(row.get('object',''))
    if obj is None or obj.type!='MESH' or row.get('capture')!=_binding_source(obj):
        raise ValueError('捕获点域后对象、拓扑、位置或世界框架已改变，请重新捕获附属点域')
    return obj


def base_reference(scene):
    state=positioning.read_state(scene)
    if not state:raise ValueError('先完成身体完整参考骨定位')
    base=scene.objects.get(state.get('fitted_reference',''))
    if not base or base.type!='ARMATURE' or base.get(positioning.OWNER_KEY)!=state.get('owner'):
        raise ValueError('定位参考骨丢失或身份不匹配')
    return base


def reference(scene,base,name):
    saved=_read(scene,RESULT)
    if not isinstance(saved,dict) or saved.get('base_reference')!=base.name or saved.get('reference')!=name:
        raise ValueError('本次参考不是当前附属方案的完整参考副本')
    obj=scene.objects.get(name)
    if not obj or obj.type!='ARMATURE':raise ValueError('附属完整参考已丢失')
    return obj


def reference_record(scene,ref,*,validate=False):
    base=base_reference(scene)
    if ref==base:return None
    if reference(scene,base,ref.name)!=ref:raise ValueError('附属参考身份不匹配')
    saved=_read(scene,RESULT)
    record=saved.get('record')
    if validate:
        from . import accessory_chains,generation
        generation.validate_input(scene,base)
        record=accessory_chains.validate_reference(scene,ref,record)
    return record


class C94AUX_Options(bpy.types.PropertyGroup):
    kind:EnumProperty(name='用途',items=[('hair','发束','新建发束链'),('skirt','裙摆','新建裙摆链'),('generic','附属','新建通用附属链')])
    source_mode:EnumProperty(name='输入',items=[('CURVE','曲线','使用活动曲线的明确样条'),('POINTS','光标采点','按顺序采集根部到末端'),('BONES','手工骨链','使用活动骨架中选中的一条连续骨链')])
    side:EnumProperty(name='侧别',items=[('C','中央','不加侧别'),('L','左','保留.L'),('R','右','保留.R')])
    segments:IntProperty(name='节数',default=3,min=1,max=64)
    group:StringProperty(name='分组',default='新发束')
    parent_bone:StringProperty(name='主形变骨语义',default='形变-头颅')
    spline_index:IntProperty(name='样条',default=0,min=0)
    reverse:BoolProperty(name='从曲线末端为根',default=False)
    roll_axis:FloatVectorProperty(name='参考横轴',default=(1,0,0),size=3)
    reference_label:StringProperty(name='参考副本名称',default='附属完整参考')
    target_rig:StringProperty(name='生成角色')
    chain_id:EnumProperty(name='目标链',items=_chain_items)
    bind_method:EnumProperty(name='权重方式',items=[('CENTERLINE','中心线分段','只对已指定链建立片束参数权重'),('RIGID','刚性段','全部指定点跟随一段')])
    rigid_segment:IntProperty(name='刚性段',default=0,min=0)
    max_distance:FloatProperty(name='中心线覆盖距离（米）',default=.1,min=.000001)


class C94AUX_OT_Point(bpy.types.Operator):
    bl_idname='c94_workflow.accessory_point'
    bl_label='采集当前3D光标点'
    action:EnumProperty(items=[('ADD','采点',''),('REMOVE','撤回末点',''),('CLEAR','清空采点','')])
    def execute(self,context):
        try:
            points=_read(context.scene,POINTS,[])
            if not isinstance(points,list):raise ValueError('采点记录损坏')
            if self.action=='ADD':points.append(list(context.scene.cursor.location))
            elif self.action=='REMOVE':points=points[:-1]
            else:points=[]
            _save(context.scene,POINTS,points)
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94AUX_OT_Add(bpy.types.Operator):
    bl_idname='c94_workflow.add_accessory_chain'
    bl_label='加入本次附属方案'
    def execute(self,context):
        try:
            o=getattr(context.scene,OPTIONS)
            specs=_read(context.scene,DRAFT,[])
            if not isinstance(specs,list):raise ValueError('附属草案记录损坏')
            if o.source_mode=='POINTS':
                points=_read(context.scene,POINTS,[])
                if len(points)<2:raise ValueError('依次采集至少根部、末端两个点')
                source={'type':'POINTS','space':'WORLD','points':points}
            elif o.source_mode=='CURVE':
                obj=context.object
                if not obj or obj.type!='CURVE':raise ValueError('选择一件明确活动曲线')
                if o.spline_index>=len(obj.data.splines):raise ValueError('指定样条不存在')
                source={'type':'CURVE','object':obj.name,'spline_index':o.spline_index,'root':'LAST' if o.reverse else 'FIRST'}
            else:
                obj=context.object
                if not obj or obj.type!='ARMATURE':raise ValueError('选择手工参考骨架中的连续骨链')
                if context.mode=='EDIT_ARMATURE':selected=list(context.selected_editable_bones or [])
                else:selected=[b for b in obj.data.bones if b.select]
                selected_names={b.name for b in selected}
                roots=[b for b in selected if not b.parent or b.parent.name not in selected_names]
                if len(roots)!=1:raise ValueError('只选择一条有明确根部的连续骨链')
                chain=[roots[0]]
                while True:
                    children=[b for b in selected if b.parent and b.parent.name==chain[-1].name]
                    if len(children)>1:raise ValueError('选择的骨链有分叉，请分成独立链')
                    if not children:break
                    chain.append(children[0])
                if len(chain)!=len(selected):raise ValueError('选择的骨链不连续')
                source={'type':'BONES','object':obj.name,'bones':[b.name for b in chain]}
            if not o.group.strip() or not o.parent_bone.strip():raise ValueError('填写分组和明确主骨语义')
            specs.append({'id':uuid.uuid4().hex[:10],'kind':o.kind,'side':o.side,'parent_bone':o.parent_bone.strip(),
                          'source':source,'segments':o.segments,'group':o.group.strip(),'roll_axis':list(o.roll_axis)})
            _save(context.scene,DRAFT,specs)
            self.report({'INFO'},'已加入附属链；准备完整参考后可原生编辑端点')
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94AUX_OT_Remove(bpy.types.Operator):
    bl_idname='c94_workflow.remove_accessory_chain'
    bl_label='移除这条方案'
    index:IntProperty(default=-1)
    def execute(self,context):
        try:
            specs=_read(context.scene,DRAFT,[])
            if not 0<=self.index<len(specs):raise ValueError('这条草案已不存在')
            del specs[self.index];_save(context.scene,DRAFT,specs)
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94AUX_OT_Prepare(bpy.types.Operator):
    bl_idname='c94_workflow.prepare_accessory_reference'
    bl_label='准备含附属链的完整参考副本'
    @classmethod
    def poll(cls,context):return context.mode=='OBJECT'
    def execute(self,context):
        from . import accessory_chains,generation,module_configuration,generation_ui
        before=module_configuration._ids();active=context.view_layer.objects.active;selected=list(context.selected_objects);owned=[]
        old=context.scene.get(RESULT);options=getattr(context.scene,generation_ui.OPTIONS);old_name=options.reference_object
        try:
            base=base_reference(context.scene);generation.validate_input(context.scene,base)
            out=accessory_chains.prepare_reference(context.scene,base,_read(context.scene,DRAFT,[]),getattr(context.scene,OPTIONS).reference_label,owned_ids=owned)
            _save(context.scene,RESULT,{'schema':'c94.accessory-reference-ui.v1','base_reference':base.name,'reference':out['reference'].name,'record':out['record']})
            options.reference_object=out['reference'].name
        except Exception as exc:
            if old is None:
                if RESULT in context.scene:del context.scene[RESULT]
            else:context.scene[RESULT]=old
            options.reference_object=old_name
            module_configuration._cleanup(before,active,selected,owned_ids=owned)
            self.report({'ERROR'},str(exc));traceback.print_exc();return {'CANCELLED'}
        self.report({'INFO'},'完整参考副本已准备；原参考保留，附属方案效果暂缓评定')
        return {'FINISHED'}


class C94AUX_OT_Capture(bpy.types.Operator):
    bl_idname='c94_workflow.capture_accessory_reference'
    bl_label='同步附属参考手改'
    def execute(self,context):
        try:
            from . import accessory_chains,generation_ui
            ref=generation_ui.reference(context);saved=_read(context.scene,RESULT)
            if not saved or saved.get('reference')!=ref.name:raise ValueError('选择本次附属完整参考')
            saved['record']=accessory_chains.capture_reference(ref,saved['record'])
            _save(context.scene,RESULT,saved)
            self.report({'INFO'},'扩展参考手改已同步；需要按新参考准备新配置库')
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94AUX_OT_BindDomain(bpy.types.Operator):
    bl_idname='c94_workflow.capture_accessory_bind_domain'
    bl_label='加入当前网格点域'
    clear:BoolProperty(default=False)
    def execute(self,context):
        try:
            if self.clear:_save(context.scene,BINDINGS,[]);return {'FINISHED'}
            o=getattr(context.scene,OPTIONS)
            if not o.chain_id:raise ValueError('选择本次参考中的目标链')
            obj=context.object
            if not obj or obj.type!='MESH':raise ValueError('选择明确网格；编辑模式可仅捕获所选点')
            if context.mode=='EDIT_MESH':
                import bmesh
                bm=bmesh.from_edit_mesh(obj.data);bm.verts.index_update();bm.verts.ensure_lookup_table();ids=[v.index for v in bm.verts if v.select]
                obj.update_from_editmode()
            elif context.mode=='OBJECT':ids=list(range(len(obj.data.vertices)))
            else:raise ValueError('回对象模式或网格编辑模式捕获点域')
            if not ids:raise ValueError('当前没有明确选择的点')
            rows=_read(context.scene,BINDINGS,[])
            row=next((r for r in rows if r['object']==obj.name),None)
            if row is None:row={'object':obj.name,'parts':[],'capture':_binding_source(obj)};rows.append(row)
            else:_check_binding_source(context.scene,row)
            if set(ids)&{i for p in row['parts'] for i in p['vertex_ids']}:raise ValueError('点域与已有链分配重叠，请先清空或调整选区')
            part={'chain':o.chain_id,'vertex_ids':ids,'method':o.bind_method}
            if o.bind_method=='RIGID':part['segment']=o.rigid_segment
            else:part['max_distance_m']=o.max_distance
            row['parts'].append(part);_save(context.scene,BINDINGS,rows)
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}


class C94AUX_OT_Bind(bpy.types.Operator):
    bl_idname='c94_workflow.bind_accessory_copies'
    bl_label='创建附属蒙皮副本'
    @classmethod
    def poll(cls,context):return context.mode=='OBJECT'
    def execute(self,context):
        try:
            from . import accessory_chains,generation_ui
            o=getattr(context.scene,OPTIONS);ref=generation_ui.reference(context)
            record=reference_record(context.scene,ref,validate=True)
            rig=context.scene.objects.get(o.target_rig)
            if rig is None:raise ValueError('选择已经生成并安装附属链的角色')
            rows=_read(context.scene,BINDINGS,[])
            for row in rows:_check_binding_source(context.scene,row)
            bindings=[{'object':row['object'],'parts':row['parts']} for row in rows]
            result=accessory_chains.bind_copies(context.scene,rig,record,bindings,getattr(context.scene,generation_ui.OPTIONS).label+'_附属')
        except Exception as exc:self.report({'ERROR'},str(exc));traceback.print_exc();return {'CANCELLED'}
        self.report({'INFO'},'已创建%d件附属蒙皮副本，原网格保留'%len(result['objects']))
        return {'FINISHED'}


class C94AUX_PT_Chains(bpy.types.Panel):
    bl_label='发束、裙摆与附属方案'
    bl_idname='C94AUX_PT_chains'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='角色绑定';bl_order=0
    @classmethod
    def poll(cls,context):
        try:return bool(base_reference(context.scene))
        except ValueError:return False
    def draw(self,context):
        layout=self.layout;o=getattr(context.scene,OPTIONS)
        layout.prop(o,'kind');layout.prop(o,'source_mode');layout.prop(o,'group');layout.prop(o,'side')
        layout.prop(o,'segments');layout.prop_search(o,'parent_bone',base_reference(context.scene).data,'bones')
        layout.label(text='选择模板主形变语义，如形变-头颅；生成时校验')
        layout.prop(o,'roll_axis')
        if o.source_mode=='POINTS':
            try:count=len(_read(context.scene,POINTS,[]))
            except ValueError:count=0
            layout.label(text='按根部→末端采点：%d点'%count)
            row=layout.row(align=True)
            row.operator('c94_workflow.accessory_point',text='采光标点').action='ADD'
            row.operator('c94_workflow.accessory_point',text='撤回末点').action='REMOVE'
            row.operator('c94_workflow.accessory_point',text='清空').action='CLEAR'
        elif o.source_mode=='CURVE':layout.prop(o,'spline_index');layout.prop(o,'reverse')
        else:layout.label(text='在手工骨架中选择一条连续骨链')
        layout.operator('c94_workflow.add_accessory_chain')
        try:
            for index,spec in enumerate(_read(context.scene,DRAFT,[])):
                row=layout.row(align=True);row.label(text='%s · %s · %d节'%(spec['group'],spec['kind'],spec['segments']))
                row.operator('c94_workflow.remove_accessory_chain',text='',icon='X').index=index
        except (ValueError,TypeError,KeyError):layout.label(text='附属草案损坏，请检查方案',icon='ERROR')
        layout.prop(o,'reference_label')
        layout.operator('c94_workflow.prepare_accessory_reference')
        layout.operator('c94_workflow.capture_accessory_reference')
        layout.label(text='先完成全部方案与参考，再准备模块配置和生成')
        box=layout.box();box.label(text='附属链分部件蒙皮')
        box.prop_search(o,'target_rig',context.scene,'objects');box.prop(o,'chain_id');box.prop(o,'bind_method')
        if o.bind_method=='RIGID':box.prop(o,'rigid_segment')
        else:box.prop(o,'max_distance')
        box.label(text='对象模式捕获全点；编辑模式捕获所选点')
        box.operator('c94_workflow.capture_accessory_bind_domain')
        box.operator('c94_workflow.capture_accessory_bind_domain',text='清空附属点域方案').clear=True
        try:
            rows=_read(context.scene,BINDINGS,[])
            box.label(text='本次%d件网格，%d个明确点域'%(len(rows),sum(len(r['parts']) for r in rows)))
        except (ValueError,TypeError,KeyError):box.label(text='点域方案损坏',icon='ERROR')
        box.operator('c94_workflow.bind_accessory_copies')


_CLASSES=(C94AUX_Options,C94AUX_OT_Point,C94AUX_OT_Add,C94AUX_OT_Remove,C94AUX_OT_Prepare,C94AUX_OT_Capture,
          C94AUX_OT_BindDomain,C94AUX_OT_Bind,C94AUX_PT_Chains)
def register():
    global _PROPERTY_OWNED
    if _REGISTERED:return
    if hasattr(bpy.types.Scene,OPTIONS):raise RuntimeError('附属方案选项已由其它实例注册')
    try:
        for cls in _CLASSES:bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94AUX_Options))
        _PROPERTY_OWNED=True
    except Exception:unregister();raise
def unregister():
    global _PROPERTY_OWNED
    if _PROPERTY_OWNED and hasattr(bpy.types.Scene,OPTIONS):
        prop=bpy.types.Scene.bl_rna.properties.get(OPTIONS)
        if prop and prop.fixed_type.identifier==C94AUX_Options.bl_rna.identifier:delattr(bpy.types.Scene,OPTIONS)
    _PROPERTY_OWNED=False
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
