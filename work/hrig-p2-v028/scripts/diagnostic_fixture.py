"""Owned synthetic E1 fixture. Draft: Blender-native execution is NOT yet validated.

Run create('alpha') or create('beta') only in a disposable owned Blender worker.
No file saving, factory reset, registration, preferences, handlers or Cloth.
"""

import hashlib
import json
import math
import struct


SUPPORTED_VERSION = (5, 2, 1)
CASES = ("alpha", "beta")
ROWS = 9
COLS = 7
HEIGHT = 2.0
HALF_WIDTH = 0.45
ROOT_ROLL = 0.31
TIP_ROLL = -0.47
SHAPE_VALUE = 0.125
OWNER = "hrig-v020-synthetic-observation-fixture"
# frame, root local-X Euler rotation, root local-X translation, child driver angle
KEY_INPUTS = ((1, 0.0, 0.0, -0.12), (4, 0.28, 0.06, 0.35), (8, -0.16, -0.04, -0.24))


def _names(case):
    if case not in CASES:
        raise ValueError("case must be alpha or beta")
    # Both cases share this implementation and every numeric parameter.
    return {role: f"{case}_fixture_{role}" for role in (
        "scene", "view_layer", "armature", "armature_data", "mesh", "mesh_data",
        "root", "tip", "material", "uv", "basis", "shape", "armature_modifier",
        "subsurf_modifier", "bend_angle",
    )}


def _grid():
    vertices = []
    for row in range(ROWS):
        z = HEIGHT * row / (ROWS - 1)
        for col in range(COLS):
            x = HALF_WIDTH * (2.0 * col / (COLS - 1) - 1.0)
            y = 0.03 * math.sin(math.pi * row / (ROWS - 1)) * (x / HALF_WIDTH)
            vertices.append((x, y, z))
    faces = []
    for row in range(ROWS - 1):
        for col in range(COLS - 1):
            lower = row * COLS + col
            faces.append((lower, lower + 1, lower + 1 + COLS, lower + COLS))
    return vertices, faces


def _sha_floats(values):
    return hashlib.sha256(b"".join(struct.pack("<f", float(value)) for value in values)).hexdigest()


def _sha_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def protection(manifest):
    """Read-only declared static witness; intentionally excludes animated pose/property.

    Returned hashes are comparable before/after within ONE case. Case-specific
    names in bone parents and material references are not alpha/beta equivalence.
    """
    import bpy

    names = manifest["names"]
    obj = bpy.data.objects[names["mesh"]]
    arm = bpy.data.objects[names["armature"]]
    mesh = obj.data
    keys = mesh.shape_keys
    uv = mesh.uv_layers[names["uv"]]
    bones = [{"name": bone.name, "parent": bone.parent.name if bone.parent else None,
              "connected": bone.use_connect, "deform": bone.use_deform,
              "matrix_local": [list(row) for row in bone.matrix_local]}
             for bone in arm.data.bones]
    return {
        "raw_coordinates_f32": _sha_floats(c for v in mesh.vertices for c in v.co),
        "polygons": _sha_json([list(poly.vertices) for poly in mesh.polygons]),
        "uv_f32": _sha_floats(c for item in uv.data for c in item.uv),
        "material": _sha_json({"slots": [m.name if m else None for m in mesh.materials],
                                "diffuse": list(mesh.materials[0].diffuse_color)}),
        "shape_keys": _sha_json([
            {"name": key.name, "value": key.value,
             "coordinates_f32": _sha_floats(c for v in key.data for c in v.co)}
            for key in keys.key_blocks]),
        "rest_bones": _sha_json(bones),
        "mesh_parent": _sha_json({"parent": obj.parent.name if obj.parent else None,
                                    "parent_type": obj.parent_type,
                                    "matrix_parent_inverse": [list(row) for row in obj.matrix_parent_inverse],
                                    "matrix_basis": [list(row) for row in obj.matrix_basis]}),
        "armature_world": _sha_floats(c for row in arm.matrix_world for c in row),
        "weights": _sha_json([[v.index, [[g.group, g.weight] for g in v.groups]]
                               for v in mesh.vertices]),
    }


def create(case):
    """Create one new owned scene, make it active, return JSON-serializable manifest.

    The caller owns this process. Existing scene/data are neither deleted nor
    overwritten. On failure discard the worker; partial fixture is not accepted.
    Setup evaluates frame 1 once to validate indices; this is NOT a cold-first-frame
    benchmark. Timed evaluation/complete native acceptance belong to the caller.
    """
    import bpy
    from mathutils import Matrix

    names = _names(case)
    if tuple(bpy.app.version) != SUPPORTED_VERSION:
        raise RuntimeError(f"requires Blender {SUPPORTED_VERSION}, got {bpy.app.version}")
    if bpy.context.mode != "OBJECT" or bpy.context.window is None:
        raise RuntimeError("owned worker must have an active window and OBJECT mode")
    for collection, role in ((bpy.data.scenes, "scene"), (bpy.data.objects, "armature"),
                             (bpy.data.objects, "mesh"), (bpy.data.armatures, "armature_data"),
                             (bpy.data.meshes, "mesh_data"), (bpy.data.materials, "material")):
        if collection.get(names[role]) is not None:
            raise RuntimeError(f"fixture name already exists: {names[role]}")

    scene = bpy.data.scenes.new(names["scene"])
    scene["hrig_fixture_owner"] = OWNER
    scene["hrig_fixture_case"] = case
    scene.frame_start, scene.frame_end = 1, 8
    scene.render.fps, scene.render.fps_base = 24, 1.0
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    bpy.context.window.scene = scene
    view_layer = scene.view_layers[0]
    view_layer.name = names["view_layer"]

    arm_data = bpy.data.armatures.new(names["armature_data"])
    arm = bpy.data.objects.new(names["armature"], arm_data)
    scene.collection.objects.link(arm)
    arm.location = (0.37, -0.26, 0.18)
    arm.rotation_mode = "XYZ"
    arm.rotation_euler = (0.12, -0.20, 0.28)
    arm.scale = (1.1, 1.1, 1.1)
    view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        root = arm_data.edit_bones.new(names["root"])
        root.head, root.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        root.roll = ROOT_ROLL
        root.use_deform = True
        tip = arm_data.edit_bones.new(names["tip"])
        tip.head, tip.tail = (0.0, 0.0, 1.0), (0.0, 0.0, 2.0)
        tip.parent, tip.use_connect = root, True
        tip.roll = TIP_ROLL
        tip.use_deform = True
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    vertices, faces = _grid()
    mesh = bpy.data.meshes.new(names["mesh_data"])
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(names["mesh"], mesh)
    scene.collection.objects.link(obj)
    # Same local coordinate space as the armature, no bone parenting or implicit
    # world-space preservation. Skinning is performed solely by the modifier.
    obj.parent = arm
    obj.parent_type = "OBJECT"
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_basis = Matrix.Identity(4)
    uv = mesh.uv_layers.new(name=names["uv"])
    for loop in mesh.loops:
        x, _, z = vertices[loop.vertex_index]
        uv.data[loop.index].uv = ((x / HALF_WIDTH + 1.0) * 0.5, z / HEIGHT)
    material = bpy.data.materials.new(names["material"])
    material.diffuse_color = (0.28, 0.42, 0.65, 1.0)
    mesh.materials.append(material)
    obj.shape_key_add(name=names["basis"], from_mix=False)
    shape = obj.shape_key_add(name=names["shape"], from_mix=False)
    for index, (_, _, z) in enumerate(vertices):
        shape.data[index].co.x += 0.04 * (z / HEIGHT) ** 2
    shape.value = SHAPE_VALUE

    root_group = obj.vertex_groups.new(name=names["root"])
    tip_group = obj.vertex_groups.new(name=names["tip"])
    for index, (_, _, z) in enumerate(vertices):
        root_weight = min(1.0, max(0.0, (1.35 - z) / 0.7))
        if root_weight > 0.0:
            root_group.add([index], root_weight, "REPLACE")
        if root_weight < 1.0:
            tip_group.add([index], 1.0 - root_weight, "REPLACE")
    skin = obj.modifiers.new(names["armature_modifier"], "ARMATURE")
    skin.object = arm
    skin.use_vertex_groups, skin.use_bone_envelopes = True, False
    subdiv = obj.modifiers.new(names["subsurf_modifier"], "SUBSURF")
    subdiv.subdivision_type = "CATMULL_CLARK"
    subdiv.levels, subdiv.render_levels = 1, 1

    root_pose, tip_pose = arm.pose.bones[names["root"]], arm.pose.bones[names["tip"]]
    root_pose.rotation_mode = tip_pose.rotation_mode = "XYZ"
    arm[names["bend_angle"]] = KEY_INPUTS[0][3]
    curve = tip_pose.driver_add("rotation_euler", 2)
    curve.driver.type = "SCRIPTED"
    # Explicitly own this new driver's variables, regardless of native defaults.
    for existing in list(curve.driver.variables):
        curve.driver.variables.remove(existing)
    variable = curve.driver.variables.new()
    variable.name, variable.type = "bend", "SINGLE_PROP"
    variable.targets[0].id = arm
    variable.targets[0].data_path = '["' + names["bend_angle"] + '"]'
    curve.driver.expression = "bend"
    for frame, angle, offset, bend in KEY_INPUTS:
        root_pose.rotation_euler[0] = angle
        root_pose.location[0] = offset
        arm[names["bend_angle"]] = bend
        root_pose.keyframe_insert(data_path="rotation_euler", index=0, frame=frame)
        root_pose.keyframe_insert(data_path="location", index=0, frame=frame)
        arm.keyframe_insert(data_path='["' + names["bend_angle"] + '"]', frame=frame)
    # No Action.fcurves assumption: keyframe_insert creates the current native
    # Action/slot arrangement; equivalence is checked through evaluated outputs.
    scene.frame_set(1, subframe=0.0)
    view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    evaluated_mesh = evaluated.to_mesh()
    try:
        evaluated_count = len(evaluated_mesh.vertices)
    finally:
        evaluated.to_mesh_clear()
    if evaluated_count <= len(vertices):
        raise RuntimeError("Subsurf must produce more evaluated than raw vertices")
    sample_indices = sorted({0, evaluated_count // 4, evaluated_count // 2,
                             3 * evaluated_count // 4, evaluated_count - 1})
    manifest = {
        "schema": "hrig.synthetic-diagnostic-fixture.v1",
        "case": case, "owner": OWNER, "names": names,
        "blender": {"version": list(bpy.app.version),
                    "build_hash": bpy.app.build_hash.decode("ascii", errors="replace")},
        "context": {"scene": scene.name, "view_layer": view_layer.name,
                    "frame": 1, "subframe": 0.0},
        "objects": [{"name": obj.name, "vertex_indices": sample_indices}],
        "bones": [{"armature": arm.name, "name": names["root"]},
                  {"armature": arm.name, "name": names["tip"]}],
        "frames": list(range(1, 9)),
        "construction": {"raw_vertex_count": len(vertices), "raw_polygon_count": len(faces),
                         "evaluated_vertex_count_at_frame1": evaluated_count,
                         "root_roll": ROOT_ROLL, "tip_roll": TIP_ROLL,
                         "modifier_order": [mod.type for mod in obj.modifiers],
                         "shape_value": SHAPE_VALUE,
                         "key_inputs": [list(row) for row in KEY_INPUTS],
                         "driver": {"bone": names["tip"], "path": "rotation_euler",
                                    "index": 2, "expression": "bend",
                                    "source_property": names["bend_angle"]},
                         "setup_evaluated_frame1": True},
        "status": "constructed_not_yet_independently_validated",
    }
    manifest["protection_baseline"] = protection(manifest)
    return manifest
