"""Build declared owned-limb jobs for the existing public extension.run."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re


NEGATIVE_CASES = ('wrong_source','external_driver','empty_witness','changed_weight',
                  'modifier_disabled','quaternion_w_lock','arbitrary_fk_twist','duplicate_module')


def descriptor(file):
    path = Path(file).resolve(strict=True)
    return {'file':str(path),'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


parser=argparse.ArgumentParser()
parser.add_argument('--mode',required=True,choices=('roundtrip','negative','cold'))
parser.add_argument('--case',choices=NEGATIVE_CASES)
parser.add_argument('--pose-index',type=int,choices=(0,1,2),default=0)
parser.add_argument('--module-id')
parser.add_argument('--side',type=int,choices=(-1,1),default=1)
parser.add_argument('--roll',type=float,default=.37)
parser.add_argument('--scale',type=float,default=1.2)
parser.add_argument('--input')
parser.add_argument('--expected-sha256')
parser.add_argument('--producer-report')
parser.add_argument('--manifest')
parser.add_argument('--out',required=True)
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
out=Path(args.out)
if not out.is_absolute() or out.exists():
    parser.error('Request must be a new absolute file')
if not math.isfinite(args.roll):
    parser.error('Roll must be finite')
if not math.isfinite(args.scale) or not 1e-3<=args.scale<=1e3:
    parser.error('Scale must be finite and within 1e-3..1e3')
if args.mode!='negative' and args.case is not None:
    parser.error('--case is only valid for negative mode')
files=[descriptor(f) for f in sorted((root/'addon/humanoid_rigging_probe').glob('*.py'))]
config={'mode':args.mode,'implementation':files,'addon_root':str(root/'addon'),
        'pose_index':args.pose_index}
resources=list(files)
outputs=['output-limb.json']
if args.mode in ('roundtrip','negative'):
    if not args.module_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.module_id):
        parser.error('Explicit module-id: 1..64 ASCII letters/digits/_/- required')
    if args.mode=='negative':
        if args.case is None:
            parser.error('Negative mode requires --case')
        config['case']=args.case
    config['spec']={'module_id':args.module_id,'name':args.module_id,'side':args.side,
                    'rest_roll':args.roll,'object_uniform_scale':args.scale}
    outputs+=['output-manifest.json','output-fixture.blend']
else:
    if not all((args.input,args.expected_sha256,args.producer_report,args.manifest)):
        parser.error('Cold input/SHA/report/manifest required')
    if not re.fullmatch(r'[0-9a-f]{64}',args.expected_sha256):
        parser.error('Expected SHA must be lowercase 64-character SHA-256')
    for key,file in [('input',args.input),('producer_report',args.producer_report),('manifest',args.manifest)]:
        config[key]=descriptor(file)
        resources.append(config[key])
    if config['input']['expected_sha256']!=args.expected_sha256:
        parser.error('Input SHA mismatch')
request={'schema_version':'1.0','command':'extension.run','params':{
    'manifest':{'script':descriptor(root/'scripts/run_limb.py'),'trust':'execute_local_python_without_sandbox',
        'params':config,'outputs':outputs},'resources':resources}}
# Validate every value before creating a directory/file. A serialization error
# must not leave a truncated exclusive request that looks ready to execute.
encoded=json.dumps(request,ensure_ascii=False,indent=2,allow_nan=False)
out.parent.mkdir(parents=True,exist_ok=True)
with out.open('x',encoding='utf-8') as stream:
    stream.write(encoded)
print(json.dumps({'request':str(out),'mode':args.mode},ensure_ascii=False))
