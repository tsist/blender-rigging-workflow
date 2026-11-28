"""Load a hash-checked snapshot of the canonical Win53 rigging workflow."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib


HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
ROOT = SOURCE.parents[2]
TOOLS = ROOT / "tools/blenderctl"
sys.path.insert(0, str(TOOLS))

from material_draft_handoff import guard_file, require


MINIMUM_BLENDER = (5, 3, 0)
MANIFEST = SOURCE / "exchange-runtime-manifest.json"
manifest_content = MANIFEST.read_bytes()
MANIFEST_SHA = hashlib.sha256(manifest_content).hexdigest()
addon_metadata = (SOURCE / "blender_manifest.toml").read_bytes()
require(hashlib.sha256(addon_metadata).hexdigest() == json.loads(manifest_content)["files"]["blender_manifest.toml"],
        "CONFLICT", "Rigging Blender manifest differs from the pinned package")
EDITION = tomllib.loads(addon_metadata.decode("utf-8"))["version"]
SCENE_FIELDS = {"file", "expected_sha256", "scene", "view_layer"}


def scene_input(request, held):
    require(all(isinstance(request.get(key), str) and request[key] and "\0" not in request[key]
                for key in SCENE_FIELDS), "INVALID_REQUEST",
            "A saved source needs file, expected_sha256, scene and view_layer")
    source = guard_file(request["file"], held, request["expected_sha256"])
    require(source.suffix.lower() == ".blend", "INVALID_REQUEST", "Expected a saved .blend")
    return {**request, "file": str(source)}


def selected_host(blender):
    from protocol import Failure

    try:
        observed = subprocess.run([str(blender), "--version"], capture_output=True, encoding="utf-8",
                                  timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise Failure("TIMEOUT", "Could not verify the selected rigging host") from exc
    version = re.search(r"^Blender (\d+)\.(\d+)\.(\d+)", observed.stdout, re.MULTILINE)
    build = re.search(r"^\s*build hash:\s*([0-9a-f]+)\s*$", observed.stdout, re.MULTILINE)
    require(observed.returncode == 0 and version and build, "UNSUPPORTED", "Cannot verify the selected Blender")
    number = tuple(map(int, version.groups()))
    require(number >= MINIMUM_BLENDER, "UNSUPPORTED", "Rigging requires Blender 5.3 or newer")
    return {"version": list(number), "build": build.group(1)}


def activate(bpy, directory, *, background, expected_host=None):
    host = {"version": list(bpy.app.version), "build": bpy.app.build_hash.decode()}
    require(sys.platform == "win32" and tuple(host["version"]) >= MINIMUM_BLENDER
            and re.fullmatch(r"[0-9a-f]+", host["build"]) and bpy.app.background == background
            and not bpy.app.online_access, "UNSUPPORTED", "Use offline Windows Blender 5.3 or newer")
    require(expected_host is None or host == expected_host, "CONFLICT", "Rigging worker host differs from the selected Blender")
    raw = MANIFEST.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == MANIFEST_SHA, "CONFLICT", "Rigging runtime manifest changed")
    manifest = json.loads(raw)
    require(manifest["schema"] == "c94.exchange-runtime.package.v1" and manifest["host"].get("offline") is True,
            "CONFLICT", "Unsupported rigging runtime manifest")
    require("rigging_workflow" not in sys.modules, "CONFLICT", "A rigging add-on is already loaded")
    package = directory / "rigging_workflow"
    package.mkdir(parents=True)
    for name, expected in manifest["files"].items():
        path = PurePosixPath(name)
        require(path.parts and not path.is_absolute() and ".." not in path.parts
                and "\\" not in name and ":" not in name, "CONFLICT", "Invalid staged package path")
        content = (SOURCE / path).read_bytes()
        require(hashlib.sha256(content).hexdigest() == expected, "CONFLICT", "Rigging source changed: " + name)
        output = package / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    (package / MANIFEST.name).write_bytes(raw)
    sys.path.insert(0, str(directory))
    import rigging_workflow

    require(Path(rigging_workflow.__file__).resolve().parent == directory / "rigging_workflow",
            "CONFLICT", "Rigging module did not resolve to the pinned package")
    rigging_workflow.register()
    return {"edition": EDITION, **host, "package_sha256": MANIFEST_SHA}


def open_scene(bpy, file, request, *, ui=False):
    require(bpy.ops.wm.open_mainfile(filepath=str(file), load_ui=ui, use_scripts=False) == {"FINISHED"}
            and Path(bpy.data.filepath).samefile(file), "VALIDATION_FAILED",
            "Could not reopen the saved Blender input")
    manager = bpy.context.window_manager
    window = bpy.context.window or next(iter(manager.windows), None)
    scene = bpy.data.scenes.get(request["scene"])
    require(scene is not None and request["view_layer"] in scene.view_layers,
            "CONFLICT", "The requested scene or view layer is absent")
    require(window is not None, "WORKER_FAILED", "Blender did not create a scene window")
    window.scene = scene
    window.view_layer = scene.view_layers[request["view_layer"]]
    return window


def user_environment(profile):
    temporary = profile / "temp"
    temporary.mkdir(parents=True)
    return {**os.environ, "BLENDER_USER_RESOURCES": str(profile),
            "BLENDER_USER_CONFIG": str(profile / "config"),
            "BLENDER_USER_SCRIPTS": str(profile / "scripts"),
            "BLENDER_USER_DATAFILES": str(profile / "datafiles"),
            "BLENDER_USER_EXTENSIONS": str(profile / "extensions"),
            "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1"}
