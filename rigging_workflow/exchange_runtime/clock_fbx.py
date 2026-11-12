# SPDX-License-Identifier: GPL-3.0-or-later
# Canonical candidate relocation; see SOURCE-MAP.json.
import math
import json
from pathlib import Path
from contextlib import contextmanager
import numpy as np
from .io_support import Failure
MARKER = b'BlenderCtlNativeClock'
MAX_BYTES = 1024 ** 3

def clock_scale(keys):
    values = np.asarray(keys, dtype=np.float64)
    if not 2 <= len(values) <= 1024 or not np.isfinite(values).all() or np.any(values < 1) or np.any(values > 100000) or np.any(values.astype(np.float32) != values) or np.any(np.diff(values) <= 0) or (not values[0].is_integer()) or (not values[-1].is_integer()):
        raise Failure('UNSUPPORTED', 'FBX clock requires 2..1024 ordered exact native-frame keys, integer endpoints')
    scale = 1
    while float(np.diff(values).min()) * scale < 1 / 64:
        scale *= 2
    if values[-1] * scale > 1048574:
        raise Failure('UNSUPPORTED', 'Required internal clock exceeds NLA action range')
    return scale

def clone_tree(tree, transform=lambda node, values, ancestors: values):
    from io_scene_fbx import encode_bin, data_types
    methods = {getattr(data_types, n): 'add_' + n.lower() for n in ('BOOL', 'CHAR', 'INT8', 'INT16', 'INT32', 'INT64', 'FLOAT32', 'FLOAT64', 'BYTES', 'STRING', 'INT32_ARRAY', 'INT64_ARRAY', 'FLOAT32_ARRAY', 'FLOAT64_ARRAY', 'BOOL_ARRAY', 'BYTE_ARRAY')}

    def visit(node, ancestors=()):
        values = transform(node, list(node.props), ancestors)
        out = encode_bin.FBXElem(node.id)
        for typ, value in zip(node.props_type, values):
            if typ not in methods:
                raise Failure('UNSUPPORTED', 'Unsupported FBX property type')
            getattr(out, methods[typ])(value)
        out.elems = [visit(c, ancestors + (node.id,)) for c in node.elems]
        return out
    with encode_bin.FBXElem.enable_multithreading_cm():
        return visit(tree)

def write_fbx(template, path, metadata):
    from io_scene_fbx import parse_fbx, encode_bin, fbx_utils
    if path.exists():
        raise Failure('CONFLICT', 'FBX clock output already exists')
    tree, version = parse_fbx.parse(str(template))
    if version != 7400:
        raise Failure('UNSUPPORTED', 'FBX clock writer is pinned to FBX 7400')
    keys = np.asarray(metadata['key_frames'])
    fps = metadata['fps']
    ticks = fbx_utils.FBX_KTIME_V7
    mapped = np.rint(keys / fps * ticks).astype(np.int64)
    if np.any(np.diff(mapped) <= 0) or not np.array_equal((mapped / ticks * fps).astype(np.float32), keys):
        raise Failure('UNSUPPORTED', 'FBX KTIME cannot round-trip native frame keys')
    stats = {'curves': 0, 'time_values': 0, 'max_time_error_frames': float(np.max(np.abs(mapped / ticks * fps - keys)))}

    def remap(node, values, ancestors):
        if node.id == b'KeyTime':
            times = np.asarray(values[0]) * fps / ticks
            indices = np.rint(times).astype(np.int64)
            if np.any(np.abs(times - indices) > 0.0001) or np.any(indices < 1) or np.any(indices > len(keys)):
                raise Failure('VALIDATION_FAILED', 'Unexpected FBX template key time')
            values = [mapped[indices - 1]]
            stats['curves'] += 1
            stats['time_values'] += len(indices)
        elif node.id == b'P' and values:
            name = values[0]
            if b'GlobalSettings' in ancestors and name in (b'TimeSpanStart', b'TimeSpanStop'):
                values[-1] = int(mapped[-1 if name.endswith(b'Stop') else 0])
            if b'AnimationStack' in ancestors and name in (b'LocalStart', b'LocalStop', b'ReferenceStart', b'ReferenceStop'):
                values[-1] = int(mapped[-1 if name.endswith(b'Stop') else 0])
        elif node.id in (b'LocalTime', b'ReferenceTime') and b'Takes' in ancestors:
            values = [int(mapped[0]), int(mapped[-1])]
        return values
    root = clone_tree(tree, remap)
    marker = encode_bin.FBXElem(MARKER)
    marker.add_string(json.dumps(metadata, separators=(',', ':'), allow_nan=False).encode())
    root.elems.append(marker)
    temp = path.with_suffix('.fbx.tmp')
    encode_bin.write(str(temp), root, version)
    temp.replace(path)
    if path.stat().st_size > MAX_BYTES:
        raise Failure('UNSUPPORTED', 'FBX clock exceeds 1 GiB')
    return stats

@contextmanager
def import_time_map(meta, index_domain=False):
    from io_scene_fbx import import_fbx, fbx_utils
    original = import_fbx._convert_fbx_time_to_blender_time
    keys = np.asarray(meta['key_frames'])
    ticks = np.rint(keys / meta['fps'] * fbx_utils.FBX_KTIME_V7).astype(np.int64)

    def convert(values, blen_start_offset, fbx_start_offset, fps, fbx_ktime):
        indices = np.searchsorted(ticks, values)
        if blen_start_offset != 0 or fbx_start_offset != 0 or fps != meta['fps'] or (fbx_ktime != fbx_utils.FBX_KTIME_V7) or np.any(indices >= len(ticks)) or (not np.array_equal(ticks[indices], values)):
            raise Failure('UNSUPPORTED', 'Importer received times outside explicit FBX clock map')
        return (indices + 1).astype(np.float64) if index_domain else keys[indices] * meta['clock_scale']
    import_fbx._convert_fbx_time_to_blender_time = convert
    try:
        yield
    finally:
        import_fbx._convert_fbx_time_to_blender_time = original
