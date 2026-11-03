"""Pinned bone-collection producer and independent native cold worker."""
import json
from pathlib import Path
import sys
sys.dont_write_bytecode=True
import bpy
import importlib.util
ROOT=Path(__file__).resolve().parents[1]
OBSERVER=ROOT.parents[1]/'tools/blenderctl'
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def run(config,job):
    if tuple(bpy.app.version)!=(5,2,1) or bpy.context.preferences.filepaths.use_scripts_auto_execute:raise ValueError('Verified Blender5.2.1 with autoexec disabled required')
    if Path(config['addon_root']).resolve()!=ROOT/'addon' or Path(config['observer_root']).resolve()!=OBSERVER:raise ValueError('Role roots differ')
    sys.path.insert(0,str(OBSERVER));support=load('_hrig_collection_support',ROOT/'scripts/run_weights.py')
    required=set((ROOT/'addon/humanoid_rigging_probe').glob('*.py'))|{ROOT/'scripts'/x for x in (
        'character_state.py','collection_state.py','collection_fixture.py','run_weights.py','run_styles.py','style_state.py','style_fixture.py')}|{OBSERVER/'inspection.py',OBSERVER/'protocol.py'}
    files=[support.checked(x) for x in config['implementation']]
    if len(files)!=len(set(files)) or set(files)!=required:raise ValueError('Exact collection implementation closure required')
    sys.path.insert(0,str(ROOT/'addon'))
    from humanoid_rigging_probe import bone_collections as core,collection_contract as contract,persistence
    import inspection
    if Path(inspection.__file__).resolve()!=OBSERVER/'inspection.py':raise ValueError('Observer role import differs')
    state=load('_hrig_collection_character',ROOT/'scripts/character_state.py')
    native=load('_hrig_collection_state',ROOT/'scripts/collection_state.py')
    fixture=load('_hrig_collection_fixture',ROOT/'scripts/collection_fixture.py')
    style=load('_hrig_collection_evaluation',ROOT/'scripts/run_styles.py')
    mode=config['mode'];extra={'inspect':{'input','selection'},'apply':{'input','operation'},'fixture':{'case'},'cold':{'input','producer_report'}}
    expected={'mode','implementation','addon_root','observer_root'}|extra.get(mode,set())
    if mode=='cold' and 'operation' in config:expected.add('operation')
    if mode not in extra or set(config)!=expected:raise ValueError('Unknown collection worker mode/fields')
    report={'schema':'hrig.collections.report.v1','mode':mode,'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode(),
        'implementation':config['implementation'],'entry_sha':support.sha(__file__),'GUI':'deferred_by_user','FPS':'not_measured','complete_improved_Aon':False}
    op=None;manifest=None
    def operation(value):
        if set(value)!={'schema','context','rig','request','external_inputs','inherited_missing_weak_references'} or value['schema']!='hrig.bone-collections.operation.v1':raise ValueError('Exact collection operation required')
        contract.canonical_request(value['request']);style.checked_context(value['context']);return value
    if mode=='cold':
        producer=support.read(config['producer_report'])
        if (producer.get('status')!='candidate_waiting_independent_cold' or producer['candidate']!=config['input']
            or producer['implementation']!=config['implementation'] or producer['entry_sha']!=support.sha(__file__)
            or producer.get('operation_descriptor')!=config.get('operation')):raise ValueError('Cold producer/candidate/implementation mismatch')
        if 'operation' in config:
            op=operation(support.read(config['operation']))
            if op!=producer['operation']:raise ValueError('Cold operation content differs')
        bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])),load_ui=False,use_scripts=False)
        support.select_context(style.checked_context(producer['context']),frames=True);rig=bpy.data.objects[producer['rig']]
        actual=state.summarize(state.capture())
        if actual!=producer['after_snapshot']:
            report.update(status='cold_mismatch',changes=state.diff(producer['after_snapshot'],actual));support.dump(job/'output-collections.json',report);raise ValueError('Cold character storage differs')
        captured=native.capture(rig)
        if captured!=producer['native_after']:raise ValueError('Cold native collection system differs')
        if support.paths()!=producer['external_paths_after']:raise ValueError('Cold external files differ')
        evaluated=style.evaluation(rig,support);comparison=style.compare_evaluation(producer['evaluation_after'],evaluated)
        if op:report['external_input_validation']=support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],support.paths())
        manifest=producer.get('manifest')
        if manifest:
            if manifest.get('inject_rollback') and (producer.get('rollback_injection',{}).get('completed_mutations')!=1 or producer['rollback_injection'].get('changed_native') is not True):raise ValueError('Missing actual completed rollback mutation evidence')
            validation=fixture.validate(manifest,producer['fixture_before'],fixture.capture(manifest),producer['result'])
            if not validation['ok']:raise ValueError('Independent cold collection fixture failed: '+str(validation))
            report['fixture_validation']=validation
        report.update(status='pass',source_unchanged=True,operation_ok=producer['operation_ok'],after_snapshot=actual,native_after=captured,
            evaluation=evaluated,evaluation_comparison=comparison,runtime_extra_users_observed=support.runtime_extra_users(),automatic_texspace_observed=support.automatic_texspace())
    else:
        if mode=='fixture':
            bpy.ops.wm.read_factory_settings(use_empty=True);manifest=fixture.create_case(config['case']);rig=bpy.data.objects[manifest['rig']]
            request=fixture.prepare(manifest,core);ctx=manifest['context']
        else:
            bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])),load_ui=False,use_scripts=False)
            if tuple(bpy.data.version[:2])>tuple(bpy.app.version[:2]):raise ValueError('Future source rejected')
            if mode=='apply':
                op=operation(support.read(config['operation']));request=op['request'];picked=op
                report.update(operation=op,operation_descriptor=config['operation'])
            else:
                picked=support.read(config['selection'])
                if set(picked)!={'schema','context','rig','template','bindings'} or picked['schema']!='hrig.bone-collections.selection.v1':raise ValueError('Exact collection selection required')
            ctx=style.checked_context(picked['context']);support.select_context(ctx,frames=True);rig=bpy.data.objects.get(picked['rig'])
        if rig is None:raise ValueError('Explicit rig missing')
        before=state.capture();native_before=native.capture(rig);runtime_before=support.runtime_extra_users();external_before=support.paths()
        evaluated_before=style.evaluation(rig,support)
        report.update(rig=rig.name,context=ctx,before_snapshot=state.summarize(before),native_before=native_before,
            external_paths_before=external_before,evaluation_before=evaluated_before)
        if op:report['external_input_validation']=support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],external_before)
        if mode=='inspect':
            inspected=core.inspect_collections(bpy.context,rig,picked['template'],picked['bindings'])
            if not inspected['ok']:raise ValueError('Collection inspect refused: '+str(inspected['error']))
            after=state.summarize(state.capture())
            if after!=report['before_snapshot'] or native.capture(rig)!=native_before:raise ValueError('Read-only inspection changed storage')
            report.update(status='pass',source_unchanged=True,inspection=inspected,after_snapshot=after)
        else:
            fixture_before=fixture.capture(manifest) if manifest else None
            original=core._mutate;calls=0;witness=[]
            def injected(kind,*args):
                nonlocal calls
                first=native.capture(rig);value=original(kind,*args);calls+=1
                if calls==1:
                    second=native.capture(rig);witness.append({'kind':kind,'before_sha256':state.digest(first),'after_sha256':state.digest(second),'changed_native':first!=second})
                    raise RuntimeError('fixture injected after first completed collection mutation')
                return value
            if manifest and manifest.get('inject_rollback'):core._mutate=injected
            try:answer=core.apply_collections(bpy.context,rig,request)
            finally:core._mutate=original
            report.update(result=answer,operation_ok=answer['ok'])
            if manifest and manifest.get('inject_rollback'):
                if calls!=1 or len(witness)!=1 or not witness[0]['changed_native'] or answer['ok'] or 'fixture injected after first completed collection mutation' not in answer.get('error',{}).get('message',''):raise ValueError('Rollback did not follow one actual completed mutation')
                report['rollback_injection']={'completed_mutations':calls,'changed_native':True,'witness':witness}
            after=state.capture();native_after=native.capture(rig)
            if manifest:
                actual_fixture=fixture.capture(manifest);validation=fixture.validate(manifest,fixture_before,actual_fixture,answer)
                report.update(manifest=manifest,fixture_before=fixture_before,fixture_after=actual_fixture,fixture_validation=validation)
                if not validation['ok']:
                    report.update(status='fixture_failed');support.dump(job/'output-collections.json',report);raise ValueError('Independent native collection fixture failed: '+str(validation))
            elif not answer['ok']:
                report.update(status='rejected');support.dump(job/'output-collections.json',report);raise ValueError('Collection apply refused: '+str(answer['error']))
            request_checked=contract.canonical_request(request) if answer['ok'] else request
            report['native_validation']=native.validate(native_before,native_after,request_checked,answer)
            if answer['ok']:
                first=native.protected(before,rig.name,rig.data.name,request_checked,native_before)
                second=native.protected(after,rig.name,rig.data.name,request_checked,native_after)
                if first!=second:
                    report.update(status='protection_failed',changes=state.diff(first,second));support.dump(job/'output-collections.json',report);raise ValueError('Non-target character storage changed')
            elif before!=after:raise ValueError('Rejected/rolled-back collection changed character storage')
            evaluated_after=style.evaluation(rig,support);external_after=support.paths()
            report['evaluation_comparison']=style.compare_evaluation(evaluated_before,evaluated_after)
            if external_after!=external_before:raise ValueError('Collection operation changed external files')
            writer=persistence.save_candidate(job/'output-candidate.blend',job);after_summary=state.summarize(after)
            if state.summarize(state.capture())!=after_summary or support.runtime_extra_users()!=runtime_before:raise ValueError('Native writer changed original storage/runtime users')
            report.update(status='candidate_waiting_independent_cold',candidate={k:writer[k] for k in ('file','expected_sha256')},writer=writer,
                after_snapshot=after_summary,native_after=native_after,external_paths_after=external_after,evaluation_after=evaluated_after,
                runtime_extra_users_after=support.runtime_extra_users(),automatic_texspace_after=support.automatic_texspace())
    for row in config['implementation']:support.checked(row)
    for name in ('input','operation','selection','producer_report'):
        if name in config:support.checked(config[name])
    if op:support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],support.paths())
    if support.sha(__file__)!=report['entry_sha']:raise ValueError('Collection entry changed')
    report['collection_active_index_runtime_observation']=state.collection_active_indices()
    report['collection_active_index_scope']='raw index observed separately; null active identity canonical index=-1, native new-empty0/cold-1; nonnull active identity/index remain strict'
    support.dump(job/'output-collections.json',report)
    return {'status':report['status'],'mode':mode,'operation_ok':report.get('operation_ok'),'source_unchanged':report.get('source_unchanged'),'GUI':'deferred','FPS':'not_measured'}


if __name__=='__main__':
    try:result=run(parameters,Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8');raise
