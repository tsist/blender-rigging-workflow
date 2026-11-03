"""Pure host dynamics candidate frontend; only existing public extension.run.

Independent cold jobs certify bounded pose/storage evidence, not complete
character preservation, visual deformation, GUI playback or performance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
import rigctl as host
import motion_cli as motion
from host_contract import (CODES, ContractError, check_descriptor, check_descriptors,
                           context, descriptor, fields, number, read_json, require,
                           text, walk_descriptors)

SCHEMA = 'hrig.dynamics.bundle.v1'
VERSION = '0.9.0'
ROOT, SCRIPTS, ADDON, PROJECT = host.ROOT, host.SCRIPTS, host.ADDON, host.PROJECT
OBSERVER = PROJECT / 'tools' / 'blenderctl'
REPORT = 'output-dynamics.json'
CANDIDATE = 'output-candidate.blend'
SELECTION_SCHEMA = 'hrig.cloth-quality.selection.v1'
OP_SCHEMA = 'hrig.cloth-quality.operation.v1'
LIMITATIONS = [
    'partial: exact local Cloth.settings.quality scalar only, bounded integer1..80; no collision/cache/switch/animation edits',
    'all original Action/AnimData, pose/rest/constraints/drivers/weights/body geometry remain strictly protected',
    'identified native readers/writers of selected quality paths are refused; unrelated drivers remain protected',
    'independent cold certifies declared storage and frame0 evaluated pose/mesh equality only',
    'replay is fresh-process sequential recomputation with complete coordinates; unbaked runtime cache is not saved',
    'frame timing is background stage wall time, not viewport FPS; no collision/continuous/visual quality claim',
    'explicit static inputs required; inherited missing weak references remain unresolved',
    'GUI deferred by user; complete improved Aon not claimed; no installation/publication',
    'trusted local Python without OS sandbox; SAVED_DISK_V1 cannot observe unsaved GUI edits']



def replay_config(prior, inputs):
    return {**cold_config(prior['producer'], prior['inputs'].get('operation')), 'mode': 'replay',
            'source_bundle': inputs['bundle'], 'witness': inputs['witness'], 'clip_request': inputs['clip_request']}


def check_replay_report(report, picked, clip_request, prior, record):
    require(report.get('schema') == 'hrig.dynamics.report.v1' and report.get('mode') == 'replay' and
            report.get('status') == 'pass' and report.get('source_unchanged') is True and
            report.get('source_saved') is False and report.get('context_restored') is True,
            'Dynamics replay did not pass as read-only fresh sequential evaluation', 'VALIDATION_FAILED')
    require(record['worker_result'].get('mode') == 'replay' and record['worker_result'].get('status') == 'pass',
            'Dynamics replay public worker result differs')
    require(report.get('implementation') == implementation() and report.get('entry_sha') == identity()['entry']['expected_sha256'],
            'Dynamics replay implementation/entry differs', 'CONFLICT')
    require(report.get('witness') == picked and report.get('input') == prior['candidate'] and
            report.get('after_snapshot') == prior['verification']['after_snapshot'] and
            report.get('clip_request') == clip_request, 'Dynamics replay source/author state/clip request binding differs', 'CONFLICT')
    require(report.get('quality_validation', {}).get('ok') is True and report.get('clip_verification', {}).get('ok') is True,
            'Dynamics replay lacks exact original Action and quality validation')
    require(report['quality_validation']['snapshot'] == prior['verification']['quality_validation']['snapshot'],
            'Dynamics replay quality and protected native settings differ from accepted cold evidence', 'CONFLICT')
    motion.check_replay_samples(report, picked, clip_request)
    timing = report.get('frame_timings')
    require(type(timing) is list and [r.get('frame') for r in timing] == picked['frames'], 'Complete frame timing coverage differs')
    for row in timing:
        fields(row, ('frame', 'frame_set_update_first_eval_seconds', 'coordinate_capture_seconds'), label='background frame timing')
        number(row['frame_set_update_first_eval_seconds'], 'frame evaluation seconds', 0)
        number(row['coordinate_capture_seconds'], 'coordinate capture seconds', 0)
    return report


def check_replay_bundle(value):
    fields(value, ('schema', 'frontend_version', 'status', 'mode', 'identity', 'implementation', 'inputs',
                   'source', 'config', 'record', 'report', 'verification', 'GUI', 'FPS', 'limitations', 'created_utc'),
           label='dynamics replay bundle')
    require(value['schema'] == 'hrig.dynamics.replay.bundle.v1' and value['frontend_version'] == VERSION and
            value['status'] == 'succeeded' and value['mode'] == 'replay', 'Expected succeeded dynamics replay bundle')
    unchanged(value['identity'], value['implementation'])
    fields(value['inputs'], ('bundle', 'witness', 'clip_request'), label='dynamics replay inputs')
    for _ in walk_descriptors(value):
        pass
    prior = check_bundle(read_json(value['inputs']['bundle']['file']))
    require(prior['mode'] != 'inspect' and prior['verification'].get('operation_ok') is True, 'Quality replay requires accepted candidate')
    expected = replay_config(prior, value['inputs'])
    require(value['config'] == expected and value['source'] == prior['candidate'], 'Quality replay config/source differs', 'CONFLICT')
    report = public_record(value['record'], value['identity'], value['implementation'], expected, [REPORT])
    require(value['report'] == value['record']['outputs'][REPORT] and value['verification'] == report,
            'Quality replay verification is not its exact public artifact', 'CONFLICT')
    require(report.get('source_bundle') == value['inputs']['bundle'] and report.get('witness_descriptor') == value['inputs']['witness'] and
            report.get('clip_request_descriptor') == value['inputs']['clip_request'], 'Quality replay resource descriptors differ', 'CONFLICT')
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and value['limitations'] == LIMITATIONS,
            'Dynamics replay limitations differ')
    picked = motion.witness(read_json(value['inputs']['witness']['file']))
    clip = motion.canonical('canonical_request', read_json(value['inputs']['clip_request']['file']))
    check_replay_report(report, picked, clip, prior, value['record'])
    return value


def execute_replay(args, run_dir, inputs, prior, roles, impl):
    config = replay_config(prior, inputs)
    record = host.public_job(run_dir, 'replay', request(roles, impl, config, [REPORT]), args.timeout)
    evidence = public_record(record, roles, impl, config, [REPORT])
    picked = motion.witness(read_json(inputs['witness']['file']))
    clip = motion.canonical('canonical_request', read_json(inputs['clip_request']['file']))
    check_replay_report(evidence, picked, clip, prior, record)
    unchanged(roles, impl)
    final = {'schema': 'hrig.dynamics.replay.bundle.v1', 'frontend_version': VERSION, 'status': 'succeeded',
             'mode': 'replay', 'identity': roles, 'implementation': impl, 'inputs': inputs,
             'source': prior['candidate'], 'config': config, 'record': record, 'report': record['outputs'][REPORT],
             'verification': evidence, 'GUI': 'deferred_by_user', 'FPS': 'not_measured', 'limitations': LIMITATIONS,
             'created_utc': datetime.now(timezone.utc).isoformat()}
    check_replay_bundle(final)
    host.write_new(run_dir/'bundle.json', final)
    return {'ok': True, 'status': 'succeeded', 'mode': 'replay', 'bundle': descriptor(run_dir/'bundle.json'),
            'report': final['report'], 'source_saved': False, 'job_id': record['job_id'], 'GUI': final['GUI'], 'FPS': final['FPS']}


def fixture_cases():
    path = SCRIPTS / 'dynamics_fixture.py'
    require(path.is_file(), 'Dynamics fixture source missing', 'NOT_FOUND')
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CASE_IDS' for t in item.targets):
            values = ast.literal_eval(item.value)
            require(type(values) in (tuple, list) and len(values) > 0 and
                    all(type(x) is str and x for x in values) and len(set(values)) == len(values),
                    'Invalid static dynamics fixture CASE_IDS')
            return tuple(values)
    raise ContractError('UNSUPPORTED', 'No static CASE_IDS in dynamics fixture')


def dynamics_contract():
    path = ADDON / 'humanoid_rigging_probe' / 'dynamics_contract.py'
    require(path.is_file(), 'Dynamics contract source missing', 'NOT_FOUND')
    spec = importlib.util.spec_from_file_location('_hrig_host_dynamics_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(method, *values):
    core = dynamics_contract()
    try:
        return getattr(core, method)(*values)
    except ValueError as error:
        raise ContractError('INVALID_REQUEST', getattr(error, 'code', 'INVALID_REQUEST') + ': ' + str(error)) from error


def selection(value):
    fields(value, ('schema', 'context', 'rig', 'targets'), label='dynamics selection')
    require(value['schema'] == SELECTION_SCHEMA, 'Expected ' + SELECTION_SCHEMA)
    motion.motion_context(value['context'])
    text(value['rig'], 'rig')
    canonical('canonical_targets', value['targets'])
    return value


def parser():
    p = host.Parser(description='Isolated dynamics candidate CLI; backend: existing public extension.run')
    commands = p.add_subparsers(dest='command', required=True, parser_class=host.Parser)
    commands.add_parser('describe', help='host discovery; never executes Blender')
    i = commands.add_parser('inspect', help='read-only saved controller-dynamics inspection')
    i.add_argument('--source', required=True)
    i.add_argument('--expected-sha256', required=True)
    i.add_argument('--selection', required=True)
    i.add_argument('--expected-selection-sha256', required=True)
    a = commands.add_parser('apply', help='exact selected rotation request plus independent cold validation')
    a.add_argument('--source', required=True)
    a.add_argument('--expected-sha256', required=True)
    a.add_argument('--operation', required=True)
    a.add_argument('--expected-operation-sha256', required=True)
    f = commands.add_parser('fixture', help='one explicit fixture plus independent cold')
    f.add_argument('--case', required=True)
    v = commands.add_parser('validate', help='new independent cold for an immutable candidate bundle')
    v.add_argument('--bundle', required=True)
    v.add_argument('--expected-sha256', required=True)
    r = commands.add_parser('replay', help='read-only quality candidate sequential replay')
    r.add_argument('--bundle', required=True)
    r.add_argument('--expected-sha256', required=True)
    r.add_argument('--clip-request', required=True)
    r.add_argument('--expected-clip-request-sha256', required=True)
    r.add_argument('--witness', required=True)
    r.add_argument('--expected-witness-sha256', required=True)
    for command in (i, a, f, v, r):
        command.add_argument('--out-dir', required=True)
        command.add_argument('--timeout', type=float, default=180)
    return p

def operation(value):
    fields(value, ('schema', 'context', 'rig', 'request', 'external_inputs',
                   'inherited_missing_weak_references'), label='dynamics operation')
    require(value['schema'] == OP_SCHEMA, 'Expected ' + OP_SCHEMA)
    motion.motion_context(value['context'])
    text(value['rig'], 'rig')
    canonical('canonical_request', value['request'])
    check_descriptors(value['external_inputs'])
    missing = value['inherited_missing_weak_references']
    require(type(missing) is list and len(missing) <= 1024,
            'Explicit bounded inherited missing weak paths required')
    seen = set()
    for item in missing:
        require(type(item) is str and Path(item).is_absolute() and len(item) <= 4096,
                'Inherited missing path must be absolute')
        key = str(Path(item).resolve()).casefold()
        require(key not in seen and not Path(item).exists(),
                'Inherited missing path duplicated or now exists; re-inspect and pin existing file', 'CONFLICT')
        seen.add(key)
    return value

def implementation():
    paths = sorted((ADDON / 'humanoid_rigging_probe').glob('*.py'))
    require(paths and ADDON / 'humanoid_rigging_probe' / '__init__.py' in paths,
            'Candidate addon closure is missing', 'NOT_FOUND')
    paths += [SCRIPTS / 'character_state.py', SCRIPTS / 'dynamics_state.py',
              SCRIPTS / 'dynamics_fixture.py', SCRIPTS / 'motion_state.py', SCRIPTS / 'motion_cli.py',
              SCRIPTS / 'run_motion.py', SCRIPTS / 'run_weights.py', SCRIPTS / 'run_styles.py',
              OBSERVER / 'inspection.py', OBSERVER / 'protocol.py']
    return check_descriptors([descriptor(path) for path in paths])

def identity():
    paths = {'frontend': SCRIPTS / 'rigctl.py', 'dynamics_cli': Path(__file__),
             'host_contract': SCRIPTS / 'host_contract.py', 'entry': SCRIPTS / 'run_dynamics.py',
             'public_wrapper': host.PUBLIC, 'inspection': OBSERVER / 'inspection.py',
             'protocol': OBSERVER / 'protocol.py'}
    paths.update({p.stem: p for p in (SCRIPTS / 'character_state.py', SCRIPTS / 'dynamics_state.py',
                  SCRIPTS / 'dynamics_fixture.py', SCRIPTS / 'motion_state.py', SCRIPTS / 'motion_cli.py',
                  SCRIPTS / 'run_motion.py', SCRIPTS / 'run_weights.py', SCRIPTS / 'run_styles.py')})
    paths.update({'addon_' + p.stem: p for p in sorted((ADDON / 'humanoid_rigging_probe').glob('*.py'))})
    return {role: descriptor(path) for role, path in paths.items()}

def unchanged(roles, impl):
    require(roles == identity(), 'Dynamics frontend/entry/helper/public role identity changed', 'CONFLICT')
    require(impl == implementation(), 'Exact dynamics implementation closure changed', 'CONFLICT')


def request(roles, impl, config, outputs):
    config = {**config, 'addon_root': str(ADDON), 'observer_root': str(OBSERVER), 'implementation': impl}
    resources = list(impl)
    for item in walk_descriptors(config):
        if item not in resources:
            resources.append(item)
    if 'selection' in config:
        selection(read_json(check_descriptor(config['selection'])['file']))
    if 'operation' in config:
        loaded = operation(read_json(check_descriptor(config['operation'])['file']))
        # extension.run resources do not recursively discover descriptors inside
        # an operation JSON file. Pin each declared native static input explicitly
        # in both producer and independent cold public requests.
        for item in loaded['external_inputs']:
            if item not in resources:
                resources.append(item)
    check_descriptors(resources)
    return {'schema_version': '1.0', 'command': 'extension.run', 'params': {
        'manifest': {'script': roles['entry'], 'trust': 'execute_local_python_without_sandbox',
                     'params': config, 'outputs': outputs}, 'resources': resources}}


def check_request(value, roles, impl, config, outputs):
    require(value == request(roles, impl, config, outputs),
            'Public dynamics request differs from exact role/config/resource/output contract', 'CONFLICT')
    check_descriptors(value['params']['resources'])


def public_record(record, roles, impl, config, outputs):
    value = host.verify_public_record(record)
    check_request(value, roles, impl, config, outputs)
    result = read_json(record['result']['file'])
    data = result['data']
    require(Path(record['result']['file']).name == 'result.json' and result.get('error') is None,
            'Unexpected public result identity/error', 'CONFLICT')
    require(record['cli_version'] == result.get('cli_version') and
            data.get('script_sha256_observed') == roles['entry']['expected_sha256'],
            'Public CLI version/entry SHA evidence differs', 'CONFLICT')
    rows = data['outputs']
    require(type(rows) is list and len(rows) == len(outputs) and
            {Path(row['file']).name for row in rows} == set(outputs),
            'Public output set differs', 'CONFLICT')
    for row in rows:
        path = Path(row['file'])
        require(not path.is_symlink() and path.stat().st_size == row['bytes'],
                'Public output identity/byte count differs', 'CONFLICT')
    for doc in value['params']['resources']:
        require(data.get('input_hashes', {}).get(doc['file']) == doc['expected_sha256'],
                'Public pinned resource hash evidence differs: ' + doc['file'], 'CONFLICT')
    return motion.read_report(record['outputs'][REPORT]['file'])


def source(value, expected):
    result = descriptor(value, expected)
    require(Path(result['file']).suffix.lower() == '.blend', 'Source must be a .blend')
    return result


def producer_config(mode, inputs, case=None):
    config = {'mode': mode}
    if mode in ('inspect', 'apply'):
        config['input'] = inputs['source']
    if mode == 'apply':
        config['operation'] = inputs['operation']
    elif mode == 'inspect':
        config['selection'] = inputs['selection']
    elif mode == 'fixture':
        config['case'] = case
    return config

def cold_config(producer, operation_desc=None):
    config = {'mode': 'cold', 'input': producer['outputs'][CANDIDATE],
              'producer_report': producer['outputs'][REPORT]}
    if operation_desc is not None:
        config['operation'] = operation_desc
    return config


def check_producer(report, producer, roles, impl, mode):
    require(type(report) is dict and report.get('schema') == 'hrig.dynamics.report.v1',
            'Unexpected dynamics producer report schema', 'CONFLICT')
    require(report.get('implementation') == impl and
            report.get('entry_sha') == roles['entry']['expected_sha256'],
            'Producer implementation/entry binding differs', 'CONFLICT')
    require(report.get('mode') == mode and type(producer.get('worker_result')) is dict and
            producer['worker_result'].get('mode') == mode and
            producer['worker_result'].get('status') == report.get('status'),
            'Producer native report/public result mode or status differs', 'CONFLICT')
    if mode == 'inspect':
        require(report.get('status') == 'pass' and report.get('source_unchanged') is True,
                'Read-only dynamics inspection did not pass unchanged-source check', 'VALIDATION_FAILED')
    else:
        require(report.get('status') == 'candidate_waiting_independent_cold' and
                report.get('candidate') == producer['outputs'][CANDIDATE],
                'Dynamics producer is not paired with its candidate', 'VALIDATION_FAILED')
        if mode == 'apply':
            require(report.get('operation_ok') is True,
                    'Requested apply was not applied successfully', 'VALIDATION_FAILED')
        else:
            require(type(report.get('operation_ok')) is bool,
                    'Fixture report must explicitly distinguish operation success/refusal', 'VALIDATION_FAILED')


def check_cold(report, record):
    require(type(report) is dict and report.get('schema') == 'hrig.dynamics.report.v1',
            'Unexpected dynamics cold report schema', 'CONFLICT')
    require(report.get('status') == 'pass' and report.get('source_unchanged') is True and
            type(record['worker_result']) is dict and record['worker_result'].get('status') == 'pass',
            'Independent dynamics cold validation did not pass', 'VALIDATION_FAILED')
    require(report.get('mode') == 'cold' and record['worker_result'].get('mode') == 'cold' and
            report.get('implementation') == implementation() and
            report.get('entry_sha') == identity()['entry']['expected_sha256'],
            'Independent cold native identity/mode differs', 'CONFLICT')
    require(report.get('evaluation_comparison', {}).get('ok') is True and
            report.get('evaluation_comparison', {}).get('pose_exact') is True,
            'Independent cold lacks native evaluated comparison', 'VALIDATION_FAILED')


def check_bundle(value):
    fields(value, ('schema', 'frontend_version', 'status', 'mode', 'identity', 'implementation',
                   'inputs', 'producer_config', 'producer', 'report', 'verification', 'created_utc',
                   'GUI', 'FPS', 'limitations'),
           ('cold', 'candidate', 'validated_bundle'), 'dynamics bundle')
    require(value['schema'] == SCHEMA and value['frontend_version'] == VERSION and
            value['status'] == 'succeeded' and value['mode'] in ('inspect', 'apply', 'fixture'),
            'Expected immutable succeeded dynamics bundle')
    roles, impl, mode = value['identity'], value['implementation'], value['mode']
    unchanged(roles, impl)
    for _ in walk_descriptors(value):
        pass
    inputs, config = value['inputs'], value['producer_config']
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and
            value['limitations'] == LIMITATIONS, 'Bundle limitations or deferred GUI/FPS changed', 'CONFLICT')
    if mode in ('inspect', 'apply'):
        check_descriptor(inputs.get('source'))
        source(inputs['source']['file'], inputs['source']['expected_sha256'])
    if mode == 'apply':
        fields(inputs, ('source', 'operation'), label='apply bundle inputs')
        operation(read_json(inputs['operation']['file']))
        expected_config = producer_config(mode, inputs)
    elif mode == 'fixture':
        fields(inputs, (), label='fixture bundle inputs')
        require(config.get('case') in fixture_cases(), 'Invalid fixture bundle case')
        expected_config = producer_config(mode, inputs, case=config['case'])
    else:
        fields(inputs, ('source', 'selection'), label='inspect bundle inputs')
        selection(read_json(inputs['selection']['file']))
        expected_config = producer_config(mode, inputs)
    require(config == expected_config, 'Producer config is not bound to bundle inputs', 'CONFLICT')
    outputs = [REPORT] if mode == 'inspect' else [REPORT, CANDIDATE]
    producer_report = public_record(value['producer'], roles, impl, config, outputs)
    check_producer(producer_report, value['producer'], roles, impl, mode)
    require(value['report'] == value['producer']['outputs'][REPORT],
            'Bundle report is not the producer artifact', 'CONFLICT')
    if mode == 'inspect':
        require('candidate' not in value and 'cold' not in value and
                value['verification'] == producer_report,
                'Inspect bundle may contain only read-only inspection evidence', 'CONFLICT')
    else:
        require(value.get('candidate') == value['producer']['outputs'][CANDIDATE] and 'cold' in value,
                'Candidate is not the producer artifact', 'CONFLICT')
        cold = value['cold']
        require(cold['job_id'] != value['producer']['job_id'],
                'Cold job must be independent of the producer', 'CONFLICT')
        cold_report = public_record(cold, roles, impl,
                                    cold_config(value['producer'], inputs.get('operation')), [REPORT])
        check_cold(cold_report, cold)
        require(value['verification'] == cold_report, 'Bundle cold evidence differs', 'CONFLICT')
    return value


def preflight(args):
    if args.command == 'describe':
        return None
    number(args.timeout, 'timeout', 1, 86400)
    inputs = {}
    if args.command in ('inspect', 'apply'):
        inputs['source'] = source(args.source, args.expected_sha256)
    if args.command == 'apply':
        inputs['operation'] = descriptor(args.operation, args.expected_operation_sha256)
        operation(read_json(inputs['operation']['file']))
    elif args.command == 'inspect':
        inputs['selection'] = descriptor(args.selection, args.expected_selection_sha256)
        selection(read_json(inputs['selection']['file']))
    elif args.command == 'fixture':
        require(args.case in fixture_cases(), 'Unknown dynamics fixture case')
    elif args.command in ('validate', 'replay'):
        inputs['bundle'] = descriptor(args.bundle, args.expected_sha256)
        prior = check_bundle(read_json(inputs['bundle']['file']))
        require(prior['mode'] != 'inspect',
                'Read-only inspection bundles have no candidate; validate accepts only apply/fixture bundles',
                'UNSUPPORTED')
        if args.command == 'replay':
            require(prior['verification'].get('operation_ok') is True, 'Replay needs an accepted quality candidate')
            inputs['clip_request'] = descriptor(args.clip_request, args.expected_clip_request_sha256)
            motion.canonical('canonical_request', read_json(inputs['clip_request']['file']))
            inputs['witness'] = descriptor(args.witness, args.expected_witness_sha256)
            motion.witness(read_json(inputs['witness']['file']))
        return inputs, prior
    return inputs, None

def execute(args, run_dir, inputs, prior):
    roles, impl = identity(), implementation()
    if args.command == 'replay':
        return execute_replay(args, run_dir, inputs, prior, roles, impl)
    if prior is None:
        mode = args.command
        config = producer_config(mode, inputs, case=getattr(args, 'case', None))
        outputs = [REPORT] if mode == 'inspect' else [REPORT, CANDIDATE]
        req = request(roles, impl, config, outputs)
        check_request(req, roles, impl, config, outputs)
        producer = host.public_job(run_dir, 'producer', req, args.timeout)
        report = public_record(producer, roles, impl, config, outputs)
        check_producer(report, producer, roles, impl, mode)
        final = {'schema': SCHEMA, 'frontend_version': VERSION, 'status': 'succeeded',
                 'mode': mode, 'identity': roles, 'implementation': impl, 'inputs': inputs,
                 'producer_config': config, 'producer': producer, 'report': producer['outputs'][REPORT],
                 'verification': report, 'created_utc': datetime.now(timezone.utc).isoformat(),
                 'GUI': 'deferred_by_user', 'FPS': 'not_measured', 'limitations': LIMITATIONS}
    else:
        final = {**prior, 'validated_bundle': inputs['bundle'],
                 'created_utc': datetime.now(timezone.utc).isoformat()}
        mode, producer = prior['mode'], prior['producer']
        inputs = prior['inputs']
    unchanged(roles, impl)
    if mode != 'inspect':
        config = cold_config(producer, inputs.get('operation'))
        cold = host.public_job(run_dir, 'cold', request(roles, impl, config, [REPORT]), args.timeout)
        evidence = public_record(cold, roles, impl, config, [REPORT])
        check_cold(evidence, cold)
        final.update(cold=cold, candidate=producer['outputs'][CANDIDATE], verification=evidence)
    unchanged(roles, impl)
    check_bundle(final)
    host.write_new(run_dir / 'bundle.json', final)
    return {'ok': True, 'status': 'succeeded', 'mode': mode,
            'bundle': descriptor(run_dir / 'bundle.json'), 'report': final['report'],
            'candidate': final.get('candidate'), 'producer_job_id': producer['job_id'],
            'cold_job_id': final.get('cold', {}).get('job_id'),
            'operation_ok': read_json(final['report']['file']).get('operation_ok'),
            'GUI': final['GUI'], 'FPS': final['FPS']}


def describe():
    paths = {'entry': SCRIPTS / 'run_dynamics.py', 'character_state': SCRIPTS / 'character_state.py',
             'dynamics_state': SCRIPTS / 'dynamics_state.py', 'dynamics_fixture': SCRIPTS / 'dynamics_fixture.py',
             'host_dynamics_contract': ADDON / 'humanoid_rigging_probe' / 'dynamics_contract.py',
             'frontend': Path(__file__)}
    try:
        cases = {'status': 'available', 'values': list(fixture_cases())}
    except (ContractError, SyntaxError, ValueError) as error:
        cases = {'status': 'missing_or_invalid', 'values': [], 'message': str(error)}
    return {'schema': 'hrig.dynamics.cli.describe.v1', 'frontend_version': VERSION,
            'backend': 'existing public extension.run', 'public_dynamics_commands_added': False,
            'commands': ['describe', 'inspect', 'apply', 'fixture', 'validate', 'replay'],
            'fixture_cases': cases, 'operation_schema': OP_SCHEMA, 'selection_schema': SELECTION_SCHEMA,
            'request_schema': 'hrig.cloth-quality.patch.v1', 'bundle_schema': SCHEMA,
            'operation_fields': ['schema', 'context', 'rig', 'request', 'external_inputs',
                                 'inherited_missing_weak_references'],
            'selection_fields': ['schema', 'context', 'rig', 'targets'],
            'request_fields': ['schema', 'expected_snapshot_sha256', 'changes'],
            'change_fields': ['object', 'modifier', 'index', 'before', 'after'],
            'selected_quality_consumer_policy': {'native_reader': 'reject DYNAMICS_CONSUMER',
                                                 'native_writer': 'reject DYNAMICS_WRITER',
                                                 'unrelated_drivers': 'preserve',
                                                 'arbitrary_script_consumers': 'not_certified'},
            'external_inputs_contract': 'exact native static file descriptors and inherited missing weak origins checked independently; every existing file included as public resource',
            'context_fields': ['scene', 'view_layer', 'frame', 'subframe'],
            'inspect_validate': 'UNSUPPORTED: inspection has no candidate',
            'roles': {name: descriptor(path) if path.is_file() else {'file': str(path), 'status': 'missing'}
                      for name, path in paths.items()},
            'native_validation': 'not_asserted_by_discovery', 'limitations': LIMITATIONS}


def main(argv=None):
    run_dir = None
    try:
        args = parser().parse_args(argv)
        if args.command == 'describe':
            response = describe()
        else:
            inputs, prior = preflight(args)
            # Verify the current frozen closure before allocating a run directory.
            identity()
            implementation()
            run_dir = host.new_run_dir(args.out_dir)
            response = execute(args, run_dir, inputs, prior)
        print(json.dumps(response, ensure_ascii=False, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, ContractError):
            code = error.code
        elif isinstance(error, KeyboardInterrupt):
            code = 'CANCELLED'
        elif isinstance(error, subprocess.TimeoutExpired):
            code = 'TIMEOUT'
        elif isinstance(error, OSError):
            code = 'IO_ERROR'
        elif isinstance(error, (ValueError, KeyError, TypeError)):
            code = 'INVALID_REQUEST'
        else:
            code = 'WORKER_FAILED'
        failure = {'ok': False, 'status': 'failed', 'error': {'code': code, 'message': str(error)},
                   'run_directory': str(run_dir) if run_dir else None,
                   'candidate_policy': 'retained unverified/quarantined; no succeeded bundle'}
        if run_dir is not None:
            try:
                host.write_new(run_dir / 'failure.json', failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False, allow_nan=False))
        return error.exit_code if isinstance(error, ContractError) else CODES[code]


if __name__ == '__main__':
    raise SystemExit(main())
