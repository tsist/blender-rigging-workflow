# SPDX-License-Identifier: GPL-3.0-or-later
"""Complete scoped evidence receipts and frozen worker proofs; no Blender IO."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
import os
import sys
from pathlib import Path

EVIDENCE_SCHEMA = 'rigging.skeleton-evidence-set.v2'
RECEIPT_SCHEMA = 'rigging.skeleton-quality-receipt.v2'
CHECKS = ('anatomical_placement', 'depth_and_views', 'chains_and_axes', 'body_connections',
          'isolated_fk', 'ik_and_support', 'intermediate_and_extension', 'bilateral', 'neutral_restore')


class GateError(ValueError):
    """Evidence is absent, incomplete, unapproved or no longer current."""


def need(condition, message):
    if not condition:
        raise GateError('骨架质量门：' + message)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def descriptor(path):
    p = Path(path)
    need(p.is_absolute() and p.is_file(), '证据必须是存在的绝对本地文件')
    with p.open('rb') as stream:
        digest_hex = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': str(p), 'sha256': digest_hex}


def _file_sha256(path):
    p = Path(path)
    need(p.is_absolute() and p.is_file(), '证据必须是存在的绝对本地文件')
    with p.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_file(row):
    need(isinstance(row, dict) and set(row) >= {'path', 'sha256'}, '文件描述符缺失')
    need(isinstance(row['sha256'], str) and len(row['sha256']) == 64, '文件内容身份无效')
    actual = descriptor(row['path'])
    need(actual['sha256'] == row['sha256'], '证据文件缺失或已改变：' + str(row['path']))
    return actual


def verify_files_batch(rows, *, preverified=None):
    """Verify every unique file byte-by-byte in parallel; never trust mtime/size."""
    verified = dict(preverified) if preverified else {}
    pending = {}
    for row in rows:
        need(isinstance(row, dict) and set(row) >= {'path', 'sha256'}, '文件描述符缺失')
        expected = row['sha256']
        need(isinstance(expected, str) and len(expected) == 64, '文件内容身份无效')
        p = Path(row['path'])
        need(p.is_absolute() and p.is_file(), '证据必须是存在的绝对本地文件')
        resolved = str(p.resolve())
        key = resolved.casefold()
        if key in verified:
            need(verified[key] == expected, '证据文件缺失或已改变：' + str(row['path']))
            continue
        if key in pending:
            need(pending[key][1] == expected, '依赖闭包存在冲突SHA：' + key)
            continue
        pending[key] = (resolved, expected, str(row['path']))
    if not pending:
        return verified
    items = list(pending.items())
    if len(items) == 1:
        key, (resolved, expected, original) = items[0]
        actual = _file_sha256(resolved)
        need(actual == expected, '证据文件缺失或已改变：' + original)
        verified[key] = actual
        return verified
    workers = min(8, os.cpu_count() or 4, len(items))
    def check_item(item):
        key, (resolved, expected, original) = item
        actual = _file_sha256(resolved)
        return key, actual, expected, original
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for key, actual, expected, original in executor.map(check_item, items):
            need(actual == expected, '证据文件缺失或已改变：' + original)
            verified[key] = actual
    return verified


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, TypeError) as exc:
        raise GateError('骨架质量门：无法读取有效回执或证据') from exc


def read_verified(row):
    """Hash and parse the same buffer rather than reopen a checked JSON file."""
    need(isinstance(row, dict) and set(row) >= {'path', 'sha256'}, '原生文件描述符缺失')
    p = Path(row['path'])
    need(p.is_absolute() and p.is_file(), '原生文件不存在')
    raw = p.read_bytes()
    need(hashlib.sha256(raw).hexdigest() == row['sha256'], '原生内容已改变：' + str(p))
    try:
        return json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise GateError('骨架质量门：原生JSON无效') from exc


def unique_strings(values, name, *, nonempty=True):
    need(isinstance(values, list) and (bool(values) or not nonempty), name + '列表缺失')
    need(all(isinstance(s, str) and s.strip() for s in values)
         and len(set(values)) == len(values), name + '为空或重复')
    return set(values)


def validate_evidence(doc, *, _verified=None):
    """Validate coverage and every listed byte, never determine art quality."""
    need(isinstance(doc, dict) and doc.get('schema') == EVIDENCE_SCHEMA
         and doc.get('status') == 'PENDING_REVIEW', '需要完整v2待审集合，不能使用旧分包或手改PASS')
    need(isinstance(doc.get('identity_sha256'), str) and len(doc['identity_sha256']) == 64,
         '骨架内容身份缺失')
    scope = doc.get('scope', {})
    need(isinstance(scope, dict) and all(isinstance(scope.get(k), str) and scope[k].strip()
         for k in ('subject', 'purpose', 'motion_scope')), '角色、用途或动作范围不明确')
    need(scope.get('eligible_for_skinning') is True, '当前审查范围尚不支持蒙皮许可')
    need(scope.get('required_checks') == list(CHECKS), '必要质量检查不得删项')
    allowed = unique_strings(scope.get('allowed_operations'), '获准操作')
    if 'agent_write_scopes' in scope:
        scoped_operations = scope['agent_write_scopes']
        need(isinstance(scoped_operations, dict) and bool(scoped_operations)
             and set(scoped_operations) <= allowed, '精确写入范围须对应获准操作')
        for values in scoped_operations.values():
            digests = unique_strings(values, '精确写入范围摘要')
            need(all(len(value) == 64 and all(c in '0123456789abcdef' for c in value)
                     for value in digests), '写入范围摘要无效')
    cases = unique_strings(scope.get('required_cases'), '必要职责')
    required_bones = unique_strings(scope.get('required_bones'), '必要骨骼')
    required_controls = unique_strings(scope.get('required_controls'), '必要原生控制')
    need(isinstance(scope.get('exclusions'), list) and isinstance(scope.get('limitations'), list),
         '必须明示排除项及未验边界')
    plan = doc.get('plan')
    parts = doc.get('parts')
    need(isinstance(plan, list) and plan and isinstance(parts, list) and parts, '完整分包计划缺失')
    planned = unique_strings([p.get('id') for p in plan], '计划分包')
    need(unique_strings([p.get('id') for p in parts], '实际分包') == planned, '分包缺失、重复或多出')
    expected = {p['id']: p for p in plan}
    artifacts = {}; coverage = set(); captures = set(); source_cache = {}
    verified = {} if _verified is None else _verified
    batch_files = []
    bindings = doc.get('case_bindings', [])
    binding_remaining = {}
    if isinstance(bindings, list):
        for binding in bindings:
            cap = binding.get('capture') if isinstance(binding, dict) else None
            if isinstance(cap, dict) and isinstance(cap.get('path'), str) and isinstance(cap.get('sha256'), str) and Path(cap['path']).is_absolute():
                ck = (str(Path(cap['path']).resolve()).casefold(), cap['sha256'])
                binding_remaining[ck] = binding_remaining.get(ck, 0) + 1
    validated_json_sources = set()

    def source_json(row, *, retain=True):
        need(isinstance(row, dict) and set(row) >= {'path', 'sha256'}, '原生文件描述符缺失')
        resolved_key = str(Path(row['path']).resolve()).casefold() if Path(row['path']).is_absolute() else row['path']
        key = (resolved_key, row['sha256'])
        if key in source_cache:
            return source_cache[key]
        if not retain and key in validated_json_sources:
            return None
        parsed = read_verified(row)
        verified[str(Path(row['path']).resolve()).casefold()] = row['sha256']
        validated_json_sources.add(key)
        if retain or binding_remaining.get(key, 0) > 0:
            source_cache[key] = parsed
            return parsed
        return None
    for part in parts:
        p = expected[part['id']]
        declared = unique_strings(p.get('case_ids'), '计划职责', nonempty=False)
        actual = unique_strings(part.get('case_ids'), '实际职责', nonempty=False)
        need(actual == declared and actual <= cases, '分包职责与已冻结计划不符')
        coverage |= actual
        rows = part.get('artifacts', [])
        ids = unique_strings([r.get('id') for r in rows], '分包图像')
        need(ids == unique_strings(p.get('artifact_ids'), '计划图像'), '图像缺失或替换')
        for row in rows:
            need(row['id'] not in artifacts, '图像ID跨分包重复')
            need(row.get('kind') in {'rest_grid', 'rest_reference_grid', 'motion_grid', 'bone_detail_grid'},
                 '未知图像职责')
            batch_files.append(row)
            sources = row.get('native_sources')
            need(isinstance(sources, list) and sources, '缺少图像对应的原生采集/渲染来源')
            for source in sources:
                source_json(source, retain=False); captures.add(source['sha256'])
            artifacts[row['id']] = row
    need(coverage == cases, '必要职责没有完整覆盖')
    need(all(any(a['kind'] == k for a in artifacts.values())
         for k in ('rest_grid', 'rest_reference_grid', 'motion_grid')), '缺少静置、静态叠图或运动图')
    dependencies = doc.get('dependencies')
    need(isinstance(dependencies, list) and dependencies, '缺少输入/原生动作/控制源码的来源绑定')
    batch_files.extend(dependencies)
    need(unique_strings([b.get('case_id') for b in bindings], '原生职责绑定') == cases,
         '职责原生绑定缺失、重复或多出')
    for binding in bindings:
        native = source_json(binding['capture'], retain=True)
        cap_key = (str(Path(binding['capture']['path']).resolve()).casefold(), binding['capture']['sha256'])
        rows = [c for c in native.get('cases', []) if c.get('id') == binding['source_case_id']]
        need(len(rows) == 1, '职责不在真实采集报告中')
        state_ids = ['neutral'] + rows[0]['samples']
        need(binding.get('state_ids') == state_ids, '职责关键/中间态被删改')
        states = {s['id']: s for s in native.get('states', [])}
        need(len(states) == len(native.get('states', [])), '原生状态ID重复')
        need(all(s in states for s in state_ids)
             and binding.get('snapshot_sha256') == {s: digest(states[s]) for s in state_ids},
             '职责与真实原生状态内容不符')
        for sid in state_ids:
            state = states[sid]
            need(unique_strings([b.get('name') for b in state.get('bones', [])], '原生骨快照') == required_bones,
                 '原生状态缺骨或骨集合改变')
            need(set(state.get('inputs', {})) == required_controls, '原生输入控制缺失')
            for bone in state['bones']:
                matrix = bone.get('matrix_world', [])
                vectors = [bone.get('head_world', []), bone.get('tail_world', [])]
                need(len(matrix) == 4 and all(isinstance(row, list) and len(row) == 4 for row in matrix)
                     and all(len(v) == 3 for v in vectors), '原生骨矩阵/端点格式不完整')
                values = [v for row in matrix for v in row] + [v for row in vectors for v in row]
                need(all(isinstance(v, (int, float)) and math.isfinite(v) for v in values), '原生骨数据非有限')
        ids = unique_strings(binding.get('artifact_ids'), '职责图像绑定')
        need(ids <= set(artifacts) and all(binding['capture']['sha256'] in
             {s['sha256'] for s in artifacts[i]['native_sources']} for i in ids),
             '职责图像与原生采集来源不符')
        if cap_key in binding_remaining:
            binding_remaining[cap_key] -= 1
            if binding_remaining[cap_key] <= 0:
                source_cache.pop(cap_key, None)
    verified.update(verify_files_batch(batch_files, preverified=verified))
    return {'identity_sha256': doc['identity_sha256'], 'scope': scope,
            'scope_sha256': digest(scope), 'artifact_ids': sorted(artifacts),
            'part_count': len(parts), 'case_count': len(cases), 'capture_count': len(captures)}


def validate_review(review, artifact_ids, scope_sha256, evidence_sha256, *, _verified=None):
    need(isinstance(review, dict) and isinstance(review.get('reviewer'), str)
         and bool(review['reviewer'].strip()) and isinstance(review.get('reason'), str)
         and bool(review['reason'].strip()), '缺少实际审核者及依据')
    need(review.get('verdict') == 'PASS_WITHIN_SCOPE' and review.get('scope_sha256') == scope_sha256,
         '审核没有通过本次明确范围')
    need(review.get('evidence_sha256') == evidence_sha256, '审核没有绑定当前完整证据内容')
    need(set(review.get('checks', {})) == set(CHECKS)
         and all(review['checks'][k] == 'pass' for k in CHECKS), '存在失败、未验或缺少的必要项')
    need(unique_strings(review.get('evidence_seen'), '已看证据') == set(artifact_ids),
         '审核未实际覆盖完整图像集合')
    need(isinstance(review.get('sources'), list) and review['sources'], '缺少可回读的实际审核记录')
    checked = verify_files_batch(review['sources'], preverified=_verified)
    if _verified is not None:
        _verified.update(checked)


def record_review(evidence_path, main_review, independent_review, outfile):
    """Explicit human/agent attestation; no automatic construction of PASS checks."""
    doc = read(evidence_path); facts = validate_evidence(doc)
    for review in (main_review, independent_review):
        validate_review(review, facts['artifact_ids'], facts['scope_sha256'], digest(doc))
    need(main_review['reviewer'] != independent_review['reviewer'], '主审与独审不能同人')
    receipt = {'schema': RECEIPT_SCHEMA, 'status': 'PASS_WITHIN_SCOPE',
               'identity_sha256': facts['identity_sha256'], 'scope_sha256': facts['scope_sha256'],
               'evidence': descriptor(evidence_path), 'main_review': main_review,
               'independent_review': independent_review}
    p = Path(outfile); need(p.is_absolute(), '回执需要绝对路径')
    with p.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return receipt


# One immutable payload; parsed native captures never survive in this cache.
_RECEIPT_CACHE_LIMIT_BYTES = 4 * 1024 * 1024
_RECEIPT_CACHE = None
_RECEIPT_CACHE_EPOCH = 0
_RECEIPT_RULE_SOURCES = tuple((str(p.resolve()), hashlib.sha256(p.read_bytes()).hexdigest())
    for p in (Path(__file__), Path(__file__).with_name('skeleton_gate.py')) if p.is_file())


def clear_receipt_cache():
    global _RECEIPT_CACHE, _RECEIPT_CACHE_EPOCH
    _RECEIPT_CACHE = None
    _RECEIPT_CACHE_EPOCH += 1


def _receipt_buffer(row, *, allow_unpinned=False):
    need(isinstance(row, dict) and set(row) >= {'path', 'sha256'}, '文件描述符缺失')
    expected = row['sha256']
    need((allow_unpinned and expected is None) or
         (isinstance(expected, str) and len(expected) == 64), '文件内容身份无效')
    path = Path(row['path'])
    need((allow_unpinned or path.is_absolute()) and path.is_file(), '证据必须是存在的本地文件')
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    need(expected is None or actual == expected, '证据文件缺失或已改变：' + str(path))
    return raw, actual


def _receipt_json(raw):
    try:
        return json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise GateError('骨架质量门：原生JSON无效') from exc


def _receipt_rules():
    for path, expected in _RECEIPT_RULE_SOURCES:
        need(hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected,
             '静态验证或生命周期源码已改变，请重新加载模块')
    return digest([_RECEIPT_RULE_SOURCES, EVIDENCE_SCHEMA, RECEIPT_SCHEMA, CHECKS])


def _receipt_closure(path, receipt_sha, receipt, doc, *, _verified=None):
    # Complete file-read closure of the existing three validation functions.
    rows = [{'path': str(Path(path).resolve()), 'sha256': receipt_sha}, receipt['evidence']]
    rows += list(doc['dependencies'])
    for part in doc['parts']:
        for artifact in part['artifacts']:
            rows.append(artifact)
            rows += artifact['native_sources']
    rows += [binding['capture'] for binding in doc['case_bindings']]
    rows += [row for name in ('main_review', 'independent_review') for row in receipt[name]['sources']]
    closure = {}
    def add(row):
        name = str(Path(row['path']).resolve()).casefold()
        expected = row['sha256']
        need(name not in closure or closure[name]['sha256'] == expected, '依赖闭包存在冲突SHA：' + name)
        is_new = name not in closure
        closure[name] = {'path': str(Path(row['path']).resolve()), 'sha256': expected}
        return is_new
    for row in rows:
        add(row)
    extra_rb = []
    for row in doc.get('runtime_binding', {}).get('dependencies', []):
        if add(row):
            extra_rb.append(row)
    if extra_rb:
        checked = verify_files_batch(extra_rb, preverified=_verified)
        if _verified is not None:
            _verified.update(checked)
    for path, expected in _RECEIPT_RULE_SOURCES:
        add({'path': path, 'sha256': expected})
    return list(closure.values())


def _receipt_cache_store(key, receipt, doc, facts, closure, epoch):
    global _RECEIPT_CACHE
    payload = json.dumps({'receipt': receipt, 'doc': doc, 'facts': facts, 'closure': closure},
                         ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    entry = (key, payload)
    retained = sys.getsizeof(entry) + sys.getsizeof(key) + sys.getsizeof(payload)
    if epoch == _RECEIPT_CACHE_EPOCH and retained <= _RECEIPT_CACHE_LIMIT_BYTES:
        _RECEIPT_CACHE = entry


def _load_receipt_detailed(path, *, expected_sha256=None, preverified_closure=None):
    global _RECEIPT_CACHE
    try:
        rules = _receipt_rules()
        raw, receipt_sha = _receipt_buffer({'path': str(path), 'sha256': expected_sha256},
                                           allow_unpinned=expected_sha256 is None)
        receipt = _receipt_json(raw)
        need(isinstance(receipt, dict) and receipt.get('schema') == RECEIPT_SCHEMA
             and receipt.get('status') == 'PASS_WITHIN_SCOPE', '缺少完整通过回执，禁止任何蒙皮')
        evidence_raw, evidence_sha = _receipt_buffer(receipt.get('evidence'))
        key = digest([str(Path(path).resolve()).casefold(), receipt_sha, evidence_sha, rules])
        known = {str(Path(path).resolve()).casefold(): receipt_sha,
                 str(Path(receipt['evidence']['path']).resolve()).casefold(): evidence_sha}
        known.update({str(Path(p).resolve()).casefold(): s for p, s in _RECEIPT_RULE_SOURCES})
        if _RECEIPT_CACHE is not None and _RECEIPT_CACHE[0] == key:
            cached = _receipt_json(_RECEIPT_CACHE[1])
            if preverified_closure is not None:
                # Synchronous nested check inside an active _scoped_operation whose entry
                # just verified every byte of the closure from disk; still verify receipt,
                # evidence, and rule sources freshly above and fail closed on any missing key.
                merged = {**preverified_closure, **known}
                for row in cached['closure']:
                    name = str(Path(row['path']).resolve()).casefold()
                    need(name in merged and merged[name] == row['sha256'], '依赖闭包存在冲突SHA：' + name)
                verified = merged
            else:
                verified = verify_files_batch(cached['closure'], preverified=known)
            return copy.deepcopy(cached['receipt']), copy.deepcopy(cached['doc']), copy.deepcopy(cached['facts']), verified
        clear_receipt_cache()
        epoch = _RECEIPT_CACHE_EPOCH
        doc = _receipt_json(evidence_raw)
        verified = dict(known)
        facts = validate_evidence(doc, _verified=verified)
        need(receipt.get('identity_sha256') == facts['identity_sha256']
             and receipt.get('scope_sha256') == facts['scope_sha256'], '回执身份或用途范围不符')
        doc_digest = digest(doc)
        for name in ('main_review', 'independent_review'):
            validate_review(receipt.get(name), facts['artifact_ids'], facts['scope_sha256'], doc_digest, _verified=verified)
        need(receipt['main_review']['reviewer'] != receipt['independent_review']['reviewer'], '独审身份无效')
        closure = _receipt_closure(path, receipt_sha, receipt, doc, _verified=verified)
        _receipt_cache_store(key, receipt, doc, facts, closure, epoch)
        return copy.deepcopy(receipt), copy.deepcopy(doc), copy.deepcopy(facts), verified
    except Exception:
        clear_receipt_cache()
        raise


def load_receipt(path, *, expected_sha256=None):
    receipt, doc, facts, _ = _load_receipt_detailed(path, expected_sha256=expected_sha256)
    return receipt, doc, facts


def make_worker_proof(receipt_path, request):
    """Legacy format fixture only. It can never issue a production IPC proof."""
    receipt, doc, facts = load_receipt(receipt_path)
    need(facts['scope'].get('test_only') is True, '纯文件凭据只允许隔离工具fixture')
    need('quality_gate' not in request, '不能重用或覆盖旧后台证明')
    return {'schema': 'rigging.skeleton-worker-proof.v1', 'receipt': descriptor(receipt_path),
            'identity_sha256': facts['identity_sha256'], 'scope_sha256': facts['scope_sha256'],
            'request_sha256': digest(request)}


def require_worker(request, channel=None, *, validation_only=False):
    """Authenticate owned-process IPC before any array load or solver import."""
    need(isinstance(request, dict), '后台请求格式无效')
    proof = request.get('quality_gate')
    need(isinstance(proof, dict), '后台请求缺少证明')
    body = {k: v for k, v in request.items() if k != 'quality_gate'}
    request_sha256 = digest(body)
    need(proof.get('request_sha256') == request_sha256, '后台请求与已发放许可不符')
    metadata = {}
    if validation_only is True:
        need(proof.get('schema') == 'rigging.skeleton-worker-proof.v1', '格式fixture只接受旧v1证明')
    else:
        need(proof.get('schema') == 'rigging.skeleton-worker-proof.v2',
             '真实后台求权必须由live父会话经匿名管道发放')
        need(isinstance(proof.get('nonce'), str) and len(proof['nonce']) == 64,
             '后台一次性许可缺失')
        from . import worker_channel
        try:
            metadata = worker_channel.authenticate(proof, channel)
        except Exception:
            raise GateError('骨架质量门：后台匿名通道认证失败') from None
    need(isinstance(proof.get('receipt'), dict), '后台回执来源缺失')
    receipt, doc, facts = load_receipt(proof['receipt']['path'], expected_sha256=proof['receipt']['sha256'])
    need((facts['scope'].get('test_only') is True) if validation_only is True
         else not facts['scope'].get('test_only'), '工具fixture与真实求权必须隔离')
    need('worker.solve' in facts['scope']['allowed_operations'] or validation_only is True,
         '回执范围不允许后台求权')
    need(proof.get('identity_sha256') == facts['identity_sha256']
         and proof.get('scope_sha256') == facts['scope_sha256'], '后台许可身份不符')
    return {**facts, **metadata, 'request_sha256': request_sha256,
            'validation_only': validation_only is True}


def read_frozen_npz(root, request):
    """Hash the exact bytes that the consumer must load with BytesIO."""
    rows = request.get('files')
    need(isinstance(rows, list), '冻结文件清单缺失')
    matches = [r for r in rows if isinstance(r, dict) and r.get('relative_path') == 'input.npz']
    need(len(matches) == 1, '需要唯一明确input.npz冻结项')
    # Reject aliases too: a second spelling must not defeat uniqueness checks.
    root = Path(root).resolve()
    target = root / 'input.npz'
    need(all(isinstance(r, dict) and isinstance(r.get('relative_path'), str)
             and not Path(r['relative_path']).is_absolute()
             and (root / r['relative_path']).resolve().is_relative_to(root) for r in rows),
         '冻结文件路径必须位于本次作业内')
    need(sum((root / r['relative_path']).resolve() == target.resolve() for r in rows) == 1,
         'input.npz存在别名或重复项')
    need(target.resolve().is_relative_to(root), '冻结数组不能指向作业外')
    raw = target.read_bytes()
    need(hashlib.sha256(raw).hexdigest() == matches[0].get('sha256'), '实际加载的冻结数组内容已改变')
    return raw
