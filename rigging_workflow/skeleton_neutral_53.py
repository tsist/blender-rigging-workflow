"""Evaluate the original C94 neutral probe points directly against native pose bones."""

import math

import bpy
from mathutils import Vector


METHOD = "c94_original_probe_points_native_bbone_matrix_v1"
FRACTIONS = (0.05, 0.25, 0.50, 0.75, 0.95)


def evaluate(rig):
    rig.update_tag(refresh={"OBJECT", "DATA"})
    bpy.context.view_layer.update()
    return rig.evaluated_get(bpy.context.evaluated_depsgraph_get())


def point_samples(rig, evaluated, name):
    bone = rig.data.bones[name]
    pose = evaluated.pose.bones[name]
    rest_inverse = bone.matrix_local.inverted()
    count = bone.bbone_segments
    radius = min(0.002, bone.length * 0.1)
    parts = None
    if count > 1:
        parts = [
            pose.bbone_segment_matrix(index, rest=False)
            @ pose.bbone_segment_matrix(index, rest=True).inverted()
            for index in range(count + 1)
        ]

    samples = []
    for fraction in FRACTIONS:
        for x, z in ((0.0, 0.0), (radius, 0.0), (0.0, radius)):
            point = bone.matrix_local @ Vector((x, bone.length * fraction, z))
            if parts is None:
                changed = (pose.matrix @ rest_inverse) @ point
            else:
                index, factor = pose.bbone_segment_index(point)
                if not (0 <= index < count and math.isfinite(factor) and 0.0 <= factor <= 1.0):
                    raise ValueError("Invalid native B-Bone segment index or blend factor: " + name)
                segment = parts[index] * (1.0 - factor) + parts[index + 1] * factor
                changed = (pose.matrix @ segment @ rest_inverse) @ point
            if not all(math.isfinite(value) for value in changed):
                raise ValueError("Non-finite native neutral probe: " + name)
            samples.append((point, changed))
    return samples


def verify_neutral(rig, reference, names, reference_mapping=None):
    """Same sites and armature-local units as the 0.40.1 test, without a weighted mesh."""
    evaluated = evaluate(rig)
    reference_mapping = reference_mapping or {name: name for name in names}
    endpoints, sampled = [], {}
    for name in names:
        pose = evaluated.pose.bones[name]
        target = reference.data.bones[reference_mapping[name]]
        endpoints.extend(((pose.head - target.head_local).length, (pose.tail - target.tail_local).length))
        sampled[name] = max((actual - rest).length
                            for rest, actual in point_samples(rig, evaluated, name))
    if not all(math.isfinite(value) for value in endpoints):
        raise ValueError("Non-finite C94 reference endpoint")
    return {
        "head_tail_max": max(endpoints, default=0.0),
        "sample_max": max(sampled.values(), default=0.0),
        "sample_count": len(sampled) * len(FRACTIONS) * 3,
        "sample_worst": sorted(sampled.items(), key=lambda item: item[1], reverse=True)[:10],
        "sample_metric": METHOD,
        "sample_scope": "Original finite armature-local probes, single-output native B-Bone deformation",
    }
