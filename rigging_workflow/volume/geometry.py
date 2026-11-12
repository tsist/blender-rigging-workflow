# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed-surface interior cells and surface-blocked finite-volume adjacency."""
import math
from itertools import product
from collections import deque
import numpy as np
from .contract import require, VolumeError


def validate_closed(vertices,triangles):
    a,b,c=(vertices[triangles[:,i]] for i in range(3))
    span=np.ptp(vertices,axis=0)
    require(bool((span>0).all()),'OPEN_OR_THIN','The surface has no 3D bounding volume')
    area=np.linalg.norm(np.cross(b-a,c-a),axis=1)
    require(bool((area>max(float(np.max(span))**2*1e-14,1e-30)).all()),'DEGENERATE_SURFACE','Degenerate triangles cannot define a certified volume')
    edges=np.concatenate((triangles[:,[0,1]],triangles[:,[1,2]],triangles[:,[2,0]]))
    unique,inverse,counts=np.unique(np.sort(edges,axis=1),axis=0,return_inverse=True,return_counts=True)
    bad=int(np.count_nonzero(counts!=2))
    require(not bad,'OPEN_OR_NONMANIFOLD','Closed manifold edges must have exactly two incident triangles',{'boundary_or_nonmanifold_edges':bad})
    signs=np.where(edges[:,0]<edges[:,1],1.,-1.)
    balance=np.bincount(inverse,weights=signs,minlength=len(unique))
    require(bool((balance==0).all()),'INCONSISTENT_ORIENTATION','Adjacent triangle winding is inconsistent')
    used=np.unique(triangles)
    require(len(used)==len(vertices),'UNUSED_VERTICES','Unreferenced vertices do not have a volumetric surface sampling contract')
    parent=np.arange(len(vertices),dtype=np.int32)
    def find(x):
        while parent[x]!=x:
            parent[x]=parent[parent[x]];x=parent[x]
        return x
    for aa,bb in unique:
        ra,rb=find(int(aa)),find(int(bb))
        if ra!=rb:parent[rb]=ra
    labels=np.asarray([find(i) for i in range(len(vertices))])
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
        self.bvh=BVHTree.FromPolygons([Vector(v) for v in self.vertices],triangles.tolist(),all_triangles=True,epsilon=0.0)
        self._inside_cache={}
        self.progress=None

    def intersections(self,origin,direction,distance):
        origin=np.asarray(origin,dtype=np.float64);direction=np.asarray(direction,dtype=np.float64)
        progress=0.;hits=[];seen_triangles=set()
        for _ in range(self.settings['max_ray_hits']):
            location,normal,index,remaining=self.bvh.ray_cast(self.Vector(origin+progress*direction),self.Vector(direction),float(distance-progress))
            if location is None:
                return hits
            point=np.asarray(location,dtype=np.float64)
            at=float(np.dot(point-origin,direction))
            if at<progress-self.eps:
                raise VolumeError('CLASSIFICATION_AMBIGUOUS','BVH returned a backwards ray intersection')
            # BVH is float32: after a very short step it can return the same
            # triangle again. A straight non-coplanar ray intersects a planar
            # triangle at most once. Do not change parity for that duplicate;
            # distinct coincident triangles and all existing failure guards stay.
            if index not in seen_triangles:
                hits.append(at)
                seen_triangles.add(index)
            progress=max(at+self.eps,progress+self.eps)
            if progress>=distance:return hits
        raise VolumeError('RAY_LIMIT','Surface layers exceed explicit max_ray_hits; occupancy is not accepted')

    def inside(self,point):
        point=np.asarray(point,dtype=np.float64)
        key=tuple(np.rint(point/(self.eps*.5)).astype(np.int64))
        if key in self._inside_cache:return self._inside_cache[key]
        nearest=self.bvh.find_nearest(self.Vector(point))
        if nearest[0] is not None and nearest[3]<=self.eps*3:
            value=None  # Explicit boundary case, not silently classified inside.
        else:
            distance=float(np.max(self.vertices[:,0])-point[0]+self.h*3)
            value=False if distance<=0 else len(self.intersections(point,np.asarray([1.,0,0]),distance))%2==1
        if len(self._inside_cache)>=min(65536,max(1024,math.prod(self.shape)//2)):
            self._inside_cache.clear()
        self._inside_cache[key]=value
        return value

    def clear_segment(self,a,b):
        delta=np.asarray(b)-np.asarray(a);length=float(np.linalg.norm(delta))
        if length<=self.eps*4:return True
        direction=delta/length
        location,normal,index,distance=self.bvh.ray_cast(self.Vector(np.asarray(a)+direction*self.eps*2),self.Vector(direction),length-self.eps*4)
        return location is None

    def occupy(self):
        occupied=np.zeros(self.shape,dtype=bool)
        xs=self.grid_low[0]+(np.arange(self.shape[0])+.5)*self.h
        ambiguous=0;near_surface=0;rays=0
        for iy in range(self.shape[1]):
            if self.progress is not None:
                self.progress('occupancy',done=iy,total=self.shape[1],unit='scanline_columns')
            self.budget.check()
            y=self.grid_low[1]+(iy+.5)*self.h
            for iz in range(self.shape[2]):
                z=self.grid_low[2]+(iz+.5)*self.h
                origin=np.asarray([self.grid_low[0]-self.h,y,z])
                hits=self.intersections(origin,np.asarray([1.,0,0]),self.h*(self.shape[0]+2))
                rays+=1
                if len(hits)%2:
                    ambiguous+=1
                    continue
                for first,last in zip(hits[::2],hits[1::2]):
                    occupied[:,iy,iz]|=(xs>origin[0]+first+self.eps)&(xs<origin[0]+last-self.eps)
        require(not ambiguous,'CLASSIFICATION_AMBIGUOUS','Odd surface crossings prevent closed-volume classification',{'ambiguous_columns':ambiguous,'ray_columns':rays})
        indices=np.argwhere(occupied)
        require(len(indices)>0,'UNDER_RESOLVED','No reliable interior cell centers at requested cell size')
        centers=self.grid_low+(indices+.5)*self.h
        keep=np.ones(len(indices),dtype=bool)
        for i,point in enumerate(centers):
            if i%1024==0:self.budget.check()
            nearest=self.bvh.find_nearest(self.Vector(point))
            if nearest[0] is None or nearest[3]<=self.eps*3:
                keep[i]=False;near_surface+=1
        indices=indices[keep];centers=centers[keep]
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
        for axis,step in enumerate(steps):
            delta=np.asarray(step,dtype=np.int32);distance2=int(np.dot(delta,delta))
            edge_weight=1.0/distance2
            for i,index in enumerate(self.indices):
                if i%2048==0:
                    if self.progress is not None:
                        self.progress('graph',done=axis*count+i,total=axis_visits,unit='cell_axis_visits')
                    self.budget.check()
                other=index+delta
                if bool((other<0).any()) or bool((other>=self.shape).any()):continue
                j=int(self.grid[tuple(other)])
                if j<0:continue
                raw+=1
                if not self.clear_segment(self.centers[i],self.centers[j]):
                    blocked+=1;blocked_by_distance[distance2]+=1;continue
                adjacent[i,2*axis+1]=j;adjacent[j,2*axis]=i
                conductance[i,2*axis+1]=edge_weight;conductance[j,2*axis]=edge_weight
                accepted_by_distance[distance2]+=1
        labels=np.full(count,-1,dtype=np.int32);sizes=[]
        for start in range(count):
            if labels[start]>=0:continue
            ident=len(sizes);labels[start]=ident;queue=deque([start]);size=0
            while queue:
                i=queue.popleft();size+=1
                for j in adjacent[i]:
                    if j>=0 and labels[j]<0:
                        labels[j]=ident;queue.append(int(j))
            sizes.append(size)
        require(min(sizes)>=self.settings['min_component_cells'],'UNDER_RESOLVED_COMPONENT','An interior component has too few cells for the explicit volume policy',{'component_cells':sizes,'minimum':self.settings['min_component_cells']})
        self.adjacent=adjacent;self.conductance=conductance;self.neighbor_deltas=deltas;self.labels=labels
        self.stats.update(connected_components=len(sizes),component_cells=sizes,raw_neighbor_edges=raw,
            cross_surface_edges_blocked=blocked,graph_edges=raw-blocked,neighbor_count=neighbor_count,
            neighbor_deltas=deltas.tolist(),weighted_degree_min=float(conductance.sum(axis=1).min()),
            weighted_degree_max=float(conductance.sum(axis=1).max()),
            edges_by_squared_grid_distance={str(key):value for key,value in accepted_by_distance.items()},
            blocked_edges_by_squared_grid_distance={str(key):value for key,value in blocked_by_distance.items()},
            conductance=self.h if neighbor_count==6 else 'dimensionless 1 / squared grid delta length',
            solver_conductance='uniform 1 after common h factor cancels' if neighbor_count==6 else 'symmetric positive weights 1, 1/2, 1/3',
            graph_boundary=('no edge to air; zero flux. Shared-face conductance h^2/h=h (uniform common factor cancels).'
                            if neighbor_count==6 else
                            'no edge through a surface or to air; weighted graph Laplacian, not shared-face finite volume'))
