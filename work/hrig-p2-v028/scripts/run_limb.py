"""Independent native output, skin, stored-state and rejection limb oracle.

Calls public addon functions only; native observations do not use core-private
helpers. Producer status stays provisional until a paired independent cold job.
"""
import hashlib
import json
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

TOLERANCE = 2e-4
NEGATIVE_CODES = {
    'wrong_source': 'WRONG_SOURCE_MODE',
    'external_driver': 'EXTERNAL_DRIVER_CONSUMER',
    'empty_witness': 'WITNESS_GEOMETRY_CHANGED',
    'changed_weight': 'WITNESS_CHANGED',
    'modifier_disabled': 'WITNESS_CHANGED',
    'quaternion_w_lock': 'LOCKED_MODULE',
    'arbitrary_fk_twist': 'IK_UNREPRESENTABLE_FK',
    'duplicate_module': 'MODULE_EXISTS',
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    Path(path).write_text(encoded, encoding='utf-8')


def plain(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict) or hasattr(value, 'items'):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted(plain(v) for v in value)
    return [plain(v) for v in value]


def props(owner):
    try:
        keys = sorted(owner.keys())
    except TypeError:  # Some native bpy_struct kinds do not support IDProperties.
        return {}
    return {key: plain(owner[key]) for key in keys}


def rna(owner):
    """Writable native scalar/array/pointer fields, excluding final pose caches."""
    result = {}
    excluded = {'rna_type', 'tag'}  # ID.tag is a transient native work flag.
    if isinstance(owner, bpy.types.PoseBone):
        excluded |= {'matrix', 'matrix_channel'}
    for prop in owner.bl_rna.properties:
        key = prop.identifier
        if key in excluded or prop.is_readonly or prop.type == 'COLLECTION':
            continue
        value = getattr(owner, key)
        if prop.type == 'POINTER':
            result[key] = None if value is None else {
                'rna': value.bl_rna.identifier, 'name': getattr(value, 'name', None)}
        else:
            result[key] = plain(value)
    result['custom_properties'] = props(owner)
    return result


def animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    result = {'rna': rna(ad), 'drivers': [], 'action': None,
              'nla': [{'rna': rna(track), 'strips': [rna(s) for s in track.strips]}
                      for track in ad.nla_tracks]}
    for curve in ad.drivers:
        result['drivers'].append({
            'fcurve': rna(curve), 'driver': rna(curve.driver),
            'variables': [{'rna': rna(var), 'targets': [rna(t) for t in var.targets]}
                          for var in curve.driver.variables],
            'modifiers': [rna(m) for m in curve.modifiers]})
    if ad.action:
        action, layers = ad.action, []
        for layer in action.layers:
            strips = []
            for strip in layer.strips:
                bags = []
                for slot in action.slots:
                    bag = strip.channelbag(slot, ensure=False)
                    if bag is not None:
                        bags.append({'slot': slot.identifier, 'curves': [
                            {'rna': rna(fc), 'keys': [rna(k) for k in fc.keyframe_points],
                             'modifiers': [rna(m) for m in fc.modifiers]}
                            for fc in bag.fcurves]})
                strips.append({'type': strip.type, 'bags': bags})
            layers.append({'name': layer.name, 'strips': strips})
        result['action'] = {'rna': rna(action), 'layers': layers,
                            'slots': [rna(s) for s in action.slots]}
    return result


def refresh():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def checked_matrix(value):
    if not isinstance(value, list) or len(value) != 4:
        raise RuntimeError('Oracle requires a 4x4 matrix')
    if any(not isinstance(row, list) or len(row) != 4 for row in value):
        raise RuntimeError('Oracle matrix row shape differs')
    if any(type(v) not in (int, float) or not math.isfinite(v) for row in value for v in row):
        raise RuntimeError('Oracle matrix must be finite numeric data')
    return value


def checked_sample(value):
    for key in ('world', 'module_world'):
        if not isinstance(value.get(key), dict) or not value[key]:
            raise RuntimeError('Oracle missing named bone matrices')
        for name, matrix in value[key].items():
            if not isinstance(name, str) or not name:
                raise RuntimeError('Oracle invalid bone identity')
            checked_matrix(matrix)
    if not set(value['world']).issubset(value['module_world']):
        raise RuntimeError('Output bone names are not module bone names')
    if any(value['world'][name] != value['module_world'][name] for name in value['world']):
        raise RuntimeError('Output and module matrices disagree')
    if not isinstance(value.get('ik_basis'), dict) or not value['ik_basis']:
        raise RuntimeError('Oracle missing named IK basis matrices')
    for matrix in value['ik_basis'].values():
        checked_matrix(matrix)
    for key in ('selector', 'pole_angle'):
        if type(value.get(key)) not in (int, float) or not math.isfinite(value[key]):
            raise RuntimeError('Oracle selector/pole angle must be finite')
    positions = value.get('mesh_world')
    if (not isinstance(positions, list) or type(value.get('mesh_count')) is not int
            or value['mesh_count'] != len(positions)):
        raise RuntimeError('Oracle mesh count/shape differs')
    for point in positions:
        if (not isinstance(point, list) or len(point) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
            raise RuntimeError('Oracle mesh point must be finite xyz')
    return value


def sample(manifest):
    """Read every native bone and any witness size, including empty rejection."""
    graph = refresh()
    rig = bpy.data.objects[manifest['rig']]
    evaluated = rig.evaluated_get(graph)
    mesh = bpy.data.objects[manifest['mesh']].evaluated_get(graph)
    if not evaluated.is_evaluated or not mesh.is_evaluated:
        raise RuntimeError('Oracle requires real evaluated IDs')
    expected = set(sum((manifest['bones'][key] for key in ('FK', 'IK', 'DEF')), [])
                   + [manifest['bones'][key] for key in ('target', 'pole', 'settings')])
    if set(evaluated.pose.bones.keys()) != expected:
        raise RuntimeError('Oracle native bone names differ from declared module')
    matrices = {pb.name: plain(evaluated.matrix_world @ pb.matrix)
                for pb in evaluated.pose.bones}
    positions = [plain(mesh.matrix_world @ v.co) for v in mesh.data.vertices]
    return checked_sample({
        'world': {name: matrices[name] for name in manifest['bones']['DEF']},
        'module_world': matrices, 'mesh_world': positions, 'mesh_count': len(positions),
        'selector': rig.pose.bones[manifest['bones']['settings']]['ik_fk'],
        'ik_basis': {name: plain(rig.pose.bones[name].matrix_basis)
                     for name in manifest['bones']['IK']},
        'pole_angle': rig.pose.bones[manifest['bones']['IK'][1]].constraints[
            manifest['ik_constraint']].pole_angle})


def inventory():
    # Native module ID collections; UI screens/workspaces are intentionally
    # outside this headless artifact contract, including during load_ui=False.
    names = ('objects', 'armatures', 'meshes', 'actions', 'materials',
             'shape_keys', 'node_groups', 'texts', 'collections', 'scenes', 'worlds')
    return {key: sorted(x.name for x in getattr(bpy.data, key)) for key in names}


def capture(manifest, extra_names=()):
    """Complete generated module state, captured after hostile injection."""
    observed, graph = sample(manifest), refresh()
    objects, extra_evaluated = {}, {}
    for name in [manifest['rig'], manifest['mesh']] + list(extra_names):
        obj = bpy.data.objects[name]
        state = {'rna': rna(obj), 'matrix_basis': plain(obj.matrix_basis),
                 'matrix_world': plain(obj.matrix_world),
                 'matrix_parent_inverse': plain(obj.matrix_parent_inverse),
                 'constraints': [rna(c) for c in obj.constraints],
                 'modifiers': [rna(m) for m in obj.modifiers],
                 'animation': animation(obj), 'data_name': obj.data.name if obj.data else None}
        if obj.type == 'ARMATURE':
            state['armature'] = rna(obj.data)
            state['rest'] = {b.name: {'rna': rna(b), 'matrix_local': plain(b.matrix_local),
                                      'head_local': plain(b.head_local), 'tail_local': plain(b.tail_local),
                                      'parent': b.parent.name if b.parent else None,
                                      'collections': sorted(c.name for c in b.collections)}
                             for b in obj.data.bones}
            state['pose_solver'] = obj.pose.ik_solver
            state['pose'] = {pb.name: {'rna': rna(pb), 'matrix_basis': plain(pb.matrix_basis),
                                      'constraints': [rna(c) for c in pb.constraints]}
                             for pb in obj.pose.bones}
        if obj.type == 'MESH':
            state['mesh'] = {
                'rna': rna(obj.data), 'vertices': [plain(v.co) for v in obj.data.vertices],
                'edges': [list(e.vertices) for e in obj.data.edges],
                'polygons': [list(p.vertices) for p in obj.data.polygons],
                'groups': [rna(g) for g in obj.vertex_groups],
                'weights': [[[g.group, g.weight] for g in v.groups] for v in obj.data.vertices],
                'materials': [m.name if m else None for m in obj.data.materials], 'shape_keys': None}
            if obj.data.shape_keys:
                state['mesh']['shape_keys'] = [
                    {'rna': rna(key), 'coords': [plain(v.co) for v in key.data]}
                    for key in obj.data.shape_keys.key_blocks]
        objects[name] = state
        if name in extra_names:
            extra_evaluated[name] = checked_matrix(plain(obj.evaluated_get(graph).matrix_world))
    return {'stored': {'objects': objects, 'inventory': inventory()},
            'evaluated': observed, 'extra_evaluated': extra_evaluated}


def matrix_error(a, b):
    checked_matrix(a)
    checked_matrix(b)
    return max(abs(a[r][c] - b[r][c]) for r in range(4) for c in range(4))


def compare(first, second, all_bones=False):
    checked_sample(first)
    checked_sample(second)
    key = 'module_world' if all_bones else 'world'
    if (set(first[key]) != set(second[key])
            or len(first['mesh_world']) != len(second['mesh_world'])):
        raise RuntimeError('Independent oracle output identity/count differs')
    world = max((matrix_error(first[key][name], second[key][name]) for name in first[key]), default=0.0)
    mesh = max(((Vector(first['mesh_world'][i]) - Vector(second['mesh_world'][i])).length
                for i in range(len(first['mesh_world']))), default=0.0)
    if not math.isfinite(world) or not math.isfinite(mesh) or world > TOLERANCE or mesh > TOLERANCE:
        raise RuntimeError('Independent output oracle failed: ' + repr((world, mesh)))
    return {'world_matrix_max_abs': world, 'mesh_world_max_distance': mesh,
            'mesh_count': len(first['mesh_world']), 'all_module_bones': all_bones, 'tolerance': TOLERANCE}


def compare_capture(first, second):
    if first['stored'] != second['stored']:
        raise RuntimeError('Independent full native module storage changed')
    result = compare(first['evaluated'], second['evaluated'], all_bones=True)
    if set(first['extra_evaluated']) != set(second['extra_evaluated']):
        raise RuntimeError('Foreign consumer identity changed')
    extra = max((matrix_error(value, second['extra_evaluated'][name])
                 for name, value in first['extra_evaluated'].items()), default=0.0)
    if extra > TOLERANCE:
        raise RuntimeError('Foreign consumer evaluated transform changed')
    result.update(stored_exact=True, foreign_world_max_abs=extra)
    return result


def setup_pose(config, manifest, control_space):
    rig = bpy.data.objects[manifest['rig']]
    rig.location = (1.1, -.7, .4)
    rig.rotation_mode = 'QUATERNION'
    rig.rotation_quaternion = Euler((.2, -.15, .33)).to_quaternion()
    rig.update_tag()
    refresh()
    poses = (((1.8, -.1, .3), (.3, -.2, .15)),
             ((1.55, .35, .55), (-.21, .27, -.18)),
             ((1.3, -.4, .15), (.17, .31, .29)))
    location, rotation = poses[config.get('pose_index', 0)]
    desired = Euler(rotation).to_matrix().to_4x4()
    desired.translation = Vector((manifest['side'] * location[0], location[1], location[2]))
    move = control_space.match_world(bpy.context, rig, manifest['bones']['target'], plain(rig.matrix_world @ desired))
    if not move['ok']:
        raise RuntimeError('Oracle target setup failed: ' + str(move))
    rig.pose.bones[manifest['bones']['settings']]['ik_fk'] = 1.0
    rig.update_tag()
    bpy.context.scene.frame_set(1)
    return move


def move_inactive_controls(manifest, control_space):
    rig, before, moves = bpy.data.objects[manifest['rig']], sample(manifest), {}
    for key, delta in (('target', (.45, -.35, .4)), ('pole', (-.5, .6, -.3))):
        name = manifest['bones'][key]
        old = Matrix(sample(manifest)['module_world'][name])
        desired = old.copy()
        desired.translation += Vector(delta)
        moved = control_space.match_world(bpy.context, rig, name, plain(desired))
        if not moved['ok']:
            raise RuntimeError('Inactive control perturbation refused: ' + str(moved))
        actual = Matrix(sample(manifest)['module_world'][name])
        distance = (actual.translation - old.translation).length
        if distance < .1:
            raise RuntimeError('Inactive IK control did not actually move')
        moves[key] = {'result': moved, 'world_distance': distance}
    return {'moves': moves, 'output_unchanged': compare(before, sample(manifest))}


def inject_negative(case, manifest, limb):
    rig, mesh = bpy.data.objects[manifest['rig']], bpy.data.objects[manifest['mesh']]
    names, extras = manifest['bones'], []
    if case == 'external_driver':
        foreign = bpy.data.objects.new(manifest['module_id'] + '_ForeignConsumer', None)
        bpy.context.scene.collection.objects.link(foreign)
        fc = foreign.driver_add('location', 0)
        fc.driver.type = 'SCRIPTED'
        var = fc.driver.variables.new()
        var.name, var.type = 'selector', 'SINGLE_PROP'
        var.targets[0].id = rig
        var.targets[0].data_path = rig.pose.bones[names['settings']].path_from_id() + '["ik_fk"]'
        fc.driver.expression = 'selector'
        extras.append(foreign.name)
    elif case == 'empty_witness':
        mesh.data.clear_geometry()
        mesh.data.update()
    elif case == 'changed_weight':
        mesh.vertex_groups[names['DEF'][0]].add([0], .5, 'REPLACE')
    elif case == 'modifier_disabled':
        mesh.modifiers[0].show_viewport = False
    elif case == 'quaternion_w_lock':
        pb = rig.pose.bones[names['FK'][0]]
        pb.lock_rotations_4d, pb.lock_rotation_w = True, True
    elif case == 'arbitrary_fk_twist':
        converted = limb.snap_ik_to_fk(bpy.context, manifest)
        if not converted['ok']:
            raise RuntimeError('Twist fixture preparation refused: ' + str(converted))
        refresh()
        lower_matrix = rig.pose.bones[names['FK'][1]].matrix.copy()
        end_matrix = rig.pose.bones[names['FK'][2]].matrix.copy()
        upper = rig.pose.bones[names['FK'][0]]
        upper.rotation_quaternion = upper.rotation_quaternion @ Quaternion((0, 1, 0), .57)
        rig.update_tag()
        refresh()
        # Native pose assignment retains joint geometry but changes upper roll.
        rig.pose.bones[names['FK'][1]].matrix = lower_matrix
        rig.update_tag()
        refresh()
        rig.pose.bones[names['FK'][2]].matrix = end_matrix
    elif case not in ('wrong_source', 'duplicate_module'):
        raise ValueError('Unknown negative case: ' + case)
    rig.update_tag()
    refresh()
    return extras


def negative_operation(case, manifest, spec, limb):
    if case == 'duplicate_module':
        return limb.create_module(bpy.context, spec)
    if case in ('wrong_source', 'arbitrary_fk_twist'):
        return limb.snap_fk_to_ik(bpy.context, manifest)
    return limb.snap_ik_to_fk(bpy.context, manifest)


def require_rejection(result, case):
    expected = NEGATIVE_CODES[case]
    if result.get('ok') is not False or result.get('error', {}).get('code') != expected:
        raise RuntimeError('Expected refusal ' + expected + ': ' + str(result))
    if case == 'arbitrary_fk_twist':
        if result.get('channels_selector_solver_restored') is not True:
            raise RuntimeError('Twist refusal did not certify rollback')
        errors = (result.get('diagnostics') or {}).get('matrix_errors', {})
        if (not errors or any(not math.isfinite(v) for v in errors.values())
                or max(errors.values()) <= TOLERANCE):
            raise RuntimeError('Twist refusal lacks full-matrix mismatch evidence')


def check_implementation(config):
    expected_addon = Path(__file__).resolve().parents[1] / 'addon'
    addon = Path(config['addon_root']).resolve(strict=True)
    if addon != expected_addon:
        raise RuntimeError('Runner role mismatch: local addon directory required')
    entries = config['implementation']
    paths = [Path(x['file']).resolve(strict=True) for x in entries]
    if len(paths) != len(set(paths)) or set(paths) != set((addon / 'humanoid_rigging_probe').glob('*.py')):
        raise RuntimeError('Limb implementation closure mismatch or duplicate')
    for entry in entries:
        if digest(entry['file']) != entry['expected_sha256']:
            raise RuntimeError('Implementation SHA drift')
    return addon


def run(config, job):
    if tuple(bpy.app.version) != (5, 2, 1) or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise RuntimeError('Requires verified 5.2.1 with embedded autoexec disabled')
    addon = check_implementation(config)
    sys.path.insert(0, str(addon))
    from humanoid_rigging_probe import limb, control_space
    job = Path(job)
    entry_sha = digest(__file__)
    report = {'mode': config['mode'], 'blender': bpy.app.version_string,
              'build': bpy.app.build_hash.decode(), 'GUI': 'deferred_by_user', 'FPS': 'not_measured',
              'scope': 'owned limb prototype, not improved complete Aon', 'status': 'running',
              'implementation': config['implementation'], 'entry_sha': entry_sha}
    dump(job / 'output-limb.json', report)
    if config['mode'] in ('roundtrip', 'negative'):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        report.update(spec=config['spec'], pose_index=config.get('pose_index', 0))
        created = limb.create_module(bpy.context, config['spec'])
        report['creation'] = created
        dump(job / 'output-limb.json', report)
        if not created['ok']:
            raise RuntimeError('Module creation refused')
        manifest = created['manifest']
        report['manifest'] = manifest
        report['pose_setup'] = setup_pose(config, manifest, control_space)
        if config['mode'] == 'roundtrip':
            first = sample(manifest)
            if first['mesh_count'] != 12:
                raise RuntimeError('Positive witness must contain twelve vertices')
            to_fk = limb.snap_ik_to_fk(bpy.context, manifest)
            report.update(initial=first, to_fk=to_fk)
            dump(job / 'output-limb.json', report)
            if not to_fk['ok']:
                raise RuntimeError('IK to FK refused: ' + str(to_fk))
            fk = sample(manifest)
            report['ik_to_fk_oracle'] = compare(first, fk)
            report['inactive_control_perturbation'] = move_inactive_controls(manifest, control_space)
            to_ik = limb.snap_fk_to_ik(bpy.context, manifest)
            report['to_ik'] = to_ik
            dump(job / 'output-limb.json', report)
            if not to_ik['ok']:
                raise RuntimeError('FK to IK refused: ' + str(to_ik))
            final = sample(manifest)
            report['fk_to_ik_oracle'] = compare(fk, final)
            if first['ik_basis'] != final['ik_basis'] or final['pole_angle'] != 0.0:
                raise RuntimeError('Hidden IK basis seed or pole-angle change')
            seeks = []
            for frame in (2, 7, 1, 2):
                bpy.context.scene.frame_set(frame)
                fresh = sample(manifest)
                seeks.append({'frame': frame, 'oracle': compare(final, fresh),
                              'ik_basis_unchanged': fresh['ik_basis'] == first['ik_basis'],
                              'fixed_pole_angle': fresh['pole_angle'] == 0.0})
                if not seeks[-1]['ik_basis_unchanged'] or not seeks[-1]['fixed_pole_angle']:
                    raise RuntimeError('Seek changed fixed IK state')
            report.update(seek_oracles=seeks, next_frame_oracle=seeks[0]['oracle'],
                          after=fresh, final_capture=capture(manifest))
        else:
            case = config['case']
            if case not in NEGATIVE_CODES:
                raise ValueError('Unknown negative case')
            extras = inject_negative(case, manifest, limb)
            report.update(negative_case=case, expected_code=NEGATIVE_CODES[case], extra_names=extras)
            before = capture(manifest, extras)  # Deliberately AFTER hostile injection.
            report['injected_before'] = before
            dump(job / 'output-limb.json', report)
            rejected = negative_operation(case, manifest, config['spec'], limb)
            report['refusal'] = rejected
            after = capture(manifest, extras)
            report.update(final_capture=after, after=after['evaluated'])
            dump(job / 'output-limb.json', report)
            require_rejection(rejected, case)
            report['refusal_oracle'] = compare_capture(before, after)
        dump(job / 'output-manifest.json', manifest)
        bpy.ops.wm.save_as_mainfile(filepath=str(job / 'output-fixture.blend'), copy=True, check_existing=False)
        report['status'] = 'candidate_waiting_independent_cold'
    elif config['mode'] == 'cold':
        for key in ('input', 'producer_report', 'manifest'):
            if digest(config[key]['file']) != config[key]['expected_sha256']:
                raise RuntimeError('Cold input SHA changed')
        producer = json.loads(Path(config['producer_report']['file']).read_text(encoding='utf-8'))
        manifest = json.loads(Path(config['manifest']['file']).read_text(encoding='utf-8'))
        if (producer.get('manifest') != manifest
                or producer.get('status') != 'candidate_waiting_independent_cold'
                or producer.get('mode') not in ('roundtrip', 'negative')
                or producer.get('implementation') != config['implementation']
                or producer.get('entry_sha') != entry_sha):
            raise RuntimeError('Unpaired/unqualified producer or implementation/entry SHA mismatch')
        bpy.ops.wm.open_mainfile(filepath=config['input']['file'], load_ui=False, use_scripts=False)
        extras = producer.get('extra_names', [])
        actual_capture = capture(manifest, extras)
        report.update(manifest=manifest, producer_mode=producer['mode'],
                      after=actual_capture['evaluated'], final_capture=actual_capture,
                      cold_oracle=compare_capture(producer['final_capture'], actual_capture))
        if producer['mode'] == 'roundtrip':
            actual = actual_capture['evaluated']
            if (actual['selector'] != 1.0 or actual['pole_angle'] != 0.0
                    or actual['ik_basis'] != producer['after']['ik_basis']):
                raise RuntimeError('Cold module state differs')
            preflight = limb.snap_fk_to_ik(bpy.context, manifest)
            report['cold_preflight'] = preflight
            require_rejection(preflight, 'wrong_source')
        else:
            case = producer['negative_case']
            report.update(negative_case=case, expected_code=NEGATIVE_CODES[case])
            preflight = negative_operation(case, manifest, producer['spec'], limb)
            report['cold_repeated_refusal'] = preflight
            require_rejection(preflight, case)
        report['cold_refusal_unchanged'] = compare_capture(actual_capture, capture(manifest, extras))
        report['source_unchanged'] = digest(config['input']['file']) == config['input']['expected_sha256']
        if not report['source_unchanged']:
            raise RuntimeError('Cold input changed during validation')
        report['status'] = 'pass'
    else:
        raise ValueError('Unknown limb mode')
    check_implementation(config)
    if digest(__file__) != entry_sha:
        raise RuntimeError('Runner changed during work unit')
    report['implementation_unchanged'] = True
    dump(job / 'output-limb.json', report)
    return {'status': report['status'], 'mode': config['mode'], 'module_id': manifest['module_id'],
            'negative_case': report.get('negative_case'), 'to_fk': report.get('to_fk', {}).get('ok'),
            'to_ik': report.get('to_ik', {}).get('ok'), 'cold_oracle': report.get('cold_oracle'),
            'GUI': 'deferred', 'FPS': 'not_measured'}


if __name__ == '__main__':
    try:
        result = run(parameters, job_directory)
    except Exception as error:
        import traceback
        trace = traceback.format_exc()
        (Path(job_directory) / 'output-debug.txt').write_text(trace, encoding='utf-8')
        report_path = Path(job_directory) / 'output-limb.json'
        failure = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
        failure.update(status='failed', error={'type': type(error).__name__, 'message': str(error)}, traceback=trace)
        dump(report_path, failure)
        raise
