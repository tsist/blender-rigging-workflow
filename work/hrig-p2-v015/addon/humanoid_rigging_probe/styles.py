"""Bounded display-style instances. No rest, constraints, weights or widget edits."""
import copy
import json
import re
import unicodedata
import bpy
from . import style_contract as contract

LEDGER='hrig_style_instances_v1'
LEDGER_SCHEMA='hrig.control-style.instances.v1'


def identity(item):
    if item is None:return None
    return {'type':item.bl_rna.identifier,'name':item.name_full,
            'library':bpy.path.abspath(item.library.filepath) if item.library else None}


def _fail(code,message,details=None):
    error=contract.StyleError(code,message,details or {})
    raise error


def _json(raw):
    def pairs(rows):
        value={}
        for key,item in rows:
            if key in value:_fail('LEDGER_INVALID','Duplicate ledger key')
            value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:_fail('LEDGER_INVALID','Nonfinite ledger'))


def ledger(rig):
    if LEDGER not in rig:return {'schema':LEDGER_SCHEMA,'instances':{}}
    raw=rig[LEDGER]
    if type(raw) is not str or len(raw)>262144:_fail('LEDGER_INVALID','Ledger must be a bounded JSON string')
    value=_json(raw)
    if type(value) is not dict or set(value)!={'schema','instances'} or value['schema']!=LEDGER_SCHEMA:
        _fail('LEDGER_INVALID','Ledger schema/fields differ')
    instances=value['instances']
    if type(instances) is not dict or len(instances)>64:_fail('LEDGER_INVALID','Bounded instance map required')
    owned=set()
    for key,item in instances.items():
        if type(key) is not str or not key.strip() or len(key)>128 or any(unicodedata.category(c).startswith('C') for c in key):_fail('LEDGER_INVALID','Invalid instance ID')
        if type(item) is not dict or set(item)!={'template','bindings','template_sha256','generation'}:
            _fail('LEDGER_INVALID','Instance fields differ')
        template=contract.canonical_template(item['template'])
        bindings=contract.canonical_bindings(item['bindings'],template)
        if template!=item['template'] or bindings!=item['bindings'] or contract.digest(template)!=item['template_sha256']:
            _fail('LEDGER_INVALID','Instance canonical template or hash differs')
        if type(item['generation']) is not int or not 1<=item['generation']<=1000000:_fail('LEDGER_INVALID','Invalid generation')
        for role,info in template['roles'].items():
            for field in info['fields']:
                token=(bindings[role],field)
                if token in owned:_fail('LEDGER_INVALID','Instances overlap')
                owned.add(token)
    return value


def _owner(rig,bone,field):
    path=field.split('.')
    obj=rig.pose.bones[bone] if path[0]=='pose' else rig.data.bones[bone]
    for part in path[1:-1]:obj=getattr(obj,part)
    return obj,path[-1]


def _read_field(rig,bone,field):
    owner,name=_owner(rig,bone,field)
    value=getattr(owner,name)
    return list(value) if not isinstance(value,(str,bool,int,float)) else value


def _write_field(rig,bone,field,value):
    owner,name=_owner(rig,bone,field)
    setattr(owner,name,value)


def _paths(rig,template,bindings):
    result=[]
    for role,info in template['roles'].items():
        for field in info['fields']:
            parts=field.split('.')
            anchor=rig.pose.bones[bindings[role]] if parts[0]=='pose' else rig.data.bones[bindings[role]]
            path=anchor.path_from_id()+'.'+'.'.join(parts[1:])
            anchor.id_data.path_resolve(path)  # ThemeBoneColorSet cannot create its own path.
            result.append((anchor.id_data,path,role,field))
    return result


def _intersects(a,b):
    # Parent aggregate reads are conservatively treated as consumers too.
    return a==b or a.startswith(b+'.') or a.startswith(b+'[') or b.startswith(a+'.') or b.startswith(a+'[')


def _action_curves(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip,'channelbags',()):
                yield from bag.fcurves
    if getattr(action,'is_action_legacy',False):
        yield from getattr(action,'fcurves',())


def _resolved_property(root,path):
    """Resolve native aliases to the RNA container/property, never eval strings."""
    if root is None or not path:return None
    path=re.sub(r'\[-?\d+\]$','',path)
    parent,separator,leaf=path.rpartition('.')
    try:
        root.path_resolve(path)
        owner=root.path_resolve(parent) if separator else root
        if not hasattr(owner,'as_pointer'):return None
        return owner.as_pointer(),leaf if separator else path
    except (ValueError,AttributeError,KeyError):return None


def _consumers(paths):
    # Inspect all Main IDs, including embedded node trees, not only the rig.
    ids=[]
    seen=set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type!='COLLECTION':continue
        for item in getattr(bpy.data,prop.identifier):
            if isinstance(item,bpy.types.ID) and item.as_pointer() not in seen:
                ids.append(item);seen.add(item.as_pointer())
                tree=getattr(item,'node_tree',None)
                if tree is not None and tree.as_pointer() not in seen:ids.append(tree);seen.add(tree.as_pointer())
    result=[]
    selected={(target.path_resolve(path.rpartition('.')[0]).as_pointer(),path.rpartition('.')[2]):(role,field)
              for target,path,role,field in paths}
    def resolved_hit(root,path,kind,owner):
        pair=_resolved_property(root,path)
        if pair in selected:
            role,field=selected[pair]
            result.append({'kind':kind,'owner':identity(owner),'path':path,'role':role,'field':field})
    for owner in ids:
        ad=getattr(owner,'animation_data',None)
        if ad is None:continue
        for curve in ad.drivers:
            resolved_hit(owner,curve.data_path,'driver_writer_alias',owner)
            for target,path,role,field in paths:
                if owner==target and _intersects(curve.data_path,path):
                    result.append({'kind':'driver_writer','owner':identity(owner),'path':curve.data_path,'role':role,'field':field})
            for variable in curve.driver.variables:
                for slot in variable.targets:
                    root=slot.id
                    if variable.type=='CONTEXT_PROP':
                        roots={'ACTIVE_SCENE':bpy.context.scene,'ACTIVE_VIEW_LAYER':bpy.context.view_layer}
                        root=roots.get(slot.context_property)
                    resolved_hit(root,slot.data_path,'driver_reader_alias',owner)
                    for target,path,role,field in paths:
                        if slot.id==target and slot.data_path and _intersects(slot.data_path,path):
                            result.append({'kind':'driver_reader','owner':identity(owner),'path':slot.data_path,'role':role,'field':field})
        actions=[]
        if ad.action:actions.append(ad.action)
        def strips(items):
            for strip in items:
                if strip.action:actions.append(strip.action)
                strips(strip.strips)
        for track in ad.nla_tracks:strips(track.strips)
        for action in actions:
            for curve in _action_curves(action):resolved_hit(owner,curve.data_path,'action_writer_alias',owner)
    # Conservative: even currently unbound Actions can later write these paths.
    for action in bpy.data.actions:
        for curve in _action_curves(action):
            for target,path,role,field in paths:
                if _intersects(curve.data_path,path):
                    result.append({'kind':'action_path','owner':identity(action),'path':curve.data_path,'role':role,'field':field})
    return result


def _domain(context,rig,template,bindings):
    if tuple(bpy.app.version)!=(5,2,1):_fail('DOMAIN','Requires verified Blender 5.2.1')
    if context.mode!='OBJECT' or rig.mode!='OBJECT':_fail('DOMAIN','Object mode required')
    if rig.type!='ARMATURE' or rig.library or rig.override_library or rig.data.library or rig.data.override_library:
        _fail('DOMAIN','Local non-override armature required')
    if context.scene.objects.get(rig.name)!=rig or context.view_layer.objects.get(rig.name)!=rig:
        _fail('DOMAIN','Rig must belong to explicit scene/view layer')
    users=[o for o in bpy.data.objects if o.type=='ARMATURE' and o.data==rig.data]
    if users!=[rig] or rig.data.users!=1+int(rig.data.use_fake_user):_fail('SHARED_ARMATURE','Single rig/data owner required')
    for bone in bindings.values():
        if rig.pose.bones.get(bone) is None:_fail('BINDING_MISSING','Bound bone missing',{'bone':bone})
    consumers=_consumers(_paths(rig,template,bindings))
    if consumers:_fail('STYLE_CONSUMER','Selected fields have native animation/driver consumers',{'consumers':consumers})


def _observe(context,rig,template,bindings):
    book=ledger(rig)
    _domain(context,rig,template,bindings)
    fields={role:{field:_read_field(rig,bone,field) for field in template['roles'][role]['fields']}
            for role,bone in bindings.items()}
    refs={role:{'custom_shape':identity(rig.pose.bones[bone].custom_shape),
                'custom_shape_transform':{'rig':identity(rig.pose.bones[bone].custom_shape_transform.id_data),
                    'bone':rig.pose.bones[bone].custom_shape_transform.name} if rig.pose.bones[bone].custom_shape_transform else None}
            for role,bone in bindings.items()}
    snapshot={'rig':identity(rig),'armature':identity(rig.data),'bindings':bindings,'fields':fields,'references':refs,
              'ledger_present':LEDGER in rig,'ledger_raw':rig.get(LEDGER),
              'scene':context.scene.name,'view_layer':context.view_layer.name,
              'frame':context.scene.frame_current,'subframe':context.scene.frame_subframe}
    return {'ok':True,'fields':fields,'references':refs,'ledger':book,'snapshot':snapshot,
            'snapshot_sha256':contract.digest(snapshot)}


def _error(error):
    return {'ok':False,'error':{'code':getattr(error,'code','STYLE_INVALID'),
            'message':str(error),'details':getattr(error,'details',{})},'changed':False}


def inspect_styles(context,rig,template,bindings):
    try:
        template=contract.canonical_template(template)
        bindings=contract.canonical_bindings(bindings,template)
        return _observe(context,rig,template,bindings)
    except Exception as error:return _error(error)


def apply_style(context,rig,request):
    backup=None
    try:
        request=contract.canonical_request(request)
        template,bindings=request['template'],request['bindings']
        observed=_observe(context,rig,template,bindings)
        if observed['snapshot_sha256']!=request['expected_snapshot_sha256']:_fail('SNAPSHOT_CONFLICT','Selected fields/references/ledger changed')
        book=observed['ledger'];instances=book['instances'];key=request['instance_id']
        previous=instances.get(key)
        for other,entry in instances.items():
            if other==key:continue
            owned={(entry['bindings'][role],field) for role,info in entry['template']['roles'].items() for field in info['fields']}
            chosen={(bindings[role],field) for role,info in template['roles'].items() for field in info['fields']}
            if owned&chosen:_fail('INSTANCE_OVERLAP','Other instance owns selected fields',{'instance_id':other})
        target={role:info['fields'] for role,info in template['roles'].items()}
        current=observed['fields'];preserved=[]
        if previous:
            base_template=previous['template']
            if previous['bindings']!=bindings or base_template['template_id']!=template['template_id']:
                _fail('STRUCTURE_CHANGE','Rebinding/template replacement requires explicit migration')
            if {r:set(x['fields']) for r,x in base_template['roles'].items()}!={r:set(x['fields']) for r,x in template['roles'].items()}:
                _fail('STRUCTURE_CHANGE','Role/field-set change requires explicit migration')
            if template['revision']<base_template['revision']:_fail('REVISION_CONFLICT','Template downgrade rejected')
            if template['revision']==base_template['revision'] and template!=base_template:_fail('REVISION_CONFLICT','Same revision has different content')
            merged=contract.merge_fields({r:x['fields'] for r,x in base_template['roles'].items()},current,target)
            if merged['conflicts']:_fail('MERGE_CONFLICT','Template and instance changed the same field',{'conflicts':merged['conflicts']})
            target=merged['values'];preserved=merged['preserved']
        updated=copy.deepcopy(book)
        entry={'template':template,'bindings':bindings,'template_sha256':contract.digest(template),
               'generation':previous['generation']+(template!=previous['template']) if previous else 1}
        updated['instances'][key]=entry
        if len(updated['instances'])>64 or entry['generation']>1000000:
            _fail('LEDGER_BUDGET','Instance/generation limit exceeded')
        serialized=json.dumps(updated,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        if len(serialized)>262144:_fail('LEDGER_INVALID','Ledger size budget exceeded')
        writes=[(bindings[r],f,v) for r,fs in target.items() for f,v in fs.items() if current[r][f]!=v]
        # Keep original string byte-for-byte when the logical instance is unchanged.
        book_changed=updated!=book
        backup={'present':LEDGER in rig,'raw':rig.get(LEDGER),
                'fields':[(b,f,_read_field(rig,b,f)) for b,f,v in writes]}
        for bone,field,value in writes:_write_field(rig,bone,field,value)
        if book_changed:rig[LEDGER]=serialized
        for role,fs in target.items():
            for field,value in fs.items():
                actual=_read_field(rig,bindings[role],field)
                if actual!=value:_fail('WRITE_MISMATCH','Native field did not store canonical value',{'role':role,'field':field,'expected':value,'actual':actual})
        return {'ok':True,'changed':bool(writes or book_changed),'writes':[{'bone':b,'field':f,'value':v} for b,f,v in writes],
                'preserved':preserved,'generation':entry['generation'],'instance_id':key}
    except Exception as error:
        if backup is not None:
            # Restoration deliberately bypasses the injectable writer seam.
            for bone,field,value in reversed(backup['fields']):
                owner,name=_owner(rig,bone,field);setattr(owner,name,value)
            if backup['present']:rig[LEDGER]=backup['raw']
            elif LEDGER in rig:del rig[LEDGER]
            for bone,field,value in backup['fields']:
                if _read_field(rig,bone,field)!=value:raise RuntimeError('Style rollback failed') from error
        return _error(error)
