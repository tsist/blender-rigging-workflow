# SPDX-License-Identifier: GPL-3.0-or-later
"""Public-domain candidate for explicit shared-rig native-clock import.

The baseline is the actual file/index import, never a source rig or witness.
This candidate is not installed in the root CLI.
"""
import math
import time
import hashlib
from pathlib import Path
from contextlib import ExitStack
import bpy
import numpy as np
from . import format_io as exchange
from . import resource_policy as nodes
from . import io_support as scenes
from . import core as core
from . import key_library as keylib
from . import native_reader as reader
from .io_support import Failure, atomic_json, digest
ADAPTER = 'SHARED_RIG_NATIVE_TIME_V1'
TOLERANCE = 1e-05

def unsupported(message):
    raise Failure('UNSUPPORTED', 'Shared rig import: ' + message)

def inventory():
    scene = bpy.context.scene
    rigs = [o for o in bpy.data.objects if o.type == 'ARMATURE']
    if len(rigs) != 1:
        unsupported('exactly one shared armature required')
    rig = rigs[0]
    meshes = {}
    excluded = []
    if rig.name not in scene.objects:
        unsupported('shared armature is outside the imported Scene')
    if not rig.data.bones or rig.pose is None:
        unsupported('shared armature requires nonempty native bones and a concrete pose')
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and obj.name not in scene.objects and (not core._is_hidden_custom_shape(obj)):
            unsupported('unlinked extra Mesh object is not an eligible hidden custom shape: ' + obj.name)
    for obj in scene.objects:
        if obj.type == 'MESH':
            if core._is_hidden_custom_shape(obj):
                excluded.append(obj.name)
                continue
            mods = list(obj.modifiers)
            if len(mods) != 1 or mods[0].type != 'ARMATURE' or mods[0].object != rig:
                unsupported('every real Mesh requires only one Armature modifier referencing the shared rig: ' + obj.name)
            if not obj.data.vertices:
                unsupported('empty skin Mesh')
            for vertex in obj.data.vertices:
                if not any((g.weight > 0 and obj.vertex_groups[g.group].name in rig.data.bones for g in vertex.groups)):
                    unsupported('unweighted skin vertex: ' + obj.name)
            meshes[obj.name] = obj
        elif obj.type not in {'ARMATURE', 'EMPTY'}:
            unsupported('unexpected imported object type ' + obj.type)
    if not 1 <= len(meshes) <= 16:
        unsupported('requires 1..16 real skin Mesh objects')
    allowed = {rig.as_pointer()} | {o.data.shape_keys.as_pointer() for o in meshes.values() if o.data.shape_keys}
    bound = reader.owners()
    if any((owner.as_pointer() not in allowed for owner, *_ in bound)):
        unsupported('extra animated owner outside the shared rig and its Mesh Key libraries')
    return (rig, meshes, excluded)

def structure(rig, meshes):
    return {'rig': rig.name, 'bones': len(rig.data.bones), 'meshes': [{'name': name, 'data': o.data.name, 'vertices': len(o.data.vertices), 'shape_keys': [k.name for k in o.data.shape_keys.key_blocks] if o.data.shape_keys else [], 'armature': next((m.object.name for m in o.modifiers if m.type == 'ARMATURE'))} for name, o in meshes.items()]}

def file_sample(obj):
    value = core._eval_snapshot(obj, bpy.context.evaluated_depsgraph_get(), with_triangles=True)
    value['vertices'] = np.asarray(value['vertices'], dtype=np.float64)
    return value

def key_values(obj):
    return {k.name: float(k.value) for k in obj.data.shape_keys.key_blocks} if obj.data.shape_keys else {}

def reference_table(meshes, clock):
    samples = []
    proof = []
    for i, frame in enumerate(clock['native_frames']):
        core._set_frame(bpy.context.scene, i + 1)
        row = {}
        identity = {}
        for name, o in meshes.items():
            snapshot = file_sample(o)
            values = key_values(o)
            row[name] = (snapshot, values)
            identity[name] = {'file_import_world_sha256': hashlib.sha256(snapshot['vertices'].tobytes()).hexdigest(), 'vertices': len(snapshot['vertices']), 'triangles': snapshot['triangles'], 'Key_values': values}
        samples.append(row)
        proof.append({'file_sample_index': i + 1, 'file_frame': clock['file_frames'][i], 'native_frame': frame, 'meshes': identity})
    return (samples, proof)

def compare_table(meshes, clock, samples, phase, reverse=False):
    checks = []
    keys = []
    order = range(len(samples) - 1, -1, -1) if reverse else range(len(samples))
    for i in order:
        frame = clock['native_frames'][i]
        core._set_frame(bpy.context.scene, frame)
        for name, o in meshes.items():
            before, expected_keys = samples[i][name]
            after = file_sample(o)
            if before['triangle_rows'] != after['triangle_rows']:
                raise Failure('VALIDATION_FAILED', 'File-import evaluated triangle order changed: ' + name)
            check = exchange.compare_geometry(before, after)
            indexed = float(np.linalg.norm(before['vertices'] - after['vertices'], axis=1).max(initial=0.0))
            check.update(frame=frame, mesh=name, layer=phase, indexed_error_meters=indexed, declared_position_tolerance=TOLERANCE, declared_position_pass=indexed <= TOLERANCE and check['bidirectional_vertex_distance'] <= TOLERANCE)
            check['scope'] = 'Actual file-import sample at index %d -> native file-derived frame %.17g; %s; ' % (i + 1, frame, phase) + check['scope']
            check['ok'] = bool(check['ok'] and check['declared_position_pass'])
            checks.append(check)
            actual = key_values(o)
            if set(actual) != set(expected_keys):
                raise Failure('VALIDATION_FAILED', 'File Key inventory changed')
            if actual:
                error = max((abs(v - actual[k]) for k, v in expected_keys.items()))
                keys.append({'file_sample_index': i + 1, 'native_frame': frame, 'mesh': name, 'phase': phase, 'max_Key_value_error': error, 'ok': error <= TOLERANCE})
    if not all((r['ok'] for r in checks + keys)):
        raise Failure('VALIDATION_FAILED', 'Actual file/native-time/reopen comparison exceeded 1e-5 position/Key gate')
    return (checks, keys)

def checkpoint_check(actions, clock):
    expected = {r['index_action']: r['original_action_digest'] for r in actions['actions']}
    count = 0
    bindings = []
    for owner, ad, a, slot, stash in reader.owners():
        if not a.name.startswith('NativeTime::'):
            unsupported('native active Action binding missing')
        old = bpy.data.actions.get(a.name[len('NativeTime::'):])
        if old is None or not old.use_fake_user or reader.action_digest(old) != expected.get(old.name):
            raise Failure('VALIDATION_FAILED', 'Retained index Action/slot checkpoint changed')
        oldslot = next((s for s in old.slots if s.identifier == slot.identifier and s.target_id_type == slot.target_id_type))
        oldbag = next((b for b in reader.bags(old) if b.slot_handle == oldslot.handle))
        newbag = next((b for b in reader.bags(a) if b.slot_handle == slot.handle))
        channels = {(c.data_path, c.array_index): c for c in oldbag.fcurves}
        if len(channels) != len(newbag.fcurves):
            raise Failure('VALIDATION_FAILED', 'Native curve inventory changed')
        for c in newbag.fcurves:
            p = reader.co(c)
            q = reader.co(channels[c.data_path, c.array_index])
            if not np.array_equal(p[:, 0], clock['native_frames']) or not np.array_equal(p[:, 1], q[:, 1]):
                raise Failure('VALIDATION_FAILED', 'Actual file sample storage is not exact after native-time restoration')
            count += 1
        bindings.append({'owner': owner.name, 'type': owner.bl_rna.identifier, 'native_action': a.name, 'index_action': old.name, 'slot_identifier': slot.identifier, 'slot_handle': slot.handle, 'preserved_inactive_nla': stash})
    return {'curves': count, 'keys_per_curve': len(clock['native_frames']), 'format_values_exact': True, 'checkpoint_digests_match': True, 'bindings': bindings}

def entry(file):
    return {'file': str(file), 'sha256': digest(file), 'bytes': file.stat().st_size}

def native_frame_bounds(scene, clock, initial_frame):
    lo = math.floor(clock['native_frames'][0])
    hi = math.ceil(clock['native_frames'][-1])
    for name, value in [('frame_start', lo), ('frame_end', hi)]:
        prop = scene.bl_rna.properties[name]
        if not prop.hard_min <= value <= prop.hard_max:
            unsupported('file clock exceeds actual Scene ' + name + ' RNA limits')
    if not lo <= initial_frame <= hi:
        raise Failure('INVALID_REQUEST', 'Initial frame must lie in the actual file clock range')
    return (lo, hi)

def import_(params, spec, job):
    started = time.monotonic()
    job = Path(job)
    path = Path(params['file']).resolve(strict=True)
    candidate = job / 'exchange-candidate.blend'
    sidecar = job / 'shared-rig-import.json'
    report_path = job / 'exchange-report.json'
    if any((p.exists() for p in (candidate, sidecar, report_path))):
        raise Failure('CONFLICT', 'Shared rig import requires new candidate and sidecar paths')
    expected = params.get('expected_sha256')
    if expected is None or digest(path) != expected:
        raise Failure('CONFLICT', 'Protected input format SHA mismatch')
    evidence = {'adapter': ADAPTER, 'status': 'running', 'source_format': entry(path), 'comparison_source': 'actual bundled format-import values only; no original rig/native oracle input'}
    atomic_json(sidecar, evidence)
    try:
        with ExitStack() as stack:
            clock, temporary = reader.import_index(path, spec['format'], spec['fps'])
            scene = bpy.context.scene
            lo, hi = native_frame_bounds(scene, clock, spec['frame'])
            hashes = nodes.resource_guards(params, stack)
            rig, meshes, excluded = inventory()
            file_structure = structure(rig, meshes)
            core._set_frame(scene, 1)
            libraries = {name: keylib.capture_library(o) for name, o in meshes.items()}
            samples, identity = reference_table(meshes, clock)
            actions = reader.restore_native_actions(clock)
            checks, keychecks = compare_table(meshes, clock, samples, 'file_import_to_native_clock')
            core._set_frame(scene, clock['native_frames'][0])
            library_checks = {name: keylib.verify_native_library(libraries[name], o, position_tolerance=TOLERANCE) for name, o in meshes.items()}
            exact = checkpoint_check(actions, clock)
            scenes.named(scene, spec['scene'])
            core._set_frame(scene, spec['frame'])
            exchange.save_candidate(job)
            bpy.ops.wm.open_mainfile(filepath=str(candidate), load_ui=False, use_scripts=False)
            rig, meshes, reexcluded = inventory()
            if structure(rig, meshes) != file_structure or reexcluded != excluded:
                raise Failure('VALIDATION_FAILED', 'File/native candidate structure changed after reopen')
            reopened = bpy.context.scene
            if reopened.render.fps != spec['fps'] or reopened.render.fps_base != 1 or reopened.frame_start != lo or (reopened.frame_end != hi):
                raise Failure('VALIDATION_FAILED', 'Native candidate clock changed after reopen')
            coldchecks, coldkeys = compare_table(meshes, clock, samples, 'file_import_to_native_clock_reopen', reverse=True)
            core._set_frame(reopened, clock['native_frames'][0])
            coldlibrary = {name: keylib.verify_native_library(libraries[name], o, position_tolerance=TOLERANCE) for name, o in meshes.items()}
            coldexact = checkpoint_check(actions, clock)
            if digest(path) != expected:
                raise Failure('CONFLICT', 'Input format changed during shared native-time import')
            evidence.update(status='pass', clock=clock, temporary_index_import=temporary, structure=file_structure, excluded_custom_shapes=excluded, actions=actions, exact_format_storage=exact, reopened_exact_format_storage=coldexact, file_reference_table=identity, Key_checks=keychecks + coldkeys, library_checks=library_checks, reopened_library_checks=coldlibrary, source_format_sha256_after=digest(path), candidate=entry(candidate), scope='Complete actual file table, native clock and reopened candidate; no original-source or arbitrary continuous-time semantic proof')
            atomic_json(sidecar, evidence)
            report = {'exchange_report_version': '1.0', 'operation': 'import', 'format': spec['format'], 'mode': 'ANIMATION', 'settings': spec, 'checks': checks + coldchecks, 'outputs': [entry(candidate), entry(sidecar)], 'candidate': str(candidate), 'candidate_sha256': digest(candidate), 'resource_hashes': hashes, 'seconds': time.monotonic() - started, 'source_saved': False, 'reopen': 'pass', 'losses': ['Explicit shared LINEAR single-table reader; native values preserve actual bundled-import interpretation, not unknown original-source semantics', 'Original imported index Actions are retained as fake-user checkpoints; their muted NLA stashes must remain disabled', 'Complete finite file table was checked before/after native-time restoration and reopen; arbitrary continuous time, unsupported interpolation/NLA and ordinary importer repair are not certified']}
            atomic_json(report_path, report)
            return report
    except Exception:
        evidence['status'] = 'failed'
        atomic_json(sidecar, evidence)
        raise
