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
            or value['schema'] != 'hrig.motion-clip.operation.v1'):
        raise ValueError('Exact pose operation schema/fields required')
    if type(value['rig']) is not str or not value['rig']:
        raise ValueError('Explicit rig required')
    contract.canonical_request(value['request'])
    checked_context(value['context'])
    return value


def selection(value, contract):
    if (type(value) is not dict or set(value) != {'schema', 'context', 'rig', 'bones', 'action_name'}
            or value['schema'] != 'hrig.motion-clip.selection.v1'):
        raise ValueError('Exact pose selection schema/fields required')
    if type(value['rig']) is not str or not value['rig']:
        raise ValueError('Explicit rig required')
    contract.canonical_bones(value['bones'])
    contract.canonical_name(value['action_name'])
    checked_context(value['context'])
    return value


def checked_context(value):
    if (type(value) is not dict or set(value) != {'scene', 'view_layer', 'frame', 'subframe'}
            or any(type(value[k]) is not str or not value[k] for k in ('scene', 'view_layer'))
            or type(value['frame']) is not int or value['frame'] != 0
            or type(value['subframe']) not in (int, float) or value['subframe'] != 0):
        raise ValueError('Explicit motion context requires frame0/subframe0')
    return value


def checked_witness(value):
    if type(value) is not dict or set(value) != {'schema', 'context', 'frames', 'meshes', 'bones', 'proxy_objects'} or value['schema'] != 'hrig.motion-clip.replay.v1':
        raise ValueError('Exact motion replay witness fields required')
    checked_context(value['context'])
    frames = value['frames']
    if (type(frames) is not list or not 1 <= len(frames) <= 64
            or any(type(f) is not int or not 1 <= f <= 250 for f in frames)
            or frames != list(range(frames[0], frames[0] + len(frames)))):
        raise ValueError('Replay requires bounded continuous ordered integer frames')
    for key, minimum, maximum in (('meshes', 1, 8), ('bones', 1, 256), ('proxy_objects', 0, 4)):
        names = value[key]
        if (type(names) is not list or not minimum <= len(names) <= maximum
                or any(type(n) is not str or not n or len(n) > 1024 for n in names)
                or len(set(names)) != len(names)):
            raise ValueError('Invalid explicit replay ' + key)
    return value


def _plain_numeric(value):
    import math
    from mathutils import Euler, Matrix, Quaternion, Vector
    if isinstance(value, (Vector, Matrix, Quaternion, Euler)) or type(value) in (list, tuple):
        return [_plain_numeric(v) for v in value]
    if type(value) not in (int, float):
        raise ValueError('Unsupported native replay numeric type: ' + type(value).__name__)
    try:
        value = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError('Native replay number is not finite and representable') from error
    if not math.isfinite(value):
        raise ValueError('Nonfinite native replay value')
    return value


def _coordinates(points):
    import base64, hashlib, struct, zlib
    raw = b''.join(struct.pack('<3f', *_plain_numeric(point)) for point in points)
    return {'vertices': len(raw) // 12, 'world_f32_sha256': hashlib.sha256(raw).hexdigest(),
            'finite': True, 'world_f32_zlib_base64': base64.b64encode(zlib.compress(raw)).decode('ascii')}


def _mesh(obj, graph, local=False):
    evaluated = graph.id_eval_get(obj)
    if not evaluated.is_evaluated:
        raise ValueError('Explicit replay Mesh lacks native evaluated ID: ' + obj.name)
    mesh = evaluated.to_mesh()
    try:
        if len(mesh.vertices) > 4000000:
            raise ValueError('Native mesh point budget exceeded')
        world = _coordinates(evaluated.matrix_world @ vertex.co for vertex in mesh.vertices)
        if local:
            return {'world': world, 'local': _coordinates(vertex.co for vertex in mesh.vertices),
                    'matrix_world': _plain_numeric(evaluated.matrix_world), 'cloth': _cloth(obj)}
        return world
    finally:
        evaluated.to_mesh_clear()


def _cloth(obj):
    rows = []
    for index, modifier in enumerate(obj.modifiers):
        if modifier.type != 'CLOTH':
            continue
        cache = modifier.point_cache
        rows.append({'index': index, 'name': modifier.name, 'show_viewport': modifier.show_viewport,
                     'show_render': modifier.show_render,
                     'cache': {key: getattr(cache, key) for key in ('frame_start', 'frame_end', 'is_baked',
                         'is_baking', 'is_outdated', 'use_disk_cache', 'use_external', 'info') if hasattr(cache, key)}})
    return rows


def fixture_animation(fixture, manifest, state):
    before = state.capture()
    try:
        value = fixture.observe_animation(manifest, [0, 1, 2, 4, 6, 7, 9])
    finally:
        bpy.context.scene.frame_set(0, subframe=0)
        bpy.context.view_layer.update()
    if not value.get('ok') or state.capture() != before:
        raise ValueError('Independent fixture animation or restored authoring state failed: ' + str(value))
    return value


def _all_cloth():
    return {obj.name: rows for obj in bpy.context.scene.objects if (rows := _cloth(obj))}


def _clip_inputs(rig, request, frame):
    import struct
    failures = []
    samples = {}
    f32 = lambda value: struct.unpack('<f', struct.pack('<f', value))[0]
    action, slot = rig.animation_data.action, rig.animation_data.action_slot
    bag = action.layers[0].strips[0].channelbag(slot)
    for track in request['tracks']:
        bone = rig.pose.bones[track['bone']]
        field = 'rotation_quaternion' if track['rotation_mode'] == 'QUATERNION' else 'rotation_euler'
        path = bone.path_from_id(field)
        curves = [bag.fcurves.find(path, index=index) for index in range(len(track['before']))]
        expected = [f32(curve.evaluate(frame)) for curve in curves]
        actual = list(getattr(bone, field))
        if actual != expected:
            failures.append({'bone': bone.name, 'expected': expected, 'actual': actual})
        samples[bone.name] = {'rotation_mode': bone.rotation_mode, 'expected_curve_values': expected,
                              'native_active_values': actual}
    return {'ok': not failures, 'failures': failures, 'tracks': samples}


def replay(config, job, report, support, evaluator, state, core, contract):
    import time
    started = time.perf_counter()
    producer = support.read(config['producer_report'])
    if (producer.get('schema') != report['schema'] or producer.get('operation_ok') is not True
            or producer.get('status') != 'candidate_waiting_independent_cold'
            or producer['candidate'] != config['input'] or producer['implementation'] != config['implementation']
            or producer['entry_sha'] != report['entry_sha'] or producer.get('operation_descriptor') != config.get('operation')):
        raise ValueError('Replay requires the exact successful authored producer/candidate')
    bundle = support.read(config['source_bundle'])
    if (bundle.get('status') != 'succeeded' or bundle.get('schema') != 'hrig.motion.bundle.v1'
            or bundle.get('candidate') != config['input'] or bundle.get('report') != config['producer_report']
            or bundle.get('implementation') != config['implementation'] or bundle.get('verification', {}).get('status') != 'pass'):
        raise ValueError('Replay source bundle identity/independent cold differs')
    picked = checked_witness(support.read(config['witness']))
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
    verified = core.verify_clip(bpy.context, rig, producer['request'])
    if not verified['ok'] or verified['state'] != producer['clip_verification']['state']:
        raise ValueError('Replay exact Action/slot state differs')
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
    if not set(row['bone'] for row in producer['request']['tracks']) <= set(picked['bones']):
        raise ValueError('Replay must observe every authored input bone')
    initial_cloth = _all_cloth()
    for rows in initial_cloth.values():
        for row in rows:
            cache = row['cache']
            if cache.get('is_baked') or cache.get('is_baking') or cache.get('use_disk_cache') or cache.get('use_external'):
                raise ValueError('Replay refuses baked/baking/external/disk Cloth cache')
            if row['show_viewport'] and (picked['frames'][0] != cache['frame_start'] or picked['frames'][-1] > cache['frame_end']):
                raise ValueError('Replay must start at every active Cloth cache start and stay in range')
    if any(not _cloth(bpy.data.objects[name]) for name in picked['proxy_objects']):
        raise ValueError('Every proxy must have explicit native Cloth')
    rows, total, previous_counts = [], 0, None
    try:
        for frame in picked['frames']:
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            graph = bpy.context.evaluated_depsgraph_get()
            evaluated_rig = graph.id_eval_get(rig)
            if not evaluated_rig.is_evaluated:
                raise ValueError('Replay rig has no native evaluated ID')
            input_validation = _clip_inputs(rig, producer['request'], frame)
            if not input_validation['ok']:
                raise ValueError('Native Action component input evaluation differs: ' + str(input_validation))
            sample = {'frame': frame, 'meshes': {}, 'bones': {}, 'proxies': {}, 'clip_input_validation': input_validation}
            for name in picked['meshes']:
                sample['meshes'][name] = _mesh(bpy.data.objects[name], graph)
            for name in picked['proxy_objects']:
                sample['proxies'][name] = _mesh(bpy.data.objects[name], graph, local=True)
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
                if original.rotation_mode not in contract.MODES:
                    raise ValueError('Replay selected bone active mode unsupported')
                sample['bones'][name] = {'rotation_mode': original.rotation_mode,
                    'native_active_values': _plain_numeric(getattr(original, field)),
                    'world_matrix': _plain_numeric(evaluated_rig.matrix_world @ pb.matrix),
                    'matrix': _plain_numeric(pb.matrix), 'matrix_basis': _plain_numeric(pb.matrix_basis),
                    'head': _plain_numeric(evaluated_rig.matrix_world @ pb.head),
                    'tail': _plain_numeric(evaluated_rig.matrix_world @ pb.tail)}
            rows.append(sample)
    finally:
        bpy.context.scene.frame_set(picked['context']['frame'], subframe=picked['context']['subframe'])
        bpy.context.view_layer.update()
    final = state.capture()
    if final != initial:
        support.dump(job / 'output-motion-replay-diff.json', state.diff(initial, final))
        raise ValueError('Read-only replay changed authoring state after returning to context frame0')
    final_clip = core.verify_clip(bpy.context, rig, producer['request'])
    if not final_clip['ok'] or final_clip['state'] != verified['state'] or support.paths() != external:
        raise ValueError('Replay changed Action structure or external dependencies')
    for item in config['implementation']:
        support.checked(item)
    for role in ('input', 'operation', 'producer_report', 'witness', 'source_bundle'):
        if role in config:
            support.checked(config[role])
    if op:
        support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
    report.update(status='pass', input=config['input'], source_bundle=config['source_bundle'],
                  producer_report=config['producer_report'], witness_descriptor=config['witness'], witness=picked,
                  rig=rig.name, context=picked['context'], source_unchanged=True, source_saved=False,
                  context_restored=True, after_snapshot=state.summarize(final), clip_verification=final_clip,
                  initial_cloth=initial_cloth, final_cloth=_all_cloth(), samples=rows, complete_coordinate_points=total,
                  external_paths=external, background_wall_seconds=time.perf_counter() - started,
                  cache_scope='fresh-process sequential recomputation only; unbaked runtime caches are neither persisted nor random-access certified',
                  topology_scope='complete selected evaluated vertex coordinates with fixed count; connectivity/continuous collision/render/GUI quality not certified')
    support.dump(job / 'output-motion.json', report)
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
        ROOT / 'scripts/character_state.py', ROOT / 'scripts/motion_state.py', ROOT / 'scripts/motion_fixture.py',
        ROOT / 'scripts/run_weights.py', ROOT / 'scripts/run_styles.py', OBSERVER / 'inspection.py', OBSERVER / 'protocol.py'}
    files = [support.checked(x) for x in config['implementation']]
    if len(files) != len(set(files)) or set(files) != required:
        raise ValueError('Exact pose implementation closure required')
    sys.path.insert(0, str(ROOT / 'addon'))
    from humanoid_rigging_probe import motion_clips as core, motion_contract as contract, persistence
    import inspection
    if Path(inspection.__file__).resolve() != OBSERVER / 'inspection.py':
        raise ValueError('Pinned observer import differs')
    state = load('_hrig_motion_state', ROOT / 'scripts/motion_state.py')
    fixture = load('_hrig_pose_fixture', ROOT / 'scripts/motion_fixture.py')
    mode = config['mode']
    extra = {'inspect': {'input', 'selection'}, 'create': {'input', 'operation'},
             'fixture': {'case'}, 'cold': {'input', 'producer_report'},
             'replay': {'input', 'producer_report', 'witness', 'source_bundle'}}
    expected = {'mode', 'implementation', 'addon_root', 'observer_root'} | extra.get(mode, set())
    if mode in ('cold', 'replay') and 'operation' in config:
        expected.add('operation')
    if mode not in extra or set(config) != expected:
        raise ValueError('Unknown pose mode/fields')
    report = {'schema': 'hrig.motion.report.v1', 'mode': mode, 'blender': bpy.app.version_string,
              'build': bpy.app.build_hash.decode(), 'implementation': config['implementation'],
              'entry_sha': support.sha(__file__), 'GUI': 'deferred_by_user', 'FPS': 'not_measured',
              'complete_improved_Aon': False}
    op = None
    manifest = None
    if mode == 'replay':
        return replay(config, job, report, support, evaluator, state, core, contract)
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
            support.dump(job / 'output-motion.json', report)
            raise ValueError('Cold character storage mismatch')
        if producer['operation_ok']:
            clip = core.verify_clip(bpy.context, rig, producer['request'])
            if not clip['ok'] or clip['state'] != producer['clip_verification']['state']:
                raise ValueError('Cold exact Action content/slot verification differs')
            report['clip_verification'] = clip
        actual_evaluation = evaluator.evaluation(rig, support)
        comparison = evaluator.compare_evaluation(producer['evaluation_after'], actual_evaluation)
        if support.paths() != producer['external_paths_after']:
            raise ValueError('Cold external files differ')
        if op:
            report['external_input_validation'] = support.validate_native_inputs(
                op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
        manifest = producer.get('manifest')
        if manifest:
            if manifest.get('inject_rollback') or manifest.get('inject_stage'):
                evidence = producer.get('rollback_injection', {})
                if evidence.get('completed_writes') != 1 or evidence.get('triggered') is not True:
                    raise ValueError('Cold rollback lacks completed native write evidence')
            observed = fixture.capture(manifest)
            validation = fixture.validate(manifest, producer['fixture_before'], observed, producer['result'])
            if not validation['ok']:
                raise ValueError('Independent cold pose fixture failed: ' + str(validation))
            report.update(manifest=manifest, fixture_after=observed, fixture_validation=validation)
            if producer['operation_ok']:
                report['fixture_animation'] = fixture_animation(fixture, manifest, state)
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
            ctx = {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                   'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe}
        else:
            bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
            if tuple(bpy.data.version[:2]) > tuple(bpy.app.version[:2]):
                raise ValueError('Future source version rejected')
            if mode == 'create':
                op = operation(support.read(config['operation']), contract)
                request, ctx, rig = op['request'], op['context'], bpy.data.objects.get(op['rig'])
                report.update(operation=op, operation_descriptor=config['operation'])
            else:
                picked = selection(support.read(config['selection']), contract)
                ctx, rig = picked['context'], bpy.data.objects.get(picked['rig'])
            support.select_context(checked_context(ctx), frames=True)
        if rig is None:
            raise ValueError('Explicit rig missing')
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
            inspected = core.inspect_clip(bpy.context, rig, picked['bones'], picked['action_name'])
            if not inspected['ok']:
                raise ValueError('Pose inspection refused: ' + str(inspected['error']))
            after = state.summarize(state.capture())
            if after != report['before_snapshot']:
                raise ValueError('Pose inspection changed character storage')
            support.checked(config['input'])
            report.update(status='pass', source_unchanged=True, inspection=inspected, after_snapshot=after)
        else:
            fixture_before = fixture.capture(manifest) if manifest else None
            original, calls, injection = core._mutation_seam, 0, []
            target_stage = manifest.get('inject_stage') if manifest else None
            if manifest and manifest.get('inject_rollback') and not target_stage:
                target_stage = 'after_bind'
            def injected(stage, owned_rig, action):
                nonlocal calls
                original(stage, owned_rig, action)
                if stage == target_stage:
                    calls += 1
                    injection.append({'stage': stage, 'action': action.name,
                                      'action_count': len(bpy.data.actions), 'action_users': action.users,
                                      'clip_state': core.clip_state(action), 'animation_state': core.animation_state(owned_rig)})
                    raise RuntimeError('fixture injected after completed motion stage ' + stage)
            if target_stage:
                core._mutation_seam = injected
            try:
                answer = core.create_clip(bpy.context, rig, request)
            finally:
                core._mutation_seam = original
            rig.update_tag()
            bpy.context.view_layer.update()
            if target_stage:
                if calls != 1 or len(injection) != 1 or injection[0]['stage'] != target_stage:
                    raise ValueError('Rollback did not observe exactly one completed native mutation stage')
                if answer['ok'] or 'fixture injected after completed motion stage ' not in answer.get('error', {}).get('message', ''):
                    raise ValueError('Rollback result did not originate from injected native mutation')
                report['rollback_injection'] = {'completed_writes': calls, 'triggered': True, 'writes': injection}
            report.update(result=answer, operation_ok=answer['ok'], request=request)
            after = state.capture()
            if manifest:
                fixture_after = fixture.capture(manifest)
                validation = fixture.validate(manifest, fixture_before, fixture_after, answer)
                report.update(manifest=manifest, fixture_before=fixture_before, fixture_after=fixture_after, fixture_validation=validation)
                if not validation['ok']:
                    report['status'] = 'fixture_failed'
                    support.dump(job / 'output-motion.json', report)
                    raise ValueError('Independent native pose fixture failed: ' + str(validation))
            elif not answer['ok']:
                report['status'] = 'rejected'
                support.dump(job / 'output-motion.json', report)
                raise ValueError('Pose apply refused: ' + str(answer['error']))
            if answer['ok']:
                verification = core.verify_clip(bpy.context, rig, request)
                protection = state.compare_create(before, after, rig.name, request, verification)
                report.update(clip_verification=verification, authoring_protection=protection)
                if not protection['ok']:
                    report.update(status='protection_failed', changes=protection['changes'])
                    support.dump(job / 'output-motion.json', report)
                    raise ValueError('Non-target motion authoring fields changed')
                if manifest:
                    report['fixture_animation'] = fixture_animation(fixture, manifest, state)
            elif before != after:
                raise ValueError('Rejected/rolled-back pose changed character storage')
            evaluated_after, external_after = evaluator.evaluation(rig, support), support.paths()
            if external_after != external_before:
                raise ValueError('Pose operation changed external files')
            runtime_expected_writer = support.runtime_extra_users()
            if (not set(runtime_before) <= set(runtime_expected_writer)
                    or any(runtime_expected_writer[key] != value for key, value in runtime_before.items())):
                raise ValueError('Motion authoring changed original runtime extra-user states')
            runtime_added = set(runtime_expected_writer) - set(runtime_before)
            expected_added = {inspection.key(inspection.identity(bpy.data.actions[request['action_name']]))} if answer['ok'] else set()
            if runtime_added != expected_added or any(runtime_expected_writer[key] for key in runtime_added):
                raise ValueError('Only the new non-extra-user Action may extend runtime accounting')
            writer = persistence.save_candidate(job / 'output-candidate.blend', job)
            after_summary = state.summarize(after)
            if state.summarize(state.capture()) != after_summary:
                raise ValueError('Writer changed in-memory storage')
            if support.runtime_extra_users() != runtime_expected_writer:
                raise ValueError('Writer did not restore runtime users')
            report.update(status='candidate_waiting_independent_cold', candidate={k: writer[k] for k in ('file', 'expected_sha256')},
                          writer=writer, after_snapshot=after_summary, external_paths_after=external_after,
                          evaluation_after=evaluated_after, runtime_extra_users_after=support.runtime_extra_users(),
                          automatic_texspace_after=support.automatic_texspace(), object_dimensions_after=dimensions())
    for item in config['implementation']:
        support.checked(item)
    for role in ('input', 'operation', 'selection', 'producer_report', 'witness', 'source_bundle'):
        if role in config:
            support.checked(config[role])
    if op:
        support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
    if support.sha(__file__) != report['entry_sha']:
        raise ValueError('Pose entry changed')
    report['object_dimensions_scope'] = 'All Object raw dimensions reported separately as derived evaluated bounds; not authoring state; no extra pose/rest/solver mask'
    report['collection_active_index_runtime_observation'] = state.collection_active_indices()
    report['collection_active_index_scope'] = 'raw index separate; only null active identity canonical index=-1; nonnull strict'
    support.dump(job / 'output-motion.json', report)
    return {'status': report['status'], 'mode': mode, 'operation_ok': report.get('operation_ok'),
            'source_unchanged': report.get('source_unchanged'), 'GUI': 'deferred', 'FPS': 'not_measured'}


if __name__ == '__main__':
    try:
        result = run(parameters, Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory) / 'output-debug.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
