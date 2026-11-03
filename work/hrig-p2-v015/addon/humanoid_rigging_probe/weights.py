"""Exact raw deform-weight patches, with declared protection and rollback.

No geometry/shape-key/UV edit, group creation, automatic normalization, copying
of shared Mesh data, operator, keying, or pose change is performed here. This is
raw storage authoring; normalization is not a human deformation-quality claim.
"""
import hashlib
import math
import re
import struct

import bpy


SCHEMA = 'hrig.weights.patch.v1'
PRESERVE_SCHEMA = 'hrig.weights.patch.v2'
PRESERVE_POLICY = 'preserve_source_deform_sum'
INSPECTION_SCHEMA = 'hrig.weights.inspect.v1'
MAX_VERTICES = 500000
MAX_BONES = 4096
MAX_GROUPS = 8192
MAX_ROWS = 65536
CONSUMER_LIMITS = {'objects': 10000, 'constraints': 100000, 'pose_bones': 100000,
                   'trees': 10000, 'nodes': 100000, 'items': 65536,
                   'modifiers': 10000, 'material_slots': 10000,
                   'path_chars': 4096, 'report_chars': 4 * 1024 * 1024}
NORMALIZATION_TOLERANCE = 1e-6
SCOPE = ('Local editable single-user raw Mesh weights only; existing shape keys '
         'are allowed and never edited. No automatic normalization or group '
         'definition changes. Geometry/UV/attributes/shape-key metadata and '
         'animation preservation require the independent artifact oracle. '
         'Existing invalid untouched weights are preserved, not repaired. '
         'Selected source rows must have only finite [0,1] raw weights. '
         'Named native consumer checks are partial: arbitrary scripts, custom '
         'plugins and dynamically constructed attribute names are not proven. '
         'Normalized raw weights do not certify human deformation quality.')


class Refused(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _require(condition, code, message):
    if not condition:
        raise Refused(code, message)


def _error(error):
    answer = {'code': getattr(error, 'code', 'NATIVE_ERROR'),
              'type': type(error).__name__, 'message': str(error)}
    if hasattr(error, 'details'):
        answer['details'] = error.details
    return answer


def _integer(value, label, low, high):
    _require(type(value) is int and low <= value <= high, 'INVALID_REQUEST',
             label + ' must be an integer in ' + str((low, high)))
    return value


def _weight(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    except OverflowError:
        valid = False
    _require(valid, 'INVALID_WEIGHT', label + ' must be a finite number in [0,1]')
    return float(value)


def _f32(value):
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


def _u32(hash_value, value):
    hash_value.update(struct.pack('<I', int(value)))


def _u64(hash_value, value):
    hash_value.update(struct.pack('<Q', int(value)))


def _text(hash_value, value):
    encoded = value.encode('utf-8')
    _u32(hash_value, len(encoded))
    hash_value.update(encoded)


def _float(hash_value, value):
    _require(math.isfinite(value), 'NONFINITE_GEOMETRY', 'Finite raw coordinates required')
    hash_value.update(struct.pack('<f', float(value)))


def _topology(mesh):
    """Stable raw coordinates + edge/polygon/loop topology, float32 LE."""
    value = hashlib.sha256(b'hrig.weights.topology.f32le.v1\0')
    for collection in (mesh.vertices, mesh.edges, mesh.polygons, mesh.loops):
        _u64(value, len(collection))
    for index, vertex in enumerate(mesh.vertices):
        _require(vertex.index == index, 'TOPOLOGY_INDEX', 'Noncontiguous vertex indices')
        _u32(value, index)
        for coordinate in vertex.co:
            _float(value, coordinate)
    for index, edge in enumerate(mesh.edges):
        _u32(value, index)
        for vertex_index in edge.vertices:
            _u32(value, vertex_index)
    for index, polygon in enumerate(mesh.polygons):
        _u32(value, index)
        _u32(value, polygon.loop_start)
        _u32(value, polygon.loop_total)
        _u32(value, len(polygon.vertices))
        for vertex_index in polygon.vertices:
            _u32(value, vertex_index)
    for index, loop in enumerate(mesh.loops):
        _u32(value, index)
        _u32(value, loop.vertex_index)
        _u32(value, loop.edge_index)
    return value.hexdigest()


def _groups(obj):
    groups = list(obj.vertex_groups)
    _require(len(groups) <= MAX_GROUPS, 'GROUP_BUDGET', 'At most 8192 groups supported')
    value = hashlib.sha256(b'hrig.weights.groups.v1\0')
    _u32(value, len(groups))
    names = set()
    for order, group in enumerate(groups):
        _require(group.index == order and group.name not in names,
                 'GROUP_DEFINITION', 'Unique ordered contiguous group definitions required')
        names.add(group.name)
        _u32(value, order)
        _u32(value, group.index)
        _text(value, group.name)
        value.update(b'\1' if group.lock_weight else b'\0')
    return groups, value.hexdigest()


def _rig_deform(rig):
    value = hashlib.sha256(b'hrig.weights.rig-deform.v1\0')
    _u32(value, len(rig.data.bones))
    deform = set()
    for index, bone in enumerate(rig.data.bones):
        _u32(value, index)
        _text(value, bone.name)
        value.update(b'\1' if bone.use_deform else b'\0')
        if bone.use_deform:
            deform.add(bone.name)
    return deform, value.hexdigest()


def _raw_row(mesh, index, groups):
    answer = {}
    for membership in mesh.vertices[index].groups:
        group_index = membership.group
        _require(0 <= group_index < len(groups), 'UNKNOWN_NATIVE_GROUP',
                 'Raw membership references an undefined group index')
        name = groups[group_index].name
        _require(name not in answer, 'DUPLICATE_NATIVE_GROUP', 'Duplicate raw group membership')
        # Observation and hashing must preserve existing invalid raw storage.
        # Authoring validation belongs only to selected source/target rows.
        answer[name] = float(membership.weight)
    return answer


def raw_weight_row(obj, index):
    """Read raw memberships, including zero/invalid values; not a JSON encoder."""
    _require(isinstance(obj, bpy.types.Object) and obj.type == 'MESH',
             'INVALID_MESH', 'Explicit Mesh Object required')
    _integer(index, 'index', 0, len(obj.data.vertices) - 1)
    groups, _ = _groups(obj)
    return _raw_row(obj.data, index, groups)


def _row_hash(value, index, row, group_indices):
    _u32(value, index)
    ordered = sorted(row.items(), key=lambda item: group_indices[item[0]])
    _u32(value, len(ordered))
    for name, weight in ordered:
        _u32(value, group_indices[name])
        value.update(struct.pack('<f', weight))


def _invalid_raw(value):
    return not math.isfinite(value) or not 0 <= value <= 1


def _diagnostic_value(value):
    if math.isnan(value):
        return {'non_finite': 'nan'}
    if math.isinf(value):
        return {'non_finite': 'inf' if value > 0 else '-inf'}
    return value


def _constraint_targets(constraint):
    """Native Object-pointer roles; Mesh subtargets name vertex groups."""
    if constraint.type == 'ARMATURE':
        for index, slot in enumerate(constraint.targets):
            yield 'targets[' + str(index) + ']', slot.target, slot.subtarget
    for prop in constraint.bl_rna.properties:
        if prop.type != 'POINTER' or prop.fixed_type.identifier != 'Object':
            continue
        role = prop.identifier
        sub_name = {'target': 'subtarget', 'pole_target': 'pole_subtarget',
                    'space_object': 'space_subtarget'}.get(role)
        if role == 'space_object' and not (
                getattr(constraint, 'owner_space', None) == 'CUSTOM'
                or getattr(constraint, 'target_space', None) == 'CUSTOM'):
            continue
        yield role, getattr(constraint, role), getattr(constraint, sub_name, '') if sub_name else ''


def _group_consumers(obj, names):
    """First-version explicit native group-name readers, never semantic proof.

    Scan modifiers on the weight-owning object, native constraints in all
    Objects/pose bones, and its material/Geometry Nodes trees. No arbitrary
    Python expression or driver expression is evaluated.
    """
    items, unresolved, seen_items = [], [], set()
    counts = {key: 0 for key in CONSUMER_LIMITS if key != 'path_chars'}

    def budget(kind, amount=1):
        counts[kind] += amount
        if counts[kind] > CONSUMER_LIMITS[kind]:
            error = Refused('CONSUMER_BUDGET', 'Named consumer scan exceeded ' + kind + ' budget')
            error.details = {'budget': kind, 'limit': CONSUMER_LIMITS[kind], 'counts': dict(counts)}
            raise error

    def path_guard(path):
        if len(path) > CONSUMER_LIMITS['path_chars']:
            error = Refused('CONSUMER_BUDGET', 'Named consumer path exceeded bounded diagnostic length')
            error.details = {'budget': 'path_chars', 'limit': CONSUMER_LIMITS['path_chars'],
                             'observed': len(path), 'counts': dict(counts)}
            raise error

    budget('objects', len(bpy.data.objects))
    budget('modifiers', len(obj.modifiers))
    budget('material_slots', len(obj.data.materials))

    def add(group, kind, owner, path, **extra):
        # Bound examined native name fields as well as matched output records;
        # unmatched/empty fields do not make a huge scan artificially free.
        budget('items')
        path_guard(path)
        if group not in names:
            return
        key = (group, kind, owner, path)
        if key not in seen_items:
            seen_items.add(key)
            budget('report_chars', len(group) + len(kind) + len(owner) + len(path) + 256)
            items.append({'group': group, 'kind': kind, 'owner': owner, 'path': path, **extra})

    keys = obj.data.shape_keys
    if keys:
        for key in keys.key_blocks:
            add(key.vertex_group, 'shape_key_mask', obj.name,
                'shape_keys.' + key.name + '.vertex_group', mute=key.mute)

    def fields(owner, path, modifier, depth=0):
        if owner is None:
            return
        for prop in owner.bl_rna.properties:
            name = prop.identifier
            if prop.type == 'STRING' and name.startswith('vertex_group'):
                group = getattr(owner, name)
                kind = ('armature_modifier_mask' if owner == modifier and modifier.type == 'ARMATURE'
                        and name == 'vertex_group' else 'modifier_vertex_group')
                add(group, kind, obj.name, path + '.' + name,
                    modifier_type=modifier.type, show_viewport=modifier.show_viewport,
                    show_render=modifier.show_render)
            elif (depth < 2 and prop.type == 'POINTER'
                  and name in ('settings', 'collision_settings')):
                fields(getattr(owner, name), path + '.' + name, modifier, depth + 1)

    trees = []
    for modifier in obj.modifiers:
        fields(modifier, 'modifiers.' + modifier.name, modifier)
        tree = getattr(modifier, 'node_group', None)
        if tree is not None:
            trees.append((tree, obj.name + '.modifiers.' + modifier.name))
    for material in obj.data.materials:
        if material and material.node_tree:
            trees.append((material.node_tree, 'material.' + material.name))

    def constraint_owners(owner):
        yield owner.name, owner.constraints
        if owner.type == 'ARMATURE' and owner.pose:
            for bone in owner.pose.bones:
                budget('pose_bones')
                yield owner.name + '.pose.' + bone.name, bone.constraints

    for owner in bpy.data.objects:
        for owner_name, constraints in constraint_owners(owner):
            for constraint in constraints:
                budget('constraints')
                for role, target, subtarget in _constraint_targets(constraint):
                    if target == obj:
                        add(subtarget, 'constraint_vertex_group', owner_name,
                            'constraints.' + constraint.name + '.' + role,
                            constraint_type=constraint.type, mute=constraint.mute)

    # Runtime ID sets break nested node-group cycles; no pointers are persisted.
    seen_trees = set()
    for root_tree, origin in trees:
        stack = [(root_tree, origin)]
        while stack:
            tree, tree_path = stack.pop()
            if tree in seen_trees:
                continue
            seen_trees.add(tree)
            budget('trees')
            for node in tree.nodes:
                budget('nodes')
                node_path = tree_path + '.nodes.' + node.name
                path_guard(node_path)
                if node.bl_idname == 'ShaderNodeAttribute':
                    add(node.attribute_name, 'shader_attribute', obj.name, node_path + '.attribute_name',
                        attribute_type=getattr(node, 'attribute_type', 'UNKNOWN'))
                elif node.bl_idname == 'GeometryNodeInputNamedAttribute':
                    socket = next((item for item in node.inputs if item.identifier == 'Name'), None)
                    if socket is not None and not socket.is_linked and isinstance(socket.default_value, str):
                        add(socket.default_value, 'geometry_named_attribute', obj.name, node_path + '.inputs.Name')
                    else:
                        budget('items')
                        budget('report_chars', len(node_path) + 128)
                        unresolved.append({'kind': 'dynamic_named_attribute', 'path': node_path})
                nested = getattr(node, 'node_tree', None)
                if nested is not None:
                    stack.append((nested, node_path + '.node_tree'))
    return {'items': items, 'coverage': 'partial_named_native_fields_only',
            'unresolved': unresolved, 'counts': counts, 'limits': dict(CONSUMER_LIMITS),
            'limitations': ['arbitrary scripts/custom plugins and driver expressions are not semantic-analyzed',
                            'linked/dynamically constructed attribute names are not resolved',
                            'shared native node trees are scanned once, reporting their first associated origin',
                            'node inspection covers only material/Geometry Nodes trees associated with this object']}


def _weights_hash(mesh, groups, skip=frozenset(), protected_names=None,
                  selected=None):
    value = hashlib.sha256(b'hrig.weights.raw-rows.f32le.v1\0')
    indices = {group.name: group.index for group in groups}
    _u64(value, len(mesh.vertices))
    # Original vertex indices and row cardinality distinguish absent and zero.
    for index in range(len(mesh.vertices)):
        if index in skip or selected is not None and index not in selected:
            continue
        row = _raw_row(mesh, index, groups)
        if protected_names is not None:
            row = {name: weight for name, weight in row.items() if name in protected_names}
        _row_hash(value, index, row, indices)
    return value.hexdigest()


def _local_editable(value):
    return (value is not None and not value.library
            and getattr(value, 'override_library', None) is None and value.is_editable)


def _mesh_users(mesh):
    real = sorted(owner.name for owner in bpy.data.objects
                  if owner.type == 'MESH' and owner.data == mesh)
    fake = bool(mesh.use_fake_user)
    return {'object_users': len(real), 'object_names': real,
            'native_users': mesh.users, 'fake_user': fake,
            'adjusted_users': mesh.users - int(fake)}


def _domain(context, obj, rig):
    _require(context.mode == 'OBJECT', 'OBJECT_MODE_REQUIRED', 'Only Object mode supported')
    _require(isinstance(obj, bpy.types.Object) and obj.type == 'MESH',
             'INVALID_MESH', 'Explicit Mesh Object required')
    _require(isinstance(rig, bpy.types.Object) and rig.type == 'ARMATURE',
             'INVALID_RIG', 'Explicit Armature Object required')
    for value in (obj, obj.data, rig, rig.data):
        _require(_local_editable(value), 'READ_ONLY_DATA',
                 'Local editable non-override Mesh and rig IDs required')
    _require(obj.mode == 'OBJECT' and rig.mode == 'OBJECT' and not obj.data.is_editmode,
             'OBJECT_MODE_REQUIRED', 'Mesh and rig must both be outside Edit/Pose mode')
    users = _mesh_users(obj.data)
    _require(users['object_users'] == 1 and users['adjusted_users'] == 1,
             'SHARED_MESH', 'reject_shared: exactly one real Object user and no unknown '
             'extra native users required; a retained fake user is allowed; no implicit copying')
    _require(len(obj.data.vertices) <= MAX_VERTICES, 'VERTEX_BUDGET',
             'At most 500000 vertices supported')
    _require(len(rig.data.bones) <= MAX_BONES, 'BONE_BUDGET',
             'At most 4096 rig bones supported')
    groups, group_sha = _groups(obj)
    modifiers = [modifier for modifier in obj.modifiers if modifier.type == 'ARMATURE']
    _require(len(modifiers) == 1 and modifiers[0].object == rig,
             'ARMATURE_BINDING', 'Exactly one Armature modifier targeting the explicit rig required')
    modifier = modifiers[0]
    _require(modifier.show_viewport and modifier.show_render and modifier.use_vertex_groups,
             'ARMATURE_DISABLED', 'Enabled viewport/render vertex-group Armature modifier required')
    deform, rig_sha = _rig_deform(rig)
    return groups, group_sha, deform, rig_sha


def _snapshot(obj, rig, groups, group_sha, rig_sha):
    return {'topology_sha256': _topology(obj.data), 'groups_sha256': group_sha,
            'weights_sha256': _weights_hash(obj.data, groups),
            'rig_deform_sha256': rig_sha}


def inspect_weights(context, obj, rig):
    """Read signatures and object-local raw/deform statistics; never modify IDs."""
    try:
        groups, group_sha, deform, rig_sha = _domain(context, obj, rig)
        counts = {'vertices': len(obj.data.vertices), 'group_definitions': len(groups),
                  'deform_group_definitions': sum(group.name in deform for group in groups),
                  'raw_memberships': 0, 'zero_memberships': 0, 'deform_unweighted': 0,
                  'deform_not_normalized': 0, 'max_deform_influences': 0,
                  'deform_sum_min': None, 'deform_sum_max': None,
                  'invalid_raw_weights': 0, 'invalid_deform_weights': 0,
                  'nonfinite_deform_vertices': 0, 'finite_deform_vertices': 0}
        invalid_examples = []
        for index in range(len(obj.data.vertices)):
            row = _raw_row(obj.data, index, groups)
            weights = [weight for name, weight in row.items() if name in deform]
            counts['raw_memberships'] += len(row)
            counts['zero_memberships'] += sum(weight == 0 for weight in row.values())
            for name, weight in row.items():
                if _invalid_raw(weight):
                    counts['invalid_raw_weights'] += 1
                    counts['invalid_deform_weights'] += name in deform
                    if len(invalid_examples) < 32:
                        invalid_examples.append({'vertex': index, 'group': name,
                                                 'value': _diagnostic_value(weight)})
            counts['max_deform_influences'] = max(counts['max_deform_influences'],
                                                  sum(weight > 0 for weight in weights))
            if any(not math.isfinite(weight) for weight in weights):
                counts['nonfinite_deform_vertices'] += 1
                continue
            counts['finite_deform_vertices'] += 1
            total = math.fsum(weights)
            counts['deform_unweighted'] += total <= 1e-8
            counts['deform_not_normalized'] += abs(total - 1.0) > NORMALIZATION_TOLERANCE
            counts['deform_sum_min'] = total if counts['deform_sum_min'] is None else min(counts['deform_sum_min'], total)
            counts['deform_sum_max'] = total if counts['deform_sum_max'] is None else max(counts['deform_sum_max'], total)
        consumers = _group_consumers(obj, {group.name for group in groups})
        return {'ok': True, 'operation': 'inspect_weights', 'schema': INSPECTION_SCHEMA,
                'object': obj.name, 'mesh': obj.data.name, 'rig': rig.name,
                'data_scope': 'reject_shared', 'mesh_user_accounting': _mesh_users(obj.data),
                'snapshot': _snapshot(obj, rig, groups, group_sha, rig_sha),
                'groups': [{'order': index, 'index': group.index, 'name': group.name,
                            'locked': group.lock_weight, 'deform': group.name in deform}
                           for index, group in enumerate(groups)],
                'statistics': counts, 'invalid_raw_examples': invalid_examples,
                'invalid_raw_examples_truncated': counts['invalid_raw_weights'] > len(invalid_examples),
                'deform_sum_aggregation': 'finite-deform rows only; nonfinite rows counted separately',
                'group_consumers': consumers['items'],
                'consumer_coverage': {key: value for key, value in consumers.items() if key != 'items'},
                'scope': SCOPE}
    except Exception as error:
        return {'ok': False, 'operation': 'inspect_weights', 'error': _error(error), 'scope': SCOPE}


def _request(value):
    fields = {'schema', 'expected_topology_sha256', 'expected_groups_sha256',
              'rows', 'max_influences'}
    if type(value) is dict and value.get('schema') == PRESERVE_SCHEMA:
        fields.add('sum_policy')
    _require(type(value) is dict and set(value) == fields,
             'INVALID_REQUEST', 'Exact patch request fields required')
    _require(value['schema'] in (SCHEMA, PRESERVE_SCHEMA), 'INVALID_SCHEMA',
             'Expected ' + SCHEMA + ' or ' + PRESERVE_SCHEMA)
    if value['schema'] == PRESERVE_SCHEMA:
        _require(value['sum_policy'] == PRESERVE_POLICY, 'INVALID_REQUEST',
                 'v2 requires explicit preserve_source_deform_sum')
    for key in ('expected_topology_sha256', 'expected_groups_sha256'):
        _require(type(value[key]) is str and re.fullmatch(r'[0-9a-f]{64}', value[key]) is not None,
                 'INVALID_REQUEST', key + ' must be a lowercase SHA-256')
    _integer(value['max_influences'], 'max_influences', 1, MAX_BONES)
    _require(type(value['rows']) is list and 1 <= len(value['rows']) <= MAX_ROWS,
             'ROW_BUDGET', 'One through 65536 explicit rows required')


def _plans(obj, groups, deform, request):
    by_name = {group.name: group for group in groups}
    locked_deform = {group.name for group in groups if group.lock_weight and group.name in deform}
    plans, seen = [], set()
    for row in request['rows']:
        row_fields = {'index', 'before', 'after'}
        preserving = request['schema'] == PRESERVE_SCHEMA
        if preserving:
            row_fields.add('expected_deform_sum')
        _require(type(row) is dict and set(row) == row_fields,
                 'INVALID_REQUEST', 'Exact schema-specific row fields required')
        index = _integer(row['index'], 'row.index', 0, len(obj.data.vertices) - 1)
        _require(index not in seen, 'DUPLICATE_ROW', 'Duplicate vertex row: ' + str(index))
        seen.add(index)
        current = _raw_row(obj.data, index, groups)
        _require(not any(_invalid_raw(weight) for weight in current.values()),
                 'INVALID_SOURCE_WEIGHT', 'Selected source row must contain only finite [0,1] '
                 'raw weights; existing invalid weights are not repaired: ' + str(index))
        for role in ('before', 'after'):
            _require(type(row[role]) is dict, 'INVALID_REQUEST', role + ' must be a group-name weight map')
            for name, weight in row[role].items():
                _require(type(name) is str and name in by_name,
                         'UNKNOWN_GROUP', 'Existing explicit vertex group required: ' + str(name))
                _weight(weight, role + '.' + name)
        _require(current == row['before'], 'STALE_WEIGHT_ROW',
                 'before must exactly match every raw membership, including zero: ' + str(index))
        desired_deform = {}
        for name, weight in row['after'].items():
            _require(name in deform, 'NONDEFORM_GROUP',
                     'after may contain only groups named by explicit rig deform bones: ' + name)
            weight = float(weight)
            if weight == 0:
                _require((preserving or name in locked_deform)
                         and name in current and current[name] == 0,
                         'ZERO_WEIGHT', 'Zero after values may only retain existing zero '
                         'memberships (v1 additionally requires locked deform)')
            stored = _f32(weight)
            _require(weight == 0 or stored > 0, 'WEIGHT_UNDERFLOW',
                     'Positive weight must survive native float32 storage')
            desired_deform[name] = stored
        for name in locked_deform:
            _require((name in current) == (name in desired_deform)
                     and (name not in current or
                          current[name] == float(row['after'][name]) == desired_deform[name]),
                     'LOCKED_GROUP', 'Locked membership/value must remain exact: ' + name)
        if preserving:
            actual_sum = math.fsum(weight for name, weight in current.items() if name in deform)
            expected_sum = row['expected_deform_sum']
            _require(type(expected_sum) in (int, float) and math.isfinite(expected_sum)
                     and expected_sum > 0 and expected_sum == actual_sum,
                     'DEFORM_SUM_CONFLICT', 'Explicit expected deform sum must exactly match '
                     'the positive native source total: ' + str(index))
            _require(math.fsum(row['after'].values()) > 0
                     and abs(math.fsum(row['after'].values()) - actual_sum) <= NORMALIZATION_TOLERANCE,
                     'DEFORM_SUM_CHANGED', 'Requested deform total must preserve source within 1e-6')
            _require(math.fsum(desired_deform.values()) > 0
                     and abs(math.fsum(desired_deform.values()) - actual_sum) <= NORMALIZATION_TOLERANCE,
                     'DEFORM_SUM_CHANGED', 'Float32 deform total must preserve source within 1e-6')
        else:
            _require(abs(math.fsum(row['after'].values()) - 1.0) <= NORMALIZATION_TOLERANCE,
                     'NOT_NORMALIZED', 'Requested deform sum must equal one within 1e-6')
            _require(abs(math.fsum(desired_deform.values()) - 1.0) <= NORMALIZATION_TOLERANCE,
                     'NOT_NORMALIZED', 'Float32 stored deform sum must remain within 1e-6')
        _require(sum(weight > 0 for weight in desired_deform.values()) <= request['max_influences'],
                 'INFLUENCE_BUDGET', 'Positive deform influences exceed the explicit budget')
        desired = {name: weight for name, weight in current.items() if name not in deform}
        desired.update(desired_deform)
        plans.append({'index': index, 'before': current, 'after': desired})
    return plans


def _set_row(obj, groups, index, desired):
    """Minimal native add/remove operations, with exact membership semantics."""
    current = _raw_row(obj.data, index, groups)
    by_name = {group.name: group for group in groups}
    for name in sorted(current, key=lambda key: by_name[key].index):
        if name not in desired:
            by_name[name].remove([index])
    for name in sorted(desired, key=lambda key: by_name[key].index):
        if name not in current or current[name] != desired[name]:
            by_name[name].add([index], desired[name], 'REPLACE')
    _require(_raw_row(obj.data, index, groups) == desired,
             'WEIGHT_READBACK', 'Native complete membership readback differs: ' + str(index))


def _protection(obj, rig, groups, selected, protected_names):
    current_groups, group_sha = _groups(obj)
    _, rig_sha = _rig_deform(rig)
    return {'topology_sha256': _topology(obj.data), 'groups_sha256': group_sha,
            'rig_deform_sha256': rig_sha, 'mesh_user_accounting': _mesh_users(obj.data),
            'unselected_rows_sha256': _weights_hash(obj.data, current_groups, skip=selected),
            'selected_protected_rows_sha256': _weights_hash(
                obj.data, current_groups, selected=selected, protected_names=protected_names)}


def _rollback(obj, rig, groups, plans, baseline_protection, selected, protected_names):
    errors = []
    for plan in plans:
        try:
            _set_row(obj, groups, plan['index'], plan['before'])
        except Exception as error:
            errors.append({'index': plan['index'], **_error(error)})
    try:
        obj.data.update()
        current_groups, _ = _groups(obj)
        _require(all(_raw_row(obj.data, p['index'], current_groups) == p['before'] for p in plans),
                 'ROLLBACK_READBACK', 'Original complete raw rows were not restored')
        restored_protection = _protection(obj, rig, current_groups, selected, protected_names)
        _require(restored_protection == baseline_protection, 'ROLLBACK_PROTECTION',
                 'Original topology/groups/unselected/protected memberships differ')
    except Exception as error:
        errors.append(_error(error))
    return {'attempted': True, 'ok': not errors, 'errors': errors}


def patch_weights(context, obj, rig, request):
    """Preflight all rows, author exact raw deform memberships, or restore all.

    after is the COMPLETE new deform set for each selected vertex. Nondeform
    memberships are retained. Existing zero membership is distinct from absence.
    Caller JSON parsing must also reject duplicate object keys before decoding.
    """
    groups = plans = selected = protected_names = baseline = None
    writing = False
    try:
        _request(request)
        groups, group_sha, deform, rig_sha = _domain(context, obj, rig)
        _require(group_sha == request['expected_groups_sha256'], 'GROUPS_CONFLICT',
                 'Group order/index/name/lock baseline changed')
        topology_sha = _topology(obj.data)
        _require(topology_sha == request['expected_topology_sha256'], 'TOPOLOGY_CONFLICT',
                 'Raw coordinates or edge/polygon/loop topology baseline changed')
        plans = _plans(obj, groups, deform, request)  # ALL rows before ANY write.
        touched = {name for plan in plans for name in set(plan['before']) | set(plan['after'])
                   if name in deform and ((name in plan['before']) != (name in plan['after'])
                   or plan['before'].get(name) != plan['after'].get(name))}
        consumers = _group_consumers(obj, {group.name for group in groups})
        conflicts = [item for item in consumers['items'] if item['group'] in touched]
        if conflicts:
            error = Refused('WEIGHT_GROUP_CONSUMER',
                            'Modified deform groups also have declared named non-skin consumers')
            error.details = {'touched_deform_groups': sorted(touched), 'consumers': conflicts}
            raise error
        selected = frozenset(plan['index'] for plan in plans)
        protected_names = {group.name for group in groups if group.name not in deform or group.lock_weight}
        baseline = _protection(obj, rig, groups, selected, protected_names)
        snapshot_before = {'topology_sha256': topology_sha, 'groups_sha256': group_sha,
                           'weights_sha256': _weights_hash(obj.data, groups),
                           'rig_deform_sha256': rig_sha}
        writing = True
        for plan in plans:
            _set_row(obj, groups, plan['index'], plan['after'])
        obj.data.update()
        after_protection = _protection(obj, rig, groups, selected, protected_names)
        _require(after_protection == baseline, 'PROTECTION_CHANGED',
                 'Topology/groups/rig deform flags/unselected/selected protected state changed')
        for plan in plans:
            _require(_raw_row(obj.data, plan['index'], groups) == plan['after'],
                     'WEIGHT_READBACK', 'Final native row differs: ' + str(plan['index']))
        changed = [plan['index'] for plan in plans if plan['before'] != plan['after']]
        result = {
            'schema': 'hrig.weights.patch-result.v1', 'object': obj.name,
            'mesh': obj.data.name, 'rig': rig.name, 'data_scope': 'reject_shared',
            'changed_vertices': changed, 'changed_vertex_count': len(changed),
            'before_weight_rows': [{'index': p['index'], 'weights': p['before']} for p in plans],
            'after_weight_rows': [{'index': p['index'], 'weights': p['after']} for p in plans],
            'snapshot_before': snapshot_before,
            'snapshot_after': _snapshot(obj, rig, groups, group_sha, rig_sha),
            'protection_before': baseline, 'protection_after': after_protection,
            'max_influences': request['max_influences'], 'scope': SCOPE,
            'touched_deform_groups': sorted(touched), 'group_consumers': consumers['items'],
            'consumer_coverage': {key: value for key, value in consumers.items() if key != 'items'},
        }
        if request['schema'] == PRESERVE_SCHEMA:
            result['sum_contract'] = {
                'schema': PRESERVE_SCHEMA, 'policy': PRESERVE_POLICY,
                'absolute_tolerance': NORMALIZATION_TOLERANCE,
                'rows': [{'index': p['index'],
                          'before': math.fsum(w for n, w in p['before'].items() if n in deform),
                          'after': math.fsum(w for n, w in p['after'].items() if n in deform)}
                         for p in plans]}
        return {'ok': True, 'operation': 'patch_weights', 'result': result, 'error': None,
                'rollback': {'attempted': False, 'ok': True, 'errors': []}}
    except Exception as error:
        rollback = {'attempted': False, 'ok': True, 'errors': []}
        if writing:
            rollback = _rollback(obj, rig, groups, plans, baseline, selected, protected_names)
        return {'ok': False, 'operation': 'patch_weights', 'result': None,
                'error': _error(error), 'rollback': rollback, 'scope': SCOPE}
