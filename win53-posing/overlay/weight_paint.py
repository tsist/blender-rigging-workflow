# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Weight Paint on an independent proposal, then protected local commit."""
import bpy,json
import numpy as np
from . import skeleton_gate, skinning_policy as policy, skinning_job, corrective_authoring as sig

_require=skinning_job._require
_SELECTION={'.select_vert','.select_edge','.select_poly'}


def _present(obj,view):
    try:return obj is not None and view.objects.get(obj.name)==obj
    except ReferenceError:return False


def _geometry(obj):
    data=sig._mesh_signature(obj.data,details=True)
    data['attributes']=[a for a in data['attributes'] if a[0] not in _SELECTION]
    state=skinning_job._proxy_state(obj);state['mesh']=sig._hash(data)
    state['keys']=policy._shape_state(obj)
    return state


class PaintSession:
    def __init__(self,context,target,rig,names,ids,alpha,active_bone):
        skeleton_gate.require_interactive_paint(rig)
        scope=policy.local_scope(target,rig,names,ids,alpha)
        active_bone=active_bone or scope['editable'][0]
        _require(active_bone in scope['editable'],'活动刷权重骨必须是未锁定的可写组')
        self.target,self.rig=target,rig;self._scene=context.scene;self._view=context.view_layer
        self._source_mesh,self._rig_data=target.data,rig.data
        self._before=policy._state(target,rig);self._scope=scope;self._names=list(names)
        self._selected=list(context.selected_objects);self._active=context.view_layer.objects.active
        self._source_display=(target.hide_get(view_layer=self._view),target.hide_select)
        self._original_locks=[(g.name,g.lock_weight) for g in target.vertex_groups]
        self._settings={};self._done=False;self.object=None
        obj=target.copy();mesh=target.data.copy();obj.data=mesh
        obj.name=target.name+'_权重绘制提案'
        try:
            context.scene.collection.objects.link(obj);self.object=obj;self._mesh=mesh
            obj.hide_set(False);obj.hide_viewport=False;obj.hide_select=False
            self._record={'schema':'c94.paint-proposal.v1','source':target.name,'source_mesh':target.data.name,
                'rig':rig.name,'vertex_indices':scope['ids'].tolist(),'alpha':scope['alpha'].tolist(),
                'editable_bones':scope['editable'],'original_locks':self._original_locks,
                'submission_authority':'live session only; saved record never restores permission'}
            self._record_status('painting')
            for group in obj.vertex_groups:group.lock_weight=group.name not in scope['editable']
            selected=np.zeros(len(mesh.vertices),dtype=bool);selected[scope['ids'][scope['active']]]=True
            mesh.vertices.foreach_set('select',selected)
            mesh.use_paint_mask_vertex=True;mesh.use_paint_mask=False
            for key in ('use_mirror_x','use_mirror_y','use_mirror_z','use_mirror_topology'):
                setattr(mesh,key,False)
            self._geometry_before=_geometry(obj)
            self._locks=[(g.name,g.lock_weight) for g in obj.vertex_groups]
            for key,value in (('use_auto_normalize',True),('use_multipaint',False)):
                old=getattr(context.scene.tool_settings,key)
                self._settings[key]=(old,value);setattr(context.scene.tool_settings,key,value)
            for item in list(context.selected_objects):item.select_set(False)
            obj.select_set(True);context.view_layer.objects.active=obj
            obj.vertex_groups.active_index=obj.vertex_groups[active_bone].index
            target.hide_set(True,view_layer=self._view);target.hide_select=True
            _require(bpy.ops.object.mode_set.poll(),'当前上下文不能进入原生权重绘制')
            bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
            _require(obj.mode=='WEIGHT_PAINT','未进入权重绘制模式')
        except Exception:
            self._restore(context)
            bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
            raise

    def _record_status(self,status):
        if not _present(self.object,self._view):return
        self._record['status']=status
        self.object['c94_paint_proposal_v1']=json.dumps(self._record,ensure_ascii=False,allow_nan=False)
        self.object['c94_skinning_v1']=json.dumps({'schema':'c94.skinning.v1','method':'native_weight_paint_proposal',
            'source':self.target.name if _present(self.target,self._view) else self._record['source'],
            'status':'unsubmitted','proposal_status':status},ensure_ascii=False)
        if 'c94_skinning_policy_v1' in self.object:del self.object['c94_skinning_policy_v1']

    def _restore_locks(self):
        if _present(self.object,self._view):
            for name,locked in self._original_locks:
                group=self.object.vertex_groups.get(name)
                if group is not None:group.lock_weight=locked

    def _leave(self,context):
        _require(context.scene==self._scene and context.view_layer==self._view,'绘制会话场景或视图层已改变')
        _require(_present(self.object,context.view_layer) and self.object.data==self._mesh,
                 '绘制提案对象或Mesh身份已改变')
        if context.mode!='OBJECT':
            _require(context.view_layer.objects.active==self.object and self.object.mode=='WEIGHT_PAINT',
                     '请先结束其它对象的编辑模式，再结束本次权重绘制')
            bpy.ops.object.mode_set(mode='OBJECT')

    def _restore(self,context):
        # ToolSettings belong to the captured Scene, even after the user switches
        # scenes. Never write the new Scene's settings or exit its edit mode.
        for key,(old,applied) in self._settings.items():
            if getattr(self._scene.tool_settings,key)==applied:setattr(self._scene.tool_settings,key,old)
        if _present(self.target,self._view):
            hidden,locked=self._source_display
            if self.target.hide_get(view_layer=self._view):self.target.hide_set(hidden,view_layer=self._view)
            if self.target.hide_select:self.target.hide_select=locked
        if context.scene!=self._scene or context.view_layer!=self._view:
            if (_present(self.object,self._view) and self._view.objects.active==self.object
                    and self.object.mode=='WEIGHT_PAINT'):
                with context.temp_override(scene=self._scene,view_layer=self._view,
                        object=self.object,active_object=self.object):
                    bpy.ops.object.mode_set(mode='OBJECT')
            return
        present=_present(self.object,context.view_layer)
        if present and context.view_layer.objects.active==self.object and self.object.mode=='WEIGHT_PAINT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if context.mode=='OBJECT' and context.view_layer==self._view and (
                context.view_layer.objects.active==self.object if present else context.view_layer.objects.active is None):
            for item in list(context.selected_objects):item.select_set(False)
            for item in self._selected:
                if _present(item,context.view_layer):item.select_set(True)
            if _present(self._active,context.view_layer):context.view_layer.objects.active=self._active

    def finish(self,context):
        _require(not self._done,'绘制会话已结束')
        try:
            self._leave(context)
            policy._object(self.target,'MESH');policy._object(self.rig,'ARMATURE')
            _require(self.target.data==self._source_mesh and self.rig.data==self._rig_data
                     and policy._state(self.target,self.rig)==self._before,'原源、权重/锁、骨架、帧或单位已改变')
            obj=self.object;scope=self._scope
            _require(_geometry(obj)==self._geometry_before,'绘制提案的几何、Key、变换或绑定已改变')
            _require([(g.name,g.lock_weight) for g in obj.vertex_groups]==self._locks,'绘制提案的组集合或保护锁已改变')
            names=scope['editable'];ids=scope['ids'];values=np.zeros((len(ids),len(names)),dtype=np.float32)
            for j,name in enumerate(names):
                index=obj.vertex_groups[name].index
                for row,i in enumerate(ids):values[row,j]=next((g.weight for g in obj.data.vertices[int(i)].groups if g.group==index),0.)
            # Paint masks/locks are UI aids; exact protected values are checked at submission.
            active_ids=ids[scope['active']]
            policy._verify_local(self.target,obj,names,active_ids,values[scope['active']],check_locks=False)
            plan=policy.prepare_local_field(self.target,self.rig,values,names,ids,scope['alpha'],self._names,
                method='native_weight_paint',details={'proposal_object':obj.name,'scope':scope['summary'],
                    'proposal_sha256':sig._array_hash(values),'coordinates':'neutral RAW Basis'})
            self._restore_locks();self._record_status('prepared')
            plan.donor=obj;plan._donor_mesh=obj.data;plan._donor_before=policy._state(obj,self.rig)
            self._done=True
            return plan
        finally:
            self._restore(context)

    def cancel(self,context):
        if self._done:return
        try:
            if _present(self.object,context.view_layer):self._leave(context)
        finally:self._restore(context)
        self._restore_locks();self._record_status('cancelled')
        self._done=True


def begin(context,target,rig,editable_bone_names,vertex_indices,alpha,*,active_bone=None):
    return PaintSession(context,target,rig,editable_bone_names,vertex_indices,alpha,active_bone)
