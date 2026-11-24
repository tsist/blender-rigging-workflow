# SPDX-License-Identifier: GPL-3.0-or-later
"""Guarded local Linux transport for explicitly qualified v003 read-only modes."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys


PACKAGE = Path(__file__).resolve().parents[1]
MAX_REQUEST_BYTES = 4 * 1024**2
# Exact executable from the tested Linux x86_64 portable build.
BLENDER_LINUX_SHA256 = 'baaa36cb5d7a35092388e6d123739b0829012c252b0705bc2debec292e4ebffb'


def linux_mode(entry, mode):
    if sys.platform != 'linux':
        return False
    from rigging_candidate import require
    manifest = json.loads((PACKAGE / 'linux-capabilities.json').read_text(encoding='utf-8'))
    require(mode in manifest['routes'].get(entry, []),
            'UNSUPPORTED_HOST', f'Linux v003 has not qualified {entry} {mode}; use a supported read-only mode')
    return True


def _guard_types():
    # Select the vendored guard from this exact package, not a user add-on.
    sys.path.insert(0, str(PACKAGE))
    try:
        from rigging_workflow import linux_file_guard as guard
    finally:
        sys.path.pop(0)
    from rigging_candidate import require
    require(Path(guard.__file__).absolute().parent == PACKAGE / 'rigging_workflow',
            'PACKAGE_MISMATCH', 'Linux file guard came from another runtime')
    return guard.LinuxGuardMonitor, guard.LinuxFileGuard


def _canonical(raw, suffix=None):
    from rigging_candidate import require
    require(isinstance(raw, (str, Path)), 'INVALID_REQUEST', 'Expected an absolute local path')
    path = Path(raw)
    require(path.is_absolute() and path == path.resolve() and '..' not in path.parts,
            'INVALID_REQUEST', 'Linux requires a canonical absolute path without symlinks')
    require(suffix is None or path.suffix.lower() == suffix, 'INVALID_REQUEST', f'Expected {suffix} path')
    return path


def _read_request(guard):
    from rigging_candidate import require
    blocks, size = [], 0
    for chunk in guard.chunks():
        size += len(chunk)
        require(size <= MAX_REQUEST_BYTES, 'INVALID_REQUEST', 'Linux v003 request exceeds 4 MiB')
        blocks.append(chunk)
    return json.loads(b''.join(blocks).decode('utf-8-sig'))


def _source_path(request):
    from rigging_candidate import SHA256, require
    source = request.get('source') if isinstance(request, dict) else None
    require(isinstance(source, dict) and set(source) == {'path', 'sha256'} and
            isinstance(source['sha256'], str) and SHA256.fullmatch(source['sha256']),
            'INVALID_REQUEST', 'Source requires a path and lowercase SHA-256')
    return _canonical(source['path'], '.blend')


def _verify(guard, request):
    from rigging_candidate import require
    require(guard.sha256() == request['source']['sha256'], 'SOURCE_SHA_MISMATCH', 'Source SHA-256 does not match')


def require_self_contained(bpy):
    from rigging_candidate import require
    require(not bpy.data.libraries and not bpy.utils.blend_paths(packed=False),
            'EXTERNAL_DEPENDENCY', 'Linux read-only v003 expects a self-contained, local .blend')


@contextmanager
def worker_request(entry, mode, snapshot):
    """Guard the request and native Blender read in the actual worker process."""
    from rigging_candidate import read_json
    if not linux_mode(entry, mode):
        yield read_json(snapshot)
        return
    Monitor, Guard = _guard_types()
    with Monitor() as monitor, Guard(_canonical(snapshot, '.json'), monitor=monitor) as req:
        request = _read_request(req)
        with Guard(_source_path(request), monitor=monitor) as source:
            _verify(source, request)
            yield request
            _verify(source, request)


def run_readonly(entry, mode, request_path, run_dir, blender_path, script, validate, worker_tail=()):
    """Launch a private background Blender while holding input leases until it exits."""
    from rigging_candidate import require, read_json, write_json
    linux_mode(entry, mode)
    run = _canonical(run_dir)
    blender = _canonical(blender_path)
    require(run.parent.is_dir() and not run.exists(), 'INVALID_RUN_DIRECTORY',
            'Linux requires a new run directory under an existing canonical parent')
    require(os.access(blender, os.X_OK), 'INPUT_MISSING', 'Blender is not executable')
    Monitor, Guard = _guard_types()
    with Monitor() as monitor, Guard(_canonical(request_path, '.json'), monitor=monitor) as req:
        request = _read_request(req)
        validate(request)
        # Passing an executable *script* as Blender must not execute arbitrary code.
        with Guard(blender, monitor=monitor) as exe:
            require(exe.sha256() == BLENDER_LINUX_SHA256, 'UNSUPPORTED_HOST',
                    'Linux requires the SHA-256-pinned Blender 5.3.0 x86_64 executable')
            with Guard(_source_path(request), monitor=monitor) as source:
                _verify(source, request)
                run.mkdir(mode=0o700)
                snapshot, pending = run / 'request.json', run / 'worker-result.pending.json'
                write_json(snapshot, request)
                env = {'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
                for name, child in {'HOME':'home', 'BLENDER_USER_RESOURCES':'blender-profile',
                                    'XDG_CONFIG_HOME':'config', 'XDG_CACHE_HOME':'cache',
                                    'XDG_DATA_HOME':'data', 'XDG_RUNTIME_DIR':'runtime', 'TMPDIR':'tmp'}.items():
                    path = run / child
                    path.mkdir(mode=0o700)
                    env[name] = str(path)
                command = [str(blender), '--background', '--factory-startup', '--disable-autoexec',
                           '--offline-mode', '--threads', '2', '--python-exit-code', '1',
                           '--python', str(script), '--', '_worker', mode, str(snapshot), str(pending), *worker_tail]
                with (run / 'blender.log').open('xb') as log:
                    proc = subprocess.run(command, cwd=run, env=env, stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=subprocess.STDOUT, timeout=180, check=False)
                req.check()
                exe.check()
                _verify(source, request)
                require(pending.is_file(), 'WORKER_FAILED', 'No worker result; inspect blender.log')
                report = read_json(pending)
                require(not report['ok'] or proc.returncode == 0, 'WORKER_FAILED', 'Nonzero Blender exit')
                require(not report.get('weights_written') and not report.get('preview_created') and
                        not report.get('candidate') and not any(run.glob('*.blend')),
                        'UNEXPECTED_WRITE', 'Linux read-only mode produced a write artifact')
    # Context exits validate the guards again; only then publish the report.
    result = run / 'result.json'
    pending.rename(result)
    return report, result
