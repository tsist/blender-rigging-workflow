"""Managed native embedded Cloth bake candidates through public extension.run."""
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
import rigctl as host
import motion_cli as motion
from host_contract import (CODES, ContractError, check_descriptor, check_descriptors, descriptor,
                           fields, number, read_json, require, text, walk_descriptors)

ROOT, SCRIPTS, ADDON, PROJECT = host.ROOT, host.SCRIPTS, host.ADDON, host.PROJECT
OBSERVER = PROJECT / 'tools/blenderctl'
VERSION = '0.10.0'
SCHEMA = 'hrig.cache.bundle.v1'
REPORT, CANDIDATE = 'output-cache.json', 'output-candidate.blend'
SELECTION_SCHEMA, OP_SCHEMA = 'hrig.cloth-cache.selection.v1', 'hrig.cloth-cache.operation.v1'
LIMIT = 2e-5
LIMITATIONS = [
    'selected native embedded Cloth bake with original cache ranges/settings/Action unchanged',
    'bake covers original range1..250; full coordinates certify explicit witness frames only',
    'cold first jumps to witness/cache end, then reverse order and repeated endpoints; no sequential warmup',
    'cache identity includes full coordinate witnesses and fixed candidate SHA, not baked flags alone',
    'replay accepts only producer-observed frames and identical mesh/bone/proxy coverage',
    'failed/interrupted cache mutation requires discarding owned worker; no cache rollback guarantee',
    'full authoring observation is bounded; opaque scripts and complete blend serialization not certified',
    'GUI deferred by user; FPS not measured; no collision/visual/complete improved Aon claim',
    'trusted local Python without OS sandbox; source disk SHA cannot observe unsaved GUI changes']
HELPERS = ('character_state.py', 'motion_state.py', 'dynamics_state.py', 'cache_state.py',
           'run_weights.py', 'run_styles.py', 'run_motion.py', 'motion_cli.py',
           'cache_cli.py', 'rigctl.py', 'host_contract.py')


def canonical(method, value):
    path = ADDON/'humanoid_rigging_probe/cache_contract.py'
    spec = importlib.util.spec_from_file_location('_hrig_host_cache_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return getattr(module, method)(value)
    except ValueError as error:
        raise ContractError('INVALID_REQUEST', getattr(error, 'code', 'CACHE_INVALID') + ': ' + str(error)) from error


def witness(value):
    fields(value, ('schema', 'context', 'frames', 'meshes', 'bones', 'proxy_objects'), label='cache witness')
    require(value['schema'] == 'hrig.cloth-cache.witness.v1', 'Expected cache-specific witness schema')
    motion.motion_context(value['context'])
    frames = value['frames']
    require(type(frames) is list and 1 <= len(frames) <= 64 and all(type(f) is int and 1 <= f <= 250 for f in frames)
            and frames == sorted(set(frames)), 'Cache witness needs up to64 explicit ascending unique integer frames1..250')
    for key, low, high in (('meshes', 1, 8), ('bones', 1, 256), ('proxy_objects', 0, 4)):
        names = value[key]
        require(type(names) is list and low <= len(names) <= high, 'Explicit bounded cache witness ' + key + ' required')
        for name in names: text(name, key + ' name', 1024)
        require(len(names) == len(set(names)), 'Duplicate witness ' + key)
    return value


def cold_frames(frames):
    return [frames[-1], *reversed(frames[:-1]), frames[0], frames[-1]]


def selection(value):
    fields(value, ('schema', 'context', 'rig', 'targets'), label='cache selection')
    require(value['schema'] == SELECTION_SCHEMA, 'Expected ' + SELECTION_SCHEMA)
    motion.motion_context(value['context']); text(value['rig'], 'rig')
    canonical('canonical_targets', value['targets'])
    return value


def operation(value):
    fields(value, ('schema', 'context', 'rig', 'request', 'external_inputs', 'inherited_missing_weak_references'), label='cache operation')
    require(value['schema'] == OP_SCHEMA, 'Expected ' + OP_SCHEMA)
    # Reuse exact existing native static-file and inherited-weak declarations.
    motion.operation({**value, 'schema': motion.OP_SCHEMA, 'request': {
        'schema': 'hrig.motion-clip.create.v1', 'action_name': '_ContractOnly', 'expected_snapshot_sha256': '0'*64,
        'tracks': [{'bone': '_ContractOnly', 'rotation_mode': 'XYZ', 'before': [0.,0.,0.],
                    'keys': [{'frame':1,'value':[0.,0.,0.]},{'frame':2,'value':[0.,0.,0.]}]}]}})
    canonical('canonical_request', value['request'])
    return value


def implementation():
    paths = sorted((ADDON/'humanoid_rigging_probe').glob('*.py')) + [SCRIPTS/name for name in HELPERS]
    paths += [OBSERVER/'inspection.py', OBSERVER/'protocol.py']
    return check_descriptors([descriptor(path) for path in paths])


def identity():
    paths = {'entry': SCRIPTS/'run_cache.py', 'public_wrapper': host.PUBLIC}
    paths.update({name[:-3]: SCRIPTS/name for name in HELPERS})
    paths.update({'addon_'+p.stem:p for p in sorted((ADDON/'humanoid_rigging_probe').glob('*.py'))})
    paths.update(inspection=OBSERVER/'inspection.py', protocol=OBSERVER/'protocol.py')
    return {role:descriptor(path) for role,path in paths.items()}


def unchanged(roles, impl):
    require(roles == identity() and impl == implementation(), 'Cache implementation/entry/helper identity changed', 'CONFLICT')


def request(roles, impl, config, outputs):
    config = {**config, 'addon_root':str(ADDON), 'observer_root':str(OBSERVER), 'implementation':impl}
    resources = list(impl)
    for item in walk_descriptors(config):
        if item not in resources: resources.append(item)
    if 'operation' in config:
        op = operation(read_json(check_descriptor(config['operation'])['file']))
        for item in op['external_inputs']:
            if item not in resources: resources.append(item)
    if 'selection' in config: selection(read_json(check_descriptor(config['selection'])['file']))
    if 'clip_request' in config: motion.canonical('canonical_request', read_json(check_descriptor(config['clip_request'])['file']))
    if 'witness' in config: witness(read_json(check_descriptor(config['witness'])['file']))
    check_descriptors(resources)
    return {'schema_version':'1.0','command':'extension.run','params':{
        'manifest':{'script':roles['entry'],'trust':'execute_local_python_without_sandbox','params':config,'outputs':outputs},
        'resources':resources}}


def public_record(record, roles, impl, config, outputs):
    actual = host.verify_public_record(record)
    require(actual == request(roles,impl,config,outputs), 'Exact cache public request differs', 'CONFLICT')
    result = read_json(record['result']['file']); data = result['data']
    require(result.get('error') is None and record['cli_version'] == result.get('cli_version')
            and data.get('script_sha256_observed') == roles['entry']['expected_sha256'], 'Cache public result identity differs', 'CONFLICT')
    rows = data['outputs']
    require(len(rows) == len(outputs) and {Path(row['file']).name for row in rows} == set(outputs), 'Cache public output set differs')
    for row in rows:
        require(not Path(row['file']).is_symlink() and Path(row['file']).stat().st_size == row['bytes'], 'Cache artifact bytes differ')
    for item in actual['params']['resources']:
        require(data.get('input_hashes',{}).get(item['file']) == item['expected_sha256'], 'Cache pinned input hash evidence differs', 'CONFLICT')
    return motion.read_report(record['outputs'][REPORT]['file'])


def compare_samples(reference, actual, picked, clip_request, frames):
    producer_witness = reference.get('witness', picked)
    require(all(producer_witness[key] == picked[key] for key in ('context','meshes','bones','proxy_objects')),
            'Cache witness object/context coverage changed')
    motion.check_replay_samples(reference,producer_witness,clip_request)
    motion.check_replay_samples(actual,{**picked,'frames':frames},clip_request)
    expected = {sample['frame']:sample for sample in reference['samples']}
    require(len(expected) == len(reference['samples']), 'Producer witness frames must be unique')
    maximum, changed = 0., 0
    def coords(first,second):
        nonlocal maximum, changed
        a,b = motion.decode_coordinates(first),motion.decode_coordinates(second)
        require(first['vertices'] == second['vertices'], 'Cache witness point count changed')
        for p,q in zip(struct.iter_unpack('<3f',a),struct.iter_unpack('<3f',b)):
            distance = math.sqrt(sum((x-y)**2 for x,y in zip(p,q)))
            maximum = max(maximum,distance); changed += p != q
            require(distance <= LIMIT, 'Cold cached geometry differs from producer same-frame witness', 'VALIDATION_FAILED')
    def array(first,second):
        if type(first) is list:
            require(type(second) is list and len(first) == len(second), 'Cached transform shape changed')
            for a,b in zip(first,second): array(a,b)
        else:
            require(type(first) in (int,float) and type(second) in (int,float) and abs(first-second) <= LIMIT,
                    'Cached bone/proxy transform exceeds cold tolerance', 'VALIDATION_FAILED')
    for sample in actual['samples']:
        require(sample['frame'] in expected, 'No producer same-frame cache identity witness')
        before = expected[sample['frame']]
        for name,item in sample['meshes'].items(): coords(before['meshes'][name],item)
        for name,item in sample['proxies'].items():
            for space in ('local','world'): coords(before['proxies'][name][space],item[space])
            array(before['proxies'][name]['matrix_world'],item['matrix_world'])
        for name,bone in sample['bones'].items():
            old = before['bones'][name]
            require(bone['rotation_mode'] == old['rotation_mode'] and bone['native_active_values'] == old['native_active_values'],
                    'Original native Action input changed across cache consumers')
            for key in ('world_matrix','matrix','matrix_basis','head','tail'): array(old[key],bone[key])
    return {'ok':True,'world_distance_limit':LIMIT,'transform_component_limit':LIMIT,
            'maximum_world_distance':maximum,'changed_coordinate_rows':changed,
            'sample_frames':frames,'scope':'explicit cached same-frame full-coordinate witnesses; no unsampled frame claim'}


def producer_config(mode, inputs):
    config = {'mode':mode,'input':inputs['source']}
    for key in ('selection','operation','clip_request','witness'):
        if key in inputs: config[key] = inputs[key]
    return config


def cold_config(producer, inputs):
    return {'mode':'cold','input':producer['outputs'][CANDIDATE],'producer_report':producer['outputs'][REPORT],
            **{key:inputs[key] for key in ('operation','clip_request','witness')}}


def header(report, mode, roles, impl):
    require(report.get('schema') == 'hrig.cache.report.v1' and report.get('mode') == mode
            and report.get('entry_sha') == roles['entry']['expected_sha256'] and report.get('implementation') == impl,
            'Cache native report identity/schema differs', 'CONFLICT')


def baked_evidence(report, targets):
    rows=report.get('derived_cache_final')
    require(type(rows) is list and len(rows)==len(targets),'Selected baked-state coverage differs')
    observed=[]
    for row in rows:
        fields(row,('object','modifier','index','cache'),label='native baked observation')
        observed.append({key:row[key] for key in ('object','modifier','index')})
        cache=row['cache']
        require(cache.get('is_baked') is True and cache.get('is_baking') is False
                and cache.get('is_outdated') is False and cache.get('is_frame_skip') is False,
                'Selected native embedded cache is not complete/current')
    require(observed==targets,'Selected baked-state identities/order differ')


def check_producer(report, record, roles, impl, mode, inputs):
    header(report,mode,roles,impl)
    require(record['worker_result'].get('mode') == mode and record['worker_result'].get('status') == report.get('status'), 'Cache public/native status differs')
    require(report.get('source_unchanged') is True, 'Cache original source hash changed')
    if mode == 'inspect':
        require(report.get('status') == 'pass', 'Cache inspect failed')
    else:
        require(report.get('status') == 'candidate_waiting_independent_cold' and report.get('operation_ok') is True
                and report.get('candidate') == record['outputs'][CANDIDATE] and report.get('discard_required') is False,
                'Cache candidate not safely ready for independent cold', 'VALIDATION_FAILED')
        picked = witness(read_json(inputs['witness']['file']))
        clip = motion.canonical('canonical_request',read_json(inputs['clip_request']['file']))
        require(report.get('witness') == picked and report.get('clip_request') == clip, 'Cache producer clip/witness differs')
        op=operation(read_json(inputs['operation']['file'])); targets=canonical('canonical_request',op['request'])['targets']
        require(report.get('operation')==op and report.get('input')==inputs['source']
                and report.get('clip_request_descriptor')==inputs['clip_request'] and report.get('witness_descriptor')==inputs['witness'],
                'Cache producer explicit input bindings differ')
        native_range=report['native_cache_range']
        require(picked['frames'][0]==native_range['frame_start'] and picked['frames'][-1]==native_range['frame_end'],
                'Producer cache witnesses do not cover original range endpoints')
        baked_evidence(report,targets)
        require(report['before_snapshot'] == report['after_snapshot'] and report['authoring_preserved'] is True, 'Cache authoring state changed')
        motion.check_replay_samples(report,picked,clip)
        require(report['complete_coordinate_points']//len(picked['frames'])*len(cold_frames(picked['frames']))<=4000000,
                'Cold repeated-witness full-point budget exceeded')


def check_cold(report, record, producer, roles, impl, inputs):
    header(report,'cold',roles,impl)
    require(report.get('status') == 'pass' and report.get('source_saved') is False and report.get('source_unchanged') is True
            and report.get('context_restored') is True and record['worker_result'].get('status') == 'pass'
            and record['worker_result'].get('mode')=='cold', 'Independent cached cold failed')
    original = motion.read_report(producer['outputs'][REPORT]['file'])
    require(report['after_snapshot'] == original['after_snapshot'] and report['clip_verification']['state'] == original['clip_verification']['state'],
            'Cold authoring/Action state differs')
    picked = witness(read_json(inputs['witness']['file'])); clip = motion.canonical('canonical_request',read_json(inputs['clip_request']['file']))
    require(report.get('input')==producer['outputs'][CANDIDATE] and report.get('producer_report')==producer['outputs'][REPORT]
            and report.get('witness')==picked and report.get('clip_request')==clip,'Cold explicit source/producer/witness/Action binding differs')
    baked_evidence(report,canonical('canonical_request',original['request'])['targets'])
    expected = compare_samples(original,report,picked,clip,cold_frames(picked['frames']))
    require(report.get('sample_comparison') == expected, 'Cold numerical comparison evidence differs')


def check_bundle(value):
    fields(value,('schema','frontend_version','status','mode','identity','implementation','inputs','producer_config','producer','report',
                  'verification','created_utc','GUI','FPS','limitations'),('cold','candidate','validated_bundle'),label='cache bundle')
    require(value['schema'] == SCHEMA and value['frontend_version'] == VERSION and value['status'] == 'succeeded'
            and value['mode'] in ('inspect','bake'), 'Expected succeeded cache authoring bundle')
    roles,impl,mode = value['identity'],value['implementation'],value['mode']; unchanged(roles,impl)
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and value['limitations'] == LIMITATIONS,'Cache scope differs')
    for _ in walk_descriptors(value): pass
    inputs = value['inputs']; fields(inputs,('source','selection') if mode == 'inspect' else ('source','operation','clip_request','witness'),label='cache inputs')
    config = producer_config(mode,inputs); require(value['producer_config'] == config,'Cache config/input binding differs')
    outputs = [REPORT] if mode == 'inspect' else [REPORT,CANDIDATE]
    report = public_record(value['producer'],roles,impl,config,outputs); check_producer(report,value['producer'],roles,impl,mode,inputs)
    require(value['report'] == value['producer']['outputs'][REPORT],'Cache report artifact differs')
    if mode == 'inspect':
        require('cold' not in value and 'candidate' not in value and value['verification'] == report,'Read-only inspect contains a candidate')
    else:
        require(value['candidate'] == value['producer']['outputs'][CANDIDATE] and value['cold']['job_id'] != value['producer']['job_id'], 'Cold/candidate not independent')
        cold = public_record(value['cold'],roles,impl,cold_config(value['producer'],inputs),[REPORT])
        check_cold(cold,value['cold'],value['producer'],roles,impl,inputs)
        require(value['verification'] == cold,'Cache cold verification differs')
    return value


def check_replay_bundle(value):
    fields(value,('schema','frontend_version','status','mode','identity','implementation','inputs','config','record','report','verification',
                  'source','created_utc','GUI','FPS','limitations'),label='cache replay bundle')
    require(value['schema'] == 'hrig.cache.replay.bundle.v1' and value['frontend_version'] == VERSION and value['status'] == 'succeeded' and value['mode'] == 'replay','Invalid cache replay bundle')
    unchanged(value['identity'],value['implementation'])
    for _ in walk_descriptors(value): pass
    fields(value['inputs'],('bundle','witness'),label='cache replay inputs')
    prior = check_bundle(motion.read_report(value['inputs']['bundle']['file'])); require(prior['mode'] == 'bake','Replay requires baked candidate')
    expected = {**cold_config(prior['producer'],prior['inputs']),'mode':'replay','witness':value['inputs']['witness'],'source_bundle':value['inputs']['bundle']}
    require(value['config'] == expected and value['source'] == prior['candidate'],'Cache replay source/config differs')
    report = public_record(value['record'],value['identity'],value['implementation'],expected,[REPORT]); header(report,'replay',value['identity'],value['implementation'])
    require(report['status'] == 'pass' and report['source_saved'] is False and report['source_unchanged'] is True and report['context_restored'] is True,'Cache replay failed')
    require(value['record']['worker_result'].get('mode')=='replay' and value['record']['worker_result'].get('status')=='pass',
            'Cache replay public/native mode or status differs')
    require(value['report'] == value['record']['outputs'][REPORT] and value['verification'] == report,'Replay exact artifact differs')
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and value['limitations'] == LIMITATIONS,'Replay scope differs')
    original = motion.read_report(prior['report']['file']); picked = witness(read_json(value['inputs']['witness']['file']))
    require(report.get('input')==prior['candidate'] and report.get('source_bundle')==value['inputs']['bundle']
            and report.get('producer_report')==prior['report'] and report.get('witness')==picked
            and report.get('clip_request')==original['clip_request'],'Cache replay explicit source/producer/witness/Action binding differs')
    baked_evidence(report,canonical('canonical_request',original['request'])['targets'])
    require(all(picked[key] == original['witness'][key] for key in ('context','meshes','bones','proxy_objects'))
            and set(picked['frames']) <= set(original['witness']['frames']), 'Replay may use only producer-observed frames/coverage')
    comparison = compare_samples(original,report,picked,original['clip_request'],picked['frames'])
    require(report['after_snapshot'] == original['after_snapshot'] and report['clip_verification']['state'] == original['clip_verification']['state']
            and report['sample_comparison'] == comparison,'Replay original authoring/Action/cache witnesses differ')
    return value


def parser():
    p=host.Parser(description=__doc__); commands=p.add_subparsers(dest='command',required=True,parser_class=host.Parser)
    commands.add_parser('describe')
    inspect=commands.add_parser('inspect'); bake=commands.add_parser('bake'); validate=commands.add_parser('validate'); replay=commands.add_parser('replay')
    for command in (inspect,bake): command.add_argument('--source',required=True); command.add_argument('--expected-sha256',required=True)
    inspect.add_argument('--selection',required=True); inspect.add_argument('--expected-selection-sha256',required=True)
    bake.add_argument('--operation',required=True); bake.add_argument('--expected-operation-sha256',required=True)
    for key in ('clip-request','witness'): bake.add_argument('--'+key,required=True); bake.add_argument('--expected-'+key+'-sha256',required=True)
    for command in (validate,replay): command.add_argument('--bundle',required=True); command.add_argument('--expected-sha256',required=True)
    replay.add_argument('--witness',required=True); replay.add_argument('--expected-witness-sha256',required=True)
    for command in (inspect,bake,validate,replay): command.add_argument('--out-dir',required=True); command.add_argument('--timeout',type=float,default=900)
    return p


def preflight(args):
    number(args.timeout,'timeout',1,86400); inputs={}; prior=None
    if args.command in ('inspect','bake'):
        inputs['source']=descriptor(args.source,args.expected_sha256); require(Path(inputs['source']['file']).suffix.lower()=='.blend','Blend source required')
        for key in (('selection',) if args.command=='inspect' else ('operation','clip_request','witness')):
            inputs[key]=descriptor(getattr(args,key),getattr(args,'expected_'+key+'_sha256'))
        if args.command=='inspect': selection(read_json(inputs['selection']['file']))
        else: operation(read_json(inputs['operation']['file'])); motion.canonical('canonical_request',read_json(inputs['clip_request']['file'])); witness(read_json(inputs['witness']['file']))
    else:
        inputs['bundle']=descriptor(args.bundle,args.expected_sha256); prior=check_bundle(motion.read_report(inputs['bundle']['file']))
        require(prior['mode']=='bake','Cache validation/replay requires successful baked candidate')
        if args.command=='replay': inputs['witness']=descriptor(args.witness,args.expected_witness_sha256); witness(read_json(inputs['witness']['file']))
    return inputs,prior


def execute(args,run,inputs,prior):
    roles,impl=identity(),implementation()
    if args.command=='replay':
        config={**cold_config(prior['producer'],prior['inputs']),'mode':'replay','witness':inputs['witness'],'source_bundle':inputs['bundle']}
        record=host.public_job(run,'replay',request(roles,impl,config,[REPORT]),args.timeout)
        evidence=public_record(record,roles,impl,config,[REPORT])
        final={'schema':'hrig.cache.replay.bundle.v1','frontend_version':VERSION,'status':'succeeded','mode':'replay','identity':roles,'implementation':impl,
               'inputs':inputs,'config':config,'record':record,'report':record['outputs'][REPORT],'verification':evidence,'source':prior['candidate'],
               'created_utc':datetime.now(timezone.utc).isoformat(),'GUI':'deferred_by_user','FPS':'not_measured','limitations':LIMITATIONS}
        check_replay_bundle(final)
    else:
        if prior is None:
            mode=args.command; config=producer_config(mode,inputs); outputs=[REPORT] if mode=='inspect' else [REPORT,CANDIDATE]
            producer=host.public_job(run,'producer',request(roles,impl,config,outputs),args.timeout)
            evidence=public_record(producer,roles,impl,config,outputs); check_producer(evidence,producer,roles,impl,mode,inputs)
            final={'schema':SCHEMA,'frontend_version':VERSION,'status':'succeeded','mode':mode,'identity':roles,'implementation':impl,'inputs':inputs,
                   'producer_config':config,'producer':producer,'report':producer['outputs'][REPORT],'verification':evidence,
                   'created_utc':datetime.now(timezone.utc).isoformat(),'GUI':'deferred_by_user','FPS':'not_measured','limitations':LIMITATIONS}
        else:
            final={**prior,'validated_bundle':inputs['bundle'],'created_utc':datetime.now(timezone.utc).isoformat()}; inputs=prior['inputs']; mode=prior['mode']; producer=prior['producer']
        if mode=='bake':
            config=cold_config(producer,inputs); cold=host.public_job(run,'cold',request(roles,impl,config,[REPORT]),args.timeout)
            evidence=public_record(cold,roles,impl,config,[REPORT]); check_cold(evidence,cold,producer,roles,impl,inputs)
            final.update(cold=cold,candidate=producer['outputs'][CANDIDATE],verification=evidence)
        check_bundle(final)
    unchanged(roles,impl); host.write_new(run/'bundle.json',final)
    return {'ok':True,'status':'succeeded','mode':final['mode'],'bundle':descriptor(run/'bundle.json'),'report':final['report'],
            'candidate':final.get('candidate'),'source_saved':False,'operation_ok':final['verification'].get('operation_ok'),
            'producer_job_id':final.get('producer',{}).get('job_id'),'cold_job_id':final.get('cold',{}).get('job_id'),
            'job_id':final.get('record',{}).get('job_id'),'GUI':'deferred_by_user','FPS':'not_measured'}


def describe():
    return {'schema':'hrig.cache.cli.describe.v1','frontend_version':VERSION,'commands':['describe','inspect','bake','validate','replay'],
            'backend':'existing public extension.run','public_cache_commands_added':False,'selection_schema':SELECTION_SCHEMA,'operation_schema':OP_SCHEMA,
            'request_schema':'hrig.cloth-cache.bake.v1','witness_schema':'hrig.cloth-cache.witness.v1',
            'cold_order':'last; all other witness frames reverse; repeat first and last','max_witness_frames':64,'max_complete_coordinate_points':4000000,
            'cold_geometry_limit':LIMIT,'native_validation':'not_asserted_by_discovery','limitations':LIMITATIONS}


def main(argv=None):
    run=None
    try:
        args=parser().parse_args(argv)
        if args.command=='describe': response=describe()
        else:
            inputs,prior=preflight(args); identity(); implementation(); run=host.new_run_dir(args.out_dir); response=execute(args,run,inputs,prior)
        print(json.dumps(response,ensure_ascii=False,allow_nan=False)); return 0
    except (Exception,KeyboardInterrupt) as error:
        code=error.code if isinstance(error,ContractError) else 'CANCELLED' if isinstance(error,KeyboardInterrupt) else 'TIMEOUT' if isinstance(error,subprocess.TimeoutExpired) else 'IO_ERROR' if isinstance(error,OSError) else 'INVALID_REQUEST' if isinstance(error,(ValueError,KeyError,TypeError)) else 'WORKER_FAILED'
        failure={'ok':False,'status':'failed','error':{'code':code,'message':str(error)},'run_directory':str(run) if run else None,
                 'candidate_policy':'no succeeded bundle; failed/interrupted native cache worker must be discarded'}
        if run is not None:
            try: host.write_new(run/'failure.json',failure)
            except OSError: pass
        print(json.dumps(failure,ensure_ascii=False,allow_nan=False)); return error.exit_code if isinstance(error,ContractError) else CODES[code]


if __name__=='__main__': raise SystemExit(main())
