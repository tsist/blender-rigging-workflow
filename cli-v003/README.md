# Rigging Workflow 0.42.3-agent-v003

This source preview provides the four background commands used by the matching [blenderctl adapter](https://github.com/tsist/blenderctl). It uses Windows Blender 5.3.0 (build `02f0899d78e3`) and its bundled NumPy. Run the command using 64-bit Python 3.11 or newer. The Python launcher starts Blender offline with factory startup and automatic script execution disabled.

Set `BLENDERCTL_RIGGING_ROOT` to the root of this repository for the adapter. The same entry points run directly. This PowerShell example displays the selected-weight command's arguments.

```powershell
python -B .\cli-v003\cli\selected_skinning_cli.py --help
```

| blenderctl subcommand | Direct script under `cli/` | Modes |
| --- | --- | --- |
| `candidate-entry` | [character_entry_candidate.py](cli/character_entry_candidate.py) | `inspect`, `scope`, `audition`, `finish`, `result` |
| `candidate-selected-skin` | [selected_skinning_cli.py](cli/selected_skinning_cli.py) | `inspect`, `execute` |
| `candidate-repair` | [rigging_candidate.py](cli/rigging_candidate.py) | `inspect`, `review-handoff`, `apply`, `cancel` |
| `candidate-skinning-policy` | [skinning_policy_candidate.py](cli/skinning_policy_candidate.py) | `qualify`, `plan`, `preview`, `apply` |

Pass the mode, `--request` with an absolute JSON path, `--run-dir` with a new absolute directory under an existing parent, and `--blender` with the actual executable path. The repair command's `cancel` mode takes the existing repair run directory. See each script's request validator for the schema, applicable input fields, and review receipt required for writing. Inspect and plan on the saved input, then submit the reviewed scope and its file hashes for the write step. Keep each request, source fingerprint and result together for resumption. Existing workspaces should use the exact historical checkout for old candidate receipts.

Selected-bone recomputation writes the selected bones on the explicitly targeted meshes inside the reviewed vertex ranges. Non-selected vertex group entries are preserved even when the resulting total exceeds 1. Check the relevant posed joint after adoption. Review close-contact regions and hand off unresolved lips and eyelashes for manual correction.

C94 operations require the caller's authorized template and positioning data. Modular generation expects compatible installed Rigify when the operation requests it. Supply the saved Blender inputs, supporting files and authorization receipts for the intended operation.

## CPU and CUDA

Voxel settings accept `compute_backend` values `CPU`, `AUTO`, and `CUDA`. `AUTO` uses CPU when the validated CUDA image or runtime is unavailable. To enable CUDA for the verified NVIDIA GeForce RTX 5080 Laptop GPU (compute capability 12.0, CUDA driver API 13.0 or later), build the checked-in kernel with a local [NVIDIA NVRTC 13.0](https://docs.nvidia.com/cuda/archive/13.0.0/nvrtc/index.html) installation.

```powershell
python -B .\cli-v003\build_cuda.py --nvrtc-dir $nvrtcDirectory
```

Set `$nvrtcDirectory` to the directory containing both `nvrtc64_130_0.dll` and `nvrtc-builtins64_130.dll`. The helper writes the local compiled image and SHA-256 manifest beside [direct_pcg.cu](rigging_workflow/volume/cuda/direct_pcg.cu). For a CUDA job, confirm the solve result reports the CUDA backend and passes its residual check. The backend checks device identity and package bytes.

## Source and dependencies

The CLI runtime and CUDA kernel source are [GPL-3.0-or-later](../LICENSE). The bundled pycpd source derives from upstream commit `e5ca02d2501fb4b633c1664de939c336ffa2349e` and retains its [MIT license](rigging_workflow/vendor/pycpd/LICENSE). Blender, optional Rigify and the NVIDIA toolchain are provided by the user's environment. This directory is a background source preview. The repository's existing development add-on uses its own release instructions.
