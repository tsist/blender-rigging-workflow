"""Current runtime registration smoke; no user scenes or quality certification."""
from pathlib import Path
import importlib, json, sys
import bpy
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
addon = importlib.import_module('rigging_workflow')
assert Path(addon.__file__).resolve() == ROOT/'rigging_workflow/__init__.py'
addon.register()
addon.unregister()
report = {'scope':'factory-startup current runtime registration only; no character quality certification',
          'blender':bpy.app.version_string,'runtime_version':list(addon.bl_info['version']),
          'register_unregister':True}
path = ROOT/'test-output/blender-smoke.json'
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report))
