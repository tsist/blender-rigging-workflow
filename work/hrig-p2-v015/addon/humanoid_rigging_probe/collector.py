"""Read original RNA only; no evaluation, expression execution or datablock writes.

collect(context, armature=None) returns JSON-compatible primitives. The optional
armature must be an Object, never a name. A missing selection is only inferred
when the active View Layer contains exactly one Armature object.
"""

from collections import Counter
from datetime import datetime, timezone
import heapq
import math

import bpy
from mathutils import Color, Euler, Matrix, Quaternion, Vector


MAX_BONES = 10000
MAX_DRIVERS = 10000
MAX_CONSTRAINTS = 50000
MAX_DETAILS = 1000
MAX_IDS = 100000
MAX_OBJECTS = 50000


def _ref(item):
    if item is None:
        return None
    library = getattr(item, "library", None)
    return {"name": item.name_full, "id_type": item.bl_rna.identifier,
            "library": library.filepath if library else None}


def _number(value):
    value = float(value)
    return value if math.isfinite(value) else None


class _Details:
    """Independent bounded heaps prevent targets from starving driver details."""

    CAPS = {'constraint_targets': 500, 'drivers': 500}

    def __init__(self):
        self.total = Counter()
        self.heaps = {key: [] for key in self.CAPS}
        self.destinations = {}
        self.sequence = 0

    def add(self, category, destination, row, priority=(0, 0, 0)):
        self.total[category] += 1
        self.destinations[category] = destination
        # Higher priority wins; the earliest row wins ties. Sequence uniqueness
        # also prevents heapq from ever comparing report dictionaries.
        entry = (priority, -self.sequence, row)
        self.sequence += 1
        heap = self.heaps[category]
        if len(heap) < self.CAPS[category]:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)

    def report(self):
        stored = {key: len(heap) for key, heap in self.heaps.items()}
        for key, heap in self.heaps.items():
            if key in self.destinations:
                self.destinations[key][:] = [entry[2] for entry in sorted(heap, reverse=True)]
        used = sum(stored.values())
        return {"max": MAX_DETAILS, "total": sum(self.total.values()),
                "stored": used, "truncated": sum(self.total.values()) > used,
                "selection_policy": "main_rig; unresolved; mesh_lattice_subtarget; stable_input_order",
                "by_category": {key: {"max": cap, "total": self.total[key], "stored": stored[key],
                                      "truncated": self.total[key] > stored[key]}
                                for key, cap in self.CAPS.items()}}


def _unresolved(resolution):
    return not (resolution.startswith('resolved_') or resolution == 'optional_empty')


def _related_id_pointers(objects):
    """Only membership ranking; this neither expands collection scope nor evaluates."""
    result = set()
    for obj in objects:
        for item in (obj, obj.data):
            if item is not None:
                result.add(item.as_pointer())
                for attr in ('node_tree', 'shape_keys'):
                    embedded = getattr(item, attr, None)
                    if embedded is not None:
                        result.add(embedded.as_pointer())
        for slot in obj.material_slots:
            material = slot.material
            if material is not None:
                result.add(material.as_pointer())
                if material.node_tree is not None:
                    result.add(material.node_tree.as_pointer())
    return result


def _all_ids():
    """Top-level bpy.data ID collections plus embedded node trees/shape keys."""
    found = {}
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION':
            continue
        for item in getattr(bpy.data, prop.identifier):
            if isinstance(item, bpy.types.ID):
                found[item.as_pointer()] = item
                if len(found) > MAX_IDS:
                    return None
    # Embedded IDs are not consistently present in bpy.data.node_groups.
    for item in tuple(found.values()):
        for attr in ("node_tree", "shape_keys"):
            embedded = getattr(item, attr, None)
            if isinstance(embedded, bpy.types.ID):
                found[embedded.as_pointer()] = embedded
                if len(found) > MAX_IDS:
                    return None
    return list(found.values())


def _constraints(objects):
    for obj in objects:
        for constraint in obj.constraints:
            yield obj, None, constraint
        if obj.type == 'ARMATURE' and obj.pose:
            for bone in obj.pose.bones:
                for constraint in bone.constraints:
                    yield obj, bone, constraint


def _target_resolution(target, subtarget, optional=False):
    if target is None:
        return "optional_empty" if optional and not subtarget else "missing_object"
    if not subtarget:
        return "resolved_object"
    if target.type == 'ARMATURE':
        return "resolved_bone" if target.data.bones.get(subtarget) else "missing_bone"
    if target.type in {'MESH', 'LATTICE'}:
        return ("resolved_vertex_group" if target.vertex_groups.get(subtarget)
                else "missing_vertex_group")
    return "unsupported"


def _target_slots(constraint):
    """Inspect native Object pointer roles, including ArmatureConstraint targets."""
    if constraint.type == 'ARMATURE':
        for index, slot in enumerate(constraint.targets):
            yield "targets[%d]" % index, slot.target, slot.subtarget, False
    for prop in constraint.bl_rna.properties:
        if prop.type != 'POINTER' or prop.fixed_type.identifier != 'Object':
            continue
        role = prop.identifier
        target = getattr(constraint, role)
        sub_name = {"target": "subtarget", "pole_target": "pole_subtarget",
                    "space_object": "space_subtarget"}.get(role)
        subtarget = getattr(constraint, sub_name, "") if sub_name else ""
        optional = (constraint.type == 'IK' and role == 'target') or role == 'pole_target'
        # A custom-space pointer is inactive unless that space is selected.
        if role == 'space_object' and not (
                getattr(constraint, 'owner_space', None) == 'CUSTOM' or
                getattr(constraint, 'target_space', None) == 'CUSTOM'):
            continue
        yield role, target, subtarget, optional


def _resolve_path(owner, path, array_index=None):
    """Native RNA resolver; never eval a path or driver expression."""
    if owner is None:
        return {"resolution": "missing_id"}
    if not path:
        return {"resolution": "empty_path"}
    try:
        value = owner.path_resolve(path)
    except (ValueError, TypeError, AttributeError, KeyError, RuntimeError) as exc:
        return {"resolution": "unresolved_path", "error_type": type(exc).__name__}
    result = {"resolution": "resolved_path", "value_type": type(value).__name__}
    if array_index is not None:
        if isinstance(value, Matrix):
            result.update(resolution='unsupported_value_type', kind='matrix')
            return result
        # Scalar FCurves use array_index=0; strings and mappings are not arrays.
        # bpy_prop_array is a runtime wrapper, not a guaranteed bpy.types export.
        is_array = (isinstance(value, (tuple, list, Euler, Vector, Quaternion, Color)) or
                    type(value).__name__ in {'bpy_prop_array', 'IDPropertyArray'})
        if is_array:
            length = len(value)
            result.update(kind="array", length=length)
            if not 0 <= array_index < length:
                result['resolution'] = 'invalid_array_index'
        else:
            result['kind'] = 'scalar'
            if array_index != 0:
                result['resolution'] = 'invalid_scalar_index'
    return result


def _variable(var, context):
    kind = var.type
    active = {'SINGLE_PROP': 1, 'TRANSFORMS': 1, 'ROTATION_DIFF': 2,
              'LOC_DIFF': 2, 'CONTEXT_PROP': 1}.get(kind)
    row = {"name": var.name, "type": kind, "active_slots": active,
           "native_slots": len(var.targets), "targets": []}
    if active is None:
        row['resolution'] = 'unsupported_variable_type'
        return row
    row['ignored_inactive_slots'] = max(0, len(var.targets) - active)
    for index, target in enumerate(var.targets):
        if index >= active:
            break
        item = {"slot": index}
        if kind == 'CONTEXT_PROP':
            source = target.context_property
            owner = {'ACTIVE_SCENE': context.scene,
                     'ACTIVE_VIEW_LAYER': context.view_layer}.get(source)
            item.update(context_property=source, data_path=target.data_path)
            item.update(_resolve_path(owner, target.data_path) if owner else
                        {"resolution": "unsupported_context_source"})
        elif kind == 'SINGLE_PROP':
            item.update(id=_ref(target.id), data_path=target.data_path)
            item.update(_resolve_path(target.id, target.data_path))
            item['use_fallback_value'] = bool(getattr(target, 'use_fallback_value', False))
        else:
            owner = target.id
            bone = target.bone_target
            item.update(id=_ref(owner), bone_target=bone,
                        transform_type=target.transform_type,
                        transform_space=target.transform_space)
            if owner is None:
                item['resolution'] = 'missing_id'
            elif not isinstance(owner, bpy.types.Object):
                item['resolution'] = 'unsupported_transform_id'
            elif bone and owner.type != 'ARMATURE':
                item['resolution'] = 'unsupported_bone_target_type'
            elif bone and owner.data.bones.get(bone) is None:
                item['resolution'] = 'missing_bone'
            else:
                item['resolution'] = 'resolved_bone' if bone else 'resolved_object'
        row['targets'].append(item)
    return row


def _modifier(modifier):
    row = {"name": modifier.name, "type": modifier.type,
           "show_viewport": bool(modifier.show_viewport),
           "show_render": bool(modifier.show_render)}
    cache = getattr(modifier, 'point_cache', None)
    if cache is not None:
        row['point_cache'] = {key: getattr(cache, key) for key in
                              ('is_baked', 'is_baking', 'is_outdated', 'frame_start',
                               'frame_end', 'use_disk_cache') if hasattr(cache, key)}
    if modifier.type == 'SOFT_BODY' and cache is None:
        row['point_cache_status'] = 'not_exposed_on_modifier'
    return row


def collect(context, armature=None):
    """Return schema p2.1. No frame changes, depsgraph evaluation or RNA writes."""
    scene, layer = context.scene, context.view_layer
    all_objects = list(bpy.data.objects)
    scene_objects = list(scene.objects)
    layer_objects = list(layer.objects)
    scene_pointers = {item.as_pointer() for item in scene_objects}
    layer_pointers = {item.as_pointer() for item in layer_objects}
    rigs = [obj for obj in all_objects if obj.type == 'ARMATURE']
    active_rigs = [obj for obj in layer_objects if obj.type == 'ARMATURE']
    selected = armature
    selection = 'explicit' if armature is not None else 'unique_active_view_layer'
    selection_error = None
    if selected is None:
        selected = active_rigs[0] if len(active_rigs) == 1 else None
        if selected is None:
            selection_error = 'explicit_armature_required'
    elif not isinstance(selected, bpy.types.Object) or selected.type != 'ARMATURE':
        selected = None
        selection_error = 'explicit_target_is_not_armature_object'
    elif selected.as_pointer() not in {obj.as_pointer() for obj in all_objects}:
        selected = None
        selection_error = 'explicit_target_not_in_file'
    selected_pointer = selected.as_pointer() if selected else None
    object_rank = lambda obj: (0 if obj.as_pointer() == selected_pointer else
                              1 if obj.as_pointer() in layer_pointers else 2)
    # Python's stable sort retains bpy.data order within the same membership.
    all_objects.sort(key=object_rank)
    scene_objects.sort(key=object_rank)
    rigs.sort(key=object_rank)
    main_id_pointers = _related_id_pointers([selected]) if selected else set()
    active_id_pointers = _related_id_pointers(layer_objects)
    report = {
        'schema_version': 'p2.1', 'status': 'ok',
        'meta': {'prototype_version': '0.2.0', 'created_utc': datetime.now(timezone.utc).isoformat(),
                 'blender_version': bpy.app.version_string,
                 'blender_build_hash': bpy.app.build_hash.decode('ascii'), 'read_only': True,
                 'evaluation_performed': False, 'expression_execution': False,
                 'unsupported_claims': ['editing', 'template_generation', 'FPS', 'fault_or_cycle_proof']},
        'context': {'scene': scene.name_full, 'view_layer': layer.name,
                    'frame': scene.frame_current, 'mode': context.mode,
                    'active_object': _ref(layer.objects.active),
                    'selected_objects': [_ref(obj) for obj in getattr(context, 'selected_objects', ())],
                    'main_rig': _ref(selected), 'main_rig_selection': selection,
                    'main_rig_in_current_scene': selected_pointer in scene_pointers if selected else None,
                    'main_rig_in_active_view_layer': selected_pointer in layer_pointers if selected else None,
                    'selection_error': selection_error},
        'scope': {'inventory': 'current_scene_objects; active_view_layer_membership',
                  'constraints': 'all_file_objects_and_pose_bones',
                  'bones': 'sum_per_file_armature_object; shared_data_counted_per_object',
                  'drivers': 'native_animation_data.drivers_on_all_file_IDs_and_embedded_node_trees',
                  'semantic_bone_mapping': 'none', 'excluded_is_not_hidden': True},
        'rigs': [], 'objects': [], 'constraint_targets': [],
        'constraints': {'summary': {}}, 'drivers': {'summary': {}, 'details': []},
        'diagnostics': [], 'limits': {'bones': MAX_BONES, 'drivers': MAX_DRIVERS,
                                     'constraints': MAX_CONSTRAINTS, 'details': MAX_DETAILS,
                                     'ids': MAX_IDS, 'objects': MAX_OBJECTS}}
    if len(all_objects) > MAX_OBJECTS:
        report.update(status='budget_exceeded', refusal={'resource': 'objects', 'observed': len(all_objects)})
        return report
    ids = _all_ids()
    if ids is None:
        report.update(status='budget_exceeded', refusal={'resource': 'ids', 'observed_at_least': MAX_IDS + 1})
        return report
    ids.sort(key=lambda item: (0 if item.as_pointer() in main_id_pointers else
                              1 if item.as_pointer() in active_id_pointers else 2))
    bone_count = sum(len(obj.data.bones) for obj in rigs)
    constraint_count = sum(len(obj.constraints) + (sum(len(bone.constraints) for bone in obj.pose.bones)
                           if obj.type == 'ARMATURE' and obj.pose else 0) for obj in all_objects)
    driver_count = sum(len(animation.drivers) for item in ids
                       if (animation := getattr(item, 'animation_data', None)) is not None)
    report['limits']['observed'] = {'bones': bone_count, 'constraints': constraint_count,
                                   'drivers': driver_count, 'ids': len(ids), 'objects': len(all_objects)}
    exceeded = {name: {'observed': value, 'maximum': report['limits'][name]}
                for name, value in report['limits']['observed'].items()
                if value > report['limits'][name]}
    if exceeded:
        report.update(status='budget_exceeded', refusal=exceeded)
        return report
    details = _Details()
    for rig in rigs:
        pointer = rig.as_pointer()
        report['rigs'].append({'object': _ref(rig), 'data': _ref(rig.data),
                               'in_current_scene': pointer in scene_pointers,
                               'in_active_view_layer': pointer in layer_pointers,
                               'membership': ('active_view_layer' if pointer in layer_pointers else
                                              'excluded_from_active_view_layer' if pointer in scene_pointers else 'other_file_data'),
                               'is_main_rig': rig == selected, 'bones': len(rig.data.bones),
                               'pose_bones': len(rig.pose.bones) if rig.pose else 0,
                               'object_constraints': len(rig.constraints),
                               'pose_constraints': (sum(len(b.constraints) for b in rig.pose.bones)
                                                    if rig.pose else 0),
                               'object_drivers': (len(rig.animation_data.drivers)
                                                  if rig.animation_data else 0),
                               'data_drivers': (len(rig.data.animation_data.drivers)
                                                if rig.data.animation_data else 0),
                               'data_users': rig.data.users})
    for obj in scene_objects:
        row = {'object': _ref(obj), 'type': obj.type, 'data': _ref(obj.data),
               'in_active_view_layer': obj.as_pointer() in layer_pointers,
               'hide_viewport': bool(obj.hide_viewport), 'hide_render': bool(obj.hide_render),
               'object_constraints': len(obj.constraints),
               'modifiers': [_modifier(mod) for mod in obj.modifiers]}
        soft_body = getattr(obj, 'soft_body', None)
        if soft_body is not None:
            row['soft_body_present'] = True
        report['objects'].append(row)
    constraint_types, resolution_counts = Counter(), Counter()
    constraint_owners = {}
    flags = Counter()
    for obj, bone, constraint in _constraints(all_objects):
        pointer = obj.as_pointer()
        if pointer not in constraint_owners:
            constraint_owners[pointer] = {
                'owner_object': obj.name_full, 'owner_id': _ref(obj),
                'is_main_rig': pointer == selected_pointer,
                'in_active_view_layer': pointer in layer_pointers,
                'total': 0, 'target_resolution': Counter()}
        owner_counts = constraint_owners[pointer]
        owner_counts['total'] += 1
        constraint_types[constraint.type] += 1
        flags['native_is_valid_false'] += not constraint.is_valid
        flags['muted'] += bool(constraint.mute)
        flags['zero_influence'] += constraint.influence == 0.0
        for role, target, subtarget, optional in _target_slots(constraint):
            resolution = _target_resolution(target, subtarget, optional)
            resolution_counts[resolution] += 1
            owner_counts['target_resolution'][resolution] += 1
            row = {'owner_object': obj.name_full, 'owner_bone': bone.name if bone else None,
                   'constraint': constraint.name, 'constraint_type': constraint.type,
                   'role': role, 'target_object': target.name_full if target else None,
                   'target_type': target.type if target else None, 'subtarget': subtarget,
                   'resolution': resolution, 'native_is_valid': bool(constraint.is_valid),
                   'mute': bool(constraint.mute), 'influence': _number(constraint.influence),
                   'interpretation': 'structural_resolution_only_not_fault_diagnosis'}
            priority = (int(pointer == selected_pointer), int(_unresolved(resolution)),
                        int(target is not None and target.type in {'MESH', 'LATTICE'} and bool(subtarget)))
            details.add('constraint_targets', report['constraint_targets'], row, priority)
    report['constraints']['summary'] = {'total': constraint_count, 'by_type': dict(constraint_types),
                                        'target_resolution': dict(resolution_counts),
                                        'by_owner_object': [dict(owner, target_resolution=dict(owner['target_resolution']))
                                                            for owner in constraint_owners.values()],
                                        'native_flags_informational_only': dict(flags)}
    driver_types, path_counts, variable_counts = Counter(), Counter(), Counter()
    driver_owners = []
    for item in ids:
        animation = getattr(item, 'animation_data', None)
        if not animation:
            continue
        pointer = item.as_pointer()
        owner_counts = {'owner_id': _ref(item), 'total': len(animation.drivers),
                        'is_main_rig_related_id': pointer in main_id_pointers,
                        'is_active_view_layer_related_id': pointer in active_id_pointers,
                        'driven_path_resolution': Counter(),
                        'active_variable_target_resolution': Counter()}
        if animation.drivers:
            driver_owners.append(owner_counts)
        for fcurve in animation.drivers:
            driver = fcurve.driver
            path = _resolve_path(item, fcurve.data_path, fcurve.array_index)
            variables = [_variable(var, context) for var in driver.variables]
            driver_types[driver.type] += 1
            path_counts[path['resolution']] += 1
            owner_counts['driven_path_resolution'][path['resolution']] += 1
            unresolved = _unresolved(path['resolution'])
            for var in variables:
                if 'resolution' in var:
                    variable_counts[var['resolution']] += 1
                    owner_counts['active_variable_target_resolution'][var['resolution']] += 1
                    unresolved = unresolved or _unresolved(var['resolution'])
                for target in var['targets']:
                    variable_counts[target['resolution']] += 1
                    owner_counts['active_variable_target_resolution'][target['resolution']] += 1
                    unresolved = unresolved or _unresolved(target['resolution'])
            row = {'owner_id': _ref(item), 'data_path': fcurve.data_path,
                   'array_index': fcurve.array_index, 'path': path, 'driver_type': driver.type,
                   'native_is_valid': bool(driver.is_valid), 'mute': bool(fcurve.mute),
                   'is_simple_expression': bool(driver.is_simple_expression),
                   'expression_evaluated': False, 'variables': variables,
                   'interpretation': 'native_flags_and_resolution_are_not_fault_proof'}
            priority = (int(pointer in main_id_pointers), int(unresolved), 0)
            details.add('drivers', report['drivers']['details'], row, priority)
    report['drivers']['summary'] = {'total': driver_count, 'by_type': dict(driver_types),
                                     'driven_path_resolution': dict(path_counts),
                                     'by_owner_id': [dict(owner,
                                                         driven_path_resolution=dict(owner['driven_path_resolution']),
                                                         active_variable_target_resolution=dict(owner['active_variable_target_resolution']))
                                                     for owner in driver_owners],
                                     'active_variable_target_resolution': dict(variable_counts)}
    report['limits']['detail_accounting'] = details.report()
    if selection_error:
        report['status'] = 'selection_required'
    return report
