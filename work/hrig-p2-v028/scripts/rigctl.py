"""Independent host CLI for pinned Blender rigging candidates.

Uses the existing public extension.run, never a fabricated rigging command.
Only a successful producer plus a separate successful cold job yields a bundle.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from host_contract import (BUNDLE_SCHEMA, CODES, FRONTEND_VERSION, NEGATIVE_CASES,
                           ContractError, check_descriptor, check_descriptors,
                           descriptor, fields, number, operation, operation_schemas,
                           parse_json, read_json, require, sha256, spec,
                           walk_descriptors)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
ADDON = ROOT / 'addon'
PROJECT = ROOT.parents[1]
PUBLIC = Path(os.environ['BLENDERCTL_ENTRY']).expanduser().resolve() if os.environ.get('BLENDERCTL_ENTRY') else PROJECT / 'blenderctl.ps1'
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ContractError('INVALID_REQUEST', message)


def parser():
    p = Parser(description='Independent candidate rig CLI; backend: public extension.run')
    commands = p.add_subparsers(dest='command', required=True, parser_class=Parser)
    pair = commands.add_parser('hair-pair', add_help=False, help='experimental front/rear branches preserving rest parents')
    pair.add_argument('pair_args', nargs=argparse.REMAINDER)
    hair = commands.add_parser('hair', add_help=False, help='experimental Aon front-hair branch with independent cold validation')
    hair.add_argument('hair_args', nargs=argparse.REMAINDER)
    subdiv = commands.add_parser('subdiv', add_help=False, help='single-Subsurf temporary causal diagnostic')
    subdiv.add_argument('subdivision_args', nargs=argparse.REMAINDER)
    diagnostic = commands.add_parser('diagnose', add_help=False, help='bounded background timing and response observation')
    diagnostic.add_argument('diagnostic_args', nargs=argparse.REMAINDER)
    derive = commands.add_parser('derive', add_help=False, help='derive a declared rotation clip while preserving its original Action')
    derive.add_argument('derive_args', nargs=argparse.REMAINDER)
    rebake = commands.add_parser('rebake', add_help=False, help='unbaked pose authoring and fresh cache workflow')
    rebake.add_argument('rebake_args', nargs=argparse.REMAINDER)
    cache = commands.add_parser('cache', add_help=False, help='selected native embedded Cloth cache candidates')
    cache.add_argument('cache_args', nargs=argparse.REMAINDER)
    dynamics = commands.add_parser('dynamics', add_help=False, help='isolated Cloth quality candidate frontend')
    dynamics.add_argument('dynamics_args', nargs=argparse.REMAINDER)
    settings = commands.add_parser('settings', add_help=False, help='isolated existing-control settings frontend')
    settings.add_argument('settings_args', nargs=argparse.REMAINDER)
    m = commands.add_parser('motion', add_help=False, help='isolated rotation clip author/replay frontend')
    m.add_argument('motion_args', nargs=argparse.REMAINDER)
    c = commands.add_parser('collections', add_help=False, help='isolated native bone-collection frontend')
    c.add_argument('collection_args', nargs=argparse.REMAINDER)
    s = commands.add_parser('styles', add_help=False, help='isolated controller-style template frontend')
    s.add_argument('style_args', nargs=argparse.REMAINDER)
    w = commands.add_parser('weights', add_help=False, help='isolated weight inspection/patch candidate frontend')
    w.add_argument('weight_args', nargs=argparse.REMAINDER)
    d = commands.add_parser('describe', help='pure host discovery; never launches Blender')
    d.add_argument('--operation', choices=tuple(operation_schemas()))
    d.add_argument('--schema', action='store_true')
    f = commands.add_parser('fixtures', help='run an explicit fixture and independent cold validation')
    f.add_argument('--family', choices=('controls', 'limb'), required=True)
    f.add_argument('--list', action='store_true')
    f.add_argument('--case')
    f.add_argument('--module-id')
    f.add_argument('--side', type=int, choices=(-1, 1))
    f.add_argument('--roll', type=float)
    f.add_argument('--scale', type=float)
    f.add_argument('--pose-index', type=int, choices=(0, 1, 2))
    f.add_argument('--out-dir')
    f.add_argument('--timeout', type=float, default=180)
    r = commands.add_parser('run', help='one exact operation JSON; candidate plus independent cold')
    r.add_argument('--operation', required=True)
    r.add_argument('--expected-operation-sha256', required=True)
    r.add_argument('--source')
    r.add_argument('--expected-sha256')
    r.add_argument('--out-dir', required=True)
    r.add_argument('--timeout', type=float, default=180)
    v = commands.add_parser('validate', help='new cold job for an immutable succeeded bundle')
    v.add_argument('--bundle', required=True)
    v.add_argument('--expected-sha256', required=True)
    v.add_argument('--out-dir', required=True)
    v.add_argument('--timeout', type=float, default=180)
    return p


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def write_new(path, value):
    raw = json_bytes(value)  # Serialize fully before exclusive creation.
    with Path(path).open('xb') as stream:
        stream.write(raw)


def write_log(path, text):
    with Path(path).open('x', encoding='utf-8') as stream:
        stream.write(text)


def controls_cases():
    tree = ast.parse((SCRIPTS / 'space_fixture.py').read_text(encoding='utf-8-sig'))
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CASE_IDS' for t in item.targets):
            values = ast.literal_eval(item.value)
            require(isinstance(values, (tuple, list)) and all(type(x) is str for x in values),
                    'Invalid static control fixture list')
            return list(values)
    raise ContractError('UNSUPPORTED', 'No static CASE_IDS in control fixture')


def implementation(family):
    paths = sorted((ADDON / 'humanoid_rigging_probe').glob('*.py'))
    require(paths and (ADDON / 'humanoid_rigging_probe' / '__init__.py') in paths,
            'Candidate addon closure is missing', 'NOT_FOUND')
    if family == 'controls':
        paths.append(SCRIPTS / 'space_fixture.py')
    if family == 'operation':
        paths.append(SCRIPTS / 'operation_state.py')
    return check_descriptors([descriptor(path) for path in paths])


def identity(family):
    names = {'frontend': Path(__file__), 'host_contract': SCRIPTS / 'host_contract.py',
             'public_wrapper': PUBLIC}
    if family in ('controls', 'limb'):
        suffix = 'control' if family == 'controls' else 'limb'
        names.update(builder=SCRIPTS / ('build_' + suffix + '_request.py'),
                     entry=SCRIPTS / ('run_controls.py' if family == 'controls' else 'run_limb.py'))
    else:
        names.update(entry=SCRIPTS / 'run_operation.py', helper=SCRIPTS / 'operation_state.py')
    return {role: descriptor(path) for role, path in names.items()}


def check_identity(roles, family):
    expected = identity(family)
    require(roles == expected, 'Frontend/entry/helper/builder/public wrapper identity changed', 'CONFLICT')


def describe(args):
    schemas = operation_schemas()
    if args.schema:
        require(args.operation is not None, '--schema requires --operation')
        return {'frontend_version': FRONTEND_VERSION, 'operation': args.operation,
                'schema': schemas[args.operation],
                'procedural_constraints': 'finite numbers, absolute existing descriptors and observed SHA; schema alone does not certify runtime'}
    actions = [args.operation] if args.operation else list(schemas)
    entry = SCRIPTS / 'run_operation.py'
    helper = SCRIPTS / 'operation_state.py'
    available = entry.is_file() and helper.is_file()
    return {'schema': 'hrig.cli.describe.v1', 'frontend_version': FRONTEND_VERSION,
            'backend': 'existing public extension.run', 'public_rigging_commands_added': False,
            'state_source': 'SAVED_DISK_V1; GUI unsaved state NOT_OBSERVED',
            'operations': {name: {'host_contract_available': True,
                'worker_adapter_available': available, 'native_validation': 'not_asserted_by_discovery'} for name in actions},
            'fixtures': {'controls': controls_cases(), 'limb': ['roundtrip', *NEGATIVE_CASES]},
            'roles': {name: descriptor(path) if path.is_file() else {'file': str(path), 'status': 'missing'}
                      for name, path in {'frontend': Path(__file__), 'host_contract': SCRIPTS/'host_contract.py',
                                         'operation_entry': entry, 'operation_helper': helper}.items()},
            'verification': 'producer candidate plus independent cold required for succeeded bundle',
            'limitations': ['current-pose bounded controls and owned limb only',
                            'no complete improved Aon, arbitrary rig retrofit, animation bake or human deformation certification',
                            'GUI/FPS authorized; not measured',
                            'trusted current-user Python, no OS sandbox']}


def new_run_dir(value):
    path = Path(value)
    require(path.is_absolute(), '--out-dir must be absolute')
    # resolve parents before creation so a junction cannot change the recorded root.
    path = path.resolve()
    require(not path.exists(), '--out-dir must be entirely new: ' + str(path), 'CONFLICT')
    require(path.parent.is_dir(), '--out-dir parent must already exist', 'NOT_FOUND')
    try:
        path.mkdir(exist_ok=False)
    except FileExistsError as error:
        raise ContractError('CONFLICT', 'Output directory was created concurrently') from error
    (path / 'jobs').mkdir()
    return path


def invoke_builder(run_dir, family, options):
    suffix = 'control' if family == 'controls' else 'limb'
    script = SCRIPTS / ('build_' + suffix + '_request.py')
    destination = run_dir / 'producer-request.json'
    call = [sys.executable, '-B', str(script), *options, '--out', str(destination)]
    completed = subprocess.run(call, capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=30, creationflags=NO_WINDOW)
    write_log(run_dir / 'builder-stdout.txt', completed.stdout)
    write_log(run_dir / 'builder-stderr.txt', completed.stderr)
    require(completed.returncode == 0, 'Pinned request builder failed; see builder logs',
            'INVALID_REQUEST')
    return read_json(destination), destination


def extension_request(family, roles, impl, config, outputs):
    config = {**config, 'addon_root': str(ADDON), 'implementation': impl}
    documents = list(impl)
    for document in walk_descriptors(config):
        if document not in documents:
            documents.append(document)
    return {'schema_version': '1.0', 'command': 'extension.run', 'params': {
        'manifest': {'script': roles['entry'], 'trust': 'execute_local_python_without_sandbox',
                     'params': config, 'outputs': outputs}, 'resources': documents}}


def check_request(request, family, roles, impl):
    fields(request, ('schema_version', 'command', 'params'), label='public request')
    require(request['schema_version'] == '1.0' and request['command'] == 'extension.run',
            'Unexpected backend request')
    fields(request['params'], ('manifest', 'resources'), label='public params')
    manifest = request['params']['manifest']
    fields(manifest, ('script', 'trust', 'params', 'outputs'), label='extension manifest')
    require(manifest['script'] == roles['entry'] and
            manifest['trust'] == 'execute_local_python_without_sandbox', 'Backend role/trust differs')
    config = manifest['params']
    require(config['addon_root'] == str(ADDON) and config['implementation'] == impl,
            'Exact implementation role closure differs')
    check_descriptors(impl)
    check_descriptors(request['params']['resources'])
    resources = request['params']['resources']
    for document in walk_descriptors(config):
        require(document in resources, 'Undeclared pinned input: ' + document['file'])
    if family == 'controls':
        require(config.get('fixture_script') == str(SCRIPTS / 'space_fixture.py'), 'Fixture role mismatch')
    return request


def public_job(run_dir, label, request, timeout):
    powershell = shutil.which('pwsh') or shutil.which('powershell')
    require(powershell is not None, 'PowerShell executable missing', 'NOT_FOUND')
    request_path = run_dir / (label + '-request.json')
    if request_path.exists():
        require(read_json(request_path) == request, 'Prebuilt request content changed', 'CONFLICT')
    else:
        write_new(request_path, request)
    command = [powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', str(PUBLIC), '--jobs-dir', str(run_dir / 'jobs'), '--timeout', str(timeout),
               'request', str(request_path)]
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=timeout + 60, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise ContractError('TIMEOUT', 'Public CLI did not finish within its host allowance') from error
    write_log(run_dir / (label + '-stdout.txt'), process.stdout)
    write_log(run_dir / (label + '-stderr.txt'), process.stderr)
    try:
        value = parse_json(process.stdout.lstrip('\ufeff'))
    except ContractError as error:
        raise ContractError('WORKER_FAILED', 'Public CLI returned invalid JSON; see host logs',
                            exit_code=process.returncode if 1 <= process.returncode <= 13 else 5) from error
    if process.returncode != 0 or value.get('ok') is not True:
        details = value.get('error') or {}
        raise ContractError(details.get('code', 'WORKER_FAILED'), details.get('message', 'Public job failed'),
                            exit_code=process.returncode if 1 <= process.returncode <= 13 else None)
    require(value.get('command') == 'extension.run' and value.get('error') is None,
            'Public result has wrong command/error', 'VALIDATION_FAILED')
    job_id = value.get('job_id')
    require(type(job_id) is str and re.fullmatch(r'[0-9a-f]{32}', job_id) is not None,
            'Public result has invalid job identity', 'VALIDATION_FAILED')
    job = (run_dir / 'jobs' / job_id).resolve()
    require(Path(value['artifacts']['job_directory']).resolve() == job,
            'Public artifact job directory escapes this run', 'VALIDATION_FAILED')
    require(read_json(job / 'result.json') == value, 'Public stdout differs from saved result', 'CONFLICT')
    require(read_json(job / 'request.json') == request, 'Public saved request differs', 'CONFLICT')
    data = value.get('data') or {}
    require(data.get('script') == request['params']['manifest']['script'] and
            data.get('script_sha256_observed') == request['params']['manifest']['script']['expected_sha256'],
            'Public executed entry identity mismatch', 'CONFLICT')
    output_rows = data.get('outputs')
    require(type(output_rows) is list, 'Public output descriptors missing', 'VALIDATION_FAILED')
    outputs = {}
    for row in output_rows:
        path = Path(row['file'])
        require(path.is_absolute() and not path.is_symlink() and path.resolve().parent == job,
                'Public output escapes producer job', 'VALIDATION_FAILED')
        require(path.name not in outputs and path.name in request['params']['manifest']['outputs'],
                'Unexpected or duplicate public output', 'VALIDATION_FAILED')
        require(value['artifacts'].get(path.name) == str(path),
                'Public artifact path differs from hashed output', 'CONFLICT')
        outputs[path.name] = descriptor(path, row['sha256'])
        require(path.stat().st_size == row['bytes'], 'Public output byte count differs', 'CONFLICT')
    require(set(outputs) == set(request['params']['manifest']['outputs']),
            'Not all declared outputs are verified', 'VALIDATION_FAILED')
    for document in request['params']['resources']:
        require(data.get('input_hashes', {}).get(document['file']) == document['expected_sha256'],
                'Public input hash evidence missing/different: ' + document['file'], 'CONFLICT')
    return {'job_id': job_id, 'result': descriptor(job / 'result.json'),
            'request': descriptor(job / 'request.json'), 'outputs': outputs,
            'cli_version': value.get('cli_version'), 'worker_result': data.get('result')}


def report_name(family):
    return {'operation': 'output-operation.json', 'controls': 'output-control.json',
            'limb': 'output-limb.json'}[family]


def producer_check(family, public):
    report = read_json(public['outputs'][report_name(family)]['file'])
    if family == 'controls':
        require(report.get('validation', {}).get('ok') is True,
                'Control producer fixture validation failed', 'VALIDATION_FAILED')
        require(public['worker_result'].get('status') in ('pass', 'candidate_waiting_independent_cold'),
                'Unexpected control producer state', 'VALIDATION_FAILED')
    else:
        require(report.get('status') == 'candidate_waiting_independent_cold',
                'Producer is not ready for independent cold validation', 'VALIDATION_FAILED')
    return report


def cold_request(family, roles, impl, producer, operation_desc=None):
    outputs = producer['outputs']
    candidate = outputs['output-candidate.blend' if family == 'operation' else 'output-fixture.blend']
    config = {'mode': 'cold', 'input': candidate,
              'producer_report': outputs[report_name(family)]}
    if family == 'operation':
        require(operation_desc is not None, 'Operation descriptor missing from bundle')
        config['operation'] = operation_desc
        loaded = operation(read_json(operation_desc['file']))
        # Nested manifest is a native input role, not merely operation JSON text.
        request = extension_request(family, roles, impl, config, [report_name(family)])
        if 'manifest' in loaded and loaded['manifest'] not in request['params']['resources']:
            request['params']['resources'].append(loaded['manifest'])
        return request
    config['fixture_manifest' if family == 'controls' else 'manifest'] = outputs['output-manifest.json']
    if family == 'controls':
        config['fixture_script'] = str(SCRIPTS / 'space_fixture.py')
    return extension_request(family, roles, impl, config, [report_name(family)])


def cold_check(family, public):
    report = read_json(public['outputs'][report_name(family)]['file'])
    require(report.get('source_unchanged') is True, 'Cold source changed or not certified', 'CONFLICT')
    require(public['worker_result'].get('status') == 'pass', 'Cold worker did not pass', 'VALIDATION_FAILED')
    if family == 'controls':
        require(report.get('validation', {}).get('ok') is True and
                report.get('producer_stored_state_matches') is True,
                'Cold control storage/evaluation failed', 'VALIDATION_FAILED')
    else:
        require(report.get('status') == 'pass', 'Cold report did not pass', 'VALIDATION_FAILED')
    return report


def verify_public_record(record):
    """Bind saved job identity, request, result and exact artifact descriptors."""
    fields(record, ('job_id', 'result', 'request', 'outputs', 'cli_version', 'worker_result'), label='job record')
    for document in walk_descriptors(record):
        check_descriptor(document)
    result = read_json(record['result']['file'])
    request = read_json(record['request']['file'])
    job = Path(record['result']['file']).resolve().parent
    require(result.get('ok') is True and result.get('command') == 'extension.run'
            and result.get('job_id') == record['job_id'] and job.name == record['job_id'],
            'Bundle job is not an exact successful public producer', 'CONFLICT')
    require(Path(record['request']['file']).resolve() == job / 'request.json' and
            Path(result['artifacts']['job_directory']).resolve() == job,
            'Bundle public job paths differ', 'CONFLICT')
    expected_outputs = {Path(row['file']).name: {'file': row['file'], 'expected_sha256': row['sha256']}
                        for row in result['data']['outputs']}
    require(record['outputs'] == expected_outputs and
            record['worker_result'] == result['data']['result'], 'Bundle output/result binding differs', 'CONFLICT')
    for name, document in record['outputs'].items():
        require(Path(document['file']).resolve().parent == job and
                result['artifacts'].get(name) == document['file'], 'Bundle output escapes job', 'CONFLICT')
    require(result['data']['script'] == request['params']['manifest']['script'],
            'Bundle executed entry differs from request', 'CONFLICT')
    return request


def execute(args, run_dir):
    timeout = number(args.timeout, 'timeout', 1, 86400)
    inputs = {}
    if args.command == 'validate':
        bundle_desc = descriptor(args.bundle, args.expected_sha256)
        prior = read_json(bundle_desc['file'])
        fields(prior, ('schema', 'status', 'family', 'identity', 'implementation', 'inputs',
                       'producer', 'cold', 'candidate', 'verification', 'frontend_version',
                       'created_utc', 'GUI', 'FPS'), ('operation', 'manifest', 'validated_bundle'), 'bundle')
        require(prior['schema'] == BUNDLE_SCHEMA and prior['status'] == 'succeeded',
                'Only succeeded immutable bundles can be validated')
        family = prior['family']
        require(family in ('controls', 'limb', 'operation'), 'Unknown bundle family')
        roles, impl = identity(family), implementation(family)
        check_identity(prior['identity'], family)
        require(prior['implementation'] == impl, 'Bundle implementation identity changed', 'CONFLICT')
        for _ in walk_descriptors(prior):
            pass
        original_request = verify_public_record(prior['producer'])
        check_request(original_request, family, roles, impl)
        verify_public_record(prior['cold'])
        producer_check(family, prior['producer'])
        cold_check(family, prior['cold'])
        producer = prior['producer']
        expected_candidate = producer['outputs']['output-candidate.blend' if family == 'operation' else 'output-fixture.blend']
        require(prior['candidate'] == expected_candidate, 'Bundle candidate differs from producer', 'CONFLICT')
        inputs = prior['inputs']
        operation_desc = prior.get('operation')
        cold = cold_request(family, roles, impl, producer, operation_desc)
        prior_cold_request = verify_public_record(prior['cold'])
        require(prior_cold_request == cold,
                'Bundle cold job does not verify this exact producer/candidate/operation', 'CONFLICT')
        if family == 'operation':
            require(original_request['params']['manifest']['params'].get('operation') == operation_desc and
                    inputs.get('operation') == operation_desc,
                    'Bundle operation differs from the producer operation role', 'CONFLICT')
            require(inputs.get('source') == original_request['params']['manifest']['params'].get('input'),
                    'Bundle source differs from the producer input role', 'CONFLICT')
        check_request(cold, family, roles, impl)
        verified = public_job(run_dir, 'cold', cold, timeout)
        evidence = cold_check(family, verified)
        final = {**prior, 'identity': roles, 'cold': verified, 'verification': evidence,
                 'created_utc': datetime.now(timezone.utc).isoformat(), 'validated_bundle': bundle_desc}
    else:
        family = 'operation' if args.command == 'run' else args.family
        roles, impl = identity(family), implementation(family)
        operation_desc = None
        if family == 'operation':
            operation_desc = descriptor(args.operation, args.expected_operation_sha256)
            value = operation(read_json(operation_desc['file']))
            inputs['operation'] = operation_desc
            config = {'mode': 'operate', 'operation': operation_desc}
            if value['action'] == 'create_limb':
                require(args.source is None and args.expected_sha256 is None,
                        'create_limb does not accept a source file')
            else:
                require(args.source is not None and args.expected_sha256 is not None,
                        'Editing an existing source requires --source and --expected-sha256')
                inputs['source'] = descriptor(args.source, args.expected_sha256)
                require(Path(inputs['source']['file']).suffix.lower() == '.blend', 'Source must be a .blend')
                config['input'] = inputs['source']
            request = extension_request(family, roles, impl, config,
                                        ['output-operation.json', 'output-candidate.blend'])
            if 'manifest' in value:
                inputs['manifest'] = value['manifest']
                request['params']['resources'].append(value['manifest'])
        else:
            options = ['--mode', 'fixture' if family == 'controls' else ('roundtrip' if args.case == 'roundtrip' else 'negative')]
            if family == 'controls':
                options += ['--case', args.case]
            else:
                options += ['--module-id', args.module_id, '--side', str(args.side), '--roll', str(args.roll),
                            '--scale', str(args.scale), '--pose-index', str(args.pose_index)]
                if args.case != 'roundtrip':
                    options += ['--case', args.case]
            request, _ = invoke_builder(run_dir, family, options)
        check_request(request, family, roles, impl)
        producer = public_job(run_dir, 'producer', request, timeout)
        producer_check(family, producer)
        check_identity(roles, family)
        require(impl == implementation(family), 'Implementation changed after producer', 'CONFLICT')
        cold = cold_request(family, roles, impl, producer, operation_desc)
        check_request(cold, family, roles, impl)
        verified = public_job(run_dir, 'cold', cold, timeout)
        evidence = cold_check(family, verified)
        final = {'schema': BUNDLE_SCHEMA, 'frontend_version': FRONTEND_VERSION, 'status': 'succeeded',
                 'family': family, 'identity': roles, 'implementation': impl, 'inputs': inputs,
                 'producer': producer, 'cold': verified,
                 'candidate': producer['outputs']['output-candidate.blend' if family == 'operation' else 'output-fixture.blend'],
                 'verification': evidence, 'created_utc': datetime.now(timezone.utc).isoformat(),
                 'GUI': 'not_measured', 'FPS': 'not_measured'}
        if operation_desc:
            final['operation'] = operation_desc
        else:
            final['manifest'] = producer['outputs']['output-manifest.json']
    check_identity(roles, family)
    require(impl == implementation(family), 'Implementation changed before bundle commit', 'CONFLICT')
    for _ in walk_descriptors(final):
        pass
    write_new(run_dir / 'bundle.json', final)
    return {'ok': True, 'status': 'succeeded', 'bundle': descriptor(run_dir / 'bundle.json'),
            'family': family, 'candidate': final['candidate'],
            'producer_job_id': final['producer']['job_id'], 'cold_job_id': final['cold']['job_id'],
            'operation_ok': final['producer']['worker_result'].get('operation_ok'),
            'GUI': final['GUI'], 'FPS': final['FPS']}


def preflight_args(args):
    """Perform caller-input failures before creating a directory or any worker."""
    if args.command == 'run':
        value = operation(read_json(descriptor(args.operation, args.expected_operation_sha256)['file']))
        if value['action'] == 'create_limb':
            require(args.source is None and args.expected_sha256 is None, 'create_limb has no source')
        else:
            require(args.source is not None and args.expected_sha256 is not None,
                    'Existing source editing requires explicit source SHA')
            descriptor(args.source, args.expected_sha256)
    elif args.command == 'validate':
        descriptor(args.bundle, args.expected_sha256)
    elif args.command == 'fixtures':
        cases = controls_cases() if args.family == 'controls' else ['roundtrip', *NEGATIVE_CASES]
        if args.list:
            require(args.case is None and args.out_dir is None and args.module_id is None and
                    all(getattr(args, key) is None for key in ('side', 'roll', 'scale', 'pose_index')),
                    '--list accepts only --family')
            return {'family': args.family, 'cases': cases, 'Blender_executed': False}
        require(args.out_dir is not None and args.case in cases, 'Explicit supported --case and new --out-dir required')
        if args.family == 'controls':
            require(args.module_id is None and all(getattr(args, key) is None for key in ('side', 'roll', 'scale', 'pose_index')),
                    'Controls fixtures do not accept limb options')
        else:
            require(args.module_id is not None, 'Limb fixture requires --module-id')
            args.side = args.side if args.side is not None else 1
            args.roll = args.roll if args.roll is not None else .37
            args.scale = args.scale if args.scale is not None else 1.2
            args.pose_index = args.pose_index if args.pose_index is not None else 0
            spec({'module_id': args.module_id, 'side': args.side, 'rest_roll': args.roll,
                  'object_uniform_scale': args.scale})
    number(args.timeout, 'timeout', 1, 86400)
    return None


def main(argv=None):
    run_dir = None
    try:
        forwarded = list(sys.argv[1:] if argv is None else argv)
        if forwarded and forwarded[0] == 'hair-action':
            import hair_actions_cli
            return hair_actions_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'hair-pair':
            import hair_pair_cli
            return hair_pair_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'hair':
            import front_hair_cli
            return front_hair_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'subdiv':
            import subdivision_cli
            return subdivision_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'diagnose':
            import diagnostic_cli
            return diagnostic_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'derive':
            import derivation_cli
            return derivation_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'rebake':
            import rebake_cli
            return rebake_cli.main(forwarded[1:])
        # argparse REMAINDER does not capture a leading --help option. Dispatch
        # the isolated grammar first so weights help is owned by weight_cli.
        if forwarded and forwarded[0] == 'cache':
            import cache_cli
            return cache_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'dynamics':
            import dynamics_cli
            return dynamics_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'settings':
            import existing_control_cli
            return existing_control_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'motion':
            import motion_cli
            return motion_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'pose':
            import pose_cli
            return pose_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'collections':
            import collection_cli
            return collection_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'styles':
            import style_cli
            return style_cli.main(forwarded[1:])
        if forwarded and forwarded[0] == 'weights':
            import weight_cli
            return weight_cli.main(forwarded[1:])
        args = parser().parse_args(argv)
        if args.command == 'weights':
            import weight_cli
            return weight_cli.main(args.weight_args)
        if args.command == 'describe':
            response = describe(args)
        else:
            response = preflight_args(args)
            if response is None:
                run_dir = new_run_dir(args.out_dir)
                response = execute(args, run_dir)
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
        exit_code = error.exit_code if isinstance(error, ContractError) else CODES[code]
        failure = {'ok': False, 'status': 'failed', 'error': {'code': code, 'message': str(error)},
                   'run_directory': str(run_dir) if run_dir else None,
                   'candidate_policy': 'retained unverified/quarantined; no succeeded bundle'}
        if run_dir is not None and not (run_dir / 'failure.json').exists():
            try:
                write_new(run_dir / 'failure.json', failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False, allow_nan=False))
        return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
