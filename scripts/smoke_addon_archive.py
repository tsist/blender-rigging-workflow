"""Register the packaged legacy add-on from an isolated extracted directory."""
from pathlib import Path,PurePosixPath
import importlib,json,sys,tempfile,zipfile
args=sys.argv[sys.argv.index('--')+1:]
archive=Path(args[0]);output=Path(args[1])
with tempfile.TemporaryDirectory(prefix='hrig-archive-') as directory:
    root=Path(directory)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            parts=PurePosixPath(name).parts
            assert not name.startswith('/') and '..' not in parts and ':' not in name
        z.extractall(root)
    sys.path.insert(0,str(root))
    module=importlib.import_module('humanoid_rigging_probe')
    assert Path(module.__file__).resolve().is_relative_to(root.resolve())
    module.register();module.unregister()
output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps({'packaged_addon_extract_import_register_unregister':True,
 'scope':'isolated factory-startup archive registration; no character or quality acceptance'},indent=2),encoding='utf8')
print('Packaged base add-on registration passed.')
