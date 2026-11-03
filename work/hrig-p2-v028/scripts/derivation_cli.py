"""Explicit derived rotation Action candidates via existing extension.run."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import subprocess
import sys
import types
import json

sys.dont_write_bytecode = True
import rigctl as host
import motion_cli as motion
from host_contract import (CODES, ContractError, check_descriptor, check_descriptors, descriptor,
                           fields, number, read_json, require, text, walk_descriptors)

ROOT, SCRIPTS, ADDON, PROJECT = host.ROOT, host.SCRIPTS, host.ADDON, host.PROJECT
OBSERVER = PROJECT/'tools/blenderctl'
VERSION = '0.12.0'
SCHEMA = 'hrig.motion-derivation.bundle.v1'
REPORT_SCHEMA = 'hrig.motion-derivation.report.v1'
REPLAY_SCHEMA = 'hrig.motion-derivation.replay.bundle.v1'
OP_SCHEMA = 'hrig.motion-clip.derivation-operation.v1'
SELECTION_SCHEMA = 'hrig.motion-clip.derivation-selection.v1'
REPORT, CANDIDATE = 'output-derivation.json', 'output-candidate.blend'
HELPERS = ('derivation_cli.py', 'derivation_state.py', 'motion_cli.py', 'motion_state.py',
           'character_state.py', 'run_motion.py', 'run_weights.py', 'run_styles.py',
           'rigctl.py', 'host_contract.py')
LIMITATIONS = [
    'explicit Action.copy and added nonoverlapping rotation tracks; old create guard unchanged',
    'old Action remains exact with real users1-to0 and no fake/extra user; independent cold required',
    'four verified AnimData binding fields and one removed/added rig edge only; all other authoring strict',
    'first keys equal original frame0 values; body mesh/weights/Key values/rest/drivers are not masked',
    'replay uses combined declared input curves and complete bounded fresh sequential coordinates',
    'replay returns to frame0 before full authoring verification; no baked/disk/external cache replay',
    'native static files SHA-bound; inherited missing weak references unresolved',
    'GUI deferred; FPS not measured; no continuous collision/artistic or complete Aon acceptance',
    'trusted local Python without OS sandbox; disk SHA does not observe unsaved GUI changes']


def canonical(method, value):
    # Load the two pure contracts without executing the addon's bpy __init__.
    package_name = '_hrig_derivation_pure'
    package = types.ModuleType(package_name)
    package.__path__ = [str(ADDON/'humanoid_rigging_probe')]
    sys.modules[package_name] = package
    for name in ('motion_contract', 'motion_derivation_contract'):
        fullname = package_name + '.' + name
        spec = importlib.util.spec_from_file_location(fullname, ADDON/'humanoid_rigging_probe'/(name+'.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[fullname] = module
        spec.loader.exec_module(module)
        setattr(package, name, module)
    try:
        return getattr(module, method)(value)
    except ValueError as error:
        raise ContractError('INVALID_REQUEST', getattr(error, 'code', 'DERIVATION_INVALID') + ': ' + str(error)) from error


def selection(value):
    fields(value, ('schema','context','rig','bones','action_name','base_clip'), label='derivation selection')
    require(value['schema'] == SELECTION_SCHEMA, 'Expected '+SELECTION_SCHEMA)
    motion.motion_context(value['context']); text(value['rig'], 'rig')
    bones = canonical('canonical_bones', value['bones'])
    name = canonical('canonical_name', value['action_name'])
    base = canonical('canonical_base', value['base_clip'])
    require(name != base['action_name'] and not set(bones) & {row['bone'] for row in base['tracks']},
            'New Action and added bone identities must differ from base')
    require(len(bones)+len(base['tracks']) <= 32, 'Combined clip exceeds32 bones')
    return value


def operation(value):
    fields(value, ('schema','context','rig','request','external_inputs','inherited_missing_weak_references'), label='derivation operation')
    require(value['schema'] == OP_SCHEMA, 'Expected '+OP_SCHEMA)
    motion.motion_context(value['context']); text(value['rig'], 'rig')
    canonical('canonical_request', value['request'])
    check_descriptors(value['external_inputs'])
    missing = value['inherited_missing_weak_references']
    require(type(missing) is list and len(missing) <= 1024, 'Explicit bounded missing weak origins required')
    seen = set()
    for path in missing:
        require(type(path) is str and len(path) <= 4096 and Path(path).is_absolute(), 'Missing weak origin must be absolute')
        key = str(Path(path).resolve()).casefold()
        require(key not in seen and not Path(path).exists(), 'Weak origin duplicate/now exists; re-inspect and pin it', 'CONFLICT')
        seen.add(key)
    return value


def implementation_paths():
    return sorted((ADDON/'humanoid_rigging_probe').glob('*.py')) + [SCRIPTS/name for name in HELPERS] + [OBSERVER/'inspection.py', OBSERVER/'protocol.py']


def implementation():
    return check_descriptors([descriptor(path) for path in implementation_paths()])


def identity():
    paths = {'entry':SCRIPTS/'run_derivation.py', 'public_wrapper':host.PUBLIC}
    paths.update({name[:-3]:SCRIPTS/name for name in HELPERS})
    paths.update({'addon_'+p.stem:p for p in sorted((ADDON/'humanoid_rigging_probe').glob('*.py'))})
    paths.update(inspection=OBSERVER/'inspection.py', protocol=OBSERVER/'protocol.py')
    return {role:descriptor(path) for role,path in paths.items()}


def unchanged(roles, impl):
    require(roles == identity() and impl == implementation(), 'Derivation implementation identity changed', 'CONFLICT')


def request(roles, impl, config, outputs):
    config = {**config, 'addon_root':str(ADDON), 'observer_root':str(OBSERVER), 'implementation':impl}
    resources = list(impl)
    for item in walk_descriptors(config):
        if item not in resources: resources.append(item)
    if 'selection' in config: selection(read_json(check_descriptor(config['selection'])['file']))
    if 'operation' in config:
        op = operation(read_json(check_descriptor(config['operation'])['file']))
        for item in op['external_inputs']:
            if item not in resources: resources.append(item)
    if 'witness' in config: motion.witness(read_json(check_descriptor(config['witness'])['file']))
    check_descriptors(resources)
    return {'schema_version':'1.0','command':'extension.run','params':{
        'manifest':{'script':roles['entry'],'trust':'execute_local_python_without_sandbox','params':config,'outputs':outputs},'resources':resources}}


def public_record(record, roles, impl, config, outputs):
    actual = host.verify_public_record(record)
    require(actual == request(roles,impl,config,outputs), 'Exact derivation public request differs', 'CONFLICT')
    result = read_json(record['result']['file']); data = result['data']
    require(result.get('error') is None and record['cli_version'] == result.get('cli_version')
            and data.get('script_sha256_observed') == roles['entry']['expected_sha256'], 'Public derivation result identity differs', 'CONFLICT')
    rows = data['outputs']
    require(len(rows) == len(outputs) and {Path(row['file']).name for row in rows} == set(outputs), 'Derivation output closure differs')
    for row in rows:
        require(not Path(row['file']).is_symlink() and Path(row['file']).stat().st_size == row['bytes'], 'Output bytes/symlink differ')
    for item in actual['params']['resources']:
        require(data.get('input_hashes',{}).get(item['file']) == item['expected_sha256'], 'Pinned derivation input differs', 'CONFLICT')
    return motion.read_report(record['outputs'][REPORT]['file'])


def producer_config(mode, inputs):
    return {'mode':mode,'input':inputs['source'], **{key:inputs[key] for key in ('selection','operation') if key in inputs}}


def cold_config(producer, inputs):
    return {'mode':'cold','input':producer['outputs'][CANDIDATE], 'producer_report':producer['outputs'][REPORT], 'operation':inputs['operation']}


def header(report, record, mode, roles, impl):
    require(report.get('schema') == REPORT_SCHEMA and report.get('mode') == mode
            and report.get('entry_sha') == roles['entry']['expected_sha256'] and report.get('implementation') == impl,
            'Derivation native header differs', 'CONFLICT')
    require(record['worker_result'].get('mode') == mode and record['worker_result'].get('status') == report.get('status'),
            'Derivation public/native status differs', 'CONFLICT')


def verification(report, req, baseline):
    import derivation_state as state
    combined = canonical('combined_request', req)
    require(report.get('request') == req and report.get('combined_request') == combined, 'Derived request/combined declaration differs', 'CONFLICT')
    state.check_verification(req,combined,report['derivation_verification'],baseline)
    require(report.get('original_action_before') == baseline and report.get('original_action_after') == report['derivation_verification']['original_action_after'],
            'Original Action complete state differs', 'CONFLICT')
    return combined


def check_producer(report, record, roles, impl, mode, inputs):
    header(report,record,mode,roles,impl)
    require(report.get('input') == inputs['source'] and report.get('source_unchanged') is True and report.get('source_saved') is False,
            'Original derivation source changed', 'CONFLICT')
    if mode == 'inspect':
        picked = selection(read_json(inputs['selection']['file']))
        inspected = report.get('inspection', {})
        require(report.get('status') == 'pass' and report.get('selection') == picked
                and report.get('selection_descriptor') == inputs['selection'] and inspected.get('ok') is True
                and set(inspected.get('fields',{})) == set(picked['bones'])
                and inspected['snapshot']['base_clip'] == canonical('canonical_base',picked['base_clip'])
                and inspected['snapshot']['action_name'] == picked['action_name']
                and canonical('digest',inspected['snapshot']) == inspected['snapshot_sha256']
                and report['before_snapshot'] == report['after_snapshot'], 'Derivation inspect evidence differs')
        return
    op = operation(read_json(inputs['operation']['file'])); req = canonical('canonical_request',op['request'])
    snapshot = report['original_snapshot']
    require(report.get('status') == 'candidate_waiting_independent_cold' and report.get('operation_ok') is True
            and report.get('candidate') == record['outputs'][CANDIDATE] and report.get('operation') == op
            and report.get('operation_descriptor') == inputs['operation'], 'Derivation producer is not an exact candidate', 'VALIDATION_FAILED')
    require(canonical('digest',snapshot) == req['expected_snapshot_sha256'] and snapshot['base_clip'] == req['base_clip']
            and snapshot['action_name'] == req['action_name'], 'Derivation native snapshot differs', 'CONFLICT')
    verification(report,req,snapshot['original_action'])
    require(report.get('authoring_protection',{}).get('ok') is True and report['authoring_protection'].get('changes') == [],
            'Derivation non-target authoring changed', 'VALIDATION_FAILED')
    writer = report.get('writer',{})
    require(['Action',req['base_clip']['action_name']] in [list(row) for row in writer.get('runtime_extra_user_temporarily_modified',[])]
            and writer.get('fake_user_modified') is False and writer.get('runtime_bookkeeping_restored') is True,
            'Zero-user original Action has no actual retention evidence', 'VALIDATION_FAILED')


def check_cold(report, record, producer_report, roles, impl, inputs, producer):
    header(report,record,'cold',roles,impl)
    require(report.get('status') == 'pass' and report.get('operation_ok') is True and report.get('source_unchanged') is True
            and report.get('source_saved') is False and report.get('context_restored') is True,
            'Independent derivation cold failed', 'VALIDATION_FAILED')
    require(report.get('input') == producer['outputs'][CANDIDATE] and report.get('producer_report') == producer['outputs'][REPORT]
            and report.get('operation_descriptor') == inputs['operation'] and report.get('after_snapshot') == producer_report['after_snapshot'],
            'Cold candidate/producer/storage differs', 'CONFLICT')
    verification(report,producer_report['request'],producer_report['original_action_before'])
    require(report['derivation_verification'] == producer_report['derivation_verification']
            and report.get('evaluation_comparison',{}).get('ok') is True and report['evaluation_comparison'].get('pose_exact') is True,
            'Cold original/new Action or native evaluation differs', 'VALIDATION_FAILED')


def check_bundle(value):
    fields(value,('schema','frontend_version','status','mode','identity','implementation','inputs','producer_config','producer','report','verification','created_utc','GUI','FPS','limitations'),
           ('cold','candidate','combined_request','validated_bundle'),label='derivation bundle')
    require(value['schema'] == SCHEMA and value['frontend_version'] == VERSION and value['status'] == 'succeeded'
            and value['mode'] in ('inspect','create'), 'Expected current succeeded derivation bundle')
    roles,impl,mode,inputs = value['identity'],value['implementation'],value['mode'],value['inputs']
    unchanged(roles,impl)
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and value['limitations'] == LIMITATIONS,'Derivation scope differs')
    for _ in walk_descriptors(value): pass
    fields(inputs,('source','selection') if mode == 'inspect' else ('source','operation'),label='derivation inputs')
    source = motion.source(inputs['source']['file'],inputs['source']['expected_sha256'])
    config = producer_config(mode,inputs)
    require(source == inputs['source'] and value['producer_config'] == config,'Producer source/config differs', 'CONFLICT')
    outputs = [REPORT] if mode == 'inspect' else [REPORT,CANDIDATE]
    report = public_record(value['producer'],roles,impl,config,outputs)
    check_producer(report,value['producer'],roles,impl,mode,inputs)
    require(value['report'] == value['producer']['outputs'][REPORT], 'Producer report identity differs')
    if mode == 'inspect':
        require('cold' not in value and 'candidate' not in value and 'combined_request' not in value and value['verification'] == report,'Inspect may not contain a candidate')
    else:
        require(value.get('candidate') == value['producer']['outputs'][CANDIDATE] and value['cold']['job_id'] != value['producer']['job_id'], 'Candidate/cold identity differs')
        cold = public_record(value['cold'],roles,impl,cold_config(value['producer'],inputs),[REPORT])
        check_cold(cold,value['cold'],report,roles,impl,inputs,value['producer'])
        require(value['verification'] == cold and value.get('combined_request') == report['combined_request'],'Bundle combined/cold differs')
    return value


def check_replay_bundle(value):
    fields(value,('schema','frontend_version','status','mode','identity','implementation','inputs','config','record','report','verification','source','combined_request','created_utc','GUI','FPS','limitations'),label='derivation replay bundle')
    require(value['schema'] == REPLAY_SCHEMA and value['frontend_version'] == VERSION and value['status'] == 'succeeded' and value['mode'] == 'replay','Invalid derivation replay bundle')
    unchanged(value['identity'],value['implementation'])
    for _ in walk_descriptors(value): pass
    fields(value['inputs'],('bundle','witness'),label='derivation replay inputs')
    prior = check_bundle(motion.read_report(value['inputs']['bundle']['file']))
    require(prior['mode'] == 'create', 'Replay requires independently accepted derived Action')
    picked = motion.witness(read_json(value['inputs']['witness']['file']))
    config = {**cold_config(prior['producer'],prior['inputs']),'mode':'replay','witness':value['inputs']['witness'],'source_bundle':value['inputs']['bundle']}
    require(value['config'] == config and value['source'] == prior['candidate'] and value['combined_request'] == prior['combined_request'],'Replay source/config differs')
    report = public_record(value['record'],value['identity'],value['implementation'],config,[REPORT])
    header(report,value['record'],'replay',value['identity'],value['implementation'])
    producer = motion.read_report(prior['report']['file'])
    require(report.get('status') == 'pass' and report.get('source_unchanged') is True and report.get('source_saved') is False
            and report.get('context_restored') is True and report.get('input') == prior['candidate']
            and report.get('producer_report') == prior['report'] and report.get('source_bundle') == value['inputs']['bundle']
            and report.get('witness') == picked and report.get('after_snapshot') == prior['verification']['after_snapshot'], 'Replay preservation or exact inputs differ')
    verification(report,producer['request'],producer['original_action_before'])
    require(report['derivation_verification'] == producer['derivation_verification'], 'Replay frame0 original/new Action changed')
    checks = report.get('derivation_checks')
    expected_state = canonical('digest',producer['derivation_verification']['state'])
    expected_old = canonical('digest',producer['original_action_after'])
    require(type(checks) is list and [row.get('frame') for row in checks] == picked['frames']
            and all(row.get('ok') is True and row.get('derived_state_sha256') == expected_state
                    and row.get('original_state_sha256') == expected_old for row in checks),'Replay current-declaration checks differ')
    motion.check_replay_samples(report,picked,prior['combined_request'])
    require(value['report'] == value['record']['outputs'][REPORT] and value['verification'] == report
            and value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and value['limitations'] == LIMITATIONS,'Replay artifact/scope differs')
    return value


def parser():
    p=host.Parser(description=__doc__); commands=p.add_subparsers(dest='command',required=True,parser_class=host.Parser)
    commands.add_parser('describe')
    inspect=commands.add_parser('inspect'); create=commands.add_parser('create'); validate=commands.add_parser('validate'); replay=commands.add_parser('replay')
    for command in (inspect,create): command.add_argument('--source',required=True); command.add_argument('--expected-sha256',required=True)
    inspect.add_argument('--selection',required=True); inspect.add_argument('--expected-selection-sha256',required=True)
    create.add_argument('--operation',required=True); create.add_argument('--expected-operation-sha256',required=True)
    for command in (validate,replay): command.add_argument('--bundle',required=True); command.add_argument('--expected-sha256',required=True)
    replay.add_argument('--witness',required=True); replay.add_argument('--expected-witness-sha256',required=True)
    for command in (inspect,create,validate,replay): command.add_argument('--out-dir',required=True); command.add_argument('--timeout',type=float,default=900)
    return p


def preflight(args):
    number(args.timeout,'timeout',1,86400); inputs={}; prior=None
    if args.command in ('inspect','create'):
        inputs['source']=motion.source(args.source,args.expected_sha256)
        key='selection' if args.command == 'inspect' else 'operation'
        inputs[key]=descriptor(getattr(args,key),getattr(args,'expected_'+key+'_sha256'))
        (selection if key == 'selection' else operation)(read_json(inputs[key]['file']))
    else:
        inputs['bundle']=descriptor(args.bundle,args.expected_sha256)
        prior=check_bundle(motion.read_report(inputs['bundle']['file']))
        require(prior['mode'] == 'create','Validate/replay requires a derived candidate, not inspect')
        if args.command == 'replay':
            inputs['witness']=descriptor(args.witness,args.expected_witness_sha256)
            motion.witness(read_json(inputs['witness']['file']))
    return inputs,prior


def execute(args,run,inputs,prior):
    roles,impl=identity(),implementation()
    if args.command == 'replay':
        config={**cold_config(prior['producer'],prior['inputs']),'mode':'replay','witness':inputs['witness'],'source_bundle':inputs['bundle']}
        record=host.public_job(run,'replay',request(roles,impl,config,[REPORT]),args.timeout)
        report=public_record(record,roles,impl,config,[REPORT])
        final={'schema':REPLAY_SCHEMA,'frontend_version':VERSION,'status':'succeeded','mode':'replay','identity':roles,'implementation':impl,
               'inputs':inputs,'config':config,'record':record,'report':record['outputs'][REPORT],'verification':report,'source':prior['candidate'],
               'combined_request':prior['combined_request'],'created_utc':datetime.now(timezone.utc).isoformat(),
               'GUI':'deferred_by_user','FPS':'not_measured','limitations':LIMITATIONS}
        check_replay_bundle(final)
    else:
        if prior is None:
            mode=args.command; config=producer_config(mode,inputs); outputs=[REPORT] if mode == 'inspect' else [REPORT,CANDIDATE]
            producer=host.public_job(run,'producer',request(roles,impl,config,outputs),args.timeout)
            report=public_record(producer,roles,impl,config,outputs); check_producer(report,producer,roles,impl,mode,inputs)
            final={'schema':SCHEMA,'frontend_version':VERSION,'status':'succeeded','mode':mode,'identity':roles,'implementation':impl,'inputs':inputs,
                   'producer_config':config,'producer':producer,'report':producer['outputs'][REPORT],'verification':report,
                   'created_utc':datetime.now(timezone.utc).isoformat(),'GUI':'deferred_by_user','FPS':'not_measured','limitations':LIMITATIONS}
        else:
            final={**prior,'validated_bundle':inputs['bundle'],'created_utc':datetime.now(timezone.utc).isoformat()}
            inputs,mode,producer=prior['inputs'],prior['mode'],prior['producer']
            report=motion.read_report(prior['report']['file'])
        if mode == 'create':
            config=cold_config(producer,inputs); cold=host.public_job(run,'cold',request(roles,impl,config,[REPORT]),args.timeout)
            observed=public_record(cold,roles,impl,config,[REPORT]); check_cold(observed,cold,report,roles,impl,inputs,producer)
            final.update(cold=cold,candidate=producer['outputs'][CANDIDATE],combined_request=report['combined_request'],verification=observed)
        check_bundle(final)
    unchanged(roles,impl); host.write_new(run/'bundle.json',final)
    return {'ok':True,'status':'succeeded','mode':final['mode'],'bundle':descriptor(run/'bundle.json'),'report':final['report'],
            'candidate':final.get('candidate'),'combined_request':final.get('combined_request'),
            'operation_ok':final['verification'].get('operation_ok'),'source_saved':False,
            'producer_job_id':final.get('producer',{}).get('job_id'),'cold_job_id':final.get('cold',{}).get('job_id'),
            'job_id':final.get('record',{}).get('job_id'),'GUI':'deferred_by_user','FPS':'not_measured'}


def describe():
    return {'schema':'hrig.motion-derivation.describe.v1','frontend_version':VERSION,'commands':['describe','inspect','create','validate','replay'],
            'selection_schema':SELECTION_SCHEMA,'operation_schema':OP_SCHEMA,'request_schema':'hrig.motion-clip.derive.v1',
            'bundle_schema':SCHEMA,'report_schema':REPORT_SCHEMA,'replay_bundle_schema':REPLAY_SCHEMA,
            'backend':'existing public extension.run','native_validation':'not_asserted_by_discovery','limitations':LIMITATIONS}


def main(argv=None):
    run=None
    try:
        args=parser().parse_args(argv)
        if args.command == 'describe': response=describe()
        else:
            inputs,prior=preflight(args); identity(); implementation(); run=host.new_run_dir(args.out_dir)
            response=execute(args,run,inputs,prior)
        print(json.dumps(response,ensure_ascii=False,allow_nan=False)); return 0
    except (Exception,KeyboardInterrupt) as error:
        code=error.code if isinstance(error,ContractError) else 'CANCELLED' if isinstance(error,KeyboardInterrupt) else 'TIMEOUT' if isinstance(error,subprocess.TimeoutExpired) else 'IO_ERROR' if isinstance(error,OSError) else 'INVALID_REQUEST' if isinstance(error,(ValueError,KeyError,TypeError)) else 'WORKER_FAILED'
        failure={'ok':False,'status':'failed','error':{'code':code,'message':str(error)},'run_directory':str(run) if run else None,
                 'candidate_policy':'no succeeded bundle; unverified native candidate remains diagnostic only'}
        if run is not None:
            try: host.write_new(run/'failure.json',failure)
            except OSError: pass
        print(json.dumps(failure,ensure_ascii=False,allow_nan=False)); return error.exit_code if isinstance(error,ContractError) else CODES[code]


if __name__ == '__main__': raise SystemExit(main())
