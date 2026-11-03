"""Fixed v028 manual demo Actions on the accepted v027 Aon hair pair."""
import argparse,json,sys,subprocess,hashlib
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import diagnostic_cli as D,host_contract as H
PAIR_SHA='506f9dd91c0e7d0808b934a7333e7b500c58e59bff33aa0ef927d77735e7ca45'
BASE_SHA='216f4c0a8fa919d538ca95c45e3dea2d4157974ee5c5036d0f99885e6c9a10bd'
def load(p):return H.parse_json(Path(p).read_text(encoding='utf8'))
def desc(p):return H.descriptor(Path(p))
def implementation():
    paths=[*sorted((ROOT/'addon/humanoid_rigging_probe').glob('*.py')),
        ROOT/'scripts/character_state.py',ROOT/'scripts/host_contract.py',ROOT/'scripts/diagnostic_cli.py',
        ROOT/'scripts/run_hair_actions.py',Path(__file__),D.PUBLIC,D.PUBLIC.parent/'protocol.py',D.PUBLIC.parent/'inspection.py']
    result=[]
    for p in paths:
        d=desc(p)
        if d not in result:result.append(d)
    return result
def phase(config,out,timeout):
    out=D._run_dir(str(out));worker=desc(ROOT/'scripts/run_hair_actions.py')
    rows=[config['input'],*config['implementation'],*config['external_inputs']]
    if config['producer']:rows.append(config['producer'])
    resources=[]
    for d in rows:
        H.check_descriptor(d)
        if d not in resources:resources.append(d)
    outputs=['output-validation.json','output-geometry.json']
    if config['phase']=='producer':outputs.append('output-candidate.blend')
    req={'schema_version':'1.0','command':'extension.run','params':{'manifest':{'script':worker,
        'trust':'execute_local_python_without_sandbox','params':config,'outputs':outputs},'resources':resources}}
    D._write_json(out/'request.json',req)
    p=subprocess.run([sys.executable,'-B',str(D.PUBLIC),'--jobs-dir',str(out/'jobs'),'--timeout',str(timeout),'request',str(out/'request.json')],
                     capture_output=True,timeout=timeout+60,creationflags=D.NO_WINDOW)
    D._write_bytes(out/'stdout.txt',p.stdout);D._write_bytes(out/'stderr.txt',p.stderr)
    r=H.parse_json(p.stdout);D._write_json(out/'result.json',r)
    assert p.returncode==0 and r['ok'] and r['error'] is None,r.get('error')
    job=out/'jobs'/r['job_id'];status=load(job/'status.json')
    assert status['state']=='succeeded' and status['worker_exit_code']==0
    for d in resources:
        H.check_descriptor(d);assert r['data']['input_hashes'][d['file']]==d['expected_sha256']
    for d in r['data']['outputs']:
        assert Path(d['file']).parent.resolve()==job.resolve()
        H.check_descriptor({'file':d['file'],'expected_sha256':d['sha256']})
    report=load(job/'output-validation.json')
    assert report['status']=='pass' and report['protection_exact'] and report['frame_restored'] and report['source_unchanged']
    receipt={'status':'pass_in_declared_scope','job_id':r['job_id'],'cli_version':r['cli_version'],
             'report':desc(job/'output-validation.json'),'geometry':desc(job/'output-geometry.json'),'request':desc(out/'request.json')}
    if config['phase']=='producer':receipt['candidate']=report['candidate']
    D._write_json(out/'ACCEPTED.json',receipt)
    print(json.dumps(receipt,ensure_ascii=False),flush=True)
    return receipt
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=['describe','build','reference'])
    for n in ['source','expected-sha256','external-inputs','expected-external-inputs-sha256','out-dir']:p.add_argument('--'+n)
    p.add_argument('--timeout',type=int,default=360);a=p.parse_args(argv)
    if a.operation=='describe':
        print(json.dumps({'version':'0.22.0-experimental','operations':['describe','build','reference'],
            'scope':'Fixed 1..48 manual demo on front-02/rear-02, not generic migration',
            'build_source_sha256':PAIR_SHA,'reference_source_sha256':BASE_SHA,
            'frames':[1,8,16,24,32,40,48],'rotation_delta_degrees':{'front':[0,2,-2,2,-2,0,0],'rear':[0,-2,2,-2,2,0,0]},
            'external_inputs':'17 original static descriptors, explicit JSON SHA','out_dir':'new absolute directory'},ensure_ascii=False));return
    assert all(getattr(a,n.replace('-','_')) for n in ['source','expected-sha256','external-inputs','expected-external-inputs-sha256','out-dir'])
    assert a.expected_sha256==(PAIR_SHA if a.operation=='build' else BASE_SHA)
    source={'file':str(Path(a.source).resolve()),'expected_sha256':a.expected_sha256};H.check_descriptor(source)
    external_file={'file':str(Path(a.external_inputs).resolve()),'expected_sha256':a.expected_external_inputs_sha256};H.check_descriptor(external_file)
    ext=load(a.external_inputs);assert len(ext)==17
    for d in ext:H.check_descriptor(d)
    impl=implementation();out=D._run_dir(a.out_dir)
    c={'input':source,'implementation':impl,'external_inputs':ext,'addon_root':str(ROOT/'addon'),'producer':None,'phase':'reference' if a.operation=='reference' else 'producer'}
    first=phase(c,out/c['phase'],a.timeout)
    if a.operation=='reference':return
    c={**c,'input':first['candidate'],'producer':first['report'],'phase':'cold'}
    cold=phase(c,out/'cold',a.timeout)
    assert first['geometry']['expected_sha256']==cold['geometry']['expected_sha256'],'Producer/cold geometry differs; compare offline'
    bundle={'schema':'hrig.hair-manual-demo-bundle.v1','status':'succeeded','source':source,'candidate':first['candidate'],
            'producer':first,'cold':cold,'implementation':impl,'external_inputs_file':external_file,
            'scope':'Fixed manual demo, two independent new OBJECT Actions, original head Action preserved; not full rig/ROM/collision/FPS'}
    D._write_json(out/'bundle.json',bundle)
if __name__=='__main__':main()
