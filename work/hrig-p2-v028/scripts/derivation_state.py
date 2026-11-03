"""Precise Action-copy derivation protection; capture is native, comparison pure.

No pose, Key.value, driver, mesh or general user-count mask is introduced.
"""
import copy
import importlib.util
import json
from pathlib import Path

_BASE = None


def _base():
    global _BASE
    if _BASE is None:
        spec = importlib.util.spec_from_file_location('_hrig_derivation_motion_state',
            Path(__file__).with_name('motion_state.py'))
        _BASE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_BASE)
    return _BASE


def capture():
    return _base().capture()


def summarize(value):
    return _base().summarize(value)


def diff(first, second):
    changes = []
    def visit(a, b, path):
        if len(changes) >= 40: return
        if type(a) is not type(b):
            changes.append({'path':path,'before':a,'after':b})
        elif type(a) is dict:
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b: changes.append({'path':path+'/'+key,'before':a.get(key),'after':b.get(key)})
                else: visit(a[key],b[key],path+'/'+key)
                if len(changes) >= 40: return
        elif type(a) is list:
            if len(a) != len(b): changes.append({'path':path,'before_length':len(a),'after_length':len(b)})
            else:
                for index,(left,right) in enumerate(zip(a,b)): visit(left,right,path+'/'+str(index))
        elif a != b: changes.append({'path':path,'before':a,'after':b})
    visit(first,second,'')
    return changes


def _key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _identity(row):
    return {key: row[key] for key in ('type', 'name', 'library', 'owner') if key in row}


def _index(rows):
    result = {}
    for row in rows:
        key = _key(_identity(row))
        if key in result:
            raise ValueError('Duplicate character block identity')
        result[key] = row
    return result


def _ad(rows, identity):
    found = [row for row in rows if row['identity'] == identity and row['embedded_owner'] is None]
    if len(found) != 1:
        raise ValueError('Exactly one original AnimData observation is required')
    return found[0]


def check_verification(request, combined, verification, original_before):
    """Validate concrete original/new Action facts before allowing any delta."""
    if verification.get('ok') is not True or verification.get('combined_request') != combined:
        raise ValueError('Combined native declaration has not verified')
    if verification.get('original_content_exact_against_baseline') is not True:
        raise ValueError('Historical original Action content must be verified')
    if verification.get('original_action_before') != original_before:
        raise ValueError('Original Action baseline differs')
    if (original_before['name'] != request['base_clip']['action_name'] or original_before['users'] != 1
            or original_before['use_fake_user'] or original_before['use_extra_user']):
        raise ValueError('Original Action must start with one real user and no fake/extra user')
    expected = copy.deepcopy(original_before)
    expected['users'] = 0
    if verification.get('original_action_after') != expected:
        raise ValueError('Original Action changed beyond exact users1-to0')
    change = verification.get('original_reference_change')
    if change != {'before_real_users': 1, 'after_real_users': 0, 'after_consumers': []}:
        raise ValueError('Original Action reference transition differs')
    new = verification.get('state', {})
    if (new.get('name') != request['action_name'] or new.get('users') != 1
            or new.get('use_fake_user') is not False or new.get('use_extra_user') is not False):
        raise ValueError('Copied Action needs exactly its real rig user')
    return verification


def compare_derive(before, after, rig_name, request, combined, verification, original_before):
    check_verification(request, combined, verification, original_before)
    first, second = copy.deepcopy(before), copy.deepcopy(after)
    rig = {'type': 'Object', 'name': rig_name, 'library': None}
    old = {'type': 'Action', 'name': request['base_clip']['action_name'], 'library': None}
    new = {'type': 'Action', 'name': request['action_name'], 'library': None}
    b, a = _index(first['blocks']), _index(second['blocks'])
    rk, ok, nk = _key(rig), _key(old), _key(new)
    if set(a) - set(b) != {nk} or set(b) - set(a) or ok not in b or rk not in b:
        raise ValueError('Only one new Action may extend the original ID inventory')
    if b[ok]['native_user_accounting']['users'] != 1 or a[ok]['native_user_accounting']['users'] != 0:
        raise ValueError('Original real user count did not change exactly1-to0')
    if a[nk]['native_user_accounting']['users'] != 1 or a[nk]['fake_user'] is not False:
        raise ValueError('New Action must have one real user and no fake user')
    a[ok]['native_user_accounting']['users'] = 1
    if a[ok] != b[ok]:
        raise ValueError('Original Action block content changed')
    second['blocks'].remove(a[nk])
    old_edge, new_edge = {'user': rig, 'target': old}, {'user': rig, 'target': new}
    if (first['edges'].count(old_edge) != 1 or first['edges'].count(new_edge) != 0
            or second['edges'].count(old_edge) != 0 or second['edges'].count(new_edge) != 1):
        raise ValueError('Expected exactly one old edge removed and one new edge added')
    left = first['edges'].copy()
    right = second['edges'].copy()
    left.remove(old_edge)
    right.remove(new_edge)
    if sorted(map(_key, left)) != sorted(map(_key, right)):
        raise ValueError('Non-target reference edges changed')
    # Canonical observer edge order is not an authoring change. Restore the
    # proved edge replacement using the exact original list, after multiplicity.
    second['edges'] = copy.deepcopy(first['edges'])
    before_ad = _ad(first['animdata_authoring'], rig)
    after_ad = _ad(second['animdata_authoring'], rig)
    if (before_ad['state'] is None or after_ad['state'] is None
            or before_ad['state']['settings']['action'] != old
            or after_ad['state']['settings']['action'] != new):
        raise ValueError('Original and derived exact rig bindings differ')
    binding = verification.get('binding')
    names = ('action', 'action_slot', 'action_slot_handle', 'last_slot_identifier')
    if type(binding) is not dict or set(binding) != set(names):
        raise ValueError('Four native binding fields must be explicit')
    for name in names:
        if after_ad['state']['settings'][name] != binding[name]:
            raise ValueError('Native verified binding differs from character capture')
        after_ad['state']['settings'][name] = before_ad['state']['settings'][name]
    # Blender 5.2 Action has no animation_data attribute, so capture normally
    # emits no row. An observer row is optional, but never duplicated/nonempty.
    new_ad = [row for row in second['animdata_authoring'] if row['identity'] == new]
    if len(new_ad) > 1 or (new_ad and (new_ad[0]['embedded_owner'] is not None or new_ad[0]['state'] is not None)):
        raise ValueError('Copied Action permits at most one empty nonembedded AnimData observation')
    if new_ad:
        second['animdata_authoring'].remove(new_ad[0])
    # The public observer repeats binding identity in three places. Each alias
    # must agree with its independent AnimData row before it can be restored.
    for block, settings in ((b[rk], before_ad['state']['settings']),
                            (a[rk], verification['binding'])):
        slot = settings['action_slot']
        if (block['animation']['action'] != settings['action']
                or block['animation']['slot'] != (slot['identifier'] if slot else None)
                or block['character_extra']['animation_fields']['action_slot'] != slot):
            raise ValueError('Repeated raw rig binding aliases differ from verified AnimData')
    a[rk]['animation']['action'] = b[rk]['animation']['action']
    a[rk]['animation']['slot'] = b[rk]['animation']['slot']
    a[rk]['character_extra']['animation_fields']['action_slot'] = b[rk]['character_extra']['animation_fields']['action_slot']
    equal = first == second
    return {'ok': equal, 'changes': [] if equal else diff(first, second),
            'scope': 'one verified copied Action, exact old users1-to0, one removed/added rig edge, four binding fields and aliases; all other authoring strict'}
