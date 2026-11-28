# SPDX-License-Identifier: GPL-3.0-or-later
# Canonical candidate relocation; see SOURCE-MAP.json.
import os
import math
from pathlib import Path
import bpy
from .io_support import Failure
UI_TYPES = {'Screen', 'WorkSpace', 'WindowManager'}

def path_value(raw, library=None):
    return os.path.normcase(os.path.normpath(bpy.path.abspath(raw, library=library)))

def identity(item):
    library = item.library
    return {'type': item.bl_rna.identifier, 'name': item.name, 'library': path_value(library.filepath) if library else None}

def all_ids():
    seen = set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type == 'COLLECTION':
            for item in getattr(bpy.data, prop.identifier, ()):
                if isinstance(item, bpy.types.ID) and item.as_pointer() not in seen:
                    seen.add(item.as_pointer())
                    if item.bl_rna.identifier not in UI_TYPES:
                        yield item

def value(raw):
    if isinstance(raw, bpy.types.ID):
        return identity(raw)
    if isinstance(raw, float) and (not math.isfinite(raw)):
        return {'non_finite': repr(raw)}
    if raw is None or isinstance(raw, (str, bool, int, float)):
        return raw
    if isinstance(raw, set):
        return sorted(raw)
    return [value(v) for v in raw]

def properties(item, exclude=()):
    result = {}
    for prop in item.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in {'rna_type', 'pixels', 'tag', *exclude}:
            continue
        if isinstance(item, bpy.types.Image) and name == 'file_format':
            continue
        if prop.type in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
            result[name] = value(getattr(item, name))
        elif prop.type == 'POINTER' and isinstance(getattr(item, name), bpy.types.ID):
            result[name] = identity(getattr(item, name))
    return result

def curve_content(curve):
    return {'path': curve.data_path, 'index': curve.array_index, 'extrapolation': curve.extrapolation, 'points': [{'co': value(p.co), 'left': value(p.handle_left), 'right': value(p.handle_right), 'interpolation': p.interpolation, 'left_type': p.handle_left_type, 'right_type': p.handle_right_type} for p in curve.keyframe_points], 'samples': [value(p.co) for p in curve.sampled_points], 'modifiers': [properties(m) for m in curve.modifiers]}
