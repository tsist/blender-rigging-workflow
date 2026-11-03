"""Bounded native input/restore core, shared by GUI and CLI transports.

S1 supports a declared right-wrist local-X delta only. It preserves native
rigs, drivers and weights; downstream evaluated response is observed separately.
"""
import hashlib,json,struct,uuid
import bpy,numpy as np
import character_state as C

class ControlError(RuntimeError):
    def __init__(self,code,detail=None):super().__init__(code);self.code=code;self.detail=detail

def f32(value):return struct.pack('<f',float(value)).hex()
def context():
    return {'scene':bpy.context.scene.name,'layer':bpy.context.view_layer.name,'frame':bpy.context.scene.frame_current_final,
      'mode':bpy.context.mode,'active':None if bpy.context.view_layer.objects.active is None else bpy.context.view_layer.objects.active.name,
      'selected':sorted(o.name for o in bpy.context.selected_objects),
      'active_bones':{a.name:None if a.bones.active is None else a.bones.active.name for a in bpy.data.armatures},
      'constraint_active':{o.name:{p.name:None if p.constraints.active is None else p.constraints.active.name for p in o.pose.bones} for o in bpy.data.objects if o.pose}}

def curves(action,slot=None):
    if action is None:return []
    if slot is None and len(action.slots)>1:raise ControlError('PRECHECK_DEFERRED','ambiguous_action_slot:'+action.name)
    handle=slot.handle if slot is not None else action.slots[0].handle if len(action.slots)==1 else None
    out=[]
    for layer in action.layers:
        for strip in layer.strips:
            if not hasattr(strip,'channelbags'):raise ControlError('PRECHECK_DEFERRED','unknown_action_strip')
            for bag in strip.channelbags:
                if handle is None or bag.slot_handle==handle:out.extend(bag.fcurves)
    return out

def writer_scan(rig,paths):
    writers=[];observed=[]
    def inspect_curves(items,kind,owner):
        for fc in items:
            if (fc.data_path,fc.array_index) in paths:
                writers.append({'kind':kind,'owner':owner,'path':fc.data_path,'index':fc.array_index})
    for owner in (rig,rig.data):
        ad=owner.animation_data
        if ad is None:continue
        inspect_curves(ad.drivers,'driver',owner.name)
        inspect_curves(curves(ad.action,ad.action_slot),'active_action',owner.name)
        def nla(strips):
            for s in strips:
                inspect_curves(curves(s.action,s.action_slot) if s.action else [],'NLA',s.name)
                if getattr(s,'strips',None):nla(s.strips)
        for track in ad.nla_tracks:nla(track.strips)
        observed.append({'owner':owner.name,'drivers':len(ad.drivers),'action':None if ad.action is None else ad.action.name,'nla_tracks':len(ad.nla_tracks)})
    for pb in rig.pose.bones:
        for con in pb.constraints:
            if con.type=='ACTION' and con.action is not None:
                inspect_curves(curves(con.action,getattr(con,'action_slot',None)),'ACTION_constraint',pb.name+'/'+con.name)
    return {'writers':writers,'observed':observed}

def changes(before,after):
    out=[]
    def walk(a,b,path):
        if type(a) is not type(b):out.append(path);return
        if isinstance(a,dict):
            if a.keys()!=b.keys():out.append(path);return
            for k in a:walk(a[k],b[k],path+(k,))
        elif isinstance(a,list):
            if len(a)!=len(b):out.append(path);return
            for i,(x,y) in enumerate(zip(a,b)):walk(x,y,path+(i,))
        elif a!=b:out.append(path)
    walk(before,after,());return out

class NativeControl:
    def __init__(self,binding):
        self.binding=binding;self.pending=None;self.write_count=0
        self.rig=bpy.data.objects.get(binding['rig']);self.inspect_token=None
        if self.rig is None or self.rig.type!='ARMATURE' or self.rig.library is not None:raise ControlError('RIG_IDENTITY')
        if self.rig.data.name!=binding['data']:raise ControlError('RIG_DATA_IDENTITY')

    def preflight(self,operations):
        if not isinstance(operations,list) or not 1<=len(operations)<=2:raise ControlError('BATCH_SHAPE')
        if bpy.context.mode not in ('OBJECT','POSE'):raise ControlError('MODE_UNSUPPORTED')
        resolved=[];paths=set()
        for op in operations:
            if op.get('kind')!='pose_component_delta':raise ControlError('TARGET_KIND_FORBIDDEN',op.get('kind'))
            if set(op)!={'kind','bone','channel','index','delta'}:raise ControlError('COMPONENT_SCHEMA')
            bone=op.get('bone');channel=op.get('channel');index=op.get('index');delta=op.get('delta')
            if channel not in ('location','rotation_euler') or type(index) is not int or not 0<=index<3 or type(delta) not in (int,float) or not np.isfinite(delta):raise ControlError('COMPONENT_SCHEMA')
            pb=self.rig.pose.bones.get(bone)
            if pb is None:raise ControlError('BONE_MISSING',bone)
            locks=pb.lock_location if channel=='location' else pb.lock_rotation
            if locks[index]:raise ControlError('COMPONENT_LOCKED',bone+'/'+channel+'/'+str(index))
            if bone!=self.binding['bone'] or channel!='location' or index!=0 or float(delta)!=.005:raise ControlError('S1_BINDING_UNSUPPORTED')
            if pb.parent is None or pb.parent.name!=self.binding['parent'] or pb.rotation_mode!=self.binding['rotation_mode']:raise ControlError('BONE_BINDING_DRIFT')
            path=pb.path_from_id(channel);key=(path,index)
            if key in paths:raise ControlError('DUPLICATE_COMPONENT')
            paths.add(key);resolved.append((pb,channel,index,float(delta)))
            paths.update((pb.path_from_id('matrix_basis'),i) for i in range(16))
        modepath=self.binding['mode_path'];mode=self.rig.path_resolve(modepath)
        if type(mode) not in (float,int) or mode!=1:raise ControlError('MODE_ASSERTION')
        paths.add((modepath,0));scan=writer_scan(self.rig,paths)
        if scan['writers']:raise ControlError('WRITER_CONFLICT',scan['writers'])
        # Invalid flags only block the actual input, asserted mode or its readers.
        invalid=[];ad=self.rig.animation_data
        for fc in ad.drivers if ad else []:
            relevant=(fc.data_path,fc.array_index) in paths or any(t.id==self.rig and t.data_path==modepath for v in fc.driver.variables for t in v.targets)
            if relevant and not fc.driver.is_valid:invalid.append(fc.data_path)
        if invalid:raise ControlError('INVALID_DRIVER_IN_FOOTPRINT',invalid)
        return resolved,{'mode':mode,'mode_type':type(mode).__name__,'scan':scan,'invalid_relevant':invalid}

    def observe(self):
        dg=bpy.context.evaluated_depsgraph_get();er=self.rig.evaluated_get(dg)
        out={}
        for name in self.binding['witnesses']:
            if name not in er.pose.bones:raise ControlError('WITNESS_MISSING',name)
            matrix=np.asarray(er.matrix_world@er.pose.bones[name].matrix,dtype='<f4')
            if not np.isfinite(matrix).all():raise ControlError('NONFINITE_WITNESS')
            out[name]=matrix.tolist()
        return out

    def inspect(self,operations):
        resolved,preflight=self.preflight(operations)
        before=C.capture();token='snap_'+uuid.uuid4().hex
        derived=[]
        def declare(owner,destination,fc,record):
            if fc.mute or not fc.driver.is_valid or not fc.driver.variables:return
            targets=[t for v in fc.driver.variables for t in v.targets]
            if not all(v.type=='TRANSFORMS' for v in fc.driver.variables) or not targets:return
            if not all(t.id==self.rig and t.bone_target in self.binding['witnesses'] for t in targets):return
            record.update(path=fc.data_path,type=fc.driver.type,expression=fc.driver.expression,source_bones=sorted({t.bone_target for t in targets}),
              targets=[{'bone':t.bone_target,'transform_type':t.transform_type,'rotation_mode':t.rotation_mode,'space':t.transform_space} for t in targets],
              native_value_before=float(getattr(destination,record['field'])))
            derived.append((owner,destination,fc,record))
        for key in bpy.data.shape_keys:
            for fc in key.animation_data.drivers if key.animation_data else []:
                for block in key.key_blocks:
                    if fc.data_path==block.path_from_id('value'):
                        declare(key,block,fc,{'kind':'key_value','field':'value','key':key.name,'owner':C.key_owner(key).name,'block':block.name})
        constraints={c.path_from_id():(p,c) for p in self.rig.pose.bones for c in p.constraints}
        for fc in self.rig.animation_data.drivers if self.rig.animation_data else []:
            prefix,sep,field=fc.data_path.rpartition('.')
            if prefix not in constraints:continue
            pb,con=constraints[prefix];prop=con.bl_rna.properties.get(field)
            if prop is not None and prop.type=='FLOAT' and not prop.is_array and fc.array_index==0:
                declare(self.rig,con,fc,{'kind':'constraint_scalar','field':field,'rig':self.rig.name,'bone':pb.name,'constraint':con.name})
        self.inspect_token={'token':token,'before':before,'context':context(),'operations':operations,'fingerprint':C.digest(before),'witness':self.observe(),'derived':derived}
        return {'status':'inspected','snapshot_token':token,'live_fingerprint':self.inspect_token['fingerprint'],'preflight':preflight,'witness':self.inspect_token['witness'],
          'declared_native_driven_outputs':[d[3] for d in derived],'write_count':self.write_count}

    def _allowed(self,before,pb,actual_witness):
        allowed=[]
        for i,b in enumerate(before['blocks']):
            if b['type']=='Object' and b['name']==self.rig.name and b['library'] is None:
                j=next(j for j,p in enumerate(b['pose']) if p['name']==pb.name)
                allowed=[('blocks',i,'pose',j,'matrix_basis'),('blocks',i,'pose',j,'settings','location',0),('blocks',i,'pose',j,'settings','matrix_basis'),
                 ('blocks',i,'character_extra','pose_custom',pb.name,'location',0),('blocks',i,'character_extra','pose_custom',pb.name,'matrix_basis')]
        for owner,destination,fc,record in self.inspect_token['derived']:
            if not fc.driver.is_valid:raise ControlError('DERIVED_DRIVER_BECAME_INVALID',record)
            changed=any(actual_witness[n]!=self.inspect_token['witness'][n] for n in record['source_bones'])
            if not changed:continue
            for i,b in enumerate(before['blocks']):
                if record['kind']=='key_value' and b['type']=='Key' and b['name']==owner.name and b.get('owner',{}).get('name')==record['owner']:
                    j=next(j for j,k in enumerate(b['character_extra']['blocks']) if k['name']==destination.name)
                    allowed.append(('blocks',i,'character_extra','blocks',j,'settings','value'))
                elif record['kind']=='constraint_scalar' and b['type']=='Object' and b['name']==self.rig.name:
                    j=next(j for j,p in enumerate(b['pose']) if p['name']==record['bone'])
                    k=next(k for k,c in enumerate(b['pose'][j]['constraints']) if c['name']==record['constraint'])
                    allowed.append(('blocks',i,'pose',j,'constraints',k,record['field']))
        return allowed

    def apply(self,operations,snapshot_token,fault=None):
        if fault not in (None,'after_write'):raise ControlError('FAULT_SCHEMA')
        if self.pending is not None:raise ControlError('APPLY_ALREADY_PENDING')
        token=self.inspect_token
        if token is None or token['token']!=snapshot_token or token['operations']!=operations:raise ControlError('STALE_SNAPSHOT')
        resolved,preflight=self.preflight(operations)
        live=C.capture()
        if live!=token['before'] or context()!=token['context']:raise ControlError('LIVE_STATE_CHANGED')
        if self.observe()!=token['witness']:raise ControlError('STALE_ACTUAL_WITNESS')
        pb,channel,index,delta=resolved[0];original=float(pb.location[index]);expected=float(np.float32(original+delta));before=token['before']
        self.pending={'token':'rst_'+uuid.uuid4().hex,'before':before,'context':token['context'],'pb':pb,'index':index,'original':original,'written':expected,'witness':token['witness']}
        try:
            pb.location[index]=expected;self.write_count+=1;self.rig.update_tag(refresh={'OBJECT','DATA'});bpy.context.view_layer.update()
            if f32(pb.location[index])!=f32(expected):raise ControlError('WRITE_READBACK')
            if fault=='after_write':raise ControlError('INJECTED_AFTER_WRITE')
            actual=self.observe();after=C.capture();delta_paths=changes(before,after);allowed=self._allowed(before,pb,actual)
            unexpected=[p for p in delta_paths if not any(p[:len(prefix)]==prefix for prefix in allowed)]
            if unexpected:raise ControlError('PROTECTION_CHANGED',[list(p) for p in unexpected[:80]])
            if context()!=token['context']:raise ControlError('CONTEXT_CHANGED')
            self.pending['after']=after;self.pending['after_fingerprint']=C.digest(after);self.pending['after_witness']=actual
            responding=[n for n in actual if n!=pb.name and np.asarray(actual[n],dtype='<f4').tobytes()!=np.asarray(token['witness'][n],dtype='<f4').tobytes()]
            return {'status':'applied_mechanism_observed' if responding else 'unresolved_no_downstream_response','restore_token':self.pending['token'],
              'written_f32':f32(expected),'before_f32':f32(original),'changed_author_paths':[list(p) for p in delta_paths],'allowed_prefixes':[list(p) for p in allowed],
              'after_fingerprint':self.pending['after_fingerprint'],'witness_before':token['witness'],'witness_after':actual,'responding_downstream':responding,'write_count':self.write_count,
              'native_driven_outputs':[{**record,'native_value_after':float(getattr(destination,record['field']))} for owner,destination,fc,record in token['derived']]}
        except Exception as error:
            rollback=self._restore_internal()
            if not rollback['full_author_exact'] or not rollback['actual_witness_exact'] or not rollback['context_exact']:raise ControlError('ROLLBACK_FAILED',rollback) from error
            if isinstance(error,ControlError):error.detail={'cause_detail':error.detail,'rollback':rollback}
            raise

    def _restore_internal(self):
        state=self.pending
        if state is None:raise ControlError('NO_PENDING_RESTORE')
        state['pb'].location[state['index']]=state['original'];self.rig.update_tag(refresh={'OBJECT','DATA'});bpy.context.view_layer.update()
        after=C.capture();actual=self.observe()
        report={'full_author_exact':after==state['before'],'author_sha256':C.digest(after),'context_exact':context()==state['context'],
          'actual_witness_exact':actual==state['witness'],'raw_original_exact':f32(state['pb'].location[state['index']])==f32(state['original'])}
        if all(report[k] for k in ('full_author_exact','context_exact','actual_witness_exact','raw_original_exact')):self.pending=None;self.inspect_token=None
        return report

    def restore(self,restore_token):
        if self.pending is None or self.pending['token']!=restore_token:raise ControlError('RESTORE_TOKEN_REQUIRED')
        if C.capture()!=self.pending['after'] or context()!=self.pending['context']:raise ControlError('STALE_APPLIED_STATE')
        if self.observe()!=self.pending['after_witness']:raise ControlError('STALE_ACTUAL_WITNESS')
        report=self._restore_internal()
        if not all(report[k] for k in ('full_author_exact','context_exact','actual_witness_exact','raw_original_exact')):raise ControlError('RESTORE_FAILED',report)
        return {'status':'restored_exact',**report}
