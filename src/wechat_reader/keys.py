"""Read explicitly selected, locally protected key material without persisting secrets."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


class KeyUnavailable(ValueError):
    pass


def unprotect(payload: bytes) -> bytes:
    if os.name != "nt":
        raise KeyUnavailable("DPAPI key files require the owning Windows login")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    backing = ctypes.create_string_buffer(payload)
    incoming = Blob(len(payload), ctypes.cast(backing, ctypes.c_void_p))
    outgoing = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(Blob)]
    library.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not library.CryptUnprotectData(ctypes.byref(incoming), None, None,
            None, None, 1, ctypes.byref(outgoing)):
        raise KeyUnavailable("Windows could not unlock the selected key file")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def load_keys(path: Path) -> dict:
    """Accept DPAPI JSON or an explicit JSON key file. Never include values in errors."""
    try:
        payload = path.read_bytes()
        if not payload.lstrip().startswith(b"{"):
            payload = unprotect(payload)
        values = json.loads(payload)
        key = bytes.fromhex(values["db"])
        if len(key) != 32:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise KeyUnavailable("Selected key file is missing, invalid, or cannot be unlocked") from None
    return values


def database_key(path: Path) -> bytes:
    return bytes.fromhex(load_keys(path)["db"])
