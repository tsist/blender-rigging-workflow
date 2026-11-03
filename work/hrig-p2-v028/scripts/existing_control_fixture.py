"""Owned native settings fixtures and an independent literal authoring oracle.

No product protection mask or observer is used here. The worker owns factory
reset, setter/refresh fault injection, full-character evaluation and persistence.
"""
import copy
import json
import math

import bpy

SCHEMA = 'hrig.settings.fixture.v1'
RIG = '千早爱音-素体骨架'
BONE = '骨架设置'
KEYS = ('前发物理', '后发物理')
VAR = '____________'
CASE_IDS = (
    'both_off', 'front_off', 'back_off', 'no_op',
    'rollback_first', 'rollback_second', 'rollback_refresh',
    'stale_snapshot', 'stale_definition', 'stale_before', 'ui_drift', 'bool_value',
    'property_driver_writer', 'property_action_alias', 'fifth_reader', 'effect_reader',
    'driver_modifier', 'muted_driver', 'shared_rig', 'nonzero_frame', 'unrelated_missing_reader',
)
CODES = {
    'stale_snapshot': 'SETTINGS_SNAPSHOT_CONFLICT',
    'stale_definition': 'SETTINGS_DEFINITION_CONFLICT',
    'stale_before': 'SETTINGS_BEFORE_CONFLICT',
    'ui_drift': 'SETTINGS_SNAPSHOT_CONFLICT',
    'bool_value': 'SETTINGS_SCHEMA',
    'property_driver_writer': 'SETTINGS_PROPERTY_WRITER',
    'property_action_alias': 'SETTINGS_ACTION_WRITER',
    'fifth_reader': 'SETTINGS_CONSUMER',
    'effect_reader': 'SETTINGS_CONSUMER',
    'driver_modifier': 'SETTINGS_DRIVER_DEFINITION',
    'muted_driver': 'SETTINGS_DRIVER_DEFINITION',
    'shared_rig': 'SETTINGS_SHARED_ARMATURE',
    'nonzero_frame': 'SETTINGS_CONTEXT',
}
CACHE_AUTHOR = ('frame_start', 'frame_end', 'frame_step', 'use_disk_cache', 'use_external',
                'filepath', 'name', 'compression', 'use_library_path')


def _identity(item):
    if item is None:
        return None
    if isinstance(item, bpy.types.ID):
        return {'type': item.bl_rna.identifier, 'name': item.name,
                'library': item.library.filepath if item.library else None}
    if item.bl_rna.identifier == 'ActionSlot':
        return {'type': 'ActionSlot', 'identifier': item.identifier,
                'handle': item.handle, 'target_id_type': item.target_id_type}
    return {'type': item.bl_rna.identifier, 'name': getattr(item, 'name', None)}


def _plain(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, bpy.types.ID):
        return _identity(value)
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, 'items'):
        return {str(key): _plain(item) for key, item in value.items()}
    return [_plain(item) for item in value]


def _props(owner):
    return {key: {'type': type(owner[key]).__name__, 'value': _plain(owner[key])}
            for key in sorted(owner.keys())}


def _rna(owner, exclude=()):
    result = {}
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in ('rna_type', 'use_extra_user', *exclude):
            continue
        value = getattr(owner, name)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            result[name] = _plain(value)
        elif prop.type == 'POINTER':
            result[name] = _identity(value)
    return result


def _curve(curve):
    return {'settings': _rna(curve), 'is_valid': curve.is_valid,
            'group': curve.group.name if curve.group else None,
            'keys': [_rna(point) for point in curve.keyframe_points],
            'samples': [_plain(point.co) for point in curve.sampled_points],
            'modifiers': [_rna(item) for item in curve.modifiers],
            'driver': None if curve.driver is None else {
                'settings': _rna(curve.driver), 'is_valid': curve.driver.is_valid,
                'variables': [{'settings': _rna(variable), 'targets': [_rna(target) for target in variable.targets]}
                              for variable in curve.driver.variables]}}


def _animation(owner):
    animation = getattr(owner, 'animation_data', None)
    if animation is None:
        return None
    def strips(items):
        return [{'settings': _rna(strip), 'curves': [_curve(curve) for curve in strip.fcurves],
                 'strips': strips(strip.strips)} for strip in items]
    return {'settings': _rna(animation), 'drivers': [_curve(curve) for curve in animation.drivers],
            'nla': [{'settings': _rna(track), 'strips': strips(track.strips)} for track in animation.nla_tracks]}


def _actions():
    rows = []
    for action in bpy.data.actions:
        row = {'name': action.name, 'users': action.users, 'settings': _rna(action),
               'props': _props(action), 'slots': [_rna(slot) for slot in action.slots],
               'layers': [{'settings': _rna(layer), 'strips': [
                   {'settings': _rna(strip), 'bags': [
                       {'slot_handle': bag.slot_handle, 'groups': [_rna(group) for group in bag.groups],
                        'curves': [_curve(curve) for curve in bag.fcurves]}
                       for bag in strip.channelbags]} for strip in layer.strips]}
                   for layer in action.layers]}
        if getattr(action, 'is_action_legacy', False):
            row['legacy_curves'] = [_curve(curve) for curve in action.fcurves]
        rows.append(row)
    return sorted(rows, key=lambda row: row['name'])


def _refresh(rig):
    rig.update_tag(refresh={'OBJECT'})
    bpy.context.view_layer.update()
    bpy.context.evaluated_depsgraph_get().update()


def _mesh(name, vertices, faces):
    data = bpy.data.meshes.new(name + '_Data')
    data.from_pydata(vertices, [], faces)
    data.update()
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _driver(owner, path, target, target_path, *, expression=VAR + '>0'):
    curve = owner.driver_add(path)
    # Only these self-created fixture curves are altered. Some native driver_add
    # routes create a Generator, incompatible with the selected finite contract.
    for modifier in list(curve.modifiers):
        curve.modifiers.remove(modifier)
    driver = curve.driver
    driver.type, driver.use_self = 'SCRIPTED', False
    for variable in list(driver.variables):
        driver.variables.remove(variable)
    if target is not None:
        variable = driver.variables.new()
        variable.name, variable.type = VAR, 'SINGLE_PROP'
        variable.targets[0].id_type = 'OBJECT'
        variable.targets[0].id, variable.targets[0].data_path = target, target_path
    driver.expression = expression
    return curve


def _selection():
    effects = []
    for side in ('前发', '后发'):
        for field in ('show_viewport', 'show_render'):
            effects.append({'object': '千早爱音-' + side + '物理', 'modifier': '布料', 'index': 0,
                            'type': 'CLOTH', 'field': field, 'control_key': side + '物理',
                            'formula': 'int_gt_zero', 'driver_path': 'modifiers["布料"].' + field,
                            'array_index': 0})
    return {'schema': 'hrig.existing-control-properties.selection.v1', 'adapter': 'AON_HAIR_SWITCHES_V1',
            'context': {'scene': 'Scene', 'view_layer': 'ViewLayer', 'frame': 0, 'subframe': 0.0},
            'rig': RIG, 'control_bone': BONE, 'properties': list(KEYS), 'effects': effects}


def create_case(case):
    if case not in CASE_IDS or bpy.context.mode != 'OBJECT' or bpy.data.objects.get(RIG):
        raise ValueError('Known settings fixture requires fresh owned Object-mode worker')
    scene = bpy.context.scene
    scene.name, bpy.context.view_layer.name = 'Scene', 'ViewLayer'
    scene.frame_set(0, subframe=0.0)
    data = bpy.data.armatures.new('HRIG_Settings_Fixture_Armature')
    rig = bpy.data.objects.new(RIG, data)
    scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for name, x, deform in ((BONE, 0., False), ('ProtectedDeform', .2, True), ('ProtectedControl', -.2, False)):
        bone = data.edit_bones.new(name)
        bone.head, bone.tail, bone.use_deform = (x, 0, 0), (x, 1, 0), deform
    bpy.ops.object.mode_set(mode='OBJECT')
    pb = rig.pose.bones[BONE]
    for key in KEYS:
        pb[key] = 1
        pb.id_properties_ui(key).update(min=0, max=1, soft_min=0, soft_max=1, default=1,
                                         description='Owned fixture existing int: ' + key)
    rig.pose.bones['ProtectedControl'].rotation_mode = 'XYZ'
    rig.pose.bones['ProtectedControl'].rotation_euler = (.07, -.03, .11)
    rig.pose.bones['ProtectedControl']['sentinel'] = 'preserve native pose property'
    for bone in data.bones:
        bone['sentinel'] = 'rest-' + bone.name
    collection = data.collections.new('ProtectedFixtureBones')
    for bone in data.bones:
        collection.assign(bone)
    data.collections.active = collection
    data.bones.active = data.bones[BONE]
    rig['protected_animation'] = .375
    rig.keyframe_insert(data_path='["protected_animation"]', frame=1)
    rig.keyframe_insert(data_path='["protected_animation"]', frame=9)
    rig.animation_data.action.name = 'HRIG_Settings_ProtectedAction'
    skin = _mesh('HRIG_Settings_ProtectedSkin', [(-.2, .2, 0), (.2, .3, .01), (0, .8, 0)], [(0, 1, 2)])
    skin.vertex_groups.new(name='ProtectedDeform').add([0, 1, 2], .8, 'REPLACE')
    skin.vertex_groups.new(name='ProtectedControl').add([0], .2, 'REPLACE')
    armature = skin.modifiers.new('ProtectedSkin', 'ARMATURE')
    armature.object = rig
    skin.shape_key_add(name='Basis')
    key = skin.shape_key_add(name='ProtectedExpression')
    key.data[1].co.z += .025
    key.value = .2
    uv = skin.data.uv_layers.new(name='ProtectedUV')
    for item, point in zip(uv.data, ((0, 0), (1, 0), (.5, 1))):
        item.uv = point
    material = bpy.data.materials.new('HRIG_Settings_ProtectedMaterial')
    material.diffuse_color = (.23, .41, .59, 1.)
    skin.data.materials.append(material)
    selected = _selection()
    for number, side in enumerate(('前发', '后发')):
        obj = _mesh('千早爱音-' + side + '物理',
                    [(x * .2 + number, y * .2, 2 + .01 * x * y) for y in range(2) for x in range(2)],
                    [(0, 1, 3, 2)])
        obj['sentinel'] = 'protected-cloth-' + side
        obj.vertex_groups.new(name='Pins').add([2, 3], 1., 'REPLACE')
        modifier = obj.modifiers.new('布料', 'CLOTH')
        modifier.settings.quality, modifier.settings.vertex_group_mass = 5, 'Pins'
        modifier.settings.mass = .4 + .1 * number
        modifier.point_cache.frame_start, modifier.point_cache.frame_end = 1, 12
        for field in ('show_viewport', 'show_render'):
            _driver(obj, modifier.path_from_id() + '.' + field, rig,
                    pb.path_from_id() + '[' + json.dumps(side + '物理', ensure_ascii=False) + ']')
    property_path = pb.path_from_id() + '[' + json.dumps(KEYS[0], ensure_ascii=False) + ']'
    front = bpy.data.objects['千早爱音-前发物理']
    if case == 'property_driver_writer':
        _driver(rig, property_path, None, '', expression='1')
    elif case == 'property_action_alias':
        action = bpy.data.actions.new('HRIG_Settings_ConflictingUnboundAlias')
        slot = action.slots.new(id_type='SCENE', name='AliasWriter')
        layer = action.layers.new('AliasWriter')
        strip = layer.strips.new(type='KEYFRAME')
        bag = strip.channelbag(slot, ensure=True)
        alias = 'objects[' + json.dumps(rig.name, ensure_ascii=False) + '].' + property_path
        if type(scene.path_resolve(alias)) is not int or scene.path_resolve(alias) != 1:
            raise ValueError('Action alias fixture must actually resolve to native selected int')
        curve = bag.fcurves.new(data_path=alias, index=0)
        curve.keyframe_points.insert(0, 1)
    elif case == 'fifth_reader':
        rig['fifth_reader'] = 1.
        _driver(rig, '["fifth_reader"]', rig, property_path)
    elif case == 'effect_reader':
        rig['effect_reader'] = 1.
        _driver(rig, '["effect_reader"]', front, 'modifiers["布料"].show_viewport')
    elif case == 'driver_modifier':
        front.animation_data.drivers[0].modifiers.new('GENERATOR')
    elif case == 'muted_driver':
        front.animation_data.drivers[0].mute = True
    elif case == 'shared_rig':
        second = bpy.data.objects.new('HRIG_Settings_SharedRig', data)
        scene.collection.objects.link(second)
    elif case == 'unrelated_missing_reader':
        # Same selected Bone but a divergent final key is not a consumer of
        # either selected property. Keep this unrelated legacy failure intact.
        rig['unrelated_missing_reader'] = 0.
        other_path = pb.path_from_id() + '["UnrelatedMissingKey"]'
        _driver(rig, '["unrelated_missing_reader"]', rig, other_path)
    _refresh(rig)
    if case == 'nonzero_frame':
        scene.frame_set(1, subframe=0.0)
    after = {'前发物理': 0, '后发物理': 0}
    if case == 'front_off':
        after['后发物理'] = 1
    elif case == 'back_off':
        after['前发物理'] = 1
    elif case == 'no_op':
        after = dict.fromkeys(KEYS, 1)
    injection = case.removeprefix('rollback_') if case.startswith('rollback_') else None
    return {'schema': SCHEMA, 'case': case, 'case_id': case, 'rig': RIG, 'selection': selected,
            'expected_ok': case in ('both_off', 'front_off', 'back_off', 'no_op', 'unrelated_missing_reader'),
            'expected_code': 'SETTINGS_NATIVE' if injection else CODES.get(case),
            'initial_values': dict.fromkeys(KEYS, 1), 'target_after': after,
            'inject_rollback': injection,
            'scope': 'Owned native settings fixture; full storage/evaluation/save/cold certified separately by worker'}


def _check(manifest):
    if manifest.get('schema') != SCHEMA or manifest.get('case') not in CASE_IDS:
        raise ValueError('Unknown settings fixture manifest')
    rig = bpy.data.objects.get(manifest['rig'])
    if rig is None or rig.type != 'ARMATURE' or BONE not in rig.pose.bones:
        raise ValueError('Owned settings fixture rig identity changed')
    return rig


def prepare(manifest, core, job_directory=None):
    rig = _check(manifest)
    inspected = core.inspect_settings(bpy.context, manifest['selection'])
    case = manifest['case']
    # A domain-refusal case still reaches the actual core apply operation with a
    # syntactically complete patch. It must return the specific native refusal.
    if inspected.get('ok') is not True and inspected.get('error', {}).get('code') != manifest['expected_code']:
        raise ValueError('Fixture setup unexpectedly refused: ' + str(inspected))
    patch = {'schema': 'hrig.existing-control-properties.patch.v1',
             'expected_snapshot_sha256': inspected.get('snapshot_sha256', '0' * 64),
             'expected_definition_sha256': inspected.get('definition_sha256', '0' * 64),
             'changes': [{'key': key, 'before': {'type': 'int', 'value': manifest['initial_values'][key]},
                          'after': {'type': 'int', 'value': manifest['target_after'][key]}} for key in KEYS]}
    if case == 'stale_snapshot':
        patch['expected_snapshot_sha256'] = '0' * 64
    elif case == 'stale_definition':
        patch['expected_definition_sha256'] = '0' * 64
    elif case == 'stale_before':
        patch['changes'][0]['before']['value'] = 0
    elif case == 'ui_drift':
        rig.pose.bones[BONE].id_properties_ui(KEYS[0]).update(description='Deliberate post-inspect UI definition drift')
    elif case == 'bool_value':
        patch['changes'][0]['after']['value'] = False
    manifest['request'] = copy.deepcopy(patch)
    return patch


def _mesh_state(obj):
    data = obj.data
    keys = data.shape_keys
    return {'settings': _rna(data, ('texspace_location', 'texspace_size')), 'props': _props(data),
            'animation': _animation(data), 'vertices': [_plain(vertex.co) for vertex in data.vertices],
            'edges': [_plain(edge.vertices) for edge in data.edges],
            'polygons': [_plain(face.vertices) for face in data.polygons],
            'weights': [[{'group': group.group, 'weight': group.weight} for group in vertex.groups]
                        for vertex in data.vertices],
            'uv': [{'name': layer.name, 'values': [_plain(item.uv) for item in layer.data]}
                   for layer in data.uv_layers],
            'materials': [_identity(material) for material in data.materials],
            'keys': None if keys is None else {
                'name': keys.name, 'settings': _rna(keys), 'props': _props(keys), 'animation': _animation(keys),
                'blocks': [{'name': block.name, 'settings': _rna(block),
                            'coords': [_plain(point.co) for point in block.data]} for block in keys.key_blocks]}}


def capture(manifest):
    rig = _check(manifest)
    # No evaluation, frame stepping, native refresh or product capture call here.
    # The worker controls updates; this oracle reads literal original RNA.
    objects, meshes = {}, {}
    for obj in bpy.data.objects:
        row = {'type': obj.type, 'data': _identity(obj.data),
               'settings': _rna(obj, ('matrix_world', 'matrix_local', 'matrix_basis', 'dimensions', 'bound_box')),
               'props': _props(obj), 'animation': _animation(obj),
               'constraints': [_rna(item) for item in obj.constraints],
               'groups': [_rna(group) for group in obj.vertex_groups], 'modifiers': []}
        for index, modifier in enumerate(obj.modifiers):
            part = {'index': index, 'name': modifier.name, 'type': modifier.type,
                    'settings': _rna(modifier, ('execution_time',))}
            if modifier.type == 'CLOTH':
                part.update(cloth=_rna(modifier.settings), collision=_rna(modifier.collision_settings),
                            effector_weights=_rna(modifier.settings.effector_weights),
                            cache_authoring={key: _plain(getattr(modifier.point_cache, key)) if hasattr(modifier.point_cache, key)
                                             else {'native_RNA': 'not_exposed_on_verified_build'} for key in CACHE_AUTHOR})
            row['modifiers'].append(part)
        if obj.pose:
            row['pose'] = {bone.name: {'settings': _rna(bone, ('matrix', 'matrix_channel', 'matrix_basis')),
                                      'props': _props(bone), 'constraints': [_rna(item) for item in bone.constraints]}
                           for bone in obj.pose.bones}
        objects[obj.name] = row
        if obj.type == 'MESH':
            meshes[obj.name] = _mesh_state(obj)
    armatures = {data.name: {'settings': _rna(data), 'props': _props(data), 'animation': _animation(data),
                            'bones': {bone.name: {'settings': _rna(bone), 'props': _props(bone),
                                                 'rest_matrix': _plain(bone.matrix_local),
                                                 'parent': bone.parent.name if bone.parent else None} for bone in data.bones},
                            'collections': [{'settings': _rna(group), 'props': _props(group),
                                             'members': sorted(bone.name for bone in group.bones)} for group in data.collections_all]}
                 for data in bpy.data.armatures}
    pb = rig.pose.bones[BONE]
    properties = {key: {'type': type(pb[key]).__name__, 'value': _plain(pb[key]),
                        'ui': _plain(pb.id_properties_ui(key).as_dict()),
                        'overridable_library': pb.is_property_overridable_library('[' + json.dumps(key, ensure_ascii=False) + ']')}
                  for key in KEYS}
    effects = [{**row, 'value': getattr(bpy.data.objects[row['object']].modifiers[row['index']], row['field'])}
               for row in manifest['selection']['effects']]
    return {'schema': 'hrig.settings.fixture-observation.v1', 'properties': properties, 'effects': effects,
            'objects': objects, 'meshes': meshes, 'armatures': armatures, 'actions': _actions(),
            'materials': {item.name: {'settings': _rna(item), 'props': _props(item), 'animation': _animation(item)}
                          for item in bpy.data.materials},
            'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                        'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe,
                        'mode': bpy.context.mode, 'active': bpy.context.view_layer.objects.active.name
                        if bpy.context.view_layer.objects.active else None,
                        'selected': sorted(item.name for item in bpy.context.selected_objects)}}


def _finite(value):
    if type(value) is dict:
        return all(_finite(item) for item in value.values())
    if type(value) in (list, tuple):
        return all(_finite(item) for item in value)
    return math.isfinite(value) if type(value) is float else True


def validate(manifest, before, after, answer):
    checks = []
    def check(name, passed):
        checks.append({'name': name, 'ok': bool(passed)})
    check('expected_operation_result', answer.get('ok') is manifest['expected_ok'])
    check('native_observations_finite', _finite(before) and _finite(after))
    if not manifest['expected_ok']:
        check('specific_refusal_code', answer.get('error', {}).get('code') == manifest['expected_code'])
        check('entire_native_oracle_unchanged', before == after)
        if manifest['inject_rollback']:
            check('rollback_attempted', answer.get('rollback', {}).get('attempted') is True)
            check('rollback_restored', answer.get('rollback', {}).get('ok') is True and answer.get('changed') is False)
        else:
            check('preflight_did_not_modify', answer.get('changed') is False)
        return {'ok': all(row['ok'] for row in checks), 'checks': checks,
                'observed_error_code': answer.get('error', {}).get('code')}
    expected_changed = manifest['target_after'] != manifest['initial_values']
    check('changed_matches_literal_case', answer.get('changed') is expected_changed)
    first, second = copy.deepcopy(before), copy.deepcopy(after)
    for key in KEYS:
        wanted = manifest['target_after'][key]
        old = manifest['initial_values'][key]
        for label, sample, value in (('before', first, old), ('after', second, wanted)):
            prop = sample['properties'][key]
            stored = sample['objects'][RIG]['pose'][BONE]['props'][key]
            check('native_typed_' + label + '_' + key,
                  prop['type'] == stored['type'] == 'int' and type(prop['value']) is int
                  and type(stored['value']) is int and prop['value'] == stored['value'] == value)
        # Normalize only independently verified literal value leaves.
        second['properties'][key]['value'] = first['properties'][key]['value']
        second['objects'][RIG]['pose'][BONE]['props'][key]['value'] = first['objects'][RIG]['pose'][BONE]['props'][key]['value']
    for index, declared in enumerate(manifest['selection']['effects']):
        for label, sample, values in (('before', first, manifest['initial_values']), ('after', second, manifest['target_after'])):
            effect = sample['effects'][index]
            part = sample['objects'][declared['object']]['modifiers'][declared['index']]
            actual = part['settings'][declared['field']]
            check('native_flag_' + label + '_%d' % index,
                  part['name'] == declared['modifier'] and part['type'] == 'CLOTH'
                  and type(actual) is bool and type(effect['value']) is bool
                  and actual == effect['value'] == (values[declared['control_key']] > 0))
        second['effects'][index]['value'] = first['effects'][index]['value']
        second['objects'][declared['object']]['modifiers'][declared['index']]['settings'][declared['field']] = (
            first['objects'][declared['object']]['modifiers'][declared['index']]['settings'][declared['field']])
    check('only_two_typed_values_and_four_flags_changed', first == second)
    if not expected_changed:
        check('no_op_entire_native_oracle_exact', before == after)
    return {'ok': all(row['ok'] for row in checks), 'checks': checks,
            'scope': 'Independent literal original-RNA fixture oracle; worker separately certifies full storage/evaluation/save/cold'}
