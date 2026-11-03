"""Declared character-storage observer built on the pinned public inspector.

This is a bounded semantic fingerprint, not a complete .blend serializer. UI,
opaque add-on state, continuous simulation and visual quality are not certified.
Capture observes the current Main; it never reopens, edits or retains IDs.
"""
import copy
import hashlib
import json
from pathlib import Path

import bpy
import inspection

SCHEMA = 'hrig.character-storage.v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def layer_tree(layer):
    return {'collection': inspection.identity(layer.collection),
            'settings': inspection.properties(layer),
            'children': [layer_tree(c) for c in layer.children]}


def typed_fields(item, names):
    """Observe declared scalar/array fields without expanding arbitrary RNA."""
    result = {}
    missing = []
    for name in names:
        if not hasattr(item, name):
            missing.append(name)
        else:
            result[name] = inspection.value(getattr(item, name))
    if missing:
        result['coverage_not_certified'] = {'unavailable_fields': missing}
    return result


def key_owner(item):
    owner = getattr(item, 'user', None)
    if not isinstance(owner, bpy.types.ID):
        owners = [x for collection in (bpy.data.meshes, bpy.data.curves, bpy.data.lattices)
                  for x in collection if getattr(x, 'shape_keys', None) == item]
        if len(owners) != 1:
            raise ValueError('Key owner identity ambiguous')
        owner = owners[0]
    return owner


def constraint_targets(constraints):
    result = []
    for index, constraint in enumerate(constraints):
        prop = constraint.bl_rna.properties.get('targets')
        if prop is None or prop.type != 'COLLECTION':
            continue
        targets = []
        for target_index, target in enumerate(constraint.targets):
            record = {'index': target_index, 'rna_type': target.bl_rna.identifier,
                      'settings': inspection.configuration(target)}
            if target.bl_rna.identifier == 'ConstraintTargetBone':
                record['bone_target'] = typed_fields(target, ('target', 'subtarget', 'weight'))
            else:
                record['coverage_not_certified'] = 'Unknown constraint target RNA; scalar/ID settings only'
            nested = [p.identifier for p in target.bl_rna.properties
                      if p.type == 'COLLECTION' and p.identifier != 'rna_type']
            if nested:
                record['coverage_not_certified_nested_collections'] = nested
            targets.append(record)
        result.append({'index': index, 'name': constraint.name, 'type': constraint.type,
                       'rna_type': constraint.bl_rna.identifier, 'targets': targets})
    return result


def curve_keyframes(curve):
    return {'path': curve.data_path, 'index': curve.array_index,
            'points': [typed_fields(point, (
                'co', 'handle_left', 'handle_right', 'handle_left_type', 'handle_right_type',
                'interpolation', 'easing', 'type', 'amplitude', 'back', 'period',
                'select_control_point', 'select_left_handle', 'select_right_handle'))
                for point in curve.keyframe_points]}


def slot_reference(slot):
    return typed_fields(slot, ('identifier', 'target_id_type', 'handle')) if slot else None


def nla_extras(strip):
    return {'name': strip.name, 'type': strip.type,
            'action': inspection.identity(strip.action) if strip.action else None,
            'action_slot': slot_reference(strip.action_slot),
            'slot_binding': typed_fields(strip, ('action_slot_handle',)),
            'curves': [curve_keyframes(curve) for curve in strip.fcurves],
            'strips': [nla_extras(child) for child in strip.strips]}


def animation_extras(item):
    animation = getattr(item, 'animation_data', None)
    if animation is None:
        return None
    return {'action_slot': slot_reference(animation.action_slot),
            'drivers': [curve_keyframes(curve) for curve in animation.drivers],
            'nla': [{'name': track.name, 'strips': [nla_extras(strip) for strip in track.strips]}
                    for track in animation.nla_tracks]}


def action_keyframes(action):
    return [{'index': layer_index, 'name': layer.name,
             'strips': [{'index': strip_index, 'type': strip.type,
                         'bags': [{'slot_handle': bag.slot_handle,
                                   'curves': [curve_keyframes(curve) for curve in bag.fcurves]}
                                  for bag in getattr(strip, 'channelbags', ())]}
                        for strip_index, strip in enumerate(layer.strips)]}
            for layer_index, layer in enumerate(action.layers)]


def mesh_layer_metadata(mesh):
    def name(layer):
        return layer.name if layer else None
    colors = mesh.color_attributes
    return {'uv': {'layers': [typed_fields(layer, ('name', 'active', 'active_render', 'active_clone'))
                              for layer in mesh.uv_layers],
                   'active': name(mesh.uv_layers.active), 'active_index': mesh.uv_layers.active_index,
                   'clone': name(mesh.uv_layer_clone), 'stencil': name(mesh.uv_layer_stencil),
                   'indices': typed_fields(mesh, ('uv_layer_clone_index', 'uv_layer_stencil_index'))},
            'color': {'layers': [typed_fields(layer, ('name', 'data_type', 'domain')) for layer in colors],
                      'active': name(colors.active), 'active_color': name(colors.active_color),
                      'selection': typed_fields(colors, ('active_index', 'active_color_index',
                          'active_color_name', 'default_color_name', 'render_color_index'))},
            'attributes_active': {'name': name(mesh.attributes.active),
                                  'selection': typed_fields(mesh.attributes, ('active_index',))}}


def nonmesh_key_points(key):
    owner = key_owner(key)
    if isinstance(owner, bpy.types.Mesh):
        return {'owner': inspection.identity(owner), 'coordinates': 'covered by base mesh shape-key hashes'}
    fields = {'ShapeKeyPoint': ('co',),
              'ShapeKeyCurvePoint': ('co', 'radius', 'tilt'),
              'ShapeKeyBezierPoint': ('co', 'handle_left', 'handle_right', 'radius', 'tilt')}
    result = {'owner': inspection.identity(owner), 'blocks': []}
    if not isinstance(owner, (bpy.types.Curve, bpy.types.Lattice)):
        result['coverage_not_certified'] = 'Unsupported non-Mesh Key owner type'
    for block in key.key_blocks:
        hashed = hashlib.sha256()
        types = {}
        unknown = set()
        unavailable = set()
        for point in block.data:
            rna_type = point.bl_rna.identifier
            types[rna_type] = types.get(rna_type, 0) + 1
            if rna_type in fields:
                content = typed_fields(point, fields[rna_type])
                unavailable.update(content.get('coverage_not_certified', {}).get('unavailable_fields', ()))
            else:
                content = inspection.properties(point)
                unknown.add(rna_type)
            payload = {'rna_type': rna_type, 'fields': content}
            hashed.update(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).encode('utf-8'))
            hashed.update(b'\n')
        record = {'name': block.name, 'point_count': len(block.data), 'point_types': types,
                  'observed_fields': {name: list(fields[name]) for name in types if name in fields},
                  'points_sha256': hashed.hexdigest()}
        if unknown or unavailable:
            record['coverage_not_certified'] = {'unknown_point_types': sorted(unknown),
                                               'unavailable_fields': sorted(unavailable)}
        result['blocks'].append(record)
    return result


def extras(item):
    identity = inspection.identity
    properties = inspection.properties
    configuration = inspection.configuration
    result = {}
    if isinstance(item, bpy.types.Object):
        result['stored_settings'] = properties(item, exclude=('matrix_world','matrix_local','matrix_basis','dimensions','bound_box'))
        result['group_definitions'] = [{'name': g.name, 'index': g.index, 'lock_weight': g.lock_weight} for g in item.vertex_groups]
        result['active_vertex_group_index'] = item.vertex_groups.active_index
        result['collections'] = sorted((identity(c) for c in item.users_collection), key=inspection.key)
        result['constraint_targets'] = constraint_targets(item.constraints)
        if item.pose:
            result['pose_solver'] = properties(item.pose)
            result['pose_custom'] = {p.name: configuration(p) for p in item.pose.bones}
            result['pose_colors'] = {p.name: {'palette':p.color.palette,
                'custom':{n:list(getattr(p.color.custom,n)) for n in ('normal','select','active')}} for p in item.pose.bones}
            result['pose_constraint_targets'] = {p.name: constraint_targets(p.constraints) for p in item.pose.bones}
    elif isinstance(item, bpy.types.Armature):
        result['settings'] = properties(item)
        result['bone_settings'] = {b.name: {'settings': configuration(b),
            'color':{'palette':b.color.palette,'custom':{n:list(getattr(b.color.custom,n)) for n in ('normal','select','active')}},
            'head': list(b.head_local), 'tail': list(b.tail_local),
            'parent': b.parent.name if b.parent else None,
            'collections': sorted(c.name for c in b.collections)} for b in item.bones}
        result['bone_collections'] = [{'name':c.name,'settings':configuration(c),
            'parent':c.parent.name if c.parent else None} for c in item.collections_all]
        result['collection_system'] = {
            'active':item.collections.active.name if item.collections.active else None,
            'active_index':item.collections.active_index if item.collections.active else -1,'solo_active':item.collections.is_solo_active,
            'layout':{c.name:{'index':c.index,'children':[x.name for x in c.children],
                'ancestors_visible':c.is_visible_ancestors,'effectively_visible':c.is_visible_effectively} for c in item.collections_all},
            'bone_visibility':{b.name:{'collection_effective_any':any(c.is_visible_effectively for c in b.collections),
                'ungrouped':len(b.collections)==0} for b in item.bones}}
    elif isinstance(item, bpy.types.Key):
        result['settings'] = properties(item)
        result['reference_key'] = item.reference_key.name if item.reference_key else None
        result['blocks'] = [{'name': b.name, 'settings': configuration(b),
            'relative_key': b.relative_key.name if b.relative_key else None,
            'point_count': len(b.data)} for b in item.key_blocks]
        result['point_content'] = nonmesh_key_points(item)
    elif isinstance(item, bpy.types.Collection):
        result['settings'] = properties(item)
        result['objects'] = sorted((identity(o) for o in item.objects), key=inspection.key)
        result['children'] = sorted((identity(c) for c in item.children), key=inspection.key)
    elif isinstance(item, bpy.types.Scene):
        result['settings'] = properties(item)
        result['render'] = configuration(item.render)
        result['view_settings'] = configuration(item.view_settings)
        result['unit_settings'] = configuration(item.unit_settings)
        result['root_collection'] = identity(item.collection)
        result['root_members'] = {'objects': sorted((identity(o) for o in item.collection.objects),key=inspection.key),
            'children': sorted((identity(c) for c in item.collection.children),key=inspection.key)}
        result['view_layers'] = [{'name':v.name,'settings':properties(v),'tree':layer_tree(v.layer_collection),
            'base_hidden': {o.name:o.hide_get(view_layer=v) for o in v.objects}}
            for v in item.view_layers]
        result['compositor'] = inspection.node_content(item.compositing_node_group) if item.compositing_node_group else None
        result['vse_scope'] = 'not_certified' if item.sequence_editor and len(item.sequence_editor.strips) else 'empty'
    elif isinstance(item, bpy.types.Text):
        result['text_sha256'] = hashlib.sha256(item.as_string().encode('utf-8')).hexdigest()
        result['settings'] = properties(item)
    elif isinstance(item, bpy.types.Action):
        result['settings'] = properties(item)
        result['slot_settings'] = [properties(s) for s in item.slots]
        result['layers'] = [{'settings': properties(l), 'strips': [properties(s) for s in l.strips]} for l in item.layers]
        result['keyframe_fields'] = action_keyframes(item)
    if isinstance(item, bpy.types.Mesh):
        result['settings'] = properties(item)
        result['layer_metadata'] = mesh_layer_metadata(item)
    if isinstance(item, bpy.types.Image):
        result['settings'] = properties(item,exclude=('filepath','filepath_raw','file_format'))
        if item.source == 'GENERATED' and not item.packed_files and item.has_data:
            from array import array
            values = array('f', [0.0]) * len(item.pixels)
            item.pixels.foreach_get(values)
            result['unpacked_generated_pixels'] = hashlib.sha256(values.tobytes()).hexdigest()
    animation = animation_extras(item)
    if animation is not None:
        result['animation_fields'] = animation
    return result


def _capture():
    items = list(inspection.all_ids())
    if len(items)>10000:
        raise ValueError('Character observer ID budget exceeded')
    blocks=[]
    for item in items:
        record = inspection.record(item)
        # ID_TAG_EXTRAUSER is internal runtime bookkeeping, cleared by native
        # file writing. Worker reports it separately; real counts remain strict.
        record['native_user_accounting']={'users':item.users}
        record['character_extra'] = extras(item)
        blocks.append(record)
    blocks.sort(key=lambda b:inspection.key({k:b[k] for k in ('type','name','library','owner') if k in b}))
    # Embedded trees are represented in owner node content; their animations
    # must also be observed since they do not all appear in bpy.data.node_groups.
    embedded=[]
    for item in items:
        tree=getattr(item,'node_tree',None)
        if tree is not None:
            record=inspection.record(tree)
            record['character_extra']=extras(tree)
            embedded.append({'owner':inspection.identity(item),'record':record})
    edges=sorted([{'user':inspection.identity(user),'target':inspection.identity(target)}
        for target,users in bpy.data.user_map().items() for user in users
        if target.bl_rna.identifier not in inspection.UI_TYPES and user.bl_rna.identifier not in inspection.UI_TYPES],key=inspection.key)
    return {'schema':SCHEMA,'blocks':blocks,'embedded':embedded,'edges':edges,
            'coverage':{'base':'pinned public inspection.record/mesh_content; no open_mainfile in observer',
                'augmented':['group order/locks','Key settings/relative blocks','bone properties/collections',
                    'BoneColor/PoseBone.color palette and exact custom normal/select/active RGB',
                    'Scene/Collection links and view-layer flags','Text bytes','Action settings','embedded node-tree animation',
                    'Constraint.targets ordered scalar/ID settings and ConstraintTargetBone references/weights',
                    'Mesh UV active/render/clone/stencil and color/attribute active metadata',
                    'Action/driver/NLA keyframe easing/type/amplitude/back/period and selection fields',
                    'NLA action-slot bindings','typed Curve/Lattice Key coordinates/handles/radius/tilt hashes'],
                'not_certified':['UI/workspaces','custom property UI metadata','opaque plugin runtime state','unexpanded custom RNA collections',
                    'unknown constraint target schemas and non-Mesh Key point/owner types (marked locally)',
                    'unavailable declared RNA fields (marked locally)','complete arbitrary RNA or .blend serialization',
                    'VSE strips','continuous simulation/cache correctness','render/visual/animation quality']}}


def capture():
    # Key IDs may share the same type/name/library across different owner Mesh
    # IDs. Preserve their actual names and qualify all references by owner;
    # never rename them merely to fit an observer's identity scheme.
    original=inspection.identity
    original_properties=inspection.properties
    def qualified(item):
        value=original(item)
        if isinstance(item,bpy.types.Key):
            value['owner']=original(key_owner(item))
        return value
    def normalized_properties(item,exclude=()):
        if isinstance(item,bpy.types.ID):
            exclude=(*exclude,'use_extra_user')
        if isinstance(item,bpy.types.Mesh) and item.use_auto_texspace:
            # Both getters ensure/recompute cached automatic bounds; original
            # geometry, keys and both auto flags remain strict. Manual values
            # remain stored authoring state and are never masked.
            exclude=(*exclude,'texspace_location','texspace_size')
        if isinstance(item,bpy.types.PoseBone):
            exclude=(*exclude,'matrix','matrix_channel')
        value=original_properties(item,exclude=exclude)
        for prop in item.bl_rna.properties:
            name=prop.identifier
            if (name in value and prop.type=='STRING' and prop.subtype in {'FILE_PATH','DIR_PATH'}
                    and isinstance(value[name],str) and value[name] and value[name]!='<builtin>'):
                library=getattr(getattr(item,'id_data',None),'library',None)
                value[name]=inspection.path_value(value[name],library)
        return value
    inspection.identity=qualified
    inspection.properties=normalized_properties
    try:
        answer=_capture()
        answer['coverage']['identity']='Key IDs owner-qualified; ID names unchanged; temporary observer-only function binding'
        answer['coverage']['paths']='RNA FILE_PATH/DIR_PATH values compared by resolved absolute target; raw remap reported separately'
        return answer
    finally:
        inspection.identity=original
        inspection.properties=original_properties


def collection_active_indices():
    """Raw runtime observations; null identity has no authoring index."""
    return {a.name:{'active':a.collections.active.name if a.collections.active else None,
                    'raw_active_index':a.collections.active_index,
                    'semantic_index':a.collections.active_index if a.collections.active else -1}
            for a in bpy.data.armatures}


def protected(snapshot, object_name, mesh_name, vertex_indices):
    """Only one declared Mesh deform-weight hash can differ in the base record.

    Caller separately compares exact affected rows, non-target row/locked/group
    hashes from the weight core and an independent native per-vertex oracle.
    """
    result=copy.deepcopy(snapshot)
    found=0
    for block in result['blocks']:
        if block['type']=='Mesh' and block['name']==mesh_name and block['library'] is None:
            block['mesh']['hashes'].pop('deform_weights')
            found+=1
    if found!=1:
        raise ValueError('Weight storage target is ambiguous or missing')
    return result


def summarize(snapshot):
    """Compact full-content hashes; never omit record content before hashing."""
    def block(value):
        ident={k:value[k] for k in ('type','name','library','owner') if k in value}
        return {'identity':ident,'sha256':digest(value),
                'fields':{k:digest(v) for k,v in value.items() if k not in ident}}
    return {'schema':snapshot['schema'],'blocks':[block(x) for x in snapshot['blocks']],
            'embedded':[{'owner':x['owner'],'record':block(x['record'])} for x in snapshot['embedded']],
            'edges_sha256':digest(snapshot['edges']),'full_sha256':digest(snapshot),
            'coverage':snapshot['coverage']}


def diff(before,after,limit=40):
    changes=[]
    def visit(a,b,path):
        if len(changes)>=limit:return
        if type(a) is not type(b):
            changes.append({'path':path,'before':a,'after':b});return
        if isinstance(a,dict):
            for k in sorted(a.keys()|b.keys()):
                if k not in a or k not in b:changes.append({'path':path+'/'+k,'before':a.get(k),'after':b.get(k)})
                else:visit(a[k],b[k],path+'/'+k)
                if len(changes)>=limit:return
        elif isinstance(a,list):
            if len(a)!=len(b):changes.append({'path':path,'before_length':len(a),'after_length':len(b)});return
            for i,(x,y) in enumerate(zip(a,b)):visit(x,y,path+'/'+str(i))
        elif a!=b:changes.append({'path':path,'before':a,'after':b})
    visit(before,after,'')
    return changes
