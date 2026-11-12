"""Read-only world-space triangle queries. SPDX-License-Identifier: GPL-3.0-or-later.

Callers supply actual evaluated ``mesh.loop_triangles`` vertex indices, after
world transformation. This module never triangulates/reorients polygons and
does not import bpy, edit a scene, decide contact success, or expose UI hints.

Design reference only: libigl b41f1944a703653ae0da81e6588650ba14c270ea,
include/igl/signed_distance.h and pseudonormal_test.cpp (MPL-2.0): separate face,
uniform edge, and corner-angle-weighted vertex pseudonormals. This independent
NumPy implementation does not copy its C++ branches or fixed face threshold.

An oriented local pseudonormal projection is NOT a signed Euclidean distance
or a solid inside test. Even closed, consistently wound input may self-intersect
or contain nested components. Solid classification is deliberately unknown.
"""
from __future__ import annotations

import copy
import heapq
import math

import numpy as np


_EPS = np.finfo(np.float64).eps


def _indices(value, shape_tail, label):
    array = np.asarray(value)
    if array.dtype.kind not in 'iu' or array.ndim != 1 + len(shape_tail) or array.shape[1:] != shape_tail:
        raise ValueError(f'{label} must be an integer array with trailing shape {shape_tail}')
    if array.dtype.kind == 'u' and array.size and int(array.max()) > np.iinfo(np.int64).max:
        raise ValueError(f'{label} exceeds int64')
    return np.array(array, dtype=np.int64, copy=True)


def _segment(point, a, b):
    edge = b - a
    squared = float(edge @ edge)
    fraction = 0.0 if squared == 0 else float(np.clip((point - a) @ edge / squared, 0, 1))
    return a + fraction * edge


class SurfaceQuery:
    """Immutable snapshot, NumPy AABB BVH, Euclidean nearest features.

    ``vertices``: finite (N,3) world coordinates, N>0.
    ``triangles``: integer (M,3), M>0, actual loop-triangle vertex indices.
    ``triangle_ids``: optional unique nonnegative (M,) integer source IDs.

    ``query(point)`` returns a JSON-compatible dictionary. ``query_many`` returns
    one such dictionary per point; it does not silently broadcast invalid input.
    ``qualification`` reports topology independently of local normal usability.
    Boundary features, nonmanifold stars, conflicting winding, degenerate normal
    support, unresolved ties and inadequate coordinate resolution are ambiguous.
    Open-face interiors can have local normals, but solid classification remains
    unknown. No result is a collision/contact acceptance decision.
    """

    def __init__(self, vertices, triangles, triangle_ids=None):
        points = np.array(vertices, dtype=np.float64, copy=True)
        if points.ndim != 2 or points.shape[1:] != (3,) or not len(points) or not np.isfinite(points).all():
            raise ValueError('vertices must be a nonempty finite (N,3) array')
        faces = _indices(triangles, (3,), 'triangles')
        if not len(faces) or np.any(faces < 0) or np.any(faces >= len(points)):
            raise ValueError('triangles must be nonempty with valid nonnegative vertex indices')
        ids = np.arange(len(faces), dtype=np.int64) if triangle_ids is None else _indices(triangle_ids, (), 'triangle_ids')
        if ids.shape != (len(faces),) or np.any(ids < 0) or len(np.unique(ids)) != len(ids):
            raise ValueError('triangle_ids must be unique nonnegative IDs, one per triangle')
        low, high = points.min(axis=0), points.max(axis=0)
        with np.errstate(over='ignore', invalid='ignore'):
            extent = high - low
        if not np.isfinite(extent).all():
            raise ValueError('world coordinate range exceeds finite arithmetic')
        self._scale = float(np.max(extent)) or 1.0
        self._origin = low + extent * 0.5
        self._vertices = (points - self._origin) / self._scale
        self._world = points
        self._triangles = faces
        self._ids = ids
        # Error budget in normalized geometry units, including loss from the
        # magnitude of the supplied world coordinates. No metre-specific gate.
        self._tolerance = 32 * _EPS * max(1.0, float(np.max(np.abs(points))) / self._scale)
        self._edges = {}
        self._vertex_faces = [set() for _ in points]
        self._normals = np.zeros((len(faces), 3))
        self._normal_errors = np.zeros(len(faces))
        self._angles = np.zeros((len(faces), 3))
        self._degenerate = np.zeros(len(faces), dtype=bool)
        for face, indices in enumerate(faces):
            a, b, c = self._vertices[indices]
            edges = np.asarray((b-a, c-b, a-c))
            longest = float(np.max(np.linalg.norm(edges, axis=1)))
            normal = np.cross(b-a, c-a)
            area2 = float(np.linalg.norm(normal))
            area_error = 32 * _EPS * longest**2 + 4 * self._tolerance * longest
            bad = len(set(map(int, indices))) != 3 or area2 <= area_error
            self._degenerate[face] = bad
            if not bad:
                self._normals[face] = normal / area2
                self._normal_errors[face] = area_error / area2
                for corner in range(3):
                    u = self._vertices[indices[(corner+1) % 3]] - self._vertices[indices[corner]]
                    v = self._vertices[indices[(corner+2) % 3]] - self._vertices[indices[corner]]
                    self._angles[face, corner] = math.atan2(float(np.linalg.norm(np.cross(u, v))), float(u @ v))
            for vertex in indices:
                self._vertex_faces[int(vertex)].add(face)
            for u, v in zip(indices, np.roll(indices, -1)):
                u, v = int(u), int(v)
                key = tuple(sorted((u, v)))
                self._edges.setdefault(key, []).append((face, 1 if u < v else -1))
        self._boundary = {e for e, rows in self._edges.items() if len(rows) == 1}
        self._nonmanifold = {e for e, rows in self._edges.items() if len(rows) > 2 or e[0] == e[1]}
        self._orientation_conflicts = {e for e, rows in self._edges.items() if len(rows) == 2 and rows[0][1] == rows[1][1]}
        self._boundary_vertices = {v for e in self._boundary for v in e}
        self._bad_vertices = {v for e in self._nonmanifold for v in e}
        self._conflict_vertices = {v for e in self._orientation_conflicts for v in e}
        # Edge-manifoldness alone misses closed shells touching at one vertex.
        # A manifold vertex link is one cycle (interior) or one path (boundary).
        for vertex, incident in enumerate(self._vertex_faces):
            if not incident:
                continue
            link = {}
            degrees = {}
            malformed = False
            for face in incident:
                other = [int(v) for v in faces[face] if int(v) != vertex]
                if len(other) != 2 or other[0] == other[1]:
                    malformed = True
                    break
                u, v = other
                link.setdefault(u, set()).add(v)
                link.setdefault(v, set()).add(u)
                degrees[u] = degrees.get(u, 0) + 1
                degrees[v] = degrees.get(v, 0) + 1
            if malformed:
                self._bad_vertices.add(vertex)
                continue
            visited, todo = set(), [next(iter(link))]
            while todo:
                node = todo.pop()
                if node not in visited:
                    visited.add(node)
                    todo.extend(link[node] - visited)
            values = list(degrees.values())
            manifold = len(visited) == len(link) and (all(d == 2 for d in values) or
                (values.count(1) == 2 and all(d in (1, 2) for d in values)))
            if not manifold:
                self._bad_vertices.add(vertex)
        degenerate_ids = ids[self._degenerate].tolist()
        manifold = not self._nonmanifold and not self._bad_vertices and not degenerate_ids
        oriented = not self._orientation_conflicts and not degenerate_ids and not self._nonmanifold and not self._bad_vertices
        self._qualification = {
            'vertex_count': len(points), 'triangle_count': len(faces),
            'boundary_edge_count': len(self._boundary), 'has_boundary': bool(self._boundary),
            'nonmanifold_edge_count': len(self._nonmanifold),
            'nonmanifold_vertex_indices': sorted(self._bad_vertices),
            'orientation_conflict_edges': [list(e) for e in sorted(self._orientation_conflicts)],
            'degenerate_triangle_ids': degenerate_ids, 'manifold': bool(manifold),
            'consistently_oriented': bool(oriented),
            'closed_oriented_manifold': bool(manifold and oriented and not self._boundary),
            'normal_orientation': 'input triangle winding; global outward orientation not certified',
            'self_intersection_status': 'not_checked', 'solid_classification': 'unknown',
            'coordinate_scale': self._scale, 'feature_tolerance_world': self._tolerance * self._scale,
            'tolerance_policy': '32*float64_epsilon*max(1,max_abs_world/extent), in normalized coordinates',
        }
        xyz = self._vertices[faces]
        self._triangle_low, self._triangle_high = xyz.min(axis=1), xyz.max(axis=1)
        self._nodes = []
        self._build(np.arange(len(faces), dtype=np.int64))
        for array in (self._world, self._vertices, self._triangles, self._ids, self._normals, self._angles):
            array.setflags(write=False)

    @property
    def qualification(self):
        return copy.deepcopy(self._qualification)

    def _build(self, indices):
        node = len(self._nodes)
        low, high = self._triangle_low[indices].min(axis=0), self._triangle_high[indices].max(axis=0)
        self._nodes.append(None)
        if len(indices) <= 8:
            self._nodes[node] = (low, high, indices, None, None)
        else:
            centers = (self._triangle_low[indices] + self._triangle_high[indices]) * 0.5
            axis = int(np.argmax(np.ptp(centers, axis=0)))
            ordered = indices[np.argsort(centers[:, axis], kind='stable')]
            middle = len(ordered) // 2
            left, right = self._build(ordered[:middle]), self._build(ordered[middle:])
            self._nodes[node] = (low, high, None, left, right)
        return node

    def _closest(self, point, face):
        indices = self._triangles[face]
        a, b, c = self._vertices[indices]
        candidates = [_segment(point, a, b), _segment(point, b, c), _segment(point, c, a)]
        # A triangle can be too ill-conditioned to support a normal while its
        # nonzero geometric interior still supplies the true nearest point.
        # Cross-product barycentrics avoid subtracting nearly equal Gram terms.
        u, v, w = b-a, c-a, point-a
        cross = np.cross(u, v)
        determinant = float(cross @ cross)
        if determinant > 0:
            beta = float(np.cross(w, v) @ cross) / determinant
            gamma = float(np.cross(u, w) @ cross) / determinant
            if beta >= 0 and gamma >= 0 and beta + gamma <= 1:
                candidates.append(a + beta*u + gamma*v)
        squared = [float((point-p) @ (point-p)) for p in candidates]
        index = int(np.argmin(squared))
        return candidates[index], squared[index]

    def _feature(self, closest, face, tolerance):
        indices = self._triangles[face]
        for vertex in sorted(map(int, indices)):
            if np.linalg.norm(closest - self._vertices[vertex]) <= tolerance:
                return 'vertex', (vertex,)
        for u, v in sorted(tuple(sorted((int(u), int(v)))) for u, v in zip(indices, np.roll(indices, -1))):
            if np.linalg.norm(closest - _segment(closest, self._vertices[u], self._vertices[v])) <= tolerance:
                return 'edge', (u, v)
        return 'face', (int(face),)

    def _normal(self, kind, feature, face):
        reasons = []
        if kind == 'face':
            incident = [face]
            indices = self._triangles[face]
            edges = {tuple(sorted((int(u), int(v)))) for u, v in zip(indices, np.roll(indices, -1))}
            if edges & self._nonmanifold or any(int(v) in self._bad_vertices for v in indices):
                reasons.append('nonmanifold_face_neighborhood')
            if edges & self._orientation_conflicts:
                reasons.append('inconsistent_face_neighborhood_winding')
        elif kind == 'edge':
            rows = self._edges[feature]
            incident = sorted({f for f, _ in rows})
            if feature in self._boundary:
                reasons.append('boundary_edge')
            if feature in self._nonmanifold:
                reasons.append('nonmanifold_edge')
            if feature in self._orientation_conflicts:
                reasons.append('inconsistent_edge_winding')
        else:
            vertex = feature[0]
            incident = sorted(self._vertex_faces[vertex])
            if vertex in self._boundary_vertices:
                reasons.append('boundary_vertex')
            if vertex in self._bad_vertices:
                reasons.append('nonmanifold_vertex')
            if vertex in self._conflict_vertices:
                reasons.append('inconsistent_vertex_star_winding')
        if any(self._degenerate[f] for f in incident):
            reasons.append('degenerate_incident_triangle')
        if kind == 'vertex':
            vertex = feature[0]
            weighted = [self._normals[f] * self._angles[f, int(np.where(self._triangles[f] == vertex)[0][0])]
                        for f in incident]
            raw = np.sum(weighted, axis=0)
            normal_error = sum((self._angles[f, int(np.where(self._triangles[f] == vertex)[0][0])] + 1)
                               * self._normal_errors[f] for f in incident)
        else:
            raw = self._normals[incident].sum(axis=0)
            normal_error = float(self._normal_errors[incident].sum())
        length = float(np.linalg.norm(raw))
        if length <= max(32*_EPS*max(1, len(incident)), 8*normal_error):
            reasons.append('degenerate_or_cancelling_pseudonormal')
        normal = None if reasons else raw / length
        return normal, incident, reasons

    def query(self, point):
        world_point = np.asarray(point, dtype=np.float64)
        if world_point.shape != (3,) or not np.isfinite(world_point).all():
            raise ValueError('point must contain three finite world coordinates')
        with np.errstate(over='ignore', invalid='ignore'):
            p = (world_point - self._origin) / self._scale
        if not np.isfinite(p).all() or np.max(np.abs(p)) > math.sqrt(np.finfo(float).max) / 16:
            raise ValueError('query coordinate range exceeds finite distance arithmetic')
        tolerance = max(self._tolerance, 32*_EPS*max(1, float(np.max(np.abs(p)))))
        def box_distance(node):
            low, high = self._nodes[node][:2]
            delta = np.maximum(np.maximum(low-p, p-high), 0)
            return float(delta @ delta)
        queue, best, hits = [(box_distance(0), 0)], math.inf, []
        while queue:
            bound, node = heapq.heappop(queue)
            margin = 0 if math.isinf(best) else 2*tolerance*math.sqrt(best)+tolerance**2
            if bound > best + margin:
                continue
            _, _, indices, left, right = self._nodes[node]
            if indices is None:
                heapq.heappush(queue, (box_distance(left), left))
                heapq.heappush(queue, (box_distance(right), right))
            else:
                for face in indices:
                    closest, squared = self._closest(p, int(face))
                    best = min(best, squared)
                    hits.append((squared, int(face), closest))
        margin = 2*tolerance*math.sqrt(best)+tolerance**2
        tied = [h for h in hits if h[0] <= best+margin]
        # True minimum distance/point is retained; IDs break exact numerical ties.
        squared, face, closest = min(tied, key=lambda h: (h[0], int(self._ids[h[1]])))
        kind, feature = self._feature(closest, face, tolerance)
        feature_keys = set()
        for _, f, q in tied:
            k, key = self._feature(q, f, tolerance)
            feature_keys.add((k, key))
        normal, incident, reasons = self._normal(kind, feature, face)
        if len(feature_keys) > 1:
            reasons.append('unresolved_nearest_feature_tie')
        if tolerance >= 1e-3:
            reasons.append('insufficient_coordinate_resolution')
        # Global defects remain explicit even if the chosen local star is sound.
        # They invalidate solid semantics, not the measured Euclidean distance.
        if reasons:
            normal = None
        nearest_world = self._origin + closest*self._scale
        displacement = (p-closest)*self._scale
        distance = math.sqrt(squared)*self._scale
        if not math.isfinite(distance):
            raise ValueError('world distance overflowed')
        return {
            'point_world': world_point.tolist(), 'nearest_point_world': nearest_world.tolist(),
            'distance': float(distance), 'triangle_index': face, 'triangle_id': int(self._ids[face]),
            'tied_triangle_ids': sorted(int(self._ids[f]) for _, f, _ in tied),
            'feature_kind': kind,
            'feature_vertex_indices': self._triangles[face].tolist() if kind == 'face' else list(feature),
            'incident_triangle_ids': [int(self._ids[f]) for f in incident],
            'normal_world': None if normal is None else normal.tolist(),
            'normal_convention': 'face / uniform edge / corner-angle-weighted vertex; input winding',
            'local_side_projection': None if normal is None else float(displacement @ normal),
            'ambiguous': bool(reasons), 'ambiguity_reasons': reasons,
            'feature_tolerance_world': float(tolerance*self._scale),
            'surface_qualification': self.qualification,
            'solid_classification': 'unknown',
            'solid_classification_reason': 'self_intersection_and_nesting_not_certified' if
                self._qualification['closed_oriented_manifold'] else 'surface_not_closed_oriented_manifold',
        }

    def query_many(self, points):
        array = np.asarray(points, dtype=np.float64)
        if array.ndim != 2 or array.shape[1:] != (3,) or not np.isfinite(array).all():
            raise ValueError('query_many expects a finite (K,3) array')
        return [self.query(point) for point in array]
