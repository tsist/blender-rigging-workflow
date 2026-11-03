"""Native Blender implementation for the owned Snow weight template.

The adapter is offline and task-owned. It mirrors the mature
SURFACE_BARYCENTRIC_V1 closest-triangle/barycentric design without changing or
widening the public blenderctl guard. Raw source sums are preserved; no
normalization or influence pruning occurs.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree

from .contract import SCHEMA, MODE, TemplateError, seal_template, validate_template, decode_json
from .core import (
    canonical_bytes,
    f32,
    f32_hex,
    raw_rows_for_vertex,
    rest_sha256,
    sha256_value,
    sum_rows,
    topology_sha256,
    vertices_sha256,
)


LICENSE = {
    "asset": "CC BY 4.0",
    "url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Snow Rig (CC) Blender Foundation | studio.blender.org",
    "embedded_script": "CloudRig GPL-3.0-or-later is separate from the CC BY 4.0 asset license and is not executed.",
    "distribution": "Preserve this attribution and license with the template and any derived evidence.",
}


class InjectedFailure(RuntimeError):
    pass


def _require(condition, code, message, details=None):
    if not condition:
        raise TemplateError(code, message, details or {})


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return decode_json(stream.read())


def write_json(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    Path(path).write_text(raw + "\n", encoding="utf-8")


def open_blend(path):
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    bpy.context.view_layer.update()
    _require(
        Path(bpy.data.filepath).resolve() == Path(path).resolve(),
        "OPEN_CONFLICT",
        "Opened filepath differs from the pinned path",
    )


def object_by_name(name):
    item = bpy.data.objects.get(name)
    _require(item is not None, "NOT_FOUND", "Missing object: " + str(name))
    return item


def _mesh_topology(mesh):
    mesh.calc_loop_triangles()
    return {
        "vertex_count": len(mesh.vertices),
        "edge_count": len(mesh.edges),
        "polygon_count": len(mesh.polygons),
        "loop_count": len(mesh.loops),
        "triangle_count": len(mesh.loop_triangles),
        "sha256": topology_sha256(
            len(mesh.vertices),
            [list(edge.vertices) for edge in mesh.edges],
            [list(face.vertices) for face in mesh.polygons],
        ),
        "edges": [list(edge.vertices) for edge in mesh.edges],
        "polygons": [list(face.vertices) for face in mesh.polygons],
        "triangles": [list(triangle.vertices) for triangle in mesh.loop_triangles],
    }


def _mesh_rest(mesh, topology):
    vertices = [[float(value) for value in vertex.co] for vertex in mesh.vertices]
    return {
        "vertex_count": len(vertices),
        "vertices": vertices,
        "vertices_sha256": vertices_sha256(vertices),
        "sha256": rest_sha256(len(vertices), vertices, topology["sha256"]),
    }


def _matrix_value(matrix):
    return [[float(value) for value in row] for row in matrix]


def _shape_key_signature(mesh):
    if not mesh.shape_keys:
        return []
    result = []
    for key in mesh.shape_keys.key_blocks:
        digest = hashlib.sha256()
        for point in key.data:
            digest.update(struct.pack("<3f", *point.co))
        result.append(
            {
                "name": key.name,
                "value": float(key.value),
                "relative_key": key.relative_key.name if key.relative_key else None,
                "vertex_group": key.vertex_group,
                "mute": bool(key.mute),
                "coordinates_sha256": digest.hexdigest(),
            }
        )
    return result


def _modifier_signature(obj):
    return [
        {
            "name": modifier.name,
            "type": modifier.type,
            "object": modifier.object.name if getattr(modifier, "object", None) else None,
        }
        for modifier in obj.modifiers
    ]


def _group_signature(obj):
    return [
        {"index": group.index, "name": group.name, "lock": bool(group.lock_weight)}
        for group in obj.vertex_groups
    ]


def source_content_snapshot(obj, rig, deform_bones):
    topology = _mesh_topology(obj.data)
    rest = _mesh_rest(obj.data, topology)
    rows = raw_rows_for_vertex(obj, deform_bones)
    value = {
        "object": obj.name,
        "mesh": obj.data.name,
        "matrix_world": _matrix_value(obj.matrix_world),
        "parent": obj.parent.name if obj.parent else None,
        "modifiers": _modifier_signature(obj),
        "shape_keys": _shape_key_signature(obj.data),
        "groups": _group_signature(obj),
        "weights": rows,
        "topology_sha256": topology["sha256"],
        "rest_sha256": rest["sha256"],
        "rig": {
            "object": rig.name,
            "data": rig.data.name,
            "pose_position": rig.data.pose_position,
            "bones": _rig_rest_signature(rig, relevant_only=False),
        },
    }
    return value, sha256_value(value)


def _rig_rest_signature(rig, relevant_only=False, names=None):
    selected = rig.data.bones
    if relevant_only:
        selected = [rig.data.bones[name] for name in sorted(names or ())]
    return [
        {
            "name": bone.name,
            "parent": bone.parent.name if bone.parent else None,
            "use_deform": bool(bone.use_deform),
            "matrix": [[float(value) for value in row] for row in bone.matrix_local],
            "head": [float(value) for value in bone.head_local],
            "tail": [float(value) for value in bone.tail_local],
            "length": float(bone.length),
        }
        for bone in selected
    ]


def _bone_rest_descriptor(bone):
    return {
        "name": bone.name,
        "parent": bone.parent.name if bone.parent else None,
        "use_deform": bool(bone.use_deform),
        "head_local": [float(value) for value in bone.head_local],
        "tail_local": [float(value) for value in bone.tail_local],
        "matrix_local": [[float(value) for value in row] for row in bone.matrix_local],
        "length": float(bone.length),
    }


def _segment_distance(point, start, end):
    edge = end - start
    denominator = edge.length_squared
    if denominator <= 1e-30:
        return (point - start).length
    t = max(0.0, min(1.0, (point - start).dot(edge) / denominator))
    return (point - (start + edge * t)).length


def _region_from_bones(vertices_world, deform_sums, bones, segment_names, radius, rig_matrix):
    segments = []
    for name in segment_names:
        bone = bones.get(name)
        _require(bone is not None, "MISSING_BONE", "Region segment bone missing: " + name)
        segments.append(
            (
                name,
                rig_matrix @ Vector(bone["head_local"]),
                rig_matrix @ Vector(bone["tail_local"]),
            )
        )
    selected = []
    for index, point in enumerate(vertices_world):
        if abs(deform_sums[index] - 1.0) > 1e-6:
            continue
        if min(_segment_distance(point, start, end) for _, start, end in segments) <= radius:
            selected.append(index)
    return selected, segments


def _copy_mesh_object_clean(source, name):
    target = source.copy()
    target.data = source.data.copy()
    target.data.name = name + "-Mesh"
    target.name = name
    bpy.context.scene.collection.objects.link(target)
    target.parent = None
    target.matrix_world = source.matrix_world.copy()
    target.vertex_groups.clear()
    target.modifiers.clear()
    target.constraints.clear()
    if target.animation_data:
        target.animation_data_clear()
    if target.data.animation_data:
        target.data.animation_data_clear()
    if target.data.shape_keys:
        target.shape_key_clear()
    target.data.update()
    return target


def _link_object(obj):
    if obj.name not in bpy.context.scene.collection.objects:
        bpy.context.scene.collection.objects.link(obj)


def _group_stats(obj, deform_bones):
    names = {group.index: group.name for group in obj.vertex_groups}
    member_vertices = [0] * len(obj.vertex_groups)
    positive_vertices = [0] * len(obj.vertex_groups)
    total_positive = [0.0] * len(obj.vertex_groups)
    for vertex in obj.data.vertices:
        for membership in vertex.groups:
            index = membership.group
            member_vertices[index] += 1
            if membership.weight > 0:
                positive_vertices[index] += 1
                total_positive[index] += float(membership.weight)
    result = []
    for group in obj.vertex_groups:
        name = group.name
        deform = name in deform_bones
        if deform:
            classification = "DEFORM"
        elif positive_vertices[group.index] == 0:
            classification = "UNUSED"
        elif name.startswith(("Mask", "mask", "Side.")):
            classification = "MASK"
        else:
            classification = "CORRECTIVE_OR_HELPER"
        result.append(
            {
                "index": group.index,
                "name": name,
                "bone": name if deform else None,
                "deform": deform,
                "lock": bool(group.lock_weight),
                "member_vertices": member_vertices[group.index],
                "positive_vertices": positive_vertices[group.index],
                "total_positive_weight": float(total_positive[group.index]),
                "classification": classification,
            }
        )
    return result


def _weight_stats(group_map, rows, all_sums, deform_sums):
    counts_all = [len(row) for row in rows]
    counts_deform = [sum(item["deform"] for item in row) for row in rows]
    tolerance = 1e-6
    return {
        "group_count": len(group_map),
        "used_group_count": sum(group["positive_vertices"] > 0 for group in group_map),
        "deform_group_count": sum(group["deform"] for group in group_map),
        "used_deform_group_count": sum(
            group["deform"] and group["positive_vertices"] > 0 for group in group_map
        ),
        "max_influences_all": max(counts_all, default=0),
        "max_influences_deform": max(counts_deform, default=0),
        "total_memberships": sum(counts_all),
        "zero_memberships": 0,
        "unweighted_deform_vertices": sum(item <= 1e-8 for item in deform_sums),
        "nonunit_deform_vertices": sum(abs(item - 1.0) > tolerance for item in deform_sums),
        "deform_sum_min": min(deform_sums),
        "deform_sum_max": max(deform_sums),
        "nonunit_tolerance": tolerance,
    }


def _clean_rows(obj, deform_bones):
    rows = raw_rows_for_vertex(obj, deform_bones)
    result = []
    for vertex_index, row in enumerate(rows):
        clean = []
        for item in row:
            weight = float(item["weight"])
            _require(math.isfinite(weight) and weight > 0, 'UNSUPPORTED_MEMBERSHIP',
                     'Sparse positive-only template cannot silently discard a raw membership',
                     {'vertex': vertex_index, 'group': item['group'], 'weight': repr(weight), 'writes': 0, 'zero_write': True})
            clean.append(
                {
                    "group_index": int(item["group_index"]),
                    "group": item["group"],
                    "bone": item["bone"],
                    "weight": weight,
                    "deform": bool(item["deform"]),
                }
            )
        result.append(clean)
    return result


def _select_cross_region(source_obj, source_rows, deform_sums, group_map, rig):
    bones = {bone.name: _bone_rest_descriptor(bone) for bone in rig.data.bones}
    segment_names = [
        "DEF-Forearm_1.L",
        "DEF-Forearm_2.L",
    ]
    world = [source_obj.matrix_world @ vertex.co for vertex in source_obj.data.vertices]
    radius = 0.065
    selected, _ = _region_from_bones(
        world, deform_sums, bones, segment_names, radius, rig.matrix_world
    )
    _require(selected, "REGION_EMPTY", "No source vertices matched the bounded arm region")
    selected_set = set(selected)
    topology = _mesh_topology(source_obj.data)
    triangles = topology["triangles"]
    triangle_indices = [
        index for index, ids in enumerate(triangles) if all(vertex in selected_set for vertex in ids)
    ]
    _require(triangle_indices, "REGION_EMPTY", "No source triangles matched the bounded arm region")
    weird = []
    for index in triangle_indices:
        for vertex in triangles[index]:
            if abs(deform_sums[vertex] - 1.0) > 1e-6:
                weird.append({"triangle": index, "vertex": vertex})
    _require(not weird, "REGION_SUM", "Bounded source region is not deform-normalized")
    used = set()
    max_region_influences = 0
    for index in selected:
        deform_row = [item for item in source_rows[index] if item["deform"]]
        max_region_influences = max(max_region_influences, len(deform_row))
        for item in deform_row:
            used.add(item["group"])
    used = sorted(used)
    _require(used, "REGION_EMPTY", "Bounded region has no deform groups")
    # The full-body source maximum is the contract's source budget. The region
    # cannot exceed it, and the cross target is bounded by three source corners.
    full_max = max(
        len([item for item in row if item["deform"]]) for row in source_rows
    )
    _require(
        max_region_influences <= full_max,
        "REGION_BUDGET",
        "Region influences exceed full-body source budget",
    )
    return {
        "name": "snow_left_forearm_region",
        "definition": (
            "Body vertices with deform sum within 1e-6 of one whose rest position is "
            "within 0.065 m of the named left-forearm deform bone segments; retain source "
            "triangles whose three vertices all qualify."
        ),
        "segment_bones": segment_names,
        "radius": radius,
        "vertex_indices": selected,
        "triangle_indices": triangle_indices,
        "vertex_count": len(selected),
        "triangle_count": len(triangle_indices),
        "used_deform_groups": used,
        "max_source_influences": full_max,
        "max_target_influences": min(32, full_max * 3),
        "max_distance": 0.03,
        "max_normalization_error": 1e-6,
    }


def extract_template(source_obj, rig, target_obj, source_path, source_sha):
    _require(source_obj.type == "MESH", "INVALID_SOURCE", "Source must be a mesh")
    _require(rig.type == "ARMATURE", "INVALID_RIG", "Source rig must be an armature")
    _require(not source_obj.library and not source_obj.override_library, "UNSUPPORTED", "Source must be local")
    _require(not rig.library and not rig.override_library, "UNSUPPORTED", "Rig must be local")
    deform_bones = {bone.name for bone in rig.data.bones if bone.use_deform}
    topology = _mesh_topology(source_obj.data)
    rest = _mesh_rest(source_obj.data, topology)
    rows = _clean_rows(source_obj, deform_bones)
    all_sums = sum_rows(rows, deform=False)
    deform_sums = sum_rows(rows, deform=True)
    group_map = _group_stats(source_obj, deform_bones)
    stats = _weight_stats(group_map, rows, all_sums, deform_sums)
    cross_region = _select_cross_region(
        source_obj, rows, deform_sums, group_map, rig
    )
    snapshot, source_content_sha = source_content_snapshot(source_obj, rig, deform_bones)
    relevant_names = sorted({group["bone"] for group in group_map if group["deform"]})
    relevant_bones = [
        _bone_rest_descriptor(rig.data.bones[name]) for name in relevant_names
    ]
    non_armature = [
        modifier for modifier in source_obj.modifiers if modifier.type != "ARMATURE"
    ]
    target_modifiers = list(target_obj.modifiers)
    target_shape_keys = len(target_obj.data.shape_keys.key_blocks) if target_obj.data.shape_keys else 0
    compatibility = {
        "adapter": "SURFACE_BARYCENTRIC_V1",
        "applicable": False,
        "reasons": [
            "public source mesh guard rejects shape keys; real Snow body has "
            + str(len(snapshot["shape_keys"]))
            + " key blocks",
            "public source guard permits only Armature modifiers; real Snow body has "
            + str(len(non_armature))
            + " non-Armature modifiers",
            "real Snow body has "
            + str(stats["unweighted_deform_vertices"])
            + " vertices whose mapped deform sum is zero and "
            + str(stats["nonunit_deform_vertices"])
            + " non-unit deform sums; selected groups do not normalize at every vertex",
            "public target guard rejects parented/modifier-bearing meshes; real Snow shirt has "
            + str(len(target_modifiers))
            + " modifiers and "
            + str(target_shape_keys)
            + " key blocks",
        ],
        "observed": {
            "source_shape_keys": len(snapshot["shape_keys"]),
            "source_non_armature_modifiers": len(non_armature),
            "source_unweighted_deform_vertices": stats["unweighted_deform_vertices"],
            "source_nonunit_deform_vertices": stats["nonunit_deform_vertices"],
            "target_modifiers": len(target_modifiers),
            "target_shape_keys": target_shape_keys,
        },
    }
    template = {
        "schema": SCHEMA,
        "template_id": "snow.geo-snow-body.skin-weights.v1.20261030",
        "revision": 1,
        "mode": MODE,
        "provenance": {
            "source_file": str(Path(source_path).resolve()),
            "source_file_sha256": source_sha,
            "source_object": source_obj.name,
            "source_mesh": source_obj.data.name,
            "source_content_sha256": source_content_sha,
            "blender_version": list(bpy.app.version),
            "blender_build": bpy.app.build_hash.decode(),
            "generated_by": "v068/weight_template/native.py",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "public_transfer_compatibility": compatibility,
        },
        "license": dict(LICENSE),
        "source": {
            "object": source_obj.name,
            "mesh": source_obj.data.name,
            "coordinate_space": "OBJECT_LOCAL_BASE_REST",
            "unit_scale": 1.0,
            "object_matrix_world": _matrix_value(source_obj.matrix_world),
            "topology": {
                key: topology[key]
                for key in (
                    "vertex_count",
                    "edge_count",
                    "polygon_count",
                    "loop_count",
                    "triangle_count",
                    "sha256",
                )
            },
            "rest": {
                "vertex_count": rest["vertex_count"],
                "vertices_sha256": rest["vertices_sha256"],
                "sha256": rest["sha256"],
            },
            "index": list(range(rest["vertex_count"])),
            "vertices": rest["vertices"],
            "triangles": topology["triangles"],
        },
        "rig": {
            "object": rig.name,
            "data": rig.data.name,
            "bone_count": len(rig.data.bones),
            "deform_bone_count": len(deform_bones),
            "rest_sha256": sha256_value(_rig_rest_signature(rig, relevant_only=False)),
            "relevant_rest_sha256": sha256_value(relevant_bones),
            "bones": relevant_bones,
        },
        "group_map": group_map,
        "weights": {
            "sum_policy": "PRESERVE_SOURCE_DEFORM_SUM",
            "normalization": "NONE",
            "vertices": rows,
            "all_sums": all_sums,
            "deform_sums": deform_sums,
            "stats": stats,
        },
        "cross_region": cross_region,
        "budgets": {
            "same_topology_max_influences": stats["max_influences_deform"],
            "same_topology_max_groups": len(group_map),
            "cross_topology_max_influences": cross_region["max_target_influences"],
            "cross_topology_max_groups": len(cross_region["used_deform_groups"]),
            "cross_max_distance": cross_region["max_distance"],
            "cross_sum_tolerance": cross_region["max_normalization_error"],
            "same_rest_tolerance": 0.0,
        },
        "application_rules": {
            "same_topology": "EXACT_INDEX_REST_AND_GROUP_MAP",
            "cross_topology": "SURFACE_BARYCENTRIC_V1_OFFLINE_PRESERVE_SOURCE_SUM",
            "target_group_policy": "BLANK_OR_EXACT_REPLACE",
            "rollback": "FULL_WEIGHT_SNAPSHOT",
            "no_silent_pruning": True,
            "no_silent_normalization": True,
            "bound_claim": "NONE",
        },
    }
    return seal_template(template), snapshot


def snapshot_weights(obj):
    rows = []
    for vertex in obj.data.vertices:
        rows.append(
            sorted(
                [
                    {
                        "group_index": int(membership.group),
                        "name": obj.vertex_groups[membership.group].name,
                        "weight": float(membership.weight),
                    }
                    for membership in vertex.groups
                ],
                key=lambda item: item["group_index"],
            )
        )
    value = {
        "groups": _group_signature(obj),
        "vertices": rows,
        "active_index": int(obj.vertex_groups.active_index),
    }
    return value, sha256_value(value)


def _restore_weights(obj, snapshot):
    obj.vertex_groups.clear()
    groups = []
    for item in snapshot["groups"]:
        group = obj.vertex_groups.new(name=item["name"])
        group.lock_weight = bool(item["lock"])
        groups.append(group)
    for vertex_index, row in enumerate(snapshot["vertices"]):
        for membership in row:
            groups[membership["group_index"]].add(
                [vertex_index], float(membership["weight"]), "REPLACE"
            )
    obj.vertex_groups.active_index = snapshot['active_index']
    obj.data.update()


def _rows_equal(observed, expected):
    if len(observed) != len(expected):
        return False
    for left, right in zip(observed, expected):
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if a["group_index"] != b["group_index"]:
                return False
            if a["group"] != b["group"]:
                return False
            if f32(a["weight"]) != f32(b["weight"]):
                return False
    return True


def _topology_matches(obj, template):
    topology = _mesh_topology(obj.data)
    source_topology = template["source"]["topology"]
    return (
        topology["vertex_count"] == source_topology["vertex_count"]
        and topology["edge_count"] == source_topology["edge_count"]
        and topology["polygon_count"] == source_topology["polygon_count"]
        and topology["loop_count"] == source_topology["loop_count"]
        and topology["triangle_count"] == source_topology["triangle_count"]
        and topology["sha256"] == source_topology["sha256"]
    )


def _rest_matches(obj, template):
    topology = _mesh_topology(obj.data)
    rest = _mesh_rest(obj.data, topology)
    return (
        rest["vertex_count"] == template["source"]["rest"]["vertex_count"]
        and rest["vertices_sha256"] == template["source"]["rest"]["vertices_sha256"]
        and rest["sha256"] == template["source"]["rest"]["sha256"]
    )


def _mapped_bones(template):
    return sorted({group["bone"] for group in template["group_map"] if group["deform"]})


def _validate_rig(template, rig):
    _require(rig is not None and rig.type == "ARMATURE", "INVALID_RIG", "Rig must be an armature")
    _require(not rig.library and not rig.override_library, "UNSUPPORTED", "Rig must be local")
    for name in _mapped_bones(template):
        bone = rig.data.bones.get(name)
        _require(bone is not None, "MISSING_BONE", "Required deform bone missing: " + name)
        _require(bone.use_deform, "MISSING_BONE", "Required bone is not deform: " + name)
    relevant = {
        item["name"]: _bone_rest_descriptor(rig.data.bones[item["name"]])
        for item in template["rig"]["bones"]
    }
    _require(
        sha256_value([relevant[item["name"]] for item in template["rig"]["bones"]])
        == template["rig"]["relevant_rest_sha256"],
        "RIG_REST_CONFLICT",
        "Relevant rig rest no longer matches the template",
    )


def _check_group_defs(template, target):
    names = [group.name for group in target.vertex_groups]
    expected = [group["name"] for group in template["group_map"]]
    if not names:
        return "BLANK"
    _require(
        names == expected,
        "GROUP_CONFLICT",
        "Target group order/names differ from the complete template",
    )
    for group in target.vertex_groups:
        _require(not group.lock_weight, "LOCKED_GROUP", "Target group is locked: " + group.name)
    return "EXACT"


def apply_same_topology(
    template,
    target,
    rig,
    *,
    fault_after_writes=None,
    budget_override=None,
    note=None,
):
    validate_template(template)
    _require(target.type == "MESH", "INVALID_TARGET", "Target must be a mesh")
    _require(
        not target.library and not target.override_library,
        "UNSUPPORTED",
        "Target must be local and non-override",
    )
    _require(target.parent is None, "UNSUPPORTED", "Target must be unparented")
    _require(not target.modifiers, "UNSUPPORTED", "Target must have no modifiers")
    _require(not target.data.shape_keys, "UNSUPPORTED", "Target must have no shape keys")
    _require(target.data.users == 1, "CONFLICT", "Target mesh data must be single-user")
    _require(_topology_matches(target, template), "TOPOLOGY_CONFLICT", "Same-topology target SHA differs")
    _require(_rest_matches(target, template), "REST_CONFLICT", "Same-topology target rest differs")
    _validate_rig(template, rig)
    group_mode = _check_group_defs(template, target)
    budget_max = (
        budget_override["max_influences"]
        if budget_override and "max_influences" in budget_override
        else template["budgets"]["same_topology_max_influences"]
    )
    _require(
        template["weights"]["stats"]["max_influences_deform"] <= budget_max,
        "INFLUENCE_BUDGET",
        "Template deform influence count exceeds the explicit same-topology budget",
        {
            "observed": template["weights"]["stats"]["max_influences_deform"],
            "budget": budget_max,
            "writes": 0,
            "zero_write": True,
        },
    )
    _require(
        len(template["group_map"]) <= template["budgets"]["same_topology_max_groups"],
        "GROUP_BUDGET",
        "Template group count exceeds the explicit same-topology budget",
        {"writes": 0, "zero_write": True},
    )
    planned = [
        [
            {
                "group_index": item["group_index"],
                "group": item["group"],
                "weight": f32(item["weight"]),
                "deform": item["deform"],
            }
            for item in sorted(row, key=lambda item: item["group_index"])
        ]
        for row in template["weights"]["vertices"]
    ]
    for row in planned:
        _require(
            len([item for item in row if item["deform"]]) <= budget_max,
            "INFLUENCE_BUDGET",
            "Per-vertex influence budget exceeded",
            {"writes": 0, "zero_write": True},
        )
    before, before_sha = snapshot_weights(target)
    writes = 0
    mutated = False
    rollback = {"attempted": False, "ok": True, "errors": []}
    try:
        if group_mode == "BLANK":
            groups = []
            for item in template["group_map"]:
                group = target.vertex_groups.new(name=item["name"])
                group.lock_weight = bool(item["lock"])
                groups.append(group)
                writes += 1
                mutated = True
                if fault_after_writes is not None and writes >= fault_after_writes:
                    raise InjectedFailure("Injected failure after group creation")
        else:
            groups = list(target.vertex_groups)
        for group in groups:
            group.remove(list(range(len(target.data.vertices))))
            writes += 1
            mutated = True
            if fault_after_writes is not None and writes >= fault_after_writes:
                raise InjectedFailure("Injected failure while clearing target weights")
        for vertex_index, row in enumerate(planned):
            for item in row:
                groups[item["group_index"]].add(
                    [vertex_index], float(item["weight"]), "REPLACE"
                )
                writes += 1
                mutated = True
                if fault_after_writes is not None and writes >= fault_after_writes:
                    raise InjectedFailure("Injected failure after a weight write")
        target.data.update()
        bpy.context.view_layer.update()
        _require(_topology_matches(target, template), "POSTCONDITION", "Topology changed during apply")
        _require(_rest_matches(target, template), "POSTCONDITION", "Rest geometry changed during apply")
        # Use the template's per-membership deform flag, not the target's names,
        # to compare the complete raw storage.
        observed = []
        names = {group.index: group.name for group in target.vertex_groups}
        template_deform = {group["name"]: group["deform"] for group in template["group_map"]}
        for vertex in target.data.vertices:
            row = []
            for membership in sorted(vertex.groups, key=lambda item: item.group):
                name = names[membership.group]
                row.append(
                    {
                        "group_index": int(membership.group),
                        "group": name,
                        "weight": float(membership.weight),
                        "deform": bool(template_deform[name]),
                    }
                )
            observed.append(row)
        _require(
            _rows_equal(observed, planned),
            "WEIGHT_READBACK",
            "Stored same-topology weights differ from the template",
        )
        observed_all = sum_rows(observed, deform=False)
        observed_deform = sum_rows(observed, deform=True)
        for index in range(len(observed)):
            _require(
                abs(observed_all[index] - template["weights"]["all_sums"][index]) <= 1e-9,
                "SUM_CONTRACT",
                "Stored all-group sum differs at vertex " + str(index),
            )
            _require(
                abs(observed_deform[index] - template["weights"]["deform_sums"][index]) <= 1e-9,
                "SUM_CONTRACT",
                "Stored deform sum differs at vertex " + str(index),
            )
        after, after_sha = snapshot_weights(target)
        _require(
            [group["name"] for group in after["groups"]]
            == [group["name"] for group in before["groups"] or after["groups"]],
            "POSTCONDITION",
            "Unexpected target group set",
        )
        return {
            "status": "pass",
            "adapter": "SAME_TOPOLOGY_INDEX_V1",
            "target": target.name,
            "rig": rig.name,
            "target_topology_sha256": _mesh_topology(target.data)["sha256"],
            "target_rest_sha256": _mesh_rest(target.data, _mesh_topology(target.data))["sha256"],
            "group_mode": group_mode,
            "groups": len(template["group_map"]),
            "used_groups": template["weights"]["stats"]["used_group_count"],
            "used_deform_groups": template["weights"]["stats"]["used_deform_group_count"],
            "max_influences": template["weights"]["stats"]["max_influences_deform"],
            "max_influences_all": template["weights"]["stats"]["max_influences_all"],
            "writes": writes,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "sum_policy": template["weights"]["sum_policy"],
            "nonunit_deform_vertices": template["weights"]["stats"]["nonunit_deform_vertices"],
            "unweighted_deform_vertices": template["weights"]["stats"]["unweighted_deform_vertices"],
            "rollback": rollback,
            "note": note,
        }
    except Exception as error:
        if mutated:
            rollback["attempted"] = True
            try:
                _restore_weights(target, before)
                bpy.context.view_layer.update()
                restored, restored_sha = snapshot_weights(target)
                if restored != before:
                    raise TemplateError(
                        "ROLLBACK_READBACK",
                        "Restored weight snapshot differs from the pre-write snapshot",
                    )
                rollback["ok"] = True
                rollback["restored_sha256"] = restored_sha
            except Exception as restore_error:
                rollback["ok"] = False
                rollback["errors"].append(
                    {"type": type(restore_error).__name__, "message": str(restore_error)}
                )
                raise TemplateError(
                    "ROLLBACK_FAILED",
                    "Same-topology write failed and rollback did not restore the snapshot",
                    {
                        "cause": type(error).__name__ + ": " + str(error),
                        "rollback": rollback,
                        "writes": writes,
                    },
                ) from error
        if isinstance(error, TemplateError):
            error.details = {
                **error.details,
                "writes": writes,
                "zero_write": writes == 0,
                "rollback": rollback,
            }
            raise
        raise TemplateError(
            "NATIVE_ERROR",
            type(error).__name__ + ": " + str(error),
            {
                "writes": writes,
                "zero_write": writes == 0,
                "rollback": rollback,
                "cause": type(error).__name__,
            },
        ) from error


def _barycentric(point, a, b, c):
    u = b - a
    v = c - a
    w = point - a
    uu = u.dot(u)
    uv = u.dot(v)
    vv = v.dot(v)
    wu = w.dot(u)
    wv = w.dot(v)
    denominator = uu * vv - uv * uv
    _require(denominator > 1e-30, "DEGENERATE_TRIANGLE", "Degenerate source triangle")
    y = (vv * wu - uv * wv) / denominator
    z = (uu * wv - uv * wu) / denominator
    values = [1.0 - y - z, y, z]
    _require(
        all(math.isfinite(value) and -1e-5 <= value <= 1.00001 for value in values),
        "BARYCENTRIC",
        "Closest point barycentric coordinates are outside tolerance",
    )
    values = [max(0.0, min(1.0, value)) for value in values]
    total = sum(values)
    _require(total > 0, "BARYCENTRIC", "Zero barycentric total")
    return [value / total for value in values]


def build_target_patch(body, rig, template, name, offset=0.0):
    """Build a different-topology owned probe patch from real body triangles.

    Each selected real body triangle produces one inset triangle using three
    barycentric interior points. The default is coincident with the real body
    rest surface so each probe vertex is strictly inside one source triangle
    (avoiding vertex/edge equidistance ambiguity) while remaining a genuine
    topology change derived only from the Snow body. It is a correspondence
    probe, not a clothing mesh.
    """
    _require(body.type == "MESH", "INVALID_TARGET", "Body source must be a mesh")
    source_topology = _mesh_topology(body.data)
    region_vertices = list(template["cross_region"]["vertex_indices"])
    region_triangle_indices = list(template["cross_region"]["triangle_indices"])
    _require(region_triangle_indices, "TARGET_REGION_EMPTY", "No source triangles in region")
    region_set = set(region_vertices)
    for triangle_index in region_triangle_indices:
        for vertex in template["source"]["triangles"][triangle_index]:
            _require(vertex in region_set, "REGION_CONFLICT", "Region triangle escapes vertex set")
    vertices = []
    faces = []
    barycentric_sets = (
        (0.60, 0.20, 0.20),
        (0.20, 0.60, 0.20),
        (0.20, 0.20, 0.60),
    )
    for triangle_index in region_triangle_indices:
        a, b, c = template["source"]["triangles"][triangle_index]
        points = [body.data.vertices[a].co, body.data.vertices[b].co, body.data.vertices[c].co]
        normals = [Vector(body.data.vertices[index].normal) for index in (a, b, c)]
        for normal in normals:
            if normal.length_squared <= 1e-20:
                normal[:] = (0.0, 0.0, 1.0)
            else:
                normal.normalize()
        triangle_base = len(vertices)
        for weights in barycentric_sets:
            point = sum(
                (points[index] * weights[index] for index in range(3)),
                Vector((0.0, 0.0, 0.0)),
            )
            normal = sum(
                (normals[index] * weights[index] for index in range(3)),
                Vector((0.0, 0.0, 0.0)),
            )
            if normal.length_squared <= 1e-20:
                normal = (points[1] - points[0]).cross(points[2] - points[0])
            if normal.length_squared <= 1e-20:
                raise TemplateError("DEGENERATE_TRIANGLE", "Derived patch source triangle is degenerate")
            normal.normalize()
            vertices.append(
                [float(value) for value in point + normal * float(offset)]
            )
        faces.append([triangle_base, triangle_base + 1, triangle_base + 2])
    mesh = bpy.data.meshes.new(name + "-Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    target = bpy.data.objects.new(name, mesh)
    _link_object(target)
    target.matrix_world = body.matrix_world.copy()
    for material in body.data.materials:
        mesh.materials.append(material)
    target["wt_source_body"] = body.name
    target["wt_source_body_topology_sha256"] = source_topology["sha256"]
    target["wt_derivation"] = "REAL_BODY_BARYCENTRIC_INSET_TRIANGLE_PATCH_V1"
    target["wt_normal_offset"] = float(offset)
    target["wt_claim"] = "TASK_OWNED_UNBOUND_PATCH_NOT_COMPLETE_CLOTHING"
    mesh.update()
    target_topology = _mesh_topology(mesh)
    target_rest = _mesh_rest(mesh, target_topology)
    _require(
        target_topology["sha256"] != source_topology["sha256"],
        "TOPOLOGY_CONFLICT",
        "Derived patch did not change topology",
    )
    return target, {
        "source_object": body.name,
        "source_object_unmodified": True,
        "derivation": "REAL_BODY_BARYCENTRIC_INSET_TRIANGLE_PATCH_V1",
        "normal_offset": float(offset),
        "source_topology_sha256": source_topology["sha256"],
        "source_vertex_count": len(body.data.vertices),
        "source_triangle_count": len(source_topology["triangles"]),
        "region_source_vertex_count": len(region_vertices),
        "region_source_triangle_count": len(region_triangle_indices),
        "source_triangle_indices": region_triangle_indices,
        "source_vertex_indices": region_vertices,
        "target_vertex_count": len(vertices),
        "target_triangle_count": len(faces),
        "target_topology_sha256": target_topology["sha256"],
        "target_rest_sha256": target_rest["sha256"],
        "target_object": target.name,
        "claim": "TASK_OWNED_UNBOUND_PATCH_NOT_COMPLETE_CLOTHING",
    }


def _target_group_mode_cross(template, target, mapping_groups):
    names = [group.name for group in target.vertex_groups]
    if not names:
        return "BLANK"
    _require(
        names == mapping_groups,
        "GROUP_CONFLICT",
        "Cross-topology target group set must be blank or exactly the mapped deform groups",
    )
    for group in target.vertex_groups:
        _require(not group.lock_weight, "LOCKED_GROUP", "Target group is locked: " + group.name)
    return "EXACT"


def apply_cross_topology(
    template,
    target,
    rig,
    *,
    target_source_report=None,
    fault_after_writes=None,
):
    validate_template(template)
    _require(target.type == "MESH", "INVALID_TARGET", "Target must be a mesh")
    _require(
        not target.library and not target.override_library,
        "UNSUPPORTED",
        "Target must be local and non-override",
    )
    _require(not target.modifiers, "UNSUPPORTED", "Cross target must be unbound and have no modifiers")
    _require(target.parent is None, "UNSUPPORTED", "Cross target must be unparented")
    _require(not target.data.shape_keys, "UNSUPPORTED", "Cross target must have no shape keys")
    _require(not target.constraints, "UNSUPPORTED", "Cross target must have no constraints")
    _require(target.data.users == 1, "CONFLICT", "Cross target mesh data must be single-user")
    _validate_rig(template, rig)
    mapping_groups = list(template["cross_region"]["used_deform_groups"])
    _require(
        len(mapping_groups) <= template["budgets"]["cross_topology_max_groups"],
        "GROUP_BUDGET",
        "Cross mapping group count exceeds the actual region budget",
    )
    group_mode = _target_group_mode_cross(template, target, mapping_groups)
    target_topology_before = _mesh_topology(target.data)['sha256']
    target_rest_before = _mesh_rest(target.data, _mesh_topology(target.data))['sha256']
    source = template["source"]
    source_matrix = Matrix(source["object_matrix_world"])
    source_world = [source_matrix @ Vector(vertex) for vertex in source["vertices"]]
    region_vertices = list(template["cross_region"]["vertex_indices"])
    region_triangles = [
        list(source["triangles"][index])
        for index in template["cross_region"]["triangle_indices"]
    ]
    region_points = [source_world[index] for index in region_vertices]
    remap = {old: new for new, old in enumerate(region_vertices)}
    bvh_points = [source_world[index] for index in region_vertices]
    bvh_triangles = [[remap[vertex] for vertex in triangle] for triangle in region_triangles]
    _require(
        all(
            (bvh_points[b] - bvh_points[a]).cross(bvh_points[c] - bvh_points[a]).length_squared > 1e-20
            for a, b, c in bvh_triangles
        ),
        "DEGENERATE_TRIANGLE",
        "Source region contains a degenerate triangle",
    )
    tree = BVHTree.FromPolygons(bvh_points, bvh_triangles, all_triangles=True)
    region_rows = {
        vertex: {
            item["group"]: float(item["weight"])
            for item in template["weights"]["vertices"][vertex]
            if item["deform"]
        }
        for vertex in region_vertices
    }
    region_sums = [template["weights"]["deform_sums"][vertex] for vertex in region_vertices]
    target_world = [target.matrix_world @ vertex.co for vertex in target.data.vertices]
    plans = []
    evidence = []
    max_distance = 0.0
    max_influences = 0
    max_sum_error = 0.0
    tolerance = float(template["cross_region"]["max_normalization_error"])
    distance_budget = float(template["cross_region"]["max_distance"])
    influence_budget = int(template["cross_region"]["max_target_influences"])
    for vertex_index, point in enumerate(target_world):
        near, normal, triangle, distance = tree.find_nearest(point)
        _require(triangle is not None, "BVH_MISS", "No source region surface found")
        distance = float(distance)
        _require(
            math.isfinite(distance) and distance <= distance_budget,
            "DISTANCE_BUDGET",
            "Cross rest-surface distance budget exceeded at target vertex " + str(vertex_index),
            {
                "vertex": vertex_index,
                "distance": distance,
                "budget": distance_budget,
                "writes": 0,
                "zero_write": True,
            },
        )
        max_distance = max(max_distance, distance)
        local_ids = bvh_triangles[triangle]
        ids = [region_vertices[index] for index in local_ids]
        basis = _barycentric(near, *(bvh_points[index] for index in local_ids))
        weights = {}
        for key, vertex in enumerate(ids):
            for group, weight in region_rows[vertex].items():
                weights[group] = weights.get(group, 0.0) + basis[key] * weight
        weights = {name: weight for name, weight in weights.items() if weight > 0}
        raw_sum = math.fsum(weights.values())
        predicted_sum = math.fsum(
            basis[key] * region_sums[local_ids[key]] for key in range(3)
        )
        sum_error = abs(raw_sum - predicted_sum)
        max_sum_error = max(max_sum_error, sum_error)
        _require(
            math.isfinite(raw_sum) and raw_sum > 0,
            "SUM_CONTRACT",
            "Cross interpolation produced a nonpositive sum",
        )
        _require(
            sum_error <= tolerance,
            "SUM_CONTRACT",
            "Cross source-sum preservation budget exceeded at target vertex "
            + str(vertex_index),
            {
                "vertex": vertex_index,
                "raw_sum": raw_sum,
                "predicted_sum": predicted_sum,
                "budget": tolerance,
                "writes": 0,
                "zero_write": True,
            },
        )
        _require(
            len(weights) <= influence_budget,
            "INFLUENCE_BUDGET",
            "Cross influence budget exceeded; no automatic pruning",
            {
                "vertex": vertex_index,
                "observed": len(weights),
                "budget": influence_budget,
                "writes": 0,
                "zero_write": True,
            },
        )
        max_influences = max(max_influences, len(weights))
        tie_epsilon = max(1e-7, abs(distance) * 1e-6)
        for alt, _, alt_triangle, alt_distance in tree.find_nearest_range(point, distance + tie_epsilon):
            if alt_triangle == triangle or abs(alt_distance - distance) > tie_epsilon:
                continue
            alt_ids = bvh_triangles[alt_triangle]
            alt_basis = _barycentric(alt, *(bvh_points[index] for index in alt_ids))
            alt_weights = {}
            for key, local in enumerate(alt_ids):
                for group, weight in region_rows[region_vertices[local]].items():
                    alt_weights[group] = alt_weights.get(group, 0.0) + alt_basis[key] * weight
            alt_weights = {name: weight for name, weight in alt_weights.items() if weight > 0}
            _require(
                all(abs(alt_weights.get(name, 0.0) - weights.get(name, 0.0)) <= max(tolerance, 1e-6) for name in mapping_groups),
                "AMBIGUOUS_SURFACE",
                "Equidistant disconnected source surfaces disagree at target vertex " + str(vertex_index),
                {
                    "vertex": vertex_index,
                    "distance": distance,
                    "source_triangle_index": int(template["cross_region"]["triangle_indices"][triangle]),
                    "alternative_triangle_index": int(template["cross_region"]["triangle_indices"][alt_triangle]),
                    "source_vertices": ids,
                    "alternative_source_vertices": [region_vertices[index] for index in alt_ids],
                    "weight_difference": {
                        name: abs(alt_weights.get(name, 0.0) - weights.get(name, 0.0))
                        for name in mapping_groups
                    },
                },
            )
        plans.append({name: f32(value) for name, value in weights.items()})
        evidence.append(
            {
                "vertex": vertex_index,
                "distance": distance,
                "source_triangle_index": int(template["cross_region"]["triangle_indices"][triangle]),
                "source_vertices": ids,
                "barycentric": basis,
                "weights": plans[-1],
                "influence_count": len(plans[-1]),
                "raw_sum": raw_sum,
                "predicted_sum": predicted_sum,
            }
        )
    _require(
        max_influences <= template["budgets"]["cross_topology_max_influences"],
        "INFLUENCE_BUDGET",
        "Cross budget differs from observed result",
        {"writes": 0, "zero_write": True},
    )
    before, before_sha = snapshot_weights(target)
    writes = 0
    mutated = False
    rollback = {"attempted": False, "ok": True, "errors": []}
    try:
        if group_mode == "BLANK":
            groups = []
            for name in mapping_groups:
                groups.append(target.vertex_groups.new(name=name))
                writes += 1
                mutated = True
                if fault_after_writes is not None and writes >= fault_after_writes:
                    raise InjectedFailure("Injected failure after cross group creation")
        else:
            groups = [target.vertex_groups[name] for name in mapping_groups]
        for group in groups:
            group.remove(list(range(len(target.data.vertices))))
            writes += 1
            mutated = True
            if fault_after_writes is not None and writes >= fault_after_writes:
                raise InjectedFailure("Injected failure while clearing cross weights")
        group_index = {name: index for index, name in enumerate(mapping_groups)}
        for vertex_index, weights in enumerate(plans):
            for name, weight in weights.items():
                groups[group_index[name]].add([vertex_index], float(weight), "REPLACE")
                writes += 1
                mutated = True
                if fault_after_writes is not None and writes >= fault_after_writes:
                    raise InjectedFailure("Injected failure after a cross weight write")
        target.data.update()
        bpy.context.view_layer.update()
        _require(
            _mesh_topology(target.data)["sha256"]
            == target_topology_before,
            "POSTCONDITION",
            "Cross target topology changed during apply",
        )
        _require(_mesh_rest(target.data, _mesh_topology(target.data))['sha256'] == target_rest_before,
                 'POSTCONDITION', 'Cross target rest changed during apply')
        names = {group.index: group.name for group in target.vertex_groups}
        for vertex_index, vertex in enumerate(target.data.vertices):
            observed = {
                names[membership.group]: f32(membership.weight)
                for membership in vertex.groups
            }
            if observed != plans[vertex_index]:
                raise TemplateError(
                    "WEIGHT_READBACK",
                    "Stored cross weights differ from the plan at vertex " + str(vertex_index),
                )
            observed_sum = math.fsum(observed.values())
            if abs(observed_sum - evidence[vertex_index]["raw_sum"]) > 1e-6:
                raise TemplateError(
                    "SUM_CONTRACT",
                    "Stored cross sum differs at vertex " + str(vertex_index),
                )
        after, after_sha = snapshot_weights(target)
        return {
            "status": "pass",
            "adapter": "SURFACE_BARYCENTRIC_V1_OFFLINE",
            "target": target.name,
            "rig": rig.name,
            "group_mode": group_mode,
            "mapping_groups": mapping_groups,
            "mapping_group_count": len(mapping_groups),
            "target_vertex_count": len(target.data.vertices),
            "target_triangle_count": len(target.data.polygons),
            "source_region": {
                "vertex_count": template["cross_region"]["vertex_count"],
                "triangle_count": template["cross_region"]["triangle_count"],
                "used_deform_groups": mapping_groups,
            },
            "source_region_geometry": {
                "vertex_indices": template["cross_region"]["vertex_indices"],
                "triangle_indices": template["cross_region"]["triangle_indices"],
            },
            "target_topology_sha256": _mesh_topology(target.data)["sha256"],
            "target_rest_sha256": _mesh_rest(target.data, _mesh_topology(target.data))["sha256"],
            "target_before": {'topology_sha256': target_topology_before, 'rest_sha256': target_rest_before},
            "max_distance": max_distance,
            "distance_budget": distance_budget,
            "max_influences": max_influences,
            "influence_budget": influence_budget,
            "max_sum_error": max_sum_error,
            "sum_tolerance": tolerance,
            "no_normalization": True,
            "no_pruning": True,
            "writes": writes,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "mapping": evidence,
            "rollback": rollback,
            "scope": (
                "Task-owned unbound shirt arm proxy; closest base-rest triangle "
                "interpolation; not a complete clothing binding or fit claim"
            ),
        }
    except Exception as error:
        if mutated:
            rollback["attempted"] = True
            try:
                _restore_weights(target, before)
                bpy.context.view_layer.update()
                restored, restored_sha = snapshot_weights(target)
                if restored != before:
                    raise TemplateError("ROLLBACK_READBACK", "Restored cross snapshot differs")
                rollback["ok"] = True
                rollback["restored_sha256"] = restored_sha
            except Exception as restore_error:
                rollback["ok"] = False
                rollback["errors"].append(
                    {"type": type(restore_error).__name__, "message": str(restore_error)}
                )
                raise TemplateError(
                    "ROLLBACK_FAILED",
                    "Cross-topology write failed and rollback did not restore the snapshot",
                    {
                        "cause": type(error).__name__ + ": " + str(error),
                        "rollback": rollback,
                        "writes": writes,
                    },
                ) from error
        if isinstance(error, TemplateError):
            error.details = {
                **error.details,
                "writes": writes,
                "zero_write": writes == 0,
                "rollback": rollback,
            }
            raise
        raise TemplateError(
            "NATIVE_ERROR",
            type(error).__name__ + ": " + str(error),
            {
                "writes": writes,
                "zero_write": writes == 0,
                "rollback": rollback,
                "cause": type(error).__name__,
            },
        ) from error


def create_proxy_rig(template, name, missing_bone=None):
    data = bpy.data.armatures.new(name + "-Data")
    rig = bpy.data.objects.new(name, data)
    _link_object(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for item in template["rig"]["bones"]:
            if item["name"] == missing_bone:
                continue
            bone = data.edit_bones.new(item["name"])
            bone.head = Vector(item["head_local"])
            bone.tail = Vector(item["tail_local"])
            bone.use_deform = bool(item["use_deform"])
        for item in template["rig"]["bones"]:
            if item["name"] == missing_bone or item["parent"] is None:
                continue
            if item["parent"] in data.edit_bones:
                data.edit_bones[item["name"]].parent = data.edit_bones[item["parent"]]
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()
    return rig


def _lbs_probe_state(rig):
    return {
        'pose_position': rig.data.pose_position,
        'pose': {p.name: {'rotation_mode': p.rotation_mode, **{
            field: list(getattr(p, field)) for field in
            ('location', 'rotation_quaternion', 'rotation_euler', 'rotation_axis_angle', 'scale')}}
            for p in rig.pose.bones},
        'context': {'mode': bpy.context.mode, 'scene': bpy.context.scene.name,
                    'view_layer': bpy.context.view_layer.name,
                    'frame': bpy.context.scene.frame_current_final,
                    'active': bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
                    'selected': sorted(o.name for o in bpy.context.selected_objects),
                    'active_bone': rig.data.bones.active.name if rig.data.bones.active else None},
    }


def evaluated_lbs_check(
    target,
    rig,
    source_report,
    pose_bone="UpperArm.L",
    angle=0.30,
    tolerance=2e-5,
    deformation_model="SINGLE_MATRIX_LBS",
    fault=None,
):
    _require(not target.modifiers, "BIND_STAGE", "LBS check requires the unbound candidate first")
    _require(fault in (None, 'after_modifier', 'after_pose'), 'FAULT_SCHEMA', 'Unknown owned probe fault')
    pose = rig.pose.bones.get(pose_bone)
    _require(pose is not None, "MISSING_BONE", "Pose witness bone missing: " + pose_bone)
    original = _lbs_probe_state(rig)
    modifier = evaluated = evaluated_mesh = None
    to_mesh_attempted = False
    response = None
    try:
        modifier = target.modifiers.new("WT-LBS-Oracle", "ARMATURE")
        if fault == 'after_modifier': raise InjectedFailure('Owned fault after modifier creation')
        modifier.object = rig
        modifier.use_vertex_groups = True
        modifier.use_bone_envelopes = False
        modifier.use_deform_preserve_volume = False
        rig.data.pose_position = "POSE"
        pose.rotation_mode = "QUATERNION"
        pose.rotation_quaternion = Quaternion((0.0, 0.0, 1.0), float(angle))
        if fault == 'after_pose': raise InjectedFailure('Owned fault after raw pose assignment')
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        depsgraph.update()
        evaluated = target.evaluated_get(depsgraph)
        to_mesh_attempted = True
        evaluated_mesh = evaluated.to_mesh()
        _require(
            len(evaluated_mesh.vertices) == len(target.data.vertices),
            "EVAL_COUNTS",
            "Evaluated vertex count differs",
        )
        names = {group.index: group.name for group in target.vertex_groups}
        observed = []
        for vertex in target.data.vertices:
            observed.append(
                {
                    names[membership.group]: float(membership.weight)
                    for membership in vertex.groups
                }
            )
        deform = {bone.name for bone in rig.data.bones if bone.use_deform}
        skin_matrices = {}
        for name in deform:
            bone = rig.data.bones.get(name)
            pose_bone_item = rig.pose.bones.get(name)
            if bone is None or pose_bone_item is None:
                continue
            skin_matrices[name] = (
                rig.matrix_world
                @ pose_bone_item.matrix
                @ bone.matrix_local.inverted()
                @ rig.matrix_world.inverted()
                @ target.matrix_world
            )
        errors = []
        displacements = []
        for vertex_index, vertex in enumerate(target.data.vertices):
            row = observed[vertex_index]
            filtered = {
                name: weight for name, weight in row.items() if name in deform and weight > 0
            }
            total = math.fsum(filtered.values())
            predicted = Vector((0.0, 0.0, 0.0))
            if total > 0:
                for name, weight in filtered.items():
                    predicted += skin_matrices[name] @ vertex.co * (weight / total)
            actual = evaluated.matrix_world @ evaluated_mesh.vertices[vertex_index].co
            errors.append((actual - predicted).length)
            displacements.append((actual - (target.matrix_world @ vertex.co)).length)
        _require(
            errors and max(errors) <= tolerance,
            "LBS_ORACLE",
            "Evaluated mesh differs from normalized LBS oracle",
            {
                "max_error": max(errors, default=0.0),
                "tolerance": tolerance,
                "deformation_model": deformation_model,
            },
        )
        _require(
            max(displacements, default=0.0) > 1e-5,
            "NO_DEFORMATION",
            "Pose produced no measurable vertex displacement",
            {"max_displacement": max(displacements, default=0.0)},
        )
        if deformation_model == "SIMPLE_REST_BONE_LBS":
            scope = (
                "Task-owned unbound proxy plus a temporary Armature modifier on a "
                "simple rest-bone reference rig; exact normalized LBS comparison, "
                "not a full clothing fit/bind claim."
            )
        else:
            scope = (
                "Task-owned unbound proxy plus a temporary Armature modifier on the "
                "Snow rig; static pose injection with drivers/constraints retained. "
                "Single-matrix LBS is a bounded approximation to multi-segment "
                "B-Bone deformation; not a control-response or full-clothing claim."
            )
        model_match = deformation_model != 'SNOW_RIG_BBONE_RESIDUAL_BOUND'
        response = {
            "status": "pass" if model_match else "observed_model_mismatch",
            "model_match": model_match,
            "witness_kind": "model_matched_deformation" if model_match else "bounded_motion_witness",
            "pose_bone": pose_bone,
            "angle_radians": float(angle),
            "compared_vertices": len(errors),
            "max_lbs_error": max(errors),
            "max_displacement": max(displacements),
            "mean_displacement": math.fsum(displacements) / len(displacements),
            "tolerance": tolerance,
            "deformation_model": deformation_model,
            "contributing_bbone_segments": {
                name: int(rig.data.bones[name].bbone_segments)
                for name in sorted(
                    {
                        name
                        for row in observed
                        for name in row
                        if name in deform and name in rig.data.bones
                    }
                )
                if rig.data.bones[name].bbone_segments > 1
            },
            "normalized_for_blender_armature": True,
            "stored_sum_policy_unchanged": True,
            "scope": scope,
        }
        return response
    finally:
        original_error = sys.exc_info()[1]
        cleanup_errors = []
        def attempt(label, callback):
            try: callback()
            except Exception as error: cleanup_errors.append({'stage': label, 'type': type(error).__name__, 'message': str(error)})
        if to_mesh_attempted and evaluated is not None:
            attempt('to_mesh_clear', evaluated.to_mesh_clear)
        if modifier is not None:
            attempt('modifier_remove', lambda: target.modifiers.remove(modifier))
        attempt('pose_position', lambda: setattr(rig.data, 'pose_position', original['pose_position']))
        for name, fields in original['pose'].items():
            def restore_pose(name=name, fields=fields):
                item = rig.pose.bones[name]
                if item.rotation_mode != fields['rotation_mode']: item.rotation_mode = fields['rotation_mode']
                for field, values in fields.items():
                    if field == 'rotation_mode': continue
                    if [f32_hex(x) for x in getattr(item,field)] != [f32_hex(x) for x in values]:
                        setattr(item, field, values)
            attempt('pose:' + name, restore_pose)
        context = original['context']
        def restore_context():
            selected = set(context['selected'])
            for obj in bpy.context.view_layer.objects:
                desired = obj.name in selected
                if obj.select_get() != desired: obj.select_set(desired)
            bpy.context.view_layer.objects.active = bpy.data.objects.get(context['active']) if context['active'] else None
            rig.data.bones.active = rig.data.bones.get(context['active_bone']) if context['active_bone'] else None
        attempt('context', restore_context)
        attempt('dependency_update', bpy.context.view_layer.update)
        actual = None
        try: actual = _lbs_probe_state(rig)
        except Exception as error: cleanup_errors.append({'stage':'readback','message':str(error)})
        exact = actual is not None and sha256_value(actual) == sha256_value(original)
        if response is not None:
            response['cleanup'] = {'all_raw_pose_context_exact': exact, 'temporary_modifier_removed': modifier is None or modifier not in list(target.modifiers),
                                   'errors': cleanup_errors}
        _require(not cleanup_errors and exact, 'LBS_CLEANUP_FAILED', 'Owned LBS probe did not restore all raw pose/context',
                 {'original_error': None if original_error is None else str(original_error), 'cleanup_errors': cleanup_errors, 'raw_pose_context_exact':exact})


def brute_force_mapping_oracle(template, target):
    """Independent closest-point oracle using the pure Ericson implementation."""
    from .core import closest_point_on_triangle, distance_squared

    source = template["source"]
    matrix = Matrix(source["object_matrix_world"])
    source_world = [matrix @ Vector(vertex) for vertex in source["vertices"]]
    region_vertices = list(template["cross_region"]["vertex_indices"])
    region_triangles = [
        list(source["triangles"][index])
        for index in template["cross_region"]["triangle_indices"]
    ]
    region_points = [source_world[index] for index in region_vertices]
    remap = {vertex: index for index, vertex in enumerate(region_vertices)}
    target_world = [target.matrix_world @ vertex.co for vertex in target.data.vertices]
    mapping_groups = list(template["cross_region"]["used_deform_groups"])
    region_rows = {
        vertex: {
            item["group"]: float(item["weight"])
            for item in template["weights"]["vertices"][vertex]
            if item["deform"]
        }
        for vertex in region_vertices
    }
    evidence = []
    for vertex_index, point in enumerate(target_world):
        best = None
        best_distance = math.inf
        for triangle_index, triangle in enumerate(region_triangles):
            a = region_points[remap[triangle[0]]]
            b = region_points[remap[triangle[1]]]
            c = region_points[remap[triangle[2]]]
            closest, basis = closest_point_on_triangle(point, a, b, c)
            distance = math.sqrt(distance_squared(point, closest))
            if distance < best_distance:
                best_distance = distance
                best = (triangle_index, triangle, basis)
        _require(best is not None, "BVH_MISS", "Brute-force oracle found no triangle")
        triangle_index, triangle, basis = best
        weights = {}
        for key, vertex in enumerate(triangle):
            for group, weight in region_rows[vertex].items():
                weights[group] = weights.get(group, 0.0) + basis[key] * weight
        weights = {name: float(weight) for name, weight in weights.items() if weight > 0}
        evidence.append(
            {
                "vertex": vertex_index,
                "distance": best_distance,
                "source_triangle_index": int(template["cross_region"]["triangle_indices"][triangle_index]),
                "source_vertices": triangle,
                "barycentric": list(basis),
                "weights": weights,
                "raw_sum": math.fsum(weights.values()),
                "influence_count": len(weights),
            }
        )
    return {
        "mapping": evidence,
        "max_distance": max(item["distance"] for item in evidence),
        "max_influences": max(item["influence_count"] for item in evidence),
    }
