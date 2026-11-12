# SPDX-License-Identifier: GPL-3.0-or-later
"""C94 elastic clip capture, native delta outputs and isolated Action baking.

The preview replays a complete RAM clip; no solver/depsgraph reentry occurs in
the frame handler. Source edits require recapture. Native baked NLA plays with
the production package absent; base Actions and author control channels remain.
"""
import json
import math
import uuid

import bpy
import numpy as np
from mathutils import Quaternion
from bpy.app.handlers import persistent
from . import control_panel as panel, pose_switching as pose, mode_animation as animation
from . import corrective_authoring as sig, face_capture as capture_state

STATE_KEY='c94_elastic_v1'
SCHEMA='c94.elastic.v1'
COLLECTION='弹性动态输出'
CONSTRAINT='C94弹性叠加v1'
DEFAULTS={'frequency_hz':3.,'damping_ratio':.7,'gravity':[0.,0.,0.],
          'max_angle_degrees':30.,'strength':1.}
SPECS={'BREAST':('胸部',['形变-胸部.L','形变-胸部.R']),
       'GLUTE':('臀部',['形变-臀部.L','形变-臀部.R'])}
_SESSIONS={}
_BUSY=False
_HANDLERS_OWNED=set()
_PREVIEW_FRAME_OWNED=False
_LAST_ERROR=None


def _require(value,message):
    if not value:raise ValueError(message)


def _params(values):
    _require(set(values)==set(DEFAULTS),'弹性参数字段无效')
    d=dict(values);d['gravity']=list(d['gravity'])
    _require(len(d['gravity'])==3 and all(type(v) in (int,float) and math.isfinite(v) for v in d['gravity']), '重力须为有限世界米制三维向量')
    for k,low,high in [('frequency_hz',.1,30.),('damping_ratio',0.,3.),('max_angle_degrees',0.,75.),('strength',0.,1.)]:
        _require(type(d[k]) in (int,float) and math.isfinite(d[k]) and low<=d[k]<=high,'弹性参数超出范围：'+k)
    return d


def _record(rig):
    panel._writable(rig)
    try:d=json.loads(rig[STATE_KEY])
    except (KeyError,TypeError,ValueError) as exc:raise ValueError('先为当前角色安装胸臀弹性输出') from exc
    _require(d.get('schema')==SCHEMA and isinstance(d.get('groups'),dict),'弹性记录版本无效')
    for key,row in d['groups'].items():
        _require(key in SPECS and len(row['targets'])==len(row['helpers'])==2,'弹性组记录无效')
        _params(row['params'])
        for target,helper in zip(row['targets'],row['helpers']):
            pb=rig.pose.bones.get(target);hp=rig.pose.bones.get(helper)
            _require(pb is not None and hp is not None and not hp.bone.use_deform and hp.rotation_mode=='QUATERNION', '弹性输出骨缺失或已改变')
            rest=row.get('helper_rest',{}).get(helper)
            _require(rest is not None and hp.parent is None and not hp.constraints
                and tuple(hp.location)==(0.,0.,0.) and tuple(hp.scale)==(1.,1.,1.)
                and sig._plain(hp.bone.matrix_local)==rest['matrix'] and hp.bone.length==rest['length'],
                '弹性helper静置帧/平移/缩放或层级已改变；请保留检查点检查输出')
            cs=[c for c in pb.constraints if c.name==CONSTRAINT]
            _require(len(cs)==1 and cs[0].type=='COPY_ROTATION' and cs[0].target==rig and cs[0].subtarget==helper
                and cs[0].mix_mode=='AFTER' and cs[0].owner_space=='LOCAL' and cs[0].target_space=='LOCAL'
                and cs[0].use_x and cs[0].use_y and cs[0].use_z and not cs[0].invert_x and not cs[0].invert_y
                and not cs[0].invert_z and cs[0].euler_order=='AUTO'
                and not cs[0].mute and pb.constraints[-1]==cs[0], '弹性唯一叠加路径已改变；保留工程检查约束')
            path=cs[0].path_from_id('influence');ad=rig.animation_data
            curves=[f for f in ad.drivers if f.data_path==path and f.array_index==0] if ad else []
            _require(len(curves)==1,'弹性混合驱动缺失或重复')
            f=curves[0];driver=f.driver
            _require(not f.mute and not f.modifiers and driver.type=='SCRIPTED' and driver.expression=='e*min(max(w,0),1)'
                and len(driver.variables)==2,'弹性混合驱动已改变')
            expected=row.get('driver_mapping',{}).get(target)
            actual={'curve':sig._rna(f),'points':[sig._rna(p) for p in f.keyframe_points],
                    'samples':[list(p.co) for p in f.sampled_points]}
            _require(actual==expected,'弹性混合驱动的原生曲线映射已改变')
            _require(not any(fc.data_path.startswith(hp.path_from_id()+'.') for fc in ad.drivers),
                     '弹性helper有额外驱动，不能同时作为求解写入通道')
            for var,(name,prop) in zip(driver.variables,[('e',row['property_enabled']),('w',row['property_mix'])]):
                _require(var.name==name and var.type=='SINGLE_PROP' and var.targets[0].id==rig
                    and var.targets[0].data_path==animation._property_path(rig.pose.bones['骨架设置'],prop),
                    '弹性混合驱动输入引用已改变')
    return d


def _save(rig,d):rig[STATE_KEY]=json.dumps(d,ensure_ascii=False,allow_nan=False)


def _tracks(rig,d):
    ad=rig.animation_data
    if not ad:return []
    result=[]
    allowed={rig.pose.bones[n].path_from_id('rotation_quaternion') for g in d['groups'].values() for n in g['helpers']}
    for baked in d['bakes']:
        track=ad.nla_tracks.get(baked['track'])
        if track is None:continue
        _require(len(track.strips)==1,'原受管弹性轨道内容已改变；保留并明确整理轨道')
        strip=track.strips[0];action=strip.action
        _require(action is not None and action.name==baked['action'] and strip.action_slot is not None
            and strip.action_slot.identifier==baked['slot'], '原受管弹性轨道已换Action或Slot；不能按同名轨道接管')
        for layer in action.layers:
            for action_strip in layer.strips:
                _require(action_strip.type=='KEYFRAME','原弹性Action条带类型已改变')
                for bag in action_strip.channelbags:
                    _require(all(f.data_path in allowed and f.array_index in range(4) for f in bag.fcurves),
                        '原弹性Action含受管helper以外的通道；保留原数据，不静音或接管')
        result.append(track)
    return result


def _track_identity(track):
    return [(s.as_pointer(),s.action.session_uid if s.action else None,
             s.action_slot.identifier if s.action_slot else None,
             [(f.data_path,f.array_index) for layer in s.action.layers for action_strip in layer.strips
              for bag in action_strip.channelbags for f in bag.fcurves] if s.action else []) for s in track.strips]


def inspect(rig):
    d=_record(rig)
    return {'rig':rig.name,'groups':d['groups'],'bakes':d['bakes'],
            'preview':session(rig) is not None,'scope':'captured base clip; recapture after source/parameter edits'}


def install(rig):
    panel._writable(rig,data=True);pose._metadata(rig)
    if STATE_KEY in rig:return inspect(rig)
    _require(rig.mode!='EDIT' and rig.data.pose_position=='POSE','结束编辑模式并使用姿态求值')
    settings=rig.pose.bones.get('骨架设置');_require(settings is not None,'缺少骨架设置')
    groups={}
    for key,(word,targets) in SPECS.items():
        present=[n in rig.pose.bones for n in targets]
        _require(all(present) or not any(present),'胸臀模块不完整')
        if all(present):groups[key]={'targets':targets,'helpers':['弹性输出-'+n[3:] for n in targets],
            'property_enabled':word+'弹性启用','property_mix':word+'弹性混合','params':dict(DEFAULTS)}
    _require(groups,'当前配置未启用胸部或臀部模块')
    helpers=[n for g in groups.values() for n in g['helpers']]
    _require(not any(n in rig.data.bones for n in helpers) and COLLECTION not in rig.data.collections_all,'弹性输出名称已被使用')
    props=[n for g in groups.values() for n in (g['property_enabled'],g['property_mix'])]
    _require(not any(n in settings for n in props),'弹性属性已存在，未覆盖')
    layout=rig.get(panel.LAYOUT_KEY);created=[];collection=None
    selected={pb.name for pb in rig.pose.bones if pb.select};active=rig.data.bones.active.name if rig.data.bones.active else None
    before={p.name:tuple(v for row in p.matrix for v in row) for p in pose._update(rig).pose.bones}
    try:
        with panel._preserve_context(bpy.context,rig):
            bpy.ops.object.mode_set(mode='EDIT')
            for group in groups.values():
                for target,helper in zip(group['targets'],group['helpers']):
                    original=rig.data.edit_bones[target];bone=rig.data.edit_bones.new(helper)
                    bone.length=original.length;bone.matrix=original.matrix.copy();bone.use_deform=False
            bpy.ops.object.mode_set(mode='OBJECT')
            collection=rig.data.collections.new(COLLECTION);collection.is_visible=False
            for group in groups.values():
                for field in ('property_enabled','property_mix'):
                    name=group[field];settings[name]=True if field=='property_enabled' else 1.
                    if field=='property_mix':settings.id_properties_ui(name).update(min=0.,max=1.,soft_min=0.,soft_max=1.)
                for target,helper in zip(group['targets'],group['helpers']):
                    hp=rig.pose.bones[helper];hp.rotation_mode='QUATERNION';hp.bone.hide_select=True;collection.assign(hp.bone)
                    c=rig.pose.bones[target].constraints.new('COPY_ROTATION');created.append(c)
                    c.name=CONSTRAINT;c.target=rig;c.subtarget=helper;c.owner_space='LOCAL';c.target_space='LOCAL';c.mix_mode='AFTER'
                    f=c.driver_add('influence');f.driver.type='SCRIPTED';f.driver.expression='e*min(max(w,0),1)'
                    for varname,prop in [('e',group['property_enabled']),('w',group['property_mix'])]:
                        var=f.driver.variables.new();var.name=varname;var.type='SINGLE_PROP'
                        var.targets[0].id=rig;var.targets[0].data_path=animation._property_path(settings,prop)
            current=pose._update(rig)
            error=max(max(abs(a-b) for a,b in zip(row,(v for line in current.pose.bones[n].matrix for v in line))) for n,row in before.items())
            _require(error<=1e-5,'零增量安装改变原生姿态，已撤回')
        for group in groups.values():
            group['helper_rest']={name:{'matrix':sig._plain(rig.data.bones[name].matrix_local),'length':rig.data.bones[name].length} for name in group['helpers']}
            group['driver_mapping']={}
            for target in group['targets']:
                path=rig.pose.bones[target].constraints[CONSTRAINT].path_from_id('influence')
                f=next(f for f in rig.animation_data.drivers if f.data_path==path)
                group['driver_mapping'][target]={'curve':sig._rna(f),'points':[sig._rna(p) for p in f.keyframe_points],
                                               'samples':[list(p.co) for p in f.sampled_points]}
        d={'schema':SCHEMA,'groups':groups,'bakes':[],'zero_install_error':error};_save(rig,d)
        declaration=panel.read_layout(rig)
        for group in groups.values():
            for field in ('property_enabled','property_mix'):
                name=group[field];declaration['rows'].append({'id':uuid.uuid4().hex,'kind':'property','group':'胸臀弹性动态',
                    'label':name,'visible':True,'bone':'骨架设置','property':name})
        panel._save(rig,declaration)
        return inspect(rig)
    except Exception:
        for group in groups.values():
            for target in group['targets']:
                pb=rig.pose.bones.get(target)
                if pb:
                    for c in list(pb.constraints):
                        if c in created:c.driver_remove('influence');pb.constraints.remove(c)
        with panel._preserve_context(bpy.context,rig):
            bpy.ops.object.mode_set(mode='EDIT')
            for name in helpers:
                bone=rig.data.edit_bones.get(name)
                if bone:rig.data.edit_bones.remove(bone)
            bpy.ops.object.mode_set(mode='OBJECT')
        if collection:rig.data.collections.remove(collection)
        for prop in props:
            if prop in settings:del settings[prop]
        if STATE_KEY in rig:del rig[STATE_KEY]
        if layout is not None:rig[panel.LAYOUT_KEY]=layout
        raise
    finally:
        for pb in rig.pose.bones:pb.select=pb.name in selected
        if active and active in rig.data.bones:rig.data.bones.active=rig.data.bones[active]


def configure(rig,group,**kwargs):
    d=_record(rig);_require(group in d['groups'],'当前角色没有该弹性模块')
    _require(set(kwargs)<=set(DEFAULTS),'未知弹性参数')
    d['groups'][group]['params']=_params({**d['groups'][group]['params'],**kwargs});_save(rig,d)
    stop_preview(rig)
    return inspect(rig)


def _base_binding(rig,d):
    ad=rig.animation_data
    if ad:
        _require(not ad.use_tweak_mode,'先退出NLA Tweak')
        own=set(_tracks(rig,d))
        _require(not any(t not in own and not t.mute and len(t.strips) for t in ad.nla_tracks),
                 '先静音非本模块NLA（即使NLA全局关闭）；烘焙需启用原生NLA，不能连带启用外来轨道')
        if ad.action:
            _require(not ad.action.library and not ad.action.override_library and not ad.action.asset_data,'使用本地普通基础Action副本')
            _require(ad.action_blend_type=='REPLACE' and ad.action_influence==1. and ad.action_extrapolation=='HOLD','基础Action须REPLACE/满影响/HOLD')
            animation._bag(ad.action,ad.action_slot)
    return ad


def _signature(rig,d):
    """Boundary guard: writable unanimated inputs plus complete base Action.

    Managed helper values, playback enable/mix and generated NLA are outputs.
    Animated input components are masked because sampling changes scene time.
    """
    ad=_base_binding(rig,d);paths={}
    curves=list(ad.drivers) if ad else []
    if ad and ad.action:
        bag=animation._bag(ad.action,ad.action_slot)
        if bag:curves+=list(bag.fcurves)
    for fc in curves:paths.setdefault(fc.data_path,set()).add(fc.array_index)
    def inputs(owner,omit=()):
        values=sig._rna(owner)
        for field in tuple(values):
            if field in {'matrix','matrix_basis','matrix_channel','matrix_world','matrix_local','select','hide','dimensions'} or field in omit:del values[field];continue
            try:path=owner.path_from_id(field)
            except Exception:path=field
            if path in paths:
                value=values[field];values[field]=[None if i in paths[path] else x for i,x in enumerate(value)] if isinstance(value,list) else None
        return values
    helpers={n for g in d['groups'].values() for n in g['helpers']}
    propnames={n for g in d['groups'].values() for n in (g['property_enabled'],g['property_mix'])}
    bones=[]
    for pb in rig.pose.bones:
        if pb.name in helpers:continue
        props={k:v for k,v in pb.items() if not (pb.name=='骨架设置' and k in propnames)}
        for k in tuple(props):
            if animation._property_path(pb,k) in paths:props[k]=None
        bones.append([pb.name,pb.parent.name if pb.parent else None,sig._plain(pb.bone.matrix_local),pb.bone.use_deform,
            inputs(pb),sig._plain(props),[inputs(c) for c in pb.constraints if c.name!=CONSTRAINT]])
    all_drivers=[]
    for fc in ad.drivers if ad else []:
        if CONSTRAINT in fc.data_path:continue
        all_drivers.append([sig._rna(fc),sig._rna(fc.driver),
            [[sig._rna(v),[[sig._rna(t),t.id.session_uid if t.id else None] for t in v.targets]] for v in fc.driver.variables],
            [sig._rna(p) for p in fc.keyframe_points],[sig._rna(m) for m in fc.modifiers]])
    return sig._hash({'bones':bones,'object':inputs(rig,('active_material_index',)),
        'parent':rig.parent.session_uid if rig.parent else None,'drivers':all_drivers,
        'action':capture_state._action(ad.action) if ad else None,
        'slot':ad.action_slot_handle if ad else None,'groups':{k:g['params'] for k,g in d['groups'].items()},
        'units':bpy.context.scene.unit_settings.scale_length,'fps':[bpy.context.scene.render.fps,bpy.context.scene.render.fps_base]})


def _zero(rig,d):
    for group in d['groups'].values():
        for helper in group['helpers']:rig.pose.bones[helper].rotation_quaternion=(1.,0.,0.,0.)
    rig.update_tag(refresh={'OBJECT'})


def _zero_existing(rig,d):
    # STOP/invalidations must not turn a renamed/deleted managed helper into an
    # endlessly repeating KeyError. Only write helpers that still exist.
    for group in d['groups'].values():
        for helper in group['helpers']:
            pb=rig.pose.bones.get(helper)
            if pb is not None:pb.rotation_quaternion=(1.,0.,0.,0.)
    rig.update_tag(refresh={'OBJECT'})


def session(rig):
    try:return _SESSIONS.get(rig.session_uid)
    except ReferenceError:return None


def stop_preview(rig):
    clip=session(rig)
    if clip:
        _SESSIONS.pop(rig.session_uid,None)
        _restore_preview_tracks(clip)
        try:_zero_existing(rig,_record(rig))
        except (ReferenceError,RuntimeError,ValueError,KeyError):pass
        pose._update(rig)
    return {'stopped':clip is not None,'rig':rig.name}


def _restore_preview_tracks(clip):
    # Restore only tracks this preview muted and whose identity still matches;
    # later user edits to the managed tracks are preserved.
    for track,muted,identity in getattr(clip,'_preview_tracks',()):
        try:
            if track.mute and _track_identity(track)==identity:track.mute=muted
        except ReferenceError:pass


def _invalidate(uid,clip,error):
    global _LAST_ERROR
    _SESSIONS.pop(uid,None)
    try:_restore_preview_tracks(clip)
    except Exception:pass
    _LAST_ERROR='弹性预览已停止；受管helper缺失/重命名或输入改变，请恢复helper或重新安装弹性输出：'+str(error)
    try:clip.error=_LAST_ERROR
    except Exception:pass


def freeze(rig):
    clip=session(rig);_require(clip is not None,'先计算并播放预览片段')
    clip._check();clip._apply(bpy.context.scene.frame_current+bpy.context.scene.frame_subframe)
    _SESSIONS.pop(rig.session_uid,None)
    # Keep managed playback tracks muted: the retained delta is a static native
    # helper pose relative to the moving base, not a frozen world-space body.
    pose._update(rig)
    return {'frozen':True,'scope':'current relative delta only; base controls and Actions retained'}


def reset(rig):
    stop_preview(rig);d=_record(rig)
    for track in _tracks(rig,d):track.mute=True
    _zero(rig,d);pose._update(rig)
    return {'reset':True,'scope':'managed elastic delta reset at current base; no main-control reset'}


class Clip:
    def _check(self):
        _require(self.rig.name in self.scene.objects and self.rig.session_uid==self.uid
                 and self.rig.data.session_uid==self.data_uid and bpy.context.scene==self.scene,'弹性片段场景或角色身份改变')
        _require(self.scene.render.frame_map_old==self.scene.render.frame_map_new,
                 '弹性片段仅支持1:1场景时间；恢复时间映射后重新采样')
        d=_record(self.rig)
        _require(_signature(self.rig,d)==self.signature,'基础动作、输入、骨架、单位或弹性参数改变；重新采样预览')
        return d

    def _apply(self,frame):
        if frame<self.start or frame>self.end:
            _zero(self.rig,self.record);return
        index=(frame-self.sample_start)*self.substeps
        lo=max(0,min(len(self.times)-1,int(math.floor(index))));hi=min(len(self.times)-1,lo+1);weight=index-lo
        for helper,values in self.quaternions.items():
            q=(1-weight)*values[lo]+weight*values[hi];q=q/np.linalg.norm(q)
            self.rig.pose.bones[helper].rotation_quaternion=q.tolist()
        self.rig.update_tag(refresh={'OBJECT'})

    def preview(self):
        self._check();stop_preview(self.rig)
        self._preview_tracks=[(t,t.mute,_track_identity(t)) for t in _tracks(self.rig,self.record)]
        for track,_,_ in self._preview_tracks:track.mute=True
        _SESSIONS[self.uid]=self
        global _PREVIEW_FRAME_OWNED
        if _frame not in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.append(_frame)
            _PREVIEW_FRAME_OWNED=True
        self._apply(self.scene.frame_current+self.scene.frame_subframe);pose._update(self.rig)
        return {'preview':True,'samples':len(self.times),'start':self.start,'end':self.end,'statistics':self.statistics,
                'scope':'captured complete clip; parameter/base edits require recapture'}

    def bake(self,*,name):
        d=self._check();_require(isinstance(name,str) and name.strip() and name not in bpy.data.actions,'烘焙Action名称为空或已使用')
        rig=self.rig;ad=rig.animation_data_create();old_use_nla=ad.use_nla
        original_action=ad.action;original_slot=ad.action_slot
        before={t:t.mute for t in _tracks(rig,d)};action=None;track=None
        previous_session=session(rig);previous_record=rig[STATE_KEY]
        previous_helpers={n:tuple(rig.pose.bones[n].rotation_quaternion) for n in self.quaternions}
        try:
            action=bpy.data.actions.new(name);slot=action.slots.new(id_type='OBJECT',name=rig.name)
            strip=action.layers.new('胸臀弹性输出').strips.new(type='KEYFRAME');bag=strip.channelbag(slot,ensure=True)
            selected=np.flatnonzero((self.times>=self.start)&(self.times<=self.end))
            for helper,values in self.quaternions.items():
                for axis in range(4):
                    fc=bag.fcurves.new(rig.pose.bones[helper].path_from_id('rotation_quaternion'),index=axis)
                    fc.keyframe_points.add(len(selected))
                    points=np.column_stack((self.times[selected],values[selected,axis])).astype(np.float32)
                    fc.keyframe_points.foreach_set('co',points.ravel())
                    for point in fc.keyframe_points:point.interpolation='LINEAR'
                    fc.update()
            track=ad.nla_tracks.new();track.name='C94弹性-'+uuid.uuid4().hex[:12]
            native=track.strips.new(name,self.start,action);native.action_slot=slot
            native.action_frame_start=self.start;native.action_frame_end=self.end
            native.frame_start=self.start;native.frame_end=self.end;native.blend_type='REPLACE';native.extrapolation='NOTHING'
            ad.use_nla=True
            for t in before:t.mute=True
            _SESSIONS.pop(self.uid,None);_zero(rig,d);pose._update(rig)
            _require(ad.action==original_action and ad.action_slot==original_slot,'烘焙改变了基础Action绑定')
            record={'action':action.name,'track':track.name,'slot':slot.identifier,'start':self.start,'end':self.end,
                    'preroll':self.preroll,'substeps':self.substeps,'source_signature':self.signature,'samples':len(selected),
                    'scope':'managed helper quaternion curves only; base Action retained; no runtime solver required'}
            d['bakes'].append(record);_save(rig,d)
            return {'baked':True,**record}
        except Exception:
            if track:ad.nla_tracks.remove(track)
            if action and action.users==0:bpy.data.actions.remove(action)
            ad.use_nla=old_use_nla
            for t,muted in before.items():t.mute=muted
            rig[STATE_KEY]=previous_record
            if previous_session:_SESSIONS[self.uid]=previous_session
            for n,q in previous_helpers.items():rig.pose.bones[n].rotation_quaternion=q
            pose._update(rig)
            raise


def capture(rig,*,start,end,preroll=12,substeps=4,max_samples=10000):
    global _BUSY
    d=_record(rig);pose._metadata(rig);_base_binding(rig,d)
    _require(all(type(v) is int for v in (start,end,preroll,substeps,max_samples)) and -100000<=start<end<=100000
        and 0<=preroll<=10000 and substeps in {1,2,4,8} and (end-start+preroll)*substeps+1<=max_samples<=100000,
        '使用有界整数帧域/预卷、1/2/4/8子步和明确样本预算')
    scene=bpy.context.scene;_require(rig.data.pose_position=='POSE' and rig.mode!='EDIT','先结束编辑并使用姿态显示')
    _require(scene.render.frame_map_old==scene.render.frame_map_new,'当前弹性采样不支持场景时间重映射')
    _require(rig.parent is None and not rig.constraints,'当前弹性基础采样使用无外部父级/对象约束的生成角色')
    scale=scene.unit_settings.scale_length;fps=scene.render.fps/scene.render.fps_base
    _require(math.isfinite(scale) and scale>0 and math.isfinite(fps) and fps>0,'单位与fps无效')
    signature=_signature(rig,d);snapshot=animation._snapshot(rig)
    frame=scene.frame_current;subframe=scene.frame_subframe;tracks=[(t,t.mute) for t in _tracks(rig,d)]
    helpers={n for g in d['groups'].values() for n in g['helpers']}
    ad=rig.animation_data
    if ad and ad.action:
        bag=animation._bag(ad.action,ad.action_slot)
        _require(not bag or not any(any(n in f.data_path for n in helpers) for f in bag.fcurves),'基础Action不可同时写受管弹性输出；先保留并整理动作')
    times=np.arange((end-start+preroll)*substeps+1,dtype=float)/substeps+(start-preroll)
    samples={target:[] for g in d['groups'].values() for target in g['targets']}
    _BUSY=True
    try:
        for track,_ in tracks:track.mute=True
        _zero(rig,d)
        for time in times:
            integer=math.floor(time);scene.frame_set(integer,subframe=time-integer);evaluated=pose._update(rig)
            for target in samples:
                pb=evaluated.pose.bones[target];matrix=evaluated.matrix_world@pb.matrix
                a=np.asarray(matrix,dtype=float);a[:3,3]*=scale;a[:3,:3]*=scale
                _require(np.isfinite(a).all() and np.linalg.det(a[:3,:3])>1e-12,'弹性输出矩阵须有限、可逆、正尺度')
                samples[target].append((a[:3,3].copy(),a[:3,3]+a[:3,:3]@np.array([0.,pb.bone.length,0.]),np.linalg.inv(a[:3,:3])))
    finally:
        scene.frame_set(frame,subframe=subframe)
        for n,values in snapshot['bones'].items():
            pb=rig.pose.bones[n]
            for field in pose._CHANNELS:setattr(pb,field,values[field])
            pb.rotation_mode=values['rotation_mode']
        for (n,k),v in snapshot['modes'].items():rig.pose.bones[n][k]=v
        for field in animation._OBJECT_CHANNELS:setattr(rig,field,snapshot['object'][field])
        rig.rotation_mode=snapshot['object']['rotation_mode']
        for (path,index),value in snapshot['animated'].items():animation._restore_value(rig,path,index,value)
        for track,muted in tracks:track.mute=muted
        pose._update(rig);_BUSY=False
    _require(_signature(rig,d)==signature,'采样未保持原基础输入；保留检查点')
    from .elastic_solver import solve
    clip=Clip();clip.rig=rig;clip.uid=rig.session_uid;clip.data_uid=rig.data.session_uid;clip.scene=scene;clip.record=d;clip.signature=signature
    times.setflags(write=False)
    clip.times=times;clip.start=start;clip.end=end;clip.sample_start=start-preroll;clip.substeps=substeps;clip.preroll=preroll
    clip.quaternions={};clip.statistics={};clip._preview_tracks=[]
    for key,group in d['groups'].items():
        for target,helper in zip(group['targets'],group['helpers']):
            heads,tips,inverse=zip(*samples[target]);result=solve(np.array(heads),np.array(tips),np.array(inverse),dt=1/(fps*substeps),**group['params'])
            result['quaternions'].setflags(write=False)
            clip.quaternions[helper]=result['quaternions'];clip.statistics[target]=result['statistics']
    return clip


@persistent
def _frame(scene,*_args):
    if _BUSY:return
    for uid,clip in tuple(_SESSIONS.items()):
        try:
            if clip.scene==scene:clip._apply(scene.frame_current+scene.frame_subframe)
        except (ReferenceError,RuntimeError,ValueError,KeyError) as error:
            _invalidate(uid,clip,error)


@persistent
def _clear(*_args):
    for clip in tuple(_SESSIONS.values()):
        try:stop_preview(clip.rig)
        except (ReferenceError,RuntimeError,ValueError,KeyError):pass
    _SESSIONS.clear()


def register():
    pairs=((bpy.app.handlers.load_pre,_clear),(bpy.app.handlers.save_pre,_clear))
    for handlers,callback in pairs:
        _require(callback not in handlers,'弹性动态handler已注册')
    added=[]
    try:
        for handlers,callback in pairs:
            handlers.append(callback);added.append((handlers,callback))
    except Exception:
        for handlers,callback in reversed(added):
            if callback in handlers:handlers.remove(callback)
        raise
    _HANDLERS_OWNED.update(callback for _handlers,callback in added)


def unregister():
    global _PREVIEW_FRAME_OWNED
    _clear()
    for handlers,callback in ((bpy.app.handlers.frame_change_post,_frame),(bpy.app.handlers.load_pre,_clear),(bpy.app.handlers.save_pre,_clear)):
        if callback in _HANDLERS_OWNED and callback in handlers:handlers.remove(callback)
    if _PREVIEW_FRAME_OWNED and _frame in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame)
    _PREVIEW_FRAME_OWNED=False
    _HANDLERS_OWNED.clear()
