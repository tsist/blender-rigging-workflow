"""Small explicit session integration; no implicit state changes or file saving."""

from hrig_paths import project_path
from copy import deepcopy
import sys

from .contract import SCHEMA

DEFAULT_SOURCE = {
    'file':project_path('work/hrig-p2-v063/inputs/snow/extracted-v1/Snow/snow_v4.2.blend'),
    'expected_sha256':'80fd0a81bca90f0efdcdcb98422df0e198ae57f4a1c239e13fb795922779b1fc',
}


def create_snow_session(idle_guard, source_identity=None):
    """Caller has already opened the source safely with autoexec disabled.

    Only Python import search paths are extended. No mode/frame/selection,
    rig pose, object geometry, preferences or global scene values are changed.
    """
    for location in (project_path('tools/blenderctl'),
                     project_path('work/hrig-p2-v045/scripts')):
        if location not in sys.path:
            sys.path.insert(0,location)
    from .native import GarmentTransaction
    return GarmentTransaction(deepcopy(source_identity or DEFAULT_SOURCE),
        'GEO-snow-body','GEO-snow-shirt','RIG-Snow',idle_guard)


def snow_transfer_request(session, vertices, source_polygons, max_distance, *, tie_tolerance=1e-7, correspondence=None):
    """Build an explicit request, never infer a spatial region or distance budget.

    Identity group mapping is valid only for this Snow body/shirt/rig pair.
    Frozen deform groups are listed in allowed_groups to retain their source
    correspondence declaration, while the runtime freezes their target values.
    """
    deform = {bone.name for bone in session.rig.data.bones if bone.use_deform}
    targets = {group.name for group in session.target.vertex_groups}
    sources = {group.name for group in session.body.vertex_groups}
    names = sorted(targets & sources & deform)
    return {'schema':SCHEMA,'operation':'transfer',**session.names,'space':'REST_BASE',
            'vertices':list(vertices),'allowed_groups':names,'mapping':{name:name for name in names},
            'source_polygons':list(source_polygons),'max_distance':max_distance,'tie_tolerance':tie_tolerance,
            'correspondence':deepcopy(correspondence or []),
            'normalization':'preserve_fixed_deform_residual_to_one',
            'membership':'preserve_existing_append_positive'}


def local_delta_request(session, vertices, allowed_groups, deltas):
    return {'schema':SCHEMA,'operation':'local_delta',**session.names,'space':'REST_BASE',
            'vertices':list(vertices),'allowed_groups':list(allowed_groups),'deltas':deepcopy(deltas),
            'normalization':'preserve_fixed_deform_residual_to_one',
            'membership':'preserve_existing_append_positive'}
