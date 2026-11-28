# SPDX-License-Identifier: GPL-3.0-or-later
# Canonical candidate relocation; see SOURCE-MAP.json.
import ctypes
import hashlib
import json
import math
import os
import time
import uuid
from ctypes import wintypes as w
from pathlib import Path
import bpy

class Failure(Exception):

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def read_json(path):

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise Failure('INVALID_REQUEST', f'Duplicate JSON key: {key}')
            out[key] = value
        return out
    try:
        if os.name == 'nt':
            from .io_support import read_shared_bytes
            text = read_shared_bytes(path).decode('utf-8-sig')
        else:
            text = Path(path).read_text(encoding='utf-8-sig')
        return json.loads(text, object_pairs_hook=pairs)
    except FileNotFoundError as exc:
        raise Failure('NOT_FOUND', f'JSON file not found: {path}') from exc
    except (ValueError, UnicodeError) as exc:
        raise Failure('INVALID_REQUEST', f'Invalid UTF-8 JSON: {exc}') from exc

def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    deadline = time.monotonic() + 0.25
    while True:
        try:
            os.replace(temp, path)
            break
        except OSError as exc:
            if os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)

def digest(path, checkpoint=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            if checkpoint:
                checkpoint()
            h.update(chunk)
    return h.hexdigest()

def kernel():
    if os.name != 'nt':
        raise Failure('UNSUPPORTED', 'Transactions currently require Windows')
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {'CreateFileW': ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE), 'CloseHandle': ([w.HANDLE], w.BOOL), 'ReadFile': ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL), 'SetFilePointerEx': ([w.HANDLE, ctypes.c_int64, ctypes.c_void_p, w.DWORD], w.BOOL), 'GetFileInformationByHandle': ([w.HANDLE, ctypes.c_void_p], w.BOOL), 'SetFileInformationByHandle': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL), 'CreateMutexW': ([ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE), 'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD), 'ReleaseMutex': ([w.HANDLE], w.BOOL)}
    for name, (args, result) in signatures.items():
        getattr(k, name).argtypes = args
        getattr(k, name).restype = result
    return k

def native_path(path):
    text = os.path.abspath(path)
    if text.startswith('\\\\?\\'):
        return text
    return '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text

def fail_io(label):
    number = ctypes.get_last_error()
    code = 'CONFLICT' if number in (5, 32, 33, 80, 183) else 'NOT_FOUND' if number in (2, 3) else 'IO_ERROR'
    raise Failure(code, f'{label}: {ctypes.WinError(number)}')

def read_shared_bytes(path):
    """Read an atomic JSON generation without excluding the writer's rename handle.

    This is a status reader, NOT a transaction content guard. Share DELETE allows
    the writer to replace the path while this reader finishes the old generation.
    """
    import msvcrt
    k = kernel()
    handle = k.CreateFileW(native_path(path), 2147483648, 7, None, 3, 128, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        k.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'rb') as stream:
        return stream.read()

class FileGuard:
    """Hold actual file open; hash and rename through that SAME handle (no TOCTOU gap)."""

    def __init__(self, path, *, renameable=False, directory=False):
        self.path = Path(os.path.abspath(path))
        self.k = kernel()
        self.renameable = renameable
        access = 0 if directory else 2147483648 | (65536 if renameable else 0)
        share = 3 if directory else 1
        self.handle = self.k.CreateFileW(native_path(self.path), access, share, None, 3, 2097152 | (33554432 if directory else 128), None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            fail_io(f'Cannot guard {self.path}')
        try:
            attributes = self._information().attributes
            if attributes & 1024 or (not directory and attributes & 16):
                raise Failure('CONFLICT', 'Reparse points and non-regular transaction files are forbidden')
        except BaseException:
            self.close()
            raise

    def _information(self):

        class Info(ctypes.Structure):
            _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME), ('accessed', w.FILETIME), ('written', w.FILETIME), ('volume', w.DWORD), ('size_high', w.DWORD), ('size_low', w.DWORD), ('links', w.DWORD), ('index_high', w.DWORD), ('index_low', w.DWORD)]
        info = Info()
        if not self.k.GetFileInformationByHandle(self.handle, ctypes.byref(info)):
            fail_io('GetFileInformationByHandle')
        return info

    def identity(self):
        info = self._information()
        return {'volume': info.volume, 'file_index': info.index_high << 32 | info.index_low}

    def chunks(self, checkpoint=lambda: None):
        if not self.k.SetFilePointerEx(self.handle, 0, None, 0):
            fail_io('SetFilePointerEx')
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = w.DWORD()
        while True:
            checkpoint()
            if not self.k.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None):
                fail_io('ReadFile')
            if not count.value:
                break
            yield buffer.raw[:count.value]

    def sha256(self, checkpoint=lambda: None):
        digest = hashlib.sha256()
        for block in self.chunks(checkpoint):
            digest.update(block)
        return digest.hexdigest()

    def rename_new(self, destination):
        if not self.renameable:
            raise ValueError('Guard was not opened for rename')
        destination = Path(os.path.abspath(destination))
        if os.path.lexists(destination):
            raise Failure('CONFLICT', f'Destination already exists: {destination}')
        if self._information().links != 1:
            raise Failure('CONFLICT', 'Rename requires an unaliased single-link transaction file')
        encoded = str(destination).encode('utf-16-le')

        class RenameInfo(ctypes.Structure):
            _fields_ = [('Flags', w.DWORD), ('RootDirectory', w.HANDLE), ('FileNameLength', w.DWORD), ('FileName', w.WCHAR * (len(encoded) // 2 + 1))]
        info = RenameInfo()
        info.Flags = 0
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(info) + RenameInfo.FileName.offset, encoded, len(encoded))
        if not self.k.SetFileInformationByHandle(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info)):
            fail_io(f'Rename without replacement to {destination}')
        self.path = destination

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

def finished(result):
    if result != {'FINISHED'}:
        raise Failure('VALIDATION_FAILED', 'Operator did not finish: ' + str(result))

def find(collection, name, write=False):
    matches = [x for x in collection if x.name == name]
    if len(matches) != 1:
        raise Failure('CONFLICT' if matches else 'NOT_FOUND', 'Name must resolve uniquely: ' + name)
    item = matches[0]
    if write and (item.library or item.override_library):
        raise Failure('UNSUPPORTED', 'Editing linked/override data is outside scene preparation: ' + name)
    return item

def named(item, name):
    item.name = name
    if item.name != name:
        raise Failure('CONFLICT', 'Blender changed requested name: ' + name)
    return item
