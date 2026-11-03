"""Owned control-style fixtures with an addon-independent native oracle.

The caller owns core calls, fault injection, persistence and cold reads.  This
module never imports the addon or its observation/masking implementation.
"""
import copy
import hashlib
import json
import math
import struct

import bpy


SCHEMA = 'hrig.control-style.fixture.v1'
LEDGER = 'hrig_style_instances_v1'
CASE_IDS = (
    'create_all_fields', 'create_palette', 'repeat', 'repeat_hand_edit', 'update',
    'preserve_hand_edit', 'same_target', 'custom_palette_bundle',
    'reject_conflict', 'reject_downgrade', 'reject_same_revision',
    'reject_role_change', 'reject_field_change', 'reject_rebind',
    'reject_duplicate_binding', 'reject_shared_armature',
    'reject_action_writer', 'reject_driver_writer', 'reject_driver_reader',
    'reject_scene_alias_reader', 'reject_context_reader',
    'reject_bad_ledger', 'reject_instance_overlap', 'allow_disjoint_instances',
    'reject_ledger_instance_budget', 'reject_generation_budget',
    'rollback_injected', 'reject_snapshot', 'unselected_animation_positive',
)
FIELDS = (
    'bone.color.palette', 'bone.color.custom.normal',
    'bone.color.custom.select', 'bone.color.custom.active',
    'pose.color.palette', 'pose.color.custom.normal',
    'pose.color.custom.select', 'pose.color.custom.active',
    'pose.custom_shape_scale_xyz', 'pose.custom_shape_translation',
    'pose.use_custom_shape_bone_size', 'bone.show_wire',
)
_VECTOR_FIELDS = frozenset(x for x in FIELDS if '.custom.' in x) | {
    'pose.custom_shape_scale_xyz', 'pose.custom_shape_translation'}
_REJECTIONS = frozenset(x for x in CASE_IDS if x.startswith('reject_')) | {'rollback_injected'}
EXPECTED_CODES = {
    'reject_conflict': 'MERGE_CONFLICT',
    'reject_downgrade': 'REVISION_CONFLICT',
    'reject_same_revision': 'REVISION_CONFLICT',
    'reject_role_change': 'STRUCTURE_CHANGE',
    'reject_field_change': 'STRUCTURE_CHANGE',
    'reject_rebind': 'STRUCTURE_CHANGE',
    'reject_duplicate_binding': 'STYLE_BINDINGS',
    'reject_shared_armature': 'SHARED_ARMATURE',
    'reject_action_writer': 'STYLE_CONSUMER',
    'reject_driver_writer': 'STYLE_CONSUMER',
    'reject_driver_reader': 'STYLE_CONSUMER',
    'reject_scene_alias_reader': 'STYLE_CONSUMER',
    'reject_context_reader': 'STYLE_CONSUMER',
    'reject_bad_ledger': 'STYLE_INVALID',
    'reject_instance_overlap': 'INSTANCE_OVERLAP',
    'reject_ledger_instance_budget': 'LEDGER_BUDGET',
    'reject_generation_budget': 'LEDGER_BUDGET',
    'reject_snapshot': 'SNAPSHOT_CONFLICT',
    'rollback_injected': 'STYLE_INVALID',
}
_BASE_FIELDS = {
    'bone.color.palette': 'CUSTOM',
    'bone.color.custom.normal': [.125, .25, .5],
    'bone.color.custom.select': [.25, .5, .75],
    'bone.color.custom.active': [.5, .75, 1.0],
    'pose.color.palette': 'CUSTOM',
    'pose.color.custom.normal': [.5, .25, .125],
    'pose.color.custom.select': [.75, .5, .25],
    'pose.color.custom.active': [1.0, .75, .5],
    'pose.custom_shape_scale_xyz': [1.25, 1.5, 1.75],
    'pose.custom_shape_translation': [.125, -.25, .5],
    'pose.use_custom_shape_bone_size': False,
    'bone.show_wire': True,
}


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'items'):
        return {str(key): _plain(item) for key, item in value.items()}
    return [_plain(item) for item in value]


def _f32(value):
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


def _canon_field(name, value):
    if '.color.custom.' in name:
        # Independently reproduce observed native ThemeBoneColorSet byte
        # storage.  This is not linear float32 storage: Blender rounds a
        # float32 multiply/add to a byte, then its getter multiplies by the
        # float32 reciprocal.  Preserve both float32 roundings at half steps.
        return [_f32(math.floor(_f32(_f32(_f32(x) * 255.0) + .5)) * _f32(1.0 / 255.0))
                for x in value]
    return [_f32(x) for x in value] if name in _VECTOR_FIELDS else value


def _props(owner, exclude=()):
    try:
        keys = sorted(owner.keys())
    except TypeError:
        # Some non-ID RNA structs (notably ArmatureModifier) expose keys()
        # but explicitly reject IDProperty access.  Real IDs and supported
        # nested owners still retain every exact custom-property value.
        return {'__idproperties_unsupported__': owner.bl_rna.identifier}
    return {key: _plain(owner[key]) for key in keys if key not in exclude}


def _id(value):
    return None if value is None else {
        'name': value.name, 'type': value.bl_rna.identifier,
        'library': value.library.filepath if value.library else None}


def _read_field(rig, bone_name, name):
    parts = name.split('.')
    value = rig.data.bones[bone_name] if parts[0] == 'bone' else rig.pose.bones[bone_name]
    for part in parts[1:]:
        value = getattr(value, part)
    return _plain(value)


def _write_field(rig, bone_name, name, value):
    parts = name.split('.')
    owner = rig.data.bones[bone_name] if parts[0] == 'bone' else rig.pose.bones[bone_name]
    for part in parts[1:-1]:
        owner = getattr(owner, part)
    setattr(owner, parts[-1], value)


def _fields(owner, names):
    return {name: _plain(getattr(owner, name)) for name in names if hasattr(owner, name)}


def _native_rna(owner, exclude=()):
    """Native writable scalar/array/enum and real ID references, no core helper."""
    result = {}
    for prop in owner.bl_rna.properties:
        key = prop.identifier
        if key in exclude or key == 'rna_type' or prop.is_readonly:
            continue
        value = getattr(owner, key)
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
            result[key] = sorted(value) if isinstance(value, set) else _plain(value)
        elif prop.type == 'POINTER' and (value is None or isinstance(value, bpy.types.ID)):
            result[key] = _id(value)
    return result


def _curve(fc):
    return {'settings': _native_rna(fc),
            'keys': [_native_rna(k) for k in fc.keyframe_points],
            'modifiers': [_native_rna(m) for m in fc.modifiers],
            'driver': {'settings': _native_rna(fc.driver),
                       'variables': [{'settings': _native_rna(v),
                                      'targets': [_native_rna(t) for t in v.targets]}
                                     for v in fc.driver.variables]}
            if fc.driver is not None else None}


def _animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    return {'settings': _native_rna(ad),
            'slot': ad.action_slot.identifier if ad.action_slot else None,
            'drivers': [_curve(fc) for fc in ad.drivers],
            'nla': [{'settings': _native_rna(track),
                     'strips': [_native_rna(strip) for strip in track.strips]}
                    for track in ad.nla_tracks]}


def _action(action):
    layers = []
    for layer in action.layers:
        strips = []
        for strip in layer.strips:
            bags = []
            for slot in action.slots:
                bag = strip.channelbag(slot, ensure=False)
                if bag is not None:
                    bags.append({'slot': slot.identifier, 'curves': [_curve(fc) for fc in bag.fcurves]})
            strips.append({'settings': _native_rna(strip), 'channelbags': bags})
        layers.append({'settings': _native_rna(layer), 'strips': strips})
    return {'name': action.name, 'settings': _native_rna(action), 'props': _props(action),
            'slots': [_native_rna(slot) for slot in action.slots], 'layers': layers}


def _template(fields=None, revision=1, roles=None):
    return {'schema': 'hrig.control-style.template.v1', 'template_id': 'fixture.controls',
            'revision': revision,
            'roles': copy.deepcopy(roles if roles is not None else {
                'head': {'fields': fields if fields is not None else _BASE_FIELDS}})}


def _refresh():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def _check(manifest):
    if manifest.get('schema') != SCHEMA or manifest.get('case_id') not in CASE_IDS:
        raise ValueError('Unknown native style fixture manifest')
    for role in ('rig', 'mesh', 'widget'):
        obj = bpy.data.objects.get(manifest[role])
        if obj is None or obj.get('hrig_style_fixture') != manifest['fixture_id']:
            raise ValueError('Style fixture identity changed: ' + role)


def create_case(case):
    if case not in CASE_IDS:
        raise ValueError('Unknown style fixture case: ' + str(case))
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        raise ValueError('Fixture creation requires Object mode')
    prefix = 'HRIG_STYLE_' + case
    if bpy.data.objects.get(prefix + '_Rig'):
        raise ValueError('Fixture already exists; use a fresh owned worker')
    armature = bpy.data.armatures.new(prefix + '_Armature')
    rig = bpy.data.objects.new(prefix + '_Rig', armature)
    bpy.context.scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    a = armature.edit_bones.new('Control')
    a.head, a.tail = (0, 0, 0), (0, 0, 1)
    b = armature.edit_bones.new('Other')
    b.head, b.tail = (0, 0, 1), (0, 0, 2)
    b.parent, b.use_connect = a, True
    if case == 'reject_ledger_instance_budget':
        for index in range(64):
            bone = armature.edit_bones.new('Budget' + str(index).zfill(2))
            bone.head, bone.tail = (index + 1, 0, 0), (index + 1, 0, .5)
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.pose.bones['Control'].rotation_mode = 'XYZ'
    rig.pose.bones['Control'].rotation_euler.z = .125
    widget_data = bpy.data.meshes.new(prefix + '_WidgetData')
    widget_data.from_pydata([(-.5, 0, 0), (.5, 0, 0), (0, .5, 0)], [(0, 1), (1, 2), (2, 0)], [])
    widget = bpy.data.objects.new(prefix + '_Widget', widget_data)
    bpy.context.scene.collection.objects.link(widget)
    widget.hide_render = True
    for pb in rig.pose.bones:
        pb.custom_shape = widget
        pb.custom_shape_transform = rig.pose.bones['Other']
        pb['fixture_sentinel'] = 'pose-' + pb.name
    mesh_data = bpy.data.meshes.new(prefix + '_MeshData')
    mesh_data.from_pydata([(-.25, 0, .25), (.25, 0, .25), (.25, 0, 1.5), (-.25, 0, 1.5)], [], [(0, 1, 2, 3)])
    mesh_data.update()
    mesh = bpy.data.objects.new(prefix + '_Mesh', mesh_data)
    bpy.context.scene.collection.objects.link(mesh)
    mesh.vertex_groups.new(name='Control').add([0, 1], 1.0, 'REPLACE')
    mesh.vertex_groups.new(name='Other').add([2, 3], 1.0, 'REPLACE')
    mod = mesh.modifiers.new('FixtureArmature', 'ARMATURE')
    mod.object = rig
    objects = [rig.name, mesh.name, widget.name]
    if case == 'reject_shared_armature':
        shared = bpy.data.objects.new(prefix + '_SharedRig', armature)
        bpy.context.scene.collection.objects.link(shared)
        objects.append(shared.name)
    for name in objects:
        bpy.data.objects[name]['hrig_style_fixture'] = prefix
        bpy.data.objects[name]['untouched_sentinel'] = {'integer': 7, 'text': 'protect'}
    armature['untouched_sentinel'] = 'armature-protect'
    bpy.context.scene.frame_set(1)
    _refresh()
    return {'schema': SCHEMA, 'case_id': case, 'fixture_id': prefix,
            'rig': rig.name, 'mesh': mesh.name, 'widget': widget.name, 'objects': objects,
            'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                        'frame': 1, 'subframe': 0.0},
            'inject_rollback': case == 'rollback_injected',
            'expected': {'ok': case not in _REJECTIONS, 'code': EXPECTED_CODES.get(case)},
            'oracle': 'independent native RNA, literal requested fields, exact non-target equality; no addon capture or masks',
            'scope': ('synthetic 66-bone ledger-budget fixture; does not certify human appearance or continuous animation'
                      if case == 'reject_ledger_instance_budget' else
                      'synthetic two-bone fixture; does not certify human appearance or continuous animation')}


def _inspected_request(core, rig, template, bindings, instance='fixture.main', allow_bad=False):
    observed = core.inspect_styles(bpy.context, rig, template, bindings)
    if not observed.get('ok') and not allow_bad:
        raise AssertionError('Fixture precondition inspect failed: ' + repr(observed))
    return {'schema': 'hrig.control-style.apply.v1', 'instance_id': instance,
            'template': copy.deepcopy(template), 'bindings': copy.deepcopy(bindings),
            'expected_snapshot_sha256': observed.get('snapshot_sha256', '0' * 64)}


def _apply_precondition(core, rig, template, bindings, instance='fixture.main'):
    request = _inspected_request(core, rig, template, bindings, instance)
    result = core.apply_style(bpy.context, rig, request)
    if not result.get('ok'):
        raise AssertionError('Fixture setup apply failed: ' + repr(result))
    # Core-computed expectations are deliberately not used for this assertion.
    for role, binding in bindings.items():
        for name, value in template['roles'][role]['fields'].items():
            if _read_field(rig, binding, name) != _canon_field(name, value):
                raise AssertionError('Fixture setup native field differs: ' + name)


def prepare(manifest, core):
    _check(manifest)
    case, rig = manifest['case_id'], bpy.data.objects[manifest['rig']]
    binding = {'head': 'Control'}
    template = _template()
    generation = 1
    expected_changed = True
    if case in {'create_palette', 'unselected_animation_positive'}:
        template = _template({'pose.color.palette': 'THEME03'})
    if case == 'custom_palette_bundle':
        template = _template({key: value for key, value in _BASE_FIELDS.items() if key.startswith('pose.color.')})
    needs_base = case in {
        'repeat', 'repeat_hand_edit', 'update', 'preserve_hand_edit', 'same_target', 'reject_conflict',
        'reject_downgrade', 'reject_same_revision', 'reject_role_change',
        'reject_field_change', 'reject_rebind', 'reject_instance_overlap',
        'allow_disjoint_instances', 'reject_generation_budget'}
    if needs_base:
        if case == 'allow_disjoint_instances':
            other = _template({'bone.show_wire': True})
            other['template_id'] = 'fixture.other'
            _apply_precondition(core, rig, other, {'head': 'Other'}, 'fixture.other')
        else:
            _apply_precondition(core, rig, template, binding)
            generation = 2
        if case in {'repeat', 'repeat_hand_edit'}:
            expected_changed, generation = False, 1
            if case == 'repeat_hand_edit':
                _write_field(rig, 'Control', 'pose.custom_shape_scale_xyz', [2.0, 2.0, 2.0])
        elif case == 'reject_downgrade':
            newer = _template(revision=2)
            _apply_precondition(core, rig, newer, binding)
        elif case == 'reject_same_revision':
            template['roles']['head']['fields']['pose.custom_shape_translation'] = [.5, .5, .5]
        elif case == 'reject_role_change':
            template = _template(revision=2, roles={'renamed': {'fields': _BASE_FIELDS}})
            binding = {'renamed': 'Control'}
        elif case == 'reject_field_change':
            template = _template({'pose.color.palette': 'THEME03'}, revision=2)
        elif case == 'reject_rebind':
            template['revision'], binding = 2, {'head': 'Other'}
        elif case == 'reject_instance_overlap':
            template['template_id'] = 'fixture.overlap'
        elif case == 'reject_generation_budget':
            book = json.loads(rig[LEDGER])
            book['instances']['fixture.main']['generation'] = 1000000
            rig[LEDGER] = json.dumps(book, sort_keys=True, separators=(',', ':'))
            template['revision'] = 2
            template['roles']['head']['fields']['pose.custom_shape_translation'] = [.25, -.5, .75]
        elif case != 'allow_disjoint_instances':
            template['revision'] = 2
            template['roles']['head']['fields']['pose.custom_shape_translation'] = [.25, -.5, .75]
            if case in {'preserve_hand_edit', 'reject_conflict', 'same_target'}:
                hand = ([2.0, 2.0, 2.0] if case == 'preserve_hand_edit' else [.5, .5, .5])
                field = ('pose.custom_shape_scale_xyz' if case == 'preserve_hand_edit'
                         else 'pose.custom_shape_translation')
                _write_field(rig, 'Control', field, hand)
                if case == 'same_target':
                    template['roles']['head']['fields'][field] = hand
    if case == 'reject_duplicate_binding':
        template['roles']['another'] = {'fields': copy.deepcopy(_BASE_FIELDS)}
        binding['another'] = 'Control'
    if case == 'reject_bad_ledger':
        rig[LEDGER] = '{broken-json'
    if case == 'reject_ledger_instance_budget':
        for index in range(64):
            budget_template = _template({'bone.show_wire': True})
            budget_template['template_id'] = 'fixture.budget.' + str(index)
            _apply_precondition(core, rig, budget_template,
                                {'head': 'Budget' + str(index).zfill(2)},
                                'fixture.budget.' + str(index))
        if len(json.loads(rig[LEDGER])['instances']) != 64:
            raise AssertionError('Fixture setup must contain exactly 64 legal, disjoint instances')
    # Select the final snapshot before adding writer/reader consumers.  These
    # consumers do not mutate selected fields; they must be rejected on their
    # native identities rather than merely causing a stale expected snapshot.
    request = _inspected_request(core, rig, template, binding,
                                 'fixture.overlap' if case == 'reject_instance_overlap' else 'fixture.main',
                                 allow_bad=case in _REJECTIONS)
    if case in {'reject_action_writer', 'unselected_animation_positive'}:
        bone = rig.pose.bones['Control' if case == 'reject_action_writer' else 'Other']
        if not bone.keyframe_insert(data_path='custom_shape_scale_xyz', index=0, frame=1):
            raise AssertionError('Native Action fixture key insertion failed')
    if case == 'reject_driver_writer':
        curve = rig.pose.bones['Control'].driver_add('custom_shape_scale_xyz', 0)
        curve.driver.type, curve.driver.expression = 'SCRIPTED', '1.0'
    if case in {'reject_driver_reader', 'reject_scene_alias_reader', 'reject_context_reader'}:
        reader = bpy.data.objects.new(manifest['fixture_id'] + '_DriverReader', None)
        bpy.context.scene.collection.objects.link(reader)
        reader['hrig_style_fixture'] = manifest['fixture_id']
        manifest['objects'].append(reader.name)
        curve = reader.driver_add('location', 0)
        curve.driver.type, curve.driver.expression = 'SCRIPTED', 'shape'
        var = curve.driver.variables.new()
        var.name = 'shape'
        if case == 'reject_context_reader':
            var.type = 'CONTEXT_PROP'
            var.targets[0].context_property = 'ACTIVE_SCENE'
        else:
            var.type = 'SINGLE_PROP'
            var.targets[0].id_type = 'SCENE' if case == 'reject_scene_alias_reader' else 'OBJECT'
            var.targets[0].id = bpy.context.scene if case == 'reject_scene_alias_reader' else rig
        native_path = rig.pose.bones['Control'].path_from_id('custom_shape_scale_xyz') + '[0]'
        if case in {'reject_scene_alias_reader', 'reject_context_reader'}:
            native_path = 'objects[' + json.dumps(rig.name) + '].' + native_path
            resolved = bpy.context.scene.path_resolve(native_path)
            if resolved != rig.pose.bones['Control'].custom_shape_scale_xyz[0]:
                raise AssertionError('Fixture scene alias does not resolve to selected native value')
        var.targets[0].data_path = native_path
    if case == 'reject_snapshot':
        request['expected_snapshot_sha256'] = '0' * 64
    _refresh()
    manifest['request'] = copy.deepcopy(request)
    manifest['expected'].update({'generation': generation, 'changed': expected_changed,
                                 'revision': template['revision']})
    manifest['expected_fields'] = {}
    if manifest['expected']['ok']:
        for role, bone in binding.items():
            manifest['expected_fields'][bone] = {
                name: _canon_field(name, value) for name, value in template['roles'][role]['fields'].items()}
        if case in {'preserve_hand_edit', 'repeat_hand_edit'}:
            manifest['expected_fields']['Control']['pose.custom_shape_scale_xyz'] = [2.0, 2.0, 2.0]
    if manifest['inject_rollback']:
        manifest['runtime_injection'] = {'kind': 'one_shot_after_native_write',
            'core_helper': '_write_field', 'after_successful_call': 1,
            'instruction': 'Call original setter, raise RuntimeError once, then allow rollback setters; restore helper in finally.'}
    return request


def capture(manifest):
    _check(manifest)
    graph = _refresh()
    rig = bpy.data.objects[manifest['rig']]
    styles = {bone.name: {name: _read_field(rig, bone.name, name) for name in FIELDS}
              for bone in rig.data.bones}
    objects = []
    meshes = {}
    for name in sorted(manifest['objects']):
        obj = bpy.data.objects[name]
        record = {'id': _id(obj), 'settings': _native_rna(obj, ('dimensions',) if obj == rig else ()),
                  'props': _props(obj, (LEDGER,) if obj == rig else ()),
                  'world': _plain(obj.matrix_world), 'animation': _animation(obj),
                  'constraints': [{'settings': _native_rna(c), 'props': _props(c)} for c in obj.constraints],
                  'modifiers': [{'settings': _native_rna(m), 'props': _props(m)} for m in obj.modifiers],
                  'groups': [{'name': g.name, 'index': g.index, 'lock': g.lock_weight} for g in obj.vertex_groups]}
        if obj.type == 'ARMATURE':
            record['pose'] = [{'name': pb.name,
                'settings': _native_rna(pb, ('custom_shape_scale_xyz', 'custom_shape_translation', 'use_custom_shape_bone_size')) if obj == rig else _native_rna(pb),
                'shape_transform': pb.custom_shape_transform.name if pb.custom_shape_transform else None,
                'props': _props(pb), 'matrix': _plain(pb.matrix),
                'constraints': [{'settings': _native_rna(c), 'props': _props(c)} for c in pb.constraints]}
                for pb in obj.pose.bones]
        if obj.type == 'MESH':
            data = obj.data
            meshes[data.name] = {'id': _id(data), 'props': _props(data),
                'vertices': [_plain(v.co) for v in data.vertices],
                'edges': [_plain(e.vertices) for e in data.edges],
                'polygons': [_plain(p.vertices) for p in data.polygons],
                'weights': [[{'group': g.group, 'weight': g.weight} for g in v.groups] for v in data.vertices],
                'uv': [{'name': uv.name, 'data': [_plain(d.uv) for d in uv.data]} for uv in data.uv_layers],
                'attributes': [{'name': a.name, 'type': a.data_type, 'domain': a.domain,
                                'data': [_fields(d, ('value', 'vector', 'color', 'color_srgb')) for d in a.data]}
                               for a in data.attributes],
                'keys': None if data.shape_keys is None else {
                    'settings': _native_rna(data.shape_keys), 'props': _props(data.shape_keys),
                    'blocks': [{'name': k.name, 'settings': _native_rna(k),
                                'data': [_plain(d.co) for d in k.data]} for k in data.shape_keys.key_blocks]}}
            evaluated = obj.evaluated_get(graph)
            native_mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
            try:
                record['evaluated_world'] = [_plain(evaluated.matrix_world @ v.co) for v in native_mesh.vertices]
            finally:
                evaluated.to_mesh_clear()
        objects.append(record)
    armature = {'id': _id(rig.data), 'settings': _native_rna(rig.data), 'props': _props(rig.data),
        'bones': [{'name': bone.name, 'parent': bone.parent.name if bone.parent else None,
                   'settings': _native_rna(bone, ('show_wire',)),
                   'props': _props(bone), 'rest': _plain(bone.matrix_local),
                   'head': _plain(bone.head_local), 'tail': _plain(bone.tail_local)}
                  for bone in rig.data.bones]}
    return {'schema': 'hrig.control-style.fixture-observation.v1', 'styles': styles,
        'ledger': rig.get(LEDGER), 'objects': objects, 'meshes': meshes, 'armature': armature,
        'derived_style_bounds': {rig.name: _plain(rig.dimensions)},
        'actions': [_action(a) for a in bpy.data.actions],
        'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                    'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe,
                    'mode': bpy.context.mode, 'active': bpy.context.view_layer.objects.active.name
                    if bpy.context.view_layer.objects.active else None,
                    'selected': sorted(o.name for o in bpy.context.selected_objects)}}


def validate(manifest, before, after, result):
    checks = []

    def check(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok), **({'detail': detail} if detail is not None else {})})

    expected = manifest['expected']
    check('result_ok_matches_case', result.get('ok') is expected['ok'])
    if not expected['ok']:
        check('rejection_preserves_complete_native_observation', before == after)
        check('structured_error_present', isinstance(result.get('error'), dict) and bool(result['error'].get('code')))
        check('expected_rejection_code', result.get('error', {}).get('code') == expected['code'])
        if manifest['case_id'] == 'rollback_injected':
            check('rollback_fault_origin', 'fixture injected after first completed style field'
                  in result.get('error', {}).get('message', ''))
        return {'ok': all(c['ok'] for c in checks), 'checks': checks,
                'observed_error_code': result.get('error', {}).get('code')}
    check('reported_changed', result.get('changed') is expected['changed'])
    protected_before = {k: v for k, v in before.items() if k not in {'styles', 'ledger', 'derived_style_bounds'}}
    protected_after = {k: v for k, v in after.items() if k not in {'styles', 'ledger', 'derived_style_bounds'}}
    check('non_style_native_structures_exact', protected_before == protected_after)
    check('target_rig_dimensions_are_separate_derived_display_observations',
          set(before.get('derived_style_bounds', {})) == {manifest['rig']} and
          set(after.get('derived_style_bounds', {})) == {manifest['rig']},
          'Target rig dimensions may change with widget display scale/offset; other Object dimensions remain exact. This does not certify bounds or visual clarity.')
    wanted = copy.deepcopy(before['styles'])
    for bone, changes in manifest['expected_fields'].items():
        wanted[bone].update(changes)
    check('all_twelve_fields_every_bone_match_independent_literal_oracle', after['styles'] == wanted)
    try:
        ledger = json.loads(after['ledger'])
        instance = ledger['instances'][manifest['request']['instance_id']]
        check('ledger_schema_and_exact_root_fields', set(ledger) == {'schema', 'instances'} and
              ledger.get('schema') == 'hrig.control-style.instances.v1')
        check('instance_exact_fields', set(instance) == {'template', 'bindings', 'template_sha256', 'generation'})
        revision = instance.get('revision', instance.get('template', {}).get('revision'))
        template_id = instance.get('template_id', instance.get('template', {}).get('template_id'))
        check('ledger_generation', instance.get('generation') == expected['generation'])
        check('ledger_revision', revision == expected['revision'])
        check('ledger_template_identity', template_id == manifest['request']['template']['template_id'])
        check('ledger_bindings', instance.get('bindings') == manifest['request']['bindings'])
        literal_template = copy.deepcopy(manifest['request']['template'])
        for role in literal_template['roles'].values():
            role['fields'] = {name: _canon_field(name, value) for name, value in role['fields'].items()}
        check('ledger_baseline_is_literal_new_template', instance.get('template') == literal_template)
        digest = instance.get('template_sha256')
        independent_digest = hashlib.sha256(json.dumps(
            literal_template, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        check('ledger_template_digest_matches_literal', digest == independent_digest)
        before_ledger = json.loads(before['ledger']) if before['ledger'] is not None else {'instances': {}}
        other_before = {k: v for k, v in before_ledger['instances'].items() if k != manifest['request']['instance_id']}
        other_after = {k: v for k, v in ledger['instances'].items() if k != manifest['request']['instance_id']}
        check('other_instances_exact', other_before == other_after)
        check('no_extra_instances', set(ledger['instances']) == set(other_before) | {manifest['request']['instance_id']})
        if manifest['case_id'] in {'repeat', 'repeat_hand_edit'}:
            check('repeat_preserves_exact_ledger_bytes', before['ledger'] == after['ledger'])
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        check('valid_instance_ledger', False, type(exc).__name__ + ': ' + str(exc))
    return {'ok': all(c['ok'] for c in checks), 'checks': checks}
