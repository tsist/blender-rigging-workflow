# SPDX-License-Identifier: GPL-3.0-or-later
"""Executed only by public blenderctl extension.run, not the live GUI."""
import hashlib,json,sys,traceback
from pathlib import Path
sys.dont_write_bytecode=True
import bpy
J=Path(job_directory)
def write(name,value):
    (J/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def phase(value):write('output-import-progress.json',{'phase':value})
try:
    if not bpy.app.background:raise ValueError('MMD文件导入只允许公共CLI后台作业')
    sys.path.insert(0,str(Path(parameters['package']).parent))
    from rigging_workflow import mmd_source_backend as backend,retarget_mmd_standard as standard
    phase('VALIDATE_BACKEND')
    chosen=standard.source_parameters(mode=parameters['mode'],pmx=parameters.get('pmx'),
        resource_root=parameters.get('resource_root'),reference_acknowledged=parameters.get('reference_acknowledged',False))
    identity={**chosen['batch_identity'],'entry_schema':'c94.retarget.mmd_file_entry.v1','entry_id':parameters['entry_id']}
    phase('IMPORT_SOURCE')
    imported=backend.import_source(chosen['pmx'],parameters['vmd'],output_dir=str(J),
        clips=parameters.get('clips',[]),primary_clip_id=parameters['primary_clip_id'],
        scale=.08,axis='MMD_XZY_SWIZZLE',source_fps=30.,addon_path=parameters.get('addon_path') or None,
        opencc_wheel=parameters.get('opencc_wheel') or None,import_camera=False,require_verified=True,
        output_stem='output-source',blend_name='output-source.blend',profile_name='output-source-profile.json',
        descriptor_name='output-source-descriptor.json',batch_identity=identity,overwrite=False,
        expected_pmx_bones=chosen['expected_pmx_bones'],expected_pmx_vertices=chosen['expected_pmx_vertices'],require_complete_fingers=True)
    phase('VALIDATE_OUTPUT')
    profile=backend.load_profile_document(imported['profile_path']);backend.validate_profile_document(profile)
    source_scene=bpy.data.scenes.get(imported['source_scene'])
    if source_scene is None:raise ValueError('backend返回的来源Scene不存在')
    resolved=backend.validate_persisted_source(profile,scene=source_scene,require_scene_config=True,require_all_actions=True)
    if profile.get('batch_identity')!=identity:raise ValueError('backend未原样记录明确来源身份；不在外部改签profile')
    def artifact(path):
        path=Path(path);return {'file':str(path),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    receipt={'schema':'c94.retarget.mmd_file_import_receipt.v1','ok':True,'entry_id':parameters['entry_id'],
        'batch_identity':identity,'profile_sha256':profile['profile_sha256'],
        'source_uid':profile['scene_config']['source_uid'],'objects':{
            'root':resolved['root'].name,'armature':resolved['rig'].name,'meshes':[o.name for o in resolved['meshes']]},
        'all_actions':[a['name'] for a in profile['actions']],'clips':[c['id'] for c in profile['clips']],
        'artifacts':{'blend':artifact(imported['blend_path']),'profile':artifact(imported['profile_path']),
            'descriptor':artifact(imported['descriptor_path'])},
        'limitations':profile.get('limitations',[]),'current_gui_scene_accessed':False,'extra_curve_or_contact_correction':False}
    write('output-import-receipt.json',receipt);(J/'output-import-error.txt').write_text('',encoding='utf-8');phase('COMPLETE')
    result={'ok':True,'entry_id':parameters['entry_id'],'source_uid':receipt['source_uid'],'artifacts':receipt['artifacts'],
        'source_mode':parameters['mode'],'standard_reference':identity['standard_reference'],'objects':receipt['objects'],
        'actions':receipt['all_actions'],'clips':receipt['clips']}
except Exception:
    (J/'output-import-error.txt').write_text(traceback.format_exc(),encoding='utf-8');phase('FAILED');raise
