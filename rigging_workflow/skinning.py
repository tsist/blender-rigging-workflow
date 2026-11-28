"""Explicit volume candidates and non-destructive production-mesh binding."""
import hashlib
import json

import bpy
import numpy as np

from . import generation, skeleton_gate, volume
from .skinning_fields import combine_partitions, transfer_surface_field


def mesh_arrays(obj):
    if obj.type != 'MESH':
        raise ValueError('蒙皮输入必须是网格')
    coordinates = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
    obj.data.vertices.foreach_get('co', coordinates)
    matrix = np.array(obj.matrix_world, dtype=float)
    world = coordinates.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
    obj.data.calc_loop_triangles()
    triangles = np.asarray([tuple(t.vertices) for t in obj.data.loop_triangles], dtype=np.int64)
    return world, triangles


def geometry_fingerprint(obj):
    points, triangles = mesh_arrays(obj)
    return hashlib.sha256(points.tobytes() + triangles.tobytes()).hexdigest()


def topology_fingerprint(mesh):
    """RAW index topology for persisted explicit vertex scopes, not positions."""
    data=[len(mesh.vertices),[tuple(e.vertices) for e in mesh.edges],
          [l.vertex_index for l in mesh.loops],[(p.loop_start,p.loop_total) for p in mesh.polygons]]
    return hashlib.sha256(json.dumps(data,separators=(',',':')).encode('utf8')).hexdigest()


def bone_segments(rig, names):
    from . import rig_identity
    metadata = rig_identity.read(rig)
    if not names or len(set(names)) != len(names):
        raise ValueError('需要明确且无重复的蒙皮骨组')
    result = []
    for name in names:
        bone = rig.data.bones.get(name)
        if bone is None or not bone.use_deform or name not in metadata['deforms']:
            raise ValueError('求解骨不是该角色的最终蒙皮输出：' + name)
        result.append({'name': name, 'head': list(rig.matrix_world @ bone.head_local),
                       'tail': list(rig.matrix_world @ bone.tail_local)})
    return result


def solve_partition(rig, proxy, bone_names, settings, *, progress=None, cancel_check=None, sample_points=None):
    """Compute a complete array candidate; never write a partial Blender group."""
    skeleton_gate.require_pass(rig, operation='skinning.solve_partition')
    vertices, triangles = mesh_arrays(proxy)
    source = {'id': proxy.name, 'vertices': vertices, 'triangles': triangles}
    return volume.solve([source], bone_segments(rig, bone_names), settings,
                        progress=progress, cancel_check=cancel_check,
                        sample_points={proxy.name: sample_points} if sample_points is not None else None)




def create_bound_copy(target, rig, weights, bone_names, *, name, report, method='volume_diffusion'):
    """Commit a full new binding to a separate Mesh, preserving production topology."""
    skeleton_gate.require_pass(rig, operation='skinning.create_bound_copy')
    if bpy.context.mode != 'OBJECT' or target.type != 'MESH' or target.library:
        raise ValueError('新绑定需要对象模式下的本地生产网格')
    if target.vertex_groups or target.parent or target.constraints or target.animation_data or target.data.animation_data:
        raise ValueError('此入口仅接受已准备的中立新绑定副本；已有权重局修走独立流程')
    if any(vertex.groups for vertex in target.data.vertices):
        raise ValueError('生产源Mesh仍有孤立变形权重；不能把旧权重带入新候选')
    if any(m.type != 'SUBSURF' for m in target.modifiers):
        raise ValueError('新绑定副本只保留已确认的细分修改器')
    if name in bpy.data.objects:
        raise ValueError('蒙皮候选名称已存在，原候选保持')
    bone_segments(rig, bone_names)
    matrix = np.asarray(weights, dtype=np.float64)
    if (matrix.shape != (len(target.data.vertices), len(bone_names))
            or not np.isfinite(matrix).all() or (matrix < 0).any()
            or np.max(abs(matrix.sum(axis=1) - 1), initial=0) > 1e-5):
        raise ValueError('所有生产顶点都必须有完整、非负且和为一的候选权重')
    source_fingerprint = geometry_fingerprint(target)
    obj = None
    mesh = None
    try:
        obj = target.copy()
        mesh = target.data.copy()
        obj.data = mesh
        obj.name = name
        obj.data.name = name + '__网格'
        bpy.context.scene.collection.objects.link(obj)
        for column, bone_name in enumerate(bone_names):
            group = obj.vertex_groups.new(name=bone_name)
            for vertex in np.flatnonzero(matrix[:, column] > 0):
                group.add([int(vertex)], float(matrix[vertex, column]), 'REPLACE')
        modifier = obj.modifiers.new('C94体积蒙皮' if method == 'volume_diffusion' else 'C94权重绑定', 'ARMATURE')
        modifier.object = rig
        modifier.use_vertex_groups = True
        modifier.use_bone_envelopes = False
        obj.modifiers.move(len(obj.modifiers) - 1, 0)
        if geometry_fingerprint(obj) != source_fingerprint or geometry_fingerprint(target) != source_fingerprint:
            raise ValueError('生产几何保护校验失败')
        obj['c94_skinning_v1'] = json.dumps({'schema': 'c94.skinning.v1', 'source': target.name,
            'source_fingerprint': source_fingerprint, 'rig': rig.name,
            'method': method, 'report': report}, ensure_ascii=False, allow_nan=False)
        return obj
    except Exception:
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise
