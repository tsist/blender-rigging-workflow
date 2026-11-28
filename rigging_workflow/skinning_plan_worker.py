# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen multi-domain arrays to complete weights; no source scene is loaded."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import traceback

import numpy as np


def write_json(path, value):
    temporary = path.with_suffix('.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf8')
    os.replace(temporary, path)


def run(directory):
    root = Path(directory)
    request = json.loads((root/'input.json').read_text(encoding='utf8'))
    authorization = None
    def cancelled():
        return (root/'cancel.request').exists()
    def progress(phase, **details):
        with (root/'progress.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps({'phase': phase, **details}, ensure_ascii=False, allow_nan=False)+'\n')
    try:
        sys.path.insert(0, str(root/'runtime'))
        from rigging_workflow.skeleton_collection import require_worker, read_frozen_npz
        facts = require_worker(request)
        authorization = {k: facts[k] for k in ('channel_schema', 'parent_pid', 'child_pid', 'request_sha256')}
        input_bytes = read_frozen_npz(root, request)
        for item in request['files']:
            if item['relative_path'] == 'input.npz':
                continue
            if hashlib.sha256((root/item['relative_path']).read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('冻结的多域数组或求解代码已改变')
        from rigging_workflow.volume import solve
        from rigging_workflow.volume.contract import VolumeError
        from rigging_workflow.volume.domain_cache import CompletedDomains, identity
        from rigging_workflow.skinning_fields import combine_partitions, transfer_surface_field
        def cancellation():
            if cancelled():
                raise VolumeError('CANCELLED', 'Multi-domain solve cancelled; no candidate accepted')
        with np.load(BytesIO(input_bytes), allow_pickle=False) as archive:
            arrays = {key: archive[key].copy() for key in archive.files}
        cache = CompletedDomains(root.parent)
        fingerprint = sorted((item['relative_path'].replace('\\', '/'), item['sha256']) for item in request['files']
                             if '/volume/' in item['relative_path'].replace('\\', '/'))
        partitions, statistics, warnings, failures = [], [], [], []
        for index, row in enumerate(request['domains']):
            cancellation()
            prefix = 'd'+str(index)
            ids = arrays[prefix+'_source_ids']
            vertices, triangles = arrays[prefix+'_vertices'], arrays[prefix+'_triangles']
            points = arrays['surface_vertices'][ids]
            key = identity(fingerprint, row['bones'], row['settings'], vertices, triangles, points, ids)
            def domain_progress(phase, **details):
                progress(phase, domain=row['name'], domain_index=index, domain_total=len(request['domains']), **details)
            cached = cache.load(key, ids, row['bone_names'])
            if cached is None:
                try:
                    solved = solve([{'id': 'proxy', 'vertices': vertices, 'triangles': triangles}],
                        row['bones'], row['settings'], cancel_check=cancelled, progress=domain_progress,
                        sample_points={'proxy': points})
                except VolumeError as exc:
                    if cancelled() or exc.code == 'CANCELLED':
                        raise
                    failures.append({'domain': row['name'], 'code': exc.code, 'message': str(exc),
                                     'statistics': exc.statistics})
                    domain_progress('domain_failed', code=exc.code)
                    continue
                domain_weights = solved['mesh_weights']['proxy']
                domain_stats, domain_warnings = solved['statistics'], solved['warnings']
                cancellation()
                try:
                    digest = cache.store(key, ids, row['bone_names'], domain_weights, domain_stats, domain_warnings)
                except OSError as exc:
                    digest = None
                    domain_warnings = [*domain_warnings, {'code': 'CACHE_WRITE_SKIPPED', 'message': str(exc)}]
            else:
                domain_weights, domain_stats, domain_warnings, digest = cached
                domain_progress('domain_reused', fingerprint=key)
            partitions.append({'source_ids': ids, 'bone_names': row['bone_names'],
                'weights': domain_weights, 'blend': arrays[prefix+'_blend']})
            statistics.append({'name': row['name'], 'statistics': domain_stats,
                               'resumption': {'reused': cached is not None, 'domain_fingerprint': key, 'field_sha256': digest}})
            warnings.extend({'domain': row['name'], **warning} for warning in domain_warnings)
            with (root/('domain-'+str(index)+'.npz')).open('wb') as stream:
                np.savez_compressed(stream, weights=domain_weights, source_ids=ids,
                                    bone_names=np.asarray(row['bone_names']))
        cancellation()
        if failures:
            write_json(root/'result.json', {'status': 'failed', 'phase': 'domain_failures', 'authorization': authorization,
                       'error': {'code': 'INCOMPLETE_DOMAINS', 'message': '部分分区未求出权重，已完成分区可续算'},
                       'statistics': {'domains': statistics, 'failed_domains': failures, 'plan': request['summary']},
                       'warnings': warnings})
            return
        names = request['bone_names']
        combined = combine_partitions(len(arrays['surface_vertices']), names, partitions)
        triangles = arrays['surface_triangles'][arrays['valid_triangles']]
        volume_ids = arrays['volume_ids']
        weights = np.zeros((len(arrays['raw_vertices']), len(names)), dtype=np.float32)
        sampled, transfer = transfer_surface_field(arrays['raw_vertices'][volume_ids], arrays['surface_vertices'], triangles,
            combined['weights'], max_distance=request['summary']['transfer_distance_meters'],
            cancel_check=cancelled, progress=progress, components=(arrays['raw_shells'][volume_ids], arrays['surface_shells']))
        weights[volume_ids] = sampled
        for index, row in enumerate(request['rigid']):
            weights[arrays['r'+str(index)+'_ids'], names.index(row['bone_name'])] = 1
        cancellation()
        if not np.isfinite(weights).all() or (weights < 0).any() or np.max(abs(weights.sum(axis=1)-1), initial=0) > 1e-5:
            raise ValueError('完整原网格仍有未分配或无效权重；不创建部分候选')
        path = root/'weights.npz'
        with (root/'weights.part').open('wb') as stream:
            np.savez_compressed(stream, weights=weights)
        os.replace(root/'weights.part', path)
        progress('complete', domain_total=len(request['domains']), done=len(weights), total=len(weights))
        write_json(root/'result.json', {'status': 'succeeded', 'phase': 'complete', 'bone_names': names,
            'authorization': authorization,
            'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'warnings': warnings,
            'statistics': {'domains': statistics, 'plan': request['summary'], 'transfer': transfer,
                'weight_shape': list(weights.shape), 'authenticated_npz_buffer_bytes': len(input_bytes),
                'row_sum_max_error': float(abs(weights.sum(axis=1)-1).max(initial=0))}})
    except Exception as exc:
        (root/'traceback.txt').write_text(traceback.format_exc(), encoding='utf8')
        code = getattr(exc, 'code', 'WORKER_ERROR')
        write_json(root/'result.json', {'status': 'cancelled' if cancelled() or code == 'CANCELLED' else 'failed',
            'authorization': authorization,
            'error': {'code': code, 'message': str(exc)}, 'statistics': getattr(exc, 'statistics', None)})


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    run(sys.argv[sys.argv.index('--')+1])
