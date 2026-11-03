"""Finite garment point correction: one declared local delta in every Key.

Edits only selected garment Mesh points and corresponding points in ALL Keys;
never edits the body, weights, modifiers, UV, drivers, values or context. Raw
coordinates outside the selection are independently bit exact. Restoration
requires a complete planned-before/planned-after coordinate state; unknown
partial native writes are retained for review rather than overwritten.
"""
from copy import deepcopy
import math
import uuid

import numpy as np
from mathutils import Vector

from .contract import require, numeric, f32, GarmentError
from .native import C, error_record


class GarmentGeometryTransaction:
    def __init__(self,session):
        self.session=session
        self._snapshot=None;self._pending=None
        require(session.mesh.shape_keys is not None and C.key_owner(session.mesh.shape_keys)==session.mesh,
                'GEOMETRY_KEY_OWNER',session.target.name)

    @property
    def pending(self):
        return deepcopy({k:self._pending[k] for k in ('token','phase')}) if self._pending else None

    def _coordinates(self):
        s=self.session
        mesh=np.empty(len(s.mesh.vertices)*3,dtype='<f4');s.mesh.vertices.foreach_get('co',mesh)
        keys=[]
        for key in s.mesh.shape_keys.key_blocks:
            value=np.empty(len(key.data)*3,dtype='<f4');key.data.foreach_get('co',value)
            keys.append(value.reshape(-1,3))
        result={'mesh':mesh.reshape(-1,3),'keys':np.asarray(keys,dtype='<f4'),
                'names':tuple(k.name for k in s.mesh.shape_keys.key_blocks)}
        require(bool(np.isfinite(result['mesh']).all()) and bool(np.isfinite(result['keys']).all()),
                'GEOMETRY_NONFINITE_COORDINATES',s.target.name)
        return result

    def _coord_equal(self,left,right):
        return left['names']==right['names'] and all(left[k].dtype==right[k].dtype and left[k].shape==right[k].shape
                and left[k].tobytes()==right[k].tobytes() for k in ('mesh','keys'))

    def _projection(self,author):
        result=deepcopy(author);s=self.session;count=0
        for block in result['blocks']:
            if block['type']=='Mesh' and block['name']==s.mesh.name and block['library'] is None:
                hashes=block['mesh']['hashes'];hashes.pop('positions')
                for key in s.mesh.shape_keys.key_blocks:
                    hashes.pop('shape:'+key.name)
                # These are deterministic observations of the declared point
                # geometry, not independently authored values. Full coordinate
                # and topology oracles supply their causes; report them openly.
                block['mesh']['diagnostics'].pop('zero_area_faces')
                block['mesh']['diagnostics'].pop('zero_length_edges')
                count+=1
        require(count==1,'GEOMETRY_OBSERVER_TARGET',s.mesh.name)
        return result

    def _collect(self):
        s=self.session
        state=s._collect();state['coordinates']=self._coordinates()
        return state

    def _protected(self,current):
        before=self._snapshot['before'];s=self.session
        checks={'author_projection':self._projection(current['author'])==self._snapshot['protected'],
                'raw_all_weights':current['rows']==before['rows'],'context':current['context']==before['context'],
                'body_skin':current['body_evaluated']==before['body_evaluated'],'bones':current['bones']==before['bones'],
                'material_slots':current['material_slots']==before['material_slots'],
                'garment_topology':current['garment_evaluated']['count']==before['garment_evaluated']['count']}
        require(all(checks.values()),'GEOMETRY_NONPOINT_STATE_CHANGED',checks)

    def _exact(self,current,expected,code):
        self.session._exact({k:v for k,v in current.items() if k!='coordinates'},
                            {k:v for k,v in expected.items() if k!='coordinates'},code)
        require(self._coord_equal(current['coordinates'],expected['coordinates']),code,{'coordinate_bytes_exact':False})

    def inspect(self,request):
        s=self.session;s._guard()
        require(self._pending is None and s.pending is None,'GEOMETRY_OTHER_PENDING',self.pending)
        require(type(request) is dict and set(request)=={'schema','object','space','max_displacement','deltas'}
                and request['schema']=='hrig.garment-key-sync-geometry.v1'
                and request['object']==s.target.name and request['space']=='WORLD','GEOMETRY_REQUEST',request)
        maximum=numeric(request['max_displacement'],'GEOMETRY_DISPLACEMENT_BUDGET',low=1e-12,high=1e6)
        rows=request['deltas']
        require(type(rows) is list and 1<=len(rows)<=len(s.mesh.vertices),'GEOMETRY_DELTA_COUNT',len(rows))
        seen=set();converted=[];inverse=s.target.matrix_world.to_3x3().inverted()
        for item in rows:
            require(type(item) is dict and set(item)=={'vertex','delta'},'GEOMETRY_DELTA_SCHEMA',item)
            index=item['vertex'];values=item['delta']
            require(type(index) is int and 0<=index<len(s.mesh.vertices) and index not in seen,'GEOMETRY_VERTEX_DOMAIN',index)
            require(type(values) is list and len(values)==3,'GEOMETRY_DELTA_VECTOR',item)
            values=[numeric(v,'GEOMETRY_DELTA_FINITE',low=-maximum,high=maximum) for v in values]
            magnitude=math.sqrt(math.fsum(v*v for v in values))
            require(magnitude<=maximum,'GEOMETRY_DISPLACEMENT_OVER_BUDGET',{'vertex':index,'distance':magnitude,'maximum':maximum})
            seen.add(index)
            local=[f32(float(v)) for v in inverse@Vector(values)]
            converted.append({'vertex':index,'local_delta':local,'world_delta':values})
        before=self._collect();expected=deepcopy(before['coordinates'])
        for item in converted:
            index=item['vertex'];delta=np.asarray(item['local_delta'],dtype=np.float64)
            expected['mesh'][index]=np.asarray(before['coordinates']['mesh'][index].astype(np.float64)+delta,dtype='<f4')
            expected['keys'][:,index,:]=np.asarray(before['coordinates']['keys'][:,index,:].astype(np.float64)+delta,dtype='<f4')
        require(bool(np.isfinite(expected['mesh']).all()) and bool(np.isfinite(expected['keys']).all()),'GEOMETRY_EXPECTED_NONFINITE',None)
        token='geometry_plan_'+uuid.uuid4().hex
        self._snapshot={'token':token,'request':deepcopy(request),'before':before,'expected':expected,
                        'converted':converted,'protected':self._projection(before['author'])}
        return {'schema':'hrig.garment-geometry-inspection.v1','snapshot_token':token,'request':deepcopy(request),
                'local_deltas':deepcopy(converted),'key_names':list(expected['names']),'all_keys_synchronized':True,
                'changed_vertices':sum(before['coordinates']['mesh'][i].tobytes()!=expected['mesh'][i].tobytes() for i in seen),
                'author_before_sha256':C.digest(before['author']),
                'qualification':'Explicit point authoring plan; not surface fit/collision/deformation quality certification'}

    def _write(self,coordinates):
        s=self.session
        for item in self._snapshot['converted']:
            vertex=item['vertex']
            s.mesh.vertices[vertex].co=coordinates['mesh'][vertex].tolist()
            for index,key in enumerate(s.mesh.shape_keys.key_blocks):
                key.data[vertex].co=coordinates['keys'][index,vertex].tolist()
        s._update()

    def _recoverable_current(self):
        s=self.session;s._guard()
        current=self._collect();self._protected(current)
        allowed=self._coord_equal(current['coordinates'],self._snapshot['before']['coordinates'])
        allowed=allowed or self._coord_equal(current['coordinates'],self._snapshot['expected'])
        require(allowed,'GEOMETRY_CURRENT_NOT_COMPLETE_DECLARED_ENDPOINT',self.pending)
        return current

    def preview(self,snapshot_token,*,fault=None):
        s=self.session;s._guard()
        require(self._pending is None and self._snapshot and snapshot_token==self._snapshot['token'],
                'GEOMETRY_STALE_SNAPSHOT',snapshot_token)
        require(s.pending is None and (fault is None or callable(fault)),'GEOMETRY_PENDING_OR_FAULT_SCHEMA',None)
        self._exact(self._collect(),self._snapshot['before'],'GEOMETRY_BASELINE_CHANGED')
        self._pending={'token':'geometry_restore_'+uuid.uuid4().hex,'phase':'writing','after':None}
        try:
            self._write(self._snapshot['expected'])
            if fault:
                fault(self)
            after=self._recoverable_current()
            require(self._coord_equal(after['coordinates'],self._snapshot['expected']),'GEOMETRY_EXPECTED_COORDINATES',None)
            require(after['garment_evaluated']['finite'],'GEOMETRY_GARMENT_NONFINITE',s.target.name)
            self._pending.update(phase='applied',after=after)
            diagnostics=lambda author:next(deepcopy(b['mesh']['diagnostics']) for b in author['blocks']
                if b['type']=='Mesh' and b['name']==s.mesh.name and b['library'] is None)
            return {'schema':'hrig.garment-geometry-preview.v1','status':'previewed','restore_token':self._pending['token'],
                    'all_native_mesh_and_all_Key_f32_coordinates_expected':True,'unselected_point_bytes_exact':True,
                    'all_weights_body_bones_context_authors_protected':True,'author_sha256':C.digest(after['author']),
                    'geometry_diagnostics_before':diagnostics(self._snapshot['before']['author']),
                    'geometry_diagnostics_after':diagnostics(after['author']),
                    'quality_qualification':'Geometry observations only; visual/pose/clearance assessment still required'}
        except Exception as error:
            try:
                restored=self.restore(self._pending['token'])
            except Exception as recovery:
                self._pending['phase']='recovery_requires_review'
                raise GarmentError('GEOMETRY_RECOVERY_REQUIRED',{'cause':error_record(error),'recovery':error_record(recovery),
                    'pending':self.pending,'qualification':'Unknown partial coordinate writes are not overwritten'}) from error
            raise GarmentError('GEOMETRY_FAILED_RESTORED',{'cause':error_record(error),'restore':restored}) from error

    def validate_applied(self):
        require(self._pending and self._pending['phase']=='applied','GEOMETRY_NO_APPLIED_PREVIEW',self.pending)
        self.session._guard()
        self._exact(self._collect(),self._pending['after'],'GEOMETRY_APPLIED_STATE_CHANGED')
        return {'status':'geometry_applied_valid','author_sha256':C.digest(self._pending['after']['author'])}

    def restore(self,restore_token):
        require(self._pending and self._pending['token']==restore_token,'GEOMETRY_RESTORE_TOKEN',restore_token)
        require(self.session.pending is None,'WEIGHT_PREVIEW_RESTORE_FIRST',self.session.pending)
        if self._pending['phase']=='applied':
            self.validate_applied()
        current=self._recoverable_current()
        if not self._coord_equal(current['coordinates'],self._snapshot['before']['coordinates']):
            self._pending['phase']='restoring'
            self._write(self._snapshot['before']['coordinates'])
        self._exact(self._collect(),self._snapshot['before'],'GEOMETRY_RESTORE_EXACT')
        result={'status':'restored','full_author_coordinates_all_weights_context_body_bones_garment_exact':True,
                'author_sha256':C.digest(self._snapshot['before']['author'])}
        self._pending=None;self._snapshot=None
        return result
