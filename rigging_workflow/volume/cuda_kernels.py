# SPDX-License-Identifier: GPL-3.0-or-later
"""CUDA Driver bindings and batched PCG launches for the isolated Windows backend."""

import ctypes as c
import math

import numpy as np


INT = c.c_int
UINT = c.c_uint
SIZE = c.c_size_t
PTR = c.c_void_p
CHAR = c.c_char_p
DEV = c.c_uint64

DONE, ACTIVE, VERIFY, FAILED = range(4)


class CudaDriverError(RuntimeError):
    def __init__(self, operation, status, name):
        self.operation = operation
        self.status = status
        super().__init__(f"{operation}: {name}")


def _bind(library, name, *types):
    function = getattr(library, name)
    function.argtypes = types
    function.restype = INT
    return function


class CudaDriver:
    def __init__(self):
        # LOAD_LIBRARY_SEARCH_SYSTEM32 avoids resolving a user-supplied DLL on PATH.
        library = self.library = c.WinDLL("nvcuda.dll", winmode=0x00000800)
        self.version = _bind(library, "cuDriverGetVersion", c.POINTER(INT))
        self.error_name = _bind(library, "cuGetErrorName", INT, c.POINTER(CHAR))
        self.init = _bind(library, "cuInit", UINT)
        self.get_device = _bind(library, "cuDeviceGet", c.POINTER(INT), INT)
        self.device_count = _bind(library, "cuDeviceGetCount", c.POINTER(INT))
        self.attribute = _bind(library, "cuDeviceGetAttribute", c.POINTER(INT), INT, INT)
        self.device_name = _bind(library, "cuDeviceGetName", PTR, INT, INT)
        self.retain = _bind(library, "cuDevicePrimaryCtxRetain", c.POINTER(PTR), INT)
        self.get_current = _bind(library, "cuCtxGetCurrent", c.POINTER(PTR))
        self.set_current = _bind(library, "cuCtxSetCurrent", PTR)
        self.release = _bind(library, "cuDevicePrimaryCtxRelease_v2", INT)
        self.module_load = _bind(library, "cuModuleLoadData", c.POINTER(PTR), PTR)
        self.module_unload = _bind(library, "cuModuleUnload", PTR)
        self.get_function = _bind(library, "cuModuleGetFunction", c.POINTER(PTR), PTR, CHAR)
        self.alloc = _bind(library, "cuMemAlloc_v2", c.POINTER(DEV), SIZE)
        self.free = _bind(library, "cuMemFree_v2", DEV)
        self.h_to_d = _bind(library, "cuMemcpyHtoD_v2", DEV, PTR, SIZE)
        self.d_to_h = _bind(library, "cuMemcpyDtoH_v2", PTR, DEV, SIZE)
        self.zero_words = _bind(library, "cuMemsetD32Async", DEV, UINT, SIZE, PTR)
        self.launch_kernel = _bind(library, "cuLaunchKernel", PTR, UINT, UINT, UINT,
                                   UINT, UINT, UINT, UINT, PTR, c.POINTER(PTR), PTR)
        self.sync = _bind(library, "cuCtxSynchronize")
        self.previous_context = PTR()
        self.context = None
        self.device = None
        self.modules = []
        self.allocations = []

    def check(self, function, *args):
        status = function(*args)
        if status:
            name = CHAR()
            self.error_name(status, c.byref(name))
            detail = name.value.decode() if name.value else str(status)
            raise CudaDriverError(function.__name__, status, detail)

    def start(self, target_name, target_capability):
        self.check(self.init, 0)
        version, count = INT(), INT()
        self.check(self.version, c.byref(version))
        self.check(self.device_count, c.byref(count))
        for ordinal in range(count.value):
            device, major, minor = INT(), INT(), INT()
            self.check(self.get_device, c.byref(device), ordinal)
            self.check(self.attribute, c.byref(major), 75, device)
            self.check(self.attribute, c.byref(minor), 76, device)
            capability = (major.value, minor.value)
            if capability != target_capability:
                continue
            buffer = c.create_string_buffer(128)
            self.check(self.device_name, buffer, len(buffer), device)
            name = buffer.value.decode()
            if name == target_name:
                self.device = device
                return {"driver_api_version": version.value, "compute_capability": capability, "name": name}
        return None

    def attach(self):
        self.check(self.get_current, c.byref(self.previous_context))
        context = PTR()
        self.check(self.retain, c.byref(context), self.device)
        self.context = context
        self.check(self.set_current, context)

    def load(self, cubin):
        module = PTR()
        self.check(self.module_load, c.byref(module), cubin)
        self.modules.append(module)
        return module

    def kernel(self, module, name):
        function = PTR()
        self.check(self.get_function, c.byref(function), module, name.encode("ascii"))
        return function

    def buffer(self, size):
        pointer = DEV()
        self.check(self.alloc, c.byref(pointer), size)
        self.allocations.append(pointer)
        return pointer

    def copy_in(self, pointer, address, size):
        self.check(self.h_to_d, pointer, address, size)

    def copy_out(self, address, pointer, size):
        self.check(self.d_to_h, address, pointer, size)

    def zero(self, pointer, byte_count):
        self.check(self.zero_words, pointer, 0, byte_count // 4, None)

    def launch(self, function, grid, block, *args):
        params = (PTR * len(args))(*(c.addressof(arg) for arg in args))
        self.check(self.launch_kernel, function, *grid, *block, 0, None, params, None)

    def close(self):
        if self.context is None:
            return

        errors = []

        def release(function, value):
            try:
                self.check(function, value)
            except CudaDriverError as exc:
                errors.append(exc)

        for pointer in reversed(self.allocations):
            release(self.free, pointer)
        for module in reversed(self.modules):
            release(self.module_unload, module)
        release(self.set_current, self.previous_context)
        release(self.release, self.device)
        self.context = None
        self.allocations.clear()
        self.modules.clear()
        if errors:
            for extra in errors[1:]:
                errors[0].add_note(str(extra))
            raise errors[0]


class DirectPCG:
    KERNELS = (
        "csr_float", "reduce_float", "choose_step", "advance_solution", "choose_direction",
        "advance_direction", "commit_correction", "csr_true_residual", "reduce_true_residual",
        "finish_verification", "restart_vectors", "resume_bones",
    )

    def __init__(self, driver, kernels, problem):
        self.driver = driver
        self.kernels = kernels
        offsets, columns, values, degrees, rhs, tolerance, max_iterations = problem
        self.rows, self.bones = rhs.shape
        self.pitch = (self.bones + 31) // 32 * 32
        self.elements = self.rows * self.pitch
        self.dense4 = self.elements * 4
        self.dense8 = self.elements * 8
        self.stats4 = self.pitch * 4
        self.stats8 = self.pitch * 8
        self.n = INT(self.rows)
        self.stride = INT(self.pitch)
        self.grid = ((self.elements + 255) // 256, 1, 1)
        self.reduce_grid = ((self.rows + 255) // 256, self.pitch // 32, 1)
        self.reduce_block = (32, 8, 1)
        self.column_block = (self.pitch, 1, 1)
        self.max_iterations = INT(max_iterations)
        self.tolerance = tolerance
        self.tolerance2 = c.c_double(tolerance**2)
        self.factor2 = c.c_double(max(1e-4, math.sqrt(tolerance)) ** 2)
        self.bytes_allocated = 0

        self.offsets = self.upload(offsets)
        self.columns = self.upload(columns)
        self.values32 = self.upload(values.astype(np.float32))
        self.values64 = self.upload(values)
        inverse_degree = (1.0 / degrees).astype(np.float32)
        self.inverse_degree = self.upload(inverse_degree)
        padded_rhs = np.zeros((self.rows, self.pitch), dtype=np.float64)
        padded_rhs[:, :self.bones] = rhs
        norm2 = (padded_rhs * padded_rhs).sum(axis=0)
        residual = padded_rhs.astype(np.float32)
        direction = residual * inverse_degree[:, None]
        self.residual = self.upload(residual)
        self.direction = self.upload(direction)
        self.rho = self.upload((residual * direction).sum(axis=0, dtype=np.float64).astype(np.float32))
        self.rhs = self.upload(padded_rhs)
        self.original_norm2 = self.upload(norm2)
        self.target = self.upload(norm2 * self.factor2.value)
        self.state = self.upload(np.where(norm2 > 0.0, ACTIVE, DONE).astype(np.int32))
        self.product = self.allocate(self.dense4)
        self.correction = self.allocate(self.dense4, initialize=True)
        self.solution = self.allocate(self.dense8, initialize=True)
        self.true_residual = self.allocate(self.dense8)
        self.step = self.allocate(self.stats4)
        self.beta = self.allocate(self.stats4)
        self.iterations = self.allocate(self.stats4, initialize=True)
        self.restarts = self.allocate(self.stats4, initialize=True)
        self.relative = self.allocate(self.stats8, initialize=True)
        self.curvature = self.allocate(self.stats4 * 3)
        self.next_rho = DEV(self.curvature.value + self.stats4)
        self.norm2 = DEV(self.curvature.value + self.stats4 * 2)
        self.true_norm2 = self.allocate(self.stats8 * 2)
        self.true_rho = DEV(self.true_norm2.value + self.stats8)
        driver.check(driver.sync)

    def allocate(self, size, initialize=False):
        target = self.driver.buffer(size)
        self.bytes_allocated += size
        if initialize:
            self.driver.zero(target, size)
        return target

    def upload(self, array):
        array = np.ascontiguousarray(array)
        target = self.allocate(array.nbytes)
        self.driver.copy_in(target, array.ctypes.data, array.nbytes)
        return target

    def download(self, target, array):
        self.driver.copy_out(array.ctypes.data, target, array.nbytes)
        return array

    def vector_kernel(self, name, *args):
        self.driver.launch(self.kernels[name], self.grid, (256, 1, 1), *args, self.n, self.stride)

    def column_kernel(self, name, *args):
        self.driver.launch(self.kernels[name], (1, 1, 1), self.column_block, *args)

    def reduce_float(self, x, other, product, square, precondition):
        self.driver.launch(self.kernels["reduce_float"], self.reduce_grid, self.reduce_block,
                           x, other, self.inverse_degree, self.state, product, square,
                           self.n, self.stride, INT(precondition))

    def step_pcg(self):
        self.driver.zero(self.curvature, self.stats4 * 3)
        self.vector_kernel("csr_float", self.offsets, self.columns, self.values32, self.direction,
                           self.product, self.state)
        self.reduce_float(self.direction, self.product, self.curvature, self.norm2, precondition=0)
        self.column_kernel("choose_step", self.state, self.iterations, self.rho, self.curvature, self.step)
        self.vector_kernel("advance_solution", self.correction, self.residual, self.direction,
                           self.product, self.state, self.step)
        self.reduce_float(self.residual, self.product, self.next_rho, self.norm2, precondition=1)
        self.column_kernel("choose_direction", self.state, self.rho, self.next_rho, self.norm2,
                           self.target, self.iterations, self.beta, self.max_iterations)
        self.vector_kernel("advance_direction", self.direction, self.residual, self.inverse_degree,
                           self.state, self.beta)

    def verify(self):
        self.driver.zero(self.true_norm2, self.stats8 * 2)
        self.vector_kernel("commit_correction", self.solution, self.correction, self.state)
        self.vector_kernel("csr_true_residual", self.offsets, self.columns, self.values64, self.solution,
                           self.rhs, self.true_residual, self.state)
        self.driver.launch(self.kernels["reduce_true_residual"], self.reduce_grid, self.reduce_block,
                           self.true_residual, self.inverse_degree, self.state, self.true_norm2,
                           self.true_rho, self.n, self.stride)
        self.column_kernel("finish_verification", self.state, self.iterations, self.restarts, self.rho,
                           self.target, self.relative, self.original_norm2, self.true_norm2,
                           self.true_rho, self.max_iterations, self.tolerance2, self.factor2)
        self.vector_kernel("restart_vectors", self.residual, self.direction, self.true_residual,
                           self.inverse_degree, self.state)
        self.column_kernel("resume_bones", self.state)

    def solve(self, check_every, check):
        check()
        status = np.empty(self.pitch, dtype=np.int32)
        self.download(self.state, status)
        checks = 0
        while np.any(status == ACTIVE):
            for _ in range(check_every):
                self.step_pcg()
            self.download(self.state, status)
            check()
            if np.any(status == VERIFY):
                self.verify()
                self.download(self.state, status)
                check()
                checks += 1
        return status, checks

    def output(self):
        field = self.download(self.solution, np.empty((self.rows, self.pitch), dtype=np.float64))
        stats = {}
        for name, target, dtype in (
            ("iterations", self.iterations, np.int32),
            ("restarts", self.restarts, np.int32),
            ("true_relative_residual", self.relative, np.float64),
        ):
            array = self.download(target, np.empty(self.pitch, dtype=dtype))[:self.bones]
            stats[name] = [x.item() if np.isfinite(x) else None for x in array]
        return np.ascontiguousarray(field[:, :self.bones]), stats

