"""Bounded saved-disk native batch; tokens remain inside this worker lifetime."""
import hashlib,json,os
from pathlib import Path
import bpy,numpy as np
import character_state as C,native_control as S1
from .transport import load_profile,bind
from native_controls.native import AtomicControls

def resolved_resources():
    return {kind:{item.name:{'path':os.path.normcase(os.path.normpath(bpy.path.abspath(item.filepath,library=item.library))),'packed':bool(getattr(item,'packed_file',None))} for item in collection if getattr(item,'filepath','')}
        for kind,collection in [('Image',bpy.data.images),('Library',bpy.data.libraries),('Font',bpy.data.fonts),('Sound',bpy.data.sounds),('MovieClip',bpy.data.movieclips),('Text',bpy.data.texts)]}

def load_image_metadata():
    for image in bpy.data.images:
        if image.source=='FILE' and image.filepath:
            # A real read gives cold and producer observers identical loaded metadata.
            length=len(image.pixels)
            if image.packed_file or Path(bpy.path.abspath(image.filepath,library=image.library)).is_file():assert length>0,image.name

def prepare_saved_state(source):
    before_metadata=C.capture()
    saved={'source':source,'texts':[(t.name,t.as_string()) for t in bpy.data.texts],
        'render_original':bpy.context.scene.render.filepath,'render_absolute':bpy.path.abspath(bpy.context.scene.render.filepath),
        'all_render_targets':{scene.name:bpy.path.abspath(scene.render.filepath) for scene in bpy.data.scenes},'resources_absolute':resolved_resources()}
    # Save relocation must preserve every original resolved external target.
    bpy.ops.file.make_paths_absolute()
    for scene in bpy.data.scenes:scene.render.filepath=saved['all_render_targets'][scene.name]
    for text in bpy.data.texts:text.use_module=False
    assert resolved_resources()==saved['resources_absolute'],{'before':saved['resources_absolute'],'after':resolved_resources()}
    before_load=C.capture()
    load_image_metadata()
    from native_controls.native import get
    after_load=C.capture()
    saved['image_metadata_load_changes']=[{'path':list(path),'before':get(before_load,path),'after':get(after_load,path)} for path in S1.changes(before_load,after_load)]
    saved['save_preparation_changes']=[{'path':list(path),'before':get(before_metadata,path),'after':get(after_load,path)} for path in S1.changes(before_metadata,after_load)]
    return saved

def execute(parameters,job_directory):
    job=Path(job_directory)
    def dump(name,value):(job/name).write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False),encoding='utf8')
    source=parameters['input'];assert hashlib.sha256(Path(source['file']).read_bytes()).hexdigest()==source['expected_sha256']
    bpy.ops.wm.open_mainfile(filepath=source['file'],use_scripts=False,load_ui=False)
    bpy.context.window.scene=bpy.data.scenes[parameters['scene']];bpy.context.window.view_layer=bpy.context.scene.view_layers[parameters['layer']];bpy.context.view_layer.update()
    profile=load_profile(json.loads(Path(parameters['profile']).read_bytes()),parameters.get('profile_action','apply_typed'));dump('output-profile-typed.json',profile)
    operations=parameters['operations'];core=AtomicControls(profile);core.preflight(operations)
    saved=prepare_saved_state(source)
    names=parameters['mesh_names'];arrays={}
    for label in ('neutral','applied'):
        if label=='applied':
            binding=bind(profile,operations);snapshot=binding.inspect();applied=binding.apply(snapshot['snapshot_token'])
            saved['retained_pose']={'operations':operations,'inputs':applied['inputs'],'cross_process_restore_token_supported':False}
            dump('output-apply.json',{key:value for key,value in applied.items() if key not in ('witness_before','witness_after','restore_token')})
        output=job/('output-'+label+'.blend');assert not output.exists()
        bpy.ops.wm.save_as_mainfile(filepath=str(output),check_existing=False,relative_remap=False,copy=True)
        bpy.context.view_layer.update();dg=bpy.context.evaluated_depsgraph_get()
        dump('output-'+label+'-author.json',C.capture());dump('output-'+label+'-context.json',S1.context())
        witness={rig.name:{pb.name:np.asarray(rig.evaluated_get(dg).matrix_world@rig.evaluated_get(dg).pose.bones[pb.name].matrix,dtype='<f4').tolist() for pb in rig.pose.bones} for rig in core.consumer_rigs}
        dump('output-'+label+'-witness.json',witness)
        for n,name in enumerate(names):
            obj=bpy.data.objects[name].evaluated_get(dg);mesh=obj.to_mesh()
            try:
                points=np.empty(len(mesh.vertices)*3,dtype='<f4');mesh.vertices.foreach_get('co',points);m=np.asarray(obj.matrix_world,dtype='<f4')
                arrays['output-'+label+'_skin_'+str(n)]=points.reshape(-1,3)@m[:3,:3].T+m[:3,3]
            finally:obj.to_mesh_clear()
        saved[label]={'file':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}
    np.savez_compressed(job/'output-saved-skin.npz',**arrays);dump('output-saved.json',saved)
    exact=all(hashlib.sha256(Path(item['file']).read_bytes()).hexdigest()==item['expected_sha256'] for item in parameters['resources'])
    report={'status':'pass' if exact else 'failed_resource_drift','resources_exact':exact,'source':source,'saved':saved,
        'native_entry':'same BoundBatch/AtomicControls; existing public extension.run','cross_process_restore_token_supported':False}
    dump('output-report.json',report);return {'status':report['status']}
