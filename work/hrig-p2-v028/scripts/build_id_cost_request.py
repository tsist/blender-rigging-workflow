"""Build a pinned ID-cost extension.run request without executing Blender.

This is a standalone request builder, not a rigctl profile product command.
It refuses output reuse. Native execution and report acceptance remain separate.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve()
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parent
PACKAGE = ROOT / 'addon' / 'humanoid_rigging_probe'
ENTRY = SCRIPTS / 'run_id_cost.py'
MAX_PLAN_BYTES = 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_RESOURCES = 100


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load installed host module: ' + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = _module(SCRIPTS / 'diagnostic_cli.py', '_hrig_id_cost_builder_diagnostic_host')
H = D.H
PROTOCOL = D.PUBLIC.parent / 'protocol.py'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise H.ContractError('INVALID_REQUEST', message)


def parser():
    result = Parser(description=__doc__)
    for name in ('source', 'expected-sha256', 'plan', 'expected-plan-sha256', 'out-dir'):
        result.add_argument('--' + name, required=True)
    result.add_argument('--external-inputs', required=True,
                        help='raw JSON array of exact {file, expected_sha256}; explicitly [] if none')
    result.add_argument('--timeout', type=float, default=180.0,
                        help='timeout for the printed future public CLI invocation; no job is submitted')
    return result


def _dedupe(rows):
    result, seen = [], {}
    for row in rows:
        H.check_descriptor(row)
        key = str(Path(row['file']).resolve()).casefold()
        H.require(key not in seen or seen[key] == row,
                  'Conflicting implementation descriptors for one file', 'CONFLICT')
        if key not in seen:
            seen[key] = row
            result.append(row)
    return H.check_descriptors(result)


def implementation():
    rows = D.implementation() + [H.descriptor(SCRIPTS / 'host_contract.py'),
                                 H.descriptor(ENTRY), H.descriptor(PROTOCOL)]
    result = _dedupe(rows)
    H.require(H.descriptor(PACKAGE / 'id_cost.py') in result,
              'ID-cost contract is outside the addon implementation closure', 'CONFLICT')
    return result


def identity():
    return {name: H.descriptor(path) for name, path in {
        'builder': HERE, 'diagnostic_host': SCRIPTS / 'diagnostic_cli.py',
        'host_helpers': SCRIPTS / 'host_contract.py', 'entry': ENTRY,
        'public_cli': D.PUBLIC, 'protocol': PROTOCOL,
    }.items()}


def _unchanged(roles, impl):
    H.require(identity() == roles and implementation() == impl,
              'Builder, implementation or public entry changed during construction', 'CONFLICT')


def _id_contract():
    # Relative imports need a private package, never a cached addon from another
    # work version. The reviewed __init__ import does not register the addon.
    namespace = '_hrig_id_cost_builder_contract_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(
        namespace, PACKAGE / '__init__.py', submodule_search_locations=[str(PACKAGE)])
    H.require(spec is not None and spec.loader is not None,
              'Cannot load the installed ID-cost package', 'NOT_FOUND')
    package = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = package
    try:
        spec.loader.exec_module(package)
        return importlib.import_module(namespace + '.id_cost')
    finally:
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + '.'):
                del sys.modules[name]


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


def build_request(*, source, expected_sha256, plan, expected_plan_sha256, external_inputs):
    """Return (request, metadata) after pure-host exact-input validation; no writes."""
    H.require(not D.STAGED and D.ROOT.resolve() == ROOT.resolve(),
              'Builder requires the installed work/<version>/scripts layout', 'UNSUPPORTED')
    roles, impl = identity(), implementation()
    source_descriptor = H.descriptor(source, expected_sha256)
    H.require(Path(source_descriptor['file']).suffix.lower() == '.blend',
              'Source must be an explicitly saved .blend file')
    plan_descriptor = H.descriptor(plan, expected_plan_sha256)
    plan_path = Path(plan_descriptor['file'])
    H.require(plan_path.stat().st_size <= MAX_PLAN_BYTES, 'Plan exceeds the 1 MiB worker limit')
    parsed = H.parse_json(plan_path.read_text(encoding='utf-8-sig'))
    try:
        validated = _id_contract().validate_plan(parsed)
    except ValueError as exc:
        raise H.ContractError('INVALID_REQUEST', str(exc)) from exc
    H.require(json.dumps(parsed, sort_keys=True, allow_nan=False) ==
              json.dumps(validated, sort_keys=True, allow_nan=False),
              'ID-cost plan validation must not silently normalize input')
    H.require(type(external_inputs) is str, 'external-inputs must be original JSON text')
    H.require(len(external_inputs) <= MAX_JSON_BYTES and
              len(external_inputs.encode('utf-8')) <= MAX_JSON_BYTES,
              'External-input JSON exceeds the 32 MiB host limit')
    external = H.parse_json(external_inputs)
    H.check_descriptors(external)
    config = {'input': source_descriptor, 'plan': plan_descriptor,
              'implementation': impl,
              'external_inputs': [H.descriptor(row['file'], row['expected_sha256']) for row in external],
              'addon_root': str(ROOT / 'addon')}
    H.fields(config, ('input', 'plan', 'implementation', 'external_inputs', 'addon_root'),
             label='ID-cost runner params')
    resources = D._resources(config)
    H.require(len(resources) <= MAX_RESOURCES,
              'Public extension resource budget exceeds 100 descriptors')
    request = {'schema_version': '1.0', 'command': 'extension.run', 'params': {
        'manifest': {'script': roles['entry'], 'trust': 'execute_local_python_without_sandbox',
                     'params': config, 'outputs': ['output-diagnostic.json']},
        'resources': resources}}
    H.require(len(_json_bytes(request)) <= MAX_JSON_BYTES, 'Request exceeds the 32 MiB host limit')
    _unchanged(roles, impl)
    # Rehash source/plan/external inputs after parsing; do not adopt drift.
    D._resources(config)
    metadata = {'identity': roles, 'implementation': impl, 'validated_plan': validated,
                'resource_count': len(resources),
                'closure_scope': 'D addon/character/fixture/inspection closure plus worker, H and protocol; '
                    'stdlib/bpy supplied by pinned native runtime; unused public lazy command branches not claimed'}
    return request, metadata


def _new_directory(raw):
    path = Path(raw)
    H.require(path.is_absolute(), '--out-dir must be absolute')
    H.require(not path.is_symlink(), '--out-dir cannot be a symlink', 'CONFLICT')
    path = path.resolve()
    H.require(path.parent.is_dir(), '--out-dir parent must already exist', 'NOT_FOUND')
    H.require(not path.exists(), '--out-dir must be entirely new', 'CONFLICT')
    return path


def main(argv=None):
    run_dir = None
    try:
        args = parser().parse_args(argv)
        H.number(args.timeout, 'timeout', minimum=0.001)
        destination = _new_directory(args.out_dir)
        request, metadata = build_request(source=args.source, expected_sha256=args.expected_sha256,
            plan=args.plan, expected_plan_sha256=args.expected_plan_sha256,
            external_inputs=args.external_inputs)
        run_dir = D._run_dir(str(destination))
        _unchanged(metadata['identity'], metadata['implementation'])
        D._resources(request['params']['manifest']['params'])
        request_path = run_dir / 'request.json'
        D._write_bytes(request_path, _json_bytes(request))
        _unchanged(metadata['identity'], metadata['implementation'])
        D._resources(request['params']['manifest']['params'])
        result = {'schema': 'hrig.id-cost.request-builder.v1', 'ok': True,
                  'status': 'built_not_executed', 'Blender_executed': False,
                  'native_validation': 'not_run', 'FPS_claim': False, 'edit_success_bundle': False,
                  'request': H.descriptor(request_path), 'identity': metadata['identity'],
                  'implementation': metadata['implementation'], 'resource_count': metadata['resource_count'],
                  'closure_scope': metadata['closure_scope'],
                  'profile': metadata['validated_plan']['profile'],
                  'public_cli_argv': [sys.executable, '-B', str(D.PUBLIC), '--jobs-dir', str(run_dir / 'jobs'),
                                      '--timeout', str(args.timeout), 'request', str(request_path)],
                  'limitations': ['Request construction does not certify native input admission, report acceptance or performance',
                      'No embedded source scripts, cache changes, source save or Blender execution by this builder',
                      'Explicit static descriptors do not certify inherited weak/dynamic dependency closure',
                      'New closure is not retroactive validation of previously executed requests']}
        D._write_json(run_dir / 'builder-result.json', result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, H.ContractError):
            code = exc.code
        elif isinstance(exc, KeyboardInterrupt):
            code = 'CANCELLED'
        elif isinstance(exc, OSError):
            code = 'IO_ERROR'
        elif isinstance(exc, (ValueError, TypeError, KeyError)):
            code = 'INVALID_REQUEST'
        else:
            code = 'WORKER_FAILED'
        failure = {'schema': 'hrig.id-cost.request-builder.v1', 'ok': False,
                   'status': 'failed', 'Blender_executed': False, 'native_validation': 'not_run',
                   'error': {'code': code, 'message': str(exc)},
                   'run_directory': str(run_dir) if run_dir else None}
        if run_dir is not None:
            try:
                D._write_json(run_dir / 'failure.json', failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False, allow_nan=False))
        return exc.exit_code if isinstance(exc, H.ContractError) else H.CODES.get(code, 5)


if __name__ == '__main__':
    raise SystemExit(main())
