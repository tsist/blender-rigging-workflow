"""Exact original-driver destinations for explicitly declared native consumers.

This catalogs existing RNA; it never writes outputs or evaluates expressions.
The source witness comparison is a bounded change witness, not an emulation of
Blender's transform-variable scalar evaluation or a Python security boundary.
"""
import json, math
import bpy, numpy as np
import character_state as C
from .contract import require


def source_descriptor(fc, rigs, keys):
    if fc.mute or not fc.is_valid or not fc.driver.is_valid or fc.driver.use_self or not fc.driver.variables: return None
    sources = []
    for variable in fc.driver.variables:
        if variable.type not in ('TRANSFORMS', 'LOC_DIFF', 'ROTATION_DIFF', 'SINGLE_PROP'): return None
        for target in variable.targets:
            if target.id not in rigs and target.id not in keys: return None
            record = {'type':variable.type, 'variable':variable.name, 'rig':target.id.name, 'id_type':'Key' if target.id in keys else 'Object', 'bone':target.bone_target,
                      'path':target.data_path, 'transform_type':target.transform_type, 'transform_space':target.transform_space, 'rotation_mode':target.rotation_mode}
            if variable.type == 'SINGLE_PROP':
                if target.id in keys:
                    if target.data_path not in {block.path_from_id('value') for block in target.id.key_blocks}: return None
                elif not target.data_path.startswith('pose.bones['): return None
                try: value = target.id.path_resolve(target.data_path)
                except (ValueError, KeyError): return None
                if type(value) not in (int, float, bool) or not math.isfinite(value): return None
            elif target.id not in rigs or target.bone_target not in target.id.pose.bones: return None
            sources.append(record)
    return sources


def source_values(sources):
    dg = bpy.context.evaluated_depsgraph_get(); answer = []
    for source in sources:
        rig = bpy.data.shape_keys[source['rig']] if source['id_type']=='Key' else bpy.data.objects[source['rig']]
        if source['type'] == 'SINGLE_PROP': answer.append(rig.path_resolve(source['path']))
        else:
            evaluated = rig.evaluated_get(dg); pb = evaluated.pose.bones[source['bone']]
            # Store both final world response and local authored basis. Neither
            # is presented as the driver's exact scalar transform variable.
            matrices = [evaluated.matrix_world @ pb.matrix, pb.matrix_basis]
            answer.append([np.asarray(matrix,dtype='<f4').tolist() for matrix in matrices])
    return answer


def declare(before, rigs, keys):
    outputs = []
    def block_index(kind, owner):
        matches = [i for i, row in enumerate(before['blocks']) if row['type']==kind and row['name']==owner.name and row['library'] is None and
                   (kind!='Key' or row.get('owner',{}).get('name')==C.key_owner(owner).name)]
        if len(matches)!=1: return None
        return matches[0]
    def add(owner, target, field, index, fc, paths, kind):
        sources = source_descriptor(fc, rigs, keys)
        if sources is None or not paths: return
        value = getattr(target,field) if field is not None else target[fc.data_path.rsplit('[',1)[-1][1:-2]]
        if index is not None: value = value[index]
        if type(value) not in (int,float,bool) or not math.isfinite(value): return
        outputs.append({'owner':owner,'target':target,'field':field,'index':index,'fc':fc,'paths':paths,'sources':sources,'source_before':source_values(sources),
            'metadata':{'kind':kind,'owner':owner.name,'path':fc.data_path,'index':fc.array_index,'value_before':value,'sources':sources,
                        'driver_type':fc.driver.type,'expression':fc.driver.expression,'use_self':fc.driver.use_self,
                        'source_comparison':'whole-bone world+basis witness or original scalar; not driver scalar emulation'}})
    for key in keys:
        i = block_index('Key',key)
        if i is None: continue
        destinations = {item.path_from_id('value'):(j,item) for j,item in enumerate(key.key_blocks)}
        for fc in key.animation_data.drivers if key.animation_data else []:
            if fc.data_path in destinations and fc.array_index==0:
                j,item = destinations[fc.data_path]
                add(key,item,'value',None,fc,[('blocks',i,'character_extra','blocks',j,'settings','value')],'key_value')
    for rig in rigs:
        i=block_index('Object',rig)
        if i is None: continue
        destinations={}
        block=before['blocks'][i]
        for j,pb in enumerate(rig.pose.bones):
            # Native inspector pose order is not assumed to match RNA order.
            matches=[n for n,row in enumerate(block['pose']) if row['name']==pb.name]
            require(len(matches)==1,'SNAPSHOT_SCHEMA',pb.name); j=matches[0]
            root=('blocks',i,'pose',j); extra=('blocks',i,'character_extra','pose_custom',pb.name)
            matrices=[root+('matrix_basis',),root+('settings','matrix_basis'),extra+('matrix_basis',)]
            for channel in ('location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale'):
                for index in range(len(getattr(pb,channel))):
                    paths=[extra+(channel,index)]
                    if channel in block['pose'][j].get('settings',{}): paths.append(root+('settings',channel,index))
                    destinations[(pb.path_from_id(channel),index)]=(pb,channel,index,paths+matrices,'pose_component')
            # Existing scalar PoseBone drivers (e.g. B-Bone ease parameters)
            # are original native destinations too. Register by actual RNA
            # type/path; no bone names or one-off failure paths are admitted.
            for prop in pb.bl_rna.properties:
                if prop.type not in ('FLOAT','BOOLEAN','INT') or prop.is_array or prop.is_readonly: continue
                field=prop.identifier
                paths=[extra+(field,)]
                if field in block['pose'][j].get('settings',{}): paths.append(root+('settings',field))
                destinations[(pb.path_from_id(field),0)]=(pb,field,None,paths,'pose_scalar')
            for prop in pb.keys():
                if type(pb[prop]) not in (int,float,bool): continue
                path=pb.path_from_id()+'['+json.dumps(prop,ensure_ascii=False)+']'
                destinations[(path,0)]=(pb,('idprop',prop),None,[extra+('custom_properties',prop)],'pose_idproperty')
            for k,con in enumerate(pb.constraints):
                for prop in con.bl_rna.properties:
                    if prop.type not in ('FLOAT','BOOLEAN','INT') or prop.is_array or prop.is_readonly: continue
                    field=prop.identifier
                    if field not in block['pose'][j]['constraints'][k]: continue
                    destinations[(con.path_from_id(field),0)]=(con,field,None,[root+('constraints',k,field)],'constraint_scalar')
                for n,target in enumerate(getattr(con,'targets',[])):
                    if not hasattr(target,'weight'): continue
                    rows=block['character_extra']['pose_constraint_targets'].get(pb.name,[])
                    positions=[q for q,row in enumerate(rows) if row['index']==k and row['name']==con.name]
                    require(len(positions)==1,'SNAPSHOT_SCHEMA',con.name)
                    prefix=('blocks',i,'character_extra','pose_constraint_targets',pb.name,positions[0],'targets',n)
                    destinations[(target.path_from_id('weight'),0)]=(target,'weight',None,[prefix+('settings','weight'),prefix+('bone_target','weight')],'constraint_target_weight')
        for fc in rig.animation_data.drivers if rig.animation_data else []:
            destination=destinations.get((fc.data_path,fc.array_index))
            if destination is None: continue
            target,field,index,paths,kind=destination
            if isinstance(field,tuple):
                sources=source_descriptor(fc,rigs,keys)
                if sources is None: continue
                prop=field[1]
                outputs.append({'owner':rig,'target':target,'property':prop,'field':None,'index':None,'fc':fc,'paths':paths,'sources':sources,'source_before':source_values(sources),
                    'metadata':{'kind':kind,'owner':rig.name,'path':fc.data_path,'index':fc.array_index,'value_before':target[prop],'sources':sources,
                        'driver_type':fc.driver.type,'expression':fc.driver.expression,'use_self':fc.driver.use_self}})
            else: add(rig,target,field,index,fc,paths,kind)
        armature=rig.data; i=block_index('Armature',armature)
        if i is None: continue
        destinations={item.path_from_id('is_visible'):item for item in armature.collections_all}
        for fc in armature.animation_data.drivers if armature.animation_data else []:
            collection=destinations.get(fc.data_path)
            if collection is None or fc.array_index!=0: continue
            rows=before['blocks'][i]['character_extra']['bone_collections']; j=next(n for n,row in enumerate(rows) if row['name']==collection.name)
            paths=[('blocks',i,'character_extra','bone_collections',j,'settings','is_visible')]
            def chain(item):
                names=[]
                while item is not None: names.append(item.name); item=item.parent
                return names
            for item in armature.collections_all:
                if collection.name in chain(item.parent): paths.append(('blocks',i,'character_extra','collection_system','layout',item.name,'ancestors_visible'))
                if collection.name in chain(item): paths.append(('blocks',i,'character_extra','collection_system','layout',item.name,'effectively_visible'))
            for bone in armature.bones:
                if any(collection.name in chain(item) for item in bone.collections): paths.append(('blocks',i,'character_extra','collection_system','bone_visibility',bone.name,'collection_effective_any'))
            add(armature,collection,'is_visible',None,fc,paths,'collection_visible')
    return outputs


def value(output):
    if output.get('property') is not None: return output['target'][output['property']]
    result=getattr(output['target'],output['field'])
    return result if output['index'] is None else result[output['index']]
