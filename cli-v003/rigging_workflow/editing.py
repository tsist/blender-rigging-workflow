"""Local, persistent editing of owned positioning markers and reference endpoints.

All coordinates in this public API are world coordinates.  Shared joints come
from explicit landmark aliases and connected source bones, never proximity in
the fitted character.  Ordinary edits do not run a whole-character warp.
"""
import copy
import math
import re

import bpy
from mathutils import Vector

from . import positioning as p

EPS = 1e-8
MATCH = 2e-7


def _vector(value):
    value = tuple(float(x) for x in value)
    if len(value) != 3 or not all(math.isfinite(x) for x in value):
        raise ValueError('端点需要三个有限的世界坐标')
    return Vector(value)


def _candidate(scene, state):
    obj = scene.objects.get(state.get('fitted_reference', ''))
    if (not obj or obj.type != 'ARMATURE' or obj.get(p.OWNER_KEY) != state['owner']
            or obj.library or obj.parent or obj.animation_data or obj.constraints
            or obj.data.users != 1):
        raise ValueError('需要本角色独占的拟合参考骨候选')
    return obj


def _groups(scene, state):
    source = scene.objects.get(state['atlas_reference'])
    if not source or source.type != 'ARMATURE':
        raise ValueError('完整原参考骨缺失')
    parent = {b.name+'/'+e:b.name+'/'+e for b in source.data.bones for e in ('H','T')}
    def find(key):
        if key not in parent:
            raise ValueError('未知参考骨端点：'+key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    def union(a,b):
        a,b=find(a),find(b)
        if a!=b:
            parent[max(a,b)] = min(a,b)
    for marker in state['markers']:
        aliases = [n+'/'+e for n,e in marker['endpoints']]
        for alias in aliases[1:]:
            union(aliases[0],alias)
    for bone in source.data.bones:
        if bone.use_connect and bone.parent:
            union(bone.name+'/H',bone.parent.name+'/T')
    groups = {}
    for key in parent:
        groups.setdefault(find(key),[]).append(key)
    lookup = {key:group for group,keys in groups.items() for key in keys}
    markers = {}
    for marker in state['markers']:
        group=lookup[marker['endpoints'][0][0]+'/'+marker['endpoints'][0][1]]
        lookup[marker['key']] = group
        markers.setdefault(group,[]).append(marker)
    return groups,lookup,markers


def _native(obj):
    return {b.name+'/'+e:list(obj.matrix_world@co)
            for b in obj.data.bones for e,co in (('H',b.head_local),('T',b.tail_local))}


def _marker_native(scene,state):
    objects=p._marker_objects(scene,state)
    bpy.context.view_layer.update()
    return {key:list(obj.matrix_world.translation) for key,obj in objects.items()}


def ensure(scene):
    state=p._state(scene)
    p._geometry(scene,state)
    obj=_candidate(scene,state)
    edit=state.get('editing')
    if not edit or edit.get('reference') != obj.name:
        previous=edit or {}
        state['editing']={'version':1,'reference':obj.name,
            'matrix':[list(r) for r in obj.matrix_world],
            'baseline':_native(obj),'marker_baseline':{m['key']:list(m['position']) for m in state['markers']},
            'overrides':copy.deepcopy(previous.get('overrides',{})),
            'locks':list(previous.get('locks',[])),
            'plane':copy.deepcopy(previous.get('plane',{'origin':[state['mirror_plane_x'],0,0],'normal':[1,0,0]}))}
        p._save(scene,state)
    elif edit['matrix'] != [list(r) for r in obj.matrix_world]:
        raise ValueError('拟合参考对象变换改变，请恢复后再编辑骨端点')
    return state


def selection(context):
    state=p.read_state(context.scene)
    if not state:
        raise ValueError('当前场景没有定位任务')
    candidate=_candidate(context.scene,state)
    if context.mode=='EDIT_ARMATURE':
        if context.edit_object != candidate:
            raise ValueError('只可精调当前拟合参考层')
        return list(dict.fromkeys(b.name+'/'+e for b in candidate.data.edit_bones
                    for e,chosen in (('H',b.select_head),('T',b.select_tail)) if chosen))
    if context.mode not in {'OBJECT','POSE'}:
        raise ValueError('请选择定位标记或拟合参考骨端点')
    if context.mode=='POSE':
        if context.object != candidate:
            raise ValueError('只可选择当前拟合参考层')
        return [b.name+'/'+e for b in context.selected_pose_bones or [] for e in ('H','T')]
    owned={m['object']:m['key'] for m in state['markers']}
    return [owned[o.name] for o in context.selected_objects if o.name in owned]


def positions(scene, keys):
    state=ensure(scene) if bpy.context.mode=='OBJECT' else p.read_state(scene)
    if not state:
        raise ValueError('当前场景没有定位任务')
    obj=_candidate(scene,state)
    groups,lookup,markers=_groups(scene,state)
    raw=_native(obj)
    if obj.mode=='EDIT':
        raw={b.name+'/'+e:list(obj.matrix_world@co) for b in obj.data.edit_bones
             for e,co in (('H',b.head),('T',b.tail))}
    native_markers=_marker_native(scene,state)
    result={}
    for key in keys:
        if key not in lookup:
            raise ValueError('未知端点或标记：'+key)
        result[key]=native_markers[key] if key in native_markers else raw[key]
    return result


def _locked(state, groups, markers):
    locks=set(state['editing'].get('locks',[]))
    return {g for g in groups if g in locks or any(m['locked'] for m in markers.get(g,[]))}


def _snapshot(state, obj, scene, preserve_previous=False):
    saved=copy.deepcopy(state)
    saved.pop('previous',None)
    if not preserve_previous:
        saved['editing'].pop('previous',None)
    return {'owner':state['owner'],'reference':obj.name,'state':saved,
            'endpoints':_native(obj),'markers':_marker_native(scene,state)}


def snapshot(scene):
    sync(scene)
    state=ensure(scene)
    return _snapshot(state,_candidate(scene,state),scene,preserve_previous=True)


def _write(obj, pending):
    """Write only listed native endpoints, retaining each bone's existing roll."""
    active=bpy.context.view_layer.objects.active
    selected=list(bpy.context.selected_objects)
    hidden=obj.hide_get()
    try:
        obj.hide_set(False)
        for item in selected:
            item.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active=obj
        bpy.ops.object.mode_set(mode='EDIT')
        inverse=obj.matrix_world.inverted()
        for key,pos in pending.items():
            name,endpoint=key.rsplit('/',1)
            bone=obj.data.edit_bones[name]
            if endpoint=='H': bone.head=inverse@Vector(pos)
            else: bone.tail=inverse@Vector(pos)
        bpy.ops.object.mode_set(mode='OBJECT')
    finally:
        if obj.mode!='OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        obj.select_set(False)
        obj.hide_set(hidden)
        for item in selected:
            if item.name in bpy.context.view_layer.objects:
                item.select_set(True)
        bpy.context.view_layer.objects.active=active
    bpy.context.view_layer.update()


def _side(keys):
    sides={match.group(1) for key in keys for match in [re.search(r'\.([LR])/',key)] if match}
    return next(iter(sides)) if len(sides)==1 else 'CENTER'


def _counterpart(group,groups,lookup):
    candidates=set()
    for key in groups[group]:
        swapped=re.sub(r'\.([LR])/',lambda m:'.'+('R' if m[1]=='L' else 'L')+'/',key)
        if swapped!=key and swapped in lookup:
            candidates.add(lookup[swapped])
    candidates.discard(group)
    if len(candidates)>1:
        raise ValueError('左右端点关联不唯一：'+group)
    return next(iter(candidates)) if candidates else None


def _reflect(pos,plane,center=False):
    point=_vector(pos);origin=_vector(plane['origin']);normal=_vector(plane['normal']).normalized()
    return list(point-normal*(point-origin).dot(normal)*(1 if center else 2))


def _expand_mirror(state,pending,groups,lookup,locked):
    result=copy.deepcopy(pending);skipped=[]
    for group,pos in pending.items():
        if _side(groups[group])=='CENTER':
            result[group]=_reflect(pos,state['editing']['plane'],center=True)
            continue
        other=_counterpart(group,groups,lookup)
        if not other:
            skipped.append(group);continue
        if other in locked:
            skipped.append(other);continue
        mirrored=_reflect(pos,state['editing']['plane'])
        if other in pending and (Vector(pending[other])-Vector(mirrored)).length>MATCH:
            raise ValueError('两侧同时修改且与实时镜像冲突，请指定一侧或关闭实时镜像')
        result[other]=mirrored
    return result,skipped


def _commit(scene,state,pending,label,checkpoint=True):
    obj=_candidate(scene,state)
    groups,lookup,markers=_groups(scene,state)
    original=_native(obj)
    old_markers=_marker_native(scene,state)
    rollback_state=copy.deepcopy(state)
    before=_snapshot(state,obj,scene)
    expanded={key:list(pos) for group,pos in pending.items() for key in groups[group]}
    final={**original,**expanded}
    for bone in obj.data.bones:
        if (Vector(final[bone.name+'/H'])-Vector(final[bone.name+'/T'])).length<1e-8:
            raise ValueError('局部调整会产生零长骨：'+bone.name)
    try:
        _write(obj,expanded)
        actual=_native(obj)
        marker_objects=p._marker_objects(scene,state)
        for group in pending:
            representative=actual[groups[group][0]]
            for marker in markers.get(group,[]):
                marker_objects[marker['key']].location=representative
        bpy.context.view_layer.update()
        actual_markers=_marker_native(scene,state)
        for marker in state['markers']:
            if lookup[marker['key']] in pending:
                marker.update(position=actual_markers[marker['key']],method='manual_local',status='needs_review')
        edit=state['editing']
        edit['baseline']=actual
        edit['marker_baseline']=actual_markers
        for key in expanded:
            edit['overrides'][key]=actual[key]
        edit['last_operation']={'label':label,'groups':list(pending),'endpoints':list(expanded)}
        if checkpoint:
            edit['previous']=before
        # A local sync cannot apply unrelated re-estimated marker positions.
        # Only positioning.fit may clear a globally stale fitted reference.
        p._save(scene,state)
    except Exception:
        _write(obj,original)
        for key,pos in old_markers.items():
            scene.objects[next(m['object'] for m in state['markers'] if m['key']==key)].location=pos
        p._save(scene,rollback_state)
        raise
    return {'changed':len(pending),'endpoints':len(expanded),'affected':list(expanded),'label':label}


def _native_changes(scene,state):
    groups,lookup,markers=_groups(scene,state)
    raw=_native(_candidate(scene,state));mark=_marker_native(scene,state)
    edit=state['editing'];pending={}
    def add(key,value):
        group=lookup[key]
        if group in pending and (Vector(pending[group])-Vector(value)).length>MATCH:
            raise ValueError('同一共享端点的手改位置冲突：'+key)
        pending[group]=list(value)
    for key,pos in raw.items():
        if (Vector(pos)-Vector(edit['baseline'][key])).length>EPS:
            add(key,pos)
    for key,pos in mark.items():
        if (Vector(pos)-Vector(edit['marker_baseline'][key])).length>EPS:
            add(key,pos)
    return pending


def sync(scene, mirror=False):
    state=ensure(scene)
    groups,lookup,markers=_groups(scene,state)
    pending=_native_changes(scene,state)
    if not pending:
        return {'changed':0,'endpoints':0,'affected':[]}
    locked=_locked(state,groups,markers)
    if set(pending)&locked:
        raise ValueError('锁定端点被外部修改，请恢复或先解锁：'+next(iter(set(pending)&locked)))
    skipped=[]
    if mirror:
        pending,skipped=_expand_mirror(state,pending,groups,lookup,locked)
    report=_commit(scene,state,pending,'同步手工端点')
    report['skipped_locked_or_unpaired']=skipped
    return report


def apply(scene, positions, mirror=False, radius=0.0, strength=1.0, label='调整', checkpoint=True):
    sync(scene)
    state=ensure(scene)
    if state.get('fit_stale'):
        raise ValueError('标记候选已重新估计，请先更新完整参考骨再做局部精调')
    groups,lookup,markers=_groups(scene,state)
    locked=_locked(state,groups,markers)
    pending={}
    for key,pos in positions.items():
        if key not in lookup:
            raise ValueError('未知端点：'+key)
        group=lookup[key];value=list(_vector(pos))
        if group in pending and (Vector(pending[group])-Vector(value)).length>MATCH:
            raise ValueError('同一共享端点的目标冲突：'+key)
        if group in locked:
            raise ValueError('所选端点已锁定：'+key)
        pending[group]=value
    if not pending:
        raise ValueError('请先选择可调整的标记或骨端点')
    radius=float(radius);strength=float(strength)
    if not math.isfinite(radius) or radius<0 or not math.isfinite(strength) or not 0<=strength<=1:
        raise ValueError('邻域半径须非负，强度须在0到1之间')
    if radius>0:
        raw=state['editing']['baseline']
        centers={g:Vector(raw[groups[g][0]]) for g in groups}
        seeds={g:Vector(pos)-centers[g] for g,pos in pending.items()}
        for group,center in centers.items():
            if group in pending or group in locked:
                continue
            weighted=[((1-(center-centers[g]).length/radius)**2,delta)
                      for g,delta in seeds.items() if (center-centers[g]).length<radius]
            if weighted:
                total=sum(w for w,_ in weighted)
                delta=sum((delta*w for w,delta in weighted),Vector())/max(total,1.0)
                pending[group]=list(center+delta*strength)
    skipped=[]
    if mirror:
        pending,skipped=_expand_mirror(state,pending,groups,lookup,locked)
    report=_commit(scene,state,pending,label,checkpoint)
    report['skipped_locked_or_unpaired']=skipped
    return report


def mirror(scene, keys, direction='L_TO_R'):
    sync(scene)
    state=ensure(scene)
    if direction not in {'L_TO_R','R_TO_L'}:
        raise ValueError('镜像方向必须是L_TO_R或R_TO_L')
    groups,lookup,markers=_groups(scene,state)
    locked=_locked(state,groups,markers)
    source_side='L' if direction=='L_TO_R' else 'R'
    raw=state['editing']['baseline'];pending={};skipped=[]
    for key in keys:
        if key not in lookup:
            raise ValueError('未知端点：'+key)
        group=lookup[key]
        if _side(groups[group])!=source_side:
            skipped.append(group);continue
        other=_counterpart(group,groups,lookup)
        if not other or other in locked:
            skipped.append(other or group);continue
        pending[other]=_reflect(raw[groups[group][0]],state['editing']['plane'])
    report=_commit(scene,state,pending,'手动镜像 '+direction) if pending else {'changed':0,'endpoints':0,'affected':[]}
    report['skipped_locked_unpaired_or_direction']=list(dict.fromkeys(skipped))
    return report


def set_mirror_plane(scene, origin, normal):
    sync(scene)
    state=ensure(scene)
    origin,normal=_vector(origin),_vector(normal)
    if normal.length<1e-8:
        raise ValueError('镜像平面法线不能为零')
    state['editing']['plane']={'origin':list(origin),'normal':list(normal.normalized())}
    p._save(scene,state)
    return copy.deepcopy(state['editing']['plane'])


def set_locked(scene, keys, locked):
    sync(scene)
    state=ensure(scene)
    groups,lookup,markers=_groups(scene,state)
    chosen={lookup[key] for key in keys}
    if not chosen:
        raise ValueError('请先选择标记或骨端点')
    state['editing']['previous']=_snapshot(state,_candidate(scene,state),scene)
    locks=set(state['editing']['locks'])
    for group in chosen:
        if locked: locks.add(group)
        else: locks.discard(group)
        for marker in markers.get(group,[]):
            marker['locked']=bool(locked)
    state['editing']['locks']=sorted(locks)
    p._write_markers(scene,state)
    p._save(scene,state)
    return {'changed':len(chosen),'locked':bool(locked)}


def restore(scene, snapshot=None):
    state=ensure(scene)
    explicit=snapshot is not None
    saved=snapshot if explicit else state['editing'].get('previous')
    if not saved or saved.get('owner')!=state['owner']:
        raise ValueError('尚无本角色可恢复的端点检查点')
    restored=copy.deepcopy(saved['state'])
    if any(restored.get(key)!=state.get(key) for key in ('owner','atlas_reference','atlas_mesh','target_mesh','reference_fingerprint','input_fingerprints')):
        raise ValueError('检查点不属于当前角色的相同定位来源')
    p._geometry(scene,restored)
    obj=_candidate(scene,restored)
    if set(saved['endpoints'])!=set(_native(obj)):
        raise ValueError('检查点骨集合不匹配')
    for key,value in saved['endpoints'].items():
        _vector(value)
    for bone in obj.data.bones:
        if (Vector(saved['endpoints'][bone.name+'/H'])-Vector(saved['endpoints'][bone.name+'/T'])).length<1e-8:
            raise ValueError('检查点含零长骨：'+bone.name)
    if set(saved['markers'])!={m['key'] for m in restored['markers']}:
        raise ValueError('检查点标记集合不匹配')
    p._marker_objects(scene,restored)
    for value in saved['markers'].values():
        _vector(value)
    previous=_snapshot(state,_candidate(scene,state),scene)
    _write(obj,saved['endpoints'])
    # Explicit snapshots are modal/transaction rollbacks. Preserve their prior
    # recovery point instead of committing the cancelled preview as history.
    # A user-requested restore(None) still toggles the last accepted operation.
    if not explicit:
        restored['editing']['previous']=previous
    current_obj=_candidate(scene,state)
    if current_obj!=obj:
        current_obj.hide_set(True);obj.hide_set(False)
    for marker in restored['markers']:
        marker['position']=saved['markers'][marker['key']]
    p._write_markers(scene,restored)
    restored['editing']['baseline']=_native(obj)
    restored['editing']['marker_baseline']=_marker_native(scene,restored)
    p._save(scene,restored)
    return {'restored':True,'reference':obj.name}


def accept_fit(scene,state,candidate):
    """Replay persistent local authoring after a newly computed global fit."""
    edit=state.get('editing',{})
    overrides=edit.get('overrides',{})
    if overrides:
        # A previously shared endpoint always has all its aliases in overrides.
        final={**_native(candidate),**overrides}
        for bone in candidate.data.bones:
            if (Vector(final[bone.name+'/H'])-Vector(final[bone.name+'/T'])).length<1e-8:
                raise ValueError('保留手调会产生零长骨：'+bone.name)
        _write(candidate,overrides)
    edit.update(version=1,reference=candidate.name,matrix=[list(r) for r in candidate.matrix_world],
                baseline=_native(candidate),marker_baseline=_marker_native(scene,state))
    edit.setdefault('overrides',{})
    edit.setdefault('locks',[])
    edit.setdefault('plane',{'origin':[state['mirror_plane_x'],0,0],'normal':[1,0,0]})
    state['editing']=edit
    groups,lookup,markers=_groups(scene,state)
    raw=edit['baseline']
    for group,items in markers.items():
        if any(key in overrides for key in groups[group]):
            for marker in items:
                marker['position']=raw[groups[group][0]]
    p._write_markers(scene,state)
    edit['marker_baseline']=_marker_native(scene,state)

