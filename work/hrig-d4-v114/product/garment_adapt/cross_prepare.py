"""Owned foreign-garment geometry/driver/binding preparation on C7.

Only appended IDs are edited. Existing body/Keys/rig authors are protected;
the declared new scene member and rig reference accounting are recorded. Full
original author/context/body restoration follows fenced removal of owned IDs.
The result is a native FIT STUDY, not a skinned/quality-qualified final garment.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import uuid

import bpy
import numpy as np
from mathutils import Matrix
import character_state as C

from .contract import require, GarmentError
from .native import error_record


def id_key(block):
    return C.digest({k:block[k] for k in ('type','name','library','owner') if k in block})


def local_ids():
    result=set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type=='COLLECTION':
            for item in getattr(bpy.data,prop.identifier,()):
                if (isinstance(item,bpy.types.ID) and not item.library and not getattr(item,'is_embedded_data',False)
                        and item.bl_rna.identifier not in {'Screen','WorkSpace','WindowManager'}):
                    result.add(item)
    return result


def context():
    layer=bpy.context.view_layer
    return {'scene':bpy.context.scene.name,'layer':layer.name,'mode':bpy.context.mode,
            'frame':bpy.context.scene.frame_current,'subframe':bpy.context.scene.frame_subframe,
            'active':layer.objects.active.name if layer.objects.active else None,
            'selection':{obj.name:bool(obj.select_get()) for obj in layer.objects}}


def skin(obj):
    evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());mesh=evaluated.to_mesh()
    try:
        co=np.empty(len(mesh.vertices)*3,dtype='<f4');mesh.vertices.foreach_get('co',co)
        matrix=np.asarray(evaluated.matrix_world,dtype='<f4')
        value=co.reshape(-1,3)@matrix[:3,:3].T+matrix[:3,3]
    finally:
        evaluated.to_mesh_clear()
    require(bool(np.isfinite(value).all()),'CROSS_BODY_NONFINITE',obj.name)
    return value


def bone_output(rig):
    evaluated=rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    values=np.asarray([evaluated.matrix_world@b.matrix for b in evaluated.pose.bones],dtype='<f4')
    require(bool(np.isfinite(values).all()),'CROSS_BONE_NONFINITE',rig.name)
    return values


class CrossGarmentPreparation:
    def __init__(self,source_identity,donor_identity,body_name,rig_name,new_name):
        self.source=deepcopy(source_identity);self.donor=deepcopy(donor_identity)
        self.body=bpy.data.objects.get(body_name);self.rig=bpy.data.objects.get(rig_name)
        require(self.body and self.body.type=='MESH' and self.rig and self.rig.type=='ARMATURE','CROSS_TARGET_BINDING',body_name)
        require(type(new_name) is str and new_name.startswith('D4_') and new_name not in bpy.data.objects,'CROSS_NEW_NAME',new_name)
        self.new_name=new_name;self.garment=None;self._pending=None
        self._guard()
        bpy.context.view_layer.update()
        original_skin=skin(self.body)
        original_bones=bone_output(self.rig)
        self.before={'author':C.capture(),'context':context(),'body_skin':original_skin,'bones':original_bones}
        self.original_ids=local_ids();self.owned=set()

    @property
    def pending(self):
        return deepcopy({k:self._pending[k] for k in ('token','phase')}) if self._pending else None

    def _guard(self):
        require(bpy.context.mode=='OBJECT','CROSS_OBJECT_MODE',bpy.context.mode)
        require(Path(bpy.data.filepath).resolve()==Path(self.source['file']).resolve(),'CROSS_SOURCE_MAIN',bpy.data.filepath)
        for descriptor in (self.source,self.donor):
            require(hashlib.sha256(Path(descriptor['file']).read_bytes()).hexdigest()==descriptor['expected_sha256'],
                    'CROSS_SOURCE_SHA',descriptor['file'])

    def _collect(self):
        bpy.context.view_layer.update()
        points=skin(self.body)
        bones=bone_output(self.rig)
        return {'author':C.capture(),'context':context(),'body_skin':points,'bones':bones}

    def _same(self,actual,expected,code):
        checks={'author':actual['author']==expected['author'],'context':actual['context']==expected['context'],
                'body_skin':actual['body_skin'].dtype==expected['body_skin'].dtype
                    and actual['body_skin'].shape==expected['body_skin'].shape
                    and actual['body_skin'].tobytes()==expected['body_skin'].tobytes(),
                'bones':actual['bones'].dtype==expected['bones'].dtype and actual['bones'].shape==expected['bones'].shape
                    and actual['bones'].tobytes()==expected['bones'].tobytes()}
        require(all(checks.values()),code,checks)

    def _existing_authors(self,after):
        """Only root member/base entry and target-rig reference count may grow."""
        before=self.before['author'];actual=deepcopy(after)
        old_map={id_key(block):block for block in before['blocks']}
        new_map={id_key(block):block for block in actual['blocks']}
        require(set(old_map)<=set(new_map),'CROSS_ORIGINAL_ID_MISSING',sorted(set(old_map)-set(new_map)))
        member={'type':'Object','name':self.garment.name,'library':None}
        accounting={}
        for key,old in old_map.items():
            now=new_map[key]
            if old['type']=='Object' and old['name']==self.rig.name and old['library'] is None:
                accounting={'before':old['native_user_accounting']['users'],'after':now['native_user_accounting']['users'],
                            'reason':'Owned garment parent/Armature and nine explicit Key driver target references'}
                now['native_user_accounting']['users']=old['native_user_accounting']['users']
            if old['type']=='Scene' and old['name']==self.before['context']['scene'] and old['library'] is None:
                objects=now['character_extra']['root_members']['objects']
                require(objects.count(member)==1,'CROSS_ROOT_MEMBER',member)
                objects.remove(member)
                for layer in now['character_extra']['view_layers']:
                    hidden=layer['base_hidden']
                    require(hidden.pop(self.garment.name,None) is False,'CROSS_NEW_BASE_VISIBILITY',layer['name'])
            require(now==old,'CROSS_ORIGINAL_AUTHOR_CHANGED',C.diff(old,now,limit=20))
        # Existing embedded materials/world node authors and all original graph
        # edges remain exact. New ownership edges have at least one owned end.
        old_embedded={(C.digest(e['owner']),id_key(e['record'])):e for e in before['embedded']}
        new_embedded={(C.digest(e['owner']),id_key(e['record'])):e for e in after['embedded']}
        require(all(new_embedded.get(k)==v for k,v in old_embedded.items()),'CROSS_ORIGINAL_EMBEDDED_CHANGED',None)
        old_identities={C.digest({k:b[k] for k in ('type','name','library','owner') if k in b}) for b in before['blocks']}
        surviving_edges=[e for e in after['edges'] if C.digest(e['user']) in old_identities and C.digest(e['target']) in old_identities]
        require(surviving_edges==before['edges'],'CROSS_ORIGINAL_GRAPH_CHANGED',None)
        return {'existing_authors_exact_except_declared_root_link_and_rig_users':True,'rig_reference_accounting':accounting}

    def prepare(self,request,fitted_keys_world):
        self._guard()
        require(self._pending is None,'CROSS_PREPARATION_PENDING',self.pending)
        require(type(request) is dict and set(request)=={'schema','donor_object','key_names','target_deform_groups','driver_mapping'},
                'CROSS_PREPARATION_REQUEST',request)
        require(request['schema']=='hrig.foreign-garment-preparation.v1' and request['donor_object']=='GEO-snow-shirt',
                'CROSS_DONOR_OBJECT',request)
        points=np.asarray(fitted_keys_world)
        require(points.dtype==np.dtype('<f4') and points.shape==(10,1839,3) and bool(np.isfinite(points).all()),
                'CROSS_FIT_ARRAY_DOMAIN',{'dtype':str(points.dtype),'shape':points.shape})
        require(type(request['key_names']) is list and len(request['key_names'])==10
                and len(set(request['key_names']))==10,'CROSS_KEY_NAMES',request['key_names'])
        deform={b.name for b in self.rig.data.bones if b.use_deform}
        names=request['target_deform_groups']
        require(type(names) is list and bool(names) and len(set(names))==len(names)
                and set(names)<=deform,'CROSS_TARGET_DEFORM_DEFINITIONS',names)
        mapping=request['driver_mapping']
        require(type(mapping) is list and len(mapping)==9,'CROSS_DRIVER_MAPPING',mapping)
        for item in mapping:
            require(type(item) is dict and set(item)=={'driver_path','source_control','source_axis','target_control','target_axis','sign'},
                    'CROSS_DRIVER_ENTRY',item)
            require(item['target_control'] in self.rig.pose.bones and item['target_axis'] in ('ROT_X','ROT_Y','ROT_Z')
                    and item['sign'] in (-1,1) and type(item['sign']) is int,'CROSS_DRIVER_NEW_BINDING',item)
        self._same(self._collect(),self.before,'CROSS_PREPARATION_STALE')
        self._pending={'token':'cross_restore_'+uuid.uuid4().hex,'phase':'preparing','after':None}
        try:
            with bpy.data.libraries.load(self.donor['file'],link=False) as (available,loaded):
                require(request['donor_object'] in available.objects,'CROSS_DONOR_NOT_FOUND',request['donor_object'])
                loaded.objects=[request['donor_object']]
            self.owned=local_ids()-self.original_ids
            garment=loaded.objects[0];self.garment=garment
            require(garment and garment in self.owned and garment.type=='MESH','CROSS_OWNED_GARMENT',request['donor_object'])
            garment.name=self.new_name;garment.data.name=self.new_name+'_Mesh'
            keys=garment.data.shape_keys
            require(keys and [k.name for k in keys.key_blocks]==request['key_names'],'CROSS_IMPORTED_KEY_ROSTER',None)
            keys.name=self.new_name+'_Keys'
            original_groups=[(g.index,g.name,g.lock_weight) for g in garment.vertex_groups]
            require(not ({g.name for g in garment.vertex_groups}&deform),'CROSS_OLD_NAMES_COLLIDE_TARGET_DEFORM',None)
            original_active=garment.vertex_groups.active_index
            old_drivers=[{'path':f.data_path,'expression':f.driver.expression,'targets':[
                {'id':t.id.name if t.id else None,'bone':t.bone_target,'axis':t.transform_type,'space':t.transform_space}
                for v in f.driver.variables for t in v.targets]} for f in keys.animation_data.drivers]
            paths={f.data_path:f for f in keys.animation_data.drivers}
            require(set(paths)=={x['driver_path'] for x in mapping},'CROSS_DRIVER_ROSTER',list(paths))
            for item in mapping:
                f=paths[item['driver_path']]
                require(len(f.driver.variables)==1,'CROSS_DRIVER_VARIABLE_COUNT',item['driver_path'])
                variable=f.driver.variables[0]
                require(variable.name=='var' and variable.type=='TRANSFORMS' and len(variable.targets)==1,
                        'CROSS_DRIVER_VARIABLE_SCHEMA',item['driver_path'])
                target=variable.targets[0]
                require(target.bone_target==item['source_control'] and target.transform_type==item['source_axis']
                        and target.transform_space=='LOCAL_SPACE','CROSS_DRIVER_SOURCE_CHANGED',item)
                target.id=self.rig;target.bone_target=item['target_control'];target.transform_type=item['target_axis']
                if item['sign']==-1:
                    f.driver.expression=re.sub(r'\bvar\b','(-var)',f.driver.expression)
            object_controls=[]
            for f in garment.animation_data.drivers:
                require(len(f.driver.variables)==1 and len(f.driver.variables[0].targets)==1,'CROSS_OBJECT_DRIVER_SCHEMA',f.data_path)
                variable=f.driver.variables[0];target=variable.targets[0]
                require(variable.type=='SINGLE_PROP' and variable.name in ('Hide_Left_Arm','Hide_Right_Arm','Quality'),
                        'CROSS_OBJECT_DRIVER_SOURCE',variable.name)
                value=target.id.path_resolve(target.data_path)
                require(type(value) in (int,float,bool),'CROSS_OBJECT_PROPERTY_VALUE',variable.name)
                prop='D4_'+variable.name;garment[prop]=value
                target.id=garment;target.data_path='["'+prop+'"]';target.bone_target=''
                object_controls.append({'property':prop,'value':value,'driver_path':f.data_path,'expression':f.driver.expression})
            modifiers=[m for m in garment.modifiers if m.type=='ARMATURE']
            require(len(modifiers)==1,'CROSS_ARMATURE_COUNT',len(modifiers))
            modifiers[0].object=self.rig
            garment.parent=self.rig;garment.parent_type='OBJECT';garment.parent_bone=''
            garment.matrix_parent_inverse=self.rig.matrix_world.inverted();garment.matrix_world=Matrix.Identity(4)
            garment.data.vertices.foreach_set('co',points[0].reshape(-1))
            for index,key in enumerate(keys.key_blocks):
                key.data.foreach_set('co',points[index].reshape(-1))
            for name in names:
                require(name not in garment.vertex_groups,'CROSS_DEFORM_ALREADY_EXISTS',name)
                require(garment.vertex_groups.new(name=name).name==name,'CROSS_DEFORM_NATIVE_NAME',name)
            garment.vertex_groups.active_index=original_active
            bpy.context.scene.collection.objects.link(garment)
            garment.select_set(False)
            garment.data.update();garment.update_tag(refresh={'OBJECT','DATA'});bpy.context.view_layer.update()
            self.owned=local_ids()-self.original_ids
            require([(g.index,g.name,g.lock_weight) for g in list(garment.vertex_groups)[:len(original_groups)]]==original_groups,
                    'CROSS_ORIGINAL_GROUP_DEFINITIONS_CHANGED',None)
            for index,key in enumerate(keys.key_blocks):
                actual=np.empty(len(key.data)*3,dtype='<f4');key.data.foreach_get('co',actual)
                require(actual.tobytes()==points[index].reshape(-1).tobytes(),'CROSS_ALL_KEY_F32_WRITE',key.name)
            require([(s.link,s.material.name if s.material else None) for s in garment.material_slots]==[('OBJECT','snow.shirt')],
                    'CROSS_EFFECTIVE_MATERIAL',None)
            after=self._collect();protection=self._existing_authors(after['author'])
            actual_context=deepcopy(after['context']);actual_context['selection'].pop(garment.name)
            require(actual_context==self.before['context'],'CROSS_ORIGINAL_CONTEXT_CHANGED',actual_context)
            require(after['body_skin'].shape==self.before['body_skin'].shape
                    and after['body_skin'].tobytes()==self.before['body_skin'].tobytes(),'CROSS_ORIGINAL_BODY_SKIN_CHANGED',None)
            require(after['bones'].shape==self.before['bones'].shape
                    and after['bones'].tobytes()==self.before['bones'].tobytes(),'CROSS_ORIGINAL_BONE_OUTPUT_CHANGED',None)
            self._pending.update(phase='prepared',after=after)
            return {'schema':'hrig.foreign-garment-prepared.v1','restore_token':self._pending['token'],
                'garment':garment.name,'mesh':garment.data.name,'keys':keys.name,'key_count':len(keys.key_blocks),
                'same_declared_affine_all_keys_applied_f32':True,'body_skin_exact':True,'all_bone_outputs_exact':True,'protection':protection,
                'original_key_driver_provenance':old_drivers,'new_key_driver_semantics':deepcopy(mapping),
                'owned_object_properties':object_controls,'owned_ids':[{'type':i.bl_rna.identifier,'name':i.name} for i in self.owned],
                'author_sha256':C.digest(after['author']),
                'qualification':'Native cross-character geometry/driver/binding preparation only; C7 weights and fit/pose quality pending'}
        except Exception as error:
            self.owned=local_ids()-self.original_ids
            self._pending['phase']='recovery_requires_review'
            raise GarmentError('CROSS_PREPARATION_FAILED_REVIEW',{'cause':error_record(error),'pending':self.pending,
                'owned_id_count':len(self.owned),'source_disk_unchanged':True,'no_forced_cleanup':True}) from error

    def validate_prepared(self):
        self._guard()
        require(self._pending and self._pending['phase']=='prepared','CROSS_NOT_PREPARED',self.pending)
        self._same(self._collect(),self._pending['after'],'CROSS_PREPARED_CURRENT_CHANGED')
        return {'status':'prepared_exact','author_sha256':C.digest(self._pending['after']['author'])}

    def restore(self,token):
        require(self._pending and self._pending['token']==token,'CROSS_RESTORE_TOKEN',token)
        self.validate_prepared()
        require(local_ids()-self.original_ids==self.owned,'CROSS_OWNED_GRAPH_CHANGED',None)
        self._pending['phase']='restoring'
        try:
            bpy.data.batch_remove(ids=self.owned)
            bpy.context.view_layer.update()
            self._same(self._collect(),self.before,'CROSS_FULL_SOURCE_RESTORE')
            result={'status':'restored','full_original_author_context_body_skin_exact':True,'all_owned_ids_removed':True,
                    'author_sha256':C.digest(self.before['author'])}
            self._pending=None;self.garment=None;self.owned=set()
            return result
        except Exception:
            self._pending['phase']='recovery_requires_review'
            raise
