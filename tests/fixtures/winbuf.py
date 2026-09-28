"""Builders for the Win32 buffers the enumeration backends parse.

These construct byte-exact ``USN_RECORD_V2``/``V3`` and directory-info buffers
from the documented layouts, so the parsers can be tested on any platform. They
are written independently of the parsers — laying the bytes out by explicit
offset rather than reusing the parser's struct formats — so a wrong offset in
one is not mirrored by the same wrong offset in the other.
"""

from __future__ import annotations

import struct

FILETIME_EPOCH_DELTA = 116_444_736_000_000_000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010


def ns_to_filetime(nanoseconds: int) -> int:
    if nanoseconds <= 0:
        return 0
    return nanoseconds // 100 + FILETIME_EPOCH_DELTA


def usn_record_v2(
    *,
    frn: int,
    parent_frn: int,
    name: str,
    attributes: int = 0,
    usn: int = 0,
    timestamp_ns: int = 0,
    reason: int = 0,
) -> bytes:
    encoded = name.encode("utf-16-le")
    name_offset = 60
    length = name_offset + len(encoded)
    padding = (-length) % 8  # records are 8-byte aligned
    total = length + padding

    buffer = bytearray(total)
    struct.pack_into("<I", buffer, 0, total)
    struct.pack_into("<H", buffer, 4, 2)          # MajorVersion
    struct.pack_into("<H", buffer, 6, 0)          # MinorVersion
    struct.pack_into("<Q", buffer, 8, frn)
    struct.pack_into("<Q", buffer, 16, parent_frn)
    struct.pack_into("<q", buffer, 24, usn)
    struct.pack_into("<q", buffer, 32, ns_to_filetime(timestamp_ns))
    struct.pack_into("<I", buffer, 40, reason)
    struct.pack_into("<I", buffer, 44, 0)         # SourceInfo
    struct.pack_into("<I", buffer, 48, 0)         # SecurityId
    struct.pack_into("<I", buffer, 52, attributes)
    struct.pack_into("<H", buffer, 56, len(encoded))
    struct.pack_into("<H", buffer, 58, name_offset)
    buffer[name_offset : name_offset + len(encoded)] = encoded
    return bytes(buffer)


def usn_record_v3(
    *,
    frn: int,
    parent_frn: int,
    name: str,
    attributes: int = 0,
    usn: int = 0,
    timestamp_ns: int = 0,
    reason: int = 0,
) -> bytes:
    encoded = name.encode("utf-16-le")
    name_offset = 76
    length = name_offset + len(encoded)
    padding = (-length) % 8
    total = length + padding

    buffer = bytearray(total)
    struct.pack_into("<I", buffer, 0, total)
    struct.pack_into("<H", buffer, 4, 3)
    struct.pack_into("<H", buffer, 6, 0)
    buffer[8:24] = frn.to_bytes(16, "little")           # FILE_ID_128
    buffer[24:40] = parent_frn.to_bytes(16, "little")
    struct.pack_into("<q", buffer, 40, usn)
    struct.pack_into("<q", buffer, 48, ns_to_filetime(timestamp_ns))
    struct.pack_into("<I", buffer, 56, reason)
    struct.pack_into("<I", buffer, 60, 0)
    struct.pack_into("<I", buffer, 64, 0)
    struct.pack_into("<I", buffer, 68, attributes)
    struct.pack_into("<H", buffer, 72, len(encoded))
    struct.pack_into("<H", buffer, 74, name_offset)
    buffer[name_offset : name_offset + len(encoded)] = encoded
    return bytes(buffer)


def usn_buffer(records: list[bytes], *, cursor: int = 0) -> bytes:
    """Concatenate records behind the 8-byte cursor both IOCTLs prepend."""
    return struct.pack("<q", cursor) + b"".join(records)


def _dir_info_entry(
    *,
    name: str,
    name_offset: int,
    attributes: int,
    size: int,
    alloc_size: int,
    mtime_ns: int,
    ctime_ns: int,
    file_id: int | None,
    last: bool,
) -> bytes:
    encoded = name.encode("utf-16-le")
    length = name_offset + len(encoded)
    padding = (-length) % 8
    total = length + padding

    buffer = bytearray(total)
    struct.pack_into("<I", buffer, 0, 0 if last else total)
    struct.pack_into("<I", buffer, 4, 0)                       # FileIndex
    struct.pack_into("<q", buffer, 8, ns_to_filetime(ctime_ns))
    struct.pack_into("<q", buffer, 16, 0)                      # LastAccessTime
    struct.pack_into("<q", buffer, 24, ns_to_filetime(mtime_ns))
    struct.pack_into("<q", buffer, 32, 0)                      # ChangeTime
    struct.pack_into("<q", buffer, 40, size)                   # EndOfFile
    struct.pack_into("<q", buffer, 48, alloc_size)
    struct.pack_into("<I", buffer, 56, attributes)
    struct.pack_into("<I", buffer, 60, len(encoded))
    struct.pack_into("<I", buffer, 64, 0)                      # EaSize
    if file_id is not None:
        struct.pack_into("<b", buffer, 68, 0)                  # ShortNameLength
        struct.pack_into("<q", buffer, 96, file_id)
    buffer[name_offset : name_offset + len(encoded)] = encoded
    return bytes(buffer)


def full_dir_info(entries: list[dict]) -> bytes:
    """A ``FILE_FULL_DIR_INFO`` buffer (exFAT: no file ids)."""
    out = []
    for index, entry in enumerate(entries):
        out.append(
            _dir_info_entry(
                name=entry["name"],
                name_offset=68,
                attributes=entry.get("attributes", 0),
                size=entry.get("size", 0),
                alloc_size=entry.get("alloc_size", 0),
                mtime_ns=entry.get("mtime_ns", 0),
                ctime_ns=entry.get("ctime_ns", 0),
                file_id=None,
                last=index == len(entries) - 1,
            )
        )
    return b"".join(out)


def id_both_dir_info(entries: list[dict]) -> bytes:
    """A ``FILE_ID_BOTH_DIR_INFO`` buffer (NTFS: carries file ids)."""
    out = []
    for index, entry in enumerate(entries):
        out.append(
            _dir_info_entry(
                name=entry["name"],
                name_offset=104,
                attributes=entry.get("attributes", 0),
                size=entry.get("size", 0),
                alloc_size=entry.get("alloc_size", 0),
                mtime_ns=entry.get("mtime_ns", 0),
                ctime_ns=entry.get("ctime_ns", 0),
                file_id=entry.get("file_id", 0),
                last=index == len(entries) - 1,
            )
        )
    return b"".join(out)
