from __future__ import annotations

import ctypes
import os

import pytest

from tiancheng_mcp.execution_paths import _windows_dacl_is_private, private_directory, private_open, validate_private


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows ACL trustee identity')
@pytest.mark.parametrize(('sid', 'aces', 'expected'), [
    ('S-1-5-32-544', '(A;;FA;;;BA)(A;;FA;;;SY)', True),
    ('S-1-5-18', '(A;;FA;;;SY)', True),
    ('S-1-5-21-1-2-3-500', '(A;;FA;;;S-1-5-21-1-2-3-500)(A;;FA;;;SY)', True),
    ('S-1-5-32-544', '(D;;FA;;;WD)(A;;FA;;;BA)', True),
    ('S-1-5-32-544', '(A;;FA;;;BA)(A;;FR;;;BU)', False),
    ('S-1-5-32-544', '(A;;FA;;;WD)', False),
    ('S-1-5-21-1-2-3-500', '(A;;FA;;;S-1-5-21-1-2-3-501)', False),
    ('S-1-5-32-544', '', False),
    ('S-1-5-32-544', '(OA;;FA;00112233-4455-6677-8899-aabbccddeeff;;BA)', False),
])
def test_acl_compares_actual_sid_and_keeps_private_boundary(sid, aces, expected):
    from ctypes import wintypes as w

    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(w.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        'D:P' + aces, 1, ctypes.byref(descriptor), None)
    try:
        present, defaulted = w.BOOL(), w.BOOL()
        assert adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))
        assert present.value
        assert _windows_dacl_is_private(dacl, sid, adv, kernel) is expected
    finally:
        kernel.LocalFree(descriptor)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows ACL trustee identity')
def test_null_dacl_is_not_private():
    assert not _windows_dacl_is_private(
        ctypes.c_void_p(), 'S-1-5-18', ctypes.WinDLL('advapi32'), ctypes.WinDLL('kernel32'))


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows file owner creation')
def test_new_files_have_private_current_user_owner_and_keep_existing_bytes(tmp_path):
    root = private_directory(tmp_path / 'records')
    path = root / 'store.lock'
    with private_open(path) as handle:
        handle.write(b'private-marker')
    validate_private(path)
    with private_open(path, 'r+b') as handle:
        assert handle.read() == b'private-marker'
    validate_private(path)
    with pytest.raises(FileNotFoundError):
        private_open(root / 'missing.lock', 'r+b')


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows file owner creation')
def test_file_creation_race_validates_existing_file_without_replacing_it(tmp_path):
    from tiancheng_mcp.execution_paths import _windows_descriptor
    root = private_directory(tmp_path / 'records')
    path = root / 'store.lock'
    with private_open(path) as handle:
        handle.write(b'preserved')
    _windows_descriptor(path, create=True, create_file=True)
    assert path.read_bytes() == b'preserved'
    validate_private(path)
