"""Pure JSON contract for bounded, three-way bone-collection templates.

No Blender imports or scene writes. Native names are validated before Blender
can truncate them. Memberships and names are indivisible merge atoms.
"""

import copy
import hashlib
import json
import math
import re
import unicodedata


TEMPLATE_SCHEMA = "hrig.bone-collections.template.v1"
REQUEST_SCHEMA = "hrig.bone-collections.apply.v1"


class CollectionError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = {} if details is None else details


def _fail(code, message, path, **details):
    raise CollectionError(code, message, {"path": path, **details})


def _object(value, path):
    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail("COLLECTION_SCHEMA", "Expected a JSON object with string keys", path)
    return value


def _keys(value, expected, path):
    _object(value, path)
    if set(value) != set(expected):
        _fail("COLLECTION_SCHEMA", "Object keys do not match the contract", path,
              missing=sorted(set(expected) - set(value)), extra=sorted(set(value) - set(expected)))


def _identifier(value, path):
    if (type(value) is not str or not value.strip() or len(value) > 128
            or any(unicodedata.category(char).startswith("C") for char in value)):
        _fail("COLLECTION_VALUE", "Expected a readable nonempty identifier of at most 128 characters", path)
    return value


def _name(value, path):
    _identifier(value, path)
    if len(value.encode("utf-8")) > 63:
        _fail("COLLECTION_VALUE", "Native names must fit within 63 UTF-8 bytes", path)
    return value


def _unique_strings(value, path, validator, maximum):
    if type(value) is not list or len(value) > maximum:
        _fail("COLLECTION_SCHEMA", "Expected a bounded JSON list", path, maximum=maximum)
    result = [validator(item, "%s[%d]" % (path, index)) for index, item in enumerate(value)]
    if len(set(result)) != len(result):
        _fail("COLLECTION_VALUE", "List entries must be unique", path)
    return sorted(result)


def canonical_template(value):
    """Validate all fields and return independent deterministic JSON."""
    _keys(value, ("schema", "template_id", "revision", "roles", "detach"), "template")
    if value["schema"] != TEMPLATE_SCHEMA:
        _fail("COLLECTION_SCHEMA", "Unsupported bone-collection template schema", "template.schema")
    template_id = _identifier(value["template_id"], "template.template_id")
    revision = value["revision"]
    if type(revision) is not int or not 1 <= revision <= 1000000:
        _fail("COLLECTION_VALUE", "Revision must be an integer from 1 through 1000000", "template.revision")
    roles = _object(value["roles"], "template.roles")
    if not 1 <= len(roles) <= 32:
        _fail("COLLECTION_SCHEMA", "A template must have 1 through 32 collection roles", "template.roles")
    for role in roles:
        _identifier(role, "template.roles.<role>")
    canonical_roles, all_members, names = {}, set(), set()
    for role in sorted(roles):
        path = "template.roles." + role
        record = roles[role]
        _keys(record, ("name", "parent", "is_visible", "members"), path)
        name = _name(record["name"], path + ".name")
        if name in names:
            _fail("COLLECTION_VALUE", "Desired collection names must be unique", path + ".name")
        names.add(name)
        parent = record["parent"]
        if parent is not None:
            _identifier(parent, path + ".parent")
            if parent not in roles or parent == role:
                _fail("COLLECTION_STRUCTURE", "Parent must reference a different collection role", path + ".parent")
        if type(record["is_visible"]) is not bool:
            _fail("COLLECTION_VALUE", "Collection visibility must be a JSON boolean", path + ".is_visible")
        members = _unique_strings(record["members"], path + ".members", _identifier, 256)
        all_members.update(members)
        canonical_roles[role] = {"name": name, "parent": parent,
                                 "is_visible": record["is_visible"], "members": members}
    if not 1 <= len(all_members) <= 256:
        _fail("COLLECTION_SCHEMA", "The member-role union must contain 1 through 256 entries", "template.roles")
    for role in canonical_roles:
        seen, cursor = set(), role
        while cursor is not None:
            if cursor in seen:
                _fail("COLLECTION_STRUCTURE", "Collection parent graph must be acyclic", "template.roles", role=role)
            seen.add(cursor)
            cursor = canonical_roles[cursor]["parent"]
    detach = _object(value["detach"], "template.detach")
    canonical_detach, pairs = {}, 0
    for bone_role in sorted(detach):
        _identifier(bone_role, "template.detach.<bone_role>")
        if bone_role not in all_members:
            _fail("COLLECTION_STRUCTURE", "Detached bone roles must occur in template members", "template.detach", role=bone_role)
        external = _unique_strings(detach[bone_role], "template.detach." + bone_role, _name, 1024)
        if not external:
            _fail("COLLECTION_SCHEMA", "Each detach entry must name at least one external collection", "template.detach." + bone_role)
        if set(external).intersection(names):
            _fail("COLLECTION_STRUCTURE", "Detach names must not be desired collection names", "template.detach." + bone_role)
        pairs += len(external)
        if pairs > 1024:
            _fail("COLLECTION_SCHEMA", "At most 1024 explicit detach pairs are allowed", "template.detach")
        canonical_detach[bone_role] = external
    return {"schema": TEMPLATE_SCHEMA, "template_id": template_id, "revision": revision,
            "roles": canonical_roles, "detach": canonical_detach}


def canonical_bindings(bindings, template):
    template = canonical_template(template)
    _object(bindings, "bindings")
    members = {member for record in template["roles"].values() for member in record["members"]}
    if set(bindings) != members:
        _fail("COLLECTION_BINDINGS", "Bindings must exactly cover the member-role union", "bindings")
    result = {role: _name(bindings[role], "bindings." + role) for role in sorted(bindings)}
    if len(set(result.values())) != len(result):
        _fail("COLLECTION_BINDINGS", "Each semantic bone role must bind a different native bone", "bindings")
    return result


def canonical_request(value):
    _keys(value, ("schema", "instance_id", "template", "bindings", "expected_snapshot_sha256"), "request")
    if value["schema"] != REQUEST_SCHEMA:
        _fail("COLLECTION_SCHEMA", "Unsupported bone-collection request schema", "request.schema")
    instance_id = _identifier(value["instance_id"], "request.instance_id")
    template = canonical_template(value["template"])
    bindings = canonical_bindings(value["bindings"], template)
    expected = value["expected_snapshot_sha256"]
    if type(expected) is not str or re.fullmatch("[0-9a-f]{64}", expected) is None:
        _fail("COLLECTION_VALUE", "Expected a lowercase SHA-256 digest", "request.expected_snapshot_sha256")
    return {"schema": REQUEST_SCHEMA, "instance_id": instance_id, "template": template,
            "bindings": bindings, "expected_snapshot_sha256": expected}


def _key(*tokens):
    return json.dumps(list(tokens), ensure_ascii=False, separators=(",", ":"))


def atoms(template, bindings):
    """Flatten writable fields; parent hierarchy remains structural metadata."""
    template = canonical_template(template)
    bindings = canonical_bindings(bindings, template)
    result = {}
    for role, record in template["roles"].items():
        result[_key("name", role)] = record["name"]
        result[_key("visible", role)] = record["is_visible"]
        for member in bindings:
            result[_key("member", role, member)] = member in record["members"]
    for member, collections in template["detach"].items():
        for collection in collections:
            result[_key("detach", collection, member)] = False
    return dict(sorted(result.items()))


def _merge_input(value, path):
    _object(value, path)
    if not 1 <= len(value) <= 9280:
        _fail("COLLECTION_SCHEMA", "Merge atoms must be nonempty and bounded", path)
    result = {}
    for key in sorted(value):
        try:
            tokens = json.loads(key)
        except (ValueError, TypeError):
            _fail("COLLECTION_SCHEMA", "Atom key must be a JSON token list", path, key=key)
        if (type(tokens) is not list or not tokens or any(type(t) is not str for t in tokens)
                or key != _key(*tokens)):
            _fail("COLLECTION_SCHEMA", "Atom key must be a canonical JSON token list", path, key=key)
        kind = tokens[0]
        if kind not in ("name", "visible", "member", "detach") or len(tokens) != (2 if kind in ("name", "visible") else 3):
            _fail("COLLECTION_SCHEMA", "Unknown atom kind or arity", path, key=key)
        for token in tokens[1:]:
            _identifier(token, path + ".key")
        if kind == "detach":
            _name(tokens[1], path + ".key")
        item = value[key]
        if kind == "name":
            _name(item, path + "." + key)
        elif type(item) is not bool:
            _fail("COLLECTION_VALUE", "Visibility and membership atoms must be JSON booleans", path, key=key)
        result[key] = item
    return result


def merge_atoms(base, current, target):
    """Merge atomically, retaining current values for every reported conflict.

    Callers must reject the whole mutation on any conflict. Native preflight
    separately checks the merged name set against all existing collections.
    """
    base, current, target = (_merge_input(v, p) for v, p in ((base, "base"), (current, "current"), (target, "target")))
    if set(base) != set(current) or set(base) != set(target):
        _fail("COLLECTION_STRUCTURE", "The merge atom set must remain fixed", "merge")
    values, conflicts, preserved = {}, [], []
    for key in base:
        b, c, t = base[key], current[key], target[key]
        if c == b:
            chosen = t
        elif t == b:
            chosen = c
            preserved.append({"key": key})
        elif c == t:
            chosen = t
        else:
            chosen = c
            conflicts.append({"key": key, "base": copy.deepcopy(b), "current": copy.deepcopy(c), "target": copy.deepcopy(t)})
        values[key] = copy.deepcopy(chosen)
    # A merged name collision is itself an atomic conflict, even if different
    # roles were edited independently. Do not defer self-collisions to Blender.
    owners = {}
    for key, value in values.items():
        tokens = json.loads(key)
        if tokens[0] == "name":
            owners.setdefault(value, []).append(key)
    for name, keys in sorted(owners.items()):
        if len(keys) > 1:
            conflicts.append({"kind": "name_collision", "name": name, "keys": keys})
    return {"values": values, "conflicts": conflicts, "preserved": preserved}


def digest(value):
    """Deterministic UTF-8 JSON SHA-256; only finite native JSON is accepted."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for nested in item:
                check(nested)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for nested in item.values():
                check(nested)
            return
        raise CollectionError("COLLECTION_VALUE", "Digest input must be finite JSON", {})
    check(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CollectionError("COLLECTION_VALUE", "Digest input cannot be encoded as UTF-8 JSON", {}) from exc
