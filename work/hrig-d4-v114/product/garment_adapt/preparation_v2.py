"""Explicit reversible mask/deform separation for the v114 garment candidate.

Original deform memberships are never edited here. All new definitions are
appended; exact full C plus independent ordered rows fence their restoration.
This is a preparation dependency for the weight transaction, not a way to
weaken its consumer protection. No file saving or context mutation occurs.
"""
from copy import deepcopy
import uuid

from .contract import require, bits, number, digest
from .native import C, error_record, raw_rows, rows_digest


class MaskPreparation:
    def __init__(self,session):
        self.session=session
        self._snapshot=None
        self._pending=None

    @property
    def pending(self):
        return deepcopy({k:self._pending[k] for k in ('token','phase')}) if self._pending else None

    def inspect(self,request):
        s=self.session
        require(self._pending is None and s.pending is None,'PREPARATION_OTHER_PENDING',self.pending)
        s._guard()
        require(type(request) is dict and set(request)=={'schema','mask_copies','add_deform_groups'}
                and request['schema']=='hrig.garment-mask-preparation.v1','MASK_PREPARATION_SCHEMA',request)
        copies=request['mask_copies']; additions=request['add_deform_groups']
        require(type(copies) is list and 1<=len(copies)<=16,'MASK_COPY_BUDGET',copies)
        require(type(additions) is list and len(additions)<=256 and all(type(n) is str and n for n in additions)
                and len(set(additions))==len(additions),'NEW_DEFORM_DEFINITIONS',additions)
        groups=list(s.target.vertex_groups);names=[g.name for g in groups]
        deform={b.name for b in s.rig.data.bones if b.use_deform}
        keys=s.target.data.shape_keys
        require(keys is not None,'SHAPE_KEYS_REQUIRED',s.target.name)
        used=set();newnames=[]
        consumers=s._consumers()
        for item in copies:
            require(type(item) is dict and set(item)=={'source','target','keys'},'MASK_COPY_SCHEMA',item)
            require(type(item['source']) is str and item['source'] in set(names)&deform and item['source'] not in used,
                    'MASK_COPY_SOURCE',item)
            require(type(item['target']) is str and item['target'].startswith('D4_MASK_')
                    and item['target'] not in set(names)|deform|set(newnames),'MASK_COPY_TARGET',item)
            actual=[k.name for k in keys.key_blocks if k.vertex_group==item['source']]
            require(type(item['keys']) is list and bool(actual) and sorted(item['keys'])==sorted(actual)
                    and len(set(item['keys']))==len(item['keys']),'MASK_KEY_READER_SET',{'request':item,'actual':actual})
            require(all(c['kind']=='shape_key_mask' for c in consumers['items'] if c['group']==item['source']),
                    'MASK_SOURCE_OTHER_CONSUMERS',item['source'])
            used.add(item['source']);newnames.append(item['target'])
        require(set(additions)<=deform and not (set(additions)&(set(names)|set(newnames))),
                'NEW_DEFORM_BONE_DOMAIN',additions)
        before=s._collect()
        expected=deepcopy(before['author'])
        expected_rows=[list(row) for row in before['rows']]
        appended=[]
        for item in copies:
            source_index=names.index(item['source'])
            target_index=len(names)+len(appended)
            appended.append({'name':item['target'],'index':target_index,'lock_weight':True})
            for index,row in enumerate(before['rows']):
                original=dict(row)
                if source_index in original:
                    expected_rows[index].append((target_index,original[source_index]))
        for name in additions:
            appended.append({'name':name,'index':len(names)+len(appended),'lock_weight':False})
        matched={'object':0,'key':0}
        replacements={key:item['target'] for item in copies for key in item['keys']}
        for block in expected['blocks']:
            if block['type']=='Object' and block['name']==s.target.name and block['library'] is None:
                block['object']['vertex_groups'].extend(v['name'] for v in appended)
                block['character_extra']['group_definitions'].extend(deepcopy(appended))
                matched['object']+=1
            if block['type']=='Key' and block.get('owner')=={'type':'Mesh','name':s.mesh.name,'library':None}:
                for key in block['character_extra']['blocks']:
                    if key['name'] in replacements:
                        key['settings']['vertex_group']=replacements[key['name']]
                matched['key']+=1
        require(matched=={'object':1,'key':1},'MASK_PREPARATION_OBSERVER_TARGET',matched)
        token='mask_plan_'+uuid.uuid4().hex
        self._snapshot={'token':token,'request':deepcopy(request),'before':before,'expected_author':expected,
                        'expected_rows':tuple(tuple(row) for row in expected_rows),'appended':appended,
                        'original_active':s.target.vertex_groups.active_index}
        return {'schema':'hrig.garment-mask-inspection.v1','snapshot_token':token,'request':deepcopy(request),
                'appended_definitions':appended,'author_before_sha256':C.digest(before['author']),
                'expected_rows_sha256':rows_digest(self._snapshot['expected_rows']),
                'mask_effective_weight_policy':'Present/absent and f32 copied exactly; Key coordinates/drivers unchanged',
                'failure_policy':'Unobserved partial preparation refuses recovery writes; source disk remains unchanged'}

    def preview(self,snapshot_token):
        s=self.session;s._guard()
        require(self._pending is None and self._snapshot and self._snapshot['token']==snapshot_token,
                'MASK_PREPARATION_STALE',snapshot_token)
        require(s.pending is None,'WEIGHT_TRANSACTION_PENDING',s.pending)
        s._exact(s._collect(),self._snapshot['before'],'MASK_PREPARATION_BASELINE_CHANGED')
        self._pending={'token':'mask_restore_'+uuid.uuid4().hex,'phase':'preparing','after':None}
        try:
            before=self._snapshot['before']
            original_names=[g.name for g in s.target.vertex_groups]
            for item in self._snapshot['request']['mask_copies']:
                source_index=original_names.index(item['source'])
                group=s.target.vertex_groups.new(name=item['target'])
                require(group.name==item['target'],'MASK_NATIVE_NAME_CHANGED',item)
                for vertex,row in enumerate(before['rows']):
                    original=dict(row)
                    if source_index in original:
                        group.add([vertex],number(original[source_index]),'REPLACE')
                group.lock_weight=True
            for name in self._snapshot['request']['add_deform_groups']:
                require(s.target.vertex_groups.new(name=name).name==name,'DEFORM_NATIVE_NAME_CHANGED',name)
            for item in self._snapshot['request']['mask_copies']:
                for name in item['keys']:
                    s.mesh.shape_keys.key_blocks[name].vertex_group=item['target']
            s.target.vertex_groups.active_index=self._snapshot['original_active']
            s._update()
            after=s._collect()
            selected=list(range(len(s.mesh.vertices)))
            actual=C.protected(after['author'],s.target.name,s.mesh.name,selected)
            expected=C.protected(self._snapshot['expected_author'],s.target.name,s.mesh.name,selected)
            require(actual==expected,'MASK_PREPARATION_AUTHOR_CHANGED',C.diff(expected,actual,limit=25))
            require(after['rows']==self._snapshot['expected_rows'],'MASK_PREPARATION_ORDERED_F32_ROWS',
                    {'actual':rows_digest(after['rows']),'expected':rows_digest(self._snapshot['expected_rows'])})
            require(all(after[k]==before[k] for k in ('context','bones','body_evaluated','garment_evaluated','material_slots')),
                    'MASK_PREPARATION_CONTEXT_OR_EVALUATION_CHANGED',
                    {k:after[k]==before[k] for k in ('context','bones','body_evaluated','garment_evaluated','material_slots')})
            self._pending.update(phase='applied',after=after)
            return {'schema':'hrig.garment-mask-preview.v1','status':'prepared','restore_token':self._pending['token'],
                    'full_expected_author_projection_exact':True,'all_native_rows_exact':True,
                    'effective_masks_f32_preserved':True,'body_bones_skin_context_exact':True,
                    'author_sha256':C.digest(after['author']),'ordered_rows_sha256':rows_digest(after['rows'])}
        except Exception as error:
            self._pending['phase']='recovery_requires_review'
            from .contract import GarmentError
            raise GarmentError('MASK_PREPARATION_FAILED_REVIEW',{'cause':error_record(error),'pending':self.pending,
                'qualification':'No arbitrary partial state adopted; no whole-Mesh restoration. Original disk unchanged.'}) from error

    def validate_prepared(self):
        s=self.session;s._guard()
        require(self._pending and self._pending['phase']=='applied','MASK_PREPARATION_NOT_APPLIED',self.pending)
        require(s.pending is None,'WEIGHT_TRANSACTION_PENDING',s.pending)
        s._exact(s._collect(),self._pending['after'],'MASK_PREPARATION_CURRENT_CHANGED')
        return {'status':'prepared_exact','author_sha256':C.digest(self._pending['after']['author'])}

    def restore(self,restore_token):
        s=self.session
        require(self._pending and self._pending['token']==restore_token,'MASK_RESTORE_TOKEN',restore_token)
        self.validate_prepared()
        self._pending['phase']='restoring'
        try:
            for item in self._snapshot['request']['mask_copies']:
                for name in item['keys']:
                    s.mesh.shape_keys.key_blocks[name].vertex_group=item['source']
            for definition in reversed(self._snapshot['appended']):
                group=s.target.vertex_groups.get(definition['name'])
                require(group and group.index==len(s.target.vertex_groups)-1,'MASK_RESTORE_DEFINITION_ORDER',definition)
                s.target.vertex_groups.remove(group)
            s.target.vertex_groups.active_index=self._snapshot['original_active']
            s._update()
            s._exact(s._collect(),self._snapshot['before'],'MASK_PREPARATION_RESTORE_EXACT')
            result={'schema':'hrig.garment-mask-restore.v1','status':'restored',
                    'full_author_raw_order_f32_context_body_garment_bones_exact':True,
                    'author_sha256':C.digest(self._snapshot['before']['author'])}
            self._pending=None;self._snapshot=None
            return result
        except Exception:
            self._pending['phase']='recovery_requires_review'
            raise
