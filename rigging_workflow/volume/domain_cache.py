# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated completed-domain results shared by jobs under one explicit root."""

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from zipfile import BadZipFile
import zlib

import numpy as np


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf8')


def _update_array(digest, array):
    array = np.ascontiguousarray(array)
    digest.update(_json((array.dtype.str, array.shape)))
    digest.update(memoryview(array).cast('B'))


def identity(runtime_fingerprint, bones, settings, vertices, triangles, points, source_ids):
    digest = sha256(_json(('rw.completed-volume-domain.v1', runtime_fingerprint, bones, settings)))
    for array in (vertices, triangles, points, source_ids):
        _update_array(digest, array)
    return digest.hexdigest()


class CompletedDomains:
    def __init__(self, job_root):
        self.directory = Path(job_root) / 'completed-volume-domains-v1'

    def _file(self, key):
        if len(key) != 64 or any(character not in '0123456789abcdef' for character in key):
            raise ValueError('Invalid completed domain fingerprint')
        return self.directory / (key + '.npz')

    def load(self, key, source_ids, bone_names):
        path = self._file(key)
        try:
            if self.directory.is_symlink() or self.directory.is_junction() or path.is_symlink():
                return None
            with np.load(path, allow_pickle=False) as archive:
                metadata = json.loads(str(archive['metadata'].item()))
                weights = archive['weights']
                if (metadata['key'] != key or archive['bone_names'].tolist() != bone_names
                        or not np.array_equal(archive['source_ids'], source_ids)
                        or weights.dtype != np.float32 or weights.shape != (len(source_ids), len(bone_names))
                        or not np.isfinite(weights).all() or (weights < 0).any()
                        or np.max(np.abs(weights.sum(axis=1) - 1), initial=0) > 1e-5):
                    return None
                digest = sha256()
                _update_array(digest, weights)
                if digest.hexdigest() != metadata['weights_sha256']:
                    return None
                statistics, warnings = metadata['statistics'], metadata['warnings']
                if not isinstance(statistics, dict) or not isinstance(warnings, list):
                    return None
                return weights, statistics, warnings, metadata['weights_sha256']
        except (OSError, KeyError, ValueError, TypeError, EOFError, BadZipFile, zlib.error):
            return None

    def store(self, key, source_ids, bone_names, weights, statistics, warnings):
        path = self._file(key)
        self.directory.mkdir(exist_ok=True)
        if self.directory.is_symlink() or self.directory.is_junction():
            raise OSError('The completed-domain directory cannot be redirected')
        digest = sha256()
        _update_array(digest, weights)
        checksum = digest.hexdigest()
        metadata = _json({'key': key, 'weights_sha256': checksum, 'statistics': statistics,
                          'warnings': warnings}).decode('utf8')
        with tempfile.NamedTemporaryFile(dir=self.directory, prefix='pending-', suffix='.npz', delete=False) as stream:
            pending = Path(stream.name)
            try:
                np.savez_compressed(stream, weights=weights, metadata=np.asarray(metadata),
                                    source_ids=source_ids, bone_names=np.asarray(bone_names))
            except BaseException:
                stream.close()
                pending.unlink(missing_ok=True)
                raise
        try:
            # A same-key retry repairs an invalid generated cache entry. Every
            # reader sees a complete file, including across worker restarts.
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
        return checksum
