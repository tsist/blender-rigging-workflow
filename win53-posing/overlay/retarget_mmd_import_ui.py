# SPDX-License-Identifier: GPL-3.0-or-later
"""PMX/VMD file entry over public blenderctl jobs; no GUI MMD Tools import."""
import hashlib,json,os,re,shutil,subprocess,time,uuid,zipfile
from pathlib import Path
import bpy
from bpy.props import BoolProperty,EnumProperty,IntProperty,PointerProperty,StringProperty
from . import retarget_mmd_standard as standard

OPTIONS='c94_retarget_mmd_import_options_v1'
MMD_OPTIONS='c94_retarget_mmd_options_v1'
_TASK=None
_REGISTERED=[]
_OWNED=False
TERMINAL={'succeeded','failed','cancelled','interrupted','timed_out'}

def require(value,message):
    if not value:raise ValueError(message)

def _backend():
    from . import mmd_source_backend
    return mmd_source_backend

def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for data in iter(lambda:handle.read(1024*1024),b''):digest.update(data)
    return digest.hexdigest()

def _descriptor(path):
    path=Path(path).resolve(strict=True)
    require(path.is_file(),'资源不是文件：'+str(path))
    return {'file':str(path),'expected_sha256':_sha(path)}

def _absolute_file(value,suffix=None):
    path=Path(bpy.path.abspath(value))
    require(path.is_absolute() and path.is_file(),'请明确选择存在的绝对文件路径：'+str(path))
    require(suffix is None or path.suffix.lower()==suffix,'文件格式应为'+str(suffix))
    return path.resolve(strict=True)

def _small_json(path,limit=32768):
    """Use CLI's read/share-delete contract for atomic Windows status files."""
    path=Path(path)
    require(path.stat().st_size<=limit,'状态/结果超过当前读取预算')
    if os.name=='nt':
        import ctypes,msvcrt
        from ctypes import wintypes
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        k.CreateFileW.restype=wintypes.HANDLE
        k.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=k.CreateFileW(str(path.resolve()),0x80000000,7,None,3,0x80,None)
        if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        except BaseException:k.CloseHandle(handle);raise
        with os.fdopen(fd,'rb') as stream:raw=stream.read(limit+1)
    else:raw=path.read_bytes()
    require(len(raw)<=limit,'状态/结果超过当前读取预算')
    return json.loads(raw.decode('utf-8-sig'))

def _cli(cli,shell,jobs_root,args):
    require(Path(cli).is_absolute() and Path(cli).is_file(),'公共CLI路径无效')
    shell=shell or shutil.which('pwsh')
    require(shell and Path(shell).is_file(),'未发现pwsh；请配置已有PowerShell，不自动安装')
    command=[str(shell),'-NoProfile','-NonInteractive','-File',str(cli),'--jobs-dir',str(jobs_root),*args]
    result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding='utf-8',errors='replace',timeout=20,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),shell=False)
    lines=[line.strip() for line in result.stdout.splitlines() if line.strip().startswith('{')]
    require(lines,'CLI未返回JSON；'+result.stderr[-300:])
    payload=json.loads(lines[-1]);require(result.returncode==0 and payload.get('ok'),
        '公共CLI失败：'+str(payload.get('error') or result.stderr[-300:]))
    return payload

def _clip_requests(raw):
    value=json.loads(raw or '[]');require(isinstance(value,list),'额外片段须为JSON数组')
    ids=set();paths=set();rows=[]
    for item in value:
        require(isinstance(item,dict) and set(item)<={'id','vmd','fps'},'额外片段只能含id/vmd/fps')
        require(isinstance(item.get('id'),str) and item['id'].strip() and item['id'] not in ids,'额外片段ID须非空且不重复')
        require(type(item.get('fps')) in (int,float) and item['fps']==30,'当前已验VMD后端须显式fps=30')
        path=_absolute_file(item.get('vmd',''),'.vmd')
        require(path not in paths,'同一VMD仅登记一次，重复请求请移除：'+str(path))
        ids.add(item['id']);paths.add(path);rows.append({'id':item['id'],'vmd':str(path),'fps':30.})
    return rows

def prepare_request(settings):
    """Prepare immutable code/resources packet only; never serialize GUI scene."""
    chosen=standard.source_parameters(mode=settings['mode'],pmx=settings.get('pmx'),
        resource_root=settings.get('resource_root') or None,reference_acknowledged=settings.get('reference_acknowledged',False))
    vmd=_absolute_file(settings.get('vmd',''),'.vmd');clips=_clip_requests(settings.get('clips_json','[]'))
    primary=settings.get('primary_clip_id','dance').strip()
    require(primary and primary not in {row['id'] for row in clips},'主片段ID须非空且不与额外片段重复')
    require(vmd not in {Path(row['vmd']) for row in clips},'同一VMD仅登记一次，重复请求请移除：'+str(vmd))
    cli=_absolute_file(settings.get('cli_path',''),'.ps1')
    output=Path(bpy.path.abspath(settings.get('output_root','')))
    require(output.is_absolute() and str(settings.get('output_root','')).strip(),'请明确选择绝对后台输出根目录')
    output.mkdir(parents=True,exist_ok=True)
    entry_id=uuid.uuid4().hex;packet=output/('packet-'+entry_id);packet.mkdir(exist_ok=False)
    package=Path(__file__).resolve().parent;runtime=packet/'runtime/rigging_workflow'
    # Only source code/legal files. No current .blend, private C94/target asset,
    # texture, audio or copied user scene enters the background packet.
    for source in package.rglob('*'):
        if source.is_file() and '__pycache__' not in source.parts and (source.suffix in ('.py','.toml') or source.name.startswith('LICENSE') or source.name=='THIRD_PARTY_NOTICES.md'):
            dest=runtime/source.relative_to(package);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    worker=runtime/'retarget_mmd_import_worker.py';require(worker.is_file(),'缺少同包后台源导入worker')
    inspection=_backend().inspect_backend(settings.get('addon_path') or None,settings.get('opencc_wheel') or None)
    require(inspection.get('available') and inspection.get('verified'),'未发现当前已验MMD Tools后端；请配置已有路径，不自动安装')
    manifest=runtime/'exchange-runtime-manifest.json'
    manifest.write_text(json.dumps({'schema':'c94.mmd_file_entry.packet.v1','files':{
        path.relative_to(runtime).as_posix():_sha(path) for path in sorted(runtime.rglob('*')) if path.is_file()}},ensure_ascii=False,indent=2),encoding='utf-8')
    archive=packet/'runtime.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(runtime.rglob('*')):
            if path.is_file():z.write(path,'rigging_workflow/'+path.relative_to(runtime).as_posix())
    params={'package':str(runtime),'entry_id':entry_id,'mode':settings['mode'],'pmx':chosen['pmx'],
        'resource_root':str(Path(chosen['pmx']).parent) if settings['mode']=='STANDARD_REFERENCE' else None,
        'reference_acknowledged':settings.get('reference_acknowledged',False),
        'vmd':str(vmd),'clips':clips,'primary_clip_id':primary,'addon_path':inspection['addon_path'],
        'opencc_wheel':inspection['wheel']['file']}
    files=[worker,archive,manifest,runtime/'mmd_source_backend.py',runtime/'retarget_mmd_standard.py',chosen['pmx'],vmd,
        *chosen['resource_dependencies'],*[row['vmd'] for row in clips],inspection['manifest'],inspection['wheel']['file'],
        *[row['file'] for row in inspection['files'].values()]]
    resources={str(Path(path).resolve(strict=True)):_descriptor(path) for path in files}
    request={'schema_version':'1.0','command':'extension.run','params':{'resources':list(resources.values()),
        'manifest':{'trust':'execute_local_python_without_sandbox','script':_descriptor(worker),'params':params,
            'resource_max_bytes':1073741824,'outputs':['output-source.blend','output-source-profile.json','output-source-descriptor.json',
                'output-import-receipt.json','output-import-progress.json','output-import-error.txt']}}}
    request_path=packet/'request.json';request_path.write_text(json.dumps(request,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'entry_id':entry_id,'packet':str(packet),'request':str(request_path),'cli':str(cli),'shell':settings.get('shell_path') or '',
        'jobs_root':str(output/'cli-jobs'),'timeout':int(settings.get('timeout_seconds',300)),
        'mode':settings['mode'],'hide_source_render':settings.get('hide_source_render',True),'request_document':request}

def submit(settings):
    global _TASK
    require(_TASK is None,'请先处理并释放当前导入任务；公共CLI输出始终保留')
    began=time.perf_counter();prepared=prepare_request(settings);prepared_at=time.perf_counter()
    response=_cli(prepared['cli'],prepared['shell'],prepared['jobs_root'],
        ['--timeout',str(prepared['timeout']),'--async','request',prepared['request']])
    job_id=response.get('job_id');require(isinstance(job_id,str) and re.fullmatch('[0-9a-f]{32}',job_id),'CLI缺少确切job ID')
    directory=Path(prepared['jobs_root'])/job_id
    require(Path(response['artifacts']['job_directory']).resolve()==directory.resolve(),'CLI返回的job路径与明确输出根不符')
    _TASK={**prepared,'id':job_id,'directory':str(directory),'cli_version':response.get('cli_version'),
        'cache':{'state':'queued'},'append_receipt':None,'submit_seconds':time.perf_counter()-began,
        'prepare_seconds':prepared_at-began,'cli_submit_seconds':time.perf_counter()-prepared_at}
    poll_once();return _TASK

def poll_once():
    """RAM cache only: no bpy evaluation, object scan, hash or CLI process."""
    if _TASK is None:return None
    directory=Path(_TASK['directory'])
    try:
        cache=_small_json(directory/'status.json')
        require(cache.get('job_id')==_TASK['id'],'状态job ID不符')
        progress=directory/'output-import-progress.json'
        if progress.is_file():cache['import_progress']=_small_json(progress)
        _TASK['cache']=cache
    except (OSError,ValueError,RuntimeError) as exc:_TASK['status_read_error']=str(exc)
    return _TASK['cache']

def cancel():
    require(_TASK is not None,'没有本入口提交的导入任务')
    result=_cli(_TASK['cli'],_TASK['shell'],_TASK['jobs_root'],['job','cancel',_TASK['id']])
    poll_once();return result

def completed_artifacts(task=None):
    task=task or _TASK;require(task is not None,'没有本次导入任务')
    poll_once() if task is _TASK else None
    require(task['cache'].get('state')=='succeeded','公共CLI未报告成功终态，不能追加部分结果')
    directory=Path(task['directory']);result=_small_json(directory/'result.json',4*1024*1024)
    require(result.get('ok') is True and result.get('job_id')==task['id'],'公共CLI最终结果不是成功任务')
    outputs={Path(row['file']).name:row for row in result['data']['outputs']}
    for name in ('output-source.blend','output-source-profile.json','output-source-descriptor.json','output-import-receipt.json'):
        row=outputs.get(name);require(row and Path(row['file']).resolve()==(directory/name).resolve(),'缺少确切CLI输出：'+name)
        require(_sha(row['file'])==row['sha256'],'CLI输出字节已改变：'+name)
    receipt=_small_json(directory/'output-import-receipt.json',4*1024*1024)
    require(receipt.get('ok') is True and receipt.get('entry_id')==task['entry_id'],'导入回执身份不符')
    profile=_backend().load_profile_document(directory/'output-source-profile.json');_backend().validate_profile_document(profile)
    require(profile['profile_sha256']==receipt['profile_sha256'],'回执/profile身份不符')
    require(profile['batch_identity']['entry_id']==task['entry_id'],'来源profile不属于本次导入')
    return {'blend':str(directory/'output-source.blend'),'profile_path':str(directory/'output-source-profile.json'),
        'profile':profile,'receipt':receipt}

def _cleanup_owned(loaded,loaded_actions,owned_data,owned_keys=(),owned_materials=()):
    """Only explicit returned objects/actions and their proven new data."""
    for obj in reversed(loaded):
        try:bpy.data.objects.remove(obj,do_unlink=True)
        except ReferenceError:pass
    for kind,data in owned_data:
        try:
            if data.users==0:
                collection=bpy.data.meshes if kind=='MESH' else bpy.data.armatures if kind=='ARMATURE' else None
                if collection is not None:collection.remove(data)
        except ReferenceError:pass
    for key in owned_keys:
        try:
            if key.users==0:bpy.data.shape_keys.remove(key)
        except ReferenceError:pass
    for material in owned_materials:
        try:
            if material.users==0:bpy.data.materials.remove(material)
        except ReferenceError:pass
    for action in loaded_actions:
        try:
            if action.users==int(action.use_fake_user):bpy.data.actions.remove(action)
        except ReferenceError:pass

def append_exact(source_blend,profile,*,scene=None,hide_render=True):
    """Append every declared Action, registering only libraries.load returns."""
    backend=_backend();profile=backend.load_profile_document(profile);backend.validate_profile_document(profile)
    scene=scene or bpy.context.scene;path=Path(source_blend).resolve(strict=True)
    require(scene==bpy.context.scene,'请切到要追加的当前Scene后执行')
    require(path==Path(profile['scene_config']['source_blend']).resolve(),'源blend不是profile登记路径；需明确重绑，不猜测来源')
    objects=profile['objects'];names=[objects['root']['name'],objects['armature']['name'],*[row['name'] for row in objects['meshes']]]
    action_names=[row['name'] for row in profile['actions']];uid=profile['scene_config']['source_uid']
    require(len(names)==len(set(names)) and len(action_names)==len(set(action_names)),'profile对象/Action身份重复')
    require(not set(names)&set(bpy.data.objects.keys()),'源Object与当前文件同名；不重命名、不按相似名认领')
    require(not set(action_names)&set(bpy.data.actions.keys()),'源Action与当前文件同名；完整来源无法精确追加')
    require(not any(obj.get(backend.UID_KEY)==uid for obj in bpy.data.objects),'当前文件已存在来源UID对象')
    require(not any(action.get(backend.UID_KEY)==uid for action in bpy.data.actions),'当前文件已存在来源UID Action')
    require(objects['armature']['data'] not in bpy.data.armatures,'源Armature数据与当前文件同名')
    require(not any(row['data'] in bpy.data.meshes for row in objects['meshes']),'源Mesh数据与当前文件同名')
    before={'camera':scene.camera,'fps':scene.render.fps,'fps_base':scene.render.fps_base,'range':(scene.frame_start,scene.frame_end),
        'frame':scene.frame_current,'subframe':scene.frame_subframe,'active':bpy.context.view_layer.objects.active,'selected':tuple(bpy.context.selected_objects)}
    loaded=[];loaded_actions=[];owned_data=[];owned_keys=[];owned_materials=[]
    prior_materials=set(bpy.data.materials)
    try:
        with bpy.data.libraries.load(str(path),link=False) as (available,requested):
            require(set(names)<=set(available.objects),'登记源blend缺少精确Object')
            require(set(action_names)<=set(available.actions),'登记源blend缺少profile完整Actions')
            # Blender replaces entries in these assigned lists with returned
            # IDs in-place; keep separate immutable expected-name records.
            requested.objects=list(names);requested.actions=list(action_names)
        loaded=[obj for obj in requested.objects if obj is not None];loaded_actions=[a for a in requested.actions if a is not None]
        require(len(loaded)==len(names) and len(loaded_actions)==len(action_names),'libraries.load未返回全部确切IDs')
        require([obj.name for obj in loaded]==names and [a.name for a in loaded_actions]==action_names,'实际追加ID被重命名，拒绝归入来源')
        data_names={objects['armature']['name']:objects['armature']['data'],**{row['name']:row['data'] for row in objects['meshes']}}
        # These data names were absent before load. A renamed/reused dependency
        # is not claimed; it is never deleted as an inferred global difference.
        owned_data=[(obj.type,obj.data) for obj in loaded if obj.name in data_names and obj.data is not None and obj.data.name==data_names[obj.name]]
        owned_keys=[data.shape_keys for kind,data in owned_data if kind=='MESH' and data.shape_keys and data.shape_keys.user==data]
        owned_materials=list({material for kind,data in owned_data if kind=='MESH' for material in data.materials if material is not None and material not in prior_materials})
        for obj in loaded:
            require(obj.get(backend.UID_KEY)==uid,'实际追加Object UID不符')
            if obj.name not in scene.objects:scene.collection.objects.link(obj)
            obj.select_set(False)
        if bpy.context.view_layer.objects.active!=before['active']:bpy.context.view_layer.objects.active=before['active']
        for action,record in zip(loaded_actions,profile['actions']):
            require(action.get(backend.UID_KEY)==uid,'实际追加Action UID不符')
            # Native append clears fake users. Restore only the declared flag
            # on IDs returned by this load, so unbound clips survive saving.
            action.use_fake_user=bool(record['fake_user'])
        resolved=backend.validate_persisted_source(profile,scene=scene,require_scene_config=False,require_all_actions=True)
        pairs=dict(zip(names,loaded));require(resolved['rig']==pairs[objects['armature']['name']],'backend解析对象不是本次返回ID')
        for record in profile['actions']:
            actual=loaded_actions[action_names.index(record['name'])]
            require({(s.identifier,s.target_id_type) for s in actual.slots}=={(s['identifier'],s['target_id_type']) for s in record['slots']},'完整Action Slot集合不符')
        if hide_render:
            for obj in loaded:obj.hide_render=True
        require(scene.camera==before['camera'] and scene.render.fps==before['fps'] and scene.render.fps_base==before['fps_base']
            and (scene.frame_start,scene.frame_end)==before['range'] and scene.frame_current==before['frame'] and scene.frame_subframe==before['subframe'],
            '追加未保留当前Scene相机/时基/帧域')
        require(bpy.context.view_layer.objects.active==before['active'] and tuple(bpy.context.selected_objects)==before['selected'],'追加未保留原选择')
        return {'ok':True,'rig':resolved['rig'],'objects':loaded,'actions':loaded_actions,'profile':profile,
            '_owned_data':owned_data,'_owned_keys':owned_keys,'_owned_materials':owned_materials,
            'all_actions_validated':True,'old_scene_preserved':True,'source_render_hidden':bool(hide_render)}
    except BaseException:
        _cleanup_owned(loaded,loaded_actions,owned_data,owned_keys,owned_materials)
        if bpy.context.view_layer.objects.active!=before['active']:bpy.context.view_layer.objects.active=before['active']
        raise

def collect(context):
    require(_TASK is not None,'没有本入口提交的任务');require(_TASK.get('append_receipt') is None,'本任务已追加，不重复认领来源')
    artifacts=completed_artifacts();require(hasattr(bpy.types.Scene,MMD_OPTIONS),'请先注册已冻结MMD场景界面')
    mmd=getattr(context.scene,MMD_OPTIONS);previous={key:getattr(mmd,key) for key in ('source','source_is_appended','profile_path','profile_text','clip_id','source_fps','mapping_json','clip_choices','summary')}
    appended=append_exact(artifacts['blend'],artifacts['profile'],scene=context.scene,hide_render=_TASK['hide_source_render'])
    try:
        mmd.source=appended['rig'];mmd.source_is_appended=True;mmd.profile_path=artifacts['profile_path']
        ret=bpy.ops.c94_workflow.retarget_mmd_scene(operation='LOAD_PROFILE')
        require(ret=={'FINISHED'},'MMD场景UI未接受严格完整来源')
        mmd.clip_id=artifacts['profile']['primary_clip'];clip=next(c for c in artifacts['profile']['clips'] if c['id']==mmd.clip_id)
        mmd.start,mmd.end=clip['range'];mmd.source_fps=clip['fps']
        _TASK['append_receipt']={'scene':context.scene.name,'source_uid':artifacts['receipt']['source_uid'],
            'objects':[o.name for o in appended['objects']],'actions':[a.name for a in appended['actions']],
            'profile_text':mmd.profile_text.name,'all_actions_validated':True}
        return appended
    except BaseException:
        new_text=mmd.profile_text if mmd.profile_text!=previous['profile_text'] else None
        for key,value in previous.items():setattr(mmd,key,value)
        if new_text and new_text.users==int(new_text.use_fake_user):bpy.data.texts.remove(new_text)
        _cleanup_owned(appended['objects'],appended['actions'],appended['_owned_data'],appended['_owned_keys'],appended['_owned_materials'])
        _TASK['append_receipt']=None
        raise

def _timer():
    if not _OWNED:return None
    poll_once()
    for wm in bpy.data.window_managers:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type=='VIEW_3D':area.tag_redraw()
    return .75

class C94MMDIMPORT_Options(bpy.types.PropertyGroup):
    mode:EnumProperty(name='来源方式',items=(('EXPLICIT_PMX','明确原PMX（优先）','选择要作为动作来源的实际PMX'),('STANDARD_REFERENCE','无原PMX：Uka标准参考源','不保证原模型rest/比例，自定义骨差异明确报告')),default='EXPLICIT_PMX')
    pmx:StringProperty(name='PMX模型',subtype='FILE_PATH');vmd:StringProperty(name='主VMD动作',subtype='FILE_PATH')
    resource_root:StringProperty(name='已核Uka资源目录',subtype='DIR_PATH')
    reference_acknowledged:BoolProperty(name='明确接受标准参考源及原模型/比例不保证',default=False)
    primary_clip_id:StringProperty(name='主片段ID',default='dance')
    clips_json:StringProperty(name='同源额外片段 JSON（id/vmd/fps=30）',default='[]')
    cli_path:StringProperty(name='公共blenderctl.ps1',subtype='FILE_PATH',default='')
    shell_path:StringProperty(name='已有pwsh路径（空则PATH）',subtype='FILE_PATH')
    output_root:StringProperty(name='明确后台输出根',subtype='DIR_PATH')
    addon_path:StringProperty(name='已有MMD Tools目录（空则已知便携布局）',subtype='DIR_PATH')
    opencc_wheel:StringProperty(name='已有OpenCC wheel（空则后端定位）',subtype='FILE_PATH')
    timeout_seconds:IntProperty(name='CLI最长导入秒数',default=300,min=10,max=86400)
    hide_source_render:BoolProperty(name='新源渲染隐藏（保持可求值）',default=True)
    message:StringProperty(default='文件只在公共CLI后台导入，当前旧角色/动作与相机保持')

class C94MMDIMPORT_OT_action(bpy.types.Operator):
    bl_idname='c94_workflow.retarget_mmd_import';bl_label='后台导入MMD文件'
    operation:EnumProperty(items=[(v,v,'') for v in ('SUBMIT','REFRESH','CANCEL','COLLECT','RELEASE')])
    def execute(self,context):
        global _TASK
        opts=getattr(context.scene,OPTIONS)
        try:
            if self.operation=='SUBMIT':
                keys=('mode','pmx','vmd','resource_root','reference_acknowledged','primary_clip_id','clips_json','cli_path','shell_path','output_root','addon_path','opencc_wheel','timeout_seconds','hide_source_render')
                task=submit({key:getattr(opts,key) for key in keys});opts.message='已提交公共CLI job '+task['id']
            elif self.operation=='REFRESH':poll_once();opts.message='已读取公共CLI状态'
            elif self.operation=='CANCEL':cancel();opts.message='已通过公共CLI请求取消，等待其终态；当前Scene保持'
            elif self.operation=='COLLECT':
                appended=collect(context);profile=appended['profile']
                missing=sum(len(clip.get('missing',{}).get('bone_channels',[]))+len(clip.get('missing',{}).get('morph_channels',[])) for clip in profile['clips'])
                opts.message='已追加完整来源并载入；未匹配通道%d项，见profile Text；贴图未闭合，未做修正'%missing
            else:
                require(_TASK and _TASK['cache'].get('state') in TERMINAL,'任务未终态，不能释放界面句柄')
                _TASK=None;opts.message='已释放界面句柄，公共CLI输出与已追加来源保留'
            return {'FINISHED'}
        except Exception as exc:opts.message=str(exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}

class C94MMDIMPORT_PT_panel(bpy.types.Panel):
    bl_idname='C94MMDIMPORT_PT_panel';bl_label='PMX/VMD文件 → 后台登记来源'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='动画重定向';bl_parent_id='C94MMD_PT_scene'
    def draw(self,context):
        opts=getattr(context.scene,OPTIONS);layout=self.layout
        advanced=getattr(getattr(context.scene,'c94_workspace_options_v1',None),'level','ADVANCED')!='DAILY'
        layout.prop(opts,'mode')
        if opts.mode=='EXPLICIT_PMX':layout.prop(opts,'pmx')
        else:
            layout.prop(opts,'resource_root');layout.prop(opts,'reference_acknowledged')
            layout.label(text='Uka / CC BY 4.0；不是任意VMD的原模型')
        layout.prop(opts,'vmd');layout.prop(opts,'primary_clip_id');layout.prop(opts,'output_root')
        if advanced:
            layout.prop(opts,'clips_json');layout.prop(opts,'cli_path');layout.prop(opts,'shell_path')
            layout.prop(opts,'addon_path');layout.prop(opts,'opencc_wheel');layout.prop(opts,'timeout_seconds');layout.prop(opts,'hide_source_render')
        else:layout.label(text='依赖路径与额外片段位于进阶',icon='INFO')
        row=layout.row();row.enabled=_TASK is None
        row.operator('c94_workflow.retarget_mmd_import',text='后台导入并登记原生来源').operation='SUBMIT'
        if _TASK:
            cache=_TASK['cache'];box=layout.box()
            status=str(cache.get('state','queued'))
            box.label(text={'queued':'等待处理','running':'正在导入','succeeded':'导入完成',
                            'failed':'导入失败','cancelled':'已取消','cancelling':'取消中'}.get(status,status))
            if advanced:
                box.label(text=_TASK['id'])
                if cache.get('import_progress'):box.label(text=cache['import_progress'].get('phase',''))
                box.label(text=_TASK['directory'])
            row=box.row(align=True);row.operator('c94_workflow.retarget_mmd_import',text='刷新').operation='REFRESH'
            row.operator('c94_workflow.retarget_mmd_import',text='请求取消').operation='CANCEL'
            row=box.row();row.enabled=cache.get('state')=='succeeded' and _TASK['append_receipt'] is None
            row.operator('c94_workflow.retarget_mmd_import',text='追加完整来源并载入当前场景').operation='COLLECT'
            row=box.row();row.enabled=cache.get('state') in TERMINAL
            row.operator('c94_workflow.retarget_mmd_import',text='释放任务句柄').operation='RELEASE'
        layout.label(text=opts.message[:80],icon='INFO')
        layout.label(text='不在GUI注册MMD Tools，不更换相机，不自动做曲线/接触修正')

def register():
    global _OWNED
    require(hasattr(bpy.types.Scene,MMD_OPTIONS),'请先注册已冻结MMD场景UI')
    require(not hasattr(bpy.types.Scene,OPTIONS),'文件导入界面已注册，原实例保持')
    try:
        for cls in (C94MMDIMPORT_Options,C94MMDIMPORT_OT_action,C94MMDIMPORT_PT_panel):bpy.utils.register_class(cls);_REGISTERED.append(cls)
        setattr(bpy.types.Scene,OPTIONS,PointerProperty(type=C94MMDIMPORT_Options));_OWNED=True
        bpy.app.timers.register(_timer,first_interval=.75,persistent=True)
    except Exception:unregister();raise

def unregister():
    global _OWNED,_TASK
    if bpy.app.timers.is_registered(_timer):bpy.app.timers.unregister(_timer)
    if _OWNED and hasattr(bpy.types.Scene,OPTIONS):delattr(bpy.types.Scene,OPTIONS)
    _OWNED=False;_TASK=None
    for cls in reversed(_REGISTERED):
        if getattr(cls,'is_registered',False):bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
