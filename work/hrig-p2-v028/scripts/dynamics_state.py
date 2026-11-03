"""Exact Cloth solver-quality mask over complete motion authoring observation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import bpy
import inspection

_spec = importlib.util.spec_from_file_location('_hrig_dynamics_motion_state', Path(__file__).with_name('motion_state.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)


def capture():
    result = _base.capture()
    # The public inspector excludes readonly RNA, including Modifier.type.
    # Observe identity separately from editable settings; do not manufacture a
    # type field inside the original inspector's records.
    result['modifier_identities'] = sorted([
        {'object': inspection.identity(obj),
         'modifiers': [{'index': index, 'name': modifier.name, 'type': modifier.type}
                       for index, modifier in enumerate(obj.modifiers)]}
        for obj in bpy.data.objects], key=lambda row: inspection.key(row['object']))
    result['coverage']['augmented'].append('all native Object modifier identities: ordered index/name/readonly type, separate from public editable settings')
    return result


def protected(snapshot, changes, side):
    if side not in ('before', 'after'):
        raise ValueError('Explicit before/after quality side required')
    result = copy.deepcopy(snapshot)
    for change in changes:
        matches = [row for row in result['blocks'] if row['type'] == 'Object'
                   and row['name'] == change['object'] and row['library'] is None]
        if len(matches) != 1:
            raise ValueError('Cloth quality target Object is missing or ambiguous')
        modifiers = matches[0]['object']['modifiers']
        index = change['index']
        if type(index) is not int or not 0 <= index < len(modifiers):
            raise ValueError('Cloth quality target modifier index differs')
        identity = {'type': 'Object', 'name': change['object'], 'library': None}
        native_rows = [row for row in result['modifier_identities'] if row['object'] == identity]
        if len(native_rows) != 1 or len(native_rows[0]['modifiers']) != len(modifiers):
            raise ValueError('Native Object modifier identity coverage differs')
        native = native_rows[0]['modifiers'][index]
        if native != {'index': index, 'name': change['modifier'], 'type': 'CLOTH'}:
            raise ValueError('Native target modifier index/name/type differs')
        modifier = modifiers[index]
        if modifier.get('name') != change['modifier']:
            raise ValueError('Cloth quality target modifier identity differs')
        if modifier.get('settings', {}).get('quality') != change[side]:
            raise ValueError('Cloth quality target does not store the exact declared ' + side)
        # Exactly this scalar is authorized. Collision quality, cache, solver
        # parameters, modifier flags/order and every Action/AnimData remain.
        modifier['settings'].pop('quality')
    return result


def compare_patch(before, after, request):
    first = protected(before, request['changes'], 'before')
    second = protected(after, request['changes'], 'after')
    return {'ok': first == second, 'changes': _base.diff(first, second),
            'scope': 'Only exact target Object/modifier index/name/type Cloth.settings.quality; all other observed authoring fields strict'}


def summarize(snapshot):
    result = _base.summarize(snapshot)
    raw = json.dumps(snapshot['modifier_identities'], sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    result['modifier_identities_sha256'] = hashlib.sha256(raw).hexdigest()
    return result


diff = _base.diff
collection_active_indices = _base.collection_active_indices
