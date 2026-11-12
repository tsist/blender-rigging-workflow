# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared continuous ordinary-bone + named-Key exchange candidate.

The source rig stays untouched.  The exchange copy contains one ordinary-bone
armature shared by 1..16 selected meshes, fixed native B-Bone segment support,
piecewise Hermite local-pivot affine factors, and real named relative Key
blocks with sampled native driver/Action values.
"""
from __future__ import annotations
import bisect
import hashlib
import itertools
import json
import math
import struct
import time
from contextlib import ExitStack
from pathlib import Path
import bpy
import numpy as np
from mathutils import Matrix, Vector
from .io_support import Failure, atomic_json, digest, read_json
ADAPTER = 'BBONE_SHARED_CONTINUOUS_V1'
WITNESS_SUBDIVISIONS_PER_FRAME = 32
FORMAT_FRAME_SUBDIVISIONS = 32
KEY_GUARD = 1e-05
GLB_WEIGHT_CUTOFF = 0.0001

def _np_matrix(value):
    return np.asarray(value, dtype=np.float64)

def _qmat(q):
    q = np.asarray(q, dtype=np.float64)
    norm = np.linalg.norm(q, axis=-1, keepdims=True)
    if np.any(norm <= 1e-15):
        raise Failure('VALIDATION_FAILED', 'Zero quaternion in affine factor')
    q = q / norm
    w, x, y, z = np.moveaxis(q, -1, 0)
    return np.stack((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)), axis=-1).reshape(q.shape[:-1] + (3, 3))

def _quat_from_matrix(matrix):
    q = Matrix(matrix.tolist()).to_quaternion()
    return np.array([q.w, q.x, q.y, q.z], dtype=np.float64)

def _quat_angle(a, b):
    dot = abs(float(np.dot(a, b)))
    return 2.0 * math.acos(max(-1.0, min(1.0, dot)))

def _finite_array(value, label):
    if not np.all(np.isfinite(value)):
        raise Failure('VALIDATION_FAILED', 'Nonfinite ' + label)

def _sha256_json(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')
    return hashlib.sha256(payload).hexdigest()

def _float32(value):
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]

def _snapshot_from_points(points, triangles):
    points = np.asarray(points, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    _finite_array(points, 'predicted points')
    if triangles.size:
        a = points[triangles[:, 0]]
        b = points[triangles[:, 1]]
        c = points[triangles[:, 2]]
        area = float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum() / 2.0)
    else:
        area = 0.0
    return {'vertices': points.tolist(), 'triangles': int(len(triangles)), 'area': area, 'bounds': [points.min(axis=0).tolist() if len(points) else [0.0, 0.0, 0.0], points.max(axis=0).tolist() if len(points) else [0.0, 0.0, 0.0]]}

def _set_frame(scene, frame):
    base = math.floor(float(frame))
    scene.frame_set(base, subframe=float(frame) - base)
    bpy.context.view_layer.update()

def _eval_points(obj, depsgraph, world=False):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    try:
        if world:
            return np.asarray([list(evaluated.matrix_world @ v.co) for v in mesh.vertices], dtype=np.float64)
        return np.asarray([list(v.co) for v in mesh.vertices], dtype=np.float64)
    finally:
        evaluated.to_mesh_clear()

def _eval_snapshot(obj, depsgraph, with_triangles=False):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    try:
        points = np.asarray([list(evaluated.matrix_world @ v.co) for v in mesh.vertices], dtype=np.float64)
        mesh.calc_loop_triangles()
        triangles = [tuple((int(i) for i in tri.vertices)) for tri in mesh.loop_triangles]
        _finite_array(points, 'native evaluated mesh')
        snapshot = _snapshot_from_points(points, triangles)
        if with_triangles:
            snapshot['triangle_rows'] = triangles
        return snapshot
    finally:
        evaluated.to_mesh_clear()

def _format_topology_proofs(imported, static, position_tolerance):
    """Freeze source-verified correspondence before comparing animated areas."""
    from . import key_library as keylib
    proofs = {}
    for name, obj in imported.items():
        library = static[name].get('source_library')
        if library is None:
            raise Failure('UNSUPPORTED', 'Independent source Basis/topology library required: ' + name)
        mapping = keylib.choose_format_mapping(obj, library, position_tolerance)
        with keylib._isolated_raw_copy(obj) as probe:
            ids = keylib._source_ids(probe, keylib._raw_points(probe), library, position_tolerance=position_tolerance, mapping=mapping)
        proofs[name] = {'mapping': mapping, 'source_ids': ids, 'source_triangles': library['triangles'], 'target_triangles': keylib._triangles(obj.data), 'source_vertices': library['vertices']}
    return proofs

def _triangulation_aligned_area(reference, observed, obj, topology):
    if topology is None or len(observed['vertices']) != len(topology['source_ids']):
        raise Failure('UNSUPPORTED', 'Verified fixed source/format topology required')
    native_area = _snapshot_from_points(reference['vertices'], topology['source_triangles'])['area']
    observed_area = _snapshot_from_points(observed['vertices'], topology['target_triangles'])['area']
    return (native_area, observed_area)

def _format_geometry_check(native, observed, obj, topology=None):
    from . import format_io as exchange
    native_area, observed_area = _triangulation_aligned_area(native, observed, obj, topology)
    reference, actual = (dict(native), dict(observed))
    reference['area'], actual['area'] = (native_area, observed_area)
    check = exchange.compare_geometry(reference, actual)
    check['area_reference'] = 'triangulation_aligned'
    check['scope'] += '; area uses independently source-ID/Basis/triangle-Counter verified fixed source/format tables'
    check['native_area'] = native_area
    check['observed_area'] = observed_area
    check['raw_native_area'] = native['area']
    check['raw_area_delta'] = abs(native['area'] - observed['area'])
    ids = topology['source_ids']
    check['indexed_error_meters'] = float(np.linalg.norm(np.asarray(observed['vertices'], dtype=np.float64) - np.asarray(native['vertices'], dtype=np.float64)[ids], axis=1).max(initial=0.0))
    return check

def _source_weights(obj, rig):
    names = {group.index: group.name for group in obj.vertex_groups}
    deform = {bone.name for bone in rig.data.bones if bone.use_deform}
    rows = []
    ignored = 0
    max_sum_error = 0.0
    for vertex in obj.data.vertices:
        row = []
        for entry in vertex.groups:
            if entry.weight <= 0.0:
                continue
            name = names[entry.group]
            if name not in deform:
                ignored += 1
                continue
            row.append((name, float(entry.weight)))
        total = sum((weight for _, weight in row))
        if total <= 0.0:
            raise Failure('UNSUPPORTED', 'Mesh vertex has no positive deform-bone weight: ' + obj.name)
        max_sum_error = max(max_sum_error, abs(total - 1.0))
        rows.append([(name, weight / total) for name, weight in row])
    return (rows, {'ignored_non_deform_entries': ignored, 'max_row_sum_error': max_sum_error})

def _static_mesh_data(obj, rig):
    armature_modifier = next(
        (modifier for modifier in obj.modifiers if modifier.type == "ARMATURE" and modifier.object == rig),
        None,
    )
    if armature_modifier is None:
        raise Failure("UNSUPPORTED", "Selected mesh is not bound to the selected armature: " + obj.name)
    index = list(obj.modifiers).index(armature_modifier)
    if index != 0:
        raise Failure(
            "UNSUPPORTED",
            "B-Bone segment exchange requires the armature modifier first: " + obj.name,
        )
    if (
        not armature_modifier.show_viewport
        or not armature_modifier.show_render
        or not armature_modifier.use_vertex_groups
        or armature_modifier.use_bone_envelopes
        or armature_modifier.use_deform_preserve_volume
        or armature_modifier.vertex_group
        or armature_modifier.use_multi_modifier
    ):
        raise Failure(
            "UNSUPPORTED",
            "B-Bone segment exchange requires enabled unmasked vertex-group LBS: " + obj.name,
        )
    post = list(obj.modifiers)[1:]
    allowed = {"SUBSURF", "DATA_TRANSFER", "NODES"}
    if any(modifier.type not in allowed for modifier in post):
        raise Failure(
            "UNSUPPORTED",
            "Post-modifier stack outside the bounded base-mesh exchange: "
            + obj.name
            + " "
            + ",".join(sorted({modifier.type for modifier in post if modifier.type not in allowed})),
        )
    if obj.animation_data and (
        obj.animation_data.action
        or obj.animation_data.nla_tracks
        or obj.animation_data.drivers
    ):
        raise Failure(
            "UNSUPPORTED",
            "Mesh-object transform Action/NLA/driver is outside the verified shared exchange: "
            + obj.name,
        )
    weights, weight_meta = _source_weights(obj, rig)
    basis_local = np.asarray([list(vertex.co) for vertex in obj.data.vertices], dtype=np.float64)
    mesh_world = obj.matrix_world.copy()
    basis_world = np.asarray(
        [list(mesh_world @ vertex.co) for vertex in obj.data.vertices], dtype=np.float64
    )
    edges = [tuple(edge.vertices) for edge in obj.data.edges]
    faces = [tuple(polygon.vertices) for polygon in obj.data.polygons]

    # Predicted/native snapshots use the base topology before the explicitly
    # omitted post modifiers.  Do not mix evaluated Subsurf triangle indices
    # into the base-mesh witness cache.
    obj.data.calc_loop_triangles()
    triangles = [tuple(int(i) for i in tri.vertices) for tri in obj.data.loop_triangles]

    uv_layers = []
    for layer in obj.data.uv_layers:
        uv_layers.append(
            {
                "name": layer.name,
                "uv": [float(value) for loop in layer.data for value in (loop.uv.x, loop.uv.y)],
            }
        )

    source_groups = {}
    for group in obj.vertex_groups:
        members = []
        for vertex in obj.data.vertices:
            weight = next((entry.weight for entry in vertex.groups if entry.group == group.index), 0.0)
            if weight > 0.0:
                members.append((vertex.index, float(weight)))
        if members:
            source_groups[group.name] = members

    shape_keys = []
    if obj.data.shape_keys:
        for key in obj.data.shape_keys.key_blocks:
            shape_keys.append(
                {
                    "name": key.name,
                    "relative_key": key.relative_key.name if key.relative_key else None,
                    "vertex_group": key.vertex_group,
                    "slider_min": float(key.slider_min),
                    "slider_max": float(key.slider_max),
                    "value": float(key.value),
                    "mute": bool(key.mute),
                    "lock_shape": bool(key.lock_shape),
                    "coords": np.asarray([list(point.co) for point in key.data], dtype=np.float64),
                }
            )
    if any(key["name"] == "Basis" for key in shape_keys) is False and shape_keys:
        raise Failure("UNSUPPORTED", "Shape keys without a Basis block are outside exchange")
    if shape_keys:
        names = {key["name"] for key in shape_keys}
        if any(key["relative_key"] not in names for key in shape_keys):
            raise Failure("UNSUPPORTED", "Relative Key target is missing: " + obj.name)

    return {
        "name": obj.name,
        "data_name": obj.data.name,
        "object": obj,
        "armature": armature_modifier,
        "post": post,
        "post_report": [
            {
                "name": modifier.name,
                "type": modifier.type,
                "show_viewport": modifier.show_viewport,
                "show_render": modifier.show_render,
            }
            for modifier in post
        ],
        # Restore exactly the source visibility, including post modifiers that
        # were already disabled before this adapter touched them.
        "post_viewport": [bool(modifier.show_viewport) for modifier in post],
        "weights": weights,
        "weight_meta": weight_meta,
        "basis_local": basis_local,
        "basis_world": basis_world,
        "mesh_world": mesh_world,
        "edges": edges,
        "faces": faces,
        "triangles": triangles,
        "uv_layers": uv_layers,
        "source_groups": source_groups,
        "shape_keys": shape_keys,
        "vertices": len(basis_local),
    }

def _action_rows(action, slot=None):
    rows = []
    if slot is not None and not any(
        item.identifier == slot.identifier for item in action.slots
    ):
        raise Failure(
            "UNSUPPORTED",
            "NLA strip Action slot is not part of the source Action: "
            + action.name
            + " / "
            + slot.identifier,
        )
    slots = [slot] if slot is not None else list(action.slots)
    for layer in action.layers:
        for strip in layer.strips:
            for action_slot in slots:
                bag = strip.channelbag(action_slot)
                if not bag:
                    continue
                for curve in bag.fcurves:
                    rows.append(
                        {
                            "path": curve.data_path,
                            "index": curve.array_index,
                            "times": [float(point.co.x) for point in curve.keyframe_points],
                            "interpolation": sorted(
                                {point.interpolation for point in curve.keyframe_points}
                            ),
                        }
                    )
    return rows

def _discover_breakpoints(spec, rig, meshes):
    start = float(spec["frame_start"])
    end = float(spec["frame_end"])
    times = {start, end}
    evidence = []
    seen_actions = set()
    seen_owners = set()

    def add_action(action, owner, slot=None):
        if action is None:
            return
        identity = (action.name, slot.identifier if slot is not None else None)
        if identity in seen_actions:
            return
        seen_actions.add(identity)
        rows = _action_rows(action, slot)
        keys = sorted(
            {
                round(float(t), 9)
                for row in rows
                for t in row["times"]
                if start - 1e-9 <= float(t) <= end + 1e-9
            }
        )
        times.update(keys)
        evidence.append(
            {
                "owner": owner,
                "action": action.name,
                "action_slot": slot.identifier if slot is not None else None,
                "curves": len(rows),
                "key_times_in_range": keys,
                "interpolation": sorted(
                    {mode for row in rows for mode in row["interpolation"]}
                ),
                "scope": (
                    "active Action slot only; no unused datablock is treated as timing"
                    if slot is not None
                    else "active Action; all of its source slots are inventoried"
                ),
            }
        )

    def inspect_nla(owner, label, animation):
        """Accept only verified ordinary 1:1 local-Action clips.

        Blender 5.2.1 reports NlaStrip.influence == 0 for a normal unmuted
        strip whose helper Action is demonstrably evaluated (probe
        jobs-dcdeb74074c04a4ca6eb4273d88256b2).  The static influence value is
        therefore recorded, not used as an activity gate.  Only mute, solo,
        use_nla and strip structure define participation; animated influence,
        animated strip time, speed/repeat, modifiers, meta/transition strips
        and non-REPLACE blend modes are explicitly outside this extension.
        """
        tracks = list(animation.nla_tracks)
        if not tracks:
            return
        if animation.use_tweak_mode:
            raise Failure("UNSUPPORTED", "NLA tweak mode is outside timing discovery: " + label)
        if not animation.use_nla:
            evidence.append(
                {
                    "owner": label,
                    "action": "NLA_DISABLED",
                    "curves": 0,
                    "key_times_in_range": [],
                    "interpolation": [],
                    "scope": (
                        "animation_data.use_nla is false; no NLA track timing is "
                        "treated as contributing to the evaluated pose"
                    ),
                    "tracks": [
                        {
                            "name": track.name,
                            "mute": bool(track.mute),
                            "solo": bool(track.is_solo),
                            "strips": len(track.strips),
                        }
                        for track in tracks
                    ],
                }
            )
            return
        solo_tracks = [track for track in tracks if track.is_solo]
        active_tracks = []
        for track in tracks:
            if track.mute:
                evidence.append(
                    {
                        "owner": label,
                        "action": (
                            track.strips[0].action.name
                            if len(track.strips) == 1 and track.strips[0].action
                            else "MUTED:" + track.name
                        ),
                        "curves": 0,
                        "key_times_in_range": [],
                        "interpolation": [],
                        "scope": (
                            "muted historical NLA track is preserved and excluded "
                            "from effective timing"
                        ),
                        "track": track.name,
                        "mute": True,
                        "solo": bool(track.is_solo),
                        "effective": False,
                    }
                )
                continue
            if solo_tracks and not track.is_solo:
                evidence.append(
                    {
                        "owner": label,
                        "action": (
                            track.strips[0].action.name
                            if len(track.strips) == 1 and track.strips[0].action
                            else "SHADOWED:" + track.name
                        ),
                        "curves": 0,
                        "key_times_in_range": [],
                        "interpolation": [],
                        "scope": "unmuted track is shadowed by an active solo track",
                        "track": track.name,
                        "mute": False,
                        "solo": bool(track.is_solo),
                        "effective": False,
                    }
                )
                continue
            if not track.strips:
                evidence.append(
                    {
                        "owner": label,
                        "action": "EMPTY:" + track.name,
                        "curves": 0,
                        "key_times_in_range": [],
                        "interpolation": [],
                        "scope": "empty NLA track contributes no timing",
                        "track": track.name,
                        "mute": False,
                        "solo": bool(track.is_solo),
                        "effective": False,
                    }
                )
                continue
            strip = track.strips[0] if len(track.strips) == 1 else None
            if strip is not None and bool(getattr(strip, "mute", False)):
                evidence.append(
                    {
                        "owner": label,
                        "action": (
                            strip.action.name
                            if strip.action
                            else "MUTED_STRIP:" + track.name
                        ),
                        "action_slot": (
                            strip.action_slot.identifier
                            if strip.action_slot
                            else None
                        ),
                        "curves": 0,
                        "key_times_in_range": [],
                        "interpolation": [],
                        "scope": (
                            "muted NLA strip is preserved and excluded from effective "
                            "timing; no source keys or evaluation budget are added"
                        ),
                        "track": track.name,
                        "strip": strip.name,
                        "track_mute": False,
                        "strip_mute": True,
                        "solo": bool(track.is_solo),
                        "mute": True,
                        "effective": False,
                    }
                )
                continue
            active_tracks.append(track)
        if len(active_tracks) > 1:
            raise Failure(
                "UNSUPPORTED",
                "Multiple simultaneously active NLA tracks are outside the verified "
                "ordinary 1:1 clip extension: "
                + label
                + " "
                + ",".join(track.name for track in active_tracks),
            )
        for track in active_tracks:
            if len(track.strips) != 1:
                raise Failure(
                    "UNSUPPORTED",
                    "NLA track with multiple strips is outside the verified 1:1 clip "
                    "extension: "
                    + label
                    + " / "
                    + track.name,
                )
            strip = track.strips[0]
            if strip.type != "CLIP":
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip type "
                    + str(strip.type)
                    + " is outside the verified ordinary local CLIP: "
                    + label
                    + " / "
                    + track.name,
                )
            for flag, text in (
                (strip.use_animated_time, "animated strip time"),
                (strip.use_animated_influence, "animated strip influence"),
                (strip.use_auto_blend, "automatic blend"),
            ):
                if flag:
                    raise Failure(
                        "UNSUPPORTED",
                        "NLA " + text + " is outside the verified 1:1 clip: "
                        + label + " / " + track.name,
                    )
            if bool(getattr(strip, "use_reverse", False)):
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip reverse playback is outside the verified forward "
                    "1:1 clip: " + label + " / " + track.name,
                )
            if strip.modifiers:
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip modifiers are outside the verified 1:1 clip: "
                    + label
                    + " / "
                    + track.name
                    + " "
                    + ",".join(modifier.type for modifier in strip.modifiers),
                )
            if abs(float(strip.scale) - 1.0) > 1e-9:
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip speed/scale != 1 is outside the verified 1:1 clip: "
                    + label
                    + " / "
                    + track.name
                    + " scale="
                    + repr(float(strip.scale)),
                )
            if abs(float(strip.repeat) - 1.0) > 1e-9:
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip repeat != 1 is outside the verified 1:1 clip: "
                    + label
                    + " / "
                    + track.name
                    + " repeat="
                    + repr(float(strip.repeat)),
                )
            if strip.blend_type != "REPLACE":
                raise Failure(
                    "UNSUPPORTED",
                    "NLA blend type "
                    + str(strip.blend_type)
                    + " is outside the verified REPLACE clip: "
                    + label
                    + " / "
                    + track.name,
                )
            if strip.extrapolation not in ("HOLD", "NOTHING"):
                raise Failure(
                    "UNSUPPORTED",
                    "NLA extrapolation "
                    + str(strip.extrapolation)
                    + " is outside the verified HOLD/NOTHING clip: "
                    + label
                    + " / "
                    + track.name,
                )
            action = strip.action
            slot = strip.action_slot
            if action is None or slot is None:
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip requires an explicit Action and Action slot: "
                    + label
                    + " / "
                    + track.name,
                )
            action_start = float(strip.action_frame_start)
            action_end = float(strip.action_frame_end)
            strip_start = float(strip.frame_start)
            strip_end = float(strip.frame_end)
            if action_end <= action_start or strip_end <= strip_start:
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip/action frame range is empty or reversed: "
                    + label
                    + " / "
                    + track.name,
                )
            if abs((action_end - action_start) - (strip_end - strip_start)) > 1e-9:
                raise Failure(
                    "UNSUPPORTED",
                    "NLA strip and Action frame spans differ outside the verified 1:1 "
                    "identity mapping: "
                    + label
                    + " / "
                    + track.name,
                )
            rows = _action_rows(action, slot)
            source_keys = sorted(
                {
                    round(float(t), 9)
                    for row in rows
                    for t in row["times"]
                }
            )
            used_keys = [
                key
                for key in source_keys
                if action_start - 1e-9 <= key <= action_end + 1e-9
            ]
            mapped_keys = sorted(
                {
                    round(strip_start + (key - action_start), 9)
                    for key in used_keys
                    if start - 1e-9
                    <= strip_start + (key - action_start)
                    <= end + 1e-9
                }
            )
            boundaries = {
                round(value, 9)
                for value in (
                    strip_start,
                    strip_end,
                    strip_start + float(strip.blend_in),
                    strip_end - float(strip.blend_out),
                )
                if start - 1e-9 <= value <= end + 1e-9
            }
            times.update(mapped_keys)
            times.update(boundaries)
            evidence.append(
                {
                    "owner": label,
                    "action": action.name,
                    "action_slot": slot.identifier,
                    "curves": len(rows),
                    "key_times_in_range": mapped_keys,
                    "interpolation": sorted(
                        {mode for row in rows for mode in row["interpolation"]}
                    ),
                    "scope": (
                        "unmuted ordinary local CLIP; action key times mapped 1:1 "
                        "into evaluated timeline; source keys and effective boundaries retained"
                    ),
                    "track": track.name,
                    "track_mute": bool(track.mute),
                    "track_solo": bool(track.is_solo),
                    "strip": strip.name,
                    "strip_mute": bool(getattr(strip, "mute", False)),
                    "strip_type": str(strip.type),
                    "action_frame_start": action_start,
                    "action_frame_end": action_end,
                    "frame_start": strip_start,
                    "frame_end": strip_end,
                    "scale": float(strip.scale),
                    "repeat": float(strip.repeat),
                    "blend_type": str(strip.blend_type),
                    "extrapolation": str(strip.extrapolation),
                    "influence_property": float(strip.influence),
                    "influence_scope": (
                        "recorded only; Blender 5.2.1 evaluated this unmuted strip "
                        "with a zero static influence property (reset-pose probe)"
                    ),
                    "use_reverse": False,
                    "mute": bool(track.mute) or bool(getattr(strip, "mute", False)),
                    "effective": True,
                    "source_key_times": used_keys,
                    "mapped_key_times_in_range": mapped_keys,
                    "mapping": (
                        "timeline = strip.frame_start + (action_key - "
                        "strip.action_frame_start); scale=1, repeat=1"
                    ),
                }
            )

    def inspect_owner(owner, label):
        pointer = owner.as_pointer()
        if pointer in seen_owners:
            return
        seen_owners.add(pointer)
        animation = getattr(owner, "animation_data", None)
        if not animation:
            return
        if animation.use_tweak_mode:
            raise Failure("UNSUPPORTED", "NLA tweak mode is outside timing discovery: " + label)
        if animation.action:
            add_action(animation.action, label, animation.action_slot)
        inspect_nla(owner, label, animation)

    inspect_owner(rig, "rig:" + rig.name)
    for mesh in meshes:
        inspect_owner(mesh, "mesh:" + mesh.name)
        if mesh.data.shape_keys:
            inspect_owner(mesh.data.shape_keys, "keys:" + mesh.name)

    target_objects = set()
    for owner in [rig, *meshes, *[mesh.data.shape_keys for mesh in meshes if mesh.data.shape_keys]]:
        animation = getattr(owner, "animation_data", None)
        if not animation:
            continue
        for curve in animation.drivers:
            driver = curve.driver
            for variable in driver.variables:
                for target in variable.targets:
                    if target.id and isinstance(target.id, bpy.types.Object):
                        target_objects.add(target.id.as_pointer())
                        inspect_owner(target.id, "driver-target:" + target.id.name)
    times = sorted(t for t in times if start - 1e-9 <= t <= end + 1e-9)
    return times, evidence

def _witness_times(breakpoints, start, end, per_frame=WITNESS_SUBDIVISIONS_PER_FRAME):
    result = {round(float(start), 9), round(float(end), 9)}
    for a, b in zip(breakpoints[:-1], breakpoints[1:]):
        width = float(b) - float(a)
        steps = max(1, int(round(width * per_frame)))
        for index in range(steps + 1):
            result.add(round(float(a) + width * index / steps, 9))
    return sorted(result)

def _uniform_format_times(start, end, per_frame=FORMAT_FRAME_SUBDIVISIONS):
    start = float(start)
    end = float(end)
    steps = max(1, int(round((end - start) * per_frame)))
    return [round(start + (end - start) * index / steps, 9) for index in range(steps + 1)]

def _float32_times(values):
    grouped = {}
    for value in values:
        canonical = _float32(float(value))
        grouped.setdefault(canonical, []).append(float(value))
    times = sorted(grouped)
    collapsed = [{'float32': key, 'inputs': sorted(set(rows))} for key, rows in grouped.items() if len(set(rows)) > 1]
    return (times, collapsed)

def _seed_keys(breakpoints, start, end, seed_substeps):
    result = {round(float(start), 9), round(float(end), 9)}
    for a, b in zip(breakpoints[:-1], breakpoints[1:]):
        for index in range(seed_substeps + 1):
            result.add(round(float(a) + (float(b) - float(a)) * index / seed_substeps, 9))
    return sorted(result)

def _mapping_rows(rig, mesh, pre_local, weights, rig_world, mesh_world):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = rig.evaluated_get(depsgraph)
    local = rig_world.inverted() @ mesh_world
    rows = []
    for point, weight_row in zip(pre_local, weights):
        query = local @ Vector((float(point[0]), float(point[1]), float(point[2])))
        mapped = []
        for name, weight in weight_row:
            pose_bone = evaluated.pose.bones[name]
            segments = pose_bone.bone.bbone_segments
            if segments <= 1:
                mapped.append(((name, 0), weight))
                continue
            index, blend = pose_bone.bbone_segment_index(query)
            if not 0 <= index < segments or not -1e-06 <= float(blend) <= 1.0 + 1e-06:
                raise Failure('UNSUPPORTED', 'B-Bone segment index outside validated domain: ' + name + ' ' + str(index) + ' ' + str(blend))
            blend = float(blend)
            mapped.append(((name, index), weight * (1.0 - blend)))
            mapped.append(((name, index + 1), weight * blend))
        rows.append([(joint, weight) for joint, weight in mapped if weight > 0.0])
    return rows

def _union_support(mesh_data, mapping, pre_world, joint_index):
    totals = np.zeros((len(joint_index), 3), dtype=np.float64)
    masses = np.zeros(len(joint_index), dtype=np.float64)
    for index, row in enumerate(mapping):
        point = pre_world[index]
        for joint, weight in row:
            number = joint_index[joint]
            totals[number] += weight * point
            masses[number] += weight
    if np.any(masses <= 0.0):
        raise Failure('VALIDATION_FAILED', 'A shared affine joint has no weighted support')
    return totals / masses[:, None]

def _compute_joint_matrices(rig, rig_world, bind_rig_world, bone_segments, world_mode):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = rig.evaluated_get(depsgraph)
    result = {}
    if world_mode == "common_rig_mesh_motion":
        right = bind_rig_world.inverted()
    elif world_mode == "fixed_mesh_world":
        right = rig_world.inverted()
    else:
        raise Failure("VALIDATION_FAILED", "Unknown verified world-motion mode: " + str(world_mode))
    for name in bone_segments:
        pose_bone = evaluated.pose.bones[name]
        segments = pose_bone.bone.bbone_segments
        if segments <= 1:
            deformation = pose_bone.matrix @ pose_bone.bone.matrix_local.inverted()
            result[(name, 0)] = _np_matrix(rig_world @ deformation @ right)
            continue
        for segment in range(segments + 1):
            deformation = (
                pose_bone.matrix
                @ pose_bone.bbone_segment_matrix(segment, rest=False)
                @ pose_bone.bbone_segment_matrix(segment, rest=True).inverted()
                @ pose_bone.bone.matrix_local.inverted()
            )
            result[(name, segment)] = _np_matrix(rig_world @ deformation @ right)
    return result

def _flatten_influences(mapping, joint_index):
    vertices, joints, weights = ([], [], [])
    for vertex, row in enumerate(mapping):
        for joint, weight in row:
            vertices.append(vertex)
            joints.append(joint_index[joint])
            weights.append(weight)
    return (np.asarray(vertices, dtype=np.int64), np.asarray(joints, dtype=np.int64), np.asarray(weights, dtype=np.float64))

def _reconstruct(pre_world, influences, matrices):
    infl_v, infl_j, infl_w = influences
    homogeneous = np.concatenate((pre_world, np.ones((len(pre_world), 1), dtype=np.float64)), axis=1)
    selected = matrices[infl_j]
    contributions = np.einsum('nij,nj->ni', selected, homogeneous[infl_v])[:, :3]
    points = np.zeros_like(pre_world)
    np.add.at(points, infl_v, contributions * infl_w[:, None])
    return points

def _disable_post(mesh_data):
    for modifier in mesh_data['post']:
        modifier.show_viewport = False

def _enable_post(mesh_data):
    for modifier, visible in zip(mesh_data["post"], mesh_data["post_viewport"]):
        modifier.show_viewport = visible

def _capture_native(params, spec, job, rig, meshes, static, breakpoints, sampling):
    source = Path(params["file"])
    start = float(spec["frame_start"])
    end = float(spec["frame_end"])
    profile = params.get("driver_profile")
    _set_frame(bpy.context.scene, start)
    rig_world = rig.matrix_world.copy()
    bind_rig_world = rig_world.copy()
    _validate_world_matrix(rig_world, "rig bind world")
    if bpy.context.scene.unit_settings.scale_length != 1.0:
        raise Failure("UNSUPPORTED", "Shared continuous exchange requires meter scale one")
    if bpy.context.scene.render.fps_base != 1.0:
        raise Failure("UNSUPPORTED", "Shared continuous exchange requires integer fps")
    if any(mesh_data["shape_keys"] for mesh_data in static.values()):
        pass
    for name, mesh_data in static.items():
        if mesh_data["vertices"] > 256000000:
            raise Failure("UNSUPPORTED", "Mesh vertex count outside bounded exchange: " + name)
        _validate_world_matrix(mesh_data["mesh_world"], "mesh bind world " + name)

    # Proven native slice (frozen SHA 22487edb...): fixed segment support is
    # computed from the bind/basis mesh in REST, independent of the initial
    # frame or cached/fresh depsgraph.  This exact slice passed 97 witnesses
    # in two independent processes at 7.930518706367953e-7 m.
    previous_pose_position = rig.data.pose_position
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mapping = {}
    try:
        for name, mesh_data in static.items():
            mapping[name] = _mapping_rows(
                rig,
                mesh_data["object"],
                mesh_data["basis_local"],
                mesh_data["weights"],
                rig_world,
                mesh_data["mesh_world"],
            )
    finally:
        rig.data.pose_position = previous_pose_position
        bpy.context.view_layer.update()
    joint_set = {joint for rows in mapping.values() for row in rows for joint, _ in row}
    joints = sorted(joint_set)
    joint_index = {joint: index for index, joint in enumerate(joints)}
    if not joints:
        raise Failure("VALIDATION_FAILED", "Shared affine joint union is empty")
    for name, mesh_data in static.items():
        mesh_data["mapping"] = mapping[name]
        mesh_data["influences"] = _flatten_influences(mapping[name], joint_index)
        mesh_data["joints_used"] = sorted(
            {joint_index[joint] for row in mapping[name] for joint, _ in row}
        )
    # Supports and pivots are shared across every mesh that uses the joint.
    pre_world_start = {name: mesh_data["basis_world"] for name, mesh_data in static.items()}
    totals = np.zeros((len(joints), 3), dtype=np.float64)
    masses = np.zeros(len(joints), dtype=np.float64)
    for name, mesh_data in static.items():
        for vertex, row in enumerate(mapping[name]):
            point = pre_world_start[name][vertex]
            for joint, weight in row:
                index = joint_index[joint]
                totals[index] += weight * point
                masses[index] += weight
    if np.any(masses <= 0.0):
        raise Failure("VALIDATION_FAILED", "A shared affine joint has no weighted support")
    centroids = totals / masses[:, None]
    bone_segments = sorted({name for name, _ in joints})
    seed_keys = _seed_keys(
        breakpoints, start, end, int(sampling["seed_substeps"])
    )
    uniform_format_times = _uniform_format_times(start, end, sampling.get("format_substeps", 32))
    original_witness = _witness_times(breakpoints, start, end, sampling.get("witness_substeps", 32))
    raw_times = sorted(
        set(seed_keys)
        | {round(float(frame), 9) for frame in original_witness}
        | {round(float(frame), 9) for frame in uniform_format_times}
        | {round(float(frame), 9) for frame in breakpoints}
    )
    # One unified time table: original non-uniform native witnesses, source
    # action breakpoints, seed subdivisions, refinement keys and the uniform
    # format grid.  Times are canonicalized to float32 because both FBX KTIME
    # and Blender FCurve frames only identity-preserve float32 values.
    witness, float32_collapsed = _float32_times(raw_times)
    format_times = witness
    total_vertices = sum(mesh_data["vertices"] for mesh_data in static.values())
    required_evaluations = len(witness) + 1
    required_vertex_samples = required_evaluations * total_vertices
    estimated_output = (
        source.stat().st_size * 2
        + total_vertices * 256
        + len(joints) * 1024
        + len(witness) * 64
    )
    budget = {
        "max_evaluations": int(sampling["max_evaluations"]),
        "required_evaluations": int(required_evaluations),
        "max_vertex_samples": int(sampling["max_vertex_samples"]),
        "required_vertex_samples": int(required_vertex_samples),
        "max_affine_joints": int(sampling["max_affine_joints"]),
        "required_affine_joints": len(joints),
        "max_output_bytes": int(sampling["max_output_bytes"]),
        "estimated_output_bytes_diagnostic_only": int(estimated_output),
        "witness_times": len(witness),
        "witness_substeps": int(sampling.get("witness_substeps", 32)),
        "format_substeps": int(sampling.get("format_substeps", 32)),
        "seed_substeps": int(sampling["seed_substeps"]),
        "max_depth": int(sampling["max_depth"]),
        "max_position_error": float(sampling["max_position_error"]),
        "total_vertices": int(total_vertices),
    }
    budget["ok"] = (
        budget["required_evaluations"] <= budget["max_evaluations"]
        and budget["required_vertex_samples"] <= budget["max_vertex_samples"]
        and budget["required_affine_joints"] <= budget["max_affine_joints"]
    )
    atomic_json(job / "continuous-budget.json", budget)
    if not budget["ok"]:
        checks = (
            ("required_evaluations", "max_evaluations"),
            ("required_vertex_samples", "max_vertex_samples"),
            ("required_affine_joints", "max_affine_joints"),
        )
        failures = [
            required for required, allowed in checks if budget[required] > budget[allowed]
        ]
        raise Failure(
            "UNSUPPORTED",
            "Shared continuous budget refusal before native success: " + ",".join(failures),
        )

    bind_relative = {
        name: bind_rig_world.inverted() @ mesh_data["mesh_world"]
        for name, mesh_data in static.items()
    }
    world_mode = None
    world_evidence = {
        name: {
            "max_absolute_world_error": 0.0,
            "max_relative_world_error": 0.0,
        }
        for name in static
    }
    native_times = []
    matrices_by_time = {}
    pre_world_by_time = {}
    key_values_by_time = {}
    native_evaluations = 1
    for frame in witness:
        _set_frame(bpy.context.scene, frame)
        if profile:
            from .driver_policy import evaluated as evaluate_drivers

            evaluate_drivers(profile)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        current_rig_world = rig.matrix_world.copy()
        _validate_world_matrix(current_rig_world, "rig world at frame " + repr(frame))
        current_mesh_world = {}
        absolute_constant = True
        relative_constant = True
        for name, mesh_data in static.items():
            mesh_world_now = mesh_data["object"].matrix_world.copy()
            _validate_world_matrix(
                mesh_world_now, "mesh world " + name + " at frame " + repr(frame)
            )
            current_mesh_world[name] = mesh_world_now
            absolute_error = _matrix_delta(mesh_world_now, mesh_data["mesh_world"])
            relative_error = _matrix_delta(
                current_rig_world.inverted() @ mesh_world_now,
                bind_relative[name],
            )
            world_evidence[name]["max_absolute_world_error"] = max(
                world_evidence[name]["max_absolute_world_error"], absolute_error
            )
            world_evidence[name]["max_relative_world_error"] = max(
                world_evidence[name]["max_relative_world_error"], relative_error
            )
            if absolute_error > 1e-7:
                absolute_constant = False
            if relative_error > 1e-7:
                relative_constant = False
        if world_mode is None:
            if absolute_constant and relative_constant:
                # The rig has not moved yet; both representations coincide.
                pass
            elif absolute_constant:
                world_mode = "fixed_mesh_world"
            elif relative_constant:
                world_mode = "common_rig_mesh_motion"
            else:
                raise Failure(
                    "UNSUPPORTED",
                    "Independent Mesh/rig relative world motion is outside the verified "
                    "exchange: frame "
                    + repr(frame),
                )
        elif world_mode == "fixed_mesh_world" and not absolute_constant:
            raise Failure(
                "UNSUPPORTED",
                "Mesh world transform changed after fixed_mesh_world was selected: frame "
                + repr(frame),
            )
        elif world_mode == "common_rig_mesh_motion" and not relative_constant:
            raise Failure(
                "UNSUPPORTED",
                "Mesh/rig relative world transform changed after common motion was selected: frame "
                + repr(frame),
            )
        active_world_mode = world_mode or "fixed_mesh_world"
        pre_world = {}
        snapshots = {}
        key_values = {}
        for name, mesh_data in static.items():
            obj = mesh_data["object"]
            mesh_data["armature"].show_viewport = False
            _disable_post(mesh_data)
            bpy.context.view_layer.update()
            depsgraph = bpy.context.evaluated_depsgraph_get()
            local_points = _eval_points(obj, depsgraph, world=False)
            pre_world[name] = np.asarray(
                [list(mesh_data["mesh_world"] @ Vector(point.tolist())) for point in local_points],
                dtype=np.float64,
            )
            mesh_data["armature"].show_viewport = True
            bpy.context.view_layer.update()
            depsgraph = bpy.context.evaluated_depsgraph_get()
            # The exchange is defined on the selected base meshes.  Keep the
            # documented post-modifier omission symmetric for the native
            # witness; previously only the pre-deformation read disabled it.
            snapshots[name] = _eval_snapshot(obj, depsgraph)
            key_values[name] = (
                {key.name: float(key.value) for key in obj.data.shape_keys.key_blocks}
                if obj.data.shape_keys
                else {}
            )
            _enable_post(mesh_data)
        segment_matrices = _compute_joint_matrices(
            rig,
            current_rig_world,
            bind_rig_world,
            bone_segments,
            active_world_mode,
        )
        matrices = np.stack([segment_matrices[joint] for joint in joints])
        for name, mesh_data in static.items():
            points = _reconstruct(pre_world[name], mesh_data["influences"], matrices)
            native_points = np.asarray(snapshots[name]["vertices"], dtype=np.float64)
            errors = np.linalg.norm(points - native_points, axis=1)
            error = float(errors.max(initial=0.0))
            mesh_data.setdefault("fixed_mapping_errors", {})[frame] = error
            if error > KEY_GUARD:
                vertex = int(np.argmax(errors))
                mesh_data.setdefault("fixed_mapping_diagnostics", []).append(
                    {
                        "frame": frame,
                        "mesh": name,
                        "max_error": error,
                        "worst_vertex": vertex,
                        "native_point": native_points[vertex].tolist(),
                        "reconstructed_point": points[vertex].tolist(),
                        "source_weights": [
                            [bone, weight] for bone, weight in mesh_data["weights"][vertex]
                        ],
                        "joint_weights": [
                            [joint, weight] for joint, weight in mesh_data["mapping"][vertex]
                        ],
                    }
                )
        matrices_by_time[frame] = matrices
        pre_world_by_time[frame] = pre_world
        key_values_by_time[frame] = key_values
        native_times.append(
            {
                "frame": frame,
                "requested_frame": frame,
                "actual_frame": float(bpy.context.scene.frame_current_final),
                "world": {
                    "mode": active_world_mode,
                    "rig_world": [list(row) for row in current_rig_world],
                    "mesh_world": {
                        name: [list(row) for row in current_mesh_world[name]]
                        for name in static
                    },
                    "absolute_constant": bool(absolute_constant),
                    "relative_constant": bool(relative_constant),
                },
                "meshes": {
                    name: {
                        "snapshot": snapshots[name],
                        "key_values": key_values[name],
                        "fixed_mapping_error": static[name]["fixed_mapping_errors"][frame],
                    }
                    for name in static
                },
                "joint_matrix_sha256": hashlib.sha256(matrices.tobytes()).hexdigest(),
            }
        )
        native_evaluations += 1
        for mesh_data in static.values():
            mesh_data["armature"].show_viewport = True
            _enable_post(mesh_data)

    if world_mode is None:
        world_mode = "fixed_mesh_world"
    native_cache = {
        "version": "1.0",
        "adapter": ADAPTER,
        "source": {"file": str(source), "sha256": digest(source)},
        "clock": {
            "fps": int(bpy.context.scene.render.fps),
            "fps_base": float(bpy.context.scene.render.fps_base),
            "frame_start": start,
            "frame_end": end,
        },
        "breakpoints": breakpoints,
        "bind_frame": start,
        "bind_rig_world": [list(row) for row in bind_rig_world],
        "world_motion": {
            "mode": world_mode,
            "mesh_bind_world": {
                name: [list(row) for row in mesh_data["mesh_world"]]
                for name, mesh_data in static.items()
            },
            "mesh_evidence": world_evidence,
            "scope": (
                "fixed_mesh_world uses rigWorld(t) @ deformation(t) @ "
                "rigWorld(t)^-1 with the bind mesh world; common_rig_mesh_motion "
                "uses rigWorld(t) @ deformation(t) @ bindRigWorld^-1. Independent "
                "relative object motion, singular and negative-scale worlds are rejected."
            ),
        },
        "witness_times": witness,
        "format_times": format_times,
        "uniform_format_times": uniform_format_times,
        "original_witness_times": original_witness,
        "float32_identity": float32_collapsed,
        "meshes": {
            name: {
                "source_mesh": name,
                "source_data": mesh_data["data_name"],
                "vertices": mesh_data["vertices"],
                "triangles": len(mesh_data["triangles"]),
                "joints": len(mesh_data["joints_used"]),
                "weight_meta": mesh_data["weight_meta"],
                "shape_key_names": [key["name"] for key in mesh_data["shape_keys"]],
                "post_viewport": list(mesh_data["post_viewport"]),
                "post_report": mesh_data["post_report"],
            }
            for name, mesh_data in static.items()
        },
        "joints": [
            {"index": index, "source_bone": joint[0], "segment": joint[1]}
            for index, joint in enumerate(joints)
        ],
        "centroids": centroids.tolist(),
        "times": native_times,
        "native_evaluations": native_evaluations,
        "fps": int(bpy.context.scene.render.fps),
        "fixed_mapping_max_error": max(
            error
            for mesh_data in static.values()
            for error in mesh_data.get("fixed_mapping_errors", {}).values()
        ),
        "scope": "Fixed native B-Bone segment support; base meshes before listed post modifiers; finite witness times only; verified world-motion mode "
        + world_mode,
    }
    mapping_failures = [
        row
        for mesh_data in static.values()
        for row in mesh_data.get("fixed_mapping_diagnostics", [])
    ]
    if mapping_failures:
        atomic_json(
            job / "continuous-native-mapping-diagnostic.json",
            {
                "adapter": ADAPTER,
                "failures": mapping_failures,
                "threshold": KEY_GUARD,
                "fixed_mapping_errors": {
                    name: mesh_data.get("fixed_mapping_errors", {})
                    for name, mesh_data in static.items()
                },
            },
        )
        raise Failure(
            "UNSUPPORTED",
            "Fixed native B-Bone segment support no longer reproduces the source mesh: "
            + ", ".join(
                f"{row['mesh']}@{row['frame']}={row['max_error']}"
                for row in mapping_failures[:6]
            ),
        )
    atomic_json(job / "continuous-native-witness.json", native_cache)
    return {
        "joints": joints,
        "joint_index": joint_index,
        "centroids": centroids,
        "witness": witness,
        "format_times": format_times,
        "uniform_format_times": uniform_format_times,
        "original_witness_times": original_witness,
        "float32_identity": float32_collapsed,
        "matrices": matrices_by_time,
        "pre_world": pre_world_by_time,
        "key_values": key_values_by_time,
        "native": native_cache,
        "budget": budget,
        "native_evaluations": native_evaluations,
        "native_witness_file": str(job / "continuous-native-witness.json"),
        "fps": int(bpy.context.scene.render.fps),
        "world_mode": world_mode,
        "world_evidence": world_evidence,
    }

def _svd_candidates(linear):
    u, singular, vt = np.linalg.svd(linear)
    singular = singular.copy()
    if np.linalg.det(u) < 0:
        u[:, -1] *= -1
        singular[-1] *= -1
    if np.linalg.det(vt) < 0:
        vt[-1, :] *= -1
        singular[-1] *= -1
    candidates = []
    for order in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            permutation = np.zeros((3, 3), dtype=np.float64)
            for column, row in enumerate(order):
                permutation[row, column] = signs[column]
            if np.linalg.det(permutation) < 0:
                continue
            u2 = u @ permutation
            vt2 = permutation.T @ vt
            scale2 = singular[list(order)]
            qa = _quat_from_matrix(u2)
            qd = _quat_from_matrix(vt2)
            candidates.append((qa, scale2, qd, order, signs))
    if len(candidates) != 24:
        raise Failure('VALIDATION_FAILED', 'SVD proper signed-permutation inventory is not 24')
    return candidates

def _angle_matrix(a, b):
    dot = np.abs(np.clip(a @ b.T, -1.0, 1.0))
    return 2.0 * np.arccos(dot)

def _select_factor_path(linear_by_key, centroids, joints):
    key_count = len(linear_by_key)
    joint_count = len(joints)
    locations = np.zeros((key_count, joint_count, 3), dtype=np.float64)
    rotations_a = np.zeros((key_count, joint_count, 4), dtype=np.float64)
    scales = np.zeros((key_count, joint_count, 3), dtype=np.float64)
    rotations_d = np.zeros((key_count, joint_count, 4), dtype=np.float64)
    for key_index in range(key_count):
        matrices = linear_by_key[key_index]
        for joint_index in range(joint_count):
            linear = matrices[joint_index][:3, :3]
            translation = matrices[joint_index][:3, 3]
            centroid = centroids[joint_index]
            locations[key_index, joint_index] = translation + linear @ centroid - centroid
    for joint_index in range(joint_count):
        candidate_rows = []
        for key_index in range(key_count):
            candidate_rows.append(_svd_candidates(linear_by_key[key_index][joint_index][:3, :3]))
        qa_all = np.asarray([[candidate[0] for candidate in row] for row in candidate_rows])
        qd_all = np.asarray([[candidate[2] for candidate in row] for row in candidate_rows])
        scale_all = np.asarray([[candidate[1] for candidate in row] for row in candidate_rows])
        dp = np.zeros((key_count, 24), dtype=np.float64)
        back = np.zeros((key_count, 24), dtype=np.int64)
        for key_index in range(1, key_count):
            cost = _angle_matrix(qa_all[key_index], qa_all[key_index - 1]) + _angle_matrix(qd_all[key_index], qd_all[key_index - 1])
            total = dp[key_index - 1][:, None] + cost.T
            back[key_index] = np.argmin(total, axis=0)
            dp[key_index] = total[back[key_index], np.arange(24)]
        path = np.zeros(key_count, dtype=np.int64)
        path[-1] = int(np.argmin(dp[-1]))
        for key_index in range(key_count - 1, 0, -1):
            path[key_index - 1] = back[key_index, path[key_index]]
        previous_a = None
        previous_d = None
        for key_index in range(key_count):
            candidate = int(path[key_index])
            qa = qa_all[key_index, candidate].copy()
            qd = qd_all[key_index, candidate].copy()
            if previous_a is not None:
                if float(np.dot(qa, previous_a)) < 0:
                    qa = -qa
                if float(np.dot(qd, previous_d)) < 0:
                    qd = -qd
            rotations_a[key_index, joint_index] = qa
            rotations_d[key_index, joint_index] = qd
            scales[key_index, joint_index] = scale_all[key_index, candidate]
            previous_a = qa
            previous_d = qd
    return (locations, rotations_a, scales, rotations_d)

def _quadratic_derivatives(times, values):
    times = np.asarray(times, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    derivatives = np.zeros_like(values)
    if len(times) == 0:
        return derivatives
    if len(times) == 1:
        return derivatives
    if len(times) == 2:
        slope = (values[1] - values[0]) / (times[1] - times[0])
        derivatives[0] = slope
        derivatives[1] = slope
        return derivatives
    t0, t1, t2 = (times[0], times[1], times[2])
    v0, v1, v2 = (values[0], values[1], values[2])
    derivatives[0] = v0 * (2 * t0 - t1 - t2) / ((t0 - t1) * (t0 - t2)) + v1 * (t0 - t2) / ((t1 - t0) * (t1 - t2)) + v2 * (t0 - t1) / ((t2 - t0) * (t2 - t1))
    derivatives[1:-1] = (values[2:] - values[:-2]) / (times[2:, None, None] - times[:-2, None, None])
    tl, tm, tr = (times[-3], times[-2], times[-1])
    vl, vm, vr = (values[-3], values[-2], values[-1])
    derivatives[-1] = vl * (tr - tm) / ((tl - tm) * (tl - tr)) + vm * (tr - tl) / ((tm - tl) * (tm - tr)) + vr * (2 * tr - tl - tm) / ((tr - tl) * (tr - tm))
    return derivatives

def _hermite_slopes(key_times, values, breakpoints):
    key_times = np.asarray(key_times, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    count = len(key_times)
    left = np.zeros_like(values)
    right = np.zeros_like(values)
    for a, b in zip(breakpoints[:-1], breakpoints[1:]):
        indices = [index for index, t in enumerate(key_times) if float(a) - 1e-09 <= float(t) <= float(b) + 1e-09]
        if len(indices) < 2:
            continue
        interval_times = key_times[indices]
        interval_values = values[indices]
        derivatives = _quadratic_derivatives(interval_times, interval_values)
        for position, key_index in enumerate(indices):
            if position == 0:
                right[key_index] = derivatives[0]
            elif position == len(indices) - 1:
                left[key_index] = derivatives[-1]
            else:
                left[key_index] = derivatives[position]
                right[key_index] = derivatives[position]
    left[0] = right[0]
    right[-1] = left[-1]
    if count == 2:
        left[:] = right[:] = (values[1] - values[0]) / (key_times[1] - key_times[0])
    return (left, right)

def _interpolate_factors(key_times, locations, rotations_a, scales, rotations_d, left_slopes, right_slopes, centroids, times):
    key_times = np.asarray(key_times, dtype=np.float64)
    times = np.asarray(times, dtype=np.float64)
    count = len(key_times)
    joint_count = locations.shape[1]
    result_matrices = {}
    for frame in times:
        lo = int(np.searchsorted(key_times, frame, side='right') - 1)
        lo = max(0, min(count - 2, lo))
        hi = lo + 1
        width = key_times[hi] - key_times[lo]
        alpha = 0.0 if width <= 1e-15 else (frame - key_times[lo]) / width
        h00 = 2 * alpha ** 3 - 3 * alpha ** 2 + 1
        h10 = alpha ** 3 - 2 * alpha ** 2 + alpha
        h01 = -2 * alpha ** 3 + 3 * alpha ** 2
        h11 = alpha ** 3 - alpha ** 2
        location = h00 * locations[lo] + h10 * width * right_slopes[lo] + h01 * locations[hi] + h11 * width * left_slopes[hi]
        qa = (1 - alpha) * rotations_a[lo] + alpha * rotations_a[hi]
        qd = (1 - alpha) * rotations_d[lo] + alpha * rotations_d[hi]
        scale = (1 - alpha) * scales[lo] + alpha * scales[hi]
        ra = _qmat(qa)
        rd = _qmat(qd)
        linear = np.einsum('nij,njk->nik', ra, scale[:, :, None] * rd)
        matrices = np.tile(np.eye(4), (joint_count, 1, 1))
        matrices[:, :3, :3] = linear
        matrices[:, :3, 3] = centroids + location - np.einsum('nij,nj->ni', linear, centroids)
        result_matrices[frame] = matrices
    return result_matrices

def _create_action(owner, name, id_type):
    action = bpy.data.actions.new(name)
    slot = action.slots.new(id_type=id_type, name=owner.name)
    action.layers.new('Samples').strips.new(type='KEYFRAME')
    bag = action.layers[0].strips[0].channelbag(slot, ensure=True)
    animation = owner.animation_data_create()
    animation.action = action
    animation.action_slot = slot
    return (action, bag)

def _verify_curve_storage(curve, times, values, handles=None):
    points = curve.keyframe_points
    if len(points) != len(times):
        stored_times = [float(point.co.x) for point in points]
        missing = [float(t) for t in times if not any((abs(float(t) - s) <= 1e-05 for s in stored_times))]
        raise Failure('VALIDATION_FAILED', 'FCurve writer lost keys: %s expected=%d stored=%d missing=%r' % (curve.data_path, len(times), len(points), missing[:8]))
    for index, point in enumerate(points):
        expected_time = _float32(float(times[index]))
        if float(point.co.x) != expected_time:
            raise Failure('VALIDATION_FAILED', 'FCurve writer moved key time: %s index=%d expected=%r stored=%r' % (curve.data_path, index, expected_time, float(point.co.x)))
        if index and float(point.co.x) <= float(points[index - 1].co.x):
            raise Failure('VALIDATION_FAILED', 'FCurve writer merged or reordered key times: %s index=%d' % (curve.data_path, index))
        expected_value = _float32(float(values[index]))
        if abs(float(point.co.y) - expected_value) > 1e-09:
            raise Failure('VALIDATION_FAILED', 'FCurve writer changed key value: %s index=%d expected=%r stored=%r' % (curve.data_path, index, expected_value, float(point.co.y)))
        if handles is not None:
            left, right = handles[index]
            left_ok = all((abs(float(point.handle_left[i]) - _float32(float(left[i]))) <= max(1e-06, abs(float(left[i])) * 2e-06) for i in range(2)))
            right_ok = all((abs(float(point.handle_right[i]) - _float32(float(right[i]))) <= max(1e-06, abs(float(right[i])) * 2e-06) for i in range(2)))
            if not (left_ok and right_ok):
                raise Failure('VALIDATION_FAILED', 'FCurve writer changed FREE handles: %s index=%d' % (curve.data_path, index))

def _store_curve(bag, path, index, times, values, interpolation, handles=None):
    if len(times) != len(values):
        raise Failure('VALIDATION_FAILED', 'FCurve sample count mismatch: ' + path)
    if any((float(times[i]) >= float(times[i + 1]) for i in range(len(times) - 1))):
        raise Failure('VALIDATION_FAILED', 'FCurve sample times are not strictly increasing: ' + path)
    if not all((math.isfinite(float(value)) for value in times)):
        raise Failure('VALIDATION_FAILED', 'Nonfinite FCurve sample time: ' + path)
    if not all((math.isfinite(float(value)) for value in values)):
        raise Failure('VALIDATION_FAILED', 'Nonfinite FCurve sample value: ' + path)
    if handles is not None:
        if not all((math.isfinite(float(coordinate)) for pair in handles for point in pair for coordinate in point)):
            raise Failure('VALIDATION_FAILED', 'Nonfinite FCurve handle: ' + path)
    curve = bag.fcurves.new(data_path=path, index=index)
    curve.extrapolation = 'CONSTANT'
    curve.keyframe_points.add(len(times))
    curve.keyframe_points.foreach_set('co', [coordinate for frame, value in zip(times, values) for coordinate in (frame, value)])
    for point_index, point in enumerate(curve.keyframe_points):
        point.interpolation = interpolation
        if handles is not None:
            point.handle_left_type = 'FREE'
            point.handle_right_type = 'FREE'
            point.handle_left = handles[point_index][0]
            point.handle_right = handles[point_index][1]
    _verify_curve_storage(curve, times, values, handles)
    return curve

def _curve(bag, path, index, times, values, interpolation='LINEAR'):
    return _store_curve(bag, path, index, times, values, interpolation)

def _dedupe_times(times, tolerance=1e-05):
    ordered = sorted((float(value) for value in times))
    result = []
    for value in ordered:
        if not result or value - result[-1] > tolerance:
            result.append(value)
    return result

def _key_value_curve(bag, path, times, values):
    return _store_curve(bag, path, 0, times, values, 'LINEAR')

def _location_curve(bag, path, index, times, values, left_slopes, right_slopes):
    count = len(times)
    handles = []
    for key_index in range(count):
        left_width = times[key_index] - times[key_index - 1] if key_index > 0 else times[1] - times[0] if count > 1 else 0.0
        right_width = times[key_index + 1] - times[key_index] if key_index + 1 < count else left_width
        handles.append(((times[key_index] - left_width / 3.0, values[key_index] - left_slopes[key_index] * left_width / 3.0), (times[key_index] + right_width / 3.0, values[key_index] + right_slopes[key_index] * right_width / 3.0)))
    return _store_curve(bag, path, index, times, values, 'BEZIER', handles)

def _build_armature(joints, centroids):
    armature = bpy.data.armatures.new('SharedContinuousArmature')
    rig = bpy.data.objects.new('SharedContinuousRig', armature)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    for index, (source_bone, segment) in enumerate(joints):
        centroid = Vector(centroids[index].tolist())
        parent = armature.edit_bones.new(f'A{index:04d}')
        parent.head = centroid
        parent.tail = centroid + Vector((0.0, 0.01, 0.0))
        parent.use_deform = False
        parent['source_bone'] = source_bone
        parent['segment'] = int(segment)
        child = armature.edit_bones.new(f'D{index:04d}')
        child.head = centroid
        child.tail = centroid + Vector((0.0, 0.01, 0.0))
        child.parent = parent
        child.use_connect = False
        child.use_deform = True
        child['source_bone'] = source_bone
        child['segment'] = int(segment)
    bpy.ops.object.mode_set(mode='OBJECT')
    for pose_bone in rig.pose.bones:
        pose_bone.rotation_mode = 'QUATERNION'
    rig['adapter'] = ADAPTER
    rig['affine_joints'] = len(joints)
    return rig

def _build_mesh(mesh_data, rig, joints, index):
    mesh = bpy.data.meshes.new(f"SharedMesh_{index:02d}_{mesh_data['name']}")
    mesh.from_pydata(mesh_data['basis_world'].tolist(), mesh_data['edges'], mesh_data['faces'])
    mesh.update()
    for layer in mesh_data['uv_layers']:
        target = mesh.uv_layers.new(name=layer['name'])
        target.data.foreach_set('uv', layer['uv'])
    body = bpy.data.objects.new(mesh_data['name'], mesh)
    bpy.context.scene.collection.objects.link(body)
    joint_index = {joint: number for number, joint in enumerate(joints)}
    used = sorted({joint_index[joint] for row in mesh_data['mapping'] for joint, _ in row})
    groups = {joint: body.vertex_groups.new(name=f'D{joint:04d}') for joint in used}
    for vertex, row in enumerate(mesh_data['mapping']):
        for joint, weight in row:
            groups[joint_index[joint]].add([vertex], weight, 'REPLACE')
    mask_groups = {}
    for key in mesh_data['shape_keys']:
        name = key['vertex_group']
        if not name or name in mask_groups:
            continue
        target_name = name
        if any((group.name == target_name for group in body.vertex_groups)):
            target_name = 'mask__' + name
        while any((group.name == target_name for group in body.vertex_groups)):
            target_name = '_' + target_name
        group = body.vertex_groups.new(name=target_name)
        for vertex, weight in mesh_data['source_groups'].get(name, []):
            group.add([vertex], weight, 'REPLACE')
        mask_groups[name] = target_name
    if mesh_data['shape_keys']:
        basis = body.shape_key_add(name='Basis', from_mix=False)
        basis.data.foreach_set('co', mesh_data['basis_world'].reshape(-1).tolist())
        created = {'Basis': basis}
        for key in mesh_data['shape_keys']:
            if key['name'] == 'Basis':
                continue
            block = body.shape_key_add(name=key['name'], from_mix=False)
            block.data.foreach_set('co', np.asarray([list(mesh_data['mesh_world'] @ Vector(point.tolist())) for point in key['coords']], dtype=np.float64).reshape(-1).tolist())
            block.slider_min = key['slider_min']
            block.slider_max = key['slider_max']
            block.value = key['value']
            block.mute = key['mute']
            block.lock_shape = key['lock_shape']
            if key['vertex_group'] in mask_groups:
                block.vertex_group = mask_groups[key['vertex_group']]
            created[key['name']] = block
        for key in mesh_data['shape_keys']:
            if key['name'] == 'Basis':
                continue
            created[key['name']].relative_key = created[key['relative_key']]
    body.parent = rig
    body.matrix_parent_inverse = Matrix.Identity(4)
    body.matrix_world = Matrix.Identity(4)
    modifier = body.modifiers.new('SharedContinuousLBS', 'ARMATURE')
    modifier.object = rig
    modifier.use_vertex_groups = True
    modifier.use_bone_envelopes = False
    modifier.use_deform_preserve_volume = False
    attribute = mesh.attributes.new('_C94_SOURCE_VERTEX', 'INT', 'POINT')
    attribute.data.foreach_set('value', list(range(mesh_data['vertices'])))
    body['continuous_source_mesh'] = mesh_data['name']
    body['continuous_source_data'] = mesh_data['data_name']
    body['continuous_source_bone_mapping'] = _sha256_json([[joint, round(weight, 12)] for row in mesh_data['mapping'] for joint, weight in row])
    body['continuous_key_mode'] = 'sampled_native_driver_values' if mesh_data['shape_keys'] else 'no_shape_keys'
    body['continuous_omitted_post_modifiers'] = json.dumps(mesh_data['post_report'], ensure_ascii=False, separators=(',', ':'))
    return (body, mask_groups)

def _build_candidate(native_capture, static, spec, sampling, breakpoints):
    scene = bpy.context.scene
    scene.name = 'SharedContinuousExchange'
    scene.render.fps = int(native_capture.get('fps', 24))
    scene.render.fps_base = 1.0
    scene.frame_start = int(round(float(spec['frame_start'])))
    scene.frame_end = int(round(float(spec['frame_end'])))
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 1.0
    joints = native_capture['joints']
    rig = _build_armature(joints, native_capture['centroids'])
    meshes = {}
    for index, (name, mesh_data) in enumerate(static.items()):
        body, mask_groups = _build_mesh(mesh_data, rig, joints, index)
        meshes[name] = {'object': body, 'mask_groups': mask_groups, 'shape_keys': [key['name'] for key in mesh_data['shape_keys']]}
    key_times = native_capture['key_times']
    witness = native_capture['witness']
    matrices = native_capture['matrices']
    linear_by_key = [np.stack([matrices[frame][index] for index in range(len(joints))]) for frame in key_times]
    locations, rotations_a, scales, rotations_d = _select_factor_path(linear_by_key, native_capture['centroids'], joints)
    left_slopes, right_slopes = _hermite_slopes(key_times, locations, breakpoints)
    predicted = _interpolate_factors(key_times, locations, rotations_a, scales, rotations_d, left_slopes, right_slopes, native_capture['centroids'], witness)
    action, bag = _create_action(rig, 'SharedContinuousBoneAction', 'OBJECT')
    for joint_index in range(len(joints)):
        a_path = rig.pose.bones[f'A{joint_index:04d}'].path_from_id('location')
        d_path = rig.pose.bones[f'D{joint_index:04d}'].path_from_id('rotation_quaternion')
        a_rotation_path = rig.pose.bones[f'A{joint_index:04d}'].path_from_id('rotation_quaternion')
        a_scale_path = rig.pose.bones[f'A{joint_index:04d}'].path_from_id('scale')
        for component in range(3):
            _location_curve(bag, a_path, component, key_times, locations[:, joint_index, component].tolist(), left_slopes[:, joint_index, component].tolist(), right_slopes[:, joint_index, component].tolist())
        for component in range(4):
            _curve(bag, a_rotation_path, component, key_times, rotations_a[:, joint_index, component].tolist())
        for component in range(3):
            _curve(bag, a_scale_path, component, key_times, scales[:, joint_index, component].tolist())
        for component in range(4):
            _curve(bag, d_path, component, key_times, rotations_d[:, joint_index, component].tolist())
    key_actions = {}
    shape_times = [float(value) for value in key_times]
    native_keys = _native_key_map(native_capture)
    for name, mesh_info in meshes.items():
        mesh_data = static[name]
        if not mesh_data['shape_keys']:
            continue
        key_owner = mesh_info['object'].data.shape_keys
        key_action, key_bag = _create_action(key_owner, f'SharedContinuousKeyAction_{name}', 'KEY')
        key_actions[name] = key_action.name
        for key in mesh_data['shape_keys']:
            if key['name'] == 'Basis':
                continue
            values = [float(native_keys[frame][name].get(key['name'], 0.0)) for frame in shape_times]
            _key_value_curve(key_bag, f'''key_blocks["{key['name']}"].value''', shape_times, values)
    scene.frame_set(scene.frame_start)
    return {'rig': rig, 'meshes': meshes, 'key_actions': key_actions, 'action': action.name, 'predicted': predicted, 'locations': locations, 'rotations_a': rotations_a, 'scales': scales, 'rotations_d': rotations_d}

def _structure(rig, meshes, joints, expected_keys):
    if len([obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE']) != 1:
        raise Failure('VALIDATION_FAILED', 'Shared exchange must reopen with exactly one armature')
    if len(rig.data.bones) != 2 * len(joints):
        raise Failure('VALIDATION_FAILED', 'Shared skeleton bone count differs from affine joints')
    rows = []
    for name, info in meshes.items():
        body = bpy.data.objects.get(name)
        if not body or body.type != 'MESH':
            raise Failure('VALIDATION_FAILED', 'Shared exchange mesh missing: ' + name)
        modifiers = [modifier for modifier in body.modifiers if modifier.type == 'ARMATURE']
        if len(modifiers) != 1 or modifiers[0].object != rig:
            raise Failure('VALIDATION_FAILED', 'Mesh is not bound to the one shared armature: ' + name)
        keys = [block.name for block in body.data.shape_keys.key_blocks] if body.data.shape_keys else []
        if keys != expected_keys.get(name, []):
            raise Failure('VALIDATION_FAILED', 'Named Key inventory differs: ' + name)
        weighted = 0
        for vertex in body.data.vertices:
            if any((entry.weight > 0 and body.vertex_groups[entry.group].name in rig.data.bones for entry in vertex.groups)):
                weighted += 1
        if weighted != len(body.data.vertices):
            raise Failure('VALIDATION_FAILED', 'Shared skin has unweighted vertices: ' + name)
        rows.append({'mesh': name, 'vertices': len(body.data.vertices), 'shape_keys': keys, 'rig': rig.name, 'bones': len(rig.data.bones), 'all_vertices_weighted': True})
    return rows

def _verify_native(candidate, native_capture, static, spec, key_times, frames=None, job=None):
    from . import format_io as exchange
    scene = bpy.context.scene
    rig = candidate['rig']
    meshes = candidate['meshes']
    witness = frames if frames is not None else native_capture['witness']
    native_times = {row['frame']: row for row in native_capture['native']['times']}
    checks = []
    matrix_checks = []
    key_checks = []
    for frame in witness:
        _set_frame(scene, frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for name, info in meshes.items():
            observed = _eval_snapshot(info['object'], depsgraph)
            native_row = native_times[frame]['meshes'][name]
            native = native_row['snapshot']
            check = exchange.compare_geometry(native, observed)
            indexed = float(np.linalg.norm(np.asarray(observed['vertices'], dtype=np.float64) - np.asarray(native['vertices'], dtype=np.float64), axis=1).max(initial=0.0))
            check.update(frame=frame, mesh=name, indexed_error_meters=indexed, is_key=frame in key_times, declared_position_tolerance=float(spec['rig_sampling']['max_position_error']))
            declared = float(spec['rig_sampling']['max_position_error'])
            check['declared_bidirectional_pass'] = check['bidirectional_vertex_distance'] <= declared
            check['declared_indexed_pass'] = indexed <= declared
            check['declared_position_pass'] = bool(check['declared_bidirectional_pass'] and check['declared_indexed_pass'])
            check['ok'] = bool(check['ok'] and check['declared_position_pass'])
            if frame in key_times:
                check['key_guard'] = indexed <= KEY_GUARD
                check['ok'] = bool(check['ok'] and check['key_guard'])
            checks.append(check)
            if static[name]['shape_keys']:
                observed_keys = {block.name: float(block.value) for block in info['object'].data.shape_keys.key_blocks}
                native_values = native_row['key_values']
                key_error = max((abs(observed_keys.get(key, 0.0) - native_values.get(key, 0.0)) for key in native_values), default=0.0)
                key_checks.append({'frame': frame, 'mesh': name, 'max_key_value_error': key_error, 'ok': key_error <= 1e-05})
        if frame in key_times:
            evaluated = rig.evaluated_get(depsgraph)
            output_matrices = []
            for joint_index in range(len(native_capture['joints'])):
                pose_bone = evaluated.pose.bones[f'D{joint_index:04d}']
                deformation = pose_bone.matrix @ pose_bone.bone.matrix_local.inverted()
                output_matrices.append(_np_matrix(deformation))
            output_matrices = np.stack(output_matrices)
            native_matrices = native_capture['matrices'][frame]
            delta = np.abs(output_matrices - native_matrices)
            error = float(delta.max(initial=0.0))
            worst_joint = int(delta.reshape(len(delta), -1).max(axis=1).argmax())
            a_bone = evaluated.pose.bones[f'A{worst_joint:04d}']
            d_bone = evaluated.pose.bones[f'D{worst_joint:04d}']
            matrix_checks.append({'frame': frame, 'max_joint_matrix_error': error, 'ok': error <= KEY_GUARD, 'worst_joint': worst_joint, 'native_matrix': native_matrices[worst_joint].tolist(), 'output_matrix': output_matrices[worst_joint].tolist(), 'A_location': list(a_bone.location), 'A_rotation_quaternion': list(a_bone.rotation_quaternion), 'A_scale': list(a_bone.scale), 'D_rotation_quaternion': list(d_bone.rotation_quaternion), 'A_deformation': [list(row) for row in a_bone.matrix @ a_bone.bone.matrix_local.inverted()], 'D_deformation': [list(row) for row in d_bone.matrix @ d_bone.bone.matrix_local.inverted()]})
    if not all((check['ok'] for check in checks)):
        if job:
            animation = rig.animation_data
            bone_rows = []
            for joint_index in range(min(3, len(native_capture['joints']))):
                a = rig.pose.bones[f'A{joint_index:04d}']
                d = rig.pose.bones[f'D{joint_index:04d}']
                bone_rows.append({'joint': joint_index, 'A_location': list(a.location), 'A_rotation_quaternion': list(a.rotation_quaternion), 'A_scale': list(a.scale), 'D_rotation_quaternion': list(d.rotation_quaternion)})
            atomic_json(Path(job) / 'continuous-native-failure.json', {'checks': checks, 'summary': _summary_checks(checks), 'matrix_checks': matrix_checks, 'key_checks': key_checks, 'first_failures': [check for check in checks if not check['ok']][:12], 'animation': {'action': animation.action.name if animation and animation.action else None, 'action_slot': animation.action_slot.name_display if animation and animation.action_slot else None, 'fcurves': sum((len(strip.channelbag(slot).fcurves) for layer in (animation.action.layers if animation and animation.action else []) for strip in layer.strips for slot in (animation.action.slots if animation and animation.action else []) if strip.channelbag(slot))), 'pose_rows': bone_rows}})
        raise Failure('VALIDATION_FAILED', 'Native shared candidate differs from source Mesh')
    if key_checks and (not all((check['ok'] for check in key_checks))):
        if job:
            atomic_json(Path(job) / 'continuous-native-failure.json', {'key_checks': key_checks})
        raise Failure('VALIDATION_FAILED', 'Native named Key values differ from source')
    if not all((check['ok'] for check in matrix_checks)):
        if job:
            atomic_json(Path(job) / 'continuous-native-failure.json', {'matrix_checks': matrix_checks})
        raise Failure('VALIDATION_FAILED', 'Native shared joint matrices differ from source')
    return (checks, matrix_checks, key_checks)

def _refine_keys(native_capture, static, sampling, breakpoints, job):
    seed_substeps = int(sampling['seed_substeps'])
    max_depth = int(sampling['max_depth'])
    target = float(sampling['max_position_error'])
    key_times, _seed_identity = _float32_times(_seed_keys(breakpoints, float(native_capture['native']['clock']['frame_start']), float(native_capture['native']['clock']['frame_end']), seed_substeps))
    witness = native_capture['witness']
    key_names_by_mesh = {name: [key['name'] for key in mesh_data['shape_keys']] for name, mesh_data in static.items() if mesh_data['shape_keys']}
    history = []
    for depth in range(max_depth + 1):
        linear_by_key = [np.stack([native_capture['matrices'][frame][index] for index in range(len(native_capture['joints']))]) for frame in key_times]
        locations, rotations_a, scales, rotations_d = _select_factor_path(linear_by_key, native_capture['centroids'], native_capture['joints'])
        left_slopes, right_slopes = _hermite_slopes(key_times, locations, breakpoints)
        predicted = _interpolate_factors(key_times, locations, rotations_a, scales, rotations_d, left_slopes, right_slopes, native_capture['centroids'], witness)
        key_samples = {name: np.asarray([[float(native_capture['key_values'][frame][name].get(key_name, 0.0)) for frame in key_times] for key_name in names], dtype=np.float64) for name, names in key_names_by_mesh.items()}
        cell_errors = [{'cell': index, 'worst_frame': None, 'worst_mesh': None, 'max_error': 0.0, 'key_worst_frame': None, 'key_worst_mesh': None, 'max_key_error': 0.0} for index in range(max(0, len(key_times) - 1))]
        key_errors = {}
        for frame in witness:
            cell = max(0, min(len(key_times) - 2, int(np.searchsorted(key_times, frame, side='right') - 1)))
            for name, mesh_data in static.items():
                points = _reconstruct(native_capture['pre_world'][frame][name], mesh_data['influences'], predicted[frame])
                native_points = np.asarray(native_capture['native']['times'][witness.index(frame)]['meshes'][name]['snapshot']['vertices'], dtype=np.float64)
                error = float(np.linalg.norm(points - native_points, axis=1).max(initial=0.0))
                if error > cell_errors[cell]['max_error']:
                    cell_errors[cell].update(worst_frame=frame, worst_mesh=name, max_error=error)
                if name in key_samples and frame not in key_times:
                    hi = int(np.searchsorted(key_times, frame, side='right'))
                    lo = max(0, hi - 1)
                    hi = min(hi, len(key_times) - 1)
                    width = key_times[hi] - key_times[lo]
                    alpha = 0.0 if width <= 0.0 else (frame - key_times[lo]) / width
                    interpolated = key_samples[name][:, lo] * (1.0 - alpha) + key_samples[name][:, hi] * alpha
                    native_values = np.asarray([float(native_capture['key_values'][frame][name].get(key_name, 0.0)) for key_name in key_names_by_mesh[name]], dtype=np.float64)
                    key_error = float(np.abs(interpolated - native_values).max(initial=0.0))
                    if key_error > cell_errors[cell]['max_key_error']:
                        cell_errors[cell].update(key_worst_frame=frame, key_worst_mesh=name, max_key_error=key_error)
                if frame in key_times:
                    key_errors[frame] = max(key_errors.get(frame, 0.0), error)
        failing = [row for row in cell_errors if row['max_error'] > target or row['max_key_error'] > KEY_GUARD]
        key_failures = [{'frame': frame, 'max_error': error} for frame, error in sorted(key_errors.items()) if error > KEY_GUARD]
        history.append({'depth': depth, 'keys': len(key_times), 'max_error': max((row['max_error'] for row in cell_errors), default=0.0), 'max_key_error': max((row['max_key_error'] for row in cell_errors), default=0.0), 'failing_cells': len(failing), 'key_failing_cells': sum((1 for row in cell_errors if row['max_key_error'] > KEY_GUARD)), 'key_failures': key_failures, 'target': target, 'key_target': KEY_GUARD})
        atomic_json(job / 'continuous-refinement.json', {'adapter': ADAPTER, 'target': target, 'max_depth': max_depth, 'history': history, 'key_times': key_times, 'cell_errors': cell_errors})
        if key_failures:
            raise Failure('UNSUPPORTED', 'Shared factor representation cannot meet the key guard at an evaluated key time')
        if not failing:
            native_capture['key_times'] = key_times
            native_capture['refinement'] = {'depth': depth, 'keys': len(key_times), 'target': target, 'key_target': KEY_GUARD, 'history': history}
            return key_times
        if depth >= max_depth:
            raise Failure('UNSUPPORTED', 'Shared continuous refinement exhausted max_depth before the position/Key targets')
        additions = {row['worst_frame'] for row in failing if row['max_error'] > target and row['worst_frame'] is not None}
        additions |= {row['key_worst_frame'] for row in failing if row['max_key_error'] > KEY_GUARD and row['key_worst_frame'] is not None}
        additions = sorted(additions)
        if not additions:
            raise Failure('UNSUPPORTED', 'Shared continuous refinement has failing cells without a new witness')
        key_times, _added_identity = _float32_times(sorted(set(key_times) | {float(frame) for frame in additions}))
    raise Failure('UNSUPPORTED', 'Shared continuous refinement did not terminate')

def _load_glb(path):
    raw = Path(path).read_bytes()
    magic, version, total = struct.unpack_from('<4sII', raw, 0)
    if magic != b'glTF' or version != 2 or total != len(raw):
        raise Failure('INVALID_REQUEST', 'Invalid GLB container: ' + str(path))
    json_length, json_type = struct.unpack_from('<I4s', raw, 12)
    if json_type != b'JSON':
        raise Failure('INVALID_REQUEST', 'GLB JSON chunk missing: ' + str(path))
    doc = json.loads(raw[20:20 + json_length])
    binary_header = 20 + json_length
    binary_length, binary_type = struct.unpack_from('<I4s', raw, binary_header)
    if binary_type != b'BIN\x00':
        raise Failure('INVALID_REQUEST', 'GLB BIN chunk missing: ' + str(path))
    binary = bytearray(raw[binary_header + 8:binary_header + 8 + binary_length])
    return (doc, binary)

def _write_glb(path, doc, binary):
    doc['buffers'][0]['byteLength'] = len(binary)
    payload = json.dumps(doc, separators=(',', ':'), allow_nan=False).encode('utf8')
    payload += b' ' * (-len(payload) % 4)
    while len(binary) % 4:
        binary.append(0)
    total = 28 + len(payload) + len(binary)
    with open(path, 'xb') as stream:
        stream.write(struct.pack('<4sII', b'glTF', 2, total))
        stream.write(struct.pack('<I4s', len(payload), b'JSON'))
        stream.write(payload)
        stream.write(struct.pack('<I4s', len(binary), b'BIN\x00'))
        stream.write(binary)
_COMPONENT = {5120: ('b', 1), 5121: ('B', 1), 5122: ('h', 2), 5123: ('H', 2), 5125: ('I', 4), 5126: ('f', 4)}
_WIDTH = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}

def _read_accessor(doc, binary, index):
    accessor = doc['accessors'][index]
    view = doc['bufferViews'][accessor['bufferView']]
    if 'sparse' in accessor:
        raise Failure('UNSUPPORTED', 'Sparse accessor outside shared GLB compatibility path')
    fmt, size = _COMPONENT[accessor['componentType']]
    width = _WIDTH[accessor['type']]
    offset = view.get('byteOffset', 0) + accessor.get('byteOffset', 0)
    stride = view.get('byteStride', size * width)
    rows = []
    for item in range(accessor['count']):
        values = struct.unpack_from('<' + fmt * width, binary, offset + item * stride)
        rows.append(list(values))
    return rows

def _append_accessor(doc, binary, rows, component, type_name):
    fmt = _COMPONENT[component][0]
    width = _WIDTH[type_name]
    flat = [value for row in rows for value in (row if isinstance(row, (list, tuple)) else [row])]
    if len(flat) != len(rows) * width:
        raise Failure('VALIDATION_FAILED', 'Accessor row width mismatch')
    while len(binary) % 4:
        binary.append(0)
    offset = len(binary)
    payload = struct.pack('<' + fmt * len(flat), *flat)
    binary.extend(payload)
    view_index = len(doc['bufferViews'])
    doc['bufferViews'].append({'buffer': 0, 'byteOffset': offset, 'byteLength': len(payload), 'target': 34962})
    accessor_index = len(doc['accessors'])
    accessor = {'bufferView': view_index, 'componentType': component, 'count': len(rows), 'type': type_name}
    if type_name == 'SCALAR':
        accessor['min'] = [min(flat)] if flat else [0.0]
        accessor['max'] = [max(flat)] if flat else [0.0]
    doc['accessors'].append(accessor)
    return accessor_index

def _glb_mesh_name_map(doc):
    mapping = {}
    for node in doc.get('nodes', []):
        if 'mesh' not in node:
            continue
        mesh = doc['meshes'][node['mesh']]
        mapping[(node.get('name') or '').lower()] = node
        mapping[(mesh.get('name') or '').lower()] = node
    return mapping

def _glb_backfill_weights(source_path, output_path, tables):
    import copy

    doc, binary = _load_glb(source_path)
    old_doc = copy.deepcopy(doc)
    original_binary = bytes(binary)
    name_map = _glb_mesh_name_map(doc)
    repaired = []
    for node in doc.get("nodes", []):
        if "mesh" not in node or "skin" not in node:
            continue
        node_alias = (node.get("name") or "").casefold()
        mesh_alias = (doc["meshes"][node["mesh"]].get("name") or "").casefold()
        node_entry = tables.get(node_alias)
        mesh_entry = tables.get(mesh_alias)
        if (
            node_entry is not None
            and mesh_entry is not None
            and node_entry["source"] != mesh_entry["source"]
        ):
            raise Failure(
                "UNSUPPORTED",
                "GLB weight aliases disagree for one skin node: "
                + str(node.get("name"))
                + " vs "
                + str(doc["meshes"][node["mesh"]].get("name")),
            )
        entry = node_entry or mesh_entry
        if entry is None:
            raise Failure(
                "VALIDATION_FAILED",
                "Unmapped GLB skin node while restoring weights: " + str(node.get("name")),
            )
        table = entry["rows"]
        skin = doc["skins"][node["skin"]]
        joint_names = [doc["nodes"][index].get("name") for index in skin["joints"]]
        joint_index = {}
        for index, name in enumerate(joint_names):
            if name in joint_index:
                raise Failure("UNSUPPORTED", "Ambiguous duplicate GLB joint name: " + str(name))
            joint_index[name] = index
        primitives = doc["meshes"][node["mesh"]]["primitives"]
        primitive_rows = []
        union_ids = set()
        for primitive in primitives:
            attributes = primitive["attributes"]
            if "_C94_SOURCE_VERTEX" not in attributes:
                raise Failure("VALIDATION_FAILED", "GLB source vertex ID attribute missing")
            source_ids = _read_accessor(doc, binary, attributes["_C94_SOURCE_VERTEX"])
            rows = []
            for row in source_ids:
                source_id = int(row[0])
                if row[0] != source_id or not 0 <= source_id < len(table):
                    raise Failure("VALIDATION_FAILED", "Invalid GLB source vertex ID")
                union_ids.add(source_id)
                influences = table[source_id]
                total = sum(weight for _, weight in influences)
                if total <= 0.0:
                    raise Failure("VALIDATION_FAILED", "GLB source influence row has zero mass")
                mapped = []
                for name, weight in influences:
                    if name not in joint_index:
                        raise Failure("VALIDATION_FAILED", "GLB weight joint missing: " + name)
                    mapped.append((joint_index[name], weight / total))
                rows.append(mapped)
            primitive_rows.append((primitive, attributes, rows))
        # The bundled glTF exporter may split one skinned mesh into several
        # primitives (for example a triangle primitive plus a loose-vertex
        # primitive).  The complete source identity is the union across the
        # whole mesh, not one primitive's subset; each primitive is still
        # restored from its own exact source-ID rows.
        if union_ids != set(range(len(table))):
            raise Failure(
                "VALIDATION_FAILED",
                "GLB source vertex IDs do not cover one weight table: "
                + entry["source"]
                + " missing="
                + repr(sorted(set(range(len(table))) - union_ids)[:20]),
            )
        repaired_groups = 0
        repaired_vertices = 0
        for primitive, attributes, rows in primitive_rows:
            groups = (max((len(row) for row in rows), default=0) + 3) // 4
            for key in list(attributes):
                if key.startswith(("JOINTS_", "WEIGHTS_")):
                    del attributes[key]
            for group in range(groups):
                joint_rows = []
                weight_rows = []
                for row in rows:
                    subset = row[group * 4 : (group + 1) * 4]
                    joint_rows.append([item[0] for item in subset] + [0] * (4 - len(subset)))
                    weight_rows.append([item[1] for item in subset] + [0.0] * (4 - len(subset)))
                attributes[f"JOINTS_{group}"] = _append_accessor(
                    doc, binary, joint_rows, 5123, "VEC4"
                )
                attributes[f"WEIGHTS_{group}"] = _append_accessor(
                    doc, binary, weight_rows, 5126, "VEC4"
                )
            repaired_groups += groups
            repaired_vertices += len(rows)
        repaired.append(
            {
                "node": node.get("name"),
                "primitives": len(primitives),
                "vertices_with_splits": repaired_vertices,
                "influence_sets": repaired_groups,
                "source_ids_exact": True,
            }
        )
    if doc.get("skins") != old_doc.get("skins"):
        raise Failure("VALIDATION_FAILED", "GLB skin structure changed during weight restoration")
    if doc.get("animations") != old_doc.get("animations"):
        raise Failure("VALIDATION_FAILED", "GLB animation payload changed during weight restoration")
    _write_glb(output_path, doc, binary)
    return {"repaired": repaired}

def _register_weight_alias(tables, owners, alias, source, rows):
    """Register one case-insensitive GLB weight-table alias for one source mesh."""
    key = str(alias).casefold()
    owner = owners.get(key)
    if owner is not None and owner != source:
        raise Failure('UNSUPPORTED', 'Cross-mesh GLB weight alias collision: ' + repr(alias) + ' -> ' + owner + ' and ' + source)
    owners[key] = source
    tables[key] = {'source': source, 'rows': rows}

def _weights_target_count(doc, channel):
    node = doc['nodes'][channel['target']['node']]
    mesh = doc['meshes'][node['mesh']]
    return max((len(primitive.get('targets', [])) for primitive in mesh['primitives']), default=0)

def _glb_float32_seconds(frames, fps):
    """Validate the actual FLOAT encoding, before any output or index hook."""
    values = np.asarray(frames, dtype=np.float64)
    if values.ndim != 1 or not len(values) or (not np.isfinite(values).all()) or (not math.isfinite(float(fps))) or (float(fps) <= 0) or np.any(np.diff(values) <= 0):
        raise Failure('UNSUPPORTED', 'GLB requires a finite strictly increasing unified T')
    seconds = (values / float(fps)).astype(np.float32)
    if not np.isfinite(seconds).all() or np.any(np.diff(seconds.astype(np.float64)) <= 0) or np.max(np.abs(seconds.astype(np.float64) * float(fps) - values)) > 1e-05:
        raise Failure('UNSUPPORTED', 'Unified T is not one-to-one representable as GLB float32 seconds')
    return seconds.astype(np.float64).tolist()

def _glb_compat_linear(source_path, output_path, frames, fps):
    import copy
    seconds = [[value] for value in _glb_float32_seconds(frames, fps)]
    doc, binary = _load_glb(source_path)
    old_doc = copy.deepcopy(doc)
    original_binary = bytes(binary)
    time_accessor = _append_accessor(doc, binary, seconds, 5126, 'SCALAR')
    changed = []
    counts = {}
    for animation_index, animation in enumerate(doc.get('animations', [])):
        uses = {}
        for channel in animation['channels']:
            uses.setdefault(channel['sampler'], []).append(channel)
        for sampler_index, sampler in enumerate(animation['samplers']):
            interpolation = sampler.get('interpolation', 'LINEAR')
            if interpolation not in ('CUBICSPLINE', 'LINEAR', 'STEP'):
                raise Failure('UNSUPPORTED', 'Unsupported GLB animation interpolation: ' + interpolation)
            channels = uses.get(sampler_index, [])
            paths = {channel['target']['path'] for channel in channels}
            if len(paths) != 1:
                raise Failure('UNSUPPORTED', 'Animation sampler has mixed targets')
            path = next(iter(paths))
            old_times = [row[0] for row in _read_accessor(doc, binary, sampler['input'])]
            values = _read_accessor(doc, binary, sampler['output'])
            accessor_type = doc['accessors'][sampler['output']]['type']
            if path == 'weights':
                components = _weights_target_count(doc, channels[0]) or 1
            else:
                if accessor_type not in ('SCALAR', 'VEC3', 'VEC4'):
                    raise Failure('UNSUPPORTED', 'Unsupported GLB animation accessor: ' + accessor_type)
                components = {'SCALAR': 1, 'VEC3': 3, 'VEC4': 4}[accessor_type]
            per_time_rows = components if path == 'weights' else 1
            expected_rows = (3 if interpolation == 'CUBICSPLINE' else 1) * len(old_times) * per_time_rows
            if len(values) != expected_rows:
                raise Failure('UNSUPPORTED', 'GLB animation payload length mismatch')
            if interpolation == 'CUBICSPLINE':
                tensor = np.asarray(values, dtype=np.float64).reshape(len(old_times), 3, components)
            else:
                tensor = np.asarray(values, dtype=np.float64).reshape(len(old_times), components)
            output = []
            old_times_np = np.asarray(old_times, dtype=np.float64)
            for frame in frames:
                t = float(frame) / float(fps)
                if t <= old_times_np[0]:
                    evaluated = tensor[0, 1, :] if interpolation == 'CUBICSPLINE' else tensor[0]
                elif t >= old_times_np[-1]:
                    evaluated = tensor[-1, 1, :] if interpolation == 'CUBICSPLINE' else tensor[-1]
                else:
                    index = int(np.searchsorted(old_times_np, t, side='right') - 1)
                    dt = old_times_np[index + 1] - old_times_np[index]
                    alpha = (t - old_times_np[index]) / dt
                    if interpolation == 'CUBICSPLINE':
                        h00 = 2 * alpha ** 3 - 3 * alpha ** 2 + 1
                        h10 = alpha ** 3 - 2 * alpha ** 2 + alpha
                        h01 = -2 * alpha ** 3 + 3 * alpha ** 2
                        h11 = alpha ** 3 - alpha ** 2
                        evaluated = h00 * tensor[index, 1, :] + h10 * dt * tensor[index, 2, :] + h01 * tensor[index + 1, 1, :] + h11 * dt * tensor[index + 1, 0, :]
                    elif interpolation == 'STEP':
                        evaluated = tensor[index]
                    else:
                        evaluated = (1.0 - alpha) * tensor[index] + alpha * tensor[index + 1]
                output.extend((float(value) for value in evaluated))
            if path == 'weights':
                rows = [[value] for value in output]
                output_type = 'SCALAR'
            elif accessor_type == 'VEC4':
                rows = [output[index:index + 4] for index in range(0, len(output), 4)]
                output_type = 'VEC4'
            elif accessor_type == 'VEC3':
                rows = [output[index:index + 3] for index in range(0, len(output), 3)]
                output_type = 'VEC3'
            else:
                rows = [[value] for value in output]
                output_type = 'SCALAR'
            output_accessor = _append_accessor(doc, binary, rows, 5126, output_type)
            sampler.update(input=time_accessor, output=output_accessor, interpolation='LINEAR')
            changed.append([animation_index, sampler_index, path])
            counts[path] = counts.get(path, 0) + 1
    protected = ('nodes', 'meshes', 'skins', 'materials', 'scenes', 'scene', 'extensions', 'extensionsUsed', 'extensionsRequired')
    if any((doc.get(key) != old_doc.get(key) for key in protected)):
        raise Failure('VALIDATION_FAILED', 'Protected GLB structure changed in compatibility copy')
    for animation_index, animation in enumerate(doc.get('animations', [])):
        if animation['channels'] != old_doc['animations'][animation_index]['channels']:
            raise Failure('VALIDATION_FAILED', 'GLB animation targets changed in compatibility copy')
        changed_keys = {(a, s) for a, s, _ in changed if a == animation_index}
        for sampler_index, sampler in enumerate(animation['samplers']):
            if (animation_index, sampler_index) not in changed_keys and sampler != old_doc['animations'][animation_index]['samplers'][sampler_index]:
                raise Failure('VALIDATION_FAILED', 'Non-target GLB sampler changed')
    if bytes(binary[:len(original_binary)]) != original_binary:
        raise Failure('VALIDATION_FAILED', 'Original GLB binary prefix changed')
    _write_glb(output_path, doc, binary)
    return {'converted': len(changed), 'counts': counts, 'time_count': len(frames), 'seconds': 'frame/fps; no zero shift'}

def _select_export_objects(rig, meshes):
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    rig.select_set(True)
    for info in meshes.values():
        info['object'].select_set(True)
    bpy.context.view_layer.objects.active = rig

def _add_source_id_uv(meshes):
    """Independent per-vertex identity channel carried by standard UV layers.

    The glTF path already exports the scalar _C94_SOURCE_VERTEX attribute.  FBX
    has no standard per-vertex integer attribute, so the same source IDs are
    also written as a dedicated UV set (u=id, v=0).  The channel is used only
    for identity recovery after import and is reported explicitly.
    """
    for name, info in meshes.items():
        if info['object'].data.uv_layers.get('_C94_SOURCE_ID') is not None:
            raise Failure('UNSUPPORTED', 'Reserved source-ID UV name already exists: ' + name)
    rows = []
    for name, info in meshes.items():
        obj = info['object']
        layer = obj.data.uv_layers.new(name='_C94_SOURCE_ID')
        values = []
        for loop in obj.data.loops:
            values.extend((float(loop.vertex_index), 0.0))
        layer.data.foreach_set('uv', values)
        rows.append({'mesh': name, 'layer': layer.name, 'vertices': len(obj.data.vertices)})
    return rows

def _export_fbx(path, spec, rig, meshes, bake_step, require_fractional=True):
    from . import format_io as exchange
    from . import io_support as modeling
    _select_export_objects(rig, meshes)
    operator = exchange.operator(exchange.OPS['FBX'][1])
    settings = dict(filepath=str(path), use_selection=True, object_types={'MESH', 'ARMATURE', 'EMPTY'}, bake_anim=True, bake_anim_use_nla_strips=False, bake_anim_use_all_actions=False, bake_anim_simplify_factor=0.0, bake_anim_step=float(bake_step), add_leaf_bones=False, use_mesh_modifiers=False, path_mode='STRIP', embed_textures=False, use_custom_props=False, apply_unit_scale=True, global_scale=1.0, axis_forward='-Z', axis_up='Y')
    modeling.finished(operator(**settings))
    if not path.is_file() or not path.stat().st_size:
        raise Failure('VALIDATION_FAILED', 'FBX exporter produced no file')
    from io_scene_fbx import fbx_utils, parse_fbx
    tree, version = parse_fbx.parse(str(path))
    key_times = set()
    curve_counts = []

    def walk(node):
        if node.id == b'KeyTime':
            raw = [int(value) for value in node.props[0]]
            key_times.update(raw)
            curve_counts.append(len(raw))
        for child in node.elems:
            walk(child)
    walk(tree)
    ktime = fbx_utils.FBX_KTIME
    native_frames = sorted((value / ktime * float(bpy.context.scene.render.fps) for value in key_times))
    fractional = [frame for frame in native_frames if abs(frame - round(frame)) > 1e-06]
    report = {'version': version, 'ktime_per_second': ktime, 'animation_curves': len(curve_counts), 'key_counts': sorted(set(curve_counts)), 'raw_key_times': sorted(key_times), 'native_frames': native_frames, 'fractional_native_frames': fractional, 'duration_seconds': (max(key_times) - min(key_times)) / ktime if key_times else 0.0, 'bake_step': float(bake_step)}
    if require_fractional and (not key_times or not fractional):
        raise Failure('VALIDATION_FAILED', 'FBX did not preserve fractional format keys')
    return report

def _export_glb_standard(path, spec, rig, meshes):
    from . import format_io as exchange
    from . import io_support as modeling
    _select_export_objects(rig, meshes)
    operator = exchange.operator(exchange.OPS['GLB'][1])
    settings = dict(filepath=str(path), export_format='GLB', use_selection=True, export_animations=True, export_animation_mode='ACTIVE_ACTIONS', export_force_sampling=False, export_frame_range=True, export_frame_step=1, export_optimize_animation_size=False, export_anim_slide_to_zero=False, export_skins=True, export_apply=False, export_morph=True, export_all_influences=True, export_attributes=True, export_materials='NONE', export_yup=True, export_extras=False, export_lights=False, export_cameras=False, export_anim_scene_split_object=False, use_mesh_edges=True, use_mesh_vertices=True)
    modeling.finished(operator(**settings))
    if not path.is_file() or not path.stat().st_size:
        raise Failure('VALIDATION_FAILED', 'GLB exporter produced no file')
    return {'settings': {key: value for key, value in settings.items() if key != 'filepath'}}

def _bake_format_actions(rig, meshes, sample_times, key_times=None):
    """Bake the verified candidate onto the uniformly sampled format grid.

    The exported format is verified at these exact frames.  Sampling the
    candidate pose here (rather than relying on glTF-space interpolation of
    sparse keys) keeps the file's key values equal to the candidate values and
    every key is 1/32 frame apart, above the importer's 0.01-frame merge
    threshold.
    """
    scene = bpy.context.scene
    sample_times = [float(value) for value in sample_times]
    frames = list(sample_times) if key_times is None else [float(value) for value in key_times]
    if len(frames) != len(sample_times):
        raise Failure('VALIDATION_FAILED', 'Format bake sample/key count mismatch')
    joint_bones = []
    for bone in rig.pose.bones:
        if bone.name.startswith(('A', 'D')) and bone.name[1:].isdigit():
            joint_bones.append(bone.name)
    joint_bones.sort()
    channels = {name: {'location': [], 'rotation_quaternion': [], 'scale': []} for name in joint_bones}
    key_values = {name: {block.name: [] for block in info['object'].data.shape_keys.key_blocks if block.name != 'Basis'} for name, info in meshes.items() if info['object'].data.shape_keys}
    for frame in sample_times:
        _set_frame(scene, frame)
        for name in joint_bones:
            bone = rig.pose.bones[name]
            channels[name]['location'].append(tuple((float(value) for value in bone.location)))
            channels[name]['rotation_quaternion'].append(tuple((float(value) for value in bone.rotation_quaternion)))
            channels[name]['scale'].append(tuple((float(value) for value in bone.scale)))
        for name in key_values:
            obj = meshes[name]['object']
            for block in obj.data.shape_keys.key_blocks:
                if block.name != 'Basis':
                    key_values[name][block.name].append(float(block.value))
    action, bag = _create_action(rig, 'SharedContinuousFormatBoneAction', 'OBJECT')
    for name in joint_bones:
        bone = rig.pose.bones[name]
        if name.startswith('A'):
            _curve(bag, bone.path_from_id('location'), 0, frames, [row[0] for row in channels[name]['location']])
            _curve(bag, bone.path_from_id('location'), 1, frames, [row[1] for row in channels[name]['location']])
            _curve(bag, bone.path_from_id('location'), 2, frames, [row[2] for row in channels[name]['location']])
        _curve(bag, bone.path_from_id('rotation_quaternion'), 0, frames, [row[0] for row in channels[name]['rotation_quaternion']])
        _curve(bag, bone.path_from_id('rotation_quaternion'), 1, frames, [row[1] for row in channels[name]['rotation_quaternion']])
        _curve(bag, bone.path_from_id('rotation_quaternion'), 2, frames, [row[2] for row in channels[name]['rotation_quaternion']])
        _curve(bag, bone.path_from_id('rotation_quaternion'), 3, frames, [row[3] for row in channels[name]['rotation_quaternion']])
        if name.startswith('A'):
            _curve(bag, bone.path_from_id('scale'), 0, frames, [row[0] for row in channels[name]['scale']])
            _curve(bag, bone.path_from_id('scale'), 1, frames, [row[1] for row in channels[name]['scale']])
            _curve(bag, bone.path_from_id('scale'), 2, frames, [row[2] for row in channels[name]['scale']])
    key_actions = {}
    for name in key_values:
        owner = meshes[name]['object'].data.shape_keys
        key_action, key_bag = _create_action(owner, 'SharedContinuousFormatKeyAction_' + name, 'KEY')
        key_actions[name] = key_action.name
        for key_name, values in key_values[name].items():
            _key_value_curve(key_bag, f'key_blocks["{key_name}"].value', frames, values)
    scene.frame_set(scene.frame_start)
    return {'bone_fcurves': len(bag.fcurves), 'key_actions': key_actions, 'frames': len(frames)}

def _glb_inventory(path):
    doc, _ = _load_glb(path)
    paths = {}
    interpolations = {}
    morph_meshes = []
    for index, mesh in enumerate(doc.get('meshes', [])):
        target_names = []
        for primitive in mesh.get('primitives', []):
            target_names.extend(primitive.get('extras', {}).get('targetNames', []))
        target_names.extend(mesh.get('extras', {}).get('targetNames', []))
        morph_meshes.append({'index': index, 'name': mesh.get('name'), 'targets': max((len(p.get('targets', [])) for p in mesh.get('primitives', [])), default=0), 'target_names': target_names})
    weights_channels = 0
    for animation in doc.get('animations', []):
        for channel in animation.get('channels', []):
            path_name = channel['target']['path']
            paths[path_name] = paths.get(path_name, 0) + 1
            sampler = animation['samplers'][channel['sampler']]
            interpolation = sampler.get('interpolation', 'LINEAR')
            interpolations[interpolation] = interpolations.get(interpolation, 0) + 1
            if path_name == 'weights':
                weights_channels += 1
    return {'animations': len(doc.get('animations', [])), 'skins': len(doc.get('skins', [])), 'skin_joint_counts': [len(skin.get('joints', [])) for skin in doc.get('skins', [])], 'meshes': morph_meshes, 'paths': paths, 'interpolations': interpolations, 'weights_channels': weights_channels}

def _is_hidden_custom_shape(obj):
    custom_shapes = {pose_bone.custom_shape for armature in bpy.context.scene.objects if armature.type == 'ARMATURE' for pose_bone in armature.pose.bones if pose_bone.custom_shape}
    if obj not in custom_shapes:
        return False
    collections = list(obj.users_collection)
    return bool(collections) and all((collection.hide_viewport and collection.hide_render for collection in collections))

def _map_imported_meshes(static):
    scene = bpy.context.scene
    meshes = [obj for obj in scene.objects if obj.type == 'MESH']
    imported = {}
    for name, mesh_data in static.items():
        data_name = mesh_data['data_name']
        predicates = (lambda obj: obj.name == name, lambda obj: obj.data is not None and obj.data.name == data_name, lambda obj: obj.name.startswith(name) or (obj.data is not None and obj.data.name.startswith(data_name)), lambda obj: obj.name.endswith(name) or (obj.data is not None and obj.data.name.endswith(name)), lambda obj: name in obj.name or (obj.data is not None and name in obj.data.name))
        candidates = []
        for predicate in predicates:
            candidates = [obj for obj in meshes if predicate(obj)]
            if candidates:
                break
        if len(candidates) != 1:
            raise Failure('VALIDATION_FAILED', 'Cannot uniquely map imported mesh ' + name + ' candidates=' + repr(sorted((obj.name for obj in candidates))))
        imported[name] = candidates[0]
    if len(set(imported.values())) != len(imported):
        raise Failure('VALIDATION_FAILED', 'Imported meshes are not mapped one-to-one: ' + repr({name: obj.name for name, obj in imported.items()}))
    mapped = set(imported.values())
    extras = [obj for obj in meshes if obj not in mapped]
    excluded = []
    for obj in extras:
        if not _is_hidden_custom_shape(obj):
            raise Failure('VALIDATION_FAILED', 'Unexpected imported extra mesh: ' + obj.name)
        excluded.append(obj.name)
    return (imported, excluded)

def _format_structure(imported, static, expected_bones, expected_keys):
    rigs = []
    for name, obj in imported.items():
        modifiers = [modifier for modifier in obj.modifiers if modifier.type == 'ARMATURE' and modifier.object]
        if len(modifiers) != 1:
            raise Failure('VALIDATION_FAILED', 'Imported mesh lacks one armature skin: ' + name)
        rigs.append(modifiers[0].object)
    unique = []
    for rig in rigs:
        if rig not in unique:
            unique.append(rig)
    if len(unique) != 1:
        raise Failure('VALIDATION_FAILED', 'Imported meshes do not share one armature')
    rig = unique[0]
    if len(rig.data.bones) != expected_bones:
        raise Failure('VALIDATION_FAILED', 'Imported shared skeleton bone count differs: ' + str(len(rig.data.bones)) + ' vs ' + str(expected_bones))
    rows = []
    for name, obj in imported.items():
        keys = [block.name for block in obj.data.shape_keys.key_blocks] if obj.data.shape_keys else []
        if keys != expected_keys.get(name, []):
            raise Failure('VALIDATION_FAILED', 'Imported named Key inventory differs: ' + name + ' ' + repr(keys))
        unweighted = 0
        for vertex in obj.data.vertices:
            if not any((entry.weight > 0 and obj.vertex_groups[entry.group].name in rig.data.bones for entry in vertex.groups)):
                unweighted += 1
        if unweighted:
            raise Failure('VALIDATION_FAILED', 'Imported skin has unweighted vertices: ' + name)
        rows.append({'mesh': name, 'vertices': len(obj.data.vertices), 'shape_keys': keys, 'rig': rig.name, 'bones': len(rig.data.bones), 'all_vertices_weighted': True})
    return (rig, rows)

def _glb_file_time_table(path, fps):
    doc, binary = _load_glb(path)
    if not doc.get('animations'):
        raise Failure('UNSUPPORTED', 'GLB has no animation time table')
    tables = {}
    accessors = set()
    for animation in doc['animations']:
        for sampler in animation['samplers']:
            index = sampler['input']
            accessors.add(index)
            if index not in tables:
                accessor = doc['accessors'][index]
                if accessor['type'] != 'SCALAR' or accessor['componentType'] != 5126:
                    raise Failure('UNSUPPORTED', 'GLB time input must be FLOAT SCALAR')
                tables[index] = [float(row[0]) for row in _read_accessor(doc, binary, index)]
                values = np.asarray(tables[index], dtype=np.float64)
                if not len(values) or not np.isfinite(values).all() or np.any(np.diff(values) <= 0):
                    raise Failure('UNSUPPORTED', 'GLB time input is not finite and strictly increasing')
    distinct = {tuple(rows) for rows in tables.values()}
    if len(distinct) != 1:
        raise Failure('UNSUPPORTED', 'GLB animation samplers do not share one explicit time table')
    raw = next(iter(tables.values()))
    frames = [value * float(fps) for value in raw]
    return (accessors, frames, raw)

def _fbx_file_time_table(path):
    from io_scene_fbx import parse_fbx, fbx_utils
    tree, version = parse_fbx.parse(str(path))
    arrays = []

    def walk(node):
        if node.id == b'KeyTime':
            arrays.append([int(value) for value in node.props[0]])
        for child in node.elems:
            walk(child)
    walk(tree)
    if not arrays:
        raise Failure('UNSUPPORTED', 'FBX has no animation KeyTime table')
    ticks = sorted({tick for rows in arrays for tick in rows})
    clock = {}

    def walk_clock(node, ancestors):
        if node.id == b'P' and len(node.props) >= 2:
            label = node.props[0]
            if label in (b'TimeSpanStart', b'TimeSpanStop') and b'GlobalSettings' in ancestors:
                clock[label.decode()] = int(node.props[-1])
            if label in (b'LocalStart', b'LocalStop', b'ReferenceStart', b'ReferenceStop') and b'AnimationStack' in ancestors:
                clock['AnimationStack.' + label.decode()] = int(node.props[-1])
        if node.id in (b'LocalTime', b'ReferenceTime') and b'Takes' in ancestors:
            key = 'Takes.' + node.id.decode()
            raw = node.props[0]
            if isinstance(raw, (int, float)):
                clock[key] = int(raw)
            else:
                clock[key] = [int(value) for value in raw]
        for child in node.elems:
            walk_clock(child, ancestors + (node.id,))
    walk_clock(tree, ())
    return (version, arrays, ticks, fbx_utils.FBX_KTIME_V7, clock)

def _import_format_index_domain(path, kind, times, fps):
    from . import format_io as exchange
    times = [float(value) for value in times]
    count = len(times)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = int(fps)
    bpy.context.scene.render.fps_base = 1.0
    if kind == 'GLB':
        from io_scene_gltf2.io.imp.gltf2_io_binary import BinaryData
        expected_seconds = _glb_float32_seconds(times, fps)
        accessors, file_frames, raw_times = _glb_file_time_table(path, fps)
        if raw_times != expected_seconds:
            raise Failure('UNSUPPORTED', 'GLB input accessors do not exactly encode complete unified T')
        if len(file_frames) != count:
            raise Failure('VALIDATION_FAILED', 'GLB time table length %d differs from unified T length %d' % (len(file_frames), count))
        time_errors = [abs(file_frames[index] - times[index]) for index in range(count)]
        max_time_error = max(time_errors, default=0.0)
        if max_time_error > 1e-05:
            raise Failure('VALIDATION_FAILED', 'GLB file times do not represent unified T: %.9g frames' % max_time_error)
        original = BinaryData.decode_accessor
        indices = np.arange(1, count + 1, dtype=np.float64)

        def mapped(gltf, accessor_idx, cache=False):
            if accessor_idx in accessors:
                return (indices / float(fps)).reshape((-1, 1))
            return original(gltf, accessor_idx, cache=cache)
        BinaryData.decode_accessor = staticmethod(mapped)
        try:
            exchange.import_file(path, 'GLB', 'ANIMATION')
        finally:
            BinaryData.decode_accessor = staticmethod(original)
        time_map = {'kind': 'index_domain', 'source': 'actual_GLB_sampler_time_accessors', 'index_domain': [1, count], 'file_time_count': count, 'file_time_max_error_frames': max_time_error, 'file_times_seconds_sample': raw_times[:3] + raw_times[-3:]}
    elif kind == 'FBX':
        from . import clock_fbx as fbx_clock
        version, arrays, ticks, ktime, clock = _fbx_file_time_table(path)
        expected = [int(round(value / float(fps) * ktime)) for value in times]
        if ticks != expected:
            raise Failure('VALIDATION_FAILED', 'FBX KeyTime set differs from unified T FBX ticks')
        meta = {'key_frames': times, 'fps': int(fps), 'clock_scale': fbx_clock.clock_scale(times)}
        with fbx_clock.import_time_map(meta, index_domain=True):
            exchange.import_file(path, 'FBX', 'ANIMATION')
        time_map = {'kind': 'index_domain', 'source': 'actual_FBX_KeyTime_ticks', 'index_domain': [1, count], 'fbx_version': version, 'ktime_per_second': ktime, 'file_tick_count': len(ticks), 'file_tick_max_error': max((abs(ticks[index] - expected[index]) for index in range(min(len(ticks), len(expected)))), default=0), 'clock_endpoints': clock}
    else:
        raise Failure('UNSUPPORTED', 'Unknown format time map: ' + str(kind))
    scene = bpy.context.scene
    scene.render.fps = int(fps)
    scene.render.fps_base = 1.0
    scene.frame_start = 1
    scene.frame_end = count
    return time_map

def _imported_curve_counts():
    counts = {}
    for action in bpy.data.actions:
        for layer in action.layers:
            for strip in layer.strips:
                for slot in action.slots:
                    bag = strip.channelbag(slot)
                    if not bag:
                        continue
                    for curve in bag.fcurves:
                        key = len(curve.keyframe_points)
                        counts[key] = counts.get(key, 0) + 1
    return {str(key): value for key, value in sorted(counts.items())}

def _verify_imported_format(path, kind, native_capture, static, expected_bones, expected_keys, max_position_error, job=None):
    from . import format_io as exchange
    fps = int(native_capture['fps'])
    witness = [float(frame) for frame in native_capture['format_times']]
    time_map = _import_format_index_domain(path, kind, witness, fps)
    scene = bpy.context.scene
    imported, excluded = _map_imported_meshes(static)
    rig, structure = _format_structure(imported, static, expected_bones, expected_keys)
    topology = _format_topology_proofs(imported, static, float(max_position_error))
    curve_counts = _imported_curve_counts()
    if set(curve_counts) != {str(len(witness))}:
        raise Failure('VALIDATION_FAILED', 'Index-domain %s import curve key counts differ from T: %r' % (kind, curve_counts))
    native_times = {row['frame']: row for row in native_capture['native']['times']}
    checks = []
    key_checks = []
    for index, frame in enumerate(witness):
        import_frame = index + 1
        _set_frame(scene, import_frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for name, obj in imported.items():
            observed = _eval_snapshot(obj, depsgraph, with_triangles=True)
            native = native_times[frame]['meshes'][name]['snapshot']
            check = _format_geometry_check(native, observed, obj, topology[name])
            indexed = check['indexed_error_meters']
            check.update(frame=frame, import_frame=import_frame, mesh=name, layer='format_to_native', declared_position_tolerance=float(max_position_error), import_time_map='index_domain')
            check['declared_position_pass'] = check['bidirectional_vertex_distance'] <= float(max_position_error)
            if indexed is not None:
                check['indexed_error_meters'] = indexed
                check['declared_indexed_pass'] = indexed <= float(max_position_error)
                check['declared_position_pass'] = bool(check['declared_position_pass'] and check['declared_indexed_pass'])
            check['ok'] = bool(check['ok'] and check['declared_position_pass'])
            checks.append(check)
            if static[name]['shape_keys']:
                observed_values = {block.name: float(block.value) for block in obj.data.shape_keys.key_blocks} if obj.data.shape_keys else {}
                native_values = native_times[frame]['meshes'][name]['key_values']
                error = max((abs(observed_values.get(key, 0.0) - value) for key, value in native_values.items()), default=0.0)
                key_checks.append({'frame': frame, 'import_frame': import_frame, 'mesh': name, 'max_key_value_error': error, 'ok': error <= 1e-05})
    if job is not None and (not all((check['ok'] for check in checks)) or (key_checks and (not all((check['ok'] for check in key_checks))))):
        atomic_json(Path(job) / ('format-%s-failure.json' % kind.lower()), {'checks': checks, 'summary': _summary_checks(checks), 'key_checks': key_checks, 'time_map': time_map, 'first_failures': [check for check in checks if not check['ok']][:12]})
    if not all((check['ok'] for check in checks)):
        raise Failure('VALIDATION_FAILED', 'Fresh ' + kind + ' import differs from native Mesh over T')
    if key_checks and (not all((check['ok'] for check in key_checks))):
        raise Failure('VALIDATION_FAILED', 'Fresh ' + kind + ' import lost named Key values over T')
    return {'rig': rig, 'structure': structure, 'checks': checks, 'key_checks': key_checks, 'excluded_custom_shapes': excluded, 'imported': imported, 'import_time_map': time_map, 'curve_key_counts': curve_counts}

def _ordinary_import_report(path, kind, native_capture, static, expected_bones, expected_keys, max_position_error, job):
    """Report what the ordinary bundled importer does with the real file.

    This is deliberately diagnostic: the ordinary importer calls FCurve.update()
    and merges keys closer than ~0.01 frame, so the file's non-uniform keys
    cannot all survive that path.  The gate above uses the explicit index-domain
    map derived from the actual file's sampler/KeyTime data.
    """
    from . import format_io as exchange
    fps = int(native_capture['fps'])
    times = [float(frame) for frame in native_capture['format_times']]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = fps
    bpy.context.scene.render.fps_base = 1.0
    exchange.import_file(path, kind, 'ANIMATION')
    scene = bpy.context.scene
    scene.render.fps = fps
    scene.render.fps_base = 1.0
    imported, excluded = _map_imported_meshes(static)
    rig, structure = _format_structure(imported, static, expected_bones, expected_keys)
    topology = _format_topology_proofs(imported, static, float(max_position_error))
    counts = _imported_curve_counts()
    native_times = {row['frame']: row for row in native_capture['native']['times']}
    checks = []
    key_checks = []
    for frame in times:
        _set_frame(scene, frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for name, obj in imported.items():
            observed = _eval_snapshot(obj, depsgraph, with_triangles=True)
            native = native_times[frame]['meshes'][name]['snapshot']
            check = _format_geometry_check(native, observed, obj, topology[name])
            check.update(frame=frame, mesh=name, layer='ordinary_import_to_native', declared_position_tolerance=float(max_position_error), declared_position_pass=check['bidirectional_vertex_distance'] <= float(max_position_error))
            check['ok'] = bool(check['ok'] and check['declared_position_pass'])
            checks.append(check)
            if static[name]['shape_keys']:
                observed_values = {block.name: float(block.value) for block in obj.data.shape_keys.key_blocks} if obj.data.shape_keys else {}
                native_values = native_times[frame]['meshes'][name]['key_values']
                error = max((abs(observed_values.get(key, 0.0) - value) for key, value in native_values.items()), default=0.0)
                key_checks.append({'frame': frame, 'mesh': name, 'max_key_value_error': error})
    report = {'kind': kind, 'scope': 'ordinary bundled importer; merged near keys are reported, not repaired or gated', 'curve_key_counts': counts, 'checks': len(checks), 'failed': sum((1 for check in checks if not check['ok'])), 'max_position': max((check['bidirectional_vertex_distance'] for check in checks), default=0.0), 'key_checks': len(key_checks), 'key_failed': sum((1 for check in key_checks if check['max_key_value_error'] > 1e-05)), 'max_key_error': max((check['max_key_value_error'] for check in key_checks), default=0.0), 'worst_position': sorted(checks, key=lambda check: check['bidirectional_vertex_distance'], reverse=True)[:8], 'worst_key': sorted(key_checks, key=lambda check: check['max_key_value_error'], reverse=True)[:8], 'structure': structure, 'excluded_custom_shapes': excluded}
    atomic_json(Path(job) / ('ordinary-%s-import.json' % kind.lower()), report)
    return report

def _summary_checks(checks):
    groups = {}
    for check in checks:
        if check.get('indexed_error_meters') is None:
            raise Failure('UNSUPPORTED', 'Cannot summarize an unmeasured indexed error')
        key = (check.get('layer', 'native'), check.get('mesh'))
        row = groups.setdefault(key, {'layer': check.get('layer', 'native'), 'mesh': check.get('mesh'), 'checks': 0, 'failed': 0, 'max_bidirectional_vertex_distance': 0.0, 'max_indexed_error_meters': 0.0, 'max_area_delta': 0.0})
        row['checks'] += 1
        row['failed'] += 0 if check.get('ok') else 1
        row['max_bidirectional_vertex_distance'] = max(row['max_bidirectional_vertex_distance'], float(check.get('bidirectional_vertex_distance', 0.0)))
        indexed_error = check.get('indexed_error_meters')
        if indexed_error is not None:
            row['max_indexed_error_meters'] = max(row.get('max_indexed_error_meters', 0.0), float(indexed_error))
        row['max_area_delta'] = max(row['max_area_delta'], float(check.get('area_delta', 0.0)))
    return sorted(groups.values(), key=lambda row: (row['layer'], row['mesh']))

def _verify_saved_import_blend(blend_path, native_capture, static, expected_bones, expected_keys, times, max_position_error):
    from . import format_io as exchange
    bpy.ops.wm.open_mainfile(filepath=str(blend_path), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    imported, excluded = _map_imported_meshes(static)
    rig, structure = _format_structure(imported, static, expected_bones, expected_keys)
    topology = _format_topology_proofs(imported, static, float(max_position_error))
    native_times = {row['frame']: row for row in native_capture['native']['times']}
    checks = []
    key_checks = []
    for index, frame in enumerate(times):
        import_frame = index + 1
        _set_frame(scene, import_frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for name, obj in imported.items():
            observed = _eval_snapshot(obj, depsgraph, with_triangles=True)
            native = native_times[frame]['meshes'][name]['snapshot']
            check = _format_geometry_check(native, observed, obj, topology[name])
            check.update(frame=frame, import_frame=import_frame, mesh=name, layer='saved_import_to_native', declared_position_tolerance=float(max_position_error), import_time_map='index_domain')
            check['declared_position_pass'] = check['bidirectional_vertex_distance'] <= float(max_position_error)
            check['ok'] = bool(check['ok'] and check['declared_position_pass'])
            checks.append(check)
            if static[name]['shape_keys']:
                observed_values = {block.name: float(block.value) for block in obj.data.shape_keys.key_blocks} if obj.data.shape_keys else {}
                native_values = native_times[frame]['meshes'][name]['key_values']
                error = max((abs(observed_values.get(key, 0.0) - value) for key, value in native_values.items()), default=0.0)
                key_checks.append({'frame': frame, 'import_frame': import_frame, 'mesh': name, 'max_key_value_error': error, 'ok': error <= 1e-05})
    if not all((check['ok'] for check in checks)):
        raise Failure('VALIDATION_FAILED', 'Saved format import differs from native Mesh')
    if key_checks and (not all((check['ok'] for check in key_checks))):
        raise Failure('VALIDATION_FAILED', 'Saved format import lost named Key values')
    return {'structure': structure, 'checks': checks, 'key_checks': key_checks, 'excluded_custom_shapes': excluded}

def _cold_times(native_capture, key_times, first_checks):
    return [float(frame) for frame in native_capture['format_times']]

def _source_inventory_digest(rig, meshes, static=None):
    actions = []
    for action in bpy.data.actions:
        rows = _action_rows(action)
        actions.append(
            {
                "name": action.name,
                "users": action.users,
                "fake_user": bool(action.use_fake_user),
                "curves": len(rows),
                "key_times": sorted(
                    {round(float(t), 9) for row in rows for t in row["times"]}
                ),
                "interpolation": sorted(
                    {mode for row in rows for mode in row["interpolation"]}
                ),
            }
        )
    keys = {}
    for mesh in meshes:
        keys[mesh.name] = (
            [
                {
                    "name": block.name,
                    "relative_key": block.relative_key.name if block.relative_key else None,
                    "vertex_group": block.vertex_group,
                    "value": float(block.value),
                }
                for block in mesh.data.shape_keys.key_blocks
            ]
            if mesh.data.shape_keys
            else []
        )
    owners = {
        "rig": _animation_inventory(rig),
        "meshes": {
            mesh.name: {
                "object": _animation_inventory(mesh),
                "shape_keys": (
                    _animation_inventory(mesh.data.shape_keys)
                    if mesh.data.shape_keys
                    else None
                ),
            }
            for mesh in meshes
        },
    }
    weights = {}
    if static:
        for name, mesh_data in static.items():
            rows = [
                [[bone, round(float(weight), 12)] for bone, weight in row]
                for row in mesh_data["weights"]
            ]
            weights[name] = {
                "vertices": len(rows),
                "sha256": _sha256_json(rows),
                "meta": mesh_data["weight_meta"],
            }
    payload = {
        "actions": sorted(actions, key=lambda row: row["name"]),
        "shape_keys": keys,
        "animation": owners,
        "weights": weights,
        "bind_world": {
            "frame": float(bpy.context.scene.frame_current_final),
            "rig": [list(row) for row in rig.matrix_world],
            "meshes": {
                mesh.name: [list(row) for row in mesh.matrix_world] for mesh in meshes
            },
        },
    }
    return _sha256_json(payload), payload

def _expected_keys(static):
    return {name: [key['name'] for key in mesh_data['shape_keys']] for name, mesh_data in static.items()}

def _key_semantics(static, native_capture, candidate_masks=None):
    native_keys = _native_key_map(native_capture)
    result = {}
    for name, mesh_data in static.items():
        if not mesh_data['shape_keys']:
            result[name] = {'mode': 'no_shape_keys', 'keys': []}
            continue
        keys = []
        for key in mesh_data['shape_keys']:
            alias = None
            if candidate_masks and name in candidate_masks:
                alias = candidate_masks[name].get(key['vertex_group'])
            values = [float(native_keys[frame][name].get(key['name'], 0.0)) for frame in native_capture['witness']]
            keys.append({'name': key['name'], 'relative_key': key['relative_key'], 'vertex_group': key['vertex_group'], 'mask_alias': alias, 'slider_min': key['slider_min'], 'slider_max': key['slider_max'], 'mute': key['mute'], 'value_min': min(values), 'value_max': max(values), 'non_zero': any((abs(value) > 1e-09 for value in values))})
        result[name] = {'mode': 'sampled_native_driver_values', 'keys': keys, 'key_animation': 'all witness times; LINEAR Key value curves'}
    return result

def _native_key_map(native_capture):
    witness_path = native_capture.get('native_witness_file')
    if witness_path and Path(witness_path).is_file():
        cache = read_json(Path(witness_path))
    else:
        cache = native_capture['native']
    return {row['frame']: {name: row['meshes'][name]['key_values'] for name in row['meshes']} for row in cache['times']}

def _output_entry(path):
    return {'file': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size}

def _animation_inventory(owner):
    animation = getattr(owner, "animation_data", None)
    if not animation:
        return None
    return {
        "action": animation.action.name if animation.action else None,
        "action_slot": (
            animation.action_slot.identifier if animation.action_slot else None
        ),
        "use_nla": bool(animation.use_nla),
        "tweak_mode": bool(animation.use_tweak_mode),
        "tracks": [
            {
                "name": track.name,
                "mute": bool(track.mute),
                "is_solo": bool(track.is_solo),
                "strips": [
                    {
                        "name": strip.name,
                        "type": str(strip.type),
                        "action": strip.action.name if strip.action else None,
                        "action_slot": (
                            strip.action_slot.identifier
                            if strip.action_slot
                            else None
                        ),
                        "action_frame_start": float(strip.action_frame_start)
                        if hasattr(strip, "action_frame_start")
                        else None,
                        "action_frame_end": float(strip.action_frame_end)
                        if hasattr(strip, "action_frame_end")
                        else None,
                        "frame_start": float(strip.frame_start)
                        if hasattr(strip, "frame_start")
                        else None,
                        "frame_end": float(strip.frame_end)
                        if hasattr(strip, "frame_end")
                        else None,
                        "scale": float(strip.scale) if hasattr(strip, "scale") else None,
                        "repeat": float(strip.repeat) if hasattr(strip, "repeat") else None,
                        "blend_type": str(strip.blend_type)
                        if hasattr(strip, "blend_type")
                        else None,
                        "extrapolation": str(strip.extrapolation)
                        if hasattr(strip, "extrapolation")
                        else None,
                        "mute": bool(getattr(strip, "mute", False)),
                    }
                    for strip in track.strips
                ],
            }
            for track in animation.nla_tracks
        ],
        "driver_count": len(animation.drivers),
        "driver_paths": sorted(
            {
                (curve.data_path, int(curve.array_index))
                for curve in animation.drivers
            }
        ),
    }

def _matrix_delta(left, right):
    return max(
        abs(float(a) - float(b))
        for row_left, row_right in zip(left, right)
        for a, b in zip(row_left, row_right)
    )

def _validate_world_matrix(matrix, label):
    values = np.asarray(matrix, dtype=np.float64)
    if values.shape != (4, 4) or not np.all(np.isfinite(values)):
        raise Failure("UNSUPPORTED", "Nonfinite or malformed object world matrix: " + label)
    determinant = float(np.linalg.det(values[:3, :3]))
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        raise Failure("UNSUPPORTED", "Singular object world matrix: " + label)
    if determinant <= 0.0:
        raise Failure(
            "UNSUPPORTED",
            "Negative/reflection object world matrix is outside the verified exchange: " + label,
        )
    return values

def export_shared(params, spec, job, started, hashes=None):
    from . import format_io as exchange
    from . import io_support as modeling
    from . import resource_policy as nodes
    from . import io_support as scenes

    job = Path(job)
    started = float(started)
    if spec.get("rig_adapter") != ADAPTER:
        raise Failure("INVALID_REQUEST", "continuous_skin_exchange requires " + ADAPTER)
    if spec.get("mode") != "ANIMATION":
        raise Failure("INVALID_REQUEST", "Shared continuous exchange requires ANIMATION")
    if spec.get("materials") != "NONE" or float(spec.get("meters_per_unit", 0)) != 1.0:
        raise Failure("INVALID_REQUEST", "Shared continuous exchange requires NONE materials and meter scale one")
    sampling = spec.get("rig_sampling")
    if not sampling:
        raise Failure("INVALID_REQUEST", "Shared continuous exchange requires rig_sampling")
    source = Path(params["file"])
    source_sha_before = digest(source)
    if params.get("expected_sha256") and params["expected_sha256"] != source_sha_before:
        raise Failure("CONFLICT", "Source SHA256 differs from the shared exchange request")
    if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
        raise Failure("UNSUPPORTED", "Shared continuous exchange requires matching Blender major/minor")

    scene = scenes.find(bpy.data.scenes, spec["scene"])
    layer = scenes.find(scene.view_layers, spec["view_layer"])
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    selected = [scenes.find(bpy.data.objects, name) for name in spec["objects"]]
    if any(obj.name not in layer.objects for obj in selected):
        raise Failure("UNSUPPORTED", "Shared exchange selection is outside the selected view layer")
    rigs = [obj for obj in selected if obj.type == "ARMATURE"]
    meshes = [obj for obj in selected if obj.type == "MESH"]
    if len(rigs) != 1 or not 1 <= len(meshes) <= 16 or len(selected) != len(meshes) + 1:
        raise Failure("UNSUPPORTED", "Shared exchange selects exactly one armature and 1..16 meshes")
    rig = rigs[0]
    if not rig.pose or not rig.data.bones:
        raise Failure("UNSUPPORTED", "Shared exchange source armature has no pose/bones")
    scene.render.fps_base = float(scene.render.fps_base)
    if scene.render.fps_base != 1.0 or scene.unit_settings.scale_length != 1.0:
        raise Failure("UNSUPPORTED", "Shared exchange requires integer fps and meter scale one")
    # Capture bind identity at the declared domain start, not at whatever
    # frame the source file happened to be saved on.  This matters when the
    # rig has common object motion: the B-Bone support and bind worlds must be
    # read in the declared bind frame.
    _set_frame(scene, float(spec["frame_start"]))
    static = {mesh.name: _static_mesh_data(mesh, rig) for mesh in meshes}
    from . import key_library as keylib

    source_key_libraries = {
        mesh.name: keylib.capture_library(mesh)
        for mesh in meshes
    }
    for name, library in source_key_libraries.items():
        static[name]["source_library"] = library
    breakpoints, breakpoint_evidence = _discover_breakpoints(spec, rig, meshes)
    source_inventory_sha, source_inventory = _source_inventory_digest(rig, meshes, static)

    with ExitStack() as stack:
        merged_hashes = dict(hashes or {})
        merged_hashes.update(nodes.resource_guards(params, stack))
        merged_hashes[str(source.resolve())] = source_sha_before
        exchange.safe_source(params.get("driver_profile"), str(source), job)
        native_capture = _capture_native(
            params, spec, job, rig, meshes, static, breakpoints, sampling
        )
        refined_key_times = _refine_keys(native_capture, static, sampling, breakpoints, job)
        key_times, key_float32_collapsed = _float32_times(refined_key_times)
        native_capture["refinement_key_times"] = refined_key_times
        native_capture["key_times"] = key_times
        native_capture.setdefault("refinement", {}).update(
            {
                "key_count": len(key_times),
                "float32_key_identity": key_float32_collapsed,
            }
        )

    bpy.ops.wm.read_factory_settings(use_empty=True)
    candidate = _build_candidate(native_capture, static, spec, sampling, breakpoints)
    native_library_checks = {}
    for name, library in source_key_libraries.items():
        info = candidate["meshes"][name]
        native_library_checks[name] = keylib.verify_native_library(
            library,
            info["object"],
            position_tolerance=float(sampling["max_position_error"]),
            mask_aliases=info["mask_groups"],
        )
    candidate_mask_aliases = {
        name: dict(info["mask_groups"]) for name, info in candidate["meshes"].items()
    }
    expected_keys = _expected_keys(static)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(
        filepath=str(job / "continuous-native-preverify.blend"),
        relative_remap=False,
        check_existing=False,
    )
    native_checks, native_matrix_checks, native_key_checks = _verify_native(
        candidate, native_capture, static, spec, key_times, job=job
    )
    native_structure = _structure(candidate["rig"], candidate["meshes"], native_capture["joints"], expected_keys)
    atomic_json(
        job / "continuous-native-checks.json",
        {
            "checks": native_checks,
            "matrix_checks": native_matrix_checks,
            "key_checks": native_key_checks,
            "summary": _summary_checks(native_checks),
            "structure": native_structure,
        },
    )
    bpy.context.preferences.filepaths.save_version = 0
    native_blend = job / "continuous-native-candidate.blend"
    bpy.ops.wm.save_as_mainfile(
        filepath=str(native_blend), relative_remap=False, check_existing=False
    )
    native_sha = digest(native_blend)
    bpy.ops.wm.open_mainfile(filepath=str(native_blend), load_ui=False, use_scripts=False)
    rig = bpy.data.objects["SharedContinuousRig"]
    reopened_meshes = {
        name: {"object": bpy.data.objects[name]} for name in static
    }
    reopened_candidate = {"rig": rig, "meshes": reopened_meshes}
    subset = sorted(set(key_times) | {native_capture["witness"][0], native_capture["witness"][len(native_capture["witness"]) // 2], native_capture["witness"][-1]})
    _verify_native(
        reopened_candidate,
        native_capture,
        static,
        spec,
        key_times,
        frames=subset,
        job=job,
    )
    _structure(rig, reopened_meshes, native_capture["joints"], expected_keys)

    format_kind = spec["format"]
    format_times = [float(frame) for frame in native_capture["format_times"]]
    format_count = len(format_times)
    outputs = [_output_entry(native_blend), _output_entry(Path(native_capture["native_witness_file"]))]
    format_evidence = {}
    format_evidence["source_id_uv"] = _add_source_id_uv(reopened_meshes)
    if format_kind == "FBX":
        from . import clock_fbx as fbx_clock

        format_scene = bpy.context.scene
        format_scene.frame_start = 1
        format_scene.frame_end = format_count
        format_evidence["action_bake"] = _bake_format_actions(
            rig,
            reopened_meshes,
            format_times,
            list(range(1, format_count + 1)),
        )
        template = job / "export-template-indexed.fbx"
        format_path = job / "export.fbx"
        format_evidence["fbx_template"] = _export_fbx(
            template,
            spec,
            rig,
            reopened_meshes,
            1.0,
            require_fractional=False,
        )
        meta = {
            "adapter": ADAPTER,
            "fps": int(native_capture["fps"]),
            "key_frames": format_times,
            "clock_scale": fbx_clock.clock_scale(format_times),
            "source_cache_sha256": source_sha_before,
            "meshes": [
                {"name": name, "vertices": int(static[name]["vertices"])}
                for name in static
            ],
        }
        remap = fbx_clock.write_fbx(template, format_path, meta)
        version, arrays, ticks, ktime, clock = _fbx_file_time_table(format_path)
        expected_ticks = [
            int(round(value / float(native_capture["fps"]) * ktime))
            for value in format_times
        ]
        if ticks != expected_ticks:
            raise Failure(
                "VALIDATION_FAILED", "FBX final KeyTime ticks differ from unified T"
            )
        format_evidence["fbx"] = {
            "version": version,
            "ktime_per_second": ktime,
            "animation_curves": len(arrays),
            "key_count": len(ticks),
            "key_times_match_T": True,
            "min_gap_frames": min(
                (
                    format_times[index + 1] - format_times[index]
                    for index in range(len(format_times) - 1)
                ),
                default=0.0,
            ),
            "duration_seconds": (
                (format_times[-1] - format_times[0]) / float(native_capture["fps"])
                if format_times
                else 0.0
            ),
            "clock_scale": meta["clock_scale"],
            "clock_endpoints": clock,
            "remap": remap,
        }
        format_evidence["fbx_time_map"] = {
            "key_frames": format_times,
            "tick_count": len(ticks),
            "remap": remap,
        }
        format_evidence["fbx_template_bytes"] = template.stat().st_size
        outputs.append(_output_entry(format_path))
        import_result = _verify_imported_format(
            format_path,
            "FBX",
            native_capture,
            static,
            2 * len(native_capture["joints"]),
            expected_keys,
            float(sampling["max_position_error"]),
            job=job,
        )
    else:
        format_evidence["action_bake"] = _bake_format_actions(
            rig, reopened_meshes, format_times, format_times
        )
        standard = job / "export-standard.glb"
        format_evidence["glb_standard"] = _export_glb_standard(
            standard, spec, rig, reopened_meshes
        )
        format_evidence["glb_standard_inventory"] = _glb_inventory(standard)
        outputs.append(_output_entry(standard))
        # Flattened vertex rows are required for the source-vertex-ID restore.
        vertex_tables = {}
        alias_owner = {}
        for index, (name, mesh_data) in enumerate(static.items()):
            rows = []
            for row in mesh_data["mapping"]:
                rows.append(
                    [
                        (f"D{native_capture['joint_index'][joint]:04d}", weight)
                        for joint, weight in row
                    ]
                )
            _register_weight_alias(vertex_tables, alias_owner, name, name, rows)
            _register_weight_alias(
                vertex_tables, alias_owner, mesh_data["data_name"], name, rows
            )
            _register_weight_alias(
                vertex_tables, alias_owner, f"SharedMesh_{index:02d}_{name}", name, rows
            )
        restored = job / "export-weight-restored.glb"
        format_evidence["weight_restore"] = _glb_backfill_weights(
            standard, restored, vertex_tables
        )
        outputs.append(_output_entry(restored))
        compat = job / "export-compat-linear.glb"
        format_evidence["compat"] = _glb_compat_linear(
            restored, compat, format_times, native_capture["fps"]
        )
        format_evidence["glb_compat_inventory"] = _glb_inventory(compat)
        outputs.append(_output_entry(compat))
        format_path = compat
        import_result = _verify_imported_format(
            compat,
            "GLB",
            native_capture,
            static,
            2 * len(native_capture["joints"]),
            expected_keys,
            float(sampling["max_position_error"]),
            job=job,
        )
    format_library_checks = {}
    for name, library in source_key_libraries.items():
        obj = import_result["imported"][name]
        mapping = keylib.choose_format_mapping(
            obj, library, float(sampling["max_position_error"])
        )
        format_library_checks[name] = keylib.verify_effective_library(
            library,
            obj,
            position_tolerance=float(sampling["max_position_error"]),
            mapping=mapping,
        )
    imported_blend = job / f"format-{format_kind.lower()}-reimport.blend"
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(
        filepath=str(imported_blend), relative_remap=False, check_existing=False
    )
    outputs.append(_output_entry(imported_blend))
    imported_summary = {
        "structure": import_result["structure"],
        "excluded_custom_shapes": import_result["excluded_custom_shapes"],
        "summary": _summary_checks(import_result["checks"]),
        "key_checks": import_result["key_checks"],
        "import_time_map": import_result["import_time_map"],
        "curve_key_counts": import_result["curve_key_counts"],
    }
    try:
        format_evidence["ordinary_import"] = _ordinary_import_report(
            format_path,
            format_kind,
            native_capture,
            static,
            2 * len(native_capture["joints"]),
            expected_keys,
            float(sampling["max_position_error"]),
            job,
        )
    except Exception as exc:
        format_evidence["ordinary_import"] = {
            "error": str(exc),
            "type": type(exc).__name__,
            "scope": "ordinary importer diagnostic failed; index-domain file-data gate above is unaffected",
        }
        atomic_json(
            job / ("ordinary-%s-import.json" % format_kind.lower()),
            format_evidence["ordinary_import"],
        )
    cold_times = _cold_times(native_capture, key_times, import_result["checks"])
    cold_manifest = {
        "version": "1.0",
        "adapter": ADAPTER,
        "format": format_kind,
        "format_file": str(format_path),
        "format_sha256": digest(format_path),
        "native_witness_file": Path(native_capture["native_witness_file"]).name,
        "native_witness_sha256": digest(Path(native_capture["native_witness_file"])),
        "source_mesh_names": list(static),
        "source_data_names": {name: data["data_name"] for name, data in static.items()},
        "expected_bones": 2 * len(native_capture["joints"]),
        "expected_keys": expected_keys,
        "clock": native_capture["native"]["clock"],
        "bind_frame": native_capture["native"]["bind_frame"],
        "world_mode": native_capture["native"]["world_motion"]["mode"],
        "cold_times": cold_times,
        "key_frames": format_times,
        "uniform_format_times": native_capture["uniform_format_times"],
        "original_witness_times": native_capture["original_witness_times"],
        "float32_identity": native_capture["float32_identity"],
        "import_time_map": {
            "kind": "index_domain",
            "frame_count": len(format_times),
            "file_time_map": import_result["import_time_map"],
        },
        "witness_count": len(format_times),
        "declared_position_tolerance": float(sampling["max_position_error"]),
        "scope": "Independent-process index-domain fresh format import at the complete unified T (original non-uniform witnesses, source/refinement key times and uniform format grid); import values come from the actual format file; not arbitrary continuous-time proof",
    }
    atomic_json(job / "cold-manifest.json", cold_manifest)
    reopened_format = _verify_saved_import_blend(
        imported_blend,
        native_capture,
        static,
        2 * len(native_capture["joints"]),
        expected_keys,
        cold_times,
        float(sampling["max_position_error"]),
    )
    max_output_bytes = int(sampling["max_output_bytes"])
    all_outputs = [
        path
        for path in (
            [native_blend, Path(native_capture["native_witness_file"]), format_path, imported_blend]
            + ([job / "export-standard.glb", job / "export-weight-restored.glb"] if format_kind == "GLB" else [])
        )
        if path.is_file()
    ]
    all_output_bytes = {str(path): path.stat().st_size for path in all_outputs}
    format_outputs = [
        path
        for path in (
            [format_path]
            + ([job / "export-standard.glb", job / "export-weight-restored.glb"] if format_kind == "GLB" else [])
        )
        if path.is_file()
    ]
    format_output_bytes = {str(path): path.stat().st_size for path in format_outputs}
    if any(size > max_output_bytes for size in format_output_bytes.values()):
        atomic_json(
            job / "continuous-budget.json",
            {
                "max_output_bytes": max_output_bytes,
                "actual_format_bytes": format_output_bytes,
                "all_output_bytes": all_output_bytes,
                "ok": False,
                "stage": "post_export_main_format",
            },
        )
        raise Failure("UNSUPPORTED", "Shared continuous main format output exceeds max_output_bytes")
    source_sha_after = digest(source)
    if source_sha_after != source_sha_before:
        raise Failure("CONFLICT", "Shared continuous exchange changed the source file")
    key_semantics = _key_semantics(static, native_capture, candidate_mask_aliases)
    native_summary = _summary_checks(native_checks)
    format_summary = imported_summary["summary"]
    report = {
        "exchange_report_version": "1.0",
        "operation": "export",
        "format": format_kind,
        "mode": "ANIMATION",
        "settings": spec,
        "adapter": ADAPTER,
        "checks": import_result["checks"],
        "outputs": outputs,
        "candidate": str(native_blend),
        "candidate_sha256": native_sha,
        "format_import_blend": str(imported_blend),
        "format_import_blend_sha256": digest(imported_blend),
        "resource_hashes": merged_hashes,
        "seconds": time.monotonic() - started,
        "source_saved": False,
        "reopen": "pass",
        "material_profile": [],
        "losses": [
            "Selected base meshes only; listed post modifiers are omitted from the exchange copy: "
            + json.dumps({name: data["post_report"] for name, data in static.items()}, ensure_ascii=False),
            "C94 controls/constraints/drivers and original Actions remain only in the source; the exchange receives ordinary affine bones.",
            "Named relative Keys are preserved by name/link/mask/coordinates; native driver/Action values are sampled at finite witness times, not copied as driver graphs.",
            "One shared support pivot per affine joint; finite witness Hermite representation does not claim arbitrary continuous-time equality.",
            "Formats are explicitly sampled on the complete unified T. FBX uses an integer-index template remapped to actual T KTIME ticks with fbx_clock; GLB keeps the standard file and a source-ID weight-restored copy, and the compatibility copy stores LINEAR samples at T. Both format gates import in an explicit index domain derived from the actual file's KeyTime/sampler times; the ordinary importer result is reported separately.",
        ],
        "rig_transfer": {
            "adapter": ADAPTER,
            "frames": native_capture["witness"],
            "key_frames": key_times,
            "meshes": native_structure,
            "shared_joint_count": len(native_capture["joints"]),
            "ordinary_bones": 2 * len(native_capture["joints"]),
            "pivot": "one static union weighted centroid per shared joint across all selected meshes",
            "time_branch": "time-coupled SVD signed-permutation path; piecewise Hermite A.location at actual source Action/driver breakpoints",
            "keys": key_semantics,
            "budget": native_capture["budget"],
            "native_evaluations": native_capture["native_evaluations"],
            "native_checks": _summary_checks(native_checks),
            "format_checks": format_summary,
            "format": format_kind,
            "format_file": str(format_path),
            "format_sha256": digest(format_path),
            "time_table": {
                "unified_T": len(format_times),
                "original_witness": len(native_capture["original_witness_times"]),
                "uniform_format_grid": len(native_capture["uniform_format_times"]),
                "float32_collapsed": native_capture["float32_identity"],
                "key_frames": key_times,
                "format_key_frames": format_times,
            },
            "format_time_map": import_result["import_time_map"],
            "ordinary_import": {
                key: value
                for key, value in format_evidence["ordinary_import"].items()
                if key not in ("worst_position", "worst_key")
            },
            "cold_times": cold_times,
            "cold_manifest": str(job / "cold-manifest.json"),
            "file_costs": format_output_bytes,
        },
        "continuous": {
            "status": "pass-bounded",
            "scope": "One shared ordinary-bone armature across selected base meshes, fixed native B-Bone segment support, real named Key values and finite witness times",
            "shared_skeleton": {
                "armature": "SharedContinuousRig",
                "affine_joints": len(native_capture["joints"]),
                "bones": 2 * len(native_capture["joints"]),
                "meshes": native_structure,
                "shared_joint_names_sample": [
                    f"D{index:04d}" for index in range(min(12, len(native_capture["joints"])))
                ],
                "support_pivot": "static union weighted centroid per joint across every mesh",
            },
            "breakpoints": breakpoints,
            "breakpoint_evidence": breakpoint_evidence,
            "key_times": key_times,
            "witness_times": native_capture["witness"],
            "original_witness_times": native_capture["original_witness_times"],
            "uniform_format_times": native_capture["uniform_format_times"],
            "float32_identity": native_capture["float32_identity"],
            "native_evaluations": native_capture["native_evaluations"],
            "budget": native_capture["budget"],
            "world_motion": native_capture["native"]["world_motion"],
            "bind_frame": native_capture["native"]["bind_frame"],
            "refinement": native_capture.get("refinement", {}),
            "fixed_mapping_max_error": native_capture["native"]["fixed_mapping_max_error"],
            "native_checks": {
                "summary": native_summary,
                "max_matrix_error": max(
                    (row["max_joint_matrix_error"] for row in native_matrix_checks),
                    default=0.0,
                ),
                "key_value_max_error": max(
                    (row["max_key_value_error"] for row in native_key_checks), default=0.0
                ),
                "all_pass": all(check["ok"] for check in native_checks),
            },
            "format_checks": {
                "summary": format_summary,
                "all_pass": all(check["ok"] for check in import_result["checks"]),
                "key_checks": import_result["key_checks"],
                "structure": imported_summary["structure"],
                "excluded_custom_shapes": imported_summary["excluded_custom_shapes"],
                "import_time_map": import_result["import_time_map"],
                "curve_key_counts": import_result["curve_key_counts"],
                "saved_blend_subset": {
                    "times": cold_times,
                    "summary": _summary_checks(reopened_format["checks"]),
                    "all_pass": all(check["ok"] for check in reopened_format["checks"]),
                },
            },
            "key_semantics": key_semantics,
            "key_library": {
                "native": native_library_checks,
                "format_effective": format_library_checks,
                "identity_policy": {
                    name: check["mapping"]
                    for name, check in format_library_checks.items()
                },
                "literal_format_metadata": (
                    "relative/mask are verified natively; standard formats are "
                    "verified by effective one-hot named-Key deltas, allowing "
                    "flattening into named Basis deltas"
                ),
                "scope": (
                    "Complete named-Key library, including Keys never played in "
                    "the clip; native metadata is exact, format deltas are "
                    "effective and identity-mapped"
                ),
            },
            "format_evidence": format_evidence,
            "cold_manifest": str(job / "cold-manifest.json"),
            "cold_times": cold_times,
            "file_costs": {
                "format_outputs": format_output_bytes,
                "all_outputs": all_output_bytes,
                "max_output_bytes": max_output_bytes,
            },
            "source_protection": {
                "source_file": str(source),
                "source_sha256": source_sha_before,
                "source_sha256_after": source_sha_after,
                "source_unchanged": source_sha_after == source_sha_before,
                "source_action_inventory_sha256": source_inventory_sha,
                "source_actions": [row["name"] for row in source_inventory["actions"]],
                "source_shape_keys": {
                    name: [key["name"] for key in keys]
                    for name, keys in source_inventory["shape_keys"].items()
                },
                "source_animation": source_inventory["animation"],
                "source_weights": source_inventory["weights"],
                "source_bind_world": source_inventory["bind_world"],
                "candidate_action_copy_policy": "new ordinary-bone and Key Actions only; source Actions are not copied or deleted",
            },
        },
    }
    atomic_json(job / "exchange-report.json", report)
    atomic_json(job / "continuous-report.json", report["continuous"])
    return report
