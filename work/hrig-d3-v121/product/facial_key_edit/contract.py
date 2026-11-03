"""Finite point-authoring contract, not masked-source recipe conversion."""
from copy import deepcopy
import hashlib,json,math,struct

SCHEMA='hrig.face-coordinate-edit.v1'
MAX_POINTS=8192

class EditError(ValueError):
    def __init__(self,code,detail=None):super().__init__(code);self.code=code;self.detail=detail
def require(ok,code,detail=None):
    if not ok:raise EditError(code,detail)
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()
def f32(v):
    require(type(v) in (int,float) and math.isfinite(v),'COORDINATE_NONFINITE')
    try:r=struct.unpack('<f',struct.pack('<f',v))[0]
    except (OverflowError,struct.error):raise EditError('COORDINATE_OVERFLOW')
    require(math.isfinite(r),'COORDINATE_OVERFLOW');return r
def signature(s):return type(s) is str and len(s)==64 and all(c in '0123456789abcdef' for c in s)
def validate_plan(p):
    fields={'schema','operation','object','mesh','key_id','reference','target','raw_mesh_sha256',
            'reference_sha256','target_before_sha256','region_vertices','coordinates'}
    require(type(p) is dict and set(p)==fields,'EDIT_FIELDS')
    require(p['schema']==SCHEMA and p['operation'] in ('append','edit'),'EDIT_SCHEMA')
    for field in ('object','mesh','key_id','reference','target'):
        require(type(p[field]) is str and p[field] and '\x00' not in p[field],'EDIT_NAME',field)
    require(len(p['target'].encode('utf8'))<=63 and p['target']!=p['reference'],'REFERENCE_IMMUTABLE')
    require(signature(p['raw_mesh_sha256']) and signature(p['reference_sha256']),'EDIT_SHA')
    require(p['target_before_sha256'] is None if p['operation']=='append' else signature(p['target_before_sha256']),'EDIT_TARGET_SHA')
    indices=p['region_vertices'];coords=p['coordinates']
    require(type(indices) is list and 1<=len(indices)<=MAX_POINTS and all(type(x) is int and x>=0 for x in indices)
            and len(indices)==len(set(indices)),'EDIT_REGION')
    require(type(coords) is list and len(coords)==len(indices),'EDIT_COORDINATE_COUNT')
    rows=[]
    for n,co in zip(indices,coords):
        require(type(co) is list and len(co)==3,'EDIT_COORDINATE_WIDTH');rows.append((n,[f32(v) for v in co]))
    rows.sort(key=lambda x:x[0]);out=deepcopy(p);out['region_vertices']=[r[0] for r in rows];out['coordinates']=[r[1] for r in rows]
    return out
