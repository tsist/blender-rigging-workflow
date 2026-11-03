"""Bounded original-RNA object dependency evidence, not a solver/cycle oracle.

Direction is consumer -> dependency. Endpoints retain bone/group/path context;
the object projection intentionally makes NO cycle diagnosis. No evaluation or
frame changes occur here. Dynamic observation is a separate explicit operation.
"""
from collections import Counter, deque
import hashlib
import json
import math
import struct

import bpy

from .collector import _constraints, _ref, _target_slots, _target_resolution

MAX_OBJECTS = 50000
MAX_EDGES = 50000
MAX_GROUP_VERTICES = 1000000
MAX_EVALUATED_VERTICES = 250000


def _plain(value, depth=0):
    if depth > 8:
        return {'status': 'property_depth_budget'}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {'nonfinite': str(value)}
    if isinstance(value, bpy.types.ID):
        return _ref(value)
    if isinstance(value, dict) or type(value).__name__ == 'IDPropertyGroup':
        if len(value) > 1024:
            return {'status': 'property_item_budget', 'items': len(value)}
        return {str(key): _plain(part, depth + 1) for key, part in value.items()}
    if hasattr(value, 'bl_rna'):
        return {'rna_type': value.bl_rna.identifier, 'name': getattr(value, 'name', None)}
    if len(value) > 1024:
        return {'status': 'property_item_budget', 'items': len(value)}
    return [_plain(part, depth + 1) for part in value]


def _fields(item, names):
    return {name: _plain(getattr(item, name)) for name in names if hasattr(item, name)}


def cache_state(modifier):
    cache = getattr(modifier, 'point_cache', None)
    return _fields(cache, ('is_baked', 'is_baking', 'is_outdated', 'frame_start',
                          'frame_end', 'use_disk_cache', 'info')) if cache else None


def group_summary(obj, name):
    group = obj.vertex_groups.get(name)
    if group is None:
        return {'name': name, 'status': 'missing'}
    if obj.type != 'MESH' or len(obj.data.vertices) > MAX_GROUP_VERTICES:
        return {'name': name, 'status': 'unsupported_or_budget'}
    weights = [(v.index, entry.weight) for v in obj.data.vertices for entry in v.groups
               if entry.group == group.index]
    finite = [weight for _, weight in weights if math.isfinite(weight)]
    return {'name': name, 'status': 'observed', 'lock_weight': group.lock_weight,
            'members': len(weights), 'positive_members': sum(w > 0 for w in finite),
            'nonfinite': len(weights) - len(finite),
            'min': min(finite) if finite else None, 'max': max(finite) if finite else None,
            'weights_sha256': hashlib.sha256(json.dumps(
                [(i, _plain(w)) for i, w in weights], allow_nan=False).encode()).hexdigest()}


def cloth_details(obj, modifier):
    settings = modifier.settings
    collision = modifier.collision_settings
    pin = settings.vertex_group_mass
    return {'modifier': modifier.name, 'show_viewport': modifier.show_viewport,
            'show_render': modifier.show_render, 'cache': cache_state(modifier),
            'settings': _fields(settings, ('quality', 'mass', 'time_scale', 'gravity',
                'air_damping', 'tension_stiffness', 'compression_stiffness',
                'shear_stiffness', 'bending_stiffness', 'pin_stiffness',
                'vertex_group_mass', 'use_dynamic_mesh', 'use_sewing_springs',
                'use_pressure', 'rest_shape_key')),
            'collision': _fields(collision, ('use_collision', 'use_self_collision',
                'collection', 'collision_quality', 'distance_min', 'self_distance_min')),
            'effector_weights': _fields(settings.effector_weights, ('collection', 'gravity', 'all')),
            'pin_group': group_summary(obj, pin) if pin else {'status': 'not_assigned'}}


def _collection_states(context):
    records = []
    def visit(layer, path, parent_excluded):
        excluded = parent_excluded or layer.exclude
        current = path + [layer.name]
        records.append({'collection': layer.collection.name_full, 'path': current,
                        'exclude': layer.exclude, 'effective_exclude': excluded,
                        'hide_viewport': layer.hide_viewport,
                        'collection_hide_viewport': layer.collection.hide_viewport})
        for child in layer.children:
            visit(child, current, excluded)
    visit(context.view_layer.layer_collection, [], False)
    return records


def collect(context, armature):
    """Original RNA only; explicit rig plus direct mesh consumers seed closure."""
    if not isinstance(armature, bpy.types.Object) or armature.type != 'ARMATURE':
        raise ValueError('Explicit Armature Object required')
    objects = list(bpy.data.objects)
    if len(objects) > MAX_OBJECTS:
        return {'schema_version': 'p2.dependencies.1', 'status': 'budget_exceeded'}
    lookup = {obj.name_full: obj for obj in objects}
    edges = []
    references = []
    def edge(owner, target, kind, **evidence):
        if target is not None:
            if len(edges) >= MAX_EDGES:
                raise RuntimeError('Dependency edge budget exceeded; no partial success')
            edges.append({'consumer': owner.name_full, 'dependency': target.name_full,
                          'kind': kind, **evidence})
    for obj in objects:
        if obj.parent:
            edge(obj, obj.parent, 'parent', parent_type=obj.parent_type,
                 parent_bone=obj.parent_bone)
        for modifier in obj.modifiers:
            for prop in modifier.bl_rna.properties:
                if prop.type == 'POINTER' and prop.fixed_type.identifier == 'Object':
                    edge(obj, getattr(modifier, prop.identifier), 'modifier_object',
                         modifier=modifier.name, modifier_type=modifier.type,
                         role=prop.identifier, show_viewport=modifier.show_viewport)
            if modifier.type == 'CLOTH':
                collision = modifier.collision_settings
                if collision.use_collision:
                    collection = collision.collection
                    candidates = collection.all_objects if collection else context.scene.objects
                    for target in candidates:
                        if target != obj and any(m.type == 'COLLISION' for m in target.modifiers):
                            edge(obj, target, 'cloth_collider_candidate', modifier=modifier.name,
                                 scope='explicit_collection' if collection else 'implicit_current_scene',
                                 native_contact_or_participation_proven=False)
        # Deliberately bounded ID scope. Materials/node trees and indirect ID
        # expression dependencies remain outside this structural projection.
        owners = [obj, obj.data, getattr(obj.data, 'shape_keys', None)]
        for owner in owners:
            animation = getattr(owner, 'animation_data', None)
            if animation:
                for curve in animation.drivers:
                    for variable in curve.driver.variables:
                        count = {'SINGLE_PROP': 1, 'TRANSFORMS': 1, 'LOC_DIFF': 2,
                                 'ROTATION_DIFF': 2}.get(variable.type, 0)
                        for target in list(variable.targets)[:count]:
                            if target.id is None:
                                references.append({'consumer': obj.name_full, 'kind': 'driver_input',
                                    'owner_id': _ref(owner), 'driven_path': curve.data_path,
                                    'variable': variable.name, 'resolution': 'missing_id'})
                            if isinstance(target.id, bpy.types.Object):
                                edge(obj, target.id, 'driver_object_input', owner_id=_ref(owner),
                                     driven_path=curve.data_path, array_index=curve.array_index,
                                     variable=variable.name, variable_type=variable.type,
                                     input_path=target.data_path, input_bone=target.bone_target,
                                     mute=curve.mute)
    referenced_groups = {}
    for obj, bone, constraint in _constraints(objects):
        for role, target, subtarget, optional in _target_slots(constraint):
            resolution = _target_resolution(target, subtarget, optional)
            if not resolution.startswith('resolved_'):
                references.append({'consumer': obj.name_full, 'kind': 'constraint_target',
                    'owner_bone': bone.name if bone else None, 'constraint': constraint.name,
                    'role': role, 'target': _ref(target), 'subtarget': subtarget,
                    'resolution': resolution})
            edge(obj, target, 'constraint_target', owner_bone=bone.name if bone else None,
                 constraint=constraint.name, constraint_type=constraint.type, role=role,
                 subtarget=subtarget, resolution=resolution, mute=constraint.mute,
                 influence=_plain(constraint.influence), native_is_valid=constraint.is_valid,
                 spaces=_fields(constraint, ('owner_space', 'target_space')))
            if target and target.type == 'MESH' and subtarget:
                referenced_groups.setdefault(target.name_full, set()).add(subtarget)
    seeds = [armature.name_full]
    for obj in objects:
        if obj.type == 'MESH' and (obj.parent == armature or any(
                m.type == 'ARMATURE' and m.object == armature for m in obj.modifiers)):
            seeds.append(obj.name_full)
    outgoing = {}
    for row in edges:
        outgoing.setdefault(row['consumer'], []).append(row['dependency'])
    closure, queue = set(seeds), deque(seeds)
    while queue:
        for name in outgoing.get(queue.popleft(), ()):
            if name not in closure:
                closure.add(name)
                queue.append(name)
    states = _collection_states(context)
    active = {obj.name_full for obj in context.view_layer.objects}
    records = []
    for name in sorted(closure):
        obj = lookup[name]
        row = {'object': _ref(obj), 'type': obj.type,
               'in_active_view_layer': name in active,
               'in_current_scene': name in context.scene.objects,
               'hide_viewport': obj.hide_viewport, 'hide_render': obj.hide_render,
               'users_collection': [c.name_full for c in obj.users_collection],
               'collection_paths': [s for s in states if s['collection'] in {c.name_full for c in obj.users_collection}],
               'cloth': [cloth_details(obj, m) for m in obj.modifiers if m.type == 'CLOTH']}
        if obj.type == 'MESH':
            row['original_mesh'] = {'vertices': len(obj.data.vertices), 'edges': len(obj.data.edges),
                                    'polygons': len(obj.data.polygons)}
            row['referenced_groups'] = [group_summary(obj, group) for group in sorted(referenced_groups.get(name, ()))]
        if obj.type == 'ARMATURE' and obj.pose:
            row['custom_properties'] = {key: _plain(value) for key, value in obj.items() if key != '_RNA_UI'}
            row['pose_custom_properties'] = {bone.name: {key: _plain(value) for key, value in bone.items() if key != '_RNA_UI'}
                for bone in obj.pose.bones if bone.keys()}
        records.append(row)
    selected_edges = [e for e in edges if e['consumer'] in closure]
    return {'schema_version': 'p2.dependencies.1', 'status': 'ok',
            'read_only': True, 'evaluation_performed': False,
            'scene': context.scene.name_full, 'view_layer': context.view_layer.name,
            'frame': context.scene.frame_current, 'main_rig': _ref(armature),
            'seeds': sorted(set(seeds)), 'objects': records, 'edges': selected_edges,
            'unresolved_or_optional_references': [r for r in references if r['consumer'] in closure],
            'summary': {'closure_objects': len(records), 'file_objects': len(objects),
                        'edges': len(selected_edges), 'by_kind': dict(Counter(e['kind'] for e in selected_edges))},
            'coverage': {'status': 'partial', 'projection': 'object references; consumer -> dependency',
                'included': ['parent', 'constraint Object targets with bone/group annotations',
                             'modifier Object pointers', 'object/data/Key driver Object inputs',
                             'Cloth collision object candidates'],
                'not_certified': ['native operation graph completeness', 'node tree/geometry nodes dependencies',
                    'all non-Object driver targets', 'arbitrary Python expression reads',
                    'instances', 'force field scheduling', 'runtime cycles or collision contacts'],
                'cycle_diagnosis': 'none; coarse object cycles are not solver fault proof'}}


def evaluated_snapshot(context, names, bones=()):
    """Explicit evaluation, kept separate from original-RNA collection."""
    graph = context.evaluated_depsgraph_get()
    members = {obj.original.name_full: obj for obj in graph.objects}
    rows = []
    for name in names:
        original = bpy.data.objects.get(name)
        iterated = members.get(name)
        # graph.objects iteration may omit hidden indirect dependencies. Query
        # the native evaluated ID separately and record both witnesses. A
        # returned original ID is a fallback, never evaluated geometry proof.
        candidate = graph.id_eval_get(original) if original is not None else None
        evaluated = candidate if (isinstance(candidate, bpy.types.Object)
            and candidate.is_evaluated and candidate.original == original) else None
        row = {'name': name, 'depsgraph_objects_iterator_member': iterated is not None,
               'evaluated_id_available': evaluated is not None,
               'native_id_query': 'Depsgraph.id_eval_get; is_evaluated and original identity checked',
               'original_exists': original is not None}
        if evaluated is not None:
            row['is_evaluated'] = evaluated.is_evaluated
            row['matrix_world'] = [_plain(list(part)) for part in evaluated.matrix_world]
            if evaluated.type == 'MESH':
                count = len(evaluated.data.vertices)
                row['vertices'] = count
                if count > MAX_EVALUATED_VERTICES:
                    row['geometry_status'] = 'budget_exceeded'
                    row['vertex_budget'] = MAX_EVALUATED_VERTICES
                else:
                    hasher, coords, bad, bad_count = hashlib.sha256(), [], [], 0
                    for vertex in evaluated.data.vertices:
                        co = tuple(vertex.co)
                        hasher.update(struct.pack('<3d', *co))
                        for axis, value in enumerate(co):
                            if not math.isfinite(value):
                                bad_count += 1
                                if len(bad) < 32:
                                    bad.append([vertex.index, axis])
                        if count <= 2000:
                            coords.append([_plain(value) for value in co])
                    row.update(geometry_status='observed', nonfinite_components=bad,
                        nonfinite_component_count=bad_count, local_coords_sha256=hasher.hexdigest(),
                        hash_encoding='ordered local vertex xyz, little-endian float64',
                        local_coords=coords if count <= 2000 else 'over_detail_budget')
            if evaluated.type == 'ARMATURE' and evaluated.pose:
                row['bones'] = {name: {'matrix': [_plain(list(part)) for part in evaluated.pose.bones[name].matrix],
                    'head_world': _plain(evaluated.matrix_world @ evaluated.pose.bones[name].head),
                    'tail_world': _plain(evaluated.matrix_world @ evaluated.pose.bones[name].tail)}
                    for name in bones if name in evaluated.pose.bones}
        if original:
            row['cache'] = {m.name: cache_state(m) for m in original.modifiers if m.type == 'CLOTH'}
            row['cloth_flags'] = {m.name: {'show_viewport': m.show_viewport, 'show_render': m.show_render}
                                  for m in original.modifiers if m.type == 'CLOTH'}
        rows.append(row)
    return {'frame': context.scene.frame_current, 'depsgraph_mode': graph.mode,
            'graph_objects': len(members), 'objects': rows,
            'claims': 'finite frame observation; not FPS or continuous-time correctness'}
