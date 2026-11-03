"""File-backed witnesses; this module never changes scene data or frames."""
import struct,hashlib,json
from pathlib import Path
import bpy,numpy as np
import character_state as C
from .contract import operation

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def arrays(rig,body,witness_names,original_data,*,include_lengths=False):
    dg=bpy.context.evaluated_depsgraph_get();er=rig.evaluated_get(dg)
    result={'bones':np.asarray([er.matrix_world@er.pose.bones[n].matrix for n in witness_names],dtype='<f4')}
    obj=bpy.data.objects[body];ev=obj.evaluated_get(dg);mesh=ev.to_mesh()
    try:
        coords=np.empty(len(mesh.vertices)*3,dtype='<f4');mesh.vertices.foreach_get('co',coords)
        world=np.asarray(ev.matrix_world,dtype='<f4');result['skin']=coords.reshape(-1,3)@world[:3,:3].T+world[:3,3]
    finally:ev.to_mesh_clear()
    offsets=[0];groups=[];bits=[]
    for v in obj.data.vertices:
        for g in v.groups:groups.append(g.group);bits.append(struct.unpack('<I',struct.pack('<f',g.weight))[0])
        offsets.append(len(groups))
    result.update(raw_offsets=np.asarray(offsets,dtype='<u8'),raw_group_indices=np.asarray(groups,dtype='<i4'),raw_weight_bits=np.asarray(bits,dtype='<u4'))
    for prefix,data in (('target',rig.data),('original',original_data)):
        bones=sorted(data.bones,key=lambda b:b.name)
        result[prefix+'_rest_names']=np.asarray([b.name for b in bones])
        result[prefix+'_rest_matrix_local']=np.asarray([[list(r) for r in b.matrix_local] for b in bones],dtype='<f4')
        result[prefix+'_rest_matrix_parent_relative']=np.asarray([[list(r) for r in b.matrix] for b in bones],dtype='<f4')
        result[prefix+'_rest_head_tail_local']=np.asarray([[*b.head_local,*b.tail_local] for b in bones],dtype='<f4')
        if include_lengths:result[prefix+'_rest_lengths']=np.asarray([b.length for b in bones],dtype='<f4')
    assert np.isfinite(result['bones']).all() and np.isfinite(result['skin']).all()
    return result
def write_arrays(path,value):
    with Path(path).open('xb') as f:np.savez_compressed(f,**value)
    return {'file':str(path),'expected_sha256':sha(path)}
def topology(data):
    return [{'name':b.name,'parent':b.parent.name if b.parent else None,'use_connect':bool(b.use_connect),
        'use_deform':bool(b.use_deform),'bbone_segments':int(b.bbone_segments)} for b in sorted(data.bones,key=lambda b:b.name)]
def write_topology(path,target,original):
    value={'schema':'hrig.rest-parent-topology-witness.v1','target':topology(target),'original':topology(original),
        'target_order':[b.name for b in target.bones],'original_order':[b.name for b in original.bones]}
    with Path(path).open('x',encoding='utf8') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False,indent=2)
    return {'file':str(path),'expected_sha256':sha(path)}
def export_expectation(session,resources):
    tx=session.tx;tx.validate_applied();J=session.outdir
    saved=session.saved;assert saved is not None
    evidence=tx.binding_evidence()
    op=operation(evidence['request']);endpoint=op in ('tail','head','parent')
    data=arrays(tx.rig,session.body,session.profile['rig']['witness_bones'],tx.original_data,include_lengths=endpoint)
    witness=write_arrays(J/'expected.npz',data)
    value={'schema':{'roll':'hrig.rest-edit-cold-expectation.v1','tail':'hrig.rest-edit-cold-expectation.v3','head':'hrig.rest-edit-cold-expectation.v4','parent':'hrig.rest-edit-cold-expectation.v5'}[op],'candidate':saved['candidate'],'profile':saved['profile'],
        'source_profile':{'file':session.profile_path,'expected_sha256':sha(session.profile_path)},'source':session.source,
        'original_data':tx.original_data.name,'edited_data':tx.rig.data.name,'rig':tx.rig.name,'body':session.body,'frame':0,
        'witness':witness,'author_summary':C.summarize(C.capture()),'resources':resources,
        'control_operations':[{'role':'body.root.z','value':.05}] if endpoint else [{'role':'finger.R.index.curl','value':.02}],'binding_evidence':evidence}
    if op=='parent':
        value['topology_witness']=write_topology(J/'expected-topology.json',tx.rig.data,tx.original_data)
        value['pose_order']=[p.name for p in tx.rig.pose.bones]
    with (J/'expected-summary.json').open('x',encoding='utf8') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False,indent=2)
    return {'file':str(J/'expected-summary.json'),'expected_sha256':sha(J/'expected-summary.json')}
