"""Temporal side-hint experiment; v063 own implementation copied with explicit seed input. Not default."""
"""Experimental whole-chain position constraints, no bpy and no live handler.

Original NumPy implementation of ordinary length/halfspace projections.
PBD distance-constraint design reference is recorded by the caller.
Conservative convex support model; a failure never proves no mesh path exists.
"""
import time
import numpy as np


def planes_for(joints, colliders, margin):
    result = []
    for edge in range(len(joints)-1):
        pair = joints[edge:edge+2]
        for collider in colliders:
            axes = np.concatenate((collider.axes,-collider.axes),axis=0)
            limits = (collider.vertices@axes.T).max(axis=0)
            gaps = (pair@axes.T).min(axis=0)-limits
            k = int(np.argmax(gaps))
            result.append({'edge':edge,'collider':collider.name,
                'axis':axes[k].copy(),'limit':float(limits[k]+margin),
                'original_gap':float(gaps[k])})
    return result


def solve(joints, lengths, colliders, config, plane_seed=None):
    x = np.asarray(joints,dtype=np.float64).copy()
    lengths = np.asarray(lengths,dtype=np.float64)
    if x.ndim!=2 or x.shape[1]!=3 or lengths.shape!=(len(x)-1,):
        return {'status':'rejected','reason':'invalid_shape'}
    if not np.isfinite(x).all() or not np.isfinite(lengths).all() or np.any(lengths<=1e-12):
        return {'status':'rejected','reason':'nonfinite_or_degenerate_length'}
    if plane_seed is not None:
        plane_seed=np.asarray(plane_seed,dtype=np.float64)
        if plane_seed.shape!=x.shape or not np.isfinite(plane_seed).all():
            return {'status':'rejected','reason':'invalid_plane_seed'}
    root=x[0].copy(); start=time.monotonic()
    planes=planes_for(x if plane_seed is None else plane_seed,colliders,config['solver_support_margin_BU'])
    frozen=[p for p in planes if p['edge']==0 and float(root@p['axis'])<p['limit']]
    def finish(status,reason,iteration,history):
        lens=np.linalg.norm(np.diff(x,axis=0),axis=1)
        deficits=[max(0.,p['limit']-float(x[i]@p['axis'])) for p in planes for i in (p['edge'],p['edge']+1)]
        return {'status':status,'reason':reason,'iterations':iteration,
          'wall_seconds':time.monotonic()-start,'joints':x.tolist(),
          'lengths':lens.tolist(),'max_length_residual_BU':float(np.max(np.abs(lens-lengths))),
          'max_plane_deficit_BU':max(deficits),'root_residual_BU':float(np.linalg.norm(x[0]-root)),
          'planes':[{**p,'axis':p['axis'].tolist()} for p in planes], 'history':history}
    if frozen:
        out=finish('rejected','fixed_root_violates_conservative_plane',0,[])
        out['conflicts']=[{**p,'axis':p['axis'].tolist()} for p in frozen]
        return out
    weights=np.ones(len(x));weights[0]=0
    def length_project(i):
        delta=x[i+1]-x[i];distance=float(np.linalg.norm(delta))
        if distance<=1e-14:raise ValueError('collapsed_iterate')
        corr=delta*((distance-lengths[i])/distance)/(weights[i]+weights[i+1])
        x[i]+=weights[i]*corr;x[i+1]-=weights[i+1]*corr
    history=[];best=np.inf;stalled=0
    for iteration in range(1,config['max_iterations']+1):
        for p in planes:
            axis=p['axis'];limit=p['limit']
            for i in (p['edge'],p['edge']+1):
                if weights[i]:x[i]+=max(0.,limit-float(x[i]@axis))*axis
        try:
            for i in range(len(lengths)):length_project(i)
            for i in reversed(range(len(lengths))):length_project(i)
        except ValueError as error:
            return finish('unresolved',str(error),iteration,history)
        if iteration%config['check_every_iterations']==0:
            out=finish('pending','',iteration,history)
            le=out['max_length_residual_BU'];pe=out['max_plane_deficit_BU']
            merit=max(le/config['length_tolerance_BU'],pe/config['plane_tolerance_BU'])
            history.append({'iteration':iteration,'length_error_BU':le,'plane_deficit_BU':pe,'normalized_merit':merit})
            if merit<=1:return finish('candidate','constraints_satisfied',iteration,history)
            if merit < best*(1-config['minimum_relative_merit_improvement']):best=merit;stalled=0
            else:stalled+=1
            if stalled>=config['no_progress_checkpoints']:return finish('unresolved','no_progress',iteration,history)
            if time.monotonic()-start>=config['max_wall_seconds']:return finish('unresolved','wall_budget',iteration,history)
    return finish('unresolved','iteration_budget',iteration,history)
