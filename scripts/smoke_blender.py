"""Packaging/import smoke only; never opens user scenes or certifies quality."""
from pathlib import Path
import importlib, json, sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hrig_paths import configure
configure()
import bpy
report={'scope':'factory-startup packaging/import/register smoke; no quality certification',
        'blender':bpy.app.version_string,'imports':[]}
for name in ['character_state','native_control','native_controls.native','fullbody_controls',
             'rest_workspace.session','weight_workflow.native','d_workspace.coordinator',
             'facial_controls.session','facial_key_ui.session','local_proportion.session']:
    importlib.import_module(name);report['imports'].append(name)
addon=importlib.import_module('humanoid_rigging_probe')
addon.register();addon.unregister()
report['base_addon_register_unregister']=True
path=ROOT/'test-output/blender-smoke.json';path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report))
