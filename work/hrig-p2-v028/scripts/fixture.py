"""Independent native mechanism fixture; contains no user character data."""
import hashlib
import json

import bpy
from mathutils import Vector


def create():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    arm = bpy.data.armatures.new('FixtureData')
    rig = bpy.data.objects.new('FixtureRig', arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    for name, tail in [('Aim', (0, 0, 1)), ('Other', (0, 1, 0))]:
        bone = arm.edit_bones.new(name)
        bone.head, bone.tail = (0, 0, 0), tail
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh = bpy.data.meshes.new('TargetData')
    mesh.from_pydata([(1, 0, 0), (1, .2, .4), (1, -.2, .4)], [], [])
    target = bpy.data.objects.new('MeshTarget', mesh)
    bpy.context.scene.collection.objects.link(target)
    target.vertex_groups.new(name='ExistingGroup').add([0, 1, 2], 1, 'REPLACE')
    track = rig.pose.bones['Aim'].constraints.new('DAMPED_TRACK')
    track.name = 'ValidMeshGroup'
    track.target, track.subtarget = target, 'ExistingGroup'
    track.track_axis = 'TRACK_Y'
    missing = rig.pose.bones['Other'].constraints.new('COPY_LOCATION')
    missing.name = 'MissingMeshGroup'
    missing.target, missing.subtarget, missing.mute = target, 'AbsentGroup', True
    ik = rig.pose.bones['Other'].constraints.new('IK')
    ik.name, ik.chain_count, ik.mute = 'OptionalTargetlessIK', 1, True
    bone_target = rig.pose.bones['Other'].constraints.new('COPY_ROTATION')
    bone_target.name = 'ValidBoneTarget'
    bone_target.target, bone_target.subtarget = rig, 'Aim'
    rig['input'] = .25
    curve = rig.driver_add('location', 0)
    curve.driver.expression = 'v * 2'
    variable = curve.driver.variables.new()
    variable.name, variable.type = 'v', 'SINGLE_PROP'
    variable.targets[0].id = rig
    variable.targets[0].data_path = '["input"]'
    rig['unused'] = 0.0
    broken = rig.driver_add('["unused"]')
    broken.data_path = 'pose.bones["Other"].constraints["NoSuchConstraint"].influence'
    broken.driver.expression = '0.25'
    euler_curve = rig.pose.bones['Other'].driver_add('rotation_euler', 1)
    euler_curve.driver.expression = '0.1'
    archived = bpy.data.objects.new('ArchivedRig', bpy.data.armatures.new('ArchivedData'))
    # Not linked to the Scene: file inventory must differ from active inventory.
    bpy.context.scene.frame_set(1)
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = rig.evaluated_get(graph)
    direction = (evaluated.pose.bones['Aim'].matrix.to_3x3() @ Vector((0, 1, 0))).normalized()
    expected = (evaluated.matrix_world.inverted() @ (target.matrix_world @ Vector((1, 0, .8 / 3)))).normalized()
    dot = float(direction.dot(expected))
    if dot < .999:
        raise RuntimeError('Native Damped Track did not consume the mesh vertex-group target')
    if abs(evaluated.location.x - .5) > 1e-5:
        raise RuntimeError('Native simple-property driver failed')
    return rig, {'native_mesh_group_direction_dot': dot, 'native_property_value': float(evaluated.location.x), 'archived_object': archived.name}


def fingerprint():
    """Fixture-specific stored-content witness independent of collector fields."""
    records = {'frame': bpy.context.scene.frame_current, 'objects': [], 'ids': []}
    for obj in bpy.data.objects:
        row = {'name': obj.name, 'type': obj.type, 'parent': obj.parent.name if obj.parent else None,
               'use_fake_user': obj.use_fake_user,
               'matrix_basis': [list(r) for r in obj.matrix_basis], 'props': dict(obj.items()),
               'groups': [(g.index, g.name, g.lock_weight) for g in obj.vertex_groups]}
        if obj.type == 'ARMATURE':
            row['bones'] = [(b.name, b.parent.name if b.parent else None, list(b.head_local), list(b.tail_local), b.use_deform) for b in obj.data.bones]
            row['pose'] = [(b.name, [list(r) for r in b.matrix_basis], [(c.name, c.type, c.mute, c.influence, getattr(c, 'subtarget', None), getattr(c, 'target', None).name if getattr(c, 'target', None) else None) for c in b.constraints]) for b in (obj.pose.bones if obj.pose else [])]
        if obj.type == 'MESH':
            row['vertices'] = [(list(v.co), [(w.group, w.weight) for w in v.groups]) for v in obj.data.vertices]
        ad = obj.animation_data
        row['drivers'] = [] if ad is None else [(f.data_path, f.array_index, f.mute, f.driver.type, f.driver.expression, [(v.name, v.type, [(t.id.name if t.id else None, t.data_path, t.bone_target) for t in v.targets]) for v in f.driver.variables]) for f in ad.drivers]
        records['objects'].append(row)
    records['ids'] = [(p.identifier, len(getattr(bpy.data, p.identifier))) for p in bpy.data.bl_rna.properties if p.type == 'COLLECTION']
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate(report):
    if report['status'] != 'ok':
        raise RuntimeError('Collector did not finish fixture: ' + report['status'])
    rows = report['constraint_targets']
    def target(name, role='target'):
        matches = [r for r in rows if r['constraint'] == name and r['role'] == role]
        if len(matches) != 1:
            raise RuntimeError('Ambiguous/missing fixture target row: ' + name + '/' + role)
        return matches[0]
    checks = {
        'mesh_group_resolved': target('ValidMeshGroup')['resolution'] == 'resolved_vertex_group',
        'missing_group_diagnosed': target('MissingMeshGroup')['resolution'] == 'missing_vertex_group',
        'same_rig_bone_resolved': target('ValidBoneTarget')['resolution'] == 'resolved_bone',
        'targetless_ik_allowed': target('OptionalTargetlessIK')['resolution'] == 'optional_empty',
        'empty_pole_allowed': target('OptionalTargetlessIK', 'pole_target')['resolution'] == 'optional_empty',
        'main_rig_selected': report['context']['main_rig']['name'] == 'FixtureRig',
        'archive_not_active': any(r['object']['name'] == 'ArchivedRig' and not r['in_active_view_layer'] for r in report['rigs']),
        'stale_constraint_path_diagnosed': any(d['data_path'].endswith('constraints["NoSuchConstraint"].influence') and d['path']['resolution'] == 'unresolved_path' for d in report['drivers']['details']),
        'euler_component_is_array': any(d['data_path'] == 'pose.bones["Other"].rotation_euler' and d['array_index'] == 1 and d['path']['resolution'] == 'resolved_path' and d['path'].get('length') == 3 for d in report['drivers']['details']),
    }
    if not all(checks.values()):
        raise RuntimeError('Fixture invariant failed: ' + repr(checks))
    return checks
