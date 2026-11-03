"""Explicit fixed-source hair-pair candidate, producer and independent cold validation."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
import host_contract as H
import diagnostic_cli as D
import build_id_cost_request as B

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '216f4c0a8fa919d538ca95c45e3dea2d4157974ee5c5036d0f99885e6c9a10bd'
REPORT_LIMIT = 8 * 1024 * 1024


def load(path):
    return H.parse_json(Path(path).read_text(encoding='utf-8-sig'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('operation', choices=['describe', 'build'])
    for field in ('source', 'expected-sha256', 'external-inputs', 'expected-external-inputs-sha256', 'out-dir'):
        parser.add_argument('--' + field)
    parser.add_argument('--timeout', type=int, default=360)
    args = parser.parse_args(argv)
    if args.operation == 'describe':
        print(json.dumps({
            'schema': 'hrig.hair-pair.v1', 'version': '0.21.0-experimental', 'operation': 'build',
            'source_sha256': SOURCE, 'source_domain': 'Aon v009 unbaked baseline5 only',
            'mutation': 'New 245-bone front/side and 305-bone rear branches; two existing Armature modifier pointers.',
            'retained': 'All original bones, Mesh/weights/Keys/materials/Action/proxies; six original head/root/settings boundaries.',
            'acceptance': 'Pinned implementation and actual loaded modules; producer/cold author, both Mesh geometries, resolved resource targets and inverse canonical.',
            'limitations': ['not a generic rig converter', 'no action migration', 'not GUI/FPS or full ROM/collision acceptance'],
        }, ensure_ascii=False))
        return 0
    H.require(all(getattr(args, key.replace('-', '_')) for key in (
        'source', 'expected-sha256', 'external-inputs', 'expected-external-inputs-sha256', 'out-dir')),
        'Explicit input identities required')
    H.require(args.expected_sha256 == SOURCE, 'This recipe only accepts frozen baseline5', 'UNSUPPORTED')
    H.require(1 <= args.timeout <= 1800, 'timeout out of range')
    source = H.descriptor(args.source, args.expected_sha256)
    external_file = H.descriptor(args.external_inputs, args.expected_external_inputs_sha256)
    H.require(Path(external_file['file']).stat().st_size <= 1048576, 'External descriptor file too large')
    external = H.check_descriptors(load(external_file['file']))
    H.require(len(external) == 17, 'This recipe requires 17 explicit static dependencies', 'UNSUPPORTED')
    worker = H.descriptor(ROOT / 'scripts/run_hair_pair.py')
    implementation = B._dedupe([
        *B.implementation(), worker, H.descriptor(Path(__file__)),
        H.descriptor(ROOT / 'scripts/rigctl.py'), H.descriptor(D.PUBLIC),
        H.descriptor(D.__file__), H.descriptor(B.__file__),
    ])
    for name in ('hair_pair_isolation.py', 'front_hair_isolation.py', 'rear_hair_isolation.py'):
        H.require(H.descriptor(ROOT / 'addon/humanoid_rigging_probe' / name) in implementation,
                  name + ' is outside the pinned implementation closure', 'CONFLICT')
    output = D._run_dir(args.out_dir)

    def execute(phase, input_descriptor, producer=None):
        run_dir = D._run_dir(str(output / phase))
        config = {'phase': phase, 'source': source, 'input': input_descriptor,
                  'implementation': implementation, 'external_inputs': external,
                  'addon_root': str(ROOT / 'addon'), 'producer': producer}
        resources = B._dedupe([source, input_descriptor, external_file, *implementation,
                              *external, *([producer] if producer else [])])
        expected_outputs = ['output-validation.json'] + (['output-candidate.blend'] if phase == 'producer' else [])
        request = {'schema_version': '1.0', 'command': 'extension.run', 'params': {
            'manifest': {'script': worker, 'trust': 'execute_local_python_without_sandbox',
                         'params': config, 'outputs': expected_outputs}, 'resources': resources}}
        D._write_json(run_dir / 'request.json', request)
        process = subprocess.run([
            sys.executable, '-B', str(D.PUBLIC), '--jobs-dir', str(run_dir / 'jobs'),
            '--timeout', str(args.timeout), 'request', str(run_dir / 'request.json'),
        ], capture_output=True, timeout=args.timeout + 60, creationflags=D.NO_WINDOW)
        D._write_bytes(run_dir / 'stdout.txt', process.stdout)
        D._write_bytes(run_dir / 'stderr.txt', process.stderr)
        result = H.parse_json(process.stdout)
        D._write_json(run_dir / 'result.json', result)
        H.require(process.returncode == 0 and result['ok'] is True and result['error'] is None,
                  str(result.get('error')), 'WORKER_FAILED')
        job = run_dir / 'jobs' / result['job_id']
        H.require(Path(result['artifacts']['job_directory']).resolve() == job.resolve(),
                  'Job directory identity differs', 'CONFLICT')
        state = load(job / 'status.json')
        H.require(state['state'] == 'succeeded' and state['worker_exit_code'] == 0,
                  'Native job not succeeded', 'WORKER_FAILED')
        H.require(load(job / 'request.json') == request and load(job / 'result.json') == result,
                  'Native request/result identity mismatch', 'CONFLICT')
        H.require(result['data']['script_sha256_observed'] == worker['expected_sha256'],
                  'Observed worker SHA differs', 'CONFLICT')
        for descriptor in resources:
            H.check_descriptor(descriptor)
            H.require(result['data']['input_hashes'][descriptor['file']] == descriptor['expected_sha256'],
                      'Input receipt identity mismatch', 'CONFLICT')
        descriptors = {}
        for row in result['data']['outputs']:
            path = Path(row['file'])
            H.require(path.parent.resolve() == job.resolve() and path.name in expected_outputs,
                      'Output identity differs', 'CONFLICT')
            H.require(path.name not in descriptors and path.stat().st_size == row['bytes'],
                      'Duplicate output or size mismatch', 'CONFLICT')
            if path.name == 'output-validation.json':
                H.require(row['bytes'] <= REPORT_LIMIT, 'Validation report exceeds 8 MiB', 'WORKER_FAILED')
            descriptors[path.name] = H.descriptor(path, row['sha256'])
        H.require(set(descriptors) == set(expected_outputs), 'Missing output', 'WORKER_FAILED')
        report = load(descriptors['output-validation.json']['file'])
        H.require(report['schema'] == 'hrig.hair-pair-candidate.v1' and report['phase'] == phase
                  and report['source'] == source and report['status'] == 'pass'
                  and report['original_author_exact'] and report['resources_unchanged']
                  and report['resource_targets_preserved'] and report['resource_record_count'] == 50,
                  str(report.get('error', {'status': report.get('status'), 'phase': report.get('phase'),
                                          'original_author_exact': report.get('original_author_exact'),
                                          'resource_targets_preserved': report.get('resource_targets_preserved')})),
                  'WORKER_FAILED')
        H.require(set(report['branches']) == {'front', 'rear'}
                  and set(report['candidate_geometry']) == {'front', 'rear'}
                  and set(report['neutral_comparison']) == {'front', 'rear'},
                  'Both hair branches must be witnessed', 'WORKER_FAILED')
        for part, count in (('front', 245), ('rear', 305)):
            branch = report['branches'][part]
            comparison = report['neutral_comparison'][part]
            H.require(branch['bone_count'] == count and branch['driver_count'] == 14
                      and branch['boundary_count'] == 6 and branch['rest_and_parents_exact']
                      and branch['driver_settings_targets_exact'] and branch['constraint_targets_valid'],
                      'Branch structure witness failed: ' + part, 'WORKER_FAILED')
            H.require(comparison['topology_exact'] and comparison['object_world_exact']
                      and comparison['all_finite'] and comparison['vertices'] > 0
                      and all(type(comparison[key]) in (int, float) and math.isfinite(comparison[key])
                              and 0 <= comparison[key] <= 1e-6 for key in ('local_max_component_error', 'world_max_distance')),
                      'Geometry witness failed: ' + part, 'WORKER_FAILED')
        return result, report, descriptors

    try:
        producer, producer_report, producer_files = execute('producer', source)
        cold, cold_report, cold_files = execute('cold', producer_files['output-candidate.blend'],
                                              producer_files['output-validation.json'])
        H.require(producer_report['candidate_author_sha256'] == cold_report['candidate_author_sha256'],
                  'Cold author identity differs', 'CONFLICT')
        H.require(producer_report['resource_targets_sha256'] == cold_report['resource_targets_sha256'],
                  'Cold resource target identity differs', 'CONFLICT')
        H.require(producer_report['candidate_sha256'] == cold_report['candidate_sha256']
                  == producer_files['output-candidate.blend']['expected_sha256'],
                  'Candidate identity differs across phases', 'CONFLICT')
        bundle = {'schema': 'hrig.hair-pair-bundle.v1', 'status': 'succeeded',
                  'scope': 'experimental fixed baseline5 front/side and rear branches; not generic conversion, action migration or full GUI/FPS/ROM/collision acceptance',
                  'source': source, 'candidate': producer_files['output-candidate.blend'],
                  'producer': producer_files['output-validation.json'], 'cold': cold_files['output-validation.json'],
                  'jobs': [producer['job_id'], cold['job_id']],
                  'cli_versions': [producer['cli_version'], cold['cli_version']], 'implementation': implementation}
        D._write_json(output / 'bundle.json', bundle)
        print(json.dumps(bundle, ensure_ascii=False))
        return 0
    except Exception as exc:
        D._write_json(output / 'failure.json', {'status': 'fail', 'error': str(exc),
                                               'candidate_policy': 'quarantined; no succeeded bundle'})
        raise


if __name__ == '__main__':
    raise SystemExit(main())
