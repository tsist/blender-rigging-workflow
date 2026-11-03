"""Observation helpers only; all pose mutations go through the adapted atomic core."""
import hashlib
import json
import bpy
import numpy as np
from .contract import require


def observe_faces(catalog):
    summaries, arrays = [], {}
    dg = bpy.context.evaluated_depsgraph_get()
    for index, owner in enumerate(catalog['face_owners']):
        obj = bpy.data.objects.get(owner['object'])
        require(obj is not None and obj.type == 'MESH' and obj.data.name == owner['data']
                and obj.data.shape_keys is not None and obj.data.shape_keys.name == owner['key'], 'FACE_OBSERVER_OWNER')
        values = {block.name: float(block.value) for block in obj.data.shape_keys.key_blocks}
        require(all(np.isfinite(v) for v in values.values()), 'FACE_KEY_VALUE_NONFINITE')
        evaluated = obj.evaluated_get(dg)
        mesh = evaluated.to_mesh()
        try:
            points = np.empty(len(mesh.vertices) * 3, dtype='<f4')
            mesh.vertices.foreach_get('co', points)
            points = points.reshape((-1, 3))
            matrix = np.asarray(evaluated.matrix_world, dtype='<f4')
            points = points @ matrix[:3, :3].T + matrix[:3, 3]
            require(bool(np.isfinite(points).all()), 'FACE_SKIN_NONFINITE', owner['object'])
            arrays['face_%02d' % index] = points.copy()
            summaries.append({'owner': {k: owner[k] for k in ('object', 'data', 'key')},
                              'array': 'face_%02d' % index, 'points': len(points),
                              'skin_world_sha256_le_f32': hashlib.sha256(points.tobytes()).hexdigest(),
                              'key_values': values,
                              'key_value_sha256_le_f32': hashlib.sha256(np.asarray(list(values.values()), dtype='<f4').tobytes()).hexdigest(),
                              'matrix_world': matrix.tolist()})
        finally:
            evaluated.to_mesh_clear()
    return summaries, arrays


def exact_faces(before, after, arrays_before, arrays_after):
    return (before == after and set(arrays_before) == set(arrays_after)
            and all(arrays_before[k].dtype == arrays_after[k].dtype
                    and arrays_before[k].shape == arrays_after[k].shape
                    and arrays_before[k].tobytes() == arrays_after[k].tobytes() for k in arrays_before))
