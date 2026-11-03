"""Pure finite D4 request/planning contract; contains no Blender imports.

Normalization is an explicit authoring policy, not an evaluation requirement or
a visual quality guarantee. Existing non-editable memberships remain bit exact.
"""
from copy import deepcopy
import hashlib
import json
import math
import struct

SCHEMA = 'hrig.bound-garment.v2'
TOLERANCE = 1e-6
MAX_ROWS = 25000


class GarmentError(ValueError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(code + ': ' + str(detail))


def require(value, code, detail):
    if not value:
        raise GarmentError(code, detail)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def bits(value):
    return struct.pack('<f', value)


def number(value):
    return struct.unpack('<f', value)[0]


def numeric(value, code, *, low=None, high=None):
    try:
        finite = type(value) in (float, int) and math.isfinite(value)
    except OverflowError:
        finite = False
    require(finite, code, value)
    require((low is None or value >= low) and (high is None or value <= high), code, value)
    return float(value)


def indices(value, count, code, limit=MAX_ROWS):
    require(type(value) is list and 1 <= len(value) <= limit, code, 'Explicit finite nonempty list required')
    require(all(type(i) is int and 0 <= i < count for i in value)
            and len(value) == len(set(value)), code, value)


def validate_request(request, target_count=None, polygon_count=None):
    require(type(request) is dict, 'REQUEST_SCHEMA', 'Object required')
    common = {'schema', 'operation', 'source_body', 'target_garment', 'rig', 'space',
              'vertices', 'allowed_groups', 'normalization', 'membership'}
    operation = request.get('operation')
    require(operation in ('transfer', 'local_delta'), 'OPERATION', operation)
    extra = ({'mapping', 'source_polygons', 'max_distance', 'tie_tolerance', 'correspondence'} if operation == 'transfer'
             else {'deltas'})
    require(set(request) == common | extra, 'REQUEST_KEYS', {'expected': sorted(common | extra), 'actual': sorted(request)})
    require(request['schema'] == SCHEMA and request['space'] == 'REST_BASE', 'REQUEST_VERSION', request.get('schema'))
    for key in ('source_body', 'target_garment', 'rig'):
        require(type(request[key]) is str and bool(request[key]), 'OBJECT_NAME', key)
    require(request['source_body'] != request['target_garment'], 'BODY_EQUALS_GARMENT', request['source_body'])
    require(request['normalization'] == 'preserve_fixed_deform_residual_to_one', 'NORMALIZATION_POLICY', request['normalization'])
    require(request['membership'] == 'preserve_existing_append_positive', 'MEMBERSHIP_POLICY', request['membership'])
    indices(request['vertices'], target_count if target_count is not None else 2**31, 'VERTICES')
    groups = request['allowed_groups']
    require(type(groups) is list and 1 <= len(groups) <= 4096 and all(type(n) is str and n for n in groups)
            and len(set(groups)) == len(groups), 'ALLOWED_GROUPS', groups)
    if operation == 'transfer':
        mapping = request['mapping']
        require(type(mapping) is dict and bool(mapping) and len(mapping) <= 4096
                and all(type(k) is str and k and type(v) is str and v in groups for k,v in mapping.items()),
                'MAPPING', mapping)
        indices(request['source_polygons'], polygon_count if polygon_count is not None else 2**31,
                'SOURCE_POLYGONS', 500000)
        numeric(request['max_distance'], 'MAX_DISTANCE', low=1e-12, high=1e12)
        numeric(request['tie_tolerance'], 'TIE_TOLERANCE', low=0, high=request['max_distance'])
        overrides=request['correspondence']
        require(type(overrides) is list and len(overrides)<=len(request['vertices']),'CORRESPONDENCE_SCHEMA',overrides)
        seen=set()
        for item in overrides:
            require(type(item) is dict and item.get('mode') in ('source_edge','symmetric_average'),'CORRESPONDENCE_MODE',item)
            fields=({'edge'} if item['mode']=='source_edge' else {'axis','center'})
            require(set(item)=={'vertex','mode','triangles'}|fields,'CORRESPONDENCE_KEYS',item)
            require(type(item['vertex']) is int and item['vertex'] in request['vertices'] and item['vertex'] not in seen,
                    'CORRESPONDENCE_VERTEX',item)
            seen.add(item['vertex'])
            indices(item['triangles'],2**31,'CORRESPONDENCE_TRIANGLES',2)
            require(len(item['triangles'])==2,'CORRESPONDENCE_TRIANGLES',item)
            if item['mode']=='source_edge':
                indices(item['edge'],2**31,'CORRESPONDENCE_EDGE',2)
                require(len(item['edge'])==2,'CORRESPONDENCE_EDGE',item)
            else:
                require(type(item['axis']) is int and 0<=item['axis']<=2,'CORRESPONDENCE_AXIS',item)
                numeric(item['center'],'CORRESPONDENCE_CENTER')
    else:
        deltas = request['deltas']
        require(type(deltas) is list and 1 <= len(deltas) <= MAX_ROWS * 16, 'DELTAS', 'Explicit bounded deltas required')
        seen = set()
        for item in deltas:
            require(type(item) is dict and set(item) == {'vertex', 'group', 'delta'}, 'DELTA_SCHEMA', item)
            require(type(item['vertex']) is int and item['vertex'] in request['vertices']
                    and type(item['group']) is str and item['group'] in groups, 'DELTA_DOMAIN', item)
            key = item['vertex'], item['group']
            require(key not in seen, 'DUPLICATE_DELTA', item)
            seen.add(key)
            numeric(item['delta'], 'DELTA_VALUE', low=-1, high=1)
        require({item['vertex'] for item in deltas} == set(request['vertices']), 'DELTA_VERTEX_SET', request['vertices'])
    return deepcopy(request)


def row_values(row, groups):
    result = {}
    for index, value in row:
        require(type(index) is int and 0 <= index < len(groups) and index not in result,
                'RAW_MEMBERSHIP_SCHEMA', index)
        weight = number(value)
        numeric(weight, 'SELECTED_RAW_WEIGHT', low=0, high=1)
        result[index] = weight
    return result


def normalize(desired, old, deform_ids, editable_ids):
    """Keep fixed deform values; normalize only the declared editable residual."""
    require(bool(editable_ids), 'NO_EDITABLE_GROUPS', sorted(deform_ids))
    fixed = math.fsum(old.get(i, 0.) for i in deform_ids - editable_ids)
    require(fixed <= 1. + TOLERANCE, 'FIXED_DEFORM_OVER_ONE', fixed)
    residual = max(0., 1. - fixed)
    values = {i: numeric(desired.get(i, 0.), 'DESIRED_WEIGHT', low=0) for i in editable_ids}
    mass = math.fsum(values.values())
    require(residual <= TOLERANCE or mass > 0, 'UNWEIGHTED_EDITABLE_RESIDUAL', {'fixed': fixed, 'residual': residual})
    result = {i: f32(values[i] * residual / mass) if mass > 0 else 0. for i in sorted(editable_ids)}
    require(abs(math.fsum(result.values()) + fixed - 1.) <= TOLERANCE, 'NORMALIZATION_F32', result)
    return result, {'fixed_deform_sum': fixed, 'editable_residual': residual, 'raw_editable_mass': mass,
                    'deform_sum': fixed + math.fsum(result.values())}


def predict_row(row, desired):
    """Native append policy; ordering is checked by runtime, not assumed proved."""
    existing = {i for i,_ in row}
    result = [(i, bits(desired[i]) if i in desired else value) for i,value in row]
    result.extend((i, bits(value)) for i,value in sorted(desired.items()) if i not in existing and value > 0.)
    return tuple(result)


def operations(before, after):
    """Replace existing members first, then append new positives in index order."""
    old = dict(before)
    return [(i, number(value)) for i,value in after if i not in old or value != old[i]]


def barycentric(point, triangle):
    a,b,c = triangle
    dot = lambda x,y: math.fsum(u*v for u,v in zip(x,y))
    sub = lambda x,y: tuple(u-v for u,v in zip(x,y))
    u,v,w = sub(b,a), sub(c,a), sub(point,a)
    uu,uv,vv,wu,wv = dot(u,u),dot(u,v),dot(v,v),dot(w,u),dot(w,v)
    denominator = uu*vv-uv*uv
    require(math.isfinite(denominator) and denominator > max(uu*vv, 1e-300)*1e-14,
            'DEGENERATE_SOURCE_TRIANGLE', triangle)
    t = (vv*wu-uv*wv)/denominator
    s = (uu*wv-uv*wu)/denominator
    result = (1.-t-s,t,s)
    require(all(math.isfinite(x) and -1e-5 <= x <= 1.+1e-5 for x in result), 'BARYCENTRIC_DOMAIN', result)
    clamped = [max(0.,min(1.,x)) for x in result]
    total = math.fsum(clamped)
    return tuple(x/total for x in clamped)


def interpolate(rows, factors, source_groups, source_deform, mapping, target_indices):
    result, source_sum = {}, 0.
    unmapped = set()
    for row,factor in zip(rows,factors):
        for index,weight in row_values(row, source_groups).items():
            name = source_groups[index]
            if name not in source_deform or weight == 0. or factor == 0.:
                continue
            contribution = weight * factor
            source_sum += contribution
            if name not in mapping:
                unmapped.add(name)
            else:
                target = target_indices[mapping[name]]
                result[target] = result.get(target,0.) + contribution
    require(not unmapped, 'UNMAPPED_POSITIVE_SOURCE_INFLUENCES', sorted(unmapped))
    require(source_sum > 0., 'UNWEIGHTED_SOURCE_SAMPLE', source_sum)
    return result, source_sum
