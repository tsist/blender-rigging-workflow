"""Independent diagnostic host, planned install: work/<version>/scripts/.

VERSION 0.17.0. Pure-host describe; all native work uses public extension.run.
Timing is background frame/update/graph/capture observation, never playback
FPS. Witness adds bounded full coordinates/connectivity. Worker restores the
original frame/subframe, not exact solver memory. Worker refuses baked,
disk/external Cloth caches and requires cache-start forward sampling.
Source/dependency/build/authoring checks belong to the pinned worker contract.
No edit-success bundle, source save, debug enable, install, or GUI operation.

This audit staging file can describe the intended work/hrig-p2-v020 layout;
fixture/run refuse staging and require installation in that layout's scripts.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
VERSION = '0.17.0'
REPORT = 'output-diagnostic.json'
HERE = Path(__file__).resolve()
STAGED = HERE.parent.name == 'diagnostic-draft'
if STAGED:
    _project = next((p for p in HERE.parents if (p / 'tools/blenderctl/cli.py').is_file()), None)
    if _project is None:
        raise RuntimeError('Cannot locate the public CLI for staged host discovery')
    ROOT = _project / 'work' / 'hrig-p2-v020'
else:
    ROOT = HERE.parents[1]
SCRIPTS = ROOT / 'scripts'
ADDON = ROOT / 'addon'
PACKAGE = ADDON / 'humanoid_rigging_probe'
PROJECT = ROOT.parents[1]
PUBLIC = PROJECT / 'tools' / 'blenderctl' / 'cli.py'
INSPECTION = PROJECT / 'tools' / 'blenderctl' / 'inspection.py'
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H = _module(SCRIPTS / 'host_contract.py', '_hrig_diagnostic_host_helpers')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise H.ContractError('INVALID_REQUEST', message)


def parser():
    result = Parser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True, parser_class=Parser)
    commands.add_parser('describe', help='pure-host discovery; no Blender process')
    fixture = commands.add_parser('fixture', help='one owned native diagnostic fixture')
    fixture.add_argument('--case', choices=('alpha', 'beta'), required=True)
    run = commands.add_parser('run', help='observe an explicit saved source and exact plan')
    run.add_argument('--source', required=True)
    run.add_argument('--expected-sha256', required=True)
    run.add_argument('--plan', required=True)
    run.add_argument('--expected-plan-sha256', required=True)
    run.add_argument('--external-inputs', required=True,
                     help='raw JSON array of exact {file, expected_sha256} descriptors; use [] explicitly')
    for command in (fixture, run):
        command.add_argument('--out-dir', required=True)
        command.add_argument('--timeout', type=float, default=180.0)
    return result


def _write_bytes(path, content):
    with Path(path).open('xb') as stream:
        stream.write(content)


def _write_json(path, value):
    _write_bytes(path, (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8'))


def _run_dir(raw):
    path = Path(raw)
    H.require(path.is_absolute(), '--out-dir must be absolute')
    path = path.resolve()
    H.require(path.parent.is_dir(), '--out-dir parent must exist', 'NOT_FOUND')
    H.require(not path.exists(), '--out-dir must be entirely new', 'CONFLICT')
    try:
        path.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise H.ContractError('CONFLICT', 'Output directory appeared concurrently') from exc
    (path / 'jobs').mkdir(exist_ok=False)
    return path


def implementation():
    paths = sorted(PACKAGE.glob('*.py'))
    H.require(PACKAGE / '__init__.py' in paths
              and PACKAGE / 'diagnostic_contract.py' in paths
              and PACKAGE / 'diagnostics.py' in paths,
              'Installed diagnostic addon closure is incomplete', 'NOT_FOUND')
    paths += [SCRIPTS / 'character_state.py', SCRIPTS / 'diagnostic_fixture.py', INSPECTION]
    return H.check_descriptors([H.descriptor(path) for path in paths])


def identity():
    return {role: H.descriptor(path) for role, path in {
        'frontend': HERE, 'host_helpers': SCRIPTS / 'host_contract.py',
        'entry': SCRIPTS / 'run_diagnostic.py', 'public_cli': PUBLIC,
    }.items()}


def _unchanged(roles, impl):
    H.require(roles == identity() and impl == implementation(),
              'Diagnostic implementation/entry/public/frontend identity changed', 'CONFLICT')


def _resources(config):
    result = []
    by_path = {}
    for document in H.walk_descriptors(config):
        current = H.descriptor(document['file'], document['expected_sha256'])
        key = current['file'].casefold()
        H.require(key not in by_path or by_path[key] == current,
                  'Conflicting descriptors for one resource', 'CONFLICT')
        if key not in by_path:
            by_path[key] = current
            result.append(current)
    return H.check_descriptors(result)


def request(roles, config, outputs):
    H.fields(config, ('mode', 'addon_root', 'implementation', 'input', 'plan',
                      'external_inputs', 'case'), label='diagnostic runner params')
    return {'schema_version': '1.0', 'command': 'extension.run', 'params': {
        'manifest': {'script': roles['entry'], 'trust': 'execute_local_python_without_sandbox',
                     'params': config, 'outputs': outputs},
        'resources': _resources(config)}}


def preflight(args):
    H.number(args.timeout, 'timeout', minimum=0.001)
    H.require(not STAGED and HERE.parent.resolve() == SCRIPTS.resolve(),
              'Staged draft supports describe only; install reviewed sources before native submission', 'UNSUPPORTED')
    config = {'mode': 'fixture' if args.command == 'fixture' else 'observe',
              'addon_root': str(ADDON), 'implementation': implementation(),
              'input': None, 'plan': None, 'external_inputs': [],
              'case': args.case if args.command == 'fixture' else None}
    if args.command == 'run':
        config['input'] = H.descriptor(args.source, args.expected_sha256)
        config['plan'] = H.descriptor(args.plan, args.expected_plan_sha256)
        plan_path = Path(config['plan']['file'])
        H.require(plan_path.stat().st_size <= 1 * 1024 * 1024, 'Plan exceeds the 1 MiB diagnostic JSON limit')
        contract = _module(PACKAGE / 'diagnostic_contract.py', '_hrig_diagnostic_plan_contract')
        try:
            contract.parse_plan(plan_path.read_bytes())
        except ValueError as exc:
            raise H.ContractError('INVALID_REQUEST', str(exc)) from exc
        parsed = H.parse_json(args.external_inputs)
        H.check_descriptors(parsed)
        config['external_inputs'] = [H.descriptor(row['file'], row['expected_sha256']) for row in parsed]
    outputs = [REPORT] if args.command == 'run' else ['output-fixture.blend', 'output-fixture-manifest.json', REPORT]
    return config, outputs


def _verify_public(value, code, request_value, run_dir):
    H.require(type(value) is dict, 'Public result must be an object', 'VALIDATION_FAILED')
    if code != 0 or value.get('ok') is not True:
        error = value.get('error') or {}
        H.require(type(error) is dict, 'Invalid public error object', 'VALIDATION_FAILED')
        raise H.ContractError(error.get('code', 'WORKER_FAILED'), error.get('message', 'Public diagnostic job failed'))
    H.require(value.get('command') == 'extension.run' and value.get('error') is None,
              'Unexpected public command/error', 'VALIDATION_FAILED')
    job_id = value.get('job_id')
    H.require(type(job_id) is str and re.fullmatch(r'[0-9a-f]{32}', job_id) is not None,
              'Invalid public job identity', 'VALIDATION_FAILED')
    job = (run_dir / 'jobs' / job_id).resolve()
    artifacts = value.get('artifacts')
    H.require(type(artifacts) is dict and type(artifacts.get('job_directory')) is str,
              'Public job directory missing', 'VALIDATION_FAILED')
    H.require(Path(artifacts['job_directory']).is_absolute()
              and Path(artifacts['job_directory']).resolve() == job
              and not (run_dir / 'jobs' / job_id).is_symlink(),
              'Public job directory escapes this run', 'VALIDATION_FAILED')
    H.require(H.read_json(job / 'result.json') == value, 'Public stdout differs from saved result', 'CONFLICT')
    H.require(H.read_json(job / 'request.json') == request_value, 'Public saved request differs', 'CONFLICT')
    data = value.get('data')
    H.require(type(data) is dict, 'Public extension report missing', 'VALIDATION_FAILED')
    entry = request_value['params']['manifest']['script']
    H.require(data.get('script') == entry and data.get('script_sha256_observed') == entry['expected_sha256'],
              'Executed worker differs from pinned entry', 'CONFLICT')
    worker = data.get('result')
    H.require(type(worker) is dict and worker.get('status') == 'pass'
              and worker.get('source_unchanged') is True,
              'Worker did not pass or did not certify declared source unchanged', 'VALIDATION_FAILED')
    for document in request_value['params']['resources']:
        H.require(data.get('input_hashes', {}).get(document['file']) == document['expected_sha256'],
                  'Public input hash evidence differs: ' + document['file'], 'CONFLICT')
        H.check_descriptor(document)
    expected = request_value['params']['manifest']['outputs']
    rows = data.get('outputs')
    H.require(type(rows) is list, 'Public output descriptors missing', 'VALIDATION_FAILED')
    outputs = {}
    for row in rows:
        H.fields(row, ('file', 'sha256', 'bytes'), label='public output')
        H.require(type(row['file']) is str, 'Output filename must be a string', 'VALIDATION_FAILED')
        path = Path(row['file'])
        H.require(path.is_absolute() and not path.is_symlink() and path.resolve().parent == job,
                  'Output escapes the owned public job', 'VALIDATION_FAILED')
        H.require(path.name in expected and path.name not in outputs,
                  'Unexpected or duplicate output', 'VALIDATION_FAILED')
        H.require(artifacts.get(path.name) == str(path), 'Public artifact/output mismatch', 'CONFLICT')
        document = H.descriptor(path, row['sha256'])
        H.require(type(row['bytes']) is int and path.stat().st_size == row['bytes'],
                  'Output byte count differs', 'CONFLICT')
        outputs[path.name] = {**document, 'bytes': row['bytes']}
    H.require(set(outputs) == set(expected), 'Declared output missing', 'VALIDATION_FAILED')
    report = H.read_json(outputs[REPORT]['file'])
    H.require(type(report) is dict and report.get('status') == 'pass'
              and report.get('source_unchanged') is True,
              'Persisted diagnostic report did not pass source protection', 'VALIDATION_FAILED')
    return job_id, outputs, worker


def execute(args, run_dir, config, outputs, roles):
    req = request(roles, config, outputs)
    request_path = run_dir / 'request.json'
    _write_json(request_path, req)
    _unchanged(roles, config['implementation'])
    call = [sys.executable, '-B', str(PUBLIC), '--jobs-dir', str(run_dir / 'jobs'),
            '--timeout', str(args.timeout), 'request', str(request_path)]
    try:
        process = subprocess.run(call, capture_output=True, timeout=args.timeout + 60,
                                 creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        _write_bytes(run_dir / 'stdout.txt', exc.stdout or b'')
        _write_bytes(run_dir / 'stderr.txt', exc.stderr or b'')
        _write_json(run_dir / 'result.json', {'ok': False, 'error': {'code': 'TIMEOUT',
                    'message': 'Public CLI host allowance expired; native worker terminal state not certified'}})
        raise H.ContractError('TIMEOUT', 'Public CLI exceeded host allowance; inspect owned job state and logs') from exc
    _write_bytes(run_dir / 'stdout.txt', process.stdout)
    _write_bytes(run_dir / 'stderr.txt', process.stderr)
    try:
        public = H.parse_json(process.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        _write_json(run_dir / 'result.json', {'ok': False, 'error': {'code': 'WORKER_FAILED',
                    'message': 'Invalid public JSON; complete raw stdout/stderr retained'},
                    'public_returncode': process.returncode})
        raise H.ContractError('WORKER_FAILED', 'Public CLI returned invalid JSON; inspect complete logs') from exc
    _write_json(run_dir / 'result.json', public)
    job_id, artifact_outputs, worker = _verify_public(public, process.returncode, req, run_dir)
    _unchanged(roles, config['implementation'])
    result = {'schema': 'hrig.diagnostic.cli.result.v1', 'frontend_version': VERSION,
              'ok': True, 'status': 'pass', 'mode': config['mode'], 'job_id': job_id,
              'FPS_claim': False, 'edit_success_bundle': False,
              'artifacts': {'request': H.descriptor(request_path),
                            'public_result': H.descriptor(run_dir / 'result.json'),
                            'stdout': H.descriptor(run_dir / 'stdout.txt'),
                            'stderr': H.descriptor(run_dir / 'stderr.txt'),
                            'outputs': artifact_outputs},
              'identity': roles, 'implementation': config['implementation'],
              'verification': worker}
    _write_json(run_dir / 'host-result.json', result)
    return result


def describe():
    paths = {'frontend': HERE, 'entry': SCRIPTS / 'run_diagnostic.py',
             'fixture': SCRIPTS / 'diagnostic_fixture.py',
             'contract': PACKAGE / 'diagnostic_contract.py', 'probe': PACKAGE / 'diagnostics.py',
             'host_helpers': SCRIPTS / 'host_contract.py', 'public_cli': PUBLIC,
             'character_state': SCRIPTS / 'character_state.py', 'inspection': INSPECTION}
    return {'schema': 'hrig.diagnostic.cli.describe.v1', 'frontend_version': VERSION,
            'backend': 'existing public extension.run via Python tools/blenderctl/cli.py',
            'public_rigging_commands_added': False, 'Blender_executed': False,
            'staging_only': STAGED, 'commands': ['describe', 'fixture', 'run'],
            'fixture_cases': ['alpha', 'beta'], 'modes': ['timing', 'witness'],
            'plan_schema': 'hrig.diagnostic-plan.v1',
            'runner_params': ['mode', 'addon_root', 'implementation', 'input', 'plan', 'external_inputs', 'case'],
            'root': str(ROOT), 'project': str(PROJECT),
            'roles': {name: H.descriptor(path) if path.is_file() else {'file': str(path), 'status': 'missing'}
                      for name, path in paths.items()},
            'native_validation': 'not_asserted_by_discovery',
            'limitations': ['SAVED_DISK_V1; unsaved GUI work not observed',
                            'timing measures background calls plus capture; playback FPS/draw not measured',
                            'witness adds full coords/topology under an entire-run actual vertex budget',
                            'frame/subframe restore required; exact solver memory/cache restoration not claimed',
                            'worker refuses baked/disk/external Cloth and requires cache-start forward protocol',
                            'explicit static external descriptors required; no implicit source SHA adoption',
                            'no user-source save or edit-success bundle; fixture is owned test data',
                            'trusted current-user local Python without OS sandbox; debug not enabled by probe']}


def main(argv=None):
    run_dir = None
    try:
        args = parser().parse_args(argv)
        if args.command == 'describe':
            response = describe()
        else:
            config, outputs = preflight(args)
            roles = identity()
            run_dir = _run_dir(args.out_dir)
            response = execute(args, run_dir, config, outputs, roles)
        print(json.dumps(response, ensure_ascii=False, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, H.ContractError):
            code = exc.code
        elif isinstance(exc, KeyboardInterrupt):
            code = 'CANCELLED'
        elif isinstance(exc, OSError):
            code = 'IO_ERROR'
        elif isinstance(exc, (ValueError, KeyError, TypeError)):
            code = 'INVALID_REQUEST'
        else:
            code = 'WORKER_FAILED'
        failure = {'schema': 'hrig.diagnostic.cli.result.v1', 'frontend_version': VERSION,
                   'ok': False, 'status': 'failed', 'FPS_claim': False,
                   'edit_success_bundle': False, 'error': {'code': code, 'message': str(exc)},
                   'run_directory': str(run_dir) if run_dir else None,
                   'recovery': 'retain owned job evidence; no observation or frame-restore success inferred from failure'}
        if run_dir is not None:
            try:
                _write_json(run_dir / 'failure.json', failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False, allow_nan=False))
        return exc.exit_code if isinstance(exc, H.ContractError) else H.CODES.get(code, 5)


if __name__ == '__main__':
    raise SystemExit(main())
