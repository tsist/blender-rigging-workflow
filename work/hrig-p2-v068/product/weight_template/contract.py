"""Strict JSON data contract for the owned weight template.

The template contains data only. No Python source, expressions or executable
strings are accepted by this schema.
"""
from __future__ import annotations

import math
import json
import re

from .core import canonical_bytes, rest_sha256, sha256_value, sum_rows, topology_sha256, vertices_sha256

SCHEMA = "hrig.weight-template.v1"
MODE = "SAME_AND_CROSS_TOPOLOGY_V1"
SHA = re.compile(r"[0-9a-f]{64}\Z")


class TemplateError(ValueError):
    def __init__(self, code, message, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


def decode_json(raw):
    """Unambiguous JSON boundary shared by host and native readers."""
    def pairs(items):
        answer = {}
        for key, value in items:
            if key in answer:
                raise TemplateError('DUPLICATE_JSON_KEY', 'Duplicate JSON key', {'key': key})
            answer[key] = value
        return answer
    def constant(token):
        raise TemplateError('NONFINITE_JSON', 'Nonfinite JSON number', {'token': token})
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as error:
        raise TemplateError('INVALID_JSON', str(error)) from error
    def walk(node):
        if type(node) is float and not math.isfinite(node):
            raise TemplateError('NONFINITE_JSON', 'JSON number overflows finite range')
        if type(node) is dict:
            for item in node.values(): walk(item)
        elif type(node) is list:
            for item in node: walk(item)
    walk(value)
    return value


def _fail(code, message, details=None):
    raise TemplateError(code, message, details)


def _require(condition, code, message, details=None):
    if not condition:
        _fail(code, message, details)


def _exact(value, keys, label):
    _require(type(value) is dict, "SCHEMA_TYPE", label + " must be an object")
    actual = set(value)
    expected = set(keys)
    _require(
        actual == expected,
        "SCHEMA_FIELDS",
        label + " fields differ",
        {"missing": sorted(expected - actual), "extra": sorted(actual - expected)},
    )


def _string(value, label, maximum=4096):
    _require(
        type(value) is str and 0 < len(value) <= maximum and "\x00" not in value,
        "SCHEMA_STRING",
        label + " must be a nonempty bounded string",
    )
    return value


def _number(value, label, low=None, high=None):
    _require(
        type(value) in (int, float) and math.isfinite(float(value)),
        "SCHEMA_NUMBER",
        label + " must be finite",
    )
    result = float(value)
    if low is not None:
        _require(result >= low, "SCHEMA_RANGE", label + " below minimum")
    if high is not None:
        _require(result <= high, "SCHEMA_RANGE", label + " above maximum")
    return result


def _integer(value, label, low, high):
    _require(
        type(value) is int and low <= value <= high,
        "SCHEMA_INTEGER",
        label + " must be an integer in [" + str(low) + "," + str(high) + "]",
    )
    return value


def _digest(value, label):
    _require(
        type(value) is str and SHA.fullmatch(value) is not None,
        "SCHEMA_SHA",
        label + " must be lowercase SHA-256",
    )
    return value


def _matrix(value, label):
    _require(
        type(value) is list and len(value) == 4,
        "SCHEMA_MATRIX",
        label + " must be a 4x4 matrix",
    )
    for row_index, row in enumerate(value):
        _require(
            type(row) is list and len(row) == 4,
            "SCHEMA_MATRIX",
            label + " row " + str(row_index) + " must have four values",
        )
        for column_index, item in enumerate(row):
            _number(item, label + "[" + str(row_index) + "][" + str(column_index) + "]")


def _vector(value, label, size):
    _require(
        type(value) is list and len(value) == size,
        "SCHEMA_VECTOR",
        label + " must have " + str(size) + " values",
    )
    return [_number(item, label + "[" + str(index) + "]") for index, item in enumerate(value)]


def template_payload_sha256(template):
    payload = {key: value for key, value in template.items() if key != "payload_sha256"}
    return sha256_value(payload)


def seal_template(template):
    value = dict(template)
    value.pop("payload_sha256", None)
    value["payload_sha256"] = template_payload_sha256(value)
    validate_template(value)
    return value


def validate_template(template):
    _exact(
        template,
        {
            "schema",
            "template_id",
            "revision",
            "mode",
            "provenance",
            "license",
            "source",
            "rig",
            "group_map",
            "weights",
            "cross_region",
            "budgets",
            "application_rules",
            "payload_sha256",
        },
        "template",
    )
    _require(template["schema"] == SCHEMA, "SCHEMA_VERSION", "Unexpected template schema")
    _require(template["mode"] == MODE, "SCHEMA_MODE", "Unexpected template mode")
    _string(template["template_id"], "template_id", 256)
    _integer(template["revision"], "revision", 1, 1000000)
    _digest(template["payload_sha256"], "payload_sha256")
    _require(
        template_payload_sha256(template) == template["payload_sha256"],
        "PAYLOAD_SHA",
        "Template payload SHA-256 does not match canonical data",
    )
    _validate_provenance(template["provenance"])
    _validate_license(template["license"])
    source = _validate_source(template["source"])
    rig = _validate_rig(template["rig"], source)
    group_map = _validate_group_map(template["group_map"], rig)
    weights = _validate_weights(template["weights"], source["topology"]["vertex_count"], group_map)
    _validate_cross_region(template["cross_region"], source, group_map, weights)
    _validate_budgets(template["budgets"], weights, group_map, template["cross_region"])
    _validate_rules(template["application_rules"])
    return template


def _validate_provenance(value):
    _exact(
        value,
        {
            "source_file",
            "source_file_sha256",
            "source_object",
            "source_mesh",
            "source_content_sha256",
            "blender_version",
            "blender_build",
            "generated_by",
            "created_utc",
            "public_transfer_compatibility",
        },
        "provenance",
    )
    _string(value["source_file"], "provenance.source_file")
    _digest(value["source_file_sha256"], "provenance.source_file_sha256")
    _string(value["source_object"], "provenance.source_object", 256)
    _string(value["source_mesh"], "provenance.source_mesh", 256)
    _digest(value["source_content_sha256"], "provenance.source_content_sha256")
    _require(
        type(value["blender_version"]) is list
        and len(value["blender_version"]) == 3
        and all(type(item) is int and 0 <= item < 100 for item in value["blender_version"]),
        "SCHEMA_VERSION_VALUE",
        "provenance.blender_version must contain three integers",
    )
    _string(value["blender_build"], "provenance.blender_build", 256)
    _string(value["generated_by"], "provenance.generated_by", 512)
    _string(value["created_utc"], "provenance.created_utc", 64)
    compatibility = value["public_transfer_compatibility"]
    _exact(
        compatibility,
        {"adapter", "applicable", "reasons", "observed"},
        "provenance.public_transfer_compatibility",
    )
    _require(
        compatibility["adapter"] == "SURFACE_BARYCENTRIC_V1",
        "SCHEMA_PUBLIC_ADAPTER",
        "Unexpected public adapter",
    )
    _require(type(compatibility["applicable"]) is bool, "SCHEMA_BOOL", "applicable must be bool")
    _require(
        type(compatibility["reasons"]) is list
        and 1 <= len(compatibility["reasons"]) <= 64
        and all(type(item) is str and 0 < len(item) <= 1024 for item in compatibility["reasons"]),
        "SCHEMA_REASONS",
        "public transfer reasons must be bounded strings",
    )
    observed = compatibility["observed"]
    _exact(
        observed,
        {
            "source_shape_keys",
            "source_non_armature_modifiers",
            "source_unweighted_deform_vertices",
            "source_nonunit_deform_vertices",
            "target_modifiers",
            "target_shape_keys",
        },
        "provenance.public_transfer_compatibility.observed",
    )
    for key, item in observed.items():
        _integer(item, "observed." + key, 0, 100000000)


def _validate_license(value):
    _exact(
        value,
        {"asset", "url", "attribution", "embedded_script", "distribution"},
        "license",
    )
    for key in ("asset", "url", "attribution", "embedded_script", "distribution"):
        _string(value[key], "license." + key, 4096)


def _validate_source(value):
    _exact(
        value,
        {
            "object",
            "mesh",
            "coordinate_space",
            "unit_scale",
            "object_matrix_world",
            "topology",
            "rest",
            "index",
            "vertices",
            "triangles",
        },
        "source",
    )
    _string(value["object"], "source.object", 256)
    _string(value["mesh"], "source.mesh", 256)
    _require(
        value["coordinate_space"] == "OBJECT_LOCAL_BASE_REST",
        "SCHEMA_SPACE",
        "Source coordinates must be object-local base rest",
    )
    _number(value["unit_scale"], "source.unit_scale", 1e-12, 1e12)
    _matrix(value["object_matrix_world"], "source.object_matrix_world")
    topology = value["topology"]
    _exact(
        topology,
        {"vertex_count", "edge_count", "polygon_count", "loop_count", "triangle_count", "sha256"},
        "source.topology",
    )
    vertex_count = _integer(topology["vertex_count"], "source.topology.vertex_count", 1, 200000)
    edge_count = _integer(topology["edge_count"], "source.topology.edge_count", 1, 1000000)
    polygon_count = _integer(topology["polygon_count"], "source.topology.polygon_count", 1, 1000000)
    _integer(topology["loop_count"], "source.topology.loop_count", 1, 1000000)
    triangle_count = _integer(
        topology["triangle_count"], "source.topology.triangle_count", 1, 2000000
    )
    _digest(topology["sha256"], "source.topology.sha256")
    rest = value["rest"]
    _exact(rest, {"vertex_count", "vertices_sha256", "sha256"}, "source.rest")
    _require(
        rest["vertex_count"] == vertex_count,
        "SCHEMA_COUNTS",
        "Source rest vertex count differs",
    )
    _digest(rest["vertices_sha256"], "source.rest.vertices_sha256")
    _digest(rest["sha256"], "source.rest.sha256")
    index = value["index"]
    _require(
        type(index) is list and len(index) == vertex_count,
        "SCHEMA_INDEX",
        "source.index length must equal vertex count",
    )
    for position, item in enumerate(index):
        _require(item == position, "SCHEMA_INDEX", "source.index must be contiguous")
    vertices = value["vertices"]
    _require(
        type(vertices) is list and len(vertices) == vertex_count,
        "SCHEMA_VERTICES",
        "source.vertices length must equal vertex count",
    )
    for position, vertex in enumerate(vertices):
        _vector(vertex, "source.vertices[" + str(position) + "]", 3)
    triangles = value["triangles"]
    _require(
        type(triangles) is list and len(triangles) == triangle_count,
        "SCHEMA_TRIANGLES",
        "source.triangles length must equal triangle count",
    )
    for position, triangle in enumerate(triangles):
        _require(
            type(triangle) is list and len(triangle) == 3,
            "SCHEMA_TRIANGLES",
            "triangle must contain three indices",
        )
        for item in triangle:
            _integer(item, "source.triangles[" + str(position) + "]", 0, vertex_count - 1)
        _require(
            len(set(triangle)) == 3,
            "SCHEMA_TRIANGLES",
            "source triangle must use three distinct vertices",
        )
    _require(
        vertices_sha256(vertices) == rest["vertices_sha256"],
        "REST_SHA",
        "Source vertices SHA-256 mismatch",
    )
    _require(
        rest_sha256(vertex_count, vertices, topology["sha256"]) == rest["sha256"],
        "REST_SHA",
        "Source rest SHA-256 mismatch",
    )
    return value


def _validate_rig(value, source):
    _exact(
        value,
        {
            "object",
            "data",
            "bone_count",
            "deform_bone_count",
            "rest_sha256",
            "relevant_rest_sha256",
            "bones",
        },
        "rig",
    )
    _string(value["object"], "rig.object", 256)
    _string(value["data"], "rig.data", 256)
    _integer(value["bone_count"], "rig.bone_count", 1, 4096)
    _integer(value["deform_bone_count"], "rig.deform_bone_count", 1, 4096)
    _digest(value["rest_sha256"], "rig.rest_sha256")
    _digest(value["relevant_rest_sha256"], "rig.relevant_rest_sha256")
    _require(
        type(value["bones"]) is list and 1 <= len(value["bones"]) <= 4096,
        "SCHEMA_BONES",
        "rig.bones must be a bounded list",
    )
    seen = set()
    for position, bone in enumerate(value["bones"]):
        _exact(
            bone,
            {"name", "parent", "use_deform", "head_local", "tail_local", "matrix_local", "length"},
            "rig.bones[" + str(position) + "]",
        )
        name = _string(bone["name"], "bone.name", 256)
        _require(name not in seen, "SCHEMA_BONES", "Duplicate relevant bone")
        seen.add(name)
        if bone["parent"] is not None:
            _string(bone["parent"], "bone.parent", 256)
        _require(type(bone["use_deform"]) is bool, "SCHEMA_BOOL", "bone.use_deform must be bool")
        _vector(bone["head_local"], "bone.head_local", 3)
        _vector(bone["tail_local"], "bone.tail_local", 3)
        _matrix(bone["matrix_local"], "bone.matrix_local")
        _number(bone["length"], "bone.length", 1e-12, 1e9)
        _require(
            sha256_value(bone) == sha256_value(
                {
                    "name": bone["name"],
                    "parent": bone["parent"],
                    "use_deform": bone["use_deform"],
                    "head_local": bone["head_local"],
                    "tail_local": bone["tail_local"],
                    "matrix_local": bone["matrix_local"],
                    "length": bone["length"],
                }
            ),
            "SCHEMA_BONES",
            "Relevant bone structure is not canonical",
        )
    _require(
        sha256_value(value["bones"]) == value["relevant_rest_sha256"],
        "REST_SHA",
        "Relevant rig rest SHA-256 mismatch",
    )
    return value


def _validate_group_map(value, rig):
    _require(
        type(value) is list and 1 <= len(value) <= 8192,
        "SCHEMA_GROUPS",
        "group_map must be a bounded list",
    )
    bones = {bone["name"]: bone for bone in rig["bones"]}
    names = set()
    for position, group in enumerate(value):
        _exact(
            group,
            {
                "index",
                "name",
                "bone",
                "deform",
                "lock",
                "member_vertices",
                "positive_vertices",
                "total_positive_weight",
                "classification",
            },
            "group_map[" + str(position) + "]",
        )
        _require(group["index"] == position, "SCHEMA_GROUPS", "Group indices must be contiguous")
        name = _string(group["name"], "group.name", 256)
        _require(name not in names, "SCHEMA_GROUPS", "Duplicate group name")
        names.add(name)
        if group["bone"] is not None:
            _string(group["bone"], "group.bone", 256)
            _require(group["bone"] in bones, "MISSING_BONE", "Mapped bone is not in relevant rig rest")
        _require(type(group["deform"]) is bool, "SCHEMA_BOOL", "group.deform must be bool")
        _require(type(group["lock"]) is bool, "SCHEMA_BOOL", "group.lock must be bool")
        _integer(group["member_vertices"], "group.member_vertices", 0, 200000)
        _integer(group["positive_vertices"], "group.positive_vertices", 0, 200000)
        _number(group["total_positive_weight"], "group.total_positive_weight", 0.0, 1e12)
        _require(
            group["classification"] in ("DEFORM", "MASK", "CORRECTIVE_OR_HELPER", "UNUSED"),
            "SCHEMA_GROUPS",
            "Unexpected group classification",
        )
        if group["deform"]:
            _require(group["bone"] == name, "SCHEMA_GROUPS", "Deform group must map to a same-name bone")
        else:
            _require(group["bone"] is None, "SCHEMA_GROUPS", "Non-deform group cannot claim a bone")
    return value


def _validate_weights(value, vertex_count, group_map):
    _exact(
        value,
        {
            "sum_policy",
            "normalization",
            "vertices",
            "all_sums",
            "deform_sums",
            "stats",
        },
        "weights",
    )
    _require(
        value["sum_policy"] == "PRESERVE_SOURCE_DEFORM_SUM",
        "SUM_POLICY",
        "Template must preserve the source deform sum",
    )
    _require(
        value["normalization"] == "NONE",
        "SUM_POLICY",
        "Template must not normalize weights",
    )
    rows = value["vertices"]
    _require(
        type(rows) is list and len(rows) == vertex_count,
        "SCHEMA_WEIGHTS",
        "weights.vertices length must equal vertex count",
    )
    group_names = [group["name"] for group in group_map]
    for vertex_index, row in enumerate(rows):
        _require(type(row) is list, "SCHEMA_WEIGHTS", "Weight row must be a list")
        seen = set()
        previous = -1
        for membership in row:
            _exact(
                membership,
                {"group_index", "group", "bone", "weight", "deform"},
                "weights.vertices[" + str(vertex_index) + "]",
            )
            group_index = _integer(
                membership["group_index"],
                "membership.group_index",
                0,
                len(group_map) - 1,
            )
            _require(
                group_index > previous,
                "SCHEMA_WEIGHTS",
                "Memberships must be sorted by unique group index",
            )
            previous = group_index
            _require(group_index not in seen, "SCHEMA_WEIGHTS", "Duplicate group membership")
            seen.add(group_index)
            group = group_map[group_index]
            _require(
                membership["group"] == group_names[group_index],
                "SCHEMA_WEIGHTS",
                "Membership group name/index mismatch",
            )
            _require(
                membership["bone"] == group["bone"],
                "SCHEMA_WEIGHTS",
                "Membership bone mapping mismatch",
            )
            _require(
                membership["deform"] == group["deform"],
                "SCHEMA_WEIGHTS",
                "Membership deform flag mismatch",
            )
            _number(membership["weight"], "membership.weight", 0.0, 1.0)
            _require(
                membership["weight"] > 0.0,
                "SCHEMA_WEIGHTS",
                "Sparse memberships must be positive",
            )
    all_sums = value["all_sums"]
    deform_sums = value["deform_sums"]
    for label, sums in (("all_sums", all_sums), ("deform_sums", deform_sums)):
        _require(
            type(sums) is list and len(sums) == vertex_count,
            "SCHEMA_SUMS",
            label + " length must equal vertex count",
        )
        for position, item in enumerate(sums):
            _number(item, label + "[" + str(position) + "]", 0.0, 1e9)
    expected_all = sum_rows(rows, deform=False)
    expected_deform = sum_rows(rows, deform=True)
    for position in range(vertex_count):
        _require(
            abs(expected_all[position] - all_sums[position]) <= 1e-9,
            "SUM_CONTRACT",
            "all_sums does not match memberships at " + str(position),
        )
        _require(
            abs(expected_deform[position] - deform_sums[position]) <= 1e-9,
            "SUM_CONTRACT",
            "deform_sums does not match memberships at " + str(position),
        )
    stats = value["stats"]
    _exact(
        stats,
        {
            "group_count",
            "used_group_count",
            "deform_group_count",
            "used_deform_group_count",
            "max_influences_all",
            "max_influences_deform",
            "total_memberships",
            "zero_memberships",
            "unweighted_deform_vertices",
            "nonunit_deform_vertices",
            "deform_sum_min",
            "deform_sum_max",
            "nonunit_tolerance",
        },
        "weights.stats",
    )
    counts_all = [len(row) for row in rows]
    counts_deform = [sum(item["deform"] for item in row) for row in rows]
    _require(
        stats["group_count"] == len(group_map),
        "SCHEMA_STATS",
        "group_count mismatch",
    )
    _require(
        stats["used_group_count"] == sum(group["positive_vertices"] > 0 for group in group_map),
        "SCHEMA_STATS",
        "used_group_count mismatch",
    )
    _require(
        stats["deform_group_count"] == sum(group["deform"] for group in group_map),
        "SCHEMA_STATS",
        "deform_group_count mismatch",
    )
    _require(
        stats["used_deform_group_count"]
        == sum(group["deform"] and group["positive_vertices"] > 0 for group in group_map),
        "SCHEMA_STATS",
        "used_deform_group_count mismatch",
    )
    _require(stats["max_influences_all"] == max(counts_all, default=0), "SCHEMA_STATS", "max all mismatch")
    _require(
        stats["max_influences_deform"] == max(counts_deform, default=0),
        "SCHEMA_STATS",
        "max deform mismatch",
    )
    _require(
        stats["total_memberships"] == sum(counts_all),
        "SCHEMA_STATS",
        "membership count mismatch",
    )
    _require(stats["zero_memberships"] == 0, "SCHEMA_STATS", "Sparse template must not store zeros")
    _require(
        stats["unweighted_deform_vertices"] == sum(item <= 1e-8 for item in deform_sums),
        "SCHEMA_STATS",
        "unweighted count mismatch",
    )
    tolerance = _number(stats["nonunit_tolerance"], "stats.nonunit_tolerance", 1e-12, 0.1)
    _require(
        stats["nonunit_deform_vertices"]
        == sum(abs(item - 1.0) > tolerance for item in deform_sums),
        "SCHEMA_STATS",
        "non-unit count mismatch",
    )
    _require(
        abs(stats["deform_sum_min"] - min(deform_sums)) <= 1e-9,
        "SCHEMA_STATS",
        "deform_sum_min mismatch",
    )
    _require(
        abs(stats["deform_sum_max"] - max(deform_sums)) <= 1e-9,
        "SCHEMA_STATS",
        "deform_sum_max mismatch",
    )
    return value


def _validate_cross_region(value, source, group_map, weights):
    _exact(
        value,
        {
            "name",
            "definition",
            "segment_bones",
            "radius",
            "vertex_indices",
            "triangle_indices",
            "vertex_count",
            "triangle_count",
            "used_deform_groups",
            "max_source_influences",
            "max_target_influences",
            "max_distance",
            "max_normalization_error",
        },
        "cross_region",
    )
    _string(value["name"], "cross_region.name", 256)
    _string(value["definition"], "cross_region.definition", 2048)
    _require(
        type(value["segment_bones"]) is list
        and 1 <= len(value["segment_bones"]) <= 32
        and all(type(item) is str and 0 < len(item) <= 256 for item in value["segment_bones"]),
        "SCHEMA_REGION",
        "segment_bones must be bounded strings",
    )
    _number(value["radius"], "cross_region.radius", 1e-8, 1000.0)
    vertex_indices = value["vertex_indices"]
    triangle_indices = value["triangle_indices"]
    _require(
        type(vertex_indices) is list
        and 1 <= len(vertex_indices) <= source["topology"]["vertex_count"]
        and vertex_indices == sorted(set(vertex_indices))
        and all(type(item) is int and 0 <= item < source["topology"]["vertex_count"] for item in vertex_indices),
        "SCHEMA_REGION",
        "vertex_indices must be sorted unique source indices",
    )
    _require(
        type(triangle_indices) is list
        and 1 <= len(triangle_indices) <= source["topology"]["triangle_count"]
        and triangle_indices == sorted(set(triangle_indices))
        and all(type(item) is int and 0 <= item < source["topology"]["triangle_count"] for item in triangle_indices),
        "SCHEMA_REGION",
        "triangle_indices must be sorted unique source triangle indices",
    )
    _require(
        value["vertex_count"] == len(vertex_indices),
        "SCHEMA_REGION",
        "region vertex_count mismatch",
    )
    _require(
        value["triangle_count"] == len(triangle_indices),
        "SCHEMA_REGION",
        "region triangle_count mismatch",
    )
    used = value["used_deform_groups"]
    _require(
        type(used) is list
        and used == sorted(set(used))
        and all(isinstance(item, str) for item in used),
        "SCHEMA_REGION",
        "used_deform_groups must be sorted unique strings",
    )
    deform_names = {group["name"] for group in group_map if group["deform"]}
    _require(set(used) <= deform_names, "SCHEMA_REGION", "Region references non-deform group")
    actual_used = sorted(
        {
            item["group"]
            for vertex_index in vertex_indices
            for item in weights["vertices"][vertex_index]
            if item["deform"]
        }
    )
    _require(
        used == actual_used,
        "SCHEMA_REGION",
        "used_deform_groups must exactly equal groups used by region vertices",
    )
    source_max = _integer(
        value["max_source_influences"], "cross_region.max_source_influences", 1, 32
    )
    target_max = _integer(
        value["max_target_influences"], "cross_region.max_target_influences", 1, 32
    )
    _require(
        target_max >= source_max,
        "SCHEMA_REGION",
        "Cross target influence budget cannot be below source budget",
    )
    _number(value["max_distance"], "cross_region.max_distance", 1e-8, 1000.0)
    _number(
        value["max_normalization_error"],
        "cross_region.max_normalization_error",
        1e-8,
        0.001,
    )
    _require(
        source_max == weights["stats"]["max_influences_deform"],
        "SCHEMA_REGION",
        "Region source influence budget must equal observed source maximum",
    )
    return value


def _validate_budgets(value, weights, group_map, cross_region):
    _exact(
        value,
        {
            "same_topology_max_influences",
            "same_topology_max_groups",
            "cross_topology_max_influences",
            "cross_topology_max_groups",
            "cross_max_distance",
            "cross_sum_tolerance",
            "same_rest_tolerance",
        },
        "budgets",
    )
    same_max = _integer(value["same_topology_max_influences"], "budget.same_influences", 1, 32)
    same_groups = _integer(value["same_topology_max_groups"], "budget.same_groups", 1, 8192)
    cross_max = _integer(value["cross_topology_max_influences"], "budget.cross_influences", 1, 32)
    cross_groups = _integer(value["cross_topology_max_groups"], "budget.cross_groups", 1, 8192)
    _number(value["cross_max_distance"], "budget.cross_max_distance", 1e-8, 1000.0)
    _number(value["cross_sum_tolerance"], "budget.cross_sum_tolerance", 1e-9, 0.001)
    _number(value["same_rest_tolerance"], "budget.same_rest_tolerance", 0.0, 1e-6)
    _require(
        same_max == weights["stats"]["max_influences_deform"],
        "BUDGET_SOURCE",
        "Same-topology influence budget must equal observed source maximum",
    )
    _require(
        same_groups == len(group_map),
        "BUDGET_SOURCE",
        "Same-topology group budget must equal observed group count",
    )
    _require(
        cross_max == cross_region["max_target_influences"],
        "BUDGET_SOURCE",
        "Cross influence budget must equal the declared region budget",
    )
    _require(
        cross_groups == len(cross_region["used_deform_groups"]),
        "BUDGET_SOURCE",
        "Cross group budget must equal the actual region group count",
    )
    _require(
        abs(value["cross_max_distance"] - cross_region["max_distance"]) <= 1e-12,
        "BUDGET_SOURCE",
        "Cross distance budget differs from region declaration",
    )
    _require(
        abs(value["cross_sum_tolerance"] - cross_region["max_normalization_error"]) <= 1e-12,
        "BUDGET_SOURCE",
        "Cross sum tolerance differs from region declaration",
    )
    return value


def _validate_rules(value):
    _exact(
        value,
        {
            "same_topology",
            "cross_topology",
            "target_group_policy",
            "rollback",
            "no_silent_pruning",
            "no_silent_normalization",
            "bound_claim",
        },
        "application_rules",
    )
    _require(
        value["same_topology"] == "EXACT_INDEX_REST_AND_GROUP_MAP",
        "SCHEMA_RULES",
        "Unexpected same-topology rule",
    )
    _require(
        value["cross_topology"] == "SURFACE_BARYCENTRIC_V1_OFFLINE_PRESERVE_SOURCE_SUM",
        "SCHEMA_RULES",
        "Unexpected cross-topology rule",
    )
    _require(
        value["target_group_policy"] == "BLANK_OR_EXACT_REPLACE",
        "SCHEMA_RULES",
        "Unexpected target group policy",
    )
    _require(
        value["rollback"] == "FULL_WEIGHT_SNAPSHOT",
        "SCHEMA_RULES",
        "Unexpected rollback rule",
    )
    _require(value["no_silent_pruning"] is True, "SCHEMA_RULES", "Pruning must be disabled")
    _require(
        value["no_silent_normalization"] is True,
        "SCHEMA_RULES",
        "Normalization must be disabled",
    )
    _require(value["bound_claim"] == "NONE", "SCHEMA_RULES", "Template cannot claim binding")
    return value
