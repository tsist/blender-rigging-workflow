"""Pure host settings frontend over the existing public extension.run.

Discovery never starts Blender. Only a pinned producer and an independent cold
reader can certify a candidate within the declared finite authoring domain.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
import rigctl as host
from host_contract import (CODES, ContractError, check_descriptor, check_descriptors,
                           descriptor, fields, number, parse_json, read_json,
                           require, walk_descriptors)

SCHEMA = 'hrig.settings.bundle.v1'
REPORT_SCHEMA = 'hrig.settings.report.v1'
VERSION = '0.16.0'
ROOT, SCRIPTS, ADDON, PROJECT = host.ROOT, host.SCRIPTS, host.ADDON, host.PROJECT
OBSERVER = PROJECT / 'tools' / 'blenderctl'
REPORT = 'output-settings.json'
CANDIDATE = 'output-candidate.blend'
SELECTION_SCHEMA = 'hrig.existing-control-properties.selection.v1'
OP_SCHEMA = 'hrig.existing-control-properties.operation.v1'
PATCH_SCHEMA = 'hrig.existing-control-properties.patch.v1'
HELPERS = ('character_state.py', 'motion_state.py', 'dynamics_state.py',
           'existing_control_state.py', 'existing_control_fixture.py',
           'run_existing_controls.py', 'run_weights.py', 'run_styles.py')
LIMITATIONS = [
    'finite AON_HAIR_SWITCHES_V1: two existing integer 0/1 PoseBone properties and exactly four declared native driver effects',
    'no property creation/deletion/type/UI changes, keyframes, driver edits, direct driven flag writes, or Cloth solver/cache edits',
    'all other observed authoring fields, original Actions/AnimData, Key values, pose/rest, geometry and weights remain strict',
    'native typed reader/writer and alias checks do not certify arbitrary scripts or hidden expression dependencies',
    'independent cold certifies declared saved authoring state, actual property/flag values, roundtrip/no-op and frame0 evaluated witnesses',
    'cache runtime/currentness, continuous simulation/contact, same hairstyle motion and editable secondary-bone Actions are not certified',
    'explicit static dependencies required; inherited missing weak references remain unresolved',
    'GUI deferred by user; FPS/performance improvement and complete improved Aon not claimed; no installation/publication',
    'trusted local Python without OS sandbox; SAVED_DISK_V1 cannot observe unsaved GUI edits']


def fixture_cases():
    path = SCRIPTS / 'existing_control_fixture.py'
    require(path.is_file(), 'Settings fixture source missing', 'NOT_FOUND')
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CASE_IDS' for t in item.targets):
            values = ast.literal_eval(item.value)
            require(type(values) in (tuple, list) and values and
                    all(type(x) is str and x for x in values) and len(set(values)) == len(values),
                    'Invalid static settings fixture CASE_IDS')
            return tuple(values)
    raise ContractError('UNSUPPORTED', 'No static CASE_IDS in settings fixture')


def settings_contract():
    path = ADDON / 'humanoid_rigging_probe' / 'existing_control_contract.py'
    require(path.is_file(), 'Settings contract source missing', 'NOT_FOUND')
    spec = importlib.util.spec_from_file_location('_hrig_host_existing_control_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(method, value):
    try:
        return getattr(settings_contract(), method)(value)
    except ValueError as error:
        raise ContractError('INVALID_REQUEST', getattr(error, 'code', 'INVALID_REQUEST') + ': ' + str(error)) from error


def selection(value):
    return canonical('canonical_selection', value)


def operation(value):
    result = canonical('canonical_operation', value)
    check_descriptors(result['external_inputs'])
    missing = result['inherited_missing_weak_references']
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
    return result


def parser():
    p = host.Parser(description='Isolated existing-control settings CLI; backend: existing public extension.run')
    commands = p.add_subparsers(dest='command', required=True, parser_class=host.Parser)
    commands.add_parser('describe', help='host discovery; never executes Blender')
    inspect = commands.add_parser('inspect', help='read-only saved settings and native effect inspection')
    inspect.add_argument('--source', required=True)
    inspect.add_argument('--expected-sha256', required=True)
    inspect.add_argument('--selection', required=True)
    inspect.add_argument('--expected-selection-sha256', required=True)
    apply = commands.add_parser('apply', help='exact settings patch plus independent cold validation')
    apply.add_argument('--source', required=True)
    apply.add_argument('--expected-sha256', required=True)
    apply.add_argument('--operation', required=True)
    apply.add_argument('--expected-operation-sha256', required=True)
    fixture = commands.add_parser('fixture', help='one explicit native fixture plus independent cold')
    fixture.add_argument('--case', required=True)
    validate = commands.add_parser('validate', help='new independent cold for an immutable candidate bundle')
    validate.add_argument('--bundle', required=True)
    validate.add_argument('--expected-sha256', required=True)
    for command in (inspect, apply, fixture, validate):
        command.add_argument('--out-dir', required=True)
        command.add_argument('--timeout', type=float, default=180)
    return p


def implementation():
    paths = sorted((ADDON / 'humanoid_rigging_probe').glob('*.py'))
    require(paths and ADDON / 'humanoid_rigging_probe' / '__init__.py' in paths,
            'Candidate addon closure is missing', 'NOT_FOUND')
    paths += [SCRIPTS / name for name in HELPERS]
    paths += [OBSERVER / 'inspection.py', OBSERVER / 'protocol.py']
    return check_descriptors([descriptor(path) for path in paths])


def identity():
    paths = {'frontend': SCRIPTS / 'rigctl.py', 'settings_cli': Path(__file__),
             'host_contract': SCRIPTS / 'host_contract.py', 'entry': SCRIPTS / 'run_existing_controls.py',
             'public_wrapper': host.PUBLIC, 'inspection': OBSERVER / 'inspection.py',
             'protocol': OBSERVER / 'protocol.py'}
    paths.update({Path(name).stem: SCRIPTS / name for name in HELPERS})
    paths.update({'addon_' + p.stem: p for p in sorted((ADDON / 'humanoid_rigging_probe').glob('*.py'))})
    return {role: descriptor(path) for role, path in paths.items()}


def unchanged(roles, impl):
    require(roles == identity(), 'Settings frontend/entry/helper/public role identity changed', 'CONFLICT')
    require(impl == implementation(), 'Exact settings implementation closure changed', 'CONFLICT')


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
        # Public extension resources do not recursively inspect operation files.
        # Pin every declared external static input in producer and cold requests.
        for item in loaded['external_inputs']:
            if item not in resources:
                resources.append(item)
    check_descriptors(resources)
    return {'schema_version': '1.0', 'command': 'extension.run', 'params': {
        'manifest': {'script': roles['entry'], 'trust': 'execute_local_python_without_sandbox',
                     'params': config, 'outputs': outputs}, 'resources': resources}}


def check_request(value, roles, impl, config, outputs):
    require(value == request(roles, impl, config, outputs),
            'Public settings request differs from exact role/config/resource/output contract', 'CONFLICT')
    check_descriptors(value['params']['resources'])


def read_report(path):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= 128 * 1024 * 1024,
            'Settings native report exceeds the explicit 128 MiB bound')
    return parse_json(path.read_text(encoding='utf-8-sig'))


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
    return read_report(record['outputs'][REPORT]['file'])


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


def check_roundtrip(report):
    roundtrip = report.get('roundtrip')
    require(type(roundtrip) is dict and roundtrip.get('ok') is True,
            'Settings report lacks successful independent native roundtrip', 'VALIDATION_FAILED')
    no_op = roundtrip.get('no_op')
    require(type(no_op) is dict and no_op.get('ok') is True and no_op.get('changed') is False,
            'Settings report lacks unchanged same-value no-op', 'VALIDATION_FAILED')


def check_producer(report, producer, roles, impl, mode):
    require(type(report) is dict and report.get('schema') == REPORT_SCHEMA,
            'Unexpected settings producer report schema', 'CONFLICT')
    require(report.get('implementation') == impl and report.get('entry_sha') == roles['entry']['expected_sha256'],
            'Producer implementation/entry binding differs', 'CONFLICT')
    require(report.get('mode') == mode and type(producer.get('worker_result')) is dict and
            producer['worker_result'].get('mode') == mode and
            producer['worker_result'].get('status') == report.get('status'),
            'Producer native report/public result mode or status differs', 'CONFLICT')
    if mode == 'inspect':
        require(report.get('status') == 'pass' and report.get('source_unchanged') is True and
                report.get('inspection', {}).get('ok') is True,
                'Read-only settings inspection did not pass unchanged-source check', 'VALIDATION_FAILED')
        return
    require(report.get('status') == 'candidate_waiting_independent_cold' and
            report.get('candidate') == producer['outputs'][CANDIDATE],
            'Settings producer is not paired with its candidate', 'VALIDATION_FAILED')
    require(type(report.get('operation_ok')) is bool,
            'Settings producer must distinguish operation success/refusal', 'VALIDATION_FAILED')
    if mode == 'apply':
        require(report['operation_ok'] is True, 'Requested settings apply was refused', 'VALIDATION_FAILED')
    if mode == 'fixture':
        require(report.get('fixture_validation', {}).get('ok') is True,
                'Settings fixture did not pass independent native validation', 'VALIDATION_FAILED')
    if report['operation_ok']:
        require(report.get('settings_validation', {}).get('ok') is True and
                report.get('authoring_protection', {}).get('ok') is True,
                'Settings producer lacks exact settings and authoring protection', 'VALIDATION_FAILED')
        check_roundtrip(report)
    require(report.get('evaluation_comparison', {}).get('ok') is True and
            report.get('evaluation_comparison', {}).get('pose_exact') is True,
            'Settings producer lacks preserved frame0 evaluated witnesses', 'VALIDATION_FAILED')


def check_cold(report, record, producer_report):
    require(type(report) is dict and report.get('schema') == REPORT_SCHEMA,
            'Unexpected settings cold report schema', 'CONFLICT')
    require(report.get('status') == 'pass' and report.get('source_unchanged') is True and
            type(record.get('worker_result')) is dict and record['worker_result'].get('status') == 'pass',
            'Independent settings cold validation did not pass', 'VALIDATION_FAILED')
    require(report.get('mode') == 'cold' and record['worker_result'].get('mode') == 'cold' and
            report.get('implementation') == implementation() and
            report.get('entry_sha') == identity()['entry']['expected_sha256'],
            'Independent cold native identity/mode differs', 'CONFLICT')
    require(report.get('operation_ok') is producer_report['operation_ok'] and
            report.get('after_snapshot') == producer_report.get('after_snapshot'),
            'Independent cold stored state or operation result differs from its producer', 'CONFLICT')
    require(report.get('evaluation_comparison', {}).get('ok') is True and
            report.get('evaluation_comparison', {}).get('pose_exact') is True,
            'Independent cold lacks native evaluated comparison', 'VALIDATION_FAILED')
    if report['operation_ok']:
        require(report.get('settings_validation', {}).get('ok') is True and
                report['settings_validation'] == producer_report.get('settings_validation'),
                'Independent cold native settings/flags/definitions differ from producer', 'CONFLICT')
        check_roundtrip(report)
    else:
        require(report.get('fixture_validation', {}).get('ok') is True,
                'Independent cold refusal fixture did not pass', 'VALIDATION_FAILED')


def check_bundle(value):
    fields(value, ('schema', 'frontend_version', 'status', 'mode', 'identity', 'implementation',
                   'inputs', 'producer_config', 'producer', 'report', 'verification', 'created_utc',
                   'GUI', 'FPS', 'limitations'),
           ('cold', 'candidate', 'validated_bundle'), 'settings bundle')
    require(value['schema'] == SCHEMA and value['frontend_version'] == VERSION and
            value['status'] == 'succeeded' and value['mode'] in ('inspect', 'apply', 'fixture'),
            'Expected immutable succeeded settings bundle')
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
        expected = producer_config(mode, inputs)
    elif mode == 'fixture':
        fields(inputs, (), label='fixture bundle inputs')
        require(config.get('case') in fixture_cases(), 'Invalid settings fixture bundle case')
        expected = producer_config(mode, inputs, case=config['case'])
    else:
        fields(inputs, ('source', 'selection'), label='inspect bundle inputs')
        selection(read_json(inputs['selection']['file']))
        expected = producer_config(mode, inputs)
    require(config == expected, 'Producer config is not bound to bundle inputs', 'CONFLICT')
    outputs = [REPORT] if mode == 'inspect' else [REPORT, CANDIDATE]
    produced = public_record(value['producer'], roles, impl, config, outputs)
    check_producer(produced, value['producer'], roles, impl, mode)
    require(value['report'] == value['producer']['outputs'][REPORT],
            'Bundle report is not the producer artifact', 'CONFLICT')
    if mode == 'inspect':
        require('candidate' not in value and 'cold' not in value and value['verification'] == produced,
                'Inspect bundle may contain only read-only inspection evidence', 'CONFLICT')
    else:
        require(value.get('candidate') == value['producer']['outputs'][CANDIDATE] and 'cold' in value,
                'Candidate is not the producer artifact', 'CONFLICT')
        cold = value['cold']
        require(cold['job_id'] != value['producer']['job_id'],
                'Cold job must be independent of the producer', 'CONFLICT')
        evidence = public_record(cold, roles, impl,
                                 cold_config(value['producer'], inputs.get('operation')), [REPORT])
        check_cold(evidence, cold, produced)
        require(value['verification'] == evidence, 'Bundle cold evidence differs', 'CONFLICT')
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
        require(args.case in fixture_cases(), 'Unknown settings fixture case')
    elif args.command == 'validate':
        inputs['bundle'] = descriptor(args.bundle, args.expected_sha256)
        prior = check_bundle(read_json(inputs['bundle']['file']))
        require(prior['mode'] != 'inspect',
                'Read-only inspection bundles have no candidate; validate accepts only apply/fixture bundles', 'UNSUPPORTED')
        return inputs, prior
    return inputs, None


def execute(args, run_dir, inputs, prior):
    roles, impl = identity(), implementation()
    if prior is None:
        mode = args.command
        config = producer_config(mode, inputs, case=getattr(args, 'case', None))
        outputs = [REPORT] if mode == 'inspect' else [REPORT, CANDIDATE]
        req = request(roles, impl, config, outputs)
        check_request(req, roles, impl, config, outputs)
        producer = host.public_job(run_dir, 'producer', req, args.timeout)
        produced = public_record(producer, roles, impl, config, outputs)
        check_producer(produced, producer, roles, impl, mode)
        final = {'schema': SCHEMA, 'frontend_version': VERSION, 'status': 'succeeded',
                 'mode': mode, 'identity': roles, 'implementation': impl, 'inputs': inputs,
                 'producer_config': config, 'producer': producer, 'report': producer['outputs'][REPORT],
                 'verification': produced, 'created_utc': datetime.now(timezone.utc).isoformat(),
                 'GUI': 'deferred_by_user', 'FPS': 'not_measured', 'limitations': LIMITATIONS}
    else:
        final = {**prior, 'validated_bundle': inputs['bundle'],
                 'created_utc': datetime.now(timezone.utc).isoformat()}
        mode, producer, inputs = prior['mode'], prior['producer'], prior['inputs']
        produced = read_report(producer['outputs'][REPORT]['file'])
    unchanged(roles, impl)
    if mode != 'inspect':
        config = cold_config(producer, inputs.get('operation'))
        cold = host.public_job(run_dir, 'cold', request(roles, impl, config, [REPORT]), args.timeout)
        evidence = public_record(cold, roles, impl, config, [REPORT])
        check_cold(evidence, cold, produced)
        final.update(cold=cold, candidate=producer['outputs'][CANDIDATE], verification=evidence)
    unchanged(roles, impl)
    check_bundle(final)
    host.write_new(run_dir / 'bundle.json', final)
    return {'ok': True, 'status': 'succeeded', 'mode': mode,
            'bundle': descriptor(run_dir / 'bundle.json'), 'report': final['report'],
            'candidate': final.get('candidate'), 'producer_job_id': producer['job_id'],
            'cold_job_id': final.get('cold', {}).get('job_id'), 'operation_ok': produced.get('operation_ok'),
            'GUI': final['GUI'], 'FPS': final['FPS']}


def describe():
    paths = {'entry': SCRIPTS / 'run_existing_controls.py',
             'settings_state': SCRIPTS / 'existing_control_state.py',
             'settings_fixture': SCRIPTS / 'existing_control_fixture.py',
             'host_settings_contract': ADDON / 'humanoid_rigging_probe' / 'existing_control_contract.py',
             'frontend': Path(__file__)}
    try:
        cases = {'status': 'available', 'values': list(fixture_cases())}
    except (ContractError, SyntaxError, ValueError) as error:
        cases = {'status': 'missing_or_invalid', 'values': [], 'message': str(error)}
    return {'schema': 'hrig.settings.cli.describe.v1', 'frontend_version': VERSION,
            'backend': 'existing public extension.run', 'public_settings_commands_added': False,
            'commands': ['describe', 'inspect', 'apply', 'fixture', 'validate'],
            'adapter': 'AON_HAIR_SWITCHES_V1', 'fixture_cases': cases,
            'operation_schema': OP_SCHEMA, 'selection_schema': SELECTION_SCHEMA,
            'patch_schema': PATCH_SCHEMA, 'bundle_schema': SCHEMA,
            'operation_fields': ['schema', 'selection', 'patch', 'external_inputs', 'inherited_missing_weak_references'],
            'selection_fields': ['schema', 'adapter', 'context', 'rig', 'control_bone', 'properties', 'effects'],
            'patch_fields': ['schema', 'expected_snapshot_sha256', 'expected_definition_sha256', 'changes'],
            'change_fields': ['key', 'before', 'after'], 'typed_value': {'type': 'int', 'values': [0, 1]},
            'consumer_policy': 'exactly four declared typed readers/flag writers; all other selected consumers/writers refused',
            'external_inputs_contract': 'exact native static file descriptors and inherited missing weak origins checked independently; every existing file included as public resource',
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
