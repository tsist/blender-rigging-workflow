# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated Windows Blender 5.3 operations on saved rig candidates."""

import argparse
from contextlib import contextmanager
import hashlib
from importlib import import_module
import json
from pathlib import Path
import re
import struct
import subprocess
import sys


CLI_PATH = Path(__file__).resolve()
PACKAGE = CLI_PATH.parents[1]
EDITIONS = {'v003': PACKAGE}
BUILD = b'02f0899d78e3'
SCHEMA = 'rigging.agent_candidate_cli.v1'
RUN_SCHEMA = 'rigging.agent_candidate_cli.run.v1'
PENDING_BLEND = 'candidate.pending.blend'
ACTIONS = {
    'c94_workflow.author_rework': ('author_rework', 'author_rework.apply'),
    'c94_workflow.rebuild_bound_copy': ('rebuild', 'rebuild.apply'),
    'c94_workflow.native_maintenance_action.REPAIR.WEIGHT': (
        'native_maintenance.weight_repair', 'native_maintenance.apply_repair'),
}
SHA256 = re.compile(r'[0-9a-f]{64}')
CANDIDATE = re.compile(r'[0-9a-f]{32}')


class AdapterError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise AdapterError(code, message)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path, result):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def absolute_file(raw, suffix):
    require(isinstance(raw, str) and Path(raw).is_absolute(), 'INVALID_REQUEST', f'需要绝对 {suffix} 路径')
    path = Path(raw).resolve()
    require(path.is_file() and path.suffix.lower() == suffix, 'INPUT_MISSING', f'输入文件不存在或格式有误：{path}')
    return path


def checked_descriptor(value, suffix, code):
    require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
            'INVALID_REQUEST', '文件描述需要 path 和 sha256')
    path = absolute_file(value['path'], suffix)
    expected = value['sha256']
    require(isinstance(expected, str) and SHA256.fullmatch(expected), 'INVALID_REQUEST', 'SHA-256 必须是 64 位小写十六进制')
    require(file_digest(path) == expected, code, f'输入 SHA-256 不符：{path}')
    return path


def validate(mode, request):
    required = {'schema', 'action_id', 'source', 'scene', 'view_layer', 'candidate_id'}
    reviewed = mode in {'review-handoff', 'apply'}
    handoff = {'review', 'reference', 'regions', 'expected_scope_sha256'} if reviewed else set()
    require(isinstance(request, dict) and set(request) == required | handoff,
            'INVALID_REQUEST', f'{mode} 请求字段必须为 {", ".join(sorted(required | handoff))}')
    require(request['schema'] == SCHEMA, 'INVALID_REQUEST', f'需要 schema={SCHEMA}')
    require(isinstance(request['action_id'], str) and request['action_id'] in ACTIONS,
            'INVALID_REQUEST', '该动作没有此版已保存候选入口')
    require(isinstance(request['candidate_id'], str) and CANDIDATE.fullmatch(request['candidate_id']),
            'INVALID_REQUEST', 'candidate_id 必须是原生 prepare 回执的 32 位小写十六进制 ID')
    for field in ('scene', 'view_layer'):
        require(isinstance(request[field], str) and request[field].strip(), 'INVALID_REQUEST', f'需要明确的 {field}')
    if reviewed:
        require(isinstance(request['reference'], str) and request['reference'].strip(), 'INVALID_REQUEST', '需要明确的审核参照对象')
        require(isinstance(request['regions'], dict) and request['regions'], 'INVALID_REQUEST', '需要原审核使用的骨骼分区')
        expected = request['expected_scope_sha256']
        require(isinstance(expected, str) and SHA256.fullmatch(expected), 'INVALID_REQUEST', '需要 inspect 回执中的作用域 SHA-256')


def native_inspect(scene, action, candidate_id):
    from rigging_workflow import author_rework, candidate_state, native_maintenance, rebuild

    kind, operation = ACTIONS[action]
    rig, record = candidate_state.resolve(scene, candidate_id, kind=kind)
    if kind == 'author_rework':
        _, actual_rig, _, scope, _, _, _ = author_rework.inspect_candidate(scene, candidate_id)
        module = author_rework
    elif kind == 'rebuild':
        actual_rig, _, scope, _, _ = rebuild.inspect_candidate(scene, candidate_id)
        module = rebuild
    else:
        scope = native_maintenance.repair_scope(scene, candidate_id)
        actual_rig = rig
        module = native_maintenance
    require(actual_rig == rig, 'CANDIDATE_CHANGED', '实际检查骨架与保存候选不符')
    identity = {name: record[name] for name in ('id', 'kind', 'rig', 'armature', 'structure_sha256', 'runtime', 'view_layer', 'units', 'frame')}
    scope_sha256 = candidate_state.scope_digest(scope)
    return rig, identity, scope, scope_sha256, operation, module


@contextmanager
def reviewed_scope(request, rig, operation, scope, scope_sha256, package_root):
    import bpy
    from rigging_workflow import candidate_state, skeleton_collection, skeleton_gate

    require(scope_sha256 == request['expected_scope_sha256'], 'SCOPE_MISMATCH', '此候选的最新原生检查与预期写入范围不符')
    receipt = checked_descriptor(request['review'], '.json', 'REVIEW_SHA_MISMATCH')
    _, evidence, facts = skeleton_collection.load_receipt(receipt, expected_sha256=request['review']['sha256'])
    signed = facts['scope']
    require(not signed.get('test_only'), 'REVIEW_SCOPE_DENIED', '测试回执不能用于实际候选')
    require(operation in signed['allowed_operations'] and
            scope_sha256 in signed.get('agent_write_scopes', {}).get(operation, ()),
            'REVIEW_SCOPE_DENIED', '真实回执未授权该原生动作与候选写入范围')
    ref = bpy.context.scene.objects.get(request['reference'])
    require(ref is not None and ref.type == 'MESH', 'REVIEW_REFERENCE_MISSING', '当前候选缺少原审核参照网格')
    modules = evidence.get('runtime_binding', {}).get('control_modules', ())
    require(isinstance(modules, (list, tuple)) and all(isinstance(name, str) and name.isidentifier() for name in modules),
            'REVIEW_CONTROL_MODULE', '需要回执中本包的具名控制模块')
    for name in modules:
        path = package_root / f'{name}.py'
        require(path.is_file() and path.resolve() == path, 'REVIEW_CONTROL_MODULE', f'本版缺少独立控制模块：{name}')
        loaded = import_module(f'rigging_workflow.{name}')
        require(Path(loaded.__file__).resolve() == path, 'REVIEW_CONTROL_MODULE', f'控制模块来自其他发行目录：{name}')
    try:
        permit = skeleton_gate.authorize(rig, ref, request['regions'], str(receipt))
        require(permit.receipt_sha256 == request['review']['sha256'], 'REVIEW_SHA_MISMATCH', '现场登记的回执与输入 SHA 不符')
        checked = candidate_state.require_operation(rig, operation=operation, scope=scope)
        require(checked is permit, 'REVIEW_SCOPE_DENIED', '现场登记许可变更')
        yield {'review': request['review'], 'reference': request['reference'], 'regions': request['regions'],
               'identity_sha256': permit.identity_sha256, 'operation': operation,
               'scope_sha256': scope_sha256, 'candidate_id': request['candidate_id'],
               'source': request['source'], 'scene': request['scene'], 'view_layer': request['view_layer'],
               'package': str(package_root), 'reauthorize_in_apply_process': True}
    finally:
        skeleton_gate.revoke(rig)


def check_cancel(run):
    require(not (run / 'cancel.request').exists(), 'CANCELLED', '已请求取消，未发布候选工程')


def mesh_weight_rows(obj):
    names = [group.name for group in obj.vertex_groups]
    return [{names[row.group]: float(row.weight) for row in vertex.groups} for vertex in obj.data.vertices]


def action_facts(owner):
    animation = owner.animation_data
    if animation is None:
        return None
    return {'action': animation.action.name if animation.action else None,
            'slot': animation.action_slot.identifier if animation.action_slot else None,
            'nla': [{'name': track.name, 'strips': [strip.name for strip in track.strips]} for track in animation.nla_tracks]}


def mesh_facts(obj):
    from rigging_workflow import corrective_authoring as signatures

    keys = obj.data.shape_keys
    return {'name': obj.name, 'data': obj.data.name, 'vertices': len(obj.data.vertices),
            'geometry_sha256': signatures._mesh_signature(obj.data),
            'weights_sha256': signatures._hash(mesh_weight_rows(obj)),
            'groups': [[g.name, g.lock_weight] for g in obj.vertex_groups],
            'armatures': [m.object.name for m in obj.modifiers if m.type == 'ARMATURE' and m.object],
            'shape_keys': {'names': list(keys.key_blocks.keys()), 'action': action_facts(keys)} if keys else None}


def output_facts(scene, rig, objects, operation, candidate_id, source_names, scope):
    import bpy
    from rigging_workflow import candidate_state

    if operation == 'author_rework.apply':
        from rigging_workflow import author_rework
        marker = rig.get(author_rework.STATE_KEY)
    elif operation == 'rebuild.apply':
        marker = rig.get('c94_rebuild_applied_v1')
    else:
        record, anchor = candidate_state._keys(candidate_id)
        marker = 'consumed' if record not in scene and anchor not in scene else None
    require(isinstance(marker, str) and marker, 'CANDIDATE_NOT_APPLIED', '正式采用的原生状态未落盘')
    require(objects and all(obj is not None and obj.type == 'MESH' for obj in objects),
            'OUTPUT_OBJECT_MISMATCH', '采用结果缺少预期网格')
    meshes = [mesh_facts(obj) for obj in objects]
    require(all(rig.name in mesh['armatures'] for mesh in meshes),
            'OUTPUT_OBJECT_MISMATCH', '采用结果未使用这一候选骨架')
    sources = [mesh_facts(scene.objects[name]) for name in source_names]
    facts = {'rig': rig.name, 'armature': rig.data.name, 'rig_structure_sha256': candidate_state._structure(rig),
             'rig_action': action_facts(rig), 'native_state_sha256': hashlib.sha256(marker.encode('utf-8')).hexdigest(),
             'meshes': meshes, 'embedded_sources': sources}
    if operation == 'author_rework.apply':
        hubs = json.loads(marker)['hubs']
        require(all(scene.objects.get(name) is not None for name in hubs), 'OUTPUT_OBJECT_MISMATCH', '作者返工丢失表情控制器')
        facts['author_hubs'] = hubs
        facts['author_boards'] = []
        for board in scope['P1_boards']:
            if 'display' not in board:
                continue
            display = bpy.data.collections.get(board['display'])
            expected = sorted(new_name for _, new_name in board['objects'])
            require(display is not None and sorted(display.objects.keys()) == expected,
                    'OUTPUT_OBJECT_MISMATCH', '表情板的对象列表与已签范围不符')
            facts['author_boards'].append({'collection': display.name, 'objects': expected})
    return facts


def validate_point_changes(scope, before, after):
    targets = {target['object_index']: target['object'] for target in scope['targets']}
    allowed = {}
    for edit in scope['edits']:
        obj = targets[edit['object_index']]
        for group, values in edit['assignments'].items():
            for index, value in zip(edit['indices'], values, strict=True):
                allowed[obj, index, group] = struct.unpack('<f', struct.pack('<f', value))[0] if value else None
    changes = 0
    for name, old in before.items():
        for index, (prior, current) in enumerate(zip(old, after[name], strict=True)):
            for group in prior.keys() | current.keys():
                if prior.get(group) != current.get(group):
                    require((name, index, group) in allowed, 'OUTPUT_SCOPE_MISMATCH', '逐点维修改变了未签的点或骨组')
                    changes += 1
    require(all(after[name][index].get(group) == value for (name, index, group), value in allowed.items()),
            'OUTPUT_SCOPE_MISMATCH', '逐点维修实际落盘值与签认值不符')
    return {'approved_point_group_pairs': len(allowed), 'changed_point_group_pairs': changes}


def native_apply(scene, rig, module, request, operation, scope):
    candidate_id = request['candidate_id']
    if operation == 'native_maintenance.apply_repair':
        names = [row['object'] for row in scope['targets']]
        sources = [obj.name for obj in module._pack(rig)['objects'] if obj.name not in names]
        previous = [mesh_facts(scene.objects[name]) for name in sources]
        before = {name: mesh_weight_rows(scene.objects[name]) for name in names}
        written = module.apply_repair(scene, candidate_id)
        after = {name: mesh_weight_rows(scene.objects[name]) for name in names}
        changes = validate_point_changes(scope, before, after)
        require(set(names) <= {obj.name for obj in written['objects']},
                'OUTPUT_OBJECT_MISMATCH', '原生维修结果缺少签认的网格')
    else:
        names = [row['output'] for row in scope['meshes']]
        sources = [row['source'] if isinstance(row['source'], str) else row['source']['object'] for row in scope['meshes']]
        previous = [mesh_facts(scene.objects[name]) for name in sources]
        written = module.apply_candidate(scene, candidate_id)
        actual = written['objects'] + written.get('author_sources', [])
        require([obj.name for obj in actual] == names,
                'OUTPUT_OBJECT_MISMATCH', '原生采用结果超出签认的输出对象列表')
        changes = {'approved_mesh_count': len(names)}
    require([mesh_facts(scene.objects[name]) for name in sources] == previous,
            'EMBEDDED_SOURCE_CHANGED', '工程内的非目标网格改变，停止发布')
    require(written['rig'] == rig, 'OUTPUT_OBJECT_MISMATCH', '原生采用结果超出签认的骨架')
    return {'observed_scope': changes, 'native_report': written['report'],
            'facts': output_facts(scene, rig, [scene.objects[name] for name in names], operation, candidate_id, sources, scope)}


def configure_host(edition):
    import bpy

    require(sys.platform == 'win32' and bpy.app.background and not bpy.app.online_access
            and bpy.app.version == (5, 3, 0) and bpy.app.build_hash == BUILD,
            'UNSUPPORTED_HOST', '需要离线后台 Windows Blender 5.3.0 02f0899d78e3')
    require(edition in EDITIONS, 'PACKAGE_MISMATCH', '此公开源码包只支持 v003')
    package = EDITIONS[edition].resolve()
    package_root = package / 'rigging_workflow'
    require((package_root / '__init__.py').is_file(), 'PACKAGE_MISMATCH', '该隔离发行缺少角色工作流包')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(package))
    import rigging_workflow

    require(Path(rigging_workflow.__file__).resolve().parent == package_root,
            'PACKAGE_MISMATCH', '加载的角色工作流来自其他发行目录')
    return bpy, rigging_workflow, package_root


def open_scene(bpy, source, request):
    require(bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False) == {'FINISHED'},
            'OPEN_FAILED', '无法读取已保存的准备工程')
    scene, view_layer = bpy.context.scene, bpy.context.view_layer
    require((scene.name, view_layer.name) == (request['scene'], request['view_layer']),
            'CONTEXT_MISMATCH', f'保存工程使用的场景和视图层为 {scene.name} / {view_layer.name}')
    view_layer.update()
    return scene


def run_worker(mode, request, edition, run):
    validate(mode, request)
    if mode == 'apply':
        check_cancel(run)
    source = checked_descriptor(request['source'], '.blend', 'SOURCE_SHA_MISMATCH')
    bpy, rigging_workflow, package_root = configure_host(edition)
    from rigging_workflow import candidate_state

    scene = open_scene(bpy, source, request)
    rig, identity, scope, scope_sha256, operation, module = native_inspect(scene, request['action_id'], request['candidate_id'])
    report = {'ok': True, 'mode': mode, 'action_id': request['action_id'], 'source': request['source'],
              'require_review': {'operation': operation, 'write_scope_sha256': scope_sha256},
              'identity': identity, 'scope': scope, 'weights_written': False,
              'runtime': {'version': list(bpy.app.version), 'build': bpy.app.build_hash.decode(),
                          'edition': edition, 'package': str(package_root),
                          'package_version': list(rigging_workflow.bl_info['version']),
                          'module_sha256': {item.__name__: file_digest(Path(item.__file__))
                                            for item in (rigging_workflow, candidate_state, module)}}}
    if mode in {'review-handoff', 'apply'}:
        with reviewed_scope(request, rig, operation, scope, scope_sha256, package_root) as handoff:
            if mode == 'review-handoff':
                report['handoff'] = handoff
            else:
                check_cancel(run)
                report['adoption'] = native_apply(scene, rig, module, request, operation, scope)
                report['authorization'] = {key: handoff[key] for key in ('review', 'identity_sha256', 'operation', 'scope_sha256')}
                check_cancel(run)
                pending = run / PENDING_BLEND
                require(not pending.exists(), 'OUTPUT_EXISTS', '候选暂存路径已占用')
                require(bpy.ops.wm.save_as_mainfile(filepath=str(pending), copy=True, relative_remap=True) == {'FINISHED'},
                        'SAVE_FAILED', '暂存候选未成功保存')
                report['pending'] = {'path': str(pending), 'sha256': file_digest(pending)}
                report['weights_written'] = True
    require(file_digest(source) == request['source']['sha256'], 'SOURCE_CHANGED', '检查期间源工程发生变化')
    return report


def cold_verify(request, producer, edition, run):
    check_cancel(run)
    require(producer['ok'] and producer['weights_written'], 'OUTPUT_NOT_PREPARED', '没有成功的暂存采用结果')
    pending = checked_descriptor(producer['pending'], '.blend', 'OUTPUT_CHANGED')
    require(pending == (run / PENDING_BLEND).resolve(), 'OUTPUT_ESCAPE', '候选暂存文件不属于本作业')
    bpy, _, _ = configure_host(edition)
    scene = open_scene(bpy, pending, request)
    expected = producer['adoption']['facts']
    rig = scene.objects.get(expected['rig'])
    require(rig is not None and rig.type == 'ARMATURE', 'COLD_RIG_MISSING', '独立重开缺少候选 Armature')
    objects = [scene.objects.get(mesh['name']) for mesh in expected['meshes']]
    sources = [mesh['name'] for mesh in expected['embedded_sources']]
    cold = output_facts(scene, rig, objects, producer['authorization']['operation'], request['candidate_id'], sources, producer['scope'])
    require(cold == expected, 'COLD_READBACK_MISMATCH', '独立重开后的骨架、网格、权重或原生采用状态变更')
    check_cancel(run)
    return {'ok': True, 'build': bpy.app.build_hash.decode(), 'facts': cold, 'sha256': producer['pending']['sha256']}


def worker_main(mode, snapshot, result, edition):
    try:
        request = read_json(snapshot)
        if mode == '_cold':
            report = cold_verify(request, read_json(snapshot.parent / 'worker-result.json'), edition, snapshot.parent)
        else:
            report = run_worker(mode, request, edition, snapshot.parent)
    except AdapterError as error:
        report = {'ok': False, 'mode': mode, 'error': {'code': error.code, 'message': str(error)}, 'weights_written': False}
    except (OSError, ValueError, RuntimeError) as error:
        report = {'ok': False, 'mode': mode, 'error': {'code': 'NATIVE_REJECTED', 'message': str(error)}, 'weights_written': False}
    write_json(result, report)
    return 0 if report['ok'] else 2


@contextmanager
def decision_lock(run):
    import msvcrt

    with (run / 'publication.lock').open('r+b') as stream:
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def cancel_run(run):
    require(run.is_absolute() and run.is_dir(), 'INVALID_RUN_DIRECTORY', '使用明确的现有作业绝对目录')
    require((run / 'run.json').is_file() and read_json(run / 'run.json') == {'schema': RUN_SCHEMA, 'mode': 'apply'},
            'INVALID_RUN_DIRECTORY', '指定目录不是此入口的采用作业')
    with decision_lock(run):
        require(not (run / 'result.json').exists() and not (run / 'candidate.blend').exists(),
                'ALREADY_FINISHED', '采用作业已结束')
        (run / 'cancel.request').touch(exist_ok=True)
    print(json.dumps({'ok': True, 'cancel_requested': True, 'run_directory': str(run)}))


def spawn_worker(blender, mode, snapshot, result, edition, log):
    command = [str(blender), '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
               '--threads', '2', '--python-exit-code', '1', '--python', str(CLI_PATH), '--',
               '_worker', mode, str(snapshot), str(result), edition]
    with log.open('xb') as stream:
        completed = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=180,
                                   check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    require(result.is_file(), 'WORKER_FAILED', f'Blender 未写回执，退出码 {completed.returncode}，查 {log}')
    report = read_json(result)
    require(not report['ok'] or completed.returncode == 0, 'WORKER_FAILED', f'Blender 非零退出，查 {log}')
    return report


def failure(error):
    return {'ok': False, 'error': {'code': getattr(error, 'code', 'WORKER_FAILED'), 'message': str(error)},
            'weights_written': False}


def publish_candidate(run, request, report, cold, result):
    with decision_lock(run):
        check_cancel(run)
        checked_descriptor(request['source'], '.blend', 'SOURCE_CHANGED')
        pending = checked_descriptor(report['pending'], '.blend', 'OUTPUT_CHANGED')
        require(pending == (run / PENDING_BLEND).resolve() and cold['sha256'] == report['pending']['sha256'],
                'OUTPUT_CHANGED', '冷开与当前待发布候选的文件 SHA 不符')
        output = run / 'candidate.blend'
        require(not output.exists(), 'OUTPUT_EXISTS', '输出目录已有候选文件')
        report['candidate'] = {'path': str(output), 'sha256': cold['sha256']}
        report['cold_reopen'] = {'verified': True, 'report': str(run / 'cold-result.json')}
        report['source_unchanged'] = True
        del report['pending']
        staged_report = run / 'result.pending.json'
        write_json(staged_report, report)
        pending.rename(output)
        try:
            staged_report.rename(result)
        except OSError:
            output.rename(pending)
            raise


def launch():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('inspect', 'review-handoff', 'apply', 'cancel'))
    parser.add_argument('--request')
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--blender', help='Absolute path to the audited Blender executable (required except for cancel)')
    parser.add_argument('--edition', choices=tuple(EDITIONS), default='v003')
    args = parser.parse_args()
    try:
        run = Path(args.run_dir)
        if args.mode == 'cancel':
            cancel_run(run)
            return 0
        request = read_json(absolute_file(args.request, '.json'))
        validate(args.mode, request)
        require(args.blender, 'INVALID_REQUEST', '需要 --blender 指定 Blender 可执行文件')
        blender = absolute_file(args.blender, '.exe')
        require(run.is_absolute() and run.parent.is_dir(), 'INVALID_RUN_DIRECTORY', '使用父目录已存在的全新绝对输出目录')
        run.mkdir()
    except (AdapterError, OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': {'code': getattr(error, 'code', 'INVALID_REQUEST'), 'message': str(error)},
                          'weights_written': False}))
        return 2
    snapshot, worker_result, result, log = (run / item for item in
        ('request.json', 'worker-result.json', 'result.json', 'blender.log'))
    write_json(snapshot, request)
    if args.mode == 'apply':
        (run / 'publication.lock').write_bytes(b'1')
        write_json(run / 'run.json', {'schema': RUN_SCHEMA, 'mode': 'apply'})
    try:
        report = spawn_worker(blender, args.mode, snapshot, worker_result, args.edition, log)
        if args.mode == 'apply' and report['ok']:
            check_cancel(run)
            cold = spawn_worker(blender, '_cold', snapshot, run / 'cold-result.json', args.edition, run / 'cold.log')
            require(cold['ok'], cold.get('error', {}).get('code', 'COLD_READBACK_MISMATCH'),
                    cold.get('error', {}).get('message', '独立冷开失败'))
            publish_candidate(run, request, report, cold, result)
    except (AdapterError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        report = failure(error)
    if not result.exists():
        if args.mode == 'apply':
            with decision_lock(run):
                if (run / 'cancel.request').exists():
                    report = failure(AdapterError('CANCELLED', '已请求取消，未发布候选工程'))
                write_json(result, report)
        else:
            write_json(result, report)
    summary = {'ok': report['ok'], 'run_directory': str(run), 'result': str(result), 'weights_written': report['weights_written']}
    summary.update({'require_review': report['require_review']} if report['ok'] else {'error': report['error']})
    if 'candidate' in report:
        summary['candidate'] = report['candidate']
    print(json.dumps(summary))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    marker = sys.argv.index('--') + 1 if '--' in sys.argv else len(sys.argv)
    if sys.argv[marker:marker+1] == ['_worker']:
        _, mode, snapshot, result, edition = sys.argv[marker:]
        sys.exit(worker_main(mode, Path(snapshot), Path(result), edition))
    sys.exit(launch())
