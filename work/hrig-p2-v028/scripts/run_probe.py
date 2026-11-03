"""Exact-SHA entry for public extension.run; no installation or source saving."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True

import bpy


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(config, job):
    if tuple(bpy.app.version) != (5, 2, 1):
        raise RuntimeError('This P2 runtime evidence is limited to Blender 5.2.1')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise RuntimeError('Automatic Python execution must remain disabled')
    for entry in config['implementation']:
        if digest(entry['file']) != entry['expected_sha256']:
            raise RuntimeError('Prototype implementation changed before execution')
    sys.path.insert(0, str(Path(config['addon_root']).resolve()))
    from humanoid_rigging_probe import collector
    validation = {'mode': config['mode'], 'claims': 'P2 read-only prototype, not production rigging/FPS acceptance'}
    if config['mode'] == 'fixture':
        fixture_file = Path(__file__).with_name('fixture.py')
        spec = importlib.util.spec_from_file_location('_hrig_independent_fixture', fixture_file)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        rig, native = fixture.create()
        if config.get('save_fixture'):
            # This ID is intentionally unlinked to test file-vs-scene scope.
            # Retain only the generated fixture ID, never arbitrary user data.
            bpy.data.objects['ArchivedRig'].use_fake_user = True
            validation['synthetic_fixture_retention'] = 'ArchivedRig fake_user explicitly set before protection snapshot'
        before = fixture.fingerprint()
        report = collector.collect(bpy.context, rig)
        after = fixture.fingerprint()
        if before != after:
            raise RuntimeError('Collector changed stored fixture content')
        validation.update(native=native, checks=fixture.validate(report), stored_content_unchanged=True,
                          before_fingerprint=before, after_fingerprint=after)
        import humanoid_rigging_probe as addon
        addon.register()
        try:
            bpy.context.window_manager.hrig_p2_armature = rig
            response = bpy.ops.hrig_p2.inspect()
            if response != {'FINISHED'}:
                raise RuntimeError('UI operator failed the fixture')
            from humanoid_rigging_probe import ui
            if ui.SESSION_REPORT['drivers']['summary'] != report['drivers']['summary']:
                raise RuntimeError('UI and CLI core disagree')
            validation['ui_core_same_summary'] = True
            from humanoid_rigging_probe import dependencies
            expected_dependencies = dependencies.collect(bpy.context, rig)
            if bpy.ops.hrig_p2.dependencies() != {'FINISHED'}:
                raise RuntimeError('Dependency UI operator did not finish')
            if ui.SESSION_DEPENDENCIES != expected_dependencies:
                raise RuntimeError('Dependency UI and CLI core disagree')
            validation['dependency_ui_core_identical'] = True
        finally:
            addon.unregister()
        if hasattr(bpy.types.WindowManager, 'hrig_p2_armature') or fixture.fingerprint() != before:
            raise RuntimeError('UI unregister or stored-content protection failed')
        validation['ui_unregister_clean'] = True
    elif config['mode'] == 'inspect':
        source = config['input']
        if digest(source['file']) != source['expected_sha256']:
            raise RuntimeError('Source SHA changed before open')
        bpy.ops.wm.open_mainfile(filepath=source['file'], load_ui=False, use_scripts=False)
        rig = bpy.data.objects.get(config['armature'])
        if rig is None or rig.type != 'ARMATURE':
            raise RuntimeError('Explicit Armature object not found')
        report = collector.collect(bpy.context, rig)
        if report['status'] != 'ok':
            raise RuntimeError('Read-only inspection refused: ' + report['status'])
        validation.update(source=source, source_unchanged=digest(source['file']) == source['expected_sha256'],
                          collector_evaluation=False, open_mainfile_native_evaluation_may_occur=True,
                          dependency_closure='not_certified; no render/save/edit performed')
        if not validation['source_unchanged']:
            raise RuntimeError('Source changed during inspection')
    else:
        raise RuntimeError('Unsupported P2 probe mode')
    for entry in config['implementation']:
        if digest(entry['file']) != entry['expected_sha256']:
            raise RuntimeError('Prototype implementation changed during execution')
    job = Path(job)
    (job / 'output-inspection.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    (job / 'output-validation.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    if config.get('save_fixture'):
        if config['mode'] != 'fixture':
            raise RuntimeError('Only the synthetic fixture may be saved')
        bpy.ops.wm.save_as_mainfile(filepath=str(job / 'output-fixture.blend'), copy=True, check_existing=False)
    return {'status': report['status'], 'validation': 'pass', 'mode': config['mode'],
            'observed': report['limits']['observed'], 'detail_accounting': report['limits']['detail_accounting']}


if __name__ == '__main__':
    try:
        result = run(parameters, job_directory)
    except Exception:
        import traceback
        (Path(job_directory) / 'output-debug.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
