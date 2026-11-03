"""Small owned native pose fixtures with an independent forward-skin oracle.

No production capture, writer, matrix solver or protection masks are imported.
The worker must prepare before capture, and own persistence/fault injection.
"""
import copy
import math
import struct

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

SCHEMA = 'hrig.pose-rotations.fixture.v1'
LOCATION_FIXTURE_SCHEMA = 'hrig.pose-locations.fixture.v1'
LOCATION_CASE_IDS = (
    'location_chain_positive', 'location_noop',
    'location_locked_unchanged_axis_positive', 'location_reject_locked',
    'location_reject_writer', 'location_reject_connected',
    'location_reject_stale_before', 'location_reject_snapshot',
    'location_rollback_injected',
)
LOCATION_EXPECTED_CODES = {
    'location_reject_locked': 'LOCATION_LOCKED',
    'location_reject_writer': 'LOCATION_WRITER',
    'location_reject_connected': 'LOCATION_CONNECTED',
    'location_reject_stale_before': 'POSE_BEFORE_CONFLICT',
    'location_reject_snapshot': 'SNAPSHOT_CONFLICT',
    'location_rollback_injected': 'LOCATION_INVALID',
}
CASE_IDS = (
    'quaternion_chain', 'euler_chain', 'constrained_before', 'no_op',
    'reject_snapshot', 'reject_before', 'reject_locked', 'reject_driver',
    'reject_action', 'reject_shared', 'reject_mode', 'reject_missing_bone',
    'rollback_injected', 'preserve_nonselected',
)
CASES = CASE_IDS
EXPECTED_CODES = {
    'reject_snapshot': 'SNAPSHOT_CONFLICT', 'reject_before': 'POSE_BEFORE_CONFLICT',
    'reject_locked': 'POSE_LOCKED', 'reject_driver': 'POSE_WRITER',
    'reject_action': 'POSE_WRITER', 'reject_shared': 'SHARED_ARMATURE',
    'reject_mode': 'POSE_MODE_CONFLICT', 'reject_missing_bone': 'BINDING_MISSING',
    'rollback_injected': 'POSE_INVALID',
}
CONTROLS = ('Control0', 'Control1', 'Control2')
DEFORMS = ('Deform0', 'Deform1', 'Deform2')
TOLERANCE = 3e-6


def _plain(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, set):
        return sorted(v)
    if hasattr(v, 'items'):
        return {str(k): _plain(x) for k, x in v.items()}
    return [_plain(x) for x in v]


def _id(v):
    return None if v is None else {'name': v.name, 'type': v.bl_rna.identifier,
                                  'library': v.library.filepath if v.library else None}


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
        elif prop.type == 'POINTER' and (value is None or isinstance(value, bpy.types.ID)):
            result[key] = _id(value)
    return result


def _curve(fc):
    return {'settings': _rna(fc), 'keys': [_rna(p) for p in fc.keyframe_points],
            'modifiers': [_rna(m) for m in fc.modifiers],
            'driver': None if fc.driver is None else {
                'settings': _rna(fc.driver), 'variables': [
                    {'settings': _rna(v), 'targets': [_rna(t) for t in v.targets]}
                    for v in fc.driver.variables]}}


def _animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    return {'settings': _rna(ad), 'slot': ad.action_slot.identifier if ad.action_slot else None,
            'drivers': [_curve(f) for f in ad.drivers],
            'nla': [{'settings': _rna(t), 'strips': [_rna(s) for s in t.strips]}
                    for t in ad.nla_tracks]}


def _actions():
    return [{'name': a.name, 'settings': _rna(a), 'props': _props(a),
             'slots': [_rna(s) for s in a.slots], 'layers': [
                 {'settings': _rna(l), 'strips': [
                     {'settings': _rna(s), 'bags': [
                         {'slot_handle': b.slot_handle, 'curves': [_curve(f) for f in b.fcurves]}
                         for b in getattr(s, 'channelbags', ())]} for s in l.strips]}
                 for l in a.layers]} for a in bpy.data.actions]


def _f32(v):
    return struct.unpack('f', struct.pack('f', float(v)))[0]


def _quat(axis, angle):
    # Explicit trigonometry: no production canonicalizer, no normalization.
    s = math.sin(angle / 2)
    return [_f32(math.cos(angle / 2)), *[_f32(x * s) for x in axis]]


def _refresh():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def _check(m):
    if m.get('schema') == LOCATION_FIXTURE_SCHEMA:
        return _check_location(m)
    if m.get('schema') != SCHEMA or m.get('case_id') not in CASE_IDS:
        raise ValueError('Unknown pose fixture manifest')
    for name in m['objects']:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.get('hrig_pose_fixture') != m['fixture_id']:
            raise ValueError('Owned fixture identity changed: ' + name)


def create_case(case):
    if case in LOCATION_CASE_IDS:
        return _create_location_case(case)
    if case not in CASE_IDS or bpy.context.mode != 'OBJECT':
        raise ValueError('Known case in owned Object-mode worker required')
    prefix = 'HRIG_POSE_' + case
    if bpy.data.objects.get(prefix + '_Rig'):
        raise ValueError('Fresh owned worker required')
    arm = bpy.data.armatures.new(prefix + '_Armature')
    rig = bpy.data.objects.new(prefix + '_Rig', arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for i, name in enumerate(CONTROLS):
        b = arm.edit_bones.new(name)
        b.head, b.tail = (0, i, 0), (0, i + 1, 0)
        b.use_deform = False
        if i:
            b.parent, b.use_connect = arm.edit_bones[CONTROLS[i - 1]], True
    for i, name in enumerate(DEFORMS):
        b = arm.edit_bones.new(name)
        b.head, b.tail = (0, i, 0), (0, i + 1, 0)
        b.use_deform = True
    for name, x in (('Other', 3), ('Fixed', 5)):
        b = arm.edit_bones.new(name)
        b.head, b.tail, b.use_deform = (x, 0, 0), (x, 1, 0), False
    bpy.ops.object.mode_set(mode='OBJECT')
    euler_modes = ('XYZ', 'ZYX', 'YZX') if case == 'euler_chain' else ('XZY', 'YXZ', 'ZXY')
    for i, name in enumerate(CONTROLS):
        p = rig.pose.bones[name]
        p.rotation_mode = euler_modes[i] if case in {'euler_chain', 'preserve_nonselected'} else 'QUATERNION'
        if p.rotation_mode == 'QUATERNION':
            p.rotation_quaternion = _quat((0, 0, 1), .04 * (i + 1))
        else:
            p.rotation_euler = tuple(_f32(x) for x in (.02 * (i + 1), -.03, .04))
        p['sentinel'] = 'preserve-' + name
    rig.pose.bones['Fixed'].rotation_mode = 'QUATERNION'
    rig.pose.bones['Fixed'].rotation_quaternion = _quat((0, 0, 1), .23)
    for i, name in enumerate(DEFORMS):
        c = rig.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        c.name, c.target, c.subtarget = 'ExplicitDeformCopy', rig, CONTROLS[i]
        c.target_space, c.owner_space, c.mix_mode = 'POSE', 'POSE', 'REPLACE'
    if case == 'constrained_before':
        c = rig.pose.bones['Control1'].constraints.new('COPY_ROTATION')
        c.name, c.target, c.subtarget = 'KnownBeforeRotation', rig, 'Fixed'
        c.target_space, c.owner_space, c.mix_mode = 'LOCAL', 'LOCAL', 'BEFORE'
    other = rig.pose.bones['Other']
    other.rotation_mode, other.rotation_euler = 'XYZ', (.12, -.07, .05)
    c = other.constraints.new('LIMIT_ROTATION')
    c.use_limit_x, c.min_x, c.max_x = True, -.4, .4
    other.driver_add('location', 0).driver.expression = '0.037'
    col = arm.collections.new('Fixture Bones')
    for b in arm.bones:
        col.assign(b)
        b['sentinel'] = 'rest-' + b.name
    arm.collections.active = col
    arm.bones.active = arm.bones['Other']
    verts = []
    for i in range(3):
        verts.extend([(-.17, i + .2, .1), (.19, i + .35, -.05), (.03, i + .7, .16)])
    data = bpy.data.meshes.new(prefix + '_MeshData')
    data.from_pydata(verts, [], [(i, i + 1, i + 2) for i in (0, 3, 6)])
    data.update()
    mesh = bpy.data.objects.new(prefix + '_Mesh', data)
    bpy.context.scene.collection.objects.link(mesh)
    for i, name in enumerate(DEFORMS):
        mesh.vertex_groups.new(name=name).add(list(range(i * 3, i * 3 + 3)), 1, 'REPLACE')
    mod = mesh.modifiers.new('RigidSkinWitness', 'ARMATURE')
    mod.object, mod.use_deform_preserve_volume = rig, False
    uv = data.uv_layers.new(name='WitnessUV')
    for i, item in enumerate(uv.data):
        item.uv = ((i % 3) / 2, (i // 3) / 2)
    mesh.shape_key_add(name='Basis')
    key = mesh.shape_key_add(name='UntouchedExpression')
    key.data[0].co.z += .02
    key.value = 0
    mat = bpy.data.materials.new(prefix + '_Material')
    mat.diffuse_color = (.17, .29, .43, 1)
    data.materials.append(mat)
    for obj in (rig, mesh):
        obj['hrig_pose_fixture'] = prefix
    _refresh()
    bad = case in EXPECTED_CODES
    return {'schema': SCHEMA, 'case_id': case, 'fixture_id': prefix, 'rig': rig.name,
            'mesh': mesh.name, 'objects': [rig.name, mesh.name], 'bones': list(CONTROLS),
            'inject_rollback': case == 'rollback_injected',
            'expected': {'ok': not bad, 'changed': not bad and case != 'no_op',
                         'code': EXPECTED_CODES.get(case)},
            'expected_code': EXPECTED_CODES.get(case),
            'rest': {b.name: _plain(b.matrix_local) for b in arm.bones},
            'parents': {b.name: b.parent.name if b.parent else None for b in arm.bones},
            'source_vertices': [_plain(v.co) for v in data.vertices],
            'fixed_rotation': _plain(rig.pose.bones['Fixed'].rotation_quaternion),
            'geometry_tolerance': TOLERANCE}


def prepare(m, core):
    if m.get('schema') == LOCATION_FIXTURE_SCHEMA:
        return _prepare_locations(m, core)
    _check(m)
    rig, case = bpy.data.objects[m['rig']], m['case_id']
    observed = core.inspect_rotations(bpy.context, rig, m['bones'])
    if not observed.get('ok'):
        raise ValueError('Baseline fixture inspection rejected: ' + str(observed))
    rows = []
    for i, name in enumerate(m['bones']):
        field = observed['fields'][name]
        mode = field['rotation_mode']
        value = (_quat(((1, 0, 0), (0, 1, 0), (0, 0, 1))[i], (.24, -.31, .18)[i])
                 if mode == 'QUATERNION' else [_f32(x) for x in (.19 + .03 * i, -.13, .27)])
        rows.append({'bone': name, 'rotation_mode': mode, 'before': field['value'],
                     'after': field['value'] if case == 'no_op' else value})
    request = {'schema': 'hrig.pose-rotations.patch.v1',
               'expected_snapshot_sha256': observed['snapshot_sha256'], 'rotations': rows}
    # Snapshot lock changes must be captured before obtaining the final valid SHA.
    if case == 'reject_locked':
        rig.pose.bones['Control1'].lock_rotation[0] = True
        request['expected_snapshot_sha256'] = core.inspect_rotations(
            bpy.context, rig, m['bones'])['snapshot_sha256']
    elif case == 'reject_snapshot':
        request['expected_snapshot_sha256'] = '0' * 64
    elif case == 'reject_before':
        rows[1]['before'] = _quat((0, 0, 1), .41)
    elif case == 'reject_driver':
        rig.pose.bones['Control1'].driver_add('rotation_quaternion', 1).driver.expression = '0.01'
    elif case == 'reject_action':
        rig.pose.bones['Control1'].keyframe_insert('rotation_quaternion', index=1, frame=1)
    elif case == 'reject_shared':
        duplicate = bpy.data.objects.new(m['fixture_id'] + '_Shared', rig.data)
        bpy.context.scene.collection.objects.link(duplicate)
        duplicate['hrig_pose_fixture'] = m['fixture_id']
        m['objects'].append(duplicate.name)
    elif case == 'reject_mode':
        rows[1]['rotation_mode'], rows[1]['before'], rows[1]['after'] = 'XYZ', [0, 0, 0], [.1, .2, .3]
    elif case == 'reject_missing_bone':
        rows[1]['bone'] = 'DeliberatelyMissing'
    _refresh()
    m['request'] = copy.deepcopy(request)
    return request


def capture(m):
    _check(m)
    graph = _refresh()
    rig, mesh = bpy.data.objects[m['rig']], bpy.data.objects[m['mesh']]
    objects = {}
    for name in m['objects']:
        obj = bpy.data.objects[name]
        # Dimensions are a derived evaluated bound, not an authored object input.
        row = {'settings': _rna(obj, ('dimensions',)), 'props': _props(obj), 'world': _plain(obj.matrix_world),
               'animation': _animation(obj), 'modifiers': [_rna(x) for x in obj.modifiers],
               'constraints': [_rna(x) for x in obj.constraints],
               'groups': [_rna(g) for g in obj.vertex_groups]}
        if obj.type == 'ARMATURE':
            row['pose'] = {p.name: {'settings': _rna(p, ('matrix', 'matrix_basis', 'matrix_channel')),
                'props': _props(p), 'constraints': [_rna(c) for c in p.constraints]} for p in obj.pose.bones}
        objects[name] = row
    arm, data = rig.data, mesh.data
    evaluated = mesh.evaluated_get(graph)
    native = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
    try:
        coords = [_plain(evaluated.matrix_world @ v.co) for v in native.vertices]
    finally:
        evaluated.to_mesh_clear()
    erig = rig.evaluated_get(graph)
    return {'schema': ('hrig.pose-locations.fixture-observation.v1'
                      if m['schema'] == LOCATION_FIXTURE_SCHEMA
                      else 'hrig.pose-rotations.fixture-observation.v1'), 'objects': objects,
        'armature': {'settings': _rna(arm), 'props': _props(arm), 'animation': _animation(arm),
            'active_bone': arm.bones.active.name if arm.bones.active else None,
            'bones': {b.name: {'settings': _rna(b), 'props': _props(b),
                             'parent': b.parent.name if b.parent else None,
                             'rest': _plain(b.matrix_local)} for b in arm.bones},
            'collections': [{'settings': _rna(c), 'props': _props(c),
                             'members': sorted(b.name for b in c.bones)} for c in arm.collections_all]},
        'mesh': {'settings': _rna(data), 'props': _props(data),
            'vertices': [_plain(v.co) for v in data.vertices],
            'edges': [_plain(e.vertices) for e in data.edges],
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
        'evaluated': {'controls': {n: _plain(erig.matrix_world @ erig.pose.bones[n].matrix)
                                   for n in CONTROLS}, 'deforms': {
                    n: _plain(erig.matrix_world @ erig.pose.bones[n].matrix) for n in DEFORMS},
                     'vertices_world': coords}}


def _rotation(mode, value):
    return (Quaternion(value).to_matrix().to_4x4() if mode == 'QUATERNION'
            else Euler(value, mode).to_matrix().to_4x4())


def _expected_geometry(m, observation, use_after):
    """Explicit rest-parent FK and one-hot linear skin; no evaluated matrices used."""
    if m.get('schema') == LOCATION_FIXTURE_SCHEMA:
        return _expected_location_geometry(m, observation, use_after)
    poses, vertices = {}, []
    world = Matrix(observation['objects'][m['rig']]['world'])
    request = {r['bone']: r for r in m['request']['rotations']}
    for n in CONTROLS:
        settings = observation['objects'][m['rig']]['pose'][n]['settings']
        mode = settings['rotation_mode']
        value = settings['rotation_quaternion' if mode == 'QUATERNION' else 'rotation_euler']
        if use_after:
            mode, value = request[n]['rotation_mode'], request[n]['after']
        basis = _rotation(mode, value)
        if m['case_id'] == 'constrained_before' and n == 'Control1':
            basis = _rotation('QUATERNION', m['fixed_rotation']) @ basis
        rest = Matrix(m['rest'][n])
        parent = m['parents'][n]
        poses[n] = rest @ basis if parent is None else poses[parent] @ Matrix(m['rest'][parent]).inverted() @ rest @ basis
    for i, raw in enumerate(m['source_vertices']):
        n, d = CONTROLS[i // 3], DEFORMS[i // 3]
        vertices.append(_plain(world @ poses[n] @ Matrix(m['rest'][d]).inverted() @ Vector(raw)))
    return {'controls': {n: _plain(world @ poses[n]) for n in CONTROLS},
            'deforms': {d: _plain(world @ poses[CONTROLS[i]]) for i, d in enumerate(DEFORMS)},
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


def validate(m, before, after, result):
    if m.get('schema') == LOCATION_FIXTURE_SCHEMA:
        return _validate_locations(m, before, after, result)
    checks = []
    def check(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok), **({'detail': detail} if detail is not None else {})})
    expected = m['expected']
    check('result_matches_case', result.get('ok') is expected['ok'])
    if not expected['ok']:
        check('specific_error_code', result.get('error', {}).get('code') == expected['code'])
        check('complete_observation_preserved', before == after)
        if m['inject_rollback']:
            check('rollback_reported_success', result.get('rollback', {}).get('ok') is True)
        return {'ok': all(c['ok'] for c in checks), 'checks': checks,
                'observed_error_code': result.get('error', {}).get('code')}
    check('changed_matches_case', result.get('changed') is expected['changed'])
    b, a = copy.deepcopy(before), copy.deepcopy(after)
    b.pop('evaluated'); a.pop('evaluated')
    for row in m['request']['rotations']:
        name, mode = row['bone'], row['rotation_mode']
        field = 'rotation_quaternion' if mode == 'QUATERNION' else 'rotation_euler'
        ap = a['objects'][m['rig']]['pose'][name]['settings']
        bp = b['objects'][m['rig']]['pose'][name]['settings']
        check('exact_native_requested_rotation_' + name, ap[field] == row['after'])
        ap[field] = bp[field]
    check('rest_weights_keys_constraints_drivers_materials_other_channels_exact', a == b)
    details = {}
    for label, observation, use_after in (('before', before, False), ('after', after, True)):
        independent = _expected_geometry(m, observation, use_after)
        distance = _distance(independent, observation['evaluated'])
        details[label] = {'maximum_euclidean_row_error': distance, 'tolerance': TOLERANCE,
                          'expected': independent, 'actual': observation['evaluated']}
        check('independent_rest_fk_and_rigid_skin_' + label, distance <= TOLERANCE)
    if m['case_id'] == 'no_op':
        check('no_op_complete_observation_exact', before == after)
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'geometry': details,
            'scope': 'Explicit FK, BEFORE local rotation and one-hot rigid skin only; no general solver claim'}


def _check_location(m):
    if (m.get('schema') != LOCATION_FIXTURE_SCHEMA
            or m.get('channel') != 'location' or m.get('case_id') not in LOCATION_CASE_IDS):
        raise ValueError('Unknown explicitly location-channel fixture manifest')
    for name in m['objects']:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.get('hrig_pose_fixture') != m['fixture_id']:
            raise ValueError('Owned location fixture identity changed: ' + name)


def _create_location_case(case):
    """Reuse the unchanged owned rotation rig recipe, then specialize new cases.

    One fresh owned worker creates one fixture. The original rotation case is
    intentionally kept byte-for-byte in its branch, including its old prefix.
    """
    if case not in LOCATION_CASE_IDS:
        raise ValueError('Known owned location case required')
    m = create_case('quaternion_chain')
    rig, mesh = bpy.data.objects[m['rig']], bpy.data.objects[m['mesh']]
    bpy.ops.object.mode_set(mode='EDIT')
    for name in CONTROLS[1:]:
        rig.data.edit_bones[name].use_connect = False
    bpy.ops.object.mode_set(mode='OBJECT')
    # Matching nonidentity object transforms make the world-space skin oracle
    # exercise translation/rotation/nonuniform scale without changing its rest
    # or one-hot weight assumptions. Controls remain fully inheriting FK bones.
    world = Matrix.LocRotScale(Vector((.35, -.4, .2)),
        Euler((.11, -.17, .23), 'XYZ').to_quaternion(), Vector((1.2, .8, 1.1)))
    rig.matrix_world = world
    mesh.matrix_world = world
    for name, value in zip(CONTROLS, ((.02, -.01, .03), (-.01, .02, .0), (.015, .0, -.025))):
        rig.pose.bones[name].location = [_f32(x) for x in value]
        rig.data.bones[name].use_local_location = True
        rig.data.bones[name].inherit_scale = 'FULL'
    if case == 'location_locked_unchanged_axis_positive':
        rig.pose.bones['Control1'].lock_location = (False, True, True)
    elif case == 'location_reject_locked':
        rig.pose.bones['Control1'].lock_location = (True, False, False)
    _refresh()
    code = LOCATION_EXPECTED_CODES.get(case)
    m.update(schema=LOCATION_FIXTURE_SCHEMA, channel='location', case_id=case,
        inject_rollback=case == 'location_rollback_injected', expected_code=code,
        expected={'ok': code is None, 'changed': code is None and case != 'location_noop', 'code': code})
    # The rest geometry is unchanged by disconnecting these coincident bones;
    # retain an explicit fresh observation instead of relying on that fact.
    m['rest'] = {bone.name: _plain(bone.matrix_local) for bone in rig.data.bones}
    m['parents'] = {bone.name: bone.parent.name if bone.parent else None for bone in rig.data.bones}
    return m


def _prepare_locations(m, core):
    _check_location(m)
    rig, case = bpy.data.objects[m['rig']], m['case_id']
    observed = core.inspect_locations(bpy.context, rig, m['bones'])
    if not observed.get('ok'):
        raise ValueError('Baseline location inspection rejected: ' + str(observed))
    rows = []
    deltas = ((.13, -.04, .05), (-.07, .025, .04), (.03, -.06, -.035))
    for name, delta in zip(m['bones'], deltas):
        before = observed['fields'][name]['value']
        after = [_f32(value + change) for value, change in zip(before, delta)]
        if case == 'location_noop':
            after = list(before)
        if case == 'location_locked_unchanged_axis_positive' and name == 'Control1':
            after[1:] = before[1:]
        rows.append({'bone': name, 'before': list(before), 'after': after})
    request = {'schema': 'hrig.pose-locations.patch.v1',
        'expected_snapshot_sha256': observed['snapshot_sha256'], 'locations': rows}
    if case == 'location_reject_snapshot':
        request['expected_snapshot_sha256'] = '0' * 64
    elif case == 'location_reject_stale_before':
        rows[1]['before'][0] = _f32(rows[1]['before'][0] + .001)
    elif case == 'location_reject_writer':
        rig.pose.bones['Control1'].driver_add('location', 0).driver.expression = '-0.01'
    elif case == 'location_reject_connected':
        bpy.ops.object.mode_set(mode='EDIT')
        rig.data.edit_bones['Control1'].use_connect = True
        bpy.ops.object.mode_set(mode='OBJECT')
        if not rig.data.bones['Control1'].use_connect:
            raise ValueError('Native connected negative fixture was not constructed')
    _refresh()
    m['request'] = copy.deepcopy(request)
    return request


def _expected_location_geometry(m, observation, use_after):
    """Independent T @ R @ S rest-parent FK and one-hot linear skin.

    Reads only stored authored L/R/S, rest and parent data. It deliberately does
    not read evaluated control/deform matrices or invoke the production solver.
    Matching rig/Mesh object transforms are part of this fixture's assumptions.
    """
    poses, vertices = {}, []
    world = Matrix(observation['objects'][m['rig']]['world'])
    request = {row['bone']: row for row in m['request']['locations']}
    for name in CONTROLS:
        settings = observation['objects'][m['rig']]['pose'][name]['settings']
        mode = settings['rotation_mode']
        rotation = settings['rotation_quaternion' if mode == 'QUATERNION' else 'rotation_euler']
        location = request[name]['after'] if use_after else settings['location']
        basis = (Matrix.Translation(Vector(location)) @ _rotation(mode, rotation)
                 @ Matrix.Diagonal(Vector((*settings['scale'], 1.0))))
        rest, parent = Matrix(m['rest'][name]), m['parents'][name]
        poses[name] = (rest @ basis if parent is None
            else poses[parent] @ Matrix(m['rest'][parent]).inverted() @ rest @ basis)
    for index, raw in enumerate(m['source_vertices']):
        name, deform = CONTROLS[index // 3], DEFORMS[index // 3]
        vertices.append(_plain(world @ poses[name] @ Matrix(m['rest'][deform]).inverted() @ Vector(raw)))
    return {'controls': {name: _plain(world @ poses[name]) for name in CONTROLS},
        'deforms': {name: _plain(world @ poses[CONTROLS[index]]) for index, name in enumerate(DEFORMS)},
        'vertices_world': vertices}


def _validate_locations(m, before, after, result):
    _check_location(m)
    checks = []
    def check(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok), **({'detail': detail} if detail is not None else {})})
    expected = m['expected']
    check('location_result_matches_case', result.get('ok') is expected['ok'])
    if not expected['ok']:
        check('location_specific_error_code', result.get('error', {}).get('code') == expected['code'])
        check('location_complete_observation_preserved', before == after)
        if m['inject_rollback']:
            check('location_rollback_reported_success', result.get('rollback', {}).get('ok') is True)
        return {'ok': all(row['ok'] for row in checks), 'checks': checks,
            'observed_error_code': result.get('error', {}).get('code')}
    check('location_changed_matches_case', result.get('changed') is expected['changed'])
    first, second = copy.deepcopy(before), copy.deepcopy(after)
    first.pop('evaluated'); second.pop('evaluated')
    for row in m['request']['locations']:
        name = row['bone']
        original = first['objects'][m['rig']]['pose'][name]['settings']
        changed = second['objects'][m['rig']]['pose'][name]['settings']
        check('exact_native_requested_location_' + name, changed['location'] == row['after'])
        changed['location'] = original['location']
    check('location_rest_weights_keys_constraints_drivers_materials_other_channels_exact', first == second)
    details = {}
    for label, observation, use_after in (('before', before, False), ('after', after, True)):
        independent = _expected_location_geometry(m, observation, use_after)
        distance = _distance(independent, observation['evaluated'])
        details[label] = {'maximum_euclidean_row_error': distance, 'tolerance': TOLERANCE,
            'expected': independent, 'actual': observation['evaluated']}
        check('independent_location_rest_fk_and_rigid_skin_' + label, distance <= TOLERANCE)
    if m['case_id'] == 'location_noop':
        check('location_noop_zero_reported_writes', result.get('writes') == [])
        check('location_noop_complete_observation_exact', before == after)
    else:
        delta = _distance(before['evaluated']['vertices_world'], after['evaluated']['vertices_world'])
        check('location_actual_skin_changes', delta > TOLERANCE, {'maximum_vertex_distance': delta})
    if m['case_id'] == 'location_locked_unchanged_axis_positive':
        key = m['rig']
        native_before = before['objects'][key]['pose']['Control1']['settings']
        native_after = after['objects'][key]['pose']['Control1']['settings']
        check('location_locked_axes_native_unchanged', native_before['location'][1:] == native_after['location'][1:])
        check('location_native_lock_authoring_retained', native_before['lock_location'] == native_after['lock_location'] == [False, True, True])
    return {'ok': all(row['ok'] for row in checks), 'checks': checks, 'geometry': details,
        'scope': 'Disconnected controls with full-inheritance explicit T/R/S FK, matching transformed rig/Mesh, COPY_TRANSFORMS deforms and one-hot rigid skin only; no general solver claim'}
