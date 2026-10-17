from __future__ import annotations

import ctypes
import os

import pytest

from tiancheng_mcp.execution_paths import _windows_dacl_is_private


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
