"""Pinned explicit rotation/local-location producer and independent cold worker."""
import importlib.util
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import bpy

ROOT = Path(__file__).resolve().parents[1]
OBSERVER = ROOT.parents[1] / 'tools/blenderctl'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def operation(value, contract):
    if (type(value) is not dict or set(value) != {'schema', 'context', 'rig', 'request',
            'external_inputs', 'inherited_missing_weak_references'}
            or value['schema'] not in ('hrig.pose-rotations.operation.v1', 'hrig.pose-locations.operation.v1')):
        raise ValueError('Exact pose operation schema/fields required')
    if type(value['rig']) is not str or not value['rig']:
        raise ValueError('Explicit rig required')
    if value['schema'] == 'hrig.pose-rotations.operation.v1':
        contract.canonical_request(value['request'])
    else:
        contract.canonical_location_request(value['request'])
    return value


def selection(value, contract):
    if (type(value) is not dict or set(value) != {'schema', 'context', 'rig', 'bones'}
            or value['schema'] not in ('hrig.pose-rotations.selection.v1', 'hrig.pose-locations.selection.v1')):
        raise ValueError('Exact pose selection schema/fields required')
    if type(value['rig']) is not str or not value['rig']:
        raise ValueError('Explicit rig required')
    contract.canonical_bones(value['bones'])
    return value


def request_channel(value):
    if type(value) is not dict:
        raise ValueError('Explicit pose request schema required')
    if value.get('schema') == 'hrig.pose-rotations.patch.v1':
        return 'rotation'
    if value.get('schema') == 'hrig.pose-locations.patch.v1':
        return 'location'
    raise ValueError('Unknown pose request schema')


def selection_channel(value):
    if type(value) is not dict:
        raise ValueError('Explicit pose selection schema required')
    if value.get('schema') == 'hrig.pose-rotations.selection.v1':
        return 'rotation'
    if value.get('schema') == 'hrig.pose-locations.selection.v1':
        return 'location'
    raise ValueError('Unknown pose selection schema')


def fixture_channel(manifest, request):
    channel = request_channel(request)
    if type(manifest) is not dict or (('channel' in manifest) and manifest['channel'] != 'location'):
        raise ValueError('Unknown pose fixture channel')
    expected_schema = ('hrig.pose-locations.fixture.v1' if channel == 'location'
                       else 'hrig.pose-rotations.fixture.v1')
    if manifest.get('schema') != expected_schema or ('channel' in manifest) != (channel == 'location'):
        raise ValueError('Fixture channel and request schema differ')
    return channel


def location_channels(rig):
    return {bone.name: {'location': list(bone.location), 'rotation_mode': bone.rotation_mode,
                        'rotation_quaternion': list(bone.rotation_quaternion),
                        'rotation_euler': list(bone.rotation_euler),
                        'rotation_axis_angle': list(bone.rotation_axis_angle), 'scale': list(bone.scale)}
            for bone in rig.pose.bones}


def check_location_channels(before, after, request, contract):
    """Independent full raw-channel oracle; does not use the core protection mask."""
    canonical = contract.canonical_location_request(request)
    if type(before) is not dict or type(after) is not dict or set(before) != set(after):
        raise ValueError('Location raw bone coverage differs')
    rows = {row['bone']: row for row in canonical['locations']}
    if not set(rows) <= set(before):
        raise ValueError('Location raw selected bone missing')
    keys = {'location', 'rotation_mode', 'rotation_quaternion', 'rotation_euler', 'rotation_axis_angle', 'scale'}
    for name, original in before.items():
        actual = after[name]
        if type(original) is not dict or type(actual) is not dict or set(original) != keys or set(actual) != keys:
            raise ValueError('Location raw channel coverage differs: ' + name)
        expected = dict(original)
        if name in rows:
            if original['location'] != rows[name]['before']:
                raise ValueError('Location raw before differs: ' + name)
            expected['location'] = rows[name]['after']
        if actual != expected:
            raise ValueError('Location raw channel drift: ' + name)
    return {'ok': True, 'bone_count': len(before), 'selected_bones': sorted(rows),
            'scope': 'all raw location/rotation modes/all rotation channels/scale exact except selected canonical location after'}


def dimensions():
    return {o.name: list(o.dimensions) for o in bpy.data.objects}


def run(config, job):
    if tuple(bpy.app.version) != (5, 2, 1) or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise ValueError('Requires Blender5.2.1 with embedded autoexec disabled')
    if Path(config['addon_root']).resolve() != ROOT / 'addon' or Path(config['observer_root']).resolve() != OBSERVER:
        raise ValueError('Pose role root mismatch')
    sys.path.insert(0, str(OBSERVER))
    support = load('_hrig_pose_support', ROOT / 'scripts/run_weights.py')
    evaluator = load('_hrig_pose_evaluator', ROOT / 'scripts/run_styles.py')
    required = set((ROOT / 'addon/humanoid_rigging_probe').glob('*.py')) | {
        ROOT / 'scripts/character_state.py', ROOT / 'scripts/pose_state.py', ROOT / 'scripts/pose_fixture.py',
        ROOT / 'scripts/run_weights.py', ROOT / 'scripts/run_styles.py', OBSERVER / 'inspection.py', OBSERVER / 'protocol.py'}
    files = [support.checked(x) for x in config['implementation']]
    if len(files) != len(set(files)) or set(files) != required:
        raise ValueError('Exact pose implementation closure required')
    sys.path.insert(0, str(ROOT / 'addon'))
    from humanoid_rigging_probe import pose_rotations as core, pose_contract as contract, persistence
    import inspection
    if Path(inspection.__file__).resolve() != OBSERVER / 'inspection.py':
        raise ValueError('Pinned observer import differs')
    state = load('_hrig_pose_character_state', ROOT / 'scripts/character_state.py')
    mask = load('_hrig_pose_mask', ROOT / 'scripts/pose_state.py')
    fixture = load('_hrig_pose_fixture', ROOT / 'scripts/pose_fixture.py')
    mode = config['mode']
    extra = {'inspect': {'input', 'selection'}, 'apply': {'input', 'operation'},
             'fixture': {'case'}, 'cold': {'input', 'producer_report'}}
    expected = {'mode', 'implementation', 'addon_root', 'observer_root'} | extra.get(mode, set())
    if mode == 'cold' and 'operation' in config:
        expected.add('operation')
    if mode not in extra or set(config) != expected:
        raise ValueError('Unknown pose mode/fields')
    report = {'schema': 'hrig.pose.report.v1', 'mode': mode, 'blender': bpy.app.version_string,
              'build': bpy.app.build_hash.decode(), 'implementation': config['implementation'],
              'entry_sha': support.sha(__file__), 'GUI': 'deferred_by_user', 'FPS': 'not_measured',
              'complete_improved_Aon': False}
    op = None
    manifest = None
    channel = None
    if mode == 'cold':
        producer = support.read(config['producer_report'])
        if (producer.get('schema') != report['schema'] or producer.get('status') != 'candidate_waiting_independent_cold'
                or producer['candidate'] != config['input'] or producer['implementation'] != config['implementation']
                or producer['entry_sha'] != report['entry_sha'] or producer.get('operation_descriptor') != config.get('operation')):
            raise ValueError('Cold producer/candidate/implementation conflict')
        if 'operation' in config:
            op = operation(support.read(config['operation']), contract)
            if op != producer['operation']:
                raise ValueError('Cold operation differs')
            if op['request'] != producer['request']:
                raise ValueError('Cold producer request differs from operation')
        channel = request_channel(producer['request'])
        manifest = producer.get('manifest')
        if manifest:
            if fixture_channel(manifest, producer['request']) != channel:
                raise ValueError('Cold fixture channel differs')
        if (producer.get('channel') == 'location') != (channel == 'location') or (
                'channel' in producer and producer['channel'] != 'location'):
            raise ValueError('Cold producer channel differs from explicit request schema')
        if channel == 'location':
            report['channel'] = channel
        bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
        support.select_context(evaluator.checked_context(producer['context']), frames=True)
        rig = bpy.data.objects[producer['rig']]
        actual = state.summarize(state.capture())
        if actual != producer['after_snapshot']:
            report.update(status='cold_mismatch', changes=state.diff(producer['after_snapshot'], actual))
            support.dump(job / 'output-pose.json', report)
            raise ValueError('Cold character storage mismatch')
        if channel == 'location':
            actual_channels = location_channels(rig)
            if actual_channels != producer['location_channels_after']:
                raise ValueError('Cold raw location/rotation/scale channels differ')
            if producer['operation_ok'] is True:
                raw_comparison = check_location_channels(producer['location_channels_before'], actual_channels,
                                                         producer['request'], contract)
            elif actual_channels != producer['location_channels_before']:
                raise ValueError('Cold refused location channels differ from before')
            else:
                raw_comparison = {'ok': True, 'scope': 'rejected/rolled-back full raw channels exact'}
            report.update(location_channels_after=actual_channels, location_channel_comparison=raw_comparison)
        actual_evaluation = evaluator.evaluation(rig, support)
        comparison = evaluator.compare_evaluation(producer['evaluation_after'], actual_evaluation)
        if support.paths() != producer['external_paths_after']:
            raise ValueError('Cold external files differ')
        if op:
            report['external_input_validation'] = support.validate_native_inputs(
                op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
        if manifest:
            if manifest.get('inject_rollback'):
                evidence = producer.get('rollback_injection', {})
                if evidence.get('completed_writes') != 1 or evidence.get('triggered') is not True:
                    raise ValueError('Cold rollback lacks completed native write evidence')
            observed = fixture.capture(manifest)
            validation = fixture.validate(manifest, producer['fixture_before'], observed, producer['result'])
            if not validation['ok']:
                raise ValueError('Independent cold pose fixture failed: ' + str(validation))
            report.update(manifest=manifest, fixture_after=observed, fixture_validation=validation)
        report.update(status='pass', source_unchanged=True, operation_ok=producer['operation_ok'],
                      after_snapshot=actual, evaluation=actual_evaluation, evaluation_comparison=comparison,
                      runtime_extra_users_observed=support.runtime_extra_users(),
                      automatic_texspace_observed=support.automatic_texspace(), object_dimensions_observed=dimensions())
    else:
        if mode == 'fixture':
            bpy.ops.wm.read_factory_settings(use_empty=True)
            manifest = fixture.create_case(config['case'])
            rig = bpy.data.objects[manifest['rig']]
            request = fixture.prepare(manifest, core)
            channel = fixture_channel(manifest, request)
            ctx = {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                   'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe}
        else:
            bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
            if tuple(bpy.data.version[:2]) > tuple(bpy.app.version[:2]):
                raise ValueError('Future source version rejected')
            if mode == 'apply':
                op = operation(support.read(config['operation']), contract)
                request, ctx, rig = op['request'], op['context'], bpy.data.objects.get(op['rig'])
                channel = request_channel(request)
                report.update(operation=op, operation_descriptor=config['operation'])
            else:
                picked = selection(support.read(config['selection']), contract)
                ctx, rig = picked['context'], bpy.data.objects.get(picked['rig'])
                channel = selection_channel(picked)
            support.select_context(evaluator.checked_context(ctx), frames=True)
        if rig is None:
            raise ValueError('Explicit rig missing')
        if channel == 'location':
            report.update(channel=channel, location_channels_before=location_channels(rig))
        before = state.capture()
        runtime_before, external_before = support.runtime_extra_users(), support.paths()
        evaluated_before = evaluator.evaluation(rig, support)
        report.update(rig=rig.name, context=ctx, before_snapshot=state.summarize(before),
                      external_paths_before=external_before, evaluation_before=evaluated_before,
                      automatic_texspace_before=support.automatic_texspace(), object_dimensions_before=dimensions())
        if op:
            report['external_input_validation'] = support.validate_native_inputs(
                op['external_inputs'], op['inherited_missing_weak_references'], external_before)
        if mode == 'inspect':
            inspector = core.inspect_locations if channel == 'location' else core.inspect_rotations
            inspected = inspector(bpy.context, rig, picked['bones'])
            if not inspected['ok']:
                raise ValueError('Pose inspection refused: ' + str(inspected['error']))
            after = state.summarize(state.capture())
            if after != report['before_snapshot']:
                raise ValueError('Pose inspection changed character storage')
            if channel == 'location':
                raw_after = location_channels(rig)
                if raw_after != report['location_channels_before']:
                    raise ValueError('Location inspection changed raw channels')
                report['location_channels_after'] = raw_after
            support.checked(config['input'])
            report.update(status='pass', source_unchanged=True, inspection=inspected, after_snapshot=after)
        else:
            fixture_before = fixture.capture(manifest) if manifest else None
            setter_name = '_write_location' if channel == 'location' else '_write_rotation'
            applier = core.apply_locations if channel == 'location' else core.apply_rotations
            protector = mask.protected_locations if channel == 'location' else mask.protected
            original, calls, injection = getattr(core, setter_name), 0, []
            injected_message = 'fixture injected after first completed pose ' + channel
            def injected(*args):
                nonlocal calls
                original(*args)
                calls += 1
                if calls == 1:
                    actual = list(getattr(args[0].pose.bones[args[1]], args[2]))
                    injection.append({'bone': args[1], 'field': args[2], 'requested': args[3], 'observed_after_write': actual})
                    raise RuntimeError(injected_message)
            if manifest and manifest.get('inject_rollback'):
                setattr(core, setter_name, injected)
            try:
                answer = applier(bpy.context, rig, request)
            finally:
                setattr(core, setter_name, original)
            rig.update_tag()
            bpy.context.view_layer.update()
            if manifest and manifest.get('inject_rollback'):
                if calls != 1 or len(injection) != 1 or injection[0]['requested'] != injection[0]['observed_after_write']:
                    raise ValueError('Rollback did not execute exactly one completed native ' + channel + ' write')
                if answer['ok'] or injected_message not in answer.get('error', {}).get('message', ''):
                    raise ValueError('Rollback result did not originate from injected native write')
                report['rollback_injection'] = {'completed_writes': calls, 'triggered': True, 'writes': injection}
            report.update(result=answer, operation_ok=answer['ok'], request=request)
            after = state.capture()
            if channel == 'location':
                raw_after = location_channels(rig)
                if answer['ok']:
                    raw_comparison = check_location_channels(report['location_channels_before'], raw_after, request, contract)
                elif raw_after != report['location_channels_before']:
                    raise ValueError('Rejected/rolled-back location changed raw channels')
                else:
                    raw_comparison = {'ok': True, 'scope': 'rejected/rolled-back full raw channels exact'}
                report.update(location_channels_after=raw_after, location_channel_comparison=raw_comparison)
            if manifest:
                fixture_after = fixture.capture(manifest)
                validation = fixture.validate(manifest, fixture_before, fixture_after, answer)
                report.update(manifest=manifest, fixture_before=fixture_before, fixture_after=fixture_after, fixture_validation=validation)
                if not validation['ok']:
                    report['status'] = 'fixture_failed'
                    support.dump(job / 'output-pose.json', report)
                    raise ValueError('Independent native pose fixture failed: ' + str(validation))
            elif not answer['ok']:
                report['status'] = 'rejected'
                support.dump(job / 'output-pose.json', report)
                raise ValueError('Pose apply refused: ' + str(answer['error']))
            if answer['ok']:
                first = protector(before, rig.name, rig.data.name, request)
                second = protector(after, rig.name, rig.data.name, request)
                if first != second:
                    report.update(status='protection_failed', changes=state.diff(first, second))
                    support.dump(job / 'output-pose.json', report)
                    raise ValueError('Non-target pose authoring fields changed')
            elif before != after:
                raise ValueError('Rejected/rolled-back pose changed character storage')
            evaluated_after, external_after = evaluator.evaluation(rig, support), support.paths()
            if external_after != external_before:
                raise ValueError('Pose operation changed external files')
            writer = persistence.save_candidate(job / 'output-candidate.blend', job)
            after_summary = state.summarize(after)
            if state.summarize(state.capture()) != after_summary:
                raise ValueError('Writer changed in-memory storage')
            if support.runtime_extra_users() != runtime_before:
                raise ValueError('Writer did not restore runtime users')
            report.update(status='candidate_waiting_independent_cold', candidate={k: writer[k] for k in ('file', 'expected_sha256')},
                          writer=writer, after_snapshot=after_summary, external_paths_after=external_after,
                          evaluation_after=evaluated_after, runtime_extra_users_after=support.runtime_extra_users(),
                          automatic_texspace_after=support.automatic_texspace(), object_dimensions_after=dimensions())
    for item in config['implementation']:
        support.checked(item)
    for role in ('input', 'operation', 'selection', 'producer_report'):
        if role in config:
            support.checked(config[role])
    if op:
        support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
    if support.sha(__file__) != report['entry_sha']:
        raise ValueError('Pose entry changed')
    report['object_dimensions_scope'] = 'All Object raw dimensions reported separately as derived evaluated bounds; not authoring state; no extra pose/rest/solver mask'
    report['collection_active_index_runtime_observation'] = state.collection_active_indices()
    report['collection_active_index_scope'] = 'raw index separate; only null active identity canonical index=-1; nonnull strict'
    support.dump(job / 'output-pose.json', report)
    return {'status': report['status'], 'mode': mode, 'operation_ok': report.get('operation_ok'),
            'source_unchanged': report.get('source_unchanged'), 'GUI': 'deferred', 'FPS': 'not_measured'}


if __name__ == '__main__':
    try:
        result = run(parameters, Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory) / 'output-debug.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
