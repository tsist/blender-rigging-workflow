# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the Linux preview using only disposable self-generated rigging data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SELECTED = ROOT / 'cli' / 'selected_skinning_cli.py'
POLICY = ROOT / 'cli' / 'skinning_policy_candidate.py'


def exercise(blender, work):
    env = os.environ.copy()
    for name in ['HOME', 'BLENDER_USER_RESOURCES', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR']:
        folder = work / name.lower()
        folder.mkdir(mode=0o700)
        env[name] = str(folder)
    subprocess.run([str(blender), '-b', '-t', '2', '--factory-startup', '--offline-mode', '--disable-autoexec',
                    '--python-exit-code', '1', '--python', str(Path(__file__).with_name('fixture_linux_readonly.py')),
                    '--', str(work)], cwd=work, env=env, check=True)

    def call(script, mode, name=None, *, request=None, executable=blender):
        name = name or mode
        run = work / f'run-{name}'
        p = subprocess.run([sys.executable, '-B', str(script), mode, '--request',
                            str(request or work / f'{mode}.json'), '--run-dir', str(run),
                            '--blender', str(executable)],
                           cwd=work, check=False, text=True, capture_output=True)
        assert p.stdout, p.stderr
        outcome = json.loads(p.stdout)
        assert not list(run.glob('*.blend')), outcome
        return p.returncode, outcome, run

    before = {p: p.read_bytes() for p in work.glob('*.blend')}
    for script, mode in [(SELECTED, 'inspect'), (POLICY, 'qualify'), (POLICY, 'plan')]:
        code, outcome, run = call(script, mode)
        assert code == 0 and outcome['ok'], outcome
        report = json.loads((run / 'result.json').read_text())
        assert report['source_unchanged'] and report['weights_written'] is False, report
        assert not (run / 'worker-result.pending.json').exists()
        if mode == 'inspect':
            assert set(report['scopes']) == {'start', 'apply'}, report
        else:
            assert report['qualify']['native_binding_eligible'], report
        if mode == 'plan':
            assert report['changed_point_group_pairs'] > 0 and report['prepared_weights_created'] is False, report
        print(f'PASS {mode}')

    def rejects(script, mode, name, expected, **kwargs):
        code, result, run = call(script, mode, name, **kwargs)
        assert code != 0 and not result['ok'], result
        assert result['error']['code'] == expected, result
        if expected != 'EXTERNAL_DEPENDENCY':
            assert not run.exists(), run
        print(f'PASS rejects {name}: {expected}')

    for script, mode in [(SELECTED, 'execute'), (POLICY, 'preview'), (POLICY, 'apply')]:
        rejects(script, mode, script.stem + '-' + mode, 'UNSUPPORTED_HOST')
    request = work / 'inspect.json'
    wrong = json.loads(request.read_text())
    wrong['source']['sha256'] = '0' * 64
    bad = work / 'bad.json'
    bad.write_text(json.dumps(wrong))
    rejects(SELECTED, 'inspect', 'wrong-sha', 'SOURCE_SHA_MISMATCH', request=bad)
    alias = work / 'alias.json'
    alias.symlink_to(request)
    rejects(SELECTED, 'inspect', 'symlink', 'INVALID_REQUEST', request=alias)
    unqualified = work / 'unqualified'
    unqualified.write_bytes(b'\x7fELF not a qualified Blender binary')
    unqualified.chmod(0o700)
    rejects(SELECTED, 'inspect', 'unqualified-executable', 'UNSUPPORTED_HOST', executable=unqualified)
    rejects(POLICY, 'qualify', 'external-path', 'EXTERNAL_DEPENDENCY', request=work / 'external.json')
    with (work / 'source.blend').open('r+b'):
        rejects(SELECTED, 'inspect', 'active-writer', 'CONFLICT')
    assert all(p.read_bytes() == data for p, data in before.items()), 'Source bytes changed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', type=Path, required=True)
    parser.add_argument('--work-parent', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rigging-linux-smoke-', dir=args.work_parent) as directory:
        exercise(args.blender.resolve(strict=True), Path(directory).resolve())
