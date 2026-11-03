"""Pure host motion candidate frontend; only existing public extension.run.

Independent cold jobs certify bounded motion/storage evidence, not complete
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
from host_contract import (CODES, ContractError, check_descriptor, check_descriptors,
                           context, descriptor, fields, number, read_json, require,
                           text, walk_descriptors, parse_json)

SCHEMA = 'hrig.motion.bundle.v1'
VERSION = '0.8.0'
ROOT, SCRIPTS, ADDON, PROJECT = host.ROOT, host.SCRIPTS, host.ADDON, host.PROJECT
OBSERVER = PROJECT / 'tools' / 'blenderctl'
REPORT = 'output-motion.json'
CANDIDATE = 'output-candidate.blend'
SELECTION_SCHEMA = 'hrig.motion-clip.selection.v1'
OP_SCHEMA = 'hrig.motion-clip.operation.v1'
LIMITATIONS = [
    'partial: one new rotation-only Action with one OBJECT slot; no existing Action/NLA replacement',
    'raw pose modes/channels/rest/constraints/drivers/weights/body geometry remain unchanged at frame0',
    'replay uses sequential fresh-process evaluation; runtime Cloth cache is not persisted or certified for random access',
    'complete selected evaluated mesh coordinates are bounded and self-validated; no continuous/visual/collision quality claim',
    'create requires exact static-file inputs; inherited missing weak references remain unresolved',
    'GUI deferred by user; FPS not measured; complete improved Aon not claimed',
    'trusted local Python without OS sandbox; SAVED_DISK_V1 cannot observe unsaved GUI edits']



def motion_context(value):
    context(value)
    require(value['frame'] == 0 and value['subframe'] == 0, 'Motion context requires frame0/subframe0')
    return value


def witness(value):
    fields(value, ('schema', 'context', 'frames', 'meshes', 'bones', 'proxy_objects'), label='motion replay witness')
    require(value['schema'] == 'hrig.motion-clip.replay.v1', 'Expected bounded motion replay witness')
    motion_context(value['context'])
    frames = value['frames']
    require(type(frames) is list and 1 <= len(frames) <= 64 and
            all(type(f) is int and 1 <= f <= 250 for f in frames) and
            frames == list(range(frames[0], frames[0] + len(frames))), 'Replay frames must be 1 through 64 consecutive integer frames')
    for key, minimum, maximum in (('meshes', 1, 8), ('bones', 1, 256), ('proxy_objects', 0, 4)):
        names = value[key]
        require(type(names) is list and minimum <= len(names) <= maximum, 'Bounded explicit ' + key + ' required')
        for name in names:
            text(name, key + ' item', 1024)
        require(len(set(names)) == len(names), 'Duplicate ' + key + ' identity')
    return value


def decode_coordinates(item):
    import base64, hashlib, math, re, struct, zlib
    fields(item, ('vertices', 'world_f32_sha256', 'finite', 'world_f32_zlib_base64'), label='coordinate witness')
    count = item['vertices']
    require(type(count) is int and 0 <= count <= 4000000 and item['finite'] is True, 'Invalid complete coordinate count/finite claim')
    require(type(item['world_f32_sha256']) is str and re.fullmatch('[0-9a-f]{64}', item['world_f32_sha256']) is not None, 'Invalid coordinate SHA')
    require(type(item['world_f32_zlib_base64']) is str and len(item['world_f32_zlib_base64']) <= 80000000, 'Coordinate encoding exceeds budget')
    size = count * 12
    try:
        packed = base64.b64decode(item['world_f32_zlib_base64'], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, size + 1)
        require(len(raw) == size and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, 'Incomplete or trailing coordinate data')
        require(hashlib.sha256(raw).hexdigest() == item['world_f32_sha256'], 'Coordinate bytes SHA differs')
        require(all(math.isfinite(v) for p in struct.iter_unpack('<3f', raw) for v in p), 'Coordinate payload contains nonfinite f32')
    except (ValueError, TypeError, zlib.error) as error:
        raise ContractError('VALIDATION_FAILED', 'Invalid complete coordinate payload: ' + str(error)) from error
    return raw


def read_report(path):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= 128 * 1024 * 1024,
            'Motion native report exceeds the explicit 128 MiB bound')
    return parse_json(path.read_text(encoding='utf-8-sig'))


def linear_input_values(track, frame):
    """Independent component LINEAR/CONSTANT expectation and binary32 bound.

    Explicit keys and held endpoints are exact float32 values. Between keys,
    eight binary32 epsilons cover the bounded time-ratio/subtract/multiply/add
    rounding, scaled by endpoint magnitudes. This is not a pose/mesh tolerance
    and does not normalize the intermediate quaternion components.
    """
    keys = track['keys']
    if frame <= keys[0]['frame']:
        return keys[0]['value'], [0.] * len(keys[0]['value'])
    if frame >= keys[-1]['frame']:
        return keys[-1]['value'], [0.] * len(keys[-1]['value'])
    for left, right in zip(keys, keys[1:]):
        if frame == right['frame']:
            return right['value'], [0.] * len(right['value'])
        if left['frame'] <= frame < right['frame']:
            fraction = (frame - left['frame']) / (right['frame'] - left['frame'])
            expected = [a + (b - a) * fraction for a, b in zip(left['value'], right['value'])]
            budgets = [8 * (2. ** -23) * max(1., abs(a) + abs(b))
                       for a, b in zip(left['value'], right['value'])]
            return expected, budgets
    raise ContractError('VALIDATION_FAILED', 'Replay frame has no independent key segment')


def check_replay_samples(report, picked, clip_request):
    """Pure bounded sample validation; no producer/header/receipt substitution."""
    import math
    rows = report.get('samples')
    require(type(rows) is list and [row.get('frame') for row in rows] == picked['frames'], 'Replay complete ordered frame list differs')
    clip_request = canonical('canonical_request', clip_request)
    requested_tracks = {track['bone']: track for track in clip_request['tracks']}
    require(set(requested_tracks) <= set(picked['bones']), 'Replay must observe every authored input bone')
    total = 0
    previous_counts = None
    for row in rows:
        require(set(row) == {'frame', 'meshes', 'bones', 'proxies', 'clip_input_validation'}, 'Replay sample fields differ')
        validation = row['clip_input_validation']
        fields(validation, ('ok', 'failures', 'tracks'), label='native Action input validation')
        require(validation['ok'] is True and validation['failures'] == [], 'Native Action input validation failed or has failures')
        require(type(validation['tracks']) is dict and set(validation['tracks']) == set(requested_tracks),
                'Native Action input track coverage differs from the authored request')
        require(set(row['meshes']) == set(picked['meshes']) and set(row['bones']) == set(picked['bones']) and
                set(row['proxies']) == set(picked['proxy_objects']), 'Replay selected coverage differs')
        for name, requested in requested_tracks.items():
            observed = validation['tracks'][name]
            fields(observed, ('rotation_mode', 'expected_curve_values', 'native_active_values'), label='native Action input track')
            require(observed['rotation_mode'] == requested['rotation_mode'] == row['bones'][name]['rotation_mode'],
                    'Native Action input mode differs from the request or bone sample')
            width = 4 if requested['rotation_mode'] == 'QUATERNION' else 3
            for field in ('expected_curve_values', 'native_active_values'):
                vector = observed[field]
                require(type(vector) is list and len(vector) == width and
                        all(type(value) in (int, float) and math.isfinite(value) for value in vector),
                        'Native Action input has invalid width or nonfinite components')
            require(observed['native_active_values'] == observed['expected_curve_values'] == row['bones'][name]['native_active_values'],
                    'Native Action components disagree with evaluated curves or bone sample')
            expected, budgets = linear_input_values(requested, row['frame'])
            require(all(abs(actual - wanted) <= budget for actual, wanted, budget in
                        zip(observed['native_active_values'], expected, budgets)),
                    'Native Action input differs from independent request-key interpolation')
        for item in row['meshes'].values():
            decode_coordinates(item)
            total += item['vertices']
        for bone in row['bones'].values():
            fields(bone, ('rotation_mode', 'native_active_values', 'world_matrix', 'matrix', 'matrix_basis', 'head', 'tail'), label='native bone sample')
            require(bone['rotation_mode'] in motion_contract().MODES, 'Unsupported native active mode')
            # Interpolated native quaternion components need not remain unit length.
            values = bone['native_active_values']
            require(type(values) is list and len(values) == (4 if bone['rotation_mode'] == 'QUATERNION' else 3) and all(type(v) in (int, float) and math.isfinite(v) for v in values), 'Invalid native active values')
            for key in ('world_matrix', 'matrix', 'matrix_basis'):
                require(type(bone[key]) is list and len(bone[key]) == 4 and all(type(r) is list and len(r) == 4 and all(type(v) in (int,float) and math.isfinite(v) for v in r) for r in bone[key]), 'Invalid complete bone matrix')
            for key in ('head', 'tail'):
                require(type(bone[key]) is list and len(bone[key]) == 3 and all(type(v) in (int,float) and math.isfinite(v) for v in bone[key]), 'Invalid bone head/tail')
        for item in row['proxies'].values():
            fields(item, ('world', 'local', 'matrix_world', 'cloth'), label='proxy sample')
            decode_coordinates(item['world']); decode_coordinates(item['local'])
            require(item['world']['vertices'] == item['local']['vertices'], 'Proxy coordinate counts differ')
            total += item['local']['vertices'] + item['world']['vertices']
            require(type(item['matrix_world']) is list and len(item['matrix_world']) == 4 and all(type(r) is list and len(r) == 4 and all(type(v) in (int,float) and math.isfinite(v) for v in r) for r in item['matrix_world']), 'Invalid proxy world matrix')
            require(type(item['cloth']) is list and item['cloth'], 'Proxy requires explicit Cloth observations')
        counts = {('mesh', name): item['vertices'] for name, item in row['meshes'].items()}
        counts.update({('proxy', name, space): item[space]['vertices']
                       for name, item in row['proxies'].items() for space in ('world', 'local')})
        require(previous_counts is None or counts == previous_counts,
                'Replay complete mesh/proxy point counts changed across frames')
        previous_counts = counts
    require(total <= 4000000, 'Complete mesh/proxy witness exceeds total point budget')
    require(report.get('complete_coordinate_points') == total, 'Replay point accounting differs')
    return report


def check_replay_report(report, picked, prior, record):
    import math
    require(report.get('schema') == 'hrig.motion.report.v1' and report.get('mode') == 'replay' and
            report.get('status') == 'pass' and report.get('source_unchanged') is True and
            report.get('source_saved') is False and report.get('context_restored') is True,
            'Replay did not pass without saving and with restored context', 'VALIDATION_FAILED')
    require(record['worker_result'].get('mode') == 'replay' and record['worker_result'].get('status') == 'pass', 'Replay public result differs')
    require(report.get('implementation') == implementation() and report.get('entry_sha') == identity()['entry']['expected_sha256'],
            'Replay report implementation/entry binding differs', 'CONFLICT')
    require(report.get('witness') == picked and report.get('input') == prior['candidate'] and
            report.get('after_snapshot') == prior['verification']['after_snapshot'], 'Replay source/witness/storage binding differs', 'CONFLICT')
    producer = read_report(check_descriptor(prior['report'])['file'])
    return check_replay_samples(report, picked, producer['request'])


def check_replay_bundle(value):
    fields(value, ('schema', 'frontend_version', 'status', 'mode', 'identity', 'implementation',
                   'inputs', 'source', 'config', 'record', 'report', 'verification', 'GUI', 'FPS',
                   'limitations', 'created_utc'), label='motion replay bundle')
    require(value['schema'] == 'hrig.motion.replay.bundle.v1' and value['frontend_version'] == VERSION and
            value['mode'] == 'replay' and value['status'] == 'succeeded', 'Invalid succeeded motion replay bundle')
    unchanged(value['identity'], value['implementation'])
    fields(value['inputs'], ('bundle', 'witness'), label='replay inputs')
    for _ in walk_descriptors(value):
        pass
    prior = check_bundle(read_json(value['inputs']['bundle']['file']))
    require(prior['mode'] != 'inspect' and prior['verification'].get('operation_ok') is True, 'Replay source has no authored clip')
    picked = witness(read_json(value['inputs']['witness']['file']))
    expected = {**cold_config(prior['producer'], prior['inputs'].get('operation')), 'mode': 'replay',
                'witness': value['inputs']['witness'], 'source_bundle': value['inputs']['bundle']}
    require(value['config'] == expected and value['source'] == prior['candidate'], 'Replay config/source differs', 'CONFLICT')
    report = public_record(value['record'], value['identity'], value['implementation'], expected, [REPORT])
    require(value['report'] == value['record']['outputs'][REPORT] and value['verification'] == report,
            'Replay evidence is not its exact public output', 'CONFLICT')
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and value['limitations'] == LIMITATIONS,
            'Replay limitations changed', 'CONFLICT')
    check_replay_report(report, picked, prior, value['record'])
    return value


def execute_replay(args, run_dir, inputs, prior, roles, impl):
    picked = witness(read_json(inputs['witness']['file']))
    config = {**cold_config(prior['producer'], prior['inputs'].get('operation')), 'mode': 'replay',
              'witness': inputs['witness'], 'source_bundle': inputs['bundle']}
    record = host.public_job(run_dir, 'replay', request(roles, impl, config, [REPORT]), args.timeout)
    evidence = public_record(record, roles, impl, config, [REPORT])
    check_replay_report(evidence, picked, prior, record)
    unchanged(roles, impl)
    final = {'schema': 'hrig.motion.replay.bundle.v1', 'frontend_version': VERSION, 'status': 'succeeded',
             'mode': 'replay', 'identity': roles, 'implementation': impl, 'inputs': inputs,
             'source': prior['candidate'], 'config': config, 'record': record,
             'report': record['outputs'][REPORT], 'verification': evidence,
             'GUI': 'deferred_by_user', 'FPS': 'not_measured', 'limitations': LIMITATIONS,
             'created_utc': datetime.now(timezone.utc).isoformat()}
    check_replay_bundle(final)
    host.write_new(run_dir / 'bundle.json', final)
    return {'ok': True, 'status': 'succeeded', 'mode': 'replay', 'bundle': descriptor(run_dir / 'bundle.json'),
            'report': final['report'], 'source_saved': False, 'job_id': record['job_id'], 'GUI': final['GUI'], 'FPS': final['FPS']}


def fixture_cases():
    path = SCRIPTS / 'motion_fixture.py'
    require(path.is_file(), 'Motion fixture source missing', 'NOT_FOUND')
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CASE_IDS' for t in item.targets):
            values = ast.literal_eval(item.value)
            require(type(values) in (tuple, list) and len(values) > 0 and
                    all(type(x) is str and x for x in values) and len(set(values)) == len(values),
                    'Invalid static motion fixture CASE_IDS')
            return tuple(values)
    raise ContractError('UNSUPPORTED', 'No static CASE_IDS in motion fixture')


def motion_contract():
    import types
    path = ADDON / 'humanoid_rigging_probe' / 'motion_contract.py'
    require(path.is_file(), 'Motion contract source missing', 'NOT_FOUND')
    name = '_hrig_motion_host_package'
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(path.parent)]
        sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + '.motion_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(method, *values):
    core = motion_contract()
    try:
        return getattr(core, method)(*values)
    except ValueError as error:
        raise ContractError('INVALID_REQUEST', getattr(error, 'code', 'INVALID_REQUEST') + ': ' + str(error)) from error


def selection(value):
    fields(value, ('schema', 'context', 'rig', 'bones', 'action_name'), label='motion selection')
    require(value['schema'] == SELECTION_SCHEMA, 'Expected ' + SELECTION_SCHEMA)
    motion_context(value['context'])
    text(value['rig'], 'rig')
    canonical('canonical_bones', value['bones'])
    canonical('canonical_name', value['action_name'])
    return value


def parser():
    p = host.Parser(description='Isolated motion candidate CLI; backend: existing public extension.run')
    commands = p.add_subparsers(dest='command', required=True, parser_class=host.Parser)
    commands.add_parser('describe', help='host discovery; never executes Blender')
    i = commands.add_parser('inspect', help='read-only saved controller-motion inspection')
    i.add_argument('--source', required=True)
    i.add_argument('--expected-sha256', required=True)
    i.add_argument('--selection', required=True)
    i.add_argument('--expected-selection-sha256', required=True)
    a = commands.add_parser('create', help='exact selected rotation request plus independent cold validation')
    a.add_argument('--source', required=True)
    a.add_argument('--expected-sha256', required=True)
    a.add_argument('--operation', required=True)
    a.add_argument('--expected-operation-sha256', required=True)
    f = commands.add_parser('fixture', help='one explicit fixture plus independent cold')
    f.add_argument('--case', required=True)
    v = commands.add_parser('validate', help='new independent cold for an immutable candidate bundle')
    v.add_argument('--bundle', required=True)
    v.add_argument('--expected-sha256', required=True)
    r = commands.add_parser('replay', help='owned read-only sequential native replay; no candidate save')
    r.add_argument('--bundle', required=True)
    r.add_argument('--expected-sha256', required=True)
    r.add_argument('--witness', required=True)
    r.add_argument('--expected-witness-sha256', required=True)
    for command in (i, a, f, v, r):
        command.add_argument('--out-dir', required=True)
        command.add_argument('--timeout', type=float, default=180)
    return p

def operation(value):
    fields(value, ('schema', 'context', 'rig', 'request', 'external_inputs',
                   'inherited_missing_weak_references'), label='motion operation')
    require(value['schema'] == OP_SCHEMA, 'Expected ' + OP_SCHEMA)
    motion_context(value['context'])
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
    paths += [SCRIPTS / 'character_state.py', SCRIPTS / 'motion_state.py',
              SCRIPTS / 'motion_fixture.py', SCRIPTS / 'run_weights.py', SCRIPTS / 'run_styles.py',
              OBSERVER / 'inspection.py', OBSERVER / 'protocol.py']
    return check_descriptors([descriptor(path) for path in paths])

def identity():
    paths = {'frontend': SCRIPTS / 'rigctl.py', 'motion_cli': Path(__file__),
             'host_contract': SCRIPTS / 'host_contract.py', 'entry': SCRIPTS / 'run_motion.py',
             'public_wrapper': host.PUBLIC, 'inspection': OBSERVER / 'inspection.py',
             'protocol': OBSERVER / 'protocol.py'}
    paths.update({p.stem: p for p in (SCRIPTS / 'character_state.py', SCRIPTS / 'motion_state.py',
                  SCRIPTS / 'motion_fixture.py', SCRIPTS / 'run_weights.py', SCRIPTS / 'run_styles.py')})
    paths.update({'addon_' + p.stem: p for p in sorted((ADDON / 'humanoid_rigging_probe').glob('*.py'))})
    return {role: descriptor(path) for role, path in paths.items()}

def unchanged(roles, impl):
    require(roles == identity(), 'Motion frontend/entry/helper/public role identity changed', 'CONFLICT')
    require(impl == implementation(), 'Exact motion implementation closure changed', 'CONFLICT')


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
            'Public motion request differs from exact role/config/resource/output contract', 'CONFLICT')
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
    return read_report(record['outputs'][REPORT]['file'])


def source(value, expected):
    result = descriptor(value, expected)
    require(Path(result['file']).suffix.lower() == '.blend', 'Source must be a .blend')
    return result


def producer_config(mode, inputs, case=None):
    config = {'mode': mode}
    if mode in ('inspect', 'create'):
        config['input'] = inputs['source']
    if mode == 'create':
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
    require(type(report) is dict and report.get('schema') == 'hrig.motion.report.v1',
            'Unexpected motion producer report schema', 'CONFLICT')
    require(report.get('implementation') == impl and
            report.get('entry_sha') == roles['entry']['expected_sha256'],
            'Producer implementation/entry binding differs', 'CONFLICT')
    require(report.get('mode') == mode and type(producer.get('worker_result')) is dict and
            producer['worker_result'].get('mode') == mode and
            producer['worker_result'].get('status') == report.get('status'),
            'Producer native report/public result mode or status differs', 'CONFLICT')
    if mode == 'inspect':
        require(report.get('status') == 'pass' and report.get('source_unchanged') is True,
                'Read-only motion inspection did not pass unchanged-source check', 'VALIDATION_FAILED')
    else:
        require(report.get('status') == 'candidate_waiting_independent_cold' and
                report.get('candidate') == producer['outputs'][CANDIDATE],
                'Motion producer is not paired with its candidate', 'VALIDATION_FAILED')
        if mode == 'create':
            require(report.get('operation_ok') is True,
                    'Requested create was not applied successfully', 'VALIDATION_FAILED')
        else:
            require(type(report.get('operation_ok')) is bool,
                    'Fixture report must explicitly distinguish operation success/refusal', 'VALIDATION_FAILED')


def check_cold(report, record):
    require(type(report) is dict and report.get('schema') == 'hrig.motion.report.v1',
            'Unexpected motion cold report schema', 'CONFLICT')
    require(report.get('status') == 'pass' and report.get('source_unchanged') is True and
            type(record['worker_result']) is dict and record['worker_result'].get('status') == 'pass',
            'Independent motion cold validation did not pass', 'VALIDATION_FAILED')
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
           ('cold', 'candidate', 'validated_bundle'), 'motion bundle')
    require(value['schema'] == SCHEMA and value['frontend_version'] == VERSION and
            value['status'] == 'succeeded' and value['mode'] in ('inspect', 'create', 'fixture'),
            'Expected immutable succeeded motion bundle')
    roles, impl, mode = value['identity'], value['implementation'], value['mode']
    unchanged(roles, impl)
    for _ in walk_descriptors(value):
        pass
    inputs, config = value['inputs'], value['producer_config']
    require(value['GUI'] == 'deferred_by_user' and value['FPS'] == 'not_measured' and
            value['limitations'] == LIMITATIONS, 'Bundle limitations or deferred GUI/FPS changed', 'CONFLICT')
    if mode in ('inspect', 'create'):
        check_descriptor(inputs.get('source'))
        source(inputs['source']['file'], inputs['source']['expected_sha256'])
    if mode == 'create':
        fields(inputs, ('source', 'operation'), label='create bundle inputs')
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
    if args.command in ('inspect', 'create'):
        inputs['source'] = source(args.source, args.expected_sha256)
    if args.command == 'create':
        inputs['operation'] = descriptor(args.operation, args.expected_operation_sha256)
        operation(read_json(inputs['operation']['file']))
    elif args.command == 'inspect':
        inputs['selection'] = descriptor(args.selection, args.expected_selection_sha256)
        selection(read_json(inputs['selection']['file']))
    elif args.command == 'fixture':
        require(args.case in fixture_cases(), 'Unknown motion fixture case')
    elif args.command in ('validate', 'replay'):
        inputs['bundle'] = descriptor(args.bundle, args.expected_sha256)
        prior = check_bundle(read_json(inputs['bundle']['file']))
        require(prior['mode'] != 'inspect',
                'Read-only inspection bundles have no candidate; validate accepts only apply/fixture bundles',
                'UNSUPPORTED')
        if args.command == 'replay':
            require(prior['mode'] in ('create', 'fixture') and prior['verification'].get('operation_ok') is True, 'Replay needs a successful authored clip candidate')
            inputs['witness'] = descriptor(args.witness, args.expected_witness_sha256)
            witness(read_json(inputs['witness']['file']))
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
    paths = {'entry': SCRIPTS / 'run_motion.py', 'character_state': SCRIPTS / 'character_state.py',
             'motion_state': SCRIPTS / 'motion_state.py', 'motion_fixture': SCRIPTS / 'motion_fixture.py',
             'host_motion_contract': ADDON / 'humanoid_rigging_probe' / 'motion_contract.py',
             'frontend': Path(__file__)}
    try:
        cases = {'status': 'available', 'values': list(fixture_cases())}
    except (ContractError, SyntaxError, ValueError) as error:
        cases = {'status': 'missing_or_invalid', 'values': [], 'message': str(error)}
    return {'schema': 'hrig.motion.cli.describe.v1', 'frontend_version': VERSION,
            'backend': 'existing public extension.run', 'public_motion_commands_added': False,
            'commands': ['describe', 'inspect', 'create', 'fixture', 'validate', 'replay'],
            'fixture_cases': cases, 'operation_schema': OP_SCHEMA, 'selection_schema': SELECTION_SCHEMA,
            'request_schema': 'hrig.motion-clip.create.v1', 'bundle_schema': SCHEMA,
            'operation_fields': ['schema', 'context', 'rig', 'request', 'external_inputs',
                                 'inherited_missing_weak_references'],
            'selection_fields': ['schema', 'context', 'rig', 'bones', 'action_name'],
            'request_fields': ['schema', 'action_name', 'expected_snapshot_sha256', 'tracks'],
            'track_fields': ['bone', 'rotation_mode', 'before', 'keys'],
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
