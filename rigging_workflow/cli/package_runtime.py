"""Refresh the canonical rigging manifest and optionally build a deterministic ZIP."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'exchange-runtime-manifest.json'


def package_files():
    return {path.relative_to(ROOT).as_posix(): path.read_bytes()
            for path in sorted(ROOT.rglob('*'))
            if path.is_file() and path != MANIFEST and '__pycache__' not in path.parts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-manifest', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    files = package_files()
    digest = {name: hashlib.sha256(payload).hexdigest() for name, payload in files.items()}
    observed = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if args.refresh_manifest:
        hashed = json.dumps(digest, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        observed.update(files=digest, source_map_sha256=hashlib.sha256(hashed.encode()).hexdigest(),
                        scope='Canonical SWM07 1.0 development source for Windows Blender 5.3+')
        temporary = MANIFEST.with_suffix('.json.tmp')
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(observed, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        temporary.replace(MANIFEST)
    elif observed['files'] != digest:
        raise ValueError('Source files changed. Refresh the rigging manifest first.')
    if args.output:
        if not args.output.is_absolute() or args.output.suffix.lower() != '.zip':
            raise ValueError('Provide an absolute, new .zip candidate output')
        files[MANIFEST.name] = MANIFEST.read_bytes()
        with ZipFile(args.output, 'x', compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for name, payload in sorted(files.items()):
                entry = ZipInfo(f'rigging_workflow/{name}')
                entry.compress_type = ZIP_DEFLATED
                archive.writestr(entry, payload, compresslevel=6)
        with ZipFile(args.output) as archive:
            if archive.testzip() or any(archive.read(f'rigging_workflow/{name}') != payload
                                       for name, payload in files.items()):
                raise ValueError('Candidate package could not be read back')
        print(hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
