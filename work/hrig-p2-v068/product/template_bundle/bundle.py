"""Pinned JSON template packages and metadata-only cross-character instances.

Design reuse: v063 semantic_contract.parse_template / merge_instance established
duplicate-free JSON, explicit role binding, and base/current/incoming merge.
This layer retains those rules but owns a separate full-package schema. It does
not replay v063 rest-field writes, constraints, drivers, skin or shape geometry.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

SCHEMA = 'hrig.template-bundle.v1'
INSTANCE = 'hrig.template-instance.v1'
FILES = ('native-profile.json', 'inventory.json', 'semantics.json', 'provenance.json')
ABSENT = object()


class BundleError(ValueError):
    def __init__(self, code, details=None):
        self.code, self.details = code, details or {}
        super().__init__(code + ': ' + str(self.details))


def require(condition, code, **details):
    if not condition:
        raise BundleError(code, details)


def exact(value, keys, path):
    require(type(value) is dict and set(value) == set(keys), 'SCHEMA', path=path, expected=list(keys))


def text(value):
    require(type(value) is str and 0 < len(value) <= 4096 and not any(ord(c) < 32 for c in value), 'TEXT')
    return value


def prose(value):
    require(type(value) is str and 0 < len(value) <= 20000 and not any(ord(c) < 32 and c not in '\n\r\t' for c in value), 'PROSE')


def number(value, minimum=-1e20, maximum=1e20):
    require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum, 'NUMBER')


def sha(value):
    require(type(value) is str and re.fullmatch('[a-f0-9]{64}', value), 'SHA256')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    finite(value)
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf8')


def finite(value):
    if type(value) is dict:
        require(all(type(k) is str for k in value), 'JSON_KEYS')
        for item in value.values(): finite(item)
    elif type(value) is list:
        for item in value: finite(item)
    elif type(value) is float:
        require(math.isfinite(value), 'NON_FINITE')
    else:
        require(value is None or type(value) in (str, bool, int), 'JSON_VALUE')


def parse(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'DUPLICATE_KEY', key=key)
            out[key] = value
        return out
    def constant(value):
        raise BundleError('NON_FINITE', {'value': value})
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BundleError('JSON', {'message': str(error)}) from error
    finite(result)
    return result


def read(path, expected=None):
    p = Path(path)
    require(p.is_file() and not p.is_symlink(), 'FILE', path=str(p))
    raw = p.read_bytes()
    if expected is not None:
        sha(expected)
        require(digest(raw) == expected, 'STALE_HASH', path=str(p))
    return raw


def absolute_path(value):
    p = Path(value)
    require(p.is_absolute() and not p.is_symlink(), 'ABSOLUTE_NONSYMLINK_PATH', path=str(p))
    return p.resolve()


def descriptor(path):
    p = Path(path).resolve()
    return {'file': str(p), 'expected_sha256': digest(read(p))}


def descriptor_check(value):
    exact(value, ('file', 'expected_sha256'), 'descriptor')
    text(value['file']); sha(value['expected_sha256'])
    require(Path(value['file']).is_absolute(), 'ABSOLUTE_PATH')


def load_descriptor(value):
    descriptor_check(value)
    return parse(read(value['file'], value['expected_sha256']))


def _no_programs(value):
    # Opaque trusted profile is stored, pinned and never evaluated. Reject
    # executable/RNA command fields even inside an otherwise opaque payload.
    banned = {'script', 'python', 'code', 'expression', 'rna_path', 'data_path', 'constraints', 'drivers', 'operator'}
    if type(value) is dict:
        require(not (set(value) & banned), 'EXECUTABLE_CONTENT')
        for item in value.values(): _no_programs(item)
    elif type(value) is list:
        for item in value: _no_programs(item)


def profile_check(profile):
    require(type(profile) is dict and profile.get('schema') == 'hrig.native-control-profile.v2', 'PROFILE_SCHEMA')
    _no_programs(profile)
    text(profile.get('character_id')); text(profile.get('profile_id'))
    require(type(profile.get('rig')) is dict and type(profile['rig'].get('witness_bones')) is list, 'PROFILE_RIG')
    roles = profile.get('roles')
    require(type(roles) is list and 0 < len(roles) <= 512, 'PROFILE_ROLES')
    role_map = {}
    for r in roles:
        require(type(r) is dict, 'PROFILE_ROLE')
        name, bone = text(r.get('role_id')), text(r.get('bone'))
        require(name not in role_map and bone in profile['rig']['witness_bones'], 'PROFILE_ROLE_BINDING')
        role_map[name] = bone
    return role_map


def inventory_check(inv):
    exact(inv, ('schema', 'character_id', 'source', 'rig', 'bones', 'meshes', 'blender', 'build', 'autoexec_enabled', 'saved', 'scope'), 'inventory')
    require(inv['schema'] == 'hrig.template-inventory.v1' and inv['autoexec_enabled'] is False and inv['saved'] is False, 'INVENTORY_SCOPE')
    descriptor_check(inv['source'])
    for k in ('character_id', 'rig', 'blender', 'build', 'scope'): text(inv[k])
    require(type(inv['bones']) is list and len(inv['bones']) == len(set(inv['bones'])), 'BONES')
    for b in inv['bones']: text(b)
    require(type(inv['meshes']) is list and bool(inv['meshes']), 'MESHES')
    objects = set()
    for mesh in inv['meshes']:
        exact(mesh, ('object', 'data', 'vertices', 'edges', 'polygons', 'topology_sha256', 'groups', 'weights_sha256', 'assignments', 'shape_keys'), 'mesh')
        text(mesh['object']); text(mesh['data'])
        require(mesh['object'] not in objects, 'DUPLICATE_MESH'); objects.add(mesh['object'])
        for k in ('vertices', 'edges', 'polygons', 'assignments'):
            require(type(mesh[k]) is int and mesh[k] >= 0, 'MESH_COUNT')
        sha(mesh['topology_sha256']); sha(mesh['weights_sha256'])
        require(type(mesh['groups']) is list and len(mesh['groups']) == len(set(mesh['groups'])), 'GROUPS')
        for g in mesh['groups']: text(g)
        require(type(mesh['shape_keys']) is list, 'SHAPE_KEYS')
        names = set()
        for key in mesh['shape_keys']:
            exact(key, ('key', 'relative_key', 'coordinate_sha256', 'moved_vertices', 'slider', 'mask_group'), 'key')
            text(key['key']); text(key['relative_key']); sha(key['coordinate_sha256'])
            require(key['key'] not in names, 'DUPLICATE_SHAPE'); names.add(key['key'])
            require(type(key['moved_vertices']) is int and 0 <= key['moved_vertices'] <= mesh['vertices'], 'SHAPE_COUNT')
            require(type(key['slider']) is list and len(key['slider']) == 2, 'SLIDER')
            for v in key['slider']: number(v)
            require(key['slider'][0] <= key['slider'][1] and type(key['mask_group']) is str, 'SLIDER')
        require(all(k['relative_key'] in names for k in mesh['shape_keys']), 'RELATIVE_KEY')


def settings_check(settings, role_ids):
    exact(settings, ('controls', 'user_notes'), 'settings')
    require(type(settings['controls']) is dict and set(settings['controls']) == set(role_ids), 'SETTINGS_ROLES')
    for role, item in settings['controls'].items():
        exact(item, ('label', 'visible', 'gain'), 'control_settings')
        text(item['label']); require(type(item['visible']) is bool, 'VISIBILITY'); number(item['gain'], .01, 10)
    require(type(settings['user_notes']) is dict and len(settings['user_notes']) <= 100, 'USER_NOTES')
    for k, v in settings['user_notes'].items(): text(k); text(v)


def semantics_check(s, profile, inv):
    exact(s, ('schema', 'roles', 'expressions', 'weights', 'settings'), 'semantics')
    require(s['schema'] == 'hrig.template-semantics.v1', 'SEMANTICS_SCHEMA')
    role_map = profile_check(profile)
    require(s['roles'] == role_map and set(role_map.values()) <= set(inv['bones']), 'SEMANTIC_ROLES')
    settings_check(s['settings'], role_map)
    require(type(s['expressions']) is dict and bool(s['expressions']), 'EXPRESSIONS')
    meshes = {m['object']: m for m in inv['meshes']}
    for expr_id, expr in s['expressions'].items():
        text(expr_id)
        exact(expr, ('label', 'meaning', 'terms'), 'expression')
        text(expr['label']); text(expr['meaning'])
        require(type(expr['terms']) is list and 0 < len(expr['terms']) <= 32, 'TERMS')
        seen = set()
        for t in expr['terms']:
            exact(t, ('mesh', 'key', 'coefficient'), 'term')
            number(t['coefficient'], -2, 2)
            require(t['mesh'] in meshes and (t['mesh'], t['key']) not in seen, 'SHAPE_REFERENCE')
            seen.add((t['mesh'], t['key']))
            key = next((k for k in meshes[t['mesh']]['shape_keys'] if k['key'] == t['key']), None)
            require(key is not None and key['moved_vertices'] > 0, 'SHAPE_USEFUL', mesh=t['mesh'], key=t['key'])
    exact(s['weights'], ('mode', 'same_topology', 'cross_topology', 'meshes'), 'weights')
    require(s['weights']['mode'] == 'descriptor_only' and s['weights']['same_topology'] == 'require_exact_topology_and_group_mapping' and s['weights']['cross_topology'] == 'requires_explicit_transfer_backend_and_validation', 'WEIGHT_SCOPE')
    require(s['weights']['meshes'] == {m['object']: {'topology_sha256': m['topology_sha256'], 'weights_sha256': m['weights_sha256'], 'groups': m['groups'], 'assignments': m['assignments']} for m in inv['meshes']}, 'WEIGHT_DESCRIPTOR')


def provenance_check(p):
    exact(p, ('character_id', 'distribution', 'license', 'license_url', 'attribution', 'source_url', 'authorization', 'evidence', 'changes'), 'provenance')
    for k in ('character_id', 'license', 'attribution', 'authorization', 'changes'): text(p[k])
    require(p['distribution'] in ('attribution_required', 'private_research_only'), 'DISTRIBUTION')
    for k in ('license_url', 'source_url'): require(type(p[k]) is str, 'URL')
    if p['distribution'] == 'attribution_required':
        require(p['license'] == 'CC BY 4.0' and p['license_url'] == 'https://creativecommons.org/licenses/by/4.0/' and p['source_url'].startswith('https://studio.blender.org/'), 'LICENSE')
    require(type(p['evidence']) is list and bool(p['evidence']), 'PROVENANCE_EVIDENCE')
    for e in p['evidence']:
        exact(e, ('origin', 'sha256', 'statement'), 'license_evidence')
        text(e['origin']); prose(e['statement']); sha(e['sha256'])


def create(spec):
    """Create validated package bytes from pinned source descriptors and explicit semantics."""
    exact(spec, ('template_id', 'revision', 'profile', 'inventory', 'semantics', 'provenance'), 'create')
    text(spec['template_id']); require(type(spec['revision']) is int and spec['revision'] > 0, 'REVISION')
    raw = {name: read(spec[k]['file'], spec[k]['expected_sha256']) for k, name in [('profile', FILES[0]), ('inventory', FILES[1])]}
    for k in ('profile', 'inventory'): descriptor_check(spec[k])
    raw['semantics.json'] = encode(spec['semantics']); raw['provenance.json'] = encode(spec['provenance'])
    manifest = {'schema': SCHEMA, 'template_id': spec['template_id'], 'revision': spec['revision'],
                'character_id': spec['provenance']['character_id'], 'files': {n: digest(b) for n, b in raw.items()},
                'origins': {'profile': copy.deepcopy(spec['profile']), 'inventory': copy.deepcopy(spec['inventory'])},
                'scope': 'metadata_only_no_native_or_skin_writes'}
    validate(manifest, raw)
    return manifest, raw


def validate(manifest, raw):
    exact(manifest, ('schema', 'template_id', 'revision', 'character_id', 'files', 'origins', 'scope'), 'manifest')
    require(manifest['schema'] == SCHEMA and manifest['scope'] == 'metadata_only_no_native_or_skin_writes', 'BUNDLE_SCOPE')
    text(manifest['template_id']); text(manifest['character_id'])
    require(type(manifest['revision']) is int and manifest['revision'] > 0, 'REVISION')
    exact(manifest['files'], FILES, 'files'); exact(raw, FILES, 'payload')
    exact(manifest['origins'], ('profile', 'inventory'), 'origins')
    for d in manifest['origins'].values(): descriptor_check(d)
    for n in FILES:
        sha(manifest['files'][n]); require(digest(raw[n]) == manifest['files'][n], 'PAYLOAD_TAMPER', file=n)
    require(manifest['files'][FILES[0]] == manifest['origins']['profile']['expected_sha256'] and manifest['files'][FILES[1]] == manifest['origins']['inventory']['expected_sha256'], 'ORIGIN_HASH')
    profile, inv, sem, prov = (parse(raw[n]) for n in FILES)
    profile_check(profile); inventory_check(inv); semantics_check(sem, profile, inv); provenance_check(prov)
    require(profile['character_id'] == inv['character_id'] == prov['character_id'] == manifest['character_id'], 'CHARACTER_ID')
    require(profile['rig']['object'] == inv['rig'], 'RIG_IDENTITY')
    return {'manifest': manifest, 'profile': profile, 'inventory': inv, 'semantics': sem, 'provenance': prov}


def export(spec, destination):
    """Atomic immutable directory publication. Existing destinations are never overwritten."""
    manifest, raw = create(spec)
    p = absolute_path(destination)
    require(not p.exists(), 'EXISTS', path=str(p))
    require(p.parent.is_dir(), 'PARENT_DIRECTORY')
    temp = Path(tempfile.mkdtemp(prefix='.template-', dir=p.parent))
    try:
        for n, b in {**raw, 'manifest.json': encode(manifest)}.items():
            with (temp / n).open('xb') as stream:
                stream.write(b); stream.flush(); os.fsync(stream.fileno())
        require(not p.exists(), 'EXISTS')
        os.rename(temp, p)
    finally:
        if temp.exists(): shutil.rmtree(temp)
    return inspect(p)


def inspect(directory, expected=None):
    p = absolute_path(directory)
    require(p.is_dir() and not p.is_symlink(), 'BUNDLE_DIRECTORY')
    require({f.name for f in p.iterdir()} == set(FILES) | {'manifest.json'}, 'BUNDLE_FILES')
    raw_manifest = read(p / 'manifest.json', expected)
    data = validate(parse(raw_manifest), {n: read(p / n) for n in FILES})
    data['reference'] = {'directory': str(p), 'manifest_sha256': digest(raw_manifest)}
    return data


def _ref(ref):
    exact(ref, ('directory', 'manifest_sha256'), 'bundle_reference')
    sha(ref['manifest_sha256']); text(ref['directory'])
    require(Path(ref['directory']).is_absolute(), 'ABSOLUTE_PATH')
    return inspect(ref['directory'], ref['manifest_sha256'])


def atomic_write(path, value, expected=None):
    """CAS checked immediately before atomic replace; lock excludes own writers."""
    p = absolute_path(path)
    lock = Path(str(p) + '.lock')
    require(p.parent.is_dir(), 'PARENT_DIRECTORY')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise BundleError('BUSY') from error
    os.close(fd)
    temp = None
    try:
        if expected is None: require(not p.exists(), 'EXISTS')
        else: read(p, expected)
        fd, name = tempfile.mkstemp(prefix='.instance-', dir=p.parent)
        temp = Path(name)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encode(value)); stream.flush(); os.fsync(stream.fileno())
        if expected is None: require(not p.exists(), 'EXISTS')
        else: read(p, expected)
        os.replace(temp, p); temp = None
    finally:
        if temp is not None: temp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return descriptor(p)


def instance_check(value):
    exact(value, ('schema', 'template', 'target', 'generation', 'bindings', 'base_settings', 'settings', 'scope'), 'instance')
    require(value['schema'] == INSTANCE and value['scope'] == 'metadata_only_no_native_or_skin_writes', 'INSTANCE_SCOPE')
    require(type(value['generation']) is int and value['generation'] >= 1, 'GENERATION')
    source, target = _ref(value['template']), _ref(value['target'])
    roles = source['semantics']['roles']
    settings_check(value['base_settings'], roles); settings_check(value['settings'], roles)
    require(value['base_settings'] == source['semantics']['settings'], 'BASE_TAMPER')
    exact(value['bindings'], ('roles', 'expressions'), 'bindings')
    require(value['bindings']['roles'] == target['semantics']['roles'] and set(roles) == set(value['bindings']['roles']), 'BINDING_ROLES')
    mapping = value['bindings']['expressions']
    require(type(mapping) is dict and set(mapping) == set(source['semantics']['expressions']), 'EXPRESSION_COVERAGE')
    for source_id, target_id in mapping.items():
        require(target_id in target['semantics']['expressions'], 'EXPRESSION_TARGET')
        require(source['semantics']['expressions'][source_id]['meaning'] == target['semantics']['expressions'][target_id]['meaning'], 'EXPRESSION_MEANING')
    return source, target


def instantiate(source_ref, target_ref, mapping, destination):
    """Explicitly map a source template into an existing character's metadata."""
    source, target = _ref(source_ref), _ref(target_ref)
    exact(mapping, ('roles', 'expressions'), 'mapping')
    result = {'schema': INSTANCE, 'template': source_ref, 'target': target_ref, 'generation': 1,
              'bindings': copy.deepcopy(mapping), 'base_settings': copy.deepcopy(source['semantics']['settings']),
              'settings': copy.deepcopy(source['semantics']['settings']), 'scope': 'metadata_only_no_native_or_skin_writes'}
    instance_check(result)
    return atomic_write(destination, result)


def override(path, expected, edits):
    value = parse(read(path, expected)); instance_check(value)
    require(type(edits) is list and bool(edits), 'OVERRIDES')
    seen = set()
    for edit in edits:
        exact(edit, ('path', 'value'), 'edit')
        keys = edit['path']
        require(type(keys) is list and all(type(k) is str for k in keys), 'OVERRIDE_PATH')
        require((len(keys) == 3 and keys[0] == 'controls' and keys[1] in value['settings']['controls'] and keys[2] in ('label', 'visible', 'gain')) or (len(keys) == 2 and keys[0] == 'user_notes'), 'OVERRIDE_PATH')
        require(tuple(keys) not in seen, 'DUPLICATE_OVERRIDE'); seen.add(tuple(keys))
        node = value['settings']
        for key in keys[:-1]: node = node[key]
        node[keys[-1]] = edit['value']
    value['generation'] += 1
    instance_check(value)
    return atomic_write(path, value, expected)


def _merge(base, user, incoming, path, conflicts):
    if user == base: return copy.deepcopy(incoming) if incoming is not ABSENT else ABSENT
    if incoming == base or user == incoming: return copy.deepcopy(user) if user is not ABSENT else ABSENT
    if all(type(v) is dict for v in (base, user, incoming)):
        result = {}
        for key in sorted(set(base) | set(user) | set(incoming)):
            merged = _merge(base.get(key, ABSENT), user.get(key, ABSENT), incoming.get(key, ABSENT), path + [key], conflicts)
            if merged is not ABSENT: result[key] = merged
        return result
    conflicts.append({'path': path, 'base': None if base is ABSENT else base, 'user': None if user is ABSENT else user, 'incoming': None if incoming is ABSENT else incoming,
                      'missing': [n for n, v in [('base', base), ('user', user), ('incoming', incoming)] if v is ABSENT]})
    return user


def upgrade(path, expected, incoming_ref):
    """Three-way settings upgrade. Any conflict returns a report and zero writes."""
    value = parse(read(path, expected)); source, target = instance_check(value)
    incoming = _ref(incoming_ref)
    before, after = source['manifest'], incoming['manifest']
    require(before['template_id'] == after['template_id'] and before['character_id'] == after['character_id'] and after['revision'] > before['revision'], 'UPGRADE_IDENTITY')
    # Changing native bindings, shapes, weights or provenance needs a reviewed
    # re-instantiation; this is deliberately a local-settings revision merge.
    for section in ('profile', 'inventory', 'provenance'):
        require(source[section] == incoming[section], 'UPGRADE_REBIND_REQUIRED', section=section)
    for section in ('roles', 'expressions', 'weights'):
        require(source['semantics'][section] == incoming['semantics'][section], 'UPGRADE_REBIND_REQUIRED', section=section)
    conflicts = []
    merged = _merge(value['base_settings'], value['settings'], incoming['semantics']['settings'], [], conflicts)
    if conflicts:
        read(path, expected)
        return {'status': 'conflict', 'writes': 0, 'conflicts': conflicts, 'instance': descriptor(path)}
    value.update(template=incoming_ref, base_settings=copy.deepcopy(incoming['semantics']['settings']), settings=merged, generation=value['generation'] + 1)
    instance_check(value)
    desc = atomic_write(path, value, expected)
    return {'status': 'applied', 'writes': 1, 'conflicts': [], 'instance': desc}


def compile_batch(instance, target_package, role_requests, defaults):
    """Pure conversion of a validated instance into bounded target-native inputs.

    ``instance`` must have passed instance_check, and ``target_package`` is the
    result of SHA-checked inspect. This function does no I/O and never imports
    native_controls. The transport must revalidate both pinned documents before
    calling it; arbitrary dictionaries are not an authentication mechanism.
    Amplitude is a bounded preset multiplier, not anatomical normalization.
    Hidden controls remain callable: visibility is display information only.
    """
    exact(instance, ('schema', 'template', 'target', 'generation', 'bindings', 'base_settings', 'settings', 'scope'), 'instance')
    require(instance['schema'] == INSTANCE and instance['scope'] == 'metadata_only_no_native_or_skin_writes', 'INSTANCE_SCOPE')
    require(type(instance['generation']) is int and instance['generation'] >= 1, 'GENERATION')
    exact(target_package, ('manifest', 'profile', 'inventory', 'semantics', 'provenance', 'reference'), 'target_package')
    require(instance['target'] == target_package['reference'], 'COMPILE_TARGET_IDENTITY')
    # Re-check the data needed for compilation while preserving opaque profile
    # fields. Filesystem/hash authentication belongs to the inspect transport.
    profile = target_package['profile']; profile_check(profile)
    inventory_check(target_package['inventory'])
    semantics_check(target_package['semantics'], profile, target_package['inventory'])
    provenance_check(target_package['provenance'])
    require(target_package['manifest']['character_id'] == profile['character_id'], 'CHARACTER_ID')
    source_ids = set(instance['base_settings']['controls'])
    settings_check(instance['base_settings'], source_ids); settings_check(instance['settings'], source_ids)
    exact(instance['bindings'], ('roles', 'expressions'), 'bindings')
    binding = instance['bindings']['roles']
    require(type(binding) is dict and set(binding) == source_ids, 'BINDING_ROLES')
    roles = {r['role_id']: r for r in profile['roles']}
    allowed_kinds = {'location_delta', 'euler_delta', 'quaternion_delta', 'scale_set', 'mode_enum'}
    require(type(defaults) is dict and set(defaults) == set(roles), 'DEFAULT_COVERAGE')
    for r in roles.values():
        require(r.get('kind') in allowed_kinds, 'COMPILE_KIND')
        require(type(r.get('bounds')) is list and len(r['bounds']) == 2, 'INPUT_BOUNDS')
        for bound in r['bounds']: number(bound)
        require(r['bounds'][0] <= r['bounds'][1], 'INPUT_BOUNDS')
        number(defaults[r['role_id']], r['bounds'][0], r['bounds'][1])
        if r['kind'] == 'mode_enum':
            require(defaults[r['role_id']] in (0, 1), 'MODE_ENUM')
            text(r.get('property'))
        require(type(r.get('requires')) is list and len(r['requires']) <= 4, 'MODE_REQUIREMENTS')
        for guard in r['requires']:
            exact(guard, ('bone', 'property', 'value'), 'guard')
            text(guard['bone']); text(guard['property'])
            require(type(guard['value']) in (int, float) and guard['value'] in (0, 1), 'MODE_REQUIREMENTS')
    require(type(role_requests) is list and 1 <= len(role_requests) <= 24, 'BATCH_SCHEMA')
    explicit = {}; display = []
    for item in role_requests:
        exact(item, ('role_id', 'amplitude'), 'role_request')
        source_id = text(item['role_id']); number(item['amplitude'], 0, 1)
        require(source_id in source_ids, 'ROLE_UNSUPPORTED', role=source_id)
        bone = binding[source_id]
        matches = [r for r in roles.values() if r['bone'] == bone]
        same_id = [r for r in matches if r['role_id'] == source_id]
        if same_id: matches = same_id
        require(len(matches) == 1, 'TARGET_ROLE_AMBIGUOUS', role=source_id)
        role = matches[0]; role_id = role['role_id']; preset = defaults[role_id]
        settings = instance['settings']['controls'][source_id]
        factor = item['amplitude'] * settings['gain']
        if role['kind'] == 'mode_enum': result = preset
        elif role['kind'] == 'scale_set': result = 1 + (preset - 1) * factor
        else: result = preset * factor
        number(result, role['bounds'][0], role['bounds'][1])
        if role_id in explicit:
            require(explicit[role_id] == result, 'DUPLICATE_ROLE_CONFLICT', role=source_id)
            continue
        explicit[role_id] = result
        display.append({'source_role': source_id, 'target_role': role_id, 'label': settings['label'],
                        'visible': settings['visible'], 'gain': settings['gain'], 'amplitude': item['amplitude'],
                        'primitive': role['kind'], 'value': result})
    required = {}
    def add_guards(role_id, stack):
        require(role_id not in stack, 'MODE_GUARD_CYCLE')
        for guard in roles[role_id]['requires']:
            candidates = [r for r in roles.values() if r['kind'] == 'mode_enum' and r['bone'] == guard['bone'] and r.get('property') == guard['property']]
            require(len(candidates) == 1, 'MODE_GUARD_UNMAPPED', guard=guard)
            mode_id = candidates[0]['role_id']; value = guard['value']
            number(value, roles[mode_id]['bounds'][0], roles[mode_id]['bounds'][1])
            require(mode_id not in explicit or explicit[mode_id] == value, 'MODE_GUARD_CONFLICT', role=mode_id)
            require(mode_id not in required or required[mode_id] == value, 'MODE_GUARD_CONFLICT', role=mode_id)
            if mode_id not in required:
                add_guards(mode_id, stack + [role_id]); required[mode_id] = value
    for role_id in explicit: add_guards(role_id, [])
    combined = {**required, **explicit}
    require(1 <= len(combined) <= 24, 'BATCH_LIMIT')
    destinations = set()
    for role_id in combined:
        r = roles[role_id]
        destination = (r['bone'], 'property', r.get('property')) if r['kind'] == 'mode_enum' else (r['bone'], 'transform')
        require(destination not in destinations, 'OVERLAPPING_INPUT', role=role_id)
        destinations.add(destination)
    return {'schema': 'hrig.compiled-template-batch.v1', 'template': copy.deepcopy(instance['template']),
            'target': copy.deepcopy(instance['target']), 'instance_generation': instance['generation'],
            'target_character_id': profile['character_id'],
            'operations': [{'role': role_id, 'value': value} for role_id, value in combined.items()],
            'displaylabels': display, 'inserted_mode_guards': [{'role': k, 'value': v} for k, v in required.items() if k not in explicit],
            'scope': 'target_native_preset_amplitude_not_anatomical_normalization',
            'visibility_policy': 'display_only_not_authorization'}
