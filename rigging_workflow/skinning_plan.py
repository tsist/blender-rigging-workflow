# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit overlapping volume domains and rigid RAW scopes for a new binding.

The source stays untouched. A separate captured evaluated surface hosts masks;
the worker solves those application points, composes their fields, then performs
bounded barycentric transfer to the original RAW topology.
"""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import bpy
import numpy as np

from . import corrective_authoring as signatures, skinning, skinning_job
from .volume.contract import normalize
from .volume.geometry import validate_closed
from .skinning_fields import surface_projection

_TOKEN = 'c94_volume_surface_id_v1'
_SOURCE = 'c94_volume_surface_source_v1'
_SOURCE_MESH = 'c94_volume_surface_source_mesh_v1'
_OWN_MESH = 'c94_volume_surface_own_mesh_v1'
_SCHEMA = 'c94_volume_surface_schema_v1'
_require = skinning_job._require


def _evaluated_arrays(source):
    obj = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = obj.to_mesh()
    try:
        points = np.empty((len(mesh.vertices), 3), dtype=np.float64)
        mesh.vertices.foreach_get('co', points.ravel())
        matrix = np.asarray(obj.matrix_world, dtype=float)
        points = points @ matrix[:3, :3].T + matrix[:3, 3]
        mesh.calc_loop_triangles()
        triangles = np.asarray([tuple(t.vertices) for t in mesh.loop_triangles], dtype=np.int64)
        return points, triangles
    finally:
        obj.to_mesh_clear()


def prepare_surface(source, *, name):
    _require(bpy.context.mode == 'OBJECT' and source and source.type == 'MESH'
             and source.name in bpy.context.view_layer.objects, '先选择对象模式中的本地生产源网格')
    _require(not source.library and not source.data.library and not source.override_library
             and not source.data.shape_keys and not source.vertex_groups and
             all(m.type == 'SUBSURF' for m in source.modifiers), '捕获表面需要无已有Key/权重的新源，只支持原有Subsurf')
    _require(not any(v.groups for v in source.data.vertices), '生产源Mesh仍有孤立变形权重，不能捕获为新绑定源')
    _require(isinstance(name, str) and name.strip() and name not in bpy.data.objects, '捕获表面名称必须明确且尚未使用')
    before = signatures._mesh_signature(source.data)
    obj = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
    temporary = obj.to_mesh(preserve_all_data_layers=True, depsgraph=bpy.context.evaluated_depsgraph_get())
    mesh, surface = None, None
    try:
        mesh = temporary.copy()
        matrix = obj.matrix_world.copy()
    finally:
        obj.to_mesh_clear()
    try:
        mesh.name = name + '__网格'
        surface = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(surface)
        surface.matrix_world = matrix
        token = uuid.uuid4().hex
        surface[_SCHEMA] = 'c94.volume-surface.v1'
        surface[_TOKEN] = token
        mesh[_TOKEN] = token
        surface[_SOURCE], surface[_SOURCE_MESH], surface[_OWN_MESH] = source, source.data, mesh
        _require(signatures._mesh_signature(source.data) == before, '捕获期间生产源发生变化')
        return surface
    except Exception:
        if surface is not None:
            bpy.data.objects.remove(surface, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise


def validate_surface(source, surface):
    """Check ownership only; full current evaluated geometry is checked at start."""
    _require(source and surface and source.type == surface.type == 'MESH' and surface != source,
             '选择本工具捕获的独立采样表面')
    _require(surface.get(_SCHEMA) == 'c94.volume-surface.v1' and surface.get(_TOKEN)
             and surface.data.get(_TOKEN) == surface[_TOKEN] and surface.get(_SOURCE) == source
             and surface.get(_SOURCE_MESH) == source.data and surface.get(_OWN_MESH) == surface.data,
             '采样表面的真实来源或自有Mesh引用已失效，请重新捕获')
    _require(not surface.library and not surface.data.library and not surface.override_library
             and not surface.data.shape_keys and not surface.modifiers and not surface.parent
             and not surface.constraints and not surface.animation_data and surface.data != source.data,
             '采样表面必须保持本地独立原始网格')
    consumers = [obj for obj in bpy.data.objects if obj.type == 'MESH' and obj.data == surface.data]
    _require(consumers == [surface], '采样表面Mesh已被其它对象共享；不能写入或使用其范围')


def _ids(value, count):
    raw = np.asarray(value)
    _require(raw.ndim == 1 and raw.dtype.kind in 'iu' and raw.dtype.kind != 'b', '刚性范围需要明确整数顶点ID')
    ids = np.asarray(raw, dtype=np.int64)
    _require(len(ids) and (ids >= 0).all() and (ids < count).all() and len(np.unique(ids)) == len(ids),
             '刚性范围为空、重复或越过原网格')
    return ids


def _name(value):
    _require(isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value),
             '计划行名称必须明确且无控制字符')
    return value


def _collect(source, rig, surface, domains, rigid, distance):
    _require(type(distance) in (int, float) and math.isfinite(distance) and distance > 0,
             '转移距离上限必须为明确的有限正米值')
    validate_surface(source, surface)
    _require(isinstance(domains, list) and 1 <= len(domains) <= 32 and isinstance(rigid, list),
             '需要1至32个明确体积域和刚性范围列表')
    _require(all(isinstance(row, dict) and set(row) == {'name', 'proxy', 'bone_names', 'mask_attribute', 'settings'} for row in domains),
             '体积域字段需要name/proxy/bone_names/mask_attribute/settings')
    _require(all(isinstance(row, dict) and set(row) == {'name', 'bone_name', 'vertex_indices'} for row in rigid),
             '刚性范围字段需要name/bone_name/vertex_indices')
    labels = [_name(row['name']) for row in domains + rigid]
    _require(len(set(labels)) == len(labels), '计划中的体积域和刚性行名称不得重复')
    names = []
    for row in domains:
        _require(isinstance(row['bone_names'], list) and row['bone_names'] and
                 all(isinstance(n, str) for n in row['bone_names']), '每个域都需要明确输出骨列表')
        for name in row['bone_names']:
            if name not in names:
                names.append(name)
    for row in rigid:
        _require(isinstance(row['bone_name'], str), '刚性范围需要明确最终输出骨')
        if row['bone_name'] not in names:
            names.append(row['bone_name'])
    scale = skinning_job._source_check(source, rig, surface, names)
    points, triangles = skinning.mesh_arrays(surface)
    actual_points, actual_triangles = _evaluated_arrays(source)
    _require(np.array_equal(points, actual_points) and np.array_equal(triangles, actual_triangles),
             '采样表面已不对应当前源的真实求值顶点/三角顺序，请重新捕获并指定范围')
    raw_points, _ = skinning.mesh_arrays(source)
    total = np.zeros(len(points), dtype=np.float64)
    memberships = np.zeros(len(points), dtype=np.int32)
    records, clean_rigid = [], []
    for row in domains:
        proxy = row['proxy']
        # Same supported scene/bone/transform boundary as the single-domain entry.
        skinning_job._source_check(source, rig, proxy, row['bone_names'])
        attribute = surface.data.attributes.get(row['mask_attribute'])
        _require(attribute is not None and attribute.domain == 'POINT' and attribute.data_type == 'FLOAT',
                 '体积域范围必须是采样表面的POINT/FLOAT属性：' + row['name'])
        blend = np.empty(len(points), dtype=np.float32)
        attribute.data.foreach_get('value', blend)
        _require(np.isfinite(blend).all() and (blend >= 0).all() and (blend <= 1).all(),
                 '域过渡权重必须有限且在0至1之间：' + row['name'])
        ids = np.flatnonzero(blend > 0)
        _require(len(ids), '域范围为空：' + row['name'])
        total[ids] += blend[ids]
        memberships[ids] += 1
        settings = json.loads(json.dumps(row['settings'], allow_nan=False))
        _require(isinstance(settings, dict) and 'partitions' not in settings and not settings.get('return_domain')
                 and settings.get('require_all_bones_seeded', True) is True,
                 '每域保持明确单体积设置及全部骨有种子，不接受隐式union或debug输出')
        vertices, tris = skinning.mesh_arrays(proxy)
        bones = skinning.bone_segments(rig, row['bone_names'])
        for bone in bones:
            for key in ('head', 'tail'):
                bone[key] = (np.asarray(bone[key]) * scale).tolist()
        normalize([{'id': 'proxy', 'vertices': vertices * scale, 'triangles': tris}], bones, settings)
        closed = validate_closed(vertices * scale, tris)
        records.append({'name': row['name'], 'proxy': proxy, 'bone_names': list(row['bone_names']),
            'mask_attribute': row['mask_attribute'], 'settings': settings, 'bones': bones,
            'vertices': vertices * scale, 'triangles': tris, 'source_ids': ids,
            'blend': blend[ids].astype(np.float64), 'closed': closed})
    covered = total > 0
    error = float(np.max(abs(total[covered] - 1), initial=0))
    _require(covered.any() and error <= 1e-5, '正范围内的全部域过渡权重之和须为1，不能隐式修正重叠')
    valid_triangles = np.flatnonzero(covered[triangles].all(axis=1))
    _require(len(valid_triangles), '域范围没有完整覆盖任何真实表面三角')
    represented = np.zeros(len(points), dtype=bool)
    represented[triangles[valid_triangles].ravel()] = True
    for row in records:
        ids = row['source_ids']
        _require(represented[ids].all(),
                 '域mask有正值点未进入任何完整可转移三角，请调整范围：' + row['name'])
        membership = np.zeros(len(points), dtype=bool)
        membership[ids] = True
        row['participating_triangles'] = int(membership[triangles[valid_triangles]].any(axis=1).sum())
    rigid_used = np.zeros(len(raw_points), dtype=bool)
    for row in rigid:
        ids = _ids(row['vertex_indices'], len(raw_points))
        _require(not rigid_used[ids].any(), '刚性范围存在重复分配：' + row['name'])
        rigid_used[ids] = True
        clean_rigid.append({'name': row['name'], 'bone_name': row['bone_name'], 'vertex_indices': ids})
    volume_ids = np.flatnonzero(~rigid_used)
    _require(len(volume_ids), '本入口需要非空体积目标；全部刚性部件另行配置')
    projection_ids, barycentric, projection = surface_projection(raw_points[volume_ids] * scale, points * scale,
        triangles[valid_triangles], max_distance=distance)
    for row in records:
        blend = np.zeros(len(points), dtype=np.float64)
        blend[row['source_ids']] = row['blend']
        contribution = np.sum(blend[projection_ids] * barycentric, axis=1)
        row['transfer_target_vertices'] = int(np.count_nonzero(contribution > 0))
        _require(row['transfer_target_vertices'] > 0,
                 '体积域未向任何RAW体积目标贡献权重，请调整域或刚性范围：' + row['name'])
    summary = {'schema': 'c94.multi-domain-plan.v1', 'source': source.name, 'rig': rig.name,
        'surface': surface.name, 'source_vertices': len(raw_points), 'surface_vertices': len(points),
        'domain_count': len(records), 'bone_count': len(names), 'bone_names': names,
        'volume_target_vertices': len(volume_ids), 'rigid_vertices': int(rigid_used.sum()),
        'covered_surface_vertices': int(covered.sum()), 'uncovered_surface_vertices': int((~covered).sum()),
        'overlap_surface_vertices': int((memberships > 1).sum()), 'blend_sum_max_error': error,
        'transfer_surface_triangles': len(valid_triangles), 'transfer_distance_meters': float(distance),
        'raw_transfer_preflight': projection,
        'domains': [{'name': row['name'], 'proxy': row['proxy'].name, 'bone_names': row['bone_names'],
            'application_points': len(row['source_ids']), 'mask_attribute': row['mask_attribute'],
            'participating_triangles': row['participating_triangles'],
            'transfer_target_vertices': row['transfer_target_vertices'],
            'settings': row['settings'], 'closed_surface': row['closed']} for row in records],
        'rigid': [{'name': row['name'], 'bone_name': row['bone_name'], 'vertices': len(row['vertex_indices'])} for row in clean_rigid],
        'coordinates': 'meters', 'scene_scale_length': scale,
        'scope': 'new independent binding; explicit volume domains and rigid scopes; visual review pending'}
    return {'summary': summary, 'domains': records, 'rigid': clean_rigid, 'scale': scale,
        'surface_vertices': points * scale, 'surface_vertices_world': points, 'surface_triangles': triangles,
        'valid_triangles': valid_triangles, 'raw_vertices': raw_points * scale,
        'volume_ids': volume_ids, 'bone_names': names}


def check(source, rig, surface, domains, rigid, *, transfer_distance_meters=.01):
    return _collect(source, rig, surface, domains, rigid, transfer_distance_meters)['summary']


class MultiJob(skinning_job.Job):
    def __init__(self, source, rig, surface, data, root):
        self.source, self.rig, self.proxy = source, rig, surface
        self.session_scene = bpy.context.scene
        self._source_mesh, self._rig_data, self._proxy_mesh = source.data, rig.data, surface.data
        self.bone_names, self.scale = list(data['bone_names']), data['scale']
        self.summary = data['summary']
        self._surface_points = data['surface_vertices_world'].copy()
        self._surface_triangles = data['surface_triangles'].copy()
        self._before = signatures._state(source, rig)
        self._proxy_before = skinning_job._proxy_state(surface)
        self._domain_refs = [(row['proxy'], row['proxy'].data, skinning_job._proxy_state(row['proxy'])) for row in data['domains']]
        self._cancelled, self._applied, self._terminal = False, False, None
        self._progress, self._offset, self._pending = {'phase': 'starting'}, 0, b''
        self.directory = root / uuid.uuid4().hex
        self.directory.mkdir(exist_ok=False)
        runtime = self.directory/'runtime'/'rigging_workflow'
        (runtime/'volume').mkdir(parents=True)
        (runtime/'__init__.py').write_text('', encoding='utf8')
        package = Path(__file__).resolve().parent
        files = [runtime/'__init__.py']
        for path in [*(package/'volume').glob('*.py'), package/'skinning_fields.py']:
            destination = runtime/'volume'/path.name if path.parent.name == 'volume' else runtime/path.name
            shutil.copyfile(path, destination)
            files.append(destination)
        worker = self.directory/'worker.py'
        shutil.copyfile(package/'skinning_plan_worker.py', worker)
        files.append(worker)
        arrays = {key: data[key] for key in ('surface_vertices', 'surface_triangles', 'valid_triangles', 'raw_vertices', 'volume_ids')}
        rows = []
        for index, row in enumerate(data['domains']):
            prefix = 'd' + str(index)
            for key in ('vertices', 'triangles', 'source_ids', 'blend'):
                arrays[prefix+'_'+key] = row[key]
            rows.append({key: row[key] for key in ('name', 'bone_names', 'bones', 'settings')})
        rigid_rows = []
        for index, row in enumerate(data['rigid']):
            arrays['r'+str(index)+'_ids'] = row['vertex_indices']
            rigid_rows.append({'name': row['name'], 'bone_name': row['bone_name']})
        archive = self.directory/'input.npz'
        np.savez_compressed(archive, **arrays)
        files.append(archive)
        manifest = {'schema': 'c94.multi-domain-job.v1', 'summary': self.summary,
            'bone_names': self.bone_names, 'domains': rows, 'rigid': rigid_rows,
            'files': [{'relative_path': str(p.relative_to(self.directory)), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
        (self.directory/'input.json').write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf8')
        self._input_hash = hashlib.sha256((self.directory/'input.json').read_bytes()).hexdigest()
        (self.directory/'source-state.json').write_text(json.dumps({'state': self._before,
            'surface_state': self._proxy_before, 'summary': self.summary}, ensure_ascii=False, indent=2), encoding='utf8')
        self._check_inputs()
        with (self.directory/'worker.log').open('wb') as log:
            self._process = subprocess.Popen([bpy.app.binary_path, '--background', '--factory-startup',
                '--disable-autoexec', '--offline-mode', '--threads', '2', '--python-exit-code', '1',
                '--python', str(worker), '--', str(self.directory)], stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, shell=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self._deadline = time.monotonic()+sum(float(row['settings'].get('max_seconds', 60)) for row in data['domains'])+60

    def _check_inputs(self):
        super()._check_inputs()
        validate_surface(self.source, self.proxy)
        points, triangles = _evaluated_arrays(self.source)
        _require(np.array_equal(points, self._surface_points) and np.array_equal(triangles, self._surface_triangles),
                 '源的实际求值表面已改变；本次映射过时，请重新捕获并求解')
        for obj, mesh, before in self._domain_refs:
            _require(obj.data == mesh and obj.name in bpy.context.view_layer.objects
                     and skinning_job._proxy_state(obj) == before, '体积域代理已改变；本次结果不能提交')

    def _bind_report(self, state):
        return {'schema': 'c94.multi-domain-bind.v1', 'job_directory': str(self.directory),
            'source': self.source.name, 'bone_names': self.bone_names, 'plan': self.summary,
            'statistics': state['statistics'], 'warnings': state['warnings'],
            'method': 'explicit_domain_volume_diffusion_then_bounded_barycentric_raw_transfer'}


def start(source, rig, surface, domains, rigid, *, jobs_directory, transfer_distance_meters=.01):
    root = Path(jobs_directory).expanduser()
    _require(root.is_absolute(), '后台任务目录必须为明确绝对路径')
    root.mkdir(parents=True, exist_ok=True)
    data = _collect(source, rig, surface, domains, rigid, transfer_distance_meters)
    return skinning_job._monitor(MultiJob(source, rig, surface, data, root.resolve()))
