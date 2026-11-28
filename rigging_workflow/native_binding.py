# SPDX-License-Identifier: GPL-3.0-or-later
"""One explicit NATIVE volume domain, supported-proxy samples, bounded RAW transfer.

The existing Job owns the live permit, child and candidate transaction. This
adapter creates neither a proxy nor intermediate groups, and never allocates
modular helpers. All original RAW rows and all real deform columns must survive.
"""
import hashlib
import json
import math
from pathlib import Path

import bpy
import numpy as np

from . import modular_workflow, rig_identity, skeleton_gate, skinning, skinning_job


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _array_digest(array):
    header = json.dumps({'shape': list(array.shape), 'dtype': array.dtype.str}, sort_keys=True).encode('ascii')
    return hashlib.sha256(header + b'\n' + array.tobytes(order='C')).hexdigest()


class _NativeBindingJob:
    """Only instantiated by start after its existing public scope check."""
    def __init__(self, source, rig, proxy, names, settings, jobs_directory, cells, token):
        self.source, self.rig, self.proxy = source, rig, proxy
        self.bone_names = list(names)
        self._gate_token = token
        self._settings = json.loads(json.dumps(settings, allow_nan=False))
        self._transfer_distance_cells = float(cells)
        self.candidate, self.failure = None, None
        # This validates the actual local source, native neutral rig and proxy
        # before any support-domain work; the native Job rechecks at capture.
        self.scale = skinning_job._source_check(source, rig, proxy, self.bone_names)
        vertices, triangles = skinning.mesh_arrays(proxy)
        from .volume.contract import normalize
        segments = skinning.bone_segments(rig, self.bone_names)
        for segment in segments:
            for field in ('head', 'tail'):
                segment[field] = (np.asarray(segment[field]) * self.scale).tolist()
        _, _, cfg, _ = normalize([{'id': 'proxy', 'vertices': vertices * self.scale,
                                  'triangles': triangles}], segments, self._settings)
        self._cell_size_meters = float(cfg['cell_size'] or
            np.ptp(vertices * self.scale, axis=0).max() / cfg['cells_longest'])
        self._transfer_distance = self._transfer_distance_cells * self._cell_size_meters / self.scale
        _require(math.isfinite(self._transfer_distance) and self._transfer_distance > 0,
                 '原cell_size与单位比例不能形成有限正数转移界限')
        proxy_before = skinning_job._proxy_state(proxy)
        indices, support = modular_workflow._field_samples(proxy, rig, self.bone_names, self._settings)
        _require(skinning_job._proxy_state(proxy) == proxy_before,
                 '支持域捕获期间代理发生变化，未启动任务')
        self._field_indices = np.array(indices, dtype=np.int64, copy=True)
        _require(self._field_indices.ndim == 1 and len(self._field_indices) > 0
                 and len(np.unique(self._field_indices)) == len(self._field_indices)
                 and self._field_indices.min() >= 0 and self._field_indices.max() < len(vertices),
                 '真实代理支持采样索引无效')
        remap = np.full(len(vertices), -1, dtype=np.int64)
        remap[self._field_indices] = np.arange(len(self._field_indices), dtype=np.int64)
        supported = (remap[triangles] >= 0).all(axis=1)
        self._supported_triangle_rows = np.flatnonzero(supported).astype(np.int64)
        self._supported_proxy_triangles = np.array(triangles[supported], dtype=np.int64, copy=True)
        self._sample_triangles = remap[self._supported_proxy_triangles]
        _require(len(self._sample_triangles) > 0, '没有三个角均受体素支持的完整代理三角')
        for array in (self._field_indices, self._supported_triangle_rows,
                      self._supported_proxy_triangles, self._sample_triangles):
            array.setflags(write=False)
        self._field_sha = _array_digest(self._field_indices)
        self._triangle_sha = _digest({
            'original_triangle_rows': _array_digest(self._supported_triangle_rows),
            'original_proxy_ids': _array_digest(self._supported_proxy_triangles),
            'sample_ids': _array_digest(self._sample_triangles)})
        self._field_report = json.loads(json.dumps(support, allow_nan=False))
        self._policy_sha = _digest(self._policy())
        self.native = skinning_job.start(
            source, rig, proxy, self.bone_names, self._settings,
            jobs_directory=jobs_directory, _sample_proxy=True,
            _sample_proxy_indices=self._field_indices,
            _input_guard=self._check_transfer_inputs)
        try:
            _require(self.native._gate_token is self._gate_token,
                     '准备期间原件许可已更换，不能继续旧策略')
            self._support_path = self.native.directory/'native-field-support.json'
            record = {
                'schema': 'rigging.native-single-domain.surface-support.v1',
                'rig_identity': 'rigging.native-skeleton.v1', 'source': source.name, 'proxy': proxy.name,
                'field_support': self._field_report, 'policy': self._policy(),
                'policy_sha256': self._policy_sha,
                'supported_proxy_indices_sha256': self._field_sha,
                'supported_triangles_sha256': self._triangle_sha,
                'source_vertex_count': len(source.data.vertices),
                'source_row_order': 'unchanged original RAW vertices',
                'proxy_row_order': 'input.json proxy_sample_indices; original proxy RAW vertex ids',
                'input_manifest_sha256': self.native._input_hash,
                'input_arrays_sha256': hashlib.sha256((self.native.directory/'input.npz').read_bytes()).hexdigest(),
                'source_state_sha256': hashlib.sha256((self.native.directory/'source-state.json').read_bytes()).hexdigest(),
                'proxy_geometry_sha256': skinning.geometry_fingerprint(proxy),
                'source_geometry_sha256': skinning.geometry_fingerprint(source),
                'single_explicit_source_domain': True, 'helper_allocation': False,
                'transaction_state': 'sampling_policy_only_not_a_quality_receipt'}
            with self._support_path.open('x', encoding='utf8') as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            self._support_file_sha = hashlib.sha256(self._support_path.read_bytes()).hexdigest()
            self._check_transfer_inputs()
        except Exception:
            self.native.cancel()
            raise

    def _policy(self):
        return {'method': 'supported_proxy_volume_field_to_complete_RAW_barycentric',
                'bone_names': self.bone_names, 'settings_unchanged': self._settings,
                'scene_scale_length': self.scale, 'cell_size_meters': self._cell_size_meters,
                'transfer_distance_cells': self._transfer_distance_cells,
                'transfer_distance_world_units': self._transfer_distance,
                'transfer_distance_meters': self._transfer_distance * self.scale,
                'supported_proxy_indices_sha256': self._field_sha,
                'supported_triangles_sha256': self._triangle_sha,
                'field_support': self._field_report, 'helper_allocation': False,
                'uncovered_rows': 'reject_entire_candidate', 'automatic_distance_expansion': False}

    def _check_transfer_inputs(self):
        """Native Job calls this before spawn and at its repeated input checks."""
        _require(float(bpy.context.scene.unit_settings.scale_length) == self.scale,
                 '单位比例已改变，不能重解释固定转移距离')
        _require(_array_digest(self._field_indices) == self._field_sha,
                 '代理支持采样行映射已改变，不能提交旧结果')
        triangles_sha = _digest({
            'original_triangle_rows': _array_digest(self._supported_triangle_rows),
            'original_proxy_ids': _array_digest(self._supported_proxy_triangles),
            'sample_ids': _array_digest(self._sample_triangles)})
        _require(triangles_sha == self._triangle_sha and _digest(self._policy()) == self._policy_sha,
                 '受支持完整三角或固定转移策略已改变，不能提交旧结果')
        if hasattr(self, '_support_file_sha'):
            _require(hashlib.sha256(self._support_path.read_bytes()).hexdigest() == self._support_file_sha,
                     '持久支持域/策略证据已改变，不能提交旧结果')
        if hasattr(self, '_projection_file_sha'):
            _require(hashlib.sha256(self._projection_path.read_bytes()).hexdigest() == self._projection_file_sha,
                     'RAW投影证据已改变，不能提交旧结果')

    def _check_inputs(self):
        return self.native._check_inputs()

    def status(self):
        state = self.native.status()
        if self.failure:
            state.update(native_status=state['status'], native_error=state.get('error'),
                         status='failed', error=self.failure)
        state['candidate'] = self.candidate.name if self.candidate else None
        state['method'] = 'native_volume_proxy_surface_transfer'
        return state

    def cancel(self):
        return self.native.cancel()

    def apply(self, *, name):
        _require(self.candidate is None and self.failure is None, '本次候选已处理，请建立明确的新任务')

        def postprocess(weights, names, report):
            # No new scope: Job.apply authenticates its exact live Permit and
            # calls _check_inputs before/after this trusted conversion.
            self._check_transfer_inputs()
            _require(names == self.bone_names and np.asarray(weights).shape ==
                     (len(self._field_indices), len(self.bone_names)),
                     '原生结果不是完整支持采样行×原骨列，拒绝补列或辅助分配')
            points, _ = skinning.mesh_arrays(self.source)
            vertices, triangles = skinning.mesh_arrays(self.proxy)
            _require(np.array_equal(triangles[self._supported_triangle_rows], self._supported_proxy_triangles),
                     '真实代理三角顺序已改变，不能消费旧映射')
            samples = vertices[self._field_indices]
            distance = self._transfer_distance

            def record_projection(ids, barycentric, details):
                projected = np.einsum('ij,ijk->ik', barycentric, samples[ids])
                distances = np.linalg.norm(projected - points, axis=1)
                _require(len(ids) == len(points) and np.isfinite(distances).all()
                         and (distances <= distance).all(),
                         '至少一个RAW原点未满足固定实际投影距离，拒绝整个候选')
                path = self.native.directory/'native-transfer-projection.npz'
                with path.open('xb') as stream:
                    np.savez_compressed(
                        stream, source_vertex_ids=np.arange(len(points), dtype=np.int64),
                        source_points_world=points, sample_vertex_ids=ids,
                        proxy_vertex_ids=self._field_indices[ids],
                        sample_to_original_proxy_vertex=self._field_indices,
                        supported_original_triangle_rows=self._supported_triangle_rows,
                        supported_original_proxy_triangles=self._supported_proxy_triangles,
                        barycentric=barycentric, distances_world_units=distances,
                        distances_meters=distances * self.scale,
                        transaction_state=np.asarray('projection_only_not_a_submission_receipt'))
                self._projection_path = path
                self._projection_file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                details['projection_artifact'] = {
                    'file': str(path), 'sha256': self._projection_file_sha,
                    'rows': len(points), 'source_row_order': 'unchanged original RAW vertices',
                    'sample_vertex_ids_order': 'input.json proxy_sample_indices',
                    'proxy_vertex_ids_order': 'original proxy RAW vertices',
                    'distance_bound_world_units': distance,
                    'actual_reconstructed_max_distance_world_units': float(distances.max(initial=0)),
                    'distance_bound_meters': distance * self.scale,
                    'transaction_state': 'projection_only_not_a_submission_receipt',
                    'created_before_candidate_commit': True}

            transferred, transfer = skinning.transfer_surface_field(
                points, samples, self._sample_triangles, weights,
                max_distance=distance, projection_recorder=record_projection)
            _require(transferred.shape == (len(self.source.data.vertices), len(self.bone_names)),
                     '表面回写未覆盖全部RAW原顶点和原骨列')
            transfer.update(coordinate_space='Blender world units',
                            distance_bound_meters=distance * self.scale,
                            transfer_distance_cells=self._transfer_distance_cells)
            report.update(native_volume_report_schema=report.get('schema'),
                          schema='rigging.native-single-domain.surface-bind.v1',
                          rig_identity='rigging.native-skeleton.v1',
                          final_bone_names=list(names), helper_allocation=False,
                          proxy_field_transfer=transfer, proxy_surface_support=self._field_report,
                          transfer_policy=self._policy(), transfer_policy_sha256=self._policy_sha,
                          support_artifact={'file': str(self._support_path), 'sha256': self._support_file_sha})
            return {'weights': transferred, 'bone_names': list(names), 'report': report,
                    'method': 'native_volume_proxy_surface_transfer'}

        try:
            candidate = self.native.apply(name=name, _postprocess=postprocess)
            self.candidate = candidate
            return candidate
        except Exception as error:
            self.failure = {'code': 'NATIVE_SURFACE_TRANSFER_REJECTED', 'message': str(error)}
            raise


def start(source, rig, proxy, bone_names, settings, *, jobs_directory, transfer_distance_cells=3):
    """Use the existing single-domain permit; no implicit proxy or scope expansion."""
    token = skeleton_gate.require_pass(rig, operation='skinning_job.start')
    _require(type(transfer_distance_cells) in (int, float) and math.isfinite(transfer_distance_cells)
             and .5 <= transfer_distance_cells <= 4,
             '请明确0.5–4格的固定转移距离，不自动扩大上限')
    _require(Path(jobs_directory).expanduser().is_absolute(), '请明确原生单域作业的绝对目录')
    metadata = rig_identity.read(rig)
    _require(metadata.get('schema') == 'rigging.native-skeleton.v1', '本桥接只接受显式独立NATIVE身份')
    _require(isinstance(bone_names, list) and bone_names
             and all(isinstance(name, str) for name in bone_names)
             and len(set(bone_names)) == len(bone_names)
             and set(bone_names) == set(metadata['deforms']),
             'NATIVE单域桥接必须保留全部真实变形骨，不能过滤原骨列')
    _require(isinstance(settings, dict) and 'partitions' not in settings and not settings.get('return_domain'),
             '请保留同一明确单域设置，不接受分域或debug体积输出')
    _require(settings.get('require_all_bones_seeded', True) is True,
             '所有原骨列均须获得有效种子，不允许空骨兜底')
    modular_workflow._source(source)
    return _NativeBindingJob(source, rig, proxy, bone_names, settings, jobs_directory,
                             transfer_distance_cells, token)
