# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the shared volume solver on a frozen array/code snapshot in own Blender."""
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import numpy as np


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf8')
    os.replace(temporary, path)


def run(directory):
    root = Path(directory)
    request = json.loads((root / 'input.json').read_text(encoding='utf8'))
    output = root / 'result.json'
    def cancelled():
        return (root / 'cancel.request').exists()
    def progress(phase, **details):
        # One writer appends complete lines; readers retain an unfinished tail.
        # Replacing a file actively read by Python can fail sharing checks on Windows.
        with (root / 'progress.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps({'phase': phase, **details}, ensure_ascii=False, allow_nan=False) + '\n')
    try:
        for item in request['files']:
            path = root / item['relative_path']
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('输入数组或求解代码快照已改变')
        if cancelled():
            write_json(output, {'status': 'cancelled', 'phase': 'cancelled'})
            return
        sys.path.insert(0, str(root / 'runtime'))
        from rigging_workflow.volume import solve
        with np.load(root / 'input.npz', allow_pickle=False) as data:
            vertices, triangles, points = data['vertices'], data['triangles'], data['points']
        solved = solve([{'id': 'proxy', 'vertices': vertices, 'triangles': triangles}],
                       request['bones'], request['settings'], cancel_check=cancelled,
                       progress=progress, sample_points={'proxy': points})
        if cancelled():
            write_json(output, {'status': 'cancelled', 'phase': 'cancelled'})
            return
        path = root / 'weights.npz'
        with (root / 'weights.part').open('wb') as stream:
            np.savez_compressed(stream, weights=solved['mesh_weights']['proxy'])
        os.replace(root / 'weights.part', path)
        write_json(output, {'status': 'succeeded', 'phase': 'complete',
            'bone_names': solved['bone_names'], 'statistics': solved['statistics'],
            'warnings': solved['warnings'], 'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    except Exception as exc:
        (root / 'traceback.txt').write_text(traceback.format_exc(), encoding='utf8')
        code = getattr(exc, 'code', 'WORKER_ERROR')
        write_json(output, {'status': 'cancelled' if code == 'CANCELLED' else 'failed',
                           'phase': 'stopped', 'error': {'code': code, 'message': str(exc)},
                           'statistics': getattr(exc, 'statistics', None)})


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    run(sys.argv[sys.argv.index('--') + 1])
