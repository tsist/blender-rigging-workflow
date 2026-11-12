"""World-space surface queries and selection centers for positioning inspection.

This module is intentionally limited to read-only geometry inspection.  It does
not create UI, markers, armatures, or reference bones, and it never writes mesh
data, object transforms, selection flags, the 3D cursor, or the active mode.

The surface target is built in world space.  In particular, a local-space BVH is
never used to approximate a world-space nearest query: non-uniform scale and
reflecting (negative-determinant) object transforms are both evaluated before
the BVH is built.
"""
from __future__ import annotations

import math

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree


_CENTER_MODES = ('MEAN', 'BBOX')


def _object_name(obj):
    name = getattr(obj, 'name', None)
    return str(name) if name else '<未命名对象>'


def _require_mesh_object(obj):
    if not isinstance(obj, bpy.types.Object):
        raise ValueError('几何目标必须是明确的 Blender 对象，不能是名称、ID 属性或空值')
    name = _object_name(obj)
    if obj.type != 'MESH':
        raise ValueError(f'几何目标必须是 MESH 对象：{name}')
    if obj.data is None or not isinstance(obj.data, bpy.types.Mesh):
        raise ValueError(f'几何目标缺少网格数据：{name}')
    return name


def _depsgraph_for(depsgraph):
    if depsgraph is not None:
        return depsgraph
    # Force object transforms and modifiers to reflect the current scene before
    # taking the evaluated dependency graph.  This is a read-only update.
    bpy.context.view_layer.update()
    return bpy.context.evaluated_depsgraph_get()


def _evaluated_object(obj, depsgraph, name):
    try:
        evaluated = obj.evaluated_get(depsgraph)
    except Exception as exc:
        raise ValueError(f'无法取得对象当前求值结果：{name}') from exc
    if evaluated is None:
        raise ValueError(f'对象没有可用的当前求值结果：{name}')
    return evaluated


def _world_matrix(evaluated, name):
    matrix = evaluated.matrix_world
    try:
        array = np.asarray(matrix, dtype=np.float64)
    except Exception as exc:
        raise ValueError(f'无法读取对象世界变换：{name}') from exc
    if array.shape != (4, 4) or not np.isfinite(array).all():
        raise ValueError(f'对象世界变换包含非有限值：{name}')
    return matrix, array


def _surface_orientation(evaluated, name):
    matrix, array = _world_matrix(evaluated, name)
    determinant = float(np.linalg.det(array[:3, :3]))
    if not math.isfinite(determinant):
        raise ValueError(f'对象世界变换行列式非有限：{name}')
    if determinant == 0.0:
        raise ValueError(f'对象世界变换退化（行列式为 0），无法确定几何外法线：{name}')
    return matrix, determinant < 0.0


def _world_points(local, matrix, name):
    try:
        points = np.asarray(local, dtype=np.float64)
    except Exception as exc:
        raise ValueError(f'无法读取对象几何坐标：{name}') from exc
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f'对象几何坐标不是三维点集：{name}')
    if len(points) == 0:
        raise ValueError(f'对象没有可用的几何顶点：{name}')
    transform = np.asarray(matrix, dtype=np.float64)
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        raise ValueError(f'对象世界变换包含非有限值：{name}')
    world = points @ transform[:3, :3].T + transform[:3, 3]
    if not np.isfinite(world).all():
        raise ValueError(f'对象几何包含非有限世界坐标：{name}')
    return world


def _vector3(value, label):
    try:
        vector = Vector(value)
    except Exception as exc:
        raise ValueError(f'{label}必须是包含三个有限数值的坐标') from exc
    if len(vector) != 3 or not all(math.isfinite(float(component)) for component in vector):
        raise ValueError(f'{label}必须是包含三个有限数值的坐标')
    return vector


def _finite_offset(value):
    try:
        offset = float(value)
    except Exception as exc:
        raise ValueError('offset 必须是有限数值') from exc
    if not math.isfinite(offset):
        raise ValueError('offset 必须是有限数值')
    return offset


def _evaluated_mesh(evaluated, depsgraph, name):
    try:
        mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
    except Exception as exc:
        raise ValueError(f'无法读取对象当前求值网格：{name}') from exc
    if mesh is None:
        raise ValueError(f'对象没有可用的当前求值网格：{name}')
    return mesh


def _mesh_vertices_world(evaluated, mesh, name):
    count = len(mesh.vertices)
    if count == 0:
        raise ValueError(f'对象没有可用于几何查询的顶点：{name}')
    coordinates = np.empty(count * 3, dtype=np.float64)
    try:
        mesh.vertices.foreach_get('co', coordinates)
    except Exception as exc:
        raise ValueError(f'无法读取对象求值网格顶点：{name}') from exc
    matrix, _ = _world_matrix(evaluated, name)
    return _world_points(coordinates.reshape(-1, 3), matrix, name)


def _mesh_triangles(mesh, name):
    try:
        mesh.calc_loop_triangles()
    except Exception as exc:
        raise ValueError(f'无法计算对象求值网格的三角面：{name}') from exc
    triangle_count = len(mesh.loop_triangles)
    if triangle_count == 0:
        raise ValueError(f'对象没有可用于几何查询的三角面：{name}')
    indices = np.empty(triangle_count * 3, dtype=np.int32)
    polygon_indices = np.empty(triangle_count, dtype=np.int32)
    try:
        mesh.loop_triangles.foreach_get('vertices', indices)
        mesh.loop_triangles.foreach_get('polygon_index', polygon_indices)
    except Exception as exc:
        raise ValueError(f'无法读取对象求值网格的三角面索引：{name}') from exc
    return indices.reshape(-1, 3), polygon_indices


def _hidden(element):
    return bool(getattr(element, 'hide', False))


def _object_selected_faces(obj, name):
    """Read explicit face selection from object-mode mesh flags."""
    selected = set()
    for polygon in obj.data.polygons:
        if not polygon.select or _hidden(polygon):
            continue
        if any(_hidden(obj.data.vertices[int(index)]) for index in polygon.vertices):
            continue
        selected.add(int(polygon.index))
    if not selected:
        raise ValueError(
            f'选区模式没有选中可见面：{name}；请显式选择面，'
            '或使用 selected_only=False 查询整个求值网格'
        )
    return selected


def _edit_bmesh(obj, name):
    if not getattr(obj.data, 'is_editmode', False):
        raise ValueError(f'对象不在编辑模式，无法读取原生编辑选择：{name}')
    try:
        mesh = bmesh.from_edit_mesh(obj.data)
    except Exception as exc:
        raise ValueError(f'无法读取对象编辑模式网格：{name}') from exc
    if mesh is None:
        raise ValueError(f'对象编辑模式网格不可用：{name}')
    # Index refresh only updates BMesh lookup caches; it does not change the
    # mesh, selection, mode, or any native edit data.
    mesh.verts.index_update()
    mesh.faces.index_update()
    mesh.verts.ensure_lookup_table()
    mesh.faces.ensure_lookup_table()
    return mesh


def _edit_selected_faces(bm, name):
    selected = {
        int(face.index)
        for face in bm.faces
        if face.select
        and not face.hide
        and not any(vertex.hide for vertex in face.verts)
    }
    if not selected:
        raise ValueError(
            f'选区模式没有选中可见面：{name}；请显式选择面，'
            '或使用 selected_only=False 查询整个求值网格'
        )
    return selected


def _edit_selected_vertex_indices(bm, name):
    selected = set()
    for vertex in bm.verts:
        if vertex.select and not vertex.hide:
            selected.add(int(vertex.index))
    for edge in bm.edges:
        if edge.select and not edge.hide:
            selected.update(
                int(vertex.index) for vertex in edge.verts if not vertex.hide
            )
    for face in bm.faces:
        if face.select and not face.hide:
            selected.update(
                int(vertex.index) for vertex in face.verts if not vertex.hide
            )
    if not selected:
        raise ValueError(
            f'编辑模式没有选中可见顶点：{name}；请先选择顶点、边或面，'
            '或使用 selected_only=False 查询整个部件'
        )
    return sorted(selected)


def _topology_error(name):
    return ValueError(
        f'选区无法可靠映射到当前求值网格拓扑：{name}；'
        '请使用无修改器的中立快照或 selected_only=False'
    )


def _require_mesh_topology_matches(source, evaluated, name):
    """Verify source face/vertex indices still address the evaluated mesh."""
    if (
        len(source.vertices) != len(evaluated.vertices)
        or len(evaluated.polygons) != len(source.polygons)
        or len(evaluated.loops) != len(source.loops)
    ):
        raise _topology_error(name)
    for source_polygon, evaluated_polygon in zip(
        source.polygons, evaluated.polygons
    ):
        if len(source_polygon.vertices) != len(evaluated_polygon.vertices):
            raise _topology_error(name)
        if any(
            int(a) != int(b)
            for a, b in zip(source_polygon.vertices, evaluated_polygon.vertices)
        ):
            raise _topology_error(name)


def _require_edit_topology_matches(evaluated, bm, name):
    """Verify BMesh face/vertex indices still address the evaluated mesh."""
    if len(evaluated.vertices) != len(bm.verts) or len(evaluated.polygons) != len(bm.faces):
        raise _topology_error(name)
    for face in bm.faces:
        if face.index < 0 or face.index >= len(evaluated.polygons):
            raise _topology_error(name)
        polygon = evaluated.polygons[face.index]
        if len(polygon.vertices) != len(face.verts):
            raise _topology_error(name)
        if any(
            int(polygon.vertices[offset]) != int(vertex.index)
            for offset, vertex in enumerate(face.verts)
        ):
            raise _topology_error(name)


def _evaluated_surface_geometry(
    obj, evaluated, depsgraph, name, selected_only, edit_bm=None
):
    """Return world vertices, oriented triangles, and original face indices."""
    if selected_only and len(obj.modifiers) > 0:
        raise ValueError(
            f'选区模式只支持无修改器的原生几何，不能把选择可靠映射到求值拓扑：{name}'
        )
    matrix, reflected = _surface_orientation(evaluated, name)
    mesh = _evaluated_mesh(evaluated, depsgraph, name)
    try:
        if selected_only:
            if edit_bm is not None:
                _require_edit_topology_matches(mesh, edit_bm, name)
                selected = _edit_selected_faces(edit_bm, name)
            else:
                _require_mesh_topology_matches(obj.data, mesh, name)
                selected = _object_selected_faces(obj, name)
        world = _mesh_vertices_world(evaluated, mesh, name)
        indices, polygon_indices = _mesh_triangles(mesh, name)

        if selected_only:
            mask = np.isin(
                polygon_indices,
                np.fromiter(selected, dtype=np.int64, count=len(selected)),
            )
            if not mask.any():
                raise ValueError(f'选中面没有可用于几何查询的三角面：{name}')
            indices = indices[mask]
            polygon_indices = polygon_indices[mask]
    finally:
        evaluated.to_mesh_clear()

    vertex_count = len(world)
    if (
        indices.size == 0
        or int(indices.min()) < 0
        or int(indices.max()) >= vertex_count
    ):
        raise ValueError(f'求值网格三角面包含无效顶点索引：{name}')

    a = world[indices[:, 0]]
    b = world[indices[:, 1]]
    c = world[indices[:, 2]]
    cross = np.cross(b - a, c - a)
    lengths = np.linalg.norm(cross, axis=1)
    valid = lengths > 0.0
    if not valid.any():
        raise ValueError(f'目标几何没有可用于查询的非退化表面：{name}')
    if not valid.all():
        indices = indices[valid]
        polygon_indices = polygon_indices[valid]
        cross = cross[valid]
        lengths = lengths[valid]
    normals = cross / lengths[:, None]
    if reflected:
        # The world-space triangle winding is reversed by a negative
        # determinant; flip both the geometry order and the stored normal so
        # BVH normals and offset directions agree with the source winding.
        indices = indices[:, (0, 2, 1)]
        normals = -normals

    return world, indices, polygon_indices, normals


class SurfaceTarget:
    """A world-space BVH for one explicit evaluated mesh target.

    ``selected_only=False`` uses the whole evaluated mesh.  ``selected_only=True``
    uses explicit selected visible faces.  Selection with modifiers is rejected
    rather than guessed, because there is no reliable mapping from source face
    indices to arbitrary evaluated topology.  Native Edit Mode selection is read
    directly from the current BMesh without changing mode.

    ``nearest`` and ``ray`` return ``surface_position`` as the unoffset hit and
    ``position`` as ``surface_position + normal * offset``.  Positive offsets go
    along the geometric outward face normal; negative offsets go inward.  A hit
    exactly on an edge or corner uses one adjacent triangle's face normal.
    """

    def __init__(self, obj, selected_only=False, depsgraph=None):
        name = _require_mesh_object(obj)
        if not isinstance(selected_only, bool):
            raise ValueError('selected_only 必须是明确的布尔值')
        self.object_name = name
        self.selected_only = selected_only
        self.depsgraph = _depsgraph_for(depsgraph)
        self._source_object = obj
        evaluated = _evaluated_object(obj, self.depsgraph, name)

        edit_bm = None
        if selected_only and getattr(obj.data, 'is_editmode', False):
            edit_bm = _edit_bmesh(obj, name)
        world, indices, polygon_indices, normals = _evaluated_surface_geometry(
            obj, evaluated, self.depsgraph, name, selected_only, edit_bm
        )

        try:
            self._bvh = BVHTree.FromPolygons(
                world.tolist(),
                indices.tolist(),
                all_triangles=True,
                epsilon=0.0,
            )
        except Exception as exc:
            raise ValueError(f'无法从目标当前可见几何建立世界坐标 BVH：{name}') from exc
        if self._bvh is None:
            raise ValueError(f'目标当前可见几何无法建立世界坐标 BVH：{name}')

        self._polygon_indices = polygon_indices.tolist()
        self._triangle_normals = np.asarray(
            [tuple(float(component) for component in normal) for normal in normals],
            dtype=np.float64,
        )
        if not self._polygon_indices or len(self._polygon_indices) != len(self._triangle_normals):
            raise ValueError(f'世界坐标 BVH 的三角面映射无效：{name}')

    def _result(self, location, triangle_index, offset):
        if triangle_index is None or int(triangle_index) < 0:
            raise ValueError('几何查询返回了无效的三角面索引')
        triangle_index = int(triangle_index)
        if triangle_index >= len(self._polygon_indices):
            raise ValueError('几何查询返回了越界的三角面索引')
        surface_position = _vector3(location, '表面命中位置')
        normal = Vector(self._triangle_normals[triangle_index])
        if normal.length_squared == 0.0 or not all(
            math.isfinite(float(component)) for component in normal
        ):
            raise ValueError('几何查询得到无效的外法线')
        normal.normalize()
        position = surface_position + normal * offset
        if not all(math.isfinite(float(component)) for component in position):
            raise ValueError('几何查询得到非有限的偏移位置')
        return {
            'position': [float(component) for component in position],
            'surface_position': [float(component) for component in surface_position],
            'normal': [float(component) for component in normal],
            'polygon_index': int(self._polygon_indices[triangle_index]),
        }

    def nearest(self, point, offset=0.0):
        """Return the nearest world surface point and its normal/offset result."""
        query = _vector3(point, '查询点')
        offset = _finite_offset(offset)
        try:
            location, _normal, index, _distance = self._bvh.find_nearest(query)
        except Exception as exc:
            raise ValueError('世界坐标最近表面查询失败') from exc
        if location is None:
            raise ValueError('世界坐标最近表面查询没有命中目标几何')
        return self._result(location, index, offset)

    def ray(self, origin, direction, offset=0.0, max_distance=1e6):
        """Return the first world ray hit, or ``None`` when the ray misses."""
        ray_origin = _vector3(origin, '射线起点')
        try:
            ray_direction = Vector(direction)
        except Exception as exc:
            raise ValueError('射线方向必须是包含三个有限数值的向量') from exc
        if len(ray_direction) != 3 or not all(
            math.isfinite(float(component)) for component in ray_direction
        ):
            raise ValueError('射线方向必须是包含三个有限数值的向量')
        if ray_direction.length_squared == 0.0:
            raise ValueError('射线方向不能是零向量')
        ray_direction.normalize()
        offset = _finite_offset(offset)
        try:
            distance = float(max_distance)
        except Exception as exc:
            raise ValueError('max_distance 必须是有限且非负的数值') from exc
        if not math.isfinite(distance) or distance < 0.0:
            raise ValueError('max_distance 必须是有限且非负的数值')
        if distance == 0.0:
            return None
        try:
            location, _normal, index, _hit_distance = self._bvh.ray_cast(
                ray_origin, ray_direction, distance
            )
        except Exception as exc:
            raise ValueError('世界坐标射线查询失败') from exc
        if location is None or index is None or int(index) < 0:
            return None
        return self._result(location, index, offset)


def _object_selected_vertex_indices(obj, evaluated_mesh, name):
    """Return unique source vertex indices for selected vertices/edges/faces."""
    if (
        len(evaluated_mesh.vertices) != len(obj.data.vertices)
        or len(evaluated_mesh.polygons) != len(obj.data.polygons)
    ):
        raise ValueError(
            f'选区顶点无法可靠映射到当前求值网格：{name}；'
            '请使用无修改器的中立快照或 selected_only=False'
        )
    selected = set()
    for vertex in obj.data.vertices:
        if vertex.select and not _hidden(vertex):
            selected.add(int(vertex.index))
    for edge in obj.data.edges:
        if edge.select and not _hidden(edge):
            selected.update(
                int(index)
                for index in edge.vertices
                if not _hidden(obj.data.vertices[int(index)])
            )
    for polygon in obj.data.polygons:
        if polygon.select and not _hidden(polygon):
            selected.update(
                int(index)
                for index in polygon.vertices
                if not _hidden(obj.data.vertices[int(index)])
            )
    if not selected:
        raise ValueError(
            f'选区模式没有选中可见顶点：{name}；请先选择顶点、边或面，'
            '或使用 selected_only=False 查询整个部件'
        )
    vertex_count = len(obj.data.vertices)
    if min(selected) < 0 or max(selected) >= vertex_count:
        raise ValueError(f'选区引用了越界的顶点索引：{name}')
    return sorted(selected)


def _object_points_for_center(obj, depsgraph, selected_only, name):
    evaluated = _evaluated_object(obj, depsgraph, name)
    matrix, _array = _world_matrix(evaluated, name)
    if selected_only and len(obj.modifiers) > 0:
        raise ValueError(
            f'选区模式只支持无修改器的原生几何，不能把选择可靠映射到求值拓扑：{name}'
        )

    edit_bm = None
    if selected_only and getattr(obj.data, 'is_editmode', False):
        edit_bm = _edit_bmesh(obj, name)

    mesh = _evaluated_mesh(evaluated, depsgraph, name)
    try:
        count = len(mesh.vertices)
        if count == 0:
            raise ValueError(f'对象没有可用于中心查询的顶点：{name}')
        coordinates = np.empty(count * 3, dtype=np.float64)
        try:
            mesh.vertices.foreach_get('co', coordinates)
        except Exception as exc:
            raise ValueError(f'无法读取对象求值网格顶点：{name}') from exc
        coordinates = coordinates.reshape(-1, 3)

        if selected_only:
            if edit_bm is not None:
                _require_edit_topology_matches(mesh, edit_bm, name)
                indices = _edit_selected_vertex_indices(edit_bm, name)
            else:
                _require_mesh_topology_matches(obj.data, mesh, name)
                indices = _object_selected_vertex_indices(obj, mesh, name)
            coordinates = coordinates[indices]
        return _world_points(coordinates, matrix, name)
    finally:
        evaluated.to_mesh_clear()


def _resolve_objects(objects):
    if isinstance(objects, (bpy.types.Object, str)):
        items = [objects]
    else:
        try:
            items = list(objects)
        except TypeError as exc:
            raise ValueError('objects 必须是明确的 MESH 对象或对象名列表') from exc
    if not items:
        raise ValueError('selection_center 至少需要一个明确的 MESH 对象')

    resolved = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            obj = bpy.data.objects.get(item)
            if obj is None:
                raise ValueError(f'对象不存在或名称不唯一：{item}')
        elif isinstance(item, bpy.types.Object):
            obj = item
        else:
            raise ValueError('objects 中只能包含 Blender 对象或明确的对象名')
        pointer = int(obj.as_pointer())
        if pointer in seen:
            raise ValueError(f'objects 包含重复对象，选择汇总存在歧义：{_object_name(obj)}')
        seen.add(pointer)
        resolved.append(obj)
    return resolved


def selection_center(objects, mode='MEAN', selected_only=True, depsgraph=None):
    """Return a world-space center of unique selected or whole-part vertices.

    ``MEAN`` is the arithmetic mean of unique world-space vertices.  ``BBOX`` is
    the midpoint of their world-space bounding range.  Face and edge selections
    contribute their associated unique vertices.  Each object instance is
    transformed by its own evaluated world matrix; linked mesh data reused by
    multiple objects is counted once per instance.

    The function does not move objects, change the 3D cursor, alter selection
    flags, or change Edit/Object mode.
    """
    if not isinstance(mode, str):
        raise ValueError('中心模式必须是 MEAN 或 BBOX')
    normalized_mode = mode.strip().upper()
    if normalized_mode not in _CENTER_MODES:
        raise ValueError(f'未知的中心模式：{mode}；只支持 MEAN 或 BBOX')
    if not isinstance(selected_only, bool):
        raise ValueError('selected_only 必须是明确的布尔值')

    resolved = _resolve_objects(objects)
    graph = _depsgraph_for(depsgraph)

    total = 0
    coordinate_sum = np.zeros(3, dtype=np.float64)
    minimum = np.full(3, np.inf, dtype=np.float64)
    maximum = np.full(3, -np.inf, dtype=np.float64)
    names = []

    for obj in resolved:
        name = _require_mesh_object(obj)
        names.append(name)
        points = _object_points_for_center(obj, graph, selected_only, name)
        if len(points) == 0:
            raise ValueError(f'对象没有可汇总的几何顶点：{name}')
        total += len(points)
        coordinate_sum += points.sum(axis=0)
        minimum = np.minimum(minimum, points.min(axis=0))
        maximum = np.maximum(maximum, points.max(axis=0))

    if total == 0:
        raise ValueError('selection_center 没有可汇总的几何顶点')
    if normalized_mode == 'MEAN':
        position = coordinate_sum / float(total)
    else:
        position = (minimum + maximum) * 0.5
    if not np.isfinite(position).all():
        raise ValueError('selection_center 得到非有限的中心坐标')

    return {
        'position': [float(component) for component in position],
        'vertex_count': int(total),
        'objects': names,
        'mode': normalized_mode,
    }
