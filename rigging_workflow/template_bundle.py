"""Private native template bundles; this is not public asset redistribution."""
import bpy
import json

C94_ARCHIVED_ACTION_KEY='HRIG_C1_SOURCE_ACTION_b6ebe6eb728247e58f78868702d3f006'


def duplicate_widgets(rig, source, collection, *, owned_ids=None):
    """Copy exact C94 display geometry and remap source parent/driver pointers.

    No new widget artwork is claimed. Unknown widget evaluation inputs are
    rejected so the source production rig cannot enter the library silently.
    """
    from .module_configuration import _own
    if owned_ids is not None and not isinstance(owned_ids, list):
        raise ValueError('owned_ids须为调用者的可写list或None')
    mapping={}
    for pose in rig.pose.bones:
        widget=pose.custom_shape
        if widget is None or widget in mapping:
            continue
        if widget.parent and widget.parent!=source:
            raise ValueError('Unsupported widget parent: '+widget.name)
        if widget.constraints or widget.modifiers:
            raise ValueError('Widget evaluation stack requires review: '+widget.name)
        if widget.animation_data or (widget.data and widget.data.animation_data):
            raise ValueError('Animated widget requires review: '+widget.name)
        copied=_own(owned_ids,widget.copy())
        if widget.data:copied.data=_own(owned_ids,widget.data.copy())
        copied.name=rig.name+'__控件__'+widget.name
        copied.parent=rig if widget.parent==source else None
        collection.objects.link(copied)
        copied.hide_render=True
        copied.hide_set(True)
        mapping[widget]=copied
    for pose in rig.pose.bones:
        if pose.custom_shape:pose.custom_shape=mapping[pose.custom_shape]
    return {'widget_count':len(mapping),'widgets':[v.name for v in mapping.values()],
            'provenance':'private copies of source widgets; publication licence not established'}


def write_private_bundle(rig, source, collection, filepath, *, owned_ids=None):
    """Write a new collection/scene library without deleting the author scene."""
    from pathlib import Path
    from .module_configuration import _own
    if owned_ids is not None and not isinstance(owned_ids, list):
        raise ValueError('owned_ids须为调用者的可写list或None')
    destination=Path(filepath)
    if destination.exists():raise ValueError('Bundle destination already exists')
    archive=[]
    value=rig.get(C94_ARCHIVED_ACTION_KEY)
    if value is not None:
        if not isinstance(value,bpy.types.Action):
            raise ValueError('Unexpected archived action metadata type')
        # This exact source field is a historical restore pointer, not active
        # Action/NLA. Keep provenance as text without recursively packaging it.
        for owner in (rig,rig.data):
            ad=owner.animation_data
            if ad and any(C94_ARCHIVED_ACTION_KEY in t.data_path for f in ad.drivers for v in f.driver.variables for t in v.targets):
                raise ValueError('Archived action field has an active driver consumer')
        archive.append({'property':C94_ARCHIVED_ACTION_KEY,'action':value.name})
        del rig[C94_ARCHIVED_ACTION_KEY]
        rig['c94_source_archive_metadata']=json.dumps(archive,ensure_ascii=False)
    from .template import _metadata_ids
    remaining=list(_metadata_ids(rig,set(rig.data.bones.keys())))
    if remaining:
        raise ValueError('Unpackaged metadata ID references: '+str([(p,v.name) for p,v in remaining]))
    widgets=_own(owned_ids,bpy.data.collections.new(rig.name+'__控件'))
    collection.children.link(widgets)
    report=duplicate_widgets(rig,source,widgets,owned_ids=owned_ids)
    report['archive_pointer_converted_to_provenance']=archive
    collection['c94_private_template']=True
    collection['c94_template_rig']=rig.name
    collection['c94_template_limitations']='Private structural extraction only; no character fitting, skinning, new hair dynamics or final portable runtime.'
    # libraries.write recursively writes actual ID dependencies. A fresh reader
    # must still reject accidental source rigs/character meshes/Texts/Actions.
    bpy.data.libraries.write(str(destination),{collection},path_remap='RELATIVE',fake_user=False,compress=True)
    report.update({'file':str(destination),'rig':rig.name,'collection':collection.name,'format':'native Collection library; append into a scene'})
    return report
