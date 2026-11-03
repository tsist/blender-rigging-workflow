"""Unmasked full authoring state plus explicit selected PointCache authoring."""
import hashlib
import importlib.util
import json
from pathlib import Path

import bpy
import inspection
from humanoid_rigging_probe import cache_contract, cloth_cache, motion_clips

_spec = importlib.util.spec_from_file_location('_hrig_cache_dynamics_state', Path(__file__).with_name('dynamics_state.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)


def handles(targets):
    result = []
    for target in cache_contract.canonical_targets(targets):
        obj = bpy.data.objects.get(target['object'])
        if obj is None or obj.type != 'MESH' or obj.library or target['index'] >= len(obj.modifiers):
            raise ValueError('Selected cache target Object/index differs')
        modifier = obj.modifiers[target['index']]
        if modifier.name != target['modifier'] or modifier.type != 'CLOTH' or modifier.id_data != obj:
            raise ValueError('Selected cache modifier identity differs')
        result.append((target, obj, modifier))
    return result


def capture(targets):
    snapshot = _base.capture()
    snapshot['selected_cache_authoring'] = [
        {**target, 'object_id': inspection.identity(obj), 'modifier_type': modifier.type,
         'cache_authoring': motion_clips._settings(modifier.point_cache)}
        for target, obj, modifier in handles(targets)]
    snapshot['coverage']['augmented'].append('selected native PointCache writable authoring fields, no cache mask')
    return snapshot


def derived(targets):
    return [{**target, 'cache': cloth_cache._derived(modifier.point_cache)}
            for target, obj, modifier in handles(targets)]


def baked(targets):
    # The storage flags are only admission evidence. Worker/cold also require
    # same-frame full-coordinate comparisons against producer witnesses.
    return [{**target, 'cache': cloth_cache._assert_complete(modifier)}
            for target, obj, modifier in handles(targets)]


def summarize(snapshot):
    result = _base.summarize(snapshot)
    raw = json.dumps(snapshot['selected_cache_authoring'], sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    result['selected_cache_authoring_sha256'] = hashlib.sha256(raw).hexdigest()
    return result


diff = _base.diff
