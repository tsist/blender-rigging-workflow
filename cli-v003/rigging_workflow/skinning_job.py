# SPDX-License-Identifier: GPL-3.0-or-later
"""One explicit volume domain: frozen background solve, guarded new-Mesh apply.

No Blender data is accessed off the main thread. The child only receives arrays,
bone segments and a copied solver; it cannot replay a scene or commit weights.
"""
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import bpy
import numpy as np

from . import corrective_authoring as signatures, generation, skeleton_gate, skinning

_RUNNING = set()


def _copy_volume_runtime(module_root, runtime):
    from .volume.cuda_backend import artifact_files

    source = module_root / 'volume'
    destination = runtime / 'volume'
    kernel_source, cuda_image, cuda_manifest = artifact_files()
    artifacts = [kernel_source]
    if cuda_image.is_file() and cuda_manifest.is_file():
        artifacts.extend((cuda_image, cuda_manifest))
    files = []
    for path in [*sorted(source.glob('*.py')), *artifacts]:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        files.append(target)
    return files


def _watchdog():
    """Keep owned child handles alive across UI release and .blend file loads.

    Only process/file state is read here; stale scene ID references are unused.
    The timer retires itself once all these exact Popen handles have exited.
    """
    for job in tuple(_RUNNING):
        try:
            job.status()
        except Exception:
            # A malformed/unreadable progress file cannot disable deadline checks:
            # status checks the process deadline before reading progress.
            pass
        if job._process.poll() is not None:
            _RUNNING.discard(job)
    return .25 if _RUNNING else None


def _require(value, message):
    if not value:
        raise ValueError(message)


def _animation_state(owner):
    animation = owner.animation_data
    if animation is None:
        return None
    return {'settings': signatures._rna(animation), 'drivers': signatures._drivers(owner),
            'action_id': animation.action.as_pointer() if animation.action else None,
            'nla': [{'track': signatures._rna(track),
                     'strips': [signatures._rna(strip) for strip in track.strips]}
                    for track in animation.nla_tracks]}


def _proxy_state(proxy):
    return {'mesh': signatures._mesh_signature(proxy.data),
            'matrix': signatures._plain(proxy.matrix_world),
            'evaluated_matrix': signatures._plain(proxy.evaluated_get(
                bpy.context.evaluated_depsgraph_get()).matrix_world),
            'modifiers': signatures._hash([signatures._rna(m) for m in proxy.modifiers]),
            'shape_keys_id': proxy.data.shape_keys.as_pointer() if proxy.data.shape_keys else None,
            'parent_id': proxy.parent.as_pointer() if proxy.parent else None,
            'parent_type': proxy.parent_type, 'parent_bone': proxy.parent_bone,
            'parent_inverse': signatures._plain(proxy.matrix_parent_inverse),
            'constraints': signatures._hash([signatures._rna(c) for c in proxy.constraints]),
            'animation': signatures._hash(_animation_state(proxy)),
            'mesh_animation': signatures._hash(_animation_state(proxy.data))}


def _source_check(source, rig, proxy, names):
    _require(bpy.context.mode == 'OBJECT', '请先结束编辑、姿态或雕刻模式')
    for obj in (source, rig, proxy):
        _require(obj and obj.name in bpy.context.view_layer.objects and
                 bpy.context.view_layer.objects[obj.name] == obj and not obj.library and
                 not obj.override_library, '需要当前视图层中的本地可编辑对象')
    _require(source.type == proxy.type == 'MESH' and rig.type == 'ARMATURE', '选择生产网格、骨架和闭合代理')
    _require(source.data.shape_keys is None and not source.vertex_groups and not source.parent
             and not source.constraints and not source.animation_data,
             '当前新绑定入口需要无形态键、权重、父级、约束或动画的中立生产网格')
    _require(not any(v.groups for v in source.data.vertices),
             '生产源Mesh仍有孤立变形权重；请先整理无权重独立源，不自动清除旧数据')
    _require(all(m.type == 'SUBSURF' for m in source.modifiers), '生产网格当前只支持原有Subsurf')
    _require(not proxy.modifiers and proxy.data.shape_keys is None,
             '闭合代理需为明确的原始网格；先在独立副本准备代理')
    _require(not source.data.library and not proxy.data.library and not rig.data.library,
             '当前入口不修改或绑定链接数据')
    for obj in (source, rig, proxy):
        matrix = np.asarray(obj.matrix_world, dtype=float)
        _require(np.isfinite(matrix).all() and abs(np.linalg.det(matrix[:3, :3])) > 1e-12,
                 '对象世界变换必须有限且可逆')
    from . import rig_identity
    metadata = rig_identity.read(rig)
    _require(rig.data.pose_position == 'POSE', '先返回骨架姿态显示，再恢复校准中立状态')
    allowed = set(metadata['deforms'])
    _require(isinstance(names, list) and names and all(isinstance(n, str) and n in allowed for n in names),
             '逐项指定本角色最终形变输出骨，不能将控制/机制骨投入求解')
    skinning.bone_segments(rig, names)
    scale = float(bpy.context.scene.unit_settings.scale_length)
    _require(math.isfinite(scale) and scale > 0, '场景单位比例必须为有限正数')
    from . import neutral_binding
    if metadata.get('schema') == 'rigging.native-skeleton.v1':
        from . import native_skeleton
        native_skeleton.check_current(rig, metadata, names, scale)
        return scale
    if metadata.get('schema') == 'rigging.modular.generated.v1':
        from . import modular_neutral
        modular_neutral.check_current(rig, metadata, names, scale)
        return scale
    if neutral_binding.KEY in metadata:
        neutral_binding.check_current(rig, metadata, names, scale)
        return scale
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    linear, endpoint_error = 0.0, 0.0
    for name in names:
        pose, bone = evaluated.pose.bones[name], rig.data.bones[name]
        delta = np.asarray(pose.matrix @ bone.matrix_local.inverted(), dtype=float)
        _require(np.isfinite(delta).all(), '参与骨的求值矩阵包含非有限值')
        linear = max(linear, float(np.max(abs(delta[:3, :3] - np.eye(3)))))
        for current, rest in ((pose.head, bone.head_local), (pose.tail, bone.tail_local)):
            error = np.asarray(rig.matrix_world)[:3, :3] @ (np.asarray(current)-np.asarray(rest))
            endpoint_error = max(endpoint_error, float(np.linalg.norm(error)) * scale)
    # Match the generated rig / candidate's spatial neutral contract. Delta's
    # translation at the world origin amplifies tiny rotation roundoff far away
    # from the bone; measure the actual bone endpoints instead.
    _require(linear <= 1e-5 and endpoint_error <= 1e-5,
             f'参与骨当前不在校准中立状态；先恢复中立再求解（线性{linear:g}，端点{endpoint_error:g}米）')
    return scale


def _points(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        points = np.empty((len(mesh.vertices), 3), dtype=np.float64)
        mesh.vertices.foreach_get('co', points.ravel())
        matrix = np.asarray(evaluated.matrix_world, dtype=float)
        return points @ matrix[:3, :3].T + matrix[:3, 3]
    finally:
        evaluated.to_mesh_clear()


class Job:
    """A live session owns process and real IDs; disk paths grant no write authority."""
    def __init__(self, source, rig, proxy, names, settings, root, *, sample_proxy=False, input_guard=None, sample_proxy_indices=None):
        self._gate_token = skeleton_gate.require_pass(rig, operation='skinning_job.Job')
        self.source, self.rig, self.proxy = source, rig, proxy
        self.session_scene = bpy.context.scene
        self._source_mesh, self._rig_data, self._proxy_mesh = source.data, rig.data, proxy.data
        _require(type(sample_proxy) is bool and (input_guard is None or callable(input_guard)),
                 '内部采样模式与输入保护器无效')
        self._sample_proxy, self._input_guard = sample_proxy, input_guard
        self.bone_names = list(names)
        self.scale = _source_check(source, rig, proxy, self.bone_names)
        self._before = signatures._state(source, rig)
        self._proxy_before = _proxy_state(proxy)
        self._cancelled, self._applied = False, False
        self._process = None
        self._terminal = None
        self._progress = {'phase': 'starting'}
        self._offset, self._pending = 0, b''
        settings = json.loads(json.dumps(settings, allow_nan=False))
        _require(isinstance(settings, dict) and 'partitions' not in settings and not settings.get('return_domain'),
                 '本入口一次求解一个明确代理域，不接收多域或debug体积输出')
        _require(settings.get('require_all_bones_seeded', True) is True,
                 '完整蒙皮候选要求每个声明骨都获得有效种子，不允许空骨列兜底')
        # Validate the same public settings/schema before launching, once at this boundary.
        from .volume.contract import normalize
        vertices, triangles = skinning.mesh_arrays(proxy)
        if sample_proxy_indices is not None:
            ids=np.asarray(sample_proxy_indices)
            _require(sample_proxy and ids.ndim==1 and ids.dtype.kind in 'iu' and len(ids)>0
                     and len(np.unique(ids))==len(ids) and ids.min()>=0 and ids.max()<len(vertices),
                     '代理采样点索引必须明确、唯一并属于冻结代理')
        else: ids=None
        samples, _ = skinning.mesh_arrays(source) if not sample_proxy else (vertices.copy(), None)
        if ids is not None: samples=samples[ids]
        vertices, samples = vertices * self.scale, samples * self.scale
        segments = skinning.bone_segments(rig, self.bone_names)
        for segment in segments:
            for field in ('head', 'tail'):
                segment[field] = (np.asarray(segment[field]) * self.scale).tolist()
        normalize([{'id': 'proxy', 'vertices': vertices, 'triangles': triangles}], segments, settings)
        self.directory = root / uuid.uuid4().hex
        self.directory.mkdir(parents=False, exist_ok=False)
        runtime = self.directory / 'runtime' / 'rigging_workflow'
        (runtime / 'volume').mkdir(parents=True)
        (runtime / '__init__.py').write_text('', encoding='utf8')
        module_root = Path(__file__).resolve().parent
        files = _copy_volume_runtime(module_root, runtime)
        worker = self.directory / 'worker.py'
        shutil.copyfile(module_root / 'skinning_job_worker.py', worker)
        files.extend((worker, runtime / '__init__.py'))
        collection_copy = runtime / 'skeleton_collection.py'
        shutil.copyfile(module_root / 'skeleton_collection.py', collection_copy)
        files.append(collection_copy)
        channel_copy = runtime / 'worker_channel.py'
        shutil.copyfile(module_root / 'worker_channel.py', channel_copy)
        files.append(channel_copy)
        snapshot = self.directory / 'input.npz'
        np.savez_compressed(snapshot, vertices=vertices, triangles=triangles, points=samples)
        files.append(snapshot)
        manifest = {'schema': 'c94.single-domain-volume-job.v1', 'bones': segments, 'settings': settings,
            'coordinates': 'meters', 'scene_scale_length': self.scale,
            'source_vertex_count': len(source.data.vertices), 'sample_vertex_count': len(samples),
            'sample_domain': 'proxy_surface' if sample_proxy else 'production_surface', 'bone_names': self.bone_names,
            'proxy_sample_indices':ids.tolist() if ids is not None else None,
            'files': [{'relative_path': str(p.relative_to(self.directory)),
                       'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
        manifest['quality_gate'] = skeleton_gate.worker_proof(rig, self._gate_token, manifest)
        (self.directory / 'input.json').write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf8')
        self._input_hash = hashlib.sha256((self.directory / 'input.json').read_bytes()).hexdigest()
        (self.directory / 'source-state.json').write_text(json.dumps({'source': source.name, 'rig': rig.name,
            'proxy': proxy.name, 'state': self._before, 'proxy_state': self._proxy_before},
            ensure_ascii=False, allow_nan=False, indent=2), encoding='utf8')
        _require(signatures._state(source, rig) == self._before and _proxy_state(proxy) == self._proxy_before,
                 '捕获期间输入发生变化；未启动后台任务')
        if self._input_guard is not None: self._input_guard()
        with (self.directory / 'worker.log').open('wb') as log:
            self._process = skeleton_gate.spawn_worker(rig, self._gate_token, manifest, [bpy.app.binary_path, '--background', '--factory-startup',
                '--disable-autoexec', '--offline-mode', '--threads', '2', '--python-exit-code', '1',
                '--python', str(worker), '--', str(self.directory)],
                stdout=log, stderr=subprocess.STDOUT, shell=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self._deadline = time.monotonic() + float(settings.get('max_seconds', 60)) + 30

    def status(self):
        if self._applied:
            return {'status': 'applied', 'job_directory': str(self.directory)}
        exit_code = self._process.poll()
        if exit_code is None and time.monotonic() > self._deadline:
            # This exact Popen handle belongs only to this job; no PID discovery or tree kill.
            self._process.terminate()
            self._terminal = {'status': 'failed', 'error': {'code': 'WORKER_DEADLINE',
                'message': '后台进程超过求解预算及30秒启动/退出余量，已停止本次进程'}}
        if self._terminal is not None:
            return {**self._terminal, 'job_directory': str(self.directory)}
        if exit_code is None:
            path = self.directory / 'progress.jsonl'
            if path.exists():
                with path.open('rb') as stream:
                    stream.seek(self._offset)
                    chunk = stream.read()
                    self._offset = stream.tell()
                lines = (self._pending + chunk).split(b'\n')
                self._pending = lines.pop()
                for line in lines:
                    if line:
                        self._progress = json.loads(line.decode('utf8'))
            data = {**self._progress, 'status': 'running'}
        elif self._cancelled:
            data = {'status': 'cancelled', 'phase': 'cancelled'}
        else:
            path = self.directory / 'result.json'
            data = json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
            if exit_code != 0 or data.get('status') not in {'succeeded', 'failed', 'cancelled'}:
                data = {'status': 'failed', 'error': {'code': 'WORKER_EXIT', 'message': '后台求解未正常完成，请查看本次worker.log'}}
        if exit_code is not None:
            self._terminal = data
        return {**data, 'cancel_requested': self._cancelled, 'job_directory': str(self.directory)}

    def cancel(self):
        _require(not self._applied, '候选已创建；本次任务不能再取消候选')
        self._cancelled = True
        self._terminal = None
        try:
            (self.directory / 'cancel.request').touch(exist_ok=True)
        except OSError as exc:
            if self._process.poll() is None:
                self._process.terminate()
            raise RuntimeError('无法写入取消标记；已请求停止本次自有后台进程，监控保留至退出') from exc

    def _check_inputs(self):
        skeleton_gate.check_pass(self.rig, self._gate_token, operation='skinning_job.Job')
        _require(bpy.context.scene == self.session_scene and self.source.data == self._source_mesh
                 and self.rig.data == self._rig_data and self.proxy.data == self._proxy_mesh,
                 '源对象或场景身份已改变；结果不再可提交')
        _source_check(self.source, self.rig, self.proxy, self.bone_names)
        _require(signatures._state(self.source, self.rig) == self._before
                 and _proxy_state(self.proxy) == self._proxy_before, '源网格、骨架、代理、帧或单位已改变；请重新求解')
        if self._input_guard is not None: self._input_guard()

    def _bind_report(self, state):
        return {'schema': 'c94.single-domain-volume-bind.v1', 'job_directory': str(self.directory),
                'source': self.source.name, 'proxy': self.proxy.name, 'bone_names': self.bone_names,
                'statistics': state['statistics'], 'warnings': state['warnings'],
                'scope': 'one explicit closed proxy domain; independent new Mesh; visual review pending'}

    def _read_result_weights(self, state):
        """Accept the exact result buffer and recheck the frozen request inputs."""
        raw = (self.directory / 'input.json').read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == self._input_hash,
                 '本次求解输入记录已改变')
        manifest = json.loads(raw)
        for item in manifest['files']:
            frozen = self.directory / item['relative_path']
            _require(hashlib.sha256(frozen.read_bytes()).hexdigest() == item['sha256'],
                     '冻结数组或求解源码已改变；不能提交旧结果')
        result_bytes = (self.directory / 'weights.npz').read_bytes()
        _require(state.get('bone_names') == self.bone_names and
                 hashlib.sha256(result_bytes).hexdigest() == state.get('weights_sha256'),
                 '结果身份或完整性不匹配')
        with np.load(BytesIO(result_bytes), allow_pickle=False) as arrays:
            return arrays['weights'].copy()

    def apply(self, *, name, _postprocess=None):
        skeleton_gate.check_pass(self.rig, self._gate_token, operation='skinning_job.Job')
        state = self.status()
        _require(state['status'] == 'succeeded' and not self._cancelled, '需要已成功且未取消的完整求解结果')
        _require(isinstance(name, str) and name.strip() and name not in bpy.data.objects,
                 '请提供尚未使用的候选名称')
        self._check_inputs()
        _require(not self._sample_proxy or callable(_postprocess),
                 '代理表面采样必须完成显式生产顶点回写，不能直接应用代理点行')
        weights = self._read_result_weights(state)
        before_points = _points(self.source)
        candidate = None
        try:
            report = self._bind_report(state)
            names, method = list(self.bone_names), 'volume_diffusion'
            postprocessed = False
            if _postprocess is not None:
                _require(callable(_postprocess), '内部候选处理器必须为明确可信的调用')
                transformed = _postprocess(weights.copy(), list(names), dict(report))
                _require(isinstance(transformed, dict) and set(transformed) == {'weights', 'bone_names', 'report', 'method'},
                         '内部候选处理器必须返回完整权重、骨列表、报告与方法')
                weights, names, report, method = (transformed[key] for key in ('weights', 'bone_names', 'report', 'method'))
                _require(isinstance(report, dict) and isinstance(method, str) and bool(method), '内部候选报告或方法无效')
                self._check_inputs()
                postprocessed = True
            candidate = skinning.create_bound_copy(self.source, self.rig, weights, names,
                                                    name=name, report=report, method=method)
            bpy.context.view_layer.update()
            after_points = _points(candidate)
            _require(before_points.shape == after_points.shape, '候选求值拓扑与生产源不同')
            error = float(np.linalg.norm(before_points - after_points, axis=1).max(initial=0)) * self.scale
            _require(error <= 1e-5, '候选中立求值发生跳变；本次候选已回滚')
            self._check_inputs()
            receipt = {'ok': True, 'candidate': candidate.name, 'neutral_error_meters': error,
                       'source_unchanged': True, 'weights_sha256': state['weights_sha256']}
            if postprocessed:
                receipt.update(final_bone_names=names, method=method,
                    final_weights_sha256=hashlib.sha256(np.asarray(weights, dtype='<f4').tobytes()).hexdigest())
            (self.directory / 'apply-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
        except Exception:
            if candidate is not None:
                mesh = candidate.data
                bpy.data.objects.remove(candidate, do_unlink=True)
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            raise
        self._applied = True
        return candidate


def _monitor(job):
    _RUNNING.add(job)
    try:
        if not bpy.app.timers.is_registered(_watchdog):
            bpy.app.timers.register(_watchdog, first_interval=.25, persistent=True)
    except Exception:
        if job._process.poll() is None:
            job._process.terminate()
            job._process.wait(timeout=5)
        _RUNNING.discard(job)
        raise
    return job


def start(source, rig, proxy, bone_names, settings, *, jobs_directory, _sample_proxy=False, _input_guard=None,
          _sample_proxy_indices=None):
    skeleton_gate.require_pass(rig, operation='skinning_job.start')
    root = Path(jobs_directory).expanduser()
    _require(root.is_absolute(), '后台任务目录必须为明确绝对路径')
    root.mkdir(parents=True, exist_ok=True)
    return _monitor(Job(source, rig, proxy, bone_names, settings, root.resolve(),
                        sample_proxy=_sample_proxy, input_guard=_input_guard, sample_proxy_indices=_sample_proxy_indices))
