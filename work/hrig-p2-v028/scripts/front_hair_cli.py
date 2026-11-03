"""Explicit source, new output, native producer + independent cold validation."""
import argparse,json,subprocess,sys
from pathlib import Path
sys.dont_write_bytecode=True
import host_contract as H,diagnostic_cli as D,build_id_cost_request as B
ROOT=Path(__file__).resolve().parents[1]
SOURCE='216f4c0a8fa919d538ca95c45e3dea2d4157974ee5c5036d0f99885e6c9a10bd'
def load(p):return H.parse_json(Path(p).read_text(encoding='utf-8-sig'))
def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['describe','build'])
    for field in ('source','expected-sha256','external-inputs','expected-external-inputs-sha256','out-dir'):
        parser.add_argument('--'+field)
    parser.add_argument('--timeout',type=int,default=360)
    args=parser.parse_args(argv)
    if args.operation=='describe':
        print(json.dumps({'schema':'hrig.front-hair.v1','version':'0.20.0-experimental','operation':'build',
              'source_sha256':SOURCE,'source_domain':'Aon v009 unbaked baseline5 only',
              'mutation':'New 242-bone front/side hair rig and one existing Armature modifier pointer.',
              'retained':'All original bones, Mesh/weights/Keys/materials/Action/proxies; shared original head and FK-IK settings.',
              'acceptance':'Source and implementation SHA, independent saved cold author/geometry, inverse canonical restoration.',
              'limitations':['not a generic rig converter','GUI/FPS not measured','full animation/ROM and production acceptance pending']}))
        return 0
    H.require(all(getattr(args,k.replace('-','_')) for k in ('source','expected-sha256','external-inputs','expected-external-inputs-sha256','out-dir')),'Explicit input identities required')
    H.require(args.expected_sha256==SOURCE,'This experimental recipe only accepts the frozen baseline5 source','UNSUPPORTED')
    H.require(1<=args.timeout<=1800,'timeout out of range')
    source=H.descriptor(args.source,args.expected_sha256)
    ext_file=H.descriptor(args.external_inputs,args.expected_external_inputs_sha256)
    H.require(Path(ext_file['file']).stat().st_size<=1048576,'External descriptor file too large')
    external=H.check_descriptors(load(ext_file['file']))
    H.require(len(external)==17,'This recipe requires the 17 explicit static dependencies','UNSUPPORTED')
    worker=H.descriptor(ROOT/'scripts/run_front_hair.py')
    impl=B._dedupe([*B.implementation(),worker,H.descriptor(Path(__file__)),
                    H.descriptor(ROOT/'scripts/rigctl.py'),H.descriptor(D.PUBLIC),
                    H.descriptor(D.__file__),H.descriptor(B.__file__)])
    H.require(H.descriptor(ROOT/'addon/humanoid_rigging_probe/front_hair_isolation.py') in impl,
              'Front-hair implementation is outside the pinned closure','CONFLICT')
    out=D._run_dir(args.out_dir)
    def execute(phase,input_desc,producer=None):
        run=D._run_dir(str(out/phase))
        config={'phase':phase,'source':source,'input':input_desc,'implementation':impl,
                'external_inputs':external,'addon_root':str(ROOT/'addon'),'producer':producer}
        resources=B._dedupe([source,input_desc,ext_file,*impl,*external,*([producer] if producer else [])])
        outputs=['output-validation.json']+(['output-candidate.blend'] if phase=='producer' else [])
        request={'schema_version':'1.0','command':'extension.run','params':{'manifest':{'script':worker,
                 'trust':'execute_local_python_without_sandbox','params':config,'outputs':outputs},'resources':resources}}
        D._write_json(run/'request.json',request)
        p=subprocess.run([sys.executable,'-B',str(D.PUBLIC),'--jobs-dir',str(run/'jobs'),'--timeout',str(args.timeout),
                          'request',str(run/'request.json')],capture_output=True,timeout=args.timeout+60,creationflags=D.NO_WINDOW)
        D._write_bytes(run/'stdout.txt',p.stdout);D._write_bytes(run/'stderr.txt',p.stderr)
        result=H.parse_json(p.stdout);D._write_json(run/'result.json',result)
        H.require(p.returncode==0 and result['ok'] is True and result['error'] is None,str(result.get('error')),'WORKER_FAILED')
        job=run/'jobs'/result['job_id'];state=load(job/'status.json')
        H.require(state['state']=='succeeded' and state['worker_exit_code']==0,'Native job not succeeded','WORKER_FAILED')
        H.require(load(job/'request.json')==request and load(job/'result.json')==result,'Job identity mismatch','CONFLICT')
        for d in resources:
            H.check_descriptor(d);H.require(result['data']['input_hashes'][d['file']]==d['expected_sha256'],'Input receipt mismatch','CONFLICT')
        descriptors={}
        for row in result['data']['outputs']:
            path=Path(row['file']);H.require(path.parent.resolve()==job.resolve() and path.name in outputs,'Output path mismatch','CONFLICT')
            H.require(path.stat().st_size==row['bytes'],'Output size mismatch','CONFLICT')
            descriptors[path.name]=H.descriptor(path,row['sha256'])
        H.require(set(descriptors)==set(outputs),'Missing output','WORKER_FAILED')
        report=load(descriptors['output-validation.json']['file'])
        H.require(report['status']=='pass' and report['original_author_exact'] and report['resources_unchanged']
                  and report['resource_targets_preserved'],str(report.get('error',report)),'WORKER_FAILED')
        return result,report,descriptors
    try:
        prod,pr,pd=execute('producer',source)
        cold,cr,cd=execute('cold',pd['output-candidate.blend'],pd['output-validation.json'])
        H.require(pr['candidate_author_sha256']==cr['candidate_author_sha256'],'Cold author mismatch','CONFLICT')
        bundle={'schema':'hrig.front-hair-bundle.v1','status':'succeeded','scope':'experimental fixed Aon source; not general conversion or FPS acceptance',
                'source':source,'candidate':pd['output-candidate.blend'],'producer':pd['output-validation.json'],
                'cold':cd['output-validation.json'],'jobs':[prod['job_id'],cold['job_id']],
                'cli_versions':[prod['cli_version'],cold['cli_version']],'implementation':impl}
        D._write_json(out/'bundle.json',bundle);print(json.dumps(bundle,ensure_ascii=False));return 0
    except Exception as e:
        D._write_json(out/'failure.json',{'status':'fail','error':str(e),'candidate_policy':'quarantined; no succeeded bundle'})
        raise
if __name__=='__main__':raise SystemExit(main())
