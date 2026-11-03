"""Owned native fixtures and an independent weight/skin preservation oracle.

Never imports the addon. Creation/pose sampling are explicit fixture-only edits;
the coordinator owns patch calls, controlled fault injection, save and cold read.
"""
import hashlib
import json
import math
import struct

import bpy
from mathutils import Euler, Matrix, Vector


SCHEMA = 'hrig.weights.fixture.v1'
CASE_IDS = ('keyed_mesh_patch', 'locked_deform_positive', 'reject_shared_mesh',
            'reject_locked_change', 'reject_non_normalized', 'reject_budget',
            'reject_stale_row', 'reject_nan', 'rollback_injected',
            'single_owner_fake_user', 'inherit_unselected_invalid_weight',
            'reject_selected_invalid_weight', 'reject_shape_key_consumer',
            'reject_modifier_consumer', 'untouched_consumers_positive',
            'orphan_mixed_retention')
EXPECTED_CODES = {'reject_shared_mesh': 'SHARED_MESH', 'reject_locked_change': 'LOCKED_GROUP',
                  'reject_non_normalized': 'NOT_NORMALIZED', 'reject_budget': 'INFLUENCE_BUDGET',
                  'reject_stale_row': 'STALE_WEIGHT_ROW', 'reject_nan': 'INVALID_WEIGHT',
                  'rollback_injected': 'NATIVE_ERROR',
                  'reject_selected_invalid_weight': 'INVALID_SOURCE_WEIGHT',
                  'reject_shape_key_consumer': 'WEIGHT_GROUP_CONSUMER',
                  'reject_modifier_consumer': 'WEIGHT_GROUP_CONSUMER'}
PRESERVE_CASE_IDS = ('preserve_sum_positive', 'reject_preserve_sum_conflict',
                     'reject_preserve_sum_changed', 'reject_preserve_new_zero',
                     'reject_preserve_consumer', 'preserve_rollback_injected')
CASE_IDS += PRESERVE_CASE_IDS
EXPECTED_CODES.update({'reject_preserve_sum_conflict': 'DEFORM_SUM_CONFLICT',
                      'reject_preserve_sum_changed': 'DEFORM_SUM_CHANGED',
                      'reject_preserve_new_zero': 'ZERO_WEIGHT',
                      'reject_preserve_consumer': 'WEIGHT_GROUP_CONSUMER',
                      'preserve_rollback_injected': 'NATIVE_ERROR'})
SKIN_TOLERANCE = 2e-5


def _plain(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, 'items'):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(_plain(item) for item in value)
    return [_plain(item) for item in value]


def _props(owner):
    try:
        return {key: _plain(owner[key]) for key in sorted(owner.keys())}
    except TypeError:
        return {}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _fields(owner, names):
    return {name: _plain(getattr(owner, name)) for name in names if hasattr(owner, name)}


def _refresh():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    graph.update()
    return graph


def _check(manifest):
    if manifest.get('schema') != SCHEMA or manifest.get('case_id') not in CASE_IDS:
        raise ValueError('Unknown weight fixture manifest')
    for role in ('object', 'rig'):
        obj = bpy.data.objects.get(manifest[role])
        if obj is None or obj.get('hrig_weight_fixture') != manifest['fixture_id']:
            raise ValueError('Fixture object identity changed: ' + role)


def _rows(obj):
    names = {group.index: group.name for group in obj.vertex_groups}
    result = []
    for vertex in obj.data.vertices:
        row = {}
        for membership in vertex.groups:
            if membership.group not in names:
                raise ValueError('Undefined raw group index')
            weight = float(membership.weight)
            # Keep bad untouched storage observable without emitting invalid
            # JSON or making NaN != NaN masquerade as a mutation. These are
            # observation values, never replacement authoring values.
            row[names[membership.group]] = (weight if math.isfinite(weight) else {
                'non_finite': 'nan' if math.isnan(weight) else 'inf' if weight > 0 else '-inf',
                'native_f32le_hex': struct.pack('<f', weight).hex()})
        result.append({'index': vertex.index, 'weights': row})
    return result


def _channels(owner):
    return _fields(owner, ('location', 'rotation_mode', 'rotation_quaternion',
                           'rotation_euler', 'rotation_axis_angle', 'scale', 'matrix_basis'))


def _curve(curve):
    return {'path': curve.data_path, 'index': curve.array_index,
            **_fields(curve, ('mute', 'lock', 'extrapolation', 'auto_smoothing')),
            'keys': [_fields(key, ('co', 'handle_left', 'handle_right', 'interpolation',
                                   'easing', 'handle_left_type', 'handle_right_type', 'type'))
                     for key in curve.keyframe_points],
            'modifiers': [_fields(modifier, ('type', 'mute', 'active', 'mode', 'poly_order',
                                             'coefficients', 'use_restricted_range', 'frame_start',
                                             'frame_end', 'blend_in', 'blend_out', 'influence'))
                          for modifier in curve.modifiers]}


def _animation(owner):
    ad = owner.animation_data
    if ad is None:
        return None
    return {'action': ad.action.name if ad.action else None,
            'slot': ad.action_slot.identifier if ad.action_slot else None,
            **_fields(ad, ('action_blend_type', 'action_extrapolation', 'action_influence',
                           'use_nla', 'use_tweak_mode')),
            'drivers': [{**_curve(curve), 'type': curve.driver.type,
                         'expression': curve.driver.expression,
                         'use_self': curve.driver.use_self,
                         'variables': [{'name': var.name, 'type': var.type,
                                        'targets': [{'id': target.id.name if target.id else None,
                                                     **_fields(target, ('id_type', 'data_path', 'bone_target',
                                                                        'transform_type', 'transform_space'))}
                                                    for target in var.targets]}
                                       for var in curve.driver.variables]}
                        for curve in ad.drivers],
            'nla': [{'name': track.name, 'mute': track.mute, 'is_solo': track.is_solo,
                     'strips': [{'name': strip.name, 'type': strip.type,
                                 'action': strip.action.name if strip.action else None,
                                 'slot': strip.action_slot.identifier if getattr(strip, 'action_slot', None) else None,
                                 **_fields(strip, ('frame_start', 'frame_end', 'action_frame_start',
                                                   'action_frame_end', 'blend_type', 'extrapolation',
                                                   'influence', 'scale', 'repeat', 'mute'))}
                                for strip in track.strips]} for track in ad.nla_tracks]}


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
            strips.append({'type': strip.type, 'channelbags': bags})
        layers.append({'name': layer.name, 'strips': strips})
    return {'name': action.name, 'props': _props(action),
            'slots': [{'identifier': slot.identifier,
                       **_fields(slot, ('target_id_type', 'id_type'))}
                      for slot in action.slots], 'layers': layers}


def _shape_keys(mesh):
    keys = mesh.shape_keys
    return {'name': keys.name, 'props': _props(keys),
            **_fields(keys, ('use_relative', 'eval_time')),
            'reference_key': keys.reference_key.name,
            'animation': _animation(keys),
            'blocks': [{'name': key.name, 'relative_key': key.relative_key.name,
                        'props': _props(key),
                        **_fields(key, ('value', 'slider_min', 'slider_max', 'mute',
                                       'interpolation', 'vertex_group', 'frame')),
                        'coordinates': [list(point.co) for point in key.data]}
                       for key in keys.key_blocks]}


def _attribute(attribute):
    values = []
    for item in attribute.data:
        values.append(_fields(item, ('value', 'vector', 'color', 'color_srgb')))
    return {'name': attribute.name, 'domain': attribute.domain,
            'data_type': attribute.data_type, 'values': values}


def _material(material):
    tree = material.node_tree
    nodes = []
    for node in tree.nodes:
        state = {'name': node.name, 'id': node.bl_idname,
                 **_fields(node, ('label', 'location', 'width', 'height', 'mute', 'hide',
                                 'interpolation', 'extension', 'projection', 'projection_blend')),
                 'props': _props(node),
                 'image': node.image.name if hasattr(node, 'image') and node.image else None,
                 'inputs': [{'index': index, 'name': socket.name, 'identifier': socket.identifier,
                             **_fields(socket, ('default_value',))}
                            for index, socket in enumerate(node.inputs)]}
        nodes.append(state)
    return {'name': material.name, 'props': _props(material),
            **_fields(material, ('use_nodes', 'diffuse_color', 'metallic', 'roughness')),
            'nodes': nodes,
            'links': [[link.from_node.name, link.from_socket.identifier,
                       link.to_node.name, link.to_socket.identifier] for link in tree.links]}


def _image(image):
    packed = image.packed_file
    if packed is None:
        raise ValueError('Fixture image must actually contain packed bytes')
    pixels = hashlib.sha256(b'fixture-pixels-f32le-v1\0')
    for value in image.pixels:
        pixels.update(struct.pack('<f', value))
    return {'name': image.name, 'size': list(image.size),
            **_fields(image, ('source', 'filepath_raw', 'alpha_mode', 'is_float',
                              'generated_type', 'generated_color')),
            'colorspace': image.colorspace_settings.name,
            'packed_sha256': hashlib.sha256(bytes(packed.data)).hexdigest(),
            'pixel_sha256': pixels.hexdigest(), 'props': _props(image)}


def _orphan_content(collection, item):
    """Independent declared payloads for the eight owned retention sentinels."""
    if collection == 'objects':
        return {'type': item.type, 'data': item.data.name if item.data else None,
                'collections': sorted(c.name for c in item.users_collection),
                'channels': _channels(item), 'props': _props(item),
                'animation': _animation(item)}
    if collection == 'meshes':
        return {'coordinates': [list(v.co) for v in item.vertices],
                'edges': [list(e.vertices) for e in item.edges],
                'polygons': [list(p.vertices) for p in item.polygons],
                'loops': [[x.vertex_index, x.edge_index] for x in item.loops],
                'uv': [{'name': layer.name, 'values': [list(x.uv) for x in layer.data]}
                       for layer in item.uv_layers],
                'attributes': [_attribute(a) for a in item.attributes],
                'materials': [m.name if m else None for m in item.materials], 'props': _props(item)}
    if collection == 'materials':
        return _material(item)
    if collection == 'actions':
        return _action(item)
    if collection == 'texts':
        return {'body': item.as_string(), 'props': _props(item),
                **_fields(item, ('filepath', 'use_module', 'use_tabs_as_spaces'))}
    if collection == 'images':
        return _image(item)
    if collection == 'node_groups':
        return {'type': item.bl_idname, 'props': _props(item),
                'interface': [_fields(socket, ('identifier', 'name', 'item_type', 'in_out',
                                               'socket_type', 'default_value'))
                              for socket in item.interface.items_tree],
                'nodes': [{'name': node.name, 'type': node.bl_idname, 'props': _props(node),
                           'inputs': [{'identifier': s.identifier, **_fields(s, ('default_value',))}
                                      for s in node.inputs],
                           'outputs': [{'identifier': s.identifier, **_fields(s, ('default_value',))}
                                       for s in node.outputs]} for node in item.nodes],
                'links': [[x.from_node.name, x.from_socket.identifier,
                           x.to_node.name, x.to_socket.identifier] for x in item.links]}
    raise ValueError('Unknown declared retention collection')


def _retained_orphans(manifest, mesh):
    user_map = bpy.data.user_map()
    records, counts = [], {}
    for expected in manifest['retention_ids']:
        item = getattr(bpy.data, expected['collection']).get(expected['name'])
        if item is None:
            raise ValueError('Declared retained orphan missing: ' + expected['name'])
        native_type = item.bl_rna.identifier
        counts[native_type] = counts.get(native_type, 0) + 1
        records.append({'collection': expected['collection'], 'name': item.name,
            'type': native_type, 'users': item.users, 'fake_user': item.use_fake_user,
            'extra_user': item.use_extra_user,
            'real_id_users': sorted([{'type': user.bl_rna.identifier, 'name': user.name}
                                     for user in user_map.get(item, ())], key=lambda x:(x['type'],x['name'])),
            'content': _orphan_content(expected['collection'], item)})
    keys = mesh.shape_keys
    key_owners = sorted(item.name for item in bpy.data.meshes if item.shape_keys == keys)
    return {'declared_count': len(manifest['retention_ids']), 'captured_count': len(records),
            'counts_by_type': counts, 'records': records,
            'primary_mesh': {'name': mesh.name, 'users': mesh.users,
                'fake_user': mesh.use_fake_user, 'extra_user': mesh.use_extra_user,
                'object_owners': sorted(o.name for o in bpy.data.objects if o.data == mesh),
                'key': {'name': keys.name, 'owner_mesh': key_owners[0] if len(key_owners) == 1 else None,
                    'mesh_owners': key_owners,
                    'users': keys.users, 'fake_user': keys.use_fake_user, 'extra_user': keys.use_extra_user}}}


def _create_retention_ids(prefix):
    """New unrelated zero-reference sentinels, retaining their chosen flags."""
    empty = bpy.data.objects.new(prefix + '_OrphanObject', None)
    empty.location = (.17, -.23, .41)
    orphan_mesh = bpy.data.meshes.new(prefix + '_OrphanMesh')
    orphan_mesh.from_pydata([(0, 0, 0), (.3, 0, 0), (0, .4, .2)], [], [(0, 1, 2)])
    orphan_mesh.update()
    uv = orphan_mesh.uv_layers.new(name='OrphanUV')
    for index, point in enumerate(uv.data):
        point.uv = (index / 3, (2 - index) / 3)
    material = bpy.data.materials.new(prefix + '_OrphanMaterial')
    material.use_nodes = True
    material.diffuse_color = (.13, .27, .61, 1)
    action = bpy.data.actions.new(prefix + '_OrphanAction')
    slot = action.slots.new(id_type='OBJECT', name='UnassignedOrphanSlot')
    layer = action.layers.new('OrphanLayer')
    strip = layer.strips.new(type='KEYFRAME')
    curve = strip.channelbag(slot, ensure=True).fcurves.new(data_path='location', index=0)
    for frame, value in ((1, .17), (9, -.31)):
        curve.keyframe_points.insert(frame, value)
    text = bpy.data.texts.new(prefix + '_OrphanText')
    # Text defaults to fake-user retention. This newly created sentinel
    # explicitly exercises a false flag before freezing any baseline.
    text.use_fake_user = False
    text.write('Owned orphan retention sentinel.\nNo execution requested.\n')
    text.use_module = False
    tree = bpy.data.node_groups.new(prefix + '_OrphanGeometry', 'GeometryNodeTree')
    tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    incoming = tree.nodes.new('NodeGroupInput')
    outgoing = tree.nodes.new('NodeGroupOutput')
    tree.links.new(incoming.outputs['Geometry'], outgoing.inputs['Geometry'])
    value = tree.nodes.new('ShaderNodeValue')
    value.outputs[0].default_value = .37
    image = bpy.data.images.new(prefix + '_OrphanPackedImage', width=2, height=2, alpha=True)
    image.pixels.foreach_set([.15, .65, .3, 1.0] * 4)
    image.pack()
    fake_material = bpy.data.materials.new(prefix + '_OrphanFakeMaterial')
    fake_material.use_nodes = True
    fake_material.diffuse_color = (.73, .19, .41, 1)
    fake_material.use_fake_user = True
    entries = [('objects', empty), ('meshes', orphan_mesh), ('materials', material),
               ('actions', action), ('texts', text), ('node_groups', tree),
               ('images', image), ('materials', fake_material)]
    descriptors = []
    for collection, item in entries:
        item['hrig_retention_sentinel'] = prefix
        fake = item == fake_material
        # Do not user_clear or hide an unexpected initial reference by editing
        # fake/extra flags. A fixture construction discrepancy must fail openly.
        if item.use_fake_user is not fake or item.use_extra_user or item.users != int(fake):
            raise ValueError('Native retention sentinel is not truly orphaned: ' + item.name)
        descriptors.append({'collection': collection, 'name': item.name,
                            'type': item.bl_rna.identifier, 'fake_user': fake,
                            'extra_user': False, 'users': int(fake), 'real_id_users': []})
    return descriptors


def sample(manifest):
    """Actual evaluated skin versus independent relative-key + linear skin math."""
    _check(manifest)
    graph = _refresh()
    obj, rig = bpy.data.objects[manifest['object']], bpy.data.objects[manifest['rig']]
    evaluated, evaluated_rig = obj.evaluated_get(graph), rig.evaluated_get(graph)
    if not evaluated.is_evaluated or not evaluated_rig.is_evaluated:
        raise ValueError('Native evaluated IDs required')
    actual = [list(evaluated.matrix_world @ vertex.co) for vertex in evaluated.data.vertices]
    keys = obj.data.shape_keys
    rows = _rows(obj)
    basis = keys.reference_key
    shaped = [point.co.copy() for point in basis.data]
    for key in keys.key_blocks:
        if key == basis or key.mute:
            continue
        for index, point in enumerate(key.data):
            mask = rows[index]['weights'].get(key.vertex_group, 0.0) if key.vertex_group else 1.0
            if type(mask) not in (int, float) or not math.isfinite(mask) or not 0 <= mask <= 1:
                raise ValueError('Analytic shape-mask witness requires finite [0,1] mask weights')
            shaped[index] += key.value * mask * (point.co - key.relative_key.data[index].co)
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    to_rig = evaluated_rig.matrix_world.inverted() @ evaluated.matrix_world
    predicted = []
    for index, coordinate in enumerate(shaped):
        rig_coordinate = to_rig @ coordinate
        accumulated, total = Vector((0, 0, 0)), 0.0
        for name, weight in rows[index]['weights'].items():
            if name not in deform:
                continue
            if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 1:
                raise ValueError('Analytic skin witness requires finite [0,1] deform weights')
            if weight > 0:
                matrix = evaluated_rig.pose.bones[name].matrix @ rig.data.bones[name].matrix_local.inverted()
                accumulated += weight * (matrix @ rig_coordinate)
                total += weight
        if manifest['case_id'] in PRESERVE_CASE_IDS:
            # Blender 5.2.1 BoneDeformLinearMixer::finalize uses
            # armature_weight / total (armature_deform.cc:147..151).
            # This fixture has no modifier mask, envelopes, DQ, or b-bone
            # segments, so its independent weighted-position form is /total.
            if not math.isfinite(total) or total <= 1e-8:
                raise ValueError('Preserve-sum fixture requires positive finite native deform total')
            accumulated /= total
        else:
            if abs(total - 1.0) > 1e-6:
                raise ValueError('Fixture source raw deform weights must be normalized')
        predicted.append(list(evaluated_rig.matrix_world @ accumulated))
    if len(actual) != len(predicted):
        raise ValueError('Native witness vertex count differs')
    if any(not math.isfinite(value) for point in actual + predicted for value in point):
        raise ValueError('Nonfinite skin witness')
    error = max(((Vector(a) - Vector(predicted[i])).length for i, a in enumerate(actual)), default=0.0)
    return {'actual_world_points': actual, 'linear_skin_world_points': predicted,
            'linear_skin_max_distance': error, 'tolerance': SKIN_TOLERANCE,
            'bone_world': {pb.name: _plain(evaluated_rig.matrix_world @ pb.matrix)
                           for pb in evaluated_rig.pose.bones},
            'shape_values': {key.name: key.value for key in keys.key_blocks},
            'key_driver_valid': all(curve.driver.is_valid for curve in keys.animation_data.drivers),
            'frame': bpy.context.scene.frame_current}


def _finite_points(points):
    return (type(points) is list and all(type(point) is list and len(point) == 3
        and all(type(value) in (int, float) and math.isfinite(value) for value in point)
        for point in points))


def capture(manifest):
    """Independent declared raw state and protected component signatures."""
    _check(manifest)
    observed = sample(manifest)
    obj, rig = bpy.data.objects[manifest['object']], bpy.data.objects[manifest['rig']]
    mesh = obj.data
    objects = {}
    for name in manifest['objects']:
        item = bpy.data.objects[name]
        objects[name] = {'name': item.name, 'type': item.type, 'data': item.data.name if item.data else None,
                         'parent': item.parent.name if item.parent else None,
                         'matrix_parent_inverse': _plain(item.matrix_parent_inverse),
                         'channels': _channels(item), 'props': _props(item),
                         'animation': _animation(item)}
    state = {
        'geometry': {'coordinates': [list(vertex.co) for vertex in mesh.vertices],
                     'edges': [list(edge.vertices) for edge in mesh.edges],
                     'polygons': [{'vertices': list(poly.vertices), 'loop_start': poly.loop_start,
                                   'loop_total': poly.loop_total, 'material_index': poly.material_index,
                                   'use_smooth': poly.use_smooth} for poly in mesh.polygons],
                     'loops': [[loop.vertex_index, loop.edge_index] for loop in mesh.loops],
                     'props': _props(mesh)},
        'uv': {'active_index': mesh.uv_layers.active_index,
               'layers': [{'name': layer.name, **_fields(layer, ('active_render', 'active_clone')),
                           'data': [list(loop.uv) for loop in layer.data]} for layer in mesh.uv_layers]},
        'attributes': {'items': [_attribute(attribute) for attribute in mesh.attributes],
                       'color_metadata': _fields(mesh.color_attributes,
                           ('active_color_name', 'default_color_name', 'render_color_index', 'active_color_index'))},
        'shape_keys': _shape_keys(mesh),
        'groups': [{'name': group.name, 'index': group.index, 'lock_weight': group.lock_weight}
                   for group in obj.vertex_groups],
        'objects_rig': {'objects': objects, 'rig_props': _props(rig.data),
                        'bones': {bone.name: {'rest': _plain(bone.matrix_local),
                                              'parent': bone.parent.name if bone.parent else None,
                                              'deform': bone.use_deform, 'inherit_scale': bone.inherit_scale}
                                  for bone in rig.data.bones},
                        'pose': {pb.name: {'channels': _channels(pb), 'props': _props(pb)}
                                 for pb in rig.pose.bones}},
        'material': [_material(bpy.data.materials[name]) for name in manifest['materials']],
        'image': [_image(bpy.data.images[name]) for name in manifest['images']],
        'actions': [_action(bpy.data.actions[name]) for name in manifest['actions']],
        'binding': {'mesh_users': mesh.users, 'mesh_fake_user': mesh.use_fake_user,
                    'mesh_object_owners': sorted(item.name for item in bpy.data.objects if item.data == mesh),
                    'material_slots': [m.name if m else None for m in mesh.materials],
                    'modifiers': [{'name': mod.name, 'type': mod.type,
                                   'object': mod.object.name if getattr(mod, 'object', None) else None,
                                   **_fields(mod, ('show_viewport', 'show_render', 'use_vertex_groups',
                                                   'use_bone_envelopes', 'use_deform_preserve_volume',
                                                   'vertex_group', 'invert_vertex_group', 'factor',
                                                   'iterations', 'use_x', 'use_y', 'use_z'))}
                                  for mod in obj.modifiers]},
        'context': {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
                    'frame': bpy.context.scene.frame_current, 'subframe': bpy.context.scene.frame_subframe},
    }
    if 'retention_ids' in manifest:
        state['retained_orphans'] = _retained_orphans(manifest, mesh)
    rows = _rows(obj)
    return {'schema': SCHEMA, 'case_id': manifest['case_id'], 'raw_rows': rows,
            'raw_rows_sha256': _hash(rows),
            'protected_signatures': {key + '_sha256': _hash(value) for key, value in state.items()},
            'protected_state': state, 'sample': observed}


def set_pose(manifest, pose_index):
    """Explicitly move only the fixture rig, which deliberately has no Action."""
    _check(manifest)
    if type(pose_index) is not int or pose_index not in (0, 1, 2):
        raise ValueError('Pose index must be 0/1/2')
    rig = bpy.data.objects[manifest['rig']]
    if rig.animation_data and (rig.animation_data.action or rig.animation_data.nla_tracks):
        raise ValueError('Fixture rig pose must not be overwritten by animation')
    poses = (
        ((0, 0, 0), (.18, -.1, .23), (-.15, .07, -.1)),
        ((.12, -.08, .05), (-.31, .22, -.42), (.16, -.2, .27)),
        ((-.09, .13, -.11), (.37, -.26, .51), (-.22, .1, -.31)),
    )
    for index, name in enumerate(('A', 'B', 'C')):
        bone = rig.pose.bones[name]
        bone.rotation_mode = 'QUATERNION'
        bone.rotation_quaternion = Euler(poses[pose_index][index]).to_quaternion()
        bone.location = ((.08, -.03, .04) if name == 'B' else (0, 0, 0))
        bone.scale = (1, 1, 1)
    rig.update_tag()
    return sample(manifest)


def validate(manifest, before, after, result):
    """Full expected membership, untouched fields and actual skin, independent."""
    _check(manifest)
    if type(result) is not dict:
        return {'ok': False, 'checks': {'structured_core_result': False}}
    expected = manifest['expected']
    checks = {'result_direction': result.get('ok') is expected['ok'],
              'protected_state': before['protected_signatures'] == after['protected_signatures']}
    a, b = before['raw_rows'], after['raw_rows']
    if len(a) != len(b) or [row['index'] for row in a] != [row['index'] for row in b]:
        return {'ok': False, 'checks': {**checks, 'vertex_identity': False}}
    selected = set(manifest['patched_vertex_indices'])
    raw_before = {row['index']: row['weights'] for row in a}
    raw_after = {row['index']: row['weights'] for row in b}
    checks['unselected_raw'] = all(raw_before[index] == raw_after[index] for index in raw_before if index not in selected)
    checks['selected_nondeform_memberships'] = all(
        {n:w for n,w in raw_before[index].items() if n not in manifest['deform_groups']} ==
        {n:w for n,w in raw_after[index].items() if n not in manifest['deform_groups']} for index in selected)
    invalid = manifest.get('invalid_raw_witness')
    zero_indices = selected - ({invalid['index']} if invalid else set())
    checks['nondeform_zero_membership'] = all('Mask' in raw_before[index] and 'Mask' in raw_after[index]
        and raw_before[index]['Mask'] == raw_after[index]['Mask'] == 0 for index in zero_indices)
    if invalid:
        index, group = invalid['index'], invalid['group']
        first_weight, second_weight = raw_before[index].get(group), raw_after[index].get(group)
        checks['native_invalid_membership_preserved'] = (
            type(first_weight) is float and type(second_weight) is float and first_weight > 1.0
            and struct.pack('<f', first_weight).hex() == invalid['native_f32le_hex']
            and struct.pack('<f', second_weight).hex() == invalid['native_f32le_hex'])
        checks['invalid_vertex_scope'] = (index in selected) is invalid['selected']
    if manifest['case_id'] == 'single_owner_fake_user':
        checks['single_owner_with_fake_user'] = all(
            state['protected_state']['binding']['mesh_fake_user'] is True
            and state['protected_state']['binding']['mesh_object_owners'] == [manifest['object']]
            for state in (before, after))
    if manifest['case_id'] == 'orphan_mixed_retention':
        required_counts = {'Object': 1, 'Mesh': 1, 'Material': 2, 'Action': 1,
                           'Text': 1, 'GeometryNodeTree': 1, 'Image': 1}
        signatures = []
        for snapshot in (before, after):
            retained = snapshot['protected_state'].get('retained_orphans', {})
            records = retained.get('records', [])
            signatures.append(retained)
            flags = {row['name']: {key: row.get(key) for key in
                     ('collection', 'name', 'type', 'users', 'fake_user', 'extra_user', 'real_id_users')}
                     for row in records}
            expected_flags = {row['name']: row for row in manifest['retention_ids']}
            primary = retained.get('primary_mesh', {})
            checks['mixed_orphan_count_' + ('before' if snapshot is before else 'after')] = (
                retained.get('declared_count') == retained.get('captured_count') == len(records) == 8
                and retained.get('counts_by_type') == required_counts and flags == expected_flags)
            checks['mixed_primary_flags_' + ('before' if snapshot is before else 'after')] = (
                primary.get('fake_user') is True and primary.get('extra_user') is False
                and primary.get('object_owners') == [manifest['object']]
                and primary.get('key', {}).get('owner_mesh') == primary.get('name'))
        checks['mixed_orphan_contents_and_flags'] = signatures[0] == signatures[1]
    consumer = manifest.get('consumer_witness')
    if consumer:
        name = consumer['group']
        requests = {row['index']: row for row in manifest['request']['rows']}
        requested_change = any(
            (name in raw_before[index]) != (name in requests[index]['after']) or
            (name in raw_before[index] and raw_before[index][name] !=
             struct.unpack('<f', struct.pack('<f', float(requests[index]['after'][name])))[0])
            for index in selected)
        checks['consumer_group_change_scope'] = requested_change is consumer['requested_change']
        if 'shape_key' in consumer['kinds']:
            blocks = before['protected_state']['shape_keys']['blocks']
            smile = next(block for block in blocks if block['name'] == 'Smile')
            basis = next(block for block in blocks if block['name'] == 'Basis')
            checks['actual_shape_key_consumer'] = (smile['vertex_group'] == name and
                any(smile['coordinates'][i] != basis['coordinates'][i] for i in selected))
        if 'modifier' in consumer['kinds']:
            checks['actual_modifier_consumer'] = any(
                mod['type'] == 'SMOOTH' and mod.get('vertex_group') == name and mod.get('factor') == 0.0
                for mod in before['protected_state']['binding']['modifiers'])
    if manifest['case_id'] in PRESERVE_CASE_IDS:
        deform = set(manifest['deform_groups'])
        source_totals = {index: math.fsum(weight for name, weight in raw_before[index].items()
                          if name in deform) for index in selected}
        after_totals = {index: math.fsum(weight for name, weight in raw_after[index].items()
                         if name in deform) for index in selected}
        checks['preserve_source_nonunit_075_and_125'] = source_totals == {0: .75, 1: 1.25}
        checks['preserve_native_deform_sum'] = all(
            abs(after_totals[index] - source_totals[index]) <= 1e-6 for index in selected)
        groups = before['protected_state']['groups']
        checks['preserve_existing_unlocked_zero_C'] = (
            next(group for group in groups if group['name'] == 'C')['lock_weight'] is False
            and all('C' in raw_before[index] and 'C' in raw_after[index]
                    and raw_before[index]['C'] == raw_after[index]['C'] == 0 for index in selected))
        witness = manifest['preserve_sum_witness']
        checks['preserve_witness_native_sum_exact'] = witness['source_deform_sums'] == [
            {'index': index, 'sum': source_totals[index]} for index in sorted(selected)]
        checks['preserve_explicit_request_policy'] = (
            manifest['request']['schema'] == 'hrig.weights.patch.v2'
            and manifest['request']['sum_policy'] == 'preserve_source_deform_sum')
        if manifest['case_id'] == 'reject_preserve_new_zero':
            checks['preserve_new_zero_is_absent_native_membership'] = (
                'B' not in raw_before[0] and manifest['request']['rows'][0]['after']['B'] == 0)
    first, second = before['sample'], after['sample']
    if (not _finite_points(first['actual_world_points'])
            or not _finite_points(second['actual_world_points'])
            or not math.isfinite(first['linear_skin_max_distance'])
            or not math.isfinite(second['linear_skin_max_distance'])):
        return {'ok': False, 'checks': {**checks, 'finite_skin_witness': False}}
    if len(first['actual_world_points']) != len(second['actual_world_points']):
        return {'ok': False, 'checks': {**checks, 'evaluated_count': False}}
    distances = [(Vector(point) - Vector(second['actual_world_points'][index])).length
                 for index, point in enumerate(first['actual_world_points'])]
    checks['native_linear_skin_oracle'] = (first['linear_skin_max_distance'] <= SKIN_TOLERANCE
                                           and second['linear_skin_max_distance'] <= SKIN_TOLERANCE)
    checks['nonzero_shape_driver'] = (first['shape_values']['Smile'] > 0
                                      and second['shape_values']['Smile'] > 0
                                      and first['key_driver_valid'] and second['key_driver_valid'])
    if expected['ok']:
        desired = {}
        for row in manifest['request']['rows']:
            new = {name: weight for name, weight in raw_before[row['index']].items()
                   if name not in manifest['deform_groups']}
            new.update({name: struct.unpack('<f', struct.pack('<f', float(weight)))[0]
                        for name, weight in row['after'].items()})
            desired[row['index']] = new
        checks['complete_selected_raw'] = all(raw_after[index] == desired[index] for index in selected)
        checks['actual_skin_changed'] = max((distances[index] for index in selected), default=0) > 1e-5
        checks['unselected_skin_unchanged'] = max((distance for index, distance in enumerate(distances)
                                                  if index not in selected), default=0) <= SKIN_TOLERANCE
        locked = {group['name'] for group in before['protected_state']['groups'] if group['lock_weight']}
        checks['locked_memberships'] = all(
            {name: value for name, value in raw_before[index].items() if name in locked} ==
            {name: value for name, value in raw_after[index].items() if name in locked} for index in selected)
    else:
        checks['explicit_code'] = (result.get('error') or {}).get('code') == expected['code']
        checks['all_raw_unchanged'] = a == b
        checks['all_skin_unchanged'] = max(distances, default=0) <= SKIN_TOLERANCE
        rollback = result.get('rollback', {})
        checks['rollback_contract'] = (rollback.get('ok') is True and
            rollback.get('attempted') is (manifest['case_id'] in ('rollback_injected', 'preserve_rollback_injected')))
    return {'ok': all(checks.values()), 'case_id': manifest['case_id'], 'checks': checks,
            'metrics': {'skin_change_max': max(distances, default=0),
                        'after_linear_skin_error': second['linear_skin_max_distance'],
                        'skin_tolerance': SKIN_TOLERANCE}}


def create_case(case_id):
    """New linked fixture IDs only; no reset/save or mutation of source assets."""
    if case_id not in CASE_IDS or bpy.context.mode != 'OBJECT':
        raise ValueError('Known case and Object mode required')
    prefix = 'WF_' + case_id
    for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.armatures,
                       bpy.data.materials, bpy.data.images, bpy.data.actions,
                       bpy.data.texts, bpy.data.node_groups):
        if any(item.name.startswith(prefix + '_') for item in collection):
            raise ValueError('Fixture prefix collision')
    root = bpy.data.objects.new(prefix + '_Root', None)
    bpy.context.scene.collection.objects.link(root)
    root.matrix_world = Matrix.LocRotScale(Vector((1.2, -.8, .5)),
        Euler((.2, -.1, .3)).to_quaternion(), Vector((1.1, .9, 1.2)))
    armature = bpy.data.armatures.new(prefix + '_RigData')
    rig = bpy.data.objects.new(prefix + '_Rig', armature)
    bpy.context.scene.collection.objects.link(rig)
    rig.parent = root
    selected, active = list(bpy.context.selected_objects), bpy.context.view_layer.objects.active
    try:
        for item in selected:
            item.select_set(False)
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode='EDIT')
        for name, offset in (('A', 0), ('B', 1), ('C', -.4)):
            bone = armature.edit_bones.new(name)
            bone.head, bone.tail = (0, 0, offset), (0, 0, offset + .8)
            bone.use_deform = True
        bpy.ops.object.mode_set(mode='OBJECT')
    finally:
        if rig.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        rig.select_set(False)
        for item in selected:
            item.select_set(True)
        bpy.context.view_layer.objects.active = active
    mesh = bpy.data.meshes.new(prefix + '_MeshData')
    mesh.from_pydata([(-.3, 0, 0), (.3, 0, 0), (.3, 0, .8), (-.3, 0, .8),
                      (-.25, 0, 1.2), (.25, 0, 1.2), (.25, 0, 1.8), (-.25, 0, 1.8)],
                     [], [(0, 1, 2, 3), (4, 5, 6, 7)])
    mesh.update()
    obj = bpy.data.objects.new(prefix + '_Mesh', mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = root
    for item in (obj, rig):
        item['hrig_weight_fixture'] = prefix
    for name in ('A', 'B', 'C', 'Mask'):
        obj.vertex_groups.new(name=name)
    obj.vertex_groups['A'].add([0, 1, 2, 3], 1.0, 'REPLACE')
    obj.vertex_groups['B'].add([4, 5, 6, 7], 1.0, 'REPLACE')
    obj.vertex_groups['Mask'].add([0, 1], 0.0, 'REPLACE')
    if case_id in PRESERVE_CASE_IDS:
        # Exactly representable, per-membership [0,1], nonunit deform totals.
        # B is absent from native row0; C is a real unlocked zero membership.
        obj.vertex_groups['A'].add([0, 1], .75, 'REPLACE')
        obj.vertex_groups['B'].add([1], .5, 'REPLACE')
        obj.vertex_groups['C'].add([0, 1], 0.0, 'REPLACE')
    if case_id in ('single_owner_fake_user', 'orphan_mixed_retention'):
        mesh.use_fake_user = True
    if case_id in ('inherit_unselected_invalid_weight', 'reject_selected_invalid_weight'):
        # Native scalar RNA and bmesh assignment clamp authoring values.
        # Verified native collection bulk storage creates the deliberate bad
        # source without editing geometry or using runtime memory pointers.
        bad_index = 7 if case_id == 'inherit_unselected_invalid_weight' else 0
        mask=obj.vertex_groups['Mask']
        mask.add([bad_index],.5,'REPLACE')
        memberships=mesh.vertices[bad_index].groups
        memberships.foreach_set('weight',[1.2 if m.group==mask.index else m.weight for m in memberships])
        mesh.update()
        actual_bad = _rows(obj)[bad_index]['weights'].get('Mask')
        if type(actual_bad) is not float or actual_bad <= 1.0:
            raise ValueError('Native fixture failed to create out-of-range raw membership')
    if case_id == 'untouched_consumers_positive':
        obj.vertex_groups['A'].add([0, 1], .75, 'REPLACE')
        obj.vertex_groups['C'].add([0, 1], .25, 'REPLACE')
    if case_id in ('locked_deform_positive', 'reject_locked_change'):
        obj.vertex_groups['A'].add([0, 1], .25, 'REPLACE')
        obj.vertex_groups['C'].add([0, 1], .75, 'REPLACE')
        obj.vertex_groups['A'].lock_weight = True
    modifier = obj.modifiers.new('FixtureArmature', 'ARMATURE')
    modifier.object = rig
    modifier.use_vertex_groups, modifier.use_bone_envelopes = True, False
    modifier.use_deform_preserve_volume = False
    for layer_index in range(3):
        layer = mesh.uv_layers.new(name='FixtureUV' + str(layer_index))
        for index, datum in enumerate(layer.data):
            datum.uv = ((index % 4) / 3, (index // 4 + layer_index) / 4)
    floating = mesh.attributes.new(name='FixtureFloat', type='FLOAT', domain='POINT')
    colors = mesh.color_attributes.new(name='FixtureColor', type='BYTE_COLOR', domain='POINT')
    for index in range(len(mesh.vertices)):
        floating.data[index].value = (index + 1) / 16
        colors.data[index].color = (index / 8, .4, .6, 1)
    basis = obj.shape_key_add(name='Basis', from_mix=False)
    smile = obj.shape_key_add(name='Smile', from_mix=False)
    for index in (4, 5, 6, 7):
        smile.data[index].co += Vector((.04 if index in (5, 6) else -.02, -.03, .025))
    smile.relative_key = basis
    smile.slider_min, smile.slider_max = -.25, 1.25
    smile.interpolation = 'KEY_LINEAR'
    if case_id in ('reject_shape_key_consumer', 'untouched_consumers_positive', 'reject_preserve_consumer'):
        smile.vertex_group = 'C' if case_id == 'untouched_consumers_positive' else 'A'
        for index in (0, 1):
            smile.data[index].co += Vector((.025, -.04, .03))
    if case_id in ('reject_modifier_consumer', 'untouched_consumers_positive'):
        consumer_modifier = obj.modifiers.new('FixtureGroupConsumer', 'SMOOTH')
        consumer_modifier.vertex_group = 'A' if case_id == 'reject_modifier_consumer' else 'C'
        # Exercise the native named consumer without introducing a second
        # geometric deformation model into the independent LBS oracle.
        consumer_modifier.factor = 0.0
    animation_object = bpy.data.objects.new(prefix + '_AnimatedWitness', None)
    bpy.context.scene.collection.objects.link(animation_object)
    animation_object['smile'] = .35
    action = bpy.data.actions.new(prefix + '_ObjectAction')
    slot = action.slots.new(id_type='OBJECT', name=animation_object.name)
    layer = action.layers.new('FixtureObjectLayer')
    strip = layer.strips.new(type='KEYFRAME')
    bag = strip.channelbag(slot, ensure=True)
    curve = bag.fcurves.new(data_path='location', index=1)
    for frame, value in ((1, .1), (2, .2), (3, .3)):
        curve.keyframe_points.insert(frame, value)
    animation_object.animation_data_create()
    animation_object.animation_data.action = action
    animation_object.animation_data.action_slot = slot
    driver_curve = smile.driver_add('value')
    driver_curve.driver.type = 'SCRIPTED'
    variable = driver_curve.driver.variables.new()
    variable.name, variable.type = 'smile', 'SINGLE_PROP'
    variable.targets[0].id = animation_object
    variable.targets[0].data_path = '["smile"]'
    driver_curve.driver.expression = 'smile'
    image = bpy.data.images.new(prefix + '_PackedImage', width=2, height=2, alpha=True)
    image.pixels.foreach_set([.7, .2, .1, 1.0] * 4)
    image.pack()
    material = bpy.data.materials.new(prefix + '_Material')
    material.use_nodes = True
    texture = material.node_tree.nodes.new('ShaderNodeTexImage')
    texture.image = image
    principled = next(node for node in material.node_tree.nodes if node.type == 'BSDF_PRINCIPLED')
    material.node_tree.links.new(texture.outputs['Color'], principled.inputs['Base Color'])
    mesh.materials.append(material)
    objects = [root.name, rig.name, obj.name, animation_object.name]
    retention_ids = _create_retention_ids(prefix) if case_id == 'orphan_mixed_retention' else None
    if case_id == 'reject_shared_mesh':
        sibling = bpy.data.objects.new(prefix + '_SharedMesh', mesh)
        bpy.context.scene.collection.objects.link(sibling)
        sibling.parent = root
        objects.append(sibling.name)
    bpy.context.scene.frame_set(1)
    obj.update_tag()
    _refresh()
    rows = _rows(obj)
    request_rows = [{'index': index, 'before': dict(rows[index]['weights']),
                     'after': {'A': .25, 'B': .75}} for index in (0, 1)]
    request = {'schema': 'hrig.weights.patch.v1', 'expected_topology_sha256': None,
               'expected_groups_sha256': None, 'rows': request_rows, 'max_influences': 2}
    if case_id == 'reject_locked_change':
        request_rows[0]['after'] = {'A': .5, 'B': .5}
    elif case_id == 'reject_non_normalized':
        request_rows[0]['after'] = {'A': .25, 'B': .5}
    elif case_id == 'reject_budget':
        request['max_influences'] = 1
    elif case_id == 'reject_stale_row':
        request_rows[0]['before'].pop('Mask')
    elif case_id == 'untouched_consumers_positive':
        request['max_influences'] = 3
        for row in request_rows:
            row['after'] = {'A': .25, 'B': .5, 'C': .25}
    if case_id in PRESERVE_CASE_IDS:
        request['schema'] = 'hrig.weights.patch.v2'
        request['sum_policy'] = 'preserve_source_deform_sum'
        for row in request_rows:
            row['expected_deform_sum'] = math.fsum(weight for name, weight in row['before'].items()
                if name in ('A', 'B', 'C'))
            row['after'] = ({'A': .25, 'B': .5, 'C': 0.0} if row['index'] == 0
                            else {'A': .5, 'B': .75, 'C': 0.0})
        actual_sums = [row['expected_deform_sum'] for row in request_rows]
        if actual_sums != [.75, 1.25] or any(
                'C' not in row['before'] or row['before']['C'] != 0 for row in request_rows):
            raise ValueError('Native preserve fixture source sum/zero construction differs')
        if case_id == 'reject_preserve_sum_conflict':
            request_rows[0]['expected_deform_sum'] += .125
        elif case_id == 'reject_preserve_sum_changed':
            request_rows[0]['after'] = {'A': .25, 'B': .25, 'C': 0.0}
        elif case_id == 'reject_preserve_new_zero':
            if 'B' in request_rows[0]['before']:
                raise ValueError('New-zero fixture requires B genuinely absent from source row0')
            request_rows[0]['after'] = {'A': .75, 'B': 0.0, 'C': 0.0}
    manifest = {'schema': SCHEMA, 'case_id': case_id, 'fixture_id': prefix,
                'object': obj.name, 'rig': rig.name, 'objects': objects,
                'context': {'scene': bpy.context.scene.name,
                            'view_layer': bpy.context.view_layer.name, 'frame': 1, 'subframe': 0.0},
                'patched_vertex_indices': [0, 1], 'deform_groups': ['A', 'B', 'C'],
                'materials': [material.name], 'images': [image.name], 'actions': [action.name],
                'request': request,
                'expected': {'ok': case_id not in EXPECTED_CODES, 'code': EXPECTED_CODES.get(case_id)},
                'oracle': 'native evaluated vertices versus independently computed relative-key linear skin; no core imports',
                'scope': 'synthetic eight-vertex fixture, not a human deformation-quality claim'}
    if retention_ids is not None:
        manifest['retention_ids'] = retention_ids
    if case_id in PRESERVE_CASE_IDS:
        manifest['preserve_sum_witness'] = {
            'source_deform_sums': [{'index': index, 'sum': math.fsum(weight for name, weight
                in rows[index]['weights'].items() if name in ('A', 'B', 'C'))} for index in (0, 1)],
            'unlocked_zero_group': 'C', 'native_absent_group_row0': 'B',
            'oracle': 'Independent LBS weighted-position accumulation divided by actual native positive total; v1 normalized assertion unchanged',
            'native_source': 'Blender 5.2.1 armature_deform.cc BoneDeformLinearMixer::finalize armature_weight/total; no DQ assertion'}
    if case_id in ('inherit_unselected_invalid_weight', 'reject_selected_invalid_weight'):
        manifest['invalid_raw_witness'] = {'index': bad_index, 'group': 'Mask',
            'selected': bad_index in manifest['patched_vertex_indices'],
            'native_f32le_hex': struct.pack('<f', actual_bad).hex(), 'creation': 'native VertexGroupElement collection foreach_set; scalar and bmesh clamp'}
    if case_id in ('reject_shape_key_consumer', 'reject_modifier_consumer', 'untouched_consumers_positive', 'reject_preserve_consumer'):
        manifest['consumer_witness'] = {
            'group': 'C' if case_id == 'untouched_consumers_positive' else 'A',
            'requested_change': case_id != 'untouched_consumers_positive',
            'kinds': ['shape_key', 'modifier'] if case_id == 'untouched_consumers_positive' else
                     ['shape_key'] if case_id in ('reject_shape_key_consumer', 'reject_preserve_consumer') else ['modifier']}
    if case_id == 'reject_nan':
        manifest['runtime_injection'] = {'kind': 'nonfinite_weight', 'row': 0,
                                         'group': 'A', 'value': 'NaN',
                                         'instruction': 'Coordinator assigns float("nan") only to a runtime request copy; manifest remains finite JSON.'}
    if case_id in ('rollback_injected', 'preserve_rollback_injected'):
        manifest['runtime_injection'] = {'kind': 'one_shot_after_native_write', 'core_helper': '_set_row',
                                         'after_successful_call': 1, 'exception': 'RuntimeError',
                                         'message': 'fixture injected after first completed native weight row',
                                         'instruction': 'Coordinator calls original helper, raises once after its first successful call, then allows rollback calls through; restore helper in finally.'}
    set_pose(manifest, 0)
    return manifest
