"""Bounded Subsurf viewport intervention host, candidate 0.18.0.

describe is pure host discovery. run submits only the pinned owned worker via
existing public extension.run. No source save, edit-success bundle or FPS claim.
The intervention observes the complete downstream evaluation, not isolated
Subsurf execution time. Native acceptance is never inferred from discovery.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
VERSION = '0.18.0'
REPORT = 'output-diagnostic.json'
PLAN_SCHEMA = 'hrig.subdivision-diagnostic.v1'
REPORT_SCHEMA = 'hrig.subdivision-diagnostic-report.v1'
HERE = Path(__file__).resolve()
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parent
PACKAGE = ROOT / 'addon' / 'humanoid_rigging_probe'
ENTRY = SCRIPTS / 'run_subdivision.py'
CORE = PACKAGE / 'subdivision_diagnostic.py'


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load required local host module: ' + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = _module(SCRIPTS / 'diagnostic_cli.py', '_hrig_subdivision_diagnostic_host')
H = D.H


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise H.ContractError('INVALID_REQUEST', message)


def parser():
    result = Parser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True, parser_class=Parser)
    commands.add_parser('describe', help='pure-host paths/SHA and bounded scope; no Blender')
    run = commands.add_parser('run', help='observe one explicitly declared Subsurf viewport intervention')
    for option in ('source', 'expected-sha256', 'plan', 'expected-plan-sha256',
                   'external-inputs', 'out-dir'):
        run.add_argument('--' + option, required=True,
                         help=('raw JSON array of exact {file, expected_sha256} descriptors; '
                               'use [] explicitly') if option == 'external-inputs' else None)
    run.add_argument('--timeout', type=float, default=180.0)
    return result


def implementation():
    # The new core must be present in D's addon closure, not silently optional.
    core = H.descriptor(CORE)
    rows = D.implementation() + [H.descriptor(SCRIPTS / 'host_contract.py')]
    result, seen = [], {}
    for row in rows:
        H.check_descriptor(row)
        key = str(Path(row['file']).resolve()).casefold()
        H.require(key not in seen or seen[key] == row,
                  'Conflicting implementation descriptors', 'CONFLICT')
        if key not in seen:
            seen[key] = row
            result.append(row)
    H.require(core in result, 'Subdivision core is outside the pinned addon closure', 'CONFLICT')
    return H.check_descriptors(result)


def identity():
    return {role: H.descriptor(path) for role, path in {
        'frontend': HERE, 'host_helpers': SCRIPTS / 'host_contract.py',
        'diagnostic_host': SCRIPTS / 'diagnostic_cli.py',
        'entry': ENTRY, 'public_cli': D.PUBLIC,
    }.items()}


def _unchanged(roles, impl):
    H.require(identity() == roles and implementation() == impl,
              'Subdivision frontend/helpers/entry/public/addon identity changed', 'CONFLICT')


def _same_json(left, right):
    # Python equality accepts True == 1. Typed canonical JSON equality does not.
    return json.dumps(left, sort_keys=True, separators=(',', ':'), allow_nan=False) == \
        json.dumps(right, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _plan_contract():
    # The core uses relative imports. Give it an isolated package identity rather
    # than reusing a possibly already imported addon from another work version.
    # The reviewed __init__ only defines register/unregister; neither is called.
    namespace = '_hrig_subdivision_contract_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(
        namespace, PACKAGE / '__init__.py', submodule_search_locations=[str(PACKAGE)])
    H.require(spec is not None and spec.loader is not None,
              'Cannot load the subdivision contract package', 'NOT_FOUND')
    package = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = package
    try:
        spec.loader.exec_module(package)
        return importlib.import_module(namespace + '.subdivision_diagnostic')
    finally:
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + '.'):
                del sys.modules[name]


def preflight(args, impl):
    H.number(args.timeout, 'timeout', minimum=0.001)
    H.require(not D.STAGED and D.ROOT.resolve() == ROOT.resolve()
              and D.SCRIPTS.resolve() == SCRIPTS.resolve(),
              'Run requires the installed reviewed work/<version>/scripts layout', 'UNSUPPORTED')
    source = H.descriptor(args.source, args.expected_sha256)
    plan_descriptor = H.descriptor(args.plan, args.expected_plan_sha256)
    plan_path = Path(plan_descriptor['file'])
    H.require(plan_path.stat().st_size <= 1024 * 1024,
              'Plan exceeds the 1 MiB diagnostic JSON limit')
    parsed = H.parse_json(plan_path.read_text(encoding='utf-8-sig'))
    core = _plan_contract()
    try:
        validated = core.validate_plan(parsed)
    except H.ContractError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise H.ContractError('INVALID_REQUEST', str(exc)) from exc
    H.require(type(validated) is dict and _same_json(validated, parsed),
              'Plan validator must return the exact validated plan without normalizing inputs')
    H.require(validated.get('schema') == PLAN_SCHEMA, 'Unexpected subdivision plan schema')
    H.fields(validated, ('schema', 'observation', 'target', 'enabled'), label='subdivision plan')
    H.require(type(validated['enabled']) is bool, 'enabled must be a JSON boolean')
    H.fields(validated['target'], ('object', 'modifier', 'index', 'expected_show_viewport'),
             label='subdivision target')
    H.require(validated['target']['expected_show_viewport'] is True,
              'Target before show_viewport must be explicitly true')
    # The core validates the complete nested observation and strict integer types.
    # The host also prevents accidental target vertex comparisons across topology.
    selected = [row for row in validated['observation']['objects']
                if row['name'] == validated['target']['object']]
    H.require(len(selected) == 1 and selected[0]['vertex_indices'] == [],
              'Target must be selected exactly once with vertex_indices=[]')
    external = H.parse_json(args.external_inputs)
    H.check_descriptors(external)
    config = {'input': source, 'plan': plan_descriptor,
              'external_inputs': [H.descriptor(row['file'], row['expected_sha256']) for row in external],
              'implementation': impl, 'addon_root': str(ROOT / 'addon')}
    # Parsing and importing the validator must not silently accept changed bytes.
    H.descriptor(plan_descriptor['file'], plan_descriptor['expected_sha256'])
    D._resources(config)
    return config, validated


def request(roles, config):
    H.fields(config, ('input', 'plan', 'external_inputs', 'implementation', 'addon_root'),
             label='subdivision runner params')
    return {'schema_version': '1.0', 'command': 'extension.run', 'params': {
        'manifest': {'script': roles['entry'], 'trust': 'execute_local_python_without_sandbox',
                     'params': config, 'outputs': [REPORT]},
        'resources': D._resources(config)}}


def _verify_report(report, plan):
    H.require(type(report) is dict and report.get('schema') == REPORT_SCHEMA,
              'Unexpected subdivision report schema', 'VALIDATION_FAILED')
    H.require(report.get('status') == 'pass' and report.get('source_unchanged') is True
              and report.get('implementation_unchanged') is True,
              'Subdivision report did not pass source/implementation protection', 'VALIDATION_FAILED')
    storage, restore = report.get('storage_protection'), report.get('restore')
    H.require(type(storage) is dict and storage.get('exact_equal') is True
              and storage.get('mask_verified') is True,
              'Storage exact comparison or the precise intervention mask was not verified', 'VALIDATION_FAILED')
    for name in ('before_sha256', 'after_sha256', 'masked_before_sha256', 'masked_during_sha256'):
        H.require(type(storage.get(name)) is str and re.fullmatch(r'[0-9a-f]{64}', storage[name]) is not None,
                  'Typed storage digest unavailable', 'VALIDATION_FAILED')
    H.require(storage['before_sha256'] == storage['after_sha256']
              and storage['masked_before_sha256'] == storage['masked_during_sha256'],
              'Typed storage equality differs despite boolean protection flags', 'VALIDATION_FAILED')
    H.require(type(restore) is dict and restore.get('success') is True,
              'Subdivision restore was not certified', 'VALIDATION_FAILED')
    H.require(report.get('source_saved') is False,
              'Report must explicitly certify that source was not saved', 'VALIDATION_FAILED')
    H.require(type(report.get('plan')) is dict and _same_json(report['plan'], plan),
              'Report plan differs from the exact validated request', 'CONFLICT')


def execute(args, run_dir, config, plan, roles):
    req = request(roles, config)
    request_path = run_dir / 'request.json'
    D._write_json(request_path, req)
    _unchanged(roles, config['implementation'])
    call = [sys.executable, '-B', str(D.PUBLIC), '--jobs-dir', str(run_dir / 'jobs'),
            '--timeout', str(args.timeout), 'request', str(request_path)]
    try:
        try:
            process = subprocess.run(call, capture_output=True, timeout=args.timeout + 60,
                                     creationflags=D.NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            D._write_bytes(run_dir / 'stdout.txt', exc.stdout or b'')
            D._write_bytes(run_dir / 'stderr.txt', exc.stderr or b'')
            D._write_json(run_dir / 'result.json', {'ok': False, 'error': {'code': 'TIMEOUT',
                'message': 'Public CLI host allowance expired; owned worker terminal state not certified'}})
            raise H.ContractError('TIMEOUT', 'Public CLI exceeded host allowance; inspect owned job logs') from exc
        D._write_bytes(run_dir / 'stdout.txt', process.stdout)
        D._write_bytes(run_dir / 'stderr.txt', process.stderr)
        try:
            public = H.parse_json(process.stdout.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            D._write_json(run_dir / 'result.json', {'ok': False, 'error': {'code': 'WORKER_FAILED',
                'message': 'Invalid public JSON; complete raw stdout/stderr retained'},
                'public_returncode': process.returncode})
            raise H.ContractError('WORKER_FAILED', 'Public CLI returned invalid JSON; inspect raw logs') from exc
        D._write_json(run_dir / 'result.json', public)
        job_id, outputs, worker = D._verify_public(public, process.returncode, req, run_dir)
        document = outputs[REPORT]
        report = H.read_json(document['file'])
        _verify_report(report, plan)
        H.descriptor(document['file'], document['expected_sha256'])
        # Validate inputs again even if their use by the worker was successful.
        D._resources(config)
    finally:
        # Also guard failure/timeout/cancellation paths; never report acceptance
        # using a new implementation identity after submission.
        _unchanged(roles, config['implementation'])
    result = {'schema': 'hrig.subdivision.cli.result.v1', 'frontend_version': VERSION,
              'ok': True, 'status': 'pass', 'job_id': job_id,
              'FPS_claim': False, 'edit_success_bundle': False, 'source_saved': False,
              'artifacts': {'request': H.descriptor(request_path),
                            'public_result': H.descriptor(run_dir / 'result.json'),
                            'stdout': H.descriptor(run_dir / 'stdout.txt'),
                            'stderr': H.descriptor(run_dir / 'stderr.txt'), 'outputs': outputs},
              'identity': roles, 'implementation': config['implementation'],
              'plan': plan, 'verification': worker,
              'report_verification': {'schema': report['schema'], 'status': report['status'],
                  'source_unchanged': True, 'implementation_unchanged': True,
                  'storage_exact_equal': True, 'mask_verified': True, 'restore_success': True,
                  'source_saved': False, 'plan_exact_equal': True},
              'scope': 'One Subsurf viewport intervention and downstream response; not pure modifier cost'}
    D._write_json(run_dir / 'host-result.json', result)
    return result


def describe():
    paths = {'frontend': HERE, 'host_helpers': SCRIPTS / 'host_contract.py',
             'diagnostic_host': SCRIPTS / 'diagnostic_cli.py', 'entry': ENTRY,
             'core': CORE, 'observation_contract': PACKAGE / 'diagnostic_contract.py',
             'public_cli': D.PUBLIC}
    return {'schema': 'hrig.subdivision.cli.describe.v1', 'frontend_version': VERSION,
            'backend': 'existing public extension.run', 'commands': ['describe', 'run'],
            'Blender_executed': False, 'public_rigging_commands_added': False,
            'root': str(ROOT), 'plan_schema': PLAN_SCHEMA, 'report_schema': REPORT_SCHEMA,
            'runner_params': ['input', 'plan', 'external_inputs', 'implementation', 'addon_root'],
            'roles': {name: H.descriptor(path) if path.is_file() else
                      {'file': str(path), 'status': 'missing'} for name, path in paths.items()},
            'native_validation': 'not_asserted_by_discovery',
            'limitations': ['SAVED_DISK_V1; source is not saved; unsaved GUI changes are not observed',
                'Exact wrapper plan; one explicit Subsurf index/name/object; expected show_viewport=true',
                'Target vertex_indices must be []; topology-changing output is not indexed by old vertices',
                'enabled is an explicit boolean; worker verifies actual intervention and precise storage mask',
                'Complete downstream response is observed, not pure Subsurf execution time or FPS',
                'Body/other-object protection requires independent declared witnesses',
                'Frame restoration does not certify exact simulation-memory/cache restoration',
                'No automatic repair, source save, edit bundle, installation or permission changes']}


def main(argv=None):
    run_dir = None
    try:
        args = parser().parse_args(argv)
        if args.command == 'describe':
            response = describe()
        else:
            H.number(args.timeout, 'timeout', minimum=0.001)
            # Never write to a reused directory. Once owned, failed preflight
            # also gets a structured failure.json instead of losing evidence.
            run_dir = D._run_dir(args.out_dir)
            roles, impl = identity(), implementation()
            config, plan = preflight(args, impl)
            _unchanged(roles, impl)
            response = execute(args, run_dir, config, plan, roles)
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
        failure = {'schema': 'hrig.subdivision.cli.result.v1', 'frontend_version': VERSION,
                   'ok': False, 'status': 'failed', 'FPS_claim': False,
                   'edit_success_bundle': False, 'error': {'code': code, 'message': str(exc)},
                   'run_directory': str(run_dir) if run_dir else None,
                   'recovery': 'retain owned evidence; no intervention, restore or worker-termination success inferred'}
        if run_dir is not None:
            try:
                D._write_json(run_dir / 'failure.json', failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False, allow_nan=False))
        return exc.exit_code if isinstance(exc, H.ContractError) else H.CODES.get(code, 5)


if __name__ == '__main__':
    raise SystemExit(main())
