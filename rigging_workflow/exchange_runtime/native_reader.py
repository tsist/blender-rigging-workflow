# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated native-clock reader candidate, not a registered public CLI/API.

Only actual format clocks and imported format values construct the Actions.
No witness, source rig, or native animation oracle is accepted by this module.
Supports one shared LINEAR file time table and plain layered native Actions.
"""
from __future__ import annotations
import hashlib
import math
from types import SimpleNamespace
import bpy
import numpy as np
from . import core as core
from .io_support import Failure
MAX_FRAME_QUANTIZATION = 1e-05

def refuse(message):
    raise Failure('UNSUPPORTED', 'Native-time reader: ' + message)

def validate_clock(frames):
    raw = np.asarray(frames, dtype=np.float64)
    if raw.ndim != 1 or len(raw) < 2 or (not np.isfinite(raw).all()) or np.any(np.diff(raw) <= 0):
        refuse('file frame table must be finite and strictly increasing')
    stored = raw.astype(np.float32).astype(np.float64)
    if not np.isfinite(stored).all() or np.any(np.diff(stored) <= 0):
        refuse('file times do not have one-to-one float32 native-frame identities')
    error = np.abs(stored - raw)
    if error.max() > MAX_FRAME_QUANTIZATION:
        refuse('file/native frame quantization exceeds explicit 1e-5-frame bound')
    return (stored.tolist(), {'max_abs_error_frames': float(error.max()), 'changed_times': int(np.count_nonzero(error)), 'limit_frames': MAX_FRAME_QUANTIZATION})

def read_file_clock(path, kind, fps):
    if type(fps) is not int or fps <= 0:
        refuse('a positive integer native fps is required')
    if kind == 'GLB':
        doc, binary = core._load_glb(path)
        if len(doc.get('animations', [])) != 1:
            refuse('GLB requires one Animation')
        animation = doc['animations'][0]
        used = [c['sampler'] for c in animation['channels']]
        if set(used) != set(range(len(animation['samplers']))):
            refuse('unused or missing GLB animation samplers')
        targets = []
        for c in animation['channels']:
            t = c['target']
            key = (t.get('node'), t.get('path'))
            if key[0] is None or key[1] not in {'translation', 'rotation', 'scale', 'weights'} or key in targets:
                refuse('unsupported/duplicate GLB animation target')
            targets.append(key)
        for sampler in animation['samplers']:
            if sampler.get('interpolation', 'LINEAR') != 'LINEAR':
                refuse('GLB supports only shared LINEAR sampling')
        accessors, frames, seconds = core._glb_file_time_table(path, fps)
        source = {'unit': 'seconds', 'raw_times': seconds, 'input_accessors': sorted(accessors), 'samplers': len(animation['samplers']), 'channels': len(used)}
    elif kind == 'FBX':
        from io_scene_fbx import parse_fbx
        tree, version = parse_fbx.parse(str(path))
        version2, arrays, ticks, ktime, endpoints = core._fbx_file_time_table(path)
        if version != version2:
            refuse('FBX parser version mismatch')
        if not arrays or any((len(row) != len(ticks) or row != ticks for row in arrays)):
            refuse('every FBX KeyTime array must equal one complete strictly increasing table')
        if any((a >= b for a, b in zip(ticks, ticks[1:]))):
            refuse('FBX KeyTime is not strictly increasing')
        counts = {'AnimationStack': 0, 'AnimationLayer': 0, 'AnimationCurve': 0}
        flags_seen = set()

        def walk(node):
            label = node.id.decode(errors='replace')
            if label in counts:
                counts[label] += 1
            if node.id == b'AnimationCurve':
                props = {c.id: c for c in node.elems}
                for required in (b'KeyTime', b'KeyValueFloat', b'KeyAttrFlags', b'KeyAttrRefCount'):
                    if required not in props:
                        refuse('incomplete FBX AnimationCurve')
                values = np.asarray(props[b'KeyValueFloat'].props[0], dtype=np.float64)
                flags = list(props[b'KeyAttrFlags'].props[0])
                refs = list(props[b'KeyAttrRefCount'].props[0])
                if len(values) != len(ticks) or not np.isfinite(values).all():
                    refuse('FBX curve values must be finite and complete')
                if not flags or any((int(f) != 24836 for f in flags)) or len(refs) != len(flags) or (sum(refs) != len(ticks)):
                    refuse('FBX requires known 24836 LINEAR flags with complete reference counts')
                flags_seen.update((int(f) for f in flags))
            for child in node.elems:
                walk(child)
        walk(tree)
        if counts['AnimationStack'] != 1 or counts['AnimationLayer'] != 1 or counts['AnimationCurve'] != len(arrays):
            refuse('FBX requires one complete shared AnimationStack/AnimationLayer')
        frames = [tick / ktime * fps for tick in ticks]
        source = {'unit': 'FBX_KTIME', 'raw_times': ticks, 'ktime_per_second': ktime, 'version': version, 'clock_endpoints': endpoints, 'counts': counts, 'linear_flags': sorted(flags_seen)}
    else:
        refuse('only GLB and FBX are supported')
    stored, quantization = validate_clock(frames)
    return {'kind': kind, 'fps': fps, 'file_frames': frames, 'native_frames': stored, 'quantization': quantization, 'file_clock': source, 'native_duration_seconds': (stored[-1] - stored[0]) / fps, 'file_duration_seconds': (frames[-1] - frames[0]) / fps}

def owners():
    found = []
    seen_ids = set()
    for collection in bpy.data.bl_rna.properties:
        if collection.type != 'COLLECTION' or collection.identifier == 'rna_type':
            continue
        try:
            items = getattr(bpy.data, collection.identifier)
        except AttributeError:
            continue
        for owner in items:
            identity = owner.as_pointer()
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            ad = getattr(owner, 'animation_data', None)
            if not ad:
                continue
            if ad.drivers:
                refuse('imported drivers are outside the reader model')
            if ad.action is None:
                if ad.nla_tracks:
                    refuse('NLA-only imported owner')
                continue
            if owner.bl_rna.identifier not in {'Object', 'Key'}:
                refuse('unsupported animated owner ' + owner.bl_rna.identifier)
            if not ad.action_slot:
                refuse('active Action has no concrete slot')
            if ad.action_blend_type != 'REPLACE' or ad.action_influence != 1.0 or ad.use_tweak_mode:
                refuse('active Action blending/tweak mode is unsupported')
            stash = []
            for track in ad.nla_tracks:
                inactive = bool(track.mute) or (bool(track.strips) and all((s.mute for s in track.strips)))
                if not inactive or track.is_solo:
                    refuse('enabled or solo NLA track is unsupported')
                stash.append({'name': track.name, 'mute': track.mute, 'strips': [{'name': s.name, 'mute': s.mute, 'action': s.action.name if s.action else None, 'slot_handle': s.action_slot_handle, 'start': s.frame_start, 'end': s.frame_end} for s in track.strips]})
            found.append((owner, ad, ad.action, ad.action_slot, stash))
    if not found:
        refuse('no supported imported animated owners')
    return found

def bags(action):
    if len(action.layers) != 1 or len(action.layers[0].strips) != 1:
        refuse('only one Action layer and keyframe strip are qualified')
    layer = action.layers[0]
    if getattr(layer, 'influence', 1.0) != 1.0 or getattr(layer, 'mix_mode', 'REPLACE') not in {'REPLACE'}:
        refuse('unsupported Action layer blending')
    strip = layer.strips[0]
    if strip.type != 'KEYFRAME':
        refuse('non-keyframe Action strip')
    valid = {s.handle for s in action.slots}
    if any((b.slot_handle not in valid for b in strip.channelbags)):
        refuse('orphan channelbag slot')
    return list(strip.channelbags)

def co(curve):
    points = np.empty(len(curve.keyframe_points) * 2, dtype=np.float32)
    curve.keyframe_points.foreach_get('co', points)
    return points.reshape((-1, 2))

def action_digest(action):
    h = hashlib.sha256()
    for bag in bags(action):
        h.update(str(bag.slot_handle).encode())
        for curve in bag.fcurves:
            h.update((curve.data_path + '\x00' + str(curve.array_index)).encode())
            h.update(co(curve).tobytes())
    return h.hexdigest()

class ConfiguredCurves:

    def __init__(self, bag, source):
        self.bag, self.source = (bag, source)

    def new(self, *, data_path, index):
        c = self.bag.fcurves.new(data_path=data_path, index=index)
        for attr in ('mute', 'lock', 'select', 'hide', 'color_mode', 'auto_smoothing'):
            setattr(c, attr, getattr(self.source, attr))
        c.color = self.source.color
        if self.source.group:
            name = self.source.group.name
            c.group = self.bag.groups.get(name) or self.bag.groups.new(name)
        return c

def restore_native_actions(clock):
    bound = owners()
    n = len(clock['native_frames'])
    index_times = np.arange(1, n + 1, dtype=np.float32)
    old_actions = list({a.as_pointer(): a for _, _, a, _, _ in bound}.values())
    if set(bpy.data.actions) != set(old_actions):
        refuse('unbound extra imported Actions are unsupported')
    old_hashes = {a.as_pointer(): action_digest(a) for a in old_actions}
    for a in old_actions:
        slot_handles = {s.handle for _, _, aa, s, _ in bound if aa == a}
        if slot_handles != {s.handle for s in a.slots}:
            refuse('unused/ambiguous imported slot')
        for bag in bags(a):
            seen = set()
            for c in bag.fcurves:
                if (c.data_path, c.array_index) in seen:
                    refuse('duplicate curve channel')
                seen.add((c.data_path, c.array_index))
                points = co(c)
                if len(points) != n or not np.array_equal(points[:, 0], index_times) or (not np.isfinite(points).all()):
                    refuse('curve must contain every actual file sample exactly once in index order')
                if c.modifiers or c.sampled_points or c.extrapolation != 'CONSTANT' or c.mute:
                    refuse('curve modifier/sample/mute/extrapolation mode unsupported')
                if any((p.interpolation != 'LINEAR' for p in c.keyframe_points)):
                    refuse('only LINEAR native curves are supported')
    mapping = {}
    action_records = []
    curve_total = 0
    for a in old_actions:
        copied = a.copy()
        copied.name = 'NativeTime::' + a.name
        copied.use_fake_user = True
        slot_map = {}
        for old in a.slots:
            matches = [s for s in copied.slots if s.identifier == old.identifier and s.target_id_type == old.target_id_type]
            if len(matches) != 1:
                refuse('Action.copy changed slot identity ambiguously')
            slot_map[old.handle] = matches[0]
        original_bags = {b.slot_handle: b for b in bags(a)}
        value_hash = hashlib.sha256()
        for new_bag in bags(copied):
            handles = [h for h, s in slot_map.items() if s.handle == new_bag.slot_handle]
            if len(handles) != 1:
                refuse('copied bag slot is unmapped')
            old_bag = original_bags[handles[0]]
            for c in list(new_bag.fcurves):
                new_bag.fcurves.remove(c)
            for source in old_bag.fcurves:
                values = co(source)[:, 1].copy()
                adapter = SimpleNamespace(fcurves=ConfiguredCurves(new_bag, source))
                created = core._store_curve(adapter, source.data_path, source.array_index, clock['native_frames'], values, 'LINEAR')
                actual = co(created)
                if not np.array_equal(actual[:, 1], values) or not np.array_equal(actual[:, 0], clock['native_frames']):
                    raise Failure('VALIDATION_FAILED', 'Native-time rebuild changed actual format sample storage')
                value_hash.update(actual[:, 1].tobytes())
                curve_total += 1
        a.use_fake_user = True
        mapping[a.as_pointer()] = (copied, slot_map)
        action_records.append({'index_action': a.name, 'native_action': copied.name, 'original_action_digest': old_hashes[a.as_pointer()], 'index_action_digest_after': action_digest(a), 'format_values_sha256': value_hash.hexdigest(), 'slots': [{'old_handle': s.handle, 'new_handle': slot_map[s.handle].handle, 'identifier': s.identifier, 'target_type': s.target_id_type} for s in a.slots]})
    bindings = []
    for owner, ad, a, slot, stash in bound:
        copied, slot_map = mapping[a.as_pointer()]
        ad.action = copied
        ad.action_slot = slot_map[slot.handle]
        if ad.action != copied or ad.action_slot != slot_map[slot.handle]:
            raise Failure('VALIDATION_FAILED', 'Native Action/slot owner binding changed')
        bindings.append({'owner': owner.name, 'type': owner.bl_rna.identifier, 'index_action': a.name, 'native_action': copied.name, 'slot_identifier': ad.action_slot.identifier, 'slot_handle': ad.action_slot.handle, 'preserved_inactive_nla': stash})
    for a in old_actions:
        if action_digest(a) != old_hashes[a.as_pointer()]:
            raise Failure('VALIDATION_FAILED', 'Index checkpoint Action changed')
    scene = bpy.context.scene
    scene.render.fps = clock['fps']
    scene.render.fps_base = 1.0
    scene.frame_start = math.floor(clock['native_frames'][0])
    scene.frame_end = math.ceil(clock['native_frames'][-1])
    return {'actions': action_records, 'bindings': bindings, 'curves': curve_total, 'keys_per_curve': n, 'clock_source': 'actual format seconds/KeyTime only', 'index_actions_preserved': True}

def import_index(path, kind, fps):
    clock = read_file_clock(path, kind, fps)
    import_frames = clock['file_frames']
    if kind == 'FBX':
        import_frames = clock['native_frames']
        ticks = clock['file_clock']['raw_times']
        ktime = clock['file_clock']['ktime_per_second']
        if [int(round(frame / fps * ktime)) for frame in import_frames] != ticks:
            refuse('float32 native times do not exactly re-encode the actual FBX ticks')
    temporary = core._import_format_index_domain(path, kind, import_frames, fps)
    return (clock, temporary)

def import_native_time(path, kind, fps):
    clock, temporary = import_index(path, kind, fps)
    actions = restore_native_actions(clock)
    return {'clock': clock, 'actions': actions, 'temporary_import': temporary}
