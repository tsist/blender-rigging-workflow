# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed-package exchange jobs shared by GUI and AI; no GUI scene resets."""
import hashlib,json,subprocess,uuid,os,time
from pathlib import Path
import bpy
from .exchange_runtime import contract
from .exchange_runtime.io_support import Failure,atomic_json,read_json,digest
_JOBS={}
_CHAINS={}
def package_manifest():
    root=Path(__file__).resolve().parent;path=root/'exchange-runtime-manifest.json'
    doc=read_json(path)
    if set(doc)!={'schema','files','source_map_sha256','host','scope'} or doc['schema']!='c94.exchange-runtime.package.v1':raise Failure('CONFLICT','Invalid exchange package manifest')
    declared=set(doc['files']);actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name!='exchange-runtime-manifest.json'}
    if declared!=actual:raise Failure('CONFLICT','Installed package file set differs from manifest')
    for rel,expected in doc['files'].items():
        path2=root/rel
        if not path2.resolve().is_relative_to(root) or digest(path2)!=expected:raise Failure('CONFLICT','Installed module fingerprint differs: '+rel)
    return root,digest(path),doc

def _descriptor(value):
    if not isinstance(value,dict) or set(value)!={'file','expected_sha256'} or not Path(value['file']).is_absolute():raise Failure('INVALID_REQUEST','Explicit absolute file/SHA descriptor required')
    p=Path(value['file'])
    if digest(p)!=value['expected_sha256']:raise Failure('CONFLICT','Input SHA changed: '+str(p))
    return dict(value)

def _watchdog():
    running=False
    for chain in tuple(_CHAINS):
        row=_advance_chain(chain)
        if row['state'] in ('queued','running'):running=True
    for handle in tuple(_JOBS):
        row=status(handle)
        if row['state'] in ('queued','running'):running=True
    return .25 if running else None

def _start(operation,descriptor,spec,resources,profile,jobs_root,auto_resources=False):
    root,manifest_sha,_=package_manifest();descriptor=_descriptor(descriptor)
    if operation=='export':contract.normalize_export(spec)
    if operation=='import':contract.normalize_import(spec)
    if operation not in ('export','import','inspect'):raise Failure('INVALID_REQUEST','Unknown exchange operation')
    checked=[_descriptor(d) for d in resources]
    if operation=='export':
        if profile is None:raise Failure('INVALID_REQUEST','Export requires an inspected snapshot driver profile; run inspect_snapshot first')
        contract.normalize_profile(profile)
        if profile['source_sha256']!=descriptor['expected_sha256']:raise Failure('CONFLICT','Profile belongs to another snapshot')
    jobs=Path(jobs_root)
    if not jobs.is_absolute():raise Failure('INVALID_REQUEST','jobs_root must be absolute')
    jobs.mkdir(parents=True,exist_ok=True);handle=uuid.uuid4().hex;directory=jobs/handle;directory.mkdir(exist_ok=False)
    request={'schema':'c94.exchange-job.v1','operation':operation,'input':descriptor,'spec':spec,'resources':checked,'driver_profile':profile,'package_root':str(root),'package_manifest_sha256':manifest_sha,'job_id':handle,'job_directory':str(directory),'auto_resources':bool(auto_resources)}
    atomic_json(directory/'request.json',request);atomic_json(directory/'status.json',{'state':'queued','job_id':handle})
    log=(directory/'worker.log').open('wb')
    env=os.environ.copy()
    for key,sub in (('BLENDER_USER_CONFIG','config'),('BLENDER_USER_SCRIPTS','scripts'),('BLENDER_USER_DATAFILES','datafiles')):
        target=directory/'profile'/sub;target.mkdir(parents=True,exist_ok=True);env[key]=str(target)
    try:
        proc=subprocess.Popen([bpy.app.binary_path,'--background','--factory-startup','--disable-autoexec','--offline-mode','--python',str(root/'exchange_worker.py'),'--',str(directory/'request.json')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,shell=False,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    finally:log.close()
    _JOBS[handle]={'process':proc,'directory':directory}
    if not bpy.app.timers.is_registered(_watchdog):bpy.app.timers.register(_watchdog,first_interval=.25,persistent=True)
    return {'job_id':handle,'directory':str(directory),'operation':operation,'package_manifest_sha256':manifest_sha,'worker_pid':proc.pid}

def start_export(snapshot_descriptor,spec,*,resources,profile,jobs_root):return _start('export',snapshot_descriptor,spec,resources,profile,jobs_root)
def start_import(format_descriptor,spec,*,resources,jobs_root):return _start('import',format_descriptor,spec,resources,None,jobs_root)
def inspect_snapshot(snapshot_descriptor,*,resources,jobs_root,auto_resources=False):return _start('inspect',snapshot_descriptor,{},resources,None,jobs_root,auto_resources)
def status(handle):
    if handle in _CHAINS:return _advance_chain(handle)
    if handle not in _JOBS:raise Failure('CONFLICT','Job handle is not owned by this live runtime')
    item=_JOBS[handle];directory=item['directory'];row=read_json(directory/'status.json');code=item['process'].poll()
    if code is not None and row['state'] in ('queued','running'):
        row={'state':'failed','job_id':handle,'exit_code':code,'error':'worker exited without terminal status'};atomic_json(directory/'status.json',row)
    value={**row,'directory':str(directory),'exit_code':code};item['last_status']=value;return value
def result(handle):
    if handle in _CHAINS:
        row=_advance_chain(handle)
        if row['state']!='succeeded':raise Failure('CONFLICT','Prepared export is not succeeded')
        return result(_CHAINS[handle]['child']['job_id'])
    row=status(handle)
    if row['state']!='succeeded':raise Failure('CONFLICT','Exchange job is not succeeded: '+row['state'])
    value=read_json(Path(row['directory'])/'result.json')
    if not value.get('ok'):raise Failure('VALIDATION_FAILED','Worker domain result failed')
    for d in value.get('artifacts',[]):
        if digest(d['file'])!=d['sha256']:raise Failure('CONFLICT','Output artifact changed')
    return value

def cancel(handle):
    if handle in _CHAINS:
        chain=_CHAINS[handle];row=_advance_chain(handle)
        if row['state'] not in ('queued','running'):return {'cancelled':False,'state':row['state']}
        value=cancel(chain['child']['job_id']);chain['state']='cancelled';return value
    row=status(handle);item=_JOBS[handle];proc=item['process']
    if proc.poll() is not None:return {'cancelled':False,'state':row['state']}
    proc.terminate()
    try:proc.wait(timeout=2)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)
    terminal={'state':'cancelled','job_id':handle,'owned_process':True,'exit_code':proc.returncode};atomic_json(item['directory']/'status.json',terminal)
    return {'cancelled':True,**terminal}

def capture_snapshot(jobs_root):
    root=Path(jobs_root)
    if not root.is_absolute():raise Failure('INVALID_REQUEST','jobs_root must be absolute')
    if bpy.app.background:raise Failure('UNSUPPORTED','Interactive snapshot requires a GUI session')
    before={'file':bpy.data.filepath,'dirty':bpy.data.is_dirty,'frame':bpy.context.scene.frame_current,'subframe':bpy.context.scene.frame_subframe,'active':bpy.context.view_layer.objects.active,'selected':tuple(o.name for o in bpy.context.selected_objects)}
    root.mkdir(parents=True,exist_ok=True);path=root/('snapshot-'+uuid.uuid4().hex+'.blend')
    result_code=bpy.ops.wm.save_as_mainfile(filepath=str(path),copy=True,relative_remap=True,check_existing=False)
    if result_code!={'FINISHED'}:raise Failure('VALIDATION_FAILED','Snapshot save failed')
    if (bpy.data.filepath!=before['file'] or bpy.data.is_dirty!=before['dirty'] or bpy.context.scene.frame_current!=before['frame'] or bpy.context.scene.frame_subframe!=before['subframe'] or bpy.context.view_layer.objects.active!=before['active'] or tuple(o.name for o in bpy.context.selected_objects)!=before['selected']):raise Failure('VALIDATION_FAILED','Snapshot changed GUI session state; source data was not replaced')
    return {'file':str(path),'expected_sha256':digest(path)}


def start_export_prepared(snapshot_descriptor,spec,*,jobs_root):
    """Inspect this exact snapshot, then queue export with its real profile/resources."""
    contract.normalize_export(spec);snapshot_descriptor=_descriptor(snapshot_descriptor)
    child=inspect_snapshot(snapshot_descriptor,resources=[],jobs_root=jobs_root,auto_resources=True)
    handle='chain-'+uuid.uuid4().hex
    _CHAINS[handle]={'state':'running','phase':'inspect','child':child,'snapshot':snapshot_descriptor,'spec':json.loads(json.dumps(spec)),'jobs_root':jobs_root}
    return {'job_id':handle,'directory':child['directory'],'operation':'prepared_export','state':'running','phase':'inspect'}

def _advance_chain(handle):
    chain=_CHAINS[handle]
    if chain['state'] in ('failed','cancelled','succeeded'):
        return {'job_id':handle,'state':chain['state'],'phase':chain['phase'],'directory':chain['child']['directory'],'child_job_id':chain['child']['job_id'],'error':chain.get('error')}
    try:
        row=status(chain['child']['job_id'])
        if row['state'] in ('failed','cancelled'):
            chain['state']=row['state'];chain['error']=row.get('error')
        elif row['state']=='succeeded' and chain['phase']=='inspect':
            inspected=result(chain['child']['job_id'])
            profile=inspected['report']['driver_profile'];resources=inspected['report']['resource_descriptors']
            chain['child']=start_export(chain['snapshot'],chain['spec'],resources=resources,profile=profile,jobs_root=chain['jobs_root']);chain['phase']='export'
        elif row['state']=='succeeded':chain['state']='succeeded'
    except Exception as exc:
        chain['state']='failed';chain['error']={'code':getattr(exc,'code','API_ERROR'),'message':str(exc)}
    return {'job_id':handle,'state':chain['state'],'phase':chain['phase'],'directory':chain['child']['directory'],'child_job_id':chain['child']['job_id'],'error':chain.get('error')}

def peek_status(handle):
    """RAM-only UI projection: no hashing, file reads or job dispatch from draw."""
    if handle in _CHAINS:
        chain=_CHAINS[handle];return {'state':chain['state'],'phase':chain['phase'],'directory':chain['child']['directory'],'error':chain.get('error')}
    if handle in _JOBS:return _JOBS[handle].get('last_status',{'state':'queued','directory':str(_JOBS[handle]['directory'])})
    return {'state':'unknown'}
