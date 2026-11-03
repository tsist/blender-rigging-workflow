"""Pure bounded weight algorithms and ordered membership storage contract.

Only requested editable deform groups may change. Existing records never
disappear; discarded positive influences become present zero records. New
positive records append by group index. Numerical total policy is explicit.
"""
from copy import deepcopy
import hashlib
import json
import math
import struct

SCHEMA = 'hrig.weight-workflow.v2'
MEMBERSHIP = 'preserve_existing_append_positive'
PRESERVE = 'preserve_source_deform_sum'
ONE = 'target_deform_sum_one'
TOLERANCE = 1e-6
MAX_ROWS = 65536
OPERATIONS = ('SMOOTH', 'MIRROR', 'NORMALIZE', 'LIMIT_INFLUENCES', 'GENERATE_FROM_NEIGHBORS')


class WeightError(ValueError):
    def __init__(self, code, detail=''):
        self.code, self.detail = code, deepcopy(detail)
        super().__init__(code + ': ' + str(detail))


def require(value, code, detail=''):
    if not value:
        raise WeightError(code, detail)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def numeric(value, code, low=0., high=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    require(valid, code, value)
    require(value >= low and (high is None or value <= high), code, value)
    return float(value)


def integer(value, low, high, code):
    require(type(value) is int and low <= value <= high, code, value)
    return value


def bits(value):
    return struct.pack('<f', float(value))


def number(value):
    return struct.unpack('<f', value)[0]


def f32(value):
    try:
        out = number(bits(value))
    except (OverflowError, ValueError, TypeError, struct.error) as error:
        raise WeightError('F32_RANGE', str(error)) from error
    require(math.isfinite(out), 'F32_RANGE', value)
    return out


def row_document(row):
    return [[index, value.hex()] for index, value in row]


def parse_row(value, group_count):
    require(type(value) is list and len(value) <= group_count, 'ROW_SCHEMA')
    out, seen = [], set()
    for entry in value:
        require(type(entry) is list and len(entry) == 2, 'MEMBERSHIP_SCHEMA', entry)
        index, raw = entry
        integer(index, 0, group_count - 1, 'MEMBERSHIP_GROUP')
        require(index not in seen, 'DUPLICATE_MEMBERSHIP', index)
        require(type(raw) is str and len(raw) == 8 and all(c in '0123456789abcdef' for c in raw), 'F32_HEX', raw)
        packed = bytes.fromhex(raw)
        numeric(number(packed), 'SELECTED_RAW_WEIGHT', high=1.)
        seen.add(index)
        out.append((index, packed))
    return tuple(out)


def indices(value, count, code, limit=MAX_ROWS):
    require(type(value) is list and 1 <= len(value) <= limit, code)
    require(all(type(i) is int and 0 <= i < count for i in value)
            and len(set(value)) == len(value), code, value)


def validate_request(request, count=500000):
    require(type(request) is dict and set(request) == {
        'schema', 'operation', 'object', 'rig', 'vertices', 'editable_groups',
        'membership', 'sum_policy', 'parameters'}, 'REQUEST_KEYS')
    require(request['schema'] == SCHEMA, 'REQUEST_VERSION')
    operation = request['operation']
    require(operation in OPERATIONS, 'OPERATION', operation)
    for key in ('object', 'rig'):
        require(type(request[key]) is str and 0 < len(request[key].encode('utf8')) <= 255
                and '\0' not in request[key], 'OBJECT_NAME', key)
    indices(request['vertices'], count, 'VERTICES')
    names = request['editable_groups']
    require(type(names) is list and 1 <= len(names) <= 4096
            and all(type(n) is str and 0 < len(n.encode('utf8')) <= 255 and '\0' not in n for n in names)
            and len(set(names)) == len(names), 'EDITABLE_GROUPS')
    require(request['membership'] == MEMBERSHIP, 'MEMBERSHIP_POLICY')
    expected_sum = ONE if operation in ('NORMALIZE', 'GENERATE_FROM_NEIGHBORS') else PRESERVE
    require(request['sum_policy'] == expected_sum, 'SUM_POLICY', expected_sum)
    p = request['parameters']
    require(type(p) is dict, 'PARAMETERS')
    if operation == 'SMOOTH':
        require(set(p) == {'iterations', 'factor'}, 'SMOOTH_PARAMETERS')
        integer(p['iterations'], 1, 20, 'ITERATIONS')
        numeric(p['factor'], 'FACTOR', high=1.)
        require(p['factor'] > 0., 'FACTOR')
    elif operation == 'MIRROR':
        require(set(p) == {'vertex_pairs', 'group_pairs'}, 'MIRROR_PARAMETERS')
        pairs = p['vertex_pairs']
        require(type(pairs) is list and len(pairs) == len(request['vertices']), 'VERTEX_PAIRS')
        for pair in pairs:
            require(type(pair) is list and len(pair) == 2, 'VERTEX_PAIR')
            require(all(type(i) is int and 0 <= i < count for i in pair) and pair[0] != pair[1], 'VERTEX_PAIR', pair)
        require(len({a for a, _ in pairs}) == len(pairs)
                and {b for _, b in pairs} == set(request['vertices']), 'VERTEX_PAIR_BIJECTION')
        groups = p['group_pairs']
        require(type(groups) is list and len(groups) == len(names), 'GROUP_PAIRS')
        require(all(type(pair) is list and len(pair) == 2
                    and all(type(n) is str and bool(n) for n in pair) for pair in groups), 'GROUP_PAIR')
        require(len({a for a, _ in groups}) == len(groups)
                and {b for _, b in groups} == set(names), 'GROUP_PAIR_BIJECTION')
    elif operation == 'NORMALIZE':
        require(not p, 'NORMALIZE_PARAMETERS')
    elif operation == 'LIMIT_INFLUENCES':
        require(set(p) == {'maximum'}, 'LIMIT_PARAMETERS')
        integer(p['maximum'], 1, 64, 'INFLUENCE_BUDGET')
    else:
        require(set(p) == {'max_hops', 'max_donors', 'donor_vertices'}, 'GENERATE_PARAMETERS')
        integer(p['max_hops'], 1, 4, 'MAX_HOPS')
        integer(p['max_donors'], 1, 256, 'MAX_DONORS')
        indices(p['donor_vertices'], count, 'DONOR_VERTICES')
        require(not set(p['donor_vertices']) & set(request['vertices']), 'DONOR_TARGET_OVERLAP')
    return deepcopy(request)


def adjacency(edges, count):
    require(type(edges) is list and len(edges) <= 2000000, 'EDGE_BUDGET')
    result, seen = {}, set()
    for pair in edges:
        require(type(pair) is list and len(pair) == 2 and all(type(i) is int and 0 <= i < count for i in pair)
                and pair[0] != pair[1], 'EDGE_SCHEMA', pair)
        a, b = pair
        key = min(a, b), max(a, b)
        require(key not in seen, 'DUPLICATE_EDGE', pair)
        seen.add(key)
        result.setdefault(a, set()).add(b)
        result.setdefault(b, set()).add(a)
    return {v: tuple(sorted(neighbors)) for v, neighbors in result.items()}


def required_vertices(request, edges, count):
    request = validate_request(request, count)
    selected = set(request['vertices'])
    operation = request['operation']
    if operation == 'SMOOTH':
        graph = adjacency(edges, count)
        selected.update(n for v in request['vertices'] for n in graph.get(v, ()))
    elif operation == 'MIRROR':
        selected.update(a for a, _ in request['parameters']['vertex_pairs'])
    elif operation == 'GENERATE_FROM_NEIGHBORS':
        selected.update(request['parameters']['donor_vertices'])
    require(len(selected) <= 131072, 'READ_VERTEX_BUDGET', len(selected))
    return sorted(selected)


def fit_mass(desired, old, deform, editable, target):
    """Retain all fixed deform mass; fit editable proportions to explicit total."""
    fixed = math.fsum(old.get(i, 0.) for i in sorted(deform - editable))
    require(fixed <= target, 'FIXED_MASS_EXCEEDS_TARGET', {'fixed': fixed, 'target': target})
    residual = target - fixed
    values = {i: numeric(desired.get(i, 0.), 'DESIRED_WEIGHT') for i in sorted(editable)}
    mass = math.fsum(values.values())
    require(residual == 0. or mass > 0., 'NO_EDITABLE_MASS', {'target': target, 'fixed': fixed})
    result = {i: f32(values[i] / mass * residual) if residual > 0. else 0. for i in sorted(editable)}
    if any(v > 1. for v in result.values()):
        # v2 explicitly extends the formerly-refused raw nonunit-sum domain.
        # Do not clamp and lose mass: cap saturated entries, then distribute
        # only the remaining mass proportionally among the unsaturated ones.
        # A zero desired entry cannot become a hidden fallback/root influence.
        positive = {i for i, value in values.items() if value > 0.}
        require(residual <= len(positive), 'STORED_WEIGHT_RANGE',
                {'editable_residual': residual, 'positive_capacity': len(positive)})
        result = {i: 0. for i in sorted(editable)}
        remaining, unallocated = set(positive), residual
        while remaining:
            denominator = math.fsum(values[i] for i in sorted(remaining))
            proposal = {i: values[i] / denominator * unallocated for i in sorted(remaining)}
            saturated = [i for i, value in proposal.items() if value > 1.]
            if not saturated:
                for i, value in proposal.items():
                    result[i] = f32(value)
                break
            for i in saturated:
                result[i] = 1.
                remaining.remove(i)
            unallocated -= len(saturated)
        require(unallocated >= 0., 'CAP_ALLOCATION_MASS')
    require(all(0. <= v <= 1. for v in result.values()), 'STORED_WEIGHT_RANGE', result)
    require(all(values[i] == 0. or result[i] > 0. or residual == 0. for i in result), 'WEIGHT_UNDERFLOW')
    actual = fixed + math.fsum(result.values())
    require(abs(actual - target) <= TOLERANCE, 'STORED_SUM', {'target': target, 'actual': actual})
    return result


def predict_row(row, desired):
    present = {i for i, _ in row}
    result = [(i, bits(desired[i]) if i in desired else value) for i, value in row]
    result.extend((i, bits(v)) for i, v in sorted(desired.items()) if i not in present and v > 0.)
    return tuple(result)


def operations(before, after):
    old = dict(before)
    return [(i, number(value)) for i, value in after if i not in old or value != old[i]]


def restore_operations(original, current, editable):
    """Only remove owned appended records, from the tail, then restore f32."""
    old = dict(original)
    require(len(current) >= len(original) and [i for i, _ in current[:len(original)]] == [i for i, _ in original],
            'RESTORE_ORIGINAL_MEMBERSHIP_ORDER')
    require(all(i in editable for i, _ in current[len(original):]), 'RESTORE_PROTECTED_INSERTION')
    require(all(i in editable or value == old[i] for i, value in current[:len(original)]), 'RESTORE_PROTECTED_VALUE')
    changes = [(i, None) for i, _ in reversed(current[len(original):])]
    now = dict(current)
    changes.extend((i, number(value)) for i, value in original if now[i] != value)
    return changes


def plan(snapshot, request):
    require(type(snapshot) is dict and set(snapshot) == {'schema', 'object', 'rig', 'vertex_count', 'groups', 'rows', 'edges'}, 'SNAPSHOT_KEYS')
    require(snapshot['schema'] == 'hrig.weight-workflow-snapshot.v1', 'SNAPSHOT_VERSION')
    count = integer(snapshot['vertex_count'], 1, 500000, 'VERTEX_COUNT')
    request = validate_request(request, count)
    require(snapshot['object'] == request['object'] and snapshot['rig'] == request['rig'], 'SNAPSHOT_BINDING')
    groups = snapshot['groups']
    require(type(groups) is list and 1 <= len(groups) <= 4096, 'GROUPS')
    names, deform, editable = {}, set(), set()
    for index, group in enumerate(groups):
        require(type(group) is dict and set(group) == {'index', 'name', 'deform', 'locked', 'consumer'}, 'GROUP_SCHEMA')
        require(group['index'] == index and type(group['index']) is int
                and type(group['name']) is str and bool(group['name']) and group['name'] not in names, 'GROUP_ROSTER')
        require(all(type(group[k]) is bool for k in ('deform', 'locked', 'consumer')), 'GROUP_FLAGS')
        names[group['name']] = index
        if group['deform']:
            deform.add(index)
    for name in request['editable_groups']:
        require(name in names, 'UNKNOWN_GROUP', name)
        index = names[name]
        require(index in deform and not groups[index]['locked'] and not groups[index]['consumer'], 'PROTECTED_EDITABLE_GROUP', name)
        editable.add(index)
    needed = required_vertices(request, snapshot['edges'], count)
    rows, values = {}, {}
    require(type(snapshot['rows']) is list and len(snapshot['rows']) == len(needed), 'ROW_DOMAIN')
    for item in snapshot['rows']:
        require(type(item) is dict and set(item) == {'index', 'members'}, 'ROW_KEYS')
        index = integer(item['index'], 0, count - 1, 'ROW_INDEX')
        require(index not in rows, 'DUPLICATE_ROW', index)
        rows[index] = parse_row(item['members'], len(groups))
        values[index] = {i: number(v) for i, v in rows[index]}
    require(set(rows) == set(needed), 'ROW_DOMAIN')
    selected, p = request['vertices'], request['parameters']
    totals = {v: math.fsum(values[v].get(i, 0.) for i in sorted(deform)) for v in selected}
    desired = {v: {i: values[v].get(i, 0.) for i in sorted(editable)} for v in selected}
    operation, donor_details = request['operation'], {}
    if operation == 'SMOOTH':
        graph = adjacency(snapshot['edges'], count)
        current = deepcopy(values)
        for _ in range(p['iterations']):
            updated = {}
            for v in selected:
                neighbors = graph.get(v, ())
                require(neighbors, 'NO_EDGE_NEIGHBORS', v)
                averaged = {i: math.fsum(current[n].get(i, 0.) for n in neighbors) / len(neighbors) for i in sorted(editable)}
                blended = {i: (1. - p['factor']) * current[v].get(i, 0.) + p['factor'] * averaged[i] for i in sorted(editable)}
                updated[v] = fit_mass(blended, values[v], deform, editable, totals[v])
            for v in selected:
                current[v] = {**values[v], **updated[v]}
        desired = updated
    elif operation == 'MIRROR':
        mapping = []
        for source, target in p['group_pairs']:
            require(source in names and names[source] in deform, 'MIRROR_SOURCE_NOT_DEFORM', source)
            mapping.append((names[source], names[target]))
        for source, target in p['vertex_pairs']:
            desired[target] = {b: values[source].get(a, 0.) for a, b in mapping}
            donor_details[target] = [source]
    elif operation == 'LIMIT_INFLUENCES':
        for v in selected:
            fixed_count = sum(values[v].get(i, 0.) > 0. for i in deform - editable)
            slots = p['maximum'] - fixed_count
            require(slots >= 0, 'FIXED_INFLUENCE_BUDGET', {'vertex': v, 'fixed': fixed_count})
            ranked = sorted((i for i in editable if values[v].get(i, 0.) > 0.), key=lambda i: (-values[v][i], i))
            keep = set(ranked[:slots])
            desired[v] = {i: values[v].get(i, 0.) if i in keep else 0. for i in sorted(editable)}
    elif operation == 'GENERATE_FROM_NEIGHBORS':
        graph = adjacency(snapshot['edges'], count)
        allowed = set(p['donor_vertices'])
        for v in selected:
            seen, frontier, donors = {v}, {v}, []
            for _ in range(p['max_hops']):
                frontier = {n for old in frontier for n in graph.get(old, ())} - seen
                seen.update(frontier)
                require(len(seen) <= 4096, 'NEIGHBOR_VISIT_BUDGET', v)
                donors = sorted(n for n in frontier & allowed if math.fsum(values[n].get(i, 0.) for i in sorted(editable)) > 0.)
                if donors:
                    break
            require(donors, 'NO_VALID_WEIGHTED_NEIGHBORS', v)
            require(len(donors) <= p['max_donors'], 'DONOR_BUDGET', {'vertex': v, 'donors': len(donors)})
            donor_details[v] = donors
            masses = {n: math.fsum(values[n].get(i, 0.) for i in sorted(editable)) for n in donors}
            desired[v] = {i: math.fsum(values[n].get(i, 0.) / masses[n] for n in donors) / len(donors) for i in sorted(editable)}
    output = []
    for v in selected:
        target = 1. if request['sum_policy'] == ONE else totals[v]
        # SMOOTH already quantizes/rescales at each declared iteration. Do not
        # perform an undeclared extra normalization pass after the final one.
        fitted = desired[v] if operation == 'SMOOTH' else fit_mass(desired[v], values[v], deform, editable, target)
        after = predict_row(rows[v], fitted)
        after_map = {i: number(raw) for i, raw in after}
        positive_count = sum(after_map.get(i, 0.) > 0. for i in deform)
        if operation == 'LIMIT_INFLUENCES':
            require(positive_count <= p['maximum'], 'INFLUENCE_BUDGET', v)
        require(all(i in editable or dict(after)[i] == raw for i, raw in rows[v]), 'PROTECTED_ROW_CHANGED', v)
        output.append({'index': v, 'before': row_document(rows[v]), 'after': row_document(after),
            'source_deform_sum': totals[v], 'target_deform_sum': target,
            'stored_deform_sum': math.fsum(after_map.get(i, 0.) for i in sorted(deform)),
            'present_memberships': len(after), 'positive_deform_influences': positive_count,
            'appended_groups': [i for i, _ in after if i not in values[v]],
            'donors': donor_details.get(v, []), 'changed': after != rows[v]})
    require(any(r['changed'] for r in output), 'NO_STORED_EFFECT')
    return {'schema': 'hrig.weight-workflow-plan.v2', 'request': request,
        'snapshot_sha256': digest(snapshot), 'editable_indices': sorted(editable), 'rows': output,
        'allocation_policy': 'proportional_with_explicit_unit_cap_saturation',
        'qualification': 'explicit storage plan; native oracle, restoration, save/cold and visible quality remain required'}
