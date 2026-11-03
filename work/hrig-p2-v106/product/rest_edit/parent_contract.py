"""Pure parent binding validation; native data, IDs and control proof stay external."""
from copy import deepcopy
import math
from .contract import require,digest,plan_parent,f32

FIELDS={'schema','topology_before','topology_after','source_topology_sha256','reference_topology_sha256',
    'raw_order_before','reference_order','source_pose_order','reference_pose_order',
    'raw_target_length_after','reference_target_head_tail_local','reference_target_length',
    'source_lengths_sha256','reference_lengths_sha256','authored_write','derived_fields'}

def topology(rows):
    require(type(rows) is list and bool(rows),'PARENT_TOPOLOGY_SCHEMA')
    names=[]
    for row in rows:
        require(type(row) is dict and set(row)=={'name','parent','use_connect','use_deform','bbone_segments'},'PARENT_TOPOLOGY_SCHEMA')
        require(type(row['name']) is str and bool(row['name']) and (row['parent'] is None or type(row['parent']) is str),'PARENT_TOPOLOGY_IDENTITY')
        require(type(row['use_connect']) is bool and type(row['use_deform']) is bool
            and type(row['bbone_segments']) is int and row['bbone_segments']>=1,'PARENT_TOPOLOGY_VALUES')
        names.append(row['name'])
    require(names==sorted(set(names)),'PARENT_TOPOLOGY_ROSTER')
    require(all(row['parent'] is None or row['parent'] in names and row['parent']!=row['name'] for row in rows),'PARENT_TOPOLOGY_EDGE')
    return names

def preorder(roster,rows):
    names=topology(rows)
    require(type(roster) is list and len(roster)==len(names) and len(set(roster))==len(roster) and set(roster)==set(names),'PARENT_ORDER_ROSTER')
    parents={r['name']:r['parent'] for r in rows};children={None:[]}
    for name in roster:children[name]=[]
    for name in roster:children[parents[name]].append(name)
    result=[];seen=set();stack=list(reversed(children[None]))
    while stack:
        name=stack.pop();require(name not in seen,'PARENT_ORDER_CYCLE')
        seen.add(name);result.append(name);stack.extend(reversed(children[name]))
    require(len(result)==len(names),'PARENT_ORDER_CYCLE')
    return result

def validate_binding(evidence,request):
    snapshot=evidence.get('raw_snapshot')
    plan=plan_parent(snapshot,request)
    require(evidence['plan']==plan and digest(evidence['plan'])==digest(plan),'PARENT_PLAN_BINDING')
    p=evidence.get('parent_protection')
    require(type(p) is dict and set(p)==FIELDS and p['schema']=='hrig.rest-parent-protection.v1','PARENT_PROTECTION_REQUIRED')
    before,after=p['topology_before'],p['topology_after']
    require(topology(before)==topology(after),'PARENT_TOPOLOGY_ROSTER')
    by_name={r['name']:r for r in before};target=request['bone']
    require(target in by_name and request['from_parent'] in by_name and request['to_parent'] in by_name,'PARENT_TOPOLOGY_IDENTITY')
    row=by_name[target]
    require(row['parent']==request['from_parent'] and by_name[request['from_parent']]['parent']==request['to_parent'],'PARENT_GRAPH_BINDING')
    require(row['use_connect'] is False and row['use_deform'] is True and row['bbone_segments']==1
        and not any(r['parent']==target for r in before),'PARENT_LEAF_BINDING')
    expected=deepcopy(before);next(r for r in expected if r['name']==target)['parent']=request['to_parent']
    require(after==expected and digest(after)==digest(expected),'PARENT_ONE_EDGE_REQUIRED')
    require(p['source_topology_sha256']==digest(before) and p['reference_topology_sha256']==digest(after),'PARENT_TOPOLOGY_HASH')
    order=p['raw_order_before'];new_order=preorder(order,after)
    require(preorder(order,before)==order==p['source_pose_order'],'PARENT_SOURCE_ORDER')
    require(new_order==p['reference_order']==p['reference_pose_order'],'PARENT_REFERENCE_ORDER')
    require([n for n in order if n!=target]==[n for n in new_order if n!=target],'PARENT_NON_TARGET_RELATIVE_ORDER')
    require(p['authored_write']=={'bone':target,'parent_before':request['from_parent'],'parent_after':request['to_parent']},'PARENT_AUTHORED_WRITE')
    require(p['derived_fields']==['native_target_rest','native_forest_order:armature.bones','native_forest_order:object.pose'],'PARENT_DERIVED_SCOPE')
    for key in ('raw_target_length_after','reference_target_length'):
        value=p[key];require(type(value) in (float,int) and math.isfinite(value) and value>0 and f32(value)==value,'PARENT_NATIVE_LENGTH',key)
    points=p['reference_target_head_tail_local']
    require(type(points) is list and len(points)==6 and all(type(v) in (float,int) and math.isfinite(v) and f32(v)==v for v in points),'PARENT_FINAL_ENDPOINTS')
    for key in ('source_lengths_sha256','reference_lengths_sha256'):
        value=p[key];require(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'PARENT_LENGTH_HASH',key)
    return plan
