"""Fenced local weight transaction with exact append/tail-remove recovery.

Journal design references project v110 garment_adapt; no runtime qualification
is inherited. Legacy v015 supplies unchanged owner/domain and consumer checks,
not its sum-constrained patch/inverse. Only C.protected's one weight digest is
omitted, supplied by the independent complete ordered-f32 raw row oracle.
"""

from hrig_paths import project_path
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import sys
import uuid

import bpy
import numpy as np
import character_state as C
import native_control as N

from . import contract as K
from .contract import WeightError, require, bits, number, digest

LEGACY_PATH = project_path('work/hrig-p2-v015/addon/humanoid_rigging_probe/weights.py')
PERSISTENCE_PATH = project_path('work/hrig-p2-v015/addon/humanoid_rigging_probe/persistence.py')


def error_record(error):
    return {'code': getattr(error, 'code', type(error).__name__),
        'detail': deepcopy(getattr(error, 'detail', getattr(error, 'details', str(error))))}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_record(value):
    require(type(value) is dict and set(value) == {'file', 'expected_sha256'}, 'FILE_IDENTITY')
    require(type(value['file']) is str and Path(value['file']).is_absolute(), 'ABSOLUTE_PATH')
    h = value['expected_sha256']
    require(type(h) is str and len(h) == 64 and all(c in '0123456789abcdef' for c in h), 'SHA256')
    return deepcopy(value)


def module_from_path(path, prefix):
    path = Path(path)
    require(path.is_absolute() and path.is_file(), 'DEPENDENCY_PATH', str(path))
    spec = importlib.util.spec_from_file_location(prefix + sha(path)[:12], path)
    require(spec is not None and spec.loader is not None, 'DEPENDENCY_IMPORT', str(path))
    module = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
    return module


def rows_document(rows):
    return [K.row_document(row) for row in rows]


def rows_digest(rows):
    return digest(rows_document(rows))


def array_digest(value):
    return hashlib.sha256(value.tobytes(order='C')).hexdigest()


class WeightWorkflowTransaction:
    def __init__(self, source_identity, object_name, rig_name, idle_guard, *, source_resources,
                 legacy_path=LEGACY_PATH, persistence_path=PERSISTENCE_PATH):
        self._source = source_record(source_identity)
        require(type(source_resources) is list, 'SOURCE_RESOURCES_REQUIRED')
        self._resources = [source_record(r) for r in source_resources]
        paths = [str(Path(r['file']).resolve()) for r in self._resources]
        require(len(paths) == len(set(paths)), 'DUPLICATE_RESOURCE')
        require(callable(idle_guard), 'IDLE_GUARD_REQUIRED')
        require(all(type(n) is str and n for n in (object_name, rig_name)), 'OBJECT_NAMES')
        self._idle_guard = idle_guard
        self.obj, self.rig = bpy.data.objects.get(object_name), bpy.data.objects.get(rig_name)
        require(self.obj is not None and self.obj.type == 'MESH' and self.rig is not None
                and self.rig.type == 'ARMATURE', 'BOUND_OBJECT_TYPES')
        self.mesh, self.armature = self.obj.data, self.rig.data
        self._ids = {key: (value, value.name, int(value.session_uid)) for key, value in
            (('object', self.obj), ('mesh', self.mesh), ('rig', self.rig), ('armature', self.armature))}
        self.W = module_from_path(legacy_path, 'hrig_weight_v112_legacy_')
        self.persistence = module_from_path(persistence_path, 'hrig_weight_v112_persistence_')
        require(self.W.PRESERVE_POLICY == K.PRESERVE, 'LEGACY_VERSION')
        files = [Path(legacy_path), Path(persistence_path), Path(C.__file__), Path(N.__file__), Path(__file__), Path(K.__file__)]
        self._implementation = [{'file': str(p.resolve()), 'expected_sha256': sha(p)} for p in files]
        self._snapshot, self._pending = None, None
        self.write_count = 0
        self._guard()

    @property
    def source(self):
        return deepcopy(self._source)

    @property
    def pending(self):
        return deepcopy({k: self._pending[k] for k in ('token', 'phase')}) if self._pending else None

    def _guard(self, frame=None):
        require(tuple(bpy.app.version) == (5, 2, 1) and bpy.app.build_hash.decode() == '9e2066aef7ef', 'RUNTIME_BUILD')
        require(self._idle_guard() is True, 'OTHER_EDIT_PENDING')
        require(Path(bpy.data.filepath).resolve() == Path(self._source['file']).resolve(), 'SOURCE_MAIN_CHANGED')
        for identity in [self._source, *self._resources, *self._implementation]:
            require(Path(identity['file']).is_file() and sha(identity['file']) == identity['expected_sha256'], 'SOURCE_OR_DEPENDENCY_CHANGED', identity['file'])
        for key, (value, name, uid) in self._ids.items():
            collection = bpy.data.objects if key in ('object', 'rig') else bpy.data.meshes if key == 'mesh' else bpy.data.armatures
            require(collection.get(name) == value and int(value.session_uid) == uid, 'ID_LIFETIME_CHANGED', key)
        require(self.obj.data == self.mesh and self.rig.data == self.armature, 'DATA_BINDING_CHANGED')
        require(all(bpy.context.view_layer.objects.get(o.name) == o for o in (self.obj, self.rig)), 'VIEW_LAYER_BINDING')
        if frame is not None:
            require(bpy.context.scene.frame_current_final == frame, 'OBSERVATION_FRAME_CHANGED')
        try:
            return self.W._domain(bpy.context, self.obj, self.rig)
        except Exception as error:
            record = error_record(error)
            raise WeightError(record['code'], record['detail']) from error

    def _raw_all(self):
        return tuple(tuple((int(m.group), bits(m.weight)) for m in v.groups) for v in self.mesh.vertices)

    def _context(self):
        return {'native': N.context(), 'active_group': self.obj.vertex_groups.active_index,
                'object_modes': [[o.name, o.mode] for o in bpy.context.view_layer.objects],
                'object_select': [[o.name, bool(o.select_get(view_layer=bpy.context.view_layer))] for o in bpy.context.view_layer.objects],
                'mesh_selection': [bool(v.select) for v in self.mesh.vertices]}

    def _collect(self):
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        rig = self.rig.evaluated_get(graph)
        names = [b.name for b in rig.pose.bones]
        bones = np.asarray([[list(row) for row in b.matrix] for b in rig.pose.bones], dtype='<f4')
        evaluated = self.obj.evaluated_get(graph)
        mesh = evaluated.to_mesh()
        try:
            skin = np.empty(len(mesh.vertices) * 3, dtype='<f4')
            mesh.vertices.foreach_get('co', skin)
            world = np.asarray(evaluated.matrix_world, dtype='<f4')
            skin = skin.reshape(-1, 3) @ world[:3, :3].T + world[:3, 3]
        finally:
            evaluated.to_mesh_clear()
        require(bool(np.isfinite(bones).all()), 'NONFINITE_EVALUATED_BONES')
        return {'author': C.capture(), 'context': self._context(), 'rows': self._raw_all(),
                'bone_names': names, 'bones': bones, 'skin': skin}

    @staticmethod
    def _array_same(a, b):
        return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()

    def _exact(self, actual, expected, code):
        checks = {k: actual[k] == expected[k] for k in ('author', 'context', 'rows', 'bone_names')}
        checks['author_digest'] = C.digest(actual['author']) == C.digest(expected['author'])
        checks.update({k: self._array_same(actual[k], expected[k]) for k in ('bones', 'skin')})
        require(all(checks.values()), code, checks)
        return checks

    def _projection(self, state):
        return C.protected(state['author'], self.obj.name, self.mesh.name, self._snapshot['request']['vertices'])

    def _protected_fence(self, current):
        snapshot, before = self._snapshot, self._snapshot['before']
        projection = self._projection(current)
        checks = {'protected_author': projection == snapshot['protected'] and C.digest(projection) == C.digest(snapshot['protected']),
            'context': current['context'] == before['context'], 'bone_names': current['bone_names'] == before['bone_names'],
            'bones': self._array_same(current['bones'], before['bones']), 'skin_topology': current['skin'].shape == before['skin'].shape}
        require(all(checks.values()), 'NONWEIGHT_STATE_CHANGED', checks)

    def _consumers(self):
        """Legacy coverage plus effective Object-linked material attributes."""
        names = {g.name for g in self.obj.vertex_groups}
        result = self.W._group_consumers(self.obj, names)
        seen = set()
        stack = [slot.material.node_tree for slot in self.obj.material_slots if slot.material and slot.material.node_tree]
        while stack:
            tree = stack.pop()
            if tree in seen:
                continue
            seen.add(tree)
            require(len(seen) <= 256 and len(tree.nodes) <= 10000, 'EFFECTIVE_MATERIAL_BUDGET')
            for node in tree.nodes:
                if node.bl_idname == 'ShaderNodeAttribute' and node.attribute_name in names:
                    result['items'].append({'group': node.attribute_name, 'kind': 'effective_material_attribute', 'owner': self.obj.name, 'path': tree.name + '/' + node.name})
                elif node.bl_idname == 'GeometryNodeInputNamedAttribute':
                    socket = next((x for x in node.inputs if x.identifier == 'Name'), None)
                    if socket and not socket.is_linked and isinstance(socket.default_value, str):
                        if socket.default_value in names:
                            result['items'].append({'group': socket.default_value, 'kind': 'effective_material_named_attribute', 'owner': self.obj.name, 'path': tree.name + '/' + node.name})
                    else:
                        result['unresolved'].append({'kind': 'dynamic_effective_material_attribute', 'path': tree.name + '/' + node.name})
                nested = getattr(node, 'node_tree', None)
                if nested:
                    stack.append(nested)
        require(not result['unresolved'], 'UNRESOLVED_CONSUMERS', result['unresolved'])
        return result

    def inspect(self, request):
        require(self._pending is None, 'PREVIEW_ALREADY_PENDING')
        self._snapshot = None
        groups, group_sha, deform, rig_sha = self._guard()
        request = K.validate_request(request, len(self.mesh.vertices))
        require(request['object'] == self.obj.name and request['rig'] == self.rig.name, 'REQUEST_BINDING')
        before = self._collect()
        require(bool(np.isfinite(before['skin']).all()), 'BASELINE_SKIN_NONFINITE')
        consumers = self._consumers()
        blocked = {item['group'] for item in consumers['items']}
        edges = [list(edge.vertices) for edge in self.mesh.edges]
        needed = K.required_vertices(request, edges, len(self.mesh.vertices))
        document = {'schema': 'hrig.weight-workflow-snapshot.v1', 'object': self.obj.name, 'rig': self.rig.name,
            'vertex_count': len(self.mesh.vertices), 'groups': [
                {'name': g.name, 'index': g.index, 'deform': g.name in deform, 'locked': bool(g.lock_weight), 'consumer': g.name in blocked} for g in groups],
            'rows': [{'index': i, 'members': K.row_document(before['rows'][i])} for i in needed], 'edges': edges}
        plan = K.plan(document, request)
        expected = list(before['rows'])
        for row in plan['rows']:
            require(row['before'] == K.row_document(before['rows'][row['index']]), 'PLAN_BEFORE_MISMATCH')
            expected[row['index']] = K.parse_row(row['after'], len(groups))
        self._snapshot = {'token': 'weight_plan_' + uuid.uuid4().hex, 'before': before, 'request': request,
            'document': document, 'plan': plan, 'expected': tuple(expected), 'consumers': consumers,
            'editable': set(plan['editable_indices']), 'groups_sha256': group_sha, 'rig_deform_sha256': rig_sha}
        self._snapshot['protected'] = self._projection(before)
        # Planning is read-only and may evaluate RNA; do not freeze a drifted
        # plan as a new baseline or silently accept a concurrent author change.
        self._exact(self._collect(), before, 'INSPECT_STATE_CHANGED')
        return deepcopy({'schema': 'hrig.weight-workflow-inspection.v1', 'status': 'inspected', 'snapshot_token': self._snapshot['token'],
            'plan': plan, 'source': self.source, 'source_resources': self._resources, 'implementation': self._implementation,
            'group_count': len(groups), 'consumers': consumers, 'all_raw_before_sha256': rows_digest(before['rows']),
            'all_raw_expected_sha256': rows_digest(expected), 'author_before_sha256': C.digest(before['author']),
            'runtime': {'version': list(bpy.app.version), 'build': bpy.app.build_hash.decode()},
            'qualification': 'plan only; no native writes, automatic artistic improvement or saved/cold eligibility'} )

    def _update(self):
        self.mesh.update()
        self.obj.update_tag(refresh={'OBJECT', 'DATA'})
        bpy.context.view_layer.update()

    def _operation(self, vertex, group, value=None):
        """Permit only the declared before/after endpoint of one synchronous call."""
        require(group in self._snapshot['editable'] and vertex in self._snapshot['request']['vertices'], 'WRITE_DOMAIN')
        checkpoint = self._pending['checkpoint']
        actual_row = tuple((int(m.group), bits(m.weight)) for m in self.mesh.vertices[vertex].groups)
        require(actual_row == checkpoint[vertex], 'PREWRITE_ROW_CHANGED', vertex)
        row = list(checkpoint[vertex])
        found = next((i for i, (g, _) in enumerate(row) if g == group), None)
        if value is None:
            require(found == len(row) - 1 and found is not None, 'REMOVE_NOT_OWNED_TAIL')
            require(group not in dict(self._snapshot['before']['rows'][vertex]), 'REMOVE_ORIGINAL_MEMBERSHIP')
            row.pop()
        elif found is None:
            require(value > 0., 'APPEND_NONPOSITIVE')
            row.append((group, bits(value)))
        else:
            row[found] = (group, bits(value))
        rows = list(checkpoint)
        rows[vertex] = tuple(row)
        candidate = tuple(rows)
        self._pending['attempted'] = candidate
        self.write_count += 1
        if value is None:
            self.obj.vertex_groups[group].remove([vertex])
        else:
            self.obj.vertex_groups[group].add([vertex], value, 'REPLACE')
        actual_row = tuple((int(m.group), bits(m.weight)) for m in self.mesh.vertices[vertex].groups)
        require(actual_row == candidate[vertex], 'NATIVE_MEMBERSHIP_ORDER_OR_VALUE', vertex)
        self._pending['checkpoint'], self._pending['attempted'] = candidate, None

    def _checkpoint_fence(self):
        self._guard(self._snapshot['before']['context']['native']['frame'])
        current = self._collect()
        self._protected_fence(current)
        allowed = [self._pending['checkpoint']]
        if self._pending['attempted'] is not None:
            allowed.append(self._pending['attempted'])
        require(current['rows'] in allowed, 'CURRENT_CHECKPOINT_CHANGED',
            {'actual': rows_digest(current['rows']), 'allowed': [rows_digest(r) for r in allowed]})
        # Endpoint acknowledgement is valid only after the complete immutable
        # scope and current fence. An arbitrary failure scene is never adopted.
        self._pending['checkpoint'], self._pending['attempted'] = current['rows'], None
        return current

    def _restore(self):
        self._checkpoint_fence()
        before = self._snapshot['before']
        for vertex in self._snapshot['request']['vertices']:
            actions = K.restore_operations(before['rows'][vertex], self._pending['checkpoint'][vertex], self._snapshot['editable'])
            for group, value in actions:
                self._operation(vertex, group, value)
        self._update()
        self._guard(before['context']['native']['frame'])
        actual = self._collect()
        checks = self._exact(actual, before, 'RESTORE_EXACT_FAILED')
        result = {'status': 'restored_exact', 'checks': checks, 'all_raw_rows_sha256': rows_digest(actual['rows']),
            'author_sha256': C.digest(actual['author']), 'write_count': self.write_count}
        self._pending, self._snapshot = None, None
        return result

    def preview(self, snapshot_token, *, fault=None):
        require(self._pending is None and self._snapshot is not None and snapshot_token == self._snapshot['token'], 'STALE_SNAPSHOT')
        require(fault is None or callable(fault), 'PRIVATE_FAULT_HOOK')
        before = self._snapshot['before']
        self._guard(before['context']['native']['frame'])
        self._exact(self._collect(), before, 'LIVE_SNAPSHOT_CHANGED')
        self._pending = {'token': 'weight_restore_' + uuid.uuid4().hex, 'phase': 'writing',
                         'checkpoint': before['rows'], 'attempted': None, 'after': None}
        try:
            for vertex in self._snapshot['request']['vertices']:
                for group, value in K.operations(before['rows'][vertex], self._snapshot['expected'][vertex]):
                    self._operation(vertex, group, value)
                    if fault:
                        fault(self, 'after_membership')
                        self._checkpoint_fence()  # a trusted fixture callback may still inject external drift
            self._update()
            if fault:
                fault(self, 'after_update')
            after = self._checkpoint_fence()
            require(after['rows'] == self._snapshot['expected'], 'COMPLETE_RAW_ORACLE_MISMATCH')
            require(bool(np.isfinite(after['skin']).all()), 'APPLIED_SKIN_NONFINITE')
            self._pending.update(phase='applied', after=after)
            delta = np.linalg.norm(after['skin'].astype('f8') - before['skin'].astype('f8'), axis=1)
            return {'status': 'previewed', 'restore_token': self._pending['token'],
                'all_raw_rows_oracle_exact': True, 'protected_author_exact': True, 'bones_exact': True,
                'changed_vertices': sum(r['changed'] for r in self._snapshot['plan']['rows']),
                'evaluated_skin_changed': int(np.count_nonzero(delta)), 'skin_max_delta_BU': float(delta.max()) if delta.size else 0.,
                'write_count': self.write_count, 'qualification': 'storage/evaluation response only; visual improvement not certified'}
        except Exception as error:
            try:
                restored = self._restore()
            except Exception as recovery:
                self._pending['phase'] = 'recovery_required'
                raise WeightError('RECOVERY_REQUIRED', {'cause': error_record(error), 'recovery': error_record(recovery),
                    'pending': self.pending, 'instruction': 'Revert only owned fixture drift to the declared checkpoint before retrying restore; retain source/snapshot.'}) from error
            raise WeightError('PREVIEW_FAILED_RESTORED', {'cause': error_record(error), 'restored': restored}) from error

    def validate_applied(self):
        require(self._pending is not None and self._pending['phase'] == 'applied', 'NO_APPLIED_PREVIEW')
        self._guard(self._snapshot['before']['context']['native']['frame'])
        current = self._collect()
        self._exact(current, self._pending['after'], 'APPLIED_STATE_CHANGED')
        return {'status': 'applied_valid', 'author_sha256': C.digest(current['author']),
            'all_raw_rows_sha256': rows_digest(current['rows']), 'bones_sha256': array_digest(current['bones']),
            'skin_sha256': array_digest(current['skin'])}

    def restore(self, token):
        require(self._pending is not None and token == self._pending['token'], 'RESTORE_TOKEN')
        if self._pending['phase'] == 'applied':
            self.validate_applied()
        try:
            return self._restore()
        except Exception:
            self._pending['phase'] = 'recovery_required'
            raise

    def candidate_metadata(self):
        self.validate_applied()
        after = self._pending['after']
        return deepcopy({'schema': 'hrig.weight-workflow-candidate.v1', 'source': self.source,
            'source_resources': self._resources, 'implementation': self._implementation,
            'request': self._snapshot['request'], 'plan': self._snapshot['plan'],
            'full_author_summary': C.summarize(after['author']), 'context': after['context'],
            'all_raw_rows': rows_document(after['rows']), 'all_raw_rows_sha256': rows_digest(after['rows']),
            'original_all_raw_rows': rows_document(self._snapshot['before']['rows']),
            'bone_names': after['bone_names'], 'bones_sha256': array_digest(after['bones']),
            'skin_sha256': array_digest(after['skin']), 'skin_shape': list(after['skin'].shape),
            'runtime': {'version': list(bpy.app.version), 'build': bpy.app.build_hash.decode()},
            'coverage': {'author': after['author']['coverage'], 'consumers': self._snapshot['consumers'],
                'dependencies': 'explicit caller resource list, not an arbitrary dynamic dependency closure'},
            'qualification': 'saved candidate must undergo independent matched-config cold and pose/visual acceptance'})
