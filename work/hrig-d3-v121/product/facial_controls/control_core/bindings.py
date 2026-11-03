"""Runtime checks for frozen face geometry and direct-rooted indirect Key copies."""

from hrig_paths import project_path
import hashlib
import json
from pathlib import Path
import bpy
import numpy as np
from .contract import require
from ..catalog import load, file_sha256
from ..signatures import copy_definition, POINT_FIELDS, TARGET_FIELDS

SOURCE_PROFILE = Path(project_path('runtime/audit/rigging-stage-C-completion-20261101-v094/axis-v2-jobs/0c398a427b994f4c84add758e14ab66c/output-profile.json'))


def identity(item):
    return None if item is None else {'type': item.bl_rna.identifier, 'name': item.name,
                                      'library': None if item.library is None else item.library.filepath}


def _coords(points):
    values = np.empty(len(points) * 3, dtype='<f4')
    points.foreach_get('co', values)
    require(bool(np.isfinite(values).all()), 'FACIAL_COORDINATES_NONFINITE')
    return hashlib.sha256(values.tobytes()).hexdigest()


def _owner(row):
    obj = bpy.data.objects.get(row['object'])
    require(obj is not None and obj.type == 'MESH' and obj.library is None
            and obj.override_library is None and obj.data.library is None
            and obj.data.override_library is None and obj.data.name == row['data'], 'FACIAL_OWNER_IDENTITY')
    key = obj.data.shape_keys
    require(key is not None and key.name == row['key'] and key.library is None
            and key.override_library is None, 'FACIAL_KEY_IDENTITY')
    require([o for o in bpy.data.objects if o.type == 'MESH' and o.data == obj.data] == [obj]
            and obj.data.users == 1 + int(obj.data.use_fake_user)
            and key.users == 1 + int(key.use_fake_user), 'FACIAL_SHARED_OWNER')
    return obj, key


def validate_native_signatures(profile):
    b = profile['facial_binding']; catalog = load()
    require(file_sha256() == b['catalog_sha256'], 'FACIAL_CATALOG_DRIFT')
    source_descriptor = b['source_profile']
    require(hashlib.sha256(Path(source_descriptor['file']).read_bytes()).hexdigest() == b['source_profile_sha256']
            == source_descriptor['expected_sha256'], 'FACIAL_SOURCE_PROFILE_DRIFT')
    source = json.loads(Path(source_descriptor['file']).read_bytes())
    inherited_count = len(source['roles'])
    require(source['roles'] == b['inherited_roles'] == profile['roles'][:inherited_count], 'INHERITED_ROLES_CHANGED')
    require(source['character_id'] == profile['character_id']
            and source['rig'] == {k: v for k, v in profile['rig'].items() if k != 'indirect_output_keys'}
            and source['runtime'] == profile['runtime']
            and source['rig']['object'] == catalog['rig']['name'], 'FACIAL_SOURCE_BINDING_CHANGED')
    require(b['owner_signatures'] == catalog['face_owners'], 'FACIAL_OWNER_SIGNATURES_CHANGED')
    require(profile['rig']['indirect_output_keys'] == catalog['indirect_output_keys'], 'FACIAL_INDIRECT_DECLARATION_DRIFT')
    from ..profile import catalog_roles
    require(profile['roles'][inherited_count:] == catalog_roles(catalog), 'FACIAL_ROLE_MAPPING_DRIFT')
    require(hashlib.sha256(Path(b['source']['file']).read_bytes()).hexdigest() == b['source']['expected_sha256'], 'FACIAL_SOURCE_SHA')
    from ..key_receipts import load_approvals
    approved = load_approvals(b, catalog)
    for row in b['owner_signatures']:
        obj, key = _owner(row); mesh = obj.data
        require(len(mesh.vertices) == row['vertex_count'] and _coords(mesh.vertices) == row['raw_coordinate_sha256_le_f32'], 'FACIAL_RAW_MESH_DRIFT')
        topology = {'edges': [list(e.vertices) for e in mesh.edges], 'loops': [l.vertex_index for l in mesh.loops],
                    'polygons': [[p.loop_start, p.loop_total, p.material_index] for p in mesh.polygons]}
        h = hashlib.sha256(json.dumps(topology, separators=(',', ':')).encode()).hexdigest()
        require(h == row['topology_material_digest_intake_v1'], 'FACIAL_TOPOLOGY_DRIFT')
        owner_approvals = [a for a in approved if a['owner']['data'] == row['data']]
        extensions = [a for a in owner_approvals if a['mode'] == 'append']
        actual = list(key.key_blocks)
        require(key.use_relative and key.reference_key.name == row['reference']
                and [x.name for x in actual] == [x['name'] for x in row['keys']] + [a['target'] for a in extensions], 'FACIAL_KEY_ROSTER_DRIFT')
        by_target = {a['target']:a for a in owner_approvals}
        for block, declared in zip(actual, row['keys']):
            require(block.name == declared['name'] and block.relative_key.name == declared['relative_key']
                    and block.vertex_group == declared['mask'], 'FACIAL_KEY_DEFINITION_DRIFT', block.name)
            if block.name in by_target:
                _approved_key(obj, block, by_target[block.name], key.reference_key)
            else:
                require(_coords(block.data) == declared['coordinate_sha256_le_f32'], 'FACIAL_KEY_COORDINATES_DRIFT', block.name)
        for approval in extensions:
            _approved_key(obj, key.key_blocks[approval['target']], approval, key.reference_key)
    for role in profile['roles'][inherited_count:]:
        pb = bpy.data.objects[profile['rig']['object']].pose.bones.get(role['bone'])
        require(pb is not None and (pb.parent.name if pb.parent else None) == role['parent']
                and pb.rotation_mode == role['rotation_mode'], 'FACIAL_LIVE_ROLE_BINDING')


def _approved_key(obj, block, approval, reference):
    import inspection as I
    receipt = approval['receipt']
    require(_coords(block.data) == receipt['target_sha256'], 'APPROVED_KEY_COORDINATE_DRIFT')
    values = np.empty(len(block.data)*3,dtype='<f4');block.data.foreach_get('co',values);values=values.reshape((-1,3))
    ref = np.empty(len(reference.data)*3,dtype='<f4');reference.data.foreach_get('co',ref);ref=ref.reshape((-1,3))
    ids=np.asarray(receipt['region_vertices'],dtype=np.int32);outside=np.ones(len(values),dtype=bool);outside[ids]=False
    require(hashlib.sha256(values[outside].tobytes()).hexdigest() == receipt['outside_region_sha256'], 'APPROVED_KEY_NON_REGION_DRIFT')
    if approval['mode'] == 'append':
        require(values[outside].tobytes() == ref[outside].tobytes(), 'APPROVED_KEY_OUTSIDE_BASIS')
    lo=np.asarray((-.12,-.12,1.39));hi=np.asarray((.12,.04,1.62))
    require(bool(((ref[ids]>=lo)&(ref[ids]<=hi)).all()), 'APPROVED_KEY_REGION_NOT_FACE')
    if obj.name == '千早爱音- 赋权躯干网格':
        group=obj.vertex_groups.get('形变-头颅');require(group is not None and group.index==175,'APPROVED_KEY_HEAD_MASK')
        require(all(any(g.group==175 and g.weight>0 for g in obj.data.vertices[int(n)].groups) for n in ids), 'APPROVED_KEY_HEAD_MEMBERSHIP')
    definition={'name':block.name,'settings':I.configuration(block),'relative_key':block.relative_key.name,'point_count':len(block.data)}
    expected=json.loads(json.dumps(receipt['target_definition']))
    # The approval binds coordinate/metadata, not the current animated scalar.
    definition['settings'].pop('value',None);expected['settings'].pop('value',None)
    require(definition == expected, 'APPROVED_KEY_METADATA_DRIFT')


def _runtime_definition(fc):
    d = fc.driver
    raw = {'path': fc.data_path, 'index': fc.array_index, 'mute': fc.mute,
           'extrapolation': fc.extrapolation, 'keyframes': [],
           'sampled_points': [list(p.co) for p in fc.sampled_points],
           'modifiers': [m.type for m in fc.modifiers],
           'driver': {'type': d.type, 'expression': d.expression, 'use_self': d.use_self, 'variables': []}}
    for point in fc.keyframe_points:
        raw['keyframes'].append({k: list(getattr(point, k)) if k in ('co', 'handle_left', 'handle_right')
                                 else getattr(point, k) for k in POINT_FIELDS})
    for variable in d.variables:
        targets = []
        for t in variable.targets:
            targets.append({k: identity(t.id) if k == 'id' else getattr(t, k) for k in TARGET_FIELDS})
        raw['driver']['variables'].append({'name': variable.name, 'type': variable.type, 'targets': targets})
    return copy_definition(raw)


def validate_native_indirect(core):
    from .indirect_contract import owner_key
    # Only the direct output list has already passed the unchanged strong rig guard.
    roots = {owner_key(row): key for row, key in zip(core.profile['rig']['output_keys'], core._direct_output_keys)}
    for row in core.profile['rig']['indirect_output_keys']:
        obj, key = _owner(row)
        source_obj, source = _owner(row['upstream'])
        require(source in core._direct_output_keys and roots.get(owner_key(row['upstream'])) == source,
                'INDIRECT_UNVALIDATED_ROOT')
        require(key != source and key.animation_data is not None
                and key.animation_data.action is None and not key.animation_data.nla_tracks, 'INDIRECT_KEY_WRITER')
        curves = list(key.animation_data.drivers)
        require(len(curves) == len(row['drivers']), 'INDIRECT_DRIVER_ROSTER')
        for declared in row['drivers']:
            block = key.key_blocks.get(declared['target_key']); upstream = source.key_blocks.get(declared['source_key'])
            require(block is not None and upstream is not None, 'INDIRECT_KEYBLOCK_MISSING')
            matches = [f for f in curves if f.data_path == block.path_from_id('value') and f.array_index == 0]
            require(len(matches) == 1, 'INDIRECT_DRIVER_DESTINATION')
            f = matches[0]; d = f.driver
            require(f.is_valid and d.is_valid and not f.mute and d.type == 'AVERAGE'
                    and not d.use_self and len(d.variables) == 1 and len(f.modifiers) == 0, 'INDIRECT_COPY_ONLY')
            v = d.variables[0]
            require(v.name == 'value' and v.type == 'SINGLE_PROP' and len(v.targets) == 1, 'INDIRECT_COPY_ONLY')
            target = v.targets[0]
            require(target.id == source and target.id_type == 'KEY'
                    and target.data_path == upstream.path_from_id('value')
                    and not target.use_fallback_value, 'INDIRECT_TARGET_BINDING')
            require(_runtime_definition(f) == declared['definition'], 'INDIRECT_DRIVER_SIGNATURE_DRIFT')
        if key not in core.output_keys:
            core.output_keys.append(key)
