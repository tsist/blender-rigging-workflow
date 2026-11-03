"""Explicit anatomical-section affine field for a declared donor garment.

Body dimensions come from complete geometric cross sections, independent of
bone weight densities. Shoulder bridges use declared positive interpolation
between adjacent measured domains. This is an assisted, annotated fit field;
it makes no automatic arbitrary-character fit or collision guarantee.
"""
from copy import deepcopy
import numpy as np
from .contract import require
from .fit_geometry import frame


def _measurement(record,label,cut_indices):
    cuts=record[label+'_cuts']
    selected=[]
    for index in cut_indices:
        cut=cuts[index]
        require(cut['qualified_unique_body_section'] and len(cut['selected_component_indices'])==1,
                'ANATOMICAL_SECTION_NOT_UNIQUE',{'group':record['source_group'],'label':label,'cut':index})
        component=cut['components'][cut['selected_component_indices'][0]]
        lo=np.asarray(component['minimum_radial_xz'],dtype=np.float64)
        hi=np.asarray(component['maximum_radial_xz'],dtype=np.float64)
        require(bool(np.isfinite(lo).all()) and bool(np.isfinite(hi).all()) and bool((hi>lo).all()),
                'ANATOMICAL_SECTION_DOMAIN',component)
        selected.append({'t':cut['t'],'span':hi-lo,'center':(hi+lo)*.5})
    return selected


def build_anatomical_fit(source,source_arrays,mapping,section_report):
    """All geometry/Key transport is frozen by donor Basis and explicit domains.

    Radial centers are bounding-box midpoints of complete section loops; means
    use the same fixed section samples on both bodies. This aligns the actual
    measured two-sided surface extents, while retaining bone-axis longitudinal
    position and direction. No source or target array is mutated.
    """
    records={r['source_group']:r for r in section_report['records']}
    specs={};measurements=[]
    maps={m['source_group']:m for m in mapping}
    constants=['DEF-Spine','DEF-RibCage','DEF-Chest','DEF-Hip_Center']
    constants.extend('DEF-UpperArm_'+part+'.'+side for side in ('L','R') for part in ('1','2'))
    for name in constants:
        indices=([0,1] if name=='DEF-Chest' else [1,2,3] if name.startswith('DEF-UpperArm_1.') else [0,1,2,3])
        r=records[name];s=_measurement(r,'source',indices);t=_measurement(r,'target',indices)
        source_span=np.mean([v['span'] for v in s],axis=0);target_span=np.mean([v['span'] for v in t],axis=0)
        spec={'scale':target_span/source_span,'source_center':np.mean([v['center'] for v in s],axis=0),
              'target_center':np.mean([v['center'] for v in t],axis=0)}
        specs[name]=spec
        measurements.append({'source_group':name,'mode':'complete_section_bbox_mean','cut_indices':indices,
                             'source_span':source_span.tolist(),'target_span':target_span.tolist(),
                             **{k:v.tolist() for k,v in spec.items()}})
    for side in ('L','R'):
        name='DEF-Shoulder.'+side;arm='DEF-UpperArm_1.'+side
        # Shoulder is a non-tubular bridge. The scalar radial dimensions are
        # explicitly inherited from adjacent measured domains, not represented
        # as an independently observed shoulder circumference.
        shoulder=maps[name]
        source_sh,source_length=frame(shoulder['source_head'],shoulder['source_tail'])
        target_sh,target_length=frame(shoulder['target_head'],shoulder['target_tail'])
        endpoint=[];endpoint_proofs=[]
        for adjacent,index in (('DEF-Chest',1),(arm,1)):
            r=records[adjacent];s=_measurement(r,'source',[index])[0];t=_measurement(r,'target',[index])[0]
            part=maps[adjacent]
            source_part,sl=frame(part['source_head'],part['source_tail'])
            target_part,tl=frame(part['target_head'],part['target_tail'])
            scale=t['span']/s['span']
            world_linear=target_part@np.diag((scale[0],tl/sl,scale[1]))@source_part.T
            in_shoulder=target_sh.T@world_linear@source_sh
            radial_scale=np.diag(in_shoulder)[[0,2]]
            require(bool((radial_scale>0).all()),'ANATOMICAL_BRIDGE_NONPOSITIVE_RADIAL_PROJECTION',name)
            source_center=source_sh.T@(source_part@np.asarray((s['center'][0],0.,s['center'][1])))
            target_center=target_sh.T@(target_part@np.asarray((t['center'][0],0.,t['center'][1])))
            endpoint.append({'scale':radial_scale,'source_center':source_center[[0,2]],'target_center':target_center[[0,2]]})
            endpoint_proofs.append({'adjacent':adjacent,'cut_index':index,'full_linear_in_shoulder_frames':in_shoulder.tolist(),
                'source_radial_center_world_vector':(source_part@np.asarray((s['center'][0],0.,s['center'][1]))).tolist(),
                'target_radial_center_world_vector':(target_part@np.asarray((t['center'][0],0.,t['center'][1]))).tolist(),
                'source_center_in_shoulder_frame':source_center.tolist(),'target_center_in_shoulder_frame':target_center.tolist(),
                'discarded_axial_center_source_target':[float(source_center[1]),float(target_center[1])],
                'projection_policy':'Keep positive diagonal radial components only; discard shear and axial coupling. Keep shoulder own measured bone longitudinal map.',
                **{k:v.tolist() for k,v in endpoint[-1].items()}})
        specs[name]={'bridge':endpoint}
        measurements.append({'source_group':name,'mode':'positive_adjacent_domain_bridge',
                             'endpoints':endpoint_proofs})
    maps={m['source_group']:m for m in mapping};bones={b['name']:b for b in source['rig']['bones']}
    groups={g['index']:g['name'] for g in source['garment']['groups']}
    deform={n for n,b in bones.items() if b['deform']}
    offsets=source_arrays['garment_raw_offsets'];ids=source_arrays['garment_raw_group_indices']
    weights=source_arrays['garment_raw_weight_bits'].view('<f4')
    basis_points=source_arrays['garment_keys_world'][0].astype(np.float64)
    matrices=np.zeros((len(basis_points),4,4),dtype=np.float64);positive=set()
    prepared={}
    for name,spec in specs.items():
        m=maps[name];sb,sl=frame(m['source_head'],m['source_tail']);tb,tl=frame(m['target_head'],m['target_tail'])
        prepared[name]=(m,spec,sb,sl,tb,tl)
    for vertex,point in enumerate(basis_points):
        total=0.
        for j in range(int(offsets[vertex]),int(offsets[vertex+1])):
            name=groups[int(ids[j])];weight=float(weights[j])
            if name not in deform or weight<=0.:continue
            positive.add(name)
            require(name in prepared,'ANATOMICAL_UNMAPPED_POSITIVE_DONOR',{'vertex':vertex,'group':name})
            m,spec,sb,sl,tb,tl=prepared[name]
            if 'bridge' in spec:
                axial=float(((point-np.asarray(m['source_head']))@sb)[1]/sl)
                factor=max(0.,min(1.,axial));left,right=spec['bridge']
                current={key:left[key]*(1.-factor)+right[key]*factor for key in left}
            else:current=spec
            scale=current['scale'];sc=current['source_center'];tc=current['target_center']
            require(bool((scale>0).all()),'ANATOMICAL_POSITIVE_RADIAL_SCALE',name)
            linear=tb@np.diag((scale[0],tl/sl,scale[1]))@sb.T
            radial_offset=tb@np.asarray((tc[0]-scale[0]*sc[0],0.,tc[1]-scale[1]*sc[1]))
            matrix=np.eye(4);matrix[:3,:3]=linear
            matrix[:3,3]=np.asarray(m['target_head'])-linear@np.asarray(m['source_head'])+radial_offset
            matrices[vertex]+=matrix*weight;total+=weight
        require(total>0.,'ANATOMICAL_UNWEIGHTED_DONOR',vertex)
        matrices[vertex]/=total
    keys=source_arrays['garment_keys_world'].astype(np.float64)
    h=np.concatenate((keys,np.ones((*keys.shape[:2],1))),axis=2)
    fitted=np.einsum('vij,kvj->kvi',matrices,h)[:,:,:3].astype('<f4')
    det=np.linalg.det(matrices[:,:3,:3])
    require(bool(np.isfinite(fitted).all()) and bool((det>1e-12).all()),'ANATOMICAL_NONPOSITIVE_BLENDED_AFFINE',det.tolist())
    return {'vertex_affine_world':matrices,'fitted_keys_world':fitted},{
        'schema':'hrig.annotated-anatomical-fit.v2','center_definition':'Arithmetic mean of fixed complete-loop radial bounding-box midpoints',
        'center_basis':'Matches the same two-sided geometric span used for each positive scale; independent of rig weight distributions',
        'measurements':deepcopy(measurements),'positive_donor_deform_groups':sorted(positive),
        'neck_zero_positive_donor_influence':'DEF-Neck' not in positive,
        'key_count':len(fitted),'vertex_count':len(basis_points),'minimum_blended_affine_determinant':float(det.min()),
        'maximum_blended_affine_determinant':float(det.max()),'fitted_basis_bounds':[fitted[0].min(0).tolist(),fitted[0].max(0).tolist()],
        'same_Basis_defined_affine_transports_every_Key':True,'body_or_rig_edited':False,
        'qualified_fit':False,'qualified_D4':False,
        'qualification':'Explicit assisted anatomical domain fit study; native, discrete intersections, visual fit, weights, drivers, pose and cold still required'}
