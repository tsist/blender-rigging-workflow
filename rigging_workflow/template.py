"""Private C94 native-template extraction experiment.

This preserves an existing C94 mechanism graph with Object.copy/Data.copy. It is
not a generic generator, new-character fit, weight solver, face author, dynamic
backend, or distributable template. Hair extraction is intentionally unimplemented
until the new hair/skirt backend exists; this does not cancel those product goals.
"""
from collections import Counter

import bpy

from .modules import path_bones

SCHEMA = 'c94.private-template-extraction.v1'
OPTIONAL = ('face', 'hair', 'breast', 'glute', 'toes')
SUBTARGETS = {'target': 'subtarget', 'pole_target': 'pole_subtarget',
              'space_object': 'space_subtarget'}


class TemplateExtractionError(RuntimeError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(code + ': ' + str(detail))


def _require(condition, code, detail):
    if not condition:
        raise TemplateExtractionError(code, detail)


def _ref(value):
    return None if value is None else {'type': value.bl_rna.identifier,
                                       'name': value.name}


def _constraints(rig, keep):
    yield '@object', rig.constraints
    if hasattr(rig.data, 'constraints'):
        yield '@data', rig.data.constraints
    for name in sorted(keep):
        yield name, rig.pose.bones[name].constraints


def _driver_sets(rig):
    for name, owner in (('@object', rig), ('@data', rig.data)):
        ad = owner.animation_data
        if ad:
            yield name, owner, ad


def _kept_curve(curve, keep):
    return all(bone in keep for bone in path_bones(curve.data_path))


def _id_properties(owner, prefix):
    """Inspect only. Unknown metadata is neither deleted nor normalized."""
    def walk(value, location):
        if isinstance(value, bpy.types.ID):
            yield location, value
        elif hasattr(value, 'items'):
            for key, item in value.items():
                yield from walk(item, location + '[' + repr(key) + ']')
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                yield from walk(item, location + '[' + str(index) + ']')
    for key in owner.keys():
        yield from walk(owner[key], prefix + '[' + repr(key) + ']')


def _metadata_ids(rig, keep):
    for owner, prefix in [(rig, '@object'), (rig.data, '@data')]:
        yield from _id_properties(owner, prefix)
    for name in sorted(keep):
        yield from _id_properties(rig.data.bones[name], 'bone:' + name)
        yield from _id_properties(rig.pose.bones[name], 'pose:' + name)


def _validate_evaluation_references(rig, keep, allowed_ids):
    """Reject external evaluation IDs before any new object is allocated."""
    errors = []
    for owner, constraints in _constraints(rig, keep):
        for c in constraints:
            location = {'owner': owner, 'constraint': c.name, 'type': c.type}
            for prop in c.bl_rna.properties:
                if prop.type != 'POINTER' or prop.identifier == 'rna_type':
                    continue
                target = getattr(c, prop.identifier)
                if not isinstance(target, bpy.types.ID):
                    continue
                detail = {**location, 'field': prop.identifier, 'target': _ref(target)}
                if target not in allowed_ids:
                    errors.append({**detail, 'reason': 'external_evaluation_id'})
                    continue
                subtarget = getattr(c, SUBTARGETS.get(prop.identifier, ''), '')
                if subtarget and subtarget not in keep:
                    errors.append({**detail, 'subtarget': subtarget, 'reason': 'removed_bone'})
            for item in getattr(c, 'targets', ()):
                if item.target is None:
                    if item.subtarget:
                        errors.append({**location, 'reason':'missing_armature_target',
                                       'subtarget':item.subtarget})
                elif item.target not in allowed_ids:
                    errors.append({**location, 'reason':'external_armature_target',
                                   'target':_ref(item.target)})
                elif item.subtarget and item.subtarget not in keep:
                    errors.append({**location, 'reason':'removed_armature_target_bone',
                                   'subtarget':item.subtarget})
    for domain, owner, ad in _driver_sets(rig):
        for curve in ad.drivers:
            if not _kept_curve(curve, keep):
                continue
            detail = {'owner':domain, 'path':curve.data_path, 'index':curve.array_index}
            try:
                owner.path_resolve(curve.data_path)
            except (ValueError, KeyError, TypeError):
                errors.append({**detail, 'reason':'unresolved_output'})
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id not in allowed_ids:
                        errors.append({**detail, 'variable':variable.name,
                                       'reason':'external_or_missing_driver_id',
                                       'target':_ref(target.id)})
                        continue
                    names = ([target.bone_target] if target.bone_target else []) + path_bones(target.data_path)
                    for name in names:
                        if name not in keep:
                            errors.append({**detail, 'variable':variable.name,
                                           'reason':'removed_driver_input_bone', 'bone':name})
                    if target.data_path:
                        try:
                            target.id.path_resolve(target.data_path)
                        except (ValueError, KeyError, TypeError):
                            errors.append({**detail, 'variable':variable.name,
                                           'reason':'unresolved_driver_input',
                                           'input_path':target.data_path})
    return errors


def _scene_collection(scene, target):
    if scene.collection == target:
        return True
    def walk(parent):
        for child in parent.children:
            if child == target or walk(child):
                return True
        return False
    return walk(scene.collection)


def _preflight(source, plan, enabled_modules, collection, name):
    _require(isinstance(source, bpy.types.Object) and source.type == 'ARMATURE',
             'INVALID_SOURCE', 'Expected the cleaned C94 armature object')
    _require(not source.library and not source.data.library,
             'LINKED_SOURCE', 'Use a local owned working copy')
    _require(bpy.context.mode == 'OBJECT' and source.mode == 'OBJECT',
             'OBJECT_MODE_REQUIRED', 'Finish the current edit before extracting')
    _require(isinstance(collection, bpy.types.Collection) and not collection.library,
             'INVALID_COLLECTION', 'A local dedicated collection is required')
    _require(_scene_collection(bpy.context.scene, collection),
             'COLLECTION_NOT_IN_SCENE', collection.name)
    _require(not collection.objects and not collection.children,
             'COLLECTION_NOT_EMPTY', 'Use an empty dedicated target collection')
    _require(isinstance(name, str) and bool(name.strip()) and name not in bpy.data.objects,
             'INVALID_OR_USED_NAME', name)
    _require(isinstance(plan, dict) and plan.get('schema') == 'c94.module-plan.v1',
             'INVALID_PLAN', 'Expected build_module_plan output')
    _require(plan.get('main_rig') == source.name, 'STALE_PLAN_RIG',
             {'expected':plan.get('main_rig'), 'actual':source.name})
    _require(not plan.get('unresolved'), 'UNRESOLVED_PLAN', plan.get('unresolved'))
    _require(not plan.get('blockers') and not plan.get('errors'),
             'BLOCKED_PLAN', {'blockers':plan.get('blockers'), 'errors':plan.get('errors')})
    _require(plan.get('status') not in {'incomplete','blocked','failed','not_implemented'},
             'INCOMPLETE_PLAN', plan.get('status'))
    _require(isinstance(enabled_modules, dict), 'INVALID_MODULE_SELECTION', 'Use explicit booleans')
    _require(set(OPTIONAL) <= set(enabled_modules), 'MISSING_MODULE_SELECTION',
             sorted(set(OPTIONAL)-set(enabled_modules)))
    _require(not (set(enabled_modules)-set(OPTIONAL)-{'core','skirt','generic_accessories'}),
             'UNKNOWN_MODULE', sorted(set(enabled_modules)-set(OPTIONAL)-{'core','skirt','generic_accessories'}))
    _require(all(isinstance(value, bool) for value in enabled_modules.values()),
             'INVALID_MODULE_BOOLEAN', enabled_modules)
    _require(enabled_modules.get('core', True), 'CORE_REQUIRED', 'C94 body/limb/hand core is mandatory')
    _require(enabled_modules['hair'] is False, 'HAIR_NOT_IMPLEMENTED',
             'This private extraction experiment only supports hair=False; new hair backend remains planned')
    _require(not enabled_modules.get('skirt') and not enabled_modules.get('generic_accessories'),
             'ACCESSORY_NOT_IMPLEMENTED', 'New skirt/accessory generation is not implemented')
    names = set(source.data.bones.keys())
    _require(plan.get('bone_count') == len(names) and set(plan.get('assignments', {})) == names,
             'STALE_PLAN_BONES', 'Plan bone identity does not match this source')
    modules = plan.get('modules', {})
    _require(not modules.get('unclassified', {}).get('bones'),
             'UNCLASSIFIED_PLAN', modules.get('unclassified'))
    selected = ['core'] + [key for key in OPTIONAL if enabled_modules[key]]
    keep = set()
    for key in selected:
        row = modules.get(key)
        _require(isinstance(row, dict) and isinstance(row.get('required_closure'), list)
                 and bool(row['required_closure']), 'INCOMPLETE_MODULE_CLOSURE', key)
        closure = set(row['required_closure'])
        _require(all(isinstance(n, str) for n in closure) and closure <= names,
                 'STALE_MODULE_CLOSURE', {'module':key,'missing':sorted(closure-names)})
        _require(not row.get('blockers') and row.get('status') not in {'incomplete','blocked','failed'},
                 'BLOCKED_MODULE', key)
        keep.update(closure)
    _require(set(modules['core'].get('seed_bones', ())) <= keep,
             'CORE_SEEDS_REMOVED', 'Core controls must be preserved')
    for key in OPTIONAL:
        if enabled_modules[key]:
            continue
        # Shared dependencies may remain. An exclusive disabled-module bone may
        # not silently turn the supposedly disabled feature back on.
        exclusive = set(modules.get(key, {}).get('bones', ())) & keep
        _require(not exclusive, 'DISABLED_EXCLUSIVE_DEPENDENCY',
                 {'module':key,'bones':sorted(exclusive)})
    deleted = names - keep
    for edge in plan.get('dependencies', []):
        if edge.get('kind') == 'display':
            continue
        _require(not (edge.get('owner') in keep and edge.get('target') in deleted),
                 'OPEN_MODULE_CLOSURE', edge)
    _require(source.parent is None, 'EXTERNAL_PARENT', _ref(source.parent))
    _require(not source.modifiers, 'UNSUPPORTED_OBJECT_MODIFIERS', [m.name for m in source.modifiers])
    errors = _validate_evaluation_references(source, keep, {source, source.data})
    _require(not errors, 'UNSUPPORTED_EVALUATION_REFERENCES', errors)
    for bone_name in sorted(keep):
        bone, pose = source.data.bones[bone_name], source.pose.bones[bone_name]
        _require(bone.parent is None or bone.parent.name in keep,
                 'REMOVED_PARENT', {'bone':bone_name,'parent':bone.parent.name if bone.parent else None})
        for field in ('bbone_custom_handle_start', 'bbone_custom_handle_end'):
            target = getattr(bone, field)
            _require(target is None or target.name in keep,
                     'REMOVED_BBONE_HANDLE', {'bone':bone_name,'field':field,'target':target.name if target else None})
        target = pose.custom_shape_transform
        _require(target is None or target.name in keep,
                 'REMOVED_DISPLAY_TRANSFORM', {'bone':bone_name,'target':target.name if target else None})
    metadata = [{'location':location,'id':_ref(value)} for location,value in _metadata_ids(source, keep)]
    text_refs = [row for row in metadata if row['id']['type'] == 'Text']
    _require(not text_refs, 'SOURCE_TEXT_REFERENCE', text_refs)
    return selected, keep, deleted, metadata


def _detach_animation(owner):
    ad = owner.animation_data
    report = {'action':_ref(ad.action) if ad else None,'nla_tracks':len(ad.nla_tracks) if ad else 0}
    if ad:
        ad.action = None
        for track in list(ad.nla_tracks):
            ad.nla_tracks.remove(track)
    return report


def _remap_constraints(rig, keep, mapping):
    count = 0
    for _, constraints in _constraints(rig, keep):
        for c in constraints:
            for prop in c.bl_rna.properties:
                if prop.type != 'POINTER' or prop.identifier == 'rna_type':
                    continue
                value = getattr(c, prop.identifier)
                if isinstance(value, bpy.types.ID) and value in mapping:
                    _require(not prop.is_readonly, 'READONLY_SELF_REFERENCE',
                             {'constraint':c.name,'field':prop.identifier})
                    setattr(c, prop.identifier, mapping[value])
                    count += 1
            for item in getattr(c, 'targets', ()):
                if item.target in mapping:
                    item.target = mapping[item.target]
                    count += 1
    return count


def _remap_drivers(rig, keep, mapping):
    removed, remapped = [], 0
    for domain, _, ad in _driver_sets(rig):
        for curve in list(ad.drivers):
            if not _kept_curve(curve, keep):
                removed.append({'owner':domain,'path':curve.data_path,'index':curve.array_index})
                ad.drivers.remove(curve)
                continue
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if target.id in mapping:
                        target.id = mapping[target.id]
                        remapped += 1
    return removed, remapped


def _counts(rig):
    result = {'bones':len(rig.data.bones),'deform_bones':sum(b.use_deform for b in rig.data.bones),
              'pose_constraints':sum(len(p.constraints) for p in rig.pose.bones),
              'object_constraints':len(rig.constraints),
              'custom_shapes':sum(p.custom_shape is not None for p in rig.pose.bones),
              'constraint_types':dict(Counter(c.type for p in rig.pose.bones for c in p.constraints)),
              'drivers':{}, 'actions_unbound':True, 'nla_unbound':True}
    for domain, _, ad in _driver_sets(rig):
        result['drivers'][domain] = len(ad.drivers)
        result['actions_unbound'] &= ad.action is None
        result['nla_unbound'] &= not ad.nla_tracks
    return result


def _source_witness(source):
    return {'data_pointer':source.data.as_pointer(), 'bone_names':tuple(source.data.bones.keys()),
            'counts':_counts(source), 'actions':[(domain,ad.action.as_pointer() if ad.action else None,
                                               tuple(track.name for track in ad.nla_tracks))
                                              for domain,_,ad in _driver_sets(source)]}


def extract_template(source, plan, enabled_modules, collection, name, *, owned_ids=None):
    """Return {object, report}; raise a typed error and discard only own partial IDs.

    Preconditions are checked before copying. Caller owns file saving and CLI
    source/resource protection. The source object/data/Action/NLA is never edited.
    """
    from .module_configuration import _own
    if owned_ids is not None and not isinstance(owned_ids, list):
        raise ValueError('owned_ids须为调用者的可写list或None')
    selected, keep, deleted, metadata = _preflight(source, plan, enabled_modules, collection, name)
    source_before = _source_witness(source)
    old_active = bpy.context.view_layer.objects.active
    old_selection = list(bpy.context.selected_objects)
    result, data = None, None
    try:
        result = _own(owned_ids, source.copy())
        data = _own(owned_ids, source.data.copy())
        result.data = data
        result.name = name
        data.name = name + '_骨架数据'
        _require(result.name == name, 'TARGET_NAME_CHANGED', result.name)
        collection.objects.link(result)
        for owner, original in ((result, source), (data, source.data)):
            _require(owner.animation_data is None or original.animation_data is None or
                     owner.animation_data.as_pointer() != original.animation_data.as_pointer(),
                     'SHARED_ANIMATION_DATA', owner.name)
        detached = {'object':_detach_animation(result),'data':_detach_animation(data)}
        mapping = {source:result, source.data:data}
        removed_drivers, driver_remaps = _remap_drivers(result, keep, mapping)
        constraint_remaps = _remap_constraints(result, keep, mapping)
        display_remaps = 0
        for bone_name in sorted(keep):
            original = source.pose.bones[bone_name].custom_shape_transform
            result.pose.bones[bone_name].custom_shape_transform = result.pose.bones[original.name] if original else None
            display_remaps += int(original is not None)
            for field in ('bbone_custom_handle_start', 'bbone_custom_handle_end'):
                original_handle = getattr(source.data.bones[bone_name], field)
                setattr(data.bones[bone_name], field, data.bones[original_handle.name] if original_handle else None)
        for selected_object in list(bpy.context.selected_objects):
            selected_object.select_set(False)
        result.hide_viewport = False
        result.hide_select = False
        result.hide_set(False)
        result.select_set(True)
        bpy.context.view_layer.objects.active = result
        _require(bpy.ops.object.mode_set.poll(), 'EDIT_MODE_UNAVAILABLE', result.name)
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            for bone_name in sorted(deleted):
                data.edit_bones.remove(data.edit_bones[bone_name])
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.context.view_layer.update()
        errors = _validate_evaluation_references(result, keep, {result, data})
        _require(not errors, 'EXTRACTED_REFERENCE_FAILURE', errors)
        _require(set(data.bones.keys()) == keep, 'EXTRACTED_BONE_SET_FAILURE', 'Native edit removal disagreed with keep set')
        handle_errors = []
        for bone_name in sorted(keep):
            p = result.pose.bones[bone_name]
            if p.custom_shape_transform and p.custom_shape_transform.id_data != result:
                handle_errors.append({'bone':bone_name,'field':'custom_shape_transform'})
            for field in ('bbone_custom_handle_start','bbone_custom_handle_end'):
                target = getattr(data.bones[bone_name], field)
                if target and target.id_data != data:
                    handle_errors.append({'bone':bone_name,'field':field})
        _require(not handle_errors, 'EXTRACTED_HANDLE_FAILURE', handle_errors)
        _require(_source_witness(source) == source_before, 'SOURCE_CHANGED', 'Source witness changed during extraction')
        report = {'schema':SCHEMA,'source_object':source.name,'source_data':source.data.name,
                  'template_object':result.name,'template_data':data.name,'collection':collection.name,
                  'enabled_modules':dict(enabled_modules),'selected_closures':selected,
                  'retained_bones':sorted(keep),'removed_bones':sorted(deleted),
                  'source_counts':source_before['counts'],'template_counts':_counts(result),
                  'detached_animation':detached,'removed_driver_outputs':removed_drivers,
                  'constraint_self_references_remapped':constraint_remaps,
                  'driver_self_references_remapped':driver_remaps,
                  'display_transforms_remapped':display_remaps,
                  'source_evaluation_targets_remaining':[], 'deleted_bone_targets':[],
                  'source_witness_unchanged':True,'metadata_id_references_preserved':metadata,
                  'widget_objects_shared':sorted({p.custom_shape.name for p in result.pose.bones if p.custom_shape}),
                  'unimplemented':['new-character fitting','new hair/skirt backend','skinning','face Key authorship',
                                   'portable UI','widget dependency packaging','public redistribution'],
                  'scope':'private native C94 extraction experiment; retained graph only, no cross-character or visual quality certification'}
        result['c94_private_template_schema'] = SCHEMA
        return {'object':result,'report':report}
    except Exception:
        if result is not None and result.mode != 'OBJECT' and bpy.context.view_layer.objects.active == result:
            if bpy.ops.object.mode_set.poll():
                bpy.ops.object.mode_set(mode='OBJECT')
        if result is not None:
            bpy.data.objects.remove(result, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.armatures.remove(data)
        raise
    finally:
        for selected_object in list(bpy.context.selected_objects):
            selected_object.select_set(False)
        for selected_object in old_selection:
            if selected_object.name in bpy.context.view_layer.objects:
                selected_object.select_set(True)
        bpy.context.view_layer.objects.active = old_active
