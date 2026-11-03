"""v028 fixed manual-action study; saved candidates are separate from source."""
import sys,json,hashlib,math,base64,zlib,traceback
from pathlib import Path
from array import array
sys.dont_write_bytecode=True
def run(c,directory):
    import bpy
    resources=[c['input'],*c['implementation'],*c['external_inputs']]
    if c.get('producer'):resources.append(c['producer'])
    def verify():
        for d in resources:assert hashlib.sha256(Path(d['file']).read_bytes()).hexdigest()==d['expected_sha256'],d['file']
    verify()
    for n in ('character_state.py','inspection.py'):
        ds=[d for d in c['implementation'] if Path(d['file']).name==n];assert len(ds)==1
        sys.path.insert(0,str(Path(ds[0]['file']).parent))
    sys.path.insert(0,c['addon_root'])
    import character_state as C
    from humanoid_rigging_probe import hair_pair_isolation as HP,hair_actions as HA,motion_clips as M,subdivision_diagnostic as S
    from mathutils import Quaternion
    assert bpy.app.version==(5,2,1) and bpy.app.build_hash.decode()=='9e2066aef7ef'
    assert not bpy.context.preferences.filepaths.use_scripts_auto_execute
    bpy.ops.wm.open_mainfile(filepath=c['input']['file'],use_scripts=False,load_ui=False)
    scene=bpy.data.scenes['Scene'];layer=scene.view_layers['ViewLayer']
    bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
    assert scene.frame_current==0 and scene.frame_subframe==0 and bpy.context.mode=='OBJECT'
    S._handler_policy(bpy);S._cache_policy(scene,list(range(1,49)))
    rig=bpy.data.objects['千早爱音-素体骨架']
    before=C.capture();before_digest=C.digest(before)
    original_action=rig.animation_data.action
    assert original_action.name=='HRIG_Aon_head_turn_stop_v008'
    original_action_state=M.clip_state(original_action)
    old_slot=rig.animation_data.action_slot
    names={'front':'HRIG_Aon_front_manual_v028','rear':'HRIG_Aon_rear_manual_v028'}
    specs={'front':('前发-02',[0,2,-2,2,-2,0,0]),
           'rear':('后发-02',[0,-2,2,-2,2,0,0])}
    frames=[1,8,16,24,32,40,48]
    original_q={p:list(rig.pose.bones[n].rotation_quaternion) for p,(n,_) in specs.items()}
    def requests(owners):
        obs=HA.inspect(bpy.context,names)
        out={}
        for part,(bone,degrees) in specs.items():
            q=owners[part].pose.bones[bone].rotation_quaternion.copy()
            keys=[{'frame':f,'value':list(q@Quaternion((1,0,0),math.radians(v)))} for f,v in zip(frames,degrees)]
            keys[0]['value']=list(q);keys[-1]['value']=list(q)
            out[part]={'schema':'hrig.motion-clip.create.v1','action_name':names[part],
                       'expected_snapshot_sha256':obs[part]['snapshot_sha256'],
                       'tracks':[{'bone':bone,'rotation_mode':'QUATERNION','before':list(q),'keys':keys}]}
        return out
    def paths():
        rows=[]
        for collection in ('images','libraries','sounds','fonts','movieclips','cache_files','volumes'):
            for item in getattr(bpy.data,collection):
                raw=item.filepath;lib=None if collection=='libraries' else item.library
                val=C.inspection.path_value(raw,lib) if raw and raw!='<builtin>' else raw
                rows.append({'collection':collection,'name':item.name,'raw_path':raw,'resolved_path':val,
                    'packed':bool(getattr(item,'packed_file',None)) or bool(getattr(item,'packed_files',()))})
        for item in C.inspection.all_ids():
            weak=getattr(item,'library_weak_reference',None)
            if weak:
                raw=weak.filepath
                rows.append({'collection':'weak_library_reference','name':item.bl_rna.identifier+':'+item.name,
                             'raw_path':raw,'resolved_path':C.inspection.path_value(raw,item.library) if raw else raw,'id_name':weak.id_name})
        return sorted(rows,key=lambda x:(x['collection'],x['name']))
    def resolved(rows):return [{k:v for k,v in row.items() if k!='raw_path'} for row in rows]
    initial_paths=paths()
    report={'schema':'hrig.hair-manual-study.v1','phase':c['phase'],'status':'running',
            'input':c['input'],'source_saved':False,'cache_scope':'fresh sequential memory; not baked/collision acceptance',
            'before_digest':before_digest,'samples':[]}
    geom={'schema':'hrig.hair-isolation-geometry.v1','states':[]}
    owners=None;reqs=None;backup=None;reference=None
    if c['phase']=='reference':
        assert before_digest=='4e744bb99b3e2706656dc183565d7d8839225762504bf464704508fdef3f14d7'
        reference=original_action.copy();reference.name='HRIG_Aon_reference_manual_v028'
        assert len(reference.slots)==1 and len(reference.layers)==1
        bag=reference.layers[0].strips[0].channelbag(reference.slots[0])
        assert len(bag.fcurves)==4
        # Preserve exact head curves in the copied Action; append only eight explicit curves.
        for part,(bone,degrees) in specs.items():
            q=rig.pose.bones[bone].rotation_quaternion.copy()
            keys=[list(q@Quaternion((1,0,0),math.radians(v))) for v in degrees];keys[0]=list(q);keys[-1]=list(q)
            path=rig.pose.bones[bone].path_from_id('rotation_quaternion')
            for index in range(4):
                fc=bag.fcurves.new(data_path=path,index=index);fc.extrapolation='CONSTANT'
                fc.keyframe_points.add(len(frames))
                fc.keyframe_points.foreach_set('co',[v for f,qv in zip(frames,keys) for v in (f,qv[index])])
                for point in fc.keyframe_points:point.interpolation='LINEAR'
                fc.update()
        rig.animation_data.action=reference;rig.animation_data.action_slot=reference.slots[0]
        owners={'front':rig,'rear':rig}
        # Exact native head curves, including handles/modifiers, are unchanged.
        old=M.clip_state(original_action)['layers'][0]['strips'][0]['bags'][0]['curves']
        new=M.clip_state(reference)['layers'][0]['strips'][0]['bags'][0]['curves'][:4]
        assert old==new
        report['reference_head_curves_exact']=True
    elif c['phase']=='producer':
        owners=HA.resolve(bpy.context)
        reqs=requests(owners)
        backup={p:{'raw':M._raw_ad(o.animation_data),'state':M.animation_state(o),
                   'rotations':[(specs[p][0],'QUATERNION','rotation_quaternion',list(o.pose.bones[specs[p][0]].rotation_quaternion))]}
                for p,o in owners.items()}
        # Inject after a fully created/bound front Action, not before any mutation.
        def fault(stage,part):
            if part=='front':raise RuntimeError('v028 injected after front Action binding')
        HA._mutation_seam=fault
        rejected=HA.create(bpy.context,reqs)
        HA._mutation_seam=lambda stage,part:None
        assert not rejected['ok'] and rejected['rollback']['ok'] and rejected['rollback']['begun']
        assert C.capture()==before and C.digest(C.capture())==before_digest
        report['pair_failure_after_front_bound_restored_exact']=rejected
        created=HA.create(bpy.context,reqs)
        assert created['ok'],created
        report['creation']=created
        after=C.capture()
        conflict=HA.create(bpy.context,reqs)
        assert not conflict['ok'] and not conflict['rollback']['begun'] and C.capture()==after
        report['existing_action_refused_without_mutation']=True
    elif c['phase']=='cold':
        producer=json.loads(Path(c['producer']['file']).read_text(encoding='utf8'))
        assert producer['status']=='pass'
        owners=HA.resolve(bpy.context);reqs=producer['requests'];backup=producer['backups']
        assert before_digest==producer['saved_author_digest']
        assert resolved(initial_paths)==resolved(producer['source_paths'])
        for p in owners:M._verify(bpy.context,owners[p],reqs[p])
        assert {p:M.clip_state(o.animation_data.action) for p,o in owners.items()}==producer['action_states']
    else:raise ValueError('phase')
    def observe(label):
        graph=bpy.context.evaluated_depsgraph_get()
        out={'id':str(label),'objects':[],'front_bones':{},'keys':{k.name:[float(b.value) for b in k.key_blocks] for k in bpy.data.shape_keys}}
        for name in ['千早爱音- 赋权后发网格','千早爱音- 赋权前发网格','千早爱音- 赋权躯干网格',
                     '千早爱音- 赋权上睫毛网格','千早爱音- 赋权下睫毛网格','千早爱音-前发物理','千早爱音-后发物理']:
            obj=bpy.data.objects[name];e=obj.evaluated_get(graph)
            assert e.is_evaluated and e.original==obj
            co=array('f',[0.])*(3*len(e.data.vertices));e.data.vertices.foreach_get('co',co)
            assert all(math.isfinite(v) for v in co)
            raw=co.tobytes()
            out['objects'].append({'object':name,'vertices':len(e.data.vertices),'local_f32_sha256':hashlib.sha256(raw).hexdigest(),
                'local_f32_zlib_base64':base64.b64encode(zlib.compress(raw)).decode(),
                'topology_sha256':C.digest([list(p.vertices) for p in e.data.polygons]),'matrix_world':[list(r) for r in e.matrix_world]})
        for part,owner in owners.items():
            e=owner.evaluated_get(graph)
            out['front_bones'].update({part+':'+n:[list(r) for r in e.matrix_world@e.pose.bones[n].matrix] for n in sorted(HP.bone_names(part))})
        qrows={}
        for p,(bone,_) in specs.items():
            owner=owners[p];e=owner.evaluated_get(graph);raw=list(owner.pose.bones[bone].rotation_quaternion)
            ev=list(e.pose.bones[bone].rotation_quaternion)
            assert max(abs(a-b) for a,b in zip(raw,ev))<1e-7
            slot=owner.animation_data.action_slot;action=owner.animation_data.action
            bag=action.layers[0].strips[0].channelbag(slot)
            curves=[f for f in bag.fcurves if f.data_path==owner.pose.bones[bone].path_from_id('rotation_quaternion')]
            assert len(curves)==4
            expected={f.array_index:f.evaluate(scene.frame_current) for f in curves}
            assert max(abs(raw[i]-expected[i]) for i in range(4))<1e-7
            qrows[p]={'raw':raw,'evaluated':ev,'fcurve':expected,'object':owner.name,'action':action.name,'slot':slot.identifier}
        return out,qrows
    layer.update()
    state,qrows=observe('neutral');geom['states'].append(state)
    report['samples'].append({'frame':0,'controls':qrows})
    for frame in range(1,49):
        scene.frame_set(frame);layer.update()
        state,qrows=observe(frame);geom['states'].append(state);report['samples'].append({'frame':frame,'controls':qrows})
    scene.frame_set(0);layer.update()
    # The original head Action's authoring content remains exact; user count stays one in C.
    if c['phase']=='reference':
        rig.animation_data.action=original_action;rig.animation_data.action_slot=old_slot
        for p,(bone,_) in specs.items():rig.pose.bones[bone].rotation_quaternion=original_q[p]
        bpy.data.actions.remove(reference)
        layer.update()
        assert C.capture()==before and C.digest(C.capture())==before_digest
        report['restored_digest']=before_digest
    else:
        for p in owners:M._verify(bpy.context,owners[p],reqs[p])
        assert M.clip_state(original_action)==original_action_state
        report['requests']=reqs;report['backups']=backup
        report['source_paths']=initial_paths
        report['action_states']={p:M.clip_state(o.animation_data.action) for p,o in owners.items()}
        report['saved_author_digest']=C.digest(C.capture())
        if c['phase']=='producer':
            output=str(Path(directory,'output-candidate.blend'))
            bpy.ops.wm.save_as_mainfile(filepath=output,check_existing=False,relative_remap=False)
            assert resolved(paths())==resolved(initial_paths)
            assert C.digest(C.capture())==report['saved_author_digest']
            report['candidate']={'file':output,'expected_sha256':hashlib.sha256(Path(output).read_bytes()).hexdigest()}
        for p in reversed(('front','rear')):
            o=owners[p];b=backup[p]
            restored=M._restore(o,o.animation_data.action,b['raw'],b['state'],b['rotations'])
            assert restored['ok'],restored
        layer.update()
        baseline=before_digest if c['phase']=='producer' else producer['before_digest']
        assert C.digest(C.capture())==baseline
        report['restored_digest']=baseline
        if c['phase']=='producer':assert C.capture()==before
    verify()
    report.update(status='pass',protection_exact=True,source_unchanged=True,frame_restored=scene.frame_current==0)
    mods=[]
    for name,module in tuple(sys.modules.items()):
        if name in ('character_state','inspection','protocol') or name=='humanoid_rigging_probe' or name.startswith('humanoid_rigging_probe.'):
            actual=Path(module.__file__).resolve()
            match=[d for d in c['implementation'] if Path(d['file']).resolve()==actual]
            assert len(match)==1,(name,str(actual))
            mods.append({'module':name,**match[0]})
    report['loaded_implementation']=mods
    for name,value in [('output-validation.json',report),('output-geometry.json',geom)]:
        raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')
        assert len(raw)<=67108864
        Path(directory,name).write_bytes(raw)
    return {'status':'pass'}
if __name__=='__main__':
    try:result=run(parameters,job_directory)
    except Exception:
        Path(job_directory,'preflight-error.txt').write_text(traceback.format_exc(),encoding='utf8');raise
