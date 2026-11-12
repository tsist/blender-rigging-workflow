"""Explicit local, original-rig maintenance. Never generates a replacement rig.

All editing helpers require a pack created by clone(). Objects, meshes, keys,
actions and widgets owned by that pack are independently copied. External IDs
remain read-only references and are reported. No matching/IK/FK semantics are
inferred from names.
"""
import hashlib
import json
import math
import struct
import uuid
from contextlib import contextmanager

import bpy

STATE_KEY = 'rigging_native_maintenance_v1'
_VERSION = 1
_CHANNELS = {'location': 3, 'rotation_euler': 3, 'rotation_quaternion': 4, 'scale': 3}
_WALK_COLLECTIONS = {'bones', 'constraints', 'modifiers', 'drivers', 'variables',
                     'targets', 'nla_tracks', 'strips', 'key_blocks'}
_WALK_POINTERS = {'pose', 'animation_data', 'driver'}
_SKIP_POINTERS = {'rna_type', 'id_data', 'original', 'library', 'override_library'}


def _id_ref(value):
    return {'type': value.bl_rna.identifier, 'name': value.name,
            'library': value.library.filepath if value.library else None}


def _json_value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bpy.types.ID):
        return _id_ref(value)
    if hasattr(value, 'to_dict'):
        return {str(k): _json_value(v) for k, v in value.to_dict().items()}
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    try:
        return [_json_value(v) for v in value]
    except TypeError:
        return str(value)


def _custom(owner):
    try:
        return {k: _json_value(owner[k]) for k in owner.keys() if k != STATE_KEY}
    except TypeError:
        return {}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf8')).hexdigest()


def _native(owner):
    result = {}
    for prop in owner.bl_rna.properties:
        if prop.identifier in {'rna_type', 'is_valid', 'error_location', 'error_rotation'}:
            continue
        if prop.type == 'COLLECTION':
            continue
        try:
            result[prop.identifier] = _json_value(getattr(owner, prop.identifier))
        except (AttributeError, TypeError, RuntimeError):
            continue
    return result


def _local(obj, kind=None):
    if not isinstance(obj, bpy.types.Object) or (kind and obj.type != kind):
        raise ValueError('Expected a local ' + (kind or 'Object'))
    if obj.library or obj.override_library or (obj.data and (obj.data.library or obj.data.override_library)):
        raise ValueError('Linked and library override inputs are not supported: ' + obj.name)
    if obj.mode == 'EDIT':
        raise ValueError('Leave Edit Mode before reading or copying this input: ' + obj.name)
    return obj


def _input(rig, objects):
    _local(rig, 'ARMATURE')
    if objects is None:
        objects = _pack(rig)['objects']
    objects = tuple(objects)
    if not objects:
        raise ValueError('Explicit nonempty Mesh inputs are required')
    seen = {rig.as_pointer()}
    for obj in objects:
        _local(obj, 'MESH')
        if obj.as_pointer() in seen:
            raise ValueError('Duplicate input Object')
        seen.add(obj.as_pointer())
    return objects


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(field + ' must be a nonempty string of at most 4096 characters')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Expected a finite number')
    return float(value)


def _vector(value, length):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError('Expected a numeric vector of length ' + str(length))
    return [_number(v) for v in value]


def _mapping(rig, objects, mapping):
    if not isinstance(mapping, dict) or set(mapping) - {'controls', 'properties', 'keys'}:
        raise ValueError('Mapping must contain controls/properties/keys lists only')
    result, seen = {}, set()
    for section in ('controls', 'properties', 'keys'):
        rows = mapping.get(section, [])
        if not isinstance(rows, (list, tuple)):
            raise ValueError(section + ' must be a list')
        result[section] = []
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError('Mapping rows must be dictionaries')
            fields = {'id', 'label', 'group', 'visible'}
            fields |= {'object_index', 'key'} if section == 'keys' else {'bone'}
            if section == 'properties':
                fields.add('property')
            if set(item) - fields:
                raise ValueError('Unknown mapping field')
            row = {k: _text(item.get(k), k) for k in ('id', 'label', 'group')}
            if row['id'] in seen:
                raise ValueError('Mapping IDs must be globally unique')
            seen.add(row['id'])
            visible = item.get('visible', True)
            if not isinstance(visible, bool):
                raise ValueError('visible must be boolean')
            row['visible'] = visible
            if section == 'keys':
                index = item.get('object_index')
                if type(index) is not int or not 0 <= index < len(objects):
                    raise ValueError('Invalid mapping object_index')
                key = objects[index].data.shape_keys
                key_name = _text(item.get('key'), 'key')
                if not key or key_name not in key.key_blocks or key.key_blocks[key_name] == key.reference_key:
                    raise ValueError('Mapping requires an existing non-basis ShapeKey')
                row.update(object_index=index, key=key_name)
            else:
                bone = _text(item.get('bone'), 'bone')
                if bone not in rig.pose.bones:
                    raise ValueError('Unknown original bone: ' + bone)
                row['bone'] = bone
                if section == 'properties':
                    name = _text(item.get('property'), 'property')
                    pb = rig.pose.bones[bone]
                    if name not in pb or not isinstance(pb[name], (bool, int, float)):
                        raise ValueError('Mapping property must be an existing numeric/bool original property')
                    row['property'] = name
            result[section].append(row)
    return result


def _group_objects(group):
    return [group[k] for k in sorted(group.keys())]


def _pack(rig):
    _local(rig, 'ARMATURE')
    state = rig.get(STATE_KEY)
    if not state or state.get('version') != _VERSION or state.get('rig') != rig:
        raise ValueError('This Object is not an original-rig maintenance pack')
    objects = _group_objects(state['objects'])
    widgets = _group_objects(state['widgets'])
    dependencies = _group_objects(state['dependencies'])
    if not all(isinstance(o, bpy.types.Object) for o in objects + widgets + dependencies):
        raise ValueError('Maintenance pack has missing member references')
    members = [rig] + objects + widgets + dependencies
    if 'owned_armature' not in state or state['owned_armature'] != rig.data:
        raise ValueError('Maintenance rig Armature was reattached; create a new explicit pack')
    expected_data = state.get('owned_data')
    if expected_data is None:
        raise ValueError('Maintenance pack lacks persistent owned-data identities')
    for index, obj in enumerate(members):
        _local(obj)
        expected = expected_data.get('member_%06d' % index)
        if expected != obj.data:
            raise ValueError('Maintenance member data was reattached: ' + obj.name)
        if obj.data:
            consumers = {o for o in bpy.data.objects if o.data == obj.data}
            if not consumers <= set(members):
                raise ValueError('Maintenance data has consumers outside this pack: ' + obj.data.name)
    expected_keys = state.get('owned_keys', {})
    for index, obj in enumerate(objects):
        key = obj.data.shape_keys
        expected = expected_keys.get('member_%06d' % index)
        if expected is not None and expected != key:
            raise ValueError('Maintenance ShapeKey identity changed: ' + obj.name)
        # Native creation of a new Key on an unchanged owned Mesh is permitted;
        # a Key may never be shared by a different Mesh outside this pack.
        if key and not {m for m in bpy.data.meshes if m.shape_keys == key} <= {o.data for o in objects}:
            raise ValueError('Maintenance ShapeKey has consumers outside this pack: ' + key.name)
    return {'state': state, 'objects': objects, 'widgets': widgets,
            'dependencies': dependencies, 'collection': state['collection']}


def _driver_rows(owner):
    ad = getattr(owner, 'animation_data', None)
    if not ad:
        return []
    return [{'path': f.data_path, 'index': f.array_index, 'mute': f.mute,
             'type': f.driver.type, 'expression': f.driver.expression,
             'use_self': f.driver.use_self,
             'variables': [{'name': v.name, 'type': v.type,
                            'targets': [_native(t) for t in v.targets]} for v in f.driver.variables]}
            for f in ad.drivers]


def _action_rows(owner):
    ad = getattr(owner, 'animation_data', None)
    if not ad:
        return None
    return {'action': _id_ref(ad.action) if ad.action else None,
            'slot': getattr(getattr(ad, 'action_slot', None), 'identifier', None),
            'nla': [{'name': t.name, 'mute': t.mute,
                     'strips': [{'name': s.name, 'type': s.type, 'action': _id_ref(s.action) if s.action else None,
                                 'slot': getattr(getattr(s, 'action_slot', None), 'identifier', None),
                                 'frame_start': s.frame_start, 'frame_end': s.frame_end}
                                for s in _strip_tree(t.strips)]} for t in ad.nla_tracks]}


def _strip_tree(strips):
    for strip in strips:
        yield strip
        if strip.type == 'META':
            yield from _strip_tree(strip.strips)


def _walk(owner, visit, seen=None):
    """Walk reference-bearing RNA, excluding large geometry and read-only backrefs."""
    if seen is None:
        seen = set()
    if owner is None:
        return
    identity = owner.as_pointer() if hasattr(owner, 'as_pointer') else id(owner)
    if identity in seen:
        return
    seen.add(identity)
    if hasattr(owner, 'keys'):
        visit(owner, None, None)
    for p in owner.bl_rna.properties:
        name = p.identifier
        if name in _SKIP_POINTERS:
            continue
        if p.type == 'POINTER':
            value = getattr(owner, name, None)
            if isinstance(value, bpy.types.ID):
                visit(owner, name, value)
            elif name in _WALK_POINTERS:
                _walk(value, visit, seen)
        elif p.type == 'COLLECTION' and name in _WALK_COLLECTIONS:
            for value in getattr(owner, name):
                if not isinstance(value, bpy.types.ID):
                    _walk(value, visit, seen)


def _custom_ids(group):
    result = []
    try:
        keys = list(group.keys())
    except TypeError:
        return result
    for key in keys:
        if key == STATE_KEY:
            continue
        value = group[key]
        if isinstance(value, bpy.types.ID):
            result.append(value)
        elif hasattr(value, 'keys'):
            result.extend(_custom_ids(value))
    return result


def _remap_custom(group, idmap):
    try:
        keys = list(group.keys())
    except TypeError:
        return
    for key in keys:
        if key == STATE_KEY:
            continue
        value = group[key]
        if isinstance(value, bpy.types.ID):
            if value in idmap:
                group[key] = idmap[value]
        elif hasattr(value, 'keys'):
            _remap_custom(value, idmap)


def _remap(owner, idmap):
    def rewrite(parent, name, value):
        if name is None:
            _remap_custom(parent, idmap)
        elif value in idmap:
            if parent.bl_rna.properties[name].is_readonly:
                if getattr(parent, name) != idmap[value]:
                    raise ValueError('Cannot redirect internal readonly ID reference: ' + name)
            else:
                setattr(parent, name, idmap[value])
    _walk(owner, rewrite)


def _references(owners):
    references = set()
    def collect(parent, name, value):
        if name is None:
            references.update(_custom_ids(parent))
        elif value:
            references.add(value)
    for owner in owners:
        _walk(owner, collect)
        if isinstance(owner, bpy.types.Mesh):
            references.update(m for m in owner.materials if m)
    return references


def _actions(owners):
    result = set()
    for owner in owners:
        ad = getattr(owner, 'animation_data', None)
        if not ad:
            continue
        if ad.action:
            result.add(ad.action)
        for track in ad.nla_tracks:
            for strip in _strip_tree(track.strips):
                if strip.action:
                    result.add(strip.action)
        state = owner.get(STATE_KEY) if isinstance(owner, bpy.types.Object) else None
        if state and 'action_checkpoints' in state:
            result.update(a for a in state['action_checkpoints'].values() if isinstance(a, bpy.types.Action))
    return result


def _action_signature(action):
    channels = []
    for layer in action.layers:
        for strip in layer.strips:
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag:
                    channels.append({'layer': layer.name, 'slot': slot.identifier,
                                     'curves': [{'path': f.data_path, 'index': f.array_index,
                                                 'keys': [list(k.co) for k in f.keyframe_points],
                                                 'interpolation': [k.interpolation for k in f.keyframe_points],
                                                 'modifiers': [_native(m) for m in f.modifiers]}
                                                for f in bag.fcurves]})
    return {'slots': [s.identifier for s in action.slots], 'channels': channels}


def _owner_ids(objects):
    result = set(objects)
    for obj in objects:
        if obj.data:
            result.add(obj.data)
            keys = getattr(obj.data, 'shape_keys', None)
            if keys:
                result.add(keys)
    return result


def _weights(obj):
    h = hashlib.sha256()
    rows = [{'name': g.name, 'index': g.index, 'lock_weight': g.lock_weight,
             'positive_count': 0, 'sum': 0.0} for g in obj.vertex_groups]
    for vertex in obj.data.vertices:
        for weight in vertex.groups:
            h.update(struct.pack('<IIf', vertex.index, weight.group, weight.weight))
            rows[weight.group]['positive_count'] += weight.weight > 0
            rows[weight.group]['sum'] += weight.weight
    return {'sha256': h.hexdigest(), 'groups': rows}


def inspect(rig, objects=None):
    """Read native state and references without saving or adding datablocks."""
    objects = _input(rig, objects)
    bones = []
    for bone in rig.data.bones:
        pb = rig.pose.bones[bone.name]
        bones.append({'name': bone.name, 'parent': bone.parent.name if bone.parent else None,
                      'head': list(bone.head_local), 'tail': list(bone.tail_local),
                      'matrix_local': [list(r) for r in bone.matrix_local],
                      'connected': bone.use_connect, 'use_deform': bone.use_deform,
                      'rotation_mode': pb.rotation_mode, 'location': list(pb.location),
                      'rotation_euler': list(pb.rotation_euler),
                      'rotation_quaternion': list(pb.rotation_quaternion), 'scale': list(pb.scale),
                      'locks': {'location': list(pb.lock_location), 'rotation': list(pb.lock_rotation),
                                'scale': list(pb.lock_scale)}, 'properties': _custom(pb),
                      'custom_shape': _id_ref(pb.custom_shape) if pb.custom_shape else None,
                      'constraints': [_native(c) for c in pb.constraints]})
    meshes = []
    for obj in objects:
        keys = obj.data.shape_keys
        mesh = {'object': _id_ref(obj), 'data': _id_ref(obj.data),
                'vertices': len(obj.data.vertices), 'polygons': len(obj.data.polygons),
                'parent': _id_ref(obj.parent) if obj.parent else None,
                'modifiers': [_native(m) for m in obj.modifiers], 'weights': _weights(obj),
                'drivers': _driver_rows(obj), 'data_drivers': _driver_rows(obj.data),
                'animation': _action_rows(obj), 'key': None}
        if keys:
            mesh['key'] = {'id': _id_ref(keys), 'use_relative': keys.use_relative,
                           'blocks': [{'name': k.name, 'value': k.value, 'mute': k.mute,
                                       'relative': k.relative_key.name if k.relative_key else None,
                                       'vertices': len(k.data),
                                       'coordinates_sha256': _hash([list(v.co) for v in k.data])}
                                      for k in keys.key_blocks],
                           'drivers': _driver_rows(keys), 'animation': _action_rows(keys)}
        meshes.append(mesh)
    widgets = {p.custom_shape for p in rig.pose.bones if p.custom_shape}
    owners = _owner_ids([rig] + list(objects) + list(widgets))
    actions = _actions(owners)
    internal = owners | actions
    refs = _references(owners | actions) - internal
    external = []
    for ref in sorted(refs, key=lambda i: (i.bl_rna.identifier, i.name)):
        row = _id_ref(ref)
        if hasattr(ref, 'filepath'):
            row['filepath'] = ref.filepath
        external.append(row)
    return {'schema_version': _VERSION, 'rig': _id_ref(rig), 'data': _id_ref(rig.data),
            'bones': bones, 'properties': _custom(rig), 'data_properties': _custom(rig.data),
            'constraints': [_native(c) for c in rig.constraints],
            'drivers': _driver_rows(rig), 'data_drivers': _driver_rows(rig.data),
            'animation': _action_rows(rig), 'meshes': meshes,
            'actions': [{'id': _id_ref(a), 'signature': _action_signature(a)}
                        for a in sorted(actions, key=lambda a: a.name)],
            'external_dependencies': external,
            'maintenance': STATE_KEY in rig, 'inferred_C94_capabilities': False}


@contextmanager
def _context():
    view = bpy.context.view_layer
    active = view.objects.active
    selected = list(bpy.context.selected_objects)
    mode = active.mode if active else 'OBJECT'
    scene = bpy.context.scene
    frame, subframe = scene.frame_current, scene.frame_subframe
    try:
        yield
    finally:
        current = view.objects.active
        if current and current.mode != 'OBJECT' and (current != active or current.mode != mode):
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        for obj in selected:
            if view.objects.get(obj.name) == obj:
                obj.select_set(True)
        if active and view.objects.get(active.name) == active:
            view.objects.active = active
            if active.mode != mode:
                bpy.ops.object.mode_set(mode=mode)
        else:
            view.objects.active = None
        if scene.frame_current != frame or scene.frame_subframe != subframe:
            scene.frame_set(frame, subframe=subframe)


def _cleanup(owned):
    # Every ID in this list was returned by this transaction's own copy/new call.
    for obj in [i for i in owned if isinstance(i, bpy.types.Object)]:
        bpy.data.objects.remove(obj, do_unlink=True)
    remaining = [i for i in owned if not isinstance(i, bpy.types.Object)]
    if remaining:
        bpy.data.batch_remove(ids=remaining)


def _members(values):
    return {'member_%06d' % i: value for i, value in enumerate(values)}


def _clone(rig, objects, label, mapping, dependencies, owned):
    widgets = []
    for pb in rig.pose.bones:
        if pb.custom_shape and pb.custom_shape not in widgets and pb.custom_shape not in objects:
            widgets.append(pb.custom_shape)
    all_objects = list(dict.fromkeys([rig] + list(objects) + list(widgets) + list(dependencies)))
    for obj in all_objects:
        _local(obj)
    idmap = {}
    source_owners = _owner_ids(all_objects)
    source_actions = _actions(source_owners)
    for action in source_actions:
        if action.library or action.override_library:
            raise ValueError('Linked/override Action cannot be independently copied')
    collection = bpy.data.collections.new(label)
    owned.append(collection)
    bpy.context.scene.collection.children.link(collection)
    for source in all_objects:
        copied = source.copy()
        owned.append(copied)
        copied.name = label + '__' + source.name
        if STATE_KEY in copied:
            del copied[STATE_KEY]
        idmap[source] = copied
        if source.data:
            if source.data not in idmap:
                copied_data = source.data.copy()
                owned.append(copied_data)
                idmap[source.data] = copied_data
                old_key = getattr(source.data, 'shape_keys', None)
                new_key = getattr(copied_data, 'shape_keys', None)
                if old_key:
                    if not new_key or new_key == old_key:
                        raise ValueError('Native Mesh copy did not independently copy ShapeKeys')
                    idmap[old_key] = new_key
                    owned.append(new_key)
            copied.data = idmap[source.data]
        collection.objects.link(copied)
    for action in source_actions:
        copied = action.copy()
        owned.append(copied)
        copied.name = label + '__' + action.name
        idmap[action] = copied
    for source, copied in list(idmap.items()):
        _remap(copied, idmap)
        # Rebinding an Action must preserve its exact layered slot, including NLA.
        old_ad, new_ad = getattr(source, 'animation_data', None), getattr(copied, 'animation_data', None)
        if old_ad and new_ad:
            if old_ad.action:
                new_ad.action = idmap[old_ad.action]
                old_slot = old_ad.action_slot
                if old_slot:
                    new_ad.action_slot = next(s for s in new_ad.action.slots if s.identifier == old_slot.identifier)
            for old_track, new_track in zip(old_ad.nla_tracks, new_ad.nla_tracks):
                old_tree, new_tree = list(_strip_tree(old_track.strips)), list(_strip_tree(new_track.strips))
                if len(old_tree) != len(new_tree):
                    raise ValueError('NLA strip tree changed during native copy')
                for old_strip, new_strip in zip(old_tree, new_tree):
                    if old_strip.action:
                        new_strip.action = idmap[old_strip.action]
                        old_slot = old_strip.action_slot
                        if old_slot:
                            new_strip.action_slot = next(s for s in new_strip.action.slots if s.identifier == old_slot.identifier)
                        if new_strip.action != idmap[old_strip.action] or (old_slot and new_strip.action_slot.identifier != old_slot.identifier):
                            raise ValueError('Nested NLA Action/slot redirection failed')
    new_rig = idmap[rig]
    for old_pb in rig.pose.bones:
        new_pb = new_rig.pose.bones[old_pb.name]
        transform = old_pb.custom_shape_transform
        if transform and transform.id_data in idmap:
            new_pb.custom_shape_transform = idmap[transform.id_data].pose.bones[transform.name]
    # Verify no copied reference-bearing RNA or custom property points back inside source pack.
    copied_owners = set(idmap.values())
    backrefs = _references(copied_owners) & set(idmap)
    if backrefs:
        raise ValueError('Unredirected internal references: ' + ', '.join(sorted(i.name for i in backrefs)))
    for action in source_actions:
        if _action_signature(action) != _action_signature(idmap[action]):
            raise ValueError('Action slots/channels changed during native copy')
    new_objects = [idmap[o] for o in objects]
    new_widgets = [idmap[o] for o in widgets]
    new_dependencies = [idmap[o] for o in dependencies if o not in widgets and o not in objects and o != rig]
    new_rig[STATE_KEY] = {'version': _VERSION, 'pack_id': uuid.uuid4().hex,
                          'rig': new_rig, 'collection': collection,
                          'objects': _members(new_objects), 'widgets': _members(new_widgets),
                          'dependencies': _members(new_dependencies),
                          'mapping_json': json.dumps(mapping, ensure_ascii=False),
                          'source_rig_name': rig.name, 'label': label,
                          'source_report_sha256': _hash(inspect(rig, objects))}
    # Store actual ID references, not names; later data reattachment is an
    # explicit invalidation, never permission to edit the newly shared source.
    new_members = [new_rig] + new_objects + new_widgets + new_dependencies
    new_rig[STATE_KEY]['owned_armature'] = new_rig.data
    new_rig[STATE_KEY]['owned_data'] = {
        'member_%06d' % i: obj.data for i, obj in enumerate(new_members) if obj.data}
    new_rig[STATE_KEY]['owned_keys'] = {
        'member_%06d' % i: obj.data.shape_keys for i, obj in enumerate(new_objects) if obj.data.shape_keys}
    source_state = rig.get(STATE_KEY)
    if source_state and 'action_checkpoints' in source_state:
        new_rig[STATE_KEY]['action_checkpoints'] = {
            key: idmap[action] for key, action in source_state['action_checkpoints'].items()}
    report = inspect(new_rig, new_objects)
    report.update(operation='clone', label=label, original_rig_name=rig.name,
                  copied_ids=[{'source': _id_ref(source), 'copy': _id_ref(copy)} for source, copy in idmap.items()],
                  action_slots_verified=True, internal_references_redirected=True)
    return {'rig': new_rig, 'objects': new_objects, 'collection': collection, 'report': report}


def clone(rig, objects, *, label, mapping, dependencies=()):
    """Create a separately owned maintenance pack from explicit local inputs."""
    objects = _input(rig, objects)
    label = _text(label, 'label')
    mapping = _mapping(rig, objects, mapping)
    dependencies = tuple(dependencies)
    if len({o.as_pointer() for o in dependencies if isinstance(o, bpy.types.Object)}) != len(dependencies):
        raise ValueError('Dependencies must be unique local Objects')
    for obj in dependencies:
        _local(obj)
    owned = []
    with _context():
        try:
            return _clone(rig, objects, label, mapping, dependencies, owned)
        except BaseException:
            _cleanup(owned)
            raise


def get_layout(rig):
    """Return the persisted original-control mapping in its declared order."""
    pack = _pack(rig)
    return _mapping(rig, pack['objects'], json.loads(pack['state']['mapping_json']))


def set_layout(rig, mapping):
    """Replace an explicitly supplied layout; existing IDs cannot change targets."""
    pack = _pack(rig)
    mapping = _mapping(rig, pack['objects'], mapping)
    previous = get_layout(rig)
    old = {r['id']: (section, r) for section, rows in previous.items() for r in rows}
    for section, rows in mapping.items():
        for row in rows:
            if row['id'] not in old:
                continue
            old_section, old_row = old[row['id']]
            targets = ('bone', 'property') if section == 'properties' else ('bone',) if section == 'controls' else ('object_index', 'key')
            if old_section != section or any(row[k] != old_row[k] for k in targets):
                raise ValueError('An existing mapping ID cannot be retargeted')
    pack['state']['mapping_json'] = json.dumps(mapping, ensure_ascii=False)
    return get_layout(rig)


def _row(rig, section, item_id):
    _text(item_id, 'id')
    for row in get_layout(rig)[section]:
        if row['id'] == item_id:
            return row
    raise ValueError('Unknown maintenance mapping ID: ' + item_id)


def set_control(rig, control_id, channel, value):
    row = _row(rig, 'controls', control_id)
    if channel not in _CHANNELS:
        raise ValueError('Only native location/rotation_euler/rotation_quaternion/scale channels are supported')
    pb = rig.pose.bones[row['bone']]
    value = _vector(value, _CHANNELS[channel])
    old = list(getattr(pb, channel))
    if channel == 'rotation_euler' and pb.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        raise ValueError('Bone does not use Euler rotation')
    if channel == 'rotation_quaternion' and pb.rotation_mode != 'QUATERNION':
        raise ValueError('Bone does not use Quaternion rotation')
    if channel == 'rotation_quaternion':
        if sum(v * v for v in value) < 1e-20:
            raise ValueError('Quaternion must be nonzero')
        locks = [pb.lock_rotation_w] + list(pb.lock_rotation) if pb.lock_rotations_4d else [any(pb.lock_rotation)] * 4
    else:
        locks = list(getattr(pb, 'lock_' + ('rotation' if channel == 'rotation_euler' else channel)))
    if any(lock and abs(a - b) > 1e-12 for lock, a, b in zip(locks, old, value)):
        raise ValueError('Requested channel changes a locked original control component')
    setattr(pb, channel, value)
    rig.update_tag()
    bpy.context.view_layer.update()
    return {'id': control_id, 'bone': pb.name, 'channel': channel, 'before': old, 'after': list(getattr(pb, channel))}


def set_property(rig, property_id, value):
    row = _row(rig, 'properties', property_id)
    pb = rig.pose.bones[row['bone']]
    old = pb[row['property']]
    if isinstance(old, bool):
        if type(value) is not bool:
            raise ValueError('Original property requires boolean')
    elif isinstance(old, int):
        if type(value) is not int:
            raise ValueError('Original property requires integer')
    else:
        value = _number(value)
    bounds = pb.id_properties_ui(row['property']).as_dict()
    if not isinstance(value, bool) and (value < bounds.get('min', -math.inf) or value > bounds.get('max', math.inf)):
        raise ValueError('Property value is outside native UI bounds')
    pb[row['property']] = value
    rig.update_tag()
    bpy.context.view_layer.update()
    return {'id': property_id, 'before': old, 'after': pb[row['property']]}


def set_key(rig, key_id, value):
    row = _row(rig, 'keys', key_id)
    obj = _pack(rig)['objects'][row['object_index']]
    key = obj.data.shape_keys.key_blocks[row['key']]
    value = _number(value)
    if not key.slider_min <= value <= key.slider_max:
        raise ValueError('ShapeKey value is outside its native slider bounds')
    old = key.value
    key.value = value
    obj.data.update()
    bpy.context.view_layer.update()
    return {'id': key_id, 'before': old, 'after': key.value}


def record_controls(rig, control_ids=None):
    """Record current mapped native transforms on a new independent Action.

    Single-layer/keyframe-strip ordinary Actions only. Active NLA tracks must
    be muted or disabled; drivers on target input channels are rejected. The
    replaced Action is retained by an ID reference, never a fake-user edit.
    """
    from . import mode_animation as animation
    pack = _pack(rig)
    layout = get_layout(rig)
    available = {r['id']: r for r in layout['controls']}
    if control_ids is None:
        control_ids = [r['id'] for r in layout['controls'] if r['visible']]
    if not isinstance(control_ids, (list, tuple)) or not control_ids or any(not isinstance(i, str) for i in control_ids):
        raise ValueError('Select explicit mapped native control IDs')
    if len(set(control_ids)) != len(control_ids) or not set(control_ids) <= set(available):
        raise ValueError('Unknown or duplicate native control ID')
    names = list(dict.fromkeys(available[i]['bone'] for i in control_ids))
    scene = bpy.context.scene
    if rig.data.pose_position != 'POSE' or scene.render.frame_map_old != scene.render.frame_map_new:
        raise ValueError('Recording requires pose evaluation without scene time remapping')
    frame_raw = scene.frame_current + scene.frame_subframe
    frame = animation._f32(frame_raw)
    binding = animation._binding(rig)
    rows = animation._rows(rig, names, [])
    animation._preflight(rig, binding, rows)
    snapshot = animation._snapshot(rig)
    action = None
    with _context():
        try:
            action, slot = animation._make_action(rig, binding)
            animation._write_rows(action, slot, rows, frame)
            ad = rig.animation_data_create()
            ad.action = action
            ad.action_slot = slot
            if binding['action'] is None:
                ad.action_blend_type = 'REPLACE'
                ad.action_influence = 1.0
                ad.action_extrapolation = 'HOLD'
            evaluated = animation.pose._update(rig)
            for row in rows:
                value = evaluated.path_resolve(row['path'])[row['index']]
                if animation._f32(value) != row['value']:
                    raise ValueError('Recorded Action does not reproduce the current native control input')
            animation._check_inputs(rig, snapshot, {(r['path'], r['index']) for r in rows})
            if scene.frame_current + scene.frame_subframe != frame_raw:
                raise ValueError('Frame changed during recording')
            if binding['action']:
                if 'action_checkpoints' not in pack['state']:
                    pack['state']['action_checkpoints'] = {}
                pack['state']['action_checkpoints']['action_' + uuid.uuid4().hex] = binding['action']
            return {'ok': True, 'action': action.name, 'slot': slot.identifier, 'frame': frame,
                    'control_ids': list(control_ids), 'bones': names, 'keyed_channels': len(rows),
                    'original_action': binding['action'].name if binding['action'] else None,
                    'original_retention': 'persistent pack Action ID reference; no old Action or fake-user changes',
                    'scope': 'current mapped control transforms only; no matching, previous-frame keys or NLA bake'}
        except BaseException:
            animation._restore_binding(rig, binding)
            animation.pose._update(rig)
            for name, values in snapshot['bones'].items():
                pb = rig.pose.bones[name]
                for field in animation.pose._CHANNELS:
                    setattr(pb, field, values[field])
                pb.rotation_mode = values['rotation_mode']
            for field in animation._OBJECT_CHANNELS:
                setattr(rig, field, snapshot['object'][field])
            rig.rotation_mode = snapshot['object']['rotation_mode']
            for (path, index), value in snapshot['animated'].items():
                animation._restore_value(rig, path, index, value)
            animation.pose._update(rig)
            if action is not None:
                bpy.data.actions.remove(action, do_unlink=True)
            raise


def _repair_plan(rig, pack, constraint_edits, bone_edits, weight_edits, acknowledge):
    constraints, bones, weights, seen = [], [], [], set()
    members = {rig} | set(pack['objects'] + pack['widgets'] + pack['dependencies'])
    for edit in constraint_edits:
        if not isinstance(edit, dict) or set(edit) != {'bone', 'constraint', 'field', 'value'}:
            raise ValueError('Constraint edit requires bone/constraint/field/value')
        bone, name, field = edit['bone'], edit['constraint'], edit['field']
        if bone not in rig.pose.bones or name not in rig.pose.bones[bone].constraints:
            raise ValueError('Unknown original constraint')
        if field not in {'influence', 'mute', 'target', 'subtarget'}:
            raise ValueError('Unsupported constraint repair field')
        key = ('constraint', bone, name, field)
        if key in seen:
            raise ValueError('Duplicate repair write')
        seen.add(key)
        constraint = rig.pose.bones[bone].constraints[name]
        if not hasattr(constraint, field) or constraint.bl_rna.properties[field].is_readonly:
            raise ValueError('Constraint has no writable field: ' + field)
        value = edit['value']
        if field == 'influence':
            value = _number(value)
            if not 0 <= value <= 1:
                raise ValueError('Constraint influence must be in [0,1]')
        elif field == 'mute':
            if type(value) is not bool:
                raise ValueError('Constraint mute requires boolean')
        elif field == 'target':
            if value not in members:
                raise ValueError('Constraint target must be an explicit Object of this pack')
        elif not isinstance(value, str):
            raise ValueError('Constraint subtarget requires a bone name string')
        constraints.append(dict(edit, value=value, before=_json_value(getattr(constraint, field))))
    # Validate subtargets against the complete proposed target edits, independent of input ordering.
    for edit in constraints:
        if edit['field'] not in {'target', 'subtarget'}:
            continue
        c = rig.pose.bones[edit['bone']].constraints[edit['constraint']]
        related = [e for e in constraints if e['bone'] == edit['bone'] and e['constraint'] == edit['constraint']]
        target = next((e['value'] for e in related if e['field'] == 'target'), getattr(c, 'target', None))
        subtarget = next((e['value'] for e in related if e['field'] == 'subtarget'), getattr(c, 'subtarget', ''))
        if subtarget and (target not in members or target.type != 'ARMATURE' or subtarget not in target.data.bones):
            raise ValueError('Subtarget must resolve to an actual bone in this pack')
    for edit in bone_edits:
        if not acknowledge:
            raise ValueError('Rest-bone edits require acknowledge_bone_change=True')
        if not isinstance(edit, dict) or 'bone' not in edit or not set(edit) <= {'bone', 'head', 'tail', 'roll'} or len(edit) < 2:
            raise ValueError('Bone edit requires bone and explicit head/tail/roll fields')
        name = edit['bone']
        if name not in rig.data.bones or ('bone', name) in seen:
            raise ValueError('Unknown or duplicate rest-bone edit')
        seen.add(('bone', name))
        bone = rig.data.bones[name]
        if bone.use_connect or any(c.use_connect for c in bone.children):
            raise ValueError('Connected rest-bone maintenance is not supported')
        row = {'bone': name}
        for field in ('head', 'tail', 'roll'):
            if field in edit:
                row[field] = _number(edit[field]) if field == 'roll' else _vector(edit[field], 3)
        head, tail = row.get('head', list(bone.head_local)), row.get('tail', list(bone.tail_local))
        if sum((a - b) ** 2 for a, b in zip(head, tail)) < 1e-16:
            raise ValueError('Rest-bone length must be nonzero')
        row['impact'] = {'before_head': list(bone.head_local), 'before_tail': list(bone.tail_local),
                         'before_matrix': [list(r) for r in bone.matrix_local],
                         'children': [b.name for b in bone.children], 'use_deform': bone.use_deform,
                         'weighted_objects': [o.name for o in pack['objects'] if name in o.vertex_groups],
                         'warning': 'Rest change may affect skin, descendants, constraints and existing animation; no neighbor compensation is performed.'}
        bones.append(row)
    for edit in weight_edits:
        if not isinstance(edit, dict) or set(edit) != {'object_index', 'indices', 'assignments'}:
            raise ValueError('Weight edit requires object_index/indices/assignments')
        index, indices, assignments = edit['object_index'], edit['indices'], edit['assignments']
        if type(index) is not int or not 0 <= index < len(pack['objects']):
            raise ValueError('Invalid weight object_index')
        obj = pack['objects'][index]
        if not isinstance(indices, (list, tuple)) or not indices or any(type(i) is not int or not 0 <= i < len(obj.data.vertices) for i in indices) or len(set(indices)) != len(indices):
            raise ValueError('Weight indices must be unique explicit existing RAW vertices')
        if not isinstance(assignments, dict) or not assignments:
            raise ValueError('Explicit nonempty weight assignments required')
        clean = {}
        for group, values in assignments.items():
            if group not in obj.vertex_groups or obj.vertex_groups[group].lock_weight:
                raise ValueError('Weight group is missing or locked: ' + str(group))
            if not isinstance(values, (list, tuple)) or len(values) != len(indices):
                raise ValueError('Each assignment must match the explicit indices')
            values = [_number(v) for v in values]
            if any(v < 0 or v > 1 for v in values):
                raise ValueError('Weights must be in [0,1]')
            for vertex in indices:
                key = ('weight', index, vertex, group)
                if key in seen:
                    raise ValueError('Duplicate weight repair write')
                seen.add(key)
            clean[group] = values
        weights.append({'object_index': index, 'indices': list(indices), 'assignments': clean})
    if not constraints and not bones and not weights:
        raise ValueError('A repair must contain explicit edits')
    return constraints, bones, weights


def repair(rig, *, label, reason, constraint_edits=(), bone_edits=(), weight_edits=(), acknowledge_bone_change=False):
    """Validate a bounded repair, clone a recovery candidate, then edit only it."""
    pack = _pack(rig)
    label, reason = _text(label, 'label'), _text(reason, 'reason')
    mapping = get_layout(rig)
    constraints, bones, weights = _repair_plan(rig, pack, constraint_edits, bone_edits, weight_edits, acknowledge_bone_change)
    owned = []
    with _context():
        try:
            result = _clone(rig, pack['objects'], label, mapping, pack['dependencies'], owned)
            candidate = result['rig']
            newpack = _pack(candidate)
            member_map = {rig: candidate}
            for role in ('objects', 'widgets', 'dependencies'):
                member_map.update(zip(pack[role], newpack[role]))
            for edit in constraints:
                value = edit['value']
                if edit['field'] == 'target':
                    value = member_map[value]
                setattr(candidate.pose.bones[edit['bone']].constraints[edit['constraint']], edit['field'], value)
            if bones:
                current = bpy.context.view_layer.objects.active
                if current and current.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                for selected in list(bpy.context.selected_objects):
                    selected.select_set(False)
                candidate.select_set(True)
                bpy.context.view_layer.objects.active = candidate
                bpy.ops.object.mode_set(mode='EDIT')
                for edit in bones:
                    bone = candidate.data.edit_bones[edit['bone']]
                    for field in ('head', 'tail', 'roll'):
                        if field in edit:
                            setattr(bone, field, edit[field])
                bpy.ops.object.mode_set(mode='OBJECT')
            for edit in weights:
                obj = result['objects'][edit['object_index']]
                for group, values in edit['assignments'].items():
                    for index, value in zip(edit['indices'], values):
                        if value == 0:
                            obj.vertex_groups[group].remove([index])
                        else:
                            obj.vertex_groups[group].add([index], value, 'REPLACE')
            candidate.update_tag()
            bpy.context.view_layer.update()
            candidate[STATE_KEY]['repair_reason'] = reason
            candidate[STATE_KEY]['repair_plan_json'] = json.dumps(_json_value({'constraints': constraints, 'bones': bones, 'weights': weights}), ensure_ascii=False)
            result['report'] = inspect(candidate, result['objects'])
            result['report'].update(operation='repair', label=label, reason=reason,
                                    original_pack_id=pack['state']['pack_id'],
                                    checkpoint_source_preserved=True,
                                    impact_before_writes=_json_value({'constraints': constraints, 'bones': bones, 'weights': weights}))
            return result
        except BaseException:
            current = bpy.context.view_layer.objects.active
            if current and current in owned and current.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            _cleanup(owned)
            raise
