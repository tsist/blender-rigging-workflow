"""Experimental Aon rear-hair slice. Original rig and physics proxies stay intact."""
import re

def bone_names():
    names={'头颅','形变-头颅','骨架设置','后发-IK-总控制器'}
    names.update(f'{prefix}后发-{i:02}' for prefix in ('','形变-','物理-') for i in range(1,83))
    names.update(f'{prefix}{side}侧发-{i:02}' for prefix in ('','形变-','物理-') for side in ('左','右') for i in range(1,5))
    names.update(f'后发-{i:02}-{suffix}' for i in range(1,13) for suffix in ('父级','IK目标'))
    names.update(f'{side}侧发-{suffix}' for side in ('左','右') for suffix in ('父级','IK目标'))
    assert len(names)==302
    return names

def create(rig, mesh, scene, layer):
    import bpy
    names=bone_names()
    assert rig.type=='ARMATURE' and len(rig.data.bones)==1733
    assert names<=set(rig.pose.bones.keys())
    assert rig.library is None and rig.data.library is None and rig.mode=='OBJECT'
    assert mesh.type=='MESH' and mesh.modifiers[1].type=='ARMATURE' and mesh.modifiers[1].object==rig
    assert bpy.data.objects.get('HRIG_rear_hair') is None and bpy.data.armatures.get('HRIG_rear_hair_data') is None
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
        clone.name='HRIG_rear_hair';data.name='HRIG_rear_hair_data'
        scene.collection.objects.link(clone)
        assert clone.animation_data!=rig.animation_data
        assert all(a.as_pointer()!=b.as_pointer() for a,b in zip(clone.animation_data.drivers,rig.animation_data.drivers))
        clone.animation_data.action=None
        removed=[];kept=[]
        for f in list(clone.animation_data.drivers):
            match=re.match(r'^pose\.bones\["([^"]+)"\]',f.data_path)
            if match is None or match.group(1) not in names or match.group(1) in ('头颅','形变-头颅','骨架设置'):
                removed.append([f.data_path,f.array_index]);clone.animation_data.drivers.remove(f)
            else:
                assert match.group(1) not in ('头颅','形变-头颅','骨架设置')
                kept.append(f.data_path)
                for var in f.driver.variables:
                    for target in var.targets:
                        assert target.data_path=='pose.bones["骨架设置"]["头发FK-IK"]'
                        assert target.id in (rig,clone)
                        target.id=rig
        assert len(kept)==14
        for name in ('头颅','形变-头颅'):
            p=clone.pose.bones[name]
            for con in list(p.constraints):p.constraints.remove(con)
            con=p.constraints.new('COPY_TRANSFORMS')
            con.name='HRIG original head boundary';con.target=rig;con.subtarget=name
            con.target_space='WORLD';con.owner_space='WORLD';con.mix_mode='REPLACE'
        for obj in old_selected:obj.select_set(False)
        clone.select_set(True);layer.objects.active=clone
        bpy.ops.object.mode_set(mode='EDIT')
        for name in ('头颅','形变-头颅','骨架设置'):
            eb=data.edit_bones[name];eb.use_connect=False;eb.parent=None
        for b in list(data.edit_bones):
            if b.name not in names:data.edit_bones.remove(b)
        bpy.ops.object.mode_set(mode='OBJECT')
        assert set(data.bones.keys())==names
        # Keep every retained source constraint. Object.copy remapping is verified,
        # not assumed; only the two declared boundary constraints may read orig.
        constraints=[]
        for p in clone.pose.bones:
            assert p.bone.bbone_segments==1
            for con in p.constraints:
                for field,sub in (('target','subtarget'),('pole_target','pole_subtarget'),('space_object','space_subtarget')):
                    target=getattr(con,field,None);subtarget=getattr(con,sub,'')
                    if target==clone:
                        assert subtarget and subtarget in names,(p.name,con.name,field,subtarget)
                    elif target==rig:
                        assert p.name in ('头颅','形变-头颅') and con.name=='HRIG original head boundary'
                    elif target is not None:
                        assert target.name in ('千早爱音-后发物理',)
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
                           'head_inputs':['头颅','形变-头颅'],'source_properties':['头发FK-IK'],
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
