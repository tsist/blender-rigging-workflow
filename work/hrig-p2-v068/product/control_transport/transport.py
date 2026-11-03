"""Pure-read profile binding and detached fixed-batch adapter for saved inputs."""
from copy import deepcopy
import bpy
from native_controls.native import AtomicControls, BoundBatch, rest_signature
from native_controls.contract import validate, require


def build_profile(document):
    """Apply a complete pinned typed profile without re-extracting identity."""
    profile=validate(document)
    AtomicControls(profile)
    return profile


def extract_profile(document):
    """Explicit creation from the old three-field seed on a pinned source."""
    require(type(document) is dict and type(document.get('rig')) is dict and set(document['rig'])=={'object','data','witness_bones'},'EXTRACT_REQUIRES_SEED')
    profile = deepcopy(document)
    rig = bpy.data.objects[profile['rig']['object']]
    profile['rig']['rest_sha256'] = rest_signature(rig)
    def reads(owner, targets):
        return bool(owner.animation_data and any(target.id in targets for fc in owner.animation_data.drivers for variable in fc.driver.variables for target in variable.targets))
    consumers = [obj for obj in bpy.data.objects if obj.type == 'ARMATURE' and obj != rig and obj.library is None and obj.data.library is None and (reads(obj,[rig]) or reads(obj.data,[rig]))]
    profile['rig']['consumer_rigs'] = [{'object':obj.name,'data':obj.data.name,'rest_sha256':rest_signature(obj)} for obj in consumers]
    # Include actual mesh key owners attached to the root rig or driven by it.
    # Core catalogs only exact permitted destinations and source links.
    profile['rig']['output_keys'] = [{'object':obj.name,'data':obj.data.name,'key':obj.data.shape_keys.name} for obj in bpy.data.objects
        if obj.type=='MESH' and obj.library is None and obj.data.library is None and obj.data.shape_keys and
        (any(mod.type=='ARMATURE' and mod.object==rig for mod in obj.modifiers) or reads(obj.data.shape_keys,[rig]))]
    return validate(profile)


def load_profile(document,action='apply_typed'):
    require(action in ('apply_typed','extract_seed'),'PROFILE_ACTION')
    return extract_profile(document) if action=='extract_seed' else build_profile(document)


def bind(profile, operations, binding_id='transport-batch'):
    first = next(row for row in profile['roles'] if row['role_id']==operations[0]['role'])
    return BoundBatch({'schema':'hrig.control-batch-binding.v2','binding_id':binding_id,'character_id':profile['character_id'],
        'role_id':operations[0]['role'],'runtime':deepcopy(profile['runtime']),
        'native':{'rig':profile['rig']['object'],'data':profile['rig']['data'],'bone':first['bone']},
        'profile':deepcopy(profile),'operations':deepcopy(operations)})
