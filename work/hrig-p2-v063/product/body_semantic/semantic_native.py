"""Blender-only adapter for explicit semantic templates; no operator registration.

This module is staging code. Native fixtures must be run by the integrating
caller in an isolated Blender process before shipping it.
"""
from __future__ import annotations

import copy
import json
import math
import uuid

import bpy
from mathutils import Vector

try:
    from . import semantic_contract as C
except ImportError:
    import semantic_contract as C


LEDGER_KEY = "hrig_semantic_ledger_v1"
REST_FIELDS = frozenset(("head", "tail", "roll"))


class NativeError(C.ContractError):
    pass


def _fail(code, message, **details):
    raise NativeError(code, dict(message=message, **details))


def _same(a, b):
    return json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(
        b, sort_keys=True, allow_nan=False)


def _local(id_block):
    return id_block.library is None and id_block.override_library is None


def _context_guard():
    if bpy.context.mode not in {"OBJECT", "POSE"}:
        _fail("CONTEXT_MODE", "Only OBJECT or POSE context is supported",
              actual=bpy.context.mode)
    in_mode = list(getattr(bpy.context, "objects_in_mode", ()))
    if len(in_mode) > 1:
        _fail("MULTI_OBJECT_MODE", "Multi-object mode is unsupported")


def _rig_guard(rig):
    _context_guard()
    if not isinstance(rig, bpy.types.Object) or rig.type != "ARMATURE":
        _fail("RIG_TYPE", "An explicit Armature Object is required")
    if bpy.data.objects.get(rig.name) != rig or not _local(rig) or not _local(rig.data):
        _fail("RIG_LOCAL", "Linked, overridden, evaluated or detached IDs are unsupported")
    owners = [o for o in bpy.data.objects if o.type == "ARMATURE" and o.data == rig.data]
    if rig.data.users != 1 or owners != [rig]:
        _fail("SHARED_ARMATURE", "Armature data must have exactly one object owner",
              data_users=rig.data.users, owners=[o.name for o in owners])
    if bpy.context.view_layer.objects.get(rig.name) != rig:
        _fail("RIG_CONTEXT", "Rig must belong to the current View Layer")
    scenes = list(rig.users_scene)
    if len(scenes) != 1 or scenes[0] != bpy.context.scene:
        _fail("MULTI_SCENE_RIG", "Exactly the current Scene must own this rig",
              scenes=[s.name for s in scenes])
    if rig.mode not in {"OBJECT", "POSE"}:
        _fail("RIG_MODE", "Rig must be in OBJECT or POSE mode", actual=rig.mode)
    return rig


def _bone_fields(bone, pose):
    # Bone.matrix_local is armature-space, unlike Bone.matrix (parent-relative).
    # The Blender static conversion is also used by bundled Rigify utilities.
    _axis, roll = bpy.types.Bone.AxisRollFromMatrix(bone.matrix_local.to_3x3())
    fields = {
        "head": list(bone.head_local), "tail": list(bone.tail_local),
        "roll": float(roll), "use_deform": bool(bone.use_deform),
        "rotation_mode": pose.rotation_mode,
        "lock_location": list(pose.lock_location),
        "lock_rotation": list(pose.lock_rotation),
        "lock_scale": list(pose.lock_scale), "inherit_scale": bone.inherit_scale,
    }
    # Use the contract's public receipt to normalize actual values, not intent.
    spec = {"schema": C.TEMPLATE_SCHEMA, "template_id": "native.capture",
            "revision": 1, "roles": [{"role": "capture", "purpose": "mechanism",
                "parent_role": None, "side": "C", "fields": fields}]}
    return C.validate_template(spec)["roles"][0]["fields"]


def capture_inventory(rig):
    """Read all native bones without changing mode, selection, or active ID.

    Coordinates are rest positions in Armature object space. Roll is the
    canonical axis/matrix-derived representation, not an unbounded edit roll.
    """
    _rig_guard(rig)
    result = {}
    for bone in rig.data.bones:
        pose = rig.pose.bones.get(bone.name)
        if pose is None:
            _fail("POSE_BONE_MISSING", "Native pose bone is missing", bone=bone.name)
        result[bone.name] = {
            "parent": bone.parent.name if bone.parent else None,
            "fields": _bone_fields(bone, pose),
        }
    return result


def _role_fields(inventory, bindings):
    return {role: copy.deepcopy(inventory[bone]["fields"])
            for role, bone in bindings.items()}


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def read_ledger(rig):
    _rig_guard(rig)
    raw = rig.get(LEDGER_KEY)
    if type(raw) is not str or len(raw.encode("utf-8")) > 1048576:
        _fail("LEDGER_STORAGE", "A managed JSON ledger of at most 1 MiB is required")
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                _fail("LEDGER_STORAGE", "Duplicate JSON ledger key", key=key)
            out[key] = value
        return out
    def constant(token):
        _fail("LEDGER_STORAGE", "Non-finite JSON ledger number", token=token)
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError) as exc:
        _fail("LEDGER_STORAGE", "Invalid ledger JSON", reason=str(exc))
    # merge_instance validates exact ledger keys as well as receipt fields.
    if type(value) is not dict or "template" not in value or "applied_fields" not in value:
        _fail("LEDGER_STORAGE", "Invalid semantic ledger structure")
    C.merge_instance(value, value["applied_fields"], value["template"])
    return value


def extract(rig, template_id, revision, explicit_roles):
    """Extract only explicitly declared roles; no ledger or native writes."""
    return C.extract_template(template_id, revision, explicit_roles, capture_inventory(rig))


def _original_id(current, original):
    # Keep the original RNA wrapper alive and check it remains valid. Comparing
    # only integer addresses could accept allocator reuse after ID deletion.
    try:
        pointer = original.as_pointer()
    except ReferenceError:
        return False
    return current is not None and pointer != 0 and current.as_pointer() == pointer


def _snapshot_context(*, armature_scope=None, object_scope=None):
    """Snapshot OBJECT/POSE selection using the actual Blender 5.2 RNA.

    A scoped snapshot verifies only pre-existing ID identities. New IDs may be
    present after create, but object selection/active/mode still compare exactly.
    """
    _context_guard()
    vl = bpy.context.view_layer
    active = vl.objects.active
    if object_scope is None:
        objects = {o.name: o for o in vl.objects}
    else:
        objects = {}
        for name, original in object_scope.items():
            obj = vl.objects.get(name)
            if not _original_id(obj, original):
                _fail("CONTEXT_ID_CHANGED", "Original Object disappeared or was replaced", object=name)
            objects[name] = obj
    if armature_scope is None:
        armatures = {o.name: o for o in vl.objects
                     if o.type == "ARMATURE" and _local(o.data)}
    else:
        armatures = {}
        for name, record in armature_scope.items():
            obj = vl.objects.get(name)
            if (obj is None or obj.type != "ARMATURE"
                    or not _original_id(obj, record["object_ref"])
                    or not _original_id(obj.data, record["data_ref"])
                    or obj.data.name != record["data_name"]):
                _fail("CONTEXT_ID_CHANGED", "Original Armature identity or data changed", object=name)
            armatures[name] = obj
    return {"active": active, "mode": active.mode if active else "OBJECT",
            "object_identities": objects,
            "selected": [o for o in vl.objects if o.select_get()],
            "bone_selection": {
                name: {"object_ref": o, "data_ref": o.data,
                       "data_name": o.data.name,
                       "active": o.data.bones.active.name if o.data.bones.active else None,
                       "selected": {p.name: bool(p.select) for p in o.pose.bones}}
                for name, o in armatures.items()}}


def _mode(mode):
    active = bpy.context.view_layer.objects.active
    if active and active.mode != mode:
        if not bpy.ops.object.mode_set.poll():
            _fail("MODE_POLL", "Mode operator unavailable", mode=mode)
        result = bpy.ops.object.mode_set(mode=mode)
        if result != {"FINISHED"}:
            _fail("MODE_WRITE", "Mode operator did not finish", mode=mode)


def _activate(rig):
    _mode("OBJECT")
    vl = bpy.context.view_layer
    for obj in vl.objects:
        if obj.select_get():
            obj.select_set(False)
    rig.select_set(True)
    vl.objects.active = rig
    if not rig.select_get() or vl.objects.active != rig:
        _fail("RIG_SELECT", "Cannot select and activate the rig")


def _restore_context(saved):
    # Validate original identities before mutating even object selection. Do not
    # accidentally restore into a same-named replacement ID.
    _snapshot_context(armature_scope=saved["bone_selection"],
                      object_scope=saved["object_identities"])
    _mode("OBJECT")
    vl = bpy.context.view_layer
    for obj in vl.objects:
        obj.select_set(obj in saved["selected"])
    vl.objects.active = saved["active"]
    for name, record in saved["bone_selection"].items():
        obj = vl.objects.get(name)
        if set(obj.pose.bones.keys()) != set(record["selected"]):
            _fail("CONTEXT_RESTORE", "Original pose bone set changed", object=name)
        for name_b, value in record["selected"].items():
            pose = obj.pose.bones.get(name_b)
            if pose is None:
                _fail("CONTEXT_RESTORE", "Original bone disappeared", bone=name_b)
            pose.select = value
        if record["active"] is not None and obj.data.bones.get(record["active"]) is None:
            _fail("CONTEXT_RESTORE", "Original active bone disappeared", bone=record["active"])
        obj.data.bones.active = obj.data.bones.get(record["active"]) if record["active"] else None
    if saved["active"]:
        _mode(saved["mode"])
    after = _snapshot_context(armature_scope=saved["bone_selection"],
                              object_scope=saved["object_identities"])
    if after != saved:
        _fail("CONTEXT_RESTORE", "Selection, active ID, mode or bone selection differs")


def _write_guard(rig):
    _rig_guard(rig)
    if rig.hide_select or rig.hide_get() or not rig.visible_get():
        _fail("RIG_UNSELECTABLE", "Native writes require a visible selectable rig")
    connected = [b.name for b in rig.data.bones if b.use_connect]
    if connected:
        _fail("CONNECTED_BONES", "Connected bone effects are outside v1 write domain",
              bones=connected)


def _field_preflight(fields):
    for role, values in fields.items():
        if abs(values["roll"]) > math.pi:
            _fail("ROLL_REPRESENTATION", "Native canonical roll domain is [-pi, pi]", role=role)
        for owner, field in ((bpy.types.Bone, "inherit_scale"),
                             (bpy.types.PoseBone, "rotation_mode")):
            prop = owner.bl_rna.properties.get(field)
            if prop is None or values[field] not in {v.identifier for v in prop.enum_items}:
                _fail("NATIVE_FIELD_UNSUPPORTED", "Runtime RNA does not support value",
                      role=role, field=field, value=values[field])


def _matrix_rows(matrix):
    return [[float(v) for v in row] for row in matrix]


def _capture_matrices(rig):
    return {b.name: _matrix_rows(b.matrix_local) for b in rig.data.bones}


def _project_rest(fields):
    """Isolated EditBone projection; not a parented Bone finalization model."""
    matrix = bpy.types.Bone.MatrixFromAxisRoll(
        Vector(fields["tail"]) - Vector(fields["head"]), fields["roll"])
    _axis, canonical_roll = bpy.types.Bone.AxisRollFromMatrix(matrix)
    projected = copy.deepcopy(fields)
    projected["roll"] = float(canonical_roll)
    native4 = matrix.to_4x4()
    native4.translation = Vector(fields["head"])
    return projected, _matrix_rows(native4)


def _owned_native_tree_projection(target_fields, bindings, parent_roles):
    """Forward-convert a complete tree in disposable owned native IDs.

    EditBone->Bone finalization is parent-dependent.  A separate scratch tree
    supplies the expected rest representation before the real target exists;
    no expected value is copied from the object that will be checked.  Only
    head/tail/roll are observed here.  Every non-rest field retains the caller's
    validated intent, so this projection cannot weaken its write/readback gate.
    """
    saved = _snapshot_context()
    suffix = uuid.uuid4().hex
    object_name = "HRIG-rest-forward-" + suffix
    data_name = "HRIG-rest-forward-data-" + suffix
    rig = data = None
    original_error = None
    projected_fields = {}
    projected_matrices = {}
    try:
        data = bpy.data.armatures.new(data_name)
        rig = bpy.data.objects.new(object_name, data)
        bpy.context.scene.collection.objects.link(rig)
        _activate(rig)
        _mode("EDIT")
        try:
            for role, fields in target_fields.items():
                bone = data.edit_bones.new(bindings[role])
                if bone.name != bindings[role]:
                    _fail("BONE_NAME_CHANGED", "Scratch projection changed an explicit bone name",
                          role=role)
                for field in ("head", "tail", "roll"):
                    setattr(bone, field, fields[field])
            for role, parent in parent_roles.items():
                bone = data.edit_bones[bindings[role]]
                bone.parent = data.edit_bones[bindings[parent]] if parent else None
                bone.use_connect = False
            for role, fields in target_fields.items():
                actual = _raw_edit_tuple(data.edit_bones[bindings[role]])
                expected = {field: fields[field] for field in ("head", "tail", "roll")}
                if not _same(actual, expected):
                    _fail("EDIT_WRITE_MISMATCH", "Scratch EditBone raw assignment differs exactly",
                          role=role, expected=expected, actual=actual)
        finally:
            _mode("OBJECT")
        for role, fields in target_fields.items():
            name = bindings[role]
            rest = _bone_fields(data.bones[name], rig.pose.bones[name])
            expected = copy.deepcopy(fields)
            for field in REST_FIELDS:
                expected[field] = copy.deepcopy(rest[field])
            projected_fields[role] = expected
            projected_matrices[name] = _matrix_rows(data.bones[name].matrix_local)
    except Exception as exc:
        original_error = {"type": type(exc).__name__, "message": str(exc),
                          "code": getattr(exc, "code", None),
                          "details": getattr(exc, "details", None)}
        raise
    finally:
        failures = []
        try:
            _mode("OBJECT")
        except Exception as cleanup:
            failures.append("mode: " + str(cleanup))
        try:
            if rig is not None:
                # Delete only the RNA reference created by this helper.
                bpy.data.objects.remove(rig, do_unlink=True)
            if data is not None:
                if data.users != 0:
                    failures.append("scratch Armature still has users: " + str(data.users))
                else:
                    bpy.data.armatures.remove(data)
            if (bpy.data.objects.get(object_name) is not None
                    or bpy.data.armatures.get(data_name) is not None):
                failures.append("scratch ID pair remains")
        except Exception as cleanup:
            failures.append("owned IDs: " + str(cleanup))
        try:
            _restore_context(saved)
        except Exception as cleanup:
            failures.append("context: " + str(cleanup))
        if failures:
            _fail("FORWARD_PROJECTION_CLEANUP_FAILED", "Owned forward projection cleanup failed",
                  original=original_error, failures=failures)
    return projected_fields, projected_matrices


def _raw_edit_tuple(bone):
    return {"head": list(bone.head), "tail": list(bone.tail), "roll": float(bone.roll)}


def _write_fields(rig, bindings, fields, writes, *, rest_transaction=None, restore_raw=None):
    rest = [w for w in writes if w["field"] in REST_FIELDS]
    if rest or restore_raw:
        _activate(rig)
        _mode("EDIT")
        try:
            before_raw = {b.name: _raw_edit_tuple(b) for b in rig.data.edit_bones}
            if rest_transaction is not None:
                # Capture every raw rest tuple before the first real write,
                # including unmanaged bones potentially rebuilt by mode exit.
                rest_transaction["before_raw"] = copy.deepcopy(before_raw)
            assigned = {}
            if restore_raw is not None:
                assigned = copy.deepcopy(restore_raw)
            else:
                roll_writes = {w["role"] for w in rest if w["field"] == "roll"}
                for role in sorted({w["role"] for w in rest}):
                    name = bindings[role]
                    raw = {f: copy.deepcopy(fields[role][f]) for f in ("head", "tail", "roll")}
                    if role not in roll_writes:
                        # Preserve the actual manual/native raw roll. Its
                        # matrix-derived canonical roll is not an edit scalar.
                        raw["roll"] = before_raw[name]["roll"]
                    assigned[name] = raw
            if rest_transaction is not None:
                rest_transaction["assigned_raw"] = copy.deepcopy(assigned)
            for name, raw in assigned.items():
                bone = rig.data.edit_bones[name]
                for field in ("head", "tail", "roll"):
                    setattr(bone, field, raw[field])
            for name, raw in assigned.items():
                observed = _raw_edit_tuple(rig.data.edit_bones[name])
                if not _same(observed, raw):
                    _fail("EDIT_WRITE_MISMATCH", "EditBone raw assignment differs exactly",
                          bone=name, expected=raw, actual=observed)
        finally:
            _mode("OBJECT")
    for write in writes:
        field = write["field"]
        if field not in REST_FIELDS:
            name = bindings[write["role"]]
            owner = rig.data.bones[name] if field in {"use_deform", "inherit_scale"} else rig.pose.bones[name]
            setattr(owner, field, fields[write["role"]][field])
    rig.data.update_tag()
    rig.update_tag(refresh={"OBJECT", "DATA", "TIME"})
    bpy.context.view_layer.update()


def _check_inventory(actual, expected, code):
    if not _same(actual, expected):
        changed = [name for name in sorted(set(actual) | set(expected))
                   if not _same(actual.get(name), expected.get(name))]
        _fail(code, "Native canonical read-back differs", bones=changed,
              differences={name: {"expected": expected.get(name), "actual": actual.get(name)}
                           for name in changed})


def _check_matrices(actual, expected, code):
    if not _same(actual, expected):
        changed = [name for name in sorted(set(actual) | set(expected))
                   if not _same(actual.get(name), expected.get(name))]
        _fail(code, "Native rest matrices differ exactly", bones=changed,
              differences={name: {"expected": expected.get(name), "actual": actual.get(name)}
                           for name in changed})


def merge(rig, incoming_template, *, expected_plan=None, _fault=None):
    """Recompute and atomically apply a managed three-way plan.

    ``expected_plan`` optionally protects a previously reviewed complete plan;
    stale or altered plans are rejected. ``_fault`` is a fixture-only callback
    invoked after native field writes and after ledger storage.
    """
    _write_guard(rig)
    previous = read_ledger(rig)
    before = capture_inventory(rig)
    before_matrices = _capture_matrices(rig)
    C.bind_roles(previous["template"], previous["bindings"], before)
    current = _role_fields(before, previous["bindings"])
    plan = C.merge_instance(previous, current, incoming_template)
    if expected_plan is not None and not _same(expected_plan, plan):
        _fail("STALE_PLAN", "Reviewed merge plan does not match the current rig")
    parented_rest_roles = sorted({write["role"] for write in plan["writes"]
        if write["field"] in REST_FIELDS
        and rig.data.bones[plan["bindings"][write["role"]]].parent is not None})
    if parented_rest_roles:
        _fail("REST_HIERARCHY_UNSUPPORTED",
              "Parented rest writes require a separately verified whole-tree transaction",
              roles=parented_rest_roles)
    # Validate combined head/tail after merging separate atomic fields.
    C.make_ledger(plan["template"], plan["bindings"], plan["target_fields"], plan["generation"])
    _field_preflight(plan["target_fields"])
    for write in plan["writes"]:
        field = write["field"]
        if field not in REST_FIELDS:
            name = plan["bindings"][write["role"]]
            owner = rig.data.bones[name] if field in {"use_deform", "inherit_scale"} else rig.pose.bones[name]
            if owner.is_property_readonly(field):
                _fail("NATIVE_FIELD_READONLY", "Runtime native field is not writable",
                      bone=name, field=field)
    if not plan["requires_native_commit"]:
        return dict(status="no_op", committed=False, plan=plan, ledger=previous)
    saved = _snapshot_context()
    old_raw = rig[LEDGER_KEY]
    expected = copy.deepcopy(before)
    expected_matrices = copy.deepcopy(before_matrices)
    for role, bone in plan["bindings"].items():
        expected[bone]["fields"] = copy.deepcopy(plan["target_fields"][role])
    started = False
    rest_transaction = {}
    try:
        started = True
        _write_fields(rig, plan["bindings"], plan["target_fields"], plan["writes"],
                      rest_transaction=rest_transaction)
        by_bone = {bone: role for role, bone in plan["bindings"].items()}
        for bone, raw in rest_transaction.get("assigned_raw", {}).items():
            role = by_bone[bone]
            native_fields = copy.deepcopy(plan["target_fields"][role])
            native_fields.update(raw)
            projected, matrix = _project_rest(native_fields)
            expected[bone]["fields"] = projected
            expected_matrices[bone] = matrix
        if _fault:
            _fault("after_fields", rig)
        actual = capture_inventory(rig)
        _check_inventory(actual, expected, "READBACK_MISMATCH")
        _check_matrices(_capture_matrices(rig), expected_matrices, "REST_MATRIX_MISMATCH")
        ledger = C.make_ledger(plan["template"], plan["bindings"],
                               _role_fields(actual, plan["bindings"]), plan["generation"])
        rig[LEDGER_KEY] = _dumps(ledger)
        if _fault:
            _fault("after_ledger", rig)
        if not _same(read_ledger(rig), ledger):
            _fail("LEDGER_READBACK", "Stored ledger differs from native receipt")
        _restore_context(saved)
        return dict(status=plan["status"], committed=True, plan=plan, ledger=ledger)
    except Exception as exc:
        failures = []
        if started:
            try:
                recovery_writes = plan["writes"] if "before_raw" in rest_transaction else [
                    w for w in plan["writes"] if w["field"] not in REST_FIELDS]
                _write_fields(rig, plan["bindings"], current, recovery_writes,
                              restore_raw=rest_transaction.get("before_raw"))
            except Exception as recovery:
                failures.append("fields: " + str(recovery))
            try:
                rig[LEDGER_KEY] = old_raw
            except Exception as recovery:
                failures.append("ledger: " + str(recovery))
        try:
            _restore_context(saved)
        except Exception as recovery:
            failures.append("context: " + str(recovery))
        try:
            _check_inventory(capture_inventory(rig), before, "ROLLBACK_READBACK")
            _check_matrices(_capture_matrices(rig), before_matrices, "ROLLBACK_MATRIX_READBACK")
            if rig.get(LEDGER_KEY) != old_raw:
                failures.append("ledger readback differs")
        except Exception as recovery:
            failures.append("readback: " + str(recovery))
        if failures:
            _fail("ROLLBACK_FAILED", "Native rollback could not be fully verified",
                  original=str(exc), failures=failures)
        raise


def create(template, bindings, *, object_name, data_name, collection=None, _fault=None):
    """Create only explicitly described bones in a new local Armature ID pair."""
    _context_guard()
    template = C.validate_template(template)
    # Public receipt validates exact one-to-one bindings before native creation.
    target = {r["role"]: copy.deepcopy(r["fields"]) for r in template["roles"]}
    receipt = C.make_ledger(template, bindings, target, 0)
    bindings = receipt["bindings"]
    _field_preflight(target)
    for name, table in ((object_name, bpy.data.objects), (data_name, bpy.data.armatures)):
        if type(name) is not str or not name.strip() or len(name.encode("utf-8")) > 63:
            _fail("NEW_ID_NAME", "Explicit native ID names must fit 63 UTF-8 bytes")
        if name in table:
            _fail("NEW_ID_COLLISION", "Native ID name already exists", name=name)
    if any(len(name.encode("utf-8")) > 63 for name in bindings.values()):
        _fail("BONE_NAME_LENGTH", "Bindings must fit native 63-byte bone names")
    collection = collection or bpy.context.scene.collection
    if not isinstance(collection, bpy.types.Collection) or not _local(collection):
        _fail("COLLECTION_LOCAL", "A local destination Collection is required")
    if bpy.data.collections.get(collection.name) != collection and collection != bpy.context.scene.collection:
        _fail("COLLECTION_CONTEXT", "Destination Collection is not a Main ID")
    # Collection must already be in the current scene and View Layer.
    def contains(layer):
        return layer.collection == collection or any(contains(c) for c in layer.children)
    if not contains(bpy.context.view_layer.layer_collection):
        _fail("COLLECTION_CONTEXT", "Destination Collection must be in the current View Layer")
    def scene_contains(node):
        return node == collection or any(scene_contains(c) for c in node.children)
    scene_owners = [scene for scene in bpy.data.scenes if scene_contains(scene.collection)]
    if scene_owners != [bpy.context.scene]:
        _fail("COLLECTION_MULTI_SCENE", "Destination must belong only to the current Scene")
    # All public input/ID/collection guards precede even the disposable tree.
    parent_roles = {role["role"]: role["parent_role"] for role in template["roles"]}
    projected_target, expected_matrices = _owned_native_tree_projection(
        target, bindings, parent_roles)
    saved = _snapshot_context()
    rig = data = None
    try:
        data = bpy.data.armatures.new(data_name)
        rig = bpy.data.objects.new(object_name, data)
        collection.objects.link(rig)
        _activate(rig)
        _mode("EDIT")
        try:
            for role in template["roles"]:
                bone = data.edit_bones.new(bindings[role["role"]])
                if bone.name != bindings[role["role"]]:
                    _fail("BONE_NAME_CHANGED", "Blender changed an explicit bone name")
                for field in ("head", "tail", "roll"):
                    setattr(bone, field, role["fields"][field])
            for role in template["roles"]:
                parent = role["parent_role"]
                bone = data.edit_bones[bindings[role["role"]]]
                bone.parent = data.edit_bones[bindings[parent]] if parent else None
                bone.use_connect = False
            for role, fields in target.items():
                raw = _raw_edit_tuple(data.edit_bones[bindings[role]])
                expected_raw = {f: fields[f] for f in ("head", "tail", "roll")}
                if not _same(raw, expected_raw):
                    _fail("EDIT_WRITE_MISMATCH", "Created EditBone raw assignment differs exactly",
                          role=role, expected=expected_raw, actual=raw)
        finally:
            _mode("OBJECT")
        writes = [{"role": r["role"], "field": f} for r in template["roles"]
                  for f in C.FIELD_NAMES if f not in REST_FIELDS]
        _write_fields(rig, bindings, target, writes)
        if _fault:
            _fault("after_fields", rig)
        actual = capture_inventory(rig)
        C.bind_roles(template, bindings, actual)
        for role, bone in bindings.items():
            if not _same(actual[bone]["fields"], projected_target[role]):
                _fail("READBACK_MISMATCH", "Created fields differ from native projection",
                      role=role, intent=target[role], expected=projected_target[role],
                      actual=actual[bone]["fields"])
        _check_matrices(_capture_matrices(rig), expected_matrices, "REST_MATRIX_MISMATCH")
        ledger = C.make_ledger(template, bindings, _role_fields(actual, bindings), 0)
        rig[LEDGER_KEY] = _dumps(ledger)
        if _fault:
            _fault("after_ledger", rig)
        if not _same(read_ledger(rig), ledger):
            _fail("LEDGER_READBACK", "Created ledger readback differs")
        _restore_context(saved)
        return dict(object=rig, ledger=ledger, inventory=actual)
    except Exception as exc:
        failures = []
        try:
            _mode("OBJECT")
        except Exception as recovery:
            failures.append("mode: " + str(recovery))
        try:
            if rig is not None:
                bpy.data.objects.remove(rig, do_unlink=True)
            if data is not None and data.users == 0:
                bpy.data.armatures.remove(data)
            if bpy.data.objects.get(object_name) is not None or bpy.data.armatures.get(data_name) is not None:
                failures.append("new ID pair remains after cleanup")
        except Exception as recovery:
            failures.append("new IDs: " + str(recovery))
        try:
            _restore_context(saved)
        except Exception as recovery:
            failures.append("context: " + str(recovery))
        if failures:
            _fail("ROLLBACK_FAILED", "New-ID cleanup could not be fully verified",
                  original=str(exc), failures=failures)
        raise
