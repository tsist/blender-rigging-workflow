"""Pinned public-extension work unit for native control matching evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import bpy


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def load_fixture(path):
    spec = importlib.util.spec_from_file_location('_hrig_space_fixture',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(config, job):
    if tuple(bpy.app.version) != (5,2,1) or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise RuntimeError('Requires verified Blender5.2.1 with embedded autoexec disabled')
    root = Path(__file__).resolve().parents[1]
    expected_keys = {'mode','implementation','addon_root','fixture_script'}
    expected_keys |= {'case'} if config.get('mode') == 'fixture' else {'input','producer_report','fixture_manifest'}
    if config.get('mode') not in ('fixture','cold') or set(config) != expected_keys:
        raise ValueError('Unknown mode or worker config fields')
    for item in config['implementation']:
        if sha(item['file']) != item['expected_sha256']:
            raise RuntimeError('Implementation drift')
    addon_root = Path(config['addon_root']).resolve(strict=True)
    fixture_path = Path(config['fixture_script']).resolve(strict=True)
    declared = {Path(item['file']).resolve(strict=True) for item in config['implementation']}
    required = set((addon_root/'humanoid_rigging_probe').glob('*.py')) | {fixture_path}
    if (declared != required or len(declared) != len(config['implementation'])
            or addon_root != root/'addon' or fixture_path != root/'scripts/space_fixture.py'):
        raise RuntimeError('Implementation role paths or complete file closure differ from pinned inputs')
    sys.path.insert(0,config['addon_root'])
    from humanoid_rigging_probe import control_space
    fixture = load_fixture(config['fixture_script'])
    job = Path(job)
    report = {'mode':config['mode'],'build':bpy.app.build_hash.decode(),
              'implementation':config['implementation'],'entry_sha':sha(__file__),
              'blender':bpy.app.version_string,'GUI':'deferred by user','FPS':'not_measured',
              'scope':'native control-space prototype; not complete humanoid binding'}
    if config['mode'] == 'fixture':
        bpy.ops.wm.read_factory_settings(use_empty=True)
        manifest = fixture.create_case(config['case'])
        before = fixture.capture(manifest)
        request = manifest['request']
        rig = bpy.data.objects[request['object_name']]
        if request['action'] == 'match_world':
            result = control_space.match_world(bpy.context,rig,request['bone_name'],request['target_world'],
                expected_current_world=request['expected_current_world'],tolerance=manifest['tolerance'])
        elif request['action'] == 'switch_space':
            result = control_space.switch_space(bpy.context,rig,request['bone_name'],
                request['property']['bone_name'],request['property']['key'],request['new_value'],
                expected_current_world=request['expected_current_world'],tolerance=manifest['tolerance'])
        else:
            raise RuntimeError('Unknown fixture action')
        after = fixture.capture(manifest)
        validation = fixture.validate(manifest,before,after,result)
        report.update(manifest=manifest,before=before,after=after,result=result,validation=validation)
        dump(job/'output-control.json',report)
        if not validation['ok']:
            raise RuntimeError('Fixture failed; exact report retained in output-control.json')
        dump(job/'output-manifest.json',manifest)
        bpy.ops.wm.save_as_mainfile(filepath=str(job/'output-fixture.blend'),copy=True,check_existing=False)
        report['candidate'] = {'file':str(job/'output-fixture.blend'),'expected_sha256':sha(job/'output-fixture.blend')}
        report['status'] = 'candidate_waiting_independent_cold'
    elif config['mode'] == 'cold':
        for key in ('input','producer_report','fixture_manifest'):
            descriptor = config[key]
            if sha(descriptor['file']) != descriptor['expected_sha256']:
                raise RuntimeError('Cold input SHA changed')
        bpy.ops.wm.open_mainfile(filepath=config['input']['file'],load_ui=False,use_scripts=False)
        manifest = json.loads(Path(config['fixture_manifest']['file']).read_text(encoding='utf-8'))
        producer = json.loads(Path(config['producer_report']['file']).read_text(encoding='utf-8'))
        if (manifest != producer['manifest'] or not producer['validation']['ok']
                or producer.get('implementation') != config['implementation']
                or producer.get('entry_sha') != sha(__file__)
                or producer.get('candidate') != config['input']
                or producer.get('status') != 'candidate_waiting_independent_cold'):
            raise RuntimeError('Cold manifest is not the validated producer manifest')
        after = fixture.capture(manifest)
        validation = fixture.validate(manifest,producer['before'],after,producer['result'])
        same_stored = after['storage'] == producer['after']['storage']
        report.update(manifest=manifest,after=after,validation=validation,
            producer_stored_state_matches=same_stored,
            source_unchanged=sha(config['input']['file'])==config['input']['expected_sha256'])
        if not validation['ok'] or not same_stored or not report['source_unchanged']:
            dump(job/'output-control.json',report)
            raise RuntimeError('Independent cold validation failed')
        report['status'] = 'pass'
    else:
        raise ValueError('Unknown control mode')
    for item in config['implementation']:
        if sha(item['file']) != item['expected_sha256']:
            raise RuntimeError('Implementation changed during operation')
    dump(job/'output-control.json',report)
    return {'status':report['status'],'mode':config['mode'],'case':manifest['case_id'],
            'operation_ok':report.get('result',{}).get('ok'),'validation':validation['checks'],
            'world_max_abs_error':validation['world_max_abs_error'],
            'producer_stored_state_matches':report.get('producer_stored_state_matches')}


if __name__ == '__main__':
    try:
        result = run(parameters,job_directory)
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
