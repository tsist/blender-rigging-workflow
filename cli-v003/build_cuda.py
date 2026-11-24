# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile the checked-in SM 120 kernel using a local NVIDIA NVRTC 13.0 installation."""

import argparse
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


OPTIONS = (b"--gpu-architecture=sm_120", b"--std=c++17")
PROGRAM_NAME = b"audit_kernel.cu"
NVRTC = "nvrtc64_130_0.dll"
BUILTINS = "nvrtc-builtins64_130.dll"
CUDA_ROOT = Path(__file__).resolve().parent / "rigging_workflow" / "volume" / "cuda"


def compile_kernel(directory, source):
    pointer = c.c_void_p
    char = c.c_char_p
    size = c.c_size_t
    integer = c.c_int

    with os.add_dll_directory(str(directory)):
        c.WinDLL(str(directory / BUILTINS))
        library = c.WinDLL(str(directory / NVRTC))

        def bind(name, *types, result=integer):
            function = getattr(library, name)
            function.argtypes = types
            function.restype = result
            return function

        error_string = bind("nvrtcGetErrorString", integer, result=char)

        def check(function, *args):
            status = function(*args)
            if status:
                raise RuntimeError(f"{function.__name__}: {error_string(status).decode()}")

        version = bind("nvrtcVersion", c.POINTER(integer), c.POINTER(integer))
        major, minor = integer(), integer()
        check(version, c.byref(major), c.byref(minor))
        if (major.value, minor.value) != (13, 0):
            raise RuntimeError(f"Expected NVRTC 13.0, got {major.value}.{minor.value}")

        create = bind("nvrtcCreateProgram", c.POINTER(pointer), char, char,
                      integer, c.POINTER(char), c.POINTER(char))
        compile_program = bind("nvrtcCompileProgram", pointer, integer, c.POINTER(char))
        log_size = bind("nvrtcGetProgramLogSize", pointer, c.POINTER(size))
        log = bind("nvrtcGetProgramLog", pointer, pointer)
        cubin_size = bind("nvrtcGetCUBINSize", pointer, c.POINTER(size))
        cubin = bind("nvrtcGetCUBIN", pointer, pointer)
        destroy = bind("nvrtcDestroyProgram", c.POINTER(pointer))

        program = pointer()
        check(create, c.byref(program), source, PROGRAM_NAME, 0, None, None)
        try:
            options = (char * len(OPTIONS))(*OPTIONS)
            if compile_program(program, len(options), options):
                length = size()
                check(log_size, program, c.byref(length))
                message = c.create_string_buffer(length.value)
                check(log, program, message)
                raise RuntimeError(message.value.decode(errors="replace"))
            length = size()
            check(cubin_size, program, c.byref(length))
            output = c.create_string_buffer(length.value)
            check(cubin, program, output)
            return output.raw
        finally:
            check(destroy, c.byref(program))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nvrtc-dir", type=Path, required=True,
                        help="Directory containing both NVIDIA NVRTC 13.0 DLLs")
    args = parser.parse_args()
    if sys.platform != "win32" or c.sizeof(c.c_void_p) != 8:
        parser.error("A 64-bit Windows Python is required")

    source_path = CUDA_ROOT / "direct_pcg.cu"
    image_path = CUDA_ROOT / "direct-pcg-sm120.cubin"
    manifest_path = CUDA_ROOT / "manifest.json"
    if image_path.exists() or manifest_path.exists():
        parser.error("The local CUDA image or manifest already exists")

    try:
        source = source_path.read_bytes()
        image = compile_kernel(args.nvrtc_dir.resolve(strict=True), source)
        manifest = {
            "schema_version": 1,
            "backend": "cuda-driver-jacobi-pcg",
            "source": {"file": source_path.name, "sha256": hashlib.sha256(source).hexdigest()},
            "cubin": {
                "file": image_path.name,
                "sha256": hashlib.sha256(image).hexdigest(),
                "architecture": "sm_120",
                "compute_capability": [12, 0],
                "verified_device": "NVIDIA GeForce RTX 5080 Laptop GPU",
            },
            "build": {"tool": "NVIDIA NVRTC", "api_version": "13.0",
                      "options": [option.decode() for option in OPTIONS]},
        }
        with TemporaryDirectory(dir=CUDA_ROOT) as staging:
            staged_image = Path(staging) / image_path.name
            staged_manifest = Path(staging) / manifest_path.name
            staged_image.write_bytes(image)
            staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            staged_image.rename(image_path)
            try:
                staged_manifest.rename(manifest_path)
            except OSError:
                image_path.unlink()
                raise
    except (OSError, RuntimeError) as error:
        parser.exit(1, f"CUDA build failed: {error}\n")

    print(f"Built {image_path.name} ({manifest['cubin']['sha256']})")


if __name__ == "__main__":
    main()
