"""Managed native bone collections; no rest, transforms, weights or solo edits."""
import copy
import json
import re
import bpy
from . import collection_contract as contract
from . import styles

LEDGER='hrig_collection_instances_v1'
TAG='hrig_collection_owner_v1'
BOOK_SCHEMA='hrig.bone-collections.instances.v1'
OWNER_SCHEMA='hrig.bone-collections.owner.v1'


def fail(code,message,details=None):raise contract.CollectionError(code,message,details or {})


def json_value(raw):
    def pairs(rows):
        result={}
        for k,v in rows:
            if k in result:fail('LEDGER_INVALID','Duplicate JSON key')
            result[k]=v
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:fail('LEDGER_INVALID','Nonfinite JSON'))


def owner(col):
    if TAG not in col:return None
    raw=col[TAG]
    if type(raw) is not str or len(raw)>2048:fail('OWNER_INVALID','Invalid owner tag')
    try:value=json_value(raw)
    except Exception as e:fail('OWNER_INVALID','Malformed owner JSON',{'cause':str(e)})
    if type(value) is not dict or set(value)!={'schema','instance_id','role'} or value['schema']!=OWNER_SCHEMA:
        fail('OWNER_INVALID','Owner tag fields differ')
    if any(type(value[k]) is not str or not value[k] for k in ('instance_id','role')):fail('OWNER_INVALID','Owner identifiers missing')
    return value


def ledger(rig):
    if LEDGER not in rig:return {'schema':BOOK_SCHEMA,'instances':{}}
    raw=rig[LEDGER]
    if type(raw) is not str or len(raw)>1048576:fail('LEDGER_INVALID','Bounded string ledger required')
    try:book=json_value(raw)
    except Exception as e:fail('LEDGER_INVALID','Malformed ledger JSON',{'cause':str(e)})
    if (type(book) is not dict or set(book)!={'schema','instances'} or book['schema']!=BOOK_SCHEMA
        or type(book['instances']) is not dict or len(book['instances'])>32):fail('LEDGER_INVALID','Ledger schema/budget differs')
    for key,entry in book['instances'].items():
        if type(entry) is not dict or set(entry)!={'template','bindings','template_sha256','generation'}:fail('LEDGER_INVALID','Instance fields differ')
        template=contract.canonical_template(entry['template']);bindings=contract.canonical_bindings(entry['bindings'],template)
        contract.canonical_request({'schema':'hrig.bone-collections.apply.v1','instance_id':key,'template':template,
            'bindings':bindings,'expected_snapshot_sha256':'0'*64})
        if (template!=entry['template'] or bindings!=entry['bindings'] or contract.digest(template)!=entry['template_sha256']
            or type(entry['generation']) is not int or not 1<=entry['generation']<=1000000):fail('LEDGER_INVALID','Instance baseline/hash/generation differs')
    return book


def _ids():
    seen=set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type!='COLLECTION':continue
        for item in getattr(bpy.data,prop.identifier):
            if not isinstance(item,bpy.types.ID) or item.as_pointer() in seen:continue
            seen.add(item.as_pointer());yield item
            tree=getattr(item,'node_tree',None)
            if tree and tree.as_pointer() not in seen:seen.add(tree.as_pointer());yield tree


def _graph_path(root,path,arm):
    # Resolve the native container immediately before collections/collections_all.
    # This includes indices, bone memberships and Scene/Object/Context aliases.
    if root is None or not path:return False
    if path.endswith('.visible'):
        try:
            container=root.path_resolve(path.rpartition('.')[0])
            if isinstance(container,bpy.types.Bone) and container.id_data==arm:return True
        except (ValueError,AttributeError,KeyError):pass
    for match in re.finditer(r'(?:^|\.)collections(?:_all)?(?=\[|\.|$)',path):
        prefix=path[:match.start()]
        try:container=root.path_resolve(prefix) if prefix else root
        except (ValueError,AttributeError,KeyError):continue
        if container==arm or getattr(container,'id_data',None)==arm:
            # A currently missing collection path can become valid after create.
            return True
    return False


def consumers(arm):
    result=[]
    ids=list(_ids())
    for item in ids:
        ad=getattr(item,'animation_data',None)
        if ad is None:continue
        for curve in ad.drivers:
            if _graph_path(item,curve.data_path,arm):result.append({'kind':'driver_writer','owner':styles.identity(item),'path':curve.data_path})
            for variable in curve.driver.variables:
                for target in variable.targets:
                    root=target.id
                    if variable.type=='CONTEXT_PROP':root={'ACTIVE_SCENE':bpy.context.scene,'ACTIVE_VIEW_LAYER':bpy.context.view_layer}.get(target.context_property)
                    if _graph_path(root,target.data_path,arm):result.append({'kind':'driver_reader','owner':styles.identity(item),'path':target.data_path})
        actions=[]
        if ad.action:actions.append(ad.action)
        def visit(strips):
            for strip in strips:
                if strip.action:actions.append(strip.action)
                visit(strip.strips)
        for track in ad.nla_tracks:visit(track.strips)
        for action in actions:
            for curve in styles._action_curves(action):
                if _graph_path(item,curve.data_path,arm):result.append({'kind':'action_writer','owner':styles.identity(action),'path':curve.data_path})
    for action in bpy.data.actions:
        for curve in styles._action_curves(action):
            if not (re.search(r'(?:^|\.)collections(?:_all)?(?=\[|\.|$)',curve.data_path) or curve.data_path.endswith('.visible')):continue
            roots=[*ids,*[layer for scene in bpy.data.scenes for layer in scene.view_layers]]
            if any(_graph_path(root,curve.data_path,arm) for root in roots):
                result.append({'kind':'unbound_action_path','owner':styles.identity(action),'path':curve.data_path})
    return result


def inventory(rig):
    arm=rig.data
    return {'collections':[{'name':c.name,'parent':c.parent.name if c.parent else None,'children':[x.name for x in c.children],
            'index':c.index,'child_number':c.child_number,'visible':c.is_visible,'expanded':c.is_expanded,'solo':c.is_solo,
            'visible_ancestors':c.is_visible_ancestors,'visible_effectively':c.is_visible_effectively,
            'members':sorted(b.name for b in c.bones),'owner_raw':c.get(TAG)} for c in arm.collections_all],
        'active':arm.collections.active.name if arm.collections.active else None,
        'active_index':arm.collections.active_index,'solo_active':arm.collections.is_solo_active,
        'bone_visibility':{b.name:{'hide':b.hide,'collection_effective_any':any(c.is_visible_effectively for c in b.collections),
            'ungrouped':len(b.collections)==0,'collections':sorted(c.name for c in b.collections)} for b in arm.bones},
        'visibility_scope':'native collection effective flags and explicit bone.hide only; full UI bone visibility not certified'}


def _domain(context,rig):
    if tuple(bpy.app.version)!=(5,2,1) or context.mode!='OBJECT' or rig.mode!='OBJECT':fail('DOMAIN','Verified Blender5.2.1 Object mode required')
    if rig.type!='ARMATURE' or rig.library or rig.override_library or rig.data.library or rig.data.override_library:fail('DOMAIN','Local non-override Armature required')
    if context.scene.objects.get(rig.name)!=rig or context.view_layer.objects.get(rig.name)!=rig:fail('DOMAIN','Explicit Scene/View Layer rig required')
    users=[o for o in bpy.data.objects if o.type=='ARMATURE' and o.data==rig.data]
    if users!=[rig] or rig.data.users!=1+int(rig.data.use_fake_user):fail('SHARED_ARMATURE','Single real Armature owner required')
    found=consumers(rig.data)
    if found:fail('COLLECTION_CONSUMER','Native collection graph consumers present',{'consumers':found})


def _observe(context,rig,template,bindings):
    _domain(context,rig);book=ledger(rig);owned={}
    for col in rig.data.collections_all:
        tag=owner(col)
        if tag:
            token=(tag['instance_id'],tag['role'])
            if token in owned:fail('OWNER_INVALID','Duplicate collection ownership')
            entry=book['instances'].get(token[0])
            if entry is None or token[1] not in entry['template']['roles']:fail('OWNER_INVALID','Orphan/unknown owner tag')
            owned[token]=col
    for key,entry in book['instances'].items():
        if {r for k,r in owned if k==key}!=set(entry['template']['roles']):fail('OWNER_MISSING','Managed collection missing')
    for bone in bindings.values():
        if rig.data.bones.get(bone) is None:fail('BINDING_MISSING','Bound bone missing',{'bone':bone})
    for external_names in template['detach'].values():
        for name in external_names:
            col=rig.data.collections_all.get(name)
            if col is None or owner(col) is not None:fail('DETACH_TARGET','Existing unowned detach collection required',{'collection':name})
    snapshot={'rig':styles.identity(rig),'armature':styles.identity(rig.data),'inventory':inventory(rig),
              'ledger_present':LEDGER in rig,'ledger_raw':rig.get(LEDGER),'bindings':bindings,
              'scene':context.scene.name,'view_layer':context.view_layer.name,'frame':context.scene.frame_current,'subframe':context.scene.frame_subframe}
    return {'ok':True,'snapshot_sha256':contract.digest(snapshot),'snapshot':snapshot,'ledger':book},owned


def _error(error):return {'ok':False,'changed':False,'error':{'code':getattr(error,'code','COLLECTION_INVALID'),'message':str(error),'details':getattr(error,'details',{})}}


def inspect_collections(context,rig,template,bindings):
    try:
        template=contract.canonical_template(template);bindings=contract.canonical_bindings(bindings,template)
        observed,owned=_observe(context,rig,template,bindings)
        return observed
    except Exception as e:return _error(e)


def _mutate(kind,*args):
    if kind=='new':return args[0].collections.new(args[1],parent=args[2])
    if kind=='tag':args[0][TAG]=args[1]
    elif kind=='name':args[0].name=args[1]
    elif kind=='visible':args[0].is_visible=args[1]
    elif kind=='member':return args[0].assign(args[1]) if args[2] else args[0].unassign(args[1])
    elif kind=='ledger':args[0][LEDGER]=args[1]
    elif kind=='active':args[0].collections.active=args[1]
    else:raise ValueError('Unknown collection mutation')


def apply_collections(context,rig,request):
    backup=None
    try:
        req=contract.canonical_request(request);template=req['template'];bindings=req['bindings'];key=req['instance_id']
        observed,all_owned=_observe(context,rig,template,bindings)
        if observed['snapshot_sha256']!=req['expected_snapshot_sha256']:fail('SNAPSHOT_CONFLICT','Collection system changed')
        book=observed['ledger'];previous=book['instances'].get(key);cols={role:c for (k,role),c in all_owned.items() if k==key}
        target=contract.atoms(template,bindings);preserved=[]
        if previous:
            base=previous['template']
            if (base['template_id']!=template['template_id'] or previous['bindings']!=bindings or base['detach']!=template['detach']
                or {r:x['parent'] for r,x in base['roles'].items()}!={r:x['parent'] for r,x in template['roles'].items()}):
                fail('STRUCTURE_CHANGE','Roles/parents/bindings/detach require explicit migration')
            if template['revision']<base['revision'] or (template['revision']==base['revision'] and template!=base):fail('REVISION_CONFLICT','Downgrade/same-revision content conflict')
            current={}
            for atom in target:
                parts=json.loads(atom);kind=parts[0]
                if kind=='name':value=cols[parts[1]].name
                elif kind=='visible':value=cols[parts[1]].is_visible
                elif kind=='member':value=bindings[parts[2]] in {b.name for b in cols[parts[1]].bones}
                else:value=bindings[parts[2]] in {b.name for b in rig.data.collections_all[parts[1]].bones}
                current[atom]=value
            merged=contract.merge_atoms(contract.atoms(base,bindings),current,target)
            if merged['conflicts']:fail('MERGE_CONFLICT','Template/instance conflict',{'conflicts':merged['conflicts']})
            target=merged['values'];preserved=merged['preserved']
        names={role:target[json.dumps(['name',role],ensure_ascii=False,separators=(',',':'))] for role in template['roles']}
        for role,name in names.items():
            existing=rig.data.collections_all.get(name)
            if existing is not None and existing!=cols.get(role):fail('NAME_CONFLICT','Collection name already exists; rename swaps not supported',{'name':name})
        updated=copy.deepcopy(book)
        entry={'template':template,'bindings':bindings,'template_sha256':contract.digest(template),
               'generation':previous['generation']+(previous['template']!=template) if previous else 1}
        if len(book['instances'])+(previous is None)>32 or entry['generation']>1000000:fail('LEDGER_BUDGET','Instance/generation budget exceeded')
        updated['instances'][key]=entry
        serialized=json.dumps(updated,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if len(serialized)>1048576:fail('LEDGER_BUDGET','Ledger byte budget exceeded')
        backup={'cols':[(c,{'name':c.name,'visible':c.is_visible,'expanded':c.is_expanded,'solo':c.is_solo,
                            'members':set(b.name for b in c.bones),'parent':c.parent,'child_number':c.child_number}) for c in rig.data.collections_all],
                'active':rig.data.collections.active,'present':LEDGER in rig,'raw':rig.get(LEDGER)}
        writes=[]
        if not previous:
            pending=set(template['roles'])
            while pending:
                ready=sorted(role for role in pending if template['roles'][role]['parent'] is None or template['roles'][role]['parent'] in cols)
                if not ready:fail('STRUCTURE_CHANGE','Unresolvable parent graph')
                for role in ready:
                    spec=template['roles'][role];col=_mutate('new',rig.data,names[role],cols.get(spec['parent']))
                    if col.name!=names[role]:fail('NAME_CONFLICT','Native auto-renamed requested collection')
                    cols[role]=col
                    _mutate('tag',col,json.dumps({'schema':OWNER_SCHEMA,'instance_id':key,'role':role},sort_keys=True,separators=(',',':')))
                    writes.append({'kind':'create','role':role,'name':col.name});pending.remove(role)
        for atom,value in target.items():
            parts=json.loads(atom);kind=parts[0]
            if kind in ('name','visible'):
                col=cols[parts[1]];current=col.name if kind=='name' else col.is_visible
                if current!=value:_mutate(kind,col,value);writes.append({'kind':kind,'role':parts[1],'value':value})
            else:
                col=cols[parts[1]] if kind=='member' else rig.data.collections_all[parts[1]]
                bone=rig.data.bones[bindings[parts[2]]];current=bone.name in {b.name for b in col.bones}
                if current!=value:_mutate('member',col,bone,value);writes.append({'kind':kind,'collection':col.name,'bone':bone.name,'value':value})
        if updated!=book:_mutate('ledger',rig,serialized)
        if rig.data.collections.active!=backup['active']:_mutate('active',rig.data,backup['active'])
        if rig.data.collections.active!=backup['active']:fail('ACTIVE_CHANGED','Could not restore native active identity')
        return {'ok':True,'changed':bool(writes or updated!=book),'writes':writes,'preserved':preserved,'generation':entry['generation'],
                'instance_id':key,'owned_collections':{r:c.name for r,c in cols.items()},'inventory_after':inventory(rig)}
    except Exception as e:
        if backup is not None:
            originals={c.as_pointer() for c,s in backup['cols']}
            added=[c for c in rig.data.collections_all if c.as_pointer() not in originals]
            for c in added:c.parent=None
            for c in reversed(added):rig.data.collections.remove(c)
            for c,s in backup['cols']:
                c.name=s['name'];c.is_visible=s['visible'];c.is_expanded=s['expanded'];c.is_solo=s['solo']
                for bone in list(c.bones):
                    if bone.name not in s['members']:c.unassign(bone)
                for name in s['members']:c.assign(rig.data.bones[name])
            rig.data.collections.active=backup['active']
            if backup['present']:rig[LEDGER]=backup['raw']
            elif LEDGER in rig:del rig[LEDGER]
        return _error(e)
