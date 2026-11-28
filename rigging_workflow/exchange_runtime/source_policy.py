# SPDX-License-Identifier: GPL-3.0-or-later
# Canonical candidate relocation; see SOURCE-MAP.json.
import bpy
from .io_support import Failure

def animation_safe(profile=None, source=None, job=None):
    from . import driver_policy as drivers
    if profile is not None:
        drivers.check(profile, source, job)
    for item, _ in drivers.owners():
        ad = getattr(item, 'animation_data', None)
        if ad and ad.use_tweak_mode:
            raise Failure('UNSUPPORTED', 'NLA tweak mode unsupported')
        if profile is None and ad and any((c.driver.type == 'SCRIPTED' for c in ad.drivers)):
            raise Failure('UNSUPPORTED', 'Scripted drivers require a separate trusted execution workflow')

def safe_source(profile=None, source=None, job=None):
    from .source_policy import animation_safe
    animation_safe(profile, source, job)
    if bpy.data.libraries:
        raise Failure('UNSUPPORTED', 'Exchange requires local data')
    for s in bpy.data.scenes:
        if s.rigidbody_world:
            raise Failure('UNSUPPORTED', 'Freeze simulation geometry through a dedicated adapter first')
    for o in bpy.data.objects:
        if o.particle_systems:
            raise Failure('UNSUPPORTED', 'Particles need an explicit conversion adapter')
        if any((m.type in ('CLOTH', 'SOFT_BODY', 'FLUID', 'DYNAMIC_PAINT', 'MESH_CACHE', 'MESH_SEQUENCE_CACHE') for m in o.modifiers)):
            raise Failure('UNSUPPORTED', 'Unmanaged cache modifier in export source')
    for t in bpy.data.node_groups:
        if any((n.bl_idname in ('GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput', 'GeometryNodeBake') for n in t.nodes)):
            raise Failure('UNSUPPORTED', 'Node simulation needs a frozen geometry adapter')
