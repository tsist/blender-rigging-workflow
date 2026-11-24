# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed-surface interior cells and surface-blocked finite-volume adjacency."""
import math
from itertools import product
from collections import deque
import numpy as np
from .contract import require, VolumeError


def _vertex_components(count, edges):
    parent=list(range(count))
    pairs=edges.tolist() if isinstance(edges,np.ndarray) else edges
    for aa,bb in pairs:
        ra=aa
        while parent[ra]!=ra:
            parent[ra]=parent[parent[ra]];ra=parent[ra]
        rb=bb
        while parent[rb]!=rb:
            parent[rb]=parent[parent[rb]];rb=parent[rb]
        if ra!=rb:parent[rb]=ra
    for i in range(count):
        r=i
        while parent[r]!=r:
            r=parent[r]
        parent[i]=r
    return np.asarray(parent,dtype=np.int32)


def validate_closed(vertices,triangles):
    a,b,c=(vertices[triangles[:,i]] for i in range(3))
    span=np.ptp(vertices,axis=0)
    require(bool((span>0).all()),'OPEN_OR_THIN','The surface has no 3D bounding volume')
    area=np.linalg.norm(np.cross(b-a,c-a),axis=1)
    require(bool((area>max(float(np.max(span))**2*1e-14,1e-30)).all()),'DEGENERATE_SURFACE','Degenerate triangles cannot define a certified volume')
    edges=np.concatenate((triangles[:,[0,1]],triangles[:,[1,2]],triangles[:,[2,0]]))
    sorted_edges=np.sort(edges,axis=1)
    if len(vertices)<(1<<31):
        edge_keys=(sorted_edges[:,0].astype(np.int64)<<32)|sorted_edges[:,1].astype(np.int64)
        unique_keys,inverse,counts=np.unique(edge_keys,return_inverse=True,return_counts=True)
        unique=np.column_stack((unique_keys>>32,unique_keys&0xFFFFFFFF)).astype(edges.dtype,copy=False)
    else:
        unique,inverse,counts=np.unique(sorted_edges,axis=0,return_inverse=True,return_counts=True)
    bad=int(np.count_nonzero(counts!=2))
    require(not bad,'OPEN_OR_NONMANIFOLD','Closed manifold edges must have exactly two incident triangles',{'boundary_or_nonmanifold_edges':bad})
    signs=np.where(edges[:,0]<edges[:,1],1.,-1.)
    balance=np.bincount(inverse,weights=signs,minlength=len(unique))
    require(bool((balance==0).all()),'INCONSISTENT_ORIENTATION','Adjacent triangle winding is inconsistent')
    used_count=int(np.count_nonzero(np.bincount(triangles.ravel(),minlength=len(vertices))))
    require(used_count==len(vertices),'UNUSED_VERTICES','Unreferenced vertices do not have a volumetric surface sampling contract')
    labels=_vertex_components(len(vertices),unique)
    shells=np.unique(labels)
    origin=np.mean(vertices,axis=0)
    shifted=vertices-origin
    av,bv,cv=(shifted[triangles[:,i]] for i in range(3))
    signed=np.einsum('ij,ij->i',av,np.cross(bv,cv))/6
    volumes=[]
    for shell in shells:
        volume=float(np.sum(signed[labels[triangles[:,0]]==shell]))
        require(abs(volume)>max(float(np.max(span))**3*1e-13,1e-30),'ZERO_VOLUME','A closed surface shell has near-zero signed volume')
        volumes.append(volume)
    return {'triangles':len(triangles),'surface_shells':len(shells),
            'signed_shell_volumes':volumes,'boundary_or_nonmanifold_edges':0}


class Domain:
    def __init__(self,vertices,triangles,h,settings,budget):
        try:
            from mathutils import Vector
            from mathutils.bvhtree import BVHTree
        except ImportError as exc:
            raise VolumeError('UNAVAILABLE_RUNTIME','The installed Blender mathutils BVH runtime is required; no dependency is installed automatically') from exc
        self.Vector=Vector;self.settings=settings;self.budget=budget;self.h=h
        self.world_origin=(np.min(vertices,axis=0)+np.max(vertices,axis=0))*.5
        self.vertices=vertices-self.world_origin
        self.triangles=triangles
        self.eps=max(h*1e-5,np.max(np.ptp(self.vertices,axis=0))*1e-9)
        low=np.min(self.vertices,axis=0);span=np.ptp(self.vertices,axis=0)
        self.shape=tuple(math.ceil(float(s/h))+2 for s in span)
        self.grid_low=low-h
        self.stats={'grid_shape':list(self.shape),'grid_cells':math.prod(self.shape),'cell_size':self.h,
                    'world_origin':self.world_origin.tolist(),'boundary_epsilon':self.eps}
        require(math.prod(self.shape)<=settings['max_cells'],'RESOURCE_LIMIT','Uniform grid exceeds max_cells before allocation',{'grid_shape':self.shape,'requested_grid_cells':math.prod(self.shape)})
        require(float(np.min(span))/h>=2,'UNDER_RESOLVED','The smallest bounding dimension has fewer than two cells; no silent coarsening/heat fallback',{'cell_size':h,'bbox_span':span.tolist()})
        self.bvh=BVHTree.FromPolygons(self.vertices.tolist(),triangles.tolist(),all_triangles=True,epsilon=0.0)
        self._inside_cache={}
        self.progress=None
        self._triangle_components=None
        self.nearest=None

    def triangle_component(self,index):
        if self._triangle_components is None:
            edges=np.concatenate((self.triangles[:,[0,1]],self.triangles[:,[1,2]],self.triangles[:,[2,0]]))
            labels=_vertex_components(len(self.vertices),edges)
            self._triangle_components=labels[self.triangles[:,0]]
        return self._triangle_components[index]

    def intersections(self,origin,direction,distance,*,with_faces=False):
        ox,oy,oz=float(origin[0]),float(origin[1]),float(origin[2])
        dx,dy,dz=float(direction[0]),float(direction[1]),float(direction[2])
        progress=0.;hits=[];seen_triangles=set()
        ray_cast=self.bvh.ray_cast;eps=self.eps;dir_t=(dx,dy,dz)
        for _ in range(self.settings['max_ray_hits']):
            location,normal,index,remaining=ray_cast((ox+progress*dx,oy+progress*dy,oz+progress*dz),dir_t,float(distance-progress))
            if location is None:
                return hits
            at=(location[0]-ox)*dx+(location[1]-oy)*dy+(location[2]-oz)*dz
            if at<progress-eps:
                raise VolumeError('CLASSIFICATION_AMBIGUOUS','BVH returned a backwards ray intersection')
            # BVH is float32: after a very short step it can return the same
            # triangle again. A straight non-coplanar ray intersects a planar
            # triangle at most once. Do not change parity for that duplicate;
            # distinct coincident triangles and all existing failure guards stay.
            if index not in seen_triangles:
                hits.append((at,index) if with_faces else at)
                seen_triangles.add(index)
            nxt=at+eps
            progress=nxt if nxt>progress+eps else progress+eps
            if progress>=distance:return hits
        raise VolumeError('RAY_LIMIT','Surface layers exceed explicit max_ray_hits; occupancy is not accepted')

    def inside(self,point):
        px,py,pz=float(point[0]),float(point[1]),float(point[2])
        inv_half_eps=2.0/self.eps
        key=(round(px*inv_half_eps),round(py*inv_half_eps),round(pz*inv_half_eps))
        if key in self._inside_cache:return self._inside_cache[key]
        nearest=self.bvh.find_nearest((px,py,pz))
        if nearest[0] is not None and nearest[3]<=self.eps*3:
            value=None  # Explicit boundary case, not silently classified inside.
        else:
            if not hasattr(self,'_max_x'):
                self._max_x=float(np.max(self.vertices[:,0]))
            distance=self._max_x-px+self.h*3
            value=False if distance<=0 else len(self.intersections((px,py,pz),(1.,0.,0.),distance))%2==1
        if len(self._inside_cache)>=min(65536,max(1024,math.prod(self.shape)//2)):
            self._inside_cache.clear()
        self._inside_cache[key]=value
        return value

    def clear_segment(self,a,b):
        dx=float(b[0])-float(a[0]);dy=float(b[1])-float(a[1]);dz=float(b[2])-float(a[2])
        length=math.sqrt(dx*dx+dy*dy+dz*dz)
        if length<=self.eps*4:return True
        inv=1.0/length
        ux,uy,uz=dx*inv,dy*inv,dz*inv
        shift=self.eps*2
        location,normal,index,distance=self.bvh.ray_cast((float(a[0])+ux*shift,float(a[1])+uy*shift,float(a[2])+uz*shift),(ux,uy,uz),length-shift*2)
        return location is None

    def clear_surface_segment(self, point, face, target):
        """Allow a start exactly on this surface, without skipping nearby walls."""
        point=np.asarray(point)
        delta=target-point
        distance=float(np.linalg.norm(delta))
        if distance<=self.eps:
            return False
        direction=delta/distance
        ray=self.Vector(direction)
        hit,_,index,at=self.bvh.ray_cast(self.Vector(point),ray,distance)
        if hit is None:
            return True
        # A re-hit of the generating face at the ray origin is numerical.
        # Every other surface, including a close neighbouring shell, blocks it.
        shift=self.eps*.25
        if index!=face or at>shift:
            return False
        return self.bvh.ray_cast(self.Vector(point+direction*shift),ray,distance-shift)[0] is None

    def occupy(self):
        occupied=np.zeros(self.shape,dtype=bool)
        xs=self.grid_low[0]+(np.arange(self.shape[0])+.5)*self.h
        ambiguous=0;near_surface=0;rays=0
        x0=float(self.grid_low[0]-self.h);y0=float(self.grid_low[1]);z0=float(self.grid_low[2])
        h=float(self.h);eps=float(self.eps);span_x=float(h*(self.shape[0]+2));dir_x=(1.0,0.0,0.0)
        nz=self.shape[2]
        for iy in range(self.shape[1]):
            if self.progress is not None:
                self.progress('occupancy',done=iy,total=self.shape[1],unit='scanline_columns')
            self.budget.check()
            y=y0+(iy+.5)*h
            for iz in range(nz):
                z=z0+(iz+.5)*h
                hits=self.intersections((x0,y,z),dir_x,span_x)
                rays+=1
                if len(hits)%2:
                    ambiguous+=1
                    continue
                for first,last in zip(hits[::2],hits[1::2]):
                    occupied[:,iy,iz]|=(xs>x0+first+eps)&(xs<x0+last-eps)
        require(not ambiguous,'CLASSIFICATION_AMBIGUOUS','Odd surface crossings prevent closed-volume classification',{'ambiguous_columns':ambiguous,'ray_columns':rays})
        indices=np.argwhere(occupied)
        require(len(indices)>0,'UNDER_RESOLVED','No reliable interior cell centers at requested cell size')
        centers=self.grid_low+(indices+.5)*self.h
        keep=np.ones(len(indices),dtype=bool)
        nearest_dists=np.empty(len(indices),dtype=np.float64)
        find_nearest=self.bvh.find_nearest;boundary_band=eps*3
        for i,point in enumerate(centers.tolist()):
            if i%1024==0:self.budget.check()
            nearest=find_nearest(point)
            if nearest[0] is None or nearest[3]<=boundary_band:
                keep[i]=False;near_surface+=1;nearest_dists[i]=0.0
            else:
                nearest_dists[i]=nearest[3]
        indices=indices[keep];centers=centers[keep];self.nearest=nearest_dists[keep]
        require(len(indices)>0,'UNDER_RESOLVED','All classified cells lay on uncertain surface boundaries')
        self.indices=indices.astype(np.int32);self.centers=centers
        self.grid=np.full(self.shape,-1,dtype=np.int32)
        self.grid[tuple(indices.T)]=np.arange(len(indices),dtype=np.int32)
        self.stats={'grid_shape':list(self.shape),'grid_cells':math.prod(self.shape),'occupied_cells':len(indices),
            'cell_size':self.h,'occupied_cell_volume_estimate':len(indices)*self.h**3,
            'surface_boundary_centers_excluded':near_surface,'ray_columns':rays,'ambiguous_columns':ambiguous,
            'classification':'closed-surface even/odd scanlines; all accepted centers farther than boundary epsilon',
            'world_origin':self.world_origin.tolist(),'boundary_epsilon':self.eps}

    def graph(self):
        count=len(self.centers);neighbor_count=self.settings['neighbor_count']
        # Preserve the original six slot indices and axis traversal order.
        steps=[(1,0,0),(0,1,0),(0,0,1)]
        if neighbor_count==26:
            steps.extend(delta for delta in product((-1,0,1),repeat=3)
                         if sum(x*x for x in delta)>1 and next(x for x in delta if x)>0)
        axis_visits=len(steps)*count
        adjacent=np.full((count,neighbor_count),-1,dtype=np.int32)
        conductance=np.zeros((count,neighbor_count),dtype=np.float64)
        deltas=np.asarray([tuple(-x for x in step) if sign<0 else step
                           for step in steps for sign in (-1,1)],dtype=np.int32)
        raw=0;blocked=0
        accepted_by_distance={1:0,2:0,3:0};blocked_by_distance={1:0,2:0,3:0}
        if self.nearest is not None and len(self.nearest)==count:
            nearest = self.nearest
        else:
            find_nearest = self.bvh.find_nearest
            nearest = np.empty(count, dtype=np.float64)
            for i, center in enumerate(self.centers.tolist()):
                if i % 2048 == 0:
                    self.budget.check()
                distance = find_nearest(center)[3]
                nearest[i] = distance if distance is not None else 0.0
            self.nearest = nearest
        # A surface crossing requires the two endpoint distances to total at
        # most one edge length. Keep the original BVH test near its float32
        # precision limit and for all edges that could intersect the surface.
        margin = 64 * np.finfo(np.float32).eps * np.max(np.ptp(self.vertices, axis=0)) + 2 * self.eps
        ray_cast = self.bvh.ray_cast; eps = self.eps
        ray_tests = 0
        for axis,step in enumerate(steps):
            delta=np.asarray(step,dtype=np.int32);distance2=int(np.dot(delta,delta))
            edge_weight=1.0/distance2
            if self.progress is not None:
                self.progress('graph',done=axis*count,total=axis_visits,unit='cell_axis_visits')
            self.budget.check()
            other = self.indices + delta
            valid = np.all((other >= 0) & (other < self.shape), axis=1)
            left = np.flatnonzero(valid)
            right = self.grid[tuple(other[left].T)]
            present = right >= 0
            left, right = left[present], right[present]
            raw += len(left)
            maybe_crossing = nearest[left] + nearest[right] <= self.h * math.sqrt(distance2) + margin
            checked = np.flatnonzero(maybe_crossing)
            allowed = np.ones(len(left), dtype=bool)
            if len(checked):
                ca = self.centers[left[checked]]
                diff = self.centers[right[checked]] - ca
                lens = np.linalg.norm(diff, axis=1)
                long_mask = lens > eps * 4
                if long_mask.any():
                    long_idx = checked[long_mask]
                    dl = lens[long_mask]
                    dirs = diff[long_mask] / dl[:, None]
                    starts = (ca[long_mask] + dirs * (eps * 2)).tolist()
                    dir_list = dirs.tolist()
                    ray_lens = (dl - eps * 4).tolist()
                    res = np.empty(len(long_idx), dtype=bool)
                    for visit, (o, d_vec, rl) in enumerate(zip(starts, dir_list, ray_lens)):
                        if visit % 2048 == 0:
                            self.budget.check()
                        res[visit] = ray_cast(o, d_vec, rl)[0] is None
                    allowed[long_idx] = res
            ray_tests += len(checked)
            denied = int(np.count_nonzero(~allowed))
            blocked += denied
            blocked_by_distance[distance2] += denied
            left, right = left[allowed], right[allowed]
            adjacent[left,2*axis+1]=right;adjacent[right,2*axis]=left
            conductance[left,2*axis+1]=edge_weight;conductance[right,2*axis]=edge_weight
            accepted_by_distance[distance2] += len(left)
        labels=[-1]*count;sizes=[];adj_list=adjacent.tolist()
        for start in range(count):
            if start % 2048 == 0:
                self.budget.check()
            if labels[start]>=0:continue
            ident=len(sizes);labels[start]=ident;queue=deque([start]);size=0
            while queue:
                i=queue.popleft();size+=1
                if size % 2048 == 0:
                    self.budget.check()
                for j in adj_list[i]:
                    if j>=0 and labels[j]<0:
                        labels[j]=ident;queue.append(j)
            sizes.append(size)
        require(min(sizes)>=self.settings['min_component_cells'],'UNDER_RESOLVED_COMPONENT','An interior component has too few cells for the explicit volume policy',{'component_cells':sizes,'minimum':self.settings['min_component_cells']})
        self.adjacent=adjacent;self.conductance=conductance;self.neighbor_deltas=deltas;self.labels=np.asarray(labels,dtype=np.int32)
        self.stats.update(connected_components=len(sizes),component_cells=sizes,raw_neighbor_edges=raw,
            cross_surface_edges_blocked=blocked,graph_edges=raw-blocked,graph_surface_ray_tests=ray_tests,neighbor_count=neighbor_count,
            neighbor_deltas=deltas.tolist(),weighted_degree_min=float(conductance.sum(axis=1).min()),
            weighted_degree_max=float(conductance.sum(axis=1).max()),
            edges_by_squared_grid_distance={str(key):value for key,value in accepted_by_distance.items()},
            blocked_edges_by_squared_grid_distance={str(key):value for key,value in blocked_by_distance.items()},
            conductance=self.h if neighbor_count==6 else 'dimensionless 1 / squared grid delta length',
            solver_conductance='uniform 1 after common h factor cancels' if neighbor_count==6 else 'symmetric positive weights 1, 1/2, 1/3',
            graph_boundary=('no edge to air; zero flux. Shared-face conductance h^2/h=h (uniform common factor cancels).'
                            if neighbor_count==6 else
                            'no edge through a surface or to air; weighted graph Laplacian, not shared-face finite volume'))
