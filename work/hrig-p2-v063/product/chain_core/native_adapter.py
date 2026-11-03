"""Owned, reversible native projection adapter shared by CLI and GUI."""
import bpy


class ChainPreview:
    def __init__(self, rig):
        self.rig=rig
        self.edits=[]
        self.outputs=[]

    def physics_outputs(self, enabled):
        if enabled:
            if self.outputs:raise ValueError('output_switch_already_owned')
            for i in range(1,9):
                name=f'形变-后发-{i:02}'
                matches=[c for c in self.rig.pose.bones[name].constraints
                    if c.type=='COPY_TRANSFORMS' and c.target==self.rig and c.subtarget==f'后发-{i:02}']
                if len(matches)!=1:raise ValueError('output_route_drift:'+name)
                c=matches[0];self.outputs.append((c,c.subtarget));c.subtarget=f'物理-后发-{i:02}'
        else:
            for c,subtarget in reversed(self.outputs):c.subtarget=subtarget
            self.outputs.clear()
        bpy.context.view_layer.update()

    def apply(self, joints):
        if self.edits:raise ValueError('preview_already_owned')
        if len(joints)!=9:raise ValueError('complete_nine_joints_required')
        for i in range(1,9):
            p=self.rig.pose.bones[f'物理-后发-{i:02}'];active=p.constraints.active
            ob=bpy.data.objects.new('HRIG063_CHAIN_TARGET',None)
            bpy.context.scene.collection.objects.link(ob);ob.location=joints[i]
            dt=p.constraints.new('DAMPED_TRACK');dt.name='HRIG063_CHAIN_DIRECTION'
            dt.target=ob;dt.owner_space='WORLD';dt.target_space='WORLD';dt.track_axis='TRACK_Y'
            dt.influence=1.;p.constraints.active=active
            self.edits.append((p,dt,ob,active))
            bpy.context.view_layer.update()

    def restore(self):
        while self.edits:
            p,dt,ob,active=self.edits.pop();p.constraints.remove(dt)
            p.constraints.active=active;bpy.data.objects.remove(ob,do_unlink=True)
        if self.outputs:self.physics_outputs(False)
        bpy.context.view_layer.update()
