"""Pinned worker for read-only finite-frame observation and owned fixtures."""
import hashlib
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
import bpy

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def verify(item):
    if type(item) is not dict or set(item) != {'file', 'expected_sha256'}:
        raise ValueError('Invalid file descriptor')
    p=Path(item['file'])
    if not p.is_absolute() or not p.is_file() or digest(p)!=item['expected_sha256']:
        raise ValueError('Resource identity changed: '+str(p))
    return p

def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

def run(config, output_dir):
    started=time.perf_counter_ns()
    if set(config)!={'mode','addon_root','implementation','input','plan','external_inputs','case'}:
        raise ValueError('Unknown worker fields')
    if tuple(bpy.app.version)!=(5,2,1) or bpy.app.build_hash.decode()!='9e2066aef7ef':
        raise RuntimeError('Unreviewed Blender version/build')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise RuntimeError('Embedded scripts must remain disabled')
    for item in config['implementation']+config['external_inputs']:
        verify(item)
    root=Path(config['addon_root']).resolve(strict=True)
    sys.path.insert(0,str(root))
    from humanoid_rigging_probe import diagnostic_contract, diagnostics
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    import character_state
    import diagnostic_fixture
    directory=Path(output_dir)
    if config['mode']=='fixture':
        if config['case'] not in ('alpha','beta') or config['input'] is not None or config['plan'] is not None or config['external_inputs']:
            raise ValueError('Invalid owned fixture configuration')
        bpy.ops.wm.read_factory_settings(use_empty=True)
        manifest=diagnostic_fixture.create(config['case'])
        source=directory/'output-fixture.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(source),copy=True,check_existing=False)
        dump(directory/'output-fixture-manifest.json',manifest)
        report={'schema':'hrig.diagnostic-fixture.v1','status':'pass','case':config['case'],
            'source_unchanged':True,'fixture_file':{'file':str(source),'expected_sha256':digest(source)},
            'fixture_creation_may_evaluate_frame':True,'not_cold_first_evaluation_claim':True,
            'manifest':manifest,'FPS_claim':False}
    elif config['mode']=='observe':
        if config['case'] is not None:
            raise ValueError('Observe does not accept a fixture case')
        source=verify(config['input'])
        plan_path=verify(config['plan'])
        if plan_path.stat().st_size>1048576:
            raise ValueError('Plan exceeds 1 MiB')
        plan=diagnostic_contract.parse_plan(plan_path.read_bytes())
        bpy.ops.wm.open_mainfile(filepath=str(source),load_ui=False,use_scripts=False)
        if not bpy.context.window:
            raise RuntimeError('Owned worker has no context window')
        scene=bpy.data.scenes.get(plan['context']['scene'])
        layer=scene.view_layers.get(plan['context']['view_layer']) if scene else None
        if scene is None or layer is None:
            raise ValueError('Scene/ViewLayer absent')
        bpy.context.window.scene=scene
        bpy.context.window.view_layer=layer
        # Every active Scene Cloth is part of the same sequential-memory protocol.
        for obj in scene.objects:
            for modifier in obj.modifiers:
                if modifier.type!='CLOTH':
                    continue
                cache=modifier.point_cache
                required=('is_baked','is_baking','use_disk_cache','use_external')
                if any(not hasattr(cache,name) for name in required):
                    raise ValueError('Required PointCache RNA fields unavailable; cache domain not admitted')
                if any(bool(getattr(cache,name)) for name in required):
                    raise ValueError('Baked/baking/disk/external Cloth is outside this fresh-memory observer')
                if cache.frame_start!=plan['frames'][0] or plan['frames'][-1]>cache.frame_end or cache.frame_step!=1:
                    raise ValueError('Cloth requires cache-start consecutive frames, step1, within authored range')
        before=character_state.capture()
        capture_before_end=time.perf_counter_ns()
        report=diagnostics.observe(bpy.context,plan,directory)
        observation_end=time.perf_counter_ns()
        after=character_state.capture()
        report['storage_protection']={'schema':'hrig.character-storage.v1',
            'before_sha256':character_state.digest(before),'after_sha256':character_state.digest(after),
            'exact_equal':before==after,'scope':'declared bounded character observer; not arbitrary complete RNA/UI serialization'}
        report['source']=config['input']
        report['source_unchanged']=digest(source)==config['input']['expected_sha256']
        report['external_inputs']=config['external_inputs']
        report['inherited_dependency_completeness']='explicit descriptors only; dynamic/weak references not certified'
        report['outer_phases_ns']={'load_validate_storage_before':capture_before_end-started,
            'observation_including_frame_restore':observation_end-capture_before_end,
            'storage_after':time.perf_counter_ns()-observation_end}
        if not report['source_unchanged'] or before!=after:
            report['status']='fail'
    else:
        raise ValueError('Unknown mode')
    for item in config['implementation']+config['external_inputs']:
        verify(item)
    if config['mode']=='observe':
        verify(config['input']);verify(config['plan'])
    report['implementation_unchanged']=True
    report['worker_total_before_encode_ns']=time.perf_counter_ns()-started
    encode_started=time.perf_counter_ns()
    # This measures an explicit encoding pass; final reporting overhead remains outside frame measurements.
    encoded=json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
    report['report_encoding_probe_ns']=time.perf_counter_ns()-encode_started
    report['report_encoding_probe_bytes']=len(encoded.encode('utf-8'))
    dump(directory/'output-diagnostic.json',report)
    return {'status':report['status'],'source_unchanged':report['source_unchanged'],
        'implementation_unchanged':True,'FPS_claim':False,
        'scope':'owned fixture or bounded read-only observation; no editing success bundle'}

if __name__=='__main__':
    try:
        result=run(parameters,job_directory)
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
