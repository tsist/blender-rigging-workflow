# SPDX-License-Identifier: GPL-3.0-or-later
"""Inspect and execute reviewed selected-bone skinning on a saved working copy."""

import argparse
from importlib import import_module
import json
import math
from pathlib import Path
import subprocess
import sys
import time


CLI = Path(__file__).resolve()
PACKAGE = CLI.parents[1]
sys.path.insert(0, str(CLI.parent))
from rigging_candidate import (
    AdapterError, BUILD, absolute_file, checked_descriptor, file_digest,
    read_json, require, write_json,
)


SCHEMA = 'rigging.selected_skinning_inspect.v1'
EXECUTE_SCHEMA = 'rigging.selected_skinning_execute.v1'
EXECUTE_REVIEW_AND_READBACK_SECONDS = 600
REQUEST_FIELDS = {
    'schema', 'source', 'scene', 'view_layer', 'target', 'rig', 'proxy', 'selected_bones',
    'settings', 'candidate_name',
}
SELECTION_FIELDS = {'vertex_indices', 'alpha', 'radius_scale', 'context_bones'}
TARGET_FIELDS = {'target', 'proxy', 'candidate_name'}


def _target_requests(request):
    shared = {key: value for key, value in request.items() if key != 'targets'}
    return [{**shared, **target} for target in request['targets']]


def _name(value):
    return isinstance(value, str) and bool(value.strip())


def _names(value):
    return isinstance(value, list) and bool(value) and all(map(_name, value)) and len(set(value)) == len(value)


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def validate(request, mode='inspect'):
    required = REQUEST_FIELDS | ({'authorization'} if mode == 'execute' else set())
    optional = SELECTION_FIELDS | ({'verification_frames'} if mode == 'execute' else set())
    if isinstance(request, dict) and 'targets' in request:
        common = (required - TARGET_FIELDS) | {'targets'}
        require(common <= request.keys() <= common | (optional - SELECTION_FIELDS),
                'INVALID_REQUEST', '批量请求逐网格设置代理、候选名和局部范围')
        targets = request['targets']
        require(isinstance(targets, list) and targets and all(isinstance(row, dict)
                and TARGET_FIELDS <= row.keys() <= TARGET_FIELDS | SELECTION_FIELDS for row in targets),
                'INVALID_REQUEST', 'targets 需要一份非空的具名网格清单')
        for target_request in _target_requests(request):
            validate(target_request, mode)
        mesh_names = {row['target'] for row in targets}
        output_names = {row['candidate_name'] for row in targets}
        proxy_names = {row['proxy'] for row in targets}
        require(len(mesh_names) == len(output_names) == len(targets) and not mesh_names & proxy_names
                and not output_names & (mesh_names | proxy_names | {request['rig']}),
                'INVALID_REQUEST', '批量网格与输出候选须逐件具名，且不能与输入或代理冲突')
        return
    require(isinstance(request, dict) and required <= set(request) <= required | optional,
            'INVALID_REQUEST', '需要文件、精确场景与对象、所选骨、求解设置和预期候选名')
    schema = EXECUTE_SCHEMA if mode == 'execute' else SCHEMA
    require(request['schema'] == schema, 'INVALID_REQUEST', f'需要 schema={schema}')
    for field in ('scene', 'view_layer', 'target', 'rig', 'proxy', 'candidate_name'):
        require(_name(request[field]), 'INVALID_REQUEST', f'需要明确的 {field}')
    require(len({request['target'], request['rig'], request['proxy']}) == 3,
            'INVALID_REQUEST', '网格、骨架与闭合代理必须分别指定')
    require(_names(request['selected_bones']), 'INVALID_REQUEST', '所选骨名必须非空且不重复')
    require(isinstance(request['settings'], dict), 'INVALID_REQUEST', 'settings 必须是原生求解设置对象')
    if 'context_bones' in request:
        require(_names(request['context_bones']), 'INVALID_REQUEST', '解算上下文骨名必须非空且不重复')
    if 'radius_scale' in request:
        require(_number(request['radius_scale'], 0.1, 4.0), 'INVALID_REQUEST', 'radius_scale 须在 0.1 到 4 之间')
    if 'vertex_indices' in request:
        ids = request['vertex_indices']
        require(isinstance(ids, list) and ids and all(type(i) is int and i >= 0 for i in ids)
                and len(set(ids)) == len(ids), 'INVALID_REQUEST', 'vertex_indices 需要不重复的 RAW 顶点编号')
    if 'alpha' in request:
        alpha = request['alpha']
        require('vertex_indices' in request and isinstance(alpha, list) and len(alpha) == len(request['vertex_indices'])
                and all(_number(value, 0.0, 1.0) for value in alpha) and any(alpha),
                'INVALID_REQUEST', 'alpha 需要与 vertex_indices 一一对应，并至少有一个非零值')
    if mode == 'execute':
        authorization = request['authorization']
        require(isinstance(authorization, dict) and set(authorization) == {'receipt', 'reference_mesh', 'regions'}
                and _name(authorization['reference_mesh']) and isinstance(authorization['regions'], dict)
                and bool(authorization['regions']) and all(_name(key) and _name(value)
                    for key, value in authorization['regions'].items()),
                'INVALID_REQUEST', '需要完整双审回执、骨架静态参照和分区表')
        require(_number(request['settings'].get('max_seconds', 60), 0.01, 3600),
                'INVALID_REQUEST', 'max_seconds 须在 0.01 到 3600 秒之间')
        frames = request.get('verification_frames')
        if frames is not None:
            require(isinstance(frames, list) and frames and all(isinstance(row, list) and len(row) == 2
                    and type(row[0]) is int and _number(row[1], 0, 1) and row[1] < 1 for row in frames),
                    'INVALID_REQUEST', 'verification_frames 需要一个或多个 [整数帧, 0 到 1 之间的小数帧]')


def _object(scene, view_layer, name, expected_type):
    obj = scene.objects.get(name)
    require(obj is not None and obj.type == expected_type and view_layer.objects.get(name) == obj,
            'OBJECT_MISMATCH', f'{name} 必须是当前场景和视图层内的 {expected_type}')
    return obj


def _open(bpy, source):
    require(bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False) == {'FINISHED'},
            'OPEN_FAILED', '无法读取已保存的 .blend')


def _source_context(bpy, request):
    scene, layer = bpy.context.scene, bpy.context.view_layer
    require((scene.name, layer.name) == (request['scene'], request['view_layer']),
            'CONTEXT_MISMATCH', f'保存工程的活动场景和视图层为 {scene.name} / {layer.name}')
    layer.update()
    return (scene, layer, _object(scene, layer, request['target'], 'MESH'),
            _object(scene, layer, request['rig'], 'ARMATURE'), _object(scene, layer, request['proxy'], 'MESH'))


def _weight_changes(source, candidate, scope, signatures):
    source_names, old_rows = signatures._weights(source)
    new_names, new_rows = signatures._weights(candidate)
    require(source_names == new_names and len(old_rows) == len(new_rows)
            and [g.lock_weight for g in source.vertex_groups] == [g.lock_weight for g in candidate.vertex_groups],
            'WEIGHT_READBACK_MISMATCH', '冷开后顶点组、锁定状态或顶点数改变')
    editable = set(scope['editable_bone_names'])
    selected = set(scope['vertex_indices'])
    changes = []
    for vertex, (old, new) in enumerate(zip(old_rows, new_rows, strict=True)):
        old, new = dict(old), dict(new)
        for group in sorted(old.keys() | new.keys()):
            if old.get(group) == new.get(group):
                continue
            bone = source_names[group]
            require(vertex in selected and bone in editable, 'WEIGHT_READBACK_MISMATCH',
                    f'顶点 {vertex} 的受保护组 {bone} 改变')
            changes.append({'vertex': vertex, 'bone': bone, 'before': old.get(group), 'after': new.get(group)})
    return changes


def _coincident_boundary_pairs(source, selected):
    selected = set(selected)
    coordinate_indices = {}
    for index in sorted(selected):
        coordinate_indices.setdefault(tuple(source.data.vertices[index].co), []).append(index)
    return sorted((index, vertex.index) for vertex in source.data.vertices if vertex.index not in selected
                  for index in coordinate_indices.get(tuple(vertex.co), ()))


def _seam_readback(bpy, source, candidate, selected, frames):
    pairs = _coincident_boundary_pairs(source, selected)
    sample_indices = sorted({index for pair in pairs for index in pair})
    if not sample_indices:
        return {'method': 'coincident_raw_points_across_selection', 'pair_count': 0, 'samples': []}
    scene = bpy.context.scene
    home = [scene.frame_current, scene.frame_subframe]
    samples = []
    try:
        for frame, subframe in dict.fromkeys(map(tuple, frames or [home])):
            scene.frame_set(frame, subframe=subframe)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            distances = []
            for obj in (source, candidate):
                evaluated = obj.evaluated_get(depsgraph)
                mesh = evaluated.to_mesh()
                try:
                    require(len(mesh.vertices) == len(obj.data.vertices), 'SEAM_READBACK_UNSUPPORTED',
                            '验收帧的求值网格已改变顶点数，无法按 RAW 编号检查接缝')
                    points = {i: evaluated.matrix_world @ mesh.vertices[i].co for i in sample_indices}
                    scale = scene.unit_settings.scale_length * 1000
                    distances.append([(points[a] - points[b]).length * scale for a, b in pairs])
                finally:
                    evaluated.to_mesh_clear()
            samples.append({'frame': [frame, subframe], 'pairs': [
                {'vertices': list(pair), 'source_mm': before, 'candidate_mm': after}
                for pair, before, after in zip(pairs, *distances, strict=True)]})
    finally:
        scene.frame_set(home[0], subframe=home[1])
    return {'method': 'coincident_raw_points_across_selection', 'pair_count': len(pairs), 'samples': samples}


def _action_state(bpy, qc, rig, target):
    data = target.animation_data
    slot = data.action_slot if data else None
    return {'actions': {action.name: qc._action(action) for action in bpy.data.actions},
            'rig_animation': qc._animation(rig), 'target_action': qc._action(data.action if data else None),
            'target_action_slot': slot.identifier if slot else None}


def _authorize(request, objects, reports, mark):
    from rigging_workflow import skeleton_collection, skeleton_gate

    scene, layer, _, rig, _ = objects
    mark('authorizing')
    authentication = request['authorization']
    receipt = checked_descriptor(authentication['receipt'], '.json', 'RECEIPT_SHA_MISMATCH')
    reference = _object(scene, layer, authentication['reference_mesh'], 'MESH')
    target_name = None
    try:
        receipt_doc = read_json(receipt)
        evidence_row = receipt_doc.get('evidence') if isinstance(receipt_doc, dict) else None
        require(isinstance(evidence_row, dict) and set(evidence_row) >= {'path', 'sha256'},
                'AUTHORIZATION_REJECTED', '双审回执缺少证据描述')
        evidence = skeleton_collection.read_verified(evidence_row)
        mark('receipt_loaded')
        binding = evidence.get('runtime_binding', {})
        require(isinstance(binding, dict), 'AUTHORIZATION_REJECTED', '双审回执缺少运行身份绑定')
        modules = binding.get('control_modules', ())
        require(isinstance(modules, (list, tuple)) and all(isinstance(name, str) and name.isidentifier()
                    and (PACKAGE / 'rigging_workflow' / f'{name}.py').is_file() for name in modules),
                'AUTHORIZATION_REJECTED', '双审回执的控制模块必须来自此隔离候选包')
        for name in modules:
            import_module(f'rigging_workflow.{name}')
        required_scopes = []
        for report in reports:
            for action, scope in report['scopes'].items():
                required_scopes.append((scope['operation'], scope['sha256']))
        permit = skeleton_gate.authorize(
            rig, reference, authentication['regions'], receipt, required_scopes=required_scopes,
        )
        mark('permit_registered')
        require(permit.receipt_sha256 == authentication['receipt']['sha256'], 'RECEIPT_SHA_MISMATCH',
                '登记时双审回执身份不符')
        for report in reports:
            target_name = report.get('target')
            for action in report['scopes']:
                mark(f'{action}_scope_verified', target_name)
    except skeleton_gate.GateError as error:
        message = f'{target_name}：{error}' if target_name is not None else str(error)
        raise AdapterError('AUTHORIZATION_REJECTED', message) from error


def _solve(request, run, objects, selection, mark):
    from rigging_workflow import selected_skinning

    _, _, target, rig, proxy = objects
    mark('starting_solve')
    job = selected_skinning.start(target, rig, proxy, request['selected_bones'], request['settings'],
                                  jobs_directory=run / 'jobs', **selection)
    mark('polling_solve')
    try:
        while True:
            state = job.status()
            if state['status'] != 'running':
                break
            time.sleep(0.2)
        require(state['status'] == 'succeeded', 'SOLVE_FAILED',
                f"局部求权未成功：{state.get('error') or state['status']}")
        mark('applying')
        candidate = job.apply(name=request['candidate_name'])
        require(candidate.name == request['candidate_name'], 'READBACK_MISMATCH', '原生候选名称偏离签认')
        mark('applied')
    except BaseException:
        if job.status()['status'] == 'running':
            job.cancel()
        raise
    return str(job.directory)


def _readback(bpy, request, report, source_before, action_before):
    from rigging_workflow import corrective_authoring as signatures, skeleton_qc, skinning_policy

    scene, layer, target, rig, proxy = _source_context(bpy, request)
    candidate = _object(scene, layer, request['candidate_name'], 'MESH')
    require(candidate != target and candidate.data != target.data, 'READBACK_MISMATCH', '冷开后候选没有独立网格')
    require(skinning_policy._state(target, rig) == source_before, 'READBACK_MISMATCH', '冷开后源对象状态改变')
    require(signatures._mesh_signature(candidate.data) == signatures._mesh_signature(target.data)
            and skinning_policy._shape_state(candidate) == skinning_policy._shape_state(target),
            'READBACK_MISMATCH', '冷开后候选拓扑、属性或形态键改变')
    require(_action_state(bpy, skeleton_qc, rig, target) == _action_state(bpy, skeleton_qc, rig, candidate)
            == action_before, 'READBACK_MISMATCH', '冷开后 Action 内容或绑定改变')
    scope = report['scopes']['apply']['scope']
    changes = _weight_changes(target, candidate, scope, signatures)
    seams = _seam_readback(bpy, target, candidate, scope['vertex_indices'], request.get('verification_frames'))
    return {'cold_opened': True, 'shape_keys_preserved': True, 'actions_preserved': True,
            'outside_scope_memberships_preserved': True, 'changed_memberships': changes, 'seams': seams}


def _protect_meshes(bpy):
    from rigging_workflow.workflow_entry import _durable_source

    return {obj.name: _durable_source(obj) for obj in bpy.data.objects if obj.type == 'MESH'}


def execute(bpy, requests, run, targets, aggregate, *, batch):
    from rigging_workflow import skeleton_qc, skinning_policy

    started = time.perf_counter()
    phases = {}

    def mark(phase, target=None):
        elapsed = time.perf_counter() - started
        phases[f'{target}:{phase}' if target is not None else phase] = elapsed
        event = {'phase': phase, 'elapsed_seconds': elapsed}
        if target is not None:
            event['target'] = target
        with (run / 'execution-progress.jsonl').open('a', encoding='utf-8') as progress:
            progress.write(json.dumps(event) + '\n')

    reports = [report for _, report, _ in targets]
    _authorize(requests[0], targets[0][0], reports, mark)
    protection = _protect_meshes(bpy) if batch else None
    source_state = [
        (skinning_policy._state(objects[2], objects[3]), _action_state(bpy, skeleton_qc, objects[3], objects[2]))
        for objects, _, _ in targets
    ]
    for request, (objects, report, selection) in zip(requests, targets, strict=True):
        def step(phase):
            mark(phase, request['target'] if batch else None)

        report['worker_directory'] = _solve(request, run, objects, selection, step)

    staging = run / 'selected-skinning.pending.blend'
    final = run / 'selected-skinning.blend'
    try:
        require(bpy.ops.wm.save_as_mainfile(filepath=str(staging), copy=True, check_existing=False) == {'FINISHED'},
                'SAVE_FAILED', '无法保存独立的选骨审片工程')
        mark('reopening')
        _open(bpy, staging)
        for request, (_, report, _), (source_before, action_before) in zip(requests, targets, source_state, strict=True):
            readback = _readback(bpy, request, report, source_before, action_before)
            report.update(readback=readback, weights_written=bool(readback['changed_memberships']))
        if batch:
            expected = {request['candidate_name'] for request in requests}
            after = _protect_meshes(bpy)
            require(set(after) == protection.keys() | expected and all(after[name] == row for name, row in protection.items()),
                    'READBACK_MISMATCH', '批量冷开后原网格状态改变，或出现未登记的网格对象')
            aggregate['original_meshes_preserved'] = len(protection)
        source = requests[0]['source']
        require(file_digest(Path(source['path'])) == source['sha256'],
                'SOURCE_CHANGED', '选骨重算期间源工程发生变化')
        output = {'path': str(final), 'sha256': file_digest(staging)}
        mark('verified')
        aggregate.update(weights_written=any(report['weights_written'] for report in reports), output=output,
                         authorization=requests[0]['authorization']['receipt'], elapsed_seconds=phases)
        staging.rename(final)
    finally:
        staging.unlink(missing_ok=True)


def inspect(request, *, mode='inspect', run=None):
    import bpy

    require(sys.platform == 'win32' and bpy.app.background and not bpy.app.online_access
            and bpy.app.version == (5, 3, 0) and bpy.app.build_hash == BUILD,
            'UNSUPPORTED_HOST', '需要离线后台 Windows Blender 5.3.0 02f0899d78e3')
    validate(request, mode)
    source = checked_descriptor(request['source'], '.blend', 'SOURCE_SHA_MISMATCH')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(PACKAGE))
    import rigging_workflow
    from rigging_workflow import candidate_state, local_skinning_job, selected_skinning

    modules = (rigging_workflow, candidate_state, local_skinning_job, selected_skinning)
    package = PACKAGE / 'rigging_workflow'
    require(all(Path(module.__file__).resolve().parent == package for module in modules),
            'PACKAGE_MISMATCH', '所加载的角色工作流必须来自此隔离候选目录')
    try:
        _open(bpy, source)
        report = {
            'ok': True, 'mode': mode, 'schema': request['schema'], 'source': request['source'],
            'scene': request['scene'], 'view_layer': request['view_layer'],
            'weights_written': False, 'source_unchanged': True,
            'runtime': {'version': list(bpy.app.version), 'build': bpy.app.build_hash.decode(),
                        'package': str(package), 'package_version': list(rigging_workflow.bl_info['version']),
                        'module_sha256': {module.__name__: file_digest(Path(module.__file__)) for module in modules}},
        }
        batch = 'targets' in request
        requests = _target_requests(request) if batch else [request]
        targets = []
        for row in requests:
            _, _, target, rig, proxy = objects = _source_context(bpy, row)
            require(row['candidate_name'] not in bpy.data.objects, 'CANDIDATE_NAME_IN_USE', '局部采用名称已存在')
            selection = {key: row[key] for key in SELECTION_FIELDS if key in row}
            plan = selected_skinning.check(target, rig, proxy, row['selected_bones'], row['settings'],
                                           candidate_name=row['candidate_name'], **selection)
            native_selection = plan.pop('selection')
            scope_rows = (
                ('start', local_skinning_job.SELECTED_START_OPERATION, plan.pop('selected_write_scope')),
                ('apply', local_skinning_job.SELECTED_APPLY_OPERATION, plan.pop('selected_apply_scope')),
            )
            scopes = {action: {'operation': operation, 'sha256': candidate_state.scope_digest(scope), 'scope': scope}
                      for action, operation, scope in scope_rows}
            details = {'selection': native_selection, 'plan': plan, 'scopes': scopes,
                       'coincident_boundary_pairs': _coincident_boundary_pairs(target, scopes['start']['scope']['vertex_indices'])}
            if batch:
                details.update(target=row['target'], candidate_name=row['candidate_name'], weights_written=False)
            else:
                report.update(details)
                details = report
            targets.append((objects, details, selection))
        if batch:
            report['targets'] = [details for _, details, _ in targets]
        if mode == 'execute':
            execute(bpy, requests, run, targets, report, batch=batch)
        return report
    finally:
        require(file_digest(source) == request['source']['sha256'], 'SOURCE_CHANGED', '检查期间源工程发生变化')


def _failure(error, default_code, mode):
    return {'ok': False, 'mode': mode, 'error': {'code': getattr(error, 'code', default_code),
                                                       'message': str(error)}, 'weights_written': False}


def worker(mode, snapshot, output):
    try:
        report = inspect(read_json(snapshot), mode=mode, run=output.parent)
    except (AdapterError, ImportError, OSError, OverflowError, ValueError, RuntimeError, TypeError) as error:
        report = _failure(error, 'NATIVE_REJECTED', mode)
    write_json(output, report)
    return 0 if report['ok'] else 2


def launch():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['inspect', 'execute'])
    parser.add_argument('--request', required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--blender', required=True)
    args = parser.parse_args()
    try:
        request = read_json(absolute_file(args.request, '.json'))
        validate(request, args.mode)
        blender = absolute_file(args.blender, '.exe')
        run = Path(args.run_dir)
        require(run.is_absolute() and run.parent.is_dir(), 'INVALID_RUN_DIRECTORY', '需要父目录已存在的全新绝对输出目录')
        run.mkdir()
    except (AdapterError, OSError, ValueError) as error:
        print(json.dumps(_failure(error, 'INVALID_REQUEST', args.mode)))
        return 2

    snapshot, worker_result, result, log = (run / name for name in
                                            ('request.json', 'worker-result.json', 'result.json', 'blender.log'))
    try:
        write_json(snapshot, request)
        command = [str(blender), '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
                   '--threads', '2', '--python-exit-code', '1', '--python', str(CLI), '--',
                   '_worker', args.mode, str(snapshot), str(worker_result)]
        timeout = 180
        target_count = len(request['targets']) if 'targets' in request else 1
        if args.mode == 'execute':
            timeout = EXECUTE_REVIEW_AND_READBACK_SECONDS + request['settings'].get('max_seconds', 60) * target_count
        else:
            timeout *= target_count
        with log.open('xb') as stream:
            completed = subprocess.run(command, cwd=run, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, check=False,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        require(worker_result.is_file(), 'WORKER_FAILED', f'Blender 未写回执，退出码 {completed.returncode}，日志 {log}')
        report = read_json(worker_result)
        require(not report['ok'] or completed.returncode == 0, 'WORKER_FAILED', f'Blender 非零退出，日志 {log}')
    except (AdapterError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        report = _failure(error, 'WORKER_FAILED', args.mode)
    write_json(result, report)
    summary = {'ok': report['ok'], 'result': str(result), 'weights_written': report['weights_written']}
    if report['ok']:
        if 'targets' in report:
            summary['targets'] = [{
                'target': target['target'], 'candidate_name': target['candidate_name'],
                'scope_sha256': {action: row['sha256'] for action, row in target['scopes'].items()},
            } for target in report['targets']]
        else:
            summary['scope_sha256'] = {action: row['sha256'] for action, row in report['scopes'].items()}
        if args.mode == 'execute':
            summary['output'] = report['output']
        elif 'targets' not in report:
            summary['coincident_boundary_pairs'] = report['coincident_boundary_pairs']
    else:
        summary['error'] = report['error']
    print(json.dumps(summary))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    marker = sys.argv.index('--') + 1 if '--' in sys.argv else len(sys.argv)
    if sys.argv[marker:marker+1] == ['_worker']:
        _, mode, snapshot, output = sys.argv[marker:]
        sys.exit(worker(mode, Path(snapshot), Path(output)))
    sys.exit(launch())
