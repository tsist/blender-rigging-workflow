"""Pinned Action derivation producer, strict orphan cold and sequential replay."""
import importlib.util
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import bpy

ROOT = Path(__file__).resolve().parents[1]
OBSERVER = ROOT.parents[1]/'tools/blenderctl'


def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def require(value, message):
    if not value: raise ValueError(message)


def read_document(support, item, large=False):
    path=support.checked(item)
    if large:
        import derivation_cli as frontend
        return frontend.motion.read_report(path)
    require(path.stat().st_size <= 32*1024*1024,'Derivation input JSON exceeds32MiB')
    return support.read(item)


def verify(core, frontend, rig, req, baseline, expected=None):
    answer=core.verify_derivation(bpy.context,rig,req,baseline)
    require(answer.get('ok') is True,'Native original/derived Action verification failed: '+str(answer))
    original = baseline['original_action'] if baseline.get('schema') == core.CAPTURE_SCHEMA else baseline
    from derivation_state import check_verification
    check_verification(req,frontend.canonical('combined_request',req),answer,original)
    if expected is not None:
        require(answer['state'] == expected['state'] and answer['original_action_after'] == expected['original_action_after']
                and answer['binding'] == expected['binding'],'Exact original/derived Action content or binding changed')
    return answer


def report_verification(report, req, snapshot, checked):
    report.update(request=req,combined_request=checked['combined_request'],original_snapshot=snapshot,
                  original_action_before=snapshot['original_action'],original_action_after=checked['original_action_after'],
                  derivation_verification=checked)


def replay(config,job,report,producer,op,rig,frontend,core,contract,state,support,sampler):
    bundle=read_document(support,config['source_bundle'],large=True)
    require(bundle.get('schema') == frontend.SCHEMA and bundle.get('status') == 'succeeded'
            and bundle.get('mode') == 'create' and bundle.get('candidate') == config['input']
            and bundle.get('report') == config['producer_report'] and bundle.get('implementation') == config['implementation']
            and bundle.get('identity',{}).get('entry',{}).get('expected_sha256') == report['entry_sha']
            and bundle.get('verification',{}).get('status') == 'pass'
            and bundle.get('combined_request') == producer['combined_request'],'Replay needs current independently accepted derivation bundle')
    picked=frontend.motion.witness(read_document(support,config['witness']))
    require(picked['context'] == op['context'],'Replay frame0 context differs')
    req=contract.canonical_request(op['request']); combined=contract.combined_request(req)
    require(producer['combined_request'] == combined,'Combined request differs from explicit derivation operation')
    require({track['bone'] for track in combined['tracks']} <= set(picked['bones']),'Observe every base and added input bone')
    for name in picked['meshes']+picked['proxy_objects']:
        require(bpy.context.scene.objects.get(name) is not None and bpy.data.objects[name].type == 'MESH','Witness Scene Mesh missing: '+name)
    require(all(rig.pose.bones.get(name) is not None for name in picked['bones']),'Replay bone missing')
    initial=state.capture()
    require(state.summarize(initial) == producer['after_snapshot'],'Replay saved authoring state differs')
    snapshot=producer['original_snapshot']
    initial_verification=verify(core,frontend,rig,req,snapshot,producer['derivation_verification'])
    require(initial_verification == producer['derivation_verification'],'Replay frame0 full derivation verification differs')
    external=support.paths()
    require(external == producer['external_paths_after'],'Replay external paths differ')
    support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],external)
    initial_cloth=sampler._all_cloth()
    for rows in initial_cloth.values():
        for row in rows:
            cache=row['cache']
            require(not any(cache.get(key) for key in ('is_baked','is_baking','use_disk_cache','use_external')),'Replay refuses baked/baking/disk/external cache')
            if row['show_viewport']:
                require(picked['frames'][0] == cache['frame_start'] and picked['frames'][-1] <= cache['frame_end'],'Replay starts at every active Cloth start and stays in range')
    require(all(sampler._cloth(bpy.data.objects[name]) for name in picked['proxy_objects']),'Proxy has no native Cloth')
    samples,checks,total,previous_counts=[],[],0,None
    sampling_error=None
    restore_error=None
    try:
        for frame in picked['frames']:
            bpy.context.scene.frame_set(frame,subframe=0); bpy.context.view_layer.update()
            # Clip-state baseline intentionally excludes all_pose_channels: the
            # new shoulders and their driven Key values may move during replay.
            current=verify(core,frontend,rig,req,producer['original_action_before'],initial_verification)
            checks.append({'frame':frame,'ok':True,'derived_state_sha256':contract.digest(current['state']),
                           'original_state_sha256':contract.digest(current['original_action_after'])})
            graph=bpy.context.evaluated_depsgraph_get(); evaluated=graph.id_eval_get(rig)
            require(evaluated.is_evaluated,'Replay rig has no real evaluated ID')
            inputs=sampler._clip_inputs(rig,combined,frame)
            require(inputs['ok'],'Native combined Action input differs: '+str(inputs))
            sample={'frame':frame,'meshes':{},'bones':{},'proxies':{},'clip_input_validation':inputs}
            for name in picked['meshes']: sample['meshes'][name]=sampler._mesh(bpy.data.objects[name],graph)
            for name in picked['proxy_objects']: sample['proxies'][name]=sampler._mesh(bpy.data.objects[name],graph,local=True)
            counts={('mesh',name):item['vertices'] for name,item in sample['meshes'].items()}
            counts.update({('proxy',name,space):item[space]['vertices'] for name,item in sample['proxies'].items() for space in ('local','world')})
            require(previous_counts is None or counts == previous_counts,'Evaluated counts changed; correspondence undefined')
            previous_counts=counts
            points=sum(counts.values()); require(points*len(picked['frames']) <= 4000000,'Complete coordinate budget exceeded; no hash-only downgrade')
            total+=points
            for name in picked['bones']:
                pb,original=evaluated.pose.bones[name],rig.pose.bones[name]
                require(original.rotation_mode in contract.MODES,'Unsupported native witness bone mode')
                field='rotation_quaternion' if original.rotation_mode == 'QUATERNION' else 'rotation_euler'
                sample['bones'][name]={'rotation_mode':original.rotation_mode,'native_active_values':sampler._plain_numeric(getattr(original,field)),
                    'world_matrix':sampler._plain_numeric(evaluated.matrix_world@pb.matrix),'matrix':sampler._plain_numeric(pb.matrix),
                    'matrix_basis':sampler._plain_numeric(pb.matrix_basis),'head':sampler._plain_numeric(evaluated.matrix_world@pb.head),
                    'tail':sampler._plain_numeric(evaluated.matrix_world@pb.tail)}
            samples.append(sample)
    except Exception as error:
        sampling_error=error
    finally:
        try:
            bpy.context.scene.frame_set(0,subframe=0); bpy.context.view_layer.update()
        except Exception as error:
            restore_error=error
    final=state.capture()
    final_verification=core.verify_derivation(bpy.context,rig,req,snapshot)
    restored=(restore_error is None and bpy.context.scene.frame_current == 0 and bpy.context.scene.frame_subframe == 0)
    storage_ok=final == initial
    action_ok=final_verification == initial_verification
    if sampling_error is not None or not restored or not storage_ok or not action_ok:
        report.update(status='replay_failed',operation_ok=False,source_saved=False,context_restored=restored,
            storage_restored=storage_ok,action_restored=action_ok,after_snapshot=state.summarize(final),
            changes=state.diff(initial,final),sampling_error=str(sampling_error) if sampling_error else None,
            restore_error=str(restore_error) if restore_error else None,derivation_verification=final_verification)
        support.dump(job/frontend.REPORT,report)
        raise ValueError('Replay sampling/restoration failed; no authoring mask applied: '+str(sampling_error or restore_error or report['changes']))
    require(support.paths() == external,'Replay external dependencies changed')
    support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],external)
    report_verification(report,req,snapshot,final_verification)
    report.update(status='pass',operation_ok=True,source_unchanged=True,source_saved=False,context_restored=True,
        source_bundle=config['source_bundle'],producer_report=config['producer_report'],witness=picked,witness_descriptor=config['witness'],
        after_snapshot=state.summarize(final),initial_cloth=initial_cloth,final_cloth=sampler._all_cloth(),
        samples=samples,derivation_checks=checks,complete_coordinate_points=total,
        external_paths_after=external,scope='fresh sequential complete selected coordinates; frame0 full authoring restored; no cache persistence/visual/FPS claim')
    frontend.motion.check_replay_samples(report,picked,combined)


def run(config,job):
    require(tuple(bpy.app.version) == (5,2,1) and bpy.app.build_hash.decode() == '9e2066aef7ef'
            and not bpy.context.preferences.filepaths.use_scripts_auto_execute,'Verified Blender5.2.1/build with autoexec off required')
    require(Path(config['addon_root']).resolve() == ROOT/'addon' and Path(config['observer_root']).resolve() == OBSERVER,'Pinned derivation roots differ')
    sys.path.insert(0,str(OBSERVER)); sys.path.insert(0,str(ROOT/'scripts')); sys.path.insert(0,str(ROOT/'addon'))
    import derivation_cli as frontend
    import derivation_state as state
    import inspection
    from humanoid_rigging_probe import motion_derivation as core, motion_derivation_contract as contract, persistence
    require(Path(frontend.__file__).resolve() == ROOT/'scripts/derivation_cli.py'
            and Path(state.__file__).resolve() == ROOT/'scripts/derivation_state.py'
            and Path(inspection.__file__).resolve() == OBSERVER/'inspection.py','Imported derivation helper identity differs')
    files=[Path(item['file']).resolve() for item in config['implementation']]
    require(len(files) == len(set(files)) and set(files) == set(frontend.implementation_paths()),'Exact derivation implementation closure required')
    support=load('_hrig_derivation_support',ROOT/'scripts/run_weights.py')
    evaluator=load('_hrig_derivation_evaluator',ROOT/'scripts/run_styles.py')
    sampler=load('_hrig_derivation_sampler',ROOT/'scripts/run_motion.py')
    for item in config['implementation']: support.checked(item)
    mode=config['mode']
    allowed={'inspect':{'input','selection'},'create':{'input','operation'},
             'cold':{'input','operation','producer_report'},'replay':{'input','operation','producer_report','witness','source_bundle'}}
    require(mode in allowed and set(config) == {'mode','implementation','addon_root','observer_root'}|allowed[mode],'Exact derivation worker mode/fields required')
    op=frontend.operation(read_document(support,config['operation'])) if 'operation' in config else None
    picked=frontend.selection(read_document(support,config['selection'])) if mode == 'inspect' else None
    ctx=op['context'] if op else picked['context']; rig_name=op['rig'] if op else picked['rig']
    req=contract.canonical_request(op['request']) if op else None
    producer=None
    report={'schema':frontend.REPORT_SCHEMA,'mode':mode,'input':config['input'],'entry_sha':support.sha(__file__),
        'implementation':config['implementation'],'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode(),
        'source_saved':False,'GUI':'deferred_by_user','FPS':'not_measured','complete_improved_Aon':False,'rig':rig_name,'context':ctx}
    if mode in ('cold','replay'):
        producer=read_document(support,config['producer_report'],large=True)
        require(producer.get('schema') == frontend.REPORT_SCHEMA and producer.get('mode') == 'create'
                and producer.get('status') == 'candidate_waiting_independent_cold' and producer.get('operation_ok') is True
                and producer.get('candidate') == config['input'] and producer.get('operation') == op
                and producer.get('operation_descriptor') == config['operation']
                and producer.get('entry_sha') == report['entry_sha'] and producer.get('implementation') == config['implementation'],
                'Original producer/candidate/operation/implementation differs')
        report['producer_report']=config['producer_report']
    bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])),load_ui=False,use_scripts=False)
    require(tuple(bpy.data.version[:2]) <= tuple(bpy.app.version[:2]),'Future source version rejected')
    support.select_context(frontend.motion.motion_context(ctx),frames=True)
    rig=bpy.data.objects.get(rig_name); require(rig is not None and rig.type == 'ARMATURE','Explicit Armature missing')
    external=support.paths()
    if op:
        report.update(operation=op,operation_descriptor=config['operation'],external_input_validation=support.validate_native_inputs(
            op['external_inputs'],op['inherited_missing_weak_references'],external))
    if mode == 'replay':
        replay(config,job,report,producer,op,rig,frontend,core,contract,state,support,sampler)
    elif mode == 'cold':
        actual=state.capture()
        require(state.summarize(actual) == producer['after_snapshot'],'Cold full character authoring differs')
        checked=verify(core,frontend,rig,req,producer['original_snapshot'],producer['derivation_verification'])
        require(checked == producer['derivation_verification'],'Cold complete original/new derivation state differs')
        observed=evaluator.evaluation(rig,support)
        comparison=evaluator.compare_evaluation(producer['evaluation_after'],observed)
        require(comparison.get('ok') is True and comparison.get('pose_exact') is True,'Cold native evaluation differs')
        require(state.capture() == actual and support.paths() == producer['external_paths_after'],'Cold observation changed authoring or external paths')
        report_verification(report,req,producer['original_snapshot'],checked)
        report.update(status='pass',operation_ok=True,source_unchanged=True,source_saved=False,context_restored=True,
            after_snapshot=state.summarize(actual),evaluation=observed,evaluation_comparison=comparison,
            external_paths_after=support.paths(),runtime_extra_users_observed=support.runtime_extra_users())
    else:
        before=state.capture(); runtime_before=support.runtime_extra_users()
        evaluated_before=evaluator.evaluation(rig,support)
        require(state.capture() == before,'Initial evaluation changed saved authoring; no mask applied')
        report.update(before_snapshot=state.summarize(before),evaluation_before=evaluated_before,external_paths_before=external)
        if mode == 'inspect':
            inspected=core.inspect_derivation(bpy.context,rig,picked['bones'],picked['action_name'],picked['base_clip'])
            require(inspected.get('ok') is True,'Native derivation inspect rejected: '+str(inspected))
            require(state.capture() == before,'Derivation inspect changed authoring')
            report.update(status='pass',selection=picked,selection_descriptor=config['selection'],inspection=inspected,
                source_unchanged=True,after_snapshot=state.summarize(before))
        else:
            snapshot=core.inspect_derivation(bpy.context,rig,[row['bone'] for row in req['tracks']],req['action_name'],req['base_clip'])
            require(snapshot.get('ok') is True and snapshot['snapshot_sha256'] == req['expected_snapshot_sha256'],'Explicit derivation snapshot changed')
            snapshot=snapshot['snapshot']
            answer=core.derive_clip(bpy.context,rig,req)
            report.update(result=answer,operation_ok=answer.get('ok') is True,request=req,original_snapshot=snapshot)
            if not answer.get('ok'):
                after=state.capture()
                report.update(status='rejected',source_unchanged=True,rollback_storage_restored=after == before,changes=state.diff(before,after))
                support.dump(job/frontend.REPORT,report)
                raise ValueError('Native derivation refused/failed; no accepted candidate: '+str(answer))
            rig.update_tag(); bpy.context.view_layer.update()
            evaluated_after=evaluator.evaluation(rig,support)
            checked=verify(core,frontend,rig,req,snapshot)
            after=state.capture()
            protection=state.compare_derive(before,after,rig.name,req,checked['combined_request'],checked,snapshot['original_action'])
            report.update(authoring_protection=protection)
            if not protection['ok']:
                report.update(status='protection_failed',changes=protection['changes'])
                support.dump(job/frontend.REPORT,report)
                raise ValueError('Derived frame0 changed non-target authoring, including Key values')
            require(support.paths() == external,'Derivation changed external paths')
            runtime_expected=support.runtime_extra_users()
            require(set(runtime_before) <= set(runtime_expected) and all(runtime_expected[key] == value for key,value in runtime_before.items()),'Original runtime extra-user flags changed')
            added=set(runtime_expected)-set(runtime_before)
            expected_added={inspection.key(inspection.identity(bpy.data.actions[req['action_name']]))}
            require(added == expected_added and all(runtime_expected[key] is False for key in added),'Only new non-extra Action may extend runtime accounting')
            for item in config['implementation']: support.checked(item)
            support.checked(config['input']); support.checked(config['operation'])
            support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],support.paths())
            writer=persistence.save_candidate(job/frontend.CANDIDATE,job)
            require(['Action',req['base_clip']['action_name']] in [list(row) for row in writer['runtime_extra_user_temporarily_modified']],
                    'Zero-user original Action was not retained during actual native save')
            require(state.capture() == after and support.runtime_extra_users() == runtime_expected,'Writer changed full character authoring/runtime accounting')
            report_verification(report,req,snapshot,checked)
            report.update(status='candidate_waiting_independent_cold',candidate={key:writer[key] for key in ('file','expected_sha256')},
                writer=writer,after_snapshot=state.summarize(after),evaluation_after=evaluated_after,
                external_paths_after=support.paths(),runtime_extra_users_after=runtime_expected,source_unchanged=True)
    for item in config['implementation']: support.checked(item)
    for key in ('input','selection','operation','producer_report','witness','source_bundle'):
        if key in config: support.checked(config[key])
    if op: support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],support.paths())
    require(support.sha(__file__) == report['entry_sha'],'Derivation entry changed')
    support.dump(job/frontend.REPORT,report)
    return {'status':report['status'],'mode':mode,'operation_ok':report.get('operation_ok'),'source_unchanged':True,
            'source_saved':False,'context_restored':report.get('context_restored')}


if __name__ == '__main__':
    try: result=run(parameters,Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
