# SPDX-License-Identifier: GPL-3.0-or-later
"""Run only under Blender to generate disposable read-only CLI fixtures."""
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rigging_workflow.native_skeleton import attach_identity

work = Path(sys.argv[sys.argv.index('--') + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.unit_settings.system = 'METRIC'
data = bpy.data.armatures.new('RigData')
rig = bpy.data.objects.new('Rig', data)
scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
rig.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
for name, z in [('A', 0), ('B', 1)]:
    bone = data.edit_bones.new(name)
    bone.head, bone.tail = (0, 0, z), (0, 0, z + 1)
bpy.ops.object.mode_set(mode='OBJECT')
for name, weight in [('Target', 0.6), ('Donor', 0.3), ('Proxy', None)]:
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 1))
    obj = bpy.context.object
    obj.name = name
    if weight is not None:
        for b, w in [('A', weight), ('B', 1 - weight)]:
            obj.vertex_groups.new(name=b).add(list(range(8)), w, 'REPLACE')
        mod = obj.modifiers.new('Rig Binding', 'ARMATURE')
        mod.object = rig

base = work / 'adoption-source.blend'
source = work / 'source.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(base))
attach_identity(rig, base, hashlib.sha256(base.read_bytes()).hexdigest())
bpy.ops.wm.save_as_mainfile(filepath=str(source))
descriptor = {'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
common = {'source': descriptor, 'scene': scene.name, 'view_layer': bpy.context.view_layer.name,
          'target': 'Target', 'rig': 'Rig'}
policy = {**common, 'schema': 'rigging.skinning_policy_preflight.v1', 'donor': 'Donor'}
selected = {**common, 'schema': 'rigging.selected_skinning_inspect.v1', 'proxy': 'Proxy',
            'selected_bones': ['A'], 'context_bones': ['A', 'B'], 'settings': {},
            'candidate_name': 'Unused', 'vertex_indices': [0, 1]}
requests = {
    'inspect': selected, 'qualify': policy,
    'plan': {**policy, 'candidate_name': 'Unused', 'target_vertex_indices': [0, 1],
             'donor_vertex_indices': list(range(8)), 'editable_bone_names': ['A', 'B'],
             'alpha': [1.0, 1.0], 'max_distance_meters': 0.1},
}
for name, request in requests.items():
    (work / f'{name}.json').write_text(json.dumps(request), encoding='utf-8')

image = bpy.data.images.new('Unpacked test dependency', width=4, height=4)
image.use_fake_user = True
image.source = 'FILE'
image.filepath = '//unpacked-test-dependency.png'
external = work / 'external.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(external))
external_request = {**policy, 'source': {'path': str(external),
                    'sha256': hashlib.sha256(external.read_bytes()).hexdigest()}}
(work / 'external.json').write_text(json.dumps(external_request), encoding='utf-8')
