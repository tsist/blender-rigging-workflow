"""Explicit finite Roll or local endpoint intent; no bpy/profile authority."""
from copy import deepcopy
import hashlib,json,math,struct

class RestEditError(RuntimeError):
    def __init__(self,code,detail=''):
        self.code,self.detail=code,deepcopy(detail);super().__init__(code+': '+str(detail))
def require(value,code,detail=''):
    if not value:raise RestEditError(code,detail)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()
def f32(value):
    try:out=struct.unpack('<f',struct.pack('<f',value))[0]
    except (ValueError,OverflowError,TypeError,struct.error) as e:raise RestEditError('F32_RANGE',str(e)) from e
    require(math.isfinite(out),'F32_RANGE');return out
def finite(value,label):
    require(type(value) in (float,int) and math.isfinite(value),'FINITE_NUMBER',label);return float(value)
def validate_request(request):
    if type(request) is dict and request.get('schema')=='hrig.rest-parent-change.v1':
        require(set(request)=={'schema','bone','from_parent','to_parent','raw_policy','mirror_policy'},'REQUEST_SCHEMA')
        for key in ('bone','from_parent','to_parent'):_bone_name(request[key])
        require(request['from_parent']!=request['to_parent'],'PARENT_NO_STORED_EFFECT')
        require(request['bone'] not in (request['from_parent'],request['to_parent']),'PARENT_SELF')
        require(request['raw_policy']=='preserve_head_tail_roll_connect','PARENT_RAW_POLICY')
        require(request['mirror_policy']=='explicit_only','MIRROR_POLICY')
        return deepcopy(request)
    if type(request) is dict and request.get('schema')=='hrig.rest-head-delta.v1':
        require(set(request)=={'schema','bone','space','delta','tail_policy','roll_policy','mirror_policy'},'REQUEST_SCHEMA')
        _bone_name(request['bone'])
        require(request['space']=='ARMATURE_LOCAL','HEAD_SPACE')
        require(request['tail_policy']=='preserve' and request['roll_policy']=='preserve','HEAD_PRESERVE_POLICY')
        require(request['mirror_policy']=='explicit_only','MIRROR_POLICY')
        delta=_vector(request['delta'],'delta')
        require(any(v!=0 for v in delta),'ZERO_DELTA')
        return deepcopy(request)
    if type(request) is dict and request.get('schema')=='hrig.rest-tail-delta.v1':
        require(set(request)=={'schema','bone','space','delta','head_policy','roll_policy','mirror_policy'},'REQUEST_SCHEMA')
        _bone_name(request['bone'])
        require(request['space']=='ARMATURE_LOCAL','TAIL_SPACE')
        require(request['head_policy']=='preserve' and request['roll_policy']=='preserve','TAIL_PRESERVE_POLICY')
        require(request['mirror_policy']=='explicit_only','MIRROR_POLICY')
        delta=_vector(request['delta'],'delta')
        require(any(v!=0 for v in delta),'ZERO_DELTA')
        return deepcopy(request)
    require(type(request) is dict and set(request)=={'schema','bone','delta','mirror_policy'},'REQUEST_SCHEMA')
    require(request['schema']=='hrig.rest-roll-delta.v1','REQUEST_VERSION')
    require(type(request['bone']) is str and bool(request['bone']) and len(request['bone'].encode('utf8'))<=255,'BONE_NAME')
    delta=finite(request['delta'],'delta');require(delta!=0,'ZERO_DELTA')
    require(request['mirror_policy']=='explicit_only','MIRROR_POLICY')
    return deepcopy(request)
def plan_roll(snapshot,request):
    request=validate_request(request)
    require(request['schema']=='hrig.rest-roll-delta.v1','ROLL_REQUEST_REQUIRED')
    require(type(snapshot) is dict and set(snapshot)=={'schema','bone','raw_roll'},'SNAPSHOT_SCHEMA')
    require(snapshot['schema']=='hrig.rest-roll-snapshot.v1' and snapshot['bone']==request['bone'],'SNAPSHOT_BINDING')
    before=finite(snapshot['raw_roll'],'raw_roll');require(f32(before)==before,'SNAPSHOT_NOT_NATIVE_F32')
    after=f32(before+request['delta']);require(after!=before,'NO_STORED_EFFECT')
    return {'schema':'hrig.rest-roll-plan.v1','request':request,'bone':request['bone'],'raw_before':before,'raw_after':after,
            'stored_delta':after-before,'snapshot_sha256':digest(snapshot),'units':'radians',
            'qualification':'explicit local EditBone roll intent; native forward, consumer and source guards are required'}

def _bone_name(name):
    require(type(name) is str and bool(name) and len(name.encode('utf8'))<=255,'BONE_NAME')
def _vector(value,label,native=False):
    require(type(value) is list and len(value)==3,'VECTOR3',label)
    result=[finite(v,label) for v in value]
    if native:require(all(f32(v)==v for v in result),'SNAPSHOT_NOT_NATIVE_F32',label)
    return result
def operation(request):
    request=validate_request(request)
    return {'hrig.rest-roll-delta.v1':'roll','hrig.rest-tail-delta.v1':'tail','hrig.rest-head-delta.v1':'head',
            'hrig.rest-parent-change.v1':'parent'}[request['schema']]
def plan_tail(snapshot,request):
    request=validate_request(request)
    require(request['schema']=='hrig.rest-tail-delta.v1','TAIL_REQUEST_REQUIRED')
    require(type(snapshot) is dict and set(snapshot)=={'schema','bone','head','tail','raw_roll','parent','use_connect'},'SNAPSHOT_SCHEMA')
    require(snapshot['schema']=='hrig.rest-tail-snapshot.v1' and snapshot['bone']==request['bone'],'SNAPSHOT_BINDING')
    head=_vector(snapshot['head'],'head',native=True);before=_vector(snapshot['tail'],'tail',native=True)
    roll=finite(snapshot['raw_roll'],'raw_roll');require(f32(roll)==roll,'SNAPSHOT_NOT_NATIVE_F32','raw_roll')
    require(snapshot['parent'] is None or (type(snapshot['parent']) is str and bool(snapshot['parent'])),'PARENT_IDENTITY')
    require(snapshot['use_connect'] is False,'CONNECTED_BONE')
    after=[f32(a+b) for a,b in zip(before,request['delta'])]
    require(after!=before,'NO_STORED_EFFECT')
    d=[t-h for t,h in zip(before,head)];d2=[t-h for t,h in zip(after,head)]
    n,n2=math.hypot(*d),math.hypot(*d2)
    require(n>0 and n2>0,'TAIL_ZERO_LENGTH')
    cross=[d[1]*d2[2]-d[2]*d2[1],d[2]*d2[0]-d[0]*d2[2],d[0]*d2[1]-d[1]*d2[0]]
    sine=math.hypot(*cross)/(n*n2);minimum=64*2**-23
    require(math.isfinite(sine) and sine>minimum,'TAIL_COLLINEAR_OR_DEGENERATE',{'sine':sine,'minimum':minimum})
    return {'schema':'hrig.rest-tail-plan.v1','request':request,'bone':request['bone'],
        'raw_before':before,'raw_after':after,'stored_delta':[a-b for a,b in zip(after,before)],
        'snapshot_sha256':digest(snapshot),'units':'armature_local',
        'protected':{'head':head,'raw_roll':roll,'parent':snapshot['parent'],'use_connect':False},
        'sine_angle':sine,'minimum_sine':minimum,
        'qualification':'explicit one-sided native f32 tail intent; independent native forward and full guards required'}

def plan_head(snapshot,request):
    request=validate_request(request)
    require(request['schema']=='hrig.rest-head-delta.v1','HEAD_REQUEST_REQUIRED')
    require(type(snapshot) is dict and set(snapshot)=={'schema','bone','head','tail','raw_roll','parent','use_connect'},'SNAPSHOT_SCHEMA')
    require(snapshot['schema']=='hrig.rest-head-snapshot.v1' and snapshot['bone']==request['bone'],'SNAPSHOT_BINDING')
    before=_vector(snapshot['head'],'head',native=True);tail=_vector(snapshot['tail'],'tail',native=True)
    roll=finite(snapshot['raw_roll'],'raw_roll');require(f32(roll)==roll,'SNAPSHOT_NOT_NATIVE_F32','raw_roll')
    require(snapshot['parent'] is None or (type(snapshot['parent']) is str and bool(snapshot['parent'])),'PARENT_IDENTITY')
    require(snapshot['use_connect'] is False,'CONNECTED_BONE')
    after=[f32(a+b) for a,b in zip(before,request['delta'])]
    require(after!=before,'NO_STORED_EFFECT')
    d=[t-h for t,h in zip(tail,before)];d2=[t-h for t,h in zip(tail,after)]
    n,n2=math.hypot(*d),math.hypot(*d2)
    require(n>0 and n2>0,'HEAD_ZERO_LENGTH')
    cross=[d[1]*d2[2]-d[2]*d2[1],d[2]*d2[0]-d[0]*d2[2],d[0]*d2[1]-d[1]*d2[0]]
    sine=math.hypot(*cross)/(n*n2);minimum=64*2**-23
    require(math.isfinite(sine) and sine>minimum,'HEAD_COLLINEAR_OR_DEGENERATE',{'sine':sine,'minimum':minimum})
    return {'schema':'hrig.rest-head-plan.v1','request':request,'bone':request['bone'],
        'raw_before':before,'raw_after':after,'stored_delta':[a-b for a,b in zip(after,before)],
        'snapshot_sha256':digest(snapshot),'units':'armature_local',
        'protected':{'tail':tail,'raw_roll':roll,'parent':snapshot['parent'],'use_connect':False},
        'sine_angle':sine,'minimum_sine':minimum,
        'qualification':'explicit one-sided native f32 head intent; independent native forward and full guards required'}

def plan_parent(snapshot,request):
    request=validate_request(request)
    require(operation(request)=='parent','PARENT_REQUEST_REQUIRED')
    require(type(snapshot) is dict and set(snapshot)=={'schema','bone','head','tail','raw_roll','parent',
        'use_connect','grandparent','grandparent_selection'},'SNAPSHOT_SCHEMA')
    require(snapshot['schema']=='hrig.rest-parent-snapshot.v1' and snapshot['bone']==request['bone'],'SNAPSHOT_BINDING')
    head=_vector(snapshot['head'],'head',native=True);tail=_vector(snapshot['tail'],'tail',native=True)
    roll=finite(snapshot['raw_roll'],'raw_roll');require(f32(roll)==roll,'SNAPSHOT_NOT_NATIVE_F32','raw_roll')
    require(snapshot['parent']==request['from_parent'],'PARENT_FROM_CHANGED')
    require(snapshot['grandparent']==request['to_parent'],'PARENT_GRANDPARENT_ONLY')
    require(snapshot['use_connect'] is False,'CONNECTED_BONE')
    flags=snapshot['grandparent_selection']
    require(type(flags) is dict and set(flags)=={'select','select_head','select_tail'}
        and all(value is False for value in flags.values()),'PARENT_SELECTION_UNSUPPORTED')
    require(math.hypot(*(t-h for h,t in zip(head,tail)))>0,'PARENT_ZERO_LENGTH')
    return {'schema':'hrig.rest-parent-plan.v1','request':request,'bone':request['bone'],
        'raw_before':snapshot['parent'],'raw_after':snapshot['grandparent'],'snapshot_sha256':digest(snapshot),
        'units':'parent_reference','protected':{'head':head,'tail':tail,'raw_roll':roll,'use_connect':False,
            'grandparent_selection':deepcopy(flags)},
        'qualification':'explicit nonconnected leaf to existing grandparent; complete native reference, graph/order and source guards required'}
