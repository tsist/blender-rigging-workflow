"""Finite reversible edits of two existing Aon ID properties.

Identified native RNA readers/writers only. This is neither arbitrary-script
dependency certification nor a complete character/storage observer. The worker
must additionally compare its declared full-character authoring observation.
"""
import copy
import json
import re

import bpy
from . import existing_control_contract as contract
from . import motion_clips
from . import styles

CAPTURE_SCHEMA = 'hrig.existing-control-properties.snapshot.v1'
DEFINITION_SCHEMA = 'hrig.existing-control-properties.definition.v1'
LIMITS = {'ids': 10000, 'drivers': 100000, 'targets': 200000,
          'action_curves': 200000, 'action_resolutions': 5000000, 'nla_strips': 100000, 'nla_depth': 32,
          'path_chars': 4096, 'path_tokens': 128}
CACHE_AUTHOR = ('frame_start', 'frame_end', 'frame_step', 'use_disk_cache', 'use_external',
                'filepath', 'name', 'compression', 'use_library_path')
CACHE_RUNTIME = ('is_baked', 'is_baking', 'is_outdated', 'info')
SCOPE = ('AON_HAIR_SWITCHES_V1: two original int ID-property values and four driven original '
         'Cloth visibility booleans; bounded native RNA driver/Action/NLA conflicts and selected '
         'property UI definitions; no arbitrary Python/script dependency, full-character storage, '
         'simulation/cache currentness, continuous geometry, GUI or performance certification')


def _fail(code, message, details=None):
    raise contract.SettingsError(code, message, details)


def _error(exc):
    return {'code': getattr(exc, 'code', 'SETTINGS_NATIVE'), 'type': type(exc).__name__,
            'message': str(exc), 'details': getattr(exc, 'details', {})}


def _require(condition, code, message, details=None):
    if not condition:
        _fail(code, message, details)


def _id(item):
    result = styles.identity(item)
    if isinstance(item, bpy.types.Key):
        owner = getattr(item, 'user', None)
        if not isinstance(owner, bpy.types.ID):
            owners = [value for collection in (bpy.data.meshes, bpy.data.curves, bpy.data.lattices)
                      for value in collection if getattr(value, 'shape_keys', None) == item]
            owner = owners[0] if len(owners) == 1 else None
        _require(isinstance(owner, bpy.types.ID), 'SETTINGS_IDENTITY', 'Key owner identity unavailable')
        result['owner'] = styles.identity(owner)
    return result


def _plain(value):
    if isinstance(value, bpy.types.ID):
        return _id(value)
    return motion_clips._plain(value)


def _settings(item, exclude=()):
    # Include readonly scalar identity/validity in this finite native definition.
    # Collections and non-ID struct pointers are explicitly captured by callers.
    result = {}
    for prop in item.bl_rna.properties:
        name = prop.identifier
        if name in ('rna_type', *exclude) or prop.type == 'COLLECTION':
            continue
        if prop.type == 'POINTER':
            value = getattr(item, name)
            if value is not None and not isinstance(value, bpy.types.ID):
                continue
        result[name] = _plain(getattr(item, name))
    return result


class _PathError(ValueError):
    def __init__(self, message, tokens=()):
        super().__init__(message)
        self.tokens = list(tokens)


def _path_tokens(path):
    """Bounded RNA accessor lexer. No execution/eval and no textual name splitting."""
    if type(path) is not str or not path or len(path) > LIMITS['path_chars']:
        raise _PathError('Missing/oversized RNA path')
    tokens, i, expect_attribute = [], 0, True
    def invalid(message):
        raise _PathError(message, tokens)
    while i < len(path):
        if len(tokens) >= LIMITS['path_tokens']:
            invalid('RNA path token budget exceeded')
        char = path[i]
        if char == '[':
            i += 1
            if i >= len(path):
                invalid('Truncated bracket')
            if path[i] in ('"', "'"):
                quote, text = path[i], []
                i += 1
                while i < len(path) and path[i] != quote:
                    if path[i] == '\\':
                        i += 1
                        if i >= len(path) or path[i] not in ('\\', '"', "'"):
                            invalid('Unsupported RNA key escape')
                    text.append(path[i])
                    i += 1
                if i >= len(path):
                    invalid('Unclosed RNA key')
                i += 1
                token = ('key', ''.join(text))
            else:
                match = re.match(r'-?\d+', path[i:])
                if match is None:
                    invalid('Unsupported RNA bracket')
                token = ('index', int(match.group()))
                i += len(match.group())
            if i >= len(path) or path[i] != ']':
                invalid('Unclosed RNA bracket')
            tokens.append(token)
            i += 1
            expect_attribute = False
        elif char == '.' and not expect_attribute:
            i += 1
            expect_attribute = True
        elif expect_attribute:
            start = i
            while i < len(path) and path[i] not in '.[]':
                i += 1
            name = path[start:i]
            if not name.isidentifier():
                invalid('Unsupported RNA attribute')
            tokens.append(('attr', name))
            expect_attribute = False
        else:
            invalid('Unexpected RNA path text')
    if expect_attribute or not tokens:
        invalid('Incomplete RNA path')
    return tokens


def _path_text(tokens):
    result = ''
    for kind, value in tokens:
        if kind == 'attr':
            result += ('.' if result else '') + value
        elif kind == 'key':
            result += '[' + json.dumps(value, ensure_ascii=False) + ']'
        else:
            result += '[%d]' % value
    return result


def _pointer(item):
    method = getattr(item, 'as_pointer', None)
    return method() if method is not None else None


def _path_guard(path):
    _require(type(path) is str and len(path) <= LIMITS['path_chars'],
             'SETTINGS_SCAN_BUDGET', 'Native RNA path exceeds the declared scan budget')


def _all_ids(counts):
    result, seen, pending = [], set(), []
    for prop in bpy.data.bl_rna.properties:
        if prop.type == 'COLLECTION':
            pending.extend(item for item in getattr(bpy.data, prop.identifier)
                           if isinstance(item, bpy.types.ID))
    while pending:
        item = pending.pop()
        pointer = item.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        _bump(counts, 'ids')
        result.append(item)
        for name in ('node_tree', 'shape_keys', 'compositing_node_group'):
            child = getattr(item, name, None)
            if isinstance(child, bpy.types.ID):
                pending.append(child)
    return sorted(result, key=lambda item: contract.digest(_id(item)))


def _bump(counts, key, amount=1):
    counts[key] = counts.get(key, 0) + amount
    _require(counts[key] <= LIMITS[key], 'SETTINGS_SCAN_BUDGET',
             'Native scan exceeded ' + key + ' budget', {'counts': dict(counts), 'limits': LIMITS})


def _leaf_domain(rig, pb, effects):
    leaves = []
    for key in contract.KEYS:
        path = pb.path_from_id() + '[' + json.dumps(key, ensure_ascii=False) + ']'
        leaves.append({'root': rig, 'owner': pb, 'field': key, 'kind': 'property',
                       'path': path, 'tokens': _path_tokens(path)})
    for row, obj, modifier in effects:
        path = modifier.path_from_id() + '.' + row['field']
        leaves.append({'root': obj, 'owner': modifier, 'field': row['field'], 'kind': 'effect',
                       'path': path, 'tokens': _path_tokens(path)})
    for leaf in leaves:
        # Ephemeral native identities, never included in either JSON digest.
        leaf['anchors'] = [(_pointer(leaf['root']), leaf['tokens'])]
        for end in range(1, len(leaf['tokens'])):
            ancestor = leaf['root'].path_resolve(_path_text(leaf['tokens'][:end]))
            pointer = _pointer(ancestor)
            if pointer is not None:
                leaf['anchors'].append((pointer, leaf['tokens'][end:]))
    return leaves


def _relation(root, path, leaves):
    """Return exact native leaf, parent aggregate, related unknown, or unrelated."""
    if root is None or not path:
        return None
    malformed = False
    try:
        tokens = _path_tokens(path)
    except _PathError as error:
        tokens, malformed = error.tokens, True
    # A name match is not an identity match. Resolve each finite prefix and
    # compare its actual native anchor plus the remaining accessor sequence.
    # This also handles Scene/constraint/parent aliases and collections whose
    # wrappers do not expose as_pointer(). Divergent final keys are unrelated.
    related = []
    for end in range(len(tokens) + 1):
        try:
            prefix = root.path_resolve(_path_text(tokens[:end])) if end else root
        except (ValueError, AttributeError, KeyError, TypeError):
            break
        pointer, remaining = _pointer(prefix), tokens[end:]
        if pointer is None:
            continue
        for index, leaf in enumerate(leaves):
            if any(pointer == anchor and _prefix(remaining, suffix) for anchor, suffix in leaf['anchors']):
                related.append(index)
    if malformed:
        return ('unknown', related[0]) if related else None
    parent_path = _path_text(tokens[:-1])
    try:
        value = root.path_resolve(path)
        parent = root.path_resolve(parent_path) if parent_path else root
    except (ValueError, AttributeError, KeyError, TypeError):
        return ('unknown', related[0]) if related else None
    for index, leaf in enumerate(leaves):
        if (_pointer(parent) == _pointer(leaf['owner']) and tokens[-1][0] in ('attr', 'key')
                and tokens[-1][1] == leaf['field']):
            return ('leaf', index)
    return ('aggregate', related[0]) if related else None


def _prefix(a, b):
    return a[:len(b)] == b or b[:len(a)] == a


def _curve(curve):
    driver = curve.driver
    state = {'settings': _settings(curve, ('driver', 'group')),
             'group': _settings(curve.group) if curve.group else None,
             'keys': [_settings(point) for point in curve.keyframe_points],
             'samples': [_settings(point) for point in curve.sampled_points],
             'modifiers': [{'type': modifier.type, 'settings': _settings(modifier)} for modifier in curve.modifiers]}
    if driver is not None:
        state['driver'] = {'settings': _settings(driver), 'variables': [
            {'settings': _settings(variable), 'targets': [_settings(slot) for slot in variable.targets]}
            for variable in driver.variables]}
    return state


def _action_curves(action, counts):
    for layer_index, layer in enumerate(action.layers):
        for strip_index, strip in enumerate(layer.strips):
            for bag_index, bag in enumerate(getattr(strip, 'channelbags', ())):
                for curve_index, curve in enumerate(bag.fcurves):
                    _bump(counts, 'action_curves')
                    yield curve, {'layer': layer_index, 'strip': strip_index, 'bag': bag_index,
                                  'slot_handle': bag.slot_handle, 'curve': curve_index}
    if getattr(action, 'is_action_legacy', False):
        for curve_index, curve in enumerate(action.fcurves):
            _bump(counts, 'action_curves')
            yield curve, {'legacy': True, 'curve': curve_index}


def _scan(context, leaves, rig, effects):
    counts, allowed, flag_writers, action_bindings = {}, [], {}, []
    ids = _all_ids(counts)
    for owner in ids:
        ad = getattr(owner, 'animation_data', None)
        if ad is None:
            continue
        bindings = []
        if ad.action:
            bindings.append({'action': ad.action, 'where': 'active',
                             'slot': _plain(ad.action_slot) if ad.action_slot else None})
        def walk(strips, prefix, depth=0):
            _require(depth <= LIMITS['nla_depth'], 'SETTINGS_SCAN_BUDGET', 'Recursive NLA depth exceeded')
            for index, strip in enumerate(strips):
                _bump(counts, 'nla_strips')
                where = prefix + '/%d' % index
                if strip.action:
                    bindings.append({'action': strip.action, 'where': where,
                                     'slot': _plain(strip.action_slot) if strip.action_slot else None})
                walk(strip.strips, where, depth + 1)
        for index, track in enumerate(ad.nla_tracks):
            walk(track.strips, 'nla/%d' % index)
        for binding in bindings:
            action_bindings.append({'owner': _id(owner), 'action': _id(binding['action']),
                                    'where': binding['where'], 'slot': binding['slot']})
        for curve_index, curve in enumerate(ad.drivers):
            _bump(counts, 'drivers')
            _path_guard(curve.data_path)
            output = _relation(owner, curve.data_path, leaves)
            _require(output is None or output[0] == 'leaf', 'SETTINGS_UNKNOWN_ALIAS',
                     'Driver output intersects a selected aggregate/unknown alias', {'owner': _id(owner), 'path': curve.data_path})
            if output is not None:
                _require(leaves[output[1]]['kind'] == 'effect', 'SETTINGS_PROPERTY_WRITER',
                         'Selected control property has a native driver writer', {'owner': _id(owner), 'path': curve.data_path})
            reads = []
            for variable_index, variable in enumerate(curve.driver.variables):
                for slot_index, slot in enumerate(variable.targets):
                    _bump(counts, 'targets')
                    _path_guard(slot.data_path)
                    root = slot.id
                    if variable.type == 'CONTEXT_PROP':
                        root = {'ACTIVE_SCENE': context.scene, 'ACTIVE_VIEW_LAYER': context.view_layer}.get(slot.context_property)
                    _require(not (variable.type == 'SINGLE_PROP' and not slot.data_path and root is not None
                                  and any(_pointer(root) == _pointer(leaf['root']) for leaf in leaves)),
                             'SETTINGS_UNKNOWN_ALIAS', 'Empty selected-root SINGLE_PROP target cannot be classified')
                    read = _relation(root, slot.data_path, leaves)
                    if read is not None:
                        _require(read[0] == 'leaf' and leaves[read[1]]['kind'] == 'property',
                                 'SETTINGS_CONSUMER', 'Selected aggregate/effect/unknown reader is not declared',
                                 {'owner': _id(owner), 'path': slot.data_path, 'relation': read[0]})
                        reads.append((variable_index, slot_index, read[1]))
            if output is None and not reads:
                # Direct selected-name use_self expressions are visibly related but opaque.
                expression = curve.driver.expression
                _require(not (curve.driver.use_self and any(key in expression for key in contract.KEYS)),
                         'SETTINGS_OPAQUE_CONSUMER', 'Selected-name use_self expression cannot be classified')
                continue
            _require(output is not None and len(reads) == 1, 'SETTINGS_CONSUMER',
                     'Only the four declared property-to-flag native drivers are supported')
            driver = curve.driver
            _require(len(driver.variables) == 1 and len(driver.variables[0].targets) == 1,
                     'SETTINGS_CONSUMER', 'Exactly one native variable/target slot required')
            variable = driver.variables[0]
            _require(variable.type == 'SINGLE_PROP' and driver.type == 'SCRIPTED' and not driver.use_self
                     and variable.name.isidentifier() and driver.expression == variable.name + '>0',
                     'SETTINGS_DRIVER_DEFINITION', 'Expected exact single-property int>0 expression without use_self')
            # Blender's existing native driver map has two identity endpoints.
            # The SCRIPTED expression yields only bool0/1; both endpoints must
            # map exactly to themselves. Preserve all handles/interpolation and
            # full definitions; no curve edits or general curve-map support.
            identity_map = (len(curve.keyframe_points) == 2
                            and [list(point.co) for point in curve.keyframe_points] == [[0.0, 0.0], [1.0, 1.0]]
                            and curve.evaluate(0.0) == 0.0 and curve.evaluate(1.0) == 1.0)
            _require(not curve.mute and curve.is_valid and driver.is_valid and curve.array_index == 0
                     and not len(curve.modifiers) and identity_map and not len(curve.sampled_points),
                     'SETTINGS_DRIVER_DEFINITION', 'Flag driver must be valid/unmuted, scalar, with exact native0/1 identity endpoints and no modifiers/samples',
                     {'owner': _id(owner), 'path': curve.data_path, 'mute': curve.mute,
                      'fcurve_is_valid': curve.is_valid, 'driver_is_valid': driver.is_valid,
                      'array_index': curve.array_index, 'modifiers': len(curve.modifiers),
                      'keys': len(curve.keyframe_points), 'samples': len(curve.sampled_points)})
            leaf_index, property_index = output[1], reads[0][2]
            expected_key = effects[leaf_index - 2][0]['control_key']
            _require(leaves[property_index]['field'] == expected_key and variable.targets[0].id == rig,
                     'SETTINGS_DRIVER_DEFINITION', 'Declared flag is wired to the wrong existing property')
            _require(leaf_index not in flag_writers, 'SETTINGS_EFFECT_WRITER', 'Duplicate selected flag writer')
            row = {'owner': _id(owner), 'curve_index': curve_index, 'property_key': expected_key,
                   'effect_index': leaf_index - 2, 'definition': _curve(curve)}
            flag_writers[leaf_index] = row
            allowed.append(row)
    _require(set(flag_writers) == set(range(2, 6)) and len(allowed) == 4,
             'SETTINGS_EFFECT_WRITER', 'Exactly four unique declared native flag writers required')
    # All bound owner roots plus explicit roots for every unbound Action/slot.
    # Also try all current native ID roots: a curve can reach the rig via parent,
    # constraints or another native ID pointer even if it is currently unbound.
    roots_by_action = {}
    for owner in ids:
        ad = getattr(owner, 'animation_data', None)
        if ad is None:
            continue
        def bind(action):
            if action:
                roots_by_action.setdefault(action.as_pointer(), {})[owner.as_pointer()] = owner
        bind(ad.action)
        def bind_strips(strips):
            for strip in strips:
                bind(strip.action)
                bind_strips(strip.strips)
        for track in ad.nla_tracks:
            bind_strips(track.strips)
    for action in bpy.data.actions:
        roots = dict(roots_by_action.get(action.as_pointer(), {}))
        for leaf in leaves:
            roots[leaf['root'].as_pointer()] = leaf['root']
        for curve, location in _action_curves(action, counts):
            _path_guard(curve.data_path)
            for root in ids:
                _bump(counts, 'action_resolutions')
                if root.as_pointer() not in roots:
                    # A path failing against an unrelated hypothetical root is
                    # not an unknown selected alias. Existing bindings and the
                    # explicit selected roots still use strict classification.
                    try:
                        root.path_resolve(curve.data_path)
                    except (ValueError, AttributeError, KeyError, TypeError):
                        continue
                related = _relation(root, curve.data_path, leaves)
                _require(related is None, 'SETTINGS_ACTION_WRITER',
                         'Bound or unbound Action intersects selected native property/effect/aggregate/unknown alias',
                         {'action': _id(action), 'root': _id(root), 'path': curve.data_path, 'location': location})
    return {'readers_flag_writers': sorted(allowed, key=lambda row: row['effect_index']),
            'coverage': {'counts': counts, 'limits': dict(LIMITS), 'ids': [_id(item) for item in ids],
                         'action_bindings': action_bindings,
                         'scope': 'All Main and discovered embedded node-tree/Key IDs; every native driver target slot; all layered/channelbag/legacy Action curves and recursive NLA bindings. No arbitrary scripts or hidden expression lookups.'}}


def _native_refresh(rig, context):
    rig.update_tag(refresh={'OBJECT'})
    context.view_layer.update()
    context.evaluated_depsgraph_get().update()


def _refresh(rig, context):
    """Injectable refresh seam; rollback intentionally calls _native_refresh."""
    _native_refresh(rig, context)


def _write_property(pb, key, value):
    """Injectable writer seam; wrappers must perform the real write before faulting."""
    pb[key] = value


def _capture(context, selection):
    _require(tuple(bpy.app.version) == (5, 2, 1), 'SETTINGS_DOMAIN', 'Verified Blender 5.2.1 required')
    declared = selection['context']
    _require(context.scene.name == declared['scene'] and context.view_layer.name == declared['view_layer']
             and context.scene.frame_current == 0 and context.scene.frame_subframe == 0.0 and context.mode == 'OBJECT',
             'SETTINGS_CONTEXT', 'Explicit scene/view-layer Object mode at frame0/subframe0 required')
    rig = bpy.data.objects.get(selection['rig'])
    _require(rig is not None and rig.type == 'ARMATURE', 'SETTINGS_DOMAIN', 'Explicit Armature Object missing')
    for item in (rig, rig.data):
        _require(item.is_editable and not item.library and not item.override_library,
                 'SETTINGS_DOMAIN', 'Local editable non-override rig/data required')
    owners = [obj for obj in bpy.data.objects if obj.type == 'ARMATURE' and obj.data == rig.data]
    _require(owners == [rig] and rig.data.users == 1 + int(rig.data.use_fake_user),
             'SETTINGS_SHARED_ARMATURE', 'Exactly one real rig/data owner required')
    _require(rig.mode == 'OBJECT' and context.scene.objects.get(rig.name) == rig
             and context.view_layer.objects.get(rig.name) == rig, 'SETTINGS_DOMAIN', 'Rig outside declared mode/scene/view-layer')
    pb = rig.pose.bones.get(selection['control_bone'])
    _require(pb is not None, 'SETTINGS_PROPERTY', 'Existing control PoseBone missing')
    fields, property_definitions = [], []
    for key in contract.KEYS:
        _require(key in pb and type(pb[key]) is int and pb[key] in (0, 1),
                 'SETTINGS_PROPERTY_TYPE', 'Existing native int 0/1 required', {'key': key})
        relative = '[' + json.dumps(key, ensure_ascii=False) + ']'
        path = pb.path_from_id() + relative
        alias = rig.path_resolve(path)
        _require(type(alias) is int and alias == pb[key] and pb.id_data == rig,
                 'SETTINGS_PROPERTY_ALIAS', 'Native canonical property identity/type/value differ')
        ui = pb.id_properties_ui(key).as_dict()
        contract.digest(ui)
        property_definitions.append({'key': key, 'type': 'int', 'owner': _id(rig),
                                     'bone': pb.name, 'bone_path': pb.path_from_id(), 'path': path,
                                     'ui': copy.deepcopy(ui),
                                     'overridable_library': pb.is_property_overridable_library(relative)})
        fields.append({'key': key, 'type': 'int', 'value': pb[key]})
    handles, effect_values, effect_definitions, derived = [], [], [], []
    for row in selection['effects']:
        obj = bpy.data.objects.get(row['object'])
        _require(obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT', 'SETTINGS_EFFECT', 'Declared effect Mesh Object missing/in wrong mode')
        for item in (obj, obj.data):
            _require(item.is_editable and not item.library and not item.override_library,
                     'SETTINGS_EFFECT', 'Local editable non-override effect Object/data required')
        dependency = context.evaluated_depsgraph_get().id_eval_get(obj)
        direct_member = context.view_layer.objects.get(obj.name) == obj
        _require(context.scene.objects.get(obj.name) == obj and dependency.is_evaluated and dependency.original == obj,
                 'SETTINGS_EFFECT', 'Effect must belong to the explicit Scene and have its own native evaluated dependency in the explicit ViewLayer graph')
        _require(row['index'] < len(obj.modifiers), 'SETTINGS_EFFECT', 'Declared modifier index absent')
        modifier = obj.modifiers[row['index']]
        _require(modifier.name == row['modifier'] and modifier.type == row['type'] and modifier.id_data == obj,
                 'SETTINGS_EFFECT', 'Native modifier index/name/type/owner differ')
        cache = modifier.point_cache
        _require(cache is not None and not any(getattr(cache, name) for name in ('is_baked', 'is_baking', 'use_disk_cache', 'use_external')),
                 'SETTINGS_CACHE', 'Only internal unbaked/nonbaking Cloth authoring cache supported')
        value = getattr(modifier, row['field'])
        _require(type(value) is bool and value == (pb[row['control_key']] > 0),
                 'SETTINGS_EFFECT_VALUE', 'Original native driven flag differs from declared control>0', row)
        native_path = modifier.path_from_id() + '.' + row['field']
        resolved = obj.path_resolve(native_path)
        _require(type(resolved) is bool and resolved is value, 'SETTINGS_EFFECT_ALIAS', 'Native effect path differs')
        handles.append((row, obj, modifier))
        effect_values.append({**row, 'value': value})
        effect_definitions.append({'selection': row, 'object_id': _id(obj), 'mesh_id': _id(obj.data),
                                   'domain': {'scene_member': True, 'view_layer_direct_member': direct_member,
                                              'native_graph_evaluated_dependency': True},
                                   'native_path': native_path,
                                   'modifier_settings': _settings(modifier, ('show_viewport', 'show_render', 'execution_time')),
                                   'cloth_settings': _settings(modifier.settings),
                                   'collision_settings': _settings(modifier.collision_settings),
                                   'effector_weights': _settings(modifier.settings.effector_weights),
                                   'cache_authoring': {name: _plain(getattr(cache, name)) if hasattr(cache, name)
                                                       else {'native_RNA': 'not_exposed_on_verified_build'} for name in CACHE_AUTHOR}})
        derived.append({'object': obj.name, 'modifier': modifier.name, 'index': row['index'],
                        'field': row['field'], 'cache': {name: _plain(getattr(cache, name)) for name in CACHE_RUNTIME}})
    leaves = _leaf_domain(rig, pb, handles)
    audit = _scan(context, leaves, rig, handles)
    definition = {'schema': DEFINITION_SCHEMA, 'selection': selection,
                  'rig': _id(rig), 'armature': _id(rig.data), 'rig_mode': rig.mode,
                  'armature_real_owners': [_id(owner) for owner in owners],
                  'armature_native_users': rig.data.users, 'armature_fake_user': rig.data.use_fake_user,
                  'properties': property_definitions, 'effects': effect_definitions, 'native_audit': audit}
    snapshot = {'schema': CAPTURE_SCHEMA, 'definition': definition, 'fields': fields, 'effects': effect_values,
                'context': {'scene': _id(context.scene), 'view_layer': context.view_layer.name,
                            'frame': context.scene.frame_current, 'subframe': context.scene.frame_subframe,
                            'mode': context.mode, 'active_object': _id(context.view_layer.objects.active),
                            'selected_objects': sorted(obj.name_full for obj in context.selected_objects)}}
    result = {'ok': True, 'fields': fields, 'effects': effect_values, 'snapshot': snapshot, 'definition': definition,
              'snapshot_sha256': contract.digest(snapshot), 'definition_sha256': contract.digest(definition),
              'derived_cache': derived, 'scope': SCOPE}
    return result, rig, pb


def inspect_settings(context, selection):
    try:
        result, _, _ = _capture(context, contract.canonical_selection(selection))
        return result
    except Exception as exc:
        return {'ok': False, 'changed': False, 'error': _error(exc), 'scope': SCOPE}


def _restore(context, selection, rig, pb, written, original):
    failures = []
    for key, value in reversed(written):
        try:
            pb[key] = value
            _require(type(pb[key]) is int and pb[key] == value,
                     'SETTINGS_ROLLBACK_READBACK', 'Restored native property differs', {'key': key})
        except Exception as exc:
            failures.append({'key': key, **_error(exc)})
    try:
        _native_refresh(rig, context)
        restored, _, _ = _capture(context, selection)
        _require(restored['snapshot'] == original['snapshot'], 'SETTINGS_ROLLBACK_PROTECTION',
                 'Restored full finite-domain audit differs from before')
    except Exception as exc:
        failures.append(_error(exc))
    return {'attempted': True, 'ok': not failures, 'failures': failures,
            'scope': 'Full finite-domain snapshot; runtime cache status excluded; worker separately certifies character storage'}


def apply_settings(context, selection, patch):
    written, original, rig, pb = [], None, None, None
    try:
        selection = contract.canonical_selection(selection)
        patch = contract.canonical_request(patch)
        original, rig, pb = _capture(context, selection)
        _require(original['snapshot_sha256'] == patch['expected_snapshot_sha256'],
                 'SETTINGS_SNAPSHOT_CONFLICT', 'Finite-domain native authoring snapshot changed')
        _require(original['definition_sha256'] == patch['expected_definition_sha256'],
                 'SETTINGS_DEFINITION_CONFLICT', 'Selected native definitions/consumers changed')
        for field, change in zip(original['fields'], patch['changes']):
            _require({'type': field['type'], 'value': field['value']} == change['before'],
                     'SETTINGS_BEFORE_CONFLICT', 'Native typed before value differs', {'key': change['key']})
        for change in patch['changes']:
            if change['before']['value'] != change['after']['value']:
                # Register the actual native value BEFORE entering an injectable setter.
                written.append((change['key'], pb[change['key']]))
                _write_property(pb, change['key'], change['after']['value'])
                _require(type(pb[change['key']]) is int and pb[change['key']] == change['after']['value'],
                         'SETTINGS_WRITE_MISMATCH', 'Property setter did not store the exact native int')
        if written:
            _refresh(rig, context)
        actual, _, _ = _capture(context, selection)
        expected = copy.deepcopy(original['snapshot'])
        desired = {row['key']: row['after']['value'] for row in patch['changes']}
        for field in expected['fields']:
            field['value'] = desired[field['key']]
        for effect in expected['effects']:
            effect['value'] = desired[effect['control_key']] > 0
        _require(actual['definition'] == original['definition'], 'SETTINGS_DEFINITION_CHANGED',
                 'Native selected definitions/UI/driver wiring changed after write')
        _require(actual['snapshot'] == expected, 'SETTINGS_PROTECTION_CHANGED',
                 'Finite native audit changed beyond two property/four flag values')
        return {**actual, 'changed': bool(written), 'writes': [key for key, _ in written],
                'rollback': {'attempted': False, 'ok': True, 'failures': []}}
    except Exception as exc:
        rollback = {'attempted': False, 'ok': True, 'failures': []}
        if written:
            rollback = _restore(context, selection, rig, pb, written, original)
        return {'ok': False, 'changed': bool(written) and not rollback['ok'], 'error': _error(exc),
                'rollback': rollback, 'scope': SCOPE}
