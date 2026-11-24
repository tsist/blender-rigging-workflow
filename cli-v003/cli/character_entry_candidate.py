# SPDX-License-Identifier: GPL-3.0-or-later
"""Rehearse, finish and verify reviewed character bindings on Windows Blender 5.3."""

import argparse
from importlib import import_module
import json
import time
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPT.parent))
from rigging_candidate import (CANDIDATE, absolute_file, checked_descriptor,
                              check_cancel, configure_host, file_digest, open_scene, read_json, require, write_json)

SCHEMA = 'rigging.character_entry_preflight.v1'
AUDITION_TIMEOUT = 3600
CAPTURE = 'workflow_entry.capture_reviewed'
SINGLE = 'workflow_entry.start_volume'
MULTI = 'workflow_entry.start_plan'
APPLY = 'workflow_entry.apply_volume'
RIGID = 'workflow_entry.bind_rigid'
TRANSFER = 'workflow_entry.bind_transfer'
FINISH = 'workflow_entry.finish'


def validate(mode, request):
    required = {'schema', 'source', 'scene', 'view_layer', 'candidate_id'}
    execution = {'capture_review', 'operation_review', 'plan', 'motion'}
    extra = {
        'inspect': set(),
        'scope': {'capture_review', 'operation_review', 'plan', 'geometry', 'single', 'transfer'},
        'result': {'plan', 'audition', 'finish', 'skin_motion', 'solve_status'},
        'audition': execution | {'geometry', 'worker_jobs_dir'},
        'finish': execution | {'geometry', 'worker_jobs_dir', 'finish_review', 'finish_scope', 'manual_handoff',
                               'isolate_bound'},
    }
    if mode == 'result':
        required |= extra[mode]
    if mode in {'audition', 'finish'}:
        required |= execution
    if mode == 'finish':
        required |= {'finish_review', 'finish_scope'}
    require(isinstance(request, dict) and required <= request.keys() <= required | extra[mode],
            'INVALID_REQUEST', '请提交此阶段对应的候选、输入及回执文件')
    require(request['schema'] == SCHEMA, 'INVALID_REQUEST', f'需要 schema={SCHEMA}')
    require(isinstance(request['candidate_id'], str) and CANDIDATE.fullmatch(request['candidate_id']),
            'INVALID_REQUEST', '需要实际保存的候选 ID')
    if mode == 'finish':
        require(all(isinstance(request[key], dict) and set(request[key]) == {'path', 'sha256'}
                    for key in ('finish_review', 'finish_scope')), 'INVALID_REQUEST',
                '正式完成需要审核及范围文件的绝对路径和 SHA-256')
        require(type(request.get('isolate_bound', False)) is bool, 'INVALID_REQUEST',
                'isolate_bound 需要布尔值')
    if mode in {'audition', 'finish'} and 'worker_jobs_dir' in request:
        jobs = request['worker_jobs_dir']
        require(isinstance(jobs, str) and Path(jobs).is_absolute() and Path(jobs).is_dir(),
                'INVALID_JOB_DIRECTORY', '复用已完成域需要现存的专用作业根绝对路径')
    if mode == 'scope':
        require('capture_review' in request, 'REVIEW_REQUIRED', '原生范围检查需要当前候选的真实 RAW 双审回执')
        require('plan' in request or 'single' in request, 'INVALID_REQUEST', '提供原生计划文件或单域明确参数')
        require('geometry' not in request or 'plan' in request, 'INVALID_REQUEST', '采样与代理工程须配套原生计划')


def frozen(value, suffix='.json'):
    return checked_descriptor(value, suffix, 'ARTIFACT_SHA_MISMATCH')


def retained_plan(request, capture_sha):
    from rigging_workflow import candidate_state, workflow_entry

    plan = read_json(frozen(request['plan']))
    require(plan.get('candidate') == request['candidate_id'] and plan.get('source_scope_sha256') == capture_sha,
            'PLAN_CANDIDATE_MISMATCH', '计划并非保存候选的 RAW 捕获范围')
    scopes = plan['scopes']
    digests = plan['scope_sha256']
    require(scopes and scopes.keys() == digests.keys()
            and set(scopes) <= {SINGLE, MULTI, APPLY, RIGID, TRANSFER}
            and not {SINGLE, MULTI} <= scopes.keys()
            and all(row.get('candidate_id') == request['candidate_id']
                    and candidate_state.scope_digest(row) == digests[name] for name, row in scopes.items()),
            'PLAN_SCOPE_MISMATCH', '计划的原生动作或范围 SHA-256 不一致')
    if APPLY in scopes:
        solve = [row for name, row in scopes.items() if name in {SINGLE, MULTI}]
        require(len(solve) == 1 and workflow_entry.volume_apply_scope(solve[0], name=scopes[APPLY]['name']) == scopes[APPLY],
                'PLAN_APPLY_MISMATCH', '采用范围与此次唯一求解范围不一致')
    if TRANSFER in scopes:
        require(APPLY in scopes and scopes[TRANSFER].get('donor_part_index') == scopes[APPLY].get('part_index')
                and scopes[TRANSFER].get('donor_name') == scopes[APPLY].get('name'),
                'PLAN_TRANSFER_MISMATCH', '同作业转权必须绑定此次已审供体采用范围')
    return plan


def authorize_capture(scene, request, rig, capture_scope):
    from rigging_workflow import candidate_state, skeleton_collection, skeleton_gate

    review = request['capture_review']
    require(isinstance(review, dict) and set(review) == {'receipt', 'identity', 'reference'},
            'INVALID_REQUEST', 'RAW 审核须提供双审回执、身份文件与参照网格名称')
    receipt = frozen(review['receipt'])
    proof = read_json(frozen(review['identity']))
    _, evidence, facts = skeleton_collection.load_receipt(receipt, expected_sha256=review['receipt']['sha256'])
    require(not facts['scope'].get('test_only') and proof.get('review_blend') == request['source']
            and proof.get('production_candidate') == request['candidate_id']
            and proof.get('identity_sha256') == facts['identity_sha256'],
            'REVIEW_SCOPE_DENIED', '双审身份与保存候选、原工程不一致')
    reference = scene.objects.get(review['reference'])
    require(reference is not None and reference.type == 'MESH', 'REVIEW_REFERENCE_MISSING', '当前工程缺少原审核参照网格')
    for name in evidence.get('runtime_binding', {}).get('control_modules', ()):
        import_module(f'rigging_workflow.{name}')
    permit = skeleton_gate.authorize(rig, reference, proof['regions'], receipt)
    require(permit.receipt_sha256 == review['receipt']['sha256'], 'REVIEW_SHA_MISMATCH', '现场登记的回执与固定文件不同')
    candidate_state.require_operation(rig, operation=CAPTURE, scope=capture_scope)
    return permit


def reviewed_operations(review, identity, digests, *, worker=False):
    from rigging_workflow import skeleton_collection

    file = frozen(review)
    _, _, receipt = skeleton_collection.load_receipt(file, expected_sha256=review['sha256'])
    signed = receipt['scope']
    required = {'worker.solve'} if worker else set()
    require(not signed.get('test_only') and receipt['identity_sha256'] == identity
            and (required | digests.keys()) <= set(signed['allowed_operations'])
            and all(digest in signed.get('agent_write_scopes', {}).get(name, ()) for name, digest in digests.items()),
            'REVIEW_SCOPE_DENIED', '真实回执未授权此骨架的求解、采用或部件范围')
    return receipt


def scene_mesh(scene, name):
    obj = scene.objects.get(name)
    require(obj is not None and obj.type == 'MESH', 'INPUT_OBJECT_MISSING', f'未找到明确网格：{name}')
    return obj


def replay_multidomain(bpy, scene, entry, row, geometry):
    """Prepare a native scratch surface and copy named, audited point masks by exact point order."""
    import numpy as np
    from rigging_workflow import corrective_authoring, skinning_plan

    geometry_file = frozen(geometry, '.blend')
    surface_identity = row['surface']
    domains = row['domains']
    missing = sorted({domain['proxy']['object'] for domain in domains} - set(scene.objects.keys()))
    surface = entry.prepare_surface(row['part_index'], name=surface_identity['object'])
    with bpy.data.libraries.load(str(geometry_file), link=False) as (source, loaded):
        baked_name = surface_identity['mesh']
        require(baked_name in source.meshes and all(name in source.objects for name in missing),
                'INPUT_OBJECT_MISSING', '审片工程中缺少计划的采样网格或代理')
        loaded.meshes = [baked_name]
        loaded.objects = missing
    saved = loaded.meshes[0]
    require(saved is not None and corrective_authoring._mesh_signature(saved) == surface_identity['geometry_sha256'],
            'SURFACE_CHANGED', '审片采样网格已改变')
    require(len(saved.vertices) == len(surface.data.vertices), 'SURFACE_CHANGED', '审片和当前采样顶点数不同')
    saved_co = np.empty(3 * len(saved.vertices), dtype=np.float32)
    live_co = np.empty_like(saved_co)
    saved.vertices.foreach_get('co', saved_co)
    surface.data.vertices.foreach_get('co', live_co)
    require(np.array_equal(saved_co, live_co), 'SURFACE_CHANGED', '当前采样的 RAW 点顺序或位置变化，须重做遮罩')
    mask_names = {domain['mask_attribute'] for domain in domains}
    require(mask_names <= set(saved.attributes.keys()), 'MASK_INVALID', '审片工程缺少审核计划中声明的遮罩')
    for stored in (attr for attr in saved.attributes if attr.name in mask_names):
        name = stored.name
        require(stored.domain == 'POINT' and stored.data_type == 'FLOAT',
                'MASK_INVALID', f'需要审片中的 FLOAT 点域遮罩：{name}')
        current = surface.data.attributes.get(name) or surface.data.attributes.new(name, 'FLOAT', 'POINT')
        values = np.empty(len(saved.vertices), dtype=np.float32)
        stored.data.foreach_get('value', values)
        current.data.foreach_set('value', values)
    surface.data.update()
    for proxy in loaded.objects:
        require(proxy is not None and proxy.type == 'MESH', 'INPUT_OBJECT_MISSING', '代理未读入')
        scene.collection.objects.link(proxy)
    bpy.context.view_layer.update()
    live_domains = [{**domain, 'proxy': scene_mesh(scene, domain['proxy']['object'])} for domain in domains]
    scope = entry.plan_scope(row['part_index'], surface, live_domains, row['rigid'],
                             transfer_distance_meters=row['transfer_distance_meters'])
    differences = [key for key in row if scope.get(key) != row[key]]
    if differences:
        matches = [str([key, scope[key], row[key]]) for key in differences if key in {'surface', 'staging', 'original'}]
        matches.extend(str([actual['name'], actual['proxy'], wanted['proxy']])
                       for actual, wanted in zip(scope['domains'], row['domains'], strict=True) if actual != wanted)
        require(False, 'PLAN_SCOPE_MISMATCH', f'多域范围变化 {differences}；' + '；'.join(matches))
    technical = skinning_plan.check(entry._staging[row['part_index']], entry.rig, surface, live_domains, row['rigid'],
                                    transfer_distance_meters=row['transfer_distance_meters'])
    require(file_digest(geometry_file) == geometry['sha256'], 'ARTIFACT_SHA_MISMATCH', '检查期间审片采样工程发生变化')
    return scope, technical, surface, live_domains


def native_scopes(bpy, scene, request, rig, capture_scope, capture_sha, *, run=None):
    from rigging_workflow import candidate_state, skeleton_gate, skinning_job, workflow_entry

    plan = retained_plan(request, capture_sha) if 'plan' in request else None
    entry = None
    try:
        permit = authorize_capture(scene, request, rig, capture_scope)
        if plan:
            require(plan['identity'] == permit.identity_sha256, 'PLAN_IDENTITY_MISMATCH',
                    '计划与当前真正通过 RAW 审核的骨架身份不同')
        entry = workflow_entry.capture_reviewed(scene, request['candidate_id'])
        scopes = {}
        technical = {}
        surface, live_domains = None, ()
        if 'single' in request:
            row = request['single']
            require(isinstance(row, dict) and set(row) == {'part_index', 'proxy', 'bone_names', 'settings', 'name'},
                    'INVALID_REQUEST', '单域须给部件索引、代理、骨表、设置与拟采用名称')
            proxy = scene_mesh(scene, row['proxy'])
            scope = entry.volume_scope(row['part_index'], proxy, row['bone_names'], row['settings'])
            skinning_job._source_check(entry._staging[row['part_index']], rig, proxy, row['bone_names'])
            scopes[SINGLE] = scope
            scopes[APPLY] = workflow_entry.volume_apply_scope(scope, name=row['name'])
        if plan:
            planned = plan['scopes']
            require(not ('single' in request and APPLY in planned), 'INVALID_REQUEST', '单次检查只接受一个求解计划')
            if SINGLE in planned:
                row = planned[SINGLE]
                proxy = scene_mesh(scene, row['proxy']['object'])
                scopes[SINGLE] = entry.volume_scope(row['part_index'], proxy, row['bone_names'], row['settings'])
                skinning_job._source_check(entry._staging[row['part_index']], rig, proxy, row['bone_names'])
            if MULTI in planned:
                require('geometry' in request, 'GEOMETRY_ARTIFACT_REQUIRED', '多域预检需要保存了采样遮罩与代理的审片工程')
                scopes[MULTI], technical, surface, live_domains = replay_multidomain(
                    bpy, scene, entry, planned[MULTI], request['geometry'])
            if APPLY in planned:
                solve = scopes[SINGLE] if SINGLE in planned else scopes[MULTI]
                scopes[APPLY] = workflow_entry.volume_apply_scope(solve, name=planned[APPLY]['name'])
            if RIGID in planned:
                row = planned[RIGID]
                scopes[RIGID] = entry.rigid_scope(row['part_index'], row['assignments'], name=row['name'])
            if TRANSFER in planned:
                row = planned[TRANSFER]
                require(APPLY in scopes and scopes[APPLY]['part_index'] == row['donor_part_index'],
                        'PLAN_TRANSFER_MISMATCH', '计划中的转权供体未在同作业采用')
                transfer_info = entry.transfer_preflight(
                    row['part_index'], row['donor_part_index'],
                    max_distance_meters=row['max_distance_meters'],
                    donor_vertex_indices=row.get('donor_vertex_indices'),
                )
                scopes[TRANSFER] = workflow_entry.transfer_plan_scope(
                    scopes[APPLY], entry._part_scope(row['part_index']),
                    max_distance_meters=row['max_distance_meters'], name=row['name'],
                    bone_names=row.get('bone_names'),
                    donor_vertex_indices=row.get('donor_vertex_indices'),
                )
            else:
                transfer_info = {'ready': False, 'code': 'LIVE_DONOR_REQUIRED'}
            require(all(scopes[name] == row for name, row in planned.items()), 'PLAN_SCOPE_MISMATCH',
                    '重新计算的范围与计划不一致')
        else:
            transfer_info = {'ready': False, 'code': 'LIVE_DONOR_REQUIRED'}
        if 'transfer' in request:
            row = request['transfer']
            req_fields = {'part_index', 'donor_part_index', 'max_distance_meters', 'name'}
            opt_fields = {'bone_names', 'donor_vertex_indices'}
            require(isinstance(row, dict) and req_fields <= row.keys() <= req_fields | opt_fields,
                    'INVALID_REQUEST', '同作业转权需要 part_index、donor_part_index、max_distance_meters 与 name')
            require(APPLY in scopes and scopes[APPLY]['part_index'] == row['donor_part_index'],
                    'TRANSFER_DONOR_MISSING', '同作业转权要求供体部件在同一请求中求解并采用')
            claimed = {scopes[k]['part_index'] for k in (SINGLE, MULTI, RIGID) if k in scopes}
            require(row['part_index'] not in claimed,
                    'TRANSFER_PART_CONFLICT', '同作业转权目标部件已被其它绑定动作占用')
            transfer_info = entry.transfer_preflight(
                row['part_index'], row['donor_part_index'],
                max_distance_meters=row['max_distance_meters'],
                donor_vertex_indices=row.get('donor_vertex_indices'),
            )
            scopes[TRANSFER] = workflow_entry.transfer_plan_scope(
                scopes[APPLY], entry._part_scope(row['part_index']),
                max_distance_meters=row['max_distance_meters'], name=row['name'],
                bone_names=row.get('bone_names'),
                donor_vertex_indices=row.get('donor_vertex_indices'),
            )
        digests = {name: candidate_state.scope_digest(row) for name, row in scopes.items()}
        signed = []
        if 'operation_review' in request:
            reviewed_operations(request['operation_review'], permit.identity_sha256, digests, worker=run is not None)
            signed = list(digests)
        if run is not None:
            require(APPLY in scopes and len(scopes.keys() & {SINGLE, MULTI}) == 1
                    and technical == plan['preflight'], 'PLAN_SCOPE_MISMATCH',
                    '审片执行需要完全复现本版唯一已审求解、采用范围及技术预检')
            require(SINGLE not in scopes or 'geometry' not in request, 'INVALID_REQUEST',
                    '单域执行直接使用当前候选中的闭合代理，无需另交多域采样工程')
            return create_audition(bpy, scene, entry, request, plan, surface, live_domains, run)
        try:
            finish_scope = entry.finish_scope()
        except workflow_entry.EntryError as error:
            require(error.code == 'INCOMPLETE_BINDING', error.code, str(error))
            finish = {'ready': False, 'code': error.code}
        else:
            finish = {'ready': True, 'scope': finish_scope}
        return {'identity_sha256': permit.identity_sha256, 'scopes': scopes, 'preflight': technical,
                'scope_sha256': digests, 'reviewed_operations': signed, 'finish_preflight': finish,
                'transfer_preflight': transfer_info}
    finally:
        try:
            if entry is not None and entry.state not in {'finished', 'aborted'}:
                require(entry.abort()['cleanup_complete'], 'CAPTURE_CLEANUP', '临时 RAW 捕获未完整撤回')
        finally:
            skeleton_gate.revoke(rig)



def sample_audition(bpy, scene, rig, bound, review, motion):
    import numpy as np
    from rigging_workflow import skeleton_qc, skinning_job

    require(isinstance(motion, dict) and set(motion) == {'action', 'slot', 'frames'},
            'INVALID_REQUEST', '需要已审 Action、明确槽和审片帧')
    require(motion['action'] in review.get('runtime_binding', {}).get('check_actions', ()),
            'REVIEW_SCOPE_DENIED', '审片动作必须来自此操作回执中的已审 Action')
    action = bpy.data.actions.get(motion['action'])
    slot = next((item for item in action.slots if item.identifier == motion['slot']), None) if action else None
    frames = motion['frames']
    require(slot is not None and isinstance(frames, list) and 2 <= len(frames) <= 64
            and all(type(frame) is int and action.frame_range[0] <= frame <= action.frame_range[1] for frame in frames)
            and len(set(frames)) == len(frames), 'INVALID_REQUEST', '审片帧和 Action 槽须实际存在且明确')
    pose = skeleton_qc._runtime_state(rig)
    prior_frame = scene.frame_current, scene.frame_subframe
    had_animation_data = rig.animation_data is not None
    ad = rig.animation_data_create()
    old_action, old_slot = ad.action, ad.action_slot
    neutral = [skinning_job._points(obj) for obj in bound]
    states = []
    try:
        ad.action = action
        ad.action_slot = slot
        for frame in frames:
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            parts = []
            for obj, reference in zip(bound, neutral, strict=True):
                evaluated = skinning_job._points(obj)
                require(evaluated.shape == reference.shape and np.isfinite(evaluated).all(),
                        'MOTION_EVALUATION_FAILED', f'动作求值拓扑或坐标无效：{obj.name}')
                displacement = np.linalg.norm(evaluated - reference, axis=1)
                parts.append({'name': obj.name, 'max_displacement_meters': float(displacement.max(initial=0))
                              * scene.unit_settings.scale_length})
            states.append({'frame': frame, 'parts': parts})
    finally:
        ad.action = old_action
        if old_action is not None:
            ad.action_slot = old_slot
        if not had_animation_data:
            rig.animation_data_clear()
        scene.frame_set(prior_frame[0], subframe=prior_frame[1])
        skeleton_qc._restore_runtime(pose)
    require(any(part['max_displacement_meters'] > 1e-5 for state in states for part in state['parts']),
            'MOTION_EVALUATION_FAILED', '所选动作帧没有产生可检的变形或刚性移动')
    for obj, reference in zip(bound, neutral, strict=True):
        error = np.linalg.norm(skinning_job._points(obj) - reference, axis=1).max(initial=0)
        require(error * scene.unit_settings.scale_length <= 1e-5, 'MOTION_RESTORE_FAILED', '摆姿后未恢复中立表面')
    return {'action': action.name, 'slot': slot.identifier, 'states': states, 'neutral_restored': True}


def isolate_finished_character(bpy, rig, bound):
    """Set the presentation of the output copy without changing rig or mesh data."""
    layer = bpy.context.view_layer
    character = {rig, *bound}
    for obj in layer.objects:
        if obj.type in {'ARMATURE', 'MESH'}:
            obj.hide_set(obj not in character, view_layer=layer)
            if obj.type == 'MESH':
                obj.hide_render = obj not in character
        if obj.select_get(view_layer=layer):
            obj.select_set(False, view_layer=layer)
    layer.objects.active = rig
    rig.select_set(True, view_layer=layer)


def create_audition(bpy, scene, entry, request, plan, surface, domains, run):
    from rigging_workflow import candidate_state, skeleton_collection, skeleton_gate

    scopes = plan['scopes']
    solve = scopes[SINGLE] if SINGLE in scopes else scopes[MULTI]
    apply = scopes[APPLY]
    rigid = [scopes[RIGID]] if RIGID in scopes else []
    transfers = [scopes[TRANSFER]] if TRANSFER in scopes else []
    indices = [solve['part_index'], *(part['part_index'] for part in rigid), *(part['part_index'] for part in transfers)]
    require(sorted(indices) == list(range(len(entry.pending))), 'INCOMPLETE_BINDING', '审片需要逐件覆盖捕获的角色部件')
    review = request['operation_review']
    receipt = frozen(review)
    _, evidence, _ = skeleton_collection.load_receipt(receipt, expected_sha256=review['sha256'])
    identity = read_json(frozen(request['capture_review']['identity']))
    reference = scene_mesh(scene, request['capture_review']['reference'])
    finish_review = request.get('finish_review')
    if finish_review:
        expected_finish = read_json(frozen(request['finish_scope']))
        finish_digest = expected_finish['scope_sha256']
        require(expected_finish['identity'] == plan['identity']
                and expected_finish['scope']['candidate_id'] == request['candidate_id']
                and candidate_state.scope_digest(expected_finish['scope']) == finish_digest,
                'FINISH_SCOPE_MISMATCH', '审片完成范围与冻结候选不同')
        reviewed_operations(finish_review, plan['identity'], {FINISH: finish_digest})
        finish_receipt = frozen(finish_review)
        _, finish_evidence, _ = skeleton_collection.load_receipt(
            finish_receipt, expected_sha256=finish_review['sha256'])
        if 'manual_handoff' in request:
            frozen(request['manual_handoff'], '.md')
            require(request['manual_handoff'] in finish_evidence['dependencies'], 'FINISH_REVIEW_MISMATCH',
                    '人工修复交接须与正式完成回执绑定')
    for name in evidence['runtime_binding'].get('control_modules', ()):
        import_module(f'rigging_workflow.{name}')
    permit = skeleton_gate.authorize(entry.rig, reference, identity['regions'], receipt)
    require(permit.receipt_sha256 == review['sha256'] and permit.identity_sha256 == plan['identity'],
            'REVIEW_SCOPE_DENIED', '实时采用许可和审片计划不一致')
    for name, scope in scopes.items():
        candidate_state.require_operation(entry.rig, operation=name, scope=scope)
    check_cancel(run)
    write_json(run / 'live-preflight.json', plan)
    jobs_directory = Path(request.get('worker_jobs_dir', run / 'jobs')).resolve()
    if SINGLE in scopes:
        proxy = scene_mesh(scene, solve['proxy']['object'])
        job = entry.start_volume(solve['part_index'], proxy, solve['bone_names'], solve['settings'],
                                 jobs_directory=jobs_directory)
        solve_timeout = solve['settings'].get('max_seconds', 180)
    else:
        job = entry.start_plan(solve['part_index'], surface, domains, solve['rigid'], jobs_directory=jobs_directory,
                               transfer_distance_meters=solve['transfer_distance_meters'])
        solve_timeout = sum(row['settings'].get('max_seconds', 180) for row in domains)
    deadline = time.monotonic() + min(AUDITION_TIMEOUT - 60, 60 + solve_timeout)
    while True:
        status = job.status()
        if status['status'] not in {'queued', 'running'}:
            break
        if (run / 'cancel.request').exists() or time.monotonic() >= deadline:
            job.cancel()
            check_cancel(run)
            require(False, 'AUDITION_TIMEOUT', '后台求权作业超过已审时限')
        time.sleep(1)
    write_json(run / 'solve-status.json', status)
    require(status['status'] == 'succeeded' and status['can_apply'], 'SOLVE_FAILED',
            f'实际后台求解未达可采用状态：{status.get("error", status["status"])}')
    check_cancel(run)
    require(job.apply_scope(name=apply['name']) == apply, 'PLAN_SCOPE_MISMATCH', '实际采用范围未匹配同次已审求解')
    by_index = {solve['part_index']: job.apply(name=apply['name'])}
    for part in rigid:
        check_cancel(run)
        by_index[part['part_index']] = entry.bind_rigid(part['part_index'], part['assignments'], name=part['name'])
    for part in transfers:
        check_cancel(run)
        donor = by_index[part['donor_part_index']]
        by_index[part['part_index']] = entry.bind_transfer(
            part['part_index'], donor,
            max_distance_meters=part['max_distance_meters'],
            name=part['name'],
            bone_names=part['bone_names'],
            donor_vertex_indices=part.get('donor_vertex_indices'),
            planned_scope=part,
        )
    bound = [by_index[index] for index in sorted(by_index)]
    finish = entry.finish_scope()
    motion = sample_audition(bpy, scene, entry.rig, bound, evidence, request['motion'])
    require(entry.finish_scope() == finish, 'FINISH_SCOPE_MISMATCH', '审片动作采样后部件范围变化')
    write_json(run / 'skin-motion.json', motion)
    finish_sha = candidate_state.scope_digest(finish)
    finish_record = {'scope': finish, 'scope_sha256': finish_sha,
                     'identity': permit.identity_sha256, 'objects': [obj.name for obj in bound]}
    check_cancel(run)
    source = Path(request['source']['path'])
    require(file_digest(source) == request['source']['sha256'], 'SOURCE_CHANGED', '源工程在审片期间改变')
    label = 'finished' if finish_review else 'audition'
    pending = run / (label + '.pending.blend')
    target = run / (label + '.blend')
    require(not pending.exists() and not target.exists(), 'OUTPUT_EXISTS', '审片输出已占用')
    if finish_review:
        require(finish_record == expected_finish, 'FINISH_SCOPE_MISMATCH', '当前实绑部件与已审完成范围不同')
        for name in finish_evidence['runtime_binding'].get('control_modules', ()):
            import_module(f'rigging_workflow.{name}')
        final_permit = skeleton_gate.authorize(entry.rig, reference, identity['regions'], finish_receipt)
        require(final_permit.receipt_sha256 == finish_review['sha256']
                and final_permit.identity_sha256 == plan['identity'], 'FINISH_REVIEW_MISMATCH',
                '正式完成许可与已审骨架或回执不同')
        completed = entry.finish()
        finish_record['finish_review'] = finish_review
        finish_record['complete_parts'] = [durable_part(obj) for obj in completed['objects']]
        if request.get('isolate_bound'):
            isolate_finished_character(bpy, entry.rig, completed['objects'])
        if 'manual_handoff' in request:
            finish_record['manual_handoff'] = request['manual_handoff']
    write_json(run / 'finish-scope.json', finish_record)
    require(bpy.ops.wm.save_as_mainfile(filepath=str(pending), copy=True) == {'FINISHED'},
            'SAVE_FAILED', '审片工程无法落盘')
    check_cancel(run)
    require(file_digest(source) == request['source']['sha256'], 'SOURCE_CHANGED', '保存期间源工程发生变化')
    pending.rename(target)
    audition = {'blend': {'path': str(target), 'sha256': file_digest(target)},
                'source': request['source'], 'formal_finish': finish_review is not None}
    if request.get('isolate_bound'):
        audition['isolated_bound'] = True
    if 'manual_handoff' in finish_record:
        audition['manual_handoff'] = finish_record['manual_handoff']
    write_json(run / 'audition.json', audition)
    domain_rows = status['statistics'].get('domains', ())
    cache_hits = sum(row.get('resumption', {}).get('reused', False) is True for row in domain_rows)
    return {'audition': audition, 'bound_parts': finish['bound_parts'], 'finish_scope_sha256': finish_sha,
            'motion_states': len(motion['states']), 'weights_sha256': status['weights_sha256'],
            'worker_jobs_dir': str(jobs_directory), 'domain_cache_hits': cache_hits,
            'worker_started': True, 'weights_written': True, 'formal_finish': audition['formal_finish']}


def durable_part(obj, *, as_audition=False, part_identity=None):
    from rigging_workflow import workflow_entry

    require(part_identity in {None, workflow_entry.FINISH_PART_IDENTITY},
            'FINISH_SCOPE_MISMATCH', '不支持此绑定部件身份版本')
    fingerprint = workflow_entry.finish_part if part_identity else workflow_entry._durable_source
    if as_audition:
        state = workflow_entry._source_state(obj)
        marker = json.loads(state['props'][workflow_entry.KEY])
        marker['status'] = 'bound_candidate'
        state['props'][workflow_entry.KEY] = json.dumps(marker, ensure_ascii=False)
        return fingerprint(obj, state)
    return fingerprint(obj)


def verify_saved_result(bpy, request, source_path, capture_scope, capture_sha):
    from rigging_workflow import candidate_state, generation, skeleton_collection, workflow_entry

    plan = retained_plan(request, capture_sha)
    require(APPLY in plan['scopes'], 'RESULT_PLAN_MISSING', '此记录缺少同次采用的范围')
    status = read_json(frozen(request['solve_status']))
    audition = read_json(frozen(request['audition']))
    finish = read_json(frozen(request['finish']))
    skin = read_json(frozen(request['skin_motion']))
    require(audition['source'] == request['source'], 'RESULT_SOURCE_MISMATCH', '审片源与冻结工程不同')
    review_file = frozen(audition['blend'], '.blend')
    require(review_file != source_path, 'INVALID_REQUEST', '审片结果必须是独立工程')
    job = Path(status['job_directory'])
    require(job.is_absolute() and job.is_dir(), 'RESULT_JOB_MISSING', '原生后台结果目录不可读')
    result = read_json(job / 'result.json')
    worker_input = read_json(job / 'input.json')
    applied = read_json(job / 'apply-receipt.json')
    proof = worker_input['quality_gate']
    receipt = reviewed_operations(proof['receipt'], plan['identity'], plan['scope_sha256'], worker=True)
    require(proof['identity_sha256'] == receipt['identity_sha256']
            and proof['scope_sha256'] == receipt['scope_sha256'],
            'RESULT_REVIEW_MISMATCH', '求解、采用或部件的实际回执与计划不一致')
    request_sha = skeleton_collection.digest({key: value for key, value in worker_input.items() if key != 'quality_gate'})
    require(status['status'] == result['status'] == 'succeeded' and not status['cancel_requested']
            and status['authorization'] == result['authorization']
            and proof['request_sha256'] == result['authorization']['request_sha256'] == request_sha
            and status['bone_names'] == result['bone_names'] == worker_input['bone_names'],
            'RESULT_JOB_MISMATCH', '状态记录不是本次成功后台作业')
    expected_bones = plan.get('preflight', {}).get('bone_names')
    if expected_bones is None and SINGLE in plan['scopes']:
        expected_bones = plan['scopes'][SINGLE]['bone_names']
    apply_scope = plan['scopes'][APPLY]
    require(expected_bones == result['bone_names']
            and status['index'] == apply_scope['part_index'] and status['source'] == apply_scope['original']['object']
            and (MULTI not in plan['scopes'] or worker_input['summary'] == plan['preflight']),
            'RESULT_JOB_MISMATCH', '后台骨列、源部件或多域技术摘要与审核计划不同')
    weights = job / 'weights.npz'
    require(weights.is_file() and file_digest(weights) == applied['weights_sha256']
            == result['weights_sha256'] == status['weights_sha256']
            and applied['ok'] and applied['source_unchanged'] and applied['candidate'] == plan['scopes'][APPLY]['name'],
            'RESULT_WEIGHTS_MISMATCH', '采用回执未绑定原生结果文件、目标或权重 SHA')
    scope = finish['scope']
    require(finish['identity'] == plan['identity'] and scope['candidate_id'] == request['candidate_id']
            and candidate_state.scope_digest(scope) == finish['scope_sha256']
            and finish['objects'] == [part['object'] for part in scope['bound_parts']]
            and len(scope['bound_parts']) == len(capture_scope['sources']),
            'FINISH_SCOPE_MISMATCH', '结束范围与候选、部件或范围 SHA 不同')
    scene = open_scene(bpy, review_file, request)
    rig, _ = candidate_state.resolve(scene, request['candidate_id'], kind=workflow_entry.CANDIDATE_KIND)
    method = json.loads(rig[generation.RIG_KEY]).get('skinning')
    formal = audition['formal_finish']
    require(type(formal) is bool and formal == (method == 'workflow_entry_bound'), 'FINISH_STATE_MISMATCH',
            '审片文件的实际骨架状态与结束记录不同')
    if formal:
        require('finish_review' in finish, 'FINISH_REVIEW_MISMATCH', '已完成的工程缺少精确完成审核')
        reviewed_operations(finish['finish_review'], plan['identity'], {FINISH: finish['scope_sha256']})
        require(finish.get('manual_handoff') == audition.get('manual_handoff'), 'FINISH_REVIEW_MISMATCH',
                '完工工程的人工交接记录不同')
        if 'manual_handoff' in finish:
            frozen(finish['manual_handoff'], '.md')
            _, final_evidence, _ = skeleton_collection.load_receipt(frozen(finish['finish_review']),
                expected_sha256=finish['finish_review']['sha256'])
            require(finish['manual_handoff'] in final_evidence['dependencies'], 'FINISH_REVIEW_MISMATCH',
                    '人工交接须属于完成审核')
    parts = []
    for expected, original in zip(scope['bound_parts'], capture_scope['sources'], strict=True):
        part = scene_mesh(scene, expected['object'])
        native = workflow_entry._durable_source(part)
        marker = json.loads(part.get(workflow_entry.KEY, '{}'))
        reviewed_part = durable_part(part, as_audition=formal, part_identity=scope.get('part_identity'))
        require(reviewed_part == expected and marker.get('rig') == rig.name and marker.get('source') == original['object']
                and marker.get('status') == ('complete' if formal else 'bound_candidate')
                and [mod.object for mod in part.modifiers if mod.type == 'ARMATURE'] == [rig],
                'RESULT_OBJECT_MISMATCH', f'真实采用网格已改变或缺少同一实际骨架：{part.name}')
        parts.append(native)
    if formal:
        require(parts == finish.get('complete_parts'), 'FINISH_STATE_MISMATCH', '完成后的部件内容不同')
    if audition.get('isolated_bound'):
        layer = bpy.context.view_layer
        bound_objects = {scene.objects[row['object']] for row in parts}
        expected_visible = bound_objects | {rig}
        visible = {obj for obj in layer.objects
                   if obj.type in {'ARMATURE', 'MESH'} and obj.visible_get(view_layer=layer)}
        require(formal and visible == expected_visible and layer.objects.active == rig
                and all(obj.hide_render == (obj not in bound_objects) for obj in layer.objects
                        if obj.type == 'MESH'),
                'FINISH_PRESENTATION_MISMATCH', '交付工程的可见部件、渲染或活跃骨架不符')
    require(parts and isinstance(skin.get('states'), list) and skin['states'], 'MOTION_EVIDENCE_MISSING', '缺少已采的动作状态')
    require(file_digest(review_file) == audition['blend']['sha256'], 'ARTIFACT_SHA_MISMATCH', '审片工程在检查期间改变')
    return {'audition': audition, 'bound_parts': parts, 'finish_scope': scope,
            'finish_scope_sha256': finish['scope_sha256'], 'formal_finish': audition['formal_finish'],
            'weights_sha256': status['weights_sha256'], 'motion_states': len(skin['states']),
            'worker_verified_from_artifacts': True}


def worker(mode, request, run):
    validate(mode, request)
    source = checked_descriptor(request['source'], '.blend', 'SOURCE_SHA_MISMATCH')
    bpy, _, _ = configure_host('v003')
    from rigging_workflow import candidate_state, workflow_entry

    scene = open_scene(bpy, source, request)
    inspected = workflow_entry.inspect_candidate(scene, request['candidate_id'])
    scope = workflow_entry.capture_scope(scene, request['candidate_id'])
    capture_sha = candidate_state.scope_digest(scope)
    result = {'ok': True, 'mode': mode, 'build': bpy.app.build_hash.decode(), 'source': request['source'],
              'candidate_id': request['candidate_id'], 'rig': inspected['rig'].name,
              'capture_scope': scope, 'capture_scope_sha256': capture_sha,
              'weights_written': False, 'worker_started': False, 'submission_authority': False}
    if mode in {'scope', 'audition', 'finish'}:
        result.update(native_scopes(bpy, scene, request, inspected['rig'], scope, capture_sha,
                                    run=run if mode in {'audition', 'finish'} else None))
    if mode == 'result':
        result.update(verify_saved_result(bpy, request, source, scope, capture_sha))
    require(file_digest(source) == request['source']['sha256'], 'SOURCE_CHANGED', '检查期间源文件发生变化')
    result['source_unchanged'] = True
    return result


def error_result(error):
    return {'ok': False, 'weights_written': False, 'worker_started': False, 'submission_authority': False,
            'error': {'code': getattr(error, 'code', 'NATIVE_REJECTED'), 'message': str(error)}}


def main():
    if '--' in sys.argv:
        tag, mode, snapshot, output = sys.argv[sys.argv.index('--') + 1:]
        require(tag == '_worker', 'INVALID_REQUEST', '无效内部入口')
        try:
            result = worker(mode, read_json(Path(snapshot)), Path(output).parent)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            result = error_result(error)
        write_json(Path(output), result)
        return 0 if result['ok'] else 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('inspect', 'scope', 'result', 'audition', 'finish'))
    parser.add_argument('--request', required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--blender', required=True, help='Absolute path to the audited Blender executable')
    args = parser.parse_args()
    output = None
    try:
        request = read_json(absolute_file(args.request, '.json'))
        validate(args.mode, request)
        blender = absolute_file(args.blender, '.exe')
        run = Path(args.run_dir)
        require(run.is_absolute() and run.parent.is_dir(), 'INVALID_RUN_DIRECTORY', '使用父目录已存在的新绝对目录')
        run.mkdir()
        snapshot, report = run / 'request.json', run / 'result.json'
        write_json(snapshot, request)
        command = [str(blender), '--background', '--factory-startup', '--offline-mode', '--disable-autoexec',
                   '--threads', '2', '--python-exit-code', '1', '--python', str(SCRIPT), '--',
                   '_worker', args.mode, str(snapshot), str(report)]
        with (run / 'blender.log').open('xb') as log:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=AUDITION_TIMEOUT if args.mode in {'audition', 'finish'} else 180, check=False,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        require(report.is_file(), 'WORKER_FAILED', f'Blender 退出码 {process.returncode}，查询 blender.log')
        result = read_json(report)
        require(not result['ok'] or process.returncode == 0, 'WORKER_FAILED', 'Blender 非零退出')
        output = report
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result = error_result(error)
    print(json.dumps({'ok': result['ok'], 'result': str(output) if output else None,
                      'weights_written': result['weights_written'], 'worker_started': result['worker_started'],
                      'error': result.get('error')}))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
