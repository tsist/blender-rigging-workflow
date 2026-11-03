"""Independent collection graph witness and exact authorization mask."""
import copy
import json
import bpy
import inspection
LEDGER='hrig_collection_instances_v1'
TAG='hrig_collection_owner_v1'


def capture(rig):
    a=rig.data
    return {'rows':[{'name':c.name,'settings':inspection.configuration(c),'parent':c.parent.name if c.parent else None,
                    'index':c.index,'children':[x.name for x in c.children],'members':sorted(b.name for b in c.bones)} for c in a.collections_all],
        'active':a.collections.active.name if a.collections.active else None,'active_index':a.collections.active_index,
        'solo_active':a.collections.is_solo_active,'ledger':rig.get(LEDGER),
        'derived':{'collections':{c.name:{'ancestors':c.is_visible_ancestors,'effective':c.is_visible_effectively} for c in a.collections_all},
                   'bones':{b.name:{'collection_effective_any':any(c.is_visible_effectively for c in b.collections),
                       'ungrouped':len(b.collections)==0,'hide':b.hide} for b in a.bones}}}


def owned(snapshot,key):
    result={}
    for row in snapshot['rows']:
        raw=row['settings']['custom_properties'].get(TAG)
        if raw is None:continue
        tag=json.loads(raw)
        if tag['instance_id']==key:
            if tag['role'] in result:raise ValueError('Witness duplicate owner')
            result[tag['role']]=row
    return result


def _atoms(template,bindings):
    result={}
    for role,spec in template['roles'].items():
        result[('name',role)]=spec['name'];result[('visible',role)]=spec['is_visible']
        for b in bindings:result[('member',role,b)]=b in spec['members']
    for b,names in template['detach'].items():
        for name in names:result[('detach',name,b)]=False
    return result


def expected(before,request):
    t=request['template'];bindings=request['bindings'];key=request['instance_id'];rows=owned(before,key)
    book=json.loads(before['ledger']) if before['ledger'] is not None else {'schema':'hrig.bone-collections.instances.v1','instances':{}}
    prior=book['instances'].get(key);target=_atoms(t,bindings)
    if prior:
        base=_atoms(prior['template'],bindings);native={r['name']:r for r in before['rows']}
        for token,value in list(target.items()):
            kind,role,*bone=token
            if kind=='name':current=rows[role]['name']
            elif kind=='visible':current=rows[role]['settings']['is_visible']
            elif kind=='member':current=bindings[bone[0]] in rows[role]['members']
            else:current=bindings[bone[0]] in native[role]['members']
            b=base[token]
            if current==b:pass
            elif value==b:target[token]=current
            elif current==value:pass
            else:raise ValueError('Successful mutation contains an independent three-way conflict')
    return target,prior,rows,book


def validate(before,after,request,result):
    if not result['ok']:
        if before!=after:raise ValueError('Rejected collection changed native graph')
        return {'ok':True,'rejection_exact':True}
    target,prior,old_owned,book=expected(before,request);t=request['template'];bindings=request['bindings'];key=request['instance_id']
    new_owned=owned(after,key)
    if set(new_owned)!=set(t['roles']):raise ValueError('Native owner/role graph differs')
    rename={old_owned[r]['name']:new_owned[r]['name'] for r in old_owned}
    original={r['name']:r for r in before['rows']};actual={r['name']:r for r in after['rows']}
    expected_names={rename.get(name,name) for name in original}|{r['name'] for r in new_owned.values()}
    if set(actual)!=expected_names:raise ValueError('Unexpected collection creation/deletion')
    for role,row in new_owned.items():
        if row['name']!=target[('name',role)] or row['settings']['is_visible']!=target[('visible',role)]:raise ValueError('Name/visibility literal oracle differs')
        if not prior:
            tag=json.loads(row['settings']['custom_properties'][TAG])
            if tag!={'schema':'hrig.bone-collections.owner.v1','instance_id':key,'role':role}:raise ValueError('New owner tag differs')
            parent=t['roles'][role]['parent'];wanted=new_owned[parent]['name'] if parent is not None else None
            if row['parent']!=wanted:raise ValueError('New collection parent differs')
            if row['settings']['is_expanded'] is not False or row['settings']['is_solo'] is not False:raise ValueError('New collection defaults differ')
            if set(row['settings']['custom_properties'])!={TAG}:raise ValueError('Unexpected new collection metadata')
    for name,old in original.items():
        new=actual[rename.get(name,name)];a=copy.deepcopy(old['settings']);b=copy.deepcopy(new['settings'])
        role=next((r for r,x in old_owned.items() if x['name']==name),None)
        if role is not None:
            a['name']=target[('name',role)];a['is_visible']=target[('visible',role)]
        if a!=b:raise ValueError('Non-target collection settings/custom state changed')
        if new['parent']!=rename.get(old['parent'],old['parent']) or new['children']!=[rename.get(n,n) for n in old['children']]:
            raise ValueError('Existing parent/sibling hierarchy changed')
    # Exact complete membership oracle, including unbound hand-added bones.
    wanted={rename.get(name,name):set(row['members']) for name,row in original.items()}
    for row in new_owned.values():wanted.setdefault(row['name'],set())
    for token,value in target.items():
        if token[0] not in ('member','detach'):continue
        collection=new_owned[token[1]]['name'] if token[0]=='member' else token[1];bone=bindings[token[2]]
        if value:wanted[collection].add(bone)
        else:wanted[collection].discard(bone)
    if any(set(row['members'])!=wanted[name] for name,row in actual.items()):raise ValueError('Full native membership oracle differs')
    old_order=[rename.get(x['name'],x['name']) for x in before['rows']]
    if [x['name'] for x in after['rows'] if x['name'] in old_order]!=old_order:raise ValueError('Original native collection order changed')
    for index,row in enumerate(after['rows']):
        if row['index']!=index:raise ValueError('Collection index/layout inconsistency')
        siblings=[x['name'] for x in after['rows'] if x['parent']==row['parent']]
        if row['settings']['child_number']!=siblings.index(row['name']):raise ValueError('Native sibling number inconsistent')
        if row['children']!=[x['name'] for x in after['rows'] if x['parent']==row['name']]:raise ValueError('Native children/order inconsistent')
    active=rename.get(before['active'],before['active'])
    if after['active']!=active or after['active_index']!=([x['name'] for x in after['rows']].index(active) if active else -1):raise ValueError('Active collection identity/index changed')
    if after['solo_active']!=before['solo_active']:raise ValueError('Native solo state changed')
    updated=json.loads(after['ledger']);instances=copy.deepcopy(book['instances'])
    generation=prior['generation']+(prior['template']!=t) if prior else 1
    import hashlib
    digest=hashlib.sha256(json.dumps(t,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    instances[key]={'template':t,'bindings':bindings,'template_sha256':digest,'generation':generation}
    if updated!={'schema':'hrig.bone-collections.instances.v1','instances':instances}:raise ValueError('Independent exact ledger oracle differs')
    return {'ok':True,'membership_exact':True,'existing_settings_exact':True,'parents_siblings_order_active_valid':True,
            'derived_scope':'native effective visibility observed separately; bone.hide/settings and collection flags remain strict; no GUI certification'}


def protected(snapshot,rig_name,armature_name,request,native):
    result=copy.deepcopy(snapshot);role_rows=owned(native,request['instance_id']);names={x['name']:r for r,x in role_rows.items()}
    def normalized(name):return '<managed:'+names[name]+'>' if name in names else name
    obj=next(x for x in result['blocks'] if x['type']=='Object' and x['name']==rig_name and x['library'] is None)
    arm=next(x for x in result['blocks'] if x['type']=='Armature' and x['name']==armature_name and x['library'] is None)
    obj['custom_properties'].pop(LEDGER,None)
    extra=arm['character_extra']
    extra['bone_collections']=[x for x in extra['bone_collections'] if x['name'] not in names]
    for row in extra['bone_collections']:
        if row['parent'] in names:row['parent']=normalized(row['parent'])
    reverse={v:k for k,v in request['bindings'].items()}
    for name,row in extra['bone_settings'].items():
        semantic=reverse.get(name)
        excluded=set(request['template']['detach'].get(semantic,())) if semantic is not None else set()
        row['collections']=sorted(normalized(n) for n in row['collections'] if not (semantic is not None and (n in names or n in excluded)))
    system=extra['collection_system'];system['active']=normalized(system['active'])
    # Native validator above proves index/sibling graph. These are display/layout
    # observations rather than independent authoring fields.
    for key in ('active_index','layout','bone_visibility'):system.pop(key)
    return result
