"""Explicit planar temporary volume domains; never edit the production Mesh."""
import math
import bmesh
import numpy as np
from mathutils import Vector


def closed_clip(vertices,triangles,source_vertex_ids,source_triangle_ids,planes):
    """Clip/fill independent BMesh. Keep source IDs only at unchanged coordinates.

    planes: (point, normal, keep_positive). New intersections and cap faces use -1.
    Every input must already be a closed surface; this does not weld components.
    """
    vertices=np.asarray(vertices,dtype=float)
    triangles=np.asarray(triangles,dtype=np.int64)
    source_vertex_ids=np.asarray(source_vertex_ids,dtype=np.int64)
    source_triangle_ids=np.asarray(source_triangle_ids,dtype=np.int64)
    if (vertices.ndim!=2 or vertices.shape[1]!=3 or not np.isfinite(vertices).all()
            or triangles.ndim!=2 or triangles.shape[1]!=3 or not len(triangles)
            or source_vertex_ids.shape!=(len(vertices),) or source_triangle_ids.shape!=(len(triangles),)
            or triangles.min()<0 or triangles.max()>=len(vertices)):
        raise ValueError('体积代理数组或源ID映射无效')
    for point,normal,positive in planes:
        if (len(point)!=3 or len(normal)!=3 or not np.isfinite(point).all()
                or not np.isfinite(normal).all() or np.linalg.norm(normal)<1e-12
                or type(positive) is not bool):
            raise ValueError('裁面需要有限坐标、非零法线和明确保留侧')
    bm=bmesh.new()
    vl=bm.verts.layers.int.new('source_vertex_id')
    fl=bm.faces.layers.int.new('source_triangle_id')
    original={int(ident):Vector(co) for ident,co in zip(source_vertex_ids,vertices) if ident>=0}
    for co,ident in zip(vertices,source_vertex_ids):
        vert=bm.verts.new(co);vert[vl]=int(ident)
    bm.verts.ensure_lookup_table()
    for tri,ident in zip(triangles,source_triangle_ids):
        face=bm.faces.new([bm.verts[int(i)] for i in tri]);face[fl]=int(ident)
    stages=[]
    try:
        for point,normal,positive in planes:
            normal=Vector(normal).normalized();point=Vector(point)
            before_vertices=len(bm.verts)
            bmesh.ops.bisect_plane(bm,geom=list(bm.verts)+list(bm.edges)+list(bm.faces),
                dist=1e-9,plane_co=point,plane_no=normal,clear_inner=bool(positive),clear_outer=not positive,use_snap_center=False)
            for vert in bm.verts:
                ident=int(vert[vl])
                if ident not in original or vert.co!=original[ident]:vert[vl]=-1
            boundary=[edge for edge in bm.edges if edge.is_boundary]
            off_plane=[edge for edge in boundary if any(abs((v.co-point).dot(normal))>2e-6 for v in edge.verts)]
            if off_plane:
                raise ValueError('裁切出现不在当前裁面的开口，拒绝盲目补洞')
            added=bmesh.ops.holes_fill(bm,edges=boundary,sides=0).get('faces',[]) if boundary else []
            for face in added:face[fl]=-1
            for vert in bm.verts:
                ident=int(vert[vl])
                if ident not in original or vert.co!=original[ident]:vert[vl]=-1
            if bm.faces:bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces))
            kept=[(vert.co-point).dot(normal) for vert in bm.verts]
            if not kept:raise ValueError('裁面得到空代理')
            wrong=min(kept)<-2e-6 if positive else max(kept)>2e-6
            if wrong:raise ValueError('原生bisect保留侧与给定裁面不符')
            stages.append({'point':list(point),'normal':list(normal),'keep_positive':positive,
                'vertices_before':before_vertices,'vertices_after':len(bm.verts),'cut_boundary_edges':len(boundary),'added_cap_faces':len(added)})
        bmesh.ops.triangulate(bm,faces=list(bm.faces))
        bm.verts.index_update();bm.faces.index_update();bm.verts.ensure_lookup_table();bm.faces.ensure_lookup_table()
        points=np.asarray([tuple(v.co) for v in bm.verts],dtype=float)
        tris=np.asarray([[v.index for v in f.verts] for f in bm.faces],dtype=np.int64)
        vertex_ids=np.asarray([int(v[vl]) for v in bm.verts],dtype=np.int64)
        triangle_ids=np.asarray([int(f[fl]) for f in bm.faces],dtype=np.int64)
        changed=[int(ident) for co,ident in zip(points,vertex_ids) if ident>=0 and Vector(co)!=original[int(ident)]]
        if changed:raise ValueError('原ID顶点坐标改变')
        boundaries=sum(edge.is_boundary for edge in bm.edges)
        nonmanifold=sum(not edge.is_manifold for edge in bm.edges)
        return {'vertices':points,'triangles':tris,'source_vertex_ids':vertex_ids,'source_triangle_ids':triangle_ids,
              'stages':stages,'boundary_edges':int(boundaries),'nonmanifold_edges':int(nonmanifold),'mapped_coordinates_exact':not changed}
    finally:
        bm.free()
