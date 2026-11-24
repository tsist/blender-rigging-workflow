"""Bounded surface registration and exact landmark warp. No bpy or target rig.

CPD is an experimental surface-atlas proposer, not anatomical recognition.
The RBF fit is the independent reference-bone mapping after marker editing.
"""
import numpy as np
from .vendor.pycpd import DeformableRegistration


def points(value, minimum=4):
    a = np.asarray(value, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 3 or len(a) < minimum or not np.isfinite(a).all():
        raise ValueError('需要足够的有限三维点')
    return a


def farthest_sample(a, count):
    """Geometry-only deterministic coverage, not mesh-index correspondences."""
    a = points(a)
    if len(a) <= count:
        return a.copy()
    chosen = np.empty(count, dtype=int)
    chosen[0] = np.argmin(a[:, 2])
    distance = np.full(len(a), np.inf)
    for i in range(1, count):
        distance = np.minimum(distance, np.sum((a-a[chosen[i-1]])**2, axis=1))
        chosen[i] = np.argmax(distance)
    return a[chosen]


def propose_surface(source, target, landmarks, samples=420, iterations=55):
    """Same-axis region fit: bbox initialization then full-rank nonrigid CPD.

    Assumes explicit matching surface regions in neutral A/T-like poses. Returns
    diagnostics, never a calibrated confidence or an automatic acceptance.
    """
    source, target, landmarks = points(source), points(target), points(landmarks, 1)
    slo, shi, tlo, thi = source.min(0), source.max(0), target.min(0), target.max(0)
    sspan, tspan = shi-slo, thi-tlo
    if min(sspan) <= 1e-9 or min(tspan) <= 1e-9:
        raise ValueError('定位表面退化；请重新选择部件')
    scale = float(max(tspan))
    initial = lambda a: ((a-slo)/sspan*tspan+tlo-tlo)/scale
    y = farthest_sample(initial(source), samples)
    x = farthest_sample((target-tlo)/scale, samples)
    initial_landmarks = initial(landmarks)
    # Identical surfaces have an exact identity solution; do not let finite
    # sampling and EM shrink an already equal geometry pair.
    if source.shape == target.shape and np.array_equal(source, target):
        return landmarks.copy(), {'method':'surface_atlas_cpd', 'identity_surface':True,
            'iterations':0, 'status':'needs_review', 'sample_count':len(y)}
    distances = ((y[:, None]-x[None])**2).sum(2)
    before = float(np.sqrt(distances.min(1).mean()))
    reg = DeformableRegistration(X=x, Y=y, alpha=3.0, beta=0.22,
        sigma2=max(before**2, 0.002), max_iterations=iterations,
        tolerance=1e-7, w=0.08, low_rank=False)
    reg.register()
    output = reg.transform_point_cloud(Y=initial_landmarks)*scale+tlo
    if not np.isfinite(output).all() or not np.isfinite(reg.sigma2):
        raise ValueError('表面配准未得到有限结果；没有提交候选')
    after = float(np.sqrt(((reg.TY[:, None]-x[None])**2).sum(2).min(1).mean()))
    return output, {'method':'surface_atlas_cpd', 'status':'needs_review',
        'iterations':reg.iteration, 'sample_count':len(y),
        'surface_rms_before':before*scale, 'surface_rms_after':after*scale,
        'sigma2_normalized':float(reg.sigma2),
        'limitations':'surface similarity is not anatomical correctness; manual review required'}


def lip_region_points(source, target, landmarks):
    """One ROI formula for preflight and refinement, within the supplied domain."""
    source,target,landmarks=points(source),points(target),points(landmarks)
    width=float(np.ptp(landmarks[:,0]))
    if width <= 1e-6:
        raise ValueError('嘴唇图谱标记没有宽度')
    lo=landmarks.min(0)-np.array([.6,.45,.55])*width
    hi=landmarks.max(0)+np.array([.6,.45,.55])*width
    source_lo,source_span=source.min(0),np.ptp(source,axis=0)
    target_lo,target_span=target.min(0),np.ptp(target,axis=0)
    if min(source_span) <= 1e-9 or min(target_span) <= 1e-9:
        raise ValueError('嘴唇定位域退化；请重新选择范围')
    tlo=(lo-source_lo)/source_span*target_span+target_lo
    thi=(hi-source_lo)/source_span*target_span+target_lo
    src=source[np.all((source>=lo)&(source<=hi),axis=1)]
    dst=target[np.all((target>=tlo)&(target<=thi),axis=1)]
    report=dict(source_region=[lo.tolist(),hi.tolist()],target_region=[tlo.tolist(),thi.tolist()],
                source_points=len(src),target_points=len(dst))
    if min(len(src),len(dst))<30:
        raise ValueError('嘴唇局部表面不足（源%d点，目标%d点，至少30点），需重新选择区域' % (len(src),len(dst)))
    return src,dst,report


def propose_lip_region(source, target, landmarks):
    """Refine lips using the supplied domain bbox, never semantic segmentation."""
    src,dst,region_report=lip_region_points(source,target,landmarks)
    output,report=propose_surface(src,dst,landmarks,samples=320,iterations=55)
    report.update(region_report)
    return output,report


class LandmarkWarp:
    """3D polyharmonic radial interpolation phi(r)=r plus affine polynomial.

    Solves [K P; P.T 0] [W A] = [target-source; 0]. Affine reproduction and
    exact interpolation avoid changing already accepted joints in the fit.
    """
    def __init__(self, source, target):
        source, target = points(source), points(target)
        if source.shape != target.shape:
            raise ValueError('源与目标标记数不匹配')
        self.origin = source.mean(0)
        self.scale = float(np.ptp(source, axis=0).max())
        if self.scale <= 0:
            raise ValueError('标记没有空间跨度')
        # Coincident source joints must agree; silently averaging would erase
        # a locked/manual marker. Merge only numerically identical constraints.
        unique, values = [], []
        for s, t in zip(source, target):
            hits = [i for i, p in enumerate(unique) if np.linalg.norm(s-p) < self.scale*1e-7]
            if hits:
                if np.linalg.norm(t-values[hits[0]]) > self.scale*2e-6:
                    raise ValueError('重合源关节点具有互相冲突的目标标记')
            else:
                unique.append(s); values.append(t)
        self.source = np.asarray(unique)
        y = np.asarray(values)
        self.x = (self.source-self.origin)/self.scale
        p = np.column_stack((np.ones(len(self.x)), self.x))
        if np.linalg.matrix_rank(p) < 4:
            raise ValueError('标记共面或不足，不能确定完整三维拟合')
        k = np.linalg.norm(self.x[:, None]-self.x[None], axis=2)
        matrix = np.block([[k, p], [p.T, np.zeros((4, 4))]])
        rhs = np.vstack(((y-self.source)/self.scale, np.zeros((4, 3))))
        self.solution = np.linalg.solve(matrix, rhs)
        # Validate EVERY original constraint, including near-coincident ones.
        self.residual = float(np.max(np.linalg.norm(self(source)-target, axis=1)))
        if not np.isfinite(self.solution).all() or self.residual > self.scale*1e-6:
            raise ValueError('参考骨插值数值不稳定；没有写入骨架')

    def __call__(self, query):
        query = points(query, 1)
        q = (query-self.origin)/self.scale
        # Bound intermediate arrays for full template head/tail/roll transport.
        out = []
        for i in range(0, len(q), 512):
            v = q[i:i+512]
            basis = np.column_stack((np.linalg.norm(v[:, None]-self.x[None], axis=2),
                                     np.ones(len(v)), v))
            out.append(query[i:i+512] + basis@self.solution*self.scale)
        return np.concatenate(out)
