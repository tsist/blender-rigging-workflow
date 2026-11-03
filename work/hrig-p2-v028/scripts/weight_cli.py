"""Pure host weight candidate frontend; only existing public extension.run.

Independent cold jobs certify bounded storage evidence, not complete character
preservation, visual deformation, GUI playback or performance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
import rigctl as host
from host_contract import (CODES, ContractError, check_descriptor, check_descriptors,
                           context, descriptor, fields, number, read_json, require,
                           text, walk_descriptors)

SCHEMA = 'hrig.weights.bundle.v1'
OP_SCHEMA = 'hrig.weights.operation.v1'
VERSION = '0.14.0-candidate'
ROOT, SCRIPTS, ADDON, PROJECT = host.ROOT, host.SCRIPTS, host.ADDON, host.PROJECT
OBSERVER = PROJECT / 'tools' / 'blenderctl'
REPORT = 'output-weights.json'
CANDIDATE = 'output-candidate.blend'
CASES = ('keyed_mesh_patch', 'locked_deform_positive', 'reject_shared_mesh',
         'reject_locked_change', 'reject_non_normalized', 'reject_budget',
         'reject_stale_row', 'reject_nan', 'rollback_injected',
         'single_owner_fake_user', 'inherit_unselected_invalid_weight',
         'reject_selected_invalid_weight', 'reject_shape_key_consumer',
         'reject_modifier_consumer', 'untouched_consumers_positive',
         'orphan_mixed_retention', 'preserve_sum_positive',
         'reject_preserve_sum_conflict', 'reject_preserve_sum_changed',
         'reject_preserve_new_zero', 'reject_preserve_consumer',
         'preserve_rollback_injected')
LIMITATIONS = ['bounded raw deform-weight editing of local single-user Mesh; existing shape keys permitted',
               'no complete-character preservation claim; native writer and independent cold evidence define verified scope',
               'no general rig retrofit, normalization repair, automatic weighting or human deformation certification',
               'patch requires explicit external_inputs exactly matching observed native static file targets/SHA; no implicit new hashes',
               'missing/directory/cache/unknown dependencies remain inherited observations; dependency closure is not certified',
               'inspect is read-only observation without an external-file SHA contract; it does not certify dependency closure',
               'GUI deferred by user; FPS not measured',
               'trusted local Python without OS sandbox; SAVED_DISK_V1 cannot observe unsaved GUI edits']


def parser():
    p = host.Parser(description='Isolated weight candidate CLI; backend: existing public extension.run')
    commands = p.add_subparsers(dest='command', required=True, parser_class=host.Parser)
    commands.add_parser('describe', help='host discovery; never executes Blender')
    i = commands.add_parser('inspect', help='read-only saved raw weights inspection')
    i.add_argument('--source', required=True)
    i.add_argument('--expected-sha256', required=True)
    for name in ('object', 'rig', 'scene', 'view-layer'):
        i.add_argument('--' + name, required=True)
    pch = commands.add_parser('patch', help='exact explicit weight rows plus independent cold validation')
    pch.add_argument('--source', required=True)
    pch.add_argument('--expected-sha256', required=True)
    pch.add_argument('--operation', required=True)
    pch.add_argument('--expected-operation-sha256', required=True)
    f = commands.add_parser('fixture', help='one bounded positive/rejection/rollback fixture plus cold')
    f.add_argument('--case', choices=CASES, required=True)
    v = commands.add_parser('validate', help='new independent cold for an immutable candidate bundle')
    v.add_argument('--bundle', required=True)
    v.add_argument('--expected-sha256', required=True)
    for command in (i, pch, f, v):
        command.add_argument('--out-dir', required=True)
        command.add_argument('--timeout', type=float, default=180)
    return p


def operation(value):
    fields(value, ('schema', 'context', 'target', 'patch', 'external_inputs', 'inherited_missing_weak_references'), label='weight operation')
    require(value['schema'] == OP_SCHEMA, 'Expected ' + OP_SCHEMA)
    # These are caller-pinned dependencies, never hashes silently observed and
    # accepted by the frontend. Revalidate on request reconstruction and bundle
    # commit as well as initial preflight.
    check_descriptors(value['external_inputs'])
    missing=value['inherited_missing_weak_references']
    require(type(missing) is list and len(missing)<=1024, 'Explicit bounded inherited missing weak paths required')
    seen_missing=set()
    for item in missing:
        require(type(item) is str and Path(item).is_absolute() and len(item)<=4096,
                'Inherited missing path must be absolute')
        key=str(Path(item).resolve()).casefold()
        require(key not in seen_missing and not Path(item).exists(),
                'Inherited missing path duplicated or now exists; re-inspect and pin existing file', 'CONFLICT')
        seen_missing.add(key)
    context(value['context'])
    fields(value['target'], ('object', 'rig'), label='weight target')
    for name in ('object', 'rig'):
        text(value['target'][name], 'target.' + name)
    patch = value['patch']
    preserve_sum = type(patch) is dict and patch.get('schema') == 'hrig.weights.patch.v2'
    if preserve_sum:
        fields(patch, ('schema', 'expected_topology_sha256', 'expected_groups_sha256',
                       'rows', 'max_influences', 'sum_policy'), label='weight patch')
        require(patch['sum_policy'] == 'preserve_source_deform_sum',
                'Expected preserve_source_deform_sum policy')
    else:
        fields(patch, ('schema', 'expected_topology_sha256', 'expected_groups_sha256',
                       'rows', 'max_influences'), label='weight patch')
        require(patch['schema'] == 'hrig.weights.patch.v1', 'Expected hrig.weights.patch.v1')
    for name in ('expected_topology_sha256', 'expected_groups_sha256'):
        require(type(patch[name]) is str and re.fullmatch(r'[0-9a-f]{64}', patch[name]) is not None,
                name + ' must be a lowercase SHA256')
    require(type(patch['max_influences']) is int and 1 <= patch['max_influences'] <= 4096,
            'max_influences must be an integer in [1,4096]')
    require(type(patch['rows']) is list and 1 <= len(patch['rows']) <= 65536,
            'One through 65536 explicit rows required')
    seen = set()
    for row in patch['rows']:
        if preserve_sum:
            fields(row, ('index', 'before', 'after', 'expected_deform_sum'), label='weight row')
            number(row['expected_deform_sum'], 'expected_deform_sum')
            require(row['expected_deform_sum'] > 0, 'expected_deform_sum must be positive')
        else:
            fields(row, ('index', 'before', 'after'), label='weight row')
        require(type(row['index']) is int and 0 <= row['index'] < 500000,
                'row.index must be an integer in [0,499999]')
        require(row['index'] not in seen, 'Duplicate row index: ' + str(row['index']))
        seen.add(row['index'])
        for role in ('before', 'after'):
            require(type(row[role]) is dict and len(row[role]) <= 8192,
                    role + ' must be a bounded group-name weight map')
            for name, weight in row[role].items():
                text(name, role + ' group name')
                number(weight, role + '.' + name, 0, 1)
        if preserve_sum:
            requested_sum = math.fsum(row['after'].values())
            require(requested_sum > 0, 'Requested after deform sum must be positive')
            require(abs(requested_sum - row['expected_deform_sum']) <= 1e-6,
                    'Requested after deform weights must preserve expected_deform_sum within 1e-6')
        else:
            require(abs(math.fsum(row['after'].values()) - 1.0) <= 1e-6,
                    'Requested after deform weights must sum to one within 1e-6')
        require(sum(x > 0 for x in row['after'].values()) <= patch['max_influences'],
                'Requested after weights exceed max_influences')
    return value


def implementation():
    paths = sorted((ADDON / 'humanoid_rigging_probe').glob('*.py'))
    require(paths and ADDON / 'humanoid_rigging_probe' / '__init__.py' in paths,
            'Candidate addon closure is missing', 'NOT_FOUND')
    paths += [SCRIPTS / 'character_state.py', SCRIPTS / 'weight_fixture.py',
              OBSERVER / 'inspection.py', OBSERVER / 'protocol.py']
    return check_descriptors([descriptor(path) for path in paths])


def identity():
    paths = {'frontend': SCRIPTS / 'rigctl.py', 'weight_cli': Path(__file__),
             'host_contract': SCRIPTS / 'host_contract.py', 'entry': SCRIPTS / 'run_weights.py',
             'character_state': SCRIPTS / 'character_state.py',
             'weight_fixture': SCRIPTS / 'weight_fixture.py', 'public_wrapper': host.PUBLIC,
             'inspection': OBSERVER / 'inspection.py', 'protocol': OBSERVER / 'protocol.py'}
    return {role: descriptor(path) for role, path in paths.items()}


def unchanged(roles, impl):
    require(roles == identity(), 'Weight frontend/entry/helper/public role identity changed', 'CONFLICT')
    require(impl == implementation(), 'Exact weight implementation closure changed', 'CONFLICT')


def request(roles, impl, config, outputs):
    config = {**config, 'addon_root': str(ADDON), 'observer_root': str(OBSERVER), 'implementation': impl}
    resources = list(impl)
    for item in walk_descriptors(config):
        if item not in resources:
            resources.append(item)
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
            'Public weight request differs from exact role/config/resource/output contract', 'CONFLICT')
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
    return read_json(record['outputs'][REPORT]['file'])


def source(value, expected):
    result = descriptor(value, expected)
    require(Path(result['file']).suffix.lower() == '.blend', 'Source must be a .blend')
    return result


def producer_config(mode, inputs, target=None, context_value=None, case=None):
    config = {'mode': mode}
    if mode in ('inspect', 'patch'):
        config['input'] = inputs['source']
    if mode == 'patch':
        config['operation'] = inputs['operation']
    elif mode == 'inspect':
        config.update(target=target, context=context_value)
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
    require(report.get('implementation') == impl and
            report.get('entry_sha') == roles['entry']['expected_sha256'],
            'Producer implementation/entry binding differs', 'CONFLICT')
    if mode == 'inspect':
        require(report.get('status') == 'pass' and report.get('source_unchanged') is True,
                'Read-only weight inspection did not pass unchanged-source check', 'VALIDATION_FAILED')
    else:
        require(report.get('status') == 'candidate_waiting_independent_cold' and
                report.get('candidate') == producer['outputs'][CANDIDATE],
                'Weight producer is not paired with its candidate', 'VALIDATION_FAILED')
        if mode == 'patch':
            require(report.get('operation_ok') is True,
                    'Requested patch was not applied successfully', 'VALIDATION_FAILED')
        else:
            require(type(report.get('operation_ok')) is bool,
                    'Fixture report must explicitly distinguish operation success/refusal', 'VALIDATION_FAILED')


def check_cold(report, record):
    require(report.get('status') == 'pass' and report.get('source_unchanged') is True and
            type(record['worker_result']) is dict and record['worker_result'].get('status') == 'pass',
            'Independent weight cold validation did not pass', 'VALIDATION_FAILED')


def check_bundle(value):
    fields(value, ('schema', 'frontend_version', 'status', 'mode', 'identity', 'implementation',
                   'inputs', 'producer_config', 'producer', 'report', 'verification', 'created_utc',
                   'GUI', 'FPS', 'limitations'),
           ('cold', 'candidate', 'validated_bundle'), 'weight bundle')
    require(value['schema'] == SCHEMA and value['frontend_version'] == VERSION and
            value['status'] == 'succeeded' and value['mode'] in ('inspect', 'patch', 'fixture'),
            'Expected immutable succeeded weight bundle')
    roles, impl, mode = value['identity'], value['implementation'], value['mode']
    unchanged(roles, impl)
    for _ in walk_descriptors(value):
        pass
    inputs, config = value['inputs'], value['producer_config']
    if mode == 'patch':
        fields(inputs, ('source', 'operation'), label='patch bundle inputs')
        operation(read_json(inputs['operation']['file']))
        expected_config = producer_config(mode, inputs)
    elif mode == 'fixture':
        fields(inputs, (), label='fixture bundle inputs')
        require(config.get('case') in CASES, 'Invalid fixture bundle case')
        expected_config = producer_config(mode, inputs, case=config['case'])
    else:
        fields(inputs, ('source',), label='inspect bundle inputs')
        fields(config, ('mode', 'input', 'target', 'context'), label='inspect producer config')
        fields(config['target'], ('object', 'rig'), label='inspect target')
        fields(config['context'], ('scene', 'view_layer'), label='inspect context')
        for group in ('target', 'context'):
            for name, item in config[group].items():
                text(item, 'inspect.' + name)
        expected_config = producer_config(mode, inputs, config['target'], config['context'])
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
    if args.command in ('inspect', 'patch'):
        inputs['source'] = source(args.source, args.expected_sha256)
    if args.command == 'patch':
        inputs['operation'] = descriptor(args.operation, args.expected_operation_sha256)
        operation(read_json(inputs['operation']['file']))
    elif args.command == 'inspect':
        for name in ('object', 'rig', 'scene', 'view_layer'):
            text(getattr(args, name), name)
    elif args.command == 'validate':
        inputs['bundle'] = descriptor(args.bundle, args.expected_sha256)
        prior = check_bundle(read_json(inputs['bundle']['file']))
        require(prior['mode'] != 'inspect',
                'Read-only inspection bundles have no candidate; validate accepts only patch/fixture bundles',
                'UNSUPPORTED')
        return inputs, prior
    return inputs, None


def execute(args, run_dir, inputs, prior):
    roles, impl = identity(), implementation()
    if prior is None:
        mode = args.command
        target = {'object': args.object, 'rig': args.rig} if mode == 'inspect' else None
        ctx = {'scene': args.scene, 'view_layer': args.view_layer} if mode == 'inspect' else None
        config = producer_config(mode, inputs, target, ctx, getattr(args, 'case', None))
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
    paths = {'entry': SCRIPTS / 'run_weights.py', 'character_state': SCRIPTS / 'character_state.py',
             'weight_fixture': SCRIPTS / 'weight_fixture.py', 'frontend': Path(__file__)}
    return {'schema': 'hrig.weights.cli.describe.v1', 'frontend_version': VERSION,
            'backend': 'existing public extension.run', 'public_weight_commands_added': False,
            'commands': ['describe', 'inspect', 'patch', 'fixture', 'validate'], 'fixture_cases': list(CASES),
            'operation_schema': OP_SCHEMA, 'bundle_schema': SCHEMA,
            'operation_fields': ['schema', 'context', 'target', 'patch', 'external_inputs', 'inherited_missing_weak_references'],
            'inherited_missing_weak_references_contract': 'required explicit absolute missing path list, [] when none; exact native missing set must have weak provenance and no identified live consumer; origins partial, unresolved status retained, no closure or runtime availability claim',
            'external_inputs_schema': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['file', 'expected_sha256'], 'properties': {
                    'file': {'type': 'string', 'minLength': 1},
                    'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}}},
            'external_inputs_contract': 'required explicit absolute existing file descriptors; resolved-path duplicates rejected; exact native static file target/SHA set checked by worker; rehashed before bundle commit and validate',
            'context_fields': ['scene', 'view_layer', 'frame', 'subframe'],
            'target_fields': ['object', 'rig'],
            'patch_fields': ['schema', 'expected_topology_sha256', 'expected_groups_sha256', 'rows', 'max_influences'],
            'row_fields': ['index', 'before', 'after'],
            'patch_schemas': ['hrig.weights.patch.v1', 'hrig.weights.patch.v2'],
            'patch_v2_fields': ['schema', 'expected_topology_sha256', 'expected_groups_sha256',
                                'rows', 'max_influences', 'sum_policy'],
            'patch_v2_row_fields': ['index', 'before', 'after', 'expected_deform_sum'],
            'patch_v2_sum_policy': 'preserve_source_deform_sum',
            'patch_v2_sum_contract': 'expected_deform_sum is finite and positive; native real-deform before fsum must match exactly; requested and float32 stored after sums must be positive and remain within absolute 1e-6; v1 keeps sum-to-one behavior',
            'validation': 'strict fields/duplicate JSON keys/finite numbers; patch/fixture requires independent cold',
            'inspect_validate': 'UNSUPPORTED: read-only inspect bundles contain no candidate',
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
