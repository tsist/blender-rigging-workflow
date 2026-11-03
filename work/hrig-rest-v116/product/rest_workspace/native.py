"""Batch-native adapter over the verified owned-copy/Object-fence core.

The v106 request implementation stays frozen. This adapter uses only its owned
ID, source guard, complete observer and Object-mode restoration mechanisms.
Its own request, raw plan, author projection and reference/replay are separate.
Unknown Edit partial states are retained, never promoted to checkpoints.
"""
from copy import deepcopy
from pathlib import Path
import json,uuid
import bpy,numpy as np
import character_state as C
from rest_edit.native import RestEditTransaction,block,parent_forest_order,bone_topology,data_rest_signature
from .contract import need,validate,plan,raw_protection,digest

def color(x):return {'palette':x.palette,'custom':{k:list(getattr(x.custom,k)) for k in ('normal','select','active')}}
def set_color(x,d):
    x.palette=d['palette']
    for k,v in d['custom'].items():setattr(x.custom,k,v)

class BatchRestTransaction:
    def __init__(self,profile,source,body,idle_guard=lambda:True):
        self.base=RestEditTransaction(profile,source,body,idle_guard)
        self.snapshot=None;self.applied=None;self.request=None;self.raw_plan=None
    @property
    def pending(self):return self.base.pending
    def _clone(self,arm,raw,new_name,collections,*,new=False):
        bone=arm.edit_bones.new(new_name)
        # Create disconnected and assign complete source configuration before
        # explicit parent/connected and final endpoints. No implicit X mirror.
        fields=raw['fields']
        for k,v in fields.items():
            if k in ('name','matrix','length','head','tail','roll','use_connect','custom_properties'):continue
            prop=bone.bl_rna.properties.get(k)
            need(prop is not None and not prop.is_readonly,'CLONE_RNA',k)
            setattr(bone,k,deepcopy(v))
        bone.head=fields['head'];bone.tail=fields['tail'];bone.roll=fields['roll']
        if not new:
            for k,v in fields.get('custom_properties',{}).items():bone[k]=deepcopy(v)
        for c in collections:arm.collections_all[c].assign(bone)
        bone.parent=arm.edit_bones.get(raw['parent']) if raw['parent'] else None
        bone.use_connect=fields['use_connect'] if not new else False
        for k in ('bbone_custom_handle_start','bbone_custom_handle_end'):
            value=raw[k] if not new else None
            if hasattr(bone,k):setattr(bone,k,arm.edit_bones.get(value) if value else None)
        return bone
    def _write(self,raw):
        b=self.base;arm=b.rig.data
        for a in self.request['atoms']:
            n=a['bone']
            if a['op']=='create':
                need(n not in arm.edit_bones,'NEW_BONE_EXISTS',n)
                donor=arm.edit_bones[a['donor']];palette=color(donor.color)
                bone=self._clone(arm,raw[a['donor']],n,a['collections'],new=True)
                bone.use_deform=False;bone.select=False;bone.select_head=False;bone.select_tail=False
                bone.parent=arm.edit_bones[a['parent']] if a['parent'] else None
                set_color(bone.color,palette)
            elif a['op']=='replace':
                old=arm.edit_bones[n];palette=color(old.color)
                need(not old.children and not old.use_connect,'REPLACE_SCOPE_CHANGED',n)
                arm.edit_bones.remove(old)
                bone=self._clone(arm,raw[n],n,raw[n]['collections']);set_color(bone.color,palette)
            else:bone=arm.edit_bones[n]
            for k,v in a['fields'].items():
                setattr(bone,k,arm.edit_bones[v] if v else None) if k=='parent' else setattr(bone,k,v)
                b.write_count+=1

    def _author_expected(self,actual,planned):
        b=self.base;expected=b._owned_author(assigned=True,mirror=b._pending['mirror_flag'])
        e=block(expected,b.rig.data);a=block(actual,b.rig.data)
        er=block(expected,b.rig);ar=block(actual,b.rig)
        by={r['name']:r for r in e['bones']};actualby={r['name']:r for r in a['bones']}
        born=set(planned['born']);affected=set(planned['affected'])
        need(set(actualby)==set(by)|born,'FINAL_BONE_ROSTER')
        for n in planned['direct']:
            if n in born:continue
            raw=planned['raw_expected'][n];settings=e['character_extra']['bone_settings'][n]['settings']
            for k,v in raw['fields'].items():
                if k in settings and k not in ('matrix','length','head','tail','roll'):settings[k]=deepcopy(v)
            e['character_extra']['bone_settings'][n]['parent']=raw['parent'];by[n]['parent']=raw['parent']
        for n in affected-born:
            # Final matrices/endpoints are native derivatives of verified raw
            # intent. Reference binds them; Preview cannot supply new values.
            by[n]['matrix_local']=deepcopy(actualby[n]['matrix_local'])
            for k in ('head','tail'):
                e['character_extra']['bone_settings'][n][k]=deepcopy(a['character_extra']['bone_settings'][n][k])
        for n in born:
            row=next(p for p in ar['pose'] if p['name']==n)
            pb=b.rig.pose.bones[n]
            need(not pb.constraints and np.array_equal(np.asarray(pb.matrix_basis,dtype='<f4'),np.eye(4,dtype='<f4'))
                and not any(pb.lock_location) and not any(pb.lock_rotation) and not any(pb.lock_scale),'NEW_POSE_BIRTH_CONTRACT',n)
            by[n]=deepcopy(actualby[n])
            e['character_extra']['bone_settings'][n]=deepcopy(a['character_extra']['bone_settings'][n])
            e['character_extra']['collection_system']['bone_visibility'][n]=deepcopy(a['character_extra']['collection_system']['bone_visibility'][n])
            er['pose'].append(deepcopy(row))
            for k in ('pose_custom','pose_colors','pose_constraint_targets'):
                er['character_extra'][k][n]=deepcopy(ar['character_extra'][k][n])
        topology=[{'name':n,'parent':v['parent']} for n,v in planned['raw_expected'].items()]
        order=parent_forest_order(list(planned['raw_expected']),topology)
        poseby={r['name']:r for r in er['pose']}
        need(set(poseby)==set(order),'FINAL_POSE_ROSTER')
        e['bones']=[by[n] for n in order];er['pose']=[poseby[n] for n in order]
        need(actual==expected,'AUTHOR_OUTSIDE_TYPED_REST_DOMAIN',C.diff(expected,actual,limit=25))
        return expected,order

    def _run(self,kind,baseline):
        b=self.base;stub={'schema':'hrig.rest-roll-delta.v1','bone':self.request['atoms'][0]['bone'],'delta':.01,'mirror_policy':'explicit_only'}
        owned_name=self.snapshot['owned_name'] if kind=='preview' else 'HRIG-batch-rest-'+uuid.uuid4().hex[:16]
        b._start(kind,baseline,stub,owned_name);b._create_owned();b._guard(0)
        arm=b.rig.data;mirror=b._pending['mirror_flag']
        bpy.context.view_layer.objects.active=b.rig
        with bpy.context.temp_override(object=b.rig,active_object=b.rig):
            need(bpy.ops.object.mode_set(mode='EDIT')=={'FINISHED'},'ENTER_EDIT')
        arm.use_mirror_x=False;b.write_count+=1
        raw=b._edit_raw();planned=plan(raw,self.request)
        self.raw_plan=planned
        # Parent setter selection side effects must be harmless in this
        # contract. Selected parents are refused in owned Edit with no atoms.
        for a in self.request['atoms']:
            p=a.get('parent',a['fields'].get('parent'))
            if p in raw:
                need(all(raw[p]['fields'][k] is False for k in ('select','select_head','select_tail')),'PARENT_SELECTION_UNSUPPORTED',p)
        if kind=='preview':need(planned==self.snapshot['raw_plan'],'RAW_SOURCE_PLAN_DRIFT')
        self._write(raw)
        postraw=b._edit_raw();raw_protection(postraw,planned)
        with bpy.context.temp_override(object=b.rig,active_object=b.rig):
            need(bpy.ops.object.mode_set(mode='OBJECT')=={'FINISHED'},'EXIT_EDIT')
        need(bpy.context.mode=='OBJECT' and b.rig.data==arm,'OWNED_OBJECT_REQUIRED')
        arm.use_mirror_x=mirror;b._restore_context(baseline['context'])
        after=b._collect();expected_author,order=self._author_expected(after['author'],planned)
        old=baseline['rest'];new=after['rest'];oldindex={n:i for i,n in enumerate(old['names'])};newindex={n:i for i,n in enumerate(new['names'])}
        for n in set(oldindex)-set(planned['affected']):
            for k in ('matrix_local','matrix_parent_relative','head_tail_local'):
                need(old[k][oldindex[n]].tobytes()==new[k][newindex[n]].tobytes(),'NON_TARGET_REST_CHANGED',{'bone':n,'field':k})
            need(baseline['lengths'][oldindex[n]].tobytes()==after['lengths'][newindex[n]].tobytes(),'NON_TARGET_LENGTH_CHANGED',n)
        need(after['rows']==baseline['rows'],'WEIGHTS_CHANGED')
        expected_context=b._owned_context()
        for n in planned['born']:expected_context['constraint_active'][b.rig.name][n]=None
        need(after['context']==expected_context,'CONTEXT_OUTSIDE_DECLARED_BIRTH')
        if kind=='preview':
            need(postraw==self.snapshot['raw_after'],'REPLAY_RAW_CHANGED')
            b._assert_state(after,self.snapshot['after'],'REPLAY_NOT_EXACT')
            need(int(arm.session_uid)!=self.snapshot['reference_uid'],'REFERENCE_UID_REUSED')
        b._pending['phase']='applied';b._object_checkpoint(after)
        return {'after':after,'raw_plan':planned,'raw_after':postraw,'order':order,'owned_name':owned_name,
                'reference_uid':int(arm.session_uid),'rest_sha256':data_rest_signature(arm)}

    def inspect(self,request):
        b=self.base;b._guard(0);need(not self.pending,'PENDING');self.request=validate(request)
        baseline=b._collect()
        candidate=self._run('reference',baseline)
        restored=b._rollback();need(restored['status']=='restored_exact','REFERENCE_RESTORE')
        candidate['token']='batch_snapshot_'+uuid.uuid4().hex;candidate['baseline']=baseline
        self.snapshot=candidate
        return {'status':'inspected','snapshot_token':candidate['token'],'request':deepcopy(self.request),
                'raw_plan_sha256':digest(candidate['raw_plan']),'born':candidate['raw_plan']['born'],
                'direct':candidate['raw_plan']['direct'],'affected':candidate['raw_plan']['affected'],'reference_restored':restored}
    def preview(self,token):
        b=self.base;need(self.snapshot and self.snapshot['token']==token and not self.pending,'STALE_SNAPSHOT')
        b._guard(0);b._assert_state(b._collect(),self.snapshot['baseline'],'SOURCE_CHANGED')
        self.applied=self._run('preview',self.snapshot['baseline'])
        return {'status':'previewed','restore_token':self.pending['token'],'rest_sha256':self.applied['rest_sha256'],
                'different_uid_exact_replay':True,'raw_and_author_exact':True}
    def validate_applied(self):
        need(self.pending and self.applied,'APPLIED_REQUIRED');self.base._guard(0)
        self.base._assert_state(self.base._collect(),self.applied['after'],'APPLIED_CHANGED')
        return {'status':'applied_exact','author_sha256':C.digest(self.applied['after']['author'])}
    def restore(self,token):
        need(self.pending and self.pending['token']==token,'RESTORE_TOKEN')
        result=self.base._rollback();self.applied=None;self.snapshot=None
        return result
