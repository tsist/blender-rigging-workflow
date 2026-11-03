"""Pinned P2 mechanism work unit, hosted by the existing public CLI."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
import bpy


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def nonfinite_markers(value):
    if isinstance(value, dict):
        return 'nonfinite' in value or any(nonfinite_markers(v) for v in value.values())
    if isinstance(value, list):
        return any(nonfinite_markers(v) for v in value)
    return False


def _fixture_module(config):
    spec = importlib.util.spec_from_file_location('_hrig_dependency_fixture', config['fixture_script'])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(config, job):
    if tuple(bpy.app.version) != (5, 2, 1):
        raise RuntimeError('Runtime evidence currently restricted to Blender5.2.1')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise RuntimeError('Automatic embedded Python must remain disabled')
    for item in config['implementation']:
        if digest(item['file']) != item['expected_sha256']:
            raise RuntimeError('Implementation SHA mismatch')
    sys.path.insert(0, config['addon_root'])
    from humanoid_rigging_probe import dependencies
    job = Path(job)
    mode = config['mode']
    report = {'mode': mode, 'blender_version': bpy.app.version_string,
              'build': bpy.app.build_hash.decode(), 'autoexec_enabled': False,
              'source_saved': False, 'FPS_claim': False}
    if mode in {'fixture', 'cold_fixture'}:
        module = _fixture_module(config)
        if mode == 'fixture':
            bpy.ops.wm.read_factory_settings(use_empty=True)
            manifest = module.create_case(config['case'])
        else:
            source = config['input']
            if digest(source['file']) != source['expected_sha256']:
                raise RuntimeError('Fixture input SHA mismatch')
            bpy.ops.wm.open_mainfile(filepath=source['file'], use_scripts=False, load_ui=False)
            manifest = json.loads(Path(config['fixture_manifest']['file']).read_text(encoding='utf-8'))
        observation = module.observe(manifest)
        report.update(manifest=manifest, observation=observation,
                      validation=module.validate(manifest, observation))
        rig_name = manifest['objects'].get('rig')
        report['dependencies'] = dependencies.collect(bpy.context, bpy.data.objects[rig_name]) if rig_name else None
        report['core_snapshot_after_observe'] = dependencies.evaluated_snapshot(
            bpy.context, list(manifest['objects'].values()), list(manifest['bones'].values()))
        if manifest['case'] == 'reference_edges':
            refs = report['dependencies']['unresolved_or_optional_references']
            obj_row = next(row for row in report['dependencies']['objects'] if row['object']['name'] == rig_name)
            checks = {
                'required_target_reported': any(r.get('constraint') == 'RequiredMissingTarget' and r['resolution'] == 'missing_object' for r in refs),
                'optional_target_and_pole_distinguished': sum(r.get('constraint') == 'OptionalTargetlessIK' and r['resolution'] == 'optional_empty' for r in refs) == 2,
                'missing_driver_id_reported': any(r['kind'] == 'driver_input' and r['resolution'] == 'missing_id' for r in refs),
                'nested_values_preserved': obj_row['custom_properties']['settings'] == {'nested': {'gain': .375}, 'mode': 'fixture'}}
            report['validation']['checks'].update(checks)
            report['validation']['status'] = 'pass' if all(report['validation']['checks'].values()) else 'fail'
        write_json(job / 'output-fixture-manifest.json', manifest)
        if mode == 'fixture':
            bpy.context.scene.frame_set(1)
            bpy.ops.wm.save_as_mainfile(filepath=str(job / 'output-fixture.blend'), copy=True, check_existing=False)
        report['fixture_only_persistence'] = mode == 'fixture'
    elif mode in {'dependencies', 'temporal'}:
        source = config['input']
        if digest(source['file']) != source['expected_sha256']:
            raise RuntimeError('Source SHA changed')
        bpy.ops.wm.open_mainfile(filepath=source['file'], use_scripts=False, load_ui=False)
        rig = bpy.data.objects[config['armature']]
        report['dependencies'] = dependencies.collect(bpy.context, rig)
        report['source'] = source
        if mode == 'temporal':
            frames = config['frames']
            if (not isinstance(frames, list) or not frames or len(frames) > 32
                    or any(type(frame) is not int for frame in frames)
                    or any(b != a + 1 for a, b in zip(frames, frames[1:]))):
                raise RuntimeError('Temporal probe requires 1..32 consecutive forward frames')
            names = config['objects']
            if len(names) > 6 or len(config['bones']) > 48:
                raise RuntimeError('Temporal witness budget exceeded')
            starts = [m.point_cache.frame_start for name in names for m in bpy.data.objects[name].modifiers if m.type == 'CLOTH']
            if any(m.point_cache.use_disk_cache for name in names for m in bpy.data.objects[name].modifiers if m.type == 'CLOTH'):
                raise RuntimeError('Source disk-cache writes are outside this memory-only temporal probe')
            if starts and frames[0] != min(starts):
                raise RuntimeError('Cloth temporal protocol must start at cache start')
            overrides = config.get('pose_properties', {})
            if not isinstance(overrides, dict) or sum(len(v) for v in overrides.values()) > 8:
                raise RuntimeError('Pose property override budget exceeded')
            report['temporary_pose_property_changes'] = []
            for bone_name, values in overrides.items():
                bone = rig.pose.bones[bone_name]
                for key, value in values.items():
                    if key not in bone or type(value) not in (int, float, bool) or not isinstance(bone[key], (int,float,bool)):
                        raise RuntimeError('Only explicitly existing numeric pose properties may be overridden')
                    report['temporary_pose_property_changes'].append({'bone': bone_name, 'property': key,
                        'before': bone[key], 'after': value})
                    bone[key] = value
            if overrides:
                rig.update_tag()
            report['temporary_collection_changes'] = []
            def change_collection(layer):
                if layer.name in config.get('include_collections', []):
                    report['temporary_collection_changes'].append({'name': layer.name, 'exclude_before': layer.exclude, 'exclude_after': False})
                    layer.exclude = False
                for child in layer.children:
                    change_collection(child)
            change_collection(bpy.context.view_layer.layer_collection)
            if len(report['temporary_collection_changes']) != len(config.get('include_collections', [])):
                raise RuntimeError('Explicit collection inclusion not matched uniquely')
            report['samples'] = []
            for frame in frames:
                started = time.perf_counter()
                bpy.context.scene.frame_set(frame)
                bpy.context.view_layer.update()
                sample = dependencies.evaluated_snapshot(bpy.context, names, config['bones'])
                sample['background_frame_and_observation_seconds'] = time.perf_counter() - started
                report['samples'].append(sample)
                # Persist bounded progress before the next native solver call.
                write_json(job / 'output-progress.json', {'completed_frame': frame, 'samples': len(report['samples']), 'FPS': 'not_measured'})
                write_json(job / 'output-partial.json', report)
                if (nonfinite_markers(sample) or any(row.get('nonfinite_component_count', 0) for row in sample['objects'])):
                    report['status'] = 'stopped_nonfinite'
                    break
                if any(row.get('geometry_status') == 'budget_exceeded' for row in sample['objects']):
                    report['status'] = 'stopped_geometry_budget'
                    break
        report['source_unchanged'] = digest(source['file']) == source['expected_sha256']
        if not report['source_unchanged']:
            raise RuntimeError('Source file changed during probe')
    elif mode == 'api':
        graph = bpy.context.evaluated_depsgraph_get()
        report['depsgraph_debug_function'] = [(p.identifier, p.type, p.is_required) for p in graph.bl_rna.functions['debug_relations_graphviz'].parameters]
        report['stats'] = graph.debug_stats()
        orphan = bpy.data.objects.new('HRIG_DEP_UnlinkedLookupControl', None)
        candidate = graph.id_eval_get(orphan)
        report['original_fallback_negative_control'] = {
            'unlinked': len(orphan.users_collection) == 0,
            'lookup_returns_original': candidate.as_pointer() == orphan.as_pointer(),
            'is_evaluated': candidate.is_evaluated,
            'core_snapshot': dependencies.evaluated_snapshot(bpy.context, [orphan.name_full])}
        if candidate.is_evaluated or candidate.as_pointer() != orphan.as_pointer():
            raise RuntimeError('Unexpected evaluated lookup for unlinked negative control')
    else:
        raise ValueError('Unknown mechanism mode')
    graph = bpy.context.evaluated_depsgraph_get()
    report['native_graph_stats'] = graph.debug_stats()
    # RNA signature in this build is checked by the api mode; use keywords.
    # The public extension contract accepts .txt; this is Graphviz DOT source
    # text, explicitly labelled rather than adding a shared CLI format change.
    returned = graph.debug_relations_graphviz(filepath=str(job / 'output-depsgraph.txt'))
    if not (job / 'output-depsgraph.txt').is_file() and isinstance(returned, str):
        (job / 'output-depsgraph.txt').write_text(returned, encoding='utf-8')
    report['graph_artifact_format'] = 'Graphviz DOT source text in output-depsgraph.txt'
    for item in config['implementation']:
        if digest(item['file']) != item['expected_sha256']:
            raise RuntimeError('Implementation drift during execution')
    write_json(job / 'output-mechanisms.json', report)
    status = report.get('status', report.get('validation', {}).get('status', 'observed'))
    return {'status': status, 'mode': mode, 'source_unchanged': report.get('source_unchanged'),
            'validation': report.get('validation'), 'native_graph_stats': report['native_graph_stats'],
            'scope': 'bounded P2 evidence; not full binding/FPS/solver certification'}


if __name__ == '__main__':
    try:
        result = run(parameters, job_directory)
    except Exception:
        import traceback
        (Path(job_directory) / 'output-debug.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
