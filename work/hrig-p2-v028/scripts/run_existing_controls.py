"""Pinned settings producer, actual-write recovery and independent cold worker."""
import importlib.util
from pathlib import Path
import sys
sys.dont_write_bytecode = True
import bpy

ROOT = Path(__file__).resolve().parents[1]
OBSERVER = ROOT.parents[1]/'tools/blenderctl'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(value, message):
    if not value:
        raise ValueError(message)


def run(config, job):
    require(tuple(bpy.app.version) == (5, 2, 1) and bpy.app.build_hash.decode() == '9e2066aef7ef'
            and bpy.app.background and not bpy.context.preferences.filepaths.use_scripts_auto_execute,
            'Pinned background Blender5.2.1 build with embedded autoexec disabled required')
    require(Path(config['addon_root']).resolve() == ROOT/'addon' and Path(config['observer_root']).resolve() == OBSERVER,
            'Settings role roots differ')
    sys.path.insert(0, str(OBSERVER))
    sys.path.insert(0, str(ROOT/'addon'))
    from humanoid_rigging_probe import existing_controls as core, existing_control_contract as contract, persistence
    import inspection
    support = load('_hrig_settings_support', ROOT/'scripts/run_weights.py')
    evaluator = load('_hrig_settings_evaluator', ROOT/'scripts/run_styles.py')
    state = load('_hrig_settings_state', ROOT/'scripts/existing_control_state.py')
    fixture = load('_hrig_settings_fixture', ROOT/'scripts/existing_control_fixture.py')
    helpers = ('character_state.py', 'motion_state.py', 'dynamics_state.py', 'existing_control_state.py',
               'existing_control_fixture.py', 'run_existing_controls.py', 'run_weights.py', 'run_styles.py')
    required = set((ROOT/'addon/humanoid_rigging_probe').glob('*.py')) | {ROOT/'scripts'/n for n in helpers} | {OBSERVER/'inspection.py', OBSERVER/'protocol.py'}
    files = [support.checked(d) for d in config['implementation']]
    require(len(files) == len(set(files)) and set(files) == required, 'Exact implementation closure required')
    require(Path(inspection.__file__).resolve() == OBSERVER/'inspection.py', 'Pinned original observer required')
    mode = config['mode']
    base_keys = {'mode', 'implementation', 'addon_root', 'observer_root'}
    extra = {'inspect': {'input', 'selection'}, 'apply': {'input', 'operation'},
             'fixture': {'case'}, 'cold': {'input', 'producer_report'}}
    keys = base_keys | extra.get(mode, set())
    if mode == 'cold' and 'operation' in config:
        keys.add('operation')
    require(mode in extra and set(config) == keys, 'Exact native settings mode/config required')
    report = {'schema': 'hrig.settings.report.v1', 'mode': mode, 'blender': bpy.app.version_string,
              'build': bpy.app.build_hash.decode(), 'implementation': config['implementation'],
              'entry_sha': support.sha(__file__), 'GUI': 'deferred_by_user', 'FPS': 'not_measured',
              'complete_improved_Aon': False}
    op, manifest, producer = None, None, None
    if mode == 'cold':
        producer = support.read(config['producer_report'])
        require(producer['schema'] == report['schema'] and producer['status'] == 'candidate_waiting_independent_cold'
                and producer['candidate'] == config['input'] and producer['implementation'] == config['implementation']
                and producer['entry_sha'] == report['entry_sha'] and producer.get('operation_descriptor') == config.get('operation'),
                'Producer/cold source/config/implementation conflict')
        if 'operation' in config:
            op = contract.canonical_operation(support.read(config['operation']))
            require(op == producer['operation'], 'Exact original operation required')
        selection = contract.canonical_selection(producer['selection'])
        manifest = producer.get('manifest')
        bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
        support.select_context(producer['context'], frames=True)
    elif mode == 'fixture':
        bpy.ops.wm.read_factory_settings(use_empty=True)
        manifest = fixture.create_case(config['case'])
        selection = contract.canonical_selection(manifest['selection'])
        patch = fixture.prepare(manifest, core, job_directory=job)
    else:
        bpy.ops.wm.open_mainfile(filepath=str(support.checked(config['input'])), load_ui=False, use_scripts=False)
        require(tuple(bpy.data.version[:2]) <= tuple(bpy.app.version[:2]), 'Future source file refused')
        if mode == 'apply':
            op = contract.canonical_operation(support.read(config['operation']))
            selection, patch = op['selection'], op['patch']
            report.update(operation=op, operation_descriptor=config['operation'])
        else:
            selection = contract.canonical_selection(support.read(config['selection']))
        support.select_context(selection['context'], frames=True)
    rig = bpy.data.objects[selection['rig']]
    actual_context = {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                      'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe}
    report.update(rig=rig.name, context=actual_context, selection=selection)
    external = support.paths()
    if op:
        report['external_input_validation'] = support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], external)
    before = state.capture(selection)
    before_summary = state.summarize(before)
    evaluation_before = evaluator.evaluation(rig, support)
    report.update(before_snapshot=before_summary, external_paths_before=external,
                  evaluation_before=evaluation_before, runtime_extra_users_before=support.runtime_extra_users())

    def inspect():
        observed = core.inspect_settings(bpy.context, selection)
        require(observed['ok'], 'Settings inspection refused: ' + str(observed.get('error')))
        return observed

    def stable(observed):
        # Runtime cache information is reported, never folded into author identity.
        return {k: v for k, v in observed.items() if k != 'derived_cache'}

    def values(observed):
        return [r['value'] for r in observed['fields']]

    def request(observed, desired):
        return {'schema': contract.REQUEST_SCHEMA, 'expected_snapshot_sha256': observed['snapshot_sha256'],
                'expected_definition_sha256': observed['definition_sha256'], 'changes': [
                    {'key': r['key'], 'before': {'type': 'int', 'value': r['value']},
                     'after': {'type': 'int', 'value': value}} for r, value in zip(observed['fields'], desired)]}

    def roundtrip(original_values, accepted, accepted_state, accepted_evaluation):
        steps = []
        for desired in (original_values, values(accepted)):
            current = inspect()
            answer = core.apply_settings(bpy.context, selection, request(current, desired))
            require(answer['ok'], 'Roundtrip failed: ' + str(answer))
            observed, raw = inspect(), state.capture(selection)
            require(observed['definition'] == accepted['definition'], 'Roundtrip changed immutable definition')
            comparison = state.compare_patch(accepted_state, raw, selection, values(accepted), desired)
            require(comparison['ok'], 'Roundtrip changed non-target declared storage: ' + str(comparison))
            evaluation_comparison = evaluator.compare_evaluation(accepted_evaluation, evaluator.evaluation(rig, support))
            steps.append({'desired': desired, 'actual': observed['fields'], 'effects': observed['effects'],
                          'authoring_protection': comparison, 'evaluation_comparison': evaluation_comparison})
        current = inspect()
        original_writer, original_refresh = core._write_property, core._refresh
        calls = {'setter': 0, 'refresh': 0}
        def no_op_writer(*args):
            calls['setter'] += 1
            return original_writer(*args)
        def no_op_refresh(*args):
            calls['refresh'] += 1
            return original_refresh(*args)
        core._write_property, core._refresh = no_op_writer, no_op_refresh
        try:
            no_op = core.apply_settings(bpy.context, selection, request(current, values(current)))
        finally:
            core._write_property, core._refresh = original_writer, original_refresh
        require(no_op['ok'] and no_op['changed'] is False and not no_op['writes']
                and calls == {'setter': 0, 'refresh': 0}, 'Same-value operation must do no writes/refresh')
        require(stable(inspect()) == stable(accepted) and state.capture(selection) == accepted_state, 'Roundtrip final author state differs')
        return {'ok': True, 'steps': steps, 'no_op': {'ok': True, 'changed': False, 'writes': [], 'native_seam_calls': calls}}

    if mode == 'cold':
        require(before_summary == producer['after_snapshot'], 'Cold full declared author storage differs: ' + str(state.diff(producer['after_snapshot'], before_summary)))
        require(external == producer['external_paths_after'], 'Cold native static inputs differ')
        comparison = evaluator.compare_evaluation(producer['evaluation_after'], evaluation_before)
        report.update(operation_ok=producer['operation_ok'], after_snapshot=before_summary,
                      evaluation_comparison=comparison, evaluation_after=evaluation_before)
        if producer['operation_ok']:
            current = inspect()
            require(stable(current) == producer['settings_validation'], 'Cold native values/UI/definitions/flags differ')
            report['settings_validation'] = stable(current)
            report['derived_cache_observation'] = current['derived_cache']
            report['roundtrip'] = roundtrip(producer['original_values'], current, before, evaluation_before)
        else:
            require(manifest is not None, 'Rejected candidate only allowed for explicit fixtures')
            validation = fixture.validate(manifest, producer['fixture_before'], fixture.capture(manifest), producer['result'])
            require(validation['ok'], 'Independent cold fixture refusal/recovery mismatch: ' + str(validation))
            report['fixture_validation'] = validation
        require(state.capture(selection) == before, 'Cold modified author state')
        report.update(status='pass', source_unchanged=True)
    elif mode == 'inspect':
        observed = inspect()
        require(state.capture(selection) == before, 'Read-only inspection changed declared author state')
        report.update(status='pass', source_unchanged=True, inspection=stable(observed),
                      derived_cache_observation=observed['derived_cache'], after_snapshot=before_summary)
    else:
        fixture_before = fixture.capture(manifest) if manifest else None
        # The injection wrapper executes the actual native write/refresh first.
        write_original, refresh_original = core._write_property, core._refresh
        writes, injection = [], manifest.get('inject_rollback') if manifest else None
        def writer(pb, key, value):
            native_before = pb[key]
            write_original(pb, key, value)
            writes.append({'key': key, 'before': native_before, 'requested': value, 'actual': pb[key]})
            if (injection == 'first' and len(writes) == 1) or (injection == 'second' and len(writes) == 2):
                raise RuntimeError('settings fixture actual-write failure: ' + injection)
        def refresh(target_rig, context):
            refresh_original(target_rig, context)
            if injection == 'refresh':
                raise RuntimeError('settings fixture actual-refresh failure')
        core._write_property, core._refresh = writer, refresh
        try:
            answer = core.apply_settings(bpy.context, selection, patch)
        finally:
            core._write_property, core._refresh = write_original, refresh_original
        report.update(result=answer, operation_ok=answer['ok'], request=patch, setter_observation=writes)
        after = state.capture(selection)
        if injection:
            expected_count = 1 if injection == 'first' else 2
            require(not answer['ok'] and len(writes) == expected_count and answer['rollback']['ok']
                    and all(type(w['actual']) is int and w['actual'] == w['requested'] and w['before'] != w['actual'] for w in writes),
                    'Recovery fixture must prove actual completed writes and exact successful rollback')
            report['rollback_injection'] = {'triggered': True, 'stage': injection, 'completed_writes': len(writes), 'writes': writes}
        elif not answer['ok']:
            require(not writes, 'Preflight refusal made a native property write')
        if manifest:
            validation = fixture.validate(manifest, fixture_before, fixture.capture(manifest), answer)
            report.update(manifest=manifest, fixture_before=fixture_before, fixture_validation=validation)
            require(validation['ok'], 'Native independent fixture validation failed: ' + str(validation))
        else:
            require(answer['ok'], 'Settings apply refused: ' + str(answer.get('error')))
        if answer['ok']:
            accepted = inspect()
            original_values = [r['before']['value'] for r in patch['changes']]
            desired = [r['after']['value'] for r in patch['changes']]
            require(values(accepted) == desired, 'Native post-write typed values differ')
            protection = state.compare_patch(before, after, selection, original_values, desired)
            require(protection['ok'], 'Non-target declared author state changed: ' + str(protection))
            report.update(authoring_protection=protection, settings_validation=stable(accepted),
                          derived_cache_observation=accepted['derived_cache'], original_values=original_values)
        else:
            require(after == before, 'Rejected/rolled-back settings changed full declared author state')
        evaluation_after = evaluator.evaluation(rig, support)
        comparison = evaluator.compare_evaluation(evaluation_before, evaluation_after)
        report.update(evaluation_comparison=comparison, evaluation_after=evaluation_after)
        if answer['ok']:
            report['roundtrip'] = roundtrip(original_values, accepted, after, evaluation_after)
        require(support.paths() == external, 'Native static dependencies changed')
        runtime_before_save = support.runtime_extra_users()
        writer_result = persistence.save_candidate(job/'output-candidate.blend', job)
        require(state.capture(selection) == after and support.runtime_extra_users() == runtime_before_save, 'Persistence changed declared in-memory author storage/runtime users')
        report.update(status='candidate_waiting_independent_cold', candidate={k: writer_result[k] for k in ('file', 'expected_sha256')},
                      writer=writer_result, after_snapshot=state.summarize(after), external_paths_after=support.paths())
    for item in config['implementation']:
        support.checked(item)
    for name in ('input', 'operation', 'selection', 'producer_report'):
        if name in config:
            support.checked(config[name])
    if op:
        support.validate_native_inputs(op['external_inputs'], op['inherited_missing_weak_references'], support.paths())
    require(support.paths() == external, 'External dependency set changed')
    support.dump(job/'output-settings.json', report)
    return {'status': report['status'], 'mode': mode, 'operation_ok': report.get('operation_ok'),
            'source_unchanged': report.get('source_unchanged'), 'GUI': 'deferred', 'FPS': 'not_measured'}


if __name__ == '__main__':
    try:
        result = run(parameters, Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
