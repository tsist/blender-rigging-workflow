"""Pure skeletal affine garment fit study from explicit rest-segment mapping.

The same per-vertex affine map transports Basis and every Key. No target body,
UV, topology, masks, drivers or bindings are edited by this pure module. A fit
field is a candidate for actual geometric/pose review, never fit certification.
"""
import numpy as np

from .contract import require


def frame(head,tail):
    head=np.asarray(head,dtype=np.float64);tail=np.asarray(tail,dtype=np.float64)
    direction=tail-head
    length=float(np.linalg.norm(direction))
    require(np.isfinite(length) and length>1e-12,'FIT_ZERO_SEGMENT',{'head':head.tolist(),'tail':tail.tolist()})
    y=direction/length
    reference=np.asarray((1.,0.,0.) if abs(y[2])>.9 else (0.,0.,1.))
    x=reference-np.dot(reference,y)*y
    x/=np.linalg.norm(x)
    z=np.cross(x,y)
    return np.stack((x,y,z),axis=1),length


def group_mass(record,arrays,names):
    indices={g['name']:g['index'] for g in record['groups']}
    require(set(names)<=set(indices),'FIT_BODY_GROUP_MISSING',sorted(set(names)-set(indices)))
    selected={indices[n] for n in names}
    offsets=arrays['body_raw_offsets'];groups=arrays['body_raw_group_indices']
    weights=arrays['body_raw_weight_bits'].view('<f4')
    result=np.zeros(len(offsets)-1,dtype=np.float64)
    for vertex in range(len(result)):
        for i in range(int(offsets[vertex]),int(offsets[vertex+1])):
            if int(groups[i]) in selected:
                value=float(weights[i])
                require(np.isfinite(value) and 0.<=value<=1.,'FIT_BODY_WEIGHT_DOMAIN',{'vertex':vertex,'value':value})
                result[vertex]+=value
    require(float(result.sum())>0.,'FIT_EMPTY_BODY_WEIGHT_REGION',names)
    return result


def radial_rms(points,weights,basis,head):
    local=(np.asarray(points,dtype=np.float64)-np.asarray(head,dtype=np.float64))@basis
    values=np.sqrt(np.sum(local[:,(0,2)]**2*weights[:,None],axis=0)/weights.sum())
    require(bool(np.isfinite(values).all()) and bool((values>1e-12).all()),'FIT_EMPTY_RADIAL_REGION',values.tolist())
    return values


def build_fit(source,target,source_arrays,target_arrays,mapping):
    """mapping entries: source_group, target_groups, target_start/end bone names.

    Source segment uses the source_group's actual deform bone. Target segment
    spans target_start.head -> target_end.tail. Width/depth are measured from
    the explicitly paired body weight regions, without tunable scale factors.
    """
    require(type(mapping) is list and bool(mapping),'FIT_MAPPING_REQUIRED',mapping)
    source_bones={b['name']:b for b in source['rig']['bones']}
    target_bones={b['name']:b for b in target['rig']['bones']}
    transforms={};metrics=[]
    for item in mapping:
        require(type(item) is dict and set(item)=={'source_group','target_groups','target_start','target_end'},'FIT_MAPPING_SCHEMA',item)
        name=item['source_group']
        require(name in source_bones and source_bones[name]['deform'] and name not in transforms,'FIT_SOURCE_BONE',name)
        require(type(item['target_groups']) is list and bool(item['target_groups'])
                and all(n in target_bones and target_bones[n]['deform'] for n in item['target_groups']),
                'FIT_TARGET_BONES',item)
        require(item['target_start'] in item['target_groups'] and item['target_end'] in item['target_groups'],
                'FIT_TARGET_SEGMENT_ENDPOINTS',item)
        source_bone=source_bones[name]
        source_head,source_tail=source_bone['head_world'],source_bone['tail_world']
        target_head=target_bones[item['target_start']]['head_world']
        target_tail=target_bones[item['target_end']]['tail_world']
        source_basis,source_length=frame(source_head,source_tail)
        target_basis,target_length=frame(target_head,target_tail)
        source_mass=group_mass(source['body'],source_arrays,[name])
        target_mass=group_mass(target['body'],target_arrays,item['target_groups'])
        source_radii=radial_rms(source_arrays['body_base_world'],source_mass,source_basis,source_head)
        target_radii=radial_rms(target_arrays['body_base_world'],target_mass,target_basis,target_head)
        scale=(float(target_radii[0]/source_radii[0]),float(target_length/source_length),float(target_radii[1]/source_radii[1]))
        linear=target_basis@np.diag(scale)@source_basis.T
        matrix=np.eye(4,dtype=np.float64)
        matrix[:3,:3]=linear
        matrix[:3,3]=np.asarray(target_head)-linear@np.asarray(source_head)
        require(bool(np.isfinite(matrix).all()) and abs(float(np.linalg.det(linear)))>1e-12,'FIT_SINGULAR_TRANSFORM',name)
        transforms[name]=matrix
        metrics.append({**item,'source_head':source_head,'source_tail':source_tail,'target_head':target_head,'target_tail':target_tail,
                        'source_radial_rms':source_radii.tolist(),'target_radial_rms':target_radii.tolist(),
                        'scale_radial_axial_radial':list(scale),'matrix_world':matrix.tolist()})
    groups={g['index']:g['name'] for g in source['garment']['groups']}
    deform={n for n,b in source_bones.items() if b['deform']}
    offsets=source_arrays['garment_raw_offsets'];indices=source_arrays['garment_raw_group_indices']
    weights=source_arrays['garment_raw_weight_bits'].view('<f4')
    matrices=np.zeros((len(offsets)-1,4,4),dtype=np.float64)
    for vertex in range(len(matrices)):
        total=0.
        for i in range(int(offsets[vertex]),int(offsets[vertex+1])):
            name=groups[int(indices[i])];weight=float(weights[i])
            require(np.isfinite(weight) and 0.<=weight<=1.,'FIT_GARMENT_WEIGHT_DOMAIN',{'vertex':vertex,'group':name})
            if name in deform and weight>0.:
                require(name in transforms,'FIT_UNMAPPED_GARMENT_INFLUENCE',{'vertex':vertex,'group':name})
                matrices[vertex]+=transforms[name]*weight
                total+=weight
        require(total>0.,'FIT_UNWEIGHTED_GARMENT_VERTEX',vertex)
        matrices[vertex]/=total
    keys=source_arrays['garment_keys_world'].astype(np.float64)
    homogeneous=np.concatenate((keys,np.ones((*keys.shape[:2],1))),axis=2)
    fitted=np.einsum('vij,kvj->kvi',matrices,homogeneous)[:,:,:3]
    require(bool(np.isfinite(fitted).all()),'FIT_NONFINITE_KEYS',fitted.shape)
    return {'vertex_affine_world':matrices,'fitted_keys_world':np.asarray(fitted,dtype='<f4')}, {
        'schema':'hrig.garment-skeletal-fit-study.v1','method':'Explicit rest segments and weighted body radial RMS; donor garment skin blend',
        'mapping':metrics,'key_count':len(fitted),'vertex_count':fitted.shape[1],
        'fitted_basis_bounds':[np.min(fitted[0],axis=0).tolist(),np.max(fitted[0],axis=0).tolist()],
        'same_vertex_affine_for_all_keys':True,'target_body_edited':False,
        'qualification':'Pure fit candidate; Blender native application, fit, binding, drivers, transfer, deformation and visual review not yet verified'}
