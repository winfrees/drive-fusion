"""Batch directory reads on Windows.

``GetFileInformationByHandleEx`` returns many entries per call — names, sizes,
timestamps, and attributes together — instead of a ``FindNextFile`` round trip
plus a ``stat`` per file. At 50 million files that difference is the whole
exFAT budget (docs/PLAN.md §6.4).

The handle comes from ``winio`` and the buffers are parsed by ``records``, so
this module holds only the paging loop.
"""

from __future__ import annotations

import ctypes
import sys

from drivefusion.core.enum import winio
from drivefusion.core.enum.records import (
    DirEntry,
    parse_full_dir_info,
    parse_id_both_dir_info,
)

WINDOWS = sys.platform == "win32"

# FILE_INFO_BY_HANDLE_CLASS values.
FileIdBothDirectoryInfo = 10
FileIdBothDirectoryRestartInfo = 11
FileFullDirectoryInfo = 14
FileFullDirectoryRestartInfo = 15

ERROR_NO_MORE_FILES = 18

#: Bigger buffers mean fewer user/kernel transitions per directory.
BUFFER_BYTES = 64 * 1024

#: Entries every FAT-family listing contains and no catalog should.
SKIP_NAMES = frozenset({".", ".."})


def list_directory(path: str, *, with_file_ids: bool | None = None) -> list[DirEntry]:
    """List one directory in batches.

    ``with_file_ids`` selects the NTFS variant that carries persistent file
    ids. On exFAT the id field is not dependable, so the plain variant is used
    and identity comes from path plus metadata instead (§6.4).
    """
    if not WINDOWS:  # pragma: no cover - Windows-only path
        raise winio.VolumeAccessError("batch directory reads require Windows")

    if with_file_ids is None:
        with_file_ids = True
    restart_class = (
        FileIdBothDirectoryRestartInfo if with_file_ids else FileFullDirectoryRestartInfo
    )
    continue_class = (
        FileIdBothDirectoryInfo if with_file_ids else FileFullDirectoryInfo
    )
    parse = parse_id_both_dir_info if with_file_ids else parse_full_dir_info

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = winio.open_directory(winio_long_path(path))
    entries: list[DirEntry] = []
    buffer = ctypes.create_string_buffer(BUFFER_BYTES)
    info_class = restart_class

    try:
        while True:
            ok = kernel32.GetFileInformationByHandleEx(
                handle,
                ctypes.c_int(info_class),
                buffer,
                ctypes.c_ulong(BUFFER_BYTES),
            )
            if not ok:
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    break
                raise winio.VolumeAccessError(
                    f"cannot list {path}: WinError {error}", error
                )
            for entry in parse(buffer.raw):
                if entry.name not in SKIP_NAMES:
                    entries.append(entry)
            info_class = continue_class
    finally:
        winio.close_handle(handle)

    return entries


def winio_long_path(path: str) -> str:
    """Prefix for long-path support without importing the whole gateway."""
    from drivefusion.core.fsio import long_path

    return long_path(path)
