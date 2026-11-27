# SPDX-License-Identifier: GPL-3.0-or-later
"""Background-only skin/weight/bone evidence with a first-image review gate.

Call from an isolated saved-file worker. Never pose the live user session.
This is observation, not a classifier of anatomical correctness.
"""
import csv
import hashlib
import json
import math
from pathlib import Path
import time

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from bpy_extras.object_utils import world_to_camera_view

from . import corrective_authoring as sig, skinning_job
from .weight_repair import _require, _dense, mesh_fingerprint, _curve, _animation
from . import skinning_policy as policy


def _write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _durable(value):
    if isinstance(value, dict):
        return {key: _durable(item) for key, item in value.items() if key != 'uid'}
    if isinstance(value, list):
        return [_durable(item) for item in value]
    return value


def _pose(rig, pose, baseline):
    # Reset only channels changed by this explicit diagnostic specification.
    for name, state in baseline.items():
        bone = rig.pose.bones[name]
        for field, value in state['channels'].items():
            setattr(bone, field, value)
        for prop, value in state['props'].items():
            bone[prop] = value
    bpy.context.scene.frame_set(pose['frame'], subframe=pose['subframe'])
    for name, channels in pose.get('controls', {}).items():
        bone = rig.pose.bones[name]
        for field, value in channels.items():
            _require(field in {'rotation_euler', 'rotation_quaternion', 'location', 'scale', 'rotation_mode'},
                     '诊断只接受明确控制通道')
            setattr(bone, field, value)
    for name, values in pose.get('properties', {}).items():
        for key, value in values.items():
            _require(key in rig.pose.bones[name] and type(value) in (bool, int, float) and math.isfinite(value),
                     '诊断属性需要已有有限标量')
            rig.pose.bones[name][key] = value
    bpy.context.view_layer.update()


def _color(values):
    # Fixed 0..1 blue/cyan/green/yellow/red. Black means absent/zero.
    x = np.clip(np.asarray(values, dtype=float), 0, 1)
    stops = np.array([[0, 0, 1], [0, 1, 1], [0, 1, 0], [1, 1, 0], [1, 0, 0]], dtype=float)
    position = x * 4
    lower = np.minimum(position.astype(int), 3)
    blend = (position - lower)[:, None]
    rgb = (1 - blend) * stops[lower] + blend * stops[lower + 1]
    rgb[x == 0] = 0
    return np.column_stack((rgb, np.ones(len(x)))).astype(np.float32)


def _set_color(obj, values):
    mesh = obj.data
    attr = mesh.color_attributes.get('WeightRepairEvidence')
    if attr is None:
        attr = mesh.color_attributes.new(name='WeightRepairEvidence', type='FLOAT_COLOR', domain='POINT')
    attr.data.foreach_set('color', _color(values).ravel())
    mesh.color_attributes.active_color = attr


def _set_delta(obj, values, limit):
    attr = obj.data.color_attributes.get('WeightRepairEvidence')
    if attr is None:
        attr = obj.data.color_attributes.new(name='WeightRepairEvidence', type='FLOAT_COLOR', domain='POINT')
    value = np.clip(np.asarray(values) / limit, -1, 1)
    colors = np.ones((len(value), 4), dtype=np.float32)
    colors[:, 0] -= np.maximum(-value, 0)
    colors[:, 2] -= np.maximum(value, 0)
    colors[:, 1] -= abs(value)
    attr.data.foreach_set('color', colors.ravel())
    obj.data.color_attributes.active_color = attr


def _bone_record(rig, layers):
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    rows = {}
    for role, names in layers.items():
        for name in names:
            _require(name in evaluated.pose.bones, '骨视图包含未知骨：' + name)
            bone = evaluated.pose.bones[name]
            if role == 'DEFORM':
                _require(bone.bone.use_deform, 'DEFORM视图含非变形骨')
            segments = int(bone.bone.bbone_segments)
            points = [list(evaluated.matrix_world @ bone.head), list(evaluated.matrix_world @ bone.tail)]
            if segments > 1:
                points = [list(evaluated.matrix_world @ bone.matrix @
                    bone.bbone_segment_matrix(i, rest=False).translation) for i in range(segments + 1)]
            rows[name] = {'role': role, 'use_deform': bone.bone.use_deform,
                'world_matrix': [list(row) for row in evaluated.matrix_world @ bone.matrix],
                'basis': [list(row) for row in rig.pose.bones[name].matrix_basis],
                'rest': [list(row) for row in bone.bone.matrix_local], 'bbone_segments': segments,
                'points': points, 'constraints': [{'rna': sig._rna(c),
                    'targets': [sig._rna(t) for t in getattr(c, 'targets', ())]}
                    for c in rig.pose.bones[name].constraints]}
    return rows


def _camera(scene, label, direction, center, extent, resolution, distance):
    data = bpy.data.cameras.new('权重诊断相机_' + label)
    obj = bpy.data.objects.new(data.name, data)
    scene.collection.objects.link(obj)
    obj.location = Vector(center) + Vector(direction).normalized() * max(extent * 3, distance)
    obj.rotation_euler = (Vector(center) - obj.location).to_track_quat('-Z', 'Y').to_euler()
    data.type = 'ORTHO'
    data.ortho_scale = max(extent * 1.4, 1e-4)
    data.clip_start = max(extent * .001, 1e-6)
    data.clip_end = max(distance * 4, extent * 100, 10)
    scene.camera = obj
    bpy.context.view_layer.update()
    dep = bpy.context.evaluated_depsgraph_get()
    actual = obj.evaluated_get(dep)
    return obj, {'name': obj.name, 'resolution': [resolution, resolution],
        'clip_start': data.clip_start, 'clip_end': data.clip_end,
        'matrix_world': [list(row) for row in actual.matrix_world],
        'projection': [list(row) for row in actual.calc_matrix_camera(dep, x=resolution, y=resolution)]}


def _render(scene, visible, path):
    visible = set(visible) if isinstance(visible, (list, tuple)) else {visible}
    for obj in scene.objects:
        if obj.type == 'MESH':
            obj.hide_render = obj not in visible
    scene.render.filepath = str(path)
    started = time.perf_counter()
    bpy.ops.render.render(write_still=True)
    _require(Path(path).is_file() and Path(path).stat().st_size > 0, '实际渲染没有产生有效文件')
    return time.perf_counter() - started


def _bone_mesh(scene, rows, role, radius):
    vertices, edges = [], []
    for row in rows.values():
        if row['role'] != role:
            continue
        for start, end in zip(row['points'], row['points'][1:]):
            a, b = Vector(start), Vector(end)
            delta = b - a
            if delta.length < 1e-12:
                continue
            u = delta.normalized().cross(Vector((0, 0, 1)))
            if u.length < .01:
                u = delta.normalized().cross(Vector((0, 1, 0)))
            u.normalize()
            v = delta.normalized().cross(u)
            k = len(vertices)
            vertices.extend([tuple(a + radius * u), tuple(a + radius * v),
                tuple(a - radius * u), tuple(a - radius * v), tuple(b)])
            edges.extend([(k, k+1, k+4), (k+1, k+2, k+4), (k+2, k+3, k+4), (k+3, k, k+4),
                          (k+3, k+2, k+1, k)])
    mesh = bpy.data.meshes.new('诊断骨层_' + role)
    mesh.from_pydata(vertices, [], edges)
    obj = bpy.data.objects.new(mesh.name, mesh)
    scene.collection.objects.link(obj)
    obj.color = {'CONTROL': (.1, .7, 1, 1), 'MECHANISM': (1, .6, .1, 1),
                 'DEFORM': (.3, 1, .2, 1)}.get(role, (.8, .3, .8, 1))
    return obj


def compare(source, candidate, rig, *, vertex_indices, bone_names, bone_layers,
            poses, output_directory, resolution=720, phase='FIRST', reviewed_gate=None,
            reviewed_receipt=None, views=None, delta_limit=.05):
    """Export a trusted first group, then FULL only after its explicit review.

    reviewed_gate is the digest from FIRST's receipt, entered after viewing it.
    No automatic judgment of occlusion or beauty is made.
    """
    _require(bpy.app.background, '自动采姿只在独立后台工作副本运行，保护当前操作现场')
    _require(phase in {'SURVEY', 'FIRST', 'FULL'} and type(resolution) is int and 128 <= resolution <= 2048,
             '诊断阶段/分辨率无效')
    _require(type(delta_limit) in (int, float) and math.isfinite(delta_limit) and delta_limit > 0,
             '差值色阶需要明确有限正数')
    root = Path(output_directory)
    _require(root.is_absolute() and not root.exists(), '需要全新绝对证据目录，不覆盖已有结果')
    root.mkdir(parents=True)
    _require(source.type == candidate.type == 'MESH' and mesh_fingerprint(source)['geometry'] ==
             mesh_fingerprint(candidate)['geometry'], '对照需要相同RAW几何/拓扑')
    policy._bound(source, rig)
    policy._bound(candidate, rig)
    _require(np.array_equal(np.asarray(source.matrix_world), np.asarray(candidate.matrix_world))
        and [sig._rna(m) for m in source.modifiers] == [sig._rna(m) for m in candidate.modifiers]
        and policy._shape_state(source) == policy._shape_state(candidate),
        '权重对照必须共享同一rig、相同栈/Key输入/变换，拒绝混杂因素')
    _require(poses and bone_names and bone_layers, '必须明确姿态、相关权重骨与实际骨层')
    _require(all(role in {'CONTROL', 'MECHANISM', 'DEFORM', 'OTHER'} for role in bone_layers), '骨层角色无效')
    poses = [dict(pose, frame=pose.get('frame', bpy.context.scene.frame_current),
        subframe=pose.get('subframe', bpy.context.scene.frame_subframe)) for pose in poses]
    ids = np.asarray(vertex_indices)
    _require(ids.ndim == 1 and ids.dtype.kind in 'iu' and len(ids) >= 3 and
        (ids >= 0).all() and (ids < len(source.data.vertices)).all(), '观察域需要至少3个真实RAW点')
    _require(set(bone_names) <= {g.name for g in source.vertex_groups} &
             {g.name for g in candidate.vertex_groups}, '相关权重组必须两侧都存在')
    _require(all(rig.data.bones.get(n) and rig.data.bones[n].use_deform for n in bone_names),
             '逐骨权重列必须是实际变形骨')
    baseline = {}
    for pose in poses:
        for name in set(pose.get('controls', {})) | set(pose.get('properties', {})):
            _require(name in rig.pose.bones, '采姿控制骨不存在')
            bone = rig.pose.bones[name]
            if name not in baseline:
                baseline[name] = {'channels': {field: (getattr(bone, field) if field == 'rotation_mode'
                    else list(getattr(bone, field))) for field in ('rotation_mode', 'location', 'scale',
                        'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle')}, 'props': {}}
            for prop in pose.get('properties', {}).get(name, {}):
                _require(prop in bone, '采姿属性不存在')
                baseline[name]['props'][prop] = bone[prop]
    all_names = [g.name for g in source.vertex_groups]
    _require(all_names == [g.name for g in candidate.vertex_groups], '全组选点表要求相同组顺序')
    full_before, full_after = _dense(source, all_names), _dense(candidate, all_names)
    columns = [all_names.index(name) for name in bone_names]
    weights = {'before': full_before[:, columns], 'after': full_after[:, columns]}
    captures, extents = [], []
    for pose in poses:
        _pose(rig, pose, baseline)
        before, after = skinning_job._points(source), skinning_job._points(candidate)
        _require(before.shape == after.shape and np.isfinite(before).all() and np.isfinite(after).all(),
                 '姿态求值不是有限同拓扑点场')
        _require(before.shape == (len(source.data.vertices), 3),
                 '当前自动RAW域诊断不支持求值后拓扑变化；请在独立诊断副本建立明确同拓扑输入')
        extents.extend([before[ids], after[ids]])
        rows = _bone_record(rig, bone_layers)
        captures.append({'label': pose['label'], 'frame': [bpy.context.scene.frame_current,
            bpy.context.scene.frame_subframe], 'before': before, 'after': after,
            'bones': rows, 'rig_drivers': sig._drivers(rig)})
    cloud = np.concatenate(extents)
    center = (cloud.min(axis=0) + cloud.max(axis=0)) * .5
    extent = float(np.linalg.norm(cloud.max(axis=0) - cloud.min(axis=0)))
    views = views or {'front': (0, -1, .12), 'side': (1, 0, .12), 'three_quarter': (1, -2, .6)}
    _require(all(isinstance(label, str) and label.replace('_', '').replace('-', '').isalnum()
                 for label in views), '视角标签必须是安全的文件名标识')
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.resolution_x = scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.film_transparent = False
    if scene.world is None:
        scene.world = bpy.data.worlds.new('WeightRepair_ObservationWorld')
    scene.world.color = (.08, .08, .08)
    shading = scene.display.shading
    shading.light = 'STUDIO'
    shading.color_type = 'OBJECT'
    shading.show_shadows = True
    shading.show_cavity = True
    shading.cavity_type = 'BOTH'
    shading.show_object_outline = True
    source.color = candidate.color = (.66, .66, .66, 1)
    scene.view_settings.view_transform = 'Standard'
    raw_mesh = bpy.data.meshes.new('WeightRepair_RAW_Observation')
    raw_mesh.from_pydata([tuple(v.co) for v in source.data.vertices], [],
                        [tuple(p.vertices) for p in source.data.polygons])
    raw = bpy.data.objects.new(raw_mesh.name, raw_mesh)
    scene.collection.objects.link(raw)
    raw.matrix_world = source.matrix_world
    raw.color = (.66, .66, .66, 1)
    raw_world = np.asarray([tuple(source.matrix_world @ v.co) for v in source.data.vertices])
    roi = set(ids.tolist())
    local_edges = [tuple(e.vertices) for e in source.data.edges if set(e.vertices) <= roi]
    raw_rows = {str(i): {'role': 'OTHER', 'points': [raw_world[a].tolist(), raw_world[b].tolist()]}
                for i, (a, b) in enumerate(local_edges)}
    raw_wire = _bone_mesh(scene, raw_rows, 'OTHER', max(extent * .0006, 1e-6))
    raw_wire.color = (.05, .05, .05, 1)
    cameras = {}
    whole = np.concatenate([capture[side] for capture in captures for side in ('before', 'after')])
    distance = float(np.linalg.norm(whole.max(axis=0) - whole.min(axis=0))) * 2 + extent
    source.data.calc_loop_triangles()
    triangles = np.asarray([tuple(t.vertices) for t in source.data.loop_triangles], dtype=int)
    roi_triangles = np.asarray([i for i, tri in enumerate(triangles) if set(tri.tolist()) <= roi], dtype=int)
    for label, direction in views.items():
        camera, state = _camera(scene, label, direction, center, extent, resolution, distance)
        projected = np.array([world_to_camera_view(scene, camera, Vector(point))[:] for point in cloud])
        lo, hi = projected[:, :2].min(axis=0), projected[:, :2].max(axis=0)
        _require((lo >= .02).all() and (hi <= .98).all() and
                 (projected[:, 2] > camera.data.clip_start).all() and (projected[:, 2] < camera.data.clip_end).all(),
                 '实际相机投影没有完整覆盖观察域及姿态；停止铺图')
        _require(float(np.prod((hi - lo) * resolution)) >= 256,
                 '观察域实际像素尺寸不足，停止铺图')
        state['projected_roi_bounds'] = [lo.tolist(), hi.tolist()]
        state['occlusion'] = []
        ray_direction = -(camera.matrix_world.to_3x3().col[2]).normalized()
        ray_length = max(distance * 4, 1)
        for capture in captures:
            for side in ('before', 'after'):
                points = capture[side]
                tree = BVHTree.FromPolygons(points.tolist(), triangles.tolist(), all_triangles=True)
                visible = 0
                tolerance = max(extent * 1e-5, 32 * np.finfo(np.float32).eps * max(float(abs(points).max()), extent))
                for tri_index in roi_triangles:
                    center_point = Vector(points[triangles[tri_index]].mean(axis=0))
                    origin = center_point - ray_direction * ray_length
                    hit, _, index, _ = tree.ray_cast(origin, ray_direction, ray_length + tolerance)
                    if hit is not None and (index == int(tri_index) or (hit - center_point).length <= tolerance):
                        visible += 1
                state['occlusion'].append({'pose': capture['label'], 'side': side,
                    'complete_roi_triangles': len(roi_triangles), 'visible_centroid_samples': visible,
                    'scope': 'discrete triangle centroid rays; not complete visibility proof'})
        cameras[label] = (camera, state)
    _write(root / 'visibility-check.json', {key: state for key, (_, state) in cameras.items()})
    if phase == 'SURVEY':
        receipt = {'schema': 'rigging.weight-repair.evidence.v1', 'phase': phase, 'gate': None,
            'status': 'visibility_survey_only', 'images': [], 'reused_images': [],
            'cameras': {key: state for key, (_, state) in cameras.items()}}
        _write(root / 'receipt.json', receipt)
        return receipt
    if len(roi_triangles):
        first_rows = cameras[next(iter(cameras))][1]['occlusion'][:2]
        _require(all(row['visible_centroid_samples'] > 0 for row in first_rows),
                 '首视角未见首姿态观察域；请按visibility-check.json选择实际可见视角')
        for pose in poses:
            for side in ('before', 'after'):
                _require(any(row['pose'] == pose['label'] and row['side'] == side and
                    row['visible_centroid_samples'] > 0 for _, state in cameras.values() for row in state['occlusion']),
                    '现有视角均未见某姿态观察域：' + pose['label'] + '/' + side)
    bone_cloud = np.asarray([point for capture in captures for row in capture['bones'].values()
                             for point in row['points']], dtype=float)
    bone_center = (bone_cloud.min(axis=0) + bone_cloud.max(axis=0)) * .5
    bone_extent = float(np.linalg.norm(bone_cloud.max(axis=0) - bone_cloud.min(axis=0)))
    overview_camera, overview_state = _camera(scene, 'bone_overview', (1, -2, .6),
        bone_center, bone_extent, resolution, distance)
    input_state = {'source': mesh_fingerprint(source), 'candidate': mesh_fingerprint(candidate),
        'weights': {'before': hashlib.sha256(full_before.tobytes()).hexdigest(),
                    'after': hashlib.sha256(full_after.tobytes()).hexdigest()},
        'all_group_names': all_names,
        'locks': [[g.lock_weight for g in obj.vertex_groups] for obj in (source, candidate)],
        'surface': [sig._mesh_signature(obj.data) for obj in (source, candidate)],
        'keys': [policy._shape_state(obj) for obj in (source, candidate)],
        'modifiers': [[sig._rna(m) for m in obj.modifiers] for obj in (source, candidate)],
        'animations': [_durable(_animation(owner)) for owner in (rig, rig.data)],
        'evaluated_pose_hashes': [[hashlib.sha256(capture[side].tobytes()).hexdigest()
                                  for side in ('before', 'after')] for capture in captures],
        'evaluated_bones': [capture['bones'] for capture in captures],
        'poses': poses, 'roi': ids.tolist(), 'bones': bone_names, 'layers': bone_layers,
        'cameras': {k: v[1] for k, v in cameras.items()}, 'resolution': resolution,
        'delta_limit': delta_limit}
    input_state.update(bone_overview_camera=overview_state, blender_version=bpy.app.version_string,
        blender_build=bpy.app.build_hash.decode('utf-8'), engine=scene.render.engine,
        shading=sig._rna(shading), view_settings=sig._rna(scene.view_settings))
    loaded = Path(bpy.data.filepath) if bpy.data.filepath else None
    input_state['input_file'] = {'path': str(loaded), 'sha256': hashlib.sha256(loaded.read_bytes()).hexdigest()} \
        if loaded and loaded.is_file() else None
    gate = hashlib.sha256(json.dumps(input_state, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    _require(phase == 'FIRST' or reviewed_gate == gate,
             '完整批量诊断须先查看第一组真实图并提供同一输入观察门')
    reused = []
    if phase == 'FULL':
        _require(reviewed_receipt is not None and Path(reviewed_receipt).is_file(),
                 '完整诊断需要已核首组回执以复用有效图像')
        reviewed = json.loads(Path(reviewed_receipt).read_text(encoding='utf-8'))
        _require(reviewed.get('gate') == gate and reviewed.get('phase') == 'FIRST', '首组回执与当前输入不匹配')
        for path, digest in reviewed['image_sha256'].items():
            _require(Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest,
                     '已核首组图像已改变，停止复用')
            reused.append(path)
    receipt = {'schema': 'rigging.weight-repair.evidence.v1', 'phase': phase,
        'gate': gate, 'input': input_state, 'images': [], 'timing': [], 'reused_images': reused, 'skipped_views': [],
        'status': 'first_group_pending_review' if phase == 'FIRST' else 'captured_pending_visual_review',
        'technical_pass_is_not_quality_pass': True}
    selected_poses = list(enumerate(poses))[:1] if phase == 'FIRST' else list(enumerate(poses))
    selected_views = list(cameras)[:1] if phase == 'FIRST' else list(cameras)
    for index, pose in selected_poses:
        _pose(rig, pose, baseline)
        _require(np.array_equal(skinning_job._points(source), captures[index]['before']) and
                 np.array_equal(skinning_job._points(candidate), captures[index]['after']),
                 '实际出图姿态与预采求值不同；停止诊断，不接受混合证据')
        label = f'{index:02d}'
        if phase == 'FIRST' or index > 0:
            scene.camera = overview_camera
            shading.light, shading.color_type = 'STUDIO', 'OBJECT'
            path = root / f'{label}-overview-skin.png'
            receipt['timing'].append(_render(scene, source, path))
            receipt['images'].append(str(path))
            shading.light, shading.color_type = 'FLAT', 'OBJECT'
            for role in bone_layers:
                obj = _bone_mesh(scene, captures[index]['bones'], role, max(bone_extent * .005, 1e-5))
                path = root / f'{label}-overview-bones-{role}.png'
                receipt['timing'].append(_render(scene, obj, path))
                receipt['images'].append(str(path))
                mesh = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                bpy.data.meshes.remove(mesh)
        for view in selected_views:
            visibility = [row for row in cameras[view][1]['occlusion'] if row['pose'] == pose['label']]
            if visibility and not all(row['visible_centroid_samples'] > 0 for row in visibility):
                receipt['skipped_views'].append({'pose': pose['label'], 'view': view, 'reason': 'ROI centroid samples occluded'})
                continue
            if phase == 'FULL' and index == 0 and view == list(cameras)[0]:
                continue
            scene.camera = cameras[view][0]
            if index == 0:
                shading.light, shading.color_type = 'STUDIO', 'OBJECT'
                path = root / f'RAW-{view}.png'
                receipt['timing'].append(_render(scene, [raw, raw_wire], path))
                receipt['images'].append(str(path))
            for side, obj in (('before', source), ('after', candidate)):
                shading.light, shading.color_type = 'STUDIO', 'OBJECT'
                path = root / f'{label}-{view}-{side}-skin.png'
                receipt['timing'].append(_render(scene, obj, path))
                receipt['images'].append(str(path))
                shading.light, shading.color_type = 'FLAT', 'VERTEX'
                for column, name in enumerate(bone_names):
                    _set_color(obj, weights[side][:, column])
                    path = root / f'{label}-{view}-{side}-weight-{column:02d}.png'
                    receipt['timing'].append(_render(scene, obj, path))
                    receipt['images'].append(str(path))
                    if side == 'after':
                        _set_delta(obj, weights['after'][:, column] - weights['before'][:, column], delta_limit)
                        delta_path = root / f'{label}-{view}-delta-{column:02d}.png'
                        receipt['timing'].append(_render(scene, obj, delta_path))
                        receipt['images'].append(str(delta_path))
                _set_color(obj, np.clip(weights[side].sum(axis=1), 0, 1))
                path = root / f'{label}-{view}-{side}-related-sum.png'
                receipt['timing'].append(_render(scene, obj, path))
                receipt['images'].append(str(path))
            shading.light, shading.color_type = 'FLAT', 'OBJECT'
            for role in bone_layers:
                obj = _bone_mesh(scene, captures[index]['bones'], role, max(extent * .008, 1e-5))
                path = root / f'{label}-{view}-bones-{role}.png'
                receipt['timing'].append(_render(scene, obj, path))
                receipt['images'].append(str(path))
                mesh = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                bpy.data.meshes.remove(mesh)
        array_file = root / f'{label}-values.npz'
        np.savez_compressed(array_file, ids=ids, before=captures[index]['before'],
            after=captures[index]['after'], weights_before=weights['before'],
            weights_after=weights['after'], weight_delta=weights['after'] - weights['before'])
        _write(root / f'{label}-bones.json', {'bones': captures[index]['bones'],
            'frame': captures[index]['frame'], 'rig_drivers': captures[index]['rig_drivers']})
        with (root / f'{label}-selected-points.csv').open('x', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['vertex', 'group', 'before', 'after', 'delta', 'before_x', 'before_y', 'before_z',
                             'after_x', 'after_y', 'after_z'])
            for i in ids:
                for j, name in enumerate(all_names):
                    writer.writerow([int(i), name, full_before[i, j], full_after[i, j],
                        full_after[i, j] - full_before[i, j], *captures[index]['before'][i], *captures[index]['after'][i]])
    receipt['image_sha256'] = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in receipt['images']}
    _write(root / 'receipt.json', receipt)
    return receipt
