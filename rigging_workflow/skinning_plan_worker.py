# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen multi-domain arrays to complete weights; no source scene is loaded."""
import hashlib
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
    def cancelled():
        return (root/'cancel.request').exists()
    def progress(phase, **details):
        with (root/'progress.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps({'phase': phase, **details}, ensure_ascii=False, allow_nan=False)+'\n')
    try:
        for item in request['files']:
            if hashlib.sha256((root/item['relative_path']).read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('冻结的多域数组或求解代码已改变')
        sys.path.insert(0, str(root/'runtime'))
        from rigging_workflow.volume import solve
        from rigging_workflow.volume.contract import VolumeError
        from rigging_workflow.skinning_fields import combine_partitions, transfer_surface_field
        def cancellation():
            if cancelled():
                raise VolumeError('CANCELLED', 'Multi-domain solve cancelled; no candidate accepted')
        with np.load(root/'input.npz', allow_pickle=False) as archive:
            arrays = {key: archive[key].copy() for key in archive.files}
        partitions, statistics, warnings = [], [], []
        for index, row in enumerate(request['domains']):
            cancellation()
            prefix = 'd'+str(index)
            ids = arrays[prefix+'_source_ids']
            def domain_progress(phase, **details):
                progress(phase, domain=row['name'], domain_index=index, domain_total=len(request['domains']), **details)
            solved = solve([{'id': 'proxy', 'vertices': arrays[prefix+'_vertices'], 'triangles': arrays[prefix+'_triangles']}],
                row['bones'], row['settings'], cancel_check=cancelled, progress=domain_progress,
                sample_points={'proxy': arrays['surface_vertices'][ids]})
            partitions.append({'source_ids': ids, 'bone_names': row['bone_names'],
                'weights': solved['mesh_weights']['proxy'], 'blend': arrays[prefix+'_blend']})
            statistics.append({'name': row['name'], 'statistics': solved['statistics']})
            warnings.extend({'domain': row['name'], **warning} for warning in solved['warnings'])
            with (root/('domain-'+str(index)+'.npz')).open('wb') as stream:
                np.savez_compressed(stream, weights=solved['mesh_weights']['proxy'], source_ids=ids,
                                    bone_names=np.asarray(row['bone_names']))
        cancellation()
        names = request['bone_names']
        combined = combine_partitions(len(arrays['surface_vertices']), names, partitions)
        triangles = arrays['surface_triangles'][arrays['valid_triangles']]
        volume_ids = arrays['volume_ids']
        weights = np.zeros((len(arrays['raw_vertices']), len(names)), dtype=np.float32)
        sampled, transfer = transfer_surface_field(arrays['raw_vertices'][volume_ids], arrays['surface_vertices'], triangles,
            combined['weights'], max_distance=request['summary']['transfer_distance_meters'],
            cancel_check=cancelled, progress=progress)
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
            'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'warnings': warnings,
            'statistics': {'domains': statistics, 'plan': request['summary'], 'transfer': transfer,
                'weight_shape': list(weights.shape), 'row_sum_max_error': float(abs(weights.sum(axis=1)-1).max(initial=0))}})
    except Exception as exc:
        (root/'traceback.txt').write_text(traceback.format_exc(), encoding='utf8')
        code = getattr(exc, 'code', 'WORKER_ERROR')
        write_json(root/'result.json', {'status': 'cancelled' if cancelled() or code == 'CANCELLED' else 'failed',
            'error': {'code': code, 'message': str(exc)}, 'statistics': getattr(exc, 'statistics', None)})


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    run(sys.argv[sys.argv.index('--')+1])
