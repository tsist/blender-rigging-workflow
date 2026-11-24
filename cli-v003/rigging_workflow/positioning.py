"""Owned, persistent positioning state shared by UI and public CLI workers.

Current estimator is an experimental neutral surface-atlas CPD proposal.
No target armature, vertex groups, old marker coordinates, or animation is read.
All fits are complete nondeforming reference candidates, never generated rigs.
"""
import copy
import hashlib
import json
import re
import uuid

import bpy
import numpy as np
from mathutils import Vector

from .registration import LandmarkWarp, propose_surface, propose_lip_region, lip_region_points
from .reference import create_reference

STATE_KEY = 'c94_positioning_state_v1'
DOMAINS = ('body', 'hands', 'face')
OWNER_KEY = 'c94_positioning_owner'


def read_state(scene):
    raw = scene.get(STATE_KEY)
    if raw is None:
        return None
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get('version') != 1:
        raise ValueError('定位状态版本无效')
    return state


def _save(scene, state):
    scene[STATE_KEY] = json.dumps(state, ensure_ascii=False, allow_nan=False)


def _state(scene):
    if scene != bpy.context.scene:
        raise ValueError('定位操作需要明确激活所属场景')
    if bpy.context.mode != 'OBJECT':
        raise ValueError('请先结束编辑模式')
    state = read_state(scene)
    if not state:
        raise ValueError('请先建立明确部件的定位工作副本')
    return state


def mesh_points(obj):
    """Only explicit neutral snapshots: reject hidden deform/evaluation inputs."""
    if obj.type != 'MESH' or obj.modifiers or obj.data.shape_keys or obj.parent or obj.animation_data or obj.vertex_groups:
        raise ValueError('定位输入必须为无父级/修改器/Key/动画/旧权重的中立几何快照：'+obj.name)
    a = np.empty(len(obj.data.vertices)*3, dtype=np.float64)
    obj.data.vertices.foreach_get('co', a)
    m = np.array(obj.matrix_world)
    return a.reshape(-1, 3)@m[:3, :3].T+m[:3, 3]


def reference_signature(obj):
    rows = {'object':obj.name,'data':obj.data.name,'matrix':[list(r) for r in obj.matrix_world],
            'bones':[(b.name,list(b.head_local),list(b.tail_local),
                      [list(r) for r in b.matrix_local],b.parent.name if b.parent else None,b.use_connect)
                     for b in obj.data.bones]}
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()


def _geometry(scene, state):
    reference = scene.objects.get(state['atlas_reference'])
    if not reference or reference.type != 'ARMATURE' or reference_signature(reference) != state.get('reference_fingerprint'):
        raise ValueError('原参考骨身份、骨位或变换已改变；请重新准备定位来源')
    arrays = {}
    for role in ('atlas_mesh', 'target_mesh'):
        obj = scene.objects.get(state[role])
        if obj is None:
            raise ValueError('定位输入对象缺失：'+state[role])
        a = mesh_points(obj)
        digest = hashlib.sha256(a.tobytes()).hexdigest()
        if digest != state['input_fingerprints'][role]:
            raise ValueError('中立几何已改变；请重新准备定位输入，不沿用旧标记确认')
        arrays[role] = a
    _face_arrays(arrays, state)
    return arrays


def _face_config(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'mode','atlas_bounds','target_bounds'} or value['mode'] != 'bounds_v1':
        raise ValueError('显式面部域记录损坏；不退回默认范围')
    result = {'mode':'bounds_v1'}
    for field in ('atlas_bounds','target_bounds'):
        try:
            if any(isinstance(number,(bool,np.bool_)) for row in value[field] for number in row):
                raise ValueError()
            raw = np.asarray(value[field])
            if raw.shape != (2,3) or raw.dtype.kind not in 'iuf':
                raise ValueError()
            a = raw.astype(np.float64)
            if not np.isfinite(a).all() or not np.all(a[0] < a[1]):
                raise ValueError()
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('面部范围须为2×3有限实数且min<max：'+field) from error
        result[field] = a.tolist()
    return result


def _face_arrays(arrays, state):
    config = _face_config(state.get('face_domain'))
    if config is None:
        return None
    cropped = []
    for role,field in (('atlas_mesh','atlas_bounds'),('target_mesh','target_bounds')):
        lo,hi = np.asarray(config[field])
        a = arrays[role]
        a = a[np.all((a>=lo)&(a<=hi),axis=1)]
        if len(a)<4 or not np.isfinite(a).all() or np.min(np.ptp(a,axis=0))<=1e-9:
            raise ValueError('面部范围没有足够的三维表面：%s（%d点）' % (field,len(a)))
        cropped.append(a)
    face = [m for m in state['markers'] if m['domain']=='face']
    lo,hi = np.asarray(config['atlas_bounds'])
    outside = [m['key'] for m in face if not np.all((np.asarray(m['source_position'])>=lo)&(np.asarray(m['source_position'])<=hi))]
    if outside:
        raise ValueError('图谱范围未覆盖源面部锚（含锁点）：'+'、'.join(outside[:4]))
    lips = [m['source_position'] for m in face if '嘴唇' in m['key']]
    _,_,roi = lip_region_points(cropped[0],cropped[1],lips)
    return cropped[0],cropped[1],dict(atlas_points=len(cropped[0]),target_points=len(cropped[1]),
        lip_source_points=roi['source_points'],lip_target_points=roi['target_points'])


def _mutation_snapshot(scene, state):
    objects = _marker_objects(scene,state)
    saved = {'state':scene[STATE_KEY], 'markers':{key:{'location':list(obj.location),
        'lock_location':list(obj.lock_location),'color':list(obj.color),
        'properties':{name:obj.get(name) for name in ('locked','status')}} for key,obj in objects.items()}}
    name = state.get('fitted_reference')
    obj = scene.objects.get(name) if isinstance(name,str) and name else None
    if obj is not None:
        saved['reference'] = obj
        saved['bones'] = {b.name:(b.matrix_local.copy(),b.length) for b in obj.data.bones}
        saved['edit_bones'] = _reference_edit_state(obj)
    return saved


def _reference_edit_state(obj, records=None):
    """Capture/replay real head/tail/roll; matrix/length inversion loses float bits."""
    active,selected=bpy.context.view_layer.objects.active,list(bpy.context.selected_objects)
    hidden=obj.hide_get()
    try:
        obj.hide_set(False)
        for item in selected:item.select_set(False)
        obj.select_set(True);bpy.context.view_layer.objects.active=obj
        bpy.ops.object.mode_set(mode='EDIT')
        if records is None:
            return {b.name:(b.head.copy(),b.tail.copy(),float(b.roll)) for b in obj.data.edit_bones}
        for name,(head,tail,roll) in records.items():
            bone=obj.data.edit_bones[name];bone.head=head;bone.tail=tail;bone.roll=roll
    finally:
        if obj.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        obj.select_set(False);obj.hide_set(hidden)
        for item in selected:item.select_set(True)
        bpy.context.view_layer.objects.active=active


def _restore_mutation(scene, state, saved):
    errors = []
    obj = saved.get('reference')
    if obj is not None:
        changed = any(b.matrix_local != saved['bones'][b.name][0] or b.length != saved['bones'][b.name][1] for b in obj.data.bones)
        if changed:
            try:
                _reference_edit_state(obj,saved['edit_bones'])
            except Exception as error:
                errors.append('恢复参考骨：'+str(error))
    try:
        for key,obj in _marker_objects(scene,state).items():
            row=saved['markers'][key]
            obj.location=row['location'];obj.lock_location=row['lock_location'];obj.color=row['color']
            for name,value in row['properties'].items():
                if value is None:
                    if name in obj:del obj[name]
                else:obj[name]=value
        scene[STATE_KEY]=saved['state']
        bpy.context.view_layer.update()
    except Exception as error:
        errors.append('恢复定位状态/标记：'+str(error))
    if errors:
        raise RuntimeError('定位操作恢复未完成：'+'；'.join(errors))


def set_face_domain(scene, *, atlas_bounds, target_bounds):
    state = _state(scene)
    arrays = _geometry(scene,state)
    if (atlas_bounds is None)!=(target_bounds is None):
        raise ValueError('同时指定两侧范围，或同时None显式恢复默认范围')
    config = None if atlas_bounds is None else _face_config(dict(mode='bounds_v1',atlas_bounds=atlas_bounds,target_bounds=target_bounds))
    proposed = dict(state,face_domain=config)
    pair = _face_arrays(arrays,proposed)
    counts = pair[2] if pair else None
    if _face_config(state.get('face_domain')) == config:
        return {'changed':False,'face_domain':config,'counts':counts}
    saved = _mutation_snapshot(scene,state)
    try:
        _capture(scene,state)
        _checkpoint(state)
        if config is None:
            state.pop('face_domain',None);state.pop('face_domain_counts',None)
        else:
            state['face_domain']=config;state['face_domain_counts']=counts
        objects=_marker_objects(scene,state)
        for marker in state['markers']:
            if marker['domain']=='face' and not marker['method'].startswith('manual'):
                marker['status']='unestimated';objects[marker['key']]['status']='unestimated'
        state['estimate_status']['face']='范围已变，需重估；手调/锁定坐标保留'
        state['fit_stale']=True
        _save(scene,state)
    except Exception as error:
        try:_restore_mutation(scene,state,saved)
        except Exception as recovery:raise RuntimeError(str(error)+'；'+str(recovery)) from error
        raise
    return {'changed':True,'face_domain':config,'counts':counts}


def _marker_objects(scene, state):
    result = {}
    for m in state['markers']:
        obj = scene.objects.get(m['object'])
        if (not obj or obj.type != 'EMPTY' or obj.get(OWNER_KEY) != state['owner']
                or obj.parent or obj.constraints or obj.animation_data or obj.library
                or len(obj.users_scene) != 1 or obj.delta_location.length > 0):
            raise ValueError('标记丢失、外部引用、动画驱动或身份改变：'+m['key'])
        result[m['key']] = obj
    return result


def _capture(scene, state):
    if state.get('editing') and state.get('fitted_reference'):
        from . import editing
        changed = bool(editing.sync(scene)['changed'])
        current = read_state(scene)
        state.clear()
        state.update(current)
        return changed
    objects = _marker_objects(scene, state)
    bpy.context.view_layer.update()
    changed = False
    for marker in state['markers']:
        pos = list(objects[marker['key']].matrix_world.translation)
        if not all(np.isfinite(pos)):
            raise ValueError('标记含非法坐标')
        if np.linalg.norm(np.array(pos)-marker['position']) > 1e-8:
            if marker['locked']:
                raise ValueError('锁定标记被外部修改，请先解锁或恢复：'+marker['key'])
            marker['position'], marker['method'], marker['status'] = pos, 'manual', 'needs_review'
            changed = True
    if changed:
        state['fit_stale'] = True
    return changed


def _checkpoint(state):
    state['previous'] = {k:copy.deepcopy(state[k]) for k in ('markers', 'estimate_status')}
    for key in ('face_domain','face_domain_counts'):
        state['previous'][key] = copy.deepcopy(state.get(key))
    if state.get('editing') and state.get('fitted_reference'):
        from . import editing
        state['editing']['previous'] = editing._snapshot(state,
            editing._candidate(bpy.context.scene,state),bpy.context.scene)


def _write_markers(scene, state):
    objects = _marker_objects(scene, state)
    for m in state['markers']:
        obj = objects[m['key']]
        obj.location = m['position']
        obj.lock_location = (m['locked'],)*3
        obj['locked'] = m['locked']
        obj['status'] = m['status']
        obj.color = (0.95, 0.30, 0.08, 1) if m['locked'] else (0.05, 0.7, 0.85, 1)
    bpy.context.view_layer.update()
    # Blender stores transforms as float32. Persist native round-tripped values
    # so quantization is never mistaken for a user edit of a locked marker.
    for m in state['markers']:
        actual = list(objects[m['key']].matrix_world.translation)
        if not np.array_equal(np.asarray(actual,dtype=np.float32),np.asarray(m['position'],dtype=np.float32)):
            raise ValueError('原生标记求值覆盖了候选写入：'+m['key'])
        m['position'] = actual


def _landmarks(reference):
    body = {'形变-骶骨','形变-脊柱1','形变-脊柱2','形变-胸腔1','形变-胸腔2',
            '形变-颈椎','形变-头颅'}
    body.update(n+s for s in ('.L','.R') for n in ('大臂FK','小臂FK','大腿FK','小腿FK','形变-脚掌','形变-脚趾','形变-肩膀'))
    definitions = []
    for b in reference.data.bones:
        if re.match(r'^形变-.*(?:指)[123]?[.][LR]$', b.name) or b.name.startswith('形变-手掌.'):
            domain = 'hands'
        elif re.match(r'^形变-(?:嘴唇|眼睑|眉毛|鼻部|下颚|舌部)', b.name):
            domain = 'face'
        elif b.name in body:
            domain = 'body'
        else:
            continue
        for endpoint, p in (('H',b.head_local),('T',b.tail_local)):
            wp = reference.matrix_world@p
            definitions.append({'key':b.name+'/'+endpoint,'domain':domain,'source_position':list(wp),
                                'bone':b.name,'endpoint':endpoint})
    # One native empty per spatial joint. Shared endpoints stay a single editable
    # constraint even when an anatomical joint belongs to several mechanisms.
    unique = []
    for m in sorted(definitions, key=lambda x:({'hands':0,'face':1,'body':2}[x['domain']],x['key'])):
        hit = next((n for n in unique if np.linalg.norm(np.array(n['source_position'])-m['source_position']) < 1e-7),None)
        if hit:
            hit['endpoints'].append([m['bone'],m['endpoint']])
        else:
            m['endpoints'] = [[m.pop('bone'),m.pop('endpoint')]]
            unique.append(m)
    return unique


def prepare(scene, reference, atlas_mesh, target_mesh, collection_name='定位工作区', *, owned_ids=None):
    from . import module_configuration
    module_configuration._own(owned_ids)
    if scene != bpy.context.scene or bpy.context.mode != 'OBJECT' or read_state(scene):
        raise ValueError('准备需要对象模式和无已有定位任务的场景')
    if reference.type != 'ARMATURE' or not reference.get('c94_reference_kind'):
        raise ValueError('需要完整C94原生参考层，不接受目标旧骨架')
    if reference.name not in scene.objects or atlas_mesh.name not in scene.objects or target_mesh.name not in scene.objects:
        raise ValueError('输入必须在本场景内')
    arrays = {'atlas_mesh':mesh_points(atlas_mesh),'target_mesh':mesh_points(target_mesh)}
    source, target = arrays['atlas_mesh'], arrays['target_mesh']
    smin, smax, tmin, tmax = source.min(0), source.max(0), target.min(0), target.max(0)
    if np.min(smax-smin) <= 0 or np.min(tmax-tmin) <= 0:
        raise ValueError('输入没有三维范围')
    before = module_configuration._ids()
    active, selected = bpy.context.view_layer.objects.active, list(bpy.context.selected_objects)
    old_present, old_record = STATE_KEY in scene, scene.get(STATE_KEY)
    created = []
    def own(block):
        module_configuration._own(created, block)
        module_configuration._own(owned_ids, block)
        return block
    try:
        owner = uuid.uuid4().hex
        work = own(bpy.data.collections.new(collection_name))
        scene.collection.children.link(work)
        markers_collection = own(bpy.data.collections.new('定位标记-'+owner[:8]))
        work.children.link(markers_collection)
        markers = _landmarks(reference)
        for m in markers:
            # Bbox map is a visible initialization only, never called auto detection.
            p = (np.array(m['source_position'])-smin)/(smax-smin)*(tmax-tmin)+tmin
            obj = own(bpy.data.objects.new(m['key'], None))
            markers_collection.objects.link(obj)
            obj.location = p
            obj.empty_display_type = 'SPHERE'
            obj.empty_display_size = float((tmax-tmin)[2]*(0.0018 if m['domain']=='face' else 0.0035))
            obj.show_in_front = True
            obj[OWNER_KEY] = owner
            obj['marker_key'] = m['key']
            obj['domain'] = m['domain']
            m.update(object=obj.name, position=p.tolist(), locked=False, method='bbox_initialization',status='unestimated')
        state = {'version':1,'owner':owner,'atlas_reference':reference.name,'atlas_mesh':atlas_mesh.name,
                 'reference_fingerprint':reference_signature(reference),
                 'target_mesh':target_mesh.name,'work_collection':work.name,'marker_collection':markers_collection.name,
                 'input_fingerprints':{k:hashlib.sha256(a.tobytes()).hexdigest() for k,a in arrays.items()},
                 'markers':markers,'estimate_status':{d:'unestimated' for d in DOMAINS},
                 'mirror_plane_x':float((tmin[0]+tmax[0])/2), 'fitted_reference':None,
                 'fit_stale':True,'fit_revision':0,'estimator':'experimental_surface_atlas_cpd',
                 'limitations':'same-axis neutral input and approximate matching regions; anatomical recognition unvalidated'}
        _write_markers(scene,state)
        _save(scene,state)
        return state
    except Exception:
        if old_present:
            scene[STATE_KEY] = old_record
        elif STATE_KEY in scene:
            del scene[STATE_KEY]
        module_configuration._cleanup(before, active, selected, owned_ids=created)
        raise


def _regions(source, target, domain, face_pair=None):
    # Spatial selection is geometric and explicit in the report. It does not
    # inspect target bone/weight names. Replaced by semantic part detection later.
    def normalized(a):
        lo, hi = a.min(0), a.max(0)
        return (a-lo)/(hi-lo)
    sn, tn = normalized(source), normalized(target)
    if domain == 'body':
        yield 'body', source, target, None
    elif domain == 'face':
        if face_pair is not None:
            yield 'face',face_pair[0],face_pair[1],None
        else:
            yield 'face', source[sn[:,2] > 0.83], target[tn[:,2] > 0.83], None
    else:
        for side,sign in (('L',1),('R',-1)):
            sm = sn[:,0] > 0.85 if sign == 1 else sn[:,0] < 0.15
            tm = tn[:,0] > 0.85 if sign == 1 else tn[:,0] < 0.15
            yield 'hand.'+side,source[sm],target[tm],side


def estimate(scene, domains):
    state = _state(scene)
    if not domains or set(domains)-set(DOMAINS):
        raise ValueError('未知定位分区')
    arrays = _geometry(scene,state)
    face_pair = _face_arrays(arrays,state) if 'face' in domains else None
    saved = _mutation_snapshot(scene,state)
    try:
        return _estimate(scene,state,arrays,domains,face_pair)
    except Exception as error:
        try:_restore_mutation(scene,state,saved)
        except Exception as recovery:raise RuntimeError(str(error)+'；'+str(recovery)) from error
        raise


def _estimate(scene,state,arrays,domains,face_pair):
    _capture(scene,state)
    _checkpoint(state)
    reports = {}
    for domain in dict.fromkeys(domains):
        for label, src, dst, side in _regions(arrays['atlas_mesh'],arrays['target_mesh'],domain,face_pair):
            chosen = [m for m in state['markers'] if m['domain']==domain and not m['locked']
                      and (side is None or '.'+side+'/' in m['key'])]
            if not chosen:
                reports[label] = {'status':'all_locked_or_empty'}
                continue
            positions, report = propose_surface(src,dst,[m['source_position'] for m in chosen])
            for marker, pos in zip(chosen,positions):
                marker.update(position=pos.tolist(),method='surface_atlas_cpd',status='needs_review')
            reports[label] = report
        if domain == 'face':
            lips=[m for m in state['markers'] if '嘴唇' in m['key'] and not m['locked']]
            if len(lips)>=4:
                # Crop using all source lip landmarks so locked markers do not
                # change the region definition, but never overwrite their values.
                all_lips=[m for m in state['markers'] if '嘴唇' in m['key']]
                lip_source,lip_target = face_pair[:2] if face_pair is not None else (arrays['atlas_mesh'],arrays['target_mesh'])
                refined,local_report=propose_lip_region(lip_source,lip_target,
                                                       [m['source_position'] for m in all_lips])
                for m,p in zip(all_lips,refined):
                    if not m['locked']:
                        m.update(position=p.tolist(),method='surface_atlas_cpd_lip_region')
                reports['lip_region']=local_report
        pending=sum(m['domain']==domain and m['status']=='unestimated' for m in state['markers'])
        state['estimate_status'][domain] = ('仍有%d个未估计标记，需解锁重估或实际手调' % pending) if pending else '候选：表面配准，需检查解剖位置'
    if face_pair is not None:
        reports['face_domain']=dict(mode='bounds_v1',**face_pair[2])
    reports['symmetry']=_symmetric_geometry_regularization(state,arrays['target_mesh'],set(domains))
    # All requested computations succeeded before any native coordinate write.
    _write_markers(scene,state)
    state['fit_stale'], state['last_estimate'] = True,reports
    if state.get('editing'):
        from . import editing
        state['editing']['marker_baseline'] = editing._marker_native(scene,state)
    _save(scene,state)
    return reports


def _symmetric_geometry_regularization(state, target, domains):
    """Use bilateral regularization only for numerically symmetric geometry.

    No bbox-symmetry assumption: test every reflected surface vertex against the
    actual target surface vertices. Locked pairs and nonrequested domains stay.
    """
    from mathutils.kdtree import KDTree
    tree=KDTree(len(target))
    for i,p in enumerate(target):tree.insert(p,i)
    tree.balance()
    plane=state['mirror_plane_x']
    span=float(np.ptp(target,axis=0).max())
    error=max(tree.find((2*plane-p[0],p[1],p[2]))[2] for p in target)
    if error>span*1e-6:
        return {'applied':False,'reason':'target_not_numerically_symmetric','max_vertex_error':error}
    markers=state['markers']
    source=np.array([m['source_position'] for m in markers])
    changed=[];visited=set()
    for i,m in enumerate(markers):
        if i in visited or m['domain'] not in domains or m['locked']:
            continue
        s=source[i]
        if abs(s[0])<span*1e-6:
            m['position'][0]=plane;changed.append(m['key']);visited.add(i)
            continue
        reflected=s.copy();reflected[0]*=-1
        distances=np.linalg.norm(source-reflected,axis=1)
        j=int(np.argmin(distances));other=markers[j]
        if distances[j]>span*1e-5 or j==i or other['locked'] or other['domain']!=m['domain']:
            continue
        p,q=np.array(m['position']),np.array(other['position'])
        q[0]=2*plane-q[0]
        avg=(p+q)*.5
        m['position']=avg.tolist();avg[0]=2*plane-avg[0];other['position']=avg.tolist()
        visited.update((i,j));changed.extend((m['key'],other['key']))
    return {'applied':True,'max_vertex_error':error,'adjusted_markers':len(changed),
            'reason':'geometry verified symmetric at float precision; locked pairs skipped'}


def sync_markers(scene):
    state = _state(scene)
    if state.get('fitted_reference'):
        from . import editing
        return editing.sync(scene)
    _checkpoint(state)
    changed = _capture(scene,state)
    _save(scene,state)
    return {'changed':changed}


def set_locked(scene, selected_objects, locked):
    state = _state(scene)
    if state.get('fitted_reference'):
        from . import editing
        names = {o.name for o in selected_objects}
        return editing.set_locked(scene,[m['key'] for m in state['markers'] if m['object'] in names],locked)
    _capture(scene,state)
    _checkpoint(state)
    names = {o.name for o in selected_objects}
    changed = [m for m in state['markers'] if m['object'] in names]
    if not changed:
        raise ValueError('请先选择本角色的定位标记')
    for m in changed:
        m['locked'] = bool(locked)
    _write_markers(scene,state)
    _save(scene,state)
    return {'changed':len(changed),'locked':bool(locked)}


def mirror_selected(scene, selected_objects):
    state = _state(scene)
    if state.get('fitted_reference'):
        from . import editing
        names = {o.name for o in selected_objects}
        keys = [m['key'] for m in state['markers'] if m['object'] in names]
        groups,lookup,_ = editing._groups(scene,state)
        sides = {editing._side(groups[lookup[key]]) for key in keys}-{'CENTER'}
        if len(sides)!=1:
            raise ValueError('请选择明确的一侧，或在精调面板指定镜像方向')
        return editing.mirror(scene,keys,'L_TO_R' if 'L' in sides else 'R_TO_L')
    _capture(scene,state)
    _checkpoint(state)
    names = {o.name for o in selected_objects}
    mapping = {m['key']:m for m in state['markers']}
    pending = []
    for m in state['markers']:
        if m['object'] not in names:
            continue
        counterpart = re.sub(r'\.([LR])/',lambda hit:'.'+('R' if hit[1]=='L' else 'L')+'/',m['key'])
        other = mapping.get(counterpart)
        if counterpart == m['key'] or not other or other['locked'] or other['object'] in names:
            continue
        p = list(m['position']); p[0] = 2*state['mirror_plane_x']-p[0]
        pending.append((other,p))
    if not pending:
        raise ValueError('没有未锁定、未同时选中的对应侧标记')
    for m,p in pending:
        m.update(position=p,method='manual_mirror',status='needs_review')
    _write_markers(scene,state)
    state['fit_stale'] = True
    _save(scene,state)
    return {'changed':len(pending)}


def restore_previous(scene):
    state = _state(scene)
    if state.get('editing') and state['editing'].get('previous'):
        from . import editing
        return editing.restore(scene)
    previous = state.get('previous')
    if not previous:
        raise ValueError('尚无可恢复的上一候选')
    restored=copy.deepcopy(state)
    for key in ('face_domain','face_domain_counts'):restored.pop(key,None)
    restored.update(copy.deepcopy(previous))
    _geometry(scene,restored)
    saved=_mutation_snapshot(scene,state)
    try:
        _checkpoint(state)
        restored['previous']=state['previous']
        _write_markers(scene,restored)
        restored['fit_stale'] = True
        _save(scene,restored)
    except Exception as error:
        try:_restore_mutation(scene,state,saved)
        except Exception as recovery:raise RuntimeError(str(error)+'；'+str(recovery)) from error
        raise
    return {'restored':True}


def fit(scene):
    state = _state(scene)
    _geometry(scene,state)
    _capture(scene,state)
    if any(m['status']=='unestimated' for m in state['markers']):
        raise ValueError('仍有未估计或未手调的标记；先完成整套定位候选')
    source = scene.objects.get(state['atlas_reference'])
    if not source or source.type != 'ARMATURE' or source.data.users != 1:
        raise ValueError('完整参考源缺失或共享数据')
    warp = LandmarkWarp([m['source_position'] for m in state['markers']],
                        [m['position'] for m in state['markers']])
    records = []
    for b in source.data.bones:
        h,t = source.matrix_world@b.head_local,source.matrix_world@b.tail_local
        z = source.matrix_world.to_3x3()@b.matrix_local.to_3x3().col[2]
        records.append((b.name,h,t,h+z*max(b.length*0.1,1e-5)))
    transformed = warp([list(p) for r in records for p in r[1:]])
    if np.any(np.linalg.norm(transformed[1::3]-transformed[0::3],axis=1) < 1e-8):
        raise ValueError('拟合产生零长骨，未创建候选')
    work = bpy.data.collections.get(state['work_collection'])
    if not work or work.name not in scene.collection.children:
        raise ValueError('定位工作集合缺失')
    old_active, old_selection = bpy.context.view_layer.objects.active,list(bpy.context.selected_objects)
    rollback_state = copy.deepcopy(state)
    previous_edit = None
    if state.get('fitted_reference'):
        from . import editing
        _save(scene,state)
        state = editing.ensure(scene)
        previous_edit = editing._snapshot(state,editing._candidate(scene,state),scene)
        rollback_state = copy.deepcopy(state)
    candidate = None
    try:
        revision = state['fit_revision']+1
        while '爱音-拟合参考-'+str(revision) in bpy.data.objects:
            revision += 1
        candidate = create_reference(source,work,'爱音-拟合参考-'+str(revision))
        candidate[OWNER_KEY] = state['owner']
        inv = candidate.matrix_world.inverted()
        for o in bpy.context.selected_objects:
            o.select_set(False)
        candidate.select_set(True)
        bpy.context.view_layer.objects.active = candidate
        bpy.ops.object.mode_set(mode='EDIT')
        for i,(name,*_) in enumerate(records):
            e = candidate.data.edit_bones[name]
            h,t,z = [inv@Vector(p) for p in transformed[i*3:i*3+3]]
            e.head,e.tail = h,t
            e.align_roll(z-h)
        bpy.ops.object.mode_set(mode='OBJECT')
        if set(candidate.data.bones.keys()) != set(source.data.bones.keys()):
            raise ValueError('拟合未保留完整模板骨集合')
        old_candidate = scene.objects.get(state.get('fitted_reference','')) if state.get('fitted_reference') else None
        if old_candidate and old_candidate.get(OWNER_KEY)==state['owner']:
            old_candidate.hide_set(True)
        candidate['c94_reference_kind'] = 'fitted_candidate_pending_review'
        primary = {endpoint[0] for m in state['markers'] for endpoint in m['endpoints']}
        for collection in candidate.data.collections_all:
            collection.is_visible = True
        for pb in candidate.pose.bones:
            pb.hide = pb.name not in primary
        candidate.data.display_type = 'STICK'
        state['fit_revision'] = revision
        state['fitted_reference'],state['fit_stale'] = candidate.name,False
        from . import editing
        editing.accept_fit(scene,state,candidate)
        if previous_edit:
            state['editing']['previous'] = previous_edit
        report = {'bones':len(candidate.data.bones),'marker_residual':warp.residual,
                  'reference':candidate.name,'status':'pending_visual_review',
                  'scope':'all rest bones, hierarchy, roll transport; no constraints, weights or generation'}
        state['fit_report'] = report
        _save(scene,state)
        return report
    except Exception:
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if candidate:
            data = candidate.data
            bpy.data.objects.remove(candidate,do_unlink=True)
            if data.users==0:
                bpy.data.armatures.remove(data)
        if rollback_state.get('fitted_reference'):
            old = scene.objects.get(rollback_state['fitted_reference'])
            if old:
                old.hide_set(False)
        _write_markers(scene,rollback_state)
        _save(scene,rollback_state)
        raise
    finally:
        for o in bpy.context.selected_objects:
            o.select_set(False)
        for o in old_selection:
            if o.name in bpy.context.view_layer.objects:
                o.select_set(True)
        bpy.context.view_layer.objects.active = old_active
