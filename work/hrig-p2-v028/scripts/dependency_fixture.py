"""Synthetic dependency experiments; import has no scene side effects.

Run each case in its own empty Blender process. No user files are opened,
deleted or saved here. create_case/observe change only fixture data and frame.
The caller owns persistence, CLI stdout/stderr capture and cold-read validation.
"""
import math
from pathlib import Path

import bpy
from mathutils import Vector

CASES = ('same_rig_ik', 'copy_feedback', 'property_influence',
         'mesh_group_visible', 'excluded_mesh', 'reference_edges')
PREFIX = 'HRIG_DEP_'
SCHEMA = 'hrig-dependency-fixture-1'


def _empty(name, collection, location=(0, 0, 0)):
    obj = bpy.data.objects.new(name, None)
    collection.objects.link(obj)
    obj.location = location
    return obj


def _rig(name, collection, specs):
    """specs contains (name, head, tail, parent_name); never keep EditBone refs."""
    data = bpy.data.armatures.new(name + '_Data')
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        for bone_name, head, tail, parent_name in specs:
            bone = data.edit_bones.new(bone_name)
            bone.head, bone.tail = head, tail
            if parent_name:
                bone.parent = data.edit_bones[parent_name]
                # Compare the two stored RNA vectors after float conversion.
                bone.use_connect = tuple(bone.head) == tuple(bone.parent.tail)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    return obj


def _key(owner, path, values):
    for frame, value in values:
        if path.startswith('["'):
            owner[path[2:-2]] = value
        else:
            setattr(owner, path, value)
        owner.keyframe_insert(data_path=path, frame=frame)


def _layer_collection(root, name):
    if root.name == name:
        return root
    for child in root.children:
        found = _layer_collection(child, name)
        if found is not None:
            return found
    return None


def create_case(case, *, scene=None):
    """Create one bounded fixture, returning stable JSON names for cold readers.

    Requires current context Scene, OBJECT mode, empty current Scene, and no
    existing fixture-prefixed objects. Refuses instead of clearing user content.
    """
    if case not in CASES:
        raise ValueError('Unknown dependency fixture case: ' + str(case))
    scene = scene or bpy.context.scene
    if scene != bpy.context.scene or bpy.context.mode != 'OBJECT':
        raise RuntimeError('Requires the current Scene in OBJECT mode')
    if len(scene.objects) or any(o.name.startswith(PREFIX) for o in bpy.data.objects):
        raise RuntimeError('Requires an empty isolated Scene and unused fixture prefix')
    root = bpy.data.collections.new(PREFIX + case)
    scene.collection.children.link(root)
    manifest = {'schema': SCHEMA, 'case': case, 'scene': scene.name,
                'view_layer': bpy.context.view_layer.name, 'collection': root.name,
                'objects': {}, 'bones': {}, 'frames': [1, 2]}
    scene.frame_start, scene.frame_end = 1, 2
    if case == 'same_rig_ik':
        rig = _rig(PREFIX + 'IK_Rig', root, [
            ('Upper', (0, 0, 0), (0, 1, .2), None),
            ('Lower', (0, 1, .2), (0, 2, 0), 'Upper'),
            ('Target', (.8, 1.3, 0), (.8, 1.6, 0), None),
            ('Pole', (0, 0, 1), (0, .3, 1), None)])
        constraint = rig.pose.bones['Lower'].constraints.new('IK')
        constraint.name = 'IndependentTargetIK'
        constraint.target, constraint.subtarget = rig, 'Target'
        constraint.pole_target, constraint.pole_subtarget = rig, 'Pole'
        constraint.chain_count, constraint.use_stretch, constraint.iterations = 2, False, 64
        _key(rig.pose.bones['Target'], 'location', [(1, (0, 0, 0)), (2, (.3, -.1, 0))])
        manifest['objects'] = {'rig': rig.name}
        manifest['bones'] = {'end': 'Lower', 'target': 'Target', 'pole': 'Pole'}
        manifest['rest_seed'] = {'elbow': [0, 1, .2],
                                 'reason': 'Noncollinear rest seed toward positive-Z pole; straight legacy IK control retained in earlier evidence'}
    elif case == 'copy_feedback':
        first = _empty(PREFIX + 'FeedbackA', root, (1, 0, 0))
        second = _empty(PREFIX + 'FeedbackB', root, (3, 0, 0))
        for owner, target in ((first, second), (second, first)):
            constraint = owner.constraints.new('COPY_LOCATION')
            constraint.name = 'ReciprocalCopy'
            constraint.target = target
            constraint.owner_space = constraint.target_space = 'WORLD'
        _key(first, 'location', [(1, (1, 0, 0)), (2, (2, 0, 0))])
        manifest['objects'] = {'first': first.name, 'second': second.name}
    elif case == 'property_influence':
        rig = _rig(PREFIX + 'Driver_Rig', root, [('Follower', (0, 0, 0), (0, 1, 0), None)])
        control = _empty(PREFIX + 'Property', root)
        target = _empty(PREFIX + 'LocationTarget', root, (2, 0, 0))
        control['blend'] = .25
        constraint = rig.pose.bones['Follower'].constraints.new('COPY_LOCATION')
        constraint.name, constraint.target = 'DrivenInfluence', target
        constraint.owner_space, constraint.target_space = 'POSE', 'WORLD'
        curve = constraint.driver_add('influence')
        curve.driver.type, curve.driver.expression = 'SCRIPTED', 'v'
        variable = curve.driver.variables.new()
        variable.name, variable.type = 'v', 'SINGLE_PROP'
        variable.targets[0].id = control
        variable.targets[0].data_path = '["blend"]'
        _key(control, '["blend"]', [(1, .25), (2, .75)])
        manifest['objects'] = {'rig': rig.name, 'control': control.name, 'target': target.name}
        manifest['bones'] = {'follower': 'Follower'}
    elif case == 'reference_edges':
        rig = _rig(PREFIX + 'Reference_Rig', root, [('Reference', (0, 0, 0), (0, 1, 0), None)])
        rig['settings'] = {'nested': {'gain': .375}, 'mode': 'fixture'}
        missing = rig.pose.bones['Reference'].constraints.new('COPY_LOCATION')
        missing.name = 'RequiredMissingTarget'
        optional = rig.pose.bones['Reference'].constraints.new('IK')
        optional.name, optional.chain_count, optional.mute = 'OptionalTargetlessIK', 1, True
        rig['disabled_driver_sink'] = 0.0
        curve = rig.driver_add('["disabled_driver_sink"]')
        # Keep the deliberately incomplete variable out of native evaluation.
        curve.mute = True
        curve.driver.type, curve.driver.expression = 'SCRIPTED', 'v'
        variable = curve.driver.variables.new()
        variable.name, variable.type = 'v', 'SINGLE_PROP'
        variable.targets[0].data_path = '["absent_input"]'
        manifest['objects'] = {'rig': rig.name}
        manifest['bones'] = {'reference': 'Reference'}
        manifest['driver_evaluation'] = 'Muted FCurve; missing variable target.id is structural evidence only'
    else:
        rig = _rig(PREFIX + 'Mesh_Rig', root, [('Aim', (0, 0, 0), (0, 1, 0), None)])
        proxies = bpy.data.collections.new(PREFIX + 'ProxyCollection')
        root.children.link(proxies)
        mesh = bpy.data.meshes.new(PREFIX + 'ProxyData')
        mesh.from_pydata([(1, -.2, .2), (1, .2, .2), (1, 0, .6)], [], [(0, 1, 2)])
        mesh.update()
        proxy = bpy.data.objects.new(PREFIX + 'Proxy', mesh)
        proxies.objects.link(proxy)
        proxy.vertex_groups.new(name='TrackedGroup').add([0, 1, 2], 1.0, 'REPLACE')
        proxy.shape_key_add(name='Basis')
        shift = proxy.shape_key_add(name='Shift')
        for point in shift.data:
            point.co.x += 1.0
        control = _empty(PREFIX + 'ProxyControl', root)
        control['shift'] = 0.0
        curve = shift.driver_add('value')
        curve.driver.expression = 'v'
        variable = curve.driver.variables.new()
        variable.name, variable.type = 'v', 'SINGLE_PROP'
        variable.targets[0].id, variable.targets[0].data_path = control, '["shift"]'
        _key(control, '["shift"]', [(1, 0.0), (2, 1.0)])
        constraint = rig.pose.bones['Aim'].constraints.new('DAMPED_TRACK')
        constraint.name, constraint.target, constraint.subtarget = 'GroupTrack', proxy, 'TrackedGroup'
        constraint.track_axis = 'TRACK_Y'
        layer = _layer_collection(bpy.context.view_layer.layer_collection, proxies.name)
        if layer is None:
            raise RuntimeError('Proxy LayerCollection not found')
        layer.exclude = case == 'excluded_mesh'
        manifest['objects'] = {'rig': rig.name, 'proxy': proxy.name, 'control': control.name}
        manifest['bones'] = {'aim': 'Aim'}
        manifest['proxy_collection'] = proxies.name
        manifest['expected_excluded'] = case == 'excluded_mesh'
        manifest['modifier_scope'] = 'shape_key_only_no_cloth'
    return manifest


def _xyz(vector):
    return [float(value) for value in vector]


def _world_bone(obj, name, endpoint='head'):
    return obj.matrix_world @ getattr(obj.pose.bones[name], endpoint)


def observe(manifest, *, frames=None, artifact_dir=None):
    """Sequential evaluation only, no random seek or simulation/cache claims.

    A fresh evaluated object is fetched per frame. Mesh coords, constraint
    output and depsgraph.objects membership are separate evidence fields.
    Graph export is optional/explicit and refuses to overwrite an existing dot.
    Native cycle stdout/stderr must be captured by the caller from process start.
    """
    if manifest.get('schema') != SCHEMA:
        raise ValueError('Manifest schema mismatch')
    scene = bpy.data.scenes[manifest['scene']]
    if scene != bpy.context.scene or bpy.context.view_layer.name != manifest['view_layer']:
        raise RuntimeError('Manifest Scene/ViewLayer must be current')
    frames = list(manifest['frames'] if frames is None else frames)
    if not frames or frames != sorted(set(frames)):
        raise ValueError('frames must be nonempty, unique and increasing')
    objects = {role: bpy.data.objects[name] for role, name in manifest['objects'].items()}
    result = {'schema': SCHEMA, 'case': manifest['case'], 'samples': [],
              'graph_export': {'status': 'not_requested'},
              'limits': ['Native process logs are external evidence',
                         'No FPS, Cloth behavior or general solver correctness certified']}
    for frame in frames:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        members = {obj.original.name for obj in graph.objects}
        evaluated = {role: obj.evaluated_get(graph) for role, obj in objects.items()}
        row = {'frame': frame, 'depsgraph_object_membership': {
            role: obj.name in members for role, obj in objects.items()}}
        case = manifest['case']
        if case == 'same_rig_ik':
            rig = evaluated['rig']
            constraint = objects['rig'].pose.bones['Lower'].constraints['IndependentTargetIK']
            row['ik_settings'] = {key: getattr(constraint, key) for key in
                ('use_tail','use_location','use_rotation','chain_count','pole_angle','iterations','use_stretch')}
            row['solver'] = objects['rig'].pose.ik_solver
            row['bone_endpoints'] = {name: {'head': _xyz(_world_bone(rig,name)),
                'tail': _xyz(_world_bone(rig,name,'tail'))} for name in ('Upper','Lower')}
            end = _world_bone(rig, manifest['bones']['end'], 'tail')
            target = _world_bone(rig, manifest['bones']['target'])
            row.update(end_world=_xyz(end), target_world=_xyz(target), error=float((end-target).length))
        elif case == 'copy_feedback':
            row['world_locations'] = {role: _xyz(obj.matrix_world.translation) for role, obj in evaluated.items()}
            row['stored_locations'] = {role: _xyz(obj.location) for role, obj in objects.items()}
            row['reciprocal_targets'] = {
                role: obj.constraints['ReciprocalCopy'].target.name for role, obj in objects.items()}
        elif case == 'property_influence':
            rig = evaluated['rig']
            row.update(input=float(evaluated['control']['blend']),
                       influence=float(rig.pose.bones['Follower'].constraints['DrivenInfluence'].influence),
                       follower_head_world=_xyz(_world_bone(rig, 'Follower')))
            row['driver_valid'] = bool(objects['rig'].animation_data.drivers[0].driver.is_valid)
        elif case == 'reference_edges':
            rig = objects['rig']
            constraints = rig.pose.bones['Reference'].constraints
            missing, optional = constraints['RequiredMissingTarget'], constraints['OptionalTargetlessIK']
            curve = rig.animation_data.drivers[0]
            variable = curve.driver.variables[0]
            target = variable.targets[0]
            row['raw_reference_edges'] = {
                'copy_location': {'type': missing.type, 'target': missing.target.name if missing.target else None,
                                  'mute': bool(missing.mute)},
                'ik': {'type': optional.type, 'target': optional.target.name if optional.target else None,
                       'pole_target': optional.pole_target.name if optional.pole_target else None,
                       'chain_count': optional.chain_count, 'mute': bool(optional.mute)},
                'driver': {'path': curve.data_path, 'fcurve_mute': bool(curve.mute),
                           'type': curve.driver.type, 'expression': curve.driver.expression,
                           'variable_type': variable.type, 'target_id': target.id.name if target.id else None,
                           'target_path': target.data_path}}
            row['nested_properties'] = {'settings': {'nested': {'gain': float(rig['settings']['nested']['gain'])},
                                                      'mode': str(rig['settings']['mode'])}}
            row['nested_group_python_type'] = type(rig['settings']['nested']).__name__
        else:
            proxy, rig = evaluated['proxy'], evaluated['rig']
            coords = [proxy.matrix_world @ vertex.co for vertex in proxy.data.vertices]
            centroid = sum(coords, Vector((0, 0, 0))) / len(coords)
            bone = rig.pose.bones['Aim']
            head = rig.matrix_world @ bone.head
            direction = (rig.matrix_world.to_3x3() @ bone.matrix.to_3x3() @ Vector((0, 1, 0))).normalized()
            expected = (centroid-head).normalized()
            layer = _layer_collection(bpy.context.view_layer.layer_collection, manifest['proxy_collection'])
            row.update(proxy_excluded=bool(layer.exclude), proxy_vertices_world=[_xyz(v) for v in coords],
                       proxy_centroid_world=_xyz(centroid), direction_world=_xyz(direction),
                       direction_dot=float(direction.dot(expected)), input=float(evaluated['control']['shift']),
                       group_exists='TrackedGroup' in objects['proxy'].vertex_groups,
                       native_constraint_target=objects['rig'].pose.bones['Aim'].constraints['GroupTrack'].subtarget)
        # Nonfinite values remain JSON-safe and fail explicit numerical checks.
        row['nonfinite_paths'] = _nonfinite(row)
        result['samples'].append(_json_safe(row))
    if artifact_dir is not None:
        folder = Path(artifact_dir)
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (manifest['case'] + '-depsgraph.dot')
        if destination.exists():
            raise FileExistsError(str(destination))
        try:
            graph.debug_relations_graphviz(filepath=str(destination))
            result['graph_export'] = {'status': 'written' if destination.is_file() else 'not_written',
                                      'path': str(destination)}
        except (AttributeError, RuntimeError, TypeError) as error:
            result['graph_export'] = {'status': 'unsupported_or_error', 'error': str(error)}
    return result


def _nonfinite(value, path=''):
    if isinstance(value, float):
        return [path] if not math.isfinite(value) else []
    if isinstance(value, dict):
        return [item for key, sub in value.items() for item in _nonfinite(sub, path + '/' + str(key))]
    if isinstance(value, (tuple, list)):
        return [item for index, sub in enumerate(value) for item in _nonfinite(sub, path + '/' + str(index))]
    return []


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(sub) for key, sub in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(sub) for sub in value]
    return value


def _near(actual, expected, tolerance=1e-4):
    return isinstance(actual, (float, int)) and abs(actual-expected) <= tolerance


def validate(manifest, observation):
    """Numerical fixture checks; feedback finite values are observational only."""
    samples = observation['samples']
    checks = {'identity': observation['schema'] == SCHEMA and observation['case'] == manifest['case'],
              'two_frame_protocol': [row['frame'] for row in samples] == [1, 2]}
    finite = all(not row['nonfinite_paths'] for row in samples)
    case = manifest['case']
    limits = ['Pass covers this synthetic experiment and these two frames only']
    if case == 'same_rig_ik':
        rig = bpy.data.objects[manifest['objects']['rig']]
        checks.update(independent_roots=all(rig.data.bones[name].parent is None for name in ('Target', 'Pole')),
                      finite=finite, endpoint_reaches=finite and all(row['error'] < 2e-3 for row in samples))
        checks['target_and_endpoint_move'] = finite and len(samples) == 2 and all(
            (Vector(samples[1][key])-Vector(samples[0][key])).length > .1 for key in ('end_world', 'target_world'))
    elif case == 'copy_feedback':
        a, b = manifest['objects']['first'], manifest['objects']['second']
        checks['reciprocal_relations_exist'] = all(row['reciprocal_targets'] == {'first': b, 'second': a} for row in samples)
        limits += ['Finite output does not prove no dependency cycle',
                   'Cycle log and operation graph require independent review',
                   'No automatic repair or nonfinite-result expectation']
    elif case == 'property_influence':
        checks.update(finite=finite, driver_valid=all(row['driver_valid'] for row in samples),
                      influence_matches=all(_near(row['influence'], row['input']) for row in samples),
                      actual_output_changes=finite and all(_near(row['follower_head_world'][0], 2*row['input']) for row in samples))
    elif case == 'reference_edges':
        edges = [row['raw_reference_edges'] for row in samples]
        checks.update(
            finite=finite,
            required_target_is_missing=all(edge['copy_location'] == {
                'type': 'COPY_LOCATION', 'target': None, 'mute': False} for edge in edges),
            optional_ik_targets_empty=all(edge['ik'] == {
                'type': 'IK', 'target': None, 'pole_target': None, 'chain_count': 1, 'mute': True} for edge in edges),
            incomplete_driver_muted=all(edge['driver'] == {
                'path': '["disabled_driver_sink"]', 'fcurve_mute': True,
                'type': 'SCRIPTED', 'expression': 'v', 'variable_type': 'SINGLE_PROP',
                'target_id': None, 'target_path': '["absent_input"]'} for edge in edges),
            nested_property_values=all(row['nested_properties'] == {
                'settings': {'nested': {'gain': .375}, 'mode': 'fixture'}} for row in samples))
        limits += ['Checks raw fixture construction only; collector resolution assertions are external',
                   'Muted incomplete driver expression was not intentionally evaluated']
    else:
        checks.update(finite=finite,
                      exclusion_as_requested=all(row['proxy_excluded'] == manifest['expected_excluded'] for row in samples),
                      group_resolved=all(row['group_exists'] and row['native_constraint_target'] == 'TrackedGroup' for row in samples),
                      evaluated_mesh_moves=finite and all(_near(row['proxy_centroid_world'][0], 1+row['input']) for row in samples),
                      track_matches_evaluated_group=finite and all(row['direction_dot'] > .999 for row in samples))
        checks['actual_direction_changes'] = finite and len(samples) == 2 and (Vector(samples[1]['direction_world'])-Vector(samples[0]['direction_world'])).length > .05
        limits += ['Membership, evaluated mesh and consumer output are separate witnesses',
                   'Shape-key dependency is not a Cloth scheduling or cache validation']
    return {'status': 'pass' if all(checks.values()) else 'fail', 'checks': checks,
            'finite_observed': finite, 'limits': limits}
