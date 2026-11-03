"""Bounded native ID timing observer; no .blend save or rig intervention."""
import hashlib,json,sys,time
from pathlib import Path
sys.dont_write_bytecode=True
import bpy

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def verify(d):
    if type(d) is not dict or set(d)!={'file','expected_sha256'}:raise ValueError('Invalid descriptor')
    p=Path(d['file'])
    if not p.is_absolute() or not p.is_file() or digest(p)!=d['expected_sha256']:raise ValueError('Pinned resource changed')
    return p
def dump(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
def run(c,directory):
    if set(c)!={'input','plan','implementation','external_inputs','addon_root'}:raise ValueError('Unknown worker fields')
    if tuple(bpy.app.version)!=(5,2,1) or bpy.app.build_hash.decode()!='9e2066aef7ef':raise ValueError('Unreviewed Blender build')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:raise ValueError('Embedded scripts must stay disabled')
    for d in [c['input'],c['plan'],*c['implementation'],*c['external_inputs']]:verify(d)
    sys.path.insert(0,str(Path(__file__).resolve().parent));sys.path.insert(0,c['addon_root'])
    import host_contract as H
    import character_state as C
    import inspection
    from humanoid_rigging_probe import id_cost as I, diagnostics as D, subdivision_diagnostic as S
    p=Path(c['plan']['file'])
    if p.stat().st_size>1048576:raise ValueError('Plan exceeds 1 MiB')
    plan=I.validate_plan(H.parse_json(p.read_text(encoding='utf-8-sig')))
    bpy.ops.wm.open_mainfile(filepath=c['input']['file'],load_ui=False,use_scripts=False)
    ctx=plan['observation']['context'];scene=bpy.data.scenes[ctx['scene']]
    bpy.context.window.scene=scene;bpy.context.window.view_layer=scene.view_layers[ctx['view_layer']]
    scene,layer,objects,bones=D._resolve(bpy.context,plan['observation'])
    S._handler_policy(bpy);S._cache_policy(scene,plan['observation']['frames'])
    if not hasattr(bpy.app,'debug_depsgraph_time'):raise ValueError('Native timing toggle unavailable')
    original_debug=bpy.app.debug_depsgraph_time
    if type(original_debug) is not bool or original_debug is not False:raise ValueError('Expected native debug timer initially false')
    original_frame,original_subframe=scene.frame_current,scene.frame_subframe
    before=C.capture();before_sha=C.digest(before)
    report={'schema':'hrig.id-cost-report.v1','status':'running','plan':plan,'source_saved':False,
       'authoring_properties_edited':False,'FPS_claim':False,'blender':{'version':bpy.app.version_string,'build':bpy.app.build_hash.decode()},
       'samples':[],'error':None,'storage_protection':{'before_sha256':before_sha,'exact_equal':False},
       'restore':{'success':False,'original_frame':original_frame,'original_subframe':original_subframe},
       'debug':{'initial':original_debug,'requested':plan['profile'],'restored':False},
       'memory_cache':{'before':D._cache_states(scene),'exact_cache_restoration_claim':False},
       'timing_scope':{'native_window':'frame_set+explicit update+acquire; not pure solver time',
         'stats_ns':'gnuplot text write/read/strict CSV parse only, outside native window',
         'capture_ns':'declared common geometry/bones and optional full capture, separate from stats',
         'ID_values':'At most 32 highest nonzero ID aggregates from callback elapsed timers; absent IDs are censored/possibly zero, not proof of no work. No-op and debug-off can retain stale values. Callback thread intervals overlap; not a critical path or additive wall clock.'},
       'total_full_vertices_captured':0,'total_full_vertices_requested':0}
    code={'Object':'OB','Mesh':'ME','Armature':'AR','Key':'KE','Scene':'SC','Collection':'GR','Action':'AC',
          'Material':'MA','World':'WO','Camera':'CA','Light':'LA','Curve':'CU','Lattice':'LT','Image':'IM','Text':'TX'}
    identities=[];object_metadata=[]
    for item in inspection.all_ids():
        ident=inspection.identity(item)
        prefix=code.get(ident['type'])
        if isinstance(item,bpy.types.NodeTree):prefix='NT'
        if prefix:
            if isinstance(item,bpy.types.Key):ident['owner']=inspection.identity(C.key_owner(item))
            identities.append({'label':f'[{prefix}] {item.name}','identity':ident})
        if isinstance(item,bpy.types.Object):
            in_layer=layer.objects.get(item.name)==item
            object_metadata.append({'identity':ident,'in_scene':scene.objects.get(item.name)==item,'in_view_layer':in_layer,
                'visible_get':item.visible_get(view_layer=layer) if in_layer else None,
                'hide_viewport':item.hide_viewport,'hide_render':item.hide_render,
                'pose_bones':len(item.pose.bones) if item.pose else 0,
                'modifiers':[{'name':m.name,'type':m.type,'show_viewport':m.show_viewport} for m in item.modifiers]})
    report['label_bindings']=identities;report['object_metadata']=object_metadata
    totals={'full_vertices_requested':0,'full_vertices_captured':0};frame=None
    try:
        bpy.app.debug_depsgraph_time=plan['profile']
        if bpy.app.debug_depsgraph_time is not plan['profile']:raise ValueError('Timing toggle did not take effect')
        for frame in plan['observation']['frames']:
            t0=time.perf_counter_ns();scene.frame_set(frame,subframe=0.0);t1=time.perf_counter_ns()
            layer.update();t2=time.perf_counter_ns();graph=bpy.context.evaluated_depsgraph_get();t3=time.perf_counter_ns()
            if bpy.context.scene!=scene or bpy.context.view_layer!=layer or scene.frame_current!=frame or scene.frame_subframe!=0.0:raise ValueError('Context/frame changed')
            if bpy.app.debug_depsgraph_time is not plan['profile']:raise ValueError('Native timing flag changed')
            rawdesc=None;rows=None
            if plan['profile']:
                path=Path(directory)/f'id-stats-{frame:+06d}.txt'
                if path.exists():raise ValueError('Stats output already exists')
                graph.debug_stats_gnuplot(filepath=str(path),output_filepath=str(Path(directory)/'not-rendered.png'))
                if path.stat().st_size>2*1024*1024:raise ValueError('Stats output exceeds budget')
                rows=I.parse_stats(path.read_text(encoding='utf-8'))
                if len(rows)>32:raise ValueError('Pinned native top-32 stats row bound changed')
                rawdesc={'file':str(path),'expected_sha256':digest(path),'bytes':path.stat().st_size}
            t4=time.perf_counter_ns()
            sample=D._capture(graph,objects,bones,plan['observation'],totals);t5=time.perf_counter_ns()
            sample.update(frame=frame,subframe=0.0,graph_mode=graph.mode,
                frame_set_ns=t1-t0,explicit_update_ns=t2-t1,acquire_graph_ns=t3-t2,stats_ns=t4-t3,capture_ns=t5-t4,
                native_debug_time=bpy.app.debug_depsgraph_time,id_stats=rows,raw_stats=rawdesc)
            report['samples'].append(sample);del graph
        report['status']='pass'
    except Exception as e:
        report['status']='fail';report['error']={'type':type(e).__name__,'code':getattr(e,'code','NATIVE_ID_PROFILE_ERROR'),'message':str(e),'requested_frame':frame}
    finally:
        restore_errors=[]
        try:
            bpy.app.debug_depsgraph_time=original_debug
            report['debug']['restored']=bpy.app.debug_depsgraph_time is original_debug
        except Exception as e:restore_errors.append({'phase':'debug','type':type(e).__name__,'message':str(e)})
        try:
            scene.frame_set(original_frame,subframe=original_subframe);layer.update()
            report['restore'].update(actual_frame=scene.frame_current,actual_subframe=scene.frame_subframe,
                frame_restored=scene.frame_current==original_frame and scene.frame_subframe==original_subframe)
        except Exception as e:restore_errors.append({'phase':'frame','type':type(e).__name__,'message':str(e)})
        try:
            after=C.capture();after_sha=C.digest(after)
            report['storage_protection'].update(after_sha256=after_sha,exact_equal=before==after and before_sha==after_sha)
            report['memory_cache']['after_restore']=D._cache_states(scene)
        except Exception as e:restore_errors.append({'phase':'storage','type':type(e).__name__,'message':str(e)})
        report['restore']['errors']=restore_errors
        report['restore']['success']=not restore_errors and report['debug']['restored'] and report['restore'].get('frame_restored') is True and report['storage_protection']['exact_equal']
        if report['restore']['success'] is not True:report['status']='fail'
        report['total_full_vertices_captured']=totals['full_vertices_captured'];report['total_full_vertices_requested']=totals['full_vertices_requested']
    for d in [c['input'],c['plan'],*c['implementation'],*c['external_inputs']]:verify(d)
    report.update(source=c['input'],source_unchanged=True,implementation_unchanged=True,external_inputs=c['external_inputs'],
        dependency_scope='explicit descriptors only; inherited weak/dynamic references not certified')
    dump(Path(directory)/'output-diagnostic.json',report)
    return {'status':report['status'],'source_unchanged':True,'implementation_unchanged':True,'FPS_claim':False}

if __name__=='__main__':
    try:result=run(parameters,job_directory)
    except Exception:
        import traceback
        Path(job_directory,'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
