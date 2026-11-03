"""Native character candidates with reversible runtime orphan retention.

The route preserves declared non-UI character IDs only after an independent
cold reader proves the recorded content. It does not certify the GUI layout.
"""
import hashlib
from pathlib import Path

import bpy

UI_TYPES={'Screen','WorkSpace','WindowManager'}


def character_ids():
    answer=[]
    seen=set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type!='COLLECTION':continue
        for item in getattr(bpy.data,prop.identifier,()):
            if not isinstance(item,bpy.types.ID) or item.as_pointer() in seen:continue
            seen.add(item.as_pointer())
            if item.bl_rna.identifier in UI_TYPES or item.library or getattr(item,'is_embedded_data',False):continue
            answer.append(item)
    return answer


def save_candidate(path,job_directory):
    path=Path(path)
    job=Path(job_directory).resolve(strict=True)
    if not path.is_absolute() or path.resolve().parent!=job or path.exists():
        raise ValueError('Candidate must be a new file directly inside its owned job')
    items=character_ids()
    if tuple(bpy.app.version)!=(5,2,1) or bpy.app.build_hash.decode()!='9e2066aef7ef':
        raise ValueError('Runtime-retained native save requires the verified Blender build')
    before={(i.bl_rna.identifier,i.name):(i.use_fake_user,i.use_extra_user,i.users) for i in items}
    retained=[]
    restoration_errors=[]
    try:
        for item in items:
            if item.users==0:
                if item.use_fake_user or item.use_extra_user:
                    raise ValueError('Unexpected zero-user accounting')
                retained.append(item)
                item.use_extra_user=True
        outcome=bpy.ops.wm.save_as_mainfile(filepath=str(path),copy=True,check_existing=True,
                                           relative_remap=True,compress=True)
        if outcome!={'FINISHED'}:raise RuntimeError('Native candidate save did not finish')
    finally:
        for item in reversed(retained):
            try:item.use_extra_user=False
            except Exception as error:restoration_errors.append(str(error))
        if restoration_errors:raise RuntimeError('Runtime retention restoration failed: '+str(restoration_errors))
    if {(i.bl_rna.identifier,i.name):(i.use_fake_user,i.use_extra_user,i.users) for i in items}!=before:
        raise RuntimeError('Native writer did not restore original fake/extra/user accounting')
    return {'file':str(path),'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'route':'native_copy_runtime_extra_user_retention','path_remap':'native_relative_remap','fake_user_modified':False,
            'runtime_extra_user_temporarily_modified':[(i.bl_rna.identifier,i.name) for i in retained],
            'runtime_bookkeeping_restored':True,
            'declared_local_character_ids':len(items),
            'scope':'candidate only; separate cold required; UI/opaque runtime not certified'}
