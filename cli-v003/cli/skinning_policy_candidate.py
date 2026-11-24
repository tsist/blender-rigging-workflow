# SPDX-License-Identifier: GPL-3.0-or-later
"""Qualify, preview and apply the native LOCAL skinning policy on saved copies."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT.parent))
from rigging_candidate import (AdapterError, absolute_file, checked_descriptor, configure_host,
                              file_digest, mesh_facts, mesh_weight_rows, open_scene, read_json, require, write_json)

from linux_readonly import run_readonly, worker_request

ENTRY = 'candidate-skinning-policy'
SCHEMA = 'rigging.skinning_policy_preflight.v1'
STEPS = {
    'plan': ('c94_workflow.skinning_policy_plan', 'skinning_policy.prepare_transfer'),
    'preview': ('c94_workflow.skinning_policy_preview', 'skinning_policy.create_scope_preview'),
    'apply': ('c94_workflow.skinning_policy_apply', 'skinning_policy.PreparedWeights'),
}
NATIVE_REQUIRED = {
    'plan': ['skinning_policy.prepare_transfer', 'skinning_policy.PreparedWeights'],
    'preview': ['skinning_policy.PreparedWeights', 'skinning_policy.create_scope_preview'],
    'apply': ['skinning_policy.PreparedWeights'],
}
PENDING_BLEND = 'candidate.pending.blend'


def validate(request, mode):
    required = {'schema', 'source', 'scene', 'view_layer', 'rig', 'target', 'donor'}
    domains = {'candidate_name', 'target_vertex_indices', 'donor_vertex_indices', 'editable_bone_names',
               'alpha', 'max_distance_meters'}
    if mode != 'qualify':
        required |= domains
    optional = {'review', 'reference'} if mode in {'preview', 'apply'} else set()
    require(isinstance(request, dict) and required <= set(request) <= required | optional,
            'INVALID_REQUEST', '使用与操作对应的源、当前对象及明确 RAW 读写域')
    require(request['schema'] == SCHEMA, 'INVALID_REQUEST', f'需要 schema={SCHEMA}')
    for name in ('scene', 'view_layer', 'rig', 'target', 'donor'):
        require(isinstance(request[name], str) and request[name].strip(), 'INVALID_REQUEST', f'{name} 需要明确当前对象名称')
    if 'reference' in request:
        require(isinstance(request['reference'], str) and request['reference'].strip(),
                'INVALID_REQUEST', '提供实际骨架审查的参考网格名称')
    if mode != 'qualify':
        name = request['candidate_name']
        require(isinstance(name, str) and name.strip(), 'INVALID_REQUEST', '提供明确候选对象名称')
        distance = request['max_distance_meters']
        require(type(distance) in (int, float) and math.isfinite(distance) and distance > 0,
                'INVALID_REQUEST', '提供有限正米制投影距离')
        for field in ('donor_vertex_indices', 'target_vertex_indices'):
            require(isinstance(request[field], list) and bool(request[field]),
                    'INVALID_REQUEST', '明确两个网格各自的 RAW 点域')


def scene_objects(scene, request):
    objects = [scene.objects.get(request[key]) for key in ('target', 'donor', 'rig')]
    require(all(obj is not None for obj in objects), 'INPUT_OBJECT_MISSING', '保存工程没有请求中的目标、供体或骨架')
    target, donor, rig = objects
    require(target.type == donor.type == 'MESH' and rig.type == 'ARMATURE', 'INVALID_REQUEST', '需要两个 Mesh 与实际 Armature')
    return target, donor, rig


def qualify(target, donor, rig):
    import numpy as np
    from rigging_workflow import skinning, skinning_policy as policy

    issues = []
    if target == donor or target.data == donor.data:
        issues.append({'part': 'identity', 'message': '供体和目标必须各有独立对象、独立 Mesh 数据'})
    for role, obj in (('target', target), ('donor', donor)):
        try:
            policy._bound(obj, rig, donor=role == 'donor')
        except ValueError as error:
            issues.append({'part': role, 'message': str(error)})
    bindings = [[mod.object for mod in obj.modifiers if mod.type == 'ARMATURE'] for obj in (target, donor)]
    points, triangles = skinning.mesh_arrays(target)
    donor_points, donor_triangles = skinning.mesh_arrays(donor)
    same_geometry = np.array_equal(points, donor_points) and np.array_equal(triangles, donor_triangles)
    raw_differences = None
    if same_geometry:
        old = mesh_weight_rows(target)
        new = mesh_weight_rows(donor)
        raw_differences = sum(a.get(name, 0.) != b.get(name, 0.) for a, b in zip(old, new, strict=True)
                              for name in a.keys() | b.keys())
    return {'native_binding_eligible': not issues, 'same_actual_armature': all(targets == [rig] for targets in bindings),
            'actual_armatures': [[obj.name if obj else None for obj in refs] for refs in bindings],
            'donor_shape_keys': donor.data.shape_keys is not None, 'target_shape_keys': target.data.shape_keys is not None,
            'same_raw_geometry': same_geometry, 'raw_differing_point_group_pairs': raw_differences, 'issues': issues}


def plan_scope(scene, request, target, donor, rig):
    import numpy as np
    from rigging_workflow import candidate_state, skinning, skinning_policy as policy
    from rigging_workflow.workflow_entry import _durable_source

    require(target != donor and target.data != donor.data, 'DONOR_IDENTITY', '供体和目标必须是独立对象及 Mesh')
    require(scene.objects.get(request['candidate_name']) is None, 'OUTPUT_EXISTS', '候选对象名称已占用')
    donor_ids = policy._ids(request['donor_vertex_indices'], len(donor.data.vertices))
    vertices, triangles, donor_values, columns, scale = policy._field(donor, rig, None, donor_ids)
    local = policy.local_scope(target, rig, request['editable_bone_names'], request['target_vertex_indices'], request['alpha'])
    active = local['active']
    points, _ = skinning.mesh_arrays(target)
    values, transfer = skinning.transfer_surface_field(points[local['ids']]*scale, vertices*scale, triangles, donor_values,
                                                       max_distance=request['max_distance_meters'])
    editable = local['editable']
    require(set(editable) <= set(columns), 'DONOR_EDITABLE_COLUMNS_MISSING', '供体缺少实际可写的骨列')
    proposal = np.asarray(values, dtype=float)[:, [columns.index(name) for name in editable]]
    total = proposal.sum(axis=1)
    require((total[active] > 0).all(), 'DONOR_COVERAGE_MISSING', '目标可写点没有所选骨的供体权重')
    # Mirror the native preserve-mass blend for diagnosis only. No PreparedWeights or preview object is created.
    old, blend = local['old'][active], local['alpha'][active, None]
    wanted = proposal[active]*(local['mass'][active]/total[active])[:, None]
    predicted = ((1-blend)*old+blend*wanted).astype('<f4')
    deltas = np.abs(predicted-old)
    field_digest = hashlib.sha256(donor_values.astype('<f8', copy=False).tobytes()).hexdigest()
    scope = {
        'method': 'LOCAL', 'source': request['source'], 'scene': scene.name, 'view_layer': request['view_layer'],
        'frame': [scene.frame_current, scene.frame_subframe], 'unit_scale': scale,
        'rig': {'name': rig.name, 'armature': rig.data.name, 'structure_sha256': candidate_state._structure(rig)},
        'donor': {'object': _durable_source(donor), 'raw_vertex_indices': donor_ids.tolist(),
                  'complete_triangles': len(triangles), 'field_sha256': field_digest, 'bone_columns': columns},
        'target': {'object': _durable_source(target), 'sampled_raw_vertex_indices': local['ids'].tolist(),
                   'writable_raw_vertex_indices': local['ids'][active].tolist(), 'writable_bones': editable,
                   'alpha': local['alpha'].tolist(), 'locked_bones_preserved': local['summary']['locked_bones_preserved']},
        'max_distance_meters': request['max_distance_meters'], 'candidate_name': request['candidate_name'],
        'proposed_selected_field_sha256': hashlib.sha256(predicted.tobytes()).hexdigest(),
    }
    continuation = {}
    for phase, (action, native_operation) in STEPS.items():
        exact = {**scope, 'action_id': action, 'stage': phase}
        continuation[phase] = {'action_id': action, 'operation': native_operation,
                               'native_allowed_operations': NATIVE_REQUIRED[phase],
                               'scope_sha256': candidate_state.scope_digest(exact), 'scope': exact}
    return {'scope': scope, 'scope_sha256': candidate_state.scope_digest(scope), 'native_read_domain': transfer,
            'target_write_domain': local['summary'], 'max_weight_delta': float(deltas.max(initial=0)),
            'changed_point_group_pairs': int(np.count_nonzero(deltas > 1e-6)),
            'prepared_weights_created': False, 'continuation': continuation}


def approved_review(mode, request, phases):
    from rigging_workflow import skeleton_collection

    require('review' in request, 'REVIEW_REQUIRED', '需要此候选与操作范围的主审和独立审查回执')
    receipt = checked_descriptor(request['review'], '.json', 'REVIEW_SHA_MISMATCH')
    _, evidence, facts = skeleton_collection.load_receipt(receipt, expected_sha256=request['review']['sha256'])
    scope = facts['scope']
    required = [(operation, phases['plan']['scope_sha256']) for operation in NATIVE_REQUIRED['plan']]
    phase = phases[mode]
    required.append((phase['operation'], phase['scope_sha256']))
    approved = scope.get('agent_write_scopes', {})
    require(not scope.get('test_only') and all(operation in scope['allowed_operations']
            and digest in approved.get(operation, ()) for operation, digest in required),
            'REVIEW_SCOPE_DENIED', '签认必须包含原生准备和此次操作各自的准确范围')
    return receipt, evidence, facts['identity_sha256']


def artifact_facts(scene, request, result, obj):
    import numpy as np
    from rigging_workflow.workflow_entry import _durable_source

    scope = result['scope']
    target, donor, rig = scene_objects(scene, request)
    for source, role in ((target, 'target'), (donor, 'donor')):
        require(_durable_source(source) == scope[role]['object'], 'SOURCE_CHANGED',
                '保存操作中原目标或供体的网格、权重或依赖发生变化')
    require(obj != target and obj.data != target.data, 'OUTPUT_CHANGED', '候选须有独立对象和网格')
    facts = {'candidate': mesh_facts(obj), 'durable_identity': _durable_source(obj)}
    old, new = mesh_weight_rows(target), mesh_weight_rows(obj)
    ids = scope['target']['writable_raw_vertex_indices']
    bones = scope['target']['writable_bones']
    require(len(old) == len(new) and facts['candidate']['armatures'] == [rig.name], 'OUTPUT_CHANGED',
            '候选点数或实际骨架与原目标不符')
    if result['mode'] == 'preview':
        preview_group = obj.vertex_groups.active
        require(obj.get('c94_policy_preview_v1') and preview_group is not None and
                preview_group.name.startswith('C94_计划写入范围'), 'OUTPUT_CHANGED', '缺少原生预览组')
        weights = {i: row[preview_group.name] for i, row in enumerate(new) if preview_group.name in row}
        affected = set(ids)
        expected = {i: a for i, a in zip(scope['target']['sampled_raw_vertex_indices'], scope['target']['alpha'],
                                        strict=True) if i in affected}
        require(weights == {i: float(np.float32(a)) for i, a in expected.items()} and
                old == [{k: v for k, v in row.items() if k != preview_group.name} for row in new],
                'OUTPUT_CHANGED', '预览修改了原权重或实际着色域不同')
        color = obj.data.color_attributes.active_color
        require(color and color.domain == 'POINT' and color.name.startswith('C94_计划范围颜色'),
                'OUTPUT_CHANGED', '缺少可见作用域颜色')
        color_values = np.empty((len(new), 4), dtype='<f4')
        color.data.foreach_get('color', color_values.ravel())
        alpha = np.zeros(len(old), dtype=float)
        alpha[ids] = [expected[i] for i in ids]
        expected_color = np.column_stack((alpha, 1 - abs(2 * alpha - 1), 1 - alpha, np.ones(len(alpha))))
        require(np.array_equal(color_values, expected_color.astype('<f4')), 'OUTPUT_CHANGED', '预览颜色与签认的点域不符')
        facts['preview'] = {'group': preview_group.name, 'color_attribute': color.name,
                            'raw_alpha': [[i, value] for i, value in weights.items()],
                            'color_sha256': hashlib.sha256(color_values.tobytes()).hexdigest()}
    else:
        selected = {(i, name) for i in ids for name in bones}
        differences = [(i, name, before.get(name, 0.), after.get(name, 0.))
                       for i, (before, after) in enumerate(zip(old, new, strict=True))
                       for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
        require(all((i, name) in selected for i, name, _, _ in differences), 'OUTPUT_CHANGED', '候选改动超出原签认骨组和点')
        value = np.array([[new[i].get(name, 0.) for name in bones] for i in ids], dtype='<f4')
        require(hashlib.sha256(value.tobytes()).hexdigest() == scope['proposed_selected_field_sha256'],
                'OUTPUT_CHANGED', '应用的权重和已审预值有差异')
        facts['apply'] = {'changed_group_values': [list(row) for row in differences],
                          'candidate_binding': json.loads(obj['c94_skinning_policy_v1'])}
    return facts


def execute(mode, request, scene, target, donor, rig, result, run):
    import bpy
    import numpy as np
    from rigging_workflow import skeleton_gate, skinning_policy as policy

    phases = result['continuation']
    receipt, evidence, identity = approved_review(mode, request, phases)
    ref = scene.objects.get(request.get('reference', ''))
    require(ref is not None and ref.type == 'MESH', 'REVIEW_REFERENCE_MISSING', '当前文件缺少审核时的静态参考网格')
    try:
        permit = skeleton_gate.authorize(rig, ref, evidence['regions'], str(receipt))
        require(permit.receipt_sha256 == request['review']['sha256'] and permit.identity_sha256 == identity,
                'REVIEW_SHA_MISMATCH', '当前实际登记的回执或骨架身份不符')
        plan = policy.prepare_transfer(target, rig, donor, max_distance_meters=request['max_distance_meters'],
                  donor_vertex_indices=request['donor_vertex_indices'], local_vertex_indices=request['target_vertex_indices'],
                  alpha=request['alpha'], editable_bone_names=request['editable_bone_names'],
                  write_scope_sha256=phases['plan']['scope_sha256'])
        expected = result['scope']['target']
        require(plan.names == tuple(expected['writable_bones']) and plan._ids.tolist() == expected['writable_raw_vertex_indices']
                and hashlib.sha256(np.ascontiguousarray(plan._weights, dtype='<f4').tobytes()).hexdigest()
                == result['scope']['proposed_selected_field_sha256'], 'NATIVE_PLAN_MISMATCH',
                '同会话原生计划和已审核的实际提案不同')
        output = getattr(plan, mode)(name=request['candidate_name'], write_scope_sha256=phases[mode]['scope_sha256'])
        if mode == 'preview':
            plan.cancel()
        result['output'] = artifact_facts(scene, request, result, output)
        pending = run / PENDING_BLEND
        require(not pending.exists(), 'OUTPUT_EXISTS', '候选暂存路径已占用')
        require(bpy.ops.wm.save_as_mainfile(filepath=str(pending), copy=True, relative_remap=True) == {'FINISHED'},
                'SAVE_FAILED', '暂存候选未成功保存')
        result.update(pending={'path': str(pending), 'sha256': file_digest(pending)},
                      authorization={'review': request['review'], 'identity_sha256': identity,
                                     'plan_scope_sha256': phases['plan']['scope_sha256'],
                                     'operation_scope_sha256': phases[mode]['scope_sha256']},
                      prepared_weights_created=True, weights_written=mode == 'apply', preview_created=mode == 'preview')
    finally:
        skeleton_gate.revoke(rig)


def worker(mode, request, run):
    validate(request, mode)
    source = checked_descriptor(request['source'], '.blend', 'SOURCE_SHA_MISMATCH')
    bpy, _, _ = configure_host('v003', linux_entry=ENTRY, mode=mode)
    scene = open_scene(bpy, source, request, self_contained=sys.platform == 'linux')
    target, donor, rig = scene_objects(scene, request)
    result = {'ok': True, 'mode': mode, 'source': request['source'], 'build': bpy.app.build_hash.decode(),
              'qualify': qualify(target, donor, rig), 'weights_written': False, 'preview_created': False}
    if mode != 'qualify':
        require(result['qualify']['native_binding_eligible'], 'DONOR_NOT_ELIGIBLE',
                '; '.join(issue['message'] for issue in result['qualify']['issues']))
        result.update(plan_scope(scene, request, target, donor, rig))
        if mode in {'preview', 'apply'}:
            try:
                execute(mode, request, scene, target, donor, rig, result, run)
            except AdapterError as error:
                result.update(error_report(error))
    require(file_digest(source) == request['source']['sha256'], 'SOURCE_CHANGED', '检查期间源文件发生变化')
    result['source_unchanged'] = True
    return result


def error_report(error):
    return {'ok': False, 'weights_written': False, 'preview_created': False,
            'error': {'code': getattr(error, 'code', 'NATIVE_REJECTED'), 'message': str(error)}}


def cold(request, run):
    producer = read_json(run / 'worker-result.json')
    pending = checked_descriptor(producer['pending'], '.blend', 'OUTPUT_CHANGED')
    require(pending == (run / PENDING_BLEND).resolve(), 'OUTPUT_ESCAPE', '候选暂存文件超出本作业')
    bpy, _, _ = configure_host('v003')
    scene = open_scene(bpy, pending, request)
    output = scene.objects.get(request['candidate_name'])
    require(output is not None and artifact_facts(scene, request, producer, output) == producer['output'],
            'COLD_READBACK_MISMATCH', '独立重开的网格或原生预览状态与保存前不符')
    require(file_digest(pending) == producer['pending']['sha256'], 'OUTPUT_CHANGED', '冷开期间暂存文件改变')
    return {'ok': True, 'build': bpy.app.build_hash.decode(), 'sha256': producer['pending']['sha256']}


def run_worker(blender, mode, snapshot, output, log):
    command = [str(blender), '--background', '--factory-startup', '--offline-mode', '--disable-autoexec',
               '--threads', '2', '--python-exit-code', '1', '--python', str(SCRIPT), '--',
               '_worker', mode, str(snapshot), str(output)]
    with log.open('xb') as stream:
        process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=180, check=False,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    require(output.is_file(), 'WORKER_FAILED', f'Blender 退出码 {process.returncode}，查询 {log.name}')
    result = read_json(output)
    require(not result['ok'] or process.returncode == 0, 'WORKER_FAILED', 'Blender 非零退出')
    return result


def main():
    if '--' in sys.argv:
        marker = sys.argv.index('--')+1
        tag, mode, snapshot, output = sys.argv[marker:]
        require(tag == '_worker', 'INVALID_REQUEST', '无效工作进程入口')
        try:
            with worker_request(ENTRY, mode, Path(snapshot)) as request:
                run = Path(snapshot).parent
                result = cold(request, run) if mode == '_cold' else worker(mode, request, run)
        except (OSError, ValueError, RuntimeError) as error:
            result = error_report(error)
        write_json(Path(output), result)
        return 0 if result['ok'] else 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('qualify', 'plan', 'preview', 'apply'))
    parser.add_argument('--request', required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--blender', required=True, help='Absolute path to the audited Blender executable')
    args = parser.parse_args()
    if sys.platform == 'linux':
        output = None
        try:
            result, output = run_readonly(ENTRY, args.mode, args.request, args.run_dir,
                                          args.blender, SCRIPT, lambda req: validate(req, args.mode))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result = error_report(error)
        print(json.dumps({'ok': result['ok'], 'result': str(output) if output else None,
                          'weights_written': False, 'preview_created': False, 'error': result.get('error')}))
        return 0 if result['ok'] else 2
    try:
        request = read_json(absolute_file(args.request, '.json'))
        validate(request, args.mode)
        blender = absolute_file(args.blender, '.exe')
        run = Path(args.run_dir)
        require(run.is_absolute() and run.parent.is_dir(), 'INVALID_RUN_DIRECTORY', '提供父目录已存在的新绝对目录')
        run.mkdir()
        frozen, output = run / 'request.json', run / 'result.json'
        write_json(frozen, request)
        creates = args.mode in {'preview', 'apply'}
        result = run_worker(blender, args.mode, frozen, run / 'worker-result.json' if creates else output, run / 'blender.log')
        if creates:
            if result['ok']:
                cold_path = run / 'cold-result.json'
                verification = run_worker(blender, '_cold', frozen, cold_path, run / 'cold.log')
                require(verification['ok'], 'COLD_READBACK_MISMATCH', verification.get('error', {}).get('message', '冷开失败'))
                checked_descriptor(request['source'], '.blend', 'SOURCE_CHANGED')
                pending = checked_descriptor(result['pending'], '.blend', 'OUTPUT_CHANGED')
                require(pending == (run / PENDING_BLEND).resolve() and result['pending']['sha256'] == verification['sha256'],
                        'OUTPUT_CHANGED', '暂存文件与冷开回执摘要不符')
                candidate = run / 'candidate.blend'
                pending.rename(candidate)
                result['candidate'] = {'path': str(candidate), 'sha256': result.pop('pending')['sha256']}
                result['cold_reopen'] = {'verified': True, 'report': str(cold_path)}
            write_json(output, result)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result = error_report(error)
        if 'output' in locals() and output.parent.is_dir() and not output.exists():
            write_json(output, result)
        else:
            output = None
    print(json.dumps({'ok': result['ok'], 'result': str(output) if output else None,
                      'weights_written': result['weights_written'], 'preview_created': result['preview_created'],
                      'candidate': result.get('candidate'), 'error': result.get('error')}))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
