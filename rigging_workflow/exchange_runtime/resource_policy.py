# SPDX-License-Identifier: GPL-3.0-or-later
# Canonical candidate relocation; see SOURCE-MAP.json.
import json
from pathlib import Path
import bpy
from .io_support import Failure, FileGuard
from .id_inspection import path_value
RESOURCE_KEY = 'blenderctl_image_manifest'

def image_members(image):
    raw = image.get(RESOURCE_KEY)
    if raw:
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise Failure('INVALID_REQUEST', 'Invalid stored image manifest') from exc
    return []

def resource_guards(params, stack):
    declared = {str(Path(f['file']).resolve()): f['expected_sha256'] for f in params.get('resources', [])}
    checked_files = {}
    for image in bpy.data.images:
        if image.type in ('RENDER_RESULT', 'COMPOSITING'):
            continue
        if image.source not in ('FILE', 'TILED', 'SEQUENCE', 'GENERATED'):
            raise Failure('UNSUPPORTED', 'Image source requires another media workflow: ' + image.source)
        if image.source == 'FILE' and (not image.filepath) and (not image.packed_file):
            raise Failure('INVALID_REQUEST', 'Image has no file or packed contents')
        members = image_members(image)
        if image.source == 'FILE' and (not members) and image.filepath:
            p = path_value(image.filepath, image.library)
            expected = declared.get(str(Path(p).resolve()))
            if not image.packed_file and expected is None:
                raise Failure('INVALID_REQUEST', 'Existing external image requires resources file/hash: ' + p)
            members = [{'file': p, 'expected_sha256': expected, 'number': 0}]
        packed = {p.tile_number for p in image.packed_files if p.packed_file}
        if image.source == 'TILED' and (not members):
            raise Failure('INVALID_REQUEST', 'Existing tiled image requires an authored explicit member manifest')
        if image.source == 'SEQUENCE' and (not members):
            raise Failure('INVALID_REQUEST', 'Existing sequence requires an authored explicit member manifest')
        if members and image.source == 'FILE' and (not image.packed_file):
            if len(members) != 1 or members[0]['number'] != 0 or Path(path_value(image.filepath, image.library)).resolve() != Path(members[0]['file']).resolve():
                raise Failure('CONFLICT', 'Image path differs from stored member manifest')
        if image.source == 'TILED':
            if {t.number for t in image.tiles} != {m['number'] for m in members}:
                raise Failure('CONFLICT', 'UDIM tiles differ from declared members')
            external = [m for m in members if m['number'] not in packed]
            if external:
                template = path_value(image.filepath, image.library)
                token = '<udim>' if '<udim>' in template else '<UDIM>'
                if template.count(token) != 1 or any((Path(template.replace(token, str(m['number']))).resolve() != Path(m['file']).resolve() for m in external)):
                    raise Failure('CONFLICT', 'UDIM path differs from declared member')
        for member in members:
            if image.source == 'FILE' and image.packed_file or (image.source == 'TILED' and member['number'] in packed):
                continue
            p = str(Path(member['file']).resolve())
            expected = declared.get(p, member.get('expected_sha256'))
            if p in checked_files:
                if checked_files[p] != expected:
                    raise Failure('CONFLICT', 'Conflicting image fingerprints')
                continue
            guard = stack.enter_context(FileGuard(p))
            actual = guard.sha256()
            if expected is None or actual != expected:
                raise Failure('CONFLICT', 'Texture fingerprint differs: ' + p)
            checked_files[p] = actual
    return checked_files


def discover_resources():
    """Resolve actual snapshot FILE members; ambiguous UDIM/sequence stays refused."""
    found={}
    for image in bpy.data.images:
        if image.type in ('RENDER_RESULT','COMPOSITING') or image.source=='GENERATED':continue
        if image.source not in ('FILE','TILED','SEQUENCE'):raise Failure('UNSUPPORTED','Image source needs another media workflow: '+image.source)
        if image.source=='FILE' and image.packed_file:continue
        members=image_members(image)
        if image.source in ('TILED','SEQUENCE') and not members:raise Failure('UNSUPPORTED','Explicit member manifest required for '+image.source)
        if image.source=='FILE' and not members:
            if not image.filepath:raise Failure('INVALID_REQUEST','Image has no file or packed contents')
            members=[{'file':path_value(image.filepath,image.library)}]
        packed={p.tile_number for p in image.packed_files if p.packed_file}
        for member in members:
            if image.source=='TILED' and member['number'] in packed:continue
            path=str(Path(member['file']).resolve())
            with FileGuard(path) as guard:actual=guard.sha256()
            expected=member.get('expected_sha256')
            if expected is not None and actual!=expected:raise Failure('CONFLICT','Authored image member fingerprint changed: '+path)
            if path in found and found[path]!=actual:raise Failure('CONFLICT','Conflicting image member identity')
            found[path]=actual
    return [{'file':path,'expected_sha256':value} for path,value in sorted(found.items())]
