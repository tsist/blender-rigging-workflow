# SPDX-License-Identifier: GPL-3.0-or-later
import bpy,sys,time,os,shutil,struct,tempfile
from pathlib import Path
here=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(here))
job_root=Path(tempfile.mkdtemp(dir=os.environ['RIGGING_TEST_ROOT']))
vmd=job_root/'synthetic.vmd'
with vmd.open('wb') as f:
    f.write(b'Vocaloid Motion Data 0002'.ljust(30,b'\0')+b'Synthetic'.ljust(20,b'\0'))
    f.write(struct.pack('<I',3))
    for frame,pos in [(0,0),(10,1),(20,2)]:
        f.write('センター'.encode('cp932').ljust(15,b'\0'))
        f.write(struct.pack('<I7f',frame,pos,0,0,0,0,0,1))
        f.write(bytes([20]*8+[107]*8)*4)
    f.write(bytes(20))
import rigging_workflow as addon
from rigging_workflow import retarget_mmd_import_ui as ui
addon.register()
scene=bpy.context.scene
scene.render.fps=25
original=bpy.data.objects["Cube"];original.name="PRIVATE_SCENE_MARKER"
opts=getattr(scene,ui.OPTIONS)
opts.mode='STANDARD_REFERENCE'
opts.reference_acknowledged=True
opts.resource_root=str(here/'rigging_workflow/resources/mmd/uka')
opts.vmd=str(vmd)
opts.addon_path=os.environ['RIGGING_MMD_TOOLS']
opts.output_root=str(job_root)
assert bpy.ops.c94_workflow.retarget_mmd_import(operation='SUBMIT')=={'FINISHED'}
task=ui._TASK
until=time.monotonic()+45
while time.monotonic()<until:
    state=ui.poll_once()
    if state['state'] in ui.TERMINAL:break
    time.sleep(.1)
assert state['state']=='succeeded',(state,task['directory'])
assert len(scene.objects)==3 and bpy.data.objects['PRIVATE_SCENE_MARKER']==original
art=ui.completed_artifacts()
with bpy.data.libraries.load(art['blend']) as (src,dst):
    assert 'PRIVATE_SCENE_MARKER' not in src.objects
assert bpy.ops.c94_workflow.retarget_mmd_import(operation='COLLECT')=={'FINISHED'}
assert task['append_receipt']['all_actions_validated'] and scene.render.fps==25 and bpy.data.objects['PRIVATE_SCENE_MARKER']==original
print('LINUX_NATIVE_MMD_PANEL_OK',task['directory'])

assert bpy.ops.c94_workflow.retarget_mmd_import(operation='RELEASE')=={'FINISHED'}
object_count=len(scene.objects)
root=job_root/'negative'; root.mkdir()
active_vmd=root/'synthetic.vmd'
shutil.copyfile(vmd,active_vmd)
opts.vmd=str(active_vmd); opts.output_root=str(root)
op=bpy.ops.c94_workflow.retarget_mmd_import
assert op(operation='SUBMIT')=={'FINISHED'}
assert op(operation='CANCEL')=={'FINISHED'}
def wait():
    until=time.monotonic()+45
    while time.monotonic()<until:
        s=ui.poll_once()['state']
        if s in ui.TERMINAL:return s
        time.sleep(.1)
assert wait()=='cancelled'
assert not (Path(ui._TASK['directory'])/'result.json').exists()
assert op(operation='RELEASE')=={'FINISHED'}
assert op(operation='SUBMIT')=={'FINISHED'}
d=Path(ui._TASK['directory'])
until=time.monotonic()+15
while not (d/'output-import-progress.json').exists() and time.monotonic()<until:
    assert ui.poll_once()['state']=='running'
    time.sleep(.02)
assert (d/'output-import-progress.json').exists()
try:
    fd=os.open(active_vmd,os.O_WRONLY|os.O_NONBLOCK)
except BlockingIOError:
    pass
else:
    os.close(fd)
    raise AssertionError('kernel lease did not exclude an active competing write-open')
assert wait()=='failed'
d=Path(ui._TASK['directory'])
assert 'lease' in (d/'worker.log').read_text().lower()
assert not (d/'result.json').exists()
assert len(bpy.context.scene.objects)==object_count
print('LINUX_NATIVE_MMD_CANCEL_AND_CHANGED_SOURCE_OK')
