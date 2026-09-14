"""Windows discretionary ACLs for private node records (chmod is insufficient).

Only the running user, SYSTEM and local administrators receive access.
The caller must own the specific file/directory; errors are fail-closed and
contain no filesystem paths or account names. No shell commands are used.
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
from pathlib import Path

_DACL = 4
_PROTECTED_DACL = 0x80000000


@lru_cache(maxsize=1)
def _api():
    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    pointer = c.c_void_p
    pp = c.POINTER(pointer)
    signatures = {
        "OpenProcessToken": ([w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL),
        "GetTokenInformation": ([w.HANDLE, c.c_int, pointer, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
        "ConvertSidToStringSidW": ([pointer, c.POINTER(w.LPWSTR)], w.BOOL),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": ([w.LPCWSTR, w.DWORD, pp, pointer], w.BOOL),
        "GetSecurityDescriptorDacl": ([pointer, c.POINTER(w.BOOL), pp, c.POINTER(w.BOOL)], w.BOOL),
        "SetNamedSecurityInfoW": ([w.LPWSTR, c.c_int, w.DWORD, pointer, pointer, pointer, pointer], w.DWORD),
        "GetNamedSecurityInfoW": ([w.LPCWSTR, c.c_int, w.DWORD, pp, pp, pp, pp, pp], w.DWORD),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW": ([pointer, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), pointer], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(adv, name)
        fn.argtypes, fn.restype = args, result
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], w.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    return adv, kernel


@lru_cache(maxsize=1)
def current_user_sid() -> str:
    adv, kernel = _api()
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)):
        raise OSError("private_acl_token_unavailable")
    try:
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
            raise OSError("private_acl_token_unavailable")
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]  # TOKEN_USER starts with SID_AND_ATTRIBUTES.
        string = w.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid, c.byref(string)):
            raise OSError("private_acl_sid_unavailable")
        try:
            return string.value
        finally:
            kernel.LocalFree(string)
    finally:
        kernel.CloseHandle(token)


def read_windows_dacl(path: str | Path) -> str:
    adv, kernel = _api()
    descriptor = c.c_void_p()
    if adv.GetNamedSecurityInfoW(str(path), 1, _DACL, None, None, None, None, c.byref(descriptor)):
        raise OSError("private_acl_read_failed")
    string = w.LPWSTR()
    try:
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, _DACL, c.byref(string), None
        ):
            raise OSError("private_acl_read_failed")
        return string.value
    finally:
        if string:
            kernel.LocalFree(string)
        kernel.LocalFree(descriptor)


def restrict_windows_acl(path: str | Path, *, directory: bool = False) -> None:
    adv, kernel = _api()
    inheritance = "OICI" if directory else ""
    sddl = "D:P" + "".join(f"(A;{inheritance};FA;;;{sid})" for sid in (
        "SY", "BA", current_user_sid()
    ))
    # Avoid repeatedly propagating identical directory ACLs through the spool.
    if directory and read_windows_dacl(path) == sddl:
        return
    descriptor = c.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
        raise OSError("private_acl_invalid")
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)) or not present or not acl:
            raise OSError("private_acl_invalid")
        if adv.SetNamedSecurityInfoW(str(path), 1, _DACL | _PROTECTED_DACL, None, None, acl, None):
            raise OSError("private_acl_write_failed")
    finally:
        kernel.LocalFree(descriptor)
