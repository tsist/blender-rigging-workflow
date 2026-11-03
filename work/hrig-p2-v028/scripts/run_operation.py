"""Saved-disk current-pose operations via public extension.run, then cold read.

The first editable adapter deliberately accepts closed control-only scenes or
one owned limb. Complete character preservation and animation bake are pending.
"""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import bpy


def sha(file):
    return hashlib.sha256(Path(file).read_bytes()).hexdigest()


def descriptor(file):
    return {'file': str(Path(file).resolve(strict=True)), 'expected_sha256': sha(file)}


def dump(file, data):
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    with Path(file).open('x', encoding='utf-8') as stream:
        stream.write(text)


def checked(item):
    if set(item) != {'file', 'expected_sha256'} or not Path(item['file']).is_absolute():
        raise ValueError('Expected exact absolute file/SHA descriptor')
    if sha(item['file']) != item['expected_sha256']:
        raise ValueError('Descriptor SHA mismatch: ' + str(item['file']))
    return Path(item['file']).resolve(strict=True)


def read(item):
    return json.loads(checked(item).read_text(encoding='utf-8'),
        parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def operation_schema(op):
    action = op.get('action')
    common = {'schema', 'action'}
    if op.get('schema') != 'hrig.operation.v1':
        raise ValueError('Expected hrig.operation.v1')
    if action == 'create_limb':
        required = common | {'spec'}
        if not isinstance(op['spec'], dict) or not {'module_id'} <= op['spec'].keys() <= {
                'module_id', 'name', 'side', 'rest_roll', 'object_uniform_scale'}:
            raise ValueError('Invalid owned limb spec')
    elif action in ('match_world', 'switch_space'):
        required = common | {'context', 'target', 'expected_current_world'}
        required |= {'target_world'} if action == 'match_world' else {'property'}
        if set(op['target']) != {'rig', 'bone'}:
            raise ValueError('Explicit rig/bone target required')
        if action == 'switch_space' and set(op['property']) != {'bone', 'key', 'expected', 'value'}:
            raise ValueError('Explicit selector expected/value required')
    elif action in ('snap_ik_to_fk', 'snap_fk_to_ik'):
        required = common | {'context', 'manifest', 'expected_selector'}
        source = 1 if action == 'snap_ik_to_fk' else 0
        if type(op['expected_selector']) not in (float, int) or op['expected_selector'] != source:
            raise ValueError('Snap source selector must match its explicit direction')
    else:
        raise ValueError('Unsupported operation')
    if set(op) != required:
        raise ValueError('Missing/unknown operation fields')
    if 'context' in op:
        ctx = op['context']
        if set(ctx) != {'scene', 'view_layer', 'frame', 'subframe'} or type(ctx['frame']) is not int:
            raise ValueError('Explicit exact context required')
        if type(ctx['subframe']) not in (int, float) or not math.isfinite(ctx['subframe']) or not 0 <= ctx['subframe'] < 1:
            raise ValueError('Invalid subframe')
    return action


def context_check(op):
    ctx, scene = op['context'], bpy.context.scene
    if (ctx['scene'] != scene.name or ctx['view_layer'] != bpy.context.view_layer.name
            or ctx['frame'] != scene.frame_current or abs(ctx['subframe']-scene.frame_subframe) > 1e-7):
        raise ValueError('SAVED_DISK_V1 context baseline conflict')


def verify_output(state, operation, before, after, manifest):
    action = operation['action']
    if action == 'create_limb':
        rig = after['evaluated'][manifest['rig']]['bones']
        if not all(state.equal(rig[f], rig[d], 2e-4) for f, d in zip(manifest['bones']['FK'], manifest['bones']['DEF'])):
            raise ValueError('Native initial FK/DEF output differs')
    else:
        if not state.equal(state.protected(before, operation, manifest), state.protected(after, operation, manifest)):
            raise ValueError('Protected stored state changed')
        if action in ('match_world', 'switch_space'):
            target = operation['target']
            actual = after['evaluated'][target['rig']]['bones'][target['bone']]
            desired = operation.get('target_world', operation['expected_current_world'])
            if not state.equal(actual, desired, 2e-5):
                raise ValueError('Independent world oracle failed')
            if action == 'switch_space':
                prop = operation['property']
                value = bpy.data.objects[target['rig']].pose.bones[prop['bone']][prop['key']]
                if value != prop['value']:
                    raise ValueError('Independent selector oracle failed')
        else:
            first, second = before['evaluated'][manifest['rig']]['bones'], after['evaluated'][manifest['rig']]['bones']
            if not all(state.equal(first[n], second[n], 2e-4) for n in manifest['bones']['DEF']):
                raise ValueError('Independent DEF output oracle failed')
            if not state.equal(before['evaluated'][manifest['mesh']]['vertices'], after['evaluated'][manifest['mesh']]['vertices'], 2e-4):
                raise ValueError('Independent native mesh oracle failed')


def run(config, job):
    if tuple(bpy.app.version) != (5, 2, 1) or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise RuntimeError('Requires Blender 5.2.1 with embedded autoexec disabled')
    root = Path(__file__).resolve().parents[1]
    mode = config.get('mode')
    expected_keys = {'mode', 'operation', 'implementation', 'addon_root'}
    if mode == 'cold':
        expected_keys |= {'input', 'producer_report'}
    elif mode == 'operate':
        if 'input' in config:
            expected_keys.add('input')
    else:
        raise ValueError('Unsupported worker mode')
    if set(config) != expected_keys or Path(config['addon_root']).resolve() != root/'addon':
        raise ValueError('Unknown worker config fields or wrong role root')
    actual = [checked(x) for x in config['implementation']]
    required = set((root/'addon/humanoid_rigging_probe').glob('*.py')) | {root/'scripts/operation_state.py'}
    if len(actual) != len(set(actual)) or set(actual) != required:
        raise ValueError('Exact implementation role closure required')
    operation = read(config['operation'])
    action = operation_schema(operation)
    manifest = read(operation['manifest']) if 'manifest' in operation else None
    sys.path.insert(0, str(root/'addon'))
    from humanoid_rigging_probe import control_space, limb
    def owned_check(value):
        if value is not None:
            inspection = limb.inspect_module(bpy.context, value)
            if not inspection['ok']:
                raise ValueError('Owned module contract refused: ' + str(inspection['error']))
    spec = importlib.util.spec_from_file_location('_hrig_operation_state', root/'scripts/operation_state.py')
    state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(state)
    entry_sha = sha(__file__)
    report = {'schema': 'hrig.operation-report.v1', 'mode': mode, 'operation': operation,
        'implementation': config['implementation'], 'entry_sha': entry_sha,
        'GUI': 'deferred_by_user', 'FPS': 'not_measured', 'blender': bpy.app.version_string,
        'build': bpy.app.build_hash.decode(), 'preservation_scope': 'closed local control scenes or one owned limb; declared stored RNA and native evaluated output'}
    if mode == 'cold':
        producer = read(config['producer_report'])
        if (producer.get('status') != 'candidate_waiting_independent_cold'
                or producer.get('operation') != operation or producer.get('implementation') != config['implementation']
                or producer.get('entry_sha') != entry_sha or producer.get('candidate') != config['input']):
            raise ValueError('Unpaired producer identity or candidate')
        bpy.ops.wm.open_mainfile(filepath=str(checked(config['input'])), load_ui=False, use_scripts=False)
        manifest = producer.get('module_manifest', manifest)
        state.gate(manifest)
        owned_check(manifest)
        after = state.snapshot()
        if not state.equal(after['stored'], producer['after_snapshot']['stored']) or not state.equal(after['evaluated'], producer['after_snapshot']['evaluated'], 2e-5):
            raise ValueError('Independent cold state/output oracle failed')
        report.update(status='pass', after_snapshot=after, source_unchanged=checked(config['input']) is not None,
                      producer_report=config['producer_report'], module_manifest=manifest)
    else:
        before = None
        if action == 'create_limb':
            if 'input' in config:
                raise ValueError('Creation never loads or replaces a user source')
            bpy.ops.wm.read_factory_settings(use_empty=True)
            result = limb.create_module(bpy.context, operation['spec'])
            if result['ok']:
                manifest = result['manifest']
        else:
            if 'input' not in config:
                raise ValueError('Explicit saved source/SHA required')
            bpy.ops.wm.open_mainfile(filepath=str(checked(config['input'])), load_ui=False, use_scripts=False)
            context_check(operation)
            if action in ('match_world', 'switch_space'):
                rig = bpy.data.objects.get(operation['target']['rig'])
                stored_manifest = rig.get('hrig_manifest') if rig else None
                if stored_manifest:
                    manifest = json.loads(stored_manifest)
                    if manifest.get('schema') != limb.SCHEMA or manifest.get('rig') != rig.name:
                        raise ValueError('Malformed owned module identity')
            state.gate(manifest)
            owned_check(manifest)
            before = state.snapshot()
            if action in ('match_world', 'switch_space'):
                target = operation['target']
                rig = bpy.data.objects.get(target['rig'])
                if action == 'match_world':
                    result = control_space.match_world(bpy.context, rig, target['bone'], operation['target_world'], operation['expected_current_world'])
                else:
                    prop = operation['property']
                    if rig is None or rig.pose.bones[prop['bone']].get(prop['key']) != prop['expected']:
                        raise ValueError('Selector baseline conflict')
                    result = control_space.switch_space(bpy.context, rig, target['bone'], prop['bone'], prop['key'], prop['value'], operation['expected_current_world'])
            else:
                result = getattr(limb, action)(bpy.context, manifest)
        report['result'] = result
        if not result['ok']:
            report.update(status='rejected', before_snapshot=before)
            dump(job/'output-operation.json', report)
            raise RuntimeError('Operation refused: ' + str(result['error']))
        try:
            state.gate(manifest)
            owned_check(manifest)
            after = state.snapshot()
            verify_output(state, operation, before, after, manifest)
        except Exception:
            after = state.snapshot()
            report.update(status='verification_failed', before_snapshot=before, after_snapshot=after,
                          module_manifest=manifest)
            dump(job/'output-operation.json', report)
            raise
        candidate = job/'output-candidate.blend'
        if candidate.exists():
            raise ValueError('Candidate exists; never overwrite')
        bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=True, check_existing=False)
        if 'input' in config:
            checked(config['input'])
        report.update(status='candidate_waiting_independent_cold', before_snapshot=before, after_snapshot=after,
            input=config.get('input'), candidate=descriptor(candidate), module_manifest=manifest,
            expected={'native_output': 'pass', 'stored_protection': 'pass' if before else 'new_scene'})
    for item in config['implementation']:
        checked(item)
    checked(config['operation'])
    if manifest is not None and 'manifest' in operation:
        checked(operation['manifest'])
    if sha(__file__) != entry_sha:
        raise ValueError('Entry changed during operation')
    dump(job/'output-operation.json', report)
    return {'status': report['status'], 'action': action, 'GUI': 'deferred', 'FPS': 'not_measured'}


if __name__ == '__main__':
    try:
        result = run(parameters, Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
