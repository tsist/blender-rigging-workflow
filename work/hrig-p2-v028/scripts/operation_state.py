"""Independent native snapshots for the deliberately closed P2 editing domain.

Control-only scenes have Armature/Empty objects. Owned-limb scenes may also
contain their one rigid witness. No libraries, external resources or Actions
are admitted here. This is not a preservation oracle for arbitrary characters.
"""
import copy
import math

import bpy
from mathutils import Color, Euler, Matrix, Quaternion, Vector


def plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Nonfinite state')
        return value
    if isinstance(value, bpy.types.ID):
        return {'id_type': value.bl_rna.identifier, 'name': value.name_full,
                'library': value.library.filepath if value.library else None}
    if isinstance(value, (Color, Euler, Matrix, Quaternion, Vector)):
        return [plain(value[i]) for i in range(len(value))]
    if hasattr(value, 'to_dict'):
        return {str(k): plain(v) for k, v in value.to_dict().items()}
    if hasattr(value, 'items'):
        return {str(k): plain(v) for k, v in value.items()}
    if hasattr(value, '__iter__'):
        return [plain(x) for x in value]
    if hasattr(value, 'path_from_id'):
        return {'rna_type': value.bl_rna.identifier, 'path': value.path_from_id()}
    raise ValueError('Unsupported stored value: ' + type(value).__name__)


def properties(owner, omit=()):
    answer = {}
    for prop in owner.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in omit or name == 'tag' or prop.type == 'COLLECTION':
            continue
        answer[name] = plain(getattr(owner, name))
    if hasattr(owner, 'keys'):
        try:
            keys = list(owner.keys())
        except TypeError:  # RNA structs may expose keys() but prohibit IDProperties.
            keys = []
        answer['custom_properties'] = {k: plain(owner[k]) for k in keys}
    return answer


def animation(owner):
    ad = owner.animation_data
    if not ad:
        return None
    if ad.action or len(ad.nla_tracks):
        raise ValueError('Actions/NLA outside the closed editing adapter')
    return {'properties': properties(ad), 'drivers': [
        {'curve': properties(fc), 'driver': properties(fc.driver),
         'variables': [{'properties': properties(v),
                        'targets': [properties(t) for t in v.targets]}
                       for v in fc.driver.variables],
         'modifiers': [properties(m) for m in fc.modifiers]}
        for fc in ad.drivers]}


def gate(limb_manifest=None):
    if bpy.data.libraries or bpy.data.actions or bpy.data.texts:
        raise ValueError('Libraries, Actions or embedded Text IDs outside closed P2 scope')
    for field in ('images', 'sounds', 'fonts', 'movieclips', 'cache_files',
                  'volumes', 'curves', 'hair_curves', 'materials', 'node_groups',
                  'textures', 'cameras', 'lights', 'worlds'):
        if len(getattr(bpy.data, field, ())):
            raise ValueError('Unsupported data collection: ' + field)
    if len(bpy.data.scenes) != 1 or len(bpy.context.scene.view_layers) != 1:
        raise ValueError('Exactly one Scene and one View Layer required')
    scene = bpy.context.scene
    # In 5.2.1 factory settings already have an empty SequenceEditor and the
    # deprecated use_nodes flag is true even with no compositor tree. Inspect
    # actual strips/tree identities instead of treating those flags as content.
    if ((scene.sequence_editor is not None and len(scene.sequence_editor.strips) > 0)
            or getattr(scene, 'node_tree', None) or getattr(scene, 'compositing_node_group', None)):
        raise ValueError('Sequencer strips and scene compositor outside closed scope')
    if scene.animation_data:
        raise ValueError('Scene animation outside the closed editing scope')
    if len(bpy.data.objects) > 32 or sum(len(a.bones) for a in bpy.data.armatures) > 256:
        raise ValueError('Closed adapter object/bone budget exceeded')
    allowed = {'ARMATURE', 'EMPTY'} if limb_manifest is None else {'ARMATURE', 'MESH'}
    if any(o.type not in allowed for o in bpy.data.objects):
        raise ValueError('Objects outside the closed current-pose editing scope')
    if limb_manifest and set(bpy.data.objects.keys()) != {limb_manifest['rig'], limb_manifest['mesh']}:
        raise ValueError('Owned limb file must contain exactly its declared rig and witness')
    for collection in (bpy.data.objects, bpy.data.armatures, bpy.data.meshes):
        for data in collection:
            if data.library or not data.is_editable or data.users == 0:
                raise ValueError('Linked, noneditable or orphan ID outside save-preservation domain')
    for ob in bpy.data.objects:
        animation(ob)
        if ob.type == 'MESH' and (ob.data.shape_keys or len(ob.data.vertices) != 12):
            raise ValueError('Only the owned twelve-vertex witness mesh is supported')


def snapshot():
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    objects, observed = {}, {}
    for ob in bpy.data.objects:
        # dimensions is writable RNA backed by evaluated bounds, not an
        # independent stored transform. Moving controls changes Armature bounds.
        item = {'properties': properties(ob, ('matrix_world', 'matrix_local', 'matrix_basis', 'dimensions')),
                'animation': animation(ob),
                'constraints': [properties(c) for c in ob.constraints],
                'modifiers': [properties(m) for m in ob.modifiers],
                'groups': [properties(g) for g in ob.vertex_groups]}
        ev = graph.id_eval_get(ob)
        view = {'world': plain(ev.matrix_world)}
        if ob.type == 'ARMATURE':
            item['pose'] = {p.name: {'properties': properties(p, ('matrix', 'matrix_basis')),
                'constraints': [properties(c) for c in p.constraints]} for p in ob.pose.bones}
            item['solver'] = ob.pose.ik_solver
            view['bones'] = {p.name: plain(ev.matrix_world @ p.matrix) for p in ev.pose.bones}
        elif ob.type == 'MESH':
            view['vertices'] = [plain(ev.matrix_world @ v.co) for v in ev.data.vertices]
        objects[ob.name] = item
        observed[ob.name] = view
    armatures = {a.name: {'properties': properties(a),
        'bones': {b.name: {'properties': properties(b), 'matrix': plain(b.matrix_local),
                          'parent': b.parent.name if b.parent else None,
                          'collections': sorted(c.name for c in b.collections)} for b in a.bones},
        'collections': [properties(c) for c in a.collections_all]} for a in bpy.data.armatures}
    meshes = {m.name: {'properties': properties(m),
        'vertices': [{'co': plain(v.co), 'groups': [[g.group, g.weight] for g in v.groups]} for v in m.vertices],
        'edges': [plain(e.vertices) for e in m.edges],
        'faces': [plain(p.vertices) for p in m.polygons],
        'attributes': [(a.name, a.data_type, a.domain) for a in m.attributes]} for m in bpy.data.meshes}
    return {'stored': {'objects': objects, 'armatures': armatures, 'meshes': meshes,
            'scene': properties(bpy.context.scene),
            'render': properties(bpy.context.scene.render),
            'collections': {c.name: {'objects': sorted(c.objects.keys()),
                'children': sorted(c.children.keys()), 'properties': properties(c)} for c in bpy.data.collections},
            'scene_objects': sorted(bpy.context.scene.objects.keys())},
            'evaluated': observed}


def protected(state, operation, manifest=None):
    value = copy.deepcopy(state['stored'])
    action = operation['action']
    if action in ('match_world', 'switch_space'):
        target = operation['target']
        bone = value['objects'][target['rig']]['pose'][target['bone']]['properties']
        for channel in ('location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale'):
            bone.pop(channel, None)
        if action == 'switch_space':
            prop = operation['property']
            rig = bpy.data.objects[target['rig']]
            helper = rig.pose.bones[target['bone']].parent.name
            setting = value['objects'][target['rig']]['pose'][prop['bone']]['properties']['custom_properties']
            setting.pop(prop['key'])
            for con in value['objects'][target['rig']]['pose'][helper]['constraints']:
                con.pop('influence', None)
    elif action.startswith('snap_'):
        rig = value['objects'][manifest['rig']]
        allowed = manifest['bones']['FK'] if action == 'snap_ik_to_fk' else [manifest['bones']['target'], manifest['bones']['pole']]
        for name in allowed:
            for channel in ('location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale'):
                rig['pose'][name]['properties'].pop(channel, None)
        rig['pose'][manifest['bones']['settings']]['properties']['custom_properties'].pop('ik_fk')
        for name in manifest['bones']['DEF']:
            # Only the second, selector-driven IK constraint may change.
            # The first FK constraint is permanently fixed at influence 1.
            rig['pose'][name]['constraints'][1].pop('influence', None)
    return value


def equal(a, b, tolerance=2e-6):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isfinite(a) and math.isfinite(b) and abs(a-b) <= tolerance
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k], tolerance) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(equal(x, y, tolerance) for x, y in zip(a, b))
    return type(a) is type(b) and a == b
