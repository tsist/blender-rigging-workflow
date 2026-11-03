"""Owned native motion fixtures; independent Action and linear-FK/skin oracle.

No production writer, capture, contract canonicalizer or protection mask is used
to compute expected authoring/evaluation results. Persistence and fault injection
belong to the worker. Animation observation leaves its final sampled frame active.
"""
import copy
import math
import struct

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

SCHEMA = 'hrig.motion-clip.fixture.v1'
CASE_IDS = (
    'quaternion_clip', 'driver_only_clip', 'euler_clip',
    'reject_name_collision', 'reject_existing_action', 'reject_nla',
    'reject_snapshot', 'reject_locked', 'reject_writer',
    'rollback_action_created', 'rollback_first_curve', 'rollback_bound',
)
CASES = CASE_IDS
EXPECTED_CODES = {
    'reject_name_collision': 'MOTION_NAME_CONFLICT',
    'reject_existing_action': 'MOTION_ANIMATION_CONFLICT',
    'reject_nla': 'MOTION_ANIMATION_CONFLICT',
    'reject_snapshot': 'SNAPSHOT_CONFLICT',
    'reject_locked': 'MOTION_LOCKED',
    'reject_writer': 'MOTION_WRITER',
    'rollback_action_created': 'MOTION_INVALID',
    'rollback_first_curve': 'MOTION_INVALID',
    'rollback_bound': 'MOTION_INVALID',
}
STAGES = {'rollback_action_created': 'after_action_create',
          'rollback_first_curve': 'after_first_curve', 'rollback_bound': 'after_bind'}
BINDING_FIELDS = ('action', 'action_slot', 'action_slot_handle', 'last_slot_identifier')
TOLERANCE = 6e-6


def _plain(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, set):
        return sorted(v)
    if hasattr(v, 'items'):
        return {str(k): _plain(x) for k, x in v.items()}
    return [_plain(x) for x in v]


def _id(v):
    if v is None:
        return None
    if isinstance(v, bpy.types.ID):
        return {'name': v.name, 'type': v.bl_rna.identifier,
                'library': v.library.filepath if v.library else None}
    if v.bl_rna.identifier == 'ActionSlot':
        return {'identifier': v.identifier, 'handle': v.handle,
                'target_id_type': v.target_id_type}
    return {'type': v.bl_rna.identifier, 'name': getattr(v, 'name', None)}


def _props(v):
    try:
        return {k: _plain(v[k]) for k in sorted(v.keys())}
    except TypeError:
        return {}


def _rna(owner, exclude=()):
    result = {}
    for prop in owner.bl_rna.properties:
        key = prop.identifier
        if key in {'rna_type', *exclude} or prop.is_readonly:
            continue
        value = getattr(owner, key)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            result[key] = _plain(value)
        elif prop.type == 'POINTER':
            result[key] = _id(value)
    return result


def _curve(fc):
    return {'settings': _rna(fc), 'group': fc.group.name if fc.group else None,
            'keys': [_rna(p) for p in fc.keyframe_points],
            'sampled': [_plain(p.co) for p in fc.sampled_points],
            'modifiers': [_rna(m) for m in fc.modifiers],
            'driver': None if fc.driver is None else {
                'settings': _rna(fc.driver), 'variables': [
                    {'settings': _rna(v), 'targets': [_rna(t) for t in v.targets]}
                    for v in fc.driver.variables]}}


def _animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    return {'settings': _rna(ad), 'drivers': [_curve(f) for f in ad.drivers],
            'nla': [{'settings': _rna(t), 'strips': [_rna(s) for s in t.strips]}
                    for t in ad.nla_tracks]}


def _actions():
    return [{'name': a.name, 'users': a.users, 'use_extra_user': a.use_extra_user,
             'settings': _rna(a), 'props': _props(a),
             'slots': [{'identifier': s.identifier, 'handle': s.handle,
                        'target_id_type': s.target_id_type, 'name_display': s.name_display,
                        'settings': _rna(s)} for s in a.slots], 'layers': [
                 {'settings': _rna(l), 'strips': [
                     {'type': s.type, 'settings': _rna(s), 'bags': [
                         {'slot': _id(b.slot), 'slot_handle': b.slot_handle,
                          'groups': [_rna(g) for g in b.groups],
                          'curves': [_curve(f) for f in b.fcurves]}
                         for b in s.channelbags]} for s in l.strips]}
                 for l in a.layers]} for a in bpy.data.actions]


def _f32(v):
    return struct.unpack('f', struct.pack('f', float(v)))[0]


def _quat(axis, angle):
    s = math.sin(angle / 2)
    return [_f32(math.cos(angle / 2)), *[_f32(x * s) for x in axis]]


def _refresh():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def _check(m):
    if m.get('schema') != SCHEMA or m.get('case_id') not in CASE_IDS:
        raise ValueError('Unknown motion fixture manifest')
    for name in m['objects']:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.get('hrig_motion_fixture') != m['fixture_id']:
            raise ValueError('Owned fixture identity changed: ' + name)


def create_case(case):
    if case not in CASE_IDS or bpy.context.mode != 'OBJECT':
        raise ValueError('Known case in fresh Object-mode worker required')
    prefix = 'HRIG_MOTION_' + case
    if bpy.data.objects.get(prefix + '_Rig'):
        raise ValueError('Fresh owned worker required')
    bpy.context.scene.frame_set(0, subframe=0)
    arm = bpy.data.armatures.new(prefix + '_Armature')
    rig = bpy.data.objects.new(prefix + '_Rig', arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    controls = ['Control%d' % i for i in range(6 if case == 'euler_clip' else 3)]
    deforms = ['Deform%d' % i for i in range(len(controls))]
    bpy.ops.object.mode_set(mode='EDIT')
    for i, name in enumerate(controls):
        b = arm.edit_bones.new(name)
        b.head, b.tail, b.use_deform = (0, i, 0), (0, i + 1, 0), False
        if i:
            b.parent, b.use_connect = arm.edit_bones[controls[i - 1]], True
    for i, name in enumerate(deforms):
        b = arm.edit_bones.new(name)
        b.head, b.tail, b.use_deform = (0, i, 0), (0, i + 1, 0), True
    b = arm.edit_bones.new('Other')
    b.head, b.tail, b.use_deform = (3, 0, 0), (3, 1, 0), False
    bpy.ops.object.mode_set(mode='OBJECT')
    modes = ('XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX')
    for i, name in enumerate(controls):
        p = rig.pose.bones[name]
        p.rotation_mode = modes[i] if case == 'euler_clip' else 'QUATERNION'
        if p.rotation_mode == 'QUATERNION':
            p.rotation_quaternion = _quat((0, 0, 1), .04 * (i + 1))
        else:
            p.rotation_euler = tuple(_f32(x) for x in (.02 * (i + 1), -.03, .04))
        p['sentinel'] = 'preserve-' + name
    for i, name in enumerate(deforms):
        c = rig.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        c.name, c.target, c.subtarget = 'ExplicitDeformCopy', rig, controls[i]
        c.target_space, c.owner_space, c.mix_mode = 'POSE', 'POSE', 'REPLACE'
    other = rig.pose.bones['Other']
    other.rotation_mode, other.rotation_euler = 'XYZ', (.12, -.07, .05)
    col = arm.collections.new('Fixture Bones')
    for b in arm.bones:
        col.assign(b)
        b['sentinel'] = 'rest-' + b.name
    arm.collections.active, arm.bones.active = col, arm.bones['Other']
    # Obtain real native AnimData defaults without leaving AnimData in the
    # quaternion/euler no-AD cases. This is fixture setup, not a product oracle.
    default_ad = _animation(rig) if rig.animation_data else None
    if default_ad is not None:
        raise ValueError('Fresh fixture unexpectedly contains AnimData')
    rig.animation_data_create()
    default_settings = _rna(rig.animation_data)
    rig.animation_data_clear()
    if case in {'driver_only_clip', 'rollback_first_curve', 'rollback_bound'}:
        other.driver_add('location', 0).driver.expression = '0.037'
        rig.animation_data.last_slot_identifier = 'OBStaleFixture'
        rig.animation_data.action_slot_handle = 197
    verts = []
    for i in range(len(controls)):
        verts.extend([(-.17, i + .2, .1), (.19, i + .35, -.05), (.03, i + .7, .16)])
    data = bpy.data.meshes.new(prefix + '_MeshData')
    data.from_pydata(verts, [], [(i, i + 1, i + 2) for i in range(0, len(verts), 3)])
    data.update()
    mesh = bpy.data.objects.new(prefix + '_Mesh', data)
    bpy.context.scene.collection.objects.link(mesh)
    for i, name in enumerate(deforms):
        mesh.vertex_groups.new(name=name).add(list(range(i * 3, i * 3 + 3)), 1, 'REPLACE')
    mod = mesh.modifiers.new('RigidSkinWitness', 'ARMATURE')
    mod.object, mod.use_deform_preserve_volume = rig, False
    uv = data.uv_layers.new(name='WitnessUV')
    for i, item in enumerate(uv.data):
        item.uv = ((i % 3) / 2, (i // 3) / max(1, len(controls) - 1))
    mesh.shape_key_add(name='Basis')
    key = mesh.shape_key_add(name='UntouchedExpression')
    key.data[0].co.z += .02
    key.value = 0
    mat = bpy.data.materials.new(prefix + '_Material')
    mat.diffuse_color = (.17, .29, .43, 1)
    data.materials.append(mat)
    for obj in (rig, mesh):
        obj['hrig_motion_fixture'] = prefix
    _refresh()
    return {'schema': SCHEMA, 'case_id': case, 'fixture_id': prefix, 'rig': rig.name,
            'mesh': mesh.name, 'objects': [rig.name, mesh.name], 'bones': controls,
            'deforms': deforms, 'action_name': prefix + '_Clip',
            'inject_stage': STAGES.get(case), 'inject_rollback': case in STAGES,
            'expected': {'ok': case not in EXPECTED_CODES,
                         'changed': case not in EXPECTED_CODES,
                         'code': EXPECTED_CODES.get(case)},
            'expected_code': EXPECTED_CODES.get(case), 'default_ad_settings': default_settings,
            'rest': {b.name: _plain(b.matrix_local) for b in arm.bones},
            'parents': {b.name: b.parent.name if b.parent else None for b in arm.bones},
            'source_vertices': [_plain(v.co) for v in data.vertices],
            'geometry_tolerance': TOLERANCE}


def prepare(m, core):
    _check(m)
    rig, case = bpy.data.objects[m['rig']], m['case_id']
    observed = core.inspect_clip(bpy.context, rig, m['bones'], m['action_name'])
    if not observed.get('ok'):
        raise ValueError('Baseline fixture inspection rejected: ' + str(observed))
    tracks = []
    for i, name in enumerate(m['bones']):
        field = observed['fields'][name]
        mode, before = field['rotation_mode'], field['value']
        peak = (_quat(((1, 0, 0), (0, 1, 0), (0, 0, 1))[i % 3], (.24, -.31, .18)[i % 3])
                if mode == 'QUATERNION' else [_f32(x) for x in (.19 + .02 * i, -.13, .27)])
        tracks.append({'bone': name, 'rotation_mode': mode, 'before': before,
                       'keys': [{'frame': 1, 'value': before}, {'frame': 4, 'value': peak},
                                {'frame': 7, 'value': before}]})
    request = {'schema': 'hrig.motion-clip.create.v1', 'action_name': m['action_name'],
               'expected_snapshot_sha256': observed['snapshot_sha256'], 'tracks': tracks}
    if case == 'reject_name_collision':
        bpy.data.actions.new(m['action_name']).use_fake_user = True
    elif case == 'reject_existing_action':
        action = bpy.data.actions.new(m['fixture_id'] + '_Existing')
        rig.animation_data_create().action = action
    elif case == 'reject_nla':
        rig.animation_data_create().nla_tracks.new().name = 'UntouchedNLA'
    elif case == 'reject_snapshot':
        request['expected_snapshot_sha256'] = '0' * 64
    elif case == 'reject_locked':
        rig.pose.bones[m['bones'][1]].lock_rotation[0] = True
    elif case == 'reject_writer':
        rig.pose.bones[m['bones'][1]].driver_add('rotation_quaternion', 1).driver.expression = '0.01'
    _refresh()
    m['request'] = copy.deepcopy(request)
    return request


def _evaluated(m, graph):
    rig, mesh = bpy.data.objects[m['rig']], bpy.data.objects[m['mesh']]
    erig, evaluated = rig.evaluated_get(graph), mesh.evaluated_get(graph)
    native = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
    try:
        coords = [_plain(evaluated.matrix_world @ v.co) for v in native.vertices]
    finally:
        evaluated.to_mesh_clear()
    return {'controls': {n: _plain(erig.matrix_world @ erig.pose.bones[n].matrix) for n in m['bones']},
            'deforms': {n: _plain(erig.matrix_world @ erig.pose.bones[n].matrix) for n in m['deforms']},
            'vertices_world': coords}


def capture(m):
    _check(m)
    graph = _refresh()
    rig, mesh = bpy.data.objects[m['rig']], bpy.data.objects[m['mesh']]
    objects = {}
    for name in m['objects']:
        obj = bpy.data.objects[name]
        row = {'settings': _rna(obj, ('dimensions',)), 'props': _props(obj), 'world': _plain(obj.matrix_world),
               'animation': _animation(obj), 'modifiers': [_rna(x) for x in obj.modifiers],
               'constraints': [_rna(x) for x in obj.constraints], 'groups': [_rna(g) for g in obj.vertex_groups]}
        if obj.type == 'ARMATURE':
            row['pose'] = {p.name: {'settings': _rna(p, ('matrix', 'matrix_basis', 'matrix_channel')),
                'props': _props(p), 'constraints': [_rna(c) for c in p.constraints]} for p in obj.pose.bones}
        objects[name] = row
    arm, data = rig.data, mesh.data
    return {'schema': 'hrig.motion-clip.fixture-observation.v1', 'objects': objects,
        'armature': {'settings': _rna(arm), 'props': _props(arm), 'animation': _animation(arm),
            'active_bone': arm.bones.active.name if arm.bones.active else None,
            'bones': {b.name: {'settings': _rna(b), 'props': _props(b),
                             'parent': b.parent.name if b.parent else None,
                             'rest': _plain(b.matrix_local)} for b in arm.bones},
            'collections': [{'settings': _rna(c), 'props': _props(c),
                             'members': sorted(b.name for b in c.bones)} for c in arm.collections_all]},
        'mesh': {'settings': _rna(data), 'props': _props(data),
            'vertices': [_plain(v.co) for v in data.vertices], 'edges': [_plain(e.vertices) for e in data.edges],
            'faces': [_plain(p.vertices) for p in data.polygons],
            'uv': [{'name': u.name, 'values': [_plain(v.uv) for v in u.data]} for u in data.uv_layers],
            'weights': [[{'group': g.group, 'weight': g.weight} for g in v.groups] for v in data.vertices],
            'keys': {'settings': _rna(data.shape_keys), 'props': _props(data.shape_keys),
                     'blocks': [{'settings': _rna(k), 'coords': [_plain(v.co) for v in k.data]}
                                for k in data.shape_keys.key_blocks]},
            'materials': [{'settings': _rna(x), 'props': _props(x)} for x in data.materials]},
        'actions': _actions(), 'scene': {'settings': _rna(bpy.context.scene),
            'props': _props(bpy.context.scene), 'animation': _animation(bpy.context.scene)},
        'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                    'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe,
                    'mode': bpy.context.mode, 'active': bpy.context.view_layer.objects.active.name
                    if bpy.context.view_layer.objects.active else None,
                    'selected': sorted(x.name for x in bpy.context.selected_objects)},
        'evaluated': _evaluated(m, graph)}


def _rotation(mode, value):
    return (Quaternion(value).normalized().to_matrix().to_4x4() if mode == 'QUATERNION'
            else Euler(value, mode).to_matrix().to_4x4())


def _track_value(track, frame):
    keys = track['keys']
    if frame <= keys[0]['frame']:
        return keys[0]['value']
    if frame >= keys[-1]['frame']:
        return keys[-1]['value']
    for a, b in zip(keys, keys[1:]):
        if a['frame'] <= frame <= b['frame']:
            t = (frame - a['frame']) / (b['frame'] - a['frame'])
            return [x + t * (y - x) for x, y in zip(a['value'], b['value'])]
    raise ValueError('No independent interpolation interval')


def _expected_geometry(m, frame, world):
    poses = {}
    tracks = {r['bone']: r for r in m['request']['tracks']}
    for n in m['bones']:
        row = tracks[n]
        basis = _rotation(row['rotation_mode'], _track_value(row, frame))
        rest, parent = Matrix(m['rest'][n]), m['parents'][n]
        poses[n] = rest @ basis if parent is None else poses[parent] @ Matrix(m['rest'][parent]).inverted() @ rest @ basis
    world = Matrix(world)
    vertices = []
    for i, raw in enumerate(m['source_vertices']):
        n, d = m['bones'][i // 3], m['deforms'][i // 3]
        vertices.append(_plain(world @ poses[n] @ Matrix(m['rest'][d]).inverted() @ Vector(raw)))
    return {'controls': {n: _plain(world @ poses[n]) for n in m['bones']},
            'deforms': {d: _plain(world @ poses[m['bones'][i]]) for i, d in enumerate(m['deforms'])},
            'vertices_world': vertices}


def _distance(a, b):
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        if a and all(type(x) in (int, float) for x in a):
            if not all(type(x) in (int, float) and math.isfinite(x) for x in a + b):
                return math.inf
            return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)))
        return max((_distance(x, y) for x, y in zip(a, b)), default=0)
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        return max((_distance(a[k], b[k]) for k in a), default=0)
    return 0 if a == b else math.inf


def _action_checks(m, a, animation):
    checks = []
    def check(name, value):
        checks.append({'name': name, 'ok': bool(value)})
    check('one_real_action_user_without_extra_or_fake', a['users'] == 1 and not a['use_extra_user']
          and not a['settings']['use_fake_user'])
    check('one_slot_and_one_layer', len(a['slots']) == 1 and len(a['layers']) == 1)
    if len(a['slots']) != 1 or len(a['layers']) != 1:
        return checks
    slot, layer = a['slots'][0], a['layers'][0]
    check('exact_object_slot', slot['target_id_type'] == 'OBJECT' and slot['name_display'] == 'HRIG Rotation')
    settings = animation['settings']
    check('exact_native_slot_binding', settings['action']['name'] == m['action_name']
          and settings['action_slot'] == {k: slot[k] for k in ('identifier', 'handle', 'target_id_type')}
          and settings['action_slot_handle'] == slot['handle'] and settings['last_slot_identifier'] == slot['identifier'])
    check('exact_layer_and_single_strip', layer['settings']['name'] == 'Rotation Clip' and len(layer['strips']) == 1)
    if len(layer['strips']) != 1:
        return checks
    strip = layer['strips'][0]
    check('one_keyframe_strip_and_bag', strip['type'] == 'KEYFRAME' and len(strip['bags']) == 1)
    if len(strip['bags']) != 1:
        return checks
    bag = strip['bags'][0]
    check('bag_slot_and_no_groups', bag['slot_handle'] == slot['handle'] and bag['slot'] == settings['action_slot'] and not bag['groups'])
    expected = {}
    rig = bpy.data.objects[m['rig']]
    for row in m['request']['tracks']:
        field = 'rotation_quaternion' if row['rotation_mode'] == 'QUATERNION' else 'rotation_euler'
        path = rig.pose.bones[row['bone']].path_from_id(field)
        for index in range(len(row['before'])):
            expected[path, index] = [[float(k['frame']), k['value'][index]] for k in row['keys']]
    seen = set()
    for i, curve in enumerate(bag['curves']):
        s = curve['settings']
        identity = s['data_path'], s['array_index']
        check('component_%d_declared_and_unique' % i, identity in expected and identity not in seen)
        seen.add(identity)
        check('component_%d_exact_keys_linear_constant' % i,
              [k['co'] for k in curve['keys']] == expected.get(identity)
              and all(k['interpolation'] == 'LINEAR' for k in curve['keys'])
              and s['extrapolation'] == 'CONSTANT')
        check('component_%d_no_extra_writer' % i, not s['mute'] and not s['lock'] and curve['group'] is None
              and not curve['modifiers'] and not curve['sampled'])
    check('all_and_only_requested_component_curves', seen == set(expected) and len(seen) == len(bag['curves']))
    return checks


def validate(m, before, after, result):
    checks = []
    def check(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok), **({'detail': detail} if detail is not None else {})})
    expected = m['expected']
    check('result_matches_case', result.get('ok') is expected['ok'])
    if not expected['ok']:
        check('specific_error_code', result.get('error', {}).get('code') == expected['code'])
        check('complete_observation_preserved', before == after)
        check('failure_reports_unchanged', result.get('changed') is False)
        if m['inject_rollback']:
            check('rollback_reported_success', result.get('rollback', {}).get('ok') is True)
        return {'ok': all(c['ok'] for c in checks), 'checks': checks,
                'observed_error_code': result.get('error', {}).get('code')}
    check('success_reports_changed', result.get('changed') is True)
    b, a = copy.deepcopy(before), copy.deepcopy(after)
    ba, aa = b['objects'][m['rig']]['animation'], a['objects'][m['rig']]['animation']
    new = [x for x in a['actions'] if x['name'] == m['action_name']]
    check('exactly_one_new_named_action', len(new) == 1 and all(x['name'] != m['action_name'] for x in b['actions']))
    if len(new) == 1 and aa is not None:
        checks.extend(_action_checks(m, new[0], aa))
    check('new_animdata_present', aa is not None)
    if aa is not None:
        original_settings = m['default_ad_settings'] if ba is None else ba['settings']
        check('all_nonbinding_animdata_fields_preserved',
              {k: v for k, v in aa['settings'].items() if k not in BINDING_FIELDS}
              == {k: v for k, v in original_settings.items() if k not in BINDING_FIELDS})
        check('all_drivers_and_nla_preserved', aa['drivers'] == ([] if ba is None else ba['drivers'])
              and aa['nla'] == ([] if ba is None else ba['nla']))
    # The only permitted differences are this exact new Action and its four
    # native binding fields. No pose, scene, shape, material or old ID masking.
    a['actions'] = [x for x in a['actions'] if x['name'] != m['action_name']]
    a['objects'][m['rig']]['animation'] = copy.deepcopy(ba)
    check('all_other_authoring_and_frame0_geometry_exact', a == b)
    for label, observation in (('before', before), ('after', after)):
        expected_geo = _expected_geometry(m, 0, observation['objects'][m['rig']]['world'])
        distance = _distance(expected_geo, observation['evaluated'])
        check('independent_frame0_fk_and_skin_' + label, distance <= TOLERANCE,
              {'maximum_euclidean_row_error': distance, 'tolerance': TOLERANCE})
    check('explicit_authoring_frame0', after['context']['frame'] == 0 and after['context']['subframe'] == 0)
    return {'ok': all(c['ok'] for c in checks), 'checks': checks,
            'scope': 'Exact new rotation Action, six Euler orders, normalized component-linear quaternion FK and one-hot rigid skin; not solver/physics certification'}


def observe_animation(m, frames):
    """Sample real dependency-graph evaluation against an independent oracle.

    Caller must restore frame 0 before authoring capture or persistence. This
    fixture has no physics; it cannot certify another scene's cache state.
    """
    _check(m)
    rig = bpy.data.objects[m['rig']]
    rows, checks = [], []
    for frame in frames:
        if type(frame) is not int:
            raise ValueError('Integer sample frames required')
        bpy.context.scene.frame_set(frame, subframe=0)
        graph = _refresh()
        actual = _evaluated(m, graph)
        expected = _expected_geometry(m, frame, _plain(rig.matrix_world))
        error = _distance(expected, actual)
        rows.append({'frame': frame, 'actual': actual, 'expected': expected,
                     'maximum_euclidean_row_error': error,
                     'channels': {n: {'rotation_mode': rig.pose.bones[n].rotation_mode,
                         'value': _plain(getattr(rig.pose.bones[n], 'rotation_quaternion'
                             if rig.pose.bones[n].rotation_mode == 'QUATERNION' else 'rotation_euler'))}
                         for n in m['bones']}})
        checks.append({'name': 'independent_animation_fk_skin_frame_%d' % frame,
                       'ok': error <= TOLERANCE})
    if len(rows) >= 2:
        excursion = max((_distance(rows[0]['actual'], r['actual']) for r in rows[1:]), default=0)
        checks.append({'name': 'nonzero_actual_motion', 'ok': excursion > .01})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'frames': rows,
            'tolerance': TOLERANCE, 'leaves_frame': bpy.context.scene.frame_current}
