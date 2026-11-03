"""Motion authoring protection: one verified Action and one exact binding edge."""
import copy
import importlib.util
from pathlib import Path

import bpy
import inspection
from humanoid_rigging_probe import motion_clips as core

_spec = importlib.util.spec_from_file_location('_hrig_motion_base_state', Path(__file__).with_name('character_state.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
BINDING = ('action', 'action_slot', 'action_slot_handle', 'last_slot_identifier')
NEW_AD_DEFAULTS = {'action_extrapolation': 'HOLD', 'action_blend_type': 'REPLACE',
                   'action_influence': 1.0, 'action_tweak_storage': None,
                   'action_slot_handle_tweak_storage': 0, 'use_nla': True,
                   'use_tweak_mode': False, 'use_pin': False}


def _identity(item):
    result = inspection.identity(item)
    if isinstance(item, bpy.types.Key):
        result['owner'] = inspection.identity(_base.key_owner(item))
    return result


def capture():
    result = _base.capture()
    rows = []
    for item in inspection.all_ids():
        if hasattr(item, 'animation_data'):
            rows.append({'identity': _identity(item), 'embedded_owner': None,
                         'state': core.animation_state(item)})
        tree = getattr(item, 'node_tree', None)
        if tree is not None:
            rows.append({'identity': _identity(tree), 'embedded_owner': _identity(item),
                         'state': core.animation_state(tree)})
    rows.sort(key=inspection.key)
    result['animdata_authoring'] = rows
    result['coverage']['augmented'].append('all Main ID and embedded node-tree writable AnimData settings; exact declared drivers')
    return result


def summarize(snapshot):
    value = _base.summarize(snapshot)
    value['animdata_authoring_sha256'] = _base.digest(snapshot['animdata_authoring'])
    return value


def compare_create(before, after, rig_name, request, verification):
    """Validate the precise new identity/edge before removing allowed deltas."""
    if verification.get('ok') is not True:
        raise ValueError('New Action must first pass independent request reconstruction')
    action = {'type': 'Action', 'name': request['action_name'], 'library': None}
    rig = {'type': 'Object', 'name': rig_name, 'library': None}
    first, second = copy.deepcopy(before), copy.deepcopy(after)
    key = lambda row: {k: row[k] for k in ('type', 'name', 'library', 'owner') if k in row}
    b = {inspection.key(key(row)): row for row in first['blocks']}
    a = {inspection.key(key(row)): row for row in second['blocks']}
    action_key = inspection.key(action)
    if set(a) - set(b) != {action_key} or set(b) - set(a):
        raise ValueError('Exactly one new local Action identity is allowed')
    new = a[action_key]
    if new['native_user_accounting']['users'] != 1:
        raise ValueError('New Action must have exactly its real rig user')
    second['blocks'].remove(new)
    edge = {'user': rig, 'target': action}
    if edge in first['edges'] or second['edges'].count(edge) != 1:
        raise ValueError('Expected exactly one new rig to Action edge')
    second['edges'].remove(edge)
    if first['edges'] != second['edges']:
        raise ValueError('Non-target reference edges changed')
    bo, ao = b[inspection.key(rig)], a[inspection.key(rig)]
    before_ad = next(x for x in first['animdata_authoring'] if x['identity'] == rig and x['embedded_owner'] is None)
    after_ad = next(x for x in second['animdata_authoring'] if x['identity'] == rig and x['embedded_owner'] is None)
    actual = after_ad['state']
    if actual is None or actual['settings']['action'] != action:
        raise ValueError('Verified Action is not the exact target binding')
    for row in second['animdata_authoring'][:]:
        if row['identity'] == action and row['embedded_owner'] is None:
            if row['state'] is not None:
                raise ValueError('New Action may not have its own AnimData')
            second['animdata_authoring'].remove(row)
    if before_ad['state'] is None:
        rest = {k: v for k, v in actual['settings'].items() if k not in BINDING}
        if rest != NEW_AD_DEFAULTS or actual['drivers'] or actual['nla_track_count']:
            raise ValueError('New AnimData contains undeclared non-default settings or consumers')
        if ao.get('animation', {}).get('drivers') or ao.get('animation', {}).get('nla'):
            raise ValueError('New target AnimData contains consumers')
        ao.pop('animation', None)
        ao['character_extra'].pop('animation_fields', None)
        after_ad['state'] = None
    else:
        if before_ad['state']['settings']['action'] is not None or before_ad['state']['nla_track_count']:
            raise ValueError('Before must have no active Action or NLA')
        for field in BINDING:
            actual['settings'][field] = before_ad['state']['settings'][field]
        ao['animation']['action'] = bo['animation']['action']
        ao['animation']['slot'] = bo['animation']['slot']
        ao['character_extra']['animation_fields']['action_slot'] = bo['character_extra']['animation_fields']['action_slot']
    # Ordering may move only the removed new Action; no semantic sorting masks.
    second['animdata_authoring'].sort(key=inspection.key)
    changes = _base.diff(first, second)
    return {'ok': first == second, 'changes': changes,
            'scope': 'one verified new Action, one exact rig binding edge, four AnimData binding/cache fields; all original channels/IDs/drivers/rest/weights remain strict'}


diff = _base.diff
collection_active_indices = _base.collection_active_indices
