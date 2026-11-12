# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent RAW skin subsets; preserve applied weights, never transfer them.

The optional world plane clips only the new Mesh. A live plan is deliberately
not reconstructed from saved metadata. Preview uses exactly the apply builder.
"""
import hashlib
import json
import copy

import bpy
import bmesh
import numpy as np
from mathutils import Vector

from . import skinning, skinning_policy as policy, corrective_authoring as sig

SOURCE_ID = 'c94_skin_part_source_id'
VERTEX_KIND = 'c94_skin_part_vertex_kind'
_NORMAL = '_c94_skin_part_corner_normal'
_require = policy._require
_CONNECTIVITY = {'position', '.edge_verts', '.corner_vert', '.corner_edge'}


def _plane(value, source, scale):
    if value is None:
        return None
    _require(isinstance(value, dict) and set(value) == {
        'point_world', 'normal_world', 'offset_meters', 'keep_positive'},
        '裁剪平面需要明确世界位置、世界法线、米制偏移及保留侧')
    p = np.asarray(value['point_world'], dtype=float)
    n = np.asarray(value['normal_world'], dtype=float)
    offset = value['offset_meters']
    _require(p.shape == n.shape == (3,) and np.isfinite(p).all()
             and np.isfinite(n).all() and np.linalg.norm(n) > 1e-12
             and type(offset) in (int, float) and np.isfinite(offset)
             and type(value['keep_positive']) is bool, '裁剪平面参数无效')
    n = n / np.linalg.norm(n)
    point = source.matrix_world.inverted() @ Vector(p + n * offset / scale)
    normal = source.matrix_world.to_3x3().transposed() @ Vector(n)
    normal.normalize()
    return {'point_world': p.tolist(), 'normal_world': n.tolist(),
            'offset_meters': float(offset), 'keep_positive': value['keep_positive'],
            'point_local': list(point), 'normal_local': list(normal)}


def _source(source, rig):
    policy._bound(source, rig, donor=True)
    _require(sum(m.type == 'SUBSURF' for m in source.modifiers) <= 1,
             '皮肤派生仅支持普通Armature后可选一个Subsurf')
    _require(not any(a.name in {SOURCE_ID, VERTEX_KIND, _NORMAL} for a in source.data.attributes),
             '源包含本入口的来源属性，请选择明确的原始身体来源，不能覆盖上代来源')
    available = set(policy.pose_switching._metadata(rig)['deforms'])
    names = [g.name for g in source.vertex_groups if g.name in available]
    return policy._rig(rig, names)


def _value(item):
    return {p.identifier: sig._plain(getattr(item, p.identifier))
            for p in item.bl_rna.properties
            if p.identifier != 'rna_type' and p.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING'}}


def _verify(source, obj):
    """Check exact source vertices and unchanged full faces across native remap."""
    sm, cm = source.data, obj.data
    ids = [int(v.value) for v in cm.attributes[SOURCE_ID].data]
    kinds = [int(v.value) for v in cm.attributes[VERTEX_KIND].data]
    retained = [i for i, sid in enumerate(ids) if sid >= 0]
    _require(retained and len({ids[i] for i in retained}) == len(retained)
             and all(0 <= ids[i] < len(sm.vertices) for i in retained)
             and all((sid >= 0 and kind == 0) or (sid == -1 and kind == 1)
                     for sid, kind in zip(ids, kinds)), '裁剪来源索引无效')
    sn, sw = sig._weights(source); cn, cw = sig._weights(obj)
    _require(sn == cn and [g.lock_weight for g in obj.vertex_groups]
             == [g.lock_weight for g in source.vertex_groups], '组顺序或锁组元数据改变')
    for i in retained:
        sid = ids[i]
        _require(tuple(cm.vertices[i].co) == tuple(sm.vertices[sid].co)
                 and cw[i] == sw[sid], '原始点位置或实际权重改变')
    available = set(policy.pose_switching._metadata(obj.modifiers[0].object)['deforms'])
    deform = {g.index for g in obj.vertex_groups if g.name in available}
    for row in cw:
        _require(all(np.isfinite(w) and w >= 0 for _, w in row)
                 and abs(sum(w for g, w in row if g in deform) - 1) <= 1e-5,
                 '实际形变权重不完整，禁止补列或归一化兜底')
    _require([m for m in cm.materials] == [m for m in sm.materials]
             and [u.name for u in cm.uv_layers] == [u.name for u in sm.uv_layers]
             and cm.uv_layers.active_index == sm.uv_layers.active_index,
             '材质槽或UV层配置改变')
    for su, cu in zip(sm.uv_layers, cm.uv_layers):
        _require(su.active_render == cu.active_render, 'UV渲染层改变')
    # Stable correspondence comes from retained IDs and face cycles, never KD.
    face_lookup = {}
    for p in sm.polygons:
        cycle = tuple(p.vertices)
        for shift in range(len(cycle)):
            face_lookup[cycle[shift:] + cycle[:shift]] = (p, shift)
    source_edges = {tuple(sorted(e.vertices)): e.index for e in sm.edges}
    maps = {'POINT': {i: ids[i] for i in retained}, 'EDGE': {}, 'FACE': {}, 'CORNER': {}}
    for e in cm.edges:
        a, b = (ids[i] for i in e.vertices)
        if a >= 0 and b >= 0:
            old = source_edges.get(tuple(sorted((a, b))))
            if old is not None:
                maps['EDGE'][e.index] = old
    full = 0
    for p in cm.polygons:
        cycle = tuple(ids[i] for i in p.vertices)
        if -1 in cycle:
            continue
        found = face_lookup.get(cycle)
        # A plane through existing vertices can trim a face without new points.
        if found is None:
            continue
        old, shift = found; full += 1; maps['FACE'][p.index] = old.index
        _require(p.material_index == old.material_index and p.use_smooth == old.use_smooth,
                 '完整保留面的材质或平滑设置改变')
        for j, li in enumerate(p.loop_indices):
            oli = old.loop_indices[(shift + j) % len(cycle)]
            maps['CORNER'][li] = oli
            for su, cu in zip(sm.uv_layers, cm.uv_layers):
                _require(tuple(su.data[oli].uv) == tuple(cu.data[li].uv), '完整保留面UV改变')
    normal_error = 0.0
    if sm.has_custom_normals:
        _require(cm.has_custom_normals, '自定义角法线未重建')
        for new, old in maps['CORNER'].items():
            _require(tuple(cm.attributes[_NORMAL].data[new].vector)
                     == tuple(sm.corner_normals[old].vector),
                     '完整保留角的法线输入方向改变')
            normal_error = max(normal_error,
                (cm.corner_normals[new].vector-sm.corner_normals[old].vector).length)
        # Native packed normals are lossy and depend on the changed smooth fan.
        # Report actual error; do not invent a universal int16 error bound.
        _require(np.isfinite(normal_error), '自定义角法线重建产生非有限结果')
    for a in sm.attributes:
        if a.name in _CONNECTIVITY:
            continue
        ca = cm.attributes.get(a.name)
        _require(ca is not None and (ca.domain, ca.data_type) == (a.domain, a.data_type),
                 '原生裁剪未保留源属性类型：' + a.name)
        if a.name == 'custom_normal' and sm.has_custom_normals:
            continue
        _require(a.domain in maps, '当前不支持此属性域：' + a.name)
        for new, old in maps[a.domain].items():
            _require(_value(ca.data[new]) == _value(a.data[old]),
                     '完整保留区域属性改变：' + a.name)
    return ids, {'retained_source_vertices': len(retained),
                 'new_interpolated_vertices': len(ids)-len(retained),
                 'total_vertices': len(ids), 'faces': len(cm.polygons),
                 'complete_source_faces_exact': full,
                 'custom_normal_max_vector_reencode_error': normal_error,
                 'custom_normals': 'retained corner directions re-encoded; new corners interpolate normalized source directions' if sm.has_custom_normals else 'native automatic normals',
                 'exact_scope': 'retained RAW coordinates/weights/attributes except re-encoded custom_normal; complete retained faces and their other corner attributes/UV; clipped faces and new edge points are reconstructed/interpolated'}


class PreparedSkinPart:
    def __init__(self, source, rig, ids, faces, plane, scale):
        self.source, self.rig = source, rig
        self._scene, self._mesh, self._rig_data = bpy.context.scene, source.data, rig.data
        self._before = policy._state(source, rig)
        self._faces = tuple(faces); self._plane = copy.deepcopy(plane); self._done = False
        self._report = {'schema': 'c94.skinning.v1', 'method': 'body_skin_selection',
            'source': source.name, 'source_data': source.data.name, 'rig': rig.name,
            'geometry_route': 'current_bound_skin', 'old_hand_shape_fix': False,
            'source_vertex_attribute': SOURCE_ID, 'vertex_kind_attribute': VERTEX_KIND,
            'source_geometry_sha256': self._before['source']['mesh'],
            'source_weights_sha256': self._before['source']['weights'],
            'parent_binding_record_sha256': hashlib.sha256(source['c94_skinning_v1'].encode()).hexdigest()
                if isinstance(source.get('c94_skinning_v1'), str) else None,
            'selection_sha256': hashlib.sha256(np.asarray(ids, dtype=np.int64).tobytes()).hexdigest(),
            'selected_vertices': len(ids), 'complete_faces': len(faces),
            'selection_scope': 'complete faces contained in explicit RAW vertex IDs; no automatic island or nearest-surface selection',
            'plane': copy.deepcopy(plane), 'unit_scale': scale, 'source_unchanged': True}

    @property
    def report(self):
        return copy.deepcopy(self._report)

    def _check(self):
        policy._object(self.source, 'MESH'); policy._object(self.rig, 'ARMATURE')
        _require(not self._done and bpy.context.scene == self._scene
                 and self.source.data == self._mesh and self.rig.data == self._rig_data,
                 '皮肤派生计划已结束或真实场景/数据身份改变')
        _require(policy._state(self.source, self.rig) == self._before,
                 '源几何、权重/锁、骨架、来源记录、帧或单位改变，请重新准备')

    def cancel(self):
        self._done = True

    def _build(self, name, preview):
        self._check()
        _require(isinstance(name, str) and name.strip() and name not in bpy.data.objects,
                 '新部件名称为空或已占用')
        obj = mesh = bm = None
        try:
            obj = self.source.copy(); mesh = self.source.data.copy(); obj.data = mesh; obj.name = name
            bpy.context.scene.collection.objects.link(obj)
            bm = bmesh.new(); bm.from_mesh(mesh)
            il = bm.verts.layers.int.new(SOURCE_ID); kl = bm.verts.layers.int.new(VERTEX_KIND)
            bm.verts.ensure_lookup_table(); bm.faces.ensure_lookup_table()
            for v in bm.verts:
                v[il] = v.index; v[kl] = 0
            if self.source.data.has_custom_normals:
                nl = bm.loops.layers.float_vector.new(_NORMAL)
                for face in bm.faces:
                    old = self.source.data.polygons[face.index]
                    _require(tuple(v.index for v in face.verts) == tuple(old.vertices),
                             '原生面角顺序不一致，不能搬运自定义法线')
                    for loop, li in zip(face.loops, old.loop_indices):
                        loop[nl] = self.source.data.corner_normals[li].vector
            keep = set(self._faces)
            bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.index not in keep], context='FACES')
            bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
            if self._plane is not None:
                p = self._plane; point = Vector(p['point_local']); normal = Vector(p['normal_local'])
                signs = [(v.co-point).dot(normal) for v in bm.verts]
                _require(min(signs) < 0 < max(signs), '平面未穿过明确选区，请调整平面或关闭裁剪')
                original = set(bm.verts)
                bmesh.ops.bisect_plane(bm, geom=list(bm.verts)+list(bm.edges)+list(bm.faces),
                    dist=0.0, plane_co=point, plane_no=normal,
                    clear_inner=p['keep_positive'], clear_outer=not p['keep_positive'])
                for v in bm.verts:
                    if v not in original:
                        v[il] = -1; v[kl] = 1
                bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
            _require(len(bm.faces) > 0, '裁剪后没有完整表面')
            bm.to_mesh(mesh); bm.free(); bm = None; mesh.update()
            if self.source.data.has_custom_normals:
                normal_attr = mesh.attributes[_NORMAL]
                normals = [v.vector.normalized() for v in normal_attr.data]
                _require(all(np.isfinite(v).all() and v.length > .99 for v in normals),
                         '角法线插值退化，请调整裁剪范围')
                mesh.normals_split_custom_set(normals)
                mesh.update()
            # BMesh omits built-in flags when the retained subset is all false.
            # Recreate their schema from native element flags before exact checks.
            native_flags = {
                'sharp_face': ('FACE', mesh.polygons, lambda p: not p.use_smooth),
                'sharp_edge': ('EDGE', mesh.edges, lambda e: e.use_edge_sharp),
                '.select_vert': ('POINT', mesh.vertices, lambda v: v.select),
                '.select_edge': ('EDGE', mesh.edges, lambda e: e.select),
                '.select_poly': ('FACE', mesh.polygons, lambda p: p.select),
                '.hide_vert': ('POINT', mesh.vertices, lambda v: v.hide),
                '.hide_edge': ('EDGE', mesh.edges, lambda e: e.hide),
                '.hide_poly': ('FACE', mesh.polygons, lambda p: p.hide)}
            for key, (domain, items, get) in native_flags.items():
                if key in self.source.data.attributes and key not in mesh.attributes:
                    values = [get(x) for x in items]
                    attr = mesh.attributes.new(key, 'BOOLEAN', domain)
                    attr.data.foreach_set('value', values)
            _, counts = _verify(self.source, obj)
            if _NORMAL in mesh.attributes:
                mesh.attributes.remove(mesh.attributes[_NORMAL])
            report = dict(self._report, **counts, display_only=preview,
                          plane=copy.deepcopy(self._plane))
            if self._plane is not None:
                report['method'] = 'body_skin_selection_plane_clip'
            for key in ('c94_skinning_policy_v1', 'c94_paint_proposal_v1', 'c94_policy_preview_v1'):
                if key in obj:
                    del obj[key]
            obj['c94_skinning_v1'] = json.dumps(report, ensure_ascii=False, allow_nan=False)
            obj.hide_set(False); obj.hide_viewport = False; obj.hide_select = False
            obj.hide_render = preview
            bpy.context.view_layer.update(); self._check()
            self._report = report
            return obj
        except Exception:
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            raise
        finally:
            if bm is not None:
                bm.free()

    def preview(self, *, name):
        return self._build(name, True)

    def apply(self, *, name):
        obj = self._build(name, False)
        self._done = True
        return obj


def prepare(source, rig, vertex_indices, *, plane=None):
    scale = _source(source, rig)
    ids = policy._ids(vertex_indices, len(source.data.vertices))
    selected = set(ids.tolist())
    faces = [p.index for p in source.data.polygons if set(p.vertices) <= selected]
    _require(faces, '明确选点范围没有完整源面，请补齐选区')
    # Validate the actual field on every retained face, including undeclared deforms.
    policy._field(source, rig, None, ids)
    return PreparedSkinPart(source, rig, ids, faces, _plane(plane, source, scale), scale)
