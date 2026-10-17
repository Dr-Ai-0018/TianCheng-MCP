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


def _windows_dacl_is_private(dacl, sid: str, adv, kernel) -> bool:
    """Compare actual ACE trustee SIDs, independent of SDDL account aliases."""
    import ctypes
    from ctypes import wintypes as w

    class AclSize(ctypes.Structure):
        _fields_ = [('count', w.DWORD), ('used', w.DWORD), ('free', w.DWORD)]

    class AceHeader(ctypes.Structure):
        _fields_ = [('kind', w.BYTE), ('flags', w.BYTE), ('size', w.WORD)]

    adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.c_int]
    adv.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    size = AclSize()
    if not dacl or not adv.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
        return False
    if not size.count:
        return False
    for index in range(size.count):
        ace = ctypes.c_void_p()
        if not adv.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
            return False
        header = AceHeader.from_address(ace.value)
        if header.kind == 1:  # Standard deny ACE; it cannot grant access.
            continue
        if header.kind != 0 or header.size < 12:
            return False  # Object, callback and unknown ACEs remain rejected.
        trustee = w.LPWSTR()
        if not adv.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(trustee)):
            return False
        try:
            if trustee.value not in (sid, 'S-1-5-18'):
                return False
        finally:
            kernel.LocalFree(trustee)
    return True


def _windows_descriptor(path: Path, create: bool = False, *, create_file: bool = False) -> None:
    # Create a protected inheritable DACL on NEW paths only. Existing ACLs
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
        try:
            if create_file:
                kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(Attributes), w.DWORD, w.DWORD, w.HANDLE]
                kernel.CreateFileW.restype = w.HANDLE
                handle = kernel.CreateFileW(str(path), 0xC0000000, 7, ctypes.byref(attrs), 1, 0x00200080, None)
                if handle == ctypes.c_void_p(-1).value:
                    if ctypes.get_last_error() not in (80, 183):
                        raise PermissionError('Cannot create private record file')
                    # Another producer can create store.lock concurrently. The
                    # existing file still has to pass the same owner/ACL checks.
                else:
                    kernel.CloseHandle(handle)
            else:
                kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(Attributes)]
                if not kernel.CreateDirectoryW(str(path), ctypes.byref(attrs)):
                    raise PermissionError('Cannot create private record directory')
        finally:
            kernel.LocalFree(descriptor)
    owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    if adv.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)):
        raise PermissionError('Cannot inspect private record ACL')
    owner_text = w.LPWSTR()
    try:
        if not adv.ConvertSidToStringSidW(owner, ctypes.byref(owner_text)):
            raise PermissionError('Cannot inspect private record owner')
        if owner_text.value != sid or not dacl.value:
            raise PermissionError('Records must be owned by the current user')
        if not _windows_dacl_is_private(dacl, sid, adv, kernel):
            raise PermissionError('Record ACL grants access outside the private boundary')
    finally:
        if owner_text: kernel.LocalFree(owner_text)
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
    elif os.name == 'nt' and mode != 'r+b':
        # Elevated Windows accounts may default new file ownership to the
        # Administrators group. Set the actual user owner at creation, without
        # modifying or trusting an existing file's permissions.
        _windows_descriptor(path, create=True, create_file=True)
    flags = os.O_RDWR | (os.O_CREAT if mode != 'r+b' and os.name != 'nt' else 0)
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
