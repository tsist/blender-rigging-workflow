"""Blender extension worker for the owned Snow weight-template experiment.

The worker is intentionally data-oriented: the extension request supplies
pinned descriptors and stage parameters, while all template semantics live in
the strict JSON contract and the offline native adapter.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import traceback
from pathlib import Path

sys.dont_write_bytecode = True
import bpy


PARAMS = parameters
JOB = Path(job_directory)
STAGE = PARAMS["stage"]
MODULE_ROOT = Path(PARAMS["module_root"]).resolve()
sys.path.insert(0, str(MODULE_ROOT.parent))

from weight_template.contract import TemplateError, validate_template
from weight_template.core import f32, sum_rows
from weight_template.native import (
    _barycentric,
    _bone_rest_descriptor,
    _copy_mesh_object_clean,
    _group_signature,
    _mesh_rest,
    _mesh_topology,
    _rows_equal,
    apply_cross_topology,
    apply_same_topology,
    brute_force_mapping_oracle,
    build_target_patch,
    create_proxy_rig,
    evaluated_lbs_check,
    extract_template,
    file_sha256,
    object_by_name,
    open_blend,
    read_json,
    snapshot_weights,
    source_content_snapshot,
    write_json,
)


def _verify_resources():
    resources = PARAMS.get("resources", [])
    for item in resources:
        path = Path(item["file"])
        if not path.is_file():
            raise RuntimeError("Declared resource missing: " + str(path))
        observed = file_sha256(path)
        if observed != item["expected_sha256"]:
            raise RuntimeError(
                "Declared resource SHA mismatch: " + str(path) + " " + observed
            )
    return {
        "count": len(resources),
        "all_exact": True,
        "sha256": {
            item["file"]: item["expected_sha256"] for item in resources
        },
    }


def _descriptor_path(name):
    value = PARAMS.get(name)
    if not value:
        raise RuntimeError("Missing stage descriptor: " + name)
    path = Path(value["file"])
    if file_sha256(path) != value["expected_sha256"]:
        raise RuntimeError("Stage descriptor SHA mismatch: " + name)
    return path


def _source_descriptor():
    return _descriptor_path("source")


def _template_descriptor():
    return _descriptor_path("template")


def _candidate_descriptor():
    return _descriptor_path("candidate")


def _save_candidate(path):
    filepaths = bpy.context.preferences.filepaths
    previous_save_version = filepaths.save_version
    filepaths.save_version = 0
    try:
        bpy.ops.wm.save_as_mainfile(
            filepath=str(path), copy=True, check_existing=False
        )
    finally:
        filepaths.save_version = previous_save_version
    bpy.context.view_layer.update()
    return file_sha256(path)


def _source_unchanged(before_snapshot, after_snapshot, before_sha, after_sha):
    return {
        "file_sha256_before": before_sha,
        "file_sha256_after": after_sha,
        "content_sha256_before": before_snapshot[1],
        "content_sha256_after": after_snapshot[1],
        "file_unchanged": before_sha == after_sha,
        "content_unchanged": before_snapshot == after_snapshot,
    }


def same_producer():
    _verify_resources()
    source_path = _source_descriptor()
    source_sha_before = file_sha256(source_path)
    open_blend(source_path)
    body = object_by_name(PARAMS["source_object"])
    rig = object_by_name(PARAMS["rig"])
    shirt = object_by_name(PARAMS["shirt_object"])
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    before_snapshot = source_content_snapshot(body, rig, deform)
    template, _ = extract_template(
        body, rig, shirt, source_path, source_sha_before
    )
    template_path = JOB / "output-template.json"
    write_json(template_path, template)
    target = _copy_mesh_object_clean(body, PARAMS["same_target_name"])
    apply_report = apply_same_topology(
        template,
        target,
        rig,
        note="Complete Snow body weights copied by exact vertex index on a task-owned clean mesh.",
    )
    after_snapshot = source_content_snapshot(body, rig, deform)
    source_sha_after = file_sha256(source_path)
    candidate_path = JOB / "output-same-candidate.blend"
    candidate_sha = _save_candidate(candidate_path)
    report = {
        "status": "pass",
        "stage": STAGE,
        "template": str(template_path),
        "template_sha256": file_sha256(template_path),
        "template_payload_sha256": template["payload_sha256"],
        "source": str(source_path),
        "source_sha256": source_sha_before,
        "source_unchanged": _source_unchanged(
            before_snapshot, after_snapshot, source_sha_before, source_sha_after
        ),
        "source_object": body.name,
        "source_mesh": body.data.name,
        "source_vertex_count": len(body.data.vertices),
        "source_group_count": len(body.vertex_groups),
        "source_shape_key_count": len(body.data.shape_keys.key_blocks)
        if body.data.shape_keys
        else 0,
        "target": target.name,
        "target_vertex_count": len(target.data.vertices),
        "target_group_count": len(target.vertex_groups),
        "candidate": str(candidate_path),
        "candidate_sha256": candidate_sha,
        "apply": apply_report,
        "template_stats": template["weights"]["stats"],
        "public_transfer_compatibility": template["provenance"][
            "public_transfer_compatibility"
        ],
        "license": template["license"],
        "scope": (
            "Complete raw weight storage on the real Snow body mesh, copied to a "
            "task-owned same-topology mesh. No original asset edit, no full-character "
            "or clothing claim."
        ),
    }
    write_json(JOB / "output-same-producer.json", report)
    return report


def same_cold():
    _verify_resources()
    template_path = _template_descriptor()
    candidate_path = _candidate_descriptor()
    template = validate_template(read_json(template_path))
    candidate_sha_before = file_sha256(candidate_path)
    open_blend(candidate_path)
    target = object_by_name(PARAMS["target_object"])
    rig = object_by_name(PARAMS["rig"])
    source = object_by_name(PARAMS["source_object"])
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    source_snapshot = source_content_snapshot(source, rig, deform)
    target_topology = _mesh_topology(target.data)
    target_rest = _mesh_rest(target.data, target_topology)
    target_groups = _group_signature(target)
    expected_groups = template["group_map"]
    group_names_exact = [item["name"] for item in target_groups] == [
        item["name"] for item in expected_groups
    ]
    group_locks_exact = [item["lock"] for item in target_groups] == [
        item["lock"] for item in expected_groups
    ]
    source_deform = {group["name"] for group in expected_groups if group["deform"]}
    target_rows = []
    names = {group.index: group.name for group in target.vertex_groups}
    for vertex in target.data.vertices:
        row = []
        for membership in sorted(vertex.groups, key=lambda item: item.group):
            name = names[membership.group]
            row.append(
                {
                    "group_index": int(membership.group),
                    "group": name,
                    "weight": float(membership.weight),
                    "deform": name in source_deform,
                }
            )
        target_rows.append(row)
    exact = _rows_equal(
        [
            [
                {
                    "group_index": item["group_index"],
                    "group": item["group"],
                    "weight": item["weight"],
                    "deform": item["deform"],
                }
                for item in row
            ]
            for row in target_rows
        ],
        template["weights"]["vertices"],
    )
    all_sums = sum_rows(target_rows, deform=False)
    deform_sums = sum_rows(target_rows, deform=True)
    max_all_error = max(
        abs(a - b) for a, b in zip(all_sums, template["weights"]["all_sums"])
    )
    max_deform_error = max(
        abs(a - b) for a, b in zip(deform_sums, template["weights"]["deform_sums"])
    )
    max_weight_error = 0.0
    if len(target_rows) == len(template["weights"]["vertices"]):
        for observed_row, expected_row in zip(
            target_rows, template["weights"]["vertices"]
        ):
            observed = {item["group"]: f32(item["weight"]) for item in observed_row}
            expected = {
                item["group"]: f32(item["weight"]) for item in expected_row
            }
            for name in set(observed) | set(expected):
                max_weight_error = max(
                    max_weight_error, abs(observed.get(name, 0.0) - expected.get(name, 0.0))
                )
    source_match = source_snapshot[1] == template["provenance"]["source_content_sha256"]
    checks = {
        "vertex_count": len(target.data.vertices)
        == template["source"]["topology"]["vertex_count"],
        "topology_sha256": target_topology["sha256"]
        == template["source"]["topology"]["sha256"],
        "rest_sha256": target_rest["sha256"] == template["source"]["rest"]["sha256"],
        "group_names_exact": group_names_exact,
        "group_locks_exact": group_locks_exact,
        "weights_exact": exact and max_weight_error == 0.0,
        "all_sums_exact": max_all_error <= 1e-9,
        "deform_sums_exact": max_deform_error <= 1e-9,
        "source_object_matches_template": source_match,
    }
    passed = all(checks.values())
    report = {
        "status": "pass" if passed else "failed",
        "stage": STAGE,
        "template": str(template_path),
        "template_sha256": file_sha256(template_path),
        "candidate": str(candidate_path),
        "candidate_sha256_before_cold": candidate_sha_before,
        "candidate_sha256_after_cold": file_sha256(candidate_path),
        "candidate_unchanged": candidate_sha_before == file_sha256(candidate_path),
        "target": target.name,
        "vertex_count": len(target.data.vertices),
        "group_count": len(target.vertex_groups),
        "max_weight_error": max_weight_error,
        "max_all_sum_error": max_all_error,
        "max_deform_sum_error": max_deform_error,
        "nonunit_deform_vertices": template["weights"]["stats"][
            "nonunit_deform_vertices"
        ],
        "unweighted_deform_vertices": template["weights"]["stats"][
            "unweighted_deform_vertices"
        ],
        "checks": checks,
        "scope": (
            "Independent use_scripts=False cold reopen of the complete same-topology "
            "weight storage; exact per-membership comparison and raw sum preservation."
        ),
    }
    write_json(JOB / "output-same-cold.json", report)
    if not passed:
        raise RuntimeError("Same-topology cold verification failed")
    return report


def cross_producer():
    _verify_resources()
    source_path = _source_descriptor()
    template_path = _template_descriptor()
    template = validate_template(read_json(template_path))
    source_sha_before = file_sha256(source_path)
    open_blend(source_path)
    body = object_by_name(PARAMS["source_object"])
    rig = object_by_name(PARAMS["rig"])
    shirt = object_by_name(PARAMS["shirt_object"])
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    before_snapshot = source_content_snapshot(body, rig, deform)
    shirt_topology = _mesh_topology(shirt.data)
    shirt_rest = _mesh_rest(shirt.data, shirt_topology)
    target, target_report = build_target_patch(
        body, rig, template, PARAMS["cross_target_name"]
    )
    cross_report = apply_cross_topology(
        template,
        target,
        rig,
        target_source_report=target_report,
    )
    after_snapshot = source_content_snapshot(body, rig, deform)
    source_sha_after = file_sha256(source_path)
    candidate_path = JOB / "output-cross-candidate.blend"
    candidate_sha = _save_candidate(candidate_path)
    report = {
        "status": "pass",
        "stage": STAGE,
        "template": str(template_path),
        "template_sha256": file_sha256(template_path),
        "template_payload_sha256": template["payload_sha256"],
        "source": str(source_path),
        "source_sha256": source_sha_before,
        "source_unchanged": _source_unchanged(
            before_snapshot, after_snapshot, source_sha_before, source_sha_after
        ),
        "source_object": body.name,
        "observed_shirt": {
            "object": shirt.name,
            "vertex_count": len(shirt.data.vertices),
            "topology_sha256": shirt_topology["sha256"],
            "rest_sha256": shirt_rest["sha256"],
            "modifiers": len(shirt.modifiers),
            "shape_keys": len(shirt.data.shape_keys.key_blocks)
            if shirt.data.shape_keys
            else 0,
        },
        "target_patch": target_report,
        "target": target.name,
        "candidate": str(candidate_path),
        "candidate_sha256": candidate_sha,
        "cross": cross_report,
        "scope": (
            "Real Snow body template applied by closest base-rest triangle "
            "interpolation to a task-owned unbound different-topology patch derived "
            "from a bounded real Snow forearm region. This is not a complete clothing binding, fit, "
            "or full-character claim."
        ),
    }
    write_json(JOB / "output-cross-producer.json", report)
    return report


def cross_oracle():
    _verify_resources()
    template_path = _template_descriptor()
    candidate_path = _candidate_descriptor()
    template = validate_template(read_json(template_path))
    candidate_sha_before = file_sha256(candidate_path)
    open_blend(candidate_path)
    target = object_by_name(PARAMS["target_object"])
    rig = object_by_name(PARAMS["rig"])
    target_topology = _mesh_topology(target.data)
    target_rest = _mesh_rest(target.data, target_topology)
    mapping_groups = list(template["cross_region"]["used_deform_groups"])
    group_names = [group.name for group in target.vertex_groups]
    oracle = brute_force_mapping_oracle(template, target)
    names = {group.index: group.name for group in target.vertex_groups}
    observed = []
    for vertex in target.data.vertices:
        observed.append(
            {
                names[membership.group]: f32(membership.weight)
                for membership in vertex.groups
            }
        )
    max_weight_error = 0.0
    max_sum_error = 0.0
    mapping_mismatch = 0
    weight_tolerance = 2e-5
    for vertex_index, expected in enumerate(oracle["mapping"]):
        expected_weights = {
            name: f32(weight) for name, weight in expected["weights"].items()
        }
        actual = observed[vertex_index]
        for name in set(actual) | set(expected_weights):
            max_weight_error = max(
                max_weight_error,
                abs(actual.get(name, 0.0) - expected_weights.get(name, 0.0)),
            )
        if any(
            abs(actual.get(name, 0.0) - expected_weights.get(name, 0.0))
            > weight_tolerance
            for name in set(actual) | set(expected_weights)
        ):
            mapping_mismatch += 1
        max_sum_error = max(
            max_sum_error,
            abs(math.fsum(actual.values()) - expected["raw_sum"]),
        )
    checks = {
        "target_group_set": group_names == mapping_groups,
        "weights_within_oracle_tolerance": max_weight_error <= weight_tolerance,
        "mapping_vertex_count": len(observed) == len(oracle["mapping"]),
        "source_sum_preserved": max_sum_error <= template["cross_region"][
            "max_normalization_error"
        ],
        "target_topology_recorded": target_topology["sha256"]
        == PARAMS.get("cross_report", {}).get("target_topology_sha256"),
        "target_rest_recorded": target_rest["sha256"]
        == PARAMS.get("cross_report", {}).get("target_rest_sha256"),
    }
    lbs_snow = evaluated_lbs_check(
        target,
        rig,
        PARAMS.get("cross_report", {}),
        pose_bone=PARAMS.get("pose_bone", "UpperArm.L"),
        angle=float(PARAMS.get("pose_angle", 0.30)),
        tolerance=1e-3,
        deformation_model="SNOW_RIG_BBONE_RESIDUAL_BOUND",
    )
    proxy = create_proxy_rig(template, "WT-Cross-LBS-SimpleProxy")
    lbs_proxy = evaluated_lbs_check(
        target,
        proxy,
        PARAMS.get("cross_report", {}),
        pose_bone="DEF-Forearm_1.L",
        angle=float(PARAMS.get("pose_angle", 0.30)),
        tolerance=2e-5,
        deformation_model="SIMPLE_REST_BONE_LBS",
    )
    passed = (
        all(checks.values())
        and mapping_mismatch == 0
        and lbs_proxy["status"] == "pass"
        and lbs_proxy['model_match'] is True
        and lbs_snow['cleanup']['all_raw_pose_context_exact']
        and lbs_proxy['cleanup']['all_raw_pose_context_exact']
    )
    report = {
        "status": "pass" if passed else "failed",
        "stage": STAGE,
        "template": str(template_path),
        "template_sha256": file_sha256(template_path),
        "candidate": str(candidate_path),
        "candidate_sha256_before": candidate_sha_before,
        "candidate_sha256_after": file_sha256(candidate_path),
        "candidate_unchanged": candidate_sha_before == file_sha256(candidate_path),
        "target": target.name,
        "target_vertex_count": len(target.data.vertices),
        "target_topology_sha256": target_topology["sha256"],
        "target_rest_sha256": target_rest["sha256"],
        "mapping_groups": mapping_groups,
        "mapping_group_count": len(mapping_groups),
        "oracle": {
            "method": "PURE_PYTHON_ERICSON_BRUTE_FORCE_CLOSEST_TRIANGLE",
            "max_distance": oracle["max_distance"],
            "max_influences": oracle["max_influences"],
            "mapping_count": len(oracle["mapping"]),
        },
        "max_weight_error": max_weight_error,
        "weight_tolerance": weight_tolerance,
        "max_sum_error": max_sum_error,
        "mapping_mismatch": mapping_mismatch,
        "checks": checks,
        "lbs_snow_rig": lbs_snow,
        "lbs_simple_proxy": lbs_proxy,
        "deformation_gate": "independent_simple_proxy_model_match_only",
        "scope": (
            "Independent cold reopen and independent brute-force geometric oracle "
            "for a task-owned different-topology patch derived from the real Snow "
            "forearm; temporary Armature modifier LBS checks "
            "only, not a full clothing binding/clothing-fit claim."
        ),
    }
    write_json(JOB / "output-cross-oracle.json", report)
    if not passed:
        raise RuntimeError("Cross-topology oracle verification failed")
    return report


def _error_record(error):
    if isinstance(error, TemplateError):
        return {
            "code": error.code,
            "message": str(error),
            "details": error.details,
        }
    return {
        "code": getattr(error, "code", "NATIVE_ERROR"),
        "message": str(error),
        "traceback": traceback.format_exc(),
    }


def _transaction_observation(before, after, error):
    unchanged = before == after
    explicit_zero = error.details.get("zero_write")
    writes = error.details.get("writes")
    if explicit_zero is None:
        zero_write = bool(unchanged and writes in (None, 0))
        basis = "SNAPSHOT_UNCHANGED_PREFLIGHT"
    else:
        zero_write = bool(explicit_zero)
        basis = "ADAPTER_DETAILS"
    if writes is None and zero_write:
        writes = 0
    return zero_write, writes, basis


def _same_negative_case(template, source, rig, name, mutate):
    target = _copy_mesh_object_clean(source, name)
    if mutate:
        mutate(target)
    before, before_sha = snapshot_weights(target)
    try:
        apply_same_topology(template, target, rig)
        raise RuntimeError("Negative case unexpectedly succeeded")
    except TemplateError as error:
        after, after_sha = snapshot_weights(target)
        zero_write, writes, basis = _transaction_observation(before, after, error)
        return {
            "case": name,
            "status": "rejected",
            "error": _error_record(error),
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "state_unchanged": before == after,
            "zero_write": zero_write,
            "writes": writes,
            "zero_write_basis": basis,
        }


def negatives():
    _verify_resources()
    template_path = _template_descriptor()
    source_path = _source_descriptor()
    template = validate_template(read_json(template_path))
    source_sha_before = file_sha256(source_path)
    open_blend(source_path)
    source = object_by_name(PARAMS["source_object"])
    rig = object_by_name(PARAMS["rig"])
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    source_before = source_content_snapshot(source, rig, deform)
    tests = []

    def stale_topology(target):
        target.data.vertices.add(1)
        target.data.update()

    tests.append(
        _same_negative_case(
            template, source, rig, "WT-Neg-StaleTopology", stale_topology
        )
    )

    def stale_rest(target):
        target.data.vertices[0].co.x += 0.001
        target.data.update()

    tests.append(
        _same_negative_case(template, source, rig, "WT-Neg-StaleRest", stale_rest)
    )

    locked = _copy_mesh_object_clean(source, "WT-Neg-LockedGroup")
    apply_same_topology(template, locked, rig)
    locked.vertex_groups[0].lock_weight = True
    before_locked, before_locked_sha = snapshot_weights(locked)
    try:
        apply_same_topology(template, locked, rig)
        raise RuntimeError("Locked group case unexpectedly succeeded")
    except TemplateError as error:
        after_locked, after_locked_sha = snapshot_weights(locked)
        zero_write, writes, basis = _transaction_observation(
            before_locked, after_locked, error
        )
        tests.append(
            {
                "case": "WT-Neg-LockedGroup",
                "status": "rejected",
                "error": _error_record(error),
                "before_sha256": before_locked_sha,
                "after_sha256": after_locked_sha,
                "state_unchanged": before_locked == after_locked,
                "zero_write": zero_write,
                "writes": writes,
                "zero_write_basis": basis,
            }
        )

    missing_target = _copy_mesh_object_clean(source, "WT-Neg-MissingBone")
    missing_bone = template["group_map"][0]["bone"]
    proxy = create_proxy_rig(template, "WT-Neg-ProxyRig", missing_bone)
    before_missing, before_missing_sha = snapshot_weights(missing_target)
    try:
        apply_same_topology(template, missing_target, proxy)
        raise RuntimeError("Missing bone case unexpectedly succeeded")
    except TemplateError as error:
        after_missing, after_missing_sha = snapshot_weights(missing_target)
        zero_write, writes, basis = _transaction_observation(
            before_missing, after_missing, error
        )
        tests.append(
            {
                "case": "WT-Neg-MissingBone",
                "status": "rejected",
                "missing_bone": missing_bone,
                "error": _error_record(error),
                "before_sha256": before_missing_sha,
                "after_sha256": after_missing_sha,
                "state_unchanged": before_missing == after_missing,
                "zero_write": zero_write,
                "writes": writes,
                "zero_write_basis": basis,
            }
        )

    budget_target = _copy_mesh_object_clean(source, "WT-Neg-Budget")
    apply_same_topology(template, budget_target, rig)
    before_budget, before_budget_sha = snapshot_weights(budget_target)
    try:
        apply_same_topology(
            template,
            budget_target,
            rig,
            budget_override={
                "max_influences": template["budgets"]["same_topology_max_influences"] - 1
            },
        )
        raise RuntimeError("Budget case unexpectedly succeeded")
    except TemplateError as error:
        after_budget, after_budget_sha = snapshot_weights(budget_target)
        zero_write, writes, basis = _transaction_observation(
            before_budget, after_budget, error
        )
        tests.append(
            {
                "case": "WT-Neg-InfluenceBudget",
                "status": "rejected",
                "error": _error_record(error),
                "before_sha256": before_budget_sha,
                "after_sha256": after_budget_sha,
                "state_unchanged": before_budget == after_budget,
                "zero_write": zero_write,
                "writes": writes,
                "zero_write_basis": basis,
            }
        )

    rollback_target = _copy_mesh_object_clean(source, "WT-Neg-Rollback")
    apply_same_topology(template, rollback_target, rig)
    rollback_target.vertex_groups[5].remove(
        list(range(len(rollback_target.data.vertices)))
    )
    rollback_target.data.update()
    before_rollback, before_rollback_sha = snapshot_weights(rollback_target)
    try:
        apply_same_topology(
            template,
            rollback_target,
            rig,
            fault_after_writes=len(rollback_target.vertex_groups) + 2,
        )
        raise RuntimeError("Rollback case unexpectedly succeeded")
    except TemplateError as error:
        after_rollback, after_rollback_sha = snapshot_weights(rollback_target)
        zero_write, writes, basis = _transaction_observation(
            before_rollback, after_rollback, error
        )
        tests.append(
            {
                "case": "WT-Neg-InjectedRollback",
                "status": "rejected",
                "error": _error_record(error),
                "before_sha256": before_rollback_sha,
                "after_sha256": after_rollback_sha,
                "state_unchanged": before_rollback == after_rollback,
                "zero_write": zero_write,
                "writes": writes,
                "zero_write_basis": basis,
                "rollback": error.details.get("rollback"),
            }
        )

    cross_target, cross_target_report = build_target_patch(
        source, rig, template, "WT-Neg-CrossDistanceBudget"
    )
    for vertex in cross_target.data.vertices:
        vertex.co.z += 0.10
    cross_target.data.update()
    before_cross, before_cross_sha = snapshot_weights(cross_target)
    try:
        apply_cross_topology(
            template,
            cross_target,
            rig,
            target_source_report=cross_target_report,
        )
        raise RuntimeError("Cross distance budget case unexpectedly succeeded")
    except TemplateError as error:
        after_cross, after_cross_sha = snapshot_weights(cross_target)
        zero_write, writes, basis = _transaction_observation(
            before_cross, after_cross, error
        )
        tests.append(
            {
                "case": "WT-Neg-CrossDistanceBudget",
                "status": "rejected",
                "error": _error_record(error),
                "before_sha256": before_cross_sha,
                "after_sha256": after_cross_sha,
                "state_unchanged": before_cross == after_cross,
                "zero_write": zero_write,
                "writes": writes,
                "zero_write_basis": basis,
            }
        )

    source_after = source_content_snapshot(source, rig, deform)
    source_sha_after = file_sha256(source_path)
    rollback_test = next(
        item for item in tests if item["case"] == "WT-Neg-InjectedRollback"
    )
    passed = (
        all(item["status"] == "rejected" for item in tests)
        and all(item["state_unchanged"] for item in tests)
        and all(item["zero_write"] for item in tests if item["case"] != "WT-Neg-InjectedRollback")
        and rollback_test.get("rollback", {}).get("ok") is True
        and source_before == source_after
        and source_sha_before == source_sha_after
    )
    report = {
        "status": "pass" if passed else "failed",
        "stage": STAGE,
        "template": str(template_path),
        "template_sha256": file_sha256(template_path),
        "source": str(source_path),
        "source_sha256": source_sha_before,
        "source_unchanged": _source_unchanged(
            source_before, source_after, source_sha_before, source_sha_after
        ),
        "tests": tests,
        "scope": (
            "Zero-write preflight rejection for stale topology, stale rest, locked "
            "groups, missing bones and an explicit influence budget; plus a real "
            "mid-write fault injection with full snapshot rollback."
        ),
    }
    write_json(JOB / "output-negatives.json", report)
    if not passed:
        raise RuntimeError("Negative transaction verification failed")
    return report


STAGES = {
    "same_producer": same_producer,
    "same_cold": same_cold,
    "cross_producer": cross_producer,
    "cross_oracle": cross_oracle,
    "negatives": negatives,
}


def main():
    if STAGE not in STAGES:
        raise RuntimeError("Unknown stage: " + STAGE)
    return STAGES[STAGE]()


try:
    result = main()
    if isinstance(result, dict):
        result = {
            key: result[key]
            for key in ("status", "stage", "source_sha256", "candidate_sha256")
            if key in result
        }
except Exception as error:
    failure = {
        "status": "failed",
        "stage": STAGE,
        "error": _error_record(error),
    }
    report_path = JOB / ("output-" + STAGE.replace("_", "-") + ".json")
    try:
        if not report_path.exists():
            write_json(report_path, failure)
        else:
            write_json(JOB / ("output-" + STAGE.replace("_", "-") + "-failure.json"), failure)
    finally:
        result = failure
