"""Create complete native reference layers from actual template rest geometry."""
import bpy


def create_reference(source, collection, name):
    if not isinstance(source, bpy.types.Object) or source.type != 'ARMATURE':
        raise ValueError('Reference source must be an armature')
    if not isinstance(name, str) or not name.strip() or name in bpy.data.objects:
        raise ValueError('Reference name is empty or already exists: '+str(name))
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        raise ValueError('Reference creation requires Object Mode')
    if not isinstance(collection, bpy.types.Collection) or collection.library or collection.override_library:
        raise ValueError('Reference creation requires a local Collection')
    view = bpy.context.view_layer
    def available(layer):
        if layer.exclude or layer.hide_viewport or layer.collection.hide_viewport:
            return False
        if layer.collection == collection:
            return True
        return any(available(child) for child in layer.children)
    if not available(view.layer_collection):
        raise ValueError('Reference Collection is excluded or hidden in the current View Layer')
    old_active = view.objects.active
    old_selection = list(bpy.context.selected_objects)
    data, obj = None, None
    try:
        data = bpy.data.armatures.new(name)
        obj = bpy.data.objects.new(name, data)
        collection.objects.link(obj)
        obj.matrix_world = source.matrix_world.copy()
        obj.show_in_front = True
        obj.display_type = 'WIRE'
        obj['c94_reference_source'] = source.name
        obj['c94_reference_kind'] = 'complete_source_rest'
        view.update()
        if view.objects.get(obj.name) != obj or not obj.visible_get(view_layer=view):
            raise ValueError('Reference Collection became unavailable in the current View Layer')
        for selected in old_selection:
            selected.select_set(False)
        obj.select_set(True)
        view.objects.active = obj
        if not bpy.ops.object.mode_set.poll():
            raise ValueError('Current context cannot edit the new reference armature')
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            for b in source.data.bones:
                e = data.edit_bones.new(b.name)
                e.head = b.head_local
                e.tail = b.tail_local
                e.matrix = b.matrix_local
                e.length = b.length
                e.use_deform = False
            for b in source.data.bones:
                e = data.edit_bones[b.name]
                if b.parent:
                    e.parent = data.edit_bones[b.parent.name]
                e.use_connect = b.use_connect
        finally:
            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        for c in source.data.collections_all:
            new = data.collections.new(c.name)
            new.is_visible = c.is_visible
            if c.parent:
                new.parent = data.collections_all[c.parent.name]
            for b in c.bones:
                new.assign(data.bones[b.name])
        for b in data.bones:
            b['c94_source_bone'] = b.name
        return obj
    except Exception:
        if obj is not None:
            if bpy.context.view_layer.objects.active == obj and obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.armatures.remove(data)
        raise
    finally:
        for selected in list(bpy.context.selected_objects):
            selected.select_set(False)
        for selected in old_selection:
            try:
                if view.objects.get(selected.name) == selected:
                    selected.select_set(True)
            except ReferenceError:
                pass
        try:
            view.objects.active = old_active if old_active is not None and view.objects.get(old_active.name) == old_active else None
        except ReferenceError:
            view.objects.active = None


def verify_reference(source, reference):
    src = source.data.bones
    ref = reference.data.bones
    if set(src.keys()) != set(ref.keys()):
        raise ValueError('Reference bone membership changed')
    errors = []
    max_position_error = 0.0
    max_orientation_error = 0.0
    for b in src:
        r = ref[b.name]
        max_position_error = max(max_position_error, (b.head_local-r.head_local).length,
                                 (b.tail_local-r.tail_local).length)
        max_orientation_error = max(max_orientation_error, max(abs(b.matrix_local[i][j]-r.matrix_local[i][j]) for i in range(3) for j in range(3)))
        if (b.parent.name if b.parent else None)!=(r.parent.name if r.parent else None):
            errors.append({'bone':b.name, 'error':'parent'})
        if reference.pose.bones[b.name].constraints or r.use_deform:
            errors.append({'bone':b.name, 'error':'not_reference_only'})
    return {'source':source.name, 'reference':reference.name, 'count':len(ref),
            'max_position_error':max_position_error,'max_orientation_error':max_orientation_error,
            'errors':errors, 'scope':'rest geometry and hierarchy; no fitted character or generated rig'}
