"""Bound-garment adapter: base-world BVH planning and fenced native preview.

No save/open/apply operators; all writes are explicit target memberships.
Requires the unchanged v045 observer and v015 named-consumer/domain helper.
Their coverage limitations remain visible in every inspection receipt.
"""

from hrig_paths import project_path
from copy import deepcopy
import hashlib
import importlib.util
import math
from pathlib import Path
import sys
import uuid

import bpy
from mathutils.bvhtree import BVHTree
import character_state as C

from .contract import (GarmentError, require, validate_request, digest, bits, number,
                       row_values, normalize, predict_row, operations, barycentric,
                       interpolate, TOLERANCE)

LEGACY_PATH = project_path('work/hrig-p2-v015/addon/humanoid_rigging_probe/weights.py')


def error_record(error):
    return {'code': getattr(error, 'code', type(error).__name__),
            'detail': deepcopy(getattr(error, 'detail', getattr(error, 'details', str(error))))}


def raw_rows(mesh):
    return tuple(tuple((int(x.group), bits(x.weight)) for x in vertex.groups) for vertex in mesh.vertices)


def rows_document(rows):
    return [[[index,value.hex()] for index,value in row] for row in rows]


def rows_digest(rows):
    return digest(rows_document(rows))


class GarmentTransaction:
    def __init__(self, source_identity, source_body, target_garment, rig, idle_guard, *, legacy_path=LEGACY_PATH):
        require(type(source_identity) is dict and set(source_identity) == {'file','expected_sha256'},
                'SOURCE_IDENTITY_SCHEMA', 'file/expected_sha256 required')
        require(type(source_identity['file']) is str and Path(source_identity['file']).is_absolute(), 'SOURCE_PATH', source_identity)
        sha = source_identity['expected_sha256']
        require(type(sha) is str and len(sha) == 64 and all(c in '0123456789abcdef' for c in sha), 'SOURCE_SHA', sha)
        require(callable(idle_guard), 'IDLE_GUARD_REQUIRED', 'Caller checks its known sessions')
        require(all(type(x) is str and x for x in (source_body,target_garment,rig)), 'BOUND_OBJECT_NAMES', 'Explicit names required')
        self._source = deepcopy(source_identity)
        self._names = {'source_body':source_body,'target_garment':target_garment,'rig':rig}
        self.body, self.target, self.rig = (bpy.data.objects.get(x) for x in (source_body,target_garment,rig))
        require(self.body and self.target and self.rig and self.body != self.target
                and self.body.type == self.target.type == 'MESH' and self.rig.type == 'ARMATURE', 'BOUND_OBJECT_TYPES', self.names)
        self.mesh = self.target.data
        self.body_mesh = self.body.data
        self._idle_guard = idle_guard
        path = Path(legacy_path)
        require(path.is_absolute() and path.is_file(), 'LEGACY_HELPER_PATH', str(path))
        self.legacy_identity = {'file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        spec = importlib.util.spec_from_file_location('hrig_d4_named_consumers_' + self.legacy_identity['sha256'][:12], path)
        require(spec and spec.loader, 'LEGACY_HELPER_IMPORT', str(path))
        self.W = importlib.util.module_from_spec(spec)
        old = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(self.W)
        finally:
            sys.dont_write_bytecode = old
        self._snapshot = None
        self._pending = None
        self._guard()

    @property
    def pending(self):
        return deepcopy({k:self._pending[k] for k in ('token','phase')}) if self._pending else None

    @property
    def source(self):
        return deepcopy(self._source)

    @property
    def names(self):
        return deepcopy(self._names)

    @property
    def has_pending_edit(self):
        return self._pending is not None

    def _guard(self):
        require(self._idle_guard() is True, 'OTHER_EDIT_PENDING', 'Restore known other sessions first')
        require(Path(bpy.data.filepath).resolve() == Path(self.source['file']).resolve(), 'SOURCE_MAIN_MISMATCH', bpy.data.filepath)
        require(hashlib.sha256(Path(self.source['file']).read_bytes()).hexdigest() == self.source['expected_sha256'],
                'SOURCE_SHA_CHANGED', self.source['file'])
        require(bpy.data.objects.get(self.names['source_body']) == self.body and self.body.data == self.body_mesh
                and bpy.data.objects.get(self.names['target_garment']) == self.target and self.target.data == self.mesh
                and bpy.data.objects.get(self.names['rig']) == self.rig, 'BOUND_ID_CHANGED', self.names)
        require(bpy.context.view_layer.objects.get(self.body.name) == self.body
                and bpy.context.view_layer.objects.get(self.target.name) == self.target
                and bpy.context.view_layer.objects.get(self.rig.name) == self.rig, 'VIEW_LAYER_BINDING', self.names)
        # Preserve the old ownership/shared/editability/Armature domain guard.
        try:
            self.W._domain(bpy.context, self.body, self.rig)
            self.W._domain(bpy.context, self.target, self.rig)
        except Exception as error:
            record = error_record(error)
            raise GarmentError(record['code'], record['detail']) from error
        require(self.body.parent_type == self.target.parent_type == 'OBJECT', 'BONE_PARENT_REST_SPACE', self.names)
        require(len(self.body_mesh.vertices) <= 500000 and len(self.mesh.vertices) <= 500000,
                'VERTEX_BUDGET', self.names)
        for obj in (self.body,self.target):
            matrix = obj.matrix_world
            require(all(math.isfinite(v) for row in matrix for v in row) and abs(matrix.determinant()) > 1e-12,
                    'BASE_WORLD_MATRIX', obj.name)

    def _context(self):
        context = bpy.context
        return {'scene':context.scene.name,'view_layer':context.view_layer.name,'mode':context.mode,
                'frame':context.scene.frame_current,'subframe':context.scene.frame_subframe,
                'active':context.view_layer.objects.active.name if context.view_layer.objects.active else None,
                'selection':[(obj.name,bool(obj.select_get())) for obj in context.view_layer.objects],
                'active_group':[(obj.name,obj.vertex_groups.active_index) for obj in (self.body,self.target)],
                'armature_collection_active':C.collection_active_indices()}

    def _evaluation(self, obj):
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            points = [tuple(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices]
        finally:
            evaluated.to_mesh_clear()
        finite = all(math.isfinite(v) for point in points for v in point)
        return {'count':len(points),'finite':finite,'sha256':digest(points) if finite else None}

    def _collect(self):
        bpy.context.view_layer.update()
        body, target = self._evaluation(self.body), self._evaluation(self.target)
        bones = [(bone.name,tuple(v for row in bone.matrix for v in row)) for bone in self.rig.pose.bones]
        require(all(math.isfinite(v) for _,matrix in bones for v in matrix), 'BONE_EVALUATION_NONFINITE', self.rig.name)
        return {'rows':raw_rows(self.mesh),'author':C.capture(),'context':self._context(),
                'bones':bones,'body_evaluated':body,'garment_evaluated':target,
                'material_slots':[(slot.link,slot.material.name if slot.material else None) for slot in self.target.material_slots]}

    def _projection(self, state):
        return C.protected(state['author'],self.target.name,self.mesh.name,self._snapshot['request']['vertices'])

    def _protected_fence(self, current):
        before = self._snapshot['before']
        checks = {'protected_author':self._projection(current) == self._snapshot['protected'],
                  'context':current['context'] == before['context'],
                  'bones':current['bones'] == before['bones'],
                  'body_evaluated':current['body_evaluated'] == before['body_evaluated'],
                  'garment_topology':current['garment_evaluated']['count'] == before['garment_evaluated']['count'],
                  'material_slots':current['material_slots'] == before['material_slots']}
        require(all(checks.values()), 'NONWEIGHT_STATE_CHANGED', checks)

    def _exact(self, actual, expected, code):
        checks = {key:actual[key] == expected[key] for key in expected}
        require(all(checks.values()),code,checks)

    def _consumers(self):
        groups = {group.name for group in self.target.vertex_groups}
        result = self.W._group_consumers(self.target,groups)
        # The legacy helper scans Mesh.materials. Also scan effective Object
        # slots, which are the actual material owners on Snow's shirt.
        seen = set()
        stack = [slot.material.node_tree for slot in self.target.material_slots
                 if slot.material and slot.material.node_tree]
        while stack:
            tree = stack.pop()
            if tree in seen:
                continue
            seen.add(tree)
            require(len(seen) <= 256,'OBJECT_MATERIAL_TREE_BUDGET',len(seen))
            require(len(tree.nodes) <= 10000,'OBJECT_MATERIAL_NODE_BUDGET',tree.name)
            for node in tree.nodes:
                if node.bl_idname == 'ShaderNodeAttribute' and node.attribute_name in groups:
                    result['items'].append({'group':node.attribute_name,'kind':'effective_material_attribute',
                                            'owner':self.target.name,'path':tree.name + '/' + node.name})
                elif node.bl_idname == 'GeometryNodeInputNamedAttribute':
                    socket = next((x for x in node.inputs if x.identifier == 'Name'),None)
                    if socket and not socket.is_linked and isinstance(socket.default_value,str):
                        if socket.default_value in groups:
                            result['items'].append({'group':socket.default_value,'kind':'effective_material_named_attribute',
                                                    'owner':self.target.name,'path':tree.name + '/' + node.name})
                    else:
                        result['unresolved'].append({'kind':'dynamic_effective_material_attribute','path':tree.name + '/' + node.name})
                nested = getattr(node,'node_tree',None)
                if nested:
                    stack.append(nested)
        require(not result['unresolved'],'UNRESOLVED_NAMED_CONSUMERS',result['unresolved'])
        return result

    def _base_world(self, obj):
        keys = obj.data.shape_keys
        vertices = keys.reference_key.data if keys else obj.data.vertices
        points = [obj.matrix_world @ vertex.co for vertex in vertices]
        require(all(math.isfinite(v) for point in points for v in point),'NONFINITE_BASE_WORLD',obj.name)
        return points

    def inspect(self, request):
        require(self._pending is None,'PREVIEW_ALREADY_PENDING',self.pending)
        self._guard()
        request = validate_request(request,len(self.mesh.vertices),len(self.body_mesh.polygons))
        require(all(request[k] == v for k,v in self.names.items()),'REQUEST_BINDING',self.names)
        before = self._collect()
        require(before['body_evaluated']['finite'] and before['garment_evaluated']['finite'],
                'BASELINE_EVALUATED_NONFINITE',self.names)
        groups = [group.name for group in self.target.vertex_groups]
        ids = {name:i for i,name in enumerate(groups)}
        deform = {bone.name for bone in self.rig.data.bones if bone.use_deform}
        allowed = set(request['allowed_groups'])
        require(allowed <= set(groups) & deform,'ALLOWED_GROUP_NOT_DEFORM',sorted(allowed - (set(groups) & deform)))
        consumers = self._consumers()
        frozen_names = {item['group'] for item in consumers['items']} | {g.name for g in self.target.vertex_groups if g.lock_weight}
        editable = {ids[n] for n in allowed - frozen_names}
        deform_ids = {ids[n] for n in set(groups) & deform}
        require(editable,'NO_EDITABLE_GROUPS',sorted(allowed))
        expected = list(before['rows'])
        rows, errors = [], []
        deltas = {}
        overrides={item['vertex']:item for item in request.get('correspondence',[])}
        if request['operation'] == 'local_delta':
            for item in request['deltas']:
                require(ids[item['group']] in editable,'DELTA_PROTECTED_GROUP',item)
                deltas.setdefault(item['vertex'],{})[ids[item['group']]] = item['delta']
        else:
            source_groups = [g.name for g in self.body.vertex_groups]
            require(set(request['mapping']) <= set(source_groups) & deform,'MAPPING_SOURCE_NOT_DEFORM',sorted(set(request['mapping']) - (set(source_groups) & deform)))
            self.body_mesh.calc_loop_triangles()
            polygons = set(request['source_polygons'])
            triangles = [tuple(triangle.vertices) for triangle in self.body_mesh.loop_triangles if triangle.polygon_index in polygons]
            require(0 < len(triangles) <= 1000000,'SOURCE_TRIANGLE_BUDGET',len(triangles))
            source_points = self._base_world(self.body)
            target_points = self._base_world(self.target)
            bvh = BVHTree.FromPolygons(source_points,triangles,all_triangles=True)
            source_rows = raw_rows(self.body_mesh)
        for index in request['vertices']:
            try:
                old = row_values(before['rows'][index],groups)
                info = {'vertex':index}
                if request['operation'] == 'local_delta':
                    desired = {i:old.get(i,0.) + deltas[index].get(i,0.) for i in editable}
                    desired,statistics = normalize(desired,old,deform_ids,editable)
                else:
                    point,normal,triangle_index,distance = bvh.find_nearest(target_points[index])
                    require(triangle_index is not None and distance <= request['max_distance'],'DISTANCE_GATE',
                            {'vertex':index,'distance':distance,'maximum':request['max_distance']})
                    nearby = bvh.find_nearest_range(target_points[index],distance + request['tie_tolerance'])
                    require(len(nearby) <= 128,'TIE_CANDIDATE_BUDGET',{'vertex':index,'count':len(nearby)})
                    candidates = []
                    for position,_,tri_id,dist in nearby:
                        if abs(dist-distance) > request['tie_tolerance']:
                            continue
                        tri = triangles[tri_id]
                        factors = barycentric(position,[source_points[v] for v in tri])
                        values,mass = interpolate([source_rows[v] for v in tri],factors,source_groups,deform,request['mapping'],ids)
                        candidate,statistics = normalize(values,old,deform_ids,editable)
                        candidates.append((tri_id,candidate,statistics,factors,dist,mass,values))
                    require(bool(candidates),'NO_NEAREST_TRIANGLE',index)
                    candidates.sort(key=lambda item:item[0])
                    reference = candidates[0][1]
                    if index not in overrides:
                        require(all(max(abs(reference[i]-item[1][i]) for i in editable) <= TOLERANCE for item in candidates),
                                'AMBIGUOUS_WEIGHT_CORRESPONDENCE',{'vertex':index,'triangles':[item[0] for item in candidates]})
                    tri_id,desired,statistics,factors,distance,source_mass,_ = candidates[0]
                    info.update(triangle=tri_id,source_vertices=list(triangles[tri_id]),barycentric=list(factors),
                                distance=distance,near_tie_candidate_count=len(candidates),source_deform_sum=source_mass)
                    if index in overrides:
                        rule=overrides[index]
                        require(set(rule['triangles'])=={item[0] for item in candidates},'EXPLICIT_CORRESPONDENCE_STALE',
                                {'vertex':index,'expected':rule['triangles'],'actual':[item[0] for item in candidates]})
                        if rule['mode']=='source_edge':
                            edge=rule['edge']
                            require(all(set(edge)<=set(triangles[t]) for t in rule['triangles']),
                                    'EXPLICIT_EDGE_NOT_SHARED',rule)
                            left,right=[source_points[v] for v in edge]
                            direction=right-left
                            length=math.fsum(x*x for x in direction)
                            require(length>0.,'EXPLICIT_EDGE_DEGENERATE',rule)
                            offset=target_points[index]-left
                            factor=max(0.,min(1.,math.fsum(x*y for x,y in zip(offset,direction))/length))
                            projected=left+direction*factor
                            edge_distance=float((projected-target_points[index]).length)
                            require(edge_distance<=distance+request['tie_tolerance'],'EXPLICIT_EDGE_NOT_NEAREST',
                                    {'vertex':index,'edge_distance':edge_distance,'nearest_distance':distance})
                            values,source_mass=interpolate([source_rows[v] for v in edge],(1.-factor,factor),
                                source_groups,deform,request['mapping'],ids)
                            desired,statistics=normalize(values,old,deform_ids,editable)
                            info.update(triangle=None,source_vertices=list(edge),barycentric=[1.-factor,factor],
                                        distance=edge_distance,source_deform_sum=source_mass)
                        else:
                            axis,center=rule['axis'],rule['center']
                            require(abs(target_points[index][axis]-center)<=request['tie_tolerance'],
                                    'EXPLICIT_SYMMETRY_VERTEX_OFF_PLANE',rule)
                            first,second=[triangles[t] for t in rule['triangles']]
                            reflected=[]
                            for vertex in first:
                                point=list(source_points[vertex]);point[axis]=2.*center-point[axis];reflected.append(point)
                            unmatched=[list(source_points[v]) for v in second]
                            for point in reflected:
                                nearest=min(range(len(unmatched)),key=lambda i:math.fsum((x-y)**2 for x,y in zip(point,unmatched[i])))
                                require(math.sqrt(math.fsum((x-y)**2 for x,y in zip(point,unmatched[nearest])))<=request['tie_tolerance'],
                                        'EXPLICIT_SOURCE_NOT_MIRRORED',rule)
                                unmatched.pop(nearest)
                            values={i:math.fsum(item[6].get(i,0.) for item in candidates)/len(candidates) for i in ids.values()}
                            desired,statistics=normalize(values,old,deform_ids,editable)
                            info.update(triangle=None,source_vertices=[list(triangles[t]) for t in rule['triangles']],
                                        barycentric=[list(item[3]) for item in candidates],source_deform_sum=math.fsum(item[5] for item in candidates)/len(candidates))
                        info['explicit_correspondence']=deepcopy(rule)
                expected[index] = predict_row(before['rows'][index],desired)
                info.update(statistics=statistics,changed=expected[index] != before['rows'][index],
                            appended_groups=[groups[i] for i,_ in expected[index] if i not in old])
                rows.append(info)
            except Exception as error:
                errors.append({'vertex':index,**error_record(error)})
        require(not errors,'PLAN_REJECTED',{'failed_count':len(errors),'failures':errors,
                    'successful_count':len(rows),'no_native_writes':True})
        token = 'garment_plan_' + uuid.uuid4().hex
        self._snapshot = {'token':token,'request':request,'before':before,'expected':tuple(expected),
                          'groups':groups,'editable':editable,'rows':rows,'consumers':consumers}
        self._snapshot['protected'] = self._projection(before)
        return deepcopy({'schema':'hrig.bound-garment-inspection.v1','snapshot_token':token,'request':request,
                         'rows':rows,'changed_vertex_count':sum(x['changed'] for x in rows),
                         'frozen_groups':sorted(frozen_names),'editable_groups':[groups[i] for i in sorted(editable)],
                         'consumers':consumers,'all_before_rows_sha256':rows_digest(before['rows']),
                         'all_expected_rows_sha256':rows_digest(expected),'before_author_sha256':C.digest(before['author']),
                         'source':self.source,'legacy_helper':self.legacy_identity,
                         'base_space':'Reference Key (or raw Mesh) coordinates under captured Object.matrix_world; no posed surface',
                         'quality':'No visual fit, arbitrary-pose collision or cross-character fit certification'})

    def _update(self):
        self.mesh.update()
        self.target.update_tag(refresh={'OBJECT','DATA'})
        bpy.context.view_layer.update()

    def _operation(self, vertex, group, value=None):
        """Declare both possible native call endpoints before invoking it.

        A throwing call may leave either endpoint. No arbitrary changed state is
        accepted as a restoration checkpoint, even inside editable groups.
        """
        rows = list(self._pending['checkpoint'])
        row = list(rows[vertex])
        found = next((i for i,(g,_) in enumerate(row) if g == group),None)
        if value is None:
            require(found is not None,'REMOVE_MEMBERSHIP_MISSING',{'vertex':vertex,'group':group})
            # Only reverse-order removal of our appended memberships is used.
            require(found == len(row)-1,'RESTORE_APPEND_ORDER',{'vertex':vertex,'group':group})
            row.pop(found)
        elif found is None:
            row.append((group,bits(value)))
        else:
            row[found] = (group,bits(value))
        rows[vertex] = tuple(row)
        candidate = tuple(rows)
        self._pending['attempted'] = candidate
        native_group = self.target.vertex_groups[group]
        if value is None:
            native_group.remove([vertex])
        else:
            native_group.add([vertex],value,'REPLACE')
        self._pending['checkpoint'] = candidate
        self._pending['attempted'] = None

    def _checkpoint_fence(self):
        self._guard()
        current = self._collect()
        self._protected_fence(current)
        permitted = [self._pending['checkpoint']]
        if self._pending.get('attempted') is not None:
            permitted.append(self._pending['attempted'])
        require(current['rows'] in permitted,'CURRENT_CHECKPOINT_CHANGED',
                {'actual':rows_digest(current['rows']),'allowed':[rows_digest(r) for r in permitted]})
        self._pending['checkpoint'] = current['rows']
        self._pending['attempted'] = None
        return current

    def _restore(self):
        self._checkpoint_fence()
        before = self._snapshot['before']
        for vertex in self._snapshot['request']['vertices']:
            original = dict(before['rows'][vertex])
            # Remove only memberships inserted by this transaction, in reverse
            # append order. Retain originally-present zero memberships.
            for group,_ in reversed(self._pending['checkpoint'][vertex]):
                if group not in original:
                    require(group in self._snapshot['editable'],'RESTORE_NONEDITABLE_INSERTION',group)
                    self._operation(vertex,group)
            current = dict(self._pending['checkpoint'][vertex])
            for group,value in before['rows'][vertex]:
                if current[group] != value:
                    require(group in self._snapshot['editable'],'RESTORE_NONEDITABLE_CHANGE',group)
                    self._operation(vertex,group,number(value))
        self._update()
        actual = self._collect()
        self._exact(actual,before,'RESTORE_EXACT_FAILED')
        result = {'schema':'hrig.bound-garment-restore.v1','status':'restored','all_raw_rows_exact':True,
                  'full_author_exact':True,'context_exact':True,'evaluated_body_garment_bones_exact':True,
                  'author_sha256':C.digest(actual['author']),'all_raw_rows_sha256':rows_digest(actual['rows'])}
        self._pending = None
        self._snapshot = None
        return result

    def preview(self, snapshot_token, *, fault=None):
        require(self._pending is None and self._snapshot and snapshot_token == self._snapshot['token'],
                'STALE_SNAPSHOT',snapshot_token)
        require(fault is None or callable(fault),'FAULT_HOOK','Trusted fixture callable only')
        self._guard()
        self._exact(self._collect(),self._snapshot['before'],'LIVE_SNAPSHOT_CHANGED')
        self._pending = {'token':'garment_restore_' + uuid.uuid4().hex,'phase':'patching',
                         'checkpoint':self._snapshot['before']['rows'],'attempted':None,'after':None}
        try:
            for index in self._snapshot['request']['vertices']:
                for group,value in operations(self._snapshot['before']['rows'][index],self._snapshot['expected'][index]):
                    self._operation(index,group,value)
            self._update()
            if fault:
                fault(self)
            after = self._checkpoint_fence()
            require(after['rows'] == self._snapshot['expected'],'EXPECTED_RAW_ROWS_CHANGED',rows_digest(after['rows']))
            require(after['garment_evaluated']['finite'],'GARMENT_EVALUATED_NONFINITE',self.target.name)
            self._pending.update(phase='applied',after=after)
            return deepcopy({'schema':'hrig.bound-garment-preview.v1','status':'previewed','restore_token':self._pending['token'],
                    'full_protected_author_exact':True,'all_raw_rows_oracle_exact':True,
                    'material_slots_exact':True,'body_and_bones_exact':True,
                    'garment_evaluated':after['garment_evaluated'],
                    'garment_evaluated_changed':after['garment_evaluated'] != self._snapshot['before']['garment_evaluated'],
                    'changed_vertices':sum(row['changed'] for row in self._snapshot['rows']),
                    'all_raw_rows_sha256':rows_digest(after['rows']),'author_sha256':C.digest(after['author'])})
        except Exception as error:
            try:
                rollback = self._restore()
            except Exception as recovery:
                self._pending['phase'] = 'recovery_required'
                raise GarmentError('PREVIEW_RECOVERY_REQUIRED',{'cause':error_record(error),'recovery':error_record(recovery),
                                   'pending':self.pending,'instruction':'Undo only the owned external fixture/drift back to the declared checkpoint, then restore with the same token'}) from error
            raise GarmentError('PREVIEW_FAILED_RESTORED',{'cause':error_record(error),'rollback':rollback}) from error

    def validate_applied(self):
        require(self._pending and self._pending['phase'] == 'applied','NO_APPLIED_PREVIEW',self.pending)
        self._guard()
        current = self._collect()
        self._exact(current,self._pending['after'],'APPLIED_STATE_CHANGED')
        return {'status':'applied_valid','author_sha256':C.digest(current['author']),
                'all_raw_rows_sha256':rows_digest(current['rows'])}

    def restore(self, restore_token):
        require(self._pending and restore_token == self._pending['token'],'RESTORE_TOKEN',restore_token)
        if self._pending['phase'] == 'applied':
            self.validate_applied()
        try:
            return self._restore()
        except Exception:
            self._pending['phase'] = 'recovery_required'
            raise

    def candidate_metadata(self):
        self.validate_applied()
        snapshot,after = self._snapshot,self._pending['after']
        return deepcopy({'schema':'hrig.bound-garment-candidate.v1','source':self.source,'objects':self.names,
                         'request':snapshot['request'],'author_before_sha256':C.digest(snapshot['before']['author']),
                         'author_after_sha256':C.digest(after['author']),'protected_author_sha256':C.digest(snapshot['protected']),
                         'target_all_native_rows':rows_document(after['rows']),'target_all_native_rows_sha256':rows_digest(after['rows']),
                         'target_original_native_rows':rows_document(snapshot['before']['rows']),
                         'body_evaluated':after['body_evaluated'],'garment_evaluated':after['garment_evaluated'],
                         'material_slots':after['material_slots'],'legacy_helper':self.legacy_identity,
                         'save_policy':'Caller saves a NEW explicit candidate with copy=True; this module never saves',
                         'qualification':'Cold reopen, representative pose/deformation and visual garment review still required'})
