"""Derive a new bounded control identity only from a verified edit receipt.

This is a trusted local composition function, not an authentication boundary.
Candidate/cold verification must independently check the state and source.
"""
from copy import deepcopy
from pathlib import Path
import struct
from native_controls.contract import validate
from .contract import require,digest,validate_request,operation,plan_tail,plan_head,f32

def derive(profile,evidence,candidate):
    before=validate(profile)
    require(type(candidate) is dict and set(candidate)=={'file','expected_sha256'},'CANDIDATE_IDENTITY')
    require(type(candidate['file']) is str and Path(candidate['file']).is_absolute(),'CANDIDATE_PATH')
    sha=candidate['expected_sha256'];require(type(sha) is str and len(sha)==64 and all(c in '0123456789abcdef' for c in sha),'CANDIDATE_SHA')
    require(type(evidence) is dict and evidence.get('schema') in ('hrig.rest-edit-binding-evidence.v1','hrig.rest-edit-binding-evidence.v3','hrig.rest-edit-binding-evidence.v4','hrig.rest-edit-binding-evidence.v5'),'BINDING_EVIDENCE')
    e=deepcopy(evidence);request=validate_request(e['request'])
    op=operation(request)
    require(e['schema']=={'roll':'hrig.rest-edit-binding-evidence.v1','tail':'hrig.rest-edit-binding-evidence.v3','head':'hrig.rest-edit-binding-evidence.v4','parent':'hrig.rest-edit-binding-evidence.v5'}[op],'EVIDENCE_OPERATION_VERSION')
    if op=='parent':
        from .parent_contract import validate_binding
        validate_binding(e,request)
    if op=='head':
        plan=e['plan'];guard=plan['protected']
        snapshot={'schema':'hrig.rest-head-snapshot.v1','bone':request['bone'],'head':plan['raw_before'],'tail':guard['tail'],
            'raw_roll':guard['raw_roll'],'parent':guard['parent'],'use_connect':guard['use_connect']}
        require(type(e.get('raw_snapshot')) is dict and e['raw_snapshot']==snapshot
            and digest(e['raw_snapshot'])==plan['snapshot_sha256'],'HEAD_RAW_SNAPSHOT_BINDING')
        expected=plan_head(snapshot,request)
        require(plan==expected and digest(plan)==digest(expected),'HEAD_PLAN_BINDING')
        protection=e.get('head_protection',{})
        require(type(protection) is dict and set(protection)=={'schema','endpoint_policy','topology','reference_target_head_tail_local',
            'raw_target_length_after','reference_target_length','source_lengths_sha256','reference_lengths_sha256','authored_write','derived_fields'}
            and protection.get('schema')=='hrig.rest-head-protection.v1','HEAD_PROTECTION_REQUIRED')
        require(protection.get('endpoint_policy')=='qualified_bit_exact_raw_endpoints','HEAD_ENDPOINT_POLICY')
        endpoints=protection.get('reference_target_head_tail_local')
        require(type(endpoints) is list and len(endpoints)==6 and struct.pack('<6f',*endpoints)==struct.pack('<6f',*(plan['raw_after']+guard['tail'])),'HEAD_FINAL_ENDPOINT_BINDING')
        import math
        for key in ('raw_target_length_after','reference_target_length'):
            require(type(protection.get(key)) in (int,float) and math.isfinite(protection[key]) and protection[key]>0 and f32(protection[key])==protection[key],'HEAD_NATIVE_LENGTH_EVIDENCE',key)
        for key in ('source_lengths_sha256','reference_lengths_sha256'):
            value=protection.get(key);require(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'HEAD_LENGTHS_HASH',key)
    if op=='tail':
        plan=e['plan'];guard=plan['protected']
        expected=plan_tail({'schema':'hrig.rest-tail-snapshot.v1','bone':request['bone'],'head':guard['head'],'tail':plan['raw_before'],
            'raw_roll':guard['raw_roll'],'parent':guard['parent'],'use_connect':guard['use_connect']},request)
        require(plan==expected and digest(plan)==digest(expected),'TAIL_PLAN_BINDING')
        protection=e.get('tail_protection',{})
        require(protection.get('schema')=='hrig.rest-tail-protection.v2','TAIL_PROTECTION_REQUIRED')
        require(protection.get('endpoint_policy')=='qualified_bit_exact_raw_endpoints','TAIL_ENDPOINT_POLICY')
        endpoints=protection.get('reference_target_head_tail_local')
        require(type(endpoints) is list and len(endpoints)==6 and struct.pack('<6f',*endpoints)==struct.pack('<6f',*(guard['head']+plan['raw_after'])),'TAIL_FINAL_ENDPOINT_BINDING')
        import math
        for key in ('raw_target_length_after','reference_target_length'):
            require(type(protection.get(key)) in (int,float) and math.isfinite(protection[key]) and protection[key]>0 and f32(protection[key])==protection[key],'TAIL_NATIVE_LENGTH_EVIDENCE',key)
        for key in ('source_lengths_sha256','reference_lengths_sha256'):
            value=protection.get(key);require(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'TAIL_LENGTHS_HASH',key)
    require(e['request_sha256']==digest(request),'REQUEST_BINDING')
    require(e['source_profile']['semantic_sha256']==digest(before) and e['source_profile']['profile_id']==before['profile_id'],'SOURCE_PROFILE_BINDING')
    require(e['old_rest_sha256']==before['rig']['rest_sha256']==e['source_profile']['rest_sha256'],'SOURCE_REST_BINDING')
    require(e['rig']['object']==before['rig']['object'] and e['rig']['original_data']==before['rig']['data'],'SOURCE_RIG_BINDING')
    require(type(e['rig']['owned_data']) is str and e['rig']['owned_data'] and e['rig']['owned_data']!=e['rig']['original_data'],'NEW_DATA_IDENTITY')
    require(e['frame']==0 and e['new_rest_sha256']!=e['old_rest_sha256'],'NEW_REST_IDENTITY')
    require(type(e['new_rest_sha256']) is str and len(e['new_rest_sha256'])==64 and all(c in '0123456789abcdef' for c in e['new_rest_sha256']),'NEW_REST_SIGNATURE')
    protected=sorted({r['bone'] for r in before['roles']}|{g['bone'] for r in before['roles'] for g in r['requires']})
    require(e['roles_rest_exact'] is True and e['role_count']==len(before['roles']) and e['role_rest_protected_bones']==protected,'ROLE_REST_NOT_PROVEN')
    require(request['bone'] not in protected and e['target_bones']==[request['bone']],'ROLE_AXIS_REBIND_REQUIRED')
    for field in ('reference_restored_exact','actual_reference_exact','original_data_protected','mirror_flag_restored','scan_admissible'):
        require(e['proof'].get(field) is True,'INCOMPLETE_EDIT_PROOF',field)
    require(e['proof'].get('old_profile_rejection')=='RIG_DATA_IDENTITY' and e['proof'].get('old_bound_profile_rest_rejection')=='REST_BINDING_DRIFT','OLD_PROFILE_REFUSAL_REQUIRED')
    require(Path(candidate['file']).resolve()!=Path(e['source']['file']).resolve(),'CANDIDATE_IS_SOURCE')
    after=deepcopy(before);after['profile_id']=before['profile_id']+{'roll':'.rest097.','tail':'.rest102.tail.','head':'.rest104.head.','parent':'.rest106.parent.'}[op]+digest({'request':request,'data':e['rig']['owned_data']})[:12]
    after['revision']+=1;after['rig']['data']=e['rig']['owned_data'];after['rig']['rest_sha256']=e['new_rest_sha256']
    after=validate(after)
    return {'profile':after,'binding':{'schema':'hrig.explicit-rest-profile-binding.v1','candidate':deepcopy(candidate),
        'source':e['source'],'source_profile_sha256':digest(before),'new_profile_sha256':digest(after),
        'edit_evidence_sha256':digest(e),'changed_profile_fields':['profile_id','revision','rig.data','rig.rest_sha256'],
        'role_definitions_exact':after['roles']==before['roles'],'qualification':'new control identity proposal; independent cold/control proof required; no changed-rest skin/animation equivalence'}}
