"""Finite indirect Key closure; only direct-rooted AVERAGE single-input copies."""
import json
from .contract import require, exact, digest


def owner_key(value):
    return value['object'], value['data'], value['key']


def validate_indirect(direct, indirect):
    require(type(indirect) is list and len(indirect) <= 8, 'INDIRECT_KEY_LIST')
    roots = {owner_key(x) for x in direct}
    seen = set()
    for row in indirect:
        exact(row, ('object', 'data', 'key', 'upstream', 'drivers'), 'indirect_owner')
        target = owner_key(row)
        require(target not in roots and target not in seen, 'INDIRECT_KEY_DUPLICATE')
        seen.add(target)
        exact(row['upstream'], ('object', 'data', 'key'), 'indirect_upstream')
        require(owner_key(row['upstream']) in roots and owner_key(row['upstream']) != target,
                'INDIRECT_KEY_ROOT_REQUIRED')
        require(type(row['drivers']) is list and 1 <= len(row['drivers']) <= 16, 'INDIRECT_DRIVER_LIST')
        destinations = set()
        for driver in row['drivers']:
            exact(driver, ('target_key', 'source_key', 'definition'), 'indirect_driver')
            require(type(driver['target_key']) is str and driver['target_key']
                    and driver['target_key'] not in destinations, 'INDIRECT_TARGET_KEY')
            require(type(driver['source_key']) is str and driver['source_key'], 'INDIRECT_SOURCE_KEY')
            destinations.add(driver['target_key'])
            definition = driver['definition']
            require(type(definition) is dict and definition.get('driver_type') == 'AVERAGE'
                    and definition.get('variable_count') == 1
                    and definition.get('variable_type') == 'SINGLE_PROP'
                    and definition.get('variable_name') == 'value'
                    and definition.get('target_count') == 1
                    and definition.get('use_self') is False
                    and definition.get('mute') is False
                    and definition.get('modifiers') == [], 'INDIRECT_COPY_ONLY')
            require(definition.get('path') == 'key_blocks[' + json.dumps(driver['target_key'], ensure_ascii=False) + '].value'
                    and definition.get('index') == 0 and definition.get('sampled_points') == [], 'INDIRECT_DESTINATION_PATH')
            targets = definition.get('targets')
            require(type(targets) is list and len(targets) == 1, 'INDIRECT_TARGET_BINDING')
            target = targets[0]
            require(target.get('id') == {'type':'Key','name':row['upstream']['key'],'library':None}
                    and target.get('id_type') == 'KEY' and target.get('use_fallback_value') is False
                    and target.get('data_path') == 'key_blocks[' + json.dumps(driver['source_key'], ensure_ascii=False) + '].value',
                    'INDIRECT_TARGET_BINDING')
    return True


def validate_binding(binding, roles):
    exact(binding, ('schema', 'source', 'source_profile_sha256', 'catalog_sha256',
                    'inherited_roles', 'inherited_roles_sha256', 'owner_signatures', 'source_profile', 'approved_key_edits'), 'facial_binding')
    require(binding['schema'] == 'hrig.facial-source-binding.v2', 'FACIAL_BINDING_SCHEMA')
    inherited = binding['inherited_roles']
    require(type(inherited) is list and 1 <= len(inherited) <= 189
            and roles[:len(inherited)] == inherited
            and digest(inherited) == binding['inherited_roles_sha256'], 'INHERITED_ROLES_CHANGED')
    exact(binding['source'], ('file', 'expected_sha256'), 'facial_source')
    for value in (binding['source']['expected_sha256'], binding['source_profile_sha256'], binding['catalog_sha256']):
        require(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value), 'FACIAL_BINDING_SHA')
    require(type(binding['owner_signatures']) is list and len(binding['owner_signatures']) == 6, 'FACIAL_OWNER_SIGNATURES')

    exact(binding['source_profile'], ('file', 'expected_sha256'), 'facial_source_profile')
    require(binding['source_profile']['expected_sha256'] == binding['source_profile_sha256'], 'FACIAL_SOURCE_PROFILE_SHA')
    require(type(binding['approved_key_edits']) is list and len(binding['approved_key_edits']) <= 16, 'FACIAL_APPROVED_KEYS')
