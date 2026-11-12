# SPDX-License-Identifier: GPL-3.0-or-later
"""Background-only, package-fingerprinted closed exchange worker."""
import sys,json,hashlib,importlib.util,traceback,time,os
from pathlib import Path
import bpy

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
    request_path=Path(sys.argv[sys.argv.index('--')+1]).resolve();request=json.loads(request_path.read_text(encoding='utf8'));job=request_path.parent
    def write(name,value):
        target=job/name;temp=job/(name+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8');temp.replace(target)
    try:
        if not bpy.app.background:raise RuntimeError('Exchange worker is background-only')
        root=Path(__file__).resolve().parent
        if Path(request['package_root']).resolve()!=root or digest(root/'exchange-runtime-manifest.json')!=request['package_manifest_sha256']:raise RuntimeError('Package manifest identity changed')
        manifest=json.loads((root/'exchange-runtime-manifest.json').read_text(encoding='utf8'))
        actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name!='exchange-runtime-manifest.json'}
        if actual!=set(manifest['files']):raise RuntimeError('Installed package set changed')
        for rel,sha in manifest['files'].items():
            p=root/rel
            if not p.resolve().is_relative_to(root) or digest(p)!=sha:raise RuntimeError('Installed code changed: '+rel)
        denied=[Path(r'F:\Blender AI\tools').resolve(),Path(r'F:\Blender AI\work\rigging-workflow-c94').resolve()]
        blocked=[]
        def guard(event,args):
            if event=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
                try:p=Path(os.fsdecode(args[0])).resolve()
                except (ValueError,TypeError,OSError):return
                if any(p.is_relative_to(d) for d in denied):blocked.append(str(p));raise RuntimeError('Development-path dependency refused: '+str(p))
        sys.addaudithook(guard)
        old=sys.modules.get('rigging_workflow')
        if old is not None and Path(getattr(old,'__file__','')).resolve()!=root/'__init__.py':raise RuntimeError('Foreign qualified addon namespace occupied')
        if old is None:
            spec=importlib.util.spec_from_file_location('rigging_workflow',root/'__init__.py',submodule_search_locations=[str(root)]);package=importlib.util.module_from_spec(spec);sys.modules['rigging_workflow']=package;spec.loader.exec_module(package)
        from rigging_workflow.exchange_runtime import io_support as io,contract,core,native_import,driver_policy,source_policy,resource_policy
        request=io.read_json(request_path)
        if request['operation'] not in ('export','import','inspect'):raise io.Failure('INVALID_REQUEST','Unknown worker operation')
        write('status.json',{'state':'running','job_id':request['job_id'],'operation':request['operation'],'pid':os.getpid()})
        desc=request['input'];source=Path(desc['file']);started=time.monotonic()
        with io.FileGuard(source) as lock:
            if lock.sha256()!=desc['expected_sha256']:raise io.Failure('CONFLICT','Protected source changed before open')
            params={'file':str(source),'expected_sha256':desc['expected_sha256'],'resources':request['resources']}
            if request['driver_profile'] is not None:params['driver_profile']=request['driver_profile']
            if request['operation'] in ('inspect','export'):
                bpy.ops.wm.open_mainfile(filepath=str(source),load_ui=False,use_scripts=False)
            if request['operation']=='inspect':
                if request.get('auto_resources'):
                    discovered=resource_policy.discover_resources();combined={r['file']:r['expected_sha256'] for r in params['resources']}
                    for r in discovered:
                        if r['file'] in combined and combined[r['file']]!=r['expected_sha256']:raise io.Failure('CONFLICT','Explicit resource differs from snapshot discovery')
                        combined[r['file']]=r['expected_sha256']
                    params['resources']=[{'file':p,'expected_sha256':s} for p,s in sorted(combined.items())]
                from contextlib import ExitStack
                profile=driver_policy.audit(source)['profile_template']
                if profile['acknowledged_unresolved']:raise io.Failure('UNSUPPORTED','Snapshot has unresolved driven paths; automatic acknowledgement refused')
                with ExitStack() as stack:
                    resources=resource_policy.resource_guards(params,stack);source_policy.safe_source(profile,str(source),job);driver_policy.evaluated(profile)
                io.atomic_json(job/'snapshot-profile.json',profile)
                report={'ok':True,'driver_profile':profile,'resource_hashes':resources,'resource_descriptors':params['resources'],'source':desc}
            elif request['operation']=='export':
                spec=contract.normalize_export(request['spec']);report=core.export_shared(params,spec,job,started)
            else:
                spec=contract.normalize_import(request['spec']);report=native_import.import_(params,spec,job)
            if lock.sha256()!=desc['expected_sha256']:raise io.Failure('CONFLICT','Input changed during exchange')
        if request['operation']!='inspect' and not all(c.get('ok') for c in report['checks']):raise io.Failure('VALIDATION_FAILED','Exchange numeric gate failed')
        artifacts=[]
        for d in report.get('outputs',[]):artifacts.append({'file':d['file'],'sha256':io.digest(d['file']),'bytes':Path(d['file']).stat().st_size})
        if request['operation']=='inspect':artifacts.append({'file':str(job/'snapshot-profile.json'),'sha256':io.digest(job/'snapshot-profile.json'),'bytes':(job/'snapshot-profile.json').stat().st_size})
        value={'ok':True,'domain_ok':True,'job_id':request['job_id'],'operation':request['operation'],'package_manifest_sha256':request['package_manifest_sha256'],'report':report,'artifacts':artifacts,'source_sha256_after':digest(source),'blocked_development_reads':blocked,'generic_modules_present':[n for n in ('protocol','nodes','exchange','continuous_skin_exchange') if n in sys.modules],'seconds':time.monotonic()-started}
        write('result.json',value);write('status.json',{'state':'succeeded','job_id':request['job_id'],'domain_ok':True})
    except Exception as exc:
        value={'ok':False,'domain_ok':False,'job_id':request.get('job_id'),'error':{'code':getattr(exc,'code','WORKER_FAILURE'),'message':str(exc)},'traceback':traceback.format_exc()}
        write('result.json',value);write('status.json',{'state':'failed','job_id':request.get('job_id'),'error':value['error']})
        print(value['traceback']);return 1
    return 0
if __name__=='__main__':sys.exit(main())
