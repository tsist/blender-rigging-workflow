import json, math, sys
from pathlib import Path
import bpy
from mathutils import Quaternion

req=json.loads(Path(sys.argv[sys.argv.index("--")+1]).read_text(encoding="utf-8"))
bpy.ops.wm.open_mainfile(filepath=req["candidate"],load_ui=False,use_scripts=False)
target=bpy.data.objects[req["target"]]; rig=bpy.data.objects[req["rig"]]
mod=target.modifiers.new("probe","ARMATURE");mod.object=rig;mod.use_deform_preserve_volume=False
rig.data.pose_position="POSE"
base=[target.matrix_world@v.co for v in target.data.vertices]
rows=[]
for name in req["bones"]:
 if name not in rig.pose.bones: rows.append({"bone":name,"exists":False});continue
 for pb in rig.pose.bones:
  pb.rotation_mode="QUATERNION";pb.rotation_quaternion=(1,0,0,0);pb.location=(0,0,0);pb.scale=(1,1,1)
 pb=rig.pose.bones[name];pb.rotation_mode="QUATERNION";pb.rotation_quaternion=Quaternion((0,0,1),0.3)
 bpy.context.view_layer.update();deps=bpy.context.evaluated_depsgraph_get();deps.update();ev=target.evaluated_get(deps);mesh=ev.to_mesh()
 disp=[(ev.matrix_world@mesh.vertices[i].co-base[i]).length for i in range(len(base))]
 rows.append({"bone":name,"exists":True,"max_displacement":max(disp),"mean_displacement":math.fsum(disp)/len(disp)})
 ev.to_mesh_clear()
print(json.dumps(rows,indent=2))
