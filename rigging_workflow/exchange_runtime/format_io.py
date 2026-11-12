# SPDX-License-Identifier: GPL-3.0-or-later
# Canonical candidate relocation; see SOURCE-MAP.json.
import json,struct
from pathlib import Path
import bpy
from mathutils import kdtree
from .io_support import Failure
from . import io_support as modeling
from .source_policy import safe_source
OPS = {'GLB': ('import_scene.gltf', 'export_scene.gltf'), 'FBX': ('import_scene.fbx', 'export_scene.fbx')}
SUFFIX = {'GLB': '.glb', 'FBX': '.fbx'}

def operator(name):
    try:
        op = bpy.ops
        for key in name.split('.'):
            op = getattr(op, key)
        op.get_rna_type()
        return op
    except Exception as ex:
        raise Failure('UNSUPPORTED', 'Required bundled operator is unavailable: ' + name) from ex

def compare_geometry(a, b):
    tolerance = 0.0001 + max((abs(x) for p in a['bounds'] for x in p)) * 1e-05

    def distance(x, y):
        tree = kdtree.KDTree(len(y))
        for i, p in enumerate(y):
            tree.insert(p, i)
        tree.balance()
        return max((tree.find(p)[2] for p in x))
    delta = max(distance(a['vertices'], b['vertices']), distance(b['vertices'], a['vertices']))
    area_delta = abs(a['area'] - b['area'])
    ok = delta <= tolerance and a['triangles'] == b['triangles'] and (area_delta <= max(1e-05, a['area'] * 0.0001))
    return {'ok': ok, 'bidirectional_vertex_distance': delta, 'position_tolerance': tolerance, 'triangle_counts': [a['triangles'], b['triangles']], 'area_delta': area_delta, 'scope': 'evaluated surface vertices, triangle count and area; not a proof of arbitrary topology or shader equivalence'}

def file_preflight(path, kind):
    if kind not in OPS:raise Failure('UNSUPPORTED','Only shared GLB/FBX import is packaged')
    if path.stat().st_size > 256 * 1024 * 1024:
        raise Failure('UNSUPPORTED', 'Exchange input exceeds 256 MiB')
    if kind == 'GLB':
        with path.open('rb') as f:
            magic, version, total = struct.unpack('<4sII', f.read(12))
            length, typ = struct.unpack('<I4s', f.read(8))
            if magic != b'glTF' or version != 2 or total != path.stat().st_size or (typ != b'JSON') or (length > total - 20):
                raise Failure('INVALID_REQUEST', 'Invalid GLB container')
            doc = json.loads(f.read(length))
        for item in [*doc.get('buffers', []), *doc.get('images', [])]:
            if 'uri' in item and (not item['uri'].startswith('data:')):
                raise Failure('UNSUPPORTED', 'Only embedded GLB resources are accepted')
        if doc.get('extensionsRequired'):
            raise Failure('UNSUPPORTED', 'Required glTF extensions need a dedicated adapter')
    elif kind == 'OBJ':
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '\\' in line or (line.split() and line.split()[0].lower() in ('mtllib', 'call', 'csh')):
                raise Failure('UNSUPPORTED', 'OBJ external materials/scripts/continuations are outside geometry-only import')
    elif kind == 'FBX':
        from io_scene_fbx import parse_fbx
        tree, version = parse_fbx.parse(str(path))

        def walk(e):
            if e.id in (b'Video', b'Texture'):
                raise Failure('UNSUPPORTED', 'FBX texture resources need a separate adapter; use embedded GLB PBR')
            for c in e.elems:
                walk(c)
        walk(tree)

def import_file(path, kind, mode='STATIC'):
    file_preflight(path, kind)
    op = operator(OPS[kind][0])
    kw = {'filepath': str(path)}
    if kind == 'GLB':
        kw.update(import_pack_images=True, import_shading='NORMALS', bone_heuristic='BLENDER', disable_bone_shape=True, import_scene_as_collection=False)
    elif kind == 'OBJ':
        kw.update(forward_axis='NEGATIVE_Z', up_axis='Y', validate_meshes=True)
    elif kind == 'STL':
        kw.update(forward_axis='Y', up_axis='Z', use_scene_unit=False)
    elif kind == 'PLY':
        kw.update(forward_axis='Y', up_axis='Z', merge_verts=False)
    elif kind == 'FBX':
        kw.update(use_image_search=False, use_anim=mode == 'ANIMATION', anim_offset=0, use_custom_props=False, automatic_bone_orientation=False)
    elif kind == 'USD':
        kw.update(import_textures_mode='IMPORT_NONE', import_materials=False, import_volumes=False, import_cameras=False, import_lights=False, import_skeletons=False, set_frame_range=False, apply_unit_conversion_scale=True)
    else:
        kw.update(as_background_job=False, set_frame_range=False, always_add_cache_reader=False)
    if kind == 'FBX':
        from .fbx_compat import stable_armature_traversal
        with stable_armature_traversal():
            modeling.finished(op(**kw))
    else:
        modeling.finished(op(**kw))

def save_candidate(job):
    p = job / 'exchange-candidate.blend'
    bpy.context.preferences.filepaths.save_version = 0
    modeling.finished(bpy.ops.wm.save_as_mainfile(filepath=str(p), relative_remap=False, check_existing=False))
    return p
