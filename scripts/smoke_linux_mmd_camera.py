# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless camera-panel smoke. Requires vetted MMD Tools and writable lease-capable root.

RIGGING_TEST_ROOT=/tmp RIGGING_MMD_TOOLS=/path/to/mmd_tools \
  blender --factory-startup --background --disable-autoexec --offline-mode \
  --python-exit-code 1 --python scripts/smoke_linux_mmd_camera.py
"""

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time

import bpy

SENTINEL = "PRIVATE_CAMERA_SCENE_SENTINEL"
FRAMES = (1, 7, 15)


def fixture(path):
    header = b"Vocaloid Motion Data 0002".ljust(30, b"\0") + b"Camera".ljust(20, b"\0")
    blocks = [header, struct.pack("<3I", 0, 0, 4)]
    for i, frame in enumerate((0, 8, 16, 24)):
        blocks.append(struct.pack(
            "<I7f24sIB", frame, -20-i, i, 2+i, 1,
            .05*i, .03*i, .02*i, bytes((20, 100, 35, 110))*6,
            45+2*i, 0 if i < 2 else 1,
        ))
    path.write_bytes(b"".join(blocks) + bytes(12))
    return path.read_bytes()


def camera_samples(scene, camera):
    samples = []
    for frame in FRAMES:
        scene.frame_set(frame)
        evaluated = camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
        samples.append([
            list(evaluated.matrix_world.to_quaternion()),
            float(evaluated.data.lens),
            evaluated.data.type,
        ])
    return samples


def cold(root):
    spec = json.loads((root / "sample.json").read_text())
    scene = bpy.context.scene
    camera = scene.objects[spec["camera"]]
    assert scene.camera.name == SENTINEL
    assert scene.render.fps == 24 and scene.frame_current == 3
    assert camera.parent is None and not camera.constraints
    assert camera.animation_data.action_slot.target_id_type == "OBJECT"
    assert camera.data.animation_data.action_slot.target_id_type == "CAMERA"
    assert camera_samples(scene, camera) == spec["values"]
    print("LINUX_NATIVE_MMD_CAMERA_COLD_OK", flush=True)


def wait(ui, opts, expected):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = ui.poll_task(opts, include_error=True, force=True)
        if state["terminal"]:
            assert state["state"] == expected, (state, opts.error, opts.job_directory)
            return
        time.sleep(.05)
    raise AssertionError((expected, opts.job_state, opts.error, opts.job_directory))


def smoke():
    here = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(here))
    import rigging_workflow
    from rigging_workflow import retarget_mmd_camera_ui as ui

    root = Path(tempfile.mkdtemp(prefix="mmd-camera-smoke-", dir=os.environ["RIGGING_TEST_ROOT"]))
    print("MMD_CAMERA_SMOKE_ROOT", root, flush=True)
    jobs = root / "jobs"
    jobs.mkdir()
    vmd = root / "synthetic.vmd"
    original = fixture(vmd)
    addon = Path(os.environ["RIGGING_MMD_TOOLS"]).resolve(strict=True)
    wheel = next((addon / "wheels").glob("opencc_python_reimplemented-*.whl"))
    rigging_workflow.register()
    scene = bpy.context.scene
    sentinel = scene.camera
    sentinel.name = SENTINEL
    scene.render.fps = 24
    scene.frame_set(3)
    original_names = tuple(scene.objects.keys())
    opts = getattr(scene, ui.OPTIONS)
    opts.vmd_path = str(vmd)
    opts.backend_path = str(addon)
    opts.opencc_path = str(wheel)
    opts.jobs_root = str(jobs)
    opts.artificial_note = True
    op = bpy.ops.c94_camera

    assert op.build_native_library() == {"FINISHED"}
    wait(ui, opts, "succeeded")
    assert not opts.profile_collected and not opts.appended_camera
    assert tuple(scene.objects.keys()) == original_names and scene.camera == sentinel
    job_dir = Path(opts.job_directory)
    receipt = json.loads((job_dir / "output-build.json").read_text())
    assert receipt["ok"] is True and receipt["job_id"] == opts.job_id
    assert receipt["checks"]["runtime_unregistered"] is True
    assert vmd.read_bytes() == original
    with bpy.data.libraries.load(str(job_dir / "output-camera.blend")) as (source, target):
        assert SENTINEL not in source.objects

    assert op.collect_result() == {"FINISHED"} and opts.profile_collected
    assert tuple(scene.objects.keys()) == original_names and scene.camera == sentinel
    assert op.append_profile() == {"FINISHED"}
    assert scene.camera == sentinel and scene.render.fps == 24 and scene.frame_current == 3
    assert len(scene.objects) == len(original_names) + 1
    camera = scene.objects[opts.appended_camera]
    samples = camera_samples(scene, camera)
    assert samples[0][0] != samples[1][0] and samples[0][1] != samples[1][1], samples
    assert samples[0][2] == "PERSP" and samples[-1][2] == "ORTHO", samples
    scene.frame_set(3)
    assert op.release_job() == {"FINISHED"}
    print("LINUX_NATIVE_MMD_CAMERA_PANEL_OK", job_dir, flush=True)

    assert op.build_native_library() == {"FINISHED"}
    assert op.cancel_job() == {"FINISHED"}
    wait(ui, opts, "cancelled")
    assert op.collect_result() == {"FINISHED"} and not opts.profile_collected
    assert op.release_job() == {"FINISHED"}

    with vmd.open("r+b"):
        assert op.build_native_library() == {"FINISHED"}
        wait(ui, opts, "failed")
    assert op.collect_result() == {"FINISHED"} and not opts.profile_collected
    conflict_dir = Path(opts.job_directory)
    assert "lease" in (conflict_dir / "worker.log").read_text().lower()
    assert not (conflict_dir / "output-camera.blend").exists()
    assert op.release_job() == {"FINISHED"}
    assert scene.camera == sentinel and len(scene.objects) == len(original_names) + 1
    assert vmd.read_bytes() == original
    print("LINUX_NATIVE_MMD_CAMERA_CANCEL_AND_GUARD_OK", flush=True)

    (root / "sample.json").write_text(json.dumps({"camera": camera.name, "values": samples}))
    blend = root / "cold.blend"
    bpy.context.preferences.filepaths.file_preview_type = 'NONE'
    assert bpy.ops.wm.save_as_mainfile(filepath=str(blend)) == {"FINISHED"}
    subprocess.run([
        bpy.app.binary_path, "--background", "--factory-startup", "--disable-autoexec",
        "--offline-mode", str(blend), "--python-exit-code", "1",
        "--python", str(Path(__file__).resolve()), "--", "--cold", str(root),
    ], check=True, timeout=45)


if "--cold" in sys.argv:
    cold(Path(sys.argv[sys.argv.index("--cold") + 1]))
else:
    smoke()
