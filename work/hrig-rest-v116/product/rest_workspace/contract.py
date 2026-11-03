"""Pure explicit batch-rest input and native-raw write-domain contract."""
from copy import deepcopy
import hashlib,json,math,struct
SCHEMA='hrig.rest-workspace.v1'
class RestError(ValueError):
    def __init__(self,code,detail=None):self.code,self.detail=code,detail;super().__init__(code+': '+str(detail))
def need(ok,code,detail=None):
    if not ok:raise RestError(code,detail)
def digest(x):return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def f32(x):
    need(type(x) in (int,float) and math.isfinite(x),'FINITE_NUMBER',x)
    return struct.unpack('<f',struct.pack('<f',x))[0]
def vector(x):
    need(type(x) is list and len(x)==3,'VECTOR3',x);return [f32(v) for v in x]
def name(x):need(type(x) is str and bool(x) and len(x.encode())<64,'BONE_NAME',x);return x
def validate(r):
    need(type(r) is dict and set(r)=={'schema','atoms','side_pairs'} and r['schema']==SCHEMA,'REQUEST_SCHEMA')
    need(type(r['atoms']) is list and 1<=len(r['atoms'])<=16,'ATOM_BUDGET')
    seen=set()
    for a in r['atoms']:
        need(type(a) is dict and a.get('op') in ('set','create','replace'),'ATOM_TYPE',a)
        fields={'op','bone','fields'}|({'donor','parent','collections'} if a['op']=='create' else set())
        need(set(a)==fields,'ATOM_KEYS',a);name(a['bone'])
        need(type(a['fields']) is dict and bool(a['fields']) and set(a['fields'])<={'head','tail','roll','parent','use_connect'},'REST_FIELDS',a)
        for k,v in a['fields'].items():
            need((a['bone'],k) not in seen,'DUPLICATE_WRITE',(a['bone'],k));seen.add((a['bone'],k))
            if k in ('head','tail'):vector(v)
            elif k=='roll':f32(v)
            elif k=='parent':need(v is None or type(v) is str,'PARENT_NAME',v)
            else:need(type(v) is bool,'CONNECTED_BOOL',v)
        if a['op']=='create':
            name(a['donor']);need(a['parent'] is None or type(a['parent']) is str,'PARENT_NAME')
            need(set(a['fields'])=={'head','tail','roll'},'NEW_REST_DEFINITION')
            need(type(a['collections']) is list and len(a['collections'])==len(set(a['collections']))
                 and all(type(x) is str and x for x in a['collections']),'NEW_COLLECTIONS')
    need(type(r['side_pairs']) is list and len(r['side_pairs'])<=8,'SIDE_PAIRS')
    flat=[]
    for p in r['side_pairs']:
        need(type(p) is list and len(p)==2 and p[0]!=p[1],'SIDE_PAIR',p);flat.extend(map(name,p))
    need(len(flat)==len(set(flat)),'SIDE_PAIR_OVERLAP')
    return deepcopy(r)
def plan(raw,request):
    """Freeze values and RNA direct-connected endpoint/radius propagation."""
    request=validate(request);expected=deepcopy(raw);writes=set();born=[];replaced=[]
    for a in request['atoms']:
        n=a['bone'];op=a['op']
        if op=='create':
            need(n not in expected and a['donor'] in expected,'CREATE_IDENTITY',a)
            expected[n]=deepcopy(expected[a['donor']]);expected[n]['fields'].update(name=n,use_connect=False,use_deform=False,
                select=False,select_head=False,select_tail=False)
            expected[n]['parent']=a['parent'];expected[n]['collections']=sorted(a['collections'])
            expected[n]['bbone_custom_handle_start']=None;expected[n]['bbone_custom_handle_end']=None
            expected[n]['fields']['custom_properties']={};born.append(n)
        else:
            need(n in expected,'BONE_MISSING',n)
            if op=='replace':
                need(not any(v['parent']==n for v in expected.values()) and not expected[n]['fields']['use_connect'],'REPLACE_NONCONNECTED_LEAF',n)
                replaced.append(n)
                saved=expected.pop(n);expected[n]=saved
        row=expected[n]
        for k,value in a['fields'].items():
            value=vector(value) if k in ('head','tail') else f32(value) if k=='roll' else value
            if k=='parent':
                row['parent']=value
                if value is None:row['fields']['use_connect']=False
                elif row['fields']['use_connect']:row['fields']['head']=deepcopy(expected[value]['fields']['tail'])
            else:row['fields'][k]=value
            writes.add(n)
            if k=='use_connect' and value:
                need(row['parent'] in expected,'CONNECTED_PARENT',n)
                row['fields']['head']=deepcopy(expected[row['parent']]['fields']['tail'])
                need(not row['fields']['select_head'],'SELECTED_CONNECTED_UNSUPPORTED',n)
            if k=='tail':
                for child,v in expected.items():
                    if v['parent']==n and v['fields']['use_connect']:
                        v['fields']['head']=deepcopy(value);v['fields']['head_radius']=row['fields']['tail_radius'];writes.add(child)
            if k=='head' and row['parent'] and row['fields']['use_connect']:
                parent=expected[row['parent']];parent['fields']['tail']=deepcopy(value)
                parent['fields']['tail_radius']=row['fields']['head_radius'];writes.add(row['parent'])
    for n,row in expected.items():
        need(row['parent'] is None or row['parent'] in expected,'PARENT_MISSING',n)
        visited={n};p=row['parent']
        while p:
            need(p not in visited,'PARENT_CYCLE',n);visited.add(p);p=expected[p]['parent']
        need(sum((a-b)**2 for a,b in zip(row['fields']['head'],row['fields']['tail']))>1e-16,'ZERO_BONE',n)
    affected=set(writes)
    while True:
        add={n for n,v in expected.items() if v['parent'] in affected}-affected
        if not add:break
        affected|=add
    for p in request['side_pairs']:need(set(p)<=set(writes),'SIDE_PAIR_NOT_EXPLICITLY_WRITTEN',p)
    return {'request':request,'raw_before':deepcopy(raw),'raw_expected':expected,'direct':sorted(writes),
        'affected':sorted(affected),'born':born,'replaced':replaced,'source_raw_sha256':digest(raw)}
def raw_protection(actual,planned):
    expected=deepcopy(planned['raw_expected']);actual=deepcopy(actual)
    need(set(actual)==set(expected),'RAW_ROSTER')
    need(list(actual)==list(expected),'RAW_ORDER')
    for n in planned['direct']:
        for k in ('matrix','length'):
            expected[n]['fields'].pop(k,None);actual[n]['fields'].pop(k,None)
    need(actual==expected,'RAW_OUTSIDE_DECLARED_DOMAIN')
    return True
