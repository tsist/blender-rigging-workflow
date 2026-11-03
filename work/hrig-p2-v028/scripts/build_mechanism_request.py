"""Build exact-input/implementation-SHA requests; no execution or overwrite."""
import argparse
import hashlib
import json
from pathlib import Path


def descriptor(path):
    path = Path(path).resolve(strict=True)
    return {'file': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


parser = argparse.ArgumentParser()
parser.add_argument('--mode', required=True, choices=['api', 'fixture', 'cold_fixture', 'dependencies', 'temporal'])
parser.add_argument('--case', choices=['same_rig_ik','copy_feedback','property_influence','mesh_group_visible','excluded_mesh','reference_edges'])
parser.add_argument('--input')
parser.add_argument('--expected-sha256')
parser.add_argument('--armature')
parser.add_argument('--fixture-manifest')
parser.add_argument('--witness-config', help='JSON with frames/objects/bones/include_collections for temporal')
parser.add_argument('--out', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
out = Path(args.out)
if not out.is_absolute() or out.exists():
    parser.error('--out must be a new absolute file')
files = list((root / 'addon/humanoid_rigging_probe').glob('*.py'))
files += [root / 'scripts/dependency_fixture.py']
implementation = [descriptor(file) for file in sorted(files)]
resources = list(implementation)
config = {'mode': args.mode, 'addon_root': str(root / 'addon'), 'implementation': implementation,
          'fixture_script': str(root / 'scripts/dependency_fixture.py')}
outputs = ['output-mechanisms.json', 'output-depsgraph.txt']
if args.mode == 'fixture':
    if not args.case:
        parser.error('fixture requires --case')
    config['case'] = args.case
    outputs += ['output-fixture.blend', 'output-fixture-manifest.json']
if args.mode in {'dependencies','temporal','cold_fixture'}:
    if not args.input or not args.expected_sha256:
        parser.error('mode requires input/expected SHA')
    source = descriptor(args.input)
    if source['expected_sha256'] != args.expected_sha256:
        parser.error('Explicit source SHA differs')
    config['input'] = source
    resources.append(source)
    if args.mode == 'cold_fixture':
        if not args.fixture_manifest:
            parser.error('cold_fixture requires --fixture-manifest')
        config['fixture_manifest'] = descriptor(args.fixture_manifest)
        resources.append(config['fixture_manifest'])
        outputs += ['output-fixture-manifest.json']
    else:
        if not args.armature:
            parser.error('Explicit armature required')
        config['armature'] = args.armature
if args.mode == 'temporal':
    if not args.witness_config:
        parser.error('temporal requires --witness-config')
    witness = descriptor(args.witness_config)
    values = json.loads(Path(witness['file']).read_text(encoding='utf-8-sig'))
    allowed = {'frames','objects','bones','include_collections','pose_properties'}
    if set(values) - allowed:
        parser.error('Unknown witness config fields')
    config.update(values)
    resources.append(witness)
    outputs += ['output-progress.json']
request = {'schema_version': '1.0', 'command': 'extension.run', 'params': {
    'manifest': {'script': descriptor(root / 'scripts/run_mechanisms.py'),
                 'trust': 'execute_local_python_without_sandbox', 'params': config, 'outputs': outputs},
    'resources': resources}}
out.parent.mkdir(parents=True, exist_ok=True)
with out.open('x',encoding='utf-8') as stream:
    json.dump(request, stream, ensure_ascii=False, indent=2, allow_nan=False)
print(json.dumps({'request':str(out),'mode':args.mode,'resources':len(resources)}, ensure_ascii=False))
