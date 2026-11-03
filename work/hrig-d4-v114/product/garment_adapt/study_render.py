"""Temporary owned scene/camera for reproducible technical garment views.

Shares source collections read-only, copies layer visibility onto the new view
layer, and changes only owned Scene/World/Camera/Lights. Never saves a blend.
The caller must compare its full author/context/body/bones after cleanup.
"""
from pathlib import Path
import math

import bpy
from mathutils import Vector

from .contract import require
from .cross_prepare import local_ids


def copy_layer_tree(source,target):
    for name in ('exclude','hide_viewport','holdout','indirect_only'):
        if hasattr(source,name) and hasattr(target,name):
            setattr(target,name,getattr(source,name))
    by_collection={child.collection:child for child in target.children}
    for child in source.children:
        require(child.collection in by_collection,'STUDY_LAYER_COLLECTION',child.collection.name)
        copy_layer_tree(child,by_collection[child.collection])


def render_views(directory,views,engine, *, focus=(0.,.02,1.22),ortho_scale=.85,resolution=768):
    require(engine in ('BLENDER_WORKBENCH','BLENDER_EEVEE','CYCLES'),'STUDY_ENGINE',engine)
    require(type(views) is list and 1<=len(views)<=6 and len(set(views))==len(views),'STUDY_VIEWS',views)
    locations={'front':(0.,-2.,1.32),'back':(0.,2.,1.32),'left':(2.,.02,1.32),
               'right':(-2.,.02,1.32),'threequarter':(1.6,-2.,1.5),'backquarter':(-1.6,2.,1.5)}
    require(set(views)<=set(locations),'STUDY_VIEW_NAMES',views)
    root=Path(directory).resolve(strict=True)
    require(type(resolution) is int and 256<=resolution<=2048,'STUDY_RESOLUTION',resolution)
    require(math.isfinite(ortho_scale) and ortho_scale>0.,'STUDY_SCALE',ortho_scale)
    original_scene=bpy.context.scene;original_layer=bpy.context.view_layer
    original_ids=local_ids();scene=None;outputs=[]
    original_window_scene=bpy.context.window.scene if bpy.context.window else None
    original_window_layer=bpy.context.window.view_layer if bpy.context.window else None
    try:
        scene=bpy.data.scenes.new('D4_Owned_Garment_Study')
        scene.frame_set(original_scene.frame_current,subframe=original_scene.frame_subframe)
        for collection in original_scene.collection.children:
            scene.collection.children.link(collection)
        for obj in original_scene.collection.objects:
            scene.collection.objects.link(obj)
        layer=scene.view_layers[0]
        copy_layer_tree(original_layer.layer_collection,layer.layer_collection)
        for obj in original_scene.objects:
            if obj.name in layer.objects:
                obj.hide_set(obj.hide_get(view_layer=original_layer),view_layer=layer)
        camera_data=bpy.data.cameras.new('D4_Owned_Study_Camera')
        camera=bpy.data.objects.new('D4_Owned_Study_Camera',camera_data)
        scene.collection.objects.link(camera);scene.camera=camera
        camera_data.type='ORTHO';camera_data.ortho_scale=ortho_scale
        scene.render.engine=engine
        scene.render.resolution_x=scene.render.resolution_y=resolution;scene.render.resolution_percentage=100
        scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA'
        scene.render.film_transparent=False
        scene.render.threads_mode='FIXED';scene.render.threads=2
        scene.view_settings.view_transform='Standard';scene.view_settings.exposure=0.;scene.view_settings.gamma=1.
        world=bpy.data.worlds.new('D4_Owned_Study_World');world.color=(.14,.14,.14);scene.world=world
        if engine=='BLENDER_WORKBENCH':
            shading=scene.display.shading
            shading.light='STUDIO';shading.color_type='RANDOM';shading.background_type='WORLD'
            shading.show_shadows=True;shading.show_cavity=True;shading.cavity_type='BOTH';shading.show_object_outline=True
        else:
            if engine=='CYCLES':
                scene.cycles.device='CPU';scene.cycles.samples=24;scene.cycles.use_denoising=True
            world.use_nodes=True
            background=next(n for n in world.node_tree.nodes if n.bl_idname=='ShaderNodeBackground')
            background.inputs['Color'].default_value=(.18,.18,.18,1.);background.inputs['Strength'].default_value=.7
            for index,(position,power,size) in enumerate((((-1.1,-1.2,2.2),450.,1.7),((1.2,-.7,1.8),300.,1.4),((0.,1.4,2.),400.,1.6))):
                data=bpy.data.lights.new('D4_Owned_Study_Light_'+str(index),'AREA');data.energy=power;data.shape='DISK';data.size=size
                obj=bpy.data.objects.new(data.name,data);scene.collection.objects.link(obj);obj.location=position
                obj.rotation_euler=(Vector(focus)-obj.location).to_track_quat('-Z','Y').to_euler()
        for view in views:
            path=root/('output-'+engine.lower()+'-'+view+'.png')
            require(not path.exists(),'STUDY_OUTPUT_EXISTS',str(path))
            camera.location=locations[view];camera.rotation_euler=(Vector(focus)-camera.location).to_track_quat('-Z','Y').to_euler()
            scene.render.filepath=str(path)
            outcome=bpy.ops.render.render(scene=scene.name,write_still=True)
            require(outcome=={'FINISHED'} and path.is_file(),'STUDY_RENDER_FAILED',view)
            outputs.append({'file':str(path),'view':view,'engine':engine,'resolution':[resolution,resolution],
                            'material_scope':'Diagnostic per-object random colors' if engine=='BLENDER_WORKBENCH' else 'Actual retained material node trees/textures in '+engine})
        return outputs
    finally:
        if bpy.context.window and scene and bpy.context.window.scene==scene:
            bpy.context.window.scene=original_window_scene
            bpy.context.window.view_layer=original_window_layer
        owned=local_ids()-original_ids
        if owned:
            bpy.data.batch_remove(ids=owned)
        original_layer.update()
