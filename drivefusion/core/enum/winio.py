"""Thin Win32 shims for volume-level enumeration.

This module is on the raw-handle allowlist in ``tools/ro_lint.py``. Everything
it opens is opened for reading only, and every structure it returns is parsed
by the pure functions in ``records.py`` and ``journal.py`` rather than here, so
the untestable surface stays as small as it can be.

Volume handles need administrator rights, which is why the enumeration helper
(``dfscan-helper``) exists: the main application runs as invoker and never
elevates (docs/PLAN.md §6.5).
"""

from __future__ import annotations

import ctypes
import sys
from typing import Iterator

from drivefusion.core.enum.records import UsnRecord, parse_usn_records

WINDOWS = sys.platform == "win32"

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_HANDLE_EOF = 38
ERROR_NO_MORE_FILES = 18


class VolumeAccessError(RuntimeError):
    """Raised when a volume handle cannot be opened or an IOCTL fails."""

    def __init__(self, message: str, winerror: int = 0) -> None:
        super().__init__(message)
        self.winerror = winerror


def _kernel32():
    if not WINDOWS:  # pragma: no cover - guarded by callers
        raise VolumeAccessError("volume enumeration requires Windows")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def open_volume(device_path: str):
    """Open a volume for reading. ``GENERIC_READ`` and nothing else.

    A handle opened without write access cannot write, at the OS level,
    whatever the code above it does.
    """
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        device_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise VolumeAccessError(
            f"cannot open {device_path}: WinError {error}"
            + (" (administrator rights required)" if error == 5 else ""),
            error,
        )
    return handle


def open_directory(path: str):
    """Open a directory for listing, read access only.

    ``FILE_FLAG_BACKUP_SEMANTICS`` is what makes a directory openable at all on
    Windows; it grants no write capability. Centralised here so the directory
    reader in ``dirinfo.py`` needs no raw-handle allowlist entry of its own.
    """
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise VolumeAccessError(f"cannot open directory {path}: WinError {error}", error)
    return handle


def close_handle(handle) -> None:
    _kernel32().CloseHandle(handle)


def device_ioctl(
    device_path: str, code: int, payload: bytes, out_size: int
) -> bytes | None:
    """One-shot IOCTL against a volume. Returns None if unsupported."""
    handle = open_volume(device_path)
    try:
        return _ioctl(handle, code, payload, out_size)
    except VolumeAccessError:
        return None
    finally:
        close_handle(handle)


def _ioctl(handle, code: int, payload: bytes, out_size: int) -> bytes:
    kernel32 = _kernel32()
    out_buffer = ctypes.create_string_buffer(out_size)
    returned = ctypes.c_ulong()
    in_buffer = ctypes.create_string_buffer(payload, len(payload)) if payload else None

    ok = kernel32.DeviceIoControl(
        handle,
        ctypes.c_ulong(code),
        in_buffer,
        ctypes.c_ulong(len(payload)),
        out_buffer,
        ctypes.c_ulong(out_size),
        ctypes.byref(returned),
        None,
    )
    if not ok:
        error = ctypes.get_last_error()
        raise VolumeAccessError(f"IOCTL {code:#x} failed: WinError {error}", error)
    return out_buffer.raw[: returned.value]


def stream_usn(
    device_path: str,
    code: int,
    initial_payload: bytes,
    *,
    buffer_bytes: int,
    next_payload,
) -> Iterator[UsnRecord]:
    """Drive a paged USN IOCTL to exhaustion, yielding parsed records.

    Both bulk enumeration and journal reads work the same way: each call
    returns an 8-byte cursor followed by records, and the cursor feeds the next
    call until no records come back. The paging loop lives here; the record
    layout lives in ``records.py``.
    """
    handle = open_volume(device_path)
    payload = initial_payload
    try:
        while True:
            try:
                data = _ioctl(handle, code, payload, buffer_bytes)
            except VolumeAccessError as exc:
                if exc.winerror in (ERROR_HANDLE_EOF, ERROR_NO_MORE_FILES):
                    return
                raise

            if len(data) <= 8:
                return  # cursor only: nothing further to read

            yielded = False
            for record in parse_usn_records(data, offset=8):
                yielded = True
                yield record

            cursor = int.from_bytes(data[:8], "little", signed=True)
            payload = next_payload(cursor)
            if not yielded:
                return
    finally:
        close_handle(handle)
