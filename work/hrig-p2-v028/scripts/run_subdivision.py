"""Pinned worker for single-SUBSURF diagnostic observation; never saves source."""
import hashlib
import json
from pathlib import Path
import sys
sys.dont_write_bytecode=True
import bpy

def verify(d):
    if type(d) is not dict or set(d)!={'file','expected_sha256'}:raise ValueError('Invalid descriptor')
    p=Path(d['file'])
    if not p.is_absolute() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=d['expected_sha256']:
        raise ValueError('Pinned resource changed')
    return p
def main(c,directory):
    if set(c)!={'input','plan','external_inputs','implementation','addon_root'}:raise ValueError('Unknown worker fields')
    if tuple(bpy.app.version)!=(5,2,1) or bpy.app.build_hash.decode()!='9e2066aef7ef':raise ValueError('Unreviewed Blender build')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:raise ValueError('Embedded scripts must be disabled')
    for d in [c['input'],c['plan'],*c['implementation'],*c['external_inputs']]:verify(d)
    sys.path.insert(0,str(Path(__file__).resolve().parent));sys.path.insert(0,c['addon_root'])
    import host_contract as H
    import character_state
    from humanoid_rigging_probe import subdivision_diagnostic as S
    plan_path=Path(c['plan']['file'])
    if plan_path.stat().st_size>1048576:raise ValueError('Plan exceeds 1 MiB')
    plan=S.validate_plan(H.parse_json(plan_path.read_text(encoding='utf-8-sig')))
    bpy.ops.wm.open_mainfile(filepath=c['input']['file'],use_scripts=False,load_ui=False)
    context=plan['observation']['context'];scene=bpy.data.scenes[context['scene']]
    bpy.context.window.scene=scene;bpy.context.window.view_layer=scene.view_layers[context['view_layer']]
    report=S.observe(bpy.context,plan,directory,character_state.capture)
    for d in [c['input'],c['plan'],*c['implementation'],*c['external_inputs']]:verify(d)
    report.update(source=c['input'],source_unchanged=True,implementation_unchanged=True,
        blender={'version':bpy.app.version_string,'build':bpy.app.build_hash.decode()},
        external_inputs=c['external_inputs'],inherited_dependency_completeness='17 declared static descriptors in real case; weak/dynamic references not certified')
    Path(directory,'output-diagnostic.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return {'status':report['status'],'source_unchanged':True,'implementation_unchanged':True,'FPS_claim':False}

if __name__=='__main__':result=main(parameters,job_directory)
