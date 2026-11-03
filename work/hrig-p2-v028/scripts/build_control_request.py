"""Build exact-SHA control requests; source work remains candidate-only."""
import argparse
import hashlib
import json
from pathlib import Path


def descriptor(file):
    path = Path(file).resolve(strict=True)
    return {'file':str(path),'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


parser = argparse.ArgumentParser()
parser.add_argument('--mode',choices=('fixture','cold'),required=True)
parser.add_argument('--case')
parser.add_argument('--input')
parser.add_argument('--expected-sha256')
parser.add_argument('--producer-report')
parser.add_argument('--fixture-manifest')
parser.add_argument('--out',required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
out = Path(args.out)
if not out.is_absolute() or out.exists():
    parser.error('Output must be a new absolute request path')
implementation = [descriptor(f) for f in sorted((root/'addon/humanoid_rigging_probe').glob('*.py'))]
implementation.append(descriptor(root/'scripts/space_fixture.py'))
config = {'mode':args.mode,'implementation':implementation,'addon_root':str(root/'addon'),
          'fixture_script':str(root/'scripts/space_fixture.py')}
resources = list(implementation)
outputs = ['output-control.json']
if args.mode == 'fixture':
    if not args.case:
        parser.error('Explicit case required')
    config['case'] = args.case
    outputs += ['output-manifest.json','output-fixture.blend']
else:
    if not all((args.input,args.expected_sha256,args.producer_report,args.fixture_manifest)):
        parser.error('Cold mode requires input/expected SHA/producer report/manifest')
    for key,file in [('input',args.input),('producer_report',args.producer_report),('fixture_manifest',args.fixture_manifest)]:
        config[key] = descriptor(file)
        resources.append(config[key])
    if config['input']['expected_sha256'] != args.expected_sha256:
        parser.error('Input differs from expected SHA')
request = {'schema_version':'1.0','command':'extension.run','params':{
    'manifest':{'script':descriptor(root/'scripts/run_controls.py'),'trust':'execute_local_python_without_sandbox',
                'params':config,'outputs':outputs},'resources':resources}}
out.parent.mkdir(parents=True,exist_ok=True)
with out.open('x',encoding='utf-8') as stream:
    json.dump(request,stream,ensure_ascii=False,indent=2,allow_nan=False)
print(json.dumps({'request':str(out),'mode':args.mode},ensure_ascii=False))
