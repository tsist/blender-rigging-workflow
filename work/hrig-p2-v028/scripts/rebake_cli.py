"""Serial unbaked pose authoring and original-range native Cloth rebake.

Pure host composition of existing pose/cache CLIs.  It never edits cache flags,
releases a cache, starts a custom bpy worker, or adopts baked authoring sources.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import subprocess
import sys

sys.dont_write_bytecode = True
import rigctl as host
import pose_cli as pose
import cache_cli as cache
import motion_cli as motion
from host_contract import (CODES, ContractError, check_descriptor, check_descriptors,
                           descriptor, fields, number, parse_json, read_json, require,
                           text, walk_descriptors)

PLAN_SCHEMA = 'hrig.author-rebake.plan.v1'
SCHEMA = 'hrig.author-rebake.bundle.v1'
STAGES = (('source_cache_inspect', 'cache', 'inspect'), ('pose_inspect', 'pose', 'inspect'),
          ('pose_apply', 'pose', 'apply'), ('author_cache_inspect', 'cache', 'inspect'),
          ('cache_bake', 'cache', 'bake'))
LIMITATIONS = [
    'existing pose/current-storage and native Cloth cached-witness contracts only',
    'source and author candidates must be unbaked; baked output is a replay copy only',
    'original cache range/settings/collision/Action unchanged; no release/force/disk or flag setters',
    'cache identity certifies explicit producer/cold full-coordinate witnesses, not every unsampled frame',
    'failure retains only independently verified unbaked author candidate; no cache rollback guarantee',
    'partial execution is a failed overall operation; failed native candidates are diagnostic only',
    'native static files are SHA-bound; inherited missing weak references remain unresolved',
    'GUI deferred by user; FPS not measured; no continuous collision, artistic or complete Aon acceptance',
    'trusted local Python without OS sandbox; disk SHA cannot observe unsaved GUI changes']


def unique_descriptors(rows):
    result = {}
    for row in rows:
        check_descriptor(row)
        key = str(Path(row['file']).resolve()).casefold()
        require(key not in result or result[key] == row, 'Conflicting file identities in workunit closure', 'CONFLICT')
        result[key] = row
    return [result[key] for key in sorted(result)]


def identity():
    return unique_descriptors([descriptor(__file__), *pose.identity().values(), *cache.identity().values(),
                               *pose.implementation(), *cache.implementation()])


def unchanged(inputs, roles):
    check_descriptors(inputs)
    require(identity() == roles, 'Workunit/pose/cache implementation identity changed', 'CONFLICT')


def plan(value):
    fields(value, ('schema', 'context', 'rig', 'pose_operation', 'cache_targets', 'clip_request', 'witness'), label='rebake plan')
    require(value['schema'] == PLAN_SCHEMA, 'Expected ' + PLAN_SCHEMA)
    motion.motion_context(value['context'])
    text(value['rig'], 'rig')
    for key in ('pose_operation', 'clip_request', 'witness'):
        check_descriptor(value[key])
    op = pose.operation(read_json(value['pose_operation']['file']))
    picked = cache.witness(read_json(value['witness']['file']))
    clip = motion.canonical('canonical_request', read_json(value['clip_request']['file']))
    targets = cache.canonical('canonical_targets', value['cache_targets'])
    require(op['context'] == value['context'] == picked['context'] and op['rig'] == value['rig'],
            'Plan/pose operation/witness context or rig conflict', 'CONFLICT')
    require({row['object'] for row in targets} <= set(picked['proxy_objects']),
            'Every selected Cloth object must be witnessed as a proxy')
    require({row['bone'] for row in clip['tracks']} <= set(picked['bones']), 'Every original clip input bone must be witnessed')
    return op, picked, clip, targets


def preflight(args):
    number(args.timeout, 'timeout', 1, 86400)
    source = pose.source(args.source, args.expected_sha256)
    plan_desc = descriptor(args.plan, args.expected_plan_sha256)
    value = read_json(plan_desc['file'])
    op, picked, clip, targets = plan(value)
    inputs = {'source': source, 'plan': plan_desc}
    frozen = unique_descriptors([source, plan_desc, *walk_descriptors(value), *op['external_inputs']])
    return inputs, value, op, picked, clip, targets, frozen, identity()


def parser():
    p = host.Parser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True, parser_class=host.Parser)
    commands.add_parser('describe', help='host discovery only; never starts Blender')
    run = commands.add_parser('run', help='source inspect, exact pose authoring, author inspect and original-range bake')
    for name in ('source', 'expected-sha256', 'plan', 'expected-plan-sha256', 'out-dir'):
        run.add_argument('--' + name, required=True)
    run.add_argument('--timeout', type=float, default=900)
    return p


def _stage_report(record):
    module = pose if record['family'] == 'pose' else cache
    response = read_json(check_descriptor(record['response'])['file'])
    require(record['exit_code'] == 0 and response.get('ok') is True and response.get('status') == 'succeeded'
            and response.get('mode') == record['mode'] and response.get('bundle') == record['bundle'],
            'Subdomain response/exit/bundle differs', 'VALIDATION_FAILED')
    reader = read_json if record['family'] == 'pose' else motion.read_report
    bundle = module.check_bundle(reader(check_descriptor(record['bundle'])['file']))
    require(bundle['mode'] == record['mode'] and bundle['inputs'] == record['inputs']
            and response.get('report') == bundle['report'], 'Subdomain exact source/operation/coverage binding differs', 'CONFLICT')
    if record['mode'] in ('apply', 'bake'):
        require(response.get('operation_ok') is True and response.get('candidate') == bundle.get('candidate'),
                'Subdomain candidate not independently accepted', 'VALIDATION_FAILED')
    else:
        require(response.get('candidate') is None, 'Read-only stage returned an authoring candidate', 'CONFLICT')
    return bundle, reader(bundle['report']['file'])


def dispatch(run, ordinal, name, family, mode, inputs, timeout):
    """One existing CLI process; its timeout handles public workers, no unknown-process kill."""
    directory = run / ('%02d-' % ordinal + name.replace('_', '-'))
    argv = [sys.executable, '-B', str(host.SCRIPTS / 'rigctl.py'), family, mode,
            '--source', inputs['source']['file'], '--expected-sha256', inputs['source']['expected_sha256']]
    for key in ('selection', 'operation', 'clip_request', 'witness'):
        if key in inputs:
            flag = key.replace('_', '-')
            argv.extend(['--' + flag, inputs[key]['file'], '--expected-' + flag + '-sha256', inputs[key]['expected_sha256']])
    argv.extend(['--out-dir', str(directory), '--timeout', str(timeout)])
    # No outer subprocess timeout: killing the domain host could orphan its public
    # job. Each existing domain enforces the requested timeout for its own jobs.
    completed = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding='utf-8',
                               creationflags=host.NO_WINDOW)
    stdout = run / ('%02d-' % ordinal + name + '-stdout.json')
    stderr = run / ('%02d-' % ordinal + name + '-stderr.txt')
    host.write_log(stdout, completed.stdout)
    host.write_log(stderr, completed.stderr)
    # Preserve raw argv/exit/log identities even when parsing or domain checking
    # fails; a nonzero child is not allowed to erase its actual attempt evidence.
    host.write_new(run / ('%02d-' % ordinal + name + '-ATTEMPT.json'), {
        'stage': name, 'family': family, 'mode': mode, 'argv': argv,
        'exit_code': completed.returncode, 'run_directory': str(directory),
        'stdout': descriptor(stdout), 'stderr': descriptor(stderr)})
    try:
        response = parse_json(completed.stdout)
    except (ValueError, TypeError) as error:
        raise ContractError('WORKER_FAILED', 'Invalid subdomain stdout for ' + name + ': ' + str(error)) from error
    require(type(response) is dict, 'Subdomain stdout must be a JSON object', 'WORKER_FAILED')
    if completed.returncode != 0 or response.get('ok') is not True:
        code = response.get('error', {}).get('code', 'WORKER_FAILED')
        raise ContractError(code if code in CODES else 'WORKER_FAILED',
                            'Stage ' + name + ' failed: ' + str(response.get('error', response)))
    check_descriptor(response.get('bundle'))
    record = {'name': name, 'family': family, 'mode': mode, 'inputs': inputs, 'argv': argv,
              'exit_code': completed.returncode, 'response': descriptor(stdout), 'stderr': descriptor(stderr),
              'bundle': response['bundle']}
    _stage_report(record)
    host.write_new(run / ('%02d-' % ordinal + name + '-STAGE.json'), record)
    return record


def inspect_cache(report, value, targets):
    require(report.get('mode') == 'inspect' and report.get('status') == 'pass'
            and report.get('context') == value['context'] and report.get('rig') == value['rig']
            and report.get('targets') == targets, 'Cache inspect context/rig/target binding differs', 'CONFLICT')
    observed = report.get('inspection', {})
    require(observed.get('ok') is True, 'Native cache admission failed', 'VALIDATION_FAILED')
    rows = report.get('derived_cache_initial')
    require(type(rows) is list and len(rows) == len(targets), 'Unbaked native cache coverage missing', 'VALIDATION_FAILED')
    require([{k: row[k] for k in ('object', 'modifier', 'index')} for row in rows] == targets,
            'Unbaked native cache identities differ', 'CONFLICT')
    for row in rows:
        require(row['cache'].get('is_baked') is False and row['cache'].get('is_baking') is False,
                'Baked/baking source cannot be used for authoring', 'UNSUPPORTED')
    fields_seen = observed['snapshot']['fields']
    require(len(fields_seen) == len(targets), 'Native cache authoring coverage differs')
    for row in fields_seen:
        author = row['cache_authoring']
        require(author.get('use_disk_cache') is False and author.get('use_external') is False,
                'Disk/external cache source cannot be used for authoring', 'UNSUPPORTED')
    return observed


def inspect_pose(report, value, op):
    require(report.get('mode') == 'inspect' and report.get('status') == 'pass'
            and report.get('context') == value['context'] and report.get('rig') == value['rig'],
            'Pose inspect context/rig differs', 'CONFLICT')
    observed = report.get('inspection', {})
    request = pose.canonical('canonical_request', op['request'])
    require(observed.get('ok') is True and observed.get('snapshot_sha256') == request['expected_snapshot_sha256'],
            'User pose operation snapshot is stale; operation was not regenerated', 'CONFLICT')
    require(set(observed.get('fields', {})) == {row['bone'] for row in request['rotations']},
            'Pose inspect selected bone set differs', 'CONFLICT')
    for row in request['rotations']:
        current = observed['fields'][row['bone']]
        require(current['rotation_mode'] == row['rotation_mode'] and current['value'] == row['before'],
                'User pose before/mode differs; operation was not regenerated', 'CONFLICT')


def execute(args, run, prepared, state):
    inputs, value, op, picked, clip, targets, frozen, roles = prepared
    state['identity'] = roles
    state['inputs'] = inputs
    state['frozen_inputs'] = frozen
    selection = {'schema': cache.SELECTION_SCHEMA, 'context': value['context'], 'rig': value['rig'], 'targets': targets}
    pose_selection = {'schema': pose.SELECTION_SCHEMA, 'context': value['context'], 'rig': value['rig'],
                      'bones': [row['bone'] for row in pose.canonical('canonical_request', op['request'])['rotations']]}
    cache.selection(selection)
    pose.selection(pose_selection)
    host.write_new(run / 'cache-selection.json', selection)
    host.write_new(run / 'pose-selection.json', pose_selection)
    cache_selection_desc, pose_selection_desc = descriptor(run / 'cache-selection.json'), descriptor(run / 'pose-selection.json')
    state['generated_inputs'] = [cache_selection_desc, pose_selection_desc]
    reports = {}
    bundles = {}
    for ordinal, (name, family, mode) in enumerate(STAGES, 1):
        state['failed_stage'] = name
        unchanged(frozen + state['generated_inputs'], roles)
        if name == 'source_cache_inspect':
            stage_inputs = {'source': inputs['source'], 'selection': cache_selection_desc}
        elif name == 'pose_inspect':
            stage_inputs = {'source': inputs['source'], 'selection': pose_selection_desc}
        elif name == 'pose_apply':
            stage_inputs = {'source': inputs['source'], 'operation': value['pose_operation']}
        elif name == 'author_cache_inspect':
            stage_inputs = {'source': state['pose_verified_candidate'], 'selection': cache_selection_desc}
        else:
            author = reports['author_cache_inspect']['inspection']
            operation = {'schema': cache.OP_SCHEMA, 'context': value['context'], 'rig': value['rig'],
                         'request': cache.canonical('canonical_request', {'schema': 'hrig.cloth-cache.bake.v1',
                             'expected_snapshot_sha256': author['snapshot_sha256'], 'targets': targets}),
                         'external_inputs': op['external_inputs'],
                         'inherited_missing_weak_references': op['inherited_missing_weak_references']}
            cache.operation(operation)
            host.write_new(run / 'cache-operation.json', operation)
            operation_desc = descriptor(run / 'cache-operation.json')
            state['generated_inputs'].append(operation_desc)
            stage_inputs = {'source': state['last_verified_unbaked'], 'operation': operation_desc,
                            'clip_request': value['clip_request'], 'witness': value['witness']}
        record = dispatch(run, ordinal, name, family, mode, stage_inputs, args.timeout)
        bundle, report = _stage_report(record)
        unchanged(frozen + state['generated_inputs'], roles)
        if name in ('source_cache_inspect', 'author_cache_inspect'):
            observed = inspect_cache(report, value, targets)
            native_range = observed['range']
            require(picked['frames'][0] == native_range['frame_start'] and picked['frames'][-1] == native_range['frame_end'],
                    'Witness must include original native cache range endpoints', 'CONFLICT')
            if name == 'author_cache_inspect':
                original = reports['source_cache_inspect']['inspection']
                require(observed['snapshot']['fields'] == original['snapshot']['fields'] and observed['range'] == original['range'],
                        'Pose authoring changed selected cache/Cloth/collision settings or range', 'CONFLICT')
                state['last_verified_unbaked'] = state['pose_verified_candidate']
        elif name == 'pose_inspect':
            inspect_pose(report, value, op)
        elif name == 'pose_apply':
            state['pose_verified_candidate'] = bundle['candidate']
        else:
            state['baked_candidate'] = bundle['candidate']
        reports[name], bundles[name] = report, bundle
        stage_path = run / ('%02d-' % ordinal + name + '-STAGE.json')
        state['steps'].append({'name': name, 'record': descriptor(stage_path), 'bundle': record['bundle']})
        host.write_new(run / ('%02d-' % ordinal + name + '-CHECKPOINT.json'), {
            'status': 'stage_verified', 'stage': name, 'steps': state['steps'],
            'source_authoring': inputs['source'], 'pose_verified_candidate': state['pose_verified_candidate'],
            'last_verified_unbaked': state['last_verified_unbaked'], 'baked_candidate': state['baked_candidate']})
    state['failed_stage'] = 'final_verification'
    unchanged(frozen + state['generated_inputs'], roles)
    final = {'schema': SCHEMA, 'status': 'succeeded', 'inputs': inputs, 'frozen_inputs': frozen,
             'identity': roles, 'generated_inputs': state['generated_inputs'], 'steps': state['steps'],
             'source_authoring': inputs['source'], 'unbaked_candidate': state['last_verified_unbaked'],
             'baked_candidate': state['baked_candidate'], 'default_continuation': state['last_verified_unbaked'],
             'created_utc': datetime.now(timezone.utc).isoformat(), 'GUI': 'deferred_by_user', 'FPS': 'not_measured',
             'complete_improved_Aon': False, 'artistic_adoption': False, 'limitations': LIMITATIONS}
    check_bundle(final)
    host.write_new(run / 'bundle.json', final)
    state['failed_stage'] = None
    return {'ok': True, 'status': 'succeeded', 'bundle': descriptor(run / 'bundle.json'),
            'unbaked_candidate': final['unbaked_candidate'], 'baked_candidate': final['baked_candidate'],
            'default_continuation': final['default_continuation'], 'GUI': final['GUI'], 'FPS': final['FPS'],
            'complete_improved_Aon': False, 'artistic_adoption': False}


def check_bundle(value):
    fields(value, ('schema', 'status', 'inputs', 'frozen_inputs', 'identity', 'generated_inputs', 'steps',
                  'source_authoring', 'unbaked_candidate', 'baked_candidate', 'default_continuation',
                  'created_utc', 'GUI', 'FPS', 'complete_improved_Aon', 'artistic_adoption', 'limitations'), label='rebake bundle')
    require(value['schema'] == SCHEMA and value['status'] == 'succeeded', 'Expected succeeded rebake bundle')
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured'
            and value['complete_improved_Aon'] is False and value['artistic_adoption'] is False
            and value['limitations'] == LIMITATIONS, 'Rebake acceptance scope differs', 'CONFLICT')
    fields(value['inputs'], ('source', 'plan'), label='rebake inputs')
    unchanged(value['frozen_inputs'] + value['generated_inputs'], value['identity'])
    for _ in walk_descriptors(value):
        pass
    source, plan_desc = value['inputs']['source'], value['inputs']['plan']
    p = read_json(plan_desc['file'])
    op, picked, clip, targets = plan(p)
    expected_frozen = unique_descriptors([source, plan_desc, *walk_descriptors(p), *op['external_inputs']])
    require(value['frozen_inputs'] == expected_frozen and value['source_authoring'] == source,
            'Rebake source/input closure differs', 'CONFLICT')
    require(len(value['steps']) == len(STAGES), 'Rebake stage chain incomplete')
    records, bundles, reports = {}, {}, {}
    for row, (name, family, mode) in zip(value['steps'], STAGES):
        fields(row, ('name', 'record', 'bundle'), label='rebake stage reference')
        require(row['name'] == name, 'Rebake stage order differs', 'CONFLICT')
        record = read_json(row['record']['file'])
        require(record['name'] == name and record['family'] == family and record['mode'] == mode
                and record['bundle'] == row['bundle'], 'Rebake stage record binding differs', 'CONFLICT')
        for _ in walk_descriptors(record):
            pass
        bundle, report = _stage_report(record)
        records[name], bundles[name], reports[name] = record, bundle, report
    cache_selection = {'schema': cache.SELECTION_SCHEMA, 'context': p['context'], 'rig': p['rig'], 'targets': targets}
    pose_selection = {'schema': pose.SELECTION_SCHEMA, 'context': p['context'], 'rig': p['rig'],
                      'bones': [row['bone'] for row in pose.canonical('canonical_request', op['request'])['rotations']]}
    for name, selection in (('source_cache_inspect', cache_selection), ('pose_inspect', pose_selection),
                            ('author_cache_inspect', cache_selection)):
        require(read_json(records[name]['inputs']['selection']['file']) == selection, 'Derived selection differs', 'CONFLICT')
    for name in ('source_cache_inspect', 'pose_inspect', 'pose_apply'):
        require(records[name]['inputs']['source'] == source, 'Authoring stage adopted a different source', 'CONFLICT')
    require(records['pose_apply']['inputs']['operation'] == p['pose_operation'], 'User pose operation was replaced', 'CONFLICT')
    inspect_pose(reports['pose_inspect'], p, op)
    first = inspect_cache(reports['source_cache_inspect'], p, targets)
    second = inspect_cache(reports['author_cache_inspect'], p, targets)
    require(first['range'] == second['range'] and first['snapshot']['fields'] == second['snapshot']['fields'],
            'Original Cloth/cache authoring fields changed', 'CONFLICT')
    require(picked['frames'][0] == second['range']['frame_start'] and picked['frames'][-1] == second['range']['frame_end'],
            'Original cache range endpoints not witnessed', 'CONFLICT')
    author = bundles['pose_apply']['candidate']
    require(records['author_cache_inspect']['inputs']['source'] == author
            and records['cache_bake']['inputs']['source'] == author == value['unbaked_candidate'] == value['default_continuation'],
            'Unbaked authoring candidate chain differs', 'CONFLICT')
    bake_inputs = records['cache_bake']['inputs']
    require(bake_inputs['clip_request'] == p['clip_request'] and bake_inputs['witness'] == p['witness'],
            'Original clip/witness descriptor changed', 'CONFLICT')
    actual_operation = cache.operation(read_json(bake_inputs['operation']['file']))
    expected_operation = {'schema': cache.OP_SCHEMA, 'context': p['context'], 'rig': p['rig'],
        'request': cache.canonical('canonical_request', {'schema': 'hrig.cloth-cache.bake.v1',
            'expected_snapshot_sha256': second['snapshot_sha256'], 'targets': targets}),
        'external_inputs': op['external_inputs'], 'inherited_missing_weak_references': op['inherited_missing_weak_references']}
    require(actual_operation == expected_operation, 'Fresh author snapshot/external/cache operation differs', 'CONFLICT')
    require(value['baked_candidate'] == bundles['cache_bake']['candidate'] and value['baked_candidate'] != author,
            'Baked replay copy and unbaked author roles differ', 'CONFLICT')
    generated = unique_descriptors([records['source_cache_inspect']['inputs']['selection'],
                                    records['pose_inspect']['inputs']['selection'], bake_inputs['operation']])
    require(unique_descriptors(value['generated_inputs']) == generated, 'Generated input closure differs', 'CONFLICT')
    return value


def describe():
    return {'schema': 'hrig.author-rebake.describe.v1', 'commands': ['describe', 'run'],
            'plan_schema': PLAN_SCHEMA, 'bundle_schema': SCHEMA, 'backend': 'existing pose/cache CLI and public extension.run',
            'plan_fields': ['schema', 'context', 'rig', 'pose_operation', 'cache_targets', 'clip_request', 'witness'],
            'stages': [row[0] for row in STAGES], 'native_validation': 'not_asserted_by_discovery',
            'subdomain_versions': {'pose': pose.VERSION, 'cache': cache.VERSION}, 'limitations': LIMITATIONS}


def main(argv=None):
    run = None
    state = {'steps': [], 'failed_stage': 'preflight', 'pose_verified_candidate': None,
             'last_verified_unbaked': None, 'baked_candidate': None}
    try:
        args = parser().parse_args(argv)
        if args.command == 'describe':
            response = describe()
        else:
            prepared = preflight(args)
            run = host.new_run_dir(args.out_dir)
            response = execute(args, run, prepared, state)
        print(json.dumps(response, ensure_ascii=False, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        code = error.code if isinstance(error, ContractError) else 'CANCELLED' if isinstance(error, KeyboardInterrupt) else 'TIMEOUT' if isinstance(error, subprocess.TimeoutExpired) else 'IO_ERROR' if isinstance(error, OSError) else 'INVALID_REQUEST' if isinstance(error, (ValueError, KeyError, TypeError)) else 'WORKER_FAILED'
        last = state['last_verified_unbaked']
        recovery_error = None
        if last is not None:
            try:
                check_descriptor(last)
            except (ValueError, OSError) as failed:
                recovery_error, last = str(failed), None
        failure = {'ok': False, 'status': 'partial' if state['steps'] else 'failed', 'error': {'code': code, 'message': str(error)},
                   'failed_stage': state['failed_stage'], 'run_directory': str(run) if run else None,
                   'source_authoring': state.get('inputs', {}).get('source'), 'steps': state['steps'],
                   'pose_verified_candidate': state['pose_verified_candidate'], 'last_verified_unbaked': last,
                   'recovery_integrity_error': recovery_error, 'baked_candidate': None,
                   'candidate_policy': 'no succeeded workunit bundle; failed/interrupted native candidates diagnostic only; reopen verified unbaked input',
                   'GUI': 'deferred_by_user', 'FPS': 'not_measured', 'complete_improved_Aon': False, 'artistic_adoption': False}
        if run is not None:
            for ordinal, (name, _, _) in enumerate(STAGES, 1):
                if name == state['failed_stage']:
                    attempt = run / ('%02d-' % ordinal + name + '-ATTEMPT.json')
                    if attempt.is_file():
                        failure['failed_attempt'] = descriptor(attempt)
            try:
                host.write_new(run / 'failure.json', failure)
            except OSError:
                pass
        print(json.dumps(failure, ensure_ascii=False, allow_nan=False))
        return error.exit_code if isinstance(error, ContractError) else CODES[code]


if __name__ == '__main__':
    raise SystemExit(main())
