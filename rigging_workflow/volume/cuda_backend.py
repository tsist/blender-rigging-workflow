# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional local RTX 5080 Laptop backend for the voxel PCG equation."""

import hashlib
import json
import math
from pathlib import Path
import sys
import time

import numpy as np

from .cuda_kernels import CudaDriver, CudaDriverError, DirectPCG, DONE


_DEVICE = "NVIDIA GeForce RTX 5080 Laptop GPU"
_CAPABILITY = (12, 0)
_MINIMUM_DRIVER_API = 13000
_CHECK_EVERY = 8
_ASSET_ROOT = Path(__file__).resolve().with_name("cuda")


class CudaUnavailable(RuntimeError):
    """The local CUDA driver, target adapter, or validated kernel is unavailable."""


class CudaNumericalError(RuntimeError):
    """At least one solved right hand side failed the original FP64 residual check."""

    def __init__(self, statistics):
        self.statistics = statistics
        super().__init__(f"CUDA PCG failed residual validation for columns {statistics['failed_columns']}")


def artifact_files():
    """Return the source, compiled image and provenance file required by this module."""
    return (
        _ASSET_ROOT / "direct_pcg.cu",
        _ASSET_ROOT / "direct-pcg-sm120.cubin",
        _ASSET_ROOT / "manifest.json",
    )


def _read_image():
    source, binary, manifest_path = artifact_files()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_info = manifest["source"]
        binary_info = manifest["cubin"]
        if (manifest["schema_version"] != 1 or manifest["backend"] != "cuda-driver-jacobi-pcg"
                or source_info["file"] != source.name or binary_info["file"] != binary.name
                or binary_info["architecture"] != "sm_120"
                or tuple(binary_info["compute_capability"]) != _CAPABILITY
                or binary_info["verified_device"] != _DEVICE):
            raise ValueError("incompatible CUDA artifact manifest")
        image = binary.read_bytes()
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        image_sha = hashlib.sha256(image).hexdigest()
        if source_sha != source_info["sha256"] or image_sha != binary_info["sha256"]:
            raise ValueError("CUDA artifact SHA-256 differs from the manifest")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise CudaUnavailable(f"The local CUDA image is invalid or missing: {exc}") from exc
    return image, image_sha


def _validate_problem(offsets, columns, values, degree, rhs, tolerance, max_iterations):
    offsets, columns, values, degree, rhs = map(np.asarray, (offsets, columns, values, degree, rhs))
    if rhs.ndim != 2 or not rhs.size or rhs.shape[1] > 1024:
        raise ValueError("CUDA PCG needs nonempty rhs with at most 1024 columns")
    rows, bones = rhs.shape
    pitch = (bones + 31) // 32 * 32
    if rows * pitch >= 2**31:
        raise ValueError("The padded field would overflow the CUDA kernel index")
    if offsets.shape != (rows + 1,) or offsets.dtype != np.int32 or (np.diff(offsets) <= 0).any():
        raise ValueError("CUDA PCG needs int32 CSR row pointers and nonempty rows")
    if columns.ndim != 1 or columns.dtype != np.int32 or values.shape != columns.shape:
        raise ValueError("CUDA PCG needs matching CSR columns and values, with int32 columns")
    if offsets[0] != 0 or offsets[-1] != len(columns) or (columns < 0).any() or (columns >= rows).any():
        raise ValueError("CUDA PCG CSR endpoints or columns are out of bounds")
    if degree.shape != (rows,) or (degree <= 0).any():
        raise ValueError("CUDA PCG needs a positive Jacobi degree per row")
    if any(x.dtype != np.float64 or not np.isfinite(x).all() for x in (values, degree, rhs)):
        raise ValueError("CUDA PCG needs finite FP64 matrix values, degrees and right hand sides")
    if (not math.isfinite(tolerance) or not 0 < tolerance < 1
            or not isinstance(max_iterations, (int, np.integer)) or not 0 < max_iterations < 2**31):
        raise ValueError("Invalid CUDA PCG convergence settings")
    return offsets, columns, values, degree, rhs, float(tolerance), int(max_iterations)


def _do_nothing():
    pass


def solve(offsets, columns, values, degree, rhs, *, tolerance, max_iterations, check=None):
    """Solve the same CSR matrix for all columns and return FP64 X plus residual statistics.

    Missing runtime/device/kernel raises CudaUnavailable. A kernel execution or convergence
    failure raises separately. `check` can raise cancellation between synchronized batches.
    """
    if check is None:
        check = _do_nothing
    elif not callable(check):
        raise TypeError("check must be callable")
    check()

    problem = _validate_problem(offsets, columns, values, degree, rhs, tolerance, max_iterations)
    if sys.platform != "win32":
        raise CudaUnavailable("The isolated CUDA backend requires Windows")
    image, image_sha = _read_image()
    started = time.perf_counter()
    try:
        driver = CudaDriver()
    except (OSError, AttributeError) as exc:
        raise CudaUnavailable(f"Cannot load the NVIDIA system driver: {exc}") from exc

    try:
        try:
            device = driver.start(_DEVICE, _CAPABILITY)
            if device is None or device["driver_api_version"] < _MINIMUM_DRIVER_API:
                raise CudaUnavailable(f"This CUDA image is limited to {_DEVICE} (SM 12.0), driver API 13.0+")
            driver.attach()
            module = driver.load(image)
            kernels = {name: driver.kernel(module, name) for name in DirectPCG.KERNELS}
        except CudaDriverError as exc:
            raise CudaUnavailable(f"Cannot start the local CUDA backend: {exc}") from exc

        check()
        solver = DirectPCG(driver, kernels, problem)
        at_solve = time.perf_counter()
        state, checks = solver.solve(_CHECK_EVERY, check)
        at_download = time.perf_counter()
        solution, stats = solver.output()
        check()
        at_cleanup = time.perf_counter()
        failed = [
            index for index, residual in enumerate(stats["true_relative_residual"])
            if state[index] != DONE or residual is None or residual > tolerance
        ]
        invalid = np.flatnonzero(~np.isfinite(solution).all(axis=0)).tolist()
        if invalid:
            failed = sorted(set(failed) | set(invalid))
        stats.update(method="CUDA Driver batched Jacobi-PCG with FP64 residual verification",
                     kernel_sha256=image_sha, device=device["name"],
                     driver_api_version=device["driver_api_version"],
                     compute_capability=list(device["compute_capability"]),
                     gpu_setup_seconds=at_solve - started,
                     gpu_solve_seconds=at_download - at_solve,
                     gpu_download_seconds=at_cleanup - at_download,
                     gpu_buffer_bytes=solver.bytes_allocated, true_residual_checks=checks,
                     failed_columns=failed)
        if failed:
            raise CudaNumericalError(stats)
    except BaseException as exc:
        try:
            driver.close()
        except CudaDriverError as cleanup:
            exc.add_note(f"CUDA cleanup also reported: {cleanup}")
        raise
    driver.close()
    check()
    return solution, stats
