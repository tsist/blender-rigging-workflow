"""Read-only, bounded native inventory for a single subdivision experiment."""
import hashlib
import json
from pathlib import Path
import sys
sys.dont_write_bytecode=True
import bpy

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def verify(d):
    assert set(d)=={'file','expected_sha256'} and Path(d['file']).is_absolute()
    assert digest(d['file'])==d['expected_sha256'],d['file']
def main(c,directory):
    assert tuple(bpy.app.version)==(5,2,1) and bpy.app.build_hash.decode()=='9e2066aef7ef'
    for d in [c['input'],*c['implementation'],*c['external_inputs']]:verify(d)
    sys.path.insert(0,c['scripts']);sys.path.insert(0,c['inspection_dir'])
    import inspection
    import character_state
    bpy.ops.wm.open_mainfile(filepath=c['input']['file'],use_scripts=False,load_ui=False)
    scene=bpy.data.scenes[c['context']['scene']];layer=scene.view_layers[c['context']['view_layer']]
    bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
    before=character_state.capture();frame=(scene.frame_current,scene.frame_subframe)
    selected=[bpy.data.objects[n] for n in c['objects']]
    rows=[]
    for obj in selected:
        anim=obj.animation_data
        rows.append({'object':inspection.identity(obj),'parent':inspection.identity(obj.parent) if obj.parent else None,
          'modifiers':[{'index':i,'name':m.name,'type':m.type,'path':m.path_from_id(),
                         'settings':inspection.properties(m)} for i,m in enumerate(obj.modifiers)],
          'drivers':[{'path':f.data_path,'index':f.array_index,'mute':f.mute,
                     'driver':{'type':f.driver.type,'expression':f.driver.expression,
                     'variables':[{'name':v.name,'type':v.type,'targets':[inspection.properties(t) for t in v.targets]} for v in f.driver.variables]}}
                     for f in anim.drivers] if anim else [],
          'animation':character_state.animation_extras(obj),
          'action_contents':character_state.action_keyframes(anim.action) if anim and anim.action else None})
    targets=set(selected);consumers=[]
    for obj in bpy.data.objects:
        if obj.parent in targets:consumers.append({'kind':'parent','owner':obj.name,'target':obj.parent.name})
        for m in obj.modifiers:
            for p in m.bl_rna.properties:
                if p.type=='POINTER' and p.identifier!='rna_type':
                    v=getattr(m,p.identifier,None)
                    if isinstance(v,bpy.types.Object) and v in targets:
                        consumers.append({'kind':'modifier_object','owner':obj.name,'modifier':m.name,'field':p.identifier,'target':v.name})
        owners=[(obj.name,obj.constraints)]
        if obj.pose:owners += [(obj.name+'/'+b.name,b.constraints) for b in obj.pose.bones]
        for owner,constraints in owners:
            for co in constraints:
                for p in co.bl_rna.properties:
                    if p.type=='POINTER' and p.identifier!='rna_type':
                        v=getattr(co,p.identifier,None)
                        if isinstance(v,bpy.types.Object) and v in targets:
                            consumers.append({'kind':'constraint_target','owner':owner,'constraint':co.name,'field':p.identifier,'target':v.name})
    for item in inspection.all_ids():
        anim=getattr(item,'animation_data',None)
        if not anim:continue
        for f in anim.drivers:
            for v in f.driver.variables:
                for t in v.targets:
                    if t.id in targets:
                        consumers.append({'kind':'driver_target','owner':inspection.identity(item),'path':f.data_path,'target':t.id.name,'target_path':t.data_path})
    callbacks={name:[getattr(f,'__module__','?')+'.'+getattr(f,'__qualname__',repr(f)) for f in getattr(bpy.app.handlers,name)]
               for name in ('frame_change_pre','frame_change_post','depsgraph_update_pre','depsgraph_update_post')}
    after=character_state.capture()
    assert before==after and frame==(scene.frame_current,scene.frame_subframe)
    for d in [c['input'],*c['implementation'],*c['external_inputs']]:verify(d)
    report={'schema':'hrig.subdivision-preflight.v1','status':'pass','source_unchanged':True,
       'storage_exact':True,'frame_unchanged':True,'source':c['input'],'objects':rows,'consumers':consumers,
       'handlers':callbacks,'blender':{'version':bpy.app.version_string,'build':bpy.app.build_hash.decode()},
       'coverage':'Declared object modifiers/animation paths, object-parent and constraint/modifier pointers, driver object targets; no arbitrary script/opaque node inference',
       'source_saved':False,'FPS_claim':False}
    Path(directory,'output-diagnostic.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return {'status':'pass','source_unchanged':True}

if __name__=='__main__':result=main(parameters,job_directory)
