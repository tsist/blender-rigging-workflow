"""Host-only semantic skeleton/controller template contract.

This module validates semantic intent and computes merge plans.  It does not
import Blender, create bones, write properties, bind weights, or claim that a
merge plan has been applied.  A native backend must explicitly perform writes
and then record the observed values with :func:`make_ledger`.

Only Python's standard library is used, and every public function returns plain
JSON-compatible dictionaries on success.
"""

from __future__ import annotations

import copy
import json
import math
import struct
import unicodedata


TEMPLATE_SCHEMA = "hrig.semantic-template.v1"
LEDGER_SCHEMA = "hrig.semantic-ledger.v1"
BINDING_REPORT_SCHEMA = "hrig.semantic-bindings.v1"
EXTRACTION_SCHEMA = "hrig.semantic-extraction.v1"
MERGE_PLAN_SCHEMA = "hrig.semantic-merge-plan.v1"

PURPOSES = frozenset(("control", "deform", "mechanism"))
SIDES = frozenset(("C", "L", "R"))
ROTATION_MODES = frozenset(
    ("QUATERNION", "XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
)
INHERIT_SCALES = frozenset(
    ("FULL", "FIX_SHEAR", "ALIGNED", "AVERAGE", "NONE", "NONE_LEGACY")
)

FIELD_NAMES = (
    "head",
    "tail",
    "roll",
    "use_deform",
    "rotation_mode",
    "lock_location",
    "lock_rotation",
    "lock_scale",
    "inherit_scale",
)

_TEMPLATE_KEYS = ("schema", "template_id", "revision", "roles")
_ROLE_KEYS = ("role", "purpose", "parent_role", "side", "fields")
_LEDGER_KEYS = ("schema", "template", "bindings", "applied_fields", "generation")
_INVENTORY_RECORD_KEYS = ("parent", "fields")
_EXPLICIT_ROLE_KEYS = ("role", "purpose", "parent_role", "side", "bone")
_COORD_LIMIT = 100000.0
_ROLL_LIMIT = 100.0
_MAX_IDENTIFIER = 128
_MIN_BONE_LENGTH = 1.0e-6


class ContractError(ValueError):
    """Stable machine-readable contract rejection.

    ``code`` is intended for programmatic handling; ``details`` is a plain
    dictionary containing at least a message and, where applicable, a path.
    """

    def __init__(self, code, details):
        self.code = code
        self.details = {} if details is None else details
        message = self.details.get("message", code)
        super().__init__("%s: %s" % (code, message))


def _fail(code, message, path=None, **details):
    payload = {"message": message}
    if path is not None:
        payload["path"] = path
    payload.update(details)
    raise ContractError(code, payload)


def _object(value, path, code):
    if type(value) is not dict:
        _fail(code, "Expected a JSON object", path, actual_type=type(value).__name__)
    if any(type(key) is not str for key in value):
        _fail(code, "Object keys must be strings", path)
    return value


def _exact_keys(value, expected, path, code):
    _object(value, path, code)
    expected_set = set(expected)
    actual_set = set(value)
    if actual_set != expected_set:
        _fail(
            code,
            "Object keys do not match the contract",
            path,
            missing=sorted(expected_set - actual_set),
            extra=sorted(repr(key) for key in actual_set - expected_set),
        )
    return value


def _identifier(value, path, code):
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _MAX_IDENTIFIER
        or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value)
    ):
        _fail(
            code,
            "Expected a readable nonempty string of at most %d characters"
            % _MAX_IDENTIFIER,
            path,
        )
    return value


def _f32(value, path, minimum, maximum, code):
    if type(value) not in (int, float):
        _fail(code, "Expected a finite JSON number, not a boolean or string", path)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _fail(code, "Number cannot be represented", path)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        _fail(
            code,
            "Number is outside the permitted finite range",
            path,
            minimum=minimum,
            maximum=maximum,
        )
    try:
        result = struct.unpack("!f", struct.pack("!f", number))[0]
    except (OverflowError, struct.error):
        _fail(code, "Number cannot be represented as binary32", path)
    if not math.isfinite(result):
        _fail(code, "Number cannot be represented as a finite binary32", path)
    return 0.0 if result == 0.0 else result


def _bool3(value, path, code):
    if type(value) is not list or len(value) != 3:
        _fail(code, "Expected a JSON list with exactly three booleans", path)
    if any(type(item) is not bool for item in value):
        _fail(code, "Lock components must be JSON booleans", path)
    return list(value)


def _field_value(name, value, path, code):
    if name in ("head", "tail"):
        if type(value) is not list or len(value) != 3:
            _fail(code, "Expected a JSON vector with exactly three numbers", path)
        return [
            _f32(component, "%s[%d]" % (path, index), -_COORD_LIMIT, _COORD_LIMIT, code)
            for index, component in enumerate(value)
        ]
    if name == "roll":
        return _f32(value, path, -_ROLL_LIMIT, _ROLL_LIMIT, code)
    if name == "use_deform":
        if type(value) is not bool:
            _fail(code, "Expected a JSON boolean", path)
        return value
    if name == "rotation_mode":
        if type(value) is not str or value not in ROTATION_MODES:
            _fail(code, "Unknown rotation mode", path, allowed=sorted(ROTATION_MODES))
        return value
    if name in ("lock_location", "lock_rotation", "lock_scale"):
        return _bool3(value, path, code)
    if name == "inherit_scale":
        if type(value) is not str or value not in INHERIT_SCALES:
            _fail(code, "Unknown inherit-scale mode", path, allowed=sorted(INHERIT_SCALES))
        return value
    _fail(code, "Unknown semantic field", path, field=repr(name))


def _fields(value, path, require_all, code):
    _object(value, path, code)
    if any(name not in FIELD_NAMES for name in value):
        _fail(
            code,
            "Unknown semantic fields are not supported",
            path,
            unknown=sorted(repr(name) for name in value if name not in FIELD_NAMES),
        )
    if require_all and set(value) != set(FIELD_NAMES):
        _fail(
            code,
            "All semantic fields must be present",
            path,
            missing=sorted(set(FIELD_NAMES) - set(value)),
        )
    result = {}
    for name in FIELD_NAMES:
        if name in value:
            result[name] = _field_value(name, value[name], path + "." + name, code)
    if "head" in result and "tail" in result:
        length = math.hypot(*(result["head"][axis] - result["tail"][axis] for axis in range(3)))
        if length < _MIN_BONE_LENGTH:
            _fail(
                code,
                "Head and tail must define a nonzero bone length",
                path,
                minimum_length=_MIN_BONE_LENGTH,
            )
    return result


def _purpose(value, path, code):
    if type(value) is not str or value not in PURPOSES:
        _fail(code, "Unknown semantic purpose", path, allowed=sorted(PURPOSES))
    return value


def _side(value, path, code):
    if type(value) is not str or value not in SIDES:
        _fail(code, "Unknown semantic side", path, allowed=sorted(SIDES))
    return value


def _parent_role(value, path, code):
    if value is None:
        return None
    return _identifier(value, path, code)


def validate_template(value):
    """Validate and return an independent canonical semantic template.

    Roles are sorted by role name and all floating-point intent is rounded to
    binary32.  Caller dictionaries are never mutated.  Unknown role, field, or
    constraint/driver entries are rejected rather than copied.
    """

    _exact_keys(value, _TEMPLATE_KEYS, "template", "TEMPLATE_SCHEMA")
    if value["schema"] != TEMPLATE_SCHEMA:
        _fail(
            "TEMPLATE_SCHEMA",
            "Unsupported semantic-template schema",
            "template.schema",
            expected=TEMPLATE_SCHEMA,
        )
    template_id = _identifier(
        value["template_id"], "template.template_id", "TEMPLATE_VALUE"
    )
    revision = value["revision"]
    if type(revision) is not int or not 1 <= revision <= 1000000:
        _fail(
            "TEMPLATE_VALUE",
            "Revision must be an integer from 1 through 1000000",
            "template.revision",
        )
    roles_value = value["roles"]
    if type(roles_value) is not list or not 1 <= len(roles_value) <= 256:
        _fail(
            "TEMPLATE_SCHEMA",
            "A template must contain 1 through 256 roles",
            "template.roles",
        )

    roles = {}
    for index, role_value in enumerate(roles_value):
        path = "template.roles[%d]" % index
        _exact_keys(role_value, _ROLE_KEYS, path, "TEMPLATE_SCHEMA")
        role = _identifier(role_value["role"], path + ".role", "TEMPLATE_VALUE")
        if role in roles:
            _fail("TEMPLATE_STRUCTURE", "Role names must be unique", path + ".role", role=role)
        roles[role] = {
            "role": role,
            "purpose": _purpose(role_value["purpose"], path + ".purpose", "TEMPLATE_VALUE"),
            "parent_role": _parent_role(
                role_value["parent_role"], path + ".parent_role", "TEMPLATE_VALUE"
            ),
            "side": _side(role_value["side"], path + ".side", "TEMPLATE_VALUE"),
            "fields": _fields(
                role_value["fields"], path + ".fields", True, "TEMPLATE_VALUE"
            ),
        }

    for role in sorted(roles):
        parent = roles[role]["parent_role"]
        if parent is not None and parent not in roles:
            _fail(
                "TEMPLATE_STRUCTURE",
                "Parent role must reference another role in this template",
                "template.roles",
                role=role,
                parent_role=parent,
            )

    done = set()
    visiting = set()

    def visit(role, stack):
        if role in done:
            return
        if role in visiting:
            start = stack.index(role)
            cycle = stack[start:] + [role]
            _fail(
                "TEMPLATE_STRUCTURE",
                "Role parent graph must be acyclic",
                "template.roles",
                cycle=cycle,
            )
        visiting.add(role)
        stack.append(role)
        parent = roles[role]["parent_role"]
        if parent is not None:
            visit(parent, stack)
        stack.pop()
        visiting.remove(role)
        done.add(role)

    for role in sorted(roles):
        visit(role, [])

    return {
        "schema": TEMPLATE_SCHEMA,
        "template_id": template_id,
        "revision": revision,
        "roles": [copy.deepcopy(roles[role]) for role in sorted(roles)],
    }


class _DuplicateJSONKey(Exception):
    def __init__(self, key):
        self.key = key
        super().__init__(key)


def parse_template(raw):
    """Parse strict JSON without duplicate keys and return a canonical template.

    ``raw`` must be text.  NaN/Infinity tokens, duplicate object keys, and all
    template validation failures raise :class:`ContractError`.
    """

    if type(raw) is not str:
        _fail("INVALID_JSON", "Expected JSON text", "template", actual_type=type(raw).__name__)

    def no_duplicate_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise _DuplicateJSONKey(key)
            result[key] = item
        return result

    def reject_constant(token):
        _fail(
            "NON_FINITE_JSON_NUMBER",
            "NaN and infinity are not valid template numbers",
            "template",
            token=token,
        )

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=no_duplicate_keys,
            parse_constant=reject_constant,
        )
    except _DuplicateJSONKey as exc:
        _fail(
            "DUPLICATE_JSON_KEY",
            "Duplicate JSON object keys are not allowed",
            "template",
            key=exc.key,
        )
    except json.JSONDecodeError as exc:
        _fail(
            "INVALID_JSON",
            "Template text is not valid JSON",
            "template",
            line=exc.lineno,
            column=exc.colno,
        )
    return validate_template(parsed)


def _bindings(bindings, template, path="bindings"):
    _object(bindings, path, "BINDINGS_SCHEMA")
    roles = {role["role"] for role in template["roles"]}
    missing = sorted(roles - set(bindings))
    extra = sorted(set(bindings) - roles)
    if missing or extra:
        _fail(
            "BINDING_COVERAGE",
            "Bindings must map exactly the template roles",
            path,
            missing_roles=missing,
            extra_roles=extra,
        )
    result = {}
    for role in sorted(bindings):
        result[role] = _identifier(
            bindings[role], path + "." + role, "BINDINGS_VALUE"
        )
    owners = {}
    for role, bone in result.items():
        owners.setdefault(bone, []).append(role)
    shared = [
        {"bone": bone, "roles": sorted(role_names)}
        for bone, role_names in sorted(owners.items())
        if len(role_names) > 1
    ]
    if shared:
        _fail(
            "SHARED_BONE_BINDING",
            "Each semantic role must bind a different native bone",
            path,
            shared=shared,
        )
    return result


def _inventory(inventory):
    _object(inventory, "inventory", "INVENTORY_SCHEMA")
    result = {}
    for bone in sorted(inventory):
        path = "inventory." + bone
        _identifier(bone, path, "INVENTORY_VALUE")
        record = _exact_keys(
            inventory[bone], _INVENTORY_RECORD_KEYS, path, "INVENTORY_SCHEMA"
        )
        parent = record["parent"]
        if parent is not None:
            parent = _identifier(parent, path + ".parent", "INVENTORY_VALUE")
        result[bone] = {
            "parent": parent,
            "fields": _fields(record["fields"], path + ".fields", False, "INVENTORY_VALUE"),
        }
    return result


def bind_roles(template, bindings, inventory):
    """Validate a one-to-one semantic-role binding against caller inventory.

    The inventory must contain each bound bone and all nine semantic fields for
    it.  Non-root roles must also match the native parent implied by the bound
    parent role.  Missing bones (``unmapped``) and present but unusable bones
    (``invalid``) are reported in separate lists on ``BINDING_INCOMPLETE``.
    """

    template = validate_template(template)
    bindings = _bindings(bindings, template)
    inventory = _inventory(inventory)
    invalid = []
    unmapped = []
    role_records = {role["role"]: role for role in template["roles"]}

    for role in sorted(role_records):
        bone = bindings[role]
        if bone not in inventory:
            unmapped.append(
                {
                    "role": role,
                    "bone": bone,
                    "code": "BONE_NOT_IN_INVENTORY",
                }
            )
            continue
        available = inventory[bone]["fields"]
        missing_fields = sorted(set(FIELD_NAMES) - set(available))
        if missing_fields:
            invalid.append(
                {
                    "role": role,
                    "bone": bone,
                    "code": "UNSUPPORTED_INVENTORY_FIELDS",
                    "missing_fields": missing_fields,
                }
            )
        parent_role = role_records[role]["parent_role"]
        if parent_role is not None:
            expected_parent = bindings[parent_role]
            actual_parent = inventory[bone]["parent"]
            if actual_parent != expected_parent:
                invalid.append(
                    {
                        "role": role,
                        "bone": bone,
                        "code": "PARENT_BONE_MISMATCH",
                        "parent_role": parent_role,
                        "expected_parent_bone": expected_parent,
                        "actual_parent_bone": actual_parent,
                    }
                )

    if invalid or unmapped:
        _fail(
            "BINDING_INCOMPLETE",
            "One or more roles could not be bound safely",
            "bindings",
            invalid=invalid,
            unmapped=unmapped,
            invalid_count=len(invalid),
            unmapped_count=len(unmapped),
        )

    roles = []
    for role in sorted(role_records):
        bone = bindings[role]
        parent_role = role_records[role]["parent_role"]
        roles.append(
            {
                "role": role,
                "bone": bone,
                "parent_role": parent_role,
                "parent_bone": None if parent_role is None else bindings[parent_role],
                "native_parent": inventory[bone]["parent"],
                "source": "inventory.%s.fields" % bone,
            }
        )
    return {
        "schema": BINDING_REPORT_SCHEMA,
        "template_id": template["template_id"],
        "revision": template["revision"],
        "bindings": bindings,
        "roles": roles,
        "complete": True,
        "invalid": [],
        "unmapped": [],
    }


def _explicit_roles(value):
    if type(value) is not list or not 1 <= len(value) <= 256:
        _fail(
            "EXTRACTION_SCHEMA",
            "Explicit roles must be a nonempty list of at most 256 records",
            "explicit_roles",
        )
    result = []
    seen_roles = set()
    seen_bones = {}
    for index, item in enumerate(value):
        path = "explicit_roles[%d]" % index
        _exact_keys(item, _EXPLICIT_ROLE_KEYS, path, "EXTRACTION_SCHEMA")
        role = _identifier(item["role"], path + ".role", "EXTRACTION_VALUE")
        if role in seen_roles:
            _fail(
                "EXPLICIT_ROLE_DUPLICATE",
                "Each explicit role may be declared only once",
                path + ".role",
                role=role,
            )
        seen_roles.add(role)
        bone = _identifier(item["bone"], path + ".bone", "EXTRACTION_VALUE")
        if bone in seen_bones:
            _fail(
                "SHARED_BONE_BINDING",
                "Each semantic role must bind a different native bone",
                path + ".bone",
                bone=bone,
                roles=sorted((seen_bones[bone], role)),
            )
        seen_bones[bone] = role
        result.append(
            {
                "role": role,
                "purpose": _purpose(item["purpose"], path + ".purpose", "EXTRACTION_VALUE"),
                "parent_role": _parent_role(
                    item["parent_role"], path + ".parent_role", "EXTRACTION_VALUE"
                ),
                "side": _side(item["side"], path + ".side", "EXTRACTION_VALUE"),
                "bone": bone,
            }
        )
    return sorted(result, key=lambda role: role["role"])


def extract_template(template_id, revision, explicit_roles, inventory):
    """Extract explicitly declared roles from native-shaped inventory.

    No role, bone, side, or parent is inferred.  Successfully extracted roles
    are returned both as a partial canonical template and as a role-to-bone
    map.  ``missing``, ``invalid``, and ``unmapped`` are separate lists; a
    partial report never implies a complete body or skeleton.
    """

    template_id = _identifier(template_id, "template_id", "EXTRACTION_VALUE")
    if type(revision) is not int or not 1 <= revision <= 1000000:
        _fail(
            "EXTRACTION_VALUE",
            "Revision must be an integer from 1 through 1000000",
            "revision",
        )
    declared = _explicit_roles(explicit_roles)
    inventory = _inventory(inventory)
    by_role = {role["role"]: role for role in declared}
    candidates = {}
    missing = []
    invalid = []
    unmapped = []

    for role in declared:
        name = role["role"]
        bone = role["bone"]
        if bone not in inventory:
            unmapped.append(
                {
                    "role": name,
                    "bone": bone,
                    "code": "BONE_NOT_IN_INVENTORY",
                }
            )
            continue
        fields = inventory[bone]["fields"]
        missing_fields = sorted(set(FIELD_NAMES) - set(fields))
        if missing_fields:
            invalid.append(
                {
                    "role": name,
                    "bone": bone,
                    "code": "UNSUPPORTED_INVENTORY_FIELDS",
                    "missing_fields": missing_fields,
                }
            )
            continue
        candidates[name] = role

    for role in sorted(tuple(candidates)):
        parent_role = candidates[role]["parent_role"]
        if parent_role is not None and parent_role not in by_role:
            missing.append(
                {
                    "role": role,
                    "code": "PARENT_ROLE_NOT_REQUESTED",
                    "parent_role": parent_role,
                }
            )
            candidates.pop(role, None)

    for role in sorted(tuple(candidates)):
        parent_role = candidates[role]["parent_role"]
        if parent_role is None or parent_role not in candidates:
            continue
        bone = candidates[role]["bone"]
        expected_parent = candidates[parent_role]["bone"]
        actual_parent = inventory[bone]["parent"]
        if actual_parent != expected_parent:
            invalid.append(
                {
                    "role": role,
                    "bone": bone,
                    "code": "PARENT_BONE_MISMATCH",
                    "parent_role": parent_role,
                    "expected_parent_bone": expected_parent,
                    "actual_parent_bone": actual_parent,
                }
            )
            candidates.pop(role, None)

    changed = True
    while changed:
        changed = False
        for role in sorted(tuple(candidates)):
            parent_role = candidates[role]["parent_role"]
            if parent_role is None:
                continue
            if parent_role not in by_role:
                missing.append(
                    {
                        "role": role,
                        "code": "PARENT_ROLE_NOT_REQUESTED",
                        "parent_role": parent_role,
                    }
                )
                candidates.pop(role, None)
                changed = True
            elif parent_role not in candidates:
                invalid.append(
                    {
                        "role": role,
                        "bone": candidates[role]["bone"],
                        "code": "PARENT_ROLE_UNAVAILABLE",
                        "parent_role": parent_role,
                    }
                )
                candidates.pop(role, None)
                changed = True

    while candidates:
        state = {}
        cycle = None

        def walk(role, stack):
            nonlocal cycle
            status = state.get(role, 0)
            if status == 2:
                return
            if status == 1:
                start = stack.index(role)
                cycle = stack[start:] + [role]
                return
            state[role] = 1
            stack.append(role)
            parent_role = candidates[role]["parent_role"]
            if parent_role is not None:
                walk(parent_role, stack)
            stack.pop()
            state[role] = 2

        for role in sorted(candidates):
            if role not in state:
                walk(role, [])
            if cycle is not None:
                break
        if cycle is None:
            break
        for role in sorted(set(cycle)):
            invalid.append(
                {
                    "role": role,
                    "bone": candidates[role]["bone"],
                    "code": "PARENT_ROLE_CYCLE",
                    "cycle": cycle,
                }
            )
            candidates.pop(role, None)
        changed = True
        while changed:
            changed = False
            for role in sorted(tuple(candidates)):
                parent_role = candidates[role]["parent_role"]
                if parent_role is not None and parent_role not in candidates:
                    invalid.append(
                        {
                            "role": role,
                            "bone": candidates[role]["bone"],
                            "code": "PARENT_ROLE_UNAVAILABLE",
                            "parent_role": parent_role,
                        }
                    )
                    candidates.pop(role, None)
                    changed = True

    sorted_candidates = [candidates[role] for role in sorted(candidates)]
    bindings = {role["role"]: role["bone"] for role in sorted_candidates}
    sources = [
        {
            "role": role["role"],
            "bone": role["bone"],
            "source": "inventory.%s.fields" % role["bone"],
        }
        for role in sorted_candidates
    ]
    extracted = copy.deepcopy(sources)
    template = None
    if sorted_candidates:
        template = validate_template(
            {
                "schema": TEMPLATE_SCHEMA,
                "template_id": template_id,
                "revision": revision,
                "roles": [
                    {
                        "role": role["role"],
                        "purpose": role["purpose"],
                        "parent_role": role["parent_role"],
                        "side": role["side"],
                        "fields": copy.deepcopy(inventory[role["bone"]]["fields"]),
                    }
                    for role in sorted_candidates
                ],
            }
        )

    missing.sort(key=lambda item: (item["role"], item["code"]))
    invalid.sort(key=lambda item: (item["role"], item["code"]))
    unmapped.sort(key=lambda item: (item["role"], item["code"]))
    return {
        "schema": EXTRACTION_SCHEMA,
        "template_id": template_id,
        "revision": revision,
        "requested_roles": [role["role"] for role in declared],
        "extracted": extracted,
        "missing": missing,
        "invalid": invalid,
        "unmapped": unmapped,
        "sources": sources,
        "bindings": bindings,
        "template": template,
        "complete": not missing and not invalid and not unmapped,
    }


def _generation(value):
    if type(value) is not int or value < 0:
        _fail(
            "LEDGER_VALUE",
            "Generation must be a nonnegative integer",
            "generation",
        )
    return value


def _role_field_map(value, template, path, schema_code, value_code):
    _object(value, path, schema_code)
    expected_roles = {role["role"] for role in template["roles"]}
    if set(value) != expected_roles:
        _fail(
            schema_code,
            "Role fields must cover exactly the template roles",
            path,
            missing_roles=sorted(expected_roles - set(value)),
            extra_roles=sorted(set(value) - expected_roles),
        )
    return {
        role_name: _fields(value[role_name], path + "." + role_name, True, value_code)
        for role_name in sorted(expected_roles)
    }


def make_ledger(template, bindings, actual_fields, generation):
    """Build the explicit receipt for values already observed in native state.

    Native callers must invoke this only after their own write/read-back confirms
    ``actual_fields``.  This function only validates and canonicalizes the
    receipt; it does not mutate an armature or imply that a merge was applied.
    """

    template = validate_template(template)
    bindings = _bindings(bindings, template)
    actual_fields = _role_field_map(
        actual_fields,
        template,
        "applied_fields",
        "LEDGER_APPLIED_SCHEMA",
        "LEDGER_APPLIED_VALUE",
    )
    return {
        "schema": LEDGER_SCHEMA,
        "template": copy.deepcopy(template),
        "bindings": bindings,
        "applied_fields": actual_fields,
        "generation": _generation(generation),
    }


def _ledger(previous):
    _exact_keys(previous, _LEDGER_KEYS, "previous", "LEDGER_SCHEMA")
    if previous["schema"] != LEDGER_SCHEMA:
        _fail(
            "LEDGER_SCHEMA",
            "Unsupported semantic ledger schema",
            "previous.schema",
            expected=LEDGER_SCHEMA,
        )
    template = validate_template(previous["template"])
    bindings = _bindings(previous["bindings"], template, "previous.bindings")
    applied_fields = _role_field_map(
        previous["applied_fields"],
        template,
        "previous.applied_fields",
        "LEDGER_APPLIED_SCHEMA",
        "LEDGER_APPLIED_VALUE",
    )
    return {
        "schema": LEDGER_SCHEMA,
        "template": template,
        "bindings": bindings,
        "applied_fields": applied_fields,
        "generation": _generation(previous["generation"]),
    }


def _same(left, right):
    """Type-sensitive canonical equality (not Python's True == 1 equality)."""

    if type(left) is not type(right):
        return False
    if type(left) is list:
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right)
        )
    if type(left) is dict:
        return set(left) == set(right) and all(
            _same(left[key], right[key]) for key in left
        )
    return left == right


def _structure(previous_template, incoming_template):
    previous_roles = {role["role"]: role for role in previous_template["roles"]}
    incoming_roles = {role["role"]: role for role in incoming_template["roles"]}
    if set(previous_roles) != set(incoming_roles):
        _fail(
            "STRUCTURE_MIGRATION_REQUIRED",
            "Adding or removing semantic roles requires explicit structure migration",
            "template.roles",
            added_roles=sorted(set(incoming_roles) - set(previous_roles)),
            removed_roles=sorted(set(previous_roles) - set(incoming_roles)),
        )
    changes = []
    for role in sorted(previous_roles):
        for name in ("purpose", "parent_role", "side"):
            old = previous_roles[role][name]
            new = incoming_roles[role][name]
            if not _same(old, new):
                changes.append(
                    {
                        "role": role,
                        "attribute": name,
                        "previous": copy.deepcopy(old),
                        "incoming": copy.deepcopy(new),
                    }
                )
    if changes:
        _fail(
            "STRUCTURE_MIGRATION_REQUIRED",
            "Role purpose, parent, side, and binding structure are fixed in v1 merge",
            "template.roles",
            changes=changes,
        )


def merge_instance(previous, current_fields, incoming_template):
    """Compute, but never commit, an atomic three-way semantic merge plan.

    The per-role/per-field rule is: unchanged template intent preserves current
    native values; a changed intent applies to a clean field; an intent that
    already equals current is accepted; any other state is an atomic conflict.
    Vectors such as ``head`` are single atoms.  Conflicts raise one
    ``MERGE_CONFLICT`` with every conflict listed and no partial plan.
    """

    previous = _ledger(previous)
    incoming = validate_template(incoming_template)
    previous_template = previous["template"]
    if incoming["template_id"] != previous_template["template_id"]:
        _fail(
            "TEMPLATE_ID_MISMATCH",
            "Incoming template does not belong to this instance",
            "template.template_id",
            previous=previous_template["template_id"],
            incoming=incoming["template_id"],
        )
    if incoming["revision"] < previous_template["revision"]:
        _fail(
            "REVISION_DOWNGRADE",
            "Incoming template revision is older than the committed ledger",
            "template.revision",
            previous=previous_template["revision"],
            incoming=incoming["revision"],
        )
    _structure(previous_template, incoming)
    if (
        incoming["revision"] == previous_template["revision"]
        and not _same(incoming, previous_template)
    ):
        _fail(
            "REVISION_CONTENT_MISMATCH",
            "The same revision cannot describe different template content",
            "template",
            revision=incoming["revision"],
        )

    current = _role_field_map(
        current_fields,
        previous_template,
        "current_fields",
        "MERGE_INPUT_SCHEMA",
        "MERGE_INPUT_VALUE",
    )
    previous_roles = {role["role"]: role for role in previous_template["roles"]}
    incoming_roles = {role["role"]: role for role in incoming["roles"]}
    target_fields = {}
    writes = []
    preserved = []
    conflicts = []

    for role in sorted(previous_roles):
        previous_fields = previous_roles[role]["fields"]
        incoming_fields = incoming_roles[role]["fields"]
        applied = previous["applied_fields"][role]
        current_values = current[role]
        target_fields[role] = {}
        for field in FIELD_NAMES:
            base_intent = previous_fields[field]
            incoming_intent = incoming_fields[field]
            previous_applied = applied[field]
            current_value = current_values[field]
            if _same(incoming_intent, base_intent):
                chosen = copy.deepcopy(current_value)
                if not _same(current_value, incoming_intent):
                    preserved.append(
                        {
                            "role": role,
                            "field": field,
                            "current": copy.deepcopy(current_value),
                            "incoming": copy.deepcopy(incoming_intent),
                        }
                    )
            elif _same(current_value, previous_applied):
                chosen = copy.deepcopy(incoming_intent)
                if not _same(current_value, incoming_intent):
                    writes.append(
                        {
                            "role": role,
                            "field": field,
                            "previous_applied": copy.deepcopy(previous_applied),
                            "current": copy.deepcopy(current_value),
                            "target": copy.deepcopy(incoming_intent),
                            "reason": "incoming_intent",
                        }
                    )
            elif _same(current_value, incoming_intent):
                chosen = copy.deepcopy(current_value)
            else:
                chosen = copy.deepcopy(current_value)
                conflicts.append(
                    {
                        "role": role,
                        "field": field,
                        "previous_intent": copy.deepcopy(base_intent),
                        "previous_applied": copy.deepcopy(previous_applied),
                        "current": copy.deepcopy(current_value),
                        "incoming_intent": copy.deepcopy(incoming_intent),
                    }
                )
            target_fields[role][field] = chosen

    if conflicts:
        _fail(
            "MERGE_CONFLICT",
            "The merge has conflicting edits and was rejected atomically",
            "merge",
            atomic=True,
            conflicts=conflicts,
            conflict_count=len(conflicts),
        )

    template_changed = not _same(previous_template, incoming)
    if writes:
        status = "apply"
        generation = previous["generation"] + 1
    elif template_changed:
        status = "metadata_only"
        generation = previous["generation"]
    else:
        status = "no_op"
        generation = previous["generation"]

    return {
        "schema": MERGE_PLAN_SCHEMA,
        "status": status,
        "committed": False,
        "requires_native_commit": bool(writes) or template_changed,
        "template_id": incoming["template_id"],
        "previous_revision": previous_template["revision"],
        "revision": incoming["revision"],
        "previous_generation": previous["generation"],
        "generation": generation,
        "template": copy.deepcopy(incoming),
        "bindings": copy.deepcopy(previous["bindings"]),
        "target_fields": target_fields,
        "writes": writes,
        "preserved": preserved,
        "conflicts": [],
    }
