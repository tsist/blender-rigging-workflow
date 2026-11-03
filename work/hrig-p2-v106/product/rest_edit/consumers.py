"""Read-only, finite native rest-edit consumer qualification.

``admissible`` covers only the declared native domains. It never certifies
arbitrary Python, dynamic node strings, or every possible Blender consumer.
"""
from copy import deepcopy
import json

import bpy


class ConsumerError(ValueError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(f'{code}: {detail}')


def _require(value, code, detail):
    if not value:
        raise ConsumerError(code, detail)


def _pointer(value):
    try:
        return value.as_pointer()
    except (AttributeError, ReferenceError, TypeError):
        return None


def _identity(value):
    library = getattr(value, 'library', None)
    result = {'type': value.bl_rna.identifier, 'name': value.name,
              'library': bpy.path.abspath(library.filepath) if library else None}
    if isinstance(value, bpy.types.Key):
        result['mesh_owners'] = sorted(m.name for m in bpy.data.meshes if m.shape_keys == value)
    return result


def _main_ids():
    """All top-level Main IDs plus embedded node trees/master collections."""
    seen, pending = set(), []
    for prop in bpy.data.bl_rna.properties:
        if prop.type == 'COLLECTION':
            pending.extend(item for item in getattr(bpy.data, prop.identifier, ())
                           if isinstance(item, bpy.types.ID))
    while pending:
        item = pending.pop()
        pointer = item.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        yield item
        for name in ('node_tree', 'collection'):
            child = getattr(item, name, None)
            if isinstance(child, bpy.types.ID):
                pending.append(child)


def _prefixes(path):
    """RNA resolve candidates; respect dots/brackets inside quoted names."""
    ends, quote, escaped = {len(path)}, None, False
    for i, char in enumerate(path):
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char in ('.', '['):
            if i:
                ends.add(i)
        elif char == ']':
            ends.add(i + 1)
    return [path[:end] for end in sorted(ends) if end]


def scan(rig, body, profile, bone_name):
    """Return detached JSON; finding a consumer is a report, not an exception."""
    _require(isinstance(rig, bpy.types.Object) and rig.type == 'ARMATURE', 'RIG_INPUT', 'Armature Object required')
    _require(isinstance(body, bpy.types.Object) and body.type == 'MESH', 'BODY_INPUT', 'Mesh Object required')
    _require(type(profile) is dict and type(profile.get('roles')) is list
             and profile.get('rig', {}).get('object') == rig.name, 'PROFILE_INPUT', 'Current rig profile required')
    _require(type(bone_name) is str and bone_name in rig.data.bones, 'BONE_INPUT', bone_name)
    bone, pose_bone = rig.data.bones[bone_name], rig.pose.bones[bone_name]
    aliases = [obj for obj in bpy.data.objects if obj.type == 'ARMATURE' and obj.data == rig.data]
    alias_ptrs = {obj.as_pointer() for obj in aliases}
    target_ptrs = {bone.as_pointer()} | {obj.pose.bones[bone_name].as_pointer() for obj in aliases}
    blockers, skins, unresolved = [], [], []
    seen = set()
    counts = {'armatures': 0, 'pose_bones': 0, 'objects': 0, 'constraints': 0,
              'main_ids': 0, 'driver_curves': 0, 'driver_targets': 0, 'rna_paths': 0}

    def add(kind, owner, path, detail):
        item = {'kind': kind, 'owner': deepcopy(owner), 'path': path, 'detail': deepcopy(detail)}
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            blockers.append(item)
            seen.add(key)

    def unresolved_path(owner, path, reason, blocking):
        item = {'owner': deepcopy(owner), 'path': path, 'reason': reason, 'blocking': blocking}
        if item not in unresolved:
            unresolved.append(item)
        if blocking:
            add('UNRESOLVED_TARGET_REFERENCE', owner, path, reason)

    def is_alias(value):
        return _pointer(value) in alias_ptrs

    def resolve_reads(root, path, owner, location, kind):
        counts['rna_paths'] += 1
        if root is None or not isinstance(path, str) or not path:
            unresolved_path(owner, location, 'Missing driver RNA root/path; no concrete target reference established', False)
            return
        direct = False
        resolved_alias = is_alias(root) or _pointer(root) == _pointer(rig.data)
        full_ok = False
        for prefix in _prefixes(path):
            try:
                value = root.path_resolve(prefix, False)
                if prefix == path:
                    full_ok = True
                if is_alias(value) or _pointer(value) == _pointer(rig.data):
                    resolved_alias = True
                if _pointer(value) in target_ptrs:
                    add(kind, owner, location, {'rna_path': path, 'resolved_target_prefix': prefix,
                                               'resolved_type': value.bl_rna.identifier})
                    direct = True
            except (ValueError, TypeError, AttributeError, KeyError, ReferenceError):
                pass
        if not full_ok and not direct:
            # A generally broken unrelated driver is not a universal veto.
            relevant = resolved_alias and bone_name in path
            unresolved_path(owner, location, 'RNA path unresolved: ' + path, relevant)

    group = body.vertex_groups.get(bone_name)
    positive = sum(any(m.group == group.index and m.weight > 0 for m in v.groups)
                   for v in body.data.vertices) if group else 0
    role_ids = sorted(r['role_id'] for r in profile['roles'] if r.get('bone') == bone_name)
    required_by = sorted(r['role_id'] for r in profile['roles'] if any(g.get('bone') == bone_name for g in r.get('requires', [])))
    qualifiers = {'use_deform': bool(bone.use_deform), 'children_count': len(bone.children),
        'use_connect': bool(bone.use_connect), 'bbone_segments': bone.bbone_segments,
        'positive_body_weight_vertices': positive, 'profile_role_ids': role_ids, 'profile_required_by_roles': required_by,
        'own_constraints_count': len(pose_bone.constraints), 'pose_writers': [],
        'rig_object_aliases': sorted(obj.name for obj in aliases)}
    own = _identity(rig)
    tests = [('NOT_DEFORM', bone.use_deform), ('NOT_LEAF', not bone.children),
             ('CONNECTED_BONE', not bone.use_connect), ('MULTISEGMENT_BONE', bone.bbone_segments == 1),
             ('NO_POSITIVE_BODY_WEIGHT', positive > 0), ('PROFILE_ROLE_TARGET', not role_ids and not required_by),
             ('OWN_CONSTRAINTS', not pose_bone.constraints)]
    for kind, okay in tests:
        if not okay:
            add(kind, own, pose_bone.path_from_id(), deepcopy(qualifiers))

    def action_curves(action, slot, owner, location):
        if action is None:
            return []
        if slot is None and len(action.slots) > 1:
            unresolved_path(owner, location, 'Ambiguous active Action slot on target rig/data', True)
            return []
        handle = slot.handle if slot is not None else action.slots[0].handle if len(action.slots) == 1 else None
        result = []
        for layer in action.layers:
            for strip in layer.strips:
                if not hasattr(strip, 'channelbags'):
                    unresolved_path(owner, location, 'Unsupported Action strip on target rig/data', True)
                    continue
                for bag in strip.channelbags:
                    if handle is None or bag.slot_handle == handle:
                        result.extend(bag.fcurves)
        return result

    def check_writers(items, owner, location, root):
        for fc in items:
            previous = len(blockers)
            resolve_reads(root, fc.data_path, owner, location + '/' + fc.data_path, 'POSE_OR_BONE_WRITER')
            if len(blockers) > previous:
                qualifiers['pose_writers'].append({'owner': owner, 'kind': location,
                    'path': fc.data_path, 'index': fc.array_index})

    for owner_obj in aliases + [rig.data]:
        ad = owner_obj.animation_data
        if ad is None:
            continue
        owner_id = _identity(owner_obj)
        check_writers(ad.drivers, owner_id, 'pose_driver', owner_obj)
        check_writers(action_curves(ad.action, ad.action_slot, owner_id, 'active_action'), owner_id, 'active_action', owner_obj)

        def nla(strips):
            for strip in strips:
                check_writers(action_curves(strip.action, strip.action_slot, owner_id, 'NLA/' + strip.name)
                              if strip.action else [], owner_id, 'NLA/' + strip.name, owner_obj)
                if getattr(strip, 'strips', None):
                    nla(strip.strips)
        for track in ad.nla_tracks:
            nla(track.strips)
    for obj in aliases:
        for pb in obj.pose.bones:
            for con in pb.constraints:
                if con.type == 'ACTION' and con.action is not None:
                    owner_id = _identity(obj)
                    location = 'ACTION_constraint/' + pb.name + '/' + con.name
                    check_writers(action_curves(con.action, getattr(con, 'action_slot', None), owner_id, location),
                                  owner_id, location, obj)

    for armature in bpy.data.armatures:
        counts['armatures'] += 1
        for candidate in armature.bones:
            path = candidate.path_from_id()
            if _pointer(candidate.parent) in target_ptrs:
                add('BONE_PARENT_READER', _identity(armature), path + '.parent', bone_name)
            for field in ('bbone_custom_handle_start', 'bbone_custom_handle_end'):
                if _pointer(getattr(candidate, field, None)) in target_ptrs:
                    add('BBONE_HANDLE_READER', _identity(armature), path + '.' + field, bone_name)

    for obj in bpy.data.objects:
        counts['objects'] += 1
        owner = _identity(obj)
        if is_alias(obj.parent) and obj.parent_type == 'BONE' and obj.parent_bone == bone_name:
            add('OBJECT_BONE_PARENT_READER', owner, 'parent_bone', bone_name)
        owners = [(obj, obj.constraints, 'constraints')]
        if obj.pose:
            for pb in obj.pose.bones:
                counts['pose_bones'] += 1
                if _pointer(pb.custom_shape_transform) in target_ptrs:
                    add('CUSTOM_SHAPE_TRANSFORM_READER', owner, pb.path_from_id() + '.custom_shape_transform', bone_name)
                owners.append((pb, pb.constraints, pb.path_from_id() + '.constraints'))
        for _, constraints, base in owners:
            for con in constraints:
                counts['constraints'] += 1
                location = base + '[' + json.dumps(con.name, ensure_ascii=False) + ']'
                for target_field, bone_field in (('target', 'subtarget'), ('pole_target', 'pole_subtarget'),
                                                ('space_object', 'space_subtarget')):
                    target = getattr(con, target_field, None)
                    subtarget = getattr(con, bone_field, '')
                    if is_alias(target) and subtarget == bone_name:
                        add('CONSTRAINT_BONE_READER', owner, location + '.' + bone_field,
                            {'constraint_type': con.type, 'target_field': target_field,
                             'target': target.name, 'bone': subtarget, 'mute': bool(con.mute), 'influence': float(con.influence)})
                if con.type == 'ARMATURE':
                    for index, target in enumerate(con.targets):
                        if is_alias(target.target) and target.subtarget == bone_name:
                            add('ARMATURE_CONSTRAINT_TARGET_READER', owner, location + f'.targets[{index}]',
                                {'target': target.target.name, 'bone': target.subtarget, 'weight': float(target.weight)})
        for modifier in obj.modifiers:
            if is_alias(getattr(modifier, 'object', None)) and getattr(modifier, 'subtarget', '') == bone_name:
                add('MODIFIER_BONE_READER', owner, 'modifiers/' + modifier.name + '.subtarget',
                    {'type': modifier.type, 'bone': bone_name, 'target': modifier.object.name})
            if modifier.type == 'ARMATURE' and is_alias(modifier.object):
                vg = obj.vertex_groups.get(bone_name)
                amount = sum(any(m.group == vg.index and m.weight > 0 for m in v.groups)
                             for v in obj.data.vertices) if obj.type == 'MESH' and vg else 0
                if modifier.use_bone_envelopes:
                    add('UNDECLARED_ENVELOPE_CONSUMER', owner, 'modifiers/' + modifier.name,
                        'Native envelopes may read target rest outside the declared named-group skin domain')
                if obj.type == 'MESH' and amount > 0 and modifier.use_vertex_groups:
                    skins.append({'object': obj.name, 'mesh': obj.data.name, 'modifier': modifier.name,
                        'rig': modifier.object.name, 'weight_group': bone_name, 'positive_vertices': amount,
                        'use_vertex_groups': bool(modifier.use_vertex_groups),
                        'use_bone_envelopes': bool(modifier.use_bone_envelopes),
                        'viewport': bool(modifier.show_viewport), 'render': bool(modifier.show_render)})

    for owner in _main_ids():
        counts['main_ids'] += 1
        ad = getattr(owner, 'animation_data', None)
        if ad is None:
            continue
        owner_id = _identity(owner)
        for fc in ad.drivers:
            counts['driver_curves'] += 1
            location = 'drivers/' + fc.data_path + f'[{fc.array_index}]'
            # Detect data Bone writers as well as ordinary pose FCurve writers.
            resolve_reads(owner, fc.data_path, owner_id, location + '/destination', 'POSE_OR_BONE_WRITER')
            for variable in fc.driver.variables:
                for index, target in enumerate(variable.targets):
                    counts['driver_targets'] += 1
                    field = location + '/' + variable.name + f'/targets[{index}]'
                    root = target.id
                    if is_alias(root) and target.bone_target == bone_name:
                        add('DRIVER_BONE_TARGET_READER', owner_id, field,
                            {'variable_type': variable.type, 'rig': root.name, 'bone': bone_name})
                    if variable.type == 'SINGLE_PROP':
                        resolve_reads(root, target.data_path, owner_id, field, 'DRIVER_RNA_ALIAS_READER')
                    elif variable.type == 'CONTEXT_PROP':
                        context_roots = {'ACTIVE_SCENE': bpy.context.scene, 'ACTIVE_VIEW_LAYER': bpy.context.view_layer}
                        context_root = context_roots.get(target.context_property)
                        if context_root is None:
                            unresolved_path(owner_id, field, 'Unsupported context root: ' + target.context_property,
                                            bone_name in target.data_path)
                        else:
                            resolve_reads(context_root, target.data_path, owner_id, field, 'DRIVER_CONTEXT_ALIAS_READER')
                    elif variable.type not in ('TRANSFORMS', 'LOC_DIFF', 'ROTATION_DIFF'):
                        unresolved_path(owner_id, field, 'Unsupported driver variable type: ' + variable.type,
                                        is_alias(root) and (target.bone_target == bone_name or bone_name in target.data_path))

    report = {'admissible': not blockers, 'bone': bone_name, 'qualifiers': qualifiers,
        'blockers': blockers, 'skin_consumers': skins,
        'coverage': {'scanned_domains': [
            'target rig/data active Action, NLA, pose/data drivers and ACTION constraints',
            'all Main Armature Bone parents and custom B-Bone handles',
            'all Main Object bone parenting and PoseBone custom_shape_transform',
            'all Object/PoseBone constraint target, pole, space and Armature targets',
            'all Main ID drivers including embedded node trees; bone targets and resolved RNA-prefix aliases',
            'native modifier object/subtarget bone references; Armature envelopes separately blocked',
            'Mesh Armature enabled positive named-group skin consumers'],
            'counts': counts, 'unresolved': unresolved,
            'not_certified': ['Arbitrary Python/scripts or dynamically constructed node/attribute strings',
                'Unenumerated third-party custom RNA collections or future constraint/driver schemas',
                'This is a finite current-Main native scan, not a universal no-consumer guarantee'],
            'admissible_scope': 'Only declared scanned domains; skin consumers are explicitly reported and allowed'}}
    return json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
