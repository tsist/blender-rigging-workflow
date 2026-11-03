"""Pinned style producer / independent cold worker, public extension.run."""
import copy
import base64
import hashlib
import importlib.util
import json
import math
import re
from pathlib import Path
import struct
import sys
import zlib
sys.dont_write_bytecode=True
import bpy

ROOT=Path(__file__).resolve().parents[1]
OBSERVER=ROOT.parents[1]/'tools/blenderctl'


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def evaluation(rig,support):
    bpy.context.view_layer.update()
    deps=bpy.context.evaluated_depsgraph_get()
    ev=deps.id_eval_get(rig)
    if not ev.is_evaluated:raise ValueError('Selected rig lacks native evaluated ID')
    value=hashlib.sha256()
    for pb in ev.pose.bones:
        value.update(pb.name.encode('utf-8')+b'\0')
        for row in ev.matrix_world@pb.matrix:
            if any(not math.isfinite(x) for x in row):raise ValueError('Nonfinite native evaluated pose')
            value.update(struct.pack('<4f',*row))
    meshes={};unavailable=[]
    members={x.original.name for x in deps.objects}
    for obj in bpy.context.scene.objects:
        if obj.type!='MESH':continue
        evaluated=deps.id_eval_get(obj)
        if not evaluated.is_evaluated:
            unavailable.append({'object':obj.name,'status':'not_evaluated','depsgraph_iterator_member':obj.name in members})
            continue
        mesh=evaluated.to_mesh()
        try:
            raw=bytearray()
            for vertex in mesh.vertices:
                point=evaluated.matrix_world@vertex.co
                if any(not math.isfinite(x) for x in point):raise ValueError('Nonfinite evaluated scene mesh')
                raw.extend(struct.pack('<3f',*point))
            meshes[obj.name]={'vertices':len(mesh.vertices),'world_f32_sha256':hashlib.sha256(raw).hexdigest(),'finite':True,
                'world_f32_zlib_base64':base64.b64encode(zlib.compress(raw)).decode('ascii')}
        finally:evaluated.to_mesh_clear()
    return {'pose_world_f32_sha256':value.hexdigest(),
            'meshes':meshes,'not_evaluated_meshes':sorted(unavailable,key=lambda x:x['object']),
            'scope':'current native-evaluated scene meshes and selected rig pose only; unevaluated IDs retain storage checks, no deformation/continuous/visual/material/GUI certification'}


def compare_evaluation(before,after):
    limit=1e-7
    for sample in (before,after):
        if (type(sample) is not dict or set(sample)!={'pose_world_f32_sha256','meshes','not_evaluated_meshes','scope'}
            or type(sample['pose_world_f32_sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}',sample['pose_world_f32_sha256']) is None
            or type(sample['meshes']) is not dict or type(sample['not_evaluated_meshes']) is not list
            or type(sample['scope']) is not str or not sample['scope']):raise ValueError('Invalid evaluation envelope')
        names=set(sample['meshes'])
        if any(type(n) is not str or not n for n in names):raise ValueError('Invalid mesh name')
        for item in sample['not_evaluated_meshes']:
            if (type(item) is not dict or set(item)!={'object','status','depsgraph_iterator_member'}
                or type(item['object']) is not str or not item['object'] or item['object'] in names
                or item['status']!='not_evaluated' or type(item['depsgraph_iterator_member']) is not bool):
                raise ValueError('Invalid unevaluated mesh observation')
            names.add(item['object'])
    if (before['pose_world_f32_sha256']!=after['pose_world_f32_sha256']
        or before['not_evaluated_meshes']!=after['not_evaluated_meshes'] or set(before['meshes'])!=set(after['meshes'])):
        raise ValueError('Pose or evaluated mesh coverage changed')
    drift=[]
    def points(item):
        if (type(item) is not dict or set(item)!={'vertices','world_f32_sha256','finite','world_f32_zlib_base64'}
            or type(item['vertices']) is not int or not 0<=item['vertices']<=3000000 or item['finite'] is not True
            or type(item['world_f32_sha256']) is not str or type(item['world_f32_zlib_base64']) is not str):
            raise ValueError('Invalid native mesh witness record')
        size=item['vertices']*12
        if type(size) is not int or not 0<=size<=36000000:raise ValueError('Mesh witness size budget exceeded')
        decoder=zlib.decompressobj()
        raw=decoder.decompress(base64.b64decode(item['world_f32_zlib_base64'],validate=True),size+1)
        if len(raw)!=size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Invalid bounded native coordinate witness')
        if hashlib.sha256(raw).hexdigest()!=item['world_f32_sha256']:raise ValueError('Coordinate witness hash differs')
        values=list(struct.iter_unpack('<3f',raw))
        if any(not math.isfinite(x) for p in values for x in p):raise ValueError('Nonfinite point witness')
        return values
    for name,first in before['meshes'].items():
        second=after['meshes'][name]
        first_points=points(first);second_points=points(second)
        if first['vertices']!=second['vertices']:
            raise ValueError('Evaluated mesh count/finite check changed')
        if first['world_f32_sha256']==second['world_f32_sha256']:continue
        maximum=0.;changed=0
        for a,b in zip(first_points,second_points):
            if any(not math.isfinite(x) for x in (*a,*b)):raise ValueError('Nonfinite point witness')
            distance=math.sqrt(sum((x-y)**2 for x,y in zip(a,b)))
            maximum=max(maximum,distance);changed+=a!=b
        drift.append({'object':name,'exact_hash_match':False,'changed_vertices':changed,'max_world_distance':maximum})
        if maximum>limit:raise ValueError('Evaluated mesh world displacement exceeds explicit 1e-7 limit: '+name)
    return {'ok':True,'pose_exact':True,'mesh_count':len(before['meshes']),'exact_meshes':len(before['meshes'])-len(drift),
        'mesh_drift':drift,'world_distance_limit':limit,
        'basis':'fixed build native no-data-change tag witness: one auxiliary point max7.68e-9; exact storage and pose remain strict'}


def operation(value,contract):
    if set(value)!={'schema','context','rig','request','external_inputs','inherited_missing_weak_references'} or value['schema']!='hrig.control-style.operation.v1':
        raise ValueError('Exact style operation schema/fields required')
    if type(value['rig']) is not str or not value['rig']:raise ValueError('Explicit rig required')
    contract.canonical_request(value['request'])
    return value


def selection(value,contract):
    if set(value)!={'schema','context','rig','template','bindings'} or value['schema']!='hrig.control-style.selection.v1':
        raise ValueError('Exact style selection schema/fields required')
    template=contract.canonical_template(value['template'])
    contract.canonical_bindings(value['bindings'],template)
    return value


def checked_context(value):
    if type(value) is not dict or set(value)!={'scene','view_layer','frame','subframe'}:
        raise ValueError('Exact scene/view-layer/frame/subframe required')
    if any(type(value[k]) is not str or not value[k] for k in ('scene','view_layer')):
        raise ValueError('Explicit context names required')
    if type(value['frame']) is not int or not -1048574<=value['frame']<=1048574:
        raise ValueError('Native frame must be bounded integer')
    if type(value['subframe']) not in (int,float) or not math.isfinite(value['subframe']) or not 0<=value['subframe']<1:
        raise ValueError('Native subframe must be finite in [0,1)')
    return value


def run(config,job):
    if tuple(bpy.app.version)!=(5,2,1) or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise ValueError('Requires Blender5.2.1 with embedded autoexec disabled')
    if Path(config['addon_root']).resolve()!=ROOT/'addon' or Path(config['observer_root']).resolve()!=OBSERVER:
        raise ValueError('Style role root mismatch')
    sys.path.insert(0,str(OBSERVER))
    support=load('_hrig_style_support',ROOT/'scripts/run_weights.py')
    required=set((ROOT/'addon/humanoid_rigging_probe').glob('*.py'))|{
        ROOT/'scripts/character_state.py',ROOT/'scripts/style_state.py',ROOT/'scripts/style_fixture.py',
        ROOT/'scripts/run_weights.py',OBSERVER/'inspection.py',OBSERVER/'protocol.py'}
    files=[support.checked(x) for x in config['implementation']]
    if len(files)!=len(set(files)) or set(files)!=required:raise ValueError('Exact style implementation closure required')
    sys.path.insert(0,str(ROOT/'addon'))
    from humanoid_rigging_probe import styles,persistence,style_contract as contract
    import inspection
    if Path(inspection.__file__).resolve()!=OBSERVER/'inspection.py':raise ValueError('Pinned observer import differs')
    state=load('_hrig_style_character_state',ROOT/'scripts/character_state.py')
    mask=load('_hrig_style_mask',ROOT/'scripts/style_state.py')
    fixture=load('_hrig_style_fixture',ROOT/'scripts/style_fixture.py')
    mode=config['mode'];extra={'inspect':{'input','selection'},'apply':{'input','operation'},'fixture':{'case'},'cold':{'input','producer_report'}}
    expected={'mode','implementation','addon_root','observer_root'}|extra.get(mode,set())
    if mode=='cold' and 'operation' in config:expected.add('operation')
    if mode not in extra or set(config)!=expected:raise ValueError('Unknown style mode/fields')
    report={'schema':'hrig.styles.report.v1','mode':mode,'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode(),
            'implementation':config['implementation'],'entry_sha':support.sha(__file__),
            'GUI':'deferred_by_user','FPS':'not_measured','complete_improved_Aon':False}
    op=None;manifest=None
    if mode=='cold':
        producer=support.read(config['producer_report'])
        if (producer.get('status')!='candidate_waiting_independent_cold' or producer['candidate']!=config['input']
            or producer['implementation']!=config['implementation'] or producer['entry_sha']!=support.sha(__file__)
            or producer.get('operation_descriptor')!=config.get('operation')):raise ValueError('Cold producer/candidate/implementation conflict')
        if 'operation' in config:
            op=operation(support.read(config['operation']),contract)
            if op!=producer['operation']:raise ValueError('Cold operation differs')
        bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])),load_ui=False,use_scripts=False)
        support.select_context(checked_context(producer['context']),frames=True)
        rig=bpy.data.objects[producer['rig']]
        actual=state.summarize(state.capture())
        if actual!=producer['after_snapshot']:
            report.update(status='cold_mismatch',changes=state.diff(producer['after_snapshot'],actual));support.dump(job/'output-styles.json',report)
            raise ValueError('Cold character storage mismatch')
        actual_evaluation=evaluation(rig,support)
        evaluated_comparison=compare_evaluation(producer['evaluation_after'],actual_evaluation)
        if support.paths()!=producer['external_paths_after']:raise ValueError('Cold external files differ')
        if op:report['external_input_validation']=support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],support.paths())
        manifest=producer.get('manifest')
        if manifest:
            if manifest.get('inject_rollback'):
                evidence=producer.get('rollback_injection',{})
                if evidence.get('completed_writes')!=1 or evidence.get('triggered') is not True:
                    raise ValueError('Cold rollback has no completed native write evidence')
            actual_fixture=fixture.capture(manifest)
            validation=fixture.validate(manifest,producer['fixture_before'],actual_fixture,producer['result'])
            if not validation['ok']:raise ValueError('Independent cold style fixture failed: '+str(validation))
            report['fixture_validation']=validation
        report.update(status='pass',source_unchanged=True,operation_ok=producer['operation_ok'],after_snapshot=actual,
            evaluation=actual_evaluation,evaluation_comparison=evaluated_comparison,runtime_extra_users_observed=support.runtime_extra_users(),
            automatic_texspace_observed=support.automatic_texspace(),
            derived_observation_scope='runtime extra-user and automatic texspace caches observed separately; true users/fake/manual texspace stay strict')
    else:
        if mode=='fixture':
            bpy.ops.wm.read_factory_settings(use_empty=True)
            manifest=fixture.create_case(config['case']);rig=bpy.data.objects[manifest['rig']]
            request=fixture.prepare(manifest,styles)
            ctx={'scene':bpy.context.scene.name,'view_layer':bpy.context.view_layer.name,
                 'frame':bpy.context.scene.frame_current,'subframe':bpy.context.scene.frame_subframe}
        else:
            bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])),load_ui=False,use_scripts=False)
            if tuple(bpy.data.version[:2])>tuple(bpy.app.version[:2]):raise ValueError('Future source version rejected')
            if mode=='apply':
                op=operation(support.read(config['operation']),contract);request=op['request'];ctx=op['context'];rig=bpy.data.objects.get(op['rig'])
                report.update(operation=op,operation_descriptor=config['operation'])
            else:
                picked=selection(support.read(config['selection']),contract);ctx=picked['context'];rig=bpy.data.objects.get(picked['rig'])
            support.select_context(checked_context(ctx),frames=True)
        if rig is None:raise ValueError('Explicit rig missing')
        before=state.capture();runtime_before=support.runtime_extra_users();external_before=support.paths()
        evaluated_before=evaluation(rig,support)
        report.update(rig=rig.name,context=ctx,before_snapshot=state.summarize(before),external_paths_before=external_before,
                      evaluation_before=evaluated_before,automatic_texspace_before=support.automatic_texspace())
        if op:report['external_input_validation']=support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],external_before)
        if mode=='inspect':
            inspected=styles.inspect_styles(bpy.context,rig,picked['template'],picked['bindings'])
            if not inspected['ok']:raise ValueError('Style inspection refused: '+str(inspected['error']))
            after=state.summarize(state.capture())
            if after!=report['before_snapshot']:raise ValueError('Style inspection changed character storage')
            support.checked(config['input'])
            report.update(status='pass',source_unchanged=True,inspection=inspected,after_snapshot=after)
        else:
            fixture_before=fixture.capture(manifest) if manifest else None
            original=styles._write_field;calls=0;injection=[]
            def injected(*args):
                nonlocal calls
                original(*args);calls+=1
                if calls==1:
                    injection.append({'bone':args[1],'field':args[2],'requested':args[3],
                                      'observed_after_write':styles._read_field(*args[:3])})
                    raise RuntimeError('fixture injected after first completed style field')
            if manifest and manifest.get('inject_rollback'):styles._write_field=injected
            try:answer=styles.apply_style(bpy.context,rig,request)
            finally:styles._write_field=original
            if manifest and manifest.get('inject_rollback'):
                if calls!=1 or len(injection)!=1 or injection[0]['requested']!=injection[0]['observed_after_write']:
                    raise ValueError('Rollback fixture did not execute exactly one completed native write')
                if answer['ok'] or 'fixture injected after first completed style field' not in answer.get('error',{}).get('message',''):
                    raise ValueError('Rollback result did not originate from injected native write')
                report['rollback_injection']={'completed_writes':calls,'triggered':True,'writes':injection}
            report.update(result=answer,operation_ok=answer['ok'])
            after=state.capture()
            if manifest:
                fixture_after=fixture.capture(manifest)
                validation=fixture.validate(manifest,fixture_before,fixture_after,answer)
                report.update(manifest=manifest,fixture_before=fixture_before,fixture_after=fixture_after,fixture_validation=validation)
                if not validation['ok']:
                    report['status']='fixture_failed';support.dump(job/'output-styles.json',report)
                    raise ValueError('Independent native style fixture failed: '+str(validation))
            elif not answer['ok']:
                report['status']='rejected';support.dump(job/'output-styles.json',report)
                raise ValueError('Style apply refused: '+str(answer['error']))
            if answer['ok']:
                first=mask.protected(before,rig.name,rig.data.name,request)
                second=mask.protected(after,rig.name,rig.data.name,request)
                if first!=second:
                    report.update(status='protection_failed',changes=state.diff(first,second));support.dump(job/'output-styles.json',report)
                    raise ValueError('Non-target style character fields changed')
                old=next(x for x in before['blocks'] if x['type']=='Object' and x['name']==rig.name)['custom_properties'].get(styles.LEDGER)
                new=next(x for x in after['blocks'] if x['type']=='Object' and x['name']==rig.name)['custom_properties'].get(styles.LEDGER)
                if mask.ledger_other_instances(old,request['instance_id'])!=mask.ledger_other_instances(new,request['instance_id']):
                    raise ValueError('Other style instances changed')
            elif before!=after:raise ValueError('Rejected/rolled-back style changed character storage')
            external_after=support.paths();evaluated_after=evaluation(rig,support)
            if external_after!=external_before:raise ValueError('Style operation changed external file set')
            try:report['evaluation_comparison']=compare_evaluation(evaluated_before,evaluated_after)
            except ValueError:
                report.update(status='evaluation_mismatch',evaluation_after=evaluated_after,
                              evaluation_changes=state.diff(evaluated_before,evaluated_after))
                support.dump(job/'output-styles.json',report)
                raise
            writer=persistence.save_candidate(job/'output-candidate.blend',job)
            after_summary=state.summarize(after)
            if state.summarize(state.capture())!=after_summary:raise ValueError('Writer changed in-memory storage')
            if support.runtime_extra_users()!=runtime_before:raise ValueError('Writer did not restore runtime users')
            report.update(status='candidate_waiting_independent_cold',candidate={k:writer[k] for k in ('file','expected_sha256')},
                writer=writer,after_snapshot=after_summary,external_paths_after=external_after,
                evaluation_after=evaluated_after,runtime_extra_users_after=support.runtime_extra_users(),automatic_texspace_after=support.automatic_texspace())
    for item in config['implementation']:support.checked(item)
    for role in ('input','operation','selection','producer_report'):
        if role in config:support.checked(config[role])
    if op:support.validate_native_inputs(op['external_inputs'],op['inherited_missing_weak_references'],support.paths())
    if support.sha(__file__)!=report['entry_sha']:raise ValueError('Style entry changed')
    report['collection_active_index_runtime_observation']=state.collection_active_indices()
    report['collection_active_index_scope']='raw index observed separately; null active identity canonical index=-1, native new-empty0/cold-1; nonnull active identity/index remain strict'
    support.dump(job/'output-styles.json',report)
    return {'status':report['status'],'mode':mode,'operation_ok':report.get('operation_ok'),
            'source_unchanged':report.get('source_unchanged'),'GUI':'deferred','FPS':'not_measured'}


if __name__=='__main__':
    try:result=run(parameters,Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
