"""Fresh, isolated Blender-native Cloth for captured metre-space trajectories.

The caller owns capture, proxy-to-rig transfer and editable baking. This module
only solves sequential integer frames in a private Scene; it retains no cache.
"""
from __future__ import annotations

import math
import time
import uuid

import numpy as np


_COMMON = {
    'quality': 8, 'mass': 0.15,
    'tension_stiffness': 40.0, 'compression_stiffness': 40.0,
    'shear_stiffness': 20.0, 'bending_stiffness': 0.5,
    'tension_damping': 5.0, 'compression_damping': 5.0,
    'shear_damping': 5.0, 'bending_damping': 0.5,
    'air_damping': 1.0,
    'collision_quality': 4, 'collision_distance': 0.003,
    'self_collision': False, 'self_distance': 0.003,
    'gravity': (0.0, 0.0, -9.81), 'max_seconds': 300.0,
}
_RANGES = {
    'quality': (1, 64), 'mass': (0.0001, 100.0),
    'tension_stiffness': (0.0, 10000.0),
    'compression_stiffness': (0.0, 10000.0),
    'shear_stiffness': (0.0, 10000.0),
    'bending_stiffness': (0.0, 10000.0),
    'tension_damping': (0.0, 50.0), 'compression_damping': (0.0, 50.0),
    'shear_damping': (0.0, 50.0), 'bending_damping': (0.0, 1000.0),
    'air_damping': (0.0, 10.0), 'collision_quality': (1, 32),
    # Keep above the native float RNA hard minimum (~0.00100000005).
    'collision_distance': (0.001001, 1.0),
    'self_distance': (0.001001, 0.1), 'max_seconds': (1.0, 7200.0),
}


def defaults(kind):
    """Return independent technical defaults, not calibrated material values."""
    if kind not in {'HAIR', 'SKIRT'}:
        raise ValueError('kind must be HAIR or SKIRT')
    values = dict(_COMMON)
    if kind == 'HAIR':
        values.update(mass=0.08, tension_stiffness=100.0,
                      compression_stiffness=100.0, shear_stiffness=50.0,
                      bending_stiffness=5.0, bending_damping=2.0)
    return values


def normalize_settings(kind, settings=None):
    """Validate before record mutation; return a new, fully populated dict."""
    values = defaults(kind)
    if settings is not None:
        if not isinstance(settings, dict):
            raise ValueError('settings must be a dict')
        unknown = set(settings) - set(values)
        if unknown:
            raise ValueError('unknown Cloth settings: ' + ', '.join(sorted(map(str, unknown))))
        values.update(settings)
    for key, (low, high) in _RANGES.items():
        value = values[key]
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
            raise ValueError(key + ' must be numeric')
        value = float(value)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} must be finite in [{low}, {high}]')
        if key in {'quality', 'collision_quality'}:
            if not value.is_integer():
                raise ValueError(key + ' must be integral')
            value = int(value)
        values[key] = value
    if not isinstance(values['self_collision'], (bool, np.bool_)):
        raise ValueError('self_collision must be boolean')
    values['self_collision'] = bool(values['self_collision'])
    try:
        gravity = np.asarray(values['gravity'], dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError('gravity must be a finite 3-vector') from exc
    if gravity.shape != (3,) or not np.isfinite(gravity).all() or np.max(np.abs(gravity)) > 1000:
        raise ValueError('gravity must be a finite 3-vector, each component <= 1000 m/s²')
    values['gravity'] = tuple(float(v) for v in gravity)
    return values


def _trajectory(value, label, frames=None):
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(label + ' must be a numeric (F,N,3) trajectory') from exc
    if (array.ndim != 3 or array.shape[2] != 3 or array.shape[0] < 1 or
            array.shape[0] > 4096 or array.shape[1] < 3 or array.shape[1] > 200000 or
            not np.isfinite(array).all()):
        raise ValueError(label + ' must be finite (1..4096,3..200000,3)')
    if frames is not None and array.shape[0] != frames:
        raise ValueError(label + ' frame count differs from base_world')
    if np.max(np.abs(array)) > 1e6:
        raise ValueError(label + ' exceeds metre-space coordinate bound')
    return array


def _faces(value, count, label):
    try:
        result = []
        for face in value:
            indices = tuple(face)
            if (len(indices) < 3 or any(isinstance(i, (bool, np.bool_)) or
                    not isinstance(i, (int, np.integer)) for i in indices) or
                    min(indices) < 0 or max(indices) >= count or len(set(indices)) != len(indices)):
                raise ValueError(label + ' contains an invalid polygon')
            result.append(tuple(int(i) for i in indices))
    except TypeError as exc:
        raise ValueError(label + ' must contain polygon index sequences') from exc
    if not result or len(result) > 400000:
        raise ValueError(label + ' needs 1..400000 polygons')
    return result


def simulate(base_world, faces, pin_indices, colliders, *, fps, kind, settings=None):
    """Solve native Cloth and return ``positions[F,N,3]`` in world metres.

    Input arrays are read-only to this routine. Only pins follow base_world
    after the first frame; free vertices start at the first captured shape.
    A fresh native Key/Action drives upstream geometry, with an explicit
    collision Collection admitted to the private physics ViewLayer.
    ``max_seconds`` is checked between native evaluations, not an OS deadline.
    """
    import bpy

    config = normalize_settings(kind, settings)
    base = _trajectory(base_world, 'base_world')
    frame_count, vertex_count, _ = base.shape
    polygons = _faces(faces, vertex_count, 'faces')
    if isinstance(fps, (bool, np.bool_)) or not isinstance(fps, (int, float, np.integer, np.floating)):
        raise ValueError('fps must be numeric')
    fps = float(fps)
    if not math.isfinite(fps) or not 1 <= fps <= 10000:
        raise ValueError('fps must be finite in [1,10000]')
    try:
        pins = tuple(pin_indices)
    except TypeError as exc:
        raise ValueError('pin_indices must be explicit nonempty vertex IDs') from exc
    if (not pins or any(isinstance(i, (bool, np.bool_)) or not isinstance(i, (int, np.integer)) for i in pins) or
            min(pins) < 0 or max(pins) >= vertex_count or len(set(pins)) != len(pins)):
        raise ValueError('pin_indices must be unique valid vertex IDs')
    pins = np.asarray(pins, dtype=np.int64)
    if not isinstance(colliders, (list, tuple)) or len(colliders) > 16:
        raise ValueError('colliders must be an explicit list of at most 16 trajectories')
    collider_data = []
    point_budget = frame_count * vertex_count
    for index, item in enumerate(colliders):
        if not isinstance(item, dict) or 'vertices' not in item or 'faces' not in item:
            raise ValueError('collider needs vertices and faces')
        trajectory = _trajectory(item['vertices'], f'collider[{index}]', frame_count)
        topology = _faces(item['faces'], trajectory.shape[1], f'collider[{index}].faces')
        collider_data.append((trajectory, topology, str(item.get('name', f'collider-{index}'))))
        point_budget += trajectory.shape[0] * trajectory.shape[1]
    if point_budget > 32000000:
        raise ValueError('captured trajectories exceed 32 million animated vertex samples')

    started = time.monotonic()
    token = 'RWCloth-' + uuid.uuid4().hex
    owned = {name: [] for name in ('scenes', 'collections', 'objects', 'meshes', 'shape_keys', 'actions')}
    window = bpy.context.window
    original_scene = window.scene if window else bpy.context.scene
    original_layer = window.view_layer if window else bpy.context.view_layer
    result = None
    cleanup_errors = []

    def own(category, item):
        owned[category].append(item)
        return item

    def check_time():
        if time.monotonic() - started > config['max_seconds']:
            raise TimeoutError('native Cloth exceeded max_seconds between evaluations')

    def make_mesh(scene, coordinates, topology, name, animate_pins=None):
        mesh = own('meshes', bpy.data.meshes.new(token + '-' + name))
        mesh.from_pydata(coordinates[0].tolist(), [], topology)
        mesh.update()
        obj = own('objects', bpy.data.objects.new(token + '-' + name, mesh))
        scene.collection.objects.link(obj)
        obj.shape_key_add(name='Basis', from_mix=False)
        keys = own('shape_keys', mesh.shape_keys)
        keys.use_relative = False
        key_times = [keys.key_blocks[0].frame]
        for index in range(1, frame_count):
            check_time()
            key = obj.shape_key_add(name=f'capture-{index+1}', from_mix=False)
            points = coordinates[index] if animate_pins is None else coordinates[0].copy()
            if animate_pins is not None:
                points[animate_pins] = coordinates[index, animate_pins]
            key.data.foreach_set('co', np.asarray(points, dtype=np.float32).ravel())
            key.interpolation = 'KEY_LINEAR'
            key_times.append(float(key.frame))
        keys.key_blocks[0].interpolation = 'KEY_LINEAR'
        action = own('actions', bpy.data.actions.new(token + '-' + name + '-capture'))
        slot = action.slots.new(id_type='KEY', name=keys.name)
        bag = action.layers.new('Captured world geometry').strips.new(type='KEYFRAME').channelbag(slot, ensure=True)
        curve = bag.fcurves.new(data_path='eval_time')
        curve.keyframe_points.add(frame_count)
        for index, keyframe in enumerate(curve.keyframe_points):
            keyframe.co = (index + 1, key_times[index])
            keyframe.interpolation = 'LINEAR'
        curve.update()
        keys.animation_data_create()
        keys.animation_data.action = action
        keys.animation_data.action_slot = slot
        return obj

    try:
        scene = own('scenes', bpy.data.scenes.new(token))
        scene.unit_settings.system = 'METRIC'
        scene.unit_settings.scale_length = 1.0
        scene.render.fps = int(math.ceil(fps))
        scene.render.fps_base = scene.render.fps / fps
        scene.frame_start = 1
        scene.frame_end = frame_count
        scene.gravity = config['gravity']
        scene.use_gravity = True
        layer = scene.view_layers[0]
        collection = own('collections', bpy.data.collections.new(token + '-colliders'))
        scene.collection.children.link(collection)
        proxy = make_mesh(scene, base, polygons, 'proxy', pins)
        group = proxy.vertex_groups.new(name=token + '-pins')
        group.add(pins.tolist(), 1.0, 'REPLACE')
        cloth = proxy.modifiers.new(name=token + '-Cloth', type='CLOTH')
        cs = cloth.settings
        for key in ('quality', 'mass', 'tension_stiffness', 'compression_stiffness',
                    'shear_stiffness', 'bending_stiffness', 'tension_damping',
                    'compression_damping', 'shear_damping', 'bending_damping', 'air_damping'):
            setattr(cs, key, config[key])
        cs.bending_model = 'ANGULAR'
        cs.vertex_group_mass = group.name
        cs.pin_stiffness = 1.0
        cs.use_dynamic_mesh = False
        collision = cloth.collision_settings
        collision.use_collision = bool(collider_data)
        collision.collection = collection
        collision.collision_quality = config['collision_quality']
        collision.distance_min = config['collision_distance']
        collision.use_self_collision = config['self_collision']
        collision.self_distance_min = config['self_distance']
        cloth.point_cache.frame_start = 1
        cloth.point_cache.frame_end = frame_count
        cloth.point_cache.frame_step = 1
        collider_objects = []
        for trajectory, topology, name in collider_data:
            obj = make_mesh(scene, trajectory, topology, name)
            collection.objects.link(obj)
            scene.collection.objects.unlink(obj)
            obj.modifiers.new(name=token + '-Collision', type='COLLISION')
            obj.collision.thickness_outer = config['collision_distance']
            obj.collision.thickness_inner = config['collision_distance']
            obj.collision.use_culling = False
            obj.collision.use_normal = False
            collider_objects.append(obj)
        if window:
            window.scene = scene
            window.view_layer = layer
        # RNA foreach_get writes into the supplied flat buffer. Keep output
        # C-contiguous even when callers supply Fortran/strided trajectories;
        # ravel() of a noncontiguous slice would otherwise be a discarded copy.
        positions = np.empty_like(base, order='C')
        with bpy.context.temp_override(scene=scene, view_layer=layer):
            layer.update()
            members = {obj.as_pointer() for obj in layer.objects}
            if any(obj.as_pointer() not in members for obj in [proxy] + collider_objects):
                raise RuntimeError('physics object is not admitted to private ViewLayer')
            for index in range(frame_count):
                check_time()
                scene.frame_set(index + 1, subframe=0.0)
                graph = bpy.context.evaluated_depsgraph_get()
                evaluated = proxy.evaluated_get(graph)
                if not evaluated.is_evaluated or len(evaluated.data.vertices) != vertex_count:
                    raise RuntimeError('native Cloth evaluated geometry identity mismatch')
                evaluated.data.vertices.foreach_get('co', positions[index].ravel())
                if not np.isfinite(positions[index]).all():
                    raise RuntimeError('native Cloth produced nonfinite positions')
                for obj in collider_objects:
                    if not obj.evaluated_get(graph).is_evaluated:
                        raise RuntimeError('collision object was not evaluated')
            pin_error = float(np.max(np.linalg.norm(positions[:, pins] - base[:, pins], axis=2)))
            if pin_error > 0.0001:
                raise RuntimeError(f'native Cloth pin drift {pin_error:.9g}m exceeds 0.1mm')
            result = {'positions': positions, 'statistics': {
                'backend': 'BLENDER_NATIVE_CLOTH', 'kind': kind,
                'frames': frame_count, 'vertices': vertex_count, 'pins': len(pins),
                'colliders': len(collider_objects), 'fps': fps,
                'pin_max_error_m': pin_error,
                'max_delta_from_base_m': float(np.max(np.linalg.norm(positions - base, axis=2))),
                'elapsed_seconds': time.monotonic() - started,
                'fresh_scene': True, 'fresh_cloth': True,
                'sequential_integer_evaluation': True,
                'settings': dict(config),
                'animation': 'ABSOLUTE_SHAPE_KEYS_NATIVE_KEY_ACTION',
                'timeout_semantics': 'checked_between_native_evaluations',
            }}
    finally:
        if window:
            try:
                window.scene = original_scene
                window.view_layer = original_layer
            except Exception as exc:
                cleanup_errors.append('context restore: ' + str(exc))
        # Remove only exact IDs returned by this call, never an all-ID diff.
        for category in ('scenes', 'collections', 'objects', 'meshes', 'shape_keys', 'actions'):
            database = getattr(bpy.data, category)
            for item in reversed(owned[category]):
                try:
                    item.as_pointer()
                    database.remove(item, do_unlink=True)
                except ReferenceError:
                    pass  # Removing an owning Mesh may already remove its Key.
                except Exception as exc:
                    cleanup_errors.append(category + ': ' + str(exc))
        if cleanup_errors:
            raise RuntimeError('native Cloth cleanup failed: ' + '; '.join(cleanup_errors))
    result['statistics']['owned_ids_removed'] = sum(len(items) for items in owned.values())
    result['statistics']['cleanup_complete'] = True
    return result
