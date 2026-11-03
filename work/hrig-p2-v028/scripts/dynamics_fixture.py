"""Owned native Cloth quality fixtures, independent authoring/evaluation checks.

Only the public inspect API prepares the snapshot token. Native RNA, animation,
weights and geometry are captured here without importing the production capture,
writer or protection masks. The worker owns persistence and fault injection.
No simulation bake, performance, collision or visual-quality claim is made.
"""
import copy
import math
from pathlib import Path

import bpy


SCHEMA = 'hrig.cloth-quality.fixture.v1'
CASES = (
    'two_modifiers_quality', 'no_op', 'reader_refused',
    'before_conflict', 'snapshot_conflict', 'missing_target',
    'name_conflict', 'type_conflict', 'wrong_object_type',
    'quality_driver', 'quality_action', 'cache_disk', 'cache_external',
    'rollback_after_first',
)
CASE_IDS = (
    'two_modifiers_quality', 'no_op', 'reader_refused',
    'before_conflict', 'snapshot_conflict', 'missing_target',
    'name_conflict', 'type_conflict', 'wrong_object_type',
    'quality_driver', 'quality_action', 'cache_disk', 'cache_external',
    'rollback_after_first',
)
EXPECTED_CODES = {
    'reader_refused': 'DYNAMICS_CONSUMER',
    'before_conflict': 'DYNAMICS_BEFORE_CONFLICT',
    'snapshot_conflict': 'SNAPSHOT_CONFLICT',
    'missing_target': 'DYNAMICS_TARGET', 'name_conflict': 'DYNAMICS_TARGET',
    'type_conflict': 'DYNAMICS_TARGET', 'wrong_object_type': 'DYNAMICS_TARGET',
    'quality_driver': 'DYNAMICS_WRITER', 'quality_action': 'DYNAMICS_WRITER',
    'cache_disk': 'DYNAMICS_CACHE', 'cache_external': 'DYNAMICS_CACHE',
    'rollback_after_first': 'DYNAMICS_INVALID',
}
CACHE_AUTHOR_FIELDS = ('frame_start', 'frame_end', 'frame_step', 'use_disk_cache',
                       'use_external', 'filepath', 'name', 'compression', 'use_library_path')
READER_PROPERTY = 'quality_sum'


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, 'items'):
        return {str(key): _plain(item) for key, item in value.items()}
    return [_plain(item) for item in value]


def _identity(value):
    if value is None:
        return None
    if isinstance(value, bpy.types.ID):
        return {'type': value.bl_rna.identifier, 'name': value.name,
                'library': value.library.filepath if value.library else None}
    if value.bl_rna.identifier == 'ActionSlot':
        return {'type': 'ActionSlot', 'identifier': value.identifier,
                'handle': value.handle, 'target_id_type': value.target_id_type}
    return {'type': value.bl_rna.identifier, 'name': getattr(value, 'name', None)}


def _props(owner, exclude=()):
    return {key: _plain(owner[key]) for key in sorted(owner.keys()) if key not in exclude}


def _rna(owner, exclude=()):
    result = {}
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if name == 'rna_type' or name in exclude or prop.is_readonly:
            continue
        value = getattr(owner, name)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            result[name] = _plain(value)
        elif prop.type == 'POINTER':
            result[name] = _identity(value)
    return result


def _curve(curve):
    return {'settings': _rna(curve),
            'group': curve.group.name if curve.group else None,
            'keys': [_rna(key) for key in curve.keyframe_points],
            'samples': [_plain(point.co) for point in curve.sampled_points],
            'modifiers': [_rna(modifier) for modifier in curve.modifiers],
            'driver': None if curve.driver is None else {
                'settings': _rna(curve.driver),
                'variables': [{'settings': _rna(variable),
                               'targets': [_rna(target) for target in variable.targets]}
                              for variable in curve.driver.variables]}}


def _animation(owner):
    animation = owner.animation_data
    if animation is None:
        return None
    return {'settings': _rna(animation), 'drivers': [_curve(f) for f in animation.drivers],
            'nla': [{'settings': _rna(track), 'strips': [_rna(strip) for strip in track.strips]}
                    for track in animation.nla_tracks]}


def _actions():
    return [{'name': action.name, 'users': action.users,
             'use_extra_user': action.use_extra_user, 'settings': _rna(action),
             'props': _props(action), 'slots': [_rna(slot) for slot in action.slots],
             'layers': [{'settings': _rna(layer), 'strips': [
                 {'settings': _rna(strip), 'bags': [
                     {'slot': _identity(bag.slot), 'slot_handle': bag.slot_handle,
                      'groups': [_rna(group) for group in bag.groups],
                      'curves': [_curve(curve) for curve in bag.fcurves]}
                     for bag in strip.channelbags]} for strip in layer.strips]}
                 for layer in action.layers]} for action in bpy.data.actions]


def _refresh():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def _check(manifest):
    if manifest.get('schema') != SCHEMA or manifest.get('case_id') not in CASES:
        raise ValueError('Unknown dynamics fixture manifest')
    for name in manifest['objects']:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.get('hrig_dynamics_fixture') != manifest['fixture_id']:
            raise ValueError('Owned fixture identity changed: ' + name)


def _mesh_object(name, vertices, faces):
    data = bpy.data.meshes.new(name + '_Data')
    data.from_pydata(vertices, [], faces)
    data.update()
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def create_case(case):
    if case not in CASES or bpy.context.mode != 'OBJECT':
        raise ValueError('Known case in fresh owned Object-mode worker required')
    prefix = 'HRIG_DYNAMICS_' + case
    if bpy.data.objects.get(prefix + '_Rig'):
        raise ValueError('Fresh owned worker required')
    bpy.context.scene.frame_set(0, subframe=0)
    armature = bpy.data.armatures.new(prefix + '_Armature')
    rig = bpy.data.objects.new(prefix + '_Rig', armature)
    bpy.context.scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for name, x, deform in (('Control', 0, False), ('Deform', 0, True)):
        bone = armature.edit_bones.new(name)
        bone.head, bone.tail, bone.use_deform = (x, 0, 0), (x, 1, 0), deform
    bpy.ops.object.mode_set(mode='OBJECT')
    control = rig.pose.bones['Control']
    control.rotation_mode = 'XYZ'
    control.rotation_euler = (.09, -.04, .13)
    constraint = rig.pose.bones['Deform'].constraints.new('COPY_TRANSFORMS')
    constraint.name, constraint.target, constraint.subtarget = 'ProtectedCopy', rig, 'Control'
    constraint.target_space, constraint.owner_space = 'POSE', 'POSE'
    rig['animation_sentinel'] = .25
    rig.keyframe_insert(data_path='["animation_sentinel"]', frame=1)
    rig.keyframe_insert(data_path='["animation_sentinel"]', frame=9)
    rig.animation_data.action.name = prefix + '_ExistingAction'
    rig.pose.bones['Control']['sentinel'] = 'preserve-control'
    collection = armature.collections.new('Fixture Bones')
    for bone in armature.bones:
        bone['sentinel'] = 'rest-' + bone.name
        collection.assign(bone)
    armature.collections.active = collection
    armature.bones.active = armature.bones['Control']
    skin = _mesh_object(prefix + '_Skin',
        [(-.2, .2, .1), (.2, .3, -.1), (.0, .8, .12)], [(0, 1, 2)])
    skin.vertex_groups.new(name='Deform').add([0, 1, 2], 1.0, 'REPLACE')
    skin_modifier = skin.modifiers.new('ProtectedSkin', 'ARMATURE')
    skin_modifier.object = rig
    skin_modifier.use_deform_preserve_volume = False
    skin.shape_key_add(name='Basis')
    expression = skin.shape_key_add(name='ProtectedExpression')
    expression.data[1].co.z += .025
    expression.value = .2
    uv = skin.data.uv_layers.new(name='ProtectedUV')
    for item, value in zip(uv.data, ((0, 0), (1, 0), (.5, 1))):
        item.uv = value
    material = bpy.data.materials.new(prefix + '_Material')
    material.diffuse_color = (.23, .41, .59, 1)
    skin.data.materials.append(material)
    cloths, targets = [], []
    for number in range(2):
        vertices = [(x * .2 + number, y * .2, 2 + .03 * x * y)
                    for y in range(3) for x in range(3)]
        faces = [(y * 3 + x, y * 3 + x + 1, (y + 1) * 3 + x + 1, (y + 1) * 3 + x)
                 for y in range(2) for x in range(2)]
        obj = _mesh_object(prefix + '_Cloth%d' % number, vertices, faces)
        obj.vertex_groups.new(name='Pins').add([6, 7, 8], 1.0, 'REPLACE')
        modifier = obj.modifiers.new('OwnedCloth', 'CLOTH')
        modifier.settings.quality = 5
        modifier.settings.vertex_group_mass = 'Pins'
        modifier.settings.mass = .4 + number * .1
        modifier.collision_settings.collision_quality = 3 - number
        modifier.collision_settings.use_self_collision = bool(number)
        modifier.point_cache.frame_start, modifier.point_cache.frame_end = 1, 12
        witness = obj.modifiers.new('NonTargetWeld', 'WELD')
        witness.merge_threshold = .00001
        obj['sentinel'] = 'cloth-authoring-%d' % number
        cloths.append(obj)
        targets.append({'object': obj.name, 'modifier': modifier.name, 'index': 0})
    for obj in (rig, skin, *cloths):
        obj['hrig_dynamics_fixture'] = prefix
    _refresh()
    return {'schema': SCHEMA, 'case_id': case, 'fixture_id': prefix,
            'rig': rig.name, 'mesh': skin.name, 'cloths': [obj.name for obj in cloths],
            'objects': [rig.name, skin.name, *[obj.name for obj in cloths]],
            'targets': targets, 'bones': ['Control', 'Deform'],
            'inject_rollback': case == 'rollback_after_first',
            'expected': {'ok': case not in EXPECTED_CODES,
                         'changed': case not in EXPECTED_CODES and case != 'no_op',
                         'code': EXPECTED_CODES.get(case)},
            'expected_code': EXPECTED_CODES.get(case)}


def prepare(manifest, core, *, job_directory=None):
    _check(manifest)
    observed = core.inspect_quality(bpy.context, manifest['targets'])
    if not observed.get('ok'):
        raise ValueError('Baseline fixture inspection rejected: ' + str(observed))
    changes = [{**target, 'before': 5, 'after': 5 if manifest['case_id'] == 'no_op' else 3}
               for target in manifest['targets']]
    request = {'schema': 'hrig.cloth-quality.patch.v1',
               'expected_snapshot_sha256': observed['snapshot_sha256'], 'changes': changes}
    case = manifest['case_id']
    obj = bpy.data.objects[manifest['cloths'][1]]
    if case == 'reader_refused':
        rig = bpy.data.objects[manifest['rig']]
        rig[READER_PROPERTY] = 0.0
        driver = rig.driver_add('["%s"]' % READER_PROPERTY).driver
        driver.expression = 'q0 + q1'
        for number, name in enumerate(manifest['cloths']):
            source = bpy.data.objects[name]
            variable = driver.variables.new()
            variable.name, variable.type = 'q%d' % number, 'SINGLE_PROP'
            variable.targets[0].id = source
            variable.targets[0].data_path = source.modifiers[0].path_from_id() + '.settings.quality'
    elif case == 'before_conflict':
        changes[1]['before'] = 4
    elif case == 'snapshot_conflict':
        request['expected_snapshot_sha256'] = '0' * 64
    elif case == 'missing_target':
        changes[1]['object'] = manifest['fixture_id'] + '_DeliberatelyMissing'
    elif case == 'name_conflict':
        changes[1]['modifier'] = 'DeliberatelyWrongName'
    elif case == 'type_conflict':
        changes[1]['modifier'], changes[1]['index'] = obj.modifiers[1].name, 1
    elif case == 'wrong_object_type':
        changes[1]['object'] = manifest['rig']
    elif case == 'quality_driver':
        obj.modifiers[0].settings.driver_add('quality').driver.expression = '5'
    elif case == 'quality_action':
        obj.modifiers[0].settings.keyframe_insert(data_path='quality', frame=1)
        obj.modifiers[0].settings.keyframe_insert(data_path='quality', frame=9)
        obj.animation_data.action.name = manifest['fixture_id'] + '_QualityWriter'
    elif case in {'cache_disk', 'cache_external'}:
        # Native cache flags are not mock state: unsaved Main can silently
        # refuse disk-cache enablement. Persist this owned fixture at a fresh
        # bootstrap path before setting either flag; before capture follows
        # prepare, so all save effects belong to the declared fixture baseline.
        if not isinstance(job_directory, (str, Path)):
            raise ValueError('Owned absolute existing worker job directory required')
        owned_job = Path(job_directory)
        if not owned_job.is_absolute() or not owned_job.is_dir():
            raise ValueError('Owned absolute existing worker job directory required')
        owned_job = owned_job.resolve(strict=True)
        bootstrap = owned_job / 'fixture-cache-bootstrap.blend'
        if bootstrap.exists() or bootstrap.is_symlink():
            raise ValueError('Refusing to overwrite fixture cache bootstrap')
        saved = bpy.ops.wm.save_as_mainfile(filepath=str(bootstrap), check_existing=False)
        if saved != {'FINISHED'} or not bootstrap.is_file() or Path(bpy.data.filepath).resolve() != bootstrap:
            raise ValueError('Native owned fixture cache bootstrap did not persist')
        flag = 'use_disk_cache' if case == 'cache_disk' else 'use_external'
        setattr(obj.modifiers[0].point_cache, flag, True)
        if getattr(obj.modifiers[0].point_cache, flag) is not True:
            raise ValueError('Native saved-Main cache flag did not enable: ' + flag)
    _refresh()
    manifest['request'] = copy.deepcopy(request)
    return request


def _mesh_state(obj):
    data = obj.data
    keys = data.shape_keys
    return {'settings': _rna(data), 'props': _props(data),
            'animation': _animation(data),
            'vertices': [_plain(v.co) for v in data.vertices],
            'edges': [_plain(edge.vertices) for edge in data.edges],
            'faces': [_plain(face.vertices) for face in data.polygons],
            'weights': [[{'group': group.group, 'weight': group.weight} for group in v.groups]
                        for v in data.vertices],
            'uv': [{'name': layer.name, 'values': [_plain(item.uv) for item in layer.data]}
                   for layer in data.uv_layers],
            'keys': None if keys is None else {
                'settings': _rna(keys), 'props': _props(keys), 'animation': _animation(keys),
                'blocks': [{'settings': _rna(block), 'coords': [_plain(v.co) for v in block.data]}
                           for block in keys.key_blocks]},
            'materials': [{'settings': _rna(material), 'props': _props(material),
                           'animation': _animation(material)} for material in data.materials]}


def capture(manifest):
    _check(manifest)
    graph = _refresh()
    objects, meshes, evaluated, caches = {}, {}, {}, []
    for name in manifest['objects']:
        obj = bpy.data.objects[name]
        objects[name] = {'settings': _rna(obj, ('dimensions',)),
                        'props': _props(obj),
                        'animation': _animation(obj),
                        'world': _plain(obj.matrix_world),
                        'constraints': [_rna(constraint) for constraint in obj.constraints],
                        'groups': [_rna(group) for group in obj.vertex_groups],
                        'modifiers': []}
        for index, modifier in enumerate(obj.modifiers):
            row = {'settings': _rna(modifier)}
            if modifier.type == 'CLOTH':
                cache = modifier.point_cache
                row.update(cloth=_rna(modifier.settings), collision=_rna(modifier.collision_settings),
                    effector_weights=_rna(modifier.settings.effector_weights),
                    cache={key: _plain(getattr(cache, key)) for key in CACHE_AUTHOR_FIELDS
                           if hasattr(cache, key)})
                caches.append({'object': name, 'index': index,
                    'is_baked': cache.is_baked, 'is_baking': cache.is_baking,
                    'is_outdated': cache.is_outdated, 'info': cache.info})
            objects[name]['modifiers'].append(row)
        native_obj = obj.evaluated_get(graph)
        if not native_obj.is_evaluated or native_obj.original != obj:
            raise ValueError('Fixture evaluation did not return a native evaluated object')
        if obj.type == 'MESH':
            meshes[name] = _mesh_state(obj)
            native_mesh = native_obj.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
            try:
                evaluated[name] = {'local': [_plain(v.co) for v in native_mesh.vertices],
                                   'world': [_plain(native_obj.matrix_world @ v.co)
                                             for v in native_mesh.vertices]}
            finally:
                native_obj.to_mesh_clear()
        elif obj.type == 'ARMATURE':
            objects[name]['pose'] = {bone.name: {
                'settings': _rna(bone, ('matrix', 'matrix_basis', 'matrix_channel')),
                'props': _props(bone), 'constraints': [_rna(c) for c in bone.constraints]}
                for bone in obj.pose.bones}
            evaluated[name] = {bone.name: _plain(native_obj.matrix_world @ bone.matrix)
                               for bone in native_obj.pose.bones}
    rig = bpy.data.objects[manifest['rig']]
    armature = rig.data
    reader = None
    if manifest['case_id'] == 'reader_refused':
        reader = {'inputs': [bpy.data.objects[name].modifiers[0].settings.quality
                             for name in manifest['cloths']],
                  'native_output': rig.evaluated_get(graph)[READER_PROPERTY]}
    return {'schema': 'hrig.cloth-quality.fixture-observation.v1',
            'objects': objects, 'meshes': meshes,
            'armature': {'settings': _rna(armature), 'props': _props(armature),
                'animation': _animation(armature),
                'active_bone': armature.bones.active.name if armature.bones.active else None,
                'bones': {bone.name: {'settings': _rna(bone), 'props': _props(bone),
                    'rest': _plain(bone.matrix_local),
                    'parent': bone.parent.name if bone.parent else None} for bone in armature.bones},
                'collections': [{'settings': _rna(collection), 'props': _props(collection),
                    'members': sorted(bone.name for bone in collection.bones)}
                    for collection in armature.collections_all]},
            'actions': _actions(), 'evaluated': evaluated,
            'derived_cache': caches, 'reader': reader,
            'scene': {'settings': _rna(bpy.context.scene), 'props': _props(bpy.context.scene),
                      'animation': _animation(bpy.context.scene)},
            'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe,
                'mode': bpy.context.mode,
                'active': bpy.context.view_layer.objects.active.name
                    if bpy.context.view_layer.objects.active else None,
                'selected': sorted(obj.name for obj in bpy.context.selected_objects)}}


def _finite(value):
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(item) for item in value)
    return math.isfinite(value) if type(value) in (int, float) else True


def validate(manifest, before, after, result):
    checks = []
    def check(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok),
                       **({'detail': detail} if detail is not None else {})})
    expected = manifest['expected']
    check('result_matches_case', result.get('ok') is expected['ok'])
    check('complete_native_evaluation_finite', _finite(before['evaluated']) and _finite(after['evaluated']))
    check('frame_zero_context', before['context']['frame'] == after['context']['frame'] == 0
          and before['context']['subframe'] == after['context']['subframe'] == 0)
    b, a = copy.deepcopy(before), copy.deepcopy(after)
    # Cache runtime flags are observations, never writable authoring or fabricated
    # restore targets. Exact cache settings remain inside each modifier record.
    b.pop('derived_cache'); a.pop('derived_cache')
    if not expected['ok']:
        check('specific_error_code', result.get('error', {}).get('code') == expected['code'])
        check('complete_authoring_and_static_evaluation_preserved', b == a)
        if manifest['case_id'] == 'reader_refused':
            for label, observation in (('before', before), ('after', after)):
                reader = observation['reader']
                check('reader_native_inputs_' + label, reader['inputs'] == [5, 5])
                check('independent_reader_sum_' + label, reader['native_output'] == 10, reader)
                check('original_rna_reader_sum_' + label,
                      observation['objects'][manifest['rig']]['props'].get(READER_PROPERTY) == 10)
        if manifest['inject_rollback']:
            check('rollback_reported_success', result.get('rollback', {}).get('ok') is True)
        return {'ok': all(item['ok'] for item in checks), 'checks': checks,
                'observed_error_code': result.get('error', {}).get('code')}
    check('changed_matches_case', result.get('changed') is expected['changed'])
    for change in manifest['request']['changes']:
        name, index = change['object'], change['index']
        actual = a['objects'][name]['modifiers'][index]['cloth']['quality']
        check('native_literal_quality_' + name, actual == change['after'])
        a['objects'][name]['modifiers'][index]['cloth']['quality'] = (
            b['objects'][name]['modifiers'][index]['cloth']['quality'])
    check('only_literal_quality_changed', a == b)
    if manifest['case_id'] == 'no_op':
        check('no_op_full_observation_exact', before == after)
    return {'ok': all(item['ok'] for item in checks), 'checks': checks,
            'scope': 'Native static frame0 authoring/geometry protection; no simulated-motion or quality certification'}
