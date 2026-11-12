# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent hair/skirt Cloth clips with native, editable Mesh residuals.

Capture every base/collision sample before solving; preview never runs physics.
The rig's base Action, source weights and original chain controls are read-only.
"""
import json
import math
import re
import uuid

import bpy
import numpy as np
from bpy.app.handlers import persistent

from . import cloth_backend as physics, surface_bake as surface
from . import corrective_authoring as sig, face_capture as state, control_panel as panel

STATE_KEY = 'c94_surface_dynamic_v1'
OWNER_KEY = 'c94_surface_dynamic_owner_v1'
_SESSIONS = {}
_BUSY = False
_HANDLERS_OWNED = set()


def _require(value, message):
    if not value:
        raise ValueError(message)


def _record(rig):
    _require(rig and rig.type == 'ARMATURE' and not rig.library and not rig.override_library,
             '需要本地可写角色骨架')
    value = json.loads(rig.get(STATE_KEY, '{"schema":"c94.surface.dynamic.v1","groups":{}}'))
    _require(value.get('schema') == 'c94.surface.dynamic.v1', '发裙动态记录版本不兼容')
    _require(isinstance(value.get('groups'),dict),'动态分组记录损坏')
    for row in value['groups'].values():
        _require(isinstance(row,dict) and isinstance(row.get('bakes'),list),'动态烘焙记录损坏，保留工程检查')
        for item in row['bakes']:
            _require(isinstance(item,dict) and all(isinstance(item.get(k),str) and item[k]
                     for k in ('object','carrier','action','token')) and item.get('kind')=='mesh_key_residual',
                     '旧动态输出记录不完整，保留工程检查')
    return value


def _save(rig, record):
    rig[STATE_KEY] = json.dumps(record, ensure_ascii=False, allow_nan=False)


def inspect(rig):
    return _record(rig)


def _mesh(obj, rig, source=False):
    if source:
        surface._supported(obj, rig)
        return
    _require(obj and obj.type == 'MESH' and bpy.context.view_layer.objects.get(obj.name) == obj
             and not obj.library and not obj.data.library and not obj.override_library
             and not obj.data.override_library and not obj.constraints
             and obj.animation_data is None and obj.data.animation_data is None
             and obj.data.shape_keys is None,
             '碰撞输入须为当前视图层明确的本地静态或同rig蒙皮Mesh，无Key/对象动画/约束')
    _require([m.type for m in obj.modifiers] in ([], ['SUBSURF'], ['ARMATURE'], ['ARMATURE', 'SUBSURF']),
             '碰撞输入仅支持普通Mesh或Armature后可选Subsurf')
    for modifier in obj.modifiers:
        if modifier.type == 'ARMATURE':
            _require(modifier.object == rig and modifier.show_viewport, '碰撞蒙皮必须使用当前角色骨架')


def _pins(obj, pins):
    _require(isinstance(pins, (list, tuple)) and pins and all(type(i) is int for i in pins),
             '明确指定非空整数根部pin索引；不自动猜测或豁免碰撞')
    selected = sorted(set(pins))
    _require(len(selected) == len(pins) and selected[0] >= 0 and selected[-1] < len(obj.data.vertices),
             '根部pin索引重复或越界')
    adjacency = [set() for _ in obj.data.vertices]
    for polygon in obj.data.polygons:
        face = list(polygon.vertices)
        for a, b in zip(face, face[1:]+face[:1]):
            adjacency[a].add(b); adjacency[b].add(a)
    seen = set(); pinset = set(selected)
    for start in range(len(adjacency)):
        if start in seen:
            continue
        todo = [start]; seen.add(start); component = set()
        while todo:
            node = todo.pop(); component.add(node)
            for other in adjacency[node]-seen:
                seen.add(other); todo.append(other)
        _require(bool(component & pinset), '每个独立面片都需要明确根部pin；孤立点必须固定')
    _require(len(obj.data.polygons) > 0 and len(selected) < len(obj.data.vertices),
             '需要含自由顶点的实际表面；不支持纯边或全部固定')
    return selected


def install(rig, source, *, kind, group_id, pins, colliders, settings=None):
    record = _record(rig); _mesh(source, rig, True)
    _require(isinstance(group_id, str) and re.fullmatch(r'[\w\-\u4e00-\u9fff]{1,48}', group_id),
             '组ID使用1至48个中英文字母、数字、下划线或连字符')
    _require(group_id not in record['groups'], '组ID已安装；重配使用configure，保留现有输出')
    _require(not any(g['source']==source.name for g in record['groups'].values()),
             '同一源Mesh已属于另一个动态组；复制明确输入后再建立独立组')
    config = physics.normalize_settings(kind, settings)
    pins = _pins(source, pins); colliders = list(colliders)
    _require(colliders and len({c.session_uid for c in colliders}) == len(colliders)
             and source not in colliders, '明确指定独立身体碰撞输入，不自动扫描场景')
    for obj in colliders:
        _mesh(obj, rig)
    control = rig.pose.bones.get('骨架设置')
    _require(control is not None, '骨架缺少骨架设置控件')
    enabled, mix = '发裙_'+group_id+'_启用', '发裙_'+group_id+'_混合'
    _require(enabled not in control and mix not in control, '原生动态属性名称冲突，不覆盖用户属性')
    row = {'kind': kind, 'source': source.name, 'colliders': [o.name for o in colliders],
           'pins': pins, 'settings': config, 'enabled': enabled, 'mix': mix, 'bakes': []}
    old = rig.get(STATE_KEY)
    old_layout = rig.get(panel.LAYOUT_KEY)
    try:
        control[enabled] = True; control[mix] = 1.
        control.id_properties_ui(mix).update(min=0., max=1., default=1.)
        record['groups'][group_id] = row; _save(rig, record)
        if old_layout is not None:
            layout=panel.read_layout(rig)
            for key in (enabled,mix):
                layout['rows'].append({'id':uuid.uuid4().hex,'kind':'property','group':'发裙动态 · '+group_id,
                                      'label':key,'visible':True,'bone':'骨架设置','property':key})
            panel._save(rig,layout)
    except Exception:
        for key in (enabled, mix):
            if key in control: del control[key]
        if old is None:
            if STATE_KEY in rig: del rig[STATE_KEY]
        else: rig[STATE_KEY] = old
        if old_layout is not None:rig[panel.LAYOUT_KEY]=old_layout
        raise
    return inspect(rig)


def configure(rig, group_id, settings):
    record = _record(rig); row = record['groups'][group_id]
    config = physics.normalize_settings(row['kind'], settings)
    # A RAM clip is only valid for the parameters it was solved with. Stop the
    # current group's playback and restore its visibility before the record is
    # changed; other groups on the same rig keep their own previews.
    stop_preview(rig, group_id)
    row['settings'] = config; _save(rig, record)
    return config


def _resolve(rig, group_id):
    record = _record(rig); row = record['groups'][group_id]
    source = bpy.context.scene.objects.get(row['source'])
    colliders = [bpy.context.scene.objects.get(n) for n in row['colliders']]
    _mesh(source, rig, True)
    for obj in colliders: _mesh(obj, rig)
    _pins(source, row['pins']); physics.normalize_settings(row['kind'], row['settings'])
    surface._controls(rig, row['enabled'], row['mix'])
    return record, row, source, colliders


def _signature(rig, group_id):
    """Mask animated input values, retain actual animation/driver definitions."""
    record, row, source, colliders = _resolve(rig, group_id)
    from . import elastic
    _require(elastic.session(rig) is None,
             '先将胸臀RAM弹性预览烘焙为原生Action，再捕获发裙；基础动作与原生胸臀NLA可共同输入')
    _require(all(fn in (_frame,elastic._frame) for handlers in
                 (bpy.app.handlers.frame_change_pre,bpy.app.handlers.frame_change_post) for fn in handlers),
             '当前捕获不接外来逐帧回调；请在无其它逐帧求解回调的制作会话重试，原回调不会被卸载')
    _require(bpy.context.scene.render.frame_map_old == bpy.context.scene.render.frame_map_new,
             '当前动态片段需要1:1场景时间映射')
    _require(rig.parent is None and not rig.constraints and rig.data.animation_data is None,
             '当前捕获需要无外部父级/对象约束/骨架数据动画的角色')
    paths = {}
    ad = rig.animation_data
    curves = [f for f in ad.drivers if not f.mute] if ad else []
    actions = []
    if ad:
        _require(not ad.use_tweak_mode, '先退出NLA Tweak')
        if ad.action: actions.append(ad.action)
        # Validate every track even when muted, but only mask curves that can
        # actually evaluate. A muted/disabled/non-solo track must not hide a
        # later hand edit to its would-be channel.
        for track in ad.nla_tracks:
            for strip in track.strips:
                _require(strip.type != 'META', '当前捕获不支持META NLA，请准备独立基础动作')
        solo = any(getattr(track, 'is_solo', False) and not track.mute
                   for track in ad.nla_tracks)
        if ad.use_nla:
            for track in ad.nla_tracks:
                if track.mute or (solo and not getattr(track, 'is_solo', False)):
                    continue
                for strip in track.strips:
                    if strip.mute:
                        continue
                    if strip.action:
                        actions.append(strip.action)
        for curve in ad.drivers:
            for variable in curve.driver.variables:
                if variable.type == 'SINGLE_PROP':
                    _require(len(variable.targets) == 1 and variable.targets[0].id is not None,
                             '捕获不接空SINGLE_PROP driver目标；先修复或移除该无关驱动')
                for target in variable.targets:
                    _require(target.id in (rig,rig.data)
                             or (target.id==bpy.context.scene and target.data_path in ('frame_current','frame_subframe')),
                             '捕获不接未跟踪的外部driver输入；先建立明确独立基础动作')
    for action in actions:
        for layer in action.layers:
            for strip in layer.strips:
                for slot in action.slots:
                    bag = strip.channelbag(slot)
                    if bag: curves.extend(bag.fcurves)
    for curve in curves: paths.setdefault(curve.data_path, set()).add(curve.array_index)
    internal_driver_inputs=[]
    for curve in ad.drivers if ad else []:
        for variable in curve.driver.variables:
            if variable.type=='SINGLE_PROP':
                for target in variable.targets:
                    if target.id in (rig,rig.data) and target.data_path:
                        masked=target.id==rig and target.data_path in paths
                        component=re.fullmatch(r'(.*)\[(\d+)\]',target.data_path)
                        if target.id==rig and component:
                            masked=masked or int(component[2]) in paths.get(component[1],set())
                        internal_driver_inputs.append([target.id.session_uid,target.data_path,
                            None if masked else sig._plain(target.id.path_resolve(target.data_path))])
    controls = {g[k] for g in record['groups'].values() for k in ('enabled', 'mix')}
    def inputs(owner):
        values = sig._rna(owner)
        for field in tuple(values):
            if field in {'matrix','matrix_basis','matrix_channel','matrix_world','matrix_local',
                         'select','hide','dimensions','active_material_index'}:
                del values[field]; continue
            try: path = owner.path_from_id(field)
            except Exception: path = field
            if path in paths:
                value = values[field]
                values[field] = [None if i in paths[path] else x for i,x in enumerate(value)] if isinstance(value,list) else None
        return values
    bones = []
    for pb in rig.pose.bones:
        for constraint in pb.constraints:
            targets=[getattr(constraint,'target',None),getattr(constraint,'pole_target',None)]
            targets.extend(getattr(t,'target',None) for t in getattr(constraint,'targets',()))
            _require(all(t is None or t==rig for t in targets), '捕获不接未跟踪的外部骨约束目标')
        properties = {k:sig._plain(v) for k,v in pb.items()
                      if not (pb.name == '骨架设置' and k in controls)}
        for key in properties:
            if surface._property_path(pb, key) in paths: properties[key] = None
        bones.append([pb.name, pb.parent.name if pb.parent else None, inputs(pb), properties,
                      [inputs(c) for c in pb.constraints]])
    objects = []
    for obj in [source]+colliders:
        _require(obj.parent is None or obj.parent==rig,'动态表面/碰撞输入不能依赖未跟踪的外部父级')
        objects.append([obj.session_uid, obj.data.session_uid, sig._mesh_signature(obj.data),
                        sig._weights(obj), [sig._rna(m) for m in obj.modifiers],
                        sig._plain(obj.matrix_basis), sig._plain(obj.matrix_parent_inverse),
                        obj.parent.session_uid if obj.parent else None, obj.parent_type, obj.parent_bone])
    return sig._hash({'rig':rig.session_uid, 'data':rig.data.session_uid, 'inputs':inputs(rig),
                     'bones':bones, 'rest':[[b.name, sig._plain(b.matrix_local),b.use_deform,b.bbone_segments]
                                          for b in rig.data.bones],
                     'animation':state._animation(rig), 'objects':objects,
                     'internal_driver_inputs':internal_driver_inputs,
                     'group':{k:v for k,v in row.items() if k != 'bakes'},
                     'fps':[bpy.context.scene.render.fps,bpy.context.scene.render.fps_base],
                     'units':bpy.context.scene.unit_settings.scale_length})


def _evaluated(obj):
    graph = bpy.context.evaluated_depsgraph_get(); evaluated = obj.evaluated_get(graph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
    try:
        co = sig._coordinates(mesh)
        points = (np.asarray(evaluated.matrix_world) @ np.column_stack((co,np.ones(len(co)))).T).T[:,:3]
        return points*bpy.context.scene.unit_settings.scale_length, [tuple(p.vertices) for p in mesh.polygons]
    finally: evaluated.to_mesh_clear()


def session(rig, group_id):
    return _SESSIONS.get((rig.session_uid,group_id))


def _owned_output(rig, source_name, token, obj):
    if not isinstance(obj, bpy.types.Object) or obj.type != 'MESH':
        return False
    if obj.get(OWNER_KEY) != token:
        return False
    try:
        state = json.loads(obj.get(surface.STATE_KEY, '{}'))
    except (TypeError, ValueError):
        return False
    return (isinstance(state, dict) and state.get('schema') == 'c94.surface-key-residual.v1'
            and state.get('source') == source_name and state.get('rig') == rig.name
            and state.get('kind') == 'mesh_key_residual')


def _owned_outputs(rig,group_id,scene):
    row = _record(rig)['groups'][group_id]
    objects=[]
    for item in row['bakes']:
        token = item['token']
        named = scene.objects.get(item['object'])
        if named is not None:
            _require(_owned_output(rig, row['source'], token, named),
                     '旧动态输出身份改变；保留对象并检查明确动态组：' + named.name)
        candidates = [obj for obj in scene.objects
                      if _owned_output(rig, row['source'], token, obj)]
        _require(len(candidates) == 1,
                 '旧动态输出无法按token/rig唯一证明身份；保留对象并明确整理记录：' + item['object'])
        obj = candidates[0]
        if bpy.context.view_layer.objects.get(obj.name) is not obj:
            bpy.context.view_layer.update()
        _require(bpy.context.view_layer.objects.get(obj.name) == obj,
                 '旧动态输出不在当前视图层，无法隔离显示：' + obj.name)
        objects.append(obj)
    return objects


class Clip:
    def __init__(self, rig, group_id, source, times, positions, affines, signature, statistics):
        self.rig,self.group_id,self.source,self.scene = rig,group_id,source,bpy.context.scene
        self.times,self.positions,self.affines = times,positions,affines
        self.signature,self.statistics = signature,statistics
        self.prepared = None; self.playing=False; self.frozen=False; self._visibility=None
        self.token=uuid.uuid4().hex

    def _check(self):
        _owned_outputs(self.rig,self.group_id,self.scene)
        _require(bpy.context.scene == self.scene and _signature(self.rig,self.group_id) == self.signature,
                 '基础动作/输入/碰撞/参数已改变；保留旧结果并重新捕获')

    def _prepare(self, label):
        self._check()
        if self.prepared is None:
            base=label;index=2
            while label in bpy.data.objects or label+'__时钟' in bpy.data.objects:
                label=base+'_'+str(index);index+=1
            row = _record(self.rig)['groups'][self.group_id]
            self.prepared = surface.prepare(self.source,self.rig,self.times,self.positions,self.affines,
                label=label,enable_property=row['enabled'],mix_property=row['mix'],validate_source=self._check)
        return self.prepared

    def _show(self):
        previous=_owned_outputs(self.rig,self.group_id,self.scene)
        if self._visibility is None:
            self._visibility = (self.source.hide_get(),self.source.hide_render)
        self.source.hide_set(True);self.source.hide_render=True
        for obj in previous:
            if obj!=self.prepared.object:
                obj.hide_set(True);obj.hide_render=True
        self.prepared.object.hide_set(False);self.prepared.object.hide_render=False

    def preview(self, *, name=None):
        old = session(self.rig,self.group_id)
        self._check()
        if old and old is not self: stop_preview(self.rig,self.group_id)
        result = self._prepare(name or self.source.name+'__'+self.group_id+'动态').preview()
        self._show();self.playing=self.prepared.action is None;self.frozen=False
        self.prepared.carrier[surface.FROZEN]=False
        _SESSIONS[(self.rig.session_uid,self.group_id)]=self
        _frame(self.scene)
        bpy.context.view_layer.update()
        return result

    def bake(self, *, name):
        self._check()
        _require(isinstance(name,str) and name.strip() and name not in bpy.data.actions
                 and (self.prepared is None or self.prepared.action is None),
                 '明确指定未使用的新Action名称；已有烘焙保留，重新捕获建立新候选')
        old=session(self.rig,self.group_id)
        key=(self.rig.session_uid,self.group_id);old_record=self.rig[STATE_KEY]
        prior=self.prepared;prior_visibility=self._visibility
        own_flags=(self.playing,self.frozen)
        old_flags=(old.playing,old.frozen,old._visibility) if old else None
        watched=set(_owned_outputs(self.rig,self.group_id,self.scene)+[self.source])
        if prior and prior.object:watched.add(prior.object)
        if old and old.prepared and old.prepared.object:watched.add(old.prepared.object)
        visibility={obj:(obj.hide_get(),obj.hide_render) for obj in watched}
        carrier_state=(prior.carrier[surface.CLOCK],prior.carrier[surface.FROZEN]) if prior and prior.carrier else None
        try:
            result=self._prepare(self.source.name+'__'+self.group_id+'动态').bake(name=name)
            result['object'][OWNER_KEY]=self.token;result['carrier'][OWNER_KEY]=self.token
            record=_record(self.rig);row=record['groups'][self.group_id]
            row['bakes'].append({'object':result['object'].name,'carrier':result['carrier'].name,
                                 'action':result['action'].name,'kind':'mesh_key_residual','token':self.token})
            _save(self.rig,record)
            if old and old is not self:stop_preview(self.rig,self.group_id)
            self._show();self.playing=False;self.frozen=False
            _SESSIONS[key]=self
            return result
        except Exception:
            self.rig[STATE_KEY]=old_record
            if self.prepared:
                if prior is None:
                    self.prepared._discard();self.prepared=None
                elif self.prepared.action:
                    action=self.prepared.action;self.prepared.carrier.animation_data_clear()
                    self.prepared.action=None;action.use_fake_user=False
                    if action.users==0:bpy.data.actions.remove(action)
                    for obj in (self.prepared.object,self.prepared.carrier):
                        if OWNER_KEY in obj:del obj[OWNER_KEY]
                    self.prepared.carrier[surface.CLOCK],self.prepared.carrier[surface.FROZEN]=carrier_state
            for obj,flags in visibility.items():obj.hide_set(flags[0]);obj.hide_render=flags[1]
            self._visibility=prior_visibility;self.playing,self.frozen=own_flags
            if old:
                old.playing,old.frozen,old._visibility=old_flags;_SESSIONS[key]=old
            else:_SESSIONS.pop(key,None)
            bpy.context.view_layer.update()
            raise


def stop_preview(rig, group_id):
    clip=session(rig,group_id)
    if clip:
        clip.playing=False;clip.frozen=False
        if clip.prepared and clip.prepared.object:
            clip.prepared.object.hide_set(True);clip.prepared.object.hide_render=True
        if clip._visibility is not None:
            # Restore only values this workflow applied; do not overwrite later changes.
            if clip.source.hide_get():clip.source.hide_set(clip._visibility[0])
            if clip.source.hide_render:clip.source.hide_render=clip._visibility[1]
            clip._visibility=None
    return clip


def freeze(rig, group_id):
    clip=session(rig,group_id);_require(clip and clip.playing,'先预览当前RAM动态片段')
    clip._check();clip.playing=False;clip.frozen=True
    clip.prepared.carrier[surface.FROZEN]=True
    clip.prepared.carrier.update_tag(refresh={'OBJECT'});bpy.context.view_layer.update()
    return clip


def reset(rig, group_id):
    stop_preview(rig,group_id)
    row=_record(rig)['groups'][group_id]
    rig.pose.bones['骨架设置'][row['enabled']]=False
    rig.update_tag(refresh={'OBJECT'});bpy.context.view_layer.update()


def capture(rig, group_id, *, start, end, preroll=12, max_samples=300):
    global _BUSY
    _require(not _BUSY,'已有发裙捕获正在执行')
    _require(all(type(v) is int for v in (start,end,preroll,max_samples)) and start<end
             and preroll>=0 and 2<=end-start+preroll+1<=max_samples<=4096,
             '明确整数帧域/非负预卷，需2..4096样本且不超过预算')
    record,row,source,colliders=_resolve(rig,group_id)
    signature=_signature(rig,group_id);scene=bpy.context.scene
    stamp=(scene.frame_current,scene.frame_subframe)
    frame_times=np.arange(start-preroll,end+1,dtype=float)
    count=len(frame_times);n=len(source.data.vertices)
    collision_n=sum(len(_evaluated(obj)[0]) for obj in colliders)
    numeric_estimate=count*(n*(16*8*2+3*8*3+3*4)+collision_n*(3*8*2+3*4))
    _require(numeric_estimate<=surface.MAX_NUMERIC_BYTES,
             '源LBS/残差及完整碰撞轨迹工作集超过256MiB预算；缩短片段或明确代理点域')
    _BUSY=True
    try:
        affines=[];base=[];collision=[{'name':c.name,'vertices':[],'faces':None} for c in colliders]
        basis=np.column_stack((sig._coordinates(source.data),np.ones(n)))
        for time in frame_times:
            scene.frame_set(int(time),subframe=0.)
            a=surface.capture_affines(source,rig);affines.append(a)
            base.append(np.einsum('nij,nj->ni',a,basis)[:,:3])
            for obj,data in zip(colliders,collision):
                points,faces=_evaluated(obj)
                if data['faces'] is None:data['faces']=faces
                _require(faces==data['faces'],'碰撞输入拓扑随时间改变，不提交部分片段')
                data['vertices'].append(points)
    finally:
        scene.frame_set(stamp[0],subframe=stamp[1]);_BUSY=False
    _require(_signature(rig,group_id)==signature,'采样期间基础输入改变，未运行模拟/写结果')
    for data in collision:data['vertices']=np.asarray(data['vertices'])
    solved=physics.simulate(np.asarray(base),[tuple(p.vertices) for p in source.data.polygons],
        row['pins'],collision,fps=scene.render.fps/scene.render.fps_base,kind=row['kind'],settings=row['settings'])
    _require(_signature(rig,group_id)==signature,'模拟期间基础输入改变，未写结果')
    times=frame_times[preroll:].copy();positions=solved['positions'][preroll:].copy()
    matrices=np.asarray(affines)[preroll:].copy()
    for array in (times,positions,matrices):array.setflags(write=False)
    statistics=dict(solved['statistics'],preroll=preroll,start=start,end=end,
                    numeric_working_set_estimate_bytes=numeric_estimate,
                    output='editable native relative Mesh Key residual + clock Action',
                    source_chain_controls_preserved=True,collision_pin_exemption=False)
    clip=Clip(rig,group_id,source,times,positions,matrices,signature,statistics)
    return clip


@persistent
def _frame(scene,*_args):
    if _BUSY:return
    for clip in list(_SESSIONS.values()):
        try:
            if clip.playing and clip.scene==scene and clip.prepared and clip.prepared.carrier:
                clip.prepared.carrier[surface.CLOCK]=float(scene.frame_current+scene.frame_subframe)
                clip.prepared.carrier.update_tag(refresh={'OBJECT'})
        except ReferenceError:clip.playing=False


@persistent
def _clear(*_args):
    # load_pre can run before a file actually replaces the current scene. Stop
    # every RAM preview first so a cancelled/failed load cannot leave the source
    # hidden and an unowned residual visible. Continue clearing even if one
    # stale reference fails; never touch handlers owned by another instance.
    errors = []
    for key, clip in list(_SESSIONS.items()):
        try:
            stop_preview(clip.rig, clip.group_id)
        except ReferenceError:
            pass
        except Exception as error:
            errors.append(str(error))
    _SESSIONS.clear()
    return errors


@persistent
def _save_preview(*_args):
    for clip in list(_SESSIONS.values()):
        if clip.playing:
            stop_preview(clip.rig,clip.group_id)


def register():
    pairs = ((bpy.app.handlers.frame_change_pre,_frame),(bpy.app.handlers.load_pre,_clear),
             (bpy.app.handlers.save_pre,_save_preview))
    for handlers,fn in pairs:
        _require(fn not in handlers,'发裙动态handler已注册')
    added = []
    try:
        for handlers,fn in pairs:
            handlers.append(fn)
            added.append((handlers,fn))
    except Exception:
        for handlers,fn in reversed(added):
            if fn in handlers:
                handlers.remove(fn)
        raise
    _HANDLERS_OWNED.update(fn for _handlers,fn in added)


def unregister():
    for clip in list(_SESSIONS.values()):
        try:
            stop_preview(clip.rig,clip.group_id)
        except ReferenceError:pass
        except Exception:pass
    _SESSIONS.clear()
    for handlers,fn in ((bpy.app.handlers.frame_change_pre,_frame),(bpy.app.handlers.load_pre,_clear),
                        (bpy.app.handlers.save_pre,_save_preview)):
        if fn in _HANDLERS_OWNED and fn in handlers:
            handlers.remove(fn)
    _HANDLERS_OWNED.clear()
