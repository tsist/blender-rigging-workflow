"""Read-only explicit coordinate approvals; never discover/adopt Key drift."""
import hashlib,json
from pathlib import Path
from .control_core.contract import require

IDENTITY=('object','mesh','key_id','reference','target')

def load_approvals(binding,catalog):
    owners={(o['object'],o['data'],o['key']):o for o in catalog['face_owners']}
    answer=[];seen=set()
    for descriptor in binding['approved_key_edits']:
        require(type(descriptor) is dict and set(descriptor)=={'file','expected_sha256'}
                and Path(descriptor['file']).is_absolute(),'APPROVED_KEY_DESCRIPTOR')
        raw=Path(descriptor['file']).read_bytes()
        require(hashlib.sha256(raw).hexdigest()==descriptor['expected_sha256'],'APPROVED_KEY_RECEIPT_SHA')
        doc=json.loads(raw)
        if doc.get('schema')=='hrig.face-key-edit-chain.v1':
            first,final=doc['append'],doc['edit'];mode='append'
            require(first['operation']=='append' and final['operation']=='edit'
                    and first['target_before_sha256'] is None
                    and final['target_before_sha256']==first['target_sha256']
                    and [first[x] for x in IDENTITY]==[final[x] for x in IDENTITY]
                    and first['region_vertices']==final['region_vertices']
                    and first['outside_region_sha256']==final['outside_region_sha256']
                    and doc.get('original_keys_preserved') is True
                    and doc.get('outside_new_target_equals_reference_f32') is True,'APPROVED_KEY_CHAIN')
        else:
            require(doc.get('schema')=='hrig.face-coordinate-receipt.v1' and doc.get('operation')=='edit','APPROVED_KEY_RECEIPT')
            first=final=doc;mode='edit'
        for receipt in (first,final):
            require(receipt['schema']=='hrig.face-coordinate-receipt.v1'
                    and receipt.get('non_region_stored_f32_exact') is True
                    and receipt.get('full_C_expected_forward_exact') is True
                    and receipt.get('context_and_full_profile_witness_exact') is True,'APPROVED_KEY_NATIVE_QUALIFICATION')
        owner=owners.get((final['object'],final['mesh'],final['key_id']))
        require(owner is not None and final['reference']==owner['reference'] and final['target']!=owner['reference'],'APPROVED_KEY_OWNER')
        originals={b['name']:b for b in owner['keys']};reference=originals[owner['reference']]
        require(final['raw_mesh_sha256']==owner['raw_coordinate_sha256_le_f32']
                and final['reference_sha256']==reference['coordinate_sha256_le_f32'],'APPROVED_KEY_SOURCE_COORDINATES')
        target=final['target'];identity=(owner['data'],target)
        require(identity not in seen,'APPROVED_KEY_DUPLICATE');seen.add(identity)
        require((target not in originals) if mode=='append' else
                (target in originals and final['target_before_sha256']==originals[target]['coordinate_sha256_le_f32']),
                'APPROVED_KEY_TARGET_LINEAGE')
        region=final['region_vertices']
        require(type(region) is list and 1<=len(region)<=8192 and region==sorted(set(region))
                and all(type(x) is int and 0<=x<owner['vertex_count'] for x in region),'APPROVED_KEY_REGION')
        definition=final['target_definition']
        require(definition['name']==target and definition['point_count']==owner['vertex_count']
                and definition['relative_key']==(owner['reference'] if mode=='append' else originals[target]['relative_key']),
                'APPROVED_KEY_DEFINITION')
        answer.append({'owner':owner,'target':target,'mode':mode,'receipt':final,'descriptor':descriptor})
    return answer
