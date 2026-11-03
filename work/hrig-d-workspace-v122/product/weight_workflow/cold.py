"""Read-only validator for an already loaded, independently reopened candidate."""
import json
from pathlib import Path
import numpy as np
import bpy
import character_state as C
from .native import WeightWorkflowTransaction, rows_document, rows_digest, array_digest, sha
from .contract import require


def validate(metadata_path, expected_metadata_sha256):
    path = Path(metadata_path)
    require(path.is_absolute() and sha(path) == expected_metadata_sha256, 'METADATA_IDENTITY')
    metadata = json.loads(path.read_text(encoding='utf8'))
    require(metadata['schema'] == 'hrig.weight-workflow-candidate.v1', 'CANDIDATE_METADATA_VERSION')
    require(sha(metadata['source']['file']) == metadata['source']['expected_sha256'], 'COLD_SOURCE_PROVENANCE_CHANGED')
    identity = metadata['candidate']
    request = metadata['request']
    tx = WeightWorkflowTransaction(identity, request['object'], request['rig'], lambda: True,
        source_resources=metadata['source_resources'])
    require(tx._implementation == metadata['implementation'], 'COLD_IMPLEMENTATION_CHANGED')
    # No authoring, frame setting, selection change or current-value adoption.
    state = tx._collect()
    witness = metadata['witness']
    require(sha(witness['file']) == witness['expected_sha256'], 'COLD_WITNESS_IDENTITY')
    with np.load(witness['file'], allow_pickle=False) as arrays:
        checks = {
            'full_author_summary': C.summarize(state['author']) == metadata['full_author_summary'],
            'context': state['context'] == metadata['context'],
            'all_raw_rows': rows_document(state['rows']) == metadata['all_raw_rows'],
            'all_raw_rows_digest': rows_digest(state['rows']) == metadata['all_raw_rows_sha256'],
            'bone_names': state['bone_names'] == metadata['bone_names'],
            'bones_bytes': tx._array_same(state['bones'], arrays['bones']),
            'skin_bytes': tx._array_same(state['skin'], arrays['skin']),
            'bones_digest': array_digest(state['bones']) == metadata['bones_sha256'],
            'skin_digest': array_digest(state['skin']) == metadata['skin_sha256'],
        }
    require(all(checks.values()), 'COLD_CANDIDATE_MISMATCH', checks)
    tx._guard()
    return {'status': 'cold_exact_in_declared_scope', 'checks': checks, 'candidate': identity,
        'runtime': {'version': list(bpy.app.version), 'build': bpy.app.build_hash.decode()},
        'qualification': 'one matched-config reopen; no visible quality, arbitrary pose or performance certificate'}
