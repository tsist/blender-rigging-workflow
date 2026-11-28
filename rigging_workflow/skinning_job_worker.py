# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the shared volume solver on a frozen array/code snapshot in own Blender."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import traceback

import numpy as np


def process_memory():
    """OS-observed peak for this worker lifetime, separate from array estimates."""
    if os.name != 'nt':
        return {'status': 'not_measured', 'scope': 'solver_worker_process_lifetime'}
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
                'PagefileUsage', 'PeakPagefileUsage')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return {'status': 'not_measured', 'scope': 'solver_worker_process_lifetime',
                'windows_error': ctypes.get_last_error()}
    return {'status': 'measured', 'scope': 'solver_worker_process_lifetime',
            'peak_working_set_bytes': counters.PeakWorkingSetSize,
            'working_set_bytes': counters.WorkingSetSize,
            'peak_commit_bytes': counters.PeakPagefileUsage,
            'includes': 'Blender/Python startup, frozen arrays and solver; excludes parent UI process'}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf8')
    os.replace(temporary, path)


def run(directory):
    root = Path(directory)
    request = json.loads((root / 'input.json').read_text(encoding='utf8'))
    authorization = None
    output = root / 'result.json'
    def cancelled():
        return (root / 'cancel.request').exists()
    def progress(phase, **details):
        # One writer appends complete lines; readers retain an unfinished tail.
        # Replacing a file actively read by Python can fail sharing checks on Windows.
        with (root / 'progress.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps({'phase': phase, **details}, ensure_ascii=False, allow_nan=False) + '\n')
    try:
        sys.path.insert(0, str(root / 'runtime'))
        from rigging_workflow.skeleton_collection import require_worker, read_frozen_npz
        facts = require_worker(request)
        authorization = {k: facts[k] for k in ('channel_schema', 'parent_pid', 'child_pid', 'request_sha256')}
        input_bytes = read_frozen_npz(root, request)
        for item in request['files']:
            if item['relative_path'] == 'input.npz':
                continue
            path = root / item['relative_path']
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('输入数组或求解代码快照已改变')
        if cancelled():
            write_json(output, {'status': 'cancelled', 'phase': 'cancelled'})
            return
        from rigging_workflow.volume import solve
        with np.load(BytesIO(input_bytes), allow_pickle=False) as data:
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
        solved['statistics']['process_memory'] = process_memory()
        solved['statistics']['authenticated_npz_buffer_bytes'] = len(input_bytes)
        write_json(output, {'status': 'succeeded', 'phase': 'complete',
            'authorization': authorization,
            'bone_names': solved['bone_names'], 'statistics': solved['statistics'],
            'warnings': solved['warnings'], 'weights_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    except Exception as exc:
        (root / 'traceback.txt').write_text(traceback.format_exc(), encoding='utf8')
        code = getattr(exc, 'code', 'WORKER_ERROR')
        write_json(output, {'status': 'cancelled' if code == 'CANCELLED' else 'failed',
                           'authorization': authorization,
                           'phase': 'stopped', 'error': {'code': code, 'message': str(exc)},
                           'process_memory': process_memory(),
                           'statistics': getattr(exc, 'statistics', None)})


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    run(sys.argv[sys.argv.index('--') + 1])
