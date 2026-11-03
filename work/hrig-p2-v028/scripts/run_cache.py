"""Owned selected native Cloth bake, saved candidate and cached cold consumers."""
import importlib.util
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
import bpy

ROOT = Path(__file__).resolve().parents[1]
OBSERVER = ROOT.parents[1]/'tools/blenderctl'


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def require(ok,message):
    if not ok: raise ValueError(message)


def read_document(support,item,*,large=False):
    path=support.checked(item)
    limit=(128 if large else 32)*1024*1024
    require(path.stat().st_size<=limit,'Cache JSON exceeds explicit '+str(limit//1048576)+' MiB limit')
    return support.read(item)


def inspect_native(core,contract,targets,baked=False):
    if baked:
        observed,handles=core._capture(bpy.context,contract.canonical_targets(targets),allow_baked=True)
        for obj,modifier in handles: core._assert_complete(modifier)
        return observed
    observed=core.inspect_cache(bpy.context,targets)
    require(observed.get('ok') is True,'Native embedded cache inspection refused: '+str(observed))
    return observed


def global_storage_guard():
    # Frame changes may also evaluate dependencies outside the selected bakes.
    # Refuse native storage that could write/read unmanaged disk cache files.
    for obj in bpy.context.scene.objects:
        for modifier in obj.modifiers:
            if modifier.type=='CLOTH' and modifier.show_viewport:
                cache=modifier.point_cache
                require(not cache.use_external and not cache.use_disk_cache and not cache.is_baking,
                        'Active Scene Cloth has external/disk/baking cache: '+obj.name)


def sample_now(rig,picked,clip,sampler,modes):
    bpy.context.view_layer.update()
    graph=bpy.context.evaluated_depsgraph_get(); ev=graph.id_eval_get(rig)
    require(ev.is_evaluated,'Native evaluated rig required for cached witness')
    validation=sampler._clip_inputs(rig,clip,bpy.context.scene.frame_current)
    require(validation['ok'],'Native Action component evaluation differs: '+str(validation))
    row={'frame':bpy.context.scene.frame_current,'meshes':{},'bones':{},'proxies':{},'clip_input_validation':validation}
    for name in picked['meshes']: row['meshes'][name]=sampler._mesh(bpy.data.objects[name],graph)
    for name in picked['proxy_objects']: row['proxies'][name]=sampler._mesh(bpy.data.objects[name],graph,local=True)
    for name in picked['bones']:
        original,pb=rig.pose.bones[name],ev.pose.bones[name]
        require(original.rotation_mode in modes,'Unsupported witness bone rotation mode')
        field='rotation_quaternion' if original.rotation_mode=='QUATERNION' else 'rotation_euler'
        row['bones'][name]={'rotation_mode':original.rotation_mode,'native_active_values':sampler._plain_numeric(getattr(original,field)),
            'world_matrix':sampler._plain_numeric(ev.matrix_world@pb.matrix),'matrix':sampler._plain_numeric(pb.matrix),
            'matrix_basis':sampler._plain_numeric(pb.matrix_basis),'head':sampler._plain_numeric(ev.matrix_world@pb.head),
            'tail':sampler._plain_numeric(ev.matrix_world@pb.tail)}
    return row


def counts(sample):
    result={('mesh',name):item['vertices'] for name,item in sample['meshes'].items()}
    for name,item in sample['proxies'].items():
        require(item['world']['vertices']==item['local']['vertices'],'Proxy local/world counts differ')
        result['proxy',name,'local']=item['local']['vertices']; result['proxy',name,'world']=item['world']['vertices']
    return result


def collect(frames,rig,picked,clip,sampler,modes,state,targets,expected_counts):
    require(sum(expected_counts.values())*len(frames)<=4000000,'Complete cached-coordinate point budget exceeded before sampling')
    rows=[]
    try:
        for frame in frames:
            bpy.context.scene.frame_set(frame,subframe=0)
            # Complete native flags alone are insufficient; caller must compare
            # every resulting coordinate to its producer same-frame witness.
            state.baked(targets)
            row=sample_now(rig,picked,clip,sampler,modes)
            require(counts(row)==expected_counts,'Cached evaluated point coverage/count changed')
            rows.append(row)
    finally:
        bpy.context.scene.frame_set(0,subframe=0); bpy.context.view_layer.update()
    return rows


def checked_inputs(config,support,frontend,contract):
    mode=config['mode']; allowed={'inspect':{'input','selection'},'bake':{'input','operation','clip_request','witness'},
        'cold':{'input','operation','clip_request','witness','producer_report'},
        'replay':{'input','operation','clip_request','witness','producer_report','source_bundle'}}
    require(mode in allowed and set(config)=={'mode','implementation','addon_root','observer_root'}|allowed[mode],'Exact cache worker mode/fields required')
    op=frontend.operation(read_document(support,config['operation'])) if 'operation' in config else None
    if op:
        request=contract.canonical_request(op['request']); context=op['context']; rig_name=op['rig']; targets=request['targets']
    else:
        selection=frontend.selection(read_document(support,config['selection'])); request=None
        context=selection['context']; rig_name=selection['rig']; targets=contract.canonical_targets(selection['targets'])
    return mode,op,request,context,rig_name,targets


def run(config,job):
    require(tuple(bpy.app.version)==(5,2,1) and bpy.app.build_hash.decode()=='9e2066aef7ef'
            and not bpy.context.preferences.filepaths.use_scripts_auto_execute,'Verified Blender5.2.1 build and embedded autoexec off required')
    require(Path(config['addon_root']).resolve()==ROOT/'addon' and Path(config['observer_root']).resolve()==OBSERVER,'Pinned cache roots differ')
    sys.path.insert(0,str(OBSERVER)); sys.path.insert(0,str(ROOT/'scripts')); sys.path.insert(0,str(ROOT/'addon'))
    import cache_cli as frontend
    import inspection
    require(Path(frontend.__file__).resolve()==ROOT/'scripts/cache_cli.py'
            and Path(frontend.host.__file__).resolve()==ROOT/'scripts/rigctl.py'
            and Path(frontend.motion.__file__).resolve()==ROOT/'scripts/motion_cli.py'
            and Path(inspection.__file__).resolve()==OBSERVER/'inspection.py','Imported cache helper/observer identity differs')
    from humanoid_rigging_probe import cloth_cache as core,cache_contract as contract,motion_clips,motion_contract,persistence
    support=load('_hrig_cache_support',ROOT/'scripts/run_weights.py')
    sampler=load('_hrig_cache_sampler',ROOT/'scripts/run_motion.py')
    state=load('_hrig_cache_state',ROOT/'scripts/cache_state.py')
    expected=set((ROOT/'addon/humanoid_rigging_probe').glob('*.py'))|{ROOT/'scripts'/name for name in frontend.HELPERS}|{OBSERVER/'inspection.py',OBSERVER/'protocol.py'}
    files=[support.checked(item) for item in config['implementation']]
    require(len(files)==len(set(files)) and set(files)==expected,'Exact cache implementation closure required')
    mode,op,request,ctx,rig_name,targets=checked_inputs(config,support,frontend,contract)
    report={'schema':'hrig.cache.report.v1','mode':mode,'entry_sha':support.sha(__file__),'implementation':config['implementation'],
            'input':config['input'],'source_saved':False,'GUI':'deferred_by_user','FPS':'not_measured','complete_improved_Aon':False,
            'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode()}
    producer=None
    if mode in ('cold','replay'):
        producer=read_document(support,config['producer_report'],large=True)
        require(producer.get('schema')==report['schema'] and producer.get('status')=='candidate_waiting_independent_cold'
                and producer.get('operation_ok') is True and producer['candidate']==config['input']
                and producer['entry_sha']==report['entry_sha'] and producer['implementation']==config['implementation']
                and producer['operation']==op and producer['clip_request_descriptor']==config['clip_request'],
                'Cache producer/candidate/operation/implementation conflict')
        report['producer_report']=config['producer_report']
    bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])),load_ui=False,use_scripts=False)
    support.select_context(frontend.motion.motion_context(ctx),frames=True)
    require(bpy.data.objects.get(rig_name) is not None and bpy.data.objects[rig_name].type=='ARMATURE','Explicit cache rig identity required')
    rig=bpy.data.objects[rig_name]
    global_storage_guard()
    observed=inspect_native(core,contract,targets,baked=producer is not None)
    before=state.capture(targets); before_summary=state.summarize(before); extra=support.runtime_extra_users(); external=support.paths()
    if op: report['external_input_validation']=support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],external)
    report.update(rig=rig_name,context=ctx,targets=targets,before_snapshot=before_summary,cache_authoring=observed['snapshot'],
                  derived_cache_initial=state.derived(targets),external_paths_before=external)
    if mode=='inspect':
        require(state.capture(targets)==before,'Cache read-only inspection changed authoring')
        report.update(status='pass',inspection=observed,source_unchanged=True,after_snapshot=before_summary)
    else:
        picked=frontend.witness(read_document(support,config['witness'])); clip=motion_contract.canonical_request(read_document(support,config['clip_request']))
        require(picked['context']==ctx,'Cache witness context differs from explicit frame0')
        require(set(track['bone'] for track in clip['tracks'])<=set(picked['bones']),'Witness must include every Action input bone')
        require(set(target['object'] for target in targets)<=set(picked['proxy_objects']),'Witness must include every selected cache proxy')
        for name in picked['meshes']+picked['proxy_objects']:
            require(bpy.context.scene.objects.get(name) is not None and bpy.data.objects[name].type=='MESH','Explicit witness Scene Mesh missing: '+name)
        require(all(rig.pose.bones.get(name) is not None for name in picked['bones']),'Explicit cache witness bone missing')
        native_range=observed['range']; start,end=native_range['frame_start'],native_range['frame_end']
        require(all(start<=frame<=end for frame in picked['frames']),'Cache witness outside retained native range')
        verified=motion_clips.verify_clip(bpy.context,rig,clip); require(verified['ok'],'Exact existing native Action/slot/curve verification failed: '+str(verified))
        report.update(operation=op,operation_descriptor=config['operation'],request=request,witness=picked,witness_descriptor=config['witness'],
                      clip_request=clip,clip_request_descriptor=config['clip_request'],native_cache_range=native_range)
        if mode=='bake':
            require(picked['frames'][0]==start and picked['frames'][-1]==end,'Bake witnesses must include original cache start/end')
            # Inspect evaluated counts at frame0 before native cache mutation.
            initial=sample_now(rig,picked,clip,sampler,motion_contract.MODES); expected_counts=counts(initial)
            require(sum(expected_counts.values())*max(len(picked['frames']),len(frontend.cold_frames(picked['frames'])))<=4000000,
                    'Producer/cold full-coordinate budget exceeded before native bake')
            require(state.capture(targets)==before,'Count preflight changed original authoring')
            answer=core.bake_cache(bpy.context,request); report['result']=answer
            if not answer.get('ok'):
                report.update(status='discard_required' if answer.get('discard_required') else 'rejected',operation_ok=False,
                              discard_required=answer.get('discard_required',False),source_saved=False)
                support.dump(job/'output-cache.json',report)
                raise ValueError('Native cache bake refused/failed; owned process discarded: '+str(answer))
            state.baked(targets)
            after=state.capture(targets); require(after==before,'Native bake changed protected authoring fields')
            baked_observed=inspect_native(core,contract,targets,baked=True)
            require(baked_observed['snapshot']==observed['snapshot'],'Cache native authoring snapshot changed')
            samples=collect(picked['frames'],rig,picked,clip,sampler,motion_contract.MODES,state,targets,expected_counts)
            require(state.capture(targets)==before,'Producer cached sampling changed authoring')
            clip_after=motion_clips.verify_clip(bpy.context,rig,clip)
            require(clip_after['ok'] and clip_after['state']==verified['state'],'Bake changed original Action')
            report.update(samples=samples,complete_coordinate_points=sum(expected_counts.values())*len(samples),
                          after_snapshot=before_summary,clip_verification=clip_after,authoring_preserved=True)
            frontend.motion.check_replay_samples(report,picked,clip)
            require(support.paths()==external,'Bake changed external files')
            for item in config['implementation']: support.checked(item)
            for key in ('input','operation','clip_request','witness'): support.checked(config[key])
            if op: support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],external)
            writer=persistence.save_candidate(job/'output-candidate.blend',job)
            require(state.capture(targets)==before and support.runtime_extra_users()==extra,'Writer changed original authoring/user accounting')
            report.update(status='candidate_waiting_independent_cold',operation_ok=True,discard_required=False,
                          writer=writer,candidate={key:writer[key] for key in ('file','expected_sha256')},source_unchanged=True,
                          derived_cache_final=state.baked(targets),external_paths_after=support.paths())
        else:
            require(before_summary==producer['after_snapshot'] and observed['snapshot']==producer['cache_authoring'],
                    'Cold cache authoring identity differs from producer')
            require(verified['state']==producer['clip_verification']['state'] and external==producer['external_paths_after'],
                    'Cold original Action or native external inputs differ')
            original=producer['witness']
            require(all(picked[key]==original[key] for key in ('context','meshes','bones','proxy_objects'))
                    and set(picked['frames'])<=set(original['frames']),'Cached consumers require identical coverage and producer-observed frames')
            if mode=='cold': require(picked==original,'Cold witness must be exactly the producer witness')
            else:
                bundle=read_document(support,config['source_bundle'],large=True)
                require(bundle.get('schema')=='hrig.cache.bundle.v1' and bundle.get('status')=='succeeded'
                        and bundle['candidate']==config['input'] and bundle['report']==config['producer_report']
                        and bundle['verification'].get('status')=='pass','Replay requires immutable independently cold-accepted cache bundle')
                report['source_bundle']=config['source_bundle']
            frames=frontend.cold_frames(picked['frames']) if mode=='cold' else picked['frames']
            expected_counts=counts(producer['samples'][0])
            require(sum(expected_counts.values())*len(frames)<=4000000,'Cold complete coordinate budget exceeded before first jump')
            samples=collect(frames,rig,picked,clip,sampler,motion_contract.MODES,state,targets,expected_counts)
            require(state.capture(targets)==before,'Cached consumer changed original authoring after frame0 restore')
            final_clip=motion_clips.verify_clip(bpy.context,rig,clip)
            require(final_clip['ok'] and final_clip['state']==verified['state'],'Cached consumer changed original Action')
            report.update(samples=samples,complete_coordinate_points=sum(expected_counts.values())*len(samples))
            comparison=frontend.compare_samples(producer,report,picked,clip,frames)
            report.update(status='pass',operation_ok=True,source_saved=False,source_unchanged=True,context_restored=True,
                          after_snapshot=before_summary,clip_verification=final_clip,sample_comparison=comparison,
                          derived_cache_final=state.baked(targets),cache_identity_scope='candidate SHA plus explicit producer-matched full-coordinate cached frames')
    for item in config['implementation']: support.checked(item)
    for key in ('input','operation','selection','clip_request','witness','producer_report','source_bundle'):
        if key in config: support.checked(config[key])
    require(support.paths()==external,'Cached operation changed native external inputs')
    if op: support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],support.paths())
    require(support.sha(__file__)==report['entry_sha'],'Cache entry changed')
    report['scope']='embedded native cache and explicit same-frame coordinate identity only; unsampled frames/GUI/FPS/collision quality not certified'
    support.dump(job/'output-cache.json',report)
    return {'status':report['status'],'mode':mode,'operation_ok':report.get('operation_ok'),'source_unchanged':True,
            'source_saved':report.get('source_saved'),'discard_required':False}


if __name__=='__main__':
    try: result=run(parameters,Path(job_directory))
    except Exception:
        import traceback
        owned=Path(job_directory)
        (owned/'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8')
        failure=owned/'output-cache.json'
        if not failure.exists():
            failure.write_text(json.dumps({'schema':'hrig.cache.report.v1','mode':parameters.get('mode'),
                'status':'discard_required','discard_required':True,'source_saved':False,
                'candidate_quarantined':(owned/'output-candidate.blend').exists(),
                'recovery_scope':'discard this owned worker; no cache rollback and no succeeded bundle'},indent=2),encoding='utf-8')
        raise
