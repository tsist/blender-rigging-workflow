"""Pure builder: original source profile + actual inventory -> strict D3 profile."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from .catalog import load, file_sha256
from .contract import require
from .signatures import copy_definition


def catalog_roles(data):
    roles = []
    for c in data['controls']:
        bounds = list(c['raw_bounds'])
        if c['normalized_bounds'][0] == 0:
            bounds[0] = 0.0
        if c['normalized_bounds'][1] == 0:
            bounds[1] = 0.0
        roles.append({'role_id': c['role_id'], 'bone': c['bone'], 'parent': c['parent'],
            'rotation_mode': c['rotation_mode'], 'kind': 'location_delta', 'index': c['index'],
            'axis': [float(j == c['index']) for j in range(3)], 'property': None,
            'bounds': bounds, 'requires': [deepcopy(data['mode_guard'])], 'label': c['label']})
    return roles


def build(profile_path, inventory_path, *, source_identity=None,
          source_profile_sha256=None, approved_key_edits=None):
    from native_controls.contract import decode as legacy_decode
    from .control_core.contract import validate, digest
    data = load()
    raw = Path(profile_path).read_bytes()
    base_sha = hashlib.sha256(raw).hexdigest()
    require(base_sha == (data['source_profile_sha256'] if source_profile_sha256 is None else source_profile_sha256), 'D3_SOURCE_PROFILE_SHA')
    require(base_sha == data['source_profile_sha256'] or source_identity is not None,
            'D3_CHANGED_PROFILE_REQUIRES_SOURCE_IDENTITY')
    base = legacy_decode(raw)
    require(len(base['roles']) + len(data['controls']) <= 256 and base['character_id'] == 'anion'
            and base['rig']['object'] == data['rig']['name'], 'D3_SOURCE_PROFILE')
    source = {'file':data['source']['file'],'expected_sha256':data['source']['sha256']} if source_identity is None else deepcopy(source_identity)
    require(type(source) is dict and set(source) == {'file','expected_sha256'}
            and Path(source['file']).is_absolute()
            and hashlib.sha256(Path(source['file']).read_bytes()).hexdigest() == source['expected_sha256'], 'D3_SOURCE_IDENTITY')
    raw_inventory = Path(inventory_path).read_bytes()
    require(hashlib.sha256(raw_inventory).hexdigest() == data['inventory']['sha256'], 'D3_INVENTORY_SHA')
    inventory = json.loads(raw_inventory)
    require(inventory['source'] == data['source'] and inventory['runtime'] == data['runtime'], 'D3_INVENTORY_SOURCE')
    rig = next((r for r in inventory['armature_objects'] if r['identity'] == data['rig']), None)
    require(rig is not None, 'D3_RIG_INVENTORY')
    bones = {b['name']: b for b in rig['bones']}
    profile = deepcopy(base)
    profile.update(schema='hrig.facial-control-profile.v3', profile_id='anion.facial-controls.v121', revision=base['revision'] + 1)
    for c in data['controls']:
        b = bones[c['bone']]
        require(b['parent'] == c['parent'] and b['rotation_mode'] == c['rotation_mode']
                and b['location'][c['index']] == c['baseline'] == 0.0, 'D3_BONE_SOURCE_BINDING', c['id'])
    profile['roles'].extend(catalog_roles(data))
    tiger = next(m for m in inventory['meshes'] if m['identity']['name'] == '千早爱音- 虎牙')
    upstream = next(o for o in base['rig']['output_keys'] if o['data'] == '千早爱音- 赋权躯干网格')
    profile['rig']['indirect_output_keys'] = [{'object': tiger['real_object_owners'][0]['name'],
        'data': tiger['identity']['name'], 'key': tiger['keys']['identity']['name'],
        'upstream': deepcopy(upstream), 'drivers': [
            {'target_key': name, 'source_key': name,
             'definition': copy_definition(next(f for f in tiger['keys']['animation']['drivers']
                 if f['path'] == 'key_blocks[' + json.dumps(name, ensure_ascii=False) + '].value'))}
            for name in ('哈？', '大笑')]}]
    require(profile['rig']['indirect_output_keys'] == data['indirect_output_keys'], 'D3_INDIRECT_SOURCE_DRIFT')
    profile['facial_binding'] = {'schema': 'hrig.facial-source-binding.v2',
        'source': source, 'source_profile':{'file':str(Path(profile_path).resolve()),'expected_sha256':base_sha},
        'source_profile_sha256': base_sha, 'catalog_sha256': file_sha256(),
        'inherited_roles': deepcopy(base['roles']), 'inherited_roles_sha256': digest(base['roles']),
        'owner_signatures': deepcopy(data['face_owners']), 'approved_key_edits':deepcopy(approved_key_edits or [])}
    return validate(profile)
