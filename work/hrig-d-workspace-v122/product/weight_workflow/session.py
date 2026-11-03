"""Shared GUI/API session; caller supplies loaded source, resources and idle guard."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import bpy
import numpy as np

from .contract import SCHEMA, MEMBERSHIP, PRESERVE, ONE, require, validate_request
from .native import WeightWorkflowTransaction, sha

_SESSION = None


def get_session(required=True):
    if required:
        require(_SESSION is not None, 'SESSION_NOT_INITIALIZED')
    return _SESSION


def strict_json(text):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'DUPLICATE_JSON_KEY', key)
            out[key] = value
        return out
    return json.loads(text, object_pairs_hook=pairs,
        parse_constant=lambda value: require(False, 'NONFINITE_JSON', value))


class WeightSession:
    def __init__(self, source, object_name, rig_name, outdir, idle_guard, source_resources):
        self.tx = WeightWorkflowTransaction(source, object_name, rig_name, idle_guard, source_resources=source_resources)
        self.outdir = Path(outdir).resolve()
        require(self.outdir.is_absolute(), 'OUTPUT_DIRECTORY')
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.vertices, self.donors = [], []
        self.snapshot_token, self.inspected_request = None, None
        self.last_error, self.log_error = None, None
        self.save_attempted = False
        self.session_id = uuid.uuid4().hex[:12]
        self.seq = 0

    @property
    def pending(self):
        return self.tx.pending is not None

    def invalidate(self):
        if not self.pending:
            self.snapshot_token, self.inspected_request = None, None

    def log(self, event, result):
        self.seq += 1
        value = {'session': self.session_id, 'sequence': self.seq, 'time': datetime.now(timezone.utc).isoformat(),
                 'event': event, 'result': deepcopy(result)}
        try:
            with (self.outdir / ('event-%s-%04d-%s.json' % (self.session_id, self.seq, event))).open('x', encoding='utf8') as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        except Exception as error:
            self.log_error = str(error)
        return deepcopy(result)

    def run(self, event, callback):
        try:
            result = callback()
            self.last_error = None
            return self.log(event, result)
        except Exception as error:
            self.last_error = getattr(error, 'code', type(error).__name__)
            self.log(event, {'status': 'error', 'code': self.last_error, 'detail': str(error)[:8000]})
            raise

    def selection(self, *, donors=False):
        require(not self.pending, 'RESTORE_FIRST')
        self.tx._guard()
        require(bpy.context.view_layer.objects.active == self.tx.obj, 'SELECT_TARGET_OBJECT')
        ids = [v.index for v in self.tx.mesh.vertices if v.select]
        require(ids, 'NO_VERTEX_SELECTION')
        if donors:
            self.donors = ids
        else:
            self.vertices = ids
        self.invalidate()
        return {'status': 'selection_loaded', 'kind': 'donors' if donors else 'targets', 'count': len(ids)}

    def refresh_groups(self, props, enabled=None):
        """Re-read native group locks/consumers without changing any author data."""
        require(not self.pending, 'RESTORE_FIRST')
        self.invalidate()
        groups, _, deform, _ = self.tx._guard()
        blocked = {row['group'] for row in self.tx._consumers()['items']}
        selected = set(enabled) if enabled is not None else {g.name for g in props.groups if g.enabled}
        valid = {g.name for g in groups if g.name in deform and not g.lock_weight and g.name not in blocked}
        if enabled is not None:
            require(selected <= valid, 'DEFAULT_PROTECTED_GROUPS', sorted(selected - valid))
        props.groups.clear()
        for group in groups:
            item = props.groups.add()
            item.name = group.name
            item.frozen = group.name not in valid
            item.enabled = group.name in selected and not item.frozen
        return {'status': 'groups_refreshed', 'groups': len(groups), 'editable': len(valid),
            'protected': len(groups) - len(valid), 'snapshot_invalidated': True}

    def request(self, props):
        operation = props.operation
        if operation == 'SMOOTH':
            params = {'iterations': props.iterations, 'factor': float(props.factor)}
        elif operation == 'MIRROR':
            params = {'vertex_pairs': [[r.source, r.target] for r in props.vertex_pairs],
                      'group_pairs': [[r.source, r.target] for r in props.group_pairs]}
        elif operation == 'NORMALIZE':
            params = {}
        elif operation == 'LIMIT_INFLUENCES':
            params = {'maximum': props.maximum}
        else:
            params = {'max_hops': props.max_hops, 'max_donors': props.max_donors, 'donor_vertices': list(self.donors)}
        return {'schema': SCHEMA, 'operation': operation, 'object': self.tx.obj.name, 'rig': self.tx.rig.name,
            'vertices': list(self.vertices), 'editable_groups': [g.name for g in props.groups if g.enabled],
            'membership': MEMBERSHIP, 'sum_policy': ONE if operation in ('NORMALIZE', 'GENERATE_FROM_NEIGHBORS') else PRESERVE,
            'parameters': params}

    def inspect(self, props):
        require(not self.pending, 'RESTORE_FIRST')
        self.invalidate()
        request = self.request(props)
        result = self.tx.inspect(request)
        self.snapshot_token, self.inspected_request = result['snapshot_token'], deepcopy(request)
        return result

    def preview(self, props):
        require(self.snapshot_token is not None and self.request(props) == self.inspected_request, 'INSPECT_CURRENT_INPUTS')
        try:
            return self.tx.preview(self.snapshot_token)
        finally:
            self.snapshot_token, self.inspected_request = None, None

    def restore(self):
        require(self.pending, 'NO_PENDING_EDIT')
        result = self.tx.restore(self.tx.pending['token'])
        self.invalidate()
        return result

    def return_zero(self):
        require(not self.pending, 'RESTORE_FIRST')
        self.tx._guard()
        bpy.context.scene.frame_set(0)
        bpy.context.view_layer.update()
        self.invalidate()
        return {'status': 'at_frame_zero'}

    def save_copy(self):
        require(self.pending and not self.save_attempted, 'SAVE_NOT_AVAILABLE')
        require(bpy.context.scene.frame_current_final == 0, 'SAVE_REQUIRES_FRAME_ZERO')
        path = self.outdir / 'output-weight-workflow.blend'
        metadata_path = self.outdir / 'output-weight-workflow-candidate.json'
        arrays_path = self.outdir / 'output-weight-workflow-witness.npz'
        require(all(not p.exists() and not p.is_symlink() for p in (path, metadata_path, arrays_path)), 'OUTPUT_EXISTS')
        require(path.resolve() != Path(self.tx.source['file']).resolve(), 'OUTPUT_IS_SOURCE')
        checked = self.tx.validate_applied()
        metadata = self.tx.candidate_metadata()
        main = bpy.data.filepath
        self.save_attempted = True
        saved = self.tx.persistence.save_candidate(path, self.outdir)
        require(bpy.data.filepath == main, 'SAVE_CHANGED_MAIN')
        require(self.tx.validate_applied() == checked, 'SAVE_CHANGED_APPLIED_STATE')
        state = self.tx._pending['after']
        with arrays_path.open('xb') as stream:
            np.savez_compressed(stream, bones=state['bones'], skin=state['skin'])
        metadata.update(candidate={'file': str(path), 'expected_sha256': saved['expected_sha256']},
            save=saved, witness={'file': str(arrays_path), 'expected_sha256': sha(arrays_path)},
            status='saved_requires_independent_cold_and_visual_acceptance')
        with metadata_path.open('x', encoding='utf8') as stream:
            json.dump(metadata, stream, ensure_ascii=False, allow_nan=False, indent=2)
        require(self.tx.validate_applied() == checked, 'RECEIPT_CHANGED_APPLIED_STATE')
        return {'status': 'candidate_saved', 'candidate': metadata['candidate'], 'metadata': str(metadata_path),
            'witness': metadata['witness'], 'qualification': metadata['status']}


def initialize(source, object_name, rig_name, outdir, default_request, *, idle_guard, source_resources):
    """Loaded source only. Does not open files, install addons or change preferences."""
    global _SESSION
    require(_SESSION is None or not _SESSION.pending, 'EXISTING_SESSION_PENDING')
    default_request = validate_request(default_request)
    require(default_request['object'] == object_name and default_request['rig'] == rig_name, 'DEFAULT_REQUEST_BINDING')
    session = WeightSession(source, object_name, rig_name, outdir, idle_guard, source_resources)
    from . import ui
    ui.register()
    _SESSION = session
    p = bpy.context.window_manager.hrig_weight_workflow
    session.refresh_groups(p, default_request['editable_groups'])
    p.vertex_pairs.clear()
    p.group_pairs.clear()
    p.operation = default_request['operation']
    session.vertices = list(default_request['vertices'])
    params = default_request['parameters']
    if p.operation == 'SMOOTH':
        p.iterations, p.factor = params['iterations'], params['factor']
    elif p.operation == 'MIRROR':
        for a, b in params['vertex_pairs']:
            row = p.vertex_pairs.add()
            row.source, row.target = a, b
        for a, b in params['group_pairs']:
            row = p.group_pairs.add()
            row.source, row.target = a, b
    elif p.operation == 'LIMIT_INFLUENCES':
        p.maximum = params['maximum']
    elif p.operation == 'GENERATE_FROM_NEIGHBORS':
        p.max_hops, p.max_donors = params['max_hops'], params['max_donors']
        session.donors = list(params['donor_vertices'])
    # UI FloatProperty is native f32; its actual request is authoritative.
    session.log('initialize', {'source': source, 'request': session.request(p),
        'ui_note': 'GUI FloatProperty factor may round input to f32; use this actual request for matching backend acceptance'})
    return session
