"""Exact style authorization mask over the declared character observer."""
import copy

LEDGER='hrig_style_instances_v1'


def protected(snapshot,rig_name,armature_name,request):
    result=copy.deepcopy(snapshot)
    objects=[b for b in result['blocks'] if b['type']=='Object' and b['name']==rig_name and b['library'] is None]
    arms=[b for b in result['blocks'] if b['type']=='Armature' and b['name']==armature_name and b['library'] is None]
    if len(objects)!=1 or len(arms)!=1:raise ValueError('Style target observation ambiguous')
    obj,arm=objects[0],arms[0]
    # Only this reserved key is authorized; other instance bytes are checked
    # separately by the native worker. All other metadata remains strict.
    obj['custom_properties'].pop(LEDGER,None)
    for role,info in request['template']['roles'].items():
        bone=request['bindings'][role]
        pb=next(x for x in obj['pose'] if x['name']==bone)
        for field in info['fields']:
            parts=field.split('.')
            if parts[:2]==['pose','color']:
                target=obj['character_extra']['pose_colors'][bone]
                for p in parts[2:-1]:target=target[p]
                target.pop(parts[-1])
            elif parts[:2]==['bone','color']:
                target=arm['character_extra']['bone_settings'][bone]['color']
                for p in parts[2:-1]:target=target[p]
                target.pop(parts[-1])
            elif parts[0]=='pose':
                name=parts[1]
                pb['settings'].pop(name)
                obj['character_extra']['pose_custom'][bone].pop(name)
            else:
                arm['character_extra']['bone_settings'][bone]['settings'].pop(parts[1])
    return result


def ledger_other_instances(raw,instance_id):
    import json
    value=json.loads(raw) if raw is not None else {'schema':'hrig.control-style.instances.v1','instances':{}}
    value['instances'].pop(instance_id,None)
    return value
