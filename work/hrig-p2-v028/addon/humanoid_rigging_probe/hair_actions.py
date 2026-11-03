"""Fixed-Aon paired manual rotation Actions; old motion guards remain unchanged."""
import bpy
from . import hair_pair_isolation as HP,motion_clips as M,motion_contract as MC

PARTS={'front':('HRIG_front_hair','千早爱音- 赋权前发网格','前发-02'),
       'rear':('HRIG_rear_hair','千早爱音- 赋权后发网格','后发-02')}
def need(x,message):
    if not x:raise ValueError(message)
def resolve(context):
    need(context.mode=='OBJECT' and context.scene.frame_current==0 and context.scene.frame_subframe==0,'Object mode at frame0 required')
    original=bpy.data.objects.get('千早爱音-素体骨架')
    need(original is not None and original.type=='ARMATURE' and len(original.data.bones)==1733,'Original1733 rig required')
    need(original.library is None and original.data.library is None and original.override_library is None,'Local original required')
    result={}
    for part,(name,mesh_name,bone) in PARTS.items():
        obj=bpy.data.objects.get(name);mesh=bpy.data.objects.get(mesh_name)
        need(obj is not None and obj.type=='ARMATURE' and mesh is not None and mesh.type=='MESH','Branch/mesh missing')
        need(obj.library is None and obj.data.library is None and obj.override_library is None and obj.data.override_library is None,'Local branch required')
        need(obj.data.users==1 and obj.data.pose_position=='POSE' and obj.mode=='OBJECT','Independent Pose armature required')
        need(context.view_layer.objects.get(name)==obj and context.scene.objects.get(name)==obj,'Branch outside current scene/layer')
        need(len(mesh.modifiers)>1 and mesh.modifiers[1].type=='ARMATURE' and mesh.modifiers[1].object==obj,'Actual mesh binding differs')
        need(set(obj.data.bones.keys())==HP.bone_names(part),'Fixed bone closure differs')
        for n in HP.BOUNDARIES:
            cs=obj.pose.bones[n].constraints
            need(len(cs)==1,'Boundary count differs')
            c=cs[0]
            need(c.type=='COPY_TRANSFORMS' and c.target==original and c.subtarget==n and
                 c.owner_space==c.target_space=='WORLD' and c.mix_mode=='REPLACE' and not c.mute and c.influence==1,'Boundary differs')
        need(obj.pose.bones[bone].rotation_mode=='QUATERNION','Quaternion control required')
        result[part]=obj
    return result

def _mutation_seam(stage,part):
    return None

def inspect(context,action_names):
    need(type(action_names)is dict and set(action_names)==set(PARTS),'Explicit two Action names required')
    rigs=resolve(context)
    need(len(set(action_names.values()))==2,'Action names must differ')
    out={}
    for part,rig in rigs.items():
        q=M.inspect_clip(context,rig,[PARTS[part][2]],action_names[part])
        need(q['ok'],str(q.get('error')))
        out[part]=q
    return out

def create(context,requests):
    """Preflight both; rollback the pair if either native create fails."""
    actions={};backups={};begun=False
    try:
        need(type(requests)is dict and set(requests)==set(PARTS),'Explicit two requests required')
        reqs={part:MC.canonical_request(r) for part,r in requests.items()}
        rigs=resolve(context)
        observed=inspect(context,{p:r['action_name'] for p,r in reqs.items()})
        common=None
        for p,r in reqs.items():
            need(len(r['tracks'])==1 and r['tracks'][0]['bone']==PARTS[p][2],'Only declared front/rear control supported')
            t=r['tracks'][0]
            need(t['rotation_mode']=='QUATERNION','Quaternion required')
            frames=[k['frame'] for k in t['keys']]
            need(t['keys'][-1]['value']==t['before'],'Last key must restore original rotation')
            need(common is None or frames==common,'Shared frame list required')
            common=frames
            need(r['expected_snapshot_sha256']==observed[p]['snapshot_sha256'],'Snapshot conflict')
            need(t['before']==observed[p]['fields'][t['bone']]['value'],'Before conflict')
            rig=rigs[p]
            backups[p]=(M._raw_ad(rig.animation_data),M.animation_state(rig),
                        [(t['bone'],'QUATERNION','rotation_quaternion',list(rig.pose.bones[t['bone']].rotation_quaternion))])
        begun=True
        for p in ('front','rear'):
            r=M.create_clip(context,rigs[p],reqs[p])
            need(r['ok'],str(r.get('error'))+' rollback='+str(r.get('rollback')))
            actions[p]=bpy.data.actions.get(reqs[p]['action_name'])
            _mutation_seam('after_branch_create',p)
        for p in rigs:M._verify(context,rigs[p],reqs[p])
        return {'ok':True,'changed':True,'schema':'hrig.hair-manual-pair.v1',
                'actions':{p:M._action_summary(a,rigs[p].animation_data.action_slot) for p,a in actions.items()},
                'scope':'Two new manual rotation tracks; no legacy hair tracks were migrated'}
    except Exception as exc:
        failures=[]
        if begun:
            for p in reversed(('front','rear')):
                if p in backups:
                    action=bpy.data.actions.get(reqs[p]['action_name'])
                    raw,state,rot=backups[p]
                    restored=M._restore(rigs[p],action,raw,state,rot)
                    if not restored['ok']:failures.append({'part':p,**restored})
        return {'ok':False,'changed':bool(failures),'error':{'type':type(exc).__name__,'message':str(exc)},
                'rollback':{'ok':not failures,'failures':failures,'begun':begun}}
