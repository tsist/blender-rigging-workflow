"""Pure JSON contract for bounded controller-style templates.

No bpy import or scene mutation occurs here. Vectors are single merge atoms.
Shape values use float32; bone colors use the verified Blender 5.2.1 byte-color
conversion and float32 getter grid before comparison.
"""

import copy
import hashlib
import json
import math
import re
import struct
import unicodedata


TEMPLATE_SCHEMA = "hrig.control-style.template.v1"
REQUEST_SCHEMA = "hrig.control-style.apply.v1"
PALETTES = ("DEFAULT",) + tuple("THEME%02d" % n for n in range(1, 21)) + ("CUSTOM",)
COLOR_FIELDS = tuple(
    "%s.color.custom.%s" % (owner, channel)
    for owner in ("bone", "pose") for channel in ("normal", "select", "active")
)
PALETTE_FIELDS = ("bone.color.palette", "pose.color.palette")
VECTOR_LIMITS = {name: (0.0, 1.0) for name in COLOR_FIELDS}
VECTOR_LIMITS.update({
    "pose.custom_shape_scale_xyz": (-100.0, 100.0),
    "pose.custom_shape_translation": (-100.0, 100.0),
})
BOOL_FIELDS = ("pose.use_custom_shape_bone_size", "bone.show_wire")
ALLOWED_FIELDS = frozenset(PALETTE_FIELDS + tuple(VECTOR_LIMITS) + BOOL_FIELDS)
_COLOR_BYTE_FACTOR = struct.unpack("!f", struct.pack("!f", 1.0 / 255.0))[0]


class StyleError(ValueError):
    """A stable machine-readable rejection with an explanatory message."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = {} if details is None else details


def _fail(code, message, path, **details):
    raise StyleError(code, message, {"path": path, **details})


def _object(value, path):
    if type(value) is not dict:
        _fail("STYLE_SCHEMA", "Expected a JSON object", path)
    return value


def _keys(value, expected, path):
    _object(value, path)
    if set(value) != set(expected):
        # repr permits a useful error even when callers supplied non-JSON keys.
        _fail("STYLE_SCHEMA", "Object keys do not match the contract", path,
              missing=sorted(set(expected) - set(value)),
              extra=sorted((repr(k) for k in set(value) - set(expected))))


def _identifier(value, path):
    if (type(value) is not str or not value.strip() or len(value) > 128
            or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value)):
        _fail("STYLE_VALUE", "Expected a readable nonempty string of at most 128 characters", path)
    return value


def _number(value, minimum, maximum, path):
    if type(value) not in (int, float):
        _fail("STYLE_VALUE", "Expected a finite JSON number (not bool)", path)
    try:
        number = float(value)
    except (ValueError, OverflowError):
        _fail("STYLE_VALUE", "Number cannot be represented", path)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        _fail("STYLE_VALUE", "Number is outside the permitted finite range", path,
              minimum=minimum, maximum=maximum)
    result = struct.unpack("!f", struct.pack("!f", number))[0]
    return 0.0 if result == 0.0 else result


def _color_component(component):
    # Setter first receives float32; the getter multiplies its byte by the
    # float32 reciprocal of 255. Setter multiplication AND addition round to
    # float32; double intermediates disagree at native half-byte boundaries.
    scaled = struct.unpack("!f", struct.pack("!f", component * 255.0))[0]
    rounded = struct.unpack("!f", struct.pack("!f", scaled + 0.5))[0]
    byte = math.floor(rounded)
    return struct.unpack("!f", struct.pack("!f", byte * _COLOR_BYTE_FACTOR))[0]


def _field(name, value, path, native_current=False):
    if name not in ALLOWED_FIELDS:
        _fail("STYLE_SCHEMA", "Style field is not allowed", path, field=repr(name))
    if name in PALETTE_FIELDS:
        if type(value) is not str or value not in PALETTES:
            _fail("STYLE_VALUE", "Unknown bone color palette", path)
        return value
    if name in BOOL_FIELDS:
        if type(value) is not bool:
            _fail("STYLE_VALUE", "Expected a JSON boolean", path)
        return value
    if type(value) is not list or len(value) != 3:
        _fail("STYLE_VALUE", "Expected a JSON vector with exactly three components", path)
    minimum, maximum = VECTOR_LIMITS[name]
    if native_current and name in ("pose.custom_shape_scale_xyz", "pose.custom_shape_translation"):
        minimum, maximum = -3.4028234663852886e38, 3.4028234663852886e38
    result = [_number(v, minimum, maximum, "%s[%d]" % (path, i)) for i, v in enumerate(value)]
    if name in COLOR_FIELDS:
        result = [_color_component(v) for v in result]
    if name == "pose.custom_shape_scale_xyz" and not native_current:
        if any(abs(float(v)) < 0.001 for v in value):
            _fail("STYLE_VALUE", "Each scale component must have absolute value at least 0.001", path)
    return result


def _fields(value, path, enforce_color_groups=False, native_current=False):
    _object(value, path)
    if not value:
        _fail("STYLE_SCHEMA", "At least one style field is required", path)
    if any(type(k) is not str for k in value):
        _fail("STYLE_SCHEMA", "Field names must be strings", path)
    result = {name: _field(name, value[name], path + "." + name, native_current) for name in sorted(value)}
    if enforce_color_groups:
        for owner in ("bone", "pose"):
            palette = owner + ".color.palette"
            colors = {owner + ".color.custom." + name for name in ("normal", "select", "active")}
            present = colors.intersection(result)
            if result.get(palette) == "CUSTOM":
                if present != colors:
                    _fail("STYLE_SCHEMA", "CUSTOM palette requires all three RGB fields", path,
                          missing=sorted(colors - present))
            elif present:
                _fail("STYLE_SCHEMA", "Custom RGB fields require the corresponding CUSTOM palette", path,
                      fields=sorted(present), palette=palette)
    return result


def canonical_template(value):
    """Validate and return an independent deterministic JSON template."""
    _keys(value, ("schema", "template_id", "revision", "roles"), "template")
    if value["schema"] != TEMPLATE_SCHEMA:
        _fail("STYLE_SCHEMA", "Unsupported style template schema", "template.schema")
    template_id = _identifier(value["template_id"], "template.template_id")
    revision = value["revision"]
    if type(revision) is not int or not 1 <= revision <= 1000000:
        _fail("STYLE_VALUE", "Revision must be an integer from 1 through 1000000", "template.revision")
    roles = _object(value["roles"], "template.roles")
    if not 1 <= len(roles) <= 64:
        _fail("STYLE_SCHEMA", "A template must contain 1 through 64 roles", "template.roles")
    for role in roles:
        _identifier(role, "template.roles.<role>")
    canonical_roles = {}
    for role in sorted(roles):
        path = "template.roles." + role
        _keys(roles[role], ("fields",), path)
        canonical_roles[role] = {"fields": _fields(roles[role]["fields"], path + ".fields", True)}
    return {"schema": TEMPLATE_SCHEMA, "template_id": template_id,
            "revision": revision, "roles": canonical_roles}


def canonical_bindings(bindings, template):
    """Require an exact, one-to-one mapping from roles to native bone names."""
    template = canonical_template(template)
    _object(bindings, "bindings")
    if set(bindings) != set(template["roles"]):
        _fail("STYLE_BINDINGS", "Bindings must cover exactly the template roles", "bindings")
    result = {role: _identifier(bindings[role], "bindings." + role) for role in sorted(bindings)}
    if len(set(result.values())) != len(result):
        _fail("STYLE_BINDINGS", "Each role must resolve to a different bone", "bindings")
    return result


def canonical_request(req):
    _keys(req, ("schema", "instance_id", "template", "bindings", "expected_snapshot_sha256"), "request")
    if req["schema"] != REQUEST_SCHEMA:
        _fail("STYLE_SCHEMA", "Unsupported style request schema", "request.schema")
    instance_id = _identifier(req["instance_id"], "request.instance_id")
    template = canonical_template(req["template"])
    bindings = canonical_bindings(req["bindings"], template)
    expected = req["expected_snapshot_sha256"]
    if type(expected) is not str or re.fullmatch("[0-9a-f]{64}", expected) is None:
        _fail("STYLE_VALUE", "Expected a lowercase SHA-256 digest", "request.expected_snapshot_sha256")
    return {"schema": REQUEST_SCHEMA, "instance_id": instance_id, "template": template,
            "bindings": bindings, "expected_snapshot_sha256": expected}


def _merge_input(value, path, native_current=False):
    _object(value, path)
    if not 1 <= len(value) <= 64:
        _fail("STYLE_SCHEMA", "Merge input must contain 1 through 64 roles", path)
    for role in value:
        _identifier(role, path + ".<role>")
    return {role: _fields(value[role], path + "." + role, native_current=native_current) for role in sorted(value)}


def merge_fields(base, current, target):
    """Three-way merge role/field maps, reporting every conflicting field.

    Current observations may legitimately contain custom RGB values while their
    palette was manually changed, so only templates impose color-group rules.
    Conflicting values remain at current in the result; callers must refuse the
    complete mutation when conflicts is nonempty.
    """
    base = _merge_input(base, "base")
    current = _merge_input(current, "current", native_current=True)
    target = _merge_input(target, "target")
    for label, candidate in (("current", current), ("target", target)):
        if (set(base) != set(candidate)
                or any(set(base[r]) != set(candidate[r]) for r in base)):
            _fail("STYLE_STRUCTURE", "Role and field sets must remain fixed", label)
    values, conflicts, preserved = {}, [], []
    for role in base:
        values[role] = {}
        for field in base[role]:
            b, c, t = base[role][field], current[role][field], target[role][field]
            if c == b:
                chosen = t
            elif t == b:
                chosen = c
                preserved.append({"role": role, "field": field})
            elif c == t:
                chosen = t
            else:
                chosen = c
                conflicts.append({"role": role, "field": field, "base": copy.deepcopy(b),
                                  "current": copy.deepcopy(c), "target": copy.deepcopy(t)})
            values[role][field] = copy.deepcopy(chosen)
    return {"values": values, "conflicts": conflicts, "preserved": preserved}


def digest(value):
    """Hash deterministic UTF-8 JSON; NaN and non-JSON containers are rejected."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for nested in item:
                check(nested)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for nested in item.values():
                check(nested)
            return
        raise StyleError("STYLE_VALUE", "Digest input must be finite JSON", {})
    check(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StyleError("STYLE_VALUE", "Digest input cannot be encoded as UTF-8 JSON", {}) from exc
