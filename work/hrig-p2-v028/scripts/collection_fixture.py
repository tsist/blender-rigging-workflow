"""Owned native collection fixtures; independent of addon capture/merge/masks.

The caller owns every core call, one-shot _mutate fault injection, persistence
and independent cold reads. No production observer is imported here.
"""
import copy
import hashlib
import json
import math

import bpy

SCHEMA = 'hrig.bone-collections.fixture.v1'
LEDGER = 'hrig_collection_instances_v1'
OWNER = 'hrig_collection_owner_v1'
CASE_IDS = (
    'create_tree', 'repeat', 'update_rename', 'hand_name_preserve', 'same_target',
    'member_move', 'hand_extra_member_preserve', 'hand_reparent_preserve',
    'explicit_detach', 'non_target_memberships', 'reject_name_collision',
    'reject_role_change', 'reject_bindings_change', 'reject_parents_change',
    'reject_detach_change', 'reject_downgrade', 'reject_same_revision',
    'reject_bad_ledger', 'reject_bad_owner', 'reject_missing_owned',
    'reject_shared_armature', 'reject_driver_reader', 'reject_scene_alias_reader',
    'reject_context_reader', 'reject_action_writer', 'rollback_create',
    'rollback_update', 'reject_snapshot', 'reject_name_conflict',
    'reject_future_name_reader', 'reject_future_scene_alias_reader',
    'reject_unbound_object_action', 'reject_unbound_scene_action',
)
_REJECT = frozenset(x for x in CASE_IDS if x.startswith('reject_') or x.startswith('rollback_'))
EXPECTED_CODES = {
    'reject_name_collision': 'NAME_CONFLICT',
    'reject_role_change': 'STRUCTURE_CHANGE',
    'reject_bindings_change': 'STRUCTURE_CHANGE',
    'reject_parents_change': 'STRUCTURE_CHANGE',
    'reject_detach_change': 'STRUCTURE_CHANGE',
    'reject_downgrade': 'REVISION_CONFLICT',
    'reject_same_revision': 'REVISION_CONFLICT',
    'reject_bad_ledger': 'LEDGER_INVALID',
    'reject_bad_owner': 'OWNER_INVALID',
    'reject_missing_owned': 'OWNER_MISSING',
    'reject_shared_armature': 'SHARED_ARMATURE',
    'reject_driver_reader': 'COLLECTION_CONSUMER',
    'reject_scene_alias_reader': 'COLLECTION_CONSUMER',
    'reject_context_reader': 'COLLECTION_CONSUMER',
    'reject_action_writer': 'COLLECTION_CONSUMER',
    'reject_future_name_reader': 'COLLECTION_CONSUMER',
    'reject_future_scene_alias_reader': 'COLLECTION_CONSUMER',
    'reject_unbound_object_action': 'COLLECTION_CONSUMER',
    'reject_unbound_scene_action': 'COLLECTION_CONSUMER',
    'reject_snapshot': 'SNAPSHOT_CONFLICT',
    'reject_name_conflict': 'MERGE_CONFLICT',
    'rollback_create': 'COLLECTION_INVALID',
    'rollback_update': 'COLLECTION_INVALID',
}


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, 'items'):
        return {str(k): _plain(v) for k, v in value.items()}
    return [_plain(v) for v in value]


def _id(value):
    return None if value is None else {'name': value.name, 'type': value.bl_rna.identifier,
                                      'library': value.library.filepath if value.library else None}


def _props(owner, exclude=()):
    try:
        return {k: _plain(owner[k]) for k in sorted(owner.keys()) if k not in exclude}
    except TypeError:
        return {'__idproperties_unsupported__': owner.bl_rna.identifier}


def _rna(owner, exclude=()):
    result = {}
    for prop in owner.bl_rna.properties:
        key = prop.identifier
        if prop.is_readonly or key in {'rna_type', *exclude}:
            continue
        value = getattr(owner, key)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            result[key] = _plain(value)
        elif prop.type == 'POINTER' and (value is None or isinstance(value, bpy.types.ID)):
            result[key] = _id(value)
    return result


def _curve(curve):
    return {'settings': _rna(curve), 'keys': [_rna(k) for k in curve.keyframe_points],
            'modifiers': [_rna(m) for m in curve.modifiers],
            'driver': None if curve.driver is None else {
                'settings': _rna(curve.driver), 'variables': [
                    {'settings': _rna(v), 'targets': [_rna(t) for t in v.targets]}
                    for v in curve.driver.variables]}}


def _animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    def strip(item):
        return {'settings': _rna(item), 'curves': [_curve(fc) for fc in item.fcurves],
                'strips': [strip(s) for s in item.strips]}
    return {'settings': _rna(ad), 'slot': ad.action_slot.identifier if ad.action_slot else None,
            'drivers': [_curve(fc) for fc in ad.drivers],
            'nla': [{'settings': _rna(t), 'strips': [strip(s) for s in t.strips]}
                    for t in ad.nla_tracks]}


def _action(action):
    return {'name': action.name, 'settings': _rna(action), 'props': _props(action),
            'slots': [_rna(s) for s in action.slots], 'layers': [
                {'settings': _rna(layer), 'strips': [
                    {'settings': _rna(strip), 'bags': [
                        {'slot_handle': bag.slot_handle, 'curves': [_curve(fc) for fc in bag.fcurves]}
                        for bag in getattr(strip, 'channelbags', ())]}
                    for strip in layer.strips]}
                for layer in action.layers]}


def _refresh():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def _template(revision=1):
    return {'schema': 'hrig.bone-collections.template.v1', 'template_id': 'fixture.collections',
            'revision': revision, 'roles': {
                'root': {'name': 'Managed Root', 'parent': None, 'is_visible': True, 'members': []},
                'hair': {'name': 'Managed Hair', 'parent': 'root', 'is_visible': True,
                         'members': ['control']}}, 'detach': {}}


def _check(manifest):
    if manifest.get('schema') != SCHEMA or manifest.get('case_id') not in CASE_IDS:
        raise ValueError('Unknown native collection fixture manifest')
    for name in manifest['objects']:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.get('hrig_collection_fixture') != manifest['fixture_id']:
            raise ValueError('Collection fixture identity changed: ' + name)


def create_case(case):
    if case not in CASE_IDS:
        raise ValueError('Unknown collection fixture case: ' + str(case))
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        raise ValueError('Fixture creation requires Object mode')
    prefix = 'HRIG_COLLECTION_' + case
    if bpy.data.objects.get(prefix + '_Rig'):
        raise ValueError('Fixture already exists; require a fresh owned worker')
    arm = bpy.data.armatures.new(prefix + '_Armature')
    rig = bpy.data.objects.new(prefix + '_Rig', arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    previous = None
    for index, name in enumerate(('Control', 'Other', 'Third')):
        bone = arm.edit_bones.new(name)
        bone.head, bone.tail = (0, 0, index), (0, 0, index + 1)
        if previous is not None:
            bone.parent, bone.use_connect = previous, True
        previous = bone
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.pose.bones['Control'].rotation_mode = 'XYZ'
    rig.pose.bones['Control'].rotation_euler.z = .125
    constraint = rig.pose.bones['Third'].constraints.new('LIMIT_ROTATION')
    constraint.use_limit_x, constraint.min_x, constraint.max_x = True, -.25, .25
    for bone in rig.pose.bones:
        bone['untouched_sentinel'] = 'pose-' + bone.name
    root = arm.collections.new('Original Root')
    defaults = _rna(root, ('name', 'child_number'))
    root.is_visible = False
    original = arm.collections.new('Original', parent=root)
    original['untouched_sentinel'] = 'original-protect'
    secondary = arm.collections.new('Secondary')
    for name in ('Control', 'Other'):
        original.assign(arm.bones[name])
    for name in ('Control', 'Third'):
        secondary.assign(arm.bones[name])
    secondary.is_solo = case == 'create_tree'
    arm.bones['Control'].hide = case == 'create_tree'
    arm.collections.active = original
    mesh_data = bpy.data.meshes.new(prefix + '_MeshData')
    mesh_data.from_pydata([(-.25, 0, .25), (.25, 0, .25), (.25, 0, 1.5), (-.25, 0, 1.5)], [], [(0, 1, 2, 3)])
    mesh_data.update()
    mesh = bpy.data.objects.new(prefix + '_Mesh', mesh_data)
    bpy.context.scene.collection.objects.link(mesh)
    mesh.vertex_groups.new(name='Control').add([0, 1], 1.0, 'REPLACE')
    mesh.vertex_groups.new(name='Other').add([2, 3], 1.0, 'REPLACE')
    mod = mesh.modifiers.new('FixtureArmature', 'ARMATURE')
    mod.object = rig
    basis = mesh.shape_key_add(name='Basis')
    key = mesh.shape_key_add(name='SentinelKey')
    key.data[0].co.x += .03125
    key.value = .125
    objects = [rig.name, mesh.name]
    if case == 'reject_shared_armature':
        shared = bpy.data.objects.new(prefix + '_SharedRig', arm)
        bpy.context.scene.collection.objects.link(shared)
        objects.append(shared.name)
    for name in objects:
        bpy.data.objects[name]['hrig_collection_fixture'] = prefix
        bpy.data.objects[name]['untouched_sentinel'] = {'number': 7, 'text': 'protect'}
    arm['untouched_sentinel'] = 'armature-protect'
    bpy.context.scene.frame_set(1)
    _refresh()
    return {'schema': SCHEMA, 'case_id': case, 'fixture_id': prefix, 'rig': rig.name,
            'mesh': mesh.name, 'objects': objects, 'collection_defaults': defaults,
            'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                        'frame': 1, 'subframe': 0.0},
            'inject_rollback': case.startswith('rollback_'),
            'expected': {'ok': case not in _REJECT, 'code': EXPECTED_CODES.get(case)},
            'oracle': 'Independent native RNA and literal expected graph; no addon merge or masking helpers',
            'scope': 'Synthetic three-bone collection structure; current evaluated geometry only, no GUI/continuous quality certification'}


def _request(core, rig, template, bindings, allow_bad=False):
    observed = core.inspect_collections(bpy.context, rig, template, bindings)
    if not observed.get('ok') and not allow_bad:
        raise AssertionError('Fixture precondition inspect failed: ' + repr(observed))
    return {'schema': 'hrig.bone-collections.apply.v1', 'instance_id': 'fixture.main',
            'template': copy.deepcopy(template), 'bindings': copy.deepcopy(bindings),
            'expected_snapshot_sha256': observed.get('snapshot_sha256', '0' * 64)}


def _base(core, rig, template, bindings):
    result = core.apply_collections(bpy.context, rig, _request(core, rig, template, bindings))
    if not result.get('ok'):
        raise AssertionError('Fixture setup apply failed: ' + repr(result))
    for role, row in template['roles'].items():
        col = rig.data.collections_all.get(row['name'])
        if col is None or sorted(b.name for b in col.bones) != sorted(bindings[x] for x in row['members']):
            raise AssertionError('Fixture setup differs from native literal graph: ' + role)


def prepare(manifest, core):
    _check(manifest)
    case, rig = manifest['case_id'], bpy.data.objects[manifest['rig']]
    template, bindings = _template(), {'control': 'Control'}
    generation, changed = 1, True
    fresh = {'create_tree', 'explicit_detach', 'non_target_memberships', 'reject_name_collision',
             'reject_bad_ledger', 'reject_shared_armature', 'reject_driver_reader',
             'reject_scene_alias_reader', 'reject_context_reader', 'reject_action_writer',
             'reject_future_name_reader', 'reject_future_scene_alias_reader',
             'reject_unbound_object_action', 'reject_unbound_scene_action',
             'rollback_create', 'reject_snapshot'}
    if case not in fresh:
        _base(core, rig, template, bindings)
        generation = 2
        if case == 'repeat':
            generation, changed = 1, False
        elif case == 'reject_downgrade':
            _base(core, rig, _template(2), bindings)
        elif case == 'reject_same_revision':
            template['roles']['hair']['is_visible'] = False
        else:
            template['revision'] = 2
            if case in {'update_rename', 'rollback_update'}:
                template['roles']['hair']['name'] = 'Managed Hair v2'
                template['roles']['hair']['is_visible'] = False
            elif case == 'hand_name_preserve':
                rig.data.collections_all['Managed Hair'].name = 'Hand Hair'
                template['roles']['hair']['is_visible'] = False
            elif case == 'same_target':
                rig.data.collections_all['Managed Hair'].name = 'Shared Choice'
                template['roles']['hair']['name'] = 'Shared Choice'
            elif case == 'member_move':
                template['roles']['root']['members'] = ['control']
                template['roles']['hair']['members'] = []
            elif case == 'hand_extra_member_preserve':
                rig.data.collections_all['Managed Hair'].assign(rig.data.bones['Other'])
                template['roles']['hair']['is_visible'] = False
            elif case == 'hand_reparent_preserve':
                rig.data.collections_all['Managed Hair'].parent = rig.data.collections_all['Secondary']
                template['roles']['hair']['is_visible'] = False
            elif case == 'reject_role_change':
                template['roles']['replacement'] = template['roles'].pop('hair')
            elif case == 'reject_bindings_change':
                bindings['control'] = 'Other'
            elif case == 'reject_parents_change':
                template['roles']['hair']['parent'] = None
            elif case == 'reject_detach_change':
                template['detach'] = {'control': ['Original']}
            elif case == 'reject_bad_owner':
                rig.data.collections_all['Managed Hair'][OWNER] = '{broken-owner'
            elif case == 'reject_missing_owned':
                rig.data.collections.remove(rig.data.collections_all['Managed Hair'])
            elif case == 'reject_name_conflict':
                rig.data.collections_all['Managed Hair'].name = 'Hand Hair'
                template['roles']['hair']['name'] = 'Competing Hair'
    if case == 'explicit_detach':
        template['detach'] = {'control': ['Original']}
    if case == 'reject_bad_ledger':
        rig[LEDGER] = '{broken-ledger'
    if case == 'reject_name_collision':
        rig.data.collections.new('Managed Hair')
    request = _request(core, rig, template, bindings, allow_bad=case in _REJECT)
    if case == 'reject_action_writer':
        if not rig.data.collections_all['Original'].keyframe_insert(data_path='is_visible', frame=1):
            raise AssertionError('Native collection Action key insertion failed')
    if case in {'reject_unbound_object_action', 'reject_unbound_scene_action'}:
        owner = rig if case == 'reject_unbound_object_action' else bpy.context.scene
        owner['unbound_action_sentinel'] = 1.0
        if not owner.keyframe_insert(data_path='["unbound_action_sentinel"]', frame=1):
            raise AssertionError('Native unbound Action fixture key insertion failed')
        action = owner.animation_data.action
        slot = owner.animation_data.action_slot
        required_type = 'OBJECT' if owner == rig else 'SCENE'
        if slot is None or slot.target_id_type != required_type:
            raise AssertionError('Unbound Action fixture lacks the intended native slot ID type')
        curves = [curve for layer in action.layers for strip in layer.strips
                  for bag in getattr(strip, 'channelbags', ()) for curve in bag.fcurves]
        if len(curves) != 1:
            raise AssertionError('Unbound Action fixture needs one independently identified FCurve')
        path = 'data.collections_all["Managed Root"].is_visible'
        if owner != rig:
            path = 'objects[' + json.dumps(rig.name) + '].' + path
        curves[0].data_path = path
        try:
            owner.path_resolve(path)
        except (ValueError, AttributeError, KeyError):
            pass
        else:
            raise AssertionError('Unbound future Action path must not resolve before requested creation')
        action.use_fake_user = True
        action_name = action.name
        owner.animation_data_clear()
        if (owner.animation_data is not None or bpy.data.actions.get(action_name) != action
                or not action.use_fake_user):
            raise AssertionError('Unbound native Action was lost or remained bound')
        manifest['unbound_action'] = {'name': action_name, 'slot_id_type': required_type,
                                      'path': path, 'original_slot': slot.identifier}
    if case in {'reject_driver_reader', 'reject_scene_alias_reader', 'reject_context_reader',
                'reject_future_name_reader', 'reject_future_scene_alias_reader'}:
        reader = bpy.data.objects.new(manifest['fixture_id'] + '_Reader', None)
        bpy.context.scene.collection.objects.link(reader)
        reader['hrig_collection_fixture'] = manifest['fixture_id']
        manifest['objects'].append(reader.name)
        curve = reader.driver_add('location', 0)
        curve.driver.type, curve.driver.expression = 'SCRIPTED', 'collection_flag'
        var = curve.driver.variables.new()
        var.name = 'collection_flag'
        target = var.targets[0]
        future_name = case in {'reject_future_name_reader', 'reject_future_scene_alias_reader'}
        scene_alias = case in {'reject_scene_alias_reader', 'reject_context_reader',
                               'reject_future_scene_alias_reader'}
        native_path = ('data.collections_all["Managed Root"].is_visible' if future_name else
                       'data.collections_all[' + str(rig.data.collections_all['Original'].index) + '].is_visible')
        if case == 'reject_context_reader':
            var.type = 'CONTEXT_PROP'
            target.context_property = 'ACTIVE_SCENE'
        else:
            var.type = 'SINGLE_PROP'
            target.id_type = 'SCENE' if scene_alias else 'OBJECT'
            target.id = bpy.context.scene if scene_alias else rig
        if scene_alias:
            native_path = 'objects[' + json.dumps(rig.name) + '].' + native_path
        root = bpy.context.scene if scene_alias else rig
        if future_name:
            if rig.data.collections_all.get('Managed Root') is not None:
                raise AssertionError('Future-name reader must start with no named collection')
            try:
                root.path_resolve(native_path)
            except (ValueError, AttributeError, KeyError):
                pass
            else:
                raise AssertionError('Future-name reader must have an invalid leaf before apply')
            # The container is nevertheless native and belongs to the target
            # Armature. Creating the requested name would activate this reader.
            container_path = native_path.rpartition('["Managed Root"]')[0]
            root.path_resolve(container_path)
            armature_path = container_path.rpartition('.collections_all')[0]
            if root.path_resolve(armature_path) != rig.data:
                raise AssertionError('Future-name reader container is not the target Armature')
        elif root.path_resolve(native_path) != rig.data.collections_all['Original'].is_visible:
            raise AssertionError('Fixture collection index path failed native resolution')
        target.data_path = native_path
    if case == 'reject_snapshot':
        request['expected_snapshot_sha256'] = '0' * 64
    _refresh()
    manifest['request'] = copy.deepcopy(request)
    manifest['expected'].update({'changed': changed, 'generation': generation,
                                 'revision': template['revision']})
    if manifest['inject_rollback']:
        manifest['runtime_injection'] = {
            'kind': 'one_shot_after_native_write', 'core_helper': '_mutate', 'after_successful_call': 1,
            'message': 'fixture injected after first completed collection mutation'}
    return request


def _collection_system(arm):
    rows = {}
    for col in arm.collections_all:
        rows[col.name] = {'name': col.name, 'parent': col.parent.name if col.parent else None,
            'children': [c.name for c in col.children], 'members': sorted(b.name for b in col.bones),
            'settings': _rna(col, ('name', 'child_number')), 'props': _props(col),
            'index': col.index, 'child_number': col.child_number}
    return {'rows': rows, 'all_order': [c.name for c in arm.collections_all],
            'root_order': [c.name for c in arm.collections],
            'memberships': {b.name: sorted(c.name for c in b.collections) for b in arm.bones},
            'active': arm.collections.active.name if arm.collections.active else None,
            'active_name': arm.collections.active_name, 'active_index': arm.collections.active_index}


def _derived(arm):
    return {'solo_active': arm.collections.is_solo_active,
            'collections': {c.name: {'is_visible_ancestors': c.is_visible_ancestors,
                'is_visible_effectively': c.is_visible_effectively,
                'bones_recursive': sorted(b.name for b in c.bones_recursive)} for c in arm.collections_all},
            'bone_visibility_observation': {b.name: {
                'hide': b.hide, 'memberships_or_effective': any(c.is_visible_effectively for c in b.collections),
                'has_memberships': bool(b.collections)} for b in arm.bones},
            'scope': 'Native collection derived flags and membership OR; this is not a GUI draw/selection acceptance'}


def capture(manifest):
    _check(manifest)
    graph = _refresh()
    rig = bpy.data.objects[manifest['rig']]
    objects = []
    meshes = {}
    for name in sorted(manifest['objects']):
        obj = bpy.data.objects[name]
        row = {'id': _id(obj), 'settings': _rna(obj, ('dimensions',) if obj == rig else ()),
               'props': _props(obj, (LEDGER,) if obj == rig else ()), 'world': _plain(obj.matrix_world),
               'animation': _animation(obj), 'constraints': [_rna(c) for c in obj.constraints],
               'modifiers': [_rna(m) for m in obj.modifiers],
               'groups': [{'name': g.name, 'index': g.index, 'lock_weight': g.lock_weight}
                          for g in obj.vertex_groups]}
        if obj.type == 'ARMATURE':
            row['pose'] = [{'name': p.name, 'settings': _rna(p), 'props': _props(p),
                            'matrix': _plain(p.matrix), 'constraints': [_rna(c) for c in p.constraints],
                            'custom_shape_transform': p.custom_shape_transform.name if p.custom_shape_transform else None}
                           for p in obj.pose.bones]
        if obj.type == 'MESH':
            data = obj.data
            meshes[data.name] = {'id': _id(data), 'props': _props(data),
                'vertices': [_plain(v.co) for v in data.vertices],
                'edges': [_plain(e.vertices) for e in data.edges],
                'polygons': [_plain(p.vertices) for p in data.polygons],
                'weights': [[{'group': g.group, 'weight': g.weight} for g in v.groups] for v in data.vertices],
                'keys': None if data.shape_keys is None else {'settings': _rna(data.shape_keys),
                    'props': _props(data.shape_keys), 'blocks': [
                        {'name': k.name, 'settings': _rna(k), 'data': [_plain(v.co) for v in k.data]}
                        for k in data.shape_keys.key_blocks]}}
            evaluated = obj.evaluated_get(graph)
            native = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
            try:
                row['evaluated_world'] = [_plain(evaluated.matrix_world @ v.co) for v in native.vertices]
            finally:
                evaluated.to_mesh_clear()
        objects.append(row)
    arm = rig.data
    return {'schema': 'hrig.bone-collections.fixture-observation.v1',
            'collection_system': _collection_system(arm), 'derived': _derived(arm),
            'ledger': rig.get(LEDGER), 'objects': objects, 'meshes': meshes,
            'armature': {'id': _id(arm), 'settings': _rna(arm), 'props': _props(arm),
                'animation': _animation(arm), 'bones': [
                    {'name': b.name, 'parent': b.parent.name if b.parent else None,
                     'settings': _rna(b), 'props': _props(b), 'rest': _plain(b.matrix_local),
                     'head': _plain(b.head_local), 'tail': _plain(b.tail_local)} for b in arm.bones]},
            'actions': [_action(a) for a in bpy.data.actions],
            'scene': {'id': _id(bpy.context.scene), 'props': _props(bpy.context.scene),
                      'animation': _animation(bpy.context.scene)},
            'derived_rig_dimensions': _plain(rig.dimensions),
            'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe,
                'mode': bpy.context.mode, 'active': bpy.context.view_layer.objects.active.name
                if bpy.context.view_layer.objects.active else None,
                'selected': sorted(o.name for o in bpy.context.selected_objects)}}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _canonical_template(template):
    answer = copy.deepcopy(template)
    for row in answer['roles'].values():
        row['members'] = sorted(row['members'])
    for key in answer['detach']:
        answer['detach'][key] = sorted(answer['detach'][key])
    return answer


def _owner_role(row):
    raw = row['props'].get(OWNER)
    if raw is None:
        return None
    value = json.loads(raw)
    if value.get('instance_id') == 'fixture.main':
        return value['role']
    return None


def validate(manifest, before, after, result):
    checks = []
    def check(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok), **({'detail': detail} if detail is not None else {})})
    expected = manifest['expected']
    check('result_ok_matches_case', result.get('ok') is expected['ok'])
    if not expected['ok']:
        check('rejection_preserves_complete_native_observation', before == after)
        check('structured_error_present', isinstance(result.get('error'), dict)
              and bool(result['error'].get('code')) and bool(result['error'].get('message')))
        check('expected_rejection_code', result.get('error', {}).get('code') == expected['code'])
        if manifest['inject_rollback']:
            check('rollback_fault_origin', 'fixture injected after first completed collection mutation'
                  in result.get('error', {}).get('message', ''))
        return {'ok': all(c['ok'] for c in checks), 'checks': checks,
                'observed_error_code': result.get('error', {}).get('code')}
    check('reported_changed', result.get('changed') is expected['changed'])
    excluded = {'collection_system', 'ledger', 'derived', 'derived_rig_dimensions'}
    check('rest_pose_weights_constraints_keys_and_other_native_structures_exact',
          {k: v for k, v in before.items() if k not in excluded}
          == {k: v for k, v in after.items() if k not in excluded})
    bsys, asys = before['collection_system'], after['collection_system']
    template = manifest['request']['template']
    # Literal fixture outcomes: deliberately independent of production merger.
    role_names = {role: row['name'] for role, row in template['roles'].items()}
    if manifest['case_id'] == 'hand_name_preserve':
        role_names['hair'] = 'Hand Hair'
    role_before = {role: row for row in bsys['rows'].values()
                   if (role := _owner_role(row)) is not None}
    rename = {row['name']: role_names[role] for role, row in role_before.items()}
    def renamed(name):
        return rename.get(name, name)
    wanted = {}
    for name, old in bsys['rows'].items():
        row = copy.deepcopy(old)
        row['name'] = renamed(name)
        row['parent'] = renamed(row['parent'])
        row['children'] = [renamed(n) for n in row['children']]
        wanted[row['name']] = row
    fresh_roles = [r for r in template['roles'] if r not in role_before]
    # Both fresh collections are created root before child regardless of role sorting.
    fresh_roles.sort(key=lambda r: (template['roles'][r]['parent'] is not None, r))
    for role in fresh_roles:
        spec = template['roles'][role]
        name = role_names[role]
        parent = role_names[spec['parent']] if spec['parent'] else None
        wanted[name] = {'name': name, 'parent': parent, 'children': [], 'members': [],
                        'settings': copy.deepcopy(manifest['collection_defaults']),
                        'props': {OWNER: {'schema': 'hrig.bone-collections.owner.v1',
                                         'instance_id': 'fixture.main', 'role': role}}}
        # _rna retains null pointer defaults but omits non-ID RNA pointers.
        # The explicit row['parent'] above remains the strict hierarchy oracle.
        if parent is not None:
            wanted[name]['settings'].pop('parent', None)
        if parent is not None:
            wanted[parent]['children'].append(name)
    for role, spec in template['roles'].items():
        row = wanted[role_names[role]]
        row['settings']['is_visible'] = spec['is_visible']
        # Only bound bone membership pairs are managed. Other hand additions survive.
        bound = set(manifest['request']['bindings'].values())
        row['members'] = sorted((set(row['members']) - bound)
                                | {manifest['request']['bindings'][r] for r in spec['members']})
    for member, names in template['detach'].items():
        bone = manifest['request']['bindings'][member]
        for name in names:
            wanted[name]['members'] = [n for n in wanted[name]['members'] if n != bone]
    actual = copy.deepcopy(asys['rows'])
    for row in actual.values():
        row.pop('index', None)
        row.pop('child_number', None)
        if OWNER in row['props']:
            try:
                row['props'][OWNER] = json.loads(row['props'][OWNER])
            except (ValueError, TypeError):
                pass
    for row in wanted.values():
        row.pop('index', None)
        row.pop('child_number', None)
        if isinstance(row['props'].get(OWNER), str):
            row['props'][OWNER] = json.loads(row['props'][OWNER])
    check('complete_collection_rows_match_literal_native_oracle', actual == wanted,
          None if actual == wanted else {'expected': wanted, 'actual': actual})
    expected_memberships = {bone: sorted(name for name, row in wanted.items() if bone in row['members'])
                            for bone in bsys['memberships']}
    check('every_bone_every_membership_exact', asys['memberships'] == expected_memberships)
    expected_roots = [renamed(n) for n in bsys['root_order']]
    expected_roots += [role_names[r] for r in fresh_roles if template['roles'][r]['parent'] is None]
    check('root_order_exact', asys['root_order'] == expected_roots)
    check('preexisting_flat_relative_order_exact',
          [n for n in asys['all_order'] if n in {renamed(x) for x in bsys['all_order']}]
          == [renamed(n) for n in bsys['all_order']])
    check('active_collection_identity_and_name_preserved',
          asys['active'] == renamed(bsys['active']) and asys['active_name'] == renamed(bsys['active_name']))
    check('active_index_matches_native_array',
          asys['active_index'] == (asys['all_order'].index(asys['active']) if asys['active'] else -1))
    check('all_native_indices_match_array', all(asys['rows'][n]['index'] == i
                                               for i, n in enumerate(asys['all_order'])))
    check('all_child_numbers_match_sibling_order', all(
        asys['rows'][n]['child_number'] == i
        for siblings in [asys['root_order']] + [r['children'] for r in asys['rows'].values()]
        for i, n in enumerate(siblings)))
    derived = after['derived']
    solo = any(r['settings']['is_solo'] for r in asys['rows'].values())
    check('solo_active_matches_complete_native_flags', derived['solo_active'] is solo)
    # Native flag computation is bounded to the documented ancestor conjunction
    # and collection-level solo override; no GUI visibility claim is inferred.
    ancestor_flags = {}
    def ancestors(name, seen=()):
        if name in seen:
            raise ValueError('Cycle in native collection parent graph')
        parent = asys['rows'][name]['parent']
        return True if parent is None else (asys['rows'][parent]['settings']['is_visible']
                                            and ancestors(parent, (*seen, name)))
    for name in asys['rows']:
        ancestor_flags[name] = bool(ancestors(name))
    check('native_ancestor_visibility_matches_independent_parent_walk', all(
        derived['collections'][n]['is_visible_ancestors'] is ancestor_flags[n] for n in asys['rows']))
    check('membership_or_observation_matches_complete_graph', all(
        value['memberships_or_effective'] == any(derived['collections'][n]['is_visible_effectively']
                                                for n in asys['memberships'][bone])
        for bone, value in derived['bone_visibility_observation'].items()))
    try:
        book = json.loads(after['ledger'])
        instance = book['instances']['fixture.main']
        literal = _canonical_template(template)
        check('ledger_exact_root', set(book) == {'schema', 'instances'} and
              book['schema'] == 'hrig.bone-collections.instances.v1')
        check('ledger_exact_instance_fields', set(instance) == {'template', 'bindings', 'template_sha256', 'generation'})
        check('ledger_exact_literal_template', instance['template'] == literal)
        check('ledger_exact_bindings', instance['bindings'] == manifest['request']['bindings'])
        check('ledger_generation', instance['generation'] == expected['generation'])
        check('ledger_literal_digest', instance['template_sha256'] == _digest(literal))
        old = json.loads(before['ledger']) if before['ledger'] is not None else {'instances': {}}
        check('other_instances_unchanged',
              {k: v for k, v in old['instances'].items() if k != 'fixture.main'}
              == {k: v for k, v in book['instances'].items() if k != 'fixture.main'})
        check('no_extra_instances', set(book['instances']) == set(old['instances']) | {'fixture.main'})
        if manifest['case_id'] == 'repeat':
            check('repeat_exact_ledger_bytes', before['ledger'] == after['ledger'])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        check('valid_literal_ledger', False, type(exc).__name__ + ': ' + str(exc))
    return {'ok': all(c['ok'] for c in checks), 'checks': checks}
