"""Pinned fixed-baseline producer/cold worker for two independent hair branches."""
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import traceback
from array import array

sys.dont_write_bytecode = True
AUTHOR = '4e744bb99b3e2706656dc183565d7d8839225762504bf464704508fdef3f14d7'
SOURCE = '216f4c0a8fa919d538ca95c45e3dea2d4157974ee5c5036d0f99885e6c9a10bd'
REPORT_LIMIT = 8 * 1024 * 1024
MESHES = {'front': '千早爱音- 赋权前发网格', 'rear': '千早爱音- 赋权后发网格'}
BOUNDARIES = ('根骨骼', '机制-头部FK-跟随', '头颅控制器', '头颅', '形变-头颅', '骨架设置')


def run(config, directory):
    import bpy
    assert set(config) == {'phase', 'source', 'input', 'implementation', 'external_inputs', 'addon_root', 'producer'}
    assert config['phase'] in ('producer', 'cold')
    assert config['source']['expected_sha256'] == SOURCE and len(config['external_inputs']) == 17
    assert bpy.app.version == (5, 2, 1) and bpy.app.build_hash.decode() == '9e2066aef7ef'
    assert not bpy.context.preferences.filepaths.use_scripts_auto_execute
    resources = [config['source'], config['input'], *config['implementation'], *config['external_inputs']]
    if config['producer'] is not None:
        resources.append(config['producer'])

    def verify():
        for descriptor in resources:
            assert hashlib.sha256(Path(descriptor['file']).read_bytes()).hexdigest() == descriptor['expected_sha256'], descriptor['file']

    verify()
    for name in ('character_state.py', 'inspection.py'):
        descriptors = [d for d in config['implementation'] if Path(d['file']).name == name]
        assert len(descriptors) == 1
        sys.path.insert(0, str(Path(descriptors[0]['file']).parent))
    sys.path.insert(0, config['addon_root'])
    import character_state as C
    from humanoid_rigging_probe import hair_pair_isolation as HP, subdivision_diagnostic as S
    expected_names = {part: HP.bone_names(part) for part in MESHES}
    assert tuple(HP.BOUNDARIES) == BOUNDARIES

    def loaded_implementation():
        records = []
        for name, module in tuple(sys.modules.items()):
            if name in ('character_state', 'inspection', 'protocol') or name == 'humanoid_rigging_probe' or name.startswith('humanoid_rigging_probe.'):
                actual = Path(module.__file__).resolve()
                matches = [d for d in config['implementation'] if Path(d['file']).resolve() == actual]
                assert len(matches) == 1, ('Loaded module outside pinned closure', name, str(actual))
                assert hashlib.sha256(actual.read_bytes()).hexdigest() == matches[0]['expected_sha256']
                records.append({'module': name, **matches[0]})
        return records

    loaded_before = loaded_implementation()
    closure = {Path(d['file']).resolve() for d in config['implementation']}
    assert set(Path(config['addon_root'], 'humanoid_rigging_probe').glob('*.py')) <= closure
    bpy.ops.wm.open_mainfile(filepath=config['input']['file'], use_scripts=False, load_ui=False)
    scene = bpy.data.scenes['Scene']
    layer = scene.view_layers['ViewLayer']
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    assert scene.frame_current == 0 and scene.frame_subframe == 0 and bpy.context.mode == 'OBJECT'
    S._handler_policy(bpy)
    S._cache_policy(scene, list(range(1, 49)))
    rig = bpy.data.objects['千早爱音-素体骨架']
    meshes = {part: bpy.data.objects[name] for part, name in MESHES.items()}
    assert rig.type == 'ARMATURE' and len(rig.data.bones) == 1733
    assert all(mesh.type == 'MESH' and mesh.modifiers[1].type == 'ARMATURE' for mesh in meshes.values())

    def resource_paths():
        rows = []
        for collection in ('images', 'libraries', 'sounds', 'fonts', 'movieclips', 'cache_files', 'volumes'):
            for item in getattr(bpy.data, collection):
                raw = item.filepath
                library = None if collection == 'libraries' else item.library
                resolved_path = C.inspection.path_value(raw, library) if raw and raw != '<builtin>' else raw
                rows.append({'collection': collection, 'name': item.name, 'raw_path': raw,
                             'resolved_path': resolved_path,
                             'packed': bool(getattr(item, 'packed_file', None)) or bool(getattr(item, 'packed_files', ()))})
        for item in C.inspection.all_ids():
            weak = getattr(item, 'library_weak_reference', None)
            if weak is not None:
                raw = weak.filepath
                rows.append({'collection': 'weak_library_reference',
                             'name': item.bl_rna.identifier + ':' + item.name, 'raw_path': raw,
                             'resolved_path': C.inspection.path_value(raw, item.library) if raw else raw,
                             'id_name': weak.id_name})
        return sorted(rows, key=lambda row: (row['collection'], row['name']))

    def resolved(rows):
        return [{key: value for key, value in row.items() if key != 'raw_path'} for row in rows]

    def observe():
        graph = bpy.context.evaluated_depsgraph_get()
        result = {}
        for part, mesh in meshes.items():
            evaluated = mesh.evaluated_get(graph)
            assert evaluated.is_evaluated and evaluated.original == mesh and evaluated.type == 'MESH'
            coordinates = array('f', [0.0]) * (3 * len(evaluated.data.vertices))
            evaluated.data.vertices.foreach_get('co', coordinates)
            matrix = [list(row) for row in evaluated.matrix_world]
            assert len(coordinates) > 0 and all(math.isfinite(value) for value in coordinates)
            assert all(math.isfinite(value) for row in matrix for value in row)
            result[part] = {'object': mesh.name, 'vertices': len(evaluated.data.vertices),
                            'coordinates': list(coordinates), 'matrix_world': matrix,
                            'topology': C.digest([list(polygon.vertices) for polygon in evaluated.data.polygons])}
        return result

    def compare(before, after):
        assert set(before) == set(after) == set(MESHES)
        comparison = {}
        for part in MESHES:
            left, right = before[part], after[part]
            assert left['object'] == right['object'] == MESHES[part]
            assert left['vertices'] == right['vertices'] > 0 and left['topology'] == right['topology']
            assert left['matrix_world'] == right['matrix_world']
            a, b = left['coordinates'], right['coordinates']
            assert len(a) == len(b) == left['vertices'] * 3
            assert all(math.isfinite(value) for value in a + b)
            matrix = left['matrix_world']
            assert all(math.isfinite(value) for row in matrix for value in row)
            component_error = max(abs(x-y) for x, y in zip(a, b))
            maximum_squared = 0.0
            for offset in range(0, len(a), 3):
                delta = [a[offset+i] - b[offset+i] for i in range(3)]
                world_delta = [sum(matrix[row][column] * delta[column] for column in range(3)) for row in range(3)]
                assert all(math.isfinite(value) for value in world_delta)
                maximum_squared = max(maximum_squared, sum(value*value for value in world_delta))
            world_error = math.sqrt(maximum_squared)
            assert math.isfinite(component_error) and math.isfinite(world_error)
            assert component_error <= 1e-6 and world_error <= 1e-6, (part, component_error, world_error)
            comparison[part] = {'vertices': left['vertices'], 'topology_exact': True,
                                'object_world_exact': True, 'all_finite': True,
                                'local_max_component_error': component_error, 'world_max_distance': world_error}
        return comparison

    def validate_branch(part, clone, data):
        names = expected_names[part]
        count = 245 if part == 'front' else 305
        assert clone.name == 'HRIG_' + part + '_hair' and data.name == 'HRIG_' + part + '_hair_data'
        assert clone.type == 'ARMATURE' and clone.data == data and data.users == 1
        assert clone.library is None and data.library is None
        assert meshes[part].modifiers[1].object == clone and set(data.bones.keys()) == names and len(names) == count
        assert clone.animation_data is not None and clone.animation_data.action is None
        assert not list(clone.animation_data.nla_tracks) and len(clone.animation_data.drivers) == 14
        for bone in data.bones:
            original = rig.data.bones[bone.name]
            assert bone.matrix_local == original.matrix_local, (part, 'rest', bone.name)
            assert (bone.parent.name if bone.parent else None) == (original.parent.name if original.parent else None), (part, 'parent', bone.name)
            assert bone.bbone_segments == 1 and bone.bbone_custom_handle_start is None and bone.bbone_custom_handle_end is None
            target = clone.pose.bones[bone.name].custom_shape_transform
            assert target is None or (target.id_data == clone and target.name in names
                                      and target.as_pointer() == clone.pose.bones[target.name].as_pointer())
        for name in BOUNDARIES:
            constraints = clone.pose.bones[name].constraints
            assert len(constraints) == 1
            boundary = constraints[0]
            assert boundary.name == 'HRIG original head boundary' and boundary.type == 'COPY_TRANSFORMS'
            assert boundary.target == rig and boundary.subtarget == name and not boundary.mute and boundary.influence == 1.0
            assert boundary.owner_space == boundary.target_space == 'WORLD' and boundary.mix_mode == 'REPLACE'
        for pose in clone.pose.bones:
            for constraint in pose.constraints:
                assert constraint.type != 'ARMATURE'
                for field, subfield in (('target', 'subtarget'), ('pole_target', 'pole_subtarget'), ('space_object', 'space_subtarget')):
                    target = getattr(constraint, field, None)
                    subtarget = getattr(constraint, subfield, '')
                    if target == clone:
                        assert subtarget and subtarget in names
                    elif target == rig:
                        assert pose.name in BOUNDARIES and constraint.name == 'HRIG original head boundary'
                    elif target is not None:
                        assert target.name in ('千早爱音-前发物理', '千早爱音-后发物理')
        for curve in clone.animation_data.drivers:
            owner = re.match(r'^pose\.bones\["([^"]+)"\]', curve.data_path)
            assert owner and owner.group(1) in names and owner.group(1) not in BOUNDARIES
            clone.path_resolve(curve.data_path)
            assert len(curve.driver.variables) > 0
            for variable in curve.driver.variables:
                assert variable.type == 'SINGLE_PROP'
                for target in variable.targets:
                    assert target.id == rig and target.data_path == 'pose.bones["骨架设置"]["头发FK-IK"]'
        evaluated = clone.evaluated_get(bpy.context.evaluated_depsgraph_get())
        assert evaluated.is_evaluated and evaluated.original == clone
        assert all(curve.driver.is_valid for curve in evaluated.animation_data.drivers)
        assert all(constraint.is_valid for pose in evaluated.pose.bones for constraint in pose.constraints)
        return {'object': clone.name, 'data': data.name, 'bone_count': count, 'driver_count': 14,
                'boundary_count': 6, 'rest_and_parents_exact': True, 'driver_settings_targets_exact': True,
                'constraint_targets_valid': True}

    report = {'schema': 'hrig.hair-pair-candidate.v1', 'phase': config['phase'], 'status': 'running',
              'source': config['source'], 'loaded_implementation': loaded_before,
              'source_saved': False, 'frame_set_called': False,
              'scope': 'Fixed baseline5 experimental front/side and rear branches; original 1733 bones, body, Action and proxies retained; no generic/FPS/full-ROM/collision claim.'}
    owned = {}
    try:
        if config['phase'] == 'producer':
            assert config['producer'] is None and config['input'] == config['source'] and C.digest(C.capture()) == AUTHOR
            assert all(mesh.modifiers[1].object == rig for mesh in meshes.values())
            report['source_resource_paths'] = resource_paths()
            assert len(report['source_resource_paths']) == 50
            baseline = observe()
            report['mapping'] = {}
            for part, mesh in meshes.items():
                clone, data, mapping = HP.create(rig, mesh, scene, layer, part)
                owned[part] = (clone, data)
                report['mapping'][part] = mapping
            report['branches'] = {part: validate_branch(part, *owned[part]) for part in MESHES}
            report['neutral_comparison'] = compare(baseline, observe())
            output = Path(directory, 'output-candidate.blend')
            assert not output.exists() and output.resolve() != Path(config['source']['file']).resolve()
            bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=True, relative_remap=True)
            report['candidate_resource_paths'] = resource_paths()
            assert resolved(report['source_resource_paths']) == resolved(report['candidate_resource_paths']), 'Saved resource targets changed'
            report['candidate_author_sha256'] = C.digest(C.capture())
            report['candidate_geometry'] = observe()
            report['candidate_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
        else:
            # Both objects are owned by this pinned producer candidate, never an arbitrary user session.
            for part in MESHES:
                clone = bpy.data.objects.get('HRIG_' + part + '_hair')
                if clone is not None and clone.type == 'ARMATURE':
                    owned[part] = (clone, clone.data)
            producer = json.loads(Path(config['producer']['file']).read_text(encoding='utf-8'))
            assert producer['schema'] == 'hrig.hair-pair-candidate.v1' and producer['status'] == 'pass'
            assert producer['phase'] == 'producer' and producer['source'] == config['source']
            assert config['input']['expected_sha256'] == producer['candidate_sha256']
            assert C.digest(C.capture()) == producer['candidate_author_sha256'], 'Saved author state mismatch'
            assert set(owned) == set(MESHES)
            report['source_resource_paths'] = producer['source_resource_paths']
            report['candidate_resource_paths'] = resource_paths()
            assert len(report['source_resource_paths']) == 50
            assert resolved(report['candidate_resource_paths']) == resolved(report['source_resource_paths']), 'Cold resource targets changed'
            report['branches'] = {part: validate_branch(part, *owned[part]) for part in MESHES}
            report['candidate_geometry'] = observe()
            report['neutral_comparison'] = compare(producer['candidate_geometry'], report['candidate_geometry'])
            report['candidate_author_sha256'] = producer['candidate_author_sha256']
            report['candidate_sha256'] = config['input']['expected_sha256']
        report['resource_record_count'] = len(report['source_resource_paths'])
        report['resource_targets_sha256'] = C.digest(resolved(report['source_resource_paths']))
        report['resource_targets_preserved'] = True
        report['loaded_implementation'] = loaded_implementation()
        report['status'] = 'pass'
    except Exception as exc:
        report.update(status='fail', error={'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
    finally:
        restoration_errors = []
        for part, mesh in meshes.items():
            try:
                assert mesh.modifiers[1].type == 'ARMATURE'
                mesh.modifiers[1].object = rig
            except Exception:
                restoration_errors.append({'part': part, 'phase': 'restore_modifier_pointer', 'traceback': traceback.format_exc()})
        for part, (clone, data) in reversed(list(owned.items())):
            try:
                assert clone.name == 'HRIG_' + part + '_hair' and data.name == 'HRIG_' + part + '_hair_data'
                bpy.data.objects.remove(clone, do_unlink=True)
                assert data.users == 0
                bpy.data.armatures.remove(data)
            except Exception:
                restoration_errors.append({'part': part, 'phase': 'remove_owned_branch', 'traceback': traceback.format_exc()})
        try:
            layer.update()
            report['original_author_restored_sha256'] = C.digest(C.capture())
            report['original_author_exact'] = report['original_author_restored_sha256'] == AUTHOR
            if not report['original_author_exact']:
                report['status'] = 'fail'
        except Exception:
            report['status'] = 'fail'
            report['inverse_error'] = traceback.format_exc()
        if restoration_errors:
            report.update(status='fail', restoration_errors=restoration_errors)
        try:
            verify()
            report['resources_unchanged'] = True
            report['loaded_implementation'] = loaded_implementation()
        except Exception:
            report.update(status='fail', resource_or_implementation_error=traceback.format_exc())
        raw = json.dumps(report, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        assert len(raw) <= REPORT_LIMIT, 'Validation report exceeds 8 MiB'
        Path(directory, 'output-validation.json').write_bytes(raw)
    return {'status': report['status'], 'source_saved': False}


if __name__ == '__main__':
    try:
        result = run(parameters, job_directory)
    except Exception:
        Path(job_directory, 'preflight-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
