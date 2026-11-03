"""Exact six-leaf mask plus native selected ID-property UI/alias observation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import bpy
import inspection

_spec = importlib.util.spec_from_file_location('_hrig_settings_base', Path(__file__).with_name('dynamics_state.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)


def capture(selection):
    result = _base.capture()
    rig = bpy.data.objects[selection['rig']]
    pb = rig.pose.bones[selection['control_bone']]
    properties = []
    for key in selection['properties']:
        relative = '[' + json.dumps(key, ensure_ascii=False) + ']'
        path = pb.path_from_id() + relative
        alias = 'objects[' + json.dumps(rig.name, ensure_ascii=False) + '].' + path
        properties.append({'key': key, 'type': type(pb[key]).__name__, 'value': pb[key],
            'path': path, 'owner': inspection.identity(rig), 'bone': pb.name,
            'UI': pb.id_properties_ui(key).as_dict(),
            'overridable': pb.is_property_overridable_library(relative),
            'aliases': [{'root': inspection.identity(rig), 'path': path,
                         'type': type(rig.path_resolve(path)).__name__, 'value': rig.path_resolve(path)},
                        {'root': inspection.identity(bpy.context.scene), 'path': alias,
                         'type': type(bpy.context.scene.path_resolve(alias)).__name__,
                         'value': bpy.context.scene.path_resolve(alias)}]})
    effects = []
    for row in selection['effects']:
        obj = bpy.data.objects[row['object']]
        modifier = obj.modifiers[row['index']]
        value = getattr(modifier, row['field'])
        effects.append({'selection': copy.deepcopy(row), 'native_type': modifier.type,
                        'native_name': modifier.name, 'type': type(value).__name__, 'value': value})
    result['existing_controls'] = {'properties': properties, 'effects': effects}
    result['coverage']['augmented'].append('selected two ID-property UI/as_dict/type/overridable and explicitly enumerated canonical+Scene value aliases; no other custom UI certification')
    return result


def protected(snapshot, selection, values):
    if type(values) is not list or len(values) != 2 or any(type(v) is not int or v not in (0, 1) for v in values):
        raise ValueError('Two exact native int0/1 values required')
    result = copy.deepcopy(snapshot)
    rig_rows = [b for b in result['blocks'] if b['type'] == 'Object' and b['name'] == selection['rig'] and b['library'] is None]
    if len(rig_rows) != 1:
        raise ValueError('Exactly one original rig record required')
    leaves = rig_rows[0]['character_extra']['pose_custom'][selection['control_bone']]['custom_properties']
    for key, value in zip(selection['properties'], values):
        if type(leaves[key]) is not int or leaves[key] != value:
            raise ValueError('Actual selected original int differs')
        del leaves[key]
    for row in selection['effects']:
        blocks = [b for b in result['blocks'] if b['type'] == 'Object' and b['name'] == row['object'] and b['library'] is None]
        ids = [r for r in result['modifier_identities'] if r['object'] == {'type': 'Object', 'name': row['object'], 'library': None}]
        if len(blocks) != 1 or len(ids) != 1 or ids[0]['modifiers'][row['index']] != {'index': row['index'], 'name': row['modifier'], 'type': row['type']}:
            raise ValueError('Exact original modifier identity differs')
        modifier = blocks[0]['object']['modifiers'][row['index']]
        value = values[selection['properties'].index(row['control_key'])] > 0
        if modifier['name'] != row['modifier'] or type(modifier[row['field']]) is not bool or modifier[row['field']] is not value:
            raise ValueError('Actual original driven bool differs')
        del modifier[row['field']]
    audit = result['existing_controls']
    if len(audit['properties']) != 2 or len(audit['effects']) != 4:
        raise ValueError('Native selected audit coverage differs')
    for prop, key, value in zip(audit['properties'], selection['properties'], values):
        if prop['key'] != key or prop['type'] != 'int' or type(prop['value']) is not int or prop['value'] != value:
            raise ValueError('Typed custom-property audit differs')
        del prop['value']
        if len(prop['aliases']) != 2:
            raise ValueError('Explicit alias coverage differs')
        for alias in prop['aliases']:
            if alias['type'] != 'int' or type(alias['value']) is not int or alias['value'] != value:
                raise ValueError('Typed native alias value differs')
            del alias['value']
    for item, row in zip(audit['effects'], selection['effects']):
        value = values[selection['properties'].index(row['control_key'])] > 0
        if item['selection'] != row or item['native_type'] != row['type'] or item['native_name'] != row['modifier'] or item['type'] != 'bool' or type(item['value']) is not bool or item['value'] is not value:
            raise ValueError('Typed native effect audit differs')
        del item['value']
    return result


def compare_patch(before, after, selection, first_values, second_values):
    first, second = protected(before, selection, first_values), protected(after, selection, second_values)
    return {'ok': first == second, 'changes': _base.diff(first, second),
            'scope': 'Exactly two existing int values/four driven bool leaves and enumerated same-value aliases; all other declared author observation strict'}


def summarize(snapshot):
    result = _base.summarize(snapshot)
    # Full digest binds all augmented fields, not only the legacy block hashes.
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    result['settings_full_sha256'] = hashlib.sha256(raw).hexdigest()
    return result


diff = _base.diff
