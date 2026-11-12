"""Evidence-based C94 module proposal. This module never deletes source bones."""
import json
import re
from collections import Counter, defaultdict

FACE_COLLECTIONS={'面部','面部-主控','面部-次要','面部形变','表情面板'}
BODY_COLLECTIONS={'根骨骼','FK','IK','躯干控制','四肢','矫正控制','拉伸骨骼','躯干形变'}
MODULES=('core','face','hair','breast','glute','toes','shared','unclassified')


def path_bones(path):
    return [json.loads(value) for value in re.findall(r'(?:pose\.)?bones\[("(?:\\.|[^"\\])*")\]',path or '')]


def build_module_plan(inventory):
    main=inventory['main']
    rig=main['object']
    bones={b['name']:b for b in main['bones']}
    roots={'@object','@data'}
    nodes=set(bones)|roots
    object_id=main.get('object_id',{'type':'Object','name':rig,'library':None})
    data_id=main.get('data_id',{'type':'Armature','name':main['data'],'library':None})
    edges=[]
    external=[]
    unresolved=[]

    def add(owner,target,kind,detail=None):
        if not target:
            return
        row={'owner':owner,'target':target,'kind':kind}
        if detail:row['detail']=detail
        if owner not in nodes or target not in nodes:
            unresolved.append(row)
        else:edges.append(row)

    def target_edge(owner,ref,subtarget,kind,detail=None):
        if not ref:
            if subtarget:
                unresolved.append({'owner':owner,'kind':kind,'subtarget':subtarget,
                                   'detail':detail,'reason':'target ID is missing'})
            return
        if ref==object_id:
            add(owner,subtarget or '@object',kind,detail)
        elif ref==data_id:
            add(owner,subtarget or '@data',kind,detail)
        else:
            external.append({'owner':owner,'target_id':ref,'subtarget':subtarget,
                             'kind':kind,'detail':detail})

    def constraint_edges(owner,rows):
        for c in rows:
            detail=c.get('name')
            for field,subfield,kind in (('target','subtarget','constraint'),
                                        ('pole_target','pole_subtarget','pole'),
                                        ('space_object','space_subtarget','constraint_space')):
                target_edge(owner,(c.get(field) or {}).get('id'),c.get(subfield),kind,detail)
            for t in c.get('targets') or ():
                target_edge(owner,t.get('target'),t.get('subtarget'),'constraint_target',detail)
            # Other pointer properties (e.g. a custom constraint space) must not
            # disappear from the proposal even before their semantics are supported.
            for field,value in c.items():
                if field not in {'target','pole_target','space_object','targets'} and isinstance(value,dict) and value.get('id'):
                    ref=value['id']
                    if ref in (object_id,data_id):
                        unresolved.append({'owner':owner,'kind':'unsupported_constraint_pointer',
                                           'field':field,'target_id':ref,'detail':detail})
                    else:
                        external.append({'owner':owner,'target_id':ref,'subtarget':None,
                                         'kind':'unsupported_constraint_pointer','detail':detail+':'+field if detail else field})

    add('@object','@data','armature_data')
    target_edge('@object',main.get('parent'),main.get('parent_bone'),'object_parent')
    constraint_edges('@object',main.get('object_constraints',[]))
    constraint_edges('@data',main.get('data_constraints',[]))
    for name,b in bones.items():
        add(name,b['parent'],'parent')
        add(name,b['bbone_start'],'bbone_handle')
        add(name,b['bbone_end'],'bbone_handle')
        add(name,b['custom_shape_transform'],'display')
        constraint_edges(name,b['constraints'])
    for root,drivers in (('@object',main['drivers']),('@data',main['data_drivers'])):
        for d in drivers:
            owners=path_bones(d['path']) or [root]
            if not d['output_resolves']:
                unresolved.append({'owner':owners[0],'kind':'driver_output','path':d['path']})
            for owner in owners:
                for v in d['variables']:
                    for t in v['targets']:
                        targets=([t.get('bone_target')] if t.get('bone_target') else [])+path_bones(t.get('data_path'))
                        if t.get('path_resolves') is False:
                            unresolved.append({'owner':owner,'kind':'driver_input','path':t.get('data_path'),
                                               'target_id':t.get('id'),'detail':d['path']})
                        if not t.get('id'):
                            unresolved.append({'owner':owner,'kind':'driver_target','detail':d['path'],
                                               'variable':v.get('name'),'reason':'missing or context-based target ID'})
                        if not targets:
                            target_edge(owner,t.get('id'),None,'driver',d['path'])
                        for target in targets:
                            target_edge(owner,t.get('id'),target,'driver',d['path'])

    # Deduplicate repeated fcurves without modifying the native rig.
    edges=list({json.dumps(e,ensure_ascii=False,sort_keys=True):e for e in edges}.values())
    external=list({json.dumps(e,ensure_ascii=False,sort_keys=True):e for e in external}.values())
    adj=defaultdict(set)
    neighbour=defaultdict(set)
    for e in edges:
        if e['kind']!='display':
            adj[e['owner']].add(e['target'])
            neighbour[e['owner']].add(e['target'])
            neighbour[e['target']].add(e['owner'])
    seeds={m:set() for m in MODULES}
    evidence={}
    for name,b in bones.items():
        cols=set(b['collections'])
        if cols & FACE_COLLECTIONS:
            seeds['face'].add(name); evidence[name]='face collection'
        if '头发' in cols or '头发形变' in cols or any(c.startswith('HRIG-头发-') for c in cols):
            seeds['hair'].add(name); evidence[name]='hair collection'
    for e in external:
        if e['owner'] in bones and e['target_id']['name'] in {'千早爱音-前发物理','千早爱音-后发物理'}:
            seeds['hair'].add(e['owner']); evidence[e['owner']]='constraint to explicit hair proxy'
    # Names only refine an existing collection/structural relationship, never
    # authorize removal. Separate source IK controls share the general IK group.
    changed=True
    while changed:
        changed=False
        for name in bones:
            if name not in seeds['hair'] and '发' in name and neighbour[name]&seeds['hair']:
                seeds['hair'].add(name);evidence[name]='hair semantic name and native edge to hair member';changed=True
            if (name not in seeds['face'] and name.startswith('接续-') and
                    any(term in name for term in ('下巴','眉毛','眼睑','脸颊','鼻部')) and
                    neighbour[name]&seeds['face']):
                seeds['face'].add(name);evidence[name]='face connector semantic name and native edge to face member';changed=True
    for name,b in bones.items():
        cols=set(b['collections'])
        if not cols & BODY_COLLECTIONS:
            continue
        if re.match(r'^(?:形变-|机制-)?胸部(?:控制器|拉伸摇摆控制器)?\.[LR]$',name):
            seeds['breast'].add(name);evidence[name]='body collection and explicit breast role'
        elif re.match(r'^(?:形变-|机制-)?臀部(?:控制器)?\.[LR]$',name):
            seeds['glute'].add(name);evidence[name]='body collection and explicit glute role'
        elif re.search(r'(?:第[二三四五]趾|大脚趾)',name):
            seeds['toes'].add(name);evidence[name]='body collection and independent digit role'
    optional=set().union(*(seeds[m] for m in ('face','hair','breast','glute','toes')))
    for name,b in bones.items():
        if name not in optional and set(b['collections'])&BODY_COLLECTIONS:
            seeds['core'].add(name);evidence[name]='body/control collection excluding evidenced optional seeds'

    closures={}
    for module in ('core','face','hair','breast','glute','toes'):
        found=set(seeds[module])|roots;pending=list(found)
        while pending:
            n=pending.pop()
            for target in adj[n]-found:
                found.add(target);pending.append(target)
        closures[module]=found
    assignments={}
    for name in bones:
        seed_owners=[m for m in seeds if name in seeds[m]]
        owners=[m for m,s in closures.items() if name in s]
        if len(seed_owners)==1:
            module=seed_owners[0]
        elif len(seed_owners)>1:
            module='shared';evidence[name]='multiple explicit module memberships'
        elif len(owners)==1:
            module=owners[0];evidence[name]='native dependency closure of '+module
        elif len(owners)>1:
            module='shared';evidence[name]='dependency shared by '+','.join(owners)
        else:
            module='unclassified';evidence[name]='not reached by evidenced module inputs/outputs'
        assignments[name]={'module':module,'evidence':evidence[name],'required_by':owners}
    modules={m:{'bones':sorted(n for n,a in assignments.items() if a['module']==m)} for m in MODULES}
    for m,row in modules.items():
        row['bone_count']=len(row['bones'])
        row['collections']=dict(Counter(c for n in row['bones'] for c in bones[n]['collections']))
        row['seed_bones']=sorted(seeds[m])
        row['required_closure']=sorted(closures.get(m,set())&set(bones))
        row['required_nodes']=sorted(closures.get(m,set()))
    cross=[e for e in edges if assignments.get(e['owner'],{}).get('module','common_root')!=assignments.get(e['target'],{}).get('module','common_root')]
    removal={}
    for m in ('face','hair','breast','glute','toes'):
        members=set(modules[m]['bones'])
        incoming=[e for e in edges if e['target'] in members and e['owner'] not in members]
        removal[m]={'status':'proposal_only','bone_count':len(members),'incoming_edges':incoming,
                    'external_dependencies':[e for e in external if e['owner'] in members],
                    'production_consumers':True,
                    'reason':'source skin/keys/attachments and incoming dependencies must be rebuilt or explicitly mapped; no deletion executed'}
    return {'schema':'c94.module-plan.v1','source_file':inventory['source_file'],'main_rig':rig,
            'bone_count':len(bones),'modules':modules,'assignments':assignments,'dependencies':edges,
            'common_dependency_roots':sorted(roots),
            'inventory_coverage':{'data_constraints_recorded':'data_constraints' in main,
                                  'consumer_scan':'expanded' if inventory.get('consumer_scan_version')==2 else 'legacy_saved_inventory; expanded live scan not rerun'},
            'cross_module_edges':cross,'external_dependencies':external,'unresolved':unresolved,
            'optional_removal':removal,
            'dependent_rigs':[{'object':r['object'],'bone_count':len(r['bones']),
                               'role':'existing optimized hair reference; not the new simulation backend'} for r in inventory['dependent_rigs']],
            'unimplemented_module_slots':['skirt','generic_accessories'],
            'scope':'source dependency proposal; completeness of semantic attribution and safe module deletion not certified'}
