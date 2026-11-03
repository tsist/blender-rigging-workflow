"""Aon hair branches preserve all rest parents; six original WORLD boundaries."""
import re

BOUNDARIES=('根骨骼','机制-头部FK-跟随','头颅控制器','头颅','形变-头颅','骨架设置')

def bone_names(part):
    from . import front_hair_isolation as F,rear_hair_isolation as R
    assert part in ('front','rear')
    names=(F.bone_names() if part=='front' else R.bone_names())|set(BOUNDARIES)
    assert len(names)==(245 if part=='front' else 305)
    return names

def create(rig, mesh, scene, layer, part):
    import bpy
    names=bone_names(part)
    assert rig.type=='ARMATURE' and len(rig.data.bones)==1733
    assert names<=set(rig.pose.bones.keys())
    assert rig.library is None and rig.data.library is None and rig.mode=='OBJECT'
    assert mesh.type=='MESH' and mesh.modifiers[1].type=='ARMATURE' and mesh.modifiers[1].object==rig
    assert bpy.data.objects.get('HRIG_'+part+'_hair') is None and bpy.data.armatures.get('HRIG_'+part+'_hair_data') is None
    assert not rig.animation_data.nla_tracks
    for name in names:
        pb=rig.pose.bones[name];b=pb.bone
        assert b.bbone_segments==1 and b.bbone_custom_handle_start is None and b.bbone_custom_handle_end is None
        assert pb.custom_shape_transform is None or pb.custom_shape_transform.name in names
    old_active=layer.objects.active
    old_selected=list(bpy.context.selected_objects)
    clone=None;data=None
    try:
        clone=rig.copy();data=rig.data.copy();clone.data=data
        clone.name='HRIG_'+part+'_hair';data.name='HRIG_'+part+'_hair_data'
        scene.collection.objects.link(clone)
        assert clone.animation_data!=rig.animation_data
        assert all(a.as_pointer()!=b.as_pointer() for a,b in zip(clone.animation_data.drivers,rig.animation_data.drivers))
        clone.animation_data.action=None
        removed=[];kept=[]
        for f in list(clone.animation_data.drivers):
            match=re.match(r'^pose\.bones\["([^"]+)"\]',f.data_path)
            if match is None or match.group(1) not in names or match.group(1) in BOUNDARIES:
                removed.append([f.data_path,f.array_index]);clone.animation_data.drivers.remove(f)
            else:
                assert match.group(1) not in BOUNDARIES
                kept.append(f.data_path)
                for var in f.driver.variables:
                    for target in var.targets:
                        assert target.data_path=='pose.bones["骨架设置"]["头发FK-IK"]'
                        assert target.id in (rig,clone)
                        target.id=rig
        assert len(kept)==14
        for name in BOUNDARIES:
            p=clone.pose.bones[name]
            for con in list(p.constraints):p.constraints.remove(con)
            con=p.constraints.new('COPY_TRANSFORMS')
            con.name='HRIG original head boundary';con.target=rig;con.subtarget=name
            con.target_space='WORLD';con.owner_space='WORLD';con.mix_mode='REPLACE'
        for obj in old_selected:obj.select_set(False)
        clone.select_set(True);layer.objects.active=clone
        bpy.ops.object.mode_set(mode='EDIT')
        for b in list(data.edit_bones):
            if b.name not in names:data.edit_bones.remove(b)
        bpy.ops.object.mode_set(mode='OBJECT')
        assert set(data.bones.keys())==names
        for b in data.bones:
            original=rig.data.bones[b.name]
            assert b.matrix_local==original.matrix_local,('rest matrix changed',b.name)
            assert (b.parent.name if b.parent else None)==(original.parent.name if original.parent else None)
            assert b.bbone_custom_handle_start is None and b.bbone_custom_handle_end is None
            t=clone.pose.bones[b.name].custom_shape_transform
            assert t is None or (t.id_data==clone and t.name in names and t.as_pointer()==clone.pose.bones[t.name].as_pointer())
        assert data.users==1
        # Keep every retained source constraint. Object.copy remapping is verified,
        # not assumed; only the six declared boundary constraints may read orig.
        constraints=[]
        for p in clone.pose.bones:
            assert p.bone.bbone_segments==1
            for con in p.constraints:
                for field,sub in (('target','subtarget'),('pole_target','pole_subtarget'),('space_object','space_subtarget')):
                    target=getattr(con,field,None);subtarget=getattr(con,sub,'')
                    if target==clone:
                        assert subtarget and subtarget in names,(p.name,con.name,field,subtarget)
                    elif target==rig:
                        assert p.name in BOUNDARIES and con.name=='HRIG original head boundary'
                    elif target is not None:
                        assert target.name in ('千早爱音-前发物理','千早爱音-后发物理')
                assert con.type!='ARMATURE','unexpected multi-target constraint'
                constraints.append([p.name,con.name,con.type])
        for f in clone.animation_data.drivers:clone.path_resolve(f.data_path)
        for g in mesh.vertex_groups:
            if any(vg.group==g.index and vg.weight>0 for v in mesh.data.vertices for vg in v.groups):
                assert g.name in names and data.bones[g.name].use_deform,g.name
        mesh.modifiers[1].object=clone
        layer.update()
        e=clone.evaluated_get(bpy.context.evaluated_depsgraph_get())
        assert e.is_evaluated and e.original==clone
        assert all(f.driver.is_valid for f in e.animation_data.drivers)
        assert all(c.is_valid for p in e.pose.bones for c in p.constraints)
        return clone,data,{'bones':sorted(names),'bone_count':len(names),'retained_drivers':kept,
                           'removed_clone_driver_count':len(removed),'constraints':constraints,
                           'boundary_inputs':list(BOUNDARIES),'rest_matrices_exact':True,'source_properties':['头发FK-IK'],
                           'physics':'original proxies, original switches','original_bones_retained':1733}
    except Exception:
        if clone is not None and clone.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        mesh.modifiers[1].object=rig
        if clone is not None:bpy.data.objects.remove(clone,do_unlink=True)
        if data is not None:bpy.data.armatures.remove(data)
        raise
    finally:
        if bpy.context.mode=='OBJECT':
            for obj in bpy.context.selected_objects:obj.select_set(False)
            for obj in old_selected:obj.select_set(True)
            layer.objects.active=old_active
