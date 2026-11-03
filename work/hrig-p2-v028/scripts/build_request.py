"""Construct a pinned, bounded public CLI request; refuses output overwrite."""
import argparse
import hashlib
import json
from pathlib import Path


def descriptor(path):
    path = path.resolve(strict=True)
    return {'file': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


parser = argparse.ArgumentParser()
parser.add_argument('--mode', required=True, choices=['fixture', 'inspect'])
parser.add_argument('--input')
parser.add_argument('--expected-sha256')
parser.add_argument('--armature')
parser.add_argument('--save-fixture', action='store_true')
parser.add_argument('--out', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
out = Path(args.out)
if not out.is_absolute() or out.exists():
    parser.error('--out must be a new absolute file')
implementation = [descriptor(path) for path in sorted((root / 'addon/humanoid_rigging_probe').glob('*.py'))]
implementation.append(descriptor(Path(__file__).with_name('fixture.py')))
parameters = {'mode': args.mode, 'addon_root': str(root / 'addon'), 'implementation': implementation}
if args.save_fixture:
    if args.mode != 'fixture':
        parser.error('--save-fixture only applies to the synthetic fixture')
    parameters['save_fixture'] = True
resources = list(implementation)
if args.mode == 'inspect':
    if not all((args.input, args.expected_sha256, args.armature)):
        parser.error('inspect requires --input, --expected-sha256 and --armature')
    source = descriptor(Path(args.input))
    if source['expected_sha256'] != args.expected_sha256:
        parser.error('Input bytes differ from the explicitly expected SHA')
    parameters.update(input=source, armature=args.armature)
    resources.append(source)
request = {'schema_version': '1.0', 'command': 'extension.run', 'params': {
    'manifest': {'script': descriptor(Path(__file__).with_name('run_probe.py')),
                 'trust': 'execute_local_python_without_sandbox', 'params': parameters,
                 'outputs': ['output-inspection.json', 'output-validation.json'] + (['output-fixture.blend'] if args.save_fixture else [])},
    'resources': resources}}
out.parent.mkdir(parents=True, exist_ok=True)
with out.open('x', encoding='utf-8') as stream:
    json.dump(request, stream, ensure_ascii=False, indent=2, allow_nan=False)
print(json.dumps({'request': str(out), 'mode': args.mode, 'resources': len(resources)}, ensure_ascii=False))
