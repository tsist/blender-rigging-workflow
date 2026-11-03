"""Pinned selected rotation producer and independent native cold worker."""
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
            or value['schema'] != 'hrig.cloth-quality.operation.v1'):
        raise ValueError('Exact pose operation schema/fields required')
    if type(value['rig']) is not str or not value['rig']:
        raise ValueError('Explicit rig required')
    contract.canonical_request(value['request'])
    checked_context(value['context'])
    return value


def selection(value, contract):
    if (type(value) is not dict or set(value) != {'schema', 'context', 'rig', 'targets'}
            or value['schema'] != 'hrig.cloth-quality.selection.v1'):
        raise ValueError('Exact pose selection schema/fields required')
    if type(value['rig']) is not str or not value['rig']:
        raise ValueError('Explicit rig required')
    contract.canonical_targets(value['targets'])
    checked_context(value['context'])
    return value



def checked_context(value):
    if (type(value) is not dict or set(value) != {'scene', 'view_layer', 'frame', 'subframe'}
            or any(type(value[k]) is not str or not value[k] for k in ('scene', 'view_layer'))
            or type(value['frame']) is not int or value['frame'] != 0
            or type(value['subframe']) not in (int, float) or value['subframe'] != 0):
        raise ValueError('Explicit dynamics context requires frame0/subframe0')
    return value


def targets_for(request):
    return [{key: row[key] for key in ('object', 'modifier', 'index')} for row in request['changes']]


def expected_fields(request, success):
    return [{**target, 'quality': row['after' if success else 'before']}
            for target, row in zip(targets_for(request), request['changes'])]

def replay(config, job, report, support, evaluator, state, core, contract, sampler):
    import time
    from humanoid_rigging_probe import motion_clips as motion_core, motion_contract
    started = time.perf_counter()
    producer = support.read(config['producer_report'])
    if (producer.get('schema') != report['schema'] or producer.get('operation_ok') is not True
            or producer.get('status') != 'candidate_waiting_independent_cold'
            or producer['candidate'] != config['input'] or producer['implementation'] != config['implementation']
            or producer['entry_sha'] != report['entry_sha'] or producer.get('operation_descriptor') != config.get('operation')):
        raise ValueError('Replay requires the exact successful authored producer/candidate')
    bundle = support.read(config['source_bundle'])
    if (bundle.get('status') != 'succeeded' or bundle.get('schema') != 'hrig.dynamics.bundle.v1'
            or bundle.get('candidate') != config['input'] or bundle.get('report') != config['producer_report']
            or bundle.get('implementation') != config['implementation'] or bundle.get('verification', {}).get('status') != 'pass'):
        raise ValueError('Replay source bundle identity/independent cold differs')
    picked = sampler.checked_witness(support.read(config['witness']))
    clip_request = motion_contract.canonical_request(support.read(config['clip_request']))
    if picked['context'] != producer['context']:
        raise ValueError('Replay witness context differs from frame0 producer')
    op = None
    if 'operation' in config:
        op = operation(support.read(config['operation']), contract)
        if op != producer['operation']:
            raise ValueError('Replay operation differs from producer')
    bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
    support.select_context(checked_context(picked['context']), frames=True)
    rig = bpy.data.objects[producer['rig']]
    initial = state.capture()
    if state.summarize(initial) != producer['after_snapshot']:
        raise ValueError('Replay fresh saved authoring state differs')
    verified = motion_core.verify_clip(bpy.context, rig, clip_request)
    if not verified['ok']:
        raise ValueError('Replay exact Action/slot state differs')
    quality = core.inspect_quality(bpy.context, targets_for(producer['request']))
    if not quality['ok'] or quality['snapshot'] != producer['quality_validation']['snapshot']:
        raise ValueError('Replay exact candidate quality/settings differ')
    external = support.paths()
    if external != producer['external_paths_after']:
        raise ValueError('Replay external paths differ')
    if op:
        support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], external)
    for name in picked['meshes'] + picked['proxy_objects']:
        obj = bpy.context.scene.objects.get(name)
        if obj is None or obj.type != 'MESH':
            raise ValueError('Explicit replay mesh identity missing: ' + name)
    if any(rig.pose.bones.get(name) is None for name in picked['bones']):
        raise ValueError('Explicit replay bone missing')
    if not set(row['bone'] for row in clip_request['tracks']) <= set(picked['bones']):
        raise ValueError('Replay must observe every authored input bone')
    initial_cloth = sampler._all_cloth()
    for rows in initial_cloth.values():
        for row in rows:
            cache = row['cache']
            if cache.get('is_baked') or cache.get('is_baking') or cache.get('use_disk_cache') or cache.get('use_external'):
                raise ValueError('Replay refuses baked/baking/external/disk Cloth cache')
            if row['show_viewport'] and (picked['frames'][0] != cache['frame_start'] or picked['frames'][-1] > cache['frame_end']):
                raise ValueError('Replay must start at every active Cloth cache start and stay in range')
    if any(not sampler._cloth(bpy.data.objects[name]) for name in picked['proxy_objects']):
        raise ValueError('Every proxy must have explicit native Cloth')
    rows, total, previous_counts, timings = [], 0, None, []
    try:
        for frame in picked['frames']:
            evaluation_started = time.perf_counter()
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            graph = bpy.context.evaluated_depsgraph_get()
            evaluated_rig = graph.id_eval_get(rig)
            if not evaluated_rig.is_evaluated:
                raise ValueError('Replay rig has no native evaluated ID')
            coordinate_started = time.perf_counter()
            input_validation = sampler._clip_inputs(rig, clip_request, frame)
            if not input_validation['ok']:
                raise ValueError('Native Action component input evaluation differs: ' + str(input_validation))
            sample = {'frame': frame, 'meshes': {}, 'bones': {}, 'proxies': {}, 'clip_input_validation': input_validation}
            for name in picked['meshes']:
                sample['meshes'][name] = sampler._mesh(bpy.data.objects[name], graph)
            for name in picked['proxy_objects']:
                sample['proxies'][name] = sampler._mesh(bpy.data.objects[name], graph, local=True)
            counts = {name: item['vertices'] for name, item in sample['meshes'].items()}
            counts.update({'proxy:' + name: item['world']['vertices'] for name, item in sample['proxies'].items()})
            if previous_counts is not None and counts != previous_counts:
                raise ValueError('Replay evaluated point counts changed; correspondence is undefined')
            previous_counts = counts
            frame_points = sum(item['vertices'] for item in sample['meshes'].values()) + sum(item['world']['vertices'] * 2 for item in sample['proxies'].values())
            if frame_points * len(picked['frames']) > 4000000:
                raise ValueError('Complete replay coordinate budget exceeded; no hash-only downgrade')
            total += frame_points
            for name in picked['bones']:
                pb, original = evaluated_rig.pose.bones[name], rig.pose.bones[name]
                field = 'rotation_quaternion' if original.rotation_mode == 'QUATERNION' else 'rotation_euler'
                if original.rotation_mode not in motion_contract.MODES:
                    raise ValueError('Replay selected bone active mode unsupported')
                sample['bones'][name] = {'rotation_mode': original.rotation_mode,
                    'native_active_values': sampler._plain_numeric(getattr(original, field)),
                    'world_matrix': sampler._plain_numeric(evaluated_rig.matrix_world @ pb.matrix),
                    'matrix': sampler._plain_numeric(pb.matrix), 'matrix_basis': sampler._plain_numeric(pb.matrix_basis),
                    'head': sampler._plain_numeric(evaluated_rig.matrix_world @ pb.head),
                    'tail': sampler._plain_numeric(evaluated_rig.matrix_world @ pb.tail)}
            rows.append(sample)
            timings.append({'frame': frame, 'frame_set_update_first_eval_seconds': coordinate_started - evaluation_started,
                            'coordinate_capture_seconds': time.perf_counter() - coordinate_started})
    finally:
        bpy.context.scene.frame_set(picked['context']['frame'], subframe=picked['context']['subframe'])
        bpy.context.view_layer.update()
    final = state.capture()
    if final != initial:
        support.dump(job / 'output-dynamics-replay-diff.json', state.diff(initial, final))
        raise ValueError('Read-only replay changed authoring state after returning to context frame0')
    final_clip = motion_core.verify_clip(bpy.context, rig, clip_request)
    if not final_clip['ok'] or final_clip['state'] != verified['state'] or support.paths() != external:
        raise ValueError('Replay changed Action structure or external dependencies')
    final_quality = core.inspect_quality(bpy.context, targets_for(producer['request']))
    if not final_quality['ok'] or final_quality['snapshot'] != quality['snapshot']:
        raise ValueError('Replay changed quality authoring settings')
    for item in config['implementation']:
        support.checked(item)
    for role in ('input', 'operation', 'producer_report', 'witness', 'source_bundle', 'clip_request'):
        if role in config:
            support.checked(config[role])
    if op:
        support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
    report.update(status='pass', input=config['input'], source_bundle=config['source_bundle'],
                  producer_report=config['producer_report'], witness_descriptor=config['witness'], witness=picked,
                  clip_request_descriptor=config['clip_request'], clip_request=clip_request, quality_validation=final_quality,
                  frame_timings=timings, timing_scope='background stage wall seconds only; not viewport FPS',
                  rig=rig.name, context=picked['context'], source_unchanged=True, source_saved=False,
                  context_restored=True, after_snapshot=state.summarize(final), clip_verification=final_clip,
                  initial_cloth=initial_cloth, final_cloth=sampler._all_cloth(), samples=rows, complete_coordinate_points=total,
                  external_paths=external, background_wall_seconds=time.perf_counter() - started,
                  cache_scope='fresh-process sequential recomputation only; unbaked runtime caches are neither persisted nor random-access certified',
                  topology_scope='complete selected evaluated vertex coordinates with fixed count; connectivity/continuous collision/render/GUI quality not certified')
    support.dump(job / 'output-dynamics.json', report)
    return {'status': 'pass', 'mode': 'replay', 'source_unchanged': True, 'source_saved': False, 'GUI': 'deferred', 'FPS': 'not_measured'}


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
        ROOT / 'scripts/character_state.py', ROOT / 'scripts/dynamics_state.py', ROOT / 'scripts/dynamics_fixture.py',
        ROOT / 'scripts/motion_state.py', ROOT / 'scripts/motion_cli.py', ROOT / 'scripts/run_motion.py',
        ROOT / 'scripts/run_weights.py', ROOT / 'scripts/run_styles.py', OBSERVER / 'inspection.py', OBSERVER / 'protocol.py'}
    files = [support.checked(x) for x in config['implementation']]
    if len(files) != len(set(files)) or set(files) != required:
        raise ValueError('Exact pose implementation closure required')
    sys.path.insert(0, str(ROOT / 'addon'))
    from humanoid_rigging_probe import cloth_quality as core, dynamics_contract as contract, persistence
    import inspection
    if Path(inspection.__file__).resolve() != OBSERVER / 'inspection.py':
        raise ValueError('Pinned observer import differs')
    state = load('_hrig_dynamics_state', ROOT / 'scripts/dynamics_state.py')
    sampler = load('_hrig_dynamics_motion_sampler', ROOT / 'scripts/run_motion.py')
    fixture = load('_hrig_pose_fixture', ROOT / 'scripts/dynamics_fixture.py')
    mode = config['mode']
    extra = {'inspect': {'input', 'selection'}, 'apply': {'input', 'operation'},
             'fixture': {'case'}, 'cold': {'input', 'producer_report'},
             'replay': {'input', 'producer_report', 'witness', 'source_bundle', 'clip_request'}}
    expected = {'mode', 'implementation', 'addon_root', 'observer_root'} | extra.get(mode, set())
    if mode in ('cold', 'replay') and 'operation' in config:
        expected.add('operation')
    if mode not in extra or set(config) != expected:
        raise ValueError('Unknown pose mode/fields')
    report = {'schema': 'hrig.dynamics.report.v1', 'mode': mode, 'blender': bpy.app.version_string,
              'build': bpy.app.build_hash.decode(), 'implementation': config['implementation'],
              'entry_sha': support.sha(__file__), 'GUI': 'deferred_by_user', 'FPS': 'not_measured',
              'complete_improved_Aon': False}
    op = None
    manifest = None
    if mode == 'replay':
        return replay(config, job, report, support, evaluator, state, core, contract, sampler)
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
        bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
        support.select_context(checked_context(producer['context']), frames=True)
        rig = bpy.data.objects[producer['rig']]
        actual = state.summarize(state.capture())
        if actual != producer['after_snapshot']:
            report.update(status='cold_mismatch', changes=state.diff(producer['after_snapshot'], actual))
            support.dump(job / 'output-dynamics.json', report)
            raise ValueError('Cold character storage mismatch')
        if producer['operation_ok']:
            inspected = core.inspect_quality(bpy.context, targets_for(producer['request']))
            if not inspected['ok'] or inspected['fields'] != producer['quality_validation']['fields'] or inspected['snapshot'] != producer['quality_validation']['snapshot']:
                raise ValueError('Cold exact Cloth quality or original settings differ')
            report['quality_validation'] = inspected
        actual_evaluation = evaluator.evaluation(rig, support)
        comparison = evaluator.compare_evaluation(producer['evaluation_after'], actual_evaluation)
        if support.paths() != producer['external_paths_after']:
            raise ValueError('Cold external files differ')
        if op:
            report['external_input_validation'] = support.validate_native_inputs(
                op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
        manifest = producer.get('manifest')
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
            request = fixture.prepare(manifest, core, job_directory=job)
            ctx = {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                   'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe}
        else:
            bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
            if tuple(bpy.data.version[:2]) > tuple(bpy.app.version[:2]):
                raise ValueError('Future source version rejected')
            if mode == 'apply':
                op = operation(support.read(config['operation']), contract)
                request, ctx, rig = op['request'], op['context'], bpy.data.objects.get(op['rig'])
                report.update(operation=op, operation_descriptor=config['operation'])
            else:
                picked = selection(support.read(config['selection']), contract)
                ctx, rig = picked['context'], bpy.data.objects.get(picked['rig'])
            support.select_context(checked_context(ctx), frames=True)
        if rig is None:
            raise ValueError('Explicit rig missing')
        if mode != 'inspect':
            request = contract.canonical_request(request)
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
            inspected = core.inspect_quality(bpy.context, picked['targets'])
            if not inspected['ok']:
                raise ValueError('Pose inspection refused: ' + str(inspected['error']))
            after = state.summarize(state.capture())
            if after != report['before_snapshot']:
                raise ValueError('Pose inspection changed character storage')
            support.checked(config['input'])
            report.update(status='pass', source_unchanged=True, inspection=inspected, after_snapshot=after)
        else:
            fixture_before = fixture.capture(manifest) if manifest else None
            original, calls, injection = core._write_quality, 0, []
            def observed_setter(modifier, value):
                nonlocal calls
                before_value = modifier.settings.quality
                original(modifier, value)
                calls += 1
                injection.append({'modifier': modifier.name, 'before': before_value,
                                  'requested': value, 'observed_after_write': modifier.settings.quality})
                if manifest and manifest.get('inject_rollback') and calls == 1:
                    raise RuntimeError('fixture injected after first completed Cloth quality setter')
            core._write_quality = observed_setter
            try:
                answer = core.apply_quality(bpy.context, request)
            finally:
                core._write_quality = original
            bpy.context.view_layer.update()
            if manifest and manifest.get('inject_rollback'):
                if calls != 1 or injection[0]['requested'] != injection[0]['observed_after_write'] or injection[0]['before'] == injection[0]['observed_after_write']:
                    raise ValueError('Rollback fixture requires exactly one completed real quality change')
                if answer['ok'] or 'fixture injected after first completed Cloth quality setter' not in answer.get('error', {}).get('message', ''):
                    raise ValueError('Quality rollback must originate from injected real setter')
                report['rollback_injection'] = {'completed_writes': calls, 'triggered': True, 'writes': injection}
            elif answer['ok']:
                expected_calls = sum(row['before'] != row['after'] for row in request['changes'])
                if calls != expected_calls or any(row['requested'] != row['observed_after_write'] for row in injection):
                    raise ValueError('Quality setter calls differ from exact non-noop changes')
            elif calls:
                raise ValueError('Preflight rejection must not call a quality setter')
            report['setter_observation'] = {'calls': calls, 'writes': injection}
            report.update(result=answer, operation_ok=answer['ok'], request=request)
            after = state.capture()
            if manifest:
                fixture_after = fixture.capture(manifest)
                validation = fixture.validate(manifest, fixture_before, fixture_after, answer)
                report.update(manifest=manifest, fixture_before=fixture_before, fixture_after=fixture_after, fixture_validation=validation)
                if not validation['ok']:
                    report['status'] = 'fixture_failed'
                    support.dump(job / 'output-dynamics.json', report)
                    raise ValueError('Independent native pose fixture failed: ' + str(validation))
            elif not answer['ok']:
                report['status'] = 'rejected'
                support.dump(job / 'output-dynamics.json', report)
                raise ValueError('Pose apply refused: ' + str(answer['error']))
            if answer['ok']:
                protection = state.compare_patch(before, after, request)
                report['authoring_protection'] = protection
                if not protection['ok']:
                    report.update(status='protection_failed', changes=protection['changes'])
                    support.dump(job / 'output-dynamics.json', report)
                    raise ValueError('Non-target dynamics authoring fields changed')
                quality_after = core.inspect_quality(bpy.context, targets_for(request))
                if not quality_after['ok'] or quality_after['fields'] != expected_fields(request, True):
                    raise ValueError('Exact post-apply quality validation failed')
                report['quality_validation'] = quality_after
            elif before != after:
                raise ValueError('Rejected/rolled-back pose changed character storage')
            evaluated_after, external_after = evaluator.evaluation(rig, support), support.paths()
            if external_after != external_before:
                raise ValueError('Pose operation changed external files')
            report['evaluation_comparison'] = evaluator.compare_evaluation(evaluated_before, evaluated_after)
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
    for role in ('input', 'operation', 'selection', 'producer_report', 'witness', 'source_bundle', 'clip_request'):
        if role in config:
            support.checked(config[role])
    if op:
        support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
    if support.sha(__file__) != report['entry_sha']:
        raise ValueError('Pose entry changed')
    report['object_dimensions_scope'] = 'All Object raw dimensions reported separately as derived evaluated bounds; not authoring state; no extra pose/rest/solver mask'
    report['collection_active_index_runtime_observation'] = state.collection_active_indices()
    report['collection_active_index_scope'] = 'raw index separate; only null active identity canonical index=-1; nonnull strict'
    support.dump(job / 'output-dynamics.json', report)
    return {'status': report['status'], 'mode': mode, 'operation_ok': report.get('operation_ok'),
            'source_unchanged': report.get('source_unchanged'), 'GUI': 'deferred', 'FPS': 'not_measured'}


if __name__ == '__main__':
    try:
        result = run(parameters, Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory) / 'output-debug.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
