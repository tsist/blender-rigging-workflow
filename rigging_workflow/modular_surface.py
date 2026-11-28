# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional native surface correction on a new, neutral modular skin copy.

This does not calculate weights, fix anatomy, or alter the source mesh. The
ordinary weighted skin remains the donor for later weight maintenance.
"""
import hashlib
import json
import uuid
import bpy
import numpy as np
from . import rig_identity, skeleton_gate, skinning, skinning_policy, corrective_authoring

KEY = 'rigging_modular_surface_v1'


def _evaluated_points(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        values = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get('co', values)
        if not len(values) or not np.isfinite(values).all():
            raise RuntimeError('原生平滑候选求值为空或非有限')
        return values.reshape((-1, 3))
    finally:
        evaluated.to_mesh_clear()


def create_candidate(source, rig, *, name, pin_boundary):
    """Return a separate Armature -> native Corrective Smooth -> Subsurf copy.

    The rig must already be in its recorded neutral pose. `pin_boundary` is
    explicit because raw generated meshes can have open seams. The recipe is
    fixed; broad tuning and weight edits belong to separate workflows.
    """
    skeleton_gate.require_pass(rig, operation='modular_surface.create_candidate')
    if not isinstance(name, str) or not name.strip() or name in bpy.data.objects:
        raise ValueError('请指定未使用的平滑候选名称')
    if type(pin_boundary) is not bool:
        raise ValueError('请明确是否固定原网格的开放边界')
    metadata = rig_identity.read(rig)
    if metadata.get('schema') != 'rigging.modular.generated.v1':
        raise ValueError('此候选入口仅用于独立模块化骨架')
    if metadata.get('configuration', {}).get('kind') != 'HUMAN':
        raise ValueError('当前平滑配方仅验证接纳人形；四足请保留原蒙皮')
    skinning_policy._bound(source, rig, donor=True)
    skinning_policy._rig(rig, metadata['deforms'])
    if KEY in source:
        raise ValueError('请从保留的原蒙皮创建新候选，不叠加已有平滑候选')
    before = corrective_authoring._state(source, rig)
    geometry = skinning.geometry_fingerprint(source)
    baseline = _evaluated_points(source)
    selected = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    candidate = None
    mesh = None
    try:
        candidate = source.copy()
        mesh = source.data.copy()
        candidate.data = mesh
        candidate.name = name
        bpy.context.scene.collection.objects.link(candidate)
        candidate.hide_set(False)
        modifier = candidate.modifiers.new('原生姿态修正平滑', 'CORRECTIVE_SMOOTH')
        candidate.modifiers.move(len(candidate.modifiers) - 1, 1)
        modifier.factor = .5
        modifier.iterations = 5
        modifier.scale = 1.
        modifier.smooth_type = 'SIMPLE'
        modifier.rest_source = 'BIND'
        modifier.use_only_smooth = False
        modifier.use_pin_boundary = pin_boundary
        modifier.vertex_group = ''
        modifier.invert_vertex_group = False
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        candidate.select_set(True)
        bpy.context.view_layer.objects.active = candidate
        bpy.context.view_layer.update()
        returned = bpy.ops.object.correctivesmooth_bind(modifier=modifier.name)
        if returned != {'FINISHED'} or not modifier.is_bind:
            raise RuntimeError('原生修正平滑未完成中立绑定，未保留候选')
        # Inspect the evaluated result without applying any native modifier.
        values = _evaluated_points(candidate)
        if values.shape != baseline.shape:
            raise RuntimeError('平滑候选改变了求值网格的顶点数量')
        world = np.asarray(candidate.matrix_world.to_3x3(), dtype=np.float64)
        neutral_error = float(np.linalg.norm((values - baseline) @ world.T, axis=1).max())
        neutral_error *= float(bpy.context.scene.unit_settings.scale_length)
        if neutral_error > 1e-5:
            raise RuntimeError('平滑候选未保持原中立外观')
        if (skinning.geometry_fingerprint(candidate) != geometry
                or corrective_authoring._state(source, rig) != before):
            raise RuntimeError('候选原几何或原角色保护检查失败')
        report = {'schema': 'rigging.modular.surface.v1', 'id': uuid.uuid4().hex,
            'method': 'native_corrective_smooth_after_armature',
            'source': source.name, 'candidate': candidate.name, 'rig': rig.name,
            'modifier': modifier.name, 'factor': .5, 'iterations': 5,
            'scale': 1., 'smooth_type': 'SIMPLE', 'rest_source': 'BIND',
            'pin_boundary': pin_boundary, 'bound': True,
            'neutral_error_meters': neutral_error,
            'source_state_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode('utf-8')).hexdigest(),
            'raw_geometry_preserved': True, 'source_unchanged': True,
            'weight_calculation_changed': False, 'artistic_approval': None,
            'recovery': 'disable the named native modifier or return to the retained source skin'}
        candidate[KEY] = json.dumps(report, ensure_ascii=False)
        return {'candidate': candidate, 'report': report}
    except Exception:
        if candidate is not None:
            bpy.data.objects.remove(candidate, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise
    finally:
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        for obj in selected:
            if obj.name in bpy.context.view_layer.objects:
                obj.select_set(True)
        bpy.context.view_layer.objects.active = active
