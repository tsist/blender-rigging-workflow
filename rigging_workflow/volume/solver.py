# SPDX-License-Identifier: GPL-3.0-or-later
"""Dirichlet harmonic bone weights on explicit closed-volume cell graphs.

This independently authored CPU solver offers six-face finite volume or an
explicit 26-neighbor weighted graph. It is not AAS, SHD, Unity code, adaptive
multigrid or a GPU backend. No bpy data is written.
"""
import math
import time
import numpy as np
from .contract import VolumeError, require, normalize, Budget, array
from .geometry import Domain, validate_closed


def _estimate(grid_cells,vertices,triangles,bones,retained_output=0,neighbor_count=6,sample_count=None):
    # Conservative working-set accounting: dense grid/mask/index arrays, domain
    # coordinates/adjacency, weight+seed normalization temporary, PCG gather and
    # vectors, input topology temporaries and an explicit opaque BVH allowance.
    # It is an estimate, never advertised as a measured RSS or OS hard limit.
    # Include stored edge coefficients and simultaneous adjacency/gather/where
    # temporaries per neighbor. Dense cells upper-bound occupied/free cells.
    sample_count=vertices if sample_count is None else sample_count
    return int(retained_output + sample_count*bones*8 + grid_cells*(256+16*bones+neighbor_count*64)
               + vertices*96 + triangles*(24*5+192))


def _seeds(domain,bones):
    count=len(domain.centers);h=domain.h
    weights=np.zeros((count,len(bones)),dtype=np.float64)
    details=[]
    for column,bone in enumerate(bones):
        if domain.progress is not None:
            domain.progress('seeds',done=column,total=len(bones),unit='bone_columns',
                            bone=bone['name'])
        domain.budget.check()
        head=bone['head']-domain.world_origin;tail=bone['tail']-domain.world_origin
        axis=tail-head;length2=float(np.dot(axis,axis))
        t=np.clip((domain.centers-head)@axis/length2,0,1)
        closest=head+t[:,None]*axis
        distances=np.linalg.norm(domain.centers-closest,axis=1)
        candidates=np.flatnonzero(distances<=domain.settings['seed_radius_cells']*h)
        accepted=[];projected=[];internal=[];projected_nodes=[];rejected_crossings=0
        for number,node in enumerate(candidates):
            if number%512==0:domain.budget.check()
            p=closest[node];center=domain.centers[node];distance=float(distances[node])
            inside=domain.inside(p)
            if inside is True:
                if not domain.clear_segment(p,center):
                    rejected_crossings+=1;continue
                internal.append(int(node))
            else:
                if distance>domain.settings['max_seed_projection_cells']*h or domain.settings['max_seed_projection_cells']==0:
                    continue
                if inside is None:
                    if not domain.clear_segment(p,center):
                        rejected_crossings+=1;continue
                else:
                    if distance<=domain.eps:continue
                    direction=(center-p)/distance
                    hits=domain.intersections(p+direction*domain.eps*2,direction,max(0,distance-domain.eps*4))
                    # A bounded projection may enter this volume exactly once;
                    # it may not leave one finger and enter its neighbor.
                    if len(hits)!=1:
                        rejected_crossings+=1;continue
                projected.append(distance)
                projected_nodes.append(int(node))
            accepted.append(int(node))
        internal_components=set(domain.labels[internal].tolist())
        projected_components=set(domain.labels[projected_nodes].tolist())
        require(internal_components or len(projected_components)<=1,'AMBIGUOUS_SEED_PROJECTION',
            'An external bone would project into multiple disconnected volumes; provide an explicit anatomical partition',
            {'bone':bone['name'],'projected_components':sorted(projected_components),'projection_bound':domain.settings['max_seed_projection_cells']*h})
        rejected_projection_components=0
        if internal_components:
            keep=[i for i,node in enumerate(projected_nodes) if int(domain.labels[node]) in internal_components]
            rejected_projection_components=len(projected_nodes)-len(keep)
            accepted=internal+[projected_nodes[i] for i in keep]
            projected=[projected[i] for i in keep]
        if accepted:
            ids=np.asarray(accepted,dtype=np.int32)
            weights[ids,column]=1.0/(distances[ids]+h*.25)**2
        details.append({'bone':bone['name'],'seed_cells':len(accepted),'projected_cells':len(projected),
            'max_projection_distance':max(projected,default=0.),'cross_surface_candidates_rejected':rejected_crossings,
            'outside_projection_components_rejected':rejected_projection_components})
        if domain.progress is not None:
            domain.progress('seeds',done=column+1,total=len(bones),unit='bone_columns',
                            bone=bone['name'],seed_cells=len(accepted),
                            projected_cells=len(projected),
                            rejected_crossings=rejected_crossings)
    row_sum=weights.sum(axis=1)
    fixed=row_sum>0
    require(bool(fixed.any()),'NO_SEEDS','No bone segment has valid internal or explicitly bounded projected source cells',{'bone_seeds':details})
    missing=[d['bone'] for d in details if not d['seed_cells']]
    require(not missing or not domain.settings['require_all_bones_seeded'],'UNSEEDED_BONES','Requested bones lack valid volume seeds; choose an explicit partition/range or repair the domain',{'unseeded_bones':missing,'bone_seeds':details})
    weights[fixed]/=row_sum[fixed,None]
    components=[]
    unsourced=[]
    for label,size in enumerate(domain.stats['component_cells']):
        mask=domain.labels==label
        seeds=int(np.count_nonzero(fixed&mask))
        rows={'component':label,'cells':size,'seed_cells':seeds,
              'bones':[bones[b]['name'] for b in range(len(bones)) if np.any(weights[mask,b]>0)]}
        components.append(rows)
        if seeds==0:unsourced.append(label)
    require(not unsourced,'UNSEEDED_COMPONENT','Every actual connected volume component needs a valid bone boundary; no nearest-bone fallback',{'unseeded_components':unsourced,'components':components,'bone_seeds':details})
    domain.stats.update(seed_cells=int(np.count_nonzero(fixed)),bone_seeds=details,
        unseeded_bones=missing,components=components,unseeded_components=[],
        seed_policy='distance-to-segment source radius; inside visibility or bounded single-entry projection; overlapping sources normalized')
    return weights,fixed


def _diffuse(domain,weights,fixed):
    adjacent=domain.adjacent
    free=np.flatnonzero(~fixed)
    fmap=np.full(len(weights),-1,dtype=np.int32);fmap[free]=np.arange(len(free),dtype=np.int32)
    neighbor=adjacent[free]
    conductance=domain.conductance[free]
    safe=np.maximum(neighbor,0)
    free_neighbor=fmap[safe];free_neighbor[neighbor<0]=-1
    degree=conductance.sum(axis=1)
    require(bool((degree>0).all()),'UNSEEDED_COMPONENT','An unfixed node has no active graph neighbors')
    is_boundary=(neighbor>=0)&fixed[safe]
    def apply(x):
        gathered=x[np.maximum(free_neighbor,0)]
        gathered[free_neighbor<0]=0
        return degree*x-(conductance*gathered).sum(axis=1)
    if len(free):
        # A constant partition field is invariant: L_FF*1 equals L_FS*1.
        boundary_sum=(conductance*np.where(is_boundary,weights.sum(axis=1)[safe],0)).sum(axis=1)
        invariant=float(np.max(np.abs(apply(np.ones(len(free)))-boundary_sum)))
    else:invariant=0.
    require(invariant<=1e-10,'MATRIX_INVARIANT','Weighted constant-field partition is not preserved',{'constant_field_residual':invariant})
    columns=[];bone_total=weights.shape[1]
    for column in range(bone_total):
        bone_name=domain.stats['bone_seeds'][column]['bone']
        if domain.progress is not None:
            domain.progress('diffusion',done=column,total=bone_total,unit='bone_columns',
                            bone=bone_name,bone_index=column+1,bone_total=bone_total)
        domain.budget.check()
        if not len(free):
            columns.append({'bone':domain.stats['bone_seeds'][column]['bone'],'iterations':0,'relative_residual':0.})
            if domain.progress is not None:
                domain.progress('diffusion',done=column+1,total=bone_total,unit='bone_columns',
                                bone=bone_name,bone_index=column+1,bone_total=bone_total,
                                iteration=0,residual=0.)
            continue
        rhs=(conductance*np.where(is_boundary,weights[safe,column],0)).sum(axis=1)
        norm_rhs=float(np.linalg.norm(rhs))
        if norm_rhs==0:
            weights[free,column]=0
            columns.append({'bone':domain.stats['bone_seeds'][column]['bone'],'iterations':0,'relative_residual':0.})
            if domain.progress is not None:
                domain.progress('diffusion',done=column+1,total=bone_total,unit='bone_columns',
                                bone=bone_name,bone_index=column+1,bone_total=bone_total,
                                iteration=0,residual=0.)
            continue
        if bone_total==1:
            # _seeds has already verified a Dirichlet source in every connected
            # component. With one bone all boundary values are exactly one;
            # the unique harmonic extension is the same constant. Evaluate its
            # actual residual instead of introducing avoidable PCG roundoff.
            require(bool((weights[fixed,column]==1).all()),'MATRIX_INVARIANT',
                    'Single-bone boundary values must be exactly one')
            x=np.ones(len(free),dtype=np.float64)
            actual=float(np.linalg.norm(rhs-apply(x)))/norm_rhs
            require(actual<=domain.settings['tolerance'],'MATRIX_INVARIANT',
                    'Single-bone constant solution failed the actual graph residual',
                    {'relative_residual':actual})
            weights[free,column]=x
            columns.append({'bone':bone_name,'iterations':0,'relative_residual':actual,
                            'method':'exact constant Dirichlet solution'})
            if domain.progress is not None:
                domain.progress('diffusion',done=column+1,total=bone_total,unit='bone_columns',
                                bone=bone_name,bone_index=column+1,bone_total=bone_total,
                                iteration=0,residual=actual)
            continue
        x=np.zeros(len(free),dtype=np.float64)
        residual=rhs.copy();z=residual/degree;direction=z.copy();rz=float(np.dot(residual,z))
        converged=False;actual=math.inf;iteration=0
        for iteration in range(1,domain.settings['max_iterations']+1):
            domain.budget.check()
            product=apply(direction);denominator=float(np.dot(direction,product))
            require(math.isfinite(denominator) and denominator>0,'NUMERICAL_BREAKDOWN','PCG matrix ceased to be positive definite')
            alpha=rz/denominator;x+=alpha*direction;residual-=alpha*product
            relative=float(np.linalg.norm(residual))/norm_rhs
            if domain.progress is not None:
                domain.progress('diffusion',bone=bone_name,bone_index=column+1,
                                bone_total=bone_total,iteration=iteration,
                                residual=relative)
            if relative<=domain.settings['tolerance']:
                true_residual=rhs-apply(x);actual=float(np.linalg.norm(true_residual))/norm_rhs
                if actual<=domain.settings['tolerance']:
                    converged=True;break
                residual=true_residual;z=residual/degree;direction=z.copy();rz=float(np.dot(residual,z))
                continue
            z=residual/degree;next_rz=float(np.dot(residual,z))
            require(math.isfinite(next_rz) and next_rz>=0 and rz>0,'NUMERICAL_BREAKDOWN','PCG residual became nonfinite')
            direction=z+(next_rz/rz)*direction;rz=next_rz
        actual=float(np.linalg.norm(rhs-apply(x)))/norm_rhs
        columns.append({'bone':domain.stats['bone_seeds'][column]['bone'],'iterations':iteration,'relative_residual':actual})
        require(converged,'NO_CONVERGENCE','Volume PCG did not converge at the requested tolerance; partial fields are not accepted',{'columns':columns,'tolerance':domain.settings['tolerance'],'unfixed_nodes':len(free)})
        if domain.progress is not None:
            domain.progress('diffusion',done=column+1,total=bone_total,unit='bone_columns',
                            bone=bone_name,bone_index=column+1,bone_total=bone_total,
                            iteration=iteration,residual=actual)
        weights[free,column]=x
    require(bool(np.isfinite(weights).all()),'INVALID_WEIGHTS','Diffusion produced nonfinite weights')
    minimum=float(weights.min());maximum=float(weights.max())
    numeric_tolerance=max(1e-8,domain.settings['tolerance']*10)
    require(minimum>=-numeric_tolerance and maximum<=1+numeric_tolerance,'INVALID_WEIGHTS','Diffusion violated nonnegative bounded harmonic weights',{'minimum':minimum,'maximum':maximum})
    partition_error=float(np.max(np.abs(weights.sum(axis=1)-1)))
    sum_tolerance=max(1e-5,8*domain.settings['tolerance']*weights.shape[1])
    require(partition_error<=sum_tolerance,'PARTITION_ERROR','Partition-of-unity error is too large; normalization cannot hide it',{'partition_error':partition_error,'allowed_numeric_error':sum_tolerance})
    clamped=int(np.count_nonzero((weights<0)|(weights>1)))
    np.clip(weights,0,1,out=weights)
    weights/=weights.sum(axis=1)[:,None]
    domain.stats.update(unfixed_cells=len(free),solver=(
        'exact constant single-bone Dirichlet solution' if bone_total==1
        else 'matrix-free PCG with positive degree preconditioner'),
        convergence=columns,constant_field_residual=invariant,raw_min_weight=minimum,raw_max_weight=maximum,
        pre_normalization_partition_error=partition_error,numeric_partition_tolerance=sum_tolerance,
        roundoff_values_clamped=clamped,negative_or_nonfinite_output=0)
    return weights


def _sample(domain,vertices,weights):
    result=np.empty((len(vertices),weights.shape[1]),dtype=np.float64)
    local=vertices-domain.world_origin
    radius=domain.settings['sample_radius_cells']*domain.h
    extent=math.ceil(domain.settings['sample_radius_cells'])+1
    distances=[];counts=[];uncovered=[];ambiguous=[]
    for row,point in enumerate(local):
        if row%128==0:
            if domain.progress is not None:
                domain.progress('sample',done=row,total=len(vertices),unit='vertices')
            domain.budget.check()
        center=np.floor((point-domain.grid_low)/domain.h).astype(np.int64)
        low=np.maximum(center-extent,0);high=np.minimum(center+extent+1,domain.shape)
        ids=domain.grid[low[0]:high[0],low[1]:high[1],low[2]:high[2]].reshape(-1)
        ids=ids[ids>=0]
        if len(ids):
            d=np.linalg.norm(domain.centers[ids]-point,axis=1)
            selected=d<=radius
            ids=ids[selected];d=d[selected]
            visible=np.asarray([domain.clear_segment(point,domain.centers[i]) for i in ids],dtype=bool)
            ids=ids[visible];d=d[visible]
        if not len(ids):
            uncovered.append(row);continue
        if len(np.unique(domain.labels[ids]))!=1:
            ambiguous.append(row);continue
        interpolation=1.0/np.maximum(d,domain.eps)**2
        interpolation/=interpolation.sum()
        result[row]=interpolation@weights[ids]
        distances.append(float(d.min()));counts.append(len(ids))
    domain.stats.update(sample_uncovered_vertices=len(uncovered),sample_uncovered_examples=uncovered[:32],
        sample_ambiguous_vertices=len(ambiguous),sample_ambiguous_examples=ambiguous[:32],
        sample_max_projection_distance=max(distances,default=0.),sample_mean_projection_distance=float(np.mean(distances)) if distances else None,
        sample_min_internal_neighbors=min(counts,default=0),sample_max_internal_neighbors=max(counts,default=0),
        sample_radius=radius,sampling='bounded visible interior-cell inverse-distance interpolation; no nearest-bone fallback')
    require(not uncovered and not ambiguous,'UNCOVERED_VERTICES','Surface sampling has uncovered/ambiguous vertices at this cell size; refine/repartition explicitly',dict(domain.stats))
    require(bool(np.isfinite(result).all()) and float(result.min())>=0,'INVALID_WEIGHTS','Sampled weights contain invalid values')
    error=float(np.max(np.abs(result.sum(axis=1)-1)))
    require(error<=1e-5,'PARTITION_ERROR','Surface sampling did not preserve the constant partition field',{'sample_partition_error':error})
    result/=result.sum(axis=1)[:,None]
    domain.stats['sample_partition_error']=error
    if domain.progress is not None:
        domain.progress('sample',done=len(vertices),total=len(vertices),unit='vertices',
                        uncovered=len(uncovered),ambiguous=len(ambiguous))
    return result.astype(np.float32)


def solve(meshes,bones,settings,*,cancel_check=None,progress=None,sample_points=None):
    """Return full-order NxB arrays or raise VolumeError with diagnostics.

    `settings` is a finite typed data object. `cancel_check` is an optional
    trusted in-process callback, never read from an external JSON setting.
    `progress` is an optional trusted in-process callable invoked as
    progress(phase, **details); it is never read from settings or JSON.
    `sample_points` optionally declares a nonempty Nx3 array for every mesh id.
    Only those points are sampled; the closed processing volume is unchanged.
    """
    require(progress is None or callable(progress),'INVALID_INPUT','progress is a trusted internal callable, not JSON settings')
    started=time.perf_counter();statistics={'schema':'rw.volume-diffusion/1.0','partitions':[],'timings':{},
        'method':'uniform closed-volume six-neighbor harmonic diffusion','mesh_mode':'independent',
        'writes_blender_data':False,'fallback_used':False}
    active_domain=None;active_mesh=None;active_phase={};stage='validation';budget=None
    try:
        source,bone_rows,config,partitions=normalize(meshes,bones,settings)
        if sample_points is None:
            sampling={m['id']:m['vertices'] for m in source}
            sample_input_bytes=0
        else:
            require(isinstance(sample_points,dict) and set(sample_points)=={m['id'] for m in source},
                    'INVALID_INPUT','sample_points must exactly cover mesh ids')
            sampling={}
            for mesh in source:
                points=array(sample_points[mesh['id']],'sample_points for '+mesh['id'],3,
                             max_bytes=config['max_memory_mb']*1048576)
                require(len(points)>0,'INVALID_INPUT','Declared sample_points must be nonempty')
                require(float(np.max(np.abs(points)))<=1e12,'NUMERICAL_RANGE','Sample world coordinates exceed the declared numeric working range')
                sampling[mesh['id']]=points
            sample_input_bytes=sum(points.nbytes for points in sampling.values())
        statistics['method']=('uniform closed-volume six-neighbor harmonic diffusion' if config['neighbor_count']==6
                              else 'uniform closed-volume 26-neighbor weighted graph harmonic diffusion')
        statistics['sampling_scope']='full_processing_surface' if sample_points is None else 'declared_application_points'
        budget=Budget(config,cancel_check,progress);budget.started=started;budget.check()
        budget.report('validation',mesh_total=len(source),bone_total=len(bone_rows),
                      partition_total=len(partitions))
        statistics['timings']['validation']=time.perf_counter()-started
        output_bytes=sum(len(points)*len(bone_rows)*4 for points in sampling.values())
        input_bytes=sum(m['vertices'].nbytes+m['triangles'].nbytes for m in source)+sample_input_bytes
        require(output_bytes+input_bytes<=config['max_memory_mb']*1048576,'RESOURCE_LIMIT','Input/output arrays alone exceed explicit memory estimate budget',{'input_bytes':input_bytes,'output_bytes':output_bytes})
        outputs={};domains={};warnings=[];total_grid_cells=0;memory_estimate=0;retained_debug=0
        bone_index={b['name']:i for i,b in enumerate(bone_rows)}
        mesh_index={m['id']:m for m in source}
        for partition_index,partition in enumerate(partitions):
            budget.check();mesh=mesh_index[partition['mesh_id']]
            active_mesh=mesh['id'];active_domain=None
            selected=[bone_rows[bone_index[name]] for name in partition['bone_names']]
            vertices=mesh['vertices'];triangles=mesh['triangles']
            phase={};active_phase=phase;stage='resource_preflight'
            span=np.ptp(vertices,axis=0)
            h=config['cell_size'] or float(np.max(span))/config['cells_longest']
            shape=tuple(math.ceil(float(s/h))+2 for s in span)
            cells=math.prod(shape);total_grid_cells+=cells
            require(total_grid_cells<=config['max_cells'],'RESOURCE_LIMIT','Total independent partition grids exceed max_cells',{'requested_total_grid_cells':total_grid_cells,'max_cells':config['max_cells']})
            estimate=_estimate(cells,len(vertices),len(triangles),len(selected),input_bytes+output_bytes+retained_debug,
                               config['neighbor_count'],len(sampling[mesh['id']]))
            memory_estimate=max(memory_estimate,estimate)
            require(estimate<=config['max_memory_mb']*1048576,'RESOURCE_LIMIT','Conservative domain/PCG/BVH estimate exceeds explicit memory budget',{'estimated_bytes':estimate,'max_memory_mb':config['max_memory_mb'],'grid_shape':shape})
            budget.report('resource_preflight',mesh_id=mesh['id'],
                          partition_index=partition_index,partition_total=len(partitions),
                          grid_cells=cells,estimated_bytes=estimate,
                          vertex_count=len(vertices),triangle_count=len(triangles),
                          bone_total=len(selected))
            stage='closed_surface';at=time.perf_counter();surface=validate_closed(vertices,triangles);phase['closed_surface']=time.perf_counter()-at
            stage='occupancy';at=time.perf_counter();domain=Domain(vertices,triangles,h,config,budget);active_domain=domain
            if progress is not None:
                domain.progress=lambda phase,**details: budget.report(phase,
                    mesh_id=mesh['id'],partition_index=partition_index,
                    partition_total=len(partitions),**details)
            budget.report('occupancy',mesh_id=mesh['id'],partition_index=partition_index,
                          partition_total=len(partitions),done=0,total=shape[1],
                          unit='scanline_columns',grid_cells=cells)
            domain.occupy();phase['occupancy']=time.perf_counter()-at
            budget.report('occupancy',mesh_id=mesh['id'],partition_index=partition_index,
                          partition_total=len(partitions),done=shape[1],total=shape[1],
                          unit='scanline_columns',occupied_cells=len(domain.indices),
                          ray_columns=domain.stats['ray_columns'],
                          ambiguous_columns=domain.stats['ambiguous_columns'])
            stage='graph';at=time.perf_counter()
            graph_total=(config['neighbor_count']//2)*len(domain.centers)
            budget.report('graph',mesh_id=mesh['id'],partition_index=partition_index,
                          partition_total=len(partitions),done=0,total=graph_total,
                          unit='cell_axis_visits',cells=len(domain.centers))
            domain.graph();phase['graph']=time.perf_counter()-at
            budget.report('graph',mesh_id=mesh['id'],partition_index=partition_index,
                          partition_total=len(partitions),done=graph_total,total=graph_total,
                          unit='cell_axis_visits',cells=len(domain.centers),
                          connected_components=domain.stats['connected_components'],
                          graph_edges=domain.stats['graph_edges'],
                          cross_surface_edges_blocked=domain.stats['cross_surface_edges_blocked'])
            stage='seeds';at=time.perf_counter();weights,fixed=_seeds(domain,selected);phase['seeds']=time.perf_counter()-at
            stage='diffusion';at=time.perf_counter();weights=_diffuse(domain,weights,fixed);phase['diffusion']=time.perf_counter()-at
            stage='surface_sampling';at=time.perf_counter();sampled=_sample(domain,sampling[mesh['id']],weights);phase['surface_sampling']=time.perf_counter()-at
            full=np.zeros((len(sampled),len(bone_rows)),dtype=np.float32)
            full[:,[bone_index[name] for name in partition['bone_names']]]=sampled
            outputs[mesh['id']]=full
            domain.stats.update(mesh_id=mesh['id'],bone_names=partition['bone_names'],surface=surface,
                memory_estimate_bytes=estimate,actual_retained_numpy_bytes=int(domain.indices.nbytes+domain.centers.nbytes+domain.grid.nbytes+domain.adjacent.nbytes+domain.conductance.nbytes+domain.labels.nbytes+weights.nbytes+fixed.nbytes+sampled.nbytes),
                sampling_scope=statistics['sampling_scope'],sampling_point_count=len(sampled),processing_vertex_count=len(vertices),
                timings=phase,resolution_policy='explicit uniform cell size; never automatically enlarged to meet a budget')
            statistics['partitions'].append(domain.stats)
            if domain.stats['unseeded_bones']:
                warnings.append({'mesh_id':mesh['id'],'code':'EXPLICIT_UNSEEDED_BONE_COLUMNS','bones':domain.stats['unseeded_bones']})
            projected=sum(row['projected_cells'] for row in domain.stats['bone_seeds'])
            if projected:
                warnings.append({'mesh_id':mesh['id'],'code':'BOUNDED_SEED_PROJECTION','cells':projected,
                                 'bound':config['max_seed_projection_cells']*h})
            if config['return_domain']:
                domains[mesh['id']]={'centers':domain.centers+domain.world_origin,'weights':weights.copy(),
                    'components':domain.labels.copy(),'adjacent':domain.adjacent.copy(),
                    'conductance':domain.conductance.copy(),'neighbor_deltas':domain.neighbor_deltas.copy(),'bone_names':partition['bone_names']}
                retained_debug+=sum(a.nbytes for a in domains[mesh['id']].values() if isinstance(a,np.ndarray))
            active_domain=None;active_mesh=None
            del domain,weights,fixed,sampled,full
        statistics.update(total_grid_cells=total_grid_cells,max_memory_estimate_bytes=memory_estimate,
            input_array_bytes=input_bytes,output_array_bytes=output_bytes,elapsed_seconds=time.perf_counter()-started,
            memory_scope='conservative NumPy working-set plus declared BVH allowance, not measured RSS/OS quota',
            settings=config)
        result={'status':'succeeded','mesh_weights':outputs,'bone_names':[b['name'] for b in bone_rows],
                'statistics':statistics,'warnings':warnings}
        if config['return_domain']:result['domains']=domains
        budget.report('complete',mesh_total=len(source),bone_total=len(bone_rows),
                      partition_total=len(partitions),status='succeeded',
                      elapsed_seconds=statistics['elapsed_seconds'])
        return result
    except VolumeError as exc:
        if budget is not None and budget.callback_raised:raise
        if active_mesh is not None:
            statistics['partitions'].append({**(active_domain.stats if active_domain is not None else {}),
                'mesh_id':active_mesh,'status':'failed','failed_stage':stage,'timings':active_phase})
        exc.statistics={'run':statistics,'failure':exc.statistics,'elapsed_seconds':time.perf_counter()-started}
        raise
    except (ValueError,TypeError,KeyError,IndexError,OverflowError) as exc:
        if budget is not None and budget.callback_raised:raise
        raise VolumeError('INVALID_INPUT','Volume input or numeric shape is invalid: '+str(exc),statistics) from exc
