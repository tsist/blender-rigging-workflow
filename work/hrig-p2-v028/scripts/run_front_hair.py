"""Pinned producer/cold worker for the experimental Aon front-hair branch."""
import hashlib,json,sys,traceback
from pathlib import Path
from array import array
sys.dont_write_bytecode=True
AUTHOR='4e744bb99b3e2706656dc183565d7d8839225762504bf464704508fdef3f14d7'
SOURCE='216f4c0a8fa919d538ca95c45e3dea2d4157974ee5c5036d0f99885e6c9a10bd'
def run(c,directory):
    import bpy
    assert set(c)=={'phase','source','input','implementation','external_inputs','addon_root','producer'}
    assert c['phase'] in ('producer','cold')
    assert c['source']['expected_sha256']==SOURCE and len(c['external_inputs'])==17
    assert bpy.app.version==(5,2,1) and bpy.app.build_hash.decode()=='9e2066aef7ef'
    assert not bpy.context.preferences.filepaths.use_scripts_auto_execute
    resources=[c['source'],c['input'],*c['implementation'],*c['external_inputs']]
    if c['producer'] is not None:resources.append(c['producer'])
    def verify():
        for d in resources:assert hashlib.sha256(Path(d['file']).read_bytes()).hexdigest()==d['expected_sha256'],d['file']
    verify()
    for name in ('character_state.py','inspection.py'):
        ds=[d for d in c['implementation'] if Path(d['file']).name==name];assert len(ds)==1
        sys.path.insert(0,str(Path(ds[0]['file']).parent))
    sys.path.insert(0,c['addon_root'])
    import character_state as C
    from humanoid_rigging_probe import front_hair_isolation as F,subdivision_diagnostic as S
    # Validate the modules Python actually loaded, not merely files on disk.
    loaded_implementation=[]
    for name,module in tuple(sys.modules.items()):
        if name in ('character_state','inspection','protocol') or name=='humanoid_rigging_probe' or name.startswith('humanoid_rigging_probe.'):
            actual=Path(module.__file__).resolve()
            matches=[d for d in c['implementation'] if Path(d['file']).resolve()==actual]
            assert len(matches)==1,('Loaded module outside pinned closure',name,str(actual))
            assert hashlib.sha256(actual.read_bytes()).hexdigest()==matches[0]['expected_sha256']
            loaded_implementation.append({'module':name,**matches[0]})
    bpy.ops.wm.open_mainfile(filepath=c['input']['file'],use_scripts=False,load_ui=False)
    scene=bpy.data.scenes['Scene'];layer=scene.view_layers['ViewLayer']
    bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
    assert scene.frame_current==0 and scene.frame_subframe==0 and bpy.context.mode=='OBJECT'
    S._handler_policy(bpy);S._cache_policy(scene,list(range(1,49)))
    rig=bpy.data.objects['千早爱音-素体骨架'];mesh=bpy.data.objects['千早爱音- 赋权前发网格']
    def resource_paths():
        # Author canonical intentionally excludes image paths. Preserve their
        # resolved targets independently, including packed and weak references.
        rows=[]
        for collection in ('images','libraries','sounds','fonts','movieclips','cache_files','volumes'):
            for item in getattr(bpy.data,collection):
                raw=item.filepath
                library=None if collection=='libraries' else item.library
                resolved=C.inspection.path_value(raw,library) if raw and raw!='<builtin>' else raw
                rows.append({'collection':collection,'name':item.name,'raw_path':raw,'resolved_path':resolved,
                             'packed':bool(getattr(item,'packed_file',None)) or bool(getattr(item,'packed_files',()))})
        for item in C.inspection.all_ids():
            weak=getattr(item,'library_weak_reference',None)
            if weak is not None:
                raw=weak.filepath
                rows.append({'collection':'weak_library_reference','name':item.bl_rna.identifier+':'+item.name,
                             'raw_path':raw,'resolved_path':C.inspection.path_value(raw,item.library) if raw else raw,
                             'id_name':weak.id_name})
        return sorted(rows,key=lambda r:(r['collection'],r['name']))
    def resolved(rows):
        return [{k:v for k,v in r.items() if k!='raw_path'} for r in rows]
    def observe():
        graph=bpy.context.evaluated_depsgraph_get();e=mesh.evaluated_get(graph)
        assert e.is_evaluated and e.original==mesh
        co=array('f',[0.0])*(3*len(e.data.vertices));e.data.vertices.foreach_get('co',co)
        return {'vertices':len(e.data.vertices),'coordinates':list(co),
                'topology':C.digest([list(p.vertices) for p in e.data.polygons]),
                'matrix_world':[list(r) for r in e.matrix_world]}
    def compare(a,b):
        import math
        assert a['vertices']==b['vertices'] and a['topology']==b['topology'] and a['matrix_world']==b['matrix_world']
        assert all(math.isfinite(v) for v in a['coordinates']+b['coordinates'])
        error=max(abs(x-y) for x,y in zip(a['coordinates'],b['coordinates']))
        assert error<=1e-6,error
        return error
    report={'schema':'hrig.front-hair-candidate.v1','phase':c['phase'],'status':'running','source':c['source'],
            'loaded_implementation':loaded_implementation,
            'source_saved':False,'scope':'Fixed baseline5 experimental front/side hair branch; original 1733 bones, body and proxies retained. No FPS claim.'}
    clone=None;data=None
    try:
        if c['phase']=='producer':
            assert c['producer'] is None and c['input']==c['source'] and C.digest(C.capture())==AUTHOR
            report['source_resource_paths']=resource_paths()
            base=observe()
            clone,data,report['mapping']=F.create(rig,mesh,scene,layer)
            new=observe();report['neutral_max_component_error']=compare(base,new)
            output=Path(directory,'output-candidate.blend')
            assert not output.exists() and output.resolve()!=Path(c['source']['file']).resolve()
            bpy.ops.wm.save_as_mainfile(filepath=str(output),check_existing=True,relative_remap=True)
            report['candidate_resource_paths']=resource_paths()
            assert resolved(report['source_resource_paths'])==resolved(report['candidate_resource_paths']),'Saved resource targets changed'
            report['resource_targets_preserved']=True
            report['candidate_author_sha256']=C.digest(C.capture())
            report['candidate_geometry']=observe()
            report['candidate_sha256']=hashlib.sha256(output.read_bytes()).hexdigest()
        else:
            producer=json.loads(Path(c['producer']['file']).read_text(encoding='utf8'))
            assert producer['status']=='pass' and producer['phase']=='producer' and producer['source']==c['source']
            assert c['input']['expected_sha256']==producer['candidate_sha256']
            report['candidate_resource_paths']=resource_paths()
            assert resolved(report['candidate_resource_paths'])==resolved(producer['source_resource_paths']),'Cold resource targets changed'
            report['resource_targets_preserved']=True
            assert C.digest(C.capture())==producer['candidate_author_sha256'],'saved author state mismatch'
            clone=bpy.data.objects['HRIG_front_hair'];data=clone.data
            assert mesh.modifiers[1].object==clone and set(data.bones.keys())==F.bone_names()
            assert clone.animation_data.action is None and len(clone.animation_data.drivers)==14
            report['candidate_author_sha256']=producer['candidate_author_sha256']
            report['neutral_max_component_error']=compare(producer['candidate_geometry'],observe())
            report['candidate_sha256']=c['input']['expected_sha256']
        report['status']='pass'
    except Exception as e:
        report.update(status='fail',error={'type':type(e).__name__,'message':str(e),'traceback':traceback.format_exc()})
    finally:
        # In-memory inverse, including cold: proves all original stored IDs after
        # removing the owned branch and restoring exactly one pointer.
        mesh.modifiers[1].object=rig
        if clone is not None:bpy.data.objects.remove(clone,do_unlink=True)
        if data is not None:bpy.data.armatures.remove(data)
        layer.update()
        report['original_author_restored_sha256']=C.digest(C.capture())
        report['original_author_exact']=report['original_author_restored_sha256']==AUTHOR
        if not report['original_author_exact']:report['status']='fail'
        verify();report['resources_unchanged']=True
        Path(directory,'output-validation.json').write_text(json.dumps(report,ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf8')
    return {'status':report['status'],'source_saved':False}
if __name__=='__main__':
    try:result=run(parameters,job_directory)
    except Exception:
        Path(job_directory,'preflight-error.txt').write_text(traceback.format_exc(),encoding='utf8')
        raise
