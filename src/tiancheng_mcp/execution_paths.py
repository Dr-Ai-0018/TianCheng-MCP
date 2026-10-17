"""Private local paths and process locks shared by writer and read-only viewer."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import stat
import time


def checked_path(path: Path, root: Path | None = None) -> Path:
    path = Path(os.path.abspath(path))
    if root is not None and path != root and root not in path.parents:
        raise PermissionError('Record path is outside the private root')
    for p in (*reversed(path.parents), path):
        try:
            info = p.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PermissionError('Record paths cannot follow links or reparse points')
    return path


def validate_root(root: Path, workspace: Path | None = None) -> Path:
    root = checked_path(root)
    if workspace is not None:
        workspace = Path(workspace).resolve()
        if root == workspace or root in workspace.parents or workspace in root.parents:
            raise PermissionError('Record storage must not overlap the workspace')
    return root


def _windows_descriptor(path: Path, create: bool = False) -> None:
    # Create a protected inheritable DACL on NEW directories only. Existing ACLs
    # are inspected, never rewritten or relaxed to satisfy a test.
    import ctypes
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = w.DWORD
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.c_void_p]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise PermissionError('Cannot inspect record owner')
    try:
        needed = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise PermissionError('Cannot inspect record owner')
        sid_ptr = ctypes.c_void_p.from_buffer(buffer).value
        sid_text = w.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_text)):
            raise PermissionError('Cannot inspect record owner')
        sid = sid_text.value
        kernel.LocalFree(sid_text)
    finally:
        kernel.CloseHandle(token)
    descriptor = ctypes.c_void_p()
    if create:
        sddl = f'O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise PermissionError('Cannot create private record ACL')
        class Attributes(ctypes.Structure):
            _fields_ = [('length', w.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', w.BOOL)]
        attrs = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(Attributes)]
        try:
            if not kernel.CreateDirectoryW(str(path), ctypes.byref(attrs)):
                raise PermissionError('Cannot create private record directory')
        finally:
            kernel.LocalFree(descriptor)
    owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    if adv.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)):
        raise PermissionError('Cannot inspect private record ACL')
    owner_text, text = w.LPWSTR(), w.LPWSTR()
    try:
        if not adv.ConvertSidToStringSidW(owner, ctypes.byref(owner_text)):
            raise PermissionError('Cannot inspect private record owner')
        if owner_text.value != sid or not dacl.value:
            raise PermissionError('Records must be owned by the current user')
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None):
            raise PermissionError('Cannot inspect private record ACL')
        import re
        entries = re.findall(r'\(([^()]*)\)', text.value or '')
        # Reject null/unknown/conditional/object ACLs conservatively. Only the
        # current account and SYSTEM may receive access; deny ACEs are harmless.
        if not entries or any(parts[0] != 'D' and (parts[0] != 'A' or parts[-1] not in (sid, 'SY', 'S-1-5-18'))
                              for entry in entries for parts in [entry.split(';')]):
            raise PermissionError('Record ACL grants access outside the private boundary')
    finally:
        if owner_text: kernel.LocalFree(owner_text)
        if text: kernel.LocalFree(text)
        kernel.LocalFree(descriptor)


def private_directory(path: Path, root: Path | None = None) -> Path:
    path = checked_path(path, root)
    if not path.exists():
        if not path.parent.exists():
            private_directory(path.parent)
        if os.name == 'nt':
            _windows_descriptor(path, create=True)
        else:
            path.mkdir(mode=0o700)
    validate_private(path, directory=True)
    return path


def validate_private(path: Path, *, directory: bool = False) -> None:
    checked_path(path)
    info = path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise PermissionError('Records require ordinary private files and directories')
    if os.name == 'nt':
        _windows_descriptor(path)
    elif info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError('Records require current ownership and private permissions')


def private_open(path: Path, mode: str = 'a+b'):
    checked_path(path)
    if path.exists():
        validate_private(path)
    flags = os.O_RDWR | (os.O_CREAT if mode != 'r+b' else 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        validate_private(path)
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise


class FileLock:
    def __init__(self, path: Path, *, create: bool = True) -> None:
        self.path, self.create, self.handle = path, create, None

    def acquire(self, timeout: float = 0) -> 'FileLock':
        handle = private_open(self.path, 'a+b' if self.create else 'r+b')
        if self.create and self.path.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise BlockingIOError('Record lock is busy') from None
                time.sleep(0.001)

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self):
        return self.acquire(0.25)

    def __exit__(self, *_):
        self.release()


def producer_live(path: Path) -> bool | None:
    try:
        lock = FileLock(path / 'owner.lock', create=False).acquire()
    except BlockingIOError:
        return True
    except (OSError, ValueError):
        return None
    else:
        lock.release()
        return False


def atomic_json(path: Path, payload: bytes) -> None:
    import uuid
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    if path.exists(): validate_private(path)
    try:
        with private_open(temporary) as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        checked_path(path)
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError): temporary.unlink()
