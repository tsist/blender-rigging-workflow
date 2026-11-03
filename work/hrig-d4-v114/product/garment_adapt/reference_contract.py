"""Pure strict original-author and explicit new-rig-reader proof.

The observed C7/Snow preparation has eleven pointer fields and native Object
users delta ZERO. Do not infer ID user counts from pointer counts. Every old
user counter remains exact; any different future source requires a new intent.
"""
from copy import deepcopy
from .contract import require, digest


def identity(block):
    return {k:block[k] for k in ('type','name','library','owner') if k in block}


def prove_originals(before,after,scene_name,garment_name,mesh_name,key_name,rig_name):
    require(before['schema']==after['schema'] and before['coverage']==after['coverage'],'CROSS_OBSERVER_CHANGED',None)
    old={digest(identity(b)):b for b in before['blocks']}
    new={digest(identity(b)):b for b in after['blocks']}
    require(set(old)<=set(new),'CROSS_ORIGINAL_ID_MISSING',sorted(set(old)-set(new)))
    rig_id={'type':'Object','name':rig_name,'library':None}
    object_id={'type':'Object','name':garment_name,'library':None}
    key_id={'type':'Key','name':key_name,'library':None,'owner':{'type':'Mesh','name':mesh_name,'library':None}}
    require(digest(object_id) not in old and digest(key_id) not in old,'CROSS_NEW_ID_COLLISION',None)
    require(digest(object_id) in new and digest(key_id) in new,'CROSS_NEW_ID_MISSING',None)
    rig_before=old[digest(rig_id)]['native_user_accounting']['users']
    rig_after=new[digest(rig_id)]['native_user_accounting']['users']
    require(rig_after==rig_before,'CROSS_RIG_NATIVE_USERS_CHANGED',{'before':rig_before,'after':rig_after,'expected_delta':0})
    # No counter is removed or normalized, including the target rig's counter.
    for key,original in old.items():
        current=deepcopy(new[key])
        if original['type']=='Scene' and original['name']==scene_name and original['library'] is None:
            members=current['character_extra']['root_members']['objects']
            require(members.count(object_id)==1,'CROSS_ROOT_MEMBER',object_id)
            members.remove(object_id)
            for layer in current['character_extra']['view_layers']:
                require(layer['base_hidden'].pop(garment_name,None) is False,'CROSS_NEW_BASE_VISIBILITY',layer['name'])
        require(current==original,'CROSS_ORIGINAL_AUTHOR_CHANGED',identity(original))
    old_embedded={(digest(e['owner']),digest(identity(e['record']))):e for e in before['embedded']}
    new_embedded={(digest(e['owner']),digest(identity(e['record']))):e for e in after['embedded']}
    require(all(new_embedded.get(k)==v for k,v in old_embedded.items()),'CROSS_ORIGINAL_EMBEDDED_CHANGED',None)
    old_ids=set(old)
    original_edges=[e for e in after['edges'] if digest(e['user']) in old_ids and digest(e['target']) in old_ids]
    require(original_edges==before['edges'],'CROSS_ORIGINAL_GRAPH_CHANGED',None)
    obj=new[digest(object_id)]['object']
    require(obj['parent']==rig_id,'CROSS_PARENT_READER',obj['parent'])
    modifiers=[m for m in obj['modifiers'] if m.get('object')==rig_id]
    require(len(modifiers)==1 and modifiers[0].get('use_vertex_groups') is True
            and 'use_deform_preserve_volume' in modifiers[0],'CROSS_ARMATURE_READER',modifiers)
    readers=[{'owner':object_id,'field':'parent','target':rig_id},
             {'owner':object_id,'field':'modifiers['+modifiers[0]['name']+'].object','target':rig_id}]
    key_drivers=new[digest(key_id)]['animation']['drivers']
    require(len(key_drivers)==9,'CROSS_KEY_DRIVER_READER_COUNT',len(key_drivers))
    for curve in key_drivers:
        require(len(curve['variables'])==1,'CROSS_DRIVER_VARIABLE_COUNT',curve['path'])
        variable=curve['variables'][0]
        require(variable['name']=='var' and variable['type']=='TRANSFORMS' and len(variable['targets'])==1,
                'CROSS_DRIVER_VARIABLE_TYPE',curve['path'])
        target=variable['targets'][0]
        require(target['id']==rig_id and target['transform_space']=='LOCAL_SPACE','CROSS_RIG_READER_ID',curve['path'])
        readers.append({'owner':key_id,'field':'drivers['+curve['path']+'].variables[var].targets[0].id',
                        'target':rig_id,'bone':target['bone_target'],'axis':target['transform_type'],
                        'rotation_mode':target['rotation_mode'],'space':target['transform_space'],'expression':curve['expression']})
    actual_new_owners={digest(e['user']) for e in after['edges'] if e['target']==rig_id and digest(e['user']) not in old_ids}
    require(actual_new_owners=={digest(object_id),digest(key_id)},'CROSS_UNDECLARED_RIG_READER_OWNER',sorted(actual_new_owners))
    return {'schema':'hrig.foreign-garment-strict-original-proof.v2',
        'all_original_authors_and_all_user_counters_exact':True,'only_original_author_change':'Declared root garment member/base entry',
        'original_graph_and_embedded_authors_exact':True,'new_direct_reader_fields':readers,
        'new_direct_reader_field_count':len(readers),'new_reader_owner_count':len(actual_new_owners),
        'rig_reference_accounting':{'before':rig_before,'after':rig_after,'expected_delta':0,'actual_delta':rig_after-rig_before,
            'rule':'Observed same-source contract; pointer count is not native users count; no normalization'},
        'scope':'Matching C7/Snow native preparation only; no general Blender ID user-count rule'}
