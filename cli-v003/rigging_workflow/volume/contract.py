# SPDX-License-Identifier: GPL-3.0-or-later
import math
import time
import numpy as np


class VolumeError(RuntimeError):
    def __init__(self,code,message,statistics=None):
        self.code=code
        self.statistics=statistics or {}
        super().__init__(message)


def require(ok,code,message,statistics=None):
    if not ok:
        raise VolumeError(code,message,statistics)


def finite(value,name,lo,hi):
    require(type(value) in (int,float) and math.isfinite(value) and lo<=value<=hi,'INVALID_INPUT',name+' must be a finite bounded number')
    return float(value)


def integer(value,name,lo,hi):
    require(type(value)is int and lo<=value<=hi,'INVALID_INPUT',name+' must be a bounded integer')
    return value


def text(value,name):
    require(isinstance(value,str) and 0<len(value)<=256 and not any(ord(c)<32 for c in value),'INVALID_INPUT',name+' must be a nonempty short string')
    return value


def array(value,name,columns,kind='float',max_bytes=None):
    try:
        raw=np.asarray(value)
    except (ValueError,TypeError) as exc:
        raise VolumeError('INVALID_INPUT',name+' is not a rectangular numeric array') from exc
    require(raw.ndim==2 and raw.shape[1]==columns and raw.dtype.kind in ('iu' if kind=='index' else 'iuf'),'INVALID_INPUT',name+' has invalid shape/type')
    require(max_bytes is None or raw.size*8<=max_bytes,'RESOURCE_LIMIT',name+' normalized array alone exceeds memory budget')
    result=np.asarray(raw,dtype=np.int64 if kind=='index' else np.float64)
    require(bool(np.isfinite(result).all()),'INVALID_INPUT',name+' contains nonfinite values')
    return result


def normalize(meshes,bones,settings):
    require(isinstance(meshes,list) and 1<=len(meshes)<=64,'INVALID_INPUT','meshes needs 1..64 entries')
    require(isinstance(bones,list) and 1<=len(bones)<=1024,'INVALID_INPUT','bones needs 1..1024 entries')
    require(isinstance(settings,dict),'INVALID_INPUT','settings must be an explicit object')
    memory_bytes=finite(settings.get('max_memory_mb',512),'max_memory_mb',1,32768)*1048576
    allowed={'cell_size','cells_longest','mesh_mode','partitions','max_cells','max_memory_mb','max_iterations','tolerance',
        'seed_radius_cells','max_seed_projection_cells','seed_overrides','sample_radius_cells','max_sample_entry_cells','require_all_bones_seeded',
        'max_seconds','min_component_cells','max_ray_hits','return_domain','neighbor_count','compute_backend'}
    require(not set(settings)-allowed,'INVALID_INPUT','Unknown volume settings: '+repr(sorted(set(settings)-allowed)))
    require(not ('cell_size'in settings and 'cells_longest'in settings),'INVALID_INPUT','Choose cell_size or cells_longest')
    require(settings.get('mesh_mode','independent')=='independent','UNSUPPORTED','Only explicit independent mesh partitions are implemented; union is not silently approximated')
    neighbor_count=settings.get('neighbor_count',6)
    require(type(neighbor_count)is int and neighbor_count in (6,26),'INVALID_INPUT','neighbor_count must be the integer 6 or 26')
    backend=settings.get('compute_backend','AUTO')
    require(isinstance(backend,str) and backend in ('AUTO','CPU','CUDA'),'INVALID_INPUT',
            'compute_backend must be AUTO, CPU or CUDA')
    clean=[]
    for entry in meshes:
        require(isinstance(entry,dict) and set(entry)=={'id','vertices','triangles'},'INVALID_INPUT','Mesh fields must be id/vertices/triangles')
        ident=text(entry['id'],'mesh id')
        vertices=array(entry['vertices'],'vertices',3,max_bytes=memory_bytes)
        triangles=array(entry['triangles'],'triangles',3,'index',max_bytes=memory_bytes)
        require(len(vertices)>=4 and len(triangles)>=4,'OPEN_OR_THIN','Volume diffusion requires a closed volumetric surface')
        require(float(np.max(np.abs(vertices)))<=1e12,'NUMERICAL_RANGE','World coordinates exceed the declared CPU/BVH working range; rescale explicitly')
        span=np.ptp(vertices,axis=0)
        require(float(np.max(span))<=1e8 and float(np.max(span))>=1e-8,'NUMERICAL_RANGE','Volume extent is outside the explicit CPU/BVH working range [1e-8,1e8]; rescale explicitly')
        require(bool((triangles>=0).all()) and int(triangles.max())<len(vertices),'INVALID_INPUT','Triangle indices leave the mesh')
        clean.append({'id':ident,'vertices':vertices,'triangles':triangles})
    require(len({m['id'] for m in clean})==len(clean),'INVALID_INPUT','Distinct mesh ids required')
    bone_rows=[]
    for bone in bones:
        require(isinstance(bone,dict) and set(bone)=={'name','head','tail'},'INVALID_INPUT','Bone fields must be name/head/tail')
        name=text(bone['name'],'bone name')
        for key in ('head','tail'):
            require(isinstance(bone[key],(list,tuple,np.ndarray)) and len(bone[key])==3,'INVALID_INPUT','Bone points need 3 components')
        points=array([bone['head'],bone['tail']],'bone endpoints',3)
        require(float(np.max(np.abs(points)))<=1e12,'NUMERICAL_RANGE','Bone world coordinates exceed the declared numeric working range')
        require(float(np.linalg.norm(points[1]-points[0]))>0,'INVALID_INPUT','Bone segment has zero length')
        bone_rows.append({'name':name,'head':points[0],'tail':points[1]})
    require(len({b['name'] for b in bone_rows})==len(bone_rows),'INVALID_INPUT','Distinct bone names required')
    config={
        'neighbor_count':neighbor_count,
        'compute_backend':backend,
        'cells_longest':integer(settings.get('cells_longest',32),'cells_longest',4,512),
        'cell_size':finite(settings['cell_size'],'cell_size',1e-12,1e12) if 'cell_size'in settings else None,
        'max_cells':integer(settings.get('max_cells',250000),'max_cells',8,2000000),
        'max_memory_mb':finite(settings.get('max_memory_mb',512),'max_memory_mb',1,32768),
        'max_iterations':integer(settings.get('max_iterations',2000),'max_iterations',1,10000),
        'tolerance':finite(settings.get('tolerance',1e-6),'tolerance',1e-10,1e-2),
        'seed_radius_cells':finite(settings.get('seed_radius_cells',1.25),'seed_radius_cells',.25,4),
        'max_seed_projection_cells':finite(settings.get('max_seed_projection_cells',1.25),'max_seed_projection_cells',0,4),
        'sample_radius_cells':finite(settings.get('sample_radius_cells',2),'sample_radius_cells',.5,4),
        'max_sample_entry_cells':finite(settings.get('max_sample_entry_cells',0),'max_sample_entry_cells',0,1),
        'max_seconds':finite(settings.get('max_seconds',60),'max_seconds',.01,3600),
        'min_component_cells':integer(settings.get('min_component_cells',8),'min_component_cells',1,4096),
        'max_ray_hits':integer(settings.get('max_ray_hits',1024),'max_ray_hits',8,16384),
        'require_all_bones_seeded':settings.get('require_all_bones_seeded',True),
        'return_domain':settings.get('return_domain',False)}
    require(type(config['require_all_bones_seeded'])is bool and type(config['return_domain'])is bool,'INVALID_INPUT','Boolean settings must be actual bool values')

    seed_overrides=settings.get('seed_overrides',{})
    require(isinstance(seed_overrides,dict),'INVALID_INPUT','seed_overrides must be an explicit bone-to-settings object')
    all_bones={b['name'] for b in bone_rows}
    config['seed_overrides']={}
    for name,override in seed_overrides.items():
        require(name in all_bones and isinstance(override,dict)
                and set(override)=={'radius_cells','max_projection_cells'},
                'INVALID_INPUT','Every seed override must name a requested bone and declare both bounds')
        config['seed_overrides'][name]={
            'radius_cells':finite(override['radius_cells'],'seed override radius_cells',.25,4),
            'max_projection_cells':finite(override['max_projection_cells'],'seed override max_projection_cells',0,4),
        }
    partitions=settings.get('partitions')
    if partitions is None:
        partitions=[{'mesh_id':m['id'],'bone_names':[b['name'] for b in bone_rows]} for m in clean]
    require(isinstance(partitions,list) and len(partitions)==len(clean),'INVALID_INPUT','partitions must cover each mesh exactly once')
    seen=set();names={b['name'] for b in bone_rows};ids={m['id'] for m in clean}
    for row in partitions:
        require(isinstance(row,dict) and set(row)=={'mesh_id','bone_names'},'INVALID_INPUT','Partition needs mesh_id/bone_names')
        require(isinstance(row['mesh_id'],str) and row['mesh_id'] in ids and row['mesh_id'] not in seen,'INVALID_INPUT','Unknown or duplicate partition mesh')
        used=row['bone_names']
        require(isinstance(used,list) and used and all(isinstance(n,str) and n in names for n in used) and len(set(used))==len(used),'INVALID_INPUT','Partition bone names invalid')
        seen.add(row['mesh_id'])
    return clean,bone_rows,config,partitions


class Budget:
    _PROGRESS_MIN_INTERVAL=.1

    def __init__(self,settings,cancel_check=None,progress=None):
        require(cancel_check is None or callable(cancel_check),'INVALID_INPUT','cancel_check is a trusted internal callable, not JSON settings')
        self.started=time.perf_counter();self.seconds=settings['max_seconds'];self.cancel_check=cancel_check
        # Public solve() validates progress once. This internal budget only
        # invokes the already-trusted callback and throttles repeated reports.
        self.progress=progress;self.callback_raised=False
        self._reported_phase=None;self._reported_at=float('-inf')

    def check(self):
        if self.cancel_check is not None and self.cancel_check():
            raise VolumeError('CANCELLED','Volume solve cancelled; no weights accepted')
        require(time.perf_counter()-self.started<=self.seconds,'TIME_BUDGET','Volume solve exceeded its explicit time budget')

    def report(self,phase,**details):
        """Emit one small pure-JSON progress event, throttled per phase.

        Phase transitions and a terminal done==total count are never dropped.
        Callback exceptions propagate unchanged; callback_raised tells solve()
        not to wrap them as solver input/numeric failures.
        """
        if self.progress is None:return
        now=time.perf_counter()
        terminal=('done'in details and 'total'in details
                  and details['done']==details['total'])
        if (phase==self._reported_phase
                and now-self._reported_at<self._PROGRESS_MIN_INTERVAL
                and not terminal):
            return
        self._reported_phase=phase;self._reported_at=now
        try:
            self.progress(phase,**details)
        except BaseException:
            self.callback_raised=True
            raise
