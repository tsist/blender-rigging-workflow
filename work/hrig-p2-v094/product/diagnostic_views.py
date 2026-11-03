"""Temporary gray-skin views. Never saves or edits the source mesh."""
from pathlib import Path
import bpy,numpy as np
from mathutils import Vector

def render_skin(body_name,output,views=('front','right'),focus=None,flat=False):
    original_images=set(bpy.data.images)
    obj=bpy.data.objects[body_name];dg=bpy.context.evaluated_depsgraph_get();evaluated=obj.evaluated_get(dg)
    mesh=bpy.data.meshes.new_from_object(evaluated,depsgraph=dg)
    shown=bpy.data.objects.new('HRIG094_DIAGNOSTIC_BODY',mesh);shown.matrix_world=evaluated.matrix_world.copy()
    scene=bpy.data.scenes.new('HRIG094_DIAGNOSTIC_SCENE');scene.collection.objects.link(shown)
    camera_data=bpy.data.cameras.new('HRIG094_DIAGNOSTIC_CAMERA');camera=bpy.data.objects.new('HRIG094_DIAGNOSTIC_CAMERA',camera_data);scene.collection.objects.link(camera);scene.camera=camera
    try:
        scene.render.engine='BLENDER_WORKBENCH';scene.render.resolution_x=960;scene.render.resolution_y=1280;scene.render.resolution_percentage=100
        scene.render.image_settings.file_format='PNG';scene.render.film_transparent=False
        shade=scene.display.shading;shade.light='STUDIO';shade.color_type='SINGLE';shade.single_color=(.66,.7,.74);shade.show_shadows=True;shade.show_cavity=True;shade.cavity_type='BOTH';shade.show_specular_highlight=True;shade.background_type='WORLD';shade.background_color=(.055,.055,.055)
        if flat:shade.show_shadows=False;shade.show_cavity=False
        coords=np.array([shown.matrix_world@v.co for v in mesh.vertices],dtype=float);lo=coords.min(0);hi=coords.max(0)
        center=Vector((lo+hi)/2);extent=max(float(hi[2]-lo[2]),float(hi[0]-lo[0])*1280/960)*1.12
        if focus is not None:center=Vector(focus['center']);extent=float(focus['extent'])
        camera_data.type='ORTHO';camera_data.ortho_scale=extent;camera_data.clip_start=.001;camera_data.clip_end=1000
        paths=[]
        for view in views:
            direction={'front':Vector((0,-1,0)),'back':Vector((0,1,0)),'right':Vector((1,0,0)),'left':Vector((-1,0,0)),'threequarter':Vector((1,-2,.15)),'threequarter_left':Vector((-1,-2,.15)),'low_inner_L':Vector((.1,-.25,-.08)),'low_inner_R':Vector((-.1,-.25,-.08))}[view]
            camera.location=center+direction.normalized()*max(extent*3,1)
            camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
            path=str(Path(output).with_name(Path(output).stem+'-'+view+'.png'));scene.render.filepath=path
            bpy.ops.render.render(write_still=True,scene=scene.name);paths.append(path)
        return {'images':paths,'bounds':[lo.tolist(),hi.tolist()],'camera_center':list(center),'ortho_scale':extent,'mesh_vertices':len(mesh.vertices)}
    finally:
        bpy.data.objects.remove(shown,do_unlink=True);bpy.data.objects.remove(camera,do_unlink=True)
        bpy.data.meshes.remove(mesh);bpy.data.cameras.remove(camera_data);bpy.data.scenes.remove(scene)
        for image in list(bpy.data.images):
            if image not in original_images and image.type=='RENDER_RESULT':
                bpy.data.images.remove(image)
