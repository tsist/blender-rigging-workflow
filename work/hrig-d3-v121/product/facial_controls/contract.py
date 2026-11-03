"""Strict normalized requests. No Blender/Python/RNA supplied by request data."""
from copy import deepcopy
import hashlib
import json
import math
from .catalog import load, index

SCHEMA = 'hrig.facial-controls.request.v1'
CLIP_SCHEMA = 'hrig.facial-controls.clip.v1'


class FaceError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def require(ok, code, detail=None):
    if not ok:
        raise FaceError(code, detail)


def finite(x):
    return type(x) in (int, float) and math.isfinite(x)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def exact(value, fields, code):
    require(type(value) is dict and set(value) == set(fields), code)


def decode_request(raw):
    def pairs(items):
        row = {}
        for k, v in items:
            require(k not in row, 'DUPLICATE_JSON_KEY', k)
            row[k] = v
        return row
    try:
        result = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        if isinstance(error, FaceError):
            raise
        raise FaceError('FACE_JSON') from error
    compile_request(result)
    return result


def compile_request(request, catalog=None):
    data = load() if catalog is None else catalog
    controls = index(data)
    exact(request, ('schema', 'mode', 'preset', 'strength', 'values'), 'FACE_REQUEST_FIELDS')
    require(request['schema'] == SCHEMA, 'FACE_REQUEST_SCHEMA')
    require(request['mode'] == 'independent', 'FACE_MODE_UNSUPPORTED')
    require(type(request['preset']) is str and request['preset'] in ('manual', *data['presets']), 'FACE_PRESET')
    strength = request['strength']
    require(finite(strength) and 0 <= strength <= 1, 'FACE_STRENGTH')
    require(type(request['values']) is dict and len(request['values']) <= 24, 'FACE_VALUES')
    require(request['preset'] == 'manual' or not request['values'], 'PRESET_OVERRIDE_AMBIGUOUS')
    values = deepcopy(request['values'] if request['preset'] == 'manual' else data['presets'][request['preset']])
    require(len(values) <= 24, 'FACE_BATCH_LIMIT')
    compiled = []
    for name in sorted(values):
        require(name in controls, 'FACE_CONTROL_UNSUPPORTED', name)
        c = controls[name]
        value = values[name]
        lo, hi = c['normalized_bounds']
        require(finite(value) and lo <= value <= hi, 'FACE_NORMALIZED_BOUNDS', name)
        wanted = float(value * strength)
        raw = wanted * (c['positive_scale'] if wanted >= 0 else c['negative_scale'])
        require(c['raw_bounds'][0] <= raw <= c['raw_bounds'][1], 'FACE_RAW_BOUNDS', name)
        compiled.append({'control': name, 'role': c['role_id'], 'normalized': wanted,
                         'bone': c['bone'], 'index': c['index'], 'value': raw})
    return {'schema': 'hrig.facial-controls.plan.v1', 'request': deepcopy(request),
            'request_sha256': digest(request), 'inputs': compiled,
            'operations': [{'role': r['role'], 'value': r['value']} for r in compiled],
            'mode': 'independent', 'required_zero_pads': deepcopy(data['required_zero_pads']),
            'preset_qualification': data['preset_qualification']}


def compile_clip(clip, catalog=None):
    exact(clip, ('schema', 'name', 'keys'), 'FACE_CLIP_FIELDS')
    require(clip['schema'] == CLIP_SCHEMA and type(clip['name']) is str
            and 0 < len(clip['name']) <= 63 and '\x00' not in clip['name'], 'FACE_CLIP_SCHEMA')
    require(type(clip['keys']) is list and 2 <= len(clip['keys']) <= 256, 'FACE_CLIP_GRID')
    plans, frames, union = [], [], set()
    for row in clip['keys']:
        exact(row, ('frame', 'request'), 'FACE_CLIP_KEY')
        f = row['frame']
        require(type(f) is int and 0 <= f <= 1048573 and (not frames or f > frames[-1]), 'FACE_CLIP_FRAME')
        frames.append(f)
        plan = compile_request(row['request'], catalog)
        plans.append(plan)
        union.update(x['role'] for x in plan['operations'])
    require(bool(union) and len(union) <= 24, 'FACE_CLIP_ROSTER')
    roster = sorted(union)
    return {'schema': 'hrig.facial-control-animation.v3', 'name': clip['name'],
            'replace_targets': True,
            'keys': [{'frame': f, 'operations': [
                {'role': role, 'value': next((op['value'] for op in p['operations'] if op['role'] == role), 0.0)}
                for role in roster]} for f, p in zip(frames, plans)]}
