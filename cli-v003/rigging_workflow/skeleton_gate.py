# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed shared boundary for weight operations in this isolated candidate."""
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import hmac
import math
import os
import secrets
import subprocess
import sys
import time
import uuid
import bpy
from . import skeleton_qc as qc
from . import skeleton_collection as collection

# Gate reload must not carry old static validation cache entries.
collection.clear_receipt_cache()

GateError = collection.GateError
_sessions = {}
_active_write = ContextVar('rigging_native_write_scope', default=None)
_active_verified = ContextVar('rigging_native_verified_closure', default=None)
_worker_calls = frozenset({'启动冻结后台求权'})
_delegated_calls = {
    'workflow_entry.start_volume': _worker_calls | {'skinning_job.start', 'skinning_job.Job'},
    'workflow_entry.start_plan': _worker_calls | {'skinning_plan.start', 'skinning_plan.MultiJob', 'skinning_job.Job'},
    'local_skinning_job.start_selected': _worker_calls | {'local_skinning_job.LocalVolumeJob', 'skinning_policy.create_scope_preview'},
    'local_skinning_job.apply_selected': {'skinning_policy.prepare_local_field', 'skinning_policy.PreparedWeights'},
    'workflow_entry.apply_volume': {'skinning_job.Job', 'skinning_plan.MultiJob', 'skinning.create_bound_copy'},
    'workflow_entry.bind_rigid': {'skinning_policy.prepare_rigid', 'skinning_policy.PreparedWeights',
                                 'skinning.create_bound_copy'},
    'workflow_entry.bind_transfer': {'skinning_policy.prepare_transfer', 'skinning_policy.PreparedWeights',
                                    'skinning.create_bound_copy'},
}


def _revoke_pointer(pointer):
    session = _sessions.pop(pointer, None)
    if session is not None:
        session.get('worker_grants', {}).clear()
        session.pop('worker_key', None)


@dataclass(frozen=True)
class Permit:
    permission_id: str
    rig_pointer: int
    receipt_path: str
    receipt_sha256: str
    identity_sha256: str
    scope_sha256: str


def current_identity(rig, reference, regions, *, check_actions=(), control_texts=(),
                     control_modules=(), dependencies=(), _verified_files=None):
    """Bind checked Actions, actual control source and native rig RNA."""
    palette = qc.validate(rig, reference, regions)
    scene = bpy.context.scene
    collection.need(scene.objects.get(rig.name) == rig and scene.objects.get(reference.name) == reference,
                    '骨架或参照不属于当前实际场景')
    collection.need(not reference.constraints and not reference.data.animation_data,
                    '静态参照不能带约束或数据动画/驱动')
    action_names = set(check_actions)
    for owner in [rig] + list(rig.pose.bones):
        for constraint in owner.constraints:
            action = getattr(constraint, 'action', None)
            if isinstance(action, bpy.types.Action):
                action_names.add(action.name)
    actions = []
    for name in sorted(action_names):
        action = bpy.data.actions.get(name)
        collection.need(action is not None, '检查Action已丢失：' + name)
        actions.append(qc._action(action))
    text_names = set(control_texts)
    generated_ui = rig.get('rig_ui')
    if isinstance(generated_ui, bpy.types.Text):
        text_names.add(generated_ui.name)
    texts = []
    for name in sorted(text_names):
        text = bpy.data.texts.get(name)
        collection.need(text is not None, '控制源码Text已丢失：' + name)
        texts.append({'name': name, 'content_sha256': qc.digest(text.as_string()),
                      'use_module': bool(text.use_module), 'filepath': text.filepath,
                      'is_in_memory': bool(text.is_in_memory)})
    collection.need(isinstance(control_modules, (list, tuple))
                    and all(isinstance(name, str) and name.isidentifier() for name in control_modules),
                    '控制模块必须是本包具名模块')
    modules = []
    package_dir = Path(__file__).resolve().parent
    for name in sorted(set(control_modules)):
        path = package_dir / f'{name}.py'
        loaded = sys.modules.get(f'{__package__}.{name}')
        actual = getattr(loaded, '__file__', None)
        collection.need(actual and Path(actual).resolve() == path,
                        '当前未从本包加载控制模块：' + name)
        modules.append({'name': name, **collection.descriptor(path)})
    collection.need(bool(actions) and bool(texts or modules), '必须绑定已验Action和实际控制源码内容')
    frame, subframe = scene.frame_current, scene.frame_subframe
    runtime = qc._runtime_state(rig)
    try:
        scene.frame_set(1, subframe=0); bpy.context.view_layer.update()
        canonical_basis = {p.name: {'matrix_basis': [list(r) for r in p.matrix_basis],
                                   'rotation_mode': p.rotation_mode} for p in rig.pose.bones}
    finally:
        scene.frame_set(frame, subframe=subframe); qc._restore_runtime(runtime)
    verified_deps = collection.verify_files_batch(dependencies, preverified=_verified_files) if dependencies else {}
    source_dependencies = []
    for d in dependencies:
        resolved_key = str(Path(d['path']).resolve()).casefold()
        source_dependencies.append({'path': str(Path(d['path'])), 'sha256': verified_deps[resolved_key]})
    identity = {'native': qc.identity(rig, reference, regions, palette),
            'check_actions': actions, 'control_texts': texts,
            'canonical_basis': canonical_basis, 'pose_position': rig.data.pose_position,
            'source_dependencies': source_dependencies,
            'scene_units': {'scale_length': scene.unit_settings.scale_length,
                            'system': scene.unit_settings.system},
            'blender_version': list(bpy.app.version)}
    if modules:
        identity['control_modules'] = modules
    return identity


def _identity(rig, session, *, _verified_files=None):
    doc = session['doc']; binding = doc.get('runtime_binding', {})
    collection.need(isinstance(binding, dict), '缺少当前运行身份绑定')
    return qc.digest(current_identity(rig, session['reference'], session['regions'],
        check_actions=binding.get('check_actions', ()), control_texts=binding.get('control_texts', ()),
        control_modules=binding.get('control_modules', ()),
        dependencies=binding.get('dependencies', ()),
        _verified_files=_verified_files))


def authorize(rig, reference, regions, receipt_path, *, required_scopes=()):
    """Explicit registration only; failed replacement revokes existing authority."""
    collection.need(rig is not None and getattr(rig, 'type', None) == 'ARMATURE', '没有可验收骨架')
    pointer = rig.as_pointer(); _revoke_pointer(pointer)
    receipt, doc, facts, verified = collection._load_receipt_detailed(receipt_path)
    collection.need(set(facts['scope']['required_bones']) == set(rig.data.bones.keys()),
                    '完整骨架回执不得缩成少量骨')
    collection.need(set(facts['scope']['required_controls']) <= set(rig.pose.bones.keys()),
                    '回执声明了不存在的控制骨')
    receipt_key = str(Path(receipt_path).resolve()).casefold()
    session = {'rig': rig, 'reference': reference, 'regions': dict(regions), 'doc': doc,
               'scene': bpy.context.scene,
               'receipt': {'path': str(Path(receipt_path)), 'sha256': verified[receipt_key]}}
    collection.need(_identity(rig, session, _verified_files=verified) == facts['identity_sha256'],
                    '骨架、参照、Action或控制源码已改变')
    scopes = facts['scope'].get('agent_write_scopes', {})
    allowed = facts['scope']['allowed_operations']
    for operation, write_scope_sha256 in required_scopes:
        collection.need(isinstance(operation, str) and operation.strip(), '操作职责缺失')
        collection.need(operation in allowed, '回执范围不允许操作：' + operation)
        if scopes or write_scope_sha256 is not None:
            approved = scopes.get(operation, ())
            collection.need(write_scope_sha256 is not None and write_scope_sha256 in approved,
                            '回执缺少此操作的准确写入范围：' + operation)
        collection.need(not facts['scope'].get('test_only') or operation == '__gate_validation_only__',
                        '工具测试回执不能授权真实蒙皮操作')
    permit = Permit(uuid.uuid4().hex, pointer, session['receipt']['path'], session['receipt']['sha256'],
                    facts['identity_sha256'], facts['scope_sha256'])
    session.update(permit=permit, worker_key=secrets.token_bytes(32), worker_grants={})
    _sessions[pointer] = session
    _install_lifecycle()
    return permit


def revoke(rig):
    if rig is not None:
        _revoke_pointer(rig.as_pointer())


def _require_pass_detailed(rig, *, operation, write_scope_sha256=None):
    collection.need(isinstance(operation, str) and operation.strip(), '操作职责缺失')
    try:
        valid = rig is not None and getattr(rig, 'type', None) == 'ARMATURE'
        pointer = rig.as_pointer() if valid else None
    except ReferenceError as exc:
        raise GateError('骨架质量门：原骨架已被加载/撤销/删除替换，许可失效') from exc
    collection.need(valid, '缺少有效骨架许可')
    session = _sessions.get(pointer)
    collection.need(session is not None, '未登记当前骨架的完整验收回执，禁止' + operation)
    try:
        collection.need(session['rig'] == rig and bpy.context.scene == session['scene'], '骨架或场景已更换')
        active = _active_write.get()
        active_ver = _active_verified.get()
        in_active_scope = (active is not None and active[0] is session['permit']
                           and active_ver is not None and active_ver[0] is session['permit'])
        preverified = active_ver[1] if in_active_scope else None
        receipt, doc, facts, verified = collection._load_receipt_detailed(
            session['receipt']['path'],
            expected_sha256=session['receipt']['sha256'],
            preverified_closure=preverified,
        )
        scopes = facts['scope'].get('agent_write_scopes', {})
        delegated = (write_scope_sha256 is None and active is not None
                     and active[0] is session['permit']
                     and operation in _delegated_calls.get(active[1], ()))
        collection.need(operation in facts['scope']['allowed_operations'] or delegated,
                        '回执范围不允许操作：' + operation)
        if (scopes or write_scope_sha256 is not None) and not delegated:
            approved = scopes.get(operation, ())
            collection.need(write_scope_sha256 is not None and write_scope_sha256 in approved,
                            '回执缺少此操作的准确写入范围：' + operation)
        collection.need(not facts['scope'].get('test_only') or operation == '__gate_validation_only__',
                        '工具测试回执不能授权真实蒙皮操作')
        collection.need(doc == session['doc'], '证据集合已更换')
        if in_active_scope:
            # Entry of this synchronous _scoped_operation already verified full RNA identity;
            # confirm live pointers and pose position remain intact without re-serializing Actions 6x.
            collection.need(rig.data.as_pointer() == active_ver[2]
                            and session['reference'].as_pointer() == active_ver[3]
                            and len(rig.data.bones) == active_ver[4]
                            and rig.data.pose_position == active_ver[5],
                            '骨架或参照在同次写入域内发生改变，许可已撤销')
        else:
            collection.need(_identity(rig, session, _verified_files=verified) == session['permit'].identity_sha256,
                            '骨架、参照、控制、Action或输入来源已失效，先复验')
    except Exception as exc:
        _revoke_pointer(pointer)
        if isinstance(exc, GateError):
            raise
        raise GateError('骨架质量门：运行身份无法复核，许可已撤销') from exc
    return session['permit'], verified


def require_pass(rig, *, operation, write_scope_sha256=None):
    permit, _ = _require_pass_detailed(rig, operation=operation, write_scope_sha256=write_scope_sha256)
    return permit


@contextmanager
def _scoped_operation(rig, *, operation, scope_sha256):
    """Call native write internals only inside the parent scope that was reviewed."""
    permit, verified = _require_pass_detailed(rig, operation=operation, write_scope_sha256=scope_sha256)
    session = _sessions[permit.rig_pointer]
    scope_guard = (permit, verified, rig.data.as_pointer(), session['reference'].as_pointer(),
                   len(rig.data.bones), rig.data.pose_position)
    previous = _active_write.set((permit, operation))
    previous_ver = _active_verified.set(scope_guard)
    try:
        yield permit
    finally:
        _active_verified.reset(previous_ver)
        _active_write.reset(previous)


def check_pass(rig, token, *, operation):
    current = require_pass(rig, operation=operation)
    collection.need(type(token) is Permit and current is token, '准备后许可已更换或token非本会话原件，不能提交旧结果')
    return current


def _manifest_bones(rig, manifest):
    """Bind every solver bone row to the current native rest/world/unit input."""
    collection.need(isinstance(manifest, dict), '后台请求必须为字典')
    schema = manifest.get('schema')
    single = schema in {'c94.single-domain-volume-job.v1', 'c94.local-volume-job.v1'}
    multi = schema == 'c94.multi-domain-job.v1'
    collection.need(single or multi, '未知后台作业schema')
    scale = float(bpy.context.scene.unit_settings.scale_length)
    collection.need(math.isfinite(scale) and scale > 0, '后台场景单位无效')
    summary = manifest.get('summary', {})
    collection.need(isinstance(summary, dict), '后台summary格式无效')
    declared_scale = manifest.get('scene_scale_length') if schema == 'c94.single-domain-volume-job.v1' else summary.get('scene_scale_length')
    coordinates = manifest.get('coordinates') if single else summary.get('coordinates')
    collection.need(type(declared_scale) in (int, float) and declared_scale == scale
                    and coordinates == 'meters', '后台坐标或场景单位与当前骨架不符')
    names = manifest.get('bone_names')
    all_names = collection.unique_strings(names, '后台最终骨列')

    def native(name):
        bone = rig.data.bones.get(name)
        collection.need(bone is not None and bone.use_deform, '后台包含非最终形变骨：' + name)
        return bone

    for name in names:
        native(name)

    def segments(rows, ordered):
        collection.unique_strings(ordered, '后台域骨列')
        collection.need(isinstance(rows, list) and len(rows) == len(ordered)
                        and all(isinstance(r, dict) for r in rows)
                        and [r.get('name') for r in rows] == ordered, '后台骨段与有序骨列不符')
        for row, name in zip(rows, ordered):
            bone = native(name)
            for field, vector in (('head', bone.head_local), ('tail', bone.tail_local)):
                value = row.get(field)
                expected = [float(v) * scale for v in rig.matrix_world @ vector]
                collection.need(isinstance(value, list) and len(value) == 3
                    and all(type(v) in (int, float) and math.isfinite(v) for v in value)
                    and all(v == e for v, e in zip(value, expected)),
                    '后台骨段不是当前rest/world/单位输入：' + name)

    if single:
        segments(manifest.get('bones'), names)
    else:
        domains, rigid = manifest.get('domains'), manifest.get('rigid')
        collection.need(isinstance(domains, list) and domains and isinstance(rigid, list), '后台多域计划缺失')
        covered = set()
        for domain in domains:
            collection.need(isinstance(domain, dict), '后台体积域格式无效')
            ordered = domain.get('bone_names')
            segments(domain.get('bones'), ordered)
            covered.update(ordered)
        for row in rigid:
            collection.need(isinstance(row, dict) and isinstance(row.get('bone_name'), str), '后台刚性骨格式无效')
            native(row['bone_name']); covered.add(row['bone_name'])
        collection.need(covered == all_names, '后台多域/刚性骨与最终骨集合不符')


def worker_proof(rig, token, manifest):
    """Issue public metadata plus an unexported, short-lived one-shot grant."""
    check_pass(rig, token, operation='启动冻结后台求权')
    collection.need('quality_gate' not in manifest, '不能重发或覆盖后台证明')
    session = _sessions[token.rig_pointer]
    collection.need('worker.solve' in session['doc']['scope']['allowed_operations'], '回执范围不允许后台求权')
    _manifest_bones(rig, manifest)
    grants = session['worker_grants']
    now = time.monotonic()
    for nonce, grant in list(grants.items()):
        if now > grant['expires']:
            del grants[nonce]
    nonce = secrets.token_hex(32)
    proof = {'schema': 'rigging.skeleton-worker-proof.v2',
             'receipt': {'path': token.receipt_path, 'sha256': token.receipt_sha256},
             'identity_sha256': token.identity_sha256, 'scope_sha256': token.scope_sha256,
             'request_sha256': collection.digest(manifest), 'nonce': nonce,
             'parent_pid': os.getpid(), 'permission_id': token.permission_id}
    grants[nonce] = {'token': token, 'proof_sha256': collection.digest(proof),
                     'request_sha256': proof['request_sha256'], 'expires': now + 120.,
                     'key': hmac.new(session['worker_key'], nonce.encode('ascii'), 'sha256').digest()}
    return proof


def spawn_worker(rig, token, manifest, args, **kwargs):
    """Start only this owned child and hand off a live grant through its stdin."""
    from . import worker_channel
    process = None
    proof = manifest.get('quality_gate') if isinstance(manifest, dict) else None
    nonce = proof.get('nonce') if isinstance(proof, dict) else None
    grants = {}
    try:
        check_pass(rig, token, operation='启动冻结后台求权')
        session = _sessions[token.rig_pointer]
        grants = session['worker_grants']
        collection.need(isinstance(nonce, str), '缺少本会话后台grant')
        grant = grants.get(nonce)

        def verify():
            check_pass(rig, token, operation='启动冻结后台求权')
            collection.need(grant is not None and grants.get(nonce) is grant
                and grant['token'] is token and time.monotonic() <= grant['expires']
                and grant['proof_sha256'] == collection.digest(proof)
                and grant['request_sha256'] == collection.digest({k: v for k, v in manifest.items() if k != 'quality_gate'}),
                '后台grant已消费、过期、更换或请求已改变')
            _manifest_bones(rig, manifest)

        verify()
        collection.need('stdin' not in kwargs and 'bufsize' not in kwargs
                        and kwargs.get('shell', False) is False, '后台启动必须使用自有匿名stdin且不能经过shell')
        collection.need(isinstance(args, (list, tuple)) and args, '后台启动参数缺失')
        process = subprocess.Popen(args, stdin=subprocess.PIPE, bufsize=0, **kwargs)
        verify()
        # At-most-once even if the pipe write is partial or the child exits.
        grants.pop(nonce)
        worker_channel.send(process, worker_channel.packet(grant['key'], proof, process.pid))
        return process
    except Exception:
        if isinstance(nonce, str):
            grants.pop(nonce, None)
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5.)
            except Exception:
                # Only this task-owned Popen handle is eligible for a stronger stop.
                try:
                    process.kill(); process.wait(timeout=5.)
                except Exception:
                    pass
            finally:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except Exception:
                    pass
            if process.poll() is None:
                raise GateError('骨架质量门：grant已撤销，但自有子进程未确认退出；PID=' + str(process.pid)) from None
        raise GateError('骨架质量门：后台许可发放失败，已撤销该grant并收尾自有子进程') from None


def require_interactive_paint(rig):
    require_pass(rig, operation='原生刷权')
    raise GateError('骨架质量门：当前版本未实现逐笔前置失效保护，原生刷权保持停用')


def reject_legacy_capture():
    raise GateError('骨架质量门：旧生成后立即清组流程停用；先独立生成并验收骨架')


def _clear_sessions(*_args):
    collection.clear_receipt_cache()
    for pointer in tuple(_sessions):
        _revoke_pointer(pointer)


def _install_lifecycle():
    for name in ('load_pre', 'undo_pre', 'redo_pre'):
        handlers = getattr(bpy.app.handlers, name)
        if _clear_sessions not in handlers:
            handlers.append(_clear_sessions)


def close():
    _clear_sessions()
    for name in ('load_pre', 'undo_pre', 'redo_pre'):
        handlers = getattr(bpy.app.handlers, name)
        if _clear_sessions in handlers:
            handlers.remove(_clear_sessions)
